"""P1 del auditor: las dos puertas del lease leen el reloj FUERA de la transacción.

`now = self._clock()` se captura antes de `_tras_precheck()` y del
`BEGIN IMMEDIATE`, y la decisión de vencimiento se toma con ese valor. Entre la
lectura y el `UPDATE` cabe el vencimiento entero.

Reproducido con el seam (`_gancho_carrera`), que es el mecanismo que este repo
ya tiene para falsar esa ventana sin `sleep` — un test que duerme mide la
paciencia del CI, no la carrera:

    renew   ttl=100, el hook avanza +101
            ⇒ ACEPTA, fencing 1->1, devuelve expires_at=1000100 con now=1000101
              o sea: promete continuidad Y entrega una valla ya caducada.

    acquire  el defecto SIMÉTRICO, y por eso se cura a la vez: con el reloj
             viejo el lease se ve VIVO, así que NIEGA un relevo legítimo. Falla
             cerrado, pero es el mismo defecto — curar uno y dejar el gemelo es
             dejar el bug con otra cara.
"""
from __future__ import annotations

import pytest

import coordination as C
from ._arnes import Reloj, journal, sesion, sesiones


def test_renew_NO_acepta_un_lease_que_vence_en_la_ventana(tmp_path):
    r = Reloj()
    j = journal(tmp_path, reloj=r)
    s = sesion(j, "c", principal="p", role="be", ttl_s=99999)
    l0 = j.acquire_lease(s.token, "deploy", ttl_s=100)
    j._gancho_carrera = lambda: r.avanza(101)
    try:
        with pytest.raises(C.LeaseConflict):
            j.renew_lease(s.token, "deploy", ttl_s=100)
    finally:
        j._gancho_carrera = None
    # y el fencing NO se ha consumido: el siguiente acquire estrena el N+1
    assert j.acquire_lease(s.token, "deploy", ttl_s=100).fencing_token == \
        l0.fencing_token + 1
    j.close()


def test_renew_NUNCA_devuelve_un_expires_at_ya_pasado(tmp_path):
    """El daño que se ve desde fuera: el llamante recibe `200` y una valla
    caducada, y sigue mutando con un token que ya no autoriza."""
    r = Reloj()
    j = journal(tmp_path, reloj=r)
    s = sesion(j, "c", principal="p", role="be", ttl_s=99999)
    j.acquire_lease(s.token, "deploy", ttl_s=100)
    j._gancho_carrera = lambda: r.avanza(101)
    try:
        with pytest.raises(C.LeaseConflict):
            l = j.renew_lease(s.token, "deploy", ttl_s=100)
            assert l.expires_at > r.t, (
                f"devolvió expires_at={l.expires_at} con now={r.t}: caducada al nacer")
    finally:
        j._gancho_carrera = None
    j.close()


def test_acquire_SI_releva_cuando_vence_en_la_ventana(tmp_path):
    """El gemelo: con el reloj viejo el lease se ve vivo y se niega un relevo
    legítimo. Falla cerrado, y sigue siendo el mismo defecto."""
    r = Reloj()
    j = journal(tmp_path, reloj=r)
    a, b = sesiones(j,
        {"credential": "c-a", "principal": "p-a", "role": "be", "ttl_s": 99999},
        {"credential": "c-b", "principal": "p-b", "role": "cto", "ttl_s": 99999})
    l0 = j.acquire_lease(a.token, "deploy", ttl_s=100)
    j._gancho_carrera = lambda: r.avanza(101)
    try:
        l1 = j.acquire_lease(b.token, "deploy", ttl_s=100)
    finally:
        j._gancho_carrera = None
    assert l1.fencing_token == l0.fencing_token + 1
    assert l1.principal_id != l0.principal_id
    j.close()


def test_el_expires_at_nuevo_se_calcula_con_el_reloj_de_DENTRO(tmp_path):
    """No basta con decidir bien: el `expires_at` que se escribe tiene que salir
    del mismo reloj. Con el viejo, un renew legítimo nace con el TTL ya comido."""
    r = Reloj()
    j = journal(tmp_path, reloj=r)
    s = sesion(j, "c", principal="p", role="be", ttl_s=99999)
    j.acquire_lease(s.token, "deploy", ttl_s=1000)
    j._gancho_carrera = lambda: r.avanza(500)      # avanza, pero NO vence
    try:
        l = j.renew_lease(s.token, "deploy", ttl_s=1000)
    finally:
        j._gancho_carrera = None
    assert l.expires_at == pytest.approx(r.t + 1000), (
        f"expires_at={l.expires_at} sale del reloj viejo; con el de dentro sería "
        f"{r.t + 1000}")
    j.close()


def test_OMEGA_sin_carrera_las_dos_puertas_siguen_como_antes(tmp_path):
    """⊖ de alcance: mover la lectura del reloj no puede cambiar el camino
    normal. Si esto se rompiera, la cura habría movido la conducta, no el bug."""
    r = Reloj()
    j = journal(tmp_path, reloj=r)
    s = sesion(j, "c", principal="p", role="be", ttl_s=99999)
    l0 = j.acquire_lease(s.token, "deploy", ttl_s=100)
    r.avanza(50)
    l1 = j.renew_lease(s.token, "deploy", ttl_s=100)
    assert l1.fencing_token == l0.fencing_token and l1.expires_at > l0.expires_at
    r.avanza(101)
    with pytest.raises(C.LeaseConflict):
        j.renew_lease(s.token, "deploy", ttl_s=100)
    assert j.acquire_lease(s.token, "deploy").fencing_token == l0.fencing_token + 1
    j.close()
