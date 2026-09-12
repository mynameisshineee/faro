"""La frontera es `= ?` exacta en SQL. El `MATCH` es selectividad y nunca frontera."""

import pytest

from .conftest import ACTOR, CARRIL, ACTORES_HERMANOS, HERMANOS, LANE
import search_contract as sc
import search_store as ss


def test_carriles_hermanos_no_se_cuelan(poblado):
    r = poblado.search(lane=CARRIL, ledger=LANE, query="born red", limit=50)
    carriles = {f["ledger"] for f in r["filas"]}
    assert carriles == {LANE}, f"se colaron carriles ajenos: {carriles - {LANE}}"
    assert len(r["filas"]) == 9, f"población inesperada: {len(r['filas'])}/9"


def test_falsador_born_red_quitar_la_frontera_SI_filtra_hermanos(poblado):
    """⊖ El mutante que importa: quitar `AND e.ledger=?` dejando el `MATCH`.

    Si esto NO devolviera filas ajenas, el test de arriba estaría pasando por el corpus y
    no por la frontera — sería un verde que no mide. Se ejecuta el mutante sobre la MISMA
    sentencia de producción para que no pueda divergir de ella.
    """
    mutante = ss.SQL_BUSQUEDA.replace("   AND d.ledger = ?\n", "") \
                             .replace("   AND e.ledger = ?\n", "")
    expr, _ = sc.compile_query("born red")
    # El mutante quita LOS DOS `ledger`, así que lleva 2 parámetros menos que el original.
    filas = poblado.con.execute(mutante, (
        expr, CARRIL, None, None, None, None, None, None, None, None,
        0, 0, "", 100)).fetchall()
    ajenos = {f["ledger"] for f in filas} - {LANE}
    assert ajenos == set(HERMANOS), (
        f"el mutante NO filtró carriles ajenos ({ajenos}): el corpus no discrimina y el "
        f"test de la frontera estaría verde sin probar nada")


def test_actores_hermanos_no_se_cuelan(poblado):
    r = poblado.search(lane=CARRIL, ledger=LANE, query="born red", limit=50, actor=ACTOR)
    actores = {f["actor"] for f in r["filas"]}
    assert actores == {ACTOR}, f"actores ajenos: {actores - {ACTOR}}"
    todos = poblado.search(lane=CARRIL, ledger=LANE, query="born red", limit=50)
    assert {f["actor"] for f in todos["filas"]} == {ACTOR, *ACTORES_HERMANOS}, (
        "sin los actores hermanos en el corpus, el filtro exacto no discrimina")


@pytest.mark.parametrize("crudo", [
    'foo"', 'AND', '*', '((((a', 'a NOT b', 'noexiste:foo',
    'NEAR("el" "de", 2147483647)', 'ledger:"64bis-wiki" AND secreto',
    '" OR 1=1 --', "'; DROP TABLE entries; --", 'body:*',
])
def test_la_entrada_cruda_no_llega_al_motor(poblado, crudo):
    """Ninguna entrada revienta el motor ni ejecuta álgebra que nadie ofreció.

    ⊖ implícito y medido por `qa`: reenviada CRUDA, 5 de 7 de estas cadenas dan
    `OperationalError` (que sube como 500 y el llamante lee como «sin resultados»), y
    `a NOT b` se ACEPTA como booleano.
    """
    try:
        r = poblado.search(lane=CARRIL, ledger=LANE, query=crudo, limit=5)
    except sc.SearchContractError as e:
        assert e.code in ("NO_SEARCHABLE_TERMS", "TOO_MANY_TERMS", "TERM_TOO_LONG",
                          "QUERY_TOO_LONG"), e.code
        return
    # `⊆ {LANE}` sobre una lista VACÍA es cierto siempre: la aserción que importa es que
    # la consulta se ejecutó y devolvió una estructura utilizable, y que si trajo filas,
    # todas son del carril. Sin la primera mitad, este test daba verde con `0` filas.
    assert isinstance(r["filas"], list) and r["truncado"]["servidas"] == len(r["filas"])
    if r["filas"]:
        assert {f["ledger"] for f in r["filas"]} == {LANE}


def test_el_usuario_no_puede_nombrar_la_columna_del_carril():
    expr, terminos = sc.compile_query('ledger:"64bis-wiki" AND secreto')
    assert "ledger:" not in expr and ":" not in expr
    assert expr == '"ledger" AND "64bis" AND "wiki" AND "and" AND "secreto"', expr


def test_not_es_termino_literal_no_operador():
    expr, terminos = sc.compile_query("a NOT b")
    assert terminos == ["a", "not", "b"]
    assert expr == '"a" AND "not" AND "b"'


def test_las_tablas_siguen_ahi_tras_la_inyeccion(poblado):
    # ⊕ el corpus responde a una consulta legítima ANTES: si `entries` estuviera ya vacía,
    # el recuento de después no probaría nada.
    assert poblado.search(lane=CARRIL, ledger=LANE, query="born red", limit=5)["filas"]
    poblado.search(lane=CARRIL, ledger=LANE, query="'; DROP TABLE entries; --", limit=5)
    assert poblado.con.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 21


# ── M29/M30: dos cláusulas del `WHERE` que NINGÚN mutante discriminaba ────────────
# El censo de mutantes tenía un hueco de numeración (M01–M70 con 68 entradas, sin M29 ni
# M30, y sin rastro de ellos en historial ni en docs). Se reconstruyen sobre invariantes
# reales y no cubiertos, no rellenando números: los dos mutantes SOBREVIVÍAN a la suite
# entera antes de estos tests — medido, no supuesto.

def test_una_entrada_sin_posicion_no_se_sirve(poblado):
    """`arrival` es la CLAVE DE ORDEN y viaja dentro del cursor. Una entrada sin ella no
    tiene sitio en el keyset `(arrival, eid)`: servirla mete un `NULL` en la clave con la
    que se pagina, y la página siguiente se calcula contra un valor que no ordena.

    Distinto de `ausente`: aquélla es una entrada RETIRADA; ésta es una entrada que aún no
    ha sido POSICIONADA por el indexador.

    Se meten DOS entradas GEMELAS —mismo carril, mismo actor, mismo `ts`, mismo cuerpo— y
    la ÚNICA diferencia es el `arrival`. El control posicionado es lo que convierte la
    ausencia de la otra en una medida: sin él, «no sale» podría ser cualquier cosa.
    """
    con = poblado.con
    for eid, arrival in (("gemela-posicionada", 800), ("gemela-sin-posicion", None)):
        con.execute(
            "INSERT INTO entries(ledger,eid,arrival,ts,actor,tipo,head,body)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (LANE, eid, arrival, "2026-09-05", ACTOR, "FYI",
             f"titular {eid}", f"titular {eid}\nborn red comun"))
    con.commit()

    # ⊕ CONTROL 1: LAS DOS están en el índice FTS. Sin esto, la ausencia de la segunda
    # podría ser porque nunca entró —y el test daría verde con la guarda quitada.
    indexadas = {r["eid"] for r in con.execute(
        "SELECT e.eid FROM search_documents d"
        " JOIN entries e ON e.ledger=d.ledger AND e.eid=d.eid"
        " JOIN search_fts f ON f.rowid = d.rid"
        " WHERE f.search_fts MATCH ? AND e.ledger = ?",
        ("comun", LANE))}
    assert {"gemela-posicionada", "gemela-sin-posicion"} <= indexadas, (
        f"el corpus no discrimina: falta alguna gemela en el índice ({indexadas})")

    r = poblado.search(lane=CARRIL, ledger=LANE, query="born red", limit=50)
    servidas = {f["eid"] for f in r["filas"]}
    # ⊕ CONTROL 2: la POSICIONADA sí se sirve. La diferencia entre las dos es `arrival`
    # y nada más.
    assert "gemela-posicionada" in servidas
    assert "gemela-sin-posicion" not in servidas

    # ⊖ FALSADOR: sin `AND e.arrival IS NOT NULL` la fila SÍ sale. Se ejecuta sobre la
    # MISMA sentencia de producción para que el mutante no pueda divergir de ella.
    mutante = ss.SQL_BUSQUEDA.replace("   AND e.arrival IS NOT NULL\n", "")
    assert mutante != ss.SQL_BUSQUEDA
    expr, _ = sc.compile_query("born red")
    filas = con.execute(mutante, (expr, CARRIL, LANE, LANE, None, None, None, None,
                                  None, None, None, None, 0, 0, "", 100)).fetchall()
    assert "gemela-sin-posicion" in {f["eid"] for f in filas}, (
        "quitar la guarda NO cambia el resultado: el corpus no discrimina y el test de "
        "arriba estaría verde sin probar nada")


def test_la_ventana_de_fechas_es_el_intervalo_semiabierto(poblado):
    """`[desde, hasta)`: `desde` INCLUSIVO, `hasta` EXCLUSIVO.

    No es un detalle de gusto: es lo que hace que dos ventanas contiguas —`[a,b)` y
    `[b,c)`— PARTICIONEN el corpus. Con `hasta` inclusivo la entrada del borde sale en
    LAS DOS, y quien recorre el histórico por tramos la cuenta dos veces sin que nada
    falle ni avise.

    Se acreditan los TRES lados del borde (`<`, `==`, `>`) con un control SIN filtro que
    demuestra que las tres están en el corpus.
    """
    con = poblado.con
    ANTES, BORDE, DESPUES = "2026-01-01", "2026-01-02", "2026-01-03"
    for i, ts in enumerate((ANTES, BORDE, DESPUES)):
        con.execute(
            "INSERT INTO entries(ledger,eid,arrival,ts,actor,tipo,head,body)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (LANE, f"borde-{ts}", 900 + i, ts, ACTOR, "FYI",
             f"titular borde-{ts}", f"titular borde-{ts}\nborn red comun"))
    con.commit()
    tres = {f"borde-{ANTES}", f"borde-{BORDE}", f"borde-{DESPUES}"}

    def eids(**kw):
        return {f["eid"] for f in poblado.search(
            lane=CARRIL, ledger=LANE, query="born red", limit=50, **kw)["filas"]}

    # ⊕ CONTROL SIN FILTRO: las tres están y se sirven. Cualquier ausencia de abajo es,
    # por tanto, del filtro y no del corpus.
    assert tres <= eids(), "el corpus no trae las tres entradas del borde"

    # `hasta` EXCLUSIVO: `ts < hasta` entra, `ts == hasta` NO, `ts > hasta` NO.
    con_hasta = eids(hasta=BORDE)
    assert f"borde-{ANTES}" in con_hasta                       # ts <  hasta
    assert f"borde-{BORDE}" not in con_hasta                   # ts == hasta  ← el borde
    assert f"borde-{DESPUES}" not in con_hasta                 # ts >  hasta

    # `desde` INCLUSIVO: `ts == desde` entra. Es la otra mitad de la partición.
    con_desde = eids(desde=BORDE)
    assert f"borde-{ANTES}" not in con_desde                   # ts <  desde
    assert f"borde-{BORDE}" in con_desde                       # ts == desde ← el borde
    assert f"borde-{DESPUES}" in con_desde                     # ts >  desde

    # Y LA PROPIEDAD QUE IMPORTA: dos ventanas contiguas particionan, sin solape.
    izq, der = eids(desde=ANTES, hasta=BORDE), eids(desde=BORDE, hasta=DESPUES)
    assert izq & der == set(), f"las ventanas contiguas se solapan en {izq & der}"
    assert f"borde-{BORDE}" in der and f"borde-{BORDE}" not in izq

    # ⊖ FALSADOR: con `<=` la fila del borde SÍ sale por arriba, y las dos ventanas
    # contiguas dejan de particionar.
    mutante = ss.SQL_BUSQUEDA.replace("   AND (? IS NULL OR e.ts    < ?)\n",
                                      "   AND (? IS NULL OR e.ts   <= ?)\n")
    assert mutante != ss.SQL_BUSQUEDA
    expr, _ = sc.compile_query("born red")
    filas = con.execute(mutante, (expr, CARRIL, LANE, LANE, None, None, None, None,
                                  ANTES, ANTES, BORDE, BORDE,
                                  0, 0, "", 100)).fetchall()
    assert f"borde-{BORDE}" in {f["eid"] for f in filas}, (
        "hacer inclusivo el `hasta` NO cambia el resultado: el corpus no discrimina")
