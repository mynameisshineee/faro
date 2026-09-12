"""El EMPAREJADO resuelve alias→rol igual que el cursor — o hay correo que un
alias no ve NUNCA (infra, MARK:infra-el-cursor-va-por-rol-y-el-emparejado-por-
alias-y-en-medio-caen-mensajes-que-nadie-ve, 2026-08-16; daño confirmado por
cto: 3 pendientes por el alias estrecho, 6 por el ancho, subconjunto estricto).

La asimetría medida: `cursors` colapsa por rol (migrar_alias_a_rol) pero el
match de `/inbox` compara `recipients.who` (alias EN CRUDO del texto) contra
`escuchados(agent)` (literal+escuchas, sin hermanos de rol). Drenar por un
alias adelanta el cursor del ROL por encima de entradas que sólo se ven desde
el otro alias: 200, contador baja, nadie ve un error.

La cura es UNA capa (la que infra nombró): `escuchados()` expande cada nombre
por `firmas_del_rol(rol)` — la función que ya existía para esto y que
`escuchados_autor()` usa desde el 08-13. Fail-open en la dirección buena: un
rol ve MÁS de su propio correo, jamás menos, y jamás el de otro rol.

Falsadores por brazo (qué se vería si la cura no estuviera): los dos ⊕ dan
lista vacía HOY — este fichero nació ROJO y ese rojo es la medida del defecto.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import construir

# El ROSTER fijo del arnés ya trae el par exacto del defecto real:
# `backend` y `backend-biklabs`, ambos rol `be` — y DEMO-LEDGER trae dos
# entradas dirigidas «→ backend» (ninguna a backend-biklabs).


def test_alias_hermano_ve_el_correo_del_rol(tmp_path, monkeypatch):
    """⊕ /inbox/backend-biklabs ve lo dirigido a `backend` (mismo rol `be`).

    Falsador: sin expansión por rol, el match es literal sobre
    recipients.who='backend' y la bandeja del hermano sale VACÍA — el correo
    existe, su cursor (compartido por rol) avanzará por encima, y nadie ve
    un error. Exactamente lo que cto pagó el 16-ago.
    """
    servicio = construir(tmp_path, monkeypatch)
    with TestClient(servicio.app) as c:
        servicio.barrido()
        r = c.get("/inbox/backend-biklabs", headers={"X-Llminbox-Token": "test-token"})
        assert r.status_code == 200
        assert "primera" in r.text and "segunda" in r.text


def test_el_alias_ancho_sigue_viendo_lo_suyo(tmp_path, monkeypatch):
    """⊕ control: /inbox/backend sigue viendo sus dos entradas (la expansión
    no puede COSTARLE correo al alias que ya funcionaba)."""
    servicio = construir(tmp_path, monkeypatch)
    with TestClient(servicio.app) as c:
        servicio.barrido()
        r = c.get("/inbox/backend", headers={"X-Llminbox-Token": "test-token"})
        assert r.status_code == 200
        assert "primera" in r.text and "segunda" in r.text


def test_la_expansion_no_cruza_roles(tmp_path, monkeypatch):
    """⊖ control de fuga: /inbox/cto-A (rol `cto`) NO ve el correo de `be`.

    Falsador del brazo: si la expansión se hiciera por censo entero en vez de
    por rol, el correo de backend aparecería aquí — más entrega no puede
    significar entrega CRUZADA. (cto-A es AUTOR de las entradas del fixture,
    no destinatario: si apareciera por la rama de suscripción-por-autor sería
    otro mecanismo, y el fixture no suscribe a nadie.)
    """
    servicio = construir(tmp_path, monkeypatch)
    with TestClient(servicio.app) as c:
        servicio.barrido()
        r = c.get("/inbox/cto-A", headers={"X-Llminbox-Token": "test-token"})
        assert r.status_code == 200
        assert "primera" not in r.text and "segunda" not in r.text
