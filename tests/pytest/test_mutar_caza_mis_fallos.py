"""El falsador de falsadores: cada test reproduce UN fallo real de la noche del 30/31-ago.

Si mañana alguien simplifica `tests/mutar.py` quitando una de estas guardas, el fallo que
esa guarda impedía vuelve — y vuelve con el mismo síntoma de siempre: un verde que no
significa nada. Por eso cada caso lleva el incidente que lo motivó.
"""
from __future__ import annotations
import sys
import pathlib
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from mutar import falsa, FalsadorInservible          # noqa: E402


@pytest.fixture
def proyecto(tmp_path):
    """Un mini-proyecto con un 'producto' y su test, ambos verdes."""
    (tmp_path / "prod.py").write_text("def suma(a, b):\n    return a + b\n")
    (tmp_path / "prueba.py").write_text(
        "import prod, sys\n"
        "sys.exit(0 if prod.suma(2, 2) == 4 else 1)\n")
    return tmp_path


CMD = [sys.executable, "prueba.py"]


def test_un_ancla_que_no_encaja_NO_pasa_por_verde(proyecto):
    """INCIDENTE 4 y 6: un `sed` sin encaje y un ancla rota por el escape del shell. Los
    dos mutaron CERO líneas y su verde se leyó como «el arnés no discrimina»."""
    with pytest.raises(FalsadorInservible, match="aparece 0 veces"):
        falsa("prod.py", [("no-existe-en-el-fichero", "x", "fantasma")], CMD,
              cwd=proyecto, verbose=False)


def test_un_ancla_ambigua_tampoco(proyecto):
    """Con 2+ apariciones mutas de más y no sabes cuál pesó: la medida no distingue."""
    (proyecto / "prod.py").write_text("def suma(a, b):\n    return a + b\n# return a + b\n")
    with pytest.raises(FalsadorInservible, match="aparece 2 veces"):
        falsa("prod.py", [("return a + b", "return 0", "ambiguo")], CMD,
              cwd=proyecto, verbose=False)


def test_una_base_ROJA_invalida_el_ejercicio(proyecto):
    """INCIDENTE 5, y la trampa que más veces me pasó: medir un mutante sobre algo que ya
    estaba en rojo. El mutante 'confirma' lo que la base ya decía."""
    (proyecto / "prueba.py").write_text("import sys; sys.exit(1)")
    with pytest.raises(FalsadorInservible, match="LÍNEA BASE"):
        falsa("prod.py", [("a + b", "a - b", "resta")], CMD, cwd=proyecto, verbose=False)


def test_una_mutacion_que_no_cambia_el_fichero_se_rechaza(proyecto):
    """Reemplazo idéntico al ancla: el fichero queda igual y el verde no vale."""
    with pytest.raises(FalsadorInservible, match="NO CAMBIÓ"):
        falsa("prod.py", [("a + b", "a + b", "no-op")], CMD, cwd=proyecto, verbose=False)


def test_mide_el_CODIGO_DE_SALIDA_y_no_lo_que_imprime(proyecto):
    """INCIDENTES 1, 2 y 3: leí `tail`, perdí PIPESTATUS y me creí un «todo verde» que
    hablaba de otra cosa. Aquí el producto IMPRIME «todo verde» mientras sale con 1."""
    (proyecto / "prueba.py").write_text(
        "import prod\n"
        "print('todo verde')\n"
        "import sys; sys.exit(0 if prod.suma(2, 2) == 4 else 1)\n")
    r = falsa("prod.py", [("a + b", "a - b", "suma rota")], CMD, cwd=proyecto, verbose=False)
    assert r["murieron"] == 1, "leyó la salida en vez del rc: el mutante pareció sobrevivir"
    assert not r["sobrevivieron"]


def test_un_superviviente_sin_justificar_se_reporta(proyecto):
    """Mutar un comentario no puede matar a nadie: el instrumento tiene que DECIRLO en vez
    de contarlo como cobertura."""
    (proyecto / "prod.py").write_text("def suma(a, b):\n    # nota\n    return a + b\n")
    r = falsa("prod.py", [("# nota", "# otra", "comentario")], CMD,
              cwd=proyecto, verbose=False)
    assert r["sobrevivieron"] == ["comentario"]


def test_un_superviviente_JUSTIFICADO_no_cuenta_como_fallo(proyecto):
    """La mitad que más se olvida (cto-64bis): declarar el mutante que sobrevive CON RAZÓN.
    Un mutation score sin esa lista no es una medida, es una nota."""
    (proyecto / "prod.py").write_text("def suma(a, b):\n    # nota\n    return a + b\n")
    r = falsa("prod.py", [("# nota", "# otra", "comentario", True)], CMD,
              cwd=proyecto, verbose=False)
    assert r["sobrevivieron"] == []


def test_el_fichero_queda_COMO_ESTABA(proyecto):
    """Un ⊖ que deja el árbol tocado convierte la siguiente medida en basura."""
    antes = (proyecto / "prod.py").read_bytes()
    falsa("prod.py", [("a + b", "a - b", "resta")], CMD, cwd=proyecto, verbose=False)
    assert (proyecto / "prod.py").read_bytes() == antes


# ── LA MUERTE VACUA · incidente del 2026-09-05 ───────────────────────────────────
# El canon llevaba un mutante que cerraba con `)` un `[` que abría. Daba `SyntaxError`,
# moría sin ejecutar una sola aserción, y el runner —`murio = rc != 0`— lo contaba como
# MUERTE. Acreditaba un falsador que nunca llegó a correr. Un mutante que no compila no
# es un ⊖ que detecta: es un ⊖ roto, y eso es INSERVIBLE, no un aprobado.

@pytest.fixture
def proyecto_rc(tmp_path):
    """Mini-proyecto cuyo test sale con el código que diga el producto: así se puede
    pedir un `rc` concreto sin simular nada."""
    (tmp_path / "prod.py").write_text("CODIGO = 0\n")
    (tmp_path / "prueba.py").write_text("import prod, sys\nsys.exit(prod.CODIGO)\n")
    return tmp_path


CMD_RC = [sys.executable, "prueba.py"]


def test_un_mutante_que_NO_COMPILA_es_INSERVIBLE_no_una_muerte(proyecto_rc):
    with pytest.raises(FalsadorInservible) as e:
        falsa("prod.py", [("CODIGO = 0", "CODIGO = (0", "no compila")],
              CMD_RC, cwd=proyecto_rc, verbose=False)
    assert "NO COMPILA" in str(e.value)


@pytest.mark.parametrize("rc,que_es", [
    (2, "interrumpido"), (3, "error interno"),
    (4, "mal uso"), (5, "no recogió NINGÚN test"),
])
def test_los_codigos_que_NO_son_un_test_que_falla_son_INSERVIBLES(proyecto_rc, rc, que_es):
    """El 5 es el peor de los cuatro: describe exactamente el estado que este arnés
    existe para impedir —cero tests corridos— y se contabilizaba como aprobado. Un
    fallo de recogida o de import entra por ahí."""
    with pytest.raises(FalsadorInservible) as e:
        falsa("prod.py", [("CODIGO = 0", f"CODIGO = {rc}", f"sale {rc}")],
              CMD_RC, cwd=proyecto_rc, verbose=False)
    assert f"rc={rc}" in str(e.value) and que_es in str(e.value)


def test_SOLO_el_rc_1_mata(proyecto_rc):
    """CONTROL POSITIVO. Si nada matara, los cuatro casos de arriba pasarían por el
    motivo contrario y el arnés no acreditaría a nadie."""
    r = falsa("prod.py", [("CODIGO = 0", "CODIGO = 1", "falla un test")],
              CMD_RC, cwd=proyecto_rc, verbose=False)
    assert r["murieron"] == 1 and r["sobrevivieron"] == []


def test_el_rc_0_sigue_siendo_SUPERVIVENCIA(proyecto_rc):
    """CONTROL POSITIVO del otro lado: un mutante que no cambia el veredicto tiene que
    seguir apareciendo como superviviente, no perderse entre los inservibles."""
    r = falsa("prod.py", [("CODIGO = 0", "CODIGO = 0 or 0", "no cambia nada")],
              CMD_RC, cwd=proyecto_rc, verbose=False)
    assert r["murieron"] == 0 and r["sobrevivieron"] == ["no cambia nada"]
