#!/usr/bin/env python3
"""Check collected notice bytes against their dependency inputs; never release approval.

No network access, package imports, dependency resolution or code execution from
the collection. This deliberately does not infer the contents of a web bundle.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys


INPUTS = frozenset({
    "requirements.in", "requirements.lock", "web/package.json",
    "web/pnpm-lock.yaml", "web/pnpm-workspace.yaml",
})
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
PIN = re.compile(r"([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_.,-]+\])?"
                 r"==([^\s;\\]+)(?:\s*\\)?")
HASH_LINE = re.compile(r"--hash=sha256:[0-9a-f]{64}(?:\s*\\)?")


class NoticeError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise NoticeError(message)


def read_regular(root: Path, relative: str) -> bytes:
    require(isinstance(relative, str) and bool(relative), "missing relative path")
    parts = PurePosixPath(relative)
    require(not parts.is_absolute() and ".." not in parts.parts
            and str(parts) == relative, f"invalid relative path: {relative}")
    path = root
    for part in parts.parts:
        path = path / part
        require(not path.is_symlink(), f"symlink is not collected evidence: {relative}")
    require(path.is_file(), f"missing regular file: {relative}")
    data = path.read_bytes()
    require(bool(data.strip()), f"empty file: {relative}")
    return data


def verify_hash(data: bytes, expected: object, relative: str) -> None:
    require(isinstance(expected, str) and SHA256.fullmatch(expected) is not None,
            f"invalid SHA-256: {relative}")
    require(hashlib.sha256(data).hexdigest() == expected,
            f"SHA-256 mismatch: {relative}")


def normalized_python_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def locked_python_packages(lock: str) -> dict[str, str]:
    """Accept the supported exact-pin format; never silently omit other syntax.

    Extras belong to the named distribution. Conditional markers would require
    a target environment, so this collector rejects them instead of guessing.
    """
    locked = {}
    for number, raw in enumerate(lock.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pin = PIN.fullmatch(line)
        if pin:
            name, version = pin.groups()
            key = normalized_python_name(name)
            require(key not in locked, f"duplicate Python lock pin: {name}")
            locked[key] = version
        elif HASH_LINE.fullmatch(line) and locked:
            continue
        else:
            raise NoticeError(f"unsupported requirements.lock syntax at line {number}")
    require(bool(locked), "no exact Python pins in requirements.lock")
    return locked


def check(input_root: Path, notices_root: Path) -> dict:
    manifest = json.loads(read_regular(notices_root, "manifest.json"))
    require(isinstance(manifest, dict), "manifest must be an object")
    require(type(manifest.get("schema_version")) is int
            and manifest["schema_version"] == 1, "unsupported manifest schema")
    require(manifest.get("evidence_level") == "collected_dependency_texts"
            and manifest.get("release_qualified") is False,
            "collection must not claim release qualification")
    inputs = manifest.get("inputs")
    require(isinstance(inputs, dict) and set(inputs) == INPUTS,
            "manifest must bind all five dependency inputs")
    for relative, digest in inputs.items():
        verify_hash(read_regular(input_root, relative), digest, relative)

    packages = manifest.get("packages")
    require(isinstance(packages, list) and bool(packages), "empty package collection")
    identities, paths, python_packages = set(), set(), {}
    for package in packages:
        require(isinstance(package, dict), "package must be an object")
        ecosystem, name, version = (package.get(k) for k in ("ecosystem", "name", "version"))
        require(ecosystem in ("npm", "pypi")
                and isinstance(name, str) and bool(name.strip())
                and isinstance(version, str) and bool(version.strip()),
                "package requires ecosystem, name and version")
        identity = (ecosystem, name, version)
        require(identity not in identities, f"duplicate package: {identity}")
        identities.add(identity)
        if ecosystem == "pypi":
            key = normalized_python_name(name)
            require(key not in python_packages, f"duplicate Python package: {name}")
            python_packages[key] = version
        texts = package.get("texts")
        require(isinstance(texts, list) and bool(texts), f"no notice text: {identity}")
        for item in texts:
            require(isinstance(item, dict), f"invalid notice record: {identity}")
            relative = item.get("path")
            require(isinstance(relative, str) and relative.startswith("licenses/"),
                    f"notice path outside licenses/: {identity}")
            require(relative not in paths, f"duplicate notice path: {relative}")
            paths.add(relative)
            verify_hash(read_regular(notices_root, relative), item.get("sha256"), relative)

    # Collection completeness is bounded by these exact Python pins. For npm,
    # dependency input hashes bind the reviewed collection; bundle closure is a
    # separate pending gate, not something a hand-written manifest can prove.
    lock = read_regular(input_root, "requirements.lock").decode("utf-8")
    locked = locked_python_packages(lock)
    require(python_packages == locked, "Python notice packages differ from lock pins")

    license_root = notices_root / "licenses"
    actual = {path.relative_to(notices_root).as_posix()
              for path in license_root.rglob("*")
              if path.is_file() or path.is_symlink()}
    require(actual == paths, "license files differ from manifest paths")
    return {
        "evidence_level": "collected_dependency_texts",
        "release_qualified": False,
        "dependency_inputs": len(inputs),
        "packages": len(identities),
        "texts": len(paths),
        "coverage": {
            "npm": {"packages": sum(e == "npm" for e, _, _ in identities),
                    "anchor": "declared_collection_only",
                    "bundle_closure_verified": False},
            "pypi": {"packages": len(python_packages),
                     "anchor": "exact_requirements_lock_package_set",
                     "installed_image_verified": False},
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", required=True, type=Path)
    parser.add_argument("--notices-root", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = check(args.input_root, args.notices_root)
    except (NoticeError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"notice collection rejected: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
