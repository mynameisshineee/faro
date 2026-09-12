"""Puente estrecho entre identidad server-side y los sensores de M3.

No lee peticiones ni cabeceras. Sus entradas son ``SessionView``/``BoundSearchScope``
ya resueltos por M1/M2 o una identidad fija del workload para señales de ciclo de vida.
El runtime del gateway es una huella opaca del proceso, no un valor que el cliente pueda
declarar. El pool es acotado y comparte exportador/budget; captura de contenido y digests
permanecen apagados siempre en este borde.
"""
from __future__ import annotations

import hashlib
import os
import platform
import re
import threading
from collections import OrderedDict
from typing import Any, Callable

import observability as obs


_CANONICAL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")


def _resource_component(field: str, value: Any) -> str:
    """Preserva identidades canónicas; pseudonimiza las legacy sin publicarlas.

    El valor procede siempre del servidor, pero un censo anterior a M3 puede usar
    espacios, unicode o más de 64 bytes. Perder toda la telemetría de una operación
    válida por esa diferencia de contrato es peor que usar un identificador opaco.
    El dominio ``field`` evita que el mismo texto colisione entre rol/carril/runtime.
    """
    if isinstance(value, str) and _CANONICAL.fullmatch(value):
        return value
    raw = str(value).encode("utf-8", "surrogatepass")
    digest = hashlib.sha256(field.encode("ascii") + b"\0" + raw).hexdigest()[:32]
    return {"principal": "pid", "role": "role", "lane": "lane",
            "runtime_instance": "rti"}[field] + "_" + digest


def _source_identity(*, principal: Any, role: Any, lane: Any,
                     runtime_instance: Any, credential_generation: Any) -> tuple:
    """Identidad cruda derivada por el servidor, usada sólo para validar el cable."""
    return (principal, role, lane, runtime_instance, credential_generation)


def gateway_runtime_instance() -> str:
    crudo = (os.environ.get("LLMINBOX_RUNTIME_INSTANCE")
             or os.environ.get("HOSTNAME") or platform.node() or "gateway")
    return "gw_" + hashlib.sha256(crudo.encode("utf-8")).hexdigest()[:20]


def _acredita_bundle(candidate: Any, identity: obs.Identity):
    """Reacredita providers y Resource; no confía en los booleanos del DTO.

    ``TelemetryBundle`` es mutable y una factory es un borde de composición: puede
    entregar flags viejos junto a providers sustituidos. Se reconstruye el bundle
    desde los providers conservados en ``detalle`` para que instrumentos, Resource y
    estado provengan de los mismos objetos efectivos.
    """
    if candidate is None:
        return None
    if (getattr(candidate, "identity", None) != identity
            or getattr(candidate, "resource_verificado", None) is not True
            or getattr(candidate, "state", None) is not obs.Pipeline.READY):
        return None
    adapter = getattr(candidate, "adapter", None)
    detail = getattr(candidate, "detalle", None)
    if adapter is None or not isinstance(detail, dict):
        return None
    expected = {str(k): str(v)
                for k, v in adapter.resource_attributes(identity).items()}
    recorded = detail.get("resource_esperado")
    if not isinstance(recorded, dict) or {
            str(k): str(v) for k, v in recorded.items()} != expected:
        return None
    providers = tuple(detail.get(k) for k in (
        "tracer_provider", "meter_provider", "logger_provider"))
    if any(p is None or not obs._resource_casa(p, expected) for p in providers):
        return None
    try:
        rebuilt = obs.build_bundle(
            identity, tracer_provider=providers[0], meter_provider=providers[1],
            logger_provider=providers[2], adapter=adapter,
            registro=getattr(candidate, "registro", None),
            identity_budget=getattr(candidate, "identity_budget", None))
    except Exception:
        return None
    return rebuilt if rebuilt.state is obs.Pipeline.READY else None


def _bundle_still_valid(bundle: Any, identity: obs.Identity,
                        expected: dict[str, str]) -> bool:
    """Validación de último instante contra objetos efectivos y baseline inmutable."""
    if (getattr(bundle, "identity", None) != identity
            or getattr(bundle, "resource_verificado", None) is not True
            or getattr(bundle, "state", None) is not obs.Pipeline.READY):
        return False
    adapter = getattr(bundle, "adapter", None)
    detail = getattr(bundle, "detalle", None)
    if adapter is None or not isinstance(detail, dict):
        return False
    try:
        current = {str(k): str(v)
                   for k, v in adapter.resource_attributes(identity).items()}
    except Exception:
        return False
    if current != expected:
        return False
    recorded = detail.get("resource_esperado")
    if not isinstance(recorded, dict) or {
            str(k): str(v) for k, v in recorded.items()} != expected:
        return False
    providers = tuple(detail.get(k) for k in (
        "tracer_provider", "meter_provider", "logger_provider"))
    if any(p is None or not obs._resource_casa(p, expected) for p in providers):
        return False
    outputs = (
        obs._tiene_salida(providers[0], obs.SALIDA_TRACES),
        obs._tiene_salida(providers[1], obs.SALIDA_METRICAS),
        obs._tiene_salida(providers[2], obs.SALIDA_LOGS),
    )
    if not all(v is True for v in outputs):
        return False
    # Si el instrumento expone Resource, ése es el que usará al emitir y también
    # tiene que casar. Si no lo expone, el provider ya reacreditado es la autoridad.
    for instrument in (bundle.tracer, bundle.meter, bundle.logger):
        declares_resource = any(
            getattr(instrument, name, None) is not None
            for name in ("resource", "_resource", "_sdk_config"))
        if declares_resource and not obs._resource_casa(instrument, expected):
            return False
    return True


class _EmissionGuard:
    """Adapter fail-closed: revalida justo antes de entregar CADA señal."""
    nombre = "verified"

    def __init__(self, inner: Any, bundle: Any, identity: obs.Identity):
        self.inner = inner
        self.bundle = bundle
        self.identity = identity
        self.expected = {str(k): str(v) for k, v in
                         bundle.adapter.resource_attributes(identity).items()}

    def export(self, signal):
        if not _bundle_still_valid(self.bundle, self.identity, self.expected):
            # Sensor._emit lo convierte en NOT_EXPORTED+DEGRADED; el inner no ve bytes.
            raise obs.IdentityMismatch(
                "el bundle cambió después de acreditarse; se descarta la señal")
        return self.inner.export(signal)

    def flush(self, *args, **kwargs):
        if not _bundle_still_valid(self.bundle, self.identity, self.expected):
            return obs.ExportResult.NOT_EXPORTED
        flush = getattr(self.inner, "flush", None)
        return (flush(*args, **kwargs) if flush is not None
                else obs.ExportResult.ACCEPTED_BY_SDK)


class SensorPool:
    """Un sensor por Resource exacto, con memoria acotada y ninguna clave secreta."""

    def __init__(self, *, exporter: Any = None,
                 bundle_factory: Callable[[obs.Identity], Any] | None = None,
                 enabled: bool | None = None, max_identities: int = 512,
                 max_series: int = 512):
        self.exporter = exporter
        self.bundle_factory = bundle_factory
        self.enabled = enabled
        self.max_identities = max(1, int(max_identities))
        self.budget = obs.CardinalityBudget(max_series=max_series)
        self._sensores: OrderedDict[tuple, obs.Sensor] = OrderedDict()
        self._lock = threading.Lock()

    def sensor(self, *, principal: str, role: str, lane: str,
               runtime_instance: str, credential_generation: int = 0):
        source = _source_identity(
            principal=principal, role=role, lane=lane,
            runtime_instance=runtime_instance,
            credential_generation=credential_generation)
        try:
            identity = obs.Identity.server_derived(
                principal=_resource_component("principal", principal),
                role=_resource_component("role", role),
                lane=_resource_component("lane", lane),
                runtime_instance=_resource_component("runtime_instance", runtime_instance),
                credential_generation=credential_generation)
        except obs.ObservabilityError:
            return None
        # La caché se separa por los bytes server-side, no sólo por su pseudónimo.
        # Una colisión criptográfica no puede mezclar atribución entre sesiones.
        key = source
        with self._lock:
            existente = self._sensores.get(key)
            if existente is not None:
                self._sensores.move_to_end(key)
                return existente
            try:
                candidate = self.bundle_factory(identity) if self.bundle_factory else None
                bundle = _acredita_bundle(candidate, identity) if candidate is not None else None
                if self.bundle_factory is not None and bundle is None:
                    return None
                exporter_bundle = getattr(self.exporter, "bundle", None)
                exporter = self.exporter
                if exporter_bundle is not None:
                    # El exportador debe citar exactamente el candidato reacreditado;
                    # se vuelve a atar al bundle reconstruido para no usar sus
                    # instrumentos/flags mutables originales.
                    if exporter_bundle is not candidate:
                        return None
                    exporter = obs.OtelExporter(bundle)
                if bundle is not None:
                    exporter = _EmissionGuard(
                        exporter or obs.OtelExporter(bundle), bundle, identity)
                sensor = obs.build(
                    identity, obs.Trust.GATEWAY, enabled=self.enabled,
                    exporter=exporter, bundle=bundle, budget=self.budget,
                    capture_content=False, content_digest=False, strict=False)
            except Exception:
                return None
            # Marca privada para que Journal/SearchStore puedan comprobar que una
            # factory defectuosa no devolvió el sensor de otra sesión. No se exporta.
            sensor._llminbox_source_identity = source
            self._sensores[key] = sensor
            while len(self._sensores) > self.max_identities:
                self._sensores.popitem(last=False)
            return sensor

    def for_session(self, session):
        return self.sensor(
            principal=session.principal_id, role=session.role, lane=session.lane,
            runtime_instance=session.runtime_instance,
            credential_generation=session.generation)

    def for_search_scope(self, scope):
        if not getattr(scope, "principal_id", None) or not getattr(scope, "role", None):
            return None
        return self.sensor(
            principal=scope.principal_id, role=scope.role, lane=scope.lane,
            runtime_instance=gateway_runtime_instance())

    def for_search_lifecycle(self):
        """Sensor del workload, sin fingir que una petición disparó la sonda.

        El ciclo de vida no tiene una credencial ni un sujeto cliente. Atribuir su
        degradación al último ``BoundSearchScope`` mezclaría dos identidades; dejarla
        sin Resource haría la señal imposible de asignar a una instancia. Los tres
        componentes son constantes server-side y el runtime conserva la huella opaca
        que ya usa el gateway.
        """
        return self.sensor(
            principal="llminbox-gateway", role="gateway", lane="control",
            runtime_instance=gateway_runtime_instance())


def sensor_matches(sensor: Any, *, principal: Any, role: Any, lane: Any,
                   runtime_instance: Any, credential_generation: Any = 0) -> bool:
    """Comprueba origen exacto Y Resource efectivo antes de atribuir una señal."""
    source = _source_identity(
        principal=principal, role=role, lane=lane,
        runtime_instance=runtime_instance,
        credential_generation=credential_generation)
    if getattr(sensor, "_llminbox_source_identity", source) != source:
        return False
    ident = getattr(sensor, "identity", None)
    if ident is None:
        return False
    expected = (
        _resource_component("principal", principal),
        _resource_component("role", role),
        _resource_component("lane", lane),
        _resource_component("runtime_instance", runtime_instance),
        credential_generation,
    )
    if (ident.principal, ident.role, ident.lane, ident.runtime_instance,
            ident.credential_generation) != expected:
        return False
    adapter = getattr(sensor, "adapter", None)
    if adapter is None:
        return False
    resource = adapter.resource_attributes(ident)
    return (
        resource.get("llminbox.principal") == expected[0]
        and resource.get("llminbox.role") == expected[1]
        and resource.get("llminbox.lane") == expected[2]
        and resource.get("service.instance.id") == expected[3]
        and resource.get("llminbox.credential_generation") == expected[4]
    )
