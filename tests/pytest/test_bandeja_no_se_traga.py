"""Drenar la bandeja no puede enterrar lo que nunca se enseñó.

`/inbox` sirve `ORDER BY arrival DESC LIMIT n` —las MÁS NUEVAS— y devolvía como
`hasta` la MÁS VIEJA DE LAS MOSTRADAS. Como el cursor filtra por `arrival > last`,
todo lo que quedaba POR DEBAJO caía fuera del filtro para siempre.

Medido en la flota el 2026-08-29 por `harness`: 8.859 usos de la vía vulnerable en
14 de los 16 agentes. Y el caso real ya estaba documentado en `ledger-vigia.sh:483`
desde el 21 de agosto — «enseñó 25 de 102, se tragó 77».
"""
from __future__ import annotations

import json
import re

from fastapi.testclient import TestClient

from .conftest import construir

H = {"X-Llminbox-Token": "test-token"}


def indexar(s):
    """El arnés anula el vigilante, así que sin esto la bandeja sale VACÍA y los dos
    tests pasarían por la razón equivocada — la misma trampa que el ⊖ destapó en
    `test_salud_no_miente.py`. Se barre a mano lo que el vigilante barrería.

    Va DENTRO del `TestClient`: el esquema lo crea el arranque de la app, así que
    fuera del contexto no existe ni la tabla `entries`."""
    from .conftest import db_directa
    con = db_directa(s)
    for nombre, ruta in s.LEDGERS.items():
        s.reindex(nombre, ruta, con)
    con.commit()
    con.close()


def hasta_de(texto):
    """El `{"hasta":{...}}` que el propio servicio manda pegar — es lo que el CLI
    copia, así que probar otra cosa sería probar un camino que nadie recorre."""
    linea = [x.strip() for x in texto.splitlines() if x.strip().startswith('{"hasta"')][-1]
    return json.loads(linea)["hasta"]


def test_no_entierra_lo_no_mostrado(tmp_path, monkeypatch):
    """FALSADOR: pedir menos de lo pendiente, drenar con lo que dice el servicio, y
    exigir que lo NO mostrado SIGA en la bandeja. Si desaparece, se ha perdido.
    """
    s = construir(tmp_path, monkeypatch)
    with TestClient(s.app) as c:
        indexar(s)
        params = {"limit": 1, "only": "demo-ledger"}
        primera = c.get("/inbox/backend", params=params, headers=H).text
        assert "1 de 2 para ti" in primera, "el montaje no reproduce el caso (2 pendientes, 1 mostrada)"
        r = c.post("/inbox/backend/leido", json={"hasta": hasta_de(primera)}, headers=H)
        assert r.status_code == 200
        segunda = c.get("/inbox/backend", params={"limit": 30, "only": "demo-ledger"}, headers=H).text
    assert "(nada nuevo" not in segunda, "la bandeja quedó vacía: se tragó lo que no enseñó"
    assert "para ti" in segunda


def test_drenaje_completo_si_cabe_todo(tmp_path, monkeypatch):
    """⊖ CONTROL — sin él, «no avanzar nunca» también pasaría el test de arriba y
    tendríamos un cursor que no drena, que es el defecto opuesto y peor.
    """
    s = construir(tmp_path, monkeypatch)
    with TestClient(s.app) as c:
        indexar(s)
        todo = c.get("/inbox/backend", params={"limit": 30, "only": "demo-ledger"}, headers=H).text
        c.post("/inbox/backend/leido", json={"hasta": hasta_de(todo)}, headers=H)
        despues = c.get("/inbox/backend", params={"limit": 30, "only": "demo-ledger"}, headers=H).text
    assert "(nada nuevo" in despues, "mostrándolo todo y drenando, la bandeja debe quedar limpia"


def test_la_bandeja_parcial_se_puede_drenar(tmp_path, monkeypatch):
    """FALSADOR (lo señaló CodeRabbit en la revisión de esta misma PR): no basta con
    NO tragar. Si el cursor no avanza y nada dice cómo avanzarlo, la entrada vieja no
    aparece JAMÁS — se cambia pérdida silenciosa por atasco permanente, que es mejor
    y sigue sin ser correcto.

    No se puede servir «lo más nuevo primero» Y drenar hacia atrás con un cursor que
    sólo avanza: son incompatibles. El orden DESC es deliberado y tiene un incidente
    detrás («vi la bandeja de un humano con 30.246 mensajes empezando por junio»), así
    que lo que se arregla no es el orden: es que el atasco sea SALIDA DECLARADA.

    Se comprueba end-to-end: el rótulo dice el límite exacto, y con ese límite drena.
    """
    s = construir(tmp_path, monkeypatch)
    with TestClient(s.app) as c:
        indexar(s)
        todas = c.get("/inbox/backend", params={"limit": 30, "only": "demo-ledger"}, headers=H).text
        eids = [x.split()[0] for x in todas.splitlines()
                if x.startswith("  ") and len(x.split()) > 3 and len(x.split()[0]) == 12]
        parcial = c.get("/inbox/backend", params={"limit": 1, "only": "demo-ledger"}, headers=H).text
        # La que el límite deja fuera: la que estaba y ya no está.
        oculta_eid = next((e for e in eids if e not in parcial), None)
        # ── EL RÓTULO CAMBIÓ PORQUE CAMBIÓ LO QUE HACE EL CÓDIGO ────────────────
        # Antes decía «NO SE DRENA CON ESTE LÍMITE», y era EXACTO: el cursor no
        # avanzaba si no cabía todo. Desde que avanza siempre hasta la última servida,
        # ese texto miente hacia el lado caro —desanima justo la acción que funciona—,
        # así que la aserción se re-apunta al mensaje de hoy.
        #
        # Lo que NO cambia es la invariante que este test existe para proteger, y es la
        # de abajo: SIGUIENDO LO QUE DICE EL RÓTULO, la bandeja se vacía. Eso sigue
        # comprobándose end-to-end y es lo que impide que el mensaje vuelva a ser prosa.
        assert "SE DRENA POR PARTES" in parcial, (
            "el rótulo no dice cómo drenar: un atasco sin salida declarada era el "
            "defecto original, y un rótulo que miente al revés es el nuevo")
        # ANCLADO, no subcadena: «limit=2» casa también con «limit=20» y la aserción
        # pasaría con un número equivocado. Lo señaló CodeRabbit y tiene razón.
        assert re.search(r"limit=2\b", parcial), (
            "el límite que ofrece no es el que dice — y «limit=2» a secas casaría "
            "también con «limit=20», que era el fallo señalado")
        # Y siguiendo lo que dice, drena de verdad.
        c.post("/inbox/backend/leido", json={"hasta": hasta_de(parcial)}, headers=H)
        completo = c.get("/inbox/backend", params={"limit": 2, "only": "demo-ledger"}, headers=H).text
        c.post("/inbox/backend/leido", json={"hasta": hasta_de(completo)}, headers=H)
        final = c.get("/inbox/backend", params={"limit": 30, "only": "demo-ledger"}, headers=H).text
    assert "(nada nuevo" in final, "siguiendo la instrucción del rótulo, la bandeja debe vaciarse"
    # Y LA ENTRADA OMITIDA TIENE QUE HABERSE VISTO, no sólo haber desaparecido: sin
    # esto, un arreglo que la BORRARA en vez de mostrarla pasaría el test igual.
    assert oculta_eid and oculta_eid in completo, (
        f"la entrada que se había ocultado ({oculta_eid}) nunca llegó a mostrarse")
