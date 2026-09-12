#!/usr/bin/env python3
"""Cheap structural and evidence gate for a 16-member llminbox fleet manifest."""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any


SCHEMA = "llminbox.fleet-manifest.v1"
EVIDENCE_SCHEMA = "llminbox.fleet-evidence.v1"
RECEIPT_SCHEMA = "llminbox.fleet-probe-receipt.v1"
EXPECTED_MEMBERS = 16
STAGES = ("sent", "indexed", "peeked", "consumed", "monitor_armed")
RESULTS = {"unmeasured", "pass", "fail"}
PACK_STATES = {"present_observed", "absent_observed", "unmeasured"}
RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path}: top-level value must be an object")
    return value


def norm_alias(value: str) -> str:
    return value.strip().casefold()


def parse_rfc3339(value: Any) -> datetime | None:
    if not isinstance(value, str) or not RFC3339.fullmatch(value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def is_uuid(value: Any) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value.lower()
    except (ValueError, AttributeError):
        return False


def validate_manifest(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if data.get("schema") != SCHEMA:
        errors.append(f"schema must be {SCHEMA!r}")
    if data.get("lane") != "llminbox":
        errors.append("lane must be 'llminbox'")
    if data.get("deployment_scope") != "biklabs_instance_not_oss_template":
        errors.append("deployment_scope must declare this as the BikLabs instance, not an OSS template")
    if data.get("expected_members") != EXPECTED_MEMBERS:
        errors.append(f"expected_members must be {EXPECTED_MEMBERS}")
    adoption = data.get("adoption_policy")
    expected_adoption = {
        "pack_required": True,
        "required_pack_state": "present_observed",
        "on_missing_pack": "block_adoption",
    }
    if adoption != expected_adoption:
        errors.append(f"adoption_policy must be exactly {expected_adoption}")

    contract = data.get("role_contract")
    members = data.get("members")
    if not isinstance(contract, dict):
        errors.append("role_contract must be an object")
        contract = {}
    if not isinstance(members, list):
        errors.append("members must be an array")
        return errors
    if len(members) != EXPECTED_MEMBERS:
        errors.append(f"fleet must contain exactly {EXPECTED_MEMBERS} members, got {len(members)}")

    sessions: dict[str, str] = {}
    slots: dict[int, str] = {}
    roles: dict[str, int] = {}
    alias_owner: dict[str, str] = {}

    for index, member in enumerate(members):
        where = f"members[{index}]"
        if not isinstance(member, dict):
            errors.append(f"{where} must be an object")
            continue
        role = member.get("role")
        if not isinstance(role, str) or not role:
            errors.append(f"{where}.role must be a non-empty string")
            continue
        if role not in contract:
            errors.append(f"{where}.role {role!r} is unknown to role_contract")
        roles[role] = roles.get(role, 0) + 1

        slot = member.get("slot")
        if not isinstance(slot, int):
            errors.append(f"{where}.slot must be an integer")
        elif slot in slots:
            errors.append(f"duplicate slot {slot}: {slots[slot]} and {role}")
        else:
            slots[slot] = role

        session = member.get("session")
        session_name = session.get("name") if isinstance(session, dict) else None
        if not isinstance(session_name, str) or not session_name:
            errors.append(f"{where}.session.name must be a non-empty string")
        elif session_name in sessions:
            errors.append(f"duplicate session {session_name!r}: {sessions[session_name]} and {role}")
        else:
            sessions[session_name] = role

        identity = member.get("identity")
        actor = identity.get("received_actor") if isinstance(identity, dict) else None
        cursor = identity.get("canonical_cursor") if isinstance(identity, dict) else None
        if not isinstance(actor, str) or not actor:
            errors.append(f"{where}.identity.received_actor must be a non-empty string")
        if cursor != role:
            errors.append(f"{where}.identity.canonical_cursor must equal canonical role {role!r}")

        boot = member.get("boot")
        handle = boot.get("handle") if isinstance(boot, dict) else None
        if not isinstance(handle, str) or not handle:
            errors.append(f"{where}.boot.handle must be a non-empty string")
        if not isinstance(boot, dict) or boot.get("hook") != "vigia-boot.sh":
            errors.append(f"{where}.boot.hook must be 'vigia-boot.sh'")

        aliases = member.get("aliases")
        if not isinstance(aliases, list) or any(not isinstance(a, str) or not a for a in aliases):
            errors.append(f"{where}.aliases must be an array of non-empty strings")
            aliases = []
        identity_names = [role, *aliases]
        for candidate in (actor, handle):
            if isinstance(candidate, str) and candidate:
                identity_names.append(candidate)
        for candidate in identity_names:
            alias = norm_alias(candidate)
            previous = alias_owner.get(alias)
            if previous is not None and previous != role:
                errors.append(f"ambiguous alias {candidate!r}: resolves to both {previous!r} and {role!r}")
            else:
                alias_owner[alias] = role

        lane_context = member.get("lane_context")
        pack = lane_context.get("pack") if isinstance(lane_context, dict) else None
        if not isinstance(pack, dict) or pack.get("state") not in PACK_STATES:
            errors.append(f"{where}.lane_context.pack.state must be one of {sorted(PACK_STATES)}")
        if not isinstance(pack, dict) or not isinstance(pack.get("path"), str) or not pack.get("path"):
            errors.append(f"{where}.lane_context.pack.path must be a non-empty string")

    duplicate_roles = sorted(role for role, count in roles.items() if count > 1)
    if duplicate_roles:
        errors.append(f"duplicate canonical roles: {', '.join(duplicate_roles)}")
    contract_roles = set(contract)
    member_roles = set(roles)
    missing = sorted(contract_roles - member_roles)
    extra = sorted(member_roles - contract_roles)
    if missing:
        errors.append(f"contract roles absent from fleet: {', '.join(missing)}")
    if extra:
        errors.append(f"fleet roles absent from contract: {', '.join(extra)}")
    if set(slots) != set(range(1, EXPECTED_MEMBERS + 1)):
        errors.append(f"slots must be exactly 1..{EXPECTED_MEMBERS}")
    return errors


def validate_pack_adoption(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    policy = data.get("adoption_policy", {})
    if not policy.get("pack_required"):
        return errors
    required_state = policy.get("required_pack_state")
    for member in data.get("members", []):
        state = member.get("lane_context", {}).get("pack", {}).get("state")
        if state != required_state:
            errors.append(
                f"{member.get('role')}: adoption blocked; pack state is {state!r}, "
                f"required {required_state!r}"
            )
    return errors


def validate_sources(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    sources = data.get("sources")
    if not isinstance(sources, dict):
        return ["sources must be an object"]
    for name in ("lane_map", "role_contract", "boot_gate", "ledger"):
        source = sources.get(name)
        if not isinstance(source, dict) or not isinstance(source.get("path"), str):
            errors.append(f"sources.{name}.path is required")
            continue
        path = Path(source["path"])
        if not path.is_file():
            errors.append(f"sources.{name}.path is not a file: {path}")
            continue
        expected = source.get("sha256")
        if expected is not None:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != expected:
                errors.append(f"sources.{name} sha256 drift: expected {expected}, got {actual}")

    lane_path = Path(sources.get("lane_map", {}).get("path", ""))
    ledger_path = sources.get("ledger", {}).get("path")
    if lane_path.is_file():
        lane_rows = []
        for raw in lane_path.read_text(encoding="utf-8").splitlines():
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue
            fields = raw.split("\t")
            if fields[0] == data.get("lane"):
                lane_rows.append(fields)
        if len(lane_rows) != 1:
            errors.append(f"lane map must contain exactly one {data.get('lane')!r} row, got {len(lane_rows)}")
        elif len(lane_rows[0]) < 2 or lane_rows[0][1] != ledger_path:
            errors.append(f"lane map ledger does not match sources.ledger.path: {lane_rows[0]}")

    role_path = Path(sources.get("role_contract", {}).get("path", ""))
    source_contract: dict[str, Any] = {}
    if role_path.is_file():
        try:
            source_contract = json.loads(role_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"cannot parse role contract source: {exc}")
    source_aliases = source_contract.get("rol_por_alias", {})
    source_hierarchy = source_contract.get("jerarquia", {})
    for member in data.get("members", []):
        if not isinstance(member, dict):
            continue
        role = member.get("role")
        names = [*member.get("aliases", [])]
        names.extend((member.get("identity", {}).get("received_actor"), member.get("boot", {}).get("handle")))
        for name in {name for name in names if isinstance(name, str)}:
            canonical_name = name == role and role in source_hierarchy
            if source_aliases.get(name) != role and not canonical_name:
                errors.append(f"{role}: source role contract does not map alias {name!r} to this role")
        declared = data.get("role_contract", {}).get(role, {})
        authoritative = source_hierarchy.get(role, {})
        if declared.get("reports_to") != authoritative.get("reporta_a"):
            errors.append(f"{role}: reports_to differs from role contract source")
        if declared.get("layer") != authoritative.get("capa"):
            errors.append(f"{role}: layer differs from role contract source")

        agent_root = member.get("lane_context", {}).get("agent_root")
        settings_path = Path(agent_root or "") / ".claude" / "settings.json"
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{role}: cannot read SessionStart settings {settings_path}: {exc}")
            settings = {}
        commands = [
            hook.get("command", "")
            for group in settings.get("hooks", {}).get("SessionStart", [])
            for hook in group.get("hooks", [])
            if isinstance(hook, dict)
        ]
        handle = member.get("boot", {}).get("handle")
        if not any("vigia-boot.sh" in command and command.rstrip().endswith(f" {handle}") for command in commands):
            errors.append(f"{role}: SessionStart does not call vigia-boot.sh with handle {handle!r}")

        pack = member.get("lane_context", {}).get("pack", {})
        path = Path(pack.get("path", ""))
        state = pack.get("state")
        exists = path.is_dir()
        if state == "present_observed" and not exists:
            errors.append(f"{member.get('role')}: pack declared present but missing: {path}")
        if state == "absent_observed" and exists:
            errors.append(f"{member.get('role')}: pack declared absent but now exists: {path}")
    errors.extend(validate_pack_adoption(data))
    return errors


def validate_evidence(
    evidence: dict[str, Any], manifest: dict[str, Any], manifest_path: Path
) -> list[str]:
    errors: list[str] = []
    if evidence.get("schema") != EVIDENCE_SCHEMA:
        errors.append(f"evidence schema must be {EVIDENCE_SCHEMA!r}")
    if evidence.get("fleet_id") != manifest.get("fleet_id"):
        errors.append("evidence fleet_id does not match manifest")
    expected_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    if evidence.get("manifest_sha256") != expected_hash:
        errors.append("evidence manifest_sha256 does not match manifest bytes")
    if parse_rfc3339(evidence.get("updated_at")) is None:
        errors.append("evidence.updated_at must be RFC3339 with an explicit timezone")

    authority = evidence.get("authority")
    if not isinstance(authority, dict):
        errors.append("evidence.authority must be an object")
        authority = {}
    authority_unavailable = authority.get("status") == "unavailable"
    if authority.get("receipt_schema") != RECEIPT_SCHEMA:
        errors.append(f"evidence.authority.receipt_schema must be {RECEIPT_SCHEMA!r}")
    if authority_unavailable:
        if authority.get("endpoint") is not None or authority.get("verifier") is not None:
            errors.append("unavailable receipt authority cannot declare endpoint or verifier")
        if authority.get("reason_code") != "receipt_v1_not_implemented":
            errors.append("unavailable receipt authority must use reason_code receipt_v1_not_implemented")
    else:
        errors.append(
            "receipt authority is unsupported: this checker has no v1 endpoint/signature verifier; "
            "stages must remain unmeasured"
        )

    records = evidence.get("members")
    if not isinstance(records, list):
        return errors + ["evidence.members must be an array"]

    expected: dict[str, dict[str, Any]] = {}
    for member in manifest["members"]:
        session = member["session"]["name"]
        expected[session] = {
            "lane": manifest["lane"],
            "role": member["role"],
            "session": session,
            "cursor": member["identity"]["canonical_cursor"],
            "handle": member["boot"]["handle"],
        }
    expected_sessions = set(expected)
    seen: set[str] = set()
    probe_owners: dict[str, str] = {}
    receipt_owners: dict[str, tuple[str, str]] = {}
    for index, record in enumerate(records):
        where = f"evidence.members[{index}]"
        if not isinstance(record, dict):
            errors.append(f"{where} must be an object")
            continue
        bindings = record.get("bindings")
        session = bindings.get("session") if isinstance(bindings, dict) else None
        if session in seen:
            errors.append(f"duplicate evidence session {session!r}")
        if session not in expected_sessions:
            errors.append(f"unknown evidence session {session!r}")
        if isinstance(session, str):
            seen.add(session)

        expected_binding = expected.get(session, {})
        binding_keys = {"lane", "role", "session", "cursor", "handle", "generation", "org_revision"}
        if not isinstance(bindings, dict) or set(bindings) != binding_keys:
            errors.append(f"{where}.bindings must contain exactly {', '.join(sorted(binding_keys))}")
            bindings = {}
        for key, value in expected_binding.items():
            if bindings.get(key) != value:
                errors.append(f"{where}.bindings.{key} must equal manifest value {value!r}")
        for key in ("generation", "org_revision"):
            value = bindings.get(key)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                errors.append(f"{where}.bindings.{key} must be null or a non-negative integer")

        probe_id = record.get("probe_id")
        if probe_id is not None and not is_uuid(probe_id):
            errors.append(f"{where}.probe_id must be null or a canonical UUID")
        if isinstance(probe_id, str):
            previous_session = probe_owners.get(probe_id)
            if previous_session is not None and previous_session != session:
                errors.append(
                    f"probe_id {probe_id!r} reused across sessions {previous_session!r} and {session!r}"
                )
            else:
                probe_owners[probe_id] = str(session)

        stages = record.get("stages")
        if not isinstance(stages, dict):
            errors.append(f"{where}.stages must be an object")
            continue
        if set(stages) != set(STAGES):
            errors.append(f"{where}.stages must contain exactly {', '.join(STAGES)}")
        measured_count = 0
        for stage in STAGES:
            observation = stages.get(stage)
            if not isinstance(observation, dict):
                errors.append(f"{where}.stages.{stage} must be an object")
                continue
            expected_stage_keys = {"result", "observed_at", "authority_receipt"}
            if stage == "monitor_armed":
                expected_stage_keys.add("freshness")
            if set(observation) != expected_stage_keys:
                errors.append(
                    f"{where}.stages.{stage} must contain exactly "
                    f"{', '.join(sorted(expected_stage_keys))}"
                )
            result = observation.get("result")
            if result not in RESULTS:
                errors.append(f"{where}.stages.{stage}.result must be one of {sorted(RESULTS)}")
                continue
            receipt = observation.get("authority_receipt")
            observed_at = observation.get("observed_at")
            if result == "unmeasured":
                if receipt is not None or observed_at is not None:
                    errors.append(
                        f"{where}.stages.{stage}: unmeasured cannot carry authority_receipt or observed_at"
                    )
            else:
                measured_count += 1
                if authority_unavailable:
                    errors.append(
                        f"{where}.stages.{stage}: cannot be {result}; receipt authority v1 is unavailable"
                    )
                observed_dt = parse_rfc3339(observed_at)
                if observed_dt is None:
                    errors.append(f"{where}.stages.{stage}: {result} requires an RFC3339 observed_at")
                if not is_uuid(probe_id):
                    errors.append(f"{where}.stages.{stage}: {result} requires the member's UUID probe_id")
                if not isinstance(bindings.get("generation"), int) or isinstance(bindings.get("generation"), bool):
                    errors.append(f"{where}.stages.{stage}: {result} requires integer generation")
                if not isinstance(bindings.get("org_revision"), int) or isinstance(bindings.get("org_revision"), bool):
                    errors.append(f"{where}.stages.{stage}: {result} requires integer org_revision")
                receipt_keys = {
                    "schema", "receipt_id", "probe_id", "stage", "bindings_sha256",
                    "issued_at", "signature",
                }
                if not isinstance(receipt, dict) or set(receipt) != receipt_keys:
                    errors.append(
                        f"{where}.stages.{stage}: {result} requires a typed authority receipt"
                    )
                    receipt = {}
                if receipt.get("schema") != RECEIPT_SCHEMA:
                    errors.append(f"{where}.stages.{stage}: receipt schema mismatch")
                if receipt.get("probe_id") != probe_id:
                    errors.append(f"{where}.stages.{stage}: receipt probe_id mismatch")
                if receipt.get("stage") != stage:
                    errors.append(f"{where}.stages.{stage}: receipt recycled across stages")
                binding_sha = hashlib.sha256(
                    json.dumps(bindings, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest()
                if receipt.get("bindings_sha256") != binding_sha:
                    errors.append(f"{where}.stages.{stage}: receipt bindings_sha256 mismatch")
                if parse_rfc3339(receipt.get("issued_at")) is None:
                    errors.append(f"{where}.stages.{stage}: receipt issued_at must be RFC3339")
                if not isinstance(receipt.get("signature"), str) or not receipt.get("signature"):
                    errors.append(f"{where}.stages.{stage}: receipt signature is required")
                receipt_id = receipt.get("receipt_id")
                if not is_uuid(receipt_id):
                    errors.append(f"{where}.stages.{stage}: receipt_id must be a canonical UUID")
                elif receipt_id in receipt_owners:
                    old_session, old_stage = receipt_owners[receipt_id]
                    errors.append(
                        f"authority receipt {receipt_id!r} reused across "
                        f"{old_session}/{old_stage} and {session}/{stage}"
                    )
                else:
                    receipt_owners[receipt_id] = (str(session), stage)

            if stage == "monitor_armed":
                freshness = observation.get("freshness")
                if not isinstance(freshness, dict) or set(freshness) != {"ttl_seconds", "valid_until"}:
                    errors.append(
                        f"{where}.stages.monitor_armed.freshness must contain ttl_seconds and valid_until"
                    )
                    freshness = {}
                ttl = freshness.get("ttl_seconds")
                valid_until = freshness.get("valid_until")
                if result == "unmeasured":
                    if ttl is not None or valid_until is not None:
                        errors.append(f"{where}.stages.monitor_armed: unmeasured freshness must be null")
                else:
                    if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl <= 0:
                        errors.append(f"{where}.stages.monitor_armed: ttl_seconds must be a positive integer")
                    valid_dt = parse_rfc3339(valid_until)
                    observed_dt = parse_rfc3339(observed_at)
                    if valid_dt is None:
                        errors.append(f"{where}.stages.monitor_armed: valid_until must be RFC3339")
                    elif observed_dt is not None and isinstance(ttl, int):
                        if (valid_dt - observed_dt).total_seconds() != ttl:
                            errors.append(
                                f"{where}.stages.monitor_armed: valid_until must equal observed_at + ttl_seconds"
                            )
                        if result == "pass" and valid_dt <= datetime.now(timezone.utc):
                            errors.append(f"{where}.stages.monitor_armed: pass receipt is stale")

        if measured_count == 0:
            if probe_id is not None:
                errors.append(f"{where}: fully unmeasured member must have probe_id=null")
            if bindings.get("generation") is not None or bindings.get("org_revision") is not None:
                errors.append(f"{where}: fully unmeasured member must have null generation/org_revision")
        if isinstance(stages.get("indexed"), dict) and stages["indexed"].get("result") == "pass":
            if stages.get("sent", {}).get("result") != "pass":
                errors.append(f"{where}: indexed=pass requires sent=pass")
        for dependent in ("peeked", "consumed"):
            if isinstance(stages.get(dependent), dict) and stages[dependent].get("result") == "pass":
                if stages.get("indexed", {}).get("result") != "pass":
                    errors.append(f"{where}: {dependent}=pass requires indexed=pass")
    missing = sorted(expected_sessions - seen)
    if missing:
        errors.append(f"evidence absent for sessions: {', '.join(missing)}")
    return errors


def require_stages(evidence: dict[str, Any], required: list[str]) -> list[str]:
    errors: list[str] = []
    if required and evidence.get("authority", {}).get("status") == "unavailable":
        return [
            "operational evidence gate is not executable: receipt authority v1 is unavailable; "
            "all stages must remain unmeasured"
        ]
    for record in evidence.get("members", []):
        if not isinstance(record, dict):
            continue
        for stage in required:
            result = record.get("stages", {}).get(stage, {}).get("result")
            if result != "pass":
                session = record.get("bindings", {}).get("session")
                errors.append(f"gate {stage}: {session} is {result or 'missing'}, not pass")
    return errors


def self_test(manifest: dict[str, Any], evidence: dict[str, Any] | None, manifest_path: Path) -> list[str]:
    failures: list[str] = []

    def must_fail(label: str, mutated: dict[str, Any], needle: str) -> None:
        messages = validate_manifest(mutated)
        if not any(needle in message for message in messages):
            failures.append(f"self-test {label} did not fail with {needle!r}: {messages}")

    duplicate = copy.deepcopy(manifest)
    duplicate["members"][1]["session"]["name"] = duplicate["members"][0]["session"]["name"]
    must_fail("duplicate-session", duplicate, "duplicate session")

    unknown = copy.deepcopy(manifest)
    unknown["members"][0]["role"] = "unknown-role"
    must_fail("unknown-role", unknown, "unknown to role_contract")

    ambiguous = copy.deepcopy(manifest)
    ambiguous["members"][1]["aliases"].append(manifest["members"][0]["identity"]["received_actor"])
    must_fail("ambiguous-alias", ambiguous, "ambiguous alias")

    absent = copy.deepcopy(manifest)
    absent["members"].pop()
    must_fail("missing-member", absent, "exactly 16 members")

    pack_errors = validate_pack_adoption(manifest)
    if len(pack_errors) != 6:
        failures.append(f"self-test missing-pack gate expected 6 blockers, got {pack_errors}")

    if evidence is not None:
        synthetic = copy.deepcopy(evidence)
        receipt_counter = 1000
        for member_index, record in enumerate(synthetic["members"], start=1):
            probe_id = str(uuid.UUID(int=member_index))
            record["probe_id"] = probe_id
            record["bindings"]["generation"] = 1
            record["bindings"]["org_revision"] = 1
            binding_sha = hashlib.sha256(
                json.dumps(record["bindings"], sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            for stage in STAGES:
                receipt_counter += 1
                observation = record["stages"][stage]
                observation.update({
                    "result": "pass",
                    "observed_at": "2099-01-01T00:00:00Z",
                    "authority_receipt": {
                        "schema": RECEIPT_SCHEMA,
                        "receipt_id": str(uuid.UUID(int=receipt_counter)),
                        "probe_id": probe_id,
                        "stage": stage,
                        "bindings_sha256": binding_sha,
                        "issued_at": "2099-01-01T00:00:00Z",
                        "signature": "synthetic-shape-is-not-authority",
                    },
                })
                if stage == "monitor_armed":
                    observation["freshness"] = {
                        "ttl_seconds": 300,
                        "valid_until": "2099-01-01T00:05:00Z",
                    }

        messages = validate_evidence(synthetic, manifest, manifest_path)
        unavailable = [message for message in messages if "receipt authority v1 is unavailable" in message]
        if len(unavailable) != EXPECTED_MEMBERS * len(STAGES):
            failures.append(
                "self-test 80 synthetic passes did not all die at unavailable authority: "
                f"got {len(unavailable)} authority failures"
            )

        cross_session = copy.deepcopy(synthetic)
        cross_session["members"][1]["probe_id"] = cross_session["members"][0]["probe_id"]
        messages = validate_evidence(cross_session, manifest, manifest_path)
        if not any("reused across sessions" in message for message in messages):
            failures.append("self-test cross-session probe recycling was accepted")

        cross_stage = copy.deepcopy(synthetic)
        first_stages = cross_stage["members"][0]["stages"]
        first_stages["indexed"]["authority_receipt"] = copy.deepcopy(
            first_stages["sent"]["authority_receipt"]
        )
        messages = validate_evidence(cross_stage, manifest, manifest_path)
        if not any("recycled across stages" in message for message in messages):
            failures.append("self-test cross-stage receipt recycling was accepted")
        if not any("reused across" in message and "/sent" in message for message in messages):
            failures.append("self-test duplicate authority receipt was accepted")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--check-sources", action="store_true")
    parser.add_argument("--require-stage", action="append", choices=STAGES, default=[])
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    try:
        manifest = load_json(args.manifest)
        evidence = load_json(args.evidence) if args.evidence else None
    except ValueError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    errors = validate_manifest(manifest)
    manifest_valid = not errors
    if args.require_stage and not args.check_sources:
        errors.append("operational gates require --check-sources")
    if args.check_sources and manifest_valid:
        errors.extend(validate_sources(manifest))
    evidence_valid = False
    if evidence is not None:
        if manifest_valid:
            evidence_errors = validate_evidence(evidence, manifest, args.manifest)
            errors.extend(evidence_errors)
            evidence_valid = not evidence_errors
            if evidence_valid:
                errors.extend(require_stages(evidence, args.require_stage))
    elif args.require_stage:
        errors.append("--require-stage needs --evidence")
    if args.self_test and manifest_valid and (evidence is None or evidence_valid):
        errors.extend(self_test(manifest, evidence, args.manifest))

    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1

    pack_counts: dict[str, int] = {}
    for member in manifest["members"]:
        state = member["lane_context"]["pack"]["state"]
        pack_counts[state] = pack_counts.get(state, 0) + 1
    summary = ", ".join(f"{key}={value}" for key, value in sorted(pack_counts.items()))
    print(f"PASS: {len(manifest['members'])}/16 members; aliases unambiguous; {summary}")
    if evidence is not None:
        counts = {stage: {result: 0 for result in RESULTS} for stage in STAGES}
        for record in evidence["members"]:
            for stage in STAGES:
                counts[stage][record["stages"][stage]["result"]] += 1
        print("EVIDENCE: " + "; ".join(
            f"{stage}=" + "/".join(f"{result}:{counts[stage][result]}" for result in ("pass", "fail", "unmeasured"))
            for stage in STAGES
        ))
    if args.self_test:
        print(
            "SELFTEST: PASS duplicate/unknown/ambiguous/missing; 80 synthetic passes; "
            "cross-session/stage recycling; six missing packs"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
