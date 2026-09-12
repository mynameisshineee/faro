"""Un AGENTE del falsador de 16 procesos / dos carriles colisionantes.

Lanzado por `test_concurrencia_dieciseis_agentes_dos_carriles_colisionan.py`.
Mezcla, bajo un solo proceso real, los tres verbos que el execution doc de
v1.0 pide medir juntos (G8/G9/G10): eventos nativos, lease+fencing y claim del
outbox — todos contra un recurso/ledger con el MISMO nombre literal en las
DOS lanes, que es la forma en que este repo ya prueba aislamiento (nombres
coincidentes, nunca un carril que nadie más usaría).

Nada se traga: cada excepción no prevista se REPORTA en el JSON de salida, y
un intento acotado (reintentos de lease agotados) se declara `truncado`
explícitamente en vez de fingir que completó.

argv: <db> <token> <carril> <clave_compartida> <n_eventos> <n_claims>
stdout: UNA línea JSON.
"""
from __future__ import annotations

import hashlib
import json
import os
import resource
import sys
import time

sys.path.insert(0, os.environ["LLMINBOX_RAIZ"])

import coordination as C                                            # noqa: E402
from _arnes import (GRAMATICA, INTENT, LANES, RECURSO_COLISIONANTE,  # noqa: E402
                    censo)

LEASE_TTL_S = 1
LEASE_REINTENTOS_MAX = 100
LEASE_ESPERA_S = 0.05


def _entry_eid(job: C.ProjectionJob) -> str:
    """Hash sintético de metadatos para ejercitar transiciones del kernel.

    No acredita una escritura del proyector. La fase de recovery del banco
    y test_scale_bench_projection usan bytes reales de MarkdownProjector.
    """
    contenido = json.dumps(
        {"event_id": job.event_id, "receipt_id": job.receipt_id,
         "payload_sha256": job.payload_sha256, "ledger": job.ledger},
        sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(contenido).hexdigest()


def _journal(db: str) -> C.Journal:
    j = C.Journal(db, pepper=os.environ["LLMINBOX_PEPPER"].encode(),
                  busy_timeout_ms=30_000, lane_ledgers=LANES,
                  recipient_resolver=censo, grammar=GRAMATICA)
    j.initialize()
    return j


def main() -> int:
    db, token, carril, clave_compartida, n_eventos, n_claims = sys.argv[1:7]
    n_eventos, n_claims = int(n_eventos), int(n_claims)
    pid = os.getpid()
    out = {"carril": carril, "pid": pid, "errores": []}
    j = _journal(db)
    try:
        # ── 1 · el término COMPARTIDO entre TODOS los agentes de las DOS lanes:
        # La clave vive en (principal, lane, verbo), no sólo en lane. Cada
        # agente conserva su propio recibo y repite mientras los demás escriben.
        a = j.accept_event(token, idempotency_key=clave_compartida,
                           intent={**INTENT, "body": "cuerpo compartido"},
                           ledger="l")
        out["clave_compartida_event_id"] = a.event_id
        out["clave_compartida_replayed"] = a.replayed
        replay = j.accept_event(token, idempotency_key=clave_compartida,
                                intent={**INTENT, "body": "cuerpo compartido"},
                                ledger="l")
        out["replay_mismo_evento"] = replay.replayed and replay.event_id == a.event_id
        out["replay_mismo_recibo"] = replay.receipt_id == a.receipt_id
        if not (out["replay_mismo_evento"] and out["replay_mismo_recibo"]):
            out["errores"].append("el replay propio cambió el evento o su recibo")

        # ── 2 · eventos propios: hacen crecer el ledger colisionante.
        aceptados = 0
        for i in range(n_eventos):
            j.accept_event(token, idempotency_key=f"{carril}-{pid}-{i}",
                           intent={**INTENT, "body": f"evento {carril} {pid} {i}"},
                           ledger="l")
            aceptados += 1
        out["eventos_propios_aceptados"] = aceptados

        # ── 3 · lease + fencing sobre el recurso COLISIONANTE (mismo nombre en
        # las dos lanes). Con 8 procesos por carril compitiendo de verdad por
        # `BEGIN IMMEDIATE`, la mayoría de intentos se topan con `LeaseConflict`
        # mientras otro lo tiene vivo: NO es un error, es el falsador
        # funcionando. Se reintenta acotado y el agotamiento se DECLARA.
        lease = None
        conflictos = 0
        t0 = time.monotonic()
        for _ in range(LEASE_REINTENTOS_MAX):
            try:
                lease = j.acquire_lease(token, RECURSO_COLISIONANTE, ttl_s=LEASE_TTL_S)
                break
            except C.LeaseConflict:
                conflictos += 1
                time.sleep(LEASE_ESPERA_S)
        out["lease_conflictos"] = conflictos
        out["lease_segundos"] = round(time.monotonic() - t0, 3)
        if lease is None:
            out["lease_adquirido"] = False
            out["truncado"] = True
            out["truncado_motivo"] = (
                f"{LEASE_REINTENTOS_MAX} reintentos agotados sin adquirir "
                f"`{RECURSO_COLISIONANTE}`: NO se finge una adquisición que no ocurrió")
            out["fencing_token"] = None
        else:
            out["lease_adquirido"] = True
            out["fencing_token"] = lease.fencing_token
            assert lease.lane == carril, f"lease.lane={lease.lane!r} != {carril!r}"
            # ⊕ el propio token vale DENTRO de su carril.
            j.check_fence(token, RECURSO_COLISIONANTE, lease.fencing_token)
            j.release_lease(token, RECURSO_COLISIONANTE)

        # ── 4 · claim del outbox: NINGÚN item claimado por esta sesión puede
        # pertenecer a la lane hermana. Ésa es la frontera que este falsador
        # existe para ejercitar bajo concurrencia real, no en un mock.
        claims_ok = claims_carril_ajeno = 0
        eids = []
        for _ in range(n_claims):
            job = j.claim_outbox(token, lease_s=30)
            if job is None:
                continue
            if job.lane != carril:
                claims_carril_ajeno += 1
                out["errores"].append(
                    f"claim_outbox devolvió lane={job.lane!r} a una sesión de {carril!r}")
                continue
            eid = _entry_eid(job)
            eids.append(eid)
            j.mark_materialized(token, job.event_id, entry_eid=eid,
                               ledger=job.ledger, claim_token=job.claim_token)
            j.mark_indexed(token, job.event_id)
            claims_ok += 1
        out["claims_ok"] = claims_ok
        out["claims_carril_ajeno"] = claims_carril_ajeno
        out["entry_eids"] = eids
        out["ok"] = not out["errores"]
    except Exception as e:                            # se REPORTA, no se traga
        out["ok"] = False
        out["errores"].append(f"{type(e).__name__}: {e}")
    finally:
        # MEMORIA PROPIA del agente: RUSAGE_CHILDREN del banco sólo ve el
        # MÁXIMO de UN hijo, no responde a «cuánta memoria usa la flota».
        # Cada worker se acredita su pico y el banco lo agrega. ru_maxrss
        # viene en bytes (darwin) o KiB (linux) — se normaliza a bytes.
        ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        out["rss_bytes"] = ru if sys.platform == "darwin" else ru * 1024
        j.close()
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
