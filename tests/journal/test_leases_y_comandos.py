"""Exclusión con fencing y supersesión de comandos."""
from __future__ import annotations

import pytest

import coordination as C
from ._arnes import Reloj, journal, sesion, sesiones


def test_el_dueno_vencido_es_RECHAZADO_al_mutar_tras_el_relevo(tmp_path):
    """Falsador 8, y el punto es DÓNDE se comprueba.

    Vencer no vuelve inofensivo al trabajador pausado: lo que lo para es el
    fencing en el BORDE DE LA MUTACIÓN. Por eso el test no mira si el lease
    caducó, sino si la mutación con el token viejo pasa.
    """
    reloj = Reloj()
    j = journal(tmp_path, reloj=reloj)
    a, b = sesiones(j,
        {"credential": "cred-A", "principal": "p-a"},
        {"credential": "cred-B", "principal": "p-b"})

    viejo = j.acquire_lease(a.token, "migracion", ttl_s=60)
    j.check_fence(a.token, "migracion", viejo.fencing_token)   # ⊕ ahora sí vale

    with pytest.raises(C.LeaseConflict):                     # dueño vivo
        j.acquire_lease(b.token, "migracion", ttl_s=60)

    reloj.avanza(61)                                          # vence
    nuevo = j.acquire_lease(b.token, "migracion", ttl_s=60)   # relevo
    assert nuevo.fencing_token == viejo.fencing_token + 1     # monótono

    # El relevado ya no pasa la valla NI CON SU PROPIO token NI con el nuevo:
    # no es sólo que su número sea viejo, es que su sesión ya no es la dueña.
    with pytest.raises(C.FencingConflict):
        j.check_fence(a.token, "migracion", viejo.fencing_token)
    with pytest.raises(C.FencingConflict):
        j.check_fence(a.token, "migracion", nuevo.fencing_token)
    j.check_fence(b.token, "migracion", nuevo.fencing_token)   # ⊕ el dueño sí
    j.close()


def test_renovar_conserva_el_token_y_otra_instancia_recibe_uno_nuevo(tmp_path):
    reloj = Reloj()
    j = journal(tmp_path, reloj=reloj)
    a = sesion(j, "cred-A", principal="p")
    uno = j.acquire_lease(a.token, "r", ttl_s=60)
    assert j.renew_lease(a.token, "r", ttl_s=60).fencing_token == uno.fencing_token

    # Otra SESIÓN del MISMO principal: es otro runtime_instance, así que tiene
    # que adquirir o relevar; no hereda el token.
    otra = j.open_session("cred-A")
    reloj.avanza(61)
    assert j.acquire_lease(otra.token, "r", ttl_s=60).fencing_token > uno.fencing_token
    j.close()


def test_un_recurso_no_puede_tener_dos_duenos_sin_vencer(tmp_path):
    j = journal(tmp_path)
    a, b = sesiones(j,
        {"credential": "cred-A", "principal": "p-a"},
        {"credential": "cred-B", "principal": "p-b"})
    j.acquire_lease(a.token, "r", ttl_s=300)
    with pytest.raises(C.LeaseConflict):
        j.acquire_lease(b.token, "r", ttl_s=300)
    assert j._connect().execute("SELECT COUNT(*) c FROM leases WHERE resource='r'"
                                ).fetchone()["c"] == 1
    j.close()


def test_un_comando_viejo_que_llega_tarde_se_recibe_pero_NO_se_ejecuta(tmp_path):
    """Falsador 9. Se registra y se audita: no se tira, no se ejecuta."""
    j = journal(tmp_path)
    s = sesion(j)
    r2, e2 = j.submit_command(s.token, workstream_id="ws", revision=2, payload={"x": 1})
    assert e2 == "accepted" and j.may_execute(s.token, r2) is True

    r1, e1 = j.submit_command(s.token, workstream_id="ws", revision=1, payload={"x": 0})
    assert e1 == "superseded"
    assert j.may_execute(s.token, r1) is False
    rc = j._connect().execute("SELECT receipt_id FROM receipts WHERE subject_kind='command'"
                              " AND subject_id=?", (r1,)).fetchone()
    assert [t["state"] for t in j.transitions(s.token, rc["receipt_id"])] == ["superseded"]
    assert j.may_execute(s.token, r2) is True         # ⊕ el vigente sigue vigente
    j.close()


def test_una_revision_mayor_supersede_a_la_anterior_y_lo_recibe(tmp_path):
    j = journal(tmp_path)
    s = sesion(j)
    r1, _ = j.submit_command(s.token, workstream_id="ws", revision=1, payload={})
    assert j.may_execute(s.token, r1) is True                          # ⊕
    r2, e2 = j.submit_command(s.token, workstream_id="ws", revision=2, payload={})
    assert e2 == "accepted"
    assert j.may_execute(s.token, r1) is False and j.may_execute(s.token, r2) is True
    rc = j._connect().execute("SELECT receipt_id FROM receipts WHERE subject_kind='command'"
                              " AND subject_id=?", (r1,)).fetchone()
    assert [t["state"] for t in j.transitions(s.token, rc["receipt_id"])] == \
        ["accepted", "superseded"]
    j.close()


def test_un_token_de_fencing_MAYOR_que_el_vigente_tambien_se_rechaza(tmp_path):
    """La valla es IGUALDAD, no «no menor».

    Con `<` en vez de `!=`, un número inventado HACIA ARRIBA pasaba: nadie lo
    emitió nunca y aun así autorizaba. Es la llave maestra más barata de
    fabricar, porque el atacante no necesita saber el token vigente — le basta
    con pasarse.
    """
    j = journal(tmp_path)
    a = sesion(j, "cred-A", principal="p-a")
    lease = j.acquire_lease(a.token, "r", ttl_s=300)
    j.check_fence(a.token, "r", lease.fencing_token)          # ⊕ el exacto
    with pytest.raises(C.FencingConflict):
        j.check_fence(a.token, "r", lease.fencing_token + 1)  # ⊖ uno de más
    with pytest.raises(C.FencingConflict):
        j.check_fence(a.token, "r", lease.fencing_token + 999)
    j.close()


def test_soltar_el_lease_de_OTRO_falla_y_no_lo_suelta(tmp_path):
    """Un `UPDATE` que no casa filas devuelve éxito: soltar lo ajeno «funcionaba»
    y el que llamaba se iba creyendo que había liberado el recurso."""
    j = journal(tmp_path)
    a, b = sesiones(j,
        {"credential": "cred-A", "principal": "p-a"},
        {"credential": "cred-B", "principal": "p-b", "lane": "llminbox"})
    j.acquire_lease(a.token, "r", ttl_s=300)
    with pytest.raises(C.LeaseConflict):
        j.release_lease(b.token, "r")
    fila = j._connect().execute("SELECT released_at FROM leases").fetchone()
    assert fila["released_at"] is None, "el ajeno soltó el lease"
    j.release_lease(a.token, "r")                             # ⊕ el dueño sí
    assert j._connect().execute("SELECT released_at FROM leases"
                                ).fetchone()["released_at"] is not None
    j.close()


def test_soltar_un_recurso_sin_lease_falla_en_vez_de_callar(tmp_path):
    j = journal(tmp_path)
    a = sesion(j, "cred-A", principal="p-a")
    with pytest.raises(C.LeaseConflict):
        j.release_lease(a.token, "no-existe")
    j.close()


# ── MÁQUINA DE COMANDOS Y GATE DE ARRANQUE ───────────────────────────────────

def test_la_maquina_de_comandos_recorre_su_camino_y_recibo_y_dominio_no_divergen(tmp_path):
    j = journal(tmp_path)
    s = sesion(j)
    cid, _ = j.submit_command(s.token, workstream_id="ws", revision=1, payload={})
    for estado in ("received", "executing", "succeeded"):
        j.advance_command(s.token, cid, estado)
        dominio = j._connect().execute("SELECT state FROM commands WHERE command_id=?",
                                       (cid,)).fetchone()["state"]
        recibo = j._connect().execute(
            "SELECT current_state FROM receipts WHERE subject_kind='command'"
            " AND subject_id=?", (cid,)).fetchone()["current_state"]
        assert dominio == recibo == estado, "recibo y dominio divergen"
    assert j.may_execute(s.token, cid) is False           # terminal
    j.close()


def test_las_transiciones_ilegales_se_rechazan_y_no_tocan_el_dominio(tmp_path):
    j = journal(tmp_path)
    s = sesion(j)
    cid, _ = j.submit_command(s.token, workstream_id="ws", revision=1, payload={})
    for ilegal in ("executing", "succeeded", "failed", "superseded", "inventado"):
        with pytest.raises(C.CommandTransitionInvalid):
            j.advance_command(s.token, cid, ilegal)     # desde `accepted`
    assert j._connect().execute("SELECT state FROM commands WHERE command_id=?",
                                (cid,)).fetchone()["state"] == "accepted"
    j.advance_command(s.token, cid, "received")          # ⊕ la legal sí
    j.advance_command(s.token, cid, "cancelled")
    with pytest.raises(C.CommandTransitionInvalid):
        j.advance_command(s.token, cid, "executing")     # desde terminal, nunca
    j.close()


def test_may_execute_es_FALSE_en_todos_los_terminales(tmp_path):
    j = journal(tmp_path)
    s = sesion(j)
    for i, camino in enumerate([["received", "cancelled"],
                                ["received", "executing", "succeeded"],
                                ["received", "executing", "failed"]]):
        cid, _ = j.submit_command(s.token, workstream_id=f"ws{i}", revision=1,
                                  payload={})
        assert j.may_execute(s.token, cid) is True            # ⊕ antes de arrancar
        for estado in camino:
            j.advance_command(s.token, cid, estado)
        assert j.may_execute(s.token, cid) is False, camino
    j.close()


def test_una_revision_nueva_no_desaloja_ni_duplica_lo_que_YA_ejecuta(tmp_path):
    """rev2 llega con rev1 EJECUTANDO: ni la supersede ni puede arrancar."""
    j = journal(tmp_path)
    s = sesion(j)
    r1, _ = j.submit_command(s.token, workstream_id="ws", revision=1, payload={})
    j.advance_command(s.token, r1, "received")
    j.advance_command(s.token, r1, "executing")

    r2, e2 = j.submit_command(s.token, workstream_id="ws", revision=2, payload={})
    assert e2 == "accepted"
    # rev1 sigue EJECUTANDO: el trabajo en curso no se reescribe.
    assert j._connect().execute("SELECT state FROM commands WHERE command_id=?",
                                (r1,)).fetchone()["state"] == "executing"
    j.advance_command(s.token, r2, "received")
    with pytest.raises(C.CommandTransitionInvalid):
        j.advance_command(s.token, r2, "executing")     # dos executing, jamás
    assert j._connect().execute(
        "SELECT COUNT(*) c FROM commands WHERE state='executing'").fetchone()["c"] == 1

    j.advance_command(s.token, r1, "succeeded")          # rev1 terminaliza
    j.advance_command(s.token, r2, "executing")          # ⊕ ahora sí
    j.close()


def test_una_revision_vieja_no_arranca_aunque_may_execute_se_consultara_antes(tmp_path):
    """El gate no confía en el preflight: la comprobación vive en la tx."""
    j = journal(tmp_path)
    s = sesion(j)
    r1, _ = j.submit_command(s.token, workstream_id="ws", revision=1, payload={})
    j.advance_command(s.token, r1, "received")
    assert j.may_execute(s.token, r1) is True                     # preflight dice que sí…
    j.submit_command(s.token, workstream_id="ws", revision=2, payload={})
    # …y entre medias llega rev2, que supersede a rev1.
    with pytest.raises(C.CommandTransitionInvalid):
        j.advance_command(s.token, r1, "executing")
    j.close()


def test_un_comando_puede_citar_material_bridge_sin_ascenderlo(tmp_path):
    j = journal(tmp_path)
    s = sesion(j)
    cid, _ = j.submit_command(s.token, workstream_id="ws", revision=1, payload={},
                              external_causes=[{"ledger": "llminbox",
                                                "entry_eid": "b" * 64}])
    fila = j._connect().execute(
        "SELECT * FROM external_causes WHERE child_kind='command' AND child_id=?",
        (cid,)).fetchone()
    assert fila["entry_eid"] == "b" * 64 and fila["authority"] == "false"
    j.close()
