from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

import coordination as C
import native_gateway as G
import observability as O
import projector as P
import projector_runner as PR
import runtime_root as R
import servicio


@pytest.fixture()
def role_census(monkeypatch):
    monkeypatch.setattr(
        R.lp, "canon_identidad", lambda value: str(value).strip().lower())
    monkeypatch.setattr(R.lp, "rol_de", lambda value: value)


def config(tmp_path, **changes):
    values = {
        "journal_path": str(tmp_path / "coordination.sqlite"),
        "pepper": b"p" * 48,
        "lane_ledgers": {"llminbox": ("llminbox",)},
        "credential_map": {
            "credential-a": {
                "rol": "security", "carril": "llminbox",
                "principal_id": "security-workload",
            },
        },
        "capability_grants": {
            "security-workload": (G.CAP_EVENT_WRITER,),
        },
    }
    values.update(changes)
    return R.RuntimeConfig(**values)


def abre_barrera(app, *, lane="llminbox"):
    """Abre la barrera de admisión del carril sobre el journal YA montado.

    El root NO la mueve —y no debe: cerrar un carril es un acto de operador
    sobre el Core, no un efecto de arrancar un proceso—, así que los tests que
    ESCRIBEN por la pasarela la abren aquí, como lo haría ese operador.

    Se liga la credencial ANTES de que el test emita ninguna sesión HTTP: una
    ligadura sube la generación del mapa e invalidaría las ya emitidas.
    """
    journal = app.state.native_journal
    journal.bind_credential("cred-admision-runtime", principal="admision-runtime",
                            role="infra", lane=lane,
                            capabilities=(C.CAP_ADMISSION_OPERATOR,))
    operador = journal.open_session("cred-admision-runtime")
    for verbo in C.ADMISSION_VERBS:
        journal.open_admission(operador.token, verbo, reason_code="ROLLOUT")


def active_config(tmp_path):
    ledger = tmp_path / "llminbox.md"
    ledger.write_bytes(b"")
    return config(
        tmp_path,
        credential_map={
            "projector-credential-with-enough-entropy": {
                "rol": "projector", "carril": "llminbox",
                "principal_id": "pilot-projector",
            },
            "operator-credential-with-enough-entropy": {
                "rol": "infra", "carril": "llminbox",
                "principal_id": "pilot-operator",
            },
        },
        capability_grants={
            "pilot-projector": (C.CAP_OUTBOX_WORKER,),
            "pilot-operator": (C.CAP_ADMISSION_OPERATOR,),
        },
        projector_mode="active",
        projector_lane="llminbox",
        projector_credential="projector-credential-with-enough-entropy",
        projector_frame_key=b"f" * 32,
        projector_targets=(P.LedgerTarget(
            "llminbox", "llminbox", str(ledger)),),
    )


def lightweight_legacy(monkeypatch):
    """Evita que una prueba del root dependa del índice global de servicio."""
    @R.asynccontextmanager
    async def lifespan(_app):
        yield

    monkeypatch.setattr(servicio.app.router, "lifespan_context", lifespan)


def test_root_es_hoja_y_no_entra_en_sus_dependencias():
    for module in (C, G, PR, servicio):
        tree = ast.parse(inspect.getsource(module))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert "runtime_root" not in imports


def test_otel_no_se_filtra_al_root_ni_al_nucleo():
    for module in (R, C, servicio):
        assert "opentelemetry" not in inspect.getsource(module).lower()
    assert "opentelemetry" in inspect.getsource(O).lower()
    probe = subprocess.run(
        [sys.executable, "-c", (
            "import sys; import runtime_root; "
            "assert 'opentelemetry.sdk' not in sys.modules"
        )],
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr


def test_journal_del_root_usa_las_dos_autoridades_reales(tmp_path):
    journal = R._build_journal(config(tmp_path))
    assert journal._grammar.canonical_kind is servicio.lp.canonical_tipo
    assert journal._grammar.normalize_resource is servicio.tema_norm
    assert journal._recipient_resolver is R._recipient_resolver
    assert journal._lane_ledgers == {"llminbox": ("llminbox",)}


def test_root_comparte_un_pool_not_configured_y_lo_posee_solo_en_lifespan(
        tmp_path, monkeypatch):
    previous_pool = servicio.OBSERVABILIDAD
    sdk_was_loaded = "opentelemetry.sdk" in sys.modules
    monkeypatch.setattr(servicio, "tipo_de_destinatario", lambda _value: "agente")
    monkeypatch.setattr(R.lp, "canon_identidad", lambda value: value)
    monkeypatch.setattr(R.lp, "rol_de", lambda value: value)

    @R.asynccontextmanager
    async def lightweight_legacy(_app):
        assert servicio.OBSERVABILIDAD is app.state.observability_pool
        yield

    monkeypatch.setattr(servicio.app.router, "lifespan_context", lightweight_legacy)
    app = R.build_app(config(tmp_path))
    pool = app.state.observability_pool
    journal = app.state.native_journal
    sensor_factory = journal._sensor_factory
    assert sensor_factory.__self__ is pool
    assert servicio.OBSERVABILIDAD is previous_pool

    with TestClient(app) as client:
        abre_barrera(app)
        opened = client.post(
            "/native/v1/sessions",
            headers={"Authorization": "Bearer credential-a"},
        )
        session = opened.json()
        token = session["token"]
        accepted = client.post(
            "/native/v1/events",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "otel-seam"},
            json={
                "type": "message", "verb": "inform", "to": ["security"],
                "kind": "DELIVERED", "head": "h", "body": "b",
            },
        )
        assert accepted.status_code == 202
        session_view = journal.authenticate(token)
        assert session_view is not None
        native_sensor = next(
            sensor for key, sensor in pool._sensores.items()
            if key[0] == session_view.principal_id
        )
        assert native_sensor.state.value == "not_configured"
        assert native_sensor.stats.emitidas == 2
        assert native_sensor.stats.export_ok == 0
        assert native_sensor.stats.ultimo_export_ok is None
        sensor = pool.for_search_lifecycle()
        assert sensor.state.value == "not_configured"
        assert sensor.stats.motivo_exportador == "not_configured"
        assert sensor.bundle is None
        assert ("opentelemetry.sdk" in sys.modules) is sdk_was_loaded

    assert servicio.OBSERVABILIDAD is previous_pool


def test_censo_del_root_separa_agente_difusion_humano_y_desconocido(monkeypatch):
    monkeypatch.setattr(servicio, "tipo_de_destinatario", lambda value: {
        "sec": "agente", "FLOTA": "difusion", "operador": "humano",
    }.get(value, "desconocido"))
    monkeypatch.setattr(R.lp, "canon_identidad", lambda value: {
        "sec": "security", "backend": "be",
    }.get(value))
    monkeypatch.setattr(R.lp, "rol_de", lambda value: value)
    assert R._recipient_resolver("sec") == ("security", False)
    assert R._recipient_resolver("backend") == (None, False)
    assert R._recipient_resolver("FLOTA") == (None, True)
    assert R._recipient_resolver("operador") == (None, False)
    assert R._recipient_resolver("nadie") == (None, False)


@pytest.mark.parametrize(("capability", "expected"), [
    ("session", set()),
    ("events.write", {G.CAP_EVENT_WRITER}),
    ("events.ack", {G.CAP_DELIVERY_ACK}),
    ("events.index", {C.CAP_INDEXER}),
    ("leases", {G.CAP_LEASE_HOLDER}),
    ("commands.submit", {G.CAP_COMMAND_SUBMITTER}),
    ("commands.advance", {C.CAP_COMMAND_WORKER}),
    ("outbox.project", {C.CAP_OUTBOX_WORKER}),
    ("outbox.operate", {C.CAP_OUTBOX_OPERATOR}),
    ("admission.operate", {C.CAP_ADMISSION_OPERATOR}),
])
def test_mapa_piloto_separa_cada_capacidad_sin_colapsar_roles(
        capability, expected, role_census):
    capabilities = ["session"] if capability == "session" else ["session", capability]
    raw = json.dumps({"credential-a": {
        "principal": "workload-a", "rol": "be", "carril": "llminbox",
        "capacidades": capabilities,
    }}).encode()
    credential_map, grants = R._native_v8_configuration(raw)
    assert credential_map == {"credential-a": {
        "rol": "be", "carril": "llminbox", "principal_id": "workload-a",
    }}
    assert set(grants["workload-a"]) == expected


def test_preflight_y_runtime_comparten_vocabulario_de_capacidades():
    from tools import pilot_preflight
    assert pilot_preflight.CAPACIDADES == set(R.PILOT_CAPABILITY_GRANTS)


def test_falta_session_no_crea_una_credencial_implicitamente(role_census):
    raw = json.dumps({"c": {
        "principal": "p", "rol": "be", "carril": "llminbox",
        "capacidades": ["events.write"],
    }}).encode()
    with pytest.raises(R.RuntimeConfigurationError, match="falta la capacidad session"):
        R._native_v8_configuration(raw)


@pytest.mark.parametrize("raw", [
    b'{"c":{"principal":"p","rol":"be","carril":"l","capacidades":["typo"]}}',
    b'{"c":{"principal":"p","rol":"be","carril":"l","capacidades":[],"extra":1}}',
])
def test_mapa_piloto_falla_cerrado_sin_reflejar_entrada(raw, role_census):
    with pytest.raises(R.RuntimeConfigurationError) as caught:
        R._native_v8_configuration(raw)
    assert "credential" not in str(caught.value)


def test_factory_lee_los_mismos_bytes_atestados_y_pepper_por_fichero(
        tmp_path, monkeypatch, role_census):
    raw = b'{"c":{"principal":"p","rol":"be","carril":"l","capacidades":["session"]}}'
    pepper = tmp_path / "pepper"
    pepper.write_bytes(b"p" * 40 + b"\n")
    pepper.chmod(0o600)
    monkeypatch.setenv("LLMINBOX_JOURNAL", str(tmp_path / "journal.sqlite"))
    monkeypatch.setenv("LLMINBOX_PEPPER_FILE", str(pepper))
    monkeypatch.setenv("LLMINBOX_CREDENCIALES_SHA", hashlib.sha256(raw).hexdigest())
    monkeypatch.setattr(servicio, "CARRIL_LEDGER", {"l": "ledger"})
    monkeypatch.setattr(servicio, "_BYTES_MAPA", [raw])
    loaded = R.RuntimeConfig.from_environment()
    assert loaded.pepper == b"p" * 40
    assert loaded.lane_ledgers == {"l": ("ledger",)}
    assert loaded.credential_map["c"]["principal_id"] == "p"


def test_pepper_legible_por_el_grupo_falla_cerrado(tmp_path):
    pepper = tmp_path / "pepper"
    pepper.write_bytes(b"p" * 40)
    pepper.chmod(0o640)
    with pytest.raises(R.RuntimeConfigurationError, match="fuera de su propietario"):
        R._read_regular_file(str(pepper), minimum_bytes=32)


def test_pepper_enlace_fifo_y_cambio_durante_lectura_fallan_cerrado(
        tmp_path, monkeypatch):
    pepper = tmp_path / "pepper"
    pepper.write_bytes(b"p" * 40)
    pepper.chmod(0o600)
    link = tmp_path / "pepper-link"
    link.symlink_to(pepper)
    with pytest.raises(R.RuntimeConfigurationError):
        R._read_regular_file(str(link), minimum_bytes=32)

    fifo = tmp_path / "pepper-fifo"
    os.mkfifo(fifo)
    with pytest.raises(R.RuntimeConfigurationError, match="no es regular"):
        R._read_regular_file(str(fifo), minimum_bytes=32)

    real_fstat = R.os.fstat
    calls = 0

    def changed_fstat(fd):
        nonlocal calls
        calls += 1
        value = real_fstat(fd)
        if calls == 2:
            fields = list(value)
            fields[6] += 1  # st_size
            return os.stat_result(fields)
        return value

    monkeypatch.setattr(R.os, "fstat", changed_fstat)
    with pytest.raises(R.RuntimeConfigurationError, match="cambio durante"):
        R._read_regular_file(str(pepper), minimum_bytes=32)


@pytest.mark.parametrize(("snapshots", "expected"), [
    ([], "0" * 64),
    ([b"{}", b"{}"], hashlib.sha256(b"{}").hexdigest()),
    ([b"{}"], ""),
    ([b"{}"], "f" * 64),
])
def test_factory_rechaza_snapshot_ausente_ambiguo_o_no_atestado(
        tmp_path, monkeypatch, snapshots, expected):
    monkeypatch.setenv("LLMINBOX_JOURNAL", str(tmp_path / "journal.sqlite"))
    monkeypatch.setenv("LLMINBOX_CREDENCIALES_SHA", expected)
    monkeypatch.setattr(servicio, "CARRIL_LEDGER", {"l": "ledger"})
    monkeypatch.setattr(servicio, "_BYTES_MAPA", snapshots)
    with pytest.raises(R.RuntimeConfigurationError):
        R.RuntimeConfig.from_environment()


def test_app_monta_gateway_antes_del_legacy_y_lifespan_posee_el_journal(
        tmp_path, monkeypatch):
    entered = []

    @R.asynccontextmanager
    async def lightweight_legacy(_app):
        entered.append("legacy-up")
        yield
        entered.append("legacy-down")

    monkeypatch.setattr(servicio.app.router, "lifespan_context", lightweight_legacy)
    journal_path = tmp_path / "coordination.sqlite"
    app = R.build_app(config(tmp_path, raw_body_max_bytes=8))
    # FastAPI >=0.116 conserva include_router como un _IncludedRouter perezoso;
    # versiones anteriores aplanaban sus APIRoute en app.routes. La propiedad
    # que importa es la misma en ambos casos: el router nativo precede al mount
    # catch-all del legado.
    native_container = next(
        route for route in app.routes
        if "/native/v1/sessions" in {
            getattr(child, "path", None)
            for child in getattr(
                getattr(route, "original_router", None), "routes", ())
        }
        or getattr(route, "path", None) == "/native/v1/sessions"
    )
    legacy_mount = next(
        route for route in app.routes if getattr(route, "path", None) == "")
    assert app.routes.index(native_container) < app.routes.index(legacy_mount)
    assert app.user_middleware[0].cls is G.RawBodyLimitMiddleware
    assert not journal_path.exists(), "build_app migro el Journal antes del lifespan"
    assert legacy_mount.app is servicio.app
    legacy_gets = {
        route.path for route in legacy_mount.app.routes
        if "GET" in (getattr(route, "methods", set()) or set())
    }
    assert legacy_gets == {
        "/recibos/censo", "/version", "/", "/ui", "/health", "/stat",
        "/carriles", "/roster", "/entries", "/search", "/inbox/{agent}",
        "/cursor/{agent}", "/pendientes", "/canon/pendientes", "/adopcion",
        "/wiki", "/wiki/citas", "/wiki/pagina", "/claim/{tema}",
        "/organigrama", "/claims", "/doctor", "/lint", "/chain/verify",
    }

    with TestClient(app) as client:
        opened = client.post(
            "/native/v1/sessions",
            headers={"Authorization": "Bearer credential-a"})
        assert opened.status_code == 201
        too_large = client.post(
            "/native/v1/sessions", content=b"x" * 9,
            headers={"Authorization": "Bearer credential-a"})
        assert too_large.status_code == 413 and too_large.content == b""
        legacy_too_large = client.post("/append", content=b"x" * 9)
        assert legacy_too_large.status_code == 413
        assert legacy_too_large.content == b""
        assert client.get("/live").status_code == 200
        assert entered == ["legacy-up"]
    assert entered == ["legacy-up", "legacy-down"]
    assert app.state.native_journal._estado == C.Journal.NUEVO


@pytest.mark.parametrize("legacy_policy", ["bridge", "native-required"])
def test_root_posee_un_runner_disabled_independiente_de_politica_legacy(
        tmp_path, monkeypatch, legacy_policy):
    lightweight_legacy(monkeypatch)
    app = R.build_app(config(tmp_path, legacy_mutation_policy=legacy_policy))
    runner = app.state.projector_runner
    assert isinstance(runner, PR.DisabledProjectorRunner)
    assert not hasattr(servicio.app.state, "projector_runner")
    with TestClient(app) as client:
        assert client.get("/live").status_code == 200
        snapshot = runner.snapshot()
        assert snapshot.state is PR.RunnerState.DISABLED
        assert snapshot.pending is None and snapshot.failed is None
        assert snapshot.outbox_certifiable is False
    assert runner.snapshot().state is PR.RunnerState.DISABLED


def test_health_del_root_sombrea_el_adaptador_m1_y_no_filtra_internos(
        tmp_path, monkeypatch):
    lightweight_legacy(monkeypatch)
    monkeypatch.setattr(servicio, "health", lambda: {
        "ok": True,
        "journal": {"motivo": "M1_NO_INTEGRADO", "path": "legacy-canary"},
        "politica": {"motivo": "M1_NO_INTEGRADO"},
        "indice": {"presente": True, "ruta": "/secret/index-canary"},
    })
    app = R.build_app(config(tmp_path))
    journal = app.state.native_journal
    real_health = journal.health

    def health_with_canaries():
        measured = real_health()
        measured.update({
            "path": "/secret/journal-canary",
            "detalle": "pepper-canary credential-canary",
            "pragmas": {"secret": "pragma-canary"},
        })
        return measured

    monkeypatch.setattr(journal, "health", health_with_canaries)
    with TestClient(app) as client:
        assert real_health()["estado"] == "conocida"
        response = client.get("/health")
        assert response.status_code == 200
        document = response.json()
        assert R._coordination_ready(journal) is True
        assert document["ok"] is False
        assert document["audit_suspended"] is False
        assert "journal" not in document and "politica" not in document
        ready = client.get("/ready")
        assert ready.status_code == 503
        assert ready.json() == {"name": "coordination", "state": "not_ready"}
    serialized = response.text
    for canary in (
            "M1_NO_INTEGRADO", "journal-canary", "pepper-canary",
            "credential-canary", "pragma-canary", "index-canary"):
        assert canary not in serialized


def test_health_del_root_falla_cerrado_sin_serializar_la_excepcion(
        tmp_path, monkeypatch):
    lightweight_legacy(monkeypatch)
    monkeypatch.setattr(servicio, "health", lambda: {"ok": True})
    app = R.build_app(config(tmp_path))

    def broken_health():
        raise RuntimeError("pepper-super-secreto")

    monkeypatch.setattr(app.state.native_journal, "health", broken_health)
    with TestClient(app) as client:
        health = client.get("/health")
        ready = client.get("/ready")
        live = client.get("/live")
    assert health.status_code == 200 and ready.status_code == 503
    assert live.status_code == 200
    assert health.json()["audit_suspended"] is True
    assert ready.json() == {"name": "coordination", "state": "not_ready"}
    assert "pepper-super-secreto" not in health.text


def test_health_del_root_exige_que_legacy_y_journal_estén_listos(
        tmp_path, monkeypatch):
    lightweight_legacy(monkeypatch)
    monkeypatch.setattr(servicio, "health", lambda: {"ok": False})
    app = R.build_app(config(tmp_path))
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["ok"] is False


@pytest.mark.parametrize("mutation", [
    lambda value: None,
    lambda value: {k: v for k, v in value.items() if k != "schema_too_new"},
    lambda value: {**value, "schema_too_new": "false"},
    lambda value: {**value, "estado": "inventado"},
    lambda value: {**value, "durable_v_stored": str(C.DURABLE_V)},
])
def test_ready_del_root_falla_cerrado_ante_medicion_incompleta(
        tmp_path, monkeypatch, mutation):
    lightweight_legacy(monkeypatch)
    monkeypatch.setattr(servicio, "health", lambda: {"ok": True})
    app = R.build_app(config(tmp_path))
    journal = app.state.native_journal
    real_health = journal.health

    def malformed_health():
        return mutation(real_health())

    monkeypatch.setattr(journal, "health", malformed_health)
    with TestClient(app) as client:
        assert R._coordination_ready(journal) is False
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["ok"] is False


def test_live_no_consulta_legacy_ni_journal(tmp_path, monkeypatch):
    lightweight_legacy(monkeypatch)
    app = R.build_app(config(tmp_path))

    def touched():
        raise AssertionError("liveness consultó una dependencia")

    monkeypatch.setattr(servicio, "health", touched)
    monkeypatch.setattr(app.state.native_journal, "health", touched)
    with TestClient(app) as client:
        response = client.get("/live")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"ok": True, "state": "live"}


def test_lifespan_ordena_initialize_configure_legacy_y_dispose(tmp_path, monkeypatch):
    events = []

    @R.asynccontextmanager
    async def observed_legacy(_app):
        events.append("legacy-up")
        yield
        events.append("legacy-down")

    monkeypatch.setattr(servicio.app.router, "lifespan_context", observed_legacy)
    app = R.build_app(config(tmp_path))
    journal = app.state.native_journal
    runner = app.state.projector_runner
    monkeypatch.setattr(journal, "initialize", lambda: events.append("initialize"))
    monkeypatch.setattr(
        G, "configure_journal_from_v8",
        lambda *_args, **_kwargs: events.append("configure"),
    )
    monkeypatch.setattr(journal, "dispose", lambda: events.append("dispose"))
    monkeypatch.setattr(runner, "start", lambda: events.append("runner-start"))
    monkeypatch.setattr(runner, "stop", lambda: events.append("runner-stop"))

    with TestClient(app) as client:
        assert client.get("/live").status_code == 200
        assert events == [
            "initialize", "configure", "runner-start", "legacy-up",
        ]
    assert events == [
        "initialize", "configure", "runner-start", "legacy-up", "legacy-down",
        "runner-stop", "dispose",
    ]


def test_active_abre_sesion_hebra_y_revoca_al_cerrar(tmp_path, monkeypatch):
    lightweight_legacy(monkeypatch)
    app = R.build_app(active_config(tmp_path))
    runner = app.state.projector_runner
    assert isinstance(runner, PR.ActiveProjectorRunner)
    with TestClient(app) as client:
        deadline = time.monotonic() + 2
        snapshot = runner.snapshot()
        while snapshot.state is PR.RunnerState.STARTING and time.monotonic() < deadline:
            time.sleep(0.01)
            snapshot = runner.snapshot()
        assert snapshot.state in {PR.RunnerState.IDLE, PR.RunnerState.PROJECTING}
        assert snapshot.thread_alive is True
        assert snapshot.session_open is True
        assert client.get("/ready").status_code == 503
        opened = client.post(
            "/native/v1/sessions",
            headers={"Authorization":
                     "Bearer operator-credential-with-enough-entropy"},
        )
        assert opened.status_code == 201
        token = opened.json()["token"]
        transition = client.post(
            "/native/v1/operator/admission/transition",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "target": "open",
                "expected_epochs": {verb: 0 for verb in C.ADMISSION_VERBS},
                "reason_code": "ROLLOUT",
            },
        )
        assert transition.status_code == 200, transition.text
        assert client.get("/ready").status_code == 200
        closed = client.post(
            "/native/v1/operator/admission/transition",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "target": "closed",
                "expected_epochs": {verb: 1 for verb in C.ADMISSION_VERBS},
                "reason_code": "MAINTENANCE",
            },
        )
        assert closed.status_code == 200
        assert client.get("/ready").status_code == 503
    stopped = runner.snapshot()
    assert stopped.state is PR.RunnerState.STOPPED
    assert stopped.session_open is False


@pytest.mark.parametrize("mode", ("typo", "bridge", "", True, None))
def test_build_rechaza_modo_projector_fuera_del_vocabulario(tmp_path, mode):
    with pytest.raises(R.RuntimeConfigurationError):
        R.build_app(config(tmp_path, projector_mode=mode))


def test_ready_y_health_fallan_cerrado_si_snapshot_rompe(tmp_path, monkeypatch):
    lightweight_legacy(monkeypatch)
    monkeypatch.setattr(servicio, "health", lambda: {"ok": True})
    app = R.build_app(active_config(tmp_path))
    with TestClient(app) as client:
        monkeypatch.setattr(
            app.state.projector_runner, "snapshot",
            lambda: (_ for _ in ()).throw(RuntimeError("detalle-secreto")),
        )
        ready = client.get("/ready")
        health = client.get("/health")
    assert ready.status_code == 503
    assert ready.json() == {"name": "coordination", "state": "not_ready"}
    assert health.status_code == 200
    assert health.json()["ok"] is False
    assert "detalle-secreto" not in health.text


def test_lifecycle_del_projector_no_oculta_fallo_del_cuerpo_y_del_stop():
    class BrokenRunner:
        def start(self):
            return None

        def stop(self):
            raise RuntimeError("runner stop")

    with pytest.raises(BaseExceptionGroup) as caught:
        with R._running_projector(BrokenRunner()):
            raise ValueError("runtime body")
    assert {type(item) for item in caught.value.exceptions} == {
        ValueError, RuntimeError,
    }


def test_lifecycle_del_projector_limpia_un_arranque_parcial():
    events = []

    class PartialRunner:
        def start(self):
            events.append("start")
            raise ValueError("runner start")

        def stop(self):
            events.append("stop")

    with pytest.raises(ValueError, match="runner start"):
        with R._running_projector(PartialRunner()):
            raise AssertionError("cuerpo inalcanzable")
    assert events == ["start", "stop"]


def test_lifecycle_preserva_fallo_de_arranque_y_de_limpieza_parcial():
    class BrokenStartRunner:
        def start(self):
            raise ValueError("runner start")

        def stop(self):
            raise RuntimeError("runner stop")

    with pytest.raises(BaseExceptionGroup) as caught:
        with R._running_projector(BrokenStartRunner()):
            raise AssertionError("cuerpo inalcanzable")
    assert {type(item) for item in caught.value.exceptions} == {
        ValueError, RuntimeError,
    }


def test_lifespan_preserva_fallo_de_stop_y_de_dispose(tmp_path, monkeypatch):
    lightweight_legacy(monkeypatch)
    app = R.build_app(config(tmp_path))
    runner = app.state.projector_runner
    journal = app.state.native_journal
    real_dispose = journal.dispose

    def broken_stop():
        raise ValueError("runner stop")

    def broken_dispose():
        real_dispose()
        raise RuntimeError("journal dispose")

    monkeypatch.setattr(runner, "stop", broken_stop)
    monkeypatch.setattr(journal, "dispose", broken_dispose)
    with pytest.raises(BaseExceptionGroup) as caught:
        with TestClient(app):
            pass
    assert [str(item) for item in caught.value.exceptions] == [
        "runner stop", "journal dispose",
    ]


def test_build_congela_identidad_y_capacidades_antes_del_lifespan(tmp_path, monkeypatch):
    mutable_map = {
        "credential-a": {
            "rol": "security", "carril": "llminbox",
            "principal_id": "security-workload",
        },
    }
    mutable_grants = {"security-workload": [G.CAP_EVENT_WRITER]}

    @R.asynccontextmanager
    async def lightweight_legacy(_app):
        yield

    monkeypatch.setattr(servicio.app.router, "lifespan_context", lightweight_legacy)
    monkeypatch.setattr(servicio, "tipo_de_destinatario", lambda _value: "agente")
    monkeypatch.setattr(R.lp, "canon_identidad", lambda value: value)
    monkeypatch.setattr(R.lp, "rol_de", lambda value: value)
    app = R.build_app(config(
        tmp_path, credential_map=mutable_map, capability_grants=mutable_grants))
    mutable_map["credential-a"]["rol"] = "qa"
    mutable_grants["security-workload"].clear()

    with TestClient(app) as client:
        abre_barrera(app)
        opened = client.post(
            "/native/v1/sessions",
            headers={"Authorization": "Bearer credential-a"})
        assert opened.status_code == 201
        assert opened.json()["role"] == "security"
        token = opened.json()["token"]
        event = client.post(
            "/native/v1/events",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "k"},
            json={
                "type": "message", "verb": "inform", "to": ["security"],
                "kind": "DELIVERED", "head": "h", "body": "b",
            },
        )
        assert event.status_code == 202


def test_lifespan_preserva_el_fallo_de_arranque_y_el_de_dispose(tmp_path, monkeypatch):
    previous_pool = servicio.OBSERVABILIDAD

    @R.asynccontextmanager
    async def broken_legacy(_app):
        raise ValueError("legacy startup")
        yield

    monkeypatch.setattr(servicio.app.router, "lifespan_context", broken_legacy)
    app = R.build_app(config(tmp_path))
    real_dispose = app.state.native_journal.dispose

    def broken_dispose():
        real_dispose()
        raise RuntimeError("journal dispose")

    monkeypatch.setattr(app.state.native_journal, "dispose", broken_dispose)
    with pytest.raises(BaseExceptionGroup) as caught:
        with TestClient(app):
            pass
    assert {type(item) for item in caught.value.exceptions} == {ValueError, RuntimeError}
    assert servicio.OBSERVABILIDAD is previous_pool


def test_rol_no_canonico_puede_acusar_como_su_rol_canonico(tmp_path, monkeypatch):
    monkeypatch.setattr(R.lp, "canon_identidad", lambda value: str(value).lower())
    monkeypatch.setattr(R.lp, "rol_de", lambda value: value)
    monkeypatch.setattr(
        servicio, "tipo_de_destinatario",
        lambda value: "agente" if str(value).lower() == "security" else "desconocido")

    @R.asynccontextmanager
    async def lightweight_legacy(_app):
        yield

    monkeypatch.setattr(servicio.app.router, "lifespan_context", lightweight_legacy)
    raw = json.dumps({
        "author": {
            "principal": "author", "rol": "BE", "carril": "llminbox",
            "capacidades": ["session", "events.write", "outbox.project", "events.index"],
        },
        "recipient": {
            "principal": "recipient", "rol": "SECURITY", "carril": "llminbox",
            "capacidades": ["session", "events.ack"],
        },
    }).encode()
    credential_map, grants = R._native_v8_configuration(raw)
    app = R.build_app(config(
        tmp_path, credential_map=credential_map, capability_grants=grants))

    with TestClient(app) as client:
        abre_barrera(app)
        author = client.post(
            "/native/v1/sessions", headers={"Authorization": "Bearer author"}
        ).json()["token"]
        recipient_response = client.post(
            "/native/v1/sessions", headers={"Authorization": "Bearer recipient"})
        assert recipient_response.json()["role"] == "security"
        recipient = recipient_response.json()["token"]
        accepted = client.post(
            "/native/v1/events",
            headers={"Authorization": f"Bearer {author}", "Idempotency-Key": "role-case"},
            json={
                "type": "message", "verb": "inform", "to": ["security"],
                "kind": "DELIVERED", "head": "h", "body": "b",
            },
        ).json()
        claim = client.post(
            "/native/v1/outbox/claims", headers={"Authorization": f"Bearer {author}"},
            json={"lease_s": 60},
        ).json()["job"]
        materialized = client.post(
            f"/native/v1/outbox/{accepted['event_id']}/materialized",
            headers={"Authorization": f"Bearer {author}"},
            json={"entry_eid": "e" * 64, "ledger": "llminbox",
                  "claim_token": claim["claim_token"], "byte_off": 0},
        )
        assert materialized.status_code == 204
        indexed = client.post(
            f"/native/v1/events/{accepted['event_id']}/indexed",
            headers={"Authorization": f"Bearer {author}"}, json={"index_ref": "idx:1"})
        assert indexed.status_code == 204
        ack = client.post(
            f"/native/v1/events/{accepted['event_id']}/acks",
            headers={"Authorization": f"Bearer {recipient}"}, json={"ack_ref": "seen:1"})
        assert ack.status_code == 200 and ack.json()["state"] == "delivered"
