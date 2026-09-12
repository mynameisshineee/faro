"""Una bandeja más grande que `TOPE_INBOX` no se podía drenar NUNCA.

La regla anti-pérdida es correcta y se conserva: si no cabe todo, no se puede avanzar el
cursor a la entrada más nueva, porque en orden DESC eso ENTIERRA lo que no se enseñó.
El comentario del código lo explica con su incidente («pidió 102, se tragó 77»).

Pero de ahí salía un punto muerto que nadie había medido:

    no cabe ⇒ el cursor no avanza ⇒ la próxima llamada ve LO MISMO ⇒ no cabe ⇒ …

Con `limit` por debajo del atraso, repetir la llamada no cambia nada. Y el rótulo dice
«repite con limit=N», que es un consejo INSEGUIBLE cuando N > TOPE_INBOX (200).

MEDIDO contra el índice vivo el 2026-09-04, por pareja (rol, ledger):

    pendientes > 30   (no drena con el límite por defecto) ...... 166
    pendientes > 200  (no drena NI CON EL TOPE MÁXIMO) .......... 109  ← atascadas
                                                                        para siempre
    roles afectados ............................................. 27

O sea: 109 bandejas de la flota son INALCANZABLES por diseño. Entre ellas, la de
`security` sobre el ledger `llminbox` — 36 pendientes, cursor inexistente — que es por
donde le estaba pidiendo una revisión que bloquea una entrega.

LA CURA NO ES QUITAR LA REGLA, es darle la vuelta AL ORDEN cuando no cabe:

    cabe todo   → lo más nuevo primero, cursor a lo más nuevo   (IGUAL QUE HOY)
    NO cabe     → las MÁS VIEJAS desde el cursor, y el cursor avanza hasta la última
                  que se enseñó

Las dos ramas cumplen lo mismo —nunca se entierra lo que no se ha enseñado— y la
segunda además TERMINA: el atraso se drena en ⌈N/limit⌉ pasadas.

«Lo que uno se ha perdido se lee del final hacia atrás» sigue valiendo para el caso
normal, que es el que ese incidente describía. Para un atraso que no cabe, leer del
final hacia atrás es justamente lo que impide llegar al principio.
"""
from __future__ import annotations

import json
import os
import sqlite3

CAB = "### [cto-A → backend · REQUEST] E%03d\ncuerpo %d\n"


def _con():
    c = sqlite3.connect(os.environ["LLMINBOX_DB"]); c.row_factory = sqlite3.Row
    return c


def _sembrar(servicio, tmp_path, n):
    md = tmp_path / "DEMO-LEDGER.md"
    md.write_text(md.read_text() + "".join(CAB % (i, i) for i in range(n)))
    servicio.barrido()
    return md


def _vistas(txt):
    return {l.split("] ")[1].strip() for l in txt.splitlines()
            if "· REQUEST]" in l and "] E" in l}


def _cursor():
    f = _con().execute("SELECT last_arrival FROM cursors WHERE agent='be' "
                       "AND ledger='demo-ledger'").fetchone()
    return f["last_arrival"] if f else -1


def test_lo_que_cabe_se_sirve_como_siempre(servicio, cliente, tmp_path):
    """⊕ EL CONTROL QUE PROTEGE EL DISEÑO DE ANTES: con el atraso dentro del límite,
    ni el orden ni el cursor cambian — lo más nuevo primero y el cursor a la cabeza."""
    _sembrar(servicio, tmp_path, 5)
    txt = cliente.get("/inbox/backend", params={"only": "demo-ledger", "limit": 30}).text
    ent = [l for l in txt.splitlines() if "] E" in l]
    assert len(ent) >= 5
    assert "E000" in ent[0] and "E004" in ent[-1], "el orden cronológico se rompió"
    hasta = json.loads(next(l for l in txt.splitlines() if l.strip().startswith('{"hasta"')))
    cliente.post("/inbox/backend/leido", json=hasta)
    assert cliente.get("/inbox/backend", params={"only": "demo-ledger"}).text.count("] E") == 0


def test_una_bandeja_que_no_cabe_avanza_el_cursor(servicio, cliente, tmp_path):
    """⊖ EL PUNTO MUERTO. Con más pendientes que `limit`, el cursor NO se movía y la
    llamada siguiente veía exactamente lo mismo, para siempre."""
    _sembrar(servicio, tmp_path, 40)
    antes = _cursor()
    txt = cliente.get("/inbox/backend", params={"only": "demo-ledger", "limit": 10}).text
    hasta = json.loads(next(l for l in txt.splitlines() if l.strip().startswith('{"hasta"')))
    cliente.post("/inbox/backend/leido", json=hasta)
    assert _cursor() > antes, (
        f"el cursor sigue en {antes}: la bandeja no se puede drenar nunca")


def test_se_sirven_las_MAS_VIEJAS_cuando_no_cabe(servicio, cliente, tmp_path):
    """Si no cabe todo, avanzar exige empezar por el principio: servir las nuevas y
    mover el cursor hasta ellas es exactamente lo que enterraría el resto."""
    _sembrar(servicio, tmp_path, 40)
    txt = cliente.get("/inbox/backend", params={"only": "demo-ledger", "limit": 10}).text
    v = _vistas(txt)
    assert "E000" in v, f"no empieza por la más vieja: {sorted(v)[:4]}"
    assert "E039" not in v, "sirvió la más nueva con 30 sin enseñar debajo"


def test_drenando_a_trozos_no_se_pierde_ni_una(servicio, cliente, tmp_path):
    """⊖ EL QUE DE VERDAD IMPORTA: repetir hasta vaciar tiene que enseñar las 40
    exactamente una vez. Cubre a la vez las dos formas de fallar — enterrar (faltaría
    alguna) y no avanzar (no terminaría)."""
    _sembrar(servicio, tmp_path, 40)
    todas, pasadas = set(), 0
    while pasadas < 20:
        txt = cliente.get("/inbox/backend",
                          params={"only": "demo-ledger", "limit": 10}).text
        if "nada nuevo" in txt:
            break
        nuevas = _vistas(txt)
        assert not (nuevas & todas), f"repitió entradas ya servidas: {sorted(nuevas & todas)}"
        todas |= nuevas
        cliente.post("/inbox/backend/leido",
                     json=json.loads(next(l for l in txt.splitlines()
                                          if l.strip().startswith('{"hasta"'))))
        pasadas += 1
    assert pasadas < 20, "no terminó: el cursor no avanza"
    assert todas == {f"E{i:03d}" for i in range(40)}, (
        f"faltan {sorted({f'E{i:03d}' for i in range(40)} - todas)}")


def test_el_orden_normal_no_dependia_de_DESC(servicio, cliente, tmp_path):
    """Fija la MEDIDA que hizo que la cura fuera `ASC` a secas.

    La primera versión escribía `"DESC" if atras <= limit else "ASC"`, que se lee como
    una decisión y no lo es: cuando cabe todo, `DESC LIMIT n` invertido y `ASC LIMIT n`
    devuelven la misma lista. Su mutante («siempre ASC») SOBREVIVÍA — no había
    diferencia que medir — y ése fue el aviso.

    Esto lo pin-ea contra el fuente para que nadie «restaure» el DESC creyendo que
    protege algo: lo único que protegía era el caso donde daba igual.
    """
    fuente = open("servicio.py", encoding="utf-8").read()
    i = fuente.index("SELECT e.arrival,e.eid,e.ts,e.actor,e.tipo,e.line_no,e.head")
    bloque = fuente[i:i + 900]
    assert "ORDER BY e.arrival ASC LIMIT" in bloque, bloque[:300]
    assert "reversed" not in bloque, (
        "volvió el `reversed`: existía sólo para deshacer un DESC que ya no está")


def test_el_rotulo_no_dice_lo_mas_reciente_cuando_sirve_lo_mas_viejo(servicio, cliente,
                                                                     tmp_path):
    """Los DOS textos de la misma paréntesis tienen que decir la verdad.

    «lo más reciente» era cierto con el orden DESC. Desde que el atraso que no cabe se
    sirve de lo más viejo hacia delante, con 40 pendientes y `limit=10` lo que llega
    son las 10 PRIMERAS. Lo vi en producción justo después de arreglar el otro texto de
    ese mismo paréntesis: curé uno y dejé el de al lado mintiendo.
    """
    _sembrar(servicio, tmp_path, 40)
    parcial = cliente.get("/inbox/backend",
                          params={"only": "demo-ledger", "limit": 10}).text
    cabecera = next(l for l in parcial.splitlines() if l.startswith("── demo-ledger"))
    assert "lo más reciente" not in cabecera, (
        f"dice «lo más reciente» sirviendo las más viejas: {cabecera[:140]}")
    assert "E000" in parcial, "control: no está sirviendo las más viejas"

    # ⊕ y cuando SÍ cabe todo, «lo más reciente» sigue siendo verdad y se dice.
    # Se usa el OTRO ledger del arnés, intacto y con una sola entrada: forzar el
    # drenaje del de arriba para reutilizarlo me costó dos intentos y medía el arnés,
    # no el rótulo.
    entera = cliente.get("/inbox/backend", params={"only": "otro-ledger"}).text
    cab2 = next(l for l in entera.splitlines() if l.startswith("── otro-ledger"))
    assert "lo más reciente" in cab2, cab2[:140]
