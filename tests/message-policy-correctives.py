#!/usr/bin/env python3
"""Falsadores stdlib para el correctivo del contrato de mensajes."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "journal"))

import coordination as C
import kind_registry as kr
import ledger_parse as lp
import message_policy as mp
import _arnes as h


def db(path: str = ":memory:") -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript("""
      CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
      CREATE TABLE entries(
        ledger TEXT, eid TEXT, raw_tipo TEXT, tipo TEXT,
        canonical_kind TEXT, kind_registry_rev INTEGER,
        PRIMARY KEY(ledger,eid));
    """)
    return con


con = db()
con.execute("INSERT INTO entries VALUES('l','a','CONSULT',NULL,NULL,NULL)")
assert kr.audit_and_materialize(con) == 1
con.commit()
assert tuple(con.execute(
    "SELECT tipo,raw_tipo,canonical_kind,kind_registry_rev FROM entries"
).fetchone()) == (None, "CONSULT", "CONSULT", 1)
assert con.execute("SELECT v FROM meta WHERE k='kind_materializer_digest'").fetchone()[0] \
       == kr.registry_digest()

# v1 arranca, un lector v0.9 añade una fila NULL/NULL y v1 vuelve: el sello global
# coincide, pero la nueva fila se reexamina y materializa.
con.execute("INSERT INTO entries VALUES('l','b','ESCALATION',NULL,NULL,NULL)")
con.commit()
assert kr.audit_and_materialize(con) == 1
assert tuple(con.execute(
    "SELECT canonical_kind,kind_registry_rev FROM entries WHERE eid='b'"
).fetchone()) == ("ESCALATION", 1)
con.commit()

# Auditoría RO pura: limpio, nueva NULL/NULL post-sello y corrupción. Ninguna
# consulta muta; la proyección oculta toda pareja cuando el veredicto es corrupto.
ro = db()
ro.execute("INSERT INTO entries VALUES('l','clean','CONSULT',NULL,'CONSULT',1)")
ro.execute("INSERT INTO meta VALUES('kind_materializer_rev','1')")
ro.execute("INSERT INTO meta VALUES('kind_materializer_digest',?)",
           (kr.registry_digest(),))
ro.commit(); before = ro.total_changes
clean = kr.audit_materialization(ro)
assert clean["state"] == "clean" and ro.total_changes == before
ro.execute("INSERT INTO entries VALUES('l','late','ESCALATION',NULL,NULL,NULL)")
ro.commit(); before = ro.total_changes
pending = kr.audit_materialization(ro)
assert pending["state"] == "pending_materialization"
assert pending["pending_materialization"] == 1 and ro.total_changes == before
assert kr.semantic_view("ESCALATION", None, None, pending)[2] == "pending_materialization"
ro.execute("UPDATE meta SET v='0' WHERE k='kind_materializer_digest'")
ro.commit(); before = ro.total_changes
bad_digest = kr.audit_materialization(ro)
assert bad_digest["state"] == "corrupt"
assert bad_digest["reason"] == "SEMANTIC_DIGEST_MISMATCH" and ro.total_changes == before
ro.execute("UPDATE meta SET v=? WHERE k='kind_materializer_digest'",
           (kr.registry_digest(),))
ro.execute("UPDATE entries SET canonical_kind='CRITICAL_ALERT' WHERE eid='clean'")
ro.commit(); before = ro.total_changes
corrupt = kr.audit_materialization(ro)
assert corrupt["state"] == "corrupt" and ro.total_changes == before
assert kr.semantic_view("CONSULT", "CRITICAL_ALERT", 1, corrupt) == (
    None, None, "untrusted")

# SQLite acepta REAL en una columna de afinidad INTEGER. No se puede acreditar
# truncándolo con int(): los bytes dicen 1.5, no revisión 1.
exact = db()
exact.execute("INSERT INTO entries VALUES('l','real','CONSULT',NULL,'CONSULT',1.5)")
exact.execute("INSERT INTO meta VALUES('kind_materializer_rev','1')")
exact.execute("INSERT INTO meta VALUES('kind_materializer_digest',?)",
              (kr.registry_digest(1),))
exact.commit()
assert exact.execute(
    "SELECT typeof(kind_registry_rev) FROM entries").fetchone()[0] == "real"
real_rev = kr.audit_materialization(exact)
assert real_rev["state"] == "corrupt"
assert real_rev["reason"] == "SEMANTIC_ROW_REV_INVALID"
try:
    kr.audit_and_materialize(exact)
except RuntimeError:
    exact.rollback()
else:
    raise AssertionError("kind_registry_rev REAL fue truncado a INTEGER")
exact.close()

# Un runtime r2 delante de un índice legítimamente sellado en r1 no es clean:
# publica ambos lados del desfase y oculta la proyección hasta materializar/sellar.
kr.REGISTRIES[2] = {"ROUTINE_NOTE": "ROUTINE_NOTE"}
kr.REVISION_DIGESTS[2] = kr._digest(kr.REGISTRIES[2])
kr.CURRENT_REV = 2
try:
    stale = db()
    stale.execute("INSERT INTO entries VALUES('l','old','CONSULT',NULL,'CONSULT',1)")
    stale.execute("INSERT INTO meta VALUES('kind_materializer_rev','1')")
    stale.execute("INSERT INTO meta VALUES('kind_materializer_digest',?)",
                  (kr.registry_digest(1),))
    stale.commit()
    behind = kr.audit_materialization(stale)
    assert behind["state"] == "stale_registry"
    assert behind["reason"] == "SEMANTIC_SEAL_BEHIND_RUNTIME"
    assert behind["sealed_registry_rev"] == 1
    assert behind["runtime_registry_rev"] == 2
    assert behind["sealed_registry_digest"] == kr.registry_digest(1)
    assert behind["runtime_registry_digest"] == kr.registry_digest(2)
    assert kr.semantic_view("CONSULT", "CONSULT", 1, behind) == (
        None, None, "untrusted")
    stale.close()
finally:
    del kr.REGISTRIES[2]
    del kr.REVISION_DIGESTS[2]
    kr.CURRENT_REV = 1
    kr._validate()

# Mismo auditor sobre una conexión que físicamente no admite escrituras.
fd, ro_path = tempfile.mkstemp(); os.close(fd); os.unlink(ro_path)
disk = db(ro_path)
ro.backup(disk); disk.close(); ro.close()
disk_ro = sqlite3.connect(f"file:{ro_path}?mode=ro&immutable=1", uri=True)
disk_ro.row_factory = sqlite3.Row
assert kr.audit_materialization(disk_ro)["state"] == "corrupt"
disk_ro.close()
os.unlink(ro_path)

# Toda pareja poblada se revalida incluso con sello coincidente.
con.execute("UPDATE entries SET canonical_kind='CRITICAL_ALERT' WHERE eid='a'")
con.commit()
try:
    kr.audit_and_materialize(con)
except RuntimeError:
    con.rollback()
else:
    raise AssertionError("una pareja corrupta paso la auditoria")
con.execute("UPDATE entries SET canonical_kind='CONSULT' WHERE eid='a'")
con.execute("UPDATE meta SET v='0' WHERE k='kind_materializer_digest'")
con.commit()
try:
    kr.audit_and_materialize(con)
except RuntimeError:
    con.rollback()
else:
    raise AssertionError("un digest durable corrupto fue aceptado")

# La huella adjudicada detecta tanto mutacion como retirada bajo r1.
saved = dict(kr.REGISTRIES[1])
for mutate in (lambda: kr.REGISTRIES[1].pop("CONSULT"),
               lambda: kr.REGISTRIES[1].__setitem__("CONSULT", "ESCALATION")):
    kr.REGISTRIES[1].clear(); kr.REGISTRIES[1].update(saved); mutate()
    try:
        kr._validate()
    except RuntimeError:
        pass
    else:
        raise AssertionError("r1 mutable no aborto")
kr.REGISTRIES[1].clear(); kr.REGISTRIES[1].update(saved); kr._validate()

# El actor bridge de politica es el alias por carril, no YO tecleado.
tmp = Path(tempfile.mkdtemp())
roster = tmp / "roster.json"
roster.write_text(json.dumps({
    "agentes": [
        {"nombre": "backend", "rol": "be"},
        {"nombre": "backend-demo", "rol": "qa"},
        {"nombre": "qa", "rol": "qa"},
    ], "difusion": ["FLOTA"]}), encoding="utf-8")
contract = tmp / "qa.yaml"
contract.write_text("""id: qa
communication:
  broadcast: false
  permitido: [CONSULT]
  deprecado: [ACK]
""", encoding="utf-8")
decision = mp.decide("backend", "CONSULT", ["qa"], roster_path=roster,
                     contract_path=contract, lane="demo")
assert decision.allowed and decision.role == "qa"
assert not mp.decide("backend", "CONSULT", ["FLOTA"], roster_path=roster,
                     contract_path=contract, lane="demo").allowed

# Los cinco actos entran en el journal por la pareja explicita. El `kind` durable
# es su proyeccion consultable; el intent no contiene el slot legacy.
jpath = tmp / "journal.sqlite"
grammar = C.Grammar(
    canonical_kind=lp.canonical_tipo,
    opens_entry=lambda line: bool(lp.H_ENTRY.match(line)),
    normalize_resource=h._normaliza_recurso,
    canonical_agent_kind=kr.materialize_at,
)
journal = C.Journal(str(jpath), pepper=h.PEPPER,
                    lane_ledgers={"llminbox": ["llminbox"]},
                    grammar=grammar, recipient_resolver=h.censo)
assert journal.initialize() == C.DURABLE_V
h.abre_admision(journal, lanes=["llminbox"], verbs=["events.accept"])
journal.bind_credential("cred-policy", principal="backend-llminbox",
                        role="be", lane="llminbox")
token = journal.open_session("cred-policy").token
for n, kind in enumerate(sorted(kr.current_kinds())):
    accepted = journal.accept_event(
        token, idempotency_key=f"semantic-{n}", intent={
            "type": "message", "verb": "inform", "to": ["qa"],
            "canonical_kind": kind, "kind_registry_rev": 1,
            "head": "semantic", "body": "body",
        })
    row = journal._connect().execute(
        "SELECT kind,intent FROM events WHERE event_id=?", (accepted.event_id,)
    ).fetchone()
    intent = json.loads(row["intent"])
    assert row["kind"] == kind and "kind" not in intent
    assert (intent["canonical_kind"], intent["kind_registry_rev"]) == (kind, 1)

try:
    journal.accept_event(token, idempotency_key="semantic-bad", intent={
        "type": "message", "verb": "inform", "to": ["qa"],
        "canonical_kind": "CONSULT", "kind_registry_rev": 99,
        "head": "bad", "body": "bad",
    })
except C.GrammarRejected:
    pass
else:
    raise AssertionError("revision semantica falsa entro al journal")

docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
assert any("kind_registry.py" in line and line.lstrip().startswith("COPY ")
           for line in docker.splitlines()), "Dockerfile no copia kind_registry.py"
print("OK message-policy correctives")
