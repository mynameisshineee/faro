"""Un PROCESO que intenta aceptar el mismo evento. Lo lanza el test de 20.

Procesos y no hilos a propósito: el GIL de un test con hilos puede esconder una
carrera que en SQLite es entre CONEXIONES. El falsador del ADR dice «competing
processes» y esto son procesos de verdad.

argv: <db> <token> <clave> <cuerpo>
stdout: una línea JSON.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.environ["LLMINBOX_RAIZ"])

import coordination as C
from _arnes import GRAMATICA, censo  # noqa: E402

INTENT = {"type": "message", "verb": "inform", "to": ["security"],
          "kind": "DELIVERED", "head": "candidato listo"}


def main() -> int:
    db, token, clave, cuerpo = sys.argv[1:5]
    j = C.Journal(db, pepper=os.environ["LLMINBOX_PEPPER"].encode(),
                  busy_timeout_ms=20_000,
                  lane_ledgers={"llminbox": ["llminbox", "l"]}, recipient_resolver=censo, grammar=GRAMATICA)
    try:
        # Desde M1-7 todo `Journal` se CLASIFICA antes de usarse: sin veredicto no
        # se abre nada. Es idempotente y barato sobre una base ya conocida.
        j.initialize()
        a = j.accept_event(token, idempotency_key=clave,
                           intent={**INTENT, "body": cuerpo}, ledger="llminbox")
        print(json.dumps({"ok": True, "event_id": a.event_id,
                          "receipt_id": a.receipt_id, "replayed": a.replayed}))
    except C.IdempotencyConflict:
        print(json.dumps({"ok": False, "error": "conflict"}))
    except Exception as e:                       # se REPORTA, no se traga
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
    finally:
        j.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
