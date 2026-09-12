"""Fija el pie forma-E (commit 1174d5e3) en sus TRES estados, por AST.

Es la 2ª aserción que exige cto (#2335): el estado NUEVO que introduce el pie v2
(sin cabecera y sin carril derivable) queda fijado — la rama honesta SIGUE
CALLANDO. Si alguien la toca para que ofrezca un curl a ciegas, este test cae.

Mecánica: se ejecuta por AST la cola de la rama SIN identidad de la función del
pie (la que contiene «marcar leído»), envuelta en función sintética porque sus
`return` anidados no son legales en un Module pasado a exec(). Los placeholders
`<tu carril>` vivos en esa rama (servicio.py 6297-6298) son CORRECTOS (veredicto
cpo #2338): dicen qué cabecera añadir, no simulan un valor.
"""
import ast
import json
import re
from pathlib import Path

SERVICIO = Path(__file__).resolve().parents[2] / "servicio.py"

LEDGER_CARRIL = {
    "llminbox": "llminbox",
    "64bis": "64bis",
    "PM": "PM",
    "bikeus": "bikeus",
    "biklabs-landing": "biklabs-landing",
    "cfocockpit": "cfocockpit",
}


def _cola_del_pie():
    """Statements desde la 1ª asignación de `carril_pie` hasta el fin de la rama."""
    tree = ast.parse(SERVICIO.read_text(encoding="utf-8"))
    pie_fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            if any(
                isinstance(c, ast.Constant) and isinstance(c.value, str)
                and "marcar leído" in c.value
                for c in ast.walk(node)
            ):
                pie_fn = node
                break
    assert pie_fn is not None, "no se encontró la función del pie (marcar leído)"

    rama = None
    for node in ast.walk(pie_fn):
        if isinstance(node, ast.If) and ast.unparse(node.test) == "identity_scope is None":
            if any(
                isinstance(c, ast.Constant) and isinstance(c.value, str)
                and "marcar leído" in c.value
                for c in ast.walk(node)
            ):
                rama = node
                break
    assert rama is not None, "rama sin identidad del pie no encontrada"

    start = next(
        i
        for i, st in enumerate(rama.body)
        if isinstance(st, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "carril_pie" for t in st.targets)
    )
    return rama.body[start:]


def _produce(ns):
    """Ejecuta la cola como función sintética `__pie__()` y devuelve lo que retorna."""
    cola = _cola_del_pie() + ast.parse("raise AssertionError('pie sin return')").body
    fnsrc = ast.parse("def __pie__(): pass")
    fnsrc.body[0].body = cola
    ast.fix_missing_locations(fnsrc)
    g = dict(ns)
    exec(compile(fnsrc, "<pie>", "exec"), g)
    return g["__pie__"]()


def _ns(tope, carril_cabecera):
    return {
        "AVISO": "",
        "_aviso_alias_mudo": lambda a: "",
        "_agent_pedido": "backend",
        "agent": "backend",
        "out": ["### [x → backend · T] sello\n  cuerpo"],
        "lp": type("LP", (), {"canonico": staticmethod(lambda a: a.lower())})(),
        "json": json,
        "LEDGER_CARRIL": LEDGER_CARRIL,
        "x_llminbox_carril": carril_cabecera,
        "tope": tope,
    }


def test_con_cabecera_el_curl_lleva_ese_carril():
    salida = _produce(_ns({"llminbox": 2195}, "llminbox"))
    assert "curl" in salida
    assert "X-Llminbox-Carril: llminbox" in salida


def test_sin_cabecera_y_tope_unico_deriva_el_carril():
    salida = _produce(_ns({"64bis": 77}, None))
    assert "curl" in salida
    assert "X-Llminbox-Carril: 64bis" in salida


def test_estado_nuevo_sin_carril_derivable_calla():
    """La 2ª aserción de cto: la rama honesta NO ofrece nada pegable.

    Anclado a la FORMA del acto (cura de sdet #2501, falsada en su banco
    ⊕declina→pasa · ⊖pie-v1→cae): lo que no debe haber NO es la palabra
    «curl» — la prosa honesta la nombra para negarlo («NO imprimo el curl») —
    es una LÍNEA PEGABLE que empiece por `curl`.
    """
    salida = _produce(_ns({"llminbox": 2195, "64bis": 77}, None))
    assert not re.search(r"^\s*curl ", salida, re.M), (
        "la rama honesta imprimió un curl pegable: alguien la tocó"
    )
    assert "127.0.0.1:8077" not in salida, "la rama honesta filtró la URL del pie"
    assert "NO imprimo el curl" in salida, "debe explicar qué falta, no callar seco"
