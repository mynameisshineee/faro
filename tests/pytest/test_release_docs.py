"""Link and command checks for the C4 release docs (docs/architecture,
docs/runbooks, docs/tutorial added for the single-lane pilot cut).

Two falsifiers, both static — no Docker, no live service:

1. every repo-relative markdown link in the three docs resolves to a real
   path in this tree;
2. every fenced code block immediately preceded by a literal
   ``<!-- doctest:run -->`` comment is executed with ``bash -c`` and must
   exit 0. Only commands the doc author marked as safe and local are
   tagged this way — anything that needs Docker or a live service is left
   unmarked on purpose and is not run here.

Runnable directly (no pytest required): ``python3 tests/pytest/test_release_docs.py``.
Also collectible by pytest once it is installed, via plain ``test_*`` functions.
"""
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

DOCS = [
    REPO_ROOT / "docs/architecture/coordination-kernel.md",
    REPO_ROOT / "docs/runbooks/single-lane-pilot.md",
    REPO_ROOT / "docs/tutorial/fleet-quickstart.md",
]

LINK_RE = re.compile(r"\]\(([^)]+)\)")
RUN_BLOCK_RE = re.compile(
    r"<!--\s*doctest:run\s*-->\s*```(?:bash|sh)?\n(.*?)```", re.DOTALL
)


def _local_links(text):
    for target in LINK_RE.findall(text):
        target = target.strip()
        if not target or target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        yield target.split("#", 1)[0]


def test_docs_exist():
    missing = [str(d) for d in DOCS if not d.is_file()]
    assert not missing, f"missing docs: {missing}"


def test_local_links_resolve():
    failures = {}
    for doc in DOCS:
        text = doc.read_text()
        # 🩸 UN ENLACE RELATIVO SE RESUELVE DESDE EL FICHERO QUE LO CONTIENE, no desde la
        # raiz del repo. Esto hacia `REPO_ROOT / t`, que para un doc en `docs/architecture/`
        # convierte `../GUARANTEES.md` en una ruta FUERA del repo y la declara colgada. Era
        # latente: hasta hoy ninguno de los tres DOCS usaba `../` (medido: 0 en cd4ffbc, 1 en
        # la punta), asi que el defecto no habia disparado nunca. `tools/higiene.py --links`
        # ya resolvia bien y por eso los dos instrumentos se contradecian sobre el MISMO
        # artefacto: `0 broken` alli, `dangling` aqui.
        bad = [t for t in _local_links(text) if not (doc.parent / t).resolve().exists()]
        if bad:
            failures[str(doc)] = bad
    assert not failures, f"dangling links: {failures}"


def test_marked_commands_pass():
    failures = []
    for doc in DOCS:
        text = doc.read_text()
        for block in RUN_BLOCK_RE.findall(text):
            result = subprocess.run(
                ["bash", "-c", block],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                failures.append(
                    f"{doc} :: rc={result.returncode}\n"
                    f"--- command ---\n{block}"
                    f"--- stdout ---\n{result.stdout}"
                    f"--- stderr ---\n{result.stderr}"
                )
    assert not failures, "\n\n".join(failures)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}\n{e}\n")
        else:
            print(f"PASS {t.__name__}")
    sys.exit(1 if failed else 0)
