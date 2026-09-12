#!/usr/bin/env python3
"""Falsador rojo de la composición real Search + gateway nativo + Journal.

Este fichero no se llama ``test_*`` para no fingir que la rama M2 aislada contiene el
gateway. Se ejecuta expresamente sobre el commit de composición:

    python3 tests/search/falsificador_native_lifecycle_v3.py

La ausencia o un blob distinto del gateway fijado es un ERROR de precondición, nunca
un skip/xfail verde. El ancla identifica el sujeto que se prueba; por sí sola no
acredita una auditoría ni un resultado de ejecución.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
GATEWAY = ROOT / "native_gateway.py"
# Gateway del snapshot Faro con las rutas de supervisión y organización integradas.
# El gate histórico v4 sustituye ambas constantes al superponer su propio sujeto.
GATEWAY_SHA256 = "683996722dbc3ec1714990533fdd1c0024807847856793939123174fdb170d94"
GATEWAY_GIT_BLOB = "d5024b40524bdbde99578ff4fab8b0f9ed8a6745"
if not GATEWAY.is_file():
    raise RuntimeError(
        "PRECONDICIÓN NO CUMPLIDA: native_gateway.py no está integrado; se esperaba "
        f"sha256={GATEWAY_SHA256} git-blob={GATEWAY_GIT_BLOB}. Este rojo no se "
        "puede convertir en skip/xfail")
gateway_bytes = GATEWAY.read_bytes()
gateway_sha256 = hashlib.sha256(gateway_bytes).hexdigest()
gateway_git_blob = hashlib.sha1(
    f"blob {len(gateway_bytes)}\0".encode("ascii") + gateway_bytes,
    usedforsecurity=False,
).hexdigest()
if gateway_sha256 != GATEWAY_SHA256 or gateway_git_blob != GATEWAY_GIT_BLOB:
    raise RuntimeError(
        "PRECONDICIÓN NO CUMPLIDA: native_gateway.py no coincide con el blob fijado "
        f"sha256={GATEWAY_SHA256} git-blob={GATEWAY_GIT_BLOB}; observado "
        f"sha256={gateway_sha256} git-blob={gateway_git_blob}")

import coordination as C  # noqa: E402
import native_gateway as G  # noqa: E402  (sólo después de verificar el ancla)
from tests.journal._arnes import GRAMATICA, abre_admision, censo  # noqa: E402
from tests.pytest.conftest import construir  # noqa: E402


INTENT = {"type": "message", "verb": "inform", "to": ["backend"],
          "kind": "DELIVERED", "head": "lifecycle", "body": "payload"}


def _compuesta(tmp_path, monkeypatch):
    search = tmp_path / "search"
    search.mkdir()
    servicio = construir(
        search, monkeypatch,
        extra_env={"LLMINBOX_SEARCH_CURSOR_KEY": "k" * 32})
    servicio.CREDENCIALES = {
        "cred-http": {"rol": "be", "carril": "demo", "principal_id": "p-http"}}
    journal_dir = tmp_path / "journal"
    journal_dir.mkdir()
    journal = C.Journal(str(journal_dir / "coordination.sqlite"),
                        pepper=b"lifecycle-native-composition",
                        lane_ledgers={"demo": ["demo-ledger"]},
                        grammar=GRAMATICA,
                        recipient_resolver=censo)
    journal.initialize()
    G.configure_journal_from_v8(
        journal,
        {"native-credential": {"rol": "be", "carril": "demo",
                               "principal_id": "p-native"}},
        {"p-native": [G.CAP_EVENT_WRITER]},
    )
    # La prueba presupone un carril operativo. Su operador abre la admisión
    # mediante sesión y capacidad reales antes de emitir la sesión del escritor.
    # El cierre por defecto del producto se conserva.
    abre_admision(journal, lanes=["demo"])
    servicio.app.include_router(G.create_native_router(journal))
    return servicio, journal, search, journal_dir


def _abre_sesion(cliente):
    respuesta = cliente.post(
        G.API_PREFIX + "/sessions",
        headers={"Authorization": "Bearer native-credential"},
        json={"ttl_s": 300})
    assert respuesta.status_code == 201, respuesta.text
    return respuesta.json()["token"]


def _evento_y_recibo(cliente, token, key):
    evento = cliente.post(
        G.API_PREFIX + "/events",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key},
        json=INTENT)
    assert evento.status_code == 202, evento.text
    aceptado = evento.json()
    recibo = cliente.get(
        G.API_PREFIX + f"/receipts/{aceptado['receipt_id']}",
        headers={"Authorization": f"Bearer {token}"})
    assert recibo.status_code == 200, recibo.text
    assert recibo.json()["event_id"] == aceptado["event_id"]
    assert recibo.json()["subject_type"] == "event"
    assert recibo.json()["subject_id"] == aceptado["event_id"]
    assert recibo.json()["state"] == "accepted"
    assert "current_state" not in recibo.json()
    assert "subject_kind" not in recibo.json()
    assert "principal_id" not in recibo.json()


@pytest.mark.parametrize("averia", ["ddl_corrupto", "volumen_ro"])
def test_search_fisicamente_roto_no_bloquea_evento_y_recibo_publicos(
        tmp_path, monkeypatch, averia):
    servicio, journal, search_dir, _ = _compuesta(tmp_path, monkeypatch)
    with TestClient(servicio.app):
        servicio.barrido()
        con = servicio.db()
        try:
            servicio.preparar_busqueda_publica(con, reconstruir=True)
        finally:
            con.close()
    ruta = Path(os.environ["LLMINBOX_DB"])
    if averia == "ddl_corrupto":
        con = sqlite3.connect(ruta)
        con.execute("DROP TRIGGER search_ai")
        con.commit()
        con.close()
    else:
        for fichero in search_dir.iterdir():
            if fichero.name.startswith(ruta.name):
                fichero.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        search_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        with TestClient(servicio.app) as cliente:
            token = _abre_sesion(cliente)
            search = cliente.get(
                "/search?q=cuerpo", headers={"X-Llminbox-Token": "cred-http"})
            if averia == "ddl_corrupto":
                assert search.status_code == 503
                assert search.json() == {"detail": "índice de búsqueda no listo"}
            else:
                # Un derivado válido en RO sigue siendo legible; RO sólo veta repararlo.
                assert search.status_code == 200, search.text
            _evento_y_recibo(cliente, token, f"despues-{averia}")
    finally:
        search_dir.chmod(stat.S_IRWXU)
        for fichero in search_dir.iterdir():
            fichero.chmod(stat.S_IRUSR | stat.S_IWUSR)
        journal.dispose()


def test_journal_fisicamente_ro_deja_leer_recibo_y_rechaza_mutacion_publica(
        tmp_path, monkeypatch):
    servicio, journal, _, journal_dir = _compuesta(tmp_path, monkeypatch)
    with TestClient(servicio.app) as cliente:
        token = _abre_sesion(cliente)
        _evento_y_recibo(cliente, token, "antes-journal-ro")
    journal.close()
    for fichero in journal_dir.iterdir():
        fichero.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    journal_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        # Detección pública del propio Journal sobre permisos físicos, sin flags privados.
        journal.initialize()
        assert journal.health()["writable"] is False
        with TestClient(servicio.app) as cliente:
            lectura = cliente.get(
                G.API_PREFIX + "/events/evt_inexistente/receipt",
                headers={"Authorization": f"Bearer {token}"})
            assert lectura.status_code == 404
            mutacion = cliente.post(
                G.API_PREFIX + "/events",
                headers={"Authorization": f"Bearer {token}",
                         "Idempotency-Key": "journal-realmente-ro"},
                json=INTENT)
            assert mutacion.status_code == 503
            assert mutacion.json()["code"] == "JOURNAL_READ_ONLY"
    finally:
        journal_dir.chmod(stat.S_IRWXU)
        for fichero in journal_dir.iterdir():
            fichero.chmod(stat.S_IRUSR | stat.S_IWUSR)
        journal.dispose()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
