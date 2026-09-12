"""① fail-closed — nombre no resoluble ⇒ 422, NUNCA cursor fantasma.

Cada test afirmativo va emparejado con su falsador explícito (T1): qué se vería
si el fail-closed NO estuviera cerrado.
"""
from __future__ import annotations

from .conftest import db_directa


def test_inbox_censado_ok(cliente):
    r = cliente.get("/inbox/backend")
    assert r.status_code == 200


def test_inbox_no_censado_422(cliente):
    """FALSADOR: si esto diera 200 con '(nada nuevo...)', el fail-closed no
    cierra — es el bug original (cursor fantasma) reproducido por otra vía."""
    r = cliente.get("/inbox/NOEXISTE9999")
    assert r.status_code == 422
    assert "NOEXISTE9999" in r.json()["detail"]
    assert "censo" in r.json()["detail"]


def test_cursor_censado_ok(cliente):
    r = cliente.get("/cursor/backend")
    assert r.status_code == 200
    body = r.json()
    assert "demo-ledger" in body


def test_cursor_no_censado_422(cliente):
    """FALSADOR: si esto diera 200 con -1 por ledger, el fail-closed no cierra
    en /cursor — un cursor fantasma leído en vez de escrito."""
    r = cliente.get("/cursor/agente-inventado-xyz")
    assert r.status_code == 422
    assert "agente-inventado-xyz" in r.json()["detail"]


def test_leido_censado_ok(cliente, servicio):
    r = cliente.post("/inbox/backend/leido", json={"hasta": {"demo-ledger": 1}})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["agent"] == "be"          # la clave es el ROL (②), no "backend"
    con = db_directa(servicio)
    fila = con.execute(
        "SELECT last_arrival FROM cursors WHERE agent='be' AND ledger='demo-ledger'"
    ).fetchone()
    con.close()
    assert fila is not None and fila["last_arrival"] == 1


def test_leido_no_censado_422_sin_fantasma(cliente, servicio):
    """El falsador LITERAL del cursor fantasma: no basta con mirar el código HTTP
    (podría ser 422 y aun así haber escrito la fila) — se comprueba el EFECTO en
    la tabla. Si `agent='NOEXISTE9999'` aparece en `cursors`, el fix es teatro
    aunque la respuesta diga 422.
    """
    r = cliente.post("/inbox/NOEXISTE9999/leido", json={"hasta": {"demo-ledger": 1}})
    assert r.status_code == 422
    con = db_directa(servicio)
    n = con.execute(
        "SELECT COUNT(*) c FROM cursors WHERE agent='NOEXISTE9999'"
    ).fetchone()["c"]
    con.close()
    assert n == 0


def test_mutante_resolver_neutralizado_se_ve(cliente, servicio, monkeypatch):
    """Mutación mínima (§5.3, barata): si `resolver_o_422` dejara pasar TODO sin
    comprobar el censo, el propio test del caso central
    (`test_leido_no_censado_422_sin_fantasma`) tendría que teñirse de rojo. Se
    simula el mutante con monkeypatch (sin tocar el fichero de producción) y se
    confirma que, bajo esa condición, aparece la fila fantasma que ese test
    prohíbe — o sea que el test SÍ detecta la ausencia del fail-closed.
    """
    monkeypatch.setattr(servicio, "resolver_o_422", lambda nombre: nombre)

    r = cliente.post("/inbox/NOEXISTE9999/leido", json={"hasta": {"demo-ledger": 1}})
    assert r.status_code == 200        # con el mutante, YA NO cierra en 422
    con = db_directa(servicio)
    n = con.execute(
        "SELECT COUNT(*) c FROM cursors WHERE agent='NOEXISTE9999'"
    ).fetchone()["c"]
    con.close()
    assert n == 1                      # y aparece justo el fantasma que el gate prohíbe


def test_inbox_por_rol_no_422(cliente):
    """Consistencia cruzada ①×②: 'be' es un ROL válido (roles_validos), no un
    nombre de agente del roster — debe resolver, igual que 'backend'.

    FALSADOR: si 'be' diera 422 aquí, ①(rol) y ②(migración, que escribe filas
    bajo agent='be') quedarían inconsistentes entre sí — la migración dejaría
    cursores bajo una clave que el propio fail-closed rechaza al leer.

    `== 200`, no `!= 422`: esta última deja pasar CUALQUIER error que no sea
    422 —un 500 por una excepción no capturada incluido— como si fuera éxito,
    que es precisamente la clase de aserción débil que el review×3 marcó.
    """
    r = cliente.get("/inbox/be")
    assert r.status_code == 200
