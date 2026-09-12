"""M4-3 · falsador CAUSAL por cada bloqueante de guion.

Causal quiere decir: cada prueba rompe **la condición concreta** que la cura afirma
vigilar y exige rojo, y trae su ⊕ con la condición intacta. Un test que sólo comprueba
el camino verde no distingue «la guarda funciona» de «la guarda no existe».

No se levanta ni se tumba ningún servicio: los gates de shell se ejercitan con dobles de
`docker` y `curl` en el PATH, y la semántica de ficheros (EXDEV, bundle WAL) se prueba
sobre ficheros de verdad en `tmp_path`, que es donde esa semántica vive.

⚠️ DEPENDENCIAS PENDIENTES DE INTEGRACIÓN, dichas y no tapadas: M1-M3 no están en esta
rama, así que `/health.journal.disponible` es `false` y `/health.politica.outbox` no
existe. Los gates correspondientes se NIEGAN, y eso es el comportamiento correcto, no un
fallo del test. Ninguna prueba de aquí declara el piloto listo.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
LIB = RAIZ / "scripts" / "m4-evidencia.sh"


def corre(cuerpo: str, tmp_path: Path, entorno: dict | None = None):
    """Ejecuta un fragmento con la biblioteca cargada y dobles en el PATH."""
    binario = tmp_path / "bin"
    binario.mkdir(exist_ok=True)
    env = {"PATH": f"{binario}:/usr/bin:/bin:/usr/sbin:/sbin",
           "HOME": str(tmp_path), "TMPDIR": str(tmp_path)}
    env.update(entorno or {})
    guion = f'. "{LIB}"\n{cuerpo}\n'
    return subprocess.run(["bash", "-c", guion], capture_output=True, text=True,
                          cwd=RAIZ, env=env, timeout=60)


def doble_curl(tmp_path: Path, cuerpo: str, *, rc: int = 0):
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    f = d / "curl"
    f.write_text("#!/bin/sh\n" + (f"exit {rc}\n" if rc else
                                  f"cat <<'JSON'\n{cuerpo}\nJSON\n"))
    f.chmod(0o755)


_RUNNER_FILES = [
    "coordination.py", "ledger_parse.py", "native_gateway.py", "observability.py",
    "projector.py", "projector_runner.py", "runtime_root.py", "search_contract.py",
    "search_cursor.py", "search_store.py", "servicio.py", "telemetry_bridge.py",
]
_ARTEFACTO_SANO = {
    "disponible": True,
    "esquema": 1,
    "requirements_lock_sha256": "a" * 64,
    "fuentes_sha256": {name: "b" * 64 for name in _RUNNER_FILES},
    "runner_empaquetado": {
        "entrypoints": ["runtime_root", "projector", "projector_runner"],
        "exigidos": _RUNNER_FILES,
        "faltan": [],
        "completo": True,
    },
}
SANO = json.dumps({"ok": True, "v8": {"integridad": "verificada", "mapa_alterado": False},
                   "artefacto": _ARTEFACTO_SANO,
                   "journal": {"disponible": True},
                   "politica": {"outbox": {"disponible": True, "pendientes": 0,
                                               "fallidos": 0, "pares": []}}})


# ══ ① SALUD: el cuerpo, no el código HTTP ═══════════════════════════════════════
@pytest.mark.parametrize("cuerpo,espera,porque", [
    (SANO, 0, "⊕ control: cuerpo sano ⇒ verde, o el rojo de abajo no diría nada"),
    (json.dumps({"ok": False, "avisos": ["sin ledgers"]}), 1,
     "200 con ok:false es un servicio que se declara ENFERMO"),
    (json.dumps({"v8": {"integridad": "verificada"}}), 1,
     "sin clave `ok`: su AUSENCIA es rojo, no verde"),
    ("<html>502 proxy</html>", 1, "200 con HTML no es salud"),
    ("[1,2,3]", 1, "JSON válido que no es objeto tampoco es salud"),
])
def test_gate_salud_exige_que_el_cuerpo_lo_diga(tmp_path, cuerpo, espera, porque):
    doble_curl(tmp_path, cuerpo)
    r = corre('gate_salud http://x; echo "rc=$?"', tmp_path)
    assert f"rc={espera}" in r.stdout, f"{porque}\n{r.stdout}\n{r.stderr}"


def test_gate_salud_se_niega_si_health_no_responde(tmp_path):
    doble_curl(tmp_path, "", rc=7)
    r = corre('gate_salud http://x; echo "rc=$?"', tmp_path)
    assert "rc=1" in r.stdout


# ══ ② JOURNAL: la ausencia NUNCA es ok ══════════════════════════════════════════
@pytest.mark.parametrize("bloque,espera,porque", [
    ('{"disponible":true}', 0, "⊕ control: presente ⇒ verde"),
    ('{"disponible":false,"motivo":"M1 no integrado"}', 1,
     "un motivo EXPLICA la ausencia; no la convierte en presencia"),
    ('{"motivo":"algo"}', 1, "sin `disponible` no se puede acreditar nada"),
    (None, 1, "sin bloque `journal`: imagen anterior a M4"),
])
def test_ausencia_de_journal_nunca_cuenta_ok(tmp_path, bloque, espera, porque):
    cuerpo = {"ok": True}
    if bloque is not None:
        cuerpo["journal"] = json.loads(bloque)
    doble_curl(tmp_path, json.dumps(cuerpo))
    r = corre('gate_journal http://x; echo "rc=$?"', tmp_path)
    assert f"rc={espera}" in r.stdout, f"{porque}\n{r.stdout}\n{r.stderr}"


# ══ ③ IMAGEN: el digest es la evidencia; la etiqueta no ═════════════════════════
def doble_docker(tmp_path: Path, cuerpo: str):
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    f = d / "docker"
    f.write_text("#!/bin/sh\n" + cuerpo)
    f.chmod(0o755)


DIG = "sha256:" + "ab" * 32


def test_una_etiqueta_movida_se_caza_por_digest(tmp_path):
    # La etiqueta resuelve HOY a otro contenido que cuando se ancló.
    otro = "sha256:" + "cd" * 32
    doble_docker(tmp_path, f'''
if [ "$1 $2" = "image inspect" ]; then
  printf '[{{"Id":"{otro}","RepoDigests":[]}}]\\n'; exit 0
fi
exit 0
''')
    r = corre(f'verifica_digest img:rollback-abc {DIG}; echo "rc=$?"', tmp_path)
    assert "rc=1" in r.stdout
    assert "DIGEST DISTINTO" in r.stderr


def test_control_positivo_el_digest_que_no_se_movio_pasa(tmp_path):
    doble_docker(tmp_path, f'''
if [ "$1 $2" = "image inspect" ]; then
  printf '[{{"Id":"{DIG}","RepoDigests":[]}}]\\n'; exit 0
fi
exit 0
''')
    r = corre(f'verifica_digest img:rollback-abc {DIG}; echo "rc=$?"', tmp_path)
    assert "rc=0" in r.stdout, f"sin este ⊕, el rojo de arriba no discrimina\n{r.stderr}"


def test_imagen_ausente_no_resuelve_digest(tmp_path):
    doble_docker(tmp_path, "exit 1\n")
    r = corre('digest_de_imagen img:x; echo "rc=$?"', tmp_path)
    assert "rc=1" in r.stdout


# ══ ④ SNAPSHOT: sha256 · integrity_check · CLASE ════════════════════════════════
#
# 🩸 ANTES ESTE BLOQUE EXIGÍA `meta.durable_v`, Y ERA EL CRITERIO INVERTIDO.
# `durable_v` es el sello del JOURNAL de coordinación (`coordination.py`); el índice
# escribe `meta.schema_v` (`servicio.py:3115-3121`) y NUNCA aquél — `durable_v` tiene 0
# hits en `servicio.py` fuera de dos `None` del bloque de salud. O sea que la única
# comprobación de clase que existía rechazaba todo snapshot REAL del índice y aceptaba
# uno del journal, que es justo el almacén que ADR-001 prohíbe restaurar.
# El fixture de entonces (`meta.durable_v` + una tabla `datos`) no era ninguno de los
# dos, así que el test verde no distinguía nada.
INDICE_TABLAS = ("entries", "recipients", "files", "cursors")
INDICE_SCHEMA_V = "e7f6c011776e8db7"
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
JOURNAL_TABLAS = ("principals", "credential_bindings", "runtime_sessions", "events",
                  "receipts", "receipt_transitions", "idempotency", "outbox", "leases",
                  "commands")


def base(ruta: Path, *, clase: str = "indice", tablas=None, meta=None,
         ddl_indice: str = INDICE_DDL):
    """Fabrica una base de la CLASE pedida, con su sello y sus tablas de verdad."""
    if clase == "indice":
        tablas = INDICE_TABLAS if tablas is None else tablas
        meta = {"schema_v": INDICE_SCHEMA_V} if meta is None else meta
    elif clase == "journal":
        tablas = JOURNAL_TABLAS if tablas is None else tablas
        meta = ({"durable_v": "3", "pepper_check": "x", "generation": "1"}
                if meta is None else meta)
    else:                                    # indeterminada: ni una cosa ni la otra
        tablas = () if tablas is None else tablas
        meta = {} if meta is None else meta
    con = sqlite3.connect(ruta)
    if set(tablas) == set(INDICE_TABLAS):
        con.executescript(ddl_indice)
    else:
        con.execute("CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT)")
    for k, v in meta.items():
        con.execute("INSERT INTO meta VALUES(?, ?)", (k, v))
    for t in tablas:
        if t in INDICE_TABLAS and set(tablas) == set(INDICE_TABLAS):
            continue
        con.execute(f"CREATE TABLE {t}(x INTEGER)")
        con.executemany(f"INSERT INTO {t} VALUES(?)", [(i,) for i in range(20)])
    con.commit()
    con.close()


def test_control_positivo_un_snapshot_bueno_publica_su_inventario(tmp_path):
    s = tmp_path / "snap.sqlite"
    base(s)
    r = corre(f'valida_snapshot "{s}" /data', tmp_path)
    assert r.returncode == 0, r.stderr
    inv = json.loads(r.stdout)
    assert inv["integrity_check"] == "ok" and inv["clase"] == "indice"
    assert len(inv["sha256"]) == 64 and inv["bytes"] > 0


def test_control_positivo_indice_REAL_preparado_por_servicio(tmp_path, monkeypatch):
    """El ⊕ no se autocertifica con el DDL copiado en este test: usa el productor real."""
    import servicio

    s = tmp_path / "indice-real.sqlite"
    monkeypatch.setattr(servicio, "DB", str(s))
    con = sqlite3.connect(s)
    con.row_factory = sqlite3.Row
    servicio._preparar_indice(con)
    con.commit(); con.close()
    assert servicio.huella_esquema() == INDICE_SCHEMA_V
    r = corre(f'valida_snapshot "{s}" /data', tmp_path)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["clase"] == "indice"
    assert _clasificador_del_helper()(str(s))[0] == "indice"


@pytest.mark.parametrize("defecto", ["sello", "columnas", "indices", "trigger"])
def test_nombres_correctos_NO_bastan_para_acreditar_un_indice(tmp_path, defecto):
    """Falsador del clasificador superficial: cuatro nombres públicos no son DDL."""
    s = tmp_path / "falso.sqlite"
    base(s)
    con = sqlite3.connect(s)
    if defecto == "sello":
        con.execute("UPDATE meta SET v='FAKE-NOT-A-SCHEMA' WHERE k='schema_v'")
    elif defecto == "columnas":
        con.execute("DROP TABLE entries")
        con.execute("CREATE TABLE entries(wrong TEXT)")
    elif defecto == "indices":
        con.execute("DROP INDEX i_arr")
    else:
        con.execute("CREATE TRIGGER t_malicioso AFTER INSERT ON entries "
                    "BEGIN DELETE FROM entries; END")
    con.commit(); con.close()
    r = corre(f'valida_snapshot "{s}" /data', tmp_path)
    assert r.returncode == 1
    assert "no acredita ser el índice" in r.stderr


def test_CHECK_hostil_no_clasifica_ni_se_publica_y_destino_sigue_escribible(tmp_path):
    """`table_info` no proyecta CHECK: el DDL completo tiene que discriminarlo."""
    ddl_hostil = INDICE_DDL.replace(
        "PRIMARY KEY (ledger, eid));",
        "CHECK(arrival IS NULL), PRIMARY KEY (ledger, eid));",
        1,
    )
    fuente = tmp_path / "hostil.sqlite"
    d = tmp_path / "data"; d.mkdir()
    destino = d / "llminbox.sqlite"
    base(fuente, ddl_indice=ddl_hostil); base(destino)
    antes = hashlib.sha256(destino.read_bytes()).hexdigest()

    host = corre(f'valida_snapshot "{fuente}" /data', tmp_path)
    assert host.returncode == 1 and "no acredita ser el índice" in host.stderr
    assert _clasificador_del_helper()(str(fuente))[0] == "indeterminado"

    helper = _corre_helper(tmp_path, destino, fuente,
                           hashlib.sha256(fuente.read_bytes()).hexdigest())
    assert helper.returncode == 1 and "DDL" in helper.stdout
    assert hashlib.sha256(destino.read_bytes()).hexdigest() == antes
    assert not list(d.glob(".m4-stage-*")) and not list(d.glob("*.rollback-*"))

    # No basta byte identity: el escritor normal conserva su semántica después del NO.
    con = sqlite3.connect(destino)
    con.execute("INSERT INTO entries(ledger,eid,arrival) VALUES('fleet','e1',1)")
    con.commit()
    assert con.execute("SELECT arrival FROM entries WHERE eid='e1'").fetchone()[0] == 1
    con.close()


@pytest.mark.parametrize("mutante", ["tabla", "columna", "indice", "columna_citada"])
def test_DDL_fisico_con_identificadores_ASCII_mixed_case_es_canonico(tmp_path, mutante):
    ddl = INDICE_DDL
    if mutante == "tabla":
        ddl = ddl.replace("CREATE TABLE entries", "CREATE TABLE EnTrIeS")
        ddl = ddl.replace(" ON entries(", " ON EnTrIeS(")
    elif mutante == "columna":
        ddl = re.sub(r"\barrival\b", "ArRiVaL", ddl)
    elif mutante == "indice":
        ddl = ddl.replace("CREATE INDEX i_arr", "CREATE INDEX I_ArR")
    else:
        ddl = re.sub(r"\barrival\b", '"ArRiVaL"', ddl)
    # También varía de verdad keywords/tipos (el control v3 sólo los pasaba a
    # uppercase, que ya era su forma original) y el espaciado del DDL persistido.
    ddl = re.sub(r"\b(CREATE|TABLE|INDEX|ON|PRIMARY|KEY|TEXT|INTEGER|REAL|NOT|NULL|DEFAULT|COLLATE|NOCASE)\b",
                 lambda m: m.group(1).swapcase(), ddl)
    ddl = re.sub(r"\s+", " \n\t", ddl)
    s = tmp_path / "benigno.sqlite"
    base(s, ddl_indice=ddl)
    host = corre(f'valida_snapshot "{s}" /data', tmp_path)
    assert host.returncode == 0, host.stderr
    assert _clasificador_del_helper()(str(s))[0] == "indice"
    con = sqlite3.connect(s)
    con.execute("INSERT INTO entries(ledger,eid,arrival) VALUES('fleet','mixed',1)")
    con.commit()
    assert con.execute("SELECT arrival FROM ENTRIES WHERE eid='mixed'").fetchone()[0] == 1
    con.close()


@pytest.mark.parametrize("original,unicode_no_equivalente", [
    ("cursors", "curſors"),       # casefold() convierte ſ a s
    ("canonical_kind", "canonical_Kind"),  # casefold() convierte K a k
    ("i_seq", "i_ſeq"),
])
def test_case_Unicode_NO_se_confunde_con_case_ASCII_de_identificador(
        tmp_path, original, unicode_no_equivalente):
    """Unicode casefold puede parecer ASCII, pero SQLite no da esa equivalencia."""
    ddl = INDICE_DDL.replace(original, unicode_no_equivalente)
    s = tmp_path / "unicode-no-equivalente.sqlite"
    base(s, ddl_indice=ddl)
    host = corre(f'valida_snapshot "{s}" /data', tmp_path)
    assert host.returncode == 1 and "no acredita ser el índice" in host.stderr
    assert _clasificador_del_helper()(str(s))[0] == "indeterminado"


def test_trigger_sobre_tabla_mixed_case_no_elude_la_guarda(tmp_path):
    ddl = INDICE_DDL.replace("CREATE TABLE entries", "CREATE TABLE EnTrIeS")
    ddl = ddl.replace(" ON entries(", " ON EnTrIeS(")
    s = tmp_path / "trigger-mixed.sqlite"
    base(s, ddl_indice=ddl)
    con = sqlite3.connect(s)
    con.execute("CREATE TRIGGER t_hostil AFTER INSERT ON EnTrIeS "
                "BEGIN DELETE FROM EnTrIeS; END")
    con.commit(); con.close()
    host = corre(f'valida_snapshot "{s}" /data', tmp_path)
    assert host.returncode == 1 and "no acredita ser el índice" in host.stderr
    assert _clasificador_del_helper()(str(s))[0] == "indeterminado"


def test_senal_de_journal_mixed_case_se_detecta_en_el_inventario(tmp_path):
    s = tmp_path / "journal-mixed.sqlite"
    base(s, clase="indeterminada", tablas=("PrInCiPaLs",))
    host = corre(f'valida_snapshot "{s}" /data', tmp_path)
    assert host.returncode == 1 and "JOURNAL" in host.stderr
    assert _clasificador_del_helper()(str(s))[0] == "journal"


def test_un_snapshot_TRUNCADO_no_pasa(tmp_path):
    s = tmp_path / "snap.sqlite"
    base(s)
    entero = s.read_bytes()
    s.write_bytes(entero[: len(entero) // 2])          # la mitad: sigue "no vacío"
    r = corre(f'valida_snapshot "{s}" /data', tmp_path)
    assert r.returncode == 1, "un truncado pasa `-s` y no es una base íntegra"


def test_un_snapshot_del_JOURNAL_no_pasa(tmp_path):
    """③ · el journal se rechaza AQUÍ, y el ⊕ es el test de arriba: la misma función
    acepta el índice. Sin ese ⊕, negarse siempre pasaría esta aserción."""
    s = tmp_path / "snap.sqlite"
    base(s, clase="journal")
    r = corre(f'valida_snapshot "{s}" /data', tmp_path)
    assert r.returncode == 1
    assert "JOURNAL" in r.stderr and "ADR-001" in r.stderr


def test_el_journal_RENOMBRADO_a_llminbox_sqlite_se_rechaza_igual(tmp_path):
    """⊖ QUE MATA LA VACUIDAD: si la clasificación fuera por nombre o por ruta, esto
    pasaría. El nombre lo elige quien despliega; la identidad no."""
    s = tmp_path / "llminbox.sqlite"
    base(s, clase="journal")
    r = corre(f'valida_snapshot "{s}" /data', tmp_path)
    assert r.returncode == 1 and "JOURNAL" in r.stderr


@pytest.mark.parametrize("meta,tablas,porque", [
    ({"durable_v": "3"}, (), "el sello del journal SOLO ya es señal"),
    ({}, ("events",), "una tabla del journal SOLA ya es señal"),
    ({"pepper_check": "x"}, INDICE_TABLAS, "sello de journal SOBRE tablas de índice"),
])
def test_una_sola_senal_de_journal_basta_para_rechazar(tmp_path, meta, tablas, porque):
    """Una base a medio construir tiene las tablas sin el sello; una desmochada tiene el
    sello sin las tablas. CUALQUIERA de las dos señales rechaza — la dirección segura de
    esta clasificación es negarse."""
    s = tmp_path / "snap.sqlite"
    base(s, clase="journal", tablas=tablas, meta=meta)
    r = corre(f'valida_snapshot "{s}" /data', tmp_path)
    assert r.returncode == 1, porque
    assert "JOURNAL" in r.stderr, porque


@pytest.mark.parametrize("meta,tablas", [
    ({}, INDICE_TABLAS),                       # tablas del índice sin su sello
    ({"schema_v": "h"}, ("entries",)),          # sello del índice sin sus tablas
    ({}, ()),                                   # ni lo uno ni lo otro
])
def test_lo_que_no_acredita_ser_el_indice_tampoco_pasa(tmp_path, meta, tablas):
    """Fail-closed en la tercera rama: no basta con NO ser journal."""
    s = tmp_path / "snap.sqlite"
    base(s, clase="otra", tablas=tablas, meta=meta)
    r = corre(f'valida_snapshot "{s}" /data', tmp_path)
    assert r.returncode == 1
    assert "no acredita ser el índice" in r.stderr


def test_un_fichero_que_no_es_sqlite_no_pasa(tmp_path):
    s = tmp_path / "snap.sqlite"
    s.write_bytes(b"esto no es una base de datos" * 100)
    r = corre(f'valida_snapshot "{s}" /data', tmp_path)
    assert r.returncode == 1


def test_un_snapshot_DENTRO_del_volumen_no_pasa(tmp_path):
    vol = tmp_path / "data"
    vol.mkdir()
    s = vol / "snap.sqlite"
    base(s)
    r = corre(f'valida_snapshot "{s}" "{vol}"', tmp_path)
    assert r.returncode == 1
    assert "DENTRO del volumen" in r.stderr


# ══ ⑤ RESTAURACIÓN: contra servicio vivo, jamás ═════════════════════════════════
@pytest.mark.parametrize("estado,espera", [("running", 1), ("exited", 0), ("created", 0)])
def test_no_se_restaura_sobre_un_servicio_vivo(tmp_path, estado, espera):
    doble_docker(tmp_path, f'''
if [ "$1" = "inspect" ]; then
  printf '[{{"Id":"c1","Created":"x","State":{{"StartedAt":"y","Status":"{estado}"}},"RestartCount":0,"Image":"i","Config":{{"Image":"n"}},"Mounts":[]}}]\\n'
  exit 0
fi
exit 0
''')
    r = corre('exige_servicio_parado inst; echo "rc=$?"', tmp_path)
    assert f"rc={espera}" in r.stdout, r.stderr


# ══ ⑥ EMERGENCIA: nunca puede salir 0 ═══════════════════════════════════════════
def test_una_puerta_en_fallo_hace_que_el_veredicto_salga_1(tmp_path):
    """El agujero exacto: `--emergencia-sin-outbox` dejaba `outbox` en fallo, el
    veredicto salía `ok:false` y el guion imprimía ACREDITADA y salía 0."""
    v = tmp_path / "ver.json"
    r = corre(f'''
veredicto_anota destino ok
veredicto_anota outbox fallo "SALTADO por --emergencia-sin-outbox"
veredicto_anota gate ok
veredicto_escribe "{v}" reversion inst destino outbox gate; echo "rc=$?"
''', tmp_path)
    assert "rc=1" in r.stdout, "una emergencia declarada NO puede salir 0"
    d = json.loads(v.read_text())
    assert d["ok"] is False and "outbox" in d["fallidas"]


def test_control_positivo_todas_las_puertas_ok_sale_0(tmp_path):
    v = tmp_path / "ver.json"
    r = corre(f'''
veredicto_anota destino ok
veredicto_anota outbox ok
veredicto_escribe "{v}" reversion inst destino outbox; echo "rc=$?"
''', tmp_path)
    assert "rc=0" in r.stdout, "sin este ⊕ el rojo de arriba no discrimina"
    assert json.loads(v.read_text())["ok"] is True


def test_una_puerta_que_no_se_corrio_cuenta_como_fallo(tmp_path):
    v = tmp_path / "ver.json"
    r = corre(f'''
veredicto_anota destino ok
veredicto_escribe "{v}" reversion inst destino outbox journal; echo "rc=$?"
''', tmp_path)
    assert "rc=1" in r.stdout
    d = json.loads(v.read_text())
    assert d["puertas"]["outbox"]["estado"] == "no_medido"
    assert set(d["fallidas"]) == {"outbox", "journal"}


# ══ ⑦ CERROJO DE FLUJO ══════════════════════════════════════════════════════════
def test_dos_flujos_a_la_vez_sobre_la_misma_instancia_se_rechazan(tmp_path):
    r = corre('''
cerrojo_flujo pilotaje inst || echo "primero=rechazado"
( . "''' + str(LIB) + '''"; cerrojo_flujo pilotaje inst; echo "segundo=$?" )
''', tmp_path)
    assert "segundo=1" in r.stdout, f"el segundo flujo entró\n{r.stdout}\n{r.stderr}"


def test_control_positivo_otra_instancia_no_choca(tmp_path):
    r = corre('''
cerrojo_flujo pilotaje inst-a
( . "''' + str(LIB) + '''"; cerrojo_flujo pilotaje inst-b; echo "otra=$?" )
''', tmp_path)
    assert "otra=0" in r.stdout, "el cerrojo es POR INSTANCIA, no global"


# ══ ⑧ ARTEFACTO DE EVIDENCIA: hashes y rc ═══════════════════════════════════════
def test_el_veredicto_sella_hashes_y_rc(tmp_path):
    a = tmp_path / "a.json"
    a.write_text('{"x":1}')
    v = tmp_path / "ver.json"
    corre(f'''
veredicto_anota p ok
artefacto_anota "{a}"
artefacto_anota "{tmp_path}/no-existe.json"
rc_anota restore_swap 0
rc_anota restore_cp 3
veredicto_escribe "{v}" reversion inst p
''', tmp_path)
    d = json.loads(v.read_text())
    assert d["esquema"] == 2
    por_ruta = {x["ruta"]: x for x in d["artefactos"]}
    import hashlib
    assert por_ruta[str(a)]["sha256"] == hashlib.sha256(a.read_bytes()).hexdigest()
    assert por_ruta[str(a)]["bytes"] == 7
    # Un artefacto AUSENTE se publica como hecho, no se omite: omitirlo lo haría
    # indistinguible de uno que nunca se pidió.
    assert por_ruta[f"{tmp_path}/no-existe.json"]["sha256"] == "AUSENTE"
    assert d["rc"] == {"restore_swap": 0, "restore_cp": 3}


# ══ ⑨ EXDEV: la etapa TIENE que compartir filesystem con el destino ═════════════
def test_una_etapa_en_otro_montaje_no_permite_publicacion_atomica(tmp_path):
    """Falsador del /tmp separado. `os.replace` entre sistemas de ficheros distintos
    levanta `EXDEV`: llamar «swap atómico» a un cruce de montaje promete una garantía
    que el kernel no da. No se puede montar un tmpfs en un test, así que se prueba la
    ASERCIÓN que el guion corre dentro del contenedor: `st_dev` iguales."""
    destino = tmp_path / "data" / "llminbox.sqlite"
    destino.parent.mkdir()
    base(destino)
    fuera = tmp_path / "tmp"
    fuera.mkdir()
    etapa_mala = fuera / "stage.sqlite"
    base(etapa_mala)
    etapa_buena = destino.parent / ".m4-stage-x.sqlite"
    base(etapa_buena)
    mismo = lambda p: os.stat(p).st_dev == os.stat(destino.parent).st_dev
    # En este arnés los dos comparten fs; lo que se fija es la FORMA de la aserción,
    # que es la que el guion evalúa dentro del contenedor, donde /tmp y /data NO lo son.
    assert mismo(etapa_buena), "la etapa junto al destino comparte st_dev"
    import errno
    try:
        os.replace(etapa_mala, destino)
    except OSError as e:                                    # pragma: no cover
        assert e.errno == errno.EXDEV
    # Y la aserción de permisos que acompaña a la etapa:
    os.chmod(etapa_buena, 0o600)
    assert oct(os.stat(etapa_buena).st_mode)[-3:] == "600"


# ══ ⑩ BUNDLE: un -wal obsoleto NO puede sobrevivir a la publicación ═════════════
def test_un_wal_pendiente_del_almacen_viejo_no_sobrevive_a_la_restauracion(tmp_path):
    """Falsador del WAL pendiente. Una base SQLite es la terna db+`-wal`+`-shm`. Si se
    publica sólo el `.sqlite`, al abrir se REPRODUCE el `-wal` del almacén anterior
    sobre la base nueva: corrupción silenciosa con cara de restauración correcta."""
    d = tmp_path / "data"
    d.mkdir()
    destino = d / "llminbox.sqlite"
    base(destino)
    # dejo un -wal PENDIENTE del almacén viejo (autocheckpoint apagado)
    # ⚠️ EL `-wal` NO SOBREVIVE A UN `close()` LIMPIO: SQLite hace checkpoint y lo
    # borra. Mi primera versión cerraba, así que el arnés no reproducía el escenario y
    # los dos tests fallaban por MI causa, no por la cura. Se sujeta una segunda
    # conexión abierta: mientras haya un lector, el checkpoint de cierre no puede
    # retirar el WAL, que es exactamente el estado que deja un proceso vivo.
    con = sqlite3.connect(destino)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA wal_autocheckpoint=0")
    con.execute("INSERT INTO entries(ledger,eid) VALUES('l','999999')")
    con.commit()
    retenedor = sqlite3.connect(destino)
    retenedor.execute("BEGIN"); retenedor.execute("SELECT COUNT(*) FROM entries").fetchone()
    con.close()
    assert (d / "llminbox.sqlite-wal").exists(), "el arnés no reprodujo el WAL pendiente"

    etapa = d / ".m4-stage-nuevo.sqlite"
    base(etapa)
    sello = "20260905T000000Z"

    # LA MISMA SECUENCIA que corre el guion dentro del contenedor.
    for suf in ("", "-wal", "-shm"):
        p = Path(str(destino) + suf)
        if p.exists():
            os.replace(p, str(p) + ".previo-" + sello)
    os.replace(etapa, destino)
    fd = os.open(destino, os.O_RDONLY); os.fsync(fd); os.close(fd)
    dfd = os.open(d, os.O_RDONLY); os.fsync(dfd); os.close(dfd)

    retenedor.close()
    vivos = [s for s in ("-wal", "-shm") if Path(str(destino) + s).exists()]
    assert not vivos, f"sidecars obsoletos sobrevivieron: {vivos}"
    # el conjunto previo queda ARCHIVADO, no borrado: es lo único a lo que volver
    assert Path(str(destino) + "-wal.previo-" + sello).exists()
    # y la base publicada NO contiene la fila que sólo vivía en el WAL viejo
    con = sqlite3.connect(destino)
    assert con.execute("SELECT COUNT(*) FROM entries WHERE eid='999999'").fetchone()[0] == 0
    con.close()


def test_control_negativo_si_no_se_retiran_los_sidecars_el_wal_viejo_se_reproduce(tmp_path):
    """⊕ del anterior: sin retirar el `-wal`, la fila del almacén VIEJO reaparece en la
    base nueva. Sin este control, el verde de arriba no distingue «los retiré» de «no
    había ninguno»."""
    d = tmp_path / "data"
    d.mkdir()
    destino = d / "llminbox.sqlite"
    base(destino)
    # ⚠️ EL `-wal` NO SOBREVIVE A UN `close()` LIMPIO: SQLite hace checkpoint y lo
    # borra. Mi primera versión cerraba, así que el arnés no reproducía el escenario y
    # los dos tests fallaban por MI causa, no por la cura. Se sujeta una segunda
    # conexión abierta: mientras haya un lector, el checkpoint de cierre no puede
    # retirar el WAL, que es exactamente el estado que deja un proceso vivo.
    con = sqlite3.connect(destino)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA wal_autocheckpoint=0")
    con.execute("INSERT INTO entries(ledger,eid) VALUES('l','999999')")
    con.commit()
    retenedor = sqlite3.connect(destino)
    retenedor.execute("BEGIN"); retenedor.execute("SELECT COUNT(*) FROM entries").fetchone()
    con.close()
    etapa = d / ".m4-stage-nuevo.sqlite"
    base(etapa)
    assert (d / "llminbox.sqlite-wal").exists(), "el arnés no dejó WAL pendiente"
    os.replace(etapa, destino)          # publico SIN retirar sidecars: el defecto
    retenedor.close()
    con = sqlite3.connect(destino)
    n = con.execute("SELECT COUNT(*) FROM entries WHERE eid='999999'").fetchone()[0]
    con.close()
    assert n == 1, ("sin retirar el -wal, SQLite reproduce el almacén VIEJO sobre la "
                    "base nueva; si esto fuera 0, el falsador de arriba no mediría nada")


# ══ ⑪ TRAVESÍA DE RUTAS EN EL CERROJO ═══════════════════════════════════════════
# `INST` sólo se comprobaba «no vacío» y entraba crudo en la ruta del cerrojo, cuya
# limpieza corría `rm -rf`. Un nombre con `/` o `..` dirigía ese borrado fuera de la
# raíz de cerrojos.
@pytest.mark.parametrize("nombre,acepta", [
    ("llminbox-pilot", True),
    ("m4_test.1", True),
    ("../../etc", False),
    ("a/../../b", False),
    ("con/barra", False),
    ("MAYUSCULA", False),
    ("-empieza-mal", False),
    ("", False),
    ("x" * 65, False),
])
def test_la_gramatica_de_instancia_corta_la_travesia(tmp_path, nombre, acepta):
    r = corre(f'instancia_valida {json.dumps(nombre)}; echo "rc=$?"', tmp_path)
    assert f"rc={0 if acepta else 1}" in r.stdout, f"{nombre!r}\n{r.stderr}"


def test_un_nombre_con_travesia_no_crea_cerrojo_fuera_de_la_raiz(tmp_path):
    raiz = tmp_path / "locks"
    raiz.mkdir()
    fuera = tmp_path / "fuera"
    fuera.mkdir()
    centinela = fuera / "NO-BORRAR"
    centinela.write_text("si esto desaparece, el rm -rf salió de la raíz")
    r = corre('cerrojo_flujo pilotaje "../fuera"; echo "rc=$?"', tmp_path,
              {"LLMI_LOCK_DIR": str(raiz)})
    assert "rc=1" in r.stdout, "la travesía tenía que rechazarse antes de crear nada"
    assert centinela.exists(), "el centinela desapareció: la limpieza salió de la raíz"
    assert list(raiz.iterdir()) == [], f"quedó basura en la raíz: {list(raiz.iterdir())}"


def test_el_componente_de_ruta_del_cerrojo_es_un_hash_no_el_nombre(tmp_path):
    raiz = tmp_path / "locks"
    raiz.mkdir()
    corre('cerrojo_flujo pilotaje instancia-legible; sleep 0', tmp_path,
          {"LLMI_LOCK_DIR": str(raiz)})
    # El cerrojo se suelta al salir; lo que se fija es que el NOMBRE no aparezca en
    # ninguna ruta creada bajo la raíz mientras existe.
    r = corre(f'''
cerrojo_flujo pilotaje instancia-legible
ls -1A "{raiz}"
''', tmp_path, {"LLMI_LOCK_DIR": str(raiz)})
    assert "instancia-legible" not in r.stdout, (
        f"el nombre entra en la ruta del cerrojo:\n{r.stdout}")
    assert re.search(r"\.m4-flujo-[0-9a-f]{32}\.lock", r.stdout), r.stdout


def test_dos_cerrojos_seguidos_no_dejan_huerfano_el_primero(tmp_path):
    """El `trap ... EXIT` del segundo REEMPLAZABA al del primero."""
    raiz = tmp_path / "locks"
    raiz.mkdir()
    r = corre(f'''
cerrojo_flujo pilotaje inst-a
cerrojo_flujo pilotaje inst-b
echo "durante=$(ls -1A "{raiz}" | wc -l | tr -d ' ')"
''', tmp_path, {"LLMI_LOCK_DIR": str(raiz)})
    assert "durante=2" in r.stdout, r.stdout
    assert list(raiz.iterdir()) == [], (
        f"tras salir quedaron cerrojos huérfanos: {[p.name for p in raiz.iterdir()]}")


def test_lease_de_pid_muerto_no_bloquea_reanudacion(tmp_path):
    raiz = tmp_path / "locks"; raiz.mkdir(); inst = "inst-resume"
    h = hashlib.sha256(inst.encode()).hexdigest()[:32]
    lock = raiz / f".m4-flujo-{h}.lock"; lock.mkdir()
    (lock / "quien").write_text(json.dumps({"pid": 99999999, "op": "reversion",
                                             "instancia": inst, "cuando": "x"}))
    r = corre(f'cerrojo_flujo reversion {inst}; echo rc=$?', tmp_path,
              {"LLMI_LOCK_DIR": str(raiz)})
    assert "rc=0" in r.stdout and list(raiz.iterdir()) == []


# ══ ⑫ «NO MEDIBLE» NO ES «AUSENTE» ══════════════════════════════════════════════
@pytest.mark.parametrize("cuerpo_docker,espera,porque", [
    ('echo "Error: No such object: inst" >&2; exit 1', 0,
     "⊕ Docker lo dice explícitamente: ausente, se puede restaurar"),
    ('echo "Cannot connect to the Docker daemon" >&2; exit 1', 1,
     "daemon caído: NO SÉ si está vivo, y eso no autoriza"),
    ('echo "context deadline exceeded" >&2; exit 1', 1,
     "timeout: tampoco autoriza"),
])
def test_inspect_no_medible_no_abre_la_restauracion(tmp_path, cuerpo_docker, espera, porque):
    doble_docker(tmp_path, f'if [ "$1" = "inspect" ]; then {cuerpo_docker}\nfi\nexit 0\n')
    r = corre('exige_servicio_parado inst; echo "rc=$?"', tmp_path)
    assert f"rc={espera}" in r.stdout, f"{porque}\n{r.stdout}\n{r.stderr}"


# ══ ⑬ EL SNAPSHOT SE COMPARA CONTRA EL SHA ANCLADO ══════════════════════════════
def test_un_sqlite_INTEGRO_PERO_AJENO_no_pasa(tmp_path):
    """`integrity_check` dice que una base está SANA, no que sea LA base."""
    import hashlib
    buena = tmp_path / "buena.sqlite"
    base(buena)
    esperado = hashlib.sha256(buena.read_bytes()).hexdigest()
    ajena = tmp_path / "ajena.sqlite"
    base(ajena)
    ajena_con = sqlite3.connect(ajena)
    ajena_con.execute("INSERT INTO entries(ledger,eid) VALUES('l','4242')")  # íntegra, pero otra
    ajena_con.commit()
    ajena_con.close()
    r_ok = corre(f'valida_snapshot "{buena}" /data "{esperado}"', tmp_path)
    assert r_ok.returncode == 0, f"⊕ el snapshot anclado tiene que pasar\n{r_ok.stderr}"
    assert json.loads(r_ok.stdout)["sha_verificado_contra_ancla"] is True
    r_no = corre(f'valida_snapshot "{ajena}" /data "{esperado}"', tmp_path)
    assert r_no.returncode == 1, "un SQLite íntegro pero ajeno NO puede pasar"
    assert "NO es el que se ancló" in r_no.stderr


# ══ ⑭ UN CURL FALLIDO NO DEJA ARTEFACTO SELLADO ═════════════════════════════════
def test_un_artefacto_vacio_no_se_sella_con_el_sha_del_vacio(tmp_path):
    vacio = tmp_path / "vacio.json"
    vacio.write_bytes(b"")
    lleno = tmp_path / "lleno.json"
    lleno.write_text("{}")
    v = tmp_path / "ver.json"
    corre(f'''
veredicto_anota p ok
artefacto_anota "{vacio}"
artefacto_anota "{lleno}"
veredicto_escribe "{v}" pilotaje inst p
''', tmp_path)
    por_ruta = {x["ruta"]: x for x in json.loads(v.read_text())["artefactos"]}
    assert por_ruta[str(vacio)]["sha256"] == "VACIO", (
        "el sha del fichero vacío (e3b0c442…) es válido y mentiroso: dice «aquí está la "
        "evidencia» sobre una lectura que no ocurrió")
    assert len(por_ruta[str(lleno)]["sha256"]) == 64


# ══ ⑮ UNA SOLA LECTURA DE /health POR FASE ══════════════════════════════════════
def test_los_gates_de_una_fase_comparten_una_unica_lectura(tmp_path):
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    cuenta = tmp_path / "llamadas"
    (d / "curl").write_text(
        f"#!/bin/sh\necho x >> {cuenta}\ncat <<'JSON'\n{SANO}\nJSON\n")
    (d / "curl").chmod(0o755)
    r = corre('''
gate_salud http://x >/dev/null 2>&1
gate_integridad http://x >/dev/null 2>&1
gate_artefacto http://x >/dev/null 2>&1
gate_journal http://x >/dev/null 2>&1
echo "listo"
''', tmp_path)
    assert "listo" in r.stdout, r.stderr
    n = len(cuenta.read_text().splitlines()) if cuenta.exists() else 0
    assert n == 1, (f"{n} lecturas de /health para decidir una fase: son {n} instantes "
                    "distintos presentados como uno")


def test_salud_olvida_fuerza_una_relectura_cuando_el_mundo_cambia(tmp_path):
    """⊕ del anterior: si la caché no se pudiera invalidar, tras recrear se decidiría
    con la foto del contenedor ANTERIOR."""
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    cuenta = tmp_path / "llamadas"
    (d / "curl").write_text(
        f"#!/bin/sh\necho x >> {cuenta}\ncat <<'JSON'\n{SANO}\nJSON\n")
    (d / "curl").chmod(0o755)
    corre('gate_salud http://x >/dev/null 2>&1; salud_olvida; gate_salud http://x >/dev/null 2>&1',
          tmp_path)
    assert len(cuenta.read_text().splitlines()) == 2


def test_cache_salud_es_privada_y_la_clave_es_sha_exacta(tmp_path):
    doble_curl(tmp_path, SANO)
    r = corre('''
gate_salud http://x/a >/dev/null
modo="$(python3 -c 'import os,stat,sys;print(oct(stat.S_IMODE(os.stat(sys.argv[1]).st_mode))[2:])' "$_M4_SALUD_DIR")"
printf 'dir=%s modo=%s k1=%s k2=%s\n' "$_M4_SALUD_DIR" "$modo" \
  "$(_salud_key http://x/a)" "$(_salud_key http://x_a)"
''', tmp_path)
    linea = r.stdout.strip().splitlines()[-1]
    assert "modo=700" in linea
    m = re.search(r"k1=([0-9a-f]{64}) k2=([0-9a-f]{64})", linea)
    assert m and m.group(1) != m.group(2), "dos APIs distintas no colisionan por normalización lossy"
    assert str(tmp_path / "m4-salud.") in linea


def test_plan_publicado_no_reemplaza_preplantado(tmp_path):
    p = tmp_path / "plan.json"; p.write_text("CENTINELA")
    r = corre(f'plan_escribe "{p}" "{{\\"x\\":1}}"; echo rc=$?', tmp_path)
    assert "rc=1" in r.stdout and p.read_text() == "CENTINELA"


def test_evidencia_y_veredicto_preplantados_no_se_pisan(tmp_path):
    a = tmp_path / "artefacto.json"; a.write_text("CENTINELA")
    r = corre(f'texto_publica "{a}" otro; echo rc=$?', tmp_path)
    assert "rc=1" in r.stdout and a.read_text() == "CENTINELA"
    v = tmp_path / "veredicto.json"; v.write_text("CENTINELA")
    r = corre(f'veredicto_anota p ok; veredicto_escribe "{v}" op inst p; echo rc=$?', tmp_path)
    assert "rc=1" in r.stdout and v.read_text() == "CENTINELA"


def test_state_es_o_excl_confinado_y_ligado_al_token(tmp_path):
    evid = tmp_path / "evid"; evid.mkdir(mode=0o700)
    token = "a" * 64
    r = corre(f'evidencia_prepara "{evid}"; estado_publica "{evid}" {token} pre_restore inst', tmp_path)
    marca = Path(r.stdout.strip())
    assert marca.parent == evid and token in marca.name
    assert json.loads(marca.read_text())["token"] == token
    r2 = corre(f'estado_publica "{evid}" {token} pre_restore inst; echo rc=$?', tmp_path)
    assert "rc=1" in r2.stdout


def test_evidencias_ligadas_rechazan_un_byte_cambiado(tmp_path):
    plan, _ = _plan_v2(tmp_path)
    p = json.loads(plan.read_text())
    Path(p["evidencias"]["health"]["ruta"]).write_text("{}")
    r = corre(f'evidencias_verifica {json.dumps(json.dumps(p))}; echo rc=$?', tmp_path)
    assert "rc=1" in r.stdout


# ══ ⑯ DRY-RUN: veredicto NO acreditado, y su rc ═════════════════════════════════
def test_dry_run_deja_veredicto_no_acreditado(tmp_path):
    """Salir 0 sin escribir nada hacía un `--seco` indistinguible de una corrida
    certificada para cualquier automatismo que encadene por rc."""
    v = tmp_path / "ver.json"
    r = corre(f'''
veredicto_anota destino ok
veredicto_anota gate no_medido "dry-run"
veredicto_escribe "{v}" reversion inst destino gate; echo "rc=$?"
''', tmp_path)
    assert "rc=1" in r.stdout
    d = json.loads(v.read_text())
    assert d["ok"] is False and d["puertas"]["gate"]["estado"] == "no_medido"


# ══ ⑰ ORDEN: el restore va ANTES del arranque ═══════════════════════════════════
def test_el_guion_no_arranca_antes_de_exigir_el_servicio_parado(tmp_path):
    """Falsador de ORDEN sobre el fuente, que es donde vive el defecto: `llmi up`
    estaba ANTES del bloque `--datos`, así que `exige_servicio_parado` encontraba
    siempre `running` y toda reversión con datos era inalcanzable."""
    fuente = (RAIZ / "scripts" / "m4-rollback.sh").read_text()
    i_up = fuente.index("./llmi up")
    i_parado = fuente.index("ultimo_stopped_plan")
    assert i_parado < i_up, (
        "`llmi up` aparece ANTES de exigir el servicio parado: la ruta --datos vuelve a "
        "ser inalcanzable")
    i_restore = fuente.index("helper_volumen ")
    assert i_restore < i_up, "el helper corre DESPUÉS de arrancar: orden inválido"


def test_recreate_no_se_anota_ok_antes_del_restore(tmp_path):
    fuente = (RAIZ / "scripts" / "m4-rollback.sh").read_text()
    i_post = fuente.index('estado_publica "$EVID" "$REANUDA" post_restore')
    i_restore = fuente.index('estado_publica "$EVID" "$REANUDA" restored')
    i_tag = fuente.index('"$DOCKER" tag "$DESTINO_ID" "$IMG_HELPER"')
    assert i_restore < i_post, "se entra en post_restore antes de acreditar restored"
    assert i_tag < i_post, "post_restore afirma que el tag se movió ANTES de moverlo"


def test_sin_autoridad_para_parar_se_emite_plan_con_token(tmp_path):
    """El token sustituyó al nonce: es el sha256 del propio fichero y no vive dentro."""
    fuente = (RAIZ / "scripts" / "m4-rollback.sh").read_text()
    assert "--reanudar" in fuente and "plan_escribe" in fuente
    assert "NONCE" not in fuente, "queda un nonce interno, que no sella nada"
    # La FASE 1 no arranca nada: emite el plan y para. (Antes se comparaban índices de
    # texto, que tras el dispatch dejaron de significar orden de ejecución: `llmi up`
    # vive en la fase 2, que está ANTES en el fichero y DESPUÉS en el tiempo.)
    fase1 = fuente[fuente.index("# ══ FASE 1"):]
    assert "./llmi up" not in fase1, "la fase 1 arranca el servicio: no debe"
    assert 'PLAN="$EVID/$SELLO-plan.json"' in fase1 and "plan_escribe" in fase1
    assert fase1.rstrip().endswith("exit 1"), "la fase 1 nunca puede acreditar"




# ══ ⑱ DISPATCH DE FASE: la fase 2 no puede exigir requisitos de la fase 1 ═══════
RB = RAIZ / "scripts" / "m4-rollback.sh"
HELPER = RAIZ / "scripts" / "m4-restore-helper.py"


def test_el_dispatch_ocurre_justo_tras_el_parseo(tmp_path):
    """El defecto estructural: la cabecera exigía `--a` y corría outbox/journal/health
    —gates que necesitan la app VIVA— antes de mirar si venía un plan. La fase 2, que
    requiere la app PARADA, era inalcanzable por construcción."""
    f = RB.read_text()
    i_dispatch = f.index("if [ -n \"$REANUDA\" ] || [ -n \"$PLAN_RUTA\" ]; then FASE=2")
    for gate in ("gate_outbox", "gate_journal", 'falta --a <imagen'):
        assert i_dispatch < f.index(gate), (
            f"`{gate}` corre ANTES del dispatch: la fase 2 no llega")


def test_la_fase_2_rechaza_parametros_de_cliente(tmp_path):
    f = RB.read_text()
    i = f.index('[ -z "$DESTINO$SNAPSHOT$DIGEST_ESP" ]')
    assert "NO acepta --a" in f[i:i + 300]


def test_la_fase_2_no_llama_a_los_gates_de_app_viva(tmp_path):
    f = RB.read_text()
    fase2 = f[f.index('if [ "$FASE" -eq 2 ]; then'):f.index("# ══ FASE 1")]
    for gate in ("gate_outbox", "gate_journal"):
        assert gate not in fase2, f"la fase 2 llama a `{gate}`, que exige la app viva"
    assert "gate_salud" in fase2, "la salud POST-recreate sí tiene que medirse"


def test_el_plan_congela_mounts_contenedor_e_imagen(tmp_path):
    f = RB.read_text()
    for campo in ("volumen", "mountpoint", "db_interna", "contenedor_id",
                  "imagen_digest", "imagen_helper", "snapshot_sha"):
        assert f'"{campo}"' in f, f"el plan no congela `{campo}`"
    assert "montajes[]? | select(.destino==$m)" in (f + LIB.read_text()), (
        "el volumen no se deriva por inspect")


def test_la_fase_2_comprueba_que_los_mounts_siguen_siendo_los_congelados(tmp_path):
    f = RB.read_text()
    assert 'ultimo_stopped_plan "$INST" "$CID_PLAN" "$IMG_DIGEST" "$VOLUMEN"' in f
    lib = LIB.read_text()
    assert '.estado=="exited" and .id==$cid and .imagen==$img' in lib
    assert 'volumen_del_mount "$ev" "$mnt"' in lib and 'db_del_contenedor "$ev" "$mnt"' in lib


# ══ ⑲ END-TO-END SIMULADO: token+plan sobre app PARADA llega al helper ═════════
def _plan_v2(tmp_path, **campos):
    d = tmp_path / "data"
    d.mkdir(exist_ok=True)
    evid = tmp_path / "evid"; evid.mkdir(exist_ok=True)
    snap = evid / "snap.sqlite"
    base(snap)
    evidencias = {}
    for nombre, contenido in (("health", SANO.encode()), ("outbox", b'{"disponible":true}'),
                              ("journal", b'{"disponible":true}'), ("inspect", b'{"id":"cid-1"}\n'),
                              ("helper", HELPER.read_bytes())):
        ruta = evid / f"{nombre}.json"; ruta.write_bytes(contenido)
        evidencias[nombre] = {"ruta": str(ruta), "sha256": hashlib.sha256(contenido).hexdigest(),
                              "bytes": len(contenido)}
    evidencias["snapshot"] = {"ruta": str(snap),
                               "sha256": hashlib.sha256(snap.read_bytes()).hexdigest(),
                               "bytes": snap.stat().st_size}
    p = {"esquema": 4, "operacion": "reversion", "instancia": "inst-m4",
         "restaura_indice": True,
         "sello": "S", "destino_imagen": "img@sha256:" + "ab" * 32,
         "destino_digest": "sha256:" + "ab" * 32, "destino_image_id": "sha256:" + "ab" * 32,
         "snapshot": str(snap),
         "snapshot_sha": hashlib.sha256(snap.read_bytes()).hexdigest(),
         "volumen": "vol-m4", "mountpoint": "/data",
         "db_interna": "/data/llminbox.sqlite", "contenedor_id": "cid-1",
         "imagen_digest": "sha256:" + "cd" * 32, "imagen_helper": "img:local",
         "imagen_helper_digest": "sha256:" + "cd" * 32,
         "evid_root": str(evid),
         "volumen_identidad": {"name": "vol-m4", "driver": "local", "mountpoint": "/vm/vol-m4",
                                "scope": "local", "created_at": "C", "labels": {}, "options": {}},
         "evidencias": evidencias}
    p.update(campos)
    f = evid / "plan.json"
    f.write_text(json.dumps(p, sort_keys=True, indent=1))
    return f, hashlib.sha256(f.read_bytes()).hexdigest()


def _entorno_fase2(tmp_path, estado="exited", registro=None):
    binario = tmp_path / "bin"
    binario.mkdir(exist_ok=True)
    reg = registro or (tmp_path / "docker.log")
    (binario / "docker").write_text(f'''#!/bin/sh
printf '%s\\n' "$*" >> "{reg}"
if [ "$1" = "inspect" ]; then
  printf '[{{"Id":"cid-1","Created":"c","State":{{"StartedAt":"s","Status":"{estado}"}},"RestartCount":0,"Image":"sha256:{"cd"*32}","Config":{{"Image":"img:local","Env":["LLMINBOX_DB=/data/llminbox.sqlite","LLMINBOX_TOKEN=SECRETO-NO-DEBE-SALIR"]}},"Mounts":[{{"Type":"volume","Name":"vol-m4","Source":"/var/lib/docker/volumes/vol-m4/_data","Destination":"/data","RW":true}}]}}]\\n'
  exit 0
fi
if [ "$1 $2" = "image inspect" ]; then
  if [ "$3" = "img:local" ] || [ "$3" = "sha256:{"cd"*32}" ]; then d="sha256:{"cd"*32}"; else d="sha256:{"ab"*32}"; fi
  printf '[{{"Id":"%s","RepoDigests":[]}}]\n' "$d"; exit 0
fi
if [ "$1 $2" = "volume inspect" ]; then
  printf '[{{"Name":"vol-m4","Driver":"local","Mountpoint":"/vm/vol-m4","Scope":"local","CreatedAt":"C","Labels":{{}},"Options":{{}}}}]\n'; exit 0
fi
if [ "$1" = "run" ]; then echo "OK {{}}"; exit 0; fi
exit 0
''')
    (binario / "docker").chmod(0o755)
    (binario / "curl").write_text(f"#!/bin/sh\ncat <<'JSON'\n{SANO}\nJSON\n")
    (binario / "curl").chmod(0o755)
    (binario / "llmi").write_text("#!/bin/sh\nexit 0\n")
    (binario / "llmi").chmod(0o755)
    return binario, reg


def _corre_rollback(tmp_path, args, binario, extra=None):
    env = {"PATH": f"{binario}:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
           "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
           "LLMINBOX_NAME": "inst-m4", "M4_EVIDENCIA": str(tmp_path / "evid"),
           "LLMI_LOCK_DIR": str(tmp_path)}
    env.update(extra or {})
    # `./llmi` se invoca por ruta relativa desde la raíz del repo: se interpone un doble.
    return subprocess.run(["bash", str(RB), *args], capture_output=True, text=True,
                          cwd=RAIZ, env=env, timeout=90)


def test_fase2_con_solo_token_y_plan_sobre_app_parada_alcanza_el_helper(tmp_path):
    """El end-to-end que faltaba: NO se pasa `--a` ni `--datos`, la app está parada, y
    el flujo tiene que llegar hasta la ejecución del helper."""
    plan, token = _plan_v2(tmp_path)
    binario, reg = _entorno_fase2(tmp_path, estado="exited")
    r = _corre_rollback(tmp_path, ["--reanudar", token, "--plan", str(plan)], binario)
    llamadas = reg.read_text() if reg.exists() else ""
    assert "run --rm" in llamadas, (
        f"nunca se llamó al helper con `docker run`.\nSTDOUT:\n{r.stdout}\n"
        f"STDERR:\n{r.stderr}\nDOCKER:\n{llamadas}")
    assert "--entrypoint python3" in llamadas
    assert "-v vol-m4:/data" in llamadas, "el helper no monta el volumen congelado"
    assert ("sha256:" + "cd" * 32 + " -c") in llamadas, "docker run no usa image ID inmutable"
    helper_sha = json.loads(plan.read_text())["evidencias"]["helper"]["sha256"]
    assert helper_sha in llamadas, "el wrapper no liga los bytes ejecutados al SHA del plan"
    # y no ha exigido nada de fase 1
    assert "falta --a" not in r.stderr


def test_fase2_sin_plan_no_arranca_y_no_pide_gates_de_app_viva(tmp_path):
    binario, _ = _entorno_fase2(tmp_path)
    r = _corre_rollback(tmp_path, ["--reanudar", "x" * 64], binario)
    assert r.returncode == 2
    assert "--plan" in r.stderr


def test_fase2_con_token_arbitrario_no_toca_nada(tmp_path):
    plan, _ = _plan_v2(tmp_path)
    binario, reg = _entorno_fase2(tmp_path)
    r = _corre_rollback(tmp_path, ["--reanudar", "0" * 64, "--plan", str(plan)], binario)
    assert r.returncode == 1
    assert "TOKEN QUE NO CORRESPONDE" in r.stderr
    assert "run --rm" not in (reg.read_text() if reg.exists() else "")


def test_fase2_con_plan_alterado_invalida_el_token(tmp_path):
    plan, token = _plan_v2(tmp_path)
    plan.write_text(plan.read_text().replace('"instancia": "inst-m4"',
                                             '"instancia": "inst-m4" '))
    binario, reg = _entorno_fase2(tmp_path)
    r = _corre_rollback(tmp_path, ["--reanudar", token, "--plan", str(plan)], binario)
    assert r.returncode == 1 and "TOKEN QUE NO CORRESPONDE" in r.stderr
    assert "run --rm" not in (reg.read_text() if reg.exists() else "")


def test_fase2_con_la_app_VIVA_se_niega_antes_del_helper(tmp_path):
    plan, token = _plan_v2(tmp_path)
    binario, reg = _entorno_fase2(tmp_path, estado="running")
    r = _corre_rollback(tmp_path, ["--reanudar", token, "--plan", str(plan)], binario)
    assert r.returncode == 1
    assert "último inspect" in r.stderr
    assert "run --rm" not in (reg.read_text() if reg.exists() else "")


def test_fallo_post_restore_se_reanuda_sin_repetir_restore(tmp_path):
    plan, token = _plan_v2(tmp_path)
    binario, reg = _entorno_fase2(tmp_path, estado="exited")
    r1 = _corre_rollback(tmp_path, ["--reanudar", token, "--plan", str(plan)], binario)
    r2 = _corre_rollback(tmp_path, ["--reanudar", token, "--plan", str(plan)], binario)
    assert r1.returncode == r2.returncode == 1
    llamadas = reg.read_text()
    assert llamadas.count("run --rm") == 1, "post_restore no puede volver a tocar datos"
    assert list((tmp_path / "evid").glob(f"state-{token}-post_restore.json"))


def test_corte_entre_tag_y_marker_se_reanuda_sin_repetir_restore(tmp_path):
    """Falsador del corte exacto: tag ya movido pero el marker post_restore no llegó a
    hacerse durable. `restored` debe aceptar ese único segundo estado y retomar."""
    plan, token = _plan_v2(tmp_path)
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    for fuente in (RB, LIB, HELPER):
        destino = scripts / fuente.name
        destino.write_bytes(fuente.read_bytes())
        destino.chmod(fuente.stat().st_mode)

    llamadas_up = tmp_path / "up.log"
    (repo / "llmi").write_text(f'''#!/bin/sh
printf 'up\n' >> "{llamadas_up}"
n=$(wc -l < "{llamadas_up}")
[ "$n" -gt 1 ]
''')
    (repo / "llmi").chmod(0o755)

    binario = tmp_path / "bin-stateful"
    binario.mkdir()
    registro = tmp_path / "docker-stateful.log"
    tag_movido = tmp_path / "tag-movido"
    destino_id = "sha256:" + "ab" * 32
    helper_id = "sha256:" + "cd" * 32
    (binario / "docker").write_text(f'''#!/bin/sh
printf '%s\n' "$*" >> "{registro}"
if [ "$1" = "inspect" ]; then
  printf '[{{"Id":"cid-1","Created":"c","State":{{"StartedAt":"s","Status":"exited"}},"RestartCount":0,"Image":"{helper_id}","Config":{{"Image":"img:local","Env":["LLMINBOX_DB=/data/llminbox.sqlite"]}},"Mounts":[{{"Type":"volume","Name":"vol-m4","Source":"/var/lib/docker/volumes/vol-m4/_data","Destination":"/data","RW":true}}]}}]\n'
  exit 0
fi
if [ "$1 $2" = "image inspect" ]; then
  if [ "$3" = "img:local" ]; then
    if [ -f "{tag_movido}" ]; then d="{destino_id}"; else d="{helper_id}"; fi
  elif [ "$3" = "{helper_id}" ]; then d="{helper_id}"
  else d="{destino_id}"
  fi
  printf '[{{"Id":"%s","RepoDigests":[]}}]\n' "$d"; exit 0
fi
if [ "$1 $2" = "volume inspect" ]; then
  printf '[{{"Name":"vol-m4","Driver":"local","Mountpoint":"/vm/vol-m4","Scope":"local","CreatedAt":"C","Labels":{{}},"Options":{{}}}}]\n'; exit 0
fi
if [ "$1" = "run" ]; then echo 'OK {{}}'; exit 0; fi
if [ "$1" = "tag" ]; then : > "{tag_movido}"; exit 0; fi
exit 0
''')
    (binario / "docker").chmod(0o755)
    (binario / "curl").write_text(f"#!/bin/sh\ncat <<'JSON'\n{SANO}\nJSON\n")
    (binario / "curl").chmod(0o755)

    env = {"PATH": f"{binario}:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
           "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
           "LLMINBOX_NAME": "inst-m4", "M4_EVIDENCIA": str(tmp_path / "evid"),
           "LLMI_LOCK_DIR": str(tmp_path)}
    orden = ["bash", str(scripts / "m4-rollback.sh"),
             "--reanudar", token, "--plan", str(plan)]
    r1 = subprocess.run(orden, capture_output=True, text=True, cwd=repo, env=env, timeout=90)
    # El primer intento movió el tag y luego falló en `up`. Retirar sólo este marker
    # reproduce el crash inmediatamente posterior al tag y anterior a su publicación.
    post = list((tmp_path / "evid").glob(f"state-{token}-post_restore.json"))
    assert len(post) == 1 and tag_movido.exists()
    post[0].unlink()
    r2 = subprocess.run(orden, capture_output=True, text=True, cwd=repo, env=env, timeout=90)
    llamadas = registro.read_text()
    assert r1.returncode == 1, f"el primer up debía fallar\n{r1.stdout}\n{r1.stderr}"
    assert llamadas.count("run --rm") == 1, "el retry post_restore repitió el helper"
    assert llamadas.count("tag ") == 2, "no repitió idempotentemente el tag tras el corte"
    assert llamadas_up.read_text().splitlines() == ["up", "up"], (
        f"el retry no alcanzó el segundo up\n{r2.stdout}\n{r2.stderr}\n{llamadas}")


def test_fase2_rechaza_parametros_de_cliente_en_ejecucion(tmp_path):
    plan, token = _plan_v2(tmp_path)
    binario, _ = _entorno_fase2(tmp_path)
    r = _corre_rollback(tmp_path,
                        ["--reanudar", token, "--plan", str(plan), "--a", "otra:img"],
                        binario)
    assert r.returncode == 2 and "NO acepta --a" in r.stderr


def test_fase1_emergencia_NO_emite_plan_consumible(tmp_path):
    binario, _ = _entorno_fase2(tmp_path, estado="running")
    snap = tmp_path / "s.sqlite"
    base(snap)
    evid = tmp_path / "evid"
    evid.mkdir(parents=True, exist_ok=True)
    (evid / "X-snapshot.json").write_text(json.dumps(
        {"snapshot": str(snap),
         "inventario": {"sha256": hashlib.sha256(snap.read_bytes()).hexdigest()}}))
    r = _corre_rollback(tmp_path, ["--a", "img@sha256:" + "ab" * 32,
                                   "--datos", str(snap),
                                   "--emergencia-sin-outbox", "--emergencia-sin-journal"],
                        binario)
    assert r.returncode == 1, "la fase 1 NUNCA puede acreditar por sí sola"
    assert not list(evid.glob("*-plan.json")), "una excepción roja no debe acuñar autorización de fase 2"
    emergencias = list(evid.glob("*-emergencia-sin-autorizacion.json"))
    assert emergencias and json.loads(emergencias[0].read_text())["operacion"] == "emergencia-no-plan"


def test_fase1_verde_liga_los_cinco_artefactos_en_plan_v4(tmp_path):
    binario, _ = _entorno_fase2(tmp_path, estado="running")
    snap = tmp_path / "s.sqlite"; base(snap)
    sha = hashlib.sha256(snap.read_bytes()).hexdigest()
    r = _corre_rollback(tmp_path, ["--a", "img@sha256:" + "ab" * 32, "--datos", str(snap)],
                        binario, {"M4_SNAPSHOT_SHA": sha})
    assert r.returncode == 1 and "FASE 1 completa" in r.stdout
    planes = list((tmp_path / "evid").glob("*-plan.json")); assert len(planes) == 1
    p = json.loads(planes[0].read_text())
    assert p["esquema"] == 4 and p["restaura_indice"] is True
    assert set(p["evidencias"]) == {
        "health", "outbox", "journal", "snapshot", "inspect", "helper"}
    for item in p["evidencias"].values():
        b = Path(item["ruta"]).read_bytes()
        assert item["sha256"] == hashlib.sha256(b).hexdigest() and item["bytes"] == len(b)
    verdictos = list((tmp_path / "evid").glob("*-veredicto.json"))
    v = json.loads(verdictos[0].read_text())
    assert v["ok"] is False and v["acreditacion"] == "no_acreditado"
    assert v["puertas"]["ejecucion"]["estado"] == "no_medido"


def test_dry_run_valida_pero_no_crea_state_ni_ejecuta_helper(tmp_path):
    plan, token = _plan_v2(tmp_path); binario, reg = _entorno_fase2(tmp_path, estado="exited")
    r = _corre_rollback(tmp_path, ["--reanudar", token, "--plan", str(plan), "--seco"], binario)
    assert r.returncode == 1
    assert not list((tmp_path / "evid").glob(f"state-{token}-*.json"))
    assert "run --rm" not in (reg.read_text() if reg.exists() else "")


def test_snapshot_invalido_no_crea_claim_pre_restore(tmp_path):
    plan, token = _plan_v2(tmp_path); p = json.loads(plan.read_text())
    Path(p["snapshot"]).write_bytes(b"alterado")
    binario, reg = _entorno_fase2(tmp_path, estado="exited")
    r = _corre_rollback(tmp_path, ["--reanudar", token, "--plan", str(plan)], binario)
    assert r.returncode == 1 and not list((tmp_path / "evid").glob(f"state-{token}-*.json"))
    assert "run --rm" not in (reg.read_text() if reg.exists() else "")


def test_plan_no_se_puede_reanudar_desde_otro_EVID(tmp_path):
    plan, token = _plan_v2(tmp_path); binario, reg = _entorno_fase2(tmp_path, estado="exited")
    alterno = tmp_path / "otro-evid"
    r = _corre_rollback(tmp_path, ["--reanudar", token, "--plan", str(plan)], binario,
                        {"M4_EVIDENCIA": str(alterno)})
    assert r.returncode == 1 and "ligado a EVID" in r.stderr
    assert "run --rm" not in (reg.read_text() if reg.exists() else "")


def test_helper_alterado_o_symlink_nunca_se_ejecuta(tmp_path):
    for symlink in (False, True):
        caso = tmp_path / ("link" if symlink else "hash"); caso.mkdir()
        plan, token = _plan_v2(caso); p = json.loads(plan.read_text())
        helper = Path(p["evidencias"]["helper"]["ruta"])
        if symlink:
            real = caso / "otro.py"; real.write_text("print('otro')"); helper.unlink(); helper.symlink_to(real)
        else:
            helper.write_bytes(helper.read_bytes() + b"\n# alterado\n")
        binario, reg = _entorno_fase2(caso, estado="exited")
        r = _corre_rollback(caso, ["--reanudar", token, "--plan", str(plan)], binario)
        assert r.returncode == 1 and "run --rm" not in (reg.read_text() if reg.exists() else "")


def test_jq_roto_en_inspect_falla_cerrado(tmp_path):
    doble_docker(tmp_path, "if [ \"$1\" = inspect ]; then echo NO-JSON; exit 0; fi\nexit 0\n")
    r = corre('evidencia_contenedor inst; echo rc=$?', tmp_path)
    assert "rc=1" in r.stdout and "jq no medible" in r.stdout


def test_ultimo_inspect_caza_cambio_a_running(tmp_path):
    d = tmp_path / "bin"; d.mkdir(exist_ok=True); cuenta = tmp_path / "n"
    (d / "docker").write_text(f'''#!/bin/sh
n=$(cat "{cuenta}" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "{cuenta}"
estado=exited; [ "$n" -gt 1 ] && estado=running
printf '[{{"Id":"cid-1","Created":"c","State":{{"StartedAt":"s","Status":"%s"}},"RestartCount":0,"Image":"{DIG}","Config":{{"Image":"img","Env":["LLMINBOX_DB=/data/llminbox.sqlite"]}},"Mounts":[{{"Type":"volume","Name":"vol","Source":"x","Destination":"/data","RW":true}}]}}]\n' "$estado"
'''); (d / "docker").chmod(0o755)
    r = corre(f'''evidencia_contenedor inst >/dev/null
ultimo_stopped_plan inst cid-1 {DIG} vol /data /data/llminbox.sqlite; echo rc=$?''', tmp_path)
    assert "rc=1" in r.stdout and "último inspect" in r.stderr


def test_identidad_fuerte_del_volumen_debe_coincidir(tmp_path):
    doble_docker(tmp_path, '''
if [ "$1 $2" = "volume inspect" ]; then
 printf '[{"Name":"vol","Driver":"local","Mountpoint":"/nuevo","Scope":"local","Labels":{},"Options":{}}]\n'; exit 0
fi
exit 1
''')
    esperado = json.dumps({"name":"vol","driver":"local","mountpoint":"/viejo",
                           "scope":"local","created_at":"","labels":{},"options":{}})
    r = corre(f'volumen_valida vol {json.dumps(esperado)}; echo rc=$?', tmp_path)
    assert "rc=1" in r.stdout


def test_state_machine_exige_prefijo_contiguo(tmp_path):
    evid = tmp_path / "evid"; evid.mkdir(mode=0o700); token = "b" * 64
    corre(f'estado_publica "{evid}" {token} post_restore inst', tmp_path)
    r = corre(f'estado_actual "{evid}" {token} inst; echo rc=$?', tmp_path)
    assert "rc=1" in r.stdout


def test_completed_es_idempotente_y_no_muta(tmp_path):
    plan, token = _plan_v2(tmp_path); evid = tmp_path / "evid"
    corre(f'''estado_publica "{evid}" {token} pre_restore inst-m4 >/dev/null
estado_publica "{evid}" {token} restored inst-m4 >/dev/null
estado_publica "{evid}" {token} post_restore inst-m4 >/dev/null
estado_publica "{evid}" {token} completed inst-m4 >/dev/null''', tmp_path)
    binario, reg = _entorno_fase2(tmp_path, estado="running")
    r = _corre_rollback(tmp_path, ["--reanudar", token, "--plan", str(plan)], binario)
    llamadas = reg.read_text() if reg.exists() else ""
    assert r.returncode == 0 and "ya completado" in r.stdout
    assert "run --rm" not in llamadas and "tag " not in llamadas


# ══ ⑳ EL VOLUMEN SE NOMBRA; `.Source` es la ruta de la VM ══════════════════════
def _ev(montajes, env=None):
    return json.dumps({"medible": True, "id": "cid-1", "montajes": montajes,
                       "db_env": (env or "")})


VOL_OK = [{"tipo": "volume", "nombre": "vol-m4", "origen": "/var/lib/docker/volumes/vol-m4/_data",
           "destino": "/data", "rw": True}]


@pytest.mark.parametrize("montajes,acepta,porque", [
    (VOL_OK, True, "⊕ control: un volumen con nombre, rw y destino correcto"),
    ([{"tipo": "bind", "nombre": "", "origen": "/host/x", "destino": "/data", "rw": True}],
     False, "un bind mount no es un volumen nombrado"),
    ([{"tipo": "volume", "nombre": "", "origen": "/v/anon/_data", "destino": "/data", "rw": True}],
     False, "volumen anónimo: `-v` no puede nombrarlo"),
    ([{"tipo": "volume", "nombre": "vol-m4", "origen": "/v", "destino": "/data", "rw": False}],
     False, "montaje de sólo lectura: no se puede restaurar ahí"),
    (VOL_OK + [{"tipo": "bind", "nombre": "", "origen": "/otro", "destino": "/data", "rw": True}],
     False, "dos montajes al mismo destino: elegir uno sería a ciegas"),
    ([], False, "sin montaje al mountpoint"),
])
def test_el_volumen_se_deriva_sin_ambiguedad(tmp_path, montajes, acepta, porque):
    r = corre(f'volumen_del_mount {json.dumps(_ev(montajes))} /data; echo "rc=$?"', tmp_path)
    assert f"rc={0 if acepta else 1}" in r.stdout, f"{porque}\n{r.stdout}\n{r.stderr}"
    if acepta:
        assert "vol-m4" in r.stdout and "/var/lib/docker" not in r.stdout, (
            "se congeló `.Source` (ruta de la VM) en vez del nombre")


# ══ ㉑ LA RUTA DE LA DB SALE DEL INSPECT Y CAE BAJO EL MOUNT ═══════════════════
@pytest.mark.parametrize("env,acepta,porque", [
    ("/data/llminbox.sqlite", True, "⊕ control: bajo el montaje"),
    ("", True, "sin LLMINBOX_DB se usa el defecto CONTRACTUAL, no el del shell"),
    ("/otro/sitio.sqlite", False, "fuera del montaje: el helper tocaría algo no congelado"),
    ("/data/../etc/passwd", False, "travesía"),
    ("relativa.sqlite", False, "no es absoluta"),
])
def test_la_db_se_deriva_del_inspect_y_se_valida(tmp_path, env, acepta, porque):
    r = corre(f'db_del_contenedor {json.dumps(_ev(VOL_OK, env))} /data; echo "rc=$?"',
              tmp_path)
    assert f"rc={0 if acepta else 1}" in r.stdout, f"{porque}\n{r.stdout}\n{r.stderr}"


def test_la_db_NO_sale_de_la_variable_del_shell(tmp_path):
    """Tomarla de `LLMINBOX_DB_INTERNO` describe el entorno de quien ejecuta, no el del
    contenedor congelado — y entre las dos fases ese shell puede ser otro."""
    r = corre(f'db_del_contenedor {json.dumps(_ev(VOL_OK, "/data/real.sqlite"))} /data',
              tmp_path, {"LLMINBOX_DB_INTERNO": "/data/DEL-SHELL.sqlite"})
    assert "/data/real.sqlite" in r.stdout and "DEL-SHELL" not in r.stdout


def test_la_evidencia_no_serializa_los_secretos_del_entorno(tmp_path):
    """`Config.Env` lleva `LLMINBOX_TOKEN`; la evidencia se comparte."""
    binario, _ = _entorno_fase2(tmp_path)
    r = corre('evidencia_contenedor inst-m4', tmp_path,
              {"PATH": f"{binario}:/opt/homebrew/bin:/usr/bin:/bin"})
    assert "SECRETO-NO-DEBE-SALIR" not in r.stdout, (
        f"la evidencia serializa el entorno entero:\n{r.stdout}")
    assert '"db_env":"/data/llminbox.sqlite"' in r.stdout.replace(" ", ""), r.stdout


def test_helper_no_pisa_backup_preplantado(tmp_path):
    d = tmp_path / "data"; d.mkdir()
    destino = d / "llminbox.sqlite"; fuente = tmp_path / "fuente.sqlite"
    base(destino); base(fuente)
    sello = "S-PREPLANT"; backup = Path(str(destino) + ".rollback-" + sello)
    backup.write_text("CENTINELA")
    sha = hashlib.sha256(fuente.read_bytes()).hexdigest()
    r = subprocess.run(["python3", str(HELPER), str(destino), str(fuente), sello, sha, "fresh"],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 1 and "preplantado" in r.stdout
    assert backup.read_text() == "CENTINELA"
    assert not (d / f".m4-stage-{sello}.sqlite").exists()


def test_helper_no_sigue_symlink_de_fuente(tmp_path):
    d = tmp_path / "data"; d.mkdir()
    destino = d / "llminbox.sqlite"; real = tmp_path / "real.sqlite"; enlace = tmp_path / "snap.sqlite"
    base(destino); base(real); enlace.symlink_to(real)
    sha = hashlib.sha256(real.read_bytes()).hexdigest()
    r = subprocess.run(["python3", str(HELPER), str(destino), str(enlace), "S-LINK", sha, "fresh"],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 1 and "symlink" in r.stdout


def test_helper_no_sigue_symlink_de_destino_ni_toca_su_objetivo(tmp_path):
    d = tmp_path / "data"; d.mkdir()
    real = d / "real.sqlite"; enlace = d / "llminbox.sqlite"
    fuente = tmp_path / "fuente.sqlite"
    base(real); base(fuente); enlace.symlink_to(real)
    antes = _sha(real)
    r = _corre_helper(tmp_path, enlace, fuente, _sha(fuente))
    assert r.returncode == 1 and "symlink" in r.stdout
    assert _sha(real) == antes and enlace.is_symlink()
    assert not list(d.glob(".m4-stage-*")) and not list(d.glob("*.rollback-*"))


def test_helper_resume_reconoce_restore_ya_publicado(tmp_path):
    d = tmp_path / "data"; d.mkdir()
    destino = d / "llminbox.sqlite"; fuente = tmp_path / "fuente.sqlite"
    base(destino); base(fuente)
    con = sqlite3.connect(fuente); con.execute(
        "INSERT INTO entries(ledger,eid) VALUES('l','777')"); con.commit(); con.close()
    sha = hashlib.sha256(fuente.read_bytes()).hexdigest(); token = "c" * 64
    uno = subprocess.run(["python3", str(HELPER), str(destino), str(fuente), token, sha, "fresh"],
                         capture_output=True, text=True, timeout=30)
    dos = subprocess.run(["python3", str(HELPER), str(destino), str(fuente), token, sha, "resume"],
                         capture_output=True, text=True, timeout=30)
    assert uno.returncode == dos.returncode == 0 and '"reanudado": true' in dos.stdout
    assert len(list(d.glob("*.rollback-*"))) == 1


def test_pilot_inspect_no_medible_no_es_primer_piloto(tmp_path):
    binario = tmp_path / "bin"; binario.mkdir()
    (binario / "docker").write_text('#!/bin/sh\necho "Cannot connect to daemon" >&2\nexit 1\n')
    (binario / "docker").chmod(0o755)
    env = {"PATH": f"{binario}:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
           "HOME": str(tmp_path), "TMPDIR": str(tmp_path), "LLMINBOX_NAME": "inst-m4",
           "M4_EVIDENCIA": str(tmp_path / "evid"), "LLMI_LOCK_DIR": str(tmp_path)}
    r = subprocess.run(["bash", str(RAIZ / "scripts/m4-pilot.sh")], cwd=RAIZ, env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 1 and "NO equivale a primer pilotaje" in r.stderr


# ══ ㉒ ③ · EL JOURNAL NO SE RESTAURA GENÉRICAMENTE ══════════════════════════════
#
# La guarda que había era la TOPOLOGÍA: el compose del piloto pone `/journal` y `/data`
# en volúmenes distintos, así que el helper nunca veía un journal. `grep -c
# 'journal\|coordination' scripts/m4-restore-helper.py` daba 0. Lo que el sustrato regala
# se lee como ahorro y es una dependencia: en la composición de la flota hay un solo
# `/data`. Ahora la negativa es POSITIVA y vive en el código.
def _corre_helper(tmp_path, destino, fuente, sha, modo="fresh", sello="S"):
    return subprocess.run(
        ["python3", str(HELPER), str(destino), str(fuente), sello, sha, modo],
        capture_output=True, text=True, cwd=tmp_path, timeout=60)


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _bundle_sha(destino: Path) -> dict:
    """El almacén no es un fichero: es `db` y todos sus sidecars conocidos."""
    out = {}
    for suf in ("", "-wal", "-shm", "-journal"):
        f = Path(str(destino) + suf)
        out[suf or "db"] = _sha(f) if f.exists() else None
    return out


def test_el_helper_rechaza_un_DESTINO_que_es_el_journal(tmp_path):
    d = tmp_path / "vol"; d.mkdir()
    destino = d / "coordination.sqlite"; base(destino, clase="journal")
    fuente = tmp_path / "fuente.sqlite"; base(fuente)          # un índice legítimo
    antes = _bundle_sha(destino)
    r = _corre_helper(tmp_path, destino, fuente, _sha(fuente))
    assert r.returncode == 1, r.stdout
    assert "JOURNAL" in r.stdout and "ADR-001" in r.stdout
    assert _bundle_sha(destino) == antes, "se negó DESPUÉS de tocar bytes"
    assert not list(d.glob(".m4-stage-*")), "dejó una etapa dentro del volumen del journal"
    assert not list(d.glob("*.rollback-*")), "dejó un backup dentro del volumen del journal"


def test_rechazar_journal_WAL_no_modifica_ni_un_byte_del_bundle(tmp_path):
    """Abrir con SQLite incluso en mode=ro cambia locks/readmarks de `-shm`. Un bundle
    caliente se rechaza por existencia antes de abrir, y los cuatro hashes quedan iguales."""
    d = tmp_path / "vol"; d.mkdir()
    destino = d / "llminbox.sqlite"; base(destino, clase="journal")
    fuente = tmp_path / "fuente.sqlite"; base(fuente)
    retenedor = sqlite3.connect(destino)
    retenedor.execute("PRAGMA journal_mode=WAL")
    retenedor.execute("PRAGMA wal_autocheckpoint=0")
    retenedor.execute("INSERT INTO events VALUES(777)")
    retenedor.commit()
    assert Path(str(destino) + "-wal").exists()
    assert Path(str(destino) + "-shm").exists()
    antes = _bundle_sha(destino)
    r = _corre_helper(tmp_path, destino, fuente, _sha(fuente))
    despues = _bundle_sha(destino)
    retenedor.close()
    assert r.returncode == 1 and "sidecars" in r.stdout
    assert despues == antes, "el mero rechazo participó en WAL y mutó el bundle"
    assert not list(d.glob(".m4-stage-*")) and not list(d.glob("*.rollback-*"))


def test_el_helper_rechaza_un_journal_RENOMBRADO_como_el_indice(tmp_path):
    """⊖ QUE MATA LA VACUIDAD DE ③: por nombre o por ruta esto pasaría — el fichero se
    llama exactamente igual que el índice y está en el sitio del índice."""
    d = tmp_path / "vol"; d.mkdir()
    destino = d / "llminbox.sqlite"; base(destino, clase="journal")
    fuente = tmp_path / "fuente.sqlite"; base(fuente)
    antes = _bundle_sha(destino)
    r = _corre_helper(tmp_path, destino, fuente, _sha(fuente))
    assert r.returncode == 1 and "JOURNAL" in r.stdout
    assert _bundle_sha(destino) == antes


def test_el_helper_rechaza_una_FUENTE_que_es_el_journal(tmp_path):
    """El otro extremo: destino legítimo, pero lo que se iba a PUBLICAR es un journal."""
    d = tmp_path / "vol"; d.mkdir()
    destino = d / "llminbox.sqlite"; base(destino)
    fuente = tmp_path / "fuente.sqlite"; base(fuente, clase="journal")
    antes = _bundle_sha(destino)
    r = _corre_helper(tmp_path, destino, fuente, _sha(fuente))
    assert r.returncode == 1 and "JOURNAL" in r.stdout
    assert _bundle_sha(destino) == antes, "publicó un journal sobre el índice"
    assert not list(d.glob(".m4-stage-*")), "la etapa rechazada no se retiró"


def test_CONTROL_POSITIVO_el_helper_SI_restaura_un_indice(tmp_path):
    """⊕ SIN EL CUAL LOS TRES DE ARRIBA NO MIDEN NADA: un helper que se negara siempre
    los pasaría los tres. Aquí tiene que restaurar de verdad."""
    d = tmp_path / "vol"; d.mkdir()
    destino = d / "llminbox.sqlite"; base(destino)
    fuente = tmp_path / "fuente.sqlite"; base(fuente)
    con = sqlite3.connect(fuente); con.execute(
        "INSERT INTO entries(ledger,eid) VALUES('l','4242')")
    con.commit(); con.close()
    antes = _bundle_sha(destino)
    r = _corre_helper(tmp_path, destino, fuente, _sha(fuente))
    assert r.returncode == 0, r.stdout
    assert r.stdout.startswith("OK ")
    assert _bundle_sha(destino) != antes, "dijo OK y no cambió nada"
    assert _sha(destino) == _sha(fuente), "publicó algo que no es la fuente anclada"
    con = sqlite3.connect(destino)
    con.execute("INSERT INTO entries(ledger,eid,arrival) VALUES('fleet','post-restore',1)")
    con.commit()
    assert con.execute("SELECT arrival FROM entries WHERE eid='post-restore'").fetchone()[0] == 1
    con.close()


def test_el_helper_rechaza_un_destino_INDETERMINADO(tmp_path):
    """Fail-closed: no basta con no ser journal."""
    d = tmp_path / "vol"; d.mkdir()
    destino = d / "llminbox.sqlite"; base(destino, clase="otra")
    fuente = tmp_path / "fuente.sqlite"; base(fuente)
    r = _corre_helper(tmp_path, destino, fuente, _sha(fuente))
    assert r.returncode == 1 and "no acredita ser el índice" in r.stdout


def _clasificador_del_helper():
    """El helper es un GUION, no un módulo: al importarlo se ejecuta entero. Pero el
    clasificador se define ANTES del desempaquetado de `sys.argv`, así que basta con
    ejecutarlo con un argv que reviente ahí y quedarse con el espacio de nombres."""
    import sys as _sys
    ns = {"__name__": "_clasificador_bajo_prueba", "__file__": str(HELPER)}
    guardado = _sys.argv
    _sys.argv = ["helper"]                      # `sys.argv[1:6]` -> ValueError seguro
    try:
        exec(compile(HELPER.read_text(encoding="utf-8"), str(HELPER), "exec"), ns)
    except (ValueError, SystemExit, OSError):
        pass
    finally:
        _sys.argv = guardado
    assert "clasifica_almacen" in ns, "el helper ya no expone `clasifica_almacen`"
    return ns["clasifica_almacen"]


@pytest.mark.parametrize("clase,tablas,meta,espera", [
    ("indice", None, None, "indice"),
    ("journal", None, None, "journal"),
    ("journal", ("events",), {}, "journal"),
    ("journal", (), {"durable_v": "6"}, "journal"),
    ("journal", INDICE_TABLAS, {"pepper_check": "x"}, "journal"),
    ("otra", ("entries",), {"schema_v": "h"}, "indeterminado"),
    ("otra", INDICE_TABLAS, {}, "indeterminado"),
    ("otra", (), {}, "indeterminado"),
])
def test_las_dos_implementaciones_clasifican_igual(tmp_path, clase, tablas, meta, espera):
    """El precio de tener el clasificador DOS veces —una en el helper, que viaja como un
    solo fichero hasheado dentro del contenedor, y otra en `valida_snapshot`, que corre en
    el host— es la DERIVA. Se paga aquí: si divergen, esto se pone rojo.

    Se comparan las TRES clases, no «pasa / no pasa»: dos implementaciones que rechazaran
    lo mismo por motivos distintos serían indistinguibles de un acuerdo real."""
    s = tmp_path / "b.sqlite"
    base(s, clase=clase, tablas=tablas, meta=meta)

    del_helper = _clasificador_del_helper()(str(s))[0]

    r = corre(f'valida_snapshot "{s}" /data', tmp_path)
    if r.returncode == 0:
        del_shell = "indice"
    elif "JOURNAL" in r.stderr:
        del_shell = "journal"
    elif "no acredita ser el índice" in r.stderr:
        del_shell = "indeterminado"
    else:
        del_shell = f"OTRO ({r.stderr.strip()[:80]})"

    assert del_helper == espera, f"el helper clasificó {del_helper!r}"
    assert del_shell == espera, f"`valida_snapshot` clasificó {del_shell!r}"
    assert del_helper == del_shell, "las dos implementaciones han DERIVADO"


# ══ ㉓ ② · EL EJE BINARIO NO EXIGE SNAPSHOT NI TOCA HASHES DE DATOS ═════════════
def test_fase1_sin_elegir_eje_de_datos_se_niega_nombrando_LAS_DOS_opciones(tmp_path):
    """No hay defecto implícito: el eje que PIERDE datos no se elige por omisión."""
    binario, _ = _entorno_fase2(tmp_path, estado="running")
    r = _corre_rollback(tmp_path, ["--a", "img@sha256:" + "ab" * 32], binario)
    assert r.returncode == 2
    assert "--solo-binario" in r.stderr and "--datos" in r.stderr


def test_solo_binario_y_datos_son_excluyentes(tmp_path):
    binario, _ = _entorno_fase2(tmp_path, estado="running")
    snap = tmp_path / "s.sqlite"; base(snap)
    r = _corre_rollback(tmp_path, ["--a", "img@sha256:" + "ab" * 32,
                                   "--solo-binario", "--datos", str(snap)], binario)
    assert r.returncode == 2 and "EXCLUYENTES" in r.stderr


def test_fase1_solo_binario_emite_plan_v4_SIN_snapshot_y_sin_hashear_datos(tmp_path):
    binario, _ = _entorno_fase2(tmp_path, estado="running")
    r = _corre_rollback(tmp_path, ["--a", "img@sha256:" + "ab" * 32, "--solo-binario"],
                        binario)
    assert r.returncode == 1 and "FASE 1 completa (SOLO BINARIO)" in r.stdout
    planes = list((tmp_path / "evid").glob("*-plan.json")); assert len(planes) == 1
    p = json.loads(planes[0].read_text())
    assert p["esquema"] == 4 and p["restaura_indice"] is False
    assert p["snapshot"] is None and p["snapshot_sha"] is None
    # NO SE LIGA ARTEFACTO DE DATOS: ni copia congelada, ni inventario, ni hash.
    assert "snapshot" not in p["evidencias"], "ligó un artefacto de datos sin eje de datos"
    assert set(p["evidencias"]) == {"health", "outbox", "journal", "inspect", "helper"}
    assert not list((tmp_path / "evid").glob("*-rb-snapshot.sqlite"))


def test_fase2_solo_binario_no_llama_al_helper_y_deja_los_datos_BYTE_IDENTICOS(tmp_path):
    """⊖ EL QUE DECIDE ②. Su ⊕ es `test_fase2_..._alcanza_el_helper`, que con
    `restaura_indice: true` SÍ corre el helper: sin él, «no tocó los datos» lo cumpliría
    también un guion que no hace nada."""
    d = tmp_path / "data"; d.mkdir(exist_ok=True)
    almacen = d / "llminbox.sqlite"; base(almacen)
    con = sqlite3.connect(almacen); con.execute("PRAGMA journal_mode=WAL")
    con.execute("INSERT INTO entries(ledger,eid) VALUES('l','7')"); con.commit(); con.close()
    antes = _bundle_sha(almacen)
    plan, token = _plan_v2(tmp_path, restaura_indice=False, snapshot=None,
                           snapshot_sha=None)
    # el artefacto de datos tampoco puede quedar ligado en un plan solo-binario
    p = json.loads(plan.read_text()); p["evidencias"].pop("snapshot", None)
    plan.write_text(json.dumps(p, sort_keys=True, indent=1))
    token = hashlib.sha256(plan.read_bytes()).hexdigest()
    binario, reg = _entorno_fase2(tmp_path, estado="exited")
    r = _corre_rollback(tmp_path, ["--reanudar", token, "--plan", str(plan)], binario)
    assert "docker run" not in reg.read_text(), "llamó al helper sin eje de datos"
    assert _bundle_sha(almacen) == antes, "tocó el almacén en una reversión solo-binario"
    v = json.loads(sorted((tmp_path / "evid").glob("*-veredicto.json"))[-1].read_text())
    for puerta in ("pre_restore", "restore"):
        assert v["puertas"][puerta]["estado"] == "no_aplica", v["puertas"][puerta]
        assert v["puertas"][puerta]["detalle"], "un `no_aplica` sin motivo es un hueco"
        assert puerta not in v["fallidas"], f"{puerta} declarada no_aplica y aun así fallida"
    assert r.returncode in (0, 1)


def test_un_plan_solo_binario_con_snapshot_cargado_se_rechaza(tmp_path):
    """O restaura o no: un plan que dice `false` y trae la ruta de datos deja preparada
    exactamente la restauración que dice que no va a hacer."""
    plan, _ = _plan_v2(tmp_path, restaura_indice=False)
    token = hashlib.sha256(plan.read_bytes()).hexdigest()
    binario, _ = _entorno_fase2(tmp_path, estado="exited")
    r = _corre_rollback(tmp_path, ["--reanudar", token, "--plan", str(plan)], binario)
    assert r.returncode == 1 and "PLAN NO VÁLIDO" in r.stderr


@pytest.mark.parametrize("campo,valor", [
    ("snapshot", ""), ("snapshot", 0), ("snapshot", False),
    ("snapshot_sha", ""), ("snapshot_sha", 0), ("snapshot_sha", False),
])
def test_plan_solo_binario_exige_null_EXACTO(tmp_path, campo, valor):
    plan, _ = _plan_v2(tmp_path, restaura_indice=False, snapshot=None,
                       snapshot_sha=None)
    p = json.loads(plan.read_text()); p["evidencias"].pop("snapshot", None)
    p[campo] = valor
    plan.write_text(json.dumps(p, sort_keys=True, indent=1))
    token = hashlib.sha256(plan.read_bytes()).hexdigest()
    binario, _ = _entorno_fase2(tmp_path, estado="exited")
    r = _corre_rollback(tmp_path, ["--reanudar", token, "--plan", str(plan)], binario)
    assert r.returncode == 1 and "PLAN NO VÁLIDO" in r.stderr


@pytest.mark.parametrize("campo", ["snapshot", "snapshot_sha"])
def test_plan_solo_binario_exige_los_dos_null_PRESENTES(tmp_path, campo):
    plan, _ = _plan_v2(tmp_path, restaura_indice=False, snapshot=None,
                       snapshot_sha=None)
    p = json.loads(plan.read_text()); p["evidencias"].pop("snapshot", None); p.pop(campo)
    plan.write_text(json.dumps(p, sort_keys=True, indent=1))
    token = hashlib.sha256(plan.read_bytes()).hexdigest()
    binario, _ = _entorno_fase2(tmp_path, estado="exited")
    r = _corre_rollback(tmp_path, ["--reanudar", token, "--plan", str(plan)], binario)
    assert r.returncode == 1 and "PLAN NO VÁLIDO" in r.stderr


def test_plan_solo_binario_rechaza_clave_snapshot_vacia_en_evidencias(tmp_path):
    plan, _ = _plan_v2(tmp_path, restaura_indice=False, snapshot=None,
                       snapshot_sha=None)
    p = json.loads(plan.read_text())
    p["evidencias"]["snapshot"] = {}
    plan.write_text(json.dumps(p, sort_keys=True, indent=1))
    token = hashlib.sha256(plan.read_bytes()).hexdigest()
    binario, _ = _entorno_fase2(tmp_path, estado="exited")
    r = _corre_rollback(tmp_path, ["--reanudar", token, "--plan", str(plan)], binario)
    assert r.returncode == 1 and "artefactos de fase 1 cambiaron" in r.stderr


def test_un_plan_sin_restaura_indice_no_es_valido(tmp_path):
    """Un campo ausente leído como `False` convertiría un plan viejo en autorización a
    saltarse el eje de datos sin que nadie lo declarara."""
    plan, _ = _plan_v2(tmp_path)
    p = json.loads(plan.read_text()); p.pop("restaura_indice")
    plan.write_text(json.dumps(p, sort_keys=True, indent=1))
    token = hashlib.sha256(plan.read_bytes()).hexdigest()
    binario, _ = _entorno_fase2(tmp_path, estado="exited")
    r = _corre_rollback(tmp_path, ["--reanudar", token, "--plan", str(plan)], binario)
    assert r.returncode == 1 and "restaura_indice" in r.stderr


def test_la_fase_2_tampoco_acepta_solo_binario_por_linea(tmp_path):
    plan, token = _plan_v2(tmp_path)
    binario, _ = _entorno_fase2(tmp_path, estado="exited")
    r = _corre_rollback(tmp_path, ["--reanudar", token, "--plan", str(plan),
                                   "--solo-binario"], binario)
    assert r.returncode == 2 and "--solo-binario" in r.stderr


def test_un_no_aplica_SIN_motivo_sigue_hundiendo_el_veredicto(tmp_path):
    """Motivo no es autorización: puerta y modo también tienen que estar ligados."""
    r = corre('veredicto_anota p no_aplica ""; veredicto_escribe "%s" op inst p; echo "rc=$?"'
              % (tmp_path / "v1.json"), tmp_path)
    assert "rc=1" in r.stdout, r.stderr
    r = corre('veredicto_anota p no_aplica "modo sin ese eje"; '
              'veredicto_escribe "%s" op inst p; echo "rc=$?"' % (tmp_path / "v2.json"),
              tmp_path)
    assert "rc=1" in r.stdout, r.stderr


def test_no_aplica_solo_acredita_las_dos_puertas_del_plan_solo_binario(tmp_path):
    v = tmp_path / "v.json"
    r = corre(f'''
VEREDICTO_MODO_ACREDITADO=solo_binario
veredicto_anota pre_restore no_aplica "plan v4 solo-binario"
veredicto_anota restore no_aplica "plan v4 solo-binario"
veredicto_escribe "{v}" reversion inst pre_restore restore; echo "rc=$?"
''', tmp_path)
    assert "rc=0" in r.stdout
    assert json.loads(v.read_text())["modo"] == "solo_binario"


def test_no_aplica_no_puede_apagar_salud_ni_en_solo_binario(tmp_path):
    v = tmp_path / "v.json"
    r = corre(f'''
VEREDICTO_MODO_ACREDITADO=solo_binario
veredicto_anota salud no_aplica "cualquier texto"
veredicto_escribe "{v}" reversion inst salud; echo "rc=$?"
''', tmp_path)
    assert "rc=1" in r.stdout
    assert json.loads(v.read_text())["fallidas"] == ["salud"]


def test_una_puerta_AUSENTE_sigue_sin_poder_ser_no_aplica(tmp_path):
    """La ausencia no se puede leer como «este modo no la tiene»: sigue dando
    `no_medido`, que es fallo. `no_aplica` hay que declararlo a mano."""
    r = corre('veredicto_escribe "%s" op inst puerta_que_nadie_anoto; echo "rc=$?"'
              % (tmp_path / "v.json"), tmp_path)
    assert "rc=1" in r.stdout
    v = json.loads((tmp_path / "v.json").read_text())
    assert v["puertas"]["puerta_que_nadie_anoto"]["estado"] == "no_medido"
