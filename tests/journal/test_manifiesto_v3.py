"""Falsadores del sello v3: READY exige la anatomía durable completa."""
from __future__ import annotations

import hashlib
import os
import sqlite3

import pytest

import coordination as C
from ._arnes import GRAMATICA, LANES, PEPPER, censo, journal


def _huella(ruta: str) -> dict[str, tuple[int, str]]:
    out = {}
    for suf in ("", "-wal", "-shm"):
        pieza = ruta + suf
        if os.path.exists(pieza):
            with open(pieza, "rb") as fh:
                contenido = fh.read()
            out[suf or "db"] = (len(contenido), hashlib.sha256(contenido).hexdigest())
    return out


def _base_v3(tmp_path) -> str:
    j = journal(tmp_path)
    j.dispose()
    ruta = str(tmp_path / "coordination.sqlite")
    con = sqlite3.connect(ruta)
    try:
        assert con.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    finally:
        con.close()
    assert set(_huella(ruta)) == {"db"}
    return ruta


def _debe_fallar_cerrado_sin_tocar(ruta: str) -> None:
    antes = _huella(ruta)
    j = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
    with pytest.raises(C.SchemaIndeterminate):
        j.initialize()
    assert j.health()["writable"] is False
    assert j.health()["estado_ciclo"] != C.Journal.READY
    with pytest.raises(C.JournalError):
        j.bind_credential("intrusa", principal="x", role="be", lane="llminbox")
    j.dispose()
    assert _huella(ruta) == antes


@pytest.mark.parametrize(("tabla", "columna"), [
    ("events", "head"),
    ("outbox", "last_error"),
    ("receipts", "updated_at"),
    ("receipt_transitions", "detail"),
    ("idempotency", "req_hash_v"),
    ("leases", "released_at"),
    ("credential_bindings", "capabilities"),
    ("runtime_sessions", "annotations"),
    ("commands", "payload"),
])
def test_v3_no_admite_columnas_operacionales_mutiladas(
        tmp_path, tabla, columna):
    ruta = _base_v3(tmp_path)
    con = sqlite3.connect(ruta)
    try:
        con.execute(f"ALTER TABLE {tabla} DROP COLUMN {columna}")
        con.commit()
    finally:
        con.close()

    _debe_fallar_cerrado_sin_tocar(ruta)


@pytest.mark.parametrize("tabla", [
    "event_causes", "command_causes", "command_event_causes", "external_causes",
])
def test_v3_no_admite_una_tabla_de_causalidad_ausente(tmp_path, tabla):
    ruta = _base_v3(tmp_path)
    con = sqlite3.connect(ruta)
    try:
        con.execute("PRAGMA foreign_keys=OFF")
        con.execute(f"DROP TABLE {tabla}")
        con.commit()
    finally:
        con.close()

    _debe_fallar_cerrado_sin_tocar(ruta)


@pytest.mark.parametrize("indice", [
    "u_binding_activa", "i_outbox_ready", "i_cmd_ws",
])
def test_v3_no_admite_indices_criticos_ausentes(tmp_path, indice):
    ruta = _base_v3(tmp_path)
    con = sqlite3.connect(ruta)
    try:
        con.execute(f"DROP INDEX {indice}")
        con.commit()
    finally:
        con.close()

    _debe_fallar_cerrado_sin_tocar(ruta)


def _reescribe_ddl(ruta: str, objeto: str, viejo: str, nuevo: str) -> None:
    """Mutila sólo el fixture; obliga a SQLite a releer la forma al reabrir."""
    con = sqlite3.connect(ruta)
    try:
        ddl = con.execute(
            "SELECT sql FROM sqlite_master WHERE name=?", (objeto,)).fetchone()[0]
        assert viejo in ddl, (objeto, ddl)
        con.execute("PRAGMA writable_schema=ON")
        con.execute("UPDATE sqlite_master SET sql=? WHERE name=?",
                    (ddl.replace(viejo, nuevo), objeto))
        version = con.execute("PRAGMA schema_version").fetchone()[0]
        con.execute(f"PRAGMA schema_version={version + 1}")
        con.execute("PRAGMA writable_schema=OFF")
        con.commit()
    finally:
        con.close()


def test_v3_no_admite_indice_parcial_convertido_en_total(tmp_path):
    ruta = _base_v3(tmp_path)
    _reescribe_ddl(ruta, "u_binding_activa", " WHERE retired_at IS NULL", "")
    _debe_fallar_cerrado_sin_tocar(ruta)


@pytest.mark.parametrize(("tabla", "viejo", "nuevo"), [
    ("events", "head         TEXT NOT NULL", "head         BLOB NOT NULL"),
    ("leases", "expires_at   REAL NOT NULL", "expires_at   REAL"),
])
def test_v3_no_admite_tipo_o_nulabilidad_de_columna_alterados(
        tmp_path, tabla, viejo, nuevo):
    ruta = _base_v3(tmp_path)
    _reescribe_ddl(ruta, tabla, viejo, nuevo)
    _debe_fallar_cerrado_sin_tocar(ruta)


def test_v3_no_admite_check_de_autoridad_eliminado(tmp_path):
    ruta = _base_v3(tmp_path)
    _reescribe_ddl(ruta, "external_causes",
                   " CHECK (authority = 'false')", "")
    _debe_fallar_cerrado_sin_tocar(ruta)


def test_v3_no_admite_fk_de_outbox_eliminada(tmp_path):
    ruta = _base_v3(tmp_path)
    _reescribe_ddl(ruta, "outbox",
                   " UNIQUE REFERENCES events(event_id)", " UNIQUE")
    _debe_fallar_cerrado_sin_tocar(ruta)


@pytest.mark.parametrize(("tabla", "viejo", "nuevo"), [
    ("outbox", "DEFAULT 'pending'", "DEFAULT 'failed'"),
    ("outbox", "attempts    INTEGER NOT NULL DEFAULT 0",
               "attempts    INTEGER NOT NULL DEFAULT 7"),
    # INSERT OR IGNORE omite authority: sin este default acuñaría NULL o
    # descartaría la causa, según la forma mutilada. No es una preferencia.
    ("external_causes", " DEFAULT 'false'", ""),
])
def test_v3_no_admite_defaults_que_cambian_el_efecto_implicito(
        tmp_path, tabla, viejo, nuevo):
    ruta = _base_v3(tmp_path)
    _reescribe_ddl(ruta, tabla, viejo, nuevo)
    _debe_fallar_cerrado_sin_tocar(ruta)


def test_manifiesto_enumera_todos_los_defaults_del_schema_v3():
    con = sqlite3.connect(":memory:")
    try:
        con.executescript(C.SCHEMA)
        encontrados = {}
        for tabla in C._OBJETOS_V3:
            for columna in con.execute(f"PRAGMA table_info({tabla})"):
                if columna[4] is not None:
                    encontrados[(tabla, columna[1])] = C._normalizar_default(columna[4])
    finally:
        con.close()

    assert encontrados == C._DEFAULTS_V3
