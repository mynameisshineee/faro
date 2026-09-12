"""Falsador 5 del ADR: 20 procesos compitiendo por la misma clave.

Por qué 20 PROCESOS y no 20 hilos: la exclusión que se prueba es entre
CONEXIONES de SQLite, y un test con hilos puede pasar por el GIL en vez de por
`BEGIN IMMEDIATE`. Este repo ya probó así el cerrojo de `claims` (20 procesos,
exactamente 1 ganador); se mantiene el listón.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import coordination as C
from ._arnes import GRAMATICA, LANES, PEPPER, censo, journal, sesion

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(os.path.dirname(AQUI))
WORKER = os.path.join(AQUI, "_worker_idem.py")
N = 20


def _lanza(db, token, clave, cuerpos):
    env = {**os.environ, "LLMINBOX_RAIZ": RAIZ,
           "LLMINBOX_PEPPER": PEPPER.decode()}
    procs = [subprocess.Popen([sys.executable, WORKER, db, token, clave, c],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, env=env) for c in cuerpos]
    salidas = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, f"worker rc={p.returncode}: {err}"
        salidas.append(json.loads(out.strip().splitlines()[-1]))
    return salidas


def test_veinte_procesos_misma_clave_mismo_cuerpo_producen_UN_evento(tmp_path):
    j = journal(tmp_path)
    s = sesion(j)
    db = j.path
    j.close()

    salidas = _lanza(db, s.token, "clave-compartida", ["mismo cuerpo"] * N)

    assert all(o["ok"] for o in salidas), [o for o in salidas if not o["ok"]]
    ids = {o["event_id"] for o in salidas}
    recibos = {o["receipt_id"] for o in salidas}
    assert len(ids) == 1, f"{len(ids)} eventos distintos para una clave"
    assert len(recibos) == 1
    # Exactamente uno creó; los otros 19 releyeron el recibo ORIGINAL.
    assert sum(1 for o in salidas if not o["replayed"]) == 1
    assert sum(1 for o in salidas if o["replayed"]) == N - 1

    j2 = C.Journal(db, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
    j2.initialize()
    con = j2._connect()
    assert con.execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == 1
    assert con.execute("SELECT COUNT(*) c FROM receipts").fetchone()["c"] == 1
    assert con.execute("SELECT COUNT(*) c FROM outbox").fetchone()["c"] == 1
    assert con.execute("SELECT COUNT(*) c FROM idempotency").fetchone()["c"] == 1
    j2.close()


def test_veinte_procesos_misma_clave_cuerpos_DISTINTOS_dan_conflicto_sin_mutacion(tmp_path):
    """El ⊖ del anterior: si la comparación fuera sólo por clave, aquí saldrían
    20 réplicas silenciosas del primer recibo en vez de 19 conflictos."""
    j = journal(tmp_path)
    s = sesion(j)
    db = j.path
    j.close()

    salidas = _lanza(db, s.token, "clave-compartida",
                     [f"cuerpo distinto {i}" for i in range(N)])

    aceptados = [o for o in salidas if o["ok"]]
    conflictos = [o for o in salidas if not o["ok"] and o["error"] == "conflict"]
    otros = [o for o in salidas if not o["ok"] and o["error"] != "conflict"]
    assert otros == [], otros
    assert len(aceptados) == 1, f"{len(aceptados)} aceptados: la clave no protegió"
    assert len(conflictos) == N - 1
    assert aceptados[0]["replayed"] is False

    j2 = C.Journal(db, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
    j2.initialize()
    con = j2._connect()
    assert con.execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == 1
    assert con.execute("SELECT COUNT(*) c FROM idempotency").fetchone()["c"] == 1
    j2.close()
