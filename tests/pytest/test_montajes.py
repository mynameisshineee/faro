"""Ningún bind-mount de FICHERO ÚNICO en la composición de Docker.

Nace de un fallo de producción del 2026-08-18 y de su repetición evitada:

`roles-por-alias.json` estaba montado como fichero suelto. El host lo reemplazó
por rename —que es como se DEBE escribir un fichero de forma atómica: tmp + mv—,
el bind-mount se quedó clavado al INODO borrado, y `/organigrama` sirvió DOS DÍAS
un organigrama viejo sin avisar. Nadie lo notó porque el fallo no da error: da
datos, y son plausibles.

Un mount de DIRECTORIO no tiene el problema: la resolución del nombre se rehace en
cada `open()`, así que un rename encima se ve inmediatamente.

Esto es la invariante, no el recordatorio. Un comentario pide que alguien se
acuerde; un test falla solo cuando alguien no se acuerda.

⛔ LO QUE ESTE GUARDA NO CUBRE, dicho aquí para que su nombre no prometa de más:
   un fichero SIN EXTENSIÓN montado a un destino SIN EXTENSIÓN es indistinguible
   de un directorio mirando sólo la sintaxis, y mirar el disco no vale — en CI las
   rutas del host no existen y un guarda que sólo funciona en una máquina no
   guarda nada. Cubre las dos formas de Compose (corta y larga) y cualquier
   extremo con extensión conocida. El hueco queda escrito, no tapado.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
# SÓLO lo versionado. `docker-compose.override.yml` es GENERADO y está en
# `.gitignore` junto a `roster.json` y `.llmi-mounts.json`: en un checkout limpio
# no existe, así que exigirle propiedades desde aquí hacía que el test demostrase
# localmente algo que en CI no podía demostrar — verde en mi máquina, rojo en el
# runner, y la propiedad sin verificar en ninguno de los dos.
#
# Tres contratos distintos, tres niveles (el CI los encontró mezclados):
#
#   ① el PARSER y los canarios  → fixtures sintéticos, herméticos
#   ② la composición PUBLICADA  → docker-compose.yml y nada más
#   ③ el override GENERADO      → se ejecuta el generador en un tmp y se mira
#                                  su salida; así una regresión de `llmi init`
#                                  también se pone roja
COMPOSES = ("docker-compose.yml",)

# Extensiones que delatan un FICHERO. Se miran los DOS extremos: un `source` sin
# extensión con `target: /censo.json` sigue siendo un mount de fichero.
EXT_DE_FICHERO = (".json", ".tsv", ".md", ".yml", ".yaml", ".txt", ".env",
                  ".sqlite", ".db", ".sh", ".py", ".token", ".log", ".csv")

# ── RIESGO ACEPTADO TEMPORALMENTE ────────────────────────────────────────────
# No es «exento = seguro». Es un riesgo vivo con su ficha:
#
#   single-file mount ................ sí
#   protección mecánica contra rename . NO
#   invariante operativa ............. el ledger es append-only y se escribe con
#                                      `>>` en sitio, nunca con tmp+mv
#   consecuencia si se viola ......... inodo rancio en silencio, sin un error
#
# No se arregla montando su directorio: cada ledger vive en la raíz de un repo
# distinto y expondría nueve proyectos dentro del contenedor. La excepción se
# apoya en una DISCIPLINA, no en un mecanismo — y por eso v0.3.1 debe ligarla
# formalmente al guarda que sostiene esa disciplina (`guard-ledger-append-only`):
# si ese guarda se desactiva o deja de cubrir tmp+mv, ESTA excepción debería
# ponerse roja. Hoy sólo está documentada; esa dependencia no es demostrable
# desde aquí, y decir lo contrario sería el mismo defecto que el guarda persigue.
#
# Casa ORIGEN y DESTINO, no un prefijo suelto: `/ledgers/` a secas perdonaría
# cualquier fichero futuro que alguien meta ahí.
RIESGO_ACEPTADO = (
    (re.compile(r"/LEDGER[\w.-]*\.md$"),
     re.compile(r"^/ledgers/[\w.-]+\.md$"),
     "ledgers: append-only en sitio (disciplina, no mecanismo) — ver v0.3.1"),
)


def _aceptado(origen: str, destino: str) -> str | None:
    return next((m for o, d, m in RIESGO_ACEPTADO
                 if o.search(origen) and d.match(destino)), None)


def _montajes(texto: str):
    """(origen, destino) de cada volumen, en las DOS formas de Compose.

    La forma larga es Compose perfectamente válido:

        - type: bind
          source: ./roster.json
          target: /censo.json

    Un guarda que sólo mira la forma corta se puede rodear sin querer, y entonces
    su nombre promete una clase que no cubre. Ignora volúmenes nombrados.
    """
    lineas = texto.splitlines()
    i = 0
    while i < len(lineas):
        ln = lineas[i]
        m = re.match(r"(\s*)-\s*(.*)$", ln)
        if not m:
            i += 1
            continue
        sangria, resto = len(m.group(1)), m.group(2)
        # ── forma larga: el item abre un MAPA. Se acepta cualquier clave y
        #    cualquier ORDEN — exigir que empiece por `type:` hacía que un
        #    montaje perfectamente válido con las claves en otro orden fuera
        #    invisible, y un guarda que se rodea sin querer da permiso.
        if re.match(r"[A-Za-z_][\w-]*\s*:", resto):
            bloque = [resto]
            j = i + 1
            while j < len(lineas):
                sig = lineas[j]
                if not sig.strip():
                    j += 1
                    continue
                sang_sig = len(sig) - len(sig.lstrip())
                if sang_sig <= sangria and re.match(r"\s*-\s", sig):
                    break
                if sang_sig <= sangria:
                    break
                bloque.append(sig.strip())
                j += 1
            texto_bloque = "\n".join(bloque)
            src = re.search(r"source\s*:\s*[\"\']?([^\"\'\n]+)", texto_bloque)
            dst = re.search(r"target\s*:\s*[\"\']?([^\"\'\n]+)", texto_bloque)
            if src and dst:
                yield src.group(1).strip(), dst.group(1).strip()
            i = j
            continue
        # ── forma corta ──
        c = re.match(r'"?([^":]+):([^":]+?)(?::(ro|rw))?"?\s*$', resto)
        if c:
            origen, destino = c.group(1).strip(), c.group(2).strip()
            if "/" in origen or "." in origen:      # no es un volumen nombrado
                yield origen, destino
        i += 1


def _es_fichero(origen: str, destino: str) -> bool:
    return (origen.lower().endswith(EXT_DE_FICHERO)
            or destino.lower().endswith(EXT_DE_FICHERO))


def test_ningun_bind_mount_de_fichero_unico():
    """② La composición PUBLICADA — sólo lo que está en Git.

    FALSADOR: devolver `- ./roster.json:/censo.json:ro` —en cualquiera de las dos
    formas— pone esto rojo con el nombre del fichero."""
    culpables = []
    for nombre in COMPOSES:
        ruta = RAIZ / nombre
        if not ruta.exists():
            continue                      # el override es local, puede no estar
        for origen, destino in _montajes(ruta.read_text()):
            if _es_fichero(origen, destino) and not _aceptado(origen, destino):
                culpables.append(f"{nombre}: {origen} → {destino}")
    assert not culpables, (
        "bind-mount de FICHERO ÚNICO — se ata al inodo y el host puede jubilarlo "
        "con un rename, dejando al contenedor leyendo bytes muertos para siempre "
        "(pasó el 2026-08-18 con roles-por-alias.json: 2 días sirviendo un "
        "organigrama viejo, sin error). Monta el DIRECTORIO y apunta la ruta "
        "dentro:\n  " + "\n  ".join(culpables))


FIXTURE = """services:
  x:
    volumes:
      - llminbox-data:/data
      - "./algo:/repo:ro"
      - "/host/roles.json:/shared/roles.json:ro"
      - type: bind
        source: ./roster.json
        target: /censo.json
        read_only: true
"""


def test_el_parser_ve_las_dos_formas_y_no_confunde_volumenes_nombrados():
    """CONTROL del parser, sobre un fixture SINTÉTICO — no sobre ficheros que en
    CI pueden no existir. Si el parser no reconociera ningún volumen, el guarda
    pasaría siempre: verde por no mirar, que es el modo de fallo más caro de este
    repo y tiene su propia cicatriz.

    Antes este control leía la composición real, incluido el override GENERADO.
    En CI ese fichero no existe (`.gitignore`), así que el test demostraba
    localmente una propiedad que en el runner no podía demostrar. Lo cazó el CI,
    no yo."""
    vistos = list(_montajes(FIXTURE))
    assert ("llminbox-data", "/data") not in vistos, "cuenta un volumen NOMBRADO"
    assert ("./algo", "/repo") in vistos, f"pierde la forma corta: {vistos}"
    assert ("/host/roles.json", "/shared/roles.json") in vistos, vistos
    assert ("./roster.json", "/censo.json") in vistos, f"pierde la forma larga: {vistos}"
    assert any(not _es_fichero(o, d) for o, d in vistos), "no ve ningún directorio"


CANARIO_CORTO = '      - "./roster.json:/censo.json:ro"\n'
# Origen SIN extensión, destino con ella: sigue siendo un mount de fichero, y
# mirar sólo el origen lo dejaba pasar. El mutante que quitaba el destino de la
# comprobación sobrevivía porque en las composiciones de hoy los dos extremos
# tienen extensión — el hueco existía y no había forma de verlo.
CANARIO_SIN_EXT_EN_ORIGEN = '      - "./censo:/censo.json:ro"\n'
CANARIO_LARGO = """      - type: bind
        source: ./roster.json
        target: /censo.json
        read_only: true
"""
# El MISMO montaje con las claves en otro orden. Es Compose igual de válido, y el
# parser anterior no lo veía porque exigía que el item empezara por `type:`.
CANARIO_LARGO_DESORDENADO = """      - read_only: true
        target: /censo.json
        source: ./roster.json
        type: bind
"""


def test_el_canario_se_caza_en_LAS_DOS_formas():
    """El guarda de la lista de excepciones Y del parser, que son los dos sitios
    donde un guarda se muere en silencio.

    · Una excepción puede ensancharse hasta tragárselo todo.
    · Un parser que sólo entiende la forma corta deja pasar la larga, que es
      Compose igual de válido. El mutante existía: reescribir el canario en forma
      larga sobrevivía al guarda anterior.
    · Mirar sólo el ORIGEN deja pasar `./censo:/censo.json`, que es un mount de
      fichero con el origen disfrazado. Ese mutante también sobrevivía, porque en
      las composiciones de hoy los dos extremos llevan extensión.
    · Y exigir que el mapa empiece por `type:` hacía invisible el MISMO montaje
      con las claves en otro orden. El orden de las claves de un mapa YAML no
      significa nada; que decidiera si el guarda mira, sí.

    El canario es el montaje EXACTO que rompió el organigrama. Si deja de cazarse
    en cualquiera de las dos formas, el guarda ya no protege de la clase que su
    nombre promete."""
    for etiqueta, texto in (("corta", CANARIO_CORTO), ("larga", CANARIO_LARGO),
                            ("larga desordenada", CANARIO_LARGO_DESORDENADO),
                            ("sin extensión en el origen", CANARIO_SIN_EXT_EN_ORIGEN)):
        montajes = list(_montajes(texto))
        assert montajes, f"el parser no ve la forma {etiqueta}: {texto!r}"
        origen, destino = montajes[0]
        assert _es_fichero(origen, destino), f"forma {etiqueta}: {origen} → {destino}"
        assert _aceptado(origen, destino) is None, (
            f"un riesgo aceptado se tragó el canario en forma {etiqueta} "
            f"({origen} → {destino}): el guarda ya no protege de nada")


# ── ③ el override GENERADO ───────────────────────────────────────────────────

def test_el_generador_no_introduce_mounts_de_fichero_de_CONFIG(tmp_path):
    """El tercer contrato, y el único que puede hablar del override: no una copia
    estática, sino EJECUTAR el generador y mirar lo que produce. Así una regresión
    de `llmi init` —volver a montar un fichero de configuración suelto— también se
    pone roja.

    Corre hermético: se copia `llmi` a un tmp (su `DIR` sale de su propia ruta) y
    se le da un `HOME` propio, así que no toca el repo ni la máquina. Verificado
    antes de escribirlo, porque `llmi init` está marcado ⛔ en este repo por lo que
    destruye si se ejecuta en el sitio equivocado.

    FALSADOR: que el generador emita `- ./roster.json:/censo.json:ro` pone esto
    rojo — y ése es exactamente el montaje que rompió el organigrama."""
    import subprocess
    repo, casa = tmp_path / "repo", tmp_path / "home"
    repo.mkdir()
    casa.mkdir()
    for f in ("llmi", "docker-compose.yml", "roster.example.json"):
        shutil.copy(RAIZ / f, repo / f)
    (casa / "LEDGER.md").write_text("### [cto-A → backend · FYI] uno\ncuerpo\n")

    r = subprocess.run([str(repo / "llmi"), "init", "--demo"], cwd=repo,
                       env={"HOME": str(casa), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
                       capture_output=True, text=True, timeout=180, check=False)
    # EL CÓDIGO DE SALIDA, no sólo que el fichero exista. `check=False` calla a
    # Ruff y no demuestra nada: un generador que escribe el override y luego falla
    # dejaría este test en verde con el proceso roto.
    assert r.returncode == 0, f"`llmi init` salió {r.returncode}:\n{r.stdout}\n{r.stderr}"
    override = repo / "docker-compose.override.yml"
    assert override.exists(), f"el generador no produjo override:\n{r.stdout}{r.stderr}"

    montajes = list(_montajes(override.read_text()))
    assert montajes, "el override generado no trae volúmenes reconocibles"
    ficheros = [(o, d) for o, d in montajes if _es_fichero(o, d)]
    # Los ledgers SÍ salen como fichero — es el riesgo aceptado y documentado.
    # Cualquier OTRO fichero suelto es una regresión del generador.
    fuera = [f"{o} → {d}" for o, d in ficheros if not _aceptado(o, d)]
    assert not fuera, (
        "`llmi init` genera bind-mounts de FICHERO fuera del riesgo aceptado:\n  "
        + "\n  ".join(fuera))
    assert ficheros, "no generó NINGÚN mount de fichero: el control no mide nada"


def test_el_riesgo_aceptado_sigue_cubriendo_algo_real(tmp_path):
    """CONTROL del riesgo aceptado, y vive AQUÍ y no arriba: sólo se puede
    comprobar contra un override de verdad, y el único que este test puede exigir
    es el que él mismo genera.

    Un riesgo aceptado que ya no casa con nada es deuda invisible: parece cubrir
    algo, no cubre nada, y tapará el día que alguien reintroduzca ese montaje.

    FALSADOR: cuando los ledgers dejen de montarse como fichero —que es el
    objetivo— esto se pone rojo y obliga a RETIRAR la entrada."""
    import subprocess
    repo, casa = tmp_path / "repo", tmp_path / "home"
    repo.mkdir()
    casa.mkdir()
    for f in ("llmi", "docker-compose.yml", "roster.example.json"):
        shutil.copy(RAIZ / f, repo / f)
    (casa / "LEDGER.md").write_text("### [cto-A → backend · FYI] uno\ncuerpo\n")
    r = subprocess.run([str(repo / "llmi"), "init", "--demo"], cwd=repo,
                       env={"HOME": str(casa), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
                       capture_output=True, text=True, timeout=180, check=False)
    assert r.returncode == 0, f"`llmi init` salió {r.returncode}:\n{r.stdout}\n{r.stderr}"
    usados = {_aceptado(o, d) for o, d in _montajes(
        (repo / "docker-compose.override.yml").read_text()) if _es_fichero(o, d)}
    huerfanos = {m for _, _, m in RIESGO_ACEPTADO} - usados
    assert not huerfanos, (
        f"riesgos aceptados que ya no cubren nada: {sorted(huerfanos)} — retíralos "
        "en vez de dejarlos tapando algo que ya no existe")


def test_la_composicion_publicada_no_monta_el_checkout():
    """El perímetro de lectura del proceso, como propiedad y no como comentario.

    Mi primer intento montaba `.` entero para llegar a dos ficheros de estado. Eso
    da acceso a `.git` —historial completo, que puede contener secretos retirados
    en commits posteriores—, a los backups `*.bak-*` y al override con las rutas
    reales de nueve repos. `:ro` limita la escritura, no la lectura: un path
    traversal pasaría de alcanzar un fichero a alcanzar el checkout entero.

    FALSADOR: devolver `- .:/repo:ro` pone esto rojo. El directorio dedicado
    (`.llminbox-state/`, que `llmi up` rellena) cubre el mismo caso sin exponer
    el resto."""
    raiz = {".", "./", "..", "../"}
    culpables = [f"{o} → {d}" for o, d in _montajes((RAIZ / "docker-compose.yml").read_text())
                 if o.strip() in raiz]
    assert not culpables, (
        "la composición publicada monta el CHECKOUT ENTERO en el contenedor: "
        + ", ".join(culpables))


# Estado que produce `llmi up` DENTRO del repo. Estas variables no pueden quedar
# a merced del override: si el propio `up` deja el fichero, la composición
# publicada tiene que saber dónde está. Las demás (p. ej. `LLMINBOX_CARRILES`,
# que apunta a una ruta del host fuera del repo) sí son externas y se interpolan.
#
# La distinción no se puede inferir del texto — se declara aquí, que es donde se
# puede discutir.
ESTADO_INTERNO = ("LLMINBOX_ROSTER", "LLMINBOX_MOUNTS_JSON")


def _env_publicado(texto: str):
    """`CLAVE: valor` del bloque `environment:` de la composición publicada."""
    dentro = False
    for ln in texto.splitlines():
        if re.match(r"\s*environment\s*:\s*$", ln):
            dentro = True
            continue
        if dentro:
            if ln.strip() and not ln.startswith(" " * 6):
                dentro = False
                continue
            m = re.match(r'\s+([A-Z_][A-Z0-9_]*)\s*:\s*"?([^"\n#]*)"?\s*$', ln)
            if m:
                yield m.group(1), m.group(2).strip()


def test_ninguna_ruta_de_entorno_apunta_donde_no_hay_nada_montado():
    """El guarda de la clase, no del caso.

    `llmi up` empezó a dejar el mapa de montajes en `/state/mounts.json` y la
    composición publicada seguía con `LLMINBOX_MOUNTS_JSON` vacío por defecto:
    `_cargar_carriles()` devolvía {} aunque el fichero estuviera ahí. La variable
    no señalaba a ninguna parte y **nadie lo veía**, porque un mapa de carriles
    vacío degrada en silencio.

    La propiedad general: toda ruta ABSOLUTA fijada en la composición publicada
    tiene que caer dentro de algún destino montado. Las interpolaciones
    (`${VAR:-}`) se saltan: su valor lo pone el override, que no está versionado.

    FALSADOR: dejar `LLMINBOX_MOUNTS_JSON` vacío, o apuntarlo a `/mounts.json`
    sin montar nada ahí, pone esto rojo con el nombre de la variable."""
    texto = (RAIZ / "docker-compose.yml").read_text()
    destinos = [d for _, d in _montajes(texto)] + ["/data"]
    huerfanas = []
    for clave, valor in _env_publicado(texto):
        if not valor.startswith("/") or "${" in valor:
            continue
        if not any(valor == d or valor.startswith(d.rstrip("/") + "/") for d in destinos):
            huerfanas.append(f"{clave}={valor}")
    assert not huerfanas, (
        "variables de entorno que apuntan donde no hay nada montado — el servicio "
        f"degrada en silencio: {huerfanas}  (destinos montados: {sorted(set(destinos))})")


def test_el_lector_de_entorno_ve_de_verdad():
    """CONTROL: si `_env_publicado` no leyera nada, el test de arriba pasaría
    siempre. Verde por no mirar, otra vez."""
    claves = dict(_env_publicado((RAIZ / "docker-compose.yml").read_text()))
    assert "LLMINBOX_ROSTER" in claves, claves
    assert claves["LLMINBOX_ROSTER"].startswith("/"), claves


def test_el_estado_que_produce_llmi_up_no_queda_a_merced_del_override():
    """El agujero que dejó vivo el guarda anterior, y que CodeRabbit encontró:
    `llmi up` empezó a dejar el mapa de montajes en `/state/mounts.json` y la
    composición publicada seguía con `LLMINBOX_MOUNTS_JSON: "${...:-}"`. Una
    interpolación VACÍA apunta a ninguna parte, así que `_cargar_carriles()`
    devolvía {} con el fichero montado delante — y un mapa de carriles vacío
    degrada en SILENCIO.

    El guarda de rutas huérfanas no lo veía porque salta las interpolaciones, y
    saltarlas es correcto para las variables genuinamente externas. La diferencia
    hay que DECLARARLA: lo que `llmi up` produce dentro del repo no puede depender
    de un fichero que no está versionado.

    FALSADOR: devolver cualquiera de las dos a `"${VAR:-}"` pone esto rojo."""
    valores = dict(_env_publicado((RAIZ / "docker-compose.yml").read_text()))
    for clave in ESTADO_INTERNO:
        v = valores.get(clave, "")
        assert v and not v.startswith("${") and v.startswith("/"), (
            f"{clave}={v!r} — `llmi up` produce ese fichero dentro del repo, así que "
            "la composición PUBLICADA tiene que decir dónde está. Con una "
            "interpolación vacía el servicio degrada sin un solo error.")


def test_si_el_estado_no_se_puede_preparar_NO_se_levanta_el_contenedor(tmp_path):
    """FAIL-CLOSED de `llmi up`, con Docker falso para poder demostrarlo.

    El script lleva `set -uo pipefail`, **no** `set -e`: sin `|| exit` explícito,
    un `mkdir`/`cp` que falle sigue adelante y levanta el contenedor con el estado
    a medias — censo ausente, mapa de carriles vacío, todo degradando en silencio.
    Arrancar con el estado roto es peor que no arrancar.

    Se fuerza el fallo de la forma más simple y menos mágica: `.llminbox-state`
    existe como FICHERO, así que `mkdir -p` no puede crear el directorio.

    Y se pone un `docker` FALSO delante en el PATH que deja una marca al ser
    invocado. Comprobar sólo el código de salida no bastaría: lo que importa es
    que NO se llegue a Docker.

    FALSADOR: quitar los `|| exit 1` deja rc=0 (o el de docker) y, sobre todo,
    hace aparecer la marca — el contenedor se habría levantado."""
    import subprocess
    repo, casa, binfalso = tmp_path / "repo", tmp_path / "home", tmp_path / "bin"
    for d in (repo, casa, binfalso):
        d.mkdir()
    for f in ("llmi", "docker-compose.yml", "roster.example.json"):
        shutil.copy(RAIZ / f, repo / f)
    (repo / "roster.json").write_text('{"agentes": [], "humanos": [], "difusion": []}')
    (repo / ".llmi-mounts.json").write_text("{}")
    (casa / ".llminbox.token").write_text("token-de-prueba")
    marca = tmp_path / "docker-fue-invocado"
    (binfalso / "docker").write_text(
        f'#!/bin/sh\n{_SIN_CONTENEDOR}touch "{marca}"\nexit 0\n')
    (binfalso / "docker").chmod(0o755)
    # El estado NO se puede preparar: ya hay un fichero con ese nombre.
    (repo / ".llminbox-state").write_text("soy un fichero, no un directorio")

    r = subprocess.run([str(repo / "llmi"), "up"], cwd=repo,
                       env={"HOME": str(casa),
                            "PATH": f"{binfalso}:/usr/bin:/bin:/usr/sbin:/sbin"},
                       capture_output=True, text=True, timeout=120, check=False)
    assert r.returncode != 0, f"siguió adelante con el estado roto:\n{r.stdout}\n{r.stderr}"
    assert not marca.exists(), (
        "se invocó a Docker con el estado sin preparar: el contenedor habría "
        "arrancado con censo ausente y mapa de carriles vacío, degradando en "
        "silencio")


# EL DOBLE DE `docker` TIENE QUE HABLAR COMO DOCKER. Todos los dobles de este fichero
# decían `exit 0` a CUALQUIER subcomando sin imprimir nada — incluido `inspect`, que en
# el Docker de verdad o falla (no existe ese contenedor) o imprime. Ese estado imposible
# dejó en rojo 14 tests en cuanto `llmi up` empezó a preguntarle a Docker si el servicio
# que va a reemplazar tiene V8 encendido: `v8_se_apaga` leía «hay contenedor pero no sé
# su entorno» y declaraba NO MEDIDO, que es lo correcto ante esa respuesta.
# En estos sandboxes NO hay contenedor, así que el doble lo dice — Y LO DICE COMO
# Docker: `No such object` por stderr. Sin ese mensaje, «falló el inspect» y «no hay
# contenedor» son indistinguibles, y confundirlas es el P0 que Security reprodujo:
# un daemon que tarda un instante se leería como arranque en frío y apagaría V8.
_SIN_CONTENEDOR = (
    '[ "$1" = "inspect" ] && { echo "Error: No such object: $2" >&2; exit 1; }\n')


def _up_hermetico(tmp_path, roster_json, estado_previo=None, aplicado=None,
                  extra=(), env_contenedor=None):
    """`llmi up` con un `docker` FALSO que registra sus argumentos. Devuelve
    (returncode, argumentos_de_docker)."""
    import subprocess
    repo, casa, binfalso = tmp_path / "repo", tmp_path / "home", tmp_path / "bin"
    for d in (repo, casa, binfalso):
        d.mkdir(parents=True, exist_ok=True)
    for f in ("llmi", "docker-compose.yml", "roster.example.json"):
        shutil.copy(RAIZ / f, repo / f)
    (repo / "roster.json").write_text(roster_json)
    (repo / ".llmi-mounts.json").write_text("{}")
    (casa / ".llminbox.token").write_text("token-de-prueba")
    if estado_previo is not None:
        (repo / ".llminbox-state").mkdir(exist_ok=True)
        (repo / ".llminbox-state" / "roster.json").write_text(estado_previo)
        (repo / ".llminbox-state" / "mounts.json").write_text("{}")
    if aplicado is not None:
        (repo / ".llmi-applied").write_text(aplicado)
    args = tmp_path / "docker-args.txt"
    # `docker ps` devuelve VACÍO a propósito: simula a la vez un nombre de
    # contenedor personalizado (`LLMINBOX_NAME`) y un fallo temporal del daemon.
    # Con el detector que preguntaba a Docker, los dos casos impedían la recarga.
    # `docker inspect` FALLA a propósito: en este sandbox NO hay ningún contenedor, y
    # eso es lo que contesta el Docker de verdad ante un nombre que no existe. El doble
    # decía `exit 0` sin imprimir nada —un estado que Docker no produce jamás: un
    # contenedor que existe SIEMPRE tiene `Config.Env`— y con eso `v8_se_apaga` veía
    # «hay contenedor, no sé su entorno» y declaraba NO MEDIDO, dejando en rojo los 14
    # tests de este fichero. El doble mentía sobre la plataforma, no el código.
    if env_contenedor is None:
        _inspect = _SIN_CONTENEDOR
    else:
        _inspect = f'[ "$1" = "inspect" ] && {{ printf %s \'{env_contenedor}\'; exit 0; }}\n'
    (binfalso / "docker").write_text(
        f'#!/bin/sh\n[ "$1" = "ps" ] && exit 0\n'
        f'{_inspect}'
        f'echo "$@" >> "{args}"\nexit 0\n')
    (binfalso / "docker").chmod(0o755)
    r = subprocess.run([str(repo / "llmi"), "up", *extra], cwd=repo,
                       env={"HOME": str(casa),
                            "PATH": f"{binfalso}:/usr/bin:/bin:/usr/sbin:/sbin"},
                       capture_output=True, text=True, timeout=120, check=False)
    return r, (args.read_text() if args.exists() else "")


A = '{"agentes": [{"nombre": "backend", "humano": "a", "clave": "", "rol": "be"}], "humanos": [], "difusion": []}'
B = ('{"agentes": [{"nombre": "backend", "humano": "a", "clave": "", "rol": "be"},'
     ' {"nombre": "prueba-nueva", "humano": "a", "clave": "", "rol": "prueba"}],'
     ' "humanos": [], "difusion": []}')


def test_si_el_censo_cambia_el_contenedor_se_RECREA(tmp_path):
    """El agujero que quedaba, y es el que decide si el piloto del EM funciona.

    `llmi up` copia el estado a `/state`, pero el reinicio lo delega en
    `docker compose up -d --build`, que **reutiliza** el contenedor cuando imagen
    y config no cambian — y estos ficheros no son config de Compose: viven dentro
    de un directorio montado. El servicio lee censo y mapa de carriles AL
    IMPORTAR, así que el proceso seguiría con el censo viejo: se habría arreglado
    el inodo del mount y recreado el MISMO estado rancio una capa más adentro, en
    las estructuras de Python.

    Es exactamente lo que el alta del EM necesita: cambias el roster y el proceso
    tiene que observarlo.

    FALSADOR: quitar el `--force-recreate` condicional deja los argumentos sin él
    y el proceso arrancaría con el censo anterior."""
    r, args = _up_hermetico(tmp_path, B, estado_previo=A)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "--force-recreate" in args, (
        f"el censo cambió y no se recrea el contenedor: el proceso seguiría con el "
        f"anterior.\nargumentos de docker: {args!r}")
    assert "recreo el contenedor" in r.stdout, (
        "recrea sin decirlo: cortar la bandeja de la flota no puede ser un efecto "
        "colateral silencioso")


def test_si_el_censo_NO_cambia_no_se_corta_la_bandeja_de_la_flota(tmp_path):
    """CONTROL, y no es simetría decorativa: `--force-recreate` incondicional
    reiniciaría el buzón compartido en CADA `llmi up`, que es un comando de uso
    diario. Cortar la bandeja de 20 agentes tiene que ser deliberado, no un efecto
    colateral de un comando genérico.

    FALSADOR: poner `--force-recreate` fijo pone esto rojo."""
    r, args = _up_hermetico(tmp_path, A, estado_previo=A, aplicado=_hash(A))
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "--force-recreate" not in args, (
        f"recrea sin motivo: corta la bandeja de la flota en cada `up`.\n{args!r}")


def test_ni_un_nombre_distinto_ni_un_docker_mudo_impiden_la_recarga(tmp_path):
    """El detector preguntaba a Docker si había contenedor vivo, y eso fallaba dos
    veces: fijaba el nombre `llminbox` —cuando Compose soporta `LLMINBOX_NAME`
    para una segunda instancia— y trataba «no pude determinar si está vivo» como
    «no está vivo». Fail-OPEN en el camino que promete frescura.

    Aquí `docker ps` devuelve vacío, que es lo que se ve con un nombre distinto Y
    con un daemon que falla. El estado cambió: hay que recargar igual.

    FALSADOR: devolver el `docker ps ... | grep -q llminbox` deja el recreate
    fuera y el proceso arranca con el censo anterior."""
    r, args = _up_hermetico(tmp_path, B, estado_previo=A)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "--force-recreate" in args, (
        f"con `docker ps` mudo no recarga: un nombre personalizado o un daemon "
        f"con hipo dejarían el proceso rancio.\n{args!r}")


def test_lo_anunciado_corresponde_a_los_BYTES_INSTALADOS(tmp_path):
    """El TOCTOU, convertido en invariante comprobable.

    Comparar la FUENTE y copiarla después permite que otra sesión la sustituya en
    medio: `/state` acaba con B, el proceso conserva A, y `llmi up` sale con éxito
    sin recrear. El detector reintroducía la clase de fallo que esta rama cierra.

    La propiedad que se fija: **lo que se anuncia describe los bytes que quedaron
    instalados**. Se comprueban las dos direcciones sobre el fichero publicado.

    ⚠️ Esto NO demuestra ausencia de carrera —haría falta interponer entre medida
    y publicación—: fija la invariante que el staging hace cierta bajo
    concurrencia. Lo digo porque afirmar lo contrario sería el defecto del día."""
    r, _ = _up_hermetico(tmp_path, B, estado_previo=A)
    instalado = (tmp_path / "repo" / ".llminbox-state" / "roster.json").read_text()
    assert instalado == B, "publicó unos bytes distintos de los que iba a instalar"
    assert "recreo el contenedor" in r.stdout, "instaló bytes nuevos y no lo anunció"

    otro = tmp_path / "b"
    otro.mkdir()
    r2, _ = _up_hermetico(otro, B, estado_previo=B, aplicado=_hash(B))
    assert "recreo el contenedor" not in r2.stdout, "anunció un cambio que no existió"


def test_una_escritura_entre_medir_y_publicar_no_puede_pasar_desapercibida(tmp_path):
    """EL TOCTOU, falsado de forma DETERMINISTA — sin dormir ni carreras reales.

    La ventana es exactamente ésta: otra sesión sustituye `roster.json` DESPUÉS de
    que midas y ANTES de que publiques. Se reproduce interponiendo un `cmp` falso
    que, tras comparar, reescribe la fuente. Eso coloca la escritura ajena justo
    en el hueco, sin depender del reloj.

    · Comparando la FUENTE (el defecto): `cmp` ve A==A → «sin cambios» → no
      recrea; luego `cp` copia la B que apareció en medio. Queda instalado B con
      el proceso en A y `llmi up` diciendo que no pasó nada. Silencioso y verde.

    · Comparando los bytes YA PREPARADOS: la copia se hizo antes, así que lo
      medido y lo publicado son los mismos bytes. Lo que se anuncia describe lo
      que queda instalado, pase lo que pase con la fuente en medio.

    FALSADOR: volver a `cmp -s "$ROSTER" ...` deja instalado B con «sin cambios»
    anunciado, y las dos aserciones caen."""
    import subprocess
    repo, casa, binfalso = tmp_path / "repo", tmp_path / "home", tmp_path / "bin"
    for d in (repo, casa, binfalso):
        d.mkdir()
    for f in ("llmi", "docker-compose.yml", "roster.example.json"):
        shutil.copy(RAIZ / f, repo / f)
    (repo / "roster.json").write_text(A)
    (repo / ".llmi-mounts.json").write_text("{}")
    (casa / ".llminbox.token").write_text("token-de-prueba")
    (repo / ".llminbox-state").mkdir()
    (repo / ".llminbox-state" / "roster.json").write_text(A)
    (repo / ".llminbox-state" / "mounts.json").write_text("{}")
    # El sello de APLICADO acredita que el proceso ya cargó A. Sin él, `up` recrea
    # siempre —y con razón—, y este test dejaría de medir la ventana que busca.
    (repo / ".llmi-applied").write_text(_hash(A))
    (binfalso / "docker").write_text(f"#!/bin/sh\n{_SIN_CONTENEDOR}exit 0\n")
    (binfalso / "docker").chmod(0o755)
    # `cmp` real primero, y DESPUÉS la escritura ajena: la ventana exacta.
    #
    # El contenido va por FICHERO auxiliar, no incrustado en el script: mi primera
    # versión metía `json.dumps(B)!r` con un `.replace("'", "")` encima y lo que
    # acababa escrito era `{\"agentes\": ...}` con barras literales — basura, no B.
    # El test seguía en verde, así que afirmaba reproducir una ventana con un
    # contenido que nunca escribía. Cazado por CodeRabbit.
    aux = tmp_path / "roster-B.json"
    aux.write_text(B)
    marca = tmp_path / "escritura-ajena-ocurrio"
    # Se interpone en `cat`, que es lo que LEE los bytes preparados para calcular
    # el hash deseado. (La versión anterior interponía en `cmp`, y `cmp` ya no se
    # usa: la comparación pasó a ser por hash. Un falsador que interpone donde ya
    # no pasa nada no falsa nada — cazado al cambiar la implementación.)
    cat_real = shutil.which("cat", path="/bin:/usr/bin:/usr/local/bin")
    assert cat_real, "no encuentro `cat` real: el arnés no puede montar la ventana"
    (binfalso / "cat").write_text(
        f'#!/bin/sh\n"{cat_real}" "$@"\n_rc=$?\n'
        f'cp -f "{aux}" "{repo}/roster.json" && : > "{marca}"\nexit $_rc\n')
    (binfalso / "cat").chmod(0o755)
    # Este test NO va de V8, pero `up` pregunta ahora a Docker antes de tocar nada, y
    # sin doble el `docker` ni siquiera está en este PATH: se pararía en NO MEDIDO
    # —correctamente— sin llegar a abrir la ventana que aquí se mide.
    (binfalso / "docker").write_text(f"#!/bin/sh\n{_SIN_CONTENEDOR}exit 0\n")
    (binfalso / "docker").chmod(0o755)

    r = subprocess.run([str(repo / "llmi"), "up"], cwd=repo,
                       env={"HOME": str(casa),
                            "PATH": f"{binfalso}:/usr/bin:/bin:/usr/sbin:/sbin"},
                       capture_output=True, text=True, timeout=120, check=False)
    # LA VENTANA TIENE QUE HABER OCURRIDO. Sin esto, un `cmp` falso que fallara en
    # silencio dejaría el test en verde habiendo probado NADA — que es exactamente
    # lo que pasaba antes de arreglar el citado.
    assert marca.exists(), (
        "la escritura ajena no llegó a ocurrir: el test no reprodujo la ventana y "
        "no demuestra nada")
    assert (repo / "roster.json").read_text() == B, (
        "la fuente no quedó en B: la escritura ajena escribió otra cosa")
    instalado = (repo / ".llminbox-state" / "roster.json").read_text()
    anuncio = "recreo el contenedor" in r.stdout
    assert (instalado != A) == anuncio, (
        f"lo instalado y lo anunciado no coinciden: instalado={'B' if instalado != A else 'A'}, "
        f"anunciado_cambio={anuncio}\n{r.stdout}")
    assert instalado == A, (
        "publicó bytes que nunca midió: la escritura ajena entró entre la medida y "
        "la publicación")


def test_up_no_construye_y_build_es_una_decision_aparte(tmp_path):
    """BUILD ≠ UP ≠ STATE, y el shadow del PR #11 demostró por qué hace falta.

    `llmi up` hacía `docker compose up -d --build`, y dos `up` idénticos sin tocar
    nada producían IMÁGENES DISTINTAS. Una imagen nueva hace que Compose recree el
    contenedor, así que `llmi up` cortaba la bandeja de la flota SIEMPRE — y el
    gate de estado, aun siendo correcto, era impotente: no anunciaba recreate y el
    recreate ocurría igual.

    No basta con quitar `--build`: `docker compose up` construye por su cuenta los
    servicios con `build:` si no encuentra imagen. `--no-build` es lo único que lo
    GARANTIZA.

    Consecuencia buscada, no evitada: cambiar `servicio.py` y hacer sólo `llmi up`
    ya no despliega el código nuevo. Hay que pedir `llmi build`. El despliegue pasa
    a ser una acción explícita en vez de un efecto secundario de «asegúrate de que
    está levantado».

    FALSADOR: devolver `--build` al `up` hace que aparezca en los argumentos y que
    `--no-build` desaparezca."""
    r, args = _up_hermetico(tmp_path, A, estado_previo=A, aplicado=_hash(A))
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "--no-build" in args, f"`up` puede construir: {args!r}"
    assert "compose build" not in args, f"`up` construyó sin que se lo pidieran: {args!r}"
    assert "--force-recreate" not in args, args


def test_up_con_build_lo_anuncia_como_operacion_que_corta(tmp_path):
    """`--build` explícito sigue disponible, pero DICE lo que hace. Esconder otra
    vez la construcción dentro del `up` normal sería reintroducir el defecto con
    otro nombre.

    FALSADOR: construir en silencio deja el aviso fuera y esto se pone rojo."""
    import shutil
    import subprocess
    repo, casa, binfalso = tmp_path / "repo", tmp_path / "home", tmp_path / "bin"
    for d in (repo, casa, binfalso):
        d.mkdir()
    for f in ("llmi", "docker-compose.yml", "roster.example.json"):
        shutil.copy(RAIZ / f, repo / f)
    (repo / "roster.json").write_text(A)
    (repo / ".llmi-mounts.json").write_text("{}")
    (casa / ".llminbox.token").write_text("t")
    args = tmp_path / "docker-args.txt"
    (binfalso / "docker").write_text(
        f'#!/bin/sh\n{_SIN_CONTENEDOR}echo "$@" >> "{args}"\nexit 0\n')
    (binfalso / "docker").chmod(0o755)
    r = subprocess.run([str(repo / "llmi"), "up", "--build"], cwd=repo,
                       env={"HOME": str(casa),
                            "PATH": f"{binfalso}:/usr/bin:/bin:/usr/sbin:/sbin"},
                       capture_output=True, text=True, timeout=120, check=False)
    txt = args.read_text() if args.exists() else ""
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "compose build" in txt, f"`--build` no construyó: {txt!r}"
    assert "--no-build" in txt, f"el `up` posterior no lleva --no-build: {txt!r}"
    assert "PUEDE recrear" in r.stdout, "construyó sin avisar de que puede cortar la bandeja"


DOCS_CON_QUICKSTART = ("README.md", "CONTRIBUTING.md")


def test_la_documentacion_no_promete_que_up_construya():
    """La interfaz no puede prometer una propiedad que los bytes ya no cumplen.

    El Quick Start decía `./llmi up  # builds and starts the container`. Desde que
    `up` lleva `--no-build`, eso es FALSO: en un checkout limpio el usuario sigue
    la documentación al pie de la letra y su primer `up` no tiene imagen que
    arrancar. Es el mismo antipatrón que este repo lleva la sesión entera
    quitando, sólo que en la puerta de entrada.

    Se comprueba lo que el código HACE contra lo que la documentación DICE, no la
    documentación contra sí misma.

    FALSADOR: devolver `--build` al `up` sin tocar el README —o al revés— pone
    esto rojo."""
    up_construye = "--no-build" not in (RAIZ / "llmi").read_text()
    for nombre in DOCS_CON_QUICKSTART:
        ruta = RAIZ / nombre
        if not ruta.exists():
            continue
        texto = ruta.read_text()
        # SÓLO líneas de COMANDO, no prosa. La primera versión de este guarda
        # marcaba mi propia frase explicativa (`./llmi build && ./llmi up`) como
        # si prometiera que `up` construye: un guarda con falso positivo se
        # desactiva a la semana, y entonces no guarda nada.
        promete = [ln for ln in texto.splitlines()
                   if ln.strip().startswith(("./llmi up", "llmi up"))
                   and "build" in ln.lower() and "--build" not in ln
                   and "WITHOUT rebuilding" not in ln]
        assert bool(promete) == up_construye, (
            f"{nombre} y `llmi` no dicen lo mismo sobre si `up` construye "
            f"(código construye={up_construye}): {promete}")
        if not up_construye:
            # También como LÍNEA DE COMANDO: la prosa que explica la separación
            # contiene «llmi build» y hacía pasar la aserción aunque el Quick
            # Start lo hubiera perdido. Lo que tiene que poder copiar quien llega
            # es el comando, no la explicación.
            assert any(ln.strip().startswith(("./llmi build", "llmi build"))
                       for ln in texto.splitlines()), (
                f"{nombre} no enseña `llmi build` como comando, así que quien "
                "siga el Quick Start no tendrá imagen que arrancar")


# ── ESTADO DESEADO vs ESTADO APLICADO ────────────────────────────────────────
# Los bytes publicados en `/state` NO acusan que el proceso los haya cargado.
# Comparar «lo publicado con lo publicado» deja dos huecos, y los dos terminan en
# un servicio VIVO sirviendo estado viejo sin decirlo.

def _up(tmp_path, roster, *, args=(), build_falla=False, up_falla=False,
        aplicado=None, estado=None):
    """`llmi up` hermético. Devuelve (proceso, argumentos_docker, sello_aplicado)."""
    import subprocess
    repo, casa, binfalso = tmp_path / "repo", tmp_path / "home", tmp_path / "bin"
    for d in (repo, casa, binfalso):
        d.mkdir(parents=True, exist_ok=True)
    for f in ("llmi", "docker-compose.yml", "roster.example.json"):
        shutil.copy(RAIZ / f, repo / f)
    (repo / "roster.json").write_text(roster)
    (repo / ".llmi-mounts.json").write_text("{}")
    (casa / ".llminbox.token").write_text("t")
    if estado is not None:
        (repo / ".llminbox-state").mkdir(exist_ok=True)
        (repo / ".llminbox-state" / "roster.json").write_text(estado)
        (repo / ".llminbox-state" / "mounts.json").write_text("{}")
    if aplicado is not None:
        (repo / ".llmi-applied").write_text(aplicado)
    reg = tmp_path / "docker-args.txt"
    fallos = []
    if build_falla:
        fallos.append('[ "$2" = "build" ] && exit 1')
    if up_falla:
        fallos.append('[ "$2" = "up" ] && exit 1')
    (binfalso / "docker").write_text(
        "#!/bin/sh\n" + _SIN_CONTENEDOR + "\n".join(fallos)
        + f'\necho "$@" >> "{reg}"\nexit 0\n')
    (binfalso / "docker").chmod(0o755)
    r = subprocess.run([str(repo / "llmi"), "up", *args], cwd=repo,
                       env={"HOME": str(casa),
                            "PATH": f"{binfalso}:/usr/bin:/bin:/usr/sbin:/sbin"},
                       capture_output=True, text=True, timeout=120, check=False)
    sello = (repo / ".llmi-applied")
    return r, (reg.read_text() if reg.exists() else ""), (
        sello.read_text() if sello.exists() else None)


def _hash(roster, mapa_sha="sin-mapa"):
    """El estado deseado cubre TRES cosas, no dos.

    Al mapa de credenciales de V8 se le añadió su huella aquí a propósito: entra por un
    bind-mount cuya RUTA no cambia, así que cambiarlo no cambiaba nada que Compose
    pudiera ver — reutilizaba el contenedor y el proceso seguía con el mapa viejo,
    mientras el sello declaraba el nuevo. Metiéndolo en el hash, un mapa distinto entra
    por la rama de recreación que ya existía.
    """
    import hashlib
    return hashlib.sha256(
        (roster + "{}" + f"v8-mapa:{mapa_sha}\n").encode()).hexdigest()


def test_si_el_BUILD_falla_el_sello_no_avanza_y_el_siguiente_up_recrea(tmp_path):
    """El hueco que encontró CodeRabbit: se publicaba B, el build fallaba, el
    script salía — y el siguiente `up` comparaba B con B, no recreaba, y el
    proceso seguía sirviendo A. Vivo y en silencio.

    FALSADOR: publicar antes de construir deja `/state` en B con el sello en A y
    el aviso perdido; peor aún si además se avanzara el sello."""
    r, args, sello = _up(tmp_path, B, args=("--build",), build_falla=True,
                         aplicado=_hash(A), estado=A)
    assert r.returncode != 0, f"el build falló y salió con éxito:\n{r.stdout}"
    assert sello == _hash(A), f"el sello avanzó con el build roto: {sello}"
    assert "compose up" not in args, "levantó pese a fallar el build"
    assert (tmp_path / "repo" / ".llminbox-state" / "roster.json").read_text() == A, (
        "publicó B con el build roto: el siguiente `up` compararía B con B")
    # Y el siguiente intento, ya sin fallo, SÍ recrea.
    _r2, args2, sello2 = _up(tmp_path / "2", B, aplicado=_hash(A), estado=A)
    assert "--force-recreate" in args2, f"el siguiente `up` no recupera: {args2!r}"
    assert sello2 == _hash(B)


def test_si_el_UP_falla_el_sello_no_avanza(tmp_path):
    """El segundo hueco, que mover el build NO cubre: `/state` ya tiene B y
    `compose up` revienta, así que el contenedor viejo sigue vivo con A.
    Comparando publicado-con-publicado, el siguiente `up` no recrearía nunca.

    FALSADOR: avanzar el sello antes de que Docker termine bien — el mismo bug
    con otra representación."""
    r, _args, sello = _up(tmp_path, B, up_falla=True, aplicado=_hash(A), estado=A)
    assert r.returncode != 0, "el `up` falló y salió con éxito"
    assert sello == _hash(A), f"el sello avanzó con el `up` roto: {sello}"
    _r2, args2, _ = _up(tmp_path / "2", B, aplicado=_hash(A), estado=B)
    assert "--force-recreate" in args2, (
        f"con /state ya en B y el proceso en A, no recupera: {args2!r}")


def test_cuando_todo_va_bien_el_sello_avanza_y_el_siguiente_up_no_recrea(tmp_path):
    """CONTROL, y es el que impide «recrear siempre» como solución perezosa: tras
    un `up` exitoso el sello queda en B, y un `up` idéntico no debe tocar nada."""
    r, args, sello = _up(tmp_path, B, aplicado=_hash(A), estado=A)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "--force-recreate" in args
    assert sello == _hash(B), f"el sello no avanzó tras un `up` bueno: {sello}"
    _r2, args2, sello2 = _up(tmp_path / "2", B, aplicado=_hash(B), estado=B)
    assert "--force-recreate" not in args2, f"recrea sin motivo: {args2!r}"
    assert sello2 == _hash(B)


def test_un_typo_en_el_argumento_no_puede_parecerse_a_un_despliegue(tmp_path):
    """`llmi up --buidl` se ignoraba en silencio y arrancaba la imagen ANTERIOR:
    el operador cree que ha desplegado y no lo ha hecho.

    FALSADOR: aceptar cualquier argumento deja rc=0 y llama a Docker."""
    r, args, sello = _up(tmp_path, B, args=("--buidl",), aplicado=_hash(A), estado=A)
    assert r.returncode == 2, f"un typo no dio mal uso: rc={r.returncode}"
    assert "desconocido" in r.stderr, r.stderr
    assert args == "", f"invocó a Docker con un argumento inválido: {args!r}"
    assert sello == _hash(A), "tocó el sello con un argumento inválido"
    assert (tmp_path / "repo" / ".llminbox-state" / "roster.json").read_text() == A, (
        "publicó estado con un argumento inválido")


def test_build_repetido_tambien_es_mal_uso(tmp_path):
    """El contrato dice DOS formas, no «--build cuantas veces quieras». Aceptar
    `up --build --build` es aceptar una tercera forma que nadie declaró, y una
    interfaz que tolera lo que no documenta acaba documentándose por lo que
    tolera.

    FALSADOR: quitar la guarda deja rc=0 e invoca a Docker."""
    r, args, sello = _up(tmp_path, B, args=("--build", "--build"),
                         aplicado=_hash(A), estado=A)
    assert r.returncode == 2, f"rc={r.returncode}"
    assert "repetido" in r.stderr, r.stderr
    assert args == "", f"invocó a Docker: {args!r}"
    assert sello == _hash(A), "tocó el sello"


def test_si_no_se_puede_calcular_el_hash_no_se_publica_ni_se_sella(tmp_path):
    """El script lleva `set -uo pipefail` pero NO `set -e`. Sin cortar, un fallo
    de `cat`/`python3` seguía adelante, publicaba el staging y escribía un sello
    VACÍO — que ya no describe nada y hace que el gate mienta en la dirección que
    más duele: un sello vacío coincide con otro sello vacío.

    Se fuerza el fallo interponiendo un `python3` que revienta, que es lo que
    calcula el hash.

    FALSADOR: quitar el `||` deja el sello escrito y `/state` publicado con un
    valor que no describe esos bytes."""
    import subprocess
    repo, casa, binfalso = tmp_path / "repo", tmp_path / "home", tmp_path / "bin"
    for d in (repo, casa, binfalso):
        d.mkdir()
    for f in ("llmi", "docker-compose.yml", "roster.example.json"):
        shutil.copy(RAIZ / f, repo / f)
    (repo / "roster.json").write_text(B)
    (repo / ".llmi-mounts.json").write_text("{}")
    (casa / ".llminbox.token").write_text("t")
    (repo / ".llminbox-state").mkdir()
    (repo / ".llminbox-state" / "roster.json").write_text(A)
    (repo / ".llminbox-state" / "mounts.json").write_text("{}")
    (repo / ".llmi-applied").write_text(_hash(A))
    reg = tmp_path / "docker-args.txt"
    (binfalso / "docker").write_text(
        f'#!/bin/sh\n{_SIN_CONTENEDOR}echo "$@" >> "{reg}"\nexit 0\n')
    (binfalso / "docker").chmod(0o755)
    # `python3` revienta SÓLO al calcular el hash (recibe el script por -c).
    py_real = shutil.which("python3", path="/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin")
    assert py_real, "no encuentro python3 real"
    (binfalso / "python3").write_text(
        f'#!/bin/sh\ncase "$*" in *hashlib*) exit 1 ;; esac\nexec "{py_real}" "$@"\n')
    (binfalso / "python3").chmod(0o755)

    r = subprocess.run([str(repo / "llmi"), "up"], cwd=repo,
                       env={"HOME": str(casa),
                            "PATH": f"{binfalso}:/usr/bin:/bin:/usr/sbin:/sbin"},
                       capture_output=True, text=True, timeout=120, check=False)
    assert r.returncode != 0, f"siguió adelante sin poder calcular el hash:\n{r.stdout}"
    assert (repo / ".llmi-applied").read_text() == _hash(A), "selló con el hash roto"
    assert (repo / ".llminbox-state" / "roster.json").read_text() == A, (
        "publicó estado sin poder describir qué publicaba")
    assert not reg.exists(), f"invocó a Docker: {reg.read_text()!r}"


def test_dos_up_a_la_vez_no_se_pisan_el_sello(tmp_path):
    """Dos `llmi up` simultáneos —y en esta casa hay varias sesiones a la vez—
    compiten por el sello: uno puede mover el temporal del otro y dejar
    `.llmi-applied` describiendo un despliegue que no es el que corre.

    Se RECHAZA en vez de esperar: dos despliegues a la vez no son una cola, son un
    accidente, y adivinar cuál gana sería peor que negarse.

    Se simula el cerrojo tomado por otro proceso creándolo antes.

    FALSADOR: sin cerrojo, el segundo `up` entra y compite."""
    import subprocess
    repo, casa, binfalso = tmp_path / "repo", tmp_path / "home", tmp_path / "bin"
    for d in (repo, casa, binfalso):
        d.mkdir()
    for f in ("llmi", "docker-compose.yml", "roster.example.json"):
        shutil.copy(RAIZ / f, repo / f)
    (repo / "roster.json").write_text(A)
    (repo / ".llmi-mounts.json").write_text("{}")
    (casa / ".llminbox.token").write_text("t")
    (casa / ".llmi-lifecycle-llminbox.lock").mkdir()          # otro `up` ya está dentro
    reg = tmp_path / "docker-args.txt"
    (binfalso / "docker").write_text(
        f'#!/bin/sh\n{_SIN_CONTENEDOR}echo "$@" >> "{reg}"\nexit 0\n')
    (binfalso / "docker").chmod(0o755)
    r = subprocess.run([str(repo / "llmi"), "up"], cwd=repo,
                       env={"HOME": str(casa),
                            "PATH": f"{binfalso}:/usr/bin:/bin:/usr/sbin:/sbin"},
                       capture_output=True, text=True, timeout=120, check=False)
    assert r.returncode != 0, "entró con el cerrojo tomado"
    assert "otro `llmi up`" in r.stderr, r.stderr
    assert not reg.exists(), f"invocó a Docker con el cerrojo tomado: {reg.read_text()!r}"
    assert (casa / ".llmi-lifecycle-llminbox.lock").exists(), "se llevó por delante el cerrojo ajeno"


def test_sin_cerrojo_no_se_toca_NADA_ni_siquiera_el_bootstrap(tmp_path):
    """El cerrojo tiene que proteger desde el PRIMER write, no desde la parte que
    parecía peligrosa.

    Con el token/roster/mounts creados fuera del cerrojo, dos `up` en un checkout
    limpio todavía se pisaban: los dos ven que no hay token, los dos lo generan,
    uno arranca Docker con el suyo y el disco acaba con el del otro — servicio
    vivo con credenciales que ya no coinciden con las del cliente. El cerrojo
    existía y no cubría eso.

    «No conseguí exclusión» tiene que significar «no toqué nada».

    FALSADOR: devolver la toma del cerrojo detrás del bootstrap hace que el token
    y los ficheros aparezcan aunque el cerrojo esté ocupado."""
    import subprocess
    repo, casa, binfalso = tmp_path / "repo", tmp_path / "home", tmp_path / "bin"
    for d in (repo, casa, binfalso):
        d.mkdir()
    for f in ("llmi", "docker-compose.yml", "roster.example.json"):
        shutil.copy(RAIZ / f, repo / f)
    (casa / ".llmi-lifecycle-llminbox.lock").mkdir()        # otro proceso ya está dentro
    reg = tmp_path / "docker-args.txt"
    (binfalso / "docker").write_text(
        f'#!/bin/sh\n{_SIN_CONTENEDOR}echo "$@" >> "{reg}"\nexit 0\n')
    (binfalso / "docker").chmod(0o755)
    r = subprocess.run([str(repo / "llmi"), "up"], cwd=repo,
                       env={"HOME": str(casa),
                            "PATH": f"{binfalso}:/usr/bin:/bin:/usr/sbin:/sbin"},
                       capture_output=True, text=True, timeout=120, check=False)
    assert r.returncode != 0, "entró con el cerrojo ocupado"
    assert not (casa / ".llminbox.token").exists(), "generó token sin exclusión"
    assert not (repo / "roster.json").exists(), "creó el censo sin exclusión"
    assert not (repo / ".llmi-mounts.json").exists(), "creó el mapa sin exclusión"
    assert not (repo / ".llminbox-state").exists(), "creó el estado sin exclusión"
    assert not reg.exists(), "invocó a Docker sin exclusión"
    assert (casa / ".llmi-lifecycle-llminbox.lock").exists(), "se llevó el cerrojo ajeno"


def test_build_comparte_el_cerrojo_con_up(tmp_path):
    """`build` y `up` son operaciones distintas del MISMO ciclo de vida mutable.
    Un `build` que cambia la imagen mientras un `up` decide qué hacer con ella es
    la misma carrera con otro disfraz — y `build` ignoraba el cerrojo.

    FALSADOR: quitarle la toma de cerrojo a `build` deja que invoque a Docker con
    un `up` en marcha."""
    import subprocess
    repo, casa, binfalso = tmp_path / "repo", tmp_path / "home", tmp_path / "bin"
    for d in (repo, casa, binfalso):
        d.mkdir()
    for f in ("llmi", "docker-compose.yml", "roster.example.json"):
        shutil.copy(RAIZ / f, repo / f)
    (casa / ".llmi-lifecycle-llminbox.lock").mkdir()
    reg = tmp_path / "docker-args.txt"
    (binfalso / "docker").write_text(
        f'#!/bin/sh\n{_SIN_CONTENEDOR}echo "$@" >> "{reg}"\nexit 0\n')
    (binfalso / "docker").chmod(0o755)
    r = subprocess.run([str(repo / "llmi"), "build"], cwd=repo,
                       env={"HOME": str(casa),
                            "PATH": f"{binfalso}:/usr/bin:/bin:/usr/sbin:/sbin"},
                       capture_output=True, text=True, timeout=120, check=False)
    assert r.returncode != 0, "construyó con el ciclo de vida ocupado"
    assert not reg.exists(), f"invocó a Docker: {reg.read_text()!r}"


def test_un_up_no_puede_APAGAR_V8_sin_que_se_lo_pidan(tmp_path):
    """El gate de identidad no puede colgar de que el shell de quien arranca tenga
    una variable.

    `docker-compose.yml` hace `LLMINBOX_CREDENCIALES: ${LLMINBOX_CREDENCIALES:+…}`, así
    que un `llmi up` desde un shell SIN esa variable devuelve el bus con
    `v8.configurado=false` — el gate APAGADO, sin un aviso. Verificado con
    `docker compose config`: sin la variable sale `LLMINBOX_CREDENCIALES: ""` y el
    volumen cae a `roster.example.json`.

    Y no es hipotético que otro lo haga por ti: `~/.local/bin/llmi` —el que usa la
    flota— cuelga de OTRO worktree, y su `up` es tan capaz de recrear el contenedor
    como el de este repo.

    ⊕/⊖ de este test: con la credencial en el contenedor y sin ella en el entorno, el
    `up` NO puede llegar a `compose`; con `--sin-v8` sí, porque entonces está DICHO.
    """
    CRED = "LLMINBOX_DB=/data/x\nLLMINBOX_CREDENCIALES=/credenciales/mapa.json"
    r, args = _up_hermetico(tmp_path / "a", A, estado_previo=A, aplicado=_hash(A),
                            env_contenedor=CRED)
    assert r.returncode != 0, f"el up siguió y habría apagado V8:\n{r.stdout}"
    assert "up" not in args.split(), (
        f"llegó a `compose up` pese al gate: {args!r} — el aviso sin la parada no es gate")
    assert "V8" in (r.stdout + r.stderr), "no dice de qué se está quejando"

    # ⊖ EL CONTROL QUE MUERDE: si el gate dijera que no a todo, este ⊖ no lo vería.
    # `--sin-v8` es apagarlo A SABIENDAS, y eso sí tiene que pasar.
    r2, args2 = _up_hermetico(tmp_path / "b", A, estado_previo=A, aplicado=_hash(A),
                              env_contenedor=CRED, extra=("--sin-v8",))
    assert r2.returncode == 0, f"`--sin-v8` no deja arrancar:\n{r2.stdout}\n{r2.stderr}"
    assert "up" in args2.split(), f"con --sin-v8 no llegó a compose: {args2!r}"

    # ⊖ Y sin contenedor vivo no hay nada que proteger: no puede estorbar un arranque
    # en frío, que es el caso de cualquiera que despliegue por primera vez.
    r3, args3 = _up_hermetico(tmp_path / "c", A, estado_previo=A, aplicado=_hash(A))
    assert r3.returncode == 0, f"bloquea un arranque en frío:\n{r3.stdout}\n{r3.stderr}"
    assert "up" in args3.split()
