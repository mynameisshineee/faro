"""Una entrada que desaparece y VUELVE se colaba por debajo de todos los cursores.

`arrival` es el orden en que esta instancia vio la entrada, y es el número que el
cursor de cada agente persigue. El esquema declara la regla en su propio comentario:
«una entrada mezclada en medio recibe un `arrival` nuevo y **aparece en la bandeja de
quien no la había visto**». Y el docstring de `reindex` prometía además que «si había
desaparecido, se anota que ha vuelto».

Ninguna de las dos era cierta al volver. El UPDATE de la rama «ya conocida» ponía
`ausente=NULL` y NO tocaba `arrival` — ni imprimía nada.

REPRODUCIDO de punta a punta antes de tocar código (2026-09-04):

    H1 H2 H3 H4          H2 en arrival=1, sin entregar a nadie todavía
    se borra H2          → ausente. /chain/verify: «✗ 1 entrada que ESTUVO…» ✔
    backend lee          → ve H1 H3 H4 H5. NO ve H2. Su cursor sube a 4.
    vuelve H2            → ausente=NULL, arrival SIGUE en 1
    /chain/verify        → «✓ sin pérdidas»
    bandeja de backend   → «(nada nuevo para backend)»   ← H2, perdida para siempre

La ventana de ausencia deja que los cursores SALTEN por encima, y al volver la entrada
se reinserta en el pasado, detrás de ellos. El agujero se abre justo con el remedio:
alguien ve gritar a `/chain/verify`, restaura el fichero, y con eso pierde el correo
en silencio y apaga la alarma que lo denunciaba.

En producción hoy: 29 entradas `ausente` vivas en 2 ledgers, la más reciente de esta
misma mañana. No es un caso de laboratorio.

Se re-entrega con `arrival` nuevo. Quien ya la había leído la ve dos veces —y eso se
declara aquí, no se esconde—: un duplicado se lee y se descarta; una pérdida no se ve.
"""
from __future__ import annotations

import json
import os
import sqlite3

CAB = "### [cto-A → backend · REQUEST] H%d\ncuerpo %d\n"


def _db():
    con = sqlite3.connect(os.environ["LLMINBOX_DB"])
    con.row_factory = sqlite3.Row
    return con


def _eid(head_frag):
    return _db().execute("SELECT eid, arrival FROM entries WHERE ledger='demo-ledger' "
                         "AND head LIKE ?", (f"%{head_frag}%",)).fetchone()


def _sembrar(tmp_path, servicio, n=4):
    """Las H* se APENDIZAN a lo que el arnés ya trae. Reescribir el fichero entero
    haría desaparecer las dos entradas del conftest y este test mediría, además de
    lo suyo, una desaparición que él mismo provoca."""
    md = tmp_path / "DEMO-LEDGER.md"
    base = md.read_text()
    todas = [CAB % (i, i) for i in range(1, n + 1)]
    md.write_text(base + "".join(todas))
    servicio.barrido()
    return md, base, todas


def test_una_entrada_que_vuelve_llega_a_quien_no_la_vio(servicio, cliente, tmp_path):
    md, base, todas = _sembrar(tmp_path, servicio)
    if True:
        c = cliente
        h2 = _eid("H2")

        # ① desaparece del MEDIO (edición real de un fichero de sólo-apéndice)
        md.write_text(base + "".join([todas[0], todas[2], todas[3]]) + CAB % (5, 5))
        servicio.barrido()
        assert _db().execute("SELECT ausente FROM entries WHERE eid=?",
                             (h2["eid"],)).fetchone()["ausente"] is not None

        # ② el cursor de backend SALTA por encima de ella — control: H2 no le llega
        txt = c.get("/inbox/backend", params={"only": "demo-ledger"}).text
        assert "H2" not in txt, "el control falla: la entrada ausente no debe servirse"
        hasta = json.loads(next(l for l in txt.splitlines()
                                if l.strip().startswith('{"hasta"')))
        c.post("/inbox/backend/leido", json=hasta)
        cursor = _db().execute("SELECT last_arrival FROM cursors "
                               "WHERE ledger='demo-ledger'").fetchone()["last_arrival"]
        assert cursor > h2["arrival"], "el control falla: el cursor no llegó a saltarla"

        # ③ vuelve, byte-idéntica
        md.write_text(base + "".join(todas) + CAB % (5, 5))
        servicio.barrido()
        vuelta = _db().execute("SELECT arrival, ausente FROM entries WHERE eid=?",
                               (h2["eid"],)).fetchone()
        assert vuelta["ausente"] is None

        # ⊖ LO QUE SE CIERRA: vuelve a estar entregable, no enterrada bajo el cursor
        assert vuelta["arrival"] > cursor, (
            f"volvió con arrival={vuelta['arrival']} y el cursor ya iba por {cursor}: "
            "invisible para siempre")
        assert "H2" in c.get("/inbox/backend", params={"only": "demo-ledger"}).text


def test_la_vuelta_deja_rastro(servicio, cliente, tmp_path):
    """La desaparición imprime 🔴 y `idas`. La vuelta no decía nada — y es el mismo
    evento de integridad visto por el otro lado."""
    md, base, todas = _sembrar(tmp_path, servicio)
    md.write_text(base + "".join([todas[0], todas[2], todas[3]]))
    servicio.barrido()
    md.write_text(base + "".join(todas))
    con = servicio.db()
    r = servicio.reindex("demo-ledger", str(md), con)
    con.close()
    assert r["vueltas"] == 1, r


def test_un_apendice_normal_no_renumera_a_nadie(servicio, cliente, tmp_path):
    """⊕ EL CONTROL QUE IMPORTA MÁS QUE EL ⊖.

    Una cura que reasignase `arrival` en cada refresco pasaría el test de arriba y
    volcaría el histórico entero en TODAS las bandejas en la primera pasada. El
    `arrival` sólo se mueve cuando la entrada estuvo AUSENTE; nunca por un refresco.
    """
    md, base, todas = _sembrar(tmp_path, servicio)
    antes = {r["eid"]: r["arrival"] for r in
             _db().execute("SELECT eid, arrival FROM entries WHERE ledger='demo-ledger'")}
    md.write_text(base + "".join(todas) + CAB % (5, 5))    # apéndice puro
    con = servicio.db()
    r = servicio.reindex("demo-ledger", str(md), con)
    con.close()
    assert r["vueltas"] == 0 and r["nuevas"] == 1
    despues = {r["eid"]: r["arrival"] for r in
               _db().execute("SELECT eid, arrival FROM entries WHERE ledger='demo-ledger'")}
    movidas = {k: (antes[k], despues[k]) for k in antes if antes[k] != despues[k]}
    assert not movidas, f"un apéndice movió el arrival de entradas ya vistas: {movidas}"


# ── LOS HUECOS QUE ENCONTRÓ LA AUDITORÍA DE FALSADORES (2026-09-04) ─────────────
# Los tres de arriba dejaban vivo el mutante que MÁS importa: partir el asignador de
# `arrival` en dos contadores —el bug que el comentario de `servicio.py` dice haber
# cerrado— sobrevivía a la suite entera. Razón: en ninguno de ellos una entrada VUELVE
# y otra LLEGA NUEVA en la MISMA pasada, que es la única situación en la que dos
# contadores separados se pisan. El comentario afirmaba una invariante que ningún test
# medía.

def test_una_vuelta_y_una_nueva_en_la_misma_pasada_no_comparten_arrival(
        servicio, cliente, tmp_path):
    """⊖ del asignador único. Mutante que ANTES sobrevivía y ahora muere:
    `(prox + asignados, …)` → `(prox + vueltas, …)` en la rama de la vuelta.

    Con dos contadores, la primera vuelta y la primera entrada nueva de la pasada
    reciben las dos `prox + 0`. Las dos filas existen (la PK es `(ledger, eid)`, no
    `arrival`), así que la base no protesta: lo que se rompe es el CURSOR, que avanza
    hasta un número y se lleva por delante la otra.
    """
    md, base, todas = _sembrar(tmp_path, servicio)
    h2 = _eid("H2")
    md.write_text(base + "".join([todas[0], todas[2], todas[3]]))     # se va H2
    servicio.barrido()
    assert _db().execute("SELECT ausente FROM entries WHERE eid=?",
                         (h2["eid"],)).fetchone()["ausente"] is not None

    # UNA sola pasada: vuelve H2 y llega H6 nueva.
    md.write_text(base + "".join(todas) + CAB % (6, 6))
    con = servicio.db()
    r = servicio.reindex("demo-ledger", str(md), con)
    con.close()
    assert r["vueltas"] == 1 and r["nuevas"] == 1, r

    vuelta = _eid("H2")["arrival"]
    nueva = _eid("H6")["arrival"]
    assert vuelta != nueva, (
        f"la que vuelve y la nueva comparten arrival={vuelta}: un cursor que avance "
        "hasta ahí se lleva una de las dos por delante")
    # Y las dos llegan de verdad, que es lo que el número existe para sostener.
    txt = cliente.get("/inbox/backend", params={"only": "demo-ledger"}).text
    assert "H2" in txt and "H6" in txt, txt[:300]


def test_dos_que_vuelven_a_la_vez_no_comparten_arrival(servicio, cliente, tmp_path):
    """El mismo mecanismo sin entrada nueva de por medio: dos resurrecciones en una
    pasada tienen que recibir números DISTINTOS, no el mismo dos veces."""
    md, base, todas = _sembrar(tmp_path, servicio)
    md.write_text(base + "".join([todas[0], todas[3]]))               # se van H2 y H3
    servicio.barrido()
    assert _db().execute(
        "SELECT COUNT(*) c FROM entries WHERE ledger='demo-ledger' "
        "AND ausente IS NOT NULL").fetchone()["c"] == 2

    md.write_text(base + "".join(todas))                              # vuelven las dos
    con = servicio.db()
    r = servicio.reindex("demo-ledger", str(md), con)
    con.close()
    assert r["vueltas"] == 2, r
    a2, a3 = _eid("H2")["arrival"], _eid("H3")["arrival"]
    assert a2 != a3, f"las dos que vuelven comparten arrival={a2}"
    txt = cliente.get("/inbox/backend", params={"only": "demo-ledger"}).text
    assert "H2" in txt and "H3" in txt, txt[:300]


def test_chain_verify_deja_de_acusar_solo_cuando_la_entrada_vuelve_a_entregarse(
        servicio, cliente, tmp_path):
    """El docstring de este módulo NARRA `/chain/verify` y ningún test lo llamaba.

    Lo que se fija aquí no es que la alarma se apague —eso ya pasaba, y ERA el
    defecto—: es que se apague A LA VEZ que la entrada vuelve a estar entregable. La
    alarma apagándose sola es exactamente lo que hacía el código viejo.
    """
    md, base, todas = _sembrar(tmp_path, servicio)
    assert "sin pérdidas" in cliente.get("/chain/verify").text

    md.write_text(base + "".join([todas[0], todas[2], todas[3]]))
    servicio.barrido()
    acusa = cliente.get("/chain/verify").text
    assert "ESTUVIERON y ya no están" in acusa, acusa[:300]

    # el cursor salta por encima mientras está ausente
    txt = cliente.get("/inbox/backend", params={"only": "demo-ledger"}).text
    cliente.post("/inbox/backend/leido",
                 json=json.loads(next(l for l in txt.splitlines()
                                      if l.strip().startswith('{"hasta"'))))

    md.write_text(base + "".join(todas))
    servicio.barrido()
    limpio = cliente.get("/chain/verify").text
    assert "ESTUVIERON y ya no están" not in limpio, limpio[:300]
    assert "H2" in cliente.get("/inbox/backend", params={"only": "demo-ledger"}).text, (
        "`/chain/verify` dejó de acusar la pérdida y la entrada NO volvió a la "
        "bandeja: la alarma se apagó sola, que es el defecto original")
