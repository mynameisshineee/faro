from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import kind_registry as kr
import ledger_parse as lp
import message_policy as mp
import servicio


AGENT_OS = {
    "DECISION_REQUEST", "ESCALATION", "CONSULT", "HUMAN_INPUT_REQUEST",
    "CRITICAL_ALERT",
}
LEGACY = {
    "PRODUCED", "INGESTED", "FYI", "REQUEST", "ACK", "HELD", "AMEND", "DELTA",
    "MEASURED", "RESP", "FINDING", "RULING", "DELIVERED",
}
DEPRECATED = "[ACK, INGESTED, FYI_rutina, HEARTBEAT, STATUS, DELTA]"


def _roster(tmp_path: Path, *, role: str = "be") -> Path:
    path = tmp_path / "roster.json"
    path.write_text(json.dumps({
        "agentes": [{"nombre": "backend", "rol": role},
                    {"nombre": "qa", "rol": "qa"}],
        "difusion": ["FLOTA", "equipo"],
    }), encoding="utf-8")
    return path


def _contract(tmp_path: Path, *, role: str = "be", broadcast: bool = False,
              allowed=AGENT_OS, name: str | None = None) -> Path:
    path = tmp_path / (name or f"{role}.yaml")
    path.write_text(
        f"id: {role}\nauthority:\n  decide: [local]\ncommunication:\n"
        f"  broadcast: {str(broadcast).lower()}\n"
        f"  permitido: [{', '.join(sorted(allowed))}]\n"
        f"  deprecado: {DEPRECATED}\ndefault_model: opus\n",
        encoding="utf-8")
    return path


def _semantic_db(tmp_path: Path):
    con = sqlite3.connect(tmp_path / "index.sqlite")
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE entries (
          ledger TEXT NOT NULL, eid TEXT NOT NULL, tipo TEXT, raw_tipo TEXT,
          canonical_kind TEXT, kind_registry_rev INTEGER,
          PRIMARY KEY (ledger, eid));
        CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
    """)
    return con


def test_legacy_canon_and_version_are_frozen_and_agent_os_is_separate():
    assert lp.CANON_TIPOS == LEGACY
    assert servicio.CANON_V == "1"
    assert kr.CURRENT_REV == 1
    assert kr.current_kinds() == AGENT_OS
    for kind in AGENT_OS:
        assert lp.canonical_tipo(kind) is None
        assert kr.materialize(kind) == (kind, 1)


def test_registry_materialization_survives_legacy_canon_and_preserves_bytes(tmp_path):
    ledger = tmp_path / "lane.md"
    ledger.write_text(
        "### [backend → qa · CONSULT] 2026-09-07T10:00:00Z — pregunta\ntexto\n",
        encoding="utf-8")
    before = hashlib.sha256(ledger.read_bytes()).hexdigest()
    entry = lp.parse(str(ledger))[0][0]
    con = _semantic_db(tmp_path)
    # Estado exacto que dejó el commit corregido: CONSULT metido por error en `tipo`
    # y el sello legacy adelantado a 2. El correctivo debe poder volver a v1 sin
    # perder la interpretación Agent OS que ahora vive en su pareja propia.
    # reindex persiste Entrada.sha como eid; seq es sólo la posición del parser.
    con.execute("INSERT INTO entries VALUES(?,?,?,?,?,?)",
                (str(ledger), entry.sha, "CONSULT", "CONSULT", None, None))
    con.execute("INSERT INTO meta VALUES('canon_v','2')")
    con.commit()

    servicio.materializar_kinds_agent_os(con)
    row = con.execute("SELECT * FROM entries").fetchone()
    assert (row["tipo"], row["raw_tipo"]) == ("CONSULT", "CONSULT")
    assert (row["canonical_kind"], row["kind_registry_rev"]) == ("CONSULT", 1)

    # Rollback completo del canon equivocado: v1 no conoce CONSULT y sólo gobierna tipo.
    servicio.migrar_canon(con)
    after = con.execute("SELECT * FROM entries").fetchone()
    assert (after["tipo"], after["raw_tipo"], after["canonical_kind"],
            after["kind_registry_rev"]) == (None, "CONSULT", "CONSULT", 1)
    assert after["eid"] == entry.sha
    assert con.execute("SELECT v FROM meta WHERE k='canon_v'").fetchone()["v"] == "1"
    assert hashlib.sha256(ledger.read_bytes()).hexdigest() == before


def test_materializer_never_rewrites_a_populated_pair(tmp_path):
    con = _semantic_db(tmp_path)
    con.execute("INSERT INTO entries VALUES(?,?,?,?,?,?)",
                ("lane", "eid", None, "CONSULT", "CONSULT", 1))
    con.commit()
    servicio.materializar_kinds_agent_os(con)
    assert tuple(con.execute(
        "SELECT canonical_kind,kind_registry_rev FROM entries").fetchone()) == ("CONSULT", 1)


def test_materializer_rolls_back_an_incoherent_pair_without_sealing(tmp_path):
    con = _semantic_db(tmp_path)
    con.execute("INSERT INTO entries VALUES(?,?,?,?,?,?)",
                ("lane", "eid", None, "CONSULT", "CRITICAL_ALERT", 1))
    con.commit()
    try:
        servicio.materializar_kinds_agent_os(con)
    except RuntimeError as exc:
        assert "no acreditables" in str(exc)
    else:
        raise AssertionError("una pareja corrupta no aborto el arranque")
    assert con.execute(
        "SELECT COUNT(*) FROM meta WHERE k='kind_materializer_rev'").fetchone()[0] == 0
    assert tuple(con.execute(
        "SELECT canonical_kind,kind_registry_rev FROM entries").fetchone()) == (
            "CRITICAL_ALERT", 1)


def test_policy_accepts_registered_kind_and_denies_legacy(tmp_path):
    roster, contract = _roster(tmp_path), _contract(tmp_path)
    assert mp.decide("backend", "CONSULT", ["qa"], roster_path=roster,
                     contract_path=contract).allowed
    for kind in LEGACY:
        decision = mp.decide("backend", kind, ["qa"], roster_path=roster,
                             contract_path=contract)
        assert not decision.allowed, (kind, decision)
        assert decision.code == "MESSAGE_KIND_UNKNOWN"


def test_broadcast_false_denies_real_diffusion_and_true_allows_it(tmp_path):
    roster = _roster(tmp_path)
    deny = _contract(tmp_path, broadcast=False, name="deny.yaml")
    decision = mp.decide("backend", "CRITICAL_ALERT", ["FLOTA"],
                         roster_path=roster, contract_path=deny)
    assert decision.code == "MESSAGE_BROADCAST_DENIED"

    allow = _contract(tmp_path, broadcast=True, name="allow.yaml")
    assert mp.decide("backend", "CRITICAL_ALERT", ["equipo"],
                     roster_path=roster, contract_path=allow).allowed
    assert mp.decide("backend", "CONSULT", ["qa"], roster_path=roster,
                     contract_path=deny).allowed


def test_unresolved_recipient_fails_closed(tmp_path):
    decision = mp.decide("backend", "CONSULT", ["fantasma"],
                         roster_path=_roster(tmp_path), contract_path=_contract(tmp_path))
    assert decision.code == "MESSAGE_POLICY_UNAVAILABLE"


def test_parser_rejects_duplicate_communication_and_nested_permitido(tmp_path):
    duplicate = tmp_path / "duplicate.yaml"
    block = ("communication:\n  broadcast: false\n  permitido: [CONSULT]\n"
             f"  deprecado: {DEPRECATED}\n")
    duplicate.write_text("id: be\n" + block + block, encoding="utf-8")
    try:
        mp.allowed_for_role("be", duplicate)
    except mp.PolicyError as exc:
        assert "communication duplicado" in str(exc)
    else:
        raise AssertionError("acepto dos bloques communication")

    nested = tmp_path / "nested.yaml"
    nested.write_text(
        "id: be\ncommunication:\n  broadcast: false\n  policy:\n"
        "    permitido: [CONSULT]\n"
        f"  deprecado: {DEPRECATED}\n", encoding="utf-8")
    try:
        mp.allowed_for_role("be", nested)
    except mp.PolicyError as exc:
        assert "policy" in str(exc) or "profundidad" in str(exc)
    else:
        raise AssertionError("acepto permitido anidado")


def test_contract_cannot_expand_registry_or_duplicate_a_field(tmp_path):
    expanded = _contract(tmp_path, allowed=AGENT_OS | {"ACK"}, name="expanded.yaml")
    try:
        mp.allowed_for_role("be", expanded)
    except mp.PolicyError as exc:
        assert "fuera del contrato" in str(exc)
    else:
        raise AssertionError("el contrato local amplio el registro adjudicado")

    duplicate = _contract(tmp_path, name="duplicate-field.yaml")
    text = duplicate.read_text(encoding="utf-8").replace(
        "  broadcast: false\n", "  broadcast: false\n  broadcast: false\n")
    duplicate.write_text(text, encoding="utf-8")
    try:
        mp.allowed_for_role("be", duplicate)
    except mp.PolicyError as exc:
        assert "broadcast duplicado" in str(exc)
    else:
        raise AssertionError("acepto broadcast duplicado")


def test_environment_modes_are_backward_compatible_and_explicit(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("LLMINBOX_MESSAGE_POLICY", raising=False)
    assert mp.check_from_env("nobody", "ACK", ["nobody"]) == 0

    monkeypatch.setenv("LLMINBOX_MESSAGE_POLICY", "advisory")
    monkeypatch.setenv("LLMINBOX_ROSTER", str(_roster(tmp_path)))
    monkeypatch.setenv("LLMINBOX_ROLE_CONTRACT", str(_contract(tmp_path)))
    assert mp.check_from_env("backend", "ACK", ["qa"]) == 0
    assert "MESSAGE_POLICY_ADVISORY" in capsys.readouterr().err

    monkeypatch.setenv("LLMINBOX_MESSAGE_POLICY", "enforce")
    assert mp.check_from_env("backend", "ACK", ["qa"]) == 1
    assert mp.check_from_env("backend", "CONSULT", ["qa"]) == 0

    monkeypatch.setenv("LLMINBOX_MESSAGE_POLICY", "typo")
    assert mp.check_from_env("backend", "CONSULT", ["qa"]) == 2
