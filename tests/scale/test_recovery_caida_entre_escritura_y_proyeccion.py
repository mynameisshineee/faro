"""Falsador de G5 (recovery) a escala de proceso real, no de excepción inyectada.

`docs/GUARANTEES.md` fija la frase exacta: "Kill the process between the
durable write and its projection, then restart: the entry must exist exactly
once and the pending work must still be drainable. Duplicate or vanish, and
G5 is not met." Este falsador ejecuta esa frase literalmente: un PROCESO
hijo acepta un evento, arrienda su item de outbox, y muere con `os._exit()`
sin marcar nada más. El padre "reinicia" — abre un `Journal` nuevo sobre el
MISMO fichero — y comprueba las dos caras de G5 más una tercera, gratis por
compartir mecanismo con G4 (fencing): la ficha de arriendo del proceso
muerto queda INVÁLIDA tras el re-claim, igual que un lease relevado.
"""
from __future__ import annotations

import hashlib
import json
import time

import pytest

from ._arnes import GRAMATICA, INTENT, LANES, PEPPER, censo, journal, lanza, sesion
import coordination as C


def _entry_eid(event_id: str, receipt_id: str, ledger: str) -> str:
    """sha256 del CONTENIDO proyectado — lo que la DDL declara que ES
    `entry_eid` («es hash de CONTENIDO», coordination.py). Los literales
    `"e"*64`/`"f"*64` que había aquí no los validaba el kernel: pasaban
    igual escribiese el proyector basura o no."""
    contenido = json.dumps(
        {"event_id": event_id, "receipt_id": receipt_id, "ledger": ledger},
        sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(contenido).hexdigest()


def test_caida_entre_claim_y_materializado_no_duplica_y_sigue_drenable(tmp_path):
    j = journal(tmp_path)
    victima = sesion(j, "cred-victima", principal="victima")
    db = j.path
    j.close()

    clave = "clave-recovery"
    p = lanza("_worker_recovery_caida.py", [db, victima.token, clave])
    # LA CAÍDA ES EL PUNTO DE PARTIDA, no un efecto secundario: si el proceso
    # terminara limpio (`0`), este falsador no estaría midiendo lo que dice.
    assert p.returncode == 137, (
        f"el worker terminó rc={p.returncode} (limpio): no hubo caída que recuperar\n"
        f"stdout={p.stdout!r} stderr={p.stderr!r}")
    lineas = [l for l in p.stdout.strip().splitlines() if l.strip()]
    assert lineas, f"el worker no llegó a imprimir el JSON antes de morir; stderr={p.stderr!r}"
    datos = json.loads(lineas[-1])

    # El lease del worker muerto dura 1s; se espera a que venza de verdad —
    # nada de reloj inyectado, porque el vencimiento real es lo que hace
    # reclamable el trabajo sin que nadie lo libere a mano.
    time.sleep(1.4)

    # "REINICIO": una instancia nueva de `Journal` sobre el mismo fichero,
    # como haría el proceso que sustituye al caído.
    j2 = C.Journal(db, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo,
                  grammar=GRAMATICA)
    j2.initialize()
    try:
        # ── CARA 1 de G5, verbatim: mismo evento existe UNA vez. Reintentar
        # con la MISMA clave y el MISMO cuerpo devuelve el recibo ORIGINAL.
        replay = j2.accept_event(victima.token, idempotency_key=clave, intent=INTENT,
                                 ledger="l")
        assert replay.replayed is True
        assert replay.event_id == datos["event_id"]
        assert replay.receipt_id == datos["receipt_id"]

        # ── ⊖: la MISMA clave con OTRO cuerpo se rechaza, CERO evento nuevo;
        # el rechazo sí deja su recibo de auditoría, separado del evento.
        # la otra mitad de la frase de G5 ("a delayed command cannot revive").
        with pytest.raises(C.IdempotencyConflict):
            j2.accept_event(victima.token, idempotency_key=clave,
                            intent={**INTENT, "body": "cuerpo distinto tras la caída"},
                            ledger="l")

        con = j2._connect()
        assert con.execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == 1
        recibos_evento = con.execute(
            "SELECT receipt_id,subject_id FROM receipts WHERE subject_kind='event'"
        ).fetchall()
        assert [(r["receipt_id"], r["subject_id"]) for r in recibos_evento] == [
            (datos["receipt_id"], datos["event_id"])]
        rechazos = con.execute(
            "SELECT d.receipt_id FROM denials d JOIN receipts r USING (receipt_id)"
            " WHERE d.reason='IDEMPOTENCY_CONFLICT' AND r.subject_kind='denial'"
        ).fetchall()
        assert len(rechazos) == 1
        assert rechazos[0]["receipt_id"] != datos["receipt_id"]
        assert con.execute("SELECT COUNT(*) c FROM idempotency").fetchone()["c"] == 1
        assert j2.pending_outbox(victima.token) == 1, (
            "el trabajo del proceso caído tiene que seguir PENDIENTE, no perdido")

        # ── bonus (mecanismo de G4 aplicado al lease del outbox): la ficha
        # que tenía el proceso muerto NO puede usarse tras el re-claim — un
        # dueño anterior no escribe después de un relevo, aunque despierte
        # creyendo que todavía es suyo.
        job2 = j2.claim_outbox(victima.token, lease_s=60)
        assert job2 is not None and job2.event_id == datos["event_id"]
        assert job2.claim_token != datos["claim_token"], (
            "el re-claim reusó la ficha del proceso muerto: no hubo relevo real")
        with pytest.raises(C.FencingConflict):
            j2.mark_materialized(
                victima.token, datos["event_id"],
                entry_eid=_entry_eid(datos["event_id"], datos["receipt_id"], "l"),
                ledger="l", claim_token=datos["claim_token"])

        # ── CARA 2 de G5: el trabajo pendiente sigue siendo DRENABLE hasta el
        # final con la ficha correcta — no sólo "no perdido", sino completable.
        j2.mark_materialized(
            victima.token, job2.event_id,
            entry_eid=_entry_eid(job2.event_id, job2.receipt_id, job2.ledger),
            ledger="l", claim_token=job2.claim_token)
        j2.mark_indexed(victima.token, job2.event_id)
        assert j2.pending_outbox(victima.token) == 0, (
            "el trabajo quedó drenado: cero pendientes tras recovery + drain completo")
        con = j2._connect()
        assert con.execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == 1, (
            "el drain no puede haber duplicado el evento")
    finally:
        j2.close()
