"""⑤ `only=` en GET /inbox — acota a UN ledger, y ATRAVIESA el archivo
(INBOX_EXCLUIR) cuando se pide explícitamente.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import construir


def test_only_filtra_de_verdad(cliente):
    """El falsador EXACTO que ya usó backend-biklabs en producción: comparar
    con vs sin `only=` tiene que dar contenido DISTINTO. Si dan lo mismo,
    `only` sigue sin filtrar.
    """
    base = cliente.get("/inbox/backend").text
    solo_demo = cliente.get("/inbox/backend?only=demo-ledger").text

    assert "demo-ledger" in base and "otro-ledger" in base
    assert "demo-ledger" in solo_demo
    assert "otro-ledger" not in solo_demo
    assert base != solo_demo


def test_only_ledger_inexistente_422(cliente):
    """FALSADOR: si devuelve 200 vacío, vuelve al patrón "silencio en vez de
    error" que ① ya cerró en otro sitio — inconsistente entre endpoints.
    """
    r = cliente.get("/inbox/backend?only=no-existe")
    assert r.status_code == 422
    assert "no-existe" in r.json()["detail"]


def test_only_atraviesa_el_archivo(tmp_path, monkeypatch):
    """`only=<ledger-archivado>` con entradas reales pendientes debe TRAERLAS,
    no devolver el mensaje de "excluidos" — si no, `only` sobre el único ledger
    que se excluye por defecto reproduce el mismo bug que se está arreglando,
    con otra forma. Se comprueban las DOS ramas en el mismo test: con `only=`
    trae contenido; sin él, sigue excluido del listado normal (declarado al pie).
    """
    s = construir(tmp_path, monkeypatch, extra_env={"LLMINBOX_INBOX_EXCLUIR": "otro-ledger"})
    with TestClient(s.app) as c:
        s.barrido()
        c.headers.update({"X-Llminbox-Token": "test-token"})

        normal = c.get("/inbox/backend").text
        assert "otro-ledger" not in normal.split("fuera de la bandeja")[0]
        assert "fuera de la bandeja por archivo: otro-ledger" in normal

        solo_archivo = c.get("/inbox/backend?only=otro-ledger")
        assert solo_archivo.status_code == 200
        assert "otro-ledger" in solo_archivo.text
        assert "(nada nuevo" not in solo_archivo.text
