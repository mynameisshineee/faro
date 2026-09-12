"""Aceptación atómica, idempotencia, stream de recibos, outbox y causalidad."""
from __future__ import annotations

import pytest

import coordination as C
from ._arnes import INTENT, journal, sesion, sesiones


def test_aceptar_escribe_evento_recibo_transicion_outbox_e_idempotencia(tmp_path):
    """Atomicidad: las cinco filas nacen en la MISMA transacción."""
    j = journal(tmp_path)
    s = sesion(j)
    a = j.accept_event(s.token, idempotency_key="k1", intent=INTENT, ledger="llminbox")
    con = j._connect()
    assert con.execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == 1
    assert con.execute("SELECT COUNT(*) c FROM receipts").fetchone()["c"] == 1
    assert con.execute("SELECT COUNT(*) c FROM outbox WHERE state='pending'"
                       ).fetchone()["c"] == 1
    assert con.execute("SELECT COUNT(*) c FROM idempotency").fetchone()["c"] == 1
    t = j.transitions(s.token, a.receipt_id)
    assert [x["state"] for x in t] == ["accepted"]
    assert a.replayed is False
    j.close()


def test_accepted_no_es_materialized_y_el_entry_eid_llega_DESPUES(tmp_path):
    """`accepted` = el journal posee el evento. NO dice que el markdown lo tenga.

    Y `entry_eid` no existe al aceptar: entra en el stream al materializar, y
    nunca como identidad.
    """
    j = journal(tmp_path)
    s = sesion(j)
    a = j.accept_event(s.token, idempotency_key="k1", intent=INTENT, ledger="llminbox")
    assert j.receipt(s.token, a.receipt_id)["current_state"] == "accepted"
    crudo = str(dict(j._connect().execute("SELECT * FROM events").fetchone()))
    assert "entry_eid" not in crudo

    item = j.claim_outbox(s.token, lease_s=60)
    j.mark_materialized(s.token, a.event_id, entry_eid="e" * 64, ledger="llminbox",
                        claim_token=item.claim_token)
    assert j.receipt(s.token, a.receipt_id)["current_state"] == "materialized"
    estados = [t["state"] for t in j.transitions(s.token, a.receipt_id)]
    assert estados == ["accepted", "materialized"]      # append-only, no sobrescribe
    assert "e" * 64 in j.transitions(s.token, a.receipt_id)[1]["detail"]
    assert j.pending_outbox(s.token) == 0
    j.close()


def test_misma_clave_mismo_cuerpo_devuelve_el_recibo_ORIGINAL(tmp_path):
    j = journal(tmp_path)
    s = sesion(j)
    a = j.accept_event(s.token, idempotency_key="k", intent=INTENT, ledger="llminbox")
    b = j.accept_event(s.token, idempotency_key="k", intent=INTENT, ledger="llminbox")
    assert (b.event_id, b.receipt_id) == (a.event_id, a.receipt_id)
    assert b.replayed is True
    assert j._connect().execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == 1
    j.close()


def test_misma_clave_cuerpo_distinto_es_conflicto_SIN_mutacion(tmp_path):
    """La parte que más importa no es el error: es el CERO mutación."""
    j = journal(tmp_path)
    s = sesion(j)
    j.accept_event(s.token, idempotency_key="k", intent=INTENT, ledger="llminbox")
    antes = _foto(j)
    with pytest.raises(C.IdempotencyConflict):
        j.accept_event(s.token, idempotency_key="k",
                       intent={**INTENT, "body": "otro cuerpo"}, ledger="llminbox")
    assert _foto(j) == antes
    j.close()


def test_la_clave_esta_ACOTADA_por_principal(tmp_path):
    """Sin `principal_id` en la PK, la clave de un agente devuelve el recibo de
    otro. ⊖: quitarlo de la PK hace que el segundo `accept` salga `replayed`."""
    j = journal(tmp_path)
    a, b = sesiones(j,
        {"credential": "cred-A", "principal": "p-a"},
        {"credential": "cred-B", "principal": "p-b"})
    ra = j.accept_event(a.token, idempotency_key="misma", intent=INTENT, ledger="l")
    rb = j.accept_event(b.token, idempotency_key="misma", intent=INTENT, ledger="l")
    assert ra.event_id != rb.event_id
    assert rb.replayed is False
    j.close()


def test_la_peticion_no_puede_elegir_su_atribucion_ni_la_atestacion(tmp_path):
    """Se RECHAZA, no se ignora en silencio (ADR §Identity model + falsador 26)."""
    j = journal(tmp_path, attestation={"build_sha": "dc28897"})
    s = sesion(j)
    for campo in ("principal", "role", "lane", "runtime_instance", "attestation"):
        with pytest.raises(C.AttributionRejected):
            j.accept_event(s.token, idempotency_key=f"k-{campo}",
                           intent={**INTENT, campo: "mio"}, ledger="l")
    a = j.accept_event(s.token, idempotency_key="ok", intent=INTENT, ledger="l")
    fila = j._connect().execute("SELECT attestation FROM events WHERE event_id=?",
                                (a.event_id,)).fetchone()
    assert "dc28897" in fila["attestation"]        # la del SERVIDOR
    j.close()


def test_una_causa_nativa_inexistente_se_rechaza_sin_mutacion(tmp_path):
    j = journal(tmp_path)
    s = sesion(j)
    antes = _foto(j)
    with pytest.raises(C.CauseRejected):
        j.accept_event(s.token, idempotency_key="k", intent=INTENT, ledger="l",
                       causes=["evt_que_no_existe"])
    assert _foto(j) == antes
    j.close()


def test_un_hash_bridge_como_causa_NATIVA_se_rechaza_y_como_externa_se_acepta(tmp_path):
    """Falsador 24: las causas nativas llevan FK; las externas son tipadas, sin
    FK, y NO elevan la autoridad del material bridge."""
    j = journal(tmp_path)
    s = sesion(j)
    eid = "a" * 64                                   # un entry_eid de markdown
    with pytest.raises(C.CauseRejected):
        j.accept_event(s.token, idempotency_key="k1", intent=INTENT, ledger="l",
                       causes=[eid])
    a = j.accept_event(s.token, idempotency_key="k2", intent=INTENT, ledger="l",
                       external_causes=[{"ledger": "llminbox", "entry_eid": eid}])
    fila = j._connect().execute(
        "SELECT * FROM external_causes WHERE child_id=?", (a.event_id,)).fetchone()
    assert fila["entry_eid"] == eid and fila["authority"] == "false"
    j.close()


def test_una_causa_nativa_de_verdad_sí_entra_y_con_FK(tmp_path):
    """Control POSITIVO del test anterior: sin esto, «rechaza todo» pasaría igual."""
    j = journal(tmp_path)
    s = sesion(j)
    primero = j.accept_event(s.token, idempotency_key="k1", intent=INTENT, ledger="l")
    segundo = j.accept_event(s.token, idempotency_key="k2",
                             intent={**INTENT, "head": "segundo"}, ledger="l",
                             causes=[primero.event_id])
    fila = j._connect().execute("SELECT * FROM event_causes WHERE event_id=?",
                                (segundo.event_id,)).fetchone()
    assert fila["cause_event_id"] == primero.event_id
    j.close()


def test_una_tormenta_de_rechazos_no_produce_una_fila_por_peticion(tmp_path):
    """Falsador 22. Bajo cuota: recibo propio. Por encima: UNO agregado + contador."""
    # Reloj CONGELADO: con el real, 50 denegaciones pueden cruzar la frontera del
    # cubo de 300 s en una corrida lenta, la cuota se renueva y el test falla por
    # el calendario en vez de por el código. Lo vi fallar sólo dentro de la suite
    # completa, que es la forma más cara de aprenderlo.
    from ._arnes import Reloj
    j = journal(tmp_path, denial_quota=5, reloj=Reloj())
    b = j.bind_credential("cred-A", principal="p", role="be", lane="llminbox")
    for _ in range(50):
        j._record_denial(b.principal_id, "FENCING_CONFLICT", lane="llminbox",
                         runtime_instance="rti_x")
    con = j._connect()
    assert con.execute("SELECT COUNT(*) c FROM denials").fetchone()["c"] == 5
    agg = con.execute("SELECT * FROM denial_aggregates").fetchone()
    assert agg["suppressed"] == 45
    total = con.execute("SELECT COUNT(*) c FROM receipts WHERE subject_kind LIKE 'denial%'"
                        ).fetchone()["c"]
    assert total == 6, f"{total} recibos durables para 50 rechazos"
    j.close()


def test_el_outbox_se_arrienda_y_el_arriendo_no_se_pisa(tmp_path):
    j = journal(tmp_path)
    s = sesion(j)
    j.accept_event(s.token, idempotency_key="k", intent=INTENT, ledger="llminbox")
    uno = j.claim_outbox(s.token, lease_s=60)
    assert uno is not None and uno.ledger == "llminbox"
    otro = j.open_session("cred-A")                      # OTRO runtime de verdad
    assert j.claim_outbox(otro.token, lease_s=60) is None  # arrendado: no lo coge
    j.close()


def _foto(j) -> dict:
    """Estado de DOMINIO. A propósito NO cuenta `receipts` ni
    `receipt_transitions`: un rechazo ahora deja recibo de denegación, y ése es
    el comportamiento que se quiere. «Cero mutación» significa que no entró el
    efecto, no que no quedara rastro — confundirlos haría que la auditoría
    rompiera el test que protege la atomicidad."""
    con = j._connect()
    return {t: con.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
            for t in ("events", "outbox", "idempotency", "event_causes",
                      "external_causes")}
