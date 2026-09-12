"""El titular de ② mezclaba dos poblaciones y su mezcla cambia con la ventana.

Las entradas SIN SELLO DE HORA se incluyen a propósito, y ese razonamiento es bueno y
se conserva entero: con `ts >= corte` a secas desaparecían del informe justo las que
más suelen venir sin destinatario —quien no pone la hora tampoco pone la flecha—, o
sea que el filtro escondía el caso que la sección existe para contar, y el sesgo iba
en la dirección cómoda.

Lo que NO se sostiene es fundirlas con lo fechable en UN porcentaje, porque las sin
sello caen en TODAS las ventanas y lo fechable no:

    MEDIDO contra el índice vivo el 2026-09-04
    dias=1    12.563 entradas, de las que 10.434 (83%) no se pueden fechar → 42%
    dias=7    26.502 entradas, las mismas 10.434 (39%)                      → 26%
    dias=90   99.525 entradas, las mismas 10.434 (10%)                      → 34%

    lo FECHABLE de las últimas 24 h:  295 de 2.119  →  13%
    el stock sin sello:             5.192 de 10.434 →  49%

O sea: el titular decía 42% donde la conducta reciente es 13%, 3,2× por encima, y la
diferencia entera venía de un stock fijo que no depende de lo que la flota haga hoy.

La sección se llama PUBLICA Y NO DIRIGE y la lee la flota para decidir a quién avisar.
Un número que empeora al ESTRECHAR la ventana manda a corregir a quien no ha hecho
nada.

La cura no es quitar las sin sello —eso reintroduce el sesgo cómodo— sino DECIRLAS
APARTE. Las dos cifras visibles, y el titular donde estaba.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone


def _sembrar(entradas):
    """(actor, ts, con_destinatario) → filas en entries/recipients."""
    con = sqlite3.connect(os.environ["LLMINBOX_DB"])
    for i, (actor, ts, dirigida) in enumerate(entradas):
        eid = f"seed{i:04d}"
        con.execute("INSERT OR REPLACE INTO entries(ledger,eid,arrival,seq,ts,actor,head,"
                    "visto,ausente) VALUES ('demo-ledger',?,?,?,?,?,?,?,NULL)",
                    (eid, 9000 + i, 9000 + i, ts, actor, f"cab {i}", "2026-01-01"))
        if dirigida:
            con.execute("INSERT OR REPLACE INTO recipients VALUES ('demo-ledger',?,'backend')",
                        (eid,))
    con.commit()
    con.close()


def _titular(cliente, dias=1):
    txt = cliente.get("/doctor", params={"dias": dias}).text
    # El selector distingue el TITULAR (que también menciona «sin sello») de las dos
    # líneas nuevas, que empiezan por «· ». Mi primera versión cogía el titular por la
    # frase suelta y medía otra cosa.
    return [l for l in txt.splitlines()
            if "PUBLICA Y NO DIRIGE" in l or l.strip().startswith("· ")]


def test_las_dos_poblaciones_salen_por_separado(cliente):
    hoy = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _sembrar(
        # fechables de hoy: 1 de 4 sin dirigir  → 25%
        [("cto-A", hoy, True), ("cto-A", hoy, True), ("cto-A", hoy, True),
         ("cto-A", hoy, False)]
        # sin sello: 3 de 4 sin dirigir → 75%. Caen en CUALQUIER ventana.
        + [("cto-A", None, False), ("cto-A", None, False), ("cto-A", None, False),
           ("cto-A", None, True)])
    lineas = _titular(cliente)
    junto = "\n".join(lineas)
    assert "fechables en la ventana" in junto, (
        f"el titular no separa lo fechable del stock sin sello:\n{junto}")
    # la línea de lo fechable tiene que llevar SU propio porcentaje, no el mezclado
    # Lo esperado se CALCULA de la base, no se cablea: las entradas que siembra el
    # propio conftest tampoco llevan sello, y cablear «75%» medía mi aritmética en vez
    # del código. Me lo dijo el primer rojo.
    con = sqlite3.connect(os.environ["LLMINBOX_DB"])
    q = lambda w: con.execute(
        "SELECT COUNT(*) FROM entries e WHERE e.ausente IS NULL AND " + w).fetchone()[0]
    sin_dest = ("NOT EXISTS (SELECT 1 FROM recipients r WHERE r.ledger=e.ledger "
                "AND r.eid=e.eid)")
    st_hue, st_tot = q(f"(e.ts IS NULL OR e.ts='') AND {sin_dest}"), q("(e.ts IS NULL OR e.ts='')")
    con.close()

    fech = next(l for l in lineas if l.strip().startswith("· fechables"))
    assert "1 de 4 (25%)" in fech, fech
    stock = next(l for l in lineas if l.strip().startswith("· sin sello"))
    assert f"{st_hue} de {st_tot}" in stock, (stock, st_hue, st_tot)
    assert st_hue == 3, f"el arnés no sembró las 3 sin sello y sin dirigir: {st_hue}"


def test_el_stock_sin_sello_no_cambia_al_estrechar_la_ventana(cliente):
    """Lo que hacía ilegible el titular: estrechar la ventana SUBÍA el porcentaje,
    porque el stock pesaba más. La línea del stock tiene que dar igual en las dos."""
    viejo = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat(timespec="seconds")
    hoy = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _sembrar([("cto-A", hoy, True)] * 3 + [("cto-A", viejo, True)] * 20
             + [("cto-A", None, False)] * 5)
    corto = next(l for l in _titular(cliente, 1) if l.strip().startswith("· sin sello"))
    largo = next(l for l in _titular(cliente, 90) if l.strip().startswith("· sin sello"))
    assert corto == largo, f"el stock cambia con la ventana:\n  {corto}\n  {largo}"


def test_las_sin_sello_siguen_contando_en_el_titular(cliente):
    """⊖ DE NO PASARSE, y es el que protege el razonamiento que ya estaba: si al
    separar las poblaciones alguien las EXCLUYE del total, vuelve el sesgo cómodo que
    el comentario del código lleva describiendo desde el principio — el filtro
    escondía justo el caso que la sección existe para contar."""
    _sembrar([("cto-A", None, False)] * 7)
    titular = next(l for l in _titular(cliente) if "PUBLICA Y NO DIRIGE" in l)
    n = int(titular.split("—")[1].split(" de ")[0])
    assert n >= 7, f"las 7 sin sello y sin dirigir no cuentan en el titular: {titular}"
