"""`X-Filas-Capadas` se decidía por el `limit` PEDIDO, no por si hubo recorte.

    capado = cuerpo and limit > CUERPO_MAX_FILAS

Con `cuerpo=true` y `limit=400`, `capado` es True SIEMPRE — haya 4.000 entradas o ninguna.
Medido en producción, con una búsqueda sin resultados:

    devuelve 0 entradas · x-filas-capadas=0
    y la interfaz decía: «0 entradas (recortado a 0 — hay más)»

O sea que la cabecera que puse anoche PARA distinguir «esto es todo lo que hay» de «esto es
todo lo que te doy» afirmaba lo segundo cuando la verdad era lo primero. Curé una mentira
y sembré la contraria.

La cura es medir el EFECTO y no la intención: hubo recorte si las filas devueltas llegan al
tope. Si vuelven menos, no se recortó nada aunque el `limit` pedido fuera enorme.
"""
from __future__ import annotations
import os
import sqlite3


def _planta(n: int) -> None:
    con = sqlite3.connect(os.environ["LLMINBOX_DB"])
    for i in range(n):
        con.execute(
            "INSERT OR REPLACE INTO entries (ledger,eid,arrival,seq,line_no,byte_off,ts,"
            "actor,tipo,head,body,visto,ausente,provisional,raw_tipo) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("l", f"k{i}", 400 + i, i, i, i, "2026-09-01T00:00:00", "a", "FYI",
             f"### [a → b · FYI] h{i}", "cuerpo", None, None, 0, "FYI"))
    con.commit(); con.close()


def test_sin_recorte_NO_manda_la_cabecera(cliente):
    """⊖ el que importa: una búsqueda sin resultados decía «hay más»."""
    r = cliente.get("/entries", params={"q": "zqxjkvwpmb-no-existe", "limit": 400,
                                        "cuerpo": True})
    assert len(r.json()) == 0
    assert "x-filas-capadas" not in {k.lower() for k in r.headers}, (
        f"con 0 resultados manda la cabecera de recorte: la interfaz dice «0 entradas "
        f"(recortado a 0 — hay más)» y no hay más")


def test_con_pocas_entradas_tampoco(cliente, servicio):
    """Menos filas que el tope ⇒ no hubo recorte, aunque se pidieran 400."""
    _planta(3)
    r = cliente.get("/entries", params={"ledger": "l", "limit": 400, "cuerpo": True})
    n = len(r.json())
    assert 0 < n < servicio.CUERPO_MAX_FILAS, f"el fixture no plantó el caso: {n} filas"
    assert "x-filas-capadas" not in {k.lower() for k in r.headers}, (
        f"{n} filas por debajo del tope y aun así declara recorte")


def test_cuando_SI_hay_recorte_la_cabecera_VIENE(cliente, servicio):
    """⊕ obligatorio, y es la mitad que se olvida: una cura que deje de mandarla NUNCA
    apagaría el aviso entero, que es la avería original —la interfaz decía «10 entradas»
    habiendo decenas de miles— con otra cara."""
    _planta(servicio.CUERPO_MAX_FILAS + 5)
    r = cliente.get("/entries", params={"ledger": "l", "limit": 400, "cuerpo": True})
    n = len(r.json())
    assert n == servicio.CUERPO_MAX_FILAS, n
    cab = {k.lower(): v for k, v in r.headers.items()}
    assert "x-filas-capadas" in cab, (
        "hay recorte de verdad y NO se declara: vuelve la mentira original")
    assert int(cab["x-filas-capadas"]) == n
