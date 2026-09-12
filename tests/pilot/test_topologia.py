"""Pruebas ESTÁTICAS de la topología del piloto M1 — con su mutante por invariante.

La forma de este fichero es deliberada y responde a la cicatriz del carril: un
`⊕` sobre el compose real es un verde que NO discrimina —pasaría igual con el
verificador desconectado— así que cada invariante trae su MUTANTE: se rompe esa
propiedad en una COPIA del compose y se exige que el verificador la cace. Si un
mutante sale verde, la fila que dice proteger no la protege nadie.

Y hay un control de que el instrumento no está mudo (`test_verificador_no_es_mudo`):
sobre un compose vacío tiene que salir rojo. Sin él, un verificador que devolviera
siempre `[]` aprobaría el compose real Y todos los mutantes... no: los mutantes lo
cazarían. Al revés: uno que devolviera SIEMPRE un fallo pasaría todos los mutantes
y sólo lo caza el `⊕` del compose real. Los dos controles se necesitan.
"""
from __future__ import annotations

import copy
import pathlib
import re
import sys

import pytest
import yaml

RAIZ = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ / "tools"))

import pilot_topologia  # noqa: E402

COMPOSE = RAIZ / "docker-compose.pilot.yml"


@pytest.fixture()
def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def codigos(fallos: list[str]) -> set[str]:
    return {f.split(":", 1)[0] for f in fallos}


# ── ⊕ EL CONTROL POSITIVO ───────────────────────────────────────────────────────

def test_el_compose_del_piloto_cumple_la_topologia(compose):
    fallos = pilot_topologia.verificar(compose, ledger_piloto="/ledgers/llminbox")
    assert fallos == [], "\n".join(fallos)


def test_verificador_no_es_mudo():
    """⊖ del INSTRUMENTO: sobre un compose vacío tiene que haber rojo."""
    assert pilot_topologia.verificar({"services": {}}) != []


# ── ⊖ UN MUTANTE POR INVARIANTE ─────────────────────────────────────────────────

def test_I1_sin_volumen_de_journal(compose):
    m = copy.deepcopy(compose)
    m["services"]["gateway"]["volumes"] = [
        v for v in m["services"]["gateway"]["volumes"] if "/journal" not in v]
    assert "I1" in codigos(pilot_topologia.verificar(m))


def test_I1_journal_como_bind_no_como_volumen_nombrado(compose):
    m = copy.deepcopy(compose)
    m["services"]["gateway"]["volumes"] = [
        "/tmp/journal-del-host:/journal" if "/journal" in v else v
        for v in m["services"]["gateway"]["volumes"]]
    assert "I1" in codigos(pilot_topologia.verificar(m))


def test_I2_journal_e_indice_en_el_mismo_volumen(compose):
    """El mutante que reproduce el riesgo que abre esta entrega."""
    m = copy.deepcopy(compose)
    m["services"]["gateway"]["volumes"] = [
        "llminbox-pilot-index:/journal" if v.endswith(":/journal") else v
        for v in m["services"]["gateway"]["volumes"]]
    assert "I2" in codigos(pilot_topologia.verificar(m))


def test_I3_journal_en_solo_lectura(compose):
    m = copy.deepcopy(compose)
    m["services"]["gateway"]["volumes"] = [
        v + ":ro" if v.endswith(":/journal") else v
        for v in m["services"]["gateway"]["volumes"]]
    assert "I3" in codigos(pilot_topologia.verificar(m))


def test_I3_el_agente_monta_el_journal(compose):
    m = copy.deepcopy(compose)
    m["services"]["agente"]["volumes"] = ["llminbox-pilot-journal:/journal"]
    assert "I3" in codigos(pilot_topologia.verificar(m))


def test_I4_pepper_con_valor_literal_en_el_yaml(compose):
    m = copy.deepcopy(compose)
    m["services"]["gateway"]["environment"]["LLMINBOX_PEPPER"] = "a" * 40
    assert "I4" in codigos(pilot_topologia.verificar(m))


def test_I4_sin_fichero_de_pepper(compose):
    m = copy.deepcopy(compose)
    del m["services"]["gateway"]["environment"]["LLMINBOX_PEPPER_FILE"]
    assert "I4" in codigos(pilot_topologia.verificar(m))


def test_I4_la_indireccion_no_cuenta_como_valor(compose):
    """⊕ del propio detector: `${VAR}` NO puede leerse como secreto en claro."""
    m = copy.deepcopy(compose)
    m["services"]["gateway"]["environment"]["LLMINBOX_TOKEN"] = "${LLMINBOX_TOKEN:?x}"
    assert "I4" not in codigos(pilot_topologia.verificar(m))


def test_I5_mapa_sin_atestado(compose):
    m = copy.deepcopy(compose)
    del m["services"]["gateway"]["environment"]["LLMINBOX_CREDENCIALES_SHA"]
    assert "I5" in codigos(pilot_topologia.verificar(m))


def test_I5_sin_roster_montado_el_censo_seria_vacio(compose):
    m = copy.deepcopy(compose)
    m["services"]["gateway"]["volumes"] = [
        v for v in m["services"]["gateway"]["volumes"]
        if "/state/roster.json" not in v]
    assert "I5" in codigos(pilot_topologia.verificar(m))


def test_I5_roster_es_unico_y_solo_lectura(compose):
    m = copy.deepcopy(compose)
    m["services"]["gateway"]["volumes"] = [
        v.replace("/state/roster.json:ro", "/state/roster.json")
        for v in m["services"]["gateway"]["volumes"]]
    assert "I5" in codigos(pilot_topologia.verificar(m))


def test_I5_mapa_montado_en_escritura(compose):
    m = copy.deepcopy(compose)
    m["services"]["gateway"]["volumes"] = [
        v.replace("/credenciales/mapa.json:ro", "/credenciales/mapa.json")
        for v in m["services"]["gateway"]["volumes"]]
    assert "I5" in codigos(pilot_topologia.verificar(m))


def test_I6_dos_ledgers_escribibles(compose):
    m = copy.deepcopy(compose)
    m["services"]["gateway"]["volumes"] = [
        v.replace("/ledgers/testigo:ro", "/ledgers/testigo")
        for v in m["services"]["gateway"]["volumes"]]
    assert "I6" in codigos(pilot_topologia.verificar(m))


def test_I6_sin_segundo_ledger_no_hay_control_negativo(compose):
    m = copy.deepcopy(compose)
    m["services"]["gateway"]["volumes"] = [
        v for v in m["services"]["gateway"]["volumes"] if "/ledgers/testigo" not in v]
    assert "I6" in codigos(pilot_topologia.verificar(m))


def test_I7_el_agente_monta_un_ledger(compose):
    m = copy.deepcopy(compose)
    # Ruta NEUTRA a propósito: antes había rutas absolutas de máquinas concretas
    # metidas en el arnés — el auditor pidió sacarlas. Lo que el test
    # mide es que el AGENTE no monte un ledger, no de qué host viene.
    m["services"]["agente"]["volumes"] = ["${LEDGER_HOST}:/ledgers/llminbox:ro"]
    assert "I7" in codigos(pilot_topologia.verificar(m))


def test_I7_el_agente_monta_el_socket_de_docker(compose):
    m = copy.deepcopy(compose)
    m["services"]["agente"]["volumes"] = ["/var/run/docker.sock:/var/run/docker.sock"]
    assert "I7" in codigos(pilot_topologia.verificar(m))


def test_I7_el_agente_monta_el_home(compose):
    m = copy.deepcopy(compose)
    m["services"]["agente"]["volumes"] = ["${HOME}:/tmp/home-montado:ro"]
    assert "I7" in codigos(pilot_topologia.verificar(m))


def test_I7_el_agente_privilegiado(compose):
    m = copy.deepcopy(compose)
    m["services"]["agente"]["privileged"] = True
    assert "I7" in codigos(pilot_topologia.verificar(m))


def test_I7_la_sintaxis_LARGA_no_evade_el_verificador(compose):
    """Si el normalizador sólo entendiera la sintaxis corta, reindentar el montaje
    lo esquivaría en silencio. Este mutante usa la LARGA a propósito."""
    m = copy.deepcopy(compose)
    m["services"]["agente"]["volumes"] = [
        {"type": "bind", "source": "/var/run/docker.sock",
         "target": "/var/run/docker.sock"}]
    assert "I7" in codigos(pilot_topologia.verificar(m))


def test_I8_healthcheck_que_solo_mira_el_http(compose):
    m = copy.deepcopy(compose)
    m["services"]["gateway"]["healthcheck"]["test"] = [
        "CMD", "curl", "-sf", "http://127.0.0.1:8077/health"]
    assert "I8" in codigos(pilot_topologia.verificar(m))


def test_I8_sin_healthcheck(compose):
    m = copy.deepcopy(compose)
    del m["services"]["gateway"]["healthcheck"]
    assert "I8" in codigos(pilot_topologia.verificar(m))


def test_I9_puerto_fuera_de_loopback(compose):
    m = copy.deepcopy(compose)
    m["services"]["gateway"]["ports"] = ["8099:8077"]
    assert "I9" in codigos(pilot_topologia.verificar(m))


def test_I9_el_agente_publica_puerto(compose):
    m = copy.deepcopy(compose)
    m["services"]["agente"]["ports"] = ["127.0.0.1:9000:9000"]
    assert "I9" in codigos(pilot_topologia.verificar(m))


# ── PROPIEDADES QUE SE LEEN DEL FICHERO, NO DEL VERIFICADOR ─────────────────────

def test_el_agente_no_declara_ni_un_montaje(compose):
    """No es «todos en ro»: es que NO HAY. Se asierta sobre el fichero para que el
    día que alguien añada uno, falle aquí y no sólo en la lente del verificador."""
    assert "volumes" not in compose["services"]["agente"]


def test_el_gateway_no_se_reinicia_solo(compose):
    """Un fail-closed con reinicio automático es un crashloop, y este repo ya pagó
    once reinicios por una precondición mal puesta."""
    assert compose["services"]["gateway"]["restart"] == "no"


def _mezclas_de_piloto(raiz):
    """Detecta cambios que mezclan piloto y flota, incluidos los pendientes.

    Un commit sin padres es el snapshot inicial, no un cambio de despliegue.
    Se exige historia completa para no confundir un límite shallow con una raíz.
    """
    import subprocess

    def git(*a):
        return subprocess.run(["git", "-C", str(raiz), *a], capture_output=True,
                              text=True)

    shallow = git("rev-parse", "--is-shallow-repository")
    assert shallow.returncode == 0, shallow.stderr
    assert shallow.stdout.strip() == "false", "el gate exige historia completa"
    prohibidos = {"Dockerfile", "docker-compose.yml"}
    log = git("log", "--full-history", "--format=%H", "--", "docker-compose.pilot*.yml")
    assert log.returncode == 0, f"git log falló: {log.stderr!r}"
    lotes: list[tuple[str, set[str]]] = []
    for commit in log.stdout.splitlines():
        padres = git("show", "-s", "--format=%P", commit)
        assert padres.returncode == 0, padres.stderr
        if not padres.stdout.strip():
            continue
        shown = git("diff-tree", "-m", "--no-commit-id",
                    "--name-only", "-r", commit)
        assert shown.returncode == 0, (
            f"git diff-tree {commit[:8]} falló: {shown.stderr!r}")
        lotes.append((commit[:12], set(shown.stdout.splitlines())))

    tracked = git("diff", "--name-only", "HEAD", "--")
    untracked = git("ls-files", "--others", "--exclude-standard")
    assert tracked.returncode == untracked.returncode == 0
    pendientes = set(tracked.stdout.splitlines()) | set(untracked.stdout.splitlines())
    if any(pathlib.PurePosixPath(p).match("docker-compose.pilot*.yml")
           for p in pendientes):
        lotes.append(("WORKTREE", pendientes))

    return {lote: sorted(prohibidos & ficheros)
            for lote, ficheros in lotes if prohibidos & ficheros}


def test_el_pilot_no_toca_el_compose_de_la_flota():
    mezclas = _mezclas_de_piloto(RAIZ)
    assert mezclas == {}, (
        "un mismo lote mezcla manifests piloto con despliegue global: "
        f"{mezclas}")


@pytest.fixture
def historia_piloto(tmp_path):
    """Historia sintética con snapshot inicial completo, sin ancestros privados."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True,
            capture_output=True, text=True).stdout.strip()

    git("init", "-q")
    git("config", "user.name", "Pilot fixture")
    git("config", "user.email", "pilot@example.invalid")
    for name in ("Dockerfile", "docker-compose.yml", "docker-compose.pilot.yml"):
        (repo / name).write_text("initial\n")
    git("add", ".")
    git("-c", "commit.gpgsign=false", "commit", "-qm", "Initial snapshot")
    return repo, git


def test_el_snapshot_inicial_no_oculta_cambios_mixtos_posteriores(historia_piloto):
    repo, git = historia_piloto
    assert _mezclas_de_piloto(repo) == {}
    # Los cambios separados son válidos, aunque existan ambos en la historia.
    for name in ("docker-compose.pilot.yml", "Dockerfile"):
        (repo / name).write_text("separate change\n")
        git("add", name)
        git("-c", "commit.gpgsign=false", "commit", "-qm", "Separate change")
        assert _mezclas_de_piloto(repo) == {}

    (repo / "docker-compose.pilot.yml").write_text("mixed change\n")
    (repo / "docker-compose.yml").write_text("mixed change\n")
    assert _mezclas_de_piloto(repo) == {"WORKTREE": ["docker-compose.yml"]}
    git("add", ".")
    git("-c", "commit.gpgsign=false", "commit", "-qm", "Mixed change")
    assert _mezclas_de_piloto(repo) == {
        git("rev-parse", "--short=12", "HEAD"): ["docker-compose.yml"]}


def test_el_limite_shallow_no_se_confunde_con_snapshot_inicial(historia_piloto, tmp_path):
    repo, git = historia_piloto
    (repo / "docker-compose.pilot.yml").write_text("changed\n")
    git("add", ".")
    git("-c", "commit.gpgsign=false", "commit", "-qm", "Later change")
    shallow = tmp_path / "shallow"
    git("clone", "-q", "--depth=1", repo.as_uri(), str(shallow))
    with pytest.raises(AssertionError, match="historia completa"):
        _mezclas_de_piloto(shallow)

def test_el_separador_de_montajes_no_se_rompe_dentro_de_una_variable():
    """REGRESIÓN, y la cazó el control positivo de este mismo fichero.

    Con `split(":")` pelado, `${VAR:?mensaje}:/destino` se parte por el `:?` y el
    destino sale `?mensaje}`. El verificador entonces no encuentra el montaje y da
    VERDE POR NO VERLO — que es el modo de fallo que este arnés existe para cazar.
    """
    partes = pilot_topologia._partir_montaje("${A:?falta A}:/ledgers/x:ro")
    assert partes == ["${A:?falta A}", "/ledgers/x", "ro"]
    m = pilot_topologia._normaliza_montajes(
        {"volumes": ["${A:?falta A}:/ledgers/x:ro"]})
    assert m[0]["destino"] == "/ledgers/x" and m[0]["ro"] is True


# ══════════════════════════════════════════════════════════════════════════════
# CORRECTIVA sobre los P1 de @security (`19:56:36Z`, MARK:security-auditoria-2911b13)
# ══════════════════════════════════════════════════════════════════════════════

def test_P1_3_el_agente_que_monta_el_pepper_y_el_mapa_es_ROJO(compose):
    """A1 de @security: pasaba. `I7` era una lista enumerada —ledger, socket, HOME,
    privileged— y el pepper no estaba en ella. Una política en prosa («no tiene
    volumes») aplicada por enumeración deja fuera todo lo que nadie enumeró."""
    m = copy.deepcopy(compose)
    m["services"]["agente"]["volumes"] = [
        "/tmp/pilot.pepper:/run/pepper/journal.pepper:ro",
        "/tmp/pilot-mapa.json:/credenciales/mapa.json:ro"]
    assert "I7" in codigos(pilot_topologia.verificar(m))


def test_P1_3_el_agente_que_monta_el_volumen_del_indice_es_ROJO(compose):
    """A2 de @security: pasaba."""
    m = copy.deepcopy(compose)
    m["services"]["agente"]["volumes"] = ["llminbox-pilot-index:/data"]
    assert "I7" in codigos(pilot_topologia.verificar(m))


def test_P1_3_el_agente_con_secrets_declarados_es_ROJO(compose):
    m = copy.deepcopy(compose)
    m["services"]["agente"]["secrets"] = ["pepper"]
    assert "I7" in codigos(pilot_topologia.verificar(m))


def test_P1_3_el_agente_con_una_lista_de_volumenes_VACIA_pasa(compose):
    """⊕ del propio detector: `volumes: []` es cero montajes, no un montaje."""
    m = copy.deepcopy(compose)
    m["services"]["agente"]["volumes"] = []
    assert "I7" not in codigos(pilot_topologia.verificar(m))


def test_P1_4_el_gateway_NO_hereda_el_token_de_la_flota(compose):
    """El portador del token compartido dispara el fail-open de `exige_ser`
    (`servicio.py:3494-3496`): sin identidad, anota y DEJA PASAR. Meter en el
    piloto la credencial que hace anónimas las escrituras es meter dentro justo
    lo que el piloto existe para prohibir."""
    m = copy.deepcopy(compose)
    m["services"]["gateway"]["environment"]["LLMINBOX_TOKEN"] = "${LLMINBOX_TOKEN:?x}"
    assert "I10" in codigos(pilot_topologia.verificar(m))


def test_P1_4_el_token_propio_del_piloto_pasa(compose):
    assert "I10" not in codigos(
        pilot_topologia.verificar(compose, ledger_piloto="/ledgers/llminbox"))


def test_I11_el_compose_pasa_las_cuatro_perillas_de_testigo(compose):
    """Una perilla que el preflight lee y el compose no pasa es inerte: se puede
    poner, el arranque no protesta y no hace nada. Este repo tiene un guarda para
    eso en su otro compose; el del piloto necesita el suyo."""
    m = copy.deepcopy(compose)
    del m["services"]["gateway"]["environment"]["LLMINBOX_LEDGER_PILOTO_ID"]
    assert "I11" in codigos(pilot_topologia.verificar(m))


def test_P1_4_FRONTERA_el_token_legacy_no_autoriza_ninguna_ruta_native():
    """La frontera declarada, con el falsador que la mantiene viva.

    Hoy la afirmación «el token legacy del piloto no autoriza ninguna ruta
    nativa» es cierta POR AUSENCIA: no hay rutas nativas en `servicio.py`. Eso no
    es una garantía, es un estado — y el día que `P4` cablee `/events` o
    `/sessions`, la frase se vuelve falsa sin que nadie toque este fichero.
    Esta prueba se pone ROJA ese día y obliga a decidir en vez de heredar.

    ⊕ del instrumento: la misma aguja encuentra las rutas que SÍ existen.
    """
    import re
    fuente = (RAIZ / "servicio.py").read_text(encoding="utf-8")
    nativas = re.findall(
        r'@app\.(?:get|post)\("/(sessions|whoami|events|receipts|leases|commands)',
        fuente)
    existentes = re.findall(r'@app\.(?:get|post)\("/', fuente)
    assert len(existentes) > 20, "la aguja no ve ni las rutas que sí están"
    assert nativas == [], (
        f"han aparecido rutas nativas {sorted(set(nativas))}: el token legacy del "
        f"piloto ya NO puede quedarse sin decisión. Ver docs/PILOT-M1-TOPOLOGY.md "
        f"§frontera del token")


# ══════════════════════════════════════════════════════════════════════════════
# 2ª CORRECTIVA · NO-GO de @sdet (`20:09:32Z`, MARK:sdet-reanclo-mi-auditoria-a-0f7d024)
# «Un verificador estático fuera de CI no verifica: documenta.»
# ══════════════════════════════════════════════════════════════════════════════

CI = RAIZ / ".github" / "workflows" / "ci.yml"


def _pasos_run(job: str = "pilot-m1") -> list[str]:
    """Los `run:` REALES del job, por estructura.

    🩸 Esto nació de un mutante que SOBREVIVIÓ: la primera versión hacía
    `"tests/pilot" in ci.read_text()`, así que cuando cambié el `run:` por un
    `echo`, la aguja siguió casando **con el comentario y con el `name:` del paso**
    y las 92 pruebas salieron verdes. Un `grep` sobre el fichero entero no
    distingue «el CI lo ejecuta» de «el CI lo menciona» — y en este repo la
    diferencia entre existir y ejecutarse es justo el hallazgo que estamos
    cerrando."""
    import yaml as _y
    ci = _y.safe_load(CI.read_text(encoding="utf-8"))
    return [str(p.get("run", "")) for p in ci["jobs"][job]["steps"]]


def test_SDET_1_el_CI_ejecuta_tests_pilot():
    """① de @sdet. El job corre `tests/pytest`, no `tests/`: `tests/pilot` quedaba
    fuera POR CONSTRUCCIÓN —un prefijo que no lo alcanza—, no por una línea
    olvidada. `69 passed` era un número que sólo existía en la consola de quien lo
    corría a mano."""
    corridas = _pasos_run()
    assert any("pytest" in r and "tests/pilot" in r for r in corridas), (
        f"ningún `run:` del job ejecuta tests/pilot: {corridas}")


def test_SDET_1_requirements_test_trae_PyYAML():
    """`1` y `2` de @sdet son un AND: con el job y sin `pyyaml`, el CI se pone rojo
    por COLECCIÓN (`1 error during collection`), no verde. Reproducido por él con el
    entorno de CI."""
    req = (RAIZ / "requirements-test.txt").read_text(encoding="utf-8")
    assert re.search(r"(?im)^pyyaml==", req), "falta el pin de PyYAML"


def test_SDET_1_el_pin_de_PyYAML_es_compatible_con_lo_que_se_usa():
    """Un pin que no case con la versión con la que se escribió el arnés es peor que
    no tenerlo: el CI instalaría otra cosa y el verde sería de otro programa."""
    import yaml
    req = (RAIZ / "requirements-test.txt").read_text(encoding="utf-8")
    m = re.search(r"(?im)^pyyaml==(\d+)\.", req)
    assert m, "sin pin no hay nada que comparar"
    assert m.group(1) == yaml.__version__.split(".")[0], (
        f"el pin es {m.group(1)}.x y aquí corre {yaml.__version__}")


def test_SDET_4_el_CI_valida_el_compose_COMO_compose():
    """④ de @sdet: `verificar()` mide el YAML como dict. Que sea un COMPOSE válido
    —interpolación, claves que Docker acepta, servicios resolubles— no lo dice
    ningún test: lo dice `docker compose config`, y no lo invocaba nadie."""
    corridas = _pasos_run()
    assert any("docker compose" in r and "docker-compose.pilot.yml" in r
               and "config" in r for r in corridas), (
        f"ningún `run:` del job valida el compose: {corridas}")
    assert any("tests/pilot/env.example" in r for r in corridas), (
        "el `config` se invoca sin el --env-file de valores falsos, así que con "
        "los `:?` sin valor daría rc=1 y el gate sería rojo permanente")


def test_SDET_4_existe_el_env_example_y_NO_lleva_secretos():
    """Hoy `docker compose config` no se podía correr: cuatro `:?` sin valor (rc=1
    medido por @sdet). El `env.example` lo hace ejecutable **con valores falsos**, y
    falso tiene que significar falso: un ejemplo con un secreto de verdad lo publica
    en git para siempre."""
    ej = RAIZ / "tests" / "pilot" / "env.example"
    assert ej.exists(), "falta tests/pilot/env.example"
    texto = ej.read_text(encoding="utf-8")
    for var in ("LLMINBOX_JOURNAL_VOLUME_ID", "LLMINBOX_JOURNAL_VOLUME_WITNESS",
                "LLMINBOX_LEDGER_PILOTO_ID", "LLMINBOX_LEDGER_PILOTO_WITNESS",
                "LLMINBOX_PILOT_CREDENCIALES_SHA", "LLMINBOX_PILOT_TOKEN",
                "LLMINBOX_PILOT_LEDGER_HOST", "LLMINBOX_PILOT_LEDGER_TESTIGO_HOST",
                "LLMINBOX_PILOT_MAPA_HOST", "LLMINBOX_PILOT_ROSTER_HOST",
                "LLMINBOX_PILOT_PEPPER_HOST",
                "LLMINBOX_PILOT_PROJECTOR_FRAME_KEY_HOST",
                "LLMINBOX_PILOT_AGENTE_CREDENCIAL"):
        assert re.search(rf"(?m)^{var}=", texto), f"{var} no está en el env.example"
    # Y que los valores se DECLAREN falsos, no que lo parezcan.
    for linea in texto.splitlines():
        if "=" in linea and not linea.startswith("#"):
            valor = linea.split("=", 1)[1]
            assert "FALSO" in valor or "example" in valor or set(valor) <= set("0123456789abcdef-/.pilot_"), (
                f"valor sospechoso de ser real en el ejemplo: {linea.split('=')[0]}")


def test_SDET_7_la_cabecera_del_compose_no_cita_ficheros_que_no_existen():
    """⑦ de @sdet: la línea 13 citaba `tests/pytest/test_m1_pilot_topologia.py`, que
    se movió a `tests/pilot/test_topologia.py` en la primera entrega. Una ref rota en
    la cabecera del fichero que la gente lee primero manda a buscar donde no hay
    nada — y este repo ya sabe lo que cuesta una cita que no resuelve."""
    import re as _re
    texto = (RAIZ / "docker-compose.pilot.yml").read_text(encoding="utf-8")
    citados = _re.findall(r"`(tests/[\w./-]+|tools/[\w./-]+|docs/[\w./-]+)`", texto)
    assert citados, "la aguja no encuentra ni una cita: no mide nada"
    rotas = [c for c in citados if not (RAIZ / c).exists()]
    assert not rotas, f"el compose cita ficheros que no existen: {rotas}"


def test_SDET_3_el_test_de_no_tocar_la_flota_MIRA_el_returncode():
    """③ de @sdet. Si `git` fallara —binario ausente, repo roto—, `stdout` sale
    vacío y la aserción «no tocaste nada» pasa **por el fallo**. Un test que sólo
    mira stdout no distingue «no hay diferencias» de «no pude preguntar»."""
    fuente = (RAIZ / "tests" / "pilot" / "test_topologia.py").read_text(encoding="utf-8")
    bloque = fuente[fuente.index("def test_el_pilot_no_toca_el_compose_de_la_flota"):]
    bloque = bloque[:bloque.index("\ndef ") if "\ndef " in bloque else len(bloque)]
    assert "check=True" in bloque or "returncode" in bloque, (
        "el subprocess del test de flota no comprueba su propio rc")


@pytest.mark.parametrize("knob", ["LLMINBOX_JOURNAL_VOLUME_ID",
                                  "LLMINBOX_JOURNAL_VOLUME_WITNESS",
                                  "LLMINBOX_LEDGER_PILOTO_ID",
                                  "LLMINBOX_LEDGER_PILOTO_WITNESS"])
def test_I11_cada_perilla_por_separado(compose, knob):
    """② de @sdet: un falsador que sólo quita UNA de las cuatro deja tres guardas sin
    discriminar — sobrevivirían a su mutante y el censo las cuenta, con razón."""
    m = copy.deepcopy(compose)
    del m["services"]["gateway"]["environment"][knob]
    fallos = pilot_topologia.verificar(m)
    assert "I11" in codigos(fallos)
    assert any(knob in f for f in fallos), "el mensaje no dice CUÁL falta"


def test_SDET_5_el_estreno_ataca_LOS_MISMOS_volumenes_que_el_piloto():
    """Un fichero de estreno con otro `name:` estrenaría un juego de volúmenes
    PARALELO: testigos creados en un sitio, gateway leyendo otro, y el rojo llegando
    con el mensaje de montaje cruzado sin que nadie hubiera montado nada mal."""
    import yaml as _y
    piloto = _y.safe_load((RAIZ / "docker-compose.pilot.yml").read_text(encoding="utf-8"))
    estreno = _y.safe_load((RAIZ / "docker-compose.pilot-estreno.yml").read_text(encoding="utf-8"))
    assert piloto["name"] == estreno["name"], "project name distinto = volúmenes distintos"
    assert "llminbox-pilot-journal" in estreno["volumes"]
    assert "llminbox-pilot-journal" in piloto["volumes"]


def test_SDET_5_el_estreno_NO_monta_ni_el_mapa_ni_el_pepper():
    """El estreno no los necesita, y montarlos ahí sería abrir por la puerta de
    servicio lo que el compose del piloto cierra por la principal."""
    import yaml as _y
    estreno = _y.safe_load((RAIZ / "docker-compose.pilot-estreno.yml").read_text(encoding="utf-8"))
    montajes = " ".join(estreno["services"]["estreno"]["volumes"])
    assert "mapa.json" not in montajes and "pepper" not in montajes


# ══════════════════════════════════════════════════════════════════════════════
# La semántica de `unhealthy` DESPUÉS de arrancar: documentada Y probada.
#
# Estaba escrita en prosa y no la ataba nada. Una frase del doc que nadie falsa
# envejece sola: cambia el `interval` del compose y el documento sigue diciendo
# `30s` con la misma seguridad. Estas pruebas ponen a la prosa un falsador.
# ══════════════════════════════════════════════════════════════════════════════

DOC = RAIZ / "docs" / "PILOT-M1-TOPOLOGY.md"


def test_UNHEALTHY_la_ventana_que_dice_el_doc_es_la_del_compose(compose):
    """El doc afirma «el healthcheck corre cada `interval` (`30s`)». Si alguien
    sube el intervalo, la frase se vuelve falsa sin que nadie toque el doc — y una
    ventana de exposición mal declarada es peor que no declararla."""
    declarado = re.search(r"corre cada `interval` \(`(\w+)`\)", DOC.read_text(encoding="utf-8"))
    assert declarado, "el doc ya no declara la ventana: si se quita, hay que quitar esta prueba a sabiendas"
    real = compose["services"]["gateway"]["healthcheck"]["interval"]
    assert declarado.group(1) == real, (
        f"el doc dice `{declarado.group(1)}` y el compose dice `{real}`")


def test_UNHEALTHY_nada_en_el_compose_reinicia_por_estar_enfermo(compose):
    """Docker no actúa sobre `unhealthy` por sí solo, y este compose tampoco añade
    quien lo haga: ni `restart` que reintente, ni un `autoheal` de los que vigilan
    el estado ajeno. Si mañana entra uno, el doc deja de ser cierto AQUÍ."""
    for nombre, srv in compose["services"].items():
        assert srv.get("restart", "no") == "no", (
            f"`{nombre}` declara restart={srv.get('restart')!r}: un fail-closed con "
            f"reinicio automático es un crashloop")
        etiquetas = " ".join(srv.get("labels") or [])
        assert "autoheal" not in etiquetas.lower(), f"`{nombre}` se apunta a un autoheal"
    assert not any("autoheal" in s.lower() for s in compose["services"]), (
        "hay un servicio autoheal en el compose: entonces SÍ actúa alguien y el doc miente")


def test_UNHEALTHY_lo_que_actua_es_la_sonda_y_el_doc_lo_dice_asi(compose):
    """Ata las tres piezas: el doc dice `SIGTERM` al PID 1, el código lo hace, y el
    healthcheck invoca ESA ruta. Si una de las tres se mueve, esto cae."""
    doc = DOC.read_text(encoding="utf-8")
    assert "SIGTERM" in doc and "PID 1" in doc, "el doc ya no dice qué actúa"
    codigo = (RAIZ / "tools" / "pilot_preflight.py").read_text(encoding="utf-8")
    assert "kill_fn(1, signal.SIGTERM)" in codigo, "el código ya no corta el PID 1"
    prueba = " ".join(compose["services"]["gateway"]["healthcheck"]["test"])
    assert "--readiness" in prueba, "el healthcheck no invoca la ruta que corta"


def _seccion(titulo_empieza: str) -> str:
    """Devuelve UNA sección del doc, delimitada por sus `##`.

    🩸 Segunda vez en esta entrega que un `in texto_entero` sobrevive a su mutante:
    la frase que buscaba aparece DOS veces en el documento, así que romper la del
    apartado que importa dejaba la otra sosteniendo el verde. Un `in` sobre el
    fichero entero no comprueba que la salvedad esté DONDE hace falta — y una
    salvedad en otro apartado no protege a quien lee éste."""
    doc = DOC.read_text(encoding="utf-8")
    i = doc.index(f"## {titulo_empieza}")
    j = doc.find("\n## ", i + 1)
    return doc[i:j if j != -1 else len(doc)]


def test_UNHEALTHY_el_doc_declara_que_corta_TAMBIEN_las_lecturas():
    """La divergencia con el ADR §23 se declara o se está prometiendo de más. Es la
    salvedad que protege al LECTOR, no al autor: quien lea «readiness fail-closed»
    sin esto entenderá «rechaza mutaciones», que es lo que el ADR promete y lo que
    esto NO hace. Se exige EN SU APARTADO, no en cualquier parte del fichero."""
    sec = _seccion("Qué pasa —y qué NO— cuando el gateway se vuelve unhealthy")
    assert "también las lecturas" in sec, (
        "el apartado de `unhealthy` ya no dice que el corte se lleva las lecturas")
    assert "§23" in sec, "sin citar el ADR ahí, la divergencia no se puede comprobar"


def test_SEC_el_job_que_corre_tests_pilot_tiene_historia_SUFICIENTE():
    """P1 de @security, probado por él con los dos brazos: `actions/checkout@v5` no
    fija `fetch-depth` y su defecto es `1`. En un clon shallow el objeto de la base
    **no existe**, así que el test que compara contra ella falla — y el gate que
    acabamos de cablear para dejar de ser decorado rompería el build por algo que no
    es el piloto. La reacción natural a un build rojo es aflojar el test, y eso lo
    devuelve a decorado."""
    import yaml as _y
    ci = _y.safe_load(CI.read_text(encoding="utf-8"))
    job = ci["jobs"]["pilot-m1"]
    checkouts = [p for p in job["steps"] if "checkout" in str(p.get("uses", ""))]
    assert checkouts, "el job no hace checkout"
    for p in checkouts:
        prof = (p.get("with") or {}).get("fetch-depth")
        assert str(prof) == "0", (
            f"checkout sin `fetch-depth: 0` (vale {prof!r}): shallow por defecto, y "
            f"el test de la flota pide historia")


def test_SEC_el_test_de_la_flota_mide_lotes_sin_base_historica():
    """El guarda recorre commits alcanzables y el worktree, sin depender de una
    rama remota, merge-base o SHA que pueda desaparecer en otro clon."""
    fuente = (RAIZ / "tests" / "pilot" / "test_topologia.py").read_text(encoding="utf-8")
    bloque = fuente[fuente.index("def test_el_pilot_no_toca_el_compose_de_la_flota"):]
    bloque = bloque[:bloque.index("\ndef ") if "\ndef " in bloque else len(bloque)]
    assert 'git("log"' in bloque and 'git("diff-tree"' in bloque
    assert "WORKTREE" in bloque
    assert "merge-base" not in bloque and "cat-file" not in bloque


# ══════════════════════════════════════════════════════════════════════════════
# AUDITORÍA INDEPENDIENTE · NO-GO sobre 23d869c/4f8187a — seis supervivientes.
# Cada uno con SU falsador: un ataque que hoy PASA y tiene que dejar de pasar.
# ══════════════════════════════════════════════════════════════════════════════

def test_S1_un_overmount_del_TESTIGO_es_ROJO(compose):
    """Superviviente ①: montar un fichero forjado ENCIMA de `/journal/.volume-id`.
    El testigo deja de ser del volumen y pasa a ser del que monta — y la guarda ⓑ
    lee exactamente lo que le pusieron. `I1` mira el montaje de `/journal`; nadie
    miraba lo que se monta DENTRO."""
    m = copy.deepcopy(compose)
    m["services"]["gateway"]["volumes"].append("./forjado:/journal/.volume-id:ro")
    assert "I12" in codigos(pilot_topologia.verificar(m))


def test_S1_un_overmount_dentro_del_LEDGER_del_piloto_es_ROJO(compose):
    m = copy.deepcopy(compose)
    m["services"]["gateway"]["volumes"].append(
        "./forjado:/ledgers/llminbox/.llminbox-ledger-id:ro")
    assert "I12" in codigos(pilot_topologia.verificar(m))


def test_S1_el_montaje_del_propio_journal_NO_es_un_overmount(compose):
    """⊕ del detector: si contara el montaje del volumen como overmount de sí
    mismo, el compose bueno saldría rojo y el ⊖ de arriba no probaría nada."""
    assert "I12" not in codigos(
        pilot_topologia.verificar(compose, ledger_piloto="/ledgers/llminbox"))


def test_S2_el_agente_con_configs_es_ROJO(compose):
    """Superviviente ②: `I7` enumeraba `volumes` y `secrets`. `configs:` entrega el
    mapa o el pepper por otra puerta con el mismo efecto — y era la misma clase de
    fallo que ya me habían cazado: una política aplicada por enumeración."""
    m = copy.deepcopy(compose)
    m["services"]["agente"]["configs"] = ["mapa"]
    assert "I7" in codigos(pilot_topologia.verificar(m))


def test_S2_el_agente_con_env_file_es_ROJO(compose):
    m = copy.deepcopy(compose)
    m["services"]["agente"]["env_file"] = [".env.pilot"]
    assert "I7" in codigos(pilot_topologia.verificar(m))


def test_S2_el_agente_con_volumes_from_es_ROJO(compose):
    m = copy.deepcopy(compose)
    m["services"]["agente"]["volumes_from"] = ["gateway"]
    assert "I7" in codigos(pilot_topologia.verificar(m))


def test_S3_healthcheck_que_llama_al_preflight_SIN_readiness_es_ROJO(compose):
    """Superviviente ③: `I8` sólo exigía que el healthcheck nombrara
    `pilot_preflight`. Sin `--readiness` corre el preflight NORMAL: comprueba, pero
    **no corta** — y el corte es justo lo que el apartado de `unhealthy` promete."""
    m = copy.deepcopy(compose)
    m["services"]["gateway"]["healthcheck"]["test"] = [
        "CMD", "python3", "/app/tools/pilot_preflight.py"]
    assert "I8" in codigos(pilot_topologia.verificar(m))


def test_S6_I10_rechaza_un_default_PUBLICO(compose):
    """Superviviente ⑥a: `${LLMINBOX_PILOT_TOKEN:-publico}` pasaba la aguja. Un
    default convierte el fail-closed en fail-open: quien no ponga la variable
    arranca con un token conocido."""
    m = copy.deepcopy(compose)
    m["services"]["gateway"]["environment"]["LLMINBOX_TOKEN"] = "${LLMINBOX_PILOT_TOKEN:-publico}"
    assert "I10" in codigos(pilot_topologia.verificar(m))


def test_S6_I10_rechaza_la_COMPOSICION_con_el_token_de_la_flota(compose):
    """Superviviente ⑥b: `${LLMINBOX_PILOT_TOKEN:-${LLMINBOX_TOKEN}}` casaba con la
    aguja Y hereda el token de la flota si nadie pone el propio. Es exactamente el
    camino que `P1-4` cerraba, reabierto por la puerta del default."""
    m = copy.deepcopy(compose)
    m["services"]["gateway"]["environment"]["LLMINBOX_TOKEN"] = \
        "${LLMINBOX_PILOT_TOKEN:-${LLMINBOX_TOKEN}}"
    assert "I10" in codigos(pilot_topologia.verificar(m))


def test_S6_I10_acepta_la_forma_OBLIGATORIA(compose):
    """⊕: la forma con `:?` —sin default— sigue pasando. Sin este control, exigir
    de más habría roto el compose bueno sin que nadie lo viera."""
    m = copy.deepcopy(compose)
    m["services"]["gateway"]["environment"]["LLMINBOX_TOKEN"] = \
        "${LLMINBOX_PILOT_TOKEN:?token propio}"
    assert "I10" not in codigos(pilot_topologia.verificar(m))


def test_CI_prueba_el_ESTRENO():
    """El estreno es el camino que el operador corre UNA vez y del que salen las dos
    huellas. Se ejercita SIN duplicar: sus pruebas viven dentro de `tests/pilot`, que
    el job corre entero, y su COMPOSE tiene paso propio porque ningún test lo parsea.

    La aserción va por las DOS mitades y ninguna se da por supuesta: que existan
    pruebas de estreno (si alguien las borra, esto cae) y que el compose se valide.
    """
    import subprocess
    corridas = _pasos_run()
    assert any("pytest" in r and "tests/pilot" in r for r in corridas), (
        f"el job no corre la suite del piloto: {corridas}")
    r = subprocess.run([sys.executable, "-m", "pytest", str(RAIZ / "tests" / "pilot"),
                        "-k", "estreno", "--collect-only", "-q",
                        "-p", "no:cacheprovider"],
                       capture_output=True, text=True, cwd=str(RAIZ))
    n = re.search(r"(\d+)/\d+ tests collected", r.stdout) or \
        re.search(r"(\d+) tests? collected", r.stdout)
    assert n and int(n.group(1)) > 0, (
        f"la suite que corre el CI no contiene ninguna prueba de estreno: "
        f"{r.stdout[-300:]}")
    assert any("docker compose" in x and "docker-compose.pilot-estreno.yml" in x
               and "config" in x for x in corridas), (
        "el CI no valida el compose del estreno")

def test_DOC_el_numero_de_pruebas_que_declara_es_el_REAL():
    """El doc decía `96` cuando ya eran `100`. Un número en prosa que nadie compara
    envejece a la primera prueba nueva, y este documento es lo que lee quien llega."""
    import subprocess
    doc = DOC.read_text(encoding="utf-8")
    declarado = re.search(r"`(\d+)` pruebas", doc)
    assert declarado, "el doc ya no declara cuántas pruebas hay"
    r = subprocess.run([sys.executable, "-m", "pytest", str(RAIZ / "tests" / "pilot"),
                        "--collect-only", "-q", "-p", "no:cacheprovider"],
                       capture_output=True, text=True, cwd=str(RAIZ))
    real = re.search(r"(\d+) tests? collected", r.stdout)
    assert real, f"no pude contar las pruebas: {r.stdout[-300:]}"
    assert declarado.group(1) == real.group(1), (
        f"el doc dice `{declarado.group(1)}` y hay {real.group(1)}")


def test_BE_el_gate_del_piloto_NO_cuelga_de_una_suite_ROJA():
    """El apunte de @backend (`21:31:18Z`), medido sobre mi propio `ci.yml`.

    Mis cuatro pasos estaban dentro del job `pytest`, DETRÁS de «Correr la suite».
    Un `run` que falla corta el job, y `tests/pytest` está HOY ROJO por un fallo
    ajeno y preexistente (`test_ninguna_perilla_nace_muerta`, por
    `LLMINBOX_SEARCH_CURSOR_KEY`, que llegó con `m2`). ⇒ **el job abortaba antes de
    llegar al piloto y el gate que cablé para dejar de ser decorado no se ejecutaba
    nunca**: presente en el YAML, ausente del log.

    Job propio ⇒ el orden deja de importar y el veredicto del piloto no depende de
    que otro cure su rojo. Esta prueba se pone roja si alguien lo devuelve a un job
    que también corra la suite general.
    """
    import yaml as _y
    ci = _y.safe_load(CI.read_text(encoding="utf-8"))
    dueños = [j for j, job in ci["jobs"].items()
              if any("tests/pilot" in str(p.get("run", "")) and "pytest" in str(p.get("run", ""))
                     for p in job.get("steps", []))]
    assert dueños, "ningún job ejecuta las pruebas del piloto"
    for j in dueños:
        corre_la_suite = [str(p.get("run", "")) for p in ci["jobs"][j]["steps"]
                          if "tests/pytest" in str(p.get("run", ""))]
        assert not corre_la_suite, (
            f"el job `{j}` corre el piloto Y la suite general {corre_la_suite}: si la "
            f"suite falla —hoy falla—, el job aborta y el piloto no se ejecuta")


# ══════════════════════════════════════════════════════════════════════════════
# DEUDA FUNCIONAL · `LLMINBOX_SEARCH_CURSOR_KEY`: el código la lee, el compose de
# la flota no la pasaba. Es la perilla inerte que dejaba ROJO el job `pytest` y
# que, por eso mismo, tapaba el gate del piloto detrás de ella.
# ══════════════════════════════════════════════════════════════════════════════

FLOTA = RAIZ / "docker-compose.yml"


def test_KNOB_ninguna_perilla_del_codigo_queda_sin_pasar():
    """El mismo cálculo que el guarda del repo (`test_ninguna_perilla_nace_muerta`),
    replicado aquí para que el piloto no dependa de una suite que hoy está roja
    para saber si SU cura funcionó. Una variable que el código lee y el compose no
    pasa es INERTE en el contenedor: se puede poner, el arranque no protesta y no
    hace nada."""
    cod = set(re.findall(r'["\'](LLMINBOX_[A-Z_]+)["\']',
                         (RAIZ / "servicio.py").read_text(encoding="utf-8")))
    comp = set(re.findall(r"^\s+(LLMINBOX_[A-Z_]+):", FLOTA.read_text(encoding="utf-8"),
                          re.M))
    exc = set(re.findall(r'^\s*"(LLMINBOX_[A-Z_]+)":',
                         (RAIZ / "tests" / "pytest" / "test_ninguna_perilla_es_inerte.py")
                         .read_text(encoding="utf-8"), re.M))
    assert len(cod) > 20 and len(comp) > 20, ("los detectores están mudos", cod, comp)
    assert not (cod - comp - exc), f"perillas inertes: {sorted(cod - comp - exc)}"


def test_KNOB_la_clave_de_cursor_NO_lleva_valor_ni_default(compose):
    """NEGATIVO. La clave firma los cursores de búsqueda: un literal en el compose
    la publica en git para siempre, y un default —aunque parezca inocuo— sería una
    clave COMPARTIDA por todo el que clone, o sea cursores forjables por cualquiera.
    Sin valor, `servicio.py:3681` no monta la búsqueda: fail-closed, que es la
    conducta de hoy y no cambia."""
    import yaml as _y
    flota = _y.safe_load(FLOTA.read_text(encoding="utf-8"))
    valor = str(flota["services"]["llminbox"]["environment"]["LLMINBOX_SEARCH_CURSOR_KEY"])
    assert "$" in valor, f"la clave lleva valor literal en el compose: {valor!r}"
    m = re.fullmatch(r"\$\{LLMINBOX_SEARCH_CURSOR_KEY:-\}", valor.strip())
    assert m, (f"la forma admitida es `${{LLMINBOX_SEARCH_CURSOR_KEY:-}}` — sin "
               f"default, porque un default es una clave compartida; vale {valor!r}")


def test_CI_no_corre_tests_pilot_DOS_VECES():
    """El job corría `pytest tests/pilot -v` y luego `pytest tests/pilot -v -k
    estreno`: la segunda es un SUBCONJUNTO de la primera. Duplicar no añade
    cobertura —los mismos casos, el mismo veredicto— y sí añade minutos y una
    segunda cosa que mantener sincronizada."""
    corridas = [r for r in _pasos_run() if "pytest" in r and "tests/pilot" in r]
    assert len(corridas) == 1, f"el piloto se ejecuta {len(corridas)} veces: {corridas}"


# ══════════════════════════════════════════════════════════════════════════════
# CIERRE del auditor · P2 sobre los gates estáticos
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("valor,es_literal", [
    ("${LLMINBOX_PILOT_TOKEN}", False),
    ("${LLMINBOX_PILOT_TOKEN:-}", False),
    ("${LLMINBOX_PILOT_TOKEN:?falta}", False),
    ("$LLMINBOX_PILOT_TOKEN", False),
    ("un-token-en-claro", True),
    ("p$ssw0rd", True),                      # ← llevaba `$` y PASABA
    ("pre${VAR}", True),                     # ← llevaba `$` y PASABA
    ("${A}${B}", True),                      # ← llevaba `$` y PASABA
    ("${VAR}-cola", True),                   # ← llevaba `$` y PASABA
])
def test_I4_solo_acepta_una_indirección_PURA(compose, valor, es_literal):
    """🩸 CIERRE del auditor: el chequeo decía `if valor and "$" not in valor`, o
    sea que **cualquier cosa con un `$` dentro pasaba por indirección**. Los
    cuatro casos marcados llevan `$` y son secretos literales que se commitean.

    Los cuatro primeros son el ⊕: sin ellos, endurecer esto podría estar
    rechazando la forma legítima y todos los ⊖ seguirían verdes."""
    m = copy.deepcopy(compose)
    clave = pilot_topologia.SECRETOS_PROHIBIDOS_EN_YAML[0]
    m["services"]["gateway"].setdefault("environment", {})[clave] = valor
    hay = "I4" in codigos(pilot_topologia.verificar(m))
    assert hay is es_literal, f"{valor!r} -> I4={hay}, se esperaba {es_literal}"


@pytest.mark.parametrize("mutacion,motivo", [
    ({"disable": True}, "declarado y MUERTO"),
    ({"test": ["NONE"]}, "anula el de la imagen"),
    ({"test": ["CMD-SHELL", "python3 /app/tools/pilot_preflight.py --readiness || true"]},
     "se traga su código de salida"),
    ({"test": ["CMD-SHELL", "python3 /app/tools/pilot_preflight.py --readiness || exit 0"]},
     "se traga su código de salida"),
])
def test_I8_un_healthcheck_INERTE_es_ROJO(compose, mutacion, motivo):
    """Un healthcheck inerte es PEOR que no tenerlo: pinta `healthy` y nadie
    vuelve a mirar. Las tres formas de estarlo —desactivado, `NONE`, y tragarse
    su propio `rc`— pasaban las comprobaciones anteriores, que sólo miraban que
    la cadena `--readiness` estuviera presente."""
    m = copy.deepcopy(compose)
    m["services"]["gateway"]["healthcheck"] = dict(
        m["services"]["gateway"].get("healthcheck") or {}, **mutacion)
    assert "I8" in codigos(pilot_topologia.verificar(m)), motivo


@pytest.mark.parametrize("arranque,rojo", [
    (["/bin/sh", "-c", "python3 /app/tools/pilot_preflight.py && exec uvicorn servicio:app"], False),
    (["/bin/sh", "-c", "exec uvicorn servicio:app"], True),
    (["/bin/sh", "-c", "uvicorn servicio:app & python3 /app/tools/pilot_preflight.py"], True),
    (["/bin/sh", "-c", "python3 /app/tools/pilot_preflight.py ; exec uvicorn servicio:app"], True),
    (["/bin/sh", "-c", "python3 /app/tools/pilot_preflight.py || exec uvicorn servicio:app"], True),
])
def test_I8_el_preflight_va_ANTES_de_uvicorn_y_con_doble_ampersand(compose, arranque, rojo):
    """El compose lo dice en un COMENTARIO —«el preflight corre ANTES de uvicorn y
    con `&&`»— y **no lo hacía cumplir nadie**. Detrás, el preflight es un aviso;
    con `;` o `||`, el servidor arranca igual aunque diga rojo. Un invariante que
    sólo vive en un comentario no es un invariante."""
    m = copy.deepcopy(compose)
    m["services"]["gateway"]["entrypoint"] = arranque
    assert ("I8" in codigos(pilot_topologia.verificar(m))) is rojo, arranque[-1]


def test_I8_el_compose_REAL_pasa_las_tres_nuevas(compose):
    """⊕ sobre el sujeto de verdad: las tres comprobaciones nuevas de I8 no pueden
    poner rojo el compose que ya estaba bien. Sin esto, un gate que rechaza todo
    dejaría los ⊖ de arriba verdes y el piloto sin poder arrancar."""
    assert "I8" not in codigos(pilot_topologia.verificar(compose))


# ── correctiva P1 · I4/I8 EXACTOS ────────────────────────────────────────────

@pytest.mark.parametrize("clave", list(pilot_topologia.SECRETOS_PROHIBIDOS_EN_YAML))
def test_I4_un_DEFECTO_con_valor_es_un_secreto_literal(compose, clave):
    """🩸 «Indirección PURA» cerraba la FORMA y dejaba abierto el CONTENIDO.
    `${TOKEN:-hardcoded-secret}` es una sustitución entera, nada pegado a los
    lados, y `fullmatch` la aprobaba — con el secreto escrito en el YAML que se
    commitea, y el fail-closed convertido en fail-open.

    Medido antes de curar, con `${OTRO:-…}` en las CUATRO claves: `LLMINBOX_PEPPER`,
    `LLMINBOX_JOURNAL_PEPPER` y `LLMINBOX_WATCHER_TOKEN` salían VERDE ENTERO; sólo
    `LLMINBOX_TOKEN` caía, y por `I10`, no por aquí. Por eso va parametrizado por
    clave: sin eso, el ⊕ de una sola habría tapado tres sin guarda."""
    m = copy.deepcopy(compose)
    m["services"]["gateway"].setdefault("environment", {})[clave] = "${OTRO:-secreto}"
    assert "I4" in codigos(pilot_topologia.verificar(m))


def test_I4_el_mensaje_NO_reproduce_el_secreto(compose):
    """El mensaje acaba en el log del gate y en el ledger. Se da la LONGITUD del
    defecto, que basta para localizarlo en el YAML y no lo publica."""
    m = copy.deepcopy(compose)
    m["services"]["gateway"]["environment"]["LLMINBOX_PEPPER"] = "${OTRO:-p3pp3r-en-claro}"
    fallos = [f for f in pilot_topologia.verificar(m) if f.startswith("I4")]
    assert fallos
    assert "p3pp3r-en-claro" not in " ".join(fallos)


@pytest.mark.parametrize("cola,rojo", [
    (" || true", True),
    (" ||true", True),        # ← sin espacio: `"|| true" in prueba` era False
    (" | true", True),        # ← UNA barra: no estaba en la lista siquiera
    (" | grep -q ok", True),  # el rc de una tubería es el de su última etapa
    (" ; true", True),
    (" & true", True),        # `&` suelto manda el preflight al fondo
    (" && true", False),      # ⊕ `&&` NO se traga nada: propaga el fallo
    ("", False),              # ⊕ el healthcheck limpio
])
def test_I8_el_healthcheck_no_puede_perder_el_rc_del_preflight(compose, cola, rojo):
    """🩸 La lista de LITERALES enumeraba CÓMO se escribió el tragante, no QUÉ
    hace. Una política en prosa se aplica enumerando sus casos, y una lista de
    ejemplos siempre deja fuera el que nadie escribió.

    Los dos ⊕ del final son los que impiden que endurecer esto se convierta en un
    rechazo ciego: sin ellos, una guarda que rechazara toda cola pasaría los ⊖."""
    m = copy.deepcopy(compose)
    m["services"]["gateway"]["healthcheck"] = dict(
        m["services"]["gateway"].get("healthcheck") or {},
        test=["CMD-SHELL", "python3 /app/tools/pilot_preflight.py --readiness" + cola])
    assert ("I8" in codigos(pilot_topologia.verificar(m))) is rojo, repr(cola)


PRE = "python3 /app/tools/pilot_preflight.py"


@pytest.mark.parametrize("linea,rojo", [
    (f"{PRE} && exec uvicorn servicio:app", False),                 # ⊕ el bueno
    (f"{PRE} && true ; exec uvicorn servicio:app", True),           # `&&` cuelga de `true`
    (f"{PRE} && exec not-uvicorn-wrapper", True),                   # no es uvicorn
    (f"{PRE} && exec /opt/bin/uvicorn-wrapper servicio:app", True),
    (f"{PRE} && sleep 1 && exec uvicorn servicio:app", False),      # ⊕ eslabón de más, todo `&&`
    (f"{PRE} || exec uvicorn servicio:app", True),
])
def test_I8ter_la_CADENA_de_arranque_se_mira_por_ESLABONES(compose, linea, rojo):
    """🩸 `I8-ter` decidía con `str.index` sobre SUBCADENAS y las dos mitades
    fallaban por lo mismo:

        `preflight && true ; exec uvicorn`  ← `&&` está DENTRO de la rodaja `entre`
                                              y cuelga de `true`; el `;` suelta uvicorn
        `preflight && exec not-uvicorn-wrapper` ← `"uvicorn" in arranque` es True

    Preguntar «¿aparece `&&` en algún sitio entre estas dos posiciones?» no es
    preguntar «¿está la cadena unida?», y `in` no es identidad. Ahora se parte por
    los separadores y se exige que TODOS los de en medio sean `&&` y que el último
    eslabón ejecute exactamente `uvicorn`."""
    m = copy.deepcopy(compose)
    m["services"]["gateway"]["entrypoint"] = ["/bin/sh", "-c", linea]
    assert ("I8" in codigos(pilot_topologia.verificar(m))) is rojo, linea


# ── correctiva P1 · un ROJO no puede publicar lo que protege ─────────────────

CANARIO = "CANARIO-9f3a2b71-ESTO-NO-PUEDE-SALIR-EN-UN-MENSAJE"

# Las cuatro claves de secreto MÁS `LLMINBOX_TOKEN`, que es la que fugaba: `I10`
# imprimía su valor literal en el fallo. Se enumera la POBLACIÓN entera y no sólo
# el caso cazado — una política se aplica barriendo sus casos o no se aplica.
# `LLMINBOX_TOKEN` ya está DENTRO de `SECRETOS_PROHIBIDOS_EN_YAML`: concatenarla
# sin deduplicar daba dos filas idénticas por forma (`LLMINBOX_TOKEN0`/`...1`) y un
# recuento inflado. Lo cazó un mutante al matar 7 donde yo esperaba 2.
CLAVES_SENSIBLES = tuple(dict.fromkeys(
    tuple(pilot_topologia.SECRETOS_PROHIBIDOS_EN_YAML) + ("LLMINBOX_TOKEN",)))


@pytest.mark.parametrize("clave", CLAVES_SENSIBLES)
@pytest.mark.parametrize("forma,debe_haber_rojo", [
    ("{c}", True),                          # literal en claro
    ("${OTRO:-{c}}", True),                 # indirección con DEFECTO
    ("pre${VAR}{c}", True),                 # composición
    ("${LLMINBOX_PILOT_TOKEN:?{c}}", False),  # forma ADMITIDA: aquí el silencio es correcto
])
def test_SEC_ningun_mensaje_reproduce_el_valor_de_una_clave_sensible(
        compose, clave, forma, debe_haber_rojo):
    """I10 decía, literalmente, que `LLMINBOX_TOKEN` «vale» y a continuación el
    valor. Ese mensaje acaba en el log del gate, en el CI y en el ledger: un rojo
    que publica lo que protege convierte la comprobación de seguridad en el canal
    de fuga.

    El falsador busca el valor EXACTO —no «parece que no hay secretos»— sobre las
    CINCO claves y las CUATRO formas: el defecto vivía en una y la guarda tiene que
    valer para todas.

    🔑 `debe_haber_rojo` es el control que impide pasar por SILENCIO: un mensaje que
    no existe tampoco filtra, y sin él un verificador mudo aprobaría la matriz
    entera. Va por forma y no global porque `${VAR:?…}` es la forma ADMITIDA —ahí
    callar es lo correcto—, y ponerlo global daba un rojo que no era una fuga sino
    una población mal elegida por mí."""
    m = copy.deepcopy(compose)
    m["services"]["gateway"].setdefault("environment", {})[clave] = forma.replace("{c}", CANARIO)
    fallos = pilot_topologia.verificar(m, ledger_piloto="/ledgers/llminbox")
    if debe_haber_rojo:
        assert fallos, "el verificador no dijo nada: el test pasaría por silencio"
    texto = "\n".join(fallos)
    assert CANARIO not in texto, f"el valor sale en el mensaje: {texto}"


def test_SEC_I10_conserva_el_contexto_NO_sensible(compose):
    """⊕ del par, y es el que separa la cura de vaciar el mensaje. Callar también
    deja de filtrar, y deja al operador sin saber qué arreglar.

    Se exige que sobreviva TODO lo que sitúa el fallo y no es el secreto: el código,
    la clave, la forma admitida, POR QUÉ está mal, y la longitud —que localiza el
    valor en el YAML que el operador ya tiene delante—."""
    m = copy.deepcopy(compose)
    m["services"]["gateway"]["environment"]["LLMINBOX_TOKEN"] = "${OTRO:-" + CANARIO + "}"
    fallos = [f for f in pilot_topologia.verificar(m, ledger_piloto="/ledgers/llminbox")
              if f.startswith("I10")]
    assert fallos, "I10 dejó de cazar la forma prohibida"
    texto = " ".join(fallos)
    assert CANARIO not in texto
    assert "LLMINBOX_TOKEN" in texto, "no dice de QUÉ clave habla"
    assert "LLMINBOX_PILOT_TOKEN:?" in texto, "no dice cuál es la forma admitida"
    assert "DEFECTO" in texto, "no dice POR QUÉ está mal"
    assert str(len(CANARIO)) in texto, "no da la longitud, que es lo que lo localiza"


def test_SEC_el_clasificador_de_forma_nunca_devuelve_el_valor():
    """La guarda vive en `_forma_de`, así que se falsa AHÍ y no sólo en sus
    llamantes: si mañana alguien añade un mensaje nuevo, hereda la propiedad."""
    for valor in (CANARIO, f"${{OTRO:-{CANARIO}}}", f"pre${{VAR}}{CANARIO}",
                  f"${{LLMINBOX_PILOT_TOKEN:?{CANARIO}}}", f"${{A}}${{B}}{CANARIO}"):
        forma = pilot_topologia._forma_de(valor)
        assert CANARIO not in forma, f"{valor!r} -> {forma!r}"
        assert str(len(valor)) in forma or "DEFECTO" in forma or "…" in forma, forma


# ── correctiva sobre `acea43ce` · I8 por PROPIEDAD e I13 endurecimiento ──────

@pytest.mark.parametrize("cola,rojo", [
    # 🩸 los 6 que @security midió pasando MI gramática: `_NOOP` enumeraba
    # PROGRAMAS, y la lista de ordenes que salen `0` no se puede enumerar.
    ("|| echo ok", True), ("|| printf ''", True), ("; sleep 0", True),
    ("|| test 1", True), ("|| cat /dev/null", True), ("|| builtin true", True),
    # no-regresión: los que la lista sí cazaba
    ("|| true", True), ("||true", True), ("| true", True), ("|| exit 0", True),
    ("; true", True), ("& true", True), ("| grep -q ok", True),
    # ⊕ sin estos, «rechaza separadores» se cumple rechazándolo TODO
    ("", False), ("&& true", False), ("&& curl -sf localhost/h", False),
])
def test_I8_el_healthcheck_se_asevera_por_PROPIEDAD_no_por_lista_de_noops(
        compose, cola, rojo):
    """🩸 Dos pasadas y las dos por lo mismo. Primero la guarda era una lista de
    LITERALES (`"|| true"`), y se le escapaban `||true` y `| true`. La cambié por
    una gramática con `_NOOP = (true|:|/bin/true|exit 0)` … **que enumera igual**,
    sólo que programas en vez de literales — lo cazó @security
    (`MARK:security-el-doble-pipe-se-cierra-por-lista-no-por-forma`).

    Y el falsador de la segunda lo había escrito yo justificando la primera:
    *«una política se aplica ENUMERANDO sus casos, y una lista de ejemplos siempre
    deja fuera el que nadie escribió»*. Para `|` sí aserté la propiedad; para `||`,
    `;` y `&` volví a enumerar.

    La propiedad, una y para los cinco separadores: **UN solo eslabón, o todos
    `&&`** — sin mirar qué programa hay detrás. `&&` es el único que pasa porque es
    el único que NO se traga el `rc` (`cmd && x` propaga el fallo de `cmd`)."""
    m = copy.deepcopy(compose)
    m["services"]["gateway"]["healthcheck"] = dict(
        m["services"]["gateway"].get("healthcheck") or {},
        test=["CMD-SHELL", ("python3 /app/tools/pilot_preflight.py --readiness "
                            + cola).strip()])
    assert ("I8" in codigos(pilot_topologia.verificar(m))) is rojo, repr(cola)


def test_I8_el_healthcheck_REAL_es_una_lista_CMD_sin_shell(compose):
    """⊕ sobre el sujeto: hoy el compose usa `CMD` (lista, sin shell) ⇒ un solo
    eslabón ⇒ nada que tragarse. Por eso @security lo graduó MEDIO y no alto: no es
    un fallo vivo, es **un gate que no gatea**, y existe para el commit de mañana."""
    assert compose["services"]["gateway"]["healthcheck"]["test"][0] == "CMD"
    assert "I8" not in codigos(pilot_topologia.verificar(compose))


ESTRENO = _yaml_estreno = None


def _estreno():
    import yaml as _y
    return _y.safe_load((RAIZ / "docker-compose.pilot-estreno.yml").read_text(encoding="utf-8"))


def test_I13_los_dos_compose_REALES_pasan_el_endurecimiento(compose):
    """⊕ del par, y es el que impide que `I13` sea un gate que rechaza todo: los dos
    ficheros de verdad —incluido el `estreno`, que vive en otro compose y no tiene
    `gateway`, así que `verificar()` no lo alcanza— tienen que salir VERDES."""
    assert pilot_topologia.verificar_endurecimiento(compose) == []
    assert pilot_topologia.verificar_endurecimiento(_estreno()) == []


@pytest.mark.parametrize("quitar", ["read_only", "user", "tmpfs", "cap_drop", "security_opt"])
@pytest.mark.parametrize("fichero", ["piloto", "estreno"])
def test_I13_quitar_CUALQUIER_pieza_del_endurecimiento_es_ROJO(compose, quitar, fichero):
    """🩸 @security midió el endurecimiento INVERTIDO respecto al radio de daño: los
    tres `read_only`/`tmpfs`/`user` estaban en el `agente` —que no monta NADA— y no
    en `gateway` ni `estreno`, que tocan journal, pepper, mapa y el único ledger RW.

    Se parametriza por PIEZA y por FICHERO porque una guarda de endurecimiento que
    sólo comprueba una de las cinco pasa igual con las otras cuatro quitadas."""
    c = copy.deepcopy(compose if fichero == "piloto" else _estreno())
    servicio = "gateway" if fichero == "piloto" else "estreno"
    c["services"][servicio].pop(quitar, None)
    fallos = pilot_topologia.verificar_endurecimiento(c)
    assert any(servicio in f for f in fallos), f"{fichero}/{quitar} -> {fallos}"


@pytest.mark.parametrize("user,rojo", [
    ("1000:1000", False), ("1000", False),
    (None, True), ("0", True), ("0:0", True),
    ("root", True),      # por NOMBRE: desde el YAML no se sabe a qué uid resuelve
    ("llmi", True),      # idem, y por eso se rechaza: la guarda no puede medirlo
])
def test_I13_el_usuario_tiene_que_ser_numerico_y_no_root(compose, user, rojo):
    """Un `user:` por NOMBRE se rechaza A PROPÓSITO: desde el compose no se puede
    saber a qué uid resuelve dentro de la imagen, y una guarda que no puede medir su
    sujeto no es una guarda. Los dos ⊕ del principio impiden cerrar por el lado malo."""
    c = copy.deepcopy(compose)
    if user is None:
        c["services"]["gateway"].pop("user", None)
    else:
        c["services"]["gateway"]["user"] = user
    hay = any("gateway" in f and "user" in f.lower() or
              ("gateway" in f and "ROOT" in f) for f in
              pilot_topologia.verificar_endurecimiento(c))
    assert hay is rojo, f"{user!r} -> {pilot_topologia.verificar_endurecimiento(c)}"


def test_I13_el_agente_ya_pasaba_y_sigue_pasando(compose):
    """⊕ histórico: el `agente` llevaba las cinco desde el principio. Si `I13` lo
    pusiera rojo, la guarda estaría midiendo otra cosa."""
    assert not any("agente" in f for f in pilot_topologia.verificar_endurecimiento(compose))


def test_HC_el_healthcheck_declara_retries_1_porque_la_sonda_corta_al_primer_rojo(compose):
    """🩸 @security: `retries` NO gobierna el corte. `readiness()` manda `SIGTERM` al
    PID 1 en el primer rojo, sin contador ni estado; `retries` sólo gobierna la
    ETIQUETA `unhealthy`. Con `2`, el compose declaraba una tolerancia a fallos
    transitorios que no existe — y con `restart: "no"` al lado, un `EIO` momentáneo
    deja el piloto caído hasta que un humano lo mire.

    Este test ata la DECLARACIÓN a la CONDUCTA: el día que alguien añada el contador
    de rojos consecutivos, esto se pone rojo y le obliga a mover las dos a la vez."""
    assert compose["services"]["gateway"]["healthcheck"]["retries"] == 1
    fuente = (RAIZ / "tools" / "pilot_preflight.py").read_text(encoding="utf-8")
    assert not re.search(r"rojos_consecutivos|racha_roja", fuente), (
        "si ya hay contador de rojos, `retries: 1` dejó de ser lo correcto")
