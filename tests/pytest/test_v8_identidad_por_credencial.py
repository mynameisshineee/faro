"""V8 — la identidad sale de la CREDENCIAL, no de la URL ni del payload.

Hasta hoy el servicio no tenía principal: `agent` era un parámetro que ponía quien
llamaba, así que cualquier portador del token compartido podía actuar como cualquiera.
Estaba declarado y adjudicado en el docstring de `auth()` — y el 2026-09-04 el operador
reabrió la premisa y dio el go a construirlo.

QUÉ SE CIERRA, y es lo que de verdad está vivo hoy (no la escritura de ledger, que en
este despliegue no pasa por el servicio: los 13 montajes van `:ro` y `/append` contesta
503 antes de hacer nada):

    POST /inbox/{agent}/ack      grant + sujeto URL    → intento vaciar bandeja ajena
    POST /claim                  sujeto en el payload  → cojo trabajo como otro
    POST /claim/cierro           sujeto en el payload  → cierro el claim de otro
    POST /vigilancia/ack         sujeto en el payload  → acuso el latido por otro

LEER sigue abierto a propósito; el ACK V8 exige grant y `/leido` queda sólo legacy.

POR ROL, NO POR FIRMA. El censo tiene 51 nombres para 27 roles, así que una credencial
atada a la firma se esquivaría firmando con otro alias del mismo rol — el mismo motivo
por el que el tope por owner se indexa por rol.

LA FASE 1 NO ES UNA PERILLA. No existe variable que apague el 403 de quien SÍ tiene
credencial. Lo que hay es que quien todavía usa el token compartido no tiene identidad
que comprobar: se le ANOTA y se le sirve, para que la migración vaya agente a agente y
no tumbe las ~71 sesiones el día que infra deje el fichero. Ese hueco se dice en voz
alta y se cierra en la fase 2, que es código y no configuración.
"""
from __future__ import annotations

import json
import hashlib
import os
import sqlite3
from pathlib import Path

import pytest

CRED_BE = "cred-be-demo-0000000000"
CRED_CTO = "cred-cto-demo-000000000"
SECRETO_EN_METADATO = "cred-super-secreta-0123456789"


def _assert_credencial_fuera_de_salidas(capsys, error, credencial):
    """El error de arranque acaba en los logs del contenedor: no basta con
    ocultar el token completo; tampoco puede sobrevivir el prefijo/sufijo que
    antes se usaba para identificarlo."""
    capturado = capsys.readouterr()
    salida = f"{error}\n{capturado.out}\n{capturado.err}"
    for fragmento in (credencial, credencial[:8], credencial[-8:]):
        assert fragmento not in salida, (
            f"la salida de error filtra un fragmento de la credencial: {fragmento!r}")


def _mapa(tmp_path, contenido=None):
    p = tmp_path / "credenciales.json"
    p.write_text(json.dumps(contenido if contenido is not None else {
        CRED_BE: {"rol": "be", "carril": "demo"},
        CRED_CTO: {"rol": "cto", "carril": "demo"},
    }))
    return str(p)


def _configura_mapa(monkeypatch, ruta):
    """Las pruebas de identidad usan un mapa verificado; las de integridad ausente
    viven en su banco específico y no deben heredar esta comodidad."""
    p = Path(ruta)
    monkeypatch.setenv("LLMINBOX_CREDENCIALES", str(p))
    monkeypatch.setenv(
        "LLMINBOX_CREDENCIALES_SHA",
        hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else "0" * 64,
    )


def _con():
    c = sqlite3.connect(os.environ["LLMINBOX_DB"])
    c.row_factory = sqlite3.Row
    return c


def _cliente(servicio_mod):
    from fastapi.testclient import TestClient
    c = TestClient(servicio_mod.app)
    c.__enter__()
    servicio_mod.barrido()
    c.headers.update({"X-Llminbox-Token": "test-token"})
    return c


# ── ⊕ EL CONTROL QUE MÁS IMPORTA: sin mapa, NADA cambia ────────────────────────
def test_sin_mapa_de_credenciales_todo_sigue_como_hoy(cliente):
    """Si esto falla, V8 tumbó la flota antes de emitir una sola credencial.

    Va el primero a propósito: los ⊖ de abajo prueban que el 403 existe, y éste
    prueba que no existe donde no debe.
    """
    r = cliente.post("/claim", json={"tema": "t1", "agent": "backend", "rol": "ejecuta"})
    assert r.status_code == 200, r.text
    assert cliente.post("/inbox/cto-A/leido", json={"hasta": {}}).status_code == 200


# ── ⊖ CON MAPA: la credencial de un rol no puede actuar como otro ─────────────
def test_una_credencial_no_puede_coger_trabajo_como_otro(servicio, tmp_path, monkeypatch):
    _configura_mapa(monkeypatch, _mapa(tmp_path))
    from .conftest import construir
    s = construir(tmp_path, monkeypatch)
    c = _cliente(s)

    # ⊕ control: con SU rol, entra y deja fila
    ok = c.post("/claim", json={"tema": "mio", "agent": "backend", "rol": "ejecuta"},
                headers={"X-Llminbox-Token": CRED_BE})
    assert ok.status_code == 200, ok.text
    assert _con().execute("SELECT COUNT(*) c FROM claims").fetchone()["c"] == 1

    # ⊖ suplantando a otro rol: 403 Y CERO filas nuevas (la tabla, no el código)
    mal = c.post("/claim", json={"tema": "ajeno", "agent": "cto-A", "rol": "ejecuta"},
                 headers={"X-Llminbox-Token": CRED_BE})
    assert mal.status_code == 403, mal.text
    assert _con().execute("SELECT COUNT(*) c FROM claims").fetchone()["c"] == 1, (
        "el 403 llegó pero la fila se escribió igual")


def test_una_credencial_no_puede_mover_el_cursor_de_otro(servicio, tmp_path, monkeypatch):
    _configura_mapa(monkeypatch, _mapa(tmp_path))
    from .conftest import construir
    s = construir(tmp_path, monkeypatch)
    c = _cliente(s)

    def cursor_de(a):
        # REKEY (sdet #1308, Clase B): el ack V8 escribe la clave NUEVA en
        # `cursors_v2`; la v1 queda congelada. La aserción mira AMBOS almacenes:
        # v2 manda si existe (es lo que el camino V8 escribe), si no v1.
        f = _con().execute("SELECT last_arrival FROM cursors WHERE agent=?", (a,)).fetchone()
        v1 = f["last_arrival"] if f else None
        g = _con().execute(
            "SELECT last_arrival FROM cursors_v2 "
            "WHERE role=? AND carril='demo' AND ledger='demo-ledger'",
            (a,)).fetchone()
        return g["last_arrival"] if g else v1

    mal = c.post("/inbox/cto-A/leido", json={"hasta": {"demo-ledger": 1}},
                 headers={"X-Llminbox-Token": CRED_BE})
    assert mal.status_code == 403, mal.text
    assert cursor_de("cto") is None, "el cursor ajeno se movió pese al 403"

    h = {"X-Llminbox-Token": CRED_BE, "X-Llminbox-Carril": "demo"}
    texto = c.get("/inbox/backend", params={"only": "demo-ledger"}, headers=h).text
    sobre = json.loads([x.strip() for x in texto.splitlines()
                        if x.strip().startswith('{"hasta"')][-1])
    ok = c.post("/inbox/backend/ack",
                json={"grant": sobre["ack"], "arrival": sobre["ack"]["watermark"]},
                headers=h)
    assert ok.status_code == 200, ok.text
    assert cursor_de("be") == 1, "el 403 se comió también lo legítimo"


def test_una_credencial_no_puede_cerrar_el_claim_de_otro(servicio, tmp_path, monkeypatch):
    _configura_mapa(monkeypatch, _mapa(tmp_path))
    from .conftest import construir
    s = construir(tmp_path, monkeypatch)
    c = _cliente(s)
    c.post("/claim", json={"tema": "suyo", "agent": "cto-A", "rol": "ejecuta"},
           headers={"X-Llminbox-Token": CRED_CTO})
    mal = c.post("/claim/cierro", json={"tema": "suyo", "agent": "cto-A"},
                 headers={"X-Llminbox-Token": CRED_BE})
    assert mal.status_code == 403, mal.text
    assert _con().execute(
        "SELECT cerrado FROM claims WHERE tema='suyo'").fetchone()["cerrado"] is None, (
        "el claim ajeno quedó cerrado pese al 403")


# ── ⊖ POR ROL, NO POR FIRMA ───────────────────────────────────────────────────
def test_la_credencial_ata_el_ROL_no_la_firma(servicio, tmp_path, monkeypatch):
    """`backend` y `backend-biklabs` son DOS firmas del MISMO rol `be`. Si la
    credencial atara la firma, la de `backend` no valdría para `backend-biklabs` y
    el enforcement sería un letrero que se esquiva cambiando de alias."""
    _configura_mapa(monkeypatch, _mapa(tmp_path))
    from .conftest import construir
    s = construir(tmp_path, monkeypatch)
    c = _cliente(s)
    r = c.post("/claim", json={"tema": "otro-alias", "agent": "backend-biklabs",
                               "rol": "ejecuta"},
               headers={"X-Llminbox-Token": CRED_BE})
    assert r.status_code == 200, (
        f"la credencial de `be` no reconoce otra firma del mismo rol: {r.text}")


# ── ⊕ FASE 1: el token compartido se ANOTA y se sirve, no se tumba ────────────
def test_el_token_compartido_sigue_sirviendo_y_queda_anotado(servicio, tmp_path,
                                                             monkeypatch):
    """El hueco de la fase 1, declarado y MEDIDO — no disfrazado de fase.

    Si esto diera 403, el día que infra deje el fichero con emisión parcial se caen
    todas las sesiones que aún no tienen credencial.
    """
    _configura_mapa(monkeypatch, _mapa(tmp_path))
    from .conftest import construir
    s = construir(tmp_path, monkeypatch)
    c = _cliente(s)
    r = c.post("/claim", json={"tema": "viejo", "agent": "cto-A", "rol": "ejecuta"})
    assert r.status_code == 200, r.text          # token compartido: sirve
    n = _con().execute("SELECT COUNT(*) c FROM v8_anon").fetchone()["c"]
    assert n == 1, f"la llamada sin identidad no quedó anotada ({n})"


def test_health_publica_la_cobertura(servicio, tmp_path, monkeypatch):
    """La fase 2 se dispara con un número, no con una sensación."""
    _configura_mapa(monkeypatch, _mapa(tmp_path))
    from .conftest import construir
    s = construir(tmp_path, monkeypatch)
    c = _cliente(s)
    c.post("/claim", json={"tema": "x", "agent": "cto-A", "rol": "ejecuta"})
    v8 = c.get("/health").json()["v8"]
    assert v8["credenciales"] == 2 and v8["hay_mapa"] is True
    assert v8["sin_identidad_24h"] == 1
    assert v8["roles_cubiertos"] == 2 and v8["roles_emisibles"] >= 2


# ── ⊖ EL ARRANQUE: un mapa medio cargado es peor que ninguno ──────────────────
@pytest.mark.parametrize("contenido,trozo", [
    ({CRED_BE: {"rol": "no-existe", "carril": "demo"}}, "no resuelve"),
    ({CRED_BE: {"rol": "be", "carril": "inventado"}}, "carril"),
    ({CRED_BE: {"carril": "demo"}}, "rol"),
    ("[]", "objeto"),
])
def test_un_mapa_invalido_no_arranca(tmp_path, monkeypatch, capsys, contenido, trozo):
    """Presente e inválido ⇒ ruido y parada. Un mapa medio cargado autentica a unos
    y a otros no, y el segundo grupo se cree protegido."""
    p = tmp_path / "credenciales.json"
    p.write_text(contenido if isinstance(contenido, str) else json.dumps(contenido))
    _configura_mapa(monkeypatch, p)
    from .conftest import construir
    with pytest.raises(SystemExit) as e:
        construir(tmp_path, monkeypatch)
    assert trozo in str(e.value), str(e.value)
    if isinstance(contenido, dict) and CRED_BE in contenido:
        _assert_credencial_fuera_de_salidas(capsys, e.value, CRED_BE)


@pytest.mark.parametrize("entrada,trozo", [
    ({"rol": "be", SECRETO_EN_METADATO: "valor"}, "falta `carril`"),
    ({"rol": SECRETO_EN_METADATO, "carril": "demo"}, "rol no resuelve"),
    ({"rol": "be", "carril": SECRETO_EN_METADATO}, "carril no existe"),
])
def test_metadatos_malformados_tampoco_llegan_al_log(
        tmp_path, monkeypatch, capsys, entrada, trozo):
    """La credencial no es el único campo sensible: cualquier clave o valor del
    mapa puede contener un secreto por copy/paste. El diagnóstico sólo puede usar
    posición y vocabulario fijo del contrato."""
    _configura_mapa(monkeypatch, _mapa(tmp_path, {CRED_BE: entrada}))
    from .conftest import construir
    with pytest.raises(SystemExit) as e:
        construir(tmp_path, monkeypatch)
    assert trozo in str(e.value), str(e.value)
    _assert_credencial_fuera_de_salidas(capsys, e.value, CRED_BE)
    _assert_credencial_fuera_de_salidas(capsys, e.value, SECRETO_EN_METADATO)


def test_un_fichero_que_no_esta_donde_dices_no_arranca(tmp_path, monkeypatch):
    """Ausente NO es lo mismo que «configurado y no está». Lo primero es el defecto;
    lo segundo es un despliegue roto que se creería en fase 1 estando en ninguna."""
    _configura_mapa(monkeypatch, tmp_path / "no-existe.json")
    from .conftest import construir
    with pytest.raises(SystemExit) as e:
        construir(tmp_path, monkeypatch)
    assert "CREDENCIALES" in str(e.value)


def test_una_credencial_no_puede_acusar_el_latido_por_otro(servicio, tmp_path,
                                                           monkeypatch):
    """El cuarto verbo con sujeto, y el que más fácil se olvida porque YA tiene una
    puerta: `X-Llminbox-Watcher`.

    Autenticar y autorizar no son lo mismo. El watcher-token dice «esto viene de un
    watcher»; no dice «viene del watcher QUE DICE SER». Con una sola credencial de
    watcher compartida, `quien=` era declarativo: cualquiera acreditaba el ciclo de
    cualquiera, y `/health` daba por viva una vigilancia que no lo estaba.
    """
    _configura_mapa(monkeypatch, _mapa(tmp_path))
    from .conftest import construir
    s = construir(tmp_path, monkeypatch,
                  extra_env={"LLMINBOX_WATCHER_TOKEN": "w-token"})
    c = _cliente(s)
    cab = {"X-Llminbox-Token": CRED_BE, "X-Llminbox-Watcher": "w-token"}

    mal = c.post("/vigilancia/ack", params={"quien": "cto-A"}, headers=cab)
    assert mal.status_code == 403, mal.text

    ok = c.post("/vigilancia/ack", params={"quien": "backend"}, headers=cab)
    assert ok.status_code == 200, ok.text

    # ⊕ un ack SIN sujeto no suplanta a nadie: sigue pasando (no se cierra de más)
    assert c.post("/vigilancia/ack", headers=cab).status_code == 200


# ── LO QUE ENCONTRÓ LA REVISIÓN ADVERSARIAL DE LA SUPERFICIE DE AUTH ───────────
def test_la_anotacion_no_es_una_escritura_sin_validar(servicio, tmp_path, monkeypatch):
    """⊖ del defecto que introduje YO al poner `exige_ser` el primero de todo.

    `exige_ser` corre ANTES del gate de censo —y tiene que hacerlo, si no queda detrás
    de un chequeo de capacidad y se vuelve inerte—, así que la anotación se convirtió en
    la única escritura del servicio que aceptaba lo que le mandaran. Con el token
    compartido, o sea el 100% de la flota durante la fase 1.

    MEDIDO sobre la primera versión: un `agent` de 2.000.000 de bytes se guardaba
    entero, y 300 sujetos inventados daban 300 filas en 0,43 s.

    La cota NO es truncar: truncar deja igual de abierto el número de filas DISTINTAS.
    Es que el sujeto pase por el censo — con eso la tabla queda acotada por |censo| × 4
    verbos, pase lo que pase con el tráfico.
    """
    _configura_mapa(monkeypatch, _mapa(tmp_path))
    from .conftest import construir
    s = construir(tmp_path, monkeypatch)
    c = _cliente(s)

    c.post("/claim", json={"tema": "x", "agent": "A" * 2_000_000, "rol": "ejecuta"})
    for i in range(50):
        c.post("/claim", json={"tema": f"t{i}", "agent": f"inventado-{i}", "rol": "ejecuta"})

    filas = _con().execute("SELECT pedido, veces FROM v8_anon").fetchall()
    assert len(filas) == 1, f"51 sujetos inventados dejaron {len(filas)} filas: {filas}"
    assert filas[0]["pedido"] == "(fuera del censo)", filas[0]["pedido"]
    assert filas[0]["veces"] == 51, filas[0]["veces"]
    assert max(len(f["pedido"]) for f in filas) < 64

    # ⊕ y un sujeto DEL censo sí se nombra: si no, el número no dice a quién le falta
    c.post("/claim", json={"tema": "y", "agent": "cto-A", "rol": "ejecuta"})
    quienes = {r["pedido"] for r in _con().execute("SELECT pedido FROM v8_anon")}
    assert "cto-A" in quienes, quienes


def test_una_llamada_repetida_no_es_un_sujeto_nuevo(servicio, tmp_path, monkeypatch):
    """`sin_identidad_24h` cuenta SUJETOS, no llamadas: para decidir la fase 2 lo que
    hace falta saber es a quién le falta credencial. Un agente que llama mil veces es
    un agente por migrar, no mil."""
    _configura_mapa(monkeypatch, _mapa(tmp_path))
    from .conftest import construir
    s = construir(tmp_path, monkeypatch)
    c = _cliente(s)
    for i in range(10):
        c.post("/claim", json={"tema": f"r{i}", "agent": "cto-A", "rol": "ejecuta"})
    assert c.get("/health").json()["v8"]["sin_identidad_24h"] == 1


def test_una_credencial_repetida_en_el_mapa_no_arranca(tmp_path, monkeypatch, capsys):
    """JSON se queda con la ÚLTIMA clave repetida, así que un mapa con la misma
    credencial dos veces perdía una entrada EN SILENCIO — infra escribe la credencial
    de `be` y la de `cto` con el mismo valor por un copy-paste, y una desaparece.

    La comprobación de duplicados vivía DESPUÉS de `json.loads`, o sea sobre un dict
    que ya había colapsado: código muerto. La revisión adversarial lo cazó. Se mira
    ahora sobre los pares CRUDOS.
    """
    p = tmp_path / "credenciales.json"
    p.write_text('{"%s": {"rol": "be", "carril": "demo"}, '
                 '"%s": {"rol": "cto", "carril": "demo"}}' % (CRED_BE, CRED_BE))
    _configura_mapa(monkeypatch, p)
    from .conftest import construir
    with pytest.raises(SystemExit) as e:
        construir(tmp_path, monkeypatch)
    assert "dos veces" in str(e.value), str(e.value)
    _assert_credencial_fuera_de_salidas(capsys, e.value, CRED_BE)


def test_la_ventana_de_24h_acota_de_verdad(servicio, tmp_path, monkeypatch):
    """⊖ que faltaba: ningún test tenía una fila MÁS VIEJA de 24 h, así que romper la
    ventana (`WHERE visto > ? OR 1=1`) sobrevivía a la suite entera.

    Y la ventana es lo que hace alcanzable el umbral de la fase 2: contando el total,
    el número sólo sube y nunca llega a 0 — un umbral que no se puede cruzar es lo
    mismo que no tener umbral.
    """
    _configura_mapa(monkeypatch, _mapa(tmp_path))
    from .conftest import construir
    s = construir(tmp_path, monkeypatch)
    c = _cliente(s)

    c.post("/claim", json={"tema": "hoy", "agent": "cto-A", "rol": "ejecuta"})
    assert c.get("/health").json()["v8"]["sin_identidad_24h"] == 1   # ⊕ control

    # el mismo sujeto, envejecido tres días: ya migró, no debe seguir contando
    con = _con()
    con.execute("UPDATE v8_anon SET visto=?",
                ("2026-01-01T00:00:00+00:00",))
    con.commit()
    assert c.get("/health").json()["v8"]["sin_identidad_24h"] == 0, (
        "una fila de hace meses sigue contando: el umbral de la fase 2 nunca se cruza")


def test_en_append_el_403_va_ANTES_del_503(servicio, tmp_path, monkeypatch):
    """CORRIGE una afirmación mía que era falsa, cazada por @security en P5.

    Yo escribí —en el docstring, en el commit, en la PR y en tres mensajes al ledger—
    que el `exige_ser` de `/append` era INERTE porque la ruta contesta 503 antes de
    escribir. Verificado a mano después de que security lo señalara:

        exige_ser ................ 5914   ← el 403 sale aquí
        os.access(W_OK) → 503 .... 5974   ← sesenta líneas después

    Lo inerte es la ESCRITURA (503 para todos, incluido el rol correcto), no el gate de
    identidad: un portador con el rol equivocado recibe 403 EN PRODUCCIÓN.

    EL LEDGER SE PONE EN SÓLO LECTURA, que es como está producción — 13 de 13 montajes
    `:ro`. Un arnés donde el fichero es escribible mediría un orden que producción no
    tiene, y este repo ya se comió exactamente eso con el guard de inyección de
    cabecera.
    """
    import os as _os
    import stat as _stat
    _configura_mapa(monkeypatch, _mapa(tmp_path))
    from .conftest import construir
    s = construir(tmp_path, monkeypatch)
    c = _cliente(s)

    md = tmp_path / "DEMO-LEDGER.md"
    _os.chmod(md, _stat.S_IRUSR | _stat.S_IRGRP | _stat.S_IROTH)   # 444, como producción
    try:
        cuerpo = {"ledger": "demo-ledger", "actor": "cto-A", "to": ["backend"],
                  "tipo": "FYI", "head": "x", "body": "y"}
        # ⊖ rol equivocado ⇒ 403, NO 503: la identidad se comprueba antes de la capacidad
        mal = c.post("/append", json=cuerpo, headers={"X-Llminbox-Token": CRED_BE})
        assert mal.status_code == 403, (
            f"con el ledger :ro devolvió {mal.status_code}: el gate de identidad quedó "
            "detrás del chequeo de capacidad y no corre en producción")
        # ⊕ control: con el rol CORRECTO sí se llega al 503 — o sea que el 403 de arriba
        # es de identidad y no un rechazo genérico de la ruta
        ok = c.post("/append", json=cuerpo, headers={"X-Llminbox-Token": CRED_CTO})
        assert ok.status_code == 503, (
            f"el rol correcto devolvió {ok.status_code}: si no llega al 503, el ⊖ de "
            "arriba no prueba que sea el gate de identidad")
    finally:
        _os.chmod(md, _stat.S_IRUSR | _stat.S_IWUSR | _stat.S_IRGRP | _stat.S_IROTH)


def test_la_cobertura_no_pide_credencial_a_la_difusion_ni_a_los_humanos(
        servicio, tmp_path, monkeypatch):
    """El denominador de la cobertura decide cuándo se dispara la fase 2.

    Mi primera versión contaba los roles del censo A SECAS, y ahí dentro hay cosas a
    las que NUNCA se les va a emitir una credencial: los destinos de difusión
    (`TODOS`/`equipo`/`flota`, que no son nadie) y los humanos. Con el denominador
    inflado, `roles_cubiertos == roles_emisibles` no se cumple por mucho que infra
    emita, y el disparador queda inalcanzable — que es lo mismo que no tener umbral, y
    lo tengo escrito veinte líneas más arriba sobre la ventana de 24 h.

    MEDIDO contra el censo vivo: 36 del censo = 3 difusión + 2 humanos + 31 emisibles.
    """
    _configura_mapa(monkeypatch, _mapa(tmp_path))
    from .conftest import construir
    s = construir(tmp_path, monkeypatch, roster={
        "agentes": [{"nombre": "backend", "humano": "operador", "clave": "", "rol": "be"},
                    {"nombre": "cto-A", "humano": "operador", "clave": "", "rol": "cto"}],
        "humanos": [{"nombre": "operador", "alias": ["Operador"]}],
        "difusion": ["equipo", "TODOS"],
    })
    c = _cliente(s)
    v8 = c.get("/health").json()["v8"]

    emisibles = v8["roles_emisibles"]
    assert "roles_del_censo" not in v8, "el denominador inflado sigue publicándose"
    # `be` y `cto` son emisibles; `operador` (humano) y `equipo`/`TODOS` (difusión) no.
    assert emisibles == 2, (
        f"cuenta {emisibles} emisibles: la difusión o los humanos siguen dentro")
    # ⊕ y con las dos credenciales del mapa, la cobertura ES completa — que es lo que
    # el disparador de la fase 2 necesita poder alcanzar alguna vez.
    assert v8["roles_cubiertos"] == emisibles


def test_el_watcher_no_cuenta_como_sujeto_fuera_del_censo(servicio, tmp_path, monkeypatch):
    """`/vigilancia/ack` valida su sujeto con OTRA autoridad (`canoniza_quien`), así que
    un watcher legítimo no está en el censo del troceador.

    Sin distinguirlo, sus acks caían en «(fuera del censo)» y el recuento se leía como
    llamadas de alguien no censado — 41 de ellas en el índice vivo. Es la conclusión
    falsa que una tabla así induce, y la iba a mandar como dato para priorizar.

    LA COTA SE MANTIENE: el bucket es un LITERAL, no `canoniza_quien` como clave. Esa
    función es una regex de cardinalidad ilimitada y usarla para agrupar reabriría el
    crecimiento sin cota que esta tabla cerró.
    """
    _configura_mapa(monkeypatch, _mapa(tmp_path))
    from .conftest import construir
    s = construir(tmp_path, monkeypatch, extra_env={"LLMINBOX_WATCHER_TOKEN": "w-tok"})
    c = _cliente(s)
    cab = {"X-Llminbox-Watcher": "w-tok"}      # token compartido: sin identidad V8

    c.post("/vigilancia/ack", params={"quien": "watcher-compartido-harness"}, headers=cab)
    filas = {r["pedido"]: r["veces"] for r in _con().execute(
        "SELECT pedido, veces FROM v8_anon WHERE verbo LIKE '%vigilancia/ack%'")}
    assert filas == {"(watcher)": 1}, (
        f"el watcher se contabiliza como sujeto no censado: {filas}")

    # ⊖ un `quien` que NI SIQUIERA es canónico sigue cayendo donde debe
    c.post("/vigilancia/ack", params={"quien": "NO CANONICO !!"}, headers=cab)
    assert {r["pedido"] for r in _con().execute(
        "SELECT pedido FROM v8_anon WHERE verbo LIKE '%vigilancia/ack%'")} == {
        "(watcher)", "(fuera del censo)"}

    # ⊕ y en OTRO verbo, un sujeto no censado NO se disfraza de watcher
    c.post("/claim", json={"tema": "x", "agent": "inventado-zzz", "rol": "ejecuta"})
    q = {r["pedido"] for r in _con().execute(
        "SELECT pedido FROM v8_anon WHERE verbo LIKE '%claim%'")}
    assert q == {"(fuera del censo)"}, q


def test_censo_vacio_no_se_confunde_con_mapa_invalido(tmp_path, monkeypatch):
    """LA RAÍZ de los cuatro falsos rojos que tuvo `llmi credenciales` esta tarde.

    Sin censo, `lp.CANON` es {} y ningún rol resuelve, así que un mapa PERFECTAMENTE
    VÁLIDO se rechazaba con «el rol X no resuelve en el censo» — un mensaje que manda a
    arreglar el fichero cuando lo roto es el entorno.

    Lo señaló @harness en su pre-mortem del rollout: fail-closed y molesto, no
    peligroso, pero recurrente mientras no se nombre.
    """
    (tmp_path / "vacio.json").write_text(
        '{"agentes": [], "humanos": [], "difusion": []}')
    (tmp_path / "credenciales.json").write_text(
        '{"c-000000": {"rol": "be", "carril": "demo"}}')
    _configura_mapa(monkeypatch, tmp_path / "credenciales.json")
    from .conftest import construir
    with pytest.raises(SystemExit) as e:
        construir(tmp_path, monkeypatch, roster={"agentes": [], "humanos": [],
                                                 "difusion": []})
    msg = str(e.value)
    assert "CENSO está vacío" in msg, msg
    assert "NO es tu fichero" in msg, (
        "el mensaje no exculpa al mapa: seguirá mandando a arreglar el fichero")


def test_con_censo_el_mismo_mapa_pasa(tmp_path, monkeypatch):
    """⊕ que impide curar de más: el guard sólo puede disparar con el censo VACÍO.
    Con censo, ese mismo mapa arranca."""
    _configura_mapa(monkeypatch, _mapa(tmp_path))
    from .conftest import construir
    s = construir(tmp_path, monkeypatch)          # roster normal del arnés
    assert len(s.CREDENCIALES) == 2


def test_health_distingue_no_emitido_de_no_cableado(servicio, cliente, tmp_path,
                                                    monkeypatch):
    """`credenciales: 0` mezclaba dos estados MUY distintos y costó dos horas.

    Con `LLMINBOX_CREDENCIALES` vacía el contador da 0 tanto si nadie ha emitido como
    si hay un mapa emitido y sin cablear. En el primero falta trabajo de @infra; en el
    segundo falta un acto del operador. Leí el 0 como el primero mientras infra
    esperaba a que yo montara lo que ya había emitido a las 19:47Z.

    `configurado` responde a la pregunta que faltaba: ¿hay algo que montar?
    """
    # ① sin variable: nadie ha cableado nada
    v8 = cliente.get("/health").json()["v8"]
    assert v8["configurado"] is False and v8["credenciales"] == 0

    # ② con variable y mapa: cableado y contando
    _configura_mapa(monkeypatch, _mapa(tmp_path))
    from .conftest import construir
    from fastapi.testclient import TestClient
    s2 = construir(tmp_path / "b", monkeypatch) if False else construir(tmp_path, monkeypatch)
    c2 = TestClient(s2.app); c2.__enter__(); s2.barrido()
    c2.headers.update({"X-Llminbox-Token": "test-token"})
    v8b = c2.get("/health").json()["v8"]
    assert v8b["configurado"] is True and v8b["credenciales"] == 2, v8b


def test_configurado_no_es_lo_mismo_que_hay_credenciales(tmp_path, monkeypatch):
    """⊖ EL QUE DE VERDAD SEPARA LAS DOS MEDIDAS, y sin él el campo era decorativo.

    Su mutante —`configurado = bool(CREDENCIALES)`— SOBREVIVÍA a todo lo demás, porque
    en los otros casos la variable y el mapa van siempre juntos. El caso que los separa
    es el mapa VACÍO: variable puesta, cero credenciales.

    Y no es rebuscado: es exactamente lo que ve alguien que cablea un fichero antes de
    llenarlo. `configurado: true · credenciales: 0` dice «hay algo montado y está
    vacío», que es un estado distinto de «no hay nada montado» y pide un arreglo
    distinto.
    """
    p = tmp_path / "vacio-pero-valido.json"
    p.write_text("{}")
    _configura_mapa(monkeypatch, p)
    from .conftest import construir
    from fastapi.testclient import TestClient
    s = construir(tmp_path, monkeypatch)
    c = TestClient(s.app); c.__enter__(); s.barrido()
    c.headers.update({"X-Llminbox-Token": "test-token"})
    v8 = c.get("/health").json()["v8"]
    assert v8["configurado"] is True, "hay variable puesta y dice que no"
    assert v8["credenciales"] == 0
    assert v8["hay_mapa"] is False, "un mapa vacío no puede contar como identidad"
