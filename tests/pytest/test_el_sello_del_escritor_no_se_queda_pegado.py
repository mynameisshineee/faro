"""Si un ledger revienta entre el sello y el commit, la marca queda y la próxima miente.

`_tomo_el_escritor` es idempotente a propósito —la primera escritura gana— y ese mismo
`setdefault` es lo que la vuelve peligrosa cuando la transacción NO llega al commit:
`barrido()` captura el fallo por ledger y hace `con.rollback()`, pero la marca sigue en
`_PRIMERA_ESCRITURA`. En el barrido siguiente, `setdefault` no la pisa y
`_solte_el_escritor` mide desde el intento ROTO de hace dos minutos.

O sea que el instrumento que puse hoy para dejar de medir mal, mide mal en cuanto algo
falla — y no de cualquier manera: hacia ARRIBA, que es la dirección que dispara alarmas
falsas. Un medidor que se dispara solo enseña a ignorar el medidor, que es peor que no
tenerlo.

Es la tercera cara de la misma clase en un día: medí la función en vez del lock, sellé
90 líneas tarde, y ahora el sello sobrevive a su propia transacción.
"""
from __future__ import annotations
import pathlib
import re
import time

import pytest


def test_TODA_operacion_que_sella_descarta_primero():
    """El ⊖ generalizado, y a propósito no sólo sobre `reindex`.

    La salvaguarda no puede vivir en la primitiva: `_tomo_el_escritor` es `setdefault` —la
    primera escritura gana— y eso es lo que tiene que ser DENTRO de una operación. Lo que
    define el límite es la OPERACIÓN, así que cada función que sella tiene que descartar al
    entrar. Fijarlo sólo para `reindex` dejaría al tercer llamador repetir el defecto
    exacto, y es el tipo de regresión que nadie ve porque el número sigue saliendo.
    """
    import ast
    src = pathlib.Path("servicio.py").read_text()
    arbol = ast.parse(src)
    fallos = []
    for n in ast.walk(arbol):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        cuerpo = ast.get_source_segment(src, n) or ""
        if "_tomo_el_escritor(" not in cuerpo or n.name == "_tomo_el_escritor":
            continue
        # LA CLAVE TIENE QUE SER LA MISMA. Antes bastaba con que apareciera un `.pop`:
        # `_PRIMERA_ESCRITURA.pop("otra-cosa", None)` pasaba el guarda y no descartaba
        # nada. Un falsador que comprueba que EXISTE una llamada, y no que haga lo suyo,
        # es la misma clase que persigue — lo cazó un barrido adversarial, no yo.
        sellos = re.findall(r"_tomo_el_escritor\(\s*([^),]+)", cuerpo)
        pops = re.findall(r"_PRIMERA_ESCRITURA\.pop\(\s*([^),]+)", cuerpo)
        if not pops:
            fallos.append(n.name); continue
        if cuerpo.index("_PRIMERA_ESCRITURA.pop") > cuerpo.index("_tomo_el_escritor("):
            fallos.append(f"{n.name} (descarta DESPUÉS de sellar)")
        elif set(sellos) - set(pops):
            fallos.append(f"{n.name} (descarta {pops} pero sella {sorted(set(sellos))})")
    assert not fallos, (
        f"sellan el escritor sin descartar primero: {fallos}. Si la transacción revienta "
        f"entre el sello y el commit, la marca sobrevive y la siguiente medida sale desde "
        f"el intento roto — hacia arriba, que es la dirección que dispara alarmas falsas")


def test_reindex_descarta_la_marca_al_empezar(servicio, tmp_path):
    """⊕ del mecanismo: que la marca se descarte al ENTRAR, no al salir. Descartarla al
    salir es justo lo que no ocurre cuando la salida es una excepción."""
    import pathlib
    t = pathlib.Path("servicio.py").read_text().split("\n")
    i = next(k for k, l in enumerate(t) if l.startswith("def reindex("))
    prim = next(k for k in range(i, i + 60) if "_PRIMERA_ESCRITURA.pop" in t[k])
    sello = next(k for k in range(i, len(t)) if "_tomo_el_escritor(ledger)" in t[k])
    assert prim < sello, (
        "reindex no descarta la marca antes de sellar: un fallo previo la deja viva")
