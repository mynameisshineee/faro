"""Correctivas M2-3. Un fichero por tanda para que el falsador se lea junto a su motivo."""

import base64
import json
import sqlite3
import unicodedata
import threading

import pytest

from .conftest import ACL, CARRIL, CLAVE, LANE, mete, nueva_con
import search_contract as sc
import search_cursor as scur
import search_store as ss


def _store(tmp_path, filas=None):
    con = nueva_con(tmp_path)
    st = ss.SearchStore(con, cursor_key=CLAVE, acl=ACL)
    st.ensure_schema()
    st.set_acl(ACL)
    for kw in (filas or []):
        mete(con, LANE, **kw)
    con.commit()
    if filas:
        st.rebuild()
    return st


# ── ① framing fuera de la PROYECCIÓN, no sólo del fragmento ───────────────────────
FRAMED = ("<!-- LLMINBOX-EVENT-BEGIN event_id=evt_deadbeef payload_sha=cafe1234 -->\n"
          "titular real\nborn red cuerpo de verdad\n"
          "<!-- LLMINBOX-EVENT-END event_id=evt_deadbeef -->")


def test_el_cuerpo_indexado_no_contiene_NADA_del_framing(tmp_path):
    con = nueva_con(tmp_path)
    st = ss.SearchStore(con, cursor_key=CLAVE, acl=ACL)
    st.ensure_schema()
    st.set_acl(ACL)
    con.execute("INSERT INTO entries(ledger,eid,arrival,head,body) VALUES(?,?,?,?,?)",
                (LANE, "e0", 1, "titular real", FRAMED))
    con.commit()
    st.rebuild()
    proyectado = con.execute("SELECT body FROM search_view").fetchone()["body"]
    for prohibido in ("llminbox-event-begin", "llminbox-event-end", "event_id",
                      "payload_sha", "evt_deadbeef", "cafe1234", "<!--"):
        assert prohibido not in proyectado.lower(), prohibido
    assert "born red" in proyectado, "se llevó por delante el texto"


@pytest.mark.parametrize("aguja", ["event_id", "payload_sha", "llminbox",
                                   "evt_deadbeef", "cafe1234"])
def test_los_metadatos_de_framing_no_producen_falsos_MATCH(tmp_path, aguja):
    """No basta con sanear el fragmento: el falso positivo ya ocurrió en el `MATCH`."""
    st = _store(tmp_path)
    st.con.execute("INSERT INTO entries(ledger,eid,arrival,head,body) VALUES(?,?,?,?,?)",
                   (LANE, "e0", 1, "titular real", FRAMED))
    st.con.commit()
    st.rebuild()
    assert st.search(lane=CARRIL, ledger=LANE, query=aguja, limit=5)["filas"] == []
    # ⊕ control: el texto REAL de la misma entrada sí se encuentra, o el cero de arriba
    # sería de un índice vacío y no de la proyección.
    assert len(st.search(lane=CARRIL, ledger=LANE, query="born red", limit=5)["filas"]) == 1


def test_una_trama_INCOMPLETA_tampoco_entra_en_resultados(tmp_path):
    st = _store(tmp_path)
    st.con.execute(
        "INSERT INTO entries(ledger,eid,arrival,head,body) VALUES(?,?,?,?,?)",
        (LANE, "e0", 1, "titular",
         "<!-- LLMINBOX-EVENT-BEGIN event_id=evt_huerfano -->\ntitular\nborn red suelto"))
    st.con.commit()
    st.rebuild()
    assert st.search(lane=CARRIL, ledger=LANE, query="evt_huerfano", limit=5)["filas"] == []
    assert st.search(lane=CARRIL, ledger=LANE, query="born red", limit=5)["filas"]


def test_un_marcador_CITADO_dentro_de_una_frase_se_conserva(tmp_path):
    """La regla retira LÍNEAS que son un marcador entero. Con texto detrás no lo es."""
    proyectado = sc.project_body(
        "hablando del <!-- LLMINBOX-EVENT-BEGIN --> y de otras cosas\nborn red")
    assert "llminbox-event-begin" in proyectado


def test_sin_el_normalizador_la_conexion_FALLA_CERRADA(tmp_path):
    """El contrato es «toda conexión escritora registra la UDF». Sin ella no se indexa
    crudo: no se puede ni escribir ni leer la vista."""
    st = _store(tmp_path, [dict(eid="e0", arrival=1, cuerpo="born red")])
    ruta = str(tmp_path / "m2.sqlite")
    pelada = sqlite3.connect(ruta)                       # SIN `SearchStore.register`
    pelada.isolation_level = None
    with pytest.raises(sqlite3.OperationalError, match="no such function"):
        pelada.execute("SELECT body FROM search_view").fetchall()
    with pytest.raises(sqlite3.OperationalError, match="no such function"):
        pelada.execute("INSERT INTO entries(ledger,eid,arrival,head,body)"
                       " VALUES(?,?,?,?,?)", (LANE, "nueva", 9, "t", "t\nborn red"))
    # ⊕ control: registrada, las dos cosas funcionan.
    ss.SearchStore.register_udf(pelada)
    pelada.execute("SELECT body FROM search_view").fetchall()
    pelada.execute("INSERT INTO entries(ledger,eid,arrival,head,body)"
                   " VALUES(?,?,?,?,?)", (LANE, "nueva", 9, "t", "t\nborn red"))


def test_si_cambia_el_normalizador_el_indice_deja_de_estar_listo(tmp_path):
    st = _store(tmp_path, [dict(eid="e0", arrival=1, cuerpo="born red")])
    assert st.readiness()["ready"] is True
    st._set("normalizador", "otra-huella-cualquiera")
    r = st.readiness()
    assert r["ready"] is False and "normalizador" in " ".join(r["problems"])


# ── ② una sola instantánea de lectura ─────────────────────────────────────────────
def test_un_building_concurrente_NUNCA_sirve_una_pagina(tmp_path):
    """El cambio a `building` entra entre readiness y el SELECT. La página no sale.

    Se usa la costura del fusible para ejecutar la mutación EXACTAMENTE en esa ventana:
    sin una costura, el test dependería de ganar una carrera y sería intermitente.
    """
    st = _store(tmp_path, [dict(eid=f"e{i}", arrival=i + 1, cuerpo="born red")
                           for i in range(3)])
    otra = sqlite3.connect(str(tmp_path / "m2.sqlite"))
    ss.SearchStore.prepare_search_connection(otra)
    disparos = []

    original = ss.SearchStore._exigir_listo

    def entre(self):
        gen = original(self)
        if not disparos:                       # sólo la primera vez
            disparos.append(1)
            otra.execute("INSERT OR REPLACE INTO search_state(k,v)"
                         " VALUES('state','building')")
        return gen

    ss.SearchStore._exigir_listo = entre
    try:
        r = st.search(lane=CARRIL, ledger=LANE, query="born red", limit=5)
    finally:
        ss.SearchStore._exigir_listo = original
    # La transacción ya tenía su instantánea: sirve coherente con lo que decidió…
    assert len(r["filas"]) == 3
    # …y la SIGUIENTE llamada ve el estado nuevo y se niega.
    with pytest.raises(ss.SearchNotReady):
        st.search(lane=CARRIL, ledger=LANE, query="born red", limit=5)


def test_search_exige_no_tener_transaccion_abierta(tmp_path):
    st = _store(tmp_path, [dict(eid="e0", arrival=1, cuerpo="born red")])
    st.con.execute("BEGIN DEFERRED")
    try:
        with pytest.raises(ss.SearchError, match="transacción abierta"):
            st.search(lane=CARRIL, ledger=LANE, query="born red", limit=5)
    finally:
        st.con.execute("COMMIT")


# ── ③ huella del esquema, no el nombre ────────────────────────────────────────────
def test_un_trigger_NO_OP_con_el_MISMO_nombre_pone_not_ready(tmp_path):
    st = _store(tmp_path, [dict(eid="e0", arrival=1, cuerpo="born red")])
    assert st.readiness()["ready"] is True
    st.con.execute("DROP TRIGGER search_ai")
    st.con.execute("CREATE TRIGGER search_ai AFTER INSERT ON entries BEGIN"
                   "  SELECT 1; END")                    # mismo nombre, cuerpo vacío
    r = st.readiness()
    assert r["ready"] is False, "un trigger vaciado pasó por llamarse igual"
    assert "search_ai" in " ".join(r["problems"])
    assert r["missing"] == [], "el nombre SÍ está: lo que falla es el cuerpo"


def test_la_vista_reescrita_con_el_mismo_nombre_pone_not_ready(tmp_path):
    st = _store(tmp_path, [dict(eid="e0", arrival=1, cuerpo="born red")])
    st.con.execute("DROP VIEW search_view")
    st.con.execute("CREATE VIEW search_view AS SELECT d.rid AS rowid, e.body AS body"
                   "  FROM search_documents d JOIN entries e"
                   "    ON e.ledger=d.ledger AND e.eid=d.eid")   # ¡sin normalizar!
    r = st.readiness()
    assert r["ready"] is False and "search_view" in " ".join(r["problems"])


# ── ④ generación presente y de 32 hex ─────────────────────────────────────────────
@pytest.mark.parametrize("gen", [None, "", "abc", "z" * 32, "a" * 31, "a" * 33])
def test_ready_exige_generacion_de_32_hex(tmp_path, gen):
    st = _store(tmp_path, [dict(eid="e0", arrival=1, cuerpo="born red")])
    if gen is None:
        st.con.execute("DELETE FROM search_state WHERE k='generation'")
    else:
        st._set("generation", gen)
    r = st.readiness()
    assert r["ready"] is False, f"aceptó generación {gen!r}"
    assert "generación" in " ".join(r["problems"])


# ── ⑤ el cursor siempre se puede volver a consumir ────────────────────────────────
def test_un_eid_largo_NO_sella_el_indice(tmp_path):
    """Fail-closed en el rebuild, no al paginar: descubrirlo al paginar sería servir una
    página que nadie puede continuar."""
    con = nueva_con(tmp_path)
    st = ss.SearchStore(con, cursor_key=CLAVE, acl=ACL)
    st.ensure_schema()
    st.set_acl(ACL)
    mete(con, LANE, "x" * (scur.MAX_EID_BYTES + 1), 1, cuerpo="born red")
    con.commit()
    with pytest.raises(ss.RebuildFailed, match="eid"):
        st.rebuild()
    assert st.state() == ss.ESTADO_CONSTRUYENDO


def test_el_cursor_emitido_SIEMPRE_lo_acepta_el_decodificador(tmp_path):
    st = _store(tmp_path, [dict(eid="f" * scur.MAX_EID_BYTES, arrival=2,
                                cuerpo="born red"),
                           dict(eid="e" * scur.MAX_EID_BYTES, arrival=1,
                                cuerpo="born red")])
    r = st.search(lane=CARRIL, ledger=LANE, query="born red", limit=1)
    assert len(r["cursor"].encode("utf-8")) <= scur.MAX_CURSOR_BYTES
    seg = st.search(lane=CARRIL, ledger=LANE, query="born red", limit=1,
                    cursor=r["cursor"])
    assert seg["filas"], "el cursor que emití no me sirvió para continuar"


# ── ⑥ misma normalización en índice y consulta ────────────────────────────────────
@pytest.mark.parametrize("indexado,consulta", [
    ("oﬃce", "office"), ("office", "oﬃce"),
    ("straße", "strasse"), ("strasse", "straße"),
])
def test_sin_asimetria_entre_lo_indexado_y_lo_consultado(tmp_path, indexado, consulta):
    st = _store(tmp_path, [dict(eid="e0", arrival=1, cuerpo=f"born red {indexado}")])
    r = st.search(lane=CARRIL, ledger=LANE, query=consulta, limit=5)
    assert len(r["filas"]) == 1, (
        f"indexado {indexado!r} no lo encuentra la consulta {consulta!r}: la "
        f"normalización de los dos lados no es la misma")


@pytest.mark.parametrize("indexado,consulta", [
    ("ｏｆｆｉｃｅ", "office"), ("office", "ｏｆｆｉｃｅ"),
    ("ＢＩＫ", "bik"), ("bik", "ＢＩＫ"),
])
def test_la_normalizacion_pliega_COMPATIBILIDAD_y_no_solo_el_CASO(tmp_path, indexado,
                                                                  consulta):
    """`NFKC` es una mitad del normalizador y `casefold` la otra. Los casos de arriba
    —`oﬃce`, `straße`— NO separan las dos: `str.casefold()` ya las pliega él solo.

    ⊖ MEDIDO: por eso el mutante `M43` (`normalize` sin `NFKC`) SOBREVIVÍA a la suite
    entera mientras `M42` (sin `casefold`) moría. Hace falta un par que `casefold` deje
    intacto y sólo `NFKC` una: las formas de ANCHO COMPLETO, que es lo que produce un IME
    CJK o un copiado desde una hoja de cálculo — no un caso de laboratorio.
    """
    # ⊕ CONTROL DE DISCRIMINACIÓN, en el propio test: si `casefold` ya los uniera, este
    # par no probaría `NFKC` y el test daría verde sin medir nada.
    assert indexado.casefold() != consulta.casefold(), (
        f"{indexado!r}/{consulta!r} lo pliega ya `casefold`: no discrimina NFKC")
    assert (unicodedata.normalize("NFKC", indexado).casefold()
            == unicodedata.normalize("NFKC", consulta).casefold()), (
        "el par tiene que ser el MISMO texto tras NFKC+casefold")

    st = _store(tmp_path, [dict(eid="e0", arrival=1, cuerpo=f"born red {indexado}")])
    r = st.search(lane=CARRIL, ledger=LANE, query=consulta, limit=5)
    assert len(r["filas"]) == 1, (
        f"indexado {indexado!r} no lo encuentra la consulta {consulta!r}: sin NFKC el "
        f"índice guarda un token y la consulta pregunta por otro, y nadie ve un error — "
        f"sólo menos resultados")


# ── ⑦ base64url estricta y canónica ───────────────────────────────────────────────
def _cursor_con(caracter: str) -> str:
    """Cursor DETERMINISTA cuyo base64url contiene `-` o `_`.

    Se busca variando el `arrival`, que va dentro del payload firmado: en cuanto uno de
    los cuerpos codificados trae el carácter, ése es el sujeto. Sin esto, el caso del
    alfabeto estándar dependía de que el cursor de turno tuviera `-`, y cuando no lo
    tenía el mutador no cambiaba nada.
    """
    # El payload es JSON, así que con `eid` ASCII el base64 casi nunca produce los
    # índices 62/63 (`-` y `_`). Se barre sobre `eid` con caracteres no-ASCII, cuyos bytes
    # UTF-8 sí los generan. La búsqueda es determinista y se falla en voz alta si no
    # encuentra: un caso que no discrimina tiene que verse, no saltarse.
    for base in "ÿþýüûúùøא漢🙂":
        for k in range(1, 6):
            cur = scur.encode(arrival=1, eid=base * k, generation="a" * 32,
                              filter_sha256="b" * 64, key=CLAVE)
            if caracter in cur.split(".")[0]:
                return cur
    raise AssertionError(f"no encontré un cursor con {caracter!r}")


def _cursor_valido():
    return scur.encode(arrival=1, eid="e", generation="a" * 32,
                       filter_sha256="b" * 64, key=CLAVE)


@pytest.mark.parametrize("urlsafe,estandar", [("-", "+"), ("_", "/")])
def test_el_alfabeto_ESTANDAR_decodifica_igual_y_aun_asi_se_rechaza(urlsafe, estandar):
    """Primero se DEMUESTRA el alias, después se exige el rechazo.

    Sustituir `-`→`+` (o `_`→`/`) da una cadena DISTINTA que el decodificador permisivo
    convierte en LOS MISMOS bytes: dos cursores para un solo estado, los dos con firma
    válida. Ésa es la avería. Mi versión anterior anteponía `+` quitando el primer
    carácter, o sea cambiaba los bytes — probaba «base64 roto», que no es lo mismo y se
    habría rechazado igual sin la regla canónica.
    """
    cur = _cursor_con(urlsafe)
    cuerpo, firma = cur.split(".", 1)
    alias = cuerpo.replace(urlsafe, estandar, 1)
    assert alias != cuerpo

    # ⊕ LA DEMOSTRACIÓN DEL ALIAS: el decodificador permisivo da los mismos bytes.
    relleno = "=" * (-len(cuerpo) % 4)
    assert base64.urlsafe_b64decode(cuerpo + relleno) == \
        base64.b64decode(alias + relleno), "no es un alias: el caso no probaría nada"

    # ⊖ y aun así el cursor con alias se rechaza, porque no es la forma canónica.
    with pytest.raises(scur.CursorMalformed):
        scur.decode(f"{alias}.{firma}", key=CLAVE, filter_sha256="b" * 64,
                    generation="a" * 32)


@pytest.mark.parametrize("ignorado", ["\n", "\r\n", " "])
def test_los_caracteres_IGNORADOS_tambien_son_alias_y_se_rechazan(ignorado):
    """`base64` ignora saltos de línea: `AB\nCD` y `ABCD` son el mismo cuerpo."""
    cur = _cursor_valido()
    cuerpo, firma = cur.split(".", 1)
    mitad = len(cuerpo) // 2
    con_ruido = cuerpo[:mitad] + ignorado + cuerpo[mitad:]

    relleno = "=" * (-len(cuerpo) % 4)
    assert base64.b64decode(con_ruido + relleno) == \
        base64.urlsafe_b64decode(cuerpo + relleno), "no es un alias"

    with pytest.raises(scur.CursorMalformed):
        scur.decode(f"{con_ruido}.{firma}", key=CLAVE, filter_sha256="b" * 64,
                    generation="a" * 32)


@pytest.mark.parametrize("relleno", ["=", "==", "==="])
def test_el_relleno_sobrante_tambien_se_rechaza(relleno):
    cur = _cursor_valido()
    cuerpo, firma = cur.split(".", 1)
    with pytest.raises(scur.CursorMalformed):
        scur.decode(f"{cuerpo}{relleno}.{firma}", key=CLAVE, filter_sha256="b" * 64,
                    generation="a" * 32)


def test_el_cursor_valido_pasa():
    a, e = scur.decode(_cursor_valido(), key=CLAVE, filter_sha256="b" * 64,
                       generation="a" * 32)
    assert (a, e) == (1, "e")


# ── ⑧ fusible: sólo segundos finitos positivos y pasos enteros positivos ──────────
@pytest.mark.parametrize("s_,pasos", [
    (float("inf"), 1), (float("nan"), 1), (-1.0, 1), (True, 1), ("30", 1), (None, 1),
    (1.0, 0), (1.0, -5), (1.0, True), (1.0, 1.5), (1.0, None),
])
def test_el_fusible_rechaza_configuraciones_invalidas(tmp_path, s_, pasos):
    con = nueva_con(tmp_path)
    with pytest.raises(ValueError):
        ss.SearchStore(con, cursor_key=CLAVE, acl=ACL, fusible_s=s_, fusible_pasos=pasos)


def test_el_fusible_valido_se_acepta(tmp_path):
    ss.SearchStore(nueva_con(tmp_path), cursor_key=CLAVE, acl=ACL, fusible_s=0.5, fusible_pasos=1)


# ── ⑨ cotas baratas antes de normalizar ───────────────────────────────────────────
@pytest.mark.parametrize("campo", ["lane", "ledger", "actor", "tipo", "desde", "hasta"])
def test_cotas_baratas_de_los_filtros(campo):
    base = dict(lane=CARRIL, ledger=LANE, query="born red")
    base[campo] = "x" * (sc.MAX_FILTER_BYTES + 1)
    with pytest.raises(sc.InvalidFilter, match="bytes"):
        sc.canonical_filters(**base)


def test_la_consulta_cruda_enorme_se_rechaza_SIN_normalizar(monkeypatch):
    llamadas = []
    real = sc.normalize_text
    monkeypatch.setattr(sc, "normalize_text",
                        lambda t: llamadas.append(len(t)) or real(t))
    with pytest.raises(sc.QueryTooLong):
        sc.compile_query("a" * (sc.MAX_QUERY_BYTES * sc.COTA_CRUDA + 1))
    assert llamadas == [], "normalizó antes de mirar el tamaño crudo"


# ── ⑩ empate REAL: mismo ledger y mismo arrival ───────────────────────────────────
def test_cuatro_filas_con_el_MISMO_arrival_paginan_sin_perdida(tmp_path):
    """El desempate lo hace `eid`. Con cuatro filas de `arrival` idéntico, un orden que
    sólo mire `arrival` no es total y la paginación pierde o repite."""
    st = _store(tmp_path, [dict(eid=f"tie-{i}", arrival=7, cuerpo="born red")
                           for i in range(4)])
    assert st.con.execute(
        "SELECT COUNT(DISTINCT arrival) FROM entries").fetchone()[0] == 1, (
        "el fixture no tiene el empate que el test dice probar")
    vistos, cur, vueltas = [], None, 0
    while True:
        vueltas += 1
        assert vueltas < 20
        r = st.search(lane=CARRIL, ledger=LANE, query="born red", limit=1, cursor=cur)
        vistos += [f["eid"] for f in r["filas"]]
        if not r["hay_mas"]:
            break
        cur = r["cursor"]
    assert sorted(vistos) == [f"tie-{i}" for i in range(4)], vistos
    assert len(vistos) == len(set(vistos))


# ── ⑪ el campo dice lo que mide ───────────────────────────────────────────────────
def test_el_campo_se_llama_bytes_presupuestados_y_no_promete_el_cable(tmp_path):
    st = _store(tmp_path, [dict(eid="e0", arrival=1, cuerpo="born red")])
    r = st.search(lane=CARRIL, ledger=LANE, query="born red", limit=5)
    assert "bytes_presupuestados" in r["truncado"]
    assert "bytes_respuesta" not in r["truncado"], (
        "el nombre promete bytes del cable y esto no ve el cable")
    # Suplente del test HTTP hasta que se cable: la serialización que emitiría un
    # `JSONResponse`. El de verdad es `len(response.content) <= MAX_RESPONSE_BYTES`.
    assert len(json.dumps(r, ensure_ascii=False).encode("utf-8")) <= ss.MAX_RESPONSE_BYTES


def test_las_marcas_son_desplazamientos_y_no_corchetes_dentro_del_texto(tmp_path):
    st = _store(tmp_path, [dict(eid="e0", arrival=1,
                                cuerpo="texto [con corchetes] y born red dentro")])
    f = st.search(lane=CARRIL, ledger=LANE, query="born", limit=5)["filas"][0]
    assert f["marcas"], "sin marcas no hay resaltado que verificar"
    for ini, fin in f["marcas"]:
        assert 0 <= ini < fin <= len(f["fragmento"])
        assert f["fragmento"][ini:fin] == "born"
    assert sc.CENTINELA_INI not in f["fragmento"]
    assert "[con corchetes]" in f["fragmento"], (
        "los corchetes del contenido siguen ahí: por eso el resaltado no puede usarlos")


# ── ⑫ head ⊆ body con rtrim, y su control negativo ────────────────────────────────
def test_el_espacio_final_del_titular_no_bloquea_el_rebuild(tmp_path):
    con = nueva_con(tmp_path)
    st = ss.SearchStore(con, cursor_key=CLAVE, acl=ACL)
    st.ensure_schema()
    st.set_acl(ACL)
    con.execute("INSERT INTO entries(ledger,eid,arrival,head,body) VALUES(?,?,?,?,?)",
                (LANE, "e0", 1, "titular con espacio final ",
                 "titular con espacio final\nborn red"))
    con.commit()
    st.rebuild()                                   # no debe reventar
    assert st.readiness()["ready"] is True


def test_CONTROL_NEGATIVO_un_head_ausente_del_body_SIGUE_bloqueando(tmp_path):
    """Sin este control, `rtrim` podría estar tapando el caso real y no sólo el espacio."""
    con = nueva_con(tmp_path)
    st = ss.SearchStore(con, cursor_key=CLAVE, acl=ACL)
    st.ensure_schema()
    st.set_acl(ACL)
    con.execute("INSERT INTO entries(ledger,eid,arrival,head,body) VALUES(?,?,?,?,?)",
                (LANE, "e0", 1, "PALABRAS QUE NO ESTAN", "born red y nada mas"))
    con.commit()
    with pytest.raises(ss.RebuildFailed, match="head"):
        st.rebuild()


# ── contrato de la conexión: no comitear el trabajo del llamante ──────────────────
def test_register_RECHAZA_una_conexion_con_transaccion_abierta(tmp_path):
    """Medido: asignar `isolation_level` COMITEA la transacción abierta.

        con.execute("INSERT ...")   -> in_transaction True
        con.isolation_level = None  -> in_transaction False, y otra conexión ya lo ve

    Poner la conexión «bajo contrato» publicaría trabajo que su dueño no había decidido
    publicar. Se rechaza ANTES de tocar nada.
    """
    ruta = str(tmp_path / "m2.sqlite")
    con = sqlite3.connect(ruta)
    con.executescript("CREATE TABLE t(a);")
    con.commit()
    con.execute("INSERT INTO t VALUES(1)")          # transacción implícita viva
    assert con.in_transaction

    with pytest.raises(ss.ConnectionContractViolation, match="transacción abierta"):
        ss.SearchStore.prepare_search_connection(con)

    assert con.in_transaction, "me la comí: la transacción del llamante ya no está"
    otra = sqlite3.connect(ruta)
    assert otra.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0, (
        "el trabajo a medias del llamante se publicó al poner la conexión bajo contrato")
    con.rollback()


def test_prepare_search_connection_acepta_una_conexion_limpia(tmp_path):
    con = sqlite3.connect(str(tmp_path / "m2.sqlite"))
    assert not con.in_transaction
    ss.SearchStore.prepare_search_connection(con)
    assert con.isolation_level is None
    assert con.execute("SELECT llminbox_proyecta('a')").fetchone()[0] == "a"


# ── gate de interfaz: registrar la UDF NO puede cambiar la atomicidad de nadie ─────
def test_register_udf_NO_toca_isolation_level_ni_la_transaccion_abierta(tmp_path):
    """La razón por la que son DOS funciones y no una.

    `register_udf` tiene que poder llamarse en la conexión del SERVICIO —que es quien
    escribe en `entries`— sin cambiarle la semántica de transacción. Fundida con la
    preparación de la conexión de búsqueda, enchufarla al servicio habría comiteado el
    trabajo a medias del llamante: asignar `isolation_level` cierra la transacción viva,
    y lo hace sin un solo error.

    ⊕/⊖ en el mismo test: se comprueba que la UDF QUEDA registrada (si no, «no tocó nada»
    sería cierto también para una función que no hace nada) y que la transacción sigue
    exactamente donde estaba.
    """
    ruta = str(tmp_path / "m2.sqlite")
    con = sqlite3.connect(ruta)
    con.executescript("CREATE TABLE t(a);")
    con.commit()
    nivel_antes = con.isolation_level
    con.execute("INSERT INTO t VALUES(1)")            # transacción implícita viva
    assert con.in_transaction

    ss.SearchStore.register_udf(con)                  # no puede exigir nada ni tocar nada

    assert con.in_transaction, "se comió la transacción del llamante"
    assert con.isolation_level == nivel_antes, (
        f"cambió el isolation_level de {nivel_antes!r} a {con.isolation_level!r}: eso "
        f"altera la atomicidad del código que ya usaba esta conexión")
    otra = sqlite3.connect(ruta)
    assert otra.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0, (
        "publicó el trabajo a medias del llamante")
    # ⊕ y la UDF SÍ quedó registrada: si no, este test pasaría con un `def register_udf:
    # pass` y estaría certificando una función vacía.
    assert con.execute("SELECT llminbox_proyecta('AB')").fetchone()[0] == "ab"
    con.rollback()
    assert otra.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
