"""NO-GO independiente sobre `8b5eb51`: los `4` focales cerraban, la GARANTÍA no.

Yo curé los sitios que la auditoría anterior me nombró —`intent`, `trace`,
`payload`, profundidad— y presenté eso como una garantía global. **No lo era.**
Reproducido por mi mano sobre `8b5eb51`:

    kind ......... 2149 B                  causes ....... 2142 B · 2 lineas · 2 ctl
    recipient .... 2132 B · 2 lineas · 2 ctl   command_id ... 2051 B · 2 lineas · 2 ctl
    ledger ....... 2077 B · 2 lineas · 2 ctl   ttl_s 1000 digitos ... 1096 B
    resource ..... 210 B no-ASCII

🩸 **Es la MISMA clase, por TERCERA vez, un nivel más arriba cada vez**: sanear el
trozo que uno mira da la sensación de haber saneado. Primero fue la clave y no el
contexto; luego el contexto y no la profundidad; ahora los cuatro de
`_sin_atribucion` y no los `73` `raise` del resto del fichero.
**La cura no es más sitios: es que no haya sitios.** El saneado vive en
`JournalError.__init__`, así que toda subclase lo hereda y un `raise` escrito
dentro de un año lo tiene por construcción.

⚖️ Este fichero prueba la garantía **por FAMILIAS**, no por sondas: una lista de
sondas cubre las que ya me enseñaron. El censo por AST cubre la superficie.
"""
from __future__ import annotations

import ast
import inspect
import pathlib
import tempfile

import pytest

import coordination as C
from ._arnes import INTENT, journal, sesion

# El veneno lleva las CUATRO cosas a la vez: salto de línea, control, no-ASCII y
# volumen. Una sonda que sólo lleve una no distingue qué garantía falló.
VENENO = "\nINYECTADO severity=INFO \x1b[31m\x00а℮" + "Z" * 2000


def _incumple(m: str) -> list:
    fallos = []
    if len(m.splitlines()) > 1:       # `<= 1`: un detalle VACIO es 0 lineas y es legitimo
        fallos.append(f"{len(m.splitlines())} lineas")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in m):
        fallos.append("controles")
    if not m.isascii():
        fallos.append("no-ASCII")
    if len(m) > C.DETALLE_MAX:
        fallos.append(f"{len(m)} > {C.DETALLE_MAX}")
    return fallos


@pytest.fixture()
def j(tmp_path):
    d = journal(tmp_path)
    yield d, sesion(d, "c", principal="p", role="be")
    d.close()


# ── FAMILIAS: un campo externo por cada puerta que lo refleja ───────────────
FAMILIAS = [
    ("kind",        lambda d, s: d.accept_event(s.token, idempotency_key="k",
                                                intent={**INTENT, "kind": VENENO}, ledger="llminbox")),
    ("head",        lambda d, s: d.accept_event(s.token, idempotency_key="k",
                                                intent={**INTENT, "head": VENENO}, ledger="llminbox")),
    ("recipient",   lambda d, s: d.accept_event(s.token, idempotency_key="k",
                                                intent={**INTENT, "to": [VENENO]}, ledger="llminbox")),
    ("ledger",      lambda d, s: d.accept_event(s.token, idempotency_key="k",
                                                intent=INTENT, ledger=VENENO)),
    ("causes",      lambda d, s: d.accept_event(s.token, idempotency_key="k", intent=INTENT,
                                                ledger="llminbox", causes=[VENENO])),
    ("ext_causes",  lambda d, s: d.accept_event(s.token, idempotency_key="k", intent=INTENT,
                                                ledger="llminbox",
                                                external_causes=[{"ledger": VENENO}])),
    ("fencing",     lambda d, s: d.accept_event(s.token, idempotency_key="k", intent=INTENT,
                                                ledger="llminbox", fenced_resource="r",
                                                fencing_token="9" * 1000)),
    ("resource",    lambda d, s: d.renew_lease(s.token, VENENO, ttl_s=100)),
    ("ttl_s",       lambda d, s: d.open_session("c", ttl_s=-int("9" * 1000))),
    ("command_id",  lambda d, s: d.may_execute(s.token, VENENO)),
    ("event_id",    lambda d, s: d.receipt_for_event(s.token, VENENO)),
    ("intent",      lambda d, s: d.accept_event(s.token, idempotency_key="k",
                                                intent={**INTENT, "meta": {VENENO: "x"}},
                                                ledger="llminbox")),
    ("payload",     lambda d, s: d.submit_command(s.token, workstream_id="w", revision=1,
                                                  payload={VENENO: "x"})),
]


@pytest.mark.parametrize("familia,verbo", FAMILIAS, ids=[f for f, _ in FAMILIAS])
def test_GLOBAL_ninguna_familia_refleja_el_veneno(j, familia, verbo):
    d, s = j
    with pytest.raises(C.JournalError) as e:
        verbo(d, s)
    m = str(e.value)
    assert not _incumple(m), f"{familia}: {_incumple(m)} · {m[:100]!r}"


def test_GLOBAL_el_barrido_no_es_VACUO_todas_las_familias_LEVANTAN(tmp_path):
    """⊕ ANTI-VACUIDAD del barrido, y es la mitad que lo hace valer.

    Si una familia dejara de levantar —porque la validación cambió, porque el
    veneno ya no la dispara— su fila mediría un `raise` que nunca ocurre y
    seguiría VERDE. Se exige que las `13` levanten y que cubran varias clases:
    un barrido que sólo llega a una puerta no es un barrido.
    """
    clases = set()
    for familia, verbo in FAMILIAS:
        sub = tmp_path / familia
        sub.mkdir(parents=True, exist_ok=True)   # el journal no crea su directorio
        d = journal(sub)
        s = sesion(d, "c", principal="p", role="be")
        with pytest.raises(C.JournalError) as e:
            verbo(d, s)
        clases.add(type(e.value).__name__)
        d.close()
    assert len(clases) >= 6, f"solo {len(clases)} clases distintas: {sorted(clases)}"


# ── CENSO: la garantía vive en la BASE, no en una lista de sitios ───────────

def test_CENSO_todas_las_subclases_de_JournalError_heredan_el_saneado():
    """La población es **toda** subclase, enumerada del módulo vivo. Una clase
    nueva que se saltara la base saldría aquí, no en una revisión."""
    subclases = [getattr(C, n) for n in dir(C)
                 if isinstance(getattr(C, n), type)
                 and issubclass(getattr(C, n), C.JournalError)
                 and getattr(C, n) is not C.JournalError]
    # 🔻 33 -> 35 al entrar `AdmissionClosed` y `AdmissionConflict` con la
    # barrera de admision. DECIDIDO, no relajado: las dos heredan de
    # `JournalError` y por tanto el saneado, que es lo que este censo mide.
    assert len(subclases) == 35, (
        f"la poblacion de subclases es {len(subclases)}, no 35. Si has anadido o "
        f"quitado una, DECIDE y actualiza este numero — un censo con un `>=` no "
        f"ve salir a nadie, y salir de la jerarquia es perder el saneado")
    for cls in subclases:
        m = str(cls(VENENO))
        assert not _incumple(m), f"{cls.__name__}: {_incumple(m)}"


def test_CENSO_ningun_raise_del_fichero_queda_FUERA_de_la_jerarquia():
    """🔴 El hueco que el propio invariante no puede ver desde dentro.

    Un censo sobre «las subclases de `JournalError`» **no puede** ver un `raise`
    de algo que no es subclase — y sale verde. Medido: de `75` `raise` con
    f-string, `2` son `ValueError` (`:5371` y `:5480`, ambos con `{reason}`), y
    la base no los toca.

    **Este test NO exige que estén curados**: exige que su número esté
    DECLARADO. Si aparece un tercero, se pone rojo y alguien tiene que venir a
    decidir — que es distinto de que pase inadvertido.
    """
    src = pathlib.Path(C.__file__).read_text()
    subclases = {n for n in dir(C)
                 if isinstance(getattr(C, n), type) and issubclass(getattr(C, n), C.JournalError)}
    fuera = []
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call):
            nombre = getattr(n.exc.func, "id", getattr(n.exc.func, "attr", None))
            if any(isinstance(a, ast.JoinedStr) for a in n.exc.args) and nombre not in subclases:
                fuera.append((n.lineno, nombre))
    assert fuera == [], (
        f"hay `raise` con f-string CRUDO fuera de la jerarquia: {fuera}. La base "
        f"no los toca (no heredan de JournalError), asi que o pasan por "
        f"`_saneado` a mano o son un hueco")
    # ⚠️ Y el diente que hay que conservar: los `raise ValueError` SIGUEN
    # existiendo —la clase no cambia— pero ahora su argumento es una LLAMADA a
    # `_saneado`, no un f-string. Se exige exactamente eso, o el test de arriba
    # se volveria vacuo el dia que alguien escriba un `ValueError` crudo con
    # `.format()` o concatenacion en vez de f-string.
    ve = [n2 for n2 in ast.walk(ast.parse(src))
          if isinstance(n2, ast.Raise) and isinstance(n2.exc, ast.Call)
          and getattr(n2.exc.func, "id", None) == "ValueError"]
    assert len(ve) == 3, f"hay {len(ve)} `raise ValueError`, no 3"
    saneados = [r for r in ve
                if r.exc.args and isinstance(r.exc.args[0], ast.Call)
                and getattr(r.exc.args[0].func, "id", None) == "_saneado"]
    assert len(saneados) == 2, (
        f"solo {len(saneados)} de los `raise ValueError` pasan por `_saneado`. "
        f"El tercero es el del `pepper` en `__init__`, que NO lleva dato externo "
        f"—es una constante mia— y por eso no entra")
    # ⚠️ MISMO DIENTE PARA `AssertionError`, y por un defecto MEDIDO, no por
    # simetria: la guarda del enum de `_rle` nacio como `raise AssertionError`
    # con f-string CRUDO y `campo` —un argumento— interpolado tal cual. El
    # `assert fuera == []` de arriba la cazo; sin ESTE diente, la cura se puede
    # deshacer con `.format()` o concatenacion y el censo no lo veria.
    ae = [n3 for n3 in ast.walk(ast.parse(src))
          if isinstance(n3, ast.Raise) and isinstance(n3.exc, ast.Call)
          and getattr(n3.exc.func, "id", None) == "AssertionError"]
    assert len(ae) == 1, f"hay {len(ae)} `raise AssertionError`, no 1"
    assert all(r.exc.args and isinstance(r.exc.args[0], ast.Call)
               and getattr(r.exc.args[0].func, "id", None) == "_saneado" for r in ae), (
        "el `raise AssertionError` lleva `campo` —un argumento— en el mensaje: "
        "tiene que pasar por `_saneado`, igual que los `ValueError` con dato externo")


def test_LIMITE_CERRADO_los_dos_ValueError_YA_NO_reflejan_el_crudo():
    """✅ LÍMITE CERRADO — y se escribe la retirada, no se borra el test.

    Aquí vivía `test_LIMITE_los_dos_ValueError_siguen_reflejando_el_crudo_y_se_
    DECLARA`, que EXIGÍA que siguieran reflejando el crudo porque el encargo de
    entonces acotaba a «toda salida `JournalError`». Su propio mensaje decía:
    *«borra este limite y actualiza el censo, porque el hueco dejo de existir»*.
    **Se puso rojo al cerrarlo, y esto es venir a hacerlo.**

    Un límite que se declaró en público se retira en público — es la tercera vez
    en esta rama que un test-centinela cumple su función y hay que jubilarlo.
    """
    d = journal(pathlib.Path(tempfile.mkdtemp()))
    s = sesion(d, "c", principal="p", role="be")
    with pytest.raises(ValueError) as e:
        d.record_rejection(s.token, "MOTIVO\nINVENTADO\x1b[31m" + "Z" * 3000)
    assert not _incumple(str(e.value)), _incumple(str(e.value))
    d.close()


def test_la_funcion_de_saneado_es_UNA_y_esta_en_la_BASE():
    """Estructural: si alguien mueve el saneado a los sitios de llamada, la
    garantía vuelve a depender de que nadie olvide uno."""
    fuente = inspect.getsource(C.JournalError)
    assert "_saneado" in fuente, (
        "`JournalError.__init__` ya no sanea: la garantia dejo de ser global y "
        "volvio a depender de cada `raise`")


# ══ ALTO previo al commit: la garantia no puede depender de la ARIDAD ══════
#
# Medido sobre la version anterior de este mismo correctivo, antes de firmarlo:
#
#     JournalError(lista) .............. 2023 B   <- arg NO-str: no se saneaba
#     JournalError("ok", VENENO) ....... 2024 B   <- `args[1:]` intactos
#     JournalError("ok", 1, VENENO) .... 2027 B
#     _saneado(tope=-5) ................ len 95   <- `t[:-5]` recorta DESDE EL FINAL
#
# 🩸 El `if args and isinstance(args[0], str)` hacia que el invariante dependiera
# de que nadie escriba nunca un `raise` con dos argumentos, o con uno que no sea
# `str`. **Un invariante global no se apoya en una convencion de llamada
# futura**: o se cierra la aridad, o se congelan TODOS los args. Se congelan.


class _StrMutante(str):
    """No-str NI str fijo: cambia lo que renderiza entre lecturas."""
    n = 0

    def __str__(self):
        type(self).n += 1
        return "limpio" if type(self).n <= 1 else VENENO


@pytest.mark.parametrize("args", [
    (["a", VENENO],),                       # arg NO-str: una lista
    ({"k": VENENO},),                       # arg NO-str: un dict
    (123456789 ** 50,),                     # arg NO-str: un entero enorme
    ("ok", VENENO),                         # multi-arg
    ("ok", 1, VENENO),                      # multi-arg con relleno
    (VENENO, VENENO, VENENO),               # multi-arg todo veneno
    (),                                     # sin args
])
def test_ALTO_la_garantia_NO_depende_de_la_ARIDAD_ni_del_TIPO(args):
    m = str(C.GrammarRejected(*args))
    assert not _incumple(m), f"args={len(args)}: {_incumple(m)} · {m[:90]!r}"


def test_ALTO_str_de_cada_objeto_se_llama_UNA_sola_vez():
    """El congelado, medido con un contador. Si se llamara dos veces, la segunda
    lectura seria la envenenada y se guardaria esa."""
    class K(_StrMutante):
        n = 0
    m = str(C.GrammarRejected(K()))
    assert K.n == 1, f"`str()` se llamo {K.n} veces: hay ventana para mentir"
    assert not _incumple(m)
    # y el detalle guardado NO cambia al volver a leerlo
    e = C.GrammarRejected(K())
    assert str(e) == str(e)


@pytest.mark.parametrize("tope", [-1000, -5, -1, 0, 1, 5, len("...(trunc)"), 320])
def test_ALTO_un_tope_NEGATIVO_o_MINUSCULO_sigue_acotando(tope):
    """`t[:-5]` recorta desde el final y devolvia `95` caracteres para `tope=-5`:
    no acotaba, hacia otra cosa. Un invariante que se rompe con una entrada que
    nadie usa HOY sigue siendo un invariante roto."""
    r = C.Journal.detalle_seguro("x" * 500, tope=tope)
    assert len(r) <= max(0, tope), f"tope={tope} -> len={len(r)}"
    assert not _incumple(r)


def test_ALTO_hay_UNA_sola_funcion_de_saneado_y_UNAS_solas_constantes():
    """Habia DOS sanitizadores con DOS pares de constantes que coincidian por
    casualidad. Dos verdades es como `/append` acabo aceptando `8` de `12` tipos:
    el dia que alguien toque una y no la otra, las dos se leen bien."""
    # 🩸 AQUI HABIA UN `is` SOBRE UN ENTERO, y su mutante SOBREVIVIO. CPython
    # comparte el objeto de dos constantes iguales del mismo modulo, asi que
    # `_DETALLE_MAX = 320` y `_DETALLE_MAX = DETALLE_MAX` dan `is -> True` LAS
    # DOS: el test no podia distinguir «derivada» de «reescrita a mano». Es la
    # misma clase que el assert tautologico del centinela: una igualdad que se
    # cumple sola. La derivacion se mide donde vive, en el AST.
    arbol = ast.parse(pathlib.Path(C.__file__).read_text())
    clase = next(n for n in ast.walk(arbol)
                 if isinstance(n, ast.ClassDef) and n.name == "Journal")
    derivadas = {}
    for nodo in clase.body:
        if isinstance(nodo, ast.Assign) and isinstance(nodo.value, ast.Name):
            for objetivo in nodo.targets:
                if getattr(objetivo, "id", "") in ("_DETALLE_MAX", "_MARCA_TRUNCADO"):
                    derivadas[objetivo.id] = nodo.value.id
    assert derivadas == {"_DETALLE_MAX": "DETALLE_MAX",
                         "_MARCA_TRUNCADO": "MARCA_TRUNCADO"}, (
        f"las constantes de `Journal` ya no se DERIVAN de las del modulo: "
        f"{derivadas}. Dos verdades que hoy coinciden por casualidad se parten "
        f"el dia que alguien toque una y no la otra, y las dos se leen bien")
    # y el valor, ademas de la forma
    assert C.Journal._DETALLE_MAX == C.DETALLE_MAX
    assert C.Journal._MARCA_TRUNCADO == C.MARCA_TRUNCADO
    entrada = "acentuado más, símbolo ⇒, control \n, y volumen " + "Z" * 900
    assert C.Journal.detalle_seguro(entrada) == C._saneado(entrada)
    for t in (0, 7, 64, 320):
        assert C.Journal.detalle_seguro(entrada, tope=t) == C._saneado(entrada, t)


def test_ALTO_el_acento_se_PLIEGA_y_no_se_ESCAPA():
    """La decision que hace viable el saneado en la base: sin plegar, los `73`
    mensajes en castellano saldrian como `m\\xe1s` y habria que reescribirlos
    todos. Vive aqui y no en la suite de normalizacion porque es una propiedad de
    `_saneado`, y su mutante tiene que poder seleccionarla."""
    assert C._saneado("más") == "mas"
    assert C._saneado("cañón señal") == "canon senal"
    assert C._saneado("а") == "\\u0430"        # lo que NO decompone, se escapa


# ══ Los DOS `ValueError` publicos, cerrados SIN cambiar su clase ════════════
#
# Estuvieron DOS freezes como limite DECLARADO: `ValueError` no hereda de
# `JournalError`, asi que la garantia de la base no los tocaba y el censo de
# subclases **no podia verlos desde dentro**. El encargo anterior acotaba a
# «toda salida JournalError»; este los incluye.
#
#     :5389  _record_denial     f"motivo `{reason}` fuera del vocabulario…"
#     :5498  record_rejection   idem
#
# 🔑 `record_rejection` es la SUPERFICIE DEL GATEWAY —lo dice su propio docstring
# y el test `..._es_la_puerta_del_gateway_y_esta_ACOTADA`—, asi que `reason` es
# dato EXTERNO, no un enum interno. Y la guarda garantiza que el valor impreso
# **nunca es canonico**: el unico que llega al f-string es el que NO esta en
# `REASON_CODES`. Es el peor sitio posible, no dos residuos.
#
# ⛔ La clase NO cambia: siguen siendo `ValueError`. Lo que cambia es que su
# detalle pasa por el MISMO `_saneado` y el MISMO tope.

MOTIVOS_VENENOSOS = [
    ("LF",        "MOTIVO\nINYECTADO severity=INFO"),
    ("CR",        "MOTIVO\rFAKE"),
    ("NUL",       "MOTIVO\x00X"),
    ("ESC",       "MOTIVO\x1b[31mROJO\x1b[0m"),
    ("TAB",       "MOTIVO\tX"),
    ("no-ASCII",  "MOTIVO_а℮\U0001F600"),
    ("longitud",  "M" * 5000),
    ("todo",      "\nINY \x1b[31m\x00а℮" + "Z" * 3000),
]


@pytest.mark.parametrize("etiqueta,motivo", MOTIVOS_VENENOSOS,
                         ids=[e for e, _ in MOTIVOS_VENENOSOS])
def test_VE_record_rejection_no_refleja_el_crudo(tmp_path, etiqueta, motivo):
    d = journal(tmp_path)
    s = sesion(d, "c", principal="p", role="be")
    with pytest.raises(ValueError) as e:
        d.record_rejection(s.token, motivo)
    m = str(e.value)
    assert not _incumple(m), f"{etiqueta}: {_incumple(m)} · {m[:90]!r}"
    d.close()


@pytest.mark.parametrize("etiqueta,motivo", MOTIVOS_VENENOSOS,
                         ids=[e for e, _ in MOTIVOS_VENENOSOS])
def test_VE_record_denial_no_refleja_el_crudo(tmp_path, etiqueta, motivo):
    """El gemelo interno. Curar uno y dejar el otro es dejar el hueco con otro
    nombre — que es literalmente lo que hice en la primera ronda de esta serie."""
    d = journal(tmp_path)
    with pytest.raises(ValueError) as e:
        d._record_denial("prn_x", motivo, lane="l")
    m = str(e.value)
    assert not _incumple(m), f"{etiqueta}: {_incumple(m)}"
    d.close()


class _MotivoToctou(str):
    """`__str__` da un motivo NO canonico —asi la guarda levanta— y `__format__`
    inyecta. Si el mensaje se construyera con un f-string sobre el objeto VIVO,
    el veneno entraria; con la lectura congelada, `__format__` no se llama."""
    n = 0

    def __str__(self):
        type(self).n += 1
        return "NO_CANONICO"

    def __format__(self, spec):
        type(self).n += 1
        return "\nINYECTADO \x1b[31m" + "Z" * 3000


def test_VE_el_TOCTOU_del_motivo_esta_MUERTO(tmp_path):
    class M(_MotivoToctou):
        n = 0
    d = journal(tmp_path)
    s = sesion(d, "c", principal="p", role="be")
    with pytest.raises(ValueError) as e:
        d.record_rejection(s.token, M("NO_CANONICO"))
    m = str(e.value)
    assert not _incumple(m), _incumple(m)
    assert "INYECTADO" not in m
    assert M.n == 1, f"`str()`/`__format__` se llamo {M.n} veces: hay ventana"
    d.close()


def test_VE_ANTIVACUIDAD_la_guarda_levanta_ANTES_de_resolver_la_identidad(tmp_path):
    """⊕ El discriminante de la ANTI-VACUIDAD, y costó encontrarlo.

    Quitar la guarda de `record_rejection` NO cambia el mensaje: `_record_denial`
    tiene la SUYA y levanta igual, así que todos mis ⊖ seguían verdes por
    defensa en profundidad. Lo que **sí** cambia es el ORDEN: con la guarda, un
    motivo no canónico se rechaza ANTES de resolver identidad; sin ella, el token
    que no resuelve devuelve `None` y el motivo malo **no se rechaza nunca**.

        sano      -> ValueError
        mutante   -> None
    """
    d = journal(tmp_path)
    with pytest.raises(ValueError):
        d.record_rejection("token-que-no-resuelve", "NO_CANONICO")
    d.close()


def test_OMEGA_VE_un_motivo_CANONICO_sigue_registrando(tmp_path):
    """⊕ El control que impide cerrar de más: sanear el rechazo no puede romper
    el camino bueno. Sin esto, un `raise` incondicional pasaria todos los ⊖."""
    d = journal(tmp_path)
    s = sesion(d, "c", principal="p", role="be")
    assert d.record_rejection(s.token, "POLICY_DENIED") is not None
    assert d.record_rejection("token-que-no-resuelve", "POLICY_DENIED") is None
    d.close()


def test_VE_la_clase_NO_ha_cambiado():
    """El encargo dice «sin cambiar su clase». Se fija, porque la tentacion
    obvia era convertirlos en `JournalError` y heredar la garantia gratis — y eso
    habria movido `32`/`21`/`23`, que es justo lo que `D10`/`D11` estan
    cableando."""
    import inspect
    for fn in (C.Journal.record_rejection, C.Journal._record_denial):
        fuente = inspect.getsource(fn)
        assert "raise ValueError(" in fuente, f"{fn.__name__} cambio de clase"
    n = sum(1 for x in dir(C) if isinstance(getattr(C, x), type)
            and issubclass(getattr(C, x), C.JournalError) and getattr(C, x) is not C.JournalError)
    # 🔻 32/21/23 -> 33/22/24 al entrar `IntentTooLarge`/`INTENT_TOO_LARGE` por
    # la cota adjudicada. El `==` esta puesto a proposito: un censo con `>=` no
    # ve entrar ni salir a nadie, y estos tres numeros son los que D10/D11
    # cablean. Cambiarlos obliga a venir aqui y DECIDIR, que es el punto.
    # 🔻 33 -> 35: `AdmissionClosed` + `AdmissionConflict`, ambas con su fila en
    # `REASON_CODES` y en `_POR_MOTIVO`, que es lo que D10/D11 exigen.
    assert n == 35, f"la taxonomia se movio a {n}: D10/D11 dependen de ese numero"
    assert len(C.Journal._POR_MOTIVO) == 24 and len(C.REASON_CODES) == 26


def test_EQUIVALENCIA_DECLARADA_el_congelado_del_motivo_no_tiene_falsador_hoy():
    """⚖️ EQUIVALENCIA DECLARADA, con sus intentos escritos.

    `motivo = str(reason)` en `record_rejection` es **redundante hoy**: su
    mutante (`motivo = reason`) da resultados IDÉNTICOS al sano en las CUATRO
    sondas que construí —mensaje del rechazo, contador de lecturas, `reason`
    persistido en `denials`, y `subject_id` del camino AGREGADO con la cuota
    forzada a `2`—. La razón es que `_saneado()` **ya llama a `str()` por
    dentro**, así que el objeto vivo nunca llega a un f-string.

    **El congelado se queda en el código** por consistencia con las otras dos
    puertas y porque una refactorización que meta `reason` en un f-string lo
    volvería discriminante al instante. **Su mutante se retira del arnés**: uno
    que no puede distinguir ensucia el informe y entrena a ignorar
    supervivientes.

    Este test es el centinela de esa equivalencia: si alguien interpola `reason`
    directamente, se pone rojo y hay que devolverle su mutante.
    """
    import inspect
    fuente = inspect.getsource(C.Journal._record_denial) + inspect.getsource(
        C.Journal.record_rejection)
    assert "motivo = str(reason)" in fuente
    # ⊖ ningún f-string sobre `reason` en las dos puertas: es lo que haría
    #   discriminante al congelado, y hoy no existe.
    assert 'f"motivo `{reason}`' not in fuente, (
        "alguien volvio a interpolar `reason` crudo: el congelado PASA a ser "
        "discriminante y hay que devolverle su mutante al arnes")


# ══ LAS 11 SUPERFICIES DEL AUDITOR, una a una ══════════════════════════════
#
# Mis `13` FAMILIAS de arriba las derive yo; estas `11` son SU lista, con su
# clase esperada. Se prueban las DOS poblaciones porque no son la misma cosa: la
# mia sale de las firmas, la suya de haber ATACADO el sistema. Ninguna contiene a
# la otra, y esa es la razon de no sustituir una por la otra.

ENORME = "Z" * 3000 + "\n\x1b[31m\x00а℮"


def _capaz(d, s):
    """Una sesion con capacidades de operador, para llegar a las puertas que las
    exigen — sin esto, tres de las once mueren en `PolicyDenied` y el test mide
    la puerta de capacidades en vez de la que dice medir."""
    from ._arnes import OPERADOR
    d.bind_credential("c-op", principal="op", role="be", lane="llminbox",
                      capabilities=OPERADOR)
    return d.open_session("c-op")


SUPERFICIES_11 = [
    ("1 kind",            lambda d, s: d.accept_event(s.token, idempotency_key="k",
                              intent={**INTENT, "kind": ENORME}, ledger="llminbox")),
    ("2 recipient",       lambda d, s: d.accept_event(s.token, idempotency_key="k",
                              intent={**INTENT, "to": [ENORME]}, ledger="llminbox")),
    ("3 ledger",          lambda d, s: d.accept_event(s.token, idempotency_key="k",
                              intent=INTENT, ledger=ENORME)),
    ("4 causes",          lambda d, s: d.accept_event(s.token, idempotency_key="k",
                              intent=INTENT, ledger="llminbox", causes=[ENORME])),
    # ⚠️ El recurso se NORMALIZA y se trunca a 120, asi que `ENORME` a secas NO
    # levanta: hay que darle uno que normalice a VACIO, que es el caso que la
    # gramatica rechaza. Medido antes de escribirlo.
    ("5 resource",        lambda d, s: d.acquire_lease(s.token, "!" * 3000, ttl_s=100)),
    ("6a ttl_s open",     lambda d, s: d.open_session("c", ttl_s=-int("9" * 1000))),
    ("6b ttl_s refresh",  lambda d, s: d.refresh_session(s.token, ttl_s=-int("9" * 1000))),
    ("7 fencing_token",   lambda d, s: d.check_fence(s.token, "r", "9" * 1000)),
    ("8 new_state",       lambda d, s: d.advance_command(s.token, "cmd_x", ENORME)),
    ("9 command_id",      lambda d, s: d.advance_command(s.token, ENORME, "received")),
    # ⚠️ `CommandRevisionConflict` necesita la revision DUPLICADA: con un solo
    # envio no hay conflicto y el caso no llega al `raise` que dice medir.
    ("10 workstream_id",  lambda d, s: (d.submit_command(s.token, workstream_id=ENORME,
                              revision=1, payload={"o": "x"}),
                              d.submit_command(s.token, workstream_id=ENORME,
                              revision=1, payload={"o": "y"}))),
    ("11 mark_ledger",    lambda d, s: d.mark_materialized(s.token, "ev_x", entry_eid="e",
                              ledger=ENORME, claim_token="t")),
]


@pytest.mark.parametrize("etiqueta,verbo", SUPERFICIES_11,
                         ids=[e for e, _ in SUPERFICIES_11])
def test_LAS_11_SUPERFICIES_del_auditor_cumplen_la_garantia(tmp_path, etiqueta, verbo):
    d = journal(tmp_path)
    s = _capaz(d, sesion(d, "c", principal="p", role="be"))
    try:
        with pytest.raises(C.JournalError) as e:
            verbo(d, s)
        m = str(e.value)
        assert not _incumple(m), f"{etiqueta}: {_incumple(m)} · {m[:100]!r}"
    finally:
        d.close()


def test_LAS_11_no_es_VACUO_todas_levantan_y_cubren_VARIAS_clases(tmp_path):
    """⊕ Si una dejara de levantar, su fila mediria un `raise` que no ocurre y
    seguiria verde. Y si todas dieran la MISMA clase, el barrido estaria tocando
    una sola puerta con once nombres."""
    clases = set()
    for i, (etiqueta, verbo) in enumerate(SUPERFICIES_11):
        sub = tmp_path / f"s{i}"
        sub.mkdir(parents=True, exist_ok=True)
        d = journal(sub)
        s = _capaz(d, sesion(d, "c", principal="p", role="be"))
        try:
            with pytest.raises(C.JournalError) as e:
                verbo(d, s)
            clases.add(type(e.value).__name__)
        finally:
            d.close()
    assert len(clases) >= 5, f"solo {len(clases)} clases distintas: {sorted(clases)}"
