"""Un contenedor que NUNCA arrancó no es «V8 encendido», y `llmi logs` no es del vecino.

Los dos defectos se midieron el 2026-09-05 en un banco aislado (`llminbox-val2`,
puerto 8094, proyecto propio), sin tocar el contenedor de la flota:

① `_estado_v8()` clasificaba por `Config.Env` y **nunca miraba `.State`**. Docker
   hornea el entorno al CREAR el contenedor, así que un `up` que murió haciendo `bind`
   del puerto dejaba un husk `Status=created StartedAt=0001-01-01T00:00:00Z` que
   puntuaba `on`. Ese `on` bloqueaba el reintento (`rc=1`) exigiendo
   `--cobertura-verificada` — un flag que afirma «ya comparé contra el mapa VIVO»
   cuando no hay ninguno vivo. Y los dos remedios que el mensaje imprime tampoco
   corren en ese estado: `llmi credenciales` → `rc=3` («container is not running»),
   `curl $API/health` → `rc=28`. La única salida era firmar en vacío.

   Por qué importa más allá de la fricción: un flag que se aprende a pasar sin
   contenido deja de proteger el día que SÍ hay cobertura viva que perder.

② `llmi:1668` era `docker logs --tail "${1:-40}" llminbox` — literal. Única línea del
   ciclo de vida que no respetaba `LLMINBOX_NAME`: `llmi logs` desde una instancia
   aislada leía el contenedor de la flota.

Cada cura viene con su control POSITIVO al lado: una guarda que deja pasar todo no
distingue nada, y una que bloquea todo tampoco.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]

ROSTER = ('{"agentes": [{"nombre": "backend", "humano": "a", "clave": "", "rol": "be"}],'
          ' "humanos": [], "difusion": []}')

# `docker` de mentira que HABLA COMO DOCKER: `inspect` o falla diciendo por qué, o
# imprime. El payload de estado se sirve SÓLO cuando el formato lo pide (lleva el
# marcador); cualquier otro `inspect` —el del `.Id` para el atestado— devuelve un id.
DOBLE = r"""#!/bin/sh
[ "$1" = "ps" ] && exit 0
if [ "$1" = "inspect" ]; then
  case "$*" in
    *@@ESTADO@@*) __PAYLOAD__ ;;
    *) echo "deadbeefcafe0123"; exit 0 ;;
  esac
fi
echo "$@" >> "__ARGS__"
exit 0
"""


def _banco(tmp_path, payload_inspect):
    """Repo hermético + `docker` falso. Devuelve (repo, casa, binfalso, args)."""
    repo, casa, binfalso = tmp_path / "repo", tmp_path / "home", tmp_path / "bin"
    for d in (repo, casa, binfalso):
        d.mkdir(parents=True, exist_ok=True)
    for f in ("llmi", "docker-compose.yml", "roster.example.json"):
        shutil.copy(RAIZ / f, repo / f)
    (repo / "roster.json").write_text(ROSTER)
    (repo / ".llmi-mounts.json").write_text("{}")
    (casa / ".llminbox.token").write_text("token-de-prueba")
    (casa / "mapa.json").write_text('{"cred-de-prueba-xxxxxxxxxxxx": {"rol": "be", "carril": "val"}}')
    args = tmp_path / "docker-args.txt"
    (binfalso / "docker").write_text(
        DOBLE.replace("__PAYLOAD__", payload_inspect).replace("__ARGS__", str(args)))
    (binfalso / "docker").chmod(0o755)
    return repo, casa, binfalso, args


def _up(tmp_path, payload_inspect, extra=()):
    repo, casa, binfalso, args = _banco(tmp_path, payload_inspect)
    r = subprocess.run([str(repo / "llmi"), "up", *extra], cwd=repo,
                       env={"HOME": str(casa), "LLMINBOX_NAME": "llminbox-banco",
                            "LLMINBOX_CREDENCIALES": str(casa / "mapa.json"),
                            "PATH": f"{binfalso}:/usr/bin:/bin:/usr/sbin:/sbin"},
                       capture_output=True, text=True, timeout=120, check=False)
    return r, (args.read_text() if args.exists() else "")


def _payload(status, desde, con_v8=True, con_marcador=True):
    """El cuerpo lleva SALTOS DE LÍNEA REALES, no `\\n` literales.

    Lo cazó la revisión independiente: con `printf %s` entre comillas simples, un
    `\\n` escrito en el guion sale como los dos caracteres barra-ene, `sed -n 2p` no
    encuentra segunda línea y el doble clasificaba `on` en TODOS los casos. La prueba
    del husk pasaba a rojo por culpa del instrumento, no de la cura — y las otras
    siete pasaban en verde por el mismo motivo: no medían nada del camino nuevo.
    """
    env = "LLMINBOX_CREDENCIALES=/credenciales/mapa.json\n" if con_v8 else "LLMINBOX_DB=/data/x\n"
    if not con_marcador:                      # doble ANTIGUO: sólo sabe emitir entorno
        return f"printf %s '{env}'; exit 0"
    return f"printf %s '{env}@@ESTADO@@\n{status}\n{desde}\n'; exit 0"


# ── ① EL HUSK ────────────────────────────────────────────────────────────────────

def test_husk_created_que_nunca_arranco_NO_bloquea_el_reintento(tmp_path):
    """LA CURA. `created` + `StartedAt` en el cero de Go ⇒ no hay cobertura viva."""
    r, argumentos = _up(tmp_path, _payload("created", "0001-01-01T00:00:00Z"))
    assert "cobertura-verificada" not in (r.stdout + r.stderr), (
        "el husk sigue bloqueando el reintento y pidiendo un flag que afirma una "
        f"comparación imposible:\n{r.stderr}")
    assert "NUNCA llegó a arrancar" in r.stderr, (
        f"proceder sin decirlo parece un descuido; falta la nota:\n{r.stderr}")
    assert "compose" in argumentos, (
        f"no llegó a desplegar: se paró antes de compose\n{r.stdout}\n{r.stderr}")


def test_contenedor_EN_MARCHA_sigue_bloqueando(tmp_path):
    """CONTROL POSITIVO. Si dejara pasar esto también, la cura no distinguiría nada."""
    r, argumentos = _up(tmp_path, _payload("running", "2026-09-05T01:00:00Z"))
    assert r.returncode != 0, f"un contenedor VIVO dejó de gatear la cobertura:\n{r.stdout}"
    assert "cobertura-verificada" in r.stderr, f"bloqueó por otro motivo:\n{r.stderr}"
    assert "compose" not in argumentos, "llegó a desplegar pese a bloquear"


def test_parado_pero_que_SI_arranco_sigue_bloqueando(tmp_path):
    """CONTROL POSITIVO, el caso fino: `exited` con fecha real SÍ pudo servir identidad."""
    r, argumentos = _up(tmp_path, _payload("exited", "2026-09-04T21:07:26.376546917Z"))
    assert r.returncode != 0, (
        "un contenedor parado que SÍ llegó a correr perdió su gate: la cura se pasó "
        f"de ancha y ya no distingue «nunca arrancó» de «está parado»:\n{r.stdout}")
    assert "cobertura-verificada" in r.stderr, f"bloqueó por otro motivo:\n{r.stderr}"


def test_created_con_fecha_REAL_no_se_considera_husk(tmp_path):
    """CONTROL POSITIVO. Se exigen LAS DOS señales, no sólo `created`."""
    r, _ = _up(tmp_path, _payload("created", "2026-09-04T21:07:26Z"))
    assert r.returncode != 0, (
        "bastó con `Status=created` para saltarse el gate: una sola señal es "
        f"falsificable por cualquiera que pare un contenedor\n{r.stdout}")


def test_inspect_ambiguo_sigue_siendo_FAIL_CLOSED(tmp_path):
    """CONTROL POSITIVO. «No lo sé» no puede haberse vuelto «no arrancó»."""
    r, argumentos = _up(tmp_path, 'echo "Error: daemon ocupado" >&2; exit 1')
    assert r.returncode != 0, "un inspect ambiguo dejó de parar el despliegue"
    assert "NO MEDIDO" in r.stderr, f"perdió el camino de nomedido:\n{r.stderr}"
    assert "compose" not in argumentos, "llegó a Docker con el estado sin medir"


def test_un_doble_SIN_marcador_se_clasifica_como_siempre(tmp_path):
    """CONTROL POSITIVO de compatibilidad. Sin marcador no se inventa un estado: se
    cae al camino de siempre. Si la lectura fuera POSICIONAL, las dos primeras
    variables de entorno pasarían por `Status` y `StartedAt` y el gate se movería
    sin que nadie lo pidiera."""
    r, _ = _up(tmp_path, _payload("", "", con_marcador=False))
    assert r.returncode != 0, (
        "un doble que sólo emite entorno dejó de puntuar `on`: la lectura se volvió "
        f"posicional y cambió el gate para todos los tests que usan ese doble\n{r.stdout}")
    assert "cobertura-verificada" in r.stderr


# ── ② `llmi logs` ────────────────────────────────────────────────────────────────

def _lineas_logs(argumentos):
    """Las invocaciones `docker logs`. Existe porque el bucle `for … if startswith`
    que tenía antes pasaba en VERDE cuando no había ninguna línea: la aserción no se
    llegaba a evaluar. Un test que no puede fallar por ausencia no prueba presencia.
    """
    return [l for l in argumentos.splitlines() if l.split()[:1] == ["logs"]]


def _logs(tmp_path, nombre=None):
    repo, casa, binfalso, args = _banco(tmp_path, 'echo x; exit 0')
    entorno = {"HOME": str(casa), "PATH": f"{binfalso}:/usr/bin:/bin:/usr/sbin:/sbin"}
    if nombre:
        entorno["LLMINBOX_NAME"] = nombre
    subprocess.run([str(repo / "llmi"), "logs"], cwd=repo, env=entorno,
                   capture_output=True, text=True, timeout=60, check=False)
    return args.read_text() if args.exists() else ""


def test_llmi_logs_lee_ESTA_instancia_y_no_la_de_la_flota(tmp_path):
    """LA CURA. Era la única línea del ciclo de vida con el nombre en literal."""
    lineas = _lineas_logs(_logs(tmp_path, nombre="llminbox-banco"))
    assert len(lineas) == 1, (
        f"esperaba EXACTAMENTE una invocación `docker logs`, hubo {len(lineas)}: {lineas!r}")
    assert lineas[0].split()[-1] == "llminbox-banco", (
        f"`llmi logs` sigue leyendo el contenedor de la flota: {lineas[0]!r}")


def test_llmi_logs_sin_LLMINBOX_NAME_sigue_leyendo_llminbox(tmp_path):
    """CONTROL POSITIVO. El defecto se cura sin cambiarle el defecto a nadie: quien no
    declara instancia sigue viendo la de siempre."""
    lineas = _lineas_logs(_logs(tmp_path, nombre=None))
    assert len(lineas) == 1, (
        f"esperaba EXACTAMENTE una invocación `docker logs`, hubo {len(lineas)}: {lineas!r}")
    assert lineas[0].split()[-1] == "llminbox", (
        f"cambió el comportamiento por defecto: {lineas[0]!r}")
