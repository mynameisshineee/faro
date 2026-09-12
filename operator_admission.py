"""Pod C: el router HTTP de la barrera de admisión del operador.

Se monta dentro del ÚNICO runtime root/Journal/runner del proceso — no hay
Journal, app ni ciclo de vida propios aquí. "Escritor único" es una propiedad
de RUTA, no de proceso: dentro del runtime root, sólo este router llama a
``Journal.transition_admissions`` (:mod:`coordination`); no se monta ningún
otro camino de mutación de la barrera al lado.

La credencial y la capacidad del operador (``admission_operator``, EXACTA) las
integra el composition root en el ÚNICO mapa V8 autoritativo mediante UNA sola
recarga (``native_gateway.configure_journal_from_v8`` /
``Journal.reload_credential_map``) — este módulo no liga credenciales
(``Journal.bind_credential``) ni abre sesiones: el bearer que sus rutas
consumen se obtiene en ``POST /native/v1/sessions`` del gateway principal, con
esa misma credencial ya integrada.

Invariantes que este módulo sostiene (y no el Journal, que ya sostiene los
suyos):

- **Carril de sesión.** Ninguna ruta acepta `lane` como parámetro. El carril
  sale de la sesión autenticada (`Journal.admissions`/`transition_admissions`
  ya lo exigen así); este módulo no añade un segundo camino para colarlo.
- **Cerrado por defecto · bootstrap explícito · el reinicio conserva.** Este
  módulo no escribe NUNCA en `admission_history` fuera de la petición POST
  explícita de un operador autenticado. No hay lógica de arranque que abra la
  puerta: la ausencia de fila ya significa `closed` (`Journal._admision_locked`),
  y un proceso que se reinicia vuelve a leer la MISMA base — el estado es del
  Journal, no de este router.
- **`open` exige runner sano Y del MISMO carril; `close`/`sealed` no pasan por
  ningún gate.** Abrir la puerta es una promesa de que hay quien drena lo que
  entra, en ESE carril — eso lo sabe el runner, no el dominio de admisión.
  Cerrar o sellar es la promesa contraria (que YA NO entra nada), y negarla
  por un runner caído impediría la única acción segura-por-defecto.
- **Capacidad EXACTA, no mera pertenencia.** Cada ruta exige que la sesión
  declare EXACTAMENTE ``{admission_operator}`` — ni de más ni de menos — antes
  de tocar el Journal. Esto es una comprobación ADICIONAL a la de Core
  (`journal.admissions`/`transition_admissions` ya exigen que la capacidad
  esté PRESENTE, dentro de su propia transacción): el precheck de este router
  usa `journal.authenticate`, que es barato y NO autoritativo — un reload que
  cambie las capacidades sube la generación, y si eso ocurre entre el precheck
  y la escritura, la revalidación de Core dentro de su propia transacción
  cierra la ventana.
"""
from __future__ import annotations

import os
import stat
from types import MappingProxyType
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import Field, ValidationError

import coordination as C
import native_gateway as G
import projector_runner as PR


PREFIX = "/native/v1/operator"

OPERATOR_CREDENTIAL_MIN_BYTES = 32
OPERATOR_CREDENTIAL_MAX_BYTES = 4096

# Allowlist de estados "sano" para abrir. Un estado nuevo que
# `projector_runner.py` no declara todavía (hoy sólo existe `disabled`) nace
# NO-listo hasta que se añada aquí a propósito: fail-closed, no negación de
# los estados que ya conocemos como malos.
_READY_RUNNER_STATES = frozenset({PR.RunnerState.IDLE, PR.RunnerState.PROJECTING})


class OperatorCredentialError(ValueError):
    """El fichero de credencial del operador no cumple su contrato de arranque."""


def load_operator_credential(path: str) -> bytes:
    """Lee la credencial ``0600`` del operador, separada del mapa V8 general.

    "Root-owned" es autoridad del COMPOSITION ROOT que despliega el fichero,
    no que este proceso corra como uid 0: el piloto corre como usuario sin
    privilegios (uid 1000 en producción), y exigir ``st_uid == 0`` dejaría el
    fichero 0600 ilegible para el propio servicio. La comprobación real es
    fichero regular + modo EXACTO ``0600`` + propiedad del UID EFECTIVO de
    este proceso — el mismo principio que ``runtime_root._read_regular_file``
    ya usa para el pepper, con el modo endurecido a un valor exacto en vez de
    "sin bits de grupo/otros".

    Si se quiere separar QUIEN COLOCA la credencial (un helper con privilegio
    propio que la escribe o la rota) de quien la SIRVE, ese helper corre con
    su propio uid y deja el fichero ya `chown`eado al uid efectivo de este
    servicio — no hace falta, y no se debe, correr ESTE proceso como root
    para conseguirlo.

    TOCTOU-safe: fd, ``O_NOFOLLOW``, identidad estable durante toda la
    lectura — mismo argumento que el pepper. Las tres banderas de apertura
    (``O_NOFOLLOW``, ``O_NONBLOCK``, ``O_CLOEXEC``) son EXIGIDAS, no
    "mejor-esfuerzo": una plataforma que no las declare no degrada en
    silencio a abrir sin esa protección — se niega a servir la credencial.
    ``O_NOFOLLOW`` evita seguir un symlink (la comprobación de propietario/modo
    de abajo pasaría a leer OTRO fichero); ``O_CLOEXEC`` evita que el fd
    sobreviva a un ``exec`` de un proceso hijo que no debería heredar la
    credencial; ``O_NONBLOCK`` evita bloquear para siempre si la ruta es un
    FIFO o un dispositivo en vez del fichero regular que se comprueba después.
    """
    if not path:
        raise OperatorCredentialError(
            "falta la ruta de la credencial de operador")
    flags = os.O_RDONLY
    for nombre in ("O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC"):
        try:
            flags |= getattr(os, nombre)
        except AttributeError as exc:
            raise OperatorCredentialError(
                f"esta plataforma no soporta {nombre}: la credencial de "
                f"operador no se puede abrir de forma segura"
            ) from exc
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise OperatorCredentialError(
            "la credencial de operador no se puede abrir de forma segura"
        ) from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise OperatorCredentialError(
                "la credencial de operador no es un fichero regular")
        if metadata.st_uid != os.geteuid():
            raise OperatorCredentialError(
                "la credencial de operador tiene que ser propiedad del uid "
                "efectivo de este proceso")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise OperatorCredentialError(
                "la credencial de operador tiene que tener permisos exactos 0600")
        if metadata.st_size > OPERATOR_CREDENTIAL_MAX_BYTES:
            raise OperatorCredentialError(
                f"la credencial de operador supera el maximo de "
                f"{OPERATOR_CREDENTIAL_MAX_BYTES} bytes")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(fd, min(remaining, 64 * 1024))
            if not chunk:
                raise OperatorCredentialError(
                    "la credencial de operador se corto al leerla")
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        final_metadata = os.fstat(fd)
        initial_identity = (
            metadata.st_dev, metadata.st_ino, metadata.st_size,
            metadata.st_mtime_ns, metadata.st_ctime_ns,
        )
        final_identity = (
            final_metadata.st_dev, final_metadata.st_ino, final_metadata.st_size,
            final_metadata.st_mtime_ns, final_metadata.st_ctime_ns,
        )
        if len(data) != metadata.st_size or final_identity != initial_identity:
            raise OperatorCredentialError(
                "la credencial de operador cambio durante la lectura")
        # NO se normaliza (nada de `.strip()`): un espacio o un salto de
        # linea colado en el fichero se RECHAZA en vez de admitirse en
        # silencio como si fuera parte, o no, del secreto.
        if data != data.strip():
            raise OperatorCredentialError(
                "la credencial de operador no puede llevar espacio en "
                "blanco al principio o al final")
        if b"\n" in data or b"\r" in data:
            raise OperatorCredentialError(
                "la credencial de operador tiene que ser una sola linea")
        if len(data) < OPERATOR_CREDENTIAL_MIN_BYTES:
            raise OperatorCredentialError(
                f"la credencial de operador exige al menos "
                f"{OPERATOR_CREDENTIAL_MIN_BYTES} bytes utiles")
        return data
    finally:
        os.close(fd)


def _runner_ready(runner: PR.ProjectorRunnerLike, *, lane: str) -> bool:
    """`sano` para ABRIR, y del MISMO carril que la sesión que pide abrir.

    `close`/`sealed` NUNCA llaman a esto — ver
    `create_operator_admission_router`: cerrar o sellar no dependen de que
    el runner esté vivo, y menos de en qué carril proyecte.

    `session_open` es EXIGIDO, no "cuando exista": si el snapshot no declara
    ese atributo, o leerlo falla por cualquier motivo, el runner NO está
    listo. Un ``RunnerSnapshot`` de hoy (``projector_runner.py`` sólo instala
    el Null Object `disabled`) no lo declara — y por eso, hoy, ningún runner
    real puede abrir la barrera hasta que una implementación lo declare
    explícitamente `True`. Fail-closed: la ausencia del campo nunca se lee
    como "no aplica".

    Un ``runner.snapshot()`` que LEVANTE (runner roto, en vez de simplemente
    no-sano) tampoco escapa de aquí: se trata como "no listo", nunca como un
    500 — un runner que no puede ni contestar su propio estado es, para
    quien pide abrir, exactamente lo mismo que uno deshabilitado.
    """
    try:
        snapshot = runner.snapshot()
    except Exception:
        return False
    if snapshot.state not in _READY_RUNNER_STATES:
        return False
    if not (snapshot.required and snapshot.thread_alive
            and snapshot.accepting_claims):
        return False
    if snapshot.fatal_code is not None:
        return False
    try:
        session_open = snapshot.session_open
    except Exception:
        return False
    if session_open is not True:
        return False
    return getattr(runner, "lane", None) == lane


def _admission_wire(state: C.AdmissionState) -> dict[str, Any]:
    return {"lane": state.lane, "verb": state.verb,
            "state": state.state, "epoch": state.epoch}


def _admissions_wire(states) -> dict[str, Any]:
    return {"admissions": [_admission_wire(s) for s in states]}


def _outbox_wire(counts: C.OutboxCounts) -> dict[str, Any]:
    return {"lane": counts.lane, "pending": counts.pending,
            "failed": counts.failed, "unresolved": counts.unresolved}


def _rollback_status_wire(status: C.RollbackStatus) -> dict[str, Any]:
    return {
        "lane": status.lane,
        "admissions": [_admission_wire(s) for s in status.admissions],
        "outbox": _outbox_wire(status.outbox),
        "durable_v": status.durable_v,
        "certifiable": status.certifiable,
    }


def _rollback_certificate_wire(
        certificate: C.RollbackCertificate) -> dict[str, Any]:
    return {
        "lane": certificate.lane,
        "admissions": [_admission_wire(s) for s in certificate.admissions],
        "outbox": _outbox_wire(certificate.outbox),
        "durable_v": certificate.durable_v,
        "certified_at": certificate.certified_at,
    }


def _projector_not_ready() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"code": "PROJECTOR_NOT_READY",
                 "message": "el runner del projector esta deshabilitado, no "
                            "sano o no sirve este carril"},
        headers={"Cache-Control": "no-store"},
    )


def _alias(name: str) -> str:
    """Misma normalización que ``native_gateway._alias``: aquí se referencia
    el VOCABULARIO compartido (``ATTRIBUTION_ALIASES``), no se copia."""
    return name.replace("-", "_").lower()


def _read_bearer_token(request: Request) -> str:
    """Extrae el ``Bearer`` y rechaza los canales de atribución legacy.

    Duplica a propósito el mecanismo de ``native_gateway._bearer`` — privado a
    ese módulo, y este módulo no importa símbolos con guion bajo de otro
    módulo — pero toma el VOCABULARIO de las mismas constantes públicas
    (``ATTRIBUTION_ALIASES``, ``LEGACY_SHARED_HEADER``): la parte que sí
    podría desviarse en silencio no se copia, se referencia.
    """
    if G.LEGACY_SHARED_HEADER in request.headers:
        raise G.NativeInputError(
            "SHARED_TOKEN_REJECTED", 401,
            "las rutas del operador no aceptan la credencial compartida legacy",
        )
    for header in request.headers:
        candidate = _alias(header)
        if candidate.startswith("x_llminbox_"):
            candidate = candidate[len("x_llminbox_"):]
        elif candidate.startswith("x_"):
            candidate = candidate[2:]
        if candidate in G.ATTRIBUTION_ALIASES:
            raise C.AttributionRejected(
                "la identidad de runtime no se acepta en cabeceras")
    if request.query_params:
        for key in request.query_params:
            if _alias(key) in G.ATTRIBUTION_ALIASES:
                raise C.AttributionRejected(
                    "la identidad de runtime no se acepta en query")
        raise G.NativeInputError("QUERY_NOT_ALLOWED", 400,
                                 "esta ruta no acepta parametros query")
    value = request.headers.get("authorization", "")
    scheme, separator, token = value.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip() \
            or token != token.strip() or " " in token:
        raise G.NativeInputError(
            "SESSION_REQUIRED", 401,
            "se requiere Authorization: Bearer con sesion de runtime",
        )
    return token


def _bound_operator_token(request: Request) -> str:
    token = getattr(request.state, "native_token", None)
    if not isinstance(token, str) or not token:
        raise G.NativeInputError(
            "CAPABILITY_POLICY_UNDECLARED", 503,
            "la mutacion no paso por el middleware nativo",
        )
    return token


def _require_exact_operator_capability(journal: C.Journal, token: str) -> C.SessionView:
    """Precheck DECLARATIVO: la sesión tiene que declarar EXACTAMENTE
    ``{admission_operator}`` — ni de más ni de menos.

    Esto es ADICIONAL a lo que ya exige Core: `journal.admissions` y
    `journal.transition_admissions` comprueban que la capacidad esté
    PRESENTE, dentro de su propia transacción — mera pertenencia. Aquí se
    pide además que no haya NINGUNA otra: una credencial que junte
    `admission_operator` con cualquier otra capacidad no debe poder usar
    esta ruta, aunque el Journal la dejara pasar.

    `journal.authenticate` está FUERA de transacción (ver su docstring): un
    reload que cambie las capacidades entre este precheck y la escritura
    sube la generación, y la revalidación de Core dentro de su propia
    transacción cierra esa ventana — este precheck no la sustituye.
    """
    view = journal.authenticate(token)
    if view is None:
        raise C.AuthError("se requiere sesion de runtime valida")
    if set(view.capabilities) != {C.CAP_ADMISSION_OPERATOR}:
        denied = C.PolicyDenied(
            "esta ruta exige EXACTAMENTE la capacidad `admission_operator`, "
            "ni de mas ni de menos"
        )
        denied.receipt_id = journal.record_rejection(token, "POLICY_DENIED")
        raise denied
    return view


class AdmissionTransitionRequest(G.StrictDTO):
    target: Literal["open", "closed", "sealed"]
    expected_epochs: dict[str, int]
    # El vocabulario cerrado de motivos (`ADMISSION_OPERATOR_REASON_CODES`) lo
    # valida el Journal, no aquí: mantenerlo en un solo sitio evita que este
    # DTO y `coordination.py` diverjan sobre qué motivo es válido.
    reason_code: Annotated[str, Field(min_length=1, max_length=128)]


async def _required_body(
    request: Request, model: type[G.StrictDTO], *,
    audit: tuple[C.Journal, str],
) -> G.StrictDTO:
    """Cuerpo JSON obligatorio, validado contra ``model``.

    Reproduce (no delega en, por el mismo motivo de guion-bajo del resto del
    módulo) el mecanismo de ``native_gateway._reject_attribution_keys`` +
    ``_audit_attribution``: una clave de atribución colada en el cuerpo
    (``lane``, ``principal``, ``actor``…) se rechaza con
    ``ATTRIBUTION_REJECTED`` — no con el ``INVALID_BODY`` genérico que
    ``StrictDTO(extra="forbid")`` produciría por su cuenta — y deja recibo
    vía ``journal.record_rejection``, igual que cualquier otro intento de
    colar atribución en las rutas nativas.
    """
    try:
        raw = await request.json()
    except Exception as exc:
        raise G.NativeInputError("INVALID_JSON", 400,
                                 "el cuerpo debe ser JSON valido") from exc
    if not isinstance(raw, dict):
        raise G.NativeInputError("INVALID_BODY", 422,
                                 "el cuerpo debe ser un objeto JSON")
    for key in raw:
        if isinstance(key, str) and _alias(key) in G.ATTRIBUTION_ALIASES:
            exc = C.AttributionRejected(
                "la atribucion en `body` la deriva el servidor")
            journal, token = audit
            exc.receipt_id = journal.record_rejection(token, "ATTRIBUTION_REJECTED")
            raise exc
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise G.NativeInputError("INVALID_BODY", 422,
                                 "el cuerpo no cumple el contrato") from exc


def create_operator_admission_router(
    journal: C.Journal, *, runner: PR.ProjectorRunnerLike, prefix: str = PREFIX,
) -> APIRouter:
    """Router de un solo escritor: dentro del runtime root que lo monta,
    ninguna otra ruta llama a ``transition_admissions`` por HTTP.

    ``journal`` y ``runner`` se inyectan — son el journal/runner del runtime
    root, no una instancia propia: este router no abre ni migra el Journal
    (eso es del lifespan que lo monta, igual que
    ``native_gateway.create_native_router``), y no liga credenciales — el
    bearer que consume ya viene autenticado por `POST /native/v1/sessions`
    del gateway principal, contra el mismo mapa autoritativo.
    """
    policy = MappingProxyType({
        "admissions.transition": C.CAP_ADMISSION_OPERATOR,
        "rollback.certify": C.CAP_ADMISSION_OPERATOR,
    })

    class BoundOperatorRoute(G.NativeJournalRoute):
        pass

    BoundOperatorRoute.journal = journal
    BoundOperatorRoute.mutation_policy = policy
    router = APIRouter(prefix=prefix, route_class=BoundOperatorRoute,
                       tags=["operator-admission"])

    @router.get("/admission")
    async def read_admissions(request: Request):
        token = _read_bearer_token(request)
        _require_exact_operator_capability(journal, token)
        # `journal.admissions` autentica y exige `admission_operator` DENTRO
        # de la misma transaccion de lectura (ver coordination.py) — este
        # router no repite ESE chequeo (mera pertenencia), sólo lo deja
        # fallar como `AuthError`/`PolicyDenied`, que `BoundOperatorRoute` ya
        # traduce. El precheck de arriba es el que exige EXACTITUD.
        states = journal.admissions(token)
        return JSONResponse(
            content=_admissions_wire(states),
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/admission/transition")
    @G.native_mutation("admissions.transition")
    async def transition_admission(request: Request):
        token = _bound_operator_token(request)
        _require_exact_operator_capability(journal, token)
        dto = await _required_body(
            request, AdmissionTransitionRequest, audit=(journal, token))
        if dto.target == "open":
            # El carril del gate lo deriva `Journal.admissions`, NUNCA el
            # cuerpo de la peticion — ver docstring del modulo, "carril de
            # sesion". `closed`/`sealed` no pasan por ninguna de estas dos
            # lineas: van derechas a `transition_admissions`.
            lane = journal.admissions(token)[0].lane
            if not _runner_ready(runner, lane=lane):
                return _projector_not_ready()
        states = journal.transition_admissions(
            token, target=dto.target, expected_epochs=dto.expected_epochs,
            reason_code=dto.reason_code,
        )
        return JSONResponse(
            content=_admissions_wire(states),
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/rollback/status")
    async def rollback_status(request: Request):
        token = _read_bearer_token(request)
        _require_exact_operator_capability(journal, token)
        status = journal.rollback_status(token)
        return JSONResponse(
            content=_rollback_status_wire(status),
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/rollback/certify")
    @G.native_mutation("rollback.certify")
    async def certify_rollback(request: Request):
        token = _bound_operator_token(request)
        _require_exact_operator_capability(journal, token)
        certificate = journal.certify_rollback(token)
        return JSONResponse(
            content=_rollback_certificate_wire(certificate),
            headers={"Cache-Control": "no-store"},
        )

    return router
