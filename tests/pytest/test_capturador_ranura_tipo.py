"""La ranura de tipo existe cuando la GRAMÁTICA la tiene, no cuando hay un `·`.

Defecto medido el 2026-08-20: `### [wiki-vault·64bis]` es `agente·carril` —dos
campos, sin ruta— y `raw_tipo_de()` tomaba el último campo igualmente, así que
devolvía el CARRIL como si fuera un tipo. 58 entradas en producción, y `64bis`
llegó a aparecer entre los tipos más frecuentes «fuera de canon», camino de que
alguien lo canonizara.

LA REGLA ES ESTRUCTURAL, y tenía que serlo: mirar si el candidato «parece un
carril» o «parece un agente» no sirve para decidir semántica, y está demostrado
en este mismo corpus — `HARNESS` parece un nombre de agente (lo es) y sin embargo
está legítimamente escrito en la posición de tipo. Un parche del tipo
`if candidato in CARRILES: return None` arreglaría el corpus de hoy y no la
sintaxis.

Las tres gramáticas, y sólo la del medio tiene ranura al final:

    [actor · carril]            → NO hay ranura de tipo
    [actor → destino · TIPO]    → sí la hay
    [TIPO actor → destino]      → tipo FRONTAL (otra rama, `ETIQUETAS`)

El discriminante es la FLECHA. Sin él la cabecera no está dirigiendo a
nadie, y el último campo es un calificador —carril, sello, nota—, no un tipo.

Medido sobre producción antes de tocar nada: de las capturas de esta rama, 10.512
llevan flecha y **272 no**. De esas 272: `64bis` 58, unos 35 timestamps
(`## [qa-biklabs · 2026-07-18T21:45:15Z]` capturaba la FECHA), nombres de agente y
fragmentos de prosa. El daño colateral aceptado y declarado: algún calificador que
sí parecía un tipo —`TRIAGE-PARKED`, `MÉTODO`— deja de extraerse, porque
estructuralmente no está en una ranura de tipo. El lexema NO se pierde: sigue
entero en `head`, que es de donde se deriva todo esto.
"""
from __future__ import annotations


def _raw(head):
    import ledger_parse as lp
    return lp.raw_tipo_de(head)


def test_agente_carril_sin_ruta_no_produce_el_carril_como_tipo():
    """EL CASO ROJO. `[wiki-vault·64bis]` no dirige a nadie: no hay ranura.

    FALSADOR: con la rama anterior devuelve `'64bis'` — un carril presentado como
    tipo, 58 veces en el corpus vivo.
    """
    assert _raw("### [wiki-vault·64bis] 2026-07-19T09:15:17Z — DONE") is None


def test_una_CONJUNCION_no_acredita_direccion():
    """`∧` y `&` juntan participantes; no dirigen a nadie.

    Mi primera versión de este arreglo usó `OPERADORES_RUTA`, que incluye las dos
    —porque `_es_token_de_tipo` las necesita para OTRA cosa: descartar candidatos
    que sean rutas—. Con eso, `[wiki-vault ∧ qa · 64bis]` volvía a dar `64bis`:
    maté una forma del defecto y dejé viva su variante, reutilizando una constante
    cuya semántica es más ancha que «flecha». Lo cazó el operador.

    FALSADOR: volver a `OPERADORES_RUTA` en `_dirigida` resucita las dos.
    """
    assert _raw("### [wiki-vault ∧ qa · 64bis] 2026-08-20T10:00Z — algo") is None
    assert _raw("### [wiki-vault & qa · 64bis] 2026-08-20T10:00Z — algo") is None


def test_una_conjuncion_JUNTO_a_una_flecha_si_dirige():
    """CONTROL: el caso mayoritario del corpus lleva las dos cosas —una flecha y
    varias conjunciones— y tiene que seguir extrayendo.

    FALSADOR: una regla que RECHACE la presencia de `∧` en vez de exigir flecha
    tira los miles de envíos multidestinatario.
    """
    assert _raw("### [db-mig → security ∧ qa ∧ cto · MEASURED] 2026-08-20T10:00Z — algo") == "MEASURED"


def test_FLECHAS_RUTA_es_exactamente_lo_que_consume_el_parser_de_rutas():
    """Las dos constantes tienen que decir lo mismo, o la cabecera tiene ranura de
    tipo para una función y no para la otra.

    Medido antes de estrecharla: con `←` dentro, `raw_tipo_de("[a ← b · 64bis]")`
    devolvía `64bis` mientras `_campos()` —que sólo conoce `→` y `->`— tomaba
    `64bis` como ACTOR. Dos lecturas de la misma cabecera.

    CodeRabbit propuso arreglarlo por el otro lado: ampliar `_campos` a `←`/`<-`.
    No, y no es conservadurismo: en `A ← B` la dirección se invierte, así que hay
    que decidir quién es actor y quién destinatario. Eso es semántica nueva, no
    dos caracteres más en un regex.

    FALSADOR: cualquier flecha en `FLECHAS_RUTA` que `FLECHA` no reconozca pone
    esto en rojo, en las dos direcciones.
    """
    import ledger_parse as lp
    reconocidas = {f for f in ("→", "->", "←", "<-") if lp.FLECHA.search(f"a {f} b")}
    assert set(lp.FLECHAS_RUTA) == reconocidas, (
        f"FLECHAS_RUTA={lp.FLECHAS_RUTA} no coincide con lo que consume "
        f"FLECHA={lp.FLECHA.pattern} → {reconocidas}")
    # y las inversas siguen descalificando un candidato a tipo, que es OTRA pregunta
    assert "←" in lp.OPERADORES_RUTA and "<-" in lp.OPERADORES_RUTA
    assert not lp._es_token_de_tipo("a←b")


def test_una_flecha_inversa_no_abre_ranura_de_tipo():
    """Consecuencia directa: sin soporte de `←`, esa cabecera no dirige.

    FALSADOR: meter `←` en FLECHAS_RUTA devuelve `64bis` — el defecto original,
    por la tercera puerta.
    """
    assert _raw("### [a ← b · 64bis] 2026-08-20T10:00Z — algo") is None


def test_con_ruta_el_ultimo_campo_SI_es_el_tipo():
    """CONTROL ①: la gramática dirigida conserva su ranura, y con un valor que
    ADEMÁS es nombre de agente — para que quede claro que la regla no mira el
    parecido del candidato.

    FALSADOR: una regla que exija que el tipo «no parezca un agente» rompe esto.
    """
    assert _raw("### [harness → sdet · HARNESS] 2026-08-19T09:35Z — algo") == "HARNESS"


def test_con_ruta_y_tipo_normal_sigue_igual():
    """CONTROL ②: el caso mayoritario, 10.512 capturas, no se mueve."""
    assert _raw("### [marketing → marketing · WAKE] 2026-08-11T16:43:05Z — algo") == "WAKE"


def test_el_tipo_FRONTAL_no_lo_toca_esta_regla():
    """CONTROL ③: `[TIPO actor → destino]` va por `ETIQUETAS`, otra rama.

    FALSADOR: gatear la rama equivocada tira los 21.574 HEARTBEAT y compañía —
    medido: 22.082 capturas vienen de cabeceras sin operador de ruta, y la
    inmensa mayoría son tipos FRONTALES que esta regla no debe rozar.
    """
    assert _raw("### [RESP marketing → bikeus] 2026-08-05T22:33:44Z — algo") == "RESP"
    assert _raw("### [HEARTBEAT bikeus] 2026-07-30T11:36:09Z — leí el ledger") == "HEARTBEAT"


def test_el_sello_de_fecha_deja_de_leerse_como_tipo():
    """El otro cuerpo de la basura: `[agente · <ISO>]` capturaba la FECHA.

    FALSADOR: sin la regla devuelve el timestamp como raw_tipo (~35 entradas).
    """
    assert _raw("## [qa-biklabs · 2026-07-18T21:45:15Z] Fechar el puntero") is None


def test_el_bump_de_PARSER_V_rederiva_lo_YA_indexado_sin_tocar_los_ficheros(
        tmp_path, monkeypatch):
    """Arreglar el parser NO arregla el pasado: hay que probar el bump.

    `barrido()` salta un ledger cuyo tamaño y mtime no han cambiado, así que las
    58 entradas que ya tienen `raw_tipo='64bis'` en la base no se volverían a
    mirar NUNCA — se fosilizarían con el valor falso mientras el código nuevo
    presume de estar arreglado. Es el mismo defecto que ya costó una corrección en
    la migración de `raw_tipo` (#9): el arreglo inerte sobre lo existente.

    Se prueba la PROPIEDAD OBSERVABLE, no la mecánica interna: una base sellada
    como `parser_v=9` con el valor falso guardado, el fichero SIN tocar, y tras el
    arranque con `PARSER_V=10` el valor tiene que estar corregido.

    FALSADOR: quitar el efecto del cambio de `parser_v` deja `'64bis'` vivo en la
    base y la aserción cae.
    """
    from fastapi.testclient import TestClient
    from .conftest import construir, db_directa

    # `construir()` SOBRESCRIBE el ledger de demo, así que la siembra va DESPUÉS.
    s_ = construir(tmp_path, monkeypatch)
    led = tmp_path / "DEMO-LEDGER.md"
    led.write_text("### [wiki-vault·64bis] 2026-07-19T09:15:17Z — DONE de la tanda\n\ncuerpo\n")
    with TestClient(s_.app):
        s_.barrido()

    con = db_directa(s_)
    fila = con.execute(
        "SELECT eid, ledger, head FROM entries WHERE head LIKE '%64bis%'").fetchone()
    assert fila, "la entrada sembrada no se indexó: el test no mide nada"
    # ESTADO HISTÓRICO: el valor falso guardado y el sello de la versión ANTERIOR.
    con.execute("UPDATE entries SET raw_tipo='64bis' WHERE eid=?", (fila["eid"],))
    con.execute("INSERT OR REPLACE INTO meta VALUES ('parser_v', '9')")
    con.commit()
    antes_mtime = led.stat().st_mtime
    cursores_antes = [dict(r) for r in con.execute("SELECT * FROM cursors ORDER BY agent, ledger")]

    # SEGUNDO ARRANQUE con el PARSER_V de hoy. `construir()` reescribe el ledger de
    # demo, así que se restaura BYTE A BYTE y se le devuelve su mtime: si el
    # fichero pareciera nuevo, `barrido()` lo re-leería por el camino fácil y el
    # test no probaría el bump, sino la detección de cambios que ya existía.
    contenido = led.read_bytes()
    s2 = construir(tmp_path, monkeypatch)
    led.write_bytes(contenido)
    import os
    os.utime(led, (antes_mtime, antes_mtime))
    with TestClient(s2.app):
        s2.barrido()

    assert led.stat().st_mtime == antes_mtime, "el fichero se movió: mediría el camino fácil"
    ver = db_directa(s2)
    e = ver.execute("SELECT raw_tipo FROM entries WHERE eid=?", (fila["eid"],)).fetchone()
    assert e is not None, "la entrada desapareció: rederivar no puede perder filas"
    assert e["raw_tipo"] is None, f"el valor falso sobrevivió al bump: {e['raw_tipo']!r}"
    assert ver.execute("SELECT v FROM meta WHERE k='parser_v'").fetchone()["v"] == str(
        __import__("ledger_parse").PARSER_V)
    assert [dict(r) for r in ver.execute(
        "SELECT * FROM cursors ORDER BY agent, ledger")] == cursores_antes, \
        "la rederivación movió cursores"
