"""El mismo endpoint declara un recorte y calla el otro — y calla el que más oculta.

`GET /entries` recorta de DOS formas cuando se piden cuerpos, y las trata al revés:

    por BYTES   -> marca cada fila con `cuerpo_recortado: true` Y manda la cabecera
                   `X-Cuerpos-Recortados: N`.  Declarado, meticuloso.
    por FILAS   -> `limit = min(limit, CUERPO_MAX_FILAS)`.  SILENCIOSO.

Medido contra producción el 2026-09-01:

    GET /entries?limit=400&cuerpo=true   -> 10 entradas    <- pediste 400
    GET /entries?limit=400&cuerpo=false  -> 400 entradas

② HAY CONSUMIDOR y actúa sobre ello: `web/src/lib/api.ts` pone `cuerpo: p.cuerpo ?? true`
—o sea que TODA la lista de la interfaz va con cuerpos— y `queries.ts` pide `limit: 400`.
La interfaz muestra 10 de 400 y **no tiene cómo saber que faltan 390**: la respuesta es
una lista plana, sin campo ni cabecera que lo diga.

El capado en sí es correcto y su motivo está medido en el propio fichero (las 10 entradas
más grandes suman 1,6 MB). Lo que no puede ser es MUDO: un techo que no se anuncia
convierte «esto es todo lo que hay» en indistinguible de «esto es todo lo que te doy».
"""
from __future__ import annotations
from fastapi.testclient import TestClient
from .conftest import construir


def _monta(tmp_path, monkeypatch, n=40):
    s = construir(tmp_path, monkeypatch)
    led = tmp_path / "L.md"
    led.write_text("".join(
        f"### [a → backend · FYI] 2026-09-01T10:{i//60:02d}:{i%60:02d}Z — e{i}\ncuerpo largo {i} " +
        "x" * 200 + "\n\n" for i in range(n)))
    monkeypatch.setattr(s, "LEDGERS", {"L": str(led)})
    con = s.db(); s._preparar_indice(con); s.reindex("L", str(led), con); con.commit(); con.close()
    return s, TestClient(s.app), {"X-Llminbox-Token": s.TOKEN}


def test_si_se_capan_filas_la_respuesta_LO_DICE(tmp_path, monkeypatch):
    s, c, H = _monta(tmp_path, monkeypatch)
    r = c.get("/entries?limit=400&cuerpo=true", headers=H)
    assert r.status_code == 200
    devueltas = len(r.json())
    assert devueltas < 400, "el montaje no llega a capar: el test no mediría nada"
    assert "X-Filas-Capadas" in r.headers, (
        f"devolvió {devueltas} de 400 pedidas y no lo dice por ningún sitio: la interfaz "
        f"muestra una lista incompleta sin marca")
    assert r.headers["X-Filas-Capadas"] == str(devueltas)


def test_sin_capado_NO_aparece_la_cabecera(tmp_path, monkeypatch):
    """⊕ obligatorio: una cabecera que sale siempre no informa de nada, y enseña a
    ignorarla — la misma avería que un rojo permanente."""
    s, c, H = _monta(tmp_path, monkeypatch)
    r = c.get("/entries?limit=5&cuerpo=true", headers=H)
    assert len(r.json()) == 5, "el montaje debería devolver las 5 sin capar"
    assert "X-Filas-Capadas" not in r.headers


def test_sin_cuerpos_no_se_capa_y_no_se_anuncia(tmp_path, monkeypatch):
    """⊖ de alcance: el capado es CONSECUENCIA de pedir cuerpos. Sin ellos, `limit` manda
    y no hay nada que declarar."""
    s, c, H = _monta(tmp_path, monkeypatch, n=40)
    r = c.get("/entries?limit=400&cuerpo=false", headers=H)
    assert len(r.json()) == 40
    assert "X-Filas-Capadas" not in r.headers


def test_el_recorte_por_BYTES_sigue_declarándose(tmp_path, monkeypatch):
    """⊖ de no-regresión: el hermano que YA se declaraba tiene que seguir haciéndolo. La
    cura de uno no puede tapar al otro — son dos recortes distintos y el consumidor
    necesita distinguirlos."""
    s, c, H = _monta(tmp_path, monkeypatch)
    monkeypatch.setattr(s, "CUERPO_MAX_BYTES", 300)
    r = c.get("/entries?limit=400&cuerpo=true", headers=H)
    assert "X-Cuerpos-Recortados" in r.headers
    assert any(x.get("cuerpo_recortado") for x in r.json())
