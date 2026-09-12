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

@pytest.mark.parametrize("variante", ["A", "B"])
def test_la_migracion_preserva_todo_y_deja_la_base_integra(tmp_path, variante):
    ruta, b, s1, s2, ev, (rc1, rc2) = _fixture_v1(tmp_path, variante)
    j = _abre(ruta)
    assert j.stored_durable_v() == 1
    assert j.initialize() == C.DURABLE_V
    # Las lecturas de recibo van autenticadas desde M1-4: hace falta sesión viva.
    sx = j.open_session("cred-A")
    con = j._connect()

    # ① filas y payloads intactos
    filas = {r["command_id"]: dict(r) for r in con.execute("SELECT * FROM commands")}
    assert set(filas) == {"cmd_1", "cmd_2"}
    assert filas["cmd_1"]["payload"] == '{"n": 1}'
    assert filas["cmd_2"]["state"] == "accepted"

    # ② rol derivado del principal · ③ atribución según de dónde venga
    for f in filas.values():
        assert f["role"] == "be"
        if variante == "A":
            assert f["attribution_status"] == "legacy_unattributed"
            assert f["runtime_instance"] is None      # NO se inventa
        else:
            assert f["attribution_status"] == "verified"
            assert f["runtime_instance"] == s1.runtime_instance

    # ④ las TRES causalidades siguen apuntando a `commands`, no a `commands_v1`
    assert con.execute("SELECT cause_command_id FROM command_causes WHERE"
                       " command_id='cmd_2'").fetchone()[0] == "cmd_1"
    assert con.execute("SELECT cause_event_id FROM command_event_causes WHERE"
                       " command_id='cmd_2'").fetchone()[0] == ev.event_id
    assert con.execute("SELECT entry_eid FROM external_causes WHERE"
                       " child_kind='command' AND child_id='cmd_2'").fetchone()[0] \
        == "b" * 64
    for tabla in ("command_causes", "command_event_causes"):
        sql = con.execute("SELECT sql FROM sqlite_master WHERE name=?",
                          (tabla,)).fetchone()[0]
        assert "commands_v1" not in sql, f"{tabla} quedó apuntando al nombre viejo"
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []

    # ⑤ agregados fusionados SUMANDO, y el recibo sobrante marcado
    aggs = con.execute("SELECT * FROM denial_aggregates").fetchall()
    assert len(aggs) == 1 and aggs[0]["suppressed"] == 12      # 7 + 5
    assert "runtime_instance" not in {c[1] for c in
                                      con.execute("PRAGMA table_info(denial_aggregates)")}
    sobrante = [r for r in (rc1, rc2) if r != aggs[0]["receipt_id"]][0]
    assert con.execute("SELECT current_state FROM receipts WHERE receipt_id=?",
                       (sobrante,)).fetchone()[0] == "superseded"
    assert any(t["state"] == "superseded" and "1->2" in (t["detail"] or "")
               for t in j.transitions(sx.token, sobrante))

    # ⑥ huellas v1 purgadas, con su contador
    assert con.execute("SELECT COUNT(*) c FROM unknown_credentials").fetchone()["c"] == 0
    assert con.execute("SELECT v FROM meta WHERE k='fp_v1_purgadas'"
                       ).fetchone()["v"] == "13"

    # ⑦ FK reactivadas · ⑧ índices de `commands` conservados
    assert j._connect().execute("PRAGMA foreign_keys").fetchone()[0] == 1
    idx = {r[1]: r[2] for r in con.execute("PRAGMA index_list(commands)")}
    assert "i_cmd_ws" in idx
    assert any(nombre.startswith("sqlite_autoindex") and unico
               for nombre, unico in idx.items()), idx

    # ⑨ segunda pasada idempotente
    antes = _foto(con)
    assert j.initialize() == C.DURABLE_V
    assert _foto(j._connect()) == antes
    j.close()


@pytest.mark.parametrize("variante", ["A", "B"])
def test_tras_migrar_una_escritura_nueva_nace_verified(tmp_path, variante):
    ruta, b, s1, s2, ev, _ = _fixture_v1(tmp_path, variante)
    j = _abre(ruta)
    j.initialize()
    s = j.open_session("cred-A")
    cid, estado = j.submit_command(s.token, workstream_id="ws-nuevo", revision=1,
                                   payload={"ok": True})
    fila = j._connect().execute("SELECT * FROM commands WHERE command_id=?",
                                (cid,)).fetchone()
    assert estado == "accepted"
    assert fila["attribution_status"] == "verified"
    assert fila["runtime_instance"] == s.runtime_instance
    assert fila["role"] == s.role
    j.close()


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


def test_de_v2_a_v3_se_migra_conservando_los_datos(tmp_path):
    ruta, b, ev = _fixture_v2(tmp_path)
    j = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
    assert j.stored_durable_v() == 2
    antes = _foto(_crudo(ruta))
    assert j.initialize() == C.DURABLE_V
    con = _crudo(ruta)

    # La migración a v6 SÍ escribe filas, y son las únicas que puede escribir:
    # los pares `(carril con historia, verbo)` de la barrera, en `closed`. Se
    # comparan aparte en vez de relajar la igualdad — un `>=` o un `pop()` sin
    # comprobar dejaría pasar cualquier otra tabla que creciera.
    despues = _foto(con)
    barrera = despues.pop("admission_history")
    assert despues == antes, "la migración perdió o inventó filas"
    assert barrera == 2, "un carril con historia, sus DOS verbos"
    assert [tuple(r) for r in con.execute(
        "SELECT lane, verb, epoch, state, origin, operator, runtime_instance,"
        " reason_code FROM admission_history ORDER BY verb")] == [
        ("llminbox", "events.accept", 1, "closed", "migration", None, None,
         "SCHEMA_MIGRATION"),
        ("llminbox", "outbox.requeue", 1, "closed", "migration", None, None,
         "SCHEMA_MIGRATION")]
    assert "capabilities" in {c[1] for c in
                              con.execute("PRAGMA table_info(credential_bindings)")}
    assert "req_hash_v" in {c[1] for c in con.execute("PRAGMA table_info(idempotency)")}
    # Los defaults dicen la verdad sobre lo ya escrito.
    assert con.execute("SELECT capabilities FROM credential_bindings"
                       ).fetchone()[0] == "[]"
    assert con.execute("SELECT req_hash_v FROM idempotency").fetchone()[0] == 1
    # El índice parcial de ligadura activa sobrevive (por eso se usa ALTER y no
    # una reconstrucción: no hay nada que reconstruir).
    assert "u_binding_activa" in {r[1] for r in
                                  con.execute("PRAGMA index_list(credential_bindings)")}
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    assert j.initialize() == C.DURABLE_V                      # idempotente
    j.close()


def test_la_ruta_completa_1_a_2_a_3_llega_entera(tmp_path):
    """Una base v1 tiene que poder subir DOS escalones de una vez: quien lleve
    tiempo sin actualizar no debería tener que pasar por una versión intermedia
    que ya no existe en ningún sitio."""
    ruta, b, s1, s2, ev, _ = _fixture_v1(tmp_path, "A")
    j = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
    assert j.stored_durable_v() == 1
    assert j.initialize() == C.DURABLE_V
    con = j._connect()
    cols_cmd = {c[1] for c in con.execute("PRAGMA table_info(commands)")}
    assert "attribution_status" in cols_cmd                     # paso 1->2
    assert "capabilities" in {c[1] for c in
                              con.execute("PRAGMA table_info(credential_bindings)")}
    assert "req_hash_v" in {c[1] for c in con.execute("PRAGMA table_info(idempotency)")}
    assert con.execute("SELECT COUNT(*) c FROM commands").fetchone()["c"] == 2
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    j.close()


def test_un_evento_v2_escrito_con_ledger_None_hace_replay_tras_migrar(tmp_path):
    """El caso que sólo se ve reconstruyendo la fórmula HISTÓRICA.

    En `bcd05c5` el hash se calculaba ANTES de derivar el destino y sobre el
    `ledger` CRUDO del argumento — que por defecto es `None`. Su fila migra con
    `req_hash_v=1`. Si al comprobarla usáramos el destino EFECTIVO (`llminbox`),
    el hash no casaría y el reintento legítimo saldría `409`: castigar a quien
    reintenta por una migración que él no pidió.

    Los dos brazos del reintento —`ledger=None` y el explícito equivalente—
    tienen que hacer replay, porque en v1 la fórmula hasheaba `None` y hoy la
    fila se juzga con SU fórmula.
    """
    ruta = str(tmp_path / "coordination.sqlite")
    intent = {"type": "message", "verb": "inform", "kind": "FYI", "to": ["be"],
              "head": "h", "body": "cuerpo"}
    # v2 EXACTO: sin `capabilities` ni `req_hash_v`, y con la fila de
    # idempotencia calculada como la calculaba aquel binario.
    j = C.Journal(ruta, pepper=PEPPER, lane_ledgers={"llminbox": ["llminbox"]}, recipient_resolver=censo, grammar=GRAMATICA)
    j.initialize()
    abre_admision(j, lanes=["llminbox"])
    j.bind_credential("cred-A", principal="backend", role="be", lane="llminbox")
    s = j.open_session("cred-A", ttl_s=10 ** 6)
    a = j.accept_event(s.token, idempotency_key="k", intent=intent)   # sin ledger
    con = j._connect()
    con.execute("PRAGMA foreign_keys=OFF")
    for tabla, ddl in (("credential_bindings", BINDINGS_V2),
                       ("idempotency", IDEMPOTENCY_V2)):
        cols = [c[1] for c in con.execute(f"PRAGMA table_info({tabla})")]
        comunes = ",".join(c for c in cols
                           if c not in {"capabilities", "req_hash_v"})
        con.execute(f"ALTER TABLE {tabla} RENAME TO {tabla}_tmp")
        con.executescript(ddl)
        con.execute(f"INSERT INTO {tabla}({comunes}) SELECT {comunes} FROM {tabla}_tmp")
        con.execute(f"DROP TABLE {tabla}_tmp")
        if tabla == "credential_bindings":
            con.execute(IDX_BINDINGS)
    # El hash TAL Y COMO lo escribía bcd05c5: ledger crudo `None`, sin fence.
    con.execute("UPDATE idempotency SET req_hash=?",
                (C._req_hash(1, intent=intent, ledger_raw=None,
                             ledger_efectivo="llminbox", causes=(),
                             external_causes=(), fenced_resource=None,
                             fencing_token=None),))
    con.execute("DROP TABLE admission_history")   # v2 tampoco la tenía
    con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('durable_v','2')")
    con.execute("PRAGMA foreign_keys=ON")
    j.close()

    j2 = C.Journal(ruta, pepper=PEPPER, lane_ledgers={"llminbox": ["llminbox"]}, recipient_resolver=censo, grammar=GRAMATICA)
    assert j2.initialize() == C.DURABLE_V
    assert j2._connect().execute("SELECT req_hash_v FROM idempotency"
                                 ).fetchone()[0] == 1
    s2 = j2.open_session("cred-A", ttl_s=10 ** 6)
    # ① El reintento en la MISMA forma en que se escribió: replay limpio.
    b = j2.accept_event(s2.token, idempotency_key="k", intent=intent)
    assert b.replayed is True and b.event_id == a.event_id

    # ② El equivalente EXPLÍCITO da conflicto, y es lo correcto aunque incomode:
    #    bajo v1 el ledger se hasheaba CRUDO, así que `None` y `"llminbox"` eran
    #    dos peticiones distintas de verdad. Ésa era la fisura que v2 cierra, y
    #    hacer que ahora casen sería REINTERPRETAR una fila histórica —decidir
    #    hoy lo que aquel binario no decidió— para que salga un número bonito.
    #    Las filas nuevas ya no tienen el problema: nacen en v2, sobre el destino
    #    efectivo, y ahí las dos formas coinciden (lo prueba
    #    `test_el_destino_derivado_y_el_explicito_dan_el_MISMO_hash`).
    with pytest.raises(C.IdempotencyConflict):
        j2.accept_event(s2.token, idempotency_key="k", intent=intent,
                        ledger="llminbox")
    assert j2._connect().execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == 1
    # ⊕ y un cuerpo DISTINTO con la misma clave sigue siendo conflicto.
    with pytest.raises(C.IdempotencyConflict):
        j2.accept_event(s2.token, idempotency_key="k",
                        intent={**intent, "body": "otro"})
    j2.close()


def test_una_version_de_hash_DESCONOCIDA_no_se_reinterpreta(tmp_path):
    """Ni replay ciego ni evento nuevo: se declara que no se puede comprobar."""
    ruta, b, ev = _fixture_v2(tmp_path)
    j = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
    j.initialize()
    j._connect().execute("UPDATE idempotency SET req_hash_v=99")
    s = j.open_session("cred-A", ttl_s=10 ** 6)
    with pytest.raises(C.ReplayUnverifiable):
        j.accept_event(s.token, idempotency_key="k1",
                       intent={"type": "message", "verb": "inform", "kind": "FYI",
                               "to": ["be"], "head": "h", "body": "cuerpo"},
                       ledger="llminbox")
    con = j._connect()
    assert con.execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == 1
    assert con.execute("SELECT reason FROM denials ORDER BY at DESC LIMIT 1"
                       ).fetchone()["reason"] == "REPLAY_UNVERIFIABLE"
    j.close()
