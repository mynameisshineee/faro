"""REKEY (rol, carril, ledger) — los 6 falsadores adjudicados (cto #1194).

El defecto que motiva el rekey: la clave v1 (agent, ledger) comparte UNA fila de
cursor entre todos los carriles del rol, así que el segundo carril heredaba la
posición de lectura del primero. La cura es ADITIVA: `cursors_v2` /
`cursor_generations_v2` con clave (role, carril, ledger), backfill bajo el
centinela `carril=''`, V8 lee y escribe SOLO la clave nueva, legacy sigue en la
v1 — ventana de doble lectura SIN alimentación cruzada.

Lo que este fichero falsa, pieza por pieza:

1. DOS CARRILES del mismo rol: cursores SEPARADOS sin fuga. Si la clave v2
   ignorara `carril`, el segundo carril heredaría el cursor del primero y su
   bandeja saldría vacía.
2. MONOTONÍA POR CLAVE y TAPIA STALE: la generación cuenta por (rol, carril,
   ledger) — no comparte contador entre carriles — y un grant que apunta a un
   estado que ya pasó muere en ACK_GRANT_STALE (la tapia no se re-abre al
   partir la clave).
3. ATRIBUCIÓN del backfill: el estado v1 se copia a `carril=''`, centinela que
   NINGÚN carril real reclama ni lee (el scope V8 resuelve el carril desde la
   credencial). Idempotente por bandera en `meta`.
4. RESCATE EN CLAVE NUEVA: `reconstruir_indice` reconstruye `cursors_v2` con su
   clave compuesta y ancla por eid — si `cursors_v2` saliera de
   TABLAS_RESCATADAS, una corrupción resetearía la bandeja V8 de todo carril.
5. REPLAY PRE→POST RESCATE: un grant V8 usado devuelve el MISMO recibo después
   de reconstruir (el estado del replay sobrevive a la foto).
6. LOS 403 V8 INTACTOS: mismatch de rol y de carril siguen cortando ANTES de
   tocar estado, con el camino v2 vivo.

Ejecución: turno de sdet en copia aislada del SHA (jamás el worktree del autor).
Escrito y compilado bajo capacidad NO-GO, como el resto del lote.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

from fastapi.testclient import TestClient

from .conftest import construir

A = "cred-be-rekey-a-00000000"     # rol be, carril demo
B = "cred-be-rekey-b-00000000"     # rol be, carril demo2 (MISMO ledger)
# OTRO ROL: el censo del arnés (conftest ROSTER) sólo trae {be, cto} — un rol
# fuera del censo mata el import entero (sdet #1308, Clase A: posición 3).
Q = "cred-qa-rekey-000000000"      # rol cto (OTRO rol), carril demo


def _dos_carriles(tmp_path, monkeypatch):
    """Dos credenciales del MISMO rol con carriles DISTINTOS hacia el MISMO
    ledger — la configuración exacta que la v1 no podía distinguir."""
    demo_md = tmp_path / "DEMO-LEDGER.md"     # conftest escribe las 2 entradas aquí
    carriles = tmp_path / "carriles-rekey.tsv"
    carriles.write_text(
        "carril\tledger_path\twiki_path\testado\tnotas\n"
        f"demo\t{demo_md}\t-\tcompleto\t-\n"
        f"demo2\t{demo_md}\t-\tcompleto\t-\n"
    )
    mapa = tmp_path / "creds-rekey.json"
    mapa.write_text(json.dumps({
        A: {"rol": "be", "carril": "demo", "principal_id": "wl-be-a"},
        B: {"rol": "be", "carril": "demo2", "principal_id": "wl-be-b"},
        Q: {"rol": "cto", "carril": "demo", "principal_id": "wl-qa"},
    }))
    s = construir(tmp_path, monkeypatch, extra_env={
        "LLMINBOX_CARRILES": str(carriles),
        "LLMINBOX_CREDENCIALES": str(mapa),
        "LLMINBOX_CREDENCIALES_SHA": hashlib.sha256(mapa.read_bytes()).hexdigest(),
        "LLMINBOX_WATCHER_TOKEN": "w",
    })
    hA = {"X-Llminbox-Token": A, "X-Llminbox-Carril": "demo"}
    hB = {"X-Llminbox-Token": B, "X-Llminbox-Carril": "demo2"}
    hQ = {"X-Llminbox-Token": Q, "X-Llminbox-Carril": "demo"}
    return s, hA, hB, hQ


def _sobre_grant(c, h) -> dict:
    r = c.get("/inbox/backend", params={"only": "demo-ledger"}, headers=h)
    assert r.status_code == 200, r.text
    g = json.loads([x.strip() for x in r.text.splitlines()
                    if x.strip().startswith('{"hasta"')][-1])["ack"]
    assert g, f"sin grant en el sobre: {r.text[-300:]}"
    return g


def _ack(c, h, grant: dict, arrival: int):
    return c.post("/inbox/backend/ack",
                  json={"grant": grant, "arrival": arrival}, headers=h)


def test_pendientes_no_sobre_cuenta_a_un_agente_v8_sin_v1(tmp_path, monkeypatch):
    """(falsador del hallazgo security #1299) Un agente V8 SIN fila v1 pero CON
    cursor v2 real (A consumió TODO el ledger) NO puede salir en `/pendientes`
    con su backlog entero: el MAX de DOS argumentos de SQLite es escalar y
    PROPAGA NULL — `MAX(NULL, 42)` es NULL, y sin la segunda caída del COALESCE
    el cutoff caía a -1 y TODAS las entradas contaban como pendientes. Con la
    v1 vacía y v2 en la última llegada, la fila de (backend, demo-ledger)
    desaparece del agregado."""
    s, hA, _hB, _hQ = _dos_carriles(tmp_path, monkeypatch)
    with TestClient(s.app) as c:
        s.barrido()
        g = _sobre_grant(c, hA)
        assert c.get("/pendientes", headers={"X-Llminbox-Token": "test-token",
                                             "X-Llminbox-Watcher": "w"}
                     ).json()["pendientes"], "el ⊕ falta: nadie pendiente antes del ack"
        r = _ack(c, hA, g, max(g["allowed_arrivals"]))
        assert r.status_code == 200, r.text
        cuerpo = c.get("/pendientes", headers={"X-Llminbox-Token": "test-token",
                                               "X-Llminbox-Watcher": "w"}).json()
        filas = [f for f in cuerpo["pendientes"]
                 if f["quien"] == "backend" and f["ledger"] == "demo-ledger"]
        assert filas == [], f"backend sobre-contado tras confirmar todo el grant: {filas}"


def test_dos_carriles_del_mismo_rol_cursors_separados_sin_fuga(tmp_path, monkeypatch):
    """(1) A consume TODO el ledger; B NO hereda nada: su bandeja sigue llena y
    su clave v2 no existe. FALSADOR: con clave (role, ledger) sin carril, la
    lectura de B encontraría el cursor de A y el sobre saldría vacío."""
    s, hA, hB, _hQ = _dos_carriles(tmp_path, monkeypatch)
    with TestClient(s.app) as c:
        s.barrido()
        g = _sobre_grant(c, hA)
        assert g["lane"] == "demo"
        assert g["allowed_arrivals"] == [0, 1], "la fixture debe contener dos llegadas"
        r = _ack(c, hA, g, max(g["allowed_arrivals"]))      # A se lo come entero
        assert r.status_code == 200, r.text

        con = sqlite3.connect(s.DB)
        filas = con.execute("SELECT carril, last_arrival FROM cursors_v2 "
                            "WHERE role='be' AND ledger='demo-ledger'").fetchall()
        v1 = con.execute("SELECT COUNT(*) FROM cursors").fetchone()[0]
        con.close()
        assert filas == [("demo", 1)]
        assert [f for f in filas if f[0] == "demo2"] == [], "fuga: B tiene fila"
        assert v1 == 0, "la v1 se escribió desde el camino V8 (alimentación cruzada)"

        sobre_b = c.get("/inbox/backend", params={"only": "demo-ledger"}, headers=hB)
        assert sobre_b.status_code == 200, sobre_b.text
        gB = json.loads([x.strip() for x in sobre_b.text.splitlines()
                         if x.strip().startswith('{"hasta"')][-1])
        # B ve las MISMAS entradas que A ya consumió: la clave partida no
        # alimenta lecturas entre carriles (ni hereda posición, ni la tira).
        assert gB["hasta"]["demo-ledger"] == 1
        assert gB["ack"]["allowed_arrivals"] == [0, 1]


def test_generacion_por_clave_y_tapia_stale(tmp_path, monkeypatch):
    """(2) La generación cuenta POR CLAVE (el primer ack de B es gen 1, no hereda
    el de A) y un grant cuyo estado ya pasó muere en ACK_GRANT_STALE."""
    s, hA, hB, _hQ = _dos_carriles(tmp_path, monkeypatch)
    with TestClient(s.app) as c:
        s.barrido()
        gA = _sobre_grant(c, hA)
        assert gA["allowed_arrivals"] == [0, 1]
        assert _ack(c, hA, gA, 0).status_code == 200        # A parcial: cursor=0

        gB = _sobre_grant(c, hB)
        assert gB["cursor_before"] == -1, "B heredó el cursor de A"
        recibo_b = _ack(c, hB, gB, min(gB["allowed_arrivals"]))
        assert recibo_b.status_code == 200, recibo_b.text
        assert recibo_b.json()["cursor_generation"] == 1, \
            "la generación no cuenta por clave"

        g3 = _sobre_grant(c, hB)                            # cb=0, gen=1, arrivals=[1]
        assert g3["cursor_generation"] == 1 and g3["cursor_before"] == 0
        assert g3["allowed_arrivals"] == [1]
        # OTRO escritor mueve el estado de la MISMA clave entre emisión y POST:
        # sólo la GENERACIÓN avanza (el fencing existe justo para esto).
        con = sqlite3.connect(s.DB)
        con.execute("UPDATE cursor_generations_v2 SET generation=2 "
                    "WHERE role='be' AND carril='demo2' AND ledger='demo-ledger'")
        con.commit()
        con.close()
        r = _ack(c, hB, g3, max(g3["allowed_arrivals"]))
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "ACK_GRANT_STALE"


def test_backfill_centinela_carril_vacio_idempotente(tmp_path, monkeypatch):
    """(3) El backfill copia v1 a `carril=''` UNA vez, y ningún carril real lee
    esa fila: `_cursor_v2(be, demo, …)` es None pese a que el estado existe."""
    s, hA, _hB, _hQ = _dos_carriles(tmp_path, monkeypatch)
    # construir importa la app; sólo el lifespan inicializa sus tablas.
    with TestClient(s.app):
        pass
    con = sqlite3.connect(s.DB)
    # El arranque ya corrió el backfill con la v1 vacía (bandera puesta): se
    # re-arms para sembrar estado v1 PREVIO a la migración y probar la copia.
    con.execute("DELETE FROM meta WHERE k='cursors_rekey_v2'")
    con.execute("INSERT INTO cursors(agent,ledger,last_arrival,updated) "
                "VALUES('be','demo-ledger',7,'2026-09-08T00:00:00')")
    con.execute("INSERT INTO cursor_generations(agent,ledger,generation) "
                "VALUES('be','demo-ledger',3)")
    con.commit()
    s._rekey_backfill_carril(con)
    s._rekey_backfill_carril(con)                # la 2ª pasada NO duplica
    filas = con.execute("SELECT carril,last_arrival,updated FROM cursors_v2 "
                        "WHERE role='be' AND ledger='demo-ledger'").fetchall()
    gens = con.execute("SELECT carril,generation FROM cursor_generations_v2 "
                       "WHERE role='be' AND ledger='demo-ledger'").fetchall()
    bandera = con.execute(
        "SELECT v FROM meta WHERE k='cursors_rekey_v2'").fetchone()[0]
    assert filas == [("", 7, "2026-09-08T00:00:00")], filas
    assert gens == [("", 3)], gens
    assert bandera == "1"
    # ATRIBUCIÓN, no herencia: el carril real de la credencial NO ve la fila ''.
    assert s._cursor_v2(con, "be", "demo", "demo-ledger") is None
    assert s._cursor_v2(con, "be", "demo2", "demo-ledger") is None
    con.close()


def test_rescate_reconstruye_en_clave_nueva_y_replay_sobrevive(tmp_path, monkeypatch):
    """(4)+(5) `reconstruir_indice` reconstruye `cursors_v2`/`cursor_generations_v2`
    en su clave compuesta (ancla por eid), conserva el grant SIN usar, y un grant
    YA USADO sigue devolviendo el MISMO recibo tras la reconstrucción."""
    s, hA, hB, _hQ = _dos_carriles(tmp_path, monkeypatch)
    with TestClient(s.app) as c:
        s.barrido()
        g = _sobre_grant(c, hA)
        recibo = _ack(c, hA, g, max(g["allowed_arrivals"])).json()
        gB = _sobre_grant(c, hB)                     # SIN usar: debe sobrevivir
        assert gB["lane"] == "demo2"
    con = sqlite3.connect(s.DB)
    antes_sin_usar = con.execute(
        "SELECT COUNT(*) FROM ack_grants WHERE used_arrival IS NULL").fetchone()[0]
    con.close()
    assert antes_sin_usar == 1

    assert s.reconstruir_indice("prueba de rescate rekey") is True

    con = sqlite3.connect(s.DB)
    fila = con.execute("SELECT carril,last_arrival FROM cursors_v2 "
                       "WHERE role='be' AND carril='demo' AND ledger='demo-ledger'"
                       ).fetchone()
    gen = con.execute("SELECT generation FROM cursor_generations_v2 "
                      "WHERE role='be' AND carril='demo' AND ledger='demo-ledger'"
                      ).fetchone()
    sin_usar = con.execute(
        "SELECT COUNT(*) FROM ack_grants WHERE nonce_hash=? AND used_arrival IS NULL",
        (hashlib.sha256(gB["grant"].encode("ascii")).hexdigest(),)).fetchone()[0]
    con.close()
    assert fila == ("demo", 1), "el cursor V8 no sobrevive en la CLAVE NUEVA"
    assert gen == (1,), "la generación de fencing v2 se pierde en la reconstrucción"
    assert sin_usar == 1, "el grant sin usar de B desaparece en la reconstrucción"

    # (5) replay del grant USADO: el mismo recibo, sin mutar nada — la fila con
    # su receipt_json sobrevivió a la foto doble.
    with TestClient(s.app) as c2:
        r = _ack(c2, hA, g, max(g["allowed_arrivals"]))
        assert r.status_code == 200, r.text
        assert r.json() == recibo


def test_los_403_v8_no_se_abren_con_la_clave_nueva(tmp_path, monkeypatch):
    """(6) Mismatch de rol (otra credencial pidiendo la bandeja/ack de backend) y
    de carril (cabecera ≠ credencial) siguen cortando ANTES de tocar estado: la
    tabla v2 queda exactamente igual después de los 4 intentos."""
    s, hA, _hB, hQ = _dos_carriles(tmp_path, monkeypatch)
    with TestClient(s.app) as c:
        s.barrido()
        g = _sobre_grant(c, hA)

        con = sqlite3.connect(s.DB)
        antes = con.execute("SELECT carril,last_arrival FROM cursors_v2").fetchall()
        con.close()

        r_rol_get = c.get("/inbox/backend", params={"only": "demo-ledger"}, headers=hQ)
        assert r_rol_get.status_code == 403
        assert r_rol_get.json()["detail"] == "ACK_PRINCIPAL_MISMATCH"

        r_lane_get = c.get("/inbox/backend", params={"only": "demo-ledger"},
                           headers={**hA, "X-Llminbox-Carril": "demo2"})
        assert r_lane_get.status_code == 403
        assert r_lane_get.json()["detail"] == "ACK_CREDENTIAL_LANE_MISMATCH"

        r_rol_ack = _ack(c, hQ, g, min(g["allowed_arrivals"]))
        assert r_rol_ack.status_code == 409
        assert r_rol_ack.json()["detail"]["code"] == "ACK_GRANT_MISMATCH"

        r_lane_ack = _ack(c, {**hA, "X-Llminbox-Carril": "demo2"}, g,
                          min(g["allowed_arrivals"]))
        assert r_lane_ack.status_code == 409
        assert r_lane_ack.json()["detail"]["code"] == "ACK_GRANT_MISMATCH"

        con = sqlite3.connect(s.DB)
        despues = con.execute("SELECT carril,last_arrival FROM cursors_v2").fetchall()
        con.close()
        assert despues == antes, "un guard V8 mutó estado antes de cortar"


def test_pendientes_combina_cursores_por_la_ruta_real(tmp_path, monkeypatch):
    """El agregado usa el cursor más avanzado de los dos almacenes.

    Se ejercita el consumidor HTTP, sin copiar su consulta SQL al test. Con
    ambas filas presentes deben funcionar los dos órdenes de avance; cuando
    falta una fila, SQLite no debe hacer desaparecer el cursor de la otra.
    """
    s, hA, _hB, _hQ = _dos_carriles(tmp_path, monkeypatch)
    with TestClient(s.app) as c:
        s.barrido()
        assert _sobre_grant(c, hA)["allowed_arrivals"] == [0, 1]
        casos = [
            (None, None, 2),  # ninguna fila: quedan las dos entradas
            (0, None, 1),     # sólo v1: la primera está leída
            (None, 1, 0),     # sólo v2: ambas están leídas
            (0, 1, 0),        # ambas: v2 lleva la posición más avanzada
            (1, 0, 0),        # ambas: v1 lleva la posición más avanzada
        ]
        for v1, v2, esperado in casos:
            with sqlite3.connect(s.DB) as con:
                con.execute("DELETE FROM cursors WHERE agent='be' AND ledger='demo-ledger'")
                con.execute("DELETE FROM cursors_v2 WHERE role='be' AND ledger='demo-ledger'")
                if v1 is not None:
                    con.execute("INSERT INTO cursors(agent,ledger,last_arrival) "
                                "VALUES('be','demo-ledger',?)", (v1,))
                if v2 is not None:
                    con.execute("INSERT INTO cursors_v2(role,carril,ledger,last_arrival) "
                                "VALUES('be','demo','demo-ledger',?)", (v2,))
            respuesta = c.get("/pendientes", headers={
                "X-Llminbox-Token": "test-token", "X-Llminbox-Watcher": "w"})
            assert respuesta.status_code == 200, respuesta.text
            filas = [f for f in respuesta.json()["pendientes"]
                     if f["quien"] == "backend" and f["ledger"] == "demo-ledger"]
            assert sum(f["n"] for f in filas) == esperado, (v1, v2, esperado, filas)
