"""Hallazgos de @vision-canon (bikeus) y @marketing, 2026-08-11 — medidos por ellos.

⑬ `/entries` ordena por `ts`, que sella EL EMISOR: una entrada con sello futuro se
   sienta en la cabeza de la ventana y no se mueve, así que quien tome «la primera»
   como cabeza del ledger queda ciego. Falla hacia VERDE (un medidor de atraso
   reporta «al día» con entradas sin leer). Salida: `orden=arrival`, que lo pone el
   servidor y el emisor no controla.
⑭ `q=` daba CERO EXACTO sobre entradas ENTREGADAS cuando el término cruzaba un salto
   de línea — los posts van envueltos a ~90 columnas.
⑮ `/stat` publicaba como «última» una entrada AUSENTE (`9999-99-99T99:99:99`), que
   `/entries` no sirve: la métrica se contradecía con la vista que resume.
"""
from __future__ import annotations

import sqlite3

from .conftest import db_directa


def _sembrar_futuro(servicio):
    """Una entrada con sello FUTURO pero arrival BAJO, y otra actual con arrival alto:
    la pareja mínima que separa «orden del emisor» de «orden del servidor»."""
    con = db_directa(servicio)
    con.execute("INSERT OR REPLACE INTO entries (ledger,eid,arrival,seq,line_no,byte_off,"
                "ts,actor,tipo,head,body,visto,ausente,provisional) VALUES"
                "('demo-ledger','ffff01',1,1,1,0,'2099-10-17T23:30:00','cto-A',NULL,"
                "'### [cto-A → backend] sello del futuro','cuerpo futuro',NULL,NULL,NULL)")
    con.execute("INSERT OR REPLACE INTO entries (ledger,eid,arrival,seq,line_no,byte_off,"
                "ts,actor,tipo,head,body,visto,ausente,provisional) VALUES"
                "('demo-ledger','ffff02',9999,2,2,0,'2026-08-11T10:00:00','cto-A',NULL,"
                "'### [cto-A → backend] la cabeza REAL','cuerpo cabeza',NULL,NULL,NULL)")
    con.commit()
    con.close()


def test_sello_futuro_secuestra_la_cabeza_por_defecto(cliente, servicio):
    """El comportamiento de HOY, escrito para que se vea: por `ts` manda el emisor.

    No es una aserción de que esté bien — es la línea base que justifica ⑬, y el
    control que demuestra que el test siguiente mide un cambio real.
    """
    _sembrar_futuro(servicio)
    filas = cliente.get("/entries?ledger=demo-ledger&limit=5").json()
    assert filas[0]["eid"] == "ffff01", "el sello futuro ya no encabeza: revisa ⑬"


def test_orden_arrival_devuelve_la_cabeza_real(cliente, servicio):
    """`orden=arrival` ⇒ manda el servidor.

    FALSADOR: si el parámetro se ignorara (como `only=` durante meses), la primera
    fila seguiría siendo la del sello futuro y esto se pone rojo.
    """
    _sembrar_futuro(servicio)
    filas = cliente.get("/entries?ledger=demo-ledger&limit=5&orden=arrival").json()
    assert filas[0]["eid"] == "ffff02"
    assert filas[0]["arrival"] == 9999


def test_orden_invalido_es_422_no_silencio(cliente):
    """Un valor que no existe no puede caer al default en silencio — la clase de
    fallo del `?only=` ignorado y del carril que degradaba."""
    assert cliente.get("/entries?orden=inventado").status_code == 422


def test_q_encuentra_frases_que_cruzan_el_salto_de_linea(cliente, servicio):
    """⑭ El caso de @marketing: la frase existe, pero partida en dos líneas.

    FALSADOR: con el `LIKE` sobre la columna cruda (sin aplanar los saltos), esto
    devuelve 0 — el cero exacto que casi provoca un duplicado.
    """
    con = db_directa(servicio)
    con.execute("INSERT OR REPLACE INTO entries (ledger,eid,arrival,seq,line_no,byte_off,"
                "ts,actor,tipo,head,body,visto,ausente,provisional) VALUES"
                "('demo-ledger','ffff03',7,3,3,0,'2026-08-11T11:00:00','cto-A',NULL,"
                "'### [cto-A → backend] envuelta','una frase que cruza\n\nel salto de linea',"
                "NULL,NULL,NULL)")
    con.commit()
    con.close()
    # DOBLE salto a propósito: es lo que trae el corpus real (fin de párrafo). La
    # primera versión de este test sembraba UNO solo —más fácil que la realidad— y
    # pasaba en verde mientras producción seguía devolviendo 0 sobre la frase que
    # motivó el arreglo. Un test más benévolo que el mundo certifica un fix a medias.
    assert len(cliente.get("/entries?q=cruza%20el%20salto").json()) == 1
    # y el término con espacios de más también, que es como se teclea al copiar
    assert len(cliente.get("/entries?q=cruza%20%20%20el%20salto").json()) == 1


def test_stat_no_publica_como_ultima_una_entrada_ausente(cliente, servicio):
    """⑮ `stat` resumía con las DESAPARECIDAS dentro: publicaba un sello imposible
    como «última» mientras `/entries` no servía esa entrada jamás.

    FALSADOR: con `MAX(ts)` a secas, `ultima` sale '9999-99-99T99:99:99' y la
    métrica contradice a la vista que resume.
    """
    con = db_directa(servicio)
    con.execute("INSERT OR REPLACE INTO entries (ledger,eid,arrival,seq,line_no,byte_off,"
                "ts,actor,tipo,head,body,visto,ausente,provisional) VALUES"
                "('demo-ledger','ffff04',5,4,4,0,'9999-99-99T99:99:99','cto-A',NULL,"
                "'### [cto-A → backend] fantasma','cuerpo',NULL,'2026-08-11T00:00:00',NULL)")
    con.commit()
    con.close()
    fila = next(s for s in cliente.get("/stat").json() if s["ledger"] == "demo-ledger")
    assert fila["ultima"] != "9999-99-99T99:99:99"
    assert fila["desaparecidas"] >= 1, "la ausente sigue contándose donde SÍ toca"
    assert fila["ultimo_arrival"] is not None
