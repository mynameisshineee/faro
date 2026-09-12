#!/usr/bin/env python3
"""Arnés mínimo de mutación para la frontera lifecycle de Search.

Cada mutante nace de un snapshot temporal y sólo cuenta como muerto si pytest devuelve
exactamente 1. Errores de colección, timeout o aguja ambigua son fallo del arnés.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
FOCAL = "tests/search/test_lifecycle_core_independiente.py"


@dataclasses.dataclass(frozen=True)
class Mutante:
    nombre: str
    fichero: str
    viejo: str
    nuevo: str
    test: str


MUTANTES = (
    Mutante(
        "L01-ignora-ready-false", "servicio.py",
        '        if _estado_search not in (None, "ready"):',
        "        if False:",
        "test_objeto_search_roto_real_degrada_emite_sensor_y_core_acepta"),
    Mutante(
        "L02-cualquier-schema-viejo-parece-migrable", "servicio.py",
        '            return "stale" if (version, version_codigo) == (1, 2) else "corrupt"',
        '            return "stale"',
        "test_solo_el_arco_schema_1_a_2_se_clasifica_como_stale_migrable"),
    Mutante(
        "L03-migrate-no-ejecuta", "servicio.py",
        "            generacion = migrar()",
        '            generacion = "0" * 32',
        "test_operacion_migrate_despacha_arco_explicito_y_devuelve_json_estable"),
    Mutante(
        "L04-admin-no-cierra", "servicio.py",
        "    finally:\n        if con is not None:\n            con.close()\n"
        "    salida = {\"ok\": True, \"operation\": operacion, \"state\": \"ready\",",
        "    finally:\n        pass\n"
        "    salida = {\"ok\": True, \"operation\": operacion, \"state\": \"ready\",",
        "test_operacion_migrate_cierra_conexion_si_el_dominio_rechaza"),
    Mutante(
        "L05-cli-pierde-subcomando", "llmi",
        '          docker exec "$LLMINBOX_NAME" python3 -m servicio search "$@" ;;',
        '          docker exec "$LLMINBOX_NAME" python3 -m servicio search rebuild ;;',
        "test_hunk_portable_llmi_deja_operaciones_search_alcanzables"),
    Mutante(
        "L06-lifespan-reconstruye", "servicio.py",
        "        _estado_search = _sonda_busqueda_publica()",
        '        _estado_search = preparar_busqueda_publica('
        'db(), reconstruir=True)["state"]',
        "test_search_degradado_no_impide_el_siguiente_evento_y_su_recibo"),
)


def _copia(destino: Path) -> None:
    shutil.copytree(
        ROOT, destino,
        ignore=shutil.ignore_patterns(".git", ".pytest_cache", "__pycache__", "*.pyc"))


def main() -> int:
    # Preflight global antes de gastar procesos: toda aguja debe ser exacta y única.
    for mutante in MUTANTES:
        fuente = (ROOT / mutante.fichero).read_text()
        n = fuente.count(mutante.viejo)
        if n != 1:
            print(f"ARNÉS ROTO {mutante.nombre}: aguja aparece {n} veces", file=sys.stderr)
            return 2

    with tempfile.TemporaryDirectory(prefix="llminbox-mut-lifecycle-") as base:
        for indice, mutante in enumerate(MUTANTES, start=1):
            clon = Path(base) / f"m{indice:02d}"
            _copia(clon)
            objetivo = clon / mutante.fichero
            fuente = objetivo.read_text()
            objetivo.write_text(fuente.replace(mutante.viejo, mutante.nuevo, 1))
            nodeid = f"{FOCAL}::{mutante.test}"
            corrida = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", nodeid], cwd=clon,
                capture_output=True, text=True, timeout=30)
            if corrida.returncode != 1:
                print(f"{mutante.nombre}: NO ACREDITADO rc={corrida.returncode}",
                      file=sys.stderr)
                print((corrida.stdout + corrida.stderr)[-3000:], file=sys.stderr)
                return 1
            print(f"{mutante.nombre}: MUERTO por {nodeid}")
    print(f"lifecycle: {len(MUTANTES)}/{len(MUTANTES)} mutantes muertos")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
