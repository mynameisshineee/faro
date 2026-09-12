"""Dos carriles con nombres IGUALES no pueden interferir.

El carril es la frontera de aislamiento del producto. Que funcione no puede
depender de que nadie llame `deploy` a su recurso en dos sitios: los nombres los
eligen personas distintas que no se conocen, así que la colisión no es un caso
raro, es lo que va a pasar.
"""
from __future__ import annotations

import pytest

import coordination as C
from ._arnes import INTENT, Reloj, journal, sesion, sesiones


def _dos_carriles(j):
    a, b = sesiones(j,
        {"credential": "cred-A", "principal": "be-uno", "role": "be", "lane": "carril-uno"},
        {"credential": "cred-B", "principal": "be-dos", "role": "be", "lane": "carril-dos"})
    return a, b


def test_el_mismo_nombre_de_recurso_en_dos_carriles_son_DOS_leases(tmp_path):
    j = journal(tmp_path)
    a, b = _dos_carriles(j)
    la = j.acquire_lease(a.token, "deploy", ttl_s=300)
    lb = j.acquire_lease(b.token, "deploy", ttl_s=300)   # NO debe chocar
    assert (la.lane, lb.lane) == ("carril-uno", "carril-dos")
    assert j._connect().execute("SELECT COUNT(*) c FROM leases").fetchone()["c"] == 2
    j.close()


def test_el_fencing_de_un_carril_no_vale_en_el_otro(tmp_path):
    """⚠️ Este control sólo discrimina si los dos carriles tienen tokens
    DISTINTOS. Escrito de la forma obvia —un lease en cada carril— los dos nacen
    en `1` y la aserción pasa por casualidad, midiendo cero. Así que primero se
    fuerza un relevo en un carril para llevarlo a `2`, y ENTONCES se pregunta.
    """
    reloj = Reloj()
    j = journal(tmp_path, reloj=reloj)
    a, b = _dos_carriles(j)
    j.acquire_lease(a.token, "deploy", ttl_s=60)     # carril-uno, token 1
    j.acquire_lease(b.token, "deploy", ttl_s=600)    # carril-dos, token 1

    reloj.avanza(61)                                  # vence sólo el de carril-uno
    a2 = j.open_session("cred-A")
    nuevo = j.acquire_lease(a2.token, "deploy", ttl_s=600)
    assert nuevo.fencing_token == 2                   # carril-uno va por 2

    j.check_fence(a2.token, "deploy", 2)              # ⊕ el 2 vale en carril-uno
    with pytest.raises(C.FencingConflict):
        j.check_fence(b.token, "deploy", 2)           # ⊖ y NO vale en carril-dos
    j.check_fence(b.token, "deploy", 1)               # ⊕ allí el vigente sigue 1
    j.close()


def test_soltar_el_deploy_de_un_carril_no_suelta_el_del_otro(tmp_path):
    j = journal(tmp_path)
    a, b = _dos_carriles(j)
    j.acquire_lease(a.token, "deploy", ttl_s=300)
    j.acquire_lease(b.token, "deploy", ttl_s=300)
    j.release_lease(a.token, "deploy")
    vivos = j._connect().execute(
        "SELECT lane FROM leases WHERE released_at IS NULL").fetchall()
    assert [r["lane"] for r in vivos] == ["carril-dos"]
    j.close()


def test_las_revisiones_de_un_workstream_homonimo_no_se_supersiden(tmp_path):
    """`ws` en dos carriles son dos workstreams. Sin el carril en la clave, la
    revisión 2 de uno mataría la 1 del otro — y la víctima no se enteraría."""
    j = journal(tmp_path)
    a, b = _dos_carriles(j)
    c1, e1 = j.submit_command(a.token, workstream_id="ws", revision=1, payload={})
    c2, e2 = j.submit_command(b.token, workstream_id="ws", revision=2, payload={})
    assert (e1, e2) == ("accepted", "accepted")
    assert j.may_execute(a.token, c1) is True, "la revisión ajena supersidió la mía"
    assert j.may_execute(b.token, c2) is True
    j.close()


def test_una_causa_nativa_no_puede_cruzar_de_carril(tmp_path):
    j = journal(tmp_path)
    a, b = _dos_carriles(j)
    ev_a = j.accept_event(a.token, idempotency_key="k", intent=INTENT,
                          ledger="ledger-uno")
    with pytest.raises(C.CauseRejected):
        j.accept_event(b.token, idempotency_key="k2", intent=INTENT,
                       ledger="ledger-dos", causes=[ev_a.event_id])
    # ⊕ dentro del suyo, la misma causa entra sin problema.
    j.accept_event(a.token, idempotency_key="k3",
                   intent={**INTENT, "head": "otro"}, ledger="ledger-uno",
                   causes=[ev_a.event_id])
    j.close()


def test_un_comando_no_puede_citar_causa_de_otro_carril(tmp_path):
    j = journal(tmp_path)
    a, b = _dos_carriles(j)
    ca, _ = j.submit_command(a.token, workstream_id="wsa", revision=1, payload={})
    with pytest.raises(C.CauseRejected):
        j.submit_command(b.token, workstream_id="wsb", revision=1, payload={},
                         causes=[ca])
    j.close()


def test_un_comando_puede_citar_un_EVENTO_nativo_con_FK_tipada(tmp_path):
    """ADR §Commands: las causas nativas son eventos O comandos, con FK. Cada
    tipo en su tabla, porque una FK no puede ser condicional."""
    j = journal(tmp_path)
    a, _ = _dos_carriles(j)
    ev = j.accept_event(a.token, idempotency_key="k", intent=INTENT, ledger="l")
    c1, _ = j.submit_command(a.token, workstream_id="ws", revision=1, payload={})
    c2, _ = j.submit_command(a.token, workstream_id="ws", revision=2, payload={},
                             causes=[ev.event_id, c1])
    con = j._connect()
    assert con.execute("SELECT cause_event_id FROM command_event_causes"
                       " WHERE command_id=?", (c2,)).fetchone()["cause_event_id"] \
        == ev.event_id
    assert con.execute("SELECT cause_command_id FROM command_causes"
                       " WHERE command_id=?", (c2,)).fetchone()["cause_command_id"] == c1
    j.close()


def test_la_idempotencia_esta_acotada_por_carril(tmp_path):
    j = journal(tmp_path)
    a, b = _dos_carriles(j)
    ra = j.accept_event(a.token, idempotency_key="misma", intent=INTENT, ledger="l")
    rb = j.accept_event(b.token, idempotency_key="misma", intent=INTENT, ledger="l")
    assert ra.event_id != rb.event_id and rb.replayed is False
    j.close()
