"""⑰ `/lint` — denuncia el correo perdido de VERDAD, no el `LIKE '%→%'` naive.

Contexto (forense re-verificado contra el índice vivo, 2026-08-11): de las
26.315 entradas vigentes sin fila en `recipients`, la señal honesta es 367 —
cabecera con flecha REAL, `to`/`difusion` con contenido, NO retenida por
política (`por_arroba`). Un `head LIKE '%→%'` da 21.325 sin filtrar `ausente`
(basura de rotación) y 6.559 filtrándolo (HEARTBEAT con `→` decorativo +
prosa con `→` retórico). El diseño correcto re-ejecuta `lp._campos()` real
—el mismo extractor que ya decide `to`/`difusion`/`por_arroba` en
producción— porque es la única forma de heredar el filtro de censo
(`RE_AGENTE`) que distingue una flecha de dirección de una decorativa.

Estos tests seed FILAS DE `entries` DIRECTAMENTE (sin pasar por `reindex()`):
el estado que se está midiendo —flecha real sin fila en `recipients`— es
backlog de una re-derivación interrumpida (ver spec §2), no algo que el
indexador de HOY produzca sobre un ledger limpio de test. Sembrar a mano es
la única forma de reproducir ese estado en el arnés.
"""
from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from .conftest import construir, db_directa

# Censo propio de este fichero: necesita `security` (agente real, cabecera
# citada abajo), `bikeus` (agente real, actor del caso sintético `@`-precorte
# — hace falta un actor RECONOCIDO delante del `@`, o `_campos()` atribuye
# la autoría al propio `security` mencionado y el `@` deja de sumar: medido
# en vivo antes de fijar esta forma, ver comentario en el test) y `TODOS`
# (difusión real) — el ROSTER compartido de conftest.py no los tiene, y no
# se toca ese fichero por esto (parámetro `roster` de `construir()`, añadido
# para exactamente este caso).
ROSTER_SEG = {
    "agentes": [
        {"nombre": "backend", "humano": "operador", "clave": "", "rol": "be"},
        {"nombre": "security", "humano": "operador", "clave": "", "rol": "security"},
        {"nombre": "bikeus", "humano": "operador", "clave": "", "rol": "bikeus"},
    ],
    "humanos": [{"nombre": "operador", "alias": ["Operador"]}],
    "difusion": ["equipo", "TODOS"],
}


@pytest.fixture
def cliente_seg(tmp_path, monkeypatch):
    """Igual que la fixture `cliente` de conftest, pero con `ROSTER_SEG` y
    `LLMINBOX_ARROBA_DESDE` fijado (para el caso de retención por política)."""
    s = construir(tmp_path, monkeypatch, roster=ROSTER_SEG,
                  extra_env={"LLMINBOX_ARROBA_DESDE": "2026-08-01"})
    with TestClient(s.app) as c:
        s.barrido()
        c.headers.update({"X-Llminbox-Token": "test-token"})
        yield s, c


def _seed(servicio_mod, ledger, head, actor=None, ausente=None, ts=None, seq=900):
    """Inserta una fila de `entries` DIRECTAMENTE, sin recipients."""
    eid = hashlib.sha256(head.encode()).hexdigest()
    con = db_directa(servicio_mod)
    con.execute(
        "INSERT OR REPLACE INTO entries (ledger,eid,arrival,seq,line_no,byte_off,"
        "ts,actor,tipo,head,body,visto,ausente,provisional) VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
        (ledger, eid, seq, seq, seq, 0, ts, actor, None, head, head,
         "2026-08-11T00:00:00", ausente))
    con.commit()
    con.close()
    return eid


def test_lint_flecha_sin_recipients_cuenta(cliente_seg):
    """Cabecera REAL del corpus (`bik-marketing-web`, eid `3b759c478635…`,
    confirmado en el índice vivo 2026-08-11: actor `security`, difusión
    `TODOS`, sin fila en `recipients`)."""
    s, c = cliente_seg
    head = ("### [MSG security→TODOS] 2026-08-03 — alta del Head of "
            "Security en el canal 👋")
    _seed(s, "demo-ledger", head, actor="security")

    r = c.get("/lint", params={"ledger": "demo-ledger"})
    assert r.status_code == 200
    assert "dirigida por flecha, sin entregar: 1 de" in r.text


def test_lint_heartbeat_arrow_no_cuenta(cliente, servicio):
    """FALSADOR de la elección de diseño (no del bug): si el implementador
    usara `head LIKE '%→%'` en vez de re-ejecutar `_campos()`, este test
    fallaría — es la prueba de por qué el naive-LIKE está prohibido. Cabecera
    verbatim del corpus real (`bik-marketing-web`): el `→` es decorativo
    dentro del texto de estado de un HEARTBEAT, `_campos()` devuelve
    `to=[]`/`difusion=[]` (confirmado contra el índice vivo) y no debe
    contar."""
    head = ("### [HEARTBEAT bikeus-auto] 2026-07-30T13:00Z — monitor: "
            "bik.eus home HTTP 200 · sin-pushear:0 · prod:OK · "
            "→bikeus-sin-leer:1 — status: MONITOR")
    _seed(servicio, "demo-ledger", head)

    r = cliente.get("/lint", params={"ledger": "demo-ledger"})
    assert r.status_code == 200
    assert "dirigida por flecha, sin entregar: 0 de" in r.text


def test_lint_arroba_precorte_no_cuenta(cliente_seg):
    """Retención por POLÍTICA (`ARROBA_DESDE`), no bug: cabecera SIN flecha
    que menciona `@security` (censo real de este fichero), sello anterior al
    corte. `_campos()` sigue devolviendo `to=['security']` con
    `por_arroba=True` —el cómputo de `_campos()` no aplica el corte de
    fecha, eso lo hace `reindex()` al decidir si enruta—, así que el filtro
    correcto es `not por_arroba`, no el sello. Sintético a propósito (spec
    §6): necesita una fecha de test controlada, no hay un caso equivalente
    fácil de citar literal del corpus."""
    s, c = cliente_seg
    head = ("2026-07-15T10:00:00Z bikeus informa del cambio de alcance a "
            "@security antes del corte de política (sintético, sin flecha)")
    _seed(s, "demo-ledger", head, ts="2026-07-15T10:00:00")

    r = c.get("/lint", params={"ledger": "demo-ledger"})
    assert r.status_code == 200
    assert "dirigida por flecha, sin entregar: 0 de" in r.text


def test_lint_ausente_no_cuenta(cliente_seg):
    """Evita el 21.325 fantasma: una entrada `ausente IS NOT NULL` (copia
    histórica re-indexada, ya no vigente) con flecha real y destino real no
    debe contar aunque no tenga `recipients` — es basura de rotación, no
    correo perdido."""
    s, c = cliente_seg
    head = ("### [MSG security→TODOS] 2026-08-03 — alta del Head of "
            "Security en el canal 👋")
    _seed(s, "demo-ledger", head, actor="security",
          ausente="2026-08-05T00:00:00", seq=901)

    r = c.get("/lint", params={"ledger": "demo-ledger"})
    assert r.status_code == 200
    assert "dirigida por flecha, sin entregar: 0 de" in r.text


def test_lint_no_rompe_censo_section_ni_grep_ancla(cliente):
    """El ancla `──` (`humo.sh:204`, ≥7 vigías de la flota la parsean) sigue
    presente, y la sección de censo sigue apareciendo cuando `ledger` no se
    pasa — la fila nueva no puede desplazar ni romper ninguna de las dos."""
    r = cliente.get("/lint")
    assert r.status_code == 200
    assert "──" in r.text
    assert "── censo:" in r.text


def test_mutante_filtro_arroba_neutralizado_se_ve(cliente_seg, monkeypatch):
    """Mutación mínima: forzar `por_arroba=False` en TODO lo que devuelve
    `_campos()` neutraliza el filtro `not por_arroba` exactamente como si esa
    condición se hubiera quitado del `if`. Prueba que
    `test_lint_arroba_precorte_no_cuenta` de verdad depende de ese filtro:
    con el mutante activo, la misma entrada (retención por política) pasa de
    0 a 1."""
    s, c = cliente_seg
    head = ("2026-07-15T10:00:00Z bikeus informa del cambio de alcance a "
            "@security antes del corte de política (sintético, sin flecha)")
    _seed(s, "demo-ledger", head, ts="2026-07-15T10:00:00")

    original = s.lp._campos

    def sin_por_arroba(head, cola):
        # El stub tiene que devolver la MISMA forma que producción: cuando
        # `_campos` ganó `raw_tipo`, un stub de 6 campos hacía reventar el
        # endpoint con «too many values to unpack» y el test dejaba de medir
        # el mutante para medir su propio andamio.
        ts, actor, to, difusion, tipo, _, raw = original(head, cola)
        return ts, actor, to, difusion, tipo, False, raw

    monkeypatch.setattr(s.lp, "_campos", sin_por_arroba)

    r = c.get("/lint", params={"ledger": "demo-ledger"})
    assert r.status_code == 200
    assert "dirigida por flecha, sin entregar: 1 de" in r.text
