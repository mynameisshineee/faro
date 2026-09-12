"""HTTP de flota sobre Journal real y temporal; sin parchear autorizaciones."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import coordination as C
import native_gateway as G
from tests.journal._arnes import GRAMATICA, censo


@pytest.fixture
def fleet(tmp_path):
    clock = [1_700_000_000.0]
    journal = C.Journal(
        str(tmp_path / "coordination.sqlite"), pepper=b"fleet-http-test-pepper",
        lane_ledgers={"lane-a": ["ledger-a"], "lane-b": ["ledger-b"], "lane-c": ["ledger-c"]},
        grammar=GRAMATICA, recipient_resolver=censo, clock=lambda: clock[0])
    journal.initialize()
    bindings = {}
    for lane in ("a", "b"):
        specs = {
            "operator": ("cto", (C.CAP_ORGANIZATION_ACTIVATE,
                                  C.CAP_ORGANIZATION_READ, C.CAP_COMMAND_WORKER)),
            "observer": ("infra", (C.CAP_RUNTIME_OBSERVE, C.CAP_RUNTIME_READ,
                                    C.CAP_ORGANIZATION_READ)),
            "reader": ("infra", (C.CAP_RUNTIME_READ, C.CAP_ORGANIZATION_READ)),
            "recoverer": ("infra", (C.CAP_RUNTIME_RECOVER, G.CAP_LEASE_HOLDER)),
            "target": ("be", ()),
        }
        for role, (name, caps) in specs.items():
            identity = f"{role}-{lane}"
            bindings[identity] = journal.bind_credential(
                f"credential-{identity}", principal=identity, role=name,
                lane=f"lane-{lane}", capabilities=caps)
    bindings["unconfigured"] = journal.bind_credential(
        "credential-unconfigured", principal="unconfigured", role="infra", lane="lane-c",
        capabilities=(C.CAP_ORGANIZATION_READ, C.CAP_RUNTIME_READ))
    # Todas las credenciales se vinculan antes de emitir sesiones: bind cambia
    # la generación global y no debe invalidar un token de la propia fixture.
    sessions = {name: journal.open_session(f"credential-{name}") for name in bindings}
    for lane in ("a", "b"):
        target = sessions[f"target-{lane}"]
        journal.activate_organization(
            sessions[f"operator-{lane}"].token, revision=1, source_sha256=lane * 64,
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
                        "principal_id": bindings[f"target-{lane}"].principal_id,
                        "runtime_instance": target.runtime_instance,
                        "credential_generation": target.generation},
                       {"workload_id": "be-02", "role": "be",
                        "principal_id": None, "runtime_instance": None,
                        "credential_generation": None}])
    with TestClient(G.create_native_app(journal)) as client:
        yield journal, client, sessions, clock
    journal.dispose()


def _auth(sessions, name="observer-a", **headers):
    return {"Authorization": f"Bearer {sessions[name].token}", **headers}


def _path(sessions, suffix="", target="target-a"):
    return f"/native/v1/runtimes/{sessions[target].runtime_instance}{suffix}"


def _observation(**changes):
    return {"workload_id": "be-01", "supervisor_seq": 1,
            "observation_kind": "cycle_ack", "reason_code": "PROCESS_PRESENT",
            "heartbeat_age_ms": 0, **changes}


def _observe(client, sessions, *, key="observe-1", name="observer-a", **changes):
    return client.post(_path(sessions, "/observations"),
                       headers=_auth(sessions, name, **{"Idempotency-Key": key}),
                       json=_observation(**changes))


def _counts(journal, *, receipts=False):
    tables = ["runtime_observations", "runtime_status_transitions",
              "runtime_recoveries", "commands"]
    if receipts:
        tables.append("receipts")
    return tuple(journal._connect().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                 for table in tables)


def _recovery(journal, sessions):
    lease = journal.acquire_lease(sessions["recoverer-a"].token, "runtime/be-01")
    return {"workload_id": "be-01", "reason_code": "OPERATOR_REQUESTED",
            "action_code": "RESTART_RUNTIME", "fenced_resource": lease.resource,
            "fencing_token": lease.fencing_token}


def test_reads_project_one_authoritative_revision_and_each_expected_workload(fleet):
    journal, client, sessions, _ = fleet
    org = client.get("/native/v1/organization", headers=_auth(sessions))
    assert org.status_code == 200, org.text
    assert org.headers["cache-control"] == "no-store"
    body = org.json()
    assert body["authority"] is True
    assert body["revision"] == {
        "lane": "lane-a", "revision": 1, "source_digest": "a" * 64,
        "freshness": "atestiguada", "activated_at": "2023-11-14T22:13:20Z"}
    assert next(row for row in body["roles"] if row["role"] == "be") == {
        "role": "be", "reports_to": "infra", "reviewers": ["infra"],
        "escalation_routes": [{"trigger_code": "BLOCKED", "target_role": "infra"}],
        "layer": 2}
    assert body["workloads"] == [
        {"workload_id": "be-01", "role": "be"},
        {"workload_id": "be-02", "role": "be"}]
    response = client.get("/native/v1/runtimes", headers=_auth(sessions))
    assert response.status_code == 200, response.text
    rows = response.json()
    assert [(r["workload_id"], r["status"]) for r in rows] == [
        ("be-01", "absent"), ("be-02", "absent")]
    assert rows[0]["principal"] == sessions["target-a"].principal_id
    assert rows[0]["runtime_instance"] == sessions["target-a"].runtime_instance
    assert rows[1]["runtime_instance"] is None
    assert all(r["lane"] == "lane-a" and r["receipt_id"] and r["transition_id"]
               and r["organization_revision"] == 1 for r in rows)
    assert all("principal_id" not in r for r in rows)
    assert client.get(_path(sessions), headers=_auth(sessions)).json() == rows[0]
    before = _counts(journal, receipts=True)
    with TestClient(G.create_native_app(journal)) as restarted:
        assert restarted.get("/native/v1/runtimes", headers=_auth(sessions)).json() == rows
    assert _counts(journal, receipts=True) == before


def test_observation_replay_and_sequence_preserve_durable_result(fleet):
    journal, client, sessions, _ = fleet
    first = _observe(client, sessions)
    assert first.status_code == 202, first.text
    initial = first.json()
    before = _counts(journal, receipts=True)
    replay = _observe(client, sessions)
    assert replay.status_code == 202, replay.text
    assert replay.json() == {**initial, "replayed": True}
    assert _counts(journal, receipts=True) == before
    changed = _observe(client, sessions, supervisor_seq=2)
    assert changed.status_code == 409
    assert changed.json()["code"] == "IDEMPOTENCY_CONFLICT"
    old_seq = _observe(client, sessions, key="other-key")
    assert old_seq.status_code == 409
    assert old_seq.json()["code"] == "OBSERVATION_SEQUENCE_CONFLICT"
    assert _counts(journal) == before[:4]
    confirmed = _observe(client, sessions, key="confirm", supervisor_seq=2)
    assert confirmed.status_code == 202
    assert confirmed.json()["receipt_id"] is None
    assert confirmed.json()["transition_id"] is None
    assert _counts(journal)[1] == before[1]


def test_operational_states_come_from_external_evidence_and_server_deadline(fleet):
    journal, client, sessions, clock = fleet
    assert _observe(client, sessions).json()["status"] == "fresh"
    degraded = _observe(client, sessions, key="degraded", supervisor_seq=2,
                        observation_kind="resource_degraded", reason_code="MEMORY_SATURATED",
                        rss_bytes=8192)
    assert degraded.status_code == 202, degraded.text
    assert degraded.json()["status"] == "degraded"
    assert _observe(client, sessions, key="still-present", supervisor_seq=3).json()["status"] == "degraded"
    recovered = _observe(client, sessions, key="recovered", supervisor_seq=4,
                         observation_kind="resource_recovered", reason_code="HEARTBEAT_RECOVERED")
    assert recovered.status_code == 202, recovered.text
    assert recovered.json()["status"] == "fresh"
    clock[0] += 6
    assert journal.evaluate_runtime_deadlines(sessions["observer-a"].token, stale_after_s=5) == 1
    stale = client.get(_path(sessions), headers=_auth(sessions)).json()
    assert stale["status"] == "stale"
    exited = _observe(client, sessions, key="exit", supervisor_seq=5,
                      observation_kind="exited", reason_code="PROCESS_EXITED", exit_code=1)
    assert exited.status_code == 202, exited.text
    assert exited.json()["status"] == "stopped"


@pytest.mark.parametrize("kind,reason,status", [
    ("recovery_succeeded", "RECOVERY_SUCCEEDED", "fresh"),
    ("recovery_failed", "RECOVERY_FAILED", "degraded"),
])
def test_recovery_is_a_fenced_request_with_an_explicit_external_outcome(fleet, kind, reason, status):
    journal, client, sessions, _ = fleet
    payload = _recovery(journal, sessions)
    path = _path(sessions, "/recoveries")
    headers = _auth(sessions, "recoverer-a", **{"Idempotency-Key": "recover-1"})
    result = client.post(path, headers=headers, json=payload)
    assert result.status_code == 202, result.text
    record = result.json()
    assert client.get(_path(sessions), headers=_auth(sessions)).json()["status"] == "recovering"
    before = _counts(journal, receipts=True)
    assert client.post(path, headers=headers, json=payload).json() == {**record, "replayed": True}
    assert _counts(journal, receipts=True) == before
    changed = client.post(path, headers=headers, json={**payload, "reason_code": "STALE_RUNTIME"})
    assert changed.status_code == 409
    assert changed.json()["code"] == "IDEMPOTENCY_CONFLICT"
    # El operador hace avanzar el comando; ninguna ruta de flota ejecuta o mata procesos.
    journal.advance_runtime_recovery(
        sessions["operator-a"].token, record["command_id"], "received",
        workload_id=record["workload_id"], runtime_instance=record["runtime_instance"])
    journal.advance_runtime_recovery(
        sessions["operator-a"].token, record["command_id"], "executing",
        workload_id=record["workload_id"], runtime_instance=record["runtime_instance"])
    outcome = _observe(client, sessions, observation_kind=kind, reason_code=reason,
                       recovery_command_id=record["command_id"])
    assert outcome.status_code == 202, outcome.text
    assert outcome.json()["status"] == status


@pytest.mark.parametrize("suffix", ["/observations", "/recoveries"])
@pytest.mark.parametrize("key", [None, "", " padded ", "x" * 257])
def test_runtime_writes_require_a_valid_header_idempotency_key(fleet, suffix, key):
    journal, client, sessions, _ = fleet
    name = "observer-a" if suffix == "/observations" else "recoverer-a"
    headers = _auth(sessions, name)
    if key is not None:
        headers["Idempotency-Key"] = key
    before = _counts(journal)
    response = client.post(_path(sessions, suffix), headers=headers, json={})
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert _counts(journal) == before


@pytest.mark.parametrize("change", [
    {"supervisor_seq": True}, {"supervisor_seq": "1"}, {"supervisor_seq": 0},
    {"cpu_millis": C.MAX_CPU_MILLIS + 1}, {"rss_bytes": -1},
    {"heartbeat_age_ms": True}, {"exit_code": 1 << 31}, {"message": "free text"},
])
def test_observation_dto_rejects_invalid_typed_input(fleet, change):
    journal, client, sessions, _ = fleet
    before = _counts(journal)
    response = _observe(client, sessions, **change)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "INVALID_BODY"
    assert _counts(journal) == before


@pytest.mark.parametrize("channel", ["body", "header", "query"])
@pytest.mark.parametrize("field", ["lane", "principal", "trust", "observed_at"])
def test_caller_cannot_supply_identity_or_observation_time(fleet, channel, field):
    journal, client, sessions, _ = fleet
    kwargs = {"headers": _auth(sessions, **{"Idempotency-Key": "scope-1"}),
              "json": _observation()}
    if channel == "body":
        kwargs["json"][field] = "untrusted"
    elif channel == "header":
        kwargs["headers"]["X-Llminbox-" + field.replace("_", "-")] = "untrusted"
    else:
        kwargs["params"] = {field: "untrusted"}
    before = _counts(journal)
    response = client.post(_path(sessions, "/observations"), **kwargs)
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "ATTRIBUTION_REJECTED"
    assert "untrusted" not in response.text
    assert _counts(journal) == before


@pytest.mark.parametrize("suffix", ["", "/observations", "/recoveries"])
def test_unknown_and_other_lane_targets_have_identical_responses_and_audit_effects(fleet, suffix):
    journal, client, sessions, _ = fleet
    payload = _observation() if suffix == "/observations" else _recovery(journal, sessions)
    name = "recoverer-a" if suffix == "/recoveries" else "observer-a"
    kwargs = {"headers": _auth(sessions, name, **{"Idempotency-Key": "scope-target"})}
    method = client.post if suffix else client.get
    if suffix:
        kwargs["json"] = payload
    before = _counts(journal, receipts=True)
    other = method(_path(sessions, suffix, "target-b"), **kwargs)
    after_other = _counts(journal, receipts=True)
    absent = method("/native/v1/runtimes/missing-runtime" + suffix, **kwargs)
    after_absent = _counts(journal, receipts=True)
    assert other.status_code == absent.status_code == 404
    assert other.json() == absent.json() == {
        "code": "SUBJECT_NOT_FOUND", "message": "sujeto no encontrado"}
    # Las lecturas escriben cero filas. Una mutación rechazada conserva su
    # auditoría acotada, idéntica en los dos casos y sin efecto operacional.
    assert before[:-1] == after_other[:-1] == after_absent[:-1]
    expected_receipts = 1 if suffix else 0
    assert after_other[-1] - before[-1] == expected_receipts
    assert after_absent[-1] - after_other[-1] == expected_receipts
    denials = journal._connect().execute(
        "SELECT principal_id,lane,runtime_instance,reason FROM denials").fetchall()
    caller = sessions[name]
    assert [tuple(row) for row in denials] == [
        (caller.principal_id, "lane-a", caller.runtime_instance, "SUBJECT_NOT_FOUND")
    ] * (2 if suffix else 0)


@pytest.mark.parametrize("suffix", ["/observations", "/recoveries"])
def test_unknown_target_audit_quota_has_the_same_shape_for_other_lane(fleet, suffix):
    journal, client, sessions, clock = fleet
    journal._denial_quota = 1
    payload = _observation() if suffix == "/observations" else _recovery(journal, sessions)
    name = "recoverer-a" if suffix == "/recoveries" else "observer-a"
    headers = _auth(sessions, name, **{"Idempotency-Key": "scope-quota"})
    deltas = []
    for path in (_path(sessions, suffix, "target-b"),
                 "/native/v1/runtimes/missing-runtime" + suffix):
        # El mismo estado de cuota en dos cubos distintos permite comparar
        # rechazo individual, primer agregado y agregado ya existente.
        clock[0] += journal._denial_bucket_s
        counts = [_counts(journal, receipts=True)]
        for _ in range(3):
            response = client.post(path, headers=headers, json=payload)
            assert response.status_code == 404
            assert response.json() == {
                "code": "SUBJECT_NOT_FOUND", "message": "sujeto no encontrado"}
            counts.append(_counts(journal, receipts=True))
        assert all(count[:-1] == counts[0][:-1] for count in counts)
        deltas.append([b[-1] - a[-1] for a, b in zip(counts, counts[1:])])
    assert deltas == [[1, 1, 0], [1, 1, 0]]


@pytest.mark.parametrize("path", ["/native/v1/runtimes", "/native/v1/organization"])
def test_reads_require_a_session_and_their_own_capability(fleet, path):
    _, client, sessions, _ = fleet
    assert client.get(path).status_code == 401
    assert client.get(path, headers=_auth(sessions, "target-a")).status_code == 403
    assert client.get(path, headers=_auth(sessions, "reader-a")).status_code == 200


def test_missing_organization_is_a_typed_absence_not_a_missing_route(fleet):
    journal, client, sessions, _ = fleet
    before = _counts(journal, receipts=True)
    headers = _auth(sessions, "unconfigured")
    response = client.get("/native/v1/organization", headers=headers)
    assert response.status_code == 404
    assert response.json() == {"code": "SUBJECT_NOT_FOUND", "message": "sujeto no encontrado"}
    empty = client.get("/native/v1/runtimes", headers=headers)
    assert empty.status_code == 200 and empty.json() == []
    assert _counts(journal, receipts=True) == before


@pytest.mark.parametrize("suffix,operation", [
    ("/observations", "runtime.observe"), ("/recoveries", "runtime.recover"),
])
def test_kernel_rechecks_capability_even_if_gateway_policy_is_relaxed(fleet, suffix, operation):
    journal, _, sessions, _ = fleet
    policy = {**G.DEFAULT_MUTATION_CAPABILITIES, operation: None}
    payload = _observation() if suffix == "/observations" else _recovery(journal, sessions)
    with TestClient(G.create_native_app(journal, mutation_capabilities=policy)) as client:
        response = client.post(_path(sessions, suffix), json=payload,
                               headers=_auth(sessions, "reader-a", **{"Idempotency-Key": "denied"}))
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "POLICY_DENIED"


def test_sequence_conflict_reports_authenticated_scope_and_allows_restart(fleet):
    journal, client, sessions, _ = fleet
    first = _observe(client, sessions, key="before-restart", supervisor_seq=5)
    assert first.status_code == 202
    # Un nuevo cliente de proceso conserva la MISMA sesión autenticada.
    with TestClient(G.create_native_app(journal)) as restarted:
        before = _counts(journal)
        rejected = _observe(restarted, sessions, key="after-restart", supervisor_seq=1)
        assert rejected.status_code == 409
        assert rejected.json()["code"] == "OBSERVATION_SEQUENCE_CONFLICT"
        assert rejected.json()["max_supervisor_seq"] == 5
        assert _counts(journal) == before
        accepted = _observe(restarted, sessions, key="after-resync", supervisor_seq=6)
        assert accepted.status_code == 202
        before_replay = _counts(journal, receipts=True)
        replay = _observe(restarted, sessions, key="after-resync", supervisor_seq=6)
        assert replay.status_code == 202 and replay.json()["replayed"] is True
        assert replay.json()["observation_id"] == accepted.json()["observation_id"]
        assert _counts(journal, receipts=True) == before_replay

    # Otra sesión del MISMO principal tiene su propio runtime de observador.
    other_session = journal.open_session("credential-observer-a")
    sessions = {**sessions, "other-observer-a": other_session}
    assert _observe(client, sessions, name="other-observer-a", key="other-session",
                    supervisor_seq=2).status_code == 202
    rejected = _observe(client, sessions, name="other-observer-a", key="other-conflict",
                        supervisor_seq=1)
    assert rejected.status_code == 409 and rejected.json()["max_supervisor_seq"] == 2
    own = _observe(client, sessions, key="original-conflict", supervisor_seq=1)
    assert own.status_code == 409 and own.json()["max_supervisor_seq"] == 6
    unknown = client.post("/native/v1/runtimes/missing/observations",
                          headers=_auth(sessions, **{"Idempotency-Key": "unknown-seq"}),
                          json=_observation())
    assert unknown.status_code == 404 and "max_supervisor_seq" not in unknown.json()


@pytest.mark.parametrize("latest", [True, 0, -1, "42", C.MAX_SUPERVISOR_SEQ + 1])
def test_sequence_error_serializer_revalidates_floor(latest):
    exc = C.ObservationSequenceConflict("private diagnostic")
    # Una excepción alterada por otra capa no puede eludir validación del wire.
    exc.latest = latest
    response = G._journal_error(exc)
    assert response.status_code == 500
    assert b"max_supervisor_seq" not in response.body
    assert b"private diagnostic" not in response.body


def test_sequence_error_without_floor_remains_typed_non_retryable_conflict():
    response = G._journal_error(C.ObservationSequenceConflict("out of range"))
    assert response.status_code == 409
    assert b"OBSERVATION_SEQUENCE_CONFLICT" in response.body
    assert b"max_supervisor_seq" not in response.body


def test_recovery_with_expired_fence_is_http_conflict_with_zero_operational_writes(fleet):
    journal, client, sessions, clock = fleet
    payload = _recovery(journal, sessions)
    clock[0] += 301  # lease=300; las sesiones siguen dentro de sus 900 segundos.
    before = _counts(journal)
    response = client.post(_path(sessions, "/recoveries"), json=payload,
                           headers=_auth(sessions, "recoverer-a",
                                         **{"Idempotency-Key": "expired-fence"}))
    assert response.status_code == 409
    assert response.json()["code"] == "FENCING_CONFLICT"
    assert response.json()["receipt_id"]
    assert _counts(journal) == before


def test_only_five_fleet_routes_are_added_and_organization_has_no_write_endpoint(fleet):
    _, client, _, _ = fleet
    routes = {(method, r.path) for r in client.app.routes for method in r.methods
              if "/runtimes" in r.path or r.path == "/native/v1/organization"}
    assert routes == {
        ("POST", "/native/v1/runtimes/{runtime_instance}/observations"),
        ("GET", "/native/v1/runtimes"), ("GET", "/native/v1/runtimes/{runtime_instance}"),
        ("POST", "/native/v1/runtimes/{runtime_instance}/recoveries"),
        ("GET", "/native/v1/organization"),
    }
    assert client.post("/native/v1/organization", json={}).status_code == 405
