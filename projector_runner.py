"""Ciclo de vida del proyector de Markdown.

Dos runners y UN selector cerrado. El modo del proyector es independiente de la
política de mutaciones legacy y no se infiere de ella.

``DisabledProjectorRunner`` es el Null Object: no crea sesiones, hebras ni
claims del outbox. ``ActiveProjectorRunner`` sólo es seleccionable con backend
y carril explícitos; la barrera de admisión y la certificación de rollback se
componen por encima, en el runtime/operator.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class RunnerConfigurationError(ValueError):
    """El modo exige un runner que el runtime no puede construir."""


class RollbackNotCertifiable(RuntimeError):
    """El runner no puede demostrar que el outbox esté drenado."""


class RunnerState(str, Enum):
    DISABLED = "disabled"
    NEW = "new"
    STARTING = "starting"
    IDLE = "idle"
    PROJECTING = "projecting"
    DRAINING = "draining"
    BLOCKED = "blocked"
    FATAL = "fatal"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class RunnerSnapshot:
    state: RunnerState
    required: bool
    thread_alive: bool
    accepting_claims: bool
    in_flight: bool
    outbox_certifiable: bool
    pending: int | None
    failed: int | None
    fatal_code: str | None
    # OBSERVABILIDAD DE LA SESIÓN, no de la cola. `session_open` dice si queda
    # una credencial de worker viva a nombre de este runner, y `close_failures`
    # cuenta los cierres que el backend RECHAZÓ. Van con default para no romper
    # a quien ya construye snapshots por palabra clave; van en el snapshot y no
    # en un log porque una fuga que sólo se cuenta en un log no la ve nadie.
    session_open: bool = False
    close_failures: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.state, RunnerState):
            raise TypeError("state debe ser RunnerState")
        for name in (
            "required", "thread_alive", "accepting_claims", "in_flight",
            "outbox_certifiable",
        ):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} debe ser bool")
        if type(self.session_open) is not bool:
            raise TypeError("session_open debe ser bool")
        if type(self.close_failures) is not int or self.close_failures < 0:
            raise ValueError("close_failures debe ser un entero no negativo")
        for name in ("pending", "failed"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} debe ser un entero no negativo o None")
        if self.state is RunnerState.DISABLED:
            if (self.required or self.thread_alive or self.accepting_claims
                    or self.in_flight or self.outbox_certifiable
                    or self.pending is not None or self.failed is not None
                    or self.fatal_code is not None
                    or self.session_open or self.close_failures):
                raise ValueError("disabled no puede publicar actividad ni contadores")
        elif self.required is not True:
            raise ValueError("todo runner no disabled debe declarar required=true")
        if self.accepting_claims and not self.thread_alive:
            raise ValueError("no se aceptan claims sin hebra viva")
        if self.in_flight and not self.thread_alive:
            raise ValueError("no puede haber trabajo en vuelo sin hebra viva")
        if self.outbox_certifiable:
            if (self.accepting_claims or self.in_flight
                    or self.pending != 0 or self.failed != 0):
                raise ValueError(
                    "un outbox certificable debe estar cerrado, drenado y sin fallos")
        if self.state is RunnerState.FATAL:
            if not isinstance(self.fatal_code, str) or not self.fatal_code:
                raise ValueError("fatal exige fatal_code")
        elif self.fatal_code is not None:
            raise ValueError("fatal_code sólo es válido en estado fatal")


class ProjectorRunnerLike(Protocol):
    def start(self) -> None: ...
    def snapshot(self) -> RunnerSnapshot: ...
    # `quiesce` promete cerrar la admisión LOCAL y esperar al ciclo en vuelo.
    # `drain` promete vaciar el outbox — y por eso el runner activo lo NIEGA:
    # vaciarlo exige una barrera de admisión que hoy no existe. Son dos verbos
    # porque son dos promesas, y fundirlos fue el defecto.
    def quiesce(self, *, timeout_s: float) -> RunnerSnapshot: ...
    def drain(self, *, timeout_s: float) -> RunnerSnapshot: ...
    def stop(self) -> None: ...
    def certify_rollback(self) -> RunnerSnapshot: ...


_DISABLED = RunnerSnapshot(
    state=RunnerState.DISABLED,
    required=False,
    thread_alive=False,
    accepting_claims=False,
    in_flight=False,
    outbox_certifiable=False,
    pending=None,
    failed=None,
    fatal_code=None,
)


class DisabledProjectorRunner:
    """Null Object terminal: no evalúa ni toca ninguna dependencia."""

    def start(self) -> None:
        return None

    def snapshot(self) -> RunnerSnapshot:
        return _DISABLED

    def quiesce(self, *, timeout_s: float) -> RunnerSnapshot:
        return _DISABLED

    def drain(self, *, timeout_s: float) -> RunnerSnapshot:
        # El Null Object no reclama nada, así que no hay cola suya que drenar y
        # el drenado es trivialmente cierto. Sigue SIN certificar: `_DISABLED`
        # lleva `outbox_certifiable=False`.
        return _DISABLED

    def stop(self) -> None:
        return None

    def certify_rollback(self) -> RunnerSnapshot:
        raise RollbackNotCertifiable(
            "projector disabled: el outbox no es certificable")


# ---------------------------------------------------------------------------
# Runner ACTIVO. Existe como clase; NO es seleccionable por configuración.
# ---------------------------------------------------------------------------

# Espejo literal de ``coordination.CAP_OUTBOX_WORKER``. Este módulo es una hoja
# y no importa el núcleo; el espejo se falsa con un test que compara los dos
# símbolos, porque una constante duplicada que nadie compara es una que deriva.
CAP_OUTBOX_WORKER = "outbox_worker"

# Mínimo privilegio: EXACTAMENTE esta capacidad. No un superconjunto. La misma
# configuración del piloto ya emite un principal así (`runtime_root.py:81`,
# ``"outbox.project": (C.CAP_OUTBOX_WORKER,)``), de modo que exigir igualdad no
# pide nada que el despliegue no pueda dar.
LEAST_PRIVILEGE_CAPABILITIES = frozenset({CAP_OUTBOX_WORKER})

# La precondición que mantiene `active` fuera del selector. No es una promesa de
# trabajo futuro: es la razón por la que `certify_rollback` niega SIEMPRE.
ACTIVE_MODE_PRECONDITION = (
    "una barrera de admisión que cierre la entrada al outbox: sin ella un "
    "`pending == 0` medido no se puede congelar, porque entre la medida y el "
    "rollback cabe una escritura nueva"
)

# Techo del arriendo, ESPEJO del que impone el núcleo en `claim_outbox` y en el
# DTO del gateway (`ClaimRequest.lease_s`, `le=3600`). Se valida AQUÍ, en el
# constructor, y no en el ciclo 6: un `lease_s` fuera de rango descubierto a
# mitad de la proyección se publica como `PROJECTION_CYCLE_FAILED`, que manda a
# depurar el proyector por lo que es un error de configuración.
MIN_LEASE_S = 1
MAX_LEASE_S = 3_600

# Cuánto ANTES del vencimiento se renueva. La renovación es explícita porque el
# núcleo emite sesiones con TTL (`DEFAULT_SESSION_TTL_S = 900`) y un runner de
# vida larga que no renueve NO es un runner de vida larga: es uno que se muere a
# los quince minutos publicando el código equivocado.
DEFAULT_RENEW_MARGIN_S = 60.0

FATAL_SESSION_OPEN_FAILED = "SESSION_OPEN_FAILED"
FATAL_SESSION_INVALID = "SESSION_INVALID"
FATAL_LANE_MISMATCH = "LANE_MISMATCH"
FATAL_CAPABILITIES_NOT_LEAST_PRIVILEGE = "CAPABILITIES_NOT_LEAST_PRIVILEGE"
FATAL_BACKEND_CONTRACT = "BACKEND_CONTRACT_VIOLATED"
FATAL_PROJECTION_CYCLE_FAILED = "PROJECTION_CYCLE_FAILED"
# La hebra no llegó a nacer. Sin código propio, un `start()` que no puede lanzar
# hebra dejaba `accepting_claims=True` con `thread_alive=False`: el snapshot no
# quedaba MAL, quedaba ILEGIBLE —su propio invariante lo hacía levantar—, y la
# única lectura del runner moría justo cuando hacía falta leerla.
FATAL_THREAD_START_FAILED = "THREAD_START_FAILED"
# El token venció y renovarlo sería resucitarlo. El núcleo ya trata esa
# resurrección como un defecto (`refresh_session` mide el reloj DENTRO de la
# transacción justo para impedirla); aquí se le da nombre propio.
FATAL_SESSION_EXPIRED = "SESSION_EXPIRED"
FATAL_SESSION_REFRESH_FAILED = "SESSION_REFRESH_FAILED"
# Lo que antes se tragaba. Un fallo del PROPIO runner no es una violación de
# contrato del backend, y publicarlo como tal manda a auditar al inocente.
FATAL_RUNNER_INTERNAL_ERROR = "RUNNER_INTERNAL_ERROR"
FATAL_RUNNER_INTERRUPTED = "RUNNER_INTERRUPTED"


class RunnerLifecycleError(RuntimeError):
    """Se pidió una transición que el ciclo de vida no admite."""


class RunnerStopTimeout(RuntimeError):
    """La hebra no terminó dentro del plazo; la sesión NO se cierra debajo."""


class RunnerDrainNotSupported(RuntimeError):
    """Se pidió drenar el outbox a un runner que sólo sabe aquietarse."""


def _es_lane_valido(value: object) -> bool:
    return type(value) is str and bool(value)


def _es_entero_no_negativo(value: object) -> bool:
    # `bool` ES `int` para `isinstance`; `type(...) is int` es lo que excluye
    # a `True`/`False` de colarse como el entero que dicen ser.
    return type(value) is int and value >= 0


@dataclass(frozen=True, slots=True)
class OutboxCounts:
    """Medida del backend, ATADA al carril que la certifica.

    🩸 `lane` SE AÑADE Y SE VALIDA. La medida no declaraba de qué carril
    hablaba: un backend mal cableado -o un proceso que aloja varios runners
    de carriles distintos- podía devolver el `pending` de un carril ajeno y
    el runner lo publicaba como si fuera el suyo, sin que nada lo delatara.
    Tipada además para que un `0` inventado no pase por `int`, ni un `True`
    -que ES `int` para `isinstance`- pase por el entero que dice ser.

    Esta clase es una implementación CONFORME de `OutboxCountsLike`, no el
    contrato en sí -ver esa clase para el porqué-, y además la MEDIDA LOCAL,
    CONGELADA, que `_measure()` construye a partir de una lectura ÚNICA de
    los campos del backend: nunca se reenvía el objeto del backend tal cual,
    para que nadie vuelva a leer sus getters una segunda vez.
    """

    lane: str
    pending: int
    failed: int

    def __post_init__(self) -> None:
        if not _es_lane_valido(self.lane):
            raise ValueError("lane debe ser un texto no vacío")
        for name in ("pending", "failed"):
            if not _es_entero_no_negativo(getattr(self, name)):
                raise ValueError(f"{name} debe ser un entero no negativo")


class OutboxCountsLike(Protocol):
    """La forma que CUALQUIER medida de outbox tiene que declarar.

    🩸 PROPIEDADES DE SÓLO LECTURA, no atributos nominales de una clase
    concreta. Un backend real construye su PROPIO DTO -el canónico de
    `coordination.OutboxCounts`, no `OutboxCounts` de aquí arriba- y un
    `isinstance` contra la clase local de este módulo lo rechazaría por no
    ser LITERALMENTE la misma clase, aunque declare el mismo
    `lane`/`pending`/`failed` con los mismos tipos exactos. Dos clases
    iguales en forma y distintas en identidad no son «el backend rompió el
    contrato»: es la comprobación equivocada. `ProjectionBackend.outbox_counts`
    declara ESTE Protocol como retorno -no `OutboxCounts`- precisamente para
    que la anotación diga la verdad sobre lo que `_measure()` exige.
    """

    @property
    def lane(self) -> str: ...
    @property
    def pending(self) -> int: ...
    @property
    def failed(self) -> int: ...


class WorkerSessionLike(Protocol):
    """Lo único que el runner lee de una sesión emitida.

    ``expires_at`` NO es opcional y ése es el contrato: una sesión que no dice
    cuándo muere no se puede renovar antes de tiempo, y un runner que no puede
    renovar no puede prometer vida larga. El núcleo ya lo publica
    (``coordination.IssuedSession.expires_at``), así que exigirlo no pide nada
    que el despliegue no pueda dar.
    """

    token: str
    lane: str
    capabilities: tuple[str, ...]
    expires_at: float


class ProjectorLike(Protocol):
    """La firma real de ``projector.MarkdownProjector.project_next``."""

    def project_next(self, *, lease_s: int = 60) -> object | None: ...


class ProjectionBackend(Protocol):
    """Frontera del runner con el núcleo. Cuatro actos, ninguno más.

    El runner no ve el Journal ni el proyector concreto: por construcción no
    puede usar el token para nada que no sea proyectar y contar.
    """

    def open_worker_session(self) -> WorkerSessionLike: ...
    def projector_for(self, session: WorkerSessionLike) -> ProjectorLike: ...
    def outbox_counts(self, session: WorkerSessionLike) -> OutboxCountsLike: ...
    def close_worker_session(self, session: WorkerSessionLike) -> None: ...
    # RENOVACIÓN EXPLÍCITA, y es un VERBO de la frontera, no una llamada que el
    # runner improvise. Devuelve una sesión NUEVA; el que la implemente sobre
    # `Journal.refresh_session` hereda que la rotación revoca la sesión padre en
    # la MISMA transacción — por eso el runner no cierra la vieja: cerrarla
    # sería cerrar algo ya revocado y contar una fuga que no existe. `ttl_s` lo
    # PIDE el runner (`_renovar_si_toca` calcula cuánto), no lo decide el
    # backend: sin un TTL pedido explícitamente el runner no puede saber si la
    # hija que le devolvieron le alcanza para el ciclo que sigue.
    def refresh_worker_session(
        self, session: WorkerSessionLike, *, ttl_s: int) -> WorkerSessionLike: ...


class _StartRefusal(RunnerConfigurationError):
    """Rechazo de arranque con el código fatal que le corresponde."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _positive_number(value: object, name: str) -> float:
    if type(value) not in (int, float) or value != value or value <= 0:
        raise RunnerConfigurationError(f"{name} debe ser un número positivo")
    return float(value)


class ActiveProjectorRunner:
    """Proyecta el outbox de UN carril con una sesión de mínimo privilegio.

    El constructor es PURO: valida argumentos y no abre sesión, no crea hebra,
    no toca disco y no llama al backend. Todo efecto empieza en ``start()``, de
    modo que construir el runner en el composition root no compromete nada si
    el arranque se aborta antes del lifespan.

    ``certify_rollback`` niega SIEMPRE, y ``snapshot().outbox_certifiable`` es
    constantemente ``False``: mientras no exista barrera de admisión, un outbox
    drenado es una fotografía, no un certificado. Por la MISMA razón ``drain()``
    niega y el trabajo real lo hace ``quiesce()``, que sólo promete cerrar la
    admisión local.

    El privilegio NO es configurable: ``LEAST_PRIVILEGE_CAPABILITIES`` es una
    constante del módulo y no un argumento, porque una perilla que elige contra
    qué conjunto se compara la igualdad es la puerta de atrás de esa igualdad.
    """

    def __init__(
        self,
        backend: ProjectionBackend,
        *,
        lane: str,
        lease_s: int = 60,
        idle_poll_s: float = 0.25,
        failure_backoff_s: float = 1.0,
        max_consecutive_failures: int = 5,
        # 90 y no 30: concede al cierre al menos el presupuesto nominal del
        # arriendo por defecto. NO es una cota de ejecución: una llamada al
        # backend puede durar más que su lease y sólo la hebra sabe cuándo acabó.
        stop_timeout_s: float = 90.0,
        renew_margin_s: float = DEFAULT_RENEW_MARGIN_S,
        clock=time.time,
    ):
        if backend is None:
            raise RunnerConfigurationError("el runner activo exige un backend")
        if type(lane) is not str or not lane or "\0" in lane:
            raise RunnerConfigurationError("el carril debe ser un texto no vacío")
        if type(lease_s) is not int or not MIN_LEASE_S <= lease_s <= MAX_LEASE_S:
            # `type(...) is not int` y no `isinstance`: `True` ES un int para
            # `isinstance`, y `lease_s=True` colaría como el entero 1.
            raise RunnerConfigurationError(
                f"lease_s debe ser un entero entre {MIN_LEASE_S} y {MAX_LEASE_S}")
        if type(max_consecutive_failures) is not int or max_consecutive_failures < 1:
            raise RunnerConfigurationError(
                "max_consecutive_failures debe ser un entero >= 1")
        self._backend = backend
        self._lane = lane
        self._lease_s = lease_s
        self._idle_poll_s = _positive_number(idle_poll_s, "idle_poll_s")
        self._failure_backoff_s = _positive_number(
            failure_backoff_s, "failure_backoff_s")
        self._stop_timeout_s = _positive_number(stop_timeout_s, "stop_timeout_s")
        self._renew_margin_s = _positive_number(renew_margin_s, "renew_margin_s")
        if not callable(clock):
            raise RunnerConfigurationError("clock debe ser invocable")
        self._clock = clock
        # POLÍTICA DE PRESUPUESTO, NO PROMESA DE CANCELACIÓN. Un timeout menor
        # que el lease ni siquiera espera el periodo de autoridad solicitado al
        # claim. Aun con esta relación válida, una I/O colgada puede exceder
        # ambos valores: `stop()` lo hace visible y conserva la sesión en vez de
        # fingir que canceló una ejecución Python que no puede cancelar.
        if self._stop_timeout_s < float(lease_s):
            raise RunnerConfigurationError(
                f"stop_timeout_s={self._stop_timeout_s} es menor que "
                f"lease_s={lease_s}: el presupuesto de parada ni siquiera "
                f"cubre el arriendo solicitado")
        self._max_consecutive_failures = max_consecutive_failures
        # NO ES CONFIGURABLE, Y ÉSE ES EL ARREGLO. Era un argumento del
        # constructor, así que la garantía «mínimo privilegio POR IGUALDAD» valía
        # exactamente lo que valiera el argumento: pasando
        # `frozenset({"outbox_worker", "outbox_operator"})` la comprobación de
        # igualdad seguía pasando —contra el superconjunto— y admitía una sesión
        # de operador. La perilla ERA el bypass de la propiedad que decía
        # defender. El privilegio de este runner no es una decisión del que lo
        # construye: es una propiedad de lo que el runner ES.
        self._required_capabilities = LEAST_PRIVILEGE_CAPABILITIES

        self._cond = threading.Condition(threading.RLock())
        self._state = RunnerState.NEW
        self._accepting = False
        self._in_flight = False
        # DISTINTO de `_in_flight`: cubre el ciclo ENTERO -renovación incluida-
        # desde antes de `_renovar_si_toca()` hasta justo antes de cualquier
        # `wait()`. `_in_flight` sólo se enciende YA DENTRO de `project_next`;
        # mientras la renovación (fuera del cerrojo, puede bloquear en red
        # real) estaba en curso, `_in_flight` seguía en `False` y `quiesce()`
        # -que sólo miraba `_in_flight`- volvía diciendo «nada en vuelo» con
        # el ciclo todavía trabajando.
        self._cycle_active = False
        self._pending: int | None = None
        self._failed: int | None = None
        self._fatal_code: str | None = None
        self._consecutive_failures = 0
        self._stop_requested = False
        # PERSISTE igual que `_stop_requested`, y por el mismo motivo: sin
        # esto, un `quiesce()` pedido en `new` o a mitad de `starting` se
        # perdía en cuanto `start()` resolvía, porque nada le decía a
        # `start()` que NO debía reabrir `accepting`.
        self._quiesce_requested = False
        self._thread: threading.Thread | None = None
        self._session: WorkerSessionLike | None = None
        self._projector: ProjectorLike | None = None
        self._session_closed = False
        self._close_failures = 0
        # ORDEN DE CERROJOS, y es la única regla que este módulo impone:
        # `_close_lock` -> `_cond`. NUNCA al revés. `_cerrar_sesion_una_vez` se
        # llama SIEMPRE fuera de `_cond`; sostener `_cond` a través de una
        # llamada al backend es lo que convierte un backend lento en un runner
        # colgado que ni siquiera puede publicar su snapshot.
        self._close_lock = threading.Lock()

    # -- lectura ----------------------------------------------------------
    @property
    def lane(self) -> str:
        return self._lane

    def snapshot(self) -> RunnerSnapshot:
        with self._cond:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> RunnerSnapshot:
        # Ni `accepting_claims` ni `in_flight` se enmascaran con `thread_alive`:
        # si alguna vez se contradicen, el invariante de RunnerSnapshot tiene
        # que SONAR. Un snapshot que corrige la incoherencia que debe delatar
        # comparte mecanismo con el sujeto que vigila.
        thread = self._thread
        return RunnerSnapshot(
            state=self._state,
            required=True,
            thread_alive=thread is not None and thread.is_alive(),
            accepting_claims=self._accepting,
            in_flight=self._in_flight,
            # Constante por contrato, no por estado: ver docstring de la clase.
            outbox_certifiable=False,
            pending=self._pending,
            failed=self._failed,
            fatal_code=self._fatal_code,
            # Una sesión adoptada y no confirmada como cerrada está ABIERTA.
            # Se deriva del par (hay sesión, nadie confirmó el cierre) y no de
            # una bandera aparte: dos fuentes para el mismo hecho se separan.
            session_open=(self._session is not None and not self._session_closed),
            close_failures=self._close_failures,
        )

    # -- arranque ---------------------------------------------------------
    def start(self) -> None:
        with self._cond:
            if self._state is not RunnerState.NEW:
                raise RunnerLifecycleError(
                    f"start() sólo es válido en {RunnerState.NEW.value}; "
                    f"el runner está en {self._state.value}")
            self._state = RunnerState.STARTING
        session = None
        try:
            session = self._backend.open_worker_session()
            self._check_session(session)
            projector = self._backend.projector_for(session)
            if projector is None or not hasattr(projector, "project_next"):
                raise _StartRefusal(
                    FATAL_BACKEND_CONTRACT,
                    "el backend no devolvió un proyector con project_next")
        except BaseException as start_failure:
            code = getattr(start_failure, "code", None)
            if not isinstance(code, str):
                code = (FATAL_SESSION_OPEN_FAILED if session is None
                        else FATAL_BACKEND_CONTRACT)
            # ADOPTAR ANTES DE CERRAR. La sesión se publica en `_session` aunque
            # el arranque se rechace: si no, el cierre no tiene sujeto, no es
            # reintentable y `session_open` mentiría diciendo que no hay nada
            # abierto justo cuando lo hay.
            self._adopt_session(session)
            self._enter_fatal(code)
            self._close_session_or_group(start_failure)
            raise
        # ── LA CARRERA CON `stop()` DURANTE `starting` ────────────────────
        # `open_worker_session()` corre FUERA del cerrojo (tiene que: habla con
        # el núcleo). En esa ventana `stop()` no podía ver la sesión —aún no
        # estaba publicada— y se marchaba dejándola VIVA: el runner quedaba
        # parado y el token de worker seguía siendo válido hasta su TTL, sin
        # dueño. Se publica y se RE-LEE la intención de parada bajo el mismo
        # cerrojo; quien llegue segundo cierra, y el cierre es idempotente.
        with self._cond:
            self._session = session
            self._projector = projector
            stopping = self._stop_requested
            if stopping:
                self._accepting = False
                if self._state is not RunnerState.FATAL:
                    self._state = RunnerState.STOPPED
                self._cond.notify_all()
        if stopping:
            cleanup = self._cerrar_sesion_una_vez()
            if cleanup is not None:
                raise cleanup
            return
        spawn_failure: BaseException | None = None
        with self._cond:
            # RELEÍDO AQUÍ, NO CAPTURADO EN EL CERROJO DE ARRIBA: entre soltar
            # aquél y adquirir ÉSTE hay DOS adquisiciones distintas del mismo
            # `_cond`, no una sola sección crítica, y en esa ventana cabe un
            # `quiesce()` entero. Una copia tomada antes de la ventana no lo
            # vería -el mismo defecto que tenía `stop()` antes de re-leer
            # `_stop_requested` bajo el cerrojo que publica la sesión.
            if self._quiesce_requested:
                # LA MISMA CARRERA DE ARRIBA, SIN CERRAR LA SESIÓN. `quiesce()`
                # sólo promete admisión LOCAL cerrada -no es `stop()` y no
                # toca la sesión-, pero si `start()` reabriera `accepting` sin
                # mirar esto, la petición de `quiesce()` durante `new` o
                # `starting` se perdería exactamente igual que se perdía la de
                # `stop()` antes de re-leer `_stop_requested`. No hay ciclo que
                # lanzar: nunca se llegó a aceptar un claim.
                self._state = RunnerState.DRAINING
                self._cond.notify_all()
                return
            self._accepting = True
            self._state = RunnerState.IDLE
            self._thread = threading.Thread(
                target=self._run, name=f"projector-{self._lane}", daemon=True)
            # `start()` se queda DENTRO del lock a propósito: fuera de él existe
            # una ventana en la que `accepting_claims` ya es True y la hebra aún
            # no está viva, y ese par hace saltar el invariante del snapshot.
            try:
                self._thread.start()
            except BaseException as spawn:
                # Y POR ESO MISMO HAY QUE DESHACERLO AQUÍ DENTRO. Si el sistema
                # no puede dar una hebra más, el par que acabamos de escribir
                # (`accepting=True`, hebra muerta) no deja el snapshot MAL: lo
                # deja ILEGIBLE, porque su propio invariante levanta. La lectura
                # del runner tiene que sobrevivir al fallo que hay que leer.
                self._thread = None
                self._accepting = False
                self._in_flight = False
                self._state = RunnerState.FATAL
                self._fatal_code = FATAL_THREAD_START_FAILED
                self._cond.notify_all()
                spawn_failure = spawn
            else:
                # SIN ESTO, quien esperaba (`stop()` o `quiesce()`) a que
                # `starting` resolviera sólo se enteraba cuando su PROPIO
                # plazo expiraba: la única rama que notificaba era el fallo de
                # arranque. Un `start()` que termina bien tiene que despertar
                # a quien lo esté esperando tan rápido como uno que termina mal.
                self._cond.notify_all()
        if spawn_failure is not None:
            self._close_session_or_group(spawn_failure)
            raise spawn_failure

    def _check_session(self, session: object) -> None:
        token = getattr(session, "token", None)
        lane = getattr(session, "lane", None)
        capabilities = getattr(session, "capabilities", None)
        if type(token) is not str or not token:
            raise _StartRefusal(
                FATAL_SESSION_INVALID, "la sesión no trae un token utilizable")
        if lane != self._lane:
            raise _StartRefusal(
                FATAL_LANE_MISMATCH,
                f"la sesión es del carril {lane!r} y el runner sirve {self._lane!r}")
        try:
            granted = frozenset(capabilities)
        except TypeError:
            raise _StartRefusal(
                FATAL_SESSION_INVALID,
                "la sesión no publica un conjunto de capacidades") from None
        if granted != self._required_capabilities:
            # Igualdad, no inclusión: una sesión que además puede `requeue` o
            # `abandon` es una sesión de operador, y este runner no lo es. Y el
            # término de la derecha es una CONSTANTE del módulo, no un argumento:
            # ver el constructor.
            raise _StartRefusal(
                FATAL_CAPABILITIES_NOT_LEAST_PRIVILEGE,
                f"la sesión concede {sorted(granted)!r} y el mínimo privilegio "
                f"exige exactamente {sorted(self._required_capabilities)!r}")
        expires_at = getattr(session, "expires_at", None)
        if type(expires_at) not in (int, float) or expires_at != expires_at:
            raise _StartRefusal(
                FATAL_SESSION_INVALID,
                "la sesión no publica un vencimiento numérico: sin él no se "
                "puede renovar antes de tiempo y la vida larga es una promesa "
                "que nadie puede cumplir")
        if float(expires_at) <= self._clock():
            raise _StartRefusal(
                FATAL_SESSION_EXPIRED,
                "la sesión emitida ya está vencida: adoptarla sería empezar con "
                "una credencial muerta")

    def _adopt_session(self, session: object | None) -> None:
        """Publica la sesión para que EXISTA un sujeto al que cerrar."""
        if session is None:
            return
        with self._cond:
            self._session = session
            self._cond.notify_all()

    def _cerrar_sesion_una_vez(self) -> BaseException | None:
        """Cierra la sesión y marca cerrado SÓLO si el backend lo confirmó.

        Devuelve la excepción del backend en vez de levantarla: quien llama
        decide si la encadena (arranque) o la propaga (parada).

        🩸 LA BANDERA IBA ANTES DE LA LLAMADA Y ESO CONVERTÍA UN FALLO EN UNA
        FUGA MUDA. `_session_closed = True` se escribía y DESPUÉS se pedía el
        cierre; si el backend levantaba, la bandera ya decía «cerrada», la
        idempotencia de `stop()` impedía el reintento y el token de worker
        sobrevivía hasta su TTL sin que nadie lo supiera. Ahora la bandera es
        un ACUSE: sólo la escribe un cierre que volvió sin excepción, así que
        un cierre fallido se puede REINTENTAR y, mientras tanto, `session_open`
        lo dice en el snapshot.

        El cerrojo dedicado da exclusión REAL entre `start()` y `stop()`
        concurrentes: el segundo espera al primero y luego ve el acuse, en vez
        de volver antes de que el cierre haya ocurrido.
        """
        with self._close_lock:
            with self._cond:
                session = self._session
                if session is None or self._session_closed:
                    return None
            try:
                self._backend.close_worker_session(session)
            except BaseException as cleanup:
                with self._cond:
                    self._close_failures += 1
                    self._cond.notify_all()
                return cleanup
            with self._cond:
                self._session_closed = True
                self._cond.notify_all()
            return None

    def _close_session_or_group(self, failure: BaseException) -> None:
        cleanup = self._cerrar_sesion_una_vez()
        if cleanup is not None:
            raise BaseExceptionGroup(
                "falló el arranque Y falló el cierre de la sesión de worker",
                [failure, cleanup],
            ) from None

    def _enter_fatal(self, code: str) -> None:
        with self._cond:
            self._state = RunnerState.FATAL
            self._fatal_code = code
            self._accepting = False
            self._in_flight = False
            self._cond.notify_all()

    # -- ciclo ------------------------------------------------------------
    def _run(self) -> None:
        try:
            while True:
                with self._cond:
                    if self._stop_requested:
                        break
                    if self._state is RunnerState.FATAL:
                        break
                    if not self._accepting:
                        self._cycle_active = False
                        self._state = RunnerState.DRAINING
                        self._cond.notify_all()
                        self._cond.wait(self._idle_poll_s)
                        continue
                    # DESDE AQUÍ, ANTES DE RENOVAR. `quiesce()` espera a esto,
                    # no sólo a `_in_flight`: ver el comentario de `__init__`.
                    self._cycle_active = True
                # RENOVACIÓN FUERA DEL CERROJO: habla con el backend, y
                # sostener `_cond` a través de una llamada de red cuelga hasta
                # el snapshot. Va ANTES de proyectar, no después: renovar
                # después es renovar el token con el que ya trabajaste.
                fatal = self._renovar_si_toca()
                if fatal is not None:
                    self._enter_fatal(fatal)
                    break
                with self._cond:
                    # RE-LECTURA tras soltar el cerrojo para renovar: en esa
                    # ventana cabe un `stop()` o un `quiesce()` enteros.
                    if (self._stop_requested or not self._accepting
                            or self._state is RunnerState.FATAL):
                        self._cycle_active = False
                        continue
                    self._state = RunnerState.PROJECTING
                    self._in_flight = True
                    projector = self._projector
                    lease_s = self._lease_s
                outcome: object | None = None
                failure: BaseException | None = None
                try:
                    outcome = projector.project_next(lease_s=lease_s)
                except Exception as exc:  # el fallo del ciclo NO mata la hebra
                    failure = exc
                if not self._after_cycle(outcome, failure):
                    break
        except BaseException as internal:
            # NO SE TRAGA. Antes cualquier `BaseException` salía como
            # `BACKEND_CONTRACT_VIOLATED`: un fallo del PROPIO runner se
            # publicaba como culpa del backend y mandaba a auditar al inocente,
            # y una interrupción se leía igual que un contrato roto. Cada clase
            # tiene su código CERRADO y observable en el snapshot.
            #
            # Sólo el CÓDIGO cruza la frontera; ni el mensaje ni el tipo de la
            # excepción se publican ni se persisten. Es la misma regla que el
            # núcleo aplica a `mark_outbox_failed`: el detalle libre pertenece a
            # los sensores del proceso, no a un contrato que otros leen.
            self._enter_fatal(
                FATAL_RUNNER_INTERRUPTED
                if isinstance(internal, (KeyboardInterrupt, SystemExit))
                else FATAL_RUNNER_INTERNAL_ERROR)
        finally:
            with self._cond:
                self._accepting = False
                self._in_flight = False
                # RED DE SEGURIDAD para el camino FATAL de `_renovar_si_toca`:
                # ese `break` salta directo aquí sin pasar por la RE-LECTURA
                # que limpia `_cycle_active` en los demás caminos. Sin este
                # `finally`, un `quiesce()` esperando `_cycle_active` se
                # quedaría colgado hasta su propio `timeout_s` aunque la
                # hebra ya hubiera muerto.
                self._cycle_active = False
                if self._state is not RunnerState.FATAL:
                    self._state = RunnerState.STOPPED
                self._cond.notify_all()

    def _after_cycle(
            self, outcome: object | None, failure: BaseException | None) -> bool:
        """Cierra el ciclo y dice si la hebra debe seguir viva."""
        counts, malformed = self._measure()
        with self._cond:
            self._in_flight = False
            if malformed:
                self._cycle_active = False
                self._state = RunnerState.FATAL
                self._fatal_code = FATAL_BACKEND_CONTRACT
                self._accepting = False
                self._cond.notify_all()
                return False
            # `None` es «no medido», jamás `0`: un cero inventado convierte una
            # sonda rota en un outbox drenado.
            self._pending = None if counts is None else counts.pending
            self._failed = None if counts is None else counts.failed
            if failure is None:
                self._consecutive_failures = 0
            else:
                self._consecutive_failures += 1
                if self._consecutive_failures >= self._max_consecutive_failures:
                    self._cycle_active = False
                    self._state = RunnerState.FATAL
                    self._fatal_code = FATAL_PROJECTION_CYCLE_FAILED
                    self._accepting = False
                    self._cond.notify_all()
                    return False
            if self._stop_requested:
                self._cycle_active = False
                self._cond.notify_all()
                return False
            if not self._accepting:
                self._cycle_active = False
                self._state = RunnerState.DRAINING
            elif failure is not None:
                # ANTES DEL WAIT: `quiesce()` que sólo mirase `_in_flight`
                # volvería aquí creyendo que no queda nada por drenar, con la
                # hebra a punto de reintentar el mismo ciclo tras el backoff.
                self._cycle_active = False
                self._state = RunnerState.BLOCKED
                self._cond.notify_all()
                self._cond.wait(self._failure_backoff_s)
                return True
            elif outcome is None:
                self._cycle_active = False
                self._state = RunnerState.IDLE
                self._cond.notify_all()
                self._cond.wait(self._idle_poll_s)
                return True
            self._cond.notify_all()
            return True

    def _renovar_si_toca(self) -> str | None:
        """Renueva ANTES del vencimiento. Devuelve un código fatal, o ``None``.

        Tres decisiones que no son cosmética:

        · **Ya vencida ⇒ NO se renueva.** Se para con `SESSION_EXPIRED`. Pedir
          la rotación de un token muerto es justo lo que el núcleo llama
          resucitar, y su `refresh_session` mide el reloj DENTRO de la
          transacción para impedirlo. Reintentarlo aquí sería empujar contra un
          guard ajeno esperando que ceda.
        · **La sesión renovada se REVALIDA entera.** Una rotación que devuelva
          otro carril, o capacidades más anchas, es un ensanche de privilegio
          por la puerta de atrás: `_check_session` corre otra vez y con la
          MISMA constante.
        · **La sesión vieja no se cierra.** `Journal.refresh_session` revoca la
          padre en la misma transacción que emite la hija; cerrarla sería cerrar
          lo ya revocado y contar una fuga inexistente.

        🩸 Y EL PROYECTOR SE RECONSTRUYE CON LA HIJA. Rotar `_session` y dejar
        `_projector` en su sitio NO renueva nada: `MarkdownProjector` guarda el
        TOKEN al construirse, así que el ciclo siguiente seguiría presentando el
        de la sesión PADRE —que la rotación acaba de revocar— y el runner se
        moriría igual, sólo que ahora detrás de una renovación que el snapshot
        daba por buena. La rotación publica el par (sesión, proyector) JUNTO y
        bajo el mismo cerrojo: dos mitades del mismo hecho no pueden vivir en
        dos instantes.

        🔑 DESDE QUE `refresh_worker_session` VUELVE, LA PADRE ESTÁ MUERTA. Por
        eso la hija se ADOPTA incluso cuando la validación o el proyector la
        rechazan: si no se adoptase, `_session` seguiría apuntando a la padre
        revocada, el cierre final cerraría un cadáver y la hija —viva, emitida
        a nuestro nombre— quedaría suelta hasta su TTL. Se adopta primero y se
        decide después.

        🩸 EL TTL PEDIDO ES EXPLÍCITO, no una esperanza sobre lo que el backend
        decida solo: se pide `ceil(lease_s + renew_margin_s) + 1`, que es
        justo la holgura que el ciclo que sigue necesita más un segundo de
        colchón contra el redondeo. Y la respuesta se REVALIDA con el reloj
        RELEÍDO después de `projector_for`: pedir y validar contra el reloj de
        ANTES del round-trip deja pasar una hija cuyo TTL ya se comió el
        propio tiempo que tardó en llegar. Sólo entonces vale la pena;
        `KeyboardInterrupt`/`SystemExit` durante cualquiera de las tres
        llamadas al backend NO se convierten en `SESSION_REFRESH_FAILED` ni en
        `BACKEND_CONTRACT_VIOLATED`: se dejan subir para que `_run()` los lea
        como lo que son, una interrupción del propio runner.
        """
        with self._cond:
            session = self._session
        if session is None:
            return None
        expires_at = getattr(session, "expires_at", None)
        if type(expires_at) not in (int, float) or expires_at != expires_at:
            return FATAL_SESSION_INVALID
        now = self._clock()
        if now >= float(expires_at):
            return FATAL_SESSION_EXPIRED
        # El ciclo que sigue puede adquirir autoridad por `_lease_s` segundos.
        # Estar fuera del margen pero no tener TTL para lease+margen era un
        # hueco: el runner reclamaba trabajo con una sesión que podía vencer
        # antes que la autoridad que acababa de pedir.
        if now + self._lease_s + self._renew_margin_s < float(expires_at):
            return None                      # todavía no toca
        ttl_solicitado = math.ceil(self._lease_s + self._renew_margin_s) + 1
        try:
            renewed = self._backend.refresh_worker_session(
                session, ttl_s=ttl_solicitado)
        except Exception:
            # NO `BaseException`: un Ctrl-C aquí es una interrupción del
            # runner, no un fallo de renovación, y tiene que seguir subiendo.
            return FATAL_SESSION_REFRESH_FAILED
        # ADOPCIÓN INCONDICIONAL: la padre ya está revocada, así que la hija es
        # la única sesión viva a nuestro nombre y tiene que ser la que se cierre
        # pase lo que pase debajo.
        self._adopt_session(renewed)
        try:
            self._check_session(renewed)
        except _StartRefusal as refusal:
            return refusal.code
        except Exception:
            return FATAL_SESSION_REFRESH_FAILED
        if float(getattr(renewed, "expires_at")) <= float(expires_at):
            # Una renovación que no mueve el vencimiento no renueva: deja el
            # mismo acantilado un ciclo más tarde y lo llama éxito.
            return FATAL_SESSION_REFRESH_FAILED
        try:
            renewed_projector = self._backend.projector_for(renewed)
        except Exception:
            return FATAL_BACKEND_CONTRACT
        if (renewed_projector is None
                or not hasattr(renewed_projector, "project_next")):
            return FATAL_BACKEND_CONTRACT
        # RELOJ RELEÍDO, AQUÍ Y NO ANTES. El round-trip de renovar Y construir
        # el proyector puede tardar; validar contra `now` -leído antes de las
        # tres llamadas al backend- deja pasar una hija que, para cuando el
        # ciclo siguiente la use, ya no cubre ni su propio lease+margen.
        now_tras_renovar = self._clock()
        if (float(getattr(renewed, "expires_at"))
                <= now_tras_renovar + self._lease_s + self._renew_margin_s):
            return FATAL_SESSION_REFRESH_FAILED
        with self._cond:
            # EL PAR, JUNTO. Publicar la sesión sin su proyector deja un ciclo
            # entero proyectando con el token de la padre revocada.
            self._session = renewed
            self._projector = renewed_projector
            self._cond.notify_all()
        return None

    def _measure(self) -> tuple[OutboxCounts | None, bool]:
        """Devuelve ``(medida, contrato_roto)``.

        Una excepción de la SONDA -``outbox_counts()``- es «no lo sé» y no
        para el runner. A partir de ahí, ``lane``/``pending``/``failed`` se
        leen UNA SOLA VEZ cada uno -nunca se reenvía el objeto del backend,
        para que nadie vuelva a invocar sus getters después- y con esa
        lectura única se construye un ``OutboxCounts`` LOCAL e INMUTABLE: es
        esa copia congelada, no el objeto vivo del backend, la que el resto
        del ciclo lee.

        Un getter que levanta, o unos valores que no conforman el contrato -o
        que lo conforman pero de OTRO carril-, SÍ son terminales: eso no es
        «no lo sé», es el backend rompiendo su contrato o contando la cola de
        un vecino. Sólo se atrapa ``Exception``: una ``BaseException`` -una
        interrupción real, incluso disfrazada de getter- sube para que
        ``_run()`` la lea como lo que es.
        """
        with self._cond:
            session = self._session
        if session is None:
            return None, False
        try:
            counts = self._backend.outbox_counts(session)
        except Exception:
            return None, False
        try:
            lane = counts.lane
            pending = counts.pending
            failed = counts.failed
        except Exception:
            return None, True
        try:
            medida = OutboxCounts(lane=lane, pending=pending, failed=failed)
        except Exception:
            return None, True
        if medida.lane != self._lane:
            return None, True
        return medida, False

    # -- parada -----------------------------------------------------------
    def drain(self, *, timeout_s: float) -> RunnerSnapshot:
        """NIEGA. Este runner no puede drenar el outbox, y decirlo es el punto.

        🩸 ESTE MÉTODO HACÍA `quiesce()` Y SE LLAMABA `drain()`. Cerraba la
        admisión LOCAL —dejaba de reclamar— y esperaba al ÚNICO ciclo en vuelo.
        Eso aquieta al trabajador; no vacía la cola. `pending` podía quedar en
        400 y el método volvía diciendo «drenado», que es la clase de nombre que
        hace daño: un lector que ve `drain()` sin excepción concluye que no queda
        trabajo, y decide un rollback sobre esa conclusión.

        Drenar de verdad exige que NADIE MÁS pueda escribir en el outbox
        mientras se vacía, y esa barrera es externa a este runner
        (``ACTIVE_MODE_PRECONDITION``). Sin ella, «pending == 0» es una
        fotografía: entre la medida y el rollback cabe una escritura nueva. Por
        eso este método no devuelve un snapshot optimista: **levanta**, igual
        que ``certify_rollback``, y por la misma razón.

        Lo que sí se puede hacer hoy es ``quiesce()``, que promete exactamente
        lo que cumple.
        """
        raise RunnerDrainNotSupported(
            "este runner aquieta pero NO drena: vaciar el outbox exige "
            f"{ACTIVE_MODE_PRECONDITION}. Usa `quiesce()`, que sólo promete "
            "cerrar la admisión local y esperar al ciclo en vuelo, y no "
            "confundas su `pending` con un certificado")

    def quiesce(self, *, timeout_s: float) -> RunnerSnapshot:
        """Cierra la admisión LOCAL y espera al ciclo en vuelo. Nada más.

        NO vacía el outbox y NO certifica nada: ``outbox_certifiable`` sigue
        siendo ``False`` y ``pending`` es una MEDIDA, no una promesa. No mata la
        hebra: deja el runner en ``draining`` para que el llamante pueda leer el
        snapshot antes de ``stop()``. Si vence el plazo devuelve el snapshot
        REAL —con ``in_flight`` a true— en vez de mentir.

        🩸 PERSISTE EN `new` Y EN `starting`. `open_worker_session()` corre
        FUERA del cerrojo, así que este método puede llegar antes de que
        exista sesión, o mientras `start()` sigue esperando al backend. Antes
        sólo bajaba `_accepting` LOCALMENTE: si `start()` resolvía después,
        volvía a poner `accepting=True` por encima del cierre que aquí se
        acababa de pedir, y la quiescencia se perdía sin que nadie se
        enterara. `_quiesce_requested` es la MISMA solución que ya tiene
        `stop()` con `_stop_requested`, y `start()` la re-lee bajo el mismo
        cerrojo con el que publica la sesión: quien llega último decide.

        Durante `starting` se espera -acotado por `timeout_s`, un único
        deadline- a que resuelva; si se agota el plazo, este método se rinde
        HONESTAMENTE (como `stop()`) y devuelve el snapshot real, todavía en
        `starting`: la intención ya quedó grabada y `start()` la aplicará
        cuando por fin termine, aunque quien llamó no se quede a verlo. Una
        vez resuelto -o si ya estaba corriendo- se espera al ciclo en vuelo
        con lo que reste del MISMO deadline.

        🩸 SE ESPERA A `_cycle_active`, NO SÓLO A `in_flight`. `_in_flight`
        sólo se enciende YA DENTRO de `project_next`; la renovación previa
        (`_renovar_si_toca`, fuera del cerrojo, puede bloquear en una llamada
        de red real) no lo tocaba, así que este método volvía diciendo «nada
        en vuelo» con el ciclo todavía renovando la sesión. `_cycle_active`
        cubre esa ventana entera -desde antes de pedir la renovación hasta
        justo antes de cualquier `wait()` del bucle-, así que esperarlo es
        esperar al ciclo REAL, no a la mitad de él.
        """
        if type(timeout_s) not in (int, float) or timeout_s != timeout_s or timeout_s < 0:
            raise RunnerConfigurationError("timeout_s debe ser un número >= 0")
        deadline = time.monotonic() + timeout_s
        with self._cond:
            self._accepting = False
            self._quiesce_requested = True
            self._cond.notify_all()
            if self._state in (RunnerState.NEW, RunnerState.STOPPED,
                               RunnerState.FATAL):
                return self._snapshot_locked()
            if self._state is RunnerState.STARTING:
                while self._state is RunnerState.STARTING:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return self._snapshot_locked()
                    self._cond.wait(remaining)
                if self._state in (RunnerState.STOPPED, RunnerState.FATAL):
                    return self._snapshot_locked()
            # El estado lo fija QUIEN cierra la admisión. Dejarlo en manos del
            # ciclo publicaba `projecting` durante todo el trabajo en vuelo: una
            # lectura cierta que oculta que la puerta ya está cerrada.
            self._state = RunnerState.DRAINING
            while self._in_flight or self._cycle_active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            return self._snapshot_locked()

    def stop(self) -> None:
        """Total, idempotente y REINTENTABLE: vale desde cualquier estado.

        Un `stop()` cuyo cierre falló deja `session_open=True` y
        `close_failures` incrementado, propaga el fallo, y el SIGUIENTE `stop()`
        vuelve a intentar cerrar. Antes no: la bandera se ponía por adelantado y
        el reintento era imposible.

        🩸 UN SOLO DEADLINE PARA TODA LA LLAMADA. Antes la espera a que
        `starting` resolviera tenía su propio plazo de `stop_timeout_s` y el
        `join()` de más abajo pedía OTRO `stop_timeout_s` completo y nuevo: en
        el peor caso el presupuesto real de `stop()` era el DOBLE del
        configurado. El `deadline` se calcula UNA vez, al principio, y tanto
        la espera a `starting` como el `join()` gastan del mismo reloj.

        🩸 Y SI EL PLAZO SE AGOTA MIENTRAS `starting` SIGUE ABIERTO, SE
        LEVANTA. Antes esto volvía en silencio -`thread` seguía siendo `None`,
        así que el `if thread is not None` de más abajo nunca disparaba- y
        quien llamó a `stop()` se iba creyendo que había parado algo. La
        intención queda grabada igual (`_stop_requested` ya es `True`) y será
        `start()` quien cierre cuando la apertura por fin resuelva, pero quien
        llamó a `stop()` tiene que SABER que no cerró nada todavía.
        """
        with self._cond:
            if self._state is RunnerState.NEW:
                # Nunca arrancó: no hay sesión que cerrar ni hebra que unir. Se
                # deja la intención escrita igual, para que un `start()` que
                # corriera después la vea y no abra nada a espaldas del stop.
                self._stop_requested = True
                self._state = RunnerState.STOPPED
                return
            self._stop_requested = True
            self._accepting = False
            self._cond.notify_all()
            deadline = time.monotonic() + self._stop_timeout_s
            # ESPERA A QUE `starting` RESUELVA. Sin esto, un `stop()` que llega
            # mientras `start()` está dentro de `open_worker_session()` volvía
            # ANTES de que la sesión existiera: no había nada que cerrar, y la
            # sesión nacía huérfana medio milisegundo después. Ahora se espera a
            # que `start()` la publique; después el cierre tiene sujeto.
            if self._state is RunnerState.STARTING:
                while self._state is RunnerState.STARTING:
                    restante = deadline - time.monotonic()
                    if restante <= 0:
                        break
                    self._cond.wait(restante)
                if self._state is RunnerState.STARTING:
                    raise RunnerStopTimeout(
                        f"la sesión seguía abriéndose tras "
                        f"{self._stop_timeout_s}s: stop() se agotó esperando "
                        "a que arrancar terminara; la intención queda "
                        "grabada y start() cerrará cuando resuelva")
            thread = self._thread
        if thread is not None and thread.is_alive():
            restante = max(0.0, deadline - time.monotonic())
            thread.join(restante)
            if thread.is_alive():
                # La sesión NO se cierra debajo de un worker vivo: cerrarla
                # dejaría un `project_next` en vuelo sin autoridad y el fallo
                # aparecería como un rechazo del núcleo, lejos de su causa.
                raise RunnerStopTimeout(
                    f"la hebra del projector siguió viva tras "
                    f"{self._stop_timeout_s}s: la sesión queda abierta")
        with self._cond:
            if self._state is not RunnerState.FATAL:
                self._state = RunnerState.STOPPED
            self._cond.notify_all()
        # FUERA DEL CERROJO, SIEMPRE: orden `_close_lock` -> `_cond`.
        cleanup = self._cerrar_sesion_una_vez()
        if cleanup is not None:
            raise cleanup

    def certify_rollback(self) -> RunnerSnapshot:
        """Niega SIEMPRE, drenado incluido, y dice qué falta para poder firmar."""
        raise RollbackNotCertifiable(
            f"el runner activo no certifica rollback: falta {ACTIVE_MODE_PRECONDITION}")


def projector_runner_for_mode(
        mode: object, *, backend: ProjectionBackend | None = None,
        lane: str | None = None) -> ProjectorRunnerLike:
    """Selector cerrado del runner una vez disponible la barrera de admisión."""
    if mode == "disabled" and isinstance(mode, str):
        if backend is not None or lane is not None:
            raise RunnerConfigurationError(
                "projector disabled no acepta backend ni carril")
        return DisabledProjectorRunner()
    if mode == "active" and isinstance(mode, str):
        if backend is None or lane is None:
            raise RunnerConfigurationError(
                "projector active exige backend y carril explícitos")
        return ActiveProjectorRunner(backend, lane=lane)
    raise RunnerConfigurationError(
        "el modo del projector debe ser active o disabled")
