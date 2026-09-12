"""`detail` TRUNCA, NO RECHAZA — correctivo del operador sobre
`MARK:cto-detail-trunca-y-dimension-no-es-funcion-del-canonico`.

🩸 EL DEFECTO QUE ESTO CIERRA, Y ERA MIO: meti `detail` en la politica de
rechazo con `4 KiB`. Un `detail` de `5 KiB` AUTENTICADO daba `413` y la
transicion NO ocurria — o sea, la operacion fallaba **por el tamaño de su
explicacion**. `detail` es DIAGNOSTICO, no dato de trabajo.

⚖️ Y la parte de metodo: yo argumente que la exencion del ruling se apoyaba en
una medida del detalle de ERROR (`_saneado`, `320`) y no del de TRANSICION (el
`Mapping` persistido). El operador cerro la pregunta —**los dos truncan**— y
esto lo fija como conducta, no como prosa.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

import coordination as C
from ._arnes import journal, sesion

GORDO = {"nota": "A" * 5120}


def _detalle_de(db, estado="received"):
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    return [f["detail"] for f in
            con.execute("SELECT state, detail FROM receipt_transitions")
            if f["state"] == estado][0]


@pytest.fixture()
def puerta(tmp_path):
    d = journal(tmp_path)
    s = sesion(d, "c", principal="p", role="be")
    cid, _ = d.submit_command(s.token, workstream_id="ws", revision=1,
                              payload={"p": 1})
    yield d, s, cid, tmp_path / "coordination.sqlite"
    if not getattr(d, "_cerrado", False):
        try:
            d.close()
        except Exception:
            pass


# ── el corazon del correctivo ─────────────────────────────────────────────

def test_un_detail_de_5KiB_AUTENTICADO_PROSPERA_truncado_y_no_da_413(puerta):
    d, s, cid, db = puerta
    tid = d.advance_command(s.token, cid, "received", detail=GORDO)
    assert tid, "la transicion no ocurrio"
    d.close()
    guardado = _detalle_de(db)
    assert C.MARCA_TRUNCADO in guardado, "el truncado es MUDO: no se delata"
    obj = json.loads(guardado)
    assert "payload_truncado" in obj and "payload" not in obj, (
        f"la clave no delata el corte: {sorted(obj)}")
    assert len(obj["payload_truncado"]) <= C.DETALLE_MAX
    assert len(obj["payload_truncado"].encode("utf-8")) <= C.DETALLE_MAX
    assert "A" * 1000 not in guardado, "entro el cuerpo entero"


def test_CONTROL_un_detail_pequeno_llega_INTACTO_y_SIN_marca(puerta):
    """⊕ Sin este, «trunca» se cumple truncando SIEMPRE."""
    d, s, cid, db = puerta
    d.advance_command(s.token, cid, "received", detail={"nota": "cabe entera"})
    d.close()
    guardado = _detalle_de(db)
    assert C.MARCA_TRUNCADO not in guardado
    assert json.loads(guardado)["payload"] == {"nota": "cabe entera"}


def test_el_detail_grande_NO_levanta_ResourceLimitExceeded(puerta):
    """El falsador directo del defecto: si vuelve el tope, esto se pone rojo."""
    d, s, cid, _ = puerta
    d.advance_command(s.token, cid, "received", detail=GORDO)


# ── y lo que el correctivo NO afloja ──────────────────────────────────────

def test_token_INVALIDO_con_detail_grande_da_AuthError_sin_oraculo(puerta):
    d, s, cid, _ = puerta
    with pytest.raises(C.AuthError) as e:
        d.advance_command("token-que-no-resuelve", cid, "received", detail=GORDO)
    m = str(e.value).lower()
    assert not any(p in m for p in ("limit", "byte", "5120", "resource")), (
        f"el 401 filtra un oraculo de tamaño: {m}")


def test_un_detail_CICLICO_sigue_siendo_422_y_no_un_ValueError_crudo(puerta):
    """Lo que ningun tope vuelve valido sigue fuera: `detail` sale de la
    politica de RECHAZO POR TAMAÑO, no de la de serializabilidad."""
    d, s, cid, _ = puerta
    ciclo: dict = {}
    ciclo["s"] = ciclo
    with pytest.raises(C.OperationInvalid):
        d.advance_command(s.token, cid, "received", detail=ciclo)


# ══ EL TECHO GLOBAL DE `detail` — hallazgo CERRADO ═════════════════════════
#
# 🩸 Y EMPIEZA POR EL DEFECTO DEL TEST QUE HABÍA AQUÍ, porque es la lección.
# La versión anterior fijaba el hallazgo con
#     C._congelar(gordo, "detail", tope_bytes=None)
# y su docstring prometía «debe CAER el día que se cure». **No cayó.** Se aplicó
# la cura entera y siguió VERDE, porque le pasaba el `None` A MANO: medía el
# HELPER con el parámetro que yo le daba, no lo que `advance_command` hace. Un
# test así acredita LA ENTRADA que le pasan, jamás EL ESTADO que nombra — y el
# guard que existía para que la cura no pasara en silencio habría dejado pasar
# en silencio la cura Y su reversión.
# ⇒ Los dos de abajo miden el CALL-SITE: uno por la ruta pública, otro por AST.

def test_el_detail_tiene_el_techo_GLOBAL_y_el_corte_es_INCREMENTAL(puerta):
    """`detail` era el ÚNICO campo de estructura libre del núcleo sin NINGÚN eje
    de tamaño: `tope_bytes=None` no es «sin política», apaga el contador entero
    (`suma()` abre con `if tope_bytes is None: return`). Un valor escalar gigante
    son `2` nodos y profundidad `1`, así que pasaba la puerta y se canonicalizaba
    para persistir `347 B`.

    ⚖️ El tope que lleva ahora es el GLOBAL —`CANONICAL_REQUEST_MAX_BYTES`, el
    techo de cualquier petición— y NO `METADATO_MAX_BYTES`. Esa diferencia es
    todo el punto: lo que el operador prohibió, y `MD1` vigila, es que la
    operación muera por el TAMAÑO DE SU COMENTARIO a `4 KiB`. Un `detail` de
    `5 KiB` sigue prosperando y truncando (el test de arriba lo fija); lo único
    que ya no entra es lo que ninguna petición del núcleo puede traer.
    """
    d, s, cid, _db = puerta
    with pytest.raises(C.ResourceLimitExceeded) as e:
        d.advance_command(s.token, cid, "received",
                          detail={"x": "A" * (8 * 1_000_000)})
    assert e.value.dimension == "bytes", (
        f"dimension={e.value.dimension!r}: si no es `bytes`, lo paró otro eje y "
        f"este test acreditaría la guarda de al lado")
    assert e.value.limit == C.CANONICAL_REQUEST_MAX_BYTES, (
        f"limit={e.value.limit}: el techo tiene que ser el GLOBAL. Si sale "
        f"{C.METADATO_MAX_BYTES}, ha vuelto la política que el operador retiró")
    # ⊕ el corte es INCREMENTAL: la cota inferior es O(1) y NO materializa. Si
    # alguien lo mueve a un `_canonical` del objeto entero esto sigue verde, así
    # que se mide lo observable de que no se recorrió entero: el contador corta
    # ANTES de acabar, o sea `seen_at_least` es el acumulado del corte y el
    # rechazo llega sin haber codificado el cuerpo.
    assert e.value.seen_at_least > C.CANONICAL_REQUEST_MAX_BYTES


def test_CONTROL_un_detail_GRANDE_pero_bajo_el_techo_global_sigue_pasando(puerta):
    """⊖ del anterior, y el que impide «se arregló rechazando más». Un `detail`
    holgadamente por encima de `METADATO_MAX_BYTES` y por debajo del techo global
    tiene que SEGUIR entrando y truncándose: si esto cae, la cura reintrodujo la
    política de rechazo por tamaño de diagnóstico."""
    d, s, cid, db = puerta
    tid = d.advance_command(s.token, cid, "received",
                            detail={"x": "A" * (200 * 1024)})   # 200 KiB
    assert tid, "un detail de 200 KiB tiene que prosperar: está bajo el techo global"
    d.close()
    guardado = _detalle_de(db)
    assert C.MARCA_TRUNCADO in guardado and "payload_truncado" in json.loads(guardado)


def test_el_CALL_SITE_de_detail_lleva_el_techo_GLOBAL_por_SIMBOLO():
    """La otra mitad, por AST: que el `tope_bytes` del call-site sea el SÍMBOLO
    global y no un literal ni otro símbolo. El funcional de arriba pasaría igual
    con `1048576` escrito a mano — y entonces el día que el techo global cambie,
    `detail` se queda con el número viejo y nadie se entera.

    🔻 Este es el guard que a la versión anterior le faltaba: medir el CALL-SITE,
    no el helper con los argumentos que le pase el test."""
    import ast as _ast
    import inspect
    import pathlib as _pl
    arbol = _ast.parse(_pl.Path(inspect.getfile(C)).read_text())
    topes = []
    for nodo in _ast.walk(arbol):
        if not isinstance(nodo, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            continue
        if nodo.name != "advance_command":
            continue
        for llamada in _ast.walk(nodo):
            if not isinstance(llamada, _ast.Call):
                continue
            fn = getattr(llamada.func, "id", None)
            if fn != "_congelar":
                continue
            for kw in llamada.keywords:
                if kw.arg == "tope_bytes":
                    topes.append(_ast.dump(kw.value))
    assert len(topes) == 1, (
        f"se esperaba UN `_congelar` en `advance_command`, hay {len(topes)}: si "
        f"aparece otro, este test ya no sabe cuál es el de `detail`")
    assert topes[0] == _ast.dump(_ast.Name(id="CANONICAL_REQUEST_MAX_BYTES",
                                           ctx=_ast.Load())), (
        f"el `tope_bytes` del call-site no es el símbolo global: {topes[0]}")
