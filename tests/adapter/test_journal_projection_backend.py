"""Falsadores focales de `journal_projection_backend.py`.

Dos capas: contrato aislado (FakeJournal, sin abrir SQLite) para lo que se
puede falsar por firma y por excepción, e integración real (Journal +
MarkdownProjector) para lo único que un doble no puede acreditar — que la
sesión abierta trae EXACTAMENTE `outbox_worker` y que `outbox_counts` lee el
mismo carril que acaba de escribir.

Implementado y re-verificado contra el Protocol tras `1ebb817` (medido en este
checkout: `outbox_counts` es estructural — `OutboxCountsLike`, no una clase
concreta —, y `refresh_worker_session(self, session, *, ttl_s)` recibe el TTL
que pide el runner).
"""
from __future__ import annotations

import pytest

import coordination as C
import journal_projection_backend as J
import projector as P
import projector_runner as PR
from tests.journal._arnes import GRAMATICA, censo, journal as build_journal

LANE = "llminbox"
FRAME_KEY = b"frame-key-del-adaptador-32-bytes"


def _target(path):
    return P.LedgerTarget(LANE, LANE, str(path))


# ══ integración real: Journal + MarkdownProjector ══════════════════════════

def _backend(tmp_path, *, ttl_s=900, capabilities=(C.CAP_OUTBOX_WORKER,)):
    j = build_journal(tmp_path)
    j.bind_credential("worker", principal="projector-daemon", role="projector",
                      lane=LANE, capabilities=capabilities)
    ledger_path = tmp_path / f"{LANE}.md"
    ledger_path.write_bytes(b"")
    backend = J.JournalProjectionBackend(
        j, "worker", [_target(ledger_path)], frame_key=FRAME_KEY, ttl_s=ttl_s)
    return j, backend


def test_open_worker_session_trae_exactamente_outbox_worker(tmp_path):
    _, backend = _backend(tmp_path)
    session = backend.open_worker_session()
    assert session.lane == LANE
    assert session.capabilities == (C.CAP_OUTBOX_WORKER,)
    assert isinstance(session.token, str) and session.token


def test_projector_for_devuelve_markdownprojector_que_proyecta(tmp_path):
    j, backend = _backend(tmp_path)
    j.bind_credential("producer", principal="alice", role="backend", lane=LANE,
                      capabilities=())
    producer = j.open_session("producer")
    j.accept_event(producer.token, idempotency_key="event-1",
                    intent={"type": "message", "verb": "inform", "to": ["security"],
                            "kind": "DELIVERED", "head": "h", "body": "b"},
                    ledger=LANE)
    session = backend.open_worker_session()
    projector = backend.projector_for(session)
    assert isinstance(projector, P.MarkdownProjector)
    outcome = projector.project_next(lease_s=60)
    assert outcome is not None and outcome.appended is True


def test_outbox_counts_es_la_medida_del_nucleo_con_su_carril(tmp_path):
    """Ya NO se envuelve en un segundo DTO: el contrato es estructural desde
    `1ebb817` y `coordination.OutboxCounts` ya conforma `lane`/`pending`/`failed`."""
    _, backend = _backend(tmp_path)
    session = backend.open_worker_session()
    counts = backend.outbox_counts(session)
    assert type(counts) is C.OutboxCounts
    assert (counts.lane, counts.pending, counts.failed) == (LANE, 0, 0)


def test_close_worker_session_revoca_de_verdad(tmp_path):
    j, backend = _backend(tmp_path)
    session = backend.open_worker_session()
    backend.close_worker_session(session)
    with pytest.raises(C.AuthError):
        j.outbox_counts(session.token)


def test_close_worker_session_es_idempotente(tmp_path):
    _, backend = _backend(tmp_path)
    session = backend.open_worker_session()
    backend.close_worker_session(session)
    backend.close_worker_session(session)  # segunda vez: NO debe levantar


def test_refresh_worker_session_rota_y_extiende_con_el_ttl_pedido(tmp_path):
    """El TTL de la renovación lo pide EL LLAMANTE (`ttl_s=`), no
    `self._ttl_s` del backend -ese sigue gobernando sólo la apertura inicial."""
    _, backend = _backend(tmp_path, ttl_s=100)
    session = backend.open_worker_session()
    renewed = backend.refresh_worker_session(session, ttl_s=55)
    assert renewed.token != session.token
    assert renewed.expires_at == pytest.approx(session.expires_at - 100 + 55, abs=2)
    assert renewed.lane == LANE
    assert renewed.capabilities == (C.CAP_OUTBOX_WORKER,)


def test_ttl_s_de_apertura_exige_int_exacto_positivo(tmp_path):
    """`bool` ES subclase de `int`: `ttl_s=True` no puede colar como `1`."""
    j = build_journal(tmp_path)
    for malo in (0, -1, True, False, 1.0, "900"):
        with pytest.raises(J.ProjectionBackendConfigurationError):
            J.JournalProjectionBackend(
                j, "worker", [P.LedgerTarget(LANE, LANE, "/tmp/x")],
                frame_key=FRAME_KEY, ttl_s=malo)


# ══ contrato aislado con un Journal falso ══════════════════════════════════

class FakeJournal:
    """Mismas firmas keyword-only que `coordination.Journal` en los verbos que
    usa el adaptador — si el adaptador llamase posicional, esto lo delata con
    un `TypeError`, no con una lectura de bitácora."""

    def __init__(self):
        self.revoke_calls = []

    def open_session(self, credential, *, ttl_s):
        assert credential == "worker"
        return C.IssuedSession("tok-1", "rti-1", "pid", "projector", LANE,
                                1_000.0 + ttl_s, 1, "daemon", "explicit",
                                (C.CAP_OUTBOX_WORKER,))

    def refresh_session(self, token, *, ttl_s):
        assert token == "tok-1"
        return C.IssuedSession("tok-2", "rti-2", "pid", "projector", LANE,
                                2_000.0 + ttl_s, 1, "daemon", "explicit",
                                (C.CAP_OUTBOX_WORKER,))

    def revoke_current(self, token, reason="revoked"):
        self.revoke_calls.append((token, reason))
        raise C.AuthError("sesión ya no válida")

    def outbox_counts(self, token):
        return C.OutboxCounts(lane=LANE, pending=3, failed=1)


class _RompeAlRevocar(FakeJournal):
    def revoke_current(self, token, reason="revoked"):
        raise RuntimeError("fallo real de base de datos")


def test_open_worker_session_pasa_ttl_configurado():
    fake = FakeJournal()
    backend = J.JournalProjectionBackend(
        fake, "worker", [P.LedgerTarget(LANE, LANE, "/tmp/x")],
        frame_key=FRAME_KEY, ttl_s=42)
    session = backend.open_worker_session()
    assert session.expires_at == 1_000.0 + 42


def test_outbox_counts_pasa_la_medida_del_backend_sin_envolverla():
    fake = FakeJournal()
    backend = J.JournalProjectionBackend(
        fake, "worker", [P.LedgerTarget(LANE, LANE, "/tmp/x")], frame_key=FRAME_KEY)
    counts = backend.outbox_counts(fake.open_session("worker", ttl_s=900))
    assert type(counts) is C.OutboxCounts
    assert (counts.lane, counts.pending, counts.failed) == (LANE, 3, 1)


def test_refresh_worker_session_exige_ttl_de_palabra_clave_y_usa_el_pedido():
    """Si el adaptador llamase `refresh_session(token, ttl_s)` posicional, el
    doble —con la MISMA forma keyword-only que el núcleo— lo rechazaría. Y el
    valor que viaja es el PEDIDO por el llamante, no un TTL propio del backend."""
    fake = FakeJournal()
    backend = J.JournalProjectionBackend(
        fake, "worker", [P.LedgerTarget(LANE, LANE, "/tmp/x")], frame_key=FRAME_KEY,
        ttl_s=55)  # deliberadamente distinto del ttl_s de refresh, de abajo
    session = fake.open_session("worker", ttl_s=900)
    renewed = backend.refresh_worker_session(session, ttl_s=77)
    assert renewed.expires_at == 2_000.0 + 77


def test_close_worker_session_traga_solo_autherror():
    fake = FakeJournal()
    backend = J.JournalProjectionBackend(
        fake, "worker", [P.LedgerTarget(LANE, LANE, "/tmp/x")], frame_key=FRAME_KEY)
    session = fake.open_session("worker", ttl_s=900)
    backend.close_worker_session(session)  # AuthError: no levanta
    assert fake.revoke_calls == [("tok-1", "projector_stop")]


def test_close_worker_session_NO_traga_otros_fallos():
    fake = _RompeAlRevocar()
    backend = J.JournalProjectionBackend(
        fake, "worker", [P.LedgerTarget(LANE, LANE, "/tmp/x")], frame_key=FRAME_KEY)
    session = fake.open_session("worker", ttl_s=900)
    with pytest.raises(RuntimeError):
        backend.close_worker_session(session)


# ══ selectores cerrados de backend y runner ═════════════════════════════════

def test_selector_disabled_no_construye_nada():
    assert J.projection_backend_for_mode("disabled") is None


def test_selector_active_exige_argumentos_completos():
    fake = FakeJournal()
    with pytest.raises(J.ProjectionBackendConfigurationError):
        J.projection_backend_for_mode("active", journal=fake)


def test_selector_active_construye_el_backend_real():
    fake = FakeJournal()
    backend = J.projection_backend_for_mode(
        "active", journal=fake, credential="worker",
        targets=[P.LedgerTarget(LANE, LANE, "/tmp/x")], frame_key=FRAME_KEY)
    assert isinstance(backend, J.JournalProjectionBackend)


def test_selector_modo_desconocido_falla_cerrado():
    with pytest.raises(J.ProjectionBackendConfigurationError):
        J.projection_backend_for_mode("bridge")


def test_selector_runner_active_exige_backend_y_carril():
    with pytest.raises(PR.RunnerConfigurationError):
        PR.projector_runner_for_mode("active")
    fake = FakeJournal()
    backend = J.JournalProjectionBackend(
        fake, "worker", [P.LedgerTarget(LANE, LANE, "/tmp/x")],
        frame_key=FRAME_KEY)
    runner = PR.projector_runner_for_mode(
        "active", backend=backend, lane=LANE)
    assert isinstance(runner, PR.ActiveProjectorRunner)


@pytest.mark.parametrize("mode", ("enabled", "on", "", True, None))
def test_selector_runner_rechaza_modos_hostiles(mode):
    with pytest.raises(PR.RunnerConfigurationError):
        PR.projector_runner_for_mode(mode)


# ══ ciclo de parada y lectura de disponibilidad ═════════════════════════════

class _RunnerFalso:
    def __init__(self, quiesce_error=None, stop_error=None):
        self.quiesce_calls = 0
        self.stop_calls = 0
        self.snapshot_calls = 0
        self._quiesce_error = quiesce_error
        self._stop_error = stop_error

    def quiesce(self, *, timeout_s):
        self.quiesce_calls += 1
        assert timeout_s == 3.0
        if self._quiesce_error is not None:
            raise self._quiesce_error
        return self.snapshot()

    def stop(self):
        self.stop_calls += 1
        if self._stop_error is not None:
            raise self._stop_error

    def snapshot(self):
        self.snapshot_calls += 1
        return _snapshot()


def _snapshot(**over):
    base = dict(state=PR.RunnerState.IDLE, required=True, thread_alive=True,
                accepting_claims=True, in_flight=False, outbox_certifiable=False,
                pending=0, failed=0, fatal_code=None, session_open=True)
    base.update(over)
    return PR.RunnerSnapshot(**base)


def test_graceful_shutdown_llama_quiesce_antes_que_stop():
    runner = _RunnerFalso()
    snapshot = J.graceful_shutdown(runner, timeout_s=3.0)
    assert runner.quiesce_calls == 1
    assert runner.stop_calls == 1
    assert snapshot.state is PR.RunnerState.IDLE


def test_graceful_shutdown_intenta_stop_aunque_quiesce_falle():
    runner = _RunnerFalso(quiesce_error=RuntimeError("quiesce roto"))
    with pytest.raises(RuntimeError, match="quiesce roto"):
        J.graceful_shutdown(runner, timeout_s=3.0)
    assert runner.stop_calls == 1, "stop() tiene que intentarse igual"


def test_graceful_shutdown_preserva_los_dos_fallos_si_ambos_fallan():
    runner = _RunnerFalso(
        quiesce_error=RuntimeError("quiesce roto"),
        stop_error=RuntimeError("stop roto"))
    with pytest.raises(ExceptionGroup) as excinfo:
        J.graceful_shutdown(runner, timeout_s=3.0)
    mensajes = {str(e) for e in excinfo.value.exceptions}
    assert mensajes == {"quiesce roto", "stop roto"}


def test_graceful_shutdown_propaga_stop_si_solo_stop_falla():
    runner = _RunnerFalso(stop_error=RuntimeError("stop roto"))
    with pytest.raises(RuntimeError, match="stop roto"):
        J.graceful_shutdown(runner, timeout_s=3.0)
    assert runner.quiesce_calls == 1


def test_disabled_cuenta_como_listo_sin_participar():
    disabled = PR.DisabledProjectorRunner().snapshot()
    assert J.runner_counts_as_ready(disabled) is True


def test_fatal_no_cuenta_como_listo():
    fatal = _snapshot(state=PR.RunnerState.FATAL, fatal_code="RUNNER_INTERNAL_ERROR",
                      accepting_claims=False, session_open=True)
    assert J.runner_counts_as_ready(fatal) is False


def test_blocked_no_cuenta_como_listo_aunque_sea_backoff():
    blocked = _snapshot(state=PR.RunnerState.BLOCKED, pending=5, failed=2)
    assert blocked.thread_alive is True
    assert J.runner_counts_as_ready(blocked) is False


@pytest.mark.parametrize("estado", [
    PR.RunnerState.DRAINING, PR.RunnerState.STARTING, PR.RunnerState.NEW,
    PR.RunnerState.STOPPED,
])
def test_estados_fuera_del_conjunto_de_trabajo_no_cuentan_como_listos(estado):
    """Estados fuera del servicio normal nunca cuentan como listos."""
    fuera = _snapshot(state=estado, accepting_claims=False, thread_alive=False)
    assert J.runner_counts_as_ready(fuera) is False


def test_hebra_muerta_sin_fatal_no_cuenta_como_listo():
    colgado = _snapshot(state=PR.RunnerState.STARTING, thread_alive=False,
                        accepting_claims=False)
    assert J.runner_counts_as_ready(colgado) is False


def test_sesion_cerrada_no_cuenta_como_listo_aunque_el_estado_sea_bueno():
    """⊕ del conjunto: `state` correcto NO basta si `session_open` es falso —
    control positivo de que `runner_counts_as_ready` mira las CUATRO señales,
    no sólo el estado."""
    sin_sesion = _snapshot(state=PR.RunnerState.IDLE, session_open=False)
    assert J.runner_counts_as_ready(sin_sesion) is False


def test_ready_with_runner_es_and_estricto():
    listo = _snapshot()
    assert J.ready_with_runner(True, listo) is True
    assert J.ready_with_runner(False, listo) is False
    fatal = _snapshot(state=PR.RunnerState.FATAL, fatal_code="X",
                      accepting_claims=False, session_open=True)
    assert J.ready_with_runner(True, fatal) is False
