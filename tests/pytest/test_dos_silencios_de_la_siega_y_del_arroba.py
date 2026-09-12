"""Dos formas de perder algo sin decirlo, encontradas por el barrido y verificadas aquí.

① LA SIEGA PISA UN CIERRE REAL. `siega_vencidos` lee `SELECT rowid ... WHERE cerrado IS
   NULL` y después hace `UPDATE ... WHERE rowid=?` SIN repetir la condición. Entre las dos
   sentencias cabe un `/claim/cierro` legítimo, y entonces la siega reescribe `cerrado` y
   pone `motivo='ttl_expirado'` encima de un cierre que sí ocurrió.

   No es cosmético: `cierro` y `ttl_expirado` son la distinción con la que se sabe si un
   trabajo se terminó o se abandonó. Reetiquetar el primero como el segundo acusa a alguien
   de haber dejado tirado lo que entregó, y el registro que existe para AUDITAR el reparto
   pasa a mentir en la dirección que más duele.

   Es la misma forma que la carrera del tope de claims: comprobar y escribir en dos pasos.

② UN `LLMINBOX_ARROBA_DESDE` MAL ESCRITO APAGA EL ENRUTADO POR `@`, EN SILENCIO. Se lee
   crudo y se compara como TEXTO contra `e.ts`, que siempre viene con cero-relleno. Medido:

       '2026-8-8'  vs ts '2026-08-09T12:00:00'  →  no enruta
       '8/8/2026'                               →  no enruta
       'ayer'                                   →  no enruta
       '2026-08-08' (el correcto)               →  enruta

   `2026-8-8` es el typo que cualquiera escribe. Y esta variable existe porque hubo 995
   destinatarios por arroba sin entregar: apagarla en silencio devuelve exactamente ese
   fallo, con el operador convencido de que la configuró.

   `LLMINBOX_WIP_GLOBAL` ya aborta con `SystemExit` si su valor no vale. El patrón hermano
   estaba en el repo y esta perilla no lo tenía.
"""
from __future__ import annotations
import os
import sqlite3

import pytest


def test_la_siega_no_pisa_un_cierre_real(cliente, servicio):
    """⊖ del EFECTO en la tabla, con la carrera REPRODUCIDA a propósito.

    Mi primer intento plantaba un claim ya cerrado y pasaba sobre el código roto: la siega
    ni lo selecciona, porque su SELECT filtra `cerrado IS NULL`. Era un test que no podía
    fallar. La ventana real está ENTRE el SELECT y el UPDATE, así que hay que meterse ahí:
    se envuelve `con.execute` y, justo después de que la siega lea las filas, se cierra el
    claim a mano — que es exactamente lo que hace un `/claim/cierro` que llega en ese
    instante.
    """
    quien = sorted(servicio.lp.CANON)[0]
    rol = servicio.lp.rol_de(quien)
    db = os.environ["LLMINBOX_DB"]
    con0 = sqlite3.connect(db)
    con0.execute("DELETE FROM claims WHERE agent=?", (rol,))
    con0.execute("INSERT INTO claims(tema,rol,agent,agent_bruto,abierto,bruto) "
                 "VALUES(?,?,?,?,?,?)",
                 ("t-carrera", "ejecuta", rol, quien, "2020-01-01T00:00:00+00:00", "t"))
    con0.commit(); con0.close()

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    estado = {"cerrado": False}

    class Espia:
        """`sqlite3.Connection.execute` es de sólo lectura, así que no se puede
        monkeypatchear: se envuelve la conexión entera y se delega todo lo demás."""
        def __init__(self, c): self._c = c
        def __getattr__(self, n): return getattr(self._c, n)
        def execute(self, sql, *a, **k):
            cur = self._c.execute(sql, *a, **k)
            if "SELECT rowid" in sql and not estado["cerrado"]:
                # el cierre legítimo aterriza justo aquí, entre las dos sentencias
                estado["cerrado"] = True
                self._c.execute(
                    "UPDATE claims SET cerrado=?, motivo='cierro' WHERE tema='t-carrera'",
                    ("2026-01-01T00:00:00+00:00",))
            return cur

    servicio.siega_vencidos(Espia(con))
    con.commit()
    r = con.execute("SELECT cerrado, motivo FROM claims WHERE tema='t-carrera'").fetchone()
    con.close()
    assert estado["cerrado"], "la carrera no se llegó a montar; el test no mide nada"
    assert r["motivo"] == "cierro", (
        f"la siega reetiquetó un cierre real como {r['motivo']!r}: el registro que audita "
        f"el reparto acusa de abandono a quien entregó")
    assert r["cerrado"] == "2026-01-01T00:00:00+00:00", "y le cambió la hora del cierre"


def test_el_UPDATE_de_la_siega_repite_la_condicion():
    """⊕ del mecanismo: la carrera de arriba no siempre se reproduce en un test de un solo
    hilo, y sin esta aserción el ⊖ pasaría por suerte."""
    import pathlib
    t = pathlib.Path("servicio.py").read_text()
    i = t.index("def siega_vencidos(")
    cuerpo = t[i:i + 4000]
    j = cuerpo.index("UPDATE claims SET cerrado=")
    assert "cerrado IS NULL" in cuerpo[j:j + 300], (
        "el UPDATE de la siega no repite `cerrado IS NULL`: entre el SELECT y él cabe un "
        "cierre legítimo que se pisa")


def test_un_arroba_desde_mal_escrito_no_arranca(monkeypatch):
    """Presente e inválido ⇒ ruidoso. `2026-8-8` apagaba el enrutado por `@` entero sin
    una palabra, y esta variable existe porque hubo 995 destinatarios sin entregar."""
    import importlib, ledger_parse
    for malo in ("2026-8-8", "8/8/2026", "ayer", "2026-13-01"):
        monkeypatch.setenv("LLMINBOX_ARROBA_DESDE", malo)
        with pytest.raises(SystemExit) as e:
            importlib.reload(ledger_parse)
        assert "ARROBA_DESDE" in str(e.value), (malo, str(e.value))
    monkeypatch.delenv("LLMINBOX_ARROBA_DESDE", raising=False)
    importlib.reload(ledger_parse)


def test_un_arroba_desde_BIEN_escrito_sigue_valiendo(monkeypatch):
    """⊕ obligatorio: una validación que rechaza lo bueno apaga la función igual que el
    typo, sólo que ruidosamente. Y ausente sigue siendo legítimo."""
    import importlib, ledger_parse
    monkeypatch.setenv("LLMINBOX_ARROBA_DESDE", "2026-08-08")
    importlib.reload(ledger_parse)
    assert ledger_parse.ARROBA_DESDE == "2026-08-08"
    monkeypatch.delenv("LLMINBOX_ARROBA_DESDE", raising=False)
    importlib.reload(ledger_parse)
    assert ledger_parse.ARROBA_DESDE == ""
