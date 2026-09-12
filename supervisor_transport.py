"""Transporte HTTP del adaptador contra las 5 rutas nativas (ADR-002).

Adjudicado en MARK:astra-gateway-404-and-sequence-20260908 §@infra y endurecido
por MARK:astra-transport-pending-20260908.

  · primera propuesta `supervisor_seq=1`; si no hay historial, pasa
  · `409 OBSERVATION_SEQUENCE_CONFLICT` -> validar `max_supervisor_seq`
    TOP-LEVEL (int acotado, nunca bool/None) y UN reintento con `max+1`
    y CLAVE NUEVA, porque el cuerpo cambia
  · respuesta PERDIDA -> reintentar MISMA clave y MISMO cuerpo, y **no avanzar
    NADA hasta resolver**, ni siquiera entre llamadas distintas
  · segundo conflicto -> visible, sin bucle
  · `status_seq` NO se usa jamás

🔑 La asimetría, que es fácil de invertir:
    conflicto  = la petición fue RECHAZADA  -> el cuerpo cambia (seq nueva)
                 -> CLAVE NUEVA (misma clave + bytes distintos = choque D4)
    perdida    = la petición PUDO APLICARSE -> el cuerpo NO cambia
                 -> MISMA clave y MISMOS bytes (D4 devuelve la misma observación)

🔒 Postura de red, deliberada: sin redirects (un 30x reenviaría el Bearer a otro
   origen), sólo `202` con cuerpo de observación validado (no cualquier 2xx con
   JSON arbitrario), lectura acotada, ids codificados como segmento de URL, y
   NUNCA se propaga el texto de un error de red ni el token — esos textos
   arrastran URLs y credenciales a los logs.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping

from supervisor_adapter import (
    ConflictoConcurrente,
    IdentidadCambiada,
    ObservacionInvalida,
    Observacion,
    SecuenciaSupervisor,
    valida,
)

CODIGO_CONFLICTO_SECUENCIA = "OBSERVATION_SEQUENCE_CONFLICT"
CODIGO_SUJETO_AUSENTE = "SUBJECT_NOT_FOUND"
MAX_CUERPO_RESPUESTA = 64 * 1024
# Campos de `coordination.RuntimeObservation` que el cliente EXIGE y COMPRUEBA.
# No basta con que la clave exista: `observation_id=None` o `supervisor_seq=True`
# pasaban la comprobación nominal anterior y limpiaban la incertidumbre.
CAMPOS_OBSERVACION_ESPERADOS = frozenset({
    "observation_id", "workload_id", "runtime_instance", "supervisor_seq",
    "replayed"})


class RespuestaPerdida(Exception):
    """No se sabe si el servidor aplicó la petición. NO es un rechazo."""


class PeticionPendiente(Exception):
    """Hay una petición sin resolver: no se puede observar otra cosa todavía."""


class RespuestaInvalida(Exception):
    """El servidor contestó algo que no es una observación aceptada."""


class ErrorDelGateway(Exception):
    def __init__(self, status: int, code: str, cuerpo: Mapping[str, Any]) -> None:
        super().__init__(f"{status} {code}")
        self.status = status
        self.code = code
        self.cuerpo = cuerpo


class _SinRedirects(urllib.request.HTTPRedirectHandler):
    """Un 30x reenviaría el `Authorization` a otro origen. No se sigue."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RespuestaInvalida(f"redirect {code} no permitido en el transporte")


class TransporteSupervisor:
    """Cliente de `POST /native/v1/runtimes/{runtime_instance}/observations`."""

    def __init__(self, base_url: str, token: str, *,
                 contexto: tuple | None = None,
                 enviar: Callable[..., tuple[int, dict]] | None = None,
                 timeout: float = 20.0) -> None:
        # 🔻 CORRIGE MARK:astra-transport-response-review-20260908 §2:
        # `exige_contexto()` existía, tenía test y NADIE la llamaba, así que mi
        # afirmación de que el transporte impedía heredar identidad era FALSA.
        # Ahora el contexto autenticado (sesión real + target) vive aquí y se
        # comprueba ANTES de reservar `siguiente()` y en cada reanudación.
        # `contexto=None` NO es identidad protegida: es camino sin verificar, y
        # se dice así en vez de llamarlo de otra manera.
        self._contexto = contexto
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._enviar = enviar or self._enviar_http
        self._opener = urllib.request.build_opener(_SinRedirects())
        # Petición en vuelo sin resultado definitivo. En MEMORIA: no es una
        # segunda base durable, es no olvidar lo que aún no se resolvió.
        self._pendiente: dict | None = None

    # ── transporte ──────────────────────────────────────────────────────────
    def _enviar_http(self, url: str, cuerpo: dict, clave: str) -> tuple[int, dict]:
        datos = json.dumps(cuerpo, sort_keys=True, separators=(",", ":")).encode()
        req = urllib.request.Request(url, data=datos, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Idempotency-Key", clave)
        try:
            with self._opener.open(req, timeout=self._timeout) as r:
                return r.status, self._leer(r)
        except urllib.error.HTTPError as exc:          # respuesta REAL del servidor
            return exc.code, self._leer(exc)
        except (urllib.error.URLError, TimeoutError, OSError):
            # Sin respuesta: la petición PUDO aplicarse. No es un rechazo.
            # El texto del error NO se propaga: arrastra URL y a veces credencial.
            raise RespuestaPerdida("sin respuesta del gateway") from None

    @staticmethod
    def _leer(r) -> dict:
        crudo = r.read(MAX_CUERPO_RESPUESTA + 1)
        if len(crudo) > MAX_CUERPO_RESPUESTA:
            raise RespuestaInvalida("respuesta por encima del límite de lectura")
        try:
            cuerpo = json.loads(crudo or b"{}")
        except ValueError:
            return {}
        # Una lista o un escalar reventarían más abajo o se reflejarían en un
        # mensaje. Se normaliza a error tipado SIN citar el cuerpo remoto.
        if not isinstance(cuerpo, dict):
            raise RespuestaInvalida("cuerpo de respuesta no es un objeto")
        return cuerpo

    # ── protocolo ───────────────────────────────────────────────────────────
    def observar(self, obs: Observacion, seq: SecuenciaSupervisor, *,
                 clave_base: str) -> dict:
        valida(obs)
        if self._contexto is not None:
            # 🔻 Borde cerrado (aviso 16:2xZ): `exige_contexto` deja pasar una
            # secuencia con `_contexto=None`, así que `identidad_verificada`
            # podía decir True sin que hubiera NADA ligado. Una afirmación de
            # identidad con una secuencia suelta es peor que no afirmar nada.
            if seq.contexto is None:
                raise IdentidadCambiada(
                    "el transporte afirma identidad ligada y la secuencia no "
                    "lleva contexto: átala a la misma tupla o construye el "
                    "transporte sin contexto (camino explícito NO verificado)")
            seq.exige_contexto(self._contexto)     # antes de reservar nada
        url = (f"{self._base}/native/v1/runtimes/"
               f"{urllib.parse.quote(obs.runtime_instance, safe='')}/observations")

        if self._pendiente is not None:
            # 🔻 CORRIGE MARK:astra-transport-pending-20260908: dos pérdidas
            # seguidas dejaban la petición sin resolver y la llamada siguiente
            # avanzaba `siguiente()` otra vez, duplicando el hecho o chocando en
            # D4. Ahora no se avanza NADA hasta resolver: se reanuda con los
            # MISMOS bytes y la MISMA clave.
            if (self._pendiente["url"] != url
                    or self._pendiente["obs"] != obs.como_kwargs()
                    or self._pendiente["seq_id"] != id(seq)):
                raise PeticionPendiente(
                    "hay una observación sin resolver: reanuda ESA antes de "
                    "mandar otra distinta — avanzar sobre incertidumbre puede "
                    "duplicar un hecho que el servidor ya aceptó")
            return self._bucle(url, self._pendiente["cuerpo"],
                               self._pendiente["clave"], clave_base,
                               self._pendiente["intento"], obs, seq)

        cuerpo = self._cuerpo(obs, seq.siguiente())
        return self._bucle(url, cuerpo, f"{clave_base}-0", clave_base, 0, obs, seq)

    def _bucle(self, url: str, cuerpo: dict, clave: str, clave_base: str,
               intento: int, obs: Observacion, seq: SecuenciaSupervisor) -> dict:
        while True:
            self._pendiente = {"url": url, "cuerpo": cuerpo, "clave": clave,
                               "intento": intento, "obs": obs.como_kwargs(),
                               # la reanudación exige la MISMA secuencia, no una
                               # equivalente: otra secuencia es otra identidad
                               "seq_id": id(seq)}
            try:
                status, respuesta = self._enviar(url, cuerpo, clave)
            except RespuestaPerdida:
                try:
                    status, respuesta = self._enviar(url, cuerpo, clave)
                except RespuestaPerdida:
                    # Segunda pérdida: la petición SIGUE pendiente y así queda.
                    # No se resuelve, no se avanza, y la próxima llamada reanuda.
                    raise

            if status == 202:
                # Un 202 mal formado NO resuelve la incertidumbre: se queda
                # pendiente a propósito, para que la reanudación mande los
                # mismos bytes en vez de dar por hecho un avance que no consta.
                self._valida_observacion(respuesta, cuerpo, obs)
                self._pendiente = None
                seq.exito()
                return respuesta

            code = str(respuesta.get("code", ""))
            if status == 409 and code == CODIGO_CONFLICTO_SECUENCIA:
                nuevo = seq.conflicto(respuesta.get("max_supervisor_seq"))
                intento += 1
                clave = f"{clave_base}-{intento}"     # CLAVE NUEVA: el cuerpo cambia
                cuerpo = self._cuerpo(obs, nuevo)
                continue                              # `seq.conflicto` corta al 2º

            # Rechazo definitivo: deja de estar pendiente, no se reintenta.
            self._pendiente = None
            if 200 <= status < 300:
                raise RespuestaInvalida(f"2xx inesperado ({status}): sólo 202 acredita")
            raise ErrorDelGateway(status, code, respuesta)

    @staticmethod
    def _valida_observacion(respuesta: Mapping[str, Any], cuerpo: Mapping[str, Any],
                            obs: Observacion) -> None:
        """Comprueba contra `coordination.RuntimeObservation`, no por presencia.

        El mensaje NUNCA cita el cuerpo remoto: reflejar claves ajenas mete texto
        de otro en mis logs.
        """
        faltan = CAMPOS_OBSERVACION_ESPERADOS - set(respuesta)
        if faltan:
            raise RespuestaInvalida(
                f"202 sin campos de observación: faltan {sorted(faltan)}")
        oid = respuesta["observation_id"]
        if not isinstance(oid, str) or not oid.strip():
            raise RespuestaInvalida("observation_id no es una cadena no vacía")
        seq_r = respuesta["supervisor_seq"]
        if (type(seq_r) is not int or isinstance(seq_r, bool)
                or seq_r != cuerpo["supervisor_seq"]):
            raise RespuestaInvalida(
                "supervisor_seq de la respuesta no es el entero que se envió")
        if respuesta["runtime_instance"] != obs.runtime_instance:
            raise RespuestaInvalida("runtime_instance de la respuesta no es el target")
        if respuesta["workload_id"] != obs.workload_id:
            raise RespuestaInvalida("workload_id de la respuesta no es el enviado")
        if type(respuesta["replayed"]) is not bool:
            raise RespuestaInvalida("replayed no es booleano")

    @property
    def identidad_verificada(self) -> bool:
        """`False` = no se ató contexto: el camino NO está protegido, y se dice."""
        return self._contexto is not None

    @property
    def pendiente(self) -> bool:
        return self._pendiente is not None

    @staticmethod
    def _cuerpo(obs: Observacion, seq_valor: int) -> dict:
        cuerpo = obs.como_kwargs()
        cuerpo.pop("runtime_instance", None)   # va en la RUTA, no en el cuerpo
        cuerpo.pop("idempotency_key", None)    # va en la CABECERA
        cuerpo["supervisor_seq"] = seq_valor
        return cuerpo
