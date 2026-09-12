#!/usr/bin/env python3
"""Falsador stdlib del adaptador v0.9 físico montado read-only."""
from __future__ import annotations

import ast
import copy
import json
import sqlite3
import sys
import tempfile
import types
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import kind_registry as kr

SOURCE = (ROOT / "servicio.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
FUNCTIONS = {node.name: node for node in TREE.body if isinstance(node, ast.FunctionDef)}


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail


class Response:
    def __init__(self):
        self.headers = {}


def handler(name: str, namespace: dict):
    """Ejecuta el cuerpo real del handler sin cargar dependencias web opcionales."""
    node = copy.deepcopy(FUNCTIONS[name])
    node.decorator_list = []
    module = ast.Module(body=[ast.ImportFrom(
        module="__future__", names=[ast.alias(name="annotations")], level=0), node],
        type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(ROOT / "servicio.py"), "exec"), namespace)
    return namespace[name]


def create_legacy(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript("""
      CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
      CREATE TABLE entries(
        ledger TEXT NOT NULL, eid TEXT NOT NULL, arrival INTEGER NOT NULL,
        seq INTEGER, ts TEXT, actor TEXT, tipo TEXT, line_no INTEGER,
        head TEXT, body TEXT, ausente INTEGER,
        PRIMARY KEY(ledger,eid));
      CREATE TABLE recipients(
        ledger TEXT NOT NULL, eid TEXT NOT NULL, who TEXT NOT NULL,
        PRIMARY KEY(ledger,eid,who));
      CREATE TABLE cursors(
        agent TEXT NOT NULL, ledger TEXT NOT NULL, last_arrival INTEGER,
        PRIMARY KEY(agent,ledger));
      INSERT INTO entries VALUES(
        'legacy','eid-legacy',1,1,'2026-09-07T00:00:00+00:00',
        'backend','FYI',7,'legacy head','legacy body',NULL);
      INSERT INTO recipients VALUES('legacy','eid-legacy','qa');
    """)
    con.commit()
    con.close()


with tempfile.TemporaryDirectory() as raw_tmp:
    directory = Path(raw_tmp)
    path = directory / "legacy.sqlite"
    create_legacy(path)
    path.chmod(0o444)
    directory.chmod(0o555)
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
        con.row_factory = sqlite3.Row

        audit = kr.audit_materialization(con)
        assert audit["state"] == "unavailable"
        assert audit["reason"] == "SEMANTIC_COLUMNS_UNAVAILABLE"

        projection, columns = kr.entry_projection_sql(con)
        assert "raw_tipo" not in columns
        assert projection == {
            "raw_tipo": "NULL AS raw_tipo",
            "canonical_kind": "NULL AS canonical_kind",
            "kind_registry_rev": "NULL AS kind_registry_rev",
        }
        sql = (
            "SELECT e.ledger,e.eid,e.tipo,"
            f"{projection['raw_tipo']},{projection['canonical_kind']},"
            f"{projection['kind_registry_rev']} FROM entries e")
        row = dict(con.execute(sql).fetchone())
        canonical, rev, status = kr.semantic_view(
            row["raw_tipo"], row["canonical_kind"], row["kind_registry_rev"], audit)
        assert (row["tipo"], canonical, rev, status) == (
            "FYI", None, None, "untrusted")

        # La misma consulta legacy que sostiene /inbox funciona en este fichero y
        # directorio RO; no depende de ninguna columna v1.0.
        inbox = con.execute(
            "SELECT e.arrival,e.eid,e.ts,e.actor,e.tipo,e.line_no,e.head "
            "FROM entries e WHERE e.ledger=? AND EXISTS ("
            "SELECT 1 FROM recipients r WHERE r.ledger=e.ledger AND r.eid=e.eid "
            "AND r.who=?) AND e.ausente IS NULL ORDER BY e.arrival ASC LIMIT ?",
            ("legacy", "qa", 30)).fetchall()
        assert len(inbox) == 1 and inbox[0]["head"] == "legacy head"
        con.close()

        def ro_db(*_args, **_kwargs):
            opened = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
            opened.row_factory = sqlite3.Row
            return opened

        fake_lp = types.SimpleNamespace(
            canonico=lambda value: value,
            canonical_tipo=lambda value: value if value == "FYI" else None,
            CANON_TIPOS={"FYI"},
            refrescar_organigrama=lambda: {
                "source_sha256": None, "loaded_sha256": None, "roles_alias": {}},
            escuchados=lambda value: [value], escuchados_autor=lambda _value: [],
            DIFUSION=[],
        )
        common = {
            "Query": lambda default, **_kwargs: default,
            "HTTPException": HTTPException,
            "Response": Response,
            "db": ro_db,
            "lp": fake_lp,
            "kr": kr,
        }
        entries = handler("entries", {
            **common, "CUERPO_MAX_FILAS": 10, "CUERPO_MAX_BYTES": 1024,
            "_corte_utc": lambda _value, _name: None,
            "KIND_SEMANTICS": audit, "LEDGER_CARRIL": {"legacy": "demo"},
        })
        response = Response()
        rows = entries(
            response, ledger="legacy", to=None, actor=None, tipo=None,
            raw_tipo=None, since=None, q=None, limit=50, cuerpo=False, orden="ts")
        assert len(rows) == 1
        assert rows[0]["canonical_kind"] is None
        assert rows[0]["kind_registry_rev"] is None
        assert rows[0]["kind_materialization_status"] == "untrusted"
        assert response.headers["X-Llminbox-Untrusted"]
        try:
            entries(
                Response(), ledger="legacy", to=None, actor=None, tipo=None,
                raw_tipo="FYI", since=None, q=None, limit=50,
                cuerpo=False, orden="ts")
        except HTTPException as exc:
            assert exc.status_code == 503
            assert exc.detail["error"] == "semantic_filter_unavailable"
        else:
            raise AssertionError("/entries intentó filtrar una columna inexistente")

        inbox_handler = handler("inbox", {
            **common, "TOPE_INBOX": 200, "LEDGERS": {"legacy": str(path)},
            "INBOX_EXCLUIR": set(), "resolver_o_422": lambda value: value,
            "anota_lectura": lambda *_args: None, "datetime": datetime,
            "timezone": timezone, "clave_cursor": lambda value: value,
            "LEDGER_CARRIL": {"legacy": "demo"},
            "actor_arroba_carril": lambda actor, _ledger: actor,
            "titular_visible": lambda head: head, "AVISO": "",
            "_aviso_alias_mudo": lambda _agent: "", "json": json,
        })
        rendered = inbox_handler("qa", limit=30, only="legacy")
        assert "eid-legacy" in rendered and "legacy head" in rendered
    finally:
        directory.chmod(0o755)
        path.chmod(0o644)

# Guarda de integración barata: el endpoint usa el helper probado y degrada el
# filtro ausente de forma tipada. También fija que el SELECT de inbox no incorpore
# por accidente las columnas semánticas.
entries_src = ast.get_source_segment(SOURCE, FUNCTIONS["entries"])
assert entries_src is not None
assert "kr.entry_projection_sql(con)" in entries_src
assert '"error": "semantic_filter_unavailable"' in entries_src
assert "NULL AS raw_tipo" not in entries_src  # una sola autoridad: el helper
health_src = ast.get_source_segment(SOURCE, FUNCTIONS["health"])
assert health_src is not None
assert '"message_kinds": dict(KIND_SEMANTICS)' in health_src
assert '{"corrupt", "stale_registry"}' in health_src
inbox_literals = " ".join(
    node.value for node in ast.walk(FUNCTIONS["inbox"])
    if isinstance(node, ast.Constant) and isinstance(node.value, str))
assert "canonical_kind" not in inbox_literals
assert "kind_registry_rev" not in inbox_literals

print("OK message-policy service legacy RO compatibility")
