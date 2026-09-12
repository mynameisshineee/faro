"""Una entrada mal escrita en `LLMINBOX_LEDGERS` desaparecía sin dejar rastro.

    LEDGERS = {k: v for k, v in (p.split("=", 1)
               for p in os.environ.get("LLMINBOX_LEDGERS", "").split(",") if "=" in p)}

El `if "=" in p` descarta en silencio. Y no cae en `ROTOS`: `ROTOS` recoge ledgers
CONFIGURADOS que no aparecen en disco, y éste no llegó a configurarse. Así que un typo
—una coma de más, un `:` en vez de `=`— borra una bandeja entera de la vigilancia y
`/health` sigue diciendo `ok` con un `ledgers` más bajo que nadie compara contra nada.

Es la misma clase del día por partida doble: EL FILTRO RESPONDE Y NO EL CORPUS (lo
descartado no se cuenta), y CONFIGURADO-Y-ROTO SE LEE COMO NO-CONFIGURADO.

Vacío sigue siendo legítimo y silencioso: significa «sin ledgers».
"""
from __future__ import annotations
import importlib

import pytest


def _parse(valor):
    """Se prueba la FUNCIÓN y no el arranque entero: el arnés de `construir` fija su propio
    `LLMINBOX_LEDGERS` después del monkeypatch, así que un test por el entorno estaría
    midiendo el valor del arnés y no el mío. Ya me pasó con `RAIZ_GIT`."""
    import servicio
    return servicio._ledgers_env(valor)


def test_una_entrada_sin_igual_no_se_traga():
    """⊖ el que importa: el typo tiene que PARAR, no encogerse."""
    with pytest.raises(SystemExit) as e:
        _parse("uno=/a/b,esto-no-lleva-igual,dos=/c/d")
    assert "LLMINBOX_LEDGERS" in str(e.value)
    assert "esto-no-lleva-igual" in str(e.value), (
        "el error no dice CUÁL entrada está mal: obliga a adivinar entre las que haya")


def test_los_dos_puntos_en_vez_del_igual_tampoco():
    """El separador equivocado es el typo más probable y el que menos se ve."""
    with pytest.raises(SystemExit):
        _parse("uno=/a/b,dos:/c/d")


def test_vacio_sigue_siendo_legitimo():
    """⊕ obligatorio: ausente ⇒ sin ledgers, callando. Una validación que rechace el vacío
    convertiría un arranque legítimo en un fallo."""
    assert _parse("") == {}


def test_las_comas_sueltas_no_cuentan_como_entrada():
    """⊕ de borde: `a=/x,` y `a=/x, b=/y` son escrituras normales. Un separador final o un
    espacio no son un typo y no pueden parar el arranque, o la guarda es peor que el fallo
    que cura."""
    assert set(_parse("uno=/a/b, dos=/c/d,")) == {"uno", "dos"}
