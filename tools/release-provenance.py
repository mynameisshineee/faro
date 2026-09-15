#!/usr/bin/env python3
"""Emit a deterministic in-toto statement for one built llminbox artefact."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import subprocess
import urllib.parse


ROOT = pathlib.Path(__file__).resolve().parent.parent
HEX64 = re.compile(r"^[0-9a-f]{64}$")
FROM = re.compile(
    r"^FROM[ \t]+(?P<image>[^ \t\r\n]+)(?:[ \t]+AS[ \t]+[^ \t\r\n]+)?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()


def source_uri() -> str:
    remote = git("config", "--get", "remote.origin.url")
    user, separator, scp_path = remote.partition("@")
    if user == "git" and separator and scp_path.startswith("github.com:"):
        remote = "https://github.com/" + scp_path.removeprefix("github.com:")
    if remote.endswith(".git"):
        remote = remote[:-4]
    if remote.startswith("https://"):
        parsed = urllib.parse.urlsplit(remote)
        if (not parsed.hostname or parsed.username or parsed.password or parsed.query
                or parsed.fragment):
            raise SystemExit(f"unsafe origin URL for provenance: {remote!r}")
    elif remote.startswith("ssh://"):
        parsed = urllib.parse.urlsplit(remote)
        if not parsed.hostname or parsed.password or parsed.query or parsed.fragment:
            raise SystemExit(f"unsafe origin URL for provenance: {remote!r}")
    elif not re.fullmatch(r"git@[^:/\s]+:[^\s]+", remote):
        raise SystemExit(f"unsupported origin URL for provenance: {remote!r}")
    return f"git+{remote}@{git('rev-parse', 'HEAD')}"


def dockerfile_base_materials(path: pathlib.Path) -> list[dict[str, object]]:
    """Deriva los materiales base del Dockerfile revisado, sin una segunda verdad.

    Un `FROM` sin digest o interpolado no se puede convertir en procedencia
    verificable: se rechaza en vez de publicar el digest que alguien copió a mano.
    """
    if not path.is_file() or path.is_symlink():
        raise SystemExit("Dockerfile must be a regular, non-symlink file")
    images = [match.group("image") for match in FROM.finditer(path.read_text(encoding="utf-8"))]
    if not images:
        raise SystemExit("Dockerfile has no FROM instructions")
    materials: list[dict[str, object]] = []
    seen: dict[str, str] = {}
    for image in images:
        if "$" in image:
            raise SystemExit(f"Dockerfile FROM is interpolated, not immutable: {image!r}")
        reference, separator, digest = image.rpartition("@sha256:")
        if not separator or not reference or not HEX64.fullmatch(digest):
            raise SystemExit(f"Dockerfile FROM is not pinned by sha256 digest: {image!r}")
        # OCI references use the last colon after the final slash as tag separator.
        slash, colon = reference.rfind("/"), reference.rfind(":")
        if colon > slash:
            name, version = reference[:colon], reference[colon + 1:]
        else:
            name, version = reference, None
        uri = f"pkg:docker/{name}" + (f"@{version}" if version else "")
        if uri in seen:
            if seen[uri] != digest:
                raise SystemExit(f"Dockerfile gives conflicting digests for base material {uri!r}")
            # Notices and runtime stages can use the very same pinned image.
            continue
        seen[uri] = digest
        materials.append({"uri": uri, "digest": {"sha256": digest}})
    return materials


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("artefact", type=pathlib.Path)
    parser.add_argument("output", type=pathlib.Path)
    parser.add_argument("--platform", required=True)
    args = parser.parse_args()

    if args.platform not in {"linux/amd64", "linux/arm64"}:
        raise SystemExit(f"unsupported platform for provenance: {args.platform!r}")

    artefact = args.artefact.resolve(strict=True)
    if not artefact.is_file() or artefact.is_symlink():
        raise SystemExit("artefact must be a regular, non-symlink file")
    if git("status", "--porcelain"):
        raise SystemExit("refusing provenance for a dirty tree")

    source = source_uri()
    dependencies = dockerfile_base_materials(ROOT / "Dockerfile")
    dependencies.extend([
        {"uri": "file:Dockerfile", "digest": {"sha256": sha256(ROOT / "Dockerfile")}},
        {"uri": "file:requirements.lock", "digest": {"sha256": sha256(ROOT / "requirements.lock")}},
        {"uri": "file:web/pnpm-lock.yaml", "digest": {"sha256": sha256(ROOT / "web" / "pnpm-lock.yaml")}},
    ])
    statement = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [
            {"name": artefact.name, "digest": {"sha256": sha256(artefact)}}
        ],
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildDefinition": {
                "buildType": "urn:llminbox:build/oci/v1",
                "externalParameters": {
                    "platform": args.platform,
                    "source": source,
                },
                "internalParameters": {
                    "sourceDateEpoch": int(git("show", "-s", "--format=%ct", "HEAD")),
                },
                "resolvedDependencies": dependencies,
            },
            "runDetails": {
                "builder": {"id": source.rsplit("@", 1)[0] + "/tools/build-release.sh"},
                "metadata": {"invocationId": git("rev-parse", "HEAD")},
            },
        },
    }
    payload = (json.dumps(statement, sort_keys=True, separators=(",", ":")) + "\n").encode()
    output = args.output.absolute()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(output, flags, 0o644)
    try:
        with os.fdopen(fd, "wb", closefd=False) as target:
            target.write(payload)
            target.flush()
        os.fsync(fd)
    finally:
        os.close(fd)
    directory = os.open(output.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
