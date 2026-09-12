"""El MISMO TOCTOU del reloj que ya se curó en el lease, en las DOS puertas de
la SESIÓN: `open_session` y `refresh_session`.

`now = self._clock()` se capturaba antes de `_tras_precheck()` y del
`BEGIN IMMEDIATE`, y con ese valor viejo se decidía el vencimiento Y se escribía
el `expires_at`. Entre la lectura y el `INSERT` cabe el vencimiento entero.

MEDIDO sobre el freeze `9793f8a`, por mi mano, antes de tocar nada:

    refresh  padre `ttl=100`, el hook avanza `+101` (el padre YA está muerto)
             ⇒ ACEPTA. Revoca al padre con motivo `rotated` —un padre muerto no
               se rota, se deja morir— y emite un hijo con
               `expires_at = now_viejo + ttl`. Con `ttl=900`:
                   `authenticate(hijo)` -> True, con el padre muerto.
               Eso no es rotar: es RESUCITAR. Quien conserve un token caducado
               recupera sesión válida con sólo refrescarlo, y el vencimiento
               deja de ser exigible por nadie.

    open     `ttl=100`, el hook avanza `+500`
             ⇒ escribe `expires_at=1000100` con `now=1000500`: la sesión NACE
               VENCIDA y `authenticate()` del token recién entregado da `None`.
               Falla cerrado —el daño es una promesa rota, no una puerta— y es
               el mismo defecto con la otra cara, así que se cura a la vez:
               curar uno y dejar el gemelo es dejar el bug con otro nombre.

La segunda vía del mismo invariante NO es el reloj sino el ARGUMENTO: un
`ttl_s <= 0` produce exactamente el mismo estado —sesión muerta con token
entregado— sin ninguna carrera. Se cierra aquí también, o «ninguna sesión nace
vencida» valdría sólo para una de sus dos causas.
"""
from __future__ import annotations

import sqlite3

import pytest

import coordination as C
from ._arnes import Reloj, journal, sesion


def _sesiones(tmp_path):
    con = sqlite3.connect(str(tmp_path / "coordination.sqlite"))
    con.row_factory = sqlite3.Row
    return con


# ══ refresh_session — el padre VENCIDO no se rota, y sobre todo no revive ═══

def test_refresh_NO_acepta_un_padre_que_vence_en_la_ventana(tmp_path):
    r = Reloj()
    j = journal(tmp_path, reloj=r)
    s = sesion(j, "c", principal="p", role="be", ttl_s=100)
    j._gancho_carrera = lambda: r.avanza(101)
    try:
        with pytest.raises(C.AuthError):
            j.refresh_session(s.token, ttl_s=900)
    finally:
        j._gancho_carrera = None
    j.close()


def test_refresh_de_un_padre_VENCIDO_no_deja_hijo_que_AUTENTIQUE(tmp_path):
    """El daño real, y por eso se mide sobre `authenticate`, no sobre el `raise`.

    Con el reloj viejo el hijo nacía con `expires_at = 1000000 + 900` mientras
    el reloj real iba por `1000101`: VIVO. Un token caducado se cambiaba por uno
    válido, que es la definición de que el vencimiento no se aplica.
    """
    r = Reloj()
    j = journal(tmp_path, reloj=r, admision=False)  # `admision=False`: este test CUENTA filas de identidad, y las
    # credenciales/sesiones que el arnés usa para abrir la barrera son
    # filas como cualquier otra. No acepta eventos, así que no necesita
    # la puerta abierta.
    s = sesion(j, "c", principal="p", role="be", ttl_s=100)
    j._gancho_carrera = lambda: r.avanza(101)
    try:
        with pytest.raises(C.AuthError):
            hijo = j.refresh_session(s.token, ttl_s=900)
            assert j.authenticate(hijo.token) is None, (
                "el hijo de un padre VENCIDO autentica: la rotación resucitó la "
                "sesión en vez de reemplazarla")
    finally:
        j._gancho_carrera = None
    # y NO hay fila nueva: el rechazo tampoco escribe a medias
    con = _sesiones(tmp_path)
    assert con.execute("SELECT COUNT(*) c FROM runtime_sessions").fetchone()["c"] == 1
    assert con.execute(
        "SELECT COUNT(*) c FROM runtime_sessions WHERE rotated_from IS NOT NULL"
    ).fetchone()["c"] == 0
    j.close()


def test_al_padre_vencido_NO_se_le_pone_el_motivo_rotated(tmp_path):
    """Un padre muerto no se rota: se deja morir. Marcarlo `rotated` escribe en
    el registro autoritativo que hubo una rotación que nunca ocurrió, y ése es
    el rastro que alguien leerá después para explicar de dónde salió un token.
    """
    r = Reloj()
    j = journal(tmp_path, reloj=r)
    s = sesion(j, "c", principal="p", role="be", ttl_s=100)
    j._gancho_carrera = lambda: r.avanza(101)
    try:
        with pytest.raises(C.AuthError):
            j.refresh_session(s.token, ttl_s=900)
    finally:
        j._gancho_carrera = None
    fila = _sesiones(tmp_path).execute(
        "SELECT revoked_at, revoke_reason FROM runtime_sessions").fetchone()
    assert fila["revoke_reason"] != "rotated", (
        "el padre vencido quedó marcado como ROTADO: el registro cuenta una "
        "rotación que no pasó")
    assert fila["revoked_at"] is None
    j.close()


def test_OMEGA_el_refresh_LEGITIMO_sigue_pasando(tmp_path):
    """⊕ El control que impide cerrar de más. La cura mueve el reloj, no lo
    endurece: con el padre VIVO en la ventana, la rotación tiene que seguir
    funcionando — y el hijo hereda identidad sin ensancharla."""
    r = Reloj()
    j = journal(tmp_path, reloj=r)
    s = sesion(j, "c", principal="p", role="be", ttl_s=1000)
    j._gancho_carrera = lambda: r.avanza(101)      # avanza, pero NO vence
    try:
        hijo = j.refresh_session(s.token, ttl_s=900)
    finally:
        j._gancho_carrera = None
    v = j.authenticate(hijo.token)
    assert v is not None and v.principal_id == s.principal_id
    assert v.role == s.role and v.lane == s.lane
    assert j.authenticate(s.token) is None, "el token viejo tiene que morir"
    j.close()


def test_el_expires_at_del_hijo_se_calcula_con_el_reloj_de_DENTRO(tmp_path):
    """Con el reloj de fuera el hijo nacía con `1000000+900`; con el de dentro,
    `1000101+900`. La diferencia es exactamente lo que el hook avanzó, y por eso
    se mide como IGUALDAD y no como «> now»: un `>` lo cumplen las dos."""
    r = Reloj()
    j = journal(tmp_path, reloj=r)
    s = sesion(j, "c", principal="p", role="be", ttl_s=1000)
    j._gancho_carrera = lambda: r.avanza(101)
    try:
        hijo = j.refresh_session(s.token, ttl_s=900)
    finally:
        j._gancho_carrera = None
    assert hijo.expires_at == r.t + 900, (
        f"expires_at={hijo.expires_at} con now={r.t}: se calculó con el reloj de "
        f"ANTES de la transacción")
    # y lo devuelto es lo PERSISTIDO, no un valor de adorno
    fila = _sesiones(tmp_path).execute(
        "SELECT expires_at FROM runtime_sessions WHERE runtime_instance=?",
        (hijo.runtime_instance,)).fetchone()
    assert fila["expires_at"] == hijo.expires_at
    j.close()


# ══ open_session — ninguna sesión nace vencida ══════════════════════════════

def test_open_session_NO_nace_vencida_cuando_el_reloj_corre_en_la_ventana(tmp_path):
    r = Reloj()
    j = journal(tmp_path, reloj=r)
    j.bind_credential("c", principal="p", role="be", lane="llminbox")
    j._gancho_carrera = lambda: r.avanza(500)
    try:
        s = j.open_session("c", ttl_s=100)
    finally:
        j._gancho_carrera = None
    assert s.expires_at == r.t + 100, (
        f"expires_at={s.expires_at} con now={r.t}: nació con el reloj de antes")
    assert j.authenticate(s.token) is not None, (
        "el token recién entregado no autentica: la sesión nació vencida")
    j.close()


def test_ninguna_fila_de_sesion_nace_con_expires_at_ya_pasado(tmp_path):
    """El invariante sobre la TABLA y no sobre el valor devuelto: lo que gobierna
    `authenticate()` es la fila, y es donde se vería un `expires_at` viejo aunque
    el objeto devuelto fuera correcto."""
    r = Reloj()
    j = journal(tmp_path, reloj=r, admision=False)  # `admision=False`: este test CUENTA filas de identidad, y las
    # credenciales/sesiones que el arnés usa para abrir la barrera son
    # filas como cualquier otra. No acepta eventos, así que no necesita
    # la puerta abierta.
    j.bind_credential("c", principal="p", role="be", lane="llminbox")
    for avance in (0, 1, 99, 500, 5000):
        j._gancho_carrera = (lambda a=avance: r.avanza(a)) if avance else None
        try:
            j.open_session("c", ttl_s=100)
        finally:
            j._gancho_carrera = None
    filas = _sesiones(tmp_path).execute(
        "SELECT runtime_instance, issued_at, expires_at FROM runtime_sessions"
    ).fetchall()
    assert len(filas) == 5
    for f in filas:
        assert f["expires_at"] > f["issued_at"], f["runtime_instance"]
        assert f["issued_at"] <= r.t, (
            f"{f['runtime_instance']} dice haberse emitido en el FUTURO")
    j.close()


# ══ la otra causa del mismo estado: el TTL del argumento ════════════════════

@pytest.mark.parametrize("ttl", [0, -1, -900])
def test_open_session_con_ttl_no_positivo_se_RECHAZA(tmp_path, ttl):
    j = journal(tmp_path, admision=False)  # `admision=False`: este test CUENTA filas de identidad, y las
    # credenciales/sesiones que el arnés usa para abrir la barrera son
    # filas como cualquier otra. No acepta eventos, así que no necesita
    # la puerta abierta.
    j.bind_credential("c", principal="p", role="be", lane="llminbox")
    with pytest.raises(C.OperationInvalid):
        j.open_session("c", ttl_s=ttl)
    assert _sesiones(tmp_path).execute(
        "SELECT COUNT(*) c FROM runtime_sessions").fetchone()["c"] == 0
    j.close()


@pytest.mark.parametrize("ttl", [0, -1, -900])
def test_refresh_con_ttl_no_positivo_se_RECHAZA_y_NO_mata_la_viva(tmp_path, ttl):
    """El daño específico de esta puerta: sin la guarda, la rotación revoca la
    sesión VIVA y devuelve a cambio un token ya vencido. Se queda sin las dos."""
    j = journal(tmp_path)
    s = sesion(j, "c", principal="p", role="be", ttl_s=900)
    with pytest.raises(C.OperationInvalid):
        j.refresh_session(s.token, ttl_s=ttl)
    assert j.authenticate(s.token) is not None, (
        "la sesión viva murió en un refresh que ni siquiera llegó a emitir")
    j.close()


@pytest.mark.parametrize("puerta", ["open", "refresh"])
def test_OMEGA_un_ttl_de_UN_segundo_sigue_valiendo(tmp_path, puerta):
    """⊕ La guarda es `<= 0`, no «pequeño». Un TTL corto es una decisión
    legítima del llamante y la cura no puede convertirla en un error."""
    r = Reloj()
    j = journal(tmp_path, reloj=r)
    s = sesion(j, "c", principal="p", role="be", ttl_s=900)
    emitida = j.open_session("c", ttl_s=1) if puerta == "open" else \
        j.refresh_session(s.token, ttl_s=1)
    assert emitida.expires_at == r.t + 1
    assert j.authenticate(emitida.token) is not None
    r.avanza(2)
    assert j.authenticate(emitida.token) is None, "y sigue venciendo cuando toca"
    j.close()
