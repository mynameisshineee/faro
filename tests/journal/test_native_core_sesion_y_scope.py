"""Los tres primitives que quedaban sin sujeto: revocación, profundidad de
outbox y `may_execute`.

`@contratosbik` (`RULING 19:44`, ① y ④) midió que `revoke_session` y
`may_execute` **no toman `token`**, en un núcleo cuya doctrina es «el sujeto sale
de la credencial, no de la URL» — rota justo en los métodos que se le escaparon.
`revoke_session` además casaba CUALQUIER `runtime_instance`: un interruptor de
apagado repartido entre principals y entre carriles.

No se cura en el router: un router que compruebe de quién es la sesión es la
segunda fuente. Va aquí, y dentro de la transacción que muta.
"""
from __future__ import annotations

import pytest

import coordination as C
from ._arnes import INTENT, journal, sesion, sesiones


def jr(tmp_path, **kw):
    return journal(tmp_path, **kw)


# ══ revoke_current · el caso seguro, y transaccional ════════════════════════

def test_revoke_current_mata_la_PROPIA_sesion(tmp_path):
    j = jr(tmp_path)
    s = sesion(j, "c-be", principal="p-be", role="be")
    assert j.authenticate(s.token) is not None
    j.revoke_current(s.token, reason="fin de turno")
    assert j.authenticate(s.token) is None
    j.close()


def test_revoke_current_NO_toca_las_otras_sesiones_del_mismo_principal(tmp_path):
    """Revocar la propia es suicidio de sesión, no del principal."""
    j = jr(tmp_path)
    j.bind_credential("c-be", principal="p-be", role="be", lane="llminbox")
    a = j.open_session("c-be")
    b = j.open_session("c-be")
    j.revoke_current(a.token, reason="x")
    assert j.authenticate(a.token) is None
    assert j.authenticate(b.token) is not None
    j.close()


def test_revoke_current_es_IDEMPOTENTE_y_no_resucita(tmp_path):
    j = jr(tmp_path)
    s = sesion(j, "c-be", principal="p-be", role="be")
    j.revoke_current(s.token, reason="uno")
    with pytest.raises(C.AuthError):
        j.revoke_current(s.token, reason="dos")
    assert j.authenticate(s.token) is None
    j.close()


def test_revoke_current_con_token_desconocido_da_AuthError(tmp_path):
    j = jr(tmp_path)
    sesion(j, "c-be", principal="p-be", role="be")
    with pytest.raises(C.AuthError):
        j.revoke_current("token-que-nadie-emitio", reason="x")
    j.close()


def test_revoke_current_corta_la_MUTACION_siguiente(tmp_path):
    """El falsador que importa: revocar tiene que impedir escribir, no sólo
    ensuciar una columna."""
    j = jr(tmp_path)
    s = sesion(j, "c-be", principal="p-be", role="be")
    j.revoke_current(s.token, reason="x")
    with pytest.raises(C.AuthError):
        j.accept_event(s.token, idempotency_key="k", intent=INTENT,
                       ledger="llminbox")
    j.close()


# ══ revoke_session · ya no es un interruptor repartido ══════════════════════

def test_revoke_session_AJENA_de_otro_principal_se_RECHAZA(tmp_path):
    """⊖ de `@contratosbik` ①: `principal A` revoca el `rti` de `principal B`
    ⇒ debe negarse, y B tiene que poder mutar inmediatamente después."""
    j = jr(tmp_path)
    a, b = sesiones(j,
        {"credential": "c-a", "principal": "p-a", "role": "be"},
        {"credential": "c-b", "principal": "p-b", "role": "cto"})
    with pytest.raises(C.PolicyDenied):
        j.revoke_session(a.token, b.runtime_instance, reason="apagon")
    ev = j.accept_event(b.token, idempotency_key="k", intent=INTENT,
                        ledger="llminbox")
    assert ev.event_id
    j.close()


def test_el_principal_id_YA_deriva_del_carril(tmp_path):
    """La propiedad de la que depende que `revoke_session` no cruce carriles.

    Se falsa ESTO y no la cláusula `fila["lane"] == view.lane`, porque esa rama
    es INALCANZABLE mientras esta propiedad se cumpla: si el `principal_id` casa,
    el carril casa por construcción. Un mutante sobre una rama que no puede
    morir se lee en el informe igual que uno vivo por defecto — lo cazó mi propio
    runner (`MS1-revoke-sin-carril` sobrevivió) y ésta es la cura honesta.

    El día que `principal_id` deje de derivar del carril, este test se pone rojo
    y la cláusula pasa de defensa en profundidad a barrera necesaria.
    """
    j = jr(tmp_path)
    j.bind_credential("c1", principal="p-comun", role="be", lane="carril-uno")
    j.bind_credential("c2", principal="p-comun", role="be", lane="carril-dos")
    a = j.open_session("c1")
    b = j.open_session("c2")
    assert a.principal_id != b.principal_id, (
        "el mismo `principal` en dos carriles comparte `principal_id`: la "
        "comprobación de carril de `revoke_session` ha dejado de ser redundante "
        "y ahora es LA barrera — dale su propio falsador")
    j.close()


def test_revoke_session_de_OTRO_CARRIL_se_rechaza(tmp_path):
    """El interruptor no cruza carriles. Hoy lo cierra el `principal_id` (ver el
    test de arriba), no la cláusula de carril — se prueba la CONDUCTA igual,
    porque es la que el llamante necesita que se cumpla."""
    j = jr(tmp_path)
    a, b = sesiones(j,
        {"credential": "c-a", "principal": "p-a", "role": "be", "lane": "carril-uno"},
        {"credential": "c-b", "principal": "p-b", "role": "be", "lane": "carril-dos"})
    with pytest.raises(C.PolicyDenied):
        j.revoke_session(a.token, b.runtime_instance, reason="x")
    assert j.authenticate(b.token) is not None
    j.close()


def test_revoke_session_OMEGA_la_OTRA_sesion_PROPIA_sigue_funcionando(tmp_path):
    """⊖ de alcance: el caso legítimo del ADR es revocar OTRA sesión mía. Si esto
    se cerrara, la cura habría matado el motivo por el que el método existe."""
    j = jr(tmp_path)
    j.bind_credential("c-be", principal="p-be", role="be", lane="llminbox")
    a = j.open_session("c-be")
    b = j.open_session("c-be")
    j.revoke_session(a.token, b.runtime_instance, reason="limpieza")
    assert j.authenticate(b.token) is None
    assert j.authenticate(a.token) is not None
    j.close()


def test_revoke_session_de_un_rti_INEXISTENTE_no_dice_si_existe(tmp_path):
    """Sin oráculo de enumeración: un `rti` de otro y uno inventado tienen que
    ser indistinguibles para quien no puede tocarlos."""
    j = jr(tmp_path)
    a, b = sesiones(j,
        {"credential": "c-a", "principal": "p-a", "role": "be"},
        {"credential": "c-b", "principal": "p-b", "role": "cto"})
    with pytest.raises(C.PolicyDenied) as e_otro:
        j.revoke_session(a.token, b.runtime_instance, reason="x")
    with pytest.raises(C.PolicyDenied) as e_fake:
        j.revoke_session(a.token, "rti_no_existe", reason="x")
    assert str(e_otro.value) == str(e_fake.value)
    j.close()


# ══ pending / unresolved · con sujeto y por carril ══════════════════════════

def test_pending_outbox_cuenta_SOLO_el_carril_de_la_sesion(tmp_path):
    j = jr(tmp_path)
    a, b = sesiones(j,
        {"credential": "c-a", "principal": "p-a", "role": "be", "lane": "carril-uno"},
        {"credential": "c-b", "principal": "p-b", "role": "be", "lane": "carril-dos"})
    j.accept_event(a.token, idempotency_key="k1", intent=INTENT, ledger="ledger-uno")
    j.accept_event(a.token, idempotency_key="k2", intent={**INTENT, "head": "otro"},
                   ledger="ledger-uno")
    j.accept_event(b.token, idempotency_key="k3", intent=INTENT, ledger="ledger-dos")
    assert j.pending_outbox(a.token) == 2
    assert j.pending_outbox(b.token) == 1
    j.close()


def test_unresolved_outbox_tambien_es_por_carril(tmp_path):
    j = jr(tmp_path)
    a, b = sesiones(j,
        {"credential": "c-a", "principal": "p-a", "role": "be", "lane": "carril-uno"},
        {"credential": "c-b", "principal": "p-b", "role": "be", "lane": "carril-dos"})
    j.accept_event(a.token, idempotency_key="k1", intent=INTENT, ledger="ledger-uno")
    assert j.unresolved_outbox(a.token) == 1
    assert j.unresolved_outbox(b.token) == 0
    j.close()


def test_pending_outbox_SIN_sesion_valida_se_rechaza(tmp_path):
    """Fail-closed: la profundidad de la cola de un carril es información del
    carril. Sin sesión no se sirve."""
    j = jr(tmp_path)
    sesion(j, "c-be", principal="p-be", role="be")
    with pytest.raises(C.AuthError):
        j.pending_outbox("token-invalido")
    j.close()


# ══ may_execute · con sujeto, y sin oráculo de existencia ═══════════════════

def test_may_execute_exige_sesion(tmp_path):
    j = jr(tmp_path)
    s = sesion(j, "c-be", principal="p-be", role="be")
    cid, _ = j.submit_command(s.token, workstream_id="w", revision=1,
                              payload={"o": "x"})
    assert j.may_execute(s.token, cid) is True
    with pytest.raises(C.AuthError):
        j.may_execute("token-invalido", cid)
    j.close()


def test_may_execute_un_comando_de_OTRO_CARRIL_es_indistinguible_de_inexistente(tmp_path):
    """`@contratosbik` ④: el veredicto no cruzaba, pero la EXISTENCIA sí se
    confirmaba —un id de otro carril daba `false` igual que uno inventado, y eso
    es distinguible por otras vías—. Con sujeto, los dos son el mismo `404`."""
    j = jr(tmp_path)
    a, b = sesiones(j,
        {"credential": "c-a", "principal": "p-a", "role": "be", "lane": "carril-uno"},
        {"credential": "c-b", "principal": "p-b", "role": "be", "lane": "carril-dos"})
    ajeno, _ = j.submit_command(b.token, workstream_id="w", revision=1,
                                payload={"o": "x"})
    with pytest.raises(C.SubjectNotFound) as e_otro:
        j.may_execute(a.token, ajeno)
    with pytest.raises(C.SubjectNotFound) as e_fake:
        j.may_execute(a.token, "cmd_no_existe")
    # Se compara la FORMA, no la cadena: el mensaje lleva el `command_id` que el
    # propio llamante pasó, así que dos cadenas distintas ahí no prueban fuga —
    # ésa fue mi primera versión y no discriminaba nada. Lo que no puede
    # distinguirse es el resto del mensaje ni el tipo.
    forma = lambda e, cid: str(e.value).replace(cid, "<ID>")
    assert forma(e_otro, ajeno) == forma(e_fake, "cmd_no_existe")
    assert type(e_otro.value) is type(e_fake.value)
    j.close()


def test_may_execute_OMEGA_la_supersesion_sigue_decidiendo(tmp_path):
    """⊖ de alcance: añadir sujeto no puede cambiar el veredicto."""
    j = jr(tmp_path)
    s = sesion(j, "c-be", principal="p-be", role="be")
    c1, _ = j.submit_command(s.token, workstream_id="w", revision=1, payload={"o": "1"})
    assert j.may_execute(s.token, c1) is True
    c2, _ = j.submit_command(s.token, workstream_id="w", revision=2, payload={"o": "2"})
    assert j.may_execute(s.token, c2) is True
    assert j.may_execute(s.token, c1) is False, "la revisión vieja no puede arrancar"
    j.close()
