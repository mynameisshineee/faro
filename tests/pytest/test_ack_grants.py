"""Falsadores focales del ACK causal: capacidad, scope y monotonicidad."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from .conftest import construir

H = {"X-Llminbox-Token": "test-token", "X-Llminbox-Carril": "demo"}
CRED = "cred-be-grants-000000000"


def _v8(tmp_path, monkeypatch):
    mapa = tmp_path / "ack-credentials.json"
    mapa.write_text(json.dumps({
        CRED: {"rol": "be", "carril": "demo", "principal_id": "workload-be"},
    }))
    s = construir(tmp_path, monkeypatch, extra_env={
        "LLMINBOX_CREDENCIALES": str(mapa),
        "LLMINBOX_CREDENCIALES_SHA": hashlib.sha256(mapa.read_bytes()).hexdigest(),
    })
    return s, {"X-Llminbox-Token": CRED, "X-Llminbox-Carril": "demo"}


def _grant(texto: str) -> dict:
    return json.loads([x.strip() for x in texto.splitlines()
                       if x.strip().startswith('{"hasta"')][-1])["ack"]


def _cursor(s, role="be", ledger="demo-ledger", carril="demo", *, legacy=False):
    # REKEY (sdet #1308, Clase B): los acks V8 escriben la clave NUEVA
    # (role, carril, ledger) en `cursors_v2` — la v1 legacy queda congelada.
    con = sqlite3.connect(s.DB)
    # Cada test elige UNA autoridad. Un fallback v2→v1 escondería que un ACK
    # V8 escribió accidentalmente la tabla legacy.
    if legacy:
        row = con.execute("SELECT last_arrival FROM cursors WHERE agent=? AND ledger=?",
                          (role, ledger)).fetchone()
    else:
        row = con.execute("SELECT last_arrival FROM cursors_v2 "
                          "WHERE role=? AND carril=? AND ledger=?",
                          (role, carril, ledger)).fetchone()
    con.close()
    return row[0] if row else None


def _cliente_indexado(s):
    c = TestClient(s.app)
    c.__enter__()
    s.barrido()
    return c


def test_grant_ack_idempotente_y_replay_conflict(tmp_path, monkeypatch):
    s, h = _v8(tmp_path, monkeypatch)
    c = _cliente_indexado(s)
    g = _grant(c.get("/inbox/backend", params={"only": "demo-ledger"}, headers=h).text)
    elegido = min(g["allowed_arrivals"])  # ACK parcial dentro del rango mostrado
    pedido = {"grant": g, "arrival": elegido}
    first = c.post("/inbox/backend/ack", json=pedido, headers=h)
    again = c.post("/inbox/backend/ack", json=pedido, headers=h)
    assert first.status_code == 200 and again.json() == first.json()
    assert _cursor(s) == elegido
    otro = g["watermark"]
    conflict = c.post("/inbox/backend/ack",
                      json={"grant": g, "arrival": otro}, headers=h)
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "ACK_REPLAY_CONFLICT"
    assert _cursor(s) == elegido


def test_overack_tamper_expiry_y_cross_lane_no_mutan(tmp_path, monkeypatch):
    s, h = _v8(tmp_path, monkeypatch)
    c = _cliente_indexado(s)
    g = _grant(c.get("/inbox/backend", params={"only": "demo-ledger"}, headers=h).text)
    baseline = _cursor(s)
    tampered = {**g, "grant": ("A" if g["grant"][0] != "A" else "B") + g["grant"][1:]}
    contract_tampered = {**g, "principal": "principal:alien"}
    cases = [
        ({"grant": g, "arrival": g["watermark"] + 100}, h,
         "ACK_ARRIVAL_OUT_OF_GRANT"),
        ({"grant": tampered, "arrival": g["watermark"]}, h, "ACK_GRANT_INVALID"),
        ({"grant": contract_tampered, "arrival": g["watermark"]}, h,
         "ACK_CONTRACT_MISMATCH"),
        ({"grant": g, "arrival": g["watermark"]},
         {**h, "X-Llminbox-Carril": "otro"}, "ACK_GRANT_MISMATCH"),
    ]
    for body, headers, code in cases:
        r = c.post("/inbox/backend/ack", json=body, headers=headers)
        assert r.status_code == 409 and r.json()["detail"]["code"] == code
        assert _cursor(s) == baseline
    con = sqlite3.connect(s.DB)
    con.execute("UPDATE ack_grants SET expires_at=1")
    con.commit(); con.close()
    expired_grant = {**g, "expires_at": 1}
    expired = c.post("/inbox/backend/ack",
                     json={"grant": expired_grant, "arrival": g["watermark"]}, headers=h)
    assert expired.status_code == 409
    assert expired.json()["detail"]["code"] == "ACK_GRANT_EXPIRED"
    assert _cursor(s) == baseline


def test_dos_grants_con_mismo_cursor_el_segundo_queda_stale(tmp_path, monkeypatch):
    s, h = _v8(tmp_path, monkeypatch)
    c = _cliente_indexado(s)
    g1 = _grant(c.get("/inbox/backend", params={"only": "demo-ledger"}, headers=h).text)
    g2 = _grant(c.get("/inbox/backend", params={"only": "demo-ledger"}, headers=h).text)
    assert c.post("/inbox/backend/ack",
                  json={"grant": g1, "arrival": g1["watermark"]},
                  headers=h).status_code == 200
    stale = c.post("/inbox/backend/ack",
                   json={"grant": g2, "arrival": g2["watermark"]}, headers=h)
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "ACK_GRANT_STALE"
    assert _cursor(s) == g1["watermark"]


def test_ack_grant_con_lane_fuera_de_clase_no_llega_a_validar(tmp_path, monkeypatch):
    """RULING @cto (MARK:cto-contrato-del-envelope-lane-ledger-se-validan-con-el-mismo-
    regex-del-cli-en-el-servidor-fail-closed, 2026-09-07T17:39:35Z): `AckGrantV1.lane`/
    `.ledger` llevan ahora el mismo `pattern` que ya tenía `grant` — un valor fuera de
    `[A-Za-z0-9._-]` tiene que rechazarse en la validación Pydantic del body, ANTES de
    tocar `ack_grants` o los cursores.

    FALSADOR: sin el `pattern=`, esto pasaría de largo hasta la lógica de negocio (y
    dependiendo del valor, podría llegar a comparar/loguear un `lane` con caracteres
    que rompen el TSV de `carriles.tsv` en otro sitio del sistema).
    """
    s, h = _v8(tmp_path, monkeypatch)
    c = _cliente_indexado(s)
    g = _grant(c.get("/inbox/backend", params={"only": "demo-ledger"}, headers=h).text)
    tampered = {**g, "lane": "mal/carril"}
    r = c.post("/inbox/backend/ack", json={"grant": tampered, "arrival": g["watermark"]},
               headers=h)
    assert r.status_code == 422, r.text
    con = sqlite3.connect(s.DB)
    assert con.execute(
        "SELECT used_arrival FROM ack_grants WHERE nonce_hash IS NOT NULL"
    ).fetchone()[0] is None, "el grant no debe quedar consumido por un body que ni valida"
    con.close()


def test_alias_canonico_si_pero_otro_principal_del_mismo_rol_no(tmp_path, monkeypatch):
    cred1, cred2 = "cred-be-one-000000000", "cred-be-two-000000000"
    mapa = tmp_path / "credenciales.json"
    mapa.write_text(json.dumps({
        cred1: {"rol": "be", "carril": "demo", "principal_id": "workload-one"},
        cred2: {"rol": "be", "carril": "demo", "principal_id": "workload-two"},
    }))
    s = construir(tmp_path, monkeypatch, extra_env={
        "LLMINBOX_CREDENCIALES": str(mapa),
        "LLMINBOX_CREDENCIALES_SHA": hashlib.sha256(mapa.read_bytes()).hexdigest(),
    })
    c = _cliente_indexado(s)
    h1 = {"X-Llminbox-Token": cred1, "X-Llminbox-Carril": "demo"}
    h2 = {"X-Llminbox-Token": cred2, "X-Llminbox-Carril": "demo"}
    g = _grant(c.get("/inbox/backend", params={"only": "demo-ledger"}, headers=h1).text)
    pedido = {"grant": g, "arrival": g["watermark"]}
    wrong = c.post("/inbox/backend-biklabs/ack", json=pedido, headers=h2)
    assert wrong.status_code == 409 and _cursor(s) is None
    ok = c.post("/inbox/backend-biklabs/ack", json=pedido, headers=h1)
    assert ok.status_code == 200 and ok.json()["role"] == "be"


def test_single_ledger_legacy_no_inventa_grant_v1_y_conserva_leido(tmp_path, monkeypatch):
    demo = tmp_path / "DEMO-LEDGER.md"
    s = construir(tmp_path, monkeypatch, extra_env={
        "LLMINBOX_LEDGERS": f"solo={demo}",
        "LLMINBOX_CARRILES": "",
    })
    c = _cliente_indexado(s)
    texto = c.get("/inbox/backend", headers={"X-Llminbox-Token": "test-token"}).text
    sobre = json.loads([x.strip() for x in texto.splitlines()
                        if x.strip().startswith('{"hasta"')][-1])
    assert "ack" not in sobre
    assert "ACK_IDENTITY_REQUIRED" in texto
    r = c.post("/inbox/backend/leido", json={"hasta": sobre["hasta"]},
               headers={"X-Llminbox-Token": "test-token"})
    assert r.status_code == 200 and _cursor(s, ledger="solo", legacy=True) == sobre["hasta"]["solo"]


def test_multi_ledger_sin_mapa_no_emite_capacidad_global(tmp_path, monkeypatch):
    s = construir(tmp_path, monkeypatch, extra_env={"LLMINBOX_CARRILES": ""})
    c = _cliente_indexado(s)
    texto = c.get("/inbox/backend", headers={"X-Llminbox-Token": "test-token"}).text
    sobre = json.loads([x.strip() for x in texto.splitlines()
                        if x.strip().startswith('{"hasta"')][-1])
    assert len(sobre["hasta"]) == 2 and "ack" not in sobre
    assert "ACK_IDENTITY_REQUIRED" in texto


def test_credencial_lane_b_no_puede_elegir_lane_a(tmp_path, monkeypatch):
    cred_a, cred_b = "cred-lane-a-000000000", "cred-lane-b-000000000"
    carriles = tmp_path / "ack-carriles.tsv"
    carriles.write_text(
        "carril\tledger_path\twiki_path\testado\tnotas\n"
        f"demo\t{tmp_path / 'DEMO-LEDGER.md'}\t-\tcompleto\t-\n"
        f"otro\t{tmp_path / 'OTRO-LEDGER.md'}\t-\tcompleto\t-\n"
    )
    mapa = tmp_path / "ack-lanes.json"
    mapa.write_text(json.dumps({
        cred_a: {"rol": "be", "carril": "demo", "principal_id": "lane-a"},
        cred_b: {"rol": "be", "carril": "otro", "principal_id": "lane-b"},
    }))
    s = construir(tmp_path, monkeypatch, extra_env={
        "LLMINBOX_CARRILES": str(carriles),
        "LLMINBOX_CREDENCIALES": str(mapa),
        "LLMINBOX_CREDENCIALES_SHA": hashlib.sha256(mapa.read_bytes()).hexdigest(),
    })
    c = _cliente_indexado(s)
    r = c.get("/inbox/backend", headers={
        "X-Llminbox-Token": cred_b, "X-Llminbox-Carril": "demo",
    })
    assert r.status_code == 403
    con = sqlite3.connect(s.DB)
    assert con.execute("SELECT COUNT(*) FROM ack_grants").fetchone()[0] == 0
    con.close()
    assert _cursor(s) is None


def test_v8_leido_sin_get_no_es_bypass(tmp_path, monkeypatch):
    s, h = _v8(tmp_path, monkeypatch)
    c = _cliente_indexado(s)
    r = c.post("/inbox/backend/leido", json={"hasta": {"demo-ledger": 1}}, headers=h)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "LEGACY_ACK_DISABLED"
    assert _cursor(s) is None


def test_token_compartido_no_emite_grant_aunque_v8_este_activo(tmp_path, monkeypatch):
    s, _ = _v8(tmp_path, monkeypatch)
    c = _cliente_indexado(s)
    r = c.get("/inbox/backend", params={"only": "demo-ledger"}, headers=H)
    assert r.status_code == 200
    sobre = json.loads([x.strip() for x in r.text.splitlines()
                        if x.strip().startswith('{"hasta"')][-1])
    assert "ack" not in sobre
    assert "ACK_IDENTITY_REQUIRED" in r.text


def test_inbox_entrega_lectura_si_grant_no_puede_tomar_writer(tmp_path, monkeypatch):
    s, h = _v8(tmp_path, monkeypatch)
    c = _cliente_indexado(s)
    blocker = sqlite3.connect(s.DB, timeout=0.1)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        r = c.get("/inbox/backend", params={"only": "demo-ledger"}, headers=h)
    finally:
        blocker.rollback(); blocker.close()
    assert r.status_code == 200 and "#0" in r.text
    sobre = json.loads([x.strip() for x in r.text.splitlines()
                        if x.strip().startswith('{"hasta"')][-1])
    assert "ack" not in sobre
    assert sobre["ack_unavailable"] == {"code": "ACK_STORE_BUSY", "retryable": True}


def test_legacy_rechaza_overack_y_rewind_sin_mutar(tmp_path, monkeypatch):
    s = construir(tmp_path, monkeypatch)
    c = _cliente_indexado(s)
    over = c.post("/inbox/backend/leido", json={"hasta": {"demo-ledger": 99999}}, headers=H)
    assert over.status_code == 409
    assert over.json()["detail"]["code"] == "ACK_BEYOND_LEDGER"
    assert _cursor(s, legacy=True) is None
    assert c.post("/inbox/backend/leido", json={"hasta": {"demo-ledger": 1}}, headers=H).status_code == 200
    rewind = c.post("/inbox/backend/leido", json={"hasta": {"demo-ledger": 0}}, headers=H)
    assert rewind.status_code == 409
    assert rewind.json()["detail"]["code"] == "ACK_REWIND_REQUIRES_RECOVERY"
    assert _cursor(s, legacy=True) == 1


def test_legacy_forward_concurrente_nunca_termina_en_el_menor(tmp_path, monkeypatch):
    """Falsador del TOCTOU: high y low comparten BEGIN IMMEDIATE, no sólo preflight."""
    s = construir(tmp_path, monkeypatch)
    c = _cliente_indexado(s)

    def ack(arrival):
        return c.post("/inbox/backend/leido",
                      json={"hasta": {"demo-ledger": arrival}}, headers=H).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(ack, (1, 0)))
    assert set(statuses) <= {200, 409}
    assert 200 in statuses
    assert _cursor(s, legacy=True) == 1, "el forward menor ganó después del mayor: rewind por carrera"
