"""Contrato estatico de la capa que activa el runtime y el projector M1.

No levanta Docker. Protege el cableado que debe llegar al contenedor y deja la
prueba viva para el estreno: preflight antes del proceso, autoridad derivada del
mapa V8, secreto por fichero y readiness del runtime como healthcheck.
"""
from __future__ import annotations

import copy
import pathlib
import sys

import pytest
import yaml

RAIZ = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ / "tools"))

import pilot_topologia  # noqa: E402

COMPOSE_BASE = RAIZ / "docker-compose.pilot.yml"
COMPOSE_NATIVA = RAIZ / "docker-compose.pilot-native.yml"
COMPOSE_KEY_INIT = RAIZ / "docker-compose.pilot-key-init.yml"

ENTORNO_ACTIVO = {
    "LLMINBOX_LEGACY_MUTATION_POLICY": "native-required",
    "LLMINBOX_PROJECTOR_MODE": "active",
    "LLMINBOX_PROJECTOR_PRINCIPAL_ID": "pilot-projector",
    "LLMINBOX_PROJECTOR_LEDGER": "llminbox",
    "LLMINBOX_PROJECTOR_FRAME_KEY_FILE": "/run/projector/frame.key",
}


@pytest.fixture()
def base() -> dict:
    return yaml.safe_load(COMPOSE_BASE.read_text(encoding="utf-8"))


@pytest.fixture()
def overlay() -> dict:
    return yaml.safe_load(COMPOSE_NATIVA.read_text(encoding="utf-8"))


@pytest.fixture()
def key_init() -> dict:
    return yaml.safe_load(COMPOSE_KEY_INIT.read_text(encoding="utf-8"))


def _arranque(gateway: dict) -> str:
    entrypoint = gateway["entrypoint"]
    assert isinstance(entrypoint, list) and entrypoint[:2] == ["/bin/sh", "-c"]
    assert len(entrypoint) == 3
    return entrypoint[2]


def _destino(montaje: str) -> str:
    partes = montaje.rsplit(":", 2)
    if len(partes) == 3 and partes[-1] in {"ro", "rw"}:
        return partes[-2]
    return montaje.rsplit(":", 1)[-1]


def _compone(base: dict, overlay: dict) -> dict:
    """La parte del merge de Compose que usa esta capa.

    ``entrypoint`` reemplaza, environment/healthcheck fusionan por clave y los
    volumenes se combinan por destino. La capa introduce un destino nuevo.
    """
    fusion = copy.deepcopy(base)
    gw = fusion["services"]["gateway"]
    capa = overlay["services"]["gateway"]
    gw["entrypoint"] = copy.deepcopy(capa["entrypoint"])
    gw.setdefault("environment", {}).update(capa["environment"])
    por_destino = {_destino(v): v for v in gw.get("volumes", [])}
    por_destino.update({_destino(v): v for v in capa["volumes"]})
    gw["volumes"] = list(por_destino.values())
    gw.setdefault("healthcheck", {}).update(capa["healthcheck"])
    return fusion


def test_la_capa_es_minima_pero_declara_los_dos_gates_nuevos(overlay):
    assert set(overlay["services"]) == {"gateway"}
    assert set(overlay["services"]["gateway"]) == {
        "entrypoint", "environment", "volumes", "healthcheck",
    }


def test_la_capa_comparte_el_project_name_del_piloto(base, overlay):
    assert overlay["name"] == base["name"] == "llminbox-pilot-m1"


def test_arranca_preflight_antes_del_runtime_nativo(overlay):
    arranque = _arranque(overlay["services"]["gateway"])
    eslabones, separadores = pilot_topologia._cadena_de_arranque(arranque)
    assert separadores == ["&&"]
    assert pilot_topologia._programa(eslabones[0]) == "python3"
    assert "/app/tools/pilot_preflight.py" in eslabones[0].split()
    assert pilot_topologia._programa(eslabones[1]) == "uvicorn"
    assert "exec uvicorn runtime_root:create_app --factory" in eslabones[1]
    assert "servicio:app" not in arranque


def test_el_selector_activo_es_exacto_y_no_autodeclara_carril(overlay):
    entorno = overlay["services"]["gateway"]["environment"]
    assert entorno == ENTORNO_ACTIVO
    assert not any("PROJECTOR_LANE" in clave for clave in entorno)
    assert not any("CREDENTIAL" in clave or "SECRET" in clave for clave in entorno)


def test_frame_key_llega_desde_un_volumen_nombrado_solo_lectura(overlay):
    gw = overlay["services"]["gateway"]
    assert gw["volumes"] == ["llminbox-pilot-projector-key:/run/projector:ro"]
    assert set(overlay["volumes"]) == {"llminbox-pilot-projector-key"}
    assert gw["environment"]["LLMINBOX_PROJECTOR_FRAME_KEY_FILE"] == (
        "/run/projector/frame.key")
    assert "LLMINBOX_PILOT_PROJECTOR_FRAME_KEY_HOST" not in str(gw)


def test_inicializador_del_key_es_explicito_sin_red_y_con_mount_acotado(key_init):
    assert set(key_init["services"]) == {"projector-key-init", "pepper-init"}
    service = key_init["services"]["projector-key-init"]
    assert service["network_mode"] == "none"
    assert service["user"] == "0:0"
    assert service["entrypoint"] == ["python3", "/app/tools/pilot_key_init.py"]
    assert service["cap_drop"] == ["ALL"]
    assert set(service["cap_add"]) == {"CHOWN", "DAC_OVERRIDE"}
    assert service["read_only"] is True
    assert service["volumes"] == [
        "${LLMINBOX_PILOT_PROJECTOR_FRAME_KEY_HOST:?fichero frame key 0600}"
        ":/source/secret:ro",
        "llminbox-pilot-projector-key:/run/projector",
        "./tools:/app/tools:ro",
    ]
    pepper = key_init["services"]["pepper-init"]
    assert pepper["network_mode"] == "none"
    assert pepper["environment"]["LLMINBOX_SECRET_TARGET_NAME"] == "journal.pepper"
    assert pepper["volumes"][1] == "llminbox-pilot-pepper:/run/pepper"


def test_healthcheck_reemplaza_preflight_por_ready_sin_shell(overlay):
    health = overlay["services"]["gateway"]["healthcheck"]
    probe = health["test"]
    assert probe[:3] == ["CMD", "python3", "-c"]
    assert len(probe) == 4
    assert "127.0.0.1:8077/ready" in probe[3]
    assert "pilot_preflight" not in probe[3]
    assert "||" not in probe[3] and "; exit" not in probe[3]
    assert set(health) == {"test", "interval", "timeout", "retries", "start_period"}


def test_healthcheck_da_ventana_al_bootstrap_sin_convertirlo_en_fail_open(overlay):
    health = overlay["services"]["gateway"]["healthcheck"]
    assert health["start_period"] == "10s"
    assert health["interval"] == "5s"
    assert health["timeout"] == "4s"
    assert health["retries"] == 12
    assert "HTTPError" not in health["test"][3]


def test_la_composicion_conserva_todos_los_montajes_y_anade_el_frame_key(
        base, overlay):
    fusion = _compone(base, overlay)
    originales = {_destino(v) for v in base["services"]["gateway"]["volumes"]}
    activados = {_destino(v) for v in fusion["services"]["gateway"]["volumes"]}
    assert activados == originales | {"/run/projector"}


def test_la_composicion_pisa_y_no_duplica_los_selectores_activos(base, overlay):
    fusion = _compone(base, overlay)
    entorno = fusion["services"]["gateway"]["environment"]
    for clave, valor in ENTORNO_ACTIVO.items():
        assert entorno[clave] == valor


def test_el_resto_de_la_topologia_m1_sigue_valido(base, overlay):
    """El verificador historico exige preflight tambien como healthcheck.

    Esa propiedad se reemplaza deliberadamente por /ready y se prueba arriba;
    para aislar el resto de invariantes, restauramos solo la sonda antes de
    ejecutar el verificador completo.
    """
    fusion = _compone(base, overlay)
    fusion["services"]["gateway"]["healthcheck"] = copy.deepcopy(
        base["services"]["gateway"]["healthcheck"])
    fallos = pilot_topologia.verificar(fusion, ledger_piloto="/ledgers/llminbox")
    assert fallos == [], "\n".join(fallos)


def test_retirar_la_capa_sigue_aterrizando_en_legacy_bridge(base):
    gw = base["services"]["gateway"]
    assert "servicio:app" in _arranque(gw)
    assert gw["environment"]["LLMINBOX_LEGACY_MUTATION_POLICY"] == "bridge"
    assert "LLMINBOX_PROJECTOR_MODE" not in gw["environment"]
    assert "frame.key" not in " ".join(gw["volumes"])
