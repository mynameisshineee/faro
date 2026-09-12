"""El normalizador de recursos del journal y `tema_norm` de `/claim` son DOS
implementaciones, y este fichero es lo único que impide que diverjan.

Por qué hay dos y no una: `coordination.py` es stdlib-only y no importa
`servicio` (regla de capas, y es lo que lo hace desplegable solo); `tema_norm`
vive en `servicio.py`, que importa fastapi. Así que el journal recibe la función
INYECTADA y el arnés de `tests/journal` —que también corre en un entorno
pelado— tiene su propia copia.

🔑 Una copia que nadie ata es una bomba de relojería: el día que `tema_norm`
cambie, `/claim` y los leases tendrán espacios de nombres distintos y la
compatibilidad `/claim`→lease ABRIRÁ el defecto que la exclusión cierra —
`Deploy` y `deploy` volverían a ser dos dueños. Este test vive en
`tests/pytest` justo porque aquí sí se puede importar `servicio`.
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "tests/journal")


CORPUS = [
    "deploy", "Deploy", "DEPLOY", "Deploy X", "deploy-x", "deploy_x",
    "DEPLOY  X", "déploy x", "Escrow Freeze", "escrow_freeze",
    "  espacios  ", "---", "", "a" * 200, "ñandú", "café/té",
    "tab\there", "salto\nlinea", "PR #41", "v0.9-integración",
    "emoji🚀recurso", "punto.medio", "guion—largo", "MiXeD_CaSe-42",
]


def test_las_dos_normalizaciones_coinciden_en_todo_el_corpus():
    from servicio import tema_norm
    from _arnes import _normaliza_recurso

    divergen = [(x, tema_norm(x), _normaliza_recurso(x))
                for x in CORPUS if tema_norm(x) != _normaliza_recurso(x)]
    assert not divergen, (
        f"el normalizador del journal y `tema_norm` de `/claim` divergen en "
        f"{len(divergen)} de {len(CORPUS)}: {divergen}. Con espacios de nombres "
        f"distintos, `/claim` y los leases pueden dar DOS dueños al mismo trabajo")


def test_OMEGA_el_corpus_DISCRIMINA():
    """⊖ del anterior: si el corpus fuera todo cadenas ya normalizadas, las dos
    funciones coincidirían por vacuidad y este fichero no mediría nada."""
    from servicio import tema_norm

    cambian = [x for x in CORPUS if tema_norm(x) != x]
    assert len(cambian) >= 10, (
        f"sólo {len(cambian)} de {len(CORPUS)} entradas cambian al normalizar: "
        f"el corpus no ejercita la normalización")
