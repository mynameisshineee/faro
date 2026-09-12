"""El gate de garantias mide contratos completos, no coincidencias sueltas."""

import importlib.util
from pathlib import Path


RAIZ = Path(__file__).resolve().parents[2]


def _modulo():
    spec = importlib.util.spec_from_file_location(
        "estado_garantias", RAIZ / "tools" / "estado-garantias.py")
    modulo = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(modulo)
    return modulo


def test_el_arbol_integrado_mide_las_seis_como_implemented():
    modulo = _modulo()
    medido = modulo.medir_garantias(modulo.cargar_fuentes())
    assert {gid: dato["estado"] for gid, dato in medido.items()} == {
        gid: "IMPLEMENTED" for gid in modulo.GARANTIAS
    }


def test_una_evidencia_no_basta_para_declarar_implemented():
    modulo = _modulo()
    fuentes = modulo.cargar_fuentes()
    _, evidencias = modulo.GARANTIAS["G5"]
    etiqueta, fichero, patron = evidencias[0]
    assert etiqueta and patron
    mutiladas = {nombre: "" for nombre in fuentes}
    mutiladas[fichero] = fuentes[fichero]
    assert modulo.medir_garantias(mutiladas)["G5"]["estado"] == "PARTIAL"


def test_sin_ninguna_evidencia_el_estado_es_designed():
    modulo = _modulo()
    fuentes = {nombre: "" for nombre in modulo.cargar_fuentes()}
    assert modulo.medir_garantias(fuentes)["G6"]["estado"] == "DESIGNED"


def test_partial_y_implemented_no_son_equivalentes():
    modulo = _modulo()
    medido = {
        gid: {"estado": "IMPLEMENTED", "nombre": "", "presentes": [], "ausentes": []}
        for gid in modulo.GARANTIAS
    }
    declarado = {gid: "IMPLEMENTED" for gid in modulo.GARANTIAS}
    declarado["G1"] = "PARTIAL"
    assert modulo.discrepancias(medido, declarado) == [
        "G1: page says PARTIAL, measured state is IMPLEMENTED"
    ]


def test_comentarios_y_docstrings_no_son_evidencia():
    modulo = _modulo()
    fuente = '''"""def project_next(self): pass"""
# class ActiveProjectorRunner: pass
def real():
    """def outbox_span(self): pass"""
    return 1
'''
    limpio = modulo.solo_codigo(fuente, True)
    assert "project_next" not in limpio
    assert "ActiveProjectorRunner" not in limpio
    assert "outbox_span" not in limpio
    assert "def real" in limpio
