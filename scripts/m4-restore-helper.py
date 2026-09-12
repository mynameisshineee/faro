"""Restauración del bundle DENTRO del volumen, con la aplicación PARADA.

Corre en un contenedor efímero (`docker run --rm`, sin entrypoint de la app) porque
`docker exec` no existe contra un contenedor parado, y la restauración EXIGE que nadie
tenga el almacén abierto.

Contrato: <db_destino> <fuente_ro> <id_transaccion> <sha_esperado> <fresh|resume>

`fuente_ro` es un bind de SÓLO LECTURA del host: **no puede ser la etapa**. Un bind del
host y el volumen son sistemas de ficheros distintos, así que `os.replace` desde ahí da
`EXDEV` siempre — la fase 2 habría fallado en el 100% de las corridas. Sus bytes se
COPIAN a una etapa creada dentro del volumen, y sólo esa entra en el renombrado.

⛔ EL JOURNAL DE COORDINACIÓN NO SE RESTAURA POR AQUÍ, NUNCA. `ADR-001` dice que la
recuperación del índice no puede borrar ni recrear el journal, y el paso ⑥ de abajo es un
`os.replace`, o sea exactamente ese borrado. Hasta ahora eso lo cumplía el mapa de
volúmenes del compose del piloto (`/journal` y `/data` separados) — una propiedad de la
TOPOLOGÍA, no del código—, y este fichero tenía `0` menciones de `journal`. Lo que el
sustrato regala se lee como ahorro y es una dependencia: en la topología de la flota, con
un solo `/data`, esa separación no existe.
Ahora se clasifica el almacén por IDENTIDAD Y CONTENIDO —sello de `meta` y conjunto de
tablas— y **no por su nombre ni por su ruta**: renombrar `coordination.sqlite` a
`llminbox.sqlite` no puede convertir un journal en un índice. Ver `clasifica_almacen`.

Secuencia, y cada paso existe por un corte concreto:
  ① SHA de la fuente y comparación con el esperado  → `integrity_check` dice que una
     base está SANA, no que sea LA base.
  ② copia a etapa `O_EXCL` DENTRO del volumen, flush+fsync  → mismo dispositivo.
  ③ integrity_check y CLASE SOBRE LA ETAPA          → se valida lo que se va a publicar.
  ④ exigir bundle FRÍO: cualquier `-wal`/`-shm`/`-journal` rechaza sin abrirlo.
  ⑤ copia de rollback del db actual, con fsync      → lo único a lo que volver.
  ⑥ revalidar que no aparecieron sidecars y renombrar SÓLO el db. Un renombrado de un
     fichero es atómico; el de tres no lo era, y por eso ya no se hace.
"""
import hashlib
import json
import os
import sqlite3
import stat
import sys


def fsync_ruta(p):
    fd = os.open(p, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def no(msg):
    print("NO " + msg)
    sys.exit(1)


# ── CLASIFICADOR DE ALMACÉN · por IDENTIDAD Y CONTENIDO, jamás por nombre ────────────
#
# Medido sobre `7778de9`, y es lo que hace falsable la separación:
#   · el JOURNAL de coordinación sella `meta.durable_v` (+ `pepper_check`, `generation`)
#     y trae el núcleo de objetos que TODOS los manifiestos de `coordination.py`
#     comparten (`MANIFIESTOS[1..3]`, coordination.py:1250-1271);
#   · el ÍNDICE sella el valor exacto de `meta.schema_v` (servicio.py:3115-3121) y
#     acredita DDL efectivo: columnas/PK/defaults, índices/collations, FKs y ausencia
#     de triggers sobre `entries`/`recipients`/`files`/`cursors`;
#   · `durable_v` tiene 0 hits en `servicio.py` fuera de dos `None` del bloque de salud
#     (`:497`, `:504`), o sea que el índice NUNCA lo escribe.
# Los dos conjuntos de tablas son DISJUNTOS salvo `meta`.
JOURNAL_TABLAS = frozenset({
    "principals", "credential_bindings", "runtime_sessions", "events", "receipts",
    "receipt_transitions", "idempotency", "outbox", "leases", "commands"})
JOURNAL_META = frozenset({"durable_v", "pepper_check", "generation"})
INDICE_TABLAS = frozenset({"entries", "recipients", "files", "cursors"})
INDICE_META = frozenset({"schema_v"})
INDICE_SCHEMA_V = "e7f6c011776e8db7"  # sha256(str(SCHEMA_V=6))[:16]
INDICE_DDL = """
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE entries (
  ledger TEXT NOT NULL, eid TEXT NOT NULL,
  arrival INTEGER, seq INTEGER, line_no INTEGER, byte_off INTEGER,
  ts TEXT, actor TEXT, tipo TEXT, head TEXT, body TEXT,
  visto TEXT, ausente TEXT, provisional INTEGER DEFAULT 0,
  raw_tipo TEXT, canonical_kind TEXT, kind_registry_rev INTEGER,
  PRIMARY KEY (ledger, eid));
CREATE INDEX i_arr ON entries(ledger, arrival);
CREATE INDEX i_seq ON entries(ledger, seq);
CREATE INDEX i_ts ON entries(ledger, ts);
CREATE INDEX i_actor ON entries(ledger, actor);
CREATE INDEX i_tipo ON entries(ledger, tipo);
CREATE INDEX i_raw_tipo ON entries(raw_tipo COLLATE NOCASE, ledger);
CREATE TABLE recipients (
  ledger TEXT NOT NULL, eid TEXT NOT NULL, who TEXT NOT NULL,
  PRIMARY KEY (ledger, eid, who));
CREATE INDEX i_who ON recipients(who, ledger, eid);
CREATE TABLE files (
  ledger TEXT PRIMARY KEY, path TEXT, bytes INTEGER, entries INTEGER,
  mtime REAL, scanned REAL);
CREATE TABLE cursors (
  agent TEXT NOT NULL, ledger TEXT NOT NULL, last_arrival INTEGER, updated TEXT,
  PRIMARY KEY (agent, ledger));
"""
SIDECARS = ("-wal", "-shm", "-journal")
INDICE_OBJETOS_DDL = (
    "meta", "entries", "recipients", "files", "cursors",
    "i_arr", "i_seq", "i_ts", "i_actor", "i_tipo", "i_raw_tipo", "i_who")


def sidecars_presentes(ruta):
    return tuple(s for s in SIDECARS if os.path.lexists(ruta + s))


def ascii_sqlite_fold(valor):
    """Case-insensitive de identificadores SQLite: sólo ASCII, nunca Unicode."""
    if not isinstance(valor, str):
        return valor
    return "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in valor)


def forma_tabla(con, tabla):
    columnas = tuple(
        (ascii_sqlite_fold(r[1]), ascii_sqlite_fold(r[2]), r[3], r[4], r[5])
        for r in con.execute(f'PRAGMA table_info("{tabla}")'))
    indices = []
    for r in con.execute(f'PRAGMA index_list("{tabla}")'):
        nombre = r[1]
        cols = tuple(
            (ascii_sqlite_fold(x[2]), x[3], ascii_sqlite_fold(x[4]), x[5])
            for x in con.execute(f'PRAGMA index_xinfo("{nombre}")'))
        indices.append((ascii_sqlite_fold(nombre), bool(r[2]),
                        ascii_sqlite_fold(r[3]), bool(r[4]), cols))
    fks = tuple(
        tuple(ascii_sqlite_fold(v) for v in r[2:8])
        for r in con.execute(f'PRAGMA foreign_key_list("{tabla}")'))
    return columnas, tuple(sorted(indices)), fks


def canon_sql(sql, identificadores=()):
    """Tokens SQLite: ignora comentarios, espacios y case ASCII.

    No elimina ningún operador ni cláusula. Por eso `CHECK`, `STRICT`, `COLLATE`,
    `WITHOUT ROWID`, conflictos y expresiones permanecen en la identidad aunque
    `table_info` no los proyecte. Los identificadores citados y sin citar del
    manifiesto convergen; los literales de datos y el case Unicode no se relajan.
    """
    if not isinstance(sql, str):
        return ()
    identificadores = {ascii_sqlite_fold(x) for x in identificadores}
    tokens = []
    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        if c.isspace():
            i += 1
            continue
        if sql.startswith("--", i):
            fin = sql.find("\n", i + 2)
            i = n if fin < 0 else fin + 1
            continue
        if sql.startswith("/*", i):
            fin = sql.find("*/", i + 2)
            if fin < 0:
                return (("error", "comentario-sin-cerrar"),)
            i = fin + 2
            continue
        if c == "'":
            inicio = i
            i += 1
            while i < n:
                if sql[i] == c:
                    if i + 1 < n and sql[i + 1] == c:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            else:
                return (("error", "literal-sin-cerrar"),)
            tokens.append(("literal", sql[inicio:i]))
            continue
        if c in ('"', "`"):
            i += 1
            partes = []
            while i < n:
                if sql[i] == c:
                    if i + 1 < n and sql[i + 1] == c:
                        partes.append(c); i += 2
                        continue
                    i += 1
                    break
                partes.append(sql[i]); i += 1
            else:
                return (("error", "identificador-sin-cerrar"),)
            tokens.append(("ident", ascii_sqlite_fold("".join(partes))))
            continue
        if c == "[":
            fin = sql.find("]", i + 1)
            if fin < 0:
                return (("error", "identificador-sin-cerrar"),)
            tokens.append(("ident", ascii_sqlite_fold(sql[i + 1:fin])))
            i = fin + 1
            continue
        if c.isalnum() or c in "_$":
            fin = i + 1
            while fin < n and (sql[fin].isalnum() or sql[fin] in "_$"):
                fin += 1
            palabra = ascii_sqlite_fold(sql[i:fin])
            tokens.append(("ident" if palabra in identificadores else "word", palabra))
            i = fin
            continue
        op = next((x for x in ("->>", "||", "<<", ">>", "<=", ">=", "==", "!=", "<>", "->")
                   if sql.startswith(x, i)), None)
        if op:
            tokens.append(("op", op)); i += len(op)
        else:
            tokens.append(("punct", c)); i += 1
    return tuple(tokens)


def identificadores_canonicos(ref):
    ids = set()
    for nombre, tabla in ref.execute(
            "SELECT name, tbl_name FROM sqlite_master WHERE sql IS NOT NULL"):
        ids.update((ascii_sqlite_fold(nombre), ascii_sqlite_fold(tabla)))
    for tabla in ("meta", *sorted(INDICE_TABLAS)):
        ids.update(ascii_sqlite_fold(r[1]) for r in ref.execute(
            f'PRAGMA table_info("{tabla}")'))
        for r in ref.execute(f'PRAGMA index_list("{tabla}")'):
            ids.add(ascii_sqlite_fold(r[1]))
            for x in ref.execute(f'PRAGMA index_xinfo("{r[1]}")'):
                if x[2] is not None:
                    ids.add(ascii_sqlite_fold(x[2]))
                if x[4] is not None:
                    ids.add(ascii_sqlite_fold(x[4]))
    return ids


def ddl_indice_canonico(con, ref):
    ids = identificadores_canonicos(ref)
    reales = {}
    for nombre, tipo, tabla, sql in con.execute(
            "SELECT name, type, tbl_name, sql FROM sqlite_master"):
        clave = ascii_sqlite_fold(nombre)
        if clave in reales:  # SQLite normalmente ya impide esta ambigüedad ASCII.
            return False
        reales[clave] = (tipo, tabla, sql)
    for nombre in INDICE_OBJETOS_DDL:
        real = reales.get(ascii_sqlite_fold(nombre))
        esperado = ref.execute(
            "SELECT type, tbl_name, sql FROM sqlite_master WHERE name=?", (nombre,)
        ).fetchone()
        if not real or not esperado:
            return False
        if (tuple(ascii_sqlite_fold(x) for x in real[:2]) !=
                tuple(ascii_sqlite_fold(x) for x in esperado[:2]) or
                canon_sql(real[2], ids) != canon_sql(esperado[2], ids)):
            return False
    return True


def forma_indice_canonica(con):
    ref = sqlite3.connect(":memory:")
    try:
        ref.executescript(INDICE_DDL)
        tablas = ("meta", *sorted(INDICE_TABLAS))
        tablas_indice = {"meta", *(ascii_sqlite_fold(t) for t in INDICE_TABLAS)}
        sin_triggers = not any(
            ascii_sqlite_fold(r[0]) in tablas_indice
            for r in con.execute("SELECT tbl_name FROM sqlite_master WHERE type='trigger'")
        )
        return (sin_triggers and ddl_indice_canonico(con, ref) and
                all(forma_tabla(con, t) == forma_tabla(ref, t) for t in tablas))
    finally:
        ref.close()


def clasifica_almacen(ruta):
    """(clase, motivo) sobre un descriptor estable e inmutable. Fail-closed.

    El orden NO es preferencia: se busca primero la señal de journal porque la
    dirección segura de esta función es NEGARSE. Una base a medio construir tiene las
    tablas sin el sello, y una a la que le arrancaron tablas tiene el sello sin ellas:
    CUALQUIERA de las dos señales basta para rechazar, y ninguna sola basta para
    aceptar.
    """
    calientes = sidecars_presentes(ruta)
    if calientes:
        return ("indeterminado", "bundle caliente con sidecars: " + ", ".join(calientes))
    if not hasattr(os, "O_NOFOLLOW"):
        return ("indeterminado", "la plataforma no ofrece O_NOFOLLOW")
    try:
        fd = os.open(ruta, os.O_RDONLY | os.O_NOFOLLOW)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            os.close(fd)
            return ("indeterminado", "el almacén no es un fichero regular")
        identidad = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
        con = sqlite3.connect(f"file:/dev/fd/{fd}?mode=ro&immutable=1", uri=True)
    except (OSError, sqlite3.Error) as e:
        try:
            os.close(fd)
        except (NameError, OSError):
            pass
        return ("indeterminado", f"no abre como SQLite inmutable: {e}")
    resultado = None
    try:
        integridad = con.execute("PRAGMA integrity_check").fetchone()
        if not integridad or integridad[0] != "ok":
            resultado = ("indeterminado", f"integrity_check={integridad!r}")
        else:
            tablas = {ascii_sqlite_fold(r[0]) for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "  AND name NOT LIKE 'sqlite_%'")}
            claves = set()
            valores = {}
            if "meta" in tablas:
                try:
                    valores = dict(con.execute("SELECT k, v FROM meta"))
                    claves = set(valores)
                except (TypeError, ValueError, sqlite3.Error) as e:
                    resultado = ("indeterminado", f"`meta` ilegible: {e}")
            if resultado is None:
                senales = sorted(tablas & JOURNAL_TABLAS) + sorted(
                    f"meta.{k}" for k in claves & JOURNAL_META)
                if senales:
                    resultado = ("journal", "señales de journal de coordinación: "
                                 + ", ".join(senales))
                else:
                    faltan = sorted(INDICE_TABLAS - tablas) + sorted(
                        f"meta.{k}" for k in INDICE_META - claves)
                    if faltan:
                        resultado = ("indeterminado", "no acredita ser el índice; le faltan: "
                                     + ", ".join(faltan))
                    elif valores.get("schema_v") != INDICE_SCHEMA_V:
                        resultado = ("indeterminado", "meta.schema_v no es el sello canónico "
                                     f"{INDICE_SCHEMA_V}")
                    elif not forma_indice_canonica(con):
                        resultado = ("indeterminado", "DDL/columnas/índices no coinciden con "
                                     "el manifiesto canónico del índice")
                    else:
                        resultado = ("indice", "índice reconstruible: sello y manifiesto "
                                     "canónicos")
    except (TypeError, ValueError, sqlite3.Error) as e:
        resultado = ("indeterminado", f"no se deja consultar: {e}")
    finally:
        con.close()
        os.close(fd)
    calientes = sidecars_presentes(ruta)
    if calientes:
        return ("indeterminado", "aparecieron sidecars durante la lectura: "
                + ", ".join(calientes))
    try:
        ahora = os.stat(ruta, follow_symlinks=False)
    except OSError as e:
        return ("indeterminado", f"el path cambió durante la lectura: {e}")
    if (ahora.st_dev, ahora.st_ino, ahora.st_size, ahora.st_mtime_ns) != identidad:
        return ("indeterminado", "el path cambió durante la lectura")
    return resultado


def exige_indice(ruta, papel):
    clase, motivo = clasifica_almacen(ruta)
    if clase == "journal":
        no(f"{papel} es el JOURNAL de coordinación y la restauración genérica NO lo "
           f"toca (ADR-001): {motivo}. El journal necesita su propio procedimiento, "
           f"que hoy NO existe.")
    if clase != "indice":
        no(f"{papel} no acredita ser el índice reconstruible: {motivo}")


destino, fuente, sello, sha_esperado, modo = sys.argv[1:6]
d = os.path.dirname(destino) or "."
if not hasattr(os, "O_NOFOLLOW"):
    no("la plataforma no ofrece O_NOFOLLOW")
nofollow = os.O_NOFOLLOW
if modo not in ("fresh", "resume"):
    no("modo inválido")

if os.path.islink(fuente) or os.path.islink(destino):
    no("fuente o destino es symlink: no sigo enlaces en una restauración")
if os.path.realpath(d) != d or not os.path.isdir(d):
    no("el directorio destino no es real/confinado")

etapa = os.path.join(d, f".m4-stage-{sello}.sqlite")
respaldo = f"{destino}.rollback-{sello}"


def hash_regular(path):
    if os.path.islink(path):
        no(f"symlink no permitido: {path}")
    fd = os.open(path, os.O_RDONLY | nofollow)
    h = hashlib.sha256()
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            no(f"no es fichero regular: {path}")
        while True:
            b = os.read(fd, 1 << 20)
            if not b: break
            h.update(b)
    finally: os.close(fd)
    return h.hexdigest()


def identidad_regular(path):
    fd = os.open(path, os.O_RDONLY | nofollow)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            no(f"no es fichero regular: {path}")
        return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    finally:
        os.close(fd)


# ⛔ EL DESTINO SE CLASIFICA ANTES DEL PRIMER EFECTO LATERAL. Si lo que hay en `destino`
# es el journal de coordinación, aquí no se ha creado ni una etapa ni un backup: se sale
# sin haber tocado un byte. Ponerlo detrás de la etapa dejaría un fichero a medias dentro
# del volumen del journal, que es el sitio donde menos se puede dejar basura.
# Un destino AUSENTE no se clasifica —no hay nada que clasificar— y lo cubre la
# clasificación de la ETAPA, que es lo que de verdad se va a publicar.
destino_existia = os.path.lexists(destino)
destino_identidad = None
if destino_existia:
    exige_indice(destino, "el destino")
    destino_identidad = identidad_regular(destino)

# Un retry desde `pre_restore` no vuelve ciegamente: sólo continúa si el backup ligado
# al token acredita el estado anterior, o declara éxito si el destino ya es el snapshot.
if os.path.lexists(respaldo):
    if modo == "fresh":
        no(f"backup preplantado o colisión: {respaldo}")
    if os.path.islink(respaldo):
        no("backup de reanudación es symlink")
    if os.path.lexists(destino) and hash_regular(destino) == sha_esperado:
        print("OK " + json.dumps({"respaldo": respaldo, "sha": sha_esperado,
                                  "reanudado": True})); raise SystemExit(0)
    if not os.path.lexists(destino) or hash_regular(destino) != hash_regular(respaldo):
        no("pre_restore ambiguo: destino no coincide ni con snapshot ni con backup")

# ①+② UNA apertura de la fuente: los mismos bytes se hashean y copian. Una etapa del
# mismo token sólo se acepta en resume y sólo si ya tiene el hash esperado.
if os.path.lexists(etapa):
    if modo != "resume" or hash_regular(etapa) != sha_esperado:
        no(f"etapa preplantada o de otros bytes: {etapa}")
else:
    fd = os.open(etapa, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600)
    src = os.open(fuente, os.O_RDONLY | nofollow); h = hashlib.sha256()
    try:
        if not stat.S_ISREG(os.fstat(src).st_mode):
            os.unlink(etapa)
            no("la fuente no es un fichero regular")
        with os.fdopen(fd, "wb") as w:
            while True:
                b = os.read(src, 1 << 20)
                if not b: break
                h.update(b); w.write(b)
            w.flush(); os.fsync(w.fileno())
    finally: os.close(src)
    if h.hexdigest() != sha_esperado:
        os.unlink(etapa); fsync_ruta(d)
        no(f"la fuente no es la anclada: esperado {sha_esperado}, es {h.hexdigest()}")
    fsync_ruta(d)
if os.stat(etapa).st_dev != os.stat(d).st_dev:
    no("EXDEV: la etapa no comparte dispositivo con el destino")

# ③ se valida lo que se va a publicar
try:
    con = sqlite3.connect(f"file:{etapa}?mode=ro", uri=True)
    if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        con.close(); os.unlink(etapa); no("la etapa no supera integrity_check")
    con.close()
except sqlite3.Error as e:
    os.unlink(etapa)
    no(f"la etapa no abre como SQLite: {e}")
# 🩸 AQUÍ SE EXIGÍA `meta.durable_v`, Y ERA EL CRITERIO INVERTIDO. `durable_v` es el
# sello del JOURNAL (`coordination.py`); el índice NUNCA lo escribe. O sea que la única
# comprobación de clase que había seleccionaba A FAVOR del journal y EN CONTRA del
# índice: un snapshot real del índice no podía pasar, y uno del journal sí. Se sustituye
# por la clase, que es lo que la comprobación quería decir.
clase_etapa, motivo_etapa = clasifica_almacen(etapa)
if clase_etapa != "indice":
    os.unlink(etapa); fsync_ruta(d)
    if clase_etapa == "journal":
        no(f"la etapa es el JOURNAL de coordinación y la restauración genérica NO lo "
           f"publica (ADR-001): {motivo_etapa}")
    no(f"la etapa no acredita ser el índice reconstruible: {motivo_etapa}")

if os.path.lexists(destino):
    # ④ No se abre el bundle actual en modo SQLite normal: incluso `mode=ro` participa
    # en el protocolo WAL y cambia bytes de `-shm`. `clasifica_almacen` ya hizo
    # integrity_check sobre descriptor estable + immutable=1 y rechazó sidecars ANTES
    # de abrir. Se vuelve a comprobar justo antes del primer efecto lateral.
    vivos = sidecars_presentes(destino)
    if vivos:
        os.unlink(etapa)
        fsync_ruta(d)
        no(f"el bundle dejó de estar frío; aparecieron sidecars {list(vivos)}")
    if not destino_existia or identidad_regular(destino) != destino_identidad:
        os.unlink(etapa); fsync_ruta(d)
        no("el destino apareció o cambió después de clasificarlo")
    # ⑤ copia de rollback del almacén previo
    if not os.path.lexists(respaldo):
        try:
            bfd = os.open(respaldo, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600)
        except FileExistsError:
            os.unlink(etapa); no(f"backup preplantado o colisión: {respaldo}")
        old = os.open(destino, os.O_RDONLY | nofollow)
        try:
            st_old = os.fstat(old)
            actual = (st_old.st_dev, st_old.st_ino, st_old.st_size,
                      st_old.st_mtime_ns, st_old.st_ctime_ns)
            if not stat.S_ISREG(st_old.st_mode) or actual != destino_identidad:
                os.close(bfd); os.unlink(respaldo); os.unlink(etapa); fsync_ruta(d)
                no("el destino cambió antes de copiar el rollback")
            with os.fdopen(bfd, "wb") as w:
                while True:
                    b = os.read(old, 1 << 20)
                    if not b: break
                    w.write(b)
                w.flush(); os.fsync(w.fileno())
        finally: os.close(old)
        fsync_ruta(d)

# ⑥ Los sidecars nunca se borran como parte del swap. Si aparecen, hay otro escritor o
# una recuperación pendiente: ambos son motivo para detenerse, no basura que retirar.
vivos = sidecars_presentes(destino)
if vivos:
    if os.path.lexists(etapa):
        os.unlink(etapa)
        fsync_ruta(d)
    no(f"aparecieron sidecars antes de publicar: {list(vivos)}")
if destino_existia:
    if not os.path.lexists(destino) or identidad_regular(destino) != destino_identidad:
        if os.path.lexists(etapa):
            os.unlink(etapa); fsync_ruta(d)
        no("el destino cambió antes de publicar")
elif os.path.lexists(destino):
    if os.path.lexists(etapa):
        os.unlink(etapa); fsync_ruta(d)
    no("apareció un destino que no existía al comenzar")
os.replace(etapa, destino)
fsync_ruta(destino)
fsync_ruta(d)
sobrantes = list(sidecars_presentes(destino))
if sobrantes:
    no(f"sidecars obsoletos sobrevivieron: {sobrantes}")
print("OK " + json.dumps({"respaldo": respaldo if os.path.lexists(respaldo) else None,
                           "sha": sha_esperado, "reanudado": modo == "resume"}))
