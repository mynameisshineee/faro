"""Comprobación local de migración Journal v6→v7 y restauración operativa.

El código del ancla v0.9 crea y opera una base v6. El kernel de este árbol la
migra a v7, conserva la fotografía previa, escribe y drena un evento nuevo y
sella las admisiones. Antes de restaurar, el ancla debe rechazar una copia v7
con SchemaTooNew. Después de restaurar, los bytes deben coincidir con la
fotografía y un proceso del ancla debe reabrir la base y escribir de nuevo.
La pérdida del evento escrito después de la fotografía se comprueba y declara.

El modo se elige expresamente mediante --modo-ancla git|oci:

* git crea un worktree NUEVO en el SHA certificado. Un destino existente se
  rechaza. El destino explícito se conserva; el temporal automático se retira
  con git worktree remove sin force, salvo --conservar-ancla.
* oci conserva el nombre de la opción existente y recibe un directorio de
  código ya extraído. Cada archivo ejecutado se compara con su blob Git.
  El corredor utiliza el intérprete del HOST: no ejecuta un contenedor ni
  verifica la procedencia de la extracción. El SHA256 registrado identifica
  el ARCHIVO OCI, no un manifiesto que pueda utilizarse para pull. Recuperar
  ese archivo y ejecutar el extremo dentro de la imagen son pruebas separadas.

* arbol corre sobre el fixture ORDINARIO bench/ancla-v09 de este árbol: los
  bytes certificados del ancla fletados POR CONTENIDO, sin historia git.
  La identidad se mide superficie a superficie contra hashes FIJADOS en este
  módulo (trazables al SHA certificado); el manifest vecino del fixture debe
  coincidir con ellos, pero la autoridad es el código — un fichero modificable
  por sí solo no prueba procedencia. git y oci quedan intactos para
  instalaciones que sí tienen la historia del ancla.

La limpieza se limita al temporal de esta corrida; no poda registros de otros
worktrees. Si Git rechaza retirarlo, se conserva para inspección y se informa
por stderr y en el reporte, sin cambiar el resultado del ciclo de migración.

Códigos: 0 = DEMO_OK para este ciclo local; 1 = DemoFalsada, una postcondición
fallida; 2 = DemoNoEjecutable, un fallo de preparación o del corredor del ancla.
Ninguno acredita por sí solo reproducibilidad ni operación de una imagen OCI.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

RAIZ_ANCLA_SHA = "767702559817759a36b0be4715af6e1a8ccf9f7b"
ANCLA_OCI_ARCHIVO_SHA256 = "66972a1e42eefedabbd481303b8a07ee5b7ca7821dd8bdaf423bbb8c27dfe165"
CARRIL = "llminbox"
RAZON_ROLLBACK = "DRAIN_FOR_ROLLBACK"

# El modo del ancla se DECLARA — nunca se infiere de si la ruta existe: un
# typo no puede cambiar de modo en silencio.
MODO_ANCLA = ("git", "oci", "arbol")
# Cierre transitivo de imports del corredor sobre el ancla (medido en los
# blobs del SHA): `_arnes` importa `coordination` y `ledger_parse`, ambos en
# la RAÍZ del ancla y stdlib-only. La identidad del modo oci se mide SOBRE
# ESTAS superficies y no sobre más: nada distinto de ellas corre como 0.9.
SUPERFICIES_ANCLA = (
    "coordination.py",
    "ledger_parse.py",
    "tests/journal/_arnes.py",
)
# Fixture del ancla para el modo arbol (ruta RELATIVA a la raíz del árbol) y
# su identidad FIJADA: sha256 de cada superficie, medidos sobre los blobs del
# SHA certificado al fletar el fixture. Esta constante —revisada en el
# código— es la autoridad de la identidad en modo arbol; el manifest vecino
# (bench/ancla-v09/MANIFEST.sha256) es copia trazable y DEBE coincidir, pero
# un fichero modificable por sí solo no prueba procedencia.
ANCLA_V09_RUTA = pathlib.Path("bench") / "ancla-v09"
ANCLA_V09_SUPERFICIES_SHA256 = {
    "coordination.py":
        "89793c56232148bf531d707fd3375a324bcc5d6fd47e0c872bc6792c26d23870",
    "ledger_parse.py":
        "852d530f87c1c8e6b0593c91b39b94a8e08b5c9e2b8ce4747dfe09cd8e1dc929",
    "tests/journal/_arnes.py":
        "f7f12ece8fbe7f1b9372b174cb06729f73dc2661005c9df8905bee051da453cf",
}


class DemoNoEjecutable(Exception):
    """Fallo de preparación o del corredor que impide completar la demo."""


class DemoFalsada(Exception):
    """Una postcondición o el falsador ⊖ falló: el objetivo NO está demostrado."""


_RAIZ = pathlib.Path(__file__).resolve().parents[1]
for _ruta in (str(_RAIZ), str(_RAIZ / "tests" / "journal")):
    if _ruta not in sys.path:
        sys.path.insert(0, _ruta)

import coordination as C      # noqa: E402  (kernel 1.0 de ESTE árbol)
import _arnes as arnes        # noqa: E402  (el arnés PROPIO de 1.0: PEPPER, LANES, censo, GRAMATICA, INTENT)


# ── El corredor que ejecuta DENTRO del ancla 0.9 ─────────────────────────────
# Un modo por invocación, JSON por stdout. Importa el arnés `_arnes` DEL PROPIO
# worktree del ancla: las constantes de la demo no duplican ninguna — si el
# ancla cambiara su PEPPER/LANES/censo/GRAMATICA/INTENT, este corredor las
# seguiría, que es justo lo que «operar con el código 0.9» significa.

_CORREDOR_ANCLA = r'''
import json, sqlite3, sys, time

modo, ruta, worktree, n = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
sys.path.insert(0, worktree)
sys.path.insert(0, worktree + "/tests/journal")
import coordination as C
import _arnes as arnes


def journal():
    return C.Journal(ruta, pepper=arnes.PEPPER, lane_ledgers=arnes.LANES,
                     recipient_resolver=arnes.censo, grammar=arnes.GRAMATICA,
                     clock=time.time)


def barrera(j):
    return arnes.abre_admision(j, lanes={"llminbox": arnes.LANES["llminbox"]})


def escritor(j, prefijo, n):
    cred = "cred-demo-escritor"
    j.bind_credential(cred, principal="demo-escritor", role="be",
                      lane="llminbox", capabilities=arnes.CAPS_RUNTIME)
    s = j.open_session(cred, ttl_s=900)
    ids = []
    for i in range(n):
        ev = j.accept_event(s.token, idempotency_key="%s-%d" % (prefijo, i),
                            intent=arnes.INTENT, ledger="llminbox")
        ids.append(ev.event_id)
        job = j.claim_outbox(s.token)
        j.mark_materialized(s.token, ev.event_id, entry_eid="e" * 64,
                            ledger="llminbox", claim_token=job.claim_token)
    return ids


def conteos():
    con = sqlite3.connect(ruta)
    try:
        return {
            # `meta.v` is TEXT in both the 0.9 anchor and the 1.0 schema.
            # Normalize at the evidence boundary so the demo compares the
            # version semantically rather than depending on SQLite's storage
            # representation.
            "durable_v": int(con.execute(
                "SELECT v FROM meta WHERE k='durable_v'").fetchone()[0]),
            "events": con.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            "receipts": con.execute(
                "SELECT COUNT(*) FROM receipts").fetchone()[0],
            "outbox": dict(con.execute(
                "SELECT state, COUNT(*) FROM outbox GROUP BY state")),
        }
    finally:
        con.close()


salida = {}
if modo == "escribe_v6":
    j = journal()
    j.initialize()
    barrera(j)
    salida["eventos"] = escritor(j, "demo-pre", n)
    j.close()
    salida["conteos"] = conteos()
elif modo == "reabre_v6":
    j = journal()
    j.initialize()
    barrera(j)
    salida["eventos"] = escritor(j, "demo-post", n)
    j.close()
    salida["conteos"] = conteos()
elif modo == "rechaza_v7":
    j = journal()
    try:
        j.initialize()
        salida["rechazado"] = False
    except C.JournalError as e:
        salida["rechazado"] = True
        salida["excepcion"] = type(e).__name__
        salida["mensaje"] = str(e)
else:
    raise SystemExit("modo desconocido: " + modo)
print(json.dumps(salida))
'''


def _worktree_ancla(destino: pathlib.Path,
                    raiz: pathlib.Path | None = None) -> pathlib.Path:
    """Worktree DESCOLGADO en el SHA del ancla. Si el destino existe, rechazo:
    nada del ancla certificado se recicla ni se re-etiqueta."""
    destino = pathlib.Path(destino).resolve()
    if destino.exists():
        raise DemoNoEjecutable(
            f"el destino del worktree del ancla ya existe ({destino}): nada se recicla")
    hecho = subprocess.run(
        ["git", "-C", str(raiz or _RAIZ), "worktree", "add", "--detach",
         str(destino), RAIZ_ANCLA_SHA],
        capture_output=True, text=True, timeout=120)
    if hecho.returncode != 0:
        raise DemoNoEjecutable(
            f"git worktree add del ancla {RAIZ_ANCLA_SHA[:12]} falló: "
            f"{hecho.stderr.strip()[-500:]}")
    return destino


def _identidad_ancla_oci(raiz: pathlib.Path) -> dict:
    """Identidad del checkout externo, MEDIDA y no heredada.

    En modo git la identidad la regala `worktree add --detach <SHA>`: el SHA
    ES la identidad. Un directorio ya extraído (fase 2 OCI) no tiene ese
    regalo, así que cada superficie que el corredor va a importar se mide byte
    a byte contra el blob del SHA certificado. Un checkout que NO sea el ancla
    muere AQUÍ, antes de tocar al sujeto — si no, el gancho sería «corre la
    demo contra lo que haya en esta carpeta» y la fase 0 se falsificaría sin
    que nada lo note.

    Esto identifica los archivos que se ejecutan en el host. No comprueba un
    archivo OCI, un digest de manifiesto ni la procedencia de la extracción.
    """
    verificadas = {}
    for superficie in SUPERFICIES_ANCLA:
        fichero = raiz / superficie
        if not fichero.is_file():
            raise DemoNoEjecutable(
                f"el checkout externo no trae {superficie}: no es un ancla "
                f"0.9 operable ({raiz})")
        blob = subprocess.run(
            ["git", "-C", str(_RAIZ), "show", f"{RAIZ_ANCLA_SHA}:{superficie}"],
            capture_output=True, timeout=60)
        if blob.returncode != 0:
            raise DemoNoEjecutable(
                f"no pude leer el blob certificado {superficie} del SHA "
                f"{RAIZ_ANCLA_SHA[:12]}: "
                f"{blob.stderr.decode(errors='replace').strip()[-300:]}")
        h_blob = hashlib.sha256(blob.stdout).hexdigest()
        h_local = _sha256_fichero(fichero)
        if h_local != h_blob:
            raise DemoNoEjecutable(
                f"IDENTIDAD DEL ANCLA NO VERIFICADA: {superficie} del checkout "
                f"externo ({h_local[:16]}…) NO es el blob del SHA certificado "
                f"({h_blob[:16]}…) — este directorio no es el ancla "
                f"{RAIZ_ANCLA_SHA[:12]}")
        verificadas[superficie] = h_blob
    return verificadas


def _lee_manifest_ancla_v09(raiz: pathlib.Path) -> dict:
    """Parsea el manifest del fixture (`<hash>  <ruta>`, formato sha256sum;
    comentarios `#` y líneas vacías fuera). Sin manifest no hay fixture."""
    ruta_manifest = raiz / "MANIFEST.sha256"
    if not ruta_manifest.is_file():
        raise DemoNoEjecutable(
            f"el fixture del ancla no trae MANIFEST.sha256 ({raiz})")
    manifest: dict = {}
    for linea in ruta_manifest.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#"):
            continue
        hash_valor, _, ruta = linea.partition("  ")
        manifest[ruta.strip()] = hash_valor.strip()
    return manifest


def _identidad_ancla_arbol(raiz: pathlib.Path) -> dict:
    """Identidad del FIXTURE arbol: POR CONTENIDO, contra los hashes fijados
    en este módulo — no contra el manifest vecino, que un editor de texto
    puede mentir solo. Cada constante es el sha256 del blob certificado en
    RAIZ_ANCLA_SHA, medido al fletar el fixture; el manifest DEBE coincidir
    con las constantes, y discrepar de ellas es un fallo de preparación.

    Esto identifica los archivos que se ejecutan en el host. No acredita la
    historia git del ancla: esa acreditación es la de los modos git y oci,
    que exigen el SHA en un repositorio."""
    verificadas = {}
    for superficie in SUPERFICIES_ANCLA:
        fichero = raiz / superficie
        if not fichero.is_file():
            raise DemoNoEjecutable(
                f"el fixture del ancla no trae {superficie}: no es un ancla "
                f"0.9 operable ({raiz})")
        h_local = _sha256_fichero(fichero)
        esperado = ANCLA_V09_SUPERFICIES_SHA256[superficie]
        if h_local != esperado:
            raise DemoNoEjecutable(
                f"IDENTIDAD DEL ANCLA NO VERIFICADA: {superficie} del fixture "
                f"({h_local[:16]}…) NO es el hash fijado del ancla "
                f"{RAIZ_ANCLA_SHA[:12]} ({esperado[:16]}…)")
        verificadas[superficie] = h_local
    if _lee_manifest_ancla_v09(raiz) != ANCLA_V09_SUPERFICIES_SHA256:
        raise DemoNoEjecutable(
            "el MANIFEST.sha256 del fixture NO coincide con los hashes "
            "fijados en el código: la copia trazable del ancla está desviada")
    return verificadas


def _retira_ancla_temporal(temporal: pathlib.Path, ancla: pathlib.Path, *,
                           creada: bool) -> dict:
    """Limpieza del temporal propio; un rechazo no borra evidencia a la fuerza."""
    resultado = {"intentada": True, "completada": False}
    try:
        if creada:
            hecho = subprocess.run(
                ["git", "-C", str(_RAIZ), "worktree", "remove", "--", str(ancla)],
                capture_output=True, timeout=60)
            if hecho.returncode != 0:
                resultado["error"] = "git_worktree_remove_rechazado"
                return resultado
        # rmdir sólo retira un directorio vacío; nunca elimina un resto ajeno
        # o un checkout parcial si la creación falló a mitad.
        temporal.rmdir()
        resultado["completada"] = True
    except (OSError, subprocess.TimeoutExpired) as exc:
        resultado["error"] = type(exc).__name__
    return resultado


def _corre_ancla(modo: str, ruta: pathlib.Path, worktree: pathlib.Path,
                 n: int = 0, *, si_muere: str = "no_ejecutable") -> dict:
    hecho = subprocess.run(
        [sys.executable, "-c", _CORREDOR_ANCLA, modo, str(ruta),
         str(worktree), str(n)],
        capture_output=True, text=True, timeout=300)
    if hecho.returncode != 0:
        detalle = hecho.stderr.strip()[-800:]
        if si_muere == "falsada":
            # El extremo operativo corre SOBRE la base restaurada: si el
            # ancla muere ahí, el rollback empezó y falló — evidencia contra
            # el objetivo, no un fallo de arranque (taxonomía @qa 20:31Z).
            raise DemoFalsada(
                f"el extremo operativo murió sobre la base RESTAURADA (modo "
                f"{modo}, rc={hecho.returncode}): el rollback NO dejó una base "
                f"que el ancla 0.9 pueda operar — objetivo NO demostrado "
                f"({detalle})")
        raise DemoNoEjecutable(
            f"el corredor del ancla murió (modo {modo}, rc={hecho.returncode}): "
            f"{detalle}")
    lineas = [l for l in hecho.stdout.strip().splitlines() if l.strip()]
    if not lineas:
        raise DemoNoEjecutable(
            f"el corredor del ancla no imprimió JSON (modo {modo})")
    return json.loads(lineas[-1])


def _sha256_fichero(ruta: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()


def _conteos(ruta: pathlib.Path) -> dict:
    con = sqlite3.connect(f"file:{ruta}?mode=ro", uri=True)
    try:
        return {
            "durable_v": int(con.execute(
                "SELECT v FROM meta WHERE k='durable_v'").fetchone()[0]),
            "events": con.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            "receipts": con.execute(
                "SELECT COUNT(*) FROM receipts").fetchone()[0],
            "outbox": dict(con.execute(
                "SELECT state, COUNT(*) FROM outbox GROUP BY state")),
        }
    finally:
        con.close()


def _checkpoint(ruta: pathlib.Path) -> None:
    con = sqlite3.connect(str(ruta))
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()


def _abre_journal_10(ruta: pathlib.Path) -> "C.Journal":
    return C.Journal(str(ruta), pepper=arnes.PEPPER, lane_ledgers=arnes.LANES,
                     recipient_resolver=arnes.censo, grammar=arnes.GRAMATICA,
                     clock=time.time)


def _escribe_y_drena_10(j, prefijo: str, n: int, *, prebound: bool = False) -> list:
    cred = "cred-demo-escritor-10"
    if not prebound:
        j.bind_credential(cred, principal="demo-escritor-10", role="be",
                          lane=CARRIL, capabilities=arnes.CAPS_RUNTIME)
    s = j.open_session(cred, ttl_s=900)
    ids = []
    for i in range(n):
        ev = j.accept_event(s.token, idempotency_key=f"{prefijo}-{i}",
                            intent=arnes.INTENT, ledger=CARRIL)
        ids.append(ev.event_id)
        job = j.claim_outbox(s.token)
        j.mark_materialized(s.token, ev.event_id, entry_eid="e" * 64,
                            ledger=CARRIL, claim_token=job.claim_token)
    return ids


def _fase_ida(ruta: pathlib.Path, conteos_v6: dict) -> tuple:
    """Kernel 1.0: migra 6→7, retiene fotografía, opera v7 de verdad y sella.

    Devuelve (journal, snapshot, token_operador, ids_v7, conteos_tras_ida).
    """
    j = _abre_journal_10(ruta)
    j.initialize()
    snapshot = j.migration_snapshot()
    if snapshot is None:
        j.close()
        raise DemoFalsada(
            "initialize() sobre v6 no dejó fotografía pre-v7: la vuelta sería "
            "imposible y el objetivo NO está demostrado")
    if snapshot.source_durable_v != 6:
        j.close()
        raise DemoFalsada(
            f"la fotografía acredita source_durable_v={snapshot.source_durable_v}, "
            f"no 6: no es la base que escribió el ancla")
    if not pathlib.Path(snapshot.path).exists():
        j.close()
        raise DemoFalsada(f"la fotografía retenida no existe: {snapshot.path}")
    if j.stored_durable_v() != 7:
        j.close()
        raise DemoFalsada("tras initialize() la base no declara durable_v=7")
    tras_ida = _conteos(ruta)
    for tabla in ("events", "receipts", "outbox"):
        if tras_ida[tabla] != conteos_v6[tabla]:
            j.close()
            raise DemoFalsada(
                f"la migración NO preservó {tabla}: v6={conteos_v6[tabla]} "
                f"v7={tras_ida[tabla]}")
    # Operación 1.0 real: barrera, escritura, drenado — y luego el sello que
    # `restore_pre_v7_snapshot` exige (sealed + pending/failed en cero).
    # Credential binding advances the session generation map and invalidates
    # already-issued tokens. Bind both principals before opening either
    # session; the writer session is opened after the admission operator, so
    # the authenticated operator token remains usable for the terminal seal.
    j.bind_credential("cred-demo-escritor-10", principal="demo-escritor-10",
                      role="be", lane=CARRIL,
                      capabilities=arnes.CAPS_RUNTIME)
    admisiones = arnes.abre_admision(j, lanes={CARRIL: arnes.LANES[CARRIL]})
    ids_v7 = _escribe_y_drena_10(j, "demo-v7", 1, prebound=True)
    token_operador = admisiones[CARRIL]
    for verbo in C.ADMISSION_VERBS:
        # `seal` is terminal and only legal from `closed`; close each door
        # first, then carry the epoch returned by that authenticated transition
        # into the irreversible seal.
        cerrado = j.close_admission(
            token_operador, verbo, reason_code=RAZON_ROLLBACK,
            expected_epoch=j.admission(token_operador, verbo).epoch)
        j.seal_admission(token_operador, verbo, reason_code=RAZON_ROLLBACK,
                         expected_epoch=cerrado.epoch)
    return j, snapshot, token_operador, ids_v7, tras_ida


def ejecuta_demo(ruta_db, *, worktree_ancla=None, modo_ancla: str = "git",
                 eventos_v6: int = 3, conservar_ancla: bool = False) -> dict:
    ruta_db = pathlib.Path(ruta_db).resolve()
    if ruta_db.exists():
        raise DemoNoEjecutable(
            f"la base de evidencia ya existe ({ruta_db}): igual que el banco, "
            f"nada se reusa")
    falsador_db = ruta_db.with_name(ruta_db.name + ".v7-para-falsador")
    if falsador_db.exists():
        raise DemoNoEjecutable(
            f"la copia del falsador ya existe ({falsador_db}): nada se recicla")
    if modo_ancla not in MODO_ANCLA:
        raise DemoNoEjecutable(
            f"modo_ancla desconocido: {modo_ancla!r} — válidos: "
            f"{', '.join(MODO_ANCLA)}")
    temporal = None
    ancla_creada = False
    j = None
    if modo_ancla == "oci":
        if worktree_ancla is None:
            raise DemoNoEjecutable(
                "el modo oci exige --worktree-ancla: la extracción de la "
                "imagen ancla es EXTERNA y nunca se crea implícitamente")
        raiz_ancla = pathlib.Path(worktree_ancla).resolve()
        if not raiz_ancla.is_dir():
            raise DemoNoEjecutable(
                f"el checkout externo no existe ({raiz_ancla}): el modo oci "
                f"no crea nada — se le pasa el directorio ya extraído")
    elif modo_ancla == "arbol":
        if worktree_ancla is not None:
            raise DemoNoEjecutable(
                "el modo arbol corre sobre el fixture bench/ancla-v09 de ESTE "
                f"árbol ({_RAIZ / ANCLA_V09_RUTA}): no acepta "
                "--worktree-ancla — nada se redirige en silencio")
        raiz_ancla = _RAIZ / ANCLA_V09_RUTA
    elif worktree_ancla is None:
        temporal = pathlib.Path(tempfile.mkdtemp(prefix="llminbox-ancla-09-"))
        raiz_ancla = temporal / "ancla"
    else:
        raiz_ancla = pathlib.Path(worktree_ancla).resolve()
    reporte: dict = {
        "ancla_sha": RAIZ_ANCLA_SHA,
        "ancla_oci_archivo_sha256": ANCLA_OCI_ARCHIVO_SHA256,
        "archivo_oci_verificado": False,
        "extremo_oci_ejecutado": False,
        "ancla_ejecucion": ("codigo_extraido_en_host" if modo_ancla == "oci"
                            else "fixture_ancla_v09_en_host"
                            if modo_ancla == "arbol"
                            else "worktree_git_en_host"),
        "ancla_modo": modo_ancla,
        "carril": CARRIL,
    }
    try:
        if modo_ancla == "git":
            _worktree_ancla(raiz_ancla)
            ancla_creada = True
            reporte["ancla_identidad"] = (
                f"git worktree add --detach {RAIZ_ANCLA_SHA}")
        elif modo_ancla == "arbol":
            reporte["ancla_identidad"] = _identidad_ancla_arbol(raiz_ancla)
        else:
            reporte["ancla_identidad"] = _identidad_ancla_oci(raiz_ancla)
        reporte["worktree_ancla"] = str(raiz_ancla)

        # FASE 0 · v6 ESCRITA POR EL ANCLA
        fase0 = _corre_ancla("escribe_v6", ruta_db, raiz_ancla, eventos_v6)
        if fase0["conteos"]["durable_v"] != 6:
            raise DemoFalsada(
                f"el ancla escribió durable_v={fase0['conteos']['durable_v']}, "
                f"no 6: el insumo de la demo no es el ancla 0.9")
        reporte["fase_0_v6_del_ancla"] = {
            "conteos": fase0["conteos"],
            "eventos": len(fase0["eventos"]),
        }

        # FASE IDA · kernel 1.0 migra, retiene, opera, sella
        j, snapshot, token_operador, ids_v7, tras_ida = _fase_ida(
            ruta_db, fase0["conteos"])
        reporte["ida"] = {
            "durable_v": tras_ida["durable_v"],
            "fotografia": {
                "sha256": snapshot.sha256,
                "source_durable_v": snapshot.source_durable_v,
                "path_existe": True,
            },
            "conteos_preservados": True,
            "operacion_v7": {"eventos_aceptados": len(ids_v7)},
        }

        # FALSADOR ⊖ · los bytes v7, SIN restaurar, al ancla
        _checkpoint(ruta_db)
        shutil.copyfile(ruta_db, falsador_db)
        veredicto = _corre_ancla("rechaza_v7", falsador_db, raiz_ancla, 0)
        if not veredicto.get("rechazado"):
            raise DemoFalsada(
                "el falsador del instrumento no discrimina: el ancla 0.9 "
                "ACEPTÓ una base v7 — ni este falsador ni el rollback "
                f"prueban nada (respuesta: {veredicto})")
        if veredicto.get("excepcion") != "SchemaTooNew":
            raise DemoFalsada(
                "el ancla rechazó, pero no con SchemaTooNew: "
                f"{veredicto.get('excepcion')} — revisar antes de fiarse")
        if "durable_v=7" not in veredicto.get("mensaje", ""):
            raise DemoFalsada(
                "SchemaTooNew sin nombrar la versión: "
                f"{veredicto.get('mensaje')!r}")
        reporte["falsador_instrumento"] = {
            "copia_v7": str(falsador_db),
            "rechazado": True,
            "excepcion": veredicto["excepcion"],
            "mensaje": veredicto["mensaje"],
        }

        # FASE VUELTA · restauración certificada del kernel. Si el mecanismo
        # RECHAZA restaurar, el rollback empezó y falló ⇒ falsada (rc 1),
        # nunca no-ejecutable (taxonomía @qa 20:31Z).
        try:
            restaurado = j.restore_pre_v7_snapshot(
                token_operador, expected_sha256=snapshot.sha256)
        except C.JournalError as e:
            raise DemoFalsada(
                "el mecanismo de rollback RECHAZÓ restaurar esta base: el "
                f"rollback empezó y falló ⇒ falsada: {e}") from e
        if restaurado.sha256 != snapshot.sha256:
            raise DemoFalsada("el snapshot devuelto no es el solicitado")
        sha_base = _sha256_fichero(ruta_db)
        if sha_base != snapshot.sha256:
            raise DemoFalsada(
                "los bytes en disco no son la fotografía: "
                f"{sha_base} != {snapshot.sha256}")
        conteos_vuelta = _conteos(ruta_db)
        if conteos_vuelta["durable_v"] != 6:
            raise DemoFalsada(
                f"restaurada declara durable_v={conteos_vuelta['durable_v']}")
        for tabla in ("events", "receipts", "outbox"):
            if conteos_vuelta[tabla] != tras_ida[tabla]:
                raise DemoFalsada(
                    f"la restauración no devolvió {tabla}: "
                    f"fotografía={tras_ida[tabla]} "
                    f"restaurada={conteos_vuelta[tabla]}")

        # EXTREMO OPERATIVO · el ancla 0.9 re-abre y ESCRIBE sobre v6. Su
        # muerte ES evidencia contra el rollback ⇒ falsada, no arranque.
        post = _corre_ancla("reabre_v6", ruta_db, raiz_ancla, 1,
                            si_muere="falsada")
        if post["conteos"]["durable_v"] != 6:
            raise DemoFalsada(
                f"la reapertura del ancla vio durable_v="
                f"{post['conteos']['durable_v']}")
        esperados = tras_ida["events"] + 1
        if post["conteos"]["events"] != esperados:
            raise DemoFalsada(
                f"el ancla no escribió sobre la restaurada: events="
                f"{post['conteos']['events']}, esperados {esperados}")
        reporte["vuelta"] = {
            "mecanismo": "restore_pre_v7_snapshot",
            "sha256_base": sha_base,
            "durable_v": conteos_vuelta["durable_v"],
            "ancla_reabrio_y_escribio": True,
            "eventos_tras_reapertura": post["conteos"]["events"],
        }

        # PÉRDIDA POR DISEÑO, declarada y verificada ausente
        con = sqlite3.connect(f"file:{ruta_db}?mode=ro", uri=True)
        try:
            presente = con.execute(
                "SELECT COUNT(*) FROM events WHERE event_id=?",
                (ids_v7[0],)).fetchone()[0]
        finally:
            con.close()
        if presente:
            raise DemoFalsada(
                "el evento v7 post-fotografía SOBREVIVIÓ al rollback: la "
                "restauración no devolvió la fotografía exacta")
        reporte["perdido_post_snapshot"] = {
            "event_id": ids_v7[0],
            "presente_en_restaurada": False,
            "por_diseno": ("ADR-001 forward-only: el rollback ES la "
                           "restauración de la fotografía; no hay down"),
        }

        reporte["veredicto"] = "DEMO_OK"
        return reporte
    finally:
        if j is not None:
            try:
                j.close()
            except Exception:
                pass  # tras restore la instancia queda NUEVO; nada que sellar
        if temporal is not None and not conservar_ancla:
            limpieza = _retira_ancla_temporal(
                temporal, raiz_ancla, creada=ancla_creada)
            reporte["limpieza_ancla"] = limpieza
            if not limpieza["completada"]:
                print("AVISO: limpieza del ancla temporal pendiente; "
                      "el directorio se conserva para inspección", file=sys.stderr)
        if temporal is not None and conservar_ancla:
            # sólo se «conserva» lo que nosotros creamos; un checkout externo
            # (pasado o oci) no era nuestro y no se toca
            reporte["worktree_ancla_conservado"] = str(raiz_ancla)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Demo IDA y VUELTA de la migración Journal v6→v7 con "
                    "extremo operativo 0.9 (encargo «migración y rollback "
                    "DEMOSTRADOS»)")
    p.add_argument("--db", required=True,
                   help="ruta NUEVA de la base de evidencia (no debe existir)")
    p.add_argument("--eventos-v6", type=int, default=3,
                   help="eventos que escribe el ancla en la fase 0")
    p.add_argument("--worktree-ancla", default=None,
                   help="modo git: destino NUEVO donde crear el worktree del "
                        "ancla; un destino explícito se conserva. modo oci "
                        "(obligatorio ahí): directorio EXISTENTE de código "
                        "extraído; se compara con el SHA y se ejecuta en el "
                        "HOST, sin certificar la imagen. Por defecto (git) se "
                        "crea un worktree temporal y se retira sólo ese worktree")
    p.add_argument("--modo-ancla", choices=MODO_ANCLA, default="git",
                   help="git (defecto): descolga un worktree NUEVO en el SHA "
                        "certificado. oci: usa código EXISTENTE extraído y lo "
                        "ejecuta en el host. arbol: corre sobre el fixture "
                        "bench/ancla-v09 de este árbol, verificado por "
                        "contenido contra hashes fijados en el código — el "
                        "modo no se infiere de si la ruta existe")
    p.add_argument("--conservar-ancla", action="store_true",
                   help="no limpiar el worktree temporal (inspección)")
    p.add_argument("--salida", default=None,
                   help="escribe además el reporte JSON en esta ruta")
    a = p.parse_args(argv)
    if a.modo_ancla == "oci" and not a.worktree_ancla:
        p.error("--modo-ancla oci exige --worktree-ancla (checkout ya extraído)")
    if a.modo_ancla == "arbol" and a.worktree_ancla:
        p.error("--modo-ancla arbol no acepta --worktree-ancla (corre sobre "
                "el fixture bench/ancla-v09 de este árbol)")
    try:
        reporte = ejecuta_demo(
            a.db, worktree_ancla=a.worktree_ancla, modo_ancla=a.modo_ancla,
            eventos_v6=a.eventos_v6, conservar_ancla=a.conservar_ancla)
    except DemoFalsada as e:
        print(f"DEMO_FALSADA: {e}", file=sys.stderr)
        return 1
    except DemoNoEjecutable as e:
        print(f"DEMO_NO_EJECUTABLE: {e}", file=sys.stderr)
        return 2
    texto = json.dumps(reporte, indent=2, ensure_ascii=False, sort_keys=True)
    print(texto)
    if a.salida:
        pathlib.Path(a.salida).write_text(texto + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
