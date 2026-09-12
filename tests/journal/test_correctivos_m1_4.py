"""M1-4: los ocho hallazgos convertidos en falsificadores.

Cada uno mide el EFECTO, no la lectura del código: si la base de versión futura
se toca, se ve en el fichero; si la capacidad falta, la operación no ocurre; si
el hash no incluye la valla, dos peticiones distintas comparten recibo.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

import coordination as C
from ._arnes import GRAMATICA, INTENT, LANES, OPERADOR, PEPPER, Reloj, censo, journal, sesion, sesiones


# ── ① ESQUEMA FUTURO: CERO ESCRITURA, NI SIQUIERA EL FORMATO ────────────────

def _sellar(ruta, version):
    con = sqlite3.connect(ruta)
    con.execute("UPDATE meta SET v=? WHERE k='durable_v'", (str(version),))
    con.commit()
    con.execute("PRAGMA journal_mode=DELETE")     # deja el fichero SIN WAL
    con.close()


def test_una_base_futura_no_se_toca_NI_EL_JOURNAL_MODE(tmp_path):
    """El orden ERA el defecto: `journal_mode=WAL` se aplicaba al conectar, o sea
    ANTES de saber si entendemos el esquema — y eso cambia el FORMATO del
    fichero. «No la mutamos» era falso incluso cuando el rechazo funcionaba."""
    ruta = str(tmp_path / "coordination.sqlite")
    j = journal(tmp_path)
    j.close()
    _sellar(ruta, C.DURABLE_V + 1)

    antes = os.stat(ruta).st_mtime_ns
    modo_antes = _modo(ruta)
    assert modo_antes == "delete"                  # ⊕ el escenario está montado

    futura = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
    assert futura.stored_durable_v() == C.DURABLE_V + 1
    with pytest.raises(C.SchemaTooNew):
        futura.initialize()
    futura.close()

    assert _modo(ruta) == "delete", "le cambió el formato a una base que no entiende"
    assert os.stat(ruta).st_mtime_ns == antes
    assert not os.path.exists(ruta + "-wal") and not os.path.exists(ruta + "-shm")


def test_un_token_desconocido_no_escribe_NADA_en_esquema_futuro(tmp_path):
    """Era la única escritura que quedaba abierta sobre una base incomprendida,
    y la disparaba cualquiera SIN credencial."""
    ruta = str(tmp_path / "coordination.sqlite")
    j = journal(tmp_path)
    j.close()
    _sellar(ruta, C.DURABLE_V + 1)
    antes = os.stat(ruta).st_mtime_ns

    futura = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
    for i in range(20):
        futura.record_unknown_credential(f"cred-{i}")
    # Se cuenta con conexión CRUDA: el journal rechaza esta base y por tanto NO
    # tiene conexión operacional — pedírsela sería pedirle justo lo que acaba de
    # negarse a dar, y el `0` saldría de una excepción en vez de de la tabla.
    crudo = sqlite3.connect(ruta)
    try:
        assert crudo.execute(
            "SELECT COUNT(*) c FROM unknown_credentials").fetchone()[0] == 0
    finally:
        crudo.close()
    assert os.stat(ruta).st_mtime_ns == antes
    futura.close()


def _modo(ruta):
    con = sqlite3.connect(ruta)
    try:
        return con.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        con.close()


# ── ② CAPACIDAD DE OPERADOR, SERVER-DERIVED ────────────────────────────────

def _agotado(j, s, reloj):
    a = j.accept_event(s.token, idempotency_key="k", intent=INTENT, ledger="llminbox")
    for _ in range(j._max_attempts):
        reloj.avanza(1000)
        item = j.claim_outbox(s.token, lease_s=60)
        j.mark_outbox_failed(s.token, a.event_id, error="x",
                             claim_token=item.claim_token)
    return a


@pytest.mark.parametrize("verbo", ["requeue", "abandon"])
def test_estar_en_el_carril_no_es_ser_su_operador(tmp_path, verbo):
    reloj = Reloj()
    j = journal(tmp_path, reloj=reloj, max_attempts=1)
    s = sesion(j, ttl_s=10 ** 6)                     # SIN capacidad
    a = _agotado(j, s, reloj)
    antes = j._connect().execute("SELECT state FROM outbox").fetchone()["state"]

    with pytest.raises(C.PolicyDenied):
        getattr(j, f"{verbo}_outbox")(s.token, a.event_id, reason="porque puedo")
    con = j._connect()
    assert con.execute("SELECT state FROM outbox").fetchone()["state"] == antes
    assert con.execute("SELECT COUNT(*) c FROM outbox_operations").fetchone()["c"] == 0
    assert con.execute("SELECT reason FROM denials ORDER BY at DESC LIMIT 1"
                       ).fetchone()["reason"] == "POLICY_DENIED"
    j.close()


def test_con_la_capacidad_concedida_SI_opera(tmp_path):
    """⊕ del anterior: sin esto, «deniega siempre» pasaría igual."""
    reloj = Reloj()
    j = journal(tmp_path, reloj=reloj, max_attempts=1)
    s = sesion(j, ttl_s=10 ** 6, capabilities=OPERADOR)
    a = _agotado(j, s, reloj)
    j.abandon_outbox(s.token, a.event_id, reason="no aplica")
    assert j._connect().execute("SELECT state FROM outbox").fetchone()["state"] \
        == "abandoned"
    j.close()


def test_retirar_la_capacidad_corta_al_operador_EN_LA_SIGUIENTE_operacion(tmp_path):
    """Revalidada dentro de la transacción: leerla al abrir sesión dejaría operar
    a quien acaba de perderla."""
    reloj = Reloj()
    j = journal(tmp_path, reloj=reloj, max_attempts=1)
    s = sesion(j, ttl_s=10 ** 6, capabilities=OPERADOR)
    a = _agotado(j, s, reloj)
    j.requeue_outbox(s.token, a.event_id, reason="una vez sí")      # ⊕
    # El operador le quita la capacidad a esa credencial…
    j.bind_credential("cred-A", principal=None, role="be", lane="llminbox",
                      capabilities=())
    s2 = j.open_session("cred-A", ttl_s=10 ** 6)   # TTL largo: el reloj se mueve
    reloj.avanza(1000)
    with pytest.raises(C.PolicyDenied):
        j.claim_outbox(s2.token, lease_s=60)
    fila = j._connect().execute(
        "SELECT state, claim_token FROM outbox WHERE event_id=?", (a.event_id,)
    ).fetchone()
    assert tuple(fila) == ("pending", None), (
        "la operación denegada reclamó o alteró el trabajo")
    j.close()


# ── ③ MOVER LA IDENTIDAD MATA LAS SESIONES ─────────────────────────────────

def test_rebindear_una_credencial_revoca_lo_que_habia_emitido(tmp_path):
    j = journal(tmp_path)
    vieja = sesion(j, "cred-A", principal="uno")
    assert j.authenticate(vieja.token) is not None        # ⊕ valía
    j.bind_credential("cred-A", principal="dos", role="be", lane="llminbox")
    assert j.authenticate(vieja.token) is None, \
        "token vivo hablando por un principal que el operador retiró"
    fila = j._connect().execute(
        "SELECT revoke_reason FROM runtime_sessions WHERE runtime_instance=?",
        (vieja.runtime_instance,)).fetchone()
    assert fila["revoke_reason"] == "binding_retired"
    j.close()


# ── ④ LA MIGRACIÓN NO DEGRADA UNA CONTRADICCIÓN ────────────────────────────

def test_un_runtime_que_no_cuadra_PARA_la_migracion(tmp_path):
    """`legacy_unattributed` significa «no había runtime», no «había uno que no
    cuadra». Degradarlo haría desaparecer la contradicción en una etiqueta que
    nadie vuelve a mirar."""
    from .test_migracion_v1_a_v2 import _fixture_v1
    ruta, b, s1, s2, ev, _ = _fixture_v1(tmp_path, "B")
    con = sqlite3.connect(ruta)
    con.execute("PRAGMA foreign_keys=OFF")
    # El runtime existe y es válido, pero es de OTRO principal.
    con.execute("INSERT INTO principals(principal_id,principal,role,lane,created_at)"
                " VALUES('prn_otro','otro','be','llminbox','t')")
    con.execute("UPDATE commands SET principal_id='prn_otro' WHERE command_id='cmd_1'")
    con.commit()
    con.close()

    j = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
    with pytest.raises(C.MigrationFailed):
        j.initialize()
    assert j.stored_durable_v() == 1
    # Igual que arriba: tras `MigrationFailed` el journal no queda listo, así que
    # el esquema se inspecciona en el fichero. No hay condición de FK que medir
    # aquí —sólo la ausencia de una columna—, así que no hace falta encenderlas.
    crudo = sqlite3.connect(ruta)
    try:
        assert "attribution_status" not in {
            c[1] for c in crudo.execute("PRAGMA table_info(commands)")}
    finally:
        crudo.close()
    j.close()


# ── ⑥ LA VALLA ENTRA EN EL HASH, Y EL ORDEN IMPORTA ────────────────────────

def test_dos_vallas_distintas_no_comparten_recibo(tmp_path):
    j = journal(tmp_path)
    s = sesion(j)
    j.acquire_lease(s.token, "r1", ttl_s=600)
    j.acquire_lease(s.token, "r2", ttl_s=600)
    j.accept_event(s.token, idempotency_key="k", intent=INTENT, ledger="llminbox",
                   fenced_resource="r1", fencing_token=1)
    with pytest.raises(C.IdempotencyConflict):
        j.accept_event(s.token, idempotency_key="k", intent=INTENT,
                       ledger="llminbox", fenced_resource="r2", fencing_token=1)
    j.close()


def test_un_reintento_IDENTICO_vale_aunque_la_valla_haya_vencido(tmp_path):
    """El cliente reintenta porque no recibió la respuesta, no porque quiera
    mutar otra vez. Validando la valla ANTES del replay, se le castigaba por la
    latencia de la red."""
    reloj = Reloj()
    j = journal(tmp_path, reloj=reloj)
    s = sesion(j, ttl_s=10 ** 6)
    lease = j.acquire_lease(s.token, "r", ttl_s=60)
    a = j.accept_event(s.token, idempotency_key="k", intent=INTENT,
                       ledger="llminbox", fenced_resource="r",
                       fencing_token=lease.fencing_token)
    reloj.avanza(61)                                   # la valla vence
    with pytest.raises(C.FencingConflict):             # ⊕ una clave NUEVA no pasa
        j.accept_event(s.token, idempotency_key="otra", intent=INTENT,
                       ledger="llminbox", fenced_resource="r",
                       fencing_token=lease.fencing_token)
    b = j.accept_event(s.token, idempotency_key="k", intent=INTENT,
                       ledger="llminbox", fenced_resource="r",
                       fencing_token=lease.fencing_token)
    assert b.replayed is True and b.event_id == a.event_id
    j.close()


def test_el_destino_derivado_y_el_explicito_dan_el_MISMO_hash(tmp_path):
    """El hash se calcula sobre el destino EFECTIVO. Con el `ledger` crudo, el
    mismo reintento escrito de dos formas equivalentes daba dos hashes."""
    j = journal(tmp_path, lane_ledgers={"llminbox": ["llminbox"]})
    s = sesion(j)
    a = j.accept_event(s.token, idempotency_key="k", intent=INTENT)   # derivado
    b = j.accept_event(s.token, idempotency_key="k", intent=INTENT,
                       ledger="llminbox")                             # explícito
    assert b.replayed is True and b.event_id == a.event_id
    j.close()


def test_una_fila_de_hash_v1_se_juzga_con_SU_formula(tmp_path):
    """Comparar con la fórmula nueva daría `409` a un reintento legítimo; no
    comparar daría el recibo de otro a CUALQUIER cuerpo. Se compara con la v1."""
    j = journal(tmp_path)
    s = sesion(j)
    a = j.accept_event(s.token, idempotency_key="k", intent=INTENT, ledger="llminbox")
    j._connect().execute("UPDATE idempotency SET req_hash_v=1, req_hash=?",
                         (C._req_hash(1, intent=INTENT, ledger_raw="llminbox",
                                      ledger_efectivo="llminbox", causes=(),
                                      external_causes=(), fenced_resource=None,
                                      fencing_token=None),))
    b = j.accept_event(s.token, idempotency_key="k", intent=INTENT, ledger="llminbox")
    assert b.replayed is True and b.event_id == a.event_id      # mismo cuerpo
    with pytest.raises(C.IdempotencyConflict):                  # otro cuerpo: 409
        j.accept_event(s.token, idempotency_key="k",
                       intent={**INTENT, "body": "otro"}, ledger="llminbox")
    j.close()


# ── ⑤ LECTURAS DE RECIBO AUTENTICADAS ──────────────────────────────────────

def test_leer_un_recibo_exige_sesion_y_no_cruza_carriles(tmp_path):
    j = journal(tmp_path)
    a, b = sesiones(j,
        {"credential": "cred-A", "principal": "p-a", "lane": "carril-uno"},
        {"credential": "cred-B", "principal": "p-b", "lane": "carril-dos"})
    ev = j.accept_event(a.token, idempotency_key="k", intent=INTENT,
                        ledger="ledger-uno")
    rid = j.receipt_for_event(a.token, ev.event_id)["receipt_id"]      # ⊕ el suyo
    assert j.transitions(a.token, rid)
    # Desde M1-5, el carril ajeno es INDISTINGUIBLE de un id inexistente: con
    # errores distintos, quien prueba identificadores aprende cuáles existen en
    # carriles que no son suyos.
    with pytest.raises(C.SubjectNotFound):
        j.receipt_for_event(b.token, ev.event_id)
    with pytest.raises(C.SubjectNotFound):
        j.transitions(b.token, rid)
    with pytest.raises(C.SubjectNotFound):
        j.receipt(b.token, "rcp_inventado")      # mismo error, misma forma
    with pytest.raises(C.AuthError):
        j.receipt("token-inventado", rid)
    j.close()


# ── ⑦ CAUSAS EXTERNAS MALFORMADAS: TIPADAS Y AUDITADAS ─────────────────────

@pytest.mark.parametrize("mala", [{}, {"ledger": "l"}, {"entry_eid": "e"},
                                  {"ledger": "", "entry_eid": "e"}, "no-es-un-dict"])
def test_una_causa_externa_malformada_es_rechazo_TIPADO_no_un_500(tmp_path, mala):
    j = journal(tmp_path)
    s = sesion(j)
    with pytest.raises(C.CauseRejected):
        j.accept_event(s.token, idempotency_key="k", intent=INTENT,
                       ledger="llminbox", external_causes=[mala])
    con = j._connect()
    assert con.execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == 0
    assert con.execute("SELECT reason FROM denials ORDER BY at DESC LIMIT 1"
                       ).fetchone()["reason"] == "CAUSE_REJECTED"
    j.close()


# ── ⑧ DATOS SIN SELLO: INDETERMINADO ───────────────────────────────────────

def test_una_base_con_datos_y_sin_durable_v_no_se_sella(tmp_path):
    ruta = str(tmp_path / "coordination.sqlite")
    j = journal(tmp_path)
    s = sesion(j)
    j.accept_event(s.token, idempotency_key="k", intent=INTENT, ledger="llminbox")
    j.close()
    con = sqlite3.connect(ruta)
    con.execute("DELETE FROM meta WHERE k='durable_v'")
    con.commit(); con.close()

    otro = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
    with pytest.raises(C.SchemaIndeterminate):
        otro.initialize()
    assert otro.stored_durable_v() is None, "selló una forma que no sabe cuál es"
    otro.close()


def test_una_base_VACIA_y_sin_sello_si_se_inicializa(tmp_path):
    """⊕: el rechazo tiene que distinguir «no sé qué hay» de «no hay nada»."""
    j = C.Journal(str(tmp_path / "v.sqlite"), pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
    assert j.initialize() == C.DURABLE_V
    j.close()


# ── PREPARACIÓN DE API ─────────────────────────────────────────────────────

def test_el_error_transporta_el_recibo_de_su_rechazo(tmp_path):
    j = journal(tmp_path)
    s = sesion(j)
    j.accept_event(s.token, idempotency_key="k", intent=INTENT, ledger="llminbox")
    try:
        j.accept_event(s.token, idempotency_key="k",
                       intent={**INTENT, "body": "otro"}, ledger="llminbox")
        pytest.fail("debería haber conflicto")
    except C.IdempotencyConflict as e:
        assert e.receipt_id, "el gateway no puede citar el recibo del rechazo"
        rc = j.receipt(s.token, e.receipt_id)
        assert rc["current_state"] == "denied"
        assert rc["principal_id"] == s.principal_id
    j.close()


def test_record_rejection_es_la_puerta_del_gateway_y_esta_ACOTADA(tmp_path):
    j = journal(tmp_path, denial_quota=2, reloj=Reloj())
    s = sesion(j)
    rid = j.record_rejection(s.token, "POLICY_DENIED")
    fila = j._connect().execute("SELECT * FROM denials ORDER BY at DESC LIMIT 1"
                                ).fetchone()
    assert fila["reason"] == "POLICY_DENIED"
    assert (fila["principal_id"], fila["lane"], fila["runtime_instance"]) == \
        (s.principal_id, s.lane, s.runtime_instance)
    assert j.receipt(s.token, rid)["current_state"] == "denied"
    # Vocabulario CERRADO también para el gateway.
    with pytest.raises(ValueError):
        j.record_rejection(s.token, "porque el gateway lo dice")
    # Y la MISMA cuota: no es una puerta trasera para llenar la base.
    for _ in range(30):
        j.record_rejection(s.token, "POLICY_DENIED")
    con = j._connect()
    assert con.execute("SELECT COUNT(*) c FROM denials").fetchone()["c"] == 2
    assert con.execute("SELECT suppressed FROM denial_aggregates").fetchone()[0] == 29
    j.close()
