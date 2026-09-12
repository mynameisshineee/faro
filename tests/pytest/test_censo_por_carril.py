"""`/recibos/censo?carril=` — la partición que evita el porcentaje pelado.

Lo pidió @harness tras medir 21 DELIVERED con 1 completo: un 5% sobre ese corpus se lee
como «los agentes son descuidados» y en realidad son DOS POBLACIONES —el recibo de nueve
campos del canon y la etiqueta general «he entregado algo»— separadas por el carril donde
se publican. 20 de las 21 están en `64bis-wiki`, que no es el carril del piloto.

Ya existía `?ledger=`. El carril es la capa que la flota usa para hablar, y traducirlo a
mano obliga a cada consumidor a llevar su propia copia del mapa `carril→ledger`. Una copia
que puede derivar es exactamente el defecto que este endpoint ya se comió una vez con los
nombres de campo.

UN CARRIL DESCONOCIDO NO SE TRAGA: devolvería el corpus ENTERO con cara de estar filtrado,
que es el peor resultado posible para un endpoint cuyo trabajo es dar denominadores
honestos.
"""
from __future__ import annotations
import os
import sqlite3


def _planta(ledger, eid, tipo="DELIVERED"):
    con = sqlite3.connect(os.environ["LLMINBOX_DB"])
    con.execute(
        "INSERT OR REPLACE INTO entries (ledger,eid,arrival,seq,line_no,byte_off,ts,"
        "actor,tipo,head,body,visto,ausente,provisional,raw_tipo) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ledger, eid, 300 + len(eid), 0, 0, 0, "2026-09-04T00:00:00", "a", tipo,
         "h", "cuerpo", None, None, 0, tipo))
    con.commit(); con.close()


def test_el_carril_filtra_como_su_ledger(cliente, servicio):
    if not servicio.CARRIL_LEDGER:
        import pytest
        pytest.skip("el arnés no monta carriles")
    carril, ledger = sorted(servicio.CARRIL_LEDGER.items())[0]
    _planta(ledger, "en-carril")
    _planta("otro-ledger", "fuera")
    a = cliente.get(f"/recibos/censo?tipo=DELIVERED&carril={carril}").json()
    b = cliente.get(f"/recibos/censo?tipo=DELIVERED&ledger={ledger}").json()
    assert a["total"] == b["total"], (
        f"el corte por carril ({a['total']}) no coincide con el de su ledger "
        f"({b['total']}): el mapa carril→ledger no se está aplicando")
    assert a["carril"] == carril and a["ledger"] == ledger, a


def test_un_carril_DESCONOCIDO_no_devuelve_el_corpus_entero(cliente):
    """⊖ el que importa: tragarse el filtro devolvería todo con cara de filtrado, que en un
    endpoint cuyo trabajo es dar denominadores honestos es el peor resultado posible."""
    r = cliente.get("/recibos/censo?tipo=DELIVERED&carril=no-existe-este-carril")
    assert r.status_code == 422, f"un carril inventado devolvió {r.status_code}"
    assert "carril" in r.text


def test_sin_carril_no_cambia_nada(cliente, servicio):
    """⊕ de no-regresión: el parámetro es aditivo."""
    _planta("l", "libre")
    d = cliente.get("/recibos/censo?tipo=DELIVERED").json()
    assert d["total"] >= 1 and d["carril"] is None


def test_carril_y_ledger_a_la_vez_es_un_error(cliente, servicio):
    """Dos filtros que dicen lo mismo pueden CONTRADECIRSE, y entonces alguien lee un
    número creyendo que filtró por lo que pidió. Se rechaza en vez de elegir uno."""
    if not servicio.CARRIL_LEDGER:
        import pytest
        pytest.skip("el arnés no monta carriles")
    carril = sorted(servicio.CARRIL_LEDGER)[0]
    r = cliente.get(f"/recibos/censo?carril={carril}&ledger=otro-ledger")
    assert r.status_code == 422, f"aceptó carril y ledger contradictorios: {r.status_code}"
