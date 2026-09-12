#!/usr/bin/env bash
# Cheap, offline checks for the release chain. The two-build gate remains separate.
set -euo pipefail
cd "$(dirname "$0")/.."

python3 - <<'PY'
import pathlib
import re
import importlib.util

root = pathlib.Path.cwd()
workflow = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
uses = re.findall(r"^\s*- uses:\s*([^\s#]+)", workflow, re.M)
assert uses, "CI contains no actions"
bad = [value for value in uses if not re.fullmatch(r"[^@]+@[0-9a-f]{40}", value)]
assert not bad, f"Actions not pinned to full commit SHA: {bad}"
release_job = workflow.split("  release-reproducible:", 1)[1].split("\n  dco:", 1)[0]
assert "upload-artifact" not in release_job and "--push" not in release_job

package = __import__("json").loads((root / "web/package.json").read_text(encoding="utf-8"))
assert package["packageManager"] == "pnpm@10.33.2"

dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
stage_images = re.findall(r"^FROM\s+(\S+)", dockerfile, re.M)
froms = list(dict.fromkeys(stage_images))
assert len(froms) == 2, f"expected two base images, found {froms}"
bad = [image for image in froms if not re.fullmatch(r"[^@]+@sha256:[0-9a-f]{64}", image)]
assert not bad, f"base images not pinned by digest: {bad}"
assert "--require-hashes -r /app/requirements.lock" in dockerfile
assert "--no-compile" in dockerfile
assert "--invalidation-mode=checked-hash" in dockerfile
assert "COPY requirements.lock /app/requirements.lock" in dockerfile
assert 'org.opencontainers.image.revision="$VCS_REF"' in dockerfile
assert "COPY LICENSE NOTICE THIRD_PARTY_NOTICES.md /usr/share/doc/llminbox/" in dockerfile
assert "ARG PNPM_VERSION=10.33.2" in dockerfile
assert re.search(r"ARG PNPM_SHA512=[0-9a-f]{128}$", dockerfile, re.M)
assert 'sha512sum -c -' in dockerfile and 'npm install --global --ignore-scripts' in dockerfile

lock = (root / "requirements.lock").read_text(encoding="utf-8")
packages = re.findall(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)\s*\\$", lock, re.M)
assert len(packages) >= 10, f"lock does not look transitive: only {len(packages)} pins"
for name, version in packages:
    block = lock[lock.index(f"{name}=={version}"):]
    block = block.split("\n# via", 1)[0]
    assert "--hash=sha256:" in block, f"{name} has no artefact hash"

builder = (root / "tools/build-release.sh").read_text(encoding="utf-8")
for needle in ("one.oci.tar", "two.oci.tar", "NON-REPRODUCIBLE",
               "cyclonedx-json", "release-provenance.py", "--precheck",
               '--build-arg "VCS_REF=$VCS_REF"', "publish-release-stage.py"):
    assert needle in builder, f"release builder does not enforce {needle}"
assert re.search(r'anchore/syft:v[^@]+@sha256:[0-9a-f]{64}', builder)
assert "--push" not in builder, "release builder must remain local until the signing decision"

# Dos nombres en el log no bastan: cada export se ejecuta contra su propio daemon
# BuildKit y con la caché de instrucciones desactivada.
assert builder.count("docker buildx create --name") == 2
assert '--builder "$builder"' in builder and "--no-cache" in builder
assert 'build_one "$TMP/one.oci.tar" "$BUILDER_ONE"' in builder
assert 'build_one "$TMP/two.oci.tar" "$BUILDER_TWO"' in builder

provenance_path = root / "tools/release-provenance.py"
provenance_source = provenance_path.read_text(encoding="utf-8")
assert not re.search(r'"sha256"\s*:\s*"[0-9a-f]{64}"', provenance_source), \
    "release provenance contains a copied digest instead of deriving Dockerfile materials"
spec = importlib.util.spec_from_file_location("release_provenance", provenance_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
materials = module.dockerfile_base_materials(root / "Dockerfile")
assert len(materials) == len(froms)
for image, material in zip(froms, materials):
    reference, digest = image.rsplit("@sha256:", 1)
    assert material["digest"] == {"sha256": digest}
    slash, colon = reference.rfind("/"), reference.rfind(":")
    expected_uri = (f"pkg:docker/{reference[:colon]}@{reference[colon + 1:]}"
                    if colon > slash else f"pkg:docker/{reference}")
    assert material["uri"] == expected_uri

# Reusing the same pinned base in another stage adds no new material; using
# the same image reference with another digest must remain an error.
import tempfile
with tempfile.TemporaryDirectory() as scratch:
    fixture = pathlib.Path(scratch) / "Dockerfile"
    fixture.write_text(f"FROM {froms[0]} AS first\nFROM {froms[0]}\n")
    assert module.dockerfile_base_materials(fixture) == [materials[0]]
    reference, digest = froms[0].rsplit("@sha256:", 1)
    other_digest = ("0" if digest[0] != "0" else "1") + digest[1:]
    fixture.write_text(f"FROM {froms[0]} AS first\nFROM {reference}@sha256:{other_digest}\n")
    try:
        module.dockerfile_base_materials(fixture)
    except SystemExit as exc:
        assert "conflicting digests" in str(exc)
    else:
        raise AssertionError("different digests for one base reference were accepted")
print(f"static release chain: {len(uses)} Actions by SHA, {len(packages)} Python pins with hashes")
PY

# La publicación local nunca mezcla ni pisa evidencia existente.
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir "$TMP/stage"
printf 'evidence\n' > "$TMP/stage/item"
python3 tools/publish-release-stage.py "$TMP/stage" "$TMP/dist"
if python3 tools/publish-release-stage.py "$TMP/stage" "$TMP/dist" >/dev/null 2>&1; then
  echo "publish-release-stage overwrote an existing destination" >&2
  exit 1
fi
[ "$(cat "$TMP/dist/item")" = evidence ]

bash tests/healthcheck.sh
