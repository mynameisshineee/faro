"""WIP global en modo OBSERVE: mide, no rechaza. Para el shadow del canon v1 (@harness).

POR QUÉ SÓLO WARN, y esto es diseño y no cautela: bajar el tope por owner de 3 a 1 y
repartir competencias entre harness y llminbox son decisiones del operador, no mías. Un
`reject` construido antes de esa adjudicación es una puerta que ya existe esperando un
flag — y este repo tiene la regla de que un gate es una DEPENDENCIA, no un flag, justo
para que «apagado» no sea una promesa sino una imposibilidad. Así que el camino de
rechazo no se escribe hasta que haya a quién obedecer.

CONFIGURACIÓN, con la disciplina del repo: ausente ⇒ apagado, callando. Presente e
inválido ⇒ SystemExit ruidoso en el arranque, nunca un default silencioso.
"""
from __future__ import annotations
import os
import sqlite3

import pytest


def test_ausente_no_declara_nada(cliente, servicio):
    """⊖ el importante: si el campo apareciera con el tope por defecto puesto, el shadow
    leería un tope que nadie configuró y lo tomaría por política."""
    r = cliente.post("/claim", json={"tema": "wip-a", "agent": sorted(servicio.lp.CANON)[0]})
    assert r.status_code == 200
    assert r.json().get("wip") is None, (
        "sin `LLMINBOX_WIP_GLOBAL` el endpoint declara un WIP: eso es inventarse una "
        "política que nadie ha adjudicado")


def test_presente_mide_y_NO_rechaza(monkeypatch, tmp_path):
    """⊕ anti-gate: con el tope a 1 y dos claims, el segundo tiene que ENTRAR igual. Lo
    que cambia es que se declara `excede`, no que se cierre la puerta."""
    from .conftest import construir
    from fastapi.testclient import TestClient
    monkeypatch.setenv("LLMINBOX_WIP_GLOBAL", "1")
    s = construir(tmp_path, monkeypatch)
    with TestClient(s.app) as c:
        s.barrido()
        c.headers.update({"X-Llminbox-Token": "test-token"})
        nombres = sorted(s.lp.CANON)[:2]
        r1 = c.post("/claim", json={"tema": "wip-1", "agent": nombres[0]}).json()
        r2 = c.post("/claim", json={"tema": "wip-2", "agent": nombres[1]}).json()
        assert r1["ok"] is True and r2["ok"] is True, (r1, r2)
        assert r2["wip"]["tope"] == 1
        assert r2["wip"]["vivos"] >= 2
        assert r2["wip"]["excede"] is True, "con 2 vivos y tope 1 no marca exceso"
        assert r1["wip"]["excede"] is False, "el primero no excede y sale marcado"
        # y el EFECTO en la tabla: los dos entraron, que es lo que «warn» significa
        con = sqlite3.connect(os.environ["LLMINBOX_DB"])
        n = con.execute("SELECT COUNT(*) FROM claims WHERE cerrado IS NULL "
                        "AND rol='ejecuta'").fetchone()[0]
        con.close()
        assert n == 2, f"modo warn con {n} filas: alguien está rechazando"


def test_un_valor_ilegible_no_arranca(monkeypatch, tmp_path):
    """Config presente e inválida ⇒ ruido. Un `LLMINBOX_WIP_GLOBAL=cuatro` que cayera a
    «apagado» dejaría el shadow midiendo nada y creyendo que mide."""
    from .conftest import construir
    monkeypatch.setenv("LLMINBOX_WIP_GLOBAL", "cuatro")
    with pytest.raises(SystemExit) as e:
        construir(tmp_path, monkeypatch)
    assert "WIP_GLOBAL" in str(e.value)


def test_cero_no_es_apagado_es_un_error(monkeypatch, tmp_path):
    """`0` como «sin tope» sería un sinónimo silencioso de ausente, y este repo no
    acepta sinónimos: un tope de 0 es una política imposible (nadie puede coger nada),
    así que decirlo es un error, no una forma de apagar."""
    from .conftest import construir
    monkeypatch.setenv("LLMINBOX_WIP_GLOBAL", "0")
    with pytest.raises(SystemExit):
        construir(tmp_path, monkeypatch)
