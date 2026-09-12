"""Falsadores adversariales para el control de despliegue de V8 (#124).

Todos los dobles son herméticos: no hablan con Docker ni con el servicio vivo. El
objetivo no es describir la implementación actual, sino fijar las invariantes de
seguridad que debe cumplir cualquier cura.
"""

import shutil
import subprocess
import time
from pathlib import Path


RAIZ = Path(__file__).resolve().parents[2]


def _sandbox(root: Path, docker: str, *, envfile: str | None = None):
    repo, casa, binario = root / "repo", root / "home", root / "bin"
    for directorio in (repo, casa, binario):
        directorio.mkdir(parents=True, exist_ok=True)
    for nombre in ("llmi", "docker-compose.yml", "roster.example.json"):
        shutil.copy(RAIZ / nombre, repo / nombre)
    shutil.copy(RAIZ / "roster.example.json", repo / "roster.json")
    (repo / ".llmi-mounts.json").write_text("{}")
    (casa / ".llminbox.token").write_text("token-de-prueba")
    if envfile is not None:
        (casa / ".llminbox-env").write_text(envfile)
    doble = binario / "docker"
    doble.write_text("#!/bin/sh\n" + docker)
    doble.chmod(0o755)
    return repo, casa, binario


def _corre(repo, casa, binario, *args, extra_env=None, timeout=20):
    env = {
        "HOME": str(casa),
        "PATH": f"{binario}:/usr/bin:/bin:/usr/sbin:/sbin",
    }
    env.update(extra_env or {})
    return subprocess.run(
        [str(repo / "llmi"), *args], cwd=repo, env=env,
        capture_output=True, text=True, timeout=timeout, check=False,
    )


def test_un_inspect_fallido_no_se_confunde_con_ausencia_de_contenedor(tmp_path):
    """Un error/transitorio de Docker es NO MEDIDO, no «no existe el bus»."""
    registro = tmp_path / "docker.log"
    repo, casa, binario = _sandbox(tmp_path / "x", f'''
[ "$1" = "inspect" ] && exit 1
printf '%s\n' "$*" >> "{registro}"
exit 0
''')
    r = _corre(repo, casa, binario, "up")
    assert r.returncode != 0, "un inspect no medible pasó como arranque en frío"
    assert not registro.exists() or "compose up" not in registro.read_text()


def test_un_mapa_no_vacio_no_basta_para_preservar_la_cobertura(tmp_path):
    """Cambiar un mapa vivo por uno parcial requiere prueba de no regresión.

    El token compartido sigue abriendo la fase 1, de modo que «V8 configurado» no
    implica que la identidad por agente conserve su cobertura.
    """
    registro = tmp_path / "docker.log"
    repo, casa, binario = _sandbox(tmp_path / "x", f'''
printf 'cred=%s args=%s\n' "$LLMINBOX_CREDENCIALES" "$*" >> "{registro}"
[ "$1" = "inspect" ] && {{
  printf '%s\n' 'LLMINBOX_CREDENCIALES=/credenciales/mapa.json'
  exit 0
}}
exit 0
''')
    parcial = tmp_path / "uno-de-muchos.json"
    parcial.write_text('{"token-uno":{"rol":"be","carril":"64bis"}}')
    r = _corre(repo, casa, binario, "up",
               extra_env={"LLMINBOX_CREDENCIALES": str(parcial)})
    assert r.returncode != 0, "aceptó un mapa nuevo sin medir cobertura anterior/nueva"
    assert "compose up" not in registro.read_text()


def test_nombre_persistido_de_segunda_instancia_define_tambien_el_proyecto(tmp_path):
    """La configuración canónica del operador no puede leerse después del pin."""
    registro = tmp_path / "docker.log"
    repo, casa, binario = _sandbox(
        tmp_path / "x",
        f'''printf 'project=%s name=%s args=%s\n' "$COMPOSE_PROJECT_NAME" "$LLMINBOX_NAME" "$*" >> "{registro}"
exit 0
''',
        envfile="LLMINBOX_NAME=segunda-instancia\nLLMINBOX_PORT=9088\n",
    )
    r = _corre(repo, casa, binario, "down")
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "project=segunda-instancia name=segunda-instancia" in registro.read_text()


def test_un_project_name_ambient_no_puede_desacoplarse_del_contenedor(tmp_path):
    """Un override heredado no puede reabrir dos proyectos para un solo nombre."""
    registro = tmp_path / "docker.log"
    repo, casa, binario = _sandbox(tmp_path / "x", f'''
printf 'project=%s name=%s args=%s\n' "$COMPOSE_PROJECT_NAME" "${{LLMINBOX_NAME:-llminbox}}" "$*" >> "{registro}"
exit 0
''')
    r = _corre(repo, casa, binario, "down",
               extra_env={"COMPOSE_PROJECT_NAME": "proyecto-heredado"})
    observado = registro.read_text() if registro.exists() else ""
    assert r.returncode != 0 or "project=llminbox name=llminbox" in observado


def test_un_nombre_invalido_falla_antes_de_invocar_docker(tmp_path):
    """La frontera valida el identificador; no delega el fallo al último paso."""
    registro = tmp_path / "docker.log"
    repo, casa, binario = _sandbox(tmp_path / "x", f'''
printf '%s\n' "$*" >> "{registro}"
exit 0
''')
    r = _corre(repo, casa, binario, "down",
               extra_env={"LLMINBOX_NAME": "Nombre Invalido"})
    assert r.returncode != 0
    assert not registro.exists(), "el nombre inválido llegó a Docker"


def test_sin_v8_fuerza_el_estado_off_aunque_el_envfile_conserve_el_mapa(tmp_path):
    """La orden de rollback hace lo que dice, no sólo omite el precheck."""
    registro = tmp_path / "docker.log"
    mapa = tmp_path / "mapa.json"
    mapa.write_text("{}")
    repo, casa, binario = _sandbox(
        tmp_path / "x",
        f'''printf 'cred=%s args=%s\n' "${{LLMINBOX_CREDENCIALES:-<ausente>}}" "$*" >> "{registro}"
exit 0
''',
        envfile=f"LLMINBOX_CREDENCIALES={mapa}\n",
    )
    r = _corre(repo, casa, binario, "up", "--sin-v8")
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "cred=<ausente> args=compose up" in registro.read_text()


def test_el_cerrojo_es_por_instancia_y_no_por_checkout(tmp_path):
    """Dos checkouts del mismo proyecto no pueden entrar juntos en compose up."""
    entro_a, libera_a = tmp_path / "entro-a", tmp_path / "libera-a"
    registro = tmp_path / "docker.log"
    doble = f'''
if [ "$1" = "inspect" ]; then printf '%s\n' 'LLMINBOX_DB=/data/x'; exit 0; fi
case "$*" in
  *"compose up"*)
    printf '%s\n' "$RUNNER" >> "{registro}"
    if [ "$RUNNER" = A ]; then
      : > "{entro_a}"
      while [ ! -f "{libera_a}" ]; do sleep 0.02; done
    fi ;;
esac
exit 0
'''
    repo_a, casa, binario = _sandbox(tmp_path / "a", doble)
    repo_b, _, _ = _sandbox(tmp_path / "b", doble)
    env_a = {
        "HOME": str(casa),
        "PATH": f"{binario}:/usr/bin:/bin:/usr/sbin:/sbin",
        "RUNNER": "A",
    }
    pa = subprocess.Popen(
        [str(repo_a / "llmi"), "up"], cwd=repo_a, env=env_a,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        # Espera por condición con plazo real. Las 200 pausas de 10 ms suponían que
        # arrancar bash + varios python cabía siempre en ~2 s; bajo carga vencía sin
        # que el cerrojo hubiera fallado. Además el assert estaba fuera del finally:
        # dejaba A vivo y perdía justo stdout/stderr que permitían diagnosticarlo.
        limite = time.monotonic() + 10
        while not entro_a.exists() and pa.poll() is None and time.monotonic() < limite:
            time.sleep(0.01)
        if not entro_a.exists():
            libera_a.touch()
            salida_a, error_a = pa.communicate(timeout=10)
            raise AssertionError(
                "A no alcanzó compose up: "
                f"pid={pa.pid} rc={pa.returncode} "
                f"stdout={salida_a!r} stderr={error_a!r}")
        rb = _corre(repo_b, casa, binario, "up", extra_env={"RUNNER": "B"}, timeout=5)
        assert rb.returncode != 0, "B entró mientras A poseía el ciclo de vida"
        assert registro.read_text().splitlines() == ["A"]
    finally:
        libera_a.touch()
        if pa.poll() is None:
            pa.communicate(timeout=10)


def test_dos_prechecks_validos_no_pueden_acabar_con_v8_apagado(tmp_path):
    """El precheck y la mutación forman una sola sección crítica."""
    estado = tmp_path / "v8"
    a_termino, b_midio = tmp_path / "a-termino", tmp_path / "b-midio"
    estado.write_text("off")
    doble = f'''
if [ "$1" = "inspect" ]; then
  [ "$RUNNER" = B ] && : > "{b_midio}"
  if [ "$(cat "{estado}")" = on ]; then
    printf '%s\n' 'LLMINBOX_CREDENCIALES=/credenciales/mapa.json'
  else
    printf '%s\n' 'LLMINBOX_DB=/data/x'
  fi
  exit 0
fi
case "$*" in
  *"compose up"*)
    if [ "$RUNNER" = A ]; then
      printf on > "{estado}"; : > "{a_termino}"
    else
      n=0
      while [ ! -f "{a_termino}" ] && [ "$n" -lt 50 ]; do
        sleep 0.02; n=$((n + 1))
      done
      printf off > "{estado}"
    fi ;;
esac
exit 0
'''
    repo_a, casa, binario = _sandbox(tmp_path / "a", doble)
    repo_b, _, _ = _sandbox(tmp_path / "b", doble)
    mapa = tmp_path / "mapa.json"
    mapa.write_text('{"token":{"rol":"be","carril":"64bis"}}')
    env_b = {
        "HOME": str(casa),
        "PATH": f"{binario}:/usr/bin:/bin:/usr/sbin:/sbin",
        "RUNNER": "B",
    }
    pb = subprocess.Popen(
        [str(repo_b / "llmi"), "up"], cwd=repo_b, env=env_b,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    for _ in range(200):
        if b_midio.exists():
            break
        time.sleep(0.01)
    assert b_midio.exists(), "B no completó el precheck inicial"
    ra = _corre(repo_a, casa, binario, "up", extra_env={
        "RUNNER": "A", "LLMINBOX_CREDENCIALES": str(mapa),
    })
    salida_b, error_b = pb.communicate(timeout=20)
    ambos_pasaron_y_el_gate_cayo = (
        ra.returncode == 0 and pb.returncode == 0 and estado.read_text() == "off"
    )
    assert not ambos_pasaron_y_el_gate_cayo, (
        "los dos despliegues declararon éxito y el último apagó V8",
        ra.stdout, ra.stderr, salida_b, error_b,
    )
