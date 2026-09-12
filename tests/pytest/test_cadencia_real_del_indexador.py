"""`/health` publicaba cuánto TARDA el barrido y no cada cuánto se PIDE: sin las dos, nadie
puede ver que el intervalo configurado es inalcanzable.

Medido en producción el 2026-09-01:

    LLMINBOX_POLL = 30 s        barrido_s = 44,17 s        en_vuelo = True la mitad de las veces

⇒ un barrido tarda MÁS que el intervalo entre barridos, así que la cadencia real no es 30 s:
es el barrido encadenándose consigo mismo, y una entrada nueva puede tardar ~75 s en
aparecer. `/health` publicaba `barrido_s` pero NO `poll_s`, así que el consumidor tenía la
mitad del par y no podía notar la contradicción.

② CON VÍCTIMA DEMOSTRADA, no hipotética: @sdet dedujo un hueco de entrega porque midió dos
veces separadas 12 s y las dos cayeron dentro de la misma ventana de indexado. Lo retiró él
mismo con una frase que es la lección: **«dos muestras dentro de la ventana del fenómeno son
UNA muestra»**. No podía saber cuál era el tiempo característico porque el servicio no lo
publicaba.

`cadencia_s` es el par ya resuelto —`max(poll, barrido)`— para que el consumidor no tenga
que deducir la contradicción: si el barrido supera al intervalo, la cadencia ES el barrido.
"""
from __future__ import annotations
from fastapi.testclient import TestClient
from .conftest import construir


def _idx(tmp_path, monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    s = construir(tmp_path, monkeypatch)
    d = TestClient(s.app).get("/health", headers={"X-Llminbox-Token": s.TOKEN}).json()
    return s, d["indexador"]


def test_publica_el_intervalo_configurado(tmp_path, monkeypatch):
    s, i = _idx(tmp_path, monkeypatch, LLMINBOX_POLL="30.0")
    assert i.get("poll_s") == 30.0, (
        f"publica cuánto TARDA ({i.get('barrido_s')}) y no cada cuánto se PIDE: con la "
        f"mitad del par nadie ve que el intervalo sea inalcanzable")


def test_la_cadencia_es_el_barrido_cuando_lo_supera(tmp_path, monkeypatch):
    """El caso de producción: barrido 44 s con intervalo 30 s ⇒ la cadencia real es 44."""
    s, _ = _idx(tmp_path, monkeypatch, LLMINBOX_POLL="30.0")
    s.SALUD["duracion"] = 44.0
    i = TestClient(s.app).get("/health", headers={"X-Llminbox-Token": s.TOKEN}).json()["indexador"]
    assert i["cadencia_s"] == 44.0, (
        f"cadencia={i.get('cadencia_s')}: si el barrido tarda más que el intervalo, la "
        f"cadencia ES el barrido — el intervalo no se puede cumplir")


def test_la_cadencia_es_el_intervalo_cuando_el_barrido_es_corto(tmp_path, monkeypatch):
    """⊕ OBLIGATORIO: si `cadencia_s` fuera siempre el barrido, un servicio ocioso
    publicaría una cadencia de milisegundos y sería igual de engañoso por el otro lado."""
    s, _ = _idx(tmp_path, monkeypatch, LLMINBOX_POLL="30.0")
    s.SALUD["duracion"] = 0.4
    i = TestClient(s.app).get("/health", headers={"X-Llminbox-Token": s.TOKEN}).json()["indexador"]
    assert i["cadencia_s"] == 30.0


def test_sin_barrido_todavia_la_cadencia_es_el_intervalo(tmp_path, monkeypatch):
    """⊖ del arranque: antes del primer barrido no hay duración medida. La cadencia no
    puede ser `None` ni 0 — lo único que se sabe es lo configurado."""
    s, _ = _idx(tmp_path, monkeypatch, LLMINBOX_POLL="12.5")
    s.SALUD["duracion"] = None
    i = TestClient(s.app).get("/health", headers={"X-Llminbox-Token": s.TOKEN}).json()["indexador"]
    assert i["cadencia_s"] == 12.5
