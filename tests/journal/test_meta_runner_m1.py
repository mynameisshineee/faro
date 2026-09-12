"""METATESTS DEL RUNNER. El arnés que juzga a los falsadores también se juzga.

El defecto que cierran es real y era MÍO: `mutantes_m1.py` prometía en su
docstring que exige que caigan los tests NOMBRADOS, y en el código bastaba
`returncode != 0`. Con eso, un mutante que ni compila —o un `rc=2` de uso, o un
timeout— entraba en el saco de «muertos». Un arnés que certifica cualquier rojo
es exactamente la máquina de acreditar cualquier cosa contra la que ese mismo
fichero avisa en su cabecera.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types

import pytest

RAIZ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location(
    "mutantes_m1", os.path.join(RAIZ, "tests", "mutantes_m1.py"))
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)


def _r(rc, stdout=""):
    return types.SimpleNamespace(returncode=rc, stdout=stdout, stderr="")


NODE = "tests/journal/test_x.py::test_uno"


def test_un_rc0_es_VIVO():
    assert R._veredicto(_r(0), [NODE]) == "VIVO"


def test_solo_rc1_CON_los_nodeids_declarados_mata():
    assert R._veredicto(_r(1, f"FAILED {NODE} - assert 1 == 2\n"), [NODE]) == "MUERTO"


def test_un_rojo_AJENO_no_mata():
    """Rojo, sí, pero de OTRO test: el mutante no ha demostrado nada sobre el
    falsador que se le atribuye."""
    salida = "FAILED tests/journal/test_otro.py::test_otro - boom\n"
    with pytest.raises(R.MutanteInservible) as ei:
        R._veredicto(_r(1, salida), [NODE])
    assert "NO exactamente por los tests declarados" in str(ei.value)


def test_un_rojo_declarado_MAS_otro_ajeno_tampoco_mata():
    otro = "tests/journal/test_otro.py::test_otro"
    salida = f"FAILED {NODE} - esperado\nFAILED {otro} - contaminación\n"
    with pytest.raises(R.MutanteInservible) as ei:
        R._veredicto(_r(1, salida), [NODE])
    assert otro in str(ei.value) and "ajenos" in str(ei.value)


def test_la_palabra_ERROR_en_un_mensaje_no_finge_error_de_recoleccion():
    salida = f"FAILED {NODE} - el texto decía ERROR pero el test sí corrió\n"
    assert R._veredicto(_r(1, salida), [NODE]) == "MUERTO"


@pytest.mark.parametrize("rc", [2, 3, 4, 5])
def test_los_rc_de_USO_e_INTERRUPCION_abortan(rc):
    """`rc=2` es interrupción, `4` es uso incorrecto… ninguno es «un test
    falló», y todos eran «muerto» en la versión anterior."""
    with pytest.raises(R.MutanteInservible):
        R._veredicto(_r(rc, f"FAILED {NODE}\n"), [NODE])


def test_un_ERROR_de_recoleccion_aborta_aunque_haya_FAILED():
    salida = f"ERROR tests/journal/test_x.py\nFAILED {NODE}\n"
    with pytest.raises(R.MutanteInservible) as ei:
        R._veredicto(_r(1, salida), [NODE])
    assert "recolección" in str(ei.value)


def test_un_mutante_que_NO_COMPILA_se_detecta_antes_de_correr():
    """SyntaxError es INSERVIBLE, no muerto: la suite se pondría roja por no
    poder importar, y ese rojo no dice nada del falsador."""
    with pytest.raises(SyntaxError):
        compile("def f(:\n  pass\n", "<mutante>", "exec")


def test_el_extractor_de_FAILED_no_se_traga_lineas_parecidas():
    salida = ("FAILED tests/a.py::test_1 - x\n"
              "  mención de FAILED tests/a.py::test_2 dentro de un mensaje\n"
              "FAILED tests/a.py::test_3\n")
    assert R._fallidos(salida) == {"tests/a.py::test_1", "tests/a.py::test_3"}


def test_el_manifiesto_declara_mata_para_TODOS_los_mutantes():
    for m in R.MANIFIESTO:
        assert m.get("mata"), f"{m['id']} sin tests declarados"
        assert all("::" in t for t in m["mata"]), f"{m['id']} sin nodeid completo"


def test_pytest_recibe_el_clon_como_cwd(monkeypatch, tmp_path):
    visto = {}

    class Hijo:
        args = [sys.executable, "-m", "pytest"]
        returncode = 0
        pid = 12345
        def communicate(self, timeout=None):
            return "1 passed", ""
        def poll(self):
            return self.returncode

    def popen(*args, **kwargs):
        visto["cwd"] = kwargs.get("cwd")
        return Hijo()

    monkeypatch.setattr(R.subprocess, "Popen", popen)
    clon = str(tmp_path / "clon")
    R._pytest([NODE], sys.executable, cwd=clon)
    assert visto["cwd"] == clon


# ── AISLAMIENTO DEL ÁRBOL VIVO ─────────────────────────────────────────────

def test_una_SENAL_a_mitad_de_mutacion_deja_coordination_py_PRISTINO():
    """TERM limpia el clon y conserva su rc; no hay live que restaurar."""
    import hashlib, signal, subprocess, sys, textwrap
    objetivo = os.path.join(RAIZ, "coordination.py")
    sha = lambda: hashlib.sha256(open(objetivo, "rb").read()).hexdigest()
    antes = sha()

    guion = textwrap.dedent(f'''
        import importlib.util, os, signal, sys, time
        spec = importlib.util.spec_from_file_location(
            "m", {os.path.join(RAIZ, "tests", "mutantes_m1.py")!r})
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        base, snapshot, _digest, _live = m._snapshot_congelado()
        m._instalar_senales(base)
        clon = m._clon(snapshot, os.path.join(base, "mutante"))
        print(base, flush=True)
        objetivo = os.path.join(clon, "coordination.py")
        original = open(objetivo, encoding="utf-8").read()
        m._escribir_atomico(objetivo, original + "\\n# MUTADO EN EL CLON\\n")
        os.kill(os.getpid(), signal.SIGTERM)                 # y nos matan
    ''')
    r = subprocess.run([sys.executable, "-c", guion], capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 128 + signal.SIGTERM, (
        f"SIGTERM no conservó su estado terminal (esperaba 143): {r.returncode}")
    assert sha() == antes, (
        f"la señal dejó el fichero MUTADO: rc={r.returncode} {r.stderr[-300:]}")
    base = r.stdout.strip().splitlines()[0]
    assert not os.path.exists(base), "SIGTERM no limpió su árbol desechable"


def test_mutar_un_clon_no_cambia_el_SHA_del_worktree_vivo():
    import hashlib, pathlib, shutil
    live = pathlib.Path(RAIZ, "coordination.py")
    antes = hashlib.sha256(live.read_bytes()).hexdigest()
    base, snapshot, digest, declarado = R._snapshot_congelado()
    try:
        assert digest == R._digest(R._manifiesto(pathlib.Path(snapshot)))
        assert declarado == antes
        clon = R._clon(snapshot, os.path.join(base, "mutante"))
        objetivo = pathlib.Path(clon, "coordination.py")
        original = objetivo.read_text(encoding="utf-8")
        m = R.MANIFIESTO[0]
        R._escribir_atomico(str(objetivo),
                            original.replace(m["ancla"], m["rota"], 1))
        assert hashlib.sha256(live.read_bytes()).hexdigest() == antes
        assert hashlib.sha256(objetivo.read_bytes()).hexdigest() != antes
    finally:
        R._permisos(base, escribible=True)
        shutil.rmtree(base, ignore_errors=True)


def test_SIGKILL_al_hijo_mutante_no_puede_alterar_el_worktree_vivo():
    """El caso que una restauración/atexit no puede capturar."""
    import hashlib, signal, shutil, subprocess, sys, textwrap
    live = os.path.join(RAIZ, "coordination.py")
    antes = hashlib.sha256(open(live, "rb").read()).hexdigest()
    guion = textwrap.dedent(f'''
        import importlib.util, os, signal
        spec = importlib.util.spec_from_file_location(
            "m", {os.path.join(RAIZ, "tests", "mutantes_m1.py")!r})
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        base, snapshot, _digest, _live = m._snapshot_congelado()
        clon = m._clon(snapshot, os.path.join(base, "mutante"))
        print(base, flush=True)
        objetivo = os.path.join(clon, "coordination.py")
        original = open(objetivo, encoding="utf-8").read()
        m._escribir_atomico(objetivo, original + "\\n# MUTADO Y SIGKILL\\n")
        os.kill(os.getpid(), signal.SIGKILL)
    ''')
    r = subprocess.run([sys.executable, "-c", guion], capture_output=True,
                       text=True, timeout=60)
    base = r.stdout.strip().splitlines()[0]
    try:
        assert r.returncode == -signal.SIGKILL
        assert hashlib.sha256(open(live, "rb").read()).hexdigest() == antes
    finally:
        R._permisos(base, escribible=True)
        shutil.rmtree(base, ignore_errors=True)


def test_el_manifiesto_no_admite_mutantes_MULTIPARTE():
    """Dos defensas redundantes no convierten un cambio doble en una mutación
    causal unitaria: si hace falta tocar dos sitios para poner algo rojo, lo que
    se está midiendo no es una garantía única. M12 se retiró por esto."""
    for m in R.MANIFIESTO:
        assert "partes" not in m, f"{m['id']} es multiparte"
        assert isinstance(m["ancla"], str) and m["ancla"], f"{m['id']} sin ancla"


def test_hay_NUEVE_mutantes_causales_y_ninguno_repite_ancla():
    anclas = [m["ancla"] for m in R.MANIFIESTO]
    assert len(R.MANIFIESTO) == 9, f"esperaba 9, hay {len(R.MANIFIESTO)}"
    assert len(set(anclas)) == 9, "dos mutantes comparten ancla"
    assert "M12" not in {m["id"] for m in R.MANIFIESTO}
