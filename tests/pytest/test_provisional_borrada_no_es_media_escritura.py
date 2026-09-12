"""Una entrada provisional BORRADA de verdad se tragaba como «se estaba escribiendo».

`reindex()` trata toda entrada provisional que desaparece como una escritura a medias y la
BORRA de `entries` y `recipients`, sin marcarla `ausente`. La cicatriz que lo motivó es
real y sigue siendo válida —escribir cabecera y cuerpo en dos pasos disparaba «entrada que
ESTUVO y ya no está», una acusación de manipulación por uso normal— pero el criterio no
distingue las dos causas:

    provisional que desaparece porque SE COMPLETÓ  → legítimo, se borra
    provisional que desaparece porque LA BORRARON  → pérdida real, se tragaba

Y la consecuencia es la peor posible: `/chain/verify` existe SÓLO para detectar entradas
que estuvieron y ya no están, y sobre este caso daba «✓ sin pérdidas». El detector de
manipulación devolvía verde ante una manipulación.

LA SEÑAL QUE LAS SEPARA es la posición, y sale de la promesa del propio formato: un ledger
es de SÓLO APÉNDICE, así que lo que se está escribiendo a trozos está AL FINAL. Una
provisional que desaparece del MEDIO no se estaba escribiendo: el fichero se editó.
"""
from __future__ import annotations
import os
import sqlite3


def _escribe(p, *cabeceras):
    p.write_text("".join(
        f"\n### [a → backend · FYI] 2026-09-0{i+1}T00:00:00Z — {h}\ncuerpo de {h}\n"
        for i, h in enumerate(cabeceras)))


def test_una_provisional_del_MEDIO_que_desaparece_es_una_perdida(cliente, servicio):
    """⊖ el que importa: alguien edita el fichero y quita una entrada de en medio."""
    s = servicio
    led = list(s.LEDGERS)[0]
    p = __import__("pathlib").Path(s.LEDGERS[led])
    _escribe(p, "H1", "H2", "H3")
    con = s.db(); s.reindex(led, str(p), con)
    # H2 se marca provisional a mano: reproduce el estado en que la deja un append parcial
    con.execute("UPDATE entries SET provisional=1 WHERE ledger=? AND head LIKE '%H2%'", (led,))
    con.commit()
    _escribe(p, "H1", "H3")                      # la borran del MEDIO
    s.reindex(led, str(p), con)
    filas = con.execute(
        "SELECT ausente FROM entries WHERE ledger=? AND head LIKE '%H2%'", (led,)).fetchall()
    con.close()
    assert filas, (
        "la entrada provisional borrada del MEDIO se eliminó sin dejar rastro: "
        "`/chain/verify` dirá «sin pérdidas» sobre una pérdida real")
    assert filas[0]["ausente"] is not None, "está pero no se marcó como desaparecida"


def test_una_provisional_DEL_FINAL_sigue_sin_acusar(cliente, servicio):
    """⊕ OBLIGATORIO — la cicatriz del 2026-07-27. Escribir cabecera y cuerpo en dos pasos
    es uso NORMAL, y si eso vuelve a disparar «entrada desaparecida» la alarma salta sola y
    se aprende a ignorarla, que es peor que no tenerla."""
    s = servicio
    led = list(s.LEDGERS)[0]
    p = __import__("pathlib").Path(s.LEDGERS[led])
    _escribe(p, "H1", "H2")
    con = s.db(); s.reindex(led, str(p), con)
    con.execute("UPDATE entries SET provisional=1 WHERE ledger=? AND head LIKE '%H2%'", (led,))
    con.commit()
    # la última se COMPLETA: mismo sitio, texto distinto ⇒ el eid viejo desaparece
    p.write_text("\n### [a → backend · FYI] 2026-09-01T00:00:00Z — H1\ncuerpo de H1\n"
                 "\n### [a → backend · FYI] 2026-09-02T00:00:00Z — H2\ncuerpo COMPLETO\n")
    s.reindex(led, str(p), con)
    # SÓLO MIS ENTRADAS: el ledger de prueba viene sembrado y sobrescribirlo genera una
    # desaparición que es de mi arnés, no del código. Mi primera versión la contaba y me
    # dio un rojo que no era del sistema.
    n = con.execute("SELECT COUNT(*) c FROM entries WHERE ledger=? AND ausente IS NOT NULL "
                    "AND (head LIKE '%H1%' OR head LIKE '%H2%')", (led,)).fetchone()["c"]
    con.close()
    assert n == 0, (
        f"{n} entrada(s) acusadas de desaparecer por completarse un append: la alarma "
        f"vuelve a saltar sola con uso normal (cicatriz 2026-07-27)")
