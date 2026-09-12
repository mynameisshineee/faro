"""Composition root del runtime de llminbox.

Este modulo es una hoja: ensambla el servicio historico y el gateway nativo,
pero ninguno de ellos lo importa. Construir la app no abre ni migra el Journal;
esas operaciones viven en el lifespan para que el arranque falle antes de
servir y el cierre libere tambien las copias de preflight.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

import coordination as C
import kind_registry as kr
import ledger_parse as lp
import native_gateway as G
import operator_admission as OP
import projector as P
import projector_runner as PR
import journal_projection_backend as JPB
import servicio
from fleet_deadline_runner import FleetDeadlineRunner
from telemetry_bridge import SensorPool


PEPPER_MIN_BYTES = 32
PEPPER_MAX_BYTES = 4096
V8_MAP_MAX_BYTES = 1 << 20
LEGACY_MUTATION_POLICIES = frozenset({"bridge", "native-required"})
LEGACY_MUTATION_POLICY_ENV = "LLMINBOX_LEGACY_MUTATION_POLICY"


class RuntimeConfigurationError(ValueError):
    """La configuracion no permite construir una frontera de autoridad."""


def _legacy_mutation_policy(value: Any) -> str:
    if not isinstance(value, str) or value not in LEGACY_MUTATION_POLICIES:
        raise RuntimeConfigurationError(
            "la politica de mutaciones legacy debe ser bridge o native-required")
    return value


class LegacyMutationPolicyMiddleware:
    """Inyecta política root-owned en el scope sin mutar el singleton legacy."""

    def __init__(self, app, *, policy: str):
        self.app = app
        self.policy = _legacy_mutation_policy(policy)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope)
            state = dict(scope.get("state") or {})
            state[servicio.LEGACY_MUTATION_POLICY_STATE] = self.policy
            scope["state"] = state
        await self.app(scope, receive, send)


# La configuracion del piloto usa capacidades de producto, mientras que el
# gateway exige permisos de operacion. Esta es la unica traduccion y es cerrada:
# un nombre nuevo aborta el arranque en vez de degradarse a cero permisos.
PILOT_CAPABILITY_GRANTS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "session": (),
    "events.write": (G.CAP_EVENT_WRITER,),
    "events.ack": (G.CAP_DELIVERY_ACK,),
    "events.index": (C.CAP_INDEXER,),
    "leases": (G.CAP_LEASE_HOLDER,),
    "commands.submit": (G.CAP_COMMAND_SUBMITTER,),
    "commands.advance": (C.CAP_COMMAND_WORKER,),
    "outbox.project": (C.CAP_OUTBOX_WORKER,),
    "outbox.operate": (C.CAP_OUTBOX_OPERATOR,),
    "admission.operate": (C.CAP_ADMISSION_OPERATOR,),
    "runtime.observe": (C.CAP_RUNTIME_OBSERVE,),
    "runtime.recover": (C.CAP_RUNTIME_RECOVER,),
    "runtime.read": (C.CAP_RUNTIME_READ,),
    "organization.read": (C.CAP_ORGANIZATION_READ,),
    "organization.activate": (C.CAP_ORGANIZATION_ACTIVATE,),
})


def _read_regular_file(
    path: str, *, minimum_bytes: int = 1, maximum_bytes: int = PEPPER_MAX_BYTES,
    exact_mode: int | None = None,
) -> bytes:
    """Lee un fichero regular sin seguir enlaces ni separar check de lectura."""
    if not path:
        raise RuntimeConfigurationError("falta la ruta del fichero requerido")
    required_flags = ("O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC")
    if any(not hasattr(os, name) for name in required_flags):
        raise RuntimeConfigurationError(
            "la plataforma no ofrece apertura segura de secretos")
    flags = os.O_RDONLY
    for name in required_flags:
        flags |= getattr(os, name)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RuntimeConfigurationError(
            "el fichero requerido no se puede abrir de forma segura"
        ) from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeConfigurationError("el fichero requerido no es regular")
        if metadata.st_uid != os.geteuid():
            raise RuntimeConfigurationError(
                "el fichero requerido no pertenece al uid efectivo")
        mode = stat.S_IMODE(metadata.st_mode)
        if exact_mode is not None and mode != exact_mode:
            raise RuntimeConfigurationError(
                f"el fichero requerido exige modo exacto {exact_mode:04o}")
        if mode & 0o077:
            raise RuntimeConfigurationError(
                "el fichero requerido es legible fuera de su propietario")
        if metadata.st_size > maximum_bytes:
            raise RuntimeConfigurationError(
                f"el fichero requerido supera el maximo de {maximum_bytes} bytes")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(fd, min(remaining, 64 * 1024))
            if not chunk:
                raise RuntimeConfigurationError("el fichero requerido se corto al leerlo")
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        final_metadata = os.fstat(fd)
        initial_identity = (
            metadata.st_dev, metadata.st_ino, metadata.st_size,
            metadata.st_mtime_ns, metadata.st_ctime_ns,
        )
        final_identity = (
            final_metadata.st_dev, final_metadata.st_ino, final_metadata.st_size,
            final_metadata.st_mtime_ns, final_metadata.st_ctime_ns,
        )
        if len(data) != metadata.st_size or final_identity != initial_identity:
            raise RuntimeConfigurationError("el fichero requerido cambio durante la lectura")
        if len(data) < minimum_bytes:
            raise RuntimeConfigurationError(
                f"el fichero requerido exige al menos {minimum_bytes} bytes"
            )
        return data
    finally:
        os.close(fd)


def _native_v8_configuration(
    raw: bytes,
) -> tuple[dict[str, dict[str, str]], dict[str, tuple[str, ...]]]:
    """Convierte el mapa atestado del piloto sin volver a leer su ruta."""
    if len(raw) > V8_MAP_MAX_BYTES:
        raise RuntimeConfigurationError("el mapa V8 atestado supera su limite")
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeConfigurationError("el mapa V8 atestado no es JSON UTF-8 valido") from exc
    if not isinstance(decoded, dict) or not decoded:
        raise RuntimeConfigurationError("el mapa V8 atestado debe ser un objeto no vacio")

    credential_map: dict[str, dict[str, str]] = {}
    grants: dict[str, tuple[str, ...]] = {}
    principals: set[str] = set()
    allowed = {"principal", "rol", "carril", "capacidades"}
    for position, (credential, spec) in enumerate(decoded.items(), start=1):
        if not isinstance(credential, str) or not credential:
            raise RuntimeConfigurationError(
                f"credencial V8 #{position}: clave no valida")
        if not isinstance(spec, dict) or set(spec) != allowed:
            raise RuntimeConfigurationError(
                f"credencial V8 #{position}: metadata fuera de contrato")
        principal = spec.get("principal")
        role = spec.get("rol")
        lane = spec.get("carril")
        capabilities = spec.get("capacidades")
        if not all(isinstance(value, str) and value.strip()
                   for value in (principal, role, lane)):
            raise RuntimeConfigurationError(
                f"credencial V8 #{position}: principal, rol y carril son obligatorios")
        principal = principal.strip()
        if principal in principals:
            raise RuntimeConfigurationError(
                "dos credenciales V8 declaran el mismo principal")
        principals.add(principal)
        if (not isinstance(capabilities, list)
                or any(not isinstance(item, str) for item in capabilities)):
            raise RuntimeConfigurationError(
                f"credencial V8 #{position}: capacidades no validas")
        unknown = set(capabilities) - set(PILOT_CAPABILITY_GRANTS)
        if unknown:
            raise RuntimeConfigurationError(
                f"credencial V8 #{position}: capacidad fuera de contrato")
        if "session" not in capabilities:
            raise RuntimeConfigurationError(
                f"credencial V8 #{position}: falta la capacidad session")
        canonical_role = _canonical_role(role)
        native_caps = {
            grant
            for capability in capabilities
            for grant in PILOT_CAPABILITY_GRANTS[capability]
        }
        credential_map[credential] = {
            "rol": canonical_role,
            "carril": lane.strip(),
            "principal_id": principal,
        }
        grants[principal] = tuple(sorted(native_caps))
    return credential_map, grants


def _recipient_resolver(literal: str) -> tuple[str | None, bool]:
    """Resuelve solo agentes y difusion; un humano no es un runtime que acuse."""
    kind = servicio.tipo_de_destinatario(literal)
    if kind == "difusion":
        return None, True
    if kind == "humano":
        return None, False
    if kind == "agente":
        canonical = lp.canon_identidad(literal)
        if canonical is not None:
            return lp.rol_de(canonical), False
    return None, False


def _canonical_role(role: str) -> str:
    canonical = lp.canon_identidad(role)
    if canonical is None:
        raise RuntimeConfigurationError("el rol V8 no resuelve en el censo")
    resolved = lp.rol_de(canonical)
    if not isinstance(resolved, str) or not resolved.strip():
        raise RuntimeConfigurationError("el rol V8 no tiene forma canonica")
    return resolved.strip()


@dataclass(frozen=True)
class RuntimeConfig:
    journal_path: str
    pepper: bytes
    lane_ledgers: Mapping[str, Sequence[str]]
    credential_map: Mapping[str, Mapping[str, Any]]
    capability_grants: Mapping[str, Sequence[str]]
    legacy_mutation_policy: str = "bridge"
    raw_body_max_bytes: int = G.RAW_BODY_MAX_BYTES
    raw_body_max_chunks: int = G.RAW_BODY_MAX_CHUNKS
    raw_body_max_empty_chunks: int = G.RAW_BODY_MAX_EMPTY_CHUNKS
    raw_body_read_timeout_s: float | None = G.RAW_BODY_READ_TIMEOUT_S
    projector_mode: str = "disabled"
    projector_lane: str | None = None
    projector_credential: str | None = None
    projector_frame_key: bytes | None = None
    projector_targets: tuple[P.LedgerTarget, ...] = ()
    fleet_deadline_principals: tuple[str, ...] = ()
    fleet_stale_after_s: int = 300
    fleet_deadline_interval_s: int = 5

    @classmethod
    def from_environment(cls) -> "RuntimeConfig":
        journal_path = os.environ.get("LLMINBOX_JOURNAL", "").strip()
        if not journal_path:
            raise RuntimeConfigurationError("falta LLMINBOX_JOURNAL")
        if len(servicio._BYTES_MAPA) != 1:  # mismos bytes que servicio atestigua
            raise RuntimeConfigurationError(
                "el runtime nativo exige exactamente un mapa V8 atestado")
        expected = os.environ.get("LLMINBOX_CREDENCIALES_SHA", "").strip()
        actual = hashlib.sha256(servicio._BYTES_MAPA[0]).hexdigest()
        if not expected or actual != expected:
            raise RuntimeConfigurationError("el mapa V8 cargado no coincide con su atestado")
        credential_map, grants = _native_v8_configuration(servicio._BYTES_MAPA[0])
        legacy_mutation_policy = _legacy_mutation_policy(
            os.environ.get(LEGACY_MUTATION_POLICY_ENV, "bridge"))
        pepper = _read_regular_file(
            os.environ.get("LLMINBOX_PEPPER_FILE", ""), minimum_bytes=1,
        ).strip()
        if len(pepper) < PEPPER_MIN_BYTES:
            raise RuntimeConfigurationError(
                f"el pepper exige al menos {PEPPER_MIN_BYTES} bytes utiles")
        lane_ledgers = {
            lane: (ledger,) for lane, ledger in servicio.CARRIL_LEDGER.items()
        }
        projector_mode = os.environ.get(
            "LLMINBOX_PROJECTOR_MODE", "disabled")
        if projector_mode not in {"disabled", "active"}:
            raise RuntimeConfigurationError(
                "LLMINBOX_PROJECTOR_MODE debe ser active o disabled")
        projector_lane = None
        projector_credential = None
        projector_frame_key = None
        projector_targets: tuple[P.LedgerTarget, ...] = ()
        if projector_mode == "active":
            principal = os.environ.get(
                "LLMINBOX_PROJECTOR_PRINCIPAL_ID", "")
            ledger = os.environ.get("LLMINBOX_PROJECTOR_LEDGER", "")
            if (not principal or principal != principal.strip()
                    or not ledger or ledger != ledger.strip()):
                raise RuntimeConfigurationError(
                    "projector active exige principal y ledger explícitos y exactos")
            matches = [
                (credential, spec)
                for credential, spec in credential_map.items()
                if spec.get("principal_id") == principal
            ]
            if len(matches) != 1:
                raise RuntimeConfigurationError(
                    "el principal del projector debe resolver una credencial exacta")
            projector_credential, spec = matches[0]
            projector_lane = spec.get("carril")
            if tuple(grants.get(principal, ())) != (C.CAP_OUTBOX_WORKER,):
                raise RuntimeConfigurationError(
                    "la credencial del projector exige privilegio exacto outbox_worker")
            if not lane_ledgers:
                lane_ledgers = {projector_lane: (ledger,)}
            if (projector_lane not in lane_ledgers
                    or tuple(lane_ledgers[projector_lane]) != (ledger,)):
                raise RuntimeConfigurationError(
                    "el ledger del projector no es el único permitido en su carril")
            projector_frame_key = _read_regular_file(
                os.environ.get("LLMINBOX_PROJECTOR_FRAME_KEY_FILE", ""),
                minimum_bytes=32, exact_mode=0o600,
            )
            path = servicio.LEDGERS.get(ledger)
            if not path:
                raise RuntimeConfigurationError(
                    "el ledger del projector no tiene path montado")
            projector_targets = (P.LedgerTarget(projector_lane, ledger, path),)
        elif not lane_ledgers:
            raise RuntimeConfigurationError(
                "el runtime nativo disabled exige un mapa carril→ledger no vacío")
        try:
            deadline_principals = json.loads(os.environ.get(
                "LLMINBOX_FLEET_DEADLINE_PRINCIPALS", "[]"))
            if not isinstance(deadline_principals, list):
                raise ValueError
            stale_after_s = int(os.environ.get("LLMINBOX_FLEET_STALE_AFTER_S", "300"))
            deadline_interval_s = int(os.environ.get(
                "LLMINBOX_FLEET_DEADLINE_INTERVAL_S", "5"))
        except (ValueError, TypeError) as exc:
            raise RuntimeConfigurationError("configuración de deadlines no válida") from exc
        return cls(
            journal_path, pepper, lane_ledgers, credential_map, grants,
            legacy_mutation_policy=legacy_mutation_policy,
            projector_mode=projector_mode,
            projector_lane=projector_lane,
            projector_credential=projector_credential,
            projector_frame_key=projector_frame_key,
            projector_targets=projector_targets,
            fleet_deadline_principals=tuple(deadline_principals),
            fleet_stale_after_s=stale_after_s,
            fleet_deadline_interval_s=deadline_interval_s,
        )


def _build_journal(config: RuntimeConfig, *, sensor_factory=None) -> C.Journal:
    return C.Journal(
        config.journal_path,
        pepper=config.pepper,
        lane_ledgers=config.lane_ledgers,
        grammar=C.Grammar(
            canonical_kind=lp.canonical_tipo,
            opens_entry=lambda line: bool(lp.H_ENTRY.match(line)),
            normalize_resource=servicio.tema_norm,
            canonical_agent_kind=kr.materialize_at,
        ),
        recipient_resolver=_recipient_resolver,
        sensor_factory=sensor_factory,
    )


def _coordination_ready(journal: C.Journal) -> bool:
    """Decide readiness internamente; el detalle nunca cruza el HTTP público."""
    try:
        measured = journal.health()
        if not isinstance(measured, Mapping):
            return False
        return (
            measured.get("initialized") is True
            and measured.get("writable") is True
            and measured.get("estado") == "conocida"
            and type(measured.get("durable_v_stored")) is int
            and type(measured.get("durable_v_code")) is int
            and measured["durable_v_stored"] == measured["durable_v_code"]
            and measured.get("schema_too_new") is False
            and measured.get("audit_suspended") is False
        )
    except Exception:
        return False


def _runtime_health_document() -> dict[str, Any]:
    """Compone el health del mismo root que atiende las mutaciones nativas."""
    try:
        legacy = servicio.health()
    except Exception:
        legacy = {"ok": False, "motivo": "LEGACY_HEALTH_UNAVAILABLE"}
    if not isinstance(legacy, dict):
        legacy = {"ok": False, "motivo": "LEGACY_HEALTH_INVALID"}
    else:
        legacy = dict(legacy)
        index_health = legacy.get("indice")
        if isinstance(index_health, dict):
            # El path interno del contenedor no aporta readiness y puede revelar
            # la topologia de montajes a un endpoint público.
            index_health = dict(index_health)
            index_health.pop("ruta", None)
            index_health.pop("path", None)
            legacy["indice"] = index_health

    # ADR-001 F-28: ni /health ni /ready pueden distinguir si falló path,
    # versión, pepper, volumen o esquema. Ese diagnóstico vive en sensores del
    # operador. Los adaptadores legacy M1_NO_INTEGRADO también se retiran porque
    # describen otro root y filtran una clasificación falsa.
    legacy.pop("journal", None)
    legacy.pop("politica", None)
    return legacy


@contextmanager
def _running_projector(runner: PR.ProjectorRunnerLike):
    """Posee el runner y conserva por separado fallos de trabajo y cierre."""
    try:
        runner.start()
    except BaseException as startup:
        try:
            runner.stop()
        except BaseException as cleanup:
            raise BaseExceptionGroup(
                "falló el arranque Y falló la limpieza del projector",
                [startup, cleanup],
            ) from None
        raise
    try:
        yield runner
    except BaseException as body:
        try:
            if hasattr(runner, "quiesce"):
                JPB.graceful_shutdown(runner, timeout_s=90.0)
            else:  # compatibilidad con dobles/implementaciones legacy
                runner.stop()
        except BaseException as cleanup:
            cleanup_errors = (
                list(cleanup.exceptions)
                if isinstance(cleanup, BaseExceptionGroup)
                else [cleanup]
            )
            raise BaseExceptionGroup(
                "falló el cuerpo Y falló la limpieza del projector",
                [body, *cleanup_errors],
            ) from None
        raise
    else:
        if hasattr(runner, "quiesce"):
            JPB.graceful_shutdown(runner, timeout_s=90.0)
        else:
            runner.stop()


def build_app(config: RuntimeConfig) -> FastAPI:
    """Ensambla sin abrir ni migrar; initialize/configure/dispose van en lifespan."""
    # Un solo pool por proceso servido mantiene el presupuesto de cardinalidad
    # compartido entre Core y legacy. Sin factory/exporter OTel queda, de forma
    # explícita, en ``not_configured``; los providers reales pertenecen al canario.
    observability_pool = SensorPool(enabled=True)
    legacy_mutation_policy = _legacy_mutation_policy(config.legacy_mutation_policy)
    journal = _build_journal(config, sensor_factory=observability_pool.for_session)
    if type(config.projector_mode) is not str:
        raise RuntimeConfigurationError("projector_mode debe ser texto exacto")
    if config.projector_mode == "disabled":
        projector_runner = PR.projector_runner_for_mode("disabled")
    elif config.projector_mode == "active":
        backend = JPB.projection_backend_for_mode(
            "active", journal=journal,
            credential=config.projector_credential,
            targets=config.projector_targets,
            frame_key=config.projector_frame_key,
        )
        projector_runner = PR.projector_runner_for_mode(
            "active", backend=backend, lane=config.projector_lane)
    else:
        raise RuntimeConfigurationError(
            "projector_mode debe ser active o disabled")
    legacy_app = servicio.app
    legacy_lifespan = legacy_app.router.lifespan_context
    # El lifespan puede empezar mucho despues de construir la app. Congelar aqui
    # impide que un dict del llamante cambie identidad o capacidades en esa ventana.
    credential_map = {
        credential: MappingProxyType(dict(spec))
        for credential, spec in config.credential_map.items()
    }
    capability_grants = {
        principal: tuple(capabilities)
        for principal, capabilities in config.capability_grants.items()
    }
    principals = config.fleet_deadline_principals
    if (type(principals) is not tuple or len(principals) > 32
            or any(type(p) is not str or not p or p != p.strip() for p in principals)
            or len(set(principals)) != len(principals)):
        raise RuntimeConfigurationError("principales de deadlines no válidos")
    deadline_credentials = []
    deadline_lanes = set()
    for principal in principals:
        matches = [(credential, spec) for credential, spec in credential_map.items()
                   if spec.get("principal_id") == principal]
        if len(matches) != 1:
            raise RuntimeConfigurationError("principal de deadlines sin credencial única")
        credential, spec = matches[0]
        lane = spec.get("carril")
        if (lane not in config.lane_ledgers or lane in deadline_lanes
                or capability_grants.get(principal) != (C.CAP_RUNTIME_OBSERVE,)):
            raise RuntimeConfigurationError(
                "deadlines exige un observer exclusivo con runtime.observe por carril")
        deadline_lanes.add(lane)
        deadline_credentials.append(credential)
    try:
        deadline_runner = FleetDeadlineRunner(
            journal, credentials=tuple(deadline_credentials),
            stale_after_s=config.fleet_stale_after_s,
            interval_s=config.fleet_deadline_interval_s)
    except ValueError as exc:
        raise RuntimeConfigurationError("configuración de deadlines fuera de rango") from exc

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # El context manager del Journal preserva AMBOS fallos si el cuerpo y
        # dispose fallan; un ``finally`` normal esconderia el fallo de arranque.
        previous_pool = servicio.OBSERVABILIDAD
        servicio.OBSERVABILIDAD = observability_pool
        try:
            with journal:
                journal.initialize()
                G.configure_journal_from_v8(
                    journal, credential_map, capability_grants)
                with _running_projector(projector_runner), deadline_runner:
                    async with legacy_lifespan(legacy_app):
                        yield
        finally:
            servicio.OBSERVABILIDAD = previous_pool

    app = FastAPI(
        title="llminbox coordination runtime",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/live")
    def runtime_live():
        # Si esta funcion contesta, el event loop y el proceso están vivos. No
        # abre discos ni consulta colas: liveness no es readiness.
        return JSONResponse(
            content={"ok": True, "state": "live"},
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/ready")
    def runtime_ready():
        coordination_ready = _coordination_ready(journal)
        try:
            ready = (
                config.projector_mode == "active"
                and deadline_runner.ready
                and journal.admission_ready(config.projector_lane)
                and JPB.ready_with_runner(
                    coordination_ready, projector_runner.snapshot())
            )
        except Exception:
            ready = False
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"name": "coordination", "state": "ready" if ready else "not_ready"},
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/health")
    def runtime_health():
        document = _runtime_health_document()
        try:
            journal_health = journal.health()
        except Exception:
            journal_health = None
        journal_shape_ok = isinstance(journal_health, Mapping)
        coordination_ready = _coordination_ready(journal)
        document["audit_suspended"] = (
            not journal_shape_ok
            or journal_health.get("audit_suspended") is not False
        )
        try:
            runner_ready = JPB.runner_counts_as_ready(
                projector_runner.snapshot())
            admission_ready = journal.admission_ready(config.projector_lane)
        except Exception:
            runner_ready = False
            admission_ready = False
        document["ok"] = bool(
            config.projector_mode == "active"
            and deadline_runner.ready
            and document.get("ok") is True
            and coordination_ready
            and runner_ready
            and admission_ready
            and not document["audit_suspended"]
        )
        # Compatibilidad: llmi.vivo() y la UI usan el status como conectividad y
        # el campo `ok` como salud. Reservar 503 para /ready conserva lecturas en
        # degradación y sigue dejando el fallo visible en el cuerpo.
        return JSONResponse(
            status_code=200,
            content=document,
            headers={"Cache-Control": "no-store"},
        )

    app.include_router(G.create_native_router(journal))
    app.include_router(OP.create_operator_admission_router(
        journal, runner=projector_runner))
    # El mount va al final: las rutas nativas deben resolverse antes del catch-all.
    app.mount("/", legacy_app)
    app.add_middleware(
        LegacyMutationPolicyMiddleware,
        policy=legacy_mutation_policy,
    )
    app.add_middleware(
        G.RawBodyLimitMiddleware,
        max_bytes=config.raw_body_max_bytes,
        max_chunks=config.raw_body_max_chunks,
        max_empty_chunks=config.raw_body_max_empty_chunks,
        read_timeout_s=config.raw_body_read_timeout_s,
    )
    app.state.native_journal = journal
    app.state.observability_pool = observability_pool
    app.state.projector_runner = projector_runner
    app.state.fleet_deadline_runner = deadline_runner
    return app


def create_app() -> FastAPI:
    """Factory para ``uvicorn runtime_root:create_app --factory``."""
    return build_app(RuntimeConfig.from_environment())
