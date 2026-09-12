"""El escritor se tomaba para hacer cuentas: la wiki lo retenía 64 s y el ack dura 3,2 s.

Lo reportó @harness con su daemon —`5/5 código 503 (CONTENCIÓN)` a las 20:11:39–57Z, bien
pasado el arranque— y tenía razón en las dos cosas que dijo: NO era la ventana de
reindex-al-recrear que yo le había vendido, y la cura NO era ampliar SU ventana de
reintentos, porque eso habría tapado mi contención con mecanismo suyo.

MEDIDO EN PRODUCCIÓN (2026-09-01), con el barrido como única variable:

    barrido EN VUELO : 0 ok · 8 fallidos        barrido PARADO : 8 ok · 0 fallidos
    sonda externa    : 1425/2114 intentos de escritura bloqueados
                       bloqueo CONTIGUO más largo 59,87 s
    escritor tomado  : ledgers 0,01 s   ·   WIKI 64,28 s

CÓMO ESTUVE A PUNTO DE CURAR LO EQUIVOCADO, dos veces:

 1. «El barrido acapara»: dura 44 s ⇒ pensé ceder el escritor entre ledgers. Falso: los
    ledgers lo tienen 0,01 s.
 2. «Una escritura suelta dura 9,14 s y el ack sólo aguanta 3,2 s» ⇒ pensé dimensionar el
    presupuesto del ack. También falso: los 9,14 s eran lo que tarda `reindex`, y casi
    todo eso es PARSEO, sin escritor tomado. Medí la duración de la función por la del
    lock, que es el mismo error de instrumento de siempre.

Sólo instrumentar la ventana real (`_t_wiki`) separó las dos poblaciones.

LA CAUSA: `reindex_wiki` abría la transacción con `INSERT INTO pages` y no la soltaba hasta
el `commit` 129 líneas después. En medio, la resolución de citas: un `body LIKE '%ancla%'`
por cita contra la tabla entera. Son LECTURAS —en WAL no necesitan al escritor— retenidas
dentro de una transacción de escritura.

EFECTO: el ciclo del daemon no acreditaba ⇒ la vigilancia FLAPEABA. Con `VIGILANCIA_MUDA_S`
por debajo de la ventana, la flota se habría declarado sorda con el watcher sano.

LA CURA: primero se calcula, después se escribe. El escritor se toma en la última línea.
"""
from __future__ import annotations
import pathlib


def _cuerpo(nombre: str) -> str:
    t = pathlib.Path("servicio.py").read_text().split("\n")
    i = next(k for k, l in enumerate(t) if l.startswith(f"def {nombre}("))
    fin = next(k for k in range(i + 1, len(t)) if t[k].startswith(("def ", "async def ", "@")))
    return "\n".join(t[i:fin])


def test_la_wiki_no_escribe_antes_de_resolver_las_citas():
    """El ⊖: devolver el `INSERT INTO pages` arriba —o meter cualquier escritura antes del
    bucle— reabre la ventana de 64 s. Lo que se fija es el ORDEN, que es el invariante."""
    c = _cuerpo("reindex_wiki")
    esc = min((c.index(x) for x in ('con.executemany("INSERT OR REPLACE INTO pages',
                                    'con.execute("DELETE FROM citas")')
               if x in c), default=-1)
    assert esc > 0, "no encuentro las escrituras de la wiki"
    bucle = c.index("for rel, ledger, ref, clase in cit:")
    assert esc > bucle, (
        "la wiki escribe ANTES de resolver las citas: eso toma el escritor y lo retiene "
        "durante toda la resolución (64,28 s medidos el 2026-09-01)")


def test_las_citas_se_escriben_en_bloque_y_no_una_a_una():
    """Una escritura por cita dentro del bucle es la misma avería con otra cara: da igual
    que la transacción se abra tarde si se abre en la primera iteración."""
    c = _cuerpo("reindex_wiki")
    bucle = c.index("for rel, ledger, ref, clase in cit:")
    fin = c.index('_tomo_el_escritor("·wiki")', bucle)
    assert 'INSERT OR REPLACE INTO citas' not in c[bucle:fin], (
        "se escribe una cita por vuelta: el escritor queda tomado desde la primera")
    assert "filas_citas.append" in c[bucle:fin], "las citas no se acumulan para el bloque"


def test_la_ventana_del_escritor_se_MIDE_y_no_se_supone():
    """⊕ del instrumento. Sin esta medida la avería es invisible: el log decía 7,50 s de
    `reindex` con 0,03 s de escritor, y yo dimensioné dos curas contra el número de fuera.
    Si alguien quita `ULTIMO_LOCK`, la próxima regresión no se ve venir."""
    t = pathlib.Path("servicio.py").read_text()
    assert "ULTIMO_LOCK" in t, "se ha quitado el medidor de la ventana del escritor"
    assert t.count("_tomo_el_escritor(") >= 3, (
        "el sello de «primera escritura» no cubre los tres sitios que escriben (el UPDATE "
        "del bucle de reindex, su executemany, y la wiki). Falló ya una vez: puse el "
        "instrumento en el executemany y el escritor se tomaba 90 líneas antes, así que "
        "la medida era una cota INFERIOR disfrazada de medida")
    i = t.index("def _tomo_el_escritor")
    assert "setdefault" in t[i:i + 900], (
        "el sello ya no es idempotente: si la segunda escritura pisa a la primera, se "
        "vuelve a medir desde el sitio equivocado, que es el defecto que esto cura")


def test_el_presupuesto_del_ack_sigue_siendo_ACOTADO():
    """⊕ OBLIGATORIO: la tentación era curar esto subiendo la espera del ack hasta ganar
    siempre. Eso lo convierte en una llamada que se cuelga un minuto, y el 503 con
    `que_hacer` —que existe para que el cliente DECIDA— dejaría de llegar nunca."""
    import re
    t = pathlib.Path("servicio.py").read_text()
    m = re.search(r'for espera in \(([^)]*)\)', t)
    assert m, "no encuentro el presupuesto de reintento del ack"
    total = sum(float(x) for x in m.group(1).split(","))
    assert total <= 12, f"el ack espera {total}s en total: deja de fallar y empieza a colgar"
