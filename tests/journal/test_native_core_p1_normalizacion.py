"""NO-GO de `@contratosbik` sobre el freeze `32c3646`: la atribución recursiva
**no NORMALIZA la clave**.

La recursión estaba bien y en el núcleo; lo que faltaba era el otro eje. Medido
por mi mano antes de tocar nada — `8` de `8` variantes ACEPTADAS **y
PERSISTIDAS** en `events.intent`:

    principalId · Role · carril · runtime-instance
    PRINCIPAL   · role_ · 'lane ' · Runtime_Instance

No hay elevación de autoridad —la atribución real sigue saliendo de la sesión—
pero queda ESCRITA en el registro autoritativo, y quien renderice `intent` puede
publicar un autor forjado. Es el mismo defecto que ya cerré para las claves
exactas, por la puerta de al lado: comparar `str(k) in RESERVADAS` mide la forma
que YO escribí, no la que el cliente manda.

🔑 UNA SOLA AUTORIDAD: la misma función normaliza la clave entrante **y** el
conjunto de reservadas. Dos listas —una «bonita» y otra normalizada— es cómo
`/append` acabó aceptando `8` de `12` tipos.
"""
from __future__ import annotations

import ast
import json
import pathlib
import sqlite3
import unicodedata

import pytest

import coordination as C
from ._arnes import INTENT, journal, sesion


def jr(tmp_path, **kw):
    return journal(tmp_path, **kw)


def _ev(j, s, meta, key="k"):
    return j.accept_event(s.token, idempotency_key=key,
                          intent={**INTENT, "meta": meta}, ledger="llminbox")


# ── ⊖ las que DEBEN rechazarse ──────────────────────────────────────────────
RECHAZAR = [
    # las 8 que reproduje sobre el freeze
    "principalId", "Role", "carril", "runtime-instance",
    "PRINCIPAL", "role_", "lane ", "Runtime_Instance",
    # las que añade el auditor
    "Principal", "CARRIL", "rol",
    # separadores y unicode, por cerrar la familia y no sus ejemplos
    "principal.id", "runtime instance", "RUNTIME__INSTANCE", "Lané",
    "  role  ", "principal-id", "runtimeInstance",
    # ── las SEIS RAÍCES que faltaban ────────────────────────────────────────
    # El conjunto tenía la forma COMPUESTA de cada concepto y no la palabra que
    # alguien escribe a mano. Cada una con al menos una variante tipográfica,
    # porque lo que se fija es la FAMILIA y no el ejemplo.
    # ⚠️ SIN variantes ACENTUADAS a propósito, y no por descuido: el plegado de
    # diacríticos es hoy una DIVERGENCIA DE CONTRATO ABIERTA —`@contratosbik`
    # publicó «NO plegar acentos» (`21:51:08Z`), el núcleo los pliega y lo
    # declara, y `@qa` la levantó el `22:33:30Z` sin adjudicarla. Ese eje YA
    # tiene su test propio (`test_los_DIACRITICOS_se_pliegan_...`); repetirlo
    # aquí sería el MISMO control otra vez, y ataría seis casos más a una regla
    # que puede cambiar de lado. Lo que estas filas fijan es la PALABRA que
    # faltaba, que es ortogonal a cómo se normalice.
    "agent", "Agent", "AGENT", "agent_", " agent ", "AGENT-",
    "agente", "AGENTE", "Agente",
    "attribution", "Attribution", "ATTRIBUTION", "attribution_",
    "runtime", "Runtime", "RUNTIME", "run-time", "run time",
    "credential", "Credential", "CREDENTIAL", "credential-",
    "credencial", "CREDENCIAL", "Credencial",
]

# ── ⊕ las que DEBEN seguir pasando: el control que impide cerrar de más ─────
#
# 🔻 `runtime` SALIÓ de aquí y se fue a `RECHAZAR`, y se dice en vez de borrarlo
# en silencio: quitar un caso de control para que un test se ponga verde es
# exactamente el movimiento que este fichero persigue. Estaba aquí bajo el
# juicio «sólo SE PARECE a `runtime_instance`», y ese juicio queda REVOCADO —
# `runtime` es la raíz del concepto, no un parecido. `instance` se queda: ésa sí
# es sólo la otra mitad de la palabra compuesta y no nombra nada del servidor.
PASAR = [
    "principal_count",      # ⊖ del auditor
    "nota",                 # ⊖ del auditor
    "roles", "lanes", "principality", "statement", "sourcecode",
    "carriles", "rolodex", "instance", "principales",
    # ⊕ de las SEIS raíces nuevas: vecinas que NO se pueden cerrar de rebote.
    # Sin esto, un `in`/prefijo en vez de igualdad pasaría los `RECHAZAR` y
    # rompería el uso legítimo sin que nadie lo viera.
    "agenda", "agentes", "agencia", "agentic",
    "attributions", "attributed", "atributo",
    "runtimems", "runtimes", "credentials", "credenciales", "acreditacion",
]


@pytest.mark.parametrize("clave", RECHAZAR)
def test_una_clave_reservada_DISFRAZADA_se_rechaza(tmp_path, clave):
    j = jr(tmp_path)
    s = sesion(j, "c-be", principal="p-be", role="be")
    with pytest.raises(C.AttributionRejected):
        _ev(j, s, {clave: "cto"})
    j.close()


@pytest.mark.parametrize("clave", PASAR)
def test_OMEGA_una_clave_que_solo_SE_PARECE_sigue_pasando(tmp_path, clave):
    """El control que impide que la normalización cierre de más. Sin él, una
    guarda demasiado ancha —`in` en vez de igualdad, o un prefijo— pasaría los
    `RECHAZAR` y rompería el uso legítimo sin que nadie lo viera."""
    j = jr(tmp_path)
    s = sesion(j, "c-be", principal="p-be", role="be")
    assert _ev(j, s, {clave: "valor"}, key=clave).event_id
    j.close()


def test_ninguna_variante_llega_a_PERSISTIRSE(tmp_path):
    """El daño no es el `202`: es que queda ESCRITO en `events.intent`. Se mide
    contra la tabla, no contra la respuesta."""
    j = jr(tmp_path)
    s = sesion(j, "c-be", principal="p-be", role="be")
    for i, clave in enumerate(RECHAZAR):
        with pytest.raises(C.AttributionRejected):
            _ev(j, s, {clave: "cto"}, key=f"k{i}")
    con = sqlite3.connect(str(tmp_path / "coordination.sqlite"))
    assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    j.close()


def test_la_normalizacion_es_UNA_y_es_publica(tmp_path):
    """El gateway tiene su propio `_alias` (medido por `@contratosbik`). Dos
    normalizaciones son dos verdades: la del núcleo se expone para que la de
    arriba desaparezca, no para que convivan."""
    assert C.Journal.clave_reservada("principalId") is True
    assert C.Journal.clave_reservada("carril") is True
    assert C.Journal.clave_reservada("principal_count") is False
    # y el conjunto de reservadas está normalizado por la MISMA función
    assert C.Journal._clave_norm("Runtime_Instance") == \
           C.Journal._clave_norm("runtime-instance") == "runtimeinstance"


def test_tambien_en_el_PAYLOAD_de_comando_y_en_TRACE(tmp_path):
    j = jr(tmp_path)
    s = sesion(j, "c-be", principal="p-be", role="be")
    with pytest.raises(C.AttributionRejected):
        j.submit_command(s.token, workstream_id="w", revision=1,
                         payload={"o": "x", "meta": {"principalId": "cto"}})
    with pytest.raises(C.AttributionRejected):
        j.accept_event(s.token, idempotency_key="k", intent=INTENT,
                       ledger="llminbox", trace={"s": {"CARRIL": "otro"}})
    j.close()


def test_el_conjunto_normalizado_se_DERIVA_y_no_puede_nacer_vacio(tmp_path):
    """Falsador del fail-open que tuve durante diez minutos.

    La primera versión rellenaba `_RESERVADAS_NORM` en una línea al final del
    módulo. Si esa línea no llega a ejecutarse, el conjunto queda VACÍO y
    `clave_reservada()` dice `False` a TODO **sin un solo error**: la guarda se
    apaga en silencio, que es peor que no tenerla.

    Ahora se deriva perezosamente de `_RESERVADAS`, así que no puede quedarse a
    medias — y esto lo fija: mismo tamaño, y cada reservada tiene su forma
    normalizada dentro.
    """
    norm = C.Journal._reservadas_norm()
    assert norm, "el conjunto normalizado está VACÍO: la guarda no filtra nada"
    assert len(norm) == len({C.Journal._clave_norm(k)
                             for k in C.Journal._RESERVADAS})
    for k in C.Journal._RESERVADAS:
        assert C.Journal._clave_norm(k) in norm, k
        assert C.Journal.clave_reservada(k) is True, k


def test_los_alias_apuntan_a_una_reservada_REAL(tmp_path):
    """Falsador del fail-open que tuve en mi propia cura.

    `_ALIAS_RESERVADAS` mapeaba `"actorid" -> "principal_id"` —la forma BONITA—
    mientras el conjunto de comparación vive en forma NORMALIZADA
    (`principalid`). El alias apuntaba a algo que no está ahí, así que `actorId`
    **colaba** aunque el alias existiera: un alias mal apuntado se lee como
    cobertura y no cubre nada.

    Esto no comprueba un caso: comprueba que **todos** los destinos del mapa son
    reservadas reales. Un alias nuevo mal escrito se pone rojo aquí.
    """
    norm = C.Journal._reservadas_norm()
    rotos = {k: v for k, v in C.Journal._ALIAS_RESERVADAS.items() if v not in norm}
    assert not rotos, (
        f"estos alias apuntan a algo que NO es una reservada normalizada: {rotos}. "
        f"El alias existe y no filtra: fail-open silencioso")


@pytest.mark.parametrize("clave", ["actorId", "actor_id", "ACTOR-ID", "áctor_id"])
def test_el_alias_de_actor_id_ya_no_cuela(tmp_path, clave):
    j = jr(tmp_path)
    s = sesion(j, "c-be", principal="p-be", role="be")
    with pytest.raises(C.AttributionRejected):
        _ev(j, s, {clave: "cto"})
    j.close()


@pytest.mark.parametrize("clave,esperado", [
    ("áctor", "actor"), ("actór", "actor"), ("Rôle", "role"),
    ("lané", "lane"), ("principál", "principal"), ("cañón", "canon"),
])
def test_los_DIACRITICOS_se_pliegan_y_NO_se_borran_como_separador(tmp_path, clave, esperado):
    """`@qa` midió una normalización ajena que borraba el acento COMO SEPARADOR
    (`áctor` -> `ctor`), que es lo que pasa si el `[^a-z0-9]` corre ANTES del
    NFKD. Aquí el orden es NFKD → quitar combining → quitar separadores, y esto
    lo fija: si alguien invierte los dos pasos, `áctor` deja de casar."""
    assert C.Journal._clave_norm(clave) == esperado


# ══ Población de HOMOGLIFOS de `@security` ══════════════════════════════════
# `MARK:security-respaldo-la-regla-cerrada-y-mi-regla-fuerte-tambien-era-debil`
# Midió que `[^a-z0-9]` A SECAS cuela `6` de `10` y respaldó `NFKC + isalnum`.
# Aquí `[^a-z0-9]` NO va a secas: lleva **NFKD delante**, y la K es plegado de
# COMPATIBILIDAD — pliega fullwidth, numerales romanos y bold matemático a ASCII
# igual que NFKC. Medido con su población: `0` de `10` cuelan.
#
# Se fija con SU población y no con la mía por la razón que él mismo escribió al
# corregirse: el ⊕ sale de la GRAMÁTICA, no del corpus que uno ya conoce.

HOMOGLIFOS = [
    ("ⅼane", "lane"),            # U+217C numeral romano
    ("ｐrincipal", "principal"),  # fullwidth
    ("ＡCTOR", "actor"),
    ("ｒｏｌ", "role"),            # fullwidth + alias de idioma
    ("𝐚𝐜𝐭𝐨𝐫", "actor"),          # bold matemático
    ("ａｃｔｏｒ", "actor"),
    ("lane​", "lane"),      # ancho cero
    ("lane\xa0", "lane"),        # NBSP
    ("run‐time_instance", "runtimeinstance"),   # guion unicode
]


@pytest.mark.parametrize("clave,esperado", HOMOGLIFOS)
def test_los_HOMOGLIFOS_de_security_no_cuelan(clave, esperado):
    assert C.Journal._clave_norm(clave) == esperado
    assert C.Journal.clave_reservada(clave) is True


@pytest.mark.parametrize("clave", ["fichero", "numero", "anno_lane", "strasse"])
def test_OMEGA_la_no_regresion_de_security(clave):
    """Sus cuatro de control: una normalización agresiva con Unicode podría
    fundir palabras legítimas. Ninguna se caza."""
    assert C.Journal.clave_reservada(clave) is False


# ══ ⚖️ LÍMITE REVOCADO — y la revocación se escribe, no se borra ═══════════
#
# Aquí vivía `test_LIMITE_DECLARADO_los_homoglifos_de_OTRO_SCRIPT_no_los_pliega_
# nadie`, que EXIGÍA que `рrincipal` (р cirílica) PASARA, con esta nota mía:
#
#     «Cubrirlo exige la tabla de confusables de UTS#39, que NO está en la
#      stdlib […] Es un límite del enfoque, no un bug de una implementación.»
#
# 🩸 **Era FALSO, y de la peor manera: convertí el límite de MI enfoque en una
# ley del mundo, y lo dejé fijado con un test que defendía el agujero.** No hace
# falta ninguna tabla de confusables. Hace falta decir qué alfabeto se ADMITE —
# lista blanca, seis líneas, stdlib. El auditor lo midió sobre `811f635`:
# `8` de `8` homoglifos cross-script ACEPTADOS **y PERSISTIDOS**.
#
# El test se REVOCA y se sustituye por su contrario. No se borra en silencio:
# un límite que se declaró en público se retira en público.

HOMOGLIFOS_CROSS_SCRIPT = [
    ("аgent", "а", 0x0430),          # CYRILLIC SMALL A
    ("agеnt", "е", 0x0435),          # CYRILLIC SMALL IE
    ("аgente", "а", 0x0430),
    ("attributіon", "і", 0x0456),    # CYRILLIC SMALL BYELORUSSIAN-UKRAINIAN I
    ("runtіme", "і", 0x0456),
    ("credentіal", "і", 0x0456),
    ("credеncial", "е", 0x0435),
    ("рrincipal", "р", 0x0440),      # CYRILLIC SMALL ER
]


@pytest.mark.parametrize("clave,ofensor,punto", HOMOGLIFOS_CROSS_SCRIPT)
def test_los_HOMOGLIFOS_CROSS_SCRIPT_ya_NO_pasan(clave, ofensor, punto):
    """⊖ El falsador del límite revocado.

    Se ancla al PUNTO DE CÓDIGO y no sólo al carácter: pegar cirílico en un
    fichero de test es exactamente donde una sustitución se pierde en un copia-y-
    pega y el test se vuelve ASCII sin que nadie lo note — y entonces mediría la
    guarda contra una clave que ya era latina.
    """
    assert ord(ofensor) == punto, "el carácter cirílico se perdió al editar"
    assert ofensor in clave
    assert C.Journal.clave_fuera_del_alfabeto(clave) == ofensor
    # y la razón de que hiciera falta: la normalización SE COMÍA la letra
    assert ofensor not in C.Journal._clave_norm(clave)


@pytest.mark.parametrize("clave,_o,_p", HOMOGLIFOS_CROSS_SCRIPT)
def test_los_homoglifos_cross_script_NO_LLEGAN_A_PERSISTIRSE(tmp_path, clave, _o, _p):
    """El daño nunca fue el `202`: era que quedaban ESCRITOS. Se mide contra la
    tabla, igual que el resto de este fichero."""
    j = jr(tmp_path)
    s = sesion(j, "c-be", principal="p-be", role="be")
    with pytest.raises(C.KeyCharsetRejected):
        _ev(j, s, {clave: "cto"}, key=f"x-{_p}")
    con = sqlite3.connect(str(tmp_path / "coordination.sqlite"))
    assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    j.close()


def test_el_alfabeto_se_mira_ANTES_de_normalizar_o_no_veria_nada():
    """El orden ES la guarda, y sin esto la cura se puede colocar mal y seguir
    verde en los demás tests: si el alfabeto se mirara DESPUÉS de `_clave_norm`,
    `аgent` ya sería `gent` —ASCII puro— y pasaría con toda la razón."""
    assert C.Journal.clave_fuera_del_alfabeto("аgent") == "а"
    assert C.Journal.clave_fuera_del_alfabeto(C.Journal._clave_norm("аgent")) is None


@pytest.mark.parametrize("clave", [
    # ⊕ acentos y eñes: DECOMPONEN a ASCII en NFKD, así que la lista blanca no
    # los toca. Es el control que impide que la cura cierre el castellano.
    "año", "señal", "cañón", "fichero", "numero", "strasse", "anno_lane",
    # ⊕ y las de ancho completo, que NFKD ya plegaba: siguen entrando por su
    # camino de siempre y NO por el nuevo.
    "ｎota", "ｆichero",
])
def test_OMEGA_la_lista_blanca_NO_cierra_las_claves_legitimas(clave):
    assert C.Journal.clave_fuera_del_alfabeto(clave) is None
    assert C.Journal.clave_reservada(clave) is False


def test_LIMITE_DECLARADO_de_la_lista_blanca_una_letra_no_latina_se_RECHAZA():
    """🔴 El coste de la lista blanca, escrito para que sea una DECISIÓN y no una
    sorpresa: una clave con una letra genuinamente no latina deja de admitirse.

    `ß` no decompone en NFKD, así que `straße` se rechaza aunque no se parezca a
    ninguna reservada. Se acepta a sabiendas — el espacio de nombres del cuerpo
    de este protocolo es ASCII, y rechazar una clave rara es mejor que aceptar
    una disfrazada. Si algún día hay que admitirla, esto se pone rojo y hay que
    venir a decidirlo, no a descubrirlo.
    """
    assert C.Journal.clave_fuera_del_alfabeto("straße") == "ß"


# ══ Las SEIS RAÍCES que el conjunto no tenía ════════════════════════════════
# `agent · agente · attribution · runtime · credential · credencial`
#
# El conjunto guardaba la forma COMPUESTA de cada concepto y no su raíz, que es
# justo la palabra que alguien escribe a mano:
#     `actor` sí / `agent` NO          — y `agent` es el nombre con el que el
#                                         gateway nombra al sujeto (`/inbox`,
#                                         `/claim`, tabla `cursors`)
#     `attribution_status` sí / `attribution` NO
#     `runtime_instance` sí / `runtime` NO
#     `capabilities` sí / `credential` NO — la credencial es la ENTRADA de la
#                                          que sale toda la identidad
#
# MEDIDO sobre el freeze `9793f8a` antes de tocar nada: las seis daban
# `clave_reservada(...) == False` y su cuerpo entraba y quedaba PERSISTIDO.

RAICES = ["agent", "agente", "attribution", "runtime", "credential", "credencial"]


@pytest.mark.parametrize("clave", RAICES)
def test_las_SEIS_RAICES_son_reservadas(clave):
    """El ⊖ mínimo, sobre la autoridad de forma y no sobre un caso."""
    assert C.Journal.clave_reservada(clave) is True


@pytest.mark.parametrize("clave", RAICES)
def test_las_seis_raices_se_rechazan_ANIDADAS_y_dentro_de_LISTAS(tmp_path, clave):
    """La recursión ya existía; lo que faltaba era la palabra. Se prueba en los
    dos sitios que la recursión cubre —dict anidado y lista— porque una
    reservada nueva que sólo se cazara en el primer nivel sería media guarda."""
    j = jr(tmp_path)
    s = sesion(j, "c-be", principal="p-be", role="be")
    with pytest.raises(C.AttributionRejected):
        _ev(j, s, {"meta": {"x": {clave: "cto"}}}, key=f"anidada-{clave}")
    with pytest.raises(C.AttributionRejected):
        _ev(j, s, {"lista": [{"y": {clave: "cto"}}]}, key=f"lista-{clave}")
    j.close()


@pytest.mark.parametrize("clave", RAICES)
def test_las_seis_raices_tampoco_entran_por_PAYLOAD_ni_por_TRACE(tmp_path, clave):
    """Las otras dos puertas del mismo filtro. `accept_event` no es la única:
    curar sólo `intent` dejaría el comando y la traza abiertos."""
    j = jr(tmp_path)
    s = sesion(j, "c-be", principal="p-be", role="be")
    with pytest.raises(C.AttributionRejected):
        j.submit_command(s.token, workstream_id="w", revision=1,
                         payload={"o": "x", "meta": {clave: "cto"}})
    with pytest.raises(C.AttributionRejected):
        j.accept_event(s.token, idempotency_key=f"trace-{clave}", intent=INTENT,
                       ledger="llminbox", trace={"s": {clave: "cto"}})
    j.close()


def test_los_alias_de_castellano_de_las_raices_apuntan_a_su_INGLESA(tmp_path):
    """`agente` y `credencial` no son variantes tipográficas: son OTRA palabra
    para lo mismo, igual que `carril`/`rol`. Y el destino tiene que ser la forma
    NORMALIZADA —`test_los_alias_apuntan_a_una_reservada_REAL` lo exige para
    todo el mapa—, así que aquí se fija además que las dos COLAPSAN a la misma
    clave que su inglesa: si alguien las metiera sueltas en `_RESERVADAS` en vez
    de como alias, esto seguiría verde y no habría que tocarlo; si las apuntara
    mal, se pone rojo en los dos sitios."""
    assert C.Journal._clave_norm("agente") == C.Journal._clave_norm("agent")
    assert C.Journal._clave_norm("credencial") == C.Journal._clave_norm("credential")


@pytest.mark.parametrize("clave", ["agentId", "agent_id", "AGENT-ID", "agéntid",
                                   "credentials", "credenciales", "runtimeMs"])
def test_LIMITE_DECLARADO_las_derivadas_de_las_raices_NO_se_cierran(clave):
    """🔴 El hueco que esta cura NO cierra, escrito para que nadie lo lea como
    cerrado.

    El encargo nombra SEIS términos y se implementan SEIS. `agentId` es el
    siguiente intento obvio de quien se coma un rechazo en `agent` —y su gemelo
    `actorId` SÍ está cubierto, por alias explícito (`actorid -> principalid`)—,
    así que la asimetría es real y es una DECISIÓN, no un olvido: cerrarla
    significa afirmar que `agent_id` es el `principal_id`, y esa equivalencia no
    la ha declarado nadie.

    Este test FIJA el límite: el día que se cierre se pone rojo y hay que venir
    aquí a borrarlo con la equivalencia escrita. Es como se declara un hueco sin
    que se olvide — el mismo patrón que el test de los homoglifos cirílicos.
    """
    assert C.Journal.clave_reservada(clave) is False, (
        f"`{clave}` ya se caza: borra este caso y escribe en `_ALIAS_RESERVADAS` "
        f"a qué reservada se declara equivalente")


# ══ 3ª RONDA · la lista blanca que NO era blanca ════════════════════════════
#
# La cura de la 2ª ronda era `c.isalnum() and c.lower() not in _ALFABETO`: o sea
# «rechazo lo ALFANUMÉRICO que no es mío». Sigue siendo enumerar LO AJENO, con
# nombre de lista blanca — y `isalnum()` es una pregunta ABIERTA que contesta la
# tabla de Unicode. Todo lo que NO es alfanumérico caía por el agujero y
# `_clave_norm` lo borraba como si fuera un separador. MEDIDO sobre `c2d57a2`:
# `5` de `5` ACEPTADOS **y PERSISTIDOS**, todos de categoría `So`.
#
# 🩸 Es el MISMO error que la ronda anterior, un nivel más arriba. Por eso la
# regla nueva no pregunta qué ES un carácter: pregunta si está EN LA LISTA.

SIMBOLOS_NO_ALFANUM = [
    ("ag℮nt", "℮", 0x212E, "So"),                       # ESTIMATED SYMBOL
    ("⍺gent", "⍺", 0x237A, "So"),                       # APL FUNCTIONAL SYMBOL ALPHA
    ("⍻rincipal", "⍻", 0x237B, "So"),                   # NOT CHECK MARK
    ("\U0001F170gent", "\U0001F170", 0x1F170, "So"),    # NEGATIVE SQUARED LATIN A
    ("ag\U0001F174nt", "\U0001F174", 0x1F174, "So"),    # NEGATIVE SQUARED LATIN E
]


@pytest.mark.parametrize("clave,ofensor,punto,categoria", SIMBOLOS_NO_ALFANUM)
def test_los_SIMBOLOS_NO_ALFANUMERICOS_ya_NO_pasan(clave, ofensor, punto, categoria):
    """⊖ Los cinco del auditor, anclados al PUNTO DE CÓDIGO y a la CATEGORÍA.

    La categoría se afirma porque es el corazón del defecto: `isalnum()` daba
    `False` para los cinco, así que la guarda anterior ni los miraba.
    """
    assert ord(ofensor) == punto, "el carácter se perdió al editar el fichero"
    assert unicodedata.category(ofensor) == categoria
    assert ofensor.isalnum() is False, (
        "este carácter SÍ es alfanumérico: entonces la guarda vieja ya lo cazaba "
        "y este caso no mide lo que dice medir")
    assert C.Journal.clave_fuera_del_alfabeto(clave) == ofensor
    assert ofensor not in C.Journal._clave_norm(clave)   # la norma se lo comía


@pytest.mark.parametrize("clave,_o,punto,_c", SIMBOLOS_NO_ALFANUM)
def test_los_simbolos_no_alfanumericos_NO_LLEGAN_A_PERSISTIRSE(tmp_path, clave, _o, punto, _c):
    j = jr(tmp_path)
    s = sesion(j, "c-be", principal="p-be", role="be")
    with pytest.raises(C.KeyCharsetRejected):
        _ev(j, s, {clave: "cto"}, key=f"sym-{punto}")
    con = sqlite3.connect(str(tmp_path / "coordination.sqlite"))
    assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    j.close()


def test_la_lista_blanca_es_CERRADA_y_no_pregunta_ninguna_propiedad_de_unicode():
    """El invariante de FORMA de la regla, que es lo que impide la 3ª recaída.

    `isalnum`, `isalpha` y `unicodedata.category` son preguntas ABIERTAS: las
    contesta una tabla que crece. Una regla construida sobre ellas es una lista
    NEGRA con nombre de blanca, y por eso este bloque falló dos veces seguidas.
    Aquí se fija que el conjunto es FINITO y EXACTAMENTE el declarado.
    """
    P = C.Journal.alfabeto_de_claves()
    assert isinstance(P, frozenset)
    assert P == {chr(c) for c in range(0x20, 0x7F)}
    assert len(P) == 95, f"el alfabeto tiene {len(P)} elementos, no 95"
    assert all(c.isascii() for c in P)
    # 🔻 `D19` lo ensanchó de `66` a `95` y **no revocó el principio**: ASCII
    # imprimible son `95` caracteres ENUMERABLES cuya definición NO PUEDE CRECER
    # (ASCII está congelado desde 1968). El test de bolsillo sigue dando SÍ.
    # Lo que sí sería un predicado abierto es `str.isprintable()`, y queda fuera
    # por ese mismo argumento — esto lo fija:
    assert P != {chr(c) for c in range(0x20, 0x110000) if chr(c).isprintable()}


# ⊕ derivado de la GRAMÁTICA de Unicode y no del corpus que ya conozco: un
# representante por categoría general que NO está en la lista. Si mañana la regla
# se ensancha para «ser tolerante con los símbolos» o «con los invisibles», este
# barrido lo caza aunque el atacante concreto no se le haya ocurrido a nadie.
UNA_POR_CATEGORIA = [
    ("Ll  latina cirílica", "а"), ("Ll  griega", "α"),
    ("Lo  CJK", "中"),             ("Lo  árabe", "ا"),
    ("Nd  dígito árabe-índico", "١"), ("Nl  numeral ideografico", "〇"),
    ("So  símbolo", "℮"),         ("So  emoji", "\U0001F600"),
    ("Sm  matemático", "∑"),      ("Sc  moneda", "€"),
    ("Cf  invisible", "​"),       ("Cf  RTL override", "‮"),
    ("Pd  guion unicode", "‐"),   ("Pc  undertie", "‿"),
    ("Zs  espacio ogham", " "),    ("Po  punto medio", "·"),
    ("Sm  suma circulada", "⨁"),  ("Po  coma árabe", "،"),
]
# 🔻 `D19`: `/`, `+` y `:` SALIERON de este barrido — ya no son «de fuera»,
# son ASCII imprimible ADMITIDO y su sitio es `ASCII_IMPRIMIBLE_ADMITIDO`.
# Dejarlos aquí producía `3` `skip` MUDOS: el barrido los saltaba «porque NFKD
# los pliega a ASCII», que es cierto y NO es lo que este test mide — un skip
# que se lee como cobertura. Sustituidos por representantes NO-ASCII de sus
# MISMAS categorías (`·` U+00B7 · `⨁` U+2A01 · `،` U+060C), elegidos MIDIENDO
# que sobreviven a NFKD. Resultado: **`0` skipped**.
# ⚠️ Los representantes se eligieron MIDIENDO cuáles SOBREVIVEN a NFKD, no a ojo.
# `ⅰ`(Nl), `＿`(Pc) y los espacios anchos (`U+00A0`, `U+2003`, `U+3000`) se pliegan a
# ASCII, así que medirían el PLEGADO y no la lista blanca: un control que se ve a sí
# mismo, y además dejaban `3` `skip` en una suite de seguridad, que es justo donde
# `@qa` dice que se esconde un test apagado. Sustituidos por `〇`, `‿` y el espacio
# ogham `U+1680`, que sí sobreviven. El `skip` defensivo se queda por si la tabla
# cambia — y hoy no dispara en ninguno, que es como tiene que estar.


@pytest.mark.parametrize("etiqueta,car", UNA_POR_CATEGORIA,
                         ids=[e.split()[0] + "-" + e.split()[1] for e, _ in UNA_POR_CATEGORIA])
def test_BARRIDO_por_categoria_ningun_codepoint_de_fuera_entra(etiqueta, car):
    """⊖ derivado de la gramática. Cada uno tiene que salir por la lista blanca.

    Se comprueba además que el carácter SOBREVIVE a NFKD: si decompusiera a
    ASCII, este caso no probaría la lista blanca sino el plegado, y sería un
    control que se ve a sí mismo.
    """
    t = unicodedata.normalize("NFKD", car)
    t = "".join(c for c in t if not unicodedata.combining(c))
    if all(c in C.Journal.alfabeto_de_claves() for c in t):
        pytest.skip(f"{etiqueta}: NFKD lo pliega a ASCII, no mide la lista blanca")
    assert C.Journal.clave_fuera_del_alfabeto(f"pre{car}post") is not None, etiqueta


# ⊕ LOS CUATRO SEPARADORES REALES, uno a uno. Salen del censo de las 105 claves
# vivas (`_` ×19 · ` ` ×10 · `-` ×5 · `.` ×1), no de mi cabeza. Este es el control
# que impide que la lista blanca cierre de más — y va POR SEPARADO de las claves
# legítimas porque un `_SEPARADORES` vacío las mataría a todas y hay que verlo.
@pytest.mark.parametrize("sep,nombre", [("_", "guion bajo"), (" ", "espacio"),
                                        ("-", "guion"), (".", "punto")])
def test_OMEGA_los_CUATRO_separadores_reales_siguen_pasando_y_colapsando(sep, nombre):
    clave = f"runtime{sep}instance"
    assert C.Journal.clave_fuera_del_alfabeto(clave) is None, nombre
    # y siguen COLAPSANDO: admitirlos no puede significar dejar de fundirlos
    assert C.Journal._clave_norm(clave) == "runtimeinstance", nombre
    assert C.Journal.clave_reservada(clave) is True, nombre
    # …y sobre una clave legítima no la convierten en reservada
    assert C.Journal.clave_reservada(f"nota{sep}libre") is False, nombre


@pytest.mark.parametrize("clave", [
    "principal_count", "anno_lane", "nota", "año", "señal", "cañón",
    "runtime_instance", "principal-id", "principal.id", "runtime instance",
    "  role  ", "Role", "ｎota", "Lané", "agentid", "credentials", "runtimems",
])
def test_OMEGA_la_lista_blanca_real_no_rechaza_ninguna_clave_viva(clave):
    """⊕ sobre la población que YA existía en este fichero: si la regla nueva
    rechazara una sola de éstas, habría roto el uso legítimo y el resto de la
    suite podría seguir verde sin enterarse."""
    assert C.Journal.clave_fuera_del_alfabeto(clave) is None


@pytest.mark.parametrize("clave,ofensor", [
    ("straße", "ß"), ("lane​", "​"), ("run‐time_instance", "‐"),
])
def test_LIMITE_DECLARADO_lo_que_la_lista_blanca_deja_FUERA(clave, ofensor):
    """🔴 El coste, escrito para que sea DECISIÓN y no sorpresa.

    Fuera de `[\x20-\x7E]` no entra nada tras NFKD. Tras `D19` esto ya NO incluye
    `+`, `:` ni `/` —que son ASCII imprimible y **se aceptan**— pero sí la `ß`
    alemana (no decompone), el `U+200B` y el `U+2010`. `clave_reservada` los
    sigue viendo; el cuerpo no los admite.
    """
    assert C.Journal.clave_fuera_del_alfabeto(clave) == ofensor


def test_DECLARADO_los_espacios_anchos_se_pliegan_a_UN_ESPACIO_y_ese_SI_se_admite():
    """Lo que el barrido por categoría NO puede probar, DICHO en vez de saltado.

    Casi todo `Zs` tiene descomposición de compatibilidad a `U+0020`, que ES uno
    de los cuatro separadores autorizados. O sea que `lane\u00a0`, `lane\u2003`
    y `lane\u3000` NO los para la lista blanca: los pliega NFKD y los caza el
    conjunto de RESERVADAS — el camino correcto, y el que `@security` ya fijó.
    De `Zs`, la lista blanca sólo llega a ver el espacio ogham (`U+1680`), que no
    se pliega.

    Se escribe porque «barrido por categoría completo» sería FALSO sin esto: hay
    una categoría entera cuyos miembros no llegan nunca a la guarda nueva, y eso
    es una propiedad del plegado, no un hueco.
    """
    for c in ("\u00a0", "\u2003", "\u3000"):
        assert unicodedata.normalize("NFKD", c) == " "
        assert C.Journal.clave_fuera_del_alfabeto(f"lane{c}") is None
        assert C.Journal.clave_reservada(f"lane{c}") is True      # lo caza el OTRO camino
    assert C.Journal.clave_fuera_del_alfabeto("lane\u1680") == "\u1680"


# ══ ✅ `D19` — DECISIÓN TOMADA, y queda ESCRITA aquí ════════════════════════
#
# El centinela que vivía en este bloque decía: «rojo el día que alguien rellene
# la pieza ⇒ que venga a escribir QUIÉN decidió y CUÁNDO». Se puso rojo, y esto
# es venir a escribirlo. **El centinela se retira porque cumplió, no porque
# estorbara.**
#
#   `@contratosbik`  00:18:35Z  RULING  (enmendado 00:24:19Z a [\x20-\x7E])
#   `@cto-llminbox`  00:19:44Z  RULING  D19
#   `@cpo`           00:25:21Z  RULING  «el contrato público admite ASCII
#                                        IMPRIMIBLE (0x20-0x7E), no 4 separadores»
#   `@cto-llminbox`  00:38:45Z  CIERRE  «las TRES convergen; @backend implementa
#                                        ESTO y no espera a nadie más»
#
# ⚖️ **NO revoca el principio de las tres rondas anteriores** — `@cpo` lo dijo
# mejor de lo que yo lo tenía: ASCII imprimible **no es un predicado**. Son `95`
# caracteres enumerables cuya definición **no puede crecer** (ASCII congelado
# desde 1968), así que el test de bolsillo *«¿puedo imprimir el conjunto?»* sigue
# dando SÍ. `str.isprintable()` sí sería el predicado abierto, y queda fuera
# **por ese mismo argumento**. Mi error no fue exigir una lista enumerable: fue
# elegir una demasiado pequeña y llamar «seguridad» a lo que era gusto.
#
# 🩸 Y la mitad que me toca: mi lista de `4` separadores rechazaba `8`-`9` formas
# estándar **con un código que MENTÍA** — `ATTRIBUTION_REJECTED` a un cliente que
# escribió `$schema`. El código nuevo (`KEY_CHARSET_REJECTED`) es la mitad de la
# adjudicación que arregla algo que yo había roto, no que faltara.

DECISORES_D19 = "contratosbik 00:18:35Z · cto 00:19:44Z · cpo 00:25:21Z · cierre cto 00:38:45Z"

ASCII_IMPRIMIBLE_ADMITIDO = ["$schema", "@type", "@context", "@id", "a:b", "a/b",
                             "a+b", "foo[0]", "xml:lang", "ns:campo", "100%", "c#",
                             "f(x)", "a;b", "a,b", "a|b", "a~b", "a`b"]


@pytest.mark.parametrize("clave", ASCII_IMPRIMIBLE_ADMITIDO)
def test_D19_el_ASCII_IMPRIMIBLE_se_ACEPTA(clave):
    """③ de la adjudicación. Éstas eran las `14` que mi lista de `4` separadores
    rechazaba, y son convenciones reales (JSON Schema, JSON-LD, namespaces)."""
    assert C.Journal.clave_fuera_del_alfabeto(clave) is None, DECISORES_D19


@pytest.mark.parametrize("clave", ASCII_IMPRIMIBLE_ADMITIDO)
def test_D19_y_ADEMAS_ENTRAN_de_verdad_y_PERSISTEN(tmp_path, clave):
    """No basta con que la guarda las deje pasar: tienen que llegar al registro.
    Un ⊕ sobre la función sola no acredita la puerta."""
    j = jr(tmp_path)
    s = sesion(j, "c-be", principal="p-be", role="be")
    assert _ev(j, s, {clave: "valor"}, key=f"d19-{abs(hash(clave))}").event_id
    con = sqlite3.connect(str(tmp_path / "coordination.sqlite"))
    assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    j.close()


# ⊖ EL FALSADOR OBLIGATORIO que exigió `@cto` en el cierre, literal:
#   «`'a\tb'` · `'a\nb'` · `'a\x00b'` ⇒ KEY_CHARSET_REJECTED. Sin ese brazo,
#    implementar ② como "rechaza no-ASCII" sale VERDE con el hueco abierto.»
# Es el ÚNICO brazo que separa «imprimible» de «ASCII», y por eso va con su
# assert de que `isascii()` diría que sí.
CONTROL_ASCII_NO_IMPRIMIBLE = [("a\tb", "\t"), ("a\nb", "\n"), ("a\x00b", "\x00"),
                               ("a\rb", "\r"), ("a\x7fb", "\x7f"), ("a\x1bb", "\x1b")]


@pytest.mark.parametrize("clave,ofensor", CONTROL_ASCII_NO_IMPRIMIBLE,
                         ids=[repr(o) for _, o in CONTROL_ASCII_NO_IMPRIMIBLE])
def test_D19_FALSADOR_los_ASCII_de_CONTROL_se_RECHAZAN(clave, ofensor):
    assert clave.isascii(), (
        "este caso no mide el brazo que dice medir: si no fuera ASCII lo cazaría "
        "el cierre cross-script y no la palabra IMPRIMIBLE")
    assert C.Journal.clave_fuera_del_alfabeto(clave) == ofensor


@pytest.mark.parametrize("clave,ofensor", CONTROL_ASCII_NO_IMPRIMIBLE,
                         ids=[repr(o) for _, o in CONTROL_ASCII_NO_IMPRIMIBLE])
def test_D19_los_de_CONTROL_no_llegan_a_PERSISTIRSE(tmp_path, clave, ofensor):
    j = jr(tmp_path)
    s = sesion(j, "c-be", principal="p-be", role="be")
    with pytest.raises(C.KeyCharsetRejected):
        _ev(j, s, {clave: "cto"}, key=f"ctl-{ord(ofensor)}")
    con = sqlite3.connect(str(tmp_path / "coordination.sqlite"))
    assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    j.close()


def test_D19_el_rango_se_escribe_como_RANGO_y_NO_como_isascii():
    """La trampa que `@cto` marcó: `'a\tb'.isascii()` es `True`. Una
    implementación con `isascii()` saldría VERDE en todo lo demás de este fichero
    y dejaría entrar tabulador, salto de línea y `NUL`."""
    assert "a\tb".isascii() and "a\x00b".isascii()
    assert C.Journal.clave_fuera_del_alfabeto("a\tb") == "\t"


def test_D19_el_CODIGO_del_charset_es_PROPIO_y_no_MIENTE():
    """① vs ② de la adjudicación: una clave con `а` cirílica **no es atribución**,
    y devolver `ATTRIBUTION_REJECTED` acusaba de suplantar a quien escribió mal
    una clave. Se mide que son clases DISTINTAS y que las dos están en el
    vocabulario cerrado."""
    assert issubclass(C.KeyCharsetRejected, C.JournalError)
    assert not issubclass(C.KeyCharsetRejected, C.AttributionRejected)
    assert C.Journal._POR_MOTIVO[C.KeyCharsetRejected] == "KEY_CHARSET_REJECTED"
    assert "KEY_CHARSET_REJECTED" in C.REASON_CODES
    assert set(C.Journal._POR_MOTIVO.values()) <= C.REASON_CODES


def test_D19_el_ACOPLAMIENTO_que_la_adjudicacion_declara_MAS_SEGURO(tmp_path):
    """`@cpo`: *«la puntuación admitida COLAPSA, así que `a$gent` cae en
    `clave_reservada` — más seguro que hoy»*. Se verifica con la puerta, no sólo
    con la función: `$role` entra por ③ y lo caza ①."""
    assert C.Journal._clave_norm("$role") == "role"
    assert C.Journal._clave_norm("a$gent") == "agent"
    assert C.Journal.clave_reservada("$role") is True
    assert C.Journal.clave_reservada("a$gent") is True
    assert C.Journal.clave_reservada("$schema") is False
    j = jr(tmp_path)
    s = sesion(j, "c-be", principal="p-be", role="be")
    with pytest.raises(C.AttributionRejected):      # ① y NO el del charset
        _ev(j, s, {"$role": "cto"})
    j.close()


def test_D19_LIMITE_lo_que_la_adjudicacion_NO_cierra():
    """🔴 Sigue fuera y siempre lo estará: el ataque MONOALFABÉTICO. `principa1`
    con un uno es ASCII imprimible legítimo y **se acepta** — no lo caza ni debe.
    Y `$schema` se admite porque colapsa a `schema`, que no es reservada: la
    seguridad la da ①, no ③."""
    assert C.Journal.clave_fuera_del_alfabeto("principa1") is None
    assert C.Journal.clave_reservada("principa1") is False




# ══ 🔴 NO-GO del auditor sobre `59ad745`: EL MENSAJE ERA EL VECTOR ══════════
#
# El rechazo interpolaba la clave CRUDA. Medido antes de tocar nada:
#
#     clave = 'a\nFAKE-LOG-LINE severity=INFO'
#     -> el mensaje salía en DOS líneas y la segunda la escribía el atacante:
#        L0: `a
#        L1: FAKE-LOG-LINE severity=INFO` lleva `'\n'` (U+000A), …
#
# Inyección en log: quien manda la clave decide una línea entera de un registro
# que otros leen como propio. Y `NUL`/`ESC` crudos llegan a un terminal, donde
# `ESC` es el prefijo de las secuencias de control.
# 🩸 Ironía del sitio: el `!r` del OFENSOR ya iba escapado. La clave entera no —
# escapé el detalle y dejé cruda la parte grande, que es la que el cliente elige.

VECTORES_EN_LA_CLAVE = [
    ("LF   inyecta línea", "a\nFAKE-LOG-LINE severity=INFO"),
    ("CR   inyecta línea", "a\rFAKE"),
    ("NUL  corta cadenas", "a\x00b"),
    ("ESC  secuencia de terminal", "a\x1b[31mROJO\x1b[0m"),
    ("TAB  rompe columnas", "a\tb"),
    ("DEL", "a\x7fb"),
    ("no-ASCII cirílico", "аgent"),
    ("no-ASCII símbolo", "ag℮nt"),
    ("no-ASCII emoji", "ag\U0001F600nt"),
]


@pytest.mark.parametrize("etiqueta,clave", VECTORES_EN_LA_CLAVE,
                         ids=[e.split()[0] for e, _ in VECTORES_EN_LA_CLAVE])
def test_NOGO_el_mensaje_de_rechazo_NO_lleva_el_crudo(etiqueta, clave):
    """⊖ El invariante, medido sobre el MENSAJE y no sobre la guarda.

    Tres afirmaciones, y ninguna es «el mensaje es ASCII»: el mensaje lleva prosa
    mía con acentos y eso es legítimo. Lo que no puede llevar es un carácter de
    CONTROL, ni partirse en más de una línea, ni reproducir el no-ASCII que
    describe.
    """
    with pytest.raises(C.KeyCharsetRejected) as e:
        C.Journal._sin_atribucion({clave: "x"}, "intent")
    m = str(e.value)
    controles = [f"U+{ord(c):04X}" for c in m if ord(c) < 0x20 or ord(c) == 0x7F]
    assert not controles, f"{etiqueta}: el mensaje lleva controles CRUDOS {controles}"
    assert len(m.splitlines()) == 1, (
        f"{etiqueta}: el mensaje ocupa {len(m.splitlines())} líneas — el cliente "
        f"escribe una línea entera de un log ajeno")
    # y la parte que el ATACANTE controla va en ASCII puro
    assert C.Journal.clave_para_mensaje(clave).isascii(), etiqueta
    # …aunque el carácter siga siendo IDENTIFICABLE: sin el punto de código, el
    # saneado se comería el dato que hace útil el rechazo.
    ofensor = C.Journal.clave_fuera_del_alfabeto(clave)
    assert f"U+{ord(ofensor):04X}" in m, f"{etiqueta}: el mensaje ya no identifica el ofensor"


@pytest.mark.parametrize("etiqueta,clave", VECTORES_EN_LA_CLAVE,
                         ids=[e.split()[0] for e, _ in VECTORES_EN_LA_CLAVE])
def test_NOGO_el_render_seguro_ESCAPA_y_no_pierde_el_dato(etiqueta, clave):
    r = C.Journal.clave_para_mensaje(clave)
    assert r.isascii(), etiqueta
    assert not any(ord(c) < 0x20 or ord(c) == 0x7F for c in r), etiqueta


def test_NOGO_la_clave_en_el_mensaje_esta_ACOTADA():
    """La cardinalidad del texto no la elige quien ataca — misma doctrina que
    `REASON_CODES` aplica a los códigos, un piso más abajo."""
    larga = "а" * 5000
    r = C.Journal.clave_para_mensaje(larga)
    assert len(r) <= C.Journal._CLAVE_EN_MENSAJE_MAX + len("…(truncada)")
    with pytest.raises(C.KeyCharsetRejected) as e:
        C.Journal._sin_atribucion({larga: "x"}, "intent")
    assert len(str(e.value)) < 400, "un mensaje de rechazo no es un buffer del cliente"


def test_NOGO_el_TEXTO_del_mensaje_dice_LA_VERDAD_tras_D19():
    """El mensaje afirmaba `[a-z0-9]` y «letra de otro alfabeto» — las dos cosas
    falsas tras `D19`: el alfabeto es `[0x20-0x7E]` y el caso más común que caza
    ahora es un carácter de CONTROL, que no es letra de ningún alfabeto."""
    with pytest.raises(C.KeyCharsetRejected) as e:
        C.Journal._sin_atribucion({"a\tb": "x"}, "intent")
    m = str(e.value)
    assert "[0x20-0x7E]" in m
    assert "[a-z0-9]" not in m, "el mensaje sigue citando el alfabeto de antes de D19"
    assert "control" in m, "un TAB no es «una letra de otro alfabeto»"


def test_NOGO_el_DOCSTRING_de_la_guarda_ya_no_dice_que_se_rechacen_mas_dos_puntos_barra():
    """La prosa también caducó con `D19` y la prosa es lo que lee el siguiente.
    Se mide sobre el docstring vivo, no sobre mi recuerdo de él."""
    d = C.Journal.clave_fuera_del_alfabeto.__doc__
    assert "[0x20-0x7E]" in d
    assert "`+`, un `:` o un `/`, que hoy no usa nadie" not in d, (
        "el docstring sigue diciendo que `+ : /` se rechazan, y tras D19 se ADMITEN")


# ══ 🔴 NO-GO 2 sobre `1f7c9b2`: sanear LA CLAVE no es sanear EL DETALLE ═════
#
# La ronda anterior saneó **sólo la clave ofensora**. La auditoría encontró tres
# huecos en lo que quedaba fuera, medidos por mi mano sobre `1f7c9b2`:
#
#   F1  clave imprimible de 100000 `!` + `agent` -> normaliza a RESERVADA, y
#       `AttributionRejected` reflejaba la clave CRUDA:      100105 bytes
#   F2  padre imprimible de 50000 + hijo `a\tb` -> el contexto `donde` se
#       concatenaba crudo en `KeyCharsetRejected`:            50256 bytes
#   F3  una subclase de `str` que cambia lo que renderiza TRAS las validaciones
#       inyectaba `LF` y `ESC`: el mensaje salía en DOS líneas, en los DOS
#       caminos (atribución y contexto de charset).
#
# 🩸 LA LECCIÓN, y es la razón de que ahora haya UNA función: sanear el trozo que
# uno está mirando **da la sensación de haber saneado**, y el vector estaba en el
# campo de al lado. La cura no es más cuidado, es que **no haya campos de al
# lado** — un embudo, y un test que barre TODOS los mensajes.
#
# 🔻 Y el mecanismo exacto de F3 NO es `__str__`, es `__format__`: un f-string
# sobre una subclase de `str` llama a `__format__`, así que una sonda que sólo
# engancha `__str__` **no lo ve** — la mía no lo vio a la primera y estuve a
# punto de dar el hallazgo por no reproducido.

SUCIO_INYECTOR = "role\nINYECTADO severity=INFO \x1b[31m"


def _garantias(m: str) -> list:
    """Las CUATRO que `detalle_seguro` promete. Devuelve las incumplidas."""
    fallos = []
    if len(m.splitlines()) > 1:
        fallos.append(f"{len(m.splitlines())} lineas")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in m):
        fallos.append("lleva controles")
    if not m.isascii():
        fallos.append("no es ASCII")
    if len(m) > C.Journal._DETALLE_MAX + len("...(trunc)"):
        fallos.append(f"{len(m)} chars > tope")
    return fallos


class _FormatMutante(str):
    """Subclase de `str` que MIENTE a partir de la segunda lectura.

    El gancho es `__format__` **y** `__str__`: se enganchan los dos a propósito
    para que el test no dependa de cuál usa la implementación de hoy.
    """
    n = 0

    def __str__(self):
        type(self).n += 1
        return str.__str__(self) if type(self).n <= 1 else SUCIO_INYECTOR

    def __format__(self, spec):
        type(self).n += 1
        return str.__str__(self) if type(self).n <= 1 else SUCIO_INYECTOR


def test_F1_una_clave_ENORME_no_infla_el_detalle_de_ATRIBUCION():
    """`!`*100000 + `agent` normaliza a `agent`, que ES reservada. El rechazo
    correcto es `AttributionRejected` — y su detalle no puede ser el buffer que
    el cliente mandó."""
    clave = "!" * 100000 + "agent"
    assert C.Journal.clave_reservada(clave) is True, "el caso ya no llega a atribución"
    with pytest.raises(C.AttributionRejected) as e:
        C.Journal._sin_atribucion({clave: "x"}, "intent")
    m = str(e.value)
    assert not _garantias(m), f"{_garantias(m)} · bytes={len(m.encode())}"
    # ⚠️ NO se asserta que no aparezca NINGÚN fragmento del cliente: mostrar un
    # PREFIJO ACOTADO de la clave ofensora es diagnóstico útil y correcto. Lo que
    # no puede es reflejar el BUFFER. Mi primera versión de este test afirmaba lo
    # otro y falló contra código sano — el invariante es el tamaño, no la
    # ausencia.
    assert m.count("!") <= C.Journal._CLAVE_EN_MENSAJE_MAX, (
        f"el detalle refleja {m.count('!')} signos del buffer del cliente")


def test_F2_un_CONTEXTO_enorme_no_infla_el_detalle_de_CHARSET():
    """El `donde` se concatena en el mensaje: si va crudo, el tamaño lo elige
    quien anida. `50256` bytes medidos antes de la cura."""
    with pytest.raises(C.KeyCharsetRejected) as e:
        C.Journal._sin_atribucion({"P" * 50000: {"a\tb": "x"}}, "intent")
    m = str(e.value)
    assert not _garantias(m), f"{_garantias(m)} · bytes={len(m.encode())}"
    assert m.count("P") <= C.Journal._CONTEXTO_EN_MENSAJE_MAX


@pytest.mark.parametrize("camino", ["atribucion", "contexto"])
def test_F3_el_TOCTOU_de_str_y_format_esta_MUERTO(camino):
    """El valor se congela UNA vez al borde, antes de validar. El atacante puede
    mentir una vez —y esa mentira es la que se valida Y la que se pinta—, así que
    no queda ventana entre las dos lecturas."""
    class K(_FormatMutante):
        n = 0
    cuerpo = {K("role"): "x"} if camino == "atribucion" else {K("ok"): {"a\tb": "x"}}
    with pytest.raises(C.JournalError) as e:
        C.Journal._sin_atribucion(cuerpo, "intent")
    m = str(e.value)
    assert not _garantias(m), f"{camino}: {_garantias(m)}"
    assert "INYECTADO" not in m or m.isascii(), camino
    assert "\\n" in m or "INYECTADO" not in m, (
        f"{camino}: el inyector llegó al detalle SIN escapar")


# ⊖ EL BARRIDO: todos los mensajes que la guarda sabe producir, de una vez. Es
# la mitad que impide que vuelva a haber «un campo de al lado» — un test por
# vector cubre los vectores que ya conozco; esto cubre la SUPERFICIE.
CUERPOS_HOSTILES = [
    ("clave enorme reservada", {"!" * 100000 + "agent": "x"}),
    ("clave enorme no reservada", {"!" * 100000 + "zzz\t": "x"}),
    ("contexto enorme", {"P" * 50000: {"a\tb": "x"}}),
    ("anidado profundo", {"a": {"b": {"c": {"d": {"e": {"role": "x"}}}}}}),
    ("en lista", {"xs": [{"a\nb": "x"}]}),
    ("LF en clave", {"a\nFAKE severity=INFO": "x"}),
    ("ESC en clave", {"a\x1b[31mROJO": "x"}),
    ("NUL en clave", {"a\x00b": "x"}),
    ("cirilico", {"аgent": "x"}),
    ("emoji", {"ag\U0001F600nt": "x"}),
    ("reservada simple", {"principal": "x"}),
    ("reservada disfrazada", {"$role": "x"}),
    ("profundidad excedida", {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": {
        "i": {"j": {"k": {"l": {"m": {"n": "x"}}}}}}}}}}}}}}),
]


@pytest.mark.parametrize("etiqueta,cuerpo", CUERPOS_HOSTILES,
                         ids=[e for e, _ in CUERPOS_HOSTILES])
def test_BARRIDO_TODO_detalle_que_sale_del_nucleo_cumple_las_CUATRO(etiqueta, cuerpo):
    with pytest.raises(C.JournalError) as e:
        C.Journal._sin_atribucion(cuerpo, "intent")
    m = str(e.value)
    assert not _garantias(m), f"{etiqueta}: {_garantias(m)} · {m[:120]!r}"


def test_el_BARRIDO_no_es_vacuo_todos_los_cuerpos_LEVANTAN():
    """⊕ El control del barrido: si alguno no levantara, su fila estaría midiendo
    un `pytest.raises` que nunca se cumple… y `pytest.raises` ya fallaría. Esto
    fija además que la población cubre las DOS clases de rechazo, no una."""
    clases = set()
    for _, cuerpo in CUERPOS_HOSTILES:
        with pytest.raises(C.JournalError) as e:
            C.Journal._sin_atribucion(cuerpo, "intent")
        clases.add(type(e.value).__name__)
    assert {"AttributionRejected", "KeyCharsetRejected"} <= clases, clases


@pytest.mark.parametrize("entrada", [
    "a\nb", "a\x00b", "a\x1b[31m", "аgent", "ag\U0001F600nt", "!" * 100000,
    "", "normal_key", "a" * 400, "\U0001F170" * 200,
])
def test_detalle_seguro_cumple_las_CUATRO_para_cualquier_entrada(entrada):
    r = C.Journal.detalle_seguro(entrada)
    assert not _garantias(r), _garantias(r)


def test_detalle_seguro_es_IDEMPOTENTE():
    """Pasarlo dos veces no puede cambiarlo ni hacerlo crecer: los mensajes se
    componen de trozos que ya pasaron por él y luego pasan enteros otra vez."""
    for e in ["a\nb", "аgent", "!" * 500]:
        una = C.Journal.detalle_seguro(e)
        assert len(C.Journal.detalle_seguro(una)) <= C.Journal._DETALLE_MAX + len("...(trunc)")


def test_el_MARCADOR_de_truncado_es_ASCII():
    """🩸 Era `…` (`U+2026`) — **no-ASCII en la función que promete ASCII**. Mis
    propios tests no lo veían porque su población nunca llegaba a truncar: un
    control que no cubre su propio caso límite."""
    r = C.Journal.clave_para_mensaje("а" * 5000)
    assert r.isascii() and "…" not in r
    # 🔻 YA NO va al FINAL: la politica pasó a cabeza + marcador + COLA para que
    # un prefijo no fiable no borre la razon (NO-GO de la cota). El marcador
    # sigue estando —es lo que dice «aqui falta texto»— pero en medio.
    assert C.MARCA_TRUNCADO in r
    assert not r.endswith(C.MARCA_TRUNCADO) or len(r) == C.Journal._CLAVE_EN_MENSAJE_MAX


def test_ascii_SIEMPRE_produce_imprimible_ASCII_y_esto_no_es_una_suposicion():
    """La garantía ③ se apoya en `ascii()`. Se mide en vez de suponerse — muestreo
    de los planos que importan más los límites exactos de cada tramo."""
    cps = list(range(0, 0x300)) + [0x7F, 0x80, 0x9F, 0xA0, 0x200B, 0x2010, 0x2026,
                                   0x430, 0x212E, 0xFF50, 0x1F170, 0x1F600, 0x10FFFF]
    for cp in cps:
        if 0xD800 <= cp <= 0xDFFF:
            continue
        o = ascii(chr(cp))
        assert all(0x20 <= ord(c) <= 0x7E for c in o), hex(cp)


def test_el_DOCSTRING_ya_no_nombra_una_constante_que_NO_EXISTE():
    """`_PERMITIDOS` se retiró con `D19` y el docstring seguía citándola. Una
    prosa que nombra un símbolo muerto manda al siguiente a buscar lo que no
    está — y aquí se mide contra el atributo vivo, no contra mi recuerdo."""
    d = C.Journal.clave_fuera_del_alfabeto.__doc__ or ""
    assert "_PERMITIDOS" not in d
    assert not hasattr(C.Journal, "_PERMITIDOS")
    assert "_ASCII_IMPRIMIBLE" in d and hasattr(C.Journal, "_ASCII_IMPRIMIBLE")


# ══ El invariante que faltaba: un rechazo tiene que decir POR QUÉ ═══════════
#
# Tres mutantes del embudo SOBREVIVIERON a la primera corrida (`68/71`), y no
# porque hubiera un hueco: quitaban el saneado INTERNO mientras el embudo
# EXTERIOR seguía envolviendo el mensaje, así que las cuatro garantías se
# cumplían igual y mis tests no los distinguían.
#
# Lo que la pieza interna añade —y esto sí se mide— es que el mensaje SIGA
# DICIENDO POR QUÉ. Sin los topes por pieza, el eco del cliente se come el
# presupuesto global y la razón se trunca:
#
#     atribucion, clave de 100000  ->  sano 181 B, RAZÓN presente
#                                      mutante 330 B, RAZÓN **PERDIDA**
#     profundidad, `donde` enorme  ->  sano 214 B, RAZÓN presente
#                                      mutante 330 B, RAZÓN **PERDIDA**
#
# 🩸 Y ESTA MEDIDA ME SALIÓ MAL DOS VECES ANTES DE SALIR BIEN: mi sonda escribía
# cada mutante en el MISMO directorio y Python reusaba el `__pycache__`, así que
# los tres daban idéntico al sano y estuve a punto de declarar EQUIVALENTES a dos
# mutantes que sí discriminan. Un directorio nuevo por mutante lo arregló. El
# arnés oficial no tiene ese defecto —clona a un tmpdir por mutante—; era mi
# sonda de mano.


def _razon_sobrevive(cuerpo, razon):
    with pytest.raises(C.JournalError) as e:
        C.Journal._sin_atribucion(cuerpo, "intent")
    return razon in str(e.value), str(e.value)


def _anida(n, hoja):
    d = hoja
    for i in range(n):
        d = {f"k{i}" * 60: d}
    return d


def test_INVARIANTE_un_rechazo_por_ATRIBUCION_sigue_diciendo_POR_QUE():
    """El eco del cliente no puede comerse la razón. Con una clave de `100000`
    signos que normaliza a reservada, el mensaje tiene que seguir explicando qué
    pasó — y sin los topes por pieza, no lo hace."""
    ok, m = _razon_sobrevive({"!" * 100000 + "agent": "x"}, "la pone el servidor")
    assert ok, f"la razón se truncó; el detalle es sólo eco del cliente: {m[:120]!r}"
    assert len(m) < 260, f"len={len(m)}: el eco ocupa casi todo el presupuesto"


def test_INVARIANTE_un_rechazo_por_PROFUNDIDAD_sigue_diciendo_POR_QUE():
    """El gemelo, por la otra puerta. 🔻 REANCLADO Y ENDURECIDO tras la cura del
    `field` (hallazgo `@qa` §4): esta guarda ya no construye su mensaje a mano,
    sale por `_rle`, así que **no interpola NADA del cliente** — ni el contexto.

    El `len(m) < 260` de antes era un PROXY del daño («si el eco ocupa casi todo
    el presupuesto, se comió la razón»). Con `_rle` el proxy dejó de valer por
    dos motivos, y ninguno es que la garantía se haya aflojado:
      · el mensaje base de `_rle` mide `299 B` de `320` él solo, sin eco alguno;
      · y el eco YA NO EXISTE, que es más fuerte que acotarlo.
    ⇒ se mide la propiedad directamente: la razón está **y** el cuerpo del
    cliente NO aparece. Un `len` sobre un mensaje sin eco no mide al cliente,
    mide mi prosa."""
    ok, m = _razon_sobrevive(_anida(14, "x"), "RESOURCE_LIMIT_EXCEEDED")
    assert ok, f"la razón se truncó: {m[:120]!r}"
    # las claves del cuerpo son `k0`..`k13` repetidas 60 veces: si UNA asoma,
    # hay eco del cliente en un mensaje que ya no debería tener ninguno
    assert "k13" not in m and "k0k0" not in m, (
        f"eco del cliente en el mensaje: {m[:160]!r}")
    assert C.MARCA_TRUNCADO not in m, (
        f"len={len(m)}: el mensaje se está truncando. Sin eco del cliente el "
        f"único que puede desbordar el presupuesto soy yo con la cola")


def test_INVARIANTE_y_el_de_CHARSET_tambien_aunque_ahi_no_discrimine():
    """⚖️ EQUIVALENCIA DECLARADA, con su falsador.

    En el mensaje de charset la razón (`U+XXXX`) va **antes** que el contexto,
    así que truncar `donde` no cambia nada observable: el mutante que quita ese
    tope interno da resultados IDÉNTICOS al sano en las tres entradas que probé
    —contexto de `80000`, contexto anidado, y el caso base—. **Es un mutante
    equivalente y se retira del arnés**, no se deja «vivo» ensuciando el informe.

    El tope interno se QUEDA en el código por consistencia —todo trozo externo
    pasa por el embudo, y esa regla vale más que ahorrar una llamada— pero queda
    escrito que hoy no lo acredita ningún falsador. Si alguien reordena el
    mensaje y pone el contexto delante, este test empieza a discriminar solo.
    """
    ok, m = _razon_sobrevive({"Q" * 80000: {"a\tb": "x"}}, "U+0009")
    assert ok, m[:120]
    # 🔻 El orden ya NO se puede medir por indices de las dos subcadenas: con la
    # cola preservada el mensaje es cabeza + marcador + cola, asi que «No entra,
    # en» puede haberse quedado en el trozo cortado aunque la razon sobreviva.
    # Lo que la equivalencia necesita sigue siendo cierto y se mide directo: la
    # razon esta ANTES del marcador, o sea en la CABEZA — que es lo que hace que
    # truncar el contexto no la toque.
    assert m.index("U+0009") < m.index(C.MARCA_TRUNCADO), (
        "la razon dejo de estar en la cabeza: el tope interno de `donde` PASA a "
        "ser discriminante y hay que devolverle su mutante")


def test_INVARIANTE_un_rechazo_por_CHARSET_sigue_diciendo_POR_QUE():
    """El tercer camino del mismo invariante, y el que dejó VIVO un mutante en la
    corrida `69/70`.

    Mis tests `NOGO` de este mensaje sólo miraban controles, líneas y ASCII — y
    eso el embudo EXTERIOR ya lo garantiza aunque se quite el render interno de
    la clave. Lo que sólo la pieza interna protege es que el `U+XXXX` **siga
    ahí**: con una clave de `100000` signos, sin el tope por pieza el eco se come
    los `320` del presupuesto y el punto de código desaparece.

        sano     len=330  U+0009 presente
        mutante  len=330  U+0009 **PERDIDO**

    Un rechazo que no dice qué carácter sobra deja al cliente honesto buscando a
    ciegas en una clave que ni siquiera puede ver entera.
    """
    ok, m = _razon_sobrevive({"!" * 100000 + "\t": "x"}, "U+0009")
    assert ok, f"el punto de código se truncó: {m[:120]!r}"
    ok2, m2 = _razon_sobrevive({"a\tb": "x"}, "U+0009")
    assert ok2, m2[:120]


@pytest.mark.parametrize("tope,entrada", [
    (None, "!" * 100000), (None, "а" * 5000), (64, "x" * 500), (10, "y" * 99),
    (len("...(trunc)"), "z" * 50), (5, "w" * 50),
])
def test_P3_el_TOPE_es_el_LARGO_del_resultado_marcador_INCLUIDO(tope, entrada):
    """🟡 P3 de `@security` sobre `8b5eb51`, curado haciendo VERDADERA la promesa.

    `_DETALLE_MAX = 320  # y el TOPE GLOBAL del detalle entero` — y el peor caso
    medido daba `330`, porque el marcador se sumaba DESPUÉS de cortar. No era un
    vector; era **mi comentario prometiendo más de lo que el código hacía**, que
    es la clase que llevo cazándome toda la noche. Ahora el tope es el largo
    máximo del resultado, sin excepciones.

    Se prueba con topes ABSURDAMENTE pequeños (`5`, y el del propio marcador)
    porque ahí es donde un `max(0, ...)` mal puesto rompe o vuelve a desbordar.
    """
    lim = C.Journal._DETALLE_MAX if tope is None else tope
    r = C.Journal.detalle_seguro(entrada, tope=tope)
    assert len(r) <= lim, f"len={len(r)} > tope={lim}"
    assert not _garantias(r)


def test_P3_el_peor_caso_REAL_del_mensaje_cabe_en_el_tope():
    """El caso exacto que `@security` midió: clave enorme Y contexto enorme."""
    with pytest.raises(C.JournalError) as e:
        C.Journal._sin_atribucion({"Q" * 80000: {"!" * 100000 + "\t": "x"}}, "intent")
    m = str(e.value)
    assert len(m) <= C.Journal._DETALLE_MAX, f"len={len(m)} > {C.Journal._DETALLE_MAX}"
    assert "U+0009" in m, "y sigue diciendo POR QUÉ"


# ══ El embudo del CONTEXTO en el mensaje de profundidad ═════════════════════
#
# 🔴 Del segundo SUPERVIVIENTE del arnés (`MN1-profundidad-fuera-del-embudo`):
# sustituye `campo = detalle_seguro(donde, tope=_CONTEXTO_EN_MENSAJE_MAX)` por el
# literal `campo = "intent"`, y `test_INVARIANTE_un_rechazo_por_PROFUNDIDAD_
# sigue_diciendo_POR_QUE` SIGUE VERDE.
#
# Por qué no discriminaba: aquel test pide que la RAZÓN sobreviva y que el
# mensaje quepa en `260`. El mutante sustituye el contexto por una constante de
# `6` signos ⇒ el mensaje sale AÚN MÁS CORTO y la razón sobra de sitio. Un tope
# no se falsa haciendo el dato más pequeño: se falsa por lo que el tope PROTEGE.
#
# Aquí lo que protege son DOS cosas distintas, y por eso hay dos tests:
#   ① FIDELIDAD — `field` nombra el campo REAL. El mutante dice `intent` venga
#     de donde venga: es código que MIENTE, la misma clase que `D19` cerró.
#   ② EMBUDO — cuando el camino real es enorme, `field` sale ACOTADO y con
#     `MARCA_TRUNCADO`. Medido: sano `len=96` con marca; mutante `len=6` sin ella.

def test_el_CAMPO_del_rechazo_por_PROFUNDIDAD_nombra_el_campo_REAL():
    """① El mensaje no puede acusar a `intent` cuando el cuerpo hondo venía en
    `trace` o en `payload`: quien lo lea corrige el sitio equivocado."""
    for donde in ("trace", "payload"):
        with pytest.raises(C.ResourceLimitExceeded) as e:
            C.Journal._sin_atribucion(_anida(14, "x"), donde)
        assert e.value.field.startswith(donde), (
            f"field={e.value.field!r} con donde={donde!r}: el rechazo nombra un "
            f"campo que NO es el que falló")


def test_el_CAMPO_del_rechazo_por_PROFUNDIDAD_esta_en_el_ENUM_CERRADO():
    """② 🔻 REESCRITO ENTERO. Antes exigía que el `field` fuese el CAMINO
    acotado y con `MARCA_TRUNCADO` — o sea **exigía el valor off-contract** que
    `@qa` cazó (§4). El embudo protegía un trozo externo que jamás debió estar
    en un atributo del contrato.

    La garantía correcta es la del enum: `field` es un valor ENUMERADO, y da
    igual lo hondo o lo raro que sea el cuerpo. El camino, si hace falta, es
    prosa — y hoy ni siquiera cabe (`299` de `320` gastados por `_rle`)."""
    for donde in ("intent", "trace", "payload"):
        with pytest.raises(C.ResourceLimitExceeded) as e:
            C.Journal._sin_atribucion(_anida(14, "x"), donde)
        assert e.value.field == donde, (
            f"field={e.value.field!r} con donde={donde!r}: tiene que ser la RAÍZ "
            f"canónica, ni el camino ni otro campo")
        assert e.value.field in C.CAMPOS_CONTRATO_CORE, (
            f"field={e.value.field!r} fuera del enum CERRADO")
