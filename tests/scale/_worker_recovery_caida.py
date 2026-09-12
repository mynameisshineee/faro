"""El proceso que MUERE entre la escritura durable y su proyección (G5).

Acepta un evento, arrienda su item de outbox — las dos escrituras durables que
importan— y entonces `os._exit()` SIN pasar por `finally`, sin `j.close()` y
sin marcar `materialized`. Es la frase literal del falsador de G5 en
`docs/GUARANTEES.md`: "Kill the process between the durable write and its
projection, then restart" — aquí ejecutada contra un proceso real, no una
excepción inyectada.

argv: <db> <token> <clave>
stdout: UNA línea JSON, IMPRESA Y VOLCADA (`flush`) antes de morir — un pipe
        a un proceso que no llega a cerrarse no vacía su búfer solo.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.environ["LLMINBOX_RAIZ"])

import coordination as C                                  # noqa: E402
from _arnes import GRAMATICA, INTENT, LANES, censo         # noqa: E402

LEASE_S = 1     # corto a propósito: el padre sólo espera a que venza, no a un reloj falso.


def main() -> int:
    db, token, clave = sys.argv[1:4]
    j = C.Journal(db, pepper=os.environ["LLMINBOX_PEPPER"].encode(),
                  busy_timeout_ms=30_000, lane_ledgers=LANES,
                  recipient_resolver=censo, grammar=GRAMATICA)
    j.initialize()
    a = j.accept_event(token, idempotency_key=clave, intent=INTENT, ledger="l")
    job = j.claim_outbox(token, lease_s=LEASE_S)
    assert job is not None, "el item recién aceptado tiene que ser reclamable"
    assert job.event_id == a.event_id, "el claim debe ser del evento recién aceptado"
    print(json.dumps({"event_id": a.event_id, "receipt_id": a.receipt_id,
                      "claimed_event_id": job.event_id,
                      "claim_token": job.claim_token}))
    sys.stdout.flush()
    # LA CAÍDA: sin `j.close()`, sin `mark_materialized`, sin `finally`. `137`
    # no es un código de librería — es simplemente un valor reconocible como
    # "esto no terminó por su cuenta" para quien lea el `returncode`.
    os._exit(137)


if __name__ == "__main__":
    raise SystemExit(main())
