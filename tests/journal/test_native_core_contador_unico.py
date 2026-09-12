"""Contador ÚNICO por operación · raíz de lista · framing exterior · `true`.

**NO-GO independiente a `05bb1330`.** El contador de nodos era POR SUPERFICIE
(`intent` + `trace` + secuencias = `24.576` por petición) y el agregado medía la
SUMA DE VALORES, no el objeto exterior.

🩸 `@qa` (`09:53:23Z`) separó lo que yo había mezclado: *«el agregado nuevo cruza
superficies en BYTES y yo bloqueé el eje de NODOS — `intent` y `trace` SIGUEN con
contador propio, y `1 MiB` de bytes no los acota porque **`24.576` nulos son
`98 KB`**»*. Curé el eje de al lado y lo leí como cobertura del que faltaba.

`@cpo` (`09:47:16Z`): *«el contador es UNO por petición y atraviesa TODOS los
campos; NO se reinicia por superficie»*, con `field="request"` en el exceso.
"""
from __future__ import annotations

import pytest

import coordination as C
from ._arnes import INTENT, journal, sesion

EXT = [{"ledger": "l", "entry_eid": f"e{i}"} for i in range(256)]
DENSO = {**INTENT, "meta": [1] * 8000}          # 8.009 nodos · ~16 KB


@pytest.fixture()
def puerta(tmp_path):
    d = journal(tmp_path)
    yield d, sesion(d, "c", principal="p", role="be")
    d.close()


def test_el_contador_de_nodos_cruza_los_campos_de_la_operacion(puerta):
    """⊖⊕ `intent` 8.009 + `external_causes` 769 = 8.778 > 8.192."""
    d, s = puerta
    with pytest.raises(C.ResourceLimitExceeded) as e:
        d.accept_event(s.token, idempotency_key="cf", intent=DENSO,
                       ledger="llminbox", external_causes=EXT)
    assert e.value.dimension == "nodes"
    # ⊕ el MISMO intent SOLO entra: lo que rechaza es la SUMA, no el campo.
    assert d.accept_event(s.token, idempotency_key="cf2", intent=DENSO,
                          ledger="llminbox").event_id


def test_el_exceso_cruzado_se_reporta_como_field_request(puerta):
    """El exceso de un contador compartido no pertenece a ningún campo:
    nombrar uno sería acusar al último que sumó, que es un accidente del orden."""
    d, s = puerta
    with pytest.raises(C.ResourceLimitExceeded) as e:
        d.accept_event(s.token, idempotency_key="rq", intent=DENSO,
                       ledger="llminbox", external_causes=EXT)
    assert e.value.field == "request"


def test_la_raiz_de_una_lista_cuenta_un_nodo():
    """⊖⊕ El canónico emite `[...]`, o sea un VALOR."""
    p = [0]
    C._congelar_secuencia(["a"], "causes", nodos=p)
    assert p[0] == 2, "raíz + elemento"
    q = [0]
    C._congelar_secuencia([], "causes", nodos=q)
    assert q[0] == 1, "la raíz existe aunque no haya elementos"


@pytest.mark.parametrize("nombre", ["completo", "minimo"])
def test_el_agregado_es_el_canonico_del_objeto_exterior(nombre):
    """El agregado mide el objeto EXTERIOR: nombres de campo, comillas, dos
    puntos, comas y llaves. Sumar los canónicos de los valores da otro número, y
    un límite que el cliente no puede reproducir no es un contrato."""
    tr = {"t": 1, "z": True, "w": False, "n": None} if nombre == "completo" else None
    cau = ["evt_a", "evt_b"] if nombre == "completo" else []
    ext = EXT[:2] if nombre == "completo" else []
    ag = C._AgregadoPeticion()
    p = [0]
    C._congelar(INTENT, "intent", tope_bytes=C.PAYLOAD_MAX_BYTES, nodos=p,
                agregado=ag, obligatorio=True)
    C._congelar(tr, "trace", tope_bytes=C.METADATO_MAX_BYTES, nodos=p, agregado=ag)
    C._congelar_secuencia(cau, "causes", nodos=p, agregado=ag)
    C._congelar_secuencia(ext, "external_causes", nodos=p, agregado=ag)
    exterior = {"intent": INTENT, "causes": cau, "external_causes": ext}
    if tr is not None:
        exterior["trace"] = tr
    assert ag.total == len(C._canonical(exterior).encode("utf-8"))


@pytest.mark.parametrize("valor", [True, False])
def test_true_cuesta_4_y_false_5(valor):
    """`true` son 4 caracteres y `false` 5. Cobrar `5` a los dos SOBREESTIMA
    para `True`, y una cota inferior que sobreestima rechaza lo que cabe."""
    doc = {f"k{i}": valor for i in range(200)}
    exacto = len(C._canonical(doc).encode("utf-8"))
    assert C._congelar(doc, "trace", tope_bytes=exacto) is not None
    with pytest.raises(C.ResourceLimitExceeded):
        C._congelar(doc, "trace", tope_bytes=exacto - 1)


def test_el_limite_local_decide_ANTES_que_el_agregado(puerta):
    """⊖ Con el local Y el agregado excedidos, gana el LOCAL.

    🔑 En régimen MULTIBYTE, que es el único donde SÓLO el pase exacto puede
    cazarlo: `40.000` caracteres `ñ` son `40.000` de cota inferior —bajo el
    tope— y `~80.000` bytes reales, por encima."""
    d, s = puerta
    multi = {**INTENT, "body": "ñ" * 40_000}
    assert len(C._canonical(multi)) <= C.PAYLOAD_MAX_BYTES < \
        len(C._canonical(multi).encode("utf-8"))
    with pytest.raises(C.ResourceLimitExceeded) as e:
        d.accept_event(s.token, idempotency_key="pr", intent=multi,
                       ledger="llminbox", external_causes=EXT)
    assert e.value.field == "intent" and e.value.limit == C.PAYLOAD_MAX_BYTES


def test_el_enum_de_field_vive_en_dato():
    """Un enum que sólo vive en la prosa no es un enum, es una intención."""
    assert isinstance(C.CAMPOS_CONTRATO_CORE, frozenset)
    assert "request" in C.CAMPOS_CONTRATO_CORE
    with pytest.raises(AssertionError):
        C._rle("bytes", "causes[0]", 1, 2, "x")
    assert C._rle("bytes", "causes", 1, 2, "x") is not None


def test_intent_nulo_es_rechazo_de_forma(puerta):
    """⊖⊕ `intent`/`payload` son obligatorios: `None` no es ausencia gratis."""
    d, s = puerta
    with pytest.raises(C.OperationInvalid):
        d.accept_event(s.token, idempotency_key="nl", intent=None, ledger="llminbox")
    # ⊕ `trace=None` SÍ es ausencia válida.
    assert d.accept_event(s.token, idempotency_key="nl2", intent=INTENT,
                          ledger="llminbox", trace=None).event_id


# ══ CENSO · toda RLE PÚBLICA lleva `field` DEL ENUM ═════════════════════════
#
# 🔴 Lo pide `@qa` (§4 sobre `bb5fad94`), y nace de una divergencia REAL: la
# guarda de profundidad de `_sin_atribucion` construía `ResourceLimitExceeded`
# **directa, sin pasar por `_rle`**, y publicaba el CAMINO en `field`
# (`intent.meta.n.n.n…`) — fuera del enum cerrado. Latente en sano (el congelador
# corta antes) y VIVA justo en el escenario que justifica la redundancia.
#
# `test_el_enum_de_field_vive_en_dato` (arriba) ejercita el camino GUARDADO: le
# pregunta a `_rle` si acepta basura. **No ve al que ELUDE `_rle`.** Estos dos
# cierran esa mitad: uno por construcción, otro por ruta pública.

def test_CENSO_solo_rle_construye_ResourceLimitExceeded():
    """Por AST, sobre el fuente: `ResourceLimitExceeded(...)` se construye en UN
    solo sitio, y ese sitio está DENTRO de `_rle`.

    Es el censo por CONSTRUCCIÓN, y es el que de verdad cierra la clase: mientras
    la única puerta sea `_rle`, el enum queda impuesto para toda RLE presente y
    futura. Un censo por ruta pública sólo cubre las rutas que se me ocurran hoy.
    """
    import ast as _ast
    import inspect
    import pathlib as _pl
    arbol = _ast.parse(_pl.Path(inspect.getfile(C)).read_text())
    dentro_de_rle, fuera = [], []
    for fn in _ast.walk(arbol):
        if not isinstance(fn, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            continue
        for n in _ast.walk(fn):
            if isinstance(n, _ast.Call) and getattr(n.func, "id", None) == "ResourceLimitExceeded":
                (dentro_de_rle if fn.name == "_rle" else fuera).append((fn.name, n.lineno))
    assert len(dentro_de_rle) == 1, (
        f"se esperaba UNA construcción dentro de `_rle`, hay {dentro_de_rle}: si "
        f"son 0 el censo no está mirando nada")
    assert not fuera, (
        f"hay RLE construidas FUERA de `_rle`: {fuera}. Cada una elude la guarda "
        f"del enum y puede publicar un `field` que el contrato no enumera — que "
        f"es ampliar el wire por accidente")


def test_CENSO_por_RUTA_PUBLICA_el_field_de_toda_RLE_esta_en_el_ENUM(monkeypatch, tmp_path):
    """El otro brazo: se provocan RLE por la ruta pública y se exige `field` del
    enum en TODAS — incluida **la ruta que hoy destapó la divergencia**, con el
    `prof_max` del congelador neutralizado para que conteste la segunda guarda.

    ⚠️ Sin ese último caso este censo saldría verde midiendo el vacío: en sano
    esa rama no se alcanza nunca, así que un censo que sólo recorra el camino
    feliz nunca vería el `field` ilegal. Es el ⊖ que `@qa` declaró obligatorio.
    """
    def hondo(n=200):
        d = {"n": None}
        cur = d
        for _ in range(n):
            cur["n"] = {"n": None}
            cur = cur["n"]
        return d

    def cosecha(d, s, marca=""):
        """⚠️ LAS DOS PUERTAS PÚBLICAS, no una. `@qa` lo pide en su `F1` y su cota
        nº5 lo marca: el hallazgo se ejerció sólo por `accept_event`, y en
        `submit_command` el orden de las guardas se verificó **por lectura**, no
        por corrida. Un censo que recorre una puerta acredita esa puerta."""
        vistos = []
        eventos = (
            ("depth", dict(intent={**INTENT, "meta": hondo()})),
            ("bytes", dict(intent={**INTENT, "x": "A" * (C.PAYLOAD_MAX_BYTES + 10)})),
            ("nodes", dict(intent={**INTENT, "m": {f"k{i}": i for i in range(C.NODOS_MAX)}})),
            ("cardinality", dict(intent=INTENT, causes=["ev_x"] * (C.CARDINALIDAD_MAX + 1))),
        )
        for i, (etiqueta, kw) in enumerate(eventos):
            try:
                d.accept_event(s.token, idempotency_key=f"c{marca}{i}",
                               ledger="llminbox", **kw)
            except C.ResourceLimitExceeded as e:
                vistos.append((f"EVENT/{etiqueta}{marca}", e.dimension, e.field))
            except C.JournalError:
                pass          # otro eje contestó primero: no es objeto de ESTE censo
        comandos = (
            ("depth", dict(payload={"meta": hondo()})),
            ("bytes", dict(payload={"x": "A" * (C.PAYLOAD_MAX_BYTES + 10)})),
            ("cardinality", dict(payload={"p": 1},
                                 causes=["ev_x"] * (C.CARDINALIDAD_MAX + 1))),
        )
        for i, (etiqueta, kw) in enumerate(comandos):
            try:
                d.submit_command(s.token, workstream_id=f"w{marca}{i}", revision=1, **kw)
            except C.ResourceLimitExceeded as e:
                vistos.append((f"COMMAND/{etiqueta}{marca}", e.dimension, e.field))
            except C.JournalError:
                pass
        return vistos

    d = journal(tmp_path)
    s = sesion(d, "c", principal="p", role="be")
    vistos = cosecha(d, s)

    # ⟵ LA RUTA DE LA DIVERGENCIA: sólo la PRIMERA guarda neutralizada
    original = C._congelar

    def sin_tope_de_profundidad(obj, campo, **kw):
        kw["prof_max"] = 10 ** 9
        return original(obj, campo, **kw)

    monkeypatch.setattr(C, "_congelar", sin_tope_de_profundidad)
    vistos += [(v[0] + "/2a-capa",) + v[1:] for v in cosecha(d, s, marca="n")]
    d.close()

    puertas = {v[0].split("/")[0] for v in vistos}
    assert puertas == {"EVENT", "COMMAND"}, (
        f"el censo sólo recorrió {puertas}: `F1` de `@qa` pide LAS DOS puertas "
        f"públicas, y su cota nº5 marca que `submit_command` estaba sin correr")
    assert len(vistos) >= 5, (
        f"el censo recogió {len(vistos)} RLE: con tan pocas no está recorriendo "
        f"las rutas que dice recorrer — un verde aquí no significaría nada")
    fuera = [v for v in vistos if v[2] not in C.CAMPOS_CONTRATO_CORE]
    assert not fuera, (
        f"RLE con `field` FUERA del enum cerrado: {fuera}. El contrato de error "
        f"se amplió sin que nadie lo decidiera")
    assert any(v[0].endswith("/2a-capa") for v in vistos), (
        "no se ejerció la ruta de la segunda guarda: sin ella este censo mide el "
        "camino feliz y el ⊖ de `@qa` no está cubierto")
