"""Marcar leído no puede morir con un 500 opaco cuando la base está ocupada.

Medido en producción el 2026-08-30: 791 de 791 `database is locked` en 8 días salen de
`marcar_leido`, y el que espera abre con `timeout=30`. El agente que lo reportó veía
esperas de ~30 s seguidas de «Internal Server Error», con el cursor SIN avanzar y el
reintento inmediato funcionando en 5-10 ms.

Y el daño real no es el 500: es que quien encadena el POST a un parser recibe un
JSONDecodeError que NO menciona el 500. El fallo se lee como bug propio y el cursor se
queda atrás en silencio.
"""
from __future__ import annotations

import os
import sqlite3
import time

from fastapi.testclient import TestClient

from .conftest import construir

H = {"X-Llminbox-Token": "test-token"}


def test_con_la_base_ocupada_responde_503_con_json(tmp_path, monkeypatch):
    """FALSADOR: con el lock de escritura sostenido, la respuesta tiene que ser un 503
    CON CUERPO JSON y en un tiempo acotado — no un 500 opaco a los 30 s.

    Se comprueban las tres cosas porque son tres defectos distintos: el código, que el
    cuerpo se pueda parsear, y que no bloquee un hilo del pool medio minuto.
    """
    s = construir(tmp_path, monkeypatch)
    with TestClient(s.app) as c:
        bloqueante = sqlite3.connect(os.environ["LLMINBOX_DB"], timeout=30)
        bloqueante.execute("BEGIN EXCLUSIVE")
        t0 = time.monotonic()
        r = c.post("/inbox/backend/leido", json={"hasta": {"demo-ledger": 1}}, headers=H)
        tardo = time.monotonic() - t0
        bloqueante.rollback(); bloqueante.close()
    assert r.status_code == 503, f"con la base ocupada devolvió {r.status_code}, no 503"
    cuerpo = r.json()                      # si no es JSON, esto revienta: es el punto
    assert "reintenta" in str(cuerpo).lower(), f"el cuerpo no dice qué hacer: {cuerpo}"
    assert tardo < 20, f"tardó {tardo:.1f}s: sigue bloqueando un hilo del pool"


def test_sin_bloqueo_marca_leido_con_normalidad(tmp_path, monkeypatch):
    """⊖ CONTROL — sin él, devolver 503 SIEMPRE pasaría el test de arriba y habríamos
    roto a los 16 agentes para arreglar un intermitente.
    """
    s = construir(tmp_path, monkeypatch)
    with TestClient(s.app) as c:
        # c3ca365 ata el ack a lo que el servidor CONCEDIO: sin indexar, `hasta:1` cae
        # FUERA del ledger y da 409 ACK_BEYOND_LEDGER. El sujeto de este test no es el
        # contrato (eso lo miden test_ack_grants / test_cursor_avisa_si_se_pasa), asi que
        # se INDEXA antes y el POST vuelve a ser legitimo. sdet 2026-09-11, adjudicado @qa.
        s.barrido()
        r = c.post("/inbox/backend/leido", json={"hasta": {"demo-ledger": 1}}, headers=H)
    assert r.status_code == 200, f"sin bloqueo devolvió {r.status_code}"
    assert r.json()["aplicados"]["demo-ledger"]["ahora"] == 1


def test_un_error_que_no_es_lock_no_se_disfraza_de_reintenta(tmp_path, monkeypatch):
    """⊖ DEL ⊖ — el que faltaba, y lo destapó el mutante.

    Quitar la condición «locked/busy» y tragarse CUALQUIER error como «reintenta»
    pasaba la suite entera: 317 verdes. O sea que mis dos tests comprobaban que el
    lock se maneja bien, y ninguno que un fallo REAL no se disfrace de intermitente.

    Un esquema roto, un disco lleno o un bug de SQL contestando «reintenta» es peor
    que el 500 que vinimos a quitar: el 500 se investiga, y un «reintenta» se
    reintenta para siempre. Es la misma frontera de toda la semana — una respuesta
    que oculta su causa.
    """
    s = construir(tmp_path, monkeypatch)
    real = s.db

    class Envuelta:
        """`sqlite3.Connection.execute` es de SÓLO LECTURA y no se puede parchear —
        me lo enseñó el AttributeError. Se envuelve la conexión en lugar de mutarla."""
        def __init__(self, con): self._con = con
        def __getattr__(self, n): return getattr(self._con, n)
        def execute(self, sql, *args, **kw):
            if "INSERT OR REPLACE INTO cursors" in sql:
                raise sqlite3.OperationalError("no such table: cursors")
            return self._con.execute(sql, *args, **kw)

    def rota(*a, **k):
        return Envuelta(real(*a, **k))

    monkeypatch.setattr(s, "db", rota)
    with TestClient(s.app, raise_server_exceptions=False) as c:
        # ver la nota de arriba: se INDEXA antes para que el POST llegue a la escritura;
        # sin esto, el 409 del contrato salta ANTES y el error envuelto nunca ocurre.
        s.barrido()
        r = c.post("/inbox/backend/leido", json={"hasta": {"demo-ledger": 1}}, headers=H)
    assert r.status_code != 503, (
        "un error que NO es de lock salió como «reintenta»: se está ocultando la causa")
    assert r.status_code >= 500
