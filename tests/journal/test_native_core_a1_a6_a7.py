"""Gaps de NÚCLEO que bloquean garantías: A1 (destinatarios/ACK), A6 (renew
estricto) y A7 (payload de comando + conflicto de revisión).

Los tres los midió `@contratosbik` (`MARK:contratos-m1-letra-de-la-exposicion`).
Aquí se fijan como falsadores del núcleo: **ninguno se cura desde el gateway**,
porque un router que normalizara destinatarios por su cuenta sería la segunda
fuente de verdad que este repo ya se ha comido dos veces.

Cada bloque trae su ⊖: el mutante que DEBE hacer fallar la prueba. Un falsador
sin ⊖ no distingue «pasa porque el contrato se cumple» de «pasa porque no mide».
"""
from __future__ import annotations

import pytest

import coordination as C
from ._arnes import INTENT, LANES, PEPPER, Reloj, journal, sesion, sesiones


# ── Censo de PRUEBAS ────────────────────────────────────────────────────────
# El journal NO conoce el censo: lo PREGUNTA. Esto es lo que un despliegue
# inyectaría desde `ledger_parse`, y por eso el módulo no importa `servicio`.
# Devuelve `(rol_canónico | None, es_difusión)`.
_CENSO = {
    "backend": ("be", False), "be": ("be", False),
    "security": ("security", False),
    "cto-llminbox": ("cto", False), "cto": ("cto", False),
    "FLOTA": (None, True), "flota": (None, True), "TODOS": (None, True),
}


def censo(literal: str):
    return _CENSO.get(literal, (None, False))      # desconocido: NI rol NI difusión


def jr(tmp_path, **kw):
    """Journal con censo inyectado. El que NO lo inyecta se prueba aparte."""
    kw.setdefault("recipient_resolver", censo)
    return journal(tmp_path, **kw)


def _hasta_indexed(j, autor, destinatarios, *, key="k"):
    ev = j.accept_event(autor.token, idempotency_key=key,
                        intent={**INTENT, "to": destinatarios}, ledger="llminbox")
    item = j.claim_outbox(autor.token, lease_s=60)
    j.mark_materialized(autor.token, ev.event_id, entry_eid="e" * 64,
                        ledger="llminbox", claim_token=item.claim_token)
    j.mark_indexed(autor.token, ev.event_id, index_ref="idx-1")
    return ev


# ══ A1 · destinatarios: literal para el render, rol canónico para el ACK ════

def test_a1_acusa_por_ROL_canonico_aunque_el_literal_sea_otro_nombre(tmp_path):
    """El caso que hoy rompe 204 de 695 tokens de destinatario: `→ backend`
    con una sesión de rol `be`. El destinatario legítimo recibía 409."""
    j = jr(tmp_path)
    autor, be = sesiones(j,
        {"credential": "c-cto", "principal": "p-cto", "role": "cto"},
        {"credential": "c-be", "principal": "p-be", "role": "be"})
    ev = _hasta_indexed(j, autor, ["backend"])      # literal ≠ rol
    assert j.mark_delivered(be.token, ev.event_id, ack_ref="a") == "delivered"
    j.close()


def test_a1_OMEGA_un_rol_ajeno_sigue_sin_poder_acusar(tmp_path):
    """⊖ que protege la cura: normalizar NO puede abrir el acuse a cualquiera."""
    j = jr(tmp_path)
    autor, sec = sesiones(j,
        {"credential": "c-cto", "principal": "p-cto", "role": "cto"},
        {"credential": "c-sec", "principal": "p-sec", "role": "security"})
    ev = _hasta_indexed(j, autor, ["backend"])
    with pytest.raises(C.DeliveryConflict):
        j.mark_delivered(sec.token, ev.event_id)
    j.close()


def test_a1_el_literal_se_conserva_para_el_render(tmp_path):
    """La proyección a markdown escribe lo que el autor puso, no el rol."""
    j = jr(tmp_path)
    autor = sesion(j, "c-cto", principal="p-cto", role="cto")
    ev = j.accept_event(autor.token, idempotency_key="k",
                        intent={**INTENT, "to": ["backend", "FLOTA"]},
                        ledger="llminbox")
    job = j.claim_outbox(autor.token, lease_s=60)
    assert tuple(job.recipients) == ("backend", "FLOTA")
    j.close()


def test_a1_difusion_sola_alcanza_estado_terminal_explicito(tmp_path):
    """`→ FLOTA`: NADIE puede acusar por difusión, así que esperar un acuse es
    esperar para siempre. Tiene que llegar a un terminal que lo DIGA."""
    j = jr(tmp_path)
    autor = sesion(j, "c-cto", principal="p-cto", role="cto")
    ev = _hasta_indexed(j, autor, ["FLOTA"])
    rec = j.receipt_for_event(autor.token, ev.event_id)
    assert rec["current_state"] == "no_ack_expected"
    j.close()


def test_a1_OMEGA_difusion_no_se_queda_en_delivery_progress(tmp_path):
    """⊖ del anterior: el estado terminal no puede ser un alias de «va llegando»."""
    j = jr(tmp_path)
    autor = sesion(j, "c-cto", principal="p-cto", role="cto")
    ev = _hasta_indexed(j, autor, ["TODOS"])
    estados = [t["state"] for t in
               j.transitions(autor.token,
                             j.receipt_for_event(autor.token, ev.event_id)["receipt_id"])]
    assert "delivery_progress" not in estados
    assert estados[-1] == "no_ack_expected"
    j.close()


def test_a1_difusion_NO_cuenta_en_el_denominador_del_acuse(tmp_path):
    """Mixto `["FLOTA","security"]`: acusables = 1. Con la difusión dentro, el
    recibo se quedaba en `delivery_progress` para siempre (1 de 2)."""
    j = jr(tmp_path)
    autor, sec = sesiones(j,
        {"credential": "c-cto", "principal": "p-cto", "role": "cto"},
        {"credential": "c-sec", "principal": "p-sec", "role": "security"})
    ev = _hasta_indexed(j, autor, ["FLOTA", "security"])
    assert j.mark_delivered(sec.token, ev.event_id, ack_ref="a") == "delivered"
    j.close()


def test_a1_acuse_sobre_evento_de_pura_difusion_se_rechaza(tmp_path):
    """Nadie ES la difusión: un acuse ahí sería una firma sin sujeto."""
    j = jr(tmp_path)
    autor, be = sesiones(j,
        {"credential": "c-cto", "principal": "p-cto", "role": "cto"},
        {"credential": "c-be", "principal": "p-be", "role": "be"})
    ev = _hasta_indexed(j, autor, ["FLOTA"])
    with pytest.raises(C.DeliveryConflict):
        j.mark_delivered(be.token, ev.event_id)
    j.close()


def test_a1_SIN_censo_inyectado_falla_CERRADO(tmp_path):
    """Sin censo el journal no puede prometer A1. Se RECHAZA, no se guarda crudo:
    guardar crudo es exactamente el estado que produce el acuse imposible."""
    j = journal(tmp_path, recipient_resolver=None)      # censo AUSENTE
    s = sesion(j)
    with pytest.raises(C.RecipientUnresolved):
        j.accept_event(s.token, idempotency_key="k", intent=INTENT,
                       ledger="llminbox")
    j.close()


def test_a1_destinatario_que_no_resuelve_se_rechaza_al_ACEPTAR(tmp_path):
    """Fail-closed en la puerta: un nombre fuera del censo no produce entrada
    huérfana. Es la misma regla que `/append` ya aplica (`_indexable`)."""
    j = jr(tmp_path)
    s = sesion(j, "c-cto", principal="p-cto", role="cto")
    with pytest.raises(C.RecipientUnresolved):
        j.accept_event(s.token, idempotency_key="k",
                       intent={**INTENT, "to": ["no-existe-en-el-censo"]},
                       ledger="llminbox")
    j.close()


def test_a1_OMEGA_el_rechazo_no_escribe_nada(tmp_path):
    """⊖: un rechazo que ya escribió el evento no es un rechazo."""
    j = jr(tmp_path)
    s = sesion(j, "c-cto", principal="p-cto", role="cto")
    with pytest.raises(C.RecipientUnresolved):
        j.accept_event(s.token, idempotency_key="k",
                       intent={**INTENT, "to": ["no-existe-en-el-censo"]},
                       ledger="llminbox")
    assert j.pending_outbox(s.token) == 0
    # y la clave de idempotencia sigue libre: el rechazo no la quema
    ev = j.accept_event(s.token, idempotency_key="k",
                        intent={**INTENT, "to": ["security"]}, ledger="llminbox")
    assert ev.replayed is False
    j.close()


def test_a1_duplicados_por_alias_cuentan_UNA_vez(tmp_path):
    """`["backend","be"]` es UN destinatario, no dos: con dos, el denominador
    sube y `delivered` vuelve a ser inalcanzable por la puerta de al lado."""
    j = jr(tmp_path)
    autor, be = sesiones(j,
        {"credential": "c-cto", "principal": "p-cto", "role": "cto"},
        {"credential": "c-be", "principal": "p-be", "role": "be"})
    ev = _hasta_indexed(j, autor, ["backend", "be"])
    assert j.mark_delivered(be.token, ev.event_id, ack_ref="a") == "delivered"
    j.close()


# ══ A6 · renew ESTRICTO: no es un alias de acquire ══════════════════════════

def _lease_vivo(j, s, recurso="deploy", ttl=300):
    return j.acquire_lease(s.token, recurso, ttl_s=ttl)


def test_a6_renew_de_lease_VENCIDO_da_conflicto_y_NO_toca_el_fencing(tmp_path):
    """Hoy `renew` llama a `acquire`: tras vencer devolvía el token N+1 y el
    llamante creía haber conservado el suyo. La palabra promete continuidad."""
    r = Reloj()
    j = jr(tmp_path, reloj=r)
    s = sesion(j, "c-be", principal="p-be", role="be", ttl_s=99999)
    l0 = _lease_vivo(j, s, ttl=100)
    r.avanza(101)                                   # vencido, nadie en medio
    with pytest.raises(C.LeaseConflict):
        j.renew_lease(s.token, "deploy", ttl_s=100)
    l1 = j.acquire_lease(s.token, "deploy", ttl_s=100)
    assert l1.fencing_token == l0.fencing_token + 1, \
        "el renew fallido no puede haber consumido un token de fencing"
    j.close()


def test_a6_renew_AJENO_da_conflicto_y_NO_toca_el_fencing(tmp_path):
    r = Reloj()
    j = jr(tmp_path, reloj=r)
    a, b = sesiones(j,
        {"credential": "c-a", "principal": "p-a", "role": "be", "ttl_s": 99999},
        {"credential": "c-b", "principal": "p-b", "role": "cto", "ttl_s": 99999})
    l0 = j.acquire_lease(a.token, "deploy", ttl_s=300)
    with pytest.raises(C.LeaseConflict):
        j.renew_lease(b.token, "deploy", ttl_s=300)
    assert j.renew_lease(a.token, "deploy", ttl_s=300).fencing_token == l0.fencing_token
    j.close()


def test_a6_renew_de_lease_LIBERADO_da_conflicto(tmp_path):
    r = Reloj()
    j = jr(tmp_path, reloj=r)
    s = sesion(j, "c-be", principal="p-be", role="be", ttl_s=99999)
    l0 = _lease_vivo(j, s)
    j.release_lease(s.token, "deploy")
    with pytest.raises(C.LeaseConflict):
        j.renew_lease(s.token, "deploy", ttl_s=300)
    assert j.acquire_lease(s.token, "deploy").fencing_token == l0.fencing_token + 1
    j.close()


def test_a6_renew_de_recurso_INEXISTENTE_da_conflicto(tmp_path):
    j = jr(tmp_path)
    s = sesion(j, "c-be", principal="p-be", role="be")
    with pytest.raises(C.LeaseConflict):
        j.renew_lease(s.token, "nunca-adquirido", ttl_s=300)
    j.close()


def test_a6_OMEGA_renew_VIVO_conserva_el_token_y_extiende(tmp_path):
    """⊕ del camino bueno: si esto también fallara, «estricto» sería «roto»."""
    r = Reloj()
    j = jr(tmp_path, reloj=r)
    s = sesion(j, "c-be", principal="p-be", role="be", ttl_s=99999)
    l0 = _lease_vivo(j, s, ttl=100)
    r.avanza(50)
    l1 = j.renew_lease(s.token, "deploy", ttl_s=100)
    assert l1.fencing_token == l0.fencing_token
    assert l1.expires_at > l0.expires_at
    j.close()


def test_a6_OMEGA_acquire_SIGUE_relevando_tras_vencer(tmp_path):
    """⊖ de alcance: la cura es de `renew`. Si `acquire` dejara de relevar, un
    dueño caído bloquearía el recurso para siempre."""
    r = Reloj()
    j = jr(tmp_path, reloj=r)
    a, b = sesiones(j,
        {"credential": "c-a", "principal": "p-a", "role": "be", "ttl_s": 99999},
        {"credential": "c-b", "principal": "p-b", "role": "cto", "ttl_s": 99999})
    l0 = j.acquire_lease(a.token, "deploy", ttl_s=100)
    r.avanza(101)
    l1 = j.acquire_lease(b.token, "deploy", ttl_s=100)
    assert l1.fencing_token == l0.fencing_token + 1
    assert l1.principal_id != l0.principal_id
    j.close()


# ══ A7 · comandos: payload sin atribución, y el conflicto que se puede seguir ═

@pytest.mark.parametrize("campo", ["actor", "principal", "principal_id", "role",
                                   "lane", "runtime_instance", "attestation",
                                   "command_id", "workstream_id", "revision",
                                   "state", "supersedes"])
def test_a7_payload_con_campo_RESERVADO_se_rechaza(tmp_path, campo):
    """`accept_event` ya los rechaza; `submit_command` los guardaba y los
    ignoraba en silencio — y el cliente los veía persistidos."""
    j = jr(tmp_path)
    s = sesion(j, "c-be", principal="p-be", role="be")
    with pytest.raises(C.AttributionRejected):
        j.submit_command(s.token, workstream_id="w", revision=1,
                         payload={"orden": "x", campo: "lo-que-sea"})
    j.close()


def test_a7_OMEGA_un_payload_limpio_SIGUE_pasando(tmp_path):
    """⊖ de alcance: una lista de reservados demasiado ancha mata el caso legítimo."""
    j = jr(tmp_path)
    s = sesion(j, "c-be", principal="p-be", role="be")
    cid, estado = j.submit_command(s.token, workstream_id="w", revision=1,
                                   payload={"orden": "desplegar", "target": "prod"})
    assert estado == "accepted" and cid.startswith("cmd")
    j.close()


def test_a7_el_conflicto_de_revision_LLEVA_el_command_id_original(tmp_path):
    """Sin el id, un reintento honesto por respuesta perdida no puede continuar:
    recibe 409 y no sabe qué comando es el suyo."""
    j = jr(tmp_path)
    s = sesion(j, "c-be", principal="p-be", role="be")
    cid, _ = j.submit_command(s.token, workstream_id="w", revision=1,
                              payload={"orden": "x"})
    with pytest.raises(C.CommandRevisionConflict) as e:
        j.submit_command(s.token, workstream_id="w", revision=1,
                         payload={"orden": "otra"})
    assert e.value.command_id == cid
    j.close()


def test_a7_el_conflicto_NO_filtra_el_command_id_de_OTRO_carril(tmp_path):
    """El mismo `(workstream, revision)` en otro carril NO es conflicto — y el
    aislamiento no puede romperse por el camino del mensaje de error."""
    j = jr(tmp_path)
    a, b = sesiones(j,
        {"credential": "c-a", "principal": "p-a", "role": "be", "lane": "carril-uno"},
        {"credential": "c-b", "principal": "p-b", "role": "be", "lane": "carril-dos"})
    cid_a, _ = j.submit_command(a.token, workstream_id="w", revision=1,
                                payload={"orden": "x"})
    cid_b, estado = j.submit_command(b.token, workstream_id="w", revision=1,
                                     payload={"orden": "x"})
    assert estado == "accepted" and cid_b != cid_a
    j.close()


def test_a7_OMEGA_el_mensaje_del_conflicto_no_contiene_ids_ajenos(tmp_path):
    """⊖ del anterior por la otra puerta: aunque el veredicto sea correcto, el
    TEXTO no puede llevar el id del carril vecino."""
    j = jr(tmp_path)
    a, b = sesiones(j,
        {"credential": "c-a", "principal": "p-a", "role": "be", "lane": "carril-uno"},
        {"credential": "c-b", "principal": "p-b", "role": "be", "lane": "carril-dos"})
    cid_a, _ = j.submit_command(a.token, workstream_id="w", revision=1,
                                payload={"orden": "x"})
    j.submit_command(b.token, workstream_id="w", revision=1, payload={"orden": "x"})
    with pytest.raises(C.CommandRevisionConflict) as e:
        j.submit_command(b.token, workstream_id="w", revision=1, payload={"orden": "z"})
    assert cid_a not in str(e.value)
    assert e.value.command_id != cid_a
    j.close()


def test_a1_sin_destinatarios_y_solo_difusion_NO_dan_el_mismo_motivo(tmp_path):
    """Los dos casos acaban en `no_ack_expected` y NO son lo mismo.

    Lo cacé revisando mi propio diff: cerraban con el MISMO motivo, y «todos los
    destinos son de difusión» es falso sobre un evento sin destinos. Un recibo
    que miente en su motivo es peor que uno mudo, porque el que audita lo cita.

    🔻 `to=[]` YA NO ENTRA por `accept_event` — lo rechaza la guarda de `P1-1`,
    la misma que `/append`. El motivo «sin destinatarios» queda para las filas
    LEGADAS (pre-v4, `recipients_roles IS NULL` o vacío sin difusión), que es
    donde de verdad se va a leer. Se prueba por esa vía: fabricar el estado
    legado a mano es lo único honesto cuando la puerta nueva ya no lo produce.
    """
    import json as _json
    import sqlite3
    j = jr(tmp_path)
    s = sesion(j, "c-cto", principal="p-cto", role="cto")

    ev_dif = _hasta_indexed(j, s, ["FLOTA"], key="dif")
    rc = j.receipt_for_event(s.token, ev_dif.event_id)
    assert rc["current_state"] == "no_ack_expected"
    motivo_dif = next(_json.loads(t["detail"])["motivo"]
                      for t in j.transitions(s.token, rc["receipt_id"])
                      if t["state"] == "no_ack_expected")

    # Fila LEGADA: aceptada con difusión y luego despojada de ella, que es la
    # forma exacta de una v3 migrada sin censo.
    ev_leg = j.accept_event(s.token, idempotency_key="leg",
                            intent={**INTENT, "to": ["FLOTA"]}, ledger="llminbox")
    con = sqlite3.connect(str(tmp_path / "coordination.sqlite"))
    con.execute("UPDATE events SET recipients_broadcast='[]' WHERE event_id=?",
                (ev_leg.event_id,))
    con.commit(); con.close()
    item = j.claim_outbox(s.token, lease_s=60)
    j.mark_materialized(s.token, ev_leg.event_id, entry_eid="e" * 64,
                        ledger="llminbox", claim_token=item.claim_token)
    j.mark_indexed(s.token, ev_leg.event_id, index_ref="idx")
    rc2 = j.receipt_for_event(s.token, ev_leg.event_id)
    motivo_vacio = next(_json.loads(t["detail"])["motivo"]
                        for t in j.transitions(s.token, rc2["receipt_id"])
                        if t["state"] == "no_ack_expected")

    assert motivo_dif != motivo_vacio
    assert "difusión" in motivo_dif and "difusión" not in motivo_vacio
    j.close()


def _test_a1_viejo_sin_destinatarios(tmp_path):
    """Los dos acaban en `no_ack_expected` y NO son lo mismo.

    Lo cacé revisando mi propio diff: `to=[]` cerraba con «todos los destinos
    son de difusión», que es falso — no hay destinos. Un recibo que miente en su
    motivo es peor que uno mudo, porque el que audita lo cita.

    (Rechazar `to=[]` sería la guarda de gramática de `A2`, que está fuera del
    alcance de este trabajo. Aquí sólo se exige que el motivo diga la verdad.)
    """
    j = jr(tmp_path)
    s = sesion(j, "c-cto", principal="p-cto", role="cto")
    motivos = {}
    for clave, to in (("vacio", []), ("difusion", ["FLOTA"])):
        ev = _hasta_indexed(j, s, to, key=clave)
        rc = j.receipt_for_event(s.token, ev.event_id)
        assert rc["current_state"] == "no_ack_expected"
        motivos[clave] = next(
            __import__("json").loads(t["detail"])["motivo"]
            for t in j.transitions(s.token, rc["receipt_id"])
            if t["state"] == "no_ack_expected")
    assert motivos["vacio"] != motivos["difusion"]
    assert "difusión" not in motivos["vacio"]
    assert "difusión" in motivos["difusion"]
    j.close()
