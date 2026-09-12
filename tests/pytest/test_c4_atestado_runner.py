"""El atestado del artefacto liga el cierre COMPLETO del runner, y el build se niega si no.

Dos huecos medidos sobre `cda8995`, ambos silenciosos:

1. `fuentes_sha256` era una tupla escrita a mano dentro del `return`. Ligaba
   `runtime_root.py`, `coordination.py`, `native_gateway.py` y `projector_runner.py`,
   y NO ligaba `projector.py`, `observability.py`, `telemetry_bridge.py` ni los tres
   `search_*.py` — todos ellos empaquetados y ejercidos. Nada contrastaba esa lista
   contra el grafo de imports, así que la deriva era gratis.

2. Un digest `None` —el módulo no está en la imagen— se serializaba como un valor
   más del JSON. El build seguía en verde y publicaba un artefacto que decía, sin
   decirlo, que le faltaba una pieza.

Estas puertas cierran las dos: la lista no puede sub-declarar (se contrasta contra
el cierre AST) y el build no puede publicar un atestado incompleto (rc=1).
"""

from __future__ import annotations

import ast
import inspect
import json
import pathlib
import subprocess
import sys

import pytest

import atestigua_artefacto as A

RAIZ = pathlib.Path(__file__).resolve().parents[2]
GATE_DOCKER = RAIZ / "tests" / "pytest" / "test_docker_local_imports.py"


def _cierre_ast(entrypoints: tuple[str, ...]) -> set[str]:
    """Cierre de imports locales, recalculado aquí a propósito.

    Es la segunda mano sobre el mismo grafo: si coincidiera con el del gate del
    Dockerfile por compartir función, un fallo en esa función dejaría los dos
    gates verdes a la vez.
    """
    disponibles = {p.stem for p in RAIZ.glob("*.py")}
    pendientes = list(entrypoints)
    cierre: set[str] = set()
    while pendientes:
        modulo = pendientes.pop()
        if modulo in cierre:
            continue
        cierre.add(modulo)
        arbol = ast.parse((RAIZ / f"{modulo}.py").read_text(encoding="utf-8"))
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Import):
                nombres = [a.name.split(".", 1)[0] for a in nodo.names]
            elif isinstance(nodo, ast.ImportFrom) and nodo.module:
                nombres = [nodo.module.split(".", 1)[0]]
            else:
                continue
            pendientes.extend(n for n in nombres
                              if n in disponibles and n not in cierre)
    return cierre


# ══ ① LA LISTA DECLARADA NO PUEDE SUB-DECLARAR ══════════════════════════════════

def test_el_atestado_declara_el_cierre_ast_completo_del_runner():
    exigido = {f"{m}.py" for m in _cierre_ast(A.ENTRYPOINTS_RUNNER)}
    faltan = sorted(exigido - set(A.MODULOS_RUNNER))
    assert not faltan, (
        f"el atestado no liga estos módulos del cierre del runner: {faltan}")


def test_control_positivo_el_cierre_trae_las_piezas_que_faltaban():
    """⊕ sabido-positivo: el cierre contiene justo lo que la lista vieja omitía."""
    exigido = {f"{m}.py" for m in _cierre_ast(A.ENTRYPOINTS_RUNNER)}
    assert {"projector.py", "projector_runner.py", "observability.py",
            "telemetry_bridge.py", "search_store.py"} <= exigido
    assert {"projector.py", "projector_runner.py"} <= set(A.MODULOS_RUNNER)


def test_falsador_una_lista_sub_declarada_es_roja():
    """⊖ brazo negativo: prueba el detector, no sólo la lista sana."""
    exigido = {f"{m}.py" for m in _cierre_ast(A.ENTRYPOINTS_RUNNER)}
    for quitado in ("projector.py", "coordination.py", "servicio.py"):
        mutada = tuple(m for m in A.MODULOS_RUNNER if m != quitado)
        assert quitado in exigido - set(mutada), (
            f"quitar {quitado} de MODULOS_RUNNER tenía que ponerse rojo")


def test_los_dos_gates_hablan_del_mismo_runner():
    """La lista del atestado y la del gate del Dockerfile no pueden divergir."""
    texto = GATE_DOCKER.read_text(encoding="utf-8")
    espacio: dict = {}
    for nodo in ast.parse(texto).body:
        if isinstance(nodo, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "RUNNER_ENTRYPOINTS"
                for t in nodo.targets):
            espacio["v"] = ast.literal_eval(nodo.value)
    assert "v" in espacio, "el gate del Dockerfile ya no declara RUNNER_ENTRYPOINTS"
    assert tuple(espacio["v"]) == tuple(A.ENTRYPOINTS_RUNNER), (
        "el gate del Dockerfile y el atestado miden runners distintos")


# ══ ② EL BARRIDO LIGA LO EMPAQUETADO AUNQUE NADIE LO DECLARE ════════════════════

def _arbol(base: pathlib.Path, *, omitir: tuple[str, ...] = (),
           extra: tuple[str, ...] = ()) -> pathlib.Path:
    """Un `/app` de mentira: `artefacto()` hashea bytes, no importa módulos."""
    base.mkdir(parents=True, exist_ok=True)
    (base / "requirements.lock").write_text("fastapi==0.121.3 --hash=sha256:x\n")
    for nombre in A.MODULOS_RUNNER + A.FUENTES_EXTRA + extra:
        if nombre in omitir:
            continue
        (base / nombre).write_text(f"# {nombre}\n")
    return base


def test_el_atestado_liga_todo_py_empaquetado_no_solo_la_lista(tmp_path):
    base = _arbol(tmp_path / "app", extra=("modulo_nuevo.py",))
    datos = A.artefacto(str(base))
    fuentes = datos["fuentes_sha256"]
    assert fuentes["modulo_nuevo.py"], (
        "un módulo empaquetado que nadie declaró tiene que quedar ligado igual")
    assert fuentes["projector.py"] and fuentes["projector_runner.py"]
    assert datos["requirements_lock_sha256"]
    assert datos["runner_empaquetado"]["completo"] is True
    assert datos["runner_empaquetado"]["faltan"] == []


def test_un_modulo_ausente_no_se_atestigua_como_presente(tmp_path):
    base = _arbol(tmp_path / "app", omitir=("projector.py",))
    empaquetado = A.artefacto(str(base))["runner_empaquetado"]
    assert empaquetado["completo"] is False
    assert empaquetado["faltan"] == ["projector.py"]


# ══ ③ EL BUILD ES FAIL-CLOSED, NO UN AVISO EN /health ═══════════════════════════

def _correr(base: pathlib.Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(RAIZ / "atestigua_artefacto.py"),
         str(base / "ARTEFACTO.json")],
        capture_output=True, text=True)


def test_control_positivo_el_build_pasa_con_el_arbol_completo(tmp_path):
    base = _arbol(tmp_path / "app")
    r = _correr(base)
    assert r.returncode == 0, r.stderr
    salida = base / "ARTEFACTO.json"
    assert salida.is_file()
    datos = json.loads(salida.read_text())
    assert datos["runner_empaquetado"]["completo"] is True
    assert salida.stat().st_mode & 0o777 == 0o444, "el atestado no se reescribe en caliente"


def test_docker_no_entrega_app_al_usuario_runtime_y_lo_falsa_por_efecto():
    dockerfile = (RAIZ / "Dockerfile").read_text(encoding="utf-8")
    assert "chown llmi /data /app" not in dockerfile
    assert "chown llmi /data" in dockerfile
    assert "USER llmi" in dockerfile
    for path in ("/app", "/app/ARTEFACTO.json", "/app/projector.py"):
        assert f"test ! -w {path}" in dockerfile
    assert "mv /tmp/mv-control-source /tmp/mv-control-destination" in dockerfile
    assert "test -f /tmp/mv-control-destination" in dockerfile
    assert "! mv -f /tmp/forged-artefacto /app/ARTEFACTO.json" in dockerfile
    assert "! mv -f /tmp/forged-projector /app/projector.py" in dockerfile
    assert "test -f /tmp/forged-artefacto" in dockerfile
    assert "test -f /tmp/forged-projector" in dockerfile


def test_falsador_el_build_se_niega_si_falta_un_modulo_del_runner(tmp_path):
    base = _arbol(tmp_path / "app", omitir=("projector.py",))
    r = _correr(base)
    assert r.returncode == 1, (
        "una imagen sin el cierre del runner NO puede construirse en verde")
    assert "projector.py" in r.stderr, "no dice qué falta"
    assert not (base / "ARTEFACTO.json").exists(), (
        "no puede quedar un atestado escrito describiendo un artefacto roto")


PROHIBIDOS = ("token", "credencial", "secret", "password", "pepper")


def test_el_atestado_publicado_sigue_sin_secretos(tmp_path):
    """`/health` responde SIN token; lo que C4 añade al atestado no puede filtrar.

    `sqlite.compile_options` queda FUERA del barrido, y no es una excusa: es una
    lista cerrada que produce SQLite (`PRAGMA compile_options`), no el despliegue,
    y contiene `enable_fts3_tokenizer` — subcadena de `token`. Dejarla dentro
    convertía esta puerta en un rojo permanente sobre un valor que no es un secreto,
    y un rojo permanente enseña a ignorar el rojo.
    """
    base = _arbol(tmp_path / "app")
    datos = A.artefacto(str(base))
    del datos["sqlite"]["compile_options"]
    plano = json.dumps(datos).lower()
    for prohibido in PROHIBIDOS:
        assert prohibido not in plano, f"el atestado filtra {prohibido}"


def test_control_positivo_el_barrido_de_secretos_sabe_cazar(tmp_path):
    """⊕: si un módulo empaquetado se llamase como un secreto, esto lo vería."""
    base = _arbol(tmp_path / "app", extra=("pepper_loader.py",))
    datos = A.artefacto(str(base))
    del datos["sqlite"]["compile_options"]
    plano = json.dumps(datos).lower()
    assert any(p in plano for p in PROHIBIDOS), (
        "el barrido no alcanza `fuentes_sha256`: estaría verde por no mirar")


# ══ ④ ACTIVAR EXIGE CONFIGURACIÓN EXPLÍCITA Y COMPLETA ═══════════════════════════

def test_el_runner_activo_no_se_instala_sin_dependencias():
    """Ejecutado: active sin backend/carril sigue fallando cerrado."""
    import projector_runner as PR

    assert isinstance(PR.projector_runner_for_mode("disabled"),
                      PR.DisabledProjectorRunner)
    for modo in ("active", "enabled", "on", "", "bridge", None, True):
        with pytest.raises(PR.RunnerConfigurationError):
            PR.projector_runner_for_mode(modo)


def test_el_modo_por_defecto_del_runtime_es_disabled():
    import dataclasses

    import runtime_root as R

    campo = next(f for f in dataclasses.fields(R.RuntimeConfig)
                 if f.name == "projector_mode")
    assert campo.default == "disabled"


def test_el_entorno_solo_mueve_el_modo_por_la_variable_canonica():
    """La configuración lee el selector cerrado y no aliases ambiguos."""
    import runtime_root as R

    fuente = inspect.getsource(R.RuntimeConfig.from_environment)
    arbol = ast.parse(inspect.cleandoc(fuente).replace("@classmethod\n", "", 1))
    assert "LLMINBOX_PROJECTOR_MODE" in fuente
    assert "PROJECTOR_ENABLED" not in fuente
    assert "PROJECTOR_ACTIVE" not in fuente
