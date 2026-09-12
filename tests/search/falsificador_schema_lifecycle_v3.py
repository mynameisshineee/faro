#!/usr/bin/env python3
"""Falsadores de composición lifecycle + schema v3.

No se recoge en la suite aislada de esta rama: el sujeto deliberadamente NO copia la
rama de schema. Al ejecutarlo directamente, la precondición roja exige que el árbol ya
contenga la clasificación durable y la migración tipada. No hay ``skip`` ni ``xfail``.

    python3 tests/search/falsificador_schema_lifecycle_v3.py
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import observability as O
import search_cursor as SC
import search_store as S
from tests.pytest.conftest import construir


if (getattr(S, "SEARCH_SCHEMA_V", 0) < 2
        or not hasattr(S.SearchStore, "migrate_schema_v1_to_v2")
        or not hasattr(S.SearchStore, "_huellas_v1_legacy")
        or not hasattr(S, "guard_search_connection")
        or not hasattr(S, "SearchSchemaCorrupt")):
    raise RuntimeError(
        "PRECONDICIÓN NO CUMPLIDA: integrar schema v3 tipado antes de acreditar "
        "lifecycle; este rojo no se puede convertir en skip/xfail")


def _credenciales(servicio):
    servicio.CREDENCIALES = {
        "cred-http": {"rol": "be", "carril": "demo", "principal_id": "p-http"}}


@pytest.mark.parametrize("valor", [sqlite3.Binary(b"2"), "no-es-entero"])
def test_schema_malformado_fisico_es_503_corrupt_y_no_muta_bytes(
        tmp_path, monkeypatch, valor):
    servicio = construir(
        tmp_path, monkeypatch,
        extra_env={"LLMINBOX_SEARCH_CURSOR_KEY": "k" * 32})
    _credenciales(servicio)
    with TestClient(servicio.app):
        servicio.barrido()
        con = servicio.db()
        try:
            servicio.preparar_busqueda_publica(con, reconstruir=True)
        finally:
            con.close()
    ruta = os.environ["LLMINBOX_DB"]
    con = sqlite3.connect(ruta)
    con.execute("UPDATE search_state SET v=? WHERE k='schema_v'", (valor,))
    con.commit()
    con.close()
    antes = hashlib.sha256(open(ruta, "rb").read()).digest()

    exp = O.MemoryExporter()
    servicio.configura_observabilidad(exporter=exp, enabled=True)
    with TestClient(servicio.app) as cliente:
        respuesta = cliente.get(
            "/search?q=cuerpo", headers={"X-Llminbox-Token": "cred-http"})
    assert respuesta.status_code == 503
    assert respuesta.json() == {"detail": "índice de búsqueda no listo"}
    assert hashlib.sha256(open(ruta, "rb").read()).digest() == antes
    logs = [s for s in exp.logs() if s.nombre == "repair.required"]
    assert len(logs) == 1
    assert logs[0].attributes == {
        "component": "search", "state": "corrupt", "action": "search_rebuild"}


def test_v1_stale_ordena_migrar_y_converge_migrate_rebuild_200(
        tmp_path, monkeypatch, capsys):
    servicio = construir(
        tmp_path, monkeypatch,
        extra_env={"LLMINBOX_SEARCH_CURSOR_KEY": "k" * 32})
    _credenciales(servicio)
    with TestClient(servicio.app):
        servicio.barrido()
        con = servicio.db()
        try:
            servicio.preparar_busqueda_publica(con, reconstruir=True)
        finally:
            con.close()

    con = sqlite3.connect(os.environ["LLMINBOX_DB"])
    con.row_factory = sqlite3.Row
    S.SearchStore.register_udf(con)
    st = S.SearchStore(con, cursor_key=b"k" * 32,
                       acl={"demo": {"demo-ledger"}})
    con.execute("DROP TRIGGER search_au_key")
    con.execute(S.SQL_TRIGGER_AU_KEY_V1.strip().rstrip(";"))
    # Schema v5 usa identidad inyectiva (schema,type,name,tbl_name). La migración
    # compara primero ese inventario completo y sólo después proyecta al sello legacy
    # publicado por v1; reproducir aquí un dict[name,sql] ocultaría homónimos.
    huellas_v1 = st._huellas_v1_legacy(st.ddl_esperado_v1())
    con.execute("UPDATE search_state SET v='1' WHERE k='schema_v'")
    con.execute("UPDATE search_state SET v=? WHERE k='huellas'",
                (json.dumps(huellas_v1, sort_keys=True),))
    con.commit()
    con.close()

    exp = O.MemoryExporter()
    servicio.configura_observabilidad(exporter=exp, enabled=True)
    with TestClient(servicio.app) as cliente:
        antes = cliente.get(
            "/search?q=cuerpo", headers={"X-Llminbox-Token": "cred-http"})
    assert antes.status_code == 503
    assert antes.json() == {"detail": "índice de búsqueda no listo"}
    log = [s for s in exp.logs() if s.nombre == "repair.required"]
    assert len(log) == 1
    assert log[0].attributes == {
        "component": "search", "state": "stale",
        "action": "search_migrate_1_2"}

    capsys.readouterr()  # descarta diagnósticos de los dos lifespans, no el JSON admin
    assert servicio.main_administracion(["search", "migrate", "1", "2"]) == 0
    migrada = json.loads(capsys.readouterr().out)
    assert migrada["ok"] is True and migrada["operation"] == "search.migrate"
    assert migrada["from"] == 1 and migrada["to"] == 2
    assert servicio.main_administracion(["search", "rebuild"]) == 0
    reconstruida = json.loads(capsys.readouterr().out)
    assert reconstruida["ok"] is True and reconstruida["state"] == "ready"

    with TestClient(servicio.app) as cliente:
        despues = cliente.get(
            "/search?q=cuerpo", headers={"X-Llminbox-Token": "cred-http"})
    assert despues.status_code == 200
    assert despues.json()["filas"]


def test_writer_B_de_la_fabrica_real_guarda_TEMP_y_caduca_cursor_sin_perder_fila(
        tmp_path, monkeypatch):
    """Falsador físico del wiring, no una llamada directa al guard de schema.

    A y B salen de ``servicio.db``. B intenta instalar la misma clase de TEMP que
    permitió conservar estado/generación en el incidente; después hace una mutación
    durable real. A debe observar building, rechazar el cursor anterior y, tras rebuild,
    recorrer también la fila movida.
    """
    servicio = construir(
        tmp_path, monkeypatch,
        extra_env={"LLMINBOX_SEARCH_CURSOR_KEY": "k" * 32})
    with TestClient(servicio.app):
        servicio.barrido()
        inicial = servicio.db()
        try:
            servicio.preparar_busqueda_publica(inicial, reconstruir=True)
        finally:
            inicial.close()

    con_a = servicio.db()
    try:
        store_a = S.SearchStore(
            con_a, cursor_key=b"k" * 32, acl={"demo": {"demo-ledger"}})
        primera = store_a.search(
            lane="demo", ledger="demo-ledger", query="cuerpo", limit=1)
        assert primera["cursor"] is not None
        movida = con_a.execute(
            "SELECT eid FROM entries WHERE ledger='demo-ledger'"
            " ORDER BY arrival ASC LIMIT 1").fetchone()[0]

        con_b = servicio.db()
        try:
            with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                con_b.execute(
                    "CREATE TEMP TABLE SeArCh_StAtE(k TEXT PRIMARY KEY, v TEXT)")
            con_b.execute(
                "UPDATE entries SET arrival=99 WHERE ledger=? AND eid=?",
                ("demo-ledger", movida))
            con_b.commit()
        finally:
            con_b.close()

        with pytest.raises(S.SearchNotReady):
            store_a.search(
                lane="demo", ledger="demo-ledger", query="cuerpo", limit=20,
                cursor=primera["cursor"])
        store_a.rebuild()
        with pytest.raises(SC.CursorGenerationStale):
            store_a.search(
                lane="demo", ledger="demo-ledger", query="cuerpo", limit=20,
                cursor=primera["cursor"])

        vistos = []
        cursor = None
        while True:
            pagina = store_a.search(
                lane="demo", ledger="demo-ledger", query="cuerpo", limit=1,
                cursor=cursor)
            vistos.extend(f["eid"] for f in pagina["filas"])
            cursor = pagina["cursor"]
            if cursor is None:
                break
        assert movida in vistos
        assert len(vistos) == len(set(vistos)) == 2
    finally:
        con_a.close()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
