"""Un `limit` de 0 o negativo entraba en el SQL tal cual, y SQLite lo lee al revés
que la intuición: `LIMIT 0` es «nada» y `LIMIT -1` es «TODO, sin tope».

Las dos puntas hacen daño y las dos estaban abiertas:

  · `limit=0`  → `/inbox` contestaba «(nada nuevo para X)» CON correo sin leer. No es
    una lista vacía: es una FRASE que afirma que no hay nada. Y llega desde la flota,
    porque `llmi` pasa `"${2:-30}"` sin mirarlo (`llmi:857` y `llmi:931`), así que
    `llmi inbox backend 0` es la avería tecleable.

  · `limit=-1` → `/entries` se saltaba ENTEROS los dos topes que la ruta declara: el
    `le=500` del validador y el `min(limit, CUERPO_MAX_FILAS)` de los cuerpos
    (`capado = cuerpo and limit > CUERPO_MAX_FILAS` es falso con -1, y `min(-1,10)`
    es -1). MEDIDO el 2026-09-04 contra el índice vivo: 99.123 entradas, 55,3 MB de
    metadatos en UNA respuesta — la ruta cuyo comentario propio dice que 500 filas
    con cuerpo ya eran 6,2 MB / 1,5 M de tokens.

El arreglo es el que esta misma casa ya usaba en `/doctor` (`Query(7, ge=1, le=90)`):
declarar el suelo, no sólo el techo. Y va a las CINCO rutas con `le=`, no sólo a las
dos que duelen — es un defecto de clase y curar la mitad lo deja sembrado.
"""
from __future__ import annotations

import ast

import pytest


# ── ⊖ 1: la frase «nada nuevo» no puede salir habiendo correo ────────────────────
def test_limit_cero_no_puede_decir_que_no_hay_nada(cliente):
    # Primero se ESTABLECE que hay correo: sin este control, un 422 abajo probaría
    # sólo que el validador existe, no que tapaba algo.
    real = cliente.get("/inbox/backend", params={"only": "demo-ledger"})
    assert real.status_code == 200
    assert "nada nuevo" not in real.text, "el arnés no tiene correo que ocultar"

    r = cliente.get("/inbox/backend", params={"only": "demo-ledger", "limit": 0})
    assert r.status_code == 422, (
        f"limit=0 devolvió {r.status_code} con correo pendiente: {r.text[:120]!r}")
    assert "nada nuevo" not in r.text


@pytest.mark.parametrize("limite", [0, -1, -5])
def test_inbox_rechaza_el_suelo(cliente, limite):
    assert cliente.get("/inbox/backend", params={"limit": limite}).status_code == 422


# ── ⊖ 2: el negativo no puede saltarse el techo que la ruta declara ──────────────
def test_entries_negativo_no_se_salta_el_tope(cliente):
    # El techo declarado sigue vivo (⊕): pedir por encima de `le` es 422...
    assert cliente.get("/entries", params={"limit": 501}).status_code == 422
    # ...y el atajo por debajo de cero también, que era por donde se colaba.
    assert cliente.get("/entries", params={"limit": -1}).status_code == 422
    assert cliente.get("/entries", params={"limit": -1, "cuerpo": True}).status_code == 422


def test_pendientes_no_publica_un_recuento_negativo(cliente):
    # `mostradas: min(limite, len(pend))` con limite=-1 publicaba -1, y `pend[:-1]`
    # se comía la última fila. No es SQL: es un slice de Python, misma clase.
    r = cliente.get("/canon/pendientes", params={"limite": -1})
    assert r.status_code == 422, f"publicó {r.json().get('mostradas')!r} como recuento"


# ── ⊕: lo normal sigue funcionando, que es la mitad que un 422 barato rompería ──
def test_los_limites_de_siempre_siguen_sirviendo(cliente):
    assert cliente.get("/inbox/backend", params={"limit": 1}).status_code == 200
    assert cliente.get("/inbox/backend", params={"limit": 30}).status_code == 200
    assert cliente.get("/entries", params={"limit": 50}).status_code == 200
    assert cliente.get("/canon/pendientes", params={"limite": 40}).status_code == 200
    assert cliente.get("/lint", params={"limit": 10}).status_code == 200
    # Y la bandeja con `limit=1` enseña UNA, no cero ni todas.
    cuerpo = cliente.get("/inbox/backend", params={"only": "demo-ledger", "limit": 1}).text
    assert cuerpo.count("· REQUEST]") == 1, cuerpo[:400]


# ── LA CLASE, no los casos: ningún techo puede quedarse sin suelo ───────────────
def _techos_sin_suelo(fuente: str) -> list[str]:
    """Los `Query(...)` que declaran tope por arriba y ninguno por abajo.

    POR AST Y NO POR REGEX, y no es preferencia de estilo: la primera versión
    grepeaba `Query\\([^)]*\\)` y un paréntesis DENTRO de la llamada la partía en
    seco. Demostrado sobre este mismo texto —

        Query(50, description="tope (inclusive) de filas", le=200)

    — la regex captura hasta `(inclusive)`, se queda sin ver el `le=` y devuelve
    lista vacía: una ruta con techo y sin suelo entra EN VERDE. Lo cazó la lente de
    falsadores de la revisión triadversarial del 2026-09-04, y es la avería clásica
    de leer código como texto plano. El árbol no se deja engañar por lo que hay
    dentro de una cadena.
    """
    fuera = []
    for n in ast.walk(ast.parse(fuente)):
        if not isinstance(n, ast.Call):
            continue
        nombre = getattr(n.func, "id", None) or getattr(n.func, "attr", None)
        if nombre != "Query":
            continue
        claves = {k.arg for k in n.keywords}
        # `lt`/`gt` van al lado de `le`/`ge`: son la misma pareja techo/suelo con la
        # frontera abierta, y dejarlas fuera abriría el mismo agujero con otro nombre.
        if (claves & {"le", "lt"}) and not (claves & {"ge", "gt"}):
            fuera.append(f"servicio.py:{n.lineno}  {ast.unparse(n)}")
    return fuera


def test_el_detector_de_la_clase_ve_a_traves_de_un_parentesis():
    """⊕ DEL PROPIO DETECTOR, antes de creerle nada.

    Un guarda de clase que devuelve lista vacía se lee igual estando bien que
    estando ciego. Éste ejerce las dos caras sobre fuente sintética: la que debe
    señalar (incluida la forma que engañaba a la regex) y la que no.
    """
    assert _techos_sin_suelo("x = Query(50, le=200)"), "no ve el caso llano"
    assert _techos_sin_suelo(
        'x = Query(50, description="tope (inclusive) de filas", le=200)'), (
        "ciego al paréntesis interno — es exactamente el fallo de la regex anterior")
    assert _techos_sin_suelo("x = Query(50, lt=200)"), "no ve la frontera abierta"
    assert not _techos_sin_suelo("x = Query(50, ge=1, le=200)"), "falso positivo"
    assert not _techos_sin_suelo('x = Query("ts", pattern="^(ts|arrival)$")'), (
        "señala un Query sin techo numérico")


def test_ningun_query_declara_techo_sin_suelo():
    """Lo que de verdad se cierra aquí no son cinco rutas: es la forma.

    Los tres de arriba mueren si alguien quita el `ge` de SU ruta. Éste muere si
    alguien AÑADE una sexta ruta con `le=` y sin `ge=` — que es como llegaron las
    cinco. Sin él, la próxima entra en verde.

    Y no es decoración: medido por la auditoría de falsadores, es el ÚNICO que caza
    quitar el suelo a `/doctor` y a `/lint` (2 de las 5 rutas). Los demás tests de
    este fichero no las tocan.
    """
    sin_suelo = _techos_sin_suelo(open("servicio.py", encoding="utf-8").read())
    assert not sin_suelo, ("declaran techo y no suelo (`LIMIT 0` es «nada» y "
                           f"`LIMIT -1` es «todo»): {sin_suelo}")
