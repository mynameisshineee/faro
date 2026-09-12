"""Piezas compartidas por los falsadores de la envolvente v1.0 (G10 · scale).

VERIFICADO ANTES DE CONSTRUIR (paso 0): las dos lanes con LEDGER LITERAL
COLISIONANTE que pide el execution doc de v1.0 ("two deliberately conflicting
lanes") ya existen en `tests/journal/_arnes.LANES` — `carril-uno` y
`carril-dos` mapean AMBAS al literal `"l"`. No se inventa un mapa nuevo: se
reutiliza el mismo que ya prueba G1-G6, para que un falsador de escala no
mida un carril de juguete que producción no tiene.

Y el cursor cross-lane YA tiene falsador estático en
`tests/search/test_cursor_y_keyset.py::test_cursor_atado_a_cada_filtro`
(caso `{"lane": CARRIL_HERMANO}`). Los falsadores de este paquete NO lo
repiten: miden la MISMA propiedad bajo escritura concurrente y a escala, que
es el hueco que el execution doc de v1.0 nombra y que ese test estático no
cubre.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

from tests.journal._arnes import (  # noqa: F401  (reexportado para los tests)
    CAPS_RUNTIME, GRAMATICA, INTENT, LANES, PEPPER, censo, journal, sesion,
    sesiones)

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(os.path.dirname(AQUI))

# Las DOS lanes colisionantes del execution doc de v1.0: mismo ledger literal
# ("l") y aquí además el mismo nombre de RECURSO de lease/fencing ("deploy").
CARRIL_A = "carril-uno"
CARRIL_B = "carril-dos"
LEDGER_COLISIONANTE = "l"
RECURSO_COLISIONANTE = "deploy"


def env_worker() -> dict:
    return {**os.environ, "LLMINBOX_RAIZ": RAIZ, "LLMINBOX_PEPPER": PEPPER.decode()}


def lanza(script: str, argv: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess:
    """UN proceso, bloqueante. Para el falsador de recovery: hace falta ver el
    `returncode` real de una caída, y `Popen`+`communicate` con timeout ya
    hace eso — `run` es sólo el azúcar de ese mismo camino."""
    return subprocess.run(
        [sys.executable, os.path.join(AQUI, script), *argv],
        capture_output=True, text=True, timeout=timeout, env=env_worker())


def lanza_paralelo(script: str, argvs: list[list[str]], *,
                   timeout: int = 90) -> list[dict]:
    """N PROCESOS reales, lanzados TODOS antes de esperar a ninguno.

    Procesos y no hilos, por el motivo que ya declara
    `tests/journal/test_concurrencia_20_procesos.py`: la exclusión que estos
    falsadores miden (fencing, claim del outbox) es entre CONEXIONES de
    SQLite, y un hilo puede colarse por el GIL en vez de por `BEGIN
    IMMEDIATE`.
    """
    env = env_worker()
    procs = [
        subprocess.Popen([sys.executable, os.path.join(AQUI, script), *argv],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, env=env)
        for argv in argvs
    ]
    salidas = []
    for p in procs:
        out, err = p.communicate(timeout=timeout)
        salidas.append({"returncode": p.returncode, "stdout": out, "stderr": err})
    return salidas


def ultima_linea_json(texto: str, *, stderr: str = "") -> dict:
    lineas = [l for l in texto.strip().splitlines() if l.strip()]
    if not lineas:
        raise AssertionError(f"el worker no imprimió nada por stdout; stderr:\n{stderr}")
    return json.loads(lineas[-1])
