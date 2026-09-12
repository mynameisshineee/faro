#!/usr/bin/env python3
"""Gate hermético de composición lifecycle v5, sin copiar sujetos al commit.

Construye un árbol desechable desde esta rama y superpone por ``git show`` los objetos
externos exactos. Cualquier ref/blob ausente, deriva, conflicto, skip implícito o mutante
superviviente es rojo. No escribe en ninguno de los worktrees sujetos.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
INTEGRATION = Path(os.environ.get(
    "LLMINBOX_INTEGRATION_ROOT", "/private/tmp/llminbox-v09-integration"))
SCHEMA = Path(os.environ.get(
    "LLMINBOX_SCHEMA_ROOT", "/private/tmp/llminbox-m2-schema-keyset"))
GATEWAY = Path(os.environ.get(
    "LLMINBOX_GATEWAY_ROOT", "/private/tmp/llminbox-native-gateway"))

BASE = "7778de96c36bc4e4be4005b0b6c6e2f927673304"
INTEGRATION_COMMIT = "632a74c2fa7ff8fc8d09ae3636241e3a63bc9071"
NATIVE_CLI_COMMIT = "32bbba22"
SCHEMA_COMMIT = "efecf184069dfb8dd8e1e21611897f740526b8b6"
SCHEMA_SHA256 = "1fbcc2a8ad2fd69835a181f36e84ab0f197a88d5b633e769ce53e0651c88db1d"
GATEWAY_COMMIT = "bbf238131c1b69e6f4c1ec00ae3b51f67a4a863d"
GATEWAY_SHA256 = "c8a5f416eb9d454a8aa7688753c584268ca936583e23aebfedc5436756385576"

HELP = (
    "  llmi search rebuild       prepara ACL y reconstruye Search EXPLÍCITAMENTE;\n"
    "  llmi search migrate 1 2   migra EXPLÍCITAMENTE el schema reconocido 1→2;\n"
    "                            el arranque nunca hace ninguno de estos trabajos\n"
)
DISPATCH = (
    "  search) { [ $# -eq 1 ] && [ \"$1\" = \"rebuild\" ]; } \\\n"
    "            || { [ $# -eq 3 ] && [ \"$1\" = \"migrate\" ] \\\n"
    "                 && [ \"$2\" = \"1\" ] && [ \"$3\" = \"2\" ]; } \\\n"
    "            || { mal_uso; }\n"
    "          docker inspect \"$LLMINBOX_NAME\" >/dev/null 2>&1 \\\n"
    "            || { sin_servicio; exit 3; }\n"
    "          docker exec \"$LLMINBOX_NAME\" python3 -m servicio search \"$@\" ;;\n"
)


def _run(args, *, cwd, check=True, timeout=120):
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=check)


def _show(repo: Path, commit: str, path: str) -> bytes:
    if not repo.is_dir():
        raise RuntimeError(f"PRECONDICIÓN: falta worktree {repo}")
    p = subprocess.run(
        ["git", "show", f"{commit}:{path}"], cwd=repo, capture_output=True)
    if p.returncode:
        raise RuntimeError(
            f"PRECONDICIÓN: {commit}:{path} no está disponible en {repo}: "
            + p.stderr.decode(errors="replace"))
    return p.stdout


def _overlay(clon: Path) -> str:
    schema = _show(SCHEMA, SCHEMA_COMMIT, "search_store.py")
    gateway = _show(GATEWAY, GATEWAY_COMMIT, "native_gateway.py")
    if hashlib.sha256(schema).hexdigest() != SCHEMA_SHA256:
        raise RuntimeError("PRECONDICIÓN: deriva del search_store.py de schema v5")
    if hashlib.sha256(gateway).hexdigest() != GATEWAY_SHA256:
        raise RuntimeError("PRECONDICIÓN: deriva del native_gateway.py auditado")
    (clon / "search_store.py").write_bytes(schema)
    (clon / "native_gateway.py").write_bytes(gateway)
    # Este gate histórico superpone un gateway externo exacto sobre un árbol que hoy
    # trae el falsificador integrado repinado. El pin del CLON debe seguir al objeto
    # superpuesto, no al gateway actual de ROOT; de lo contrario el propio gate fabrica
    # una deriva determinista antes de ejecutar una sola propiedad.
    gateway_blob = hashlib.sha1(
        f"blob {len(gateway)}\0".encode("ascii") + gateway,
        usedforsecurity=False,
    ).hexdigest()
    lifecycle_pin = clon / "tests/search/falsificador_native_lifecycle_v3.py"
    pin_source = lifecycle_pin.read_text()
    pin_source, sha_n = re.subn(
        r'GATEWAY_SHA256 = "[0-9a-f]{64}"',
        f'GATEWAY_SHA256 = "{GATEWAY_SHA256}"', pin_source, count=1,
    )
    pin_source, blob_n = re.subn(
        r'GATEWAY_GIT_BLOB = "[0-9a-f]{40}"',
        f'GATEWAY_GIT_BLOB = "{gateway_blob}"', pin_source, count=1,
    )
    if sha_n != 1 or blob_n != 1:
        raise RuntimeError("el falsificador nativo no expone sus dos pins exactos")
    lifecycle_pin.write_text(pin_source)
    # Lifecycle no modifica coordination.py; en la composición pertenece íntegro al
    # sujeto de integración y el gateway debe probarse contra ese objeto, no el viejo.
    (clon / "coordination.py").write_bytes(
        _show(INTEGRATION, INTEGRATION_COMMIT, "coordination.py"))

    for nombre, commit in (
        ("native-cli", NATIVE_CLI_COMMIT),
        ("schema v5", SCHEMA_COMMIT),
        ("gateway final", GATEWAY_COMMIT),
    ):
        heredado = _run(
            ["git", "merge-base", "--is-ancestor", commit, INTEGRATION_COMMIT],
            cwd=INTEGRATION, check=False)
        if heredado.returncode != 0:
            raise RuntimeError(
                f"PRECONDICIÓN: integración final no contiene {nombre} {commit}")
    nativo = _show(INTEGRATION, INTEGRATION_COMMIT, "llmi").decode()
    base = _show(INTEGRATION, BASE, "llmi").decode()
    lifecycle = (ROOT / "llmi").read_text()
    with tempfile.TemporaryDirectory(prefix="llmi-merge-v4-") as d:
        d = Path(d)
        (d / "ours").write_text(nativo)
        (d / "base").write_text(base)
        (d / "theirs").write_text(lifecycle)
        merge = _run(
            ["git", "merge-file", "-p", "ours", "base", "theirs"], cwd=d,
            check=False)
    if merge.returncode != 0 or "<<<<<<<" in merge.stdout:
        raise RuntimeError("llmi no auto-mergea limpiamente:\n" + merge.stdout[-4000:])
    combinado = merge.stdout
    if combinado.count(HELP) != 1 or combinado.count(DISPATCH) != 1:
        raise RuntimeError("llmi combinado no contiene exactamente los dos hunks lifecycle")
    # Gate semántico mínimo y fuerte: al retirar exactamente esos dos bloques, cada byte
    # de política, sesión, secreto y código de salida del CLI nativo debe seguir igual.
    restaurado = combinado.replace(HELP, "", 1).replace(DISPATCH, "", 1)
    if restaurado != nativo:
        raise RuntimeError(
            "el auto-merge de llmi alteró native-cli fuera de los dos hunks Search")
    destino = clon / "llmi"
    destino.write_text(combinado)
    destino.chmod(0o755)
    return hashlib.sha256(combinado.encode()).hexdigest()


def _pytest(clon: Path, *nodeids: str, timeout=120) -> None:
    p = _run([sys.executable, "-m", "pytest", "-q", *nodeids], cwd=clon,
             check=False, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError("pytest focal rojo:\n" + (p.stdout + p.stderr)[-6000:])
    print(p.stdout.strip())


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="llminbox-composition-v5-") as d:
        clon = Path(d) / "tree"
        shutil.copytree(
            ROOT, clon,
            ignore=shutil.ignore_patterns(
                ".git", ".pytest_cache", "__pycache__", "*.pyc"))
        llmi_sha = _overlay(clon)
        print(f"overlay schema={SCHEMA_COMMIT} gateway={GATEWAY_COMMIT}")
        print(f"llmi-auto-merge-sha256={llmi_sha}")

        _pytest(clon, "tests/search/falsificador_schema_lifecycle_v3.py")
        _pytest(clon, "tests/search/falsificador_native_lifecycle_v3.py")
        _pytest(
            clon,
            "tests/search/test_m2_6_acl_y_orden.py::"
            "test_TODA_conexion_de_servicio_instala_guard_y_udf")
        # El fichero completo evita seleccionar sólo los verdes conocidos. El nodeid
        # crítico se repite deliberadamente: si su nombre deriva o deja de recogerse,
        # pytest devuelve rojo en vez de omitir la prueba que cerró el NO-GO de v4.
        _pytest(clon, "tests/search/test_lifecycle_core_independiente.py")
        _pytest(
            clon,
            "tests/search/test_lifecycle_core_independiente.py::"
            "test_conexion_cierra_descriptor_si_falla_su_configuracion")

        # Control causal de la correctiva tuple-keyed: revive exactamente la proyección
        # obsoleta. El caso stale debe ponerse rojo; rc distinto de 1 es arnés roto.
        mutado = Path(d) / "stale-mutant"
        shutil.copytree(clon, mutado)
        objetivo = mutado / "tests/search/falsificador_schema_lifecycle_v3.py"
        fuente = objetivo.read_text()
        aguja = "    huellas_v1 = st._huellas_v1_legacy(st.ddl_esperado_v1())"
        reemplazo = (
            "    huellas_v1 = {n: hashlib.sha256(sql.encode()).hexdigest()\n"
            "                  for n, sql in sorted(st.ddl_esperado_v1().items())}")
        if fuente.count(aguja) != 1:
            raise RuntimeError("arnés stale roto: aguja correctiva no es única")
        objetivo.write_text(fuente.replace(aguja, reemplazo, 1))
        p = _run(
            [sys.executable, "-m", "pytest", "-q",
             "tests/search/falsificador_schema_lifecycle_v3.py::"
             "test_v1_stale_ordena_migrar_y_converge_migrate_rebuild_200"],
            cwd=mutado, check=False)
        if p.returncode != 1 or "tuple" not in (p.stdout + p.stderr):
            raise RuntimeError(
                f"mutante tuple-keyed no murió limpiamente (rc={p.returncode}):\n"
                + (p.stdout + p.stderr)[-4000:])
        print("stale-tuple-mutant: MUERTO")

        p = _run(
            [sys.executable, "tests/search/mutantes_lifecycle.py"], cwd=clon,
            check=False, timeout=240)
        if p.returncode != 0 or "lifecycle: 6/6 mutantes muertos" not in p.stdout:
            raise RuntimeError(
                "mutantes lifecycle no conservados:\n" + (p.stdout + p.stderr)[-6000:])
        print(p.stdout.strip())
    print("composición lifecycle v5: GO focal")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
