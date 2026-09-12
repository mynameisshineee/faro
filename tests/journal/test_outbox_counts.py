"""`outbox_counts`: las dos cifras de la cola de UN carril, de UNA sola foto.

Qué se prueba y por qué, en tres bloques:

· **SCOPE** — la profundidad de la cola es información del carril. Se cuenta lo
  del carril de la SESIÓN, y `abandoned`/`materialized` no son cola.
· **AUTORIZACIÓN** — `outbox_worker`. Estar en el carril no es ser quien lo
  drena. El ⊕ (una sesión CON la capacidad pasa) va pegado al ⊖: sin él, un
  `PolicyDenied` por cualquier otro motivo se leería como cobertura.
· **ATOMICIDAD** — las dos cifras salen de la misma transacción. El ⊖ de este
  bloque NO es un gemelo escrito para caer: es el par de llamadas REALES y
  vivas (`pending_outbox` + `unresolved_outbox`) que esta API sustituye, sin
  tocarlas, saboteadas en la ventana que tienen entre medias. Si el ⊖ no se
  rompiera ahí, la API nueva no estaría resolviendo nada.

El sabotaje del test de la foto única dispara por `_gancho_carrera`, que en esta
lectura va DESPUÉS de la autenticación: la foto queda fijada por la lectura de la
sesión, así que lo que se falsa es que el conteo pertenezca a la MISMA foto que
autorizó. Los dos ⊖ del patrón viejo no necesitan seam: la ventana es literal,
está entre sus dos llamadas. Ninguno duerme — una ventana que se falsa durmiendo
mide la carga del CI, no el aislamiento.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

import coordination as C
from ._arnes import (CAPS_RUNTIME, GRAMATICA, INTENT, LANES, OPERADOR, PEPPER,
                     censo, journal, sesion, sesiones)

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(os.path.dirname(AQUI))
WORKER = os.path.join(AQUI, "_worker_counts.py")


def _jr(tmp_path, **kw):
    """`max_attempts=1`: el PRIMER fallo ya agota, así que llevar un item a
    `failed` es un `claim`+`mark` y no un bucle que además tiene que saltarse el
    backoff moviendo el reloj. Menos montaje ⇒ menos sitio donde esconder."""
    kw.setdefault("max_attempts", 1)
    return journal(tmp_path, **kw)


def _otro(path):
    """Segunda instancia sobre el MISMO fichero: lo que hace otro proceso.

    Se construye e INICIALIZA fuera de toda lectura a propósito. `initialize()`
    es lo único de este camino que toma el cerrojo EXCLUSIVO; dentro de un
    `_lectura()` —que retiene el COMPARTIDO en la misma hebra— se bloquearía
    contra sí mismo y el test colgaría en vez de fallar.
    """
    o = C.Journal(path, pepper=PEPPER, busy_timeout_ms=10_000, max_attempts=1,
                  lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
    o.initialize()
    return o


def _acepta(j, s, clave, ledger="llminbox", head="candidato listo"):
    return j.accept_event(s.token, idempotency_key=clave,
                          intent={**INTENT, "to": ["be"], "head": head},
                          ledger=ledger)


def _agota(j, s):
    """Lleva a `failed` el SIGUIENTE de la cola, por la vía real: arrendar y
    fallar. Con `max_attempts=1` el primer fallo agota, así que no hace falta
    mover el reloj.

    Devuelve el id que le tocó y NO lo elige: `claim_outbox` sirve por `n`, y
    un helper que pretendiera escoger estaría probando otra cosa.
    """
    item = j.claim_outbox(s.token, lease_s=300)
    assert item is not None, "no había nada pendiente que agotar"
    j.mark_outbox_failed(s.token, item.event_id, error="el ledger está :ro",
                         claim_token=item.claim_token)
    return item.event_id


# ══ SCOPE ═══════════════════════════════════════════════════════════════════

def test_outbox_counts_cuenta_pending_y_failed_SOLO_del_carril_de_la_sesion(tmp_path):
    """Las tres cifras del carril A son DISTINTAS de las de B y de las globales.

    No es adorno: con los dos carriles iguales, un conteo global pasaría el test
    por casualidad. Aquí A=(2,1) y B=(1,0), y el global sería (3,1).
    """
    j = _jr(tmp_path)
    a, b = sesiones(j,
        {"credential": "c-a", "principal": "p-a", "role": "be", "lane": "carril-uno",
         "ttl_s": 10 ** 6},
        {"credential": "c-b", "principal": "p-b", "role": "be", "lane": "carril-dos",
         "ttl_s": 10 ** 6})
    _acepta(j, a, "k1", "ledger-uno")
    _acepta(j, a, "k2", "ledger-uno", head="otro")
    _acepta(j, a, "k3", "ledger-uno", head="tercero")
    _acepta(j, b, "k4", "ledger-dos")
    _agota(j, a)

    ca = j.outbox_counts(a.token)
    cb = j.outbox_counts(b.token)
    assert (ca.lane, ca.pending, ca.failed, ca.unresolved) == ("carril-uno", 2, 1, 3)
    assert (cb.lane, cb.pending, cb.failed, cb.unresolved) == ("carril-dos", 1, 0, 1)
    j.close()


def test_outbox_counts_dice_LO_MISMO_que_las_dos_lecturas_que_sustituye(tmp_path):
    """⊕ de contrato: mide el MISMO objeto que `pending_outbox`/`unresolved_outbox`.

    Sin esto, la API nueva podría ser atómica y a la vez estar contando otra
    cosa —otro conjunto de estados, otro carril— y todos los demás tests de este
    fichero seguirían verdes: sólo se comparan consigo mismos.
    """
    j = _jr(tmp_path)
    s = sesion(j, capabilities=OPERADOR, ttl_s=10 ** 6)
    _acepta(j, s, "k1")
    _acepta(j, s, "k2", head="otro")
    _agota(j, s)

    c = j.outbox_counts(s.token)
    assert c.pending == j.pending_outbox(s.token)
    assert c.unresolved == j.unresolved_outbox(s.token)
    assert c.failed == c.unresolved - c.pending == 1
    j.close()


def test_outbox_counts_NO_cuenta_lo_materializado_ni_lo_abandonado(tmp_path):
    """Lo resuelto sale de la cola. `materialized` y `abandoned` son las dos
    salidas, y ninguna de las dos puede seguir pesando en `unresolved`: si
    pesaran, un rollback quedaría bloqueado para siempre por trabajo hecho."""
    j = _jr(tmp_path)
    s = sesion(j, capabilities=OPERADOR, ttl_s=10 ** 6)
    m = _acepta(j, s, "k1")
    d = _acepta(j, s, "k2", head="otro")
    vivo = _acepta(j, s, "k3", head="tercero")
    assert j.outbox_counts(s.token).unresolved == 3          # ⊕ los tres pesaban

    item = j.claim_outbox(s.token, lease_s=300)
    assert item.event_id == m.event_id
    j.mark_materialized(s.token, m.event_id, entry_eid="e" * 64,
                        ledger="llminbox", claim_token=item.claim_token)
    assert _agota(j, s) == d.event_id          # el siguiente por `n` es el suyo
    j.abandon_outbox(s.token, d.event_id, reason="el evento ya no aplica")

    c = j.outbox_counts(s.token)
    assert (c.pending, c.failed, c.unresolved) == (1, 0, 1)
    assert j._connect().execute(
        "SELECT COUNT(*) c FROM outbox").fetchone()["c"] == 3   # siguen ahí
    assert vivo.event_id                                        # el que queda
    j.close()


def test_outbox_counts_de_un_carril_VACIO_da_CEROS_enteros_y_no_None(tmp_path):
    """`SUM` sobre cero filas devuelve NULL, no 0. Sin el `or 0` el DTO salía
    con `None` dentro y reventaba en la primera suma del llamante — un fallo que
    sólo aparece en el carril tranquilo, que es el que nadie prueba."""
    j = _jr(tmp_path)
    a, b = sesiones(j,
        {"credential": "c-a", "principal": "p-a", "role": "be", "lane": "carril-uno"},
        {"credential": "c-b", "principal": "p-b", "role": "be", "lane": "carril-dos"})
    _acepta(j, a, "k1", "ledger-uno")                  # ⊕ el otro carril NO está vacío
    c = j.outbox_counts(b.token)
    assert (c.pending, c.failed) == (0, 0)
    assert isinstance(c.pending, int) and isinstance(c.failed, int)
    assert c.unresolved == 0
    j.close()


# ══ AUTORIZACIÓN ════════════════════════════════════════════════════════════

def test_outbox_counts_exige_la_capacidad_outbox_worker(tmp_path):
    """⊖ sin la capacidad y ⊕ con ella, en el MISMO carril y con la misma cola.

    El ⊕ no es ceremonia: sin él, un `PolicyDenied` disparado por cualquier otro
    motivo —un carril mal montado, una sesión inválida— se leería como que la
    puerta funciona.
    """
    j = _jr(tmp_path)
    pelada, capaz = sesiones(j,
        {"credential": "c-pelada", "principal": "p-pelada", "role": "be",
         "lane": "llminbox", "capabilities": ()},
        {"credential": "c-capaz", "principal": "p-capaz", "role": "be",
         "lane": "llminbox", "capabilities": CAPS_RUNTIME})
    _acepta(j, capaz, "k1")

    with pytest.raises(C.PolicyDenied) as e:
        j.outbox_counts(pelada.token)
    assert C.CAP_OUTBOX_WORKER in str(e.value)
    assert "1" not in str(e.value)                 # el rechazo no filtra la cifra

    assert j.outbox_counts(capaz.token).pending == 1        # ⊕ con capacidad, pasa
    assert j.pending_outbox(pelada.token) == 1              # ⊕ la vieja NO se aprieta
    j.close()


def test_outbox_counts_SIN_sesion_valida_se_rechaza(tmp_path):
    """Fail-closed antes que la capacidad: sin sesión no hay ni carril al que
    preguntarle la cola."""
    j = _jr(tmp_path)
    sesion(j)
    with pytest.raises(C.AuthError):
        j.outbox_counts("token-inventado")
    j.close()


def test_outbox_counts_con_la_sesion_REVOCADA_se_rechaza(tmp_path):
    j = _jr(tmp_path)
    s = sesion(j, ttl_s=10 ** 6)
    _acepta(j, s, "k1")
    assert j.outbox_counts(s.token).pending == 1            # ⊕ antes de revocar
    j.revoke_current(s.token, "fin de turno")
    with pytest.raises(C.AuthError):
        j.outbox_counts(s.token)
    j.close()


# ══ FORMA CANÓNICA DEL DTO ══════════════════════════════════════════════════

def test_OutboxCounts_acepta_la_forma_canonica(tmp_path):
    """⊕ de la puerta: sin esto, un `__post_init__` que rechazara TODO daría
    verdes los cuatro ⊖ de abajo y el tipo sería inconstruible."""
    c = C.OutboxCounts(lane="llminbox", pending=2, failed=1)
    assert (c.lane, c.pending, c.failed, c.unresolved) == ("llminbox", 2, 1, 3)
    assert C.OutboxCounts(lane="l", pending=0, failed=0).unresolved == 0   # cero SÍ


class _CarrilRaro(str):
    """Subclase de `str`: pasa `isinstance` y NO pasa `type(x) is str`. Es la
    población que separa la forma EXACTA de la forma «parecida»."""


@pytest.mark.parametrize("lane", ["", None, 0, b"llminbox", _CarrilRaro("llminbox")])
def test_OutboxCounts_rechaza_un_lane_que_no_sea_str_no_vacio(lane):
    """La cifra sin carril es la que se acaba pegando en el tablero de otro, y
    el `str` vacío es peor que ninguno: se renderiza como si fuera un nombre.

    La subclase de `str` es el ⊖ de la forma EXACTA: con `isinstance` colaría, y
    un `str` con `__str__` propio puede renderizar un carril que no es el suyo.
    """
    with pytest.raises(C.OperationInvalid) as e:
        C.OutboxCounts(lane=lane, pending=0, failed=0)
    assert "lane" in str(e.value)


@pytest.mark.parametrize("campo", ["pending", "failed"])
@pytest.mark.parametrize("valor", [-1, -100])
def test_OutboxCounts_rechaza_contadores_NEGATIVOS(campo, valor):
    """Una cuenta bajo cero no es un dato viejo: es imposible. Y es exactamente
    lo que produce el patrón que esta API sustituye — ver
    `test_el_failed_DEDUCIDO_restando_puede_salir_NEGATIVO`."""
    kw = {"lane": "llminbox", "pending": 0, "failed": 0, campo: valor}
    with pytest.raises(C.OperationInvalid) as e:
        C.OutboxCounts(**kw)
    assert campo in str(e.value) and "negativo" in str(e.value)


@pytest.mark.parametrize("campo", ["pending", "failed"])
@pytest.mark.parametrize("valor", [True, False, 1.0, None, "2", 2 + 0j])
def test_OutboxCounts_rechaza_lo_que_no_es_un_int_EXACTO_empezando_por_bool(campo, valor):
    """`bool` es SUBCLASE de `int`: `isinstance(True, int)` es `True`, y por eso
    la puerta se escribe con `type(v) is int` y no con `isinstance`.

    Es el ⊖ que separa una comprobación de tipo escrita de verdad de una escrita
    de memoria: `False` como `pending` pasaría por un `0` legítimo, `True` por un
    `1`, y `unresolved` sumaría banderas. Los otros valores (`float`, `None`,
    `str`, `complex`) son el resto del complemento — sin ellos, el test sólo
    probaría el caso que ya sé.
    """
    kw = {"lane": "llminbox", "pending": 0, "failed": 0, campo: valor}
    with pytest.raises(C.OperationInvalid) as e:
        C.OutboxCounts(**kw)
    assert campo in str(e.value)


@pytest.mark.parametrize("valor,rastro", [(-999777, "999777"),
                                          ("<guion>", "<guion>"),
                                          (1.5, "1.5")])
def test_el_rechazo_NO_repite_el_valor_sospechoso_en_el_mensaje(valor, rastro):
    """Mensaje CERRADO. Si la fila viene corrupta, el texto del error es lo único
    que sube; meter el valor dentro lo publica por la única vía que nadie sanea
    —y el rechazo es justo el camino que menos se audita—. El nombre del campo sí
    va: es un literal nuestro, no un dato de la base.
    """
    with pytest.raises(C.OperationInvalid) as e:
        C.OutboxCounts(lane="llminbox", pending=valor, failed=0)
    assert "pending" in str(e.value)          # ⊕ el mensaje sí identifica el campo
    assert rastro not in str(e.value), "el rechazo repite el valor sospechoso"


def test_outbox_counts_DEVUELVE_la_forma_canonica_desde_la_base(tmp_path):
    """La puerta no vale si el productor real nunca la cruza: aquí se comprueba
    que lo que sale del `SELECT` es un DTO válido y de tipos exactos, no que un
    constructor a mano lo sea."""
    j = _jr(tmp_path)
    s = sesion(j, capabilities=OPERADOR, ttl_s=10 ** 6)
    _acepta(j, s, "k1")
    _agota(j, s)
    c = j.outbox_counts(s.token)
    assert type(c.pending) is int and type(c.failed) is int   # ni bool ni None
    assert type(c.lane) is str and c.lane == "llminbox"
    assert (c.pending, c.failed) == (0, 1)
    j.close()



# ══ ATOMICIDAD ══════════════════════════════════════════════════════════════

def test_las_dos_cifras_salen_de_LA_MISMA_foto(tmp_path):
    """Sabotaje DENTRO de la llamada: otra instancia mueve un item de `pending`
    a `failed` entre la autenticación y el conteo.

    La respuesta tiene que ser la foto COHERENTE de antes del sabotaje. El ⊕ que
    impide el verde vacuo es la segunda llamada: si el sabotaje no hubiera
    entrado, `(2,0)` saldría igual y el test no habría medido nada.
    """
    j = _jr(tmp_path)
    s = sesion(j, capabilities=OPERADOR, ttl_s=10 ** 6)
    _acepta(j, s, "k1")
    _acepta(j, s, "k2", head="otro")
    o = _otro(j.path)

    def sabotaje():
        item = o.claim_outbox(s.token, lease_s=300)
        assert item is not None
        o.mark_outbox_failed(s.token, item.event_id, error="otro proceso",
                             claim_token=item.claim_token)

    j._gancho_carrera = sabotaje
    try:
        c = j.outbox_counts(s.token)
    finally:
        j._gancho_carrera = None
    assert (c.pending, c.failed) == (2, 0), "el conteo no es de la foto que autorizó"

    d = j.outbox_counts(s.token)                    # ⊕ el sabotaje SÍ entró
    assert (d.pending, d.failed) == (1, 1)
    o.close(); j.close()


def test_el_MISMO_par_pedido_en_DOS_llamadas_SI_se_desgarra(tmp_path):
    """⊖ del anterior, y no es un gemelo escrito para caer: son las dos lecturas
    públicas VIVAS del repo, sin tocar, con el mismo sabotaje en la ventana que
    tienen entre medias. Es el patrón de llamada que `outbox_counts` sustituye.

    El daño no es que las cifras estén viejas —eso lo asume cualquier lectura—:
    es que el `failed` DEDUCIDO restando describe un estado que la base no tuvo
    en ningún instante.
    """
    j = _jr(tmp_path)
    s = sesion(j, capabilities=OPERADOR, ttl_s=10 ** 6)
    _acepta(j, s, "k1")
    _acepta(j, s, "k2", head="otro")
    o = _otro(j.path)

    p = j.pending_outbox(s.token)                   # 2 pendientes
    item = o.claim_outbox(s.token, lease_s=300)     # ── la ventana ──
    o.mark_outbox_failed(s.token, item.event_id, error="otro proceso",
                         claim_token=item.claim_token)
    u = j.unresolved_outbox(s.token)                # 2 sin resolver, pero 1 falló

    assert (p, u) == (2, 2)
    assert u - p == 0, "el desgarro dejó de reproducirse: revisa el sabotaje"
    assert j.outbox_counts(s.token).failed == 1     # el real, de una sola foto
    o.close(); j.close()


def test_el_failed_DEDUCIDO_restando_puede_salir_NEGATIVO(tmp_path):
    """La cara que no se ve venir: el desgarro no da sólo una cifra vieja, da
    una IMPOSIBLE. Si entre las dos lecturas la cola DRENA, `unresolved - pending`
    sale bajo cero, y ningún consumidor de un contador de cola comprueba eso.

    Lo que aquí se documenta es el defecto del PATRÓN de llamada, no de las dos
    funciones: cada una es correcta por separado, y por eso el defecto no vive en
    ninguna de las dos y no se arregla dentro de ellas.
    """
    j = _jr(tmp_path)
    s = sesion(j, capabilities=OPERADOR, ttl_s=10 ** 6)
    for i in range(3):
        _acepta(j, s, f"k{i}", head=f"evento {i}")
    o = _otro(j.path)

    p = j.pending_outbox(s.token)                   # 3 pendientes
    for _ in range(2):                              # ── la ventana: la cola drena
        item = o.claim_outbox(s.token, lease_s=300)
        o.mark_materialized(s.token, item.event_id, entry_eid="e" * 64,
                            ledger="llminbox", claim_token=item.claim_token)
    u = j.unresolved_outbox(s.token)                # 1 sin resolver

    assert (p, u) == (3, 1)
    assert u - p == -2, "el drenaje dejó de reproducirse: revisa el sabotaje"
    c = j.outbox_counts(s.token)                    # de una sola foto: coherente
    assert (c.pending, c.failed, c.unresolved) == (1, 0, 1)
    o.close(); j.close()



# ══ CONCURRENCIA · 20 PROCESOS ══════════════════════════════════════════════

MOVERS, LECTORES, VUELTAS, TOTAL = 5, 15, 60, 6


def _lanza(db, token, papeles):
    env = {**os.environ, "LLMINBOX_RAIZ": RAIZ, "LLMINBOX_PEPPER": PEPPER.decode()}
    procs = [(papel, subprocess.Popen(
        [sys.executable, WORKER, papel, db, token, str(VUELTAS), str(TOTAL)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env))
        for papel in papeles]
    salidas = []
    for papel, p in procs:
        out, err = p.communicate(timeout=600)
        assert p.returncode == 0, f"{papel} rc={p.returncode}: {err}"
        salidas.append((papel, json.loads(out.strip().splitlines()[-1])))
    return salidas


def test_veinte_procesos_nunca_ven_un_total_roto_mientras_la_cola_se_mueve(tmp_path):
    """El invariante: los items ni se crean ni se resuelven mientras corre, así
    que cada uno está SIEMPRE en `pending` o en `failed` y `pending+failed` vale
    TOTAL en cualquier foto. Una lectura desgarrada cuenta uno dos veces o
    ninguna, y el total se sale.

    Este test discrimina el defecto por probabilidad, no por construcción; el ⊖
    determinista de la misma propiedad es
    `test_el_MISMO_par_pedido_en_DOS_llamadas_SI_se_desgarra`. Lo que aporta
    aquí, y no aporta aquél, es que la exclusión sea entre CONEXIONES de verdad y
    no entre dos llamadas de la misma.

    Los dos ⊕ contra el verde vacuo: los movers tienen que haber ciclado, y los
    lectores tienen que haber VISTO la cola en los dos estados. Sin ellos, un
    montaje que no mueve nada da `0` violaciones y parece una prueba.
    """
    j = _jr(tmp_path)
    s = sesion(j, capabilities=OPERADOR, ttl_s=10 ** 6)
    for i in range(TOTAL):
        _acepta(j, s, f"k{i}", head=f"evento {i}")
    assert j.outbox_counts(s.token).pending == TOTAL
    db = j.path
    j.close()

    t0 = time.time()
    salidas = _lanza(db, s.token, ["mover"] * MOVERS + ["lector"] * LECTORES)
    tardo = time.time() - t0

    malos = [o for _, o in salidas if not o["ok"]]
    assert malos == [], malos
    lectores = [o for papel, o in salidas if papel == "lector"]
    movers = [o for papel, o in salidas if papel == "mover"]

    assert sum(o["rotas"] for o in lectores) == 0, \
        f"muestras con pending+failed != {TOTAL}: {[o for o in lectores if o['rotas']]}"
    assert sum(o["muestras"] for o in lectores) == LECTORES * VUELTAS
    # ⊕ hubo movimiento REAL bajo los lectores, y lo vieron.
    assert sum(o["ciclos"] for o in movers) > 0, "ningún mover llegó a ciclar"
    assert max(o["max_failed"] for o in lectores) > 0, \
        "ningún lector vio un `failed`: midieron una cola quieta"
    assert max(o["max_pending"] for o in lectores) > 0
    assert tardo < 600
