"""HTTP nativo y desacoplado para el journal de coordinacion.

La frontera de autoridad vive en :mod:`coordination`. Este modulo solamente
traduce HTTP a ese contrato y, deliberadamente, no importa ``servicio`` ni
conoce el token compartido historico. La unica credencial de bootstrap es una
credencial por workload incluida en el mapa V8; el resto de llamadas usan la
sesion opaca emitida por el Journal.

Se puede montar sin efectos laterales::

    app.include_router(create_native_router(journal))

La inicializacion y la recarga del mapa son actos explicitos del arranque::

    journal.initialize()
    configure_journal_from_v8(journal, credential_map, capability_grants)

Separar ambos actos evita que importar o construir un router migre una base.
"""

from __future__ import annotations

import asyncio
import ast
import inspect
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from types import MappingProxyType
from typing import Any, Annotated, Literal

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, ValidationError

import coordination as C


API_PREFIX = "/native/v1"
LEGACY_SHARED_HEADER = "x-llminbox-token"

# No son todos sinonimos linguisticos: son todas las formas que ya han aparecido
# en clientes o documentos de este repo para autodeclarar autoridad. El valor
# se rechaza; nunca se ignora y nunca se refleja en la respuesta.
ATTRIBUTION_ALIASES = frozenset({
    "actor", "agent", "agente", "source", "attribution", "attestation",
    "principal", "principal_id", "principalid", "role", "rol", "lane",
    "carril", "runtime", "runtime_instance", "runtimeinstance",
    "capabilities", "credential", "credencial",
    "trust", "observed_at", "observation_time", "observer_principal",
    "observer_runtime", "observer_generation", "target_generation",
    "credential_generation",
})

# Las capacidades nuevas son nombres de politica del gateway. Las que ya son
# invariantes de Journal reutilizan sus constantes para que no haya dos
# vocabularios para el mismo permiso.
CAP_EVENT_WRITER = "event_writer"
CAP_DELIVERY_ACK = "delivery_ack"
CAP_LEASE_HOLDER = "lease_holder"
CAP_COMMAND_SUBMITTER = "command_submitter"

DEFAULT_MUTATION_CAPABILITIES: Mapping[str, str | None] = MappingProxyType({
    "sessions.open": "__runtime_credential__",
    # ``None`` es una declaracion explicita: son actos sobre la sesion propia.
    "sessions.refresh": None,
    "sessions.revoke_current": None,
    "events.accept": CAP_EVENT_WRITER,
    "outbox.claim": C.CAP_OUTBOX_WORKER,
    "outbox.materialized": C.CAP_OUTBOX_WORKER,
    "outbox.failed": C.CAP_OUTBOX_WORKER,
    "outbox.requeue": C.CAP_OUTBOX_OPERATOR,
    "outbox.abandon": C.CAP_OUTBOX_OPERATOR,
    "events.indexed": C.CAP_INDEXER,
    "events.ack": CAP_DELIVERY_ACK,
    "leases.acquire": CAP_LEASE_HOLDER,
    "leases.renew": CAP_LEASE_HOLDER,
    "leases.release": CAP_LEASE_HOLDER,
    "commands.submit": CAP_COMMAND_SUBMITTER,
    "commands.advance": C.CAP_COMMAND_WORKER,
    "runtime.observe": C.CAP_RUNTIME_OBSERVE,
    "runtime.recover": C.CAP_RUNTIME_RECOVER,
})


# ── TECHO DE TRANSPORTE ────────────────────────────────────────────────────
# Bytes CRUDOS del cuerpo HTTP. No es una cuota semantica del Core y no comparte
# vocabulario con el: aqui no hay `code`, ni envelope, ni recibo, porque la
# peticion se corta ANTES de que exista una sesion a la que atribuirla.
#
# 🔑 SE CUENTAN LOS BYTES LEIDOS, NUNCA `Content-Length`. Medido contra el
# despliegue vivo (`uvicorn 0.39`, imagen del Dockerfile): un cuerpo de 33 KiB
# entra igual con `Content-Length` que en `chunked`, asi que un techo que leyera
# la cabecera no seria un techo — una peticion troceada no la trae, y una
# mentirosa la declara pequena. El veredicto es funcion de lo que REALMENTE
# llega: una `Content-Length` enorme con un cuerpo pequeno PASA, porque el
# documento es pequeno; la cabecera no es el documento.
#
# ── EL NUMERO: `1 MiB`, REGLADO — y mi propuesta de `256 KiB`, RETIRADA ────
# El valor lo fija quien es dueno de los numeros de presupuesto, no este modulo:
# `body raw · bytes · 1 MiB · 413 · SIN code, cuerpo VACIO`. Coincide byte a
# byte con la FORMA que ya estaba implementada aqui.
#
# 🔴 PROPUSE `256 KiB` Y LO RETIRO, refutado por MEDIDA y no por autoridad. Mi
# derivacion tomaba `32 KiB` de cota del `intent` y `256`/`32` de cardinalidad;
# la politica cerrada movio `intent` a `64 KiB` y `external_causes` a `256`
# elementos. Recalculado el PEOR CUERPO LEGITIMO con las cotas nuevas —`intent`
# en su cota + `256 causes` + `256 external_causes` + escalares al tope—:
#
#     282.069 B (275 KiB)   ⇒  256 KiB ROMPERIA USO LEGITIMO
#                              1 MiB deja ×3,72 de margen
#
# La leccion, y es la que vale mas que el numero: **una cota derivada hereda las
# cotas de las que deriva**. La mia no era «conservadora»: estaba anclada a unas
# entradas que dejaron de ser las vigentes seis minutos antes de que la
# escribiera. Un techo derivado caduca cuando caduca su base.
#
# LAS TRES PIERNAS, rehechas sobre las entradas vigentes:
#   ⓐ CUBRE EL CUERPO LEGITIMO MAXIMO: `1.048.576 / 282.069 = ×3,72`. Falsador
#     `F-23`, que CONSTRUYE ese cuerpo en vez de creerse la cuenta.
#   ⓑ ORDEN CON LA COTA SEMANTICA, lo unico no negociable: el techo de
#     transporte tiene que ser ESTRICTAMENTE MAYOR que la cota del `intent`
#     (`65.536 B`). Si cortara antes, el `413` con causa del nucleo no llegaria
#     a emitirse y se perderia el rastro. `1.048.576 > 65.536` ✅ (`F-18`).
#   ⓒ MEMORIA: pico por peticion en vuelo = `2N + un chunk` = `2,31 MiB` ⇒ `100`
#     peticiones simultaneas son `231 MiB`. El `docker-compose.yml` NO declara
#     `mem_limit` ni `deploy.limits` (medido: `0` apariciones) y el `CMD` del
#     Dockerfile no pasa `--workers` ni `--limit-concurrency`, asi que hoy el
#     unico techo de memoria del piloto es la RAM del host: **el numero de
#     peticiones en vuelo NO esta acotado y este techo solo acota CADA UNA.**
#     Acotar el total es `--limit-concurrency` en el `CMD`, que no es de aqui.
#
# 🧪 COMO SE MIDE EN EL PILOTO (dos comandos, sin instalar nada; `N` y `N+1`):
#     N=1048576; URL=http://127.0.0.1:8077
#     head -c $N     /dev/zero | tr "\0" x > /tmp/n.bin
#     head -c $((N+1)) /dev/zero | tr "\0" x > /tmp/n1.bin
#     curl -s -o /dev/null -w '%{http_code}\n' -X POST $URL/native/v1/events \
#          -H 'Content-Type: application/json' --data-binary @/tmp/n.bin
#     # -> cualquier cosa MENOS 413 (el 401 sin credencial VALE: el techo no corto)
#     curl -s -o /dev/null -w '%{http_code}\n' ... --data-binary @/tmp/n1.bin
#     # -> 413, con cuerpo VACIO y sin `content-type`
# El `413` de `N+1` es el unico veredicto que este modulo promete; que el de `N`
# sea `401`, `422` o `202` lo decide el router, no el techo.
RAW_BODY_MAX_BYTES = 1 << 20

# ── LOS OTROS TRES EJES, QUE NO SON BYTES ──────────────────────────────────
# La politica de presupuestos cierra la dimension BYTES y no cubre ninguna de
# estas tres: un techo en bytes acota el DOCUMENTO y no acota el TRABAJO. Cada
# una se pasa de una forma que las otras dos no ven, y cada una trae su falsador
# `N`/`N+1` con centinela en `N+2`:
#
#   · MENSAJES: `4096` `http.request`. Para un cuerpo EN SU COTA son `256 B` de
#     media por chunk; `uvicorn` entrega del orden de `64`–`320 KiB` por mensaje,
#     asi que el margen sobre el framing real es de tres ordenes de magnitud. Un
#     cliente que trocea por debajo de eso no esta subiendo un documento: esta
#     gastando el bucle de eventos del UNICO worker del piloto.
#   · VACIOS: `64` chunks con `body=b""` y `more_body=True`. Son legales en ASGI
#     y no aportan ni un byte, asi que el techo en bytes NO los acota NUNCA.
#     Medido sobre la version anterior: `200.000` vacios con `max_bytes=16`
#     salian `200` habiendo retenido `38 MB`. `uvicorn`/`h11` emiten UN vacio
#     final y con `more_body` ya en `False`, que ni siquiera cuenta aqui.
#   · TIEMPO: `60 s` para el pre-read COMPLETO. Los dos contadores de arriba
#     acotan CUANTOS mensajes, no CUANDO llegan: `4096` chunks repartidos en
#     horas pasan los dos y ocupan el worker igual. `1 MiB / 60 s` = un suelo de
#     `17 KiB/s` (~`140 kbit/s`), por debajo de cualquier enlace que pueda
#     completar `1 MiB` con sentido. `None` lo desactiva, y solo es defendible
#     si hay un proxy delante que ponga el plazo — en el piloto NO lo hay
#     (`0` apariciones de nginx/traefik/caddy en el compose).
RAW_BODY_MAX_CHUNKS = 4096
RAW_BODY_MAX_EMPTY_CHUNKS = 64
RAW_BODY_READ_TIMEOUT_S = 60.0


class RawBodyLimitMiddleware:
    """Corta el cuerpo crudo en `max_bytes`, contando lo que se lee.

    ASGI puro y no `BaseHTTPMiddleware` a proposito: hace falta hablar con
    `receive` para contar chunk a chunk y decidir ANTES de que nadie llame a
    ``request.body()`` o ``request.json()``. Un middleware que trabaje sobre el
    `Request` ya materializado llega tarde: para entonces el cuerpo entero esta
    en memoria, que es justo lo que este techo evita.

    ⚖️ DOS LIMITES DISTINTOS QUE NO HAY QUE CONFUNDIR, Y SOLO UNO ES MIO:

    * **LIMITE CONTRACTUAL** — `max_bytes` bytes crudos LEIDOS, inclusivo. Es lo
      unico que este modulo promete y lo unico que un cliente puede comprobar
      desde fuera: `N` no da `413`, `N+1` da `413` con cuerpo vacio. Es funcion
      de lo que llega, no de lo que se declara, y no depende de la credencial.
    * **MEMORIA AGUAS ARRIBA (ASGI)** — lo que el servidor YA tiene en RAM antes
      de que este codigo pueda mirarlo. NO es una promesa de este modulo, NO se
      puede configurar desde aqui, y no aparece en ninguna respuesta::

          pico por peticion ≈ 2·max_bytes + len(mayor chunk que entregue el servidor)

      El `2·` es de aqui (acumulacion + la copia final del camino troceado; el
      camino de un solo chunk no copia). El segundo sumando NO es de aqui.

    ⛔ EL SEGUNDO SUMANDO NO SE PUEDE CONFIGURAR. Aqui llego a decir lo
    contrario —«se acota con `h11_max_incomplete_event_size` y equivalentes»— y
    era FALSO:

    * `h11_max_incomplete_event_size` es el tope de CABECERAS, no de cuerpo. Lo
      dice `h11` en su propio docstring: *«mostly sets a limit on the maximum
      size of the request/response line + headers»*. Y viene en `None`.
    * `uvicorn` NO EXPONE NINGUN tope de cuerpo. Censo de `uvicorn.Config` por
      mi mano en `0.40.0`: `ws_max_size=16777216` (WebSocket, no HTTP),
      `ws_max_queue`, `limit_concurrency=None`, `limit_max_requests=None`
      (cuentas, no tamanos) y `h11_max_incomplete_event_size=None` (cabeceras).
      **No es que nadie pusiera la bandera: no hay bandera que poner.**
    * Lo que SI acota el chunk es el control de flujo, que es implementacion y
      no contrato: `HIGH_WATER_LIMIT = 65536` (pausa la lectura) mas una lectura
      de socket de `asyncio` (`recv(256 KiB)`) ⇒ del orden de `320 KiB` por
      `http.request`. ⚠️ Leido en `uvicorn 0.40.0` sobre el bucle `asyncio`; el
      Dockerfile instala `uvicorn[standard]==0.39.*`, que trae `uvloop`, y bajo
      `uvloop` NO lo he medido. Otro servidor ASGI puede entregar mas de golpe.

    ⇒ quien quiera acotar ese sumando por CONTRATO lo pone DELANTE (un proxy con
    limite de cuerpo). En el piloto no hay proxy.

    🔴 LEE POR DELANTE, Y ESA ES LA DECISION QUE COSTO UN FALSADOR EN ROJO. La
    primera version contaba SOLO cuando la aplicacion pedia el cuerpo, y un
    `POST` de `64 B` sin `Authorization` sobre un techo de `16` salia `401` en
    vez de `413`: la ruta rechaza por credencial ANTES de leer nada, asi que el
    contador no llegaba a contar. Un techo cuyo veredicto depende de si la capa
    de arriba se molesta en leer no es un techo.

    ⛔ EL PRE-READ OCURRE ANTES DE CUALQUIER AUTENTICACION, Y ESO SE PAGA:

    * Un cliente ANONIMO puede hacer que el proceso lea y retenga hasta
      `max_bytes` (mas el chunk de arriba) por peticion en vuelo. El techo acota
      ese coste; no lo elimina. Quien quiera que el anonimo no pague nada
      necesita limite de conexiones/tasa DELANTE, que es otra capa.
    * El `413` PRECEDE al `401`: una peticion sin credencial y pasada de techo
      sale `413`, no `401`. Es deliberado —el veredicto de transporte no puede
      depender de la credencial— y no filtra nada: el `413` va vacio y es
      identico para credencial valida, invalida o ausente.
    * Por debajo del techo el gateway responde su envelope de siempre.

    🧮 LOS CHUNKS SE FUNDEN, Y ESO COSTO OTRO FALSADOR. La version anterior
    guardaba los mensajes TAL CUAL en una lista para reproducirlos: el cuerpo
    quedaba acotado por el techo, pero el numero de `dict` no, y cada uno cuesta
    ~200 B de objeto Python. Medido: `64 KiB` en chunks de `1 B` retenian
    `12,6 MB` — amplificacion `×192`. Ahora se acumulan los BYTES en un
    `bytearray` y se reproduce un unico `http.request`.

    Garantias, falsables por separado:

    * **No filtra**: si se pasa, la aplicacion NO se invoca. Ni ve un byte del
      cuerpo ni puede responder nada.
    * **Cuerpo vacio**: el `413` no lleva envelope, ni `code`, ni `receipt_id`.
      Fabricar un JSON que finja venir del contrato seria peor que no devolver
      nada: esta peticion nunca entro en el vocabulario. Y son DOS `413`
      distintos en la misma ruta: el de aqui va SIN envelope, y el del nucleo
      (`LIMITE_RECURSO`) va CON el. El cliente los distingue por el CUERPO.
    * **La desconexion del cliente no es un exceso**: un `http.disconnect` de
      verdad se reenvia tal cual y no produce `413`. Si el stream se corta asi,
      el cuerpo fundido se entrega con `more_body=True` —porque esta
      incompleto— y el `http.disconnect` detras, intacto.
    * **Un stream que no acaba tampoco pasa**: mas de `max_chunks` mensajes, o
      mas de `max_empty_chunks` vacios, se cortan con el mismo `413`. Son los
      unicos `413` que no hablan de bytes; se reusa el codigo a proposito,
      porque `RFC 9110 §15.5.14` es exactamente eso —el servidor no esta
      dispuesto a procesar esta peticion— y porque inventar un codigo nuevo
      seria meter vocabulario del Core en una capa que no lo tiene.
    * **Un stream que no acaba A TIEMPO da `408`**, no `413`: un cuerpo que
      tarda no es un cuerpo grande, y `RFC 9110 §15.5.9` dice literalmente que
      el servidor no recibio la peticion completa en el tiempo que estaba
      dispuesto a esperar. Mismo cuerpo vacio y mismo silencio de vocabulario.
    """

    def __init__(self, app, *, max_bytes: int = RAW_BODY_MAX_BYTES,
                 max_chunks: int = RAW_BODY_MAX_CHUNKS,
                 max_empty_chunks: int = RAW_BODY_MAX_EMPTY_CHUNKS,
                 read_timeout_s: float | None = RAW_BODY_READ_TIMEOUT_S) -> None:
        if max_bytes < 0:
            raise ValueError("el techo de transporte no puede ser negativo")
        if max_chunks < 1:
            raise ValueError("hacen falta al menos 1 mensaje de cuerpo")
        if max_empty_chunks < 0:
            raise ValueError("el tope de chunks vacios no puede ser negativo")
        if read_timeout_s is not None and read_timeout_s <= 0:
            raise ValueError("el plazo de lectura tiene que ser positivo o None")
        self.app = app
        self.max_bytes = max_bytes
        self.max_chunks = max_chunks
        self.max_empty_chunks = max_empty_chunks
        self.read_timeout_s = read_timeout_s

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        try:
            if self.read_timeout_s is None:
                reproducidos, status = await self._pre_leer(receive)
            else:
                async with asyncio.timeout(self.read_timeout_s):
                    reproducidos, status = await self._pre_leer(receive)
        except TimeoutError:
            # El plazo es del PRE-READ, no de la aplicacion: si vencio, la app
            # no se ha invocado todavia y no hay nada suyo que interrumpir.
            await self._rechazar(send, 408)
            return

        if status is not None:
            await self._rechazar(send, status)
            return

        async def receive_reproducido():
            if reproducidos:
                return reproducidos.pop(0)
            # Mas alla de lo que ya leimos manda el transporte real: por aqui
            # llegan los `http.disconnect` posteriores.
            return await receive()

        await self.app(scope, receive_reproducido, send)

    async def _pre_leer(self, receive) -> tuple[list[dict], int | None]:
        """Lee el cuerpo entero por delante. Devuelve `(reproducidos, status)`.

        `status` no nulo significa RECHAZO: la aplicacion no llega a invocarse y
        lo leido se tira sin tocarlo.
        """
        leidos = 0
        mensajes = 0
        vacios = 0
        primero: dict | None = None       # camino de UN chunk: sin copia
        cuerpo: bytearray | None = None   # camino troceado: bytes fundidos
        cola: list[dict] = []             # lo que NO era `http.request`
        completo = False                  # se vio un `more_body` falso

        while True:
            mensaje = await receive()
            if mensaje.get("type") != "http.request":
                # `http.disconnect` (o cualquier otro) se guarda para
                # reenviarlo intacto: no es un exceso y no lo convertimos en uno.
                cola.append(mensaje)
                break

            mensajes += 1
            if mensajes > self.max_chunks:
                return [], 413

            trozo = mensaje.get("body", b"") or b""
            hay_mas = bool(mensaje.get("more_body"))
            if leidos + len(trozo) > self.max_bytes:
                return [], 413
            if not trozo and hay_mas:
                vacios += 1
                if vacios > self.max_empty_chunks:
                    return [], 413

            leidos += len(trozo)
            if primero is None and cuerpo is None:
                primero = mensaje
            else:
                if cuerpo is None:
                    cuerpo = bytearray(primero.get("body", b"") or b"")
                    primero = None
                cuerpo += trozo

            if not hay_mas:
                completo = True
                break

        if primero is not None:
            reproducidos = [{"type": "http.request",
                             "body": primero.get("body", b"") or b"",
                             "more_body": not completo}]
        elif cuerpo is not None:
            reproducidos = [{"type": "http.request",
                             "body": bytes(cuerpo),
                             "more_body": not completo}]
        else:
            # No llego NI UN `http.request`: no se fabrica uno. Reproducir un
            # cuerpo vacio que el cliente nunca envio le mentiria a la app sobre
            # lo que paso en el transporte.
            reproducidos = []
        reproducidos.extend(cola)
        return reproducidos, None

    @staticmethod
    async def _rechazar(send, status: int) -> None:
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-length", b"0")]})
        await send({"type": "http.response.body", "body": b""})


class NativeInputError(Exception):
    """Error de protocolo estable que no debe llegar como un 500 de FastAPI."""

    def __init__(self, code: str, status: int, message: str):
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message


class StrictDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class SessionRequest(StrictDTO):
    ttl_s: Annotated[int, Field(ge=30, le=3600)] = C.DEFAULT_SESSION_TTL_S


class ClaimRequest(StrictDTO):
    lease_s: Annotated[int, Field(ge=1, le=C.MAX_OUTBOX_LEASE_S)] = 60


class ExternalCause(StrictDTO):
    ledger: Annotated[str, Field(min_length=1, max_length=512)]
    entry_eid: Annotated[str, Field(min_length=1, max_length=256)]


class TraceContext(StrictDTO):
    trace_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    span_id: Annotated[str, Field(pattern=r"^[0-9a-f]{16}$")]


class EventRequest(StrictDTO):
    type: str
    verb: str
    to: list[str]
    kind: str | None = None
    canonical_kind: str | None = None
    kind_registry_rev: int | None = None
    head: str
    body: str
    ledger: str | None = None
    causes: list[str] = Field(default_factory=list)
    external_causes: list[ExternalCause] = Field(default_factory=list)
    fenced_resource: str | None = None
    fencing_token: int | None = None
    trace: TraceContext | None = None


class MaterializedRequest(StrictDTO):
    entry_eid: Annotated[str, Field(min_length=1, max_length=256)]
    ledger: Annotated[str, Field(min_length=1, max_length=512)]
    claim_token: Annotated[str, Field(min_length=1, max_length=256)]
    byte_off: Annotated[int, Field(ge=0)] | None = None


class FailedRequest(StrictDTO):
    error: Annotated[str, Field(min_length=1, max_length=1024)]
    claim_token: Annotated[str, Field(min_length=1, max_length=256)]


class IndexedRequest(StrictDTO):
    index_ref: Annotated[str, Field(max_length=512)] | None = None


class DeliveryAckRequest(StrictDTO):
    ack_ref: Annotated[str, Field(max_length=512)] | None = None


class LeaseRequest(StrictDTO):
    ttl_s: Annotated[int, Field(ge=1, le=86400)] = 300


class OutboxOperationRequest(StrictDTO):
    reason: Annotated[str, Field(min_length=1, max_length=1024)]


class CommandRequest(StrictDTO):
    workstream_id: Annotated[str, Field(min_length=1, max_length=256)]
    revision: Annotated[int, Field(ge=0)]
    payload: dict[str, Any]
    causes: list[str] = Field(default_factory=list)
    external_causes: list[ExternalCause] = Field(default_factory=list)


class CommandTransitionRequest(StrictDTO):
    state: Literal["received", "executing", "succeeded", "failed", "cancelled"]
    detail: dict[str, Any] | None = None


class RuntimeObservationRequest(StrictDTO):
    workload_id: Annotated[str, Field(min_length=1, max_length=256)]
    supervisor_seq: Annotated[int, Field(ge=1, le=C.MAX_SUPERVISOR_SEQ)]
    observation_kind: str
    reason_code: str
    detector_state: str | None = None
    cpu_millis: Annotated[int, Field(ge=0, le=C.MAX_CPU_MILLIS)] | None = None
    rss_bytes: Annotated[int, Field(ge=0, le=C.MAX_RSS_BYTES)] | None = None
    heartbeat_age_ms: Annotated[int, Field(ge=0, le=C.MAX_HEARTBEAT_AGE_MS)] | None = None
    exit_code: Annotated[int, Field(ge=-(1 << 31), le=(1 << 31) - 1)] | None = None
    recovery_command_id: Annotated[str, Field(min_length=1, max_length=256)] | None = None


class RuntimeRecoveryRequest(StrictDTO):
    workload_id: Annotated[str, Field(min_length=1, max_length=256)]
    reason_code: str
    action_code: str
    fenced_resource: str
    fencing_token: Annotated[int, Field(ge=1)]


_ERRORS: tuple[tuple[type[BaseException], str, int], ...] = (
    (C.AuthError, "SESSION_INVALID", 401),
    (C.AttributionRejected, "ATTRIBUTION_REJECTED", 400),
    (C.GrammarRejected, "GRAMMAR_REJECTED", 422),
    (C.GrammarUnavailable, "GRAMMAR_UNAVAILABLE", 503),
    (C.RecipientUnresolved, "RECIPIENT_UNRESOLVED", 422),
    (C.ResourceLimitExceeded, "RESOURCE_LIMIT_EXCEEDED", 413),
    (C.KeyCharsetRejected, "KEY_CHARSET_REJECTED", 422),
    (C.IdempotencyConflict, "IDEMPOTENCY_CONFLICT", 409),
    (C.ReplayUnverifiable, "REPLAY_UNVERIFIABLE", 409),
    (C.CauseRejected, "CAUSE_REJECTED", 409),
    (C.FencedPairInvalid, "FENCED_PAIR_INVALID", 400),
    (C.FencingConflict, "FENCING_CONFLICT", 409),
    (C.LeaseConflict, "LEASE_CONFLICT", 409),
    (C.LedgerNotAllowed, "LEDGER_NOT_ALLOWED", 403),
    (C.PolicyDenied, "POLICY_DENIED", 403),
    # 409 y no 403: la puerta no está cerrada PARA ESTE LLAMANTE —lo estaría
    # para cualquiera—, y no es un 503 porque el servicio está sano y la
    # negativa es deliberada. Es un conflicto con el ESTADO del carril, que es
    # lo que un 409 dice.
    (C.AdmissionClosed, "ADMISSION_CLOSED", 409),
    (C.AdmissionConflict, "ADMISSION_CONFLICT", 409),
    (C.ObservationSequenceConflict, "OBSERVATION_SEQUENCE_CONFLICT", 409),
    (C.OrganizationConflict, "ORGANIZATION_CONFLICT", 409),
    (C.RecoveryConflict, "RECOVERY_CONFLICT", 409),
    (C.SubjectNotFound, "SUBJECT_NOT_FOUND", 404),
    (C.ReceiptStateInvalid, "RECEIPT_STATE_INVALID", 409),
    (C.OperationInvalid, "OPERATION_INVALID", 409),
    (C.CommandRevisionConflict, "COMMAND_REVISION_CONFLICT", 409),
    (C.CommandTransitionInvalid, "COMMAND_TRANSITION_INVALID", 409),
    (C.DeliveryConflict, "DELIVERY_CONFLICT", 409),
    (C.JournalReadOnly, "JOURNAL_READ_ONLY", 503),
    (C.OpenModeRestricted, "JOURNAL_OPEN_MODE_RESTRICTED", 503),
    (C.SchemaTooNew, "SCHEMA_TOO_NEW", 503),
    (C.SchemaMismatch, "SCHEMA_MISMATCH", 503),
    (C.SchemaIndeterminate, "SCHEMA_INDETERMINATE", 503),
    (C.SchemaCorrupt, "SCHEMA_CORRUPT", 503),
    (C.JournalNotInitialized, "JOURNAL_NOT_READY", 503),
    (C.PepperMismatch, "PEPPER_MISMATCH", 503),
    (C.MigrationSnapshotRequired, "MIGRATION_SNAPSHOT_REQUIRED", 503),
    (C.MigrationFailed, "MIGRATION_FAILED", 503),
    (C.PreflightRejected, "PREFLIGHT_REJECTED", 503),
    (C.PreflightUnstable, "PREFLIGHT_UNSTABLE", 503),
    (C.IdentityChanged, "JOURNAL_IDENTITY_CHANGED", 503),
    (C.LifecycleConflict, "JOURNAL_LIFECYCLE_CONFLICT", 503),
    (C.JournalError, "JOURNAL_INTERNAL_ERROR", 500),
)


def _error_response(code: str, status: int, message: str,
                    *, receipt_id: str | None = None,
                    details: Mapping[str, Any] | None = None) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if receipt_id:
        error["receipt_id"] = receipt_id
    if details:
        error.update(details)
    return JSONResponse(
        status_code=status,
        content=error,
        headers={"Cache-Control": "no-store"},
    )


def _journal_error(exc: C.JournalError) -> JSONResponse:
    for cls, code, status in _ERRORS:
        if isinstance(exc, cls):
            # La excepcion de nucleo puede citar ``resource``, ``ledger`` o un
            # identificador suministrado por el cliente. No se refleja: el code
            # y el receipt son el contrato publico; el detalle queda en el
            # registro interno.
            if status >= 500:
                message = "journal no disponible"
            elif status == 401:
                message = "sesion de runtime no valida"
            elif status == 404:
                message = "sujeto no encontrado"
            elif status == 403:
                message = "operacion no autorizada"
            else:
                message = "operacion rechazada por el journal"
            details = None
            if isinstance(exc, C.ObservationSequenceConflict):
                latest = getattr(exc, "latest", None)
                if latest is not None:
                    if type(latest) is not int or not 1 <= latest <= C.MAX_SUPERVISOR_SEQ:
                        return _error_response(
                            "JOURNAL_INTERNAL_ERROR", 500, "fallo interno del journal",
                            receipt_id=getattr(exc, "receipt_id", None))
                    details = {"max_supervisor_seq": latest}
            if isinstance(exc, C.ResourceLimitExceeded):
                dimension = getattr(exc, "dimension", None)
                field = getattr(exc, "field", None)
                limit = getattr(exc, "limit", None)
                seen_at_least = getattr(exc, "seen_at_least", None)
                if not (
                    dimension in {"bytes", "depth", "nodes", "cardinality"}
                    and field in C.CAMPOS_CONTRATO_CORE
                    and type(limit) is int
                    and type(seen_at_least) is int
                    and 0 <= limit < seen_at_least
                ):
                    return _error_response(
                        "JOURNAL_INTERNAL_ERROR", 500,
                        "fallo interno del journal",
                        receipt_id=getattr(exc, "receipt_id", None),
                    )
                details = {
                    "dimension": dimension,
                    "field": field,
                    "limit": limit,
                    "seen_at_least": seen_at_least,
                }
            return _error_response(code, status, message,
                                   # ADR-002 exige el mismo cuerpo para un sujeto
                                   # ajeno y uno inexistente. Las mutaciones siguen
                                   # auditadas en el Journal, bajo la identidad del
                                   # llamante; el identificador aleatorio de ese
                                   # rechazo no forma parte del 404 público.
                                   receipt_id=(None if isinstance(exc, C.SubjectNotFound)
                                               else getattr(exc, "receipt_id", None)),
                                   details=details)
    # Inalcanzable si aparece una subclase nueva: JournalError esta mapeada al
    # final. Se conserva como red de seguridad fail-closed.
    return _error_response("JOURNAL_INTERNAL_ERROR", 500,
                           "fallo interno del journal",
                           receipt_id=getattr(exc, "receipt_id", None))


class NativeJournalRoute(APIRoute):
    """Una sola traduccion de errores para todas las rutas nativas.

    Tambien cierra el fallo inesperado sin publicar excepciones, rutas locales o
    SQL. El servidor debe registrar el traceback por fuera de esta respuesta.
    """

    journal: C.Journal | None = None
    mutation_policy: Mapping[str, str | None] = MappingProxyType({})

    def get_route_handler(self):
        original = super().get_route_handler()

        async def guarded(request: Request):
            try:
                if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                    operation = getattr(self.endpoint, "_native_mutation", None)
                    if operation is None or operation not in self.mutation_policy:
                        raise NativeInputError(
                            "CAPABILITY_POLICY_UNDECLARED", 503,
                            "la operacion no tiene una capability declarada",
                        )
                    if operation == "sessions.open":
                        if self.mutation_policy[operation] != "__runtime_credential__":
                            raise NativeInputError(
                                "CAPABILITY_POLICY_UNDECLARED", 503,
                                "el bootstrap no declara credencial de runtime",
                            )
                        request.state.native_credential = _bearer(
                            request, bootstrap=True)
                    else:
                        if self.journal is None:
                            raise NativeInputError(
                                "CAPABILITY_POLICY_UNDECLARED", 503,
                                "el gateway no tiene Journal autoritativo",
                            )
                        request.state.native_token = _mutation_token(
                            request, self.journal, operation,
                            self.mutation_policy)
                return await original(request)
            except NativeInputError as exc:
                return _error_response(exc.code, exc.status, exc.message)
            except C.JournalError as exc:
                return _journal_error(exc)
            except Exception:
                return _error_response("INTERNAL_ERROR", 500,
                                       "fallo interno del gateway")

        return guarded


def native_mutation(operation: str):
    """Declara la politica de una ruta mutante antes de registrarla."""
    def decorate(endpoint):
        endpoint._native_mutation = operation
        return endpoint
    return decorate


def _bound_token(request: Request) -> str:
    """Token ya autenticado/autorizado por ``NativeJournalRoute``."""
    token = getattr(request.state, "native_token", None)
    if not isinstance(token, str) or not token:
        raise NativeInputError("CAPABILITY_POLICY_UNDECLARED", 503,
                               "la mutacion no paso por el middleware nativo")
    return token


def _bound_credential(request: Request) -> str:
    credential = getattr(request.state, "native_credential", None)
    if not isinstance(credential, str) or not credential:
        raise NativeInputError("CAPABILITY_POLICY_UNDECLARED", 503,
                               "el bootstrap no paso por el middleware nativo")
    return credential


def _reject_authority_channels(request: Request) -> None:
    if LEGACY_SHARED_HEADER in request.headers:
        raise NativeInputError(
            "SHARED_TOKEN_REJECTED", 401,
            "las rutas nativas no aceptan la credencial compartida legacy",
        )
    for header in request.headers:
        candidate = _alias(header)
        if candidate.startswith("x_llminbox_"):
            candidate = candidate[len("x_llminbox_"):]
        elif candidate.startswith("x_"):
            candidate = candidate[2:]
        if candidate in ATTRIBUTION_ALIASES:
            raise C.AttributionRejected(
                "la identidad de runtime no se acepta en cabeceras"
            )
    if request.query_params:
        for key in request.query_params:
            if _alias(key) in ATTRIBUTION_ALIASES:
                raise C.AttributionRejected(
                    "la identidad de runtime no se acepta en query"
                )
        raise NativeInputError("QUERY_NOT_ALLOWED", 400,
                               "esta ruta no acepta parametros query")


def _bearer(request: Request, *, bootstrap: bool = False) -> str:
    _reject_authority_channels(request)
    value = request.headers.get("authorization", "")
    scheme, separator, token = value.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip() \
            or token != token.strip() or " " in token:
        raise NativeInputError(
            "RUNTIME_CREDENTIAL_REQUIRED" if bootstrap else "SESSION_REQUIRED",
            401,
            "se requiere Authorization: Bearer con credencial de runtime"
            if bootstrap else "se requiere Authorization: Bearer con sesion de runtime",
        )
    return token


def _mutation_token(request: Request, journal: C.Journal, operation: str,
                    policy: Mapping[str, str | None]) -> str:
    """Comprueba que TODA mutacion tenga politica y pase una sesion valida.

    Esta lectura es un descarte temprano, no la autorizacion de dominio. El
    token original se pasa siempre al Journal, que revalida dentro de la misma
    transaccion de la mutacion. Una recarga que retire la capacidad tambien
    cambia la generacion y hace que esa segunda validacion falle.
    """
    if operation not in policy:
        raise NativeInputError(
            "CAPABILITY_POLICY_UNDECLARED", 503,
            "la operacion no tiene una capability declarada",
        )
    try:
        token = _bearer(request)
    except C.AttributionRejected as denied:
        # La cabecera Authorization no es atribucion: podemos usar su sesion
        # opaca para que el intento de forjar otra cabecera deje recibo. Si la
        # sesion tampoco vale, ``record_rejection`` devuelve None y no inventa
        # identidad.
        value = request.headers.get("authorization", "")
        scheme, separator, candidate = value.partition(" ")
        if separator and scheme.lower() == "bearer" and candidate:
            denied.receipt_id = journal.record_rejection(
                candidate, "ATTRIBUTION_REJECTED")
        raise
    view = journal.authenticate(token)
    if view is None:
        raise C.AuthError("se requiere sesion de runtime valida")
    required = policy[operation]
    if required is not None and required not in view.capabilities:
        denied = C.PolicyDenied(
            f"la operacion exige la capacidad `{required}`")
        denied.receipt_id = journal.record_rejection(token, "POLICY_DENIED")
        raise denied
    return token


def _read_token(request: Request, journal: C.Journal,
                required: str | None = None) -> str:
    token = _bearer(request)
    view = journal.authenticate(token)
    if view is None:
        raise C.AuthError("se requiere sesion de runtime valida")
    if required is not None and required not in view.capabilities:
        raise C.PolicyDenied(f"la lectura exige la capacidad `{required}`")
    return token


def _alias(name: str) -> str:
    return name.replace("-", "_").lower()


def _reject_attribution_keys(value: Any, *, where: str) -> None:
    if not isinstance(value, Mapping):
        return
    for key in value:
        if isinstance(key, str) and _alias(key) in ATTRIBUTION_ALIASES:
            raise C.AttributionRejected(
                f"la atribucion en `{where}` la deriva el servidor"
            )


def _audit_attribution(exc: C.AttributionRejected,
                       audit: tuple[C.Journal, str] | None) -> None:
    if audit is not None:
        exc.receipt_id = audit[0].record_rejection(
            audit[1], "ATTRIBUTION_REJECTED")


async def _body(request: Request, model: type[StrictDTO], *,
                audit: tuple[C.Journal, str] | None = None) -> StrictDTO:
    try:
        raw = await request.json()
    except Exception as exc:
        raise NativeInputError("INVALID_JSON", 400,
                               "el cuerpo debe ser JSON valido") from exc
    if not isinstance(raw, dict):
        raise NativeInputError("INVALID_BODY", 422,
                               "el cuerpo debe ser un objeto JSON")
    try:
        _reject_attribution_keys(raw, where="body")
    except C.AttributionRejected as exc:
        _audit_attribution(exc, audit)
        raise
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        # No se devuelve ``exc.errors()``: incluye el input rechazado y ese
        raise NativeInputError("INVALID_BODY", 422,
                               "el cuerpo no cumple el contrato") from exc


async def _optional_body(request: Request, model: type[StrictDTO], *,
                         audit: tuple[C.Journal, str] | None = None) -> StrictDTO:
    raw = await request.body()
    if not raw:
        return model()
    try:
        value = json.loads(raw)
    except Exception as exc:
        raise NativeInputError("INVALID_JSON", 400,
                               "el cuerpo debe ser JSON valido") from exc
    if not isinstance(value, dict):
        raise NativeInputError("INVALID_BODY", 422,
                               "el cuerpo debe ser un objeto JSON")
    try:
        _reject_attribution_keys(value, where="body")
    except C.AttributionRejected as exc:
        _audit_attribution(exc, audit)
        raise
    try:
        return model.model_validate(value)
    except ValidationError as exc:
        raise NativeInputError("INVALID_BODY", 422,
                               "el cuerpo no cumple el contrato") from exc


def _required_idempotency_key(request: Request) -> str:
    key = request.headers.get("idempotency-key")
    if not key or key != key.strip() or len(key) > 256:
        raise NativeInputError("IDEMPOTENCY_KEY_REQUIRED", 400,
                               "se requiere Idempotency-Key valida")
    return key


def _runtime_status_wire(row: Mapping[str, Any]) -> dict[str, Any]:
    result = {key: row[key] for key in (
        "lane", "workload_id", "role", "runtime_instance",
        "credential_generation", "status", "detector_state", "status_seq",
        "status_since", "last_observed_at", "cause_id", "transition_id",
        "receipt_id", "organization_revision",
    )}
    result["principal"] = row["principal_id"]
    return result


def _organization_wire(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Proyecta una sola revisión atómica al contrato de la consola (ADR-002)."""
    revision = snapshot["revision"]
    roles = {row["role"]: {
        "role": row["role"], "layer": row["layer"], "reports_to": None,
        "reviewers": [], "escalation_routes": [],
    } for row in snapshot["roles"]}
    for row in snapshot["reports"]:
        roles[row["role"]]["reports_to"] = row["reports_to"]
    for row in snapshot["reviewers"]:
        roles[row["role"]]["reviewers"].append(row["reviewer_role"])
    for row in snapshot["escalations"]:
        roles[row["role"]]["escalation_routes"].append({
            "trigger_code": row["trigger_code"], "target_role": row["target_role"],
        })
    return {
        "authority": True,
        "revision": {
            "lane": revision["lane"], "revision": revision["revision"],
            "source_digest": revision["source_sha256"],
            "freshness": {"attested": "atestiguada", "unattested": "no-atestiguada",
                          "stale": "rancia"}[revision["attestation_state"]],
            "activated_at": revision["activated_at"],
        },
        "roles": list(roles.values()),
        "workloads": [{"workload_id": row["workload_id"], "role": row["role"]}
                      for row in snapshot["workloads"]],
    }


def _session_wire(value: C.IssuedSession) -> dict[str, Any]:
    """Proyección pública cerrada: campos internos nuevos no saltan al wire."""
    return {
        "token": value.token,
        "runtime_instance": value.runtime_instance,
        "principal": value.principal,
        "role": value.role,
        "lane": value.lane,
        "expires_at": value.expires_at,
        "generation": value.generation,
        "principal_source": value.principal_source,
        "capabilities": list(value.capabilities),
    }


def _identity_wire(value: C.SessionView) -> dict[str, Any]:
    return {
        "principal": value.principal,
        "role": value.role,
        "lane": value.lane,
        "runtime_instance": value.runtime_instance,
        "expires_at": value.expires_at,
        "principal_source": value.principal_source,
        "capabilities": list(value.capabilities),
    }


def _acceptance_wire(value: C.Acceptance) -> dict[str, Any]:
    return {
        "event_id": value.event_id,
        "receipt_id": value.receipt_id,
        "replayed": value.replayed,
        "occurred_at": value.occurred_at,
        "payload_sha": value.payload_sha,
    }


def _receipt_wire(value: Mapping[str, Any]) -> dict[str, Any]:
    """Proyección pública tipada; ``event_id`` sólo existe para eventos."""
    subject_type = value.get("subject_kind")
    subject_id = value.get("subject_id")
    projected = {
        "receipt_id": value.get("receipt_id"),
        "state": value.get("current_state"),
        "subject_type": subject_type,
        "subject_id": subject_id,
    }
    if subject_type == "event":
        projected["event_id"] = subject_id
    return projected


def _projection_job_wire(value: C.ProjectionJob) -> dict[str, Any]:
    """Contrato del worker sin el identificador físico interno del principal."""
    return {
        "event_id": value.event_id,
        "ledger": value.ledger,
        "attempts": value.attempts,
        "claim_token": value.claim_token,
        "receipt_id": value.receipt_id,
        "recipients": list(value.recipients),
        "occurred_at": value.occurred_at,
        "payload_sha256": value.payload_sha256,
        "attestation": dict(value.attestation),
        "trace": dict(value.trace) if value.trace is not None else None,
        "principal": value.principal,
        "principal_source": value.principal_source,
        "role": value.role,
        "lane": value.lane,
        "runtime_instance": value.runtime_instance,
        "intent": dict(value.intent),
    }


def _lease_wire(value: C.Lease) -> dict[str, Any]:
    return {
        "resource": value.resource,
        "lane": value.lane,
        "runtime_instance": value.runtime_instance,
        "fencing_token": value.fencing_token,
        "expires_at": value.expires_at,
    }


def normalize_v8_credential_map(
    journal: C.Journal,
    credential_map: Mapping[str, Mapping[str, Any]],
    capability_grants: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Convierte el mapa V8 a la forma autoritativa del Journal.

    ``principal_id`` es opcional en V8. Si falta, NO se deriva del rol: se crea
    un nombre explicito y unico desde el ``credential_ref`` HMAC del Journal.
    Asi dos workloads con el mismo rol/carril siguen siendo dos principals y no
    se publica un hash simple de la credencial.

    Las capacidades no se aceptan dentro del mapa V8 ni desde HTTP. Se conceden
    por una segunda fuente server-side, indexada por el principal explicito. La
    salida siempre lleva ``principal`` y ``capabilities``, incluso si son cero.
    """
    if not isinstance(credential_map, Mapping):
        raise ValueError("el mapa V8 debe ser un objeto credencial -> metadata")
    grants = capability_grants or {}
    normalized: dict[str, dict[str, Any]] = {}
    principals: set[str] = set()
    for position, (credential, spec) in enumerate(credential_map.items(), start=1):
        if not isinstance(credential, str) or not credential:
            raise ValueError(f"credencial V8 #{position}: clave no valida")
        if not isinstance(spec, Mapping):
            raise ValueError(f"credencial V8 #{position}: metadata no valida")
        if set(spec) - {"rol", "carril", "principal_id"}:
            raise ValueError(f"credencial V8 #{position}: metadata fuera de contrato")
        role = spec.get("rol")
        lane = spec.get("carril")
        principal_id = spec.get("principal_id")
        if not isinstance(role, str) or not role.strip():
            raise ValueError(f"credencial V8 #{position}: falta rol no vacio")
        if not isinstance(lane, str) or not lane.strip():
            raise ValueError(f"credencial V8 #{position}: falta carril no vacio")
        if principal_id is not None and (
                not isinstance(principal_id, str) or not principal_id.strip()):
            raise ValueError(f"credencial V8 #{position}: principal_id no valido")
        principal = (principal_id.strip() if principal_id is not None
                     else "v8:" + journal.credential_ref(credential))
        if principal in principals:
            raise ValueError(
                "el mapa V8 asigna el mismo principal a dos credenciales; "
                "la rotacion debe ser explicita antes de fusionar workloads"
            )
        principals.add(principal)
        capabilities = grants.get(principal, ())
        if isinstance(capabilities, (str, bytes)) or not isinstance(
                capabilities, Sequence):
            raise ValueError(f"capacidades no validas para principal #{position}")
        clean_caps: list[str] = []
        for capability in capabilities:
            if not isinstance(capability, str) or not capability.strip():
                raise ValueError(f"capacidad no valida para principal #{position}")
            clean_caps.append(capability.strip())
        normalized[credential] = {
            "role": role.strip(),
            "lane": lane.strip(),
            "principal": principal,
            "capabilities": tuple(sorted(set(clean_caps))),
        }
    unknown = set(grants) - principals
    if unknown:
        # Tampoco se reflejan nombres: la configuracion puede contener secretos
        # por error y el fallo de arranque no debe imprimirlos.
        raise ValueError("hay grants para principals que no existen en el mapa V8")
    return normalized


def configure_journal_from_v8(
    journal: C.Journal,
    credential_map: Mapping[str, Mapping[str, Any]],
    capability_grants: Mapping[str, Sequence[str]] | None = None,
) -> int:
    """Recarga atomica del mapa despues de normalizarlo de forma cerrada."""
    return journal.reload_credential_map(
        normalize_v8_credential_map(journal, credential_map, capability_grants)
    )


def create_native_router(
    journal: C.Journal,
    *,
    prefix: str = API_PREFIX,
    mutation_capabilities: Mapping[str, str | None] = DEFAULT_MUTATION_CAPABILITIES,
) -> APIRouter:
    # Copia defensiva: una politica mutable cambiada despues de montar el router
    # seria una recarga sin generacion, justo lo que el Journal evita.
    policy = MappingProxyType(dict(mutation_capabilities))
    class BoundNativeJournalRoute(NativeJournalRoute):
        pass

    BoundNativeJournalRoute.journal = journal
    BoundNativeJournalRoute.mutation_policy = policy
    router = APIRouter(prefix=prefix, route_class=BoundNativeJournalRoute,
                       tags=["native-coordination"])

    @router.post("/sessions", status_code=201)
    @native_mutation("sessions.open")
    async def open_session(request: Request):
        credential = _bound_credential(request)
        dto = await _optional_body(request, SessionRequest)
        session = journal.open_session(credential, ttl_s=dto.ttl_s)
        return JSONResponse(
            status_code=201,
            content=_session_wire(session),
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    @router.post("/sessions/refresh")
    @native_mutation("sessions.refresh")
    async def refresh_session(request: Request):
        token = _bound_token(request)
        dto = await _optional_body(request, SessionRequest, audit=(journal, token))
        session = journal.refresh_session(token, ttl_s=dto.ttl_s)
        return JSONResponse(
            content=_session_wire(session),
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    @router.delete("/sessions/current", status_code=204)
    @native_mutation("sessions.revoke_current")
    async def revoke_current_session(request: Request):
        token = _bound_token(request)
        # Autenticar fuera y revocar despues abre una ventana TOCTOU y, desde el
        # Core fail-closed, ni siquiera satisface la firma de revoke_session.
        # El dominio resuelve y revoca la propia sesion en una sola transaccion.
        journal.revoke_current(token, reason="self_revoked")
        return Response(status_code=204)

    @router.get("/whoami")
    async def whoami(request: Request):
        token = _bearer(request)
        view = journal.authenticate(token)
        if view is not None:
            return _identity_wire(view)
        binding = journal.resolve_credential(token)
        if binding is None:
            raise C.AuthError("se requiere sesion o credencial de runtime valida")
        # La credencial bootstrap identifica, pero no es una sesion y por eso no
        # publica runtime_instance ni capacidades que solo una sesion puede usar.
        return {
            "principal": binding.principal,
            "role": binding.role,
            "lane": binding.lane,
            "principal_source": binding.principal_source,
        }

    @router.post("/events", status_code=202)
    @native_mutation("events.accept")
    async def accept_event(request: Request):
        token = _bound_token(request)
        idem = _required_idempotency_key(request)
        dto = await _body(request, EventRequest, audit=(journal, token))
        intent = {
            "type": dto.type,
            "verb": dto.verb,
            "to": dto.to,
            "head": dto.head,
            "body": dto.body,
        }
        if dto.canonical_kind is not None or dto.kind_registry_rev is not None:
            intent["canonical_kind"] = dto.canonical_kind
            intent["kind_registry_rev"] = dto.kind_registry_rev
            if dto.kind is not None:
                intent["kind"] = dto.kind
        else:
            intent["kind"] = dto.kind
        accepted = journal.accept_event(
            token,
            idempotency_key=idem,
            intent=intent,
            ledger=dto.ledger,
            causes=dto.causes,
            external_causes=[item.model_dump() for item in dto.external_causes],
            fenced_resource=dto.fenced_resource,
            fencing_token=dto.fencing_token,
            trace=dto.trace.model_dump() if dto.trace is not None else None,
        )
        return JSONResponse(status_code=200 if accepted.replayed else 202,
                            content=_acceptance_wire(accepted))

    @router.get("/receipts/{receipt_id}")
    async def receipt(receipt_id: str, request: Request):
        token = _bearer(request)
        return _receipt_wire(journal.receipt(token, receipt_id))

    @router.get("/receipts/{receipt_id}/transitions")
    async def transitions(receipt_id: str, request: Request):
        token = _bearer(request)
        return {"transitions": journal.transitions(token, receipt_id)}

    @router.get("/events/{event_id}/receipt")
    async def receipt_for_event(event_id: str, request: Request):
        token = _bearer(request)
        return _receipt_wire(journal.receipt_for_event(token, event_id))

    @router.post("/outbox/claims")
    @native_mutation("outbox.claim")
    async def claim_outbox(request: Request):
        token = _bound_token(request)
        dto = await _optional_body(request, ClaimRequest, audit=(journal, token))
        item = journal.claim_outbox(token, lease_s=dto.lease_s)
        return {"job": _projection_job_wire(item) if item is not None else None}

    @router.get("/outbox/pending")
    async def pending_outbox(request: Request):
        _read_token(request, journal, C.CAP_OUTBOX_WORKER)
        # El unico contador del nucleo es global. Exponerlo tras autenticar no
        # lo vuelve scoped: revelaria actividad de otros carriles. No se duplica
        # SQL de dominio en el router para fingir que el contrato ya existe.
        raise NativeInputError("CORE_PENDING_NOT_SCOPED", 501,
                               "el Journal no ofrece pending outbox por carril")

    @router.post("/outbox/{event_id}/materialized", status_code=204)
    @native_mutation("outbox.materialized")
    async def mark_materialized(event_id: str, request: Request):
        token = _bound_token(request)
        dto = await _body(request, MaterializedRequest, audit=(journal, token))
        journal.mark_materialized(
            token, event_id, entry_eid=dto.entry_eid,
            ledger=dto.ledger, claim_token=dto.claim_token,
            byte_off=dto.byte_off,
        )
        return Response(status_code=204)

    @router.post("/outbox/{event_id}/failed", status_code=204)
    @native_mutation("outbox.failed")
    async def mark_failed(event_id: str, request: Request):
        token = _bound_token(request)
        dto = await _body(request, FailedRequest, audit=(journal, token))
        journal.mark_outbox_failed(token, event_id, error=dto.error,
                                   claim_token=dto.claim_token)
        return Response(status_code=204)

    @router.post("/outbox/{event_id}/requeue", status_code=204)
    @native_mutation("outbox.requeue")
    async def requeue(event_id: str, request: Request):
        token = _bound_token(request)
        dto = await _body(request, OutboxOperationRequest, audit=(journal, token))
        journal.requeue_outbox(token, event_id, reason=dto.reason)
        return Response(status_code=204)

    @router.post("/outbox/{event_id}/abandon", status_code=204)
    @native_mutation("outbox.abandon")
    async def abandon(event_id: str, request: Request):
        token = _bound_token(request)
        dto = await _body(request, OutboxOperationRequest, audit=(journal, token))
        journal.abandon_outbox(token, event_id, reason=dto.reason)
        return Response(status_code=204)

    @router.post("/events/{event_id}/indexed", status_code=204)
    @native_mutation("events.indexed")
    async def mark_indexed(event_id: str, request: Request):
        token = _bound_token(request)
        dto = await _optional_body(request, IndexedRequest, audit=(journal, token))
        journal.mark_indexed(token, event_id, index_ref=dto.index_ref)
        return Response(status_code=204)

    @router.post("/events/{event_id}/acks")
    @native_mutation("events.ack")
    async def delivery_ack(event_id: str, request: Request):
        token = _bound_token(request)
        dto = await _optional_body(request, DeliveryAckRequest, audit=(journal, token))
        state = journal.mark_delivered(token, event_id, ack_ref=dto.ack_ref)
        return {"state": state}

    @router.post("/leases/{resource}", status_code=201)
    @native_mutation("leases.acquire")
    async def acquire_lease(resource: str, request: Request):
        token = _bound_token(request)
        dto = await _optional_body(request, LeaseRequest, audit=(journal, token))
        lease = journal.acquire_lease(token, resource, ttl_s=dto.ttl_s)
        return JSONResponse(status_code=201, content=_lease_wire(lease))

    @router.put("/leases/{resource}")
    @native_mutation("leases.renew")
    async def renew_lease(resource: str, request: Request):
        token = _bound_token(request)
        await _optional_body(request, LeaseRequest, audit=(journal, token))
        # ``Journal.renew_lease`` delega hoy en ``acquire_lease``: despues del
        # vencimiento puede relevar/re-adquirir y aumentar fencing. Un PUT renew
        # que hace acquire viola su propia semantica; se mantiene cerrado hasta
        # que el nucleo tenga una renovacion estricta y transaccional.
        raise NativeInputError("CORE_RENEW_NOT_STRICT", 501,
                               "el Journal aun no ofrece renew estricto")

    @router.delete("/leases/{resource}", status_code=204)
    @native_mutation("leases.release")
    async def release_lease(resource: str, request: Request):
        token = _bound_token(request)
        journal.release_lease(token, resource)
        return Response(status_code=204)

    @router.post("/commands", status_code=202)
    @native_mutation("commands.submit")
    async def submit_command(request: Request):
        token = _bound_token(request)
        dto = await _body(request, CommandRequest, audit=(journal, token))
        command_id, state = journal.submit_command(
            token, workstream_id=dto.workstream_id,
            revision=dto.revision, payload=dto.payload, causes=dto.causes,
            external_causes=[item.model_dump() for item in dto.external_causes],
        )
        return JSONResponse(status_code=202,
                            content={"command_id": command_id, "state": state})

    @router.post("/commands/{command_id}/transitions")
    @native_mutation("commands.advance")
    async def advance_command(command_id: str, request: Request):
        token = _bound_token(request)
        dto = await _body(request, CommandTransitionRequest, audit=(journal, token))
        transition_id = journal.advance_command(
            token, command_id, dto.state, detail=dto.detail,
        )
        return {"transition_id": transition_id, "state": dto.state}

    @router.get("/commands/{command_id}/executable")
    async def command_executable(command_id: str, request: Request):
        _read_token(request, journal, C.CAP_COMMAND_WORKER)
        # ``may_execute`` recibe solo el id y no filtra por la identidad que
        # pregunta. Publicarlo seria un oraculo cross-lane aunque devolviera un
        # simple booleano.
        raise NativeInputError("CORE_COMMAND_SCOPE_MISSING", 501,
                               "el Journal no ofrece may_execute autenticado por carril")

    @router.post("/runtimes/{runtime_instance}/observations", status_code=202)
    @native_mutation("runtime.observe")
    async def observe_runtime(runtime_instance: str, request: Request):
        token = _bound_token(request)
        idem = _required_idempotency_key(request)
        dto = await _body(request, RuntimeObservationRequest, audit=(journal, token))
        result = journal.record_runtime_observation(
            token, runtime_instance=runtime_instance, idempotency_key=idem,
            **dto.model_dump())
        return JSONResponse(status_code=202, content=asdict(result),
                            headers={"Cache-Control": "no-store"})

    @router.get("/runtimes")
    async def runtime_statuses(request: Request):
        token = _read_token(request, journal, C.CAP_RUNTIME_READ)
        rows = [_runtime_status_wire(row) for row in journal.runtime_statuses(token)]
        return JSONResponse(content=rows, headers={"Cache-Control": "no-store"})

    @router.get("/runtimes/{runtime_instance}")
    async def runtime_status(runtime_instance: str, request: Request):
        token = _read_token(request, journal, C.CAP_RUNTIME_READ)
        row = journal.runtime_status(token, runtime_instance)
        return JSONResponse(content=_runtime_status_wire(row),
                            headers={"Cache-Control": "no-store"})

    @router.post("/runtimes/{runtime_instance}/recoveries", status_code=202)
    @native_mutation("runtime.recover")
    async def recover_runtime(runtime_instance: str, request: Request):
        token = _bound_token(request)
        idem = _required_idempotency_key(request)
        dto = await _body(request, RuntimeRecoveryRequest, audit=(journal, token))
        result = journal.request_runtime_recovery(
            token, runtime_instance=runtime_instance, idempotency_key=idem,
            **dto.model_dump())
        return JSONResponse(status_code=202, content=asdict(result),
                            headers={"Cache-Control": "no-store"})

    @router.get("/organization")
    async def organization(request: Request):
        token = _read_token(request, journal, C.CAP_ORGANIZATION_READ)
        return JSONResponse(content=_organization_wire(journal.organization(token)),
                            headers={"Cache-Control": "no-store"})

    return router


def create_native_app(
    journal: C.Journal,
    *,
    prefix: str = API_PREFIX,
    mutation_capabilities: Mapping[str, str | None] = DEFAULT_MUTATION_CAPABILITIES,
    raw_body_max_bytes: int = RAW_BODY_MAX_BYTES,
    raw_body_max_chunks: int = RAW_BODY_MAX_CHUNKS,
    raw_body_max_empty_chunks: int = RAW_BODY_MAX_EMPTY_CHUNKS,
    raw_body_read_timeout_s: float | None = RAW_BODY_READ_TIMEOUT_S,
) -> FastAPI:
    """Arnes minimo para tests/piloto; produccion puede montar solo el router."""
    app = FastAPI(title="llminbox native coordination", docs_url=None,
                  redoc_url=None, openapi_url=None)
    app.include_router(create_native_router(
        journal, prefix=prefix, mutation_capabilities=mutation_capabilities))
    # El techo de transporte es ASGI, no de router: quien monte SOLO el router
    # en produccion tiene que envolver su propia app con este middleware, o no
    # tiene techo. Se dice aqui porque el docstring del modulo ofrece las dos
    # formas de montaje y solo una lo trae puesto.
    app.add_middleware(RawBodyLimitMiddleware, max_bytes=raw_body_max_bytes,
                       max_chunks=raw_body_max_chunks,
                       max_empty_chunks=raw_body_max_empty_chunks,
                       read_timeout_s=raw_body_read_timeout_s)
    return app


def assert_gateway_surface_is_decoupled() -> None:
    """Invariante barata para integradores y tests de empaquetado."""
    source = inspect.getsource(inspect.getmodule(create_native_router))
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import) and any(
                alias.name == "servicio" for alias in node.names):
            raise RuntimeError("el gateway nativo no puede depender de servicio.py")
        if isinstance(node, ast.ImportFrom) and node.module == "servicio":
            raise RuntimeError("el gateway nativo no puede depender de servicio.py")
