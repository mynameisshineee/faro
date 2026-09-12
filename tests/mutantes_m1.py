#!/usr/bin/env python3
"""Runner de mutantes M1 — CONGELADO POR MANIFIESTO, sobre `coordination.py`.

Por qué existe aparte de `mutantes_canon.py`: el canon genérico NO toca
`coordination.py`. Correrlo y presentar su verde como evidencia de M1 sería
acreditar el journal con un instrumento que no lo mira — la clase «un ⊕ valida
el instrumento, jamás su sujeto», aplicada al arnés.

CONGELADO significa tres cosas, y las tres son el punto:

  · se toma UNA instantánea manifestada y de sólo lectura. La suite limpia y
    cada mutante corren en clones desechables de esa foto; el árbol vivo no se
    escribe ni se usa como cwd, así que ni SIGKILL puede dejarlo mutado.

  · el ANCLA es texto exacto. Si no casa, esto ABORTA — no salta. Un mutante
    saltado es un mutante que nadie mató, y en un informe se lee igual que uno
    muerto.
  · el veredicto exige que caigan los tests NOMBRADOS. Que la suite se ponga
    roja «por algo» no acredita nada: un mutante que rompe la recolección
    también pone todo rojo, y eso no es discriminar.

Uso:  python3 tests/mutantes_m1.py            (necesita pytest en el intérprete)
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import pathlib
import signal
import shutil
import subprocess
import sys
import tempfile

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OBJETIVO = os.path.join(RAIZ, "coordination.py")
_TEMPORALES: set[str] = set()


def _fsync_directorio(path: str) -> None:
    fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _limpiar_temporales() -> None:
    for path in tuple(_TEMPORALES):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        finally:
            _TEMPORALES.discard(path)


def _escribir_atomico(objetivo: str, contenido: str) -> None:
    """Instala bytes completos y durables; nunca expone un fichero truncado."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(objetivo) or ".",
                               prefix=".mutacion-")
    _TEMPORALES.add(tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(contenido)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, objetivo)
        _TEMPORALES.discard(tmp)
        _fsync_directorio(objetivo)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        finally:
            _TEMPORALES.discard(tmp)


def _sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifiesto(raiz: pathlib.Path) -> dict[str, str]:
    """Contenido que identifica al sujeto, incluidos tests aún no trackeados."""
    rutas = [raiz / "coordination.py", raiz / "requirements-test.txt",
             raiz / "tests" / "mutantes_m1.py"]
    journal = raiz / "tests" / "journal"
    if journal.is_dir():
        rutas.extend(sorted(journal.glob("*.py")))
    return dict(sorted((str(p.relative_to(raiz)), _sha(p))
                       for p in rutas if p.is_file()))


def _digest(manifiesto: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(manifiesto, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _permisos(raiz: str, *, escribible: bool) -> None:
    dmode, fmode = (0o755, 0o644) if escribible else (0o555, 0o444)
    for base, dirs, files in os.walk(raiz, topdown=not escribible):
        for name in files:
            with contextlib.suppress(OSError):
                os.chmod(os.path.join(base, name), fmode)
        for name in dirs:
            with contextlib.suppress(OSError):
                os.chmod(os.path.join(base, name), dmode)
    with contextlib.suppress(OSError):
        os.chmod(raiz, dmode)


def _clon(origen: str, destino: str) -> str:
    shutil.copytree(origen, destino)
    _permisos(destino, escribible=True)
    return destino


def _snapshot_congelado() -> tuple[str, str, str, str]:
    """Una foto prístina; después de copiar no se vuelve a ejecutar dentro."""
    raiz = pathlib.Path(RAIZ)
    manifiesto = _manifiesto(raiz)
    digest = _digest(manifiesto)
    live_sha = _sha(raiz / "coordination.py")
    base = tempfile.mkdtemp(prefix="m1-mutantes-")
    snapshot = os.path.join(base, "_sujeto_congelado")
    try:
        shutil.copytree(raiz, snapshot, ignore=shutil.ignore_patterns(
            ".git", "__pycache__", ".pytest_cache", ".venv*", "*.sqlite*",
            ".restaura-*", ".mutacion-*", "node_modules"))
        snap_manifest = _manifiesto(pathlib.Path(snapshot))
        if snap_manifest != manifiesto:
            raise MutanteInservible(
                "SNAPSHOT_INFIEL: el manifiesto cambió mientras se copiaba")
        _permisos(snapshot, escribible=False)
        return base, snapshot, digest, live_sha
    except BaseException:
        _permisos(base, escribible=True)
        shutil.rmtree(base, ignore_errors=True)
        raise

# ── MANIFIESTO CONGELADO ────────────────────────────────────────────────────
# id · qué defecto histórico reintroduce · ancla exacta · sustitución · tests
#      que TIENEN que ponerse rojos.
MANIFIESTO = [
    {
        "id": "M01",
        "clase": "triple SHA — la copia deja de compararse con el original",
        "porque": "`antes == copia == despues` es lo que distingue una copia "
                  "hecha a mitad de una escritura que revierte. Quitando el "
                  "brazo de la COPIA quedan dos lecturas del mismo sitio y la "
                  "foto puede no ser de lo que se clasificó.",
        "ancla": "                 and all(k not in copiables_efectivos\n"
                 "                         or (k in copia and copia[k][1] == antes[k][1])\n"
                 "                         for k in antes))",
        "rota":  "                 and True)",
        "mata": ["tests/journal/test_correctivos_m1_8.py::"
                 "test_una_COPIA_que_no_casa_con_el_original_hace_la_foto_inestable"],
    },
    {
        "id": "M03",
        "clase": "shm ajeno — se abre el original sin revalidar identidad",
        "porque": "comprobar DESPUÉS de abrir ya creó el `-shm` sobre el "
                  "fichero de otro y dejó un handle vivo apuntándolo. El "
                  "veredicto correcto llega tarde: el rastro ya está escrito.",
        "ancla": "            self._verificar_identidad()\n            con = None",
        "rota":  "            con = None",
        "mata": ["tests/journal/test_preflight_m1_6.py::"
                 "test_09_un_reemplazo_del_MISMO_tamano_y_otro_inode_se_caza",
                 "tests/journal/test_preflight_m1_6.py::"
                 "test_11_dos_hilos_y_un_reemplazo_en_medio"],
    },
    {
        "id": "M09",
        "clase": "tope de retry — la inestabilidad se gana a reintentos",
        "porque": "`PREFLIGHT_INTENTOS` acota la carrera A PROPÓSITO: un "
                  "escritor activo es «un estado del mundo que se declara, no "
                  "una carrera que se gane a reintentos». Sin tope, el "
                  "preflight se cuelga esperando una quietud que no llega.",
        # El tope que MUERDE es el de TIEMPO: subir sólo `PREFLIGHT_INTENTOS`
        # no cambia nada porque el `break` por segundos corta antes, y el
        # mutante sobreviviría por una razón que no es la que se quiere probar.
        # Se sube a 60 s y no al infinito a propósito: un mutante que cuelga la
        # máquina no es un mutante, es una avería del runner.
        "ancla": "PREFLIGHT_INTENTOS = 3",
        "rota":  "PREFLIGHT_INTENTOS = 10_000_000",
        "mata": ["tests/journal/test_correctivos_m1_8.py::"
                 "test_la_foto_no_reintenta_MAS_de_PREFLIGHT_INTENTOS_veces"],
    },
    {
        "id": "M10",
        "clase": "pepper-before-RW — se abre el original antes de acreditarlo",
        "porque": "la sonda RW reescribe el formato y crea sidecars. Si el "
                  "pepper se valida después, el rechazo llega con el fichero "
                  "de otra casa ya tocado.",
        "ancla": "            with self._inspeccion() as con:\n"
                 "                self._pepper_de(con)\n"
                 "                fila = con.execute(\"SELECT v FROM meta WHERE k='durable_v'\").fetchone()\n"
                 "                existing = int(fila[\"v\"]) if fila else None\n"
                 "            # ③ Y AHORA sí se sondea",
        "rota":  "            with self._inspeccion() as con:\n"
                 "                fila = con.execute(\"SELECT v FROM meta WHERE k='durable_v'\").fetchone()\n"
                 "                existing = int(fila[\"v\"]) if fila else None\n"
                 "            # ③ Y AHORA sí se sondea",
        "mata": ["tests/journal/test_correctivos_m1_8.py::"
                 "test_un_pepper_INCORRECTO_no_toca_ni_un_byte_ni_crea_sidecars"],
    },
    {
        "id": "M11",
        "clase": "cache/refoto — la época deja de invalidar lo cacheado",
        "porque": "la conexión es thread-local: sin época, un worker sigue "
                  "leyendo por un handle cuyo respaldo ya se soltó.",
        "ancla": "            if getattr(self._local, \"epoca\", None) == self._epoca:",
        "rota":  "            if True:",
        "mata": ["tests/journal/test_correctivos_m1_8.py::"
                 "test_una_REFOTO_de_otro_hilo_invalida_la_conexion_CACHEADA_del_worker"],
    },
    {
        "id": "M13",
        "clase": "cleanup — el temporal con la réplica del journal no se suelta",
        "porque": "cada copia es una réplica ENTERA del canon. Sin liberar, "
                  "abrir el journal N veces deja N copias vivas.",
        "ancla": "                if viejo:\n                    shutil.rmtree(viejo, ignore_errors=True)",
        "rota":  "                if False:\n                    shutil.rmtree(viejo, ignore_errors=True)",
        "mata": ["tests/journal/test_correctivos_m1_8.py::"
                 "test_dispose_no_deja_copias_del_journal_en_el_temporal"],
    },
    {
        "id": "M14",
        "clase": "V-1 — la valla acepta un token que no es el vigente",
        "porque": "el dueño relevado sigue mutando: `!=` es lo único que "
                  "separa «tengo un número» de «soy el dueño AHORA».",
        "ancla": "        if int(fencing_token) != int(row[\"fencing_token\"]):",
        "rota":  "        if False:",
        "mata": ["tests/journal/test_correctivos_m1_8.py::"
                 "test_V1_el_token_VIEJO_del_MISMO_dueno_no_pasa_tras_RENOVAR_la_valla"],
    },
    {
        "id": "M15",
        "clase": "WAL/sidecars — el super-journal dinámico vuelve a ser invisible",
        "porque": "`-mj` literal no existe: SQLite lo llama `-mjHHHHHHHH`. Sin "
                  "buscar el prefijo, el fichero que dice que hay una "
                  "transacción multi-base a medias no entra en el inventario.",
        "ancla": "    return SUFIJOS + tuple(sorted(dinamicos))",
        "rota":  "    return SUFIJOS",
        "mata": ["tests/journal/test_correctivos_m1_8.py::"
                 "test_un_SUPER_JOURNAL_de_nombre_dinamico_hace_la_foto_inestable"],
    },
    {
        "id": "M16",
        "clase": "lifecycle/refoto — la foto fallida no limpia su temporal",
        "porque": "el finalizador se armaba ANTES de la foto buena y dejaba "
                  "huérfano el directorio anterior. Medido: puso flaky el "
                  "falsador de 20 procesos, que en HEAD pasaba 3/3.",
        "ancla": "            self._tmpdir = None\n            shutil.rmtree(nuevo_dir, ignore_errors=True)\n            raise",
        "rota":  "            raise",
        "mata": ["tests/journal/test_correctivos_m1_8.py::"
                 "test_una_foto_FALLIDA_no_deja_su_temporal_huerfano"],
    },
]


class MutanteInservible(Exception):
    """El mutante no se pudo APLICAR o su corrida no es interpretable.

    No es «muerto» ni «vivo»: es que no hubo medida. Contarlo como muerto es lo
    que convierte un arnés en una máquina de certificar cualquier cosa.
    """


_HIJO = None


def _matar_hijo() -> None:
    global _HIJO
    if _HIJO is None or _HIJO.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError, OSError):
        os.killpg(os.getpgid(_HIJO.pid), signal.SIGKILL)
    with contextlib.suppress(Exception):
        _HIJO.communicate(timeout=10)


def _pytest(sel, py, *, cwd, timeout=1800):
    global _HIJO
    try:
        _HIJO = subprocess.Popen(
            [py, "-m", "pytest", *sel, "-q", "-p", "no:cacheprovider", "-rf"],
            cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True)
        stdout, stderr = _HIJO.communicate(timeout=timeout)
        return subprocess.CompletedProcess(_HIJO.args, _HIJO.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        _matar_hijo()
        raise
    except BaseException:
        _matar_hijo()
        raise
    finally:
        _HIJO = None


def _fallidos(salida: str) -> set:
    """`nodeid`s con línea FAILED. Sólo eso cuenta como «este test cayó»."""
    out = set()
    for linea in salida.splitlines():
        if linea.startswith("FAILED "):
            out.add(linea[len("FAILED "):].split(" ")[0].strip())
    return out


def _hubo_error_de_recoleccion(salida: str) -> bool:
    lineas = salida.splitlines()
    return (any(l.startswith("ERROR ") or l.startswith("INTERNALERROR")
                for l in lineas)
            or "errors during collection" in salida.lower())


def _veredicto(r, esperados) -> str:
    """MUERTO sólo si el mutante hizo caer EXACTAMENTE lo declarado.

    Antes bastaba `returncode != 0`, y eso metía en el saco de «muertos» a los
    `SyntaxError`, los errores de recolección, los `rc` 2-5 de pytest y los
    timeouts. Un mutante que impide que la suite arranque pone todo rojo y se
    leía como discriminación: es el defecto contra el que este mismo fichero
    avisa en su cabecera, cometido por el fichero.
    """
    if r.returncode in (2, 3, 4, 5):
        raise MutanteInservible(
            f"pytest salió con rc={r.returncode} (uso/interrupción/interno), "
            f"que NO es «un test falló»")
    if _hubo_error_de_recoleccion(r.stdout + r.stderr):
        raise MutanteInservible(
            "hubo ERROR de recolección: la suite no llegó a ejercer nada")
    if r.returncode == 0:
        return "VIVO"
    if r.returncode != 1:
        raise MutanteInservible(f"rc={r.returncode} inesperado")
    caidos = _fallidos(r.stdout + r.stderr)
    faltan = set(esperados) - caidos
    ajenos = caidos - set(esperados)
    if faltan or ajenos:
        raise MutanteInservible(
            f"rojo, pero NO exactamente por los tests declarados. Faltan: "
            f"{sorted(faltan)}; ajenos: {sorted(ajenos)}; "
            f"cayeron: {sorted(caidos)}")
    return "MUERTO"


def _instalar_senales(base: str) -> None:
    """Interrumpe hijos y elimina clones; el árbol vivo nunca necesita restaurarse."""
    def al_recibir_senal(signum, _frame):
        _matar_hijo()
        _limpiar_temporales()
        _permisos(base, escribible=True)
        shutil.rmtree(base, ignore_errors=True)
        os._exit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, al_recibir_senal)
        except (ValueError, OSError):
            pass


def main() -> int:
    py = sys.executable
    base, snapshot, sujeto_digest, live_sha = _snapshot_congelado()
    _instalar_senales(base)
    original = pathlib.Path(snapshot, "coordination.py").read_text(encoding="utf-8")
    manifiesto_snapshot = _manifiesto(pathlib.Path(snapshot))
    print(f"⊕ sujeto congelado sha256={sujeto_digest}")
    try:
        # PRE-VUELO sobre la foto única, antes de ejecutar nada.
        for m in MANIFIESTO:
            if original.count(m["ancla"]) != 1:
                print(f"🔴 ABORTO ({m['id']}): el ancla no casa exactamente 1 vez "
                      f"({original.count(m['ancla'])}). Un mutante que no se "
                      f"puede aplicar NO es un mutante que se saltó: es un "
                      f"manifiesto caducado, y hay que mirarlo.")
                return 3

        # ⊕ La base limpia también corre en un clon desechable, nunca en la
        # instantánea prístina ni en el worktree vivo.
        nombrados = sorted({t for m in MANIFIESTO for t in m["mata"]})
        limpio = _clon(snapshot, os.path.join(base, "_suite_limpia"))
        try:
            resultado_base = _pytest(nombrados, py, cwd=limpio)
        finally:
            _permisos(limpio, escribible=True)
            shutil.rmtree(limpio, ignore_errors=True)
        if resultado_base.returncode != 0:
            print("🔴 ABORTO: la base ya está roja; ningún rojo probaría nada")
            print(resultado_base.stdout[-2500:])
            return 2
        print(f"⊕ base verde sobre los {len(nombrados)} tests nombrados\n")

        vivos, muertos = [], []
        for m in MANIFIESTO:
            clon = _clon(snapshot, os.path.join(base, m["id"]))
            try:
                objetivo = os.path.join(clon, "coordination.py")
                mutado = original.replace(m["ancla"], m["rota"], 1)
                try:
                    compile(mutado, objetivo, "exec")
                except SyntaxError as e:
                    print(f"🔴 ABORTO ({m['id']}): no compila ({e})")
                    return 4
                _escribir_atomico(objetivo, mutado)
                man_mut = _manifiesto(pathlib.Path(clon))
                cambios = sorted(k for k in manifiesto_snapshot
                                  if man_mut.get(k) != manifiesto_snapshot[k])
                if cambios != ["coordination.py"]:
                    print(f"🔴 ABORTO ({m['id']}): mutación imprecisa {cambios}")
                    return 7
                try:
                    r = _pytest(m["mata"], py, cwd=clon, timeout=900)
                except subprocess.TimeoutExpired:
                    print(f"🔴 ABORTO ({m['id']}): timeout; no hubo medida")
                    return 5
                try:
                    estado = _veredicto(r, m["mata"])
                except MutanteInservible as e:
                    print(f"🔴 ABORTO ({m['id']}): {e}")
                    return 6
            finally:
                _permisos(clon, escribible=True)
                shutil.rmtree(clon, ignore_errors=True)
            if estado == "VIVO":
                vivos.append(m)
                print(f"🔴 {m['id']} SOBREVIVE — {m['clase']}")
                print(f"   el roto es el FALSADOR, no el código: {m['mata']}")
            else:
                muertos.append(m)
                print(f"✅ {m['id']} muerto — {m['clase']}")
        if _manifiesto(pathlib.Path(snapshot)) != manifiesto_snapshot:
            raise MutanteInservible("SNAPSHOT_ALTERADO: la foto prístina se movió")
        if _sha(pathlib.Path(OBJETIVO)) != live_sha:
            raise MutanteInservible(
                "ARBOL_VIVO_ALTERADO durante la corrida; el runner nunca lo restaura")
        print(f"\n{len(muertos)}/{len(MANIFIESTO)} muertos · {len(vivos)} vivos")
        return 1 if vivos else 0
    finally:
        _matar_hijo()
        _limpiar_temporales()
        _permisos(base, escribible=True)
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
