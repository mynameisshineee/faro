#!/usr/bin/env python3
"""Fabrica «4 claims vivos + 1» y observa `excede_wip` SIN tocar la flota.

Lo pidió @qa para ejercitar la fila 2 de su matriz. `LLMINBOX_DEMO` NO sirve —crea un
ledger de EJEMPLO y vive dentro de `llmi init`, que en este repo es ⛔ absoluto— y una
segunda instancia tampoco: `llmi` no fija `COMPOSE_PROJECT_NAME`, así que dos contenedores
lanzados desde este directorio comparten el volumen `llminbox_llminbox-data`, o sea la
MISMA base. Serían dos puertas al mismo estado compartido, que es justo lo que se quiere
evitar.

Lo que sí aísla de verdad es el arnés que ya usan los tests: una base nueva en un temporal,
su propio censo, y nada montado. Se tira al terminar.

    .venv-test/bin/python tests/wip_aislado.py [tope]     # tope 4 por defecto

Cero efecto sobre la flota: no abre claims reales, no toca el contenedor, no escribe en
ningún ledger. Si algún día lo hiciera, este fichero estaría mintiendo en su primera línea.
"""
from __future__ import annotations
import os
import sys
import pathlib
import atexit
import shutil
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "pytest"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


class _Patch:
    """`construir()` espera el `monkeypatch` de pytest; fuera de pytest no existe."""
    def __init__(self): self._prev: list[tuple] = []
    def setenv(self, k, v): self._prev.append((k, os.environ.get(k))); os.environ[k] = v
    def delenv(self, k, raising=True): self._prev.append((k, os.environ.get(k))); os.environ.pop(k, None)
    def setattr(self, obj, n, v): setattr(obj, n, v)
    def undo(self):
        for k, v in reversed(self._prev):
            if v is None: os.environ.pop(k, None)
            else: os.environ[k] = v


def main() -> int:
    from conftest import construir
    from fastapi.testclient import TestClient

    tope = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    mp = _Patch()
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="wip-aislado-"))
    # SE BORRA DE VERDAD. El docstring decía «se tira al terminar» y no lo tiraba: cada
    # corrida dejaba una base SQLite entera en el temporal. Una promesa en la primera línea
    # de un fichero que dice ser aislado es justo la que nadie va a ir a comprobar.
    atexit.register(shutil.rmtree, tmp, True)
    mp.setenv("LLMINBOX_WIP_GLOBAL", str(tope))
    # El censo de prueba de la suite tiene 4 roles y aquí hacen falta `tope`+1: se SIEMBRA
    # uno a medida. Es legítimo porque la base es nueva y no la ve nadie — y es lo que
    # permite ejercitar la fila con el tope REAL del piloto en vez de con el que quepa.
    roster = {"agentes": [{"nombre": f"sonda-{i}", "humano": "prueba", "clave": ""}
                          for i in range(tope + 1)],
              "humanos": [{"nombre": "prueba", "alias": []}],
              "difusion": ["equipo"]}
    s = construir(tmp, mp, roster=roster)
    print(f"  base aislada: {os.environ['LLMINBOX_DB']}")
    assert "/tmp" in os.environ["LLMINBOX_DB"] or str(tmp) in os.environ["LLMINBOX_DB"], \
        "la base NO está aislada — abortando antes de escribir nada"

    with TestClient(s.app) as c:
        s.barrido()
        c.headers.update({"X-Llminbox-Token": "test-token"})
        # Cinco ROLES distintos: cuatro llenan el cupo global, el quinto lo excede. Roles y
        # no nombres, porque el WIP cuenta lo que la tabla guarda, que es el rol.
        por_rol: dict[str, str] = {}
        for n in sorted(s.lp.CANON):
            por_rol.setdefault(s.lp.rol_de(n), n)
        quienes = list(por_rol.values())[:tope + 1]
        if len(quienes) < tope + 1:
            print(f"  ⛔ sólo hay {len(quienes)} roles y hacen falta {tope + 1}")
            return 2
        for i, q in enumerate(quienes, 1):
            d = c.post("/claim", json={"tema": f"aislado-{i}", "agent": q}).json()
            w = d.get("wip") or {}
            print(f"  {i}. {q:22s} ok={d.get('ok')}  vivos={w.get('vivos')} "
                  f"tope={w.get('tope')} excede={w.get('excede')} modo={w.get('modo')}")
            if i == tope + 1:
                if not d.get("ok"):
                    print(f"  ⛔ el {tope + 1}º fue RECHAZADO: el WIP global NO está en warn")
                    return 1
                if w.get("excede") is not True:
                    print(f"  ⛔ el {tope + 1}º no marca `excede`: la fila no queda ejercitada")
                    return 1
                print(f"\n  ✅ EJERCITADA: {tope + 1} vivos con tope {tope}, `excede`=true, "
                      f"ACEPTADO (warn no rechaza)")
    mp.undo()
    return 0


if __name__ == "__main__":
    sys.exit(main())
