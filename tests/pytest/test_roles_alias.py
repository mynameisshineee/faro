"""B — la fuente del censo pasa a ser roles-por-alias.json cuando está montado.

`canon_identidad()` (ledger_parse.py) prefiere `rol_por_alias` a `roster.json`
cuando `LLMINBOX_ROLES_ALIAS` apunta a un fichero legible. `sin_rol` NO veta:
significa "sin rol que agrupe", no "sin bandeja" — falsado contra el índice
vivo (bikeus tiene cursores activos y está en sin_rol). Sin montar, degrada a
la conducta de hoy (sólo roster.json) — sin fail-open.

Cada test afirmativo lleva su falsador (T1): qué se vería si la fuente FIRMADA
no se estuviera consultando de verdad.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from .conftest import construir

# 'solo-en-alias' NO existe en el ROSTER de prueba (conftest.ROSTER): es
# justo el falsador exacto del reviewer — un alta firmada en
# roles-por-alias.json sin tocar roster.json.
ROLES_ALIAS_FIXTURE = {
    "rol_por_alias": {
        "backend": "be",
        "backend-biklabs": "be",
        "solo-en-alias": "be",
        # Valor en MAYÚSCULAS a propósito (re-review×3, qa): el fichero firmado
        # se edita a mano fuera de este repo — la normalización tiene que
        # tragárselo, o un "FE" real dejaría ese rol sin matchear en silencio.
        "front": "FE",
    },
    # 'herramienta-tool' NO está en el ROSTER de prueba; 'cto-A' SÍ (con rol).
    # La pareja reproduce los dos lados del caso `sin_rol` real: un nombre que
    # sólo vive en sin_rol no resuelve (no es parte de la unión), y uno que
    # además está en roster sigue resolviendo (el caso bikeus del índice vivo).
    "sin_rol": {
        "TOOL, no agente — subagente invocable": ["herramienta-tool"],
        "sesion/ventana, no rol": ["cto-A"],
    },
}


def _con_roles_alias(tmp_path, monkeypatch):
    fich = tmp_path / "roles-por-alias.json"
    fich.write_text(json.dumps(ROLES_ALIAS_FIXTURE))
    return construir(tmp_path, monkeypatch,
                      extra_env={"LLMINBOX_ROLES_ALIAS": str(fich)})


def test_alias_solo_en_roles_por_alias_resuelve_cuando_montado(tmp_path, monkeypatch):
    """FALSADOR: si canon_identidad() siguiera derivando su unión sólo de
    roster.json, esto seguiría dando 422 pese al fichero montado — 'solo-en-
    alias' no está en el ROSTER de prueba a propósito.
    """
    s = _con_roles_alias(tmp_path, monkeypatch)
    with TestClient(s.app) as c:
        s.barrido()
        c.headers.update({"X-Llminbox-Token": "test-token"})
        r = c.get("/inbox/solo-en-alias")
        assert r.status_code == 200


def test_nombre_solo_en_sin_rol_no_resuelve(tmp_path, monkeypatch):
    """`sin_rol` no es parte de la unión resolvente: un nombre que SÓLO vive
    ahí ('herramienta-tool', ausente del ROSTER de prueba) no resuelve — igual
    que cualquier desconocido.

    FALSADOR: si `_cargar_roles_por_alias()` metiera los nombres de sin_rol en
    la unión, esto daría 200 y un TOOL sin bandeja abriría cursor propio.
    """
    s = _con_roles_alias(tmp_path, monkeypatch)
    with TestClient(s.app) as c:
        s.barrido()
        c.headers.update({"X-Llminbox-Token": "test-token"})
        r = c.get("/inbox/herramienta-tool")
        assert r.status_code == 422
        assert "herramienta-tool" in r.json()["detail"]


def test_sin_rol_no_veta_a_quien_esta_en_roster(tmp_path, monkeypatch):
    """El caso bikeus: 'cto-A' aparece en `sin_rol` del fichero montado Y en el
    roster como agente. Debe seguir resolviendo — `sin_rol` significa "sin rol
    que agrupe", no "sin bandeja".

    FALSADOR (medido en el índice vivo, 2026-08-10): un veto sin_rol-primero
    daría 422 aquí, exactamente lo que habría cortado la bandeja de `bikeus`
    (cursores activos en 2 ledgers) al desplegar.
    """
    s = _con_roles_alias(tmp_path, monkeypatch)
    with TestClient(s.app) as c:
        s.barrido()
        c.headers.update({"X-Llminbox-Token": "test-token"})
        r = c.get("/inbox/cto-A")
        assert r.status_code == 200


def test_fichero_ausente_cae_a_conducta_roster_actual(cliente):
    """Sin LLMINBOX_ROLES_ALIAS (conducta por defecto de la fixture `cliente`),
    un alias que sólo existiría en roles-por-alias.json NO resuelve — la misma
    respuesta que daba el servicio antes de este fix, sin fail-open por el
    fichero ausente.

    FALSADOR: si esto diera 200, el fichero ausente estaría fallando ABIERTO
    en vez de degradar a roster.json — justo lo que el spec prohíbe.
    """
    r = cliente.get("/inbox/solo-en-alias")
    assert r.status_code == 422


def test_alias_de_roster_sigue_resolviendo_con_fichero_montado(tmp_path, monkeypatch):
    """Consistencia: con el fichero montado, los alias que YA resolvían por
    roster.json ('backend', declarado también en rol_por_alias con el mismo
    rol) siguen resolviendo — la unión no resta identidades que hoy existen.
    """
    s = _con_roles_alias(tmp_path, monkeypatch)
    with TestClient(s.app) as c:
        s.barrido()
        c.headers.update({"X-Llminbox-Token": "test-token"})
        r = c.get("/inbox/backend")
        assert r.status_code == 200


def test_rol_pelado_de_roster_sigue_resolviendo_montado(tmp_path, monkeypatch):
    """El MAJOR del re-review×3: montar el fichero firmado tiene que AÑADIR
    identidades, nunca quitarlas. 'cto' es rol en el ROSTER de prueba
    (cto-A → cto) pero NO aparece como valor en ROLES_ALIAS_FIXTURE — con la
    unión rota, /inbox/cto pasaba de 200 (sin montar) a 422 (montado), y la
    migración ② escribe cursores justo bajo esos tokens vía rol_de().

    FALSADOR: quitar la cláusula `bajo in ROLES_VALIDOS` de la rama montada de
    canon_identidad() devuelve el 422 y esto se pone rojo.
    """
    s = _con_roles_alias(tmp_path, monkeypatch)
    with TestClient(s.app) as c:
        s.barrido()
        c.headers.update({"X-Llminbox-Token": "test-token"})
        r = c.get("/inbox/cto")
        assert r.status_code == 200


def test_valor_con_mayusculas_normaliza(tmp_path, monkeypatch):
    """'front' → 'FE' en el fichero (mayúsculas de quien edita a mano): tanto
    el alias como el token de rol pelado 'fe' deben resolver — la comparación
    es contra valores ya normalizados.

    FALSADOR: sin el .lower() de los valores en _cargar_roles_por_alias(),
    'fe' no está en values() (está 'FE') y da 422.
    """
    s = _con_roles_alias(tmp_path, monkeypatch)
    with TestClient(s.app) as c:
        s.barrido()
        c.headers.update({"X-Llminbox-Token": "test-token"})
        assert c.get("/inbox/front").status_code == 200
        assert c.get("/inbox/fe").status_code == 200


def test_inbox_montado_muestra_contenido(tmp_path, monkeypatch):
    """La lección del falsador vivo post-deploy (2026-08-10): con el fichero
    firmado montado, /inbox/backend devolvía 200 con bandeja VACÍA — la
    resolución colapsaba 'backend' a su rol 'be' ANTES de mirar CANON, y
    escuchados('be') no casa con lo que el parser escribió en `recipients`
    ('backend'). Toda la suite pasaba porque nadie asertaba CONTENIDO.

    FALSADOR: reordenar la rama montada (ROLES_ALIAS antes que CANON) devuelve
    la bandeja vacía y esto se pone rojo — un 200 sin cuerpo ya no cuela.
    """
    s = _con_roles_alias(tmp_path, monkeypatch)
    with TestClient(s.app) as c:
        s.barrido()
        c.headers.update({"X-Llminbox-Token": "test-token"})
        r = c.get("/inbox/backend")
        assert r.status_code == 200
        assert "primera" in r.text, (
            "bandeja vacía para un alias con correo pendiente — la resolución "
            "está colapsando el nombre a rol antes de tiempo")
