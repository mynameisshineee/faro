"""La imagen contiene el cierre de imports locales de su entrypoint.

Es una comprobación estática deliberada: no necesita construir Docker y también ve los
imports diferidos dentro de funciones, justo donde `servicio.py` carga la búsqueda.
"""

from __future__ import annotations

import ast
import json
import posixpath
import pathlib
import re
import shlex
from dataclasses import dataclass, field


ROOT = pathlib.Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "Dockerfile"


@dataclass
class _Stage:
    """La parte del filesystem de una etapa que procede del contexto local."""

    name: str
    workdir: str = "/"
    # ruta absoluta en la etapa -> módulo Python local original
    files: dict[str, str] = field(default_factory=dict)
    cmds: list[str] = field(default_factory=list)


def _instrucciones(dockerfile: str) -> list[tuple[str, str]]:
    """Tokeniza directivas Docker, incluida la continuación con barra invertida."""
    unido = re.sub(r"\\\r?\n[ \t]*", " ", dockerfile)
    resultado = []
    for linea in unido.splitlines():
        if not linea.strip() or linea.lstrip().startswith("#"):
            continue
        m = re.match(r"^\s*([A-Za-z]+)\s+(.*?)\s*$", linea)
        if m:
            resultado.append((m.group(1).upper(), m.group(2)))
    return resultado


def _ruta(workdir: str, valor: str) -> str:
    if "$" in valor:
        raise AssertionError(f"ruta Docker dinámica no acreditable: {valor!r}")
    if not valor.startswith("/"):
        valor = posixpath.join(workdir, valor)
    return posixpath.normpath("/" + valor.lstrip("/"))


def _copy_args(raw: str) -> tuple[str | None, list[str], str]:
    """Lee COPY shell/JSON y sus flags; devuelve (from, fuentes, destino)."""
    origen_etapa = None
    resto = raw.strip()
    while resto.startswith("--"):
        token, separador, cola = resto.partition(" ")
        if not separador:
            raise AssertionError(f"COPY sin fuentes: {raw!r}")
        if "=" in token:
            flag, valor = token[2:].split("=", 1)
            resto = cola.lstrip()
        else:
            flag = token[2:]
            partes = shlex.split(cola, posix=True)
            if flag != "from" or len(partes) < 3:
                raise AssertionError(f"flag COPY no parseable: {raw!r}")
            valor = partes[0]
            # `--from etapa fuente destino`, forma tolerada además de `--from=etapa`.
            resto = cola.lstrip()[len(shlex.quote(valor)):].lstrip()
        if flag == "from":
            origen_etapa = valor

    if resto.startswith("["):
        valores = json.loads(resto)
        assert isinstance(valores, list) and all(isinstance(x, str) for x in valores), (
            f"COPY JSON debe ser un array de strings: {raw!r}")
    else:
        valores = shlex.split(resto)
    assert len(valores) >= 2, f"COPY necesita fuente(s) y destino: {raw!r}"
    return origen_etapa, valores[:-1], valores[-1]


def _fuentes_contexto(fuentes: list[str], root: pathlib.Path) -> list[tuple[str, str]]:
    """Devuelve (ruta relativa a la fuente, identidad local) para .py de raíz."""
    encontrados = []
    for fuente in fuentes:
        limpio = posixpath.normpath(fuente.lstrip("/"))
        candidatos: list[pathlib.Path]
        if limpio in ("", "."):
            candidatos = sorted(root.glob("*.py"))
        elif any(c in limpio for c in "*?["):
            candidatos = sorted(root.glob(limpio))
        else:
            candidatos = [root / limpio]
        for candidato in candidatos:
            if candidato.is_dir():
                ficheros = sorted(candidato.rglob("*.py"))
            elif candidato.is_file() and candidato.suffix == ".py":
                ficheros = [candidato]
            else:
                continue
            for fichero in ficheros:
                # Los imports que cierra este gate son módulos top-level del repo.
                if fichero.parent != root:
                    continue
                encontrados.append((fichero.name, fichero.stem))
    return encontrados


def _fuentes_etapa(stage: _Stage, fuentes: list[str]) -> list[tuple[str, str]]:
    encontrados = []
    for fuente in fuentes:
        origen = _ruta("/", fuente)
        if origen in stage.files:
            encontrados.append((posixpath.basename(origen), stage.files[origen]))
            continue
        prefijo = origen.rstrip("/") + "/"
        for ruta, modulo in sorted(stage.files.items()):
            if ruta.startswith(prefijo):
                encontrados.append((ruta[len(prefijo):], modulo))
    return encontrados


def _copia(stage: _Stage, entradas: list[tuple[str, str]], destino: str,
           *, varias_fuentes: bool) -> None:
    destino_abs = _ruta(stage.workdir, destino)
    es_directorio = (destino.endswith("/") or destino_abs == stage.workdir
                     or varias_fuentes or len(entradas) > 1)
    for relativa, modulo in entradas:
        final = posixpath.join(destino_abs, relativa) if es_directorio else destino_abs
        stage.files[posixpath.normpath(final)] = modulo


def _etapas(dockerfile: str, root: pathlib.Path = ROOT) -> list[_Stage]:
    etapas: list[_Stage] = []
    por_nombre: dict[str, _Stage] = {}
    actual: _Stage | None = None
    for orden, raw in _instrucciones(dockerfile):
        if orden == "FROM":
            tokens = shlex.split(raw)
            while tokens and tokens[0].startswith("--"):
                tokens.pop(0)
            assert tokens, f"FROM no parseable: {raw!r}"
            base = tokens[0]
            tokens_upper = [t.upper() for t in tokens]
            nombre = (tokens[tokens_upper.index("AS") + 1]
                      if "AS" in tokens_upper else str(len(etapas)))
            # `FROM etapa_previa` hereda su filesystem; una imagen externa, no.
            heredada = por_nombre.get(base)
            actual = _Stage(nombre, heredada.workdir if heredada else "/",
                            dict(heredada.files) if heredada else {})
            etapas.append(actual)
            por_nombre[nombre] = actual
            por_nombre[str(len(etapas) - 1)] = actual
            continue
        assert actual is not None, f"{orden} antes del primer FROM"
        if orden == "WORKDIR":
            actual.workdir = _ruta(actual.workdir, raw)
        elif orden == "CMD":
            actual.cmds.append(raw)
        elif orden == "COPY":
            desde, fuentes, destino = _copy_args(raw)
            if desde is None:
                entradas = _fuentes_contexto(fuentes, root)
            else:
                origen = por_nombre.get(desde)
                entradas = _fuentes_etapa(origen, fuentes) if origen else []
            _copia(actual, entradas, destino, varias_fuentes=len(fuentes) > 1)
    assert etapas, "Dockerfile debe tener al menos una etapa"
    return etapas


def _entrypoint_module(dockerfile: str) -> str:
    runtime = _etapas(dockerfile)[-1]
    assert len(runtime.cmds) == 1, "la etapa runtime debe declarar un único CMD JSON"
    argv = json.loads(runtime.cmds[0])
    candidatos = [arg.split(":", 1)[0] for arg in argv if ":" in arg]
    locales = [mod for mod in candidatos if (ROOT / f"{mod}.py").is_file()]
    assert len(locales) == 1, (
        "CMD debe nombrar exactamente un entrypoint módulo:objeto local; "
        f"encontrados {locales!r}"
    )
    return locales[0]


def _modulos_copiados(dockerfile: str) -> set[str]:
    runtime = _etapas(dockerfile)[-1]
    assert runtime.workdir == "/app", (
        f"la etapa runtime debe importar desde WORKDIR /app, no {runtime.workdir!r}")
    # Python añade el cwd al path. Para imports top-level sólo cuenta
    # `/app/modulo.py`, no un homónimo que quedó en /tmp, /web o /app/subdir.
    return {
        modulo for ruta, modulo in runtime.files.items()
        if posixpath.dirname(ruta) == runtime.workdir
        and posixpath.basename(ruta) == f"{modulo}.py"
    }


def _imports_locales(modulo: str, disponibles: set[str]) -> set[str]:
    arbol = ast.parse((ROOT / f"{modulo}.py").read_text(encoding="utf-8"))
    encontrados: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            nombres = (alias.name.split(".", 1)[0] for alias in nodo.names)
        elif isinstance(nodo, ast.ImportFrom) and nodo.module:
            nombres = (nodo.module.split(".", 1)[0],)
        else:
            continue
        encontrados.update(nombre for nombre in nombres if nombre in disponibles)
    return encontrados


def _cierre_local(entrypoint: str) -> set[str]:
    disponibles = {ruta.stem for ruta in ROOT.glob("*.py")}
    pendientes = [entrypoint]
    cierre: set[str] = set()
    while pendientes:
        modulo = pendientes.pop()
        if modulo in cierre:
            continue
        cierre.add(modulo)
        pendientes.extend(_imports_locales(modulo, disponibles) - cierre)
    return cierre


def test_docker_copia_el_cierre_de_imports_locales_del_entrypoint():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    entrypoint = _entrypoint_module(dockerfile)
    requeridos = _cierre_local(entrypoint)
    copiados = _modulos_copiados(dockerfile)

    faltan = sorted(requeridos - copiados)
    assert not faltan, (
        f"la imagen arranca {entrypoint}:app pero no COPY estos módulos locales "
        f"alcanzables: {faltan}; cierre completo={sorted(requeridos)}"
    )


def test_docker_copia_el_cierre_del_root_candidato_sin_activarlo():
    """C4-prep viaja en la imagen, pero el proceso por defecto sigue legacy."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    requeridos = _cierre_local("runtime_root")
    copiados = _modulos_copiados(dockerfile)
    assert not (requeridos - copiados), (
        "la imagen candidata no puede importar runtime_root; faltan "
        f"{sorted(requeridos - copiados)}"
    )
    assert _entrypoint_module(dockerfile) == "servicio", (
        "empaquetar el root candidato no autoriza activarlo")


def test_falsador_root_candidato_sin_coordination_es_rojo():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    copia = "COPY coordination.py native_gateway.py operator_admission.py runtime_root.py fleet_deadline_runner.py /app/"
    assert dockerfile.count(copia) == 1
    mutado = dockerfile.replace(copia, copia.replace("coordination.py ", ""), 1)
    assert "coordination" in (
        _cierre_local("runtime_root") - _modulos_copiados(mutado))


def test_la_guarda_ve_los_dos_caminos_diferidos_integrados():
    """Control positivo: evita una guarda verde que sólo descubra el entrypoint."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    cierre = _cierre_local(_entrypoint_module(dockerfile))
    assert {
        "servicio",
        "ledger_parse",
        "kind_registry",
        "telemetry_bridge",
        "observability",
        "search_store",
        "search_contract",
        "search_cursor",
    } <= cierre


def test_la_guarda_se_pone_roja_si_falta_un_modulo_local():
    """Brazo negativo: prueba el detector, no sólo el Dockerfile sano."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert dockerfile.count(" search_store.py") == 1
    mutado = dockerfile.replace(" search_store.py", "", 1)
    entrypoint = _entrypoint_module(mutado)
    assert "search_store" in _cierre_local(entrypoint) - _modulos_copiados(mutado)


def test_falsador_modulo_solo_en_etapa_previa_es_rojo():
    """Un COPY del builder no demuestra que el fichero exista en runtime."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    copia = "COPY ledger_parse.py kind_registry.py observability.py search_contract.py search_cursor.py search_store.py telemetry_bridge.py servicio.py ui.html atestigua_artefacto.py /app/"
    assert dockerfile.count(copia) == 1
    mutado = dockerfile.replace(
        "COPY web/ ./", "COPY web/ ./\nCOPY search_store.py /web/", 1
    ).replace(copia, copia.replace(" search_store.py", ""), 1)
    assert "search_store" in _cierre_local("servicio") - _modulos_copiados(mutado)


def test_falsador_runtime_sin_kind_registry_es_rojo():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert dockerfile.count(" kind_registry.py") == 1
    mutado = dockerfile.replace(" kind_registry.py", "", 1)
    assert "kind_registry" in (
        _cierre_local(_entrypoint_module(mutado)) - _modulos_copiados(mutado))


def test_falsador_modulo_en_runtime_pero_fuera_del_path_es_rojo():
    """Estar en la etapa final no basta si el import top-level no puede resolverlo."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert dockerfile.count(" search_store.py") == 1
    for destino in ("/tmp/search_store.py", "/app/vendor/search_store.py"):
        mutado = dockerfile.replace(" search_store.py", "", 1).replace(
            "# La interfaz compilada en la etapa anterior.",
            f"COPY search_store.py {destino}\n# La interfaz compilada en la etapa anterior.",
            1,
        )
        assert "search_store" in _cierre_local("servicio") - _modulos_copiados(mutado)


def test_copy_json_y_from_llevan_un_modulo_a_runtime_importable():
    """Control positivo del parser: JSON + --from no se ignoran ni mezclan etapas."""
    plantilla = """
FROM python AS build
WORKDIR /staging
COPY ["search_store.py", "/staging/search_store.py"]
FROM python AS runtime
WORKDIR /app
COPY ["servicio.py", "/app/servicio.py"]
{copy_from}
CMD ["uvicorn", "servicio:app"]
"""
    for copy_from in (
        'COPY --from=build ["/staging/search_store.py", "/app/search_store.py"]',
        "COPY --from=build /staging/search_store.py /app/",
    ):
        assert {"servicio", "search_store"} <= _modulos_copiados(
            plantilla.format(copy_from=copy_from))


# ══ C4 · EL CIERRE DEL RUNNER, NO SÓLO EL DEL ENTRYPOINT ════════════════════════
#
# `test_docker_copia_el_cierre_del_root_candidato_sin_activarlo` cubría
# `runtime_root`. Pero el runner tiene TRES puntos de entrada, y los otros dos no
# los miraba nadie: `projector_runner` (hoy el Null Object `disabled`) y
# `projector` (el que materializa el outbox al ledger). El primero pasaba el gate
# por construcción —su cierre de imports es él mismo, 0 imports locales— y el
# segundo no estaba en ningún COPY. Un gate verde sobre un módulo que no viaja.

RUNNER_ENTRYPOINTS = ("runtime_root", "projector", "projector_runner")


def _cierre_runner() -> set[str]:
    cierre: set[str] = set()
    for entrada in RUNNER_ENTRYPOINTS:
        cierre |= _cierre_local(entrada)
    return cierre


def test_docker_copia_el_cierre_completo_del_runner():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    faltan = sorted(_cierre_runner() - _modulos_copiados(dockerfile))
    assert not faltan, (
        f"la imagen empaqueta el runner pero no estos módulos alcanzables: {faltan}")


def test_empaquetar_el_runner_no_activa_el_runner():
    """El cierre viaja entero y el proceso por defecto sigue siendo el legacy."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert _entrypoint_module(dockerfile) == "servicio", (
        "empaquetar el runner completo no autoriza cambiar el CMD")


def test_control_positivo_el_cierre_del_runner_ve_los_tres_entrypoints():
    """⊕ sabido-positivo: que la unión contenga de verdad las tres ramas."""
    cierre = _cierre_runner()
    assert {"runtime_root", "projector", "projector_runner"} <= cierre
    # y las dependencias que sólo entran por la rama del proyector real
    assert {"coordination", "observability", "telemetry_bridge"} <= _cierre_local(
        "projector")


def test_el_root_activo_alcanza_projector_por_el_adaptador():
    assert "projector" not in _cierre_local("projector_runner"), (
        "el runner permanece desacoplado del projector concreto")
    assert "journal_projection_backend" in _cierre_local("runtime_root")
    assert "projector" in _cierre_local("runtime_root")
    assert "projector" not in _cierre_local("servicio")


def test_falsador_runner_sin_projector_es_rojo():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    copia = "COPY projector.py projector_runner.py journal_projection_backend.py /app/"
    assert dockerfile.count(copia) == 1
    mutado = dockerfile.replace(
        copia, "COPY projector_runner.py journal_projection_backend.py /app/", 1)
    assert "projector" in _cierre_runner() - _modulos_copiados(mutado)


def test_falsador_runner_sin_coordination_es_rojo():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    copia = "COPY coordination.py native_gateway.py operator_admission.py runtime_root.py fleet_deadline_runner.py /app/"
    assert dockerfile.count(copia) == 1
    mutado = dockerfile.replace(copia, copia.replace("coordination.py ", ""), 1)
    assert "coordination" in _cierre_runner() - _modulos_copiados(mutado)
