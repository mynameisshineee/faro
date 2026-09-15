"""Migración `durable_v` 1 → 2, contra fixtures v1 CONSTRUIDOS A MANO.

Por qué a mano y no desde git: un fixture que salga de `git checkout` mide que
el repo tiene ese commit, no que la migración funciona — y en un worktree o un
export sin `.git` el test se cae por un motivo que no es el suyo. Aquí el
esquema v1 está escrito literalmente, así que la prueba es reproducible en un
tarball pelado.

Sólo se recrean en forma v1 las CUATRO tablas que la migración toca
(`commands`, `event_acks`, `outbox_operations`, `denial_aggregates`). El resto
no cambió entre v1 y v2, y la migración las salta por introspección: fabricar
una copia v1 de algo idéntico mediría mi capacidad de transcribir, no la suya de
migrar.

Dos fixtures, porque «v1» fueron DOS esquemas distintos bajo el mismo sello —que
es justo la avería que trajo este correctivo:
  · `A` (859cef9): `commands` SIN `role` ni `runtime_instance`.
  · `B` (c51cd1e): `commands` CON los dos, SIN `attribution_status`.
"""
from __future__ import annotations

import sqlite3

import pytest

import coordination as C
from ._arnes import GRAMATICA, LANES, PEPPER, abre_admision, censo

# ── DDL v1 literal de lo que la migración toca ───────────────────────────────
COMMANDS_A = """
CREATE TABLE commands (
  command_id   TEXT PRIMARY KEY,
  workstream_id TEXT NOT NULL,
  revision     INTEGER NOT NULL,
  lane         TEXT NOT NULL,
  principal_id TEXT NOT NULL REFERENCES principals(principal_id),
  payload      TEXT NOT NULL,
  state        TEXT NOT NULL
      CHECK (state IN ('accepted','received','executing','succeeded',
                       'failed','cancelled','superseded')),
  supersedes   TEXT REFERENCES commands(command_id),
  created_at   TEXT NOT NULL,
  UNIQUE (lane, workstream_id, revision))"""

COMMANDS_B = """
CREATE TABLE commands (
  command_id   TEXT PRIMARY KEY,
  workstream_id TEXT NOT NULL,
  revision     INTEGER NOT NULL,
  lane         TEXT NOT NULL,
  principal_id TEXT NOT NULL REFERENCES principals(principal_id),
  role         TEXT NOT NULL,
  runtime_instance TEXT NOT NULL REFERENCES runtime_sessions(runtime_instance),
  payload      TEXT NOT NULL,
  state        TEXT NOT NULL
      CHECK (state IN ('accepted','received','executing','succeeded',
                       'failed','cancelled','superseded')),
  supersedes   TEXT REFERENCES commands(command_id),
  created_at   TEXT NOT NULL,
  UNIQUE (lane, workstream_id, revision))"""

# v1: `runtime_instance` SIN FK — es la que la migración viene a ganar.
ACKS_V1 = """
CREATE TABLE event_acks (
  event_id   TEXT NOT NULL REFERENCES events(event_id),
  recipient  TEXT NOT NULL,
  principal_id TEXT NOT NULL REFERENCES principals(principal_id),
  runtime_instance TEXT NOT NULL,
  ack_ref    TEXT,
  at         TEXT NOT NULL,
  PRIMARY KEY (event_id, recipient))"""

OPS_V1 = """
CREATE TABLE outbox_operations (
  operation_id TEXT PRIMARY KEY,
  event_id   TEXT NOT NULL REFERENCES events(event_id),
  operation  TEXT NOT NULL CHECK (operation IN ('requeue','abandon')),
  operator   TEXT NOT NULL,
  lane       TEXT NOT NULL,
  runtime_instance TEXT NOT NULL,
  reason     TEXT NOT NULL,
  from_state TEXT NOT NULL,
  at         TEXT NOT NULL)"""

# v1: `runtime_instance` DENTRO de la clave — dos runtimes = dos filas.
AGGS_V1 = """
CREATE TABLE denial_aggregates (
  principal_id TEXT NOT NULL REFERENCES principals(principal_id),
  lane         TEXT NOT NULL DEFAULT '',
  runtime_instance TEXT NOT NULL DEFAULT '',
  reason       TEXT NOT NULL,
  bucket       TEXT NOT NULL,
  receipt_id   TEXT NOT NULL REFERENCES receipts(receipt_id),
  suppressed   INTEGER NOT NULL DEFAULT 0,
  first_at     TEXT NOT NULL,
  last_at      TEXT NOT NULL,
  PRIMARY KEY (principal_id, lane, runtime_instance, reason, bucket))"""


def _fixture_v1(tmp_path, variante: str, *, ack_huerfano=False, op_huerfana=False):
    """Base sellada como `durable_v=1` con datos en todo lo que la migración toca."""
    ruta = str(tmp_path / "coordination.sqlite")
    j = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
    j.initialize()                      # esquema actual…
    # La barrera nace cerrada; este fixture ESCRIBE, así que la abre como un
    # operador. Se cierra sola más abajo al borrar la tabla: una v1 no la tenía.
    abre_admision(j, lanes=["llminbox"])
    con = j._connect()
    con.execute("PRAGMA foreign_keys=OFF")

    # …y las cuatro tablas se rehacen en forma v1.
    for tabla, ddl in (("commands", COMMANDS_A if variante == "A" else COMMANDS_B),
                       ("event_acks", ACKS_V1),
                       ("outbox_operations", OPS_V1),
                       ("denial_aggregates", AGGS_V1)):
        con.execute(f"DROP TABLE {tabla}")
        con.executescript(ddl)
    # Una v1 REAL trae este índice; el fixture lo recrea para que su ausencia
    # después signifique «la migración lo perdió» y no «nunca estuvo».
    con.execute("CREATE INDEX IF NOT EXISTS i_cmd_ws"
                " ON commands(lane, workstream_id, revision)")

    # Sujetos: un principal, DOS runtimes (para que el agregado tenga dos filas).
    b = j.bind_credential("cred-A", principal="backend", role="be", lane="llminbox")
    s1 = j.open_session("cred-A")
    s2 = j.open_session("cred-A")
    ev = j.accept_event(s1.token, idempotency_key="k1",
                        intent={"type": "message", "verb": "inform", "kind": "FYI",
                                "to": ["be"], "head": "h", "body": "cuerpo"},
                        ledger="llminbox")

    con.execute("PRAGMA foreign_keys=OFF")
    campos = ("command_id,workstream_id,revision,lane,principal_id,payload,state,"
              "created_at") if variante == "A" else (
              "command_id,workstream_id,revision,lane,principal_id,role,"
              "runtime_instance,payload,state,created_at")
    for i in (1, 2):
        base = ["cmd_%d" % i, "ws", i, "llminbox", b.principal_id]
        if variante == "B":
            base += ["be", s1.runtime_instance]
        base += ['{"n": %d}' % i, "accepted", "2026-09-05T00:00:00Z"]
        con.execute(f"INSERT INTO commands({campos}) VALUES("
                    + ",".join("?" * len(base)) + ")", base)
    # LAS TRES CAUSALIDADES pobladas: son las que el RENAME puede desviar.
    con.execute("INSERT INTO command_causes(command_id,cause_command_id)"
                " VALUES('cmd_2','cmd_1')")
    con.execute("INSERT INTO command_event_causes(command_id,cause_event_id)"
                " VALUES('cmd_2',?)", (ev.event_id,))
    con.execute("INSERT INTO external_causes(child_kind,child_id,ledger,entry_eid,"
                "noted_at) VALUES('command','cmd_2','llminbox',?,?)",
                ("b" * 64, "2026-09-05T00:00:00Z"))

    con.execute("INSERT INTO event_acks(event_id,recipient,principal_id,"
                "runtime_instance,ack_ref,at) VALUES(?,?,?,?,?,?)",
                (ev.event_id, "be", b.principal_id,
                 "rti_inventado" if ack_huerfano else s1.runtime_instance,
                 "ack-1", "2026-09-05T00:00:00Z"))
    con.execute("INSERT INTO outbox_operations(operation_id,event_id,operation,"
                "operator,lane,runtime_instance,reason,from_state,at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                ("opx_1", ev.event_id, "requeue", b.principal_id, "llminbox",
                 "rti_inventado" if op_huerfana else s2.runtime_instance,
                 "porque sí", "failed", "2026-09-05T00:00:00Z"))

    # DOS agregados que sólo se distinguen por el runtime: en v2 colapsan.
    rc1 = con.execute("SELECT receipt_id FROM receipts LIMIT 1").fetchone()["receipt_id"]
    rc2 = "rcp_extra"
    con.execute("INSERT INTO receipts(receipt_id,subject_kind,subject_id,"
                "principal_id,lane,current_state,created_at,updated_at)"
                " VALUES(?,'denial_aggregate','x',?,?,'denied_aggregate',?,?)",
                (rc2, b.principal_id, "llminbox", "2026-09-05T00:00:00Z",
                 "2026-09-05T00:00:00Z"))
    for rti, rid, sup in ((s1.runtime_instance, rc1, 7), (s2.runtime_instance, rc2, 5)):
        con.execute("INSERT INTO denial_aggregates(principal_id,lane,"
                    "runtime_instance,reason,bucket,receipt_id,suppressed,"
                    "first_at,last_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (b.principal_id, "llminbox", rti, "FENCING_CONFLICT", "1",
                     rid, sup, "2026-09-05T00:00:00Z", "2026-09-05T00:01:00Z"))

    con.execute("INSERT INTO unknown_credentials(fingerprint,count,first_at,last_at)"
                " VALUES('estable_1',13,?,?)",
                ("2026-09-05T00:00:00Z", "2026-09-05T00:00:00Z"))
    # v1 NO tenía `admission_history`. Dejarla puesta bajo el sello `1` haría
    # que la migración a v6 encontrara la tabla ya poblada y su `INSERT OR
    # IGNORE` no escribiera nada: el fixture estaría tapando justo el paso que
    # los tests de migración existen para ejercitar.
    con.execute("DROP TABLE admission_history")
    # v1 HONESTA (cura (b) de @db-mig, 2026-09-11): una v1 real tampoco tenia las tablas
    # v7-only ni sus indices. Sin esto la base es HIBRIDA —hijas v7 con FK compuesto
    # (lane, …) -> commands(lane, command_id) sobre un `commands` v1 sin `u_command_lane`— y
    # `PRAGMA foreign_key_check` revienta por un estado que ninguna instalacion tuvo.
    # Mismas listas que `_fixture_historica`: una sola fuente de verdad para «que es v7-only».
    from .test_schema_v7_fleet_control import V7_ONLY, V7_INDEXES
    for _t in V7_ONLY:
        con.execute(f"DROP TABLE IF EXISTS {_t}")
    for _i in V7_INDEXES:
        con.execute(f"DROP INDEX IF EXISTS {_i}")
    con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('durable_v','1')")
    con.execute("PRAGMA foreign_keys=ON")
    j.close()
    return ruta, b, s1, s2, ev, (rc1, rc2)


def _crudo(ruta):
    """Conexión CRUDA para inspeccionar tras un fallo de `initialize`.

    El `Journal` no está listo —y no debe estarlo— después de rechazar una base,
    así que pedirle una conexión operacional es pedirle justo lo que acaba de
    negarse a dar. Para mirar el fichero se mira el fichero.
    """
    con = sqlite3.connect(ruta)
    con.row_factory = sqlite3.Row
    return con


def _abre(ruta):
    return C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)


# ── ÉXITO ────────────────────────────────────────────────────────────────────

def _exige_rechazo(ruta, k, *, foto_antes=None, filas=()):
    """Forma de la RETIRADA, generalizada a `k` (README-falsadores-d83ae04.md; adjudicado por
    @db-mig 2026-09-11T23:35Z sobre la causa unica que medi: los 8 rojos de este fichero son
    UN sujeto retirado contado 8 veces, no 8 fallos).

    PRE-ASERTO de reconocimiento —el `k` viaja AQUI, no en la regex del mensaje—, rechazo
    TIPADO con el motivo de contrato, `0` objetos de v7 escritos, sello intacto y los datos
    que el test cuida, intactos. Sin el pre-aserto, un sustrato roto daria el mismo rojo que
    la retirada y el test pasaria por la razon equivocada."""
    con = _crudo(ruta)
    assert C._clasificar(con) == ("conocida", k, ""), f"el sustrato dejo de ser una v{k} reconocible"
    # los objetos que el manifiesto de v7 declara y el de `k` no, PERO que el sustrato YA trae:
    # contarlos como «escritos por el rechazo» seria culpar al rechazo del fixture (me paso).
    solo_v7 = set(C.MANIFIESTOS[C.DURABLE_V]["objetos"]) - set(C.MANIFIESTOS[k]["objetos"])
    ya_estaban = {t for t in solo_v7 if _existe(con, t)}
    con.close()
    with pytest.raises(C.MigrationFailed) as e:
        _abre(ruta).initialize()
    m = str(e.value)
    assert "migrador offline" in m and f"durable_v={k}" in m, f"no es el rechazo de la retirada: {m}"
    con = _crudo(ruta)
    escritos = sorted(t for t in solo_v7 if _existe(con, t) and t not in ya_estaban)
    assert escritos == [], f"el rechazo escribio bytes de v{C.DURABLE_V}: {escritos}"
    assert int(con.execute("SELECT v FROM meta WHERE k='durable_v'").fetchone()[0]) == k, "movio el sello"
    if foto_antes is not None:
        assert _foto(con) == foto_antes, "el rechazo toco los datos"
    for sql, esperado, que in filas:
        assert con.execute(sql).fetchone()[0] == esperado, f"el rechazo se llevo {que}"
    con.close()


@pytest.mark.parametrize("variante", ["A", "B"])
def test_una_v1_ya_no_migra_y_NO_PIERDE_NADA(tmp_path, variante):
    """🔻 antes: `test_la_migracion_preserva_todo_y_deja_la_base_integra`, que aseveraba la
    migracion 1→actual. El contrato beta la RETIRO («solo admite creacion nueva, v6→v7 o v7»)
    y @db-mig adjudico (2026-09-11) asertar la RETIRADA sobre los 8 de este fichero.

    HERMANO PENDIENTE (para cuando aterrice el migrador offline): la preservacion que este
    test media —filas y payloads intactos, rol derivado, atribucion segun variante, las TRES
    causalidades apuntando a `commands`— se escribe ENTONCES con este mismo fixture."""
    ruta, b, s1, s2, ev, (rc1, rc2) = _fixture_v1(tmp_path, variante)
    foto = _foto(_crudo(ruta))
    _exige_rechazo(ruta, 1, foto_antes=foto,
                   filas=(("SELECT COUNT(*) FROM commands", 2, "los commands"),))


@pytest.mark.parametrize("variante", ["A", "B"])
def test_una_v1_rechazada_NO_ADMITE_ESCRITURA_nueva(tmp_path, variante):
    """🔻 antes: `test_tras_migrar_una_escritura_nueva_nace_verified`. Sin migracion no hay
    «tras migrar»: lo que se exige hoy es que el rechazo sea TOTAL — ni escribe v7 ni deja
    la base a medias para que alguien escriba encima.

    HERMANO PENDIENTE: que una escritura nueva nazca `verified` con su `runtime_instance`,
    cuando exista el migrador."""
    ruta, b, s1, s2, ev, _ = _fixture_v1(tmp_path, variante)
    _exige_rechazo(ruta, 1)
    # y el falsador de «a medias»: la base queda SIN INICIALIZAR y lo dice con su propio tipo
    # (`JournalNotInitialized`, no el de la migracion) — medido: el rechazo no la deja a medio
    # camino ni disfraza el estado. Mi primera version esperaba aqui `MigrationFailed` y el
    # producto tiene razon: son dos estados distintos y los distingue.
    with pytest.raises(C.JournalNotInitialized):
        _abre(ruta).open_session("cred-A")


def test_una_base_NUEVA_nace_en_la_version_actual(tmp_path):
    j = C.Journal(str(tmp_path / "n.sqlite"), pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
    assert j.initialize() == C.DURABLE_V
    assert j.stored_durable_v() == C.DURABLE_V
    assert "attribution_status" in {c[1] for c in
                                    j._connect().execute("PRAGMA table_info(commands)")}
    j.close()


# ── EL PAR atribución/runtime LO IMPONE EL ESQUEMA ──────────────────────────

@pytest.mark.parametrize("estado,rti", [("verified", None),
                                        ("legacy_unattributed", "REAL")])
def test_las_combinaciones_mentirosas_las_rechaza_la_BASE(tmp_path, estado, rti):
    """Por EFECTO, no por lectura del DDL: se intenta el INSERT y tiene que
    reventar. Un enum solo no impide `verified` sobre un hueco."""
    j = C.Journal(str(tmp_path / "n.sqlite"), pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
    j.initialize()
    b = j.bind_credential("c", principal="p", role="be", lane="llminbox")
    s = j.open_session("c")
    con = j._connect()
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO commands(command_id,workstream_id,revision,lane,"
                    "principal_id,role,runtime_instance,attribution_status,payload,"
                    "state,created_at) VALUES('x','w',1,'llminbox',?,?,?,?,'{}',"
                    "'accepted','t')",
                    (b.principal_id, "be",
                     s.runtime_instance if rti == "REAL" else None, estado))
    j.close()


# ── FALLO: HUÉRFANOS ⇒ ROLLBACK COMPLETO ────────────────────────────────────

@pytest.mark.parametrize("donde", ["ack", "op"])
def test_un_huerfano_deshace_la_migracion_ENTERA(tmp_path, donde):
    ruta, b, s1, s2, ev, _ = _fixture_v1(
        tmp_path, "B", ack_huerfano=(donde == "ack"), op_huerfana=(donde == "op"))
    j = _abre(ruta)
    crudo = sqlite3.connect(ruta)
    crudo.row_factory = sqlite3.Row
    antes = _foto(crudo)          # sin pasar por el Journal: aún no clasificó
    crudo.close()
    with pytest.raises(C.MigrationFailed):
        j.initialize()
    con = _crudo(ruta)
    assert j.stored_durable_v() == 1, "selló una migración que no pudo completar"
    # Datos, tablas e índices intactos: el rollback no deja medio esquema.
    assert _foto(con) == antes
    assert "attribution_status" not in {c[1] for c in
                                        con.execute("PRAGMA table_info(commands)")}
    assert "runtime_instance" in {c[1] for c in
                                  con.execute("PRAGMA table_info(denial_aggregates)")}
    assert "i_cmd_ws" in {r[1] for r in con.execute("PRAGMA index_list(commands)")}
    assert not _existe(con, "commands_v1") and not _existe(con, "event_acks_v1")
    # (La comprobación de que las FK vuelven a estar encendidas se hace en el
    #  camino de ÉXITO: tras un fallo el journal NO queda listo, así que no hay
    #  conexión operacional suya que interrogar — y una conexión cruda nace con
    #  las FK apagadas, así que preguntárselo a ELLA no medía nada.)
    j.close()


def test_una_base_de_version_SUPERIOR_se_rechaza_sin_migrar_ni_mutar(tmp_path):
    ruta, *_ = _fixture_v1(tmp_path, "B")
    crudo = sqlite3.connect(ruta)
    crudo.execute("UPDATE meta SET v=?", (str(C.DURABLE_V + 1),))
    crudo.commit()
    crudo.row_factory = sqlite3.Row
    antes = _foto(crudo)
    crudo.close()
    j = _abre(ruta)
    with pytest.raises(C.SchemaTooNew):
        j.initialize()
    # Se inspecciona con conexión CRUDA: el journal rechazó la base, así que no
    # hay conexión operacional suya — y no debe haberla. `stored_durable_v()` sí
    # contesta, leyendo de la copia clasificada.
    con = _crudo(ruta)
    assert j.stored_durable_v() == C.DURABLE_V + 1 and _foto(con) == antes
    assert "attribution_status" not in {c[1] for c in
                                        con.execute("PRAGMA table_info(commands)")}
    con.close()
    j.close()


def _existe(con, tabla):
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                       (tabla,)).fetchone() is not None


def _foto(con) -> dict:
    out = {}
    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"
                            " ORDER BY name"):
        out[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    return out


# ── v2 → v3: la forma cambió, así que la versión sube ───────────────────────
BINDINGS_V2 = """
CREATE TABLE credential_bindings (
  binding_id      TEXT PRIMARY KEY,
  credential_ref  TEXT NOT NULL,
  principal_id    TEXT NOT NULL REFERENCES principals(principal_id),
  principal_source TEXT NOT NULL
      CHECK (principal_source IN ('explicit','derived_from_role')),
  generation      INTEGER NOT NULL,
  bound_at        TEXT NOT NULL,
  retired_at      TEXT)"""

# El índice va APARTE y se crea tras soltar la tabla temporal: el espacio de
# nombres de índices es de la BASE, así que mientras `credential_bindings_tmp`
# exista, `u_binding_activa` sigue ocupado. Es la misma trampa que la migración
# tuvo que cerrar, y aquí me la volví a comer al escribir el fixture.
IDX_BINDINGS = ("CREATE UNIQUE INDEX u_binding_activa"
                " ON credential_bindings(credential_ref) WHERE retired_at IS NULL")

IDEMPOTENCY_V2 = """
CREATE TABLE idempotency (
  principal_id TEXT NOT NULL REFERENCES principals(principal_id),
  lane         TEXT NOT NULL,
  verb         TEXT NOT NULL,
  key          TEXT NOT NULL,
  req_hash     TEXT NOT NULL,
  event_id     TEXT NOT NULL REFERENCES events(event_id),
  receipt_id   TEXT NOT NULL REFERENCES receipts(receipt_id),
  created_at   TEXT NOT NULL,
  PRIMARY KEY (principal_id, lane, verb, key))"""


def _fixture_v2(tmp_path):
    """Base con la forma EXACTA que dejaba `bcd05c5` (v2): sin `capabilities` ni
    `req_hash_v`. Se construye a mano por lo mismo que la v1 — un `git checkout`
    mediría que el repo tiene ese commit, no que la migración funciona."""
    ruta = str(tmp_path / "coordination.sqlite")
    j = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
    j.initialize()
    abre_admision(j, lanes=["llminbox"])
    b = j.bind_credential("cred-A", principal="backend", role="be", lane="llminbox")
    s = j.open_session("cred-A")
    ev = j.accept_event(s.token, idempotency_key="k1",
                        intent={"type": "message", "verb": "inform", "kind": "FYI",
                                "to": ["be"], "head": "h", "body": "cuerpo"},
                        ledger="llminbox")
    con = j._connect()
    con.execute("PRAGMA foreign_keys=OFF")
    # Se reconstruyen en forma v2 CONSERVANDO las filas.
    for tabla, ddl in (("credential_bindings", BINDINGS_V2),
                       ("idempotency", IDEMPOTENCY_V2)):
        cols = [c[1] for c in con.execute(f"PRAGMA table_info({tabla})")]
        nuevas = {"capabilities", "req_hash_v"}
        comunes = ",".join(c for c in cols if c not in nuevas)
        con.execute(f"ALTER TABLE {tabla} RENAME TO {tabla}_tmp")
        con.executescript(ddl)
        con.execute(f"INSERT INTO {tabla}({comunes}) SELECT {comunes} FROM {tabla}_tmp")
        con.execute(f"DROP TABLE {tabla}_tmp")
        if tabla == "credential_bindings":
            con.execute(IDX_BINDINGS)
    con.execute("DROP TABLE admission_history")   # v2 tampoco la tenía
    con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('durable_v','2')")
    con.execute("PRAGMA foreign_keys=ON")
    j.close()
    return ruta, b, ev


def test_una_v2_ya_no_migra_y_CONSERVA_SUS_DATOS(tmp_path):
    """🔻 antes: `test_de_v2_a_v3_se_migra_conservando_los_datos`. Mismo sujeto retirado que
    la v1, con OTRO sustrato: el rechazo no depende de la version de origen ni del contenido.

    HERMANO PENDIENTE: que la migracion 2→actual conserve la foto y escriba SOLO los pares de
    la barrera (dos verbos del carril con historia)."""
    ruta, b, ev = _fixture_v2(tmp_path)
    foto = _foto(_crudo(ruta))
    _exige_rechazo(ruta, 2, foto_antes=foto,
                   filas=(("SELECT COUNT(*) FROM events", 1, "el evento de la v2"),))


def test_la_ruta_de_DOS_escalones_se_para_en_el_PRIMERO(tmp_path):
    """🔻 antes: `test_la_ruta_completa_1_a_2_a_3_llega_entera`. Quien lleve tiempo sin
    actualizar YA NO sube dos escalones de una vez: se para en el primero, con el motivo
    escrito y sin tocar nada. Ese es hoy el contrato, y es lo que este test vigila.

    HERMANO PENDIENTE: la ruta entera 1→2→3 con sus columnas nuevas y `foreign_key_check`
    limpio, cuando el migrador offline exista."""
    ruta, b, s1, s2, ev, _ = _fixture_v1(tmp_path, "A")
    _exige_rechazo(ruta, 1, filas=(("SELECT COUNT(*) FROM commands", 2, "los commands"),))
    # ⊖ del «se para en el primero»: NINGUNA columna del escalon 1→2 aparece
    con = _crudo(ruta)
    cols = {c[1] for c in con.execute("PRAGMA table_info(commands)")}
    assert "attribution_status" not in cols, "escribio el escalon 1→2 antes de rechazar"
    con.close()


def test_la_fila_HISTORICA_del_hash_sobrevive_al_rechazo(tmp_path):
    """🔻 antes: `test_un_evento_v2_escrito_con_ledger_None_hace_replay_tras_migrar`. Su
    sujeto —el replay de una fila con la formula HISTORICA del hash— vive DESPUES de migrar,
    y migrar es lo que se retiro. Lo que si se puede exigir hoy, y es lo que este fichero
    cuida: que el rechazo NO se lleve por delante justo esa fila.

    HERMANO PENDIENTE: el replay con `req_hash_v=1` y el destino CRUDO (`None`), que es el
    caso que solo se ve reconstruyendo la formula vieja."""
    ruta, b, ev = _fixture_v2(tmp_path)
    con = _crudo(ruta)
    antes = con.execute("SELECT COUNT(*) FROM idempotency").fetchone()[0]
    con.close()
    assert antes >= 1, "el sustrato de este test necesita la fila historica"
    # ⚠️ en la v2 la columna `req_hash_v` NO existe: la añade el escalon 2→3. Por eso la fila
    # se identifica por su CLAVE, no por una columna que solo nace al migrar (mi primer intento).
    _exige_rechazo(ruta, 2,
                   filas=(("SELECT COUNT(*) FROM idempotency", antes,
                           "la fila con la formula historica"),))


def test_una_v2_con_hash_DESCONOCIDO_tampoco_se_reinterpreta(tmp_path):
    """🔻 antes: `test_una_version_de_hash_DESCONOCIDA_no_se_reinterpreta`. Tambien vivia
    DESPUES de migrar. La forma de hoy: la base se rechaza ENTERA, asi que no hay
    reinterpretacion posible — ni ciega ni declarada.

    HERMANO PENDIENTE: `ReplayUnverifiable` + el denial `REPLAY_UNVERIFIABLE` con
    `req_hash_v=99`, cuando exista el migrador."""
    ruta, b, ev = _fixture_v2(tmp_path)
    con = _crudo(ruta)
    n = con.execute("SELECT COUNT(*) FROM idempotency").fetchone()[0]
    con.close()
    # el `req_hash_v=99` del test viejo se ponia DESPUES de migrar; aqui el sustrato es la v2
    # tal cual, y lo que se exige es que el rechazo no reinterprete —ni toque— su idempotency.
    _exige_rechazo(ruta, 2, filas=(("SELECT COUNT(*) FROM idempotency", n, "su idempotency"),))
