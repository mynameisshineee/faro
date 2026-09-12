"""El censo de conformidad que consume el shadow del canon operativo, pedido por @harness.

Sirve la línea base de «entregas con recibo completo» como DATO consultable en vez de un
log que haya que grepear. Lo mide hoy: 6.633 PRODUCED vivos, 0 con los 9 campos.

LO QUE ESTE ENDPOINT TIENE QUE DECIR DE SÍ MISMO, y es la mitad del diseño: una búsqueda
por palabra mide la REDACCIÓN, no el contrato. `commit` aparece en «pendiente de commit»
igual que en `commit: abc123`, y el primero no es un campo. Si el censo publicara un solo
número, ese número diría 54% de conformidad en `commit` donde el contrato dice otra cosa,
y el shadow arrancaría con una línea base inflada.

Así que se publican DOS medidas por campo y se nombra cada una:
  · `mencion`  — la palabra aparece en algún sitio. GENEROSA: cota SUPERIOR.
  · `campo`    — aparece como `campo:` / `**campo:**` / `- campo:`. Es la forma de un
                 recibo, y es la que cuenta para conformidad.
El hueco entre las dos no es ruido: es cuánta de la conformidad aparente es prosa.
"""
from __future__ import annotations
import sqlite3
import os
import pytest


@pytest.fixture
def con_recibos(cliente, servicio):
    """Planta LA TRAMPA que el censo tiene que resistir, porque sin datos el test no
    puede fallar: mi primer intento pasó con la mutación puesta —`completos` contando
    menciones en vez de campos— justo porque la base estaba vacía y 0 <= 0.

    Las tres entradas son, a propósito:
      · PROSA PURA: nombra los nueve campos en texto corrido. Un censo que cuente
        menciones la dará por conforme. NO lo es: no hay un solo `campo:`.
      · RECIBO REAL: los nueve como campo. Es el único conforme.
      · A MEDIAS: ocho campos y el noveno sólo mencionado. Caza el `>=8` por descuido.
    """
    prosa = ("hablo del owner y del repo y de la rama, del commit y del artefacto, "
             "del falsador y del revisor, del gate y de la integration — todo en prosa")
    recibo = "\n".join(f"{c}: valor" for c in ("owner", "repo", "rama", "commit",
                                               "artefacto", "falsador", "revisor",
                                               "gate", "integration"))
    medias = "\n".join(f"{c}: valor" for c in ("owner", "repo", "rama", "commit",
                                               "artefacto", "falsador", "revisor",
                                               "gate")) + "\nhablo de la integration"
    con = sqlite3.connect(os.environ["LLMINBOX_DB"])
    for i, (actor, cuerpo) in enumerate([("prosista", prosa), ("cumplidor", recibo),
                                         ("casi", medias)]):
        con.execute(
            "INSERT OR REPLACE INTO entries (ledger,eid,arrival,seq,line_no,byte_off,"
            "ts,actor,tipo,head,body,visto,ausente,provisional,raw_tipo) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("l", f"eid{i}", 900 + i, i, i, i, "2026-09-01T00:00:00Z", actor,
             "PRODUCED", "cabecera", cuerpo, None, None, 0, "PRODUCED"))
    con.commit(); con.close()
    return cliente


def test_el_censo_separa_mencion_de_campo(con_recibos):
    """⊖ el que importa: si las dos medidas colapsan en una, el censo miente hacia arriba."""
    r = con_recibos.get("/recibos/censo")
    assert r.status_code == 200, r.text
    d = r.json()
    assert "campos" in d, d
    for c, v in d["campos"].items():
        assert "mencion" in v and "campo" in v, f"{c} no separa las dos medidas: {v}"
        assert v["mencion"] >= v["campo"], (
            f"{c}: la medida generosa ({v['mencion']}) sale por debajo de la estricta "
            f"({v['campo']}) — una de las dos está mal contada")


def test_el_censo_declara_su_propio_metodo(con_recibos):
    """Un número sin método es un número que el consumidor no puede discutir. @harness lo
    va a citar como fuente de una métrica del piloto: tiene que poder leer qué mide."""
    d = con_recibos.get("/recibos/censo").json()
    assert "metodo" in d and isinstance(d["metodo"], dict)
    assert set(d["metodo"]) >= {"mencion", "campo"}, d["metodo"]


def test_el_censo_desglosa_por_productor(con_recibos):
    """Sin el desglose, «0% de conformidad» no dice a quién avisar. El shadow existe para
    saber QUIÉN tiene que cambiar, no para tener un porcentaje global."""
    d = con_recibos.get("/recibos/censo").json()
    assert "productores" in d and isinstance(d["productores"], list)
    if d["productores"]:
        p = d["productores"][0]
        assert {"actor", "n", "completos"} <= set(p), p


def test_conformidad_exige_los_NUEVE_como_campo(con_recibos):
    """⊕ anti-teatro: `completos` no puede contar menciones. Si contara, el censo daría
    conformidad a una entrada que sólo habla de commits en prosa."""
    d = con_recibos.get("/recibos/censo").json()
    assert d["campos_exigidos"] and len(d["campos_exigidos"]) == 9, d.get("campos_exigidos")
    por = {p["actor"]: p for p in d["productores"]}
    assert por["cumplidor"]["completos"] == 1, "el recibo real no cuenta como completo"
    assert por["prosista"]["completos"] == 0, (
        "una entrada que sólo NOMBRA los nueve campos en prosa cuenta como recibo "
        "completo: `completos` está contando menciones")
    assert por["casi"]["completos"] == 0, (
        "ocho campos y el noveno mencionado cuenta como completo: falta exigir los nueve")
    # y la prosa SÍ tiene que verse en la cota superior, o la medida generosa no mide
    assert d["campos"]["integration"]["mencion"] >= 3
    assert d["campos"]["integration"]["campo"] == 1


def test_el_corte_desde_recorta_de_verdad(con_recibos):
    """@harness lo pidió por una razón que es la del propio shadow: sin ventana temporal,
    el día 0 y el día 3 del piloto leen la misma cifra acumulada y no se ve moverse nada.
    Un censo que no distingue «hoy» de «desde siempre» no mide una intervención.

    ⊖ el que importa: que `desde` se acepte y se ignore. Un filtro que no filtra es peor
    que no tenerlo — devuelve el acumulado con cara de ventana.
    """
    todo = con_recibos.get("/recibos/censo").json()
    assert todo["total"] >= 3
    vacio = con_recibos.get("/recibos/censo?desde=2099-01-01T00:00:00Z").json()
    assert vacio["total"] == 0, (
        f"`desde` en el futuro devuelve {vacio['total']} entradas: no filtra")
    assert vacio["desde"] == "2099-01-01T00:00:00Z"
    entero = con_recibos.get("/recibos/censo?desde=2000-01-01T00:00:00Z").json()
    assert entero["total"] == todo["total"], "`desde` en el pasado recorta lo que no debe"


def test_un_desde_ilegible_no_se_traga_en_silencio(con_recibos):
    """Ausente ⇒ sin corte (silencioso). Presente e inválido ⇒ error RUIDOSO. Si un
    `desde` mal escrito cayera al acumulado, el shadow leería la cifra de siempre creyendo
    que lee la de hoy — y esa es justo la lectura que el corte existe para impedir."""
    r = con_recibos.get("/recibos/censo?desde=ayer")
    assert r.status_code == 422, f"un `desde` ilegible devolvió {r.status_code}"
    assert "desde" in r.text


def _planta(ts, tipo="PRODUCED", eid=None):
    import sqlite3, os
    con = sqlite3.connect(os.environ["LLMINBOX_DB"])
    con.execute(
        "INSERT OR REPLACE INTO entries (ledger,eid,arrival,seq,line_no,byte_off,ts,"
        "actor,tipo,head,body,visto,ausente,provisional,raw_tipo) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("l", eid or f"e-{ts}", 800, 0, 0, 0, ts, "a", tipo, "h", "b", None, None, 0, tipo))
    con.commit(); con.close()


def test_el_mismo_instante_escrito_de_dos_formas_corta_igual(cliente):
    """`ts` se guarda SIN zona (`2026-01-01T00:00:00`) y el corte comparaba TEXTO. Dos
    escrituras legales del MISMO instante daban resultados distintos:

        desde=2026-09-01T22:00:00Z       → 3
        desde=2026-09-02T00:00:00+02:00  → 0

    Es la clase que este repo ya curó una vez para el sello —«una hora local con una Z
    pegada pasaba igual»— reaparecida en el otro extremo: aquí no es el dato el que trae
    la zona mal, es el FILTRO el que la ignora. Y quien lo consume mide una jornada.
    """
    _planta("2026-09-01T23:00:00", eid="dentro")
    a = cliente.get("/recibos/censo?desde=2026-09-01T22:00:00Z").json()["total"]
    b = cliente.get("/recibos/censo",
                    params={"desde": "2026-09-02T00:00:00+02:00"}).json()["total"]
    assert a == b, (
        f"el mismo instante escrito en Z da {a} y con offset +02:00 da {b}: el corte "
        f"compara texto y se traga la zona")


def test_el_corte_declara_a_quien_NO_pudo_mirar(cliente):
    """Una entrada sin `ts` es invisible a cualquier `desde=`: `NULL >= ?` es NULL, o sea
    falso, y desaparece sin que nadie lo diga. Medido en producción: 84 PRODUCED (1%).

    Ya me pasó con los sellos de la wiki y la lección fue la misma: EL FILTRO RESPONDE, NO
    EL CORPUS. Un censo que recorta en silencio le da al shadow un denominador que no es
    el que cree tener, y el «100% con recibo» se calcula sobre una población recortada por
    una razón que no tiene nada que ver con la conducta que mide.
    """
    _planta(None, eid="sin-sello")
    d = cliente.get("/recibos/censo?desde=2026-01-01T00:00:00Z").json()
    assert d["sin_ts"] >= 1, (
        "el corte no declara cuántas entradas no pudo mirar por no tener `ts`")
    assert "sin_ts" in d["metodo"], "y no explica qué significa ese número"


def test_sin_corte_no_hay_excluidos_que_declarar(cliente):
    """⊖ de simetría: `sin_ts` sólo tiene sentido cuando hay corte. Publicarlo siempre
    haría pensar que se pierden entradas cuando no se pierde ninguna."""
    _planta(None, eid="sin-sello-2")
    d = cliente.get("/recibos/censo").json()
    assert d["sin_ts"] is None, (
        f"sin `desde` declara {d['sin_ts']} excluidos, pero sin corte no se excluye a nadie")
