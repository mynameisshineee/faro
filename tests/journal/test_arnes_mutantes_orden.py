"""El arnés de mutantes tiene que poder CORRER. Falsador del orden de los grupos.

Un grupo `MUTANTES*` definido debajo del `if __name__ == "__main__"` importa sin
ruido y revienta en la ejecución directa con `NameError` dentro de `main()`: el
arnés no clona nada, no muta nada y no dice `VIVO` de nadie. **Un arnés que no
corre se lee igual que uno que no encontró nada**, y ése es el peor modo de
fallo de un instrumento de medida.

Este fichero vive en `tests/journal` porque el arnés es stdlib-only, igual que
el resto de esta carpeta, y así corre en un entorno pelado.
"""
from __future__ import annotations

import ast
import pathlib

from tests import mutantes_native_core as M

ARNES = pathlib.Path(M.__file__)


def test_ningun_grupo_de_mutantes_queda_debajo_del_guard_de_main():
    """⊖ sobre el fichero VIVO. Es el que caza la regresión."""
    assert M.grupos_despues_del_guard() == []


def test_el_detector_SI_ve_un_grupo_puesto_debajo_del_guard():
    """⊕ obligatorio: sin él, un detector que devolviera `[]` siempre —un typo en
    el prefijo, un `ast.Assign` que ya no es `Assign`— daría verde el test de
    arriba sin mirar nada."""
    fuente = ARNES.read_text() + "\n\nMUTANTES_INVENTADO = []\n"
    assert M.grupos_despues_del_guard(fuente) == ["MUTANTES_INVENTADO"]


def test_el_detector_NO_acusa_a_un_grupo_puesto_ENCIMA_del_guard():
    """El complemento del ⊕: la población que NO debe cumplir. Sin esto, un
    detector que acusara a CUALQUIER `MUTANTES*` pasaría los dos anteriores."""
    fuente = ARNES.read_text()
    guard = '\nif __name__ == "__main__":'
    assert fuente.count(guard) == 1
    i = fuente.index(guard)
    movido = fuente[:i] + "\nMUTANTES_ARRIBA = []\n" + fuente[i:]
    assert M.grupos_despues_del_guard(movido) == []


def test_sin_guard_el_detector_lo_DICE_en_vez_de_dar_verde():
    """Fail-closed: un fichero sin `if __name__` no tiene «grupos debajo», y
    devolver `[]` ahí sería un verde por ausencia de sujeto."""
    fuente = ARNES.read_text().split('\nif __name__ == "__main__":')[0]
    assert M.grupos_despues_del_guard(fuente) != []


def test_todos_los_grupos_declarados_entran_en_la_corrida_de_main():
    """El otro modo de que un grupo no mida: estar bien colocado y no aparecer
    en la concatenación que `main()` recorre. También importa sin ruido, también
    sale del informe sin una línea, y ahí ni el `NameError` avisa.
    """
    arbol = ast.parse(ARNES.read_text())
    declarados = {t.id for n in arbol.body if isinstance(n, ast.Assign)
                  for t in n.targets
                  if isinstance(t, ast.Name) and t.id.startswith("MUTANTES")}
    # Los nombres de la concatenación se sacan por AST y NO por `in` sobre el
    # texto: `"MUTANTES_O" in "…MUTANTES_OC…"` es `True`, así que un grupo cuyo
    # nombre sea PREFIJO de otro que sí está pasaría el control sin correrse.
    # Es la misma trampa de prefijo que `suite_de` documenta para el enrutado.
    recorridos = set()
    for n in ast.walk(arbol):
        if isinstance(n, ast.For) and isinstance(n.target, ast.Tuple) and [
                e.id for e in n.target.elts if isinstance(e, ast.Name)] == [
                "mid", "fich", "ancla", "nuevo", "deben_caer"]:
            recorridos = {x.id for x in ast.walk(n.iter) if isinstance(x, ast.Name)}
    assert recorridos, "no encontré el bucle de mutantes de `main()`"
    faltan = sorted(g for g in declarados if g not in recorridos)
    assert faltan == [], f"grupos declarados y NO recorridos por main(): {faltan}"
    assert len(declarados) >= 14                       # ⊕ el censo no salió vacío
