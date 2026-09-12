#!/usr/bin/env python3
"""Mide las seis garantias sobre implementacion Y falsificadores ejecutables.

El medidor no decide por frecuencia de palabras. Cada garantia declara un censo
de evidencias concretas, repartidas entre el borde nativo, el nucleo, busqueda,
observabilidad, proyeccion y al menos un test. Todas presentes significa
``IMPLEMENTED``; alguna significa ``PARTIAL``; ninguna significa ``DESIGNED``.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
DOC = RAIZ / "docs" / "GUARANTEES.md"

# (etiqueta, fichero, expresion). Las expresiones buscan nombres de contrato, no
# vocabulario incidental. Los comentarios y docstrings se eliminan antes.
GARANTIAS = {
    "G1": (
        "Identity is not self-declared",
        (
            ("HTTP /whoami", "native_gateway.py", r"@router\.get\(['\"]\/whoami['\"]\)"),
            ("server authentication", "coordination.py", r"def authenticate\(self, token"),
            ("server-derived telemetry identity", "observability.py", r"def server_derived\(cls, \*, principal"),
            ("identity falsifier", "tests/native_gateway/test_native_gateway.py", r"def test_whoami_es_derivado_y_headers_no_lo_pueden_pisar\("),
        ),
    ),
    "G2": (
        "Authority is gradable",
        (
            ("per-lane transition", "coordination.py", r"def transition_admissions\("),
            ("operator-only endpoint", "operator_admission.py", r"@router\.post\(['\"]\/admission\/transition['\"]\)"),
            ("legacy mutation policy", "runtime_root.py", r"def _legacy_mutation_policy\("),
            ("operator capability falsifier", "tests/native_gateway/test_operator_admission.py", r"def test_el_mapa_v8_concede_solo_admission_operator\("),
        ),
    ),
    "G3": (
        "Delivery is demonstrable",
        (
            ("atomic event admission", "coordination.py", r"def accept_event\(self, token"),
            ("durable receipts", "coordination.py", r"CREATE TABLE IF NOT EXISTS receipts \("),
            ("durable idempotency", "coordination.py", r"CREATE TABLE IF NOT EXISTS idempotency \("),
            ("receipt HTTP read", "native_gateway.py", r"@router\.get\(['\"]\/receipts/\{receipt_id\}['\"]\)"),
            ("idempotency falsifier", "tests/journal/test_eventos_idempotencia_causas.py", r"def test_misma_clave_cuerpo_distinto_es_conflicto_SIN_mutacion\("),
        ),
    ),
    "G4": (
        "One live owner",
        (
            ("lease acquisition", "coordination.py", r"def acquire_lease\(self, token"),
            ("fence validation", "coordination.py", r"def check_fence\(self, token"),
            ("lease HTTP mutation", "native_gateway.py", r"@router\.post\(['\"]\/leases/\{resource\}['\"]"),
            ("stale-owner falsifier", "tests/journal/test_leases_y_comandos.py", r"def test_el_dueno_vencido_es_RECHAZADO_al_mutar_tras_el_relevo\("),
        ),
    ),
    "G5": (
        "Recovery is unambiguous",
        (
            ("outbox claim", "coordination.py", r"def claim_outbox\(self, token"),
            ("projector", "projector.py", r"def project_next\(self, \*, lease_s"),
            ("active runner", "projector_runner.py", r"class ActiveProjectorRunner"),
            ("projection telemetry", "observability.py", r"def outbox_span\(self, \*, event_id"),
            ("crash convergence falsifier", "tests/projector/test_projector.py", r"def test_crash_pre_mid_post_append_converge_sin_duplicar\("),
        ),
    ),
    "G6": (
        "Reading at scale",
        (
            ("FTS5 index", "search_store.py", r"CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5\("),
            ("bound search contract", "search_store.py", r"def search\(self, \*, lane: str \| None\s*=\s*None, ledger: str, query: str"),
            ("query compiler", "search_contract.py", r"def compile_query\(crudo: str\)"),
            ("explicit truncation", "search_store.py", r"['\"]truncado['\"]: truncado"),
            ("keyset/truncation falsifier", "tests/search/test_cursor_y_keyset.py", r"def test_truncado_se_declara_siempre\("),
        ),
    ),
}

ESTADOS = ("IMPLEMENTED", "PARTIAL", "DESIGNED")


def solo_codigo(fuente: str, es_python: bool) -> str:
    """Retira comentarios y docstrings para no confundir mencion con codigo."""
    if es_python:
        try:
            arbol = ast.parse(fuente)
            for nodo in ast.walk(arbol):
                if isinstance(nodo, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    cuerpo = nodo.body
                    if (cuerpo and isinstance(cuerpo[0], ast.Expr)
                            and isinstance(cuerpo[0].value, ast.Constant)
                            and isinstance(cuerpo[0].value.value, str)):
                        nodo.body = cuerpo[1:] or [ast.Pass()]
            return ast.unparse(ast.fix_missing_locations(arbol))
        except SyntaxError:
            return ""
    return "\n".join(
        linea for linea in fuente.splitlines()
        if not linea.lstrip().startswith("#")
    )


def cargar_fuentes(raiz: Path = RAIZ) -> dict[str, str]:
    """Carga exactamente todos los ficheros censados; ninguno es opcional."""
    nombres = {fichero for _, evidencias in GARANTIAS.values()
               for _, fichero, _ in evidencias}
    fuera: dict[str, str] = {}
    for nombre in sorted(nombres):
        ruta = raiz / nombre
        if not ruta.is_file():
            raise FileNotFoundError(nombre)
        fuera[nombre] = solo_codigo(
            ruta.read_text(encoding="utf-8", errors="replace"),
            ruta.suffix == ".py",
        )
    return fuera


def medir_garantias(fuentes: dict[str, str]) -> dict[str, dict[str, object]]:
    """Devuelve estado y evidencia, sin leer docs ni git (facil de falsificar)."""
    fuera: dict[str, dict[str, object]] = {}
    for gid, (nombre, evidencias) in GARANTIAS.items():
        presentes = []
        ausentes = []
        for etiqueta, fichero, patron in evidencias:
            destino = presentes if re.search(patron, fuentes.get(fichero, "")) else ausentes
            destino.append(f"{etiqueta} [{fichero}]")
        estado = "IMPLEMENTED" if not ausentes else ("PARTIAL" if presentes else "DESIGNED")
        fuera[gid] = {
            "nombre": nombre,
            "estado": estado,
            "presentes": presentes,
            "ausentes": ausentes,
        }
    return fuera


def estados_declarados(doc: Path = DOC) -> dict[str, str]:
    if not doc.is_file():
        return {}
    texto = doc.read_text(encoding="utf-8")
    fuera = {}
    for gid in GARANTIAS:
        m = re.search(rf"^## {gid} — .*?^\*\*Status: ([A-Z]+)", texto, re.S | re.M)
        if m:
            fuera[gid] = m.group(1)
    return fuera


def discrepancias(medido: dict[str, dict[str, object]],
                  declarado: dict[str, str]) -> list[str]:
    """Compara estados exactos: PARTIAL no equivale a IMPLEMENTED."""
    problemas = []
    for gid, resultado in medido.items():
        dice = declarado.get(gid)
        actual = str(resultado["estado"])
        if dice is None:
            problemas.append(f"{gid}: docs/GUARANTEES.md does not state a status")
        elif dice not in ESTADOS:
            problemas.append(f"{gid}: unknown status `{dice}`")
        elif dice != actual:
            problemas.append(f"{gid}: page says {dice}, measured state is {actual}")
    return problemas


def main() -> int:
    try:
        fuentes = cargar_fuentes()
    except FileNotFoundError as exc:
        print(f"FATAL: falta el fichero censado {exc.args[0]}", file=sys.stderr)
        return 2
    if not fuentes or any(not texto.strip() for texto in fuentes.values()):
        print("FATAL: una fuente censada no contiene codigo medible", file=sys.stderr)
        return 2

    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=RAIZ, capture_output=True,
            text=True, check=True,
        ).stdout.strip()
        sucio = bool(subprocess.run(
            ["git", "status", "--porcelain"], cwd=RAIZ,
            capture_output=True, text=True,
        ).stdout.strip())
    except (subprocess.CalledProcessError, OSError):
        sha, sucio = "unknown", True

    medido = medir_garantias(fuentes)
    declarado = estados_declarados()
    print(f"measured against {sha}{' (DIRTY TREE)' if sucio else ''}")
    print("implementation and falsifier census: native/search/observability/projector\n")
    for gid, resultado in medido.items():
        presentes = resultado["presentes"]
        ausentes = resultado["ausentes"]
        print(f"{gid}  measured={resultado['estado']:<8} page={declarado.get(gid, '?'):<8} "
              f"evidence={len(presentes)}/{len(presentes) + len(ausentes)}")
        for falta in ausentes:
            print(f"    missing: {falta}")

    problemas = discrepancias(medido, declarado)
    print()
    if problemas:
        for problema in problemas:
            print(f"::error::{problema}")
        print(f"\n{len(problemas)} guarantee(s) whose exact status does not match this tree.")
        return 1
    print("every stated status exactly matches the measured implementation and tests.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
