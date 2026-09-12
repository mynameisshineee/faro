"""Nada de lo que la flota escribe en la posición del tipo se descarta.

Medido el 2026-08-18 sobre el ledger piloto: **641 entradas (8,2 %) llevan un tipo
escrito en posición canónica que el parser TIRA**, porque `TIPOS` es un tuple
cerrado de 8 y `publicar.py` rechaza cualquier otro. `MEDIDO` (339), `MEASURED`
(105), `ADJUDICADO` (75), `VEREDICTO` (27)… La flota los escribe en el sitio
correcto y el lector los convierte en «sin tipo».

Eso rompe dos cosas a la vez: `lint` los cuenta como «no declaran nada» cuando
declaran de sobra, y cualquier instrumento que clasifique tráfico por tipo mide
sobre un corpus mutilado.

**Sólo preservación, no interpretación** (ruling del operador): se guarda el
lexema escrito en una posición COMPATIBLE CON LA GRAMÁTICA DE TIPO — que no es
«todo lo escrito», y el nombre lo dice: `bikeus→security ∧ Operador` también estaba
ahí y no es un tipo. El literal íntegro no se pierde: vive en `head`.
`canonical_kind` y `kind_registry_rev` sólo se pueblan cuando el registro Agent OS
explícito reconoce el lexema; `MEDIDO`, `ADJUDICADO` y el legacy `FYI` de este arnés
no pertenecen a él y siguen NULL. Sin la revisión por fila, cambiar la taxonomía
cambiaría en silencio las métricas históricas.
"""
from __future__ import annotations

from .conftest import construir, db_directa
from fastapi.testclient import TestClient

CABECERAS = (
    "### [cto-A → backend · FYI] canónica\ncuerpo\n"          # de los 8
    "### [cto-A → backend · MEDIDO] desconocida\ncuerpo\n"     # escrita y hoy tirada
    "### [cto-A → backend · ADJUDICADO] otra\ncuerpo\n"
    "### [cto-A → backend · Medido] con caja mixta\ncuerpo\n"  # el lexema, tal cual
    "### [cto-A → backend · REVIEW.V2] con punto\ncuerpo\n"    # fuera de cualquier vocabulario
    "### [cto-A → backend] sin tipo ninguno\ncuerpo\n"
    "### [cto-A → cto-A] titular con · trampa]\ncuerpo\n"   # NO es un tipo
)


def _monta(tmp_path, monkeypatch):
    s = construir(tmp_path, monkeypatch)
    (tmp_path / "DEMO-LEDGER.md").write_text(CABECERAS)
    with TestClient(s.app) as c:
        s.barrido()
        c.headers.update({"X-Llminbox-Token": "test-token"})
        yield_con = db_directa(s)
    return s, yield_con


def _filas(con):
    return {r["head"].split("]")[0].split()[-1] if "·" in r["head"] else "SIN":
            (r["tipo"], r["raw_tipo"], r["canonical_kind"], r["kind_registry_rev"])
            for r in con.execute("SELECT head,tipo,raw_tipo,canonical_kind,"
                                 "kind_registry_rev FROM entries WHERE ledger='demo-ledger'")}


def test_un_tipo_desconocido_se_conserva_literal(tmp_path, monkeypatch):
    """El caso de los 641: `MEDIDO` está escrito, luego `MEDIDO` se guarda.

    FALSADOR: hoy `raw_tipo` no existe; con la columna pero sin capturar, sale
    NULL y esta aserción cae — que es exactamente el descarte silencioso."""
    _, con = _monta(tmp_path, monkeypatch)
    f = _filas(con)
    assert f["MEDIDO"][1] == "MEDIDO", f
    assert f["ADJUDICADO"][1] == "ADJUDICADO", f


def test_no_se_interpreta_nada_todavia(tmp_path, monkeypatch):
    """El límite del ruling: preservar sí, interpretar no. `MEDIDO` NO se
    convierte en `MEASUREMENT` aquí, y `tipo` sigue sin inventarse.

    FALSADOR: si alguien cablea el registro canónico antes de tiempo, o mete el
    literal en `tipo`, esto se pone rojo — y con razón: `tipo` es el vocabulario
    que el sistema entiende, y ensancharlo por la puerta de atrás haría pasar por
    canónico lo que nadie ha aprobado."""
    _, con = _monta(tmp_path, monkeypatch)
    f = _filas(con)
    # SUPERSEDIDO el 2026-08-20: cuando se escribió esto, el contrato era «raw_tipo
    # captura, tipo NO interpreta». el operador adjudicó después CANON v1 con un único
    # alias —`MEDIDO → MEASURED`—, soportado por medición: 14 de los 15 autores de
    # MEDIDO escriben TAMBIÉN MEASURED (93%), o sea idioma del autor y no semántica
    # distinta. Lo que sigue intacto es lo que este test protege de verdad: el
    # LEXEMA no se destruye.
    assert f["MEDIDO"][0] == "MEASURED", "el alias adjudicado debe normalizar"
    assert f["MEDIDO"][2] is None and f["MEDIDO"][3] is None
    assert f["FYI"][2] is None, "el legacy FYI se coló en el registro Agent OS"


def test_el_canonico_tambien_deja_su_literal(tmp_path, monkeypatch):
    """`raw_tipo` es «lo que estaba escrito», sin excepciones. Que coincida con
    `tipo` en los 8 canónicos no lo hace redundante: hace que la partición del
    corpus se pueda calcular en SQL sin casos especiales."""
    _, con = _monta(tmp_path, monkeypatch)
    f = _filas(con)
    assert f["FYI"] [:2] == ("FYI", "FYI"), f


def test_sin_tipo_escrito_no_se_inventa_uno(tmp_path, monkeypatch):
    """CONTROL NEGATIVO, y es el que hace que los de arriba signifiquen algo: una
    entrada que NO declara nada tiene que salir con los cuatro campos vacíos. Sin
    esto, una implementación que rellenara `raw_tipo` con cualquier cosa pasaría
    los tres tests anteriores."""
    _, con = _monta(tmp_path, monkeypatch)
    assert _filas(con)["SIN"] == (None, None, None, None)


HUELLA_ESPERADA = "e7f6c011776e8db7"      # SCHEMA_V = 6


def test_la_huella_de_esquema_sigue_siendo_la_misma():
    """EL GUARDA DE VERDAD, y nació de que el anterior era teatro.

    Escribí primero un test que arrancaba el servicio dos veces contra la misma
    base y comprobaba que el cursor sobrevivía. Pasaba con el mutante «sube
    SCHEMA_V» puesto — porque sube en LAS DOS arrancadas, así que nunca hay
    transición vieja→nueva, que es exactamente lo que sí ocurre al desplegar.
    Medía «dos arranques de la misma versión», no «versión nueva sobre base
    existente». Cazado corriendo el mutante, no leyéndolo.

    Lo que decide de verdad si a los 20 agentes se les borra la posición de
    lectura es UN valor: `huella_esquema()`. Así que se fija.

    FALSADOR: subir `SCHEMA_V` o meter una columna en `SCHEMA` pone esto rojo.
    Si eso es lo que quieres, súbelo aquí a la vez — pero entonces estás
    decidiendo, con nombre y apellidos, tirar `cursors` en el próximo despliegue.
    """
    import servicio
    assert servicio.huella_esquema() == HUELLA_ESPERADA, (
        "la huella de esquema cambió: el próximo arranque TIRA `cursors` y los 20 "
        "agentes ven su bandeja llena otra vez. Si la columna es aditiva, va en "
        "COLUMNAS_ANADIDAS (no toca la huella). Si de verdad cambia la FORMA de "
        "una tabla, actualiza HUELLA_ESPERADA a propósito.")


def test_una_huella_distinta_si_borra_los_cursores(tmp_path, monkeypatch):
    """CONTROL del anterior: prueba que la huella guarda algo REAL. Con la base
    sellada a una huella vieja, el arranque tira `cursors` — comportamiento
    documentado y correcto. Sin este control, el pin de arriba podría estar
    fijando un número que no gobierna nada."""
    s = construir(tmp_path, monkeypatch)
    with TestClient(s.app) as c:
        s.barrido()
        c.headers.update({"X-Llminbox-Token": "test-token"})
        c.post("/inbox/backend/leido", json={"hasta": {"demo-ledger": 1}})
    con = db_directa(s)
    assert con.execute("SELECT COUNT(*) c FROM cursors").fetchone()["c"] > 0
    con.execute("INSERT OR REPLACE INTO meta VALUES ('schema_v', 'huella-vieja')")
    con.commit()

    s2 = construir(tmp_path, monkeypatch)
    with TestClient(s2.app):
        s2.barrido()
    assert db_directa(s2).execute(
        "SELECT COUNT(*) c FROM cursors").fetchone()["c"] == 0, (
        "la huella no gobierna nada: el pin de arriba no protege de nada")


def test_las_tres_columnas_existen_tras_reiniciar(tmp_path, monkeypatch):
    """Que la vía aditiva llegue de verdad a la tabla: `executescript(SCHEMA)`
    con `IF NOT EXISTS` NO añade columnas a una tabla que ya existe — es la
    cicatriz de `coste.maximo`. El ALTER tiene que correr en cada arranque."""
    s = construir(tmp_path, monkeypatch)
    with TestClient(s.app):
        s.barrido()
    s2 = construir(tmp_path, monkeypatch)
    with TestClient(s2.app):
        s2.barrido()
    cols = {r[1] for r in db_directa(s2).execute("PRAGMA table_info(entries)")}
    assert {"raw_tipo", "canonical_kind", "kind_registry_rev"} <= cols, cols


def test_lint_separa_no_declarar_de_declarar_algo_que_no_entiendo(tmp_path, monkeypatch):
    """Son dos deudas distintas y hoy se cuentan como una. «No declara nada» se
    arregla enseñando a escribir; «declara algo que no entiendo» se arregla
    ampliando el registro — o cerrando el camino por el que entró.

    FALSADOR: con el `tipo IS NULL` de antes, las dos filas caen en el mismo saco
    y el desglose no existe."""
    s, _con = _monta(tmp_path, monkeypatch)
    with TestClient(s.app) as c:
        c.headers.update({"X-Llminbox-Token": "test-token"})
        txt = c.get("/lint").text
    import re as _re
    assert "declara un tipo que no entiendo" in txt, txt
    linea = next(ln for ln in txt.splitlines() if "no entiendo" in ln)
    # Se ancla al NÚMERO, no a un `in` suelto: el porcentaje que va detrás en la
    # misma línea hacía pasar la aserción por la puerta de al lado.
    # 4 → 2 por CANON v1 (2026-08-20): `MEDIDO` y `Medido` pasan a entenderse vía el
    # alias adjudicado `MEDIDO → MEASURED` —el único con evidencia que resiste—, y
    # quedan los DOS que siguen sin canon: `ADJUDICADO` (pendiente, no rechazado:
    # no encontré contraejemplo pero tampoco un test que lo hallara) y `REVIEW.V2`.
    # La separación que este test protege —«no declarar» ≠ «declarar algo que no
    # entiendo»— es la misma; lo que cambia es cuántos caen en el segundo cubo.
    assert _re.search(r"no entiendo: 2\b", linea), linea   # ADJUDICADO · REVIEW.V2
    sin = next(ln for ln in txt.splitlines() if "sin tipo declarado" in ln)
    assert _re.search(r"sin tipo declarado: 2\b", sin), sin   # la sin tipo y la del titular trampa


def test_el_lexema_se_guarda_tal_cual_sin_normalizar(tmp_path, monkeypatch):
    """`raw_tipo` es el LEXEMA, no una versión canonizada de él. Normalizar a
    mayúsculas ya es interpretar: decide que `Medido` y `MEDIDO` son la misma
    palabra, que es precisamente lo que el registro canónico tendrá que
    adjudicar — con su revisión anotada — y no el troceador por su cuenta.

    Era una contradicción entre el código y este mismo fichero: los tests decían
    «se guarda el texto literal» y el capturador hacía `.upper()`.

    FALSADOR: devolver el `.upper()` convierte `Medido` en `MEDIDO` y esto cae."""
    _, con = _monta(tmp_path, monkeypatch)
    assert _filas(con)["Medido"][1] == "Medido"


def test_el_capturador_no_tiene_vocabulario_implicito(tmp_path, monkeypatch):
    """El capturador nació para que NADA de lo escrito se descarte, y su primera
    versión traía un vocabulario implícito propio: sólo letras, `_`, `/` y `-`.
    Un tipo con punto, dígito o cualquier otro Unicode volvía a perderse — el
    mismo fallo, una capa más abajo y más difícil de ver.

    FALSADOR: restringir la clase de caracteres deja `REVIEW.V2` en NULL."""
    _, con = _monta(tmp_path, monkeypatch)
    f = _filas(con)
    assert f["REVIEW.V2"][1] == "REVIEW.V2", f
    assert f["REVIEW.V2"][0] is None, "no es de los 8: no puede acabar en `tipo`"


def test_un_titular_con_punto_medio_no_es_una_declaracion_de_tipo(tmp_path, monkeypatch):
    """El tipo vive DENTRO del corchete de la cabecera. Un `· algo]` en la prosa
    del titular no declara nada, y tomarlo por tipo inventaría materia prima —
    justo lo contrario de lo que este cambio persigue.

    FALSADOR (el que me pilló): buscar en la línea entera en vez de en el corchete
    cerrado devuelve `trampa` como si fuera un tipo declarado. El mutante
    sobrevivió a la primera versión de este arreglo porque `inner` llega hasta
    fin de línea y el corte no existía."""
    _, con = _monta(tmp_path, monkeypatch)
    fila = next(r for r in con.execute(
        "SELECT head,tipo,raw_tipo FROM entries WHERE head LIKE '%trampa%'"))
    assert fila["raw_tipo"] is None, fila["head"]


def test_el_corpus_QUE_YA_EXISTE_tambien_se_rellena(tmp_path, monkeypatch):
    """EL FALSADOR QUE FALTABA, y sin él este cambio sería inerte en producción.

    La tupla del volcado sólo corre para eids NUEVOS. Una entrada ya indexada
    entra por la rama de «ya conocida», que hasta ahora no tocaba `raw_tipo`: las
    641 entradas del hallazgo llevan meses en la base, así que se habrían quedado
    NULL para siempre y `/lint` seguiría llamándolas «sin tipo». El arreglo,
    inerte justo sobre los datos para los que se hizo.

    Simula el despliegue: fila existente con `raw_tipo` a NULL, markdown SIN
    tocar, y una pasada de barrido. Tiene que quedar rellena.

    FALSADOR: quitar `raw_tipo` de la comparación de cambios deja el NULL puesto
    —el markdown no cambió, así que nada más dispara la escritura— y esto se pone
    rojo. Cazado por CodeRabbit."""
    s = construir(tmp_path, monkeypatch)
    (tmp_path / "DEMO-LEDGER.md").write_text(CABECERAS)
    with TestClient(s.app):
        s.barrido()
    con = db_directa(s)
    # El estado EXACTO de un despliegue sobre base existente: las entradas ya
    # indexadas por el código viejo, la columna recién creada por el ALTER (o sea
    # NULL entera) y sin sello de migración.
    con.execute("UPDATE entries SET raw_tipo=NULL")
    con.execute("DELETE FROM meta WHERE k='raw_tipo_v'")
    con.commit()
    assert con.execute("SELECT COUNT(*) c FROM entries "
                       "WHERE raw_tipo IS NOT NULL").fetchone()["c"] == 0

    s2 = construir(tmp_path, monkeypatch)     # el despliegue
    with TestClient(s2.app):                   # el arranque corre la migración
        pass
    rellenas = db_directa(s2).execute(
        "SELECT COUNT(*) c FROM entries WHERE ledger='demo-ledger' "
        "AND raw_tipo IS NOT NULL").fetchone()["c"]
    assert rellenas == 5, f"el corpus existente se quedó sin rellenar: {rellenas}"


def test_un_lexema_rancio_se_corrige_al_reparsear(tmp_path, monkeypatch):
    """La migración rellena una vez; ESTO mantiene el campo vivo después.

    Cubre un caso que la migración no puede: una entrada cuyo `raw_tipo`
    almacenado ya no coincide con lo que hoy se deriva de su cabecera — porque
    subió `RAW_TIPO_V`, o porque el fichero se reescribió. La rama de «entrada ya
    conocida» sólo escribe si algo cambió, así que `raw_tipo` tiene que estar
    DENTRO de esa comparación o se fosiliza en silencio (lo dice el propio
    comentario del reindex, y aun así se me pasó; lo señaló CodeRabbit).

    Se corrompe una fila que NO es la última, para que el único disparador
    posible de la escritura sea la diferencia de `raw_tipo`: la última cambia de
    `provisional` al crecer el fichero y pasaría por otro motivo.

    FALSADOR: quitar `raw_tipo` de la comparación deja `OBSOLETO` puesto."""
    s = construir(tmp_path, monkeypatch)
    (tmp_path / "DEMO-LEDGER.md").write_text(CABECERAS)
    with TestClient(s.app):
        s.barrido()
    con = db_directa(s)
    con.execute("UPDATE entries SET raw_tipo='OBSOLETO' WHERE head LIKE '%· FYI]%'")
    con.commit()

    with open(tmp_path / "DEMO-LEDGER.md", "a") as fh:
        fh.write("### [cto-A → backend · ACK] nueva al final\ncuerpo\n")
    s.barrido()                      # el fichero creció: se re-parsea entero

    fila = db_directa(s).execute(
        "SELECT raw_tipo FROM entries WHERE head LIKE '%· FYI]%'").fetchone()
    assert fila["raw_tipo"] == "FYI", f"se fosilizó: {fila['raw_tipo']}"


def test_una_revision_nueva_recalcula_todo_el_corpus(tmp_path, monkeypatch):
    """`RAW_TIPO_V` promete que subirla arregla lo ya escrito. Este test es lo que
    convierte esa promesa en contrato.

    Un `WHERE raw_tipo IS NULL` funciona para v0→v1 y MIENTE después: las filas
    que ya tienen valor no se vuelven a mirar. Y es donde más duele, porque los
    ledgers dormidos tampoco pasan por `reindex` —`barrido()` los salta si su
    tamaño y mtime no cambiaron—, así que un valor incorrecto se fosiliza para
    siempre.

    Se prueban las DOS direcciones, porque una revisión nueva no sólo rellena:

      · valor RANCIO      `OBSOLETO` → `FYI`
      · FALSO POSITIVO    `FANTASMA` → NULL   (algo que se tomó por tipo y no lo era)

    El ledger NO se toca: fichero intacto, mismo tamaño y mismo mtime. Si la
    migración dependiera de una re-indexación, aquí no pasaría nada.

    FALSADOR: volver al `WHERE raw_tipo IS NULL` deja las dos filas como estaban
    —ninguna es NULL— y las dos aserciones caen."""
    s = construir(tmp_path, monkeypatch)
    (tmp_path / "DEMO-LEDGER.md").write_text(CABECERAS)
    with TestClient(s.app):
        s.barrido()
    con = db_directa(s)
    con.execute("UPDATE entries SET raw_tipo='OBSOLETO' WHERE head LIKE '%· FYI]%'")
    con.execute("UPDATE entries SET raw_tipo='FANTASMA' WHERE head LIKE '%sin tipo ninguno%'")
    con.execute("INSERT OR REPLACE INTO meta VALUES ('raw_tipo_v', '1')")
    con.commit()

    s2 = construir(tmp_path, monkeypatch)
    monkeypatch.setattr(s2, "RAW_TIPO_V", "2")     # la revisión nueva
    with TestClient(s2.app):                        # arranque, sin tocar el fichero
        pass

    con2 = db_directa(s2)
    rancio = con2.execute(
        "SELECT raw_tipo FROM entries WHERE head LIKE '%· FYI]%'").fetchone()["raw_tipo"]
    falso = con2.execute(
        "SELECT raw_tipo FROM entries WHERE head LIKE '%sin tipo ninguno%'").fetchone()["raw_tipo"]
    sello = con2.execute("SELECT v FROM meta WHERE k='raw_tipo_v'").fetchone()["v"]
    assert rancio == "FYI", f"el lexema rancio no se recalculó: {rancio}"
    assert falso is None, f"el falso positivo sobrevivió a la revisión: {falso}"
    assert sello == "2", sello


# ── la otra cara del mismo filo: no descartar de menos, pero tampoco de más ──
# Medido sobre el corpus vivo: liberar el capturador de su vocabulario lo dejó
# capturando 38.848 valores, de los que **7.570 no eran tipos sino RUTAS o prosa**
# (`bikeus→security ∧ Operador`, `BARRIDO CERRADO security→bikeus`). El separador
# `·` se usa como separador general y el último campo no siempre es el tipo.
#
# La regla NO es una lista negra de flechas —eso deja pasar 3.855 rutas con
# espacios y sin flecha—: **un tipo es UN SOLO TOKEN**. Espacios y operadores de
# ruta son gramática de la cabecera, no parte de un nombre. No es vocabulario de
# palabras; es respetar la sintaxis del propio formato.

def _raw(head):
    import ledger_parse as lp
    return lp.raw_tipo_de(head)


def test_una_ruta_en_el_ultimo_campo_no_es_un_tipo():
    """El caso mayoritario del corpus real: `·` separando prosa y ruta.

    FALSADOR: sin la guarda de forma, `/lint` reportaría 7.570 «tipos declarados
    que no entiendo» que son rutas — envenenando exactamente el instrumento que
    la taxonomía tiene que alimentar."""
    assert _raw("### [S-15 · BARRIDO CERRADO security→bikeus ∧ Operador] x") is None
    assert _raw("### [S-17 · nota corta security→marketing ∧ bikeus] x") is None


def test_una_ruta_sin_espacios_tampoco():
    """La lista negra de flechas y la regla de token se solapan aquí, y por eso
    se aplican las dos: `bikeus→security` es un solo token y sigue siendo ruta."""
    assert _raw("### [algo · bikeus→security] x") is None


def test_si_el_ultimo_campo_no_vale_se_cae_a_la_etiqueta_del_frente():
    """No se devuelve None a la ligera: si la cabecera SÍ declara tipo al frente,
    ése es el bueno. Es el caso real `### [DONE hueco … · bikeus→security ∧ …]`.

    FALSADOR: cortar en seco al rechazar el último campo perdería el `DONE` que
    la entrada sí declaraba — descartar de más otra vez, por el otro lado."""
    assert _raw("### [DONE hueco npm audit · bikeus→security ∧ Operador] x") == "DONE"


def test_el_token_con_punto_sigue_valiendo():
    """CONTROL de que la guarda no ha reintroducido un vocabulario: `REVIEW.V2`
    no tiene espacios ni operadores, luego pasa."""
    assert _raw("### [cto-A → backend · REVIEW.V2] x") == "REVIEW.V2"
    assert _raw("### [cto-A → backend · Medido] x") == "Medido"


def test_prosa_sin_flecha_tampoco_es_un_tipo():
    """EL FALSADOR QUE FALTABA, y lo delató un mutante que sobrevivía: con sólo
    una lista negra de flechas, `S-13 APLICADOS Y VERIFICADOS E2E` pasaría como
    tipo declarado. Son 3.855 casos en el corpus vivo — prosa con espacios y sin
    ninguna flecha.

    Por eso la regla es la FORMA (un solo token) y no una lista de símbolos: una
    lista negra sólo prohíbe lo que a alguien se le ocurrió enumerar."""
    assert _raw("### [S-12 · S-13 APLICADOS Y VERIFICADOS E2E] x") is None
    assert _raw("### [algo · nota corta de cierre] x") is None


def test_la_migracion_que_falla_no_deja_nada_escrito(tmp_path, monkeypatch):
    """El camino de fallo tiene que ser «todo o nada», que es lo que su comentario
    afirma. Sin `rollback()`, un fallo a mitad del `executemany` dejaba la
    transacción ABIERTA y el siguiente paso del arranque (`migrar_alias_a_rol`)
    la commiteaba: filas recalculadas confirmadas SIN el sello de versión — o sea
    una base a medio migrar que se cree migrada. Cazado por CodeRabbit.

    FALSADOR: quitar el `rollback()` deja escrita la fila parcial y esto cae."""
    import sqlite3
    s = construir(tmp_path, monkeypatch)
    (tmp_path / "DEMO-LEDGER.md").write_text(CABECERAS)
    with TestClient(s.app):
        s.barrido()
    con = db_directa(s)
    con.execute("UPDATE entries SET raw_tipo=NULL")
    con.execute("DELETE FROM meta WHERE k='raw_tipo_v'")
    con.commit()

    class _Falla:
        """Escribe una fila y REVIENTA — la forma exacta del fallo a mitad."""
        def __init__(self, real):
            self._r = real
        def __getattr__(self, n):
            return getattr(self._r, n)
        def executemany(self, sql, seq):
            self._r.executemany(sql, list(seq)[:1])
            raise sqlite3.OperationalError("disk I/O error (simulado)")

    s.migrar_raw_tipo(_Falla(con))       # no debe propagar: es diagnóstico

    # EL COMMIT AJENO, que es donde está el daño. `migrar_alias_a_rol` corre justo
    # después en el arranque y commitea SOBRE LA MISMA CONEXIÓN: sin el rollback,
    # aquí es donde la fila parcial se confirma sin sello.
    #
    # (Mi primera versión de este test consultaba con una conexión NUEVA y pasaba
    # con el mutante puesto: una escritura sin confirmar no se ve desde fuera, así
    # que medía el aislamiento de SQLite y no la propiedad. Teatro, cazado por el
    # mutante que sobrevivía.)
    con.commit()

    ver = db_directa(s)
    assert ver.execute("SELECT COUNT(*) c FROM entries "
                       "WHERE raw_tipo IS NOT NULL").fetchone()["c"] == 0, \
        "un commit posterior confirmó la fila parcial de una migración que falló"
    assert ver.execute("SELECT COUNT(*) c FROM meta "
                       "WHERE k='raw_tipo_v'").fetchone()["c"] == 0, \
        "se selló como migrada una base que no lo está"


# ── la forma spoke sin corchetes (CodeRabbit, Major, sobre #9 ya desplegada) ──

SPOKE_CON_TIPO = "## 2026-07-06T11:45Z · transcribo → wiki-vault · PRODUCED"
SPOKE_RUTA     = "## 2026-07-06T11:45Z · transcribo → wiki-vault"


def test_la_cabecera_spoke_sin_corchetes_tambien_declara_su_tipo():
    """`H_ENTRY` acepta `## <ISO> · a → b · TIPO` — sin corchetes. `RAW_TIPO`
    exige `]`, así que esa cabecera VÁLIDA devolvía None, y ni `reindex()` ni la
    migración podían rellenarla: `/lint` la contaba como «sin tipo declarado»
    teniéndolo escrito en la posición canónica. Es exactamente el hueco que #9
    venía a cerrar, en la otra forma de cabecera.

    Medido sobre producción el 2026-08-19: 556 cabeceras spoke sin corchetes, de
    las que 66 llevan un tipo con forma válida y quedaron en NULL.

    FALSADOR: sin la rama spoke, la primera aserción da None.
    """
    assert _raw(SPOKE_CON_TIPO) == "PRODUCED"
    for h, esperado in (
            ("## 2026-07-06T13:06Z · wiki-vault → transcribo · INGESTED", "INGESTED"),
            ("## 2026-07-06T13:06Z · wiki-vault → transcribo · ACK", "ACK")):
        assert _raw(h) == esperado, h


def test_la_rama_spoke_no_captura_la_ruta_como_tipo():
    """CONTROL, y es el que importa: la misma medición que motivó `_es_token_de_tipo`
    mostró que soltar el capturador se traga 7.570 rutas y prosa. La rama nueva
    corre el MISMO guarda de forma, así que una cabecera spoke cuyo último campo
    es la ruta —no un tipo— sigue devolviendo None.

    FALSADOR: una rama spoke que devuelva el último campo sin pasar por
    `_es_token_de_tipo` captura `wiki-vault` como si fuera un tipo.
    """
    assert _raw(SPOKE_RUTA) is None
    assert _raw("## 2026-07-06T11:45Z · a → b · BARRIDO CERRADO x→y") is None
    assert _raw("## 2026-07-06T11:45Z · a → b · bikeus→security ∧ Operador") is None


def test_la_forma_con_corchetes_manda_sobre_la_spoke():
    """CONTROL de precedencia: si la cabecera trae corchetes, el tipo sale de
    DENTRO, no del último campo de la línea.

    La precedencia sale del ORDEN de las dos ramas, no de un guarda: el `mb is
    None` que escribí delante resultó no decidir nada (mutante superviviente) y
    está retirado.

    FALSADOR: poner la rama spoke ANTES de `RAW_TIPO` devuelve el campo de fuera.
    """
    assert _raw("### [cto → be · DONE] · cola de fuera") == "DONE"


def test_el_fallo_de_la_migracion_no_se_lleva_por_delante_la_columna(tmp_path, monkeypatch):
    """GUARDA DE MECANISMO, no arreglo de un fallo: hoy no lo hay, y la razón
    por la que no lo hay es una configuración que se puede cambiar sin querer.

    CodeRabbit lo dio por Major revisando #9: `_preparar_indice()` añade
    `entries.raw_tipo` con un `ALTER TABLE` y NO commitea antes de llamar a la
    migración, así que —decía— el `con.rollback()` del camino de fallo se lleva
    también el DDL, el arranque continúa, y `/lint` queda consultando una columna
    que ya no existe.

    Medido antes de tocar nada: NO ocurre. `sqlite3.connect(DB, timeout=30)` deja
    `isolation_level=''` (modo legacy), y ahí el módulo abre transacción implícita
    sólo ante DML — un `ALTER TABLE` corre en autocommit (`in_transaction` sigue
    en `False`) y ningún `rollback` posterior puede revertirlo. La premisa del
    hallazgo era que el DDL va dentro de la transacción; en esta conexión no va.

    Pero la protección es una PROPIEDAD DE LA CONFIGURACIÓN, no del código, y no
    estaba escrita en ninguna parte. Con `autocommit=False` (modo PEP 249) el
    hallazgo pasa a ser cierto. `isolation_level=None` NO reproduce este caso: en
    LEGACY_TRANSACTION_CONTROL desactiva las transacciones implícitas y deja
    SQLite en autocommit. Medido, los tres modos:

        isolation_level=''    in_transaction=False · la columna sobrevive
        isolation_level=None  in_transaction=False · la columna sobrevive
        autocommit=False      in_transaction=True  · la columna NO sobrevive

    Este test ata las dos mitades para que ese cambio se vea aquí y no en `/lint`
    en producción.

    FALSADOR: cambiar la conexión a `autocommit=False` pone en rojo la aserción
    del modo Y la de la columna.
    """
    import sqlite3
    s = construir(tmp_path, monkeypatch)
    (tmp_path / "DEMO-LEDGER.md").write_text(CABECERAS)
    with TestClient(s.app):
        s.barrido()

    con = db_directa(s)
    # El índice `i_raw_tipo` (#16) apunta a la columna, así que SQLite no deja
    # soltarla mientras exista. Se retira porque este test simula un estado
    # HISTÓRICO anterior a los dos, y el arranque lo vuelve a crear.
    con.execute("DROP INDEX IF EXISTS i_raw_tipo")
    con.execute("ALTER TABLE entries DROP COLUMN raw_tipo")
    con.execute("DELETE FROM meta WHERE k='raw_tipo_v'")
    con.commit()

    class _FallaSoloEnLaMigracion:
        """Deja pasar todo el arranque y revienta EXACTAMENTE en el `UPDATE` de
        `raw_tipo`, que es la forma real del fallo que se quiere aguantar."""
        def __init__(self, real):
            self._r = real
        def __getattr__(self, n):
            return getattr(self._r, n)
        def executemany(self, sql, seq):
            if "raw_tipo" in sql:
                raise sqlite3.OperationalError("disk I/O error (simulado)")
            return self._r.executemany(sql, seq)

    s._preparar_indice(_FallaSoloEnLaMigracion(con))
    con.commit()                       # el commit ajeno del paso siguiente

    cols = {r[1] for r in db_directa(s).execute("PRAGMA table_info(entries)")}
    assert "raw_tipo" in cols, \
        "el rollback de la migración se llevó el ALTER TABLE: /lint quedaría roto"

    # LA OTRA MITAD, y sin ella lo de arriba pasa sin explicar por qué: el DDL
    # sobrevive porque la conexión está en modo legacy. Atarlo aquí convierte un
    # cambio de configuración silencioso en un test rojo.
    #
    # SOBRE `s.db()`, LA FACTORÍA DE PRODUCCIÓN, no sobre `db_directa()`. Mi
    # primera versión midió el helper de conftest, que hace su PROPIO
    # `sqlite3.connect` — o sea que un cambio en `db()` lo habría dejado pasar
    # tan campante, justo lo único que este guarda promete cazar. Lo señaló
    # CodeRabbit. (Y el mutante que di por muerto no lo mató este guarda: lo
    # mataron otros 120 tests por daño colateral. Un acierto mal atribuido es
    # un acierto que no existe.)
    prod = s.db()
    assert prod.isolation_level == "", (
        "la conexión de PRODUCCIÓN dejó el modo legacy: ahora el ALTER TABLE SÍ "
        "entra en la transacción y el rollback de la migración se lo lleva — hay "
        "que sellar las columnas con un commit antes de llamar a migrar_raw_tipo()")
    prod.execute("ALTER TABLE entries ADD COLUMN _sonda_ddl TEXT")
    assert prod.in_transaction is False, "el DDL abrió transacción: ver arriba"
