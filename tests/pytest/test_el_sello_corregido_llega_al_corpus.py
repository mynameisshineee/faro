"""La cura del sello imposible era INERTE sobre las entradas que la motivaron.

`ledger_parse` ya no acepta `9999-99-99T99:99:99Z` como sello — pero eso actúa AL
PARSEAR, y esas entradas ya estaban indexadas. `reindex()` sólo reescribe una fila
conocida si cambió `seq`/`line_no`/`byte_off`/`provisional`/`ausente`/`raw_tipo`, y
`ts` NO estaba en esa lista: el sello viejo se quedaba fosilizado.

Lo cazó verificar EN PRODUCCIÓN tras desplegar, no la suite: con el arreglo ya dentro,
`GET /entries?limit=3&orden=ts` seguía devolviendo

    9999-99-99T99:99:99  ·  2026-13-45T99:99:99  ·  2026-10-17T23:30:00

El propio comentario de esa comparación lo predecía —«si algún día un campo de estos
empieza a derivarse de otra cosa, hay que añadirlo a la comparación o se quedará
fosilizado en silencio»— y es la SEGUNDA vez que se pasa por alto: la primera fue con
`raw_tipo`, y ahí lo cazó CodeRabbit.
"""
from __future__ import annotations

import os
import sqlite3


def _ts(eid_frag):
    con = sqlite3.connect(os.environ["LLMINBOX_DB"]); con.row_factory = sqlite3.Row
    r = con.execute("SELECT ts FROM entries WHERE ledger='demo-ledger' AND head LIKE ?",
                    (f"%{eid_frag}%",)).fetchone()
    con.close()
    return r["ts"] if r else "NO-EXISTE"


def test_un_sello_fosilizado_se_corrige_al_reindexar(servicio, cliente, tmp_path):
    md = tmp_path / "DEMO-LEDGER.md"
    md.write_text(md.read_text()
                  + "### [cto-A → backend · FYI] 9999-99-99T99:99:99Z — imposible\ncuerpo\n")
    servicio.barrido()

    # Se fosiliza a mano el estado ANTERIOR a la cura: el sello imposible guardado.
    con = sqlite3.connect(os.environ["LLMINBOX_DB"])
    con.execute("UPDATE entries SET ts='9999-99-99T99:99:99' WHERE head LIKE '%imposible%'")
    con.commit(); con.close()
    assert _ts("imposible") == "9999-99-99T99:99:99", "el arnés no llegó a fosilizarlo"

    # Un reindexado normal —sin que cambie nada más del fichero— tiene que corregirlo.
    c2 = servicio.db(); servicio.reindex("demo-ledger", str(md), c2); c2.close()
    assert _ts("imposible") is None, (
        "el sello imposible sobrevive al reindexado: la cura no llega al corpus")


def test_un_sello_bueno_no_se_reescribe_en_cada_pasada(servicio, cliente, tmp_path):
    """⊕ de coste: meter `ts` en la comparación no puede convertir cada pasada en una
    reescritura del ledger entero. Un apéndice puro sigue refrescando ≤1 fila."""
    md = tmp_path / "DEMO-LEDGER.md"
    md.write_text(md.read_text()
                  + "".join("### [cto-A → backend · FYI] 2026-09-0%dT10:00:00Z — e%d\nc\n"
                            % (i + 1, i) for i in range(5)))
    servicio.barrido()
    c2 = servicio.db(); r = servicio.reindex("demo-ledger", str(md), c2); c2.close()
    assert r["refrescadas"] == 0, (
        f"una pasada sin cambios refrescó {r['refrescadas']} filas: `ts` está "
        "provocando reescrituras no-op")
