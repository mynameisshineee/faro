"""`tipo` se deriva del LEXEMA, no de buscar palabras sueltas en la cabecera.

Hasta ahora: `tipo = next((t for t in TIPOS if t in head), None)` — subcadena
sobre el head ENTERO. Con eso, una cabecera que MENCIONA «FYI» o «ACK» en su
prosa recibía ese tipo, y la columna quedó inflada: medido sobre el corpus vivo,
`tipo` contiene 13 valores fuera de `TIPOS` en 22.882 entradas.

Y añadir los canónicos nuevos por esa vía habría sido peor: `RESP` aparece como
subcadena en 2.630 cabeceras que no son RESP («la RESP anterior…», «respondió»),
y `RULING` en 1.577 («el RULING del operador», citado en prosa).

EL CONTRATO, adjudicado el 2026-08-20:

    raw_tipo = el lexema exacto que escribió el autor · NUNCA se destruye
    tipo     = normalización canónica · conjunto pequeño y GOBERNADO

Esta PR introduce el mecanismo y es SÓLO ADITIVA sobre el histórico: rellena
donde `tipo` está vacío y el lexema es canonizable, y no toca ninguna fila que ya
tenga valor. El saneamiento del histórico inflado va en su propia PR, porque
mueve 31.077 filas y tiene consumidores vivos.
"""
from __future__ import annotations


def test_el_tipo_sale_del_lexema_no_de_la_prosa():
    """El caso que motivó todo: una cabecera que MENCIONA un tipo no lo declara.

    FALSADOR: con la búsqueda por subcadena, la primera da 'RESP' y la segunda
    'RULING' — 2.630 y 1.577 falsos positivos en el corpus real.
    """
    import ledger_parse as lp
    prosa = "### [marketing → cto · FINDING] 2026-08-20T10:00Z — la RESP anterior era falsa"
    assert lp.raw_tipo_de(prosa) == "FINDING"
    assert lp.canonical_tipo(lp.raw_tipo_de(prosa)) == "FINDING"
    otra = "### [cto → be · MEASURED] 2026-08-20T10:00Z — el RULING del operador dice otra cosa"
    assert lp.canonical_tipo(lp.raw_tipo_de(otra)) == "MEASURED"


def test_los_cuatro_canonicos_nuevos():
    """CANON v1 = los 8 de siempre + MEASURED · RESP · FINDING · RULING · DELIVERED.

    EL RECUENTO NO ES REDUNDANTE con la enumeración, y esta vez lo demostró: al añadir
    `DELIVERED` a `CANON_TIPOS` este test se puso ROJO por el `== 12`, y eso es el guarda
    haciendo su trabajo — ampliar el conjunto canónico obliga a DECLARARLO aquí, no basta
    con que el código compile. Un tipo nuevo es superficie de contrato: lo parsean otros.

    Así que la forma correcta de arreglar este rojo es la que se hizo —añadir el lexema a
    la enumeración Y subir el número— y NO subir el número a secas, que dejaría el guarda
    en pie con la lista mintiendo.

    `DELIVERED` (2026-09-03): el recibo de entrega del canon operativo. Sin él, quien
    entrega tenía que ENVOLVER el recibo dentro de otro tipo y citarlo con `> ` — dos
    entradas donde debía haber una. Misma clase que FINDING el 21-ago.
    """
    import ledger_parse as lp
    for t in ("PRODUCED", "INGESTED", "FYI", "REQUEST", "ACK", "HELD", "AMEND", "DELTA",
              "MEASURED", "RESP", "FINDING", "RULING", "DELIVERED"):
        assert lp.canonical_tipo(t) == t, t
    assert len(lp.CANON_TIPOS) == 13, sorted(lp.CANON_TIPOS)


def test_el_unico_alias_autorizado():
    """`MEDIDO → MEASURED`, y sólo ése.

    Es el único cuya equivalencia está soportada por medición y no por parecido
    lingüístico: 14 de los 15 autores de MEDIDO escriben TAMBIÉN MEASURED (93%).
    Si fueran semánticas distintas, los autores se especializarían.

    FALSADOR: añadir cualquier otro alias pone en rojo la segunda mitad.
    """
    import ledger_parse as lp
    assert lp.canonical_tipo("MEDIDO") == "MEASURED"
    assert lp.ALIASES == {"MEDIDO": "MEASURED"}, lp.ALIASES


def test_lo_NO_canonico_no_inventa_tipo():
    """Un lexema fuera del canon deja `tipo` en NULL. El literal vive en raw_tipo.

    Cada uno con su motivo adjudicado:
      HEARTBEAT  fuera del canon (y en Fase 4 pasa a ser señal de runtime)
      WAKE       97/97 de UN autor: tooling privado, no vocabulario de flota
      HARNESS    23/23 de UN autor: mismo patrón
      DECIDED    contraejemplo real — significa «acepto tu corrección», no adjudicar
      ADJUDICADO · VEREDICTO   pendientes, NO rechazados: sin test que falsara
                               la equivalencia, «no encontré contraejemplo» no es
                               «equivalencia probada»
      DONE · CLAIM   colisionan con estado de Job y con la operación de lease
      ASK · AVISO · STATUS   semánticamente mixtos, medidos: AVISO mezcla alertas
                             con RETRACTACIONES, ASK mezcla estado con escalación
      EJECUTADO  su canónico inglés (EXECUTED) tiene CERO usos en el corpus

    FALSADOR: canonizar cualquiera de ellos pone esto en rojo.
    """
    import ledger_parse as lp
    for t in ("HEARTBEAT", "WAKE", "HARNESS", "DECIDED", "DECISION", "VEREDICTO",
              "ADJUDICADO", "EJECUTADO", "EXECUTED", "DONE", "MSG", "CLAIM",
              "ASK", "AVISO", "STATUS", "AMEND+MEDIDO"):
        assert lp.canonical_tipo(t) is None, f"{t} no debe ser canónico todavía"


def test_sin_lexema_no_hay_tipo():
    """CONTROL: sin `raw_tipo` no se puede derivar nada."""
    import ledger_parse as lp
    assert lp.canonical_tipo(None) is None
    assert lp.canonical_tipo("") is None


# ── el consumidor oculto: la supresión de correo de los latidos ───────────────
#
# ⚠️ `LLMINBOX_ARROBA_DESDE` NO es decorativo en estos tests: sin él, `_campos` ni
# siquiera COSECHA la arroba (`if ARROBA_DESDE:` gatea el bloque entero), así que
# `to=[]`, `por_arroba=False` y el test pasa VACÍO — sin llegar nunca a la línea
# que pretende medir. Mi primera versión de esta suite cayó justo ahí: tres verdes
# que no tocaban el código bajo prueba. Producción lo tiene en `2026-08-08`.

def _latido(tmp_path, monkeypatch, cabecera):
    """Indexa una cabecera y devuelve (destinatarios, tipo, raw_tipo).

    `LLMINBOX_ARROBA_DESDE` va por ENV y no por `setattr`: `construir()` purga
    `ledger_parse` de `sys.modules` y lo reimporta, así que cualquier atributo
    parcheado a mano se descarta — pero la variable de entorno sí la lee el import
    nuevo. Producción la tiene en `2026-08-08`.
    """
    from fastapi.testclient import TestClient
    from .conftest import construir, db_directa
    s_ = construir(tmp_path, monkeypatch,
                   extra_env={"LLMINBOX_ARROBA_DESDE": "2026-01-01"})
    (tmp_path / "DEMO-LEDGER.md").write_text(cabecera + "\n\ncuerpo\n")
    with TestClient(s_.app):
        s_.barrido()
    con = db_directa(s_)
    f = con.execute("SELECT eid, tipo, raw_tipo FROM entries "
                    "WHERE head LIKE '%vig_a viva%'").fetchone()
    assert f, "la cabecera sembrada no se indexó"
    dest = [r[0] for r in con.execute("SELECT who FROM recipients WHERE eid=?", (f["eid"],))]
    return dest, f["tipo"], f["raw_tipo"]


def test_el_arnes_SI_cosecha_la_arroba(tmp_path, monkeypatch):
    """CONTROL PRIMERO, y es el que faltaba: sin esto los tres de abajo pasan
    vacíos. Una entrada NO-latido con `@backend` en su texto sí dirige correo.

    ⚠️ SIN FLECHA a propósito: el bloque de cosecha vive en el `else` de «la
    cabecera tiene ruta». Con `→` no se cosecha nada, y el control pasaría vacío
    igual que los tests que pretende proteger. Los latidos tampoco llevan flecha.

    FALSADOR: si `LLMINBOX_ARROBA_DESDE` no está puesta, o si se mete una flecha,
    esto da `[]` y delata que el arnés no reproduce producción.
    """
    dest, _, _ = _latido(
        tmp_path, monkeypatch,
        "### [FYI cto-A] 2026-08-20T10:00:00Z — vig_a viva, pregunta a @backend")
    assert "backend" in dest, f"el arnés no cosecha arroba: {dest}"


def test_un_latido_sigue_sin_dirigir_correo_aunque_HEARTBEAT_salga_del_canon(
        tmp_path, monkeypatch):
    """Dependencia OCULTA que destapó sacar HEARTBEAT del canon.

    `servicio.py` suprimía el correo cosechado de los latidos con
    `e.tipo == "HEARTBEAT"`. Con HEARTBEAT fuera de `CANON_TIPOS`, `tipo` pasa a
    ser NULL y esa comparación deja de casar: **los latidos empezarían a dirigir
    correo** a quien mencionen con `@` en su texto libre. Son 1.040 filas de
    destinatario fabricadas en toda la red — el problema que esa línea cerró.

    Que un cambio conceptual («HEARTBEAT ya no es tipo canónico») ponga en rojo un
    test de CORREO es la señal buena: el test no defiende implementación vieja,
    descubre un acoplamiento que nadie había inventariado.

    La supresión pasa a leer el LEXEMA, que es donde vive el protocolo legacy.

    FALSADOR: dejar la comparación contra `tipo` hace que aparezca 'backend'.
    """
    dest, tipo, raw = _latido(
        tmp_path, monkeypatch,
        "### [HEARTBEAT cto-A] 2026-08-20T10:00:00Z — vig_a viva (pid 1) — el cierre es de @backend")
    assert dest == [], f"el latido dirigió correo: {dest}"
    assert raw == "HEARTBEAT", raw          # el literal se conserva
    assert tipo is None, tipo               # y NO es canónico


def test_la_supresion_del_latido_no_depende_de_la_CAJA(tmp_path, monkeypatch):
    """`raw_tipo` conserva la caja literal, así que la comparación va en casefold.

    FALSADOR: una igualdad sensible a mayúsculas deja pasar `heartbeat` y
    `Heartbeat`, y esas dos sí dirigirían correo.
    """
    for grafia in ("heartbeat", "Heartbeat"):
        dest, tipo, raw = _latido(
            tmp_path, monkeypatch,
            f"### [{grafia} cto-A] 2026-08-20T10:00:00Z — vig_a viva (pid 1) — el cierre es de @backend")
        assert dest == [], f"{grafia} dirigió correo: {dest}"
        assert raw == grafia, raw           # literal, sin normalizar


def test_CONTROL_un_latido_con_FLECHA_explicita_SI_dirige(tmp_path, monkeypatch):
    """CONTROL, y acota el arreglo: lo que se descarta es el nombre COSECHADO del
    texto libre. Si alguien escribió una flecha a mano, va a propósito.

    FALSADOR: suprimir todo latido rompe el envío deliberado.
    """
    dest, _, raw = _latido(
        tmp_path, monkeypatch,
        "### [HEARTBEAT cto-A → backend] 2026-08-20T10:00:00Z — vig_a viva dirigida")
    assert "backend" in dest, dest


def test_el_backfill_es_ESTRICTAMENTE_aditivo(tmp_path, monkeypatch):
    """B rellena donde `tipo` está vacío. NO sanea lo que ya tiene valor.

    El histórico está inflado —21.543 HEARTBEAT y 4.943 FYI que el matcher por
    subcadena metió desde la PROSA— y corregirlo mueve 31.077 filas con
    consumidores vivos. Eso es otra PR, con su matriz old→new. Si B lo hiciera de
    paso, el radio de impacto se colaría escondido dentro de «añadir cuatro tipos
    al canon», que es exactamente lo que separamos.

    FALSADOR: un backfill que recalcule TODO deja el HEARTBEAT histórico en NULL y
    la primera aserción cae; uno que no rellene deja el MEDIDO sin normalizar y
    cae la segunda.
    """
    from fastapi.testclient import TestClient
    from .conftest import construir, db_directa

    s_ = construir(tmp_path, monkeypatch)
    (tmp_path / "DEMO-LEDGER.md").write_text(
        "### [cto-A → backend · MEDIDO] 2026-08-20T10:00:00Z — gana tipo\n\nuno\n"
        "### [HEARTBEAT cto-A] 2026-08-20T10:01:00Z — historico inflado\n\ndos\n"
        "### [cto-A → backend · MEASURED] 2026-08-20T10:02:00Z — hueco real\n\ntres\n")
    with TestClient(s_.app):
        s_.barrido()

    con = db_directa(s_)
    # ESTADO HISTÓRICO: el HEARTBEAT con `tipo` puesto (como lo dejó el matcher
    # viejo) y el MEDIDO sin él. Y el sello retirado, para que la migración corra.
    con.execute("UPDATE entries SET tipo='HEARTBEAT' WHERE raw_tipo='HEARTBEAT'")
    # EL CASO QUE DE VERDAD SEPARA B DE A, y sin él el guarda `WHERE tipo IS NULL`
    # es indistinguible de no tenerlo: una fila cuyo `tipo` viejo salió de la PROSA
    # (el matcher por subcadena veía «FYI» en el texto) mientras su LEXEMA sí es
    # canonizable. Recalcular todo la cambiaría FYI → MEASURED. Eso es saneamiento
    # —correcto, y de la PR siguiente—; B tiene que dejarla intacta.
    con.execute("UPDATE entries SET tipo='FYI' WHERE raw_tipo='MEDIDO'")
    con.execute("UPDATE entries SET tipo=NULL WHERE raw_tipo='MEASURED'")
    con.execute("DELETE FROM meta WHERE k='canon_v1_v'")
    con.commit()

    s_.migrar_canon_v1(con)

    ver = db_directa(s_)
    hb = ver.execute("SELECT tipo FROM entries WHERE raw_tipo='HEARTBEAT'").fetchone()
    md = ver.execute("SELECT tipo, raw_tipo FROM entries WHERE raw_tipo='MEDIDO'").fetchone()
    assert hb["tipo"] == "HEARTBEAT", \
        f"B saneó un histórico no canónico: eso es la PR siguiente ({hb['tipo']!r})"
    assert md["tipo"] == "FYI", \
        (f"B RECALCULÓ una fila que ya tenía tipo ({md['tipo']!r}): el radio de A se "
         f"coló dentro de B")
    assert md["raw_tipo"] == "MEDIDO", "el lexema se destruyó"

    # Y el hueco SÍ se rellena: control, o lo de arriba pasaría con un no-op.
    hueco = ver.execute("SELECT tipo FROM entries WHERE raw_tipo='MEASURED'").fetchone()
    if hueco:
        assert hueco["tipo"] == "MEASURED"


def test_REDERIVAR_no_sanea_tipo_ni_resucita_el_correo_del_latido(tmp_path, monkeypatch):
    """El segundo camino de escritura, que las dos garantías de B no cubrían.

    `migrar_canon_v1()` es aditiva, sí — pero `reindex()` tiene OTRA escritura para
    entradas ya conocidas cuando `rederivar` está activo, y hacía dos cosas que B
    promete no hacer:

        UPDATE entries SET actor=?, tipo=?     ← saneaba el tipo histórico
        for w in e.to: dest.append(...)        ← sin el gate del latido

    Y no es un camino teórico: lo dispara el gate de `roster_v`/`parser_v`, o sea
    cualquier despliegue con censo o troceador nuevos. Con él, una entrada
    histórica cambiaba de tipo ANTES de la PR que lo adjudica, y los latidos
    recuperaban el correo cosechado que acabábamos de quitarles — porque
    REDERIVAR hace `DELETE FROM recipients` antes y reconstruye.

    La regla de B, y es más simple que cualquier `viejo or nuevo`:

        eid existente + tipo poblado  ⇒  tipo BYTE-IDÉNTICO tras cualquier REDERIVAR

    #16/A cambiará esa política a conciencia. Aquí no.

    FALSADOR: reintroducir `tipo=e.tipo` en el UPDATE cae en la 2ª aserción;
    quitar el gate del latido cae en la 1ª.
    """
    from fastapi.testclient import TestClient
    from .conftest import construir, db_directa

    env = {"LLMINBOX_ARROBA_DESDE": "2026-01-01"}
    s_ = construir(tmp_path, monkeypatch, extra_env=env)
    led = tmp_path / "DEMO-LEDGER.md"
    led.write_text(
        "### [HEARTBEAT cto-A] 2026-08-20T10:00:00Z — vig_a viva, el cierre es de @backend\n\nuno\n"
        "### [cto-A → backend · MEDIDO] 2026-08-20T10:01:00Z — basura historica\n\ndos\n"
        "### [HEARTBEAT cto-A → backend] 2026-08-20T10:02:00Z — vig_a viva dirigida\n\ntres\n")
    with TestClient(s_.app):
        s_.barrido()

    con = db_directa(s_)
    # ESTADO HISTÓRICO: el HEARTBEAT con su tipo puesto, y el MEDIDO con un `FYI`
    # deliberadamente incorrecto — la basura exacta que #16/A vendrá a corregir.
    con.execute("UPDATE entries SET tipo='HEARTBEAT' WHERE raw_tipo='HEARTBEAT'")
    con.execute("UPDATE entries SET tipo='FYI' WHERE raw_tipo='MEDIDO'")
    con.execute("INSERT OR REPLACE INTO meta VALUES ('parser_v', '1')")   # fuerza REDERIVAR
    con.commit()
    cursores_antes = [dict(r) for r in con.execute("SELECT * FROM cursors ORDER BY agent, ledger")]
    contenido, mtime = led.read_bytes(), led.stat().st_mtime

    # SEGUNDO ARRANQUE: el fichero NO se toca; lo que dispara la re-derivación es
    # el gate de `parser_v`, que es el camino real en producción.
    import os
    s2 = construir(tmp_path, monkeypatch, extra_env=env)
    led.write_bytes(contenido)
    os.utime(led, (mtime, mtime))
    with TestClient(s2.app):
        s2.barrido()

    ver = db_directa(s2)
    def fila(raw):
        return ver.execute("SELECT eid, tipo, raw_tipo FROM entries WHERE raw_tipo=? "
                           "AND head LIKE ? LIMIT 1", (raw, "%vig_a viva, el%" if raw == "HEARTBEAT"
                                                       else "%basura%")).fetchone()
    hb, md = fila("HEARTBEAT"), fila("MEDIDO")
    assert hb and md, "las entradas sembradas no sobrevivieron a la re-derivación"

    cosechados = [r[0] for r in ver.execute("SELECT who FROM recipients WHERE eid=?", (hb["eid"],))]
    assert cosechados == [], f"REDERIVAR resucitó el correo cosechado del latido: {cosechados}"
    assert md["tipo"] == "FYI", f"REDERIVAR saneó un tipo histórico: eso es #16/A ({md['tipo']!r})"
    assert hb["tipo"] == "HEARTBEAT", f"REDERIVAR saneó el latido: {hb['tipo']!r}"
    assert md["raw_tipo"] == "MEDIDO", "el lexema se destruyó"

    # CONTROL POSITIVO: el latido con FLECHA explícita SÍ conserva destinatario —
    # `por_arroba=False` ahí—, o estaríamos suprimiendo todos los latidos.
    dirigido = ver.execute("SELECT eid FROM entries WHERE head LIKE '%dirigida%'").fetchone()
    assert "backend" in [r[0] for r in ver.execute(
        "SELECT who FROM recipients WHERE eid=?", (dirigido["eid"],))], "se suprimió un envío deliberado"

    assert [dict(r) for r in ver.execute(
        "SELECT * FROM cursors ORDER BY agent, ledger")] == cursores_antes, "movió cursores"


def test_el_disparador_del_token_conserva_su_evaluacion():
    """La nota que dice que el disparador se evaluó y dijo NO no puede desaparecer.

    El disparador de `auth()` existe porque «estaba declarado en un mensaje, que es donde
    las premisas se pudren». Su EVALUACIÓN es igual de perecedera: sin ella, el siguiente
    canon vuelve a pedir V8, alguien vuelve a derivar la respuesta desde cero, o la
    construye porque se la piden con fecha.

    Este test no juzga la respuesta —puede caducar, y la nota dice cómo—: fija que la
    pregunta y su fecha sigan al lado del disparador.
    """
    import pathlib
    t = pathlib.Path("servicio.py").read_text()
    i = t.index("def auth(")
    doc = t[i:i + 4000]
    assert "EL DISPARADOR SE EVALUÓ" in doc, (
        "se ha borrado la evaluación del disparador: el siguiente que venga a ampliar el "
        "alcance del token no sabrá que ya se preguntó")
    assert "(a)" in doc and "(b)" in doc, "la evaluación ya no dice contra qué se comprobó"
    assert "2026-09-03" in doc, "la evaluación no lleva fecha: no se puede saber si caducó"
