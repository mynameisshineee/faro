"""Falsificadores HTTP de la frontera nativa.

No importan ``servicio`` y no parchean metodos de dominio: cada asercion cruza
FastAPI y comprueba el estado durable del Journal real.
"""

from __future__ import annotations

import inspect
import ast
import json
import shutil
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

import coordination as C
import native_gateway as G
from tests.journal._arnes import GRAMATICA, censo


CREDS = {
    "cred-author-high-entropy": {
        "rol": "be", "carril": "lane-a", "principal_id": "author-a"},
    "cred-worker-high-entropy": {
        "rol": "worker", "carril": "lane-a", "principal_id": "worker-a"},
    "cred-security-high-entropy": {
        "rol": "security", "carril": "lane-a", "principal_id": "security-a"},
    "cred-other-high-entropy": {
        "rol": "be", "carril": "lane-b", "principal_id": "author-b"},
    "cred-unprivileged-high-entropy": {
        "rol": "qa", "carril": "lane-a", "principal_id": "qa-a"},
    # La barrera es POR CARRIL, así que su operador también: una credencial de
    # `lane-a` no puede abrir la puerta de `lane-b`.
    "cred-admision-a-high-entropy": {
        "rol": "infra", "carril": "lane-a", "principal_id": "admision-a"},
    "cred-admision-b-high-entropy": {
        "rol": "infra", "carril": "lane-b", "principal_id": "admision-b"},
}

GRANTS = {
    "author-a": (G.CAP_EVENT_WRITER, G.CAP_LEASE_HOLDER,
                 G.CAP_COMMAND_SUBMITTER),
    "worker-a": (C.CAP_OUTBOX_WORKER, C.CAP_INDEXER,
                 C.CAP_COMMAND_WORKER),
    "security-a": (G.CAP_DELIVERY_ACK,),
    "author-b": (G.CAP_EVENT_WRITER,),
    "qa-a": (),
    "admision-a": (C.CAP_ADMISSION_OPERATOR,),
    "admision-b": (C.CAP_ADMISSION_OPERATOR,),
}

INTENT = {
    "type": "message", "verb": "inform", "to": ["security"],
    "kind": "DELIVERED", "head": "ready", "body": "payload",
}


@pytest.fixture
def harness(tmp_path):
    journal = C.Journal(
        str(tmp_path / "coordination.sqlite"),
        pepper=b"native-gateway-test-pepper",
        lane_ledgers={"lane-a": ["ledger-a"], "lane-b": ["ledger-b"]},
        grammar=GRAMATICA,
        recipient_resolver=censo,
    )
    journal.initialize()
    G.configure_journal_from_v8(journal, CREDS, GRANTS)
    # La barrera nace cerrada; este arnés ESCRIBE por los dos carriles, así que
    # abre los dos. No hay ruta HTTP para esto a propósito: mover la puerta es
    # un acto de operador sobre el Core, no un verbo de la pasarela.
    for credencial in ("cred-admision-a-high-entropy",
                       "cred-admision-b-high-entropy"):
        operador = journal.open_session(credencial)
        for verbo in C.ADMISSION_VERBS:
            journal.open_admission(operador.token, verbo, reason_code="ROLLOUT")
    with TestClient(G.create_native_app(journal)) as client:
        yield journal, client
    journal.dispose()


def _open(client: TestClient, credential: str) -> tuple[str, dict]:
    response = client.post(
        G.API_PREFIX + "/sessions",
        headers={"Authorization": f"Bearer {credential}"},
        json={"ttl_s": 300},
    )
    assert response.status_code == 201, response.text
    session = response.json()
    return session["token"], session


def _auth(token: str, **headers) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **headers}


def _event(client: TestClient, token: str, key: str = "idem-1", **changes):
    body = dict(INTENT)
    body.update(changes)
    return client.post(G.API_PREFIX + "/events",
                       headers=_auth(token, **{"Idempotency-Key": key}),
                       json=body)


def test_bootstrap_solo_credencial_runtime_y_no_token_compartido(harness):
    journal, client = harness
    missing = client.post(G.API_PREFIX + "/sessions", json={})
    assert missing.status_code == 401
    assert missing.json()["code"] == "RUNTIME_CREDENTIAL_REQUIRED"

    legacy = client.post(G.API_PREFIX + "/sessions",
                         headers={"X-Llminbox-Token": "shared"}, json={})
    assert legacy.status_code == 401
    assert legacy.json()["code"] == "SHARED_TOKEN_REJECTED"

    unknown = client.post(G.API_PREFIX + "/sessions",
                          headers={"Authorization": "Bearer shared-token"}, json={})
    assert unknown.status_code == 401
    assert unknown.json()["code"] == "SESSION_INVALID"
    assert "shared-token" not in unknown.text

    token, session = _open(client, "cred-author-high-entropy")
    assert token and session["principal"] == "author-a"
    assert session["role"] == "be" and session["lane"] == "lane-a"
    assert "session" not in session
    assert "principal_id" not in session
    assert "cred-author-high-entropy" not in repr(session)
    assert client.post(G.API_PREFIX + "/sessions",
                       headers={"Authorization": f"Bearer {token}"}, json={}
                       ).status_code == 401


@pytest.mark.parametrize("channel,value", [
    ("body", "principal"), ("body", "principalId"), ("body", "rol"),
    ("body", "carril"), ("body", "runtime_instance"),
    ("body", "actor"), ("header", "X-Llminbox-Role"),
    ("query", "lane"),
])
def test_atribucion_autodeclarada_se_rechaza(channel, value, harness):
    journal, client = harness
    token, _ = _open(client, "cred-author-high-entropy")
    headers = _auth(token, **{"Idempotency-Key": f"attr-{channel}-{value}"})
    body = dict(INTENT)
    url = G.API_PREFIX + "/events"
    if channel == "body":
        body[value] = "attacker"
    elif channel == "header":
        headers[value] = "attacker"
    else:
        url += f"?{value}=attacker"
    response = client.post(url, headers=headers, json=body)
    assert response.status_code == 400
    assert response.json()["code"] == "ATTRIBUTION_REJECTED"
    assert journal._connect().execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == 0


def test_whoami_es_derivado_y_headers_no_lo_pueden_pisar(harness):
    _, client = harness
    token, session = _open(client, "cred-author-high-entropy")
    response = client.get(G.API_PREFIX + "/whoami", headers=_auth(token))
    assert response.status_code == 200
    identity = response.json()
    assert identity["principal"] == "author-a"
    assert identity["runtime_instance"] == session["runtime_instance"]

    forged = client.get(G.API_PREFIX + "/whoami",
                        headers=_auth(token, **{"X-Role": "cto"}))
    assert forged.status_code == 400
    assert forged.json()["code"] == "ATTRIBUTION_REJECTED"


def test_whoami_admite_credencial_bootstrap_sin_fingir_una_sesion(harness):
    journal, client = harness
    response = client.get(
        G.API_PREFIX + "/whoami",
        headers=_auth("cred-author-high-entropy"),
    )
    assert response.status_code == 200
    identity = response.json()
    assert identity == {
        "principal": "author-a",
        "role": "be",
        "lane": "lane-a",
        "principal_source": "explicit",
    }
    assert "runtime_instance" not in identity
    assert "capabilities" not in identity
    assert "principal_id" not in identity
    assert "credential_ref" not in identity

    denied = _event(
        client, "cred-author-high-entropy", key="bootstrap-is-not-session")
    assert denied.status_code == 401
    assert denied.json()["code"] == "SESSION_INVALID"
    assert journal._connect().execute(
        "SELECT COUNT(*) c FROM events").fetchone()["c"] == 0


def test_evento_idempotente_y_dto_cerrado(harness):
    journal, client = harness
    token, _ = _open(client, "cred-author-high-entropy")
    first = _event(client, token)
    assert first.status_code == 202
    acceptance = first.json()
    replay = _event(client, token)
    assert replay.status_code == 200
    assert replay.json()["event_id"] == acceptance["event_id"]
    assert replay.json()["receipt_id"] == acceptance["receipt_id"]
    assert replay.json()["replayed"] is True

    conflict = _event(client, token, body="different")
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert journal._connect().execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == 1

    secret = "not-a-real-secret-but-must-not-reflect"
    extra = _event(client, token, key="extra", unexpected=secret)
    assert extra.status_code == 422
    assert extra.json()["code"] == "INVALID_BODY"
    assert secret not in extra.text

    wrapped = client.post(
        G.API_PREFIX + "/events",
        headers=_auth(token, **{"Idempotency-Key": "old-wrapper"}),
        json={"intent": INTENT},
    )
    assert wrapped.status_code == 422
    assert wrapped.json()["code"] == "INVALID_BODY"
    assert journal._connect().execute(
        "SELECT COUNT(*) c FROM events").fetchone()["c"] == 1


def test_receipt_cross_lane_es_indistinguible_de_inexistente(harness):
    _, client = harness
    token_a, _ = _open(client, "cred-author-high-entropy")
    token_b, _ = _open(client, "cred-other-high-entropy")
    accepted = _event(client, token_a).json()
    receipt_id = accepted["receipt_id"]
    own = client.get(G.API_PREFIX + f"/receipts/{receipt_id}",
                     headers=_auth(token_a))
    assert own.status_code == 200
    assert own.json() == {
        "receipt_id": receipt_id,
        "event_id": accepted["event_id"],
        "state": "accepted",
        "subject_type": "event",
        "subject_id": accepted["event_id"],
    }
    cross = client.get(G.API_PREFIX + f"/receipts/{receipt_id}",
                       headers=_auth(token_b))
    absent = client.get(G.API_PREFIX + "/receipts/rcp_" + "0" * 32,
                        headers=_auth(token_b))
    assert cross.status_code == absent.status_code == 404
    assert cross.json() == absent.json()
    assert receipt_id not in cross.text


def test_outbox_materializacion_index_y_ack_reales(harness):
    journal, client = harness
    author, _ = _open(client, "cred-author-high-entropy")
    worker, _ = _open(client, "cred-worker-high-entropy")
    recipient, _ = _open(client, "cred-security-high-entropy")
    accepted = _event(client, author).json()

    claim = client.post(G.API_PREFIX + "/outbox/claims",
                        headers=_auth(worker), json={"lease_s": 60})
    assert claim.status_code == 200
    job = claim.json()["job"]
    assert job["event_id"] == accepted["event_id"]
    assert job["lane"] == "lane-a" and job["ledger"] == "ledger-a"
    assert "principal_id" not in job
    assert set(job) == {
        "event_id", "ledger", "attempts", "claim_token", "receipt_id",
        "recipients", "occurred_at", "payload_sha256", "attestation", "trace",
        "principal", "principal_source", "role", "lane", "runtime_instance",
        "intent",
    }

    marked = client.post(
        G.API_PREFIX + f"/outbox/{job['event_id']}/materialized",
        headers=_auth(worker),
        json={"entry_eid": "e" * 64, "ledger": "ledger-a",
              "claim_token": job["claim_token"], "byte_off": 17},
    )
    assert marked.status_code == 204 and marked.content == b""
    indexed = client.post(G.API_PREFIX + f"/events/{job['event_id']}/indexed",
                          headers=_auth(worker), json={"index_ref": "idx:1"})
    assert indexed.status_code == 204
    ack = client.post(G.API_PREFIX + f"/events/{job['event_id']}/acks",
                      headers=_auth(recipient), json={"ack_ref": "seen:1"})
    assert ack.status_code == 200 and ack.json()["state"] == "delivered"
    states = [row["state"] for row in
              journal.transitions(author, accepted["receipt_id"])]
    assert states == ["accepted", "materialized", "indexed", "delivered"]


def test_capabilities_fail_closed_y_denegacion_tiene_recibo(harness):
    _, client = harness
    token, _ = _open(client, "cred-unprivileged-high-entropy")
    denied = _event(client, token, key="denied")
    assert denied.status_code == 403
    assert denied.json()["code"] == "POLICY_DENIED"
    assert denied.json()["receipt_id"].startswith("rcp_")
    receipt = client.get(
        G.API_PREFIX + f"/receipts/{denied.json()['receipt_id']}",
        headers=_auth(token),
    )
    assert receipt.status_code == 200
    assert receipt.json()["subject_type"] == "denial"
    assert receipt.json()["subject_id"].startswith("dnl_")
    assert "event_id" not in receipt.json()


def test_operacion_sin_declaracion_de_politica_falla_cerrada(harness):
    journal, _ = harness
    token = journal.open_session("cred-author-high-entropy").token
    # Quitar una declaracion de la politica no significa permitido: 503 cerrado.
    app = G.create_native_app(journal, mutation_capabilities={})
    with TestClient(app) as isolated:
        unavailable = isolated.post(G.API_PREFIX + "/events",
                                    headers=_auth(token, **{"Idempotency-Key": "x"}),
                                    json=INTENT)
    assert unavailable.status_code == 503
    assert unavailable.json()["code"] == "CAPABILITY_POLICY_UNDECLARED"


@pytest.mark.parametrize("policy", [
    {},
    {**G.DEFAULT_MUTATION_CAPABILITIES, "sessions.open": None},
])
def test_bootstrap_ausente_o_con_esquema_de_politica_incorrecto(policy, harness):
    journal, _ = harness
    with TestClient(G.create_native_app(
            journal, mutation_capabilities=policy)) as client:
        response = client.post(
            G.API_PREFIX + "/sessions",
            headers={"Authorization": "Bearer cred-author-high-entropy"},
            json={"ttl_s": 300},
        )
    assert response.status_code == 503
    assert response.json()["code"] == "CAPABILITY_POLICY_UNDECLARED"


def test_revoke_current_devuelve_204_y_mata_la_sesion(harness):
    journal, client = harness
    token, _ = _open(client, "cred-author-high-entropy")
    response = client.delete(G.API_PREFIX + "/sessions/current",
                             headers=_auth(token))
    assert response.status_code == 204
    assert response.content == b""
    assert journal.authenticate(token) is None
    retry = client.delete(G.API_PREFIX + "/sessions/current",
                          headers=_auth(token))
    assert retry.status_code == 401
    assert retry.json()["code"] == "SESSION_INVALID"


def test_revoke_current_si_la_sesion_cambia_entre_middleware_y_handler_da_401(
        harness, monkeypatch):
    journal, client = harness
    token, _ = _open(client, "cred-author-high-entropy")

    def expires_between_checks(candidate, reason="revoked"):
        assert candidate == token
        assert reason == "self_revoked"
        raise C.AuthError("sesion invalidada antes de la revocacion")

    monkeypatch.setattr(journal, "revoke_current", expires_between_checks)
    response = client.delete(G.API_PREFIX + "/sessions/current",
                             headers=_auth(token))
    assert response.status_code == 401
    assert response.json()["code"] == "SESSION_INVALID"
    assert journal.authenticate(token) is not None


def test_una_mutacion_cruza_el_middleware_de_capability_una_sola_vez(
        harness, monkeypatch):
    _, client = harness
    token, _ = _open(client, "cred-author-high-entropy")
    real = G._mutation_token
    calls = []

    def counted(request, journal, operation, policy):
        calls.append(operation)
        return real(request, journal, operation, policy)

    monkeypatch.setattr(G, "_mutation_token", counted)
    response = _event(client, token, key="single-middleware")
    assert response.status_code == 202
    assert calls == ["events.accept"]


def test_middleware_cierra_cualquier_mutacion_nueva_sin_declaracion(harness):
    journal, _ = harness
    router = G.create_native_router(journal)

    @router.post("/forgotten-policy")
    async def forgotten_policy():
        return {"unsafe": True}

    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)
    token = journal.open_session("cred-author-high-entropy").token
    with TestClient(app) as client:
        response = client.post(G.API_PREFIX + "/forgotten-policy",
                               headers=_auth(token))
    assert response.status_code == 503
    assert response.json()["code"] == "CAPABILITY_POLICY_UNDECLARED"


def test_leases_y_comandos_cruzan_gateway_sin_duplicar_dominio(harness):
    journal, client = harness
    author, _ = _open(client, "cred-author-high-entropy")
    worker, _ = _open(client, "cred-worker-high-entropy")
    lease = client.post(G.API_PREFIX + "/leases/resource-1",
                        headers=_auth(author), json={"ttl_s": 60})
    assert lease.status_code == 201 and lease.json()["fencing_token"] == 1
    # El nucleo no tiene renew estricto: el borde lo declara y no reacquire.
    renew = client.put(G.API_PREFIX + "/leases/resource-1",
                       headers=_auth(author), json={"ttl_s": 60})
    assert renew.status_code == 501
    assert renew.json()["code"] == "CORE_RENEW_NOT_STRICT"
    assert client.delete(G.API_PREFIX + "/leases/resource-1",
                         headers=_auth(author)).status_code == 204

    submitted = client.post(G.API_PREFIX + "/commands", headers=_auth(author),
                            json={"workstream_id": "ws", "revision": 1,
                                  "payload": {"action": "build"}})
    assert submitted.status_code == 202
    command_id = submitted.json()["command_id"]
    for state in ("received", "executing", "succeeded"):
        moved = client.post(G.API_PREFIX + f"/commands/{command_id}/transitions",
                            headers=_auth(worker), json={"state": state})
        assert moved.status_code == 200, moved.text
    assert journal.may_execute(worker, command_id) is False


def test_lecturas_no_scoped_del_core_se_mantienen_cerradas(harness):
    _, client = harness
    worker, _ = _open(client, "cred-worker-high-entropy")
    pending = client.get(G.API_PREFIX + "/outbox/pending", headers=_auth(worker))
    assert pending.status_code == 501
    assert pending.json()["code"] == "CORE_PENDING_NOT_SCOPED"
    executable = client.get(G.API_PREFIX + "/commands/cmd_" + "0" * 32 + "/executable",
                            headers=_auth(worker))
    assert executable.status_code == 501
    assert executable.json()["code"] == "CORE_COMMAND_SCOPE_MISSING"


def test_normalizador_v8_hace_principal_y_caps_explicitos_sin_filtrar_secretos(tmp_path):
    journal = C.Journal(str(tmp_path / "j.sqlite"), pepper=b"pepper")
    raw = {"top-secret-credential": {"rol": "be", "carril": "lane-a"}}
    normalized = G.normalize_v8_credential_map(journal, raw)
    spec = normalized["top-secret-credential"]
    assert spec["principal"].startswith("v8:")
    assert spec["principal"] != "be"
    assert spec["capabilities"] == ()
    assert "top-secret-credential" not in repr(spec)
    with pytest.raises(ValueError) as error:
        G.normalize_v8_credential_map(
            journal,
            {"secret": {"rol": "be", "carril": "lane-a",
                        "capabilities": [G.CAP_EVENT_WRITER]}},
        )
    assert "secret" not in str(error.value)


def test_todas_las_excepciones_del_journal_tienen_mapeo_explicito():
    classes = {
        cls for _, cls in vars(C).items()
        if inspect.isclass(cls) and issubclass(cls, C.JournalError)
    }
    mapped = {cls for cls, _, _ in G._ERRORS}
    # v7 añade snapshot obligatorio y conflictos de observación/organización/
    # recovery. Cada clase exige su propia fila; heredar una captura genérica
    # no acredita el código público de una subclase.
    # G8 añade una restricción explícita del modo de apertura del Journal.
    assert len(classes) == 41
    assert classes == mapped
    base = G._journal_error(C.JournalError("sensitive internals"))
    assert base.status_code == 500
    assert b"sensitive internals" not in base.body


@pytest.mark.parametrize("exception,code,status,message", [
    (C.MigrationSnapshotRequired, "MIGRATION_SNAPSHOT_REQUIRED", 503,
     "journal no disponible"),
    (C.OpenModeRestricted, "JOURNAL_OPEN_MODE_RESTRICTED", 503,
     "journal no disponible"),
    (C.ObservationSequenceConflict, "OBSERVATION_SEQUENCE_CONFLICT", 409,
     "operacion rechazada por el journal"),
    (C.OrganizationConflict, "ORGANIZATION_CONFLICT", 409,
     "operacion rechazada por el journal"),
    (C.RecoveryConflict, "RECOVERY_CONFLICT", 409,
     "operacion rechazada por el journal"),
])
def test_excepciones_v7_conservan_codigo_y_status_sin_reflejar_internos(
        exception, code, status, message):
    response = G._journal_error(exception("sensitive internals"))
    assert response.status_code == status
    assert json.loads(response.body) == {
        "code": code,
        "message": message,
    }


def test_las_cinco_excepciones_core_tienen_status_y_code_wire_explicitos():
    casos = (
        (C.GrammarRejected("x"), "GRAMMAR_REJECTED", 422),
        (C.GrammarUnavailable("x"), "GRAMMAR_UNAVAILABLE", 503),
        (C.RecipientUnresolved("x"), "RECIPIENT_UNRESOLVED", 422),
        (C.ResourceLimitExceeded(
            "x", dimension="bytes", field="intent", limit=10,
            seen_at_least=11), "RESOURCE_LIMIT_EXCEEDED", 413),
        (C.KeyCharsetRejected("x"), "KEY_CHARSET_REJECTED", 422),
    )
    for exc, code, status in casos:
        response = G._journal_error(exc)
        assert response.status_code == status
        assert json.loads(response.body)["code"] == code


def test_resource_limit_exceeded_publica_solo_atributos_cerrados():
    exc = C.ResourceLimitExceeded(
        "secreto intent.meta.ruta", dimension="nodes", field="request",
        limit=8192, seen_at_least=8193)
    exc.receipt_id = "receipt-safe"
    response = G._journal_error(exc)
    assert response.status_code == 413
    error = json.loads(response.body)
    assert error == {
        "code": "RESOURCE_LIMIT_EXCEEDED",
        "message": "operacion rechazada por el journal",
        "receipt_id": "receipt-safe",
        "dimension": "nodes",
        "field": "request",
        "limit": 8192,
        "seen_at_least": 8193,
    }
    assert b"intent.meta.ruta" not in response.body


@pytest.mark.parametrize("changes", [
    {"dimension": "other"},
    {"field": "intent.secret"},
    {"limit": True},
    {"seen_at_least": 10},
])
def test_resource_limit_malformado_falla_cerrado_sin_reflejar(changes):
    fields = {"dimension": "bytes", "field": "intent", "limit": 10,
              "seen_at_least": 11}
    fields.update(changes)
    exc = C.ResourceLimitExceeded("secreto", **fields)
    response = G._journal_error(exc)
    assert response.status_code == 500
    assert json.loads(response.body) == {
        "code": "JOURNAL_INTERNAL_ERROR",
        "message": "fallo interno del journal",
    }
    assert b"secreto" not in response.body


def test_413_core_lleva_error_tipado_y_recibo_y_difiere_del_transporte(harness):
    _, client = harness
    token, _ = _open(client, "cred-author-high-entropy")
    response = client.post(
        G.API_PREFIX + "/events",
        headers=_auth(token, **{"Idempotency-Key": "semantic-limit"}),
        json={**INTENT, "body": "x" * 70_000},
    )
    assert response.status_code == 413
    error = response.json()
    assert error["code"] == "RESOURCE_LIMIT_EXCEEDED"
    assert error["receipt_id"]
    assert error["dimension"] == "bytes" and error["field"] == "intent"
    assert error["limit"] < error["seen_at_least"]


def test_si_coordination_se_renombra_el_producto_pone_rojo(tmp_path):
    """Simula el artefacto con gateway presente y core renombrado/ausente."""
    shutil.copy(G.__file__, tmp_path / "native_gateway.py")
    env = {"PYTHONPATH": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "-I", "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); import native_gateway",
         str(tmp_path)],
        cwd=tmp_path, env=env, text=True, capture_output=True,
    )
    assert result.returncode != 0
    assert "coordination" in result.stderr


def test_modulo_es_desacoplado_de_servicio():
    G.assert_gateway_surface_is_decoupled()
    source = open(G.__file__, encoding="utf-8").read()
    imports = [node for node in ast.walk(ast.parse(source))
               if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert not any(
        (isinstance(node, ast.ImportFrom) and node.module == "servicio") or
        (isinstance(node, ast.Import) and any(
            alias.name == "servicio" for alias in node.names))
        for node in imports
    )
