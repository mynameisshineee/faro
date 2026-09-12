"""Correctivos del NO-GO: hebras, allowlist, auditoría atribuible y outbox poison."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

import coordination as C
from ._arnes import GRAMATICA, INTENT, LANES, OPERADOR, PEPPER, Reloj, censo, journal, sesion, sesiones


# ── ① CONEXIÓN POR HEBRA ─────────────────────────────────────────────────────

def test_ocho_hebras_escriben_ocho_eventos_sin_un_solo_error(tmp_path):
    """Con una conexión compartida esto no es lento: es INCORRECTO.

    Dos hebras dentro del mismo `BEGIN IMMEDIATE` se intercalan las sentencias y
    el `COMMIT` de una cierra la transacción a medio escribir de la otra. El
    fallo no se ve como error, se ve como filas que faltan.
    """
    j = journal(tmp_path)
    s = sesion(j)

    def escribe(i):
        return j.accept_event(s.token, idempotency_key=f"k{i}",
                              intent={**INTENT, "head": f"h{i}"}, ledger="llminbox")

    with ThreadPoolExecutor(max_workers=8) as pool:
        aceptaciones = list(pool.map(escribe, range(8)))

    assert len({a.event_id for a in aceptaciones}) == 8
    assert all(a.replayed is False for a in aceptaciones)
    con = j._connect()
    assert con.execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == 8
    assert con.execute("SELECT COUNT(*) c FROM outbox").fetchone()["c"] == 8
    assert con.execute("SELECT COUNT(*) c FROM idempotency").fetchone()["c"] == 8
    j.close()


# ── ② ALLOWLIST CARRIL → LEDGER, FAIL-CLOSED ────────────────────────────────

def test_sin_allowlist_no_escribe_NADIE(tmp_path):
    """`None` no es «todo permitido»: es «nadie la configuró»."""
    j = C.Journal(str(tmp_path / "coordination.sqlite"), pepper=PEPPER, recipient_resolver=censo, grammar=GRAMATICA)
    j.initialize()
    s = sesion(j)
    with pytest.raises(C.LedgerNotAllowed):
        j.accept_event(s.token, idempotency_key="k", intent=INTENT, ledger="llminbox")
    assert j._connect().execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == 0
    j.close()


def test_un_ledger_de_otro_carril_no_escribe_NI_UN_BYTE(tmp_path):
    j = journal(tmp_path)
    a = sesion(j, "cred-A", principal="p-a", lane="carril-uno")
    antes = _dominio(j)
    with pytest.raises(C.LedgerNotAllowed):
        j.accept_event(a.token, idempotency_key="k", intent=INTENT,
                       ledger="ledger-dos")            # el del OTRO carril
    assert _dominio(j) == antes
    # ⊕ su propio destino sí entra: sin esto, «rechaza todo» pasaría igual.
    j.accept_event(a.token, idempotency_key="k2", intent=INTENT, ledger="ledger-uno")
    assert _dominio(j)["events"] == antes["events"] + 1
    j.close()


def test_con_un_solo_destino_el_ledger_se_DERIVA_y_no_se_pide(tmp_path):
    j = journal(tmp_path, lane_ledgers={"llminbox": ["llminbox"]})
    s = sesion(j)
    a = j.accept_event(s.token, idempotency_key="k", intent=INTENT)  # sin ledger
    fila = j._connect().execute("SELECT ledger FROM outbox WHERE event_id=?",
                                (a.event_id,)).fetchone()
    assert fila["ledger"] == "llminbox"
    j.close()


# ── ③ AUDITORÍA DE RECHAZOS: ATRIBUIBLE Y SIN MEZCLAR ───────────────────────

@pytest.mark.parametrize("motivo,disparo", [
    ("IDEMPOTENCY_CONFLICT",
     lambda j, s: (j.accept_event(s.token, idempotency_key="x", intent=INTENT,
                                  ledger="llminbox"),
                   j.accept_event(s.token, idempotency_key="x",
                                  intent={**INTENT, "body": "otro"},
                                  ledger="llminbox"))),
    ("CAUSE_REJECTED",
     lambda j, s: j.accept_event(s.token, idempotency_key="y", intent=INTENT,
                                 ledger="llminbox", causes=["evt_no"])),
    ("LEDGER_NOT_ALLOWED",
     lambda j, s: j.accept_event(s.token, idempotency_key="z", intent=INTENT,
                                 ledger="prohibido")),
    ("FENCED_PAIR_INVALID",
     lambda j, s: j.accept_event(s.token, idempotency_key="w", intent=INTENT,
                                 ledger="llminbox", fenced_resource="r")),
    ("FENCING_CONFLICT",
     lambda j, s: j.check_fence(s.token, "sin-lease", 1)),
    ("COMMAND_TRANSITION_INVALID",
     lambda j, s: j.advance_command(s.token, "cmd_no", "executing")),
])
def test_cada_rechazo_conocido_deja_denial_atribuible(tmp_path, motivo, disparo):
    j = journal(tmp_path)
    s = sesion(j)
    with pytest.raises(C.JournalError):
        disparo(j, s)
    fila = j._connect().execute(
        "SELECT principal_id, lane, runtime_instance, reason FROM denials"
        " ORDER BY at DESC LIMIT 1").fetchone()
    assert fila is not None, f"{motivo} no dejó rastro"
    assert fila["reason"] == motivo
    assert fila["principal_id"] == s.principal_id
    assert fila["lane"] == s.lane
    assert fila["runtime_instance"] == s.runtime_instance
    j.close()


def test_el_lease_conflict_tambien_se_audita(tmp_path):
    j = journal(tmp_path)
    a, b = sesiones(j,
        {"credential": "cred-A", "principal": "p-a"},
        {"credential": "cred-B", "principal": "p-b"})
    j.acquire_lease(a.token, "r", ttl_s=300)
    with pytest.raises(C.LeaseConflict):
        j.acquire_lease(b.token, "r", ttl_s=300)
    fila = j._connect().execute("SELECT * FROM denials ORDER BY at DESC LIMIT 1"
                                ).fetchone()
    assert fila["reason"] == "LEASE_CONFLICT"
    assert fila["principal_id"] == b.principal_id      # el que fue rechazado
    j.close()


def test_abrir_SESIONES_no_multiplica_la_cuota_de_denegaciones(tmp_path):
    """🔴 RETIRO mi regla anterior: la cuota NO puede ir por `runtime_instance`.

    En el correctivo previo la puse ahí para que el ruido de un runtime no
    consumiera la cuota de otro. El argumento era razonable y el efecto es una
    EVASIÓN: abrir sesiones no cuesta nada, así que 200 sesiones eran 200 cuotas
    y la cota dejaba de acotar. Aislar y acotar tiraban en direcciones opuestas y
    aquí manda acotar; el runtime sigue en la fila individual, que es donde sirve
    para investigar sin servir para eludir.

    El control positivo va al lado a propósito: 200 rechazos desde UNA sesión
    tienen que dar exactamente lo mismo que 200 sesiones con uno cada una. Si
    difieren, la cuota depende de algo que el llamante elige.
    """
    def cuenta(j, sesiones, por_sesion):
        for _ in range(sesiones):
            s = j.open_session("cred-A")
            for _ in range(por_sesion):
                j._record_denial(s.principal_id, "FENCING_CONFLICT",
                                 lane=s.lane, runtime_instance=s.runtime_instance)
        con = j._connect()
        return (con.execute("SELECT COUNT(*) c FROM denials").fetchone()["c"],
                con.execute("SELECT COUNT(*) c FROM denial_aggregates"
                            ).fetchone()["c"],
                con.execute("SELECT COALESCE(SUM(suppressed),0) s FROM"
                            " denial_aggregates").fetchone()["s"])

    j = journal(tmp_path, denial_quota=1)
    j.bind_credential("cred-A", principal="p", role="be", lane="llminbox")
    assert cuenta(j, sesiones=200, por_sesion=1) == (1, 1, 199)
    j.close()

    (tmp_path / "b").mkdir()
    otro = journal(tmp_path / "b", denial_quota=1)
    otro.bind_credential("cred-A", principal="p", role="be", lane="llminbox")
    assert cuenta(otro, sesiones=1, por_sesion=200) == (1, 1, 199)   # ⊕ control
    otro.close()


def test_el_runtime_sigue_estando_en_la_fila_individual(tmp_path):
    """Quitarlo de la CLAVE no es quitarlo del registro: sin él, un rechazo deja
    de decir qué proceso lo produjo y la investigación se queda sin sujeto."""
    j = journal(tmp_path, denial_quota=5)
    s = sesion(j)
    with pytest.raises(C.FencingConflict):
        j.check_fence(s.token, "sin-lease", 1)
    fila = j._connect().execute("SELECT runtime_instance, lane FROM denials"
                                ).fetchone()
    assert fila["runtime_instance"] == s.runtime_instance
    assert fila["lane"] == s.lane
    j.close()


def test_un_motivo_fuera_del_vocabulario_se_rechaza(tmp_path):
    """Sin vocabulario cerrado, el motivo acaba llevando la clave que lo provocó
    y la agregación deja de agregar."""
    j = journal(tmp_path)
    b = j.bind_credential("c", principal="p", role="be", lane="llminbox")
    with pytest.raises(ValueError):
        j._record_denial(b.principal_id, "fallo en la clave k-8f3a")
    j.close()


def test_un_token_desconocido_no_crea_filas_de_coordinacion(tmp_path):
    j = journal(tmp_path)
    sesion(j)
    with pytest.raises(C.AuthError):
        j.accept_event("token-inventado", idempotency_key="k", intent=INTENT,
                       ledger="llminbox")
    con = j._connect()
    assert con.execute("SELECT COUNT(*) c FROM denials").fetchone()["c"] == 0
    assert con.execute("SELECT COUNT(*) c FROM unknown_credentials"
                       ).fetchone()["c"] == 1        # contador acotado
    j.close()


# ── ④ OUTBOX ENVENENADO ──────────────────────────────────────────────────────

def _agotar(j, s, reloj, a):
    for _ in range(j._max_attempts):
        reloj.avanza(1000)
        item = j.claim_outbox(s.token, lease_s=60)
        assert item is not None
        j.mark_outbox_failed(s.token, a.event_id, error="ledger :ro",
                             claim_token=item.claim_token)


def test_al_agotar_intentos_queda_failed_y_SIGUE_contando_como_sin_resolver(tmp_path):
    reloj = Reloj()
    j = journal(tmp_path, reloj=reloj, max_attempts=3)
    # TTL largo a propósito: `_agotar` mueve el reloj para saltarse el backoff, y
    # con el TTL por defecto la sesión caducaría a mitad del bucle — el test
    # mediría el vencimiento de sesión en vez del agotamiento del outbox.
    s = sesion(j, ttl_s=10 ** 6, capabilities=OPERADOR)
    a = j.accept_event(s.token, idempotency_key="k", intent=INTENT, ledger="llminbox")
    _agotar(j, s, reloj, a)

    estado = j._connect().execute("SELECT state FROM outbox").fetchone()["state"]
    assert estado == "failed"
    assert j.pending_outbox(s.token) == 0
    # 🔑 lo que importa: NO se auto-abandona, así que el rollback sigue bloqueado.
    assert j.unresolved_outbox(s.token) == 1
    assert [t["state"] for t in j.transitions(s.token,
        j.receipt_for_event(s.token, a.event_id)["receipt_id"])][-1] == "materialization_exhausted"
    reloj.avanza(10_000)
    assert j.claim_outbox(s.token, lease_s=60) is None   # ningún worker lo toca
    j.close()


def test_solo_un_operador_saca_un_item_agotado_y_queda_escrito(tmp_path):
    reloj = Reloj()
    j = journal(tmp_path, reloj=reloj, max_attempts=2)
    s = sesion(j, ttl_s=10 ** 6, capabilities=OPERADOR)
    a = j.accept_event(s.token, idempotency_key="k", intent=INTENT, ledger="llminbox")
    _agotar(j, s, reloj, a)

    j.requeue_outbox(s.token, a.event_id, reason="el ledger volvió a RW")
    assert j.pending_outbox(s.token) == 1 and j.unresolved_outbox(s.token) == 1
    op = j._connect().execute("SELECT * FROM outbox_operations").fetchone()
    assert (op["operation"], op["from_state"]) == ("requeue", "failed")
    assert op["operator"] == s.principal_id          # DERIVADO de la sesión
    assert op["reason"] == "el ledger volvió a RW"

    _agotar(j, s, reloj, a)
    j.abandon_outbox(s.token, a.event_id, reason="el evento ya no aplica")
    assert j.unresolved_outbox(s.token) == 0                # abandonado SÍ drena
    assert j._connect().execute("SELECT state FROM outbox").fetchone()["state"] \
        == "abandoned"
    estados = [t["state"] for t in j.transitions(s.token,
        j.receipt_for_event(s.token, a.event_id)["receipt_id"])]
    assert "outbox_requeue" in estados and "outbox_abandon" in estados
    j.close()


def test_una_operacion_manual_sin_motivo_se_rechaza(tmp_path):
    j = journal(tmp_path)
    s = sesion(j)
    a = j.accept_event(s.token, idempotency_key="k", intent=INTENT, ledger="llminbox")
    with pytest.raises(C.JournalError):
        j.abandon_outbox(s.token, a.event_id, reason="")
    j.close()


# ── GATE DE AUTORIDAD DEL OUTBOX ─────────────────────────────────────────────

def test_una_ficha_ROBADA_por_otro_runtime_no_marca_nada(tmp_path):
    """La ficha sola no hace dueño: el arriendo tiene que ser de esta sesión."""
    j = journal(tmp_path)
    s = sesion(j)
    a = j.accept_event(s.token, idempotency_key="k", intent=INTENT, ledger="llminbox")
    item = j.claim_outbox(s.token, lease_s=600)
    ladron = j.open_session("cred-A")          # mismo principal, OTRO runtime
    with pytest.raises(C.FencingConflict):
        j.mark_materialized(ladron.token, a.event_id, entry_eid="e" * 64,
                            ledger="llminbox", claim_token=item.claim_token)
    assert j.receipt_for_event(ladron.token, a.event_id)["current_state"] == "accepted"
    fila = j._connect().execute("SELECT reason, runtime_instance FROM denials"
                                " ORDER BY at DESC LIMIT 1").fetchone()
    assert fila["reason"] == "FENCING_CONFLICT"
    assert fila["runtime_instance"] == ladron.runtime_instance
    j.mark_materialized(s.token, a.event_id, entry_eid="e" * 64,      # ⊕ el dueño
                        ledger="llminbox", claim_token=item.claim_token)
    j.close()


def test_un_worker_de_OTRO_carril_ni_reclama_ni_marca(tmp_path):
    j = journal(tmp_path)
    a_s, b_s = sesiones(j,
        {"credential": "cred-A", "principal": "p-a", "lane": "carril-uno"},
        {"credential": "cred-B", "principal": "p-b", "lane": "carril-dos"})
    ev = j.accept_event(a_s.token, idempotency_key="k", intent=INTENT,
                        ledger="ledger-uno")
    assert j.claim_outbox(b_s.token, lease_s=60) is None   # no ve trabajo ajeno
    item = j.claim_outbox(a_s.token, lease_s=60)
    with pytest.raises(C.LedgerNotAllowed):
        j.mark_materialized(b_s.token, ev.event_id, entry_eid="e" * 64,
                            ledger="ledger-uno", claim_token=item.claim_token)
    # La lectura la hace el DUEÑO del carril: `b_s` ya no puede ni leer ese
    # recibo, y eso es lo correcto — comprobarlo con su token mediría otra cosa.
    assert j.receipt_for_event(a_s.token, ev.event_id)["current_state"] == "accepted"
    with pytest.raises(C.SubjectNotFound):     # indistinguible de «no existe»
        j.receipt_for_event(b_s.token, ev.event_id)
    j.close()


def test_el_sensor_y_el_ack_quedan_atribuidos_en_la_transicion(tmp_path):
    j = journal(tmp_path)
    s = sesion(j)                       # rol `be`
    # El evento se dirige a `be` para que ESTA sesión pueda acusar: el receptor
    # ya no es texto del cliente, así que el test tiene que montar el caso real.
    a = j.accept_event(s.token, idempotency_key="k",
                       intent={**INTENT, "to": ["be"]}, ledger="llminbox")
    item = j.claim_outbox(s.token, lease_s=60)
    j.mark_materialized(s.token, a.event_id, entry_eid="e" * 64, ledger="llminbox",
                        claim_token=item.claim_token)
    j.mark_indexed(s.token, a.event_id, index_ref="idx-1")
    j.mark_delivered(s.token, a.event_id, ack_ref="ack-1")
    for t in j.transitions(s.token, j.receipt_for_event(s.token, a.event_id)["receipt_id"])[1:]:
        assert s.runtime_instance in t["detail"], t["state"]
        assert s.principal_id in t["detail"]
    j.close()


def _dominio(j) -> dict:
    con = j._connect()
    return {t: con.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
            for t in ("events", "outbox", "idempotency")}


# ── CONTRATO DE ENTREGA: UN ACUSE POR DESTINATARIO ───────────────────────────

def _hasta_indexed(j, autor, destinatarios):
    ev = j.accept_event(autor.token, idempotency_key="k",
                        intent={**INTENT, "to": destinatarios}, ledger="llminbox")
    item = j.claim_outbox(autor.token, lease_s=60)
    j.mark_materialized(autor.token, ev.event_id, entry_eid="e" * 64,
                        ledger="llminbox", claim_token=item.claim_token)
    j.mark_indexed(autor.token, ev.event_id, index_ref="idx-1")
    return ev


def test_el_primer_acuse_no_es_delivered_y_el_segundo_lo_completa(tmp_path):
    """`delivered` es el AGREGADO de N acuses, no el sello del más rápido."""
    j = journal(tmp_path)
    autor, seg, cto = sesiones(j,
        {"credential": "cred-be", "principal": "p-be", "role": "be"},
        {"credential": "cred-sec", "principal": "p-sec", "role": "security"},
        {"credential": "cred-cto", "principal": "p-cto", "role": "cto"})
    ev = _hasta_indexed(j, autor, ["security", "cto"])
    rid = j.receipt_for_event(cto.token, ev.event_id)["receipt_id"]

    assert j.mark_delivered(seg.token, ev.event_id, ack_ref="a1") == "indexed"
    # El current_state NO miente mientras falte alguien.
    assert j.receipt(cto.token, rid)["current_state"] == "indexed"
    assert [t["state"] for t in j.transitions(cto.token, rid)][-1] == "delivery_progress"

    assert j.mark_delivered(cto.token, ev.event_id, ack_ref="a2") == "delivered"
    assert j.receipt(cto.token, rid)["current_state"] == "delivered"
    assert [t["state"] for t in j.transitions(cto.token, rid)][-1] == "delivered"
    j.close()


def test_security_no_puede_acusar_por_cto(tmp_path):
    """El destinatario se DERIVA de la sesión: como argumento era texto libre."""
    j = journal(tmp_path)
    autor, seg = sesiones(j,
        {"credential": "cred-be", "principal": "p-be", "role": "be"},
        {"credential": "cred-sec", "principal": "p-sec", "role": "security"})
    ev = _hasta_indexed(j, autor, ["security", "cto"])
    j.mark_delivered(seg.token, ev.event_id, ack_ref="a1")
    filas = j._connect().execute("SELECT recipient, principal_id FROM event_acks"
                                 " WHERE event_id=?", (ev.event_id,)).fetchall()
    assert [(r["recipient"], r["principal_id"]) for r in filas] == \
        [("security", seg.principal_id)]
    assert j.receipt_for_event(seg.token, ev.event_id)["current_state"] == "indexed"
    j.close()


def test_un_acuse_repetido_del_mismo_receptor_no_duplica_nada(tmp_path):
    j = journal(tmp_path)
    autor, seg = sesiones(j,
        {"credential": "cred-be", "principal": "p-be", "role": "be"},
        {"credential": "cred-sec", "principal": "p-sec", "role": "security"})
    ev = _hasta_indexed(j, autor, ["security", "cto"])
    rid = j.receipt_for_event(seg.token, ev.event_id)["receipt_id"]
    j.mark_delivered(seg.token, ev.event_id, ack_ref="a1")
    n_trans = len(j.transitions(seg.token, rid))
    for _ in range(3):
        assert j.mark_delivered(seg.token, ev.event_id, ack_ref="a1") == "indexed"
    con = j._connect()
    assert con.execute("SELECT COUNT(*) c FROM event_acks WHERE event_id=?",
                       (ev.event_id,)).fetchone()["c"] == 1
    assert len(j.transitions(seg.token, rid)) == n_trans
    # ⊕ y el OTRO destinatario sigue pudiendo acusar después.
    cto = sesion(j, "cred-cto", principal="p-cto", role="cto")
    assert j.mark_delivered(cto.token, ev.event_id) == "delivered"
    j.close()


def test_quien_no_es_destinatario_es_rechazado_con_auditoria_y_cero_entrega(tmp_path):
    j = journal(tmp_path)
    autor, intruso = sesiones(j,
        {"credential": "cred-be", "principal": "p-be", "role": "be"},
        {"credential": "cred-qa", "principal": "p-qa", "role": "qa"})
    ev = _hasta_indexed(j, autor, ["security", "cto"])
    with pytest.raises(C.DeliveryConflict):
        j.mark_delivered(intruso.token, ev.event_id, ack_ref="a1")
    con = j._connect()
    assert con.execute("SELECT COUNT(*) c FROM event_acks").fetchone()["c"] == 0
    assert j.receipt_for_event(intruso.token, ev.event_id)["current_state"] == "indexed"
    fila = con.execute("SELECT reason, principal_id, runtime_instance FROM denials"
                       " ORDER BY at DESC LIMIT 1").fetchone()
    assert fila["reason"] == "DELIVERY_CONFLICT"
    assert fila["principal_id"] == intruso.principal_id
    assert fila["runtime_instance"] == intruso.runtime_instance
    j.close()


def test_no_se_puede_acusar_antes_de_indexar(tmp_path):
    j = journal(tmp_path)
    autor, seg = sesiones(j,
        {"credential": "cred-be", "principal": "p-be", "role": "be"},
        {"credential": "cred-sec", "principal": "p-sec", "role": "security"})
    ev = j.accept_event(autor.token, idempotency_key="k",
                        intent={**INTENT, "to": ["security"]}, ledger="llminbox")
    with pytest.raises(C.DeliveryConflict):
        j.mark_delivered(seg.token, ev.event_id)
    j.close()


def test_la_operacion_de_outbox_guarda_carril_y_runtime_no_solo_el_principal(tmp_path):
    reloj = Reloj()
    j = journal(tmp_path, reloj=reloj, max_attempts=1)
    s = sesion(j, ttl_s=10 ** 6, capabilities=OPERADOR)
    a = j.accept_event(s.token, idempotency_key="k", intent=INTENT, ledger="llminbox")
    item = j.claim_outbox(s.token, lease_s=60)
    j.mark_outbox_failed(s.token, a.event_id, error="x", claim_token=item.claim_token)
    j.abandon_outbox(s.token, a.event_id, reason="no aplica")
    op = j._connect().execute("SELECT * FROM outbox_operations").fetchone()
    assert (op["operator"], op["lane"], op["runtime_instance"]) == \
        (s.principal_id, s.lane, s.runtime_instance)
    j.close()


# ── CONCURRENCIA REAL SOBRE EL GATE DE ARRANQUE ──────────────────────────────

def test_dos_sesiones_a_la_vez_solo_UNA_arranca_el_comando(tmp_path):
    """Barrera + hebras: el gate se prueba con las dos dentro de la ventana.

    Secuencialmente esto pasa siempre, así que el test secuencial no distingue un
    gate transaccional de uno preflight. Con barrera, las dos llegan a la vez y
    la exclusión tiene que salir del `BEGIN IMMEDIATE`, no de la suerte.
    """
    import threading
    j = journal(tmp_path, busy_timeout_ms=15_000)
    a, b = sesiones(j,
        {"credential": "cred-A", "principal": "p-a"},
        {"credential": "cred-B", "principal": "p-b"})
    cid, _ = j.submit_command(a.token, workstream_id="ws", revision=1, payload={})
    j.advance_command(a.token, cid, "received")

    barrera = threading.Barrier(2)

    def arranca(token):
        barrera.wait()
        try:
            j.advance_command(token, cid, "executing")
            return "ok"
        except C.CommandTransitionInvalid:
            return "rechazado"

    with ThreadPoolExecutor(max_workers=2) as pool:
        res = sorted(pool.map(arranca, [a.token, b.token]))

    assert res == ["ok", "rechazado"], res
    con = j._connect()
    assert con.execute("SELECT state FROM commands WHERE command_id=?",
                       (cid,)).fetchone()["state"] == "executing"
    rid = con.execute("SELECT receipt_id FROM receipts WHERE subject_kind='command'"
                      " AND subject_id=?", (cid,)).fetchone()["receipt_id"]
    estados = [t["state"] for t in j.transitions(b.token, rid)]
    assert estados.count("executing") == 1, estados
    assert con.execute("SELECT current_state FROM receipts WHERE receipt_id=?",
                       (rid,)).fetchone()["current_state"] == "executing"
    assert con.execute("SELECT COUNT(*) c FROM denials WHERE"
                       " reason='COMMAND_TRANSITION_INVALID'").fetchone()["c"] == 1
    j.close()


def test_una_revision_repetida_es_un_rechazo_TIPADO_y_no_un_500(tmp_path):
    j = journal(tmp_path)
    s = sesion(j)
    j.submit_command(s.token, workstream_id="ws", revision=1, payload={})
    antes = j._connect().execute("SELECT COUNT(*) c FROM commands").fetchone()["c"]
    with pytest.raises(C.CommandRevisionConflict):
        j.submit_command(s.token, workstream_id="ws", revision=1, payload={"x": 1})
    con = j._connect()
    assert con.execute("SELECT COUNT(*) c FROM commands").fetchone()["c"] == antes
    fila = con.execute("SELECT reason FROM denials ORDER BY at DESC LIMIT 1").fetchone()
    assert fila["reason"] == "COMMAND_REVISION_CONFLICT"
    # ⊕ control: otra revisión entra sin problema.
    _, estado = j.submit_command(s.token, workstream_id="ws", revision=2, payload={})
    assert estado == "accepted"
    j.close()


# ── TODO RECHAZO DE MUTACIÓN CON TOKEN CONOCIDO DEJA CÓDIGO CERRADO ─────────

@pytest.mark.parametrize("codigo,disparo", [
    ("SUBJECT_NOT_FOUND",
     lambda j, s, ev: j.mark_indexed(s.token, "evt_no_existe")),
    ("SUBJECT_NOT_FOUND",
     lambda j, s, ev: j.abandon_outbox(s.token, "evt_no_existe", reason="x")),
    ("RECEIPT_STATE_INVALID",
     lambda j, s, ev: j.mark_indexed(s.token, ev.event_id)),   # sin materializar
    ("LEDGER_NOT_ALLOWED",
     lambda j, s, ev: j.mark_materialized(
         s.token, ev.event_id, entry_eid="e" * 64, ledger="otro",
         claim_token=j.claim_outbox(s.token, lease_s=60).claim_token)),
    ("OPERATION_INVALID",
     lambda j, s, ev: j.abandon_outbox(s.token, ev.event_id, reason="")),
    ("OPERATION_INVALID",
     lambda j, s, ev: j.requeue_outbox(s.token, ev.event_id, reason="no toca")),
    ("ATTRIBUTION_REJECTED",
     lambda j, s, ev: j.accept_event(s.token, idempotency_key="q",
                                     intent={**INTENT, "role": "cto"},
                                     ledger="llminbox")),
    ("COMMAND_REVISION_CONFLICT",
     lambda j, s, ev: (j.submit_command(s.token, workstream_id="w", revision=1,
                                        payload={}),
                       j.submit_command(s.token, workstream_id="w", revision=1,
                                        payload={}))),
])
def test_cada_raise_de_mutacion_deja_su_codigo_cerrado(tmp_path, codigo, disparo):
    j = journal(tmp_path)
    s = sesion(j, capabilities=OPERADOR)      # para llegar al error QUE SE MIDE
    ev = j.accept_event(s.token, idempotency_key="base", intent=INTENT,
                        ledger="llminbox")
    with pytest.raises(C.JournalError):
        disparo(j, s, ev)
    fila = j._connect().execute("SELECT reason FROM denials ORDER BY at DESC"
                                " LIMIT 1").fetchone()
    assert fila is not None, f"{codigo}: el rechazo no dejó rastro"
    assert fila["reason"] == codigo
    assert fila["reason"] in C.REASON_CODES
    j.close()


def test_un_rechazo_sin_clasificar_cae_en_OPERATION_REJECTED_y_no_en_el_vacio(tmp_path):
    """La red de seguridad. Antes, el error que nadie había clasificado era
    justo el que se iba sin auditar."""
    j = journal(tmp_path)
    s = sesion(j)

    class Raro(C.JournalError):
        pass

    j._auditar_rechazo(s.token, Raro("algo que nadie mapeó"))
    fila = j._connect().execute("SELECT reason FROM denials").fetchone()
    assert fila["reason"] == "OPERATION_REJECTED"
    j.close()


def test_refresh_session_audita_al_revocado_y_al_perdedor_de_la_carrera(tmp_path):
    j = journal(tmp_path)
    s = sesion(j)
    j.revoke_current(s.token, "prueba")
    with pytest.raises(C.AuthError):
        j.refresh_session(s.token)
    fila = j._connect().execute("SELECT reason, principal_id, runtime_instance"
                                "  FROM denials ORDER BY at DESC LIMIT 1").fetchone()
    assert fila["reason"] == "SESSION_INVALID"
    assert fila["principal_id"] == s.principal_id
    assert fila["runtime_instance"] == s.runtime_instance
    j.close()


# ── HUELLA ROTATORIA DE DESCONOCIDOS ────────────────────────────────────────

def test_la_huella_del_desconocido_ROTA_con_la_epoca(tmp_path):
    """ADR: «rotating credential fingerprint». Frena la ráfaga sin dejar un
    identificador estable con el que seguir a alguien en el tiempo."""
    reloj = Reloj()
    j = journal(tmp_path, reloj=reloj, fingerprint_rotation_s=100)
    j.record_unknown_credential("una-credencial-cualquiera")
    primera = j._connect().execute("SELECT fingerprint FROM unknown_credentials"
                                   ).fetchone()["fingerprint"]
    j.record_unknown_credential("una-credencial-cualquiera")   # misma época
    assert j._connect().execute("SELECT COUNT(*) c FROM unknown_credentials"
                                ).fetchone()["c"] == 1        # ⊕ agrupa dentro
    reloj.avanza(101)                                          # otra época
    j.record_unknown_credential("una-credencial-cualquiera")
    huellas = [r["fingerprint"] for r in j._connect().execute(
        "SELECT fingerprint FROM unknown_credentials")]
    assert len(huellas) == 2 and primera in huellas
    # ⊖ y el secreto NUNCA está en la base.
    assert "una-credencial-cualquiera" not in str(huellas)
    j.close()


def test_la_cota_de_filas_aguanta_la_rotacion(tmp_path):
    reloj = Reloj()
    j = journal(tmp_path, reloj=reloj, unknown_rows_max=8,
                fingerprint_rotation_s=10)
    for i in range(200):
        reloj.avanza(11)                       # una época distinta cada vez
        j.record_unknown_credential(f"cred-{i}")
    n = j._connect().execute("SELECT COUNT(*) c FROM unknown_credentials"
                             ).fetchone()["c"]
    assert n <= 9, f"la rotación reventó la cota: {n} filas"
    total = j._connect().execute("SELECT SUM(count) s FROM unknown_credentials"
                                 ).fetchone()["s"]
    assert total == 200                        # ⊕ las contó todas
    j.close()


# ── ATRIBUCIÓN DERIVADA EN COMANDOS ─────────────────────────────────────────

def test_el_comando_y_su_transicion_guardan_QUIEN_derivado_de_la_sesion(tmp_path):
    j = journal(tmp_path)
    s = sesion(j)
    cid, _ = j.submit_command(s.token, workstream_id="ws", revision=1, payload={})
    fila = j._connect().execute("SELECT role, runtime_instance FROM commands"
                                " WHERE command_id=?", (cid,)).fetchone()
    assert (fila["role"], fila["runtime_instance"]) == (s.role, s.runtime_instance)

    # Y el actor de la transición NO lo puede escribir el llamante.
    j.advance_command(s.token, cid, "received",
                      detail={"by_principal": "prn_mentira",
                              "by_runtime": "rti_mentira"})
    rid = j._connect().execute(
        "SELECT receipt_id FROM receipts WHERE subject_kind='command'"
        " AND subject_id=?", (cid,)).fetchone()["receipt_id"]
    d = j.transitions(s.token, rid)[-1]["detail"]
    import json as _json
    detalle = _json.loads(d)
    assert detalle["by_principal"] == s.principal_id
    assert detalle["by_runtime"] == s.runtime_instance
    assert detalle["by_role"] == s.role
    assert detalle["payload"]["by_principal"] == "prn_mentira"   # bajo payload
    j.close()
