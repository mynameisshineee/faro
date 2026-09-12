"""El ack contesta lo mismo que su vecino cuando la base está ocupada.

MEDIDO EN VIVO (2026-08-31, instancia candidata, base bloqueada desde fuera con
`BEGIN EXCLUSIVE` — sin tocar el código, que es como se induce un fallo sin dejar un
inyector dentro):

    POST /inbox/{a}/leido  -> 503 {"error":"base ocupada", "que_hacer":"reintenta: el
                                   cursor NO ha avanzado…", "cursor_avanzado": false}
    POST /vigilancia/ack   -> 500 Internal Server Error

Mismo modo de fallo, dos respuestas. Y el ② se cumple —hay quien actúa sobre la
distinción—: el ack es EL endpoint que acredita el ciclo del hombre muerto, así que su
llamador tiene que decidir entre reintentar y declarar `ciclo_ok=false`. Con un 500
opaco no puede: «la base estaba un segundo ocupada» y «tu ciclo falló» llegan iguales.

Un watcher prudente ante un 500 declara el fallo, y entonces `/health` se pone en
`fallida` por una contención de dos segundos. Uno optimista lo ignora y calla un fallo
real. Ninguna de las dos lecturas es culpa suya: la respuesta no las distingue.

La cura ya existía en el vecino (#32) y sólo faltaba aplicarla aquí. Es «ya está en el
repo», no diseño nuevo.
"""
from __future__ import annotations
import sqlite3
import pytest
from fastapi.testclient import TestClient
from .conftest import construir

WT = "w"


@pytest.fixture
def cli(tmp_path, monkeypatch):
    monkeypatch.setenv("LLMINBOX_WATCHER_TOKEN", WT)
    s = construir(tmp_path, monkeypatch)
    con = s.db(); s._preparar_indice(con); con.close()
    return TestClient(s.app), s


def _H(s):
    return {"X-Llminbox-Token": s.TOKEN, "X-Llminbox-Watcher": WT}


def _bloqueada_al_escribir(s, msg="database is locked"):
    """Como una base bloqueada DE VERDAD: abre bien y revienta en el `execute`."""
    real = s.db
    def fabrica(*a, **k):
        con = real(*a, **k)
        class Presa:
            def execute(self, *aa, **kk):
                raise sqlite3.OperationalError(msg)
            def __getattr__(self, n):
                return getattr(con, n)
        return Presa()
    return fabrica


def test_base_ocupada_da_503_accionable_no_500_opaco(cli, monkeypatch):
    c, s = cli
    assert c.post("/vigilancia/ack", headers=_H(s), params={"quien": "w"}).status_code == 200, \
        "sin línea base en 200 el ⊖ no mide nada"

    # ⚠ EL STUB FALLA DONDE FALLA PRODUCCIÓN. Mi primera versión hacía fallar `db()` —la
    # APERTURA— y con eso el arreglo pasó los tests y NO funcionó en vivo: medido contra
    # la instancia con `BEGIN EXCLUSIVE`, sqlite abre tan campante y salta al ESCRIBIR.
    # Un stub que inventa el punto de fallo certifica un arreglo que no cura nada.
    monkeypatch.setattr(s, "db", _bloqueada_al_escribir(s))

    r = c.post("/vigilancia/ack", headers=_H(s), params={"quien": "w"})
    assert r.status_code == 503, f"sigue siendo {r.status_code}: el llamador no puede decidir"
    d = r.json()["detail"]
    assert d["error"] == "base ocupada"
    assert d["latido"] is False, "un ack que no escribió NO puede insinuar que latió"
    assert "reintenta" in d["que_hacer"].lower()


def test_un_error_que_NO_es_contencion_sigue_subiendo(cli, monkeypatch):
    """⊖ del alcance: el 503 es para «ocupada», no para todo. Tragarse un fallo real
    convirtiéndolo en «reintenta» haría que el watcher reintentase para siempre contra
    una base rota, y el hombre muerto nunca se enteraría."""
    c, s = cli
    monkeypatch.setattr(s, "db", _bloqueada_al_escribir(s, "no such table: meta"))
    # El TestClient RE-LANZA lo no manejado en vez de devolver 500, así que «sube» aquí
    # significa literalmente que la excepción sale. Es la forma correcta de comprobarlo:
    # si algún día el 503 se tragara esto, el `raises` fallaría en vez de pasar por
    # comparar dos números que casualmente no son iguales.
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        c.post("/vigilancia/ack", headers=_H(s), params={"quien": "w"})


def test_el_ciclo_fallido_tambien_dice_que_hacer(cli, monkeypatch):
    """El camino `ciclo_ok=false` escribe igual, así que sufre la misma contención."""
    c, s = cli
    monkeypatch.setattr(s, "db", _bloqueada_al_escribir(s))
    r = c.post("/vigilancia/ack", headers=_H(s),
               params={"quien": "w", "ciclo_ok": "false", "motivo": "fetch"})
    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "base ocupada"
