#!/usr/bin/env python3
"""BANCO M2 — corpus de 1M entradas, reproducible y AUTOIDENTIFICADO.

⛔ ESTO NO ES UN TEST Y NO SE COLECTA CON `pytest`. Vive fuera de `tests/` a propósito:
un banco mide y una suite decide, y mezclarlos convierte un host lento en un fallo de
correccion. `tests/search` sigue siendo el gate; esto es el entregable de rendimiento.

## Qué produce

Un JSON ligado a su sujeto —`git` SHA, limpieza del árbol, Python, SQLite, opciones de
compilación, disponibilidad de FTS5, host— con:

· **Corpus determinista**: semilla + manifiesto con `sha256` del contenido generado. Dos
  corridas con la misma semilla producen el MISMO digest, y el digest va en la salida. Sin
  eso, «1M filas» no nombra ningún corpus concreto y dos números no son comparables.
· **Recall con controles ⊕ y ⊖**: agujas plantadas en posiciones conocidas (el oráculo es
  la SEMILLA, no una segunda consulta), un término que no existe en ningún cuerpo, y un
  término plantado SÓLO en otro carril.
· **Latencia p50/p95** y **throughput** por clase de consulta.
· **Paginación completa** del recorrido con cursor: sin huecos, sin duplicados y en orden
  estricto por `(arrival, eid)` descendente.
· **Aislamiento cross-lane** sobre el corpus grande, no sobre tres filas de laboratorio.
· **Límites** de tamaño en disco y de memoria pico.
· **Umbrales DECLARADOS** y un veredicto por umbral.

## Lo que este banco se NIEGA a hacer

· **No hay camino sin FTS5.** Si el SQLite del host no lo trae, aborta. La alternativa
  —`LIKE`— mediría otra cosa y produciría un número que parece comparable y no lo es. Se
  comprueba además que la sentencia de producción no contenga `LIKE`.
· **No certifica en host saturado.** Mide la carga antes y después; por encima del umbral
  la corrida sale marcada `INDICATIVO` y los umbrales NO se dan por cumplidos. Un p95 en
  una máquina con otras seis suites encima no es una medida del sujeto.
· **No llama canónica a una corrida pequeña.** `es_canonico` sólo es cierto con el millón.

## Cómo se corre

    # determinismo del corpus, sin tocar disco (segundos)
    python3 bench/m2_bench.py --solo-manifiesto --filas 1000000

    # humo, para comprobar el arnés (minutos)
    python3 bench/m2_bench.py --filas 20000 --salida /tmp/humo.json

    # EVIDENCIA: ventana limpia, sin nada más en la máquina
    python3 bench/m2_bench.py --filas 1000000 --salida bench/resultados/m2-1m.json
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

import search_contract as sc      # noqa: E402
import search_cursor as scur      # noqa: E402
import search_store as ss         # noqa: E402

FILAS_CANONICAS = 1_000_000
SEMILLA_POR_DEFECTO = 20260905

# ── Corpus ───────────────────────────────────────────────────────────────────────
# Carriles con PREFIJO COMPARTIDO: es la única familia donde el `MATCH` deja de aislar y
# la frontera `= ?` tiene que hacer el trabajo. Con nombres sin relación, el aislamiento
# saldría verde por el vocabulario y no por la guarda.
CARRIL = "64bis"
LANES = ("64bis-wiki", "64bis-wiki-archivo", "64bis-wiki-queue")
LANE_PRINCIPAL = LANES[0]
ACTORES = ("cto", "cto-A", "qa", "security", "backend", "frontend")
TIPOS = ("FYI", "DECISION", "HALLAZGO", "HANDOFF")

# Vocabulario FIJO y ordenado: el corpus tiene que ser el mismo en cualquier máquina, y
# `set` de Python no lo garantiza entre versiones.
VOCABULARIO = tuple(sorted({
    "ledger", "entrada", "carril", "cursor", "indice", "consulta", "frontera", "sello",
    "manifiesto", "huella", "rebuild", "generacion", "presupuesto", "techo", "sobre",
    "fragmento", "marca", "normalizador", "framing", "keyset", "pagina", "aislamiento",
    "evidencia", "falsador", "mutante", "veredicto", "cobertura", "gate", "arnes",
    "instantanea", "digest", "semilla", "corpus", "recall", "latencia", "umbral",
}))

# Términos PLANTADOS con densidad conocida. El oráculo del recall es esta tabla más la
# semilla — no una segunda consulta al mismo motor, que mediría el motor contra sí mismo.
AGUJA_RARA = "zzrara"          # en RARAS_N documentos del carril principal
AGUJA_MEDIA = "zzmedia"        # 1 de cada MEDIA_CADA
AGUJA_AJENA = "zzajena"        # SÓLO en los carriles hermanos: control ⊖ cross-lane
AGUJA_INEXISTENTE = "zznoexiste"   # en NINGÚN cuerpo: control ⊖ de recall
RARAS_N = 2500                 # > MAX_LIMIT*20: fuerza un recorrido de muchas páginas
MEDIA_CADA = 97

# ── Umbrales DECLARADOS ──────────────────────────────────────────────────────────
# Van aquí, en el código, y no en la cabeza de quien lea el JSON: un umbral que se decide
# después de ver el número no es un umbral. Se declaran para el corpus canónico de 1M en
# una máquina desocupada; la salida dice cuáles se cumplen y cuáles no.
UMBRALES = {
    "p95_ms_rara": 150.0,
    "p95_ms_media": 400.0,
    "p95_ms_comun": 1500.0,
    "p95_ms_filtrada": 400.0,
    "throughput_min_qps_rara": 10.0,
    "recall_minimo": 1.0,
    "fugas_cross_lane": 0,
    "paginacion_duplicados": 0,
    "paginacion_huecos": 0,
    "paginacion_sobrantes": 0,
    "tamano_db_max_gib": 8.0,
    "rss_pico_max_gib": 3.0,
}
# Por encima de esto la máquina no está para medir. `loadavg1 / nucleos`.
CARGA_MAXIMA_POR_NUCLEO = 0.60


class BancoNoEjecutable(RuntimeError):
    """El host no puede producir una medida válida. Nunca se degrada a otra cosa."""


# ── Procedencia ──────────────────────────────────────────────────────────────────
def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=RAIZ, capture_output=True,
                              text=True, timeout=30).stdout.strip()
    except Exception as e:                                   # pragma: no cover
        return f"<git falló: {e}>"


def _exige_fts5() -> list[str]:
    """FTS5 o nada. **No hay camino con `LIKE`**: un banco que se degrada a otro motor de
    búsqueda emite un número que se lee como comparable y mide otra cosa."""
    con = sqlite3.connect(":memory:")
    try:
        con.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
    except sqlite3.OperationalError as e:
        raise BancoNoEjecutable(
            f"este SQLite no trae FTS5 ({e}). No mido con `LIKE`: sería otro motor y otro "
            f"número. Compila SQLite con ENABLE_FTS5 o usa otro intérprete") from None
    opciones = [r[0] for r in con.execute("PRAGMA compile_options")]
    con.close()
    # Y la sentencia de PRODUCCIÓN tampoco puede llevar `LIKE`: si lo llevara, el banco
    # estaría midiendo un escaneo disfrazado de índice.
    if "LIKE" in ss.SQL_BUSQUEDA.upper():
        raise BancoNoEjecutable("la sentencia de búsqueda contiene `LIKE`")
    return opciones


def _entorno() -> dict:
    diff = _git("diff", "HEAD")
    porcelain = _git("status", "--porcelain")
    opciones = _exige_fts5()          # aborta aquí si el host no puede medir esto
    return {
        "git_sha": _git("rev-parse", "HEAD"),
        "git_rama": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "git_limpio": porcelain == "",
        "git_status_porcelain": porcelain,
        "git_diff_sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        "python": sys.version.split()[0],
        "python_ejecutable": sys.executable,
        "sqlite_runtime": sqlite3.sqlite_version,
        # `sqlite3.version` (la del módulo, no la del motor) se retiró en Python 3.12+ y
        # nunca dijo nada útil: la versión que decide el comportamiento del índice es la
        # del MOTOR, y ésa es `sqlite_version`.
        "fts5": True,
        "fts5_comprobado_creando_tabla_virtual": True,
        "sqlite_compile_options": opciones,
        "host": {
            "sistema": platform.platform(),
            "maquina": platform.machine(),
            "procesador": platform.processor(),
            "nucleos": os.cpu_count(),
            "nombre": platform.node(),
        },
        # El banco se identifica a SÍ MISMO: un número producido por otra versión de este
        # fichero no es comparable, y el `git_sha` no lo dice si el fichero está sin
        # commitear (que es justo el caso mientras se escribe).
        "bench_sha256": hashlib.sha256(
            pathlib.Path(__file__).read_bytes()).hexdigest(),
        "fuentes_sha256": {
            f: hashlib.sha256((RAIZ / f).read_bytes()).hexdigest()
            for f in ("search_store.py", "search_contract.py", "search_cursor.py")},
    }


def _carga() -> dict:
    uno, cinco, quince = os.getloadavg()
    nucleos = os.cpu_count() or 1
    return {"loadavg1": uno, "loadavg5": cinco, "loadavg15": quince,
            "nucleos": nucleos, "por_nucleo": uno / nucleos}


# ── Generador determinista ───────────────────────────────────────────────────────
def _filas(semilla: int, n: int):
    """Emite `n` filas REPRODUCIBLES. Mismo `(semilla, n)` ⇒ mismo corpus, byte a byte.

    `head` va SIEMPRE dentro de `body`: es la premisa global que `rebuild` comprueba antes
    de sellar (indexar sólo `body` perdería los titulares), así que un corpus que no la
    cumpla no llega ni a construirse.

    Se intercala el framing de ADR-001 en una de cada siete entradas para que el banco mida
    también el coste del proyector, no un cuerpo idealizado que nadie tiene.
    """
    rng = random.Random(semilla)
    # Posiciones de las agujas raras: elegidas de una vez, ordenadas, deterministas.
    raras = sorted(rng.sample(range(n), min(RARAS_N, n)))
    conjunto_raras = set(raras)
    for i in range(n):
        lane = LANES[i % len(LANES)]
        actor = ACTORES[rng.randrange(len(ACTORES))]
        tipo = TIPOS[rng.randrange(len(TIPOS))]
        palabras = [VOCABULARIO[rng.randrange(len(VOCABULARIO))] for _ in range(24)]
        if lane == LANE_PRINCIPAL and i in conjunto_raras:
            palabras.append(AGUJA_RARA)
        if i % MEDIA_CADA == 0:
            palabras.append(AGUJA_MEDIA)
        if lane != LANE_PRINCIPAL and i % 11 == 0:
            palabras.append(AGUJA_AJENA)
        eid = hashlib.sha256(f"{semilla}:{i}".encode("utf-8")).hexdigest()
        head = f"titular {eid[:16]} numero {i}"
        cuerpo = " ".join(palabras)
        if i % 7 == 0:
            body = (f"<!-- LLMINBOX-EVENT-BEGIN event_id={eid} payload_sha={eid[:32]} -->\n"
                    f"{head}\n{cuerpo}\n"
                    f"<!-- LLMINBOX-EVENT-END event_id={eid} -->")
        else:
            body = f"{head}\n{cuerpo}"
        # `arrival` estrictamente creciente y `ts` derivado: el orden total lo da
        # `(arrival, eid)` y tiene que ser inequívoco para poder falsar la paginación.
        yield {"ledger": lane, "eid": eid, "arrival": i + 1,
               "ts": f"2026-{1 + (i % 12):02d}-{1 + (i % 28):02d}",
               "actor": actor, "tipo": tipo, "head": head, "body": body}


def _manifiesto_corpus(semilla: int, n: int) -> dict:
    """`sha256` en STREAMING de todo el corpus, sin materializarlo.

    Es lo que convierte «1M filas» en un corpus CONCRETO: dos corridas que declaren el
    mismo digest midieron sobre el mismo texto. Sin él, comparar dos p95 es comparar dos
    cosas que nadie ha comprobado que sean la misma.
    """
    h = hashlib.sha256()
    raras = medias = ajenas = 0
    t0 = time.perf_counter()
    for fila in _filas(semilla, n):
        h.update(json.dumps(fila, sort_keys=True, ensure_ascii=False,
                            separators=(",", ":")).encode("utf-8"))
        h.update(b"\x00")
        if AGUJA_RARA in fila["body"]:
            raras += 1
        if AGUJA_MEDIA in fila["body"]:
            medias += 1
        if AGUJA_AJENA in fila["body"]:
            ajenas += 1
    return {
        "semilla": semilla, "filas": n, "digest_sha256": h.hexdigest(),
        "vocabulario_sha256": hashlib.sha256(
            "\n".join(VOCABULARIO).encode("utf-8")).hexdigest(),
        "lanes": list(LANES), "lane_principal": LANE_PRINCIPAL,
        "agujas": {"rara": {"termino": AGUJA_RARA, "documentos": raras},
                   "media": {"termino": AGUJA_MEDIA, "documentos": medias},
                   "ajena_otros_carriles": {"termino": AGUJA_AJENA, "documentos": ajenas},
                   "inexistente": {"termino": AGUJA_INEXISTENTE, "documentos": 0}},
        "segundos_generacion": round(time.perf_counter() - t0, 3),
    }


# ── Construcción ─────────────────────────────────────────────────────────────────
ESQUEMA_ENTRIES = """
CREATE TABLE entries (
  ledger TEXT NOT NULL, eid TEXT NOT NULL,
  arrival INTEGER, seq INTEGER, line_no INTEGER, byte_off INTEGER,
  ts TEXT, actor TEXT, tipo TEXT, head TEXT, body TEXT,
  visto TEXT, ausente TEXT, provisional INTEGER DEFAULT 0,
  PRIMARY KEY (ledger, eid));
CREATE INDEX i_arr ON entries(ledger, arrival);
"""


def _construye(ruta: pathlib.Path, semilla: int, n: int, lote: int = 20_000) -> dict:
    """Puebla `entries` **antes** de crear los triggers, y luego `rebuild()`.

    El orden no es un detalle de velocidad: los triggers indexan cada `INSERT`, y el
    `rebuild` empieza por `DELETE FROM search_fts`. Poblar con los triggers puestos paga
    un millón de escrituras en el índice para tirarlas justo después.
    """
    if ruta.exists():
        ruta.unlink()
    con = sqlite3.connect(str(ruta))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(ESQUEMA_ENTRIES)
    t0 = time.perf_counter()
    buffer, metidas = [], 0
    for fila in _filas(semilla, n):
        buffer.append((fila["ledger"], fila["eid"], fila["arrival"], fila["ts"],
                       fila["actor"], fila["tipo"], fila["head"], fila["body"]))
        if len(buffer) >= lote:
            con.executemany("INSERT INTO entries(ledger,eid,arrival,ts,actor,tipo,head,"
                            "body) VALUES(?,?,?,?,?,?,?,?)", buffer)
            metidas += len(buffer); buffer.clear()
    if buffer:
        con.executemany("INSERT INTO entries(ledger,eid,arrival,ts,actor,tipo,head,body)"
                        " VALUES(?,?,?,?,?,?,?,?)", buffer)
        metidas += len(buffer)
    con.commit()
    t_poblar = time.perf_counter() - t0

    t1 = time.perf_counter()
    # ACL EXPLÍCITA, como en producción: el banco autoriza al carril a leer los tres
    # ledgers del corpus. Sin declararla, `search` niega —que es el defecto correcto— y el
    # banco moriría con `LaneNotAuthorized` en vez de medir.
    st = ss.SearchStore(con, cursor_key=b"banco-m2-clave-de-cursor-de-32-bytes!!",
                        acl={CARRIL: set(LANES)})
    st.ensure_schema()
    # La ACL se PERSISTE: la tabla `search_acl` es la autoridad y es lo que aplica el
    # `EXISTS` de la sentencia. Declararla sólo en memoria dejaría el `EXISTS` sin filas y
    # el banco mediría un índice que no devuelve nada.
    st.set_acl({CARRIL: set(LANES)})
    gen = st.rebuild()
    t_rebuild = time.perf_counter() - t1
    return {"store": st, "metidas": metidas, "generacion": gen,
            "segundos_poblar": round(t_poblar, 3),
            "segundos_rebuild": round(t_rebuild, 3)}


# ── Medida ───────────────────────────────────────────────────────────────────────
def _percentil(muestras: list[float], p: float) -> float:
    if not muestras:
        return float("nan")
    orden = sorted(muestras)
    k = (len(orden) - 1) * p
    bajo, alto = int(k), min(int(k) + 1, len(orden) - 1)
    return orden[bajo] + (orden[alto] - orden[bajo]) * (k - bajo)


def _mide(st, nombre: str, repeticiones: int, **kw) -> dict:
    """Latencias en ms. Calentamiento aparte: la primera consulta paga el caché frío del
    sistema de ficheros y meterla en la muestra desplaza el p95 sin decir por qué."""
    st.search(lane=CARRIL, ledger=LANE_PRINCIPAL, limit=20, **kw)   # calentamiento
    muestras, filas_vistas = [], 0
    t0 = time.perf_counter()
    for _ in range(repeticiones):
        a = time.perf_counter()
        r = st.search(lane=CARRIL, ledger=LANE_PRINCIPAL, limit=20, **kw)
        muestras.append((time.perf_counter() - a) * 1000.0)
        filas_vistas += len(r["filas"])
    total = time.perf_counter() - t0
    return {
        "consulta": nombre, "repeticiones": repeticiones,
        "p50_ms": round(_percentil(muestras, 0.50), 3),
        "p95_ms": round(_percentil(muestras, 0.95), 3),
        "p99_ms": round(_percentil(muestras, 0.99), 3),
        "min_ms": round(min(muestras), 3), "max_ms": round(max(muestras), 3),
        "throughput_qps": round(repeticiones / total, 3) if total else None,
        "filas_por_consulta": filas_vistas / repeticiones,
    }


# ── Verificaciones (correccion, no velocidad) ────────────────────────────────────
def _recall_y_aislamiento(st, manifiesto: dict) -> dict:
    """Recall contra el oráculo de la SEMILLA, con ⊕ y ⊖ explícitos."""
    esperados_rara = manifiesto["agujas"]["rara"]["documentos"]

    vistos, dups, paginas, orden_ok = [], 0, 0, True
    cursor, previo = None, None
    while True:
        r = st.search(lane=CARRIL, ledger=LANE_PRINCIPAL, query=AGUJA_RARA,
                      limit=100, cursor=cursor)
        paginas += 1
        for f in r["filas"]:
            clave = (f["arrival"], f["eid"])
            if previo is not None and not (clave < previo):
                orden_ok = False
            previo = clave
            vistos.append(f["eid"])
        if not r["hay_mas"]:
            break
        cursor = r["cursor"]
        if paginas > 10_000:                       # cota dura: un bucle no es un recorrido
            raise BancoNoEjecutable("la paginación no termina")
    dups = len(vistos) - len(set(vistos))

    # ⊖ CONTROL 1: un término que no está en NINGÚN cuerpo.
    inexistente = st.search(lane=CARRIL, ledger=LANE_PRINCIPAL,
                            query=AGUJA_INEXISTENTE, limit=10)
    # ⊖ CONTROL 2 (cross-lane): un término plantado SÓLO en los carriles hermanos.
    ajena_aqui = st.search(lane=CARRIL, ledger=LANE_PRINCIPAL,
                           query=AGUJA_AJENA, limit=100)
    # ⊕ CONTROL: ese mismo término SÍ aparece en su carril. Sin este arm, el cero de
    # arriba podría ser porque el término no se indexó nunca.
    ajena_alli = st.search(lane=CARRIL, ledger=LANES[1], query=AGUJA_AJENA, limit=100)

    # Barrido ancho del carril principal: ninguna fila puede ser de otro `ledger`.
    ancho = st.search(lane=CARRIL, ledger=LANE_PRINCIPAL, query=AGUJA_MEDIA, limit=100)
    ajenos = sorted({f["ledger"] for f in ancho["filas"]} - {LANE_PRINCIPAL})

    return {
        "recall_rara": (len(set(vistos)) / esperados_rara) if esperados_rara else None,
        "esperados_rara": esperados_rara,
        "encontrados_rara": len(set(vistos)),
        "paginacion": {"paginas": paginas, "filas_totales": len(vistos),
                       "duplicados": dups,
                       "huecos": max(0, esperados_rara - len(set(vistos))),
                       # SOBRANTES, y no sólo huecos: un recorrido que devuelve documentos
                       # que la semilla no plantó tiene `huecos=0` y `recall>=1`, o sea que
                       # las dos cifras de arriba lo dan por bueno. Es el mismo error de
                       # siempre —mirar sólo la dirección que se teme— con otra ropa.
                       "sobrantes": max(0, len(set(vistos)) - esperados_rara),
                       "orden_estricto_desc": orden_ok},
        "control_negativo_inexistente": len(inexistente["filas"]),
        "cross_lane": {"fugas_en_carril_principal": len(ajena_aqui["filas"]),
                       "control_positivo_en_su_carril": len(ajena_alli["filas"]),
                       "ledgers_ajenos_en_barrido": ajenos},
    }


def _limites(ruta: pathlib.Path) -> dict:
    bytes_db = sum(f.stat().st_size for f in
                   [ruta, ruta.with_name(ruta.name + "-wal"),
                    ruta.with_name(ruta.name + "-shm")] if f.exists())
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS informa en bytes; Linux en KiB.
    rss_bytes = rss if sys.platform == "darwin" else rss * 1024
    return {"tamano_db_bytes": bytes_db,
            "tamano_db_gib": round(bytes_db / 2 ** 30, 4),
            "rss_pico_bytes": rss_bytes,
            "rss_pico_gib": round(rss_bytes / 2 ** 30, 4)}


def _veredictos(res: dict) -> dict:
    """Un PASS/FAIL por umbral declarado. Nada de «parece bien»."""
    lat = {m["consulta"]: m for m in res["latencia"]}
    v, u = {}, UMBRALES
    def cmp(nombre, valor, umbral, mejor_menor=True):
        if valor is None:
            v[nombre] = {"valor": None, "umbral": umbral, "veredicto": "SIN_MEDIR"}
            return
        ok = valor <= umbral if mejor_menor else valor >= umbral
        v[nombre] = {"valor": valor, "umbral": umbral,
                     "veredicto": "PASA" if ok else "FALLA"}
    for clase in ("rara", "media", "comun", "filtrada"):
        cmp(f"p95_ms_{clase}", lat.get(clase, {}).get("p95_ms"), u[f"p95_ms_{clase}"])
    cmp("throughput_min_qps_rara", lat.get("rara", {}).get("throughput_qps"),
        u["throughput_min_qps_rara"], mejor_menor=False)
    ver = res["verificacion"]
    cmp("recall_minimo", ver["recall_rara"], u["recall_minimo"], mejor_menor=False)
    cmp("fugas_cross_lane", ver["cross_lane"]["fugas_en_carril_principal"],
        u["fugas_cross_lane"])
    cmp("paginacion_duplicados", ver["paginacion"]["duplicados"],
        u["paginacion_duplicados"])
    cmp("paginacion_huecos", ver["paginacion"]["huecos"], u["paginacion_huecos"])
    cmp("paginacion_sobrantes", ver["paginacion"]["sobrantes"], u["paginacion_sobrantes"])
    cmp("tamano_db_max_gib", res["limites"]["tamano_db_gib"], u["tamano_db_max_gib"])
    cmp("rss_pico_max_gib", res["limites"]["rss_pico_gib"], u["rss_pico_max_gib"])
    # Éstos no son umbrales numéricos sino invariantes: se declaran igual.
    v["orden_estricto_desc"] = {
        "valor": ver["paginacion"]["orden_estricto_desc"], "umbral": True,
        "veredicto": "PASA" if ver["paginacion"]["orden_estricto_desc"] else "FALLA"}
    v["control_negativo_da_cero"] = {
        "valor": ver["control_negativo_inexistente"], "umbral": 0,
        "veredicto": "PASA" if ver["control_negativo_inexistente"] == 0 else "FALLA"}
    v["control_positivo_cross_lane"] = {
        "valor": ver["cross_lane"]["control_positivo_en_su_carril"], "umbral": ">0",
        "veredicto": ("PASA" if ver["cross_lane"]["control_positivo_en_su_carril"] > 0
                      else "FALLA")}
    return v


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Banco M2 sobre corpus reproducible")
    ap.add_argument("--filas", type=int, default=FILAS_CANONICAS)
    ap.add_argument("--semilla", type=int, default=SEMILLA_POR_DEFECTO)
    ap.add_argument("--repeticiones", type=int, default=60)
    ap.add_argument("--db", type=pathlib.Path, default=None)
    ap.add_argument("--salida", type=pathlib.Path, default=None)
    ap.add_argument("--solo-manifiesto", action="store_true",
                    help="genera el corpus en memoria y sólo imprime su digest")
    ap.add_argument("--conservar-db", action="store_true")
    args = ap.parse_args(argv)

    if "pytest" in sys.modules:                    # cinturón: esto no es una suite
        raise BancoNoEjecutable("el banco no se ejecuta dentro de pytest")

    entorno = _entorno()
    manifiesto = _manifiesto_corpus(args.semilla, args.filas)
    if args.solo_manifiesto:
        print(json.dumps({"entorno": entorno, "corpus": manifiesto},
                         ensure_ascii=False, indent=1, sort_keys=True))
        return 0

    carga_antes = _carga()
    db = args.db or pathlib.Path(f"/tmp/m2-bench-{args.semilla}-{args.filas}.sqlite")
    construccion = _construye(db, args.semilla, args.filas)
    st = construccion.pop("store")

    latencia = [
        _mide(st, "rara", args.repeticiones, query=AGUJA_RARA),
        _mide(st, "media", args.repeticiones, query=AGUJA_MEDIA),
        _mide(st, "comun", max(10, args.repeticiones // 4), query=VOCABULARIO[0]),
        _mide(st, "filtrada", args.repeticiones, query=AGUJA_MEDIA, actor=ACTORES[0]),
        _mide(st, "dos_terminos", args.repeticiones,
              query=f"{VOCABULARIO[0]} {VOCABULARIO[1]}"),
    ]
    verificacion = _recall_y_aislamiento(st, manifiesto)
    limites = _limites(db)
    carga_despues = _carga()

    saturado = max(carga_antes["por_nucleo"], carga_despues["por_nucleo"]) > CARGA_MAXIMA_POR_NUCLEO
    res = {
        "entorno": entorno,
        "corpus": manifiesto,
        "construccion": construccion,
        "latencia": latencia,
        "verificacion": verificacion,
        "limites": limites,
        "carga": {"antes": carga_antes, "despues": carga_despues,
                  "maxima_por_nucleo_admitida": CARGA_MAXIMA_POR_NUCLEO,
                  "saturado": saturado},
        "umbrales_declarados": UMBRALES,
        "es_canonico": args.filas == FILAS_CANONICAS,
        "momento": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    res["veredictos"] = _veredictos(res)
    fallos = sorted(k for k, x in res["veredictos"].items() if x["veredicto"] == "FALLA")
    res["fallos"] = fallos
    # El estado NO se deduce sólo de los umbrales: una corrida pequeña o en host cargado
    # no acredita nada aunque todos pasen.
    # Los descalificadores se ENUMERAN. Con un `elif`, un corpus pequeño TAPABA que además
    # el host estuviera saturado, y al leer el JSON parecía que sólo faltaba subir las
    # filas. Un `AND` con un término verde sigue siendo rojo: aquí se ven los dos.
    descalifica = []
    if not res["es_canonico"]:
        descalifica.append(f"NO_CANONICO (corpus de {args.filas}, no 1M: humo del arnés)")
    if saturado:
        descalifica.append(
            f"HOST_SATURADO (loadavg1/núcleo="
            f"{max(carga_antes['por_nucleo'], carga_despues['por_nucleo']):.2f} > "
            f"{CARGA_MAXIMA_POR_NUCLEO}: los umbrales de latencia NO se dan por cumplidos)")
    if fallos:
        descalifica.append(f"UMBRALES_INCUMPLIDOS ({', '.join(fallos)})")
    res["descalificadores"] = descalifica
    res["estado"] = "PASA" if not descalifica else " · ".join(descalifica)

    salida = args.salida or pathlib.Path(
        f"/tmp/m2-bench-{args.semilla}-{args.filas}.json")
    salida.parent.mkdir(parents=True, exist_ok=True)
    salida.write_text(json.dumps(res, ensure_ascii=False, indent=1, sort_keys=True),
                      encoding="utf-8")
    if not args.conservar_db:
        for f in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
            if f.exists():
                f.unlink()
    print(f"BANCO M2 [{res['estado']}] filas={args.filas} corpus={manifiesto['digest_sha256'][:16]}"
          f" p95_rara={latencia[0]['p95_ms']}ms recall={verificacion['recall_rara']}"
          f" fugas={verificacion['cross_lane']['fugas_en_carril_principal']}"
          f" dups={verificacion['paginacion']['duplicados']}"
          f" huecos={verificacion['paginacion']['huecos']}"
          f" · fallos={fallos or 'ninguno'} · {salida}")
    return 0 if res["estado"] == "PASA" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BancoNoEjecutable as e:
        print(f"BANCO M2 [NO_EJECUTABLE]: {e}", file=sys.stderr)
        raise SystemExit(2)
