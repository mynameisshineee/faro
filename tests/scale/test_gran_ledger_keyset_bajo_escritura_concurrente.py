"""Falsador de "ledger grande + keyset" bajo escritura CONCURRENTE (G6/G9/G10).

VERIFICADO ANTES DE CONSTRUIR: el cruce de carril de un cursor YA tiene
falsador estático — `tests/search/test_cursor_y_keyset.py::
test_cursor_atado_a_cada_filtro` (caso `{"lane": CARRIL_HERMANO}`) — sobre un
corpus fijo de una sola conexión. Lo que ese test NO mide, y que el
execution doc de v1.0 nombra para la envolvente de escala, es qué pasa
cuando el ledger SIGUE CRECIENDO — otra conexión insertando y reconstruyendo
— mientras alguien pagina. Este falsador mide eso: dos conexiones reales
sobre el MISMO fichero SQLite (WAL), una que pagina y otra que escribe.

No repite el corpus de un millón de `bench/m2_bench.py` (ese banco, con su
propio umbral de carga de host, es el que pertenece al NO-GO de capacidad
mientras el swap esté alto) — usa unos miles de filas, bastantes para que
"grande" signifique más de una página y menos de un minuto de CI.
"""
from __future__ import annotations

import hashlib
import sqlite3

import pytest

import search_contract as sc
import search_cursor as scur
import search_store as ss
from tests.search.conftest import ESQUEMA_ENTRIES

CLAVE_CURSOR = b"clave-de-escala-de-32-bytes-o-mas!!"
CARRIL = "carril-grande"
LEDGER = "ledger-grande"
ACL = {CARRIL: {LEDGER}}
AGUJA = "necesitaaguja"
FILAS_INICIALES = 3_000
FILAS_AÑADIDAS = 500
PAGINA = 25


def _fila(con, n: int, *, con_aguja: bool) -> None:
    eid = hashlib.sha256(f"grande:{n}".encode()).hexdigest()
    cuerpo = f"contenido de relleno numero {n}"
    if con_aguja:
        cuerpo += f" {AGUJA}"
    con.execute(
        "INSERT INTO entries(ledger,eid,arrival,ts,actor,tipo,head,body)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (LEDGER, eid, n, "2026-09-07", "backend", "FYI", f"titular {eid[:12]}",
         f"titular {eid[:12]}\n{cuerpo}"))


def _paginar_todo(store, *, query, max_paginas=500):
    """Recorre TODO el cursor y devuelve `(eids, generaciones_vistas)`.

    La cota de páginas es DURA, igual que en `bench/m2_bench.py`: una
    paginación que no termina no se deja correr para siempre — se declara
    `AssertionError`, no se cuelga el test.
    """
    vistos, generaciones, cursor, previo, paginas = [], set(), None, None, 0
    while True:
        r = store.search(lane=CARRIL, ledger=LEDGER, query=query, limit=PAGINA,
                         cursor=cursor)
        paginas += 1
        assert paginas <= max_paginas, "paginación que no termina"
        generaciones.add(r["generacion"])
        for f in r["filas"]:
            clave = (f["arrival"], f["eid"])
            if previo is not None:
                assert clave < previo, f"orden roto: {clave} no es < {previo}"
            previo = clave
            vistos.append(f["eid"])
        if not r["hay_mas"]:
            return vistos, paginas
        cursor = r["cursor"]


def test_cursor_caduca_y_la_paginacion_completa_sin_perder_ni_repetir(tmp_path):
    con = sqlite3.connect(str(tmp_path / "grande.sqlite"))
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(ESQUEMA_ENTRIES)
    for i in range(FILAS_INICIALES):
        _fila(con, i, con_aguja=(i % 50 == 0))          # 60 agujas plantadas
    con.commit()

    store = ss.SearchStore(con, cursor_key=CLAVE_CURSOR, acl=ACL)
    store.ensure_schema()
    store.set_acl(ACL)
    store.rebuild()

    # ── página 1, y el cursor que la sigue.
    r1 = store.search(lane=CARRIL, ledger=LEDGER, query=AGUJA, limit=10)
    assert r1["hay_mas"] is True, "con 60 agujas y límite 10 tiene que quedar más"
    cursor_viejo = r1["cursor"]
    generacion_antes = r1["generacion"]

    # ── SEGUNDA CONEXIÓN, real, sobre el MISMO fichero: el ledger CRECE y se
    # reconstruye MIENTRAS el cursor de arriba sigue vivo en la primera.
    con2 = sqlite3.connect(str(tmp_path / "grande.sqlite"))
    ss.SearchStore.register_udf(con2)  # cada escritor ejecuta los triggers normalizados
    for i in range(FILAS_INICIALES, FILAS_INICIALES + FILAS_AÑADIDAS):
        _fila(con2, i, con_aguja=(i % 50 == 0))          # +10 agujas nuevas
    con2.commit()
    store2 = ss.SearchStore(con2, cursor_key=CLAVE_CURSOR, acl=ACL)
    nueva_generacion = store2.rebuild()
    con2.close()
    assert nueva_generacion != generacion_antes, "el rebuild concurrente no cambió la generación"

    # ── el cursor emitido ANTES del crecimiento CADUCA: no se le sirven filas
    # de un índice que ya no es el que firmó. Fallar cerrado, no en silencio.
    with pytest.raises(scur.CursorGenerationStale):
        store.search(lane=CARRIL, ledger=LEDGER, query=AGUJA, limit=10,
                    cursor=cursor_viejo)

    # ── recorrido COMPLETO, desde cero, sobre el ledger YA CRECIDO: ni pierde
    # ni repite las 70 agujas (60 originales + 10 nuevas), en orden estricto.
    vistos, paginas = _paginar_todo(store, query=AGUJA)
    assert paginas > 1, "con límite 25 y 70 agujas tiene que hacer falta más de una página"
    assert len(vistos) == len(set(vistos)), "paginación con eids duplicados"
    assert len(vistos) == 70, f"recall incompleto tras crecer: {len(vistos)}/70"

    con.close()


def test_dos_carriles_con_el_mismo_ledger_no_comparten_cursor(tmp_path):
    """Complemento MÍNIMO del falsador estático ya existente: aquí el segundo
    carril autorizado apunta al MISMO `ledger`, no a uno hermano — la forma
    de colisión que el execution doc de v1.0 nombra ("intentionally
    colliding logical names"), que `test_cursor_y_keyset.py` no ejercita
    (usa `CARRIL_HERMANO` sobre el ledger PROPIO, no el ajeno)."""
    con = sqlite3.connect(str(tmp_path / "colision.sqlite"))
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(ESQUEMA_ENTRIES)
    for i in range(200):
        _fila(con, i, con_aguja=(i % 20 == 0))
    con.commit()

    carril_b = "carril-grande-b"
    acl = {CARRIL: {LEDGER}, carril_b: {LEDGER}}     # LOS DOS leen el MISMO ledger
    store = ss.SearchStore(con, cursor_key=CLAVE_CURSOR, acl=acl)
    store.ensure_schema()
    store.set_acl(acl)
    store.rebuild()

    r = store.search(lane=CARRIL, ledger=LEDGER, query=AGUJA, limit=3)
    assert r["hay_mas"] is True
    with pytest.raises(scur.CursorFilterMismatch):
        store.search(lane=carril_b, ledger=LEDGER, query=AGUJA, limit=3,
                    cursor=r["cursor"])
    con.close()
