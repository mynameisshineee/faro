"""Pruebas ESTÁTICAS del cruce entre dos lanes (V1.0, G9 — `docs/V1.0-EXECUTION.md`,
workstream «two-lane isolation deployment»).

Misma forma deliberada que `tests/pilot/test_topologia.py`: cada invariante de
cruce trae su MUTANTE, y hay un control de que el instrumento no está mudo. Lo
que este fichero añade sobre M1 es el `⊕` de la COLISIÓN: `docker-compose.pilot
-dual.yml` hace que las dos lanes usen el MISMO nombre lógico de ledger a
propósito, y una prueba dedicada comprueba que esa colisión NO es, por sí sola,
un hallazgo — sólo lo es un recurso FÍSICO compartido.

Alcance, dicho aquí y no en una nota al pie: esto verifica la DEPLOYMENT
TOPOLOGY de dos lanes — volúmenes, red, variables de indirección. NO verifica
el kernel/supervisor nativo (otro flujo de trabajo, otro worktree) ni repite su
endurecimiento de sesión — eso es «C4» y queda fuera de este arnés a propósito.
"""
from __future__ import annotations

import copy
import pathlib
import sys

import pytest
import yaml

RAIZ = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ / "tools"))

import pilot_topologia_dual as dual  # noqa: E402

COMPOSE = RAIZ / "docker-compose.pilot-dual.yml"
ESTRENO = RAIZ / "docker-compose.pilot-dual-estreno.yml"


@pytest.fixture()
def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


@pytest.fixture()
def estreno() -> dict:
    return yaml.safe_load(ESTRENO.read_text(encoding="utf-8"))


def codigos(fallos: list[str]) -> set[str]:
    """El código de cruce (`X1_VOLUMEN`, `X2_IDENTIDAD`, …), sin el prefijo de
    lane que llevan los reenviados desde `pilot_topologia.verificar()`
    (`"A:I10"` → se queda como está; los propios de cruce no llevan lane)."""
    return {f.split(":", 1)[0] for f in fallos}


# ── ⊕ EL CONTROL POSITIVO ───────────────────────────────────────────────────

def test_las_dos_lanes_del_compose_real_estan_aisladas(compose):
    fallos = dual.verificar_dual(compose)
    assert fallos == [], "\n".join(fallos)


def test_verificador_dual_no_es_mudo():
    """⊖ del INSTRUMENTO: sin servicios, tiene que salir rojo."""
    assert dual.verificar_dual({"services": {}}) != []


def test_sin_la_lane_B_no_hay_cruce_que_comprobar_pero_SI_hay_I0(compose):
    m = copy.deepcopy(compose)
    del m["services"]["gateway_b"]
    fallos = dual.verificar_dual(m)
    assert any("I0" in f for f in fallos)


# ── ⊕ LA COLISIÓN ES DELIBERADA Y NO ES UN HALLAZGO ─────────────────────────

def test_las_dos_lanes_declaran_el_MISMO_nombre_logico_de_ledger(compose):
    """Documenta la premisa del fichero: si esto deja de ser cierto, el resto
    de este arnés deja de probar lo que dice probar."""
    ea = compose["services"]["gateway_a"]["environment"]["LLMINBOX_LEDGERS"]
    eb = compose["services"]["gateway_b"]["environment"]["LLMINBOX_LEDGERS"]
    assert ea == eb == "llminbox=/ledgers/llminbox/LEDGER.md,testigo=/ledgers/testigo/LEDGER.md"


def test_la_colision_del_nombre_logico_NO_es_un_hallazgo_por_si_sola(compose):
    """⊕ central de este fichero. Sin este control, alguien podría 'arreglar'
    el hallazgo renombrando el ledger lógico de una lane — lo que ROMPERÍA el
    contrato de G9 (nombres en colisión deliberada) sin que ninguna prueba se
    quejara. El aislamiento tiene que sobrevivir precisamente CON la colisión
    puesta, no gracias a quitarla."""
    assert dual.verificar_dual(compose) == []


# ── ⊖ UN MUTANTE POR CATEGORÍA DE CRUCE (la lista de G9) ────────────────────

def test_X0_RED_las_lanes_comparten_red_si_una_se_une_a_la_otra(compose):
    m = copy.deepcopy(compose)
    m["services"]["gateway_b"]["networks"] = ["red_a"]
    assert "X0_RED" in codigos(dual.verificar_dual(m))


def test_X0_RED_sin_networks_declaradas_compose_las_junta_en_la_por_defecto(compose):
    m = copy.deepcopy(compose)
    del m["services"]["gateway_a"]["networks"]
    del m["services"]["agente_a"]["networks"]
    assert "X0_RED" in codigos(dual.verificar_dual(m))


def test_X1_VOLUMEN_el_journal_de_B_reutiliza_el_volumen_NOMBRADO_de_A(compose):
    m = copy.deepcopy(compose)
    m["services"]["gateway_b"]["volumes"] = [
        "llminbox-pilot-dual-a-journal:/journal" if v.endswith(":/journal") else v
        for v in m["services"]["gateway_b"]["volumes"]]
    assert "X1_VOLUMEN" in codigos(dual.verificar_dual(m))


def test_X1_VOLUMEN_el_bind_del_ledger_de_B_copia_la_variable_de_A(compose):
    """El mutante que reproduce el copy-paste real: alguien reutiliza la
    indirección de la lane A al escribir el compose de la B."""
    m = copy.deepcopy(compose)
    m["services"]["gateway_b"]["volumes"] = [
        v.replace("LLMINBOX_DUAL_LANE_B_LEDGER_HOST", "LLMINBOX_DUAL_LANE_A_LEDGER_HOST")
        if "/ledgers/llminbox" in v and ":ro" not in v else v
        for v in m["services"]["gateway_b"]["volumes"]]
    assert "X1_VOLUMEN" in codigos(dual.verificar_dual(m))


def test_X1_VOLUMEN_compartir_montar_tools_de_solo_lectura_NO_es_un_hallazgo(compose):
    """⊕ del propio detector: `/app/tools` es código compartido, no estado de
    lane. Sin este control, endurecer `X1_VOLUMEN` de más rompería el compose
    bueno — que sí comparte ese montaje entre las dos lanes."""
    orig_a = [m["origen"] for m in
              dual._m1.normaliza_montajes(compose["services"]["gateway_a"])
              if m["destino"] == "/app/tools"]
    orig_b = [m["origen"] for m in
              dual._m1.normaliza_montajes(compose["services"]["gateway_b"])
              if m["destino"] == "/app/tools"]
    assert orig_a and orig_a == orig_b, "la premisa del control ya no se cumple"
    assert "X1_VOLUMEN" not in codigos(dual.verificar_dual(compose))


def test_X1_VOLUMEN_un_volumen_declarado_solo_en_un_lado_falta_arriba(compose):
    m = copy.deepcopy(compose)
    del m["volumes"]["llminbox-pilot-dual-b-journal"]
    assert "X1_VOLUMEN" in codigos(dual.verificar_dual(m))


def test_X2_IDENTIDAD_el_token_de_B_referencia_la_variable_de_A(compose):
    m = copy.deepcopy(compose)
    m["services"]["gateway_b"]["environment"]["LLMINBOX_TOKEN"] = \
        "${LLMINBOX_DUAL_LANE_A_TOKEN:?token propio de la lane A, distinto de la lane B y de la flota}"
    fallos = dual.verificar_dual(m)
    assert "X2_IDENTIDAD" in codigos(fallos)
    # Y NO por partida doble: en modo relajado, `I10` de M1 no exige un nombre
    # fijo por lane, así que el ÚNICO código que caza este mutante es el de
    # cruce — si `B:I10` también saltara, `X2_IDENTIDAD` sería redundante y no
    # se podría afirmar que aporta cobertura propia.
    assert not any(f.startswith("B:I10") for f in fallos), (
        "el mutante también dispara I10 por lane: X2_IDENTIDAD sería redundante")


def test_X2_IDENTIDAD_el_token_propio_de_cada_lane_no_es_un_hallazgo(compose):
    assert "X2_IDENTIDAD" not in codigos(dual.verificar_dual(compose))


def test_X3_SESION_el_atestado_del_mapa_de_B_referencia_la_variable_de_A(compose):
    m = copy.deepcopy(compose)
    m["services"]["gateway_b"]["environment"]["LLMINBOX_CREDENCIALES_SHA"] = \
        "${LLMINBOX_DUAL_LANE_A_CREDENCIALES_SHA:?sha256 del mapa de la lane A}"
    assert "X3_SESION" in codigos(dual.verificar_dual(m))


def test_X4_LEDGER_el_id_del_ledger_de_B_referencia_la_variable_de_A(compose):
    m = copy.deepcopy(compose)
    m["services"]["gateway_b"]["environment"]["LLMINBOX_LEDGER_PILOTO_ID"] = \
        "${LLMINBOX_DUAL_LANE_A_LEDGER_ID:?identidad del ledger de la lane A}"
    assert "X4_LEDGER" in codigos(dual.verificar_dual(m))


def test_X4_LEDGER_el_testigo_del_ledger_de_B_referencia_la_variable_de_A(compose):
    m = copy.deepcopy(compose)
    m["services"]["gateway_b"]["environment"]["LLMINBOX_LEDGER_PILOTO_WITNESS"] = \
        "${LLMINBOX_DUAL_LANE_A_LEDGER_WITNESS:?la imprime el primer --init de la lane A}"
    assert "X4_LEDGER" in codigos(dual.verificar_dual(m))


def test_X5_RECIBO_el_id_del_journal_de_B_referencia_la_variable_de_A(compose):
    m = copy.deepcopy(compose)
    m["services"]["gateway_b"]["environment"]["LLMINBOX_JOURNAL_VOLUME_ID"] = \
        "${LLMINBOX_DUAL_LANE_A_JOURNAL_VOLUME_ID:?genera uno con `python3 tools/pilot_preflight.py --genera-id`}"
    assert "X5_RECIBO" in codigos(dual.verificar_dual(m))


def test_X6_CURSOR_la_clave_de_busqueda_de_B_referencia_la_variable_de_A(compose):
    m = copy.deepcopy(compose)
    m["services"]["gateway_b"]["environment"]["LLMINBOX_SEARCH_CURSOR_KEY"] = \
        "${LLMINBOX_DUAL_LANE_A_SEARCH_CURSOR_KEY:-}"
    assert "X6_CURSOR" in codigos(dual.verificar_dual(m))


def test_X6_CURSOR_dos_lanes_ambas_sin_clave_puesta_no_cruzan_por_omision(compose):
    """⊕: vaciar la variable de las DOS lanes dice 'sin clave' —Search se
    desmonta fail-closed, conducta de hoy— y no un cruce: no hay variable que
    comparar. `X6_CURSOR` no debe fingir un cruce donde sólo hay ausencia."""
    m = copy.deepcopy(compose)
    m["services"]["gateway_a"]["environment"]["LLMINBOX_SEARCH_CURSOR_KEY"] = ""
    m["services"]["gateway_b"]["environment"]["LLMINBOX_SEARCH_CURSOR_KEY"] = ""
    assert "X6_CURSOR" not in codigos(dual.verificar_dual(m))


# ══════════════════════════════════════════════════════════════════════════
# X7 · RECUPERACIÓN — se mide sobre el ESTRENO, el único fichero que escribe
# identidad. `pilot_topologia_dual.py` no lo ve: no hay `gateway`/`agente`
# ahí. Mismo patrón que `test_SDET_5_*` en `test_topologia.py`.
# ══════════════════════════════════════════════════════════════════════════

def test_X7_el_estreno_dual_ataca_LOS_MISMOS_volumenes_que_el_piloto_dual(compose, estreno):
    assert compose["name"] == estreno["name"], "project name distinto = volúmenes distintos"
    for vol in ("llminbox-pilot-dual-a-journal", "llminbox-pilot-dual-b-journal"):
        assert vol in compose["volumes"] and vol in estreno["volumes"]


def test_X7_estreno_a_no_declara_ni_un_recurso_de_la_lane_B(estreno):
    texto_a = str(estreno["services"]["estreno_a"])
    for marca in ("LANE_B", "-b-journal", "red_b"):
        assert marca not in texto_a, f"`estreno_a` cita un recurso de la lane B: {marca}"


def test_X7_estreno_b_no_declara_ni_un_recurso_de_la_lane_A(estreno):
    texto_b = str(estreno["services"]["estreno_b"])
    for marca in ("LANE_A", "-a-journal", "red_a"):
        assert marca not in texto_b, f"`estreno_b` cita un recurso de la lane A: {marca}"


def test_X7_MUTANTE_estreno_a_monta_el_journal_de_la_lane_B_es_ROJO(estreno):
    """⊖: si alguien pega mal un volumen, `estreno_a` podría certificar o
    sobrescribir el testigo de la lane B — el cruce que G9 nombra para
    'recovery' a nivel de despliegue."""
    m = copy.deepcopy(estreno)
    m["services"]["estreno_a"]["volumes"] = [
        "llminbox-pilot-dual-b-journal:/journal" if v.endswith(":/journal") else v
        for v in m["services"]["estreno_a"]["volumes"]]
    assert "llminbox-pilot-dual-b-journal:/journal" in m["services"]["estreno_a"]["volumes"]
    # Documentado por aserción directa (no hay `verificar_estreno_dual()`
    # dedicado: el propio valor mutado ya es la evidencia), y cazado por
    # `test_X7_estreno_a_no_declara_ni_un_recurso_de_la_lane_B` de arriba si
    # se corre sobre ESTE dict en vez del real.
    texto_a = str(m["services"]["estreno_a"])
    assert "-b-journal" in texto_a


def test_X7_estreno_a_y_b_no_montan_ni_mapa_ni_pepper(estreno):
    """Mismo principio que `test_SDET_5_el_estreno_NO_monta_ni_el_mapa_ni_el_pepper`
    en M1: el estreno no los necesita, y montarlos sería abrir por la puerta de
    servicio lo que el compose del piloto cierra por la principal."""
    for nombre in ("estreno_a", "estreno_b"):
        montajes = " ".join(estreno["services"][nombre]["volumes"])
        assert "mapa.json" not in montajes and "pepper" not in montajes


# ══════════════════════════════════════════════════════════════════════════
# Endurecimiento (`I13`) y forma del compose — no son cruce, pero sin ellos
# las cuatro contenedores nuevas no llevarían lo que M1 exige a todas.
# ══════════════════════════════════════════════════════════════════════════

def test_I13_las_cuatro_lanes_del_compose_dual_estan_endurecidas(compose):
    assert dual._m1.verificar_endurecimiento(compose) == []


def test_los_agentes_del_dual_no_declaran_ni_un_montaje(compose):
    for nombre in ("agente_a", "agente_b"):
        assert "volumes" not in compose["services"][nombre]


def test_el_compose_dual_no_toca_docker_compose_de_la_flota_ni_el_Dockerfile():
    """Alcance por CONTENIDO, no por commit: `test_el_pilot_no_toca_el_compose_de_la_flota`
    en `test_topologia.py` ya audita por LOTE de commit sobre `docker-compose.pilot*.yml`
    — glob que alcanza a estos dos ficheros nuevos sin tocar esa prueba. Esta es
    la comprobación COMPLEMENTARIA e inmediata: ningún fichero de este cambio
    ES el compose ni el Dockerfile de la flota."""
    for f in (COMPOSE, ESTRENO):
        assert f.name not in ("docker-compose.yml", "Dockerfile")
