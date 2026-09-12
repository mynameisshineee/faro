"""Consumir por encima del final se rechaza sin modificar el cursor.

Antes de `58c3596` un `hasta=99999` era **inerte**: `/pendientes` unía por nombre y el
agregado no bajaba nunca, así que pasarse no tapaba nada. Después, ese mismo `99999` tapa
a **todos los nombres que comparten rol** — `clave_cursor()` declara que `backend`, `be` y
`backend-biklabs` comparten UNA fila.

⇒ el fix no introdujo el defecto, pero **convirtió una costumbre inofensiva en una
silenciosa**. Y `backend` ya tiene ese `99999` puesto en `64bis-wiki`: no es hipotético,
es un consumidor vivo con una consecuencia nueva encima.

El grant normal ya limita el ACK a lo mostrado. La compatibilidad `/leido` conserva
forward válido, pero tampoco puede crear una tapia ni recortar en silencio.
"""
from __future__ import annotations
from fastapi.testclient import TestClient
from .conftest import construir


def _monta(tmp_path, monkeypatch, n=3):
    s = construir(tmp_path, monkeypatch)
    led = tmp_path / "L.md"
    led.write_text("".join(
        f"### [a → backend · FYI] 2026-09-01T10:{i:02d}:00Z — e{i}\ncuerpo\n\n" for i in range(n)))
    monkeypatch.setattr(s, "LEDGERS", {"L": str(led)})
    con = s.db(); s._preparar_indice(con); s.reindex("L", str(led), con); con.commit(); con.close()
    return s, TestClient(s.app), {"X-Llminbox-Token": s.TOKEN}


def test_pasarse_mucho_se_avisa_con_el_exceso(tmp_path, monkeypatch):
    s, c, H = _monta(tmp_path, monkeypatch)      # arrivals 0,1,2 -> máximo 2
    r = c.post("/inbox/backend/leido", headers=H, json={"hasta": {"L": 99999}})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "ACK_BEYOND_LEDGER"
    assert c.get("/cursor/backend", headers=H).json().get("L", -1) == -1


def test_pasarse_por_UNO_tambien_se_registra_pero_se_distingue(tmp_path, monkeypatch):
    """El discriminante del paso ②: `tope+1` es la carrera benigna —llegó una entrada
    entre la lectura y el POST—, no un barrido. Los dos se avisan; el número los separa."""
    s, c, H = _monta(tmp_path, monkeypatch)
    r = c.post("/inbox/backend/leido", headers=H, json={"hasta": {"L": 3}})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "ACK_BEYOND_LEDGER"


def test_consumir_HASTA_el_final_NO_avisa(tmp_path, monkeypatch):
    """⊕ OBLIGATORIO: un aviso que salga siempre no informa de nada y enseña a ignorarlo.
    Consumir exactamente hasta el último arrival es el caso NORMAL."""
    s, c, H = _monta(tmp_path, monkeypatch)
    ap = c.post("/inbox/backend/leido", headers=H,
                json={"hasta": {"L": 2}}).json()["aplicados"]["L"]
    assert not ap.get("mas_alla_del_final"), f"avisó de un consumo normal: {ap}"
    assert "exceso" not in ap


def test_el_aviso_no_cambia_lo_que_se_APLICA(tmp_path, monkeypatch):
    """No se recorta: el servidor rechaza el valor completo y deja el cursor intacto."""
    s, c, H = _monta(tmp_path, monkeypatch)
    r = c.post("/inbox/backend/leido", headers=H, json={"hasta": {"L": 99999}})
    assert r.status_code == 409
    assert c.get("/cursor/backend", headers=H).json().get("L", -1) == -1
