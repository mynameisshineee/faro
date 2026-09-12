"""Falsadores de la carrera A->B del lote de re-derivación y de la revisión sellada.

El P0 que estos tests fijan NO es el crash: es el ADELANTAMIENTO. Un `reindex` largo
lee su intención al principio y la consume al final; entre medias otro arranque puede
sustituir el lote por uno de destino distinto. Consumir por `ledger` a secas borraba la
fila NUEVA y sellaba con el destino VIEJO -> `rederive_pending` vacío, `meta` sellada
con A, censo vivo B, y NADIE vuelve a re-derivar porque el sello ya coincide consigo
mismo. Es el P0 original entrando por otra puerta, y `pending == 0` no lo distingue.
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time

from .conftest import construir


ROSTER_A = {
    "agentes": [
        {"nombre": "backend", "humano": "operador", "clave": "", "rol": "be"},
        {"nombre": "cto-A", "humano": "operador", "clave": "", "rol": "cto"},
    ],
    "humanos": [{"nombre": "operador", "alias": ["Operador"]}],
    "difusion": ["FLOTA"],
}
ROSTER_B = {**ROSTER_A, "agentes": ROSTER_A["agentes"] + [
    {"nombre": "agente-b", "humano": "operador", "clave": "", "rol": "b"}]}
ROSTER_C = {**ROSTER_A, "agentes": ROSTER_A["agentes"] + [
    {"nombre": "agente-c", "humano": "operador", "clave": "", "rol": "c"}]}


def _ledgers(tmp_path):
    demo = tmp_path / "DEMO-LEDGER.md"
    otro = tmp_path / "OTRO-LEDGER.md"
    demo.write_text("### [cto-A → backend · REQUEST] uno\nx\n")
    otro.write_text("### [cto-A → backend · REQUEST] dos\ny\n")
    return demo, otro


def _recargar(monkeypatch):
    for mod in ("servicio", "ledger_parse"):
        sys.modules.pop(mod, None)
    import servicio as s

    async def _noop():
        return

    monkeypatch.setattr(s, "vigilante", _noop)
    return s


def _estado(db_path):
    con = sqlite3.connect(db_path)
    try:
        return (
            con.execute("SELECT ledger,roster_v FROM rederive_pending "
                        "ORDER BY ledger").fetchall(),
            dict(con.execute("SELECT k,v FROM meta "
                             "WHERE k IN ('roster_v','parser_v')").fetchall()),
            con.execute("SELECT ledger,eid,who FROM recipients "
                        "ORDER BY ledger,eid,who").fetchall(),
            con.execute("SELECT agent,ledger,last_arrival,updated FROM cursors "
                        "ORDER BY agent,ledger").fetchall(),
        )
    finally:
        con.close()


def test_carrera_A_B_el_reindex_viejo_no_consume_el_lote_nuevo_ni_sella(
        tmp_path, monkeypatch):
    """Determinista: el reemplazo A->B se inyecta EN la ventana, no por timing.

    La inyección va en `_tomo_el_escritor`, que corre DESPUÉS de leer `pendiente` y
    ANTES de tocar `recipients`: exactamente el hueco que el CAS cierra. Sin sleeps,
    sin hilos, sin flakies.
    """
    s0 = construir(tmp_path, monkeypatch, roster=ROSTER_A)
    demo, _otro = _ledgers(tmp_path)
    con = s0.db()
    s0._preparar_indice(con)
    con.close()
    s0.barrido()
    con = sqlite3.connect(s0.DB)
    con.execute("INSERT OR REPLACE INTO cursors(agent,ledger,last_arrival,updated) "
                "VALUES ('backend','demo-ledger',0,'antes')")
    con.commit()
    con.close()

    # Lote con destino A.
    (tmp_path / "roster.json").write_text(json.dumps(ROSTER_B))
    s1 = _recargar(monkeypatch)
    con = s1.db()
    s1._preparar_indice(con)
    destino_A = con.execute(
        "SELECT roster_v FROM rederive_pending WHERE ledger='demo-ledger'").fetchone()[0]
    antes = _estado(s1.DB)
    assert destino_A, "ARNÉS: sin lote no hay carrera que probar"

    # En plena ventana, OTRO arranque sustituye el lote por destino C.
    original = s1._tomo_el_escritor
    disparado = {"n": 0}

    def _interpone(ledger):
        if disparado["n"] == 0:
            disparado["n"] = 1
            (tmp_path / "roster.json").write_text(json.dumps(ROSTER_C))
            otro_proc = _recargar(monkeypatch)
            c2 = otro_proc.db()
            otro_proc._preparar_indice(c2)
            c2.close()
        return original(ledger)

    monkeypatch.setattr(s1, "_tomo_el_escritor", _interpone)
    res = s1.reindex("demo-ledger", str(demo), con)
    con.close()
    assert disparado["n"] == 1, "ARNÉS: la sustitución no llegó a inyectarse"
    assert res.get("abortado") == "destino-cambiado", (
        "el reindex con destino caduco tiene que abortar, no completar")

    pend, sellos, recip, cursores = _estado(s1.DB)
    destinos = {r[1] for r in pend}
    assert pend, "🔴 el lote NUEVO fue consumido por un reindex de destino viejo"
    assert destino_A not in destinos, "el lote vigente no puede ser el caduco"
    assert sellos == antes[1], "no se sella con un destino que ya no manda"
    assert recip == antes[2], "el snapshot abortado no puede dejar recipients a medias"
    assert cursores == antes[3], "la carrera no mueve cursores"

    # Y la pasada siguiente, ya sin carrera, cierra el lote vigente.
    s2 = _recargar(monkeypatch)
    con = s2.db()
    s2._preparar_indice(con)
    con.close()
    s2.barrido()
    pend2, sellos2, _r2, cursores2 = _estado(s2.DB)
    assert pend2 == [], "el lote vigente tiene que poder cerrarse después"
    assert sellos2 == {"roster_v": s2.huella_censo(),
                       "parser_v": str(s2.lp.PARSER_V)}
    assert cursores2 == antes[3]


def test_sigkill_durante_el_reindex_no_deja_pending_vacio_con_sello_caduco(
        tmp_path, monkeypatch):
    """SIGKILL real (no `os._exit`): el kernel mata sin darle turno al proceso.

    La invariante que se comprueba es la del P0: NUNCA `pending` vacío con los sellos
    apuntando a una revisión que no es la viva. Cualquier otro reparto —lote intacto,
    o lote cerrado y sellos al día— es legítimo.
    """
    s0 = construir(tmp_path, monkeypatch, roster=ROSTER_A)
    demo, _otro = _ledgers(tmp_path)
    con = s0.db()
    s0._preparar_indice(con)
    con.close()
    s0.barrido()
    (tmp_path / "roster.json").write_text(json.dumps(ROSTER_B))
    s1 = _recargar(monkeypatch)
    con = s1.db()
    s1._preparar_indice(con)
    con.close()
    assert _estado(s1.DB)[0], "ARNÉS: hace falta lote pendiente antes del kill"

    guion = (
        "import servicio, time\n"
        "c = servicio.db()\n"
        "orig = servicio._tomo_el_escritor\n"
        "def lento(l):\n"
        "    r = orig(l)\n"
        "    time.sleep(30)\n"     # el kill cae aquí: dentro de la transacción
        "    return r\n"
        "servicio._tomo_el_escritor = lento\n"
        "print('LISTO', flush=True)\n"
        f"servicio.reindex('demo-ledger', {str(demo)!r}, c)\n"
    )
    p = subprocess.Popen([sys.executable, "-c", guion],
                         cwd=os.path.dirname(s1.__file__), env=os.environ.copy(),
                         stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "LISTO"
    time.sleep(1.0)
    p.send_signal(signal.SIGKILL)
    p.wait(timeout=30)
    assert p.returncode == -signal.SIGKILL

    pend, sellos, _r, _c = _estado(s1.DB)
    vivo_roster, vivo_parser = s1.huella_censo(), str(s1.lp.PARSER_V)
    al_dia = (sellos.get("roster_v") == vivo_roster
              and sellos.get("parser_v") == vivo_parser)
    assert pend or al_dia, (
        "🔴 pending vacío con sellos caducos: nadie re-derivará nunca")


def test_health_compara_sellos_con_las_huellas_vivas_y_publica_objetivos(
        tmp_path, monkeypatch):
    """`pending == 0` NO es criterio suficiente: una purga deja esa misma foto.

    Aquí el lote se PURGA a mano (que es lo que hacía el consumo sin versión) y se
    comprueba que readiness sigue cerrada por la comparación de revisiones, no por
    la cuenta de pendientes — que vale 0, igual que en un estado sano.
    """
    s0 = construir(tmp_path, monkeypatch, roster=ROSTER_A,
                   extra_env={"LLMINBOX_WATCHER_TOKEN": "w"})
    _ledgers(tmp_path)
    con = s0.db()
    s0._preparar_indice(con)
    con.close()
    s0.barrido()
    s0.SALUD["ultimo_ok"] = time.time()
    sano = s0.health()
    assert sano["rederivacion"]["pendientes"] == 0
    assert sano["rederivacion"]["revision"]["al_dia"] is True, (
        "ARNÉS: el estado de partida tiene que estar al día, si no el rojo no discrimina")

    # Censo nuevo -> lote; y se PURGA, dejando la foto del defecto: 0 pendientes.
    (tmp_path / "roster.json").write_text(json.dumps(ROSTER_B))
    s1 = _recargar(monkeypatch)
    con = s1.db()
    s1._preparar_indice(con)
    con.close()
    con = sqlite3.connect(s1.DB)
    con.execute("DELETE FROM rederive_pending")
    con.commit()
    con.close()

    s1.SALUD["ultimo_ok"] = time.time()
    salud = s1.health()
    rev = salud["rederivacion"]["revision"]
    assert salud["rederivacion"]["pendientes"] == 0
    assert rev["al_dia"] is False, "purgado y con sellos viejos NO es estar al día"
    assert rev["roster_v_vivo"] == s1.huella_censo()
    assert rev["sello_roster_v"] != rev["roster_v_vivo"]
    assert salud["ok"] is False, "readiness no puede abrir con la revisión desfasada"
    assert any("REVISIÓN DERIVADA DESFASADA" in a for a in salud["avisos"])

    # Y con lote VIVO se publican los objetivos, para separar purga de reintroducción.
    s2 = _recargar(monkeypatch)
    con = s2.db()
    s2._preparar_indice(con)
    con.close()
    s2.SALUD["ultimo_ok"] = time.time()
    salud2 = s2.health()
    objetivos = salud2["rederivacion"]["objetivos"]
    assert objetivos, "con lote vivo hay que poder ver QUÉ ledgers y con qué destino"
    assert {o["ledger"] for o in objetivos} >= {"demo-ledger"}
    assert all(o["roster_v"] == s2.huella_censo() for o in objetivos)
