"""Cota del `intent` · adjudicada por `@cpo` (`03:57:55Z`) y `@contratosbik`
(contrato wire `03:58:19Z`).

    len(_canonical(intent).encode("utf-8")) <= 65536   ->  ACEPTA
                                            == 65537   ->  RECHAZA   RESOURCE_LIMIT_EXCEEDED

⛔ ESTE ENCABEZADO MENTIA y las aserciones de abajo ya eran las buenas: decia
`32768`/`32769` y `INTENT_TOO_LARGE`, cuando el sujeto lleva
`PAYLOAD_MAX_BYTES = 65536` y el ruling RETIRO `INTENT_TOO_LARGE` antes de
nacer (el codigo vivo es `RESOURCE_LIMIT_EXCEEDED`, por `ResourceLimitExceeded`).
Un lector que se fiara del encabezado y no del cuerpo se llevaba los DOS
numeros y el codigo mal.

🔴 EL SUELO, medido por mi mano sobre `aab2080` antes de tocar nada:

    intent de 5 MB ....... ACEPTADO · journal 229.376 -> 10.723.328 B (+10.493.952)
    `_saneado` ........... pico 9,0-9,5x la entrada (1 M de controles -> 9.448.929 B
                           de pico para devolver 320)
    prefijo no fiable .... a partir de ~400 caracteres, `U+0009` y `alfabeto`
                           DESAPARECIAN del mensaje: el eco borraba la razon

⚖️ El `+10.493.952` es el DOBLE de la entrada, y no me lo invente: `@contratosbik`
midio que `head`/`body` se guardan DOS VECES —su columna y dentro del `intent`
canonico—, o sea `2,00x`. Eso reconcilia mi medida con los `+6.008.832 B` de
`@security`, cuyo veneno no iba en `body`.

⛔ El numero NO es mio: sale de las tres derivaciones de `@cpo` (corpus real
`n=636` con `0` entradas sobre `32 KiB`; escala del propio DTO; radio de daño).
No las he re-medido y no las presento como mias.
⚠️ Y ese `32 KiB` es la COTA DEL CORPUS que `@cpo` midio, NO la cota adjudicada:
la que rige es `PAYLOAD_MAX_BYTES = 65536`. Los dos numeros conviven en este
fichero a proposito y no son el mismo objeto.
"""
from __future__ import annotations

import pathlib
import sqlite3
import tracemalloc

import pytest

import coordination as C
from ._arnes import INTENT, journal, sesion


def _intent_de(n_bytes: int) -> dict:
    """Un `intent` cuyo canonico pesa EXACTAMENTE `n_bytes`."""
    base = {**INTENT, "body": ""}
    hueco = n_bytes - len(C._canonical(base).encode("utf-8"))
    assert hueco >= 0, "el intent base ya pasa de n_bytes"
    it = {**INTENT, "body": "A" * hueco}
    assert len(C._canonical(it).encode("utf-8")) == n_bytes
    return it


@pytest.fixture()
def j(tmp_path):
    d = journal(tmp_path)
    yield d, sesion(d, "c", principal="p", role="be"), tmp_path / "coordination.sqlite"
    d.close()


# ── la FRONTERA, con el PAR ────────────────────────────────────────────────

def test_N_exacto_se_ACEPTA(j):
    """⊕ `65.536` cabe. Sin este, un `raise` incondicional pasaria todos los ⊖."""
    d, s, _ = j
    assert d.accept_event(s.token, idempotency_key="n", intent=_intent_de(C.PAYLOAD_MAX_BYTES),
                          ledger="llminbox").event_id


def test_N_mas_1_se_RECHAZA(j):
    d, s, _ = j
    with pytest.raises(C.ResourceLimitExceeded):
        d.accept_event(s.token, idempotency_key="n1",
                       intent=_intent_de(C.PAYLOAD_MAX_BYTES + 1), ledger="llminbox")


def test_la_frontera_es_INCLUSIVA_y_se_prueba_con_el_PAR(j):
    """`N` y `N+1` en la MISMA corrida: un valor suelto no fija una frontera."""
    d, s, _ = j
    assert d.accept_event(s.token, idempotency_key="par-n",
                          intent=_intent_de(C.PAYLOAD_MAX_BYTES), ledger="llminbox").event_id
    with pytest.raises(C.ResourceLimitExceeded):
        d.accept_event(s.token, idempotency_key="par-n1",
                       intent=_intent_de(C.PAYLOAD_MAX_BYTES + 1), ledger="llminbox")


# ── NO DURABLE GROWTH: el rechazo no escribe ───────────────────────────────

@pytest.mark.parametrize("tam", [C.PAYLOAD_MAX_BYTES + 1, 200_000, 5_000_000])
def test_un_intent_RECHAZADO_no_hace_crecer_el_journal(j, tam):
    """Lo que esta cota protege es el DISCO: un rechazo que ya escribio no
    protege nada. Se mide sobre el FICHERO, no sobre el `raise`."""
    d, s, db = j
    d.close()
    antes = db.stat().st_size
    d2 = journal(db.parent)
    with pytest.raises(C.ResourceLimitExceeded):
        d2.accept_event(s.token, idempotency_key=f"g{tam}",
                        intent={**INTENT, "body": "A" * tam}, ledger="llminbox")
    d2.close()
    assert db.stat().st_size == antes, f"el journal crecio {db.stat().st_size - antes} B"
    con = sqlite3.connect(str(db))
    assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_el_5MB_que_reprodujo_security_ya_NO_entra(j):
    """El caso exacto del NO-GO. Antes: +10.493.952 B. Ahora: 0."""
    d, s, db = j
    d.close()
    antes = db.stat().st_size
    d2 = journal(db.parent)
    with pytest.raises(C.ResourceLimitExceeded):
        d2.accept_event(s.token, idempotency_key="5mb",
                        intent={**INTENT, "body": "A" * 5_000_000}, ledger="llminbox")
    d2.close()
    assert db.stat().st_size == antes


# ── la UNIDAD es el BYTE UTF-8, no el caracter ─────────────────────────────

def _intent_de_N_CARACTERES(relleno: str) -> dict:
    """`intent` cuyo canonico tiene EXACTAMENTE `PAYLOAD_MAX_BYTES` CARACTERES.

    ⛔ Decia `INTENT_MAX_BYTES`, que NO EXISTE en el sujeto (`grep` ⇒ 0 en
    `coordination.py` salvo una cita de `@contratosbik`). El simbolo vivo, y el
    que este helper lee, es `PAYLOAD_MAX_BYTES`.

    🩸 Mi primera version de este falsador NO DISCRIMINABA, y lo cazo el auditor:
    usaba `body = relleno * 32768`, y el canonico salia de `32.874` caracteres en
    los CUATRO planos, asi que **bytes y caracteres rechazaban igual** — el
    mutante `MC1-unidad-caracteres-no-bytes` sobrevivia al test que existe para
    matarlo. El unico punto donde las dos unidades se separan es el canonico de
    EXACTAMENTE `N` caracteres: ASCII pesa `N` bytes y pasa; el mismo numero de
    codepoints en `ñ`/CJK/SMP pesa `2-4x` y cae.
    """
    base = len(C._canonical({**INTENT, "body": ""}))
    it = {**INTENT, "body": relleno * (C.PAYLOAD_MAX_BYTES - base)}
    assert len(C._canonical(it)) == C.PAYLOAD_MAX_BYTES, "el canonico no tiene N caracteres"
    return it


@pytest.mark.parametrize("relleno,nombre,bytes_por_char", [
    ("A", "ASCII", 1), ("ñ", "Latin-1", 2), ("中", "CJK", 3), ("\U0001D41A", "SMP", 4),
])
def test_la_UNIDAD_es_el_byte_UTF8_no_el_caracter(j, relleno, nombre, bytes_por_char):
    """⊖⊕ El PAR que separa las dos unidades, en la MISMA corrida.

    Los cuatro tienen el MISMO numero de caracteres canonicos (`N` exacto). Un
    tope por CARACTERES los aceptaria a los cuatro; el tope por BYTES acepta
    solo el ASCII. Esa diferencia ES el falsador de la unidad.
    """
    d, s, _ = j
    it = _intent_de_N_CARACTERES(relleno)
    n_bytes = len(C._canonical(it).encode("utf-8"))
    assert len(C._canonical(it)) == C.PAYLOAD_MAX_BYTES        # misma cuenta de CARACTERES
    assert n_bytes >= C.PAYLOAD_MAX_BYTES * bytes_por_char * 0.9   # y bytes MUY distintos
    if bytes_por_char == 1:
        assert d.accept_event(s.token, idempotency_key=f"u{nombre}", intent=it,
                              ledger="llminbox").event_id, "el ASCII de N bytes DEBE entrar"
    else:
        with pytest.raises(C.ResourceLimitExceeded):
            d.accept_event(s.token, idempotency_key=f"u{nombre}", intent=it, ledger="llminbox")


def test_el_ANIDAMIENTO_tambien_cuenta(j):
    """`@contratosbik`: `Field(max_length)` cuenta CLAVES y el anidamiento no se
    ve — `{"a":{"b":{"c":"y"*2M}}}` es UNA clave y `2.000.023` bytes. La cota por
    bytes del canonico lo caza porque mide el DOCUMENTO, no su forma."""
    d, s, _ = j
    hondo = {"a": {"b": {"c": "y" * 100_000}}}
    with pytest.raises(C.ResourceLimitExceeded):
        d.accept_event(s.token, idempotency_key="hondo",
                       intent={**INTENT, "meta": hondo}, ledger="llminbox")


# ── el ERROR cumple la garantia del detalle ────────────────────────────────

def test_el_error_de_la_cota_cumple_las_CUATRO_garantias(j):
    d, s, _ = j
    with pytest.raises(C.ResourceLimitExceeded) as e:
        d.accept_event(s.token, idempotency_key="err",
                       intent={**INTENT, "body": "\n\x1b[31mа℮" * 200_000}, ledger="llminbox")
    m = str(e.value)
    assert len(m.splitlines()) <= 1 and m.isascii()
    assert not any(ord(c) < 0x20 or ord(c) == 0x7F for c in m)
    assert len(m) <= C.DETALLE_MAX
    assert str(C.PAYLOAD_MAX_BYTES) in m, "el mensaje no dice cual es el maximo"


def test_el_codigo_esta_en_el_VOCABULARIO_y_no_mueve_la_taxonomia():
    """`RESOURCE_LIMIT_EXCEEDED` entra en `REASON_CODES` y en el mapa. La
    taxonomia sube en UNO por cada uno, y se declara: `D10`/`D11` cablean esa
    tabla.
    ⛔ Decia `INTENT_TOO_LARGE`, que el ruling RETIRO y que este mismo test NO
    comprueba: el `assert` de abajo siempre miro `RESOURCE_LIMIT_EXCEEDED`. El
    docstring contradecia a su propio cuerpo."""
    assert C.Journal._POR_MOTIVO[C.ResourceLimitExceeded] == "RESOURCE_LIMIT_EXCEEDED"
    assert "RESOURCE_LIMIT_EXCEEDED" in C.REASON_CODES
    assert set(C.Journal._POR_MOTIVO.values()) <= C.REASON_CODES
    clases = {getattr(C, x) for x in dir(C) if isinstance(getattr(C, x), type)
              and issubclass(getattr(C, x), C.JournalError) and getattr(C, x) is not C.JournalError}
    # 🔻 sdet 2026-09-11: `n` contaba DOS familias en un numero y por eso la terna no podia
    # moverse junta: las de RECHAZO (mapean a motivo y codigo) y las de CICLO DE VIDA, que se
    # levantan al abrir/migrar y NUNCA se graban con codigo. Anadir una de ciclo de vida movia
    # `n` sin mover las otras dos -> rojo que no era un defecto. Ahora: la familia de rechazo
    # se cuenta sola, y la de ciclo de vida se declara POR NOMBRE (anadir una es una edicion
    # deliberada, no un numero que sube).
    CICLO_DE_VIDA = {
        "IdentityChanged", "JournalNotInitialized", "JournalReadOnly", "LifecycleConflict",
        "MigrationFailed", "MigrationSnapshotRequired", "OpenModeRestricted",
        "PepperMismatch", "PreflightRejected",
        "PreflightUnstable", "SchemaCorrupt", "SchemaMismatch", "SchemaTooNew"}
    sin_motivo = {c.__name__ for c in clases if c not in C.Journal._POR_MOTIVO}
    assert sin_motivo == CICLO_DE_VIDA, (
        f"una clase cambio de familia sin decirlo: sobran {sorted(sin_motivo - CICLO_DE_VIDA)}, "
        f"faltan {sorted(CICLO_DE_VIDA - sin_motivo)}")
    n = len(clases) - len(CICLO_DE_VIDA)
    # 🔻 33/22/24 -> 35/24/26: el ruling RETIRA `INTENT_TOO_LARGE` (que nunca
    # salio de mi arbol) y mete TRES codigos, uno por EJE. El `==` sigue puesto a
    # proposito: estos numeros son los que D10/D11 cablean.
    # 🔻 33/22/24 -> 35/24/26 al entrar la BARRERA DE ADMISION: dos clases,
    # dos entradas en `_POR_MOTIVO` y dos codigos en `REASON_CODES`. La terna se
    # mueve JUNTA o el invariante «una clase, un motivo, un codigo» esta roto.
    # la terna de RECHAZO sigue clavada a proposito: D10/D11 cablean esta tabla.
    assert (n, len(C.Journal._POR_MOTIVO), len(C.REASON_CODES)) == (27, 27, 29)


def test_la_cota_es_PUBLICA_para_que_el_wire_use_EL_MISMO_numero():
    """Una cota que sólo vive en el borde es una recomendacion; dos numeros
    iguales por casualidad se parten el dia que alguien toque uno."""
    # 🔻 B2 · los literales NUEVOS (`@cpo` 08:35:36Z). Van ESCRITOS, no leidos
    # del sujeto: un test que lee la cota del sujeto no puede distinguir un
    # sujeto correcto de uno con la cota cambiada — se lleva el mutante consigo.
    assert C.PAYLOAD_MAX_BYTES == 65536 and C.METADATO_MAX_BYTES == 4096
    assert C.NODOS_MAX == 8192 and C.CARDINALIDAD_MAX == 256
    assert C.ELEMENTO_MAX_BYTES == 4096 and C.DETALLE_MAX == 320


# ── el SANEADO: trabajo acotado y cola preservada ──────────────────────────

@pytest.mark.parametrize("n", [10_000, 100_000, 1_000_000])
def test_el_saneado_hace_TRABAJO_ACOTADO(n):
    """Antes expandia la cadena ENTERA antes de truncar: pico `9,0-9,5x` medido.
    Ahora recorta ANTES de expandir, asi que el coste lo pone el TOPE y no quien
    manda la entrada. Se mide el PICO, que es lo que tumba un proceso."""
    tracemalloc.start()
    r = C._saneado("\n" * n)
    pico = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert len(r) <= C.DETALLE_MAX
    assert pico < n * 3, f"pico {pico} B para una entrada de {n}: sigue expandiendo antes"


@pytest.mark.parametrize("prefijo", [50, 200, 321, 400, 700, 5_000, 500_000])
def test_un_PREFIJO_no_fiable_NO_borra_la_razon(prefijo):
    """🔴 El `400` es el que casi se me escapa: no dispara el recorte crudo pero
    ya desborda el tope, asi que la primera version volvia a truncar por la
    cabeza y borraba la razon. El recorte crudo es una OPTIMIZACION de trabajo;
    la cabeza+cola es la POLITICA, y va siempre."""
    m = C._saneado(f"la clave {'P' * prefijo} lleva 'X' (U+0009), fuera del alfabeto")
    assert "U+0009" in m, f"prefijo={prefijo}: la razon se perdio"
    assert "alfabeto" in m
    assert len(m) <= C.DETALLE_MAX


# ══ FREEZE-BEFORE-MEMBERSHIP · el falsador que YO no supe construir ═════════
#
# Retire dos mutantes como EQUIVALENTES tras cuatro sondas que apuntaban TODAS
# al mismo lado: `__str__` devolviendo un motivo NO canonico. El auditor probo la
# direccion CONTRARIA y discriminan:
#
#     valor SUBYACENTE no canonico ("NO_CANONICO") · __str__ -> "POLICY_DENIED"
#       sano     ACEPTA · 1 llamada a str() · persiste POLICY_DENIED
#       mutante  ValueError · denials = 0
#
# La propiedad NO es el mensaje: es que el valor CONGELADO gobierne la
# comprobacion de PERTENENCIA. Mi `79/79` estaba SOBREDECLARADO — retirar un
# mutante que discrimina y luego contar «todos muertos» infla el numero.


class _MotivoEnmascarado(str):
    """Valor subyacente NO canonico; `__str__` devuelve uno CANONICO."""
    n = 0

    def __str__(self):
        type(self).n += 1
        return "POLICY_DENIED"


def test_FBM_record_rejection_congela_ANTES_de_comprobar_pertenencia(tmp_path):
    class M(_MotivoEnmascarado):
        n = 0
    d = journal(tmp_path)
    s = sesion(d, "c", principal="p", role="be")
    rid = d.record_rejection(s.token, M("NO_CANONICO"))
    assert rid is not None, "con el valor congelado el motivo ES canonico y entra"
    assert M.n == 1, f"`str()` se llamo {M.n} veces: hay ventana entre comprobar y usar"
    d.close()
    con = sqlite3.connect(str(tmp_path / "coordination.sqlite"))
    con.row_factory = sqlite3.Row
    reasons = [r["reason"] for r in con.execute("SELECT reason FROM denials")]
    assert reasons == ["POLICY_DENIED"], (
        f"persistio {reasons}: lo guardado tiene que ser el valor CONGELADO, no "
        f"el objeto ni su valor subyacente")


def test_FBM_record_denial_congela_ANTES_de_comprobar_pertenencia(tmp_path):
    """La puerta interna, independiente. Curar una y dejar la otra es dejar el
    hueco con otro nombre — el error que ya cometi en la primera ronda."""
    class M(_MotivoEnmascarado):
        n = 0
    d = journal(tmp_path)
    s = sesion(d, "c", principal="p", role="be")
    ident = d._identidad_para_auditoria(s.token)
    rid = d._record_denial(ident["principal_id"], M("NO_CANONICO"), lane=ident["lane"])
    assert rid is not None
    d.close()
    con = sqlite3.connect(str(tmp_path / "coordination.sqlite"))
    con.row_factory = sqlite3.Row
    assert [r["reason"] for r in con.execute("SELECT reason FROM denials")] == ["POLICY_DENIED"]


# ══ El RASTRO del rechazo: filas, no bytes del fichero ═════════════════════

def _censo(db: pathlib.Path) -> dict:
    con = sqlite3.connect(str(db))
    tablas = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tablas}


def test_el_rechazo_AUTENTICADO_deja_el_rastro_ACOTADO_exacto(tmp_path):
    """🩸 Yo afirme «cero escritura durable» y era FALSO: media BYTES DEL FICHERO,
    y SQLite reusa paginas — el tamaño no se mueve aunque se inserten filas.

    El invariante real, en journal AISLADO y PRIMER rechazo autenticado:
        events / outbox / idempotency ........... +0
        denials / receipts / receipt_transitions . +1
    `accept_event` va dentro de `@_audita`, asi que el rechazo DEBE dejar su
    rastro acotado — es la garantia 6, no un descuido.
    """
    d = journal(tmp_path)
    s = sesion(d, "c", principal="p", role="be")
    db = tmp_path / "coordination.sqlite"
    d.close()
    antes = _censo(db)
    # La puerta ya quedó abierta en la base por el `journal()` de arriba: volver
    # a abrirla emitiría una sesión más, y este censo —que mide el rastro EXACTO
    # de un rechazo— contaría esa fila como si fuera del rechazo.
    d2 = journal(tmp_path, admision=False)
    with pytest.raises(C.ResourceLimitExceeded):
        d2.accept_event(s.token, idempotency_key="tr",
                        intent={**INTENT, "body": "A" * 5_000_000}, ledger="llminbox")
    d2.close()
    despues = _censo(db)
    delta = {t: despues.get(t, 0) - antes.get(t, 0) for t in set(antes) | set(despues)}
    for t in ("events", "outbox", "idempotency"):
        assert delta.get(t, 0) == 0, f"{t} crecio {delta.get(t)}"
    for t in ("denials", "receipts", "receipt_transitions"):
        assert delta.get(t, 0) == 1, f"{t} = {delta.get(t)}, se esperaba +1"
    assert not [t for t, v in delta.items() if v and t not in
                ("denials", "receipts", "receipt_transitions")], (
        f"crecieron tablas fuera del rastro acotado: "
        f"{[t for t, v in delta.items() if v]}")


def test_el_rastro_lleva_el_reason_CERRADO_y_NINGUN_byte_del_intent(tmp_path):
    d = journal(tmp_path)
    s = sesion(d, "c", principal="p", role="be")
    db = tmp_path / "coordination.sqlite"
    with pytest.raises(C.ResourceLimitExceeded):
        d.accept_event(s.token, idempotency_key="tr2",
                       intent={**INTENT, "body": "ZZZQQQ" * 900_000}, ledger="llminbox")
    d.close()
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    assert [r["reason"] for r in con.execute("SELECT reason FROM denials")] == ["RESOURCE_LIMIT_EXCEEDED"]
    crudo = "".join(str(v) for t in ("denials", "receipts", "receipt_transitions")
                    for r in con.execute(f"SELECT * FROM {t}") for v in tuple(r))
    assert "ZZZQQQ" not in crudo, "un byte del intent llego a las filas del rastro"


def test_el_rechazo_NO_AUTENTICADO_da_AuthError_y_CERO_denial(tmp_path):
    """Sin identidad no hay a quien colgarle el recibo: `401` y cero rastro. Es
    la clausula `R2` del ADR funcionando, no un hueco."""
    d = journal(tmp_path)
    sesion(d, "c", principal="p", role="be")
    db = tmp_path / "coordination.sqlite"
    d.close()
    antes = _censo(db)
    d2 = journal(tmp_path)
    with pytest.raises(C.AuthError):
        d2.accept_event("token-que-no-resuelve", idempotency_key="na",
                        intent={**INTENT, "body": "A" * 5_000_000}, ledger="llminbox")
    d2.close()
    despues = _censo(db)
    assert despues.get("denials", 0) == antes.get("denials", 0), "dejo denial sin identidad"


def test_la_REPETICION_crece_ACOTADA_por_la_cuota_EXISTENTE(tmp_path):
    """No invento cifra: uso la cuota que el journal ya tiene. Por encima de
    ella, las denegaciones se AGREGAN en vez de abrir una fila por peticion —
    que es lo que impide que una tormenta de rechazos sea el propio ataque."""
    d = journal(tmp_path, denial_quota=2)
    s = sesion(d, "c", principal="p", role="be")
    db = tmp_path / "coordination.sqlite"
    for i in range(8):
        with pytest.raises(C.ResourceLimitExceeded):
            d.accept_event(s.token, idempotency_key=f"rep{i}",
                           intent={**INTENT, "body": "A" * 100_000}, ledger="llminbox")
    d.close()
    con = sqlite3.connect(str(db))
    n_den = con.execute("SELECT COUNT(*) FROM denials").fetchone()[0]
    n_agg = con.execute("SELECT COUNT(*) FROM denial_aggregates").fetchone()[0]
    assert n_den == 2, f"denials={n_den}: la cuota existente es 2 y no se respeto"
    assert n_agg == 1, f"denial_aggregates={n_agg}: la repeticion no se agrego"
    assert n_den + n_agg < 8, "el crecimiento NO esta acotado: una fila por peticion"


# ══ ciclos y profundidad, TIPADOS ══════════════════════════════════════════

def test_un_intent_CICLICO_da_error_TIPADO_y_no_un_ValueError_crudo(j):
    """Antes reventaba con el `ValueError: Circular reference detected` de `json`
    —sin tipar, sin auditar y con el texto de la libreria—."""
    d, s, _ = j
    ciclo: dict = {"a": 1}
    ciclo["self"] = ciclo
    with pytest.raises(C.OperationInvalid):
        d.accept_event(s.token, idempotency_key="ciclo",
                       intent={**INTENT, "meta": ciclo}, ledger="llminbox")


def test_un_intent_DEMASIADO_PROFUNDO_da_error_TIPADO(j):
    d, s, _ = j
    hondo: object = {"x": 1}
    for _ in range(30_000):
        hondo = {"n": hondo}
    # 🔻 `PayloadTooDeep`: el ruling le da CLASE PROPIA a la profundidad.
    with pytest.raises(C.ResourceLimitExceeded):
        d.accept_event(s.token, idempotency_key="hondo2",
                       intent={**INTENT, "meta": hondo}, ledger="llminbox")


def test_el_eje_de_NODOS_acota_un_documento_PEQUENO(j):
    """🔑 El eje que el tope de BYTES no puede dar: `@cto` lo midio — un JSON de
    `32 KiB` con claves de `4 B` son `~4.000` nodos. Un documento PEQUENO en
    bytes puede reventar el parser por cardinalidad de nodos."""
    d, s, _ = j
    # 🩸 REANCLADO a la unidad ADJUDICADA por `@cpo`: «1 nodo = 1 VALOR». El
    # documento anterior —`{f"k{i}": i}` con `5.000` claves— gastaba `5.009`
    # nodos, NO `10.000`: las CLAVES no son valores y no cuentan. Se quedaba
    # `3.183` por DEBAJO de `NODOS_MAX` y el test moria con `DID NOT RAISE`.
    # 🔑 Y subirlo a `8.200` claves TAMPOCO servia: `104.482 B` cruzan primero
    # el eje de BYTES, asi que el error habria dicho `dimension=bytes` y el test
    # habria acreditado el eje de al lado. Una LISTA de enteros separa los dos
    # ejes: `8.300` valores son `16.702 B` —un documento PEQUENO de verdad— y
    # el unico techo que cruzan es el de NODOS. Medido, no supuesto.
    muchos = [0] * 8300
    assert len(C._canonical({**INTENT, "meta": muchos}).encode()) < C.PAYLOAD_MAX_BYTES * 2
    with pytest.raises(C.ResourceLimitExceeded) as e:
        d.accept_event(s.token, idempotency_key="nodos", intent={**INTENT, "meta": muchos},
                       ledger="llminbox")
    assert "dimension=nodes" in str(e.value)


def test_el_eje_de_CARDINALIDAD_acota_las_FILAS(j):
    """`causes`/`external_causes` no son estructura libre: cada elemento es una
    FILA durable. Por eso su eje es cardinalidad y no bytes — con `3.000`
    elementos me insertaba `3.000` filas mientras yo medía bytes."""
    d, s, _ = j
    # 🩸 REANCLADO: `65` era el techo VIEJO. Hoy `CARDINALIDAD_MAX = 256`, asi
    # que `65` no cruzaba nada y la peticion seguia hasta la validacion de
    # causalidad, que la mataba con `CauseRejected` — un rojo que NO es el eje
    # que este test mide. `257` es `N+1` del techo vivo y salta en
    # `_congelar_secuencia`, ANTES de que la causalidad mire los ids.
    with pytest.raises(C.ResourceLimitExceeded) as e:
        d.accept_event(s.token, idempotency_key="card", intent=INTENT, ledger="llminbox",
                       causes=["ev_x"] * (C.CARDINALIDAD_MAX + 1))
    assert "dimension=cardinality" in str(e.value) and "field=causes" in str(e.value)


# ══ los DOS que el reanclaje de `MUTANTES_C` dejo sin juez ═════════════════
# Al reanclar el grupo por FORMA aparecieron dos mutantes que NINGUN test de
# este fichero mataba: el pico de `_congelar` y la derivacion de la
# profundidad. Un mutante sin test que lo mate se lee en el informe igual que
# uno equivalente — y no lo es.

def test_congelar_NO_materializa_el_string_gigante():
    """⊖ `MC1-cota-incremental-apagada`. La cota inferior corta ANTES de
    codificar; sin ella, `_canonical` materializa los `5 MB` (medido: pico de
    `4.650 B` con la cura, `11.250.944 B` sin ella)."""
    import tracemalloc
    gigante = {"body": "A" * 5_000_000}          # se asigna ANTES de medir
    tracemalloc.start()
    try:
        with pytest.raises(C.ResourceLimitExceeded):
            # 🩸 `"x"` NO existe en `CAMPOS_CONTRATO_CORE`, y desde que la guarda
            # del enum paso de `assert` a check de runtime el helper levanta
            # `AssertionError` antes que `ResourceLimitExceeded`: el test moria
            # por el NOMBRE del campo, no por la propiedad que mide. Se usa un
            # campo REAL del contrato; el pico de memoria no depende de cual.
            C._congelar(gigante, "intent", tope_bytes=C.PAYLOAD_MAX_BYTES)
        pico = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert pico < 2 * C.PAYLOAD_MAX_BYTES, (
        f"pico {pico} B: se esta expandiendo el cuerpo para medirlo")


def test_la_profundidad_tiene_UNA_sola_verdad_la_de_Journal():
    """⊖ `MC1-profundidad-dos-verdades`. `_congelar` DERIVA su tope de
    `Journal._PROFUNDIDAD_MAX`; un `12` cableado da dos verdades que se
    separan el dia que alguien mueva una."""
    orig = C.Journal._PROFUNDIDAD_MAX
    hondo = {"a": {"b": {"c": {"d": 1}}}}          # 4 niveles
    try:
        C.Journal._PROFUNDIDAD_MAX = 3
        with pytest.raises(C.ResourceLimitExceeded) as e:
            # 🩸 `"x"` no esta en `CAMPOS_CONTRATO_CORE` y la guarda del enum,
            # ya como check de runtime, levantaba `AssertionError` antes que el
            # `ResourceLimitExceeded` que este test espera. Campo REAL: la
            # profundidad no depende de cual sea.
            C._congelar(hondo, "intent", tope_bytes=C.PAYLOAD_MAX_BYTES)
        assert e.value.limit == 3, "no siguio al simbolo, lleva su propio numero"
    finally:
        C.Journal._PROFUNDIDAD_MAX = orig
    # ⊕ CONTROL: restaurado, el MISMO documento entra. Sin esto, «rechaza» se
    # cumple rechazando siempre.
    assert C._congelar(hondo, "intent", tope_bytes=C.PAYLOAD_MAX_BYTES) is not None
