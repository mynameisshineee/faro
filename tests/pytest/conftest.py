"""Arnés de pytest para el spec identidad-fail-closed (①②③⑤⑦a).

Decisión de arnés: `pytest` + `fastapi.testclient.TestClient` contra `servicio.app`
importado directamente (sin Docker), con SQLite temporal por test — ver §5.1 del
spec. `servicio.py`/`ledger_parse.py` calculan sus globals (TOKEN, LEDGERS, CANON,
CARRIL_LEDGER…) AL IMPORTAR el módulo, así que cada test necesita las variables de
entorno puestas ANTES del import y un import FRESCO (no cacheado en sys.modules).
"""
from __future__ import annotations

import json
import sqlite3
import sys

import pytest
from fastapi.testclient import TestClient

ROSTER = {
    "agentes": [
        {"nombre": "backend", "humano": "operador", "clave": "", "rol": "be"},
        {"nombre": "backend-biklabs", "humano": "operador", "clave": "", "rol": "be"},
        {"nombre": "cto-A", "humano": "operador", "clave": "", "rol": "cto"},
    ],
    "humanos": [{"nombre": "operador", "alias": ["Operador"]}],
    "difusion": ["equipo"],
}


def construir(tmp_path, monkeypatch, extra_env=None, roster=None):
    """Arma un roster + dos ledgers + un carriles.tsv de prueba, y devuelve el
    módulo `servicio` importado FRESCO contra ese entorno.

    Dos ledgers a propósito: `demo-ledger` (mapeado al carril `demo` en el
    carriles.tsv de prueba) y `otro-ledger` (en LLMINBOX_LEDGERS pero SIN mapear a
    ningún carril) — es la pareja mínima que necesitan los tests de ③ y ⑤.

    `roster` (⑰): opcional, default `None` = el `ROSTER` fijo de siempre (cero
    impacto en los 12 ficheros de test existentes). Los tests de censo-en-escritura
    necesitan dar de alta agentes/difusión propios (p.ej. `security`, `TODOS`) sin
    tocar el roster compartido del resto de la suite.
    """
    (tmp_path / "roster.json").write_text(json.dumps(roster if roster is not None else ROSTER))

    demo_md = tmp_path / "DEMO-LEDGER.md"
    demo_md.write_text(
        "### [cto-A → backend · REQUEST] primera\ncuerpo uno\n"
        "### [cto-A → backend · REQUEST] segunda\ncuerpo dos\n"
    )
    otro_md = tmp_path / "OTRO-LEDGER.md"
    otro_md.write_text(
        "### [cto-A → backend · REQUEST] otra\ncuerpo otro\n"
    )

    mounts = {"demo-ledger": str(demo_md), "otro-ledger": str(otro_md)}
    (tmp_path / "mounts.json").write_text(json.dumps(mounts))

    carriles = tmp_path / "carriles.tsv"
    carriles.write_text(
        "carril\tledger_path\twiki_path\testado\tnotas\n"
        f"demo\t{demo_md}\t-\tcompleto\t-\n"
    )

    env = {
        "LLMINBOX_DB": str(tmp_path / "llminbox.sqlite"),
        "LLMINBOX_TOKEN": "test-token",
        "LLMINBOX_ROSTER": str(tmp_path / "roster.json"),
        "LLMINBOX_LEDGERS": f"demo-ledger={demo_md},otro-ledger={otro_md}",
        "LLMINBOX_MOUNTS_JSON": str(tmp_path / "mounts.json"),
        "LLMINBOX_CARRILES": str(carriles),
    }
    if extra_env:
        env.update(extra_env)
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    for mod in ("servicio", "ledger_parse"):
        sys.modules.pop(mod, None)
    import servicio as s

    # El vigilante de fondo (barrido periódico, `asyncio.create_task` en
    # `lifespan`) corre en un hilo aparte y compite en el tiempo con las
    # aserciones del test. Se sustituye por un no-op: el indexado en los tests
    # que lo necesitan se dispara a mano y de forma síncrona con `servicio.barrido()`
    # (ver fixture `cliente`), así el estado que se comprueba es determinista.
    async def _vigilante_noop():
        return

    monkeypatch.setattr(s, "vigilante", _vigilante_noop)
    return s


@pytest.fixture
def servicio(tmp_path, monkeypatch):
    return construir(tmp_path, monkeypatch)


@pytest.fixture
def cliente(servicio):
    with TestClient(servicio.app) as c:
        servicio.barrido()          # indexado síncrono, sin esperar al vigilante
        c.headers.update({"X-Llminbox-Token": "test-token"})
        yield c


def db_directa(servicio_mod):
    """Conexión sqlite3 cruda a la BD de prueba, para verificar el EFECTO en la
    tabla —no sólo lo que dice la respuesta JSON, que podría mentir (ver ①)."""
    import os
    con = sqlite3.connect(os.environ["LLMINBOX_DB"])
    con.row_factory = sqlite3.Row
    return con


def sembrar_schema_y_meta(servicio_mod):
    """Crea el esquema COMPLETO de producción (via `servicio.SCHEMA`) y dos claves
    de `meta` que declaran la base "ya al día" en esquema y censo.

    Por qué hace falta esto y no basta con crear sólo `cursors`+`meta` a mano
    (lo que un primer intento de este arnés hacía, calcado del spec): con `meta`
    vacía, `indice_ilegible()` prueba `SELECT eid, body FROM entries LIMIT 1` y
    `entries` no existe todavía — y **"no such table: entries" está literalmente
    en la lista `CORRUPCION`** (servicio.py) —, así que el arranque se desvía a
    `reconstruir_indice()` (reconstrucción por corrupción) ANTES de llegar
    siquiera al bloque de ESQUEMA/CENSO donde vive la migración. Esa ruta rescata
    cursores por su cuenta con su propia lógica de anclas — un mecanismo real,
    pero DISTINTO del que este spec pide medir. Sembrar el esquema completo y las
    dos huellas de `meta` evita esa desviación y dos veces más: también evita el
    "ESQUEMA cambiado" (que haría DROP TABLE cursors antes de que la migración la
    vea) porque `meta['schema_v']` ya coincide con `huella_esquema()`.
    """
    import os
    con = sqlite3.connect(os.environ["LLMINBOX_DB"])
    con.executescript(servicio_mod.SCHEMA)
    con.execute("INSERT OR REPLACE INTO meta VALUES ('schema_v', ?)",
                (servicio_mod.huella_esquema(),))
    con.execute("INSERT OR REPLACE INTO meta VALUES ('roster_v', ?)",
                (servicio_mod.huella_censo(),))
    con.commit()
    return con
