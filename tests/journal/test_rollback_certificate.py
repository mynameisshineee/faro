"""Contrato mínimo y falsadores de la certificación de rollback v0.9."""
from __future__ import annotations

from dataclasses import asdict
import json

import pytest

import coordination as C
from ._arnes import (CAPS_RUNTIME, INTENT, LANES, PEPPER, censo, journal,
                     sesiones)


EXACT_OPERATOR = (C.CAP_ADMISSION_OPERATOR,)


def _mount(tmp_path, *, max_attempts=1):
    j = journal(tmp_path, admision=False, max_attempts=max_attempts)
    writer, operator, extra, plain = sesiones(
        j,
        {"credential": "cred-writer", "principal": "writer", "role": "be",
         "lane": "llminbox", "capabilities": CAPS_RUNTIME},
        {"credential": "cred-rollback", "principal": "rollback", "role": "infra",
         "lane": "llminbox", "capabilities": EXACT_OPERATOR},
        {"credential": "cred-extra", "principal": "extra", "role": "infra",
         "lane": "llminbox",
         "capabilities": (C.CAP_ADMISSION_OPERATOR, C.CAP_OUTBOX_WORKER)},
        {"credential": "cred-plain", "principal": "plain", "role": "infra",
         "lane": "llminbox", "capabilities": ()},
    )
    return j, writer, operator, extra, plain


def _move_pair(j, operator, target, epochs, reason="DRAIN_FOR_ROLLBACK"):
    return j.transition_admissions(
        operator.token, target=target, expected_epochs=epochs,
        reason_code=reason)


def _epochs(states):
    return {state.verb: state.epoch for state in states}


def test_status_es_una_foto_de_las_dos_puertas_y_los_dos_contadores(tmp_path):
    j, writer, operator, _, _ = _mount(tmp_path)
    opened = _move_pair(
        j, operator, "open", {verb: 0 for verb in C.ADMISSION_VERBS}, "ROLLOUT")
    j.accept_event(writer.token, idempotency_key="pending", intent=INTENT,
                   ledger="llminbox")
    j.accept_event(
        writer.token, idempotency_key="failed",
        intent={**INTENT, "head": "otra entrega"}, ledger="llminbox")
    job = j.claim_outbox(writer.token)
    j.mark_outbox_failed(
        writer.token, job.event_id, error="PROJECTOR_IO_ERROR",
        claim_token=job.claim_token)

    trace = []
    con = j._connect()
    con.set_trace_callback(trace.append)
    try:
        status = j.rollback_status(operator.token)
    finally:
        con.set_trace_callback(None)

    assert [(a.verb, a.state, a.epoch) for a in status.admissions] == [
        (C.ADMISSION_VERBS[0], "open", opened[0].epoch),
        (C.ADMISSION_VERBS[1], "open", opened[1].epoch),
    ]
    assert (status.lane, status.outbox.pending, status.outbox.failed) == (
        "llminbox", 1, 1)
    assert status.durable_v == C.DURABLE_V
    assert status.certifiable is False

    sql = [statement.strip().upper() for statement in trace]
    assert sql.count("BEGIN") == 1 and sql.count("COMMIT") == 1
    assert sum("FROM ADMISSION_HISTORY" in statement for statement in sql) == 2
    assert sum("FROM OUTBOX O JOIN EVENTS E" in statement for statement in sql) == 1
    assert sum("FROM CREDENTIAL_BINDINGS" in statement for statement in sql) == 1
    j.close()


def test_certificado_exige_capacidad_exactamente_admission_operator(tmp_path):
    j, _, operator, extra, plain = _mount(tmp_path)
    _move_pair(j, operator, "sealed",
               {verb: 0 for verb in C.ADMISSION_VERBS})

    assert j.certify_rollback(operator.token).lane == "llminbox"
    for session in (extra, plain):
        with pytest.raises(C.PolicyDenied) as caught:
            j.certify_rollback(session.token)
        assert caught.value.receipt_id, "la denegación operacional deja recibo"
    with pytest.raises(C.AuthError):
        j.certify_rollback("token-inventado")
    j.close()


def test_cero_sin_ambas_puertas_selladas_no_certifica(tmp_path):
    j, _, operator, _, _ = _mount(tmp_path)
    status = j.rollback_status(operator.token)
    assert status.outbox.unresolved == 0
    assert {a.state for a in status.admissions} == {"closed"}
    with pytest.raises(C.AdmissionConflict) as caught:
        j.certify_rollback(operator.token)
    assert caught.value.receipt_id
    j.close()


def test_sellado_con_residuo_inyectado_no_certifica(tmp_path):
    """Falsador de defensa: ni siquiera un sello previo permite inventar cero.

    La API no puede crear este estado: el test simula daño externo restaurando
    a ``pending`` una fila ya materializada después del sello. La certificación
    vuelve a medir la cola y falla cerrada.
    """
    j, writer, operator, _, _ = _mount(tmp_path)
    opened = _move_pair(
        j, operator, "open", {verb: 0 for verb in C.ADMISSION_VERBS}, "ROLLOUT")
    accepted = j.accept_event(
        writer.token, idempotency_key="materializado", intent=INTENT,
        ledger="llminbox")
    job = j.claim_outbox(writer.token)
    j.mark_materialized(
        writer.token, accepted.event_id, entry_eid="e" * 64,
        ledger="llminbox", claim_token=job.claim_token)
    closed = _move_pair(j, operator, "closed", _epochs(opened))
    _move_pair(j, operator, "sealed", _epochs(closed))

    with j._tx() as con:
        con.execute("UPDATE outbox SET state='pending' WHERE event_id=?",
                    (accepted.event_id,))

    status = j.rollback_status(operator.token)
    assert {a.state for a in status.admissions} == {"sealed"}
    assert (status.outbox.pending, status.outbox.failed) == (1, 0)
    with pytest.raises(C.AdmissionConflict):
        j.certify_rollback(operator.token)
    j.close()


def test_certificado_es_acotado_y_no_expone_token_ni_path(tmp_path):
    j, _, operator, _, _ = _mount(tmp_path)
    sealed = _move_pair(j, operator, "sealed",
                        {verb: 0 for verb in C.ADMISSION_VERBS})
    certificate = j.certify_rollback(operator.token)

    assert [(a.verb, a.state, a.epoch) for a in certificate.admissions] == [
        (state.verb, "sealed", state.epoch) for state in sealed]
    assert certificate.outbox.unresolved == 0
    assert certificate.durable_v == C.DURABLE_V
    payload = asdict(certificate)
    assert set(payload) == {
        "lane", "admissions", "outbox", "durable_v", "certified_at"}
    rendered = json.dumps(payload, sort_keys=True)
    assert operator.token not in rendered
    assert str(tmp_path) not in rendered
    assert "credential" not in rendered and "principal" not in rendered
    j.close()


def test_otro_carril_no_pesa_en_el_certificado(tmp_path):
    j = journal(tmp_path, admision=False, max_attempts=1)
    op_a, op_b, writer_b = sesiones(
        j,
        {"credential": "op-a", "principal": "op-a", "role": "infra",
         "lane": "carril-uno", "capabilities": EXACT_OPERATOR},
        {"credential": "op-b", "principal": "op-b", "role": "infra",
         "lane": "carril-dos", "capabilities": EXACT_OPERATOR},
        {"credential": "writer-b", "principal": "writer-b", "role": "be",
         "lane": "carril-dos", "capabilities": CAPS_RUNTIME},
    )
    _move_pair(j, op_a, "sealed", {verb: 0 for verb in C.ADMISSION_VERBS})
    _move_pair(j, op_b, "open", {verb: 0 for verb in C.ADMISSION_VERBS}, "ROLLOUT")
    j.accept_event(writer_b.token, idempotency_key="ajeno", intent=INTENT,
                   ledger="ledger-dos")

    certificate = j.certify_rollback(op_a.token)
    assert certificate.lane == "carril-uno"
    assert certificate.outbox.unresolved == 0
    assert j.rollback_status(op_b.token).outbox.pending == 1
    j.close()
