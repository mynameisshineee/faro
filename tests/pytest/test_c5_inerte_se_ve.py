"""Si `/pendientes` no puede servir a nadie, `/health` lo DICE.

Medido antes de desplegar (2026-08-31): el contenedor de producción no tiene
`LLMINBOX_WATCHER_TOKEN`, y `docker-compose.yml` NI SIQUIERA LO PASA — o sea que la
variable no tenía camino hasta el servicio. Con C5 desplegado, `/pendientes` respondería
401 A TODO EL MUNDO PARA SIEMPRE, y nada lo cantaría: el endpoint existe, el gate
funciona, y el efecto es un servicio que rechaza antes de poder servir.

El fail-closed es CORRECTO —abrir sería peor— pero un gate inerte y CALLADO es la forma
de la casa que llevamos toda la semana cazando: un estado que no tiene casilla cae al
lado bueno. `/health` ya publica `auth: bool(TOKEN)` para el token compartido; esto es la
misma pregunta para el otro, y la respuesta es accionable: dice qué endpoint queda
inerte y por qué.
"""
from __future__ import annotations
from fastapi.testclient import TestClient
from .conftest import construir


def _salud(tmp_path, monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v) if v is not None else monkeypatch.delenv(k, raising=False)
    s = construir(tmp_path, monkeypatch)
    return TestClient(s.app).get("/health", headers={"X-Llminbox-Token": s.TOKEN}).json(), s


def test_sin_watcher_token_health_lo_canta(tmp_path, monkeypatch):
    d, _ = _salud(tmp_path, monkeypatch, LLMINBOX_WATCHER_TOKEN=None)
    assert d["watcher_auth"] is False
    assert any("pendientes" in a.lower() for a in (d.get("avisos") or [])), (
        "el gate queda inerte y /health no lo dice: un 401 permanente que nadie explica")


def test_con_watcher_token_no_avisa_de_nada(tmp_path, monkeypatch):
    """⊕ obligatorio: si el aviso saliera SIEMPRE sería ruido, y un rojo permanente
    enseña a ignorar el rojo igual que un verde permanente enseña a confiar en él."""
    d, _ = _salud(tmp_path, monkeypatch, LLMINBOX_WATCHER_TOKEN="w")
    assert d["watcher_auth"] is True
    assert not any("pendientes" in a.lower() for a in (d.get("avisos") or []))


def test_el_aviso_NO_tumba_el_ok(tmp_path, monkeypatch):
    """`ok` responde «¿se puede servir el canon AHORA?». Un `/pendientes` inerte no impide
    servir bandejas, así que avisa sin tumbar — mismo criterio que con `reconstrucciones`:
    sólo-lectura no es verde, pero no todo aviso es rojo.

    Necesita LÍNEA BASE VERDE o no mide nada: el arnés arranca con `ok=false` por la
    vigilancia, así que sin armarla esta prueba pasaría con el aviso tumbando el `ok` y yo
    no me enteraría. Mi primera versión ni eso — era una tautología mal escrita
    (`d["ok"] is (d["ok"] is True or ...)`), o sea un test que sólo podía pasar o
    reventar por sintaxis, nunca medir.
    """
    import time
    monkeypatch.delenv("LLMINBOX_WATCHER_TOKEN", raising=False)
    s = construir(tmp_path, monkeypatch)
    s.SALUD["ultimo_ok"] = time.time()
    con = s.db(); s._preparar_indice(con)
    con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (s._META_LATIDO, str(time.time())))
    con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (s._META_QUIEN, "watcher"))
    con.commit(); con.close()
    d = TestClient(s.app).get("/health", headers={"X-Llminbox-Token": s.TOKEN}).json()
    assert d["watcher_auth"] is False
    assert d["ok"] is True, (
        "el aviso de watcher-token tumbó `ok`: el servicio SÍ puede servir bandejas, y un "
        "rojo que no se puede apagar enseña a ignorar el rojo")
    assert any("pendientes" in a.lower() for a in (d.get("avisos") or []))
