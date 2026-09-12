"""Production lifespan + real Journal: deadlines, lanes, restart and failure."""
from __future__ import annotations

import hashlib
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

import coordination as C
import runtime_root as R
import servicio
from tests.runtime.test_runtime_root import active_config, config, lightweight_legacy


def fleet_config(tmp_path, **changes):
    credentials, grants = {}, {}
    specs = {"deadline": (C.CAP_RUNTIME_OBSERVE,),
             "observer": (C.CAP_RUNTIME_OBSERVE, C.CAP_RUNTIME_READ),
             "operator": (C.CAP_ORGANIZATION_ACTIVATE,), "target": ()}
    for lane in ("a", "b"):
        for kind, caps in specs.items():
            principal = f"{kind}-{lane}"
            credentials[f"credential-{principal}"] = {
                "rol": "infra" if kind != "target" else "be",
                "carril": lane, "principal_id": principal}
            grants[principal] = caps
    values = dict(credential_map=credentials, capability_grants=grants,
                  lane_ledgers={"a": ("ledger-a",), "b": ("ledger-b",)},
                  fleet_deadline_principals=("deadline-a",),
                  fleet_stale_after_s=1, fleet_deadline_interval_s=1)
    values.update(changes)
    return config(tmp_path, **values)


def seed(journal):
    sessions = {f"{kind}-{lane}": journal.open_session(f"credential-{kind}-{lane}")
                for lane in ("a", "b") for kind in ("target", "observer", "operator")}
    for lane in ("a", "b"):
        target = sessions[f"target-{lane}"]
        journal.activate_organization(
            sessions[f"operator-{lane}"].token, revision=1, source_sha256=lane * 64,
            attestation_state="attested",
            roles=[{"role": "cto", "layer": 0, "policy_code": "STANDARD"},
                   {"role": "be", "layer": 1, "policy_code": "STANDARD"}],
            reports=[{"role": "be", "reports_to": "cto"}], reviewers=[], escalations=[],
            workloads=[{"workload_id": "be-01", "role": "be",
                        "principal_id": target.principal_id,
                        "runtime_instance": target.runtime_instance,
                        "credential_generation": target.generation}])
        journal.record_runtime_observation(
            sessions[f"observer-{lane}"].token, workload_id="be-01",
            runtime_instance=target.runtime_instance, idempotency_key="first",
            supervisor_seq=1, observation_kind="cycle_ack", reason_code="PROCESS_PRESENT")
    return sessions


def until(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(0.02)
    assert predicate(), "background worker did not reach the expected state"


def test_production_lifespan_schedules_only_the_configured_lane_and_stops_before_dispose(
        tmp_path, monkeypatch):
    lightweight_legacy(monkeypatch)
    app = R.build_app(fleet_config(tmp_path))
    journal = app.state.native_journal
    now = [time.time()]
    journal._clock = lambda: now[0]
    assert not (tmp_path / "coordination.sqlite").exists()
    with TestClient(app) as client:
        sessions = seed(journal)
        assert app.state.fleet_deadline_runner.ready
        now[0] += 3
        token = sessions["observer-a"].token
        until(lambda: journal.runtime_statuses(token)[0]["status"] == "stale")
        rows = client.get("/native/v1/runtimes", headers={
            "Authorization": f"Bearer {token}"}).json()
        assert rows[0]["status"] == "stale"
        assert rows[0]["receipt_id"]
        assert journal.runtime_statuses(sessions["observer-b"].token)[0]["status"] == "fresh"
        thread = app.state.fleet_deadline_runner._thread
    assert thread is not None and not thread.is_alive()
    assert not app.state.fleet_deadline_runner.ready


def test_restart_before_deadline_does_not_create_transition(tmp_path, monkeypatch):
    lightweight_legacy(monkeypatch)
    cfg = fleet_config(tmp_path, fleet_stale_after_s=300)
    first = R.build_app(cfg)
    with TestClient(first):
        journal = first.state.native_journal
        sessions = seed(journal)
        before = journal._connect().execute(
            "SELECT COUNT(*) FROM runtime_status_transitions").fetchone()[0]
    second = R.build_app(cfg)
    with TestClient(second):
        journal = second.state.native_journal
        assert journal._connect().execute(
            "SELECT COUNT(*) FROM runtime_status_transitions").fetchone()[0] == before
        assert journal.runtime_statuses(sessions["observer-a"].token)[0]["status"] == "fresh"


def test_deadline_failure_turns_readiness_red_and_does_not_log_secrets(tmp_path, monkeypatch, caplog):
    lightweight_legacy(monkeypatch)
    cfg = active_config(tmp_path)
    credentials = dict(cfg.credential_map)
    credentials["private-deadline-credential"] = {
        "rol": "infra", "carril": "llminbox", "principal_id": "deadlines"}
    grants = dict(cfg.capability_grants, deadlines=(C.CAP_RUNTIME_OBSERVE,))
    from dataclasses import replace
    app = R.build_app(replace(cfg, credential_map=credentials, capability_grants=grants,
                              fleet_deadline_principals=("deadlines",),
                              fleet_deadline_interval_s=1))
    journal = app.state.native_journal
    monkeypatch.setattr(R, "_coordination_ready", lambda _: True)
    monkeypatch.setattr(journal, "admission_ready", lambda _: True)
    monkeypatch.setattr(R.JPB, "ready_with_runner", lambda *_: True)
    with TestClient(app) as client:
        assert client.get("/ready").status_code == 200
        def failed(*_, **__):
            raise RuntimeError("private-deadline-credential")
        monkeypatch.setattr(journal, "evaluate_runtime_deadlines", failed)
        until(lambda: not app.state.fleet_deadline_runner.ready)
        assert client.get("/ready").status_code == 503
        assert client.get("/health").json()["ok"] is False
        assert client.get("/live").status_code == 200
    assert "private-deadline-credential" not in caplog.text


@pytest.mark.parametrize("changes", [
    {"fleet_deadline_principals": ["deadline-a"]},
    {"fleet_deadline_principals": ("deadline-a", "deadline-a")},
    {"fleet_deadline_principals": ("unknown",)},
    {"fleet_deadline_principals": ("observer-a",)},
    {"fleet_deadline_principals": (None,)},
    {"fleet_deadline_principals": (" deadline-a",)},
    {"fleet_deadline_interval_s": 0}, {"fleet_deadline_interval_s": True},
    {"fleet_deadline_interval_s": 61}, {"fleet_stale_after_s": 0},
    {"fleet_stale_after_s": True}, {"fleet_stale_after_s": 31 * 86400 + 1},
])
def test_invalid_deadline_configuration_fails_before_journal_initialization(tmp_path, changes):
    with pytest.raises(R.RuntimeConfigurationError):
        R.build_app(fleet_config(tmp_path, **changes))
    assert not (tmp_path / "coordination.sqlite").exists()


def test_environment_accepts_fleet_grants_and_explicit_deadline_principals(tmp_path, monkeypatch):
    monkeypatch.setattr(R.lp, "canon_identidad", lambda value: value)
    monkeypatch.setattr(R.lp, "rol_de", lambda value: value)
    raw = json.dumps({"credential-deadline": {
        "principal": "deadlines", "rol": "infra", "carril": "a",
        "capacidades": ["session", "runtime.observe"]}}).encode()
    pepper = tmp_path / "pepper"
    pepper.write_bytes(b"p" * 48)
    pepper.chmod(0o600)
    monkeypatch.setattr(servicio, "_BYTES_MAPA", [raw])
    monkeypatch.setattr(servicio, "CARRIL_LEDGER", {"a": "ledger-a"})
    monkeypatch.setenv("LLMINBOX_JOURNAL", str(tmp_path / "journal.sqlite"))
    monkeypatch.setenv("LLMINBOX_CREDENCIALES_SHA", hashlib.sha256(raw).hexdigest())
    monkeypatch.setenv("LLMINBOX_PEPPER_FILE", str(pepper))
    monkeypatch.setenv("LLMINBOX_PROJECTOR_MODE", "disabled")
    monkeypatch.setenv("LLMINBOX_FLEET_DEADLINE_PRINCIPALS", '["deadlines"]')
    monkeypatch.setenv("LLMINBOX_FLEET_STALE_AFTER_S", "42")
    monkeypatch.setenv("LLMINBOX_FLEET_DEADLINE_INTERVAL_S", "2")
    cfg = R.RuntimeConfig.from_environment()
    assert cfg.fleet_deadline_principals == ("deadlines",)
    assert cfg.fleet_stale_after_s == 42 and cfg.fleet_deadline_interval_s == 2
    assert cfg.capability_grants["deadlines"] == (C.CAP_RUNTIME_OBSERVE,)
    assert R.build_app(cfg).state.fleet_deadline_runner.enabled


@pytest.mark.parametrize("capability, expected", [
    ("runtime.observe", C.CAP_RUNTIME_OBSERVE), ("runtime.recover", C.CAP_RUNTIME_RECOVER),
    ("runtime.read", C.CAP_RUNTIME_READ), ("organization.read", C.CAP_ORGANIZATION_READ),
    ("organization.activate", C.CAP_ORGANIZATION_ACTIVATE),
])
def test_fleet_grants_do_not_imply_other_capabilities(monkeypatch, capability, expected):
    monkeypatch.setattr(R.lp, "canon_identidad", lambda value: value)
    monkeypatch.setattr(R.lp, "rol_de", lambda value: value)
    _, grants = R._native_v8_configuration(json.dumps({"credential": {
        "principal": "observer", "rol": "infra", "carril": "a",
        "capacidades": ["session", capability]}}).encode())
    assert grants == {"observer": (expected,)}
