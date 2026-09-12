"""Notice packaging regressions; run only with the normal capacity gate open."""
import importlib.util
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools/check-third-party-texts.py"
SPEC = importlib.util.spec_from_file_location("notice_check", SCRIPT)
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


@pytest.fixture
def collection(tmp_path):
    for relative in CHECK.INPUTS:
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)
    shutil.copytree(ROOT / "third_party", tmp_path / "third_party")
    return tmp_path


def manifest(root):
    return json.loads((root / "third_party/manifest.json").read_text())


def write_manifest(root, value):
    (root / "third_party/manifest.json").write_text(json.dumps(value))


def first_text(root):
    return root / "third_party" / manifest(root)["packages"][0]["texts"][0]["path"]


def test_real_collection_checks_without_claiming_release():
    assert CHECK.check(ROOT, ROOT / "third_party") == {
        "evidence_level": "collected_dependency_texts",
        "release_qualified": False,
        "dependency_inputs": 5,
        "packages": 60,
        "texts": 63,
        "coverage": {
            "npm": {"packages": 41, "anchor": "declared_collection_only",
                    "bundle_closure_verified": False},
            "pypi": {"packages": 19, "anchor": "exact_requirements_lock_package_set",
                     "installed_image_verified": False},
        },
    }


@pytest.mark.parametrize("mutation", ["missing", "altered", "empty", "symlink"])
def test_notice_text_must_be_present_and_exact(collection, mutation):
    path = first_text(collection)
    if mutation == "missing":
        path.unlink()
    elif mutation == "altered":
        path.write_bytes(path.read_bytes() + b"\nchanged\n")
    elif mutation == "empty":
        path.write_bytes(b"\n ")
    else:
        original = collection / "external-license"
        path.rename(original)
        path.symlink_to(original)
    with pytest.raises(CHECK.NoticeError):
        CHECK.check(collection, collection / "third_party")


@pytest.mark.parametrize("relative", sorted(CHECK.INPUTS))
def test_each_dependency_input_is_bound(collection, relative):
    path = collection / relative
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(CHECK.NoticeError, match="SHA-256 mismatch"):
        CHECK.check(collection, collection / "third_party")


def test_manifest_cannot_drop_input_binding(collection):
    value = manifest(collection)
    del value["inputs"]["web/package.json"]
    write_manifest(collection, value)
    with pytest.raises(CHECK.NoticeError, match="all five dependency inputs"):
        CHECK.check(collection, collection / "third_party")


def test_manifest_cannot_drop_pinned_python_package(collection):
    value = manifest(collection)
    index = next(i for i, p in enumerate(value["packages"]) if p["ecosystem"] == "pypi")
    removed = value["packages"].pop(index)
    for item in removed["texts"]:
        (collection / "third_party" / item["path"]).unlink()
    write_manifest(collection, value)
    with pytest.raises(CHECK.NoticeError, match="differ from lock pins"):
        CHECK.check(collection, collection / "third_party")


def test_collection_cannot_assert_release_qualification(collection):
    value = manifest(collection)
    value["release_qualified"] = True
    write_manifest(collection, value)
    with pytest.raises(CHECK.NoticeError, match="must not claim release"):
        CHECK.check(collection, collection / "third_party")


def test_unlisted_license_cannot_hide_outside_manifest(collection):
    (collection / "third_party/licenses/extra.txt").write_text("unlisted notice")
    with pytest.raises(CHECK.NoticeError, match="differ from manifest paths"):
        CHECK.check(collection, collection / "third_party")


def test_cli_missing_notice_has_failure_exit_and_no_success_receipt(collection):
    first_text(collection).unlink()
    result = subprocess.run([
        sys.executable, str(SCRIPT), "--input-root", str(collection),
        "--notices-root", str(collection / "third_party"),
    ], capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert result.stdout == ""
    assert "notice collection rejected: missing regular file" in result.stderr


@pytest.mark.parametrize("new_pin, expected", [
    ("uncollected[extra]==1.0", "differ from lock pins"),
    ('uncollected==1.0 ; python_version < "3.10"', "unsupported requirements.lock syntax"),
    ("uncollected>=1.0", "unsupported requirements.lock syntax"),
])
def test_updated_input_hash_cannot_hide_unsupported_or_uncollected_pin(
        collection, new_pin, expected):
    lock = collection / "requirements.lock"
    lock.write_bytes(lock.read_bytes() + new_pin.encode() + b"\n")
    value = manifest(collection)
    value["inputs"]["requirements.lock"] = hashlib.sha256(lock.read_bytes()).hexdigest()
    write_manifest(collection, value)
    with pytest.raises(CHECK.NoticeError, match=expected):
        CHECK.check(collection, collection / "third_party")
