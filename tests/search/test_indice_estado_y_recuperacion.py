"""Acoplamiento estable, estado del índice y sus modos de fallo."""

import json
import sqlite3

import pytest

from .conftest import ACL, CARRIL, CLAVE, ESQUEMA_ENTRIES, HERMANOS, LANE, mete, nueva_con
import search_contract as sc
import search_cursor as scur
import search_store as ss


# ── acoplamiento por `rid`, no por `entries.rowid` ────────────────────────────────
def test_borrar_y_reinsertar_LA_MISMA_clave_conserva_su_rid(poblado):
    """La lápida es lo que hace estable el `rid` de una entrada que va y vuelve.

    El reindexador del markdown borra y reinserta entradas cada vez que un fichero se
    reordena. Si el mapeo se borrara con la entrada, cada reordenación le daría un `rid`
    nuevo a la MISMA entrada: el acoplamiento se movería solo.
    """
    con = poblado.con
    diana = f"{LANE}-e0"
    rid0 = con.execute("SELECT rid FROM search_documents WHERE ledger=? AND eid=?",
                       (LANE, diana)).fetchone()["rid"]
    con.execute("DELETE FROM entries WHERE ledger=? AND eid=?", (LANE, diana))
    con.commit()
    assert con.execute("SELECT rid FROM search_documents WHERE ledger=? AND eid=?",
                       (LANE, diana)).fetchone()["rid"] == rid0, "la lápida se borró"
    assert diana not in [f["eid"] for f in poblado.search(
        lane=CARRIL, ledger=LANE, query="born red", limit=50)["filas"]], (
        "la entrada borrada sigue saliendo: los tokens no se purgaron")
    mete(con, LANE, diana, 4242, actor="cto", cuerpo="born red comun")
    con.commit()
    rid1 = con.execute("SELECT rid FROM search_documents WHERE ledger=? AND eid=?",
                       (LANE, diana)).fetchone()["rid"]
    assert rid1 == rid0, "al volver, la MISMA entrada recibió un `rid` distinto"
    assert diana in [f["eid"] for f in poblado.search(
        lane=CARRIL, ledger=LANE, query="born red", limit=50)["filas"]]


def test_una_clave_DISTINTA_no_hereda_el_rid_de_una_muerta(poblado):
    """⊕ del otro lado: `AUTOINCREMENT` impide que un `rid` liberado por el `rid` máximo
    se le entregue a una clave nueva. Se usa el MÁXIMO a propósito: sin `AUTOINCREMENT`
    SQLite sólo recicla ése, así que con cualquier otro el test no discriminaría."""
    con = poblado.con
    fila = con.execute("SELECT rid, ledger, eid FROM search_documents"
                       " ORDER BY rid DESC LIMIT 1").fetchone()
    rid_max, lg, victima = fila["rid"], fila["ledger"], fila["eid"]
    con.execute("DELETE FROM entries WHERE ledger=? AND eid=?", (lg, victima))
    con.execute("DELETE FROM search_documents WHERE ledger=? AND eid=?", (lg, victima))
    con.commit()
    mete(con, LANE, "clave-nueva", 8888, actor="cto", cuerpo="born red comun")
    con.commit()
    rid_n = con.execute("SELECT rid FROM search_documents WHERE eid='clave-nueva'"
                        ).fetchone()["rid"]
    assert rid_n != rid_max, (
        "una clave NUEVA heredó el `rid` de una muerta: heredaría también sus tokens")


def test_update_del_cuerpo_conserva_el_rid_y_reindexa(poblado):
    con = poblado.con
    diana = f"{LANE}-e1"
    rid0 = con.execute("SELECT rid FROM search_documents WHERE ledger=? AND eid=?",
                       (LANE, diana)).fetchone()["rid"]
    con.execute("UPDATE entries SET body=? WHERE ledger=? AND eid=?",
                (f"titular {diana}\npalabra-nuevisima", LANE, diana))
    con.commit()
    rid1 = con.execute("SELECT rid FROM search_documents WHERE ledger=? AND eid=?",
                       (LANE, diana)).fetchone()["rid"]
    assert rid1 == rid0, "un update de cuerpo movió el `rid`: el acoplamiento no es estable"
    assert [f["eid"] for f in poblado.search(
        lane=CARRIL, ledger=LANE, query="palabra-nuevisima", limit=5)["filas"]] == [diana]
    assert diana not in [f["eid"] for f in poblado.search(
        lane=CARRIL, ledger=LANE, query="born red", limit=50)["filas"]], "el texto viejo sigue indexado"


def test_update_que_cambia_la_clave_da_rid_nuevo(poblado):
    con = poblado.con
    rid0 = con.execute("SELECT rid FROM search_documents WHERE eid=?",
                       (f"{LANE}-e2",)).fetchone()["rid"]
    con.execute("UPDATE entries SET eid='renombrada' WHERE ledger=? AND eid=?",
                (LANE, f"{LANE}-e2"))
    con.commit()
    fila = con.execute("SELECT rid FROM search_documents WHERE eid='renombrada'").fetchone()
    assert fila is not None and fila["rid"] != rid0
    # Cambiar `eid` también mueve el keyset `(arrival, eid)`: el trigger mantiene el
    # acoplamiento, pero no puede dejar vivos cursores emitidos bajo el orden anterior.
    with pytest.raises(ss.SearchNotReady):
        poblado.search(lane=CARRIL, ledger=LANE, query="born red", limit=50)
    poblado.rebuild()
    assert "renombrada" in [f["eid"] for f in poblado.search(
        lane=CARRIL, ledger=LANE, query="born red", limit=50)["filas"]]


def test_integrity_check_rank1_caza_la_desincronizacion(poblado):
    """⊖ del propio validador: sin `rank=1`, `integrity-check` pasa VERDE con el
    contenido cambiado por debajo. Con `rank=1`, revienta."""
    poblado.con.execute("DROP TRIGGER search_au_body")
    poblado.con.execute("UPDATE entries SET body='texto que el indice no vio' "
                        "WHERE ledger=? AND eid=?", (LANE, f"{LANE}-e3"))
    poblado.con.commit()
    poblado.con.execute("INSERT INTO search_fts(search_fts) VALUES('integrity-check')")
    with pytest.raises(sqlite3.DatabaseError):
        poblado.con.execute(
            "INSERT INTO search_fts(search_fts, rank) VALUES('integrity-check', 1)")


# ── ausentes ──────────────────────────────────────────────────────────────────────
def test_las_ausentes_NO_se_sirven_y_no_hay_modo_de_incluirlas(poblado):
    """v0.9 excluye SIEMPRE. No hay perilla, y el test lo comprueba en los dos sentidos:
    que la retirada desaparece, y que la firma de `search` no admite pedirla."""
    import inspect

    con = poblado.con
    antes = len(poblado.search(lane=CARRIL, ledger=LANE, query="born red",
                               limit=50)["filas"])
    con.execute("UPDATE entries SET ausente='2026-09-05' WHERE ledger=? AND eid=?",
                (LANE, f"{LANE}-e4"))
    con.commit()
    vivas = [f["eid"] for f in poblado.search(lane=CARRIL, ledger=LANE, query="born red",
                                              limit=50)["filas"]]
    assert f"{LANE}-e4" not in vivas and len(vivas) == antes - 1
    assert "ausente" not in inspect.signature(ss.SearchStore.search).parameters
    assert "ausente" not in sc.canonical_filters(lane=CARRIL, ledger=LANE, query="x")
    assert "e.ausente IS NULL" in ss.SQL_BUSQUEDA


def test_las_entradas_sin_ts_son_alcanzables(poblado):
    """`ORDER BY ts DESC` esconde las 1.824 entradas sin fecha de `64bis-wiki`. El orden
    es por `(arrival, eid)`, así que aparecen."""
    mete(poblado.con, LANE, "sin-fecha", 7777, actor="cto", ts=None,
         cuerpo="born red comun")
    poblado.con.commit()
    assert "sin-fecha" in [f["eid"] for f in poblado.search(
        lane=CARRIL, ledger=LANE, query="born red", limit=50)["filas"]]


# ── readiness ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("trigger", ss.TRIGGERS)
def test_un_trigger_perdido_pone_la_readiness_en_ROJO(poblado, trigger):
    assert poblado.readiness()["ready"] is True
    poblado.con.execute(f"DROP TRIGGER {trigger}")
    r = poblado.readiness()
    assert r["ready"] is False and trigger in r["missing"], (
        "sin el trigger el índice deja de recibir altas SIN un solo error")
    with pytest.raises(ss.SearchNotReady):
        poblado.search(lane=CARRIL, ledger=LANE, query="born red", limit=5)


def test_sin_rebuild_no_se_sirve_y_no_hay_reserva_con_LIKE(store):
    mete(store.con, LANE, "e0", 1, cuerpo="born red")
    store.con.commit()
    assert store.readiness()["ready"] is False
    with pytest.raises(ss.SearchNotReady):
        store.search(lane=CARRIL, ledger=LANE, query="born red", limit=5)
    fuente = open(ss.__file__, encoding="utf-8").read()
    assert "LIKE ?" not in fuente and "LIKE '%" not in fuente, "hay un fallback con LIKE"


def test_rebuild_viejo_es_503_equivalente(poblado):
    poblado._set("schema_v", str(ss.SEARCH_SCHEMA_V - 1))
    r = poblado.readiness()
    assert r["ready"] is False and "rebuild viejo" in " ".join(r["problems"])
    with pytest.raises(ss.SearchStale):
        poblado.search(lane=CARRIL, ledger=LANE, query="born red", limit=5)


def test_schema_too_new_no_se_reconstruye_ni_se_sirve(poblado):
    poblado._set("schema_v", str(ss.SEARCH_SCHEMA_V + 1))
    with pytest.raises(ss.SearchSchemaTooNew):
        poblado.search(lane=CARRIL, ledger=LANE, query="born red", limit=5)
    with pytest.raises(ss.SearchSchemaTooNew):
        poblado.rebuild()


# ── fallo y recuperación ──────────────────────────────────────────────────────────
def test_un_rebuild_que_falla_deja_building_EN_DISCO_y_no_mueve_la_generacion(
        tmp_path, monkeypatch):
    con = nueva_con(tmp_path)
    st = ss.SearchStore(con, cursor_key=CLAVE, acl=ACL)
    st.ensure_schema()
    st.set_acl(ACL)
    mete(con, LANE, "e0", 1, cuerpo="born red")
    con.commit()
    gen0 = st.rebuild()

    monkeypatch.setattr(ss.SearchStore, "_head_fuera_de_body",
                        lambda self: 7)                       # simula el fallo tardío
    with pytest.raises(ss.RebuildFailed):
        st.rebuild()
    assert st.generation() == gen0, "la generación se movió con el rebuild fallido"

    # OTRA CONEXIÓN: prueba que `building` está COMITEADO, no sólo en memoria. Un estado
    # que sólo vive en la sesión que falló se pierde con el proceso y el siguiente
    # arranque lee «listo» sobre un índice a medias.
    otra = sqlite3.connect(str(tmp_path / "m2.sqlite"))
    st2 = ss.SearchStore(otra, cursor_key=CLAVE, acl=ACL)
    assert st2.state() == ss.ESTADO_CONSTRUYENDO
    assert st2.readiness()["ready"] is False
    with pytest.raises(ss.SearchNotReady):
        st2.search(lane=CARRIL, ledger=LANE, query="born red", limit=5)


def test_tras_la_caida_un_rebuild_bueno_recupera_y_cambia_la_generacion(tmp_path):
    con = nueva_con(tmp_path)
    st = ss.SearchStore(con, cursor_key=CLAVE, acl=ACL)
    st.ensure_schema()
    st.set_acl(ACL)
    mete(con, LANE, "e0", 1, cuerpo="born red")
    con.commit()
    gen0 = st.rebuild()
    st._set("state", ss.ESTADO_CONSTRUYENDO)          # como la dejaría una caída
    con.commit()
    with pytest.raises(ss.SearchNotReady):
        st.search(lane=CARRIL, ledger=LANE, query="born red", limit=5)
    gen1 = st.rebuild()
    assert gen1 != gen0 and st.readiness()["ready"] is True
    assert len(st.search(lane=CARRIL, ledger=LANE, query="born red", limit=5)["filas"]) == 1


def test_el_rebuild_no_sella_si_head_no_esta_en_body(tmp_path):
    con = nueva_con(tmp_path)
    st = ss.SearchStore(con, cursor_key=CLAVE, acl=ACL)
    st.ensure_schema()
    st.set_acl(ACL)
    con.execute("INSERT INTO entries(ledger,eid,arrival,head,body) VALUES(?,?,?,?,?)",
                (LANE, "e0", 1, "TITULAR QUE NO ESTA", "cuerpo sin el titular"))
    con.commit()
    with pytest.raises(ss.RebuildFailed) as ex:
        st.rebuild()
    assert "head" in str(ex.value)
    assert st.state() == ss.ESTADO_CONSTRUYENDO and st.generation() is None


# ── límites del compilador: rechazo tipado, jamás recorte ─────────────────────────
def test_nunca_trunca_rechaza(poblado):
    with pytest.raises(sc.TermTooLong):
        sc.compile_query("x" * (sc.MAX_TERM_BYTES + 1))
    with pytest.raises(sc.TooManyTerms):
        sc.compile_query(" ".join(f"t{i}" for i in range(sc.MAX_TERMS + 1)))
    with pytest.raises(sc.QueryTooLong):
        sc.compile_query("a " * 400)
    expr, terms = sc.compile_query("x" * sc.MAX_TERM_BYTES)      # ⊕ el borde SÍ entra
    assert terms == ["x" * sc.MAX_TERM_BYTES]


def test_el_fusible_corta_por_tiempo(poblado):
    lento = ss.SearchStore(poblado.con, cursor_key=CLAVE, acl=ACL, fusible_s=0.0,
                           fusible_pasos=1)
    with pytest.raises(ss.StatementTimeout):
        lento.search(lane=CARRIL, ledger=LANE, query="born red", limit=5)
    assert len(poblado.search(lane=CARRIL, ledger=LANE, query="born red", limit=5)["filas"]) > 0


def test_el_rebuild_NO_sella_un_indice_desacoplado(tmp_path, monkeypatch):
    """Ejercita el camino de CAPTURA del `integrity-check` con `rank=1`.

    Se corrompe el ÍNDICE, no el contenido, y por un motivo medido: mi primera versión
    cambiaba `entries.body`, y eso (a) dispara el trigger, que vuelve a sincronizar, y
    (b) rompe antes la comprobación de `head ⊆ body`. El test pasaba por el motivo
    equivocado y el mutante «integrity-check sin rank=1» SOBREVIVÍA. Purgando los tokens
    de una fila viva, el contenido sigue intacto, `head ⊆ body` sigue bien y los recuentos
    también: lo único que puede cazarlo es la comparación índice↔contenido.
    """
    con = nueva_con(tmp_path)
    st = ss.SearchStore(con, cursor_key=CLAVE, acl=ACL)
    st.ensure_schema()
    st.set_acl(ACL)
    mete(con, LANE, "e0", 1, cuerpo="born red")
    mete(con, LANE, "e1", 2, cuerpo="born red")
    con.commit()
    gen0 = st.rebuild()

    def desacopla(self):
        fila = self.con.execute(
            "SELECT d.rid rid, e.body body FROM search_documents d"
            " JOIN entries e ON e.ledger=d.ledger AND e.eid=d.eid LIMIT 1").fetchone()
        self.con.execute(
            "INSERT INTO search_fts(search_fts, rowid, body) VALUES('delete', ?, ?)",
            (fila["rid"], fila["body"]))

    monkeypatch.setattr(ss.SearchStore, "_tras_poblar", desacopla)
    with pytest.raises(ss.SearchError):
        st.rebuild()
    assert st.state() == ss.ESTADO_CONSTRUYENDO
    assert st.generation() == gen0, "sellé una generación nueva sobre un índice desacoplado"


def test_el_rebuild_conserva_los_rid_y_las_lapidas(tmp_path):
    """Un rebuild NO reasigna `rid` ni tira lápidas: sólo añade lo que falta."""
    con = nueva_con(tmp_path)
    st = ss.SearchStore(con, cursor_key=CLAVE, acl=ACL)
    st.ensure_schema()
    st.set_acl(ACL)
    for i in range(4):
        mete(con, LANE, f"e{i}", i + 1, cuerpo="born red")
    con.commit()
    st.rebuild()
    antes = {(r["ledger"], r["eid"]): r["rid"] for r in
             con.execute("SELECT ledger, eid, rid FROM search_documents")}

    con.execute("DELETE FROM entries WHERE eid='e1'")          # deja lápida
    mete(con, LANE, "e9", 99, cuerpo="born red")               # mapeo que falta
    con.commit()
    st.rebuild()
    despues = {(r["ledger"], r["eid"]): r["rid"] for r in
               con.execute("SELECT ledger, eid, rid FROM search_documents")}

    for clave, rid in antes.items():
        assert despues.get(clave) == rid, f"el rebuild movió el `rid` de {clave}"
    assert (LANE, "e1") in despues, "el rebuild se llevó la lápida"
    assert (LANE, "e9") in despues, "el rebuild no dio de alta el mapeo que faltaba"
    eids = [f["eid"] for f in st.search(lane=CARRIL, ledger=LANE, query="born red",
                                        limit=50)["filas"]]
    assert "e1" not in eids and "e9" in eids
    assert st.readiness()["ready"] is True


def test_filter_sha256_es_el_digest_COMPLETO():
    """64 hex de verdad, no 16 rellenados. Se compara contra el digest estandar del JSON
    canonico: eso es la ESPECIFICACION, no una reimplementacion de la logica."""
    import hashlib
    import json

    f = sc.canonical_filters(lane=CARRIL, ledger=LANE, query="born red")
    esperado = hashlib.sha256(json.dumps(
        f, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")).hexdigest()
    obtenido = sc.filter_sha256(f)
    assert obtenido == esperado
    assert len(obtenido) == 64 and len(set(obtenido[16:])) > 1, (
        "la cola del digest es constante: esta truncado y rellenado")


# ── techos de respuesta ───────────────────────────────────────────────────────────
def test_el_snippet_cabe_en_el_techo_MARCA_INCLUIDA(tmp_path):
    con = nueva_con(tmp_path)
    st = ss.SearchStore(con, cursor_key=CLAVE, acl=ACL)
    st.ensure_schema()
    st.set_acl(ACL)
    # Un solo TOKEN enorme junto al término: `snippet(...,12)` devuelve 12 tokens, así
    # que el fragmento sólo pasa de 2 KiB si un token lo hace. Es el caso real del corpus,
    # donde una entrada mide 11,6 MiB.
    mete(con, LANE, "gorda", 1, cuerpo="born red " + "z" * 5000)
    con.commit()
    st.rebuild()
    r = st.search(lane=CARRIL, ledger=LANE, query="born red", limit=5)
    frag = r["filas"][0]["fragmento"]
    # SIN holgura: el techo incluye la marca. Antes cortaba a 2.048 y luego pegaba `…`,
    # así que devolvía 2.051 — un techo aplicado antes de terminar el valor.
    assert len(frag.encode("utf-8")) <= ss.MAX_SNIPPET_BYTES, len(frag.encode("utf-8"))
    assert frag.endswith(ss.MARCA_RECORTE)
    assert r["truncado"]["snippets_recortados"] >= 1, (
        "se recortó el fragmento y no se dijo")


def test_la_respuesta_se_acota_por_BYTES_y_el_cursor_sigue_donde_corto(tmp_path):
    """El techo que corta aquí es el de BYTES, no el de filas — el caso que `limit` no
    cubre. Y la continuación tiene que EMPALMAR sin perder ni repetir.

    Las filas son grandes DESPUÉS de los caps por campo (`head` 2 KiB + fragmento 2 KiB),
    que es lo que hace falta para desbordar 256 KiB dentro del límite de 100 filas.
    """
    con = nueva_con(tmp_path)
    st = ss.SearchStore(con, cursor_key=CLAVE, acl=ACL)
    st.ensure_schema()
    st.set_acl(ACL)
    for i in range(100):
        cabecera = f"titular {i:03d} " + ("y" * 8_000)
        mete(con, LANE, f"e{i:03d}", i + 1, head=cabecera,
             cuerpo="born red " + ("z" * 8_000))
    con.commit()
    st.rebuild()

    r = st.search(lane=CARRIL, ledger=LANE, query="born red", limit=100)
    assert r["truncado"]["por"] == "bytes", r["truncado"]
    assert 0 < len(r["filas"]) < 100 and r["hay_mas"] is True and r["cursor"]
    assert len(json.dumps(r, ensure_ascii=False).encode("utf-8")) <= ss.MAX_RESPONSE_BYTES

    vistos = [f["eid"] for f in r["filas"]]
    cur, vueltas = r["cursor"], 0
    while cur:
        vueltas += 1
        assert vueltas < 200, "bucle de cursor"
        s2 = st.search(lane=CARRIL, ledger=LANE, query="born red", limit=100, cursor=cur)
        assert s2["filas"], "una página vacía CON cursor es un bucle"
        vistos += [f["eid"] for f in s2["filas"]]
        cur = s2["cursor"] if s2["hay_mas"] else None
    assert len(vistos) == 100 and len(set(vistos)) == 100, (
        f"el corte por bytes perdió o repitió filas: {len(vistos)}/100")


def test_la_respuesta_SERIALIZADA_cabe_en_el_techo_sobre_incluido(tmp_path):
    """⊖ del defecto que traía: `acumulado` contaba SÓLO filas.

    Se mide el objeto completo —`cursor`, `truncado`, `orden`, `generacion` y
    `filtro_sha256` incluidos—, que es lo que de verdad sale por el cable.
    """
    con = nueva_con(tmp_path)
    st = ss.SearchStore(con, cursor_key=CLAVE, acl=ACL)
    st.ensure_schema()
    st.set_acl(ACL)
    for i in range(100):
        mete(con, LANE, f"e{i:03d}", i + 1, head=f"t{i} " + "y" * 8_000,
             cuerpo="born red " + ("z" * 8_000))
    con.commit()
    st.rebuild()
    r = st.search(lane=CARRIL, ledger=LANE, query="born red", limit=100)
    total = len(json.dumps(r, ensure_ascii=False).encode("utf-8"))
    assert total <= ss.MAX_RESPONSE_BYTES, f"{total} > {ss.MAX_RESPONSE_BYTES}"
    assert r["truncado"]["bytes_presupuestados"] == total, (
        "el tamaño declarado no es el de la serialización que mide este módulo")
    # ⊕ control: el sobre pesa de verdad, así que ignorarlo NO era inocuo.
    solo_filas = len(json.dumps(r["filas"], ensure_ascii=False).encode("utf-8"))
    assert total - solo_filas > 200, (
        "el sobre no pesa nada en este caso: el test no discriminaría")


def test_una_PRIMERA_fila_gigante_no_desborda_ni_deja_bucle(tmp_path):
    """La fila que sola supera el techo. Antes: `if page and …` la admitía SIEMPRE.

    El contrato admite dos salidas y las dos son aceptables mientras no haya bucle: servir
    truncando bajo contrato, o rechazar con error tipado y SIN cursor. Aquí gana la
    primera, porque los caps por campo hacen que una fila quepa siempre — y así la
    paginación PROGRESA en vez de morir.
    """
    con = nueva_con(tmp_path)
    st = ss.SearchStore(con, cursor_key=CLAVE, acl=ACL)
    st.ensure_schema()
    st.set_acl(ACL)
    gigante = "titular gigante " + ("g" * 320_000)          # > 300 KiB de `head`
    mete(con, LANE, "e000", 10, head=gigante, cuerpo="born red")
    mete(con, LANE, "e001", 9, cuerpo="born red")
    con.commit()
    st.rebuild()
    assert len(gigante.encode("utf-8")) > 300 * 1024, "el fixture no es gigante"

    try:
        r = st.search(lane=CARRIL, ledger=LANE, query="born red", limit=10)
    except ss.ResponseTooLarge as e:
        assert "cursor" not in str(e).lower() or "bucle" in str(e).lower()
        return                                              # salida (a): rechazo tipado
    total = len(json.dumps(r, ensure_ascii=False).encode("utf-8"))
    assert total <= ss.MAX_RESPONSE_BYTES, f"{total} > {ss.MAX_RESPONSE_BYTES}"
    assert len(r["filas"]) == 2, "las dos filas caben tras los caps"
    assert r["truncado"]["campos_recortados"] >= 1, "se recortó un campo y no se dijo"
    assert r["hay_mas"] is False and r["cursor"] is None
    assert len(r["filas"][0]["head"].encode("utf-8")) <= ss.MAX_HEAD_BYTES


def test_los_caps_por_campo_son_contractuales_y_se_declaran(tmp_path):
    con = nueva_con(tmp_path)
    st = ss.SearchStore(con, cursor_key=CLAVE, acl=ACL)
    st.ensure_schema()
    st.set_acl(ACL)
    con.execute(
        "INSERT INTO entries(ledger,eid,arrival,ts,actor,tipo,head,body) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (LANE, "e0", 1, "2026-09-05", "a" * 500, "t" * 300, "H" * 9_000,
         "H" * 9_000 + "\nborn red"))
    con.commit()
    st.rebuild()
    f = st.search(lane=CARRIL, ledger=LANE, query="born red", limit=5)["filas"][0]
    assert len(f["head"].encode("utf-8")) <= ss.MAX_HEAD_BYTES
    assert len(f["actor"].encode("utf-8")) <= ss.MAX_ACTOR_BYTES
    assert len(f["tipo"].encode("utf-8")) <= ss.MAX_TIPO_BYTES
    for campo in ("head", "actor", "tipo"):
        assert f[campo].endswith(ss.MARCA_RECORTE), (
            f"`{campo}` se recortó sin marca: se lee como el valor completo")


def test_el_limite_maximo_es_100(poblado):
    assert sc.MAX_LIMIT == 100
    poblado.search(lane=CARRIL, ledger=LANE, query="born red", limit=100)
    with pytest.raises(sc.InvalidLimit):
        poblado.search(lane=CARRIL, ledger=LANE, query="born red", limit=101)


# ── los tres guardas fail-closed, ejercitados por su camino de CAPTURA ────────────
# Con los caps por campo una fila cabe SIEMPRE, así que estos tres guardas son
# inalcanzables con los techos de producción. Eso los haría «declarados y no probados»:
# se bajan los techos para llegar a ellos. Lo que se prueba es la GUARDA, no el número.
def _con_dos_filas(tmp_path):
    con = nueva_con(tmp_path)
    st = ss.SearchStore(con, cursor_key=CLAVE, acl=ACL)
    st.ensure_schema()
    st.set_acl(ACL)
    for i in range(2):
        mete(con, LANE, f"e{i}", i + 1, head=f"t{i} " + ("y" * 1_500),
             cuerpo="born red " + ("z" * 1_500))
    con.commit()
    st.rebuild()
    return st


def test_si_NI_UNA_fila_cabe_se_rechaza_tipado_y_SIN_cursor(tmp_path, monkeypatch):
    """⊖ del `if page and …` que admitía la primera fila costara lo que costara."""
    st = _con_dos_filas(tmp_path)
    r = st.search(lane=CARRIL, ledger=LANE, query="born red", limit=2)
    una = ss.SearchStore._bytes_fila(st, r["filas"][0])
    sobre = st._envelope_bytes(st.generation(), r["filtro_sha256"], r["orden"])
    monkeypatch.setattr(ss, "MAX_RESPONSE_BYTES", sobre + una - 1)
    with pytest.raises(ss.ResponseTooLarge) as ex:
        st.search(lane=CARRIL, ledger=LANE, query="born red", limit=2)
    assert "bucle" in str(ex.value), "el rechazo no dice por qué no devuelve cursor"


def test_la_reserva_del_SOBRE_es_la_que_hace_caber_la_respuesta(tmp_path, monkeypatch):
    """⊖ de `presupuesto = MAX_RESPONSE_BYTES` (sin restar el sobre).

    El techo se pone justo por encima de una fila: con la reserva entra UNA y el total
    cabe; sin ella entrarían DOS y el objeto serializado pasaría del techo.
    """
    st = _con_dos_filas(tmp_path)
    r0 = st.search(lane=CARRIL, ledger=LANE, query="born red", limit=2)
    f0 = ss.SearchStore._bytes_fila(st, r0["filas"][0])
    f1 = ss.SearchStore._bytes_fila(st, r0["filas"][1])
    sobre = st._envelope_bytes(st.generation(), r0["filtro_sha256"], r0["orden"])
    monkeypatch.setattr(ss, "MAX_RESPONSE_BYTES", sobre + f0 + (f1 // 2))

    r = st.search(lane=CARRIL, ledger=LANE, query="born red", limit=2)
    assert len(r["filas"]) == 1 and r["truncado"]["por"] == "bytes"
    total = len(json.dumps(r, ensure_ascii=False).encode("utf-8"))
    assert total <= ss.MAX_RESPONSE_BYTES, (
        f"{total} > {ss.MAX_RESPONSE_BYTES}: el sobre viajó gratis")


def test_la_medida_FINAL_RETIRA_FILAS_cuando_la_reserva_se_queda_corta(tmp_path,
                                                                        monkeypatch):
    """La reserva miente a la baja y la respuesta SIGUE cabiendo.

    Antes esto lanzaba: un `500` donde había una respuesta servible con una fila menos.
    Ahora la defensa final retira filas por el final y recalcula el cursor hasta caber —
    y el cursor sigue empalmando, que es lo que impide perder las retiradas.
    """
    st = _con_dos_filas(tmp_path)
    r0 = st.search(lane=CARRIL, ledger=LANE, query="born red", limit=2)
    filas = sum(ss.SearchStore._bytes_fila(st, f) for f in r0["filas"])
    monkeypatch.setattr(ss.SearchStore, "_envelope_bytes", lambda self, *a: 0)
    monkeypatch.setattr(ss, "MAX_RESPONSE_BYTES", filas + 8)

    r = st.search(lane=CARRIL, ledger=LANE, query="born red", limit=2)
    total = len(json.dumps(r, ensure_ascii=False).encode("utf-8"))
    assert total <= ss.MAX_RESPONSE_BYTES, f"{total} > {ss.MAX_RESPONSE_BYTES}"
    assert len(r["filas"]) == 1 and r["hay_mas"] is True and r["cursor"]
    assert r["truncado"]["por"] == "bytes" and r["truncado"]["servidas"] == 1

    seg = st.search(lane=CARRIL, ledger=LANE, query="born red", limit=2,
                    cursor=r["cursor"])
    vistos = [f["eid"] for f in r["filas"]] + [f["eid"] for f in seg["filas"]]
    assert sorted(vistos) == ["e0", "e1"], f"la retirada perdió filas: {vistos}"


def test_si_NI_UNA_fila_cabe_ni_retirando_se_rechaza_SIN_cursor(tmp_path, monkeypatch):
    st = _con_dos_filas(tmp_path)
    r0 = st.search(lane=CARRIL, ledger=LANE, query="born red", limit=2)
    una = ss.SearchStore._bytes_fila(st, r0["filas"][0])
    monkeypatch.setattr(ss.SearchStore, "_envelope_bytes", lambda self, *a: 0)
    monkeypatch.setattr(ss, "MAX_RESPONSE_BYTES", una // 2)
    with pytest.raises(ss.ResponseTooLarge) as ex:
        st.search(lane=CARRIL, ledger=LANE, query="born red", limit=2)
    assert "bucle" in str(ex.value)
