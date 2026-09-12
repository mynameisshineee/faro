"""El latido declara sus HOLDs, y los holds envejecen con él.

Pedido por @harness con adjudicación de @cto: el watcher cuenta filas de pendientes que no
rutan (agente no vivo, ledger sin mapear). No tumban el latido —son backlog retenido, no
avería— pero **tienen que verse donde se lee `viva`**: un lector de `/health` en crudo veía
`viva` a secas sobre un backlog acumulándose.

DE DÓNDE SALE EL DATO, y es la decisión de diseño: **lo trae el ack**, no lo lee este
servicio del fichero del watcher (`~/.bik-heartbeats/vigia-holds`). Leer el fichero de otro
proceso acopla este servicio a su formato y a su ruta, y crea una segunda fuente que puede
divergir de la primera. El productor lo declara en la misma llamada que acredita su ciclo,
y se persiste en la MISMA transacción que el latido.

⚠️ Y LOS HOLDS ENVEJECEN CON EL LATIDO, que es la trampa que este diseño tiene que evitar:
publicar «0 holds» de un ack de hace cuatro horas es exactamente la clase que llevamos días
cerrando —un número correcto cuando se tomó y falso cuando se lee—. Por eso van con
`de_hace_s`: la misma edad que el latido que los trajo. Un consumidor que vea `muda` sabe
que los holds son de entonces, no de ahora.
"""
from __future__ import annotations
import time
from fastapi.testclient import TestClient
from .conftest import construir

WT = "w"


def _cli(tmp_path, monkeypatch):
    monkeypatch.setenv("LLMINBOX_WATCHER_TOKEN", WT)
    s = construir(tmp_path, monkeypatch)
    con = s.db(); s._preparar_indice(con); con.close()
    return s, TestClient(s.app), {"X-Llminbox-Token": s.TOKEN, "X-Llminbox-Watcher": WT}


def _vig(c, s):
    return c.get("/health", headers={"X-Llminbox-Token": s.TOKEN}).json()["vigilancia"]


def test_el_ack_acepta_los_holds_y_health_los_publica(tmp_path, monkeypatch):
    s, c, H = _cli(tmp_path, monkeypatch)
    r = c.post("/vigilancia/ack", headers=H, params={
        "quien": "watcher", "holds": 7, "holds_no_vivo": 5,
        "holds_sin_mapear": 2, "hold_max_s": 3600})
    assert r.status_code == 200 and r.json()["latido"] is True
    h = _vig(c, s)["holds"]
    assert (h["n"], h["no_vivo"], h["sin_mapear"], h["mas_viejo_s"]) == (7, 5, 2, 3600)


def test_los_holds_NO_tumban_el_latido(tmp_path, monkeypatch):
    """⊖ de alcance: son backlog retenido, no avería. Si tumbaran `viva`, un watcher sano
    con trabajo pendiente se declararía roto — y el que decide qué es avería es @cto."""
    s, c, H = _cli(tmp_path, monkeypatch)
    c.post("/vigilancia/ack", headers=H, params={"quien": "watcher", "holds": 99})
    v = _vig(c, s)
    assert v["estado"] == "viva" and v["holds"]["n"] == 99


def test_los_holds_ENVEJECEN_con_el_latido(tmp_path, monkeypatch):
    """EL QUE IMPORTA: publicar «0 holds» de un ack viejo es un número correcto cuando se
    tomó y falso cuando se lee. Van con la edad del latido que los trajo."""
    s, c, H = _cli(tmp_path, monkeypatch)
    c.post("/vigilancia/ack", headers=H, params={"quien": "watcher", "holds": 4})
    time.sleep(0.05)
    h = _vig(c, s)["holds"]
    assert h["de_hace_s"] is not None and h["de_hace_s"] >= 0, h
    assert abs(h["de_hace_s"] - _vig(c, s)["hace_s"]) < 2, (
        "la edad de los holds no es la del latido: se leerían como frescos siendo viejos")


def test_sin_holds_declarados_el_bloque_dice_DESCONOCIDO_no_cero(tmp_path, monkeypatch):
    """⊕ OBLIGATORIO y es la mitad que decide si esto sirve: un watcher que no manda holds
    —el de hoy, o uno viejo sin migrar— NO puede producir «0 holds». Cero es una afirmación
    y aquí no se sabe. El estado sin casilla no aterriza en el lado tranquilizador."""
    s, c, H = _cli(tmp_path, monkeypatch)
    c.post("/vigilancia/ack", headers=H, params={"quien": "watcher"})
    h = _vig(c, s)["holds"]
    assert h["n"] is None, f"inventó un recuento: {h}"
    assert h["declarados"] is False


def test_un_ack_NUEVO_sin_holds_borra_los_viejos(tmp_path, monkeypatch):
    """⊖ el que evita el peor caso: si los holds sobrevivieran al ack que no los declara,
    un watcher que deja de contarlos dejaría el último número congelado para siempre — un
    dato muerto con aspecto de vivo."""
    s, c, H = _cli(tmp_path, monkeypatch)
    c.post("/vigilancia/ack", headers=H, params={"quien": "watcher", "holds": 9})
    assert _vig(c, s)["holds"]["n"] == 9
    c.post("/vigilancia/ack", headers=H, params={"quien": "watcher"})
    h = _vig(c, s)["holds"]
    assert h["n"] is None and h["declarados"] is False, f"quedó congelado: {h}"
