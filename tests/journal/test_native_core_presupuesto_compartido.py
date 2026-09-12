"""B1 · el techo de NODOS se cuenta UNA VEZ por peticion.

🩸 EL DEFECTO, Y ERA MIO: el acumulador nacia FRESCO en cada `_congelar`, y
`_congelar_secuencia` llama una vez POR ELEMENTO ⇒ el techo real era
`cardinalidad x nodos_max` = `256 x 8.192` = **`2.097.152` nodos**, no `8.192`.
Lo midio `@qa` y `@cpo` lo convirtio en regla (`08:52`): *«el techo compuesto se
cuenta UNA VEZ por peticion»* — con la frase que manda: **«esa es la CONDICION,
no el numero»**. Un contador que se reinicia por elemento no acota nada agregado.

⚠️ Se comparten los NODOS, **no los BYTES**: el eje de bytes esta declarado POR
ELEMENTO (`256` elementos ∧ `4 KiB` cada uno), asi que acumularlo convertiria
`4 KiB` por elemento en `4 KiB` en total — otro contrato, y mas apretado.
"""
from __future__ import annotations

import pytest

import coordination as C
from ._arnes import INTENT, journal, sesion


def _elem(v: int) -> dict:
    """Elemento de `v` valores ⇒ `v+1` nodos, y pequeño en bytes."""
    return {f"k{i}": i for i in range(v)}


def _de_bytes(nb: int) -> dict:
    return {"k": "A" * (nb - len(C._canonical({"k": ""}).encode("utf-8")))}


def test_el_techo_de_nodos_se_cuenta_UNA_VEZ_entre_elementos():
    """⊖ `100 x 101 = 10.100` nodos repartidos en elementos PEQUEÑOS."""
    assert len(C._canonical(_elem(100)).encode("utf-8")) < C.ELEMENTO_MAX_BYTES, (
        "el elemento ya pasa el tope de bytes: el falsador mediria OTRO eje")
    with pytest.raises(C.ResourceLimitExceeded) as e:
        C._congelar_secuencia([_elem(100)] * 100, "causes")
    assert e.value.dimension == "nodes"


def test_CONTROL_la_misma_forma_por_debajo_del_techo_ACEPTA():
    """⊕ Sin esto, «rechaza» se cumple rechazando siempre."""
    assert len(C._congelar_secuencia([_elem(100)] * 80, "causes")) == 80


def test_las_DOS_secuencias_comparten_el_presupuesto():
    """⊖ `causes` gasta y `external_causes` hereda lo gastado."""
    p = [0]
    C._congelar_secuencia([_elem(100)] * 40, "causes", nodos=p)
    assert p[0] > 0, "la primera secuencia no gasto nada del presupuesto comun"
    with pytest.raises(C.ResourceLimitExceeded) as e:
        C._congelar_secuencia([_elem(100)] * 45, "external_causes", nodos=p)
    assert e.value.dimension == "nodes"


def test_CONTROL_con_presupuestos_SEPARADOS_las_mismas_dos_entran():
    """⊕ El par que prueba que lo que rechaza es COMPARTIR, no el tamaño."""
    assert C._congelar_secuencia([_elem(100)] * 40, "causes", nodos=[0])
    assert C._congelar_secuencia([_elem(100)] * 45, "external_causes", nodos=[0])


def test_los_bytes_siguen_siendo_POR_ELEMENTO():
    """⊕⊖ `10 x 4 KiB = 40 KiB` entra; UN elemento de `4.097` no."""
    assert len(C._congelar_secuencia([_de_bytes(4096)] * 10, "causes")) == 10
    with pytest.raises(C.ResourceLimitExceeded):
        C._congelar_secuencia([_de_bytes(4097)], "causes")


def test_la_frontera_del_techo_compartido_es_N_y_N_mas_1():
    # 🔻 REANCLADO a la raiz nueva: cada secuencia aporta ADEMAS su RAIZ, asi
    # que dos secuencias de 128 elementos son 128+128+2 = 258 nodos, no 256.
    p = [C.NODOS_MAX - 258]
    C._congelar_secuencia(["a"] * 128, "causes", nodos=p)
    C._congelar_secuencia(["a"] * 128, "external_causes", nodos=p)
    q = [C.NODOS_MAX - 257]
    C._congelar_secuencia(["a"] * 128, "causes", nodos=q)
    with pytest.raises(C.ResourceLimitExceeded) as e:
        C._congelar_secuencia(["a"] * 128, "external_causes", nodos=q)
    assert e.value.seen_at_least == C.NODOS_MAX + 1


def test_el_presupuesto_NO_persiste_entre_peticiones(tmp_path):
    """⊖⊕ Dos peticiones gordas rechazan IGUAL, y las pequeñas siguen entrando:
    el presupuesto es de la peticion, no del proceso."""
    d = journal(tmp_path)
    s = sesion(d, "c", principal="p", role="be")
    gordas = [_elem(100)] * 100
    for i in range(2):
        with pytest.raises(C.ResourceLimitExceeded):
            d.accept_event(s.token, idempotency_key=f"g{i}", intent=INTENT,
                           ledger="llminbox", external_causes=gordas)
    ids = {d.accept_event(s.token, idempotency_key=f"k{i}", intent=INTENT,
                          ledger="llminbox").event_id for i in range(3)}
    assert len(ids) == 3, "una peticion limpia se contamino del presupuesto de otra"
    d.close()


# ══ B7 · CENSO EJECUTABLE del presupuesto — por AST, no por texto ═══════════
#
# 🔴 POR QUÉ ES UN CENSO Y NO UN CASO: los tests de arriba prueban que UNA
# petición comparte su contador. Eso acredita las llamadas que ejercen, jamás la
# POBLACIÓN — y la pregunta del canon (`@cpo` F-N-10: «un solo contador que
# atraviesa TODOS los campos de la operación y NO se reinicia por superficie»)
# es sobre la población entera. Un campo nuevo añadido mañana sin `nodos=` no
# rompe ningún caso de arriba: simplemente no lo mira nadie.
#
# ⚖️ Y fija el veredicto de los TRES aislados, que se revisó y NO se cambió:
# `attestation` (arranque, la norma lo excluye), `annotations` (único campo de
# `open_session`) y `detail` (único campo de `advance_command`). En una petición
# de UN SOLO campo, contador autónomo ≡ contador por petición: **no se puede
# reiniciar lo que no se reparte.** Si alguien les añade un segundo campo, la
# equivalencia muere — y este censo es lo que lo dice.

_AISLADOS_DECLARADOS = {
    # método            campo          por qué su contador autónomo es legítimo
    "__init__":        ("attestation", "del ARRANQUE, no campo de peticion: la norma "
                                       "de @cpo lo excluye del agregado explicitamente"),
    "open_session":    ("annotations", "UNICO campo de estructura libre de SESSION"),
    "advance_command": ("detail",      "UNICO campo de estructura libre de TRANSITION"),
}


def _censo_congelados():
    """Devuelve {metodo: [(campo, comparte_nodos, suma_agregado), ...]}, por AST.

    ⚠️ POBLACIÓN, dicha aquí porque un censo se lee por lo que EXCLUYE: sólo
    entran los MÉTODOS (primer parámetro `self`), que son los que reciben una
    petición. Se quedan fuera los helpers de módulo — `_congelar_secuencia`
    llama a `_congelar` una vez POR ELEMENTO, y esa llamada NO es un campo de
    petición: es el recorrido interno de uno. **El criterio es estructural
    (`self`), no una lista de nombres**: una allowlist por nombre recorta el
    universo hasta que cuadra, y entonces el censo mide su propia lista.
    ⊕ que la exclusión no tapa nada: esa llamada interna SÍ pasa `nodos=`
    compartido — es justo lo que `B1` de arriba mide caso a caso.
    """
    import ast as _ast
    import inspect
    import pathlib
    fuente = pathlib.Path(inspect.getfile(C)).read_text()
    arbol = _ast.parse(fuente)
    fuera = {}
    for nodo in _ast.walk(arbol):
        if not isinstance(nodo, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            continue
        if not (nodo.args.args and nodo.args.args[0].arg == "self"):
            continue
        for llamada in _ast.walk(nodo):
            if not isinstance(llamada, _ast.Call):
                continue
            fn = getattr(llamada.func, "id", None) or getattr(llamada.func, "attr", None)
            if fn not in ("_congelar", "_congelar_secuencia"):
                continue
            if not llamada.args or not isinstance(llamada.args[-1], _ast.Constant):
                campo = "?"
            else:
                campo = llamada.args[-1].value
            kw = {k.arg for k in llamada.keywords}
            fuera.setdefault(nodo.name, []).append((campo, "nodos" in kw, "agregado" in kw))
    return fuera


def test_B7_CENSO_toda_peticion_MULTICAMPO_comparte_su_contador():
    """F-N-10 sobre la POBLACIÓN: en cuanto un método congela DOS o más campos,
    todos comparten `nodos=` y suman al `agregado=`. Sin esto, el contador se
    reinicia por superficie y `8.192` pasa a ser `8.192 × nº de campos`."""
    censo = _censo_congelados()
    multi = {m: c for m, c in censo.items() if len(c) >= 2}
    assert multi, "el censo salió vacío: la aguja no lee, no es que no haya llamadas"
    for metodo, campos in multi.items():
        sueltos = [c for c, comparte, _ in campos if not comparte]
        assert not sueltos, (
            f"{metodo}() congela {len(campos)} campos y {sueltos} NO reciben `nodos=`: "
            f"su contador se reinicia por superficie")
        sin_agregado = [c for c, _, suma in campos if not suma]
        assert not sin_agregado, (
            f"{metodo}(): {sin_agregado} no suman al `agregado=` — un campo del "
            f"objeto exterior que no se cuenta vuelve el agregado una cota inferior")


def test_B7_CENSO_los_congelados_AISLADOS_son_exactamente_los_declarados():
    """⊖ del test de arriba, y la mitad que de verdad protege: el otro sólo mira
    los métodos MULTI-campo, así que un campo nuevo en un método de UNO se le
    escapa entero. Aquí la población son los aislados, y la lista es CERRADA.

    Si este test cae con un método nuevo, la pregunta NO es «añádelo a la lista»:
    es si ese método tiene ya DOS campos, porque entonces su contador autónomo
    dejó de ser equivalente al de la petición."""
    censo = _censo_congelados()
    aislados = {m: c[0][0] for m, c in censo.items() if len(c) == 1}
    esperado = {m: v[0] for m, v in _AISLADOS_DECLARADOS.items()}
    assert aislados == esperado, (
        f"la población de congelados aislados cambió.\n"
        f"  medido:   {aislados}\n  declarado: {esperado}\n"
        f"Un aislado nuevo NO se añade a la lista sin decir por qué su contador "
        f"autónomo sigue siendo el de su petición.")


def _tope_bytes_del_call_site(metodo):
    """El `tope_bytes` REAL del único `_congelar` de un método, por AST.

    🩸 EXISTE POR UN FALLO MEDIDO: la versión anterior de este bloque comparaba
    `DETALLE_MAX` (320) con el agregado y concluía «inalcanzable». Cuando a
    `detail` se le puso el techo GLOBAL en la puerta, el test **siguió verde**:
    `320` es el techo de la FILA PERSISTIDA, no el de ENTRADA. Decía una verdad
    sobre el símbolo equivocado, que es la forma más cara de estar verde.
    """
    import ast as _ast
    import inspect
    import pathlib as _pl
    arbol = _ast.parse(_pl.Path(inspect.getfile(C)).read_text())
    for nodo in _ast.walk(arbol):
        if not isinstance(nodo, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            continue
        if nodo.name != metodo:
            continue
        for llamada in _ast.walk(nodo):
            if isinstance(llamada, _ast.Call) and getattr(llamada.func, "id", None) == "_congelar":
                for kw in llamada.keywords:
                    if kw.arg == "tope_bytes":
                        if isinstance(kw.value, _ast.Name):
                            return kw.value.id
                        if isinstance(kw.value, _ast.Constant):
                            return kw.value.value
                        return "?"
    return None


def test_B7_ninguna_peticion_de_UN_SOLO_campo_necesita_exigir_el_agregado():
    """El punto que se revisó y NO se cambió, con el motivo de cada uno — y son
    motivos DISTINTOS, que es justo lo que un «no hace falta» a secas esconde:

    · `SESSION/open_session`  — `annotations` tope `4.096 B` + sobre `16 B` =
      `4.112 B` contra `1 MiB`: **INALCANZABLE**. Exigir el agregado ahí sería un
      control que no puede disparar, y un control que no dispara no es una
      garantía: es decoración que se lee como una.
    · `TRANSITION/advance_command` — `detail` YA lleva el techo global EN LA
      PUERTA desde la cura del hallazgo: **REDUNDANTE**, no vacío. Si alguien se
      lo quita, el que avisa es `SUITE_D`, no éste.

    ⚠️ Y es la ALARMA de ambos: si `annotations` sube o `detail` pierde su techo,
    esto cae y el veredicto hay que rehacerlo. Sin él, caduca en silencio."""
    SOBRE_SESSION = 16      # `@cpo` §1.2, framing del objeto exterior de SESSION

    techo_session = C.METADATO_MAX_BYTES + SOBRE_SESSION
    assert techo_session < C.CANONICAL_REQUEST_MAX_BYTES, (
        f"SESSION: techo={techo_session} B ya alcanza el agregado "
        f"({C.CANONICAL_REQUEST_MAX_BYTES} B) — deja de ser inalcanzable y esa "
        f"petición necesita `_exige_agregado`")

    tope_detail = _tope_bytes_del_call_site("advance_command")
    assert tope_detail == "CANONICAL_REQUEST_MAX_BYTES", (
        f"TRANSITION: el `tope_bytes` de `detail` es {tope_detail!r} y no el "
        f"símbolo global. Sin él, `detail` vuelve a admitir trabajo NO ACOTADO "
        f"y la conclusión 'no necesita agregado' se cae con ella")
