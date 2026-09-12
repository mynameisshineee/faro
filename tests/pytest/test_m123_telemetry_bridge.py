"""Puente M1/M2/M3: producción lo invoca y la identidad nunca sale del cliente."""
from __future__ import annotations

import re
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import coordination as C
import observability as O
import search_contract as SC
import search_store as S
from telemetry_bridge import SensorPool
from tests.journal._arnes import GRAMATICA, censo


def _fake_bundle(identity, *, wrong_logger_resource=False, output=True):
    attrs = O.LlminboxV1Adapter().resource_attributes(identity)

    class Provider:
        def __init__(self, channel, resource_attrs):
            self.resource = SimpleNamespace(attributes=resource_attrs)
            self._instrument = SimpleNamespace(resource=self.resource)
            if channel == "trace":
                self._active_span_processor = SimpleNamespace(
                    _span_processors=[object()] if output else [])
            elif channel == "metric":
                self._metric_readers = [object()] if output else []
            else:
                self._multi_log_record_processor = SimpleNamespace(
                    _log_record_processors=[object()] if output else [])

        def get_tracer(self, *_args): return self._instrument
        def get_meter(self, *_args): return self._instrument
        def get_logger(self, *_args): return self._instrument

    tp = Provider("trace", attrs); mp = Provider("metric", attrs)
    lp = Provider("log", attrs)
    bundle = O.build_bundle(identity, tracer_provider=tp, meter_provider=mp,
                            logger_provider=lp)
    if wrong_logger_resource:
        bundle.detalle["logger_provider"] = Provider(
            "log", {**attrs, "llminbox.lane": "lane-ajeno"})
    return bundle


def _abre_barrera(ruta, *, pepper, lane="lane-a"):
    """Abre la barrera con un Journal SIN `sensor_factory`, y a propósito.

    La barrera nace cerrada, así que un test que ACEPTA tiene que abrirla. Pero
    `open_admission` pasa por `@_audita` y emite telemetría, y estas pruebas
    MIDEN exactamente qué se emite: hacerlo con el journal instrumentado
    mezclaría el acto del operador con las señales del sujeto medido.
    """
    j = C.Journal(str(ruta), pepper=pepper, lane_ledgers={lane: ["ledger-a"]},
                  grammar=GRAMATICA, recipient_resolver=censo)
    j.initialize()
    j.bind_credential("cred-admision", principal="admision", role="infra",
                      lane=lane, capabilities=(C.CAP_ADMISSION_OPERATOR,))
    operador = j.open_session("cred-admision")
    for verbo in C.ADMISSION_VERBS:
        j.open_admission(operador.token, verbo, reason_code="ROLLOUT")
    j.close()


def _journal(tmp_path, exporter):
    pool = SensorPool(exporter=exporter, enabled=True)
    _abre_barrera(tmp_path / "coordination.sqlite", pepper=b"pepper-de-test")
    j = C.Journal(str(tmp_path / "coordination.sqlite"), pepper=b"pepper-de-test",
                  lane_ledgers={"lane-a": ["ledger-a"]},
                  grammar=GRAMATICA, recipient_resolver=censo,
                  sensor_factory=pool.for_session)
    j.initialize()
    j.bind_credential("cred-a", principal="principal-a", role="backend", lane="lane-a")
    session = j.open_session("cred-a")
    return j, session


def test_journal_emite_exito_y_rechazo_sin_payload_ni_credenciales(tmp_path):
    exp = O.MemoryExporter()
    j, sesion = _journal(tmp_path, exp)
    secreto = "sk-" + "live-NUNCA-EN-TELEMETRIA"
    assert re.fullmatch(r"sk-[A-Za-z0-9_-]{16,}", secreto)
    intent = {"verb": "inform", "kind": "DELIVERED", "to": ["security"],
              "head": "cabecera privada", "body": secreto}
    aceptado = j.accept_event(sesion.token, idempotency_key="idem-secreta",
                              intent=intent, ledger="ledger-a",
                              trace={"trace_id": "a" * 32, "span_id": "b" * 16})
    replay = j.accept_event(sesion.token, idempotency_key="idem-secreta",
                            intent=intent, ledger="ledger-a",
                            trace={"trace_id": "a" * 32, "span_id": "b" * 16})
    with pytest.raises(C.IdempotencyConflict):
        j.accept_event(sesion.token, idempotency_key="idem-secreta",
                       intent={**intent, "body": "otro secreto"}, ledger="ledger-a")

    assert [s.nombre for s in exp.metrics()] == [
        "llminbox.events.accepted", "llminbox.denials"]
    assert replay.replayed is True
    assert {s.resource["llminbox.lane"] for s in exp.signals} == {"lane-a"}
    assert {s.resource["llminbox.principal"] for s in exp.signals} == {
        sesion.principal_id}
    assert {s.resource["service.instance.id"] for s in exp.signals} == {
        sesion.runtime_instance}
    assert {s.resource["llminbox.credential_generation"] for s in exp.signals} == {
        sesion.generation}
    assert exp.spans()[0].attributes["_parent"] == {
        "trace_id": "a" * 32, "span_id": "b" * 16}
    volcado = exp.volcado()
    for prohibido in (secreto, "otro secreto", "idem-secreta", sesion.token,
                       "cabecera privada", "cred-a"):
        assert prohibido not in volcado
    assert aceptado.event_id not in exp.metrics()[0].labels


def _search_store(tmp_path, sensor):
    con = sqlite3.connect(str(tmp_path / "search.sqlite"))
    con.executescript("""
      CREATE TABLE entries(ledger TEXT NOT NULL,eid TEXT NOT NULL,arrival INTEGER,
        seq INTEGER,line_no INTEGER,byte_off INTEGER,ts TEXT,actor TEXT,tipo TEXT,
        head TEXT,body TEXT,visto TEXT,ausente TEXT,provisional INTEGER DEFAULT 0,
        PRIMARY KEY(ledger,eid));
    """)
    store = S.SearchStore(con, cursor_key=b"x" * 32,
                          acl={"lane-a": {"ledger-a"}}, sensor=sensor)
    store.ensure_schema(); store.set_acl({"lane-a": {"ledger-a"}})
    con.execute("INSERT INTO entries(ledger,eid,arrival,head,body) VALUES(?,?,?,?,?)",
                ("ledger-a", "e1", 1, "h", "h aguja secreta"))
    con.commit(); store.rebuild()
    return store


def test_search_emite_una_vez_por_operacion_y_aisla_resource(tmp_path):
    exp = O.MemoryExporter(); pool = SensorPool(exporter=exp, enabled=True)
    scope = S.BoundSearchScope(lane="lane-a", ledgers=frozenset({"ledger-a"}),
                               principal_id="principal-a", role="backend")
    sensor = pool.for_search_scope(scope)
    store = _search_store(tmp_path, sensor)
    respuesta = store.search(scope=scope, ledger="ledger-a",
                             query="aguja secreta")
    assert respuesta["filas"]
    assert len(exp.spans()) == 1 and exp.spans()[0].nombre == "runtime.job"
    assert exp.spans()[0].attributes == {
        "operation": "search", "outcome": "ok", "has_more": False}
    assert "aguja secreta" not in exp.volcado()
    assert exp.spans()[0].resource["llminbox.lane"] == "lane-a"

    otro = S.BoundSearchScope(lane="lane-b", ledgers=frozenset({"ledger-a"}),
                              principal_id="principal-b", role="backend")
    with pytest.raises(S.LaneNotAuthorized):
        store.search(scope=otro, ledger="ledger-a", query="aguja secreta")
    assert len(exp.signals) == 1, "un Resource ajeno no emite bajo el sensor de lane-a"


def test_search_error_tipado_emite_codigo_no_mensaje(tmp_path):
    exp = O.MemoryExporter(); pool = SensorPool(exporter=exp, enabled=True)
    scope = S.BoundSearchScope(lane="lane-a", ledgers=frozenset({"ledger-a"}),
                               principal_id="principal-a", role="backend")
    store = _search_store(tmp_path, pool.for_search_scope(scope))
    secreto = "query-NUNCA-sale"
    with pytest.raises(SC.InvalidLimit) as error:
        store.search(scope=scope, ledger="ledger-a", query=secreto, limit=0)
    assert type(error.value).__name__ == "InvalidLimit"
    assert [s.nombre for s in exp.signals] == [
        "llminbox.tool.failures", "runtime.job"]
    assert secreto not in exp.volcado()
    assert exp.spans()[0].attributes["error_code"] == "InvalidLimit"


def test_gateway_real_entrega_al_search_un_sensor_derivado(
        servicio, monkeypatch):
    """Si se borra el cableado de ``servicio.py``, esta prueba queda sin señales."""
    exp = O.MemoryExporter()
    servicio.configura_observabilidad(exporter=exp, enabled=True)
    monkeypatch.setattr(servicio, "CREDENCIALES", {
        "cred-http-secreta": {"rol": "be", "carril": "demo",
                              "principal_id": "principal-http"}})
    monkeypatch.setenv("LLMINBOX_SEARCH_CURSOR_KEY", "k" * 32)
    with TestClient(servicio.app) as cliente:
        servicio.barrido()
        # El rebuild de M2 es deliberadamente explícito: el lifespan sólo sondea
        # readiness y nunca recorre el corpus. Esta prueba prepara el derivado porque
        # quiere medir el camino Search sano, no el arranque.
        con = servicio.db()
        try:
            servicio.preparar_busqueda_publica(con)
        finally:
            con.close()
        respuesta = cliente.get(
            "/search", params={"q": "cuerpo uno"},
            headers={"X-Llminbox-Token": "cred-http-secreta",
                     "Authorization": "Bearer no-exportar"})
    assert respuesta.status_code == 200, respuesta.text
    assert len(exp.spans()) == 1
    recurso = exp.spans()[0].resource
    assert recurso["llminbox.principal"] == "principal-http"
    assert recurso["llminbox.role"] == "be" and recurso["llminbox.lane"] == "demo"
    assert recurso["service.instance.id"].startswith("gw_")
    volcado = exp.volcado()
    for prohibido in ("cuerpo uno", "cred-http-secreta", "Bearer no-exportar"):
        assert prohibido not in volcado


def test_journal_descarta_factory_de_resource_ajeno_sin_romper_operacion(tmp_path):
    exp = O.MemoryExporter()
    pool = SensorPool(exporter=exp, enabled=True)
    ajeno = pool.sensor(principal="otro", role="intruso", lane="lane-b",
                        runtime_instance="rti-ajeno", credential_generation=7)
    _abre_barrera(tmp_path / "mismatch.sqlite", pepper=b"pepper")
    j = C.Journal(str(tmp_path / "mismatch.sqlite"), pepper=b"pepper",
                  lane_ledgers={"lane-a": ["ledger-a"]},
                  grammar=GRAMATICA, recipient_resolver=censo,
                  sensor_factory=lambda _view: ajeno)
    j.initialize()
    j.bind_credential("cred", principal="p", role="backend", lane="lane-a")
    session = j.open_session("cred")
    accepted = j.accept_event(
        session.token, idempotency_key="k", ledger="ledger-a",
        intent={"verb": "inform", "kind": "AMEND", "to": ["backend"],
                "body": "privado"})
    assert accepted.event_id
    assert exp.signals == [], "un sensor ajeno no atribuye la operación a otro Resource"


def test_denegacion_de_sesion_expirada_usa_identidad_durable(tmp_path):
    now = [1_000.0]
    exp = O.MemoryExporter(); pool = SensorPool(exporter=exp, enabled=True)
    j = C.Journal(str(tmp_path / "expired.sqlite"), pepper=b"pepper",
                  lane_ledgers={"lane-a": ["ledger-a"]},
                  grammar=GRAMATICA, recipient_resolver=censo,
                  sensor_factory=pool.for_session, clock=lambda: now[0])
    j.initialize()
    binding = j.bind_credential("cred-expira", principal="p", role="backend",
                                lane="lane-a")
    session = j.open_session("cred-expira", ttl_s=1)
    now[0] += 2
    with pytest.raises(C.AuthError):
        j.accept_event(session.token, idempotency_key="k", ledger="ledger-a",
                       intent={"verb": "inform", "kind": "AMEND"})
    denial = next(s for s in exp.metrics() if s.nombre == "llminbox.denials")
    assert denial.labels["reason"] == "expired"
    assert denial.resource["llminbox.principal"] == binding.principal_id
    assert denial.resource["service.instance.id"] == session.runtime_instance


def test_denegacion_de_sesion_revocada_conserva_resource_durable(tmp_path):
    exp = O.MemoryExporter(); pool = SensorPool(exporter=exp, enabled=True)
    j = C.Journal(str(tmp_path / "revoked.sqlite"), pepper=b"pepper",
                  lane_ledgers={"lane-a": ["ledger-a"]},
                  grammar=GRAMATICA, recipient_resolver=censo,
                  sensor_factory=pool.for_session)
    j.initialize()
    binding = j.bind_credential("cred-revocada", principal="p", role="backend",
                                lane="lane-a")
    session = j.open_session("cred-revocada")
    j.revoke_session(session.token, session.runtime_instance)
    with pytest.raises(C.AuthError):
        j.accept_event(session.token, idempotency_key="k", ledger="ledger-a",
                       intent={"verb": "inform", "kind": "AMEND"})
    denial = next(s for s in exp.metrics() if s.nombre == "llminbox.denials")
    assert denial.labels["reason"] == "revoked"
    assert denial.resource["llminbox.principal"] == binding.principal_id
    assert denial.resource["service.instance.id"] == session.runtime_instance


def test_identidad_legacy_se_pseudonimiza_y_no_silencia_busqueda(tmp_path):
    exp = O.MemoryExporter(); pool = SensorPool(exporter=exp, enabled=True)
    scope = S.BoundSearchScope(lane="carril con espacio", ledgers=frozenset({"l"}),
                               principal_id="sujeto/🔐", role="rol á")
    sensor = pool.for_search_scope(scope)
    con = sqlite3.connect(str(tmp_path / "legacy.sqlite"))
    con.executescript("""
      CREATE TABLE entries(ledger TEXT NOT NULL,eid TEXT NOT NULL,arrival INTEGER,
        seq INTEGER,line_no INTEGER,byte_off INTEGER,ts TEXT,actor TEXT,tipo TEXT,
        head TEXT,body TEXT,visto TEXT,ausente TEXT,provisional INTEGER DEFAULT 0,
        PRIMARY KEY(ledger,eid));
    """)
    store = S.SearchStore(con, cursor_key=b"x" * 32,
                          acl={scope.lane: {"l"}}, sensor=sensor)
    store.ensure_schema(); store.set_acl({scope.lane: {"l"}}); store.rebuild()
    assert store.search(scope=scope, ledger="l", query="sin resultado")["filas"] == []
    assert len(exp.spans()) == 1
    resource = exp.spans()[0].resource
    assert resource["llminbox.principal"].startswith("pid_")
    assert resource["llminbox.role"].startswith("role_")
    assert resource["llminbox.lane"].startswith("lane_")
    assert "sujeto/🔐" not in exp.volcado()


def test_outbox_emite_link_effect_attempt_y_delivery_sin_replay(tmp_path):
    exp = O.MemoryExporter(); pool = SensorPool(exporter=exp, enabled=True)
    _abre_barrera(tmp_path / "outbox.sqlite", pepper=b"pepper")
    j = C.Journal(str(tmp_path / "outbox.sqlite"), pepper=b"pepper",
                  lane_ledgers={"lane-a": ["ledger-a"]},
                  grammar=GRAMATICA, recipient_resolver=censo,
                  sensor_factory=pool.for_session)
    j.initialize()
    j.bind_credential("worker", principal="worker", role="be", lane="lane-a",
                      capabilities=(C.CAP_OUTBOX_WORKER, C.CAP_INDEXER))
    j.bind_credential("security", principal="security", role="security", lane="lane-a")
    worker = j.open_session("worker"); security = j.open_session("security")
    ctx = {"trace_id": "a" * 32, "span_id": "b" * 16}
    accepted = j.accept_event(
        worker.token, idempotency_key="k", ledger="ledger-a", trace=ctx,
        intent={"verb": "inform", "kind": "DELIVERED",
                "to": ["backend", "security"], "body": "secreto"})
    job = j.claim_outbox(worker.token)
    assert job and job.attempts == 1
    controlled_effect = "sk-" + "live-CLIENT-CONTROLS-EFFECT-ID"
    assert re.fullmatch(r"sk-[A-Za-z0-9_-]{16,}", controlled_effect)
    j.mark_materialized(worker.token, accepted.event_id, entry_eid=controlled_effect,
                        ledger="ledger-a", claim_token=job.claim_token)
    outbox = next(s for s in exp.spans() if s.nombre == "outbox.materialize")
    assert outbox.attributes["effect_id"].startswith("eff_")
    assert outbox.attributes["effect_id"] != controlled_effect
    assert outbox.attributes["attempt"] == 1
    assert outbox.links == ({"trace_id": "a" * 32, "span_id": "b" * 16,
                             "rel": "accepted_by"},)
    assert controlled_effect not in exp.volcado()
    j.mark_indexed(worker.token, accepted.event_id)
    assert j.mark_delivered(worker.token, accepted.event_id) == "indexed"
    before_replay = len(exp.signals)
    assert j.mark_delivered(worker.token, accepted.event_id) == "indexed"
    assert len(exp.signals) == before_replay, "ACK replay no vuelve a contar ni emitir span"
    assert j.mark_delivered(security.token, accepted.event_id) == "delivered"
    delivery = [s for s in exp.spans()
                if s.attributes.get("operation") == "delivery_ack"]
    assert [s.attributes["outcome"] for s in delivery] == [
        "delivery_progress", "delivered"]
    metrics = [s for s in exp.metrics() if s.nombre == "llminbox.receipts.transitions"]
    assert [(m.labels["state"], m.labels["outcome"]) for m in metrics[-2:]] == [
        ("delivery_progress", "retry"), ("delivered", "ok")]


def test_gateway_senala_422_y_readiness_sin_exportar_parametros(
        servicio, monkeypatch):
    exp = O.MemoryExporter(); servicio.configura_observabilidad(exporter=exp, enabled=True)
    monkeypatch.setattr(servicio, "CREDENCIALES", {
        "cred-http": {"rol": "be", "carril": "demo", "principal_id": "p-http"}})
    monkeypatch.setenv("LLMINBOX_SEARCH_CURSOR_KEY", "k" * 32)
    with TestClient(servicio.app) as client:
        servicio.barrido()
        bad = client.get("/search?q=x&principal=forjado",
                         headers={"X-Llminbox-Token": "cred-http"})
        assert bad.status_code == 422
        # Fuerza readiness rojo en el mismo camino productivo del endpoint.
        monkeypatch.setattr(S.SearchStore, "readiness", lambda _self: {"ready": False})
        not_ready = client.get("/search?q=consulta-secreta",
                               headers={"X-Llminbox-Token": "cred-http"})
        assert not_ready.status_code == 503
    codes = [s.attributes.get("error_code") for s in exp.spans()]
    assert codes == ["reserved_parameter", "search_not_ready"]
    for forbidden in ("forjado", "consulta-secreta", "cred-http"):
        assert forbidden not in exp.volcado()


def test_pool_reacredita_bundle_state_y_resource_de_los_tres_providers():
    exp = O.MemoryExporter()
    identity_args = dict(principal="principal", role="backend", lane="lane-a",
                         runtime_instance="rti-1", credential_generation=1)
    identity = O.Identity.server_derived(**identity_args)

    good = _fake_bundle(identity)
    accepted = SensorPool(exporter=exp, bundle_factory=lambda _i: good,
                          enabled=True).sensor(**identity_args)
    assert accepted is not None
    accepted.count("events.accepted", verb="inform", kind="AMEND", outcome="ok")
    assert len(exp.signals) == 1, "el control positivo impide un rechazo vacuo"

    no_output = _fake_bundle(identity, output=False)
    assert no_output.state is O.Pipeline.PIPELINE_UNVERIFIED
    assert SensorPool(bundle_factory=lambda _i: no_output, enabled=True).sensor(
        **identity_args) is None
    assert SensorPool(bundle_factory=lambda _i: None, enabled=True).sensor(
        **identity_args) is None

    provider_swapped = _fake_bundle(identity, wrong_logger_resource=True)
    assert provider_swapped.state is O.Pipeline.READY  # flags viejos: el ataque
    assert SensorPool(bundle_factory=lambda _i: provider_swapped, enabled=True).sensor(
        **identity_args) is None


def test_bundle_mutado_despues_de_acreditar_no_cruza_resource():
    exp = O.MemoryExporter()
    identity_args = dict(principal="principal", role="backend", lane="lane-a",
                         runtime_instance="rti-1", credential_generation=1)
    identity = O.Identity.server_derived(**identity_args)
    candidate = _fake_bundle(identity)
    sensor = SensorPool(exporter=exp, bundle_factory=lambda _i: candidate,
                        enabled=True).sensor(**identity_args)
    assert sensor is not None
    assert sensor.count("events.accepted", verb="inform", kind="AMEND",
                        outcome="ok") is O.ExportResult.ACCEPTED_BY_SDK
    assert len(exp.signals) == 1                         # control positivo

    # Repro exacto: el provider cachea instrumentos; candidate y bundle reconstruido
    # comparten tracer. La mutación ocurre DESPUÉS de que SensorPool lo acreditó.
    candidate.tracer.resource = SimpleNamespace(attributes={
        **O.LlminboxV1Adapter().resource_attributes(identity),
        "llminbox.principal": "mallory", "llminbox.lane": "lane-b"})
    result = sensor.span("runtime.job", attributes={"operation": "post_mutation"})
    assert result is O.ExportResult.NOT_EXPORTED
    assert sensor.state is O.Pipeline.DEGRADED
    assert len(exp.signals) == 1, "la señal mutada no llega al exportador"
