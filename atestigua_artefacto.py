"""Atestado del ARTEFACTO, escrito DENTRO de la imagen en tiempo de build.

Por qué existe: hasta ahora la imagen no se autodescribía —`0` variables `LLMINBOX_*`
horneadas, `0` labels de build— así que una imagen suelta no podía decir qué era, y el
`/health` sólo publicaba un SHA `declarado` por quien desplegaba. Un tercero no podía
atar la evidencia a nada que viviera en el artefacto.

Y se atestigua **SQLite**, no sólo las versiones de pip, porque es la pieza cuyo cambio
NO se ve en `requirements.lock`: viene de la base del sistema. Una base nueva puede
traer otro SQLite —otra versión, otras opciones de compilación, sin FTS5— y el lock
seguiría idéntico. El journal de coordinación de M1 se apoya en esa librería; medirla
en el artefacto es lo único que convierte «reproducible» en una afirmación sobre lo que
de verdad corre.

⚠️ NADA DE SECRETOS. Este fichero se sirve por `/health`, que responde SIN token a
propósito. Sólo versiones, digests y opciones de compilación.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import platform
import sqlite3
import sys


def _vfs() -> dict:
    """El VFS por defecto, MEDIDO — o dicho como no medible. Nunca 'unix' a ojo.

    `sqlite3_vfs_find(NULL)` devuelve el VFS por defecto. Se busca en la librería
    dinámica y, si el módulo la lleva estáticamente enlazada, en el propio módulo.
    Si ninguna vía responde, se dice `null` con su motivo: inventar `"unix"` porque
    «es lo normal en Linux» sería convertir una expectativa en una medida.
    """
    import ctypes
    candidatos = ["libsqlite3.so.0", "libsqlite3.so"]
    try:
        import _sqlite3
        candidatos.append(_sqlite3.__file__)
    except Exception:
        pass
    for cand in candidatos:
        try:
            lib = ctypes.CDLL(cand)
            lib.sqlite3_vfs_find.restype = ctypes.c_void_p
            lib.sqlite3_vfs_find.argtypes = [ctypes.c_char_p]
            p = lib.sqlite3_vfs_find(None)
            if not p:
                continue
            # struct sqlite3_vfs: int iVersion; int szOsFile; int mxPathname;
            # sqlite3_vfs *pNext; const char *zName;  → zName es el 5º campo.
            clase = ctypes.c_char_p
            offset = ctypes.sizeof(ctypes.c_int) * 3
            offset += (-offset) % ctypes.sizeof(ctypes.c_void_p)      # alineación
            offset += ctypes.sizeof(ctypes.c_void_p)                  # pNext
            nombre = clase.from_buffer_copy(
                ctypes.string_at(p + offset, ctypes.sizeof(ctypes.c_void_p))).value
            if nombre:
                return {"nombre": nombre.decode(), "via": cand, "medido": True}
        except Exception:
            continue
    return {"nombre": None, "via": None, "medido": False,
            "motivo": "sqlite3_vfs_find no alcanzable desde este artefacto"}


def _fts5() -> dict:
    """FTS5 por EFECTO, no por leer `compile_options`. M2 se apoya en esto."""
    try:
        con = sqlite3.connect(":memory:")
        con.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        con.close()
        return {"disponible": True, "medido_por": "CREATE VIRTUAL TABLE"}
    except Exception as e:
        return {"disponible": False, "medido_por": "CREATE VIRTUAL TABLE",
                "error": type(e).__name__}


def _sha256(ruta: str) -> str | None:
    try:
        with open(ruta, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


# ── EL CIERRE DEL RUNNER, DECLARADO ──────────────────────────────────────────
# Esta lista era una tupla a mano DENTRO de `fuentes_sha256`, y por eso derivaba:
# ligaba `runtime_root.py` y `projector_runner.py` pero no `projector.py`, ni
# `observability.py`, ni `telemetry_bridge.py`, ni los tres `search_*`. Nada la
# comprobaba contra el grafo de imports real ⇒ dos imagenes con el mismo bloque
# atestado podian diferir en un modulo que si viaja y si corre.
#
# Sigue siendo EXPLICITA a proposito —un barrido a secas no sabe echar en falta lo
# que no esta— pero ahora tiene dos guardas encima:
#   · `tests/pytest/test_c4_atestado_runner.py` la contrasta contra el cierre AST
#     de los tres puntos de entrada del runner: no puede sub-declarar.
#   · el propio build (abajo, `__main__`) sale con rc=1 si un declarado falta en la
#     imagen: no puede atestiguar un modulo ausente como si estuviera.
ENTRYPOINTS_RUNNER = ("runtime_root", "projector", "projector_runner")

MODULOS_RUNNER = (
    "coordination.py",
    "ledger_parse.py",
    "native_gateway.py",
    "operator_admission.py",
    "observability.py",
    "projector.py",
    "projector_runner.py",
    "journal_projection_backend.py",
    "runtime_root.py",
    "search_contract.py",
    "search_cursor.py",
    "search_store.py",
    "servicio.py",
    "telemetry_bridge.py",
)

# Empaquetados que no son modulos del cierre pero si definen el artefacto.
FUENTES_EXTRA = ("atestigua_artefacto.py", "ui.html")


def _fuentes(base: str) -> dict[str, str | None]:
    """Digest de TODO lo empaquetado relevante, no de una lista que envejece.

    El barrido de `*.py` cierra la deriva por omision: cualquier modulo que alguien
    anada al COPY queda ligado sin tocar este fichero. La union con los declarados
    cierra la otra cara: un declarado que NO este sale con digest `None`, y eso es
    lo que el build lee para negarse.
    """
    nombres = set(MODULOS_RUNNER) | set(FUENTES_EXTRA)
    nombres |= {q.name for q in sorted(pathlib.Path(base).glob("*.py"))}
    return {n: _sha256(os.path.join(base, n)) for n in sorted(nombres)}


def _runner_empaquetado(fuentes: dict[str, str | None]) -> dict:
    """Que exige el runner, que falta. `completo=False` es motivo de build rojo."""
    faltan = sorted(n for n in MODULOS_RUNNER if fuentes.get(n) is None)
    return {
        "entrypoints": list(ENTRYPOINTS_RUNNER),
        "exigidos": list(MODULOS_RUNNER),
        "faltan": faltan,
        "completo": not faltan,
    }


def artefacto(base: str = "/app") -> dict:
    con = sqlite3.connect(":memory:")
    opciones = sorted(r[0] for r in con.execute("PRAGMA compile_options"))
    con.close()
    fuentes = _fuentes(base)
    import importlib.metadata as md
    paquetes = {}
    for nombre in ("fastapi", "uvicorn", "pydantic", "starlette"):
        try:
            paquetes[nombre] = md.version(nombre)
        except Exception:
            paquetes[nombre] = None
    return {
        "esquema": 1,
        "python": {"version": platform.python_version(),
                   "implementacion": platform.python_implementation()},
        "sqlite": {
            "version_biblioteca": sqlite3.sqlite_version,
            "threadsafety": sqlite3.threadsafety,
            "vfs": _vfs(),
            "fts5": _fts5(),
            "compile_options": opciones,
        },
        "paquetes": paquetes,
        # El lock por su digest: ata ESTA imagen al fichero exacto con el que se
        # resolvieron las dependencias, sin copiar su contenido a `/health`.
        "requirements_lock_sha256": _sha256(os.path.join(base, "requirements.lock")),
        # También se ligan los módulos que decidirán identidad, autoridad y
        # persistencia en el futuro root. Empaquetarlos sin atestiguarlos dejaría
        # que dos imágenes con el mismo bloque legacy aparentasen ser la misma.
        "fuentes_sha256": fuentes,
        # Y se DECLARA si el cierre del runner viaja entero. Un atestado que sólo
        # publica digests no distingue «este módulo no está» de «este módulo no se
        # miró»: `faltan` convierte esa ausencia en un dato que el build puede leer.
        "runner_empaquetado": _runner_empaquetado(fuentes),
    }


if __name__ == "__main__":
    salida = sys.argv[1] if len(sys.argv) > 1 else "/app/ARTEFACTO.json"
    # La base es el directorio en el que se escribe el atestado: se atestigua el
    # artefacto en el que uno vive, no una ruta cableada que podria describir otro.
    base = os.path.dirname(os.path.abspath(salida)) or "/app"
    datos = artefacto(base)
    empaquetado = datos["runner_empaquetado"]
    if not empaquetado["completo"]:
        # FAIL-CLOSED EN EL BUILD, no un aviso en `/health`. Una imagen a la que le
        # falta un modulo del runner no falla al construirse ni al arrancar: falla el
        # dia que alguien ejerce esa ruta, en produccion y sin testigo.
        print(json.dumps({"error": "cierre del runner incompleto",
                          "faltan": empaquetado["faltan"]}), file=sys.stderr)
        sys.exit(1)
    with open(salida, "w", encoding="utf-8") as fh:
        json.dump(datos, fh, indent=1, sort_keys=True)
    os.chmod(salida, 0o444)
    print(json.dumps({"sqlite": datos["sqlite"]["version_biblioteca"],
                      "vfs": datos["sqlite"]["vfs"]["nombre"],
                      "fts5": datos["sqlite"]["fts5"]["disponible"],
                      "runner_completo": empaquetado["completo"]}))
