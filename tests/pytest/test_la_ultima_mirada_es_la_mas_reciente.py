"""La columna «última mirada» de `/doctor` ① elegía UNA firma al azar, no la reciente.

`lecturas` se indexa por el NOMBRE con el que se miró (`cto-A`, `cto-llminbox`, …) y
`cursors` por el ROL (`cto`). El doctor agrupa por rol —bien, y su comentario explica
por qué: uniendo a pelo la misma persona salía dos veces—, pero lo hacía así:

    lec = {lp.rol_de(r["agent"]): r for r in con.execute("SELECT * FROM lecturas")}

Una comprensión de diccionario NO agrega: con siete filas que mapean al mismo rol gana
la ÚLTIMA QUE SALGA DE LA CONSULTA, que no tiene nada que ver con la más reciente.

MEDIDO contra el índice vivo el 2026-09-04:

    roles con más de una firma ........... 18 de 127
    roles cuya «última mirada» sale mal ... 15
    desfase máximo ....................... 26 días

`be`, `cpo`, `fe` y `wiki` figuraban como si no miraran su bandeja desde el 8-9 de
agosto. Habían mirado hacía un minuto.

Y no es una columna decorativa: la sección se llama «MIRA Y NO DRENA» y esas dos
fechas son justo lo que distingue «está mirando y no consume» (su bucle está roto) de
«ni mira» (está dormido). Con la mirada rancia, un agente vivo se lee como muerto — y
lo leí yo, sacando la conclusión equivocada, antes de mirar la consulta.
"""
from __future__ import annotations

import os
import sqlite3

import pytest


def _sembrar_lecturas(filas):
    con = sqlite3.connect(os.environ["LLMINBOX_DB"])
    con.executemany("INSERT OR REPLACE INTO lecturas(agent,primera,ultima,veces) "
                    "VALUES (?,?,?,?)", filas)
    con.commit()
    con.close()


def test_gana_la_mirada_mas_reciente_no_la_ultima_fila(cliente):
    # Dos firmas del MISMO rol (`be`): una vieja y una de hoy. Se insertan en ese
    # orden a propósito — con la comprensión de diccionario ganaba la segunda sólo por
    # llegar después, y aquí eso coincide con lo correcto; el orden inverso es el que
    # destapa el defecto, y va en el test de abajo.
    _sembrar_lecturas([
        ("backend", "2026-08-01T00:00:00+00:00", "2026-08-01T00:00:00+00:00", 5),
        ("backend-biklabs", "2026-09-04T10:00:00+00:00", "2026-09-04T10:00:00+00:00", 7),
    ])
    txt = cliente.get("/doctor", params={"dias": 90}).text
    fila = next((l for l in txt.splitlines() if l.strip().startswith("be ")), None)
    assert fila, txt[:1500]
    assert "2026-09-04" in fila, f"enseña una mirada rancia: {fila!r}"


def test_el_orden_de_la_consulta_no_decide_cual_se_enseña(cliente):
    """⊖ EL QUE IMPORTA. Con la vieja PRIMERO en la tabla el defecto no se ve; con la
    vieja DESPUÉS, la comprensión de diccionario se queda con ella y publica una
    mirada de hace un mes."""
    _sembrar_lecturas([
        ("backend-biklabs", "2026-09-04T10:00:00+00:00", "2026-09-04T10:00:00+00:00", 7),
        ("backend", "2026-08-01T00:00:00+00:00", "2026-08-01T00:00:00+00:00", 5),
    ])
    txt = cliente.get("/doctor", params={"dias": 90}).text
    fila = next((l for l in txt.splitlines() if l.strip().startswith("be ")), None)
    assert fila, txt[:1500]
    assert "2026-08-01" not in fila, (
        f"gana la última fila de la consulta en vez de la más reciente: {fila!r}")
    assert "2026-09-04" in fila, fila


def test_una_sola_firma_sigue_diciendo_lo_mismo(cliente):
    """⊕ de no pasarse: con una única firma por rol, que es el caso de 109 de 127
    roles, la columna tiene que salir exactamente igual que antes."""
    _sembrar_lecturas([
        ("backend", "2026-08-01T00:00:00+00:00", "2026-08-22T09:30:00+00:00", 5)])
    txt = cliente.get("/doctor", params={"dias": 90}).text
    fila = next((l for l in txt.splitlines() if l.strip().startswith("be ")), None)
    assert fila and "2026-08-22" in fila, (fila, txt[:1200])
