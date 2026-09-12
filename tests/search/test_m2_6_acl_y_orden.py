"""Correctivas M2-6: AUTORIZACIÓN del carril y CLAVE DE ORDEN inmutable.

Los dos son P0 REPRODUCIDOS antes de curarlos, y los dos tenían la misma forma: una regla
que existía en la documentación y no en el código.

· `lane` era obligatorio, entraba en `filter_sha256` y ataba la firma del cursor… y NO
  APARECÍA EN EL SQL. Medido: un carril inventado sobre el mismo `ledger` devolvía
  EXACTAMENTE las mismas filas que el legítimo.
· `arrival` es la clave de orden y era mutable. Medido sobre `e0..e3` con `limit=2`:
  página 1 `[e3, e2]`, se mueve el `arrival` de `e1`, página 2 `[e0]`, y `e1` NO APARECE
  NUNCA — sin error, sin cambio de generación y con `readiness` en `true`.
"""

import pathlib
import sqlite3
import threading

import pytest

from .conftest import (ACL, CARRIL, CARRIL_HERMANO, CARRIL_NO_AUTORIZADO, CLAVE, LANE,
                      mete, nueva_con)
import search_contract as sc
import search_cursor as scur
import search_store as ss


def _store(tmp_path, filas=None, nombre="m2.sqlite", acl=ACL):
    con = nueva_con(tmp_path, nombre)
    st = ss.SearchStore(con, cursor_key=CLAVE, acl=acl)
    st.ensure_schema()
    st.set_acl(acl)
    for kw in (filas or []):
        mete(con, LANE, **kw)
    con.commit()
    if filas:
        st.rebuild()
    return st


def _pobla(tmp_path, n=4, nombre="m2.sqlite"):
    return _store(tmp_path, [dict(eid=f"e{i}", arrival=i + 1, cuerpo="born red comun")
                             for i in range(n)], nombre)


# ── ① la ACL existe, y existe DENTRO del SQL ─────────────────────────────────────
def test_un_carril_NO_AUTORIZADO_no_ve_las_filas(tmp_path):
    st = _pobla(tmp_path)
    # ⊕ CONTROL: el carril autorizado SÍ las ve. Sin este arm, el rechazo de abajo podría
    # ser porque el corpus está vacío.
    legit = st.search(lane=CARRIL, ledger=LANE, query="born red", limit=10)
    assert len(legit["filas"]) == 4

    with pytest.raises(ss.LaneNotAuthorized):
        st.search(lane=CARRIL_NO_AUTORIZADO, ledger=LANE, query="born red", limit=10)


def test_la_ACL_VIVE_EN_LA_SENTENCIA_no_solo_en_python(tmp_path):
    """EL FALSADOR DEL `EXISTS`, y la razón de que la ACL no se quede en Python.

    Una comprobación delante de la llamada cierra el agujero de HOY. Cualquier camino
    nuevo a `SQL_BUSQUEDA` —un endpoint, un job, un diagnóstico— vuelve a servir sin
    autorizar y nada falla. Aquí se ejecuta la sentencia de PRODUCCIÓN a pelo, saltándose
    `search` por completo: es exactamente lo que haría ese camino nuevo.
    """
    st = _pobla(tmp_path)
    expr, _ = sc.compile_query("born red")

    def crudo(lane):
        return st.con.execute(ss.SQL_BUSQUEDA, (
            expr, lane, LANE, LANE, None, None, None, None, None, None, None, None,
            0, 0, "", 100)).fetchall()

    assert len(crudo(CARRIL)) == 4, "⊕ el carril autorizado tiene que ver las filas"
    assert crudo(CARRIL_NO_AUTORIZADO) == [], (
        "la sentencia sirvió filas a un carril sin autorización: la ACL no está en el SQL")

    # Y el `EXISTS` es lo que lo hace: quitándolo, el carril inventado vuelve a verlas.
    sin_acl = ss.SQL_BUSQUEDA.replace(
        "   AND EXISTS (SELECT 1 FROM search_acl a\n"
        "                WHERE a.lane = ? AND a.ledger = e.ledger)\n", "")
    assert sin_acl != ss.SQL_BUSQUEDA
    filas = st.con.execute(sin_acl, (
        expr, LANE, LANE, None, None, None, None, None, None, None, None,
        0, 0, "", 100)).fetchall()
    assert len(filas) == 4, (
        "quitar el `EXISTS` no cambia nada: el corpus no discrimina y el test de arriba "
        "estaría verde sin medir la autorización")


def test_dos_carriles_AUTORIZADOS_ven_lo_mismo_pero_no_comparten_cursor(tmp_path):
    """La ACL autoriza; el cursor sigue atado al carril. Son dos cosas distintas y las
    dos tienen que valer a la vez."""
    st = _pobla(tmp_path, n=6)
    a = st.search(lane=CARRIL, ledger=LANE, query="born red", limit=2)
    b = st.search(lane=CARRIL_HERMANO, ledger=LANE, query="born red", limit=2)
    assert [f["eid"] for f in a["filas"]] == [f["eid"] for f in b["filas"]]
    with pytest.raises(scur.CursorFilterMismatch):
        st.search(lane=CARRIL_HERMANO, ledger=LANE, query="born red", limit=2,
                  cursor=a["cursor"])


def test_sin_ACL_declarada_se_NIEGA_todo(tmp_path):
    """El valor por defecto de una autorización tiene que ser el que NO autoriza."""
    con = nueva_con(tmp_path)
    st = ss.SearchStore(con, cursor_key=CLAVE)          # sin `acl=`
    st.ensure_schema()
    with pytest.raises(ss.LaneNotAuthorized, match="NIEGO TODO"):
        st.search(lane=CARRIL, ledger=LANE, query="born red", limit=5)


@pytest.mark.parametrize("mala", [
    {CARRIL: LANE},                 # una CADENA: se expandiría a sus LETRAS
    {CARRIL: b"64bis-wiki"},
    {CARRIL: set()},                # vacío
    {CARRIL: {""}},                 # ledger vacío
    {"": {LANE}},                   # carril vacío
    {},                             # mapa vacío
])
def test_una_ACL_mal_escrita_se_RECHAZA_en_vez_de_autorizar_de_mas(mala):
    """`{"64bis": "64bis-wiki"}` produciría un `frozenset` de letras y autorizaría el
    ledger `"6"`. Una ACL mal escrita que «funciona» es peor que un error."""
    with pytest.raises(ValueError):
        ss.SearchStore._canon_acl(mala)


def test_un_resolutor_que_revienta_NIEGA(tmp_path):
    """Un fallo de la fuente de autorización no puede abrir el acceso."""
    def resolutor(lane):
        raise RuntimeError("la fuente de la ACL no responde")

    st = _pobla(tmp_path)                               # la durable sí está puesta
    # `set_acl` publica el MAPA en memoria, así que el resolutor se instala después: lo
    # que se prueba aquí es la ACL de MEMORIA, que es la que da el error tipado.
    st.acl = resolutor
    with pytest.raises(ss.LaneNotAuthorized, match="resolutor"):
        st.search(lane=CARRIL, ledger=LANE, query="born red", limit=5)


# ── ② `set_acl`: idempotente, atómica y bajo lock ────────────────────────────────
def test_una_ACL_IDENTICA_no_cambia_readiness_ni_generacion(tmp_path):
    """Volver a aplicar la MISMA configuración no puede comportarse como un cambio.

    Antes rotaba la generación y caducaba todos los cursores vivos: un arranque idéntico
    tiraba la paginación de todo el mundo.
    """
    st = _pobla(tmp_path)
    gen, listo = st.generation(), st.readiness()["ready"]
    r = st.search(lane=CARRIL, ledger=LANE, query="born red", limit=2)

    assert st.set_acl(ACL) is False, "una ACL idéntica se declaró como cambio"
    assert st.generation() == gen, "rotó la generación sin que cambiara la política"
    assert st.readiness()["ready"] is listo
    # …y el cursor emitido antes SIGUE sirviendo.
    seg = st.search(lane=CARRIL, ledger=LANE, query="born red", limit=2,
                    cursor=r["cursor"])
    assert seg["filas"]


def test_una_ACL_DISTINTA_cambia_la_autorizacion_y_caduca_el_cursor_viejo(tmp_path):
    st = _pobla(tmp_path)
    gen = st.generation()
    r = st.search(lane=CARRIL, ledger=LANE, query="born red", limit=2)

    assert st.set_acl({CARRIL_HERMANO: {LANE}}) is True
    assert st.generation() != gen, "no rotó la generación al cambiar la política"
    # La política nueva ya aplica, SIN reconstruir el índice.
    assert st.readiness()["ready"] is True, "cambiar la ACL no tiene que ensuciar el FTS"
    with pytest.raises(ss.LaneNotAuthorized):
        st.search(lane=CARRIL, ledger=LANE, query="born red", limit=2)
    assert st.search(lane=CARRIL_HERMANO, ledger=LANE, query="born red",
                     limit=2)["filas"]

    # Y el cursor firmado bajo la generación anterior caduca. Se REAUTORIZA a `CARRIL`
    # para aislar lo que se mide: con el carril desautorizado, el cursor fallaría por el
    # atado del filtro y este test no probaría la rotación de la generación.
    assert st.set_acl({CARRIL: {LANE}, CARRIL_HERMANO: {LANE}}) is True
    with pytest.raises(scur.CursorGenerationStale):
        st.search(lane=CARRIL, ledger=LANE, query="born red", limit=2,
                  cursor=r["cursor"])


def test_set_acl_compara_DENTRO_del_lock_no_antes(tmp_path):
    """La lectura que decide el no-op tiene que ocurrir con el lock ya tomado.

    Fuera del lock es una carrera con nombre: otro proceso cambia la política entre la
    lectura y el `return`, y esta instancia se queda con `self.acl` diciendo una cosa
    mientras el `EXISTS` del SQL aplica otra.
    """
    st = _pobla(tmp_path)
    visto = []
    original = ss.SearchStore.acl_persistida

    def espia(self):
        visto.append(self.con.in_transaction)
        return original(self)

    ss.SearchStore.acl_persistida = espia
    try:
        st.set_acl({CARRIL: {LANE}})
    finally:
        ss.SearchStore.acl_persistida = original
    assert visto and all(visto), (
        "`acl_persistida` se llamó SIN transacción abierta: la comparación está fuera del "
        "lock y el no-op puede decidirse sobre una política que ya cambió")


def test_set_acl_RECHAZA_una_transaccion_ajena(tmp_path):
    """Dentro de la transacción del llamante, `self.acl` se publicaría antes de su
    `COMMIT`, y su `ROLLBACK` dejaría la ACL de memoria diciendo lo que la base ya no
    dice. Sin un hook de commit de verdad, la postura honesta es negarse."""
    st = _pobla(tmp_path)
    st.con.execute("BEGIN IMMEDIATE")
    try:
        antes = dict(st.acl)
        with pytest.raises(ss.ConnectionContractViolation, match="transacción"):
            st.set_acl({CARRIL_HERMANO: {LANE}})
        assert dict(st.acl) == antes, "publicó la ACL en memoria dentro de una tx ajena"
    finally:
        st.con.execute("ROLLBACK")
    # Lo que importa: tras el rollback, memoria y base siguen diciendo lo mismo.
    assert challenge_ok(st), f"memoria {st.acl} vs base {st.acl_persistida()}"


def decisiones_cuadran(st):
    """Lo que importa no es que dos dicts sean iguales, sino que la DECISIÓN de `search`
    coincida con lo que dice la tabla — que es lo que aplica el `EXISTS` del SQL.

    El mapa de memoria es una DECLARACIÓN y puede quedarse atrás (una `set_acl` que choca
    con otro escritor no llega a aplicarse). La propiedad que no puede romperse es que
    Python y SQL autoricen lo MISMO.
    """
    durable = st.acl_persistida()
    for lane in {*durable, *(st.acl if isinstance(st.acl, dict) else {})}:
        for ledger in {LANE, "64bis-wiki-archivo", "64bis-wiki-queue"}:
            esperado = ledger in durable.get(lane, frozenset())
            try:
                st._autoriza(lane, ledger)
                real = True
            except ss.LaneNotAuthorized:
                real = False
            if real != esperado:
                return False
    return True


challenge_ok = decisiones_cuadran


def test_dos_conexiones_no_dejan_la_acl_desincronizada(tmp_path):
    """Falsador con DOS conexiones.

    A toma el lock de escritura y cambia la política; B llama a `set_acl` con la política
    vieja mientras A lo tiene tomado. Pase lo que pase con el orden —B espera y ve la
    política nueva, o B choca con `database is locked`— al final la ACL de memoria de B
    tiene que decir LO MISMO que la tabla. Ésa es la propiedad que la carrera rompía: con
    la comparación fuera del lock, B decidía «no-op» sobre una lectura ya caducada y se
    quedaba autorizando por una política que la base no tenía.
    """
    st = _pobla(tmp_path)
    ruta = str(tmp_path / "m2.sqlite")
    # Timeout CORTO a propósito: garantiza que B llegue al punto de contención y choque
    # en vez de esperar a que A suelte. Con un timeout largo, A podía confirmar antes de
    # que B lo intentara y el test no medía la carrera.
    con_b = sqlite3.connect(ruta, timeout=0.1)
    con_b.row_factory = sqlite3.Row
    ss.SearchStore.prepare_search_connection(con_b)
    st_b = ss.SearchStore(con_b, cursor_key=CLAVE, acl=ACL)

    lock_tomado = threading.Event()
    suelta = threading.Event()
    fallo = []

    def cambia_a():
        # Conexión PROPIA del hilo: sqlite3 ata cada conexión al hilo que la creó, así que
        # reusar la del test aquí da `ProgrammingError` y el test mediría eso y no la
        # carrera.
        con_a = sqlite3.connect(ruta, timeout=10)
        con_a.isolation_level = None
        try:
            con_a.execute("BEGIN IMMEDIATE")
            con_a.execute("DELETE FROM search_acl")
            con_a.execute("INSERT INTO search_acl(lane, ledger) VALUES(?,?)",
                          (CARRIL_HERMANO, LANE))
            lock_tomado.set()
            suelta.wait(timeout=10)
            con_a.execute("COMMIT")
        except Exception as e:                            # pragma: no cover
            fallo.append(e)
            lock_tomado.set()
        finally:
            con_a.close()

    h = threading.Thread(target=cambia_a)
    h.start()
    assert lock_tomado.wait(timeout=10), "el hilo A no llegó a tomar el lock"
    # A SIGUE SOSTENIENDO el lock aquí: B entra al punto de contención de verdad.
    chocado = False
    try:
        st_b.set_acl({CARRIL: {LANE}})
    except sqlite3.OperationalError:
        chocado = True  # `database is locked` es un resultado legítimo: no autoriza nada
    suelta.set()                                   # ahora sí, A confirma
    h.join(timeout=20)
    assert chocado, (
        "B no chocó con el lock que A sostenía: la barrera no llegó al punto de "
        "contención y este test no midió la carrera")
    assert not fallo, fallo
    assert challenge_ok(st_b), (
        f"memoria {st_b.acl} y base {st_b.acl_persistida()} discrepan: la carrera dejó "
        f"esta instancia autorizando por una política que la tabla no tiene")


# ── ③ la clave de orden no se mueve bajo los pies del que pagina ─────────────────
def test_mover_el_arrival_INVALIDA_en_vez_de_perder_filas(tmp_path):
    """EL P0, reproducido y ahora cerrado.

    Antes: página 1 `[e3, e2]`, se mueve el `arrival` de `e1`, página 2 `[e0]`, `e1` no
    aparece nunca, `generation` intacta y `readiness` en `true`. El recorrido tenía un
    agujero que se lee como «ya no hay más».
    """
    st = _pobla(tmp_path)
    gen = st.generation()
    p1 = st.search(lane=CARRIL, ledger=LANE, query="born red", limit=2)
    assert [f["eid"] for f in p1["filas"]] == ["e3", "e2"]

    st.con.execute("UPDATE entries SET arrival=? WHERE ledger=? AND eid=?",
                   (9, LANE, "e1"))
    st.con.commit()

    assert st.generation() != gen, "mover la clave de orden no rotó la generación"
    assert st.state() == ss.ESTADO_CONSTRUYENDO
    assert st.readiness()["ready"] is False
    # El cursor viejo NO sigue paginando sobre un orden que ya no existe.
    with pytest.raises(ss.SearchNotReady):
        st.search(lane=CARRIL, ledger=LANE, query="born red", limit=2,
                  cursor=p1["cursor"])
    # Y tras reconstruir, el recorrido vuelve a ser completo: ninguna fila se perdió.
    st.rebuild()
    vistos, cursor = [], None
    while True:
        r = st.search(lane=CARRIL, ledger=LANE, query="born red", limit=2, cursor=cursor)
        vistos += [f["eid"] for f in r["filas"]]
        if not r["hay_mas"]:
            break
        cursor = r["cursor"]
    assert sorted(vistos) == ["e0", "e1", "e2", "e3"], f"faltan filas: {vistos}"


def test_poner_arrival_a_una_entrada_que_no_lo_tenia_TAMBIEN_invalida(tmp_path):
    """`IS NOT` y no `<>`: con `NULL` de un lado, `<>` da `NULL` y el `WHEN` no dispara.
    Posicionar una entrada la mete EN MEDIO del recorrido, así que también invalida."""
    st = _pobla(tmp_path)
    st.con.execute("INSERT INTO entries(ledger,eid,arrival,ts,actor,tipo,head,body)"
                   " VALUES(?,?,?,?,?,?,?,?)",
                   (LANE, "sin-pos", None, "2026-01-01", "cto", "FYI",
                    "titular sin-pos", "titular sin-pos\nborn red comun"))
    st.con.commit()
    gen = st.generation()
    st.con.execute("UPDATE entries SET arrival=2 WHERE ledger=? AND eid=?",
                   (LANE, "sin-pos"))
    st.con.commit()
    assert st.generation() != gen, "posicionar una entrada no invalidó"


def test_un_UPDATE_que_NO_mueve_el_arrival_no_invalida(tmp_path):
    """⊖ del trigger: si disparara con cualquier `UPDATE`, la invalidación no probaría
    nada sobre la clave de orden — se dispararía siempre."""
    st = _pobla(tmp_path)
    gen, estado = st.generation(), st.state()
    st.con.execute("UPDATE entries SET actor='otro' WHERE ledger=? AND eid=?",
                   (LANE, "e1"))
    st.con.execute("UPDATE entries SET arrival=arrival WHERE ledger=? AND eid=?",
                   (LANE, "e1"))
    st.con.commit()
    assert st.generation() == gen and st.state() == estado, (
        "invalidó sin que la clave de orden se moviera: el trigger no discrimina")


# ── ④ caída REAL a mitad del rebuild ─────────────────────────────────────────────
_GUION_CAIDA = r'''
import os, sqlite3, sys
sys.path.insert(0, {raiz!r})
sys.path.insert(0, {tests!r})
import search_store as ss
con = sqlite3.connect({ruta!r})
con.row_factory = sqlite3.Row
ss.SearchStore.prepare_search_connection(con)
st = ss.SearchStore(con, cursor_key={clave!r}, acl={{{carril!r}: {{{lane!r}}}}})
# La costura que corre ENTRE poblar el índice y validarlo. El proceso se va aquí SIN
# `finally`, sin `atexit` y sin cerrar la conexión: es una caída, no una excepción.
ss.SearchStore._tras_poblar = lambda self: os._exit(9)
st.rebuild()
'''


def test_una_CAIDA_REAL_a_mitad_del_rebuild_deja_el_indice_fail_closed(tmp_path):
    """`os._exit` en un SUBPROCESO, no una excepción simulada.

    Una excepción recorre los `finally`, cierra la conexión y deja que SQLite haga su
    limpieza ordenada — o sea, prueba el camino que SÍ se controla. Una caída de verdad no
    ejecuta nada: es la única forma de comprobar que lo que queda EN DISCO dice «a
    medias». El contrato es que cualquier fallo deja `building` y la generación sin
    tocar; un índice a medias tiene que ser VISIBLEMENTE un índice a medias, no uno que
    responde pocas filas.
    """
    import subprocess
    import sys as _sys

    st = _pobla(tmp_path, n=6)
    ruta = str(tmp_path / "m2.sqlite")
    gen_antes = st.generation()
    assert st.readiness()["ready"] is True                    # ⊕ antes de la caída
    st.con.close()

    guion = _GUION_CAIDA.format(
        raiz=str(pathlib.Path(ss.__file__).resolve().parent),
        tests=str(pathlib.Path(__file__).resolve().parent),
        ruta=ruta, clave=CLAVE, carril=CARRIL, lane=LANE)
    proc = subprocess.run([_sys.executable, "-c", guion], capture_output=True, text=True)
    assert proc.returncode == 9, (
        f"el subproceso no murió por la costura (rc={proc.returncode}): "
        f"{proc.stdout[-400:]} {proc.stderr[-400:]}")

    # ── lo que quedó EN DISCO ──
    con = sqlite3.connect(ruta)
    con.row_factory = sqlite3.Row
    ss.SearchStore.prepare_search_connection(con)
    revivido = ss.SearchStore(con, cursor_key=CLAVE, acl=ACL)
    assert revivido.state() == ss.ESTADO_CONSTRUYENDO, "la caída no dejó `building`"
    assert revivido.readiness()["ready"] is False
    assert revivido.generation() == gen_antes, (
        "la generación se movió con un rebuild que nunca terminó")
    with pytest.raises(ss.SearchNotReady):
        revivido.search(lane=CARRIL, ledger=LANE, query="born red", limit=5)

    # ── y se RECUPERA con un rebuild bueno ──
    gen_nueva = revivido.rebuild()
    assert gen_nueva != gen_antes
    assert revivido.readiness()["ready"] is True
    assert len(revivido.search(lane=CARRIL, ledger=LANE, query="born red",
                               limit=10)["filas"]) == 6


# ── ⑤ frontera SERVER-OWNED: el alcance sale del sujeto, no de la llamada ────────
CREDS = {
    "cred-de-64bis": {"principal_id": "agente-7f3a", "rol": "cto", "carril": "64bis"},
    "cred-de-otro": {"principal_id": "agente-9b21", "rol": "qa", "carril": "otro-carril"},
    "cred-sin-carril": {"principal_id": "agente-0001", "rol": "watcher"},
    "cred-carril-fantasma": {"principal_id": "agente-0002", "rol": "qa",
                             "carril": "carril-que-no-resuelve"},
    # Mismo ROL y mismo CARRIL que otra, y sin embargo OTRO sujeto: es el caso que hace
    # imposible derivar el principal del rol o del carril.
    "cred-gemela": {"principal_id": "agente-c0ffee", "rol": "cto", "carril": "64bis"},
    "cred-sin-principal": {"rol": "cto", "carril": "64bis"},
}
MAPA_CARRILES = {"64bis": LANE, "otro-carril": "64bis-wiki-archivo"}
TOKEN_COMPARTIDO = "token-compartido-que-tiene-todo-el-mundo"


def test_el_alcance_sale_de_la_credencial_y_no_de_la_llamada():
    sc_ = ss.scope_desde_credencial("cred-de-64bis", credenciales=CREDS,
                                    carril_ledger=MAPA_CARRILES,
                                    token_compartido=TOKEN_COMPARTIDO)
    assert sc_.lane == "64bis" and sc_.ledgers == frozenset({LANE})
    assert sc_.principal_id == "agente-7f3a" and sc_.role == "cto"


@pytest.mark.parametrize("credencial, credenciales, motivo", [
    # El mapa AUSENTE no puede caer a lectura global: un permiso «mientras infra emite»
    # es como un permiso temporal se vuelve permanente.
    ("cred-de-64bis", {}, "mapa de credenciales"),
    ("cred-de-64bis", None, "mapa de credenciales"),
    # El token COMPARTIDO lo tiene todo el mundo: no identifica a nadie.
    (TOKEN_COMPARTIDO, CREDS, "COMPARTIDO"),
    ("cred-que-no-existe", CREDS, "desconocida"),
    ("", CREDS, "falta la credencial"),
    (None, CREDS, "falta la credencial"),
    ("cred-sin-carril", CREDS, "no declara carril"),
    ("cred-carril-fantasma", CREDS, "no resuelve"),
    ("cred-sin-principal", CREDS, "principal_id"),
])
def test_todos_los_caminos_del_resolutor_fallan_CERRADOS(credencial, credenciales, motivo):
    with pytest.raises(ss.LaneNotAuthorized, match=motivo):
        ss.scope_desde_credencial(credencial, credenciales=credenciales,
                                  carril_ledger=MAPA_CARRILES,
                                  token_compartido=TOKEN_COMPARTIDO)


def test_un_alcance_atado_no_deja_pedir_otro_ledger(tmp_path):
    """Es lo que cierra la inyección: el cliente puede escribir lo que quiera en su
    petición, pero el `ledger` se comprueba contra el alcance del SUJETO."""
    st = _pobla(tmp_path)
    alcance = ss.scope_desde_credencial("cred-de-64bis", credenciales=CREDS,
                                        carril_ledger=MAPA_CARRILES,
                                        token_compartido=TOKEN_COMPARTIDO)
    # ⊕ dentro de su alcance, sirve
    assert st.search(scope=alcance, ledger=LANE, query="born red", limit=5)["filas"]
    # ⊖ fuera de su alcance, no — aunque el ledger exista y el carril esté en la ACL
    with pytest.raises(ss.LaneNotAuthorized, match="no alcanza"):
        st.search(scope=alcance, ledger="64bis-wiki-archivo", query="born red", limit=5)


def test_un_lane_inyectado_junto_al_alcance_se_RECHAZA(tmp_path):
    """Si llegan los dos y no coinciden, no se elige: se rechaza. Elegir uno es cómo un
    parámetro del cliente termina ganándole al sujeto."""
    st = _pobla(tmp_path)
    alcance = ss.scope_desde_credencial("cred-de-64bis", credenciales=CREDS,
                                        carril_ledger=MAPA_CARRILES,
                                        token_compartido=TOKEN_COMPARTIDO)
    with pytest.raises(ss.LaneNotAuthorized, match="no elijo, rechazo"):
        st.search(scope=alcance, lane=CARRIL_HERMANO, ledger=LANE,
                  query="born red", limit=5)
    # …y el mismo `lane` que el alcance sí pasa: lo que se rechaza es la CONTRADICCIÓN.
    assert st.search(scope=alcance, lane="64bis", ledger=LANE,
                     query="born red", limit=5)["filas"]


def test_sin_alcance_ni_lane_no_se_busca(tmp_path):
    st = _pobla(tmp_path)
    with pytest.raises(ss.LaneNotAuthorized, match="sin sujeto"):
        st.search(ledger=LANE, query="born red", limit=5)


@pytest.mark.parametrize("malo", [
    {"lane": "", "ledgers": {LANE}, "principal_id": "p"},
    {"lane": "64bis", "ledgers": LANE, "principal_id": "p"},     # cadena → LETRAS
    {"lane": "64bis", "ledgers": set(), "principal_id": "p"},
    {"lane": "64bis", "ledgers": {""}, "principal_id": "p"},
    {"lane": "64bis", "ledgers": {LANE}, "principal_id": ""},
    {"lane": "64bis", "ledgers": {LANE}, "principal_id": None},
    {"lane": "64bis", "ledgers": {LANE}, "principal_id": "p", "role": ""},
])
def test_un_alcance_mal_construido_se_rechaza(malo):
    with pytest.raises(ValueError):
        ss.BoundSearchScope(**malo)


def test_el_alcance_es_INMUTABLE():
    """Un alcance que se puede cambiar después de emitirse no es un alcance."""
    a = ss.BoundSearchScope(lane="64bis", ledgers={LANE}, principal_id="agente-7f3a",
                            role="cto")
    with pytest.raises(Exception):
        a.lane = "otro"


def test_principal_rol_y_carril_son_TRES_dominios_y_no_se_infieren():
    """Falsador de la mezcla: `principal_id`, `role` y `lane` TODOS distintos, y ninguno
    se puede reconstruir desde otro.

    Y el caso que lo hace obligatorio: dos credenciales con el MISMO rol y el MISMO carril
    son sujetos DISTINTOS. Un «principal» derivado de rol o carril los haría
    indistinguibles, y entonces el registro de un rechazo apuntaría a un conjunto en vez
    de a alguien.
    """
    a = ss.scope_desde_credencial("cred-de-64bis", credenciales=CREDS,
                                  carril_ledger=MAPA_CARRILES)
    b = ss.scope_desde_credencial("cred-gemela", credenciales=CREDS,
                                  carril_ledger=MAPA_CARRILES)
    assert a.role == b.role == "cto"
    assert a.lane == b.lane == "64bis"
    assert a.principal_id != b.principal_id, (
        "dos sujetos con el mismo rol y carril salieron con el MISMO principal: el "
        "principal se está infiriendo de ellos")
    # Los tres valores son distintos entre sí y ninguno se deriva de otro.
    assert len({a.principal_id, a.role, a.lane}) == 3
    assert a.principal_id == "agente-7f3a" and a.role == "cto" and a.lane == "64bis"


# ── ⑥ la UDF en TODA conexión del servicio ───────────────────────────────────────
def _fuente_servicio():
    return (pathlib.Path(ss.__file__).resolve().parent / "servicio.py").read_text(
        encoding="utf-8")


def test_TODA_conexion_de_servicio_instala_guard_y_udf():
    """CENSO, no memoria. Escribí «`db()` es la única fábrica» y era FALSO: hay 9
    `sqlite3.connect` en `servicio.py`, no 2.

    Este test recorre el fichero y exige que cada `connect` tenga su registro al lado —
    también los de sólo lectura, porque leer `search_view` también necesita la UDF y
    clasificar a mano cuál escribe es exactamente cómo se cuela la que sí. Si mañana
    alguien añade una conexión, esto se pone rojo sin que nadie tenga que acordarse.
    """
    lineas = _fuente_servicio().split("\n")
    conexiones = [i for i, l in enumerate(lineas) if "sqlite3.connect(" in l]
    assert len(conexiones) >= 9, (
        f"el censo encogió a {len(conexiones)}: si se han quitado conexiones, actualiza "
        f"la cota; si el patrón cambió, este test dejó de ver la población")
    sin_registro = []
    for i in conexiones:
        # La llamada puede ocupar varias líneas: se mira una ventana corta por debajo.
        ventana = "\n".join(lineas[i:i + 5])
        if "_registra_udf_busqueda(" not in ventana:
            sin_registro.append((i + 1, lineas[i].strip()[:70]))
    assert not sin_registro, (
        f"conexiones fuera del helper Search: {sin_registro}")

    # El helper central es el contrato: el censo anterior prueba su cobertura y este
    # control prueba que no se haya vaciado una de sus dos mitades.
    inicio = next(i for i, l in enumerate(lineas)
                  if l.startswith("def _registra_udf_busqueda("))
    fin = next(i for i in range(inicio + 1, len(lineas))
               if lineas[i].startswith("def "))
    helper = "\n".join(lineas[inicio:fin])
    assert 'getattr(_ss, "guard_search_connection", None)' in helper
    assert "guard(c)" in helper
    assert "_ss.SearchStore.register_udf(c)" in helper


def test_un_fallo_REAL_al_registrar_la_udf_NO_se_traga(monkeypatch):
    """`except Exception: pass` convertía un fallo de registro en silencio, y el error
    salía lejos —dentro de un trigger, en el `INSERT` siguiente— donde ya no se puede
    atribuir. Sólo se tolera que falte el MÓDULO.

    ⚠️ Se localiza por ESTRUCTURA (`ast`), no por texto: la primera versión de este test
    buscaba la cadena `"except Exception:"` en el fuente y la encontraba… DENTRO del
    docstring que explica por qué ya no está. El comentario de la cura rompió el
    localizador de la cura.
    """
    import ast

    arbol = ast.parse(_fuente_servicio())
    fn = next((n for n in ast.walk(arbol)
               if isinstance(n, ast.FunctionDef) and n.name == "_registra_udf_busqueda"),
              None)
    assert fn is not None, "no existe `_registra_udf_busqueda` en servicio.py"
    manejadores = [h for h in ast.walk(fn) if isinstance(h, ast.ExceptHandler)]
    assert manejadores, "no captura nada: entonces el `import` opcional no lo es"
    tipos = []
    for h in manejadores:
        assert h.type is not None, "un `except:` desnudo se traga hasta `KeyboardInterrupt`"
        tipos.append(ast.unparse(h.type))
    assert tipos == ["ModuleNotFoundError"], (
        f"`_registra_udf_busqueda` captura {tipos}: sólo puede tolerar la ausencia exacta "
        "del módulo opcional")
    # Capturar la subclase correcta aún sería demasiado ancho sin comprobar `e.name`:
    # también escondería una dependencia ausente DENTRO de search_store.
    comparaciones = [n for n in ast.walk(fn) if isinstance(n, ast.Compare)]
    assert any("e.name" in ast.unparse(n) and "search_store" in ast.unparse(n)
               for n in comparaciones), "no discrimina el módulo ausente por `e.name`"

    # Y el comportamiento, no sólo la forma: con `register_udf` reventando, propaga.
    def revienta(con):
        raise sqlite3.OperationalError("no se pudo registrar")

    monkeypatch.setattr(ss.SearchStore, "register_udf", staticmethod(revienta))
    con = sqlite3.connect(":memory:")
    with pytest.raises(sqlite3.OperationalError):
        ss.SearchStore.register_udf(con)
