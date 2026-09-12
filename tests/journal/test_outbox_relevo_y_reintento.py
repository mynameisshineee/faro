"""El trabajador relevado no puede tocar nada — ni marcando, ni FALLANDO.

La vía del fallo es la que menos se audita y la que más daño hace: reportar un
error parece inofensivo, y en la versión anterior limpiaba `lease_until`, o sea
le quitaba el arriendo al trabajador VIVO que estaba a mitad de su intento.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest

import coordination as C
from ._arnes import INTENT, Reloj, journal, sesion


def _evento(j):
    """Devuelve (sesión, aceptación): el outbox ya no admite identidad declarada,
    así que el trabajador necesita una sesión igual que el que escribe."""
    s = sesion(j)
    return s, j.accept_event(s.token, idempotency_key="k",
                             intent={**INTENT, "to": ["be"]}, ledger="llminbox")


@contextmanager
def _avanza_despues_del_lock(tx_factory, reloj, segundos):
    """Simula espera por el writer lock sin sleeps ni hilos oportunistas."""
    tx = tx_factory()
    with tx as con:
        reloj.avanza(segundos)
        yield con


def test_claim_mide_el_reloj_despues_de_adquirir_el_writer_lock(tmp_path, monkeypatch):
    reloj = Reloj()
    j = journal(tmp_path, reloj=reloj)
    w, _ = _evento(j)
    real_tx = j._tx

    def tx_con_espera():
        return _avanza_despues_del_lock(real_tx, reloj, 61)

    monkeypatch.setattr(j, "_tx", tx_con_espera)
    item = j.claim_outbox(w.token, lease_s=60)

    assert item is not None
    lease_until = j._connect().execute(
        "SELECT lease_until FROM outbox WHERE event_id=?", (item.event_id,)
    ).fetchone()["lease_until"]
    assert lease_until == reloj.t + 60
    monkeypatch.setattr(j, "_tx", real_tx)
    j.dispose()


@pytest.mark.parametrize(
    "lease_s", [True, False, 0, -1, C.MAX_OUTBOX_LEASE_S + 1, 1.0, "60", None]
)
def test_claim_rechaza_lease_no_entero_positivo_sin_mutar(tmp_path, lease_s):
    j = journal(tmp_path)
    w, a = _evento(j)
    before = dict(j._connect().execute(
        "SELECT * FROM outbox WHERE event_id=?", (a.event_id,)).fetchone())
    with pytest.raises(C.OperationInvalid):
        j.claim_outbox(w.token, lease_s=lease_s)
    after = dict(j._connect().execute(
        "SELECT * FROM outbox WHERE event_id=?", (a.event_id,)).fetchone())
    assert after == before
    j.dispose()


def test_fallo_mide_expiracion_despues_de_adquirir_el_writer_lock(
        tmp_path, monkeypatch):
    reloj = Reloj()
    j = journal(tmp_path, reloj=reloj)
    w, a = _evento(j)
    item = j.claim_outbox(w.token, lease_s=60)
    reloj.avanza(59)
    before = dict(j._connect().execute(
        "SELECT * FROM outbox WHERE event_id=?", (a.event_id,)).fetchone())
    real_tx = j._tx

    def tx_con_espera():
        return _avanza_despues_del_lock(real_tx, reloj, 2)

    monkeypatch.setattr(j, "_tx", tx_con_espera)
    with pytest.raises(C.FencingConflict, match="VENCIO"):
        j.mark_outbox_failed(w.token, a.event_id, error="PROJECTOR_IO_ERROR",
                             claim_token=item.claim_token)
    after = dict(j._connect().execute(
        "SELECT * FROM outbox WHERE event_id=?", (a.event_id,)).fetchone())
    assert after == before
    j.dispose()


def test_A_caduca_B_reclama_y_el_fallo_de_A_deja_INTACTO_el_lease_de_B(tmp_path):
    reloj = Reloj()
    j = journal(tmp_path, reloj=reloj)
    w, a = _evento(j)

    A = j.claim_outbox(w.token, lease_s=60)
    reloj.avanza(61)                       # a A se le vence el arriendo
    w2 = j.open_session("cred-A")          # OTRO runtime del mismo principal
    B = j.claim_outbox(w2.token, lease_s=60)
    assert B is not None and B.claim_token != A.claim_token

    antes = dict(j._connect().execute(
        "SELECT lease_until, lease_by, claim_token, next_attempt, state"
        "  FROM outbox WHERE event_id=?", (a.event_id,)).fetchone())

    # A vuelve del limbo y reporta SU fallo. No es suyo: se le rechaza.
    with pytest.raises(C.FencingConflict):
        j.mark_outbox_failed(w.token, a.event_id, error="lo mio peto",
                             claim_token=A.claim_token)
    # …y tampoco puede marcar materializado.
    with pytest.raises(C.FencingConflict):
        j.mark_materialized(w.token, a.event_id, entry_eid="e" * 64, ledger="llminbox",
                            claim_token=A.claim_token)

    despues = dict(j._connect().execute(
        "SELECT lease_until, lease_by, claim_token, next_attempt, state"
        "  FROM outbox WHERE event_id=?", (a.event_id,)).fetchone())
    assert despues == antes, "el rezagado tocó el arriendo del vivo"
    assert despues["lease_by"] == w2.runtime_instance   # derivado, no declarado

    # ⊕ B, que sí es el dueño, materializa sin problema.
    j.mark_materialized(w2.token, a.event_id, entry_eid="e" * 64, ledger="llminbox",
                        claim_token=B.claim_token)
    assert j.receipt_for_event(w2.token, a.event_id)["current_state"] == "materialized"
    j.close()


def test_un_fallo_deja_transicion_OBSERVABLE_y_NO_impide_el_reintento(tmp_path):
    """Un fallo que cerrara el recibo convertiría un problema transitorio en
    definitivo. Se registra como historia; el estado se queda en `accepted`."""
    reloj = Reloj()
    j = journal(tmp_path, reloj=reloj)
    w, a = _evento(j)
    rid = j.receipt_for_event(w.token, a.event_id)["receipt_id"]

    A = j.claim_outbox(w.token, lease_s=60)
    j.mark_outbox_failed(w.token, a.event_id, error="ledger :ro", claim_token=A.claim_token)

    estados = [t["state"] for t in j.transitions(w.token, rid)]
    assert estados == ["accepted", "materialization_failed"]      # observable
    detail = j.transitions(w.token, rid)[1]["detail"]
    assert "PROJECTOR_ERROR" in detail
    assert "ledger :ro" not in detail
    assert j.receipt_for_event(w.token, a.event_id)["current_state"] == "accepted"
    assert j.pending_outbox(w.token) == 1                                # sigue en cola

    # El reintento llega y ESTA VEZ materializa: el fallo no cerró la puerta.
    reloj.avanza(301)   # supera el backoff interno
    w2 = j.open_session("cred-A")
    B = j.claim_outbox(w2.token, lease_s=60)
    assert B is not None, "el fallo dejó el item inalcanzable"
    j.mark_materialized(w2.token, a.event_id, entry_eid="f" * 64, ledger="llminbox",
                        claim_token=B.claim_token)
    assert [t["state"] for t in j.transitions(w2.token, rid)] == \
        ["accepted", "materialization_failed", "materialized"]
    assert j.pending_outbox(w.token) == 0
    j.close()


def test_materializar_citando_OTRO_ledger_se_rechaza(tmp_path):
    """Un puntero de proyección falso es peor que no tenerlo: parece verificado."""
    j = journal(tmp_path)
    w, a = _evento(j)
    item = j.claim_outbox(w.token, lease_s=60)
    with pytest.raises(C.JournalError):
        j.mark_materialized(w.token, a.event_id, entry_eid="e" * 64, ledger="otro-ledger",
                            claim_token=item.claim_token)
    assert j.receipt_for_event(w.token, a.event_id)["current_state"] == "accepted"
    j.mark_materialized(w.token, a.event_id, entry_eid="e" * 64, ledger="llminbox",
                        claim_token=item.claim_token)          # ⊕ el bueno sí
    j.close()


def test_sin_ficha_no_se_marca_nada(tmp_path):
    j = journal(tmp_path)
    w, a = _evento(j)
    j.claim_outbox(w.token, lease_s=60)
    with pytest.raises(C.FencingConflict):
        j.mark_materialized(w.token, a.event_id, entry_eid="e" * 64, ledger="llminbox",
                            claim_token="clm_inventada")
    with pytest.raises(C.FencingConflict):
        j.mark_outbox_failed(w.token, a.event_id, error="x", claim_token="clm_inventada")
    assert j.receipt_for_event(w.token, a.event_id)["current_state"] == "accepted"
    j.close()


def test_indexed_y_delivered_no_se_pueden_adelantar(tmp_path):
    """Los tres hechos tienen tres dueños. Ninguno puede afirmar el del siguiente."""
    j = journal(tmp_path)
    w, a = _evento(j)
    with pytest.raises(C.JournalError):
        j.mark_indexed(w.token, a.event_id)                     # sin materializar
    with pytest.raises(C.JournalError):
        j.mark_delivered(w.token, a.event_id)
    item = j.claim_outbox(w.token, lease_s=60)
    j.mark_materialized(w.token, a.event_id, entry_eid="e" * 64, ledger="llminbox",
                        claim_token=item.claim_token)
    with pytest.raises(C.JournalError):
        j.mark_delivered(w.token, a.event_id)   # sin indexar
    j.mark_indexed(w.token, a.event_id, index_ref="idx-1")
    j.mark_delivered(w.token, a.event_id, ack_ref="ack-1")
    assert [t["state"] for t in j.transitions(w.token,
        j.receipt_for_event(w.token, a.event_id)["receipt_id"])] == \
        ["accepted", "materialized", "indexed", "delivered"]
    j.close()
