#!/usr/bin/env python3
"""BANCO G10 — envolvente acotada de flota para llminbox v1.0.

⛔ ESTO NO ES UN TEST Y NO SE COLECTA CON `pytest`, por el mismo motivo que
`bench/m2_bench.py` (`docs/M2-CI-CHECKLIST.md`): un banco MIDE, una suite
DECIDE. Los falsadores rápidos que SÍ tiene que pasar CI viven en
`tests/scale/`; esto es el entregable de la envolvente medida que
`docs/V1.0-EXECUTION.md` pide para G10:

    "G10 · Bounded fleet operation. The supported beta envelope is measured,
    not implied: sixteen agents, declared ledger size, concurrency, latency,
    memory and recovery thresholds. A truncated or unmeasured result is
    reported as such."

> 🔧 **CORREGIDO 2026-09-08** (db-mig; confirma el claim del gate board
> «benchmark semantics unsound»): **D1** — `fugas_cross_lane` medía la ACL
> misma: con ambas lanes autorizadas sobre el MISMO ledger, devolver filas
> a B era el comportamiento correcto y el umbral `0` era insatisfacible por
> construcción. La frontera ahora se mide contra un ledger FUERA de la ACL de B
> (`l-ajeno`): se exige LaneNotAuthorized y se cuentan filas si indebidamente
> retorna. Los controles ⊕ exigen A viendo el ajeno y B viendo el propio. **D2** — el
> RSS de los workers de flota no se medía (`RUSAGE_SELF` sólo ve el proceso
> del banco); ahora se acredita el pico de los hijos (`RUSAGE_CHILDREN`) Y
> una cota superior de flota (cada worker reporta su pico; se suma:
> `rss_flota_cota_superior_gib`), porque `ru_maxrss` es un MÁXIMO por hijo y no
> responde a «cuánta memoria usa la flota». **D3 (mismo día, encargo del
> operador)** — la flota nacía con la barrera de admisión CERRADA y ligaba/
> emitía sesiones intercaladas: cada ligadura mataba la sesión anterior
> (generación), así que el banco habría medido 16 AuthError, no 16 agentes.
> Ahora: todas las ligaduras → todas las sesiones → barrera abierta como
> operador (patrón `abre_admision` del arnés). Los workers de concurrencia
> ejercitan transiciones del kernel con hashes sintéticos de metadatos:
> eso no acredita proyección. La fase de recovery usa MarkdownProjector,
> comprueba bytes, hash y offset reales, y exige replay sin segunda escritura.
> El rc del worker de recovery se exige == 137 (sólo eso es la caída simulada).
> **D4 (mismo día, humo 2000/16/5)** — el banco pasaba UN `--db` a SearchStore
> Y Journal: el manifiesto v7 del Journal es dueño EXCLUSIVO de su fichero
> (`exact: True`), así que la fase de concurrencia crasheaba en
> `Journal.initialize()` con `SchemaIndeterminate: tablas sin 'meta'` — el
> CONTRATO funcionando, no una avería del kernel. El Journal ahora abre su
> fichero hermano (`<db>.journal.sqlite`), fresco y distinto por guarda
> (`_prepara_db_journal`); los workers no cambian (lo reciben por argv de sus
> fases). El crash NO_CANONICO esperado del humo (rc=1 por umbral) y un crash
> de instrumento (rc=2 `NO_EJECUTABLE`) vuelven a ser cosas distintas.

## Qué mide, y con qué instrumento

- **Ledger grande + keyset + memoria + latencia**: un corpus determinista en
  `search_store.SearchStore` (mismo linaje que `bench/m2_bench.py`: semilla +
  digest, FTS5 obligatorio, sin `LIKE`), con recall por agujas plantadas,
  paginación keyset completa y límites de disco/RSS.
- **Concurrencia + aislamiento por lane**: relanza LOS MISMOS agentes que
  `tests/scale/_worker_carriles_colisionan.py` ejercitan a escala focal —no
  una segunda implementación del mismo verbo—, con `--agentes` (por defecto
  16, el envite de G10) repartidos en las DOS lanes colisionantes. La
  frontera de BÚSQUEDA se mide aparte: B contra un ledger fuera de su ACL
  (⊖, exige 0) y B contra su ledger propio (⊕, exige ≥1) — un cero sin el
  control positivo sería indistinguible de «no hay datos».
- **Recovery**: relanza `tests/scale/_worker_recovery_caida.py` sobre el
  fichero de Journal del banco — hermano del corpus y SEPARADO de él por
  contrato (D4: el manifiesto v7 es dueño exclusivo de su fichero) —, tras
  drenar su cola previa. Mide hasta la
  materialización real en un ledger conservado como evidencia; no declara
  indexación. El evento y su recibo sobreviven al replay y hay una sola entrada.

Reusar los mismos workers que los falsadores focales, en vez de escribir una
segunda versión "de banco", es deliberado: la evidencia focal (rápida, en
CI) y la evidencia de escala (cara, gateada) miden el MISMO camino de
código, no dos que puedan divergir en silencio.

## Lo que este banco se NIEGA a hacer

- **No certifica en host saturado.** Mide `loadavg1/núcleo` antes de
  arrancar y se niega a declarar "canónico" por encima del umbral — la MISMA
  disciplina que `bench/m2_bench.py`, para que la puerta de capacidad de
  `CFO Guardian` (execution doc, "Capacity gate") tenga algo objetivo que
  leer en vez de la palabra de quien corrió el banco.
- **No finge escala.** `--agentes` por debajo de 16 o `--filas` por debajo
  de lo canónico marcan la corrida `NO_CANONICA` en el veredicto; el número
  se publica igual, con su etiqueta.
- **No agota memoria del host a ciegas.** Un `--filas` que no es el
  canónico existe para probar el arnés en segundos, no para simular el
  millón.

## Cómo se corre

    # arnés, sin nada más en la máquina (segundos)
    python3 bench/scale_envelope_bench.py --filas 2000 --agentes 4 --salida /tmp/humo.json

    # EVIDENCIA canónica de G10 (minutos; requiere GO de capacidad)
    python3 bench/scale_envelope_bench.py --filas 1000000 --agentes 16 \\
        --salida bench/resultados/g10-envelope.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import platform
import random
import resource
import sqlite3
import subprocess
import sys
import time

RAIZ = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

import search_contract as sc                                # noqa: E402
import search_cursor as scur                                # noqa: E402
import search_store as ss                                   # noqa: E402

WORKERS_DIR = RAIZ / "tests" / "scale"
WORKER_CARRILES = "_worker_carriles_colisionan.py"
WORKER_RECOVERY = "_worker_recovery_caida.py"

FILAS_CANONICAS = 1_000_000
AGENTES_CANONICOS = 16
SEMILLA_POR_DEFECTO = 20260907

CARRIL_A, CARRIL_B = "carril-uno", "carril-dos"
LANE_LEDGERS = {CARRIL_A: ["l"], CARRIL_B: ["l"]}
LEDGER = "l"
LEDGER_AJENO = "l-ajeno"    # existe en el índice, FUERA de la ACL de CARRIL_B
ACL = {CARRIL_A: {LEDGER, LEDGER_AJENO}, CARRIL_B: {LEDGER}}
AGUJA_RARA = "zzenvolvente"
RARAS_N = 2_000
AJENAS_N = 500              # agujas plantadas SÓLO en el ledger ajeno
MEDIA_CADA = 97

UMBRALES = {
    "p95_ms_busqueda": 200.0,
    "recall_minimo": 1.0,
    "fugas_cross_lane": 0,
    "visibilidad_B_ledger_propio_min": 1,
    "visibilidad_A_ledger_ajeno_min": 1,
    "paginacion_duplicados": 0,
    "paginacion_huecos": 0,
    "claims_cruzados_de_lane": 0,
    "recovery_segundos_max": 10.0,
    "rss_pico_max_gib": 3.0,
    "rss_pico_hijos_max_gib": 3.0,
    # Cota superior: Σ de picos individuales; no mide el pico simultáneo.
    # Cota inicial 8 GiB (≈16 × 0,5 GiB), PENDIENTE de calibrar con la
    # primera corrida real — turno @sdet; el número se ajusta con evidencia.
    "rss_flota_max_gib": 8.0,
}
CARGA_MAXIMA_POR_NUCLEO = 0.60


class BancoNoEjecutable(RuntimeError):
    """El host o el arnés no pueden producir una medida válida ahora mismo."""


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=RAIZ, capture_output=True,
                              text=True, timeout=30).stdout.strip()
    except Exception as e:                                  # pragma: no cover
        return f"<git falló: {e}>"


def _carga() -> dict:
    uno, cinco, quince = os.getloadavg()
    nucleos = os.cpu_count() or 1
    return {"loadavg1": uno, "loadavg5": cinco, "loadavg15": quince,
            "nucleos": nucleos, "por_nucleo": uno / nucleos}


def _exige_fts5() -> None:
    con = sqlite3.connect(":memory:")
    try:
        con.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
    except sqlite3.OperationalError as e:
        raise BancoNoEjecutable(f"SQLite sin FTS5 ({e}): no mido con LIKE") from None
    finally:
        con.close()


def _entorno() -> dict:
    _exige_fts5()
    diff = _git("diff", "HEAD")
    return {
        "git_sha": _git("rev-parse", "HEAD"),
        "git_rama": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "git_limpio": _git("status", "--porcelain") == "",
        "git_diff_sha256": hashlib.sha256(diff.encode()).hexdigest(),
        "python": sys.version.split()[0],
        "sqlite_runtime": sqlite3.sqlite_version,
        "host": {"sistema": platform.platform(), "maquina": platform.machine(),
                 "nucleos": os.cpu_count(), "nombre": platform.node()},
        "bench_sha256": hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),
        "workers_sha256": {
            f: hashlib.sha256((WORKERS_DIR / f).read_bytes()).hexdigest()
            for f in (WORKER_CARRILES, WORKER_RECOVERY)},
    }


# ── Corpus determinista (mismo linaje que bench/m2_bench.py) ──────────────
def _filas(semilla: int, n: int):
    rng = random.Random(semilla)
    raras = set(rng.sample(range(n), min(RARAS_N, n)))
    for i in range(n):
        eid = hashlib.sha256(f"{semilla}:{i}".encode()).hexdigest()
        titular = f"titular {eid[:12]}"
        # entries.body contiene la entrada completa, incluido su titular.
        # El rebuild rechaza titulares ausentes del cuerpo para no perderlos.
        cuerpo = f"{titular}\nrelleno {i} " + " ".join(
            rng.choice(("ledger", "cursor", "aislamiento", "envolvente", "recovery"))
            for _ in range(12))
        if i in raras:
            cuerpo += f" {AGUJA_RARA}"
        if i % MEDIA_CADA == 0:
            cuerpo += " zzmediaenvolvente"
        yield {"eid": eid, "arrival": i + 1, "head": titular, "cuerpo": cuerpo}


def _manifiesto_corpus(semilla: int, n: int) -> dict:
    h = hashlib.sha256()
    raras = 0
    for fila in _filas(semilla, n):
        h.update(json.dumps(fila, sort_keys=True).encode())
        h.update(b"\x00")
        if AGUJA_RARA in fila["cuerpo"]:
            raras += 1
    return {"semilla": semilla, "filas": n, "digest_sha256": h.hexdigest(),
            "agujas_rara": raras}


ESQUEMA_ENTRIES = """
CREATE TABLE entries (
  ledger TEXT NOT NULL, eid TEXT NOT NULL,
  arrival INTEGER, seq INTEGER, line_no INTEGER, byte_off INTEGER,
  ts TEXT, actor TEXT, tipo TEXT, head TEXT, body TEXT,
  visto TEXT, ausente TEXT, provisional INTEGER DEFAULT 0,
  PRIMARY KEY (ledger, eid));
CREATE INDEX i_arr ON entries(ledger, arrival);
"""


def _construye_indice(ruta: pathlib.Path, semilla: int, n: int) -> tuple:
    if ruta.exists():
        raise BancoNoEjecutable("la base de evidencia ya existe: use --db único")
    con = sqlite3.connect(str(ruta))
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(ESQUEMA_ENTRIES)
    t0 = time.perf_counter()
    buf = []
    for fila in _filas(semilla, n):
        buf.append((LEDGER, fila["eid"], fila["arrival"], "2026-09-07", "backend", "FYI",
                    fila["head"], fila["cuerpo"]))
        if len(buf) >= 20_000:
            con.executemany("INSERT INTO entries(ledger,eid,arrival,ts,actor,tipo,head,"
                            "body) VALUES(?,?,?,?,?,?,?,?)", buf)
            buf.clear()
    if buf:
        con.executemany("INSERT INTO entries(ledger,eid,arrival,ts,actor,tipo,head,body)"
                        " VALUES(?,?,?,?,?,?,?,?)", buf)
    # Ledger ajeno: agujas que CARRIL_B NO tiene en su ACL. Es el objeto del
    # ⊖ de aislamiento; sin él, el umbral de fuga no tiene contra qué medir.
    con.executemany(
        "INSERT INTO entries(ledger,eid,arrival,ts,actor,tipo,head,body)"
        " VALUES(?,?,?,?,?,?,?,?)",
        [(LEDGER_AJENO, hashlib.sha256(f"{semilla}:ajeno:{i}".encode("utf-8")).hexdigest(), i + 1,
          "2026-09-07", "backend", "FYI", f"titular ajeno {i}",
          f"titular ajeno {i}\nrelleno ajeno {i} {AGUJA_RARA}") for i in range(AJENAS_N)])
    con.commit()
    segundos_poblar = time.perf_counter() - t0

    st = ss.SearchStore(con, cursor_key=b"banco-g10-clave-de-cursor-32b!!!", acl=ACL)
    st.ensure_schema()
    st.set_acl(ACL)
    t1 = time.perf_counter()
    gen = st.rebuild()
    return st, con, {"segundos_poblar": round(segundos_poblar, 3),
                     "segundos_rebuild": round(time.perf_counter() - t1, 3),
                     "agujas_ajenas": AJENAS_N, "generacion": gen}


def _percentil(muestras, p):
    if not muestras:
        return float("nan")
    orden = sorted(muestras)
    k = (len(orden) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(orden) - 1)
    return orden[lo] + (orden[hi] - orden[lo]) * (k - lo)


def _latencia_y_recall(st, manifiesto: dict, repeticiones: int) -> dict:
    st.search(lane=CARRIL_A, ledger=LEDGER, query=AGUJA_RARA, limit=20)  # calentamiento
    muestras = []
    for _ in range(repeticiones):
        t0 = time.perf_counter()
        st.search(lane=CARRIL_A, ledger=LEDGER, query=AGUJA_RARA, limit=20)
        muestras.append((time.perf_counter() - t0) * 1000.0)

    vistos, dups, paginas, cursor, previo, orden_ok = [], 0, 0, None, None, True
    while True:
        r = st.search(lane=CARRIL_A, ledger=LEDGER, query=AGUJA_RARA, limit=sc.MAX_LIMIT,
                      cursor=cursor)
        paginas += 1
        if paginas > 10_000:
            raise BancoNoEjecutable("la paginación de la envolvente no termina")
        for f in r["filas"]:
            clave = (f["arrival"], f["eid"])
            if previo is not None and not (clave < previo):
                orden_ok = False
            previo = clave
            vistos.append(f["eid"])
        if not r["hay_mas"]:
            break
        cursor = r["cursor"]
    dups = len(vistos) - len(set(vistos))

    # ⊖ frontera de aislamiento: B busca en `LEDGER_AJENO`, que NO está en su
    # ACL. SearchStore rechaza con LaneNotAuthorized ANTES de buscar; no
    # devuelve una página vacía. Sólo esa excepción acredita la denegación.
    # Otros errores siguen siendo errores del banco, nunca «cero fugas».
    try:
        ajena = st.search(lane=CARRIL_B, ledger=LEDGER_AJENO,
                         query=AGUJA_RARA, limit=sc.MAX_LIMIT)
    except ss.LaneNotAuthorized:
        ajena = {"filas": []}
        denegada_B = True
    else:
        denegada_B = False
    # El corpus ajeno debe existir también para una lectura autorizada; una
    # denegación sin datos que proteger no basta para medir esta frontera.
    ajena_A = st.search(lane=CARRIL_A, ledger=LEDGER_AJENO,
                       query=AGUJA_RARA, limit=1)
    # ⊕ control positivo: B sobre SU ledger autorizado ("l") SÍ devuelve
    # agujas. Sin este control, el cero de arriba sería indistinguible de
    # "B no ve nada por construcción" (el 0 que no dice si falta el consumo
    # o el dato).
    propia_B = st.search(lane=CARRIL_B, ledger=LEDGER, query=AGUJA_RARA, limit=5)
    esperados = manifiesto["agujas_rara"]
    return {
        "p50_ms": round(_percentil(muestras, 0.50), 3),
        "p95_ms": round(_percentil(muestras, 0.95), 3),
        "recall": (len(set(vistos)) / esperados) if esperados else None,
        "esperados": esperados, "encontrados": len(set(vistos)),
        "paginacion_duplicados": dups,
        "paginacion_huecos": max(0, esperados - len(set(vistos))),
        "orden_estricto_desc": orden_ok,
        "fugas_cross_lane_en_B": len(ajena["filas"]),
        "acceso_B_ledger_ajeno_denegado": denegada_B,
        "visibilidad_A_ledger_ajeno": len(ajena_A["filas"]),
        "visibilidad_B_ledger_propio": len(propia_B["filas"]),
    }


def _limites(ruta: pathlib.Path) -> dict:
    bytes_db = sum(f.stat().st_size for f in
                   [ruta, ruta.with_name(ruta.name + "-wal"),
                    ruta.with_name(ruta.name + "-shm")] if f.exists())

    def _gib(ru_maxrss: int) -> float:
        return round((ru_maxrss if sys.platform == "darwin" else ru_maxrss * 1024)
                     / 2 ** 30, 4)

    # SELF cubre el corpus/búsqueda (in-process); CHILDREN el pico de UN solo
    # hijo (ru_maxrss es un MÁXIMO, no una suma: no responde a «cuánta usa la
    # flota»). La suma de picos propios da una COTA SUPERIOR; los picos pueden
    # ocurrir en momentos distintos. En main se llama DOS
    # veces: antes de la flota los hijos aún no existen (≈0); después, la
    # segunda llamada actualiza el pico real de los agentes.
    return {"tamano_db_gib": round(bytes_db / 2 ** 30, 4),
            "rss_pico_gib": _gib(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss),
            "rss_pico_hijos_gib":
                _gib(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)}


# ── Fichero del Journal: hermano de la db de búsqueda, POR CONTRATO ───────
# El manifiesto v7 del Journal es dueño EXCLUSIVO de su fichero (v7 lleva
# `exact: True` en coordination.py): una base con tablas que no son suyas se
# rechaza con SchemaIndeterminate — compartir el `--db` entre SearchStore y
# Journal no era «dos schemas en una base», era un crash garantizado, y el
# crash del humo 2026-09-08 fue el contrato funcionando. La derivación vive
# en su propia función para que la guarda sea falsable (ver tests/scale/).
def _deriva_db_journal(db: pathlib.Path) -> pathlib.Path:
    return db.resolve().with_suffix(".journal.sqlite")


def _prepara_db_journal(db: pathlib.Path) -> pathlib.Path:
    """Hermano FRESCO y distinto de `db`: ni colisión de nombre ni reúso."""
    db = db.resolve()
    destino = _deriva_db_journal(db)
    if destino == db:
        raise BancoNoEjecutable(
            f"el Journal y la búsqueda no pueden compartir el mismo fichero "
            f"({db}): el manifiesto v7 es dueño exclusivo de su base y el "
            f"banco mediría su propio crash, no la envolvente")
    if destino.exists():
        raise BancoNoEjecutable(
            f"la base de evidencia del Journal ya existe ({destino}): use "
            f"--db único, igual que exige la base de búsqueda")
    return destino


# ── Fase de concurrencia + recovery: RELANZA los mismos workers que los
# falsadores focales de tests/scale/, a la escala que pida --agentes. ──────
def _lanza_agentes(db_journal: str, tokens_por_carril: dict, n_eventos: int,
                   n_claims: int, clave_compartida: str) -> list[dict]:
    env = {**os.environ, "LLMINBOX_RAIZ": str(RAIZ),
          "LLMINBOX_PEPPER": "pepper-de-pruebas-no-es-un-secreto-real"}
    procs = []
    try:
        for carril, tokens in tokens_por_carril.items():
            for token in tokens:
                procs.append(subprocess.Popen(
                    [sys.executable, str(WORKERS_DIR / WORKER_CARRILES), db_journal,
                     token, carril,
                     clave_compartida, str(n_eventos), str(n_claims)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env))
        salidas = []
        limite = time.monotonic() + 180
        for p in procs:
            out, err = p.communicate(timeout=max(0.01, limite - time.monotonic()))
            if p.returncode != 0:
                raise BancoNoEjecutable(f"agente murió rc={p.returncode}: {err}")
            lineas = [l for l in out.strip().splitlines() if l.strip()]
            if not lineas:
                raise BancoNoEjecutable("agente terminó sin reporte")
            salidas.append(json.loads(lineas[-1]))
        return salidas
    finally:
        # Sólo hijos creados por ESTE banco; un timeout no deja carga huérfana.
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.communicate()


def _cota_rss_flota(reportes: list[dict]) -> float | None:
    """Sin un pico válido por hijo no se conoce siquiera la cota superior."""
    valores = [r.get("rss_bytes") for r in reportes]
    if not valores or any(type(v) is not int or v <= 0 for v in valores):
        return None
    return sum(valores) / 2 ** 30


def _fase_concurrencia_y_aislamiento(db_journal: str, n_agentes: int) -> dict:
    import coordination as C
    from tests.journal._arnes import (ADMISION, CAPS_RUNTIME, GRAMATICA,
                                      PREFIJO_ADMISION, censo)

    por_carril = n_agentes // 2
    j = C.Journal(db_journal, pepper=b"pepper-de-pruebas-no-es-un-secreto-real",
                 lane_ledgers=LANE_LEDGERS, recipient_resolver=censo, grammar=GRAMATICA)
    j.initialize()
    # ① LIGADURAS — TODAS primero (runtime + operador de la puerta). Una
    # ligadura efectiva sube la generación global e invalida TODA sesión
    # abierta antes — lo documenta `tests/journal/_arnes.sesiones`. El bucle
    # que había aquí ligaba/emitía INTERCALADO: cada ligadura mataba la
    # sesión anterior, la flota arrancaba con tokens cadáver y el banco
    # medía su propia muerte, no contención.
    for carril in (CARRIL_A, CARRIL_B):
        for i in range(por_carril):
            j.bind_credential(f"g10-{carril}-{i}", principal=f"agente-{carril}-{i}",
                              role="be", lane=carril, capabilities=CAPS_RUNTIME)
        # La barrera de admisión nace CERRADA (ausencia = cierre) y
        # `accept_event`/`claim_outbox` la exigen ABIERTA
        # (`_exigir_admision_locked`): sin ligar al operador de la puerta,
        # cada worker moría en su primer accept_event. Mismas credenciales
        # que el arnés (`abre_admision`): prefijo propio, rol infra,
        # capacidad ADMISSION_OPERATOR — no cuelga del runtime estándar.
        j.bind_credential(f"{PREFIJO_ADMISION}-{carril}", principal=f"admision-{carril}",
                          role="infra", lane=carril, capabilities=ADMISION)
    # ② SESIONES — TODAS después de la última ligadura.
    tokens = {carril: [j.open_session(f"g10-{carril}-{i}", ttl_s=900).token
                       for i in range(por_carril)]
              for carril in (CARRIL_A, CARRIL_B)}
    token_admision = {carril: j.open_session(f"{PREFIJO_ADMISION}-{carril}",
                                             ttl_s=900).token
                      for carril in (CARRIL_A, CARRIL_B)}
    # ③ LA BARRERA se abre como operador — sesión + capacidad + epoch, el
    # acto completo, sin atajo por debajo (mismo patrón que `abre_admision`
    # del arnés; el `open -> open` es transición ilegal, se PREGUNTA antes).
    for carril in (CARRIL_A, CARRIL_B):
        for verbo in C.ADMISSION_VERBS:
            if j.admission(token_admision[carril], verbo).state == "open":
                continue
            j.open_admission(token_admision[carril], verbo, reason_code="ROLLOUT")
    j.close()

    t0 = time.perf_counter()
    reportes = _lanza_agentes(db_journal, tokens, n_eventos=10, n_claims=3,
                              clave_compartida="clave-compartida-g10")
    segundos_concurrencia = time.perf_counter() - t0

    claims_cruzados = sum(r["claims_carril_ajeno"] for r in reportes)
    errores = [e for r in reportes for e in r["errores"]]
    # No sumar como cero un worker sin medición ni ocultar un lease agotado.
    truncados = sum(bool(r.get("truncado")) for r in reportes)
    if truncados:
        errores.append(f"{truncados} agentes agotaron los reintentos de lease")
    if len(reportes) != n_agentes:
        errores.append("el número de reportes no coincide con la flota pedida")
    return {"n_agentes": len(reportes), "segundos": round(segundos_concurrencia, 3),
           "claims_cruzados_de_lane": claims_cruzados, "errores": errores,
           "tokens": tokens,
           "truncados": truncados,
           "alcance_claims": "transiciones del kernel; no acredita bytes proyectados",
           "rss_flota_cota_superior_gib": _cota_rss_flota(reportes)}


def _fase_recovery(db_journal: str, token: str, *, lane: str = CARRIL_A) -> dict:
    """Caída real y proyección a disco, con replay sin una segunda entrada.

    El trabajo de concurrencia pendiente se proyecta antes de la caída: así
    el único claim del hijo corresponde al evento que acaba de aceptar.
    Esta medida termina en materialized; no afirma haber ejecutado indexación.
    """
    import coordination as C
    import ledger_parse
    from projector import LedgerTarget, MarkdownProjector
    from tests.journal._arnes import GRAMATICA, INTENT, LANES, censo

    ruta = pathlib.Path(db_journal).resolve().with_suffix(".recovery-ledger.md")
    try:
        ruta.touch(exist_ok=False)
    except FileExistsError as exc:
        raise BancoNoEjecutable("recovery: el destino de evidencia ya existe") from exc
    target = LedgerTarget(lane=lane, ledger=LEDGER, path=str(ruta))
    frame_key = b"g10-frame-key-de-pruebas-no-secreto"

    def abrir():
        journal = C.Journal(db_journal, pepper=b"pepper-de-pruebas-no-es-un-secreto-real",
                            lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
        journal.initialize()
        return journal

    previo = abrir()
    preparadas = 0
    try:
        proyector = MarkdownProjector(previo, token, [target], frame_key=frame_key)
        while previo.unresolved_outbox(token):
            if preparadas >= 10_000 or proyector.project_next() is None:
                raise BancoNoEjecutable("recovery: no se pudo vaciar el trabajo previo")
            preparadas += 1
        bytes_antes = ruta.read_bytes()
    finally:
        previo.close()

    env = {**os.environ, "LLMINBOX_RAIZ": str(RAIZ),
          "LLMINBOX_PEPPER": "pepper-de-pruebas-no-es-un-secreto-real"}
    p = subprocess.run(
        [sys.executable, str(WORKERS_DIR / WORKER_RECOVERY), db_journal, token,
         "clave-recovery-g10"],
        capture_output=True, text=True, timeout=30, env=env)
    # SÓLO `137` es la caída simulada (`os._exit(137)` del worker). El código
    # anterior trataba CUALQUIER rc != 0 como «hubo caída»: un worker muerto
    # ANTES de tiempo (rc=1, p.ej. barrera cerrada) se colaba como corrida
    # válida y el banco medía un recovery de un accidente, no de la caída.
    if p.returncode != 137:
        raise BancoNoEjecutable(
            f"el worker de recovery terminó rc={p.returncode}; sólo 137 es la caída "
            f"simulada — cualquier otro rc es un fallo previo, no una caída que medir. "
            f"stderr: {p.stderr}")
    lineas = [l for l in p.stdout.strip().splitlines() if l.strip()]
    if not lineas:
        raise BancoNoEjecutable(
            f"recovery: el worker murió sin imprimir su JSON; stderr: {p.stderr}")
    datos = json.loads(lineas[-1])
    if datos.get("claimed_event_id") != datos.get("event_id"):
        raise BancoNoEjecutable("recovery: el hijo no reclamó su propio evento")
    if ruta.read_bytes() != bytes_antes:
        raise BancoNoEjecutable("recovery: hubo escritura antes del reinicio del proyector")

    t0 = time.perf_counter()
    time.sleep(1.4)               # el lease del worker (1s) tiene que vencer de verdad
    j2 = abrir()
    try:
        replay = j2.accept_event(token, idempotency_key="clave-recovery-g10",
                                 intent=INTENT, ledger=LEDGER)
        if (not replay.replayed or replay.event_id != datos["event_id"]
                or replay.receipt_id != datos["receipt_id"]):
            raise BancoNoEjecutable("recovery: replay no conserva evento y recibo")
        proyector = MarkdownProjector(j2, token, [target], frame_key=frame_key)
        resultado = proyector.project_next()
        if resultado is None or resultado.event_id != datos["event_id"]:
            raise BancoNoEjecutable("recovery: el proyector no drenó el evento de la caída")
        contenido = ruta.read_bytes()
        entradas, _ = ledger_parse.parse(str(ruta))
        entradas_evento = [e for e in entradas if e.sha == resultado.entry_eid]
        inicio = f"<!-- LLMINBOX-EVENT-BEGIN v=1 event_id={resultado.event_id} ".encode()
        fin = f"<!-- LLMINBOX-EVENT-END v=1 event_id={resultado.event_id} -->".encode()
        if (not contenido.startswith(bytes_antes) or contenido.count(inicio) != 1
                or contenido.count(fin) != 1 or len(entradas_evento) != 1
                or hashlib.sha256(entradas_evento[0].text.encode()).hexdigest()
                != resultado.entry_eid
                or entradas_evento[0].byte_off != resultado.byte_off):
            raise BancoNoEjecutable("recovery: bytes, hash, offset o unicidad no coinciden")
        # Reabrir el worker no debe volver a escribir un evento ya materializado.
        otra = MarkdownProjector(j2, token, [target], frame_key=frame_key)
        replay_final = j2.accept_event(token, idempotency_key="clave-recovery-g10",
                                       intent=INTENT, ledger=LEDGER)
        if (not replay_final.replayed or replay_final.event_id != resultado.event_id
                or replay_final.receipt_id != datos["receipt_id"]
                or otra.project_next() is not None or ruta.read_bytes() != contenido):
            raise BancoNoEjecutable("recovery: el replay alteró el ledger o el recibo")
        pendiente_tras_drenar = j2.pending_outbox(token)
        sin_resolver = j2.unresolved_outbox(token)
        if pendiente_tras_drenar or sin_resolver:
            raise BancoNoEjecutable("recovery: queda trabajo pendiente o fallido")
    finally:
        j2.close()
    return {"segundos_hasta_drenado": round(time.perf_counter() - t0, 3),
           "pendiente_tras_drenar": pendiente_tras_drenar,
           "sin_resolver_tras_drenar": sin_resolver,
           "proyecciones_previas": preparadas, "event_id": resultado.event_id,
           "receipt_id": datos["receipt_id"], "entry_eid": resultado.entry_eid,
           "ledger_path": str(ruta), "ledger_sha256": hashlib.sha256(contenido).hexdigest(),
           "bytes_ledger": len(contenido), "entradas_del_evento": 1,
           "replay_sin_escritura": True, "estado_acreditado": "materialized"}


def _veredictos(res: dict) -> dict:
    v, u = {}, UMBRALES

    def cmp(nombre, valor, umbral, mejor_menor=True):
        if valor is None:
            v[nombre] = {"valor": None, "umbral": umbral, "veredicto": "SIN_MEDIR"}
            return
        ok = valor <= umbral if mejor_menor else valor >= umbral
        v[nombre] = {"valor": valor, "umbral": umbral, "veredicto": "PASA" if ok else "FALLA"}

    cmp("p95_ms_busqueda", res["latencia"]["p95_ms"], u["p95_ms_busqueda"])
    cmp("recall_minimo", res["latencia"]["recall"], u["recall_minimo"], mejor_menor=False)
    cmp("fugas_cross_lane", res["latencia"]["fugas_cross_lane_en_B"], u["fugas_cross_lane"])
    cmp("visibilidad_B_ledger_propio", res["latencia"]["visibilidad_B_ledger_propio"],
        u["visibilidad_B_ledger_propio_min"], mejor_menor=False)
    cmp("visibilidad_A_ledger_ajeno", res["latencia"]["visibilidad_A_ledger_ajeno"],
        u["visibilidad_A_ledger_ajeno_min"], mejor_menor=False)
    denegada = res["latencia"]["acceso_B_ledger_ajeno_denegado"]
    v["acceso_B_ledger_ajeno_denegado"] = {
        "valor": denegada, "umbral": True,
        "veredicto": "PASA" if denegada is True else "FALLA"}
    cmp("paginacion_duplicados", res["latencia"]["paginacion_duplicados"],
        u["paginacion_duplicados"])
    cmp("paginacion_huecos", res["latencia"]["paginacion_huecos"], u["paginacion_huecos"])
    cmp("claims_cruzados_de_lane", res["concurrencia"]["claims_cruzados_de_lane"],
        u["claims_cruzados_de_lane"])
    cmp("recovery_segundos_max", res["recovery"]["segundos_hasta_drenado"],
        u["recovery_segundos_max"])
    cmp("rss_pico_max_gib", res["limites"]["rss_pico_gib"], u["rss_pico_max_gib"])
    cmp("rss_pico_hijos_max_gib", res["limites"]["rss_pico_hijos_gib"],
        u["rss_pico_hijos_max_gib"])
    cmp("rss_flota_cota_superior_max_gib", res["concurrencia"]["rss_flota_cota_superior_gib"],
        u["rss_flota_max_gib"])
    v["orden_estricto_desc"] = {
        "valor": res["latencia"]["orden_estricto_desc"], "umbral": True,
        "veredicto": "PASA" if res["latencia"]["orden_estricto_desc"] else "FALLA"}
    v["recovery_deja_cero_pendientes"] = {
        "valor": res["recovery"]["pendiente_tras_drenar"], "umbral": 0,
        "veredicto": "PASA" if res["recovery"]["pendiente_tras_drenar"] == 0 else "FALLA"}
    return v


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Banco G10 · envolvente acotada de flota v1.0")
    ap.add_argument("--filas", type=int, default=FILAS_CANONICAS)
    ap.add_argument("--agentes", type=int, default=AGENTES_CANONICOS)
    ap.add_argument("--semilla", type=int, default=SEMILLA_POR_DEFECTO)
    ap.add_argument("--repeticiones", type=int, default=40)
    ap.add_argument("--salida", type=pathlib.Path, default=None)
    ap.add_argument("--db", type=pathlib.Path, default=None)
    ap.add_argument("--conservar-db", action="store_true")
    args = ap.parse_args(argv)

    if "pytest" in sys.modules:
        raise BancoNoEjecutable("el banco no se ejecuta dentro de pytest")
    if args.agentes < 2 or args.agentes % 2 != 0:
        raise BancoNoEjecutable("--agentes tiene que ser par y >=2: DOS lanes")
    if args.filas < 1 or args.repeticiones < 1:
        raise BancoNoEjecutable("--filas y --repeticiones deben ser positivos")

    entorno = _entorno()
    carga_antes = _carga()
    manifiesto = _manifiesto_corpus(args.semilla, args.filas)

    db = args.db or pathlib.Path(f"/tmp/g10-bench-{args.semilla}-{args.filas}.sqlite")
    db_journal = _prepara_db_journal(db)          # hermano del corpus, POR CONTRATO
    st, con, construccion = _construye_indice(db, args.semilla, args.filas)
    latencia = _latencia_y_recall(st, manifiesto, args.repeticiones)
    limites = _limites(db)
    con.close()

    concurrencia = _fase_concurrencia_y_aislamiento(str(db_journal), args.agentes)
    token_recovery = concurrencia["tokens"][CARRIL_A][0]
    recovery = _fase_recovery(str(db_journal), token_recovery)
    limites["rss_pico_hijos_gib"] = _limites(db)["rss_pico_hijos_gib"]

    carga_despues = _carga()
    saturado = max(carga_antes["por_nucleo"], carga_despues["por_nucleo"]) > CARGA_MAXIMA_POR_NUCLEO

    res = {
        "entorno": entorno, "corpus": manifiesto, "construccion": construccion,
        "latencia": latencia, "limites": limites,
        "concurrencia": {k: v for k, v in concurrencia.items() if k != "tokens"},
        "recovery": recovery,
        "carga": {"antes": carga_antes, "despues": carga_despues,
                 "maxima_por_nucleo_admitida": CARGA_MAXIMA_POR_NUCLEO, "saturado": saturado},
        "umbrales_declarados": UMBRALES,
        "es_canonico": args.filas == FILAS_CANONICAS and args.agentes == AGENTES_CANONICOS,
        "momento": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    res["veredictos"] = _veredictos(res)
    fallos = sorted(k for k, x in res["veredictos"].items() if x["veredicto"] == "FALLA")
    res["fallos"] = fallos
    sin_medir = sorted(k for k, x in res["veredictos"].items()
                       if x["veredicto"] == "SIN_MEDIR")
    res["sin_medir"] = sin_medir

    descalifica = []
    if not res["es_canonico"]:
        descalifica.append(
            f"NO_CANONICO (filas={args.filas}/{FILAS_CANONICAS}, "
            f"agentes={args.agentes}/{AGENTES_CANONICOS}: humo del arnés)")
    if saturado:
        descalifica.append(
            f"HOST_SATURADO (loadavg1/núcleo="
            f"{max(carga_antes['por_nucleo'], carga_despues['por_nucleo']):.2f} > "
            f"{CARGA_MAXIMA_POR_NUCLEO}: no se declara envolvente sobre un host cargado)")
    if concurrencia["errores"]:
        descalifica.append(f"AGENTES_CON_ERROR ({len(concurrencia['errores'])})")
    if fallos:
        descalifica.append(f"UMBRALES_INCUMPLIDOS ({', '.join(fallos)})")
    if sin_medir:
        descalifica.append(f"MEDIDAS_INCOMPLETAS ({', '.join(sin_medir)})")
    res["descalificadores"] = descalifica
    res["estado"] = "PASA" if not descalifica else " · ".join(descalifica)

    salida = args.salida or pathlib.Path(f"/tmp/g10-bench-{args.semilla}-{args.filas}.json")
    salida.parent.mkdir(parents=True, exist_ok=True)
    salida.write_text(json.dumps(res, ensure_ascii=False, indent=1, sort_keys=True),
                      encoding="utf-8")
    if not args.conservar_db:
        for base in (db, db_journal):       # ambas bases comparten ciclo de vida
            for f in (base, base.with_name(base.name + "-wal"),
                      base.with_name(base.name + "-shm")):
                if f.exists():
                    f.unlink()

    print(f"BANCO G10 [{res['estado']}] filas={args.filas} agentes={args.agentes} "
         f"p95={latencia['p95_ms']}ms recall={latencia['recall']} "
         f"fugasB_ajeno={latencia['fugas_cross_lane_en_B']}"
         f"/visB_propio={latencia['visibilidad_B_ledger_propio']} "
         f"claims_cruzados={concurrencia['claims_cruzados_de_lane']} "
         f"recovery={recovery['segundos_hasta_drenado']}s · fallos={fallos or 'ninguno'} · {salida}")
    return 0 if res["estado"] == "PASA" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BancoNoEjecutable as e:
        print(f"BANCO G10 [NO_EJECUTABLE]: {e}", file=sys.stderr)
        raise SystemExit(2)
