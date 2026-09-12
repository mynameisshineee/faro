"""Registro versionado de actos Agent OS, separado del ``tipo`` legacy.

Las revisiones son inmutables y acumulativas: un lexema reconocido por primera vez
en una revisión conserva para siempre esa pareja ``(canonical_kind, rev)``. Una
revisión futura puede añadir lexemas, nunca cambiar el significado de uno anterior.
Así una reconstrucción puede reproducir la materialización por fila sin interpretar
el pasado con la taxonomía del día.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3


_TOKEN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

# La adjudicación es el allow-list común de los role-contracts Agent OS v0.2.
# Para cambiar un significado, acuña OTRO canonical_kind; no edites una revisión.
REGISTRIES: dict[int, dict[str, str]] = {
    1: {
        "DECISION_REQUEST": "DECISION_REQUEST",
        "ESCALATION": "ESCALATION",
        "CONSULT": "CONSULT",
        "HUMAN_INPUT_REQUEST": "HUMAN_INPUT_REQUEST",
        "CRITICAL_ALERT": "CRITICAL_ALERT",
    },
}
CURRENT_REV = max(REGISTRIES)

# Huella adjudicada, FUERA del objeto mutable. Cambiar o retirar una pareja de una
# revision publicada no se arregla recalculando el sello: el proceso se niega a
# arrancar. Una revision nueva necesita su entrada nueva y deja intactas las previas.
REVISION_DIGESTS: dict[int, str] = {
    1: "9317f397ac86645c0780bb9fdc00b31c8fd6998d5c77d006f5bad780a830c125",
}


def _digest(value: object) -> str:
    wire = json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")
    return hashlib.sha256(wire).hexdigest()


def _validate() -> None:
    if sorted(REGISTRIES) != list(range(1, CURRENT_REV + 1)):
        raise RuntimeError("kind registry: revisiones no contiguas")
    if set(REVISION_DIGESTS) != set(REGISTRIES):
        raise RuntimeError("kind registry: falta manifiesto inmutable de una revision")
    seen: dict[str, tuple[str, int]] = {}
    for rev, entries in REGISTRIES.items():
        if _digest(entries) != REVISION_DIGESTS[rev]:
            raise RuntimeError(
                f"kind registry r{rev}: contenido distinto del manifiesto inmutable")
        for raw, canonical in entries.items():
            if not _TOKEN.fullmatch(raw) or not _TOKEN.fullmatch(canonical):
                raise RuntimeError(f"kind registry r{rev}: token invalido")
            if raw in seen:
                raise RuntimeError(
                    f"kind registry: {raw} ya fue adjudicado en r{seen[raw][1]}; "
                    "las revisiones son append-only")
            seen[raw] = (canonical, rev)


_validate()


def registry_digest(rev: int | None = None) -> str:
    """Huella acumulada hasta ``rev``; una ampliacion reproduce sellos previos."""
    _validate()
    limit = CURRENT_REV if rev is None else rev
    if type(limit) is not int or limit not in REGISTRIES:
        raise RuntimeError(f"kind registry: revision {limit!r} no manifestada")
    return _digest({str(item): REGISTRIES[item]
                    for item in sorted(REGISTRIES) if item <= limit})


def materialize(raw: str | None) -> tuple[str | None, int | None]:
    """Primera adjudicación de ``raw``; estable aunque aparezcan revisiones nuevas."""
    if not raw:
        return None, None
    token = raw.strip().upper()
    for rev in sorted(REGISTRIES):
        canonical = REGISTRIES[rev].get(token)
        if canonical is not None:
            return canonical, rev
    return None, None


def materialize_at(raw: str | None, rev: int) -> str | None:
    """Interpreta con UNA revisión exacta; nunca cae a la revisión corriente."""
    if not raw or rev not in REGISTRIES:
        return None
    return REGISTRIES[rev].get(raw.strip().upper())


def current_kinds() -> frozenset[str]:
    return frozenset(canonical for entries in REGISTRIES.values()
                     for canonical in entries.values())


def entry_projection_sql(con) -> tuple[dict[str, str], frozenset[str]]:
    """SELECT aditivo ajustado a la forma física real de ``entries``.

    Los fragmentos son constantes internas, nunca incluyen entrada del usuario.
    Separarlo del endpoint permite falsar la compatibilidad RO sin importar FastAPI.
    """
    columns = frozenset(row[1] for row in con.execute("PRAGMA table_info(entries)"))
    projection = {
        name: (f"e.{name}" if name in columns else f"NULL AS {name}")
        for name in ("raw_tipo", "canonical_kind", "kind_registry_rev")
    }
    return projection, columns


def _sealed_revision(value: object) -> int:
    """Parsea el TEXT durable sin aceptar coerciones de SQLite/Python."""
    if type(value) is not str or not re.fullmatch(r"[1-9][0-9]*", value):
        raise ValueError("revision durable no es un entero decimal canonico")
    return int(value)


def _row_revision(value: object) -> int:
    """Una revisión por fila tiene que ser INTEGER SQLite, no sólo convertible."""
    # sqlite3 entrega INTEGER como ``int`` y REAL como ``float``. ``int(1.5)``
    # truncaba y acreditaba una pareja que los bytes durables nunca declararon.
    if type(value) is not int:
        raise ValueError("kind_registry_rev no es INTEGER SQLite")
    return value


def audit_materialization(con) -> dict[str, object]:
    """Auditoria PURA para un indice que puede estar abierto ``mode=ro``.

    Nunca crea tabla, sella ni rellena una fila. El consumidor puede seguir usando
    el indice legacy, pero sólo presenta la semántica Agent OS si este veredicto la
    acredita.
    """
    base: dict[str, object] = {
        "state": "unavailable", "reason": None,
        # Los nombres antiguos siguen significando runtime. Los campos explícitos
        # impiden confundirlo con lo que está realmente sellado en el índice.
        "registry_rev": CURRENT_REV, "registry_digest": None,
        "runtime_registry_rev": CURRENT_REV, "runtime_registry_digest": None,
        "sealed_registry_rev": None, "sealed_registry_digest": None,
        "pending_materialization": 0,
    }
    try:
        digest = registry_digest()
        base["registry_digest"] = digest
        base["runtime_registry_digest"] = digest
        tables = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('meta','entries')")}
        if tables != {"meta", "entries"}:
            base["reason"] = "SEMANTIC_SCHEMA_UNAVAILABLE"
            return base
        columns = {row[1] for row in con.execute("PRAGMA table_info(entries)")}
        required = {"ledger", "eid", "raw_tipo", "canonical_kind", "kind_registry_rev"}
        if not required <= columns:
            base["reason"] = "SEMANTIC_COLUMNS_UNAVAILABLE"
            return base
        seals = dict(con.execute(
            "SELECT k,v FROM meta WHERE k IN ('kind_materializer_rev',"
            "'kind_materializer_digest')"))
        if set(seals) != {"kind_materializer_rev", "kind_materializer_digest"}:
            base.update(state="unsealed", reason="SEMANTIC_SEAL_INCOMPLETE")
            return base
        sealed_digest = seals["kind_materializer_digest"]
        if type(sealed_digest) is str:
            base["sealed_registry_digest"] = sealed_digest
        try:
            sealed_rev = _sealed_revision(seals["kind_materializer_rev"])
        except (TypeError, ValueError):
            base.update(state="corrupt", reason="SEMANTIC_REV_INVALID")
            return base
        base["sealed_registry_rev"] = sealed_rev
        if (sealed_rev not in REGISTRIES
                or type(sealed_digest) is not str
                or sealed_digest != registry_digest(sealed_rev)):
            base.update(state="corrupt", reason="SEMANTIC_DIGEST_MISMATCH")
            return base
        pending = 0
        for row in con.execute(
                "SELECT raw_tipo,canonical_kind,kind_registry_rev FROM entries"):
            canonical, rev = row["canonical_kind"], row["kind_registry_rev"]
            if (canonical is None) != (rev is None):
                base.update(state="corrupt", reason="SEMANTIC_PAIR_INCOMPLETE")
                return base
            if canonical is not None:
                try:
                    row_rev = _row_revision(rev)
                except (TypeError, ValueError):
                    base.update(state="corrupt", reason="SEMANTIC_ROW_REV_INVALID")
                    return base
                if row_rev > sealed_rev or materialize_at(row["raw_tipo"], row_rev) != canonical:
                    base.update(state="corrupt", reason="SEMANTIC_PAIR_MISMATCH")
                    return base
            elif materialize(row["raw_tipo"])[0] is not None:
                pending += 1
        base["pending_materialization"] = pending
        if sealed_rev < CURRENT_REV:
            base.update(state="stale_registry",
                        reason="SEMANTIC_SEAL_BEHIND_RUNTIME")
        else:
            base["state"] = "pending_materialization" if pending else "clean"
            base["reason"] = "SEMANTIC_ROWS_PENDING" if pending else None
        return base
    except (RuntimeError, sqlite3.Error, KeyError, TypeError):
        # No cruza texto de SQLite ni ids del ledger al health sin autenticacion.
        base.update(state="corrupt", reason="SEMANTIC_AUDIT_FAILED")
        return base


def semantic_view(raw: str | None, canonical: str | None, rev: int | None,
                  audit: dict[str, object]) -> tuple[str | None, int | None, str]:
    """Proyección fail-closed de una fila; nunca presenta como fiable un par dudoso."""
    if audit.get("state") not in {"clean", "pending_materialization"}:
        return None, None, "untrusted"
    if canonical is not None:
        return canonical, rev, "materialized"
    if materialize(raw)[0] is not None:
        return None, None, "pending_materialization"
    return None, None, "not_registered"


def audit_and_materialize(con) -> int:
    """Audita todo el corpus y rellena sólo parejas ``NULL/NULL``; no hace commit."""
    digest = registry_digest()
    seal = con.execute(
        "SELECT v FROM meta WHERE k='kind_materializer_rev'").fetchone()
    digest_seal = con.execute(
        "SELECT v FROM meta WHERE k='kind_materializer_digest'").fetchone()
    if seal:
        try:
            sealed_rev = _sealed_revision(seal["v"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError("sello kind_materializer_rev malformado") from exc
        if sealed_rev > CURRENT_REV:
            raise RuntimeError(
                f"sello kind registry futuro r{sealed_rev}; runtime r{CURRENT_REV}")
        if not digest_seal:
            raise RuntimeError("revision kind registry sin digest durable")
        if digest_seal["v"] != registry_digest(sealed_rev):
            raise RuntimeError("digest durable del kind registry no coincide")
    elif digest_seal:
        raise RuntimeError("digest kind registry sin revision durable")

    changes: list[tuple[str, int, str, str]] = []
    for row in con.execute(
            "SELECT ledger,eid,raw_tipo,canonical_kind,kind_registry_rev FROM entries"):
        canonical, rev = row["canonical_kind"], row["kind_registry_rev"]
        if (canonical is None) != (rev is None):
            raise RuntimeError(
                f"{row['ledger']}:{row['eid']}: canonical_kind/rev incompletos")
        if canonical is not None:
            try:
                row_rev = _row_revision(rev)
                expected = materialize_at(row["raw_tipo"], row_rev)
            except (TypeError, ValueError):
                expected = None
            if expected != canonical:
                raise RuntimeError(
                    f"{row['ledger']}:{row['eid']}: materializacion r{rev} no reproduce")
            continue
        candidate, candidate_rev = materialize(row["raw_tipo"])
        if candidate is not None and candidate_rev is not None:
            changes.append((candidate, candidate_rev, row["ledger"], row["eid"]))
    if changes:
        con.executemany(
            "UPDATE entries SET canonical_kind=?,kind_registry_rev=? "
            "WHERE ledger=? AND eid=? AND canonical_kind IS NULL "
            "AND kind_registry_rev IS NULL", changes)
    con.execute("INSERT OR REPLACE INTO meta VALUES ('kind_materializer_rev', ?)",
                (str(CURRENT_REV),))
    con.execute("INSERT OR REPLACE INTO meta VALUES ('kind_materializer_digest', ?)",
                (digest,))
    return len(changes)
