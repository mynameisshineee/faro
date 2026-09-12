"""Tope de ejecución POR OWNER: «1 sólo para EM/fe» sin tocar a los otros 14.

Lo diseñó @harness y el diagnóstico era correcto: `LLMINBOX_TOPE_EJECUTA` es global, así
que un `=1` bajaba el tope de TODA la flota. El caso que el operador quiere autorizar no
era expresable.

SUS CUATRO FALSADORES, adoptados tal cual:
  ① EM con 1 en vuelo → 2º claim RECHAZADO
  ② control ⊖: un owner NO listado con 2 en vuelo → 3º ACEPTADO (el global sigue intacto).
     Éste es el que prueba que no es un global disfrazado.
  ③ el rechazo aguanta la carrera (dos simultáneos con 0 en vuelo → entra exactamente 1)
  ④ mapa ausente → conducta idéntica a hoy

TRES MÍOS, y el primero no es cosmético:

  ⑤ LA CLAVE ES EL ROL, NO LA FIRMA. Este repo ya aprendió que el censo tiene 51 nombres
     para 27 roles, y por eso el claim se guarda con `lp.rol_de(...)`. Si el mapa se
     indexara por el nombre con que alguien firma, `engineering-manager` capado a 1 se
     esquivaría firmando con cualquier otro alias del mismo rol — y el cap sería un
     letrero, no una puerta. Es el mismo motivo por el que el tope de revisores cuenta
     roles y no firmas.

  ⑥ UN OWNER QUE NO ESTÁ EN EL CENSO PARA EL ARRANQUE. `engineering-manger:1` (typo) sería
     un cap para nadie: se autoriza el 3→1, se configura, el arranque calla, y el piloto
     corre tres días con EM en 3 creyendo que está en 1. Es exactamente lo que acaba de
     pasarme con las doce perillas inertes, y no lo repito.

  ⑦ UN TOPE ILEGIBLE O <1 PARA. `0` no es «sin tope»: sería un sinónimo silencioso de
     ausente y aquí no hay sinónimos.
"""
from __future__ import annotations
import os
import sqlite3
import threading

import pytest


def _vivos(rol: str) -> int:
    con = sqlite3.connect(os.environ["LLMINBOX_DB"])
    try:
        return con.execute("SELECT COUNT(*) FROM claims WHERE agent=? AND rol='ejecuta' "
                           "AND cerrado IS NULL", (rol,)).fetchone()[0]
    finally:
        con.close()


def _limpia(rol: str) -> None:
    con = sqlite3.connect(os.environ["LLMINBOX_DB"])
    con.execute("DELETE FROM claims WHERE agent=?", (rol,)); con.commit(); con.close()


def _monta(monkeypatch, tmp_path, mapa):
    from .conftest import construir
    from fastapi.testclient import TestClient
    monkeypatch.setenv("LLMINBOX_TOPE_EJECUTA_POR_OWNER", mapa)
    s = construir(tmp_path, monkeypatch)
    c = TestClient(s.app)
    c.__enter__(); s.barrido()
    c.headers.update({"X-Llminbox-Token": "test-token"})
    return s, c


def _dos_nombres(s):
    """Dos nombres del censo que resuelven a ROLES distintos — si cogiéramos dos alias del
    mismo rol, el «control» compartiría cupo con el capado y el test mentiría."""
    por_rol: dict[str, str] = {}
    for n in sorted(s.lp.CANON):
        por_rol.setdefault(s.lp.rol_de(n), n)
    roles = list(por_rol)
    assert len(roles) >= 2, "el censo de prueba no tiene dos roles distintos"
    return por_rol[roles[0]], por_rol[roles[1]]


def test_el_owner_listado_se_capa_a_1(monkeypatch, tmp_path):
    """① de @harness."""
    from .conftest import construir
    s0 = construir(tmp_path, monkeypatch)
    capado, _ = _dos_nombres(s0)
    s, c = _monta(monkeypatch, tmp_path, f"{s0.lp.rol_de(capado)}=1")
    _limpia(s.lp.rol_de(capado))
    r1 = c.post("/claim", json={"tema": "a1", "agent": capado}).json()
    r2 = c.post("/claim", json={"tema": "a2", "agent": capado}).json()
    assert r1["ok"] is True, r1
    assert r2["ok"] is False and r2["tope"] == 1, r2
    assert _vivos(s.lp.rol_de(capado)) == 1, "el rechazo no se ve en la TABLA"


def test_control_el_owner_NO_listado_conserva_el_global(monkeypatch, tmp_path):
    """② de @harness — el que prueba que no es un global disfrazado."""
    from .conftest import construir
    s0 = construir(tmp_path, monkeypatch)
    capado, libre = _dos_nombres(s0)
    s, c = _monta(monkeypatch, tmp_path, f"{s0.lp.rol_de(capado)}=1")
    _limpia(s.lp.rol_de(libre))
    oks = [c.post("/claim", json={"tema": f"b{i}", "agent": libre}).json()["ok"]
           for i in range(3)]
    assert oks == [True, True, True], (
        f"el owner no listado quedó capado también ({oks}): es el global con otro nombre")
    assert _vivos(s.lp.rol_de(libre)) == 3


def test_el_cap_por_owner_aguanta_la_carrera(monkeypatch, tmp_path):
    """③ de @harness, medido en la TABLA. Ráfaga repetida: una sola tanda cazaba la
    carrera 1 de cada 3 veces cuando la medí en el tope global."""
    from .conftest import construir
    s0 = construir(tmp_path, monkeypatch)
    capado, _ = _dos_nombres(s0)
    rol = s0.lp.rol_de(capado)
    s, c = _monta(monkeypatch, tmp_path, f"{rol}=1")
    peor = 0
    for tanda in range(8):
        _limpia(rol)
        barrera = threading.Barrier(6)
        def coge(i, _t=tanda):
            barrera.wait(timeout=10)
            c.post("/claim", json={"tema": f"c{_t}-{i}", "agent": capado})
        hs = [threading.Thread(target=coge, args=(i,)) for i in range(6)]
        for h in hs: h.start()
        for h in hs: h.join(timeout=30)
        peor = max(peor, _vivos(rol))
    assert peor <= 1, f"{peor} claims vivos con el cap del owner en 1"


def test_mapa_ausente_no_cambia_nada(monkeypatch, tmp_path):
    """④ de @harness: sin regresión."""
    from .conftest import construir
    s0 = construir(tmp_path, monkeypatch)
    quien, _ = _dos_nombres(s0)
    s, c = _monta(monkeypatch, tmp_path, "")
    _limpia(s.lp.rol_de(quien))
    oks = [c.post("/claim", json={"tema": f"d{i}", "agent": quien}).json()["ok"]
           for i in range(4)]
    assert oks == [True, True, True, False], f"el global dejó de valer: {oks}"


def test_la_clave_es_el_ROL_y_no_la_firma(monkeypatch, tmp_path):
    """⑤ MÍO, y es el que convierte el cap en puerta. El censo tiene varios nombres por
    rol; si el mapa se indexara por firma, el capado se lo salta firmando con otro alias
    suyo. Sólo corre si el censo de prueba tiene un rol con dos nombres."""
    from .conftest import construir
    s0 = construir(tmp_path, monkeypatch)
    alias: dict[str, list[str]] = {}
    for n in sorted(s0.lp.CANON):
        alias.setdefault(s0.lp.rol_de(n), []).append(n)
    dobles = [(r, ns) for r, ns in alias.items() if len(ns) >= 2]
    if not dobles:
        pytest.skip("el censo de prueba no tiene ningún rol con dos nombres")
    rol, (n1, n2) = dobles[0][0], dobles[0][1][:2]
    s, c = _monta(monkeypatch, tmp_path, f"{rol}=1")
    _limpia(rol)
    assert c.post("/claim", json={"tema": "e1", "agent": n1}).json()["ok"] is True
    r = c.post("/claim", json={"tema": "e2", "agent": n2}).json()
    assert r["ok"] is False, (
        f"firmando con el otro alias del mismo rol ({n2}) se salta el cap: el mapa está "
        f"indexado por FIRMA y no por rol, así que es un letrero y no una puerta")


def test_un_owner_fuera_del_censo_PARA_el_arranque(monkeypatch, tmp_path):
    """⑥ MÍO. Un typo sería un cap para nadie, y el piloto correría tres días con el owner
    a 3 creyéndolo en 1. Es la avería de las doce perillas inertes, otra vez."""
    from .conftest import construir
    monkeypatch.setenv("LLMINBOX_TOPE_EJECUTA_POR_OWNER", "engineering-manger=1")
    with pytest.raises(SystemExit) as e:
        construir(tmp_path, monkeypatch)
    assert "engineering-manger" in str(e.value), "no dice cuál owner no existe"


def test_un_tope_ilegible_o_cero_PARA(monkeypatch, tmp_path):
    """⑦ MÍO. `0` no es «sin tope»: sería un sinónimo silencioso de ausente."""
    from .conftest import construir
    s0 = construir(tmp_path, monkeypatch)
    rol = s0.lp.rol_de(sorted(s0.lp.CANON)[0])
    for malo in (f"{rol}=cero", f"{rol}=0", f"{rol}=-1", rol, f"{rol}=1=2"):
        monkeypatch.setenv("LLMINBOX_TOPE_EJECUTA_POR_OWNER", malo)
        with pytest.raises(SystemExit):
            construir(tmp_path, monkeypatch)


def test_el_rol_escrito_con_otra_caja_SIGUE_capando(monkeypatch, tmp_path):
    """⑧ — lo cazó un ⊖ que sobrevivió, no yo.

    `rol_de` conserva las mayúsculas del roster, así que hay roles como `Operador`. Si el
    mapa guardara la clave TAL CUAL se escribió, un `operador=1` pasaría la validación (que
    compara sin distinguir caja) y luego el `get()` en caliente buscaría `Operador` y no
    encontraría nada: el cap quedaría MUDO.

    Es la misma avería que el owner fuera del censo —un cap para nadie, configurado y sin
    efecto— entrando por la puerta de al lado: la validación acepta, el runtime no aplica,
    y nadie protesta. Lo que evita las dos es que la clave que se guarda sea EXACTAMENTE
    la cadena que `rol_de` devuelve.
    """
    from .conftest import construir
    s0 = construir(tmp_path, monkeypatch)
    roles = {s0.lp.rol_de(n) for n in s0.lp.CANON}
    conmayus = next((r for r in sorted(roles) if r != r.lower()), None)
    if conmayus is None:
        pytest.skip("el censo de prueba no tiene ningún rol con mayúsculas")
    quien = next(n for n in sorted(s0.lp.CANON) if s0.lp.rol_de(n) == conmayus)

    s, c = _monta(monkeypatch, tmp_path, f"{conmayus.lower()}=1")
    _limpia(conmayus)
    assert c.post("/claim", json={"tema": "f1", "agent": quien}).json()["ok"] is True
    r = c.post("/claim", json={"tema": "f2", "agent": quien}).json()
    assert r["ok"] is False, (
        f"escrito en minúsculas, el cap de {conmayus!r} no aplica: la clave se guardó tal "
        f"cual y en caliente se busca la forma canónica. Cap configurado y mudo")
    assert _vivos(conmayus) == 1
