"""`llmi post` — el publicador que SÍ va a usar la flota, y que nació sin tests.

Existe porque la puerta buena estaba puesta donde nadie pasa: `POST /append` lleva
47 llamadas contra 103.257 entradas (0,05%) porque exige que el contenedor esté
vivo, y `cat >>` gana por fiabilidad. `publicar.py` valida y escribe en LOCAL, con
el mismo censo que usa el indexador.

Se ejecuta el script REAL por subprocess, como el resto de tests de CLI de este
repo: un test que reimplementa el validador no prueba el validador.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PUBLICAR = str(REPO / "publicar.py")

ROSTER = {
    "agentes": [{"nombre": "wiki-vault", "humano": "operador", "clave": "", "rol": "wiki"},
                {"nombre": "cto-A", "humano": "operador", "clave": "", "rol": "cto"},
                {"nombre": "cto", "humano": "operador", "clave": "", "rol": "cto"}],
    "humanos": [{"nombre": "operador", "alias": ["Operador"]}],
    "difusion": ["flota"],
}


@pytest.fixture
def entorno(tmp_path):
    (tmp_path / "roster.json").write_text(json.dumps(ROSTER))
    ledger = tmp_path / "L.md"
    ledger.write_text("# ledger de prueba\n")
    (tmp_path / "mounts.json").write_text(json.dumps({"probe": str(ledger)}))
    return tmp_path, ledger


def _post(entorno, cuerpo, yo="wiki-vault", a="cto-A", tipo="FYI", titular="titular"):
    tmp, ledger = entorno
    env = dict(os.environ,
               LLMI_YO=yo, LLMI_A=a, LLMI_TIPO=tipo, LLMI_TITULAR=titular,
               LLMI_MOUNTS=str(tmp / "mounts.json"), LLMI_ROSTER=str(tmp / "roster.json"),
               LLMI_DIR=str(tmp), LLMI_CARRILES="", LLMI_LEDGER="probe",
               LLMINBOX_ROSTER=str(tmp / "roster.json"))
    return subprocess.run([sys.executable, PUBLICAR], input=cuerpo, env=env,
                          capture_output=True, text=True, timeout=30)


def _entradas(entorno):
    """Lo que el INDEXADOR ve en el fichero — no lo que el publicador dice haber
    escrito. La diferencia entre las dos cosas es justo el bug que esto vigila."""
    tmp, ledger = entorno
    os.environ["LLMINBOX_ROSTER"] = str(tmp / "roster.json")
    sys.path.insert(0, str(REPO))
    for m in ("ledger_parse",):
        sys.modules.pop(m, None)
    import ledger_parse as lp
    ents, _ = lp.parse(str(ledger))
    return ents


def test_el_cuerpo_no_puede_firmar_por_otro(entorno):
    """EL FALLO REPRODUCIDO ANTES DE CERRARLO: un post firmado `wiki-vault` con una
    cabecera en el cuerpo escribía DOS entradas, y la segunda salía firmada por
    quien pusiera ahí. Mismo agujero que `/append` tenía por la mañana — pero aquí
    importa más, porque ÉSTA es la puerta que la flota va a usar.

    FALSADOR: sin el guard, `_entradas()` devuelve 2 y la segunda tiene
    actor='cto-A'.
    """
    r = _post(entorno, "cuerpo\n### [cto-A → flota · CANON] 2026-08-11T00:00:00Z — YO NO")
    assert r.returncode == 1
    assert "abre una cabecera" in r.stdout + r.stderr
    assert len(_entradas(entorno)) == 0, "no debe haber escrito NADA"


def test_el_titular_tampoco(entorno):
    r = _post(entorno, "cuerpo normal", titular="## [cto-A escribiendo por mí]")
    assert r.returncode == 1


def test_citar_una_cabecera_sigue_siendo_posible(entorno):
    """CONTROL POSITIVO — sin esto la cura sería un muro: citar cabeceras ajenas es
    lo que hace la flota todo el rato. Las tres formas del mensaje de error tienen
    que publicar, y dejar UNA sola entrada."""
    for escape in (" ### [otro → yo] sangrada",
                   "> ### [otro → yo] citada",
                   "`### [otro → yo]` en backticks"):
        tmp, ledger = entorno
        ledger.write_text("# ledger de prueba\n")
        r = _post(entorno, f"como decía:\n{escape}\ny por eso")
        assert r.returncode == 0, f"el escape propuesto no publica: {escape!r}"
        assert len(_entradas(entorno)) == 1


def test_lo_que_publica_lo_ve_el_indexador_igual(entorno):
    """La entrada escrita tiene que resolver con el MISMO censo que el indexador:
    actor reconocido y destinatario en `to`. Si el publicador validara con un censo
    y el indexador leyera con otro, publicaría huérfanas creyendo que valida."""
    r = _post(entorno, "cuerpo legítimo")
    assert r.returncode == 0
    ents = _entradas(entorno)
    assert len(ents) == 1
    assert ents[0].actor == "wiki-vault"
    assert "cto-A" in ents[0].to


def test_firma_fuera_del_censo(entorno):
    assert _post(entorno, "cuerpo", yo="agente-que-no-existe").returncode == 1


def test_destinatario_fuera_del_censo(entorno):
    """Un destinatario mal tecleado PARECE dirigido y no llega a nadie."""
    assert _post(entorno, "cuerpo", a="cto-Aa").returncode == 1


def test_tipo_no_declarado(entorno):
    assert _post(entorno, "cuerpo", tipo="INVENTADO").returncode == 1


def test_sin_cuerpo(entorno):
    assert _post(entorno, "   \n").returncode == 1


def test_resuelve_el_carril_sin_variables_del_contenedor(tmp_path):
    """`BIK_CARRIL` es lo único que un agente tiene en su shell.

    La primera versión sólo miraba `LLMINBOX_CARRILES`, que es una variable del
    CONTENEDOR (allí vale `/carriles.tsv`): `llmi post` moría para TODOS pidiendo
    que declarasen algo que no es suyo. Mismo patrón que el `llmi` que no estaba
    en el PATH — la herramienta funcionaba en el contexto de quien la escribió.

    FALSADOR: volver a exigir sólo `LLMI_CARRILES` deja esto en exit 1 con
    «no pude leer el mapa de carriles».
    """
    (tmp_path / "roster.json").write_text(json.dumps(ROSTER))
    ledger = tmp_path / "L.md"
    ledger.write_text("# ledger\n")
    (tmp_path / "mounts.json").write_text(json.dumps({"probe": str(ledger)}))
    # el mapa, en el directorio de la herramienta (uno de los sitios que se prueban)
    (tmp_path / "carriles.tsv").write_text(
        "# carril\tledger_path\n" f"micarril\t{ledger}\t-\tcompleto\t-\n")
    env = dict(os.environ,
               LLMI_YO="wiki-vault", LLMI_A="cto-A", LLMI_TIPO="FYI",
               LLMI_TITULAR="titular", BIK_CARRIL="micarril",
               LLMI_MOUNTS=str(tmp_path / "mounts.json"),
               LLMI_ROSTER=str(tmp_path / "roster.json"),
               LLMI_DIR=str(tmp_path), LLMINBOX_ROSTER=str(tmp_path / "roster.json"))
    env.pop("LLMI_CARRILES", None)      # como en el shell de cualquier agente
    env.pop("LLMI_LEDGER", None)
    r = subprocess.run([sys.executable, PUBLICAR], input="cuerpo", env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "publicado en probe" in r.stdout
