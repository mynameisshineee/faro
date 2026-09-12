"""EL GATE DEL GATE: tests del arnés de mutantes.

`mutantes_m2.py` es el instrumento que decide si los tests de M2 discriminan. Un
instrumento sin falsador certifica cualquier cosa — y los seis defectos que estos tests
fijan produjeron todos el MISMO síntoma: un `rc=0` y un JSON que se lee como evidencia.

⚠️ REGLA DE DISEÑO DE ESTE FICHERO: **nada aquí puede depender de que las agujas reales
casen con las fuentes reales.** Este fichero vive en `tests/search`, así que se ejecuta
DENTRO de cada copia mutada; un test que comprobase el censo real contra `search_store.py`
fallaría en los 68 mutantes por el motivo equivocado y los marcaría `MUERTO` a todos: el
falso verde perfecto. El censo real se valida en el PRE-VUELO del runner, que corre una
vez sobre la instantánea. Aquí sólo se prueban funciones puras y `main()` con un censo
de mentira.
"""

import json
import os
import pathlib

import pytest

from . import mutantes_m2 as mut


# ── (6) clasificación del `rc` ────────────────────────────────────────────────────
@pytest.mark.parametrize("rc, esperado", [
    (1, "MUERTO"),          # lo ÚNICO que mata: un test falló
    (0, "VIVO"),
    (2, "ARNES_ERROR"),     # interrumpido / error de recolección
    (3, "ARNES_ERROR"),     # error interno
    (4, "ARNES_ERROR"),     # error de uso
    (5, "ARNES_ERROR"),     # NINGÚN test recogido
    (7, "ARNES_ERROR"),     # código no documentado: fail-closed, no MUERTO
    (-9, "SENAL"),
    (-15, "SENAL"),
])
def test_rc_solo_uno_mata(rc, esperado):
    v, _ = mut.veredicto_por_rc(rc, "detalle")
    assert v == esperado


def test_rc_cinco_no_es_muerto():
    """El caso que más caro sale: el mutante rompe la recolección, pytest sale no-cero,
    NADIE aseveró nada y el arnés viejo lo firmaba como cazado."""
    assert mut.veredicto_por_rc(5, "no tests ran")[0] != "MUERTO"
    assert mut.veredicto_por_rc(2, "Interrupted: 3 errors")[0] != "MUERTO"


# ── (1) agujas ────────────────────────────────────────────────────────────────────
def test_aguja_duplicada_es_ambigua():
    fuente = "x = 1\ny = 1\n"
    assert mut.valida_aguja(fuente, "= 1", "= 2", "f.py")[0] == "AGUJA_AMBIGUA"


def test_aguja_ausente_es_vacua():
    assert mut.valida_aguja("a = 1\n", "no existe", "z", "f.py")[0] == "VACUO"


def test_aguja_que_no_cambia_nada_es_nula():
    assert mut.valida_aguja("a = 1\n", "a = 1", "a = 1", "f.py")[0] == "MUTACION_NULA"


def test_aguja_exacta_pasa():
    assert mut.valida_aguja("a = 1\nb = 2\n", "a = 1", "a = 9", "f.py") is None


def test_ids_del_censo_son_unicos():
    ids = mut.ids_mutantes()
    assert len(ids) == len(set(ids)), "dos mutantes con la misma clave se pisan"


def test_cada_mutante_real_tiene_matador_predeclarado():
    assert set(mut.MATADORES) == set(mut.ids_mutantes())
    assert all(nodeids and all(n.startswith("tests/search/") for n in nodeids)
               for nodeids in mut.MATADORES.values())


# ── (5) canonicidad ───────────────────────────────────────────────────────────────
def test_seleccion_parcial_no_es_canonica():
    censo = mut.MUTANTES
    parcial = list(censo[:2])
    ok, _ = mut.es_canonica(parcial, {m[0].split()[0]: {} for m in parcial})
    assert ok is False


def test_censo_completo_con_resultados_incompletos_no_es_canonica():
    censo = list(mut.MUTANTES)
    ok, _ = mut.es_canonica(censo, {censo[0][0].split()[0]: {}})
    assert ok is False


def test_censo_completo_y_resultados_completos_si_es_canonica():
    censo = list(mut.MUTANTES)
    ok, _ = mut.es_canonica(censo, {m[0].split()[0]: {} for m in censo})
    assert ok is True


# ── `main()` con un censo de MENTIRA ──────────────────────────────────────────────
@pytest.fixture
def arnes(tmp_path, monkeypatch):
    """Árbol falso + censo falso + `_corre_pytest` sustituido. Sin subprocesos."""
    raiz = tmp_path / "arbol"
    (raiz / "tests" / "search").mkdir(parents=True)
    (raiz / "objetivo.py").write_text("VALOR = 1\n", encoding="utf-8")
    (raiz / "tests" / "search" / "test_falso.py").write_text("def test_x():\n    pass\n",
                                                             encoding="utf-8")
    monkeypatch.setattr(mut, "RAIZ", raiz)
    monkeypatch.setattr(mut, "FUENTES", ("objetivo.py",))
    monkeypatch.setattr(mut, "SALIDA", tmp_path / "canon.json")
    monkeypatch.setattr(mut, "SALIDA_PARCIAL", tmp_path / "parcial.json")
    # El censo de mentira también declara sus expectativas ANTES de ejecutar nada.
    monkeypatch.setattr(mut, "MATADORES",
                        {f"F{i:02d}": (NODO,) for i in range(1, 20)})
    # No tocar los manejadores de señal del proceso de pytest que ejecuta ESTE test.
    monkeypatch.setattr(mut, "_instala_senales", lambda: None)
    monkeypatch.delenv("M2_MUTANTES", raising=False)
    return tmp_path


def _censo(n=2):
    return [(f"F{i:02d} falso {i}", "objetivo.py", "VALOR = 1", f"VALOR = {i + 2}")
            for i in range(1, n + 1)]


def _lee(p):
    return json.loads(p.read_text(encoding="utf-8"))


NODO = "tests/search/test_x.py::test_y"


def _tri(v):
    """Normaliza a la 3-tupla `(rc, detalle, nodeids)` que devuelve `_corre_pytest`.
    Un `rc=1` sin nodeids es un caso REAL —y ahora no cuenta como muerto—, así que el
    stub sólo pone el nodeid cuando el guion no lo dice."""
    if isinstance(v, BaseException) or len(v) == 3:
        return v
    rc, det = v
    return (rc, det, [NODO] if rc == 1 else [])


def _pytest_falso(*rcs):
    """Stub de `_corre_pytest`. La PRIMERA llamada es siempre la suite limpia sobre la
    instantánea; si esa sale no-cero el runner aborta con `SUJETO_EN_ROJO` y no llega a
    los mutantes. Por eso el primer valor va aparte y explícito.

    Las llamadas DIRIGIDAS (con `nodeids`) repiten el último veredicto: es lo que hace un
    mutante de verdad, que vuelve a morir por lo mismo."""
    guion = [_tri((0, "2 passed")), *[_tri(v) for v in rcs]]
    n = {"i": 0}
    ultimo = {"v": guion[0]}

    def stub(cwd, nodeids=None, salida_completa=None):
        if nodeids:
            return ultimo["v"]
        i = min(n["i"], len(guion) - 1)
        n["i"] += 1
        v = guion[i]
        if isinstance(v, BaseException):
            raise v
        ultimo["v"] = v
        return v
    return stub


def test_todo_muerto_y_censo_entero_da_go(arnes, monkeypatch):
    monkeypatch.setattr(mut, "MUTANTES", _censo(2))
    monkeypatch.setattr(mut, "_corre_pytest", _pytest_falso((1, "1 failed")))
    assert mut.main() == 0
    d = _lee(arnes / "canon.json")
    assert d["estado_corrida"] == "COMPLETA" and d["canonica"] is True
    assert d["resumen"] == {"MUERTO": 2}


def test_suite_limpia_en_rojo_no_corre_mutantes(arnes, monkeypatch, capsys):
    monkeypatch.setattr(mut, "MUTANTES", _censo(2))
    diagnostico = (f"FAILED {NODO}[parametro-sintetico] - "
                  "AssertionError: detalle-sintetico")

    def control_rojo(cwd, nodeids=None, salida_completa=None):
        if salida_completa is not None:
            salida_completa.extend([diagnostico, "1 failed"])
        return 1, "1 failed", [NODO]

    monkeypatch.setattr(mut, "_corre_pytest", control_rojo)
    # La PRIMERA llamada es la suite limpia: si sale no-cero, no hay mutantes que valgan.
    assert mut.main() != 0
    d = _lee(arnes / "canon.json")
    assert d["suite_limpia"]["rc"] == 1
    assert d["estado_corrida"] == "SUJETO_EN_ROJO"
    assert d["resultados"] == {}
    assert diagnostico in (arnes / "canon.json.suite-limpia.log").read_text()
    salida = capsys.readouterr().out
    assert f"FAILED {NODO} - AssertionError" in salida
    assert "parametro-sintetico" not in salida
    assert "detalle-sintetico" not in salida


def test_excepcion_del_arnes_es_error_terminal_y_no_en_curso(arnes, monkeypatch):
    """(2) Antes: `res` se quedaba sin asignar, el bucle reventaba y el JSON conservaba
    `EN_CURSO` — indistinguible de una corrida viva."""
    monkeypatch.setattr(mut, "MUTANTES", _censo(2))
    llamadas = {"n": 0}

    def revienta(cwd, nodeids=None, salida_completa=None):
        llamadas["n"] += 1
        if llamadas["n"] == 1:
            return 0, "2 passed", []      # suite limpia verde
        raise OSError("disco lleno")

    monkeypatch.setattr(mut, "_corre_pytest", revienta)
    assert mut.main() != 0
    d = _lee(arnes / "canon.json")
    assert d["estado_corrida"] != "EN_CURSO"
    # El bucle llegó al final, pero NADA se midió: `COMPLETA` está prohibido.
    assert d["estado_corrida"] == "ARNES_DEGRADADO"
    assert d["degradado"] == ["ERROR"]
    assert d["resumen"] == {"ERROR": 2}
    assert "OSError" in d["resultados"]["F01"]["detalle"]


def test_senal_deja_interrumpido_y_no_muerto(arnes, monkeypatch):
    """(2/señal) `INTERRUMPIDO` es un no-medido con nombre propio, nunca `MUERTO`."""
    monkeypatch.setattr(mut, "MUTANTES", _censo(3))
    llamadas = {"n": 0}

    def corta(cwd, nodeids=None, salida_completa=None):
        llamadas["n"] += 1
        if llamadas["n"] == 1:
            return 0, "2 passed", []
        if nodeids:
            return 1, "1 failed", [NODO]
        if llamadas["n"] == 2:
            return 1, "1 failed", [NODO]
        raise mut.Interrumpido("señal 15")

    monkeypatch.setattr(mut, "_corre_pytest", corta)
    assert mut.main() != 0
    d = _lee(arnes / "canon.json")
    assert d["estado_corrida"].startswith("INTERRUMPIDO")
    assert d["resumen"]["INTERRUMPIDO"] == 2 and d["resumen"]["MUERTO"] == 1


def test_corrida_parcial_va_a_otro_fichero_y_no_pasa_el_gate(arnes, monkeypatch):
    """(5) El falso GO documentado: dos mutantes elegidos, los dos muertos, `COMPLETA`
    y `rc=0`. Ahora: `PARTIAL_DIAGNOSTIC`, salida separada y no-cero."""
    monkeypatch.setattr(mut, "MUTANTES", _censo(4))
    monkeypatch.setattr(mut, "_corre_pytest", _pytest_falso((1, "1 failed")))
    monkeypatch.setenv("M2_MUTANTES", "F01,F02")
    assert mut.main() != 0
    assert not (arnes / "canon.json").exists(), "una parcial NO toca la evidencia canónica"
    d = _lee(arnes / "parcial.json")
    assert d["estado_corrida"] == "PARTIAL_DIAGNOSTIC"
    assert d["canonica"] is False and d["seleccion_parcial"] is True
    assert d["resumen"] == {"MUERTO": 2}


def test_aguja_duplicada_aborta_antes_de_correr_nada(arnes, monkeypatch):
    """(1) Pre-vuelo: con la aguja ambigua no se corre NI la suite limpia."""
    (arnes / "arbol" / "objetivo.py").write_text("VALOR = 1\nVALOR = 1\n", encoding="utf-8")
    monkeypatch.setattr(mut, "MUTANTES", _censo(1))
    corridas = {"n": 0}

    def cuenta(cwd, nodeids=None, salida_completa=None):
        corridas["n"] += 1
        return 0, "1 passed", []

    monkeypatch.setattr(mut, "_corre_pytest", cuenta)
    assert mut.main() != 0
    assert corridas["n"] == 0, "no se puede medir con el instrumento roto"
    d = _lee(arnes / "canon.json")
    assert d["estado_corrida"] == "AGUJAS_INVALIDAS"
    assert d["resultados"]["F01"]["veredicto"] == "AGUJA_AMBIGUA"


def test_rc_cinco_no_pasa_el_gate_end_to_end(arnes, monkeypatch):
    """(6) end-to-end: `rc=5` en un mutante ⇒ `ARNES_ERROR` y GO denegado."""
    monkeypatch.setattr(mut, "MUTANTES", _censo(2))
    llamadas = {"n": 0}

    def rc5(cwd, nodeids=None, salida_completa=None):
        llamadas["n"] += 1
        if llamadas["n"] == 1:
            return 0, "2 passed", []
        return 5, "no tests ran", []

    monkeypatch.setattr(mut, "_corre_pytest", rc5)
    assert mut.main() != 0
    d = _lee(arnes / "canon.json")
    assert d["resumen"] == {"ARNES_ERROR": 2}
    assert d["canonica"] is True, "el censo SÍ estaba entero: lo que falla es el veredicto"


def test_registra_la_deriva_del_arbol_vivo_sin_invalidar_la_congelada(arnes, monkeypatch):
    """(3) La afirmación precisa: al salir se rehashea la instantánea Y el vivo, y se
    dice cuál de las dos cosas prueba cada uno."""
    monkeypatch.setattr(mut, "MUTANTES", _censo(1))
    objetivo = arnes / "arbol" / "objetivo.py"

    def muta_el_vivo(cwd):
        objetivo.write_text("VALOR = 1\n# el vivo derivó durante la corrida\n",
                            encoding="utf-8")
        return 1, "1 failed", [NODO]

    llamadas = {"n": 0}

    def corre(cwd, nodeids=None, salida_completa=None):
        if nodeids:
            return 1, "1 failed", [NODO]
        llamadas["n"] += 1
        return (0, "1 passed", []) if llamadas["n"] == 1 else muta_el_vivo(cwd)

    monkeypatch.setattr(mut, "_corre_pytest", corre)
    rc = mut.main()
    d = _lee(arnes / "canon.json")
    assert d["vivo_cambio_durante_la_corrida"] is True
    assert d["manifiesto_vivo_final_digest"] != d["sujeto"]["manifiesto_digest"]
    # La instantánea NO se movió, así que la evidencia congelada sigue en pie.
    assert d["estado_corrida"] == "COMPLETA" and rc == 0


def test_json_durable_tambien_sincroniza_el_directorio(arnes, monkeypatch, tmp_path):
    """(4) `os.replace` publica la entrada de directorio; sin `fsync` de ESE directorio
    la promesa de durabilidad ante caída no se cumple."""
    vistos = []
    real_fsync = mut.os.fsync
    monkeypatch.setattr(mut.os, "fsync", lambda fd: (vistos.append(fd), real_fsync(fd))[1])
    destino = tmp_path / "d.json"
    mut._vuelca({"a": 1}, destino)
    assert len(vistos) == 2, "uno del fichero y otro del directorio"
    assert json.loads(destino.read_text()) == {"a": 1}


# ── Segunda ronda de auditoría: fallos del arnés que se leían como corrida buena ──
def test_completa_prohibida_con_cualquier_veredicto_no_medido(arnes, monkeypatch):
    """(C) `COMPLETA` es «los N se MIDIERON», no «el bucle llegó al final»."""
    monkeypatch.setattr(mut, "MUTANTES", _censo(2))
    monkeypatch.setattr(mut, "_corre_pytest", _pytest_falso((1, "1 failed"), (5, "no tests")))
    assert mut.main() != 0
    d = _lee(arnes / "canon.json")
    assert d["estado_corrida"] == "ARNES_DEGRADADO"
    assert d["degradado"] == ["ARNES_ERROR"] and d["resumen"]["MUERTO"] == 1


def test_cerrojo_niega_la_segunda_corrida_y_no_pisa_la_evidencia(arnes, monkeypatch):
    """(A) Dos corridas sobre el mismo fichero producen un JSON que no describe a
    ninguna. El modo de fallo ya se dio: un full obsoleto corriendo en paralelo."""
    import fcntl
    canon = arnes / "canon.json"
    canon.write_text('{"evidencia": "de la corrida buena"}', encoding="utf-8")
    lock = canon.with_name(canon.name + ".lock")
    otro = open(lock, "a+")
    fcntl.flock(otro.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        monkeypatch.setattr(mut, "MUTANTES", _censo(2))
        monkeypatch.setattr(mut, "_corre_pytest", _pytest_falso((1, "1 failed")))
        assert mut.main() == 1
        assert _lee(canon) == {"evidencia": "de la corrida buena"}, (
            "la corrida negada ni siquiera puede escribir su `EN_CURSO`")
    finally:
        fcntl.flock(otro.fileno(), fcntl.LOCK_UN)
        otro.close()


def test_el_temporal_de_vuelca_es_unico_por_escritura(tmp_path, monkeypatch):
    """(A) Un `.tmp` de nombre FIJO es una condición de carrera con nombre propio."""
    vistos = []
    real = mut.os.replace
    monkeypatch.setattr(mut.os, "replace", lambda a, b: (vistos.append(str(a)), real(a, b))[1])
    destino = tmp_path / "e.json"
    mut._vuelca({"n": 1}, destino)
    mut._vuelca({"n": 2}, destino)
    assert len(set(vistos)) == 2, f"el temporal se reutiliza: {vistos}"
    assert not any(v.endswith(".json.tmp") for v in vistos)
    assert _lee(destino) == {"n": 2}


def test_el_fsync_del_directorio_no_se_traga(tmp_path, monkeypatch):
    """(B) Si falla, la promesa de durabilidad queda incumplida y el JSON dice lo mismo
    que si se hubiera cumplido."""
    real = mut.os.fsync

    def falla_en_el_directorio(fd):
        if os.path.isdir(f"/dev/fd/{fd}") if os.path.exists(f"/dev/fd/{fd}") else False:
            raise OSError("fsync de directorio no soportado")
        try:
            st = os.fstat(fd)
        except OSError:
            return real(fd)
        import stat as _stat
        if _stat.S_ISDIR(st.st_mode):
            raise OSError("fsync de directorio no soportado")
        return real(fd)

    monkeypatch.setattr(mut.os, "fsync", falla_en_el_directorio)
    with pytest.raises(OSError):
        mut._vuelca({"a": 1}, tmp_path / "d.json")


def test_la_suite_limpia_no_corre_dentro_de_la_pristina(arnes, monkeypatch):
    """(D) Correrla dentro contaminaba el árbol del que nacen los mutantes."""
    monkeypatch.setattr(mut, "MUTANTES", _censo(2))
    cwds = []

    def espia(cwd, nodeids=None, salida_completa=None):
        if nodeids:
            return 1, "1 failed", [NODO]
        cwds.append(cwd)
        pathlib.Path(cwd, "basura_de_pytest.txt").write_text("x", encoding="utf-8")
        return (0, "2 passed", []) if len(cwds) == 1 else (1, "1 failed", [NODO])

    monkeypatch.setattr(mut, "_corre_pytest", espia)
    assert mut.main() == 0
    limpia, *mutantes = cwds
    assert limpia.endswith("_suite_limpia"), limpia
    assert not any(c.endswith("_sujeto_congelado") for c in cwds), (
        "nada se ejecuta DENTRO de la instantánea prístina")
    for c in mutantes:
        assert not pathlib.Path(c, "basura_de_pytest.txt").exists() or c == limpia, (
            "la basura de la suite limpia llegó al árbol de un mutante")


def test_mata_falla_cerrado_si_el_hijo_sigue_vivo(monkeypatch):
    """(E) `killpg` + `communicate` sin mirar el `poll()` era un cierre que se creía."""
    class Zombi:
        pid = 424242
        def communicate(self, timeout=None): return ("", "")
        def poll(self): return None

    monkeypatch.setattr(mut.os, "killpg", lambda *a: None)
    monkeypatch.setattr(mut.os, "getpgid", lambda pid: pid)
    with pytest.raises(mut.ArnesError):
        mut._mata(Zombi())


def test_mata_acepta_none_por_la_ventana_del_popen(monkeypatch):
    """(E) Si la señal llega ANTES de que `Popen` devuelva, no hay proceso que matar y
    el cierre no puede reventar por ello."""
    mut._mata(None)


def test_una_senal_durante_el_pytest_mata_al_hijo_y_propaga(monkeypatch):
    """(E) `BaseException` y no sólo `Interrumpido`: `KeyboardInterrupt` dejaba huérfano
    un pytest corriendo contra un árbol que el arnés iba a borrar."""
    matados = []

    class Proc:
        pid = 4242
        def communicate(self, timeout=None): raise KeyboardInterrupt()
        def poll(self): return 0

    monkeypatch.setattr(mut.subprocess, "Popen", lambda *a, **k: Proc())
    monkeypatch.setattr(mut, "_mata", lambda p: matados.append(p))
    with pytest.raises(KeyboardInterrupt):
        mut._corre_pytest("/tmp")
    assert len(matados) == 1 and isinstance(matados[0], Proc)


def test_el_manifiesto_incluye_config_y_dependencias(tmp_path):
    """(D) La suite no la define sólo el código: un `pytest.ini` o un pin distinto
    cambian LO QUE SE EJECUTA sin tocar una línea de fuente."""
    raiz = tmp_path / "r"
    (raiz / "tests" / "search").mkdir(parents=True)
    (raiz / "requirements-test.txt").write_text("pytest==9.1.*\n", encoding="utf-8")
    (raiz / "pytest.ini").write_text("[pytest]\naddopts = -x\n", encoding="utf-8")
    (raiz / "tests" / "search" / "test_x.py").write_text("def test_x(): pass\n",
                                                          encoding="utf-8")
    man = mut._manifiesto(raiz)
    assert "requirements-test.txt" in man and "pytest.ini" in man
    antes = mut._digest(man)
    (raiz / "requirements-test.txt").write_text("pytest==8.0.*\n", encoding="utf-8")
    assert mut._digest(mut._manifiesto(raiz)) != antes, (
        "cambiar el pin de pytest no movía el digest: dos corridas distintas firmaban "
        "como el mismo sujeto")


def test_el_manifiesto_incluye_servicio_y_benchmark(tmp_path):
    raiz = tmp_path / "r"
    (raiz / "tests" / "search").mkdir(parents=True)
    (raiz / "bench").mkdir()
    (raiz / "servicio.py").write_text("BORDE = 1\n", encoding="utf-8")
    (raiz / "bench" / "m2.py").write_text("BANCO = 1\n", encoding="utf-8")
    man = mut._manifiesto(raiz)
    assert "servicio.py" in man and "bench/m2.py" in man


def test_el_sujeto_declara_interprete_y_pytest(tmp_path):
    s = mut._sujeto(tmp_path, {})
    assert s["python"] and s["pytest"] and s["sqlite"]


# ── causalidad: un `rc=1` no es una CAZA ──────────────────────────────────────────
def test_nodeids_fallidos_extrae_los_nombres():
    salida = ("FAILED tests/search/test_a.py::test_uno - AssertionError: x\n"
              "ERROR tests/search/test_b.py::test_dos\n"
              "1 failed, 2 passed in 3.0s\n")
    assert mut._nodeids_fallidos(salida) == ["tests/search/test_a.py::test_uno",
                                             "tests/search/test_b.py::test_dos"]
    assert mut._nodeids_fallidos("2 passed in 1.0s") == []


def test_un_rc1_SIN_nodeids_no_cuenta_como_muerto(arnes, monkeypatch):
    """«Algo falló» no es «este mutante fue cazado»."""
    monkeypatch.setattr(mut, "MUTANTES", _censo(1))
    monkeypatch.setattr(mut, "_corre_pytest", _pytest_falso((1, "1 failed", [])))
    assert mut.main() != 0
    d = _lee(arnes / "canon.json")
    assert d["resultados"]["F01"]["veredicto"] == "ARNES_ERROR"
    assert d["estado_corrida"] == "ARNES_DEGRADADO"


def test_un_fallo_FUERA_de_tests_search_no_cuenta_como_muerto(arnes, monkeypatch):
    monkeypatch.setattr(mut, "MUTANTES", _censo(1))
    monkeypatch.setattr(mut, "_corre_pytest",
                        _pytest_falso((1, "1 failed", ["tests/journal/test_z.py::test_q"])))
    assert mut.main() != 0
    assert _lee(arnes / "canon.json")["resultados"]["F01"]["veredicto"] == "ARNES_ERROR"


def test_un_muerto_que_NO_se_reproduce_dirigido_no_cuenta(arnes, monkeypatch):
    """El caso que este control existe para cazar: un test INTERMITENTE ajeno tumba la
    suite entera, el `rc` sale 1 y el arnés lo firmaba como mutante muerto."""
    monkeypatch.setattr(mut, "MUTANTES", _censo(1))
    guion = {"n": 0}

    def flaky(cwd, nodeids=None, salida_completa=None):
        guion["n"] += 1
        if guion["n"] == 1:
            return 0, "2 passed", []                   # suite limpia
        if nodeids:
            return 0, "1 passed", []                   # dirigida: YA NO falla
        return 1, "1 failed", [NODO]                   # suite entera: falló «algo»

    monkeypatch.setattr(mut, "_corre_pytest", flaky)
    assert mut.main() != 0
    r = _lee(arnes / "canon.json")["resultados"]["F01"]
    assert r["veredicto"] == "MUERTO_NO_REPRODUCIBLE"
    assert r["dirigida"]["rc"] == 0


def test_un_muerto_reproducible_SI_cuenta_y_deja_su_causa(arnes, monkeypatch):
    monkeypatch.setattr(mut, "MUTANTES", _censo(1))
    monkeypatch.setattr(mut, "_corre_pytest", _pytest_falso((1, "1 failed", [NODO])))
    assert mut.main() == 0
    r = _lee(arnes / "canon.json")["resultados"]["F01"]
    assert r["veredicto"] == "MUERTO"
    assert r["nodeids_matadores"] == [NODO], "el resultado no nombra quién lo mató"
    assert "test_y" in r["detalle"]
