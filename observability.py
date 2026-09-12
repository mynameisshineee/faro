"""M3 · sensores externos y export OpenTelemetry — el kernel de observabilidad.

Contrato de referencia: `agent/references/llminbox/M3-OBSERVABILITY-CONTRACT.md`
(cto, 2026-09-05). Este módulo implementa el NÚCLEO de ese contrato y nada más:
no importa FastAPI, no toca `servicio.py`, no conoce el journal de M1 ni el índice
de M2. M1 y M2 lo importan; él no los importa a ellos.

CINCO REGLAS QUE ESTRUCTURAN TODO EL FICHERO, y de las que sale cada decisión rara:

  ① EL AGENTE NO ES UNA FUENTE. Una señal cuenta como sensor si y sólo si sigue
    existiendo cuando el proceso del agente decide no colaborar. Sólo hay dos
    emisores de confianza: el GATEWAY (servidor, ya resuelve identidad) y el
    SUPERVISOR (fuera del espacio de direcciones del agente). `Trust.AGENT` existe
    para poder RECHAZARLO por nombre, no para usarlo.

  ② LOS HECHOS DURABLES MANDAN SOBRE LA TELEMETRÍA. El progreso sólo se alimenta de
    `Fact`, que exige `source="journal"`. Un contador en memoria o un latido NUNCA
    pueden acreditar progreso: un reinicio devolvería «sano» a un agente muerto.

  ③ LA AUSENCIA NO ES SALUD. Ni la del SDK, ni la del exportador, ni la del latido.
    Cada una tiene su estado NOMBRADO, y la partición del detector no tiene rama
    por defecto benigna: lo que no casa con ninguna casilla es `INDETERMINADO`.

  ④ LA CARDINALIDAD LA ELIGE QUIEN DEFIENDE. Vocabulario cerrado por etiqueta,
    desbordamiento a un cubo único, y presupuesto duro de series. Un identificador
    (`event_id`, `receipt_id`, `runtime_instance`, `principal`, URI, texto libre)
    JAMÁS es etiqueta de métrica: vive en spans y logs, cuyo volumen se acota por
    muestreo, no por producto cartesiano.

  ⑤ NADA DE ESTO PUEDE ROMPER EL KERNEL. Falta el SDK → no-op determinista. Falla el
    exportador → se cuenta y se degrada, no se propaga. Una etiqueta prohibida →
    se cuenta y NO se emite. `strict=True` sólo existe para que los tests puedan
    ver la violación; en producción el valor por defecto es `False`.

APAGADO POR DEFECTO: `build()` sin `enabled=True` (o sin `LLMINBOX_OBS=1`) devuelve
un sensor que no exporta nada y ni siquiera intenta importar el SDK.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import threading
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

# ─────────────────────────────────────────────────────────────────────────────
# Versión del esquema PROPIO, y el pin de la convención ajena
# ─────────────────────────────────────────────────────────────────────────────
# Dos versiones distintas a propósito. La nuestra la controlamos; la de OTel no.
# `STATE-OF-THE-ART.md` lo dice literal: «las convenciones y URLs han cambiado. El
# mapping de M3 [debe aislarse]». Un `SchemaAdapter` es esa junta: los sitios de
# llamada nombran conceptos CANÓNICOS nuestros y el adaptador decide cómo se
# escriben hacia fuera. Cuando GenAI semconv rompa, se cambia un adaptador y no
# doscientas llamadas.
SCHEMA = "llminbox.m3.v1"
OTEL_SEMCONV_PIN = "1.37.0"


# ─────────────────────────────────────────────────────────────────────────────
# Errores ACOTADOS. Todos derivan de uno: quien nos importe puede capturar la
# familia entera sin enumerar, que es la condición para que M1/M2 puedan blindar
# su camino de mutación con un solo `except`.
# ─────────────────────────────────────────────────────────────────────────────
class ObservabilityError(Exception):
    """Raíz de todo lo que este módulo puede lanzar. Nada escapa fuera de aquí."""


class SchemaError(ObservabilityError):
    """La señal no respeta el esquema (etiqueta prohibida, clave desconocida)."""


class ForbiddenLabel(SchemaError):
    """Un identificador o texto libre intentó ser etiqueta de métrica."""


class UntrustedSource(ObservabilityError):
    """La señal viene del agente, o la identidad la propuso el cliente."""


class CardinalityBudgetExceeded(ObservabilityError):
    """El presupuesto de series se agotó. En modo laxo se desborda, no se lanza."""


class SensitiveDataRejected(ObservabilityError):
    """Contenido sensible en una señal con captura desactivada."""


class IdentityMismatch(ObservabilityError):
    """El sensor y el bundle no son de la misma identidad.

    No es un detalle de higiene: el Resource lo fijan los PROVIDERS al construirse
    y ya no cambia por señal, así que un bundle de otra identidad emite SU Resource
    con NUESTRAS etiquetas. Medido: bundle `lane-a` + sensor `lane-b` produce
    `resource.llminbox.lane=lane-a` y `datapoint.lane=lane-b` — el aislamiento por
    carril roto en el sitio donde nadie mira, y encima con las dos mitades
    contradiciéndose dentro del mismo dato.
    """


class ExporterUnavailable(ObservabilityError):
    """El exportador no entrega. NUNCA se propaga al camino de mutación."""


# ─────────────────────────────────────────────────────────────────────────────
# Confianza e identidad
# ─────────────────────────────────────────────────────────────────────────────
class Trust(Enum):
    GATEWAY = "gateway"
    SUPERVISOR = "supervisor"
    AGENT = "agent"          # existe para RECHAZARLO por nombre


TRUSTED = frozenset({Trust.GATEWAY, Trust.SUPERVISOR})

# `runtime_instance` es un id OPACO emitido por el servidor (ADR-001), asi que el
# alfabeto admite mayusculas y `:`: una regex mas estrecha habria rechazado la
# identidad que M1 emite y este modulo existe para consumir.
_RE_CAMPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")


@dataclass(frozen=True)
class Identity:
    """Los cuatro campos de ADR-001, resueltos POR EL SERVIDOR. Más la generación.

    No hay constructor público que acepte datos de cliente: `server_derived` es la
    única puerta, y `from_untrusted` existe únicamente para rechazar y dejarlo
    escrito. ADR-001 §Identity model: «A client-supplied principal, role, lane or
    runtime instance is rejected rather than silently ignored» — «rejected», no
    «ignored», así que tiene que haber algo que lance.
    """
    principal: str
    role: str
    lane: str
    runtime_instance: str
    credential_generation: int = 0

    @classmethod
    def server_derived(cls, *, principal: str, role: str, lane: str,
                       runtime_instance: str, credential_generation: int = 0) -> "Identity":
        for nombre, v in (("principal", principal), ("role", role),
                          ("lane", lane), ("runtime_instance", runtime_instance)):
            if not isinstance(v, str) or not _RE_CAMPO.match(v):
                raise SchemaError(f"{nombre} no es una forma canónica: {v!r}")
        if not isinstance(credential_generation, int) or credential_generation < 0:
            raise SchemaError("credential_generation tiene que ser un entero >= 0")
        return cls(principal, role, lane, runtime_instance, credential_generation)

    @classmethod
    def from_untrusted(cls, payload: Mapping[str, Any]) -> "Identity":
        """Siempre lanza. Es el falsador F-FORGE convertido en código.

        Un proceso de agente que emita OTLP declarando otro `principal` o
        `runtime_instance` no puede tener una ruta que lo acepte «si los campos
        parecen bien». La forma correcta no es un validador más estricto: es que
        NO EXISTA la función que construye identidad desde el cliente.
        """
        raise UntrustedSource(
            "la identidad no se acepta del cliente: se deriva de la sesión "
            f"(campos ofrecidos: {sorted(payload)})")

    @property
    def series_key(self) -> tuple:
        return (self.principal, self.role, self.lane, self.runtime_instance)


# ─────────────────────────────────────────────────────────────────────────────
# Vocabulario de etiquetas — allowlist de CLAVES + vocabulario cerrado de VALORES
# ─────────────────────────────────────────────────────────────────────────────
# El precedente es del propio repo: `coordination.py` cierra los motivos de rechazo
# porque «un motivo libre acaba llevando la clave, el recurso o el id que lo
# provocó, y entonces cada rechazo es una fila nueva —la cardinalidad la elige
# quien ataca, no quien defiende—». Y `servicio.py` guarda coste por PLANTILLA de
# ruta, no por URL concreta. Misma regla, otro soporte.
LABELS_PERMITIDAS = frozenset({
    "lane", "verb", "kind", "state", "reason", "op",
    "outcome", "subject_kind", "tool_class", "exit_class",
})

# DENYLIST EXPLÍCITA. Redundante con la allowlist a propósito: una allowlist dice
# «esto pasa», y quien añade una clave nueva tiende a añadirla a la allowlist sin
# pensar. La denylist nombra a los sospechosos de siempre, así que ampliar la
# allowlist con uno de ellos choca contra una regla que alguien escribió a mano.
LABELS_PROHIBIDAS = frozenset({
    "event_id", "receipt_id", "transition_id", "command_id", "workstream_id",
    "entry_eid", "effect_id", "resource", "key", "idempotency_key",
    "credential_ref", "fingerprint", "ledger", "path", "uri", "url",
    "recipient", "head", "body", "principal", "role", "runtime_instance",
    "session", "token", "trace_id", "span_id", "task", "task_id",
})

OVERFLOW = "__overflow__"

# `lane` NO está aquí: no es una dimensión que el llamante proponga. Ver `_labels`.
VOCABULARIO: dict[str, frozenset[str]] = {
    "verb": frozenset({"inform", "request", "deliver", "claim", "close", "ack"}),
    "kind": frozenset({"DELIVERED", "REQUEST", "FINDING", "RULING", "AMEND"}),
    "state": frozenset({"accepted", "materialized", "indexed", "delivered",
                        "delivery_progress", "materialization_failed",
                        "materialization_exhausted",
                        "failed", "received", "executing", "succeeded",
                        "cancelled", "superseded", "pending", "abandoned"}),
    "reason": frozenset({"lane_mismatch", "idempotency_conflict", "fencing_conflict",
                         "cause_rejected", "attribution_rejected", "revision_conflict",
                         "read_only", "expired", "revoked", "unknown_credential",
                         "schema_too_new", "other"}),
    "op": frozenset({"acquire", "renew", "release", "takeover", "fence_reject"}),
    "outcome": frozenset({"ok", "retry", "failed", "drop", "error",
                          "materialized", "not_configured"}),
    "subject_kind": frozenset({"event", "command", "denial", "denial_aggregate"}),
    "tool_class": frozenset({"read", "write", "network", "process", "other"}),
    "exit_class": frozenset({"clean", "error", "killed", "timeout", "unknown"}),
}

# DOS NIVELES, Y LA DIFERENCIA NO ES DE GRADO SINO DE TRATAMIENTO.
#
# · SECRETOS (credencial, contraseña, token, api_key, authorization, cookie): NUNCA
#   sale de ellos un sha256 estable. Un digest sin clave de un secreto es un ORÁCULO
#   DE CONFIRMACIÓN: si el espacio de valores es adivinable —y las credenciales de
#   una flota lo son: prefijo conocido, longitud fija— el que ve el digest puede
#   probar candidatos offline hasta acertar. Y aunque no lo sea, es un correlador
#   estable que enlaza al mismo sujeto entre sistemas para siempre. Sale un MARCADOR
#   y la longitud; si el operador configura una clave aparte, un HMAC con ESA clave,
#   que ya no se puede recomputar sin ella.
#   `capture_content=True` NO los desbloquea: encender la captura de prompts es una
#   decisión de depuración, y colgar de ella la fuga de credenciales convertiría una
#   perilla de diagnóstico en una perilla de exfiltración.
# · CONTENIDO (prompt, body, tool_args, resultados): fuera por defecto igual, pero
#   aquí el digest SÍ es útil para correlacionar dos ocurrencias del mismo texto —
#   y por eso hay que PEDIRLO explícitamente (`content_digest=True`).
CLAVES_SECRETAS = frozenset({
    "secret", "secrets", "password", "passwd", "pass", "token", "tokens",
    "access_token", "refresh_token", "session_token", "bearer", "api_key",
    "apikey", "authorization", "auth", "cookie", "cookies", "credential",
    "credentials", "credential_ref", "private_key", "signing_key", "pepper",
})

CLAVES_CONTENIDO = frozenset({
    "prompt", "prompts", "completion", "body", "head", "text", "content",
    "message", "messages", "tool_args", "tool_arguments", "arguments",
    "tool_result", "result", "output", "input",
})

# Fuera por defecto de TODA señal, no sólo de las métricas: un `body` en un atributo
# de span es la misma fuga con menos volumen.
CLAVES_SENSIBLES = CLAVES_SECRETAS | CLAVES_CONTENIDO

# SUFIJOS/TOKENS DE SECRETO. La comparación por igualdad exacta dejaba pasar
# `x-api-key`, `Authorization-Header`, `db.password`, `user_token_v2`… — o sea la
# forma en que las claves se llaman DE VERDAD en un payload real. Se normaliza el
# separador (todo a `_`) y se mira por TOKEN, no por cadena completa.
SEPARADORES = re.compile(r"[-.\s:/]+")
SUFIJOS_RUIDO = ("_v1", "_v2", "_hdr", "_header", "_raw", "_value", "_val", "_str")


def normaliza_clave(k: Any) -> str:
    """`X-Api-Key` -> `x_api_key`. Separador único y ruido de sufijo fuera."""
    s = SEPARADORES.sub("_", str(k).strip().lower())
    s = re.sub(r"_+", "_", s).strip("_")
    for suf in SUFIJOS_RUIDO:
        if s.endswith(suf) and len(s) > len(suf) + 1:
            s = s[: -len(suf)]
            break
    return s


def clase_de_clave(k: Any) -> str:
    """`secreto` | `contenido` | `normal`, mirando TOKENS de la clave normalizada."""
    n = normaliza_clave(k)
    if n in CLAVES_SECRETAS or n in CLAVES_CONTENIDO:
        return "secreto" if n in CLAVES_SECRETAS else "contenido"
    partes = set(n.split("_"))
    if partes & CLAVES_SECRETAS or any(n.endswith("_" + s) for s in CLAVES_SECRETAS):
        return "secreto"
    if partes & CLAVES_CONTENIDO or any(n.endswith("_" + s) for s in CLAVES_CONTENIDO):
        return "contenido"
    return "normal"


def contiene_secreto(val: Any, *, depth: int = 0, max_depth: int = 6) -> bool:
    """¿Hay un secreto EN ALGÚN SITIO dentro de esta estructura?

    Se corre ANTES de cualquier digest. Sin esto, un valor de contenido que
    envuelve una credencial —`{"body": {"headers": {"authorization": "Bearer …"}}}`—
    publicaba un sha256 ESTABLE del conjunto: y un digest estable de algo que
    contiene un secreto es el mismo oráculo de confirmación, sólo que con un
    envoltorio que lo hace parecer inocente.
    """
    if depth > max_depth:
        return True                      # no se pudo mirar hasta el fondo: se asume
    if isinstance(val, Mapping):
        for k, x in val.items():
            if clase_de_clave(k) == "secreto":
                return True
            if contiene_secreto(x, depth=depth + 1, max_depth=max_depth):
                return True
        return False
    if isinstance(val, (list, tuple, set, frozenset)):
        return any(contiene_secreto(x, depth=depth + 1, max_depth=max_depth)
                   for x in val)
    return False


MARCA_REDACTADO = "__redacted__"
MARCA_PROFUNDIDAD = "__depth_exceeded__"
MARCA_TRUNCADO = "__truncated__"

# NOMBRES DE SEÑAL, CERRADOS. Sin esto, `canonico` es texto libre del llamante y la
# cardinalidad se escapa POR EL NOMBRE en vez de por la etiqueta: un `count(f"job.{id}")`
# crea una serie por id sin tocar una sola etiqueta prohibida. Cerrar las etiquetas y
# dejar abierto el nombre es cerrar la puerta y dejar la ventana.
METRICAS = frozenset({
    "events.accepted", "receipts.transitions", "outbox.attempts", "denials",
    "sessions.issued", "sessions.revoked", "leases.transitions",
    "commands.transitions", "runtime.starts", "runtime.stops", "tool.failures",
    "sensor.export", "sensor.schema_violations", "outbox.pending",
    "outbox.oldest_pending_age_s", "leases.held", "runtime.liveness_state",
    "sensor.staleness_s", "events.accept_duration_s",
    "outbox.materialization_lag_s", "runtime.heartbeat_interval_s",
})

SPANS = frozenset({
    "coordination.session.issue", "coordination.session.refresh",
    "coordination.event.accept", "coordination.lease.acquire",
    "coordination.command.transition", "outbox.materialize", "runtime.job",
})

LOGS = frozenset({
    "policy.denied", "runtime.start", "runtime.stop", "heartbeat.missed",
    "sensor.export_failed", "repair.required",
})

# CÓDIGOS DE ERROR ACOTADOS. Lo que llega a un hook —y de ahí a una alarma, a un log
# o a un panel— no puede ser `str(excepción)`: el texto de una excepción arrastra la
# URL, el fichero, la fila y, cuando la lanza una librería ajena, el valor que la
# provocó. Un secreto puede viajar dentro de un mensaje de error igual que dentro de
# un campo, y el mensaje de error es el sitio donde nadie lo busca.
CODIGOS_ERROR = {
    "ExporterUnavailable": "exporter_unavailable",
    "TimeoutError": "timeout",
    "ConnectionError": "connection",
    "ConnectionRefusedError": "connection",
    "BrokenPipeError": "connection",
    "OSError": "io",
    "ValueError": "invalid",
    "TypeError": "invalid",
    "MemoryError": "resource",
}


def codigo_error(e: BaseException) -> str:
    """Clase -> código de un conjunto CERRADO. Nunca el mensaje."""
    return CODIGOS_ERROR.get(type(e).__name__, "other")


# BAGGAGE: se PROPAGA aguas abajo, potencialmente fuera de nuestra frontera. Su
# allowlist es la más corta del fichero y no coincide con la de etiquetas: `lane`
# porque es la clave de correlación mínima, `schema` para que el receptor sepa
# leerlo. Nada más. El principal NO va: en baggage sería un correlador de
# identidad viajando por una cabecera que cualquiera reenvía.
BAGGAGE_PERMITIDO = frozenset({"lane", "schema"})


def _valor_acotado(clave: str, valor: Any) -> str:
    """Vocabulario cerrado con DESBORDAMIENTO, no con rechazo.

    Rechazar un valor desconocido perdería el hecho; dejarlo pasar dejaría que el
    llamante elija la cardinalidad. El desbordamiento conserva el hecho y acota el
    coste: todo lo que no está en el vocabulario cuenta, pero cuenta JUNTO.
    """
    v = valor if isinstance(valor, str) else str(valor)
    permitidos = VOCABULARIO.get(clave)
    # SIN VOCABULARIO NO HAY VALOR. Antes, un vocabulario vacío significaba «abierto
    # pero acotado por el mapa de credenciales» y devolvía la cadena del llamante:
    # una puerta de valor libre disfrazada de acotación. Hoy no existe esa rama —
    # lo que no tiene lista de valores no es una etiqueta.
    if not permitidos:
        return OVERFLOW
    return v if v in permitidos else OVERFLOW


# ─────────────────────────────────────────────────────────────────────────────
# Redacción
# ─────────────────────────────────────────────────────────────────────────────
def huella(valor: Any, n: int = 12) -> str:
    """sha256 truncado. SÓLO para contenido, y sólo si se pide: ver `CLAVES_SECRETAS`."""
    b = valor if isinstance(valor, bytes) else str(valor).encode("utf-8")
    return hashlib.sha256(b).hexdigest()[:n]


def huella_con_clave(valor: Any, clave: bytes, n: int = 12) -> str:
    """HMAC con una clave SEPARADA. Sin la clave no se puede recomputar, así que
    deja de ser un oráculo offline y sigue sirviendo para correlacionar."""
    b = valor if isinstance(valor, bytes) else str(valor).encode("utf-8")
    return hmac.new(clave, b, hashlib.sha256).hexdigest()[:n]


def _largo(v: Any) -> int:
    return len(v) if isinstance(v, (str, bytes, list, tuple, dict)) else len(str(v))


def redacta(attrs: Mapping[str, Any], *, capture_content: bool = False,
            content_digest: bool = False, hmac_key: bytes | None = None,
            strict: bool = False, max_depth: int = 6,
            max_items: int = 64) -> dict[str, Any]:
    """Redacta RECURSIVAMENTE mappings y sequences.

    La primera versión sólo miraba el primer nivel, y eso no es «cubrir el caso
    común»: es no cubrir el caso REAL. Un atributo de span es casi siempre un
    contexto anidado —`{"job": {"tool": {"tool_args": ...}}}`, una lista de
    mensajes— y la clave sensible vive dentro, no arriba. Un barrido que sólo mira
    la superficie da verde sobre exactamente los payloads que importan.

    Dos cotas, y las dos son de seguridad y no de estilo: `max_depth` porque una
    estructura cíclica o muy honda convertiría al redactor en el DoS del proceso
    que lo llama, y `max_items` porque una lista enorme haría lo mismo por volumen.
    Al pasarse NO se emite el resto en crudo: se emite un marcador.
    """
    return _scrub_mapa(attrs, capture_content=capture_content,
                       content_digest=content_digest, hmac_key=hmac_key,
                       strict=strict, depth=0, max_depth=max_depth,
                       max_items=max_items)


def _campos_secreto(k: str, v: Any, hmac_key: bytes | None) -> dict[str, Any]:
    salida: dict[str, Any] = {f"{k}_redacted": MARCA_REDACTADO, f"{k}_len": _largo(v)}
    if hmac_key:
        salida[f"{k}_hmac"] = huella_con_clave(v, hmac_key)
    return salida


def _scrub_mapa(m: Mapping[str, Any], *, capture_content: bool, content_digest: bool,
                hmac_key: bytes | None, strict: bool, depth: int, max_depth: int,
                max_items: int) -> dict[str, Any]:
    if depth > max_depth:
        return {"_": MARCA_PROFUNDIDAD}
    fuera: dict[str, Any] = {}
    for n, (k, val) in enumerate(m.items()):
        if n >= max_items:
            fuera["_truncated"] = MARCA_TRUNCADO
            break
        clase = clase_de_clave(k)
        if clase == "secreto":
            # NUNCA un digest sin clave, y `capture_content` no lo desbloquea.
            if strict:
                raise SensitiveDataRejected(f"{k} es un secreto: no se emite nunca")
            fuera.update(_campos_secreto(str(k), val, hmac_key))
            continue
        if clase == "contenido" and not capture_content:
            if strict:
                raise SensitiveDataRejected(f"{k} es contenido y la captura está apagada")
            fuera[f"{k}_redacted"] = MARCA_REDACTADO
            fuera[f"{k}_len"] = _largo(val)
            # EL BARRIDO VA ANTES DEL DIGEST. Un contenido que ENVUELVE un secreto
            # no puede publicar un sha256 estable: sería el mismo oráculo con
            # envoltorio inocente. Marcador y longitud, o HMAC si hay clave.
            if content_digest:
                if contiene_secreto(val, max_depth=max_depth):
                    if hmac_key:
                        fuera[f"{k}_hmac"] = huella_con_clave(val, hmac_key)
                    fuera[f"{k}_digest_omitido"] = "secreto_anidado"
                else:
                    fuera[f"{k}_sha256"] = huella(val)
            continue
        fuera[str(k)] = _scrub_valor(val, capture_content=capture_content,
                                     content_digest=content_digest, hmac_key=hmac_key,
                                     strict=strict, depth=depth + 1,
                                     max_depth=max_depth, max_items=max_items)
    return fuera


def _scrub_valor(val: Any, *, capture_content: bool, content_digest: bool,
                 hmac_key: bytes | None, strict: bool, depth: int, max_depth: int,
                 max_items: int) -> Any:
    if depth > max_depth:
        return MARCA_PROFUNDIDAD
    if isinstance(val, Mapping):
        return _scrub_mapa(val, capture_content=capture_content,
                           content_digest=content_digest, hmac_key=hmac_key,
                           strict=strict, depth=depth, max_depth=max_depth,
                           max_items=max_items)
    # `str`/`bytes` son Sequence: excluirlos explícitamente o el redactor se pondría
    # a recorrer caracteres y a devolver una lista de letras del secreto.
    if isinstance(val, (list, tuple, set, frozenset)):
        items = list(val)[:max_items]
        salida = [_scrub_valor(x, capture_content=capture_content,
                               content_digest=content_digest, hmac_key=hmac_key,
                               strict=strict, depth=depth + 1, max_depth=max_depth,
                               max_items=max_items) for x in items]
        if len(val) > max_items:
            salida.append(MARCA_TRUNCADO)
        return salida
    return val


# ─────────────────────────────────────────────────────────────────────────────
# Adaptador de convención — el pin detrás de una junta
# ─────────────────────────────────────────────────────────────────────────────
class SchemaAdapter:
    """Traduce conceptos CANÓNICOS nuestros al vocabulario de salida.

    Los sitios de llamada nunca escriben un nombre de OTel. Cuando la convención
    ajena cambie —y ya lo ha hecho— se sustituye una subclase.
    """
    version = SCHEMA

    def metric_name(self, canonico: str) -> str:
        raise NotImplementedError

    def span_name(self, canonico: str) -> str:
        raise NotImplementedError

    def label_key(self, canonico: str) -> str:
        raise NotImplementedError

    def resource_attributes(self, ident: Identity) -> dict[str, Any]:
        raise NotImplementedError


class LlminboxV1Adapter(SchemaAdapter):
    """El nuestro, y el DEFECTO. Estable porque lo controlamos."""
    version = SCHEMA

    def metric_name(self, canonico: str) -> str:
        return f"llminbox.{canonico}"

    def span_name(self, canonico: str) -> str:
        return canonico

    def label_key(self, canonico: str) -> str:
        return canonico

    def resource_attributes(self, ident: Identity) -> dict[str, Any]:
        # LA IDENTIDAD VIVE AQUÍ Y NO EN LAS ETIQUETAS. Es el recurso del emisor,
        # fijo por proceso/sesión, no una dimensión que el llamante elige.
        return {
            "service.name": "llminbox",
            "service.instance.id": ident.runtime_instance,
            "llminbox.principal": ident.principal,
            "llminbox.role": ident.role,
            "llminbox.lane": ident.lane,
            "llminbox.credential_generation": ident.credential_generation,
            "llminbox.schema": self.version,
        }


class OtelSemconvAdapter(LlminboxV1Adapter):
    """Mapea a la convención GenAI de OTel, PINNEADA a una versión concreta.

    Subclase y no reemplazo: lo que no tiene equivalente estable en la convención
    ajena se queda con nuestro nombre en vez de inventar uno que mañana choque.
    """
    def __init__(self, pin: str = OTEL_SEMCONV_PIN):
        self.pin = pin
        self.version = f"otel.semconv/{pin}"

    def resource_attributes(self, ident: Identity) -> dict[str, Any]:
        attrs = super().resource_attributes(ident)
        attrs["llminbox.schema"] = self.version
        attrs["otel.semconv.version"] = self.pin
        return attrs


# ─────────────────────────────────────────────────────────────────────────────
# Señales y exportadores
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Signal:
    tipo: str                       # "metric" | "span" | "log"
    nombre: str
    valor: Any
    labels: Mapping[str, str] = field(default_factory=dict)
    attributes: Mapping[str, Any] = field(default_factory=dict)
    resource: Mapping[str, Any] = field(default_factory=dict)
    links: Sequence[Mapping[str, Any]] = ()
    at: float = 0.0


class Exporter(Protocol):
    def export(self, signal: Signal) -> "ExportResult": ...


class NullExporter:
    """No-op DETERMINISTA. No guarda, no aloja, no falla, no crece.

    Es el defecto. Que sea determinista importa: un no-op que devolviera ids
    aleatorios haría que un test verde con la observabilidad apagada dejara de ser
    reproducible, y entonces «apagado» y «encendido» no serían comparables.
    """
    nombre = "null"

    def export(self, signal: Signal) -> ExportResult:
        # NUNCA suma éxito ni toca «último éxito». Un no-op que contara entregas
        # haría que `sensor.staleness_s` mirase joven con la observabilidad
        # apagada: el silencio se leería como salud desde el propio instrumento.
        return ExportResult.NOT_EXPORTED


class MemoryExporter:
    """El exportador FALSO de los tests. Guarda todo lo que se le entrega.

    Existe para que un test pueda afirmar sobre lo que SALE, no sobre lo que el
    emisor cree haber mandado. Es la única forma de comprobar que un dato sensible
    no aparece: mirar el código de redacción es leer la intención.
    """
    nombre = "memory"

    def __init__(self) -> None:
        self.signals: list[Signal] = []

    def export(self, signal: Signal) -> ExportResult:
        self.signals.append(signal)
        return ExportResult.ACCEPTED_BY_SDK

    # — utilidades de aserción, para que los tests no reimplementen el filtro —
    def metrics(self) -> list[Signal]:
        return [s for s in self.signals if s.tipo == "metric"]

    def spans(self) -> list[Signal]:
        return [s for s in self.signals if s.tipo == "span"]

    def logs(self) -> list[Signal]:
        return [s for s in self.signals if s.tipo == "log"]

    def series(self) -> set[tuple]:
        return {(s.nombre, tuple(sorted(s.labels.items()))) for s in self.metrics()}

    def volcado(self) -> str:
        """TODO lo exportado, aplanado a texto. Para buscar un secreto dentro."""
        trozos: list[str] = []
        for s in self.signals:
            trozos.append(s.nombre)
            for d in (s.labels, s.attributes, s.resource):
                for k, v in d.items():
                    trozos.append(f"{k}={v}")
            for enlace in s.links:
                for k, v in enlace.items():
                    trozos.append(f"{k}={v}")
        return "\n".join(trozos)


class FailingExporter:
    """Exportador CAÍDO. F-MUTE / «exporter caído» como objeto, no como mock ad hoc.

    Lanza siempre. El sensor tiene que tragárselo, contarlo y degradarse — jamás
    propagarlo al camino que lo llamó.
    """
    nombre = "failing"

    def __init__(self) -> None:
        self.intentos = 0

    def export(self, signal: Signal) -> None:
        self.intentos += 1
        raise ExporterUnavailable("el destino no acepta")


class Pipeline(str, Enum):
    """CINCO estados explícitos. Ninguno se infiere de un nombre de clase.

    La versión anterior clasificaba mirando `type(provider).__name__` contra una
    lista de proveedores inertes y consultando los globals de OTel. Las dos cosas
    están mal por el mismo motivo: **una etiqueta no es una medida**. Un provider
    puede llamarse `TracerProvider` y no tener ni un procesador colgado; y los
    globals dicen qué hay montado en el proceso, no si LO NUESTRO va a salir.

    Aquí el estado se DERIVA de la estructura que se puede comprobar:
      · `disabled`             apagado a propósito; no se toca el SDK;
      · `not_configured`       no hay bundle: nadie nos dio proveedores;
      · `pipeline_unverified`  hay proveedores, pero no podemos acreditar que
                               tengan salida (0 procesadores / 0 readers) o que su
                               Resource sea el nuestro;
      · `ready`                Resource verificado Y salida acreditada en los tres;
      · `degraded`             estuvo listo y algo falló al entregar.
    """
    DISABLED = "disabled"
    NOT_CONFIGURED = "not_configured"
    PIPELINE_UNVERIFIED = "pipeline_unverified"
    READY = "ready"
    DEGRADED = "degraded"


class ExportResult(str, Enum):
    """Qué le pasó a la señal, TIPADO. `True/False` no distinguía tres casos.

    · `NOT_EXPORTED`     nadie la recogió (no-op, apagado, fallo);
    · `ACCEPTED_BY_SDK`  el SDK la aceptó — NO dice que saliera del proceso;
    · `EXPORTED`         un `force_flush` confirmó la entrega al exportador.

    La distinción entre los dos últimos es la que impide la mentira cómoda:
    entregar al SDK es todo lo que un emisor puede saber en el momento de emitir,
    y llamarlo «exportado» sería afirmar el efecto de un tercero.
    """
    NOT_EXPORTED = "not_exported"
    ACCEPTED_BY_SDK = "accepted_by_sdk"
    EXPORTED = "exported"


def _tiene_salida(proveedor: Any, atributos: Sequence[str]) -> bool | None:
    """¿Este proveedor tiene ALGO colgado por donde salir?

    Devuelve `True`/`False` si se puede mirar, y `None` si NO SE PUEDE — que es un
    tercer valor a propósito: no poder comprobarlo no es «no tiene», y desde luego
    no es «tiene». `None` empuja a `pipeline_unverified`, nunca a `ready`.

    Mira atributos internos del SDK y eso es frágil por definición; por eso la
    versión del SDK va PINNEADA y hay un test que se pone rojo si el atributo
    desaparece. Frágil y ruidoso es mejor que estable y falso.
    """
    for ruta in atributos:
        obj = proveedor
        try:
            for parte in ruta.split("."):
                obj = getattr(obj, parte)
        except AttributeError:
            continue
        try:
            return len(obj) > 0
        except TypeError:
            return None
    return None


SALIDA_TRACES = ("_active_span_processor._span_processors",)
# ⚠️ `_sdk_config.metric_readers` VA PRIMERO Y `_all_metric_readers` NO ESTÁ.
# Medido contra el SDK 1.38.0: `_all_metric_readers` es un set de weakrefs A NIVEL
# DE CLASE, compartido por todos los MeterProvider del proceso. Un provider creado
# SIN readers lo enseñaba NO VACÍO porque otro provider había dejado el suyo
# dentro, así que la comprobación daba «tiene salida» para cualquier proveedor en
# cuanto alguien, en cualquier parte, hubiera montado uno. Es exactamente el `ok`
# inferido de un global que este commit existe para quitar, reintroducido por la
# puerta de atrás. Lo cazó el test contra el SDK de verdad; contra un doble habría
# pasado.
# `_metric_readers` (1.44) primero, `_sdk_config.metric_readers` (1.38) de
# respaldo. Y `_all_metric_readers` SIGUE SIN ESTAR, en las dos versiones: medido
# otra vez en 1.44.0, un MeterProvider construido con `metric_readers=[]` lo enseña
# con UNO dentro, porque es un set de weakrefs a nivel de CLASE que llena cualquier
# otro provider del proceso. Es la trampa que ya me comí una vez; queda enumerada
# aquí para que el siguiente que toque esto no la reabra.
SALIDA_METRICAS = ("_metric_readers", "_sdk_config.metric_readers")
SALIDA_LOGS = ("_multi_log_record_processor._log_record_processors",)

# El Resource no vive en el mismo sitio en los tres proveedores del SDK 1.38.0:
# TracerProvider y LoggerProvider lo exponen como `.resource`; MeterProvider lo
# guarda en `_sdk_config.resource`. Enumerar las rutas es lo que evita que «no lo
# encuentro» se lea como «no coincide».
RUTAS_RESOURCE = ("resource", "_resource", "_sdk_config.resource")


class RegistroDeEntrega:
    """Lo único que puede acreditar `EXPORTED`: un exportador que CONSERVA su
    resultado y nos lo cuenta.

    Sin esto, `EXPORTED` era mentira, y la reprodujo el auditor: `SimpleSpanProcessor`
    IGNORA el `SpanExportResult.FAILURE` que devuelve el exportador —no lo propaga,
    no lo cuenta, no lo lanza— y `provider.force_flush()` devuelve `True` igualmente,
    porque su contrato es «vacié la cola», no «el otro lado lo aceptó». Medido:
    exportador devolviendo FAILURE en cada intento, `state=ready`,
    `emit=accepted_by_sdk`, `flush=exported`. Tres verdes sobre cero entregas.

    `force_flush` es una BARRERA, no un acuse. Así que sin un registro instrumentado
    el techo honesto es `ACCEPTED_BY_SDK`, y sólo una confirmación explícita sube a
    `EXPORTED`. Un FAILURE observado baja el sensor a `DEGRADED`.
    """

    def __init__(self) -> None:
        self.ok = 0
        self.fallos = 0

    def anota(self, exito: bool) -> None:
        if exito:
            self.ok += 1
        else:
            self.fallos += 1

    def veredicto(self) -> ExportResult:
        if self.fallos:
            return ExportResult.NOT_EXPORTED
        if self.ok:
            return ExportResult.EXPORTED
        # Ni un éxito ni un fallo observados: no se sabe. Y «no se sabe» no puede
        # caer del lado bueno.
        return ExportResult.ACCEPTED_BY_SDK


def instrumenta_exportador(exportador: Any, registro: RegistroDeEntrega) -> Any:
    """Envuelve un exportador del SDK para que su resultado NO se pierda.

    El procesador del SDK se traga el valor de retorno; este envoltorio lo anota
    antes de devolverlo, sin cambiar el comportamiento de nadie.
    """
    class _Proxy:
        def __init__(self, inner):
            self._inner = inner

        def export(self, *args, **kwargs):
            # `*args, **kwargs` A PROPÓSITO Y NO POR PEREZA. El exportador de
            # MÉTRICAS de 1.44 se llama `export(data, timeout_millis=...)`; mi
            # firma `export(datos)` reventaba con `TypeError`, el SDK se lo tragaba
            # y el registro se quedaba en 0/0 — o sea que el veredicto salía
            # `ACCEPTED_BY_SDK` sobre un camino que ni siquiera llegó a exportar.
            # Un instrumento con firma propia mide su propia firma.
            try:
                r = self._inner.export(*args, **kwargs)
            except BaseException:
                # UNA EXCEPCIÓN ES UN FALLO DE ENTREGA, no un hueco. Dejarla pasar
                # sin anotar repetiría el 0/0 por otra puerta.
                registro.anota(False)
                raise
            # Spans, logs y métricas usan enums distintos; el miembro común es
            # SUCCESS/FAILURE. Un retorno que no sea ninguno de los dos NO se
            # cuenta como éxito: no saber no cae del lado bueno.
            nombre = getattr(r, "name", "")
            if nombre in ("SUCCESS", "FAILURE"):
                registro.anota(nombre == "SUCCESS")
            return r

        def __getattr__(self, n):
            return getattr(self._inner, n)

    return _Proxy(exportador)


@dataclass
class TelemetryBundle:   # noqa: D101 - documentado abajo
    """Tracer, meter y logger EXPLÍCITOS, atados a una Identity y a un Resource
    verificado. No se sacan de los globals de OTel.

    Que los tres vengan juntos no es comodidad: un bundle con tracer y sin logger
    dejaría los logs cayendo a un no-op silencioso mientras los spans salen, y el
    estado del conjunto diría `ready`. O están los tres acreditados, o el conjunto
    no está listo.
    """
    identity: Identity          # DUEÑA ÚNICA: ver `IdentityMismatch`
    # EL ADAPTADOR VIAJA CON EL BUNDLE. El Resource de los providers se verificó
    # CONTRA ÉL; un sensor que luego use otro esquema emite etiquetas de un
    # vocabulario y un Resource de otro, y las dos mitades del dato se contradicen
    # sin que nada lo diga. O se hereda, o se rechaza: adivinar no es una opción.
    adapter: SchemaAdapter
    tracer: Any
    meter: Any
    logger: Any
    resource_verificado: bool
    salida_verificada: bool
    detalle: dict[str, Any] = field(default_factory=dict)
    registro: "RegistroDeEntrega | None" = None
    # PRESUPUESTO DE IDENTIDAD COMPARTIDO POR TODO EL PIPELINE. Vive aquí y no en
    # cada sensor porque el coste que acota es del RECEPTOR: mil sensores con su
    # cupo propio son mil identidades en el cable diciendo cada uno «voy dentro de
    # presupuesto». El eje se comparte donde se comparte el destino.
    identity_budget: CardinalityBudget = field(
        default_factory=lambda: PRESUPUESTO_IDENTIDAD_PROCESO)

    @property
    def state(self) -> Pipeline:
        if self.resource_verificado and self.salida_verificada:
            return Pipeline.READY
        return Pipeline.PIPELINE_UNVERIFIED


def _resource_casa(proveedor: Any, esperado: Mapping[str, Any]) -> bool:
    """El Resource del proveedor tiene que declarar NUESTRA identidad.

    Si no la declara, las señales saldrían atribuidas a otro workload — y ese es
    justo el fallo que la identidad server-derived existe para cerrar, reaparecido
    una capa más abajo, en el sitio donde nadie mira.
    """
    res = None
    for ruta in RUTAS_RESOURCE:
        obj = proveedor
        try:
            for parte in ruta.split("."):
                obj = getattr(obj, parte)
        except AttributeError:
            continue
        if obj is not None:
            res = obj
            break
    attrs = getattr(res, "attributes", None)
    if not attrs:
        return False
    # NI FALTANTES NI EXTRA AUTORITATIVO. Lo primero es obvio; lo segundo no, y es
    # lo que cierra la puerta de atrás: un atributo `llminbox.*` o `service.*` que
    # nosotros no emitimos es alguien añadiendo atribución por su cuenta, y el
    # consumidor no puede distinguir cuál de las dos manos la puso. El resto
    # (`telemetry.sdk.*`, `host.*`, lo que meta el operador) pasa: no es nuestro y
    # no atribuye.
    for k, val in esperado.items():
        if str(attrs.get(k)) != str(val):
            return False
    nuestros = {str(k) for k in attrs
                if str(k).startswith("llminbox.") or str(k).startswith("service.")}
    return not (nuestros - set(esperado))


def build_bundle(identity: Identity, *, tracer_provider: Any, meter_provider: Any,
                 logger_provider: Any, adapter: SchemaAdapter | None = None,
                 nombre: str = "llminbox",
                 registro: "RegistroDeEntrega | None" = None,
                 identity_budget: CardinalityBudget | None = None) -> TelemetryBundle:
    """Construye el bundle y ACREDITA lo que se puede acreditar. Nunca adivina."""
    ad = adapter or LlminboxV1Adapter()
    # EL RESOURCE ENTERO, EXACTO Y EN LOS TRES. Comparar sólo instancia, principal
    # y carril dejaba pasar un `role` forjado, `credential_generation=999` y un
    # `schema` falso — medido: `READY` con los tres mentidos. El Resource es la
    # atribución de TODA señal que sale; verificar un subconjunto es acreditar la
    # parte que da igual.
    esperado = {str(k): str(val) for k, val in ad.resource_attributes(identity).items()}
    res_ok = all(_resource_casa(p, esperado)
                 for p in (tracer_provider, meter_provider, logger_provider))
    salidas = {
        "traces": _tiene_salida(tracer_provider, SALIDA_TRACES),
        "metrics": _tiene_salida(meter_provider, SALIDA_METRICAS),
        "logs": _tiene_salida(logger_provider, SALIDA_LOGS),
    }
    # `None` (no se pudo mirar) NO cuenta como salida. Sólo un `True` explícito.
    salida_ok = all(v is True for v in salidas.values())
    return TelemetryBundle(
        identity=identity,
        tracer=tracer_provider.get_tracer(nombre, ad.version),
        meter=meter_provider.get_meter(nombre, ad.version),
        logger=logger_provider.get_logger(nombre, ad.version),
        adapter=ad,
        resource_verificado=res_ok, salida_verificada=salida_ok,
        registro=registro,
        # NI UNO NUEVO POR BUNDLE. Crear aquí un `CardinalityBudget()` propio hacía
        # que dos pipelines sin presupuesto explícito tuvieran DOS techos de 256 en
        # vez de uno: el coste lo paga el receptor, que ve la suma, así que un techo
        # por bundle no acota nada — se multiplica con los bundles. El defecto es un
        # presupuesto de PROCESO compartido; quien quiera aislar, pasa el suyo.
        identity_budget=identity_budget or PRESUPUESTO_IDENTIDAD_PROCESO,
        detalle={"salidas": salidas, "resource_esperado": esperado,
                 "tracer_provider": tracer_provider,
                 "meter_provider": meter_provider,
                 "logger_provider": logger_provider})


class OtelExporter:
    """El sink REAL: counter, gauge, histogram, span con parent y Link, y log OTel
    auténtico. Nada de contadores de mentira.

    Devuelve `ACCEPTED_BY_SDK`, jamás `EXPORTED`: entregar al SDK es todo lo que
    este objeto puede saber en el momento de emitir. `EXPORTED` sólo lo puede
    afirmar `flush()`, que espera al `force_flush` del proveedor.
    """
    nombre = "otel"

    def __init__(self, bundle: TelemetryBundle):
        self.bundle = bundle
        self._instrumentos: dict[tuple, Any] = {}
        self.entregados = 0

    def _instrumento(self, nombre: str, clase: str):
        clave = (nombre, clase)
        inst = self._instrumentos.get(clave)
        if inst is None:
            crea = {"counter": self.bundle.meter.create_counter,
                    "gauge": self.bundle.meter.create_gauge,
                    "histogram": self.bundle.meter.create_histogram}[clase]
            # (el método de escritura difiere por instrumento: ver `_escribe`)
            inst = crea(nombre)
            self._instrumentos[clave] = inst
        return inst

    def export(self, signal: "Signal") -> ExportResult:
        if signal.tipo == "metric":
            clase = signal.attributes.get("instrument", "counter")
            inst = self._instrumento(signal.nombre, clase)
            attrs = dict(signal.labels)
            # UN MÉTODO POR INSTRUMENTO, y son TRES distintos en el SDK real:
            # `add` (counter), `set` (gauge) y `record` (histogram). Usar `add` en
            # los tres convertiría una medida puntual en una suma —número plausible
            # y equivocado, el peor error de un panel—; y `record` en el gauge
            # revienta, porque el gauge síncrono del SDK 1.38 no lo tiene.
            metodo = {"counter": "add", "gauge": "set", "histogram": "record"}[clase]
            getattr(inst, metodo)(signal.valor, attrs)
        elif signal.tipo == "span":
            self._span(signal)
        elif signal.tipo == "log":
            self._log(signal)
        else:
            return ExportResult.NOT_EXPORTED
        self.entregados += 1
        return ExportResult.ACCEPTED_BY_SDK

    def _contexto(self, ctx: Mapping[str, Any] | None):
        from opentelemetry import trace as _t
        if not ctx or not ctx.get("trace_id") or not ctx.get("span_id"):
            return None
        try:
            tid = int(str(ctx["trace_id"]), 16)
            sid = int(str(ctx["span_id"]), 16)
        except ValueError:
            # UN CONTEXTO MALFORMADO TIRA EL ENLACE, NO LA SEÑAL. Antes reventaba
            # dentro del sink, `_emit` lo cazaba y la señal ENTERA se perdía
            # marcando el sensor como degradado: un id mal formateado por el
            # llamante borraba el span que sí era correcto y además acusaba al
            # exportador de una avería que no era suya.
            return None
        return _t.SpanContext(trace_id=tid, span_id=sid, is_remote=True,
                              trace_flags=_t.TraceFlags(_t.TraceFlags.SAMPLED))

    def _span(self, signal: "Signal") -> None:
        from opentelemetry import trace as _t
        enlaces = []
        for e in signal.links:
            sc = self._contexto(e)
            if sc is not None:
                attrs = {k: val for k, val in e.items()
                         if k not in ("trace_id", "span_id")}
                enlaces.append(_t.Link(sc, attrs))
        padre = None
        pctx = signal.attributes.get("_parent")
        sc = self._contexto(pctx) if isinstance(pctx, Mapping) else None
        if sc is not None:
            padre = _t.set_span_in_context(_t.NonRecordingSpan(sc))
        attrs = {k: val for k, val in signal.attributes.items()
                 if not str(k).startswith("_")}
        span = self.bundle.tracer.start_span(
            signal.nombre, context=padre, links=enlaces, attributes=attrs,
            start_time=int(signal.at * 1e9) if signal.at else None)
        span.end()

    def _log(self, signal: "Signal") -> None:
        # POR KWARGS, NO CONSTRUYENDO `LogRecord`. Construirlo a mano está
        # deprecado en 1.38 —`LogRecord` desaparece en 1.39 y lo sustituyen
        # `ReadWriteLogRecord`/`ReadableLogRecord`— y el aviso no era cosmético:
        # cuando la clase se vaya, el `_log` reventaría, `_emit` se tragaría la
        # excepción y la convertiría en `NOT_EXPORTED`. Los logs se apagarían EN
        # SILENCIO mientras spans y métricas siguen saliendo. La sobrecarga por
        # kwargs es la ruta soportada y ya existe en 1.38, así que no hay que
        # esperar a la subida para dejar de depender de lo que se va.
        # El Resource no se pasa: lo pone el LoggerProvider, que es su dueño.
        from opentelemetry._logs import SeverityNumber
        self.bundle.logger.emit(
            timestamp=int(signal.at * 1e9) if signal.at else None,
            severity_text="INFO", severity_number=SeverityNumber.INFO,
            body=signal.nombre, attributes=dict(signal.attributes))

    def _anota_fallo_de_barrera(self) -> None:
        """El fallo del `force_flush` entra por el MISMO canal que el del exportador.

        `state` deriva `DEGRADED` de `registro.fallos`, así que anotarlo aquí es lo
        que hace que un flush que no puede vaciar mueva el estado. Sin registro no se
        inventa uno: se deja constancia en el bundle para que `state` lo vea igual.
        """
        reg = getattr(self.bundle, "registro", None)
        if reg is not None:
            reg.anota(False)
        self.bundle.detalle["flush_fallido"] = True

    def flush(self, timeout_ms: int = 5000) -> ExportResult:
        """La barrera PRIMERO, el veredicto DESPUÉS — y el veredicto no lo da la
        barrera.

        `force_flush` sólo garantiza que las colas se vaciaron hacia el exportador.
        Devuelve `True` aunque el exportador haya contestado `FAILURE` en cada
        intento, porque el procesador del SDK ni siquiera mira ese valor. Por eso
        aquí `force_flush` es condición NECESARIA y nunca suficiente: sin un
        `RegistroDeEntrega` instrumentado, el techo es `ACCEPTED_BY_SDK`.
        """
        ok = True
        for clave in ("tracer_provider", "meter_provider", "logger_provider"):
            p = self.bundle.detalle.get(clave)
            f = getattr(p, "force_flush", None)
            if f is None:
                return ExportResult.ACCEPTED_BY_SDK
            try:
                ok = bool(f(timeout_ms)) and ok
            except Exception:
                # UN FALLO DE BARRERA ES UN FALLO DE ENTREGA, y tiene que quedar en el
                # registro: devolver `NOT_EXPORTED` y callar dejaba el sensor en
                # `ready` —`state` mira `registro.fallos`— mientras el flush llevaba
                # rato sin poder vaciar. El veredicto bajaba y el estado no se movía.
                self._anota_fallo_de_barrera()
                return ExportResult.NOT_EXPORTED
        if not ok:
            self._anota_fallo_de_barrera()
            return ExportResult.NOT_EXPORTED
        reg = self.bundle.registro
        if reg is None:
            # NO SE SABE. La barrera pasó y nadie conserva el resultado del otro
            # lado: es exactamente `ACCEPTED_BY_SDK`, ni un escalón más.
            return ExportResult.ACCEPTED_BY_SDK
        return reg.veredicto()


# ─────────────────────────────────────────────────────────────────────────────
# Presupuesto de cardinalidad
# ─────────────────────────────────────────────────────────────────────────────
class CardinalityBudget:
    """Techo DURO de series activas, con desbordamiento a una serie única.

    El techo se justifica leyendo el mapa de credenciales, no estimando:
    |principals| x |lanes| x |vocabulario cerrado|. Superarlo es un defecto del
    emisor, no una cuota que se sube.
    """
    def __init__(self, max_series: int = 512):
        self.max_series = max_series
        self._vistas: set[tuple] = set()
        self.desbordes = 0
        self._lock = threading.Lock()

    def admite(self, clave: tuple) -> tuple[bool, tuple]:
        with self._lock:
            if clave in self._vistas:
                return True, clave
            if len(self._vistas) < self.max_series:
                self._vistas.add(clave)
                return True, clave
            self.desbordes += 1
            return False, (clave[0], (("overflow", "true"),))

    @property
    def activas(self) -> int:
        with self._lock:
            return len(self._vistas)


# EL PRESUPUESTO DE IDENTIDAD POR DEFECTO ES DEL PROCESO, NO DE CADA PIPELINE.
# Un techo por bundle se multiplica con los bundles: dos pipelines sin presupuesto
# explícito tenían dos veces 256 y cada uno se creía dentro de su cupo mientras el
# receptor veía 512. Lo que se acota es el coste de QUIEN RECIBE, y ése es uno solo.
# Quien necesite aislar de verdad —un test, un banco— pasa el suyo y no comparte.
PRESUPUESTO_IDENTIDAD_PROCESO = CardinalityBudget(max_series=256)


# ─────────────────────────────────────────────────────────────────────────────
# Hechos durables
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Fact:
    """Un hecho que YA aterrizó en almacenamiento durable.

    `source` es obligatorio y sólo vale `"journal"`. Suena a ceremonia y no lo es:
    es el punto exacto donde se decide si el progreso lo acredita una fila o un
    contador en memoria. Con un contador, un reinicio del proceso devuelve a
    «sano» a un agente que lleva horas muerto — la avería que `servicio.py` ya
    pagó llevando el latido a `meta`.
    """
    kind: str
    seq: int
    at: float
    source: str = "journal"

    def __post_init__(self) -> None:
        if self.source != "journal":
            raise UntrustedSource(
                "el progreso sólo lo acredita un hecho durable del journal, "
                f"no {self.source!r}")


# ─────────────────────────────────────────────────────────────────────────────
# Hooks de fallo — los ganchos que piden los falsadores
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Hooks:
    """Puntos de observación NOMBRADOS, uno por falsador del contrato.

    No son fault injection para tests solamente: son el sitio donde un operador
    engancha una alarma. Que el test y la alarma compartan punto es lo que impide
    que el detector pase la prueba por un camino que producción no recorre.
    """
    on_kill: Callable[[Identity], None] | None = None
    on_sigstop: Callable[[Identity], None] | None = None
    on_mute: Callable[[Identity], None] | None = None
    on_loop: Callable[[Identity], None] | None = None
    on_cardinality: Callable[[Identity, int], None] | None = None
    on_forge: Callable[[Identity | None, int], None] | None = None   # nº de campos
    on_exporter_down: Callable[[Identity, str], None] | None = None  # código cerrado

    def _disparar(self, cb, *args) -> None:
        if cb is None:
            return
        try:
            cb(*args)
        except Exception:
            # Un hook que revienta no puede tumbar al sensor que lo llamó: sería
            # un observador que rompe lo observado.
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Contadores internos del propio sensor
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class SensorStats:
    """El sensor midiéndose a sí mismo. Sin esto, su silencio se lee como salud."""
    emitidas: int = 0
    export_ok: int = 0
    export_fallos: int = 0
    label_violaciones: int = 0
    nombre_violaciones: int = 0
    contenido_bloqueado: int = 0
    cardinalidad_desbordes: int = 0
    identidad_desbordes: int = 0
    descartadas: int = 0
    untrusted_rechazos: int = 0
    ultimo_export_ok: float | None = None
    motivo_exportador: str = "null"


# ─────────────────────────────────────────────────────────────────────────────
# Sensor
# ─────────────────────────────────────────────────────────────────────────────
class Sensor:
    """La cara que importan M1 y M2. Sin FastAPI, sin journal, sin índice.

    `strict` NO es «modo producción con más celo»: es el interruptor que convierte
    una violación silenciosa y contada en una excepción visible, y existe para que
    un test pueda distinguir «no hubo violación» de «la hubo y no se vio». En
    producción va a False por la regla ⑤.
    """

    def __init__(self, identity: Identity, trust: Trust, *,
                 exporter: Exporter | None = None,
                 adapter: SchemaAdapter | None = None,
                 budget: CardinalityBudget | None = None,
                 identity_budget: CardinalityBudget | None = None,
                 bundle: "TelemetryBundle | None" = None,
                 capture_content: bool = False,
                 content_digest: bool = False,
                 hmac_key: bytes | None = None,
                 strict: bool = False,
                 hooks: Hooks | None = None,
                 enabled: bool = False,
                 clock: Callable[[], float] = time.time):
        if trust not in TRUSTED:
            raise UntrustedSource(
                f"{trust.value} no es un emisor de confianza: sólo gateway y "
                "supervisor. El runtime del agente no se autocertifica.")
        self.identity = identity
        self.trust = trust
        self.enabled = enabled
        self.exporter: Exporter = exporter or NullExporter()
        # EL ESQUEMA SE HEREDA DEL BUNDLE, O SE RECHAZA LA CONTRADICCIÓN. El Resource
        # de los providers se verificó contra el adaptador del bundle; un sensor con
        # otro esquema emitiría etiquetas de un vocabulario y un Resource de otro, y
        # las dos mitades del dato se contradirían sin que nada lo dijera. Adivinar
        # cuál manda es la peor de las tres opciones.
        # MISMO OBJETO O NADA. Aceptar «otro adaptador con el mismo tipo y versión» era
        # una equivalencia inventada por mí: el Resource de los providers se verificó
        # contra EL objeto del bundle, y otra instancia puede nombrar los instrumentos
        # de otra forma —`metric_name` es suyo— aunque comparta tipo y `version`. Con
        # eso, los nombres salían de un vocabulario y el Resource de otro sin que nada
        # lo dijera. La igualdad estructural no es identidad de esquema.
        if bundle is not None and adapter is not None and adapter is not bundle.adapter:
            raise SchemaError(
                f"el sensor trae OTRA instancia de esquema "
                f"(`{type(adapter).__name__}/{getattr(adapter, 'version', '?')}`) y el "
                f"bundle verificó su Resource contra la suya "
                f"(`{type(bundle.adapter).__name__}"
                f"/{getattr(bundle.adapter, 'version', '?')}`). Con bundle sólo cabe "
                "heredarlo o pasar EL MISMO objeto: mismo tipo y misma versión no "
                "garantizan los mismos nombres, y los nombres son la mitad del dato")
        self.adapter = (bundle.adapter if (adapter is None and bundle is not None) else adapter or LlminboxV1Adapter())
        # DOS PRESUPUESTOS, PORQUE SON DOS EJES QUE CRECEN POR SU CUENTA. El de
        # etiquetas acota las dimensiones que elige el emisor; el de IDENTIDAD
        # acota cuántos (principal, lane, runtime_instance) —o sea cuántos
        # Resource distintos— hay vivos. Con uno solo, mil runtimes efímeros
        # agotaban el presupuesto y hacían desbordar las etiquetas de todos: un
        # eje se comía al otro y el desborde aparecía donde no estaba la causa.
        self.budget = budget or CardinalityBudget()
        self.bundle = bundle
        if bundle is not None and bundle.identity != identity:
            raise IdentityMismatch(
                f"el bundle es de `{bundle.identity.principal}@{bundle.identity.lane}"
                f"/{bundle.identity.runtime_instance}` y el sensor de "
                f"`{identity.principal}@{identity.lane}/{identity.runtime_instance}`: "
                "el Resource lo fijan los providers y no cambia por señal, así que "
                "saldría el del bundle con las etiquetas del sensor")
        # PRIORIDAD: el explícito, luego el del BUNDLE (o sea el del pipeline), y
        # sólo si no hay ninguno uno propio. Un cupo por sensor no acota nada: el
        # coste lo paga el receptor, que ve la suma de todos.
        # EL FALLBACK ES EL PRESUPUESTO DE PROCESO, NO UNO PRIVADO. Un sensor sin
        # bundle fabricaba su propio `CardinalityBudget(256)` y se evadía del global
        # entero: N sensores sueltos = N techos, cada uno creyéndose dentro del suyo
        # mientras el receptor ve la suma. El eje que se acota es el del RECEPTOR, y
        # ése es uno solo — quien quiera aislarse, lo pasa.
        self.identity_budget = (identity_budget
                                or (bundle.identity_budget if bundle else None)
                                or PRESUPUESTO_IDENTIDAD_PROCESO)
        self.capture_content = capture_content
        self.content_digest = content_digest
        self.hmac_key = hmac_key
        self.strict = strict
        self.hooks = hooks or Hooks()
        self.degradado = False
        self._cache_resource: tuple | None = None
        # ── LA ADMISIÓN DE IDENTIDAD SE DECIDE AQUÍ, UNA VEZ, PARA TODO ──────
        # Estaba dentro de `_metric`, así que acotaba las métricas y dejaba salir
        # logs, spans y `outbox_span` con SU Resource: medido, techo de identidad
        # 1 y tres Resources distintos en el cable. Un techo que sólo mira un
        # tercio de las señales no acota la cardinalidad, la reparte.
        #
        # Una identidad = una decisión. El mismo `series_key` reutiliza cupo (el
        # presupuesto es idempotente por clave), así que reconectar no gasta; una
        # identidad NUEVA por encima del techo no emite NADA, y su Resource no
        # llega a existir en el cable.
        # LAS ESTADÍSTICAS, ANTES DE LA PUERTA QUE LAS ESCRIBE. Estaban creadas
        # DESPUÉS del bloque de admisión, así que el camino no estricto —el que el
        # contrato obliga a que funcione— hacía `self.stats.identidad_desbordes += 1`
        # sobre un atributo que aún no existía: `AttributeError` en construcción, y
        # de haber existido, el `SensorStats(...)` de dos líneas más abajo lo habría
        # puesto a cero. El contador del desborde vivía menos que el desborde.
        self.stats = SensorStats(motivo_exportador=getattr(self.exporter, "nombre", "?"))
        # `None` = SIN DECIDIR, y no es lo mismo que admitida. Ponerlo a `True` cuando
        # `enabled=False` abría el bypass entero: `enabled` es un atributo PÚBLICO y
        # mutable, así que construir apagado y hacer `s.enabled = True` a continuación
        # dejaba un sensor que jamás pasó por el presupuesto de identidad — el techo se
        # esquivaba sin tocar el presupuesto, sólo cambiando una bandera.
        self._identidad_admitida: bool | None = None
        if enabled:
            # En construcción cuando ya está encendido, para que el modo estricto lance
            # su error TIPADO antes de que exista un objeto capaz de emitir.
            self._admitir_identidad()
        self._clock = clock

    def _emisible(self) -> bool:
        """Puerta ÚNICA de admisión, delante de métricas, spans y logs.

        FAIL-CLOSED a propósito y sin `overflow_sink`: aquel parámetro era un
        `Exporter` genérico y sin acreditar, así que el llamante podía pasarle el
        MISMO `OtelExporter` de la identidad y reabrir el bypass entero por la
        puerta que se abrió para cerrarlo. Un destino agregado de verdad necesita
        su propio bundle tipado con Resource propio VERIFICADO, y eso es iteración
        posterior; hasta entonces, lo honesto es no emitir.
        """
        # ADMISIÓN PEREZOSA Y FAIL-CLOSED: si no se decidió en construcción —porque el
        # sensor nació apagado— se decide AQUÍ, en la primera señal. Es la única forma
        # de que encender por atributo no sea una puerta trasera, sin volver `enabled`
        # inmutable y romper a quien ya lo alterna.
        if self._identidad_admitida is None:
            self._admitir_identidad()
        if self._identidad_admitida:
            return True
        self.stats.descartadas += 1
        return False

    def _admitir_identidad(self) -> None:
        """Consulta el presupuesto UNA vez y cachea. Idempotente por `series_key`, así
        que reconectar con la misma identidad no gasta cupo."""
        self._identidad_admitida, _ = self.identity_budget.admite(
            (self._clave_resource(), ()))
        if self._identidad_admitida:
            return
        self.stats.identidad_desbordes += 1
        self.hooks._disparar(self.hooks.on_cardinality, self.identity,
                             self.identity_budget.activas)
        if self.strict:
            raise CardinalityBudgetExceeded(
                f"identidades vivas: {self.identity_budget.activas}, techo "
                f"{self.identity_budget.max_series}; esta identidad no emite")

    # `_resource_colapsado` RETIRADO. Colapsar los atributos acotaba el Resource que
    # este módulo compone, pero NO el que el provider ya fijó al construirse: el SDK
    # sigue emitiendo el suyo, así que la señal salía con un Resource real completo y
    # un cuerpo que decía otra cosa. Acotar la representación mientras el cable emite
    # la identidad entera es contarse una historia. Para v0.9 la conducta segura es no
    # emitir; un destino agregado de verdad necesita su propio bundle tipado con
    # Resource propio VERIFICADO, y queda diferido.

    def _clave_resource(self) -> tuple:
        """El Resource tal y como sale, normalizado a tupla ordenada.

        Se cachea porque no cambia durante la vida del sensor —la identidad es
        inmutable y el adaptador también— y porque recomputarlo en cada métrica
        pondría trabajo en el camino caliente para obtener siempre lo mismo.
        """
        if self._cache_resource is None:
            attrs = self.adapter.resource_attributes(self.identity)
            self._cache_resource = tuple(sorted((str(k), str(x))
                                                for k, x in attrs.items()))
        return self._cache_resource

    # — nombre de señal, de vocabulario CERRADO —————————————————————————
    def _nombre(self, canonico: str, permitidos: frozenset) -> str | None:
        """Un nombre libre es cardinalidad por la puerta de al lado.

        Con las etiquetas cerradas y el nombre abierto, `count(f"job.{id}")` crea
        una serie por id sin usar una sola etiqueta prohibida. Se rechaza y se
        cuenta: un nombre desconocido es un defecto del emisor —nuestro código—,
        no entrada de un atacante, así que perder la señal es la dirección segura.
        """
        if canonico in permitidos:
            return canonico
        self.stats.nombre_violaciones += 1
        if self.strict:
            raise SchemaError(
                f"`{canonico}` no está en el vocabulario cerrado de nombres de señal")
        return None

    # — construcción de etiquetas ————————————————————————————————————————
    def _labels(self, bruto: Mapping[str, Any]) -> dict[str, str]:
        """Cierra las CLAVES y también los VALORES, incluido el del carril.

        Dos huecos que dejó la primera versión y que este método cierra:

        · EL VALOR DE UNA CLAVE DE VOCABULARIO ABIERTO. `lane` no tenía lista de
          valores —está acotado «por el mapa de credenciales»— así que cualquier
          cadena del llamante salía VERBATIM como etiqueta. Medido: un
          `count(..., lane="sk-live-DEADBEEF")` publicaba el secreto en la métrica,
          y el `lane` emitido podía CONTRADECIR el de la identidad resuelta por el
          servidor. Cerrar las claves y dejar abierto un valor es la misma avería
          que cerrar las etiquetas y dejar abierto el nombre, un piso más abajo.
          Cura: el carril NO lo propone el llamante — se DERIVA de la identidad,
          que es server-derived por construcción. Un `lane` distinto que llegue por
          argumento se cuenta como violación y se descarta; nunca se emite.

        · LA COMPARACIÓN CASE-SENSITIVE. La denylist miraba `k`, la de contenido
          `k.lower()`: `Event_ID` esquivaba la primera. No filtraba —la allowlist lo
          paraba después— pero un control que no discrimina acredita un instrumento
          muerto sintiéndose igual de riguroso. Se normaliza antes de comparar.
        """
        salida: dict[str, str] = {}
        for k, v in bruto.items():
            kl = str(k).lower()
            if kl in LABELS_PROHIBIDAS or kl in CLAVES_SENSIBLES:
                self.stats.label_violaciones += 1
                if self.strict:
                    raise ForbiddenLabel(
                        f"`{k}` es identificador o contenido: no puede ser etiqueta "
                        "de métrica. Va en span o log.")
                continue
            if kl == "lane":
                # CUALQUIER carril aportado por el llamante es violación, TAMBIÉN
                # si coincide. Perdonar el que coincide deja viva la ruta por la
                # que llega —y con ella el sitio donde mañana alguien pasa uno que
                # no coincide—, además de premiar la coincidencia accidental: dos
                # sesiones del mismo carril nunca verían el aviso. El carril no se
                # propone: se deriva.
                self.stats.label_violaciones += 1
                if self.strict:
                    raise ForbiddenLabel(
                        "el carril no lo propone el llamante en ningún caso: se "
                        f"deriva de la identidad (`{self.identity.lane}`)")
                continue
            if kl not in LABELS_PERMITIDAS:
                self.stats.label_violaciones += 1
                if self.strict:
                    raise SchemaError(f"`{k}` no está en la allowlist de etiquetas")
                continue
            salida[self.adapter.label_key(kl)] = _valor_acotado(kl, v)
        # SIEMPRE el de la identidad, y al final para que nada lo pueda pisar.
        salida[self.adapter.label_key("lane")] = self.identity.lane
        return salida

    # — métricas ——————————————————————————————————————————————————————————
    def count(self, canonico: str, value: int = 1, **labels: Any) -> bool:
        return self._metric(canonico, value, labels, "counter")

    def gauge(self, canonico: str, value: float, **labels: Any) -> bool:
        return self._metric(canonico, value, labels, "gauge")

    def observe(self, canonico: str, value: float, **labels: Any) -> bool:
        return self._metric(canonico, value, labels, "histogram")

    def _metric(self, canonico: str, value: Any, labels: Mapping[str, Any],
                clase: str) -> ExportResult:
        if not self.enabled:
            return False
        if not self._emisible():
            return ExportResult.NOT_EXPORTED
        if self._nombre(canonico, METRICAS) is None:
            return False
        try:
            etiquetas = self._labels(labels)
        except SchemaError:
            if self.strict:
                raise
            return False
        nombre = self.adapter.metric_name(canonico)
        # EJE 1 · IDENTIDAD: ya está decidida, en construcción y para TODAS las
        # señales — ver `_emisible` y el porqué del **Resource completo** allí.
        # EJE 2 · ETIQUETAS, dentro de esa identidad ya admitida.
        clave = (self.identity.series_key + (nombre,),
                 tuple(sorted(etiquetas.items())))
        ok, clave_final = self.budget.admite(clave)
        if not ok:
            self.stats.cardinalidad_desbordes += 1
            etiquetas = {"overflow": "true"}
            self.hooks._disparar(self.hooks.on_cardinality, self.identity,
                                 self.budget.activas)
            if self.strict:
                raise CardinalityBudgetExceeded(
                    f"{nombre}: {self.budget.activas} series, techo "
                    f"{self.budget.max_series}")
        return self._emit(Signal(
            tipo="metric", nombre=nombre, valor=value, labels=etiquetas,
            attributes={"instrument": clase},
            resource=self.adapter.resource_attributes(self.identity),
            at=self._clock()))

    # — spans y logs ————————————————————————————————————————————————————
    def span(self, canonico: str, *, attributes: Mapping[str, Any] | None = None,
             links: Sequence[Mapping[str, Any]] = (),
             parent: Mapping[str, Any] | None = None) -> bool:
        if not self.enabled:
            return False
        if not self._emisible():
            return ExportResult.NOT_EXPORTED
        if self._nombre(canonico, SPANS) is None:
            return False
        attrs = self._attrs(attributes or {})
        if attrs is None:
            return False
        # PARENT Y LINKS POR EL MISMO CONTROL. Sus atributos los compone el mismo
        # llamante que compone los del span, así que un secreto metido en un enlace
        # habría salido entero por la única puerta que nadie estaba mirando.
        enlaces = []
        for e in links:
            lim = self._attrs(dict(e))
            if lim is None:
                continue
            enlaces.append(lim)
        if parent is not None:
            pad = self._attrs(dict(parent))
            if pad is not None:
                attrs["parent_span_id"] = pad.get("span_id", "")
                attrs["_parent"] = {"trace_id": pad.get("trace_id", ""),
                                    "span_id": pad.get("span_id", "")}
        return self._emit(Signal(
            tipo="span", nombre=self.adapter.span_name(canonico), valor=None,
            attributes=attrs, links=tuple(enlaces),
            resource=self.adapter.resource_attributes(self.identity),
            at=self._clock()))

    def log(self, canonico: str, **attributes: Any) -> ExportResult:
        if not self.enabled:
            return False
        if not self._emisible():
            return ExportResult.NOT_EXPORTED
        if self._nombre(canonico, LOGS) is None:
            return False
        attrs = self._attrs(attributes)
        if attrs is None:
            return False
        return self._emit(Signal(
            tipo="log", nombre=canonico, valor=None, attributes=attrs,
            resource=self.adapter.resource_attributes(self.identity),
            at=self._clock()))

    def _attrs(self, bruto: Mapping[str, Any]) -> dict[str, Any] | None:
        try:
            return redacta(bruto, capture_content=self.capture_content,
                           content_digest=self.content_digest,
                           hmac_key=self.hmac_key, strict=self.strict)
        except SensitiveDataRejected:
            self.stats.contenido_bloqueado += 1
            if self.strict:
                raise
            return None

    # — el span de materialización, con Link y effect_id ————————————————
    def outbox_span(self, *, event_id: str, effect_id: str,
                    accept_context: Mapping[str, Any],
                    attempt: int, outcome: str,
                    parent: Mapping[str, Any] | None = None) -> ExportResult:
        """`outbox.materialize` SE ENLAZA, NO SE ANIDA — y trae `effect_id`.

        Dos cosas, y las dos son del contrato:

        · ENLACE. Corre minutos después, en otro proceso, tras reintentos. Como
          span hijo, su duración mediría la latencia de la COLA y la presentaría
          como latencia de la PETICIÓN: un número correcto en el sitio equivocado,
          que es la forma más cara de mentir en un panel. Por eso `parent` aquí es
          un error duro y no una opción.
        · `effect_id`. La proyección es at-least-once (STATE-OF-THE-ART §garantías):
          el mismo `event_id` produce N intentos y como mucho un efecto. Sin una
          identidad del EFECTO, dos reintentos son indistinguibles de dos efectos y
          el panel cuenta doble un trabajo que se hizo una vez.
        """
        # `enabled` PRIMERO, Y NO ES ORDEN COSMÉTICO. La admisión es perezosa: con
        # `_emisible()` delante, un sensor APAGADO que llamara a `outbox_span`
        # consumía cupo del presupuesto de identidad sin emitir nada. El techo se
        # gastaba con señales que no existen, y la identidad legítima siguiente se
        # encontraba la puerta cerrada por un fantasma. Las otras tres puertas ya lo
        # comprobaban antes; ésta se quedó sin él.
        if not self.enabled:
            return ExportResult.NOT_EXPORTED
        if not self._emisible():
            # LA SEÑAL QUE FALTABA. El presupuesto de identidad gobernaba métricas,
            # spans y logs, y `outbox_span` salía por debajo con SU Resource: techo 1
            # y un Resource nuevo en el cable por cada identidad que reintentara una
            # proyección. Una puerta con una señal fuera no acota, reparte.
            return ExportResult.NOT_EXPORTED
        if parent is not None:
            raise SchemaError(
                "outbox.materialize no puede colgar de la aceptación: usa Link. "
                "Anidarlo convierte la espera en la cola en latencia de petición.")
        if not effect_id:
            raise SchemaError("outbox.materialize exige effect_id (at-least-once)")
        enlace = {"trace_id": accept_context.get("trace_id", ""),
                  "span_id": accept_context.get("span_id", ""),
                  "rel": "accepted_by"}
        return self.span("outbox.materialize",
                         attributes={"event_id": event_id, "effect_id": effect_id,
                                     "attempt": attempt, "outcome": outcome},
                         links=(enlace,))

    # — baggage ——————————————————————————————————————————————————————————
    def baggage(self, **extra: Any) -> dict[str, str]:
        """Sólo `lane` y `schema`. Todo lo demás se rechaza por nombre.

        El baggage cruza fronteras que no controlamos: cualquier proceso aguas
        abajo lo ve y lo puede reenviar. Un `principal` ahí no es atribución, es un
        correlador de identidad viajando por una cabecera.
        """
        b = {"lane": self.identity.lane, "schema": self.adapter.version}
        for k in extra:
            if k not in BAGGAGE_PERMITIDO:
                self.stats.label_violaciones += 1
                if self.strict:
                    raise SchemaError(f"`{k}` no puede viajar en baggage")
        return b

    # — salida ————————————————————————————————————————————————————————————
    # SIN `sink=`. Nadie lo pasaba —quedó del `overflow_sink` retirado— y un destino
    # inyectable por llamada es exactamente la puerta que se cerró: el llamante podía
    # pasar el MISMO exportador de la identidad y saltarse la admisión.
    def _emit(self, signal: Signal) -> ExportResult:
        self.stats.emitidas += 1
        try:
            r = self.exporter.export(signal)
        except Exception as e:
            # REGLA ⑤: el exportador NUNCA tumba a quien lo llamó. Y REGLA ③: su
            # fallo no puede ser invisible, así que se cuenta y se engancha.
            self.stats.export_fallos += 1
            # CÓDIGO ACOTADO, NO EL MENSAJE. El texto de una excepción arrastra la
            # URL, el fichero y —cuando la lanza una librería ajena— el valor que la
            # provocó. Un secreto dentro de un mensaje de error es la fuga que nadie
            # audita, porque el campo por el que sale no se llama como un secreto.
            self.hooks._disparar(self.hooks.on_exporter_down, self.identity,
                                 codigo_error(e))
            self.degradado = True
            return ExportResult.NOT_EXPORTED
        r = r if isinstance(r, ExportResult) else ExportResult.NOT_EXPORTED
        if r is ExportResult.NOT_EXPORTED:
            # NI CUENTA NI REJUVENECE. Es la mitad que faltaba del no-op: si el
            # `NullExporter` tocara `ultimo_export_ok`, la observabilidad apagada
            # publicaría un `staleness` joven y su propio silencio se leería como
            # salud desde el instrumento que existe para delatarlo.
            return r
        self.stats.export_ok += 1
        self.stats.ultimo_export_ok = self._clock()
        return r

    @property
    def state(self) -> Pipeline:
        """Estado EXPLÍCITO, derivado — nunca inferido de un nombre de clase."""
        if not self.enabled:
            return Pipeline.DISABLED
        if self.bundle is None:
            return Pipeline.NOT_CONFIGURED
        # UN BUNDLE ACREDITADO CON UN CABLE NULO NO ES `READY`. `state` derivaba del
        # bundle, que sólo sabe de sus providers; con un `NullExporter` delante, todo
        # eso está bien construido y no sale una sola señal. Decir `ready` ahí es
        # exactamente la mentira que este módulo existe para no contar.
        if isinstance(self.exporter, NullExporter):
            return Pipeline.NOT_CONFIGURED
        if self.degradado:
            return Pipeline.DEGRADED
        # UNA IDENTIDAD RECHAZADA NO ESTÁ `READY`. `_identidad_admitida is False`
        # significa que este sensor no emite NADA —ni métrica, ni log, ni span, ni
        # outbox— y aun así el estado decía `ready`: el panel veía un sensor sano que
        # llevaba rato sin mandar una sola señal, que es la avería más cara de las que
        # este módulo intenta impedir. Se dice DEGRADED, que es lo que es.
        if self._identidad_admitida is False:
            return Pipeline.DEGRADED
        # Y UNA BARRERA QUE NO PUDO VACIAR TAMBIÉN DEGRADA, aunque el registro no
        # exista: el hecho queda en el bundle y `state` lo mira desde aquí.
        if self.bundle.detalle.get("flush_fallido"):
            return Pipeline.DEGRADED
        # UN FALLO OBSERVADO EN EL OTRO EXTREMO TAMBIÉN DEGRADA. Antes sólo
        # degradaba la excepción que veíamos nosotros; el `FAILURE` que el
        # procesador del SDK se traga dejaba el sensor en `ready` mientras no se
        # entregaba nada. El estado tiene que moverse con lo que PASA, no con lo
        # que nos llega a la mano.
        reg = self.bundle.registro
        if reg is not None and reg.fallos:
            return Pipeline.DEGRADED
        return self.bundle.state

    # — rechazo explícito del self-report del agente ——————————————————————
    def reject_agent_report(self, payload: Mapping[str, Any]) -> None:
        """Puerta ÚNICA por la que entra lo que dice el agente: para rechazarlo.

        Existe nombrada para que el rechazo sea contable y enganchable. Un `if`
        disperso en el llamante no deja rastro cuando alguien lo olvida.
        """
        self.stats.untrusted_rechazos += 1
        # SÓLO EL NÚMERO DE CAMPOS. Los NOMBRES los elige el agente, o sea la parte
        # no confiable: reenviarlos al hook es dejar que el sujeto rechazado escriba
        # en la alarma que lo rechaza, y un nombre de campo puede llevar dentro el
        # valor («token=sk-...») igual que un valor.
        self.hooks._disparar(self.hooks.on_forge, self.identity, len(payload))
        raise UntrustedSource(
            "el runtime del agente no emite señales de ciclo de vida ni de salud: "
            "su prosa es material bridge con authority:false")


# ─────────────────────────────────────────────────────────────────────────────
# Detector: liveness vs progreso
# ─────────────────────────────────────────────────────────────────────────────
class Estado(str, Enum):
    VIVA_CON_PROGRESO = "viva-con-progreso"
    VIVA_SIN_OBLIGACION = "viva-sin-obligacion"
    ATASCADA = "atascada"
    EN_BUCLE = "en-bucle"
    MUDA = "muda"
    SIN_ARMAR = "sin-armar"
    INARMABLE = "inarmable"
    ILEGIBLE = "ilegible"
    INDETERMINADO = "indeterminado"
    SENSOR_MUDO = "sensor-mudo"


# `sin-armar` es sano porque «todavía nadie ha latido» es el estado inicial
# legítimo. `inarmable` NO lo es: «nadie podrá latir jamás» comparte casilla con
# él si uno se descuida, y ya costó una vez que `/health` dijera ok:true sobre un
# hombre muerto que no podía nacer.
SANOS = frozenset({Estado.VIVA_CON_PROGRESO, Estado.VIVA_SIN_OBLIGACION,
                   Estado.SIN_ARMAR})


@dataclass(frozen=True)
class LoopEvidence:
    """Actividad alta con ENTROPÍA DE ESTADO baja. Sin leer contenido."""
    outbox_attempts: int = 0
    outbox_pending: bool = False
    denials_same_reason: int = 0
    supersessions: int = 0
    distinct_subjects: int = 1

    def dispara(self, *, k_attempts: int = 5, k_denials: int = 20,
                k_supersessions: int = 5) -> str | None:
        # El ⊖ que hace válido a este detector: si los sujetos son MUCHOS, la tasa
        # alta es trabajo, no bucle. Sin este divisor, un agente rápido y sano es
        # indistinguible de uno girando en el sitio.
        d = max(1, self.distinct_subjects)
        if self.outbox_pending and self.outbox_attempts > k_attempts:
            return "outbox_retry"
        if self.denials_same_reason // d >= k_denials:
            return "denial_storm"
        if self.supersessions // d >= k_supersessions:
            return "supersession_churn"
        return None


@dataclass(frozen=True)
class Veredicto:
    estado: Estado
    motivo: str
    hace_s: float | None = None

    @property
    def sano(self) -> bool:
        return self.estado in SANOS


class LivenessProgress:
    """Liveness y progreso con FUENTES DISJUNTAS. Ninguna señal sirve para las dos.

    Un latido que además cuenta como progreso reintroduce literalmente las averías
    C1 y B2 que `servicio.py` ya pagó: «el latido lo refrescaba cualquier llamante
    -> medía tráfico» y «lo refrescaba el INTENTO, antes de la consulta -> un
    watcher roto = verde». Aquí:

      · `heartbeat()` sólo lo acepta un SUPERVISOR y sólo con `cycle_ack=True`;
        un intento se registra pero NO refresca nada.
      · `progress()` sólo lo mueve un `Fact` durable.
    """

    def __init__(self, identity: Identity, *, deadline_s: float = 180.0,
                 window_s: float = 180.0, armable: bool = True,
                 hooks: Hooks | None = None,
                 clock: Callable[[], float] = time.time):
        self.identity = identity
        self.deadline_s = deadline_s
        self.window_s = window_s
        self.armable = armable
        self.hooks = hooks or Hooks()
        self._clock = clock
        self._last_beat: float | None = None
        self._beat_attempts = 0
        self._last_progress_at: float | None = None
        self._last_seq = -1
        self.readable = True

    # — liveness ————————————————————————————————————————————————————————
    def heartbeat(self, *, trust: Trust, at: float | None = None,
                  cycle_ack: bool = False) -> bool:
        if trust is not Trust.SUPERVISOR:
            raise UntrustedSource(
                "el latido lo emite el supervisor, no el gateway ni el agente: "
                "un latido que cualquiera puede refrescar mide tráfico")
        self._beat_attempts += 1
        if not cycle_ack:
            # B2 literal: refrescar con el INTENTO deja verde a un watcher roto.
            return False
        self._last_beat = self._clock() if at is None else at
        return True

    # — progreso ——————————————————————————————————————————————————————
    def progress(self, fact: Fact) -> bool:
        if fact.seq <= self._last_seq:
            return False
        self._last_seq = fact.seq
        self._last_progress_at = fact.at
        return True

    # — veredicto ————————————————————————————————————————————————————
    def evaluate(self, *, now: float | None = None, open_obligations: int = 0,
                 loop: LoopEvidence | None = None,
                 exporter_stale_s: float | None = None) -> Veredicto:
        """Partición CERRADA. No hay `else` benigno: lo que no casa es rojo y se nombra."""
        t = self._clock() if now is None else now

        if not self.readable:
            # El fallo del propio instrumento no puede presentarse como el estado
            # inicial sano. R1 del bloque de vigilancia, calcado.
            return Veredicto(Estado.ILEGIBLE, "no se pudo leer el estado durable")
        if not self.armable:
            return Veredicto(Estado.INARMABLE,
                             "no hay supervisor separado del agente: no puede haber latido")
        # EL SENSOR MUDO GANA AL RESTO A PROPOSITO. Con el exportador caido, la
        # ausencia de latido puede ser la tuberia y no el agente: emitir `muda`
        # ahi seria firmar una causa que no se midio. Se dice lo que se sabe —el
        # instrumento no entrega— y no se adivina el sujeto.
        if exporter_stale_s is not None and exporter_stale_s > self.deadline_s:
            self.hooks._disparar(self.hooks.on_mute, self.identity)
            return Veredicto(Estado.SENSOR_MUDO,
                             "el exportador no entrega: ausencia de datos != salud",
                             exporter_stale_s)
        if self._last_beat is None:
            return Veredicto(Estado.SIN_ARMAR, "aún no ha latido nadie")

        hace = t - self._last_beat
        if hace < 0:
            # Monotonía rota: un latido en el futuro NO es «recién latido».
            return Veredicto(Estado.INDETERMINADO,
                             "latido en el futuro: reloj movido hacia atrás", hace)
        if hace > self.deadline_s:
            self.hooks._disparar(self.hooks.on_kill, self.identity)
            return Veredicto(Estado.MUDA, "sin latido dentro del plazo", hace)

        if loop is not None:
            motivo = loop.dispara()
            if motivo:
                self.hooks._disparar(self.hooks.on_loop, self.identity)
                return Veredicto(Estado.EN_BUCLE, motivo, hace)

        avanzo = (self._last_progress_at is not None
                  and (t - self._last_progress_at) <= self.window_s)
        if avanzo:
            return Veredicto(Estado.VIVA_CON_PROGRESO, "hay transiciones durables", hace)
        if open_obligations > 0:
            # Latido sí, progreso no, Y hay algo que terminar. Sin la obligación
            # abierta esto sería ocio, y llamar «atascado» al ocio fabrica falsos
            # positivos hasta que nadie mira la alarma.
            self.hooks._disparar(self.hooks.on_sigstop, self.identity)
            return Veredicto(Estado.ATASCADA,
                             f"{open_obligations} obligación(es) abierta(s) sin avance",
                             hace)
        return Veredicto(Estado.VIVA_SIN_OBLIGACION, "ocioso, nada pendiente", hace)


# ─────────────────────────────────────────────────────────────────────────────
# Fábrica — APAGADO POR DEFECTO
# ─────────────────────────────────────────────────────────────────────────────
def disabled(identity: Identity | None = None) -> Sensor:
    """No-op determinista, sin exportador y sin tocar el SDK."""
    ident = identity or Identity.server_derived(
        principal="desconocido", role="desconocido", lane="desconocido",
        runtime_instance="0")
    return Sensor(ident, Trust.GATEWAY, exporter=NullExporter(), enabled=False)


def build(identity: Identity, trust: Trust, *, enabled: bool | None = None,
          exporter: Exporter | None = None, adapter: SchemaAdapter | None = None,
          budget: CardinalityBudget | None = None, strict: bool = False,
          capture_content: bool | None = None, content_digest: bool = False,
          hmac_key: bytes | None = None, hooks: Hooks | None = None,
          bundle: "TelemetryBundle | None" = None,
          identity_budget: CardinalityBudget | None = None,
          clock: Callable[[], float] = time.time) -> Sensor:
    """Punto único de construcción. Sin `enabled=True` explícito, no exporta nada.

    `LLMINBOX_OBS=1` enciende; `LLMINBOX_OBS_CONTENT=1` sería lo único que permite
    contenido, y NO se lee de aquí a propósito: encender captura de prompts es una
    decisión de política del operador, no una variable que un runtime hereda del
    entorno. Se pasa por argumento para que quede en el sitio de llamada.
    """
    if enabled is None:
        enabled = os.environ.get("LLMINBOX_OBS", "") == "1"
    # LA IDENTIDAD SE COMPRUEBA AQUÍ, ANTES DE CONSTRUIR NADA. `Sensor` ya la
    # rechaza, pero para entonces este `build` ya ha creado un `OtelExporter` atado
    # al bundle ajeno; fallar después de fabricar el cable es fallar tarde. La
    # identidad de la configuración y la del bundle son la misma cosa o no hay
    # llamada válida que hacer.
    # DOS PRESUPUESTOS PARA UN MISMO PIPELINE ES UNA CONTRADICCIÓN, no una preferencia.
    # El bundle ya declara el eje que comparte con los demás sensores de su destino;
    # pasar otro aquí crea un segundo techo sobre el mismo receptor y los dos se creen
    # correctos. Se rechaza tipado en vez de dejar ganar a uno en silencio.
    if (bundle is not None and identity_budget is not None
            and identity_budget is not bundle.identity_budget):
        raise CardinalityBudgetExceeded(
            "el bundle comparte un presupuesto de identidad y `build` recibió otro "
            "distinto: dos techos sobre el mismo destino no acotan, se suman. Pasa el "
            "del bundle (`bundle.identity_budget`) o no pases ninguno")
    if bundle is not None and bundle.identity != identity:
        raise IdentityMismatch(
            f"`build` recibió identidad `{identity.principal}@{identity.lane}"
            f"/{identity.runtime_instance}` y un bundle de "
            f"`{bundle.identity.principal}@{bundle.identity.lane}"
            f"/{bundle.identity.runtime_instance}`: el Resource lo fijan los "
            "providers del bundle, así que las señales saldrían atribuidas a la otra")
    # SIN BUNDLE NO HAY SALIDA, Y SE DICE. Antes esto salía a buscar el SDK por los
    # globals y clasificaba por nombre de clase; ahora el pipeline se CONSTRUYE
    # explícitamente con `build_bundle` y se pasa. Lo que no se construye queda en
    # `not_configured`, que es un estado, no un silencio.
    exp = exporter
    motivo = getattr(exporter, "nombre", "null")
    # EL EXPORTADOR NO DEPENDE DE `enabled`, Y ANTES SÍ. Con `enabled=False` y un
    # bundle válido se inyectaba un `NullExporter`, así que encender después por el
    # atributo dejaba un sensor sin por dónde emitir: `enabled=True` y mudo. Peor,
    # eso hacía pasar en VERDE cualquier falsador de la puerta de identidad montado
    # sobre un sensor nacido apagado —el falso verde que ya me cazó su propio
    # control positivo—, porque no había emisión que bloquear.
    #
    # Lo que gobierna `enabled` es si se EMITE, no con qué. El cable se construye si
    # hay bundle; encenderlo es otra decisión.
    if exp is None:
        if bundle is not None:
            exp, motivo = OtelExporter(bundle), "otel"
        else:
            exp, motivo = NullExporter(), "not_configured"
    s = Sensor(identity, trust, exporter=exp or NullExporter(), adapter=adapter,
               bundle=bundle, identity_budget=identity_budget,
               budget=budget, capture_content=bool(capture_content),
               content_digest=content_digest, hmac_key=hmac_key,
               strict=strict, hooks=hooks, enabled=enabled, clock=clock)
    s.stats.motivo_exportador = motivo
    return s
