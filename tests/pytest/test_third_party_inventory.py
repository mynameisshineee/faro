"""La clausura Python tiene una sola autoridad y el inventario la verifica."""

import importlib.util
from pathlib import Path


RAIZ = Path(__file__).resolve().parents[2]


def _modulo():
    spec = importlib.util.spec_from_file_location(
        "third_party_inventory", RAIZ / "tools" / "third-party-inventory.py")
    modulo = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(modulo)
    return modulo


def test_requirements_in_exacto_coincide_con_el_lock_actual():
    modulo = _modulo()
    pines, fuente = modulo.deps_python()
    assert fuente == "requirements.lock"
    assert modulo.autoridad_python(pines) == []


def test_un_rango_en_la_autoridad_no_se_acepta(tmp_path, monkeypatch):
    modulo = _modulo()
    entrada = tmp_path / "requirements.in"
    entrada.write_text("fastapi==0.121.*\n", encoding="utf-8")
    monkeypatch.setattr(modulo, "PY_INPUT", str(entrada))
    assert "non-exact" in modulo.autoridad_python([("fastapi", "0.121.3")])[0]


def test_un_lock_que_diverge_de_la_autoridad_bloquea(tmp_path, monkeypatch):
    modulo = _modulo()
    entrada = tmp_path / "requirements.in"
    entrada.write_text("pydantic==2.13.4\n", encoding="utf-8")
    monkeypatch.setattr(modulo, "PY_INPUT", str(entrada))
    assert modulo.autoridad_python([("pydantic", "2.13.5")]) == [
        "`requirements.lock` does not contain authoritative pin `pydantic==2.13.4`."
    ]
