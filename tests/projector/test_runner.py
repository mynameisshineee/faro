from __future__ import annotations

import threading

import pytest

import projector_runner as R


def test_selector_devuelve_runner_disabled():
    runner = R.projector_runner_for_mode("disabled")
    assert isinstance(runner, R.DisabledProjectorRunner)


def test_disabled_no_crea_hebra_ni_necesita_dependencias():
    before_threads = tuple(thread.ident for thread in threading.enumerate())
    runner = R.DisabledProjectorRunner()
    runner.start()
    runner.drain(timeout_s=0)
    runner.stop()
    assert tuple(thread.ident for thread in threading.enumerate()) == before_threads


def test_disabled_no_publica_cero_como_outbox_drenado():
    snapshot = R.DisabledProjectorRunner().snapshot()
    assert snapshot.outbox_certifiable is False
    assert snapshot.pending is None and snapshot.failed is None
    assert snapshot.in_flight is False and snapshot.accepting_claims is False


def test_disabled_nunca_emite_permiso_de_rollback():
    with pytest.raises(R.RollbackNotCertifiable):
        R.DisabledProjectorRunner().certify_rollback()


def test_disabled_es_idempotente_y_terminal():
    runner = R.DisabledProjectorRunner()
    expected = runner.snapshot()
    for _ in range(3):
        runner.start()
        assert runner.drain(timeout_s=0.01) == expected
        runner.stop()
        assert runner.snapshot() == expected
        assert runner.snapshot().state is R.RunnerState.DISABLED


@pytest.mark.parametrize(
    "changes",
    [
        {"required": True},
        {"pending": 0},
        {"thread_alive": True},
        {"outbox_certifiable": True},
    ],
)
def test_snapshot_disabled_rechaza_actividad_o_ceros_inventados(changes):
    values = dict(
        state=R.RunnerState.DISABLED,
        required=False,
        thread_alive=False,
        accepting_claims=False,
        in_flight=False,
        outbox_certifiable=False,
        pending=None,
        failed=None,
        fatal_code=None,
    )
    values.update(changes)
    with pytest.raises(ValueError):
        R.RunnerSnapshot(**values)


@pytest.mark.parametrize(
    "changes",
    [
        {"accepting_claims": True},
        {"in_flight": True},
        {"pending": None},
        {"failed": None},
        {"pending": 1},
        {"failed": 1},
    ],
)
def test_snapshot_certificable_exige_drenado_y_contadores_a_cero(changes):
    values = dict(
        state=R.RunnerState.IDLE,
        required=True,
        thread_alive=True,
        accepting_claims=False,
        in_flight=False,
        outbox_certifiable=True,
        pending=0,
        failed=0,
        fatal_code=None,
    )
    values.update(changes)
    with pytest.raises(ValueError):
        R.RunnerSnapshot(**values)


@pytest.mark.parametrize(
    "mode", ["", "active", "native-required", " disabled", None, True],
)
def test_modo_desconocido_no_degrada_a_disabled(mode):
    with pytest.raises(R.RunnerConfigurationError):
        R.projector_runner_for_mode(mode)
