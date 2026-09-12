"""Falsadores de la BARRERA DE ADMISIÓN (`events.accept` · `outbox.requeue`).

Regla de la casa: un ⊕ acredita que el instrumento LEE; el ⊖ es el mutante que
DEBE hacer fallar la prueba. Aquí el ⊖ que importa casi siempre es el mismo —
sacar la comprobación de la transacción Core y dejarla fuera (o en la pasarela)—
y por eso las carreras se hacen con el seam `_gancho_carrera`, que dispara ENTRE
el chequeo barato y el `BEGIN IMMEDIATE`. Sin él habría que dormir, y un test que
duerme mide la paciencia del CI, no la linealización.
"""
from __future__ import annotations

import sqlite3
import sys
import threading

import pytest

import coordination as C
from ._arnes import (GRAMATICA, INTENT, LANES, PEPPER, censo, journal,
                     sesiones)

ACCEPT = "events.accept"
REQUEUE = "outbox.requeue"
ADMISION = (C.CAP_ADMISSION_OPERATOR,)
RUNTIME = (C.CAP_OUTBOX_WORKER, C.CAP_INDEXER, C.CAP_OUTBOX_OPERATOR)


def montaje(tmp_path, *, lane="llminbox", abierta=False, **kw):
    """Journal con la barrera CERRADA por defecto y dos sesiones: escritora y
    operadora de la barrera.

    Las dos se emiten DESPUÉS de ligar las dos credenciales: una ligadura sube
    la generación del mapa e invalidaría la sesión emitida antes.
    """
    j = journal(tmp_path, admision=False, **kw)
    escritor, operador = sesiones(
        j,
        {"credential": "cred-w", "principal": "backend", "role": "be",
         "lane": lane, "capabilities": RUNTIME},
        {"credential": "cred-adm", "principal": "admision", "role": "infra",
         "lane": lane, "capabilities": ADMISION},
    )
    if abierta:
        for verbo in C.ADMISSION_VERBS:
            j.open_admission(operador.token, verbo, reason_code="ROLLOUT")
    return j, escritor, operador


def filas(j):
    return [tuple(r) for r in j._connect().execute(
        "SELECT lane, verb, epoch, state, origin, operator, runtime_instance,"
        " reason_code FROM admission_history ORDER BY lane, verb, epoch")]


def cuenta(j, tabla):
    return j._connect().execute(f"SELECT COUNT(*) c FROM {tabla}").fetchone()["c"]


# ══ 1 · LA AUSENCIA ES CIERRE ═══════════════════════════════════════════════
def test_sin_fila_de_historial_la_puerta_esta_cerrada(tmp_path):
    j, w, _ = montaje(tmp_path)
    assert filas(j) == [], "el montaje tiene que partir de la AUSENCIA"
    with pytest.raises(C.AdmissionClosed):
        j.accept_event(w.token, idempotency_key="k1", intent=INTENT,
                       ledger="llminbox")
    for tabla in ("events", "outbox", "idempotency"):
        assert cuenta(j, tabla) == 0, f"{tabla} creció con la puerta cerrada"
    j.close()


def test_con_la_puerta_abierta_el_mismo_evento_entra(tmp_path):
    """⊕ CONTROL. Sin esto, el test de arriba no distingue «la barrera niega» de
    «este montaje no acepta nada»."""
    j, w, _ = montaje(tmp_path, abierta=True)
    a = j.accept_event(w.token, idempotency_key="k1", intent=INTENT,
                       ledger="llminbox")
    assert a.replayed is False and cuenta(j, "events") == 1
    j.close()


def test_la_lectura_de_estado_dice_closed_sin_fila(tmp_path):
    j, w, _ = montaje(tmp_path)
    estado = j.admission(w.token, ACCEPT)
    assert (estado.state, estado.epoch) == ("closed", 0)
    assert estado.lane == "llminbox" and estado.verb == ACCEPT
    j.close()


# ══ 2 · EL REPLAY SIGUE RESPONDIENDO CON LA PUERTA CERRADA ══════════════════
def test_un_replay_idempotente_devuelve_su_recibo_con_el_gate_cerrado(tmp_path):
    """La barrera cierra la ENTRADA, no la RESPUESTA a lo que ya entró.

    ⊖ de la clase: con la comprobación puesta ANTES del bloque de idempotencia,
    esta llamada levantaría `AdmissionClosed` y el cliente que sólo reintenta
    —porque no recibió la respuesta— se quedaría sin poder averiguar nunca qué
    pasó con su primera petición.
    """
    j, w, op = montaje(tmp_path, abierta=True)
    a = j.accept_event(w.token, idempotency_key="k1", intent=INTENT,
                       ledger="llminbox")
    j.close_admission(op.token, ACCEPT, reason_code="INCIDENT")

    b = j.accept_event(w.token, idempotency_key="k1", intent=INTENT,
                       ledger="llminbox")
    assert b.replayed is True and b.event_id == a.event_id
    assert b.receipt_id == a.receipt_id
    assert cuenta(j, "events") == 1, "el replay escribió un evento nuevo"

    # …y una clave NUEVA con la puerta cerrada NO entra: el replay es la única
    # excepción, no un agujero por el que pase cualquier cosa.
    with pytest.raises(C.AdmissionClosed):
        j.accept_event(w.token, idempotency_key="k2", intent=INTENT,
                       ledger="llminbox")
    assert cuenta(j, "events") == 1
    j.close()


def test_el_conflicto_de_idempotencia_gana_al_gate_cerrado(tmp_path):
    """Misma clave y cuerpo DISTINTO sigue siendo `IdempotencyConflict`.

    Devolver `AdmissionClosed` aquí escondería un conflicto real detrás de un
    estado operacional: el cliente creería que basta con esperar a que abran.
    """
    j, w, op = montaje(tmp_path, abierta=True)
    j.accept_event(w.token, idempotency_key="k1", intent=INTENT, ledger="llminbox")
    j.close_admission(op.token, ACCEPT, reason_code="INCIDENT")
    with pytest.raises(C.IdempotencyConflict):
        j.accept_event(w.token, idempotency_key="k1",
                       intent={**INTENT, "body": "otro"}, ledger="llminbox")
    assert cuenta(j, "events") == 1
    j.close()


# ══ 3 · REQUEUE GATEADO · EL DRENAJE NO ═════════════════════════════════════
def _un_item_failed(j, w, max_attempts_alcanzado=True):
    a = j.accept_event(w.token, idempotency_key="kf", intent=INTENT,
                       ledger="llminbox")
    trabajo = j.claim_outbox(w.token)
    j.mark_outbox_failed(w.token, a.event_id, error="PROJECTOR_IO_ERROR",
                         claim_token=trabajo.claim_token)
    return a


def test_requeue_pasa_por_la_barrera_y_abandon_no(tmp_path):
    j, w, op = montaje(tmp_path, abierta=True, max_attempts=1)
    a = _un_item_failed(j, w)
    assert j._connect().execute("SELECT state FROM outbox WHERE event_id=?",
                                (a.event_id,)).fetchone()["state"] == "failed"

    j.close_admission(op.token, REQUEUE, reason_code="MAINTENANCE")
    with pytest.raises(C.AdmissionClosed):
        j.requeue_outbox(w.token, a.event_id, reason="reintento")
    assert j._connect().execute("SELECT state FROM outbox WHERE event_id=?",
                                (a.event_id,)).fetchone()["state"] == "failed"
    assert cuenta(j, "outbox_operations") == 0

    # ⊕ el DRENAJE sigue: si `abandon` cayera con la puerta cerrada, un carril
    # cerrado no podría vaciarse nunca y el sello sería inalcanzable.
    j.abandon_outbox(w.token, a.event_id, reason="se renuncia")
    assert j._connect().execute("SELECT state FROM outbox WHERE event_id=?",
                                (a.event_id,)).fetchone()["state"] == "abandoned"
    j.close()


def test_claim_materialized_y_failed_drenan_con_las_dos_puertas_cerradas(tmp_path):
    j, w, op = montaje(tmp_path, abierta=True)
    a = j.accept_event(w.token, idempotency_key="kd", intent=INTENT,
                       ledger="llminbox")
    b = j.accept_event(w.token, idempotency_key="kd2",
                       intent={**INTENT, "head": "otra"}, ledger="llminbox")
    for verbo in C.ADMISSION_VERBS:
        j.close_admission(op.token, verbo, reason_code="DRAIN_FOR_ROLLBACK")

    t1 = j.claim_outbox(w.token)
    assert t1 is not None, "claim murió con la puerta cerrada"
    j.mark_materialized(w.token, t1.event_id, entry_eid="e" * 64,
                        ledger="llminbox", claim_token=t1.claim_token,
                        byte_off=0)
    t2 = j.claim_outbox(w.token)
    j.mark_outbox_failed(w.token, t2.event_id, error="PROJECTOR_IO_ERROR",
                         claim_token=t2.claim_token)
    estados = dict(j._connect().execute(
        "SELECT event_id, state FROM outbox").fetchall())
    assert set(estados.values()) == {"materialized", "pending"} or \
        set(estados.values()) == {"materialized", "failed"}
    assert {a.event_id, b.event_id} == set(estados)
    j.close()


# ══ 4 · CAPACIDAD DEDICADA ══════════════════════════════════════════════════
def test_el_operador_de_cola_no_puede_mover_la_barrera(tmp_path):
    """`RUNTIME` incluye `CAP_OUTBOX_OPERATOR`: sacar un item de la cola no es
    cerrar el carril, y este test es lo que impide que se fundan."""
    j, w, op = montaje(tmp_path, abierta=True)
    assert C.CAP_OUTBOX_OPERATOR in RUNTIME
    for verbo in C.ADMISSION_VERBS:
        with pytest.raises(C.PolicyDenied):
            j.close_admission(w.token, verbo, reason_code="INCIDENT")
    assert filas(j) == [
        ("llminbox", ACCEPT, 1, "open", "operator", op.principal_id,
         op.runtime_instance, "ROLLOUT"),
        ("llminbox", REQUEUE, 1, "open", "operator", op.principal_id,
         op.runtime_instance, "ROLLOUT"),
    ], "el intento del operador de cola movió el historial"
    j.close()


def test_el_operador_de_barrera_si_puede(tmp_path):
    """⊕ CONTROL del anterior."""
    j, _, op = montaje(tmp_path, abierta=True)
    estado = j.close_admission(op.token, ACCEPT, reason_code="INCIDENT")
    assert (estado.state, estado.epoch) == ("closed", 2)
    j.close()


# ══ 5 · APPEND-ONLY, EPOCH MONÓTONO, ESTADO = EPOCH MÁXIMO ══════════════════
def test_el_historial_no_sobrescribe_y_el_epoch_es_monotono(tmp_path):
    j, _, op = montaje(tmp_path)
    j.open_admission(op.token, ACCEPT, reason_code="ROLLOUT")
    j.close_admission(op.token, ACCEPT, reason_code="INCIDENT")
    j.open_admission(op.token, ACCEPT, reason_code="ROLLOUT")
    historial = [f for f in filas(j) if f[1] == ACCEPT]
    assert [(f[2], f[3]) for f in historial] == [
        (1, "open"), (2, "closed"), (3, "open")]
    assert {f[4] for f in historial} == {"operator"}
    assert j.admission(op.token, ACCEPT).state == "open"
    assert j.admission(op.token, ACCEPT).epoch == 3
    j.close()


def test_una_transicion_al_mismo_estado_no_quema_epoch(tmp_path):
    j, _, op = montaje(tmp_path, abierta=True)
    with pytest.raises(C.AdmissionConflict):
        j.open_admission(op.token, ACCEPT, reason_code="ROLLOUT")
    assert len([f for f in filas(j) if f[1] == ACCEPT]) == 1
    j.close()


def test_el_carril_y_el_verbo_son_independientes(tmp_path):
    j, w, op = montaje(tmp_path, abierta=True)
    j.close_admission(op.token, ACCEPT, reason_code="INCIDENT")
    assert j.admission(op.token, REQUEUE).state == "open"
    with pytest.raises(C.AdmissionClosed):
        j.accept_event(w.token, idempotency_key="k1", intent=INTENT,
                       ledger="llminbox")

    # Otro carril, misma base: su puerta no se movió.
    otro, w2, _ = montaje(tmp_path, lane="carril-uno")
    otro.close()
    j.close()


# ══ 6 · SELLO: ESTADO, EPOCH Y DRENADO EN LA MISMA TRANSACCIÓN ══════════════
def test_no_se_sella_desde_open(tmp_path):
    j, _, op = montaje(tmp_path, abierta=True)
    with pytest.raises(C.AdmissionConflict):
        j.seal_admission(op.token, ACCEPT, reason_code="DRAIN_FOR_ROLLBACK",
                         expected_epoch=1)
    assert j.admission(op.token, ACCEPT).state == "open"
    j.close()


def test_no_se_sella_con_el_carril_sin_drenar(tmp_path):
    j, w, op = montaje(tmp_path, abierta=True)
    j.accept_event(w.token, idempotency_key="k1", intent=INTENT, ledger="llminbox")
    estado = j.close_admission(op.token, ACCEPT, reason_code="DRAIN_FOR_ROLLBACK")
    with pytest.raises(C.AdmissionConflict):
        j.seal_admission(op.token, ACCEPT, reason_code="DRAIN_FOR_ROLLBACK",
                         expected_epoch=estado.epoch)
    assert j.admission(op.token, ACCEPT).state == "closed"
    j.close()


def test_se_sella_cerrado_y_drenado(tmp_path):
    """⊕ CONTROL: el sello es ALCANZABLE. Sin esto, los dos ⊖ de arriba podrían
    estar midiendo «no se sella nunca»."""
    j, w, op = montaje(tmp_path, abierta=True)
    a = j.accept_event(w.token, idempotency_key="k1", intent=INTENT,
                       ledger="llminbox")
    t = j.claim_outbox(w.token)
    j.mark_materialized(w.token, a.event_id, entry_eid="e" * 64,
                        ledger="llminbox", claim_token=t.claim_token, byte_off=0)
    estado = j.close_admission(op.token, ACCEPT, reason_code="DRAIN_FOR_ROLLBACK")
    sellado = j.seal_admission(op.token, ACCEPT,
                               reason_code="DRAIN_FOR_ROLLBACK",
                               expected_epoch=estado.epoch)
    assert (sellado.state, sellado.epoch) == ("sealed", 3)
    j.close()


def test_sealed_es_terminal(tmp_path):
    j, w, op = montaje(tmp_path)
    j.seal_admission(op.token, ACCEPT, reason_code="MAINTENANCE",
                     expected_epoch=0)
    for verbo, kw in (("open", {}), ("close", {})):
        with pytest.raises(C.AdmissionConflict):
            getattr(j, f"{verbo}_admission")(op.token, ACCEPT,
                                             reason_code="ROLLOUT")
    with pytest.raises(C.AdmissionClosed):
        j.accept_event(w.token, idempotency_key="k1", intent=INTENT,
                       ledger="llminbox")
    assert len([f for f in filas(j) if f[1] == ACCEPT]) == 1
    j.close()


def test_el_sello_no_se_firma_sin_valla(tmp_path):
    """`None` es «sin valla» en `open`/`close`. Si aquí se colara, el epoch
    obligatorio del sello sería obligatorio sólo en la firma del método."""
    j, _, op = montaje(tmp_path)
    with pytest.raises(C.OperationInvalid):
        j.seal_admission(op.token, ACCEPT, reason_code="MAINTENANCE",
                         expected_epoch=None)
    assert filas(j) == []
    j.close()


def test_el_sello_exige_el_epoch_que_el_operador_vio(tmp_path):
    j, _, op = montaje(tmp_path)
    j.open_admission(op.token, ACCEPT, reason_code="ROLLOUT")     # epoch 1
    j.close_admission(op.token, ACCEPT, reason_code="INCIDENT")   # epoch 2
    with pytest.raises(C.AdmissionConflict):
        j.seal_admission(op.token, ACCEPT, reason_code="DRAIN_FOR_ROLLBACK",
                         expected_epoch=1)
    assert len([f for f in filas(j) if f[1] == ACCEPT]) == 2, "escribió perdiendo"
    j.close()


# ══ 7 · CERO TEXTO LIBRE DURABLE ════════════════════════════════════════════
def test_el_motivo_es_vocabulario_cerrado(tmp_path):
    j, _, op = montaje(tmp_path)
    for motivo in ("porque sí", "", "rollout", "INCIDENT ", None, 7):
        with pytest.raises(C.OperationInvalid):
            j.open_admission(op.token, ACCEPT, reason_code=motivo)
    assert filas(j) == []
    j.close()


def test_la_tabla_durable_no_tiene_ninguna_columna_de_texto_libre(tmp_path):
    """El censo va por la FORMA de la tabla, no por lo que hoy se escribe: una
    columna libre añadida mañana no daría error, sólo una fuga."""
    j, _, _ = montaje(tmp_path)
    columnas = {c[1] for c in j._connect().execute(
        "PRAGMA table_info(admission_history)")}
    assert columnas == {"lane", "verb", "epoch", "state", "origin", "operator",
                        "runtime_instance", "reason_code", "at"}
    ddl = j._connect().execute(
        "SELECT sql FROM sqlite_master WHERE name='admission_history'"
    ).fetchone()[0].lower()
    assert "reason " not in ddl and "detail" not in ddl and "message" not in ddl
    j.close()


def test_un_verbo_fuera_de_la_barrera_no_crea_una_puerta(tmp_path):
    j, _, op = montaje(tmp_path)
    for verbo in ("outbox.abandon", "events.ack", "", "events.accept "):
        with pytest.raises(C.OperationInvalid):
            j.open_admission(op.token, verbo, reason_code="ROLLOUT")
        with pytest.raises(C.OperationInvalid):
            j.admission(op.token, verbo)
    assert filas(j) == []
    j.close()


# ══ 8 · CARRERAS · CON EL SEAM, SIN UN SOLO `sleep` ═════════════════════════
def test_un_close_en_la_ventana_del_accept_LO_PARA(tmp_path):
    """LA prueba de la linealización.

    El seam dispara ENTRE el chequeo barato y el `BEGIN IMMEDIATE` de
    `accept_event`. El `close` COMITEA ahí dentro, así que la transacción del
    accept nace después de él.

    ⊖ DE LA CLASE: con la comprobación fuera de la transacción Core —o sólo en
    la pasarela— este accept ENTRARÍA, porque la puerta estaba abierta cuando se
    miró. Es exactamente el agujero que «el check DEBE vivir dentro de su
    transacción» existe para cerrar.
    """
    j, w, op = montaje(tmp_path, abierta=True)
    disparos = []

    def cierra_una_vez():
        if disparos:
            return
        disparos.append(True)
        j.close_admission(op.token, ACCEPT, reason_code="INCIDENT")

    j._gancho_carrera = cierra_una_vez
    try:
        with pytest.raises(C.AdmissionClosed):
            j.accept_event(w.token, idempotency_key="k1", intent=INTENT,
                           ledger="llminbox")
    finally:
        j._gancho_carrera = None
    assert disparos == [True], "el seam no llegó a dispararse"
    assert cuenta(j, "events") == 0 and cuenta(j, "outbox") == 0
    j.close()


def test_un_open_en_la_ventana_del_accept_LO_DEJA_PASAR(tmp_path):
    """⊕ CONTROL de la anterior, y no es simétrico por casualidad: prueba que el
    estado se lee DENTRO de la transacción y no se arrastra de la lectura de
    antes. Si el accept trajera cacheado el `closed` que había al empezar, esto
    fallaría."""
    j, w, op = montaje(tmp_path)
    disparos = []

    def abre_una_vez():
        if disparos:
            return
        disparos.append(True)
        j.open_admission(op.token, ACCEPT, reason_code="ROLLOUT")

    j._gancho_carrera = abre_una_vez
    try:
        a = j.accept_event(w.token, idempotency_key="k1", intent=INTENT,
                           ledger="llminbox")
    finally:
        j._gancho_carrera = None
    assert a.replayed is False and cuenta(j, "events") == 1
    j.close()


def test_un_close_en_la_ventana_del_requeue_LO_PARA(tmp_path):
    j, w, op = montaje(tmp_path, abierta=True, max_attempts=1)
    a = _un_item_failed(j, w)
    disparos = []

    def cierra_una_vez():
        if disparos:
            return
        disparos.append(True)
        j.close_admission(op.token, REQUEUE, reason_code="MAINTENANCE")

    j._gancho_carrera = cierra_una_vez
    try:
        with pytest.raises(C.AdmissionClosed):
            j.requeue_outbox(w.token, a.event_id, reason="reintento")
    finally:
        j._gancho_carrera = None
    assert disparos == [True]
    assert j._connect().execute("SELECT state FROM outbox WHERE event_id=?",
                                (a.event_id,)).fetchone()["state"] == "failed"
    j.close()


def test_una_transicion_en_la_ventana_del_seal_LO_HACE_PERDER(tmp_path):
    """La valla del epoch, ejercitada en la ventana real y no de palabra."""
    j, _, op = montaje(tmp_path)
    j.open_admission(op.token, ACCEPT, reason_code="ROLLOUT")      # 1
    visto = j.close_admission(op.token, ACCEPT, reason_code="INCIDENT")  # 2
    disparos = []

    def compite_una_vez():
        if disparos:
            return
        disparos.append(True)
        j.open_admission(op.token, ACCEPT, reason_code="ROLLOUT")  # 3

    j._gancho_carrera = compite_una_vez
    try:
        with pytest.raises(C.AdmissionConflict):
            j.seal_admission(op.token, ACCEPT,
                             reason_code="DRAIN_FOR_ROLLBACK",
                             expected_epoch=visto.epoch)
    finally:
        j._gancho_carrera = None
    assert [f[3] for f in filas(j) if f[1] == ACCEPT] == ["open", "closed", "open"]
    j.close()


def test_el_sello_cuenta_el_drenado_DENTRO_de_su_transaccion(tmp_path):
    """⊕ el contador NO se toma antes: el `abandon` que drena el carril ocurre
    en la ventana, y el sello —que empezó con la cola SIN drenar— tiene que
    verla ya vacía y firmar.

    ⊖ con el `COUNT` tomado antes de `_tras_precheck`, este sello fallaría
    citando un pendiente que ya no existe."""
    j, w, op = montaje(tmp_path, abierta=True)
    a = j.accept_event(w.token, idempotency_key="k1", intent=INTENT,
                       ledger="llminbox")
    cerrado = j.close_admission(op.token, ACCEPT, reason_code="DRAIN_FOR_ROLLBACK")
    assert j._connect().execute(
        "SELECT COUNT(*) c FROM outbox WHERE state='pending'").fetchone()["c"] == 1
    disparos = []

    def drena_una_vez():
        if disparos:
            return
        disparos.append(True)
        j.abandon_outbox(w.token, a.event_id, reason="se renuncia")

    j._gancho_carrera = drena_una_vez
    try:
        sellado = j.seal_admission(op.token, ACCEPT,
                                   reason_code="DRAIN_FOR_ROLLBACK",
                                   expected_epoch=cerrado.epoch)
    finally:
        j._gancho_carrera = None
    assert disparos == [True] and sellado.state == "sealed"
    j.close()


# ══ 9 · ESQUEMA v6 Y MIGRACIÓN DE PARES HISTÓRICOS ══════════════════════════
def test_una_base_nueva_es_v6_conocida_con_la_barrera_en_su_manifiesto(tmp_path):
    """Este test es lo que ata los FRAGMENTOS del manifiesto al DDL real: si un
    CHECK o una FK del manifiesto no casa, `_clasificar` cae a `indeterminada`
    y esto se pone rojo."""
    j, _, _ = montaje(tmp_path)
    # el 6 es la COTA (la barrera nacio en v6), no la version de hoy: quien CLAVA el numero
    # es tests/journal/test_schema_v7_fleet_control.py (5 sitios con == 7). Aqui se DERIVA.
    assert C.DURABLE_V >= 6 and j.stored_durable_v() == C.DURABLE_V
    con = sqlite3.connect(j.path)
    con.row_factory = sqlite3.Row
    try:
        assert C._clasificar(con) == ("conocida", C.DURABLE_V, "")
    finally:
        con.close()
    assert "admission_history" in C.MANIFIESTOS[C.DURABLE_V]["objetos"]
    j.close()


def test_la_migracion_cierra_los_pares_CON_historia_y_solo_esos(tmp_path):
    j, w, op = montaje(tmp_path, abierta=True)
    j.accept_event(w.token, idempotency_key="k1", intent=INTENT, ledger="llminbox")
    ruta = j.path
    j.close()
    _c = sqlite3.connect(ruta); eventos = _c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    _c.close()
    assert eventos > 0, "el sustrato de este test necesita historia que preservar"

    # Se rebaja a v5 CON la forma de v5: sin `admission_history`.
    con = sqlite3.connect(ruta)
    con.execute("DROP TABLE admission_history")
    _degradar_a_v5(con)
    con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('durable_v','5')")
    con.commit()
    con.close()

    # 🔻 antes: «la migracion cierra los pares CON historia». Hoy NO hay migracion de v5
    # (retirada del contrato beta) y lo que se exige es que la retirada se CUMPLA sin
    # tocar los datos que esa v5 ya tenia. @db-mig, 2026-09-11.
    _exige_rechazo_de_v5(ruta, eventos_antes=eventos)


def test_la_migracion_no_inventa_pares_para_carriles_sin_historia(tmp_path):
    """⊖ del anterior: si la migración recorriera la allowlist en vez de la
    HISTORIA, aquí aparecerían filas de `carril-uno` y `carril-dos`."""
    j = journal(tmp_path, admision=False)
    ruta = j.path
    j.close()
    con = sqlite3.connect(ruta)
    con.execute("DROP TABLE admission_history")
    _degradar_a_v5(con)
    con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('durable_v','5')")
    con.commit()
    con.close()

    # 🔻 antes: «la migracion no inventa pares». Sin migracion, lo que se exige es que una
    # v5 SIN historia se rechace IGUAL: el rechazo no depende de lo que la base contenga.
    _exige_rechazo_de_v5(ruta, eventos_antes=0)


# ══ 10 · EL RECHAZO DEJA RASTRO CLASIFICADO ════════════════════════════════
def test_el_rechazo_de_la_barrera_se_audita_con_su_codigo(tmp_path):
    j, w, _ = montaje(tmp_path)
    with pytest.raises(C.AdmissionClosed) as caido:
        j.accept_event(w.token, idempotency_key="k1", intent=INTENT,
                       ledger="llminbox")
    assert caido.value.receipt_id, "un rechazo autenticado sin recibo"
    motivos = {r["reason"] for r in j._connect().execute(
        "SELECT reason FROM denials")}
    assert motivos == {"ADMISSION_CLOSED"}
    assert "ADMISSION_CLOSED" in C.REASON_CODES
    assert "ADMISSION_CONFLICT" in C.REASON_CODES
    j.close()


# ══ 11 · P1 · UNA v5 NO ADOPTA UNA TABLA v6 QUE NO CREÓ ELLA ═══════════════
#
# Reproducido por auditoría sobre `c38e63b` y confirmado por mi mano antes de
# curar: `_m5_a_6` hacía `if not _existe(...): self._recrear(...)`, o sea que
# ADOPTABA la tabla si ya estaba puesta — y entonces su `INSERT OR IGNORE` no
# escribía nada, porque el `epoch 1` ya existía. Una base sellada `v5` con
# `admission_history` plantada y una fila `open` pasaba `initialize`, se sellaba
# `v6` y `accept_event` ENTRABA sin que ningún operador hubiera abierto nada.
#
# El DDL de abajo es el de `c38e63b` LITERAL —sin el CHECK defensivo— porque eso
# es lo que traería un artefacto de downgrade o una tabla plantada. Escribirlo
# con el DDL de HOY haría que el CHECK nuevo tapara el caso y el test mediría la
# defensa en profundidad en vez de la cura.
DDL_ADMISSION_C38E63B = """
CREATE TABLE admission_history (
  lane TEXT NOT NULL,
  verb TEXT NOT NULL CHECK (verb IN ('events.accept','outbox.requeue')),
  epoch INTEGER NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('open','closed','sealed')),
  origin TEXT NOT NULL CHECK (origin IN ('operator','migration')),
  operator TEXT REFERENCES principals(principal_id),
  runtime_instance TEXT REFERENCES runtime_sessions(runtime_instance),
  reason_code TEXT NOT NULL,
  at TEXT NOT NULL,
  CHECK ((origin='operator' AND operator IS NOT NULL AND runtime_instance IS NOT NULL)
      OR (origin='migration' AND operator IS NULL AND runtime_instance IS NULL)),
  PRIMARY KEY (lane, verb, epoch))"""


def _ddl_admision_c38e63b(tabla: str) -> str:
    return DDL_ADMISSION_C38E63B.replace(
        "CREATE TABLE admission_history", f"CREATE TABLE {tabla}", 1)


def _degradar_a_v5(con):
    """Deja la base en la FORMA de v5 Y la sella (HIPOTESIS sdet, 2026-09-11)."""
    m5 = C.MANIFIESTOS[5]
    objetos = set(m5.get("objetos", ())); indices = set(m5.get("indices", ()) or ())
    for tipo, nombre in con.execute(
            "SELECT type,name FROM sqlite_master WHERE type IN ('table','index') "
            "AND name NOT LIKE 'sqlite_%'").fetchall():
        if tipo == "table" and nombre not in objetos:
            con.execute(f"DROP TABLE IF EXISTS {nombre}")
        elif tipo == "index" and nombre not in indices:
            con.execute(f"DROP INDEX IF EXISTS {nombre}")
    fila = con.execute("SELECT sql FROM sqlite_master WHERE name='receipts'").fetchone()
    if fila and "'transition'" in fila[0]:
        idx = [r[0] for r in con.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='receipts' "
            "AND sql IS NOT NULL").fetchall()]
        viejo = fila[0].replace(",'transition'", "").replace(", 'transition'", "")
        con.execute("PRAGMA legacy_alter_table=ON")
        con.execute("ALTER TABLE receipts RENAME TO _receipts_v7")
        con.execute(viejo)
        con.execute("INSERT INTO receipts SELECT * FROM _receipts_v7")
        con.execute("DROP TABLE _receipts_v7")
        con.execute("PRAGMA legacy_alter_table=OFF")
        for sql in idx:
            con.execute(sql.replace("_receipts_v7", "receipts"))


def _v5_con_tabla_plantada(tmp_path, filas_plantadas=(), tabla="admission_history"):
    """Base REAL sellada a `v5` con una `admission_history` puesta a mano.

    `tabla` deja variar el CASING con el que queda escrita en `sqlite_master`.
    Para SQLite `admission_history` y `ADMISSION_HISTORY` son EL MISMO objeto
    —resuelve nombres de tabla sin distinguir mayúsculas—, así que el guard
    tiene que reconocerla sea cual sea la mayúscula con la que se plantó.
    """
    j = journal(tmp_path, admision=False)
    sesiones(j, {"credential": "cred-w", "principal": "backend", "role": "be",
                 "lane": "llminbox", "capabilities": RUNTIME})
    ruta = j.path
    j.close()
    con = sqlite3.connect(ruta)
    _degradar_a_v5(con)                       # la forma de v5 ANTES de plantar la tabla
    con.execute("DROP TABLE IF EXISTS admission_history")
    con.execute(_ddl_admision_c38e63b(tabla))
    for fila in filas_plantadas:
        con.execute(f"INSERT INTO {tabla} VALUES(?,?,?,?,?,?,?,?,?)", fila)
    con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('durable_v','5')")
    con.commit()
    con.close()
    return ruta


def _abre_crudo(ruta):
    return C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, grammar=GRAMATICA,
                     recipient_resolver=censo)


PLANTADA_OPEN = ("llminbox", ACCEPT, 1, "open", "migration", None, None,
                 "SCHEMA_MIGRATION", "2026-09-07T00:00:00Z")


def test_P1_una_v5_con_tabla_plantada_y_fila_open_no_se_adopta(tmp_path):
    """EL falsador exacto del P1: `durable_v=5` + DDL válido + fila `open`."""
    ruta = _v5_con_tabla_plantada(tmp_path, [PLANTADA_OPEN])
    j = _abre_crudo(ruta)
    with pytest.raises(C.MigrationFailed):
        j.initialize()

    # La transacción se deshizo ENTERA: ni se sella ni se toca la fila ajena.
    con = sqlite3.connect(ruta)
    try:
        assert con.execute(
            "SELECT v FROM meta WHERE k='durable_v'").fetchone()[0] == "5"
        assert con.execute(
            "SELECT COUNT(*) FROM admission_history").fetchone()[0] == 1
    finally:
        con.close()

    # Y el rechazo es ESTABLE: reintentar no acaba adoptándola por cansancio.
    with pytest.raises(C.MigrationFailed):
        j.initialize()
    j.close()


# 🩸 P1 nº2 (segunda auditoría, sobre la cura del P1 nº1 de este mismo bloque).
# La FOTO y `_existe` comparaban el nombre por LITERAL de Python. SQLite
# resuelve nombres de tabla SIN distinguir mayúsculas —es el mismo objeto
# para `CREATE TABLE IF NOT EXISTS`, para el `INSERT`, para todo—, así que
# una `ADMISSION_HISTORY` o `Admission_History` plantada colaba el mismo P1
# con otra mayúscula: ni la FOTO la reconocía (la guardaba tal cual vino) ni
# `_existe` la encontraba (buscaba el literal en minúsculas). Confirmado por
# mi mano con un `sqlite3.connect(":memory:")` antes de curar.
@pytest.mark.parametrize("tabla_plantada", [
    "admission_history", "ADMISSION_HISTORY", "Admission_History",
])
def test_P1_una_v5_con_tabla_plantada_en_otra_mayuscula_no_se_adopta(
        tmp_path, tabla_plantada):
    """⊕ el mismo falsador exacto del P1 nº1, variando sólo el CASING con el
    que queda escrita la tabla plantada en `sqlite_master`."""
    ruta = _v5_con_tabla_plantada(tmp_path, [PLANTADA_OPEN], tabla=tabla_plantada)
    j = _abre_crudo(ruta)
    with pytest.raises(C.MigrationFailed):
        j.initialize()

    con = sqlite3.connect(ruta)
    try:
        assert con.execute(
            "SELECT v FROM meta WHERE k='durable_v'").fetchone()[0] == "5"
        assert con.execute(
            f"SELECT COUNT(*) FROM {tabla_plantada}").fetchone()[0] == 1
    finally:
        con.close()
    j.close()


def test_P1_tampoco_se_adopta_una_tabla_plantada_VACIA(tmp_path):
    """⊖ que separa las dos lecturas posibles del test anterior.

    Sin esto, «rechaza» podría estar midiendo «rechaza porque la fila dice
    `open`». Lo que no se adopta es la TABLA: su procedencia no se puede
    demostrar, así que su contenido —haya o no filas— no acredita nada.
    """
    ruta = _v5_con_tabla_plantada(tmp_path)
    with pytest.raises(C.MigrationFailed):
        _abre_crudo(ruta).initialize()


def test_P1_ni_con_una_fila_open_que_el_CHECK_defensivo_deja_pasar(tmp_path):
    """El CHECK no cubre este caso y la cura sí: `origin='operator'` con su
    operador y su sesión REALES pasa cualquier restricción de la base."""
    j = journal(tmp_path, admision=False)
    _, op = sesiones(
        j,
        {"credential": "cred-w", "principal": "backend", "role": "be",
         "lane": "llminbox", "capabilities": RUNTIME},
        {"credential": "cred-adm", "principal": "admision", "role": "infra",
         "lane": "llminbox", "capabilities": ADMISION})
    ruta = j.path
    principal_id, rti = op.principal_id, op.runtime_instance
    j.close()

    con = sqlite3.connect(ruta)
    con.execute("DROP TABLE admission_history")
    con.execute(DDL_ADMISSION_C38E63B)
    con.execute("INSERT INTO admission_history VALUES(?,?,?,?,?,?,?,?,?)",
                ("llminbox", ACCEPT, 1, "open", "operator", principal_id, rti,
                 "ROLLOUT", "2026-09-07T00:00:00Z"))
    _degradar_a_v5(con)
    con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('durable_v','5')")
    con.commit()
    con.close()

    with pytest.raises(C.MigrationFailed):
        _abre_crudo(ruta).initialize()


def _exige_rechazo_de_v5(ruta, *, eventos_antes=None):
    """La forma de la casa para asertar una RETIRADA (README-falsadores-d83ae04.md):
    PRE-ASERTO de que el sustrato se reconoce, y SOLO entonces el rechazo TIPADO;
    pins de post-estado: 0 objetos de v7, sello intacto y datos preservados.

    Sin el pre-aserto, un sustrato roto daria el mismo rojo que la retirada y el test
    pasaria por la razon equivocada."""
    con = sqlite3.connect(ruta)
    assert C._clasificar(con) == ("conocida", 5, ""), "el sustrato dejo de ser una v5 reconocible"
    con.close()
    with pytest.raises(C.MigrationFailed) as e:
        _abre_crudo(ruta).initialize()
    m = str(e.value)
    assert "durable_v=5" in m and "migrador offline" in m, f"el rechazo no es el de la retirada: {m}"
    solo_v7 = set(C.MANIFIESTOS[C.DURABLE_V]["objetos"]) - set(C.MANIFIESTOS[5]["objetos"])
    con = sqlite3.connect(ruta)
    escritos = sorted(t for t in solo_v7 if con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone())
    assert escritos == [], f"el rechazo escribio bytes de v7: {escritos}"
    assert con.execute("SELECT v FROM meta WHERE k='durable_v'").fetchone()[0] == "5", "movio el sello"
    if eventos_antes is not None:
        assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == eventos_antes, (
            "el rechazo se llevo datos por delante")
    con.close()


def test_P1_una_v5_LIMPIA_TAMPOCO_migra_YA(tmp_path):
    """⊕ CONTROL. Sin esto, los tres ⊖ de arriba no distinguen «no adopta una tabla
    ajena» de «ninguna v5 migra ya» — y HOY la respuesta es la segunda.

    🔻 ANTES: `test_P1_una_v5_LIMPIA_si_migra_y_queda_cerrada`, que aseveraba la
    migracion. El contrato beta la RETIRO («solo admite creacion nueva, v6→v7 o v7») y
    @db-mig lo adjudico el 2026-09-11: se aserta la RETIRADA, no su regreso. El nombre
    cambia porque el sentido se INVIRTIO; citarlo por el viejo lleva a lo contrario."""
    j = journal(tmp_path, admision=False)
    w, = sesiones(j, {"credential": "cred-w", "principal": "backend",
                      "role": "be", "lane": "llminbox",
                      "capabilities": RUNTIME})
    ruta = j.path
    j.close()
    con = sqlite3.connect(ruta)
    con.execute("DROP TABLE admission_history")          # forma de v5: sin ella
    _degradar_a_v5(con)
    con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('durable_v','5')")
    con.commit()
    con.close()

    _exige_rechazo_de_v5(ruta)


def test_el_CHECK_defensivo_impide_una_migracion_que_no_sea_closed(tmp_path):
    """Defensa en profundidad, medida en la BASE y no en el código.

    La cura del P1 vive en `_m5_a_6`; este CHECK existe para el día en que
    alguien escriba otra ruta que inserte en esta tabla.
    """
    j, _, _ = montaje(tmp_path)
    con = j._connect()
    for estado in ("open", "sealed"):
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO admission_history(lane,verb,epoch,state,origin,"
                "operator,runtime_instance,reason_code,at)"
                " VALUES('llminbox',?,9,?,'migration',NULL,NULL,"
                "'SCHEMA_MIGRATION','2026-09-07T00:00:00Z')", (ACCEPT, estado))
    # ⊕ `closed` sí entra: el CHECK acota el estado, no prohíbe la migración.
    con.execute(
        "INSERT INTO admission_history(lane,verb,epoch,state,origin,operator,"
        "runtime_instance,reason_code,at) VALUES('llminbox',?,9,'closed',"
        "'migration',NULL,NULL,'SCHEMA_MIGRATION','2026-09-07T00:00:00Z')",
        (ACCEPT,))
    assert C.MANIFIESTOS[6]["checks"]["admission_history"] >= {
        "check(origin<>'migration'orstate='closed')"}
    j.close()


# ══ 12 · P1 · VOCABULARIO DURABLE CON TIPO EXACTO ═════════════════════════
class _StrHostil(str):
    """Bytes propios hostiles, igualdad/hash que fingen un miembro permitido."""

    def __new__(cls, value, fingido):
        obj = super().__new__(cls, value)
        obj.fingido = fingido
        return obj

    def __hash__(self):
        return hash(self.fingido)

    def __eq__(self, other):
        return other == self.fingido


@pytest.mark.parametrize("motivo", [
    _StrHostil("SECRET-FREE-TEXT", "ROLLOUT"), [], {}, True, 1,
])
def test_P1_reason_code_exige_str_exacto_y_error_cerrado(tmp_path, motivo):
    j, _, op = montaje(tmp_path)
    with pytest.raises(C.OperationInvalid):
        j.open_admission(op.token, ACCEPT, reason_code=motivo)
    assert filas(j) == [], "el motivo hostil dejó una transición durable"
    j.close()


@pytest.mark.parametrize("verbo", [
    _StrHostil("SECRET-VERB", ACCEPT), [], {}, True, 1,
])
def test_P1_verb_exige_str_exacto_y_no_llega_a_sqlite(tmp_path, verbo):
    j, _, op = montaje(tmp_path)
    with pytest.raises(C.OperationInvalid):
        j.admission(op.token, verbo)
    with pytest.raises(C.OperationInvalid):
        j.open_admission(op.token, verbo, reason_code="ROLLOUT")
    assert filas(j) == []
    j.close()


def test_el_DDL_impide_reason_code_fuera_del_vocabulario(tmp_path):
    j, _, op = montaje(tmp_path)
    con = j._connect()
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO admission_history(lane,verb,epoch,state,origin,"
            "operator,runtime_instance,reason_code,at) VALUES(?,?,1,'open',"
            "'operator',?,?,?,'2026-09-07T00:00:00Z')",
            ("llminbox", ACCEPT, op.principal_id, op.runtime_instance,
             "SECRET-FREE-TEXT"))
    assert C.MANIFIESTOS[6]["checks"]["admission_history"] >= {
        "check(reason_codein('rollout','incident','maintenance',"
        "'drain_for_rollback','schema_migration'))"}
    j.close()


def test_el_DDL_ata_SCHEMA_MIGRATION_al_origin_en_ambas_direcciones(tmp_path):
    """Ni el SQL crudo puede falsificar el origen reservado, en ningún lado."""
    j, _, op = montaje(tmp_path)
    con = j._connect()
    insert = (
        "INSERT INTO admission_history(lane,verb,epoch,state,origin,"
        "operator,runtime_instance,reason_code,at) VALUES(?,?,?,?,?,?,?,?,?)")
    at = "2026-09-07T00:00:00Z"

    # Negativo 1: una firma de operador no puede alegar ser una migración.
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(insert, (
            "llminbox", ACCEPT, 1, "closed", "operator",
            op.principal_id, op.runtime_instance, "SCHEMA_MIGRATION", at))
    # Negativo 2: una migración no puede alegar una decisión de operador.
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(insert, (
            "llminbox", REQUEUE, 1, "closed", "migration",
            None, None, "ROLLOUT", at))

    # Controles positivos de los dos lados del bicondicional.
    con.execute(insert, (
        "llminbox", ACCEPT, 1, "closed", "operator",
        op.principal_id, op.runtime_instance, "ROLLOUT", at))
    con.execute(insert, (
        "llminbox", REQUEUE, 1, "closed", "migration",
        None, None, "SCHEMA_MIGRATION", at))
    rows = con.execute(
        "SELECT origin,reason_code FROM admission_history ORDER BY verb"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("operator", "ROLLOUT"),
        ("migration", "SCHEMA_MIGRATION"),
    ]
    assert C.MANIFIESTOS[6]["checks"]["admission_history"] >= {
        "check((origin='migration'andreason_code='schema_migration')or("
        "origin='operator'andreason_code<>'schema_migration'))"}
    assert C._clasificar(con) == ("conocida", C.DURABLE_V, "")
    j.close()


# ══ 12 · FOTO Y TRANSICIÓN ATÓMICAS DEL PAR ═════════════════════════════════

def _epochs(states):
    return {state.verb: state.epoch for state in states}


def test_admissions_lee_los_dos_verbos_en_UNA_foto_y_deriva_el_carril(tmp_path):
    j, w, op = montaje(tmp_path)
    j.open_admission(op.token, ACCEPT, reason_code="ROLLOUT")
    trace = []
    con = j._connect()
    con.set_trace_callback(trace.append)
    try:
        states = j.admissions(op.token)
    finally:
        con.set_trace_callback(None)

    assert [(s.lane, s.verb, s.state, s.epoch) for s in states] == [
        ("llminbox", ACCEPT, "open", 1),
        ("llminbox", REQUEUE, "closed", 0),
    ]
    transactions = [sql.strip().upper() for sql in trace
                    if sql.strip().upper() in {"BEGIN", "BEGIN IMMEDIATE", "COMMIT"}]
    assert transactions == ["BEGIN", "COMMIT"]
    with pytest.raises(TypeError):
        j.admissions(op.token, lane="otro")
    j.close()


def test_admissions_no_da_la_foto_a_una_sesion_valida_sin_capacidad(tmp_path):
    j, w, _ = montaje(tmp_path)
    with pytest.raises(C.PolicyDenied):
        j.admissions(w.token)
    j.close()


def test_transition_admissions_abre_el_par_all_or_nothing(tmp_path):
    j, _, op = montaje(tmp_path)
    moved = j.transition_admissions(
        op.token, target="open",
        expected_epochs={ACCEPT: 0, REQUEUE: 0}, reason_code="ROLLOUT")

    assert [(s.lane, s.verb, s.state, s.epoch) for s in moved] == [
        ("llminbox", ACCEPT, "open", 1),
        ("llminbox", REQUEUE, "open", 1),
    ]
    assert [(row[1], row[2], row[3]) for row in filas(j)] == [
        (ACCEPT, 1, "open"), (REQUEUE, 1, "open"),
    ]
    j.close()


def test_un_CAS_stale_en_el_SEGUNDO_verbo_deja_CERO_inserts(tmp_path):
    """El segundo epoch es el stale: una implementación en bucle ya habría
    insertado el primero cuando descubre el conflicto que este test coloca."""
    j, _, op = montaje(tmp_path)
    with pytest.raises(C.AdmissionConflict):
        j.transition_admissions(
            op.token, target="open",
            expected_epochs={ACCEPT: 0, REQUEUE: 1}, reason_code="ROLLOUT")
    assert filas(j) == []
    j.close()


def test_una_transicion_competidora_en_la_ventana_hace_perder_TODO_el_batch(
        tmp_path):
    j, _, op = montaje(tmp_path)
    expected = {ACCEPT: 0, REQUEUE: 0}
    fired = []

    def compete_once():
        if fired:
            return
        fired.append(True)
        j.open_admission(op.token, REQUEUE, reason_code="ROLLOUT")

    j._gancho_carrera = compete_once
    try:
        with pytest.raises(C.AdmissionConflict):
            j.transition_admissions(
                op.token, target="open", expected_epochs=expected,
                reason_code="ROLLOUT")
    finally:
        j._gancho_carrera = None

    assert fired == [True]
    assert [(row[1], row[2], row[3]) for row in filas(j)] == [
        (REQUEUE, 1, "open"),
    ], "el batch escribió ACCEPT antes de descubrir el CAS perdido de REQUEUE"
    j.close()


class _DictHostil(dict):
    pass


class _IntHostil(int):
    pass


@pytest.mark.parametrize("changes", [
    {"target": True},
    {"target": _StrHostil("SECRET-TARGET", "open")},
    {"target": "OPEN"},
    {"reason_code": True},
    {"reason_code": _StrHostil("SECRET-REASON", "ROLLOUT")},
    {"expected_epochs": []},
    {"expected_epochs": _DictHostil({ACCEPT: 0, REQUEUE: 0})},
    {"expected_epochs": {ACCEPT: 0}},
    {"expected_epochs": {ACCEPT: 0, REQUEUE: 0, "events.other": 0}},
    {"expected_epochs": {_StrHostil("SECRET-KEY", ACCEPT): 0, REQUEUE: 0}},
    {"expected_epochs": {ACCEPT: True, REQUEUE: 0}},
    {"expected_epochs": {ACCEPT: -1, REQUEUE: 0}},
    {"expected_epochs": {ACCEPT: 0.0, REQUEUE: 0}},
    {"expected_epochs": {ACCEPT: _IntHostil(0), REQUEUE: 0}},
])
def test_transition_batch_rechaza_tipos_hostiles_antes_de_escribir(
        tmp_path, changes):
    j, _, op = montaje(tmp_path)
    kwargs = {
        "target": "open",
        "expected_epochs": {ACCEPT: 0, REQUEUE: 0},
        "reason_code": "ROLLOUT",
    }
    kwargs.update(changes)
    with pytest.raises(C.OperationInvalid):
        j.transition_admissions(op.token, **kwargs)
    assert filas(j) == []
    j.close()


def test_SCHEMA_MIGRATION_esta_reservado_al_origen_migration(tmp_path):
    j, _, op = montaje(tmp_path)
    assert "SCHEMA_MIGRATION" in C.ADMISSION_REASON_CODES
    assert "SCHEMA_MIGRATION" not in C.ADMISSION_OPERATOR_REASON_CODES

    for method_name in ("open_admission", "close_admission", "seal_admission"):
        kwargs = {"reason_code": "SCHEMA_MIGRATION"}
        if method_name == "seal_admission":
            kwargs["expected_epoch"] = 0
        with pytest.raises(C.OperationInvalid):
            getattr(j, method_name)(op.token, ACCEPT, **kwargs)
    with pytest.raises(C.OperationInvalid):
        j.transition_admissions(
            op.token, target="open",
            expected_epochs={ACCEPT: 0, REQUEUE: 0},
            reason_code="SCHEMA_MIGRATION")
    assert filas(j) == []
    j.close()


def test_transition_batch_exige_capacidad_dedicada(tmp_path):
    j, w, _ = montaje(tmp_path)
    with pytest.raises(C.PolicyDenied):
        j.transition_admissions(
            w.token, target="open",
            expected_epochs={ACCEPT: 0, REQUEUE: 0}, reason_code="ROLLOUT")
    assert filas(j) == []
    j.close()


def test_el_batch_congela_los_epochs_antes_de_la_ventana_TOCTOU(tmp_path):
    j, _, op = montaje(tmp_path)
    expected = {ACCEPT: 0, REQUEUE: 0}

    def muta_el_argumento():
        expected[REQUEUE] = 999

    j._gancho_carrera = muta_el_argumento
    try:
        moved = j.transition_admissions(
            op.token, target="open", expected_epochs=expected,
            reason_code="ROLLOUT")
    finally:
        j._gancho_carrera = None
    assert _epochs(moved) == {ACCEPT: 1, REQUEUE: 1}
    j.close()


def test_el_batch_toma_UN_snapshot_antes_de_leer_claves_y_valores(tmp_path):
    """Una mutación concurrente entre claves y valores jamás fuga ``KeyError``.

    El trace sincroniza el ``clear`` justo cuando ya existe la foto de claves:
    no depende de sleeps. El mutante que itera el argumento y luego vuelve a
    indexarlo conserva ``keys`` pero pierde los valores y cae con ``KeyError``.
    """
    j, _, op = montaje(tmp_path)
    expected = {ACCEPT: 0, REQUEUE: 0}
    muta = threading.Event()
    mutado = threading.Event()
    original = C.Journal.transition_admissions.__wrapped__
    disparado = []

    def clear_when_keys_exist():
        assert muta.wait(2), "el trace no alcanzó la foto de claves"
        expected.clear()
        mutado.set()

    worker = threading.Thread(target=clear_when_keys_exist)
    worker.start()

    def trace(frame, event, arg):
        if (not disparado and event == "line"
                and frame.f_code is original.__code__
                and "keys" in frame.f_locals):
            disparado.append(True)
            muta.set()
            assert mutado.wait(2), "el mutador concurrente no terminó"
        return trace

    sys.settrace(trace)
    try:
        moved = j.transition_admissions(
            op.token, target="open", expected_epochs=expected,
            reason_code="ROLLOUT")
    finally:
        sys.settrace(None)
        muta.set()
        worker.join(timeout=2)

    assert disparado == [True]
    assert not worker.is_alive()
    assert expected == {}
    assert _epochs(moved) == {ACCEPT: 1, REQUEUE: 1}
    j.close()


def test_seal_batch_usa_UN_BEGIN_IMMEDIATE_UN_COUNT_y_dos_inserts(tmp_path):
    j, _, op = montaje(tmp_path)
    trace = []
    con = j._connect()
    con.set_trace_callback(trace.append)
    try:
        sealed = j.transition_admissions(
            op.token, target="sealed",
            expected_epochs={ACCEPT: 0, REQUEUE: 0},
            reason_code="DRAIN_FOR_ROLLBACK")
    finally:
        con.set_trace_callback(None)

    upper = [sql.strip().upper() for sql in trace]
    assert upper.count("BEGIN IMMEDIATE") == 1
    assert upper.count("COMMIT") == 1
    assert sum("SELECT COUNT(*) C FROM OUTBOX O JOIN EVENTS E" in sql
               for sql in upper) == 1
    assert sum(sql.startswith("INSERT INTO ADMISSION_HISTORY")
               for sql in upper) == 2
    begin = upper.index("BEGIN IMMEDIATE")
    auth = next(i for i, sql in enumerate(upper)
                if "FROM RUNTIME_SESSIONS S" in sql)
    capability = next(i for i, sql in enumerate(upper)
                      if sql.startswith("SELECT CAPABILITIES FROM CREDENTIAL_BINDINGS"))
    first_insert = next(i for i, sql in enumerate(upper)
                        if sql.startswith("INSERT INTO ADMISSION_HISTORY"))
    commit = upper.index("COMMIT")
    assert begin < auth < capability < first_insert < commit
    assert [(s.verb, s.state, s.epoch) for s in sealed] == [
        (ACCEPT, "sealed", 1), (REQUEUE, "sealed", 1),
    ]
    j.close()


def test_seal_batch_falla_con_pending_Y_failed_sin_sellado_parcial(tmp_path):
    j, w, op = montaje(tmp_path, abierta=True, max_attempts=1)
    j.accept_event(w.token, idempotency_key="pending", intent=INTENT,
                   ledger="llminbox")
    failed = j.accept_event(
        w.token, idempotency_key="failed",
        intent={**INTENT, "head": "otro"}, ledger="llminbox")
    # La cola sirve por orden: agota primero `pending`; los nombres de las claves
    # describen el estado final, no intentan escoger el trabajo del claim.
    job = j.claim_outbox(w.token)
    j.mark_outbox_failed(w.token, job.event_id, error="PROJECTOR_IO_ERROR",
                         claim_token=job.claim_token)
    states = dict(j._connect().execute(
        "SELECT event_id, state FROM outbox"))
    assert sorted(states.values()) == ["failed", "pending"]
    assert failed.event_id in states

    closed = j.transition_admissions(
        op.token, target="closed",
        expected_epochs={ACCEPT: 1, REQUEUE: 1},
        reason_code="DRAIN_FOR_ROLLBACK")
    before = filas(j)
    with pytest.raises(C.AdmissionConflict):
        j.transition_admissions(
            op.token, target="sealed", expected_epochs=_epochs(closed),
            reason_code="DRAIN_FOR_ROLLBACK")
    assert filas(j) == before
    assert {s.verb: (s.state, s.epoch) for s in j.admissions(op.token)} == {
        ACCEPT: ("closed", 2), REQUEUE: ("closed", 2),
    }
    j.close()


def test_sealed_batch_es_terminal_para_el_par_completo(tmp_path):
    j, _, op = montaje(tmp_path)
    sealed = j.transition_admissions(
        op.token, target="sealed",
        expected_epochs={ACCEPT: 0, REQUEUE: 0},
        reason_code="DRAIN_FOR_ROLLBACK")
    before = filas(j)
    with pytest.raises(C.AdmissionConflict):
        j.transition_admissions(
            op.token, target="open", expected_epochs=_epochs(sealed),
            reason_code="ROLLOUT")
    assert filas(j) == before
    j.close()
