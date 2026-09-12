"""Correctivas pre-wire: sello de esquema total y keyset inmutable por generación."""

import json
from pathlib import Path
import secrets
import sqlite3

import pytest

from .conftest import ACL, CARRIL, CLAVE, LANE, mete, nueva_con
import search_cursor as scur
import search_contract as sc
import search_store as ss


def _store_listo(tmp_path, *, n=7):
    con = nueva_con(tmp_path)
    st = ss.SearchStore(con, cursor_key=CLAVE, acl=ACL)
    st.ensure_schema()
    st.set_acl(ACL)
    for i in range(n):
        mete(con, LANE, f"e{i}", i + 1, cuerpo="born red comun")
    con.commit()
    st.rebuild()
    return st


def _estado_crudo(st):
    return [tuple(r) for r in st.con.execute(
        "SELECT k, typeof(v), hex(CAST(v AS BLOB)) FROM search_state ORDER BY k")]


def _bytes_durables(st):
    """Main + WAL: el SHM es coordinación efímera y cambia al tomar un lock."""
    db = Path(st.con.execute("PRAGMA database_list").fetchone()[2])
    return {p.name: p.read_bytes() for p in (db, Path(str(db) + "-wal")) if p.exists()}


def _reabre_con_search_state_nullable(st):
    """Corrompe sólo el DDL para poder materializar el storage NULL hostil."""
    db = Path(st.con.execute("PRAGMA database_list").fetchone()[2])
    st.con.execute("PRAGMA writable_schema=ON")
    st.con.execute(
        "UPDATE sqlite_master SET sql=replace(sql,'v TEXT NOT NULL','v TEXT')"
        " WHERE type='table' AND name='search_state'")
    st.con.execute("PRAGMA writable_schema=OFF")
    st.con.commit()
    st.con.close()
    con = sqlite3.connect(str(db))
    return ss.SearchStore(con, cursor_key=CLAVE, acl=ACL)


def _filas_fts(st, termino="born"):
    return [tuple(r) for r in st.con.execute(
        "SELECT d.ledger,d.eid FROM search_fts"
        " JOIN search_documents d ON d.rid=search_fts.rowid"
        " WHERE search_fts MATCH ? ORDER BY d.ledger,d.eid", (termino,))]


def _integridad_fts(st):
    st.con.execute(
        "INSERT INTO search_fts(search_fts, rank) VALUES('integrity-check', 1)")


def _convierte_en_v1(st):
    st.con.execute("DROP TRIGGER search_au_key")
    st.con.execute(ss.SQL_TRIGGER_AU_KEY_V1.strip().rstrip(";"))
    st._set("schema_v", str(ss.SEARCH_SCHEMA_V1))
    st._set("huellas", json.dumps(
        st._huellas_v1_legacy(st.ddl_esperado_v1()), sort_keys=True))
    st.con.commit()


def _trigger_aleatorio(st, target, cuerpo="SELECT 1"):
    nombre = "rnd_" + secrets.token_hex(6)
    st.con.execute(
        f"CREATE TRIGGER {nombre} AFTER DELETE ON {target} BEGIN {cuerpo}; END")
    return nombre


def _store_sellado_con_trigger_homonimo(tmp_path):
    """Construye el estado que el inventario por nombre bendecía sin usar rebuild."""
    con = nueva_con(tmp_path)
    con.execute("CREATE TABLE legacy_saved(gen TEXT)")
    con.execute("""
        CREATE TRIGGER search_documents AFTER UPDATE OF arrival ON entries BEGIN
          UPDATE search_state SET v='ready' WHERE k='state';
          UPDATE search_state
             SET v=(SELECT gen FROM legacy_saved LIMIT 1) WHERE k='generation';
        END
    """)
    st = ss.SearchStore(con, cursor_key=CLAVE, acl=ACL)
    st.ensure_schema()
    st.set_acl(ACL)
    for i in range(7):
        mete(con, LANE, f"e{i}", i + 1, cuerpo="born red comun")
    con.execute(
        "INSERT OR IGNORE INTO search_documents(ledger,eid)"
        " SELECT ledger,eid FROM entries ORDER BY ledger,eid")
    con.execute(
        "INSERT INTO search_fts(rowid,body) SELECT rowid,body FROM search_view")
    gen = "a" * scur.GENERATION_HEX
    for clave, valor in (
        ("schema_v", str(ss.SEARCH_SCHEMA_V)),
        ("state", ss.ESTADO_LISTO),
        ("generation", gen),
        ("normalizador", sc.normalizer_fingerprint()),
    ):
        st._set(clave, valor)
    st._set("huellas", json.dumps(st._huellas_objetos(), sort_keys=True))
    con.execute("INSERT INTO legacy_saved VALUES (?)", (gen,))
    con.commit()
    return st


@pytest.mark.parametrize("crudo", [
    "",                         # vacío
    "texto",                    # no numérico
    "１",                        # Unicode: int()/isdecimal() lo aceptarían
    "1.0",                      # decimal, no versión entera
    str(ss.MAX_SEARCH_SCHEMA_V + 1),
    "-1",                       # fuera del rango inferior
])
def test_schema_v_corrupto_cierra_readiness_y_no_se_autocura(tmp_path, crudo):
    st = _store_listo(tmp_path)
    estado, gen = st.state(), st.generation()
    st._set("schema_v", crudo)
    st.con.commit()

    # El sensor es total: corrupción es ROJO, nunca una ValueError/Unicode sorpresa.
    salud = st.readiness()
    assert salud["ready"] is False
    assert salud["schema_v"] is None
    assert any("schema_v corrupto" in p for p in salud["problems"]), salud

    # Los caminos operacionales conservan la clasificación interna precisa.
    with pytest.raises(ss.SearchSchemaCorrupt) as busqueda:
        st.search(lane=CARRIL, ledger=LANE, query="born red", limit=2)
    assert busqueda.value.code == "SEARCH_SCHEMA_CORRUPT"
    with pytest.raises(ss.SearchSchemaCorrupt):
        st.rebuild()

    # Rebuild no "cura" el sello ni mueve el estado/generación: la evidencia queda.
    assert st._get("schema_v") == crudo
    assert st.state() == estado
    assert st.generation() == gen


@pytest.mark.parametrize("forma", ["blob", "utf8-invalido", "5000-digitos"])
def test_schema_v_corrupto_durable_conserva_bytes_y_sale_tipado(tmp_path, forma):
    st = _store_listo(tmp_path)
    if forma == "blob":
        st.con.execute("UPDATE search_state SET v=? WHERE k='schema_v'",
                       (sqlite3.Binary(b"2"),))
    elif forma == "utf8-invalido":
        st.con.execute(
            "UPDATE search_state SET v=CAST(x'80' AS TEXT) WHERE k='schema_v'")
    else:
        st.con.execute("UPDATE search_state SET v=? WHERE k='schema_v'", ("9" * 5000,))
    st.con.commit()
    antes = _estado_crudo(st)
    estado, gen = st.state(), st.generation()

    salud = st.readiness()
    assert salud["ready"] is False
    assert any("schema_v corrupto" in p for p in salud["problems"])
    with pytest.raises(ss.SearchSchemaCorrupt) as busqueda:
        st.search(lane=CARRIL, ledger=LANE, query="born red", limit=2)
    assert busqueda.value.code == "SEARCH_SCHEMA_CORRUPT"
    with pytest.raises(ss.SearchSchemaCorrupt):
        st.rebuild()

    assert _estado_crudo(st) == antes
    assert st.state() == estado
    assert st.generation() == gen


@pytest.mark.parametrize(("clave", "valor"), [
    ("generation", sqlite3.Binary(b"0" * 32)),
    ("huellas", sqlite3.Binary(b"{}")),
    ("normalizador", sqlite3.Binary(b"huella")),
    ("state", sqlite3.Binary(b"ready")),
])
def test_readiness_es_total_ante_storage_no_textual_en_sellos_criticos(
        tmp_path, clave, valor):
    st = _store_listo(tmp_path)
    st.con.execute("UPDATE search_state SET v=? WHERE k=?", (valor, clave))
    st.con.commit()
    antes = _estado_crudo(st)
    salud = st.readiness()
    assert salud["ready"] is False
    assert any(f"{clave} corrupto" in p for p in salud["problems"]), salud
    with pytest.raises(ss.SearchNotReady):
        st.search(lane=CARRIL, ledger=LANE, query="born red", limit=2)
    assert _estado_crudo(st) == antes


@pytest.mark.parametrize("clave", [
    "state", "generation", "huellas", "normalizador", "schema_v",
])
def test_readiness_es_total_ante_NULL_en_cada_sello_y_no_escribe(tmp_path, clave):
    st = _reabre_con_search_state_nullable(_store_listo(tmp_path))
    st.con.execute("UPDATE search_state SET v=NULL WHERE k=?", (clave,))
    st.con.commit()
    antes = _bytes_durables(st)

    error = ss.SearchSchemaCorrupt if clave == "schema_v" else ss.SearchStateCorrupt
    lector = st.schema_v if clave == "schema_v" else lambda: st._get(clave)
    with pytest.raises(error, match="storage class 'null'"):
        lector()
    salud = st.readiness()
    assert salud["ready"] is False
    assert any(f"{clave} corrupto" in p for p in salud["problems"]), salud
    assert _bytes_durables(st) == antes


@pytest.mark.parametrize("clave", ["generation", "schema_v"])
def test_dos_filas_para_un_sello_son_corrupcion_tipificada(tmp_path, clave):
    st = _store_listo(tmp_path)
    dependientes = [r[0] for r in st.con.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'"
        " AND instr(sql,'search_state') > 0")]
    for nombre in dependientes:
        st.con.execute(f"DROP TRIGGER {nombre}")
    st.con.execute("CREATE TABLE search_state_reemplazo(k TEXT, v TEXT)")
    st.con.execute(
        "INSERT INTO search_state_reemplazo(k,v) SELECT k,v FROM search_state")
    st.con.execute("DROP TABLE search_state")
    st.con.execute("ALTER TABLE search_state_reemplazo RENAME TO search_state")
    st.con.execute(
        "INSERT INTO search_state(k,v) SELECT k,v FROM search_state WHERE k=? LIMIT 1",
        (clave,))
    st.con.commit()

    error = ss.SearchSchemaCorrupt if clave == "schema_v" else ss.SearchStateCorrupt
    lector = st.schema_v if clave == "schema_v" else st.generation
    with pytest.raises(error, match="más de una fila"):
        lector()
    salud = st.readiness()
    assert salud["ready"] is False
    assert any(f"{clave} corrupto" in p for p in salud["problems"]), salud


def test_solo_no_such_table_equivale_a_ausencia(tmp_path):
    con = sqlite3.connect(str(tmp_path / "sin-tabla.sqlite"))
    st = ss.SearchStore(con, cursor_key=CLAVE, acl=ACL)
    assert st.schema_v() is None
    con.execute("CREATE TABLE search_state (k TEXT PRIMARY KEY, otro TEXT)")
    with pytest.raises(ss.SearchSchemaCorrupt, match="no puedo leer"):
        st.schema_v()
    salud = st.readiness()
    assert salud["ready"] is False
    assert any("no puedo leer search_state" in p for p in salud["problems"]), salud


def _recorre(st, ledger):
    vistos, cursor = [], None
    while True:
        pagina = st.search(lane=CARRIL, ledger=ledger, query="born red", limit=2,
                           cursor=cursor)
        vistos.extend(f["eid"] for f in pagina["filas"])
        if not pagina["hay_mas"]:
            return vistos
        cursor = pagina["cursor"]


@pytest.mark.parametrize(("campo", "nuevo"), [
    ("eid", "e1-renombrado"),
    ("arrival", 99),
    ("ledger", "64bis-wiki-archivo"),
])
def test_mover_cualquier_componente_del_keyset_invalida_cursor_y_recorre_completo(
        tmp_path, campo, nuevo):
    st = _store_listo(tmp_path)
    vieja_gen = st.generation()
    primera = st.search(lane=CARRIL, ledger=LANE, query="born red", limit=2)
    assert primera["cursor"]

    st.con.execute(f"UPDATE entries SET {campo}=? WHERE ledger=? AND eid=?",
                   (nuevo, LANE, "e1"))
    st.con.commit()

    assert st.state() == ss.ESTADO_CONSTRUYENDO
    assert st.generation() != vieja_gen
    with pytest.raises(ss.SearchNotReady):
        st.search(lane=CARRIL, ledger=LANE, query="born red", limit=2,
                  cursor=primera["cursor"])

    st.rebuild()
    # Incluso ya reconstruido, el cursor anterior sigue caducado por generación.
    with pytest.raises(scur.CursorGenerationStale):
        st.search(lane=CARRIL, ledger=LANE, query="born red", limit=2,
                  cursor=primera["cursor"])

    # Recorre cada población afectada con cursor nuevo y exige conjunto exacto y único.
    ledgers = (LANE, "64bis-wiki-archivo") if campo == "ledger" else (LANE,)
    for ledger in ledgers:
        esperados = {
            r[0] for r in st.con.execute(
                "SELECT eid FROM entries WHERE ledger=? AND body LIKE '%born red%'",
                (ledger,)).fetchall()
        }
        vistos = _recorre(st, ledger)
        assert len(vistos) == len(set(vistos)), f"{campo} repitió: {vistos}"
        assert set(vistos) == esperados, (
            f"{campo} perdió o inventó filas: vistos={vistos}, esperados={esperados}")


@pytest.mark.parametrize("campo", ["eid", "arrival", "ledger"])
def test_update_noop_de_cada_clave_no_rota_generacion_ni_estado(tmp_path, campo):
    st = _store_listo(tmp_path)
    estado, gen = st.state(), st.generation()
    st.con.execute(f"UPDATE entries SET {campo}={campo} WHERE ledger=? AND eid=?",
                   (LANE, "e1"))
    st.con.commit()
    assert st.state() == estado == ss.ESTADO_LISTO
    assert st.generation() == gen
    assert st.readiness()["ready"] is True


@pytest.mark.parametrize(("campo", "nuevo", "clave_nueva"), [
    ("eid", "e1-renombrado", (LANE, "e1-renombrado")),
    ("ledger", "64bis-wiki-archivo", ("64bis-wiki-archivo", "e1")),
    ("arrival", 99, (LANE, "e1")),
])
def test_trigger_de_keyset_conserva_fts_exacto_antes_del_rebuild(
        tmp_path, campo, nuevo, clave_nueva):
    st = _store_listo(tmp_path)
    vieja = (LANE, "e1")
    st.con.execute(f"UPDATE entries SET {campo}=? WHERE ledger=? AND eid=?",
                   (nuevo, *vieja))
    st.con.commit()

    _integridad_fts(st)
    filas = _filas_fts(st)
    assert clave_nueva in filas
    if clave_nueva != vieja:
        assert vieja not in filas
    assert len(filas) == 7 and len(filas) == len(set(filas))


def test_noop_de_clave_y_update_de_body_reindexa_sin_invalidar(tmp_path):
    st = _store_listo(tmp_path)
    estado, gen = st.state(), st.generation()
    st.con.execute(
        "UPDATE entries SET ledger=ledger,eid=eid,arrival=arrival,body=?"
        " WHERE ledger=? AND eid=?",
        ("titular e1\naguja-nueva", LANE, "e1"))
    st.con.commit()

    assert st.state() == estado == ss.ESTADO_LISTO
    assert st.generation() == gen
    _integridad_fts(st)
    assert (LANE, "e1") not in _filas_fts(st, "born")
    assert _filas_fts(st, "aguja") == [(LANE, "e1")]


def test_migracion_v1_a_v2_es_explicita_exacta_y_no_reconstruye(tmp_path):
    st = _store_listo(tmp_path)
    cursor_v1 = st.search(lane=CARRIL, ledger=LANE, query="born red", limit=2)["cursor"]
    _convierte_en_v1(st)
    gen_v1 = st.generation()
    ddl_v1 = {r["name"]: r["sql"] for r in st.con.execute(
        "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL")}
    fts_v1 = _filas_fts(st)

    # Ni readiness ni rebuild convierten el esquema por su cuenta.
    assert st.readiness()["ready"] is False
    antes_rebuild = _estado_crudo(st)
    with pytest.raises(ss.SearchStale):
        st.rebuild()
    assert _estado_crudo(st) == antes_rebuild

    gen_v2 = st.migrate_schema_v1_to_v2()
    ddl_v2 = {r["name"]: r["sql"] for r in st.con.execute(
        "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL")}
    assert {n for n in ddl_v1 if ddl_v1[n] != ddl_v2[n]} == {"search_au_key"}
    assert gen_v2 != gen_v1
    assert st.schema_v() == ss.SEARCH_SCHEMA_V == 2
    assert st.state() == ss.ESTADO_LISTO
    assert json.loads(st._get("huellas")) == st._huellas_objetos()
    assert st.readiness()["ready"] is True
    assert _filas_fts(st) == fts_v1
    _integridad_fts(st)
    with pytest.raises(scur.CursorGenerationStale):
        st.search(lane=CARRIL, ledger=LANE, query="born red", limit=2,
                  cursor=cursor_v1)


@pytest.mark.parametrize("target", [
    "entries", "search_documents", "search_state", "search_acl",
])
def test_migracion_rechaza_trigger_extra_aleatorio_en_todo_target_gobernado(
        tmp_path, target):
    st = _store_listo(tmp_path)
    _convierte_en_v1(st)
    nombre = _trigger_aleatorio(st, target)
    st.con.commit()
    antes_estado = _estado_crudo(st)
    antes_ddl = [tuple(r) for r in st.con.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")]
    antes_bytes = _bytes_durables(st)

    with pytest.raises(ss.SearchMigrationRejected, match="juego de objetos"):
        st.migrate_schema_v1_to_v2()

    assert _estado_crudo(st) == antes_estado
    assert [tuple(r) for r in st.con.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")] == antes_ddl
    assert _bytes_durables(st) == antes_bytes
    assert nombre in {r[1] for r in antes_ddl}


def test_trigger_global_con_target_ajeno_y_cuerpo_search_tambien_se_rechaza(tmp_path):
    st = _store_listo(tmp_path)
    _convierte_en_v1(st)
    st.con.execute("CREATE TABLE shared_jobs(id INTEGER PRIMARY KEY)")
    nombre = "rnd_" + secrets.token_hex(6)
    st.con.execute(
        f"CREATE TRIGGER {nombre} AFTER INSERT ON shared_jobs BEGIN"
        " UPDATE search_state SET v='ready' WHERE k='state'; END")
    st.con.commit()
    antes = _bytes_durables(st)

    with pytest.raises(ss.SearchMigrationRejected, match="juego de objetos"):
        st.migrate_schema_v1_to_v2()
    assert _bytes_durables(st) == antes


@pytest.mark.parametrize("objeto", ["indice-owned", "tabla-reservada"])
def test_objetos_extra_que_ocupan_search_se_rechazan(tmp_path, objeto):
    st = _store_listo(tmp_path)
    _convierte_en_v1(st)
    nombre = "rnd_" + secrets.token_hex(6)
    if objeto == "indice-owned":
        st.con.execute(f"CREATE INDEX {nombre} ON search_state(v)")
    else:
        nombre = "search_intrusa_" + secrets.token_hex(6)
        st.con.execute(f"CREATE TABLE {nombre}(id INTEGER)")
    st.con.commit()

    with pytest.raises(ss.SearchMigrationRejected, match="juego de objetos"):
        st.migrate_schema_v1_to_v2()
    assert st.schema_v() == ss.SEARCH_SCHEMA_V1


def test_objetos_legacy_no_ejecutables_fuera_de_search_se_toleran_explicitos(tmp_path):
    st = _store_listo(tmp_path)
    _convierte_en_v1(st)
    st.con.executescript("""
        CREATE TABLE legacy_shared(id INTEGER PRIMARY KEY, value TEXT);
        CREATE INDEX legacy_shared_value ON legacy_shared(value);
        CREATE VIEW legacy_shared_view AS SELECT id,value FROM legacy_shared;
    """)
    st.migrate_schema_v1_to_v2()
    assert st.readiness()["ready"] is True


def test_trigger_TEMP_sobre_entries_no_escapa_del_inventario(tmp_path):
    st = _store_listo(tmp_path)
    _convierte_en_v1(st)
    nombre = "rnd_" + secrets.token_hex(6)
    # Simula código que rompió explícitamente el contrato sustituyendo el authorizer:
    # incluso entonces el inventario de la propia conexión conserva la segunda red.
    st.con.set_authorizer(None)
    st.con.execute(
        f"CREATE TEMP TRIGGER {nombre} AFTER DELETE ON main.entries"
        " BEGIN SELECT 1; END")

    with pytest.raises(ss.SearchMigrationRejected, match="juego de objetos"):
        st.migrate_schema_v1_to_v2()
    assert st.schema_v() == ss.SEARCH_SCHEMA_V1


def test_tabla_y_trigger_homonimos_no_colapsan_y_el_cursor_no_pierde_e4(tmp_path):
    st = _store_sellado_con_trigger_homonimo(tmp_path)
    identidades = [identidad for identidad in st._ddl_vivo()
                   if identidad[2] == "search_documents"]
    assert {(i[1], i[3]) for i in identidades} == {
        ("trigger", "entries"), ("table", "search_documents")}
    salud = st.readiness()
    assert salud["ready"] is False
    assert any("sobra search_documents (trigger sobre entries)" in p
               for p in salud["problems"]), salud

    gen = st.generation()
    filtros = sc.canonical_filters(
        lane=CARRIL, ledger=LANE, actor=None, tipo=None, desde=None, hasta=None,
        query="born red", order=sc.ORDER_ARRIVAL_DESC)
    cursor = scur.encode(arrival=6, eid="e5", generation=gen,
                         filter_sha256=sc.filter_sha256(filtros), key=CLAVE)
    st.con.execute(
        "UPDATE entries SET arrival=99 WHERE ledger=? AND eid='e4'", (LANE,))
    st.con.commit()
    # El trigger hostil reproduce el incidente, pero la identidad completa impide servir.
    assert st.state() == ss.ESTADO_LISTO and st.generation() == gen
    with pytest.raises(ss.SearchNotReady):
        st.search(lane=CARRIL, ledger=LANE, query="born red", limit=20, cursor=cursor)


def test_migracion_rechaza_trigger_homonimo_y_rollback_conserva_bytes(tmp_path):
    st = _store_sellado_con_trigger_homonimo(tmp_path)
    st.con.execute("DROP TRIGGER search_au_key")
    st.con.execute(ss.SQL_TRIGGER_AU_KEY_V1.strip().rstrip(";"))
    st._set("schema_v", str(ss.SEARCH_SCHEMA_V1))
    st._set("huellas", json.dumps(
        st._huellas_v1_legacy(st.ddl_esperado_v1()), sort_keys=True))
    st.con.commit()
    antes_estado = _estado_crudo(st)
    antes_ddl = [tuple(r) for r in st.con.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY rowid")]
    antes_bytes = _bytes_durables(st)

    with pytest.raises(ss.SearchMigrationRejected, match="juego de objetos"):
        st.migrate_schema_v1_to_v2()

    assert _estado_crudo(st) == antes_estado
    assert [tuple(r) for r in st.con.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY rowid")] == antes_ddl
    assert _bytes_durables(st) == antes_bytes
    assert st.schema_v() == ss.SEARCH_SCHEMA_V1


def test_sombras_FTS_exactas_se_ignoran_pero_trigger_homonimo_no(tmp_path):
    st = _store_listo(tmp_path)
    sombras = [tuple(r) for r in st.con.execute(
        "SELECT type,name,tbl_name FROM sqlite_master"
        " WHERE name LIKE 'search_fts_%' ORDER BY name")]
    assert sombras
    vivo = st._ddl_vivo()
    assert all(("main", *sombra) not in vivo for sombra in sombras)

    nombre = sombras[0][1]
    st.con.execute(
        f"CREATE TRIGGER {nombre} AFTER DELETE ON entries BEGIN SELECT 1; END")
    identidad_trigger = ("main", "trigger", nombre, "entries")
    assert identidad_trigger in st._ddl_vivo()
    salud = st.readiness()
    assert salud["ready"] is False
    assert any(nombre in p and "trigger" in p for p in salud["problems"]), salud


def test_guard_rechaza_TEMP_preexistente_y_no_colapsa_homonimos(tmp_path):
    con = nueva_con(tmp_path)
    con.execute("CREATE TEMP TABLE search_intrusa(id INTEGER)")
    con.execute(
        "CREATE TEMP TRIGGER search_intrusa AFTER INSERT ON main.entries"
        " BEGIN SELECT 1; END")
    antes = [tuple(r) for r in con.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_temp_master ORDER BY rowid")]

    with pytest.raises(ss.ConnectionContractViolation) as error:
        ss.guard_search_connection(con)

    assert "search_intrusa (table sobre search_intrusa)" in error.value.detail
    assert "search_intrusa (trigger sobre entries)" in error.value.detail
    assert [tuple(r) for r in con.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_temp_master ORDER BY rowid")] == antes


@pytest.mark.parametrize("nombre", ["ENTRIES", "SEARCH_STATE", "SEARCH_FTS", "SeArCh_AcL"])
def test_guard_sigue_semantica_ASCII_case_insensitive_de_SQLite(tmp_path, nombre):
    con = nueva_con(tmp_path)
    con.execute(f"CREATE TEMP TABLE {nombre}(k TEXT PRIMARY KEY, v TEXT)")
    with pytest.raises(ss.ConnectionContractViolation, match="DDL TEMP"):
        ss.guard_search_connection(con)


@pytest.mark.parametrize("nombre", [
    "ENTRY", "ENTRIES_ARCHIVE", "SEARCH", "ASEARCH_STATE", "ſEARCH_STATE",
])
def test_guard_no_amplia_el_namespace_a_nombres_no_relacionados(tmp_path, nombre):
    con = nueva_con(tmp_path)
    con.execute(f'CREATE TEMP TABLE "{nombre}"(id INTEGER)')
    ss.guard_search_connection(con)
    assert con.execute(
        "SELECT COUNT(*) FROM sqlite_temp_master WHERE name=?", (nombre,)).fetchone()[0] == 1


def test_TEMP_SEARCH_STATE_con_ocho_sellos_se_rechaza_antes_de_readiness(tmp_path):
    con = nueva_con(tmp_path)
    con.execute(
        "CREATE TEMP TABLE SEARCH_STATE(k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    sellos = {
        "state": ss.ESTADO_LISTO,
        "schema_v": str(ss.SEARCH_SCHEMA_V),
        "generation": "a" * scur.GENERATION_HEX,
        "huellas": "{}",
        "normalizador": sc.normalizer_fingerprint(),
        "rebuilt_at": "1",
        "documents": "7",
        "tombstones": "0",
    }
    con.executemany("INSERT INTO temp.SEARCH_STATE(k,v) VALUES(?,?)", sellos.items())
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM temp.SEARCH_STATE").fetchone()[0] == 8
    assert con.execute(
        "SELECT COUNT(*) FROM main.sqlite_master WHERE name='search_state'").fetchone()[0] == 0

    with pytest.raises(ss.ConnectionContractViolation, match="SEARCH_STATE"):
        ss.SearchStore(con, cursor_key=CLAVE, acl=ACL)

    # No existe un Store desde el que se pueda publicar el falso ready de TEMP.
    assert con.execute(
        "SELECT COUNT(*) FROM main.sqlite_master WHERE name='search_state'").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM temp.SEARCH_STATE").fetchone()[0] == 8


def test_TEMP_mixed_case_en_writer_B_no_acepta_cursor_viejo_ni_pierde_e4(tmp_path):
    st = _store_listo(tmp_path)
    primera = st.search(lane=CARRIL, ledger=LANE, query="born red", limit=2)
    gen = st.generation()
    db = Path(st.con.execute("PRAGMA database_list").fetchone()[2])
    writer = sqlite3.connect(str(db))
    writer.row_factory = sqlite3.Row
    ss.SearchStore.register_udf(writer)
    writer.execute(
        "CREATE TEMP TABLE SeArCh_StAtE(k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    writer.execute(
        "INSERT INTO temp.SeArCh_StAtE(k,v) SELECT k,v FROM main.search_state")
    writer.commit()
    assert writer.execute("SELECT COUNT(*) FROM temp.SeArCh_StAtE").fetchone()[0] == 8
    with pytest.raises(ss.ConnectionContractViolation, match="SeArCh_StAtE"):
        ss.SearchStore(writer, cursor_key=CLAVE, acl=ACL)

    # Aun ejecutando el writer hostil para reproducir la física, los triggers MAIN no
    # escriben los sellos TEMP sombreados: main rota y la copia efímera queda vieja.
    writer.execute(
        "UPDATE main.entries SET arrival=99 WHERE ledger=? AND eid='e4'", (LANE,))
    writer.commit()
    main = dict(writer.execute("SELECT k,v FROM main.search_state").fetchall())
    temp = dict(writer.execute("SELECT k,v FROM temp.SeArCh_StAtE").fetchall())
    assert main["state"] == ss.ESTADO_CONSTRUYENDO
    assert main["generation"] != gen
    assert temp["state"] == ss.ESTADO_LISTO and temp["generation"] == gen
    with pytest.raises(ss.ConnectionContractViolation):
        ss.SearchStore(writer, cursor_key=CLAVE, acl=ACL)
    writer.close()

    with pytest.raises(ss.SearchNotReady):
        st.search(lane=CARRIL, ledger=LANE, query="born red", limit=20,
                  cursor=primera["cursor"])
    st.rebuild()
    with pytest.raises(scur.CursorGenerationStale):
        st.search(lane=CARRIL, ledger=LANE, query="born red", limit=20,
                  cursor=primera["cursor"])
    vistos = _recorre(st, LANE)
    assert set(vistos) == {f"e{i}" for i in range(7)} and "e4" in vistos


@pytest.mark.parametrize("ddl", [
    "CREATE TEMP TABLE entries(id INTEGER)",
    "CREATE TEMP VIEW entries AS SELECT 1 AS id",
    "CREATE TEMP TRIGGER cualquiera AFTER INSERT ON main.entries BEGIN SELECT 1; END",
])
def test_SearchStore_aplica_guard_y_rechaza_TEMP_preexistente_relevante(tmp_path, ddl):
    con = nueva_con(tmp_path)
    con.execute(ddl)
    with pytest.raises(ss.ConnectionContractViolation, match="DDL TEMP"):
        ss.SearchStore(con, cursor_key=CLAVE, acl=ACL)


@pytest.mark.parametrize("sql", [
    "CREATE TEMP TABLE scratch(id INTEGER)",
    "CREATE TEMP VIEW scratch AS SELECT 1 AS id",
    "CREATE TEMP TRIGGER scratch AFTER INSERT ON main.entries BEGIN SELECT 1; END",
])
def test_guard_niega_nuevo_DDL_TEMP_en_writer_B(tmp_path, sql):
    con = nueva_con(tmp_path)
    ss.guard_search_connection(con)
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        con.execute(sql)


def test_guard_niega_DROP_TEMP_y_ATTACH(tmp_path):
    con = nueva_con(tmp_path)
    con.execute("CREATE TEMP TABLE scratch(id INTEGER)")
    ss.guard_search_connection(con)
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        con.execute("DROP TABLE scratch")
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        con.execute("ATTACH ':memory:' AS lateral")
    assert con.execute(
        "SELECT COUNT(*) FROM sqlite_temp_master WHERE name='scratch'").fetchone()[0] == 1


def test_writer_B_guardado_no_puede_preservar_generacion_y_cursor_caduca(tmp_path):
    st = _store_listo(tmp_path)
    primera = st.search(lane=CARRIL, ledger=LANE, query="born red", limit=2)
    gen = st.generation()
    db = Path(st.con.execute("PRAGMA database_list").fetchone()[2])
    writer = sqlite3.connect(str(db))
    ss.SearchStore.register_udf(writer)
    ss.guard_search_connection(writer)
    try:
        for nombre, clave, condicion in (
            ("back_state", "state", "new.k='state' AND new.v='building'"),
            ("back_gen", "generation", "new.k='generation'"),
        ):
            with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                writer.execute(
                    f"CREATE TEMP TRIGGER {nombre} AFTER UPDATE OF v"
                    f" ON main.search_state WHEN {condicion} BEGIN"
                    f" UPDATE search_state SET v=old.v WHERE k='{clave}'; END")
        writer.execute(
            "UPDATE entries SET arrival=99 WHERE ledger=? AND eid='e4'", (LANE,))
        writer.commit()
    finally:
        writer.close()

    assert st.state() == ss.ESTADO_CONSTRUYENDO
    assert st.generation() != gen
    with pytest.raises(ss.SearchNotReady):
        st.search(lane=CARRIL, ledger=LANE, query="born red", limit=20,
                  cursor=primera["cursor"])
    st.rebuild()
    with pytest.raises(scur.CursorGenerationStale):
        st.search(lane=CARRIL, ledger=LANE, query="born red", limit=20,
                  cursor=primera["cursor"])


def test_extras_despues_de_migrar_impiden_que_cursor_viejo_pierda_e4(tmp_path):
    st = _store_listo(tmp_path)
    primera = st.search(lane=CARRIL, ledger=LANE, query="born red", limit=2)
    gen = st.generation()
    st.con.executescript("""
        CREATE TRIGGER rnd_backdoor_state AFTER UPDATE OF v ON search_state
        WHEN new.k='state' AND new.v='building' BEGIN
          UPDATE search_state SET v=old.v WHERE k='state';
        END;
        CREATE TRIGGER rnd_backdoor_generation AFTER UPDATE OF v ON search_state
        WHEN new.k='generation' BEGIN
          UPDATE search_state SET v=old.v WHERE k='generation';
        END;
    """)
    st.con.execute(
        "UPDATE entries SET arrival=99 WHERE ledger=? AND eid='e4'", (LANE,))
    st.con.commit()

    # Los backdoors reproducen el estado hostil original; el inventario es la frontera
    # que ahora impide servir sobre él.
    assert st.state() == ss.ESTADO_LISTO
    assert st.generation() == gen
    salud = st.readiness()
    assert salud["ready"] is False
    assert any("sobra rnd_backdoor" in p for p in salud["problems"]), salud
    with pytest.raises(ss.SearchNotReady):
        st.search(lane=CARRIL, ledger=LANE, query="born red", limit=20,
                  cursor=primera["cursor"])


@pytest.mark.parametrize("averia", ["version", "huellas", "ddl"])
def test_migracion_rechaza_origen_no_exacto_sin_mutar(tmp_path, averia):
    st = _store_listo(tmp_path)
    _convierte_en_v1(st)
    if averia == "version":
        st._set("schema_v", "0")
    elif averia == "huellas":
        st._set("huellas", "{}")
    else:
        st.con.execute("DROP TRIGGER search_au_key")
        st.con.execute(ss.SQL_TRIGGER_AU_KEY_V1.replace(
            "WHEN new.ledger <> old.ledger OR new.eid <> old.eid BEGIN",
            "WHEN 0 BEGIN").strip().rstrip(";"))
        # Mantiene el sello v1 CORRECTO: así este caso sólo puede rechazarlo comparar el
        # DDL vivo, no la guarda de huellas que ya tiene su propio caso causal.
        esperadas = st._huellas_v1_legacy(st.ddl_esperado_v1())
        st._set("huellas", json.dumps(esperadas, sort_keys=True))
    st.con.commit()
    antes_estado = _estado_crudo(st)
    antes_ddl = [tuple(r) for r in st.con.execute(
        "SELECT name,sql FROM sqlite_master ORDER BY name")]

    with pytest.raises(ss.SearchMigrationRejected):
        st.migrate_schema_v1_to_v2()
    assert _estado_crudo(st) == antes_estado
    assert [tuple(r) for r in st.con.execute(
        "SELECT name,sql FROM sqlite_master ORDER BY name")] == antes_ddl


@pytest.mark.parametrize("forma", ["blob", "utf8-invalido", "5000-digitos"])
def test_migracion_rechaza_schema_v_corrupto_y_conserva_sus_bytes(tmp_path, forma):
    st = _store_listo(tmp_path)
    _convierte_en_v1(st)
    if forma == "blob":
        st.con.execute("UPDATE search_state SET v=? WHERE k='schema_v'",
                       (sqlite3.Binary(b"1"),))
    elif forma == "utf8-invalido":
        st.con.execute(
            "UPDATE search_state SET v=CAST(x'80' AS TEXT) WHERE k='schema_v'")
    else:
        st.con.execute("UPDATE search_state SET v=? WHERE k='schema_v'", ("9" * 5000,))
    st.con.commit()
    antes = _estado_crudo(st)

    with pytest.raises(ss.SearchSchemaCorrupt):
        st.migrate_schema_v1_to_v2()
    assert _estado_crudo(st) == antes
