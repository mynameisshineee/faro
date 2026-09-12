"""Falsificadores de las 6 garantías (docs/GUARANTEES.md) sobre la app SERVIDA
por la factory ``runtime_root.create_app``.

SUJETO (a) — ``create_app()`` EN PROCESO (criterio de @vision-canon, ledger
#1666): esta pieza hace que ``create_app()`` lea un entorno ``LLMINBOX_*`` de
prueba y sirve el resultado con ``TestClient``. Demuestra que la COMPOSICIÓN
que la factory ensambla cumple las garantías. NO demuestra el artefacto que la
imagen del 8077 sirve (cond ②): ese es el sujeto (b) y necesita el flip del CMD
(``8cfa178``, ya escrito por @infra con ``--factory``) y la ventana de @sdet.

Contrato de la pieza (@engineering-manager #1662, aceptación): UNA aserción por
garantía contra la app SERVIDA — nunca contra un import del módulo —, cada una
con su cara negativa, y la cota respetada: NADA aquí descansa en «≠404 prueba
que el router está montado»; cada test es EL FALSADOR de su garantía según
docs/GUARANTEES.md, con sus caras predichas por delante de la corrida
(requisito de @qa #1667).

**G6 (6º test, añadido por el ruling de @cto #1677/#1681)**: la escalación de
la entrega original («G6 sin superficie ejercitable sobre la app servida»)
tenía la premisa falsa — la composición MONTA la app legacy entera
(``app.mount("/", legacy_app)``, ``runtime_root.py:626``) y sirve su
``GET /search`` (FTS5 + keyset + truncación EN EL CUERPO) a través de la MISMA
app; el canon (``docs/GUARANTEES.md:173``) llama *native search* a ESA MISMA
implementación FTS5, con ``Status: IMPLEMENTED`` y sin condicional. El hueco
era del ARNÉS, no de contrato: ``test_g6_...`` provisiona el índice con la API
pública ``servicio.preparar_busqueda_publica`` y ejercita la ruta sobre la app
servida — mismo sujeto (a), mismo presupuesto que G1-G5.

Ejecución: NO se corre aquí (turno serial de @sdet; cuota/host: bandeja
#1630 y auditoría #1667). Para ESCRIBIRSE no depende de nada; para CORRERSE
depende del flip del CMD (@infra) y del turno de @sdet.
"""

from __future__ import annotations

import hashlib
import re
import json

import pytest
from fastapi.testclient import TestClient

import coordination as C
import native_gateway as G
import runtime_root as R
import servicio


# El mapa V8 atestado: ÚNICA fuente de credenciales de la factory
# (``RuntimeConfig.from_environment`` exige exactamente un mapa y su SHA).
# Capacidades de PILOTO (la traducción cerrada a permisos de operación vive en
# ``runtime_root.PILOT_CAPABILITY_GRANTS``): la factory aborta con un nombre
# fuera de contrato en vez de degradarse a cero permisos.
_MAPA_V8 = {
    "cred-factory-autor-a": {
        "principal": "autor-a", "rol": "be", "carril": "lane-a",
        "capacidades": ["session", "events.write", "leases"],
    },
    "cred-factory-autor-b": {
        "principal": "autor-b", "rol": "be", "carril": "lane-b",
        "capacidades": ["session", "events.write"],
    },
    "cred-factory-obrera": {
        "principal": "obrera-a", "rol": "worker", "carril": "lane-a",
        "capacidades": ["session", "outbox.project"],
    },
    "cred-factory-admision-a": {
        "principal": "admision-a", "rol": "infra", "carril": "lane-a",
        "capacidades": ["session", "admission.operate"],
    },
    "cred-factory-tenedora-b": {
        "principal": "tenedora-b", "rol": "worker", "carril": "lane-a",
        # `events.write` NO es decoración: G4③ hace al relevo ESCRIBIR el evento
        # cercado, y el gateway exige la capacidad ANTES del fencing
        # (``_mutation_token`` → POLICY_DENIED). Primera medida servida (qa
        # #2092, clase B): sin ella, el relevo legítimo moría en 403 y el
        # falsador de G4 nunca llegaba a ejercitar la transferencia de autoridad.
        "capacidades": ["session", "leases", "events.write"],
    },
}

_INTENT = {
    "type": "message", "verb": "inform", "to": ["difusion-factory"],
    "kind": "DELIVERED", "head": "ready", "body": "payload",
}

# Aguja de G6: UN SOLO token alfanumérico sin diacríticos — ni operadores FTS5
# ni separadores que el tokenizer (unicode61) parta. Las 5 entradas la llevan.
_AGUJA = "agujafabrica"


@pytest.fixture()
def fabrica(tmp_path, monkeypatch):
    """Deja el entorno listo y devuelve ``crear()``, que INVOCA LA FACTORY.

    Cada llamada ``crear()`` re-corre ``RuntimeConfig.from_environment`` contra
    el entorno parcheado — llamarla DOS veces es literalmente un reinicio del
    proceso servido (lo que G5 necesita). El lifespan legacy se vuelve inerte
    (patrón de ``tests/runtime/test_runtime_root.py``): esta pieza no depende
    del índice global de servicio.
    """
    raw = json.dumps(_MAPA_V8, sort_keys=True, separators=(",", ":")).encode()
    pepper = tmp_path / "pepper"
    pepper.write_bytes(b"p" * 40)
    pepper.chmod(0o600)

    # Censo hermético y receptor determinista: el censo real de la flota no es
    # sujeto de esta pieza (mismo criterio que el arnés casa con su ``censo``).
    monkeypatch.setattr(R.lp, "canon_identidad", lambda valor: str(valor).strip().lower())
    monkeypatch.setattr(R.lp, "rol_de", lambda valor: valor)
    monkeypatch.setattr(R, "_recipient_resolver", lambda literal: (None, True))

    monkeypatch.setattr(servicio, "CARRIL_LEDGER",
                        {"lane-a": "ledger-factory-a", "lane-b": "ledger-factory-b"})
    monkeypatch.setattr(servicio, "_BYTES_MAPA", [raw])
    monkeypatch.setenv("LLMINBOX_JOURNAL", str(tmp_path / "coordination.sqlite"))
    monkeypatch.setenv("LLMINBOX_PEPPER_FILE", str(pepper))
    monkeypatch.setenv("LLMINBOX_CREDENCIALES_SHA", hashlib.sha256(raw).hexdigest())
    monkeypatch.delenv("LLMINBOX_PROJECTOR_MODE", raising=False)
    monkeypatch.delenv("LLMINBOX_FLEET_DEADLINE_PRINCIPALS", raising=False)

    @R.asynccontextmanager
    async def legacy_inerte(_app):
        yield

    monkeypatch.setattr(servicio.app.router, "lifespan_context", legacy_inerte)

    def crear() -> TestClient:
        return TestClient(R.create_app())

    return crear


@pytest.fixture()
def busqueda(fabrica, tmp_path, monkeypatch):
    """Capa sobre ``fabrica`` que provisiona el índice FTS5 del ``/search`` montado.

    La ruta vive en la app LEGACY que la composición sirve por montaje
    (``runtime_root.py:626``), así que el provisioning es de servicio, no del
    Journal: ``servicio.preparar_busqueda_publica`` — API pública para arneses
    (su docstring lo dice a propósito) — construye el índice con la MISMA
    configuración que usará el handler (clave de cursor del entorno y ACL
    derivada del ``CARRIL_LEDGER`` parcheado). La credencial es un mapa legacy
    AD-HOC, separado del mapa V8: ``scope_desde_credencial`` (search_store.py)
    exige ``principal_id`` explícito, que el esquema estricto V8 no lleva.
    """
    from tests.search.conftest import ESQUEMA_ENTRIES  # patrón del test de escala

    monkeypatch.setenv("LLMINBOX_SEARCH_CURSOR_KEY",
                       "clave-cursor-busqueda-fabrica-32-bytes!")
    monkeypatch.setattr(servicio, "TOKEN", "token-compartido-fabrica")
    monkeypatch.setattr(servicio, "CREDENCIALES", {
        "cred-factory-busqueda": {
            "rol": "be", "carril": "lane-a", "principal_id": "principal-busqueda",
        },
    })
    monkeypatch.setattr(servicio, "DB", str(tmp_path / "busqueda.sqlite"))

    con = servicio.db()  # registra la UDF que la vista del índice necesita
    try:
        con.executescript(ESQUEMA_ENTRIES)
        for n in range(5):
            eid = hashlib.sha256(f"g6:{n}".encode()).hexdigest()
            head = f"titular busqueda {n}"
            cuerpo = f"{head}\ncontenido de relleno numero {n} {_AGUJA}"
            con.execute(
                "INSERT INTO entries(ledger,eid,arrival,ts,actor,tipo,head,body)"
                " VALUES(?,?,?,?,?,?,?,?)",
                ("ledger-factory-a", eid, n, "2026-09-09", "autor-a", "FYI",
                 head, cuerpo))
        con.commit()
        servicio.preparar_busqueda_publica(con)
    finally:
        con.close()
    return fabrica


def _sesion(client: TestClient, credencial: str) -> dict:
    respuesta = client.post(
        G.API_PREFIX + "/sessions",
        headers={"Authorization": f"Bearer {credencial}"},
        json={"ttl_s": 300},
    )
    assert respuesta.status_code == 201, respuesta.text
    return respuesta.json()


def _auth(token: str, **cabeceras) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **cabeceras}


def _evento(client: TestClient, token: str, clave: str = "idem-factory-1",
            **cambios):
    cuerpo = dict(_INTENT)
    cuerpo.update(cambios)
    return client.post(
        G.API_PREFIX + "/events",
        headers=_auth(token, **{"Idempotency-Key": clave}),
        json=cuerpo,
    )


def _abre_puerta(journal: C.Journal) -> None:
    """Mueve la barrera de admisión del carril ``lane-a`` como haría el operador.

    No hay verbo HTTP servido para esto a propósito (el router de admisión
    exige proyector activo para ``open``; esta composición lo trae
    ``disabled``): mover la puerta es un acto de operador sobre el Core, no un
    efecto de la pasarela — mismo patrón de ``abre_barrera`` en
    ``tests/runtime/test_runtime_root.py``.
    """
    operador = journal.open_session("cred-factory-admision-a")
    for verbo in C.ADMISSION_VERBS:
        journal.open_admission(operador.token, verbo, reason_code="ROLLOUT")


def _eventos(journal: C.Journal) -> int:
    return journal._connect().execute(
        "SELECT COUNT(*) c FROM events").fetchone()["c"]


def _recibos_aceptados(journal: C.Journal) -> int:
    # Sólo recibos de ACEPTACIÓN: cada rechazo auditable deja su recibo
    # ``state='denied'`` (``_record_denial`` → ``_open_receipt(..., "denied")``,
    # coordination.py) y ese rastro es parte del diseño, no una mutación del
    # verbo. La cara de las garantías cuenta lo que el verbo ACEPTÓ.
    return journal._connect().execute(
        "SELECT COUNT(*) c FROM receipts WHERE current_state='accepted'").fetchone()["c"]


def test_g1_la_identidad_del_registro_es_la_de_la_credencial_y_la_declarada_no_entra(fabrica):
    """G1 — «Identity is not self-declared» (docs/GUARANTEES.md).

    FALSADOR: «A credential for role A writes an entry declaring role B. If
    the record shows B, G1 is not met.»
    SUJETO (a): factory ``create_app()`` en proceso.

    CARA NEGATIVA (el canal de autodeclaración): cuerpo, cabecera y query que
    intentan declarar principal/rol/carril se RECHAZAN con 400
    ``ATTRIBUTION_REJECTED`` y dejan el Journal en CERO mutaciones aceptadas
    (cada rechazo deja su recibo ``denied`` — rastro de auditoría, no mutación).
    CARA POSITIVA (lo que el registro muestra): ``/whoami`` y el registro
    durable del evento (el job que el outbox entrega) llevan la identidad
    DERIVADA de la credencial — ``autor-a`` / rol ``be`` / ``lane-a`` — y la
    identidad declarada no aparece.
    """
    with fabrica() as client:
        journal = client.app.state.native_journal

        autor = _sesion(client, "cred-factory-autor-a")
        for canal, clave, valor in (
                ("body", "principal", "falso-autor"),
                ("body", "rol", "cto"),
                ("body", "runtime_instance", "runtime-falso"),
                ("header", "X-Llminbox-Role", "cto"),
                ("query", "lane", "lane-b")):
            cuerpo = dict(_INTENT)
            url = G.API_PREFIX + "/events"
            cabeceras = _auth(autor["token"],
                              **{"Idempotency-Key": f"g1-{canal}-{clave}"})
            if canal == "body":
                cuerpo[clave] = valor
            elif canal == "header":
                cabeceras[clave] = valor
            else:
                url += f"?{clave}={valor}"
            rechazo = client.post(url, headers=cabeceras, json=cuerpo)
            assert rechazo.status_code == 400, rechazo.text
            assert rechazo.json()["code"] == "ATTRIBUTION_REJECTED"
        assert _eventos(journal) == 0 and _recibos_aceptados(journal) == 0

        whoami = client.get(G.API_PREFIX + "/whoami", headers=_auth(autor["token"]))
        assert whoami.status_code == 200
        assert whoami.json()["principal"] == "autor-a"
        assert whoami.json()["role"] == "be"
        assert whoami.json()["lane"] == "lane-a"
        falsificado = client.get(G.API_PREFIX + "/whoami",
                                 headers=_auth(autor["token"], **{"X-Role": "cto"}))
        assert falsificado.status_code == 400
        assert falsificado.json()["code"] == "ATTRIBUTION_REJECTED"

        _abre_puerta(journal)
        aceptado = _evento(client, autor["token"], clave="g1-registro")
        assert aceptado.status_code == 202, aceptado.text
        obrera = _sesion(client, "cred-factory-obrera")
        claim = client.post(G.API_PREFIX + "/outbox/claims",
                            headers=_auth(obrera["token"]), json={"lease_s": 60})
        assert claim.status_code == 200, claim.text
        job = claim.json()["job"]
        assert job["event_id"] == aceptado.json()["event_id"]
        assert job["principal"] == "autor-a"
        assert job["role"] == "be"
        assert job["lane"] == "lane-a"
        # 🩸 subcadena NO: «cto» vive dentro de «fa-cto-ry» y el propio fixture se
        # llama ledger-factory-a / difusion-factory. La garantia es que la identidad
        # DECLARADA no entra como VALOR, no que su texto no aparezca nunca.
        for impostor in ("falso-autor", "cto"):
            assert not re.search(rf'"{impostor}"', claim.text), impostor


def test_g2_graduacion_por_carril_y_el_append_que_bypasa_no_autoriza(fabrica, tmp_path):
    """G2 — «Authority is gradable, not all-or-nothing» (docs/GUARANTEES.md).

    FALSADOR: «With one verb graduated in one lane, an append that bypasses
    the service still counts as authoritative for that verb. If it does, the
    graduation did not happen.»
    SUJETO (a): factory ``create_app()`` en proceso.

    CARAS: ① sin graduación, ``POST /events`` es 409 ``ADMISSION_CLOSED`` — la
    autoridad nativa no es el default; ② el operador gradúa SOLO ``lane-a``
    (acto de Core, ver ``_abre_puerta``) y entonces ``lane-a`` escribe 202 con
    recibo mientras ``lane-b``, con LA MISMA capacidad de escritura, sigue en
    409 — no hay interruptor global; ③ EL FALSADOR: un append markdown crudo
    al ledger del carril graduado NO deja huella autoritativa — ni un evento
    ni un recibo más en el Journal.

    Cota honesta de ③: lo medido es que NINGÚN componente de la composición
    ingiere appends markdown como autoridad del verbo graduado (``events.accept``
    se exige DENTRO de la transacción que muta, ``coordination.py``). El
    tratamiento de lectura legacy del markdown es modo Legacy y queda fuera de
    la autoridad del verbo graduado (tabla de modos de GUARANTEES.md).
    """
    with fabrica() as client:
        journal = client.app.state.native_journal
        autor_a = _sesion(client, "cred-factory-autor-a")
        autor_b = _sesion(client, "cred-factory-autor-b")

        cerrada = _evento(client, autor_a["token"], clave="g2-cerrada")
        assert cerrada.status_code == 409
        assert cerrada.json()["code"] == "ADMISSION_CLOSED"
        assert _eventos(journal) == 0

        _abre_puerta(journal)

        graduada = _evento(client, autor_a["token"], clave="g2-graduada")
        assert graduada.status_code == 202, graduada.text
        assert graduada.json()["receipt_id"]

        otra_lane = _evento(client, autor_b["token"], clave="g2-lane-b")
        assert otra_lane.status_code == 409
        assert otra_lane.json()["code"] == "ADMISSION_CLOSED"

        antes = (_eventos(journal), _recibos_aceptados(journal))
        assert antes == (1, 1)

        ledger = tmp_path / "ledger-factory-a.md"
        ledger.write_text(
            "2026-09-09 12:00:00 | be | G2-BYPASS | append crudo fuera del "
            "servicio\n", encoding="utf-8")
        assert (_eventos(journal), _recibos_aceptados(journal)) == antes


def test_g3_misma_clave_mismo_cuerpo_mismo_recibo_y_otro_cuerpo_rechazado(fabrica):
    """G3 — «Delivery is demonstrable» (docs/GUARANTEES.md).

    FALSADOR: «Same key and same body must return the original receipt with no
    second mutation; same key and a *different* body must be rejected with no
    mutation at all. If either mutates twice, G3 is not met.»
    SUJETO (a): factory ``create_app()`` en proceso.

    CARAS: replay (misma clave + mismo cuerpo) devuelve 200 ``replayed=True``
    con el MISMO ``event_id`` y ``receipt_id``; conflicto (misma clave + otro
    cuerpo) es 409 ``IDEMPOTENCY_CONFLICT``; y en ambos el Journal sigue con
    EXACTAMENTE un evento — cero segunda mutación.
    """
    with fabrica() as client:
        journal = client.app.state.native_journal
        _abre_puerta(journal)
        autor = _sesion(client, "cred-factory-autor-a")

        primero = _evento(client, autor["token"], clave="g3-clave")
        assert primero.status_code == 202, primero.text
        aceptacion = primero.json()

        replay = _evento(client, autor["token"], clave="g3-clave")
        assert replay.status_code == 200, replay.text
        assert replay.json()["replayed"] is True
        assert replay.json()["event_id"] == aceptacion["event_id"]
        assert replay.json()["receipt_id"] == aceptacion["receipt_id"]

        conflicto = _evento(client, autor["token"], clave="g3-clave",
                            body="cuerpo-distinto")
        assert conflicto.status_code == 409
        assert conflicto.json()["code"] == "IDEMPOTENCY_CONFLICT"

        assert _eventos(journal) == 1 and _recibos_aceptados(journal) == 1


def test_g4_tras_relevo_la_escritura_con_la_valla_caducada_es_rechazada(fabrica):
    """G4 — «A job has exactly one live owner» (docs/GUARANTEES.md).

    FALSADOR: «After a takeover, a write carrying the stale fence must be
    rejected. If it lands, the lease is decoration.»
    SUJETO (a): factory ``create_app()`` en proceso. El relevo real por HTTP es
    liberar y re-adquirir (``PUT /leases`` es 501 ``CORE_RENEW_NOT_STRICT`` a
    propósito); cada adquisición INCREMENTA la valla monótona.

    CARAS: ① con la valla VIGENTE, la escritura cercada entra (202) y la valla
    se ve en el wire; ② tras el relevo (autor-a libera, tenedora-b adquiere),
    la escritura de autor-a con la valla CADUCADA es 409 ``FENCING_CONFLICT`` y
    no muta; ③ la del nuevo dueño con su valla nueva SÍ entra — el relevo
    transfiere la autoridad, no la duplica.
    """
    with fabrica() as client:
        journal = client.app.state.native_journal
        _abre_puerta(journal)
        autor = _sesion(client, "cred-factory-autor-a")
        relevo = _sesion(client, "cred-factory-tenedora-b")
        recurso = "recurso-fabricante-g4"

        primera = client.post(G.API_PREFIX + f"/leases/{recurso}",
                              headers=_auth(autor["token"]), json={"ttl_s": 300})
        assert primera.status_code == 201, primera.text
        valla_autor = primera.json()["fencing_token"]

        con_valla = _evento(client, autor["token"], clave="g4-vigente",
                            fenced_resource=recurso, fencing_token=valla_autor)
        assert con_valla.status_code == 202, con_valla.text
        assert _eventos(journal) == 1

        liberada = client.delete(G.API_PREFIX + f"/leases/{recurso}",
                                 headers=_auth(autor["token"]))
        assert liberada.status_code == 204
        nueva = client.post(G.API_PREFIX + f"/leases/{recurso}",
                            headers=_auth(relevo["token"]), json={"ttl_s": 300})
        assert nueva.status_code == 201, nueva.text
        valla_relevo = nueva.json()["fencing_token"]
        assert valla_relevo == valla_autor + 1  # la valla es monótona

        caducada = _evento(client, autor["token"], clave="g4-caducada",
                           fenced_resource=recurso, fencing_token=valla_autor)
        assert caducada.status_code == 409
        assert caducada.json()["code"] == "FENCING_CONFLICT"
        assert _eventos(journal) == 1  # la escritura con valla caducada NO entra

        del_nuevo = _evento(client, relevo["token"], clave="g4-relevo",
                            fenced_resource=recurso, fencing_token=valla_relevo)
        assert del_nuevo.status_code == 202, del_nuevo.text
        assert _eventos(journal) == 2


def test_g5_kill_antes_de_la_proyeccion_reabre_exacto_y_drena(fabrica):
    """G5 — «Recovery is unambiguous» (docs/GUARANTEES.md).

    FALSADOR: «Kill the process between the durable write and its projection,
    then restart: the entry must exist exactly once and the pending work must
    still be drainable. Duplicate or vanish, and G5 is not met.»
    SUJETO (a): factory ``create_app()`` en proceso — y el REINICIO es LA
    FACTORY OTRA VEZ: ``crear()`` re-lee el entorno y reabre el MISMO
    ``LLMINBOX_JOURNAL``; la «muerte» es ``dispose()`` del journal de la app 1
    tras salir del TestClient. La proyección no puede haber pasado: el
    proyector nace ``disabled``, así que el trabajo queda pendiente en el
    outbox — exactamente el hueco que el falsador exige.

    CARAS: tras reabrir, el recibo del evento sigue ahí y es EL MISMO (200);
    el replay de la misma clave+cuerpo devuelve 200 ``replayed=True`` con los
    identificadores originales y el Journal sigue con UN solo evento — ni
    duplicado ni desaparecido; y el trabajo pendiente es DRENABLE: el claim
    del outbox entrega ese ``event_id`` y se materializa (204).
    """
    crear = fabrica
    with crear() as app1:
        journal1 = app1.app.state.native_journal
        _abre_puerta(journal1)
        autor = _sesion(app1, "cred-factory-autor-a")
        aceptado = _evento(app1, autor["token"], clave="g5-clave")
        assert aceptado.status_code == 202, aceptado.text
        ids = aceptado.json()
        journal1.dispose()  # la muerte: se sueltan las conexiones del proceso 1

    with crear() as app2:
        journal2 = app2.app.state.native_journal
        autor2 = _sesion(app2, "cred-factory-autor-a")

        recibo = app2.get(G.API_PREFIX + f"/receipts/{ids['receipt_id']}",
                          headers=_auth(autor2["token"]))
        assert recibo.status_code == 200, recibo.text
        assert recibo.json()["receipt_id"] == ids["receipt_id"]
        assert recibo.json()["state"] == "accepted"

        replay = _evento(app2, autor2["token"], clave="g5-clave")
        assert replay.status_code == 200, replay.text
        assert replay.json()["replayed"] is True
        assert replay.json()["event_id"] == ids["event_id"]
        assert replay.json()["receipt_id"] == ids["receipt_id"]
        assert _eventos(journal2) == 1 and _recibos_aceptados(journal2) == 1

        obrera = _sesion(app2, "cred-factory-obrera")
        claim = app2.post(G.API_PREFIX + "/outbox/claims",
                          headers=_auth(obrera["token"]), json={"lease_s": 60})
        assert claim.status_code == 200, claim.text
        job = claim.json()["job"]
        assert job["event_id"] == ids["event_id"]
        materializado = app2.post(
            G.API_PREFIX + f"/outbox/{ids['event_id']}/materialized",
            headers=_auth(obrera["token"]),
            json={"entry_eid": "e" * 64, "ledger": "ledger-factory-a",
                  "claim_token": job["claim_token"], "byte_off": 17},
        )
        assert materializado.status_code == 204, materializado.text


def test_g6_la_consulta_que_excede_una_pagina_lo_declara_en_el_cuerpo(busqueda):
    """G6 — «Reading at scale, without turning search into a boundary»
    (docs/GUARANTEES.md:173, ``Status: IMPLEMENTED`` — ruling @cto #1681: el
    canon llama *native search* a ESTA implementación FTS5; la tabla de modos
    es de ESCRITURA, G6 es de LECTURA y no está en ella).

    FALSADOR: «A query whose result exceeds one page must say so in the
    response. If the caller cannot tell a complete answer from a cut one, G6
    is not met.»
    SUJETO (a): factory ``create_app()`` en proceso. La ruta la SIRVE la
    composición por el montaje de la app legacy (``runtime_root.py:626``) —
    ``GET /search`` atraviesa la misma app servida que G1-G5.

    CARAS: ① 5 coincidencias con ``limit=3`` → 200 con ``hay_mas=True``,
    cursor, ``truncado.por="filas"`` y ``servidas=3`` — el corte se DECLARA
    en el cuerpo, no en la documentación; ② CARA NEGATIVA:
    la segunda página cierra con ``hay_mas=False``, ``truncado.por=None`` y
    sin cursor — una respuesta COMPLETA no declara recorte (no puede
    confundirse completa con cortada en NINGUNA de las dos direcciones); ③ la
    puerta: credencial desconocida → 403 SIN el header ``X-Llminbox-Principal``
    — alcance no derivado ni en el rechazo (la identidad viene de la
    credencial, nunca autodeclarada — misma raíz que G1).
    """
    with busqueda() as client:
        credencial = {"X-Llminbox-Token": "cred-factory-busqueda"}

        primera = client.get("/search", params={"q": _AGUJA, "limit": 3},
                             headers=credencial)
        assert primera.status_code == 200, primera.text
        pagina = primera.json()
        assert len(pagina["filas"]) == 3
        assert pagina["hay_mas"] is True
        assert pagina["cursor"]
        assert pagina["truncado"]["por"] == "filas"
        assert pagina["truncado"]["servidas"] == 3
        # ⚠️ `recortadas` NO se asevera aquí como >=1: el candidato la define
        # como filas RETIRADAS por el presupuesto de bytes — la fila que la
        # sonda (`LIMIT n+1`) ve más allá de la página NO cuenta, y ese
        # contrato está defendido con test propio Y mutante dedicado (M55,
        # tests/search/mutantes_m2.py:153; test_cursor_y_keyset.py:162).
        # Primera medida servida (qa #2092, clase C): mi >=1 era
        # sobre-derivación del canon — «say so in the response» ya lo cumple
        # `hay_mas`+`por`+cursor. Si @vision-canon ruled que «explicit
        # truncation» INCLUYE contar lo que quedó fuera, la cura del store
        # está propuesta en `agent/llminbox-search-recortadas` y este assert
        # se vuelve a apretar.
        # La identidad del sobre es la DERIVADA de la credencial (principal_id
        # declarado en el mapa), no autodeclarada por el llamante.
        assert primera.headers["X-Llminbox-Principal"] == "principal-busqueda"

        segunda = client.get("/search",
                             params={"q": _AGUJA, "limit": 3,
                                     "cursor": pagina["cursor"]},
                             headers=credencial)
        assert segunda.status_code == 200, segunda.text
        cola = segunda.json()
        assert len(cola["filas"]) == 2  # 3 + 2: las 5, sin duplicar
        assert cola["hay_mas"] is False
        assert not cola["cursor"]
        assert cola["truncado"]["por"] is None

        desconocida = client.get("/search", params={"q": _AGUJA},
                                 headers={"X-Llminbox-Token": "credencial-inventada"})
        # 🩸 401, no 403: la tabla de rechazos del servicio mapea POR CLASE
        # (native_gateway.py:503 AuthError->SESSION_INVALID->401; :516-517
        # LedgerNotAllowed/PolicyDenied->403). Una credencial INVENTADA es
        # AuthError, no permiso denegado. El canon no nombra codigos (@vision-canon
        # RULING 17:07Z: 0 hits de 401/403 en GUARANTEES.md y PROTOCOL.md, con
        # control positivo). Mi 403 era sobre-derivacion: pedia OTRA clase.
        assert desconocida.status_code == 401, desconocida.text
        # Sin alcance derivado TAMBIÉN en el rechazo: la 403 salta antes del único
        # sitio donde el handler escribe el header de principal (:5895, camino de
        # éxito) — cota de @qa #1695, el docstring no promete lo que no mide.
        assert "X-Llminbox-Principal" not in desconocida.headers
