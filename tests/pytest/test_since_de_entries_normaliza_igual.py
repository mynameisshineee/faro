"""`/entries?since=` comparaba el texto crudo. Mismo instante, 136 entradas o 3.

Salió del barrido por clases: es la hermana exacta del `desde=` que curé en
`/recibos/censo` hace una hora, en un endpoint que está a 600 líneas y que el README
documenta como la búsqueda del bus.

MEDIDO EN PRODUCCIÓN antes de tocar nada:

    since=2026-09-01T22:00:00Z        → 136 entradas
    since=2026-09-02T00:00:00+02:00   →   3 entradas     ← el MISMO instante
    since=2026-09-01T22:00:00         → 136 entradas

133 entradas desaparecen con HTTP 200 y sin una palabra. Y a diferencia de
`/recibos/censo`, un `since` ilegible tampoco daba 422: entraba crudo al WHERE.

LO QUE NO SE HACE: copiar la normalización. Copiarla sería crear la segunda fuente que
hoy mismo me costó un defecto —mi lista propia de campos divergida del contrato— sólo que
en dos endpoints hermanos del mismo fichero. Se extrae `_corte_utc()` y la usan los dos.
"""
from __future__ import annotations
import pathlib
import re


def test_los_dos_endpoints_usan_LA_MISMA_normalizacion():
    """⊖ estructural: dos implementaciones que hacen lo mismo divergen. Es literalmente
    la avería del día. Si alguien vuelve a poner `p.append(since)` crudo, esto lo caza."""
    t = pathlib.Path("servicio.py").read_text()
    assert "def _corte_utc(" in t, "no hay una normalización única del corte por fecha"
    usos = len(re.findall(r"_corte_utc\(", t)) - 1          # menos la definición
    assert usos >= 2, f"sólo {usos} endpoint usa la normalización: el otro tiene su copia"
    assert 'w.append("e.ts>=?"); p.append(since)' not in t, (
        "`/entries` sigue metiendo el `since` crudo en el WHERE")


def test_el_mismo_instante_da_lo_mismo_en_las_tres_formas(cliente):
    import sqlite3, os
    con = sqlite3.connect(os.environ["LLMINBOX_DB"])
    for i, ts in enumerate(["2026-09-01T21:00:00", "2026-09-01T23:00:00"]):
        con.execute(
            "INSERT OR REPLACE INTO entries (ledger,eid,arrival,seq,line_no,byte_off,ts,"
            "actor,tipo,head,body,visto,ausente,provisional,raw_tipo) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("l", f"z{i}", 700 + i, i, i, i, ts, "a", "FYI", "h", "b", None, None, 0, "FYI"))
    con.commit(); con.close()

    n = [len(cliente.get("/entries", params={"since": s, "limit": 500}).json())
         for s in ("2026-09-01T22:00:00Z", "2026-09-02T00:00:00+02:00",
                   "2026-09-01T22:00:00")]
    assert n[0] == n[1] == n[2], (
        f"el mismo instante da {n} según cómo se escriba: el corte compara texto")
    assert n[0] >= 1, "la entrada plantada dentro de la ventana no se ve; el test no mide"


def test_un_since_ilegible_no_entra_crudo_al_WHERE(cliente):
    """⊕ de simetría con `/recibos/censo`: presente e inválido ⇒ 422 ruidoso. Antes un
    `since=ayer` se colaba al SQL y devolvía 200 con una lista recortada por comparación
    de texto contra la palabra «ayer» — un resultado plausible y falso, que es peor que
    un error."""
    r = cliente.get("/entries", params={"since": "ayer"})
    assert r.status_code == 422, f"`since=ayer` devolvió {r.status_code}"
    assert "since" in r.text
