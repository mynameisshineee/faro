"""Falsificadores del router de Pod C — montado en el MISMO runtime root que
el gateway nativo, sobre un único Journal configurado por UNA sola recarga
autoritativa (``native_gateway.configure_journal_from_v8``). No hay Journal ni
app sidecar: las sesiones se abren en ``POST /native/v1/sessions`` del
gateway principal, exactamente como cualquier otro workload.
"""
from __future__ import annotations

import os
import stat as statmod
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import coordination as C
import native_gateway as G
import operator_admission as OP
import projector_runner as PR


LANE = "llminbox"
VERBS = tuple(C.ADMISSION_VERBS)

CREDS = {
    "cred-runtime-high-entropy": {
        "rol": "be", "carril": LANE, "principal_id": "runtime"},
    "cred-operator-high-entropy": {
        "rol": "infra", "carril": LANE, "principal_id": "admision"},
    # Capacidad EXTRA además de `admission_operator`: prueba que este router
    # exige el conjunto EXACTO, no mera pertenencia.
    "cred-operator-extra-high-entropy": {
        "rol": "infra", "carril": LANE, "principal_id": "admision-extra"},
}
GRANTS = {
    "runtime": (C.CAP_OUTBOX_WORKER,),
    "admision": (C.CAP_ADMISSION_OPERATOR,),
    "admision-extra": (C.CAP_ADMISSION_OPERATOR, C.CAP_OUTBOX_WORKER),
}


class _FakeRunner:
    """Doble de ``ProjectorRunnerLike`` con estado y carril inyectables.

    El snapshot es un ``SimpleNamespace`` ESTRUCTURAL, no un
    ``projector_runner.RunnerSnapshot`` real: esta rama no trae un runner
    "hardened" (con `session_open`) — el único que existe hoy es el Null
    Object `disabled`. Mezclar ese dataclass real con un campo que no
    declara sería cherry-pickear el runner; todas las variantes "sano" de
    este fichero salen del MISMO molde de abajo, con o sin `session_open`.
    """

    def __init__(self, snapshot, *, lane: str):
        self._snapshot = snapshot
        self.lane = lane

    def start(self) -> None:
        return None

    def snapshot(self):
        return self._snapshot

    def drain(self, *, timeout_s: float):
        return self._snapshot

    def stop(self) -> None:
        return None

    def certify_rollback(self):
        raise PR.RollbackNotCertifiable("doble de pruebas: sin certificar")


def _snapshot_stub(*, session_open=None):
    """Molde único para los snapshots "sano" de este fichero. `session_open`
    se omite (falsador de ausencia) cuando no se pasa."""
    campos = dict(state=PR.RunnerState.IDLE, required=True, thread_alive=True,
                 accepting_claims=True, fatal_code=None)
    if session_open is not None:
        campos["session_open"] = session_open
    return SimpleNamespace(**campos)


# Sano en todo lo demás pero SIN `session_open`: falsador de ausencia — ver
# `test_abrir_con_runner_sano_sin_declarar_session_open_da_503`.
_HEALTHY = _snapshot_stub()
_HEALTHY_CON_SESSION_ABIERTA = _snapshot_stub(session_open=True)


@pytest.fixture
def harness(tmp_path):
    """Un solo Journal, configurado UNA vez con el mapa V8 completo — el
    operador entra en la MISMA recarga que el resto de principals, nunca por
    `bind_credential` suelto."""
    journal = C.Journal(str(tmp_path / "coordination.sqlite"),
                        pepper=b"operator-admission-test-pepper")
    journal.initialize()
    G.configure_journal_from_v8(journal, CREDS, GRANTS)
    yield journal
    journal.dispose()


def _app(journal: C.Journal, runner: PR.ProjectorRunnerLike):
    """El runtime root real monta los dos routers sobre el MISMO Journal —
    esto reproduce justo eso, sin un segundo proceso ni un segundo Journal."""
    app = G.create_native_app(journal)
    app.include_router(OP.create_operator_admission_router(journal, runner=runner))
    return app


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _open(client: TestClient, credential: str) -> str:
    response = client.post(G.API_PREFIX + "/sessions", headers=_auth(credential),
                           json={"ttl_s": 300})
    assert response.status_code == 201, response.text
    return response.json()["token"]


# ══ capacidad EXACTA, integrada en el ÚNICO mapa autoritativo ═══════════════
def test_el_mapa_v8_concede_solo_admission_operator(harness):
    with TestClient(_app(harness, PR.DisabledProjectorRunner())) as client:
        token = _open(client, "cred-operator-high-entropy")
        who = client.get(G.API_PREFIX + "/whoami", headers=_auth(token))
        assert who.status_code == 200, who.text
        assert who.json()["capabilities"] == [C.CAP_ADMISSION_OPERATOR]


def test_una_capacidad_extra_ademas_de_admission_operator_se_rechaza(harness):
    """⊖ `admission_operator` SÍ está presente — el Journal la dejaría pasar
    (mera pertenencia). El precheck de este router exige el conjunto EXACTO,
    así que una credencial con una capacidad de más tiene que seguir fuera —
    y el rechazo tiene que dejar recibo, igual que cualquier otra negativa de
    política en las rutas nativas."""
    with TestClient(_app(harness, _FakeRunner(_HEALTHY, lane=LANE))) as client:
        token = _open(client, "cred-operator-extra-high-entropy")
        response = client.get(OP.PREFIX + "/admission", headers=_auth(token))
        assert response.status_code == 403, response.text
        payload = response.json()
        assert payload["code"] == "POLICY_DENIED"
        assert payload.get("receipt_id"), "el rechazo tiene que dejar recibo"


def test_una_sesion_sin_la_capacidad_no_puede_leer_la_barrera(harness):
    with TestClient(_app(harness, _FakeRunner(_HEALTHY, lane=LANE))) as client:
        runtime_token = _open(client, "cred-runtime-high-entropy")
        denied = client.get(OP.PREFIX + "/admission", headers=_auth(runtime_token))
        assert denied.status_code == 403, denied.text


# ══ default closed / bootstrap explícito, wrapper {"admissions": [...]} ═════
def test_sin_transicion_previa_la_barrera_nace_closed_epoch_cero(harness):
    with TestClient(_app(harness, _FakeRunner(_HEALTHY, lane=LANE))) as client:
        token = _open(client, "cred-operator-high-entropy")
        response = client.get(OP.PREFIX + "/admission", headers=_auth(token))
        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {"admissions"}
        states = {row["verb"]: row for row in body["admissions"]}
        assert set(states) == set(VERBS)
        for row in states.values():
            assert (row["state"], row["epoch"]) == ("closed", 0)
            assert row["lane"] == LANE


# ══ rollback: foto y certificado son estrechos, atómicos y del carril ═══════
def test_rollback_status_expone_wire_cerrado_y_no_certifica_closed(harness):
    with TestClient(_app(harness, PR.DisabledProjectorRunner())) as client:
        token = _open(client, "cred-operator-high-entropy")
        response = client.get(OP.PREFIX + "/rollback/status",
                              headers=_auth(token))
        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {
            "lane", "admissions", "outbox", "durable_v", "certifiable"}
        assert body["lane"] == LANE
        assert body["certifiable"] is False
        assert type(body["durable_v"]) is int
        assert body["outbox"] == {
            "lane": LANE, "pending": 0, "failed": 0, "unresolved": 0}
        assert len(body["admissions"]) == 2
        assert all(set(row) == {"lane", "verb", "state", "epoch"}
                   for row in body["admissions"])


def test_rollback_certify_rechaza_hasta_que_ambas_puertas_estan_sealed(harness):
    with TestClient(_app(harness, PR.DisabledProjectorRunner())) as client:
        token = _open(client, "cred-operator-high-entropy")
        denied = client.post(OP.PREFIX + "/rollback/certify",
                             headers=_auth(token))
        assert denied.status_code == 409, denied.text
        assert denied.json()["code"] == "ADMISSION_CONFLICT"

        sealed = client.post(
            OP.PREFIX + "/admission/transition", headers=_auth(token),
            json={"target": "sealed", "expected_epochs": {v: 0 for v in VERBS},
                  "reason_code": "DRAIN_FOR_ROLLBACK"},
        )
        assert sealed.status_code == 200, sealed.text
        certified = client.post(OP.PREFIX + "/rollback/certify",
                                headers=_auth(token))
        assert certified.status_code == 200, certified.text
        body = certified.json()
        assert set(body) == {
            "lane", "admissions", "outbox", "durable_v", "certified_at"}
        assert body["lane"] == LANE
        assert body["outbox"] == {
            "lane": LANE, "pending": 0, "failed": 0, "unresolved": 0}
        assert type(body["durable_v"]) is int
        assert isinstance(body["certified_at"], str) and body["certified_at"]
        assert {(row["verb"], row["state"], row["epoch"])
                for row in body["admissions"]} == {
                    (verb, "sealed", 1) for verb in VERBS}


@pytest.mark.parametrize("path,method", [
    ("/rollback/status", "get"), ("/rollback/certify", "post")])
def test_rollback_exige_capacidad_exacta_en_ambos_endpoints(
        harness, path, method):
    with TestClient(_app(harness, PR.DisabledProjectorRunner())) as client:
        token = _open(client, "cred-operator-extra-high-entropy")
        response = getattr(client, method)(OP.PREFIX + path,
                                           headers=_auth(token))
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "POLICY_DENIED"


# ══ open exige runner sano Y del mismo carril ════════════════════════════════
def test_abrir_con_runner_disabled_da_503_y_no_muta_nada(harness):
    with TestClient(_app(harness, PR.DisabledProjectorRunner())) as client:
        token = _open(client, "cred-operator-high-entropy")
        body = {"target": "open", "expected_epochs": {v: 0 for v in VERBS},
               "reason_code": "ROLLOUT"}
        response = client.post(OP.PREFIX + "/admission/transition",
                               headers=_auth(token), json=body)
        assert response.status_code == 503, response.text
        assert response.json()["code"] == "PROJECTOR_NOT_READY"
        still = client.get(OP.PREFIX + "/admission", headers=_auth(token))
        for row in still.json()["admissions"]:
            assert (row["state"], row["epoch"]) == ("closed", 0)


def test_abrir_con_runner_sano_de_otro_carril_da_503(harness):
    """⊖ el runner declara `session_open=True` (para que el ÚNICO motivo de
    bloqueo posible sea el carril) pero declara OTRO carril: el gate tiene
    que seguir bloqueando."""
    runner = _FakeRunner(_HEALTHY_CON_SESSION_ABIERTA, lane="otro-carril")
    with TestClient(_app(harness, runner)) as client:
        token = _open(client, "cred-operator-high-entropy")
        body = {"target": "open", "expected_epochs": {v: 0 for v in VERBS},
               "reason_code": "ROLLOUT"}
        response = client.post(OP.PREFIX + "/admission/transition",
                               headers=_auth(token), json=body)
        assert response.status_code == 503, response.text
        assert response.json()["code"] == "PROJECTOR_NOT_READY"


def test_abrir_con_runner_sano_sin_declarar_session_open_da_503(harness):
    """⊖ FAIL-CLOSED: `_HEALTHY` es sano en todo lo demás (mismo molde que el
    ⊕ de más abajo) y del carril correcto, pero NO declara `session_open` —
    igual que el único runner real de esta rama (`disabled`, que tampoco lo
    declara). La ausencia del campo NUNCA se lee como "no aplica": bloquea
    igual que si fuera `False`."""
    with TestClient(_app(harness, _FakeRunner(_HEALTHY, lane=LANE))) as client:
        token = _open(client, "cred-operator-high-entropy")
        body = {"target": "open", "expected_epochs": {v: 0 for v in VERBS},
               "reason_code": "ROLLOUT"}
        response = client.post(OP.PREFIX + "/admission/transition",
                               headers=_auth(token), json=body)
        assert response.status_code == 503, response.text
        assert response.json()["code"] == "PROJECTOR_NOT_READY"


class _BrokenRunner:
    """Runner cuyo propio ``snapshot()`` levanta — no "no sano", ROTO."""

    lane = LANE

    def start(self) -> None:
        return None

    def snapshot(self):
        raise RuntimeError("el runner no puede ni contestar su estado")

    def drain(self, *, timeout_s: float):
        raise RuntimeError("el runner no puede ni contestar su estado")

    def stop(self) -> None:
        return None

    def certify_rollback(self):
        raise RuntimeError("el runner no puede ni contestar su estado")


def test_abrir_con_snapshot_que_levanta_da_503_no_500(harness):
    """⊖ un ``runner.snapshot()`` roto no debe escapar como 500: se trata
    como "no listo", igual que deshabilitado."""
    with TestClient(_app(harness, _BrokenRunner())) as client:
        token = _open(client, "cred-operator-high-entropy")
        body = {"target": "open", "expected_epochs": {v: 0 for v in VERBS},
               "reason_code": "ROLLOUT"}
        response = client.post(OP.PREFIX + "/admission/transition",
                               headers=_auth(token), json=body)
        assert response.status_code == 503, response.text
        assert response.json()["code"] == "PROJECTOR_NOT_READY"


def test_abrir_con_runner_sano_del_mismo_carril_si_transiciona(harness):
    """⊕ control: sin esto, "bloquea siempre" y "exige sano+mismo
    carril+session_open" son indistinguibles."""
    runner = _FakeRunner(_HEALTHY_CON_SESSION_ABIERTA, lane=LANE)
    with TestClient(_app(harness, runner)) as client:
        token = _open(client, "cred-operator-high-entropy")
        body = {"target": "open", "expected_epochs": {v: 0 for v in VERBS},
               "reason_code": "ROLLOUT"}
        response = client.post(OP.PREFIX + "/admission/transition",
                               headers=_auth(token), json=body)
        assert response.status_code == 200, response.text
        for row in response.json()["admissions"]:
            assert (row["state"], row["epoch"]) == ("open", 1)


# ══ close/seal: NUNCA pasan por el gate del runner ══════════════════════════
def test_cerrar_y_sellar_funcionan_con_runner_disabled_y_otro_carril(harness):
    # Se fuerza `open` por Journal directo — simula que la barrera ya estaba
    # abierta de antes de que este proceso (o su runner) existiera.
    token_directo = harness.open_session("cred-operator-high-entropy")
    for verbo in VERBS:
        harness.open_admission(token_directo.token, verbo, reason_code="ROLLOUT")

    # Runner deshabilitado Y de otro carril a la vez: si `close`/`sealed`
    # pasaran por CUALQUIER mitad del gate de `open`, esto los bloquearía.
    with TestClient(_app(harness, PR.DisabledProjectorRunner())) as client:
        token = _open(client, "cred-operator-high-entropy")

        close_body = {"target": "closed", "expected_epochs": {v: 1 for v in VERBS},
                     "reason_code": "MAINTENANCE"}
        closed = client.post(OP.PREFIX + "/admission/transition",
                             headers=_auth(token), json=close_body)
        assert closed.status_code == 200, closed.text
        for row in closed.json()["admissions"]:
            assert (row["state"], row["epoch"]) == ("closed", 2)

        seal_body = {"target": "sealed", "expected_epochs": {v: 2 for v in VERBS},
                    "reason_code": "MAINTENANCE"}
        sealed = client.post(OP.PREFIX + "/admission/transition",
                             headers=_auth(token), json=seal_body)
        assert sealed.status_code == 200, sealed.text
        for row in sealed.json()["admissions"]:
            assert (row["state"], row["epoch"]) == ("sealed", 3)


# ══ lane de sesión: nunca un parámetro del llamante, y con recibo ═══════════
def test_el_cuerpo_no_acepta_lane_ni_atribucion_y_deja_recibo(harness):
    with TestClient(_app(harness, _FakeRunner(_HEALTHY, lane=LANE))) as client:
        token = _open(client, "cred-operator-high-entropy")
        body = {"target": "open", "expected_epochs": {v: 0 for v in VERBS},
               "reason_code": "ROLLOUT", "lane": "otro-carril"}
        response = client.post(OP.PREFIX + "/admission/transition",
                               headers=_auth(token), json=body)
        # `lane` es un alias de atribución (`ATTRIBUTION_ALIASES`): se rechaza
        # con el código específico, auditado — NO con el 422 genérico que
        # `StrictDTO(extra="forbid")` daría por su cuenta.
        assert response.status_code == 400, response.text
        payload = response.json()
        assert payload["code"] == "ATTRIBUTION_REJECTED"
        assert payload.get("receipt_id"), "el rechazo tiene que dejar recibo"


def test_una_clave_desconocida_sin_atribucion_sigue_dando_422(harness):
    """⊕ control: sin esto, "toda clave de más da 400" y "sólo la atribución
    da 400, el resto sigue en 422" serían indistinguibles."""
    with TestClient(_app(harness, _FakeRunner(_HEALTHY, lane=LANE))) as client:
        token = _open(client, "cred-operator-high-entropy")
        body = {"target": "open", "expected_epochs": {v: 0 for v in VERBS},
               "reason_code": "ROLLOUT", "campo_que_no_existe": 1}
        response = client.post(OP.PREFIX + "/admission/transition",
                               headers=_auth(token), json=body)
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "INVALID_BODY"


# ══ credencial 0600 del uid efectivo (NO root) ══════════════════════════════
def test_credencial_con_permisos_de_grupo_u_otros_se_rechaza(tmp_path):
    path = tmp_path / "operator.cred"
    path.write_text("s" * 40)
    os.chmod(path, 0o640)
    with pytest.raises(OP.OperatorCredentialError):
        OP.load_operator_credential(str(path))


def test_credencial_0600_pero_de_otro_uid_se_rechaza(tmp_path, monkeypatch):
    """El proceso NO corre como root: se fabrica un `st_uid` que NO es el
    efectivo (en vez de exigir uid 0) para probar la comprobación real."""
    path = tmp_path / "operator.cred"
    path.write_text("s" * 40)
    os.chmod(path, 0o600)
    real_fstat = os.fstat
    ajeno = os.geteuid() + 1

    def fake_fstat(fd):
        st = real_fstat(fd)
        seq = (statmod.S_IFREG | 0o600, st.st_ino, st.st_dev, st.st_nlink,
              ajeno, st.st_gid, st.st_size, 0, 0, 0)
        extra = {"st_atime_ns": st.st_atime_ns, "st_mtime_ns": st.st_mtime_ns,
                 "st_ctime_ns": st.st_ctime_ns}
        return os.stat_result(seq, extra)

    monkeypatch.setattr(OP.os, "fstat", fake_fstat)
    with pytest.raises(OP.OperatorCredentialError, match="uid efectivo"):
        OP.load_operator_credential(str(path))


def test_credencial_0600_del_uid_efectivo_se_acepta(tmp_path):
    """⊕ control positivo: el caso normal (propietario = uid efectivo, modo
    0600) tiene que ACEPTAR — sin esto, "rechaza siempre" pasaría igual."""
    path = tmp_path / "operator.cred"
    secret = "s" * 40
    path.write_text(secret)
    os.chmod(path, 0o600)
    assert OP.load_operator_credential(str(path)) == secret.encode()


def test_credencial_demasiado_corta_se_rechaza(tmp_path):
    path = tmp_path / "operator.cred"
    path.write_text("corta")
    os.chmod(path, 0o600)
    with pytest.raises(OP.OperatorCredentialError):
        OP.load_operator_credential(str(path))


@pytest.mark.parametrize("bandera", ["O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC"])
def test_plataforma_sin_una_bandera_de_apertura_rechaza_la_credencial(
    tmp_path, monkeypatch, bandera,
):
    """Ninguna de las tres se degrada a "abrir sin ella": si falta CUALQUIERA,
    la credencial no se sirve."""
    path = tmp_path / "operator.cred"
    path.write_text("s" * 40)
    os.chmod(path, 0o600)
    monkeypatch.delattr(OP.os, bandera, raising=True)
    with pytest.raises(OP.OperatorCredentialError, match=bandera):
        OP.load_operator_credential(str(path))


def test_credencial_con_espacio_al_borde_se_rechaza_sin_normalizar(tmp_path):
    path = tmp_path / "operator.cred"
    path.write_text("s" * 40 + "\n")
    os.chmod(path, 0o600)
    with pytest.raises(OP.OperatorCredentialError):
        OP.load_operator_credential(str(path))


def test_credencial_multilinea_se_rechaza(tmp_path):
    path = tmp_path / "operator.cred"
    # Sin espacio al borde (empieza y acaba en "s"): esto ejercita el chequeo
    # de multilinea, NO el de espacio en los extremos.
    path.write_text("s" * 20 + "\n" + "s" * 20)
    os.chmod(path, 0o600)
    with pytest.raises(OP.OperatorCredentialError):
        OP.load_operator_credential(str(path))
