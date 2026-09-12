"""Falsador de G8/G9/G10 · 16 agentes concurrentes, dos carriles colisionantes.

El execution doc de v1.0 (`docs/V1.0-EXECUTION.md`) fija el envite: "sixteen
concurrent agents and two deliberately conflicting lanes" (G10), y la
pregunta de producto §5: "Can any identity, result, ... or recovery action
cross a lane?". Este falsador lanza DIECISÉIS PROCESOS reales — no dieciséis
llamadas secuenciales — ocho por carril, contra un recurso y un ledger con el
MISMO nombre literal en las dos lanes, y comprueba que ninguna de las tres
superficies mezcladas (idempotencia, fencing, claim del outbox) cruza.

Por qué PROCESOS: la exclusión que aquí se mide es entre CONEXIONES de
SQLite (`BEGIN IMMEDIATE`), y un hilo puede pasar por el GIL en vez de por
ella — la misma razón que ya declara
`tests/journal/test_concurrencia_20_procesos.py`.
"""
from __future__ import annotations

from ._arnes import (CAPS_RUNTIME, CARRIL_A, CARRIL_B, GRAMATICA, LANES, PEPPER,
                     RECURSO_COLISIONANTE, censo, journal, lanza_paralelo,
                     sesiones, ultima_linea_json)
import coordination as C

N_POR_CARRIL = 8
N_EVENTOS_POR_AGENTE = 5
N_CLAIMS_POR_AGENTE = 2
CLAVE_COMPARTIDA = "clave-compartida-colision-16"


def _credenciales(carril: str, n: int) -> list[dict]:
    return [{"credential": f"cred-{carril}-{i}", "principal": f"agente-{carril}-{i}",
             "role": "be", "lane": carril, "capabilities": CAPS_RUNTIME}
            for i in range(n)]


def test_dieciseis_agentes_dos_carriles_colisionantes_no_cruzan(tmp_path):
    j = journal(tmp_path)
    # ORDEN del arnés (`tests/journal/_arnes.sesiones`): se LIGAN TODAS las
    # credenciales de los DOS carriles y sólo DESPUÉS se emiten TODAS las
    # sesiones. Dos llamadas `sesiones()` consecutivas —como estaba— son
    # exactamente el defecto que `sesiones()` existe para evitar: cada
    # ligadura efectiva sube la generación global y MATA toda sesión abierta
    # antes, así que la segunda llamada dejaba los 8 tokens del carril A como
    # cadáveres y este falsador habría medido su propia muerte (AuthError en
    # 8 procesos), no la contención que dice medir.
    specs = _credenciales(CARRIL_A, N_POR_CARRIL) + _credenciales(CARRIL_B, N_POR_CARRIL)
    tokens_todos = [s.token for s in sesiones(j, *specs)]
    tokens_a = tokens_todos[:N_POR_CARRIL]
    tokens_b = tokens_todos[N_POR_CARRIL:]
    db = j.path
    j.close()                       # el padre suelta la conexión antes de la carrera

    argvs = (
        [[db, tok, CARRIL_A, CLAVE_COMPARTIDA, str(N_EVENTOS_POR_AGENTE),
          str(N_CLAIMS_POR_AGENTE)] for tok in tokens_a]
        + [[db, tok, CARRIL_B, CLAVE_COMPARTIDA, str(N_EVENTOS_POR_AGENTE),
            str(N_CLAIMS_POR_AGENTE)] for tok in tokens_b]
    )
    assert len(argvs) == 16, "el envite de G10 es dieciséis agentes, no otro número"
    salidas = lanza_paralelo("_worker_carriles_colisionan.py", argvs)

    # ── control de forma: cada proceso terminó limpio y reportó JSON.
    reportes = []
    for s in salidas:
        assert s["returncode"] == 0, (
            f"agente murió rc={s['returncode']}\nstderr:\n{s['stderr']}")
        reportes.append(ultima_linea_json(s["stdout"], stderr=s["stderr"]))
    for r in reportes:
        assert r["ok"], f"agente {r.get('carril')}/{r.get('pid')} reportó error: {r['errores']}"

    por_carril = {CARRIL_A: [r for r in reportes if r["carril"] == CARRIL_A],
                 CARRIL_B: [r for r in reportes if r["carril"] == CARRIL_B]}
    assert len(por_carril[CARRIL_A]) == N_POR_CARRIL
    assert len(por_carril[CARRIL_B]) == N_POR_CARRIL

    # ── 1 · CLAIM DEL OUTBOX: cero cruces, bajo contención real. Ésta es la
    # aserción central del falsador — no un efecto colateral de otra cosa.
    total_claims_ajeno = sum(r["claims_carril_ajeno"] for r in reportes)
    assert total_claims_ajeno == 0, (
        f"{total_claims_ajeno} claims de outbox devolvieron la lane equivocada")
    assert all(r["claims_ok"] == N_CLAIMS_POR_AGENTE for r in reportes), \
        "el control de aislamiento exige claims propios efectivos de cada agente"

    # ── 2 · La autoridad es (principal_id, lane, verb, key): misma clave de
    # ocho principales distintos NO fusiona sus eventos. Cada principal sí
    # conserva evento/recibo al repetir, y ambas lanes permanecen disjuntas.
    for carril, reps in por_carril.items():
        ids = {r["clave_compartida_event_id"] for r in reps}
        assert len(ids) == N_POR_CARRIL, f"{carril}: se mezclaron principales distintos"
        no_replayed = [r for r in reps if not r["clave_compartida_replayed"]]
        assert len(no_replayed) == N_POR_CARRIL
        assert all(r["replay_mismo_evento"] and r["replay_mismo_recibo"] for r in reps)
    ids_a = {r["clave_compartida_event_id"] for r in por_carril[CARRIL_A]}
    ids_b = {r["clave_compartida_event_id"] for r in por_carril[CARRIL_B]}
    assert ids_a.isdisjoint(ids_b), "la clave compartida cruzó de carril"

    # ── 2·bis · Hash sintético por evento para el ensayo de transiciones del
    # kernel. No acredita bytes proyectados: eso se mide separadamente con
    # MarkdownProjector en test_scale_bench_projection.py.
    for carril, reps in por_carril.items():
        eids = [e for r in reps for e in r["entry_eids"]]
        drenados = sum(r["claims_ok"] for r in reps)
        assert len(eids) == drenados, (
            f"{carril}: {len(eids)} eids reportados para {drenados} items drenados")
        assert len(set(eids)) == len(eids), (
            f"{carril}: eids repetidos entre event_ids distintos: "
            f"el hash no deriva del contenido")
        assert all(len(e) == 64 and all(c in "0123456789abcdef" for c in e)
                   for e in eids), f"{carril}: entry_eid no es hex de 64"

    # ── 3 · FENCING bajo contención real: dentro de un carril, los tokens
    # adquiridos son un prefijo de 1..k SIN REPETIR (monotonicidad real bajo
    # 8 procesos compitiendo por `BEGIN IMMEDIATE`, no de dos llamadas
    # secuenciales). Un `truncado` descalifica la envolvente completa.
    for carril, reps in por_carril.items():
        adquiridos = sorted(r["fencing_token"] for r in reps if r["lease_adquirido"])
        truncados = [r for r in reps if not r["lease_adquirido"]]
        for t in truncados:
            assert t.get("truncado") is True and t.get("truncado_motivo"), (
                f"{carril}/{t['pid']}: no adquirió lease pero no lo DECLARA truncado")
        assert not truncados, f"{carril}: la flota no completó todas las adquisiciones"
        assert adquiridos == list(range(1, len(adquiridos) + 1)), (
            f"{carril}: fencing no monótono/sin huecos bajo concurrencia real: {adquiridos}")
        assert len(adquiridos) >= 1, f"{carril}: NINGÚN agente adquirió el lease colisionante"

    # ── 4 · invariante GLOBAL del outbox por carril, leído de una foto fresca
    # tras la carrera: `pending == aceptados - drenados`. Es un ángulo
    # ARITMÉTICO independiente del punto 1 — si algo se coló de una lane a
    # otra al drenar, esta cuenta deja de cuadrar aunque el punto 1 no lo
    # hubiera atrapado por casualidad de orden.
    j2 = C.Journal(db, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo,
                  grammar=GRAMATICA)
    j2.initialize()
    try:
        for carril, reps, tok in ((CARRIL_A, por_carril[CARRIL_A], tokens_a[0]),
                                  (CARRIL_B, por_carril[CARRIL_B], tokens_b[0])):
            aceptados = sum(r["eventos_propios_aceptados"] for r in reps) + len(reps)
            drenados = sum(r["claims_ok"] for r in reps)
            counts = j2.outbox_counts(tok)
            assert counts.lane == carril
            assert counts.failed == 0
            assert counts.pending == aceptados - drenados, (
                f"{carril}: pending={counts.pending}, esperado {aceptados - drenados} "
                f"(aceptados={aceptados}, drenados={drenados})")
    finally:
        j2.close()
