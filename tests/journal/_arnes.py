"""Piezas compartidas por las pruebas del journal."""
from __future__ import annotations

import re
import time
import unicodedata

import coordination as C
import ledger_parse as lp

PEPPER = b"pepper-de-pruebas-no-es-un-secreto-real"

# Allowlist EXPLÍCITA del arnés. El módulo es fail-closed —sin esto no escribe
# nadie—, así que las pruebas la declaran igual que la declararía un despliegue.
LANES = {
    "llminbox": ["llminbox", "l"],
    "carril-uno": ["ledger-uno", "l"],
    "carril-dos": ["ledger-dos", "l"],
}


class Reloj:
    """Reloj inyectable. Los vencimientos se PRUEBAN, no se esperan durmiendo:
    un test que duerme mide la paciencia del CI, no el vencimiento."""

    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def avanza(self, s):
        self.t += s
        return self.t


# CENSO DEL ARNÉS. El journal es fail-closed: sin `recipient_resolver` no acepta
# destinatarios, porque sin censo no puede prometer que el ACK sea alcanzable.
# Las pruebas lo declaran igual que lo declararía un despliegue — es la misma
# postura que ya tiene `LANES` aquí arriba, y por el mismo motivo.
#
# Devuelve `(rol_canónico | None, es_difusión)`. Los alias son los que el censo
# real tiene (`backend`→`be`, `cto-llminbox`→`cto`): sin al menos UN alias, un
# test verde no distinguiría «resuelve por rol» de «compara el literal».
_CENSO_PRUEBAS = {
    "be": ("be", False), "backend": ("be", False),
    "security": ("security", False), "sec": ("security", False),
    "cto": ("cto", False), "cto-llminbox": ("cto", False),
    "infra": ("infra", False), "qa": ("qa", False), "sdet": ("sdet", False),
    "db-migrations": ("db-migrations", False), "db-mig": ("db-migrations", False),
    "FLOTA": (None, True), "flota": (None, True), "TODOS": (None, True),
    "equipo": (None, True),
}


def censo(literal: str):
    """Fuera del mapa: NI rol NI difusión ⇒ el journal lo rechaza al aceptar.

    Deliberadamente NO cae a la identidad. Un resolvedor permisivo por defecto
    haría verde el fail-closed sin ejercitarlo: el test diría que la puerta
    existe cuando lo que existe es un pasillo.
    """
    return _CENSO_PRUEBAS.get(literal, (None, False))


# GRAMÁTICA DEL ARNÉS: las funciones REALES de `ledger_parse`, no una copia.
# `ledger_parse` es stdlib-only (comprobado), así que estas pruebas siguen
# corriendo en un entorno pelado — que es la razón de que este conftest no
# cuelgue del de `tests/pytest`.
#
# 🔑 Usar la autoridad de verdad es el punto: con una lista de tipos propia, el
# test diría verde mientras el journal acepta lo que `/append` rechaza, que es
# exactamente el defecto que la cura persigue.
#
# `normalize_resource` es la ÚNICA que se reescribe aquí, porque `tema_norm`
# vive en `servicio.py` y ése importa fastapi. La copia no puede divergir en
# silencio: `tests/pytest/test_lease_resource_y_tema_norm_no_divergen.py` la ata
# a la original sobre un corpus.
_TEMA_FUERA = re.compile(r"[^a-z0-9_]+")


def _normaliza_recurso(t: str) -> str:
    t = unicodedata.normalize("NFKD", (t or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return _TEMA_FUERA.sub("_", t).strip("_")[:120]


GRAMATICA = C.Grammar(
    canonical_kind=lp.canonical_tipo,
    opens_entry=lambda linea: bool(lp.H_ENTRY.match(linea)),
    normalize_resource=_normaliza_recurso,
)


# CREDENCIALES DE LA BARRERA. Prefijo propio y principal propio por carril: la
# capacidad de mover la puerta NO se le cuelga al runtime estándar, porque
# entonces cada test que abre una sesión normal podría cerrar el carril y el
# `PolicyDenied` de `close_admission` no tendría a quién negarle nada.
ADMISION = (C.CAP_ADMISSION_OPERATOR,)
PREFIJO_ADMISION = "cred-admision"


def abre_admision(j, *, lanes=None, verbs=None, prefijo=PREFIJO_ADMISION,
                  reason_code="ROLLOUT"):
    """Abre la barrera COMO LO HARÍA UN OPERADOR: sesión, capacidad y epoch.

    No hay atajo por debajo —ni un flag del constructor ni un `INSERT` del
    arnés— a propósito: un atajo probaría un camino que producción no tiene, y
    el `open` es justo el acto cuya autorización queremos que esté ejercitada en
    cada test verde.

    ORDEN: se LIGAN todas las credenciales y sólo DESPUÉS se emiten las
    sesiones. Una ligadura efectiva sube la generación del mapa e invalida toda
    sesión anterior, así que ligar/emitir intercalado mataría la primera —el
    mismo motivo por el que existe `sesiones()`.
    """
    carriles = list(lanes if lanes is not None else LANES)
    verbos = list(verbs if verbs is not None else C.ADMISSION_VERBS)
    for carril in carriles:
        j.bind_credential(f"{prefijo}-{carril}", principal=f"admision-{carril}",
                          role="infra", lane=carril, capabilities=ADMISION)
    emitidas = [(carril, j.open_session(f"{prefijo}-{carril}", ttl_s=900).token)
                for carril in carriles]
    for carril, token in emitidas:
        for verbo in verbos:
            # IDEMPOTENTE: `open -> open` es una transición ilegal a propósito
            # (quemaría un epoch y movería la valla sin cambiar nada), así que
            # el arnés PREGUNTA antes. Reabrir un journal sobre el mismo fichero
            # es un caso normal en estas pruebas.
            if j.admission(token, verbo).state == "open":
                continue
            j.open_admission(token, verbo, reason_code=reason_code)
    return dict(emitidas)


def journal(tmp_path, *, reloj=None, admision=True, **kw):
    """Por defecto usa el reloj REAL, y no es un detalle del arnés.

    Con un reloj falso por defecto, la base que se le pasa a OTRO proceso —o a
    otra instancia de `Journal`— lleva vencimientos del año 1970 y toda sesión
    nace caducada. El reloj inyectado se pide EXPLÍCITAMENTE donde se prueba un
    vencimiento; en el resto, falsearlo sólo esconde.
    """
    kw.setdefault("lane_ledgers", LANES)
    kw.setdefault("recipient_resolver", censo)
    kw.setdefault("grammar", GRAMATICA)
    j = C.Journal(str(tmp_path / "coordination.sqlite"), pepper=PEPPER,
                  clock=reloj or time.time, **kw)
    j.initialize()
    if admision:
        # La barrera nace CERRADA (ausencia = cierre). El arnés la abre para que
        # los tests que no van de la barrera sigan midiendo lo suyo; los que SÍ
        # van de ella pasan `admision=False` y ejercitan el fail-closed real.
        abre_admision(j, lanes=kw.get("lane_ledgers", LANES))
    return j


CAPS_RUNTIME = (C.CAP_OUTBOX_WORKER, C.CAP_INDEXER, C.CAP_COMMAND_WORKER)


def sesion(j, credential="cred-A", *, principal=None, role="be", lane="llminbox",
           ttl_s=900, capabilities=CAPS_RUNTIME):
    """El runtime estándar puede proyectar/indexar/ejecutar, no operar la cola.

    Los tests de mínimos pasan ``capabilities=()`` expresamente; el privilegio de
    operador sigue requiriendo ``OPERADOR`` y nunca se concede de oficio.
    """
    j.bind_credential(credential, principal=principal, role=role, lane=lane,
                      capabilities=capabilities)
    return j.open_session(credential, ttl_s=ttl_s)


OPERADOR = CAPS_RUNTIME + (C.CAP_OUTBOX_OPERATOR,)


def sesiones(j, *specs):
    """Abre VARIAS sesiones: primero LIGA todas las credenciales, luego emite.

    Hace falta desde M1-5: una ligadura efectiva sube la generación del mapa, y
    la generación invalida toda sesión anterior. Encadenando `sesion()` dos
    veces, la primera moría al atar la segunda — y el test parecía roto cuando lo
    roto era el orden. Cada `spec` es un dict con los kwargs de `sesion`.
    """
    for e in specs:
        j.bind_credential(e["credential"], principal=e.get("principal"),
                          role=e.get("role", "be"), lane=e.get("lane", "llminbox"),
                          capabilities=e.get("capabilities", CAPS_RUNTIME))
    return [j.open_session(e["credential"], ttl_s=e.get("ttl_s", 900))
            for e in specs]


INTENT = {"type": "message", "verb": "inform", "to": ["security"],
          "kind": "DELIVERED", "head": "candidato listo", "body": "cuerpo"}
