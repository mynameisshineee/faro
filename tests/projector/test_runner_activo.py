"""Falsadores focales del `ActiveProjectorRunner`.

Sólo stdlib + pytest, como el resto de `tests/projector`. El backend es falso a
propósito: lo que aquí se prueba es el CICLO DE VIDA del runner, no la
proyección —que ya tiene su suite en `test_projector.py`—. Para que ese aislamiento
no se convierta en un universo privado, `test_el_protocolo_declarado_coincide_*`
compara el Protocol declarado contra las firmas REALES de `projector.py` y
`coordination.py`.
"""
from __future__ import annotations

import inspect
import math
import threading
import time
from dataclasses import dataclass, field

import pytest

import projector_runner as R


LANE = "llminbox"


def wait_until(predicate, *, timeout=3.0, interval=0.005):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


# Muy lejos: los tests que no van de renovación no deben rozarla nunca.
LEJOS = 1e12


class FakeClock:
    """Reloj DETERMINISTA. La renovación no se falsa con `sleep`."""

    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


@dataclass
class FakeSession:
    token: str = "worker-token"
    lane: str = LANE
    capabilities: tuple = (R.CAP_OUTBOX_WORKER,)
    expires_at: float = LEJOS


@dataclass(frozen=True)
class ForeignOutboxCounts:
    """Simula el DTO CANÓNICO de `coordination.py`: misma forma exacta que
    `R.OutboxCounts` -``lane``/``pending``/``failed`` con los mismos tipos-,
    IDENTIDAD DE CLASE DISTINTA a propósito. Antes del fix, un `isinstance`
    nominal contra la clase local de `projector_runner.py` rechazaba esta
    medida -real, bien tipada, del carril correcto- exactamente como si el
    backend hubiera roto el contrato.
    """
    lane: str
    pending: int
    failed: int


class FakeProjector:
    """Igual que `MarkdownProjector`, SE QUEDA CON EL TOKEN al construirse.

    Ésa es la propiedad que hace falsable la rotación: si el runner sustituye la
    sesión pero no el proyector, el token que llega a `project_next` sigue
    siendo el de la sesión padre, ya revocada.
    """

    def __init__(self, backend, session):
        self._backend = backend
        self._token = getattr(session, "token", None)

    def project_next(self, *, lease_s: int = 60):
        return self._backend._project_next(lease_s, self._token)


@dataclass
class FakeBackend:
    """Backend guionizado. Registra TODO acto para que la ausencia sea medible."""

    session: FakeSession = field(default_factory=FakeSession)
    outcomes: list = field(default_factory=list)
    counts: object = None
    counts_script: list = field(default_factory=list)
    gate: threading.Event | None = None
    open_error: BaseException | None = None
    close_error: BaseException | None = None
    close_error_once: bool = False
    refresh_error: BaseException | None = None
    refreshed: object = None
    projector_error: BaseException | None = None
    projector_override: object = "__default__"
    # Reloj COMPARTIDO con el runner (una `FakeClock`), para simular que el
    # round-trip de renovar y/o construir el proyector consume tiempo REAL sin
    # depender de un `sleep`. `refresh_delay_s`/`projector_delay_s` avanzan
    # ese reloj en el momento exacto de cada llamada.
    delay_clock: object = None
    refresh_delay_s: float = 0.0
    projector_delay_s: float = 0.0

    def __post_init__(self):
        self.calls: list[str] = []
        self.leases: list[int] = []
        self.closes = 0
        self.refreshes = 0
        self.closed_tokens: list[str] = []
        self.projector_tokens: list[str] = []
        self.tokens_usados: list[str] = []
        self.requested_ttls: list[int] = []
        # Compuertas para falsar la carrera `stop()` durante `starting` sin
        # depender de un `sleep` que la haga temporal y frágil.
        self.open_entered = threading.Event()
        self.open_gate: threading.Event | None = None
        # LA MISMA IDEA para falsar la carrera `quiesce()`/renovación: bloquea
        # `refresh_worker_session` a voluntad y da a quien la falsa un evento
        # para saber que YA está bloqueada ahí dentro, sin `sleep`.
        self.refresh_entered = threading.Event()
        self.refresh_gate: threading.Event | None = None
        self.exhausted = threading.Event()
        self._lock = threading.Lock()

    # -- Protocol -------------------------------------------------------
    def open_worker_session(self):
        self.calls.append("open_worker_session")
        self.open_entered.set()
        if self.open_gate is not None:
            self.open_gate.wait(5)
        if self.open_error is not None:
            raise self.open_error
        return self.session

    def projector_for(self, session):
        self.calls.append("projector_for")
        self.projector_tokens.append(getattr(session, "token", None))
        # SÓLO tras una renovación: `start()` YA llama a `projector_for` de
        # forma síncrona (`projector_runner.py`, antes de lanzar la hebra) para
        # construir el proyector inicial. Si el retardo se aplicase en CUALQUIER
        # llamada, esa primera —anterior a que exista renovación que probar—
        # adelantaría el reloj compartido antes de que el bucle llegase a
        # `_renovar_si_toca()`, y el escenario que esto simula (el round-trip
        # DE LA renovación) nunca se ejercitaría: la sesión moriría por
        # `SESSION_EXPIRED` antes de intentar renovar nada.
        if (self.delay_clock is not None and self.projector_delay_s
                and self.refreshes > 0):
            self.delay_clock.now += self.projector_delay_s
        if self.projector_error is not None:
            raise self.projector_error
        if self.projector_override != "__default__":
            return self.projector_override
        return FakeProjector(self, session)

    def outbox_counts(self, session):
        self.calls.append("outbox_counts")
        with self._lock:
            value = self.counts_script.pop(0) if self.counts_script else self.counts
        if isinstance(value, BaseException):
            raise value
        return value

    def close_worker_session(self, session):
        self.calls.append("close_worker_session")
        self.closes += 1
        self.closed_tokens.append(getattr(session, "token", None))
        if self.close_error is not None:
            error = self.close_error
            if self.close_error_once:
                self.close_error = None      # el reintento SÍ puede cerrar
            raise error

    def refresh_worker_session(self, session, *, ttl_s):
        self.calls.append("refresh_worker_session")
        self.refreshes += 1
        self.requested_ttls.append(ttl_s)
        self.refresh_entered.set()
        if self.refresh_gate is not None:
            self.refresh_gate.wait(5)
        if self.delay_clock is not None and self.refresh_delay_s:
            self.delay_clock.now += self.refresh_delay_s
        if self.refresh_error is not None:
            raise self.refresh_error
        if self.refreshed is not None:
            return self.refreshed
        return FakeSession(token=f"worker-token-{self.refreshes}",
                           expires_at=session.expires_at + 1000.0)

    # -- guion ----------------------------------------------------------
    def _project_next(self, lease_s, token=None):
        self.leases.append(lease_s)
        self.tokens_usados.append(token)
        if self.gate is not None:
            self.gate.wait(5)
        with self._lock:
            if not self.outcomes:
                self.exhausted.set()
                return None
            value = self.outcomes.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def runner(backend, **kwargs):
    kwargs.setdefault("lane", LANE)
    kwargs.setdefault("idle_poll_s", 0.01)
    kwargs.setdefault("failure_backoff_s", 0.01)
    # `lease_s=1` y `stop_timeout_s=2.0` cumplen la relación que el constructor
    # exige (stop >= lease). Con el default de fábrica (`lease_s=60`) un
    # `stop_timeout_s` de 2 s ya NO se puede construir, y eso es deliberado.
    kwargs.setdefault("lease_s", 1)
    kwargs.setdefault("stop_timeout_s", 2.0)
    return R.ActiveProjectorRunner(backend, **kwargs)


def projector_threads():
    return [t.name for t in threading.enumerate() if t.name.startswith("projector-")]


# ── FA-1 · constructor puro ───────────────────────────────────────────────
def test_el_constructor_no_llama_al_backend_ni_crea_hebra():
    backend = FakeBackend(counts=R.OutboxCounts(LANE, 0, 0))
    before = projector_threads()
    active = runner(backend)
    assert backend.calls == []
    assert projector_threads() == before
    snapshot = active.snapshot()
    assert snapshot.state is R.RunnerState.NEW
    assert snapshot.thread_alive is False and snapshot.accepting_claims is False
    # ⊕ control: el espía SÍ registra actos — el [] de arriba es ausencia real,
    # no un backend que no sabe registrar.
    active.start()
    try:
        assert backend.calls[:2] == ["open_worker_session", "projector_for"]
    finally:
        active.stop()


def test_el_constructor_valida_sus_argumentos_sin_efectos():
    backend = FakeBackend(counts=R.OutboxCounts(LANE, 0, 0))
    for kwargs in ({"lane": ""}, {"lease_s": 0}, {"idle_poll_s": 0},
                   {"max_consecutive_failures": 0}, {"stop_timeout_s": -1},
                   {"renew_margin_s": 0}, {"clock": "no invocable"}):
        with pytest.raises(R.RunnerConfigurationError):
            runner(backend, **kwargs)
    assert backend.calls == []


# ── FA-2/3 · mínimo privilegio y un solo carril ───────────────────────────
@pytest.mark.parametrize("capabilities, code", [
    ((R.CAP_OUTBOX_WORKER, "outbox_operator"),
     R.FATAL_CAPABILITIES_NOT_LEAST_PRIVILEGE),
    ((), R.FATAL_CAPABILITIES_NOT_LEAST_PRIVILEGE),
    (("indexer",), R.FATAL_CAPABILITIES_NOT_LEAST_PRIVILEGE),
])
def test_una_sesion_que_no_es_de_minimo_privilegio_no_arranca(capabilities, code):
    backend = FakeBackend(session=FakeSession(capabilities=capabilities),
                          counts=R.OutboxCounts(LANE, 0, 0))
    active = runner(backend)
    with pytest.raises(R.RunnerConfigurationError):
        active.start()
    snapshot = active.snapshot()
    assert snapshot.state is R.RunnerState.FATAL and snapshot.fatal_code == code
    assert snapshot.thread_alive is False and projector_threads() == []
    # La sesión autorizada NO se queda abierta detrás de un arranque rechazado.
    assert backend.closes == 1


def test_la_sesion_de_minimo_privilegio_exacto_si_arranca():
    """⊕ control de la pareja anterior: si esto no pasara, el rechazo de arriba
    podría estar midiendo «nada arranca nunca»."""
    backend = FakeBackend(counts=R.OutboxCounts(LANE, 0, 0))
    active = runner(backend)
    active.start()
    try:
        assert wait_until(lambda: active.snapshot().state is R.RunnerState.IDLE)
        assert active.snapshot().accepting_claims is True
    finally:
        active.stop()
    assert backend.closes == 1


def test_una_sesion_de_otro_carril_no_arranca():
    backend = FakeBackend(session=FakeSession(lane="64bis"),
                          counts=R.OutboxCounts(LANE, 0, 0))
    active = runner(backend)
    with pytest.raises(R.RunnerConfigurationError):
        active.start()
    snapshot = active.snapshot()
    assert snapshot.fatal_code == R.FATAL_LANE_MISMATCH
    assert backend.closes == 1


def test_un_fallo_al_abrir_sesion_no_inventa_una_sesion_que_cerrar():
    backend = FakeBackend(open_error=RuntimeError("núcleo caído"),
                          counts=R.OutboxCounts(LANE, 0, 0))
    active = runner(backend)
    with pytest.raises(RuntimeError):
        active.start()
    assert active.snapshot().fatal_code == R.FATAL_SESSION_OPEN_FAILED
    assert backend.closes == 0


# ── FA-4 · el ciclo es project_next, con el lease configurado ─────────────
def test_el_ciclo_consume_el_outbox_con_el_lease_configurado():
    backend = FakeBackend(outcomes=["evt-1", "evt-2", "evt-3"],
                          counts=R.OutboxCounts(LANE, 0, 0))
    active = runner(backend, lease_s=7, stop_timeout_s=8.0)
    active.start()
    try:
        assert backend.exhausted.wait(3)
        assert wait_until(lambda: active.snapshot().state is R.RunnerState.IDLE)
    finally:
        active.stop()
    assert len(backend.leases) >= 4
    # ⊖ del lease: con `lease_s` cableado a su default (60) esto cae.
    assert set(backend.leases) == {7}


# ── FA-5 · contadores medidos, nunca un cero inventado ────────────────────
def test_los_contadores_publicados_son_los_medidos():
    backend = FakeBackend(counts=R.OutboxCounts(LANE, 5, 2))
    active = runner(backend)
    active.start()
    try:
        assert wait_until(lambda: active.snapshot().pending == 5)
        assert active.snapshot().failed == 2
    finally:
        active.stop()


def test_una_sonda_que_falla_deja_los_contadores_en_none_y_no_en_cero():
    backend = FakeBackend(counts=RuntimeError("sonda caída"))
    active = runner(backend)
    assert active.snapshot().pending is None
    active.start()
    try:
        assert wait_until(lambda: "outbox_counts" in backend.calls)
        snapshot = active.snapshot()
        assert snapshot.pending is None and snapshot.failed is None
        # No es terminal: no saber cuánto queda no es lo mismo que estar roto.
        assert snapshot.state in (R.RunnerState.IDLE, R.RunnerState.PROJECTING)
    finally:
        active.stop()


def test_una_sonda_que_rompe_el_contrato_si_es_terminal():
    """Devolver `(0, 0)` en vez de `OutboxCounts` es el cero inventado con otra
    forma: no se sanea, se para."""
    backend = FakeBackend(counts=(0, 0))
    active = runner(backend)
    active.start()
    try:
        assert wait_until(lambda: active.snapshot().state is R.RunnerState.FATAL)
        assert active.snapshot().fatal_code == R.FATAL_BACKEND_CONTRACT
    finally:
        active.stop()


def test_un_carril_equivocado_en_los_contadores_rompe_el_contrato():
    """🩸 `OutboxCounts` ahora declara el carril que certifica. Un backend que
    devuelve la medida de OTRO carril -mal cableado, o compartiendo proceso
    entre runners de carriles distintos- rompe el contrato exactamente igual
    que un tipo equivocado, y por el mismo motivo: ninguno de los dos es «no
    lo sé», los dos son el backend contando algo que no es lo que se le pidió.
    """
    backend = FakeBackend(counts=R.OutboxCounts("otro-carril", 0, 0))
    active = runner(backend)
    active.start()
    try:
        assert wait_until(lambda: active.snapshot().state is R.RunnerState.FATAL)
        assert active.snapshot().fatal_code == R.FATAL_BACKEND_CONTRACT
    finally:
        active.stop()


def test_un_dto_foreign_con_la_forma_canonica_se_acepta_sin_isinstance_nominal():
    """🩸 EL CONTRATO ES ESTRUCTURAL, NO NOMINAL. Un backend de producción
    construye su propio DTO -el canónico de `coordination.py`, no el de este
    módulo- y antes un `isinstance(counts, R.OutboxCounts)` lo rechazaba por
    no ser LITERALMENTE la misma clase, aunque `ForeignOutboxCounts` declare
    el mismo `lane`/`pending`/`failed` con los mismos tipos exactos. Eso
    convertía una medida BUENA en `BACKEND_CONTRACT_VIOLATED`.
    """
    backend = FakeBackend(counts=ForeignOutboxCounts(LANE, 3, 1))
    active = runner(backend)
    active.start()
    try:
        assert wait_until(lambda: active.snapshot().pending == 3)
        snapshot = active.snapshot()
        assert snapshot.state is not R.RunnerState.FATAL
        assert snapshot.pending == 3 and snapshot.failed == 1
    finally:
        active.stop()
    assert active.snapshot().state is R.RunnerState.STOPPED


def test_un_dto_foreign_de_otro_carril_sigue_rompiendo_el_contrato():
    """⊕ del anterior: la validación estructural NO relaja el chequeo de
    carril. Un `ForeignOutboxCounts` con forma perfecta pero carril ajeno
    tiene que morir igual que `R.OutboxCounts("otro-carril", ...)`."""
    backend = FakeBackend(counts=ForeignOutboxCounts("otro-carril", 0, 0))
    active = runner(backend)
    active.start()
    try:
        assert wait_until(lambda: active.snapshot().state is R.RunnerState.FATAL)
        assert active.snapshot().fatal_code == R.FATAL_BACKEND_CONTRACT
    finally:
        active.stop()


@pytest.mark.parametrize("lane,pending,failed", [
    (True, 0, 0),
    (False, 0, 0),
    ("", 0, 0),
    (LANE, True, 0),
    (LANE, 0, True),
    (LANE, -1, 0),
])
def test_outbox_counts_rechaza_bool_lane_vacio_y_negativos_en_construccion(
        lane, pending, failed):
    """`bool` ES `int` para `isinstance`, pero `type(True) is not int`: por
    eso `True`/`False` se rechazan en cualquiera de los tres campos, y un
    carril vacío no es un carril."""
    with pytest.raises(ValueError):
        R.OutboxCounts(lane, pending, failed)


# ── FA-6 · quiesce dice la verdad (y NO dice que haya drenado) ───────────
def test_quiesce_no_miente_cuando_no_pudo_aquietar_y_no_claima_despues():
    gate = threading.Event()
    backend = FakeBackend(outcomes=["evt-1"], counts=R.OutboxCounts(LANE, 0, 0),
                          gate=gate)
    active = runner(backend)
    active.start()
    try:
        assert wait_until(lambda: active.snapshot().in_flight)
        vencido = active.quiesce(timeout_s=0.05)
        # ⊖ de la clase: un drain que sólo baja una bandera devolvería
        # `in_flight=False` con el trabajo todavía dentro de project_next.
        assert vencido.in_flight is True
        assert vencido.accepting_claims is False
        assert vencido.state is R.RunnerState.DRAINING
        gate.set()
        drenado = active.quiesce(timeout_s=3)
        assert drenado.in_flight is False and drenado.thread_alive is True
        assert drenado.state is R.RunnerState.DRAINING
        llamadas = len(backend.leases)
        time.sleep(0.1)
        assert len(backend.leases) == llamadas, "siguió claimando tras drenar"
    finally:
        gate.set()
        active.stop()


def test_quiesce_sobre_un_runner_sin_arrancar_no_inventa_actividad():
    backend = FakeBackend(counts=R.OutboxCounts(LANE, 0, 0))
    snapshot = runner(backend).quiesce(timeout_s=0)
    assert snapshot.state is R.RunnerState.NEW
    assert snapshot.thread_alive is False and snapshot.in_flight is False
    assert snapshot.pending is None and backend.calls == []


def test_quiesce_persiste_en_new_y_start_no_reabre_accepting():
    """🩸 PERSISTE EN `new`. Antes `quiesce()` sobre un runner sin arrancar
    devolvía el snapshot inerte y NO grababa nada: cuando `start()` llegaba
    después, abría la sesión y volvía a poner `accepting=True` por encima del
    cierre que ya se había pedido, y la intención se perdía sin que nadie se
    enterara. `_quiesce_requested` sobrevive a la llamada, y `start()` lo
    re-lee bajo el mismo cerrojo con el que publica la sesión.
    """
    backend = FakeBackend(counts=R.OutboxCounts(LANE, 0, 0))
    active = runner(backend)
    inerte = active.quiesce(timeout_s=0)
    assert inerte.state is R.RunnerState.NEW
    assert backend.calls == []

    active.start()
    try:
        assert wait_until(lambda: active.snapshot().state is R.RunnerState.DRAINING)
        snapshot = active.snapshot()
        assert snapshot.accepting_claims is False
        assert snapshot.thread_alive is False and projector_threads() == []
        assert snapshot.in_flight is False
        # `start()` SÍ hizo su trabajo -abrió la sesión- pero jamás lanzó un
        # ciclo de proyección: la quiescencia pedida en `new` se respetó.
        assert "open_worker_session" in backend.calls
        assert backend.leases == []
        assert snapshot.session_open is True
    finally:
        active.stop()
    assert active.snapshot().session_open is False


def test_quiesce_durante_starting_persistente_se_rinde_honesto_y_start_lo_respeta():
    """🩸 PERSISTE EN `starting`. `open_worker_session()` corre FUERA del
    cerrojo: si `quiesce()` llega en esa ventana y sólo bajaba `accepting`
    LOCALMENTE, `start()` -al resolver- volvía a ponerlo en `True` por
    encima, el mismo defecto que tenía `stop()` antes de re-leer su intención
    bajo el cerrojo que publica la sesión.
    """
    gate = threading.Event()
    backend = FakeBackend(counts=R.OutboxCounts(LANE, 0, 0))
    backend.open_gate = gate
    active = runner(backend, stop_timeout_s=2.0)

    fallos: list[BaseException] = []

    def _arranca():
        try:
            active.start()
        except BaseException as exc:      # pragma: no cover - se afirma abajo
            fallos.append(exc)

    hebra = threading.Thread(target=_arranca, name="t-start-lento")
    hebra.start()
    assert backend.open_entered.wait(3)
    assert active.snapshot().state is R.RunnerState.STARTING

    # `quiesce()` se rinde HONESTAMENTE: la apertura sigue bloqueada tras su
    # propio plazo, igual que `stop()` en el mismo escenario.
    vencido = active.quiesce(timeout_s=0.05)
    assert vencido.state is R.RunnerState.STARTING
    assert vencido.accepting_claims is False

    gate.set()
    hebra.join(5)
    assert not hebra.is_alive() and fallos == []

    # `start()` RESOLVIÓ Y RESPETÓ la quiescencia pedida durante `starting`:
    # nunca reabrió `accepting` ni lanzó la hebra de proyección.
    assert wait_until(lambda: active.snapshot().state is R.RunnerState.DRAINING)
    snapshot = active.snapshot()
    assert snapshot.accepting_claims is False
    assert snapshot.thread_alive is False and projector_threads() == []
    assert backend.leases == []
    active.stop()


def test_quiesce_durante_starting_que_resuelve_dentro_del_plazo_devuelve_draining():
    """El ⊕ del anterior: si `starting` resuelve DENTRO del `timeout_s` de
    `quiesce()`, la espera lo capta y devuelve el snapshot YA resuelto -no el
    `starting` obsoleto- sin que el llamante tenga que reconsultar."""
    gate = threading.Event()
    backend = FakeBackend(counts=R.OutboxCounts(LANE, 0, 0))
    backend.open_gate = gate
    active = runner(backend)

    def _arranca():
        active.start()

    hebra = threading.Thread(target=_arranca, name="t-start")
    hebra.start()
    assert backend.open_entered.wait(3)
    assert active.snapshot().state is R.RunnerState.STARTING

    threading.Timer(0.05, gate.set).start()
    resuelto = active.quiesce(timeout_s=3)
    assert resuelto.state is R.RunnerState.DRAINING
    assert resuelto.accepting_claims is False
    hebra.join(5)
    active.stop()


def test_quiesce_espera_una_renovacion_bloqueada_y_no_hay_claim_despues():
    """🩸 `quiesce()` esperaba SÓLO `in_flight`, que sólo se enciende YA
    DENTRO de `project_next`. La renovación (`_renovar_si_toca`, fuera del
    cerrojo, puede bloquear en una llamada de red real) no lo tocaba: mientras
    estuviera en curso, `in_flight` seguía en `False` y `quiesce()` volvía
    diciendo «nada en vuelo» con el ciclo todavía trabajando. Se falsa
    bloqueando `refresh_worker_session` en una puerta y probando que
    `quiesce()` NO vuelve mientras la puerta sigue cerrada -y que, tras
    abrirla y volver, no se coló ni un solo claim (`backend.leases`).
    """
    reloj = FakeClock(0.0)
    backend = FakeBackend(
        session=FakeSession(expires_at=100.0),
        counts=R.OutboxCounts(LANE, 0, 0),
    )
    backend.refresh_gate = threading.Event()
    # El margen (40) + lease (60) YA cubre el vencimiento (100) desde
    # `now=0`: la primera vuelta del bucle renueva de inmediato, antes de
    # intentar proyectar nada.
    active = runner(backend, clock=reloj, lease_s=60, renew_margin_s=40.0,
                    stop_timeout_s=90.0)
    active.start()
    try:
        assert backend.refresh_entered.wait(3)

        devuelto: dict = {}
        listo = threading.Event()

        def _quiesce():
            devuelto["snapshot"] = active.quiesce(timeout_s=5)
            listo.set()

        hebra = threading.Thread(target=_quiesce, name="t-quiesce")
        hebra.start()

        # `quiesce()` NO puede haber vuelto todavía: la renovación sigue
        # bloqueada en la puerta, y antes del fix esto fallaba -volvía casi
        # al instante, con `_in_flight` en `False`.
        assert not listo.wait(0.3)
        assert backend.leases == []

        backend.refresh_gate.set()
        assert listo.wait(5)
        hebra.join(5)
        snapshot = devuelto["snapshot"]
        assert snapshot.state is R.RunnerState.DRAINING
        assert snapshot.in_flight is False
        # CERO CLAIMS: ni mientras `quiesce()` esperaba ni DESPUÉS de que
        # devolviera se llegó a proyectar nada -la admisión ya estaba
        # cerrada cuando la renovación por fin resolvió.
        assert backend.leases == []
    finally:
        active.stop()
    assert backend.leases == []


# ── FA-7 · stop total, idempotente y que no cierra debajo del worker ──────
def test_stop_es_idempotente_y_cierra_la_sesion_una_sola_vez():
    backend = FakeBackend(counts=R.OutboxCounts(LANE, 0, 0))
    active = runner(backend)
    active.start()
    active.stop()
    active.stop()
    active.stop()
    assert backend.closes == 1
    assert active.snapshot().state is R.RunnerState.STOPPED
    assert active.snapshot().thread_alive is False


def test_stop_tras_un_arranque_rechazado_no_cierra_dos_veces():
    backend = FakeBackend(session=FakeSession(lane="64bis"),
                          counts=R.OutboxCounts(LANE, 0, 0))
    active = runner(backend)
    with pytest.raises(R.RunnerConfigurationError):
        active.start()
    active.stop()
    assert backend.closes == 1
    # El estado terminal informativo gana: `stop` no lo degrada a `stopped`.
    assert active.snapshot().state is R.RunnerState.FATAL


def test_stop_sin_arrancar_no_abre_ni_cierra_nada():
    backend = FakeBackend(counts=R.OutboxCounts(LANE, 0, 0))
    active = runner(backend)
    active.stop()
    assert backend.calls == [] and backend.closes == 0
    assert active.snapshot().state is R.RunnerState.STOPPED


def test_stop_no_cierra_la_sesion_debajo_de_un_worker_vivo():
    gate = threading.Event()
    backend = FakeBackend(outcomes=["evt-1"], counts=R.OutboxCounts(LANE, 0, 0),
                          gate=gate)
    # `stop_timeout_s` corto PERO >= `lease_s`: la pareja sigue siendo legal y
    # el timeout se provoca con el gate, no con una configuración prohibida.
    active = runner(backend, lease_s=1, stop_timeout_s=1.0)
    active.start()
    try:
        assert wait_until(lambda: active.snapshot().in_flight)
        with pytest.raises(R.RunnerStopTimeout):
            active.stop()
        assert backend.closes == 0
    finally:
        gate.set()
        active.stop()


def test_start_dos_veces_es_un_error_de_ciclo_de_vida():
    backend = FakeBackend(counts=R.OutboxCounts(LANE, 0, 0))
    active = runner(backend)
    active.start()
    try:
        with pytest.raises(R.RunnerLifecycleError):
            active.start()
        assert backend.calls.count("open_worker_session") == 1
    finally:
        active.stop()
    with pytest.raises(R.RunnerLifecycleError):
        active.start()


# ── FA-8 · fallos del ciclo: acotados y terminales ───────────────────────
def test_los_fallos_consecutivos_se_agotan_y_paran_el_runner():
    backend = FakeBackend(
        outcomes=[RuntimeError("boom")] * 10, counts=R.OutboxCounts(LANE, 0, 1))
    active = runner(backend, max_consecutive_failures=3)
    active.start()
    try:
        assert wait_until(lambda: active.snapshot().state is R.RunnerState.FATAL)
        snapshot = active.snapshot()
        assert snapshot.fatal_code == R.FATAL_PROJECTION_CYCLE_FAILED
        assert snapshot.accepting_claims is False and snapshot.in_flight is False
        assert len(backend.leases) == 3
    finally:
        active.stop()


def test_un_fallo_aislado_no_mata_el_runner():
    """⊕ control del anterior: si CUALQUIER fallo fuese terminal, el tope de
    fallos consecutivos no estaría midiendo nada."""
    backend = FakeBackend(outcomes=[RuntimeError("transitorio"), "evt-1"],
                          counts=R.OutboxCounts(LANE, 0, 0))
    active = runner(backend, max_consecutive_failures=3)
    active.start()
    try:
        assert backend.exhausted.wait(3)
        assert wait_until(lambda: active.snapshot().state is R.RunnerState.IDLE)
        assert active.snapshot().fatal_code is None
    finally:
        active.stop()


# ── FA-9 · el rollback NO se certifica, ni drenado ───────────────────────
def test_certify_rollback_niega_incluso_con_el_outbox_drenado_a_cero():
    backend = FakeBackend(counts=R.OutboxCounts(LANE, 0, 0))
    active = runner(backend)
    active.start()
    try:
        assert wait_until(lambda: active.snapshot().pending == 0)
        drenado = active.quiesce(timeout_s=3)
        assert drenado.in_flight is False and drenado.accepting_claims is False
        assert drenado.pending == 0 and drenado.failed == 0
        # Cerrado, drenado y sin fallos — y AUN ASÍ no se firma: falta la
        # barrera de admisión que congele ese cero.
        assert drenado.outbox_certifiable is False
        with pytest.raises(R.RollbackNotCertifiable):
            active.certify_rollback()
    finally:
        active.stop()


# ── FA-10 · active sólo se selecciona con dependencias explícitas ─────────
def test_el_modo_active_exige_backend_y_carril_aunque_la_clase_exista():
    assert hasattr(R, "ActiveProjectorRunner")
    with pytest.raises(R.RunnerConfigurationError) as caught:
        R.projector_runner_for_mode("active")
    assert "backend y carril" in str(caught.value)
    # ⊕ control: el selector no está roto del todo — `disabled` sí resuelve.
    assert isinstance(R.projector_runner_for_mode("disabled"),
                      R.DisabledProjectorRunner)


# ── FA-11 · el aislamiento del test no es un universo privado ────────────
def test_el_protocolo_declarado_coincide_con_el_proyector_y_la_sesion_reales():
    import coordination as C
    import projector as P

    real = inspect.signature(P.MarkdownProjector.project_next)
    declared = inspect.signature(R.ProjectorLike.project_next)
    assert list(real.parameters) == list(declared.parameters) == ["self", "lease_s"]
    assert real.parameters["lease_s"].kind is inspect.Parameter.KEYWORD_ONLY
    assert (real.parameters["lease_s"].default
            == declared.parameters["lease_s"].default == 60)
    # La sesión emitida por el núcleo publica los tres campos que el runner lee.
    assert {"token", "lane", "capabilities", "expires_at"} <= set(
        C.IssuedSession.__dataclass_fields__)
    # La renovación declarada en el Protocol tiene un acto REAL detrás.
    renew = inspect.signature(C.Journal.refresh_session)
    assert "ttl_s" in renew.parameters
    assert C.DEFAULT_SESSION_TTL_S > 0
    # El techo del arriendo es el del núcleo, no uno inventado aquí.
    assert R.MAX_LEASE_S == 3600 and R.MIN_LEASE_S == 1
    if hasattr(C, "MAX_OUTBOX_LEASE_S"):
        assert R.MAX_LEASE_S == C.MAX_OUTBOX_LEASE_S
    # Y la capacidad exigida es literalmente la del núcleo, no una parecida.
    assert R.CAP_OUTBOX_WORKER == C.CAP_OUTBOX_WORKER
    assert R.LEAST_PRIVILEGE_CAPABILITIES == frozenset({C.CAP_OUTBOX_WORKER})
    assert C.CAP_OUTBOX_OPERATOR not in R.LEAST_PRIVILEGE_CAPABILITIES


# ═══════════════════════════════════════════════════════════════════════════
# ENDURECIMIENTO C4 · un falsador determinista por cada carrera y cada fuga
# ═══════════════════════════════════════════════════════════════════════════


# ── FA-12 · la hebra no nace: FATAL con snapshot LEGIBLE ─────────────────
class _HebraRota:
    """`Thread` que no arranca. La única forma de falsar el fallo de spawn sin
    agotar de verdad las hebras del sistema."""

    def __init__(self, *a, **kw):
        self.name = kw.get("name", "rota")

    def start(self):
        raise RuntimeError("can't start new thread")

    def is_alive(self):
        return False


def test_si_la_hebra_no_arranca_el_runner_queda_fatal_y_LEGIBLE(monkeypatch):
    """🩸 El defecto no era que el snapshot quedase MAL: quedaba ILEGIBLE.

    `start()` ponía `accepting_claims=True` antes de lanzar la hebra. Si el
    lanzamiento fallaba, el par (acepta=True, hebra muerta) hace levantar al
    invariante de `RunnerSnapshot` — o sea que `snapshot()` LEVANTA, y la única
    lectura del runner muere justo cuando hace falta leerla.
    """
    backend = FakeBackend(counts=R.OutboxCounts(LANE, 0, 0))
    active = runner(backend)
    monkeypatch.setattr(R.threading, "Thread", _HebraRota)
    with pytest.raises(RuntimeError):
        active.start()
    # ⊖ de la clase: sin el rollback dentro del lock, ESTA línea levanta.
    snapshot = active.snapshot()
    assert snapshot.state is R.RunnerState.FATAL
    assert snapshot.fatal_code == R.FATAL_THREAD_START_FAILED
    assert snapshot.accepting_claims is False and snapshot.thread_alive is False
    # Y la sesión no se queda viva detrás de una hebra que nunca existió.
    assert backend.closes == 1 and snapshot.session_open is False


def test_la_hebra_si_arranca_cuando_el_sistema_la_da():
    """⊕ control de FA-12: si nada arrancara nunca, el test de arriba no mediría
    el fallo de spawn sino el fallo de todo."""
    backend = FakeBackend(counts=R.OutboxCounts(LANE, 0, 0))
    active = runner(backend)
    active.start()
    try:
        assert wait_until(lambda: active.snapshot().thread_alive)
        assert active.snapshot().fatal_code is None
    finally:
        active.stop()


# ── FA-13 · stop() DURANTE starting: cierra la sesión real, sin fuga ─────
def test_un_stop_durante_starting_cierra_la_sesion_real_y_no_la_fuga():
    """🩸 LA FUGA: `open_worker_session()` corre fuera del cerrojo. Un `stop()`
    en esa ventana no veía la sesión —aún no publicada—, se marchaba, y la
    sesión NACÍA medio milisegundo después: runner parado, token de worker vivo
    hasta su TTL y sin dueño.
    """
    gate = threading.Event()
    backend = FakeBackend(counts=R.OutboxCounts(LANE, 0, 0))
    backend.open_gate = gate
    active = runner(backend)

    fallos: list[BaseException] = []

    def _arranca():
        try:
            active.start()
        except BaseException as exc:      # pragma: no cover - se afirma abajo
            fallos.append(exc)

    def _para():
        try:
            active.stop()
        except BaseException as exc:      # pragma: no cover - se afirma abajo
            fallos.append(exc)

    hebra_start = threading.Thread(target=_arranca, name="t-start")
    hebra_start.start()
    assert backend.open_entered.wait(3), "el backend nunca entró a abrir sesión"
    # La carrera es REAL y se comprueba por API pública, no por un `sleep`.
    assert active.snapshot().state is R.RunnerState.STARTING
    hebra_stop = threading.Thread(target=_para, name="t-stop")
    hebra_stop.start()
    time.sleep(0.05)                      # deja a `stop()` entrar en su espera
    gate.set()
    hebra_start.join(5)
    hebra_stop.join(5)
    assert not hebra_start.is_alive() and not hebra_stop.is_alive()
    assert fallos == []

    snapshot = active.snapshot()
    assert snapshot.state is R.RunnerState.STOPPED
    # LO QUE FALSA LA FUGA: la sesión se abrió Y se cerró, exactamente una vez.
    assert backend.calls.count("open_worker_session") == 1
    assert backend.closes == 1
    assert snapshot.session_open is False
    assert snapshot.thread_alive is False and projector_threads() == []


# ── FA-14 · un cierre que falla es REINTENTABLE y OBSERVABLE ─────────────
def test_un_cierre_fallido_deja_la_fuga_visible_y_el_siguiente_stop_reintenta():
    """🩸 `_session_closed = True` se escribía ANTES de pedir el cierre. Si el
    backend levantaba, la bandera ya decía «cerrada»: la idempotencia impedía
    el reintento y el token sobrevivía hasta su TTL sin que nadie lo supiera.
    """
    backend = FakeBackend(counts=R.OutboxCounts(LANE, 0, 0),
                          close_error=RuntimeError("núcleo no responde"),
                          close_error_once=True)
    active = runner(backend)
    active.start()
    with pytest.raises(RuntimeError):
        active.stop()
    # OBSERVABLE: la fuga se ve en el snapshot, no sólo en un log.
    fugado = active.snapshot()
    assert fugado.session_open is True
    assert fugado.close_failures == 1
    assert backend.closes == 1
    # REINTENTABLE: el segundo `stop()` vuelve a intentarlo de verdad.
    active.stop()
    cerrado = active.snapshot()
    assert cerrado.session_open is False
    assert cerrado.close_failures == 1
    assert backend.closes == 2
    # ⊖ de la idempotencia: un tercer stop NO vuelve a cerrar.
    active.stop()
    assert backend.closes == 2


# ── FA-15 · renovación explícita ANTES del TTL ──────────────────────────
def test_la_sesion_se_renueva_antes_del_ttl_y_el_runner_sigue_vivo():
    reloj = FakeClock(0.0)
    backend = FakeBackend(session=FakeSession(expires_at=1000.0),
                          counts=R.OutboxCounts(LANE, 0, 0))
    active = runner(backend, clock=reloj, renew_margin_s=10.0)
    active.start()
    try:
        assert wait_until(lambda: active.snapshot().state is R.RunnerState.IDLE)
        # Lejos del vencimiento: NO se renueva. Un runner que renueva cada
        # ciclo convierte la rotación en una tormenta contra el núcleo.
        time.sleep(0.05)
        assert backend.refreshes == 0
        # Dentro del margen: se renueva UNA vez y sigue vivo.
        reloj.now = 995.0
        assert wait_until(lambda: backend.refreshes >= 1)
        time.sleep(0.05)
        assert backend.refreshes == 1, "renovó en bucle en vez de una vez"
        assert active.snapshot().state in (R.RunnerState.IDLE,
                                           R.RunnerState.PROJECTING)
        assert active.snapshot().fatal_code is None
    finally:
        active.stop()
    # LA SESIÓN FUE SUSTITUIDA DE VERDAD: se cierra la NUEVA, no la vieja.
    assert backend.closed_tokens == ["worker-token-1"]


def test_no_reclama_si_el_ttl_cubre_el_margen_pero_no_el_lease():
    """El margen solo no basta: el claim que sigue pide autoridad por lease_s."""
    reloj = FakeClock(0.0)
    backend = FakeBackend(
        session=FakeSession(token="padre", expires_at=65.0),
        counts=R.OutboxCounts(LANE, 0, 0))
    active = runner(
        backend, clock=reloj, lease_s=60, stop_timeout_s=90.0,
        renew_margin_s=10.0)
    active.start()
    try:
        assert wait_until(lambda: backend.refreshes == 1)
        assert wait_until(lambda: len(backend.tokens_usados) >= 1)
        assert backend.projector_tokens[:2] == ["padre", "worker-token-1"]
        assert backend.tokens_usados[0] == "worker-token-1", (
            "reclamó primero con una sesión cuyo TTL no cubría lease+margen")
        assert "padre" not in backend.tokens_usados
    finally:
        active.stop()
    assert backend.closed_tokens == ["worker-token-1"]


def test_la_renovacion_pide_ttl_s_explicito_lease_mas_margen_mas_uno():
    """🩸 EL TTL YA NO LO DECIDE EL BACKEND SOLO. Antes `refresh_worker_session`
    no recibía ningún `ttl_s`: el runner pedía la rotación y confiaba en lo
    que el backend quisiera darle. Ahora lo pide explícito -
    `ceil(lease_s + renew_margin_s) + 1`, la holgura exacta que el ciclo que
    sigue necesita más un segundo de colchón contra el redondeo."""
    reloj = FakeClock(0.0)
    backend = FakeBackend(session=FakeSession(expires_at=1000.0),
                          counts=R.OutboxCounts(LANE, 0, 0))
    active = runner(backend, clock=reloj, lease_s=7, renew_margin_s=2.5,
                    stop_timeout_s=90.0)
    active.start()
    try:
        reloj.now = 1000.0 - 7 - 2.5   # justo en el borde del margen
        assert wait_until(lambda: backend.refreshes >= 1)
        assert backend.requested_ttls[:1] == [math.ceil(7 + 2.5) + 1]
    finally:
        active.stop()


def test_renovacion_se_revalida_con_reloj_releido_tras_projector_for():
    """🩸 VALIDAR CONTRA EL RELOJ DE ANTES DEL ROUND-TRIP DEJA PASAR UNA HIJA
    INSUFICIENTE. La rotación mueve el vencimiento hacia delante (1000→1010,
    pasa el chequeo viejo), pero construir el proyector de la hija tarda -aquí,
    simulado sin `sleep`- 20s: para cuando se relee el reloj, la hija YA no
    cubre ni su propio `lease_s + renew_margin_s` desde AHORA. Sin releer el
    reloj tras `projector_for`, el runner habría adoptado una sesión que nace
    caduca para el ciclo que se le viene encima.
    """
    reloj = FakeClock(985.0)
    backend = FakeBackend(
        session=FakeSession(token="padre", expires_at=1000.0),
        counts=R.OutboxCounts(LANE, 0, 0),
        refreshed=FakeSession(token="hija", expires_at=1010.0),
    )
    backend.delay_clock = reloj
    backend.projector_delay_s = 20.0
    active = runner(backend, clock=reloj, lease_s=10, renew_margin_s=5.0,
                    stop_timeout_s=90.0)
    active.start()
    try:
        assert wait_until(lambda: active.snapshot().state is R.RunnerState.FATAL)
        assert active.snapshot().fatal_code == R.FATAL_SESSION_REFRESH_FAILED
        assert backend.refreshes == 1
    finally:
        active.stop()
    # La hija SE ADOPTÓ -para poder cerrarla- aunque la revalidación la
    # rechazara: la padre ya estaba revocada desde que `refresh` volvió.
    assert backend.closed_tokens == ["hija"]


def test_sin_renovacion_el_runner_moriria_al_vencer(monkeypatch):
    """⊖ COMPLEMENTO de FA-15: sin el verbo de renovación el vencimiento es un
    acantilado. Se falsa quitándole al backend la capacidad de renovar."""
    reloj = FakeClock(0.0)
    backend = FakeBackend(session=FakeSession(expires_at=1000.0),
                          counts=R.OutboxCounts(LANE, 0, 0),
                          refresh_error=RuntimeError("sin renovación"))
    active = runner(backend, clock=reloj, renew_margin_s=10.0)
    active.start()
    try:
        assert wait_until(lambda: active.snapshot().state is R.RunnerState.IDLE)
        reloj.now = 995.0
        assert wait_until(lambda: active.snapshot().state is R.RunnerState.FATAL)
        # Y el código DICE LO QUE PASÓ. Antes decía `PROJECTION_CYCLE_FAILED`:
        # mandaba a depurar el proyector por un token vencido.
        assert active.snapshot().fatal_code == R.FATAL_SESSION_REFRESH_FAILED
        assert active.snapshot().fatal_code != R.FATAL_PROJECTION_CYCLE_FAILED
    finally:
        active.stop()


@pytest.mark.parametrize("interrupcion", [KeyboardInterrupt, SystemExit])
def test_una_interrupcion_durante_la_renovacion_es_RUNNER_INTERRUPTED(interrupcion):
    """🩸 `_renovar_si_toca()` atrapaba `BaseException` a secas en sus tres
    llamadas al backend: un Ctrl-C exactamente ahí se publicaba como
    `SESSION_REFRESH_FAILED`, y quien lee el snapshot sale a depurar una
    renovación que nunca falló -sólo se interrumpió el propio runner. Ahora
    sólo `Exception` se atrapa ahí: `KeyboardInterrupt`/`SystemExit` suben
    hasta el `except BaseException` de `_run()`, que sí sabe nombrarlos.
    """
    reloj = FakeClock(995.0)
    backend = FakeBackend(session=FakeSession(expires_at=1000.0),
                          counts=R.OutboxCounts(LANE, 0, 0),
                          refresh_error=interrupcion("boom"))
    active = runner(backend, clock=reloj, renew_margin_s=10.0)
    active.start()
    try:
        assert wait_until(lambda: active.snapshot().state is R.RunnerState.FATAL)
        assert active.snapshot().fatal_code == R.FATAL_RUNNER_INTERRUPTED
        assert active.snapshot().fatal_code != R.FATAL_SESSION_REFRESH_FAILED
    finally:
        active.stop()


# ── FA-16 · la renovación NO puede ensanchar privilegio ─────────────────
def test_una_renovacion_que_ensancha_el_privilegio_es_terminal():
    reloj = FakeClock(0.0)
    backend = FakeBackend(session=FakeSession(expires_at=1000.0),
                          counts=R.OutboxCounts(LANE, 0, 0))
    backend.refreshed = FakeSession(
        capabilities=(R.CAP_OUTBOX_WORKER, "outbox_operator"),
        expires_at=2000.0)
    active = runner(backend, clock=reloj, renew_margin_s=10.0)
    active.start()
    try:
        reloj.now = 995.0
        assert wait_until(lambda: active.snapshot().state is R.RunnerState.FATAL)
        assert (active.snapshot().fatal_code
                == R.FATAL_CAPABILITIES_NOT_LEAST_PRIVILEGE)
    finally:
        active.stop()


def test_una_renovacion_de_otro_carril_es_terminal():
    reloj = FakeClock(0.0)
    backend = FakeBackend(session=FakeSession(expires_at=1000.0),
                          counts=R.OutboxCounts(LANE, 0, 0))
    backend.refreshed = FakeSession(lane="64bis", expires_at=2000.0)
    active = runner(backend, clock=reloj, renew_margin_s=10.0)
    active.start()
    try:
        reloj.now = 995.0
        assert wait_until(lambda: active.snapshot().state is R.RunnerState.FATAL)
        assert active.snapshot().fatal_code == R.FATAL_LANE_MISMATCH
    finally:
        active.stop()


def test_una_renovacion_que_no_mueve_el_vencimiento_no_renueva():
    """Devolver la misma fecha es dejar el mismo acantilado un ciclo más tarde
    y llamarlo éxito."""
    reloj = FakeClock(0.0)
    backend = FakeBackend(session=FakeSession(expires_at=1000.0),
                          counts=R.OutboxCounts(LANE, 0, 0))
    backend.refreshed = FakeSession(expires_at=1000.0)
    active = runner(backend, clock=reloj, renew_margin_s=10.0)
    active.start()
    try:
        reloj.now = 995.0
        assert wait_until(lambda: active.snapshot().state is R.RunnerState.FATAL)
        assert active.snapshot().fatal_code == R.FATAL_SESSION_REFRESH_FAILED
    finally:
        active.stop()


# ── FA-17 · una sesión vencida no se adopta ni se resucita ──────────────
def test_una_sesion_ya_vencida_no_arranca():
    reloj = FakeClock(1000.0)
    backend = FakeBackend(session=FakeSession(expires_at=999.0),
                          counts=R.OutboxCounts(LANE, 0, 0))
    active = runner(backend, clock=reloj)
    with pytest.raises(R.RunnerConfigurationError):
        active.start()
    assert active.snapshot().fatal_code == R.FATAL_SESSION_EXPIRED
    assert backend.closes == 1 and active.snapshot().session_open is False


def test_una_sesion_sin_vencimiento_no_arranca():
    backend = FakeBackend(session=FakeSession(expires_at=None),
                          counts=R.OutboxCounts(LANE, 0, 0))
    active = runner(backend)
    with pytest.raises(R.RunnerConfigurationError):
        active.start()
    assert active.snapshot().fatal_code == R.FATAL_SESSION_INVALID


def test_un_vencimiento_alcanzado_en_vuelo_NO_se_intenta_renovar():
    """Pedir la rotación de un token muerto es lo que el núcleo llama
    resucitar: se para, y no se empuja contra el guard ajeno."""
    reloj = FakeClock(0.0)
    backend = FakeBackend(session=FakeSession(expires_at=1000.0),
                          counts=R.OutboxCounts(LANE, 0, 0))
    active = runner(backend, clock=reloj, renew_margin_s=10.0)
    active.start()
    try:
        assert wait_until(lambda: active.snapshot().state is R.RunnerState.IDLE)
        reloj.now = 1001.0                # ya vencido, sin pasar por el margen
        assert wait_until(lambda: active.snapshot().state is R.RunnerState.FATAL)
        assert active.snapshot().fatal_code == R.FATAL_SESSION_EXPIRED
        assert backend.refreshes == 0, "intentó resucitar un token muerto"
    finally:
        active.stop()


# ── FA-18 · lease_s entero EXACTO en 1..3600 ────────────────────────────
@pytest.mark.parametrize("lease", [0, -1, 3601, 10**9, True, 1.0, "60", None])
def test_lease_s_fuera_de_rango_o_de_tipo_no_construye(lease):
    backend = FakeBackend(counts=R.OutboxCounts(LANE, 0, 0))
    with pytest.raises(R.RunnerConfigurationError):
        runner(backend, lease_s=lease, stop_timeout_s=4000.0)
    assert backend.calls == []


@pytest.mark.parametrize("lease", [1, 60, 3600])
def test_los_extremos_validos_del_lease_si_construyen(lease):
    """⊕ control: si NADA construyera, el rechazo de arriba no mediría el rango."""
    backend = FakeBackend(counts=R.OutboxCounts(LANE, 0, 0))
    assert runner(backend, lease_s=lease, stop_timeout_s=4000.0) is not None


# ── FA-19 · la pareja lease ↔ stop_timeout se valida al CONSTRUIR ───────
def test_un_stop_timeout_menor_que_el_lease_no_construye():
    """El presupuesto de stop debe cubrir al menos el lease que se solicita.

    Esto NO promete que el ciclo termine dentro del lease; un backend colgado
    puede exceder ambos y `stop()` debe seguir fallando de forma visible.
    """
    backend = FakeBackend(counts=R.OutboxCounts(LANE, 0, 0))
    with pytest.raises(R.RunnerConfigurationError) as caught:
        R.ActiveProjectorRunner(backend, lane=LANE, lease_s=60,
                                stop_timeout_s=30.0)
    assert "lease_s" in str(caught.value)
    # ⊕ control: los defaults ACTUALES sí construyen.
    assert R.ActiveProjectorRunner(backend, lane=LANE) is not None
    assert backend.calls == []


# ── FA-20 · ninguna BaseException se traga sin código cerrado ───────────
class _RaraBase(BaseException):
    """Ni `Exception` ni interrupción: lo que antes salía como culpa ajena."""


def test_una_baseexception_interna_no_se_publica_como_culpa_del_backend():
    backend = FakeBackend(counts=_RaraBase("algo muy raro"))
    active = runner(backend)
    active.start()
    try:
        assert wait_until(lambda: active.snapshot().state is R.RunnerState.FATAL)
        snapshot = active.snapshot()
        assert snapshot.fatal_code == R.FATAL_RUNNER_INTERNAL_ERROR
        # ⊖ EXACTO: éste es el código que se publicaba antes, y era mentira.
        assert snapshot.fatal_code != R.FATAL_BACKEND_CONTRACT
    finally:
        active.stop()


def test_una_interrupcion_tiene_su_propio_codigo_y_no_se_confunde():
    backend = FakeBackend(counts=KeyboardInterrupt())
    active = runner(backend)
    active.start()
    try:
        assert wait_until(lambda: active.snapshot().state is R.RunnerState.FATAL)
        assert active.snapshot().fatal_code == R.FATAL_RUNNER_INTERRUPTED
    finally:
        active.stop()


# ── FA-21 · la perilla de capacidades NO EXISTE ─────────────────────────
def test_no_hay_perilla_que_autorice_un_superconjunto_de_capacidades():
    """🩸 `required_capabilities` era un argumento, así que «mínimo privilegio
    POR IGUALDAD» valía lo que valiera el argumento: pasando
    `{outbox_worker, outbox_operator}` la igualdad se comparaba contra el
    SUPERCONJUNTO y admitía una sesión de operador. La perilla ERA el bypass."""
    backend = FakeBackend(
        session=FakeSession(capabilities=(R.CAP_OUTBOX_WORKER, "outbox_operator")),
        counts=R.OutboxCounts(LANE, 0, 0))
    with pytest.raises(TypeError):
        R.ActiveProjectorRunner(
            backend, lane=LANE,
            required_capabilities=frozenset(
                {R.CAP_OUTBOX_WORKER, "outbox_operator"}))
    # Y sin la perilla, esa misma sesión sigue siendo inadmisible.
    active = runner(backend)
    with pytest.raises(R.RunnerConfigurationError):
        active.start()
    assert (active.snapshot().fatal_code
            == R.FATAL_CAPABILITIES_NOT_LEAST_PRIVILEGE)
    # El conjunto exigido es la CONSTANTE del módulo, no estado por instancia.
    assert R.LEAST_PRIVILEGE_CAPABILITIES == frozenset({R.CAP_OUTBOX_WORKER})
    import inspect as _i
    assert "required_capabilities" not in _i.signature(
        R.ActiveProjectorRunner.__init__).parameters


# ── FA-22 · drain NIEGA; quiesce no certifica nada ──────────────────────
def test_drain_niega_porque_aquietar_no_es_drenar():
    """🩸 El método hacía `quiesce()` y se llamaba `drain()`: cerraba la
    admisión LOCAL y esperaba al ciclo en vuelo. `pending` podía quedar en 400
    y volvía sin excepción — y quien lee `drain()` sin excepción concluye que no
    queda trabajo, y decide un rollback sobre esa conclusión."""
    backend = FakeBackend(counts=R.OutboxCounts(LANE, 400, 3))
    active = runner(backend)
    active.start()
    try:
        assert wait_until(lambda: active.snapshot().pending == 400)
        with pytest.raises(R.RunnerDrainNotSupported) as caught:
            active.drain(timeout_s=1)
        assert "barrera de admisión" in str(caught.value)
        # `quiesce` SÍ funciona, y no promete haber vaciado nada: deja
        # `pending` a la vista, en 400.
        aquietado = active.quiesce(timeout_s=3)
        assert aquietado.accepting_claims is False
        assert aquietado.in_flight is False
        assert aquietado.pending == 400 and aquietado.failed == 3
        assert aquietado.outbox_certifiable is False
        with pytest.raises(R.RollbackNotCertifiable):
            active.certify_rollback()
    finally:
        active.stop()


def test_aquietar_con_pending_a_cero_TAMPOCO_certifica():
    """⊖ la cara peligrosa: un cero medido se parece mucho a un certificado."""
    backend = FakeBackend(counts=R.OutboxCounts(LANE, 0, 0))
    active = runner(backend)
    active.start()
    try:
        assert wait_until(lambda: active.snapshot().pending == 0)
        aquietado = active.quiesce(timeout_s=3)
        assert aquietado.pending == 0 and aquietado.failed == 0
        assert aquietado.outbox_certifiable is False
        with pytest.raises(R.RunnerDrainNotSupported):
            active.drain(timeout_s=1)
    finally:
        active.stop()


def test_el_null_object_sigue_teniendo_los_dos_verbos():
    nulo = R.projector_runner_for_mode("disabled")
    assert nulo.quiesce(timeout_s=0).outbox_certifiable is False
    assert nulo.drain(timeout_s=0).outbox_certifiable is False


# ── FA-23 · el snapshot del runner activo es SIEMPRE legible ────────────
@pytest.mark.parametrize("guion", [
    {"open_error": RuntimeError("núcleo caído")},
    {"session": FakeSession(lane="64bis")},
    {"session": FakeSession(capabilities=())},
    {"session": FakeSession(expires_at=None)},
])
def test_ningun_rechazo_de_arranque_deja_el_snapshot_ilegible(guion):
    """Un snapshot que levanta es peor que uno feo: la lectura del runner tiene
    que sobrevivir al fallo que hay que leer."""
    backend = FakeBackend(counts=R.OutboxCounts(LANE, 0, 0), **guion)
    active = runner(backend)
    with pytest.raises(BaseException):
        active.start()
    snapshot = active.snapshot()          # ⊖ si esto levanta, el test cae
    assert snapshot.state is R.RunnerState.FATAL
    assert isinstance(snapshot.fatal_code, str) and snapshot.fatal_code
    assert snapshot.accepting_claims is False and snapshot.in_flight is False
    assert snapshot.outbox_certifiable is False


# ── FA-24 · la rotación cambia el TOKEN QUE SE USA, no sólo el que se guarda ─
def test_tras_renovar_el_ciclo_usa_el_token_de_la_HIJA_y_no_el_de_la_padre():
    """🩸 EL BLOQUEO: sustituir `_session` sin reconstruir `_projector` NO
    renueva nada. `MarkdownProjector` guarda el token al construirse, así que el
    ciclo siguiente seguía presentando el de la sesión PADRE —que
    `Journal.refresh_session` acaba de revocar en la misma transacción— y el
    runner se moría con credencial muerta detrás de una renovación que el
    snapshot daba por buena.

    Este falsador no mira el campo guardado: mira **el token que de verdad llega
    a `project_next`**.
    """
    reloj = FakeClock(0.0)
    backend = FakeBackend(session=FakeSession(token="padre", expires_at=1000.0),
                          counts=R.OutboxCounts(LANE, 0, 0))
    active = runner(backend, clock=reloj, renew_margin_s=10.0)
    active.start()
    try:
        assert wait_until(lambda: len(backend.tokens_usados) >= 1)
        assert set(backend.tokens_usados) == {"padre"}
        assert backend.projector_tokens == ["padre"]

        usados_antes = len(backend.tokens_usados)
        reloj.now = 995.0
        assert wait_until(lambda: backend.refreshes >= 1)
        # El proyector se RECONSTRUYE con la hija: dos llamadas a projector_for.
        assert wait_until(lambda: len(backend.projector_tokens) >= 2)
        assert backend.projector_tokens == ["padre", "worker-token-1"]

        # ⊖ EL CORAZÓN DEL FALSADOR: a partir de aquí NINGÚN ciclo puede volver
        # a presentar el token de la padre. Sin la reconstrucción del proyector,
        # todos los tokens de después del refresh seguirían siendo "padre".
        assert wait_until(
            lambda: len(backend.tokens_usados) > usados_antes)
        despues = backend.tokens_usados[usados_antes:]
        assert despues, "no hubo ningún ciclo después de renovar"
        assert set(despues) == {"worker-token-1"}, (
            f"el ciclo siguió usando la credencial revocada: {set(despues)}")

        # Y no se murió: la renovación fue de verdad.
        assert active.snapshot().fatal_code is None
        assert active.snapshot().state in (R.RunnerState.IDLE,
                                           R.RunnerState.PROJECTING)
    finally:
        active.stop()

    # CIERRE FINAL SÓLO DE LA HIJA: la padre ya la revocó el núcleo al rotar;
    # cerrarla sería cerrar un cadáver y contar una fuga que no existe.
    assert backend.closed_tokens == ["worker-token-1"]
    assert "padre" not in backend.closed_tokens
    assert backend.closes == 1


def test_si_el_proyector_de_la_hija_no_se_puede_construir_la_hija_SE_CIERRA():
    """La padre ya está revocada cuando `refresh_worker_session` vuelve, así que
    la hija es la única sesión viva a nuestro nombre: se adopta aunque el
    proyector falle, o el cierre final cerraría el cadáver y la dejaría suelta."""
    reloj = FakeClock(0.0)
    backend = FakeBackend(session=FakeSession(token="padre", expires_at=1000.0),
                          counts=R.OutboxCounts(LANE, 0, 0))
    active = runner(backend, clock=reloj, renew_margin_s=10.0)
    active.start()
    try:
        assert wait_until(lambda: active.snapshot().state is R.RunnerState.IDLE)
        backend.projector_error = RuntimeError("no puedo construir proyector")
        reloj.now = 995.0
        assert wait_until(lambda: active.snapshot().state is R.RunnerState.FATAL)
        assert active.snapshot().fatal_code == R.FATAL_BACKEND_CONTRACT
        assert active.snapshot().session_open is True
    finally:
        active.stop()
    assert backend.closed_tokens == ["worker-token-1"], (
        "cerró la padre revocada y dejó la hija suelta")


def test_una_hija_rechazada_por_privilegio_TAMBIEN_se_cierra():
    """⊕ hermano del anterior por la otra rama: la hija con capacidades anchas
    no se usa NUNCA, pero existe — y lo que existe se cierra."""
    reloj = FakeClock(0.0)
    backend = FakeBackend(session=FakeSession(token="padre", expires_at=1000.0),
                          counts=R.OutboxCounts(LANE, 0, 0))
    backend.refreshed = FakeSession(
        token="hija-ancha",
        capabilities=(R.CAP_OUTBOX_WORKER, "outbox_operator"),
        expires_at=2000.0)
    active = runner(backend, clock=reloj, renew_margin_s=10.0)
    active.start()
    try:
        reloj.now = 995.0
        assert wait_until(lambda: active.snapshot().state is R.RunnerState.FATAL)
        assert (active.snapshot().fatal_code
                == R.FATAL_CAPABILITIES_NOT_LEAST_PRIVILEGE)
        # Nunca llegó a proyectar con ella.
        assert "hija-ancha" not in backend.tokens_usados
        assert backend.projector_tokens == ["padre"]
    finally:
        active.stop()
    assert backend.closed_tokens == ["hija-ancha"]


def test_si_stop_se_agota_esperando_a_starting_lanza_RunnerStopTimeout():
    """La OTRA cara de la carrera, y la que de verdad fuga.

    `stop()` espera a que `starting` resuelva, pero esa espera está ACOTADA por
    `stop_timeout_s`. Si abrir la sesión tarda más que ese plazo, `stop()` se
    rinde con `_session` todavía a `None`: no hay nada que cerrar.

    🩸 Y AHORA LO DICE. Antes esto volvía SIN avisar -`thread` seguía siendo
    `None`, así que el `if thread is not None` nunca disparaba- y quien llamó
    a `stop()` se iba creyendo que había parado algo. El contrato exige
    `RunnerStopTimeout` aquí, igual que cuando el que se cuelga es el `join()`
    de una hebra viva: los dos son la MISMA promesa incumplida.

    La intención (`_stop_requested`) queda grabada igual, y es `start()` -que
    re-lee bajo el mismo cerrojo con el que publica la sesión- quien cierra
    cuando la apertura por fin resuelve. Quien llega el último cierra.

    El punto de sincronía es el RETORNO de `stop()`, no un `sleep`: la ventana
    queda abierta de forma determinista.
    """
    gate = threading.Event()
    backend = FakeBackend(counts=R.OutboxCounts(LANE, 0, 0))
    backend.open_gate = gate
    active = runner(backend, lease_s=1, stop_timeout_s=1.0)

    fallos: list[BaseException] = []

    def _arranca():
        try:
            active.start()
        except BaseException as exc:      # pragma: no cover - se afirma abajo
            fallos.append(exc)

    hebra = threading.Thread(target=_arranca, name="t-start-lento")
    hebra.start()
    assert backend.open_entered.wait(3)
    assert active.snapshot().state is R.RunnerState.STARTING

    # `stop()` se rinde: la apertura sigue bloqueada tras su plazo, y AHORA
    # avisa en vez de volver en silencio.
    inicio = time.monotonic()
    with pytest.raises(R.RunnerStopTimeout):
        active.stop()
    transcurrido = time.monotonic() - inicio
    # UN SOLO DELOJ: la espera entera cabe en `stop_timeout_s`, no en un
    # múltiplo de él.
    assert transcurrido < 1.5
    assert hebra.is_alive(), "el arranque ya había terminado: no hay ventana"
    # LA VENTANA, MEDIDA: aquí no hay sesión y por eso `stop()` no cerró nada.
    assert backend.closes == 0
    assert active.snapshot().session_open is False

    gate.set()
    hebra.join(5)
    assert not hebra.is_alive() and fallos == []

    # ⊖ SIN la re-lectura en `start()`: la hebra se lanza, nadie cierra, y esta
    # línea ve `closes == 0` con una sesión viva hasta su TTL.
    assert wait_until(lambda: backend.closes == 1), (
        "la sesión nació huérfana tras rendirse el stop")
    snapshot = active.snapshot()
    assert snapshot.session_open is False


    assert snapshot.state is R.RunnerState.STOPPED
    assert snapshot.thread_alive is False and projector_threads() == []


# ── FA-25 · `_measure` lee cada campo UNA sola vez, nunca reenvía el objeto ──
class _ContadorDeAccesos:
    """Cuenta cada lectura de `lane`/`pending`/`failed`. Si `_measure` -o
    quien use lo que devuelve- reenviara el objeto del backend en vez de
    congelarlo en un DTO local, estos contadores subirían por encima del
    número de invocaciones a `outbox_counts()`."""

    def __init__(self, lane, pending, failed):
        self._lane, self._pending, self._failed = lane, pending, failed
        self.lecturas = {"lane": 0, "pending": 0, "failed": 0}

    @property
    def lane(self):
        self.lecturas["lane"] += 1
        return self._lane

    @property
    def pending(self):
        self.lecturas["pending"] += 1
        return self._pending

    @property
    def failed(self):
        self.lecturas["failed"] += 1
        return self._failed


def test_measure_lee_lane_pending_failed_una_sola_vez_por_invocacion():
    contador = _ContadorDeAccesos(LANE, 5, 2)
    backend = FakeBackend(counts=contador)
    active = runner(backend)
    active.start()
    try:
        assert wait_until(lambda: active.snapshot().pending == 5)
        assert active.snapshot().failed == 2
        invocaciones = backend.calls.count("outbox_counts")
        assert invocaciones >= 1
        # UNA lectura de cada campo POR invocación a la sonda, ni una más: si
        # `_after_cycle` releyera `counts.pending`/`counts.failed` del objeto
        # del backend en vez de la copia local, estos números se separarían.
        assert contador.lecturas == {
            "lane": invocaciones, "pending": invocaciones, "failed": invocaciones,
        }
    finally:
        active.stop()


def test_un_getter_de_contadores_que_levanta_BaseException_sube_y_no_es_contrato():
    """🩸 `outbox_counts_conforma` desaparecido y `_measure` reescrito: leer
    `lane`/`pending`/`failed` sólo atrapa `Exception`. Si UNO de esos getters
    levanta algo más raro -una interrupción real, no un fallo de datos-, tiene
    que subir y publicarse con SU propio código, no disfrazarse de
    `BACKEND_CONTRACT_VIOLATED` -que es lo que le pasaría a un `except
    BaseException` demasiado ancho ahí dentro."""

    class _ContadoresQueInterrumpen:
        lane = LANE
        pending = 0

        @property
        def failed(self):
            raise KeyboardInterrupt("ctrl-c en el getter, no en la sonda")

    backend = FakeBackend(counts=_ContadoresQueInterrumpen())
    active = runner(backend)
    active.start()
    try:
        assert wait_until(lambda: active.snapshot().state is R.RunnerState.FATAL)
        snapshot = active.snapshot()
        assert snapshot.fatal_code == R.FATAL_RUNNER_INTERRUPTED
        assert snapshot.fatal_code != R.FATAL_BACKEND_CONTRACT
    finally:
        active.stop()


# ── FA-26 · `start()` relee `_quiesce_requested` en el CERROJO FINAL ────────
class _CondActivable:
    """Envuelve un `threading.Condition` real y deja enganchar un `hook` a
    CADA `__enter__`. `wait()`/`notify_all()` operan siempre sobre el objeto
    real: sólo se intercepta el instante de ADQUIRIR, que es donde vive la
    ventana entre dos `with self._cond:` sucesivos."""

    def __init__(self, real, hook):
        self._real = real
        self._hook = hook

    def __enter__(self):
        self._hook()
        return self._real.__enter__()

    def __exit__(self, *exc_info):
        return self._real.__exit__(*exc_info)

    def wait(self, timeout=None):
        return self._real.wait(timeout)

    def notify_all(self):
        return self._real.notify_all()


def test_start_relee_quiesce_requested_en_el_cerrojo_final_no_el_capturado_antes():
    """🩸 EL DEFECTO: `quiescing = self._quiesce_requested` se leía en el
    cerrojo que publica la sesión y se usaba en el SIGUIENTE `with
    self._cond:` -una adquisición DISTINTA del mismo `_cond`, no la misma
    sección crítica. En el hueco entre soltar el primero y adquirir el
    segundo cabe un `quiesce()` completo, y `start()` lo ignoraba porque ya
    llevaba una copia vieja. Se fuerza esa ventana interceptando la TERCERA
    adquisición real de `_cond` dentro de `start()` -las dos primeras son
    "entrar en starting" y "publicar la sesión"- y, mientras `start()` está
    parado justo ANTES de tomar el cerrojo (que por tanto está LIBRE), se
    lanza un `quiesce()` real desde otra hebra.
    """
    backend = FakeBackend(counts=R.OutboxCounts(LANE, 0, 0))
    active = runner(backend)

    contador = {"n": 0}
    cerrojo_libre = threading.Event()
    seguir = threading.Event()

    def hook():
        contador["n"] += 1
        if contador["n"] == 3:
            cerrojo_libre.set()
            assert seguir.wait(5)

    active._cond = _CondActivable(active._cond, hook)

    hebra_start = threading.Thread(target=active.start, name="t-start")
    hebra_start.start()
    assert cerrojo_libre.wait(3)

    def _quiesce():
        active.quiesce(timeout_s=3)

    hebra_quiesce = threading.Thread(target=_quiesce, name="t-quiesce")
    hebra_quiesce.start()
    assert wait_until(lambda: active._quiesce_requested is True)

    seguir.set()
    hebra_start.join(5)
    hebra_quiesce.join(5)

    # ⊖ SIN el fix: `start()` seguía leyendo la copia vieja (`False`), abría
    # `accepting=True` y lanzaba la hebra de proyección -exactamente lo que
    # `quiesce()` acababa de pedir que NO pasara.
    assert wait_until(lambda: active.snapshot().state is R.RunnerState.DRAINING)
    snapshot = active.snapshot()
    assert snapshot.accepting_claims is False
    assert snapshot.thread_alive is False and projector_threads() == []
    assert backend.leases == []
    active.stop()
