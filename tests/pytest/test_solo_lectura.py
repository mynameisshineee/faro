"""Un índice que no se deja escribir DEGRADA, no mata el arranque.

Hallazgo del arnés de humo de @qa (run 31481815502, 2026-08-11): con el índice
dañado, el servicio se curaba —«base nueva en su sitio · 1 cursores rescatados»—
y moría a continuación escribiendo `meta('parser_v')`:

    sqlite3.OperationalError: attempt to write a readonly database
    ERROR: Application startup failed. Exiting.

⇒ contenedor `exited`, la flota sin bandeja, por no poder escribir un CONTADOR
DE VERSIÓN. La promesa del producto es la contraria: el markdown es el canon y
esto es un atajo que no puede dejar a nadie esperando.
"""
from __future__ import annotations

import os
import sqlite3
import stat

import pytest
from fastapi.testclient import TestClient

from .conftest import construir


def _solo_lectura(ruta_db: str):
    """Deja la BD y su directorio sin permiso de escritura para el usuario.

    El directorio TAMBIÉN: SQLite necesita crear `-wal`/`-journal` al lado, así
    que sin esto la base sigue siendo escribible por la puerta de al lado y el
    test mediría otra cosa.
    """
    d = os.path.dirname(ruta_db)
    os.chmod(ruta_db, stat.S_IRUSR)
    os.chmod(d, stat.S_IRUSR | stat.S_IXUSR)
    return d


def _restaurar(d: str, ruta_db: str):
    os.chmod(d, stat.S_IRWXU)
    os.chmod(ruta_db, stat.S_IRUSR | stat.S_IWUSR)


@pytest.fixture
def servicio_ro(tmp_path, monkeypatch):
    """Un índice YA construido y poblado, que después se vuelve de sólo lectura."""
    s = construir(tmp_path, monkeypatch)
    with TestClient(s.app) as c:          # arranque normal: crea esquema e indexa
        s.barrido()
        c.headers.update({"X-Llminbox-Token": "test-token"})
        assert c.get("/inbox/backend").status_code == 200
    ruta = os.environ["LLMINBOX_DB"]
    d = _solo_lectura(ruta)
    yield s
    _restaurar(d, ruta)


def test_arranca_con_indice_no_escribible(servicio_ro):
    """EL FALSADOR DEL BUG, tal cual lo vio el humo: el arranque no puede morir.

    Sin la cura, este `with` levanta OperationalError («attempt to write a
    readonly database») desde el lifespan y el test se pone rojo — que es lo que
    en producción se ve como `Application startup failed. Exiting`.
    """
    with TestClient(servicio_ro.app) as c:
        c.headers.update({"X-Llminbox-Token": "test-token"})
        assert c.get("/health").status_code == 200


def test_sigue_sirviendo_las_bandejas(servicio_ro):
    """Y sirve para lo que existe: leer. Arrancar y no servir sería la misma
    indisponibilidad con mejor cara."""
    with TestClient(servicio_ro.app) as c:
        c.headers.update({"X-Llminbox-Token": "test-token"})
        r = c.get("/inbox/backend")
        assert r.status_code == 200
        assert "primera" in r.text, "arrancó pero la bandeja vino vacía"


def test_health_no_da_verde_y_nombra_el_motivo(servicio_ro):
    """Vivo ≠ sano. Quien lea `ok:true` daría por drenado lo que no se drenó.

    FALSADOR: si `ok` siguiera saliendo `true` con el índice en sólo lectura,
    el healthcheck del contenedor diría verde sobre un servicio que no avanza
    cursores — el «verde impecable sobre un servicio ciego» que este repo ya
    documenta como su clase de fallo favorita.
    """
    with TestClient(servicio_ro.app) as c:
        c.headers.update({"X-Llminbox-Token": "test-token"})
        d = c.get("/health").json()
        assert d["ok"] is False
        assert d["solo_lectura"], "no dice el motivo: un rojo mudo no se puede depurar"
        assert "readonly" in d["solo_lectura"].lower() or "read-only" in d["solo_lectura"].lower()
        assert "cursores NO" in d["aviso"]


def test_indice_escribible_no_toca_nada(cliente):
    """Control positivo: en el camino normal la cura NO se enciende sola.

    ⚠️ Aquí NO se puede asertar `ok is True`, y la primera versión de este test lo
    hacía y fallaba por su culpa, no por el producto: `sano` cuelga de que el
    VIGILANTE haya completado un barrido, y el arnés lo sustituye por un no-op
    (`conftest.construir`) para que el indexado sea síncrono y determinista. Lo
    que este control puede afirmar de verdad es que el modo degradado no se
    activa por su cuenta — que es la mitad que la cura podría romper.
    """
    d = cliente.get("/health").json()
    assert d["solo_lectura"] is None
    # ANTES DECÍA `d["aviso"] is None`, y eso medía MÁS de lo que el comentario afirmaba:
    # `aviso` es `avisos[0]` —el primero de CUALQUIER clase—, no «el de sólo lectura». La
    # aserción pasaba por casualidad mientras no hubiera otros avisos, y se rompió el día
    # que se añadió uno legítimo y ajeno (el del watcher-token). Un test que dice medir una
    # cosa y mide «que no haya ninguna» es un falso positivo esperando su turno.
    assert not any("sólo lectura" in a.lower() or "solo lectura" in a.lower()
                   for a in (d.get("avisos") or [])), (
        "el modo degradado se encendió solo en el camino normal")
    assert d["indexador"]["error"] is None


@pytest.mark.parametrize("case,expected", [
    ("clean", "clean"),
    ("pending", "pending_materialization"),
    ("corrupt", "corrupt"),
])
def test_lifespan_ro_audita_semantica_sin_matar_inbox(
        tmp_path, monkeypatch, case, expected):
    s = construir(tmp_path, monkeypatch)
    with TestClient(s.app) as c:
        s.barrido()
    ruta = os.environ["LLMINBOX_DB"]
    con = sqlite3.connect(ruta)
    if case == "clean":
        con.execute("UPDATE entries SET raw_tipo='CONSULT',tipo=NULL,"
                    "canonical_kind='CONSULT',kind_registry_rev=1 "
                    "WHERE rowid=(SELECT MIN(rowid) FROM entries)")
    elif case == "pending":
        con.execute("UPDATE entries SET raw_tipo='CONSULT',tipo=NULL,"
                    "canonical_kind=NULL,kind_registry_rev=NULL "
                    "WHERE rowid=(SELECT MIN(rowid) FROM entries)")
    else:
        con.execute("UPDATE entries SET raw_tipo='CONSULT',tipo=NULL,"
                    "canonical_kind='CRITICAL_ALERT',kind_registry_rev=1 "
                    "WHERE rowid=(SELECT MIN(rowid) FROM entries)")
    con.commit(); con.close()
    directory = _solo_lectura(ruta)
    try:
        with TestClient(s.app) as c:
            c.headers.update({"X-Llminbox-Token": "test-token"})
            health = c.get("/health").json()
            assert health["message_kinds"]["state"] == expected
            assert c.get("/inbox/backend").status_code == 200
            rows = c.get("/entries?raw_tipo=CONSULT").json()
            assert rows
            if case == "clean":
                assert rows[0]["canonical_kind"] == "CONSULT"
                assert rows[0]["kind_materialization_status"] == "materialized"
            elif case == "pending":
                assert rows[0]["canonical_kind"] is None
                assert rows[0]["kind_materialization_status"] == "pending_materialization"
            else:
                assert rows[0]["canonical_kind"] is None
                assert rows[0]["kind_registry_rev"] is None
                assert rows[0]["kind_materialization_status"] == "untrusted"
    finally:
        _restaurar(directory, ruta)
