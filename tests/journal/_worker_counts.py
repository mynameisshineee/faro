"""Procesos del test de concurrencia de `outbox_counts`. Dos papeles.

`mover`  — cicla items entre `pending` y `failed` por la API pública:
           `claim_outbox` → `mark_outbox_failed` (con `max_attempts=1` el primer
           fallo ya agota) → `requeue_outbox`. Nunca crea ni resuelve items, así
           que el TOTAL del carril es invariante mientras corre.
`lector` — muestrea `outbox_counts` y reporta cuántas muestras violan
           `pending + failed == TOTAL`. Con una lectura atómica ese número es 0
           por construcción; con dos lecturas separadas, no.

PROCESOS y no hilos, por el mismo motivo que `test_concurrencia_20_procesos`: la
exclusión que se mide es entre CONEXIONES de SQLite, y con hilos se puede pasar
por el GIL en vez de por el aislamiento de la transacción.

argv: <papel> <db> <token> <vueltas> <total>
stdout: una línea JSON.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.environ["LLMINBOX_RAIZ"])

import coordination as C
from _arnes import GRAMATICA, LANES, censo  # noqa: E402


def _journal(db):
    j = C.Journal(db, pepper=os.environ["LLMINBOX_PEPPER"].encode(),
                  busy_timeout_ms=30_000, max_attempts=1,
                  lane_ledgers=LANES, recipient_resolver=censo,
                  grammar=GRAMATICA)
    # Todo `Journal` se CLASIFICA antes de usarse. Idempotente sobre una base ya
    # conocida, y aquí además es lo único que toma el EXCLUSIVO: pasado esto, las
    # operaciones de este proceso sólo piden el COMPARTIDO.
    j.initialize()
    return j


def mover(j, token, vueltas):
    ciclos = fallos = 0
    for _ in range(vueltas):
        try:
            item = j.claim_outbox(token, lease_s=120)
            if item is None:                      # otro mover lo tiene: no es error
                continue
            j.mark_outbox_failed(token, item.event_id, error="mover",
                                 claim_token=item.claim_token)
            j.requeue_outbox(token, item.event_id, reason="mover")
            ciclos += 1
        except C.JournalError as e:
            # Se REPORTA, no se traga: un mover que se come sus errores deja al
            # lector midiendo una cola quieta y el test sale verde sin sujeto.
            fallos += 1
            if fallos > vueltas // 2:
                return {"ok": False, "error": f"{type(e).__name__}: {e}",
                        "ciclos": ciclos}
    return {"ok": True, "ciclos": ciclos, "fallos": fallos}


def lector(j, token, vueltas, total):
    rotas, max_failed, max_pending, muestras = 0, 0, 0, 0
    for _ in range(vueltas):
        c = j.outbox_counts(token)
        muestras += 1
        if c.pending + c.failed != total:
            rotas += 1
        max_failed = max(max_failed, c.failed)
        max_pending = max(max_pending, c.pending)
    return {"ok": True, "rotas": rotas, "muestras": muestras,
            "max_failed": max_failed, "max_pending": max_pending}


def main() -> int:
    papel, db, token, vueltas, total = sys.argv[1:6]
    j = _journal(db)
    try:
        if papel == "mover":
            out = mover(j, token, int(vueltas))
        else:
            out = lector(j, token, int(vueltas), int(total))
    except Exception as e:                        # se REPORTA, no se traga
        out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        j.close()
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
