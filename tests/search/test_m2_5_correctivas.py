"""Correctivas M2-5. Cada test es el falsador de un hallazgo REPRODUCIDO antes de curarlo."""

import json
import sqlite3

import pytest

from .conftest import ACL, CARRIL, CLAVE, LANE, mete, nueva_con
import search_contract as sc
import search_cursor as scur
import search_store as ss


def _store(tmp_path, filas=None, nombre="m2.sqlite"):
    con = nueva_con(tmp_path, nombre)
    st = ss.SearchStore(con, cursor_key=CLAVE, acl=ACL)
    st.ensure_schema()
    st.set_acl(ACL)
    for kw in (filas or []):
        mete(con, LANE, **kw)
    con.commit()
    if filas:
        st.rebuild()
    return st


# ── P0 · el DDL se compara contra el CÓDIGO, no contra lo que había ───────────────
def test_un_trigger_NO_OP_PREEXISTENTE_no_se_deja_bendecir_por_el_rebuild(tmp_path):
    """EL P0. Reproducido antes de curar: `readiness=True`, el alta nueva NO entraba al
    FTS, y `readiness` seguía `True` para siempre — porque el sello fotografiaba el DDL
    vivo y luego se comparaba consigo mismo."""
    con = nueva_con(tmp_path)
    st = ss.SearchStore(con, cursor_key=CLAVE, acl=ACL)
    st.ensure_schema()
    st.set_acl(ACL)
    con.execute("DROP TRIGGER search_ai")
    con.execute("CREATE TRIGGER search_ai AFTER INSERT ON entries BEGIN SELECT 1; END")
    mete(con, LANE, "e0", 1, cuerpo="born red")
    con.commit()

    with pytest.raises(ss.RebuildFailed, match="search_ai"):
        st.rebuild()
    assert st.readiness()["ready"] is False
    assert st.state() == ss.ESTADO_CONSTRUYENDO


@pytest.mark.parametrize("objeto,ddl", [
    ("search_ai", "CREATE TRIGGER search_ai AFTER INSERT ON entries BEGIN SELECT 1; END"),
    ("search_ad", "CREATE TRIGGER search_ad AFTER DELETE ON entries BEGIN SELECT 1; END"),
    ("search_view", "CREATE VIEW search_view AS SELECT d.rid AS rowid, e.body AS body"
                    "  FROM search_documents d JOIN entries e"
                    "    ON e.ledger=d.ledger AND e.eid=d.eid"),
])
def test_cualquier_objeto_con_OTRO_cuerpo_pone_not_ready(tmp_path, objeto, ddl):
    st = _store(tmp_path, [dict(eid="e0", arrival=1, cuerpo="born red")])
    assert st.readiness()["ready"] is True
    tipo = "VIEW" if objeto.startswith("search_v") else "TRIGGER"
    st.con.execute(f"DROP {tipo} {objeto}")
    st.con.execute(ddl)
    r = st.readiness()
    assert r["ready"] is False and objeto in " ".join(r["problems"])
    assert r["missing"] == [], "el NOMBRE sigue: lo que cambió es el cuerpo"


def test_el_manifiesto_esperado_cubre_tablas_indices_vista_fts_y_triggers():
    esperado = ss.SearchStore.ddl_esperado()
    nombres = {identidad[2] for identidad in esperado}
    for nombre in ("search_documents", "search_state", "i_sd_ledger", "search_view",
                   "search_fts", *ss.TRIGGERS):
        assert nombre in nombres, f"{nombre} no está en el manifiesto del código"
    assert all(len(identidad) == 4 for identidad in esperado)
    assert ("main", "trigger", "search_ai", "entries") in esperado
    assert ("main", "table", "search_documents", "search_documents") in esperado
    assert ss.TRIGGERS_COMPARTIDOS_TOLERADOS == {}
    assert all(v.strip() for v in esperado.values())


def test_borrar_un_INDICE_tambien_pone_not_ready(tmp_path):
    """Los índices entran en el manifiesto: el anterior sólo miraba vista, FTS y triggers."""
    st = _store(tmp_path, [dict(eid="e0", arrival=1, cuerpo="born red")])
    st.con.execute("DROP INDEX i_sd_ledger")
    r = st.readiness()
    assert r["ready"] is False and "i_sd_ledger" in " ".join(r["problems"])


# ── P0 · huellas: parseo fail-closed y juego EXACTO ───────────────────────────────
@pytest.mark.parametrize("valor", ["{}", "[]", "null", '"texto"', "{roto", "", "[1,2]"])
def test_huellas_degeneradas_NUNCA_dejan_ready(tmp_path, valor):
    """`{}` y `[]` daban `ready=True` —el bucle no recorría nada— y un JSON roto SUBÍA un
    `JSONDecodeError` desde `readiness`, que es lo último que puede hacer una función de
    salud."""
    st = _store(tmp_path, [dict(eid="e0", arrival=1, cuerpo="born red")])
    st._set("huellas", valor)
    r = st.readiness()                       # no lanza
    assert r["ready"] is False, f"aceptó huellas {valor!r}"


def test_el_juego_de_huellas_se_compara_en_LAS_DOS_direcciones(tmp_path):
    st = _store(tmp_path, [dict(eid="e0", arrival=1, cuerpo="born red")])
    vivas = json.loads(st._get("huellas"))
    assert st.readiness()["ready"] is True

    faltando = {k: v for k, v in list(vivas.items())[1:]}          # falta una
    st._set("huellas", json.dumps(faltando))
    assert st.readiness()["ready"] is False

    sobrando = dict(vivas, objeto_fantasma="0" * 64)               # sobra una
    st._set("huellas", json.dumps(sobrando))
    assert st.readiness()["ready"] is False


def test_una_huella_DISTINTA_con_el_mismo_juego_de_nombres_pone_not_ready(tmp_path):
    """El tercer caso del sello, y el que faltaba: los NOMBRES cuadran y el CONTENIDO no.

    Es la única cara del sello que `_desajustes_ddl` no cubre. Aquélla compara el DDL vivo
    contra el manifiesto del CÓDIGO —y por tanto caza que el esquema esté mal—; ésta caza
    que la base la haya SELLADO otro código: mismo juego de objetos, otra forma en el
    momento del rebuild. Sin ella, un índice construido por una versión distinta se
    presenta como `ready` mientras las consultas de ésta corren sobre él.

    ⊖ MEDIDO: el mutante `M44` (`if distintos:` → `if False:`) SOBREVIVÍA a la suite
    entera — 70 mutantes corridos desde una sola instantánea, un solo VIVO, éste.
    """
    st = _store(tmp_path, [dict(eid="e0", arrival=1, cuerpo="born red")])
    vivas = json.loads(st._get("huellas"))

    # ⊕ CONTROL: con el sello BUENO está listo, y el juego de nombres es el mismo en los
    # dos casos. Lo único que cambia abajo es UN valor.
    assert st.readiness()["ready"] is True
    assert len(vivas) >= 2, "el sello tiene que cubrir más de un objeto para discriminar"

    victima = sorted(vivas)[0]
    torcidas = dict(vivas, **{victima: "f" * 64})
    assert set(torcidas) == set(vivas), "el juego de NOMBRES no puede cambiar"
    st._set("huellas", json.dumps(torcidas))

    r = st.readiness()
    assert r["ready"] is False, "una huella sellada distinta pasó por sana"
    assert any("huella distinta en" in p and victima in p for p in r["problems"]), (
        f"el problema no nombra la huella torcida: {r['problems']}")

    # Y NO se confunde con las otras dos caras: aquí el juego cuadra y el DDL vivo es el
    # del código, así que el único problema tiene que ser el del sello.
    assert not any("no cuadra" in p for p in r["problems"]), r["problems"]
    assert st._desajustes_ddl() == [], (
        "si el DDL vivo también estuviera mal, este test no probaría el sello")


# ── P1 · framing con CRLF y CR ────────────────────────────────────────────────────
@pytest.mark.parametrize("salto", ["\n", "\r\n", "\r"])
def test_el_framing_se_retira_con_cualquier_terminador(tmp_path, salto):
    """Con `CRLF`, partir por `"\\n"` dejaba el `\\r` pegado, el regex no casaba y
    `event_id`/`payload_sha` ENTRABAN al índice. Reproducido antes de curar."""
    cuerpo = salto.join([
        "<!-- LLMINBOX-EVENT-BEGIN event_id=evt_crlf payload_sha=cafe1234 -->",
        "titular real", "born red cuerpo",
        "<!-- LLMINBOX-EVENT-END event_id=evt_crlf -->"])
    st = _store(tmp_path)
    st.con.execute("INSERT INTO entries(ledger,eid,arrival,head,body) VALUES(?,?,?,?,?)",
                   (LANE, "e0", 1, "titular real", cuerpo))
    st.con.commit()
    st.rebuild()
    proyectado = st.con.execute("SELECT body FROM search_view").fetchone()["body"]
    for prohibido in ("event_id", "payload_sha", "evt_crlf", "cafe1234", "<!--", "\r"):
        assert prohibido not in proyectado, f"{prohibido!r} sobrevivió con salto {salto!r}"
    for aguja in ("event_id", "payload_sha", "evt_crlf"):
        assert st.search(lane=CARRIL, ledger=LANE, query=aguja, limit=5)["filas"] == []
    assert st.search(lane=CARRIL, ledger=LANE, query="born red", limit=5)["filas"]


@pytest.mark.parametrize("salto", ["\n", "\r\n", "\r"])
def test_trama_incompleta_y_sangrada_tambien_se_retiran(tmp_path, salto):
    proyectado = sc.project_body(
        f"   <!-- LLMINBOX-EVENT-BEGIN event_id=evt_x -->{salto}born red")
    assert "event_id" not in proyectado and "born red" in proyectado


# ── P1 · marcas: frontera retenida, sin señalar la elipsis ────────────────────────
def test_una_marca_en_la_zona_CORTADA_no_apunta_a_la_elipsis(tmp_path):
    """Reproducido: un `match` en el primer carácter omitido quedaba en `[2045, 2046]`,
    o sea señalando el `…` en vez de texto."""
    # POR EL CAMINO REAL: `search`. Llamar a `_marcas`/`_recorta` sueltas probaba las
    # piezas y dejaba VIVO el mutante que ata las marcas al fragmento CON elipsis, que es
    # donde vive el defecto.
    #
    # Un solo token gigante con la aguja al final: el `snippet` lo devuelve entero, el
    # recorte se lleva la cola, y la marca cae en la zona cortada.
    relleno = "z" * (ss.MAX_SNIPPET_BYTES * 2)
    st = _store(tmp_path, [dict(eid="e0", arrival=1,
                                cuerpo=f"born red {relleno} aguja")])
    r = st.search(lane=CARRIL, ledger=LANE, query="born aguja", limit=5)
    f = r["filas"][0]
    assert r["truncado"]["snippets_recortados"] >= 1, (
        "el fragmento no se recortó: el caso no discrimina")
    assert f["fragmento"].endswith(ss.MARCA_RECORTE)
    retenido = len(f["fragmento"]) - len(ss.MARCA_RECORTE)
    for ini, fin in f["marcas"]:
        assert fin <= retenido, (
            f"la marca [{ini},{fin}] entra en la zona de la elipsis (retenido={retenido})")
        assert ss.MARCA_RECORTE not in f["fragmento"][ini:fin]


@pytest.mark.parametrize("fin,sobrevive", [(10, True), (11, False), (12, False)])
def test_la_frontera_de_las_marcas_es_lo_RETENIDO_no_el_fragmento(fin, sobrevive):
    """El caso exacto: `retenido=10`, fragmento de 11 (10 + la elipsis).

    Una marca que acaba en 11 señala el `…`. Filtrar por `len(fragmento)` la deja pasar
    —es el defecto reproducido, `[2045, 2046]` sobre la elipsis— y filtrar por `retenido`
    la descarta. Por el camino de `search` este caso depende de dónde ponga el resaltado
    el `snippet`; aquí se fija sin depender de eso.
    """
    marcas = [[0, 3], [8, fin]]
    quedan = ss.SearchStore._marcas_validas(marcas, 10)
    assert ([8, fin] in quedan) is sobrevive


def test_las_marcas_declaran_su_unidad_y_apuntan_a_texto(tmp_path):
    st = _store(tmp_path, [dict(eid="e0", arrival=1, cuerpo="born red aguja")])
    r = st.search(lane=CARRIL, ledger=LANE, query="aguja", limit=5)
    assert r["unidad_marcas"] == "unicode_codepoint"
    f = r["filas"][0]
    assert f["marcas"], "sin marcas no hay nada que verificar"
    for ini, fin in f["marcas"]:
        assert f["fragmento"][ini:fin] == "aguja"


def test_la_unidad_importa_de_verdad_con_un_emoji(tmp_path):
    """El mismo resaltado da desplazamientos distintos según la unidad: por eso se declara.

    `a🙂aguja` — en puntos de código la aguja empieza en 2; en bytes UTF-8, en 5; en
    unidades UTF-16 (JavaScript), en 3.
    """
    st = _store(tmp_path, [dict(eid="e0", arrival=1, cuerpo="a🙂 aguja born red")])
    f = st.search(lane=CARRIL, ledger=LANE, query="aguja", limit=5)["filas"][0]
    ini, fin = f["marcas"][0]
    frag = f["fragmento"]
    assert frag[ini:fin] == "aguja"                                    # codepoints
    utf8 = len(frag[:ini].encode("utf-8"))
    utf16 = len(frag[:ini].encode("utf-16-le")) // 2
    assert utf8 != ini or utf16 != ini, (
        "el fixture no tiene ningún carácter fuera del BMP/ASCII antes de la marca: "
        "el test no demostraría que la unidad importa")


# ── P1 · techo: separadores y peor caso real del sobre ────────────────────────────
def test_el_presupuesto_cuenta_los_SEPARADORES_del_array(tmp_path):
    """`json.dumps` mete `", "` entre elementos: `2*(n-1)` bytes que la suma de filas
    sueltas no ve. Con 100 filas son ~198 fuera del presupuesto."""
    st = _store(tmp_path, [dict(eid=f"e{i:03d}", arrival=i + 1, head=f"t{i} " + "y" * 1190,
                                cuerpo="born red " + "z" * 1200) for i in range(101)])
    r = st.search(lane=CARRIL, ledger=LANE, query="born red", limit=100)
    total = len(json.dumps(r, ensure_ascii=False).encode("utf-8"))
    assert total <= ss.MAX_RESPONSE_BYTES, f"{total} > {ss.MAX_RESPONSE_BYTES}"
    assert r["truncado"]["bytes_presupuestados"] == total
    suma_filas = sum(ss.SearchStore._bytes_fila(st, f) for f in r["filas"])
    n = len(r["filas"])
    assert n > 10, f"con {n} filas los separadores no pesan: el caso no discriminaría"
    # ⊕ EL CONTROL QUE DISCRIMINA: el presupuesto que se gastó tiene que incluir los
    # separadores. Comprobar sólo `total <= techo` deja vivo el mutante que no los cuenta,
    # porque casi siempre sobra margen — y el día que no sobre, la respuesta se pasa
    # declarándose dentro.
    # ⊕ EL CONTROL QUE DISCRIMINA: `bytes_filas` es lo que se gastó del presupuesto.
    # Tiene que cubrir las filas MÁS los separadores del array. Comprobar sólo
    # `total <= techo` deja vivo el mutante que no los cuenta, porque casi siempre sobra
    # margen — y el día que no sobra, la respuesta se pasa declarándose dentro.
    assert r["truncado"]["bytes_filas"] >= suma_filas + 2 * (n - 1) - 2, (
        f"el presupuesto no cubre los {2 * (n - 1)} bytes de separadores: "
        f"gastado {r['truncado']['bytes_filas']}, filas {suma_filas}")


def test_el_sobre_reserva_el_eid_MAS_LARGO_QUE_SE_PERMITE(tmp_path):
    """Reservaba un `eid` de 64 mientras la frontera acepta `MAX_EID_BYTES`: el sobre se
    quedaba corto justo con las entradas que más pesan."""
    st = _store(tmp_path, [dict(eid=c * scur.MAX_EID_BYTES, arrival=i + 1,
                                head="t " + "y" * 1500, cuerpo="born red " + "z" * 1500)
                           for i, c in enumerate("abcdefghij")])
    r = st.search(lane=CARRIL, ledger=LANE, query="born red", limit=3)
    assert r["cursor"], "sin cursor no hay sobre que medir"
    total = len(json.dumps(r, ensure_ascii=False).encode("utf-8"))
    assert total <= ss.MAX_RESPONSE_BYTES

    sobre = st._envelope_bytes(st.generation(), r["filtro_sha256"], r["orden"])
    real = len(ss.SearchStore._serializa({
        "filas": [], "cursor": r["cursor"], "hay_mas": True,
        "truncado": r["truncado"], "orden": r["orden"],
        "unidad_marcas": r["unidad_marcas"], "generacion": r["generacion"],
        "filtro_sha256": r["filtro_sha256"]}))
    assert sobre >= real, f"la reserva {sobre} es menor que el sobre real {real}"
    # ⊕ EL CONTROL QUE DISCRIMINA, y va sobre el CURSOR, no sobre el sobre entero: los
    # contadores inflados de la reserva tienen holgura de sobra para absorber 64 caracteres
    # de `eid`, así que comparar sobres no separaba las dos versiones. Lo que tiene que
    # cubrir la reserva es el cursor real MÁS LARGO que este servicio puede emitir.
    peor = st._cursor_peor(r["generacion"], r["filtro_sha256"])
    assert len(peor) >= len(r["cursor"]), (
        f"el cursor real ({len(r['cursor'])}) es más largo que el peor caso reservado "
        f"({len(peor)}): la reserva se calcula con el `eid` que suele haber, no con el "
        f"que la frontera permite")


# ── canon del keyset, documentado y fijado ────────────────────────────────────────
def test_el_keyset_canonico_sigue_siendo_arrival_eid(tmp_path):
    """`(arrival, eid)` es orden TOTAL en 12/12 carriles (medido por `qa`); `ts` colisiona
    hasta el 70,7 %. Este test fija el canon para que nadie lo cambie por `ts` al pasar."""
    assert " ORDER BY e.arrival DESC, e.eid DESC" in ss.SQL_BUSQUEDA
    assert "ORDER BY e.ts" not in ss.SQL_BUSQUEDA
    st = _store(tmp_path, [dict(eid=f"tie-{i}", arrival=7, ts="2026-09-05T00:00:00Z",
                                cuerpo="born red") for i in range(4)])
    vistos, cur = [], None
    for _ in range(10):
        r = st.search(lane=CARRIL, ledger=LANE, query="born red", limit=1, cursor=cur)
        vistos += [f["eid"] for f in r["filas"]]
        if not r["hay_mas"]:
            break
        cur = r["cursor"]
    assert sorted(vistos) == [f"tie-{i}" for i in range(4)]


# ── el manifiesto del CÓDIGO es el que decide, no el sello ────────────────────────
def test_con_las_huellas_RESELLADAS_sobre_el_DDL_roto_sigue_not_ready(tmp_path):
    """El sello se puede re-hacer; el manifiesto del código no.

    Aquí se rompe el DDL y se vuelven a sellar las huellas de lo roto — que es justo lo
    que hacía el rebuild antes de esta tanda. Si `readiness` sólo mirara el sello, esto
    daría verde. Tiene que seguir en rojo porque el DDL vivo no es el que declara el
    código.
    """
    st = _store(tmp_path, [dict(eid="e0", arrival=1, cuerpo="born red")])
    st.con.execute("DROP TRIGGER search_ai")
    st.con.execute("CREATE TRIGGER search_ai AFTER INSERT ON entries BEGIN SELECT 1; END")
    st._set("huellas", json.dumps(st._huellas_objetos()))      # se auto-bendice el sello
    r = st.readiness()
    assert r["ready"] is False, "el sello re-hecho bendijo un trigger vaciado"
    assert "search_ai" in " ".join(r["problems"])


def test_un_objeto_QUE_FALTA_se_detecta_aunque_el_sello_cuadre(tmp_path):
    """La dirección «falta» del keyset. Con el sello re-hecho, el único camino que puede
    cazarlo es comparar contra el manifiesto del código en las DOS direcciones."""
    st = _store(tmp_path, [dict(eid="e0", arrival=1, cuerpo="born red")])
    st.con.execute("DROP TRIGGER search_ad")
    st._set("huellas", json.dumps(st._huellas_objetos()))
    r = st.readiness()
    assert r["ready"] is False
    assert "falta search_ad" in " ".join(r["problems"]), r["problems"]
