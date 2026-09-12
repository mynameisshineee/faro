"""Falsadores focales de durable_v=7 (ADR-002).

El primer run autorizado (2026-09-08, e13581f) encontró 13 fallos.
Los correctivos requieren recibo de ejecución sobre su propio SHA exacto.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import os
import sqlite3
import threading

import pytest

import coordination as C
from ._arnes import CAPS_RUNTIME, GRAMATICA, INTENT, LANES, PEPPER


V7_ONLY = (
    "runtime_status", "runtime_status_transitions", "runtime_observations",
    "runtime_recoveries", "expected_workloads", "organization_escalations",
    "organization_reviewers", "organization_reports", "organization_roles",
    "organization_revisions",
)
V7_INDEXES = (
    "i_runtime_observation_target", "u_expected_runtime", "u_org_active_lane",
    "u_runtime_lane_generation", "u_runtime_lane", "u_principal_lane",
    "u_runtime_lane_principal", "u_runtime_lane_principal_generation",
    "u_command_lane",
)
RECEIPTS_V6 = """
CREATE TABLE receipts (
  receipt_id TEXT PRIMARY KEY,
  subject_kind TEXT NOT NULL CHECK (subject_kind IN
    ('event','command','denial','denial_aggregate')),
  subject_id TEXT NOT NULL,
  principal_id TEXT REFERENCES principals(principal_id),
  lane TEXT,
  current_state TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (subject_kind, subject_id))
"""


def _journal(path):
    return C.Journal(str(path), pepper=PEPPER, lane_ledgers=LANES,
                     recipient_resolver=lambda value: (value, False),
                     grammar=GRAMATICA)


def _fixture_v6(tmp_path):
    """Materializa una v6 literal desde la forma v7 y quita sólo lo aditivo."""
    path = tmp_path / "coordination.sqlite"
    j = _journal(path)
    assert j.initialize() == 7
    con = j._connect()
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("PRAGMA legacy_alter_table=ON")
    for table in V7_ONLY:
        con.execute(f"DROP TABLE {table}")
    for index in V7_INDEXES:
        con.execute(f"DROP INDEX IF EXISTS {index}")
    con.execute("ALTER TABLE receipts RENAME TO receipts_v7")
    con.execute(RECEIPTS_V6)
    con.execute(
        "INSERT INTO receipts SELECT * FROM receipts_v7")
    con.execute("DROP TABLE receipts_v7")
    con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('durable_v','6')")
    con.execute("PRAGMA legacy_alter_table=OFF")
    con.execute("PRAGMA foreign_keys=ON")
    j.close()
    return path


def _raw(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def _sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


@pytest.mark.parametrize("table,column", [
    ("events", "recipients_roles"),
    ("events", "recipients_broadcast"),
    ("leases", "resource_literal"),
])
def test_v6_admite_nullable_historico_y_rechaza_not_null_ajeno(tmp_path, table, column):
    """La v6 real pasa; cambiar su nulabilidad debe seguir fallando cerrado."""
    from .test_manifiesto_v3 import _reescribe_ddl

    path = _fixture_v6(tmp_path)
    con = _raw(path)
    try:
        assert C._clasificar(con) == ("conocida", 6, "")
        info = {r[1]: r for r in con.execute(f"PRAGMA table_info({table})")}
        assert info[column][3] == 0
    finally:
        con.close()
    _reescribe_ddl(path, table, f"{column} TEXT", f"{column} TEXT NOT NULL")
    before = _sha(path)
    j = _journal(path)
    try:
        with pytest.raises(C.SchemaIndeterminate, match=column):
            j.initialize()
    finally:
        j.close()
    assert _sha(path) == before, "el rechazo de forma no puede mutar el original"


def test_v6_a_v7_requiere_snapshot_retenido_digest_verificado_y_preserva_recibos(tmp_path):
    path = _fixture_v6(tmp_path)
    con = _raw(path)
    con.execute(
        "INSERT INTO principals VALUES('p','p','be','llminbox','2026-09-07T00:00:00Z')")
    con.execute(
        "INSERT INTO receipts VALUES('r','event','e','p','llminbox','accepted',"
        "'2026-09-07T00:00:00Z','2026-09-07T00:00:00Z')")
    con.commit()
    con.close()

    j = _journal(path)
    assert j.initialize() == 7
    snapshot = j.migration_snapshot()
    assert snapshot is not None and snapshot.source_durable_v == 6
    assert os.path.exists(snapshot.path)
    assert _sha(snapshot.path) == snapshot.sha256
    snap = _raw(snapshot.path)
    assert snap.execute("SELECT v FROM meta WHERE k='durable_v'").fetchone()[0] == "6"
    assert snap.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1
    snap.close()
    assert j._connect().execute(
        "SELECT subject_kind FROM receipts WHERE receipt_id='r'").fetchone()[0] == "event"
    assert j.stored_durable_v() == 7


def test_v6_no_puede_sellarse_v7_si_falla_la_fotografia(tmp_path, monkeypatch):
    path = _fixture_v6(tmp_path)
    j = _journal(path)
    monkeypatch.setattr(
        j, "_retain_pre_v7_snapshot_locked",
        lambda _version: (_ for _ in ()).throw(
            C.MigrationSnapshotRequired("falsador: snapshot indisponible")))
    with pytest.raises(C.MigrationSnapshotRequired):
        j.initialize()
    con = _raw(path)
    assert con.execute("SELECT v FROM meta WHERE k='durable_v'").fetchone()[0] == "6"
    con.close()


def test_lock_sh_del_writer_se_serializa_antes_de_snapshot_y_commit_v7(tmp_path):
    """Orden contractual: writer `_Tx` toma SH; migración toma EX antes de foto.

    El SH manual representa exactamente `_cerrojo_escritor()` mantenido hasta
    COMMIT. Mientras vive, `_cerrojo_ciclo()` no puede llegar al snapshot. La
    fila commiteada queda tanto en la foto rollback como en v7: no existe el
    brazo «vive en v7 pero se pierde al restaurar».
    """
    path = _fixture_v6(tmp_path)
    lock_fd = os.open(str(path) + ".lifecycle", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_SH)
    j = _journal(path)
    started = threading.Event()
    snapshot_reached = threading.Event()
    original = j._retain_pre_v7_snapshot_locked

    def wrapped(version):
        snapshot_reached.set()
        return original(version)

    j._retain_pre_v7_snapshot_locked = wrapped
    outcome = []

    def migrate():
        started.set()
        try:
            outcome.append(j.initialize())
        except BaseException as exc:
            outcome.append(exc)

    thread = threading.Thread(target=migrate)
    thread.start()
    try:
        assert started.wait(1)
        assert not snapshot_reached.is_set(), "EX atravesó el SH del writer"
        con = _raw(path)
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            "INSERT INTO principals VALUES('writer','writer','be','llminbox',"
            "'2026-09-07T00:00:00Z')")
        con.execute("COMMIT")
        con.close()
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    thread.join(timeout=5)
    assert not thread.is_alive() and outcome == [7]
    assert snapshot_reached.is_set()
    snapshot = j.migration_snapshot()
    snap = _raw(snapshot.path)
    assert snap.execute(
        "SELECT COUNT(*) FROM principals WHERE principal_id='writer'").fetchone()[0] == 1
    snap.close()
    assert j._connect().execute(
        "SELECT COUNT(*) FROM principals WHERE principal_id='writer'").fetchone()[0] == 1


def test_base_nueva_v7_no_inventa_snapshot_previo(tmp_path):
    path = tmp_path / "coordination.sqlite"
    j = _journal(path)
    assert j.initialize() == 7
    assert j.migration_snapshot() is None


@pytest.mark.parametrize("partial_kind", ["table", "index", "column"])
def test_v6_con_forma_v7_parcial_falla_cerrado_sin_sello(tmp_path, partial_kind):
    path = _fixture_v6(tmp_path)
    con = _raw(path)
    if partial_kind == "table":
        con.execute("CREATE TABLE runtime_status(x TEXT)")
    elif partial_kind == "index":
        con.execute("CREATE UNIQUE INDEX u_principal_lane ON principals(lane,principal_id)")
    else:
        con.execute("ALTER TABLE principals ADD COLUMN future_field TEXT")
    con.commit()
    con.close()
    with pytest.raises((C.SchemaIndeterminate, C.MigrationFailed)):
        _journal(path).initialize()
    con = _raw(path)
    assert con.execute("SELECT v FROM meta WHERE k='durable_v'").fetchone()[0] == "6"
    con.close()


def _fleet(tmp_path, *, second_lane=False):
    path = tmp_path / "coordination.sqlite"
    j = _journal(path)
    j.initialize()
    credentials = [
        ("org", "org", "cto", "llminbox",
         (C.CAP_ORGANIZATION_ACTIVATE, C.CAP_ORGANIZATION_READ,
          C.CAP_ADMISSION_OPERATOR)),
        ("sup", "sup", "infra", "llminbox",
         (C.CAP_RUNTIME_OBSERVE, C.CAP_RUNTIME_RECOVER, C.CAP_RUNTIME_READ)),
        ("worker", "worker", "infra", "llminbox", (C.CAP_COMMAND_WORKER,)),
        ("target", "target", "be", "llminbox", ()),
    ]
    if second_lane:
        credentials.append(("other", "other", "be", "carril-dos", ()))
    bindings = {}
    for cred, principal, role, lane, capabilities in credentials:
        bindings[cred] = j.bind_credential(
            cred, principal=principal, role=role, lane=lane,
            capabilities=capabilities)
    sessions = {cred: j.open_session(cred) for cred, *_ in credentials}
    j.activate_organization(
        sessions["org"].token, revision=1, source_sha256="a" * 64,
        attestation_state="attested",
        roles=[{"role": "cto", "layer": 0, "policy_code": "STANDARD"},
               {"role": "infra", "layer": 1, "policy_code": "STANDARD"},
               {"role": "be", "layer": 2, "policy_code": "REVIEW_REQUIRED"}],
        reports=[{"role": "infra", "reports_to": "cto"},
                 {"role": "be", "reports_to": "infra"}],
        reviewers=[{"role": "be", "reviewer_role": "infra"}],
        escalations=[{"role": "be", "trigger_code": "BLOCKED",
                      "target_role": "infra"}],
        workloads=[{"workload_id": "be-01", "role": "be",
                    "principal_id": bindings["target"].principal_id,
                    "runtime_instance": sessions["target"].runtime_instance,
                    "credential_generation": sessions["target"].generation}])
    return j, bindings, sessions


def _observe(j, token, *, key="obs-1", seq=1, kind="cycle_ack",
             reason="PROCESS_PRESENT"):
    return j.record_runtime_observation(
        token, workload_id="be-01",
        runtime_instance=j._connect().execute(
            "SELECT w.runtime_instance FROM expected_workloads w"
            " JOIN organization_revisions o ON o.lane=w.lane"
            " AND o.revision=w.organization_revision"
            " WHERE w.lane='llminbox' AND w.workload_id='be-01'"
            " AND o.active=1").fetchone()[0],
        idempotency_key=key, supervisor_seq=seq,
        observation_kind=kind, reason_code=reason,
        heartbeat_age_ms=0)


def test_observation_replay_conflict_seq_y_un_recibo_por_transicion(tmp_path):
    j, _, sessions = _fleet(tmp_path)
    first = _observe(j, sessions["sup"].token)
    replay = _observe(j, sessions["sup"].token)
    assert replay.replayed and replay.observation_id == first.observation_id
    assert replay.transition_id == first.transition_id
    confirming = _observe(j, sessions["sup"].token, key="obs-confirm", seq=2)
    assert confirming.transition_id is None and confirming.receipt_id is None
    with pytest.raises(C.IdempotencyConflict):
        _observe(j, sessions["sup"].token, kind="exited", reason="PROCESS_EXITED")
    with pytest.raises(C.ObservationSequenceConflict):
        _observe(j, sessions["sup"].token, key="obs-regressive", seq=1)
    con = j._connect()
    assert con.execute("SELECT COUNT(*) FROM runtime_observations").fetchone()[0] == 2
    transition = con.execute(
        "SELECT * FROM runtime_status_transitions WHERE observation_id=?",
        (first.observation_id,)).fetchone()
    receipt = con.execute(
        "SELECT subject_kind,subject_id FROM receipts WHERE receipt_id=?",
        (transition["receipt_id"],)).fetchone()
    assert tuple(receipt) == ("transition", transition["transition_id"])
    assert con.execute(
        "SELECT COUNT(*) FROM runtime_status_transitions WHERE receipt_id=?",
        (transition["receipt_id"],)).fetchone()[0] == 1


def test_absent_admite_runtime_no_observado_y_timeout_es_stale_no_stopped(tmp_path):
    j, _, sessions = _fleet(tmp_path)
    initial = j.runtime_statuses(sessions["sup"].token)[0]
    assert initial["status"] == "absent"
    assert initial["runtime_instance"] == sessions["target"].runtime_instance
    first = _observe(j, sessions["sup"].token)
    assert first.status == "fresh"
    base = j._clock()
    j._clock = lambda: base + 10
    assert j.evaluate_runtime_deadlines(
        sessions["sup"].token, stale_after_s=5) == 1
    assert j.runtime_statuses(sessions["sup"].token)[0]["status"] == "stale"
    exited = _observe(
        j, sessions["sup"].token, key="obs-exit", seq=2,
        kind="exited", reason="PROCESS_EXITED")
    assert exited.status == "stopped"


def test_nueva_revision_deriva_principal_y_limpia_observacion_de_generacion_anterior(tmp_path):
    j, bindings, sessions = _fleet(tmp_path)
    _observe(j, sessions["sup"].token)
    j.activate_organization(
        sessions["org"].token, revision=2, source_sha256="c" * 64,
        attestation_state="attested",
        roles=[{"role": "cto", "layer": 0, "policy_code": "STANDARD"},
               {"role": "infra", "layer": 1, "policy_code": "STANDARD"},
               {"role": "be", "layer": 2, "policy_code": "STANDARD"}],
        reports=[{"role": "infra", "reports_to": "cto"},
                 {"role": "be", "reports_to": "infra"}],
        reviewers=[], escalations=[],
        workloads=[{"workload_id": "be-01", "role": "be",
                    "principal_id": None,
                    "runtime_instance": sessions["target"].runtime_instance,
                    "credential_generation": sessions["target"].generation}])
    row = j._connect().execute(
        "SELECT * FROM runtime_status WHERE workload_id='be-01'").fetchone()
    assert row["principal_id"] == bindings["target"].principal_id
    assert row["status"] == "absent" and row["last_observed_at"] is None


def test_dos_journals_no_asignan_la_misma_seq_del_supervisor(tmp_path):
    j1, _, sessions = _fleet(tmp_path)
    j2 = _journal(tmp_path / "coordination.sqlite")
    j2.initialize()
    barrier = threading.Barrier(2)
    j1._gancho_carrera = j2._gancho_carrera = lambda: barrier.wait(timeout=2)

    def run(j, key):
        try:
            return _observe(j, sessions["sup"].token, key=key, seq=9)
        except Exception as exc:  # el tipo exacto se afirma abajo
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda args: run(*args),
                                [(j1, "race-a"), (j2, "race-b")]))
    assert sum(isinstance(x, C.RuntimeObservation) for x in results) == 1
    assert sum(isinstance(x, C.ObservationSequenceConflict) for x in results) == 1


def test_org_rechaza_ciclo_desconocido_y_principal_cross_lane_atomicamente(tmp_path):
    j, bindings, sessions = _fleet(tmp_path, second_lane=True)
    base = j._connect().execute(
        "SELECT COUNT(*) FROM organization_revisions").fetchone()[0]
    common = dict(
        token=sessions["org"].token, revision=2, source_sha256="b" * 64,
        attestation_state="attested",
        roles=[{"role": "cto", "layer": 0, "policy_code": "STANDARD"},
               {"role": "be", "layer": 1, "policy_code": "STANDARD"}],
        reviewers=[], escalations=[],
        workloads=[{"workload_id": "be-01", "role": "be",
                    "principal_id": bindings["other"].principal_id,
                    "runtime_instance": None, "credential_generation": None}])
    with pytest.raises(C.OrganizationConflict):
        j.activate_organization(
            **common, reports=[{"role": "cto", "reports_to": "be"},
                               {"role": "be", "reports_to": "cto"}])
    with pytest.raises(C.OrganizationConflict):
        j.activate_organization(
            **{**common, "revision": 3},
            reports=[{"role": "be", "reports_to": "missing"}])
    with pytest.raises(C.OrganizationConflict):
        j.activate_organization(
            **{**common, "revision": 4}, reports=[])
    with pytest.raises(C.OrganizationConflict):
        j.activate_organization(
            **{**common, "revision": 5},
            reports=[{"role": "be", "reports_to": "cto"}])
    with pytest.raises(C.OrganizationConflict):
        j.activate_organization(
            **{**common, "revision": 6,
               "workloads": [{"workload_id": "be-01", "role": "be",
                              "principal_id": bindings["sup"].principal_id,
                              "runtime_instance": None,
                              "credential_generation": None}]},
            reports=[{"role": "be", "reports_to": "cto"}])
    assert j._connect().execute(
        "SELECT COUNT(*) FROM organization_revisions").fetchone()[0] == base


def test_recovery_es_un_command_idempotente_y_fenced_sin_segunda_maquina(tmp_path):
    j, _, sessions = _fleet(tmp_path)
    lease = j.acquire_lease(sessions["sup"].token, "runtime/be-01")
    target = j._connect().execute(
        "SELECT runtime_instance FROM expected_workloads"
        " WHERE workload_id='be-01'").fetchone()[0]
    first = j.request_runtime_recovery(
        sessions["sup"].token, workload_id="be-01", runtime_instance=target,
        idempotency_key="recover-1", reason_code="OPERATOR_REQUESTED",
        action_code="RESTART_RUNTIME", fenced_resource="runtime/be-01",
        fencing_token=lease.fencing_token)
    replay = j.request_runtime_recovery(
        sessions["sup"].token, workload_id="be-01", runtime_instance=target,
        idempotency_key="recover-1", reason_code="OPERATOR_REQUESTED",
        action_code="RESTART_RUNTIME", fenced_resource="runtime/be-01",
        fencing_token=lease.fencing_token)
    assert replay.replayed and replay.command_id == first.command_id
    con = j._connect()
    assert con.execute("SELECT COUNT(*) FROM runtime_recoveries").fetchone()[0] == 1
    assert con.execute(
        "SELECT state FROM commands WHERE command_id=?",
        (first.command_id,)).fetchone()[0] == "accepted"
    assert "state" not in {row[1] for row in con.execute(
        "PRAGMA table_info(runtime_recoveries)")}
    with pytest.raises(C.RecoveryConflict):
        j.request_runtime_recovery(
            sessions["sup"].token, workload_id="be-01", runtime_instance=target,
            idempotency_key="recover-before-outcome", reason_code="STOPPED_RUNTIME",
            action_code="RESTART_RUNTIME", fenced_resource="runtime/be-01",
            fencing_token=lease.fencing_token)
    j.advance_runtime_recovery(sessions["worker"].token, first.command_id, "received",
                               workload_id=first.workload_id,
                               runtime_instance=first.runtime_instance)
    j.advance_runtime_recovery(sessions["worker"].token, first.command_id, "executing",
                               workload_id=first.workload_id,
                               runtime_instance=first.runtime_instance)
    outcome = j.record_runtime_observation(
        sessions["sup"].token, workload_id="be-01", runtime_instance=target,
        idempotency_key="outcome-1", supervisor_seq=1,
        observation_kind="recovery_succeeded", reason_code="RECOVERY_SUCCEEDED",
        recovery_command_id=first.command_id)
    assert outcome.status == "fresh"
    assert con.execute("SELECT state FROM commands WHERE command_id=?",
                       (first.command_id,)).fetchone()[0] == "succeeded"
    replay_outcome = j.record_runtime_observation(
        sessions["sup"].token, workload_id="be-01", runtime_instance=target,
        idempotency_key="outcome-1", supervisor_seq=1,
        observation_kind="recovery_succeeded", reason_code="RECOVERY_SUCCEEDED",
        recovery_command_id=first.command_id)
    assert replay_outcome.replayed
    assert replay_outcome.observation_id == outcome.observation_id
    before_contradiction = con.execute(
        "SELECT COUNT(*) FROM runtime_observations").fetchone()[0]
    with pytest.raises(C.RecoveryConflict):
        j.record_runtime_observation(
            sessions["sup"].token, workload_id="be-01", runtime_instance=target,
            idempotency_key="outcome-contradiction", supervisor_seq=2,
            observation_kind="recovery_failed", reason_code="RECOVERY_FAILED",
            recovery_command_id=first.command_id)
    assert con.execute(
        "SELECT COUNT(*) FROM runtime_observations").fetchone()[0] \
        == before_contradiction
    second = j.request_runtime_recovery(
        sessions["sup"].token, workload_id="be-01", runtime_instance=target,
        idempotency_key="recover-after-outcome", reason_code="STOPPED_RUNTIME",
        action_code="RESTART_RUNTIME", fenced_resource="runtime/be-01",
        fencing_token=lease.fencing_token)
    assert second.command_id != first.command_id
    j.release_lease(sessions["sup"].token, "runtime/be-01")
    newer = j.acquire_lease(sessions["sup"].token, "runtime/be-01")
    assert newer.fencing_token > lease.fencing_token
    with pytest.raises(C.FencingConflict):
        j.request_runtime_recovery(
            sessions["sup"].token, workload_id="be-01", runtime_instance=target,
            idempotency_key="recover-stale-fence", reason_code="STOPPED_RUNTIME",
            action_code="RESTART_RUNTIME", fenced_resource="runtime/be-01",
            fencing_token=lease.fencing_token)


@pytest.mark.parametrize(
    "first_kind,first_reason,terminal,opposite_kind,opposite_reason",
    [("recovery_succeeded", "RECOVERY_SUCCEEDED", "succeeded",
      "recovery_failed", "RECOVERY_FAILED"),
     ("recovery_failed", "RECOVERY_FAILED", "failed",
      "recovery_succeeded", "RECOVERY_SUCCEEDED")])
def test_outcome_terminal_no_admite_segundo_outcome_opuesto(
        tmp_path, first_kind, first_reason, terminal,
        opposite_kind, opposite_reason):
    j, _, sessions = _fleet(tmp_path)
    lease = j.acquire_lease(sessions["sup"].token, "runtime/be-01")
    target = j._connect().execute(
        "SELECT runtime_instance FROM expected_workloads"
        " WHERE workload_id='be-01'").fetchone()[0]
    recovery = j.request_runtime_recovery(
        sessions["sup"].token, workload_id="be-01", runtime_instance=target,
        idempotency_key="recover", reason_code="OPERATOR_REQUESTED",
        action_code="RESTART_RUNTIME", fenced_resource="runtime/be-01",
        fencing_token=lease.fencing_token)
    j.advance_runtime_recovery(sessions["worker"].token, recovery.command_id, "received",
                               workload_id=recovery.workload_id,
                               runtime_instance=recovery.runtime_instance)
    j.advance_runtime_recovery(sessions["worker"].token, recovery.command_id, "executing",
                               workload_id=recovery.workload_id,
                               runtime_instance=recovery.runtime_instance)
    first = j.record_runtime_observation(
        sessions["sup"].token, workload_id="be-01", runtime_instance=target,
        idempotency_key="first-outcome", supervisor_seq=1,
        observation_kind=first_kind, reason_code=first_reason,
        recovery_command_id=recovery.command_id)
    con = j._connect()
    assert con.execute(
        "SELECT state FROM commands WHERE command_id=?",
        (recovery.command_id,)).fetchone()[0] == terminal
    assert con.execute(
        "SELECT current_state FROM receipts"
        " WHERE subject_kind='command' AND subject_id=?",
        (recovery.command_id,)).fetchone()[0] == terminal
    transition = con.execute(
        "SELECT observation_id,recovery_command_id FROM runtime_status_transitions"
        " WHERE transition_id=?", (first.transition_id,)).fetchone()
    assert tuple(transition) == (first.observation_id, recovery.command_id)
    before = con.execute(
        "SELECT COUNT(*) FROM runtime_observations").fetchone()[0]
    with pytest.raises(C.RecoveryConflict):
        j.record_runtime_observation(
            sessions["sup"].token, workload_id="be-01", runtime_instance=target,
            idempotency_key="opposite-outcome", supervisor_seq=2,
            observation_kind=opposite_kind, reason_code=opposite_reason,
            recovery_command_id=recovery.command_id)
    assert con.execute(
        "SELECT COUNT(*) FROM runtime_observations").fetchone()[0] == before
    assert con.execute(
        "SELECT state FROM commands WHERE command_id=?",
        (recovery.command_id,)).fetchone()[0] == terminal


def test_v7_no_tiene_columna_de_diagnostico_o_texto_libre_operacional(tmp_path):
    j, _, _ = _fleet(tmp_path)
    forbidden = {"detail", "diagnostic", "diagnostics", "message", "error", "prose"}
    for table in V7_ONLY:
        columns = {row[1] for row in j._connect().execute(
            f"PRAGMA table_info({table})")}
        assert not columns & forbidden
    ddl = "".join(row[0].lower() for row in j._connect().execute(
        "SELECT sql FROM sqlite_master WHERE type='table'"
        " AND name IN ('runtime_observations','runtime_status_transitions',"
        "'runtime_recoveries')") if row[0])
    assert "reason_code" in ddl and "check" in ddl


def test_rollback_restaura_exactamente_snapshot_v6_tras_sello_y_drenado(tmp_path):
    path = _fixture_v6(tmp_path)
    j = _journal(path)
    j.initialize()
    snapshot = j.migration_snapshot()
    j.bind_credential(
        "rollback", principal="rollback", role="infra", lane="llminbox",
        capabilities=(C.CAP_ADMISSION_OPERATOR,))
    session = j.open_session("rollback")
    for verb in C.ADMISSION_VERBS:
        j.seal_admission(
            session.token, verb, reason_code="DRAIN_FOR_ROLLBACK",
            expected_epoch=j.admission(session.token, verb).epoch)
    restored = j.restore_pre_v7_snapshot(
        session.token, expected_sha256=snapshot.sha256)
    assert restored.sha256 == snapshot.sha256
    assert _sha(path) == snapshot.sha256
    con = _raw(path)
    assert con.execute("SELECT v FROM meta WHERE k='durable_v'").fetchone()[0] == "6"
    con.close()


def test_rollback_db_wide_exige_consentimiento_y_drenado_de_todos_los_carriles(
        tmp_path, monkeypatch):
    path = _fixture_v6(tmp_path)
    j = _journal(path)
    j.initialize()
    snapshot = j.migration_snapshot()
    j.bind_credential(
        "op-a", principal="op-a", role="infra", lane="carril-uno",
        capabilities=(C.CAP_ADMISSION_OPERATOR,))
    j.bind_credential(
        "op-b", principal="op-b", role="infra", lane="carril-dos",
        capabilities=(C.CAP_ADMISSION_OPERATOR,))
    j.bind_credential(
        "writer-b", principal="writer-b", role="be", lane="carril-dos",
        capabilities=CAPS_RUNTIME)
    op_a = j.open_session("op-a")
    op_b = j.open_session("op-b")
    writer_b = j.open_session("writer-b")

    j.transition_admissions(
        op_a.token, target="sealed",
        expected_epochs={verb: 0 for verb in C.ADMISSION_VERBS},
        reason_code="DRAIN_FOR_ROLLBACK")

    original_replace = C.os.replace
    replacements = []

    def forbidden_replace(*args):
        replacements.append(args)
        raise AssertionError("un carril abierto no puede alcanzar os.replace")

    monkeypatch.setattr(C.os, "replace", forbidden_replace)
    # B ya tiene datos (principals/sessions), pero ninguna transición: el
    # `closed` implícito no constituye consentimiento DB-wide.
    with pytest.raises(C.AdmissionConflict):
        j.restore_pre_v7_snapshot(
            op_a.token, expected_sha256=snapshot.sha256)
    assert replacements == [] and j.stored_durable_v() == 7

    opened_b = j.transition_admissions(
        op_b.token, target="open",
        expected_epochs={verb: 0 for verb in C.ADMISSION_VERBS},
        reason_code="ROLLOUT")
    with pytest.raises(C.AdmissionConflict):
        j.restore_pre_v7_snapshot(
            op_a.token, expected_sha256=snapshot.sha256)
    assert replacements == [] and j.stored_durable_v() == 7

    accepted = j.accept_event(
        writer_b.token, idempotency_key="pending-b", intent=INTENT,
        ledger="ledger-dos")
    closed_b = j.transition_admissions(
        op_b.token, target="closed",
        expected_epochs={state.verb: state.epoch for state in opened_b},
        reason_code="DRAIN_FOR_ROLLBACK")
    monkeypatch.setattr(C.os, "replace", forbidden_replace)
    with pytest.raises(C.AdmissionConflict):
        j.restore_pre_v7_snapshot(
            op_a.token, expected_sha256=snapshot.sha256)
    assert replacements == [] and j.stored_durable_v() == 7
    assert j._connect().execute(
        "SELECT state FROM outbox WHERE event_id=?",
        (accepted.event_id,)).fetchone()[0] == "pending"
    monkeypatch.setattr(C.os, "replace", original_replace)

    job = j.claim_outbox(writer_b.token)
    j.mark_materialized(
        writer_b.token, accepted.event_id, entry_eid="e" * 64,
        ledger="ledger-dos", claim_token=job.claim_token)
    j.transition_admissions(
        op_b.token, target="sealed",
        expected_epochs={state.verb: state.epoch for state in closed_b},
        reason_code="DRAIN_FOR_ROLLBACK")
    # Defensa ante residuo/corrupción externa posterior al sello: con los dos
    # carriles consentidos, un pending de B sigue bloqueando el replace global.
    with j._tx() as con:
        con.execute("UPDATE outbox SET state='pending' WHERE event_id=?",
                    (accepted.event_id,))
    monkeypatch.setattr(C.os, "replace", forbidden_replace)
    with pytest.raises(C.AdmissionConflict):
        j.restore_pre_v7_snapshot(
            op_a.token, expected_sha256=snapshot.sha256)
    assert replacements == [] and j.stored_durable_v() == 7
    with j._tx() as con:
        con.execute("UPDATE outbox SET state='materialized' WHERE event_id=?",
                    (accepted.event_id,))
    monkeypatch.setattr(C.os, "replace", original_replace)
    restored = j.restore_pre_v7_snapshot(
        op_a.token, expected_sha256=snapshot.sha256)
    assert restored.sha256 == snapshot.sha256
    assert _sha(path) == snapshot.sha256


def test_recovery_command_no_avanza_por_advance_command(tmp_path):
    j, _, sessions = _fleet(tmp_path)
    lease = j.acquire_lease(sessions["sup"].token, "runtime/be-01")
    target = j._connect().execute(
        "SELECT runtime_instance FROM expected_workloads"
        " WHERE workload_id='be-01'").fetchone()[0]
    recovery = j.request_runtime_recovery(
        sessions["sup"].token, workload_id="be-01",
        runtime_instance=target, idempotency_key="recover",
        reason_code="STOPPED_RUNTIME", action_code="RESTART_RUNTIME",
        fenced_resource="runtime/be-01", fencing_token=lease.fencing_token)
    with pytest.raises(C.CommandTransitionInvalid):
        j.advance_command(sessions["worker"].token, recovery.command_id, "received")
    with pytest.raises(C.CommandTransitionInvalid):
        j.advance_command(sessions["worker"].token, recovery.command_id, "executing")


# ── Falsadores de `b6281b9` encargados por `@qa`
# (MARK:qa-b6281b9-un-invariante-de-seis-con-falsador-cero-ejecutados,
# 2026-09-07T15:55:08Z). ESCRITOS, NO EJECUTADOS — capacidad sigue NO-GO;
# ver cabecera del módulo. `@qa` audita, `db-migrations` (dueño del commit)
# escribe los suyos.
#
# F-FENCE-4 no tiene test propio a propósito, tal como especificó `@qa`: es
# una nota de contorno, no un falsador duro — el check de `canonical_resource`
# es de ALTA en `request_runtime_recovery` (ver ese test), no retroactivo, así
# que no hay fila previa al commit que pueda violarlo hoy.


_ROLES_STD = [{"role": "cto", "layer": 0, "policy_code": "STANDARD"},
              {"role": "infra", "layer": 1, "policy_code": "STANDARD"},
              {"role": "be", "layer": 2, "policy_code": "STANDARD"}]
_REPORTS_STD = [{"role": "infra", "reports_to": "cto"},
                {"role": "be", "reports_to": "infra"}]


def test_observation_rechaza_self_report_por_runtime_instance_igual(tmp_path, monkeypatch):
    """F-OBS-SELF-1 (I1, rama 1 del `or`): mismo `runtime_instance` basta solo.

    `activate_organization` exige que el `principal_id` declarado case con el
    dueño real del `runtime_instance`, así que "mismo runtime_instance" y
    "mismo principal" no se separan encadenando solo la API pública. Para
    acreditar esta rama SIN la otra —la advertencia explícita de `@qa`: "un
    test que sólo dispara F-OBS-SELF-1 no acredita F-OBS-SELF-2"— se sustituye
    sólo la vista leída del workload tras activar. Las FKs compuestas impiden
    fabricar esa asociación en la base real: este caso aísla la guarda de
    runtime, manteniendo íntegra la base del escenario.
    """
    j, bindings, sessions = _fleet(tmp_path)
    bindings["self_observer"] = j.bind_credential(
        "self_observer", principal="self_observer", role="be", lane="llminbox",
        capabilities=(C.CAP_RUNTIME_OBSERVE,))
    # El nuevo binding cambia la generación; reabrir la sesión del operador.
    sessions["org"] = j.open_session("org")
    session_self = j.open_session("self_observer")
    j.activate_organization(
        sessions["org"].token, revision=2, source_sha256="d" * 64,
        attestation_state="attested", roles=_ROLES_STD, reports=_REPORTS_STD,
        reviewers=[], escalations=[],
        workloads=[{"workload_id": "be-01", "role": "be",
                    "principal_id": bindings["self_observer"].principal_id,
                    "runtime_instance": session_self.runtime_instance,
                    "credential_generation": session_self.generation}])
    active_workload = j._active_workload_locked

    def different_principal_view(*args):
        workload = dict(active_workload(*args))
        assert workload["principal_id"] == bindings["self_observer"].principal_id
        workload["principal_id"] = bindings["sup"].principal_id
        return workload

    monkeypatch.setattr(j, "_active_workload_locked", different_principal_view)
    with pytest.raises(C.OperationInvalid):
        j.record_runtime_observation(
            session_self.token, workload_id="be-01",
            runtime_instance=session_self.runtime_instance,
            idempotency_key="self-1", supervisor_seq=1,
            observation_kind="cycle_ack", reason_code="PROCESS_PRESENT")


def test_observation_rechaza_self_report_por_principal_igual_aunque_runtime_distinto(
        tmp_path):
    """F-OBS-SELF-2 (I1, rama 2 del `or`): mismo principal por DOS sesiones
    distintas del mismo credential — la rama de `runtime_instance` no dispara
    (son instancias distintas), sólo la de `principal_id`."""
    j, bindings, sessions = _fleet(tmp_path)
    bindings["dual"] = j.bind_credential(
        "dual", principal="dual", role="be", lane="llminbox",
        capabilities=(C.CAP_RUNTIME_OBSERVE,))
    sessions["org"] = j.open_session("org")
    session_target = j.open_session("dual")
    session_observer = j.open_session("dual")
    j.activate_organization(
        sessions["org"].token, revision=2, source_sha256="e" * 64,
        attestation_state="attested", roles=_ROLES_STD, reports=_REPORTS_STD,
        reviewers=[], escalations=[],
        workloads=[{"workload_id": "be-01", "role": "be",
                    "principal_id": bindings["dual"].principal_id,
                    "runtime_instance": session_target.runtime_instance,
                    "credential_generation": session_target.generation}])
    with pytest.raises(C.OperationInvalid):
        j.record_runtime_observation(
            session_observer.token, workload_id="be-01",
            runtime_instance=session_target.runtime_instance,
            idempotency_key="dual-1", supervisor_seq=1,
            observation_kind="cycle_ack", reason_code="PROCESS_PRESENT")


def test_deadline_scoped_por_target_no_hereda_frescor_de_otra_generacion(tmp_path):
    """F-DEADLINE-GEN-1 (I2): `evaluate_runtime_deadlines` debe leer el
    `MAX(rowid)` SOLO del (target_runtime_instance,target_generation) activo.

    `activate_organization` resetea status/last_observed_at en CADA revisión
    (cubierto por
    `test_nueva_revision_deriva_principal_y_limpia_observacion_de_generacion_anterior`),
    así que el escenario que el scoping viejo dejaba pasar no es alcanzable
    encadenando sólo la API pública — un `workload_id` fresh/degraded SIEMPRE
    tiene, por construcción, su última observación real como la de mayor
    rowid. Se construye la fila "forastera" con SQL directo citando una tupla
    FK-válida real: la de la revisión 1 de `be-01`, que `activate_organization`
    no borra de `expected_workloads` al activar la 2 (sólo limpia
    `runtime_status`).

    `record_runtime_observation` es HOY el único escritor de `runtime_observations`
    en todo `coordination.py` y ya rechaza con `SubjectNotFound` cualquier intento
    de observar con el `runtime_instance` de una generación superada — por eso la
    fila forastera se fabrica por SQL en vez de reproducirse por la API. Este test
    es DEFENSIVO contra escritores futuros de esa tabla (p.ej. una herramienta de
    rescate/migración), NO la reproducción de una carrera alcanzable hoy con el
    único escritor actual.
    """
    j, bindings, sessions = _fleet(tmp_path)
    old_target = sessions["target"]
    first = _observe(j, sessions["sup"].token, key="obs-g1", seq=1)
    assert first.status == "fresh"
    base = j._clock()
    j._clock = lambda: base + 50
    new_target = j.open_session("target")
    j.activate_organization(
        sessions["org"].token, revision=2, source_sha256="f" * 64,
        attestation_state="attested", roles=_ROLES_STD, reports=_REPORTS_STD,
        reviewers=[], escalations=[],
        workloads=[{"workload_id": "be-01", "role": "be",
                    "principal_id": bindings["target"].principal_id,
                    "runtime_instance": new_target.runtime_instance,
                    "credential_generation": new_target.generation}])
    second = _observe(j, sessions["sup"].token, key="obs-g2", seq=1)
    assert second.status == "fresh"
    j._clock = lambda: base + 250
    at_now = C._now_iso(j._clock())
    with j._tx() as con:
        con.execute(
            "INSERT INTO runtime_observations(observation_id,lane,"
            "organization_revision,workload_id,target_runtime_instance,"
            "target_generation,observer_principal,observer_runtime,"
            "observer_generation,verb,idempotency_key,req_hash,supervisor_seq,"
            "observation_kind,reason_code,detector_state,cpu_millis,rss_bytes,"
            "heartbeat_age_ms,exit_code,recovery_command_id,observed_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,'runtime.observe',?,?,?,?,?,?,?,?,?,?,?,?)",
            ("obs-forastero", "llminbox", 1, "be-01",
             old_target.runtime_instance, old_target.generation,
             bindings["sup"].principal_id, sessions["sup"].runtime_instance,
             sessions["sup"].generation, "replay-tardio",
             hashlib.sha256(b"forastero").hexdigest(), 2, "cycle_ack",
             "PROCESS_PRESENT", None, None, None, None, None, None, at_now))
    changed = j.evaluate_runtime_deadlines(sessions["sup"].token, stale_after_s=5)
    assert changed == 1, (
        "un MAX(rowid) sin escopar habria leido la fila forastera (mas "
        "reciente) y nunca marcaria stale")
    assert j.runtime_statuses(sessions["sup"].token)[0]["status"] == "stale"


@pytest.mark.parametrize(
    "freeze_kind,freeze_reason,frozen_status",
    [("exited", "PROCESS_EXITED", "stopped"),
     ("resource_degraded", "CPU_SATURATED", "degraded")])
def test_cycle_ack_protegido_no_mueve_status_stopped_ni_degraded(
        tmp_path, freeze_kind, freeze_reason, frozen_status):
    """F-STATEMACHINE-1×3 (I3), dos de los tres estados protegidos."""
    j, _, sessions = _fleet(tmp_path)
    _observe(j, sessions["sup"].token, key="obs-fresh", seq=1)
    frozen = _observe(
        j, sessions["sup"].token, key="obs-freeze", seq=2,
        kind=freeze_kind, reason=freeze_reason)
    assert frozen.status == frozen_status
    before_seq = j._connect().execute(
        "SELECT status_seq FROM runtime_status WHERE workload_id='be-01'"
    ).fetchone()["status_seq"]
    ack = _observe(j, sessions["sup"].token, key="obs-ack", seq=3)
    assert ack.status == frozen_status, "cycle_ack no puede sacar del estado protegido"
    row = j._connect().execute(
        "SELECT status,status_seq FROM runtime_status WHERE workload_id='be-01'"
    ).fetchone()
    assert row["status"] == frozen_status
    assert row["status_seq"] > before_seq, (
        "protected_cycle sigue emitiendo _runtime_transition_locked (con "
        "receipt) aunque congele el status")


def test_cycle_ack_protegido_no_mueve_status_recovering(tmp_path):
    """F-STATEMACHINE-1×3 (I3), el tercer estado protegido: recovering."""
    j, _, sessions = _fleet(tmp_path)
    _observe(j, sessions["sup"].token, key="obs-fresh", seq=1)
    lease = j.acquire_lease(sessions["sup"].token, "runtime/be-01")
    target = j._connect().execute(
        "SELECT runtime_instance FROM expected_workloads"
        " WHERE workload_id='be-01'").fetchone()[0]
    j.request_runtime_recovery(
        sessions["sup"].token, workload_id="be-01", runtime_instance=target,
        idempotency_key="recover", reason_code="OPERATOR_REQUESTED",
        action_code="RESTART_RUNTIME", fenced_resource="runtime/be-01",
        fencing_token=lease.fencing_token)
    assert j.runtime_statuses(sessions["sup"].token)[0]["status"] == "recovering"
    ack = _observe(j, sessions["sup"].token, key="obs-ack", seq=2)
    assert ack.status == "recovering"
    assert j.runtime_statuses(sessions["sup"].token)[0]["status"] == "recovering"


def test_cycle_ack_actua_normal_fuera_de_estados_protegidos(tmp_path):
    """Regresión pedida por `@qa`: el cambio toca TODO `cycle_ack`, no sólo la
    rama protegida — fuera de stopped/degraded/recovering sigue moviendo el
    status con normalidad (stale -> fresh)."""
    j, _, sessions = _fleet(tmp_path)
    first = _observe(j, sessions["sup"].token, key="obs-1", seq=1)
    assert first.status == "fresh"
    base = j._clock()
    j._clock = lambda: base + 10
    assert j.evaluate_runtime_deadlines(sessions["sup"].token, stale_after_s=5) == 1
    assert j.runtime_statuses(sessions["sup"].token)[0]["status"] == "stale"
    revived = _observe(j, sessions["sup"].token, key="obs-2", seq=2)
    assert revived.status == "fresh"
    assert j.runtime_statuses(sessions["sup"].token)[0]["status"] == "fresh"


def _recovery_fleet(tmp_path):
    j, bindings, sessions = _fleet(tmp_path)
    lease = j.acquire_lease(sessions["sup"].token, "runtime/be-01")
    target = j._connect().execute(
        "SELECT runtime_instance FROM expected_workloads"
        " WHERE workload_id='be-01'").fetchone()[0]
    recovery = j.request_runtime_recovery(
        sessions["sup"].token, workload_id="be-01", runtime_instance=target,
        idempotency_key="recover", reason_code="OPERATOR_REQUESTED",
        action_code="RESTART_RUNTIME", fenced_resource="runtime/be-01",
        fencing_token=lease.fencing_token)
    return j, bindings, sessions, lease, recovery


def test_recovery_fence_target_movido_rechaza_avance(tmp_path):
    """F-FENCE-1: el target se movió (nueva runtime_instance, nueva
    revisión) entre `request_runtime_recovery` y `advance_runtime_recovery`
    -> RecoveryConflict, CERO acciones y CERO cambio en el command
    (MARK:astra-guard-link-followup-20260908 pide medir el efecto, no sólo
    el tipo de excepción)."""
    j, bindings, sessions, lease, recovery = _recovery_fleet(tmp_path)
    con = j._connect()
    new_target = j.open_session("target")
    j.activate_organization(
        sessions["org"].token, revision=2, source_sha256="1" * 64,
        attestation_state="attested", roles=_ROLES_STD, reports=_REPORTS_STD,
        reviewers=[], escalations=[],
        workloads=[{"workload_id": "be-01", "role": "be",
                    "principal_id": bindings["target"].principal_id,
                    "runtime_instance": new_target.runtime_instance,
                    "credential_generation": new_target.generation}])
    antes = con.execute(
        "SELECT state FROM commands WHERE command_id=?",
        (recovery.command_id,)).fetchone()[0]
    assert antes == "accepted"
    # `activate_organization` YA resetea be-01 a `absent` por su cuenta (todo
    # workload de la revisión nueva nace sin observar) — eso no es mío ni lo
    # toco; lo capturo para probar que MI rechazo no lo mueve NI UN PASO MÁS.
    proyeccion_antes = con.execute(
        "SELECT status FROM runtime_status WHERE lane='llminbox'"
        " AND workload_id='be-01'").fetchone()[0]
    with pytest.raises(C.RecoveryConflict):
        j.advance_runtime_recovery(
            sessions["worker"].token, recovery.command_id, "received",
            workload_id=recovery.workload_id,
            runtime_instance=recovery.runtime_instance)
    assert con.execute(
        "SELECT state FROM commands WHERE command_id=?",
        (recovery.command_id,)).fetchone()[0] == antes, \
        "el command avanzó pese al target movido de revisión"
    assert con.execute(
        "SELECT status FROM runtime_status WHERE lane='llminbox'"
        " AND workload_id='be-01'").fetchone()[0] == proyeccion_antes, \
        "el intento fallido no debe mover la proyección ni un paso más"


def test_recovery_fence_sesion_requester_revocada_rechaza_avance(tmp_path):
    """F-FENCE-2: la sesión que pidió la recovery ya no está vigente ->
    FencingConflict. Quien avanza (`worker`) no tiene por qué ser el
    requester, así que su sesión sigue viva — se revoca la del `sup`."""
    j, bindings, sessions, lease, recovery = _recovery_fleet(tmp_path)
    j.revoke_current(sessions["sup"].token)
    with pytest.raises(C.FencingConflict):
        j.advance_runtime_recovery(
            sessions["worker"].token, recovery.command_id, "received",
            workload_id=recovery.workload_id,
            runtime_instance=recovery.runtime_instance)


def test_recovery_fence_lease_liberado_rechaza_avance(tmp_path):
    """F-FENCE-3: la valla ya no es la vigente (liberada) -> FencingConflict."""
    j, bindings, sessions, lease, recovery = _recovery_fleet(tmp_path)
    j.release_lease(sessions["sup"].token, "runtime/be-01")
    with pytest.raises(C.FencingConflict):
        j.advance_runtime_recovery(
            sessions["worker"].token, recovery.command_id, "received",
            workload_id=recovery.workload_id,
            runtime_instance=recovery.runtime_instance)


def test_advance_recovery_ata_command_al_target_declarado(tmp_path):
    """Hallazgo codex/cto (MARK:astra-review-1725-20260908): con DOS
    recoveries reales en vuelo en el mismo carril (A sobre be-01, B sobre
    be-02), avanzar el command de B declarando el target de A debe rechazar
    SIN mover el estado de B — la puerta ata command+target, no sólo lane."""
    j, bindings, sessions = _fleet_asociacion(tmp_path)
    lease_a = j.acquire_lease(sessions["sup"].token, "runtime/be-01")
    rec_a = j.request_runtime_recovery(
        sessions["sup"].token, workload_id="be-01",
        runtime_instance=sessions["target-a"].runtime_instance,
        idempotency_key="recover-a", reason_code="OPERATOR_REQUESTED",
        action_code="RESTART_RUNTIME", fenced_resource="runtime/be-01",
        fencing_token=lease_a.fencing_token)
    lease_b = j.acquire_lease(sessions["sup"].token, "runtime/be-02")
    rec_b = j.request_runtime_recovery(
        sessions["sup"].token, workload_id="be-02",
        runtime_instance=sessions["target-b"].runtime_instance,
        idempotency_key="recover-b", reason_code="OPERATOR_REQUESTED",
        action_code="RESTART_RUNTIME", fenced_resource="runtime/be-02",
        fencing_token=lease_b.fencing_token)
    con = j._connect()

    def estado(command_id):
        return con.execute(
            "SELECT state FROM commands WHERE command_id=?",
            (command_id,)).fetchone()[0]

    assert estado(rec_a.command_id) == estado(rec_b.command_id) == "accepted"

    # command de B, target declarado de A: rechaza, y B NO se mueve.
    with pytest.raises(C.RecoveryConflict, match="binding workload/runtime"):
        j.advance_runtime_recovery(
            sessions["worker"].token, rec_b.command_id, "received",
            workload_id=rec_a.workload_id, runtime_instance=rec_a.runtime_instance)
    assert estado(rec_b.command_id) == "accepted", \
        "el command de B avanzó pese al binding cruzado"
    assert estado(rec_a.command_id) == "accepted", \
        "A tampoco debería haberse tocado por un avance ajeno"

    # ⊕: la pareja correcta (B con SU PROPIO target) sí avanza, y A sigue intacto.
    j.advance_runtime_recovery(
        sessions["worker"].token, rec_b.command_id, "received",
        workload_id=rec_b.workload_id, runtime_instance=rec_b.runtime_instance)
    assert estado(rec_b.command_id) == "received"
    assert estado(rec_a.command_id) == "accepted", \
        "avanzar B con su propio binding no debe tocar A"


def test_active_recovery_expone_el_binding_para_pre_vuelo_del_ejecutor(tmp_path):
    """Contrato propuesto por backend (recuperacion_manual.py,
    MARK:astra-followup-1730-20260908): lectura autoritativa lane-scoped
    para que un ejecutor externo verifique command/target/acción/valla
    ANTES de invocar su efecto. Misma condición que ata `advance_runtime_
    recovery`/`record_runtime_observation`, expuesta como lectura."""
    j, bindings, sessions, lease, recovery = _recovery_fleet(tmp_path)
    vista = j.active_recovery(sessions["worker"].token, recovery.command_id)
    assert vista["command_id"] == recovery.command_id
    assert vista["workload_id"] == "be-01"
    assert vista["runtime_instance"] == recovery.runtime_instance
    assert vista["action_code"] == "RESTART_RUNTIME"
    # `fenced_resource` se guarda NORMALIZADO (`_recurso`, :7180 — colisión
    # case/separador-insensible; `acquire_lease` la aplica sobre el MISMO
    # literal antes de fijar `Lease.resource`, :9166) y `active_recovery`
    # devuelve el identificador PERSISTIDO tal cual — no renormaliza en
    # lectura. `lease.resource` es lo que la propia fixture ya calculó al
    # pedir la valla; comparar contra ÉL (no una cadena a mano, ni una
    # segunda llamada a `_recurso`) ata la aserción al mismo cálculo que
    # produjo el dato, sin una copia que pueda divergir (sdet #1449,
    # `AssertionError: 'runtime_be_01' == 'runtime/be-01'` — mi assert
    # original asumía la forma cruda que NUNCA se persiste).
    assert vista["fenced_resource"] == lease.resource
    assert vista["fencing_token"] == lease.fencing_token
    assert vista["state"] == "accepted"
    assert vista["runtime_state"] == "recovering"
    assert vista["vigente"] is True


def test_active_recovery_command_inexistente_es_subjectnotfound(tmp_path):
    """Fail-closed simple: un command_id que nunca existió en este carril
    se rechaza igual que cualquier otro sujeto desconocido — sin caerse ni
    filtrar nada de otros commands reales."""
    j, bindings, sessions, lease, recovery = _recovery_fleet(tmp_path)
    with pytest.raises(C.SubjectNotFound):
        j.active_recovery(sessions["worker"].token, "cmd-nunca-existio")
    # ⊕: el mismo lector SÍ resuelve el command real — la aguja lee, no está
    # rota de fábrica.
    assert j.active_recovery(
        sessions["worker"].token, recovery.command_id)["command_id"] == \
        recovery.command_id


def test_active_recovery_refleja_el_outcome_ya_cerrado_no_estado_viejo(tmp_path):
    """Tras el outcome real, la lectura deja de mentir: `state` pasa a
    `succeeded`, `runtime_state` deja `recovering`, y `vigente` pasa a
    False — hallazgo codex (MARK:astra-observer-9a71b1b-active-read-
    20260908): la transición que CIERRA la recovery también lleva
    `recovery_command_id=command_id` (es como el outcome se enlaza a su
    propio command), así que un `vigente` que sólo comparara el enlace
    seguía dando True para un command YA TERMINAL — exactamente el
    escenario que un preflight de ejecutor no puede leer mal."""
    j, bindings, sessions, lease, recovery = _recovery_fleet(tmp_path)
    j.advance_runtime_recovery(
        sessions["worker"].token, recovery.command_id, "received",
        workload_id=recovery.workload_id, runtime_instance=recovery.runtime_instance)
    j.advance_runtime_recovery(
        sessions["worker"].token, recovery.command_id, "executing",
        workload_id=recovery.workload_id, runtime_instance=recovery.runtime_instance)
    j.record_runtime_observation(
        sessions["sup"].token, workload_id="be-01",
        runtime_instance=recovery.runtime_instance,
        idempotency_key="outcome-real", supervisor_seq=1,
        observation_kind="recovery_succeeded", reason_code="RECOVERY_SUCCEEDED",
        recovery_command_id=recovery.command_id)
    vista = j.active_recovery(sessions["worker"].token, recovery.command_id)
    assert vista["state"] == "succeeded"
    assert vista["runtime_state"] == "fresh"
    assert vista["vigente"] is False, (
        "vigente=True para un command YA TERMINAL autorizaría a un "
        "ejecutor a actuar sobre una recovery cerrada")


def test_active_recovery_no_vigente_tras_sustitucion_de_target(tmp_path):
    """El target se sustituyó (nueva revisión organizativa) antes de que
    nadie avance el command: la recovery sigue existiendo como fila
    histórica —no es SubjectNotFound, el command es real— pero deja de
    ser ejecutable. `vigente` False y `runtime_state` None: no se le
    atribuye a este command el estado runtime del target que lo
    reemplazó (mismo hallazgo codex de la sustitución)."""
    j, bindings, sessions, lease, recovery = _recovery_fleet(tmp_path)
    new_target = j.open_session("target")
    j.activate_organization(
        sessions["org"].token, revision=2, source_sha256="2" * 64,
        attestation_state="attested", roles=_ROLES_STD, reports=_REPORTS_STD,
        reviewers=[], escalations=[],
        workloads=[{"workload_id": "be-01", "role": "be",
                    "principal_id": bindings["target"].principal_id,
                    "runtime_instance": new_target.runtime_instance,
                    "credential_generation": new_target.generation}])
    vista = j.active_recovery(sessions["worker"].token, recovery.command_id)
    assert vista["command_id"] == recovery.command_id, \
        "el command sigue siendo un sujeto real — no es SubjectNotFound"
    assert vista["state"] == "accepted", "nadie lo avanzó ni lo cerró"
    assert vista["vigente"] is False, (
        "el target original ya no es el activo del carril — no puede "
        "autorizar una acción sobre el target NUEVO")
    assert vista["runtime_state"] is None, (
        "no se le atribuye a este command el runtime_state del target "
        "que lo sustituyó")
    # ⊕: advance_runtime_recovery, que ya cerraba este hueco en 81bbcaf,
    # rechaza la misma sustitución — los dos caminos concuerdan.
    with pytest.raises(C.RecoveryConflict):
        j.advance_runtime_recovery(
            sessions["worker"].token, recovery.command_id, "received",
            workload_id=recovery.workload_id,
            runtime_instance=recovery.runtime_instance)


@pytest.mark.parametrize("estado_terminal", [
    "succeeded", "failed", "cancelled", "superseded"])
def test_active_recovery_no_vigente_para_los_cuatro_estados_terminales(
        tmp_path, estado_terminal):
    """ALLOWLIST, no denylist (hallazgo codex, MARK:astra-observer-9a71b1b-
    active-read-20260908, 3ª ronda): `commands.state` admite SIETE valores
    (`:1366-1367`) — `cancelled`/`superseded` son tan terminales como
    `succeeded`/`failed`, y un `not in {"succeeded","failed"}` los habría
    dejado colar como si el command siguiera en curso.

    `succeeded`/`failed` SÍ tienen puerta legal (el ciclo completo ya
    los ejercita en el test de arriba); `cancelled`/`superseded` hoy sólo
    se alcanzan por un camino GENÉRICO de comandos (`submit_command` con
    revisión superior en el mismo `workstream_id`) que no es del pathset
    de este módulo y cuyo contrato de revisión no está explorado aquí —
    se declara así en vez de fingir una puerta que no se verificó: fixture
    DIRECTO sobre `commands.state`, documentado, no una llamada legal."""
    j, bindings, sessions, lease, recovery = _recovery_fleet(tmp_path)
    con = j._connect()
    con.execute("UPDATE commands SET state=? WHERE command_id=?",
               (estado_terminal, recovery.command_id))
    con.commit()
    vista = j.active_recovery(sessions["worker"].token, recovery.command_id)
    assert vista["command_id"] == recovery.command_id, \
        "el command sigue siendo un sujeto real"
    assert vista["state"] == estado_terminal
    assert vista["vigente"] is False, (
        f"command_state={estado_terminal!r} coló como vigente — "
        "la allowlist debe excluir cualquier estado que no sea "
        "accepted/received/executing")


def test_recovering_sobrevive_observaciones_protegidas_y_el_outcome_cierra_el_ciclo(tmp_path):
    """Ciclo COMPLETO, no sólo el status (hallazgo codex,
    MARK:astra-guard-link-followup-20260908): accepted -> DOS observaciones
    protegidas consecutivas, ninguna trae su propio recovery_command_id — la
    SEGUNDA debe leer el enlace que dejó la PRIMERA, no perderlo -> received
    -> executing -> el outcome real SÍ cierra: status a fresh, command
    succeeded, recibo. `recovering` no se mueve hasta ese último paso."""
    j, bindings, sessions, lease, recovery = _recovery_fleet(tmp_path)
    con = j._connect()

    def enlace_vigente():
        return con.execute(
            "SELECT t.recovery_command_id FROM runtime_status s"
            " JOIN runtime_status_transitions t USING (transition_id)"
            " WHERE s.lane='llminbox' AND s.workload_id='be-01'").fetchone()[0]

    def estado_command():
        return con.execute(
            "SELECT state FROM commands WHERE command_id=?",
            (recovery.command_id,)).fetchone()[0]

    def estado_runtime():
        return con.execute(
            "SELECT status FROM runtime_status WHERE lane='llminbox'"
            " AND workload_id='be-01'").fetchone()[0]

    assert estado_runtime() == "recovering"
    assert enlace_vigente() == recovery.command_id

    # Primera observación protegida (cycle_ack): status no se mueve, el
    # enlace al command sobrevive.
    primero = j.record_runtime_observation(
        sessions["sup"].token, workload_id="be-01",
        runtime_instance=recovery.runtime_instance,
        idempotency_key="cycle-durante-recovery", supervisor_seq=1,
        observation_kind="cycle_ack", reason_code="PROCESS_PRESENT")
    assert primero.status == "recovering"
    assert enlace_vigente() == recovery.command_id, \
        "la primera observación protegida borró el enlace al command"

    # Segunda, DISTINTA: debe leer el vínculo que dejó la PRIMERA (ninguna de
    # las dos observaciones trae su propio recovery_command_id).
    segundo = j.record_runtime_observation(
        sessions["sup"].token, workload_id="be-01",
        runtime_instance=recovery.runtime_instance,
        idempotency_key="recovered-durante-recovery", supervisor_seq=2,
        observation_kind="resource_recovered", reason_code="HEARTBEAT_RECOVERED")
    assert segundo.status == "recovering", (
        f"resource_recovered sacó la proyección de recovering: {segundo.status}")
    assert enlace_vigente() == recovery.command_id, \
        "la segunda observación protegida no heredó el enlace de la primera"
    assert estado_command() == "accepted", \
        "el command sigue vivo tras dos observaciones protegidas"

    # El command avanza de verdad — nada de lo anterior lo tocó ni lo bloqueó.
    j.advance_runtime_recovery(
        sessions["worker"].token, recovery.command_id, "received",
        workload_id=recovery.workload_id, runtime_instance=recovery.runtime_instance)
    j.advance_runtime_recovery(
        sessions["worker"].token, recovery.command_id, "executing",
        workload_id=recovery.workload_id, runtime_instance=recovery.runtime_instance)
    assert estado_command() == "executing"
    assert estado_runtime() == "recovering", \
        "avanzar el command no es un outcome — la proyección no se mueve"

    # El outcome real SÍ cierra el ciclo: fresh, succeeded, recibo.
    outcome = j.record_runtime_observation(
        sessions["sup"].token, workload_id="be-01",
        runtime_instance=recovery.runtime_instance,
        idempotency_key="outcome-real", supervisor_seq=3,
        observation_kind="recovery_succeeded", reason_code="RECOVERY_SUCCEEDED",
        recovery_command_id=recovery.command_id)
    assert outcome.status == "fresh"
    assert outcome.receipt_id, "el outcome terminalizado debe dejar recibo"
    assert estado_command() == "succeeded"


def test_resource_recovered_sigue_limpiando_degraded_sin_recovery_en_vuelo(tmp_path):
    """⊕ gemelo de la guarda anterior: SIN comando de recovery activo,
    `resource_recovered` sigue limpiando `degraded` exactamente como hoy —
    la guarda protege `recovering`, no se vuelve universal."""
    j, _, sessions = _fleet(tmp_path)
    degradado = _observe(j, sessions["sup"].token, key="deg-1", seq=1,
                         kind="resource_degraded", reason="CPU_SATURATED")
    assert degradado.status == "degraded"
    target = j._connect().execute(
        "SELECT w.runtime_instance FROM expected_workloads w"
        " JOIN organization_revisions o ON o.lane=w.lane"
        " AND o.revision=w.organization_revision"
        " WHERE w.lane='llminbox' AND w.workload_id='be-01'"
        " AND o.active=1").fetchone()[0]
    recuperado = j.record_runtime_observation(
        sessions["sup"].token, workload_id="be-01", runtime_instance=target,
        idempotency_key="deg-2", supervisor_seq=2,
        observation_kind="resource_recovered", reason_code="HEARTBEAT_RECOVERED")
    assert recuperado.status == "fresh", (
        "sin recovery en vuelo, resource_recovered debe seguir limpiando degraded")


def test_replay_idempotente_cycle_ack_reporta_status_congelado_no_el_crudo(tmp_path):
    """F-IDEMPOTENT-REPLAY-1 (I6): el replay de un `cycle_ack` protegido debe
    reportar el `to_status` de SU transición (congelado), no el mapeo crudo
    `_OBSERVATION_STATUS['cycle_ack']=='fresh'`."""
    j, _, sessions = _fleet(tmp_path)
    _observe(j, sessions["sup"].token, key="obs-fresh", seq=1)
    frozen = _observe(
        j, sessions["sup"].token, key="obs-freeze", seq=2,
        kind="exited", reason="PROCESS_EXITED")
    assert frozen.status == "stopped"
    first_ack = _observe(j, sessions["sup"].token, key="obs-replay", seq=3)
    assert first_ack.status == "stopped"
    replay = _observe(j, sessions["sup"].token, key="obs-replay", seq=3)
    assert replay.replayed and replay.observation_id == first_ack.observation_id
    assert replay.status == "stopped", (
        "el replay no puede reportar 'fresh' crudo cuando la transicion real "
        "quedo congelada en 'stopped'")


# ── F-VLEGACY · «v1-v5 rejection» (d83ae04) · encargo codex-llminbox 08-sep ──
#
# El contrato beta publicado es deliberadamente estrecho: «sólo admite
# creación nueva, v6→v7 o v7» (`coordination.py::_transicion_a_listo` — el
# guard `existing not in (6, DURABLE_V)` levanta `MigrationFailed` ANTES de
# tocar `_migrar`, que a su vez sólo acepta desde==6). La población principal
# del falsador son las CINCO FORMAS HISTÓRICAS VÁLIDAS — bases que
# `_clasificar` reconoce como su versión (`("conocida", k, "")`) — y el
# rechazo exigido es POR LÍMITE DE VERSIÓN (`MigrationFailed` que nombra
# durable_v={k} y el migrador offline exigido), no por forma rota ni pepper:
# pepper y forma pasan, y el rechazo llega en el límite de versión.

_V6_MENOS_V5 = ("admission_history",)
_V5_MENOS_V4 = ("leases.resource_literal",)
_V4_MENOS_V3 = ("events.recipients_roles", "events.recipients_broadcast")
_V3_MENOS_V2_TABLAS = ("event_causes", "command_causes",
                       "command_event_causes", "external_causes", "denials",
                       "unknown_credentials")
_V3_MENOS_V2_COLUMNAS = ("credential_bindings.capabilities",
                         "idempotency.req_hash_v")
_V2_MENOS_V1_TABLAS = ("event_acks", "outbox_operations",
                       "denial_aggregates")

_COMMANDS_V1 = """
CREATE TABLE commands (
  command_id   TEXT PRIMARY KEY,
  workstream_id TEXT NOT NULL,
  revision     INTEGER NOT NULL,
  lane         TEXT NOT NULL,
  principal_id TEXT NOT NULL REFERENCES principals(principal_id),
  role         TEXT,
  runtime_instance TEXT,
  payload      TEXT NOT NULL,
  state        TEXT NOT NULL,
  created_at   TEXT NOT NULL)
"""


def _fixture_historica(tmp_path, k: int):
    """Base v7 REAL con datos, rebajada por DELTA-INVERSA de los migradores a
    la forma histórica v{k} (cada delta es la inversa del migrador que la
    añadió; el tramo v7→v6 es el mismo que `_fixture_v6`). Si la rebaja no
    produce una forma que `_clasificar` reconozca como v{k}, el test muere en
    SU PRE-ASERCIÓN — nunca un falso verde por un fixture mal construido."""
    path = tmp_path / f"coordination-v{k}.sqlite"
    j = _journal(path)
    assert j.initialize() == 7
    con = j._connect()
    con.execute("PRAGMA foreign_keys=OFF")
    # Datos que tienen que sobrevivir al rechazo intactos.
    con.execute(
        "INSERT INTO principals(principal_id,principal,role,lane,created_at)"
        " VALUES('p-legacy','p-legacy','be','llminbox','2026-09-08T00:00:00Z')")
    con.execute(
        "INSERT INTO commands(command_id,workstream_id,revision,lane,"
        "principal_id,role,runtime_instance,attribution_status,payload,state,"
        "created_at) VALUES('cmd-legacy','ws-legacy',1,'llminbox','p-legacy',"
        "'be',NULL,'legacy_unattributed','{}','executing','2026-09-08T00:00:00Z')")
    con.execute("PRAGMA legacy_alter_table=ON")
    for tabla in V7_ONLY:
        con.execute(f"DROP TABLE {tabla}")
    for indice in V7_INDEXES:
        con.execute(f"DROP INDEX IF EXISTS {indice}")
    con.execute("ALTER TABLE receipts RENAME TO receipts_v7")
    con.execute(RECEIPTS_V6)
    con.execute("INSERT INTO receipts SELECT receipt_id,subject_kind,"
                "subject_id,principal_id,lane,current_state,created_at,"
                "updated_at FROM receipts_v7")
    con.execute("DROP TABLE receipts_v7")
    if k <= 5:
        for nombre in _V6_MENOS_V5:
            con.execute(f"DROP TABLE {nombre}")
    if k <= 4:
        for columna in _V5_MENOS_V4:
            con.execute(f"ALTER TABLE {columna.split('.')[0]}"
                        f" DROP COLUMN {columna.split('.')[1]}")
    if k <= 3:
        for columna in _V4_MENOS_V3:
            con.execute(f"ALTER TABLE {columna.split('.')[0]}"
                        f" DROP COLUMN {columna.split('.')[1]}")
    if k <= 2:
        for nombre in _V3_MENOS_V2_TABLAS:
            con.execute(f"DROP TABLE {nombre}")
        for columna in _V3_MENOS_V2_COLUMNAS:
            con.execute(f"ALTER TABLE {columna.split('.')[0]}"
                        f" DROP COLUMN {columna.split('.')[1]}")
    if k == 1:
        for nombre in _V2_MENOS_V1_TABLAS:
            con.execute(f"DROP TABLE {nombre}")
        # commands se RECONSTRUYE: la CHECK de atribución es de tabla y no
        # deja DROP COLUMN. Forma histórica B (c51cd1e): con role y
        # runtime_instance, sin attribution_status.
        con.execute("ALTER TABLE commands RENAME TO commands_pre_v1")
        con.execute(_COMMANDS_V1)
        con.execute(
            "INSERT INTO commands(command_id,workstream_id,revision,lane,"
            "principal_id,role,runtime_instance,payload,state,created_at)"
            " SELECT command_id,workstream_id,revision,lane,principal_id,"
            "role,runtime_instance,payload,state,created_at"
            " FROM commands_pre_v1")
        con.execute("DROP TABLE commands_pre_v1")
    con.execute("PRAGMA legacy_alter_table=OFF")
    con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('durable_v',?)",
                (str(k),))
    con.commit()
    con.close()
    j.close()
    return path


@pytest.mark.parametrize("k", [1, 2, 3, 4, 5])
def test_base_historica_valida_rechazada_por_limite_de_version(tmp_path, k):
    """F-VLEGACY-VALIDA-{1..5}: una forma histórica VÁLIDA de v{k} —
    reconocida como tal por `_clasificar` (pre-aserción sobre el retorno real
    de 3 elementos: ("conocida", k, "")) — se rechaza en `initialize()` POR
    LÍMITE DE VERSIÓN: `MigrationFailed` cuyo detalle nombra durable_v={k} y
    el migrador offline exigido («el contrato beta sólo admite creación
    nueva, v6→v7 o v7»), cero bytes v7 en el fichero, sello y DATOS
    originales intactos (el rechazo cierra, no corrompe)."""
    path = _fixture_historica(tmp_path, k)
    con = _raw(path)
    assert con.execute("PRAGMA foreign_key_check").fetchall() == [], \
        "precondición: la base histórica no tiene referencias huérfanas"
    veredicto = C._clasificar(con)
    con.close()
    assert veredicto == ("conocida", k, ""), (
        f"el fixture no es una forma histórica válida de v{k}: {veredicto}")
    j = _journal(path)
    with pytest.raises(C.MigrationFailed) as info:
        j.initialize()
    detalle = str(info.value)
    assert f"durable_v={k}" in detalle and "migrador offline" in detalle, (
        "el rechazo no fue por el límite de versión: " + detalle)
    crudo = _raw(path)
    presentes = {r[0] for r in crudo.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert presentes.isdisjoint(V7_ONLY), sorted(presentes & V7_ONLY)
    assert crudo.execute("SELECT v FROM meta WHERE k='durable_v'"
                         ).fetchone()[0] == str(k), "el rechazo re-selló meta"
    assert crudo.execute(
        "SELECT principal_id FROM principals"
        " WHERE principal_id='p-legacy'").fetchone() is not None, \
        "el rechazo perdió el dato original"
    assert crudo.execute(
        "SELECT command_id FROM commands"
        " WHERE command_id='cmd-legacy'").fetchone() is not None, \
        "el rechazo perdió el dato original"
    j.close()


def test_crear_path_si_materializa_bytes_v7(tmp_path):
    """⊕ de F-VLEGACY: el mismo predicado «¿hay bytes v7?» tiene que poder
    dar POSITIVO — una creación nueva escribe las tablas de V7_ONLY. Sin este
    control, la ausencia de los falsadores anteriores no distingue «no
    escribió» de «no sabe mirar»."""
    path = tmp_path / "coordination.sqlite"
    j = _journal(path)
    assert j.initialize() == 7
    crudo = _raw(path)
    presentes = {r[0] for r in crudo.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(V7_ONLY) <= presentes
    j.close()


@pytest.mark.parametrize("version_sello", ["1", "2", "3", "4", "5"])
def test_sello_v1_a_v5_sin_forma_rechazado_antes_de_cualquier_byte_v7(
        tmp_path, version_sello):
    """F-VLEGACY-SINFORMA-{1..5} — cobertura COMPLEMENTARIA (criterio
    codex-llminbox): la otra mitad de la población, un sello declarado
    durable_v∈{1..5} SIN la forma que su manifiesto exige, se rechaza con
    SchemaIndeterminate cuyo detalle nombra la versión leída, y el fichero
    queda sin ningún byte v7 ni re-sello."""
    path = tmp_path / "coordination.sqlite"
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);"
        "CREATE TABLE principals(id TEXT PRIMARY KEY);")
    con.execute("INSERT INTO meta(k,v) VALUES('durable_v',?)",
                (version_sello,))
    con.execute("INSERT INTO meta(k,v) VALUES('pepper_check','x')")
    con.commit()
    con.close()
    j = _journal(path)
    with pytest.raises(C.SchemaIndeterminate) as info:
        j.initialize()
    assert version_sello in str(info.value), (
        "el rechazo no leyó el sello declarado: " + str(info.value))
    crudo = _raw(path)
    presentes = {r[0] for r in crudo.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert presentes.isdisjoint(V7_ONLY), sorted(presentes & V7_ONLY)
    assert crudo.execute(
        "SELECT v FROM meta WHERE k='durable_v'").fetchone()[0] \
        == version_sello, "el rechazo re-selló meta"


# ── F-ASSOC · «association contradictions» (d83ae04) · tupla de 5 ────────────

def _fleet_asociacion(tmp_path):
    """Flota para F-ASSOC: be-01 (target-a) y be-02 (target-b), cada uno con
    su runtime_instance y generation. NO hay tupla compartida: el índice
    productivo `u_expected_runtime` UNIQUE (lane, organization_revision,
    runtime_instance) hace INALCANZABLE esa población — la barrera queda
    acreditada por
    `test_dos_workloads_no_comparten_runtime_en_la_misma_revision`."""
    path = tmp_path / "coordination.sqlite"
    j = _journal(path)
    j.initialize()
    credentials = [
        ("org", "org", "cto", "llminbox",
         (C.CAP_ORGANIZATION_ACTIVATE, C.CAP_ORGANIZATION_READ,
          C.CAP_ADMISSION_OPERATOR)),
        ("sup", "sup", "infra", "llminbox",
         (C.CAP_RUNTIME_OBSERVE, C.CAP_RUNTIME_RECOVER, C.CAP_RUNTIME_READ)),
        ("worker", "worker", "infra", "llminbox", (C.CAP_COMMAND_WORKER,)),
        ("target-a", "be-a", "be", "llminbox", ()),
        ("target-b", "be-b", "be", "llminbox", ()),
    ]
    bindings = {}
    for cred, principal, role, lane, capabilities in credentials:
        bindings[cred] = j.bind_credential(
            cred, principal=principal, role=role, lane=lane,
            capabilities=capabilities)
    sessions = {cred: j.open_session(cred) for cred, *_ in credentials}
    j.activate_organization(
        sessions["org"].token, revision=1, source_sha256="a" * 64,
        attestation_state="attested",
        roles=_ROLES_STD, reports=_REPORTS_STD,
        reviewers=[{"role": "be", "reviewer_role": "infra"}],
        escalations=[{"role": "be", "trigger_code": "BLOCKED",
                      "target_role": "infra"}],
        workloads=[
            {"workload_id": "be-01", "role": "be",
             "principal_id": bindings["target-a"].principal_id,
             "runtime_instance": sessions["target-a"].runtime_instance,
             "credential_generation": sessions["target-a"].generation},
            {"workload_id": "be-02", "role": "be",
             "principal_id": bindings["target-b"].principal_id,
             "runtime_instance": sessions["target-b"].runtime_instance,
             "credential_generation": sessions["target-b"].generation}])
    return j, bindings, sessions


def test_dos_workloads_no_comparten_runtime_en_la_misma_revision(tmp_path):
    """BARRERA de F-ASSOC (criterio del relevo 2026-09-08: «si una
    combinación es inalcanzable por constraints, acredita esa barrera con su
    prueba»): el
    índice productivo `u_expected_runtime` —UNIQUE (lane, revision,
    runtime_instance) WHERE runtime_instance IS NOT NULL
    (coordination.py:1629)— rechaza dos workloads que compartan runtime en
    la misma revisión. Esta población NO existe: ningún falsador de la tupla
    puede apoyarse en ella, y la restricción productiva NO se toca para que
    un fixture pase."""
    j, bindings, sessions = _fleet(tmp_path)
    tupla = {"principal_id": bindings["target"].principal_id,
             "runtime_instance": sessions["target"].runtime_instance,
             "credential_generation": sessions["target"].generation}
    with pytest.raises(sqlite3.IntegrityError):
        j.activate_organization(
            sessions["org"].token, revision=2, source_sha256="1" * 64,
            attestation_state="attested", roles=_ROLES_STD, reports=_REPORTS_STD,
            reviewers=[], escalations=[],
            workloads=[{"workload_id": "be-01", "role": "be", **tupla},
                       {"workload_id": "be-02", "role": "be", **tupla}])
    activa = j._connect().execute(
        "SELECT MAX(revision) FROM organization_revisions"
        " WHERE lane='llminbox' AND active=1").fetchone()[0]
    assert activa == 1, \
        "la activación rechazada dejó revisión activa distinta de la vigente"


def test_observacion_cruzada_rechazada_la_tupla_no_presta_su_recibo(tmp_path):
    """F-ASSOC-1..2: la asociación recovery→workload va por la tupla
    (lane, revision, workload, runtime_instance, generation) dentro de la
    MISMA transacción (`_record_runtime_observation`: JOIN de
    runtime_recoveries por los seis campos + gates de estado). Muerte
    aislada alcanzable y cotas declaradas, cada una con SU mecanismo:

    · TARGET_RUNTIME_INSTANCE — barrera previa: be-01 observada con la
      runtime_instance de be-02 → SubjectNotFound. La comprobación del
      workload activo rechaza la pareja ANTES del JOIN a recoveries.
      Esta sonda acredita esa barrera y atomicidad; no distingue un
      mutante que sólo suprima target_runtime_instance del JOIN.
    · REVISION (comportamiento, no mutante aislado — COTA DECLARADA) — la
      recovery R2 nace bajo la revisión 1 y sigue `executing`; una
      activación idéntica la deja huérfana: citarla bajo la revisión 2 →
      RecoveryConflict atómico. PERO la muerte AISLADA del campo
      `organization_revision` es INALCANZABLE por API pública: toda
      activación resetea runtime_status a `absent`
      (coordination.py::_runtime_transition_locked, incondicional en el
      bucle de workloads de activate_organization) y el JOIN exige
      `runtime_state == 'recovering'` — el rechazo llega por el gate de
      estado antes de que la revisión pueda decidir, así que el mutante
      drop-revision no se distingue con esta sonda. La sonda cubre el
      COMPORTAMIENTO: el huérfano no se cobra. No se atribuye aquí un
      resultado de ejecución de mutantes.
    · WORKLOAD_ID — COTA DECLARADA CON BARRERA ACREDITADA: la muerte
      aislada exigiría dos workloads compartiendo (lane, revisión,
      runtime, generation), población que `u_expected_runtime`
      (coordination.py:1629) hace inexistente — barrera demostrada por
      `test_dos_workloads_no_comparten_runtime_en_la_misma_revision`.
    · GENERATION y LANE — COTAS DECLARADAS, sin muerte aislable:
      `credential_generation` viaja en la MISMA fila de expected_workloads
      que la revisión y sólo cambia con un bump → nunca diverge sola; el
      `lane` del JOIN es el de la SESIÓN observadora y la validación de
      workloads exige el (runtime, generation) declarado como
      runtime_session EN ESE lane → toda variación de lane arrastra una
      runtime ajena. Las sondas no acreditan por separado los cinco
      predicados del JOIN; esa limitación queda explícita.

    · ⊕ — la misma cita desde SU tupla cierra (`fresh`, command
      `succeeded`): el rechazo discrimina por tupla, no es un veto general
      a citar recoveries."""
    j, bindings, sessions = _fleet_asociacion(tmp_path)
    con = j._connect()
    filas = {w: con.execute(
        "SELECT runtime_instance FROM expected_workloads WHERE workload_id=?",
        (w,)).fetchone()[0] for w in ("be-01", "be-02")}
    rt_a, rt_b = filas["be-01"], filas["be-02"]
    lease = j.acquire_lease(sessions["sup"].token, "runtime/be-01")
    recovery = j.request_runtime_recovery(
        sessions["sup"].token, workload_id="be-01", runtime_instance=rt_a,
        idempotency_key="recover-a", reason_code="OPERATOR_REQUESTED",
        action_code="RESTART_RUNTIME", fenced_resource="runtime/be-01",
        fencing_token=lease.fencing_token)
    j.advance_runtime_recovery(
        sessions["worker"].token, recovery.command_id, "received",
        workload_id=recovery.workload_id, runtime_instance=recovery.runtime_instance)
    j.advance_runtime_recovery(
        sessions["worker"].token, recovery.command_id, "executing",
        workload_id=recovery.workload_id, runtime_instance=recovery.runtime_instance)

    def en_paz():
        return (con.execute("SELECT COUNT(*) FROM runtime_observations"
                            ).fetchone()[0],
                con.execute("SELECT state FROM commands WHERE command_id=?",
                            (recovery.command_id,)).fetchone()[0])

    # Barrera de pareja workload/runtime, anterior al JOIN de recovery.
    antes = en_paz()
    with pytest.raises(C.SubjectNotFound, match="runtime target no existe"):
        j.record_runtime_observation(
            sessions["sup"].token, workload_id="be-01",
            runtime_instance=rt_b, idempotency_key="cross-runtime",
            supervisor_seq=1, observation_kind="recovery_succeeded",
            reason_code="RECOVERY_SUCCEEDED",
            recovery_command_id=recovery.command_id)
    assert en_paz() == antes, \
        "el cruce de runtime_instance insertó u obtuvo el recibo ajeno"
    # ⊕: la misma cita, desde SU tupla, sí cierra.
    outcome = j.record_runtime_observation(
        sessions["sup"].token, workload_id="be-01",
        runtime_instance=rt_a, idempotency_key="outcome-a",
        supervisor_seq=2, observation_kind="recovery_succeeded",
        reason_code="RECOVERY_SUCCEEDED",
        recovery_command_id=recovery.command_id)
    assert outcome.status == "fresh"
    assert con.execute(
        "SELECT state FROM commands WHERE command_id=?",
        (recovery.command_id,)).fetchone()[0] == "succeeded"

    # ── REVISION (comportamiento): R2 nace en revisión 1 y queda huérfana ──
    j.release_lease(sessions["sup"].token, "runtime/be-01")
    lease2 = j.acquire_lease(sessions["sup"].token, "runtime/be-01")
    recovery2 = j.request_runtime_recovery(
        sessions["sup"].token, workload_id="be-01", runtime_instance=rt_a,
        idempotency_key="recover-a2", reason_code="OPERATOR_REQUESTED",
        action_code="RESTART_RUNTIME", fenced_resource="runtime/be-01",
        fencing_token=lease2.fencing_token)
    j.advance_runtime_recovery(
        sessions["worker"].token, recovery2.command_id, "received",
        workload_id=recovery2.workload_id, runtime_instance=recovery2.runtime_instance)
    j.advance_runtime_recovery(
        sessions["worker"].token, recovery2.command_id, "executing",
        workload_id=recovery2.workload_id, runtime_instance=recovery2.runtime_instance)
    j.activate_organization(
        sessions["org"].token, revision=2, source_sha256="b" * 64,
        attestation_state="attested", roles=_ROLES_STD, reports=_REPORTS_STD,
        reviewers=[], escalations=[],
        workloads=[{"workload_id": "be-01", "role": "be",
                    "principal_id": bindings["target-a"].principal_id,
                    "runtime_instance": rt_a,
                    "credential_generation":
                        con.execute("SELECT credential_generation FROM"
                                    " expected_workloads WHERE workload_id="
                                    "'be-01'").fetchone()[0]},
                   {"workload_id": "be-02", "role": "be",
                    "principal_id": bindings["target-b"].principal_id,
                    "runtime_instance": rt_b,
                    "credential_generation":
                        con.execute("SELECT credential_generation FROM"
                                    " expected_workloads WHERE workload_id="
                                    "'be-02'").fetchone()[0]}])
    assert con.execute(
        "SELECT state FROM commands WHERE command_id=?",
        (recovery2.command_id,)).fetchone()[0] == "executing", \
        "la activación no debe tocar el command de la recovery"
    total_antes = con.execute(
        "SELECT COUNT(*) FROM runtime_observations").fetchone()[0]
    with pytest.raises(C.RecoveryConflict):
        j.record_runtime_observation(
            sessions["sup"].token, workload_id="be-01",
            runtime_instance=rt_a, idempotency_key="cross-revision",
            supervisor_seq=3, observation_kind="recovery_succeeded",
            reason_code="RECOVERY_SUCCEEDED",
            recovery_command_id=recovery2.command_id)
    assert con.execute("SELECT COUNT(*) FROM runtime_observations"
                       ).fetchone()[0] == total_antes, \
        "la cita a la recovery huérfana insertó"
    assert con.execute(
        "SELECT state FROM commands WHERE command_id=?",
        (recovery2.command_id,)).fetchone()[0] == "executing", \
        "la recovery huérfana se terminalizó desde la revisión nueva"


def test_conexion_cacheada_tras_restore_por_otra_instancia_exige_initialize(
        tmp_path):
    """F-CACHED-RESTORE (spec qa 10:00:44Z,
    `MARK:qa-hueco-cacheada-tras-restore-test-11-es-reconexion`): la otra
    mitad del término two-Journal/inode. Journal A deja su thread-local
    CALIENTE con una escritura; una SEGUNDA instancia sobre la MISMA ruta
    corre la ceremonia db-wide completa y `restore_pre_v7_snapshot` (la
    ceremonia es la de `test_rollback_restaura_exactamente…`); entonces la
    escritura operacional de A por su conexión CACHEADA debe levantar
    `IdentityChanged` — la capa IDENTIDAD, no cualquier fallo: un
    `AdmissionConflict` (capa drain, la admisión quedó sealed) sería verde
    por la razón equivocada, y el orden lo garantiza — la identidad corre
    en `_Tx.__enter__` → `_verificar_vigencia`, ANTES de autenticación,
    capacidades y admisión.
    Mutante que mata (P0 del término): sin el check de
    coordination.py:4146-4152, la fd cacheada de A apunta al inode HUÉRFANO
    y la escritura «triunfa» en el vacío — pérdida silenciosa."""
    path = _fixture_v6(tmp_path)
    j_a = _journal(path)
    assert j_a.initialize() == 7
    # Escritura operacional de A: thread-local caliente.
    j_a.bind_credential("org", principal="org", role="cto", lane="llminbox",
                        capabilities=(C.CAP_ORGANIZATION_ACTIVATE,
                                      C.CAP_ORGANIZATION_READ,
                                      C.CAP_ADMISSION_OPERATOR))
    j_a.open_session("org")
    cached_a = j_a._local.con
    assert cached_a is not None

    # Journal B: SEGUNDA instancia, misma ruta, ceremonia db-wide completa.
    j_b = _journal(path)
    assert j_b.initialize() == 7
    j_b.bind_credential("op", principal="op", role="infra", lane="llminbox",
                        capabilities=(C.CAP_ADMISSION_OPERATOR,))
    sesion_b = j_b.open_session("op")
    snapshot = j_b.migration_snapshot()
    for verb in C.ADMISSION_VERBS:
        j_b.seal_admission(
            sesion_b.token, verb, reason_code="DRAIN_FOR_ROLLBACK",
            expected_epoch=j_b.admission(sesion_b.token, verb).epoch)
    j_b.restore_pre_v7_snapshot(
        sesion_b.token, expected_sha256=snapshot.sha256)

    # B no cerró/reabrió el handle de A; el intento usa el mismo caché.
    assert j_a._local.con is cached_a
    # A intenta escribir por su conexión CACHEADA: identidad, no admisión.
    with pytest.raises(C.IdentityChanged):
        j_a.bind_credential("worker", principal="worker", role="infra",
                            lane="llminbox",
                            capabilities=(C.CAP_COMMAND_WORKER,))
    assert j_a._identidad_invalidada.is_set(), \
        "el evento de invalidación de identidad no quedó puesto"
    # El gate persiste: el reintento tampoco se cuela — A exige initialize().
    with pytest.raises(C.IdentityChanged):
        j_a.bind_credential("worker", principal="worker", role="infra",
                            lane="llminbox",
                            capabilities=(C.CAP_COMMAND_WORKER,))
    # Pins del fichero: la base restaurada es v6 SIN rastro del mundo v7.
    con = _raw(path)
    assert con.execute(
        "SELECT v FROM meta WHERE k='durable_v'").fetchone()[0] == "6"
    tablas = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert tablas.isdisjoint(V7_ONLY), sorted(tablas & V7_ONLY)
    con.close()
    assert _sha(path) == snapshot.sha256
