"""Agregado canónico de la petición · `field` como enum cerrado · `None` valor.

Arbitraje: **RECHAZA** bajar `ELEMENTO_MAX_BYTES` a `1.024` — se queda en
`4.096` (UTF-8 del JSON canónico, inclusivo) — y manda
`CANONICAL_REQUEST_MAX_BYTES = 1.048.576`, **símbolo distinto** del
`RAW_BODY_MAX_BYTES` del Gateway, que NO se define en Core.

🔑 **Y no están ordenados**: `1e10` son `4` bytes crudos y `13` canónicos
(`3,25x`); al revés, `20 MB` de espacios colapsan a `49 B`. Un tope sobre el
crudo NO acota el canónico, y ninguno sustituye al otro.

**Precedencia**: ① forma · ② límites locales · ③ agregado canónico AL FINAL.
"""
from __future__ import annotations

import pytest

import coordination as C
from ._arnes import INTENT, journal, sesion

CAUSA = {"ledger": "l", "entry_eid": "e" + "A" * 3900}


def _intent_de(nb: int) -> dict:
    base = {**INTENT, "body": ""}
    return {**INTENT, "body": "A" * (nb - len(C._canonical(base).encode("utf-8")))}


@pytest.fixture()
def puerta(tmp_path):
    d = journal(tmp_path)
    yield d, sesion(d, "c", principal="p", role="be")
    d.close()


def test_los_dos_simbolos_de_tamano_son_distintos():
    assert C.CANONICAL_REQUEST_MAX_BYTES == 1048576
    assert C.ELEMENTO_MAX_BYTES == 4096, "el arbitraje rechazó bajarlo a 1.024"
    assert not hasattr(C, "RAW_BODY_MAX_BYTES"), "el tope del wire no es de Core"


def test_el_agregado_canonico_rechaza_cruzando_campos(puerta):
    """⊖⊕ Ningún campo rompe SU tope local; el TOTAL sí."""
    d, s = puerta
    it = _intent_de(C.PAYLOAD_MAX_BYTES)
    n_el = len(C._canonical(CAUSA).encode("utf-8"))
    assert n_el <= C.ELEMENTO_MAX_BYTES
    k = (C.CANONICAL_REQUEST_MAX_BYTES - len(C._canonical(it).encode()) - 2) // (n_el + 1)
    assert d.accept_event(s.token, idempotency_key="ok", intent=it,
                          ledger="llminbox", external_causes=[CAUSA] * k).event_id
    with pytest.raises(C.ResourceLimitExceeded) as e:
        d.accept_event(s.token, idempotency_key="ko", intent=it,
                       ledger="llminbox", external_causes=[CAUSA] * (k + 1))
    assert e.value.field == "request" and e.value.dimension == "bytes"
    assert e.value.seen_at_least > C.CANONICAL_REQUEST_MAX_BYTES


def test_el_agregado_NO_persiste_entre_peticiones(puerta):
    """⊕ La misma petición al límite, tres veces: las tres entran."""
    d, s = puerta
    it = _intent_de(60_000)
    ids = {d.accept_event(s.token, idempotency_key=f"r{i}", intent=it,
                          ledger="llminbox", external_causes=[CAUSA] * 200).event_id
           for i in range(3)}
    assert len(ids) == 3


def test_los_separadores_de_la_lista_cuentan_en_el_agregado(puerta):
    """⊖ Frontera construida para que lo que sobra sean los SEPARADORES: con
    ellos `1.048.577`; sin ellos `1.048.325`, que pasaría."""
    d, s = puerta
    k = 251
    n_el = len(C._canonical(CAUSA).encode("utf-8"))
    sep = 2 + (k - 1)
    falta = C.CANONICAL_REQUEST_MAX_BYTES + 1 - (k * n_el + sep)
    it = _intent_de(falta)
    assert falta <= C.PAYLOAD_MAX_BYTES
    assert falta + k * n_el <= C.CANONICAL_REQUEST_MAX_BYTES, "sin separadores pasaría"
    with pytest.raises(C.ResourceLimitExceeded):
        d.accept_event(s.token, idempotency_key="sep", intent=it,
                       ledger="llminbox", external_causes=[CAUSA] * k)


def test_el_crudo_no_acota_el_canonico():
    """`1e10`: `4` bytes crudos, `13` canónicos."""
    assert len(C._canonical(1e10)) > len("1e10")


@pytest.mark.parametrize("ch,minimo", [("\U0001F600", 4), ("\x01", 6)])
def test_la_unidad_es_el_byte_del_canonico_emoji_y_escapes(ch, minimo):
    assert len(C._canonical({"k": ch}).encode("utf-8")) - 7 >= minimo


def test_diez_MB_se_rechazan_con_trabajo_acotado():
    import tracemalloc
    enorme = {"body": "A" * 10_000_000}
    tracemalloc.start()
    try:
        with pytest.raises(C.ResourceLimitExceeded):
            C._congelar(enorme, "intent", tope_bytes=C.PAYLOAD_MAX_BYTES)
        pico = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert pico < 2 * C.PAYLOAD_MAX_BYTES, f"pico {pico} B: se materializó"


def test_el_field_de_exceso_de_elemento_es_un_enum_cerrado():
    """⊖⊕ El `field` es `causes` | `external_causes`; el índice es prosa."""
    gordo = {"ledger": "l", "entry_eid": "A" * 5000}
    campos = set()
    for campo in ("causes", "external_causes"):
        with pytest.raises(C.ResourceLimitExceeded) as e:
            C._congelar_secuencia([gordo], campo)
        campos.add(e.value.field)
        assert "elemento 0" in str(e.value), "el índice no está en el detalle"
    assert campos == {"causes", "external_causes"}


def test_un_None_de_ELEMENTO_cuenta_como_nodo():
    """⊖ `causes=[None]*100` gasta `101` nodos: la RAÍZ de la lista más los
    `100` valores. Antes salían gratis los `101`."""
    p = [0]
    C._congelar_secuencia([None] * 100, "causes", nodos=p)
    assert p[0] == 101


def test_seq_None_es_FORMA_INVALIDA_y_NO_se_normaliza_a_lista_vacia():
    """⊖⊕ `None` y `[]` NO son lo mismo, y el par es el falsador.

    Normalizar `None` a `[]` en silencio hacía tres daños a la vez: el cliente
    creía haber mandado causas y no las mandaba, el campo desaparecía del
    AGREGADO sin decirlo, y un `None` por error de serialización pasaba por
    «sin causas». La firma es `Sequence`: `None` no es una secuencia vacía.
    """
    with pytest.raises(C.OperationInvalid):
        C._congelar_secuencia(None, "causes")
    # ⊕ `[]` SÍ es válido, y NO es gratis: cuenta su raíz y entra en el agregado.
    p = [0]
    ag = C._AgregadoPeticion()
    assert C._congelar_secuencia([], "causes", nodos=p, agregado=ag) == []
    assert p[0] == 1, "la raíz de la lista vacía es un nodo"
    assert ag.total == len(C._canonical({"causes": []}).encode("utf-8"))


def test_seq_None_es_invalida_en_LAS_DOS_puertas(puerta):
    """La misma regla en `Event` y en `Command`: curar una puerta y dejar la
    otra es dejar el hueco con otro nombre."""
    d, s = puerta
    with pytest.raises(C.OperationInvalid):
        d.accept_event(s.token, idempotency_key="sn1", intent=INTENT,
                       ledger="llminbox", causes=None)
    with pytest.raises(C.OperationInvalid):
        d.submit_command(s.token, workstream_id="ws", revision=1,
                         payload={"p": 1}, external_causes=None)


def test_CONTROL_un_campo_ausente_None_no_cuenta():
    """⊕ `trace=None` es AUSENCIA, no un valor: el canónico no lo emite."""
    p = [0]
    C._congelar(None, "trace", tope_bytes=C.METADATO_MAX_BYTES, nodos=p)
    assert p[0] == 0
    q = [0]
    C._congelar({"a": None, "b": 1}, "intent", tope_bytes=C.PAYLOAD_MAX_BYTES, nodos=q)
    assert q[0] == 3, "un None ANIDADO sí es un valor"
