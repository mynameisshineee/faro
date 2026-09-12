"""Carreras entre el chequeo de sesión y la escritura que autoriza.

TODAS usan el seam `_gancho_carrera`, que dispara ENTRE el chequeo barato y el
`BEGIN IMMEDIATE`, y desde ahí revocan o recargan el mapa POR OTRA CONEXIÓN. No
hay `sleep` en ninguna: una ventana que se falsa durmiendo produce un test que
mide la carga del CI. La pregunta que contestan es siempre la misma — *después
de que la sesión deje de valer, ¿entró algo?* — y la respuesta se mide contando
filas, no leyendo el error.
"""
from __future__ import annotations

import pytest

import coordination as C
from ._arnes import GRAMATICA, INTENT, PEPPER, journal, sesion


def _otro(j):
    """Segunda conexión al mismo fichero: lo que hace el reload del operador.

    `initialize()` NO es ceremonia de test: es el contrato. El estado del ciclo
    es POR INSTANCIA a propósito —un booleano global dejaría entrar a cualquier
    hebra mientras otra migra—, así que una instancia recién construida está en
    `NUEVO` aunque el fichero lleve migrado desde hace rato. El operador que
    recarga abre su journal y lo inicializa; simularlo sin inicializar probaría
    una ruta que ningún proceso real toma.
    """
    o = C.Journal(j.path, pepper=PEPPER, busy_timeout_ms=5000, grammar=GRAMATICA)
    o.initialize()
    return o


def _censo(j) -> dict:
    con = j._connect()
    return {t: con.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
            for t in ("events", "receipts", "outbox", "idempotency", "leases",
                      "commands", "runtime_sessions")}


@pytest.mark.parametrize("accion", ["revoke", "reload"])
def test_accept_event_no_entra_si_la_sesion_muere_tras_el_precheck(tmp_path, accion):
    j = journal(tmp_path)
    s = sesion(j)
    antes = _censo(j)

    def sabotaje():
        o = _otro(j)
        if accion == "revoke":
            o.revoke_current(s.token, "carrera")
        else:
            o.reload_credential_map({"cred-nueva": {"role": "be", "lane": "llminbox"}})
        o.close()

    j._gancho_carrera = sabotaje
    with pytest.raises(C.AuthError):
        j.accept_event(s.token, idempotency_key="k", intent=INTENT, ledger="llminbox")
    j._gancho_carrera = None
    assert _censo(j)["events"] == antes["events"] == 0
    assert _censo(j)["outbox"] == 0 and _censo(j)["idempotency"] == 0
    j.close()


@pytest.mark.parametrize("accion", ["revoke", "reload"])
def test_acquire_lease_no_entra_si_la_sesion_muere_tras_el_precheck(tmp_path, accion):
    j = journal(tmp_path)
    s = sesion(j)

    def sabotaje():
        o = _otro(j)
        if accion == "revoke":
            o.revoke_current(s.token, "carrera")
        else:
            o.reload_credential_map({"otra": {"role": "be", "lane": "llminbox"}})
        o.close()

    j._gancho_carrera = sabotaje
    with pytest.raises(C.AuthError):
        j.acquire_lease(s.token, "migracion", ttl_s=60)
    j._gancho_carrera = None
    assert _censo(j)["leases"] == 0
    j.close()


@pytest.mark.parametrize("accion", ["revoke", "reload"])
def test_submit_command_no_entra_si_la_sesion_muere_tras_el_precheck(tmp_path, accion):
    j = journal(tmp_path)
    s = sesion(j)

    def sabotaje():
        o = _otro(j)
        if accion == "revoke":
            o.revoke_current(s.token, "carrera")
        else:
            o.reload_credential_map({"otra": {"role": "be", "lane": "llminbox"}})
        o.close()

    j._gancho_carrera = sabotaje
    with pytest.raises(C.AuthError):
        j.submit_command(s.token, workstream_id="ws", revision=1, payload={})
    j._gancho_carrera = None
    assert _censo(j)["commands"] == 0
    j.close()


def test_release_lease_no_suelta_si_la_sesion_muere_tras_el_precheck(tmp_path):
    j = journal(tmp_path)
    s = sesion(j)
    j.acquire_lease(s.token, "r", ttl_s=300)

    def sabotaje():
        o = _otro(j)
        o.revoke_current(s.token, "carrera")
        o.close()

    j._gancho_carrera = sabotaje
    with pytest.raises(C.AuthError):
        j.release_lease(s.token, "r")
    j._gancho_carrera = None
    fila = j._connect().execute("SELECT released_at FROM leases").fetchone()
    assert fila["released_at"] is None       # el lease sigue vivo
    j.close()


def test_sin_sabotaje_las_cuatro_operaciones_SI_entran(tmp_path):
    """⊕ imprescindible: sin él, un `AuthError` permanente —o un journal que
    rechaza todo— pasaría los cuatro tests de arriba con nota."""
    j = journal(tmp_path)
    s = sesion(j)
    a = j.accept_event(s.token, idempotency_key="k", intent=INTENT, ledger="llminbox")
    lease = j.acquire_lease(s.token, "r", ttl_s=300)
    cid, estado = j.submit_command(s.token, workstream_id="ws", revision=1, payload={})
    j.release_lease(s.token, "r")
    c = _censo(j)
    assert (c["events"], c["leases"], c["commands"]) == (1, 1, 1)
    assert a.replayed is False and lease.fencing_token == 1 and estado == "accepted"
    j.close()


def test_open_session_no_emite_desde_una_ligadura_RETIRADA(tmp_path):
    """La ligadura se re-resuelve dentro de la transacción: si el reload la
    retiró mientras tanto, no nace sesión."""
    j = journal(tmp_path, admision=False)  # `admision=False`: este test CUENTA filas de identidad, y las
    # credenciales/sesiones que el arnés usa para abrir la barrera son
    # filas como cualquier otra. No acepta eventos, así que no necesita
    # la puerta abierta.
    j.bind_credential("cred-A", principal="backend", role="be", lane="llminbox")

    def sabotaje():
        o = _otro(j)
        o.reload_credential_map({"cred-B": {"role": "be", "lane": "llminbox"}})
        o.close()

    j._gancho_carrera = sabotaje
    # El precheck de `open_session` corre antes del gancho; el gancho retira la
    # ligadura; la revalidación de dentro tiene que verlo.
    with pytest.raises(C.AuthError):
        j.open_session("cred-A")
    j._gancho_carrera = None
    assert j._connect().execute("SELECT COUNT(*) c FROM runtime_sessions"
                                ).fetchone()["c"] == 0
    j.close()


def test_dos_refrescos_del_mismo_token_no_crean_dos_hijos_validos(tmp_path):
    """Rotar tiene que REEMPLAZAR la sesión, no multiplicarla.

    El segundo refresco corre desde el gancho del primero, o sea con los dos
    dentro de la misma ventana. Sólo uno puede ganar.
    """
    j = journal(tmp_path)
    s = sesion(j)
    hijos = []

    def sabotaje():
        o = _otro(j)
        try:
            hijos.append(o.refresh_session(s.token))
        except C.AuthError:
            pass
        finally:
            o.close()

    j._gancho_carrera = sabotaje
    try:
        hijos.append(j.refresh_session(s.token))
    except C.AuthError:
        pass
    j._gancho_carrera = None

    assert len(hijos) == 1, f"{len(hijos)} hijos válidos de una sola rotación"
    assert j.authenticate(s.token) is None                  # el padre, muerto
    assert j.authenticate(hijos[0].token) is not None        # ⊕ el hijo, vivo
    vivas = j._connect().execute(
        "SELECT COUNT(*) c FROM runtime_sessions WHERE revoked_at IS NULL"
        " AND rotated_from IS NOT NULL").fetchone()["c"]
    assert vivas == 1
    j.close()
