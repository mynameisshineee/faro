"""«Aún nadie ha armado» y «NADIE PODRÁ armar nunca» no son el mismo estado.

Medido en producción el 2026-08-31, justo después de desplegar C5:

    POST /vigilancia/ack -> 403      (exige X-Llminbox-Watcher)
    GET  /pendientes     -> 401
    /health -> vigilancia: sin-armar · ok: TRUE

Sin `LLMINBOX_WATCHER_TOKEN` el ack es INALCANZABLE, así que la vigilancia no puede
armarse NUNCA — y `sin-armar` está en `VIGILANCIA_SANOS`, así que `ok` seguía en `true`.
El hombre muerto que vigila a 71 agentes no puede nacer, y el indicador de salud dice que
todo está bien.

LA INCOHERENCIA ESTÁ EN EL PROPIO DISEÑO, no en una opinión mía: `sano` YA cuenta la
vigilancia —si está `muda`, `ok` es false—. O sea que el caso malo (hubo latido y se
perdió) tumba el verde, y el caso PEOR (no puede haber latido jamás) no lo tumba. El
estado sin casilla cayendo al lado bueno, en el sitio donde más caro sale.

`sin-armar` sigue siendo sano cuando es TRANSITORIO —un servicio recién arrancado espera
su primer ack y eso es normal—. Lo que no puede ser sano es lo que no tiene salida.
"""
from __future__ import annotations
import time
import pytest
from fastapi.testclient import TestClient
from .conftest import construir


def _monta(tmp_path, monkeypatch, watcher):
    if watcher is None:
        monkeypatch.delenv("LLMINBOX_WATCHER_TOKEN", raising=False)
    else:
        monkeypatch.setenv("LLMINBOX_WATCHER_TOKEN", watcher)
    s = construir(tmp_path, monkeypatch)
    s.SALUD["ultimo_ok"] = time.time()
    con = s.db(); s._preparar_indice(con); con.close()
    return s, TestClient(s.app)


def test_sin_credencial_la_vigilancia_no_es_sana(tmp_path, monkeypatch):
    s, c = _monta(tmp_path, monkeypatch, None)
    v = c.get("/health", headers={"X-Llminbox-Token": s.TOKEN}).json()
    assert v["vigilancia"]["estado"] not in s.VIGILANCIA_SANOS, (
        f"«{v['vigilancia']['estado']}» cuenta como sano y el ack es inalcanzable: la "
        f"alarma de la flota no puede nacer y /health no lo tumba")
    assert v["ok"] is False


def test_con_credencial_sin_armar_SIGUE_siendo_sano(tmp_path, monkeypatch):
    """⊕ OBLIGATORIO. Sin esto, la cura barata —«sin-armar nunca es sano»— pasa el ⊖ de
    arriba y deja TODO servicio recién arrancado en rojo hasta su primer ack. Un rojo que
    sale siempre enseña a ignorar el rojo."""
    s, c = _monta(tmp_path, monkeypatch, "w")
    v = c.get("/health", headers={"X-Llminbox-Token": s.TOKEN}).json()
    assert v["vigilancia"]["estado"] == "sin-armar"
    assert v["vigilancia"]["estado"] in s.VIGILANCIA_SANOS
    assert v["ok"] is True, "esperar el primer ack es normal, no una avería"


def test_una_vez_ARMADA_la_credencial_ausente_ya_no_la_degrada(tmp_path, monkeypatch):
    """⊖ del alcance: el estado nuevo describe «no puede armarse», no «falta un token».
    Si YA hay latido, la vigilancia está viva aunque el token desaparezca del entorno —
    porque lo que se mide es si el mecanismo funciona, no si su llave está a mano."""
    s, c = _monta(tmp_path, monkeypatch, None)
    con = s.db()
    con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (s._META_LATIDO, str(time.time())))
    con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (s._META_QUIEN, "watcher"))
    con.commit(); con.close()
    v = c.get("/health", headers={"X-Llminbox-Token": s.TOKEN}).json()
    assert v["vigilancia"]["estado"] == "viva"


def test_el_estado_nuevo_esta_en_la_particion(tmp_path, monkeypatch):
    """El enum es exhaustivo por contrato y `/health` lo publica: un estado que existe y
    no se declara deja al consumidor con un valor que su tabla no cubre."""
    s, c = _monta(tmp_path, monkeypatch, None)
    v = c.get("/health", headers={"X-Llminbox-Token": s.TOKEN}).json()["vigilancia"]
    assert v["estado"] in v["estados"], "el estado servido no está en la lista publicada"
    assert set(v["sanos"]) <= set(v["estados"])
