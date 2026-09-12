from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
import yaml
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import native_gateway as G
import runtime_root as R
import servicio


LEGACY_MUTATIONS = {
    ("POST", "/vigilancia/ack"),
    ("POST", "/inbox/{agent}/leido"),
    # c3ca365 (09-07) ata el ack a una concesion del servidor y anade ESTA ruta: muta, y el
    # servicio ya le pone `bloqueo_legacy` (medido). Lo que iba detras era el censo. sdet 11-09.
    ("POST", "/inbox/{agent}/ack"),
    ("POST", "/claim"),
    ("POST", "/claim/cierro"),
    ("POST", "/append"),
}

VALID_REQUESTS = (
    ("/vigilancia/ack", None),
    ("/inbox/backend/leido", {"hasta": {}}),
    ("/claim", {"tema": "t", "agent": "backend", "rol": "ejecuta"}),
    ("/claim/cierro", {"tema": "t", "agent": "backend", "rol": "ejecuta"}),
    ("/append", {
        "ledger": "no-existe", "actor": "backend", "tipo": "REQUEST",
        "head": "h", "to": ["security"], "body": "b",
    }),
)


def config(tmp_path, policy="bridge", *, name="coordination.sqlite", **changes):
    values = dict(
        journal_path=str(tmp_path / name),
        pepper=b"p" * 48,
        lane_ledgers={"llminbox": ("llminbox",)},
        credential_map={
            "credential-a": {
                "rol": "security", "carril": "llminbox",
                "principal_id": "security-workload",
            },
        },
        capability_grants={"security-workload": (G.CAP_EVENT_WRITER,)},
        legacy_mutation_policy=policy,
    )
    values.update(changes)
    return R.RuntimeConfig(**values)


@pytest.fixture()
def legacy_boundary(monkeypatch):
    @R.asynccontextmanager
    async def lightweight_legacy(_app):
        yield

    monkeypatch.setattr(servicio.app.router, "lifespan_context", lightweight_legacy)
    monkeypatch.setattr(servicio, "TOKEN", "legacy-token")
    monkeypatch.setattr(servicio, "_INTEGRIDAD_MAPA", [])
    monkeypatch.setattr(servicio, "exige_ser", lambda *_args, **_kwargs: None)
    return {"X-Llminbox-Token": "legacy-token"}


def test_censo_y_dependencias_cubren_exactamente_las_cinco_mutaciones():
    routes = [route for route in servicio.app.routes if isinstance(route, APIRoute)]
    unsafe = {
        (method, route.path)
        for route in routes
        for method in (route.methods or set())
        if method not in {"GET", "HEAD", "OPTIONS"}
    }
    assert unsafe == LEGACY_MUTATIONS

    guarded = set()
    for route in routes:
        calls = [dependency.call for dependency in route.dependant.dependencies]
        if servicio.bloqueo_legacy in calls:
            guarded.update((method, route.path) for method in (route.methods or set()))
            assert calls[:2] == [servicio.auth, servicio.bloqueo_legacy]
    assert guarded == LEGACY_MUTATIONS


def test_politica_root_es_cerrada_y_el_entorno_no_degrada_a_bridge(
        tmp_path, monkeypatch):
    explicit = config(tmp_path)
    without_policy = dict(explicit.__dict__)
    without_policy.pop("legacy_mutation_policy")
    assert R.RuntimeConfig(**without_policy).legacy_mutation_policy == "bridge"
    assert R._legacy_mutation_policy("bridge") == "bridge"
    assert R._legacy_mutation_policy("native-required") == "native-required"
    for invalid in ("", "native", " bridge", None, True):
        with pytest.raises(R.RuntimeConfigurationError):
            R._legacy_mutation_policy(invalid)
    with pytest.raises(R.RuntimeConfigurationError):
        R.build_app(config(tmp_path, "desconocida"))

    raw = json.dumps({"c": {
        "principal": "p", "rol": "be", "carril": "llminbox",
        "capacidades": ["session"],
    }}).encode()
    pepper = tmp_path / "pepper"
    pepper.write_bytes(b"p" * 40)
    pepper.chmod(0o600)
    monkeypatch.setenv("LLMINBOX_JOURNAL", str(tmp_path / "env.sqlite"))
    monkeypatch.setenv("LLMINBOX_PEPPER_FILE", str(pepper))
    monkeypatch.setenv("LLMINBOX_CREDENCIALES_SHA", hashlib.sha256(raw).hexdigest())
    monkeypatch.setattr(servicio, "CARRIL_LEDGER", {"llminbox": "llminbox"})
    monkeypatch.setattr(servicio, "_BYTES_MAPA", [raw])
    monkeypatch.setattr(R.lp, "canon_identidad", lambda value: value)
    monkeypatch.setattr(R.lp, "rol_de", lambda value: value)
    monkeypatch.delenv(R.LEGACY_MUTATION_POLICY_ENV, raising=False)
    assert R.RuntimeConfig.from_environment().legacy_mutation_policy == "bridge"
    monkeypatch.setenv(R.LEGACY_MUTATION_POLICY_ENV, "ilegal")
    with pytest.raises(R.RuntimeConfigurationError):
        R.RuntimeConfig.from_environment()


def test_native_required_bloquea_cinco_sin_efecto_ni_recibo_anonimo(
        tmp_path, legacy_boundary):
    app = R.build_app(config(tmp_path, "native-required"))
    with TestClient(app) as client:
        for path, body in VALID_REQUESTS:
            response = client.post(path, headers=legacy_boundary, json=body)
            assert response.status_code == 403, (path, response.text)
            assert response.json() == {
                "code": "POLICY_DENIED", "message": "operacion no autorizada",
            }
            assert response.headers["cache-control"] == "no-store"

        no_token = client.post(
            "/claim", json={"tema": "t", "agent": "backend", "rol": "ejecuta"})
        assert no_token.status_code == 401
        malformed = client.post(
            "/claim", headers=legacy_boundary, content=b"{")
        assert malformed.status_code == 422
        health = client.get("/health")
        assert health.status_code == 200
        # La credencial legacy no es una identidad nativa atribuible y este gate
        # no intentó (ni falló) una escritura de auditoría. La ausencia deliberada
        # de recibo anónimo no suspende la auditoría: el latch sólo se enciende
        # cuando una escritura de auditoría requerida falla de verdad.
        assert health.json()["audit_suspended"] is False
        assert client.get("/version", headers=legacy_boundary).status_code == 200

        con = sqlite3.connect(app.state.native_journal.path)
        try:
            assert con.execute("SELECT COUNT(*) FROM denials").fetchone()[0] == 0
            assert con.execute(
                "SELECT COUNT(*) FROM unknown_credentials").fetchone()[0] == 0
            assert con.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
        finally:
            con.close()
    assert not hasattr(servicio.app.state, servicio.LEGACY_MUTATION_POLICY_STATE)


def test_modo_de_projector_desconocido_falla_antes_de_abrir_journal(
        tmp_path):
    journal_path = tmp_path / "coordination.sqlite"
    with pytest.raises(ValueError):
        R.build_app(config(
            tmp_path,
            projector_mode="active",
        ))
    assert not journal_path.exists()


def test_politica_viaja_por_scope_y_dos_roots_no_se_contaminan(
        tmp_path, legacy_boundary):
    off = R.build_app(config(tmp_path, "bridge", name="off.sqlite"))
    on = R.build_app(config(tmp_path, "native-required", name="on.sqlite"))
    payload = VALID_REQUESTS[-1][1]

    def append_status(client):
        return client.post(
            "/append", headers=legacy_boundary, json=payload).status_code

    # 🔻 sdet 2026-09-11: antes se ABRIA Y CERRABA el lifespan de `off` dos veces, y desde
    # 05e17e2 el runner de deadlines prohibe reutilizarse («no se puede reutilizar»), que es
    # un invariante SANO del producto. Con los dos roots VIVOS A LA VEZ se mide lo mismo —y
    # mejor: la no-contaminacion se prueba con ambos en el MISMO proceso, no en serie.
    with TestClient(off) as c_off, TestClient(on) as c_on:
        assert append_status(c_off) == 404
        assert append_status(c_on) == 403
        assert append_status(c_off) == 404
    assert not hasattr(servicio.app.state, servicio.LEGACY_MUTATION_POLICY_STATE)

    with TestClient(servicio.app) as client:
        direct = client.post("/append", headers=legacy_boundary, json=payload)
    assert direct.status_code == 404


def test_estado_de_scope_desconocido_falla_cerrado(legacy_boundary):
    class CorruptPolicyScope:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                scope = dict(scope)
                state = dict(scope.get("state") or {})
                state[servicio.LEGACY_MUTATION_POLICY_STATE] = "corrupta"
                scope["state"] = state
            await self.app(scope, receive, send)

    with TestClient(CorruptPolicyScope(servicio.app)) as client:
        response = client.post(
            "/append", headers=legacy_boundary, json=VALID_REQUESTS[-1][1],
        )
    assert response.status_code == 403
    assert response.json() == {
        "code": "POLICY_DENIED", "message": "operacion no autorizada",
    }
    assert response.headers["cache-control"] == "no-store"


def test_techo_raw_body_sigue_fuera_y_compose_limita_perilla_al_piloto(
        tmp_path, legacy_boundary):
    app = R.build_app(config(
        tmp_path, "native-required", raw_body_max_bytes=8,
    ))
    assert [middleware.cls for middleware in app.user_middleware[:2]] == [
        G.RawBodyLimitMiddleware, R.LegacyMutationPolicyMiddleware,
    ]
    with TestClient(app) as client:
        too_large = client.post(
            "/append", headers=legacy_boundary, content=b"x" * 9,
        )
    assert too_large.status_code == 413
    assert too_large.content == b""

    root = Path(__file__).resolve().parents[2]
    base_compose = yaml.safe_load((root / "docker-compose.yml").read_text())
    pilot_compose = yaml.safe_load((root / "docker-compose.pilot.yml").read_text())
    assert R.LEGACY_MUTATION_POLICY_ENV not in \
        base_compose["services"]["llminbox"]["environment"]
    assert pilot_compose["services"]["gateway"]["environment"][
        R.LEGACY_MUTATION_POLICY_ENV] == "bridge"
