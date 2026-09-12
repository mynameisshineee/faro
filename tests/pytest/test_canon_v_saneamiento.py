"""`tipo` pasa a ser VERDAD GLOBAL derivada del lexema, sin excepciones históricas.

#15 introdujo el mecanismo y fue estrictamente aditiva: rellenó huecos y no tocó
ninguna fila con valor. Eso dejó la columna a medias — 29.708 filas de 68.822
seguían diciendo algo que su lexema no sostiene, porque el matcher por subcadena
las clasificó desde la PROSA.

Medido antes de escribir el diff:

    tipo → NULL ......... 29.414   HEARTBEAT 21.630 · FYI 4.943 · ACK 1.039 · …
    tipo → OTRO tipo .......  294   FYI→RESP 112 · FYI→ACK 67 · ACK→RESP 29 · …
    NULL → tipo ............... 0   (#15 ya los rellenó)

Las 294 sustituciones son falsos positivos corrigiéndose: `FYI → RESP` son
entradas cuyo lexema real es `RESP` y a las que el matcher puso `FYI` porque la
palabra aparecía en el texto.

LA AUTORIDAD ES `raw_tipo`, aunque eso signifique PERDER clasificación derivada
históricamente. Una entrada sin lexema no tiene tipo, por mucho que alguien se lo
adivinara antes.
"""
from __future__ import annotations


def _sanea(tmp_path, monkeypatch, filas):
    """Siembra (tipo, raw_tipo) a mano y corre el saneamiento. Devuelve el después."""
    from fastapi.testclient import TestClient
    from .conftest import construir, db_directa
    s_ = construir(tmp_path, monkeypatch)
    (tmp_path / "DEMO-LEDGER.md").write_text(
        "".join(f"### [cto-A → backend · X{i}] 2026-08-20T10:0{i}:00Z — sembrada {i}\n\ncuerpo\n"
                for i in range(len(filas))))
    with TestClient(s_.app):
        s_.barrido()
    con = db_directa(s_)
    eids = [r["eid"] for r in con.execute("SELECT eid FROM entries ORDER BY seq")]
    assert len(eids) >= len(filas), "no se sembraron suficientes entradas"
    for eid, (tipo, raw) in zip(eids, filas):
        con.execute("UPDATE entries SET tipo=?, raw_tipo=? WHERE eid=?", (tipo, raw, eid))
    con.execute("DELETE FROM meta WHERE k='canon_v'")
    con.commit()
    s_.migrar_canon(con)
    ver = db_directa(s_)
    return [dict(ver.execute("SELECT tipo, raw_tipo FROM entries WHERE eid=?", (e,)).fetchone())
            for e in eids[:len(filas)]]


def test_la_autoridad_es_el_lexema_aunque_se_pierda_clasificacion(tmp_path, monkeypatch):
    """Los cuatro casos que definen el contrato.

    FALSADOR: cualquier `old or new`, o una excepción para HEARTBEAT, rompe uno.
    """
    d = _sanea(tmp_path, monkeypatch, [
        ("FYI", "RESP"),            # ① falso positivo de prosa → se corrige al lexema
        ("ACK", None),              # ② sin lexema NO hay tipo, aunque lo tuviera
        ("HEARTBEAT", "HEARTBEAT"), # ③ fuera del canon → NULL
        ("FYI", "MEDIDO"),          # ④ alias adjudicado
    ])
    assert d[0]["tipo"] == "RESP", d[0]
    assert d[1]["tipo"] is None, f"rescató una clasificación sin evidencia lexical: {d[1]}"
    assert d[2]["tipo"] is None, d[2]
    assert d[3]["tipo"] == "MEASURED", d[3]
    # y el lexema, BYTE-IDÉNTICO en los cuatro
    assert [x["raw_tipo"] for x in d] == ["RESP", None, "HEARTBEAT", "MEDIDO"]


def test_la_invariante_final_no_admite_excepciones(tmp_path, monkeypatch):
    """`tipo IS NULL OR tipo ∈ CANON_TIPOS`, cero filas fuera.

    FALSADOR: preservar cualquier valor histórico deja HEARTBEAT dentro y cae.
    """
    import ledger_parse as lp
    d = _sanea(tmp_path, monkeypatch, [
        ("HEARTBEAT", "HEARTBEAT"), ("DONE", "DONE"), ("CLAIM", "CLAIM"),
        ("MSG", "MSG"), ("FYI", None), ("ACK", "ADJUDICADO"),
    ])
    fuera = [x for x in d if x["tipo"] is not None and x["tipo"] not in lp.CANON_TIPOS]
    assert fuera == [], f"quedaron tipos fuera del canon: {fuera}"


# ── el contrato de la API: `tipo` gobernado, `raw_tipo` legacy ────────────────

def test_un_tipo_no_canonico_FALLA_en_vez_de_devolver_vacio(cliente):
    """`?tipo=HEARTBEAT` devolvía filas; tras el saneamiento devolvería `[]`.

    Y `[]` es la peor respuesta posible: un cliente antiguo lo lee como «no hay
    latidos» cuando significa «tu consulta ya no es válida bajo este contrato».
    Es la rotura silenciosa que llevamos toda la semana quitando de otros sitios.

    Si `tipo` es vocabulario GOBERNADO, el endpoint tiene que hacerlo cumplir.

    FALSADOR: devolver `[]` en vez de 422 hace que las dos aserciones caigan.
    """
    r = cliente.get("/entries?tipo=HEARTBEAT")
    assert r.status_code == 422, f"{r.status_code}: {r.text[:120]}"
    d = r.json()
    assert "raw_tipo" in str(d), d          # dice DÓNDE mirar
    assert "HEARTBEAT" in str(d), d         # y qué valor rechazó


def test_el_alias_se_normaliza_tambien_en_el_filtro(tmp_path, monkeypatch):
    """`?tipo=MEDIDO` → `MEASURED`: un único normalizador gobierna almacenamiento
    Y filtro, o la API contradice a la base.

    ⚠️ Mi primera versión sólo comprobaba `status_code == 200` — **vacuo**: un
    filtro que compare crudo devuelve `[]` y sigue dando 200. Lo cazó CodeRabbit.
    Hay que SEMBRAR la entrada y exigir que la consulta por el alias la ENCUENTRE.

    FALSADOR: comparar el parámetro crudo en vez del canonizado devuelve 0 filas.
    """
    from fastapi.testclient import TestClient
    from .conftest import construir
    s_ = construir(tmp_path, monkeypatch)
    (tmp_path / "DEMO-LEDGER.md").write_text(
        "### [cto-A → backend · MEDIDO] 2026-08-20T10:00:00Z — sembrada alias\n\ncuerpo\n")
    c = TestClient(s_.app)
    c.headers.update({"X-Llminbox-Token": "test-token"})
    with c:
        s_.barrido()
        for grafia in ("MEDIDO", "medido", "MEASURED"):
            d = [x for x in c.get(f"/entries?tipo={grafia}").json()
                 if "sembrada alias" in (x["head"] or "")]
            assert d, f"?tipo={grafia} no encontró la entrada"
            assert d[0]["tipo"] == "MEASURED", d[0]      # guardado canonizado
            assert d[0]["raw_tipo"] == "MEDIDO", d[0]    # lexema intacto


def test_raw_tipo_es_el_filtro_del_protocolo_legacy(tmp_path, monkeypatch):
    """`?raw_tipo=HEARTBEAT` encuentra los latidos AUNQUE `tipo` sea NULL.

    Es la vía de salida que el 422 recomienda: sin ella, decirle a un cliente «usa
    raw_tipo» sería mandarlo a un parámetro que no existe.

    FALSADOR: sin el filtro nuevo, la primera aserción da 0; si el filtro mirara
    `tipo`, también.
    """
    from fastapi.testclient import TestClient
    from .conftest import construir, db_directa
    s_ = construir(tmp_path, monkeypatch)
    (tmp_path / "DEMO-LEDGER.md").write_text(
        "### [HEARTBEAT cto-A] 2026-08-20T10:00:00Z — latido uno\n\ncuerpo\n")
    c = TestClient(s_.app)
    c.headers.update({"X-Llminbox-Token": "test-token"})
    with c:
        s_.barrido()
        con = db_directa(s_)
        assert con.execute("SELECT tipo FROM entries WHERE raw_tipo='HEARTBEAT'").fetchone()["tipo"] is None
        d = c.get("/entries?raw_tipo=HEARTBEAT").json()
        assert len(d) >= 1, "el filtro por lexema no encuentra el latido"
        assert d[0]["raw_tipo"] == "HEARTBEAT"    # devuelve el literal, sin normalizar
        # búsqueda sin caja, pero el valor guardado NO se toca
        assert len(c.get("/entries?raw_tipo=heartbeat").json()) >= 1


# ── las tres revisiones, y sus DOS caminos independientes ────────────────────

def test_CANON_V_rederiva_tipo_SIN_reparsear_el_markdown(tmp_path, monkeypatch):
    """Camino ①: cambia la semántica, `raw_tipo` intacto, `tipo` se recalcula.

    Es lo que hace barato cambiar el vocabulario: no obliga a re-leer 68.822
    cabeceras de markdown. `barrido()` ni siquiera tiene que tocar el fichero.

    FALSADOR: un `CANON_V` que no dispare la rederivación deja el tipo viejo.
    """
    from fastapi.testclient import TestClient
    from .conftest import construir, db_directa
    s_ = construir(tmp_path, monkeypatch)
    (tmp_path / "DEMO-LEDGER.md").write_text(
        "### [cto-A → backend · RESP] 2026-08-20T10:00:00Z — sembrada\n\ncuerpo\n")
    with TestClient(s_.app):
        s_.barrido()

    con = db_directa(s_)
    # Estado histórico incoherente: el lexema dice RESP, el tipo dice FYI.
    con.execute("UPDATE entries SET tipo='FYI' WHERE raw_tipo='RESP'")
    con.execute("DELETE FROM meta WHERE k='canon_v'")     # canon sin aplicar
    con.commit()
    raw_antes = con.execute("SELECT raw_tipo, head FROM entries WHERE raw_tipo='RESP'").fetchone()

    s_.migrar_canon(con)

    ver = db_directa(s_)
    f = ver.execute("SELECT tipo, raw_tipo, head FROM entries WHERE raw_tipo='RESP'").fetchone()
    assert f["tipo"] == "RESP", f"CANON_V no rederivó: {f['tipo']!r}"
    assert f["raw_tipo"] == raw_antes["raw_tipo"], "tocó el lexema"
    assert f["head"] == raw_antes["head"], "re-parseó el markdown: no hacía falta"
    assert ver.execute("SELECT v FROM meta WHERE k='canon_v'").fetchone()["v"] == s_.CANON_V


def test_PARSER_V_deja_tipo_COHERENTE_con_el_raw_nuevo(tmp_path, monkeypatch):
    """Camino ②: cambia la extracción, `raw_tipo` cambia, y `tipo` NO puede
    quedarse con el valor del lexema viejo.

    Es el agujero que #15 tapó a mano con «REDERIVAR jamás toca tipo»: correcto
    entonces —B no podía sanear—, pero fosilizarlo dejaría un `tipo` derivado de un
    lexema que ya no existe. La política definitiva es que tras un cambio de parser
    el tipo queda coherente con el raw NUEVO.

    FALSADOR: si el bump de PARSER_V recalcula `raw_tipo` y no arrastra `tipo`,
    la entrada queda con el tipo del lexema anterior.
    """
    from fastapi.testclient import TestClient
    from .conftest import construir, db_directa
    import os

    s_ = construir(tmp_path, monkeypatch)
    led = tmp_path / "DEMO-LEDGER.md"
    led.write_text("### [wiki-vault·64bis] 2026-07-19T09:15:17Z — sin ranura de tipo\n\ncuerpo\n")
    with TestClient(s_.app):
        s_.barrido()

    con = db_directa(s_)
    fila = con.execute("SELECT eid FROM entries WHERE head LIKE '%sin ranura%'").fetchone()
    assert fila, "no se sembró"
    # ESTADO DE UN PARSER VIEJO: capturaba el carril como lexema y lo interpretaba.
    con.execute("UPDATE entries SET raw_tipo='RESP', tipo='RESP' WHERE eid=?", (fila["eid"],))
    con.execute("INSERT OR REPLACE INTO meta VALUES ('parser_v', '1')")
    con.execute("INSERT OR REPLACE INTO meta VALUES ('canon_v', ?)", (s_.CANON_V,))
    con.commit()
    contenido, mtime = led.read_bytes(), led.stat().st_mtime

    s2 = construir(tmp_path, monkeypatch)
    led.write_bytes(contenido)
    os.utime(led, (mtime, mtime))
    with TestClient(s2.app):
        s2.barrido()

    ver = db_directa(s2)
    f = ver.execute("SELECT tipo, raw_tipo FROM entries WHERE eid=?", (fila["eid"],)).fetchone()
    assert f["raw_tipo"] is None, f"el parser nuevo debía retirar el lexema: {f['raw_tipo']!r}"
    assert f["tipo"] is None, \
        (f"`tipo`={f['tipo']!r} sobrevive a un `raw_tipo` que ya no existe: quedó "
         f"derivado de un lexema retirado")


def test_migrar_raw_tipo_arrastra_el_tipo_en_la_MISMA_transaccion(tmp_path, monkeypatch):
    """El tercer writer de `raw_tipo`, y el que quedaba sin la regla.

    `reindex()` ya arrastra `tipo` cuando el lexema cambia. `migrar_raw_tipo()` no:
    recalcula el lexema de TODO el corpus desde el `head` guardado y dejaba el
    tipo con el valor del lexema anterior. Y el saneamiento no lo rescata, porque
    `migrar_canon()` corre DESPUÉS y hace `return` si su sello ya está puesto.

    El caso que lo hace visible es un ledger DORMIDO: `barrido()` lo salta cuando
    su tamaño y mtime no cambian, así que no hay `reindex()` que lo arregle nunca.

    La regla, ahora para los tres writers: **quien cambia `raw_tipo` actualiza
    `tipo` en la misma transacción**. No hace falta invalidar `canon_v` ni inventar
    un sello compuesto — eso volvería a mezclar materialización con semántica.

    FALSADOR: si `migrar_raw_tipo` sólo escribe `raw_tipo`, el tipo sobrevive a su
    lexema y la segunda aserción cae.
    """
    from fastapi.testclient import TestClient
    from .conftest import construir, db_directa
    s_ = construir(tmp_path, monkeypatch)
    (tmp_path / "DEMO-LEDGER.md").write_text(
        "### [wiki-vault·64bis] 2026-07-19T09:15:17Z — ledger dormido\n\ncuerpo\n")
    with TestClient(s_.app):
        s_.barrido()

    con = db_directa(s_)
    e = con.execute("SELECT eid FROM entries WHERE head LIKE '%dormido%'").fetchone()
    assert e, "no se sembró"
    # Estado de un extractor VIEJO: capturó el carril y alguien lo interpretó.
    # El canon YA está aplicado —su sello es el actual—, así que `migrar_canon()`
    # no volverá a mirar nada: si el arrastre no ocurre aquí, no ocurre.
    con.execute("UPDATE entries SET raw_tipo='RESP', tipo='RESP' WHERE eid=?", (e["eid"],))
    con.execute("DELETE FROM meta WHERE k='raw_tipo_v'")
    con.execute("INSERT OR REPLACE INTO meta VALUES ('canon_v', ?)", (s_.CANON_V,))
    con.commit()

    s_.migrar_raw_tipo(con)      # sólo ésta: sin reindex, sin tocar el fichero

    ver = db_directa(s_)
    f = ver.execute("SELECT tipo, raw_tipo FROM entries WHERE eid=?", (e["eid"],)).fetchone()
    assert f["raw_tipo"] is None, f"el extractor nuevo debía retirar el lexema: {f['raw_tipo']!r}"
    assert f["tipo"] is None, \
        f"`tipo`={f['tipo']!r} sobrevive a su lexema: el arrastre no ocurrió en esta transacción"


def test_el_indice_de_raw_tipo_existe_en_una_base_NUEVA(tmp_path, monkeypatch):
    """El índice se creaba ANTES de la columna que indexa.

    `SCHEMA` crea `entries` sin `raw_tipo` —la columna llega por la vía aditiva de
    `COLUMNAS_ANADIDAS`—, así que en una base nueva mi `CREATE INDEX` fallaba con
    `no such column: raw_tipo`, el `except` lo registraba y nadie reintentaba: la
    base quedaba **sin el índice que `?raw_tipo=` necesita**, en silencio.

    Es la tercera vez en esta PR que el orden de dos operaciones importa más que
    su contenido —el índice dentro de `SCHEMA`, el saneamiento tras el sello, y
    ahora esto—. El síntoma nunca fue un fallo ruidoso.

    FALSADOR: con el bloque antes del bucle de columnas, el índice no existe.
    """
    from fastapi.testclient import TestClient
    from .conftest import construir, db_directa
    s_ = construir(tmp_path, monkeypatch)          # base recién creada
    with TestClient(s_.app):
        s_.barrido()
    idx = [r[0] for r in db_directa(s_).execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='entries'")]
    assert "i_raw_tipo" in idx, f"el índice no se creó en una base nueva: {idx}"


def test_un_tipo_VACIO_tambien_falla(cliente):
    """`?tipo=` entraba por `if tipo:` y equivalía a NO poner filtro.

    Si `tipo` es vocabulario gobernado, cualquier valor SUMINISTRADO y no
    canonizable falla — y la cadena vacía es un valor suministrado. Ignorarla en
    silencio devuelve el corpus entero a quien creía estar filtrando.

    FALSADOR: con `if tipo:` esto da 200 y la aserción cae.
    """
    assert cliente.get("/entries?tipo=").status_code == 422
    assert cliente.get("/entries").status_code == 200          # SIN parámetro: sin filtro


def test_el_indice_es_EL_QUE_DISENAMOS_no_uno_cualquiera(tmp_path, monkeypatch):
    """Que exista `i_raw_tipo` no basta: tiene que ser el índice decidido.

    Las tres propiedades se eligieron por motivos concretos y un test que sólo
    mire el nombre las deja caer sin avisar:
        raw_tipo PRIMERO  → `/entries?raw_tipo=` se consulta SIN ledger
        NOCASE            → case-insensitive ASCII, que es lo que el filtro usa
        ledger DETRÁS     → sigue ayudando cuando sí se acota

    FALSADOR: invertir el orden o quitar la collation deja el índice existiendo y
    sirviendo mal — exactamente el fallo que no se ve.
    """
    from fastapi.testclient import TestClient
    from .conftest import construir, db_directa
    s_ = construir(tmp_path, monkeypatch)
    with TestClient(s_.app):
        s_.barrido()
    con = db_directa(s_)
    assert "i_raw_tipo" in [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='entries'")]
    cols = [r["name"] for r in con.execute("PRAGMA index_info(i_raw_tipo)")]
    assert cols == ["raw_tipo", "ledger"], f"orden equivocado: {cols}"
    x = {r["name"]: r["coll"] for r in con.execute("PRAGMA index_xinfo(i_raw_tipo)") if r["name"]}
    assert x.get("raw_tipo", "").upper() == "NOCASE", f"collation de raw_tipo: {x}"


def test_subir_RAW_TIPO_V_arrastra_el_tipo_sin_reindex(tmp_path, monkeypatch):
    """El contrato de ESA migración cambió, así que su versión sube.

    v2 materializaba `head → raw_tipo`. v3 materializa `head → raw_tipo` **y** su
    derivado, atómicamente. No es Canon v2 —la semántica `raw_tipo → tipo` no ha
    cambiado— y por eso `CANON_V` sigue en 1: son dos ejes distintos y el sello
    compuesto que se propuso volvería a mezclarlos.

    El bump obliga a que una base sellada en v2 pase UNA vez por el writer nuevo,
    aunque su canon ya esté aplicado y su ledger esté dormido.

    FALSADOR: sin subir la versión, `migrar_raw_tipo` hace `return` y la fila se
    queda con el tipo de un lexema que ya no existe.
    """
    from fastapi.testclient import TestClient
    from .conftest import construir, db_directa
    import os
    s_ = construir(tmp_path, monkeypatch)
    led = tmp_path / "DEMO-LEDGER.md"
    led.write_text("### [wiki-vault·64bis] 2026-07-19T09:15:17Z — sellada en v2\n\ncuerpo\n")
    with TestClient(s_.app):
        s_.barrido()

    con = db_directa(s_)
    e = con.execute("SELECT eid FROM entries WHERE head LIKE '%sellada en v2%'").fetchone()
    con.execute("UPDATE entries SET raw_tipo='RESP', tipo='RESP' WHERE eid=?", (e["eid"],))
    con.execute("INSERT OR REPLACE INTO meta VALUES ('raw_tipo_v', '2')")
    con.execute("INSERT OR REPLACE INTO meta VALUES ('canon_v', ?)", (s_.CANON_V,))
    con.commit()
    contenido, mtime = led.read_bytes(), led.stat().st_mtime

    s2 = construir(tmp_path, monkeypatch)          # arranque: _preparar_indice
    led.write_bytes(contenido)
    os.utime(led, (mtime, mtime))
    with TestClient(s2.app):
        pass                                        # sin barrido: sólo el arranque

    ver = db_directa(s2)
    f = ver.execute("SELECT tipo, raw_tipo FROM entries WHERE eid=?", (e["eid"],)).fetchone()
    assert f["raw_tipo"] is None, f"el lexema no se recalculó: {f['raw_tipo']!r}"
    assert f["tipo"] is None, f"`tipo`={f['tipo']!r} sobrevive a su lexema"
    m = dict(ver.execute("SELECT k,v FROM meta WHERE k IN ('raw_tipo_v','canon_v')").fetchall())
    assert m["raw_tipo_v"] == "3", m
    assert m["canon_v"] == s2.CANON_V == "1", f"CANON_V no debía moverse: {m}"
