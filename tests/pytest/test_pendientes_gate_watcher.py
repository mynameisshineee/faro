"""C5 · `/pendientes` exige el WATCHER_TOKEN **ADEMÁS** del compartido.

SUMA, no sustituye: la orden decía «en vez del token compartido» y escrito así quitaba
una capa. Lo cazó un test preexistente (6f375f6) y el precedente del vecino —
`/vigilancia/ack` lleva `GATE` Y el watcher—: el endpoint que agrega TODOS los inboxes
no puede acabar con menos puerta que el de al lado.

No cierra una exposición nueva: un portador del token compartido YA podía enumerar
inboxes uno a uno. Lo que hace es quitarle el atajo — y se hace HOY porque el endpoint
todavía no está desplegado (`:8077/pendientes` -> 404), o sea que cambiar la credencial
es editar un fichero. Después del cutover sería una migración con ~70 sesiones vivas.

FRONTERA DECLARADA Y NO ARREGLADA HOY: el servidor no impone la frontera de carril —
`agent` es parámetro de URL y hay UNA credencial, así que cualquier portador lee el
inbox de cualquiera. Es condición PREEXISTENTE; no la introduce este cambio ni la cierra.
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from .conftest import construir

WTOKEN = "token-solo-del-watcher"


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """El token compartido lo IMPONE el conftest (`test-token`) y pisa cualquier
    `setenv` mío. Lo leo de `s.TOKEN` en vez de fijarlo: mi primera versión mandaba un
    token inventado, así que el 401 llegaba por «token equivocado» y no por «esta puerta
    ya no la abre el GATE» — habría pasado en verde con el cambio hecho, sin hacer, o
    roto. Un test que no puede fallar."""
    monkeypatch.setenv("LLMINBOX_WATCHER_TOKEN", WTOKEN)
    s = construir(tmp_path, monkeypatch)
    con = s.db(); s._preparar_indice(con); con.close()
    return TestClient(s.app), s


def test_con_LAS_DOS_credenciales_pasa(cli):
    """⊕ obligatorio. Sin él, un endpoint que devuelve 401 a TODO el mundo pasaría los
    ⊖ de abajo y firmaría verde por mudez."""
    c, s = cli
    r = c.get("/pendientes", headers={"X-Llminbox-Token": s.TOKEN,
                                      "X-Llminbox-Watcher": WTOKEN})
    assert r.status_code == 200, f"el watcher acreditado no pasa: {r.status_code} {r.text[:120]}"
    assert r.json().get("contrato") == "pendientes/2"


def test_solo_con_el_token_compartido_NO_pasa(cli):
    """El corazón del cambio: el GATE de la flota deja de abrir esta puerta."""
    c, s = cli
    r = c.get("/pendientes", headers={"X-Llminbox-Token": s.TOKEN})
    assert r.status_code == 401, (
        "el token compartido sigue abriendo /pendientes: el cambio no muerde")


def test_sin_credencial_ninguna_NO_pasa(cli):
    c, _ = cli
    assert c.get("/pendientes").status_code == 401


def test_un_watcher_token_equivocado_NO_pasa(cli):
    c, s = cli
    r = c.get("/pendientes", headers={"X-Llminbox-Token": s.TOKEN,
                                      "X-Llminbox-Watcher": "no-es"})
    assert r.status_code == 401


def test_sin_WATCHER_TOKEN_configurado_cierra_no_abre(tmp_path, monkeypatch):
    """FAIL-CLOSED, y la consecuencia declarada: si nadie exporta el token, `/pendientes`
    responde 401 PARA SIEMPRE. Es lo correcto —abrir sería peor— pero es una forma de
    quedarse inerte, así que queda fijado por prueba y dicho en voz alta, no descubierto
    por un watcher que un día deja de recibir trabajo sin que nada lo cante."""
    monkeypatch.delenv("LLMINBOX_WATCHER_TOKEN", raising=False)
    s = construir(tmp_path, monkeypatch)
    con = s.db(); s._preparar_indice(con); con.close()
    c = TestClient(s.app)
    assert c.get("/pendientes", headers={"X-Llminbox-Token": s.TOKEN,
                                        "X-Llminbox-Watcher": ""}).status_code == 401
    assert c.get("/pendientes", headers={"X-Llminbox-Token": s.TOKEN}).status_code == 401


def test_el_cambio_NO_toca_los_demas_endpoints(cli):
    """⊖ del alcance: la credencial nueva es SÓLO para /pendientes."""
    c, s = cli
    for ruta in ("/entries", "/stat", "/doctor"):
        r = c.get(ruta, headers={"X-Llminbox-Token": s.TOKEN})
        assert r.status_code == 200, (
            f"{ruta} ya no sirve con el token compartido: {r.status_code}. "
            f"(`!= 401` dejaría pasar un 500 y firmaría verde sobre un endpoint roto)")
        r2 = c.get(ruta, headers={"X-Llminbox-Watcher": WTOKEN})
        assert r2.status_code == 401, (
            f"{ruta} acepta el token de watcher SOLO y no debería: {r2.status_code}")


def test_pendientes_NO_late(cli):
    """④ · autenticar no puede acreditar un ciclo. Si el GET vuelve a refrescar el
    latido reintroducimos C1 entero: el hombre muerto que cualquiera mantiene vivo."""
    c, s = cli
    antes = c.get("/health").json()["vigilancia"]
    for _ in range(3):
        c.get("/pendientes", headers={"X-Llminbox-Token": s.TOKEN,
                                      "X-Llminbox-Watcher": WTOKEN})
    despues = c.get("/health").json()["vigilancia"]
    assert despues["estado"] == antes["estado"], "leer /pendientes movió el estado de vigilancia"
    assert despues.get("hace_s") == antes.get("hace_s"), "‼ el GET late: C1 reintroducido"

    # ⊕ OBLIGATORIO: sin esto, un `/health` que no sabe registrar NINGÚN latido pasa el
    # ⊖ de arriba por mudez y firma verde. Hay que probar que el camino que SÍ debe
    # mover el estado lo mueve.
    ack = c.post("/vigilancia/ack", headers={"X-Llminbox-Token": s.TOKEN,
                                             "X-Llminbox-Watcher": WTOKEN},
                 params={"quien": "watcher"})
    assert ack.status_code == 200, f"el ⊕ no puede correr: el ack falla ({ack.status_code})"
    assert ack.json()["latido"] is True
    tras_ack = c.get("/health").json()["vigilancia"]
    assert tras_ack["estado"] != antes["estado"] or tras_ack.get("hace_s") != antes.get("hace_s"), (
        "el POST tampoco mueve nada: /health está mudo y el ⊖ de arriba no medía")


def test_el_watcher_SOLO_no_basta(cli):
    """⊖ QUE FALTABA EN ESTE FICHERO, y lo destapó `tests/mutantes_canon.py`: quitar el
    GATE de `PUERTA_WATCHER` dejaba pasar al watcher sin el token de casa, y **este
    fichero seguía verde**. La mitad que lo cazaba vivía en `test_pendientes_y_hombre_
    muerto.py`, o sea que el fichero que DECLARA el contrato de C5 no lo cubría entero.

    Importa dónde vive la cobertura, no sólo que exista: quien venga a cambiar esta
    puerta abrirá este fichero, y un verde aquí se lee como «la puerta está probada».
    """
    c, s = cli
    r = c.get("/pendientes", headers={"X-Llminbox-Watcher": WTOKEN})
    assert r.status_code == 401, (
        f"el watcher solo entró ({r.status_code}): la suma es GATE + watcher, y sin el "
        f"token de casa esto es una puerta menos")

