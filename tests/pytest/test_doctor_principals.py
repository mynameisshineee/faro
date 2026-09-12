"""⑥ — identidades que el servicio autentica y el organigrama no gobierna.

Medido el 2026-08-18: `roles-por-alias.json` (organigrama firmado) declara 16
roles; `roster.json` (censo de identidad) declara 31; y `canon_identidad()`
resuelve por la UNIÓN de los dos. Resultado: **16 nombres que pasan la puerta
fail-closed, reciben correo, y no tienen jefe, ni gate, ni humano responsable.**
El sistema permite que exista una identidad operativa reconocida sin saber qué
clase de entidad es ni quién responde por ella.

**Esta sección ENCUENTRA; no adjudica.** El tipo sale `UNCLASSIFIED` y se queda
así hasta que el operador lo decida. Inferirlo por el nombre —«`destilador` suena
a servicio»— sería que el software se invente el organigrama, que es exactamente
lo que este informe existe para impedir. Lo que sí enseña son HECHOS
comprobables: si está en el censo, si está en el organigrama, cuántos alias
tiene, si alguien ha consumido por él y si recibe correo.
"""
from __future__ import annotations

import json

from .conftest import construir, db_directa
from fastapi.testclient import TestClient

ORG = {                                   # organigrama firmado: sólo `be` y `cto`
    "rol_por_alias": {"backend": "be", "cto-A": "cto"},
    "jerarquia": {"cto": {"reporta_a": "OPERADOR", "capa": "c-suite"},
                  "be": {"reporta_a": "cto", "capa": "ejecucion"},
                  # EL CASO INVERSO, medido en producción: existe en el
                  # organigrama y NO tiene identidad. Hoy `/inbox/engineering-
                  # manager` da 422 — un rol que no puede recibir trabajo.
                  "engineering-manager": {"reporta_a": "cto", "capa": "ejecucion"}},
}
ROSTER = {                                # censo: además, dos que NADIE gobierna
    "agentes": [
        {"nombre": "backend", "humano": "operador", "clave": "", "rol": "be"},
        {"nombre": "cto-A", "humano": "operador", "clave": "", "rol": "cto"},
        {"nombre": "destilador", "humano": "operador", "clave": "", "rol": "destilador"},
        {"nombre": "harness-x", "humano": "operador", "clave": "", "rol": "harness-x"},
    ],
    "humanos": [{"nombre": "operador", "alias": ["Operador"]}],
    "difusion": ["equipo"],
}


def _monta(tmp_path, monkeypatch):
    (tmp_path / "org.json").write_text(json.dumps(ORG))
    s = construir(tmp_path, monkeypatch, roster=ROSTER,
                  extra_env={"LLMINBOX_ROLES_ALIAS": str(tmp_path / "org.json")})
    (tmp_path / "DEMO-LEDGER.md").write_text(
        "### [cto-A → backend · FYI] gobernado\ncuerpo\n"
        "### [cto-A → destilador · FYI] a un huérfano\ncuerpo\n")
    return s


def _seis(txt):
    ini = txt.index("⑥ ")
    resto = txt[ini:]
    for otra in ("\n① ", "\n② ", "\n③ ", "\n── ④", "\n── ⑤"):
        if otra in resto:
            resto = resto[: resto.index(otra)]
    return resto


def _doctor(s):
    with TestClient(s.app) as c:
        s.barrido()
        c.headers.update({"X-Llminbox-Token": "test-token"})
        return c.get("/doctor").text


def test_encuentra_las_identidades_que_nadie_gobierna(tmp_path, monkeypatch):
    """Los dos que están en el censo y no en el organigrama, y sólo ésos.

    FALSADOR: sin la sección no hay nada que leer; y si listara TODO el censo,
    `be` y `cto` saldrían y el control de abajo caería."""
    sec = _seis(_doctor(_monta(tmp_path, monkeypatch)))
    assert "3 " in sec.splitlines()[0], sec.splitlines()[0]
    assert "destilador" in sec and "harness-x" in sec, sec
    # Y el inverso: en el organigrama, sin identidad. Es el que rompe de verdad —
    # se le puede asignar trabajo y su bandeja contesta 422.
    assert "engineering-manager" in sec, sec


def test_no_acusa_a_quien_si_esta_en_el_organigrama(tmp_path, monkeypatch):
    """CONTROL NEGATIVO, y va aquí porque sin él lo de arriba no prueba nada: un
    informe que liste a todo el censo «encuentra» a los huérfanos por acumulación,
    no por detección."""
    sec = _seis(_doctor(_monta(tmp_path, monkeypatch)))
    filas = [ln for ln in sec.splitlines() if ln.startswith("   ")]
    nombres = {ln.split()[0] for ln in filas if ln.strip() and not ln.strip().startswith(("principal", "El ", "ⓘ"))}
    assert "be" not in nombres and "cto" not in nombres, nombres


def test_no_inventa_el_tipo_de_nadie(tmp_path, monkeypatch):
    """LA LÍNEA QUE NO SE CRUZA. `destilador` suena a servicio y `harness-x` a
    herramienta, y aun así el informe NO lo dice: adjudicar el tipo es del
    operador. Un software que se inventa el organigrama es el problema que esta
    sección denuncia, no su solución.

    FALSADOR: cualquier inferencia por nombre mete `SERVICE`/`ROLE`/`ALIAS` en la
    salida y esto se pone rojo."""
    sec = _seis(_doctor(_monta(tmp_path, monkeypatch)))
    assert sec.count("UNCLASSIFIED") == 3, sec
    for inventado in ("SERVICE", "ALIAS", "GROUP", "LEGACY", "PROJECT"):
        assert inventado not in sec, f"infirió {inventado}: {sec}"


def test_enseña_hechos_comprobables_no_opiniones(tmp_path, monkeypatch):
    """Las columnas son hechos que el servicio puede sostener, uno por proyección
    más la pregunta derivada. `destilador` RECIBE correo en el arnés y no tiene
    cursor — las dos cosas tienen que verse, porque juntas son el caso que
    importa: le llega trabajo y nadie lo drena."""
    sec = _seis(_doctor(_monta(tmp_path, monkeypatch)))
    # La línea de COLUMNAS, no la cabecera de la sección: ésta también
    # contiene «principal(es)» y el `in` suelto la cazaba a ella.
    cab = next(ln for ln in sec.splitlines() if ln.strip().startswith("principal "))
    for col in ("roster", "org_alias", "jerarquia", "resuelve", "cursor",
                "correo", "tipo"):
        assert col in cab, f"falta la columna {col}: {cab}"
    fila = next(ln for ln in sec.splitlines() if ln.strip().startswith("destilador"))
    assert "sí" in fila, fila


def test_sin_organigrama_montado_no_acusa_a_todo_el_mundo(tmp_path, monkeypatch):
    """CONTROL del caso degradado, y es el que evita un informe catastrofista: sin
    la fuente firmada montada, `JERARQUIA` está vacía y comparar contra ella diría
    que NADIE está gobernado — 31 acusaciones falsas de golpe.

    FALSADOR: quitar la guarda de «no montada» hace que esta sección liste al
    censo entero."""
    s = construir(tmp_path, monkeypatch, roster=ROSTER)   # sin LLMINBOX_ROLES_ALIAS
    txt = _doctor(s)
    sec = _seis(txt)
    assert "destilador" not in sec, sec
    assert "organigrama NO montado" in sec, sec


# ── ⑥ refresca su PROPIA fuente, y distingue cuatro realidades ────────────────
# Corrección del operador: `censo | organigrama` mezclaba tres cosas distintas.
# Hay tres proyecciones (roster.json · rol_por_alias · jerarquía) y una pregunta
# derivada (¿resuelve la identidad?), y cada combinación es una patología
# diferente:
#
#   resolvable=sí + hierarchy=no  → identidad operativa SIN GOBIERNO
#   hierarchy=sí + resolvable=no  → rol organizativo IMPOSIBLE DE EJECUTAR
#   roster ≠ org_alias            → DERIVA entre proyecciones legacy
#
# La tercera no bloquea nada mientras la unión sea el mecanismo transitorio, pero
# no puede desaparecer del diagnóstico: es el caso vivo de `engineering-manager`.

def _monta_em(tmp_path, monkeypatch):
    """El estado REAL de producción: el EM está en el organigrama (con alias) y
    NO está en el roster; su bandeja resuelve por la unión."""
    org = json.loads(json.dumps(ORG))
    org["rol_por_alias"]["engineering-manager"] = "engineering-manager"
    org["rol_por_alias"]["em-bikeus"] = "engineering-manager"
    # Y la SEGUNDA patología, que hoy tiene cero casos en producción y por eso hay
    # que fabricarla aquí: un rol declarado en la jerarquía SIN ningún alias que
    # lo resuelva. Está en el organigrama y no puede recibir trabajo.
    org["jerarquia"]["rol-inejecutable"] = {"reporta_a": "cto", "capa": "ejecucion"}
    (tmp_path / "org.json").write_text(json.dumps(org))
    s = construir(tmp_path, monkeypatch, roster=ROSTER,
                  extra_env={"LLMINBOX_ROLES_ALIAS": str(tmp_path / "org.json")})
    (tmp_path / "DEMO-LEDGER.md").write_text(
        "### [cto-A → backend · FYI] gobernado\ncuerpo\n")
    return s, tmp_path / "org.json"


def test_distingue_las_tres_patologias(tmp_path, monkeypatch):
    """`engineering-manager` no está en el roster, sí en el organigrama con dos
    alias, y su bandeja responde. Con dos columnas eso se leía como «no está en
    el censo» — que es falso y llevaría a darlo de alta sin necesidad, pagando un
    reindex completo por nada.

    FALSADOR: colapsar `org_alias` y `hierarchy` en una sola columna hace
    indistinguible esta fila de un rol que NO puede recibir trabajo."""
    s, _ = _monta_em(tmp_path, monkeypatch)
    sec = _seis(_doctor(s))
    cab = next(ln for ln in sec.splitlines() if ln.strip().startswith("principal "))
    for col in ("roster", "org_alias", "jerarquia", "resuelve", "cursor", "correo", "tipo"):
        assert col in cab, f"falta la columna {col}: {cab}"
    fila = next(ln for ln in sec.splitlines() if ln.strip().startswith("engineering-manager"))
    campos = fila.split()
    assert campos[1] == "no", f"roster debería ser no: {fila}"
    assert campos[2] == "sí" and campos[3] == "sí", f"org_alias/jerarquia: {fila}"
    assert campos[4] == "sí", f"tiene que constar que RESUELVE: {fila}"


def test_F_DOC_1_ve_el_cambio_sin_que_nadie_llame_a_organigrama(tmp_path, monkeypatch):
    """⑥ no puede depender de que otro endpoint haya refrescado antes. Si la
    fuente cambia y sólo se llama a `/doctor`, el diagnóstico tiene que ser sobre
    la fuente ACTUAL — o no ser un diagnóstico.

    FALSADOR: leer `lp.JERARQUIA` sin refrescar deja fuera al rol nuevo y ⑥
    informa de una organización que ya no existe."""
    s, ruta = _monta_em(tmp_path, monkeypatch)
    assert "sdet" not in _seis(_doctor(s))
    org = json.loads(ruta.read_text())
    org["jerarquia"]["sdet"] = {"reporta_a": "cto", "capa": "gate"}
    ruta.write_text(json.dumps(org))
    assert "sdet" in _seis(_doctor(s)), "⑥ no vio el cambio: diagnostica el pasado"


def test_F_DOC_2_con_la_fuente_rota_no_afirma_huerfanos_actuales(tmp_path, monkeypatch):
    """La precisión semántica del operador: el último-bueno vale como información
    sobre FRESCURA, no como base de una afirmación de gobierno.

    «Hay 17 principales divergentes» es una afirmación sobre AHORA. Con la fuente
    ilegible no se puede sostener, y emitirla igual convertiría un diagnóstico en
    una invención — el defecto que ⑥ existe para denunciar.

    FALSADOR: seguir calculando con el last-good imprime la lista como si fuera
    contemporánea y esto se pone rojo."""
    s, ruta = _monta_em(tmp_path, monkeypatch)
    assert "engineering-manager" in _seis(_doctor(s))
    ruta.unlink()
    sec = _seis(_doctor(s))
    assert "engineering-manager" not in sec, sec
    assert "NO VERIFICABLE" in sec, sec
    assert "ranci" in sec.lower(), sec        # «rancia o ilegible»


def test_no_se_lee_un_cero_como_SoT_resuelto(tmp_path, monkeypatch):
    """⑥ en cero demuestra que las proyecciones COINCIDEN en ese instante. No
    demuestra que haya una fuente única, ni compilador, ni gate de deriva.

    Sin esta línea, dentro de seis meses alguien lee un cero como una garantía
    que el sistema todavía no da — y es la misma clase de error que todo lo que
    ⑥ denuncia, cometido por quien lo lee."""
    s, _ = _monta_em(tmp_path, monkeypatch)
    sec = _seis(_doctor(s))
    assert "no sustituye" in sec.lower(), sec


def test_los_alias_que_resuelven_y_no_parsean_se_declaran(tmp_path, monkeypatch):
    """LO QUE SÍ SE PUEDE SOSTENER, después de retirar lo que no.

    Escribí una columna `enrutable` por principal y era un FALSO VERDE DE
    GOBIERNO: decía «sí» para un rol cuyo nombre el parser reconoce, sin poder
    sostener que la bandeja de ESE rol entregue la entrada. Medido: con
    `backend → plataforma` en el organigrama, `GET /inbox/plataforma` **no trae**
    el correo dirigido a `backend`, porque `escuchados()` deriva del roster y
    devuelve `['plataforma']`.

    «El parser produce un destinatario para un nombre» ≠ «la bandeja de este
    principal recibe ese destinatario». Son dos capas y las junté.

    Lo exactamente demostrable es esto: `em-bikeus` lo acepta
    `canon_identidad()` —su bandeja contesta 200— y `→ em-bikeus` NO produce
    destinatario, así que esa entrada queda huérfana.

    FALSADOR: quitar el bloque deja el hallazgo sin decir; y calcularlo sobre
    `CANON` en vez de sobre los alias del organigrama lo deja vacío siempre."""
    s, _ = _monta_em(tmp_path, monkeypatch)
    sec = _seis(_doctor(s))
    assert "NO PARSEABLE" in sec, sec
    assert "em-bikeus" in sec, sec
    assert "queda huérfana" in sec, sec


def test_un_alias_que_tambien_esta_en_el_censo_no_se_acusa(tmp_path, monkeypatch):
    """CONTROL: `backend` está en `rol_por_alias` Y en el censo, así que
    `→ backend` sí produce destinatario y no debe salir en la lista. Sin este
    control, listar TODOS los alias pasaría el test de arriba sin distinguir."""
    s, _ = _monta_em(tmp_path, monkeypatch)
    sec = _seis(_doctor(s))
    linea = next((ln for ln in sec.splitlines() if "NO PARSEABLE" in ln), "")
    assert "backend" not in linea, linea


def test_el_correo_del_informe_coincide_con_lo_que_la_bandeja_ENTREGA(tmp_path, monkeypatch):
    """La invariante que faltaba, y nace de un falso verde mío.

    El organigrama reasigna `backend` al rol `plataforma`. Mi versión anterior
    contaba ese correo como de `plataforma` —unía los nombres de las dos
    proyecciones— y decía `correo=sí`. Pero la bandeja NO lo entrega:
    `escuchados('plataforma')` devuelve `['plataforma']`, porque expande firmas
    desde el ROSTER. El informe afirmaba una entrega que no ocurre.

    Un informe de gobierno que cuenta correo que la bandeja no va a dar es peor
    que no contarlo: manda a alguien a buscar trabajo que no está ahí.

    Se comprueban LAS DOS COSAS en el mismo test, que es lo que las ata: lo que
    dice ⑥ y lo que devuelve `/inbox`. Si divergen, el informe miente — daría
    igual cuál de los dos «tenga razón».

    FALSADOR: volver a unir las proyecciones para `correo` pone `correo=sí` con
    la bandeja vacía y esto se pone rojo."""
    org = json.loads(json.dumps(ORG))
    org["rol_por_alias"]["backend"] = "plataforma"
    org["jerarquia"]["plataforma"] = {"reporta_a": "cto", "capa": "ejecucion"}
    (tmp_path / "org.json").write_text(json.dumps(org))
    s = construir(tmp_path, monkeypatch, roster=ROSTER,
                  extra_env={"LLMINBOX_ROLES_ALIAS": str(tmp_path / "org.json")})
    (tmp_path / "DEMO-LEDGER.md").write_text(
        "### [cto-A → backend · FYI] correo a un nombre reasignado\ncuerpo\n")
    with TestClient(s.app) as c:
        s.barrido()
        c.headers.update({"X-Llminbox-Token": "test-token"})
        entregado = "correo a un nombre reasignado" in c.get("/inbox/plataforma").text
        sec = _seis(c.get("/doctor").text)
    fila = next(ln for ln in sec.splitlines() if ln.strip().startswith("plataforma"))
    dice_correo = fila.split()[6] == "sí"
    assert entregado is False, "el arnés cambió: ahora la bandeja SÍ entrega"
    assert dice_correo == entregado, (
        f"⑥ dice correo={'sí' if dice_correo else 'no'} y la bandeja entrega="
        f"{entregado} — el informe afirma una entrega que no ocurre:\n{fila}")


def test_el_cursor_se_encuentra_aunque_el_roster_declare_el_rol_en_mayusculas(tmp_path, monkeypatch):
    """El reverso de la normalización, y por eso va junto a ella.

    `clave_cursor()` guarda el rol TAL COMO lo declara `roster.json`. Al normalizar
    los conjuntos para comparar proyecciones, la consulta de cursores empezó a
    buscar el valor en minúsculas y no encontraba la fila: `cursor=no` sobre un
    cursor que existe. Normalizar para comparar y no normalizar al consultar es el
    mismo defecto con el signo cambiado.

    FALSADOR: volver a `WHERE agent=?` deja `cursor=no` con la fila delante."""
    roster = json.loads(json.dumps(ROSTER))
    for a in roster["agentes"]:
        if a["nombre"] == "destilador":
            a["rol"] = "DESTILADOR"           # el humano escribió en mayúsculas
    (tmp_path / "org.json").write_text(json.dumps(ORG))
    s = construir(tmp_path, monkeypatch, roster=roster,
                  extra_env={"LLMINBOX_ROLES_ALIAS": str(tmp_path / "org.json")})
    (tmp_path / "DEMO-LEDGER.md").write_text(
        "### [cto-A → destilador · FYI] algo\ncuerpo\n")
    with TestClient(s.app) as c:
        s.barrido()
        c.headers.update({"X-Llminbox-Token": "test-token"})
        # 🔻 pedia `hasta: 1` sobre un ledger de UN asiento (arrival 0): un off-by-one que el
        # RECORTE del servicio viejo tapaba y que el 409 ACK_BEYOND_LEDGER de c3ca365 DESTAPA
        # (medido por sdet 2026-09-11T19:19Z). Se pide lo que EXISTE, y se exige el 200: si
        # vuelve a colarse un `hasta` inventado, esto lo dice aqui y no tres lineas mas abajo.
        _c = db_directa(s)
        tope = _c.execute("SELECT MAX(arrival) AS m FROM entries WHERE ledger='demo-ledger'").fetchone()["m"]
        _c.close()
        r = c.post("/inbox/destilador/leido", json={"hasta": {"demo-ledger": tope}})
        assert r.status_code == 200, f"el ack no fue concedido: {r.status_code} {r.text[:120]}"
        con = db_directa(s)
        clave = con.execute("SELECT agent FROM cursors").fetchone()["agent"]
        sec = _seis(c.get("/doctor").text)
    assert clave == "DESTILADOR", f"el arnés no reprodujo el caso: clave={clave}"
    fila = next(ln for ln in sec.splitlines() if ln.strip().startswith("destilador"))
    assert fila.split()[5] == "sí", f"dice cursor=no con la fila delante: {fila}"


def test_el_alias_no_parseable_se_comprueba_CONTRA_LA_BANDEJA(tmp_path, monkeypatch):
    """El hallazgo atado a la conducta, no al texto del informe.

    La primera versión de este test sólo leía `/doctor`: habría seguido verde si
    el alias dejara de resolver, o si el parser empezara a producir destinatarios
    para él — o sea, precisamente cuando el hallazgo dejara de ser cierto.

    Se comprueban las dos mitades sobre una entrada REAL dirigida a `em-bikeus`:

      · `/inbox/em-bikeus` responde 200      → la identidad SÍ resuelve
      · y NO trae esa entrada                 → el nombre NO produce destinatario

    Juntas son la patología. Por separado, ninguna lo es.

    FALSADOR: si el parser aprendiera a enrutar alias del organigrama, la segunda
    aserción cae — y el hallazgo dejaría de ser cierto al mismo tiempo."""
    s, _ = _monta_em(tmp_path, monkeypatch)
    (tmp_path / "DEMO-LEDGER.md").write_text(
        "### [cto-A → em-bikeus · FYI] delegación al EM por su alias\ncuerpo\n")
    with TestClient(s.app) as c:
        s.barrido()
        c.headers.update({"X-Llminbox-Token": "test-token"})
        r = c.get("/inbox/em-bikeus")
        sec = _seis(c.get("/doctor").text)
    assert r.status_code == 200, f"la identidad ya no resuelve: {r.status_code}"
    assert "delegación al EM" not in r.text, (
        "el parser ahora SÍ enruta ese alias — el hallazgo dejó de ser cierto y "
        "el informe tiene que dejar de declararlo")
    assert "em-bikeus" in sec and "NO PARSEABLE" in sec, sec
