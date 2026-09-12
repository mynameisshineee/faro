#!/usr/bin/env python3
"""Guarda local y optativa para la politica de comunicacion de un rol.

No es una frontera de seguridad: en bridge el actor se autodeclara y en nativo el
servidor vuelve a derivar la identidad. Esta pieza evita errores del cliente honesto;
no concede autoridad ni pretende sustituir un 403 server-side.

El parser acepta deliberadamente sólo el bloque generado exacto: ``id`` y un único
``communication`` con ``broadcast``, ``permitido`` y ``deprecado`` a dos espacios.
Un contrato ambiguo se rechaza en ``enforce`` en vez de interpretar YAML general
sin una dependencia runtime.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import kind_registry as kr
import ledger_parse as lp


MODES = frozenset({"off", "advisory", "enforce"})
_IDENT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_KIND = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_POLICY_TOKEN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class Decision:
    allowed: bool
    code: str
    detail: str
    role: str | None = None


@dataclass(frozen=True)
class ContractPolicy:
    role: str
    broadcast: bool
    allowed: frozenset[str]
    deprecated: frozenset[str]


def _roster_path() -> Path:
    return Path(os.environ.get("LLMINBOX_ROSTER", Path(__file__).with_name("roster.json")))


def _load_roster(roster_path: Path | None = None) -> tuple[Path, dict]:
    path = roster_path or _roster_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyError(f"roster ilegible: {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("agentes", []), list):
        raise PolicyError(f"roster malformado: {path}")
    return path, data


def role_for_actor(actor: str, roster_path: Path | None = None,
                   *, roster: dict | None = None) -> str:
    path, data = (_load_roster(roster_path) if roster is None
                  else (roster_path or _roster_path(), roster))
    matches = [row for row in data.get("agentes", [])
               if str(row.get("nombre", "")).casefold() == actor.casefold()]
    if len(matches) != 1:
        raise PolicyError(
            f"actor {actor!r}: esperada una fila exacta en el roster, encontradas {len(matches)}")
    role = str(matches[0].get("rol") or "").strip().lower()
    if not _IDENT.fullmatch(role):
        raise PolicyError(f"actor {actor!r}: rol ausente o invalido en el roster")
    return role


def canonical_bridge_actor(actor: str, lane: str, *, roster: dict) -> str:
    """Replica la derivacion que firma ``publicar.py``; el teclado no manda."""
    known = {str(row.get("nombre", "")).casefold(): str(row.get("nombre", ""))
             for row in roster.get("agentes", []) if row.get("nombre")}
    candidate = lp.canonico(f"{actor}-{lane}") if lane else ""
    if candidate.casefold() in known:
        return known[candidate.casefold()]
    exact = known.get(lp.canonico(actor).casefold())
    if exact is None:
        raise PolicyError(f"actor bridge {actor!r} no resuelve en el roster")
    return exact


def recipients_from_roster(raw_recipients: list[str] | tuple[str, ...], roster: dict,
                           path: Path) -> tuple[list[str], list[str]]:
    """Devuelve (destinos canonicos, grupos de difusion), sin adivinar nombres."""
    names: dict[str, str] = {}
    for row in roster.get("agentes", []):
        name = str(row.get("nombre", "")).strip()
        if name:
            names[name.casefold()] = name
    for row in roster.get("humanos", []):
        name = str(row.get("nombre", "")).strip()
        if name:
            names[name.casefold()] = name
        for alias in row.get("alias", []):
            alias = str(alias).strip()
            if alias:
                names[alias.casefold()] = alias
    groups: dict[str, str] = {}
    for value in roster.get("difusion", []):
        value = str(value).strip()
        if value:
            groups[value.casefold()] = value
    names.update(groups)

    canonical, broadcast = [], []
    for raw in raw_recipients:
        value = str(raw).strip()
        if not value:
            continue
        resolved = names.get(value.casefold())
        if resolved is None:
            raise PolicyError(f"destinatario {value!r} no resuelve en el roster {path}")
        canonical.append(resolved)
        if value.casefold() in groups:
            broadcast.append(resolved)
    if not canonical:
        raise PolicyError("la politica requiere al menos un destinatario resuelto")
    return canonical, broadcast


def _contract_path(role: str) -> Path:
    exact = os.environ.get("LLMINBOX_ROLE_CONTRACT", "").strip()
    if exact:
        return Path(exact)
    root = os.environ.get("LLMINBOX_ROLE_CONTRACTS", "").strip()
    if not root:
        raise PolicyError(
            "falta LLMINBOX_ROLE_CONTRACTS (o LLMINBOX_ROLE_CONTRACT) para la politica activa")
    # `role` ya paso una allow-list sintactica: no puede escapar del directorio.
    return Path(root) / f"{role}.yaml"


def _inline_tokens(path: Path, number: int, key: str, value: str,
                   pattern: re.Pattern[str]) -> frozenset[str]:
    if not (value.startswith("[") and value.endswith("]")):
        raise PolicyError(f"{path}:{number}: {key} debe ser una lista inline cerrada")
    tokens = [item.strip() for item in value[1:-1].split(",") if item.strip()]
    if not tokens or any(not pattern.fullmatch(item) for item in tokens):
        raise PolicyError(f"{path}:{number}: {key} vacio o con tokens invalidos")
    if len(tokens) != len(set(tokens)):
        raise PolicyError(f"{path}:{number}: {key} contiene duplicados")
    return frozenset(tokens)


def allowed_for_role(role: str, contract_path: Path | None = None) -> ContractPolicy:
    path = contract_path or _contract_path(role)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise PolicyError(f"role-contract ilegible: {path}: {exc}") from exc

    contract_id: str | None = None
    in_communication = False
    communication_seen = False
    values: dict[str, object] = {}
    for number, raw in enumerate(lines, 1):
        if "\t" in raw:
            raise PolicyError(f"{path}:{number}: tabs no admitidos")
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if indent == 0 and stripped.startswith("id:"):
            if not stripped.startswith("id: "):
                raise PolicyError(f"{path}:{number}: forma de id no admitida")
            if contract_id is not None:
                raise PolicyError(f"{path}:{number}: id duplicado")
            contract_id = stripped.partition(":")[2].strip().lower()
        if stripped == "communication:":
            if indent != 0:
                raise PolicyError(f"{path}:{number}: communication debe estar en profundidad 0")
            if communication_seen:
                raise PolicyError(f"{path}:{number}: communication duplicado")
            communication_seen = True
            in_communication = True
            continue
        if in_communication:
            if indent == 0:
                in_communication = False
            else:
                if indent != 2 or ":" not in stripped:
                    raise PolicyError(
                        f"{path}:{number}: profundidad ambigua dentro de communication")
                key, _, value = stripped.partition(":")
                if key not in {"broadcast", "permitido", "deprecado"}:
                    raise PolicyError(f"{path}:{number}: clave communication.{key} no admitida")
                if key in values:
                    raise PolicyError(f"{path}:{number}: communication.{key} duplicado")
                value = value.strip()
                if key == "broadcast":
                    if value not in {"true", "false"}:
                        raise PolicyError(f"{path}:{number}: broadcast debe ser true o false")
                    values[key] = value == "true"
                elif key == "permitido":
                    values[key] = _inline_tokens(path, number, key, value, _KIND)
                else:
                    values[key] = _inline_tokens(path, number, key, value, _POLICY_TOKEN)
                continue

    if contract_id != role:
        raise PolicyError(f"role-contract id={contract_id!r}; el roster resolvio role={role!r}")
    missing = {"broadcast", "permitido", "deprecado"} - set(values)
    if missing:
        raise PolicyError(f"{path}: faltan campos communication: {', '.join(sorted(missing))}")
    allowed = values["permitido"]
    assert isinstance(allowed, frozenset)
    unknown = sorted(allowed - kr.current_kinds())
    if unknown:
        raise PolicyError(
            f"{path}: communication.permitido contiene tipos fuera del contrato: {', '.join(unknown)}")
    deprecated = values["deprecado"]
    assert isinstance(deprecated, frozenset)
    return ContractPolicy(role, bool(values["broadcast"]), allowed, deprecated)


def decide(actor: str, raw_kind: str, recipients: list[str] | tuple[str, ...] = (), *,
           roster_path: Path | None = None, contract_path: Path | None = None,
           trusted_role: str | None = None, lane: str = "") -> Decision:
    canonical, _registry_rev = kr.materialize(raw_kind)
    if canonical is None:
        return Decision(False, "MESSAGE_KIND_UNKNOWN",
                        f"tipo {raw_kind!r} no pertenece al registro Agent OS")
    try:
        path, roster = _load_roster(roster_path)
        if trusted_role is None:
            actor = canonical_bridge_actor(actor, lane, roster=roster)
            role = role_for_actor(actor, path, roster=roster)
        else:
            if (not isinstance(actor, str) or not actor or len(actor) > 256
                    or any(ord(ch) < 32 for ch in actor)
                    or not _IDENT.fullmatch(trusted_role)):
                raise PolicyError("principal/role nativos no tienen forma canonica")
            role = trusted_role
        _resolved, groups = recipients_from_roster(recipients, roster, path)
        policy = allowed_for_role(role, contract_path)
    except PolicyError as exc:
        return Decision(False, "MESSAGE_POLICY_UNAVAILABLE", str(exc))
    if canonical not in policy.allowed:
        return Decision(False, "MESSAGE_KIND_DENIED",
                        f"{canonical} no esta en communication.permitido para {role}", role)
    if groups and not policy.broadcast:
        return Decision(False, "MESSAGE_BROADCAST_DENIED",
                        f"{role} tiene communication.broadcast=false; grupos: "
                        + ", ".join(groups), role)
    return Decision(True, "MESSAGE_KIND_ALLOWED",
                    f"{canonical} permitido para {role}", role)


def check_from_env(actor: str, raw_kind: str, recipients: list[str] | tuple[str, ...],
                   *, trusted_role: str | None = None, lane: str = "") -> int:
    mode = os.environ.get("LLMINBOX_MESSAGE_POLICY", "off").strip().lower()
    if mode not in MODES:
        print(f"· MESSAGE_POLICY_CONFIG_INVALID · modo {mode!r}; validos: "
              + " · ".join(sorted(MODES)), file=sys.stderr)
        return 2
    if mode == "off":
        return 0
    decision = decide(actor, raw_kind, recipients, trusted_role=trusted_role, lane=lane)
    if decision.allowed:
        return 0
    prefix = "MESSAGE_POLICY_ADVISORY" if mode == "advisory" else decision.code
    print(f"· {prefix} · {decision.detail}", file=sys.stderr)
    print("    guarda CLIENTE: no acredita enforcement del servidor.", file=sys.stderr)
    return 0 if mode == "advisory" else 1


def main(argv: list[str]) -> int:
    if len(argv) == 6 and argv[1] == "bridge":
        return check_from_env(argv[2], argv[4], argv[5].split(","), lane=argv[3])
    if len(argv) == 7 and argv[1] == "native":
        return check_from_env(argv[2], argv[5], argv[6].split(","),
                              trusted_role=argv[3], lane=argv[4])
    print("uso: message_policy.py bridge <actor> <lane> <TIPO> <dest[,dest2]> | "
          "native <principal> <role> <lane> <TIPO> <dest[,dest2]>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
