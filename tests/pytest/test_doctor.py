"""`/doctor` — las tres higienes del canal que nadie miraba.

No prueba que el servicio funcione: prueba que **sabe señalar el mal uso**, que es
distinto y es lo que hace útil al endpoint. Cada sección con su control positivo Y
su negativo, porque un doctor que diagnostica a todo el mundo no diagnostica.

Ojo al orden de los controles: primero se comprueba que la sección se QUEDA CALLADA
cuando no hay nada que decir. Si eso no se cumple, el hallazgo de después no prueba
nada — sería una sección que se queja siempre.
"""
from __future__ import annotations

import re

from .conftest import construir, db_directa
from fastapi.testclient import TestClient


def _texto(cliente, **params):
    r = cliente.get("/doctor", params=params)
    assert r.status_code == 200, r.text
    return r.text


def _seccion(txt, num):
    """El bloque de una sección: de su cabecera «① …» a la línea en blanco de antes
    de la siguiente. Se corta por el número y no por el título — el título es prosa
    y se reescribe; el número es el contrato."""
    marca = {1: "① ", 2: "② ", 3: "③ "}[num]
    ini = txt.index(marca)
    resto = txt[ini:]
    for otra in ("\n① ", "\n② ", "\n③ "):
        if otra in resto:
            resto = resto[: resto.index(otra)]
    return resto


# ── ① mira y no drena ────────────────────────────────────────────────────────

def test_sin_lecturas_nadie_aparece_como_moroso(cliente):
    """CONTROL NEGATIVO, y va primero: con el correo sin entregar a nadie que haya
    mirado, la sección ① tiene que estar VACÍA. Si aquí saliera gente, el test de
    abajo pasaría por acumulación de ruido, no por detección."""
    s = _seccion(_texto(cliente), 1)
    assert "0 agente(s)" in s
    assert "nadie tiene correo dirigido sin consumir" in s


def test_mirar_sin_drenar_sale_con_su_cuenta(cliente, servicio):
    """Mirar la bandeja NO consume (es la propiedad que este repo defiende). Así que
    tras mirar, el pendiente sigue ahí y ① tiene que decirlo, con el número."""
    cliente.get("/inbox/backend")          # mira, no consume
    servicio.barrido()                     # el barrido es el que persiste `lecturas`
    s = _seccion(_texto(cliente), 1)
    assert "1 agente(s)" in s
    # Sale por su ROL (`be`), no por el nombre con que miró (`backend`): la clave
    # del cursor es el rol, y mezclarlos duplicaba a la misma persona en el informe.
    # 3 entradas dirigidas a `backend` en el arnés (2 en demo-ledger, 1 en otro).
    linea = next(l for l in s.splitlines() if l.strip().startswith("be "))
    assert linea.split()[1] == "3", linea
    assert "nunca" in s                    # no ha consumido: último consumo «nunca»


def test_al_drenar_desaparece_de_la_lista(cliente, servicio):
    """FALSADOR del anterior: si ① contara «lecturas» en vez de PENDIENTES, drenar
    no cambiaría nada y el agente seguiría en la lista para siempre."""
    cliente.get("/inbox/backend")
    servicio.barrido()
    assert "1 agente(s)" in _seccion(_texto(cliente), 1)
    cliente.post("/inbox/backend/leido", json={"hasta": {"demo-ledger": 1, "otro-ledger": 0}})
    assert "0 agente(s)" in _seccion(_texto(cliente), 1)


# ── ② publica y no dirige ────────────────────────────────────────────────────

def test_todo_dirigido_no_acusa_a_nadie(cliente):
    """CONTROL NEGATIVO: el arnés tiene las 3 entradas con `→ backend`. Si ② marcara
    algo aquí, estaría contando entradas y no entradas HUÉRFANAS."""
    s = _seccion(_texto(cliente), 2)
    assert "0 de 3" in s
    assert "todo lo publicado en la ventana nombra a alguien" in s


def test_entrada_sin_destinatario_se_atribuye_a_su_autor(tmp_path, monkeypatch):
    """El caso real: 54 de 56 entradas de la flota no nombraban a nadie. Se cuenta
    POR AUTOR a propósito — un porcentaje global no le dice a nadie qué cambiar."""
    from fastapi.testclient import TestClient
    s = construir(tmp_path, monkeypatch)
    # Sin flecha y sin @nombre: no cae en ninguna bandeja.
    (tmp_path / "DEMO-LEDGER.md").write_text(
        "### [cto-A → backend · REQUEST] primera\ncuerpo uno\n"
        "### [cto-A · FYI] huerfana una\ncuerpo\n"
        "### [cto-A · FYI] huerfana dos\ncuerpo\n"
    )
    with TestClient(s.app) as c:
        s.barrido()
        c.headers.update({"X-Llminbox-Token": "test-token"})
        sec = _seccion(_texto(c), 2)
    assert "cto" in sec
    # 2 huérfanas de las 4 suyas: 3 en demo-ledger + 1 en otro-ledger, que el arnés
    # también le atribuye. La cuenta es por AUTOR y cruza ledgers a propósito.
    linea = next(l for l in sec.splitlines() if l.strip().startswith("cto"))
    assert linea.split()[1:3] == ["2", "4"], linea


def test_la_ventana_de_dias_se_respeta(tmp_path, monkeypatch):
    """FALSADOR del parámetro: si `dias` fuera decorativo, una huérfana ANTIGUA
    seguiría contando con `dias=1` y el informe hablaría de deuda ya resuelta."""
    from fastapi.testclient import TestClient
    s = construir(tmp_path, monkeypatch)
    (tmp_path / "DEMO-LEDGER.md").write_text(
        "### [cto-A · FYI] 2020-01-01T00:00:00Z vieja y huerfana\ncuerpo\n"
    )
    with TestClient(s.app) as c:
        s.barrido()
        c.headers.update({"X-Llminbox-Token": "test-token"})
        sec = _seccion(_texto(c, dias=1), 2)
        assert "todo lo publicado en la ventana nombra a alguien" in sec
        # 0 huérfanas de 1: la vieja (2020) cae fuera de la ventana; la que queda es
        # la de `otro-ledger`, que NO lleva sello de hora y por eso se incluye —
        # excluirlas escondía justo el caso que esta sección cuenta.
        assert "0 de 1" in sec and "1 sin sello de hora" in sec


# ── ③ claims pasados de TTL ──────────────────────────────────────────────────

def test_claim_recien_cogido_no_se_denuncia(cliente):
    """CONTROL NEGATIVO: un claim de hace un segundo no está pasado de plazo. Sin
    esto, la sección ③ podría estar listando TODOS los abiertos."""
    cliente.post("/claim", json={"tema": "algo", "rol": "ejecuta", "agent": "backend"})
    s = _seccion(_texto(cliente), 3)
    assert "0 sin cerrar" in s
    assert "ninguno pasado de plazo" in s


def test_claim_viejo_aparece_con_sus_horas(cliente, servicio):
    """El caso que me pasó a mí: `qa` llevaba 45 h con un claim y nadie lo veía.
    Se falsifica moviendo el `abierto` hacia atrás en la tabla — el TTL es tiempo
    real, así que no hay forma de esperarlo en un test."""
    cliente.post("/claim", json={"tema": "algo", "rol": "ejecuta", "agent": "backend"})
    con = servicio.db()
    con.execute("UPDATE claims SET abierto='2020-01-01T00:00:00+00:00' WHERE tema='algo'")
    con.commit()
    con.close()
    s = _seccion(_texto(cliente), 3)
    assert "1 sin cerrar" in s
    assert "algo" in s and "ejecuta" in s


def test_el_doctor_no_escribe_nada(cliente, servicio):
    """`/inbox` fue un GET que mutaba estado y tumbó bandejas. Un informe de
    diagnóstico que escriba repite esa avería con otro nombre: se comprueban las
    tablas ANTES y DESPUÉS."""
    def foto():
        con = servicio.db()
        f = {t: con.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]
             for t in ("cursors", "claims", "lecturas", "entries", "recipients")}
        con.close()
        return f
    antes = foto()
    _texto(cliente)
    assert foto() == antes


def test_los_nombres_fuera_del_censo_no_compiten_con_los_de_dentro(tmp_path, monkeypatch):
    """La primera corrida contra la flota real sacó 11 `zzz-*` entre los 20 primeros,
    cada uno con 433 pendientes, empujando fuera a los agentes de verdad: `lecturas`
    apunta a CUALQUIER nombre que alguien consulte, y la difusión le da bandeja a
    cualquiera, así que los fantasmas acumulan deuda por diseño. Van aparte y
    CONTADOS, no listados — enumerarlos les devolvería el sitio que se les quita.
    """
    from fastapi.testclient import TestClient
    s_mod = construir(tmp_path, monkeypatch)
    # Una difusión: le llega a todo el mundo, censado o no. Es lo que le daba 433
    # pendientes a cada `zzz-*` en la flota real.
    (tmp_path / "DEMO-LEDGER.md").write_text(
        "### [cto-A → equipo · FYI] para todos\ncuerpo\n"
    )
    con = __import__("sqlite3").connect(str(tmp_path / "llminbox.sqlite"))
    with TestClient(s_mod.app) as c:
        s_mod.barrido()
        c.headers.update({"X-Llminbox-Token": "test-token"})
        c.get("/inbox/backend")          # uno del censo
        # El fantasma se SIEMBRA en `lecturas`, no se pide por HTTP: hoy la puerta de
        # identidad es fail-closed y `/inbox/zzz-fantasma` da 422. Los 11 `zzz-*` de
        # la flota real son residuo ANTERIOR a esa puerta — la tabla los conserva y
        # el informe se los encontraba delante.
        con2 = s_mod.db()
        con2.execute("INSERT OR REPLACE INTO lecturas(agent,primera,ultima,veces) "
                     "VALUES('zzz-fantasma','2020-01-01','2020-01-01',1)")
        con2.commit(); con2.close()
        s_mod.barrido()
        sec = _seccion(_texto(c), 1)
    con.close()
    assert "1 agente(s) del censo" in sec       # sólo `be`; el fantasma no compite
    assert "FUERA DEL CENSO" in sec and "zzz-fantasma" in sec
    # FALSADOR: si el fantasma entrara en el ranking sería una FILA con su columna de
    # pendientes. Sólo puede salir en la nota de abajo, y sin número propio.
    assert not any(l.strip().startswith("zzz-fantasma ") for l in sec.splitlines())


def test_un_alias_de_difusion_va_marcado(tmp_path, monkeypatch):
    """Los tres primeros puestos de la primera corrida real eran un humano y dos
    alias de difusión. Nadie drena esas bandejas, así que su deuda no es deuda de
    nadie — y sin la marca, quien lee empieza a arreglar por arriba lo que no existe.
    FALSADOR: si la marca se pusiera a todos, no distinguiría; `be` no puede llevarla.
    """
    from fastapi.testclient import TestClient
    s_mod = construir(tmp_path, monkeypatch)
    (tmp_path / "DEMO-LEDGER.md").write_text(
        "### [cto-A -> equipo · FYI] para todos\ncuerpo\n".replace("->", "\u2192")
    )
    with TestClient(s_mod.app) as c:
        s_mod.barrido()
        c.headers.update({"X-Llminbox-Token": "test-token"})
        c.get("/inbox/backend")
        con = s_mod.db()
        con.execute("INSERT OR REPLACE INTO lecturas(agent,primera,ultima,veces) "
                    "VALUES('equipo','2020-01-01','2020-01-01',1)")
        con.commit(); con.close()
        s_mod.barrido()
        sec = _seccion(_texto(c), 1)
    equipo = next(l for l in sec.splitlines() if l.strip().startswith("equipo "))
    assert "alias de difusión" in equipo
    be = next(l for l in sec.splitlines() if l.strip().startswith("be "))
    assert "alias de difusión" not in be and "humano" not in be


def test_cerrar_y_relevar_no_se_confunden(cliente, servicio):
    """`cerrado` la escribían los DOS caminos —cerrar lo tuyo y que te relevem por
    vencimiento— y el informe los sumaba en un solo «cerrados». Medido en la tabla
    viva, eso hacía ilegible el único número de la disciplina: «qa, 10 de 10» incluía
    el relevo que le hicieron. Un dato que no distingue se lee como el bueno.

    FALSADOR: si `motivo` no se escribiera, las dos filas saldrían con el mismo valor
    (o con ninguno) y la tasa de cierre volvería a mezclar las dos cosas.
    """
    cliente.post("/claim", json={"tema": "mio", "rol": "ejecuta", "agent": "backend"})
    cliente.post("/claim/cierro", json={"tema": "mio", "rol": "ejecuta", "agent": "backend"})

    cliente.post("/claim", json={"tema": "suyo", "rol": "ejecuta", "agent": "backend"})
    con = servicio.db()
    con.execute("UPDATE claims SET abierto='2020-01-01T00:00:00+00:00' WHERE tema='suyo'")
    con.commit()
    con.close()
    r = cliente.post("/claim", json={"tema": "suyo", "rol": "ejecuta", "agent": "cto-A"})
    assert r.json().get("relevaste_a") == "be", r.json()

    con = servicio.db()
    motivos = dict(con.execute("SELECT tema, motivo FROM claims WHERE cerrado IS NOT NULL"))
    con.close()
    assert motivos == {"mio": "cierro", "suyo": "relevo"}, motivos
    # Y el informe los reparte en vez de sumarlos en un montón.
    s = _seccion(_texto(cliente), 3)
    assert "1 los cerró su dueño" in s and "1 fueron relevos" in s


# ── el tablero al coger ──────────────────────────────────────────────────────

def test_al_coger_algo_te_devuelve_lo_que_ya_esta_cogido(cliente, servicio):
    """El casi-choque del 2026-08-11: iba a abrir un tema sobre trabajo que `qa` ya
    tenía con otro nombre, y lo que me salvó fue mirar los abiertos por mi cuenta.
    Esto pone ese tablero delante en el único instante en que sirve: al coger.

    FALSADOR: si `tambien_cogido` se calculara DESPUÉS de insertar sin excluir el
    tema propio, cada uno se vería a sí mismo y la lista sería ruido creciente.
    """
    cliente.post("/claim", json={"tema": "lo_de_qa", "rol": "ejecuta", "agent": "cto-A"})
    r = cliente.post("/claim", json={"tema": "lo_mio", "rol": "ejecuta", "agent": "backend"})
    cuerpo = r.json()
    assert cuerpo["ok"] is True
    temas = [t["tema"] for t in cuerpo["tambien_cogido"]]
    assert "lo_de_qa" in temas
    assert "lo_mio" not in temas, "no puede verse a sí mismo"
    assert cuerpo["tambien_cogido"][0]["de"] == "cto"      # el rol, no el nombre


def test_el_tablero_marca_lo_vencido(cliente, servicio):
    """Un abierto pasado de plazo es RELEVABLE, y esa es justo la información que
    convierte «está cogido» en «puedes seguirlo tú». Sin la marca, el tablero dice
    «ocupado» de algo que lleva dos días parado."""
    cliente.post("/claim", json={"tema": "parado", "rol": "ejecuta", "agent": "cto-A"})
    con = servicio.db()
    con.execute("UPDATE claims SET abierto='2020-01-01T00:00:00+00:00' WHERE tema='parado'")
    con.commit()
    con.close()
    r = cliente.post("/claim", json={"tema": "otro", "rol": "ejecuta", "agent": "backend"})
    fila = next(t for t in r.json()["tambien_cogido"] if t["tema"] == "parado")
    assert fila["vencido"] is True
    # CONTROL: uno recién cogido no puede salir marcado, o la marca no distingue nada.
    cliente.post("/claim", json={"tema": "fresco", "rol": "revisa", "agent": "cto-A"})
    r2 = cliente.post("/claim", json={"tema": "otro2", "rol": "ejecuta", "agent": "backend"})
    fresco = next(t for t in r2.json()["tambien_cogido"] if t["tema"] == "fresco")
    assert fresco["vencido"] is False


def test_un_cero_de_claims_vacios_no_se_lee_como_disciplina_perfecta(cliente):
    """El 2026-08-15 una corrupción se llevó los 96 claims —70 abiertos— y ③ publicó
    «0 sin cerrar ni relevar» durante tres días: la mejor nota posible sobre una
    pérdida de datos. El cero de «nadie se pasó de plazo» y el de «no queda nada que
    mirar» son opuestos y se imprimían igual.

    FALSADOR: con un claim vivo, este aviso NO puede aparecer — si saliera siempre,
    no distinguiría nada y sería otro decorado.
    """
    s = _seccion(_texto(cliente), 3)
    assert "la tabla de claims está VACÍA" in s
    assert "NO es «todo cerrado a tiempo»" in s

    cliente.post("/claim", json={"tema": "vivo", "rol": "ejecuta", "agent": "backend"})
    s2 = _seccion(_texto(cliente), 3)
    assert "está VACÍA" not in s2
    assert "ninguno pasado de plazo" in s2


# ── la tendencia: otra población, no otro número ─────────────────────────────

def test_la_tendencia_excluye_las_sin_fecha_y_lo_dice(tmp_path, monkeypatch):
    """El titular de ② es un STOCK y no puede moverse: más de la mitad son entradas
    históricas SIN FECHA. Medido el 2026-08-18, llevaba seis días clavado en 33 %
    mientras la conducta reciente sí cambiaba (37 % a 30 días → 20 % a 7).

    La cohorte las excluye POR CONSTRUCCIÓN, así que su denominador cae solo. Sin
    decirlo, el número pequeño se lee como una mejora que nadie ha hecho — es el aviso
    que pidió `llminbox-a7` al pasar la línea base.

    FALSADOR: una huérfana SIN FECHA tiene que contar en el titular y NO en la
    tendencia. Si contara en las dos, la cohorte no sería otra población: sería el
    mismo número con otro nombre.
    """
    from datetime import datetime, timedelta, timezone
    from fastapi.testclient import TestClient
    hoy = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    s = construir(tmp_path, monkeypatch)
    (tmp_path / "DEMO-LEDGER.md").write_text(
        f"### [cto-A → backend · FYI] {hoy} dirigida y fechada\ncuerpo\n"
        f"### [cto-A · FYI] {hoy} huerfana FECHADA\ncuerpo\n"
        "### [cto-A · FYI] huerfana SIN FECHA\ncuerpo\n"
    )
    with TestClient(s.app) as c:
        s.barrido()
        c.headers.update({"X-Llminbox-Token": "test-token"})
        sec = _seccion(_texto(c), 2)

    # El titular (stock) cuenta las DOS huérfanas: la fechada y la que no lo está.
    assert "2 de 4" in sec, sec           # 3 de este ledger + 1 de otro-ledger
    assert "2 sin sello de hora" in sec

    # La tendencia cuenta SÓLO la fechada.
    # Anclado al PATRÓN «N de M», no a la posición del token: `split()[2]` cazaba el
    # «día(s):» porque el propio número de días desplaza las columnas. Es la regla que
    # tengo escrita para los falsadores sobre salida de herramienta, aplicada aquí.
    linea = next(l for l in sec.splitlines() if "últimos  2 día(s)" in l)
    assert re.search(r"\b1 de\s+2 sin dirigir", linea), linea

    # Y lo dice, para que el denominador pequeño no se lea como mejora.
    assert "EXCLUYE por construcción" in sec
    assert "no porque el problema encoja" in sec


# ── ① el orden: la deuda de quien NUNCA drenó no es deuda de nadie ───────────
# Medido contra la flota real (2026-08-18): de las 8 primeras filas de ①, las 8
# tenían CERO cursores — nadie había consumido jamás por esos nombres. Cuatro
# salían marcadas (`OPERADOR`/`OTRO-HUMANO` humanos, `flota`/`TODOS` difusión) y cuatro
# no (`destilador`, `Lead`, `Lead-cfo-cockpit`, `code-reviewer-biklabs`), pero
# las ocho ocupaban la cabecera con ~62.000 pendientes entre todas. El informe ya
# IMPRIME el dato que las separa —«último consumo: nunca»— y no lo usaba para
# nada: ni para ordenar ni para marcar. Las dos marcas que sí existían eran una
# enumeración a mano de un hecho que el dato da entero.

def _monta(tmp_path, monkeypatch, reparto):
    """Un servicio con `reparto` = {nombre: nº de entradas dirigidas a él}.

    Cada agente lleva su propio rol (== su nombre) para que ① no los agrupe, y
    el autor `cto-A` no recibe nada: sólo firma.
    """
    roster = {"agentes": [{"nombre": "cto-A", "humano": "operador", "clave": "", "rol": "cto"}]
                         + [{"nombre": n, "humano": "operador", "clave": "", "rol": n}
                            for n in reparto],
              "humanos": [{"nombre": "operador", "alias": ["Operador"]}],
              "difusion": ["equipo"]}
    s = construir(tmp_path, monkeypatch, roster=roster)
    # El ledger se reescribe ENTERO: el de `construir` dirige todo a `backend`,
    # que aquí no está censado y ensuciaría el recuento.
    (tmp_path / "DEMO-LEDGER.md").write_text("".join(
        f"### [cto-A → {n} · REQUEST] {n} numero {i}\ncuerpo\n"
        for n, k in reparto.items() for i in range(k)))
    (tmp_path / "OTRO-LEDGER.md").write_text("### [cto-A · FYI] sin dirigir\ncuerpo\n")
    return s


def _filas_de_1(txt):
    """Los nombres de ①, EN ORDEN. Una fila se reconoce por su FORMA —nombre y
    número de pendientes— y no por «empieza con tres espacios»: la prosa del pie
    también sangra, y con ese filtro el helper colaba `alto` (de «…un número alto
    con consumo reciente…») como si fuera un agente. Un parser de arnés laxo
    afirma de más igual que el código que mide."""
    return [m.group(1) for m in
            re.finditer(r"^ {3}(\S+)\s+\d+\s{2}", _seccion(txt, 1), re.M)]


def _mira_todos(c, nombres):
    for n in nombres:
        c.get(f"/inbox/{n}")


def test_quien_nunca_ha_consumido_cae_por_debajo_del_que_si_drena(tmp_path, monkeypatch):
    """LA PROPIEDAD: `drena` tiene MENOS pendientes que `moroso`, y aun así va
    primero — porque de `moroso` no hay nadie de quien esperar que drene.

    FALSADOR: el `filas.sort(reverse=True)` de antes ordenaba por pendientes a
    secas; con él, `moroso` (2) sale por encima de `drena` (1) y esto cae."""
    s = _monta(tmp_path, monkeypatch, {"moroso": 2, "drena": 2})
    with TestClient(s.app) as c:
        s.barrido()
        c.headers.update({"X-Llminbox-Token": "test-token"})
        _mira_todos(c, ("moroso", "drena"))
        con = db_directa(s)
        arr = [r[0] for r in con.execute(
            "SELECT e.arrival FROM entries e JOIN recipients r "
            "ON r.ledger=e.ledger AND r.eid=e.eid "
            "WHERE r.who='drena' ORDER BY e.arrival")]
        assert len(arr) == 2, arr           # el arnés fija lo que la aserción usa
        c.post("/inbox/drena/leido", json={"hasta": {"demo-ledger": arr[0]}})
        s.barrido()
        filas = _filas_de_1(_texto(c))
    assert filas.index("drena") < filas.index("moroso"), filas


def test_entre_los_que_drenan_sigue_mandando_la_deuda(tmp_path, monkeypatch):
    """CONTROL de que el fix no se lleva por delante el ranking: con los dos
    cursores vivos, el que más debe sigue arriba.

    FALSADOR: si el orden pasara a ser SÓLO «¿tiene cursor?», los empates
    quedarían al azar del nombre y `mucho` caería debajo de `poco`."""
    s = _monta(tmp_path, monkeypatch, {"mucho": 3, "poco": 3})
    with TestClient(s.app) as c:
        s.barrido()
        c.headers.update({"X-Llminbox-Token": "test-token"})
        _mira_todos(c, ("mucho", "poco"))
        con = db_directa(s)
        arr = {q: [r[0] for r in con.execute(
            "SELECT e.arrival FROM entries e JOIN recipients r "
            "ON r.ledger=e.ledger AND r.eid=e.eid "
            "WHERE r.who=? ORDER BY e.arrival", (q,))] for q in ("mucho", "poco")}
        c.post("/inbox/mucho/leido", json={"hasta": {"demo-ledger": arr["mucho"][0]}})
        c.post("/inbox/poco/leido", json={"hasta": {"demo-ledger": arr["poco"][1]}})
        s.barrido()
        txt = _texto(c)
    filas = _filas_de_1(txt)
    assert filas.index("mucho") < filas.index("poco"), txt


def test_la_bandeja_que_nadie_ha_drenado_nunca_sale_marcada(tmp_path, monkeypatch):
    """La marca dice lo que el DATO sostiene («no hay cursor suyo»), no lo que a
    mí me consta por fuera («no lo corre nadie»): el servicio no sabe qué
    sesiones están vivas y no puede afirmarlo.

    FALSADOR doble: sin la marca, la fila de `moroso` es indistinguible de la de
    un agente al día; y si la marca se pusiera a todos, `drena` la llevaría."""
    s = _monta(tmp_path, monkeypatch, {"moroso": 2, "drena": 2})
    with TestClient(s.app) as c:
        s.barrido()
        c.headers.update({"X-Llminbox-Token": "test-token"})
        _mira_todos(c, ("moroso", "drena"))
        con = db_directa(s)
        arr = [r[0] for r in con.execute(
            "SELECT e.arrival FROM entries e JOIN recipients r "
            "ON r.ledger=e.ledger AND r.eid=e.eid "
            "WHERE r.who='drena' ORDER BY e.arrival")]
        c.post("/inbox/drena/leido", json={"hasta": {"demo-ledger": arr[0]}})
        s.barrido()
        sec = _seccion(_texto(c), 1)
    l_moroso = next(l for l in sec.splitlines() if l.strip().startswith("moroso "))
    l_drena = next(l for l in sec.splitlines() if l.strip().startswith("drena "))
    assert "nunca ha consumido" in l_moroso, l_moroso
    assert "nunca ha consumido" not in l_drena, l_drena


def test_el_corte_de_veinte_dice_cuantas_filas_esconde(tmp_path, monkeypatch):
    """① anuncia N agentes y lista 20. Con 21, cinco filas desaparecían sin una
    palabra — y el reordenado de arriba cambia CUÁLES desaparecen, así que
    callarlo pasa de descuido a mentira.

    FALSADOR: sin la línea del corte, la cabecera dice 21 y se leen 20."""
    s = _monta(tmp_path, monkeypatch, {f"a{i:02d}": 1 for i in range(21)})
    with TestClient(s.app) as c:
        s.barrido()
        c.headers.update({"X-Llminbox-Token": "test-token"})
        _mira_todos(c, [f"a{i:02d}" for i in range(21)])
        s.barrido()
        sec = _seccion(_texto(c), 1)
    assert "21 agente(s)" in sec, sec
    assert len(_filas_de_1(_texto(c))) == 20
    assert "1 fila(s) más" in sec, sec


def test_un_cursor_sin_sello_sigue_siendo_un_cursor(tmp_path, monkeypatch):
    """`cursors.updated` es NULLABLE. Medir la EXISTENCIA de cursor con
    `MAX(updated)` confunde «no hay fila» con «hay fila sin sello», y entonces la
    marca afirma «no existe ningún cursor suyo» de alguien que sí lo tiene — el
    mismo defecto que este cambio venía a arreglar, cometido al arreglarlo.

    (Medido: 0 de 132 cursores de producción tienen `updated` NULL hoy. El
    esquema lo permite y la frase lo afirma; se mide lo que se dice.)

    FALSADOR: volver a `ult is not None` como campo de existencia marca a
    `conSello` como si no tuviera cursor y esto se pone rojo."""
    s = _monta(tmp_path, monkeypatch, {"conSello": 2})
    with TestClient(s.app) as c:
        s.barrido()
        c.headers.update({"X-Llminbox-Token": "test-token"})
        c.get("/inbox/conSello")
        con = db_directa(s)
        con.execute("INSERT OR REPLACE INTO cursors VALUES (?,?,?,?)",
                    ("conSello", "demo-ledger", -1, None))   # fila SIN sello
        con.commit()
        s.barrido()
        sec = _seccion(_texto(c), 1)
    linea = next(l for l in sec.splitlines() if l.strip().startswith("conSello "))
    assert "no existe ningún cursor" not in linea, linea
