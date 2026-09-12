"""Contrato mínimo que consumen gateway, proyector e indexador tras integrar M1."""
from __future__ import annotations

import pytest

import coordination as C
from ._arnes import CAPS_RUNTIME, INTENT, journal, sesion, sesiones


def test_session_view_expone_identidad_y_capacidades_resueltas_por_servidor(tmp_path):
    j = journal(tmp_path)
    issued = sesion(j, role="backend", principal=None,
                     capabilities=(C.CAP_INDEXER, C.CAP_OUTBOX_WORKER))
    view = j.authenticate(issued.token)

    assert view is not None
    assert (view.principal_id, view.principal, view.principal_source,
            view.role, view.lane, view.runtime_instance) == (
                issued.principal_id, "backend", "derived_from_role",
                "backend", "llminbox", issued.runtime_instance)
    assert view.capabilities == (C.CAP_INDEXER, C.CAP_OUTBOX_WORKER)
    assert issued.capabilities == view.capabilities
    j.dispose()


@pytest.mark.parametrize("campo", [
    "actor", "principal", "principal_id", "principal_source", "source", "role", "lane",
    "runtime_instance", "capabilities", "attestation",
])
def test_evento_rechaza_toda_atribucion_que_intente_declarar_el_cliente(
        tmp_path, campo):
    j = journal(tmp_path)
    s = sesion(j)
    intent = {**INTENT, campo: "inventado"}

    with pytest.raises(C.AttributionRejected):
        j.accept_event(s.token, idempotency_key=f"attr-{campo}", intent=intent)

    with j._lectura() as (con, _):
        assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM idempotency").fetchone()[0] == 0
    j.dispose()


def test_projection_job_es_completo_y_un_replay_no_duplica_efecto(tmp_path):
    j = journal(tmp_path, attestation={"build_sha": "abc"})
    s = sesion(j, principal="alice", role="backend", capabilities=CAPS_RUNTIME)
    trace = {"trace_id": "a" * 32, "span_id": "b" * 16}
    first = j.accept_event(s.token, idempotency_key="same", intent=INTENT,
                           ledger="llminbox", trace=trace)
    replay = j.accept_event(s.token, idempotency_key="same", intent=dict(INTENT),
                            ledger="llminbox")

    assert replay.replayed is True
    assert (replay.event_id, replay.receipt_id) == (first.event_id, first.receipt_id)
    job = j.claim_outbox(s.token)
    assert isinstance(job, C.ProjectionJob)
    assert not isinstance(job, C.LegacyOutboxItem)
    assert (job.event_id, job.receipt_id) == (first.event_id, first.receipt_id)
    assert job.recipients == tuple(INTENT["to"])
    assert job.occurred_at == first.occurred_at
    assert job.payload_sha256 == first.payload_sha == job.payload_sha
    assert job.attestation == {"build_sha": "abc", "trace": trace}
    assert job.trace == trace
    assert (job.principal_id, job.principal, job.principal_source, job.role, job.lane,
            job.runtime_instance) == (
                s.principal_id, "alice", "explicit", "backend", "llminbox",
                s.runtime_instance)
    with j._lectura() as (con, _):
        assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
    j.dispose()


def test_outbox_item_legacy_conserva_constructor_sin_debilitar_projection_job():
    payload = {"intent": {"type": "message"}}
    antiguo = C.OutboxItem("evt_legacy", "ledger", 2, "claim", payload)

    assert isinstance(antiguo, C.LegacyOutboxItem)
    assert (antiguo.event_id, antiguo.ledger, antiguo.attempts,
            antiguo.claim_token, antiguo.payload) == (
                "evt_legacy", "ledger", 2, "claim", payload)
    with pytest.raises(TypeError):
        C.ProjectionJob("evt_incompleto", "ledger", 1, "claim", payload)


def test_capacidades_de_worker_indexer_y_command_se_revalidan_en_su_tx(tmp_path):
    j = journal(tmp_path)
    sin, worker, indexer, command = sesiones(j,
        {"credential": "none", "principal": "none", "capabilities": ()},
        {"credential": "worker", "principal": "worker",
         "capabilities": (C.CAP_OUTBOX_WORKER,)},
        {"credential": "indexer", "principal": "indexer",
         "capabilities": (C.CAP_INDEXER,)},
        {"credential": "command", "principal": "command",
         "capabilities": (C.CAP_COMMAND_WORKER,)})

    event = j.accept_event(sin.token, idempotency_key="event", intent=INTENT,
                           ledger="llminbox")
    with pytest.raises(C.PolicyDenied):
        j.claim_outbox(sin.token)
    job = j.claim_outbox(worker.token)
    j.mark_materialized(worker.token, event.event_id, entry_eid="e" * 64,
                        ledger="llminbox", claim_token=job.claim_token)
    with pytest.raises(C.PolicyDenied):
        j.mark_indexed(worker.token, event.event_id)
    j.mark_indexed(indexer.token, event.event_id)

    cid, _ = j.submit_command(sin.token, workstream_id="ws", revision=1,
                              payload={"verb": "do"})
    with pytest.raises(C.PolicyDenied):
        j.advance_command(sin.token, cid, "received")
    j.advance_command(command.token, cid, "received")
    j.dispose()


def test_misma_clave_con_otro_contenido_no_crea_segundo_efecto(tmp_path):
    j = journal(tmp_path)
    s = sesion(j)
    first = j.accept_event(s.token, idempotency_key="same", intent=INTENT,
                           ledger="llminbox")
    with pytest.raises(C.IdempotencyConflict):
        j.accept_event(s.token, idempotency_key="same",
                       intent={**INTENT, "body": "otro"}, ledger="llminbox")
    with j._lectura() as (con, _):
        assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
        row = con.execute("SELECT event_id, receipt_id FROM idempotency").fetchone()
        assert (row["event_id"], row["receipt_id"]) == (
            first.event_id, first.receipt_id)
    j.dispose()
