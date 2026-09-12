"""Journal de coordinación nativa de llminbox — M1.

Implementa `coordination.sqlite`, la base DURABLE y SEPARADA que ADR-001 declara
fuente de verdad de la autoridad nativa. No conoce FastAPI, no conoce el índice
de búsqueda y no toca el markdown: es sólo el estado durable y sus invariantes.

POR QUÉ ES OTRO FICHERO Y NO OTRAS TABLAS EN `llminbox.sqlite`
--------------------------------------------------------------
El índice es DESECHABLE por diseño: `servicio.py` lo DROPea al cambiar la huella
de esquema y `reconstruir_indice()` lo rehace entero desde el markdown cuando se
corrompe. Un registro de autoridad dentro de esa estructura sólo lo salvaría una
LISTA (`TABLAS_RESCATADAS`) — y esa lista ya se quedó corta con pérdida medida
(96 `claims` el 2026-08-15). Aquí la garantía no es una lista: es ser otro
fichero, que ninguna de esas dos rutas nombra. Como efecto secundario, una
imagen anterior que no conozca este módulo tampoco puede destruir el journal:
no lo abre.

CONTRATO DE DURABILIDAD (ADR-001 §Storage and recovery boundary)
----------------------------------------------------------------
Cada conexión: `foreign_keys=ON`, `journal_mode=WAL`, `synchronous=FULL`,
`busy_timeout` acotado. `synchronous=FULL` NO es copiado del índice a propósito:
el índice usa `NORMAL` porque se puede reconstruir, y bajo WAL `NORMAL` puede
perder transacciones YA COMITEADAS en un corte. `accepted` significa «el journal
posee el evento de forma durable», así que aquí se paga el fsync.

Toda mutación abre `BEGIN IMMEDIATE`. Con transacción diferida, un
lee-y-luego-escribe puede recibir `SQLITE_BUSY_SNAPSHOT`, que `busy_timeout` NO
reintenta: es exactamente la forma de la carrera de idempotencia.

API PÚBLICA (pequeña a propósito; la cablea `@backend` detrás del gateway)
--------------------------------------------------------------------------
    j = Journal(path, pepper=...)          # no abre nada todavía
    j.initialize()                         # migración transaccional a durable_v

    identidad
        j.bind_credential(cred, principal=..., role=..., lane=...) -> Binding
        j.resolve_credential(cred)                  -> Binding | None   (no autoriza)
        j.reload_credential_map({cred: {...}})      -> int   (ATÓMICO, generation++)
        j.rotate_map_generation()                   -> int
        j.open_session(cred, ttl_s=...)             -> IssuedSession  (token 1 vez)
        j.refresh_session(token, ttl_s=...)         -> IssuedSession
        j.revoke_session(runtime_instance, reason)
        j.authenticate(token)                       -> SessionView | None

    eventos
        j.accept_event(token, idempotency_key=..., intent={...}) -> Acceptance
        j.receipt(receipt_id)                       -> dict
        j.transitions(receipt_id)                   -> list[dict]
        j.append_transition(receipt_id, state, detail=None)

    proyección
        j.claim_outbox(worker, lease_s=...)         -> OutboxItem | None
        j.mark_materialized(event_id, entry_eid=..., ledger=...)
        j.mark_outbox_failed(event_id, error=..., retry_in_s=...)

    exclusión
        j.acquire_lease(token, resource, ttl_s=...) -> Lease
        j.renew_lease(token, resource)              -> Lease
        j.release_lease(token, resource)
        j.check_fence(resource, fencing_token)      -> None | raise FencingConflict

    comandos
        j.submit_command(token, workstream_id=..., revision=..., payload=...)
        j.may_execute(command_id)                   -> bool

    denegaciones y salud
        j.record_denial(principal_id, reason)       -> receipt_id | None
        j.record_unknown_credential(cred)
        j.health()                                  -> dict

LO QUE ESTE MÓDULO NO HACE, DICHO EN VOZ ALTA
---------------------------------------------
No renderiza markdown, no hace el append con `flock`, no habla HTTP y no decide
la política `(lane, verb)`. Expone la tabla `outbox` y sus verbos para que el
trabajador de `@backend` reutilice el appendeador que YA existe (`publicar.py`).
Escribir aquí un segundo appendeador sería la clase de defecto que este repo ya
tiene documentada: «una segunda forma de indexar que sólo corre el peor día del
servicio es la que nunca se prueba».
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import functools
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat as stat_mod
import sys
import tempfile
import threading
import time
import unicodedata
import weakref
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

# ── Versión del esquema durable ──────────────────────────────────────────────
# Se comprueba ANTES de servir mutaciones. Una base escrita por una versión
# SUPERIOR no se migra ni se toca: se rechaza (ADR-001: «migrations are
# transactional and never run against a newer unknown version»). Ésa es la
# mitad del rollback que el código puede garantizar por sí solo.
DURABLE_V = 7

DEFAULT_BUSY_TIMEOUT_MS = 5_000
DEFAULT_SESSION_TTL_S = 900
MAX_OUTBOX_LEASE_S = 3_600
MAX_PROJECTION_REPAIR_RESIDUES = 64
MAX_PROJECTION_REPAIR_VALUE = (1 << 63) - 1
DEFAULT_DENIAL_QUOTA = 5           # recibos individuales por (principal, motivo, cubo)
DEFAULT_DENIAL_BUCKET_S = 300
DEFAULT_UNKNOWN_ROWS_MAX = 64      # cota DURA de filas del contador de desconocidos
DEFAULT_FP_ROTATION_S = 3600       # época de rotación de la huella de desconocidos

_PEPPER_CHECK = b"llminbox-credential-ref-v1"

# Capacidad para sacar a mano un item del outbox. Es la única vía por la que una
# escritura ACEPTADA deja de estar pendiente sin haber llegado al markdown, así
# que no puede tenerla cualquiera que comparta carril con ella.
CAP_OUTBOX_OPERATOR = "outbox_operator"
CAP_OUTBOX_WORKER = "outbox_worker"
CAP_INDEXER = "indexer"
CAP_COMMAND_WORKER = "command_worker"
# Revocar la sesión de OTRO principal. No existía y por eso `revoke_session` no
# tenía a quién exigírselo: sin una capacidad que nombrar, la única alternativa
# honesta era negar siempre el caso ajeno. Con ella, un operador declarado puede
# hacerlo y queda AUDITADO como cualquier otra mutación.
CAP_SESSION_ADMIN = "session_admin"

# Abrir, cerrar y SELLAR la barrera de admisión de un carril. Dedicada a
# propósito y separada de `CAP_OUTBOX_OPERATOR`: sacar UN item de la cola y
# cerrar la puerta de TODO un carril no son el mismo poder, y quien puede lo
# primero no tiene por qué poder lo segundo. Un operador de cola que además
# pudiera sellar convertiría un acto de mantenimiento en uno irreversible.
CAP_ADMISSION_OPERATOR = "admission_operator"

# Capacidades estrechas del control plane de flota (ADR-002). No se derivan del
# nombre del rol y ninguna implica a las otras.
CAP_RUNTIME_OBSERVE = "runtime.observe"
CAP_RUNTIME_RECOVER = "runtime.recover"
CAP_RUNTIME_READ = "runtime.read"
CAP_ORGANIZATION_READ = "organization.read"
CAP_ORGANIZATION_ACTIVATE = "organization.activate"

RUNTIME_STATUSES = frozenset({
    "absent", "fresh", "stale", "degraded", "stopped", "recovering",
})
DETECTOR_STATES = frozenset({
    "viva-con-progreso", "viva-sin-obligacion", "atascada", "en-bucle",
    "muda", "sin-armar", "inarmable", "ilegible", "indeterminado",
    "sensor-mudo",
})
RUNTIME_OBSERVATION_KINDS = frozenset({
    "cycle_ack", "started", "exited", "resource_degraded",
    "resource_recovered", "recovery_succeeded", "recovery_failed",
})
RUNTIME_REASON_CODES = frozenset({
    "PROCESS_PRESENT", "PROCESS_STARTED", "PROCESS_EXITED",
    "CPU_SATURATED", "MEMORY_SATURATED", "IO_STALLED",
    "HEARTBEAT_RECOVERED", "RECOVERY_SUCCEEDED", "RECOVERY_FAILED",
    "RECOVERY_REQUESTED", "DEADLINE_EXCEEDED", "ORGANIZATION_ACTIVATED",
})
RECOVERY_REASON_CODES = frozenset({
    "OPERATOR_REQUESTED", "STALE_RUNTIME", "DEGRADED_RUNTIME",
    "STOPPED_RUNTIME",
})
RECOVERY_ACTION_CODES = frozenset({"RESTART_RUNTIME"})
ORGANIZATION_ATTESTATION_STATES = frozenset({
    "attested", "stale", "unattested",
})
ORGANIZATION_POLICY_CODES = frozenset({
    "STANDARD", "REVIEW_REQUIRED", "HUMAN_APPROVAL_REQUIRED",
})
ESCALATION_TRIGGER_CODES = frozenset({
    "BLOCKED", "REVIEW_REQUIRED", "INCIDENT", "RECOVERY_FAILED",
})
MAX_SUPERVISOR_SEQ = (1 << 63) - 1
MAX_STATUS_SEQ = (1 << 63) - 1
MAX_CPU_MILLIS = 1_000_000
MAX_RSS_BYTES = 1 << 50
MAX_HEARTBEAT_AGE_MS = 31 * 24 * 60 * 60 * 1000

_OBSERVATION_STATUS = {
    "cycle_ack": "fresh",
    "started": "fresh",
    "exited": "stopped",
    "resource_degraded": "degraded",
    "resource_recovered": "fresh",
    "recovery_succeeded": "fresh",
    "recovery_failed": "degraded",
}
_OBSERVATION_REASONS = {
    "cycle_ack": frozenset({"PROCESS_PRESENT"}),
    "started": frozenset({"PROCESS_STARTED"}),
    "exited": frozenset({"PROCESS_EXITED"}),
    "resource_degraded": frozenset({
        "CPU_SATURATED", "MEMORY_SATURATED", "IO_STALLED"}),
    "resource_recovered": frozenset({"HEARTBEAT_RECOVERED"}),
    "recovery_succeeded": frozenset({"RECOVERY_SUCCEEDED"}),
    "recovery_failed": frozenset({"RECOVERY_FAILED"}),
}

# ── BARRERA DE ADMISIÓN ─────────────────────────────────────────────────────
# Verbos GATEADOS, en tupla CERRADA. Un verbo que nadie decidió gatear no se
# admite: crearía una puerta que no protege nada y cuyo `closed` por ausencia
# negaría un camino que nunca se pensó cerrar.
#
# `outbox.claim`, `outbox.materialized`, `outbox.failed` y `outbox.abandon` NO
# están aquí, y es la mitad que sostiene la otra: si cerrar la puerta parara
# también el DRENADO, un carril cerrado no podría vaciarse nunca y el sello
# —que exige `pending = failed = 0`— sería inalcanzable por construcción.
ADMISSION_VERBS = ("events.accept", "outbox.requeue")
ADMISSION_STATES = ("open", "closed", "sealed")
# `sealed` es TERMINAL. Un sello del que se puede volver es un `closed` con otro
# nombre, y entonces no acredita nada a quien lee el historial.
_TRANSICIONES_ADMISION = {
    "closed": frozenset({"open", "sealed"}),
    "open": frozenset({"closed"}),
    "sealed": frozenset(),
}
# CERO TEXTO LIBRE DURABLE: el motivo es un código de vocabulario cerrado, por
# el mismo argumento escrito abajo para `REASON_CODES`. `outbox_operations.reason`
# es texto libre y es la cicatriz que esto no repite: un motivo libre acaba
# llevando el id, la ruta o la clave que lo provocó, y esta tabla es durable.
ADMISSION_REASON_CODES = frozenset({
    "ROLLOUT",
    "INCIDENT",
    "MAINTENANCE",
    "DRAIN_FOR_ROLLBACK",
    "SCHEMA_MIGRATION",
})
# ``SCHEMA_MIGRATION`` acredita una fila ``origin=migration`` sin operador ni
# sesión. Aceptarlo por las APIs firmadas fabricaría el origen contrario con el
# motivo reservado. El DDL conserva el vocabulario total porque la migración sí
# necesita ese valor; esta mitad es el vocabulario exacto del operador.
ADMISSION_OPERATOR_REASON_CODES = frozenset(
    ADMISSION_REASON_CODES - {"SCHEMA_MIGRATION"})

# Un fallo del proyector termina en el journal y en las transiciones que puede
# consultar el carril. Por eso no admite texto libre: paths, nombres de cuenta
# o incluso tokens incrustados en una excepción no pueden convertirse en una
# vía de exfiltración durable. El detalle crudo pertenece a los sensores
# internos del proceso, no al contrato de coordinación.
OUTBOX_FAILURE_CODES = frozenset({
    "PROJECTOR_CONFIG_ERROR",
    "PROJECTOR_FRAME_CONFLICT",
    "PROJECTOR_IO_ERROR",
    "PROJECTOR_OS_ERROR",
    "PROJECTOR_ERROR",
})
DEFAULT_OUTBOX_FAILURE_CODE = "PROJECTOR_ERROR"

# Versión de la CANONICALIZACIÓN del hash de idempotencia. Se guarda con cada
# fila: el día que cambie la forma de canonicalizar, las filas viejas no se
# pueden comparar con las nuevas, y compararlas igual convertiría un reintento
# legítimo en un 409.
# El algoritmo ACTUAL es la 2: la 1 no incluía el par de fencing. Dejarlo en 1
# habría sido lo peor de los dos mundos — las filas migradas llevan el DEFAULT 1
# y se habrían comparado con hashes calculados por otra fórmula bajo el mismo
# número, que es justo lo que este campo existe para impedir.
REQ_HASH_V = 2

# VOCABULARIO CERRADO de motivos de rechazo. Cerrado a propósito: un motivo
# libre acaba llevando la clave, el recurso o el id que lo provocó, y entonces
# cada rechazo es una fila nueva —la cardinalidad la elige quien ataca, no
# quien defiende— y la cuota por cubo deja de acotar nada.
REASON_CODES = frozenset({
    "SESSION_INVALID",        # token caducado, revocado o de otra generación
    "IDEMPOTENCY_CONFLICT",   # misma clave, cuerpo distinto
    "CAUSE_REJECTED",         # causa no nativa, inexistente o de otro carril
    "FENCING_CONFLICT",       # token de valla no vigente
    "LEASE_CONFLICT",         # recurso con dueño, o soltar lo ajeno
    "LEDGER_NOT_ALLOWED",     # destino fuera de la allowlist del carril
    "ATTRIBUTION_REJECTED",   # la petición traía su propia atribución
    "OUTBOX_EXHAUSTED",       # se agotaron los intentos de materialización
    "FENCED_PAIR_INVALID",    # medio par recurso/token de valla
    "COMMAND_TRANSITION_INVALID",  # transición de comando ilegal
    "DELIVERY_CONFLICT",      # acuse de quien no es destinatario del evento
    "COMMAND_REVISION_CONFLICT",   # (carril, workstream, revisión) repetida
    "SUBJECT_NOT_FOUND",      # evento / recibo / item de outbox inexistente
    "RECEIPT_STATE_INVALID",  # el recibo no está donde ese paso exige
    "OPERATION_INVALID",      # operación manual mal formada o fuera de estado
    # RED DE SEGURIDAD: cualquier rechazo de mutación con token conocido que no
    # encaje arriba cae aquí. Antes se iba SIN auditar, así que el hueco de
    # auditoría lo abría justo el error que nadie había clasificado todavía.
    "OPERATION_REJECTED",
    "POLICY_DENIED",          # capacidad ausente para el verbo pedido
    "ADMISSION_CLOSED",       # la barrera de (carril, verbo) no está abierta
    "ADMISSION_CONFLICT",     # transición de barrera perdida: estado/epoch/drenado
    "SCHEMA_INDETERMINATE",   # datos sin sello de versión
    "REPLAY_UNVERIFIABLE",    # fila de idempotencia con hash no comprobable
    "RECIPIENT_UNRESOLVED",   # destinatario fuera del censo, o censo ausente
    "GRAMMAR_REJECTED",       # cuerpo que la gramática del ledger no admite
    "GRAMMAR_UNAVAILABLE",    # sin autoridad de gramática inyectada
    "OBSERVATION_SEQUENCE_CONFLICT",
    "ORGANIZATION_CONFLICT",
    "RECOVERY_CONFLICT",
    # `D19` cerrado por `@cto` (`00:38:45Z`) sobre adjudicaciones convergentes de
    # `@contratosbik` (`00:18:35`, enmendada `00:24:19`) y `@cpo` (`00:25:21`):
    # una clave con un codepoint fuera de `[\x20-\x7E]` NO es atribución, así que
    # devolvía un código que MENTÍA. Tiene el suyo.
    "KEY_CHARSET_REJECTED",   # clave con codepoint fuera de ASCII imprimible
    # ── POLITICA UNICA DE PRESUPUESTOS · `@cto` RULING 04:45:35Z ──────────
    # TRES ejes, TRES codigos, y la SUPERFICIE va en `field` — no en el codigo.
    # Un vocabulario que crece con cada campo nuevo es el que nadie mantiene:
    # `7` superficies x `3` ejes serian `21` codigos.
    #
    # ⛔ `INTENT_TOO_LARGE` queda RETIRADO ANTES DE NACER, por el mismo ruling.
    # Lo tuve implementado unas horas; retirarlo no cuesta nada porque nunca
    # salio de mi arbol, y dejarlo haria que `intent` tuviera codigo propio y las
    # otras seis compartieran otro — esa asimetria es la que produce vocabularios
    # inconsistentes.
    # 🔻 AMEND `05:02:46Z` (`MARK:cto-la-profundidad-tambien-es-politica-un-solo-
    # codigo-de-recurso`): mis TRES codigos se colapsan en UNO. `@cto` acepto la
    # objecion usando su propio criterio contra si mismo — «el mismo intent pasa
    # a ser valido si sube el tope, sin tocar un byte del cuerpo» vale IGUAL para
    # la profundidad: `12` es una constante, exactamente como `32 KiB`.
    # El test que lo decide, y que faltaba: «¿existe un valor de la constante que
    # lo haga aceptable?» SI -> `413` LIMITE_RECURSO · NO -> `422` FORMA.
    # La DIMENSION va en el cuerpo, no en el codigo — misma regla que ya saco la
    # superficie del codigo y llevo `21` a `3`.
    "RESOURCE_LIMIT_EXCEEDED",   # LIMITE_RECURSO -> 413
                                 # dimension: bytes|depth|nodes|cardinality
})


# ── Errores ──────────────────────────────────────────────────────────────────
# ── EL SANEADO DE TODO DETALLE QUE SALE ────────────────────────────────────
# Vive a nivel de MODULO y no dentro de `Journal` porque `JournalError` se define
# antes, y la garantia tiene que aplicarse en la BASE de las excepciones: ahi es
# el unico sitio donde cubre los `73` `raise` de una vez, los de hoy y los que
# alguien escriba manana.
DETALLE_MAX = 320
MARCA_TRUNCADO = "...(trunc)"

# ── PRESUPUESTOS · `@cpo` RULING 08:35:36Z + AMEND 08:41 y 08:52 ─────────
# 🔻 LA PROCEDENCIA, CORREGIDA: estos numeros son de `@cpo`, NO del `ADR`
# sellado (`585c56b`) — lo grepee y no contiene ninguna cota. El `ADR` sella
# garantias de auditoria y superficies de fallo; los presupuestos son otro
# documento. Citarlos al `ADR` habria sido darles una autoridad que no tienen.
#
#     profundidad ....... 12       sin cambio (`Journal._PROFUNDIDAD_MAX`)
#     intent/payload .... 64 KiB   32 -> 64 KiB
#     metadatos ......... 4 KiB    trace · annotations · attestation · detail
#     causes/external ... 256 elementos  AND  4 KiB POR ELEMENTO
#     nodos ............. 8.192    POR PETICION · 1 nodo = 1 VALOR del canonico
#     TRANSPORTE ........ 1 MiB    ⛔ es del WIRE. Core NO tiene transporte y
#                                  aqui NO se implementa: seria un numero sin
#                                  mecanismo.
#
# 🔑 EL ALCANCE de `nodos` (`@cpo` `09:47:16Z`): el contador es **UNO POR
# PETICION** y atraviesa TODOS los campos de la operacion —`Event`:
# `intent` ∪ `trace` ∪ `causes` ∪ `external_causes`; `Command`: `payload` ∪ las
# dos secuencias—. **NO se reinicia por superficie**, y el exceso sale con
# `field="request"` porque no pertenece a ningun campo. `depth`, en cambio, NO
# se acumula: es propiedad de CADA documento, y ahi `field` si nombra el campo.
#
# 🔑 LA UNIDAD DE `nodos`, que es lo que convierte `8.192` en un contrato
# (`@cpo` `08:41`): **1 nodo = 1 VALOR del canonico; la clave NO cuenta**, con
# lo que `dict` y `lista` dan `N+1` los dos y la paridad de forma se cumple por
# construccion. Sin unidad, `8.192` anunciados eran `~4.096` para la mitad de
# las formas. El recorrido ya contaba asi.
#
# 🔑 LOS EJES SON ADITIVOS, NO ALTERNATIVOS (`@cpo` `08:52`, corrigiendo su
# propia tabla): `bytes`, `depth` y `nodes` se aplican a TODA carga de
# estructura libre —**incluidos los ELEMENTOS de una secuencia**— y
# `cardinality` se aplica ADEMAS a las secuencias. Su fila decia `cardinality`
# a secas, y ese «ademas» leido como «en vez de» dejaba `causes`/`external`
# **sin ningun eje de tamaño**: `[{"k": "A"*10_000_000}]` era `1` elemento,
# `2` nodos, y entraba como FILA DURABLE.
PAYLOAD_MAX_BYTES = 65536        # intent · command payload
# 🩸 DECIA «annotations · attestation · trace · detail» Y `detail` NO PASA POR
# AQUI: su llamada es `_congelar(detail, "detail", tope_bytes=None)` (:6107) —el
# eje BYTES se le RETIRO por orden del operador, porque rechazar por el tamaño de
# un diagnostico mata la operacion por su comentario—. Su cota real es
# `DETALLE_MAX = 320` y **TRUNCA**, aplicada en `_payload_de_recibo` (:2221), que
# es lo que dice la norma (`@cpo` §5: `detail 320 B TRUNCA · no suma`).
# ⇒ el comentario nombraba un campo que NO cubre, y en la cota EQUIVOCADA. Una
# constante que declara alcance de mas es peor que una sin comentario: la
# siguiente mano lee «detail esta acotado a 4096» y no vuelve a mirar.
# ⚠️ Y LO QUE ESE `None` DEJABA ABIERTO — **YA CERRADO**, por encargo del
# operador. `detail` era el UNICO campo de estructura libre del nucleo sin
# ningun eje de TAMAÑO: `tope_bytes=None` apaga el contador entero, asi que
# `{"x": "A"*32_000_000}` (dos nodos, profundidad uno) pasaba la puerta y se
# canonicalizaba para persistir `347 B`. Ahora lleva el techo GLOBAL —
# `CANONICAL_REQUEST_MAX_BYTES`, no este simbolo— y el corte es incremental.
# El detalle, con su medida y su frontera, esta en el propio `advance_command`.
# Falsador: `SUITE_D`, `test_el_detail_tiene_el_techo_GLOBAL_y_el_corte_es_INCREMENTAL`.
METADATO_MAX_BYTES = 4096        # annotations · attestation · trace
ELEMENTO_MAX_BYTES = 4096        # CADA elemento de causes / external_causes
# ⚠️ SIMBOLO PROPIO aunque hoy valga lo mismo que `METADATO_MAX_BYTES`: son dos
# COSAS distintas —presupuesto de metadato vs tope por elemento relacional— y un
# solo simbolo para dos significados mueve el segundo el dia que alguien toque
# el primero, en silencio. Es la trampa de «dos verdades» del reves.
CARDINALIDAD_MAX = 256           # causes · external_causes (numero de elementos)
NODOS_MAX = 8192                 # nodos POR PETICION (1 nodo = 1 VALOR)

# ── EL AGREGADO CANONICO DE LA PETICION ──────────────────────────────────
# 🔴 SIMBOLO DISTINTO, A PROPOSITO, de `RAW_BODY_MAX_BYTES` del Gateway — que
# **NO se define aqui** porque Core no tiene transporte. Miden COSAS DISTINTAS y
# el dia que alguien los funda, uno de los dos deja de proteger:
#   RAW_BODY_MAX_BYTES ...... bytes del CUERPO CRUDO que entra por el wire.
#   CANONICAL_REQUEST_MAX_BYTES  bytes del JSON CANONICO de los campos
#                             controlados por cliente, YA normalizados.
# 🔑 Y NO ESTAN ORDENADOS: el canonico puede ser MAYOR que el crudo. `1e10` son
# `4` bytes crudos y `10000000000.0` —`13`— canonicos, `3,25x`; y al reves, `20 MB`
# de espacios colapsan a `49 B` (medido por `@qa`). ⇒ **un tope sobre el crudo NO
# acota el canonico, y ninguno de los dos sustituye al otro.**
CANONICAL_REQUEST_MAX_BYTES = 1048576

# ── `field`: EL ENUM, EN DATO Y NO EN LA PROSA ───────────────────────────
# 🩸 `@qa` (`09:53:23Z`): «`field` es enum CERRADO en la prosa y en NINGUN DATO».
# Tenia razon y era mio: escribi «enum cerrado» en un comentario y no habia ni un
# `frozenset` que lo impusiera. **Un enum que solo vive en la prosa no es un
# enum, es una intencion** — y la siguiente mano mete `causes[0]` sin enterarse.
# `request` es el valor del exceso CRUZADO: el que no pertenece a un campo.
#
# 🔻 EL NOMBRE DICE QUE ES UN SUBCONJUNTO, y el `_CORE` no es decorativo: este
# `frozenset` enumera lo que **ESTE NUCLEO** puede emitir en `field`, NO el
# vocabulario del contrato de wire, que es de `@contratosbik` y puede ser mas
# ancho. Llamarlo `CAMPOS_CONTRATO` a secas —sin `_CORE`— afirmaba una
# equivalencia con el canon que yo no
# he medido: si alguien lo cita como «el contrato», cita mal. Alinearlo con el
# canon —o declarar que son el mismo conjunto— es una adjudicacion ajena.
CAMPOS_CONTRATO_CORE = frozenset({
    "request",            # agregado / contador que cruza campos
    "intent", "payload", "trace", "annotations", "attestation", "detail",
    "causes", "external_causes",
})


def _escapa(x: str) -> str:
    """NFKD, fuera diacriticos, y `ascii()` sin sus comillas. Sale imprimible-ASCII."""
    t = unicodedata.normalize("NFKD", x)
    t = "".join(c for c in t if not unicodedata.combining(c))
    return ascii(t)[1:-1]


def _saneado(texto: object, tope: int | None = None) -> str:
    """UNA linea · CERO controles · ASCII imprimible · longitud ACOTADA.

    Y dos propiedades mas que la version anterior NO tenia, ambas medidas:

    🔪 ① TRABAJO ACOTADO. Antes hacia `ascii()` sobre la cadena ENTERA y truncaba
    despues, asi que el coste era proporcional a lo que manda el atacante, no al
    tope. **Medido: factor de pico `9,0-9,5x` en tres tamaños; con `1.000.000` de
    caracteres de control el pico fue `9.448.929 B` para devolver `320`.** Ahora
    se RECORTA ANTES de expandir: se escapan como mucho `~2*tope` caracteres
    crudos, pase lo que pase por la entrada. El escapado solo EXPANDE, asi que
    `tope` caracteres crudos siempre bastan para llenar `tope` de presupuesto.

    🔪 ② SE PRESERVA LA COLA. Antes cortaba por la cabeza y punto, asi que un
    PREFIJO no fiable lo bastante largo se comia el mensaje y **borraba la
    razon**: medido, a partir de `~400` caracteres de prefijo desaparecian
    `U+0009` y `alfabeto` del detalle. Y la razon vive al FINAL. Ahora se guarda
    cabeza + marcador + COLA, con la cola dimensionada para que el sufijo
    diagnostico sobreviva a un prefijo arbitrariamente grande.
    """
    limite = max(0, DETALLE_MAX if tope is None else tope)
    if limite == 0:
        return ""
    bruto = str(texto)                      # 🧊 UNA lectura del objeto

    # ── ① recorte CRUDO antes de expandir ──────────────────────────────────
    # `margen` de sobra para que el escapado nunca se quede corto de material.
    margen = limite + len(MARCA_TRUNCADO) + 4
    if len(bruto) > 2 * margen:
        cab_bruta, col_bruta = bruto[:margen], bruto[-margen:]
    else:
        cab_bruta, col_bruta = bruto, ""

    entero = _escapa(cab_bruta) + _escapa(col_bruta)
    if len(entero) <= limite:
        return entero

    # ── ② cabeza + marcador + COLA, SIEMPRE que se trunque ────────────────
    # 🩸 La primera version solo hacia esto cuando la entrada era ENORME (la
    # rama del recorte crudo). En la banda INTERMEDIA —un prefijo de `400`, que
    # ya desborda `320` pero no dispara el recorte— truncaba por la cabeza y
    # volvia a BORRAR LA RAZON. El recorte crudo es una optimizacion de TRABAJO;
    # la cabeza+cola es la POLITICA, y se aplica siempre.
    if limite < len(MARCA_TRUNCADO):
        return entero[:limite]
    presupuesto = limite - len(MARCA_TRUNCADO)
    # La cola se lleva hasta un TERCIO del presupuesto: suficiente para el
    # sufijo diagnostico (`(U+XXXX), fuera del alfabeto…`) sin dejar la cabeza
    # —donde va la clave ofensora— en nada.
    n_col = min(len(entero), presupuesto // 3)
    # 🩸 `entero[-n_col:]` con `n_col == 0` devuelve LA CADENA ENTERA, no vacio:
    # `s[-0:]` es `s[0:]`. Con `tope == len(MARCA_TRUNCADO)` el presupuesto es
    # `0`, `n_col` es `0`, y el resultado pasaba de `10` a `58` caracteres — el
    # invariante `len(res) <= tope` roto en la esquina exacta que un test mio
    # cubria. La cazo el test, no yo, y es la MISMA familia que el `t[:-5]` de
    # hace dos rondas: aritmetica de slices que en el caso cero hace lo
    # contrario de lo que se lee.
    cola = entero[-n_col:] if n_col else ""
    return entero[:presupuesto - n_col] + MARCA_TRUNCADO + cola


class JournalError(Exception):
    """Raíz de todo lo que este módulo rechaza a propósito.

    `receipt_id` lo rellena el auditor cuando el rechazo dejó recibo, para que
    quien responda al cliente pueda citarlo. `None` significa que no lo hubo
    (token desconocido, base no escribible), nunca que no se intentó.
    """

    def __init__(self, *args):
        # 🔒 LA GARANTIA, EN LA BASE. Toda subclase la hereda sin hacer nada, y
        # un `raise` nuevo escrito dentro de un año la tiene por construccion.
        # No cambia NINGUNA clase ni NINGUN codigo: solo el TEXTO que sale.
        # 🔒 TODOS los args, no `args[0]`, y no «si ya es str».
        #
        # 🔴 LO QUE ESTO CIERRA, medido sobre la version anterior:
        #     JournalError(lista)            -> 2023 B  (arg no-str: NO se saneaba)
        #     JournalError("ok", VENENO)     -> 2024 B  (args[1:] intactos)
        #     JournalError("ok", 1, VENENO)  -> 2027 B
        # El `isinstance(args[0], str)` hacia que la garantia dependiera de la
        # ARIDAD y del TIPO de la llamada — o sea, de que nadie escriba nunca un
        # `raise` con dos argumentos. Un invariante global no puede apoyarse en
        # una convencion de llamada futura.
        #
        # 🧊 Y `str(a)` se llama UNA sola vez por objeto, antes de nada: un
        # `__str__` que cambie entre lecturas miente UNA vez, y esa mentira es la
        # que se sanea Y la que se guarda. Es la misma cura que el congelado de
        # `k = str(k_vivo)`, aqui aplicada a la puerta por la que salen TODAS.
        congelados = [str(a) for a in args]
        super().__init__(_saneado(" · ".join(congelados)) if congelados else "")

    receipt_id: str | None = None


class SchemaTooNew(JournalError):
    """La base la escribió una versión superior. No se migra ni se muta."""


class JournalReadOnly(JournalError):
    """El journal no admite escrituras. Las LECTURAS siguen disponibles.

    Existe como error propio y no como un `OperationalError` suelto porque el
    ADR pide una respuesta distinguible (`JOURNAL_READ_ONLY`) en vez de un bucle
    de reinicio: este repo ya tumbó el bus 11 veces por negarse a arrancar.
    """


class AuthError(JournalError):
    """Credencial, sesión o generación del mapa no válidas."""


class IdempotencyConflict(JournalError):
    """Misma clave, cuerpo distinto. CERO mutación."""


class FencingConflict(JournalError):
    """Token de fencing viejo tras un relevo. CERO cambio de dominio."""


class CauseRejected(JournalError):
    """Una causa nativa que no es un identificador nativo (p.ej. un hash bridge)."""


class FencedPairInvalid(JournalError):
    """`fenced_resource` y `fencing_token` desparejados.

    Tipado y MAPEADO a un motivo: con un `JournalError` genérico el rechazo no
    entraba en la auditoría, así que la única mutación que se colaba sin dejar
    rastro era justo la que pide protección y la pide mal.
    """


class AdmissionClosed(JournalError):
    """La barrera de `(carril, verbo)` no está abierta. CERO mutación.

    LA AUSENCIA ES CIERRE. Sin fila de historial la puerta está cerrada: `None`
    no significa «nadie la configuró, luego pasa», significa que nadie ha
    decidido abrirla — y una barrera cuyo defecto es dejar pasar es un adorno.
    """


class AdmissionConflict(JournalError):
    """La transición de la barrera perdió: estado, epoch o drenado no encajan.

    Separada de `AdmissionClosed` porque son dos sujetos distintos: aquélla la
    ve quien ESCRIBE contra una puerta cerrada; ésta la ve quien intenta MOVER
    la puerta y llega tarde, con el epoch de una lectura que ya no vale.
    """


class ReplayUnverifiable(JournalError):
    """Hay fila de idempotencia pero su hash no se puede comprobar.

    Ni se devuelve el recibo original —eso replaya una petición que puede ser
    otra— ni se acepta como nueva —eso duplica—. Se declara que no se puede
    verificar, que es la única de las tres que no miente.
    """


class PolicyDenied(JournalError):
    """La capacidad exigida no la tiene este principal."""


class SchemaIndeterminate(JournalError):
    """Hay datos y no hay `durable_v`. No se sella: no se sabe qué son.

    Tratarlo como «base nueva» ponía el sello de la versión ACTUAL sobre tablas
    de forma desconocida — la mentira más cara, porque a partir de ahí nadie
    vuelve a mirar.
    """


class MigrationFailed(JournalError):
    """La migración no pudo dejar la base íntegra: se deshizo entera."""


class MigrationSnapshotRequired(MigrationFailed):
    """Una v6 no se convierte en v7 sin fotografía retenida y verificada."""


class ObservationSequenceConflict(JournalError):
    """La secuencia del supervisor se repite o retrocede para ese target.

    En el rechazo REGRESIVO/REPETIDO transporta `latest`: el
    `MAX(supervisor_seq)` ya calculado DENTRO de la misma transacción para
    la tupla exacta (observer, runtime del observer, generación del
    observer, lane, target y generación del target) — quien recibe el 409
    se re-alinea sin segunda consulta. Los demás usos del tipo (p.ej.
    entrada fuera de rango) se construyen SIN `latest`: `None` es «no
    aplica». El transporte HTTP (`max_supervisor_seq`) sólo existe cuando
    `latest` no es `None`.
    """

    def __init__(self, mensaje: str, *, latest: int | None = None):
        if latest is not None and (
                type(latest) is not int or not 1 <= latest <= MAX_SUPERVISOR_SEQ):
            raise ValueError(
                "latest debe ser int exacto (nunca bool/None implícito) y "
                "1 <= latest <= MAX_SUPERVISOR_SEQ")
        super().__init__(mensaje)
        self.latest = latest


class OrganizationConflict(JournalError):
    """La revisión organizativa no es íntegra, monótona o lane-local."""


class RecoveryConflict(JournalError):
    """La recuperación no casa con target, generación, idempotencia o fencing."""


class GrammarRejected(JournalError):
    """El cuerpo no pasa la gramática del ledger: `kind` no canónico, `head`
    demasiado largo o con salto, `body` que abre entrada, recurso vacío.

    Las mismas cuatro guardas que `/append` (`servicio.py`) se ganó con
    cicatriz, aplicadas al camino nativo llamando a las MISMAS funciones. Un
    validador propio aquí sería la segunda fuente: `/append` acabó aceptando
    `8` de `12` tipos por tener su propia lista.
    """


class GrammarUnavailable(JournalError):
    """No hay autoridad de gramática inyectada.

    Misma postura que el censo ausente: sin ella no se puede prometer que el
    registro sea proyectable, y el journal acepta escrituras DURABLES. Aceptar
    lo que no se puede validar deja dentro registros que necesitarán migración
    el día que la guarda llegue — que es exactamente lo que midió la auditoría.
    """


class RecipientUnresolved(JournalError):
    """Un destinatario que el censo inyectado no resuelve, o censo AUSENTE.

    Fail-closed en la puerta: sin censo el journal no puede prometer que el ACK
    sea alcanzable, y guardar el literal crudo es exactamente lo que produce el
    `delivery_progress` eterno. Se RECHAZA al aceptar, como `/append` ya hace
    con `_indexable()`, en vez de dejar una entrada que nadie podrá acusar.
    """


class ResourceLimitExceeded(JournalError):
    """Cuota de recurso superada. `LIMITE_RECURSO` -> el wire deriva `413`.

    UNA clase para los CUATRO ejes —`bytes`, `depth`, `nodes`, `cardinality`—
    porque los cuatro son POLITICA: existe un valor de la constante que vuelve
    aceptable el documento, sin tocar un byte del cuerpo. Ese es el test que
    `@cto` fijo en el AMEND y el que impide que el proximo eje caiga en la
    casilla equivocada.

    El detalle lleva `dimension` y `field`, asi que un cliente que quiera
    distinguir los lee, y el que no, los trata igual — que es lo correcto,
    porque los cuatro se arreglan igual: mandando menos.

    ⛔ `422` queda para lo que ningun tope vuelve valido: JSON no parseable, tipo
    incorrecto, CICLO, no canonicalizable.

    🔻 A7 · LOS CAMPOS DEL CONTRATO SON ATRIBUTOS, NO PROSA. Iban dentro del
    texto del `raise`, y ese texto pasa por `_saneado` —cabeza + `...(trunc)` +
    cola, `320 B`—: un mensaje largo PARTE el `key=value` por la mitad. El wire
    los lee de aqui; el texto queda para humanos y puede truncarse sin romper
    nada. `seen_at_least` y NO `seen`: el contador corta en `limite+1` y NO sabe
    cuanto habia (RULING `05:18:28Z` §6) — publicar el total exigiria recorrer
    el resto, que es exactamente el trabajo que el limite existe para no hacer.
    """

    def __init__(self, *args, dimension=None, field=None, limit=None,
                 seen_at_least=None):
        super().__init__(*args)
        self.dimension = dimension
        self.field = field
        self.limit = limit
        self.seen_at_least = seen_at_least


class KeyCharsetRejected(JournalError):
    """Clave con un codepoint fuera de `[\x20-\x7E]` tras NFKD.

    Separada de `AttributionRejected` por adjudicación de `@cto` (`D19`, cierre
    `00:38:45Z`): una clave con una `а` cirílica o un tabulador **no es
    atribución**, y devolver `ATTRIBUTION_REJECTED` le decía al cliente honesto
    que había intentado suplantar a alguien. El código miente sobre el motivo.
    """


class AttributionRejected(JournalError):
    """La petición traía su propia atribución. NO es un fallo de sesión.

    Mezclarlo con `SESSION_INVALID` borraba la diferencia entre «tu token no
    vale» y «tu token vale y has intentado firmar como otro». La segunda es la
    que hay que poder contar.
    """


class SubjectNotFound(JournalError):
    """El evento, recibo o item de outbox al que apunta la operación no existe."""


class ReceiptStateInvalid(JournalError):
    """El recibo no está en el estado que ese paso exige."""


class OperationInvalid(JournalError):
    """Operación manual mal formada o sobre un estado que no la admite."""


class CommandRevisionConflict(JournalError):
    """Esa `(carril, workstream, revisión)` ya existe.

    Lleva `command_id`: el del comando YA REGISTRADO en ESTE carril. Sin él, un
    reintento honesto por respuesta perdida recibe `409` y no puede continuar —
    la idempotencia de comandos no es por `Idempotency-Key`, es por el UNIQUE
    `(lane, workstream_id, revision)`, así que el `409` es la única vía por la
    que el cliente puede recuperar su identificador.

    ⚠️ SIEMPRE del propio carril: la consulta que lo obtiene filtra por
    `view.lane`, así que este campo no puede transportar el id de un vecino. Es
    la razón de que se resuelva ahí y no con una búsqueda por `(workstream,
    revision)` a secas, que sí cruzaría.

    Tipado porque antes salía como `sqlite3.IntegrityError` del UNIQUE: un 500
    opaco en vez de un rechazo. Un fallo determinista y auditable no necesita
    inventar idempotencia de comandos —que el ADR no pide—, pero sí necesita
    tener nombre.
    """

    command_id: str | None = None


class DeliveryConflict(JournalError):
    """Acuse imposible: no eres destinatario, o el evento no está indexado."""


class CommandTransitionInvalid(JournalError):
    """Transición de comando fuera de la máquina, o sobre un terminal."""


class LedgerNotAllowed(JournalError):
    """Destino fuera de la allowlist del carril. CERO bytes escritos."""


class LeaseConflict(JournalError):
    """El recurso tiene dueño vivo y no eres tú."""


class PepperMismatch(JournalError):
    """El pepper no es el que creó esta base.

    Sin esto, un pepper equivocado no da error: recalcula OTROS `credential_ref`
    y da de alta principals nuevos en silencio, fragmentando la identidad que
    este journal existe para fijar.
    """


# ── Vistas de datos (lo que sale de la API, sin filas de sqlite sueltas) ─────
@dataclass(frozen=True)
class Grammar:
    """Autoridad de gramática del ledger, INYECTADA — nunca importada.

    `coordination` no conoce `ledger_parse` ni `servicio`: atarlo a ellos haría
    el journal indesplegable sin el servicio entero, y `ledger_parse` además
    arrastra el roster. El despliegue cablea:

        Grammar(canonical_kind=lp.canonical_tipo,
                opens_entry=lambda l: bool(lp.H_ENTRY.match(l)),
                normalize_resource=servicio.tema_norm)

    🔑 SON LAS MISMAS FUNCIONES QUE USA `/append`, no copias. Una segunda lista
    de tipos es cómo `/append` acabó aceptando `8` de los `12` canónicos, y una
    segunda `tema_norm` haría que `/claim` y los leases tuvieran espacios de
    nombres distintos — o sea que la compatibilidad ABRIRÍA el defecto que la
    exclusión cierra.
    """
    canonical_kind: Callable[[Any], "str | None"]
    opens_entry: Callable[[str], bool]
    normalize_resource: Callable[[str], str]
    canonical_agent_kind: Callable[[Any, int], "str | None"] | None = None
    head_max: int = 200


@dataclass(frozen=True)
class Binding:
    credential_ref: str
    principal_id: str
    principal: str
    role: str
    lane: str
    principal_source: str
    generation: int
    capabilities: tuple = ()


@dataclass(frozen=True)
class IssuedSession:
    """El ÚNICO objeto que lleva el token en claro, y sólo en memoria."""
    token: str
    runtime_instance: str
    principal_id: str
    role: str
    lane: str
    expires_at: float
    generation: int
    principal: str = ""
    principal_source: str = ""
    capabilities: tuple = ()


@dataclass(frozen=True)
class SessionView:
    runtime_instance: str
    principal_id: str
    principal: str
    role: str
    lane: str
    expires_at: float
    generation: int
    principal_source: str = ""
    capabilities: tuple = ()


@dataclass(frozen=True)
class Acceptance:
    event_id: str
    receipt_id: str
    replayed: bool
    occurred_at: str
    payload_sha: str


@dataclass(frozen=True)
class Lease:
    resource: str
    lane: str
    principal_id: str
    runtime_instance: str
    fencing_token: int
    expires_at: float


@dataclass(frozen=True)
class LegacyOutboxItem:
    """DTO histórico del outbox; no es el contrato de proyección M1.

    El constructor público original tenía cuatro campos de control y un
    ``payload`` libre. Se conserva con nombre propio para que los consumidores
    antiguos puedan deserializarlo durante la transición, sin hacer opcionales
    los campos de atribución/recibo que necesita un trabajo nuevo.
    """
    event_id: str
    ledger: str
    attempts: int
    claim_token: str
    payload: Mapping[str, Any] = field(default_factory=dict)


# Compatibilidad de import Y de constructor. El productor operativo de v3 no
# devuelve esta forma: ``claim_outbox`` devuelve ``ProjectionJob``.
OutboxItem = LegacyOutboxItem


@dataclass(frozen=True)
class AdmissionState:
    """Estado VIGENTE de la barrera de un `(carril, verbo)`, con su epoch.

    El `epoch` viaja con el estado porque es lo que hace falta para MOVER la
    puerta: quien lee y luego sella tiene que poder demostrar que sella sobre lo
    que leyó. Un estado sin epoch es una lectura que no se puede defender.
    """

    lane: str
    verb: str
    state: str
    epoch: int


@dataclass(frozen=True)
class ProjectionJob:
    event_id: str
    ledger: str
    attempts: int
    claim_token: str          # ficha del arriendo: hay que devolverla al marcar
    receipt_id: str
    recipients: tuple
    occurred_at: str
    payload_sha256: str
    attestation: Mapping[str, Any]
    trace: Mapping[str, Any] | None
    principal_id: str
    principal: str
    principal_source: str
    role: str
    lane: str
    runtime_instance: str
    intent: Mapping[str, Any]

    @property
    def payload_sha(self) -> str:
        """Alias de compatibilidad; el nombre canónico evita el ambiguo `sha`."""
        return self.payload_sha256

    @property
    def payload(self) -> Mapping[str, Any]:
        """Vista compatible para consumidores M1 anteriores al contrato tipado."""
        return {"intent": self.intent, "occurred_at": self.occurred_at,
                "payload_sha": self.payload_sha256}


@dataclass(frozen=True)
class ProjectionRepairEvidence:
    """Huella acotada de residuos append-only reparados por el proyector.

    Sólo transporta offsets, tamaños y SHA-256. Nunca paths ni texto de una
    excepción: esta evidencia termina en una transición visible al carril.
    """

    residues: tuple[tuple[int, int, str], ...]

    def __post_init__(self) -> None:
        if type(self.residues) is not tuple or not self.residues:
            raise OperationInvalid("residues debe ser una tupla no vacía")
        if len(self.residues) > MAX_PROJECTION_REPAIR_RESIDUES:
            raise OperationInvalid(
                "demasiados residuos en una sola evidencia de reparación")
        previous_end = -1
        for item in self.residues:
            if type(item) is not tuple or len(item) != 3:
                raise OperationInvalid(
                    "cada residuo exige offset, longitud y sha256")
            offset, length, digest = item
            if (type(offset) is not int or offset < 0
                    or type(length) is not int or length <= 0
                    or offset > MAX_PROJECTION_REPAIR_VALUE
                    or length > MAX_PROJECTION_REPAIR_VALUE
                    or offset > MAX_PROJECTION_REPAIR_VALUE - length
                    or type(digest) is not str
                    or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                    or offset < previous_end):
                raise OperationInvalid("evidencia de residuo no canónica")
            previous_end = offset + length

    def to_detail(self) -> dict:
        return {
            "residue_count": len(self.residues),
            "residues": [
                {"byte_off": offset, "byte_len": length, "sha256": digest}
                for offset, length, digest in self.residues
            ],
        }


@dataclass(frozen=True)
class OutboxCounts:
    """Las DOS cifras vivas de la cola de UN carril, leídas de UNA sola foto.

    Existe porque el par se venía pidiendo con dos llamadas —`pending_outbox` y
    `unresolved_outbox`—, y **cada una abre su propia transacción**. Entre las
    dos cabe un trabajador entero: el llamante que resta para sacar `failed`
    publica una cifra que no existió en ningún instante de la base, y con el
    signo equivocado puede salir NEGATIVA. Un tipo con las dos dentro no es
    azúcar: es el único sitio donde la atomicidad se puede prometer, porque
    quien devuelve un `int` suelto no sabe con qué se va a combinar después.

    `unresolved` es DERIVADO y no un tercer campo medido. Si viniera de su propia
    consulta volvería a ser un número que puede no cuadrar con los otros dos —
    exactamente el defecto que este tipo cierra.

    `lane` viaja dentro a propósito: la cifra sin el carril al que pertenece es
    la que se acaba pegando en el tablero de otro.
    """
    lane: str
    pending: int
    failed: int

    def __post_init__(self) -> None:
        """Forma canónica o nada. Un DTO congelado no protege su CONTENIDO: sin
        esto, `OutboxCounts("", None, True)` se construye sin ruido y el número
        falso viaja hacia arriba con la credibilidad del tipo puesta.

        Tres decisiones:

        · **`type(x) is int`, no `isinstance`.** En Python `bool` es SUBCLASE de
          `int`, así que `isinstance(True, int)` es `True`: con `isinstance`, un
          `True` colado desde un `SELECT` mal escrito pasa por entero, suma 1 en
          `unresolved` y se renderiza como una cuenta. Forma exacta o nada.
        · **`OperationInvalid` y NO una clase nueva.** La taxonomía de
          `JournalError` está CENSADA con un `==` (`33`), y ese censo existe para
          que nadie la ensanche de paso: un DTO interno no es motivo demostrado
          para moverla. Si algún día este fallo necesita código de motivo propio
          en el wire, ESO sería la necesidad, y se decide entonces.
        · **Mensajes CERRADOS: el valor sospechoso NO se interpola.** Si la fila
          viene corrupta, el texto del error es lo único que sube; meter el valor
          dentro es publicar el dato malo por la vía que nadie sanea. El campo sí
          va, porque es un literal nuestro, no un dato de la base.
        """
        if type(self.lane) is not str or not self.lane:
            raise OperationInvalid(
                "`OutboxCounts.lane` tiene que ser un str no vacío")
        for campo, v in (("pending", self.pending), ("failed", self.failed)):
            if type(v) is not int:
                raise OperationInvalid(
                    f"`OutboxCounts.{campo}` tiene que ser un int EXACTO; "
                    f"`bool` es subclase de `int` y aquí no cuenta como uno")
            if v < 0:
                raise OperationInvalid(
                    f"`OutboxCounts.{campo}` no puede ser negativo: una cuenta "
                    f"bajo cero no es un dato viejo, es un dato imposible")

    @property
    def unresolved(self) -> int:
        """`pending + failed`: lo que IMPIDE un rollback. Mismo criterio que
        `Journal.unresolved_outbox` —un agotado NO se auto-abandona—, sólo que
        aquí los dos sumandos salen de la MISMA foto."""
        return self.pending + self.failed


@dataclass(frozen=True)
class RollbackStatus:
    """Foto indivisible de las únicas condiciones que permiten volver atrás.

    No transporta la sesión, el principal, credenciales ni paths. El carril se
    deriva de la sesión autenticada y las dos puertas y los dos contadores se
    leen en la misma transacción SQLite.
    """

    lane: str
    admissions: tuple[AdmissionState, ...]
    outbox: OutboxCounts
    durable_v: int

    @property
    def certifiable(self) -> bool:
        return (
            len(self.admissions) == len(ADMISSION_VERBS)
            and tuple(state.verb for state in self.admissions) == ADMISSION_VERBS
            and all(state.lane == self.lane and state.state == "sealed"
                    for state in self.admissions)
            and self.outbox.lane == self.lane
            and self.outbox.pending == 0
            and self.outbox.failed == 0
        )


@dataclass(frozen=True)
class _DatabaseRollbackStatus:
    """Foto interna DB-wide; nunca se expone como autoridad cliente.

    Restaurar sustituye el fichero SQLite entero, de modo que un certificado
    lane-scoped no alcanza. Cada lane que aparece en cualquier tabla durable
    con columna ``lane`` debe consentir explícitamente mediante ambos sellos.
    """

    lanes: tuple[str, ...]
    admissions: tuple[AdmissionState, ...]
    pending: int
    failed: int
    durable_v: int

    @property
    def certifiable(self) -> bool:
        expected = tuple(
            (lane, verb) for lane in self.lanes for verb in ADMISSION_VERBS)
        actual = tuple((state.lane, state.verb) for state in self.admissions)
        return (
            bool(self.lanes)
            and actual == expected
            and all(state.state == "sealed" for state in self.admissions)
            and self.pending == 0
            and self.failed == 0
        )


@dataclass(frozen=True)
class RollbackCertificate:
    """Evidencia acotada de que un carril sellado no conserva trabajo vivo."""

    lane: str
    admissions: tuple[AdmissionState, ...]
    outbox: OutboxCounts
    durable_v: int
    certified_at: str


@dataclass(frozen=True)
class MigrationSnapshot:
    """Referencia citable a una fotografía SQLite pre-v7 de un solo fichero."""

    snapshot_id: str
    path: str
    sha256: str
    source_durable_v: int
    created_at: str


@dataclass(frozen=True)
class RuntimeObservation:
    observation_id: str
    lane: str
    workload_id: str
    runtime_instance: str
    credential_generation: int
    supervisor_seq: int
    status: str
    transition_id: str | None
    receipt_id: str | None
    replayed: bool
    observed_at: str


@dataclass(frozen=True)
class RuntimeRecovery:
    recovery_id: str
    command_id: str
    lane: str
    workload_id: str
    runtime_instance: str
    credential_generation: int
    transition_id: str
    receipt_id: str
    replayed: bool
    accepted_at: str


# ── Esquema ──────────────────────────────────────────────────────────────────
# NADA de tablas STRICT: pide SQLite >= 3.37 y una imagen anterior con SQLite
# viejo no podría ni ABRIR un esquema que las contenga — el rollback dejaría de
# existir. El tipado se gana con CHECK, que sí lee una versión vieja.
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS principals (
  principal_id TEXT PRIMARY KEY,
  principal    TEXT NOT NULL,
  role         TEXT NOT NULL,
  lane         TEXT NOT NULL,
  created_at   TEXT NOT NULL);
-- Dos principals con el MISMO rol tienen que seguir siendo distinguibles
-- (falsador 1): la identidad es `principal_id`, y no hay UNIQUE por rol.
CREATE INDEX IF NOT EXISTS i_prn_role ON principals(role, lane);

-- Ligadura credencial->principal. `credential_ref` es HMAC(pepper, credencial):
-- opaco, no reversible, y el secreto NO entra en la base.
-- El principal se FIJA la primera vez que se acepta la credencial. Añadir un
-- principal explícito después crea una ligadura NUEVA y RETIRA la vieja; jamás
-- reescribe la atribución histórica (ADR-001 §Identity model).
CREATE TABLE IF NOT EXISTS credential_bindings (
  binding_id      TEXT PRIMARY KEY,
  credential_ref  TEXT NOT NULL,
  principal_id    TEXT NOT NULL REFERENCES principals(principal_id),
  principal_source TEXT NOT NULL
      CHECK (principal_source IN ('explicit','derived_from_role')),
  -- CAPACIDADES del despliegue, no del cliente ni del rol en prosa. Un rol es
  -- una etiqueta que se repite entre credenciales; una capacidad la CONCEDE
  -- quien escribe el mapa, y se revalida dentro de la transacción que la usa.
  capabilities TEXT NOT NULL DEFAULT '[]',
  generation      INTEGER NOT NULL,
  bound_at        TEXT NOT NULL,
  retired_at      TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS u_binding_activa
  ON credential_bindings(credential_ref) WHERE retired_at IS NULL;

CREATE TABLE IF NOT EXISTS runtime_sessions (
  runtime_instance TEXT PRIMARY KEY,
  principal_id  TEXT NOT NULL REFERENCES principals(principal_id),
  role          TEXT NOT NULL,
  lane          TEXT NOT NULL,
  token_hash    TEXT NOT NULL UNIQUE,   -- sha256 del token. El token NO se guarda.
  generation    INTEGER NOT NULL,
  issued_at     REAL NOT NULL,
  expires_at    REAL NOT NULL,
  rotated_from  TEXT,
  revoked_at    REAL,
  revoke_reason TEXT,
  annotations   TEXT);                  -- host/pid/imagen: NO participan en identidad

CREATE TABLE IF NOT EXISTS events (
  n            INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id     TEXT NOT NULL UNIQUE,
  lane         TEXT NOT NULL,
  verb         TEXT NOT NULL,
  kind         TEXT,
  head         TEXT NOT NULL,
  body         TEXT NOT NULL,
  recipients   TEXT NOT NULL,          -- LITERALES tal cual: es lo que se RENDERIZA
  -- Roles canónicos ACUSABLES, resueltos por el censo inyectado al ACEPTAR.
  -- Separados de `recipients` a propósito: el render necesita el literal que
  -- escribió el autor y el ACK necesita el rol, y fundirlos deja uno de los dos
  -- mintiendo. `NULL` = fila anterior a v4, escrita sin censo: no se re-deriva
  -- (el censo de entonces no está, y re-normalizar sería inventar atribución).
  recipients_roles TEXT,
  recipients_broadcast TEXT,            -- literales de DIFUSIÓN: nadie acusa por ellos
  intent       TEXT NOT NULL,
  principal_id TEXT NOT NULL REFERENCES principals(principal_id),
  role         TEXT NOT NULL,
  runtime_instance TEXT NOT NULL REFERENCES runtime_sessions(runtime_instance),
  occurred_at  TEXT NOT NULL,
  payload_sha  TEXT NOT NULL,
  attestation  TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS i_ev_lane ON events(lane, n);

-- `entry_eid` NO vive aquí y NO tiene UNIQUE en ninguna parte: es hash de
-- CONTENIDO y hay repeticiones legítimas dentro de un mismo ledger (medido:
-- 23 hashes repetidos intra-ledger). Aparece en el stream del recibo al
-- MATERIALIZAR, nunca como identidad.
CREATE TABLE IF NOT EXISTS receipts (
  receipt_id   TEXT PRIMARY KEY,
  subject_kind TEXT NOT NULL
      CHECK (subject_kind IN (
        'event','command','denial','denial_aggregate','transition')),
  subject_id   TEXT NOT NULL,
  principal_id TEXT REFERENCES principals(principal_id),
  lane         TEXT,
  current_state TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  UNIQUE (subject_kind, subject_id));

-- Append-only: una transición NUNCA sobrescribe a la anterior. `receipts
-- .current_state` es una proyección de conveniencia escrita en la MISMA
-- transacción, no la verdad.
CREATE TABLE IF NOT EXISTS receipt_transitions (
  transition_id TEXT PRIMARY KEY,
  receipt_id    TEXT NOT NULL REFERENCES receipts(receipt_id),
  seq           INTEGER NOT NULL,
  state         TEXT NOT NULL,
  at            TEXT NOT NULL,
  detail        TEXT,
  UNIQUE (receipt_id, seq));

CREATE TABLE IF NOT EXISTS idempotency (
  principal_id TEXT NOT NULL REFERENCES principals(principal_id),
  lane         TEXT NOT NULL,
  verb         TEXT NOT NULL,
  key          TEXT NOT NULL,
  req_hash     TEXT NOT NULL,
  event_id     TEXT NOT NULL REFERENCES events(event_id),
  receipt_id   TEXT NOT NULL REFERENCES receipts(receipt_id),
  -- Versión de la CANONICALIZACIÓN con la que se calculó `req_hash`. Sin ella,
  -- el día que cambie la forma de canonicalizar, un reintento legítimo con el
  -- mismo cuerpo saldría `409` porque el hash nuevo no casa con el viejo.
  req_hash_v   INTEGER NOT NULL DEFAULT 1,
  created_at   TEXT NOT NULL,
  PRIMARY KEY (principal_id, lane, verb, key));

-- El outbox termina en `materialized`, NO en `delivered`. El trabajo del
-- trabajador acaba cuando el append aterrizó; que el índice lo vea y que un
-- destinatario lo consuma son OTROS dos hechos, con otro dueño y otro momento.
-- Llamar `delivered` a esto sería afirmar el efecto de terceros al terminar el
-- propio — el falso verde exacto que `accepted != materialized` existe para
-- evitar, una capa más abajo.
CREATE TABLE IF NOT EXISTS outbox (
  n           INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id    TEXT NOT NULL UNIQUE REFERENCES events(event_id),
  ledger      TEXT NOT NULL,
  state       TEXT NOT NULL DEFAULT 'pending'
      CHECK (state IN ('pending','materialized','failed','abandoned')),
  attempts    INTEGER NOT NULL DEFAULT 0,
  next_attempt REAL NOT NULL,
  lease_until REAL,
  lease_by    TEXT,
  -- Ficha del arriendo VIGENTE. Sin ella, un trabajador cuyo arriendo venció
  -- —pausado, no muerto— vuelve del limbo y marca `materialized` un evento que
  -- ya relevó otro: el mismo defecto que el fencing cierra para los leases,
  -- abierto en el outbox. La ficha cambia en cada `claim`, así que el rezagado
  -- trae una que ya no es.
  claim_token TEXT,
  last_error  TEXT,
  created_at  TEXT NOT NULL,
  -- `materialized_at` y no `delivered_at`: lo que este sello marca es que el
  -- append aterrizó, NO que alguien lo recibiera. El esquema es nuevo, así que
  -- el nombre puede ser el verdadero desde el principio en vez de arrastrar una
  -- mentira cómoda que luego nadie se atreve a renombrar.
  materialized_at TEXT);
CREATE INDEX IF NOT EXISTS i_outbox_ready ON outbox(next_attempt, n) WHERE state='pending';

-- Una fila por (CARRIL, recurso). El carril va en la CLAVE, no de adorno: con
-- `resource` como PK, dos carriles que llamen igual a su recurso —`migracion`,
-- `deploy`— se pisan el lease y el fencing de uno vale en el otro. El
-- aislamiento por carril es la frontera del producto: no puede depender de que
-- nadie repita un nombre.
-- `fencing_token` es monótono y NO baja al soltar: un contador que se reinicia
-- con el dueño no es una valla.
-- `resource` va NORMALIZADO (`Grammar.normalize_resource`, la misma de
-- `/claim`): es la clave con la que dos trabajos CHOCAN. `resource_literal`
-- guarda lo que escribió el humano — normalizar para chocar no es perder el
-- nombre que alguien va a leer en un informe.
CREATE TABLE IF NOT EXISTS leases (
  lane         TEXT NOT NULL,
  resource     TEXT NOT NULL,
  resource_literal TEXT,
  principal_id TEXT NOT NULL REFERENCES principals(principal_id),
  runtime_instance TEXT NOT NULL REFERENCES runtime_sessions(runtime_instance),
  fencing_token INTEGER NOT NULL,
  acquired_at  REAL NOT NULL,
  expires_at   REAL NOT NULL,
  released_at  REAL,
  PRIMARY KEY (lane, resource));

CREATE TABLE IF NOT EXISTS commands (
  command_id   TEXT PRIMARY KEY,
  workstream_id TEXT NOT NULL,
  revision     INTEGER NOT NULL,
  lane         TEXT NOT NULL,
  principal_id TEXT NOT NULL REFERENCES principals(principal_id),
  -- QUIÉN lo mandó, derivado de la sesión. En columnas y no sólo en el `detail`
  -- del recibo: el `detail` lo compone el llamante en otras rutas, y un actor
  -- que puede escribir el llamante no es una atribución, es una declaración.
  role         TEXT NOT NULL,
  -- NULLABLE **sólo** para el legado. Una fila escrita antes de que existiera la
  -- atribución no tiene runtime, y no me lo invento: rellenar con la sesión de
  -- quien migra convertiría «no se sabe» en una firma falsa, que es peor que el
  -- hueco porque ya no se distingue de un dato bueno.
  runtime_instance TEXT REFERENCES runtime_sessions(runtime_instance),
  attribution_status TEXT NOT NULL DEFAULT 'verified'
      CHECK (attribution_status IN ('verified','legacy_unattributed')),
  payload      TEXT NOT NULL,
  state        TEXT NOT NULL
      CHECK (state IN ('accepted','received','executing','succeeded',
                       'failed','cancelled','superseded')),
  supersedes   TEXT REFERENCES commands(command_id),
  created_at   TEXT NOT NULL,
  -- El CARRIL entra en la unicidad por el mismo motivo que en `leases`: dos
  -- carriles pueden tener un `workstream_id` con el mismo nombre y la revisión
  -- de uno NO puede supersedir la del otro.
  UNIQUE (lane, workstream_id, revision),
  -- El enum SOLO no impide la combinación mentirosa: `verified` con el runtime
  -- a NULL diría «comprobado» sobre un hueco, y `legacy_unattributed` con
  -- runtime diría «no se sabe» sobre un dato que sí está. El invariante es el
  -- PAR, y va como restricción de TABLA porque relaciona dos columnas.
  CHECK ((attribution_status = 'verified' AND runtime_instance IS NOT NULL)
      OR (attribution_status = 'legacy_unattributed'
          AND runtime_instance IS NULL)));
CREATE INDEX IF NOT EXISTS i_cmd_ws ON commands(lane, workstream_id, revision);

-- CAUSALIDAD NATIVA: con FK de verdad, en tablas separadas por tipo. Una FK no
-- puede ser condicional, así que un solo `causes` polimórfico sería una FK
-- decorativa — y una FK decorativa es peor que ninguna: parece que valida.
CREATE TABLE IF NOT EXISTS event_causes (
  event_id       TEXT NOT NULL REFERENCES events(event_id),
  cause_event_id TEXT NOT NULL REFERENCES events(event_id),
  PRIMARY KEY (event_id, cause_event_id));

-- Un comando puede citar como causa un COMANDO o un EVENTO nativo (ADR-001
-- §Commands: «reference native event or command identifiers and use foreign
-- keys»). Son DOS tablas y no una con `cause_kind`, porque una FK no puede ser
-- condicional: una sola tabla polimórfica dejaría la mitad de las causas sin
-- integridad referencial mientras aparenta tenerla.
CREATE TABLE IF NOT EXISTS command_causes (
  command_id       TEXT NOT NULL REFERENCES commands(command_id),
  cause_command_id TEXT NOT NULL REFERENCES commands(command_id),
  PRIMARY KEY (command_id, cause_command_id));

CREATE TABLE IF NOT EXISTS command_event_causes (
  command_id     TEXT NOT NULL REFERENCES commands(command_id),
  cause_event_id TEXT NOT NULL REFERENCES events(event_id),
  PRIMARY KEY (command_id, cause_event_id));

-- CAUSALIDAD EXTERNA: material bridge/legacy. TIPADA, sin FK hacia el journal
-- y explícitamente NO autoritativa. Es la vía por la que un `entry_eid` puede
-- citarse sin que nadie lo confunda con identidad de evento.
CREATE TABLE IF NOT EXISTS external_causes (
  child_kind TEXT NOT NULL CHECK (child_kind IN ('event','command')),
  child_id   TEXT NOT NULL,
  ledger     TEXT NOT NULL,
  entry_eid  TEXT NOT NULL,
  authority  TEXT NOT NULL DEFAULT 'false' CHECK (authority = 'false'),
  noted_at   TEXT NOT NULL,
  PRIMARY KEY (child_kind, child_id, ledger, entry_eid));

-- RECHAZOS DE IDENTIDAD CONOCIDA: atribuibles a (principal, carril, runtime)
-- y con `reason_code` de un VOCABULARIO CERRADO. El motivo no puede llevar el
-- dato que lo provocó —clave, recurso, id— porque entonces cada rechazo es un
-- valor distinto y la agregación deja de agregar: la cota por cubo se vuelve
-- decorativa y el atacante elige cuántas filas durables escribe.
CREATE TABLE IF NOT EXISTS denials (
  denial_id    TEXT PRIMARY KEY,
  principal_id TEXT NOT NULL REFERENCES principals(principal_id),
  -- NOT NULL con centinela '': en SQLite varios NULL NO chocan en una clave, así
  -- que dejarlos nulos convierte la cuota por (principal,carril,runtime) en una
  -- cuota por principal a secas, y la atribución se mezcla justo donde importa.
  lane         TEXT NOT NULL DEFAULT '',
  runtime_instance TEXT NOT NULL DEFAULT '',
  reason       TEXT NOT NULL,
  bucket       TEXT NOT NULL,
  receipt_id   TEXT NOT NULL REFERENCES receipts(receipt_id),
  at           TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS i_denials ON denials(principal_id, reason, bucket);

-- Por encima de la cuota: UNA fila por (principal, motivo, cubo) y un contador
-- en sitio. Una tormenta de rechazos no puede forzar una fila durable por
-- petición (ADR-001 §Native event contract).
-- SIN `runtime_instance` EN LA CLAVE, y es la corrección de seguridad de este
-- correctivo: el agregado se cuenta por (principal, carril, motivo, cubo). Con
-- el runtime dentro, abrir una sesión nueva estrenaba cubo y la cuota se
-- evadía abriendo sesiones, que no cuestan nada. El recibo del agregado
-- atribuye principal y carril: el runtime concreto vive en las filas
-- individuales, que es donde sirve para investigar y no para eludir.
CREATE TABLE IF NOT EXISTS denial_aggregates (
  principal_id TEXT NOT NULL REFERENCES principals(principal_id),
  lane         TEXT NOT NULL DEFAULT '',
  reason       TEXT NOT NULL,
  bucket       TEXT NOT NULL,
  receipt_id   TEXT NOT NULL REFERENCES receipts(receipt_id),
  suppressed   INTEGER NOT NULL DEFAULT 0,
  first_at     TEXT NOT NULL,
  last_at      TEXT NOT NULL,
  PRIMARY KEY (principal_id, lane, reason, bucket));

-- Credencial DESCONOCIDA: no puede crear filas de coordinación. Contador
-- acotado por número de filas; al llenarse, todo cae en una fila de desborde.
-- OPERACIONES MANUALES sobre el outbox. Un item envenenado NO se abandona
-- solo: se agota, se queda `failed`, y sigue contando como SIN RESOLVER para
-- que el rollback no pueda decir que la cola está limpia. Sacarlo de ahí es un
-- ACTO DE UN OPERADOR, con su nombre y su motivo, y queda escrito.
-- UN ACK POR DESTINATARIO. `delivered` es un hecho POR RECEPTOR, no un
-- interruptor del evento: con un solo sello global, el primero que acusa cierra
-- la entrega de todos los demás y los que faltan quedan marcados como servidos
-- sin haber recibido nada.
CREATE TABLE IF NOT EXISTS event_acks (
  event_id   TEXT NOT NULL REFERENCES events(event_id),
  recipient  TEXT NOT NULL,          -- rol DERIVADO de la sesión que acusa
  principal_id TEXT NOT NULL REFERENCES principals(principal_id),
  runtime_instance TEXT NOT NULL REFERENCES runtime_sessions(runtime_instance),
  ack_ref    TEXT,
  at         TEXT NOT NULL,
  PRIMARY KEY (event_id, recipient));

CREATE TABLE IF NOT EXISTS outbox_operations (
  operation_id TEXT PRIMARY KEY,
  event_id   TEXT NOT NULL REFERENCES events(event_id),
  operation  TEXT NOT NULL CHECK (operation IN ('requeue','abandon')),
  operator   TEXT NOT NULL REFERENCES principals(principal_id),
  lane       TEXT NOT NULL,
  runtime_instance TEXT NOT NULL REFERENCES runtime_sessions(runtime_instance),
  reason     TEXT NOT NULL,
  from_state TEXT NOT NULL,
  at         TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS i_outbox_ops ON outbox_operations(event_id, at);

CREATE TABLE IF NOT EXISTS unknown_credentials (
  fingerprint TEXT PRIMARY KEY,
  count       INTEGER NOT NULL DEFAULT 0,
  first_at    TEXT NOT NULL,
  last_at     TEXT NOT NULL);

-- BARRERA DE ADMISION. Historial APPEND-ONLY por (carril, verbo): una fila por
-- transicion y NUNCA un UPDATE, igual que `receipt_transitions`. El estado
-- vigente es el de la fila con `epoch` maximo, y LA AUSENCIA DE FILA ES
-- `closed`: la puerta que nadie abrio no esta abierta.
--
-- Sin `UNIQUE (lane, verb)` con un estado mutable a proposito: sobrescribiendo,
-- el historial de quien cerro y cuando desaparece justo cuando hace falta, y
-- dos operadores concurrentes no tienen nada contra lo que fallar.
--
-- `epoch` es monotono por par y hace de VALLA. `seal` compara el epoch que el
-- operador leyo contra el que hay DENTRO de su transaccion, asi que un sello
-- apoyado en una lectura vieja pierde en vez de firmar sobre otro estado. Es la
-- misma disciplina que `leases.fencing_token`, aplicada a la puerta.
--
-- CERO TEXTO LIBRE: `reason_code` es vocabulario cerrado (`ADMISSION_REASON_CODES`).
--
-- `origin` separa lo que un operador FIRMO de lo que escribio la MIGRACION. Una
-- fila de migracion no tiene operador ni sesion; darle uno seria fabricar una
-- firma, y dejar los campos nulos sin declarar el origen seria no poder
-- distinguir una firma ausente de una firma perdida. El CHECK cierra las dos
-- mentiras simetricas a la vez.
CREATE TABLE IF NOT EXISTS admission_history (
  lane        TEXT NOT NULL,
  verb        TEXT NOT NULL CHECK (verb IN ('events.accept','outbox.requeue')),
  epoch       INTEGER NOT NULL,
  state       TEXT NOT NULL CHECK (state IN ('open','closed','sealed')),
  origin      TEXT NOT NULL CHECK (origin IN ('operator','migration')),
  operator    TEXT REFERENCES principals(principal_id),
  runtime_instance TEXT REFERENCES runtime_sessions(runtime_instance),
  reason_code TEXT NOT NULL CHECK (reason_code IN (
      'ROLLOUT','INCIDENT','MAINTENANCE','DRAIN_FOR_ROLLBACK','SCHEMA_MIGRATION')),
  at          TEXT NOT NULL,
  CHECK ((origin='operator' AND operator IS NOT NULL AND runtime_instance IS NOT NULL)
      OR (origin='migration' AND operator IS NULL AND runtime_instance IS NULL)),
  -- El motivo reservado no puede prestar apariencia de migracion a una firma
  -- de operador, ni una migracion usar motivos que implican una decision
  -- humana. Es un bicondicional, no solo una restriccion sobre un lado.
  CHECK ((origin='migration' AND reason_code='SCHEMA_MIGRATION')
      OR (origin='operator' AND reason_code<>'SCHEMA_MIGRATION')),
  -- DEFENSA EN PROFUNDIDAD. La migracion SOLO puede escribir `closed`: es lo
  -- unico que sabe: que ese par tuvo historia bajo un esquema sin barrera. Una
  -- fila `origin='migration'` en cualquier otro estado no es una migracion, es
  -- una apertura sin operador que se disfraza de una. La cura de verdad es que
  -- la migracion NO ADOPTE una tabla que no creo ella: ver `_m5_a_6`. Este
  -- CHECK existe porque esa cura vive en codigo y esta vive en la base.
  CHECK (origin <> 'migration' OR state = 'closed'),
  PRIMARY KEY (lane, verb, epoch));

-- v7 · CONTROL PLANE DE FLOTA. Los índices compuestos son padres explícitos
-- de FKs lane-locales; no descansamos en que los ids globales "suelen" ser
-- únicos para afirmar aislamiento.
CREATE UNIQUE INDEX IF NOT EXISTS u_principal_lane
  ON principals(lane, principal_id);
CREATE UNIQUE INDEX IF NOT EXISTS u_runtime_lane
  ON runtime_sessions(lane, runtime_instance);
CREATE UNIQUE INDEX IF NOT EXISTS u_runtime_lane_generation
  ON runtime_sessions(lane, runtime_instance, generation);
CREATE UNIQUE INDEX IF NOT EXISTS u_runtime_lane_principal
  ON runtime_sessions(lane, principal_id, runtime_instance);
CREATE UNIQUE INDEX IF NOT EXISTS u_runtime_lane_principal_generation
  ON runtime_sessions(lane, principal_id, runtime_instance, generation);
CREATE UNIQUE INDEX IF NOT EXISTS u_command_lane
  ON commands(lane, command_id);

CREATE TABLE IF NOT EXISTS organization_revisions (
  lane              TEXT NOT NULL,
  revision          INTEGER NOT NULL CHECK (revision > 0),
  source_sha256     TEXT NOT NULL CHECK (
      length(source_sha256)=64 AND source_sha256 NOT GLOB '*[^0-9a-f]*'),
  attestation_state TEXT NOT NULL CHECK (
      attestation_state IN ('attested','stale','unattested')),
  active            INTEGER NOT NULL CHECK (active IN (0,1)),
  activated_by      TEXT NOT NULL,
  activated_runtime TEXT NOT NULL,
  activated_at      TEXT NOT NULL,
  PRIMARY KEY (lane, revision),
  FOREIGN KEY (lane, activated_by)
    REFERENCES principals(lane, principal_id),
  FOREIGN KEY (lane, activated_runtime)
    REFERENCES runtime_sessions(lane, runtime_instance),
  FOREIGN KEY (lane, activated_by, activated_runtime)
    REFERENCES runtime_sessions(lane, principal_id, runtime_instance));
CREATE UNIQUE INDEX IF NOT EXISTS u_org_active_lane
  ON organization_revisions(lane) WHERE active=1;

CREATE TABLE IF NOT EXISTS organization_roles (
  lane        TEXT NOT NULL,
  revision    INTEGER NOT NULL,
  role        TEXT NOT NULL,
  layer       INTEGER NOT NULL CHECK (layer >= 0 AND layer <= 255),
  policy_code TEXT CHECK (policy_code IN (
      'STANDARD','REVIEW_REQUIRED','HUMAN_APPROVAL_REQUIRED')),
  PRIMARY KEY (lane, revision, role),
  FOREIGN KEY (lane, revision)
    REFERENCES organization_revisions(lane, revision));

CREATE TABLE IF NOT EXISTS organization_reports (
  lane        TEXT NOT NULL,
  revision    INTEGER NOT NULL,
  role        TEXT NOT NULL,
  reports_to  TEXT NOT NULL,
  PRIMARY KEY (lane, revision, role),
  CHECK (role <> reports_to),
  FOREIGN KEY (lane, revision, role)
    REFERENCES organization_roles(lane, revision, role),
  FOREIGN KEY (lane, revision, reports_to)
    REFERENCES organization_roles(lane, revision, role));

CREATE TABLE IF NOT EXISTS organization_reviewers (
  lane          TEXT NOT NULL,
  revision      INTEGER NOT NULL,
  role          TEXT NOT NULL,
  reviewer_role TEXT NOT NULL,
  PRIMARY KEY (lane, revision, role, reviewer_role),
  CHECK (role <> reviewer_role),
  FOREIGN KEY (lane, revision, role)
    REFERENCES organization_roles(lane, revision, role),
  FOREIGN KEY (lane, revision, reviewer_role)
    REFERENCES organization_roles(lane, revision, role));

CREATE TABLE IF NOT EXISTS organization_escalations (
  lane         TEXT NOT NULL,
  revision     INTEGER NOT NULL,
  role         TEXT NOT NULL,
  trigger_code TEXT NOT NULL CHECK (trigger_code IN (
      'BLOCKED','REVIEW_REQUIRED','INCIDENT','RECOVERY_FAILED')),
  target_role  TEXT NOT NULL,
  PRIMARY KEY (lane, revision, role, trigger_code, target_role),
  CHECK (role <> target_role),
  FOREIGN KEY (lane, revision, role)
    REFERENCES organization_roles(lane, revision, role),
  FOREIGN KEY (lane, revision, target_role)
    REFERENCES organization_roles(lane, revision, role));

CREATE TABLE IF NOT EXISTS expected_workloads (
  lane                  TEXT NOT NULL,
  organization_revision INTEGER NOT NULL,
  workload_id           TEXT NOT NULL,
  role                  TEXT NOT NULL,
  principal_id          TEXT,
  runtime_instance      TEXT,
  credential_generation INTEGER,
  PRIMARY KEY (lane, organization_revision, workload_id),
  CHECK ((runtime_instance IS NULL AND credential_generation IS NULL)
      OR (runtime_instance IS NOT NULL AND credential_generation IS NOT NULL)),
  CHECK (runtime_instance IS NULL OR principal_id IS NOT NULL),
  FOREIGN KEY (lane, organization_revision, role)
    REFERENCES organization_roles(lane, revision, role),
  FOREIGN KEY (lane, principal_id)
    REFERENCES principals(lane, principal_id),
  FOREIGN KEY (lane, runtime_instance, credential_generation)
    REFERENCES runtime_sessions(lane, runtime_instance, generation),
  FOREIGN KEY (lane, principal_id, runtime_instance, credential_generation)
    REFERENCES runtime_sessions(
      lane, principal_id, runtime_instance, generation));
CREATE UNIQUE INDEX IF NOT EXISTS u_expected_runtime
  ON expected_workloads(lane, organization_revision, runtime_instance)
  WHERE runtime_instance IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS u_expected_target
  ON expected_workloads(lane, organization_revision, workload_id,
                        runtime_instance, credential_generation);
CREATE UNIQUE INDEX IF NOT EXISTS u_expected_binding
  ON expected_workloads(lane, organization_revision, workload_id, principal_id,
                        runtime_instance, credential_generation);

-- Una recovery NO tiene estado propio: liga idempotencia y fencing a UN
-- `commands.command_id`; la máquina existente de commands sigue siendo la
-- única autoridad de ciclo de vida.
CREATE TABLE IF NOT EXISTS runtime_recoveries (
  recovery_id          TEXT PRIMARY KEY,
  lane                 TEXT NOT NULL,
  organization_revision INTEGER NOT NULL,
  workload_id          TEXT NOT NULL,
  target_runtime_instance TEXT NOT NULL,
  target_generation    INTEGER NOT NULL,
  requester_principal  TEXT NOT NULL,
  requester_runtime    TEXT NOT NULL,
  requester_generation INTEGER NOT NULL,
  verb                 TEXT NOT NULL CHECK (verb='runtime.recover'),
  idempotency_key      TEXT NOT NULL,
  req_hash             TEXT NOT NULL CHECK (
      length(req_hash)=64 AND req_hash NOT GLOB '*[^0-9a-f]*'),
  reason_code          TEXT NOT NULL CHECK (reason_code IN (
      'OPERATOR_REQUESTED','STALE_RUNTIME','DEGRADED_RUNTIME','STOPPED_RUNTIME')),
  action_code          TEXT NOT NULL CHECK (action_code='RESTART_RUNTIME'),
  fenced_resource      TEXT NOT NULL,
  fencing_token        INTEGER NOT NULL CHECK (fencing_token > 0),
  command_id           TEXT NOT NULL UNIQUE REFERENCES commands(command_id),
  accepted_at          TEXT NOT NULL,
  UNIQUE (requester_principal, lane, verb, idempotency_key),
  FOREIGN KEY (lane, organization_revision, workload_id)
    REFERENCES expected_workloads(lane, organization_revision, workload_id),
  FOREIGN KEY (lane, target_runtime_instance, target_generation)
    REFERENCES runtime_sessions(lane, runtime_instance, generation),
  FOREIGN KEY (lane, requester_principal)
    REFERENCES principals(lane, principal_id),
  FOREIGN KEY (lane, requester_runtime, requester_generation)
    REFERENCES runtime_sessions(lane, runtime_instance, generation),
  -- Las tres asociaciones de recovery quedan acreditadas con su lane y su
  -- identidad completas; los ids simples no son una frontera de aislamiento.
  FOREIGN KEY (lane, organization_revision, workload_id,
               target_runtime_instance, target_generation)
    REFERENCES expected_workloads(
      lane, organization_revision, workload_id,
      runtime_instance, credential_generation),
  FOREIGN KEY (lane, requester_principal, requester_runtime,
               requester_generation)
    REFERENCES runtime_sessions(
      lane, principal_id, runtime_instance, generation),
  FOREIGN KEY (lane, command_id)
    REFERENCES commands(lane, command_id));

CREATE TABLE IF NOT EXISTS runtime_observations (
  observation_id       TEXT PRIMARY KEY,
  lane                 TEXT NOT NULL,
  organization_revision INTEGER NOT NULL,
  workload_id          TEXT NOT NULL,
  target_runtime_instance TEXT NOT NULL,
  target_generation    INTEGER NOT NULL,
  observer_principal   TEXT NOT NULL,
  observer_runtime     TEXT NOT NULL,
  observer_generation  INTEGER NOT NULL,
  verb                 TEXT NOT NULL CHECK (verb='runtime.observe'),
  idempotency_key      TEXT NOT NULL,
  req_hash             TEXT NOT NULL CHECK (
      length(req_hash)=64 AND req_hash NOT GLOB '*[^0-9a-f]*'),
  supervisor_seq       INTEGER NOT NULL CHECK (
      supervisor_seq > 0 AND supervisor_seq <= 9223372036854775807),
  observation_kind     TEXT NOT NULL CHECK (observation_kind IN (
      'cycle_ack','started','exited','resource_degraded','resource_recovered',
      'recovery_succeeded','recovery_failed')),
  reason_code          TEXT NOT NULL CHECK (reason_code IN (
      'PROCESS_PRESENT','PROCESS_STARTED','PROCESS_EXITED','CPU_SATURATED',
      'MEMORY_SATURATED','IO_STALLED','HEARTBEAT_RECOVERED',
      'RECOVERY_SUCCEEDED','RECOVERY_FAILED')),
  detector_state       TEXT CHECK (detector_state IN (
      'viva-con-progreso','viva-sin-obligacion','atascada','en-bucle','muda',
      'sin-armar','inarmable','ilegible','indeterminado','sensor-mudo')),
  cpu_millis           INTEGER CHECK (
      cpu_millis IS NULL OR (cpu_millis >= 0 AND cpu_millis <= 1000000)),
  rss_bytes            INTEGER CHECK (
      rss_bytes IS NULL OR (rss_bytes >= 0 AND rss_bytes <= 1125899906842624)),
  heartbeat_age_ms     INTEGER CHECK (
      heartbeat_age_ms IS NULL OR
      (heartbeat_age_ms >= 0 AND heartbeat_age_ms <= 2678400000)),
  exit_code            INTEGER CHECK (
      exit_code IS NULL OR (exit_code >= -2147483648 AND exit_code <= 2147483647)),
  recovery_command_id  TEXT REFERENCES commands(command_id),
  observed_at          TEXT NOT NULL,
  CHECK ((observation_kind IN ('recovery_succeeded','recovery_failed')
          AND recovery_command_id IS NOT NULL)
      OR (observation_kind NOT IN ('recovery_succeeded','recovery_failed')
          AND recovery_command_id IS NULL)),
  CHECK (observer_runtime <> target_runtime_instance),
  CHECK ((observation_kind='cycle_ack' AND reason_code='PROCESS_PRESENT')
      OR (observation_kind='started' AND reason_code='PROCESS_STARTED')
      OR (observation_kind='exited' AND reason_code='PROCESS_EXITED')
      OR (observation_kind='resource_degraded' AND reason_code IN
          ('CPU_SATURATED','MEMORY_SATURATED','IO_STALLED'))
      OR (observation_kind='resource_recovered'
          AND reason_code='HEARTBEAT_RECOVERED')
      OR (observation_kind='recovery_succeeded'
          AND reason_code='RECOVERY_SUCCEEDED')
      OR (observation_kind='recovery_failed'
          AND reason_code='RECOVERY_FAILED')),
  UNIQUE (observer_principal, lane, verb, idempotency_key),
  UNIQUE (observer_principal, observer_runtime, observer_generation,
          lane, target_runtime_instance, target_generation, supervisor_seq),
  FOREIGN KEY (lane, organization_revision, workload_id)
    REFERENCES expected_workloads(lane, organization_revision, workload_id),
  FOREIGN KEY (lane, target_runtime_instance, target_generation)
    REFERENCES runtime_sessions(lane, runtime_instance, generation),
  FOREIGN KEY (lane, observer_principal)
    REFERENCES principals(lane, principal_id),
  FOREIGN KEY (lane, observer_runtime, observer_generation)
    REFERENCES runtime_sessions(lane, runtime_instance, generation),
  FOREIGN KEY (lane, organization_revision, workload_id,
               target_runtime_instance, target_generation)
    REFERENCES expected_workloads(
      lane, organization_revision, workload_id,
      runtime_instance, credential_generation),
  FOREIGN KEY (lane, observer_principal, observer_runtime,
               observer_generation)
    REFERENCES runtime_sessions(
      lane, principal_id, runtime_instance, generation),
  FOREIGN KEY (lane, recovery_command_id)
    REFERENCES commands(lane, command_id));
CREATE INDEX IF NOT EXISTS i_runtime_observation_target
  ON runtime_observations(lane, workload_id, target_runtime_instance,
                          target_generation, supervisor_seq);

CREATE TABLE IF NOT EXISTS runtime_status_transitions (
  transition_id        TEXT PRIMARY KEY,
  lane                 TEXT NOT NULL,
  organization_revision INTEGER NOT NULL,
  workload_id          TEXT NOT NULL,
  target_runtime_instance TEXT,
  target_generation    INTEGER,
  from_status          TEXT CHECK (from_status IS NULL OR from_status IN (
      'absent','fresh','stale','degraded','stopped','recovering')),
  to_status            TEXT NOT NULL CHECK (to_status IN (
      'absent','fresh','stale','degraded','stopped','recovering')),
  detector_state       TEXT CHECK (detector_state IN (
      'viva-con-progreso','viva-sin-obligacion','atascada','en-bucle','muda',
      'sin-armar','inarmable','ilegible','indeterminado','sensor-mudo')),
  status_seq           INTEGER NOT NULL CHECK (
      status_seq > 0 AND status_seq <= 9223372036854775807),
  cause_kind           TEXT NOT NULL CHECK (cause_kind IN (
      'organization','observation','deadline','recovery')),
  cause_id             TEXT NOT NULL,
  reason_code          TEXT NOT NULL CHECK (reason_code IN (
      'PROCESS_PRESENT','PROCESS_STARTED','PROCESS_EXITED','CPU_SATURATED',
      'MEMORY_SATURATED','IO_STALLED','HEARTBEAT_RECOVERED',
      'RECOVERY_SUCCEEDED','RECOVERY_FAILED','DEADLINE_EXCEEDED',
      'RECOVERY_REQUESTED','ORGANIZATION_ACTIVATED')),
  observation_id       TEXT REFERENCES runtime_observations(observation_id),
  recovery_command_id  TEXT REFERENCES commands(command_id),
  receipt_id           TEXT NOT NULL UNIQUE REFERENCES receipts(receipt_id),
  at                   TEXT NOT NULL,
  CHECK ((target_runtime_instance IS NULL AND target_generation IS NULL)
      OR (target_runtime_instance IS NOT NULL AND target_generation IS NOT NULL)),
  CHECK ((cause_kind='observation' AND observation_id IS NOT NULL)
      OR (cause_kind='recovery' AND observation_id IS NULL
          AND recovery_command_id IS NOT NULL)
      OR (cause_kind IN ('organization','deadline')
          AND observation_id IS NULL AND recovery_command_id IS NULL)),
  UNIQUE (lane, workload_id, status_seq),
  FOREIGN KEY (lane, organization_revision, workload_id)
    REFERENCES expected_workloads(lane, organization_revision, workload_id),
  FOREIGN KEY (lane, target_runtime_instance, target_generation)
    REFERENCES runtime_sessions(lane, runtime_instance, generation),
  FOREIGN KEY (lane, organization_revision, workload_id,
               target_runtime_instance, target_generation)
    REFERENCES expected_workloads(
      lane, organization_revision, workload_id,
      runtime_instance, credential_generation),
  FOREIGN KEY (lane, recovery_command_id)
    REFERENCES commands(lane, command_id));

CREATE TABLE IF NOT EXISTS runtime_status (
  lane                 TEXT NOT NULL,
  workload_id          TEXT NOT NULL,
  organization_revision INTEGER NOT NULL,
  principal_id         TEXT,
  role                 TEXT NOT NULL,
  runtime_instance     TEXT,
  credential_generation INTEGER,
  status               TEXT NOT NULL CHECK (status IN (
      'absent','fresh','stale','degraded','stopped','recovering')),
  detector_state       TEXT CHECK (detector_state IN (
      'viva-con-progreso','viva-sin-obligacion','atascada','en-bucle','muda',
      'sin-armar','inarmable','ilegible','indeterminado','sensor-mudo')),
  status_seq           INTEGER NOT NULL CHECK (
      status_seq > 0 AND status_seq <= 9223372036854775807),
  status_since         TEXT NOT NULL,
  last_observed_at     TEXT,
  cause_id             TEXT NOT NULL,
  transition_id        TEXT NOT NULL UNIQUE
      REFERENCES runtime_status_transitions(transition_id),
  receipt_id           TEXT NOT NULL UNIQUE REFERENCES receipts(receipt_id),
  PRIMARY KEY (lane, workload_id),
  CHECK ((runtime_instance IS NULL AND credential_generation IS NULL)
      OR (runtime_instance IS NOT NULL AND credential_generation IS NOT NULL)),
  CHECK (status='absent' OR
      (runtime_instance IS NOT NULL AND principal_id IS NOT NULL)),
  FOREIGN KEY (lane, organization_revision, workload_id)
    REFERENCES expected_workloads(lane, organization_revision, workload_id),
  FOREIGN KEY (lane, principal_id)
    REFERENCES principals(lane, principal_id),
  FOREIGN KEY (lane, runtime_instance, credential_generation)
    REFERENCES runtime_sessions(lane, runtime_instance, generation),
  FOREIGN KEY (lane, organization_revision, workload_id, principal_id,
               runtime_instance, credential_generation)
    REFERENCES expected_workloads(
      lane, organization_revision, workload_id, principal_id,
      runtime_instance, credential_generation));
"""


def _hay_contenido(con: sqlite3.Connection) -> bool:
    """¿Este fichero ya es la base de alguien?

    Se pregunta ANTES de aplicar ningún PRAGMA de escritura. Una tabla que no sea
    `meta` —con filas o sin ellas— significa que aquí vive un esquema que no
    hemos puesto nosotros, y a eso no se le cambia el `journal_mode` para luego
    anunciar que no se ha tocado.
    """
    try:
        fila = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            "   AND name NOT LIKE 'sqlite_%' AND name<>'meta' LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return True                 # no se deja ni mirar: se asume ocupada
    return fila is not None


# ══ PREFLIGHT ═══════════════════════════════════════════════════════════════
# NADA de SQLite sobre el fichero ORIGINAL antes de clasificarlo.
#
# El motivo no es prudencia genérica: abrir con SQLite un fichero que aún no
# sabemos qué es YA lo modifica. Aplica el `journal_mode`, recupera un WAL,
# reproduce un hot journal — todo eso son escrituras sobre la base de alguien
# hechas para poder decir después que no la tocamos. La clasificación se hace
# sobre una COPIA, y el original no se abre hasta saber que es nuestro.

SUFIJOS = ("", "-wal", "-shm", "-journal", "-mj")
# `-shm` NO se copia: es memoria compartida reconstruible, y copiarla arrastra el
# estado de otro proceso a un contexto donde ya no significa lo mismo. SQLite lo
# regenera al recuperar el WAL.
COPIABLES = ("", "-wal", "-journal")

PREFLIGHT_INTENTOS = 3
PREFLIGHT_SEGUNDOS = 2.0


class SchemaMismatch(JournalError):
    """La base no está en la versión que este código exige para MUTAR.

    Distinta de `SchemaTooNew`: aquí el sello falta o es MENOR. Ninguna de las
    dos autoriza una mutación, pero se separan porque piden cosas distintas —
    una futura se deja en paz, una menor se migra con `initialize()`.
    """


class LifecycleConflict(JournalError):
    """Se pidió el ciclo de vida teniendo una operación viva en la misma hebra."""


class PreflightRejected(JournalError):
    """El fichero no admite ni ser inspeccionado: symlink, no regular o vacío."""


class PreflightUnstable(JournalError):
    """Cambió mientras lo copiábamos. Sin foto quieta no hay veredicto.

    No se reintenta indefinidamente: un escritor activo es un estado legítimo
    del mundo, y la respuesta correcta es decirlo, no ganarle la carrera.
    """


class SchemaCorrupt(JournalError):
    """`quick_check` o `foreign_key_check` fallan sobre la copia recuperada."""


class JournalNotInitialized(JournalError):
    """Se pidió algo del journal antes de clasificarlo o crearlo.

    Fail-closed y SIN artefactos: contestar «no hay sello» abriendo la base para
    averiguarlo es justo lo que no se puede hacer, porque abrirla la crea o la
    modifica. Quien pregunta antes de `initialize()` recibe esto.
    """


class IdentityChanged(JournalError):
    """El fichero de la ruta ya no es el mismo objeto (dev/inode) que latimos."""


def _stat_seguro(ruta: str):
    """`lstat` + rechazo de lo que no se puede inspeccionar con garantías."""
    try:
        st = os.lstat(ruta)
    except FileNotFoundError:
        return None
    if stat_mod.S_ISLNK(st.st_mode):
        raise PreflightRejected(
            f"`{os.path.basename(ruta)}` es un symlink: seguirlo permitiría que "
            f"alguien nos apunte a un fichero que no es el que inspeccionamos")
    if not stat_mod.S_ISREG(st.st_mode):
        raise PreflightRejected(
            f"`{os.path.basename(ruta)}` no es un fichero regular")
    return st


def _huella_fd(ruta: str) -> tuple:
    """(metadatos, sha256) leídos del MISMO descriptor, sin seguir symlinks.

    `O_NOFOLLOW` y `fstat` sobre el fd abierto: hacer `lstat` y luego `open` deja
    entre medias una ventana para cambiar el objeto al que apunta el nombre.
    """
    fd = os.open(ruta, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        h = hashlib.sha256()
        while True:
            trozo = os.read(fd, 1 << 20)
            if not trozo:
                break
            h.update(trozo)
        return ((st.st_dev, st.st_ino, st.st_mode, st.st_size,
                 st.st_mtime_ns, st.st_ctime_ns), h.hexdigest())
    finally:
        os.close(fd)


def _sufijos_presentes(base: str) -> tuple:
    """Los fijos MÁS los super-journals, cuyo nombre es DINÁMICO.

    `-mj` como literal no existe: SQLite llama al master journal
    `<base>-mjHHHHHHHH` con ocho hex aleatorios. Buscando la cadena exacta, el
    fichero que coordina un commit multi-base era INVISIBLE para el inventario
    — podía aparecer y desaparecer entre las dos lecturas y la foto salía
    «estable». El sidecar que menos se mira es el que más pesa: es el que dice
    que hay una transacción a medias.
    """
    dinamicos = []
    carpeta = os.path.dirname(base) or "."
    prefijo = os.path.basename(base) + "-mj"
    try:
        for nombre in os.listdir(carpeta):
            if nombre.startswith(prefijo) and nombre != prefijo:
                dinamicos.append(nombre[len(os.path.basename(base)):])
    except OSError as e:
        # FALLA CERRADO. Tragarse el error decía «no hay super-journals» cuando
        # lo cierto es «no he podido mirar», y son cosas distintas: la primera
        # autoriza a seguir, la segunda no. Un inventario que no puede enumerar
        # no es un inventario vacío.
        raise PreflightRejected(
            f"no puedo enumerar los sidecars de `{base}`: {e}") from e
    return SUFIJOS + tuple(sorted(dinamicos))


def _inventario(base: str) -> dict:
    """Conjunto presente + metadatos + sha de cada pieza."""
    out = {}
    for suf in _sufijos_presentes(base):
        ruta = base + suf
        if _stat_seguro(ruta) is None:
            continue
        out[suf] = _huella_fd(ruta)
    return out


def _copiar_a(base: str, destino: str) -> None:
    # Los super-journals tienen nombre DINÁMICO, así que no caben en una lista
    # fija. El inventario ya los ve; si la copia no los trajera, la foto sería
    # de un estado que no incluye la transacción multi-base a medias — y el
    # brazo `copia == antes` la daría por buena porque ni la mira.
    dinamicos = [x for x in _sufijos_presentes(base)
                 if x.startswith("-mj") and x != "-mj"]
    for suf in tuple(COPIABLES) + tuple(dinamicos):
        ruta = base + suf
        if not os.path.exists(ruta):
            continue
        fd = os.open(ruta, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            with open(os.path.join(destino, os.path.basename(base) + suf), "wb") as dst:
                while True:
                    trozo = os.read(fd, 1 << 20)
                    if not trozo:
                        break
                    dst.write(trozo)
        finally:
            os.close(fd)


def _foto_estable(base: str, destino: str, *, ahora=time.time) -> dict:
    """Copia con inventario ANTES y DESPUÉS, y sólo acepta si nada se movió.

    `sha_antes == sha_copia == sha_después` para cada pieza, mismo conjunto de
    ficheros y mismo (dev, inode) y metadatos. Comparar sólo antes/después
    dejaría pasar una copia hecha a mitad de una escritura que revierte.
    """
    t0 = ahora()
    ultimo = None
    for _ in range(PREFLIGHT_INTENTOS):
        antes = _inventario(base)
        if "" not in antes:
            raise PreflightRejected("no hay fichero principal que inspeccionar")
        if antes[""][0][3] == 0:
            raise PreflightRejected(
                "el fichero principal tiene CERO bytes: no dice de quién es, y "
                "un `touch` de cualquiera se leería como base nuestra vacía")
        for f in os.listdir(destino):
            os.unlink(os.path.join(destino, f))
        _copiar_a(base, destino)
        copia = _inventario(os.path.join(destino, os.path.basename(base)))
        despues = _inventario(base)
        # COPIABLES EFECTIVOS: los `-mj*` se COPIAN pero no entraban en la
        # comparación, así que la copia podía traer un super-journal distinto
        # del origen y el brazo `copia == antes` ni lo miraba. Copiar sin
        # comparar es tener el fichero y no tener el testigo.
        copiables_efectivos = tuple(COPIABLES) + tuple(
            x for x in antes if x.startswith("-mj") and x != "-mj")
        mismo = (set(antes) == set(despues)
                 and all(antes[k][0][:2] == despues[k][0][:2] for k in antes)
                 and all(antes[k][0] == despues[k][0] for k in antes)
                 and all(antes[k][1] == despues[k][1] for k in antes)
                 and all(k not in copiables_efectivos
                         or (k in copia and copia[k][1] == antes[k][1])
                         for k in antes))
        if mismo:
            return antes
        ultimo = (set(antes), set(despues))
        if ahora() - t0 > PREFLIGHT_SEGUNDOS:
            break
    raise PreflightUnstable(
        f"el fichero se movió mientras lo copiaba ({ultimo}): hay un escritor "
        f"activo, y eso es un estado del mundo que se declara, no una carrera "
        f"que se gane a reintentos")


def _sha256_file(path: str) -> str:
    """Digest sobre el descriptor abierto, sin seguir enlaces."""
    return _huella_fd(path)[1]


def _fsync_file(path: str) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_json_atomic(path: str, value: Mapping[str, Any]) -> None:
    """Publica un manifiesto sólo después de persistir todos sus bytes."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, temporary = tempfile.mkstemp(prefix=".llminbox-manifest-", dir=directory)
    try:
        payload = _canonical(value).encode("utf-8") + b"\n"
        with os.fdopen(fd, "wb", closefd=True) as out:
            fd = -1
            out.write(payload)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
        _fsync_dir(directory)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


# ── MANIFIESTO SEMÁNTICO ────────────────────────────────────────────────────
# `durable_v` SOLO no acredita nada, y una base con `meta` y nada más tampoco:
# el sello es una afirmación sobre la forma, así que la forma se COMPRUEBA. Un
# fichero con un `meta` plantado a mano diría ser lo que quisiera.
def _nombres_columnas(spec: str) -> frozenset[str]:
    """Notación compacta para que el manifiesto completo siga siendo auditable."""
    return frozenset(spec.split())


def _sql_forma(sql: str) -> str:
    """Normaliza sólo presentación; no elimina ningún predicado durable."""
    value = "".join(line for line in sql.lower().splitlines()
                    if not line.lstrip().startswith("--"))
    value = "".join(value.split()).rstrip(";")
    return value.replace("createtableifnotexists", "createtable", 1)


def _table_sql_from_schema(table: str) -> str:
    clean = "\n".join(line for line in SCHEMA.splitlines()
                      if not line.strip().startswith("--"))
    prefix = f"createtable{table}("
    for statement in clean.split(";"):
        normalized = _sql_forma(statement)
        if normalized.startswith(prefix):
            return normalized
    raise RuntimeError(f"SCHEMA no contiene CREATE TABLE de {table}")


_OBJETOS_V3 = {
    "meta", "principals", "credential_bindings", "runtime_sessions", "events",
    "receipts", "receipt_transitions", "idempotency", "outbox", "leases",
    "commands", "event_causes", "command_causes", "command_event_causes",
    "external_causes", "denials", "denial_aggregates", "event_acks",
    "outbox_operations", "unknown_credentials",
}

# No es una muestra de columnas nuevas: es la forma durable ENTERA que los
# caminos operacionales de READY leen o escriben. Una tabla con el nombre bueno
# y media anatomía ya no acredita v3.
_COLUMNAS_V3 = {
    "meta": _nombres_columnas("k v"),
    "principals": _nombres_columnas(
        "principal_id principal role lane created_at"),
    "credential_bindings": _nombres_columnas(
        "binding_id credential_ref principal_id principal_source capabilities "
        "generation bound_at retired_at"),
    "runtime_sessions": _nombres_columnas(
        "runtime_instance principal_id role lane token_hash generation issued_at "
        "expires_at rotated_from revoked_at revoke_reason annotations"),
    "events": _nombres_columnas(
        "n event_id lane verb kind head body recipients intent principal_id role "
        "runtime_instance occurred_at payload_sha attestation"),
    "receipts": _nombres_columnas(
        "receipt_id subject_kind subject_id principal_id lane current_state "
        "created_at updated_at"),
    "receipt_transitions": _nombres_columnas(
        "transition_id receipt_id seq state at detail"),
    "idempotency": _nombres_columnas(
        "principal_id lane verb key req_hash event_id receipt_id req_hash_v created_at"),
    "outbox": _nombres_columnas(
        "n event_id ledger state attempts next_attempt lease_until lease_by "
        "claim_token last_error created_at materialized_at"),
    "leases": _nombres_columnas(
        "lane resource principal_id runtime_instance fencing_token acquired_at "
        "expires_at released_at"),
    "commands": _nombres_columnas(
        "command_id workstream_id revision lane principal_id role runtime_instance "
        "attribution_status payload state supersedes created_at"),
    "event_causes": _nombres_columnas("event_id cause_event_id"),
    "command_causes": _nombres_columnas("command_id cause_command_id"),
    "command_event_causes": _nombres_columnas("command_id cause_event_id"),
    "external_causes": _nombres_columnas(
        "child_kind child_id ledger entry_eid authority noted_at"),
    "denials": _nombres_columnas(
        "denial_id principal_id lane runtime_instance reason bucket receipt_id at"),
    "denial_aggregates": _nombres_columnas(
        "principal_id lane reason bucket receipt_id suppressed first_at last_at"),
    "event_acks": _nombres_columnas(
        "event_id recipient principal_id runtime_instance ack_ref at"),
    "outbox_operations": _nombres_columnas(
        "operation_id event_id operation operator lane runtime_instance reason "
        "from_state at"),
    "unknown_credentials": _nombres_columnas(
        "fingerprint count first_at last_at"),
}

# Afinidad, nulabilidad y orden de PK también son forma durable: tener
# ``attempts`` como TEXT o permitir un ``runtime_instance`` NULL cambia la
# semántica aunque el nombre de la columna sobreviva. Todo lo no enumerado como
# INTEGER/REAL es TEXT; todo lo no enumerado como nullable es NOT NULL, salvo
# las PK simples de tablas rowid, cuyo bit PRAGMA histórico es 0.
_ENTEROS_V3 = {
    ("credential_bindings", "generation"),
    ("runtime_sessions", "generation"), ("events", "n"),
    ("receipt_transitions", "seq"), ("idempotency", "req_hash_v"),
    ("outbox", "n"), ("outbox", "attempts"),
    ("leases", "fencing_token"), ("commands", "revision"),
    ("denial_aggregates", "suppressed"), ("unknown_credentials", "count"),
}
_REALES_V3 = {
    ("runtime_sessions", "issued_at"), ("runtime_sessions", "expires_at"),
    ("runtime_sessions", "revoked_at"), ("outbox", "next_attempt"),
    ("outbox", "lease_until"), ("leases", "acquired_at"),
    ("leases", "expires_at"), ("leases", "released_at"),
}
_NULLABLES_V3 = {
    "credential_bindings": {"retired_at"},
    "runtime_sessions": {"rotated_from", "revoked_at", "revoke_reason", "annotations"},
    "events": {"kind"},
    "receipts": {"principal_id", "lane"},
    "receipt_transitions": {"detail"},
    "outbox": {"lease_until", "lease_by", "claim_token", "last_error",
               "materialized_at"},
    "leases": {"released_at"},
    "commands": {"runtime_instance", "supersedes"},
    "event_acks": {"ack_ref"},
}
_PKS_V3 = {
    "meta": ("k",), "principals": ("principal_id",),
    "credential_bindings": ("binding_id",),
    "runtime_sessions": ("runtime_instance",), "events": ("n",),
    "receipts": ("receipt_id",), "receipt_transitions": ("transition_id",),
    "idempotency": ("principal_id", "lane", "verb", "key"),
    "outbox": ("n",), "leases": ("lane", "resource"),
    "commands": ("command_id",),
    "event_causes": ("event_id", "cause_event_id"),
    "command_causes": ("command_id", "cause_command_id"),
    "command_event_causes": ("command_id", "cause_event_id"),
    "external_causes": ("child_kind", "child_id", "ledger", "entry_eid"),
    "denials": ("denial_id",),
    "denial_aggregates": ("principal_id", "lane", "reason", "bucket"),
    "event_acks": ("event_id", "recipient"),
    "outbox_operations": ("operation_id",),
    "unknown_credentials": ("fingerprint",),
}


def _normalizar_default(valor):
    """Representación estable de literales DEFAULT expuestos por PRAGMA.

    SQLite conserva detalles de escritura como paréntesis y comillas. El
    contrato es el VALOR, no si se escribió ``0``, ``(0)`` o ``'false'`` con
    comillas dobles; expresiones no literales se comparan sin espacios ni
    diferencias de mayúsculas, pero nunca se ejecuta DDL no confiable.
    """
    if valor is None:
        return None
    texto = str(valor).strip()
    while texto.startswith("(") and texto.endswith(")"):
        profundidad = 0
        comilla = None
        envuelve = True
        for i, caracter in enumerate(texto):
            if comilla:
                if caracter == comilla:
                    # SQL escapa una comilla duplicándola.
                    if i + 1 < len(texto) and texto[i + 1] == comilla:
                        continue
                    comilla = None
                continue
            if caracter in ("'", '"'):
                comilla = caracter
            elif caracter == "(":
                profundidad += 1
            elif caracter == ")":
                profundidad -= 1
                if profundidad == 0 and i != len(texto) - 1:
                    envuelve = False
                    break
        if not envuelve or profundidad:
            break
        texto = texto[1:-1].strip()
    if len(texto) >= 2 and texto[0] == texto[-1] and texto[0] in ("'", '"'):
        q = texto[0]
        return ("text", texto[1:-1].replace(q + q, q))
    try:
        return ("integer", int(texto, 10))
    except ValueError:
        try:
            return ("real", float(texto))
        except ValueError:
            return ("expression", "".join(texto.lower().split()))


# Todos —y sólo— los DEFAULT de SCHEMA v3. También se verifica ``None`` en el
# resto de columnas, para que añadir un default silencioso bajo el mismo sello
# sea una deformación y obligue a versionar/migrar explícitamente.
_DEFAULTS_V3 = {
    ("credential_bindings", "capabilities"): ("text", "[]"),
    ("idempotency", "req_hash_v"): ("integer", 1),
    ("outbox", "state"): ("text", "pending"),
    ("outbox", "attempts"): ("integer", 0),
    ("commands", "attribution_status"): ("text", "verified"),
    ("external_causes", "authority"): ("text", "false"),
    ("denials", "lane"): ("text", ""),
    ("denials", "runtime_instance"): ("text", ""),
    ("denial_aggregates", "lane"): ("text", ""),
    ("denial_aggregates", "suppressed"): ("integer", 0),
    ("unknown_credentials", "count"): ("integer", 0),
}


def _formas_v3() -> dict:
    formas = {}
    for tabla, columnas in _COLUMNAS_V3.items():
        pk = _PKS_V3[tabla]
        simple = len(pk) == 1
        formas[tabla] = {}
        for columna in columnas:
            tipo = ("INTEGER" if (tabla, columna) in _ENTEROS_V3 else
                    "REAL" if (tabla, columna) in _REALES_V3 else "TEXT")
            orden_pk = pk.index(columna) + 1 if columna in pk else 0
            no_nulo = (columna not in _NULLABLES_V3.get(tabla, set())
                       and not (simple and orden_pk == 1))
            formas[tabla][columna] = (
                tipo, int(no_nulo), orden_pk, _DEFAULTS_V3.get((tabla, columna)))
    return formas


_FORMAS_V3 = _formas_v3()

# Índices con nombre: además de unicidad, forman parte del contrato de servicio
# (selección por carril, claim del outbox y orden de comandos). ``where`` evita
# el falso positivo de un índice con las columnas correctas pero otro dominio.
_INDICES_V3 = {
    "i_prn_role": ("principals", False, ("role", "lane"), None),
    "u_binding_activa": (
        "credential_bindings", True, ("credential_ref",),
        "whereretired_atisnull"),
    "i_ev_lane": ("events", False, ("lane", "n"), None),
    "i_outbox_ready": (
        "outbox", False, ("next_attempt", "n"), "wherestate='pending'"),
    "i_cmd_ws": (
        "commands", False, ("lane", "workstream_id", "revision"), None),
    "i_denials": (
        "denials", False, ("principal_id", "reason", "bucket"), None),
    "i_outbox_ops": (
        "outbox_operations", False, ("event_id", "at"), None),
}

# Claves semánticas que impiden doble propietario, doble efecto o doble arista.
# Se buscan por columnas y no por el nombre sqlite_autoindex_* (no es estable).
_UNICOS_V3 = {
    "principals": {("principal_id",)},
    "credential_bindings": {("binding_id",), ("credential_ref",)},
    "runtime_sessions": {("runtime_instance",), ("token_hash",)},
    "events": {("event_id",)},
    "receipts": {("receipt_id",), ("subject_kind", "subject_id")},
    "receipt_transitions": {
        ("transition_id",), ("receipt_id", "seq")},
    "idempotency": {("principal_id", "lane", "verb", "key")},
    "outbox": {("event_id",)},
    "leases": {("lane", "resource")},
    "commands": {("command_id",), ("lane", "workstream_id", "revision")},
    "event_causes": {("event_id", "cause_event_id")},
    "command_causes": {("command_id", "cause_command_id")},
    "command_event_causes": {("command_id", "cause_event_id")},
    "external_causes": {("child_kind", "child_id", "ledger", "entry_eid")},
    "denials": {("denial_id",)},
    "denial_aggregates": {("principal_id", "lane", "reason", "bucket")},
    "event_acks": {("event_id", "recipient")},
    "outbox_operations": {("operation_id",)},
    "unknown_credentials": {("fingerprint",)},
}

# Integridad referencial de identidad, causalidad, recibos y fencing. Comparar
# triples semánticos tolera el orden interno que elija SQLite, pero no una FK
# decorativa apuntando a otra tabla/columna.
_FKS_V3 = {
    "credential_bindings": {("principal_id", "principals", "principal_id")},
    "runtime_sessions": {("principal_id", "principals", "principal_id")},
    "events": {("principal_id", "principals", "principal_id"),
               ("runtime_instance", "runtime_sessions", "runtime_instance")},
    "receipts": {("principal_id", "principals", "principal_id")},
    "receipt_transitions": {("receipt_id", "receipts", "receipt_id")},
    "idempotency": {("principal_id", "principals", "principal_id"),
                    ("event_id", "events", "event_id"),
                    ("receipt_id", "receipts", "receipt_id")},
    "outbox": {("event_id", "events", "event_id")},
    "leases": {("principal_id", "principals", "principal_id"),
               ("runtime_instance", "runtime_sessions", "runtime_instance")},
    "commands": {("principal_id", "principals", "principal_id"),
                 ("runtime_instance", "runtime_sessions", "runtime_instance"),
                 ("supersedes", "commands", "command_id")},
    "event_causes": {("event_id", "events", "event_id"),
                     ("cause_event_id", "events", "event_id")},
    "command_causes": {("command_id", "commands", "command_id"),
                       ("cause_command_id", "commands", "command_id")},
    "command_event_causes": {("command_id", "commands", "command_id"),
                             ("cause_event_id", "events", "event_id")},
    "denials": {("principal_id", "principals", "principal_id"),
                ("receipt_id", "receipts", "receipt_id")},
    "denial_aggregates": {("principal_id", "principals", "principal_id"),
                          ("receipt_id", "receipts", "receipt_id")},
    "event_acks": {("event_id", "events", "event_id"),
                   ("principal_id", "principals", "principal_id"),
                   ("runtime_instance", "runtime_sessions", "runtime_instance")},
    "outbox_operations": {("event_id", "events", "event_id"),
                          ("operator", "principals", "principal_id"),
                          ("runtime_instance", "runtime_sessions", "runtime_instance")},
}

# Fragmentos normalizados del DDL para CHECK que PRAGMA no expone. Sólo se
# enumeran invariantes de autoridad/estado; no se ata el manifiesto al formato.
_CHECKS_V3 = {
    "credential_bindings": {
        "check(principal_sourcein('explicit','derived_from_role'))"},
    "receipts": {
        "check(subject_kindin('event','command','denial','denial_aggregate'))"},
    "outbox": {
        "check(statein('pending','materialized','failed','abandoned'))"},
    "commands": {
        "check(attribution_statusin('verified','legacy_unattributed'))",
        "check(statein('accepted','received','executing','succeeded','failed',"
        "'cancelled','superseded'))",
        "check((attribution_status='verified'andruntime_instanceisnotnull)or("
        "attribution_status='legacy_unattributed'andruntime_instanceisnull))"},
    "external_causes": {
        "check(child_kindin('event','command'))", "check(authority='false')"},
    "outbox_operations": {"check(operationin('requeue','abandon'))"},
}


# v4 = v3 con `events.recipients_roles` y `events.recipients_broadcast`. Se
# declara DERIVANDO de v3 y no copiando: una copia diverge, y el manifiesto es
# justo el sitio donde una divergencia no daría error, sólo un veredicto falso.
_COLUMNAS_V4 = {**_COLUMNAS_V3,
                "events": _COLUMNAS_V3["events"] | {"recipients_roles",
                                                    "recipients_broadcast"}}
# v5 = v4 + `leases.resource_literal`. Derivado, no copiado, por lo mismo que v4.
_COLUMNAS_V5 = {**_COLUMNAS_V4,
                "leases": _COLUMNAS_V4["leases"] | {"resource_literal"}}


# ── v6 = v5 + `admission_history` ───────────────────────────────────────────
# La tabla entra al manifiesto con TODA su anatomía (columnas, formas, únicos,
# FKs y CHECKs) y no sólo con su nombre: una barrera con la tabla presente y el
# CHECK de estados amputado aceptaría un `state` inventado, que es exactamente
# la deformación que READY existe para no dejar pasar.
_OBJETOS_V6 = _OBJETOS_V3 | {"admission_history"}
_COLUMNAS_V6 = {**_COLUMNAS_V5, "admission_history": _nombres_columnas(
    "lane verb epoch state origin operator runtime_instance reason_code at")}
_PKS_V6 = {**_PKS_V3, "admission_history": ("lane", "verb", "epoch")}
_ENTEROS_V6 = _ENTEROS_V3 | {("admission_history", "epoch")}
_NULLABLES_V6 = {**_NULLABLES_V3,
                 # v4/v5 añadieron TEXT sin NOT NULL; v6 conserva esa forma.
                 "events": _NULLABLES_V3["events"] | {
                     "recipients_roles", "recipients_broadcast"},
                 "leases": _NULLABLES_V3["leases"] | {"resource_literal"},
                 "admission_history": {"operator", "runtime_instance"}}


def _formas_v6() -> dict:
    """Forma exacta de TODA v6, incluidas las columnas añadidas en v4/v5."""
    formas = {}
    for tabla, columnas in _COLUMNAS_V6.items():
        pk = _PKS_V6[tabla]
        simple = len(pk) == 1
        formas[tabla] = {}
        for columna in columnas:
            tipo = ("INTEGER" if (tabla, columna) in _ENTEROS_V6 else
                    "REAL" if (tabla, columna) in _REALES_V3 else "TEXT")
            orden_pk = pk.index(columna) + 1 if columna in pk else 0
            no_nulo = (columna not in _NULLABLES_V6.get(tabla, set())
                       and not (simple and orden_pk == 1))
            formas[tabla][columna] = (
                tipo, int(no_nulo), orden_pk,
                _DEFAULTS_V3.get((tabla, columna)))
    return formas


_FORMAS_V6 = _formas_v6()
_UNICOS_V6 = {**_UNICOS_V3,
              "admission_history": {("lane", "verb", "epoch")}}
_FKS_V6 = {**_FKS_V3, "admission_history": {
    ("operator", "principals", "principal_id"),
    ("runtime_instance", "runtime_sessions", "runtime_instance")}}
_CHECKS_V6 = {**_CHECKS_V3, "admission_history": {
    "check(verbin('events.accept','outbox.requeue'))",
    "check(statein('open','closed','sealed'))",
    "check(originin('operator','migration'))",
    "check(reason_codein('rollout','incident','maintenance',"
    "'drain_for_rollback','schema_migration'))",
    # El invariante que impide una firma fabricada y una firma perdida.
    "check((origin='operator'andoperatorisnotnullandruntime_instanceisnotnull)or(origin='migration'andoperatorisnullandruntime_instanceisnull))",
    # El motivo reservado acredita exactamente el origen de migración.
    "check((origin='migration'andreason_code='schema_migration')or(origin='operator'andreason_code<>'schema_migration'))",
    # …y el que impide que una fila de migración abra nada.
    "check(origin<>'migration'orstate='closed')"}}


# ── v7 = control plane durable de ADR-002 ──────────────────────────────────
_OBJETOS_NUEVOS_V7 = {
    "organization_revisions", "organization_roles", "organization_reports",
    "organization_reviewers", "organization_escalations", "expected_workloads",
    "runtime_recoveries", "runtime_observations", "runtime_status_transitions",
    "runtime_status",
}
_OBJETOS_V7 = _OBJETOS_V6 | _OBJETOS_NUEVOS_V7
_TABLE_SQL_V7 = {
    table: _table_sql_from_schema(table)
    for table in (_OBJETOS_NUEVOS_V7 | {"receipts"})
}
_COLUMNAS_V7 = {**_COLUMNAS_V6,
    "organization_revisions": _nombres_columnas(
        "lane revision source_sha256 attestation_state active activated_by "
        "activated_runtime activated_at"),
    "organization_roles": _nombres_columnas(
        "lane revision role layer policy_code"),
    "organization_reports": _nombres_columnas(
        "lane revision role reports_to"),
    "organization_reviewers": _nombres_columnas(
        "lane revision role reviewer_role"),
    "organization_escalations": _nombres_columnas(
        "lane revision role trigger_code target_role"),
    "expected_workloads": _nombres_columnas(
        "lane organization_revision workload_id role principal_id "
        "runtime_instance credential_generation"),
    "runtime_recoveries": _nombres_columnas(
        "recovery_id lane organization_revision workload_id "
        "target_runtime_instance target_generation requester_principal "
        "requester_runtime requester_generation verb idempotency_key req_hash "
        "reason_code action_code fenced_resource fencing_token command_id accepted_at"),
    "runtime_observations": _nombres_columnas(
        "observation_id lane organization_revision workload_id "
        "target_runtime_instance target_generation observer_principal "
        "observer_runtime observer_generation verb idempotency_key req_hash "
        "supervisor_seq observation_kind reason_code detector_state cpu_millis "
        "rss_bytes heartbeat_age_ms exit_code recovery_command_id observed_at"),
    "runtime_status_transitions": _nombres_columnas(
        "transition_id lane organization_revision workload_id "
        "target_runtime_instance target_generation from_status to_status "
        "detector_state status_seq cause_kind cause_id reason_code observation_id "
        "recovery_command_id receipt_id at"),
    "runtime_status": _nombres_columnas(
        "lane workload_id organization_revision principal_id role runtime_instance "
        "credential_generation status detector_state status_seq status_since "
        "last_observed_at cause_id transition_id receipt_id"),
}
_PKS_V7 = {**_PKS_V6,
    "organization_revisions": ("lane", "revision"),
    "organization_roles": ("lane", "revision", "role"),
    "organization_reports": ("lane", "revision", "role"),
    "organization_reviewers": ("lane", "revision", "role", "reviewer_role"),
    "organization_escalations": (
        "lane", "revision", "role", "trigger_code", "target_role"),
    "expected_workloads": ("lane", "organization_revision", "workload_id"),
    "runtime_recoveries": ("recovery_id",),
    "runtime_observations": ("observation_id",),
    "runtime_status_transitions": ("transition_id",),
    "runtime_status": ("lane", "workload_id"),
}
_ENTEROS_V7 = _ENTEROS_V6 | {
    ("organization_revisions", "revision"), ("organization_revisions", "active"),
    ("organization_roles", "revision"), ("organization_roles", "layer"),
    ("organization_reports", "revision"),
    ("organization_reviewers", "revision"),
    ("organization_escalations", "revision"),
    ("expected_workloads", "organization_revision"),
    ("expected_workloads", "credential_generation"),
    ("runtime_recoveries", "organization_revision"),
    ("runtime_recoveries", "target_generation"),
    ("runtime_recoveries", "requester_generation"),
    ("runtime_recoveries", "fencing_token"),
    ("runtime_observations", "organization_revision"),
    ("runtime_observations", "target_generation"),
    ("runtime_observations", "observer_generation"),
    ("runtime_observations", "supervisor_seq"),
    ("runtime_observations", "cpu_millis"),
    ("runtime_observations", "rss_bytes"),
    ("runtime_observations", "heartbeat_age_ms"),
    ("runtime_observations", "exit_code"),
    ("runtime_status_transitions", "organization_revision"),
    ("runtime_status_transitions", "target_generation"),
    ("runtime_status_transitions", "status_seq"),
    ("runtime_status", "organization_revision"),
    ("runtime_status", "credential_generation"),
    ("runtime_status", "status_seq"),
}
_NULLABLES_V7 = {**_NULLABLES_V6,
    "events": _NULLABLES_V6["events"] | {
        "recipients_roles", "recipients_broadcast"},
    "leases": _NULLABLES_V6["leases"] | {"resource_literal"},
    "organization_roles": {"policy_code"},
    "expected_workloads": {"principal_id", "runtime_instance",
                             "credential_generation"},
    "runtime_observations": {"detector_state", "cpu_millis", "rss_bytes",
                              "heartbeat_age_ms", "exit_code",
                              "recovery_command_id"},
    "runtime_status_transitions": {"target_runtime_instance",
                                   "target_generation", "from_status",
                                   "detector_state", "observation_id",
                                   "recovery_command_id"},
    "runtime_status": {"principal_id", "runtime_instance",
                       "credential_generation", "detector_state",
                       "last_observed_at"},
}


def _formas_v7() -> dict:
    formas = {}
    for tabla, columnas in _COLUMNAS_V7.items():
        pk = _PKS_V7[tabla]
        simple = len(pk) == 1
        formas[tabla] = {}
        for columna in columnas:
            tipo = ("INTEGER" if (tabla, columna) in _ENTEROS_V7 else
                    "REAL" if (tabla, columna) in _REALES_V3 else "TEXT")
            orden_pk = pk.index(columna) + 1 if columna in pk else 0
            no_nulo = (columna not in _NULLABLES_V7.get(tabla, set())
                       and not (simple and orden_pk == 1))
            formas[tabla][columna] = (
                tipo, int(no_nulo), orden_pk,
                _DEFAULTS_V3.get((tabla, columna)))
    return formas


_FORMAS_V7 = _formas_v7()
_INDICES_V7 = {**_INDICES_V3,
    "u_principal_lane": ("principals", True, ("lane", "principal_id"), None),
    "u_runtime_lane": (
        "runtime_sessions", True, ("lane", "runtime_instance"), None),
    "u_runtime_lane_generation": (
        "runtime_sessions", True,
        ("lane", "runtime_instance", "generation"), None),
    "u_runtime_lane_principal": (
        "runtime_sessions", True,
        ("lane", "principal_id", "runtime_instance"), None),
    "u_runtime_lane_principal_generation": (
        "runtime_sessions", True,
        ("lane", "principal_id", "runtime_instance", "generation"), None),
    "u_command_lane": (
        "commands", True, ("lane", "command_id"), None),
    "u_org_active_lane": (
        "organization_revisions", True, ("lane",), "whereactive=1"),
    "u_expected_runtime": (
        "expected_workloads", True,
        ("lane", "organization_revision", "runtime_instance"),
        "whereruntime_instanceisnotnull"),
    "u_expected_target": (
        "expected_workloads", True,
        ("lane", "organization_revision", "workload_id", "runtime_instance",
         "credential_generation"), None),
    "u_expected_binding": (
        "expected_workloads", True,
        ("lane", "organization_revision", "workload_id", "principal_id",
         "runtime_instance", "credential_generation"), None),
    "i_runtime_observation_target": (
        "runtime_observations", False,
        ("lane", "workload_id", "target_runtime_instance", "target_generation",
         "supervisor_seq"), None),
}
_UNICOS_V7 = {**_UNICOS_V6,
    "principals": _UNICOS_V6["principals"] | {("lane", "principal_id")},
    "runtime_sessions": _UNICOS_V6["runtime_sessions"] | {
        ("lane", "runtime_instance"),
        ("lane", "runtime_instance", "generation"),
        ("lane", "principal_id", "runtime_instance"),
        ("lane", "principal_id", "runtime_instance", "generation")},
    "commands": _UNICOS_V6["commands"] | {("lane", "command_id")},
    "organization_revisions": {("lane", "revision"), ("lane",)},
    "organization_roles": {("lane", "revision", "role")},
    "organization_reports": {("lane", "revision", "role")},
    "organization_reviewers": {("lane", "revision", "role", "reviewer_role")},
    "organization_escalations": {
        ("lane", "revision", "role", "trigger_code", "target_role")},
    "expected_workloads": {
        ("lane", "organization_revision", "workload_id"),
        ("lane", "organization_revision", "runtime_instance"),
        ("lane", "organization_revision", "workload_id", "runtime_instance",
         "credential_generation"),
        ("lane", "organization_revision", "workload_id", "principal_id",
         "runtime_instance", "credential_generation")},
    "runtime_recoveries": {
        ("recovery_id",), ("command_id",),
        ("requester_principal", "lane", "verb", "idempotency_key")},
    "runtime_observations": {
        ("observation_id",),
        ("observer_principal", "lane", "verb", "idempotency_key"),
        ("observer_principal", "observer_runtime", "observer_generation",
         "lane", "target_runtime_instance", "target_generation",
         "supervisor_seq")},
    "runtime_status_transitions": {
        ("transition_id",), ("receipt_id",),
        ("lane", "workload_id", "status_seq")},
    "runtime_status": {
        ("lane", "workload_id"), ("transition_id",), ("receipt_id",)},
}
_FKS_V7 = {**_FKS_V6,
    "organization_revisions": {
        ("activated_by", "principals", "principal_id"),
        ("activated_runtime", "runtime_sessions", "runtime_instance")},
    "organization_roles": {("lane", "organization_revisions", "lane"),
                           ("revision", "organization_revisions", "revision")},
    "organization_reports": {
        ("role", "organization_roles", "role"),
        ("reports_to", "organization_roles", "role")},
    "organization_reviewers": {
        ("role", "organization_roles", "role"),
        ("reviewer_role", "organization_roles", "role")},
    "organization_escalations": {
        ("role", "organization_roles", "role"),
        ("target_role", "organization_roles", "role")},
    "expected_workloads": {
        ("role", "organization_roles", "role"),
        ("principal_id", "principals", "principal_id"),
        ("runtime_instance", "runtime_sessions", "runtime_instance")},
    "runtime_recoveries": {
        ("workload_id", "expected_workloads", "workload_id"),
        ("target_runtime_instance", "runtime_sessions", "runtime_instance"),
        ("requester_principal", "principals", "principal_id"),
        ("requester_runtime", "runtime_sessions", "runtime_instance"),
        ("command_id", "commands", "command_id")},
    "runtime_observations": {
        ("workload_id", "expected_workloads", "workload_id"),
        ("target_runtime_instance", "runtime_sessions", "runtime_instance"),
        ("observer_principal", "principals", "principal_id"),
        ("observer_runtime", "runtime_sessions", "runtime_instance"),
        ("recovery_command_id", "commands", "command_id")},
    "runtime_status_transitions": {
        ("workload_id", "expected_workloads", "workload_id"),
        ("target_runtime_instance", "runtime_sessions", "runtime_instance"),
        ("observation_id", "runtime_observations", "observation_id"),
        ("recovery_command_id", "commands", "command_id"),
        ("receipt_id", "receipts", "receipt_id")},
    "runtime_status": {
        ("workload_id", "expected_workloads", "workload_id"),
        ("principal_id", "principals", "principal_id"),
        ("runtime_instance", "runtime_sessions", "runtime_instance"),
        ("transition_id", "runtime_status_transitions", "transition_id"),
        ("receipt_id", "receipts", "receipt_id")},
}
_FK_GROUPS_V7 = {
    "organization_revisions": {
        (("lane", "activated_by"), "principals", ("lane", "principal_id")),
        (("lane", "activated_runtime"), "runtime_sessions",
         ("lane", "runtime_instance")),
        (("lane", "activated_by", "activated_runtime"), "runtime_sessions",
         ("lane", "principal_id", "runtime_instance"))},
    "organization_roles": {
        (("lane", "revision"), "organization_revisions", ("lane", "revision"))},
    "organization_reports": {
        (("lane", "revision", "role"), "organization_roles",
         ("lane", "revision", "role")),
        (("lane", "revision", "reports_to"), "organization_roles",
         ("lane", "revision", "role"))},
    "organization_reviewers": {
        (("lane", "revision", "role"), "organization_roles",
         ("lane", "revision", "role")),
        (("lane", "revision", "reviewer_role"), "organization_roles",
         ("lane", "revision", "role"))},
    "organization_escalations": {
        (("lane", "revision", "role"), "organization_roles",
         ("lane", "revision", "role")),
        (("lane", "revision", "target_role"), "organization_roles",
         ("lane", "revision", "role"))},
    "expected_workloads": {
        (("lane", "organization_revision", "role"), "organization_roles",
         ("lane", "revision", "role")),
        (("lane", "principal_id"), "principals", ("lane", "principal_id")),
        (("lane", "runtime_instance", "credential_generation"),
         "runtime_sessions", ("lane", "runtime_instance", "generation")),
        (("lane", "principal_id", "runtime_instance", "credential_generation"),
         "runtime_sessions",
         ("lane", "principal_id", "runtime_instance", "generation"))},
    "runtime_recoveries": {
        (("lane", "organization_revision", "workload_id"), "expected_workloads",
         ("lane", "organization_revision", "workload_id")),
        (("lane", "target_runtime_instance", "target_generation"),
         "runtime_sessions", ("lane", "runtime_instance", "generation")),
        (("lane", "requester_principal"), "principals", ("lane", "principal_id")),
        (("lane", "requester_runtime", "requester_generation"),
         "runtime_sessions", ("lane", "runtime_instance", "generation")),
        (("lane", "organization_revision", "workload_id",
          "target_runtime_instance", "target_generation"),
         "expected_workloads",
         ("lane", "organization_revision", "workload_id", "runtime_instance",
          "credential_generation")),
        (("lane", "requester_principal", "requester_runtime",
          "requester_generation"), "runtime_sessions",
         ("lane", "principal_id", "runtime_instance", "generation")),
        (("lane", "command_id"), "commands", ("lane", "command_id"))},
    "runtime_observations": {
        (("lane", "organization_revision", "workload_id"), "expected_workloads",
         ("lane", "organization_revision", "workload_id")),
        (("lane", "target_runtime_instance", "target_generation"),
         "runtime_sessions", ("lane", "runtime_instance", "generation")),
        (("lane", "observer_principal"), "principals", ("lane", "principal_id")),
        (("lane", "observer_runtime", "observer_generation"),
         "runtime_sessions", ("lane", "runtime_instance", "generation")),
        (("lane", "organization_revision", "workload_id",
          "target_runtime_instance", "target_generation"),
         "expected_workloads",
         ("lane", "organization_revision", "workload_id", "runtime_instance",
          "credential_generation")),
        (("lane", "observer_principal", "observer_runtime",
          "observer_generation"), "runtime_sessions",
         ("lane", "principal_id", "runtime_instance", "generation")),
        (("lane", "recovery_command_id"), "commands", ("lane", "command_id"))},
    "runtime_status_transitions": {
        (("lane", "organization_revision", "workload_id"), "expected_workloads",
         ("lane", "organization_revision", "workload_id")),
        (("lane", "target_runtime_instance", "target_generation"),
         "runtime_sessions", ("lane", "runtime_instance", "generation")),
        (("lane", "organization_revision", "workload_id",
          "target_runtime_instance", "target_generation"),
         "expected_workloads",
         ("lane", "organization_revision", "workload_id", "runtime_instance",
          "credential_generation")),
        (("lane", "recovery_command_id"), "commands", ("lane", "command_id"))},
    "runtime_status": {
        (("lane", "organization_revision", "workload_id"), "expected_workloads",
         ("lane", "organization_revision", "workload_id")),
        (("lane", "principal_id"), "principals", ("lane", "principal_id")),
        (("lane", "runtime_instance", "credential_generation"),
         "runtime_sessions", ("lane", "runtime_instance", "generation")),
        (("lane", "organization_revision", "workload_id", "principal_id",
          "runtime_instance", "credential_generation"), "expected_workloads",
         ("lane", "organization_revision", "workload_id", "principal_id",
          "runtime_instance", "credential_generation"))},
}
_CHECKS_V7 = {**_CHECKS_V6,
    "receipts": {"check(subject_kindin('event','command','denial',"
                 "'denial_aggregate','transition'))"},
    "organization_revisions": {
        "check(revision>0)", "check(activein(0,1))",
        "check(attestation_statein('attested','stale','unattested'))"},
    "organization_reports": {"check(role<>reports_to)"},
    "organization_reviewers": {"check(role<>reviewer_role)"},
    "organization_escalations": {"check(role<>target_role)"},
    "expected_workloads": {
        "check((runtime_instanceisnullandcredential_generationisnull)or(runtime_instanceisnotnullandcredential_generationisnotnull))",
        "check(runtime_instanceisnullorprincipal_idisnotnull)"},
    "runtime_recoveries": {
        "check(verb='runtime.recover')", "check(action_code='restart_runtime')",
        "check(fencing_token>0)"},
    "runtime_observations": {
        "check(verb='runtime.observe')",
        "check(observer_runtime<>target_runtime_instance)",
        "check(supervisor_seq>0andsupervisor_seq<=9223372036854775807)"},
    "runtime_status_transitions": {
        "check(from_statusisnullorfrom_statusin('absent','fresh','stale','degraded','stopped','recovering'))",
        "check(to_statusin('absent','fresh','stale','degraded','stopped','recovering'))",
        "check(cause_kindin('organization','observation','deadline','recovery'))"},
    "runtime_status": {
        "check(statusin('absent','fresh','stale','degraded','stopped','recovering'))",
        "check((runtime_instanceisnullandcredential_generationisnull)or(runtime_instanceisnotnullandcredential_generationisnotnull))",
        "check(status='absent'or(runtime_instanceisnotnullandprincipal_idisnotnull))"},
}

MANIFIESTOS = {
    1: {"objetos": {"principals", "credential_bindings", "runtime_sessions",
                    "events", "receipts", "receipt_transitions", "idempotency",
                    "outbox", "leases", "commands", "meta"},
        "columnas": {"commands": {"command_id", "workstream_id", "revision",
                                  "lane", "principal_id", "payload", "state",
                                  "created_at"}},
        "meta": {"durable_v", "pepper_check", "generation"}},
    2: {"objetos": {"principals", "credential_bindings", "runtime_sessions",
                    "events", "receipts", "receipt_transitions", "idempotency",
                    "outbox", "leases", "commands", "event_acks",
                    "outbox_operations", "denial_aggregates", "meta"},
        "columnas": {"commands": {"attribution_status", "role", "runtime_instance"}},
        "meta": {"durable_v", "pepper_check", "generation"}},
    3: {"objetos": _OBJETOS_V3,
        "columnas": _COLUMNAS_V3,
        "formas": _FORMAS_V3,
        "indices": _INDICES_V3,
        "unicos": _UNICOS_V3,
        "foreign_keys": _FKS_V3,
        "checks": _CHECKS_V3,
        "meta": {"durable_v", "pepper_check", "generation"}},
    5: {"objetos": _OBJETOS_V3,
        "columnas": _COLUMNAS_V5,
        "formas": _FORMAS_V3,
        "indices": _INDICES_V3,
        "unicos": _UNICOS_V3,
        "foreign_keys": _FKS_V3,
        "checks": _CHECKS_V3,
        "meta": {"durable_v", "pepper_check", "generation"}},
    6: {"objetos": _OBJETOS_V6,
        "columnas": _COLUMNAS_V6,
        "formas": _FORMAS_V6,
        "indices": _INDICES_V3,
        "unicos": _UNICOS_V6,
        "foreign_keys": _FKS_V6,
        "checks": _CHECKS_V6,
        "meta": {"durable_v", "pepper_check", "generation"},
        # v6 es la ÚNICA fuente del salto beta 6→7. Aceptar tablas, índices o
        # columnas extra como extensiones inocuas permitiría adoptar una media
        # migración v7 y también certificarla como snapshot de rollback.
        "exact": True},
    7: {"objetos": _OBJETOS_V7,
        "columnas": _COLUMNAS_V7,
        "formas": _FORMAS_V7,
        "indices": _INDICES_V7,
        "unicos": _UNICOS_V7,
        "foreign_keys": _FKS_V7,
        "foreign_key_groups": _FK_GROUPS_V7,
        "checks": _CHECKS_V7,
        "table_sql": _TABLE_SQL_V7,
        "meta": {"durable_v", "pepper_check", "generation"},
        "exact": True},
    4: {"objetos": _OBJETOS_V3,
        "columnas": _COLUMNAS_V4,
        "formas": _FORMAS_V3,
        "indices": _INDICES_V3,
        "unicos": _UNICOS_V3,
        "foreign_keys": _FKS_V3,
        "checks": _CHECKS_V3,
        "meta": {"durable_v", "pepper_check", "generation"}},
}


def _clasificar(con: sqlite3.Connection) -> tuple:
    """(veredicto, version, detalle) sobre la COPIA ya recuperada."""
    try:
        mal = con.execute("PRAGMA quick_check(1)").fetchone()[0]
    except sqlite3.DatabaseError as e:
        return ("corrupta", None, f"no se deja consultar: {e}")
    if mal != "ok":
        return ("corrupta", None, f"quick_check: {mal}")
    if con.execute("PRAGMA foreign_key_check").fetchall():
        return ("corrupta", None, "referencias huérfanas")
    objetos = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        "   AND name NOT LIKE 'sqlite_%'")}
    if not objetos:
        return ("indeterminada", None, "SQLite válida pero sin tablas")
    if objetos == {"meta"}:
        return ("indeterminada", None, "sólo `meta`: un sello sin forma que lo sostenga")
    try:
        fila = con.execute("SELECT v FROM meta WHERE k='durable_v'").fetchone()
    except sqlite3.OperationalError:
        return ("indeterminada", None, "tablas sin `meta`")
    if fila is None:
        return ("indeterminada", None, "forma sin sello")
    version = int(fila[0])
    if version > DURABLE_V:
        return ("futura", version, f"durable_v={version}")
    man = MANIFIESTOS.get(version)
    if man is None:
        return ("indeterminada", version, f"no tengo manifiesto de v{version}")
    faltan = man["objetos"] - objetos
    if faltan:
        return ("indeterminada", version,
                f"dice v{version} y le faltan objetos: {sorted(faltan)}")
    if man.get("exact") and objetos != man["objetos"]:
        return ("indeterminada", version,
                f"dice v{version} y trae objetos no reconocidos: "
                f"{sorted(objetos - man['objetos'])}")
    for tabla, columnas in man["columnas"].items():
        info_columnas = list(con.execute(f"PRAGMA table_info({tabla})"))
        reales = {c[1] for c in info_columnas}
        if columnas - reales:
            return ("indeterminada", version,
                    f"dice v{version} y a `{tabla}` le faltan {sorted(columnas - reales)}")
        if man.get("exact") and reales != columnas:
            return ("indeterminada", version,
                    f"dice v{version} y `{tabla}` trae columnas no reconocidas: "
                    f"{sorted(reales - columnas)}")
        formas = man.get("formas", {}).get(tabla)
        if formas:
            forma_real = {c[1]: ((c[2] or "").upper(), int(c[3]), int(c[5]),
                                  _normalizar_default(c[4]))
                          for c in info_columnas}
            deformadas = sorted(c for c, forma in formas.items()
                                if forma_real.get(c) != forma)
            if deformadas:
                return ("indeterminada", version,
                        f"dice v{version} y `{tabla}` deformó columnas: {deformadas}")
    # v1/v2 sólo tenían manifiesto nominal. Desde v3 READY promete además
    # idempotencia, propietario único, causalidad nativa y atribución; comprobar
    # esos invariantes DESPUÉS de entrar en READY sería descubrir la mutilación
    # cuando ya se ha abierto el original para operar.
    for nombre, (tabla, unico, columnas, fragmento) in man.get("indices", {}).items():
        fila = con.execute(
            "SELECT tbl_name, sql FROM sqlite_master"
            " WHERE type='index' AND name=?", (nombre,)).fetchone()
        if fila is None or fila[0] != tabla:
            return ("indeterminada", version,
                    f"dice v{version} y le falta el índice `{nombre}` de `{tabla}`")
        lista = {r[1]: r for r in con.execute(f"PRAGMA index_list({tabla})")}
        info = lista.get(nombre)
        cols_reales = tuple(r[2] for r in con.execute(f"PRAGMA index_info({nombre})"))
        sql_indice = "".join((fila[1] or "").lower().split())
        if (info is None or bool(info[2]) != unico or cols_reales != columnas
                or (fragmento is not None and fragmento not in sql_indice)):
            return ("indeterminada", version,
                    f"dice v{version} y el índice `{nombre}` no conserva su semántica")
    if man.get("exact"):
        indices_reales = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND name NOT LIKE 'sqlite_autoindex_%'")}
        if indices_reales != set(man.get("indices", {})):
            return ("indeterminada", version,
                    f"dice v{version} y sus índices nombrados no son exactos: "
                    f"faltan={sorted(set(man.get('indices', {})) - indices_reales)}, "
                    f"sobran={sorted(indices_reales - set(man.get('indices', {})))}")
    for tabla, esperadas in man.get("unicos", {}).items():
        reales = set()
        for indice in con.execute(f"PRAGMA index_list({tabla})"):
            if not indice[2]:
                continue
            reales.add(tuple(r[2] for r in con.execute(
                f"PRAGMA index_info({indice[1]})")))
        faltan_unicos = esperadas - reales
        if faltan_unicos:
            return ("indeterminada", version,
                    f"dice v{version} y `{tabla}` perdió claves únicas: "
                    f"{sorted(faltan_unicos)}")
    for tabla, esperadas in man.get("foreign_keys", {}).items():
        reales = {(r[3], r[2], r[4])
                  for r in con.execute(f"PRAGMA foreign_key_list({tabla})")}
        faltan_fks = esperadas - reales
        if faltan_fks:
            return ("indeterminada", version,
                    f"dice v{version} y `{tabla}` perdió referencias: "
                    f"{sorted(faltan_fks)}")
    for tabla, esperadas in man.get("foreign_key_groups", {}).items():
        grupos: dict[int, list[tuple[int, str, str, str]]] = {}
        for fk in con.execute(f"PRAGMA foreign_key_list({tabla})"):
            grupos.setdefault(int(fk[0]), []).append(
                (int(fk[1]), fk[2], fk[3], fk[4]))
        reales = set()
        for piezas in grupos.values():
            ordenadas = sorted(piezas)
            reales.add((tuple(p[2] for p in ordenadas), ordenadas[0][1],
                         tuple(p[3] for p in ordenadas)))
        if esperadas - reales:
            return ("indeterminada", version,
                    f"dice v{version} y `{tabla}` perdió FKs compuestas lane-locales")
    for tabla, fragmentos in man.get("checks", {}).items():
        fila = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (tabla,)).fetchone()
        ddl = "".join(((fila[0] if fila else "") or "").lower().split())
        faltan_checks = {frag for frag in fragmentos if frag not in ddl}
        if faltan_checks:
            return ("indeterminada", version,
                    f"dice v{version} y `{tabla}` perdió restricciones CHECK")
    for tabla, expected_sql in man.get("table_sql", {}).items():
        fila = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (tabla,)).fetchone()
        actual_sql = _sql_forma((fila[0] if fila else "") or "")
        if actual_sql != expected_sql:
            return ("indeterminada", version,
                    f"dice v{version} y `{tabla}` no conserva su DDL exacto")
    claves = {r[0] for r in con.execute("SELECT k FROM meta")}
    if man["meta"] - claves:
        return ("indeterminada", version,
                f"faltan claves de `meta`: {sorted(man['meta'] - claves)}")
    return ("conocida", version, "")


def _columnas(con: sqlite3.Connection, tabla: str) -> set:
    return {r[1] for r in con.execute(f"PRAGMA table_info({tabla})")}


def _existe(con: sqlite3.Connection, tabla: str) -> bool:
    # `COLLATE NOCASE`, no `name=?` a secas: SQLite resuelve nombres de tabla
    # sin distinguir mayúsculas de minúsculas (ASCII) para `CREATE TABLE IF
    # NOT EXISTS` y para toda referencia posterior — una `ADMISSION_HISTORY`
    # plantada y una `admission_history` del `SCHEMA` son EL MISMO objeto para
    # SQLite. Un `name=?` literal decía que no existía y dejaba una segunda
    # comprobación (P1, `_m5_a_6`) mirando por donde SQLite ya no mira.
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? COLLATE NOCASE",
        (tabla,)).fetchone() is not None


def _audita(f):
    """Envuelve una mutación autorizada para que su rechazo deje rastro.

    Centralizado y no repetido en cada `raise`: repartido por los `except` de
    cada método, el que se olvida no da error — simplemente deja un agujero de
    auditoría del que nadie se entera hasta que hace falta el registro.
    """
    @functools.wraps(f)
    def envoltorio(self, token, *a, **kw):
        self._consume_detalle_telemetria()
        # Los DTO operativos usan argumentos posicionales para sus ids. Convertirlos
        # aquí a nombres permite instrumentar sin pasar el payload completo al sensor.
        nombres = tuple(f.__code__.co_varnames[2:f.__code__.co_argcount])
        argumentos = dict(zip(nombres, a))
        argumentos.update(kw)
        try:
            resultado = f(self, token, *a, **kw)
        except JournalError as e:
            # El recibo del rechazo VIAJA CON EL ERROR: el gateway tiene que
            # poder citarlo sin volver a buscarlo, que es como se acaba citando
            # otro.
            e.receipt_id = self._auditar_rechazo(token, e)   # tras el ROLLBACK
            self._telemetria(f.__name__, token, argumentos, error=e)
            raise
        self._telemetria(f.__name__, token, argumentos, resultado=resultado)
        return resultado
    return envoltorio


def _sentencias(script: str):
    """Trocea el esquema en sentencias ejecutables una a una.

    Existe por `executescript()`: ese método COMITEA antes de correr el script,
    así que dentro de un `BEGIN IMMEDIATE` rompe la transacción en silencio —
    la migración parecería transaccional y no lo sería. Los comentarios se
    quitan antes de trocear porque un trozo que sólo tiene comentario no es una
    sentencia válida.
    """
    limpio = "\n".join(l for l in script.splitlines()
                       if not l.strip().startswith("--"))
    for trozo in limpio.split(";"):
        if trozo.strip():
            yield trozo


def _authenticate_locked(con: sqlite3.Connection, token: str,
                         now: float) -> "SessionView | None":
    """Resuelve un token sobre LA CONEXIÓN QUE SE LE PASA.

    Existe para que la validación pueda correr DENTRO del `BEGIN IMMEDIATE` de
    la mutación que autoriza. Ésa es la única posición en la que decide: hecha
    fuera, entre el «sí» y la escritura cabe un `reload_credential_map` o un
    `revoke_session` enteros, y la escritura operacional entra con una sesión que
    ya no vale. El chequeo de fuera puede quedarse, pero como OPTIMIZACIÓN —
    para no abrir transacción por una petición obviamente muerta—, nunca como
    decisión.

    Comprueba, en esta lectura: existencia · revocación · vencimiento ·
    generación del mapa. La generación se lee aquí mismo, no se recibe: recibirla
    permitiría autorizar contra una generación ya sustituida.
    """
    fila = con.execute("SELECT v FROM meta WHERE k='generation'").fetchone()
    gen = int(fila["v"]) if fila else 1
    row = con.execute(
        "SELECT s.*, p.principal, b.principal_source, b.capabilities"
        "  FROM runtime_sessions s"
        "  JOIN principals p USING (principal_id)"
        "  JOIN credential_bindings b ON b.principal_id=s.principal_id"
        "       AND b.retired_at IS NULL"
        " WHERE s.token_hash=?",
        (_sha256(token),)).fetchone()
    if row is None or row["revoked_at"] is not None:
        return None
    if row["expires_at"] <= now:
        return None
    if int(row["generation"]) != gen:
        return None
    return SessionView(row["runtime_instance"], row["principal_id"],
                       row["principal"], row["role"], row["lane"],
                       row["expires_at"], gen, row["principal_source"],
                       tuple(json.loads(row["capabilities"])))


def _difiere(actual: "Binding", spec: Mapping) -> bool:
    """¿La ligadura viva dice algo distinto de lo que pide el mapa?

    Las CAPACIDADES entran en la comparación: sin ellas, quitarle a alguien el
    permiso de operador se leía como «no ha cambiado nada» y la recarga no
    surtía efecto — la retirada más importante era justo la invisible.
    """
    esperado = "explicit" if spec.get("principal") else "derived_from_role"
    return (actual.principal != (spec.get("principal") or spec["role"])
            or actual.role != spec["role"]
            or actual.lane != spec["lane"]
            # `derived_from_role` -> `explicit` con el MISMO nombre es un cambio
            # real: deja de ser un valor supuesto y pasa a ser uno declarado. Sin
            # esto, la recarga que por fin nombra al principal se leía como
            # «nada que hacer» y la ligadura seguía marcada como derivada.
            or actual.principal_source != esperado
            or list(actual.capabilities) != sorted(set(spec.get("capabilities") or ())))


def _resolver_ligadura(con: sqlite3.Connection, ref: str) -> Binding | None:
    """Ligadura ACTIVA de un `credential_ref`, sobre la conexión que se le pasa.

    Toma la conexión a propósito: así el mismo código sirve para la lectura de
    fuera y para la revalidación DENTRO de una transacción. Una segunda función
    «igual pero transaccional» es la forma habitual de que sólo una de las dos
    reciba la próxima corrección.
    """
    row = con.execute(
        "SELECT b.principal_id, b.principal_source, b.generation, b.capabilities,"
        "       p.principal, p.role, p.lane "
        "  FROM credential_bindings b JOIN principals p USING (principal_id)"
        " WHERE b.credential_ref=? AND b.retired_at IS NULL", (ref,)).fetchone()
    if row is None:
        return None
    return Binding(ref, row["principal_id"], row["principal"], row["role"],
                   row["lane"], row["principal_source"], row["generation"],
                   tuple(json.loads(row["capabilities"])))


def _now_iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _req_hash(version: int, *, intent, ledger_raw, ledger_efectivo, causes,
              external_causes, fenced_resource, fencing_token) -> str:
    """Hash canónico de la petición, POR VERSIÓN de canonicalización.

    Las dos versiones necesitan un ledger DISTINTO, y ésa es la mitad del
    trabajo de esta función:

        v1 — la fórmula de `bcd05c5`. Se calculaba ANTES de derivar el destino
             (`coordination.py:1417` de aquel commit, frente a la derivación en
             `:1432`), así que hashea el `ledger` CRUDO del argumento, que podía
             ser `None`. Reconstruirla con el destino efectivo daría otro hash y
             convertiría cada reintento heredado en un `409`.
        v2 — se calcula tras derivar, sobre el destino EFECTIVO —para que
             `ledger=None` y su equivalente explícito coincidan— e incluye el
             par de fencing, sin el cual dos peticiones de igual cuerpo y vallas
             distintas compartían clave.

    Una versión que no sepamos reconstruir NO se reinterpreta: quien llama
    convierte eso en un conflicto declarado. Adivinar la fórmula histórica es
    peor que admitir que no se puede comprobar.
    """
    base = {"intent": dict(intent), "causes": list(causes),
            "external_causes": [dict(c) for c in external_causes]}
    if version == 1:
        return _sha256(_canonical({**base, "ledger": ledger_raw}))
    if version == 2:
        return _sha256(_canonical({**base, "ledger": ledger_efectivo, "v": 2,
                                   "fenced_resource": fenced_resource,
                                   "fencing_token": fencing_token}))
    raise ReplayUnverifiable(
        f"no sé reconstruir la canonicalización v{version}")


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ── EL NORMALIZADOR UNICO · A1-A6 ────────────────────────────────────────
# Sustituye a `_pesa_mas_de` + `_validar_estructura` + `_validar_cardinalidad`.
#
# 🔴 POR QUE UNO Y NO TRES. Los tres recorrian el objeto VIVO del llamante, y el
# canonico lo recorria una CUARTA vez para hashear y persistir. Cuatro lecturas
# de un objeto que el llamante sigue teniendo en la mano es un TOCTOU: un
# `items()`/`__iter__`/`__len__` hostil, un objeto mutable, u otro hilo, enseñan
# un documento al validador y otro al codificador — y lo que se hashea y se
# guarda no es lo que se valido. **Aqui se valida CONSTRUYENDO una copia
# privada, y de ahi en adelante nadie vuelve a mirar el vivo.**
# Es la doctrina que el fichero ya aplica en dos sitios —`str(a)` UNA vez en
# `JournalError.__init__`, `k = str(k_vivo)` en `_sin_atribucion`— y que los tres
# validadores rompian.

_INF = float("inf")

# Un `int` se renderiza a decimal para el canonico, y CPython corta esa
# conversion en `sys.get_int_max_str_digits()` (`4300` por defecto): por encima,
# `json.dumps` levanta un `ValueError` CRUDO. Ningun tope de bytes lo vuelve
# valido —es un limite del SERIALIZADOR— asi que es `422`, no `413`.
# `bits / 3.33 <= digitos`, asi que comparar por bits rechaza SOLO cuando los
# digitos superan el limite con seguridad.
def _int_irrenderizable(v: int) -> bool:
    return v.bit_length() * 10 > _MAX_DIGITOS_INT * 33


def _MAX_DIGITOS_INT_() -> int:
    f = getattr(sys, "get_int_max_str_digits", None)
    return f() if f else 4300


_MAX_DIGITOS_INT = _MAX_DIGITOS_INT_()


def _campo_raiz(donde: str) -> str:
    """El CAMPO CANONICO del que cuelga un camino de diagnostico.

    `_sin_atribucion` construye `donde` como `f"{donde}.{k}"` / `f"{donde}[{i}]"`
    partiendo SIEMPRE de un literal del contrato (`intent`, `payload`, `trace`),
    asi que el primer segmento ES el campo. Cortar por el primer `.` o `[` lo
    devuelve, y para un `donde` que ya sea el campo, se devuelve entero.

    🔴 POR QUE EXISTE (hallazgo `@qa` §4 sobre `bb5fad94`): la guarda de
    profundidad de `_sin_atribucion` construia `ResourceLimitExceeded` **directa,
    sin pasar por `_rle`**, y le metia el CAMINO en `field`
    (`intent.meta.n.n.n…`). Eso esta FUERA de `CAMPOS_CONTRATO_CORE`, que es un
    enum CERRADO, y convierte el `field` en cadena abierta — exactamente lo que
    el canon prohibe («`field` NO lleva indice de elemento») y lo que `_rle`
    existe para impedir. Latente en sano —el congelador corta antes— y VIVO
    justo en el escenario que justifica la redundancia.
    🩸 Y mi propio juez de alcanzabilidad lo CEMENTABA: `assert "." in field`
    exigia un valor que el enum prohibe. **La evidencia del veredicto y la
    divergencia eran la MISMA linea.**
    ⇒ El camino NO viaja: se RETIRA. No cabe en el presupuesto
    (`DETALLE_MAX=320`, ver la guarda) y, sobre todo, es INPUT DEL LLAMANTE:
    `field` + `dimension` + `seen_at_least` localizan el nodo en el cuerpo que el
    propio cliente envio. Lo que no puede viajar en un ATRIBUTO del contrato es
    un valor que el contrato no enumera; lo que no debe viajar en NINGUN sitio es
    el eco de las claves de quien llama.
    Retirada formal: `RULING @cto` sobre `d132f207`.

    🩸 EL PARRAFO DE ARRIBA SUSTITUYE A UNO FALSO, que afirmaba lo contrario —que
    la ruta sobrevivia dentro del mensaje— mientras el comentario de la guarda,
    en ESTE MISMO FICHERO, decia «el camino se RETIRA del mensaje». El fichero
    afirmaba `P` y `¬P`, y el que se lee primero al abrir la funcion era el falso.
    ⛔ La frase vieja NO se reproduce aqui, ni entrecomillada: `F-RUTA-5` grepea
    el FUENTE, asi que citarla la resucita para el falsador — lo comprobe
    poniendola y viendolo rojo. Un texto que se cita a si mismo entra en la
    poblacion que se esta midiendo.
    """
    corte = len(donde)
    for sep in (".", "["):
        i = donde.find(sep)
        if i != -1:
            corte = min(corte, i)
    return donde[:corte]


def _rle(dimension: str, campo: str, limite: int, visto: int, cola: str):
    """UN `ResourceLimitExceeded` con los campos del contrato en ATRIBUTOS.

    🔒 Y `field` se comprueba contra el enum EN DATO. No es paranoia: el `field`
    con indice (`causes[0]`) salio de aqui la primera vez, y un comentario que
    decia «enum cerrado» no lo impidio.
    """
    # 🔻 `if`/`raise`, NO `assert`: `python -O` BORRA los `assert`, y la guarda
    # del enum desapareceria justo en el modo con el que se despliega. Una
    # comprobacion que solo existe en desarrollo no es una guarda, es un
    # comentario que se ejecuta a veces.
    if campo not in CAMPOS_CONTRATO_CORE:
        # 🩸 Y PASA POR `_saneado`, como los dos `ValueError`. Al cambiar el
        # `assert` por `if`/`raise` meti un `raise` con f-string CRUDO fuera de
        # la jerarquia `JournalError` —la base no lo toca— con `campo`, que es
        # un ARGUMENTO, interpolado tal cual. Lo cazo el censo que ya existia
        # (`test_CENSO_ningun_raise_del_fichero_queda_FUERA_de_la_jerarquia`):
        # la garantia del detalle (una linea · ASCII imprimible · acotada) se
        # aplica a TODA salida, no solo a la jerarquia. La CLASE no cambia
        # —sigue siendo un fallo de programacion, no de dominio—; lo que cambia
        # es que el dato interpolado ya no viaja crudo.
        raise AssertionError(_saneado(
            f"`field={_saneado(campo, DETALLE_MAX // 4)}` fuera de "
            f"CAMPOS_CONTRATO_CORE: el enum es cerrado"))
    return ResourceLimitExceeded(
        f"code=RESOURCE_LIMIT_EXCEEDED dimension={dimension} field={campo} "
        f"limit={limite} seen_at_least={visto}: {cola}. `dimension` nombra UN "
        f"limite excedido, no necesariamente el unico ni el mayor: corregirlo "
        f"puede no bastar para que la peticion pase",
        dimension=dimension, field=campo, limit=limite, seen_at_least=visto)


def _congela_valor(v, campo, prof, c, ruta, prof_max, nodos_max, tope_bytes,
                   campo_nodos=None):
    """Un valor: valida y DEVUELVE su copia. Recursiva Y ACOTADA por el guard.

    🔑 La profundidad se decide en la PRIMERA linea, antes de tocar el valor, asi
    que la recursion no puede pasar de `prof_max + 1` marcos — **no hay
    `RecursionError` que tapar con un `except`**, que es el parche que
    `@contratosbik` tumbo. El recorrido anterior era iterativo por ese motivo;
    con el guard delante, la recursion es segura Y construye la copia al volver,
    que es lo que una pila LIFO hacia incomodo.
    """
    # ① PROFUNDIDAD — primero, siempre.
    if prof > prof_max:
        raise _rle("depth", campo, prof_max, prof,
                   "anida mas niveles de los que se inspeccionan, y lo que no "
                   "se mira entero no entra")
    # ② UN NODO POR VALOR. A5: ni dos por clave de diccionario ni cero por
    # elemento de lista — el conteo anterior cobraba `2N+1` a un dict y `N+1` a
    # una lista, asi que el `2.048` de entonces disparaba de hecho a `~1.024`
    # claves y el mismo documento costaba el doble por ser objeto que por ser
    # lista. `@cpo` fijo despues la UNIDAD en esos mismos terminos (`08:41`):
    # **1 nodo = 1 VALOR, la clave no cuenta** ⇒ `dict` y `lista` dan `N+1`.
    # El contador ya era ese; lo que cambia con `8.192` es solo el numero.
    c[0] += 1
    if c[0] > nodos_max:
        # 🔻 EL CAMPO DEL EXCESO DE NODOS ES `request` CUANDO EL CONTADOR CRUZA.
        # El presupuesto de nodos es UNO POR OPERACION (`@cpo` `09:47:16Z`), asi
        # que el exceso no pertenece a ningun campo: nombrar uno seria acusar al
        # ultimo que sumo, que es un accidente del orden de validacion.
        raise _rle("nodes", campo_nodos or campo, nodos_max, nodos_max + 1,
                   "demasiados nodos en la operacion: el contador es UNO por "
                   "peticion y atraviesa todos los campos")

    def suma(n):
        # A4: cota INFERIOR de bytes, acumulada. `bytes >= caracteres`, y `len()`
        # es O(1): **no se codifica nada**. El contador anterior llamaba a
        # `trozo.encode("utf-8")` sobre el trozo que `iterencode` emite, y un
        # string gigante sale de UNA pieza -> lo materializaba ENTERO y ademas
        # hacia una SEGUNDA copia en bytes.
        if tope_bytes is None:
            return
        c[1] += n
        if c[1] > tope_bytes:
            raise _rle("bytes", campo, tope_bytes, c[1],
                       "bytes del JSON canonico en UTF-8 (cota inferior: no se "
                       "codifica el cuerpo para medirlo)")

    # ③ WHITELIST DE TIPOS. Antes era un fallthrough: cualquier hoja que no
    # fuera Mapping/list/tuple llegaba viva a `json` y salia un `TypeError` CRUDO
    # —sin tipar, sin recibo, `500`—, que es la MISMA averia que `aab2080` acaba
    # de cerrar para los dos `ValueError`, reabierta por otra puerta.
    if v is None:
        suma(4)
        return None
    if v is True or v is False:
        # 🔻 `true` son 4 caracteres y `false` 5. Cobraba `5` a los dos: para
        # `True` era SOBREESTIMAR, y una cota inferior que sobreestima no es una
        # cota inferior — rechaza documentos que caben. Es el MISMO defecto que
        # ya me cazo mi falsador en la coma de la ultima clave.
        suma(4 if v is True else 5)
        return v
    if isinstance(v, str):
        suma(len(v) + 2)
        return v
    if isinstance(v, int):
        if _int_irrenderizable(v):
            raise OperationInvalid(
                f"el campo {campo} lleva un entero de mas de "
                f"{_MAX_DIGITOS_INT} digitos: el serializador no lo puede "
                f"rendir, asi que no se puede ni hashear ni guardar")
        suma(max(1, v.bit_length() // 4))
        return v
    if isinstance(v, float):
        # `NaN`/`Infinity` los escribe `json` TAL CUAL, y eso **no es JSON
        # valido**: se estaria hasheando y persistiendo un canonico que ningun
        # parser conforme vuelve a leer.
        if not (-_INF < v < _INF):
            raise OperationInvalid(
                f"el campo {campo} lleva un float no finito (NaN/Infinity): el "
                f"canonico dejaria de ser JSON valido y el hash cubriria algo "
                f"que nadie puede volver a parsear")
        suma(len(repr(v)))
        return v
    if isinstance(v, Mapping):
        # A2 · CICLO = reaparecer en la RUTA A LA RAIZ, no en el recorrido. El
        # `vistos` global de antes nunca se vaciaba, asi que un DAG —el mismo
        # dict referenciado dos veces en posiciones HERMANAS— salia rechazado
        # como «referencia circular» siendo perfectamente serializable.
        ident = id(v)
        if ident in ruta:
            raise OperationInvalid(
                f"el campo {campo} tiene una referencia circular: no se puede "
                f"serializar a JSON canonico, asi que no se puede ni medir ni "
                f"hashear")
        ruta.add(ident)
        suma(2)
        salida = {}
        primero = True
        for k, hijo in v.items():
            if not isinstance(k, str):
                # `json` COERCE las claves no-str: `{1: x}` y `{"1": x}`
                # canonicalizan IGUAL -> dos documentos distintos, mismo hash,
                # misma clave de idempotencia. Y con claves de tipos mixtos,
                # `sort_keys=True` revienta al comparar.
                raise OperationInvalid(
                    f"el campo {campo} tiene una clave que no es texto "
                    f"({type(k).__name__}): el canonico la coercionaria y dos "
                    f"documentos distintos compartirian hash")
            # 🩸 COTA INFERIOR DE VERDAD, y me lo caza mi propio falsador: cobraba
            # `len(k) + 4` —una coma por CADA clave, incluida la ULTIMA— y eso
            # SOBREESTIMA. Un acumulador que sobreestima no es una cota inferior:
            # RECHAZA documentos que si caben, y el primero que tumbo fue el de
            # `N` EXACTO, o sea la frontera inclusiva que `@cpo` adjudico.
            # `"k":` son `len(k) + 3`; la coma solo existe a partir del SEGUNDO.
            suma(len(k) + 3 if primero else len(k) + 4)
            primero = False
            salida[k] = _congela_valor(hijo, campo, prof + 1, c, ruta,
                                       prof_max, nodos_max, tope_bytes,
                                       campo_nodos)
        ruta.discard(ident)
        return salida
    if isinstance(v, (list, tuple)):
        ident = id(v)
        if ident in ruta:
            raise OperationInvalid(
                f"el campo {campo} tiene una referencia circular")
        ruta.add(ident)
        suma(2)
        salida = []
        primero = True
        for hijo in v:
            # A5-bis · EL GUARD VA DENTRO DEL BUCLE. La version anterior apilaba
            # TODOS los elementos antes de que el guard viera el siguiente `pop`:
            # una lista de `10^6` metia `10^6` tuplas en la pila -> cientos de MB
            # para rechazar. Aqui el conteo de nodos corta en el elemento
            # `nodos_max`, no en el ultimo.
            if not primero:
                suma(1)          # la coma, misma correccion que en el mapa
            primero = False
            salida.append(_congela_valor(hijo, campo, prof + 1, c, ruta,
                                         prof_max, nodos_max, tope_bytes,
                                         campo_nodos))
        ruta.discard(ident)
        return salida
    raise OperationInvalid(
        f"el campo {campo} lleva un valor de tipo {type(v).__name__}, que no "
        f"es JSON: ningun tope lo vuelve valido")


def _congelar(obj, campo, *, tope_bytes, prof_max=None, nodos_max=None,
              nodos=None, agregado=None, nulo_es_valor=False,
              obligatorio=False, n_out=None):
    """EL normalizador. UNA pasada que valida Y devuelve el SNAPSHOT privado.

    Devuelve `dict`/`list`/escalares planos —copia profunda que **nadie mas
    referencia**—. La propiedad que importa no es la inmutabilidad frente a
    nosotros: es que el llamante ya no puede cambiar lo que vamos a hashear y a
    guardar.

    `tope_bytes=None` ⇒ **sin eje de bytes**: valida tipos, ciclo, profundidad y
    nodos, y copia. Hoy lo usa UNA sola superficie —`detail`, que esta FUERA de
    la politica de rechazo por tamaño— y no debe usarlo ninguna otra: el hueco
    `B5` (elementos de `causes`/`external_causes` sin eje de bytes) lo CERRO
    `B3` con `ELEMENTO_MAX_BYTES`, y su default es fail-closed.

    🔻 `nodos` · EL CONTADOR COMPARTIDO (B1). Lista de UN elemento que sobrevive
    entre llamadas: se siembra el contador con lo ya gastado y se devuelve lo
    gastado al salir.
    🔴 LA UNIDAD DEL PRESUPUESTO ES **UN Event O UN Command**, no «una peticion»
    en abstracto. Lo crean las DOS puertas de mutacion, una vez cada una, y lo
    comparten TODOS los campos controlados por cliente de esa puerta:
        · `accept_event` .... `intent` · `trace` · `causes` · `external_causes`
        · `submit_command` .. `payload` · `causes` · `external_causes`
    ⛔ PROSA CORREGIDA — la anterior decia que `intent`, `payload` y `trace`
    tenian «presupuesto propio», y es FALSO medido en los call-sites: los tres
    reciben el COMPARTIDO. Lo que la lista describia no era el contrato sino la
    firma por defecto, y leerla al reves invita a quitar el `nodos=` de una
    puerta creyendo que se respeta el diseño.
    🔻 `None` ⇒ contador AUTONOMO, y eso solo pasa cuando el helper se llama
    AISLADO —fuera de la construccion de un Event o un Command—: `attestation`
    (constructor de `Journal`), `annotations` (`open_session`) y `detail`
    (transicion de comando). No comparten porque no forman parte de la mutacion
    de ninguna de las dos puertas, no porque el contrato les de techo propio.
    🔴 POR QUE EXISTE, y es un defecto MIO que `@qa` midio y `@cpo` convirtio en
    regla: el acumulador era FRESCO EN CADA LLAMADA, y `_congelar_secuencia`
    llama una vez POR ELEMENTO ⇒ el techo agregado era
    `cardinalidad x nodos_max` = **`256 x 8.192 = 2.097.152` nodos**, no `8.192`.
    Un contador que se reinicia por elemento **no acota nada agregado**.
    `@cpo` (`08:52`): *«el techo compuesto se cuenta UNA VEZ por peticion»* — y
    lo dijo con la frase que importa: **«esa es la CONDICION, no el numero»**.
    ⚠️ Su «peticion» se INSTANCIA aqui como **un Event o un Command**: es la
    unidad que el nucleo construye, y la unica sobre la que este contador tiene
    principio y fin.
    ⚠️ Se comparten los NODOS, **no los BYTES**: el eje de bytes esta declarado
    POR ELEMENTO (`256` elementos ∧ `4 KiB` CADA UNO), asi que acumularlo entre
    elementos convertiria `4 KiB` por elemento en `4 KiB` en total, que es otro
    contrato y ademas mas apretado que el adjudicado.
    """
    # 🔻 `None` · LAS DOS COSAS QUE SE LLAMAN IGUAL Y NO SON LA MISMA.
    #   · CAMPO AUSENTE (`trace=None`): NO se emite en el canonico ⇒ no es un
    #     valor, no cuenta nodo y no suma bytes. Cobrarlo seria cobrar por lo
    #     que no se manda.
    #   · ELEMENTO NULO (`causes=[None]`): el canonico SI emite `null` ⇒ **es un
    #     valor y cuenta como nodo**, unidad `@cpo` (`1 nodo = 1 VALOR`).
    # 🩸 Sin esta distincion, un `None` de elemento salia GRATIS: `_congelar`
    # retornaba ANTES del contador, asi que una secuencia de nulos no gastaba ni
    # un nodo y solo la cardinalidad la paraba.
    if obj is None:
        if obligatorio:
            # 🔻 `intent` y `payload` son OBLIGATORIOS. `None` ahi no es «campo
            # ausente»: es un cuerpo que falta, y colarlo como ausencia gratis
            # dejaba pasar una mutacion SIN objeto de trabajo, sin gastar un
            # nodo ni un byte. Es defecto de FORMA -> `422`: ningun tope lo
            # vuelve valido.
            raise OperationInvalid(
                f"el campo {campo} es obligatorio y llego nulo: una mutacion "
                f"sin cuerpo de trabajo no es una mutacion pequeña, es otra cosa")
        if not nulo_es_valor:
            return None
    # D5 · UNA SOLA VERDAD para la profundidad: se deriva de `Journal`, no se
    # repite el literal. `Journal` existe en globals cuando esto se llama.
    prof_max = Journal._PROFUNDIDAD_MAX if prof_max is None else prof_max
    nodos_max = NODOS_MAX if nodos_max is None else nodos_max
    # 🧊 `c[0]` NODOS —sembrado con lo ya gastado si el presupuesto es
    # compartido—; `c[1]` BYTES, SIEMPRE desde cero: es un eje por elemento.
    c = [nodos[0] if nodos is not None else 0, 0]
    # (1) El exceso de un contador COMPARTIDO no pertenece a ningun campo.
    snap = _congela_valor(obj, campo, 0, c, set(), prof_max, nodos_max, tope_bytes,
                          "request" if nodos is not None else None)
    if nodos is not None:
        nodos[0] = c[0]              # lo gastado vuelve al presupuesto comun
    if tope_bytes is not None:
        # A4-bis · EL EXACTO, y solo AQUI. Corre sobre el snapshot YA acotado en
        # nodos y con la cota inferior de caracteres bajo el tope, asi que el
        # coste esta acotado por el TOPE y no por lo que mande quien ataca.
        # Hace falta para que la frontera `N`/`N+1` siga siendo EXACTA.
        n = len(_canonical(snap).encode("utf-8"))
        if n > tope_bytes:
            raise _rle("bytes", campo, tope_bytes, n,
                       "bytes del JSON canonico en UTF-8")
        if n_out is not None:
            n_out[0] = n
        if agregado is not None:
            # PRECEDENCIA · el agregado SUMA aqui pero NO decide aqui: se
            # comprueba AL FINAL, cuando ya han pasado forma y limites locales.
            agregado.campo(campo, n)
    return snap


def _payload_de_recibo(detail):
    """El `payload` del sobre de una transicion. **TRUNCA, NO RECHAZA.**

    🔻 CORRECTIVO. Yo habia metido `detail` en la politica de rechazo con
    `4 KiB`, y contradice `MARK:cto-detail-trunca-y-dimension-no-es-funcion-del-
    canonico` — ratificado por el operador. **`detail` es DIAGNOSTICO, no dato de
    trabajo**: truncar un diagnostico conserva lo util; rechazar la operacion
    **por el tamaño de su explicacion** convierte un campo accesorio en un motivo
    de fallo, y cuerpos que hoy funcionan pasarian de «te lo corto y sigo» a
    «te lo rechazo».

    🩸 Y MI PARTE: yo argumente que la exencion se apoyaba en medir el detalle de
    ERROR (`_saneado`, `320`) y no el de TRANSICION (el `Mapping` persistido).
    El operador cerro la pregunta: **los dos truncan**. Lo aplico entero.

    El corte SE DELATA por partida doble —la clave cambia a `payload_truncado` y
    el valor lleva `...(trunc)`—, que es la mitad del hueco que `@qa` midio: un
    truncado mudo miente por omision, uno marcado no. `_saneado` devuelve ASCII
    imprimible, asi que `320` caracteres son `320` bytes.
    """
    if not detail:
        return {"payload": {}}
    crudo = _canonical(detail)
    if len(crudo) <= DETALLE_MAX:
        return {"payload": detail}
    return {"payload_truncado": _saneado(crudo, DETALLE_MAX)}


def _congelar_secuencia(seq, campo, *, tope=CARDINALIDAD_MAX,
                        tope_bytes=ELEMENTO_MAX_BYTES, nodos=None,
                        agregado=None):
    """`causes` y `external_causes`: cardinalidad, y snapshot de los elementos.

    🔴 A6 · NI `len()` NI CONSUMIR. La version anterior hacia
    `len(s) if hasattr(s,"__len__") else sum(1 for _ in s)`:
      · un `__len__` que miente (devuelve `0`, itera `10^6`) pasaba el control;
      · un GENERADOR se AGOTABA contandolo, y las dos iteraciones de despues
        —validacion de forma y persistencia— veian una secuencia VACIA: las
        causas desaparecian en silencio, cero filas y cero error;
      · un generador INFINITO colgaba el proceso dentro del `sum`.
    `islice(iter(seq), tope + 1)` materializa **como mucho `tope+1`**: no confia
    en `__len__`, no agota nada que importe, y no puede colgar.
    """
    if seq is None:
        # 🔻 `None` NO SE NORMALIZA A `[]`. La firma es `Sequence`, y `None` no
        # es una secuencia vacia: es OTRA COSA. Normalizarlo en silencio hacia
        # tres daños a la vez —el cliente creia haber mandado causas y no las
        # mandaba, el campo desaparecia del AGREGADO sin decirlo, y un `None`
        # por error de serializacion pasaba por «sin causas»—. Es defecto de
        # FORMA: ningun tope lo vuelve valido ⇒ `422`.
        # ⚠️ Y `[]` SI es valido y NO es lo mismo: cuenta su raiz (1 nodo) y
        # aporta `[]` al objeto exterior. La diferencia entre los dos tiene su
        # falsador explicito.
        raise OperationInvalid(
            f"el campo {campo} llego nulo: se espera una secuencia (`[]` para "
            f"«ninguna»), y `None` no es una secuencia vacia")
    corte = []
    for x in iter(seq):
        corte.append(x)
        if len(corte) > tope:
            raise _rle("cardinality", campo, tope, tope + 1,
                       "cada elemento es una FILA durable")
    # 🔻 B3 · CADA ELEMENTO PASA POR EL VALIDADOR ENTERO, con su eje de bytes.
    # Antes se llamaba con `tope_bytes=None` desde los cuatro call-sites, asi que
    # `suma()` era NO-OP y el pase exacto se saltaba: la secuencia tenia
    # cardinalidad y NADA de tamaño.
    # 🔻 B1 · Y EL TECHO DE NODOS SE CUENTA UNA VEZ. `nodos=None` ⇒ se crea aqui,
    # asi que incluso una llamada AISLADA comparte entre SUS elementos; el
    # llamante que pasa el suyo lo comparte ademas con la OTRA secuencia y con
    # `intent`/`payload`/`trace` del MISMO Event o Command. Ese `[0]` es el
    # presupuesto: no se reinicia por elemento ni por superficie.
    if nodos is None:
        nodos = [0]
    # 🔻 (2) LA RAIZ DE LA LISTA ES UN NODO. El canonico emite `[...]`, o sea un
    # VALOR, y la unidad de `@cpo` es «1 nodo = 1 VALOR». Se contaba `0`: una
    # secuencia salia gratis como contenedor y solo pagaban sus elementos.
    nodos[0] += 1
    if nodos[0] > NODOS_MAX:
        raise _rle("nodes", "request", NODOS_MAX, NODOS_MAX + 1,
                   "demasiados nodos en la operacion: el contador es UNO por "
                   "peticion y atraviesa todos los campos")
    # 🔻 `field` ES UN ENUM CERRADO: `causes` | `external_causes`. Pasaba
    # `f"{campo}[{i}]"`, o sea que el atributo del contrato llevaba el INDICE
    # dentro y el conjunto de valores posibles era INFINITO — un cliente no
    # puede ramificar sobre eso. El indice es DIAGNOSTICO y vive en la prosa.
    salida = []
    propio = [0]                      # bytes canonicos de ESTE campo
    uno = [0]                         # bytes del elemento en curso
    for i, x in enumerate(corte):
        try:
            salida.append(_congelar(x, campo, tope_bytes=tope_bytes,
                                    nodos=nodos, n_out=uno,
                                    nulo_es_valor=True))
            propio[0] += uno[0]
        except ResourceLimitExceeded as e:
            # 🩸 El envoltorio PISABA `request` con el nombre del campo. Existe
            # para forzar el enum en el exceso DE ELEMENTO —ahi el `field` es la
            # secuencia—, pero el exceso del contador COMPARTIDO no pertenece a
            # ningun campo y ya venia bien nombrado. Curar una direccion y
            # romper la otra en la misma linea: lo caza el falsador `①`.
            cruzado = e.field == "request"
            raise _rle(e.dimension, "request" if cruzado else campo,
                       e.limit, e.seen_at_least,
                       "el contador es UNO por peticion y atraviesa todos los "
                       "campos" if cruzado
                       else f"en el elemento {i} de la secuencia") from None
        except OperationInvalid as e:
            raise OperationInvalid(
                f"{e} (elemento {i} de {campo})") from None
    if agregado is not None:
        # El canonico de la LISTA es `[` + elementos + `,`*(n-1) + `]`, y la
        # secuencia entra en el objeto exterior como UN campo con su nombre.
        agregado.campo(campo, propio[0] + 2 + max(0, len(salida) - 1))
    return salida


class _AgregadoPeticion:
    """Mide el **OBJETO LOGICO EXTERIOR COMPLETO**, no la suma de sus valores.

    🩸 Sumar los canonicos de los campos **no es el canonico de la peticion**:
    se dejaba fuera el NOMBRE de cada campo, sus comillas, los dos puntos, las
    comas entre campos y las llaves exteriores. Un cliente que hiciera la cuenta
    por su lado sacaba otro numero que el servidor, y **un limite que el cliente
    no puede reproducir no es un contrato, es una loteria** — la misma frase que
    ya obligo a medir en BYTES del canonico y no en caracteres.

        {"causes":[...],"intent":{...},"trace":{...}}
        ^                ^^^^^^^^^^                 ^
        llaves           nombre + comillas + `:`    comas

    Exacto y en UN solo recorrido: cada campo aporta `len(nombre) + 3 + n` al
    entrar —`"` `"` `:`— y el total añade las llaves y las comas al leerse.
    No se vuelve a canonicalizar nada: `n` ya lo calculo el pase exacto de su
    campo, que corre sobre un snapshot ya acotado.
    """

    __slots__ = ("_bytes", "_campos")

    def __init__(self):
        self._bytes = 0
        self._campos = 0

    def campo(self, nombre: str, n: int) -> None:
        self._bytes += len(nombre) + 3 + n
        self._campos += 1

    @property
    def total(self) -> int:
        return 2 + self._bytes + max(0, self._campos - 1)


def _exige_agregado(agregado, campo="request",
                    tope=CANONICAL_REQUEST_MAX_BYTES) -> None:
    """EL AGREGADO CANONICO, y va **AL FINAL**. Es el ultimo peldaño de la
    precedencia publicada:

        ① FORMA .................. tipo JSON, clave de texto, ciclo, profundidad
                                   y nodos. Lo que ningun tope vuelve valido cae
                                   aqui, y cae como `422`.
        ② LIMITES LOCALES ........ el presupuesto de CADA campo y de CADA
                                   elemento, mas la cardinalidad de la secuencia.
        ③ AGREGADO CANONICO ...... la suma de TODOS los campos controlados por
                                   cliente de esta mutacion normalizada.

    🔑 EL ORDEN NO ES ESTETICO, y es el mismo argumento que ya fijo el de
    profundidad-antes-que-bytes: **el agregado PRESUPONE que cada parte ya esta
    acotada.** Sumar primero obligaria a canonicalizar campos que la forma va a
    rechazar de todos modos — trabajo por cuenta de quien ataca. Y al reves, un
    cliente que recibe el agregado sabe que sus campos son individualmente
    validos y que lo que sobra es el TOTAL, que es un arreglo distinto.
    """
    if agregado is not None and agregado.total > tope:
        raise _rle("bytes", campo, tope, agregado.total,
                   "bytes del JSON canonico del OBJETO EXTERIOR de esta "
                   "mutacion, nombres de campo y framing incluidos")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _control_id(value: Any, field_name: str, *, maximum: int = 160) -> str:
    """Identificadores de control acotados; nunca aceptan prosa/controles."""
    if (type(value) is not str or not value or len(value) > maximum
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+@-]*", value) is None):
        raise OperationInvalid(
            f"{field_name} debe ser un identificador ASCII acotado")
    return value


class Journal:
    """Estado durable de coordinación. Una instancia = una conexión.

    `pepper` NO se guarda en la base: sólo su comprobante. Sin pepper no se
    puede resolver una credencial, que es justo lo que se quiere — el mapa de
    credenciales sigue siendo del despliegue, no de la base.
    """

    def __init__(self, path: str, *, pepper: bytes | str,
                 lane_ledgers: Mapping[str, Sequence[str]] | None = None,
                 max_attempts: int = 5,
                 backoff_base_s: float = 2.0,
                 backoff_cap_s: float = 300.0,
                 busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
                 attestation: Mapping[str, Any] | None = None,
                 denial_quota: int = DEFAULT_DENIAL_QUOTA,
                 denial_bucket_s: int = DEFAULT_DENIAL_BUCKET_S,
                 unknown_rows_max: int = DEFAULT_UNKNOWN_ROWS_MAX,
                 fingerprint_rotation_s: int = DEFAULT_FP_ROTATION_S,
                 sensor_factory=None,
                 recipient_resolver: "Callable[[str], tuple[str | None, bool]] | None" = None,
                 grammar: "Grammar | None" = None,
                 clock=time.time):
        if not pepper:
            raise ValueError("pepper obligatorio: sin él `credential_ref` no es opaco")
        self.path = path
        self._pepper = pepper.encode() if isinstance(pepper, str) else pepper
        self._busy_timeout_ms = int(busy_timeout_ms)
        # La atestación la pone el SERVIDOR. Se guarda tal cual y la petición no
        # puede elegirla (ADR-001 falsador 26): `accept_event` la ignora si viene
        # dentro del `intent`.
        # ── `attestation` · CONFIGURACION DE ARRANQUE, no peticion ────────
        # Mismo validador, pero el regimen es **R3: sensor, SIN denial**. Aqui no
        # hay sesion ni principal —esto corre en `__init__`, antes de que exista
        # nadie— asi que no hay a quien colgarle un recibo: colgarselo a alguien
        # seria inventar un sujeto. El operador se entera por la EXCEPCION, que
        # es el sensor, y el proceso no arranca con una atestacion sin acotar.
        # 🔻 A9 · `dict(attestation or {})` era una copia SUPERFICIAL: los
        # valores anidados seguian siendo los del llamante, asi que lo validado
        # y lo usado podian divergir en cuanto alguien mutara su objeto. El
        # snapshot es copia PROFUNDA y privada.
        self._attestation = _congelar(attestation, "attestation",
                                      tope_bytes=METADATO_MAX_BYTES) or {}
        self._denial_quota = int(denial_quota)
        self._denial_bucket_s = int(denial_bucket_s)
        self._unknown_rows_max = int(unknown_rows_max)
        self._fingerprint_rotation_s = int(fingerprint_rotation_s)
        self._clock = clock
        # Inyectado por el gateway, nunca por la petición. Recibe una SessionView ya
        # autenticada y devuelve un Sensor M3 ligado al mismo Resource.
        self._sensor_factory = sensor_factory
        self._telemetry_local = threading.local()
        # Monótono durante la vida del proceso: si un rechazo no pudo dejar su
        # recibo, readiness permanece cerrado hasta un reinicio limpio. Un
        # fallo operacional no puede curarse sólo porque la siguiente escritura
        # sí funcione.
        self._audit_suspended = threading.Event()
        # CENSO INYECTADO, no importado. `coordination` no conoce `ledger_parse`
        # ni `servicio`: pregunta por un literal y recibe `(rol|None, difusión)`.
        # `None` NO es «todo vale»: es fail-closed — ver `_resolver_destinos`.
        self._recipient_resolver = recipient_resolver
        self._grammar = grammar
        self._read_only = False
        # ALLOWLIST carril -> ledgers. `None` NO significa «todo permitido»:
        # significa que ningún carril puede escribir. Un destino es una
        # superficie de escape entre carriles, y una allowlist que por defecto
        # deja pasar todo protege exactamente hasta que a alguien se le olvida
        # configurarla — o sea, no protege.
        self._lane_ledgers = {k: tuple(v) for k, v in (lane_ledgers or {}).items()}
        self._max_attempts = int(max_attempts)
        self._backoff_base_s = float(backoff_base_s)
        self._backoff_cap_s = float(backoff_cap_s)
        # UNA CONEXIÓN POR HEBRA. `check_same_thread=False` sobre una conexión
        # compartida no es una optimización: dos hebras dentro del MISMO
        # `BEGIN IMMEDIATE` se mezclan las sentencias, y el `COMMIT` de una
        # cierra la transacción a medio escribir de la otra. La atomicidad de
        # `accept_event` dejaría de existir sin que nada diera error.
        self._local = threading.local()
        # ¿EXISTÍA el fichero ANTES de que lo abriéramos? Se pregunta aquí porque
        # `sqlite3.connect` lo CREA, y después ya no se puede distinguir «venía
        # vacío» de «lo acabo de crear yo». Un fichero de cero bytes que ya
        # estaba puede ser de cualquiera —un `touch` de un despliegue, un volumen
        # a medio montar, un truncado— y no hay nada dentro que permita afirmar
        # que es nuestro. Crear se autoriza cuando NO existía; adoptar, nunca.
        self._preexistia = os.path.exists(path)
        self._identidad = None        # (dev, inode) latido tras clasificar
        self._veredicto = None        # ("conocida"|"futura"|"corrupta"|…, version, detalle)
        self._snapshot = None         # copia RECUPERADA que se sirve si el original no
        self._tmpdir = None
        # CLASIFICADO NO ES INICIALIZADO. Saber qué es un fichero no autoriza a
        # abrirlo para escribir ni permite decir que el journal está listo: entre
        # las dos cosas está la migración, que puede no haber corrido.
        self._estado = "NUEVO"
        self._inventario_clasificado = None
        self._limpiador = None
        self._degradacion_pendiente = False
        # Otra instancia puede restaurar la ruta con ``os.replace`` mientras ésta
        # conserva conexiones thread-local al inode anterior. La detección ocurre
        # bajo SH, donde NO se puede tomar `_mutex` (initialize usa mutex→EX): sólo
        # se marca un evento idempotente y se cierra el handle de ESTA hebra. La
        # transición global de época/estado se hace en `initialize()`, ya fuera de
        # cualquier operación y bajo `_mutex`.
        self._identidad_invalidada = threading.Event()
        self._tmpdir_previo = None
        # ÉPOCA. Una conexión cacheada es thread-local, así que `dispose()` o una
        # re-foto del hilo principal NO la tocan: el worker seguía leyendo por un
        # handle cuyo respaldo ya no existe —o peor, cuya copia ya se borró— y no
        # tenía forma de enterarse. El número sube en cada acto que invalida lo
        # cacheado; quien devuelve una conexión guardada compara primero.
        self._epoca = 0
        # Reentrante porque las consultas de ciclo (`health`, versión previa)
        # delegan en helpers que también necesitan congelar el estado del objeto.
        self._mutex = threading.RLock()

    # ── conexión y PRAGMAs ───────────────────────────────────────────────────
    def _preflight(self) -> None:
        """Clasifica el fichero SIN abrirlo con SQLite. Una vez por `Journal`.

        ⚠️ El estado del disco se vuelve a mirar AQUÍ y bajo el cerrojo, no en
        `__init__`. `self._preexistia` era una foto tomada al construir el
        objeto, y entre construir y usar cabe todo: otro actor crea el fichero, y
        con la foto vieja saltábamos la clasificación entera y mutábamos el
        original. Una respuesta sobre el disco caduca en cuanto se guarda.
        """
        if self._veredicto is not None:
            return
        # La ausencia se comprueba SIN cerrojo: crear el fichero del cerrojo
        # para averiguar que no hay base es dejar un artefacto donde no había
        # nada — exactamente lo que este preflight existe para no hacer.
        if _stat_seguro(self.path) is None:
            self._veredicto = ("nueva", None, "la ruta no existe")
            return
        with self._cerrojo_ciclo(opcional=True):
            if self._veredicto is not None:
                return
            if _stat_seguro(self.path) is None:
                self._veredicto = ("nueva", None, "la ruta no existe")
                return
            self._preexistia = True
            self._clasificar_bajo_cerrojo()

    def _clasificar_bajo_cerrojo(self) -> None:
        # TODO EN LOCALES Y COMMIT AL FINAL. Publicando `self._tmpdir` antes de
        # tener veredicto, un fallo de SQLite o de `_clasificar` dejaba un
        # temporal a medias, ya visible en el objeto y sin finalizador. Y no
        # pasaba sólo en la re-foto: el PRIMER preflight tiene el mismo camino.
        self._tmpdir_previo = self._tmpdir
        nuevo_tmp = tempfile.mkdtemp(prefix="llminbox-preflight-")
        os.chmod(nuevo_tmp, 0o700)
        self._tmpdir = nuevo_tmp
        # LIBERACIÓN AUTOMÁTICA. `dispose()` es el acto explícito, pero no todo
        # el mundo lo llama —media base de código cierra con `close()`— y cada
        # copia es una réplica ENTERA del journal. Atada al ciclo de vida del
        # objeto, la copia se va cuando se va su dueño, lo llame quien lo llame.
        # El finalizador NO captura `self`: capturarlo impediría el GC y la fuga
        # que dice cerrar sería permanente.
        nuevo_dir = nuevo_tmp
        try:
            inv = _foto_estable(self.path, nuevo_dir)
        except BaseException:
            # La foto FALLÓ: no hay copia que custodiar. Armar el finalizador
            # antes era atar el ciclo de vida del objeto a un directorio que no
            # llegó a contener nada, y dejar el anterior —el que SÍ vale— sin
            # dueño. Se limpia lo nuevo y se conserva lo viejo intacto.
            # El viejo sigue con su dueño: no se desenganchó nada todavía.
            self._tmpdir = None
            shutil.rmtree(nuevo_dir, ignore_errors=True)
            raise
        # ⚠️ EL VIEJO NO SE SUELTA TODAVÍA. Desengancharlo aquí —con la foto ya
        # buena pero la CLASIFICACIÓN aún por hacer— dejaba el directorio
        # anterior sin dueño: si `_clasificar` fallaba después, `dispose()`
        # borraba el nuevo y el viejo quedaba fugado. La instalación se cierra
        # ABAJO, cuando ya no puede fallar nada.
        # El directorio anterior se GUARDA, no se deduce. `weakref.finalize` no
        # expone `.args` —comprobado: `hasattr(f, "args") is False`—, así que el
        # `getattr(..., (None,))[0]` de antes devolvía SIEMPRE `None` y la copia
        # vieja se quedaba en disco en el camino de ÉXITO. Un defecto mudo: el
        # `getattr` con defecto no falla, contesta.
        viejo_limpiador, viejo_dir = self._limpiador, self._tmpdir_previo
        identidad_local = inv[""][0][:2]                 # (dev, inode) LATIDO
        # Inventario COMPLETO con el que se clasificó: conjunto de piezas,
        # (dev, inode), metadatos y sha de cada una. Es el testigo que permite
        # decir «esta foto SIGUE siendo la de ahora» sin volver a copiar.
        inventario_local = inv
        copia = os.path.join(self._tmpdir, os.path.basename(self.path))
        # La copia SÍ se abre RW: ahí es donde se recupera el WAL o se reproduce
        # un hot journal, que es justo lo que no puede pasarle al original.
        try:
            con = sqlite3.connect(copia, timeout=self._busy_timeout_ms / 1000.0,
                                  isolation_level=None)
            try:
                con.execute("PRAGMA foreign_keys=ON")
                veredicto_local = _clasificar(con)
            finally:
                con.close()
        except BaseException:
            # Falló SQLite o la clasificación DESPUÉS de una foto buena: el
            # temporal a medias se va aquí y el objeto queda como estaba.
            self._tmpdir = self._tmpdir_previo
            shutil.rmtree(nuevo_tmp, ignore_errors=True)
            raise
        # La copia se guarda SIEMPRE: es lo que se sirve cuando el original no
        # se puede abrir, sea porque no es nuestro o porque no se deja escribir.
        # COMMIT ATÓMICO: hasta aquí no se ha publicado NADA en el objeto salvo
        # `_tmpdir`, que el rollback del llamante ya sabe limpiar. Ahora se
        # asigna todo junto, y ya no puede fallar nada en medio.
        self._identidad = identidad_local
        self._inventario_clasificado = inventario_local
        self._veredicto = veredicto_local
        self._snapshot = copia
        # COMMIT de la instalación: pasado este punto no queda nada que pueda
        # fallar, así que ahora —y sólo ahora— el nuevo sustituye al viejo.
        if viejo_limpiador is not None:
            viejo_limpiador.detach()
        self._limpiador = weakref.finalize(self, shutil.rmtree, nuevo_dir,
                                           ignore_errors=True)
        if viejo_dir and viejo_dir != nuevo_dir:
            shutil.rmtree(viejo_dir, ignore_errors=True)

    def _verificar_identidad(self) -> None:
        """El objeto de la ruta tiene que seguir siendo el que clasificamos.

        Se comprueba en CADA conexión nueva —también las de otras hebras—: entre
        el preflight y el segundo hilo cabe un reemplazo por `rename`, y el
        tamaño puede coincidir. Un reemplazo exige `Journal` nuevo, no una
        reconexión silenciosa al fichero de otro.
        """
        if self._identidad is None:
            return
        st = _stat_seguro(self.path)
        if st is None or (st.st_dev, st.st_ino) != self._identidad:
            raise IdentityChanged(
                f"la ruta ya no apunta al fichero que clasifiqué "
                f"({self._identidad} → {None if st is None else (st.st_dev, st.st_ino)})")

    # ESTADOS, y son exactamente estos:
    #   NUEVO            — no se sabe nada; no se abre nada.
    #   CLASIFICADO      — el preflight tiene veredicto. NO autoriza a operar.
    #   READY            — inicializado, pepper validado, versión actual, RW.
    #   READ_ONLY_READY  — pepper validado y versión actual, pero no se puede
    #                      escribir. Sirve lecturas operacionales, nada más.
    # «Clasificado» y «listo» eran lo mismo y ése era el agujero: saber qué es un
    # fichero no dice que su pepper case ni que su migración haya corrido.
    NUEVO, CLASIFICADO, READY, READ_ONLY_READY = (
        "NUEVO", "CLASIFICADO", "READY", "READ_ONLY_READY")

    def _connect(self) -> sqlite3.Connection:
        """Conexión OPERACIONAL de esta hebra. Sólo en estados listos.

        La conexión de inicialización es THREAD-LOCAL a propósito: un flag global
        `_inicializando` autorizaba a CUALQUIER hebra a abrir el original
        mientras otra migraba.
        """
        init_con = getattr(self._local, "init_con", None)
        if init_con is not None:
            return init_con
        con = getattr(self._local, "con", None)
        # CADA reutilización se acredita otra vez. Un `Journal` distinto puede
        # haber restaurado el path después de que este hilo cacheara `con`; época
        # sólo coordina hilos de ESTA instancia y no ve aquel replace.
        self._verificar_identidad_operacional(con)
        if con is not None:
            if getattr(self._local, "epoca", None) == self._epoca:
                return con
            # ÉPOCA VIEJA: el respaldo de esta conexión se rehízo o se soltó.
            # Se cierra y se vuelve a resolver por el estado ACTUAL — nunca se
            # sirve un handle de una copia que ya no es la vigente.
            try:
                con.close()
            except sqlite3.Error:
                pass
            self._local.con = None
        if self._estado == self.READY:
            # ANTES de abrir: comprobar después ya habría creado el `-shm` sobre
            # el fichero de otro y dejado un handle vivo apuntándolo.
            self._verificar_identidad()
            con = None
            # EL CERROJO VA ANTES DE TOCAR SQLite, y va AQUÍ y no en `_Tx`
            # porque `_Tx.__init__` ya llamaba a `_connect()` antes de que
            # `__enter__` pidiese nada: la apertura —con su
            # `PRAGMA journal_mode=WAL`— podía crear sidecars mientras otro
            # proceso tenía el EXCLUSIVO y estaba fotografiando. Lo mismo hacía
            # `_guard_mutable` al llamar a `stored_durable_v()` antes del `_tx`.
            # Envolviendo la apertura, quedan cubiertos TODOS los llamantes y no
            # hay que acordarse en cada uno.
            with self._cerrojo_escritor():
              try:
                # El `connect` va DENTRO del `try`: fallar al abrir y fallar al
                # poner el PRAGMA son el mismo suceso para quien llama —el
                # volumen no admite escritura— y sólo uno de los dos estaba
                # cubierto.
                con = sqlite3.connect(self.path,
                                      timeout=self._busy_timeout_ms / 1000.0,
                                      isolation_level=None)
                con.execute("PRAGMA journal_mode=WAL")
                con.execute("PRAGMA synchronous=FULL")
                con.row_factory = sqlite3.Row
                con.execute("PRAGMA foreign_keys=ON")
                con.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
              except sqlite3.OperationalError as e:
                # ⛔ AQUÍ NO SE DEGRADA, Y ÉSTE ES EL PUNTO.
                # Esta rama vive DENTRO del `with _cerrojo_escritor()` (SH), y
                # antes llamaba a `_cerrojo_ciclo()` (EX) para rehacer la foto:
                # eso es un upgrade SH→EX con OTRO descriptor, y `flock` lo
                # resuelve bloqueándose contra uno mismo. Medido: 890 de 890
                # frames del `sample` dentro de `fcntl.flock`.
                #
                # Y moverlo fuera de este `with` NO bastaría: cuando `_connect`
                # viene de un `_Tx`, hay un SH EXTERIOR que seguiría retenido.
                #
                # ⇒ FALLA CERRADO y se DEJA DICHO. La transición a sólo lectura
                #   es un acto explícito y POSTERIOR (`initialize()`), corrido
                #   fuera de toda operación con SH. Nunca un upgrade.
                if con is not None:
                    con.close()
                self._read_only = True
                self._degradacion_pendiente = True
                raise JournalReadOnly(
                    f"el volumen dejó de admitir escritura y no degrado dentro "
                    f"de una operación en curso: vuelve a llamar a "
                    f"`initialize()` para pasar a sólo lectura ({e})") from e
              except Exception:
                if con is not None:
                    con.close()
                raise
            self._local.con = con
            self._local.epoca = self._epoca
            return con
        if self._estado == self.READ_ONLY_READY:
            c = self._abrir_snapshot()
            self._local.con = c
            self._local.epoca = self._epoca
            return c
        raise JournalNotInitialized(
            f"estado `{self._estado}`: el journal no está listo. Clasificar no es "
            f"inicializar — falta validar el pepper y, si toca, migrar.")

    def _verificar_identidad_operacional(
            self, con: sqlite3.Connection | None = None) -> None:
        """Falla cerrado si otra instancia sustituyó la ruta clasificada.

        El llamante operacional ya mantiene SH o EX. Aquí nunca se toma `_mutex`
        ni se cambia `_estado`: hacerlo bajo SH invertiría mutex→EX de initialize.
        Se cierra sólo el handle local, se publica un Event idempotente para los
        demás hilos y la reinicialización explícita hará el cambio global.
        """
        changed = self._identidad_invalidada.is_set()
        if not changed and self._identidad is not None:
            st = _stat_seguro(self.path)
            changed = st is None or (st.st_dev, st.st_ino) != self._identidad
        if not changed:
            return
        if con is not None:
            try:
                con.close()
            except sqlite3.Error:
                pass
        if getattr(self._local, "con", None) is con:
            self._local.con = None
        self._identidad_invalidada.set()
        raise IdentityChanged(
            "la ruta del journal cambió de identidad; cierro la conexión cacheada "
            "y exijo initialize() explícito antes de volver a operar")

    def _abrir_snapshot(self) -> sqlite3.Connection:
        """Conexión a la COPIA recuperada, `query_only`. No cachea por sí sola."""
        destino = self._snapshot
        if destino is None and self._tmpdir:
            destino = os.path.join(self._tmpdir, os.path.basename(self.path))
        if destino is None or not os.path.exists(destino):
            raise JournalNotInitialized(
                "no hay copia clasificada que servir: sin preflight no hay lectura")
        c = sqlite3.connect(destino, timeout=self._busy_timeout_ms / 1000.0,
                            isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA query_only=ON")
        c.execute("PRAGMA foreign_keys=ON")
        return c

    @contextlib.contextmanager
    def _inspeccion(self):
        """Lectura EFÍMERA de la copia, para preguntas previas a `initialize()`.

        Se abre y se cierra en el acto y NO se guarda como conexión operacional
        ni toca `_read_only`. Cachearla era el defecto: `stored_durable_v()`
        dejaba pegada una conexión `query_only` del snapshot, y la migración
        posterior la reutilizaba creyendo que hablaba con el original.
        """
        self._preflight()
        if self._veredicto[0] == "nueva":
            raise JournalNotInitialized(
                "la ruta no existe: `initialize()` la crea; preguntar no")
        c = self._abrir_snapshot()
        try:
            yield c
        finally:
            c.close()

    def _assert_pragmas(self) -> dict:   # noqa: D401 — instrumento de medida
        """PRAGMAs REALES de la conexión viva.

        Exige journal clasificado: medir los PRAGMAs de una base que aún no
        existe la crearía para poder medirla, y el instrumento pasaría a fabricar
        el objeto que dice observar.
        """
        if self._estado not in (self.READY, self.READ_ONLY_READY):
            raise JournalNotInitialized(
                "los PRAGMAs son los de la conexión VIVA: antes de `initialize()` "
                "no hay ninguna, y crearla para medirla haría que el instrumento "
                "fabricara el objeto que dice observar")
        with self._cerrojo_escritor():
            con = self._connect()
            return {
                "foreign_keys": con.execute("PRAGMA foreign_keys").fetchone()[0],
                "journal_mode": con.execute("PRAGMA journal_mode").fetchone()[0],
                "synchronous": con.execute("PRAGMA synchronous").fetchone()[0],
                "busy_timeout": con.execute("PRAGMA busy_timeout").fetchone()[0],
            }

    def close(self) -> None:
        """Cierra la conexión DE ESTA HEBRA. Las de otras hebras son suyas."""
        for nombre in ("con", "init_con"):
            c = getattr(self._local, nombre, None)
            if c is not None:
                c.close()
                setattr(self._local, nombre, None)

    def dispose(self) -> None:
        """Cierra Y suelta el directorio temporal de la copia.

        `close()` NO lo borra a propósito: se llama DENTRO de la transición, con
        la copia todavía en uso —es de donde sale el pepper y la versión—, y
        borrarla ahí dejaría a `_abrir_snapshot()` sin respaldo. La liberación
        es un acto distinto y explícito.

        Sin esto cada `Journal` dejaba un `llminbox-preflight-*` vivo hasta que
        el sistema limpiase `/tmp`: la copia contiene la base ENTERA, así que la
        fuga no es de inodos, es de una réplica completa del journal por cada
        instancia que se abrió.
        """
        # EXCLUSIÓN CONTRA OPERACIONES VIVAS. Sin el exclusivo, `dispose()`
        # corría mientras otra hebra tenía una transacción abierta y le tiraba
        # el respaldo por debajo.
        # MUTEX + EXCLUSIVO OBLIGATORIO, y el exclusivo NO se suelta hasta
        # haber desenganchado y borrado. Con `opcional=True` esto fallaba
        # ABIERTO —soltaba el respaldo sin exclusión ninguna— y soltando el EX
        # antes del `detach`, un `initialize()` concurrente podía instalar OTRO
        # finalizador en medio y quedarse sin dueño.
        with self._mutex:
            with self._cerrojo_ciclo():
                self.close()
                self._epoca += 1      # invalida lo cacheado en OTRAS hebras
                viejo = self._tmpdir
                self._tmpdir = None
                self._tmpdir_previo = None
                self._snapshot = None
                self._veredicto = None
                self._inventario_clasificado = None
                self._estado = self.NUEVO
                if self._limpiador is not None:
                    self._limpiador.detach()
                    self._limpiador = None
                if viejo:
                    shutil.rmtree(viejo, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        """Ni tapar el fallo de limpieza ni tapar el del cuerpo.

        `dispose()` falla CERRADO, así que puede lanzar. Dejarlo propagar solo
        se comía la excepción del cuerpo —la que explica de verdad qué pasó—; y
        tragárselo «best effort» escondía que el respaldo no se soltó. Cuando
        fallan los dos, se ven los DOS: `BaseExceptionGroup`.
        """
        try:
            self.dispose()
        except BaseException as limpieza:
            if exc is None:
                raise
            raise BaseExceptionGroup(
                "falló el cuerpo Y falló la limpieza del journal",
                [exc, limpieza]) from None
        return False

    # ── transacciones ────────────────────────────────────────────────────────
    class _Tx:
        """`BEGIN IMMEDIATE` con traducción del error de sólo lectura.

        No se usa `with con:` porque ése abre transacción DIFERIDA y el
        `SQLITE_BUSY_SNAPSHOT` del upgrade lee-luego-escribe NO lo reintenta
        `busy_timeout`.
        """

        def __init__(self, journal: "Journal"):
            self.j = journal
            self._sh = None
            self._epoca = None
            self.con = None          # NO se conecta aquí: construir el objeto
                                     # abría SQLite antes de que `__enter__`
                                     # pidiera el turno.

        def __enter__(self) -> sqlite3.Connection:
            # ORDEN: cerrojo ANTES que SQLite. Al revés, la transacción ya está
            # abierta —y el `-wal` ya creado— cuando se pide el turno.
            self._sh = self.j._cerrojo_escritor()
            self._sh.__enter__()
            try:
                # ESTADO Y ÉPOCA SE COMPRUEBAN BAJO EL MISMO CERROJO Y ANTES DEL
                # `BEGIN`. Construir el `_Tx` mientras otro tenía el EXCLUSIVO y
                # commitear DESPUÉS de su `dispose()` dejaba entrar una mutación
                # con el journal ya en `NUEVO`: la comprobación de arriba había
                # caducado entre el `__init__` y el `__enter__`.
                self._verificar_vigencia()
                self.con = self.j._connect()
                self._epoca = self.j._epoca
                self.con.execute("BEGIN IMMEDIATE")
                # VERSIÓN DENTRO DE LA TRANSACCIÓN: es el único sitio donde
                # comprobarla significa algo, porque nadie puede migrar por
                # debajo mientras esta transacción esté viva.
                #
                # SÓLO LA CONEXIÓN DE INICIALIZACIÓN AUTORIZA BOOTSTRAP.
                #
                # La versión anterior de esto eximía POR VALOR —«sin `meta` o
                # sin sello, será la creación»— y eso es un agujero: un borrado
                # o un downgrade EXTERNOS del sello se leían como «estoy
                # naciendo» y dejaban seguir mutando. La ausencia de sello no
                # dice quién eres, dice que no hay sello.
                #
                # Ahora la autorización viene del ÚNICO sitio que no se puede
                # dejar puesto por accidente: la conexión dedicada, que vive en
                # un `finally` y sólo durante la creación o la migración.
                if getattr(self.j._local, "init_con", None) is None:
                    try:
                        fila = self.con.execute(
                            "SELECT v FROM meta WHERE k='durable_v'").fetchone()
                    except sqlite3.OperationalError as e:
                        if "no such table" not in str(e).lower():
                            raise
                        fila = None
                    v = int(fila["v"]) if fila else None
                    if v is not None and v > DURABLE_V:
                        raise SchemaTooNew(
                            f"la base declara durable_v={v} y este código conoce "
                            f"{DURABLE_V}: no muto ni migro. Lecturas disponibles.")
                    if v != DURABLE_V:
                        raise SchemaMismatch(
                            f"la base declara durable_v={v} y para MUTAR se exige "
                            f"v{DURABLE_V}: corre `initialize()`. Un sello "
                            f"ausente o menor no autoriza una escritura.")
            except BaseException as e:
                # ROLLBACK ANTES DE SOLTAR. El `BEGIN IMMEDIATE` ya puede haber
                # entrado cuando falla la comprobación de versión, y salir sin
                # deshacerlo dejaba la transacción ABIERTA sobre la conexión
                # cacheada: la siguiente operación de esta hebra moría con
                # «cannot start a transaction within a transaction», que es un
                # error que no dice nada de la causa.
                if self.con is not None:
                    self._rollback()
                self._sh.__exit__(type(e), e, None)
                self._sh = None
                if isinstance(e, sqlite3.OperationalError):
                    raise _translate_ro(e) from e
                raise
            return self.con

        def _verificar_vigencia(self) -> None:
            """El journal tiene que seguir siendo el mismo, y seguir listo.

            Se llama DOS veces: antes del `BEGIN` y antes del `COMMIT`. Entre
            una y otra cabe un `dispose()` o una re-foto de otro hilo, y sin la
            segunda el trabajo ya hecho se sellaba igual.
            """
            # EXENCIÓN, y es la única: la propia transición a LISTO migra
            # dentro de un `_tx` y todavía NO es READY —READY se pone al
            # terminar—. Se reconoce por su conexión dedicada, que sólo existe
            # mientras se está inicializando; no es un flag que alguien pueda
            # dejar puesto.
            if getattr(self.j._local, "init_con", None) is not None:
                return
            self.j._verificar_identidad_operacional(self.con)
            if self.j._estado != Journal.READY:
                raise JournalNotInitialized(
                    f"el journal pasó a `{self.j._estado}` mientras esta "
                    f"transacción estaba viva: no la sello")
            if self._epoca is not None and self._epoca != self.j._epoca:
                raise JournalNotInitialized(
                    "el respaldo de esta transacción se rehízo mientras estaba "
                    "viva: no la sello")

        def _rollback(self) -> None:
            # Defensivo: si la transacción ya no está activa, el ROLLBACK falla y
            # ese fallo TAPARÍA el error de verdad que nos trajo aquí.
            try:
                self.con.execute("ROLLBACK")
            except sqlite3.Error:
                pass

        def __exit__(self, exc_type, exc, tb):
            # El compartido se suelta SIEMPRE y DESPUÉS del COMMIT/ROLLBACK: si
            # se soltara antes, el ciclo de vida podría tomar el exclusivo con
            # la transacción todavía viva, que es justo la ventana que esto
            # existe para cerrar.
            try:
                if exc_type is None:
                    try:
                        # SEGUNDA COMPROBACIÓN, y es la que cierra el P0: el
                        # `dispose()` ajeno ocurre DESPUÉS de que abriéramos.
                        self._verificar_vigencia()
                        self.con.execute("COMMIT")
                    except sqlite3.OperationalError as e:
                        self._rollback()
                        raise _translate_ro(e) from e
                    except JournalError:
                        self._rollback()
                        raise
                    return False
                self._rollback()
                if isinstance(exc, sqlite3.OperationalError):
                    raise _translate_ro(exc) from exc
                return False
            finally:
                if self._sh is not None:
                    self._sh.__exit__(None, None, None)
                    self._sh = None

    def _tx(self) -> "Journal._Tx":
        return Journal._Tx(self)

    def _guard_mutable(self) -> None:
        if self._estado != self.READY:
            if self._estado == self.READ_ONLY_READY:
                raise JournalReadOnly(f"journal de sólo lectura: {self.path}")
            raise JournalNotInitialized(
                f"estado `{self._estado}`: mutar exige READY, y READY sólo se "
                f"alcanza tras validar el pepper y correr la migración")
        if self._read_only:
            raise JournalReadOnly(f"journal de sólo lectura: {self.path}")
        # ⚠️ LA VERSIÓN NO SE COMPRUEBA AQUÍ. Este guard suelta el SH antes del
        #    `_tx`, así que entre la comprobación y la mutación cabe una
        #    migración entera: es un TOCTOU con cara de rigor. La comprobación
        #    de verdad vive DENTRO de `_Tx.__enter__`, en la misma transacción
        #    que muta. Aquí sólo queda el estado del ciclo, que sí es del objeto.
        return

    # ── esquema y migración ──────────────────────────────────────────────────
    def stored_durable_v(self) -> int | None:
        """Versión sellada. Viva si el journal está listo; si no, de la COPIA.

        Nunca abre el original antes de `initialize()` y nunca deja conexión
        pegada: la lectura previa es efímera por construcción.
        """
        def _leer(con):
            try:
                row = con.execute("SELECT v FROM meta WHERE k='durable_v'").fetchone()
            except sqlite3.OperationalError as e:
                # SÓLO la ausencia ESPERADA es `None`. «Bloqueada» y «corrupta»
                # se propagan: convertirlas en «no tiene sello» invita a sellarla.
                if "no such table" in str(e).lower():
                    return None
                raise
            return int(row["v"]) if row else None

        with self._mutex:
            if self._estado in (self.READY, self.READ_ONLY_READY):
                with self._lectura() as (con, _):
                    return _leer(con)
            with self._inspeccion() as con:
                return _leer(con)

    def _pepper_de(self, con) -> None:
        """Valida el pepper CONTRA LA COPIA, antes de tocar el original.

        Es lo que separa «esta base tiene la forma que digo» de «esta base es
        MÍA». Sin esto, un fichero con el esquema correcto y otro pepper se
        adoptaba y empezaba a acuñar `credential_ref` que no casan con nada.
        """
        try:
            fila = con.execute("SELECT v FROM meta WHERE k='pepper_check'").fetchone()
        except sqlite3.OperationalError:
            return
        if fila is None:
            return
        esperado = hmac.new(self._pepper, _PEPPER_CHECK, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(fila["v"], esperado):
            raise PepperMismatch(
                "el pepper no es el que creó esta base: recalcularía otros "
                "`credential_ref` y daría de alta principals nuevos en silencio")

    def _tablas_con_datos(self) -> set:
        """Tablas con al menos una fila. Una ilegible también cuenta: si no puedo
        mirarla, no puedo afirmar que la base esté vacía."""
        with self._lectura() as (con, _):
            out = set()
            for fila in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                t = fila[0] if not isinstance(fila, sqlite3.Row) else fila["name"]
                if t == "meta" or t.startswith("sqlite_"):
                    continue
                try:
                    if con.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone():
                        out.add(t)
                except sqlite3.OperationalError:
                    out.add(t)
            return out

    @contextlib.contextmanager
    def _cerrojo_ciclo(self, *, opcional: bool = False):
        """Cerrojo de ciclo de vida, tomado ANTES que SQLite.

        El orden importa: cogiendo primero la base y después el cerrojo, dos
        inicializadores pueden estar dentro de SQLite a la vez y el cerrojo sólo
        ordena lo que ya ocurrió. Cubre init, migración y restauración.
        """
        # REENTRANTE: `flock` es por descriptor, así que pedirlo dos veces desde
        # el mismo proceso con dos fds se bloquea contra sí mismo. `initialize`
        # llama a `_preflight`, y los dos lo quieren.
        # UPGRADE SH→EX EN LA MISMA HEBRA = AUTOBLOQUEO. `flock` es por
        # descriptor, así que pedir el exclusivo teniendo el compartido se
        # cuelga contra uno mismo. No se intenta y no se degrada en silencio:
        # se dice qué ha pasado.
        if getattr(self._local, "escritor", 0) and not getattr(self._local, "cerrojo", 0):
            raise LifecycleConflict(
                "esta hebra tiene el cerrojo COMPARTIDO de escritura y pide el "
                "EXCLUSIVO de ciclo de vida: eso es un upgrade y `flock` lo "
                "resuelve colgándose. Suelta la operación antes del ciclo.")
        if getattr(self._local, "cerrojo", 0):
            self._local.cerrojo += 1
            try:
                yield
            finally:
                self._local.cerrojo -= 1
            return
        ruta = self.path + ".lifecycle"
        try:
            fd = os.open(ruta, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError:
            if not opcional:
                raise
            # Directorio congelado: no se puede ni crear el cerrojo. La
            # CLASIFICACIÓN no escribe, así que sigue sin él — y si nadie puede
            # crear el fichero del cerrojo, tampoco hay escritores a los que
            # ordenar. Lo que sí exige cerrojo (crear, migrar, restaurar) lo pide
            # sin `opcional` y falla como debe.
            yield
            return
        self._local.cerrojo = 1
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            self._local.cerrojo = 0
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    @contextlib.contextmanager
    def _cerrojo_escritor(self):
        """`LOCK_SH` mientras dura una mutación. Es el otro lado del RW-lock.

        El `LOCK_EX` de `_cerrojo_ciclo` sólo ordenaba INICIALIZADORES entre sí.
        Las mutaciones no tomaban nada, así que creaban y checkpointeaban el
        `-wal` justo mientras alguien fotografiaba — y `_foto_estable` veía
        aparecer y desaparecer sidecars y declaraba inestable, con razón. No es
        que la foto fuese frágil: es que no había ninguna ventana en la que el
        fichero estuviese quieto.

        Compartido entre escritores: siguen siendo concurrentes entre sí, que es
        lo que exige el falsador 5 del ADR. Lo único que se excluye es
        escritores CONTRA ciclo de vida.

        Se toma ANTES de SQLite y se suelta después del COMMIT/ROLLBACK, en
        todos los caminos de error. Reentrante por hebra, como su hermano: una
        mutación puede anidar otra y `flock` es por descriptor.
        """
        if getattr(self._local, "cerrojo", 0) or getattr(self._local, "escritor", 0):
            # Si esta hebra ya tiene el EXCLUSIVO, pedir el compartido con otro
            # fd se bloquearía contra sí misma.
            if getattr(self._local, "escritor", 0):
                self._local.escritor += 1
                try:
                    yield
                finally:
                    self._local.escritor -= 1
            else:
                yield
            return
        try:
            fd = os.open(self.path + ".lifecycle", os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as e:
            if self._estado == self.READ_ONLY_READY:
                # El volumen congelado no permite O_RDWR, pero `flock(LOCK_SH)`
                # sólo necesita un descriptor abierto. El lockfile ya fue parte
                # del inventario clasificado; abrirlo O_RDONLY conserva la
                # exclusión frente a cualquier ciclo de vida sin convertir una
                # lectura del snapshot privado en una escritura accidental.
                try:
                    fd = os.open(self.path + ".lifecycle", os.O_RDONLY)
                except OSError:
                    raise JournalReadOnly(
                        f"no puedo tomar ni leer el cerrojo de `{self.path}`: {e}") from e
            else:
                # FALLA CERRADO para cualquier operación sobre el original.
                raise JournalReadOnly(
                    f"no puedo tomar el cerrojo de escritura de `{self.path}`: {e}") from e
        self._local.escritor = 1
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            yield
        finally:
            self._local.escritor = 0
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _crear_crash_safe(self) -> int:
        """Base NUEVA: se construye entera aparte y se publica sin pisar nada.

        Construirla en su sitio deja una ventana en la que el fichero existe a
        medias: si el proceso muere ahí, el siguiente arranque encuentra algo que
        no es ni base nueva ni base buena — y con el preflight, ese algo queda
        clasificado como indeterminado para siempre. El temporal va en el MISMO
        sistema de ficheros para que la publicación sea un `link` atómico.
        """
        carpeta = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(carpeta, exist_ok=True)
        fd, temporal = tempfile.mkstemp(prefix=".llminbox-nueva-", dir=carpeta)
        os.close(fd)
        os.unlink(temporal)
        try:
            semilla = Journal(temporal, pepper=self._pepper,
                              busy_timeout_ms=self._busy_timeout_ms,
                              lane_ledgers=self._lane_ledgers, clock=self._clock)
            # INTENCIÓN DE CREACIÓN, explícita. El sembrador es el único que
            # puede abrir un fichero que no existe, y lo dice en su estado en vez
            # de que el resto del código lo deduzca de una ausencia — deducirlo
            # era exactamente el bypass: «no existía cuando me construí» servía
            # de permiso para abrir lo que hubiera aparecido después.
            semilla._veredicto = ("creando", None, "creación explícita")
            # CONEXIÓN DE INICIALIZACIÓN EXPLÍCITA, con la MISMA disciplina que
            # la de la transición. Antes esto se resolvía poniendo
            # `semilla._estado = READY` a mano, y eso es eximir por un ESTADO
            # MUTABLE: cualquier camino que dejase READY puesto heredaba la
            # exención. La conexión dedicada no se puede «dejar puesta» por
            # accidente — vive en un `finally` y sólo durante esta llamada.
            con = sqlite3.connect(temporal,
                                  timeout=semilla._busy_timeout_ms / 1000.0,
                                  isolation_level=None)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=FULL")
            con.execute(f"PRAGMA busy_timeout={semilla._busy_timeout_ms}")
            con.execute("PRAGMA foreign_keys=ON")
            semilla._local.init_con = con
            try:
                v = semilla._inicializar_dentro(None)
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                con.execute("PRAGMA quick_check(1)")
                fd2 = os.open(temporal, os.O_RDONLY)
                try:
                    os.fsync(fd2)
                finally:
                    os.close(fd2)
            finally:
                semilla._local.init_con = None
                try:
                    con.close()
                except sqlite3.Error:
                    pass
                semilla.close()
            # PUBLICACIÓN NO-CLOBBER: `link` falla si el destino ya existe, así
            # que dos inicializadores convergen — uno publica y el otro descubre
            # que ya está y adopta lo publicado. `rename` habría pisado la base
            # del que llegó primero.
            try:
                os.link(temporal, self.path)
            except FileExistsError:
                return v
            fdd = os.open(carpeta, os.O_RDONLY)
            try:
                os.fsync(fdd)          # el directorio, o el nombre puede perderse
            finally:
                os.close(fdd)
            return v
        finally:
            for suf in ("", "-wal", "-shm"):
                try:
                    os.unlink(temporal + suf)
                except FileNotFoundError:
                    pass

    def _retain_pre_v7_snapshot_locked(self, source_version: int) -> MigrationSnapshot:
        """Fotografía SQLite consolidada, retenida antes del primer byte v7.

        El llamante ya posee ``_cerrojo_ciclo`` EX. SQLite ``backup`` incluye
        el WAL visible en una única imagen consistente; sólo después de cerrar,
        fsync y verificar se publica el fichero y, por último, su manifiesto.
        Si cualquier paso falla, la migración no empieza.
        """
        if source_version != 6:
            raise MigrationSnapshotRequired(
                "la primitiva pre-v7 sólo acepta una fuente sellada durable_v=6")
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        fd, temporary = tempfile.mkstemp(prefix=".llminbox-pre-v7-", dir=directory)
        os.close(fd)
        os.unlink(temporary)
        src = dst = None
        try:
            uri = "file:" + os.path.abspath(self.path) + "?mode=ro"
            src = sqlite3.connect(uri, uri=True,
                                  timeout=self._busy_timeout_ms / 1000.0,
                                  isolation_level=None)
            src.row_factory = sqlite3.Row
            src.execute("PRAGMA query_only=ON")
            row = src.execute(
                "SELECT v FROM meta WHERE k='durable_v'").fetchone()
            if row is None or int(row["v"]) != 6:
                raise MigrationSnapshotRequired(
                    "la fuente cambió antes de fotografiarla: no migro")
            dst = sqlite3.connect(temporary, isolation_level=None)
            dst.execute("PRAGMA journal_mode=DELETE")
            dst.execute("PRAGMA synchronous=FULL")
            src.backup(dst)
            if dst.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
                raise MigrationSnapshotRequired(
                    "la fotografía pre-v7 no supera quick_check")
            if dst.execute("PRAGMA foreign_key_check").fetchall():
                raise MigrationSnapshotRequired(
                    "la fotografía pre-v7 contiene referencias huérfanas")
            dst.close()
            dst = None
            src.close()
            src = None
            _fsync_file(temporary)
            digest = _sha256_file(temporary)
            snapshot_id = "mgs_" + digest[:32]
            final = self.path + f".pre-v7-{digest[:16]}.sqlite"
            if os.path.exists(final):
                if _sha256_file(final) != digest:
                    raise MigrationSnapshotRequired(
                        "el nombre de snapshot retenido existe con otros bytes")
                os.unlink(temporary)
            else:
                os.replace(temporary, final)
                _fsync_file(final)
                _fsync_dir(directory)
            created_at = _now_iso(self._clock())
            manifest = {
                "schema": "llminbox.migration-snapshot.v1",
                "snapshot_id": snapshot_id,
                "path": os.path.abspath(final),
                "sha256": digest,
                "source_durable_v": 6,
                "target_durable_v": 7,
                "created_at": created_at,
            }
            _write_json_atomic(self.path + ".pre-v7.json", manifest)
            result = self.migration_snapshot()
            if result is None or result.sha256 != digest:
                raise MigrationSnapshotRequired(
                    "el manifiesto pre-v7 publicado no verifica")
            return result
        except (OSError, sqlite3.Error, JournalError) as exc:
            if isinstance(exc, MigrationSnapshotRequired):
                raise
            raise MigrationSnapshotRequired(
                f"no pude retener la fotografía pre-v7: {exc}") from exc
        finally:
            if dst is not None:
                dst.close()
            if src is not None:
                src.close()
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def migration_snapshot(self) -> MigrationSnapshot | None:
        """Lee y verifica la referencia citable; nunca confía sólo en JSON."""
        manifest_path = self.path + ".pre-v7.json"
        try:
            with open(manifest_path, "r", encoding="utf-8") as src:
                raw = json.load(src)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise MigrationSnapshotRequired(
                f"manifiesto pre-v7 ilegible: {exc}") from exc
        exact = {"schema", "snapshot_id", "path", "sha256", "source_durable_v",
                 "target_durable_v", "created_at"}
        if type(raw) is not dict or set(raw) != exact:
            raise MigrationSnapshotRequired("manifiesto pre-v7 de forma desconocida")
        path = raw["path"]
        digest = raw["sha256"]
        if (raw["schema"] != "llminbox.migration-snapshot.v1"
                or raw["source_durable_v"] != 6 or raw["target_durable_v"] != 7
                or type(path) is not str or type(digest) is not str
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                or raw["snapshot_id"] != "mgs_" + digest[:32]
                or os.path.dirname(os.path.abspath(path)) !=
                   os.path.dirname(os.path.abspath(self.path))
                or os.path.abspath(path) != os.path.abspath(
                    self.path + f".pre-v7-{digest[:16]}.sqlite")):
            raise MigrationSnapshotRequired("manifiesto pre-v7 no canónico")
        try:
            measured = _sha256_file(path)
        except OSError as exc:
            raise MigrationSnapshotRequired(
                f"snapshot pre-v7 ausente o ilegible: {exc}") from exc
        if not hmac.compare_digest(measured, digest):
            raise MigrationSnapshotRequired("digest del snapshot pre-v7 no coincide")
        return MigrationSnapshot(
            raw["snapshot_id"], path, digest, 6, raw["created_at"])

    def restore_pre_v7_snapshot(self, token: str, *,
                                expected_sha256: str) -> MigrationSnapshot:
        """Restaura los bytes pre-v7 tras acreditar cierre y drenado actuales.

        No arranca ni simula un binario viejo. Al volver deja esta instancia en
        ``NUEVO``; el consumidor v6 debe abrir la imagen restaurada.
        """
        if type(expected_sha256) is not str or re.fullmatch(
                r"[0-9a-f]{64}", expected_sha256) is None:
            raise OperationInvalid("expected_sha256 debe ser un SHA-256 canónico")
        with self._mutex:
            if self._estado != self.READY:
                raise JournalNotInitialized("restaurar exige un journal v7 RW y listo")
            with self._cerrojo_ciclo():
                self.close()
                con = sqlite3.connect(self.path,
                                      timeout=self._busy_timeout_ms / 1000.0,
                                      isolation_level=None)
                con.row_factory = sqlite3.Row
                try:
                    con.execute("PRAGMA foreign_keys=ON")
                    con.execute("BEGIN IMMEDIATE")
                    view = _authenticate_locked(con, token, self._clock())
                    if view is None:
                        raise AuthError("restaurar exige sesión válida")
                    if self._capacidades_locked(con, view.principal_id) != {
                            CAP_ADMISSION_OPERATOR}:
                        raise PolicyDenied(
                            "restaurar exige exactamente admission_operator")
                    status = self._database_rollback_status_locked(con)
                    if status.durable_v != 7 or not status.certifiable:
                        raise AdmissionConflict(
                            "restaurar exige v7, ambas admisiones sealed en cada "
                            "carril durable y pending/failed globales en cero")
                    con.execute("COMMIT")
                    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except BaseException:
                    try:
                        con.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise
                finally:
                    con.close()

                snapshot = self.migration_snapshot()
                if snapshot is None or not hmac.compare_digest(
                        snapshot.sha256, expected_sha256):
                    raise MigrationSnapshotRequired(
                        "el snapshot solicitado no es el retenido y verificado")
                check = sqlite3.connect(
                    "file:" + snapshot.path + "?mode=ro", uri=True,
                    isolation_level=None)
                try:
                    verdict = _clasificar(check)
                finally:
                    check.close()
                if verdict[:2] != ("conocida", 6):
                    raise MigrationSnapshotRequired(
                        f"el snapshot ya no acredita v6 exacta: {verdict}")

                directory = os.path.dirname(os.path.abspath(self.path)) or "."
                fd, temporary = tempfile.mkstemp(
                    prefix=".llminbox-rollback-v6-", dir=directory)
                os.close(fd)
                try:
                    shutil.copyfile(snapshot.path, temporary)
                    _fsync_file(temporary)
                    if _sha256_file(temporary) != snapshot.sha256:
                        raise MigrationSnapshotRequired(
                            "la copia de restauración no conserva el digest")
                    # Tras checkpoint y cierre, los sidecars son reconstruibles;
                    # quitarlos ANTES del replace evita aplicar WAL v7 a bytes v6.
                    for suffix in ("-wal", "-shm", "-journal"):
                        try:
                            os.unlink(self.path + suffix)
                        except FileNotFoundError:
                            pass
                    os.replace(temporary, self.path)
                    _fsync_file(self.path)
                    _fsync_dir(directory)
                finally:
                    try:
                        os.unlink(temporary)
                    except FileNotFoundError:
                        pass
                self._epoca += 1
                self._estado = self.NUEVO
                self._veredicto = None
                self._identidad = None
                self._inventario_clasificado = None
                old_tmp = self._tmpdir
                self._tmpdir = None
                self._tmpdir_previo = None
                self._snapshot = None
                if self._limpiador is not None:
                    self._limpiador.detach()
                    self._limpiador = None
                if old_tmp:
                    shutil.rmtree(old_tmp, ignore_errors=True)
                return snapshot

    def initialize(self) -> int:
        """Transición única a un estado LISTO. Todo lo demás la exige.

        Orden, y cada paso está donde está por un fallo concreto:
          1. mutex de INSTANCIA — dos hebras no entran a la vez.
          2. preflight (efímero) y rechazo de futura/corrupta/indeterminada.
          3. si no existe: creación crash-safe, que converge por ESTA misma
             transición en vez de tener una propia.
          4. cerrojo de ciclo de vida y re-clasificación CON él en la mano.
          5. se cierra cualquier conexión previa: la de inspección era del
             snapshot, y reutilizarla haría que la migración creyera hablar con
             el original.
          6. PEPPER validado contra la copia. Antes de esto no se abre el
             original para escribir.
          7. RW dedicada y thread-local; migración; commit.
          8. y sólo entonces, READY.
        """
        with self._mutex:
            if self._identidad_invalidada.is_set():
                # Fuera de SH y bajo el orden mutex→EX que gobierna el ciclo.
                # La época invalida las conexiones de otros hilos; cada una se
                # cerrará localmente al volver a tocarla.
                self.close()
                self._epoca += 1
                self._estado = self.NUEVO
                self._veredicto = None
                self._identidad = None
                self._inventario_clasificado = None
                self._identidad_invalidada.clear()
            if self._degradacion_pendiente:
                # AQUÍ se consume lo que `_connect()` dejó dicho al fallar
                # cerrado. Es el «acto explícito y posterior» que sustituye al
                # upgrade SH→EX: se corre fuera de toda operación con SH, que es
                # lo que lo hace seguro. Escribir la bandera y no leerla nunca la
                # convertía en un comentario con forma de estado.
                self._degradacion_pendiente = False
                self._estado = self.NUEVO
                self._veredicto = None
            self._preflight()
            clase, version, detalle = self._veredicto
            if clase == "futura":
                raise SchemaTooNew(f"durable_v={version} ({detalle})")
            if clase == "corrupta":
                raise SchemaCorrupt(detalle)
            if clase == "indeterminada":
                raise SchemaIndeterminate(detalle)
            if clase == "nueva":
                with self._cerrojo_ciclo():
                    if _stat_seguro(self.path) is None:
                        v = self._crear_crash_safe()
                        self._veredicto = None
                        self.close()
                        self._preflight()
                    else:
                        self._veredicto = None
                        self.close()
                        self._clasificar_bajo_cerrojo()
                    c2, v2, d2 = self._veredicto
                    if c2 == "futura":
                        raise SchemaTooNew(f"durable_v={v2} ({d2})")
                    if c2 == "corrupta":
                        raise SchemaCorrupt(d2)
                    if c2 == "indeterminada":
                        raise SchemaIndeterminate(d2)
            return self._transicion_a_listo()

    def _refotografiar(self) -> None:
        """Rehace la copia y la clasificación, y TIRA las anteriores.

        `_verificar_identidad()` compara `(dev, inode)` y una escritura IN PLACE
        los conserva: el fichero puede haber cambiado de versión, de pepper o de
        contenido entero sin que el inode se mueva. Reutilizar la foto tomada
        antes del cerrojo es decidir sobre un fichero que ya no está ahí.

        La copia vieja se cierra y se borra: dejarla viva filtraría el handle y
        —peor— dejaría a `_abrir_snapshot()` sirviendo la foto caducada.
        """
        # LA FOTO CARA SÓLO SI HACE FALTA. `_foto_estable` se NIEGA —a propósito—
        # a clasificar mientras hay un escritor activo: dice que eso es «un
        # estado del mundo que se declara, no una carrera que se gane a
        # reintentos». Re-fotografiar incondicionalmente convertía esa negativa
        # en un fallo de `initialize()` para cualquiera que abriese el journal
        # mientras otros escriben, y eso tumba el falsador 5 del ADR (20 procesos
        # compitiendo tienen que llegar TODOS a un veredicto).
        #
        # El inventario es el discriminante exacto: si el conjunto de piezas, sus
        # (dev, inode), sus metadatos y sus sha son los MISMOS que cuando
        # clasifiqué, entonces no hay nada que reclasificar — la foto que tengo
        # ES la de ahora. Y si algo se movió, se rehace, que es el caso que abre
        # el bypass. Comparar es barato y no toca el original.
        if self._inventario_clasificado is not None:
            try:
                actual = _inventario(self.path)
            except Exception:
                actual = None
            if actual is not None and actual == self._inventario_clasificado:
                return
        # TRANSACCIONAL: o queda el estado NUEVO entero, o queda el VIEJO
        # entero. El intermedio —foto nueva buena y clasificación rota— dejaba
        # el viejo sin recoger y el nuevo sin dueño.
        previo = (self._tmpdir, self._snapshot, self._veredicto,
                  self._identidad, self._inventario_clasificado, self._limpiador)
        self.close()                 # ninguna conexión sobrevive a su respaldo
        self._epoca += 1
        self._veredicto = None
        self._snapshot = None
        self._tmpdir = None
        self._identidad = None
        self._inventario_clasificado = None
        try:
            self._clasificar_bajo_cerrojo()
        except BaseException:
            # Rollback: se tira lo poco que hubiera del intento y se restituye
            # el estado anterior TAL CUAL, con su finalizador incluido.
            if self._tmpdir and self._tmpdir != previo[0]:
                shutil.rmtree(self._tmpdir, ignore_errors=True)
            (self._tmpdir, self._snapshot, self._veredicto, self._identidad,
             self._inventario_clasificado, self._limpiador) = previo
            raise
        if previo[0] and previo[0] != self._tmpdir:
            shutil.rmtree(previo[0], ignore_errors=True)

    def _rechazar_veredicto(self) -> None:
        clase, version, detalle = self._veredicto
        if clase == "futura":
            raise SchemaTooNew(f"durable_v={version} ({detalle})")
        if clase == "corrupta":
            raise SchemaCorrupt(detalle)
        if clase == "indeterminada":
            raise SchemaIndeterminate(detalle)

    def _transicion_a_listo(self) -> int:
        # ¿Se puede tomar el cerrojo? Si NI ESO, el volumen está congelado: no
        # hay nada que migrar y el camino es el de sólo lectura — pero con su
        # pepper validado y su versión comprobada, no por el mero hecho de tener
        # una forma conocida.
        congelado = False
        try:
            fd = os.open(self.path + ".lifecycle", os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
        except OSError:
            congelado = True

        if congelado:
            # Aun sin cerrojo la foto se rehace: es trabajo de LECTURA sobre una
            # copia, y decidir «lista para leer» con una clasificación caducada
            # es el mismo defecto que decidir migrar con ella.
            self._refotografiar()
            self._rechazar_veredicto()
            with self._inspeccion() as con:
                self._pepper_de(con)
                fila = con.execute("SELECT v FROM meta WHERE k='durable_v'").fetchone()
            almacenada = int(fila["v"]) if fila else None
            if almacenada != DURABLE_V:
                # Una v1/v2 en sólo lectura NO está lista: le falta una migración
                # que aquí no se puede correr. Declararla lista por «forma
                # conocida» era prometer un esquema que nadie llevó a cabo.
                raise JournalReadOnly(
                    f"la base está en v{almacenada} y este código exige "
                    f"v{DURABLE_V}, pero el volumen no admite la migración")
            self._read_only = True
            self._estado = self.READ_ONLY_READY
            return almacenada

        with self._cerrojo_ciclo():
            # ① FOTO NUEVA BAJO EL CERROJO. Todo lo que viene debajo se decide
            #    sobre ESTA copia, no sobre la de antes de pedir el turno.
            self._refotografiar()
            self._rechazar_veredicto()
            # ② PEPPER, y va ANTES de cualquier apertura RW del original.
            #    La sonda de escritura estaba por ENCIMA de esto: sobre una base
            #    cuya FORMA es la nuestra —y que por tanto no para ninguna
            #    clasificación— el `journal_mode=WAL` de la sonda reescribía el
            #    formato y creaba `-wal`/`-shm` en el fichero de OTRA CASA, y el
            #    rechazo por pepper llegaba después. Rechazar después de tocar no
            #    es rechazar.
            with self._inspeccion() as con:
                self._pepper_de(con)
                fila = con.execute("SELECT v FROM meta WHERE k='durable_v'").fetchone()
                existing = int(fila["v"]) if fila else None
            if existing not in (6, DURABLE_V):
                raise MigrationFailed(
                    "el contrato beta sólo admite creación nueva, v6→v7 o v7; "
                    f"durable_v={existing} exige un migrador offline anterior")
            # v6→v7 NO EMPIEZA sin una imagen SQLite consolidada, retenida y
            # digest-verificada. Base nueva v7 pasa por `_crear_crash_safe` y no
            # necesita fingir un "antes" que nunca existió.
            required_snapshot_sha = None
            if existing == 6:
                required_snapshot_sha = self._retain_pre_v7_snapshot_locked(
                    existing).sha256
            # ③ Y AHORA sí se sondea si la base se deja escribir. El cerrojo se
            #    pudo crear, pero eso NO lo prueba: el `.lifecycle` puede existir
            #    de antes con permisos buenos mientras el `.sqlite` está en 0400.
            self._verificar_identidad()
            try:
                sonda = sqlite3.connect(self.path,
                                        timeout=self._busy_timeout_ms / 1000.0,
                                        isolation_level=None)
                try:
                    sonda.execute("PRAGMA journal_mode=WAL")
                    sonda.execute("BEGIN IMMEDIATE")
                    sonda.execute("ROLLBACK")
                finally:
                    sonda.close()
            except sqlite3.OperationalError:
                if existing != DURABLE_V:
                    raise JournalReadOnly(
                        f"la base está en v{existing} y este código exige "
                        f"v{DURABLE_V}, pero el volumen no admite la migración")
                self._read_only = True
                self._estado = self.READ_ONLY_READY
                return existing
            self._verificar_identidad()
            # ⑦ RW DEDICADA y thread-local: ninguna otra hebra la ve.
            con = sqlite3.connect(self.path, timeout=self._busy_timeout_ms / 1000.0,
                                  isolation_level=None)
            try:
                con.row_factory = sqlite3.Row
                con.execute("PRAGMA journal_mode=WAL")
                con.execute("PRAGMA synchronous=FULL")
                con.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
                con.execute("PRAGMA foreign_keys=OFF")
                con.execute("PRAGMA legacy_alter_table=ON")
                self._local.init_con = con
                v = self._inicializar_dentro(
                    existing, required_snapshot_sha=required_snapshot_sha)
            finally:
                try:
                    con.execute("PRAGMA legacy_alter_table=OFF")
                    con.execute("PRAGMA foreign_keys=ON")
                except sqlite3.Error:
                    pass
                self._local.init_con = None
                con.close()
            # ⑧ READY SE PUBLICA DENTRO DEL EXCLUSIVO. Fuera, entre soltar el
            #   cerrojo y asignar, cabía un `dispose()` que dejaba `NUEVO` y
            #   esta línea lo RESUCITABA a READY sobre un respaldo ya soltado.
            #
            #   Y AQUÍ —y sólo aquí— se limpia `_read_only`. `_connect()` lo
            #   pone a `True` al fallar cerrado, y NADIE lo bajaba nunca: un
            #   fallo TRANSITORIO de escritura dejaba el objeto en sólo lectura
            #   PARA SIEMPRE, aunque el volumen volviera. Se baja tras una
            #   transición RW ACREDITADA —sonda de escritura pasada, pepper
            #   validado, versión comprobada—, no por haber llegado hasta aquí.
            self._read_only = False
            self._estado = self.READY
        return v

    def _inicializar_dentro(self, existing: int | None, *,
                            required_snapshot_sha: str | None = None) -> int:
        with self._tx() as con:
            # 📸 LOS OBJETOS QUE YA HABÍA, ANTES de que el `SCHEMA` cree los que
            # falten. Después de ese bucle, `_existe()` no distingue «esta tabla
            # la trajo el fichero» de «la acabo de crear yo», y hay al menos una
            # migración —la de la barrera de admisión— para la que esa diferencia
            # ES la decisión. Medirlo aquí es la única ventana en que se puede.
            # `.lower()` (ASCII): el inventario tiene que casar con la misma
            # regla de identidad que usa SQLite para resolver nombres de tabla
            # — case-insensitive — o dos objetos que SQLite ve como uno solo
            # (`admission_history` / `ADMISSION_HISTORY`) se cuentan aquí como
            # si fueran distintos, y el que llegó con otra mayúscula se cuela.
            preexistentes = frozenset(
                r[0].lower() for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                    "   AND name NOT LIKE 'sqlite_%'"))
            columnas_preexistentes = {
                table: frozenset(_columnas(con, table))
                for table in preexistentes
            }
            indices_preexistentes = frozenset(
                r[0].lower() for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                    " AND name NOT LIKE 'sqlite_autoindex_%'"))
            # `executescript()` NO se puede usar aquí: hace COMMIT antes de correr
            # el script, así que se llevaría por delante el `BEGIN IMMEDIATE` y la
            # migración dejaría de ser transaccional sin decirlo. Se ejecutan las
            # sentencias una a una, dentro de la MISMA transacción.
            for sentencia in _sentencias(SCHEMA):
                con.execute(sentencia)
            row = con.execute("SELECT v FROM meta WHERE k='pepper_check'").fetchone()
            check = hmac.new(self._pepper, _PEPPER_CHECK, hashlib.sha256).hexdigest()
            if row is None:
                con.execute("INSERT INTO meta(k,v) VALUES('pepper_check',?)", (check,))
            elif not hmac.compare_digest(row["v"], check):
                raise PepperMismatch(
                    "el pepper no es el que creó esta base: recalcularía otros "
                    "`credential_ref` y daría de alta principals nuevos en silencio")
            if existing is not None and existing < DURABLE_V:
                self._migrar(
                    con, existing, preexistentes=preexistentes,
                    columnas_preexistentes=columnas_preexistentes,
                    indices_preexistentes=indices_preexistentes)
            # `foreign_key_check` DENTRO de la transacción y antes del sello: si
            # la migración dejó huérfanos, esto revienta, todo se deshace y la
            # base se queda en su versión anterior con sus datos intactos. Una
            # migración que sella primero y comprueba después no es una
            # migración, es una apuesta.
            huerfanos = con.execute("PRAGMA foreign_key_check").fetchall()
            if huerfanos:
                detalle = ", ".join(f"{r[0]}#{r[1]}->{r[2]}" for r in huerfanos[:5])
                raise MigrationFailed(
                    f"{len(huerfanos)} referencia(s) huérfana(s) tras migrar "
                    f"({detalle}): deshago y me quedo en v{existing}")
            if existing == 6:
                retained = self.migration_snapshot()
                if (required_snapshot_sha is None or retained is None
                        or not hmac.compare_digest(
                            retained.sha256, required_snapshot_sha)):
                    raise MigrationSnapshotRequired(
                        "el snapshot pre-v7 dejó de verificar antes del sello")
            con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('durable_v',?)",
                        (str(DURABLE_V),))
            con.execute("INSERT OR IGNORE INTO meta(k,v) VALUES('generation','1')")
            con.execute("INSERT OR IGNORE INTO meta(k,v) VALUES('created_at',?)",
                        (_now_iso(self._clock()),))
        return DURABLE_V

    # ── migración de esquema ────────────────────────────────────────────────
    def _migrar(self, con: sqlite3.Connection, desde: int, *,
                preexistentes: frozenset,
                columnas_preexistentes: Mapping[str, frozenset],
                indices_preexistentes: frozenset) -> None:
        """Migra hacia delante. Idempotente, y ENTERA dentro de la transacción
        que la llama: o queda la versión nueva o queda la vieja con sus datos.

        El contrato beta publicado es deliberadamente estrecho: sólo 6→7. Los
        migradores históricos se conservan como código de referencia, pero no
        son alcanzables desde `initialize()` y no pueden escribir bytes v7 sin
        la fotografía pre-v7 que sólo está definida para una fuente v6 exacta.
        """
        if desde != 6:
            raise MigrationFailed(
                f"salto durable v{desde}→v7 no soportado por el contrato beta")
        self._m6_a_7(
            con, desde=desde, preexistentes=preexistentes,
            columnas_preexistentes=columnas_preexistentes,
            indices_preexistentes=indices_preexistentes)

    def _m6_a_7(self, con: sqlite3.Connection, *, desde: int,
                 preexistentes: frozenset,
                 columnas_preexistentes: Mapping[str, frozenset],
                 indices_preexistentes: frozenset) -> None:
        """Añade el control plane y amplía receipts sin adoptar medias formas.

        El camino certificado es 6→7. Se conservan migraciones antiguas para
        fixtures históricos, pero sólo una fuente que se declara v6 recibe el
        guard exacto contra objetos/columnas/índices v7 plantados.
        """
        if desde == 6:
            planted_tables = preexistentes & _OBJETOS_NUEVOS_V7
            planted_indexes = indices_preexistentes & (
                set(_INDICES_V7) - set(_INDICES_V3))
            malformed = {
                table: sorted(columnas_preexistentes.get(table, frozenset()) -
                              _COLUMNAS_V6[table])
                for table in _OBJETOS_V6
                if columnas_preexistentes.get(table, frozenset()) !=
                   _COLUMNAS_V6[table]
            }
            if planted_tables or planted_indexes or malformed:
                raise MigrationFailed(
                    "v6 trae forma parcial/reservada de v7; no la adopto "
                    f"(tablas={sorted(planted_tables)}, "
                    f"indices={sorted(planted_indexes)}, "
                    f"columnas={sorted(malformed)})")

        # CHECK de receipts cambia de forma. Ninguna fila se pierde y todas las
        # FKs vuelven a apuntar al nombre canónico bajo legacy_alter_table=ON.
        con.execute("ALTER TABLE receipts RENAME TO receipts_v6")
        self._recrear(con, "receipts")
        before = con.execute("SELECT COUNT(*) c FROM receipts_v6").fetchone()["c"]
        con.execute(
            "INSERT INTO receipts(receipt_id,subject_kind,subject_id,principal_id,"
            "lane,current_state,created_at,updated_at)"
            " SELECT receipt_id,subject_kind,subject_id,principal_id,lane,"
            "current_state,created_at,updated_at FROM receipts_v6")
        after = con.execute("SELECT COUNT(*) c FROM receipts").fetchone()["c"]
        if before != after:
            raise MigrationFailed(
                "la reconstrucción de receipts no preservó todas las filas")
        con.execute("DROP TABLE receipts_v6")

    def _m5_a_6(self, con: sqlite3.Connection,
                preexistentes: frozenset) -> None:
        """v6: `admission_history`, y los pares CON HISTORIA nacen `closed`.

        ⚠️ NO cambia ninguna decisión: la ausencia de fila YA es cierre, así que
        sin estas filas el carril migrado también estaría cerrado. Lo que
        cambian es lo que se puede DEMOSTRAR y contra qué se puede fallar:

        · dejan CONSTANCIA de que el cierre existió, en vez de deducirlo de un
          vacío que también produce una base recién creada;
        · anclan el `epoch` en `1`, así que la primera apertura de un carril con
          historia es `2` y el `seal` tiene una valla contra la que perder.

        `origin='migration'` y operador NULO: esta fila no la firmó nadie, y
        ponerle el principal de quien corrió el despliegue sería atribuir un
        acto de operador a quien sólo arrancó un proceso.

        Sólo los carriles con historia REAL: inventar filas para carriles que
        nunca escribieron nada convertiría el manifiesto de la migración en una
        lista de deseos. Lo que no está aquí sigue cerrado por ausencia.
        """
        # 🩸 P1 (auditoria sobre `c38e63b`). Esto decia
        # `if not _existe(...): self._recrear(...)`, o sea ADOPTABA una tabla
        # que ya estuviera puesta — y entonces el `INSERT OR IGNORE` de abajo no
        # escribia nada porque el `epoch 1` ya existia. Una base sellada `v5`
        # con `admission_history` plantada y una fila `open` pasaba
        # `initialize`, se sellaba `v6` y `accept_event` ENTRABA sin que ningun
        # operador hubiera abierto nada. Reproducido por mi mano antes de curar.
        #
        # Una base que dice ser < 6 y trae un objeto de v6 es una contradiccion:
        # o es una v6 mutilada o se la planto alguien. En los dos casos la tabla
        # NO acredita nada, y adoptarla es creerle su contenido a un fichero.
        # Se RECHAZA: el `raise` deshace la transaccion entera, asi que la base
        # se queda en su version con sus datos y sin sellar.
        # ⚠️ Se mira la FOTO, no `_existe`: para cuando esto corre, el bucle del
        # `SCHEMA` ya ha creado la tabla en TODA migración legítima, así que
        # `_existe` diría «sí» siempre y este rechazo tumbaría también el camino
        # bueno. Lo cazó la suite en cuanto se escribió el guard grueso.
        # 🩸 P1 nº2 (segunda auditoria, ANTES de que este commit se cerrara).
        # La FOTO se tomaba con los nombres TAL CUAL los guarda `sqlite_master`,
        # y este `in` es comparación literal de Python. SQLite resuelve nombres
        # de tabla SIN distinguir mayúsculas (es el mismo objeto para `CREATE
        # TABLE IF NOT EXISTS`, para el `INSERT` de abajo, para todo): una base
        # con `ADMISSION_HISTORY` o `Admission_History` plantada pasaba las dos
        # comprobaciones —ni la FOTO la veía (guardaba el nombre con SU mayús-
        # cula), ni `_existe` la encontraba (buscaba el literal en minúsculas)—
        # y volvía a colar exactamente el mismo P1 con otra mayúscula. La FOTO
        # ahora normaliza a minúsculas al construirse (arriba, en
        # `_inicializar_dentro`) y `_existe` compara `COLLATE NOCASE`: los dos
        # miran la identidad como la mira SQLite, no como la escribió quien
        # creó la tabla.
        if "admission_history" in preexistentes:
            raise MigrationFailed(
                "la base dice ser anterior a v6 y ya trae `admission_history`: "
                "la barrera de admision solo la crea esta migracion, asi que una "
                "tabla preexistente no acredita su contenido y no se adopta")
        if not _existe(con, "admission_history"):
            # Cinturón: el `SCHEMA` de arriba ya la creó en el camino normal.
            self._recrear(con, "admission_history")
        at = _now_iso(self._clock())
        carriles = set()
        if _existe(con, "events"):
            carriles |= {r[0] for r in con.execute(
                "SELECT DISTINCT lane FROM events") if r[0]}
        if _existe(con, "outbox_operations"):
            carriles |= {r[0] for r in con.execute(
                "SELECT DISTINCT lane FROM outbox_operations") if r[0]}
        for carril in sorted(carriles):
            for verbo in ADMISSION_VERBS:
                con.execute(
                    "INSERT OR IGNORE INTO admission_history(lane,verb,epoch,"
                    "state,origin,operator,runtime_instance,reason_code,at)"
                    " VALUES(?,?,1,'closed','migration',NULL,NULL,"
                    "'SCHEMA_MIGRATION',?)", (carril, verbo, at))

    def _m4_a_5(self, con: sqlite3.Connection) -> None:
        """v5: `leases.resource_literal`.

        ⚠️ LAS FILAS VIEJAS NO SE RE-NORMALIZAN, y es deliberado: su `resource`
        es la clave primaria de un lease que puede estar VIVO. Reescribirlo
        movería la cerradura debajo de quien la tiene cogida — su
        `fencing_token` seguiría siendo válido para un recurso que ya no existe
        con ese nombre. Conviven: las viejas chocan por su literal, las nuevas
        por el normalizado. Se acaba solo cuando vencen, que es cuestión de
        minutos, no de una migración de datos.
        """
        if _existe(con, "leases") and \
                "resource_literal" not in _columnas(con, "leases"):
            con.execute("ALTER TABLE leases ADD COLUMN resource_literal TEXT")

    def _m3_a_4(self, con: sqlite3.Connection) -> None:
        """v4: `events.recipients_roles` y `events.recipients_broadcast`.

        `ALTER TABLE ADD COLUMN` basta: son columnas nuevas, sin FK, sin cambio
        de clave y sin índice que reconstruir. La reconstrucción es para lo que
        `ALTER` no puede hacer, no un rito por el que pasar siempre.

        ⚠️ NULL Y NO `'[]'`, y la diferencia es de significado, no de estilo:
        `'[]'` diría «este evento no tenía destinatarios acusables», que es una
        afirmación sobre filas que se escribieron cuando el censo no se
        consultaba. `NULL` dice «no resuelto en su día», y `mark_delivered` lo
        trata por el camino legado en vez de inventarles una atribución que
        nadie midió. No se re-normaliza hacia atrás: el censo de entonces no
        está, y derivarlo del de hoy sería fabricar historia.
        """
        cols = _columnas(con, "events") if _existe(con, "events") else set()
        if cols and "recipients_roles" not in cols:
            con.execute("ALTER TABLE events ADD COLUMN recipients_roles TEXT")
        if cols and "recipients_broadcast" not in cols:
            con.execute("ALTER TABLE events ADD COLUMN recipients_broadcast TEXT")

    def _m2_a_3(self, con: sqlite3.Connection) -> None:
        """v3: `credential_bindings.capabilities` y `idempotency.req_hash_v`.

        Cambia la FORMA persistente, así que sube la versión: dejar columnas
        nuevas bajo el sello `2` haría que dos esquemas distintos dijeran ser el
        mismo — que es exactamente la avería que trajo el correctivo anterior.

        Aquí SÍ vale `ALTER TABLE ADD COLUMN`: las dos son columnas nuevas con
        DEFAULT y sin FK ni cambio de clave, así que no hace falta reconstruir la
        tabla y los índices y el índice parcial de ligadura activa se quedan
        donde están. La reconstrucción es para lo que ALTER no puede hacer, no un
        rito por el que pasar siempre.
        """
        if _existe(con, "credential_bindings") and \
                "capabilities" not in _columnas(con, "credential_bindings"):
            con.execute("ALTER TABLE credential_bindings ADD COLUMN"
                        " capabilities TEXT NOT NULL DEFAULT '[]'")
        if _existe(con, "idempotency") and \
                "req_hash_v" not in _columnas(con, "idempotency"):
            # `1` para lo ya escrito: es la canonicalización con la que se
            # calculó. Ponerles la versión NUEVA diría que se calcularon con una
            # forma que no existía cuando se escribieron.
            con.execute("ALTER TABLE idempotency ADD COLUMN"
                        " req_hash_v INTEGER NOT NULL DEFAULT 1")

    @staticmethod
    def _recrear(con: sqlite3.Connection, tabla: str) -> None:
        """Recrea la TABLA desde `SCHEMA`. Los índices van aparte, ver abajo."""
        for sent in _sentencias(SCHEMA):
            if " ".join(sent.split()).startswith(
                    f"CREATE TABLE IF NOT EXISTS {tabla} "):
                con.execute(sent)

    @staticmethod
    def _indices(con: sqlite3.Connection, tabla: str) -> None:
        """Recrea los índices de una tabla — y VA DESPUÉS DEL `DROP` de la vieja.

        En SQLite el espacio de nombres de índices es de la BASE, no de la tabla:
        tras `RENAME`, `i_cmd_ws` sigue existiendo colgado de `commands_v1`, así
        que un `CREATE INDEX IF NOT EXISTS` con ese nombre es un NO-OP silencioso
        …y el `DROP TABLE commands_v1` posterior se lleva el índice por delante.
        La tabla queda correcta y sin su índice, sin un solo error: la avería no
        se ve hasta que alguien mide una consulta, meses después.
        """
        for sent in _sentencias(SCHEMA):
            plano = " ".join(sent.split())
            if plano.startswith("CREATE INDEX") or plano.startswith("CREATE UNIQUE"):
                if f" ON {tabla}(" in plano or plano.endswith(f" ON {tabla}"):
                    con.execute(sent)

    def _m1_a_2(self, con: sqlite3.Connection) -> None:
        at = _now_iso(self._clock())

        # ① COMMANDS — la avería que trajo este correctivo: `CREATE TABLE IF NOT
        # EXISTS` se salta la tabla entera, así que `role`/`runtime_instance`
        # nunca aparecían y `submit_command` reventaba con OperationalError sobre
        # una base sellada como buena.
        if _existe(con, "commands"):
            cols = _columnas(con, "commands")
            if "attribution_status" not in cols:
                con.execute("ALTER TABLE commands RENAME TO commands_v1")
                self._recrear(con, "commands")
                tenia_role = "role" in cols
                tenia_rti = "runtime_instance" in cols
                # El rol se DERIVA del principal (que sí está); el runtime se
                # queda NULL y la fila se marca como lo que es.
                con.execute(f"""
                    INSERT INTO commands(command_id,workstream_id,revision,lane,
                      principal_id,role,runtime_instance,attribution_status,
                      payload,state,supersedes,created_at)
                    SELECT c.command_id, c.workstream_id, c.revision, c.lane,
                      c.principal_id,
                      {'c.role' if tenia_role else 'p.role'},
                      {'c.runtime_instance' if tenia_rti else 'NULL'},
                      CASE WHEN {'c.runtime_instance IS NOT NULL' if tenia_rti else '0'}
                           THEN 'verified' ELSE 'legacy_unattributed' END,
                      c.payload, c.state, c.supersedes, c.created_at
                      FROM commands_v1 c JOIN principals p USING (principal_id)""")
                # CONTRADICCIÓN ⇒ SE PARA. `legacy_unattributed` significa «no
                # había runtime», no «había uno que no cuadra». Degradar la
                # contradicción a legado la haría desaparecer: la fila quedaría
                # marcada como un hueco normal y nadie volvería a mirarla.
                if tenia_rti:
                    # Cada contradicción se nombra POR SEPARADO: «no cuadra» no
                    # dice si el runtime es de otro principal, de otro carril o
                    # de otro rol, y quien tenga que arreglarlo necesita saberlo.
                    campos = {
                        "principal": "s.principal_id <> c.principal_id",
                        "lane": "s.lane <> c.lane",
                    }
                    if tenia_role:
                        campos["role"] = "s.role <> c.role"
                    partes = []
                    for nombre, cond in campos.items():
                        n = con.execute(
                            "SELECT COUNT(*) c FROM commands_v1 c"
                            "  JOIN runtime_sessions s USING (runtime_instance)"
                            f" WHERE c.runtime_instance IS NOT NULL AND ({cond})"
                        ).fetchone()["c"]
                        if n:
                            partes.append(f"{n} por `{nombre}`")
                    if partes:
                        raise MigrationFailed(
                            "comandos con runtime que contradice a su principal ("
                            + ", ".join(partes)
                            + "): no los degrado a legado")
                perdidos = con.execute("SELECT COUNT(*) c FROM commands_v1"
                                       ).fetchone()["c"] - con.execute(
                    "SELECT COUNT(*) c FROM commands").fetchone()["c"]
                if perdidos:
                    raise MigrationFailed(
                        f"{perdidos} comando(s) sin principal en el censo: no los "
                        f"tiro para que la migración parezca limpia")
                con.execute("DROP TABLE commands_v1")
                self._indices(con, "commands")

        # ② EVENT_ACKS y ③ OUTBOX_OPERATIONS — nacieron sin FK sobre el runtime.
        # Se reconstruyen para GANARLAS, y si hay huérfanos el
        # `foreign_key_check` del llamante lo caza y se deshace todo.
        for tabla, columnas in (
                ("event_acks",
                 "event_id,recipient,principal_id,runtime_instance,ack_ref,at"),
                ("outbox_operations",
                 "operation_id,event_id,operation,operator,lane,runtime_instance,"
                 "reason,from_state,at")):
            if not _existe(con, tabla):
                continue
            faltan = set(columnas.split(",")) - _columnas(con, tabla)
            sql = con.execute("SELECT sql FROM sqlite_master WHERE name=?",
                              (tabla,)).fetchone()["sql"]
            if faltan or "REFERENCES runtime_sessions" not in sql:
                con.execute(f"ALTER TABLE {tabla} RENAME TO {tabla}_v1")
                self._recrear(con, tabla)
                comunes = ",".join(sorted(
                    set(columnas.split(",")) & _columnas(con, f"{tabla}_v1")))
                con.execute(f"INSERT INTO {tabla}({comunes})"
                            f" SELECT {comunes} FROM {tabla}_v1")
                con.execute(f"DROP TABLE {tabla}_v1")
                self._indices(con, tabla)

        # ④ DENIAL_AGGREGATES — se le quita `runtime_instance` de la clave, así
        # que dos filas de v1 pueden COLAPSAR en una. Se SUMA `suppressed`: es el
        # número que dice cuánto se suprimió, y perderlo al fusionar convertiría
        # la migración en una amnistía silenciosa.
        if _existe(con, "denial_aggregates") and \
                "runtime_instance" in _columnas(con, "denial_aggregates"):
            con.execute("ALTER TABLE denial_aggregates RENAME TO denial_aggregates_v1")
            self._recrear(con, "denial_aggregates")
            con.execute("""
                INSERT INTO denial_aggregates(principal_id,lane,reason,bucket,
                  receipt_id,suppressed,first_at,last_at)
                SELECT principal_id, lane, reason, bucket,
                       MIN(receipt_id), SUM(suppressed),
                       MIN(first_at), MAX(last_at)
                  FROM denial_aggregates_v1
                 GROUP BY principal_id, lane, reason, bucket""")
            # Los recibos de los agregados que se fusionaron NO se borran —son
            # historia y alguien pudo citarlos—: se marcan `superseded` con una
            # transición que dice por qué, para que quien los lea sepa que su
            # cuenta vive ahora en otro.
            sobrantes = con.execute("""
                SELECT v.receipt_id FROM denial_aggregates_v1 v
                 WHERE v.receipt_id NOT IN (SELECT receipt_id FROM denial_aggregates)
                """).fetchall()
            for fila in sobrantes:
                rid = fila["receipt_id"]
                seq = con.execute("SELECT MAX(seq) s FROM receipt_transitions"
                                  " WHERE receipt_id=?", (rid,)).fetchone()["s"] or 0
                con.execute("INSERT INTO receipt_transitions(transition_id,"
                            "receipt_id,seq,state,at,detail) VALUES(?,?,?,?,?,?)",
                            (_new_id("trn"), rid, seq + 1, "superseded", at,
                             _canonical({"migration": "1->2",
                                         "why": "agregado fusionado al quitar "
                                                "runtime_instance de la clave"})))
                con.execute("UPDATE receipts SET current_state='superseded',"
                            " updated_at=? WHERE receipt_id=?", (at, rid))
            con.execute("DROP TABLE denial_aggregates_v1")
            self._indices(con, "denial_aggregates")

        # ⑤ HUELLAS v1 — eran ESTABLES: la misma credencial daba siempre la misma
        # huella, o sea un rastreador persistente de un tercero no autenticado,
        # que es justo lo que la rotación del ADR viene a impedir. Conservarlas
        # sería mantener vivo ese rastreador a cambio de un contador. Se PURGAN,
        # y el número se guarda agregado en `meta` para no perder la señal.
        if _existe(con, "unknown_credentials"):
            fila = con.execute("SELECT COUNT(*) f, COALESCE(SUM(count),0) t"
                               "  FROM unknown_credentials").fetchone()
            if fila["f"]:
                previo = con.execute("SELECT v FROM meta WHERE k='fp_v1_purgadas'"
                                     ).fetchone()
                total = (int(previo["v"]) if previo else 0) + int(fila["t"])
                con.execute("INSERT OR REPLACE INTO meta(k,v)"
                            " VALUES('fp_v1_purgadas',?)", (str(total),))
                con.execute("DELETE FROM unknown_credentials")

    # ── identidad ────────────────────────────────────────────────────────────
    def credential_ref(self, credential: str) -> str:
        """Referencia OPACA de una credencial. No es reversible y no se guarda
        el secreto: HMAC con el pepper del despliegue."""
        return hmac.new(self._pepper, credential.encode("utf-8"),
                        hashlib.sha256).hexdigest()[:32]

    def generation(self) -> int:
        with self._lectura() as (con, _):
            row = con.execute("SELECT v FROM meta WHERE k='generation'").fetchone()
            return int(row["v"]) if row else 1

    def rotate_map_generation(self) -> int:
        """Nueva generación del mapa de credenciales.

        Toda sesión emitida contra la generación anterior queda inválida en la
        petición SIGUIENTE, sin reiniciar el proceso (ADR-001 falsador 19).
        """
        self._guard_mutable()
        with self._tx() as con:
            row = con.execute("SELECT v FROM meta WHERE k='generation'").fetchone()
            nueva = (int(row["v"]) if row else 1) + 1
            con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('generation',?)",
                        (str(nueva),))
        return nueva

    def bind_credential(self, credential: str, *, principal: str | None,
                        role: str, lane: str,
                        capabilities: Sequence[str] = ()) -> Binding:
        """Fija la identidad de una credencial la PRIMERA vez que se ve.

        Con `principal=None` se deriva del rol y se marca
        `principal_source='derived_from_role'`. Añadir el principal explícito
        más tarde NO reescribe la ligadura: retira la vieja y crea otra, así que
        los eventos ya escritos conservan su atribución.
        """
        self._guard_mutable()
        ref = self.credential_ref(credential)
        source = "explicit" if principal else "derived_from_role"
        name = principal or role
        ts = self._clock()
        with self._tx() as con:
            row = con.execute(
                "SELECT b.binding_id, b.principal_id, b.principal_source, b.generation,"
                "       b.capabilities, p.principal, p.role, p.lane "
                "  FROM credential_bindings b JOIN principals p USING (principal_id)"
                " WHERE b.credential_ref=? AND b.retired_at IS NULL", (ref,)).fetchone()
            caps = _canonical(sorted(set(capabilities)))
            if row is not None:
                if row["principal"] == name and row["principal_source"] == source \
                        and row["role"] == role and row["lane"] == lane \
                        and row["capabilities"] == caps:
                    return Binding(ref, row["principal_id"], row["principal"],
                                   row["role"], row["lane"], row["principal_source"],
                                   row["generation"],
                                   tuple(json.loads(row["capabilities"])))
                # Cambió el mapa: se RETIRA la ligadura y nace otra. La anterior
                # se queda para que la atribución histórica siga resolviendo.
                con.execute("UPDATE credential_bindings SET retired_at=? "
                            " WHERE binding_id=?", (_now_iso(ts), row["binding_id"]))
                # …Y SE MATAN SUS SESIONES, en esta MISMA transacción. Mover la
                # identidad de una credencial sin cerrar lo que emitió deja
                # tokens vivos hablando en nombre de un principal que el operador
                # acaba de retirar: la retirada sería sólo documental.
                con.execute("UPDATE runtime_sessions SET revoked_at=?,"
                            " revoke_reason='binding_retired'"
                            " WHERE principal_id=? AND revoked_at IS NULL",
                            (ts, row["principal_id"]))
            pid = _new_id("prn")
            con.execute("INSERT INTO principals(principal_id,principal,role,lane,created_at)"
                        " VALUES(?,?,?,?,?)", (pid, name, role, lane, _now_iso(ts)))
            # Si llegamos aquí, la ligadura CAMBIA: sube la generación UNA vez,
            # igual que la recarga del mapa. Antes no lo hacía, así que atar una
            # credencial por esta vía dejaba vivas sesiones emitidas contra un
            # mapa que ya no era el vigente — dos puertas al mismo sitio con
            # reglas distintas.
            gen_row = con.execute("SELECT v FROM meta WHERE k='generation'").fetchone()
            gen = (int(gen_row["v"]) if gen_row else 1) + 1
            con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('generation',?)",
                        (str(gen),))
            con.execute(
                "INSERT INTO credential_bindings(binding_id,credential_ref,"
                "principal_id,principal_source,capabilities,generation,bound_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (_new_id("bnd"), ref, pid, source, caps, gen, _now_iso(ts)))
        return Binding(ref, pid, name, role, lane, source, gen,
                       tuple(sorted(set(capabilities))))

    def resolve_credential(self, credential: str) -> Binding | None:
        """Lectura sin transacción: vale para consultar, NO para autorizar.

        Quien emita identidad a partir de esto tiene que volver a resolver dentro
        de su `BEGIN IMMEDIATE` — ver `open_session`.
        """
        with self._lectura() as (con, _):
            return _resolver_ligadura(con, self.credential_ref(credential))

    def open_session(self, credential: str, *, ttl_s: int = DEFAULT_SESSION_TTL_S,
                     annotations: Mapping[str, Any] | None = None) -> IssuedSession:
        """Emite sesión de runtime. El token se devuelve UNA vez y sólo aquí.

        En la base entra `sha256(token)`. `annotations` (host, pid, imagen) se
        guarda como anotación NO autorizante: no participa en identidad.
        """
        self._guard_mutable()
        # ⏱️ Un TTL no positivo no es «una sesión corta»: es una sesión MUERTA a
        # la que se le entrega un token. Se rechaza aquí, antes de tocar nada,
        # porque el resto de la cura sólo cierra la vía del RELOJ y esta otra
        # llega por el ARGUMENTO — dejarla abierta haría que la invariante
        # «ninguna sesión nace vencida» valiera sólo para una de sus dos causas.
        # `OperationInvalid` y NO `AuthError`: la credencial está bien, lo que
        # está mal es el ARGUMENTO. Con `AuthError` el mapa de motivos escupe
        # `AUTH_ERROR` y el llamante honesto lee «tu credencial no vale» ante un
        # `ttl` que él mismo escribió mal.
        if ttl_s <= 0:
            raise OperationInvalid(
                f"ttl_s={ttl_s} no es positivo: la sesión nacería ya vencida y el "
                f"token entregado no autorizaría nada")
        # Sonda BARATA fuera de la transacción para el caso normal (credencial
        # desconocida) — pero NO es la que decide: la ligadura se vuelve a
        # resolver DENTRO del `BEGIN IMMEDIATE`. Entre una lectura de fuera y el
        # `INSERT` cabe un `reload_credential_map` entero, y entonces la sesión
        # nacería de una ligadura YA RETIRADA: identidad emitida contra un mapa
        # que el operador acaba de revocar.
        if self.resolve_credential(credential) is None:
            self.record_unknown_credential(credential)
            raise AuthError("credencial desconocida")
        # ⚠️ `open_session` NO lleva `@_audita` —no puede: su primer posicional es
        # una CREDENCIAL, no un token— asi que este rechazo NO deja rastro POR
        # CONSTRUCCION, no por descuido. Lo midio `@contratosbik` y coincide con
        # lo que yo aporte antes por otro camino.
        annotations = _congelar(annotations, "annotations",
                                tope_bytes=METADATO_MAX_BYTES)
        token = secrets.token_urlsafe(32)
        rti = _new_id("rti")
        ref = self.credential_ref(credential)
        self._tras_precheck()
        with self._tx() as con:
            binding = _resolver_ligadura(con, ref)
            if binding is None:
                raise AuthError(
                    "credencial retirada mientras se emitía la sesión: el mapa "
                    "cambió entre la comprobación y la escritura")
            # ⏱️ EL RELOJ SE LEE AQUÍ, DENTRO DE LA TRANSACCIÓN Y TRAS EL LOCK.
            # Estaba antes de `_tras_precheck()` y del `BEGIN IMMEDIATE`, y el
            # `expires_at` que se INSERTA salía de esa lectura vieja. Medido con
            # el seam (`ttl=100`, el hook avanza `+500`): la sesión se escribía
            # con `expires_at=1000100` y `now=1000500`, o sea NACÍA VENCIDA, y
            # `authenticate()` sobre el token recién entregado daba `None`.
            # Falla cerrado, sí — y aun así es la misma clase que la cura del
            # lease: ninguna decisión se apoya en una segunda lectura de algo
            # mutable, y el tiempo es lo mutable que nadie vigila.
            now = self._clock()
            # La generación que se DEVUELVE es la que se INSERTA, leída en esta
            # misma transacción. Devolver la de la ligadura era un dato viejo que
            # no gobierna nada: `authenticate()` compara contra la generación
            # VIVA, así que la sesión podía nacer anunciando otra.
            fila = con.execute("SELECT v FROM meta WHERE k='generation'").fetchone()
            gen = int(fila["v"]) if fila else 1
            con.execute(
                "INSERT INTO runtime_sessions(runtime_instance,principal_id,role,lane,"
                "token_hash,generation,issued_at,expires_at,annotations)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (rti, binding.principal_id, binding.role, binding.lane,
                 _sha256(token), gen, now, now + ttl_s,
                 _canonical(annotations or {})))
        return IssuedSession(token, rti, binding.principal_id, binding.role,
                             binding.lane, now + ttl_s, gen, binding.principal,
                             binding.principal_source, binding.capabilities)

    def reload_credential_map(self, mapa: Mapping[str, Mapping[str, str]]) -> int:
        """Recarga ATÓMICA del mapa: retirar + reemplazar + `generation++`, todo
        en UNA transacción. Devuelve la generación nueva.

        En tres pasos sueltos hay una ventana en la que el mapa viejo ya no está
        y el nuevo todavía no: quien pida sesión ahí dentro se la lleva contra un
        estado que nunca fue política de nadie. Y `generation++` tiene que caer
        en la MISMA transacción, o hay un instante con ligaduras nuevas y
        generación vieja — sesiones nuevas que nadie puede revocar por generación.
        """
        self._guard_mutable()
        ts = self._clock()
        at = _now_iso(ts)
        with self._tx() as con:
            vivos = {r["credential_ref"] for r in con.execute(
                "SELECT credential_ref FROM credential_bindings WHERE retired_at IS NULL")}
            nuevos = {self.credential_ref(c): v for c, v in mapa.items()}
            fila = con.execute("SELECT v FROM meta WHERE k='generation'").fetchone()
            actual_gen = int(fila["v"]) if fila else 1

            # ① QUÉ CAMBIA DE VERDAD, antes de tocar nada. Subir la generación
            # de oficio hacía que recargar el MISMO mapa —lo que hace cualquier
            # supervisor al reiniciar— tumbara todas las sesiones vivas. Una
            # recarga idéntica no es un cambio: es una comprobación.
            a_retirar = []
            for ref in vivos:
                spec = nuevos.get(ref)
                a = _resolver_ligadura(con, ref)
                if spec is None or a is None or _difiere(a, spec):
                    a_retirar.append(ref)
            a_crear = [ref for ref in nuevos if ref not in vivos or ref in a_retirar]
            if not a_retirar and not a_crear:
                return actual_gen           # NO-OP exacto: ni generación ni sesiones

            gen = actual_gen + 1            # UNA vez, pase lo que pase debajo
            con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('generation',?)",
                        (str(gen),))
            for ref in a_retirar:
                fila_b = con.execute(
                    "SELECT principal_id FROM credential_bindings"
                    " WHERE credential_ref=? AND retired_at IS NULL", (ref,)).fetchone()
                con.execute("UPDATE credential_bindings SET retired_at=?"
                            " WHERE credential_ref=? AND retired_at IS NULL",
                            (at, ref))
                if fila_b is not None:
                    con.execute("UPDATE runtime_sessions SET revoked_at=?,"
                                " revoke_reason='binding_retired'"
                                " WHERE principal_id=? AND revoked_at IS NULL",
                                (ts, fila_b["principal_id"]))
            # ② lo que falta, se liga. El principal se FIJA aquí y no se re-deriva.
            for ref, spec in nuevos.items():
                if _resolver_ligadura(con, ref) is not None:
                    continue
                pid = _new_id("prn")
                nombre = spec.get("principal") or spec["role"]
                con.execute("INSERT INTO principals(principal_id,principal,role,lane,"
                            "created_at) VALUES(?,?,?,?,?)",
                            (pid, nombre, spec["role"], spec["lane"], at))
                con.execute(
                    "INSERT INTO credential_bindings(binding_id,credential_ref,"
                    "principal_id,principal_source,capabilities,generation,bound_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (_new_id("bnd"), ref, pid,
                     "explicit" if spec.get("principal") else "derived_from_role",
                     _canonical(sorted(set(spec.get("capabilities") or ()))),
                     gen, at))
        return gen

    @_audita
    def refresh_session(self, token: str, *,
                        ttl_s: int = DEFAULT_SESSION_TTL_S) -> IssuedSession:
        """Rota el token SIN cambiar ni ensanchar la identidad.

        El token viejo queda inválido INMEDIATAMENTE — se revoca en la misma
        transacción que emite el nuevo, así que no hay ventana con dos válidos.
        """
        self._guard_mutable()
        if ttl_s <= 0:                     # argumento, no credencial — ver `open_session`
            raise OperationInvalid(
                f"ttl_s={ttl_s} no es positivo: la rotación revocaría la sesión "
                f"viva y devolvería un token ya vencido a cambio")
        nuevo = secrets.token_urlsafe(32)
        rti = _new_id("rti")
        self._tras_precheck()
        with self._tx() as con:
            # ⏱️ EL RELOJ SE LEE AQUÍ, DENTRO DE LA TRANSACCIÓN Y TRAS EL LOCK.
            # Estaba antes de `_tras_precheck()` y del `BEGIN IMMEDIATE`, y con
            # ese valor viejo se comparaba `s.expires_at > now`: entre la lectura
            # y el `SELECT` cabía el VENCIMIENTO ENTERO del padre. Medido con el
            # seam (`ttl` del padre `100`, el hook avanza `+101`):
            #   · el padre YA estaba vencido y aun así casaba,
            #   · se le revocaba con motivo `rotated` —un padre muerto no se
            #     rota, se deja morir—,
            #   · y el hijo nacía con `expires_at = now_viejo + ttl`, o sea VIVO:
            #     `authenticate(hijo)` daba `True` con el padre muerto.
            # Eso no es rotar: es RESUCITAR. Quien conserve un token caducado
            # recupera sesión válida con sólo refrescarlo, y el vencimiento deja
            # de ser exigible. Con el reloj dentro, el padre vencido no casa y la
            # llamada muere en `AuthError`.
            now = self._clock()
            # La sesión se REVALIDA aquí dentro, no fuera. Con `authenticate()`
            # fuera de la transacción, DOS refrescos simultáneos del mismo token
            # pasan los dos y crean DOS hijos válidos — o sea que rotar
            # MULTIPLICA la sesión en vez de reemplazarla, que es justo lo
            # contrario de lo que la rotación promete.
            fila = con.execute("SELECT v FROM meta WHERE k='generation'").fetchone()
            gen = int(fila["v"]) if fila else 1
            s = con.execute(
                "SELECT s.*, p.principal, b.principal_source, b.capabilities"
                "  FROM runtime_sessions s"
                "  JOIN principals p USING (principal_id)"
                "  JOIN credential_bindings b ON b.principal_id=s.principal_id"
                "       AND b.retired_at IS NULL"
                " WHERE s.token_hash=? AND s.revoked_at IS NULL"
                "   AND s.expires_at>? AND s.generation=?",
                (_sha256(token), now, gen)).fetchone()
            if s is None:
                raise AuthError("sesión no válida")
            # La revocación es CONDICIONAL y se cuenta: el perdedor de la carrera
            # no revoca nada y se entera. Sin el `changes()`, el segundo hilo
            # seguiría adelante y emitiría su propio hijo.
            con.execute("UPDATE runtime_sessions SET revoked_at=?,"
                        " revoke_reason='rotated' WHERE runtime_instance=?"
                        "   AND revoked_at IS NULL", (now, s["runtime_instance"]))
            if con.execute("SELECT changes() c").fetchone()["c"] != 1:
                raise AuthError("otro refresco rotó esta sesión primero")
            con.execute(
                "INSERT INTO runtime_sessions(runtime_instance,principal_id,role,lane,"
                "token_hash,generation,issued_at,expires_at,rotated_from,annotations)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (rti, s["principal_id"], s["role"], s["lane"], _sha256(nuevo),
                 gen, now, now + ttl_s, s["runtime_instance"], "{}"))
            datos = (s["principal_id"], s["role"], s["lane"], s["principal"],
                     s["principal_source"], tuple(json.loads(s["capabilities"])))
        return IssuedSession(nuevo, rti, datos[0], datos[1], datos[2],
                             now + ttl_s, gen, datos[3], datos[4], datos[5])

    @_audita
    def revoke_current(self, token: str, reason: str = "revoked") -> str:
        """Revoca LA PROPIA sesión. El caso seguro, y el que el gateway usa.

        Todo en UNA transacción: se resuelve el token y se revoca bajo el mismo
        `BEGIN IMMEDIATE`. Con `authenticate()` fuera y el `UPDATE` después,
        entre los dos cabe una recarga del mapa —y entonces se revocaría una
        sesión que ya no es la que se autenticó—. `authenticate()` lo dice en su
        propio docstring: no autoriza una escritura.

        Devuelve el `runtime_instance` revocado: quien llama necesita poder
        citarlo en su recibo sin volver a preguntar.
        """
        self._guard_mutable()
        with self._tx() as con:
            view = _authenticate_locked(con, token, self._clock())
            if view is None:
                raise AuthError("se requiere sesión de runtime válida")
            con.execute("UPDATE runtime_sessions SET revoked_at=?, revoke_reason=?"
                        " WHERE runtime_instance=? AND revoked_at IS NULL",
                        (self._clock(), reason, view.runtime_instance))
            return view.runtime_instance

    @_audita
    def revoke_session(self, token: str, runtime_instance: str,
                       reason: str = "revoked") -> None:
        """Revoca OTRA sesión — y ahora tiene sujeto.

        ⚠️ ESTO NO TOMABA `token` y su `UPDATE` casaba CUALQUIER
        `runtime_instance`: de otro principal y de OTRO CARRIL. En un núcleo cuya
        doctrina es «el sujeto sale de la credencial, no de la URL», era un
        interruptor de apagado repartido a cualquier portador de sesión. Lo midió
        `@contratosbik` (`RULING` ①) sobre su propia fila del contrato.

        Se permite si el objetivo es del MISMO principal —el caso legítimo del
        ADR: revocar otra sesión mía— o si el llamante trae `CAP_SESSION_ADMIN`.
        En los dos casos la comprobación va DENTRO de la transacción que muta.

        🔒 El rechazo es el MISMO mensaje para un `rti` ajeno y para uno que no
        existe. Distinguirlos convertiría este método en un oráculo de
        enumeración de sesiones vivas para quien no puede tocarlas.
        """
        self._guard_mutable()
        with self._tx() as con:
            view = _authenticate_locked(con, token, self._clock())
            if view is None:
                raise AuthError("se requiere sesión de runtime válida")
            fila = con.execute(
                "SELECT principal_id, lane FROM runtime_sessions"
                " WHERE runtime_instance=?", (runtime_instance,)).fetchone()
            # ⚠️ LA COMPROBACIÓN DE CARRIL ES HOY INALCANZABLE, y se dice para
            # que nadie la tome por la barrera. MEDIDO: `principal_id` YA deriva
            # del carril — el mismo `principal` ligado en dos carriles produce
            # DOS `principal_id` distintos, así que si el primero casa, el
            # segundo casa por construcción.
            #
            # Se deja como defensa en profundidad porque la derivación de
            # `principal_id` es una decisión que alguien puede cambiar, y ese día
            # esta línea es lo único que impide revocar a través de la frontera.
            # `test_el_principal_id_YA_deriva_del_carril` fija esa propiedad: si
            # cambia, ese test se pone rojo y esta cláusula pasa a ser necesaria.
            #
            # NO tiene mutante propio a propósito: un mutante que no puede morir
            # se lee en el informe igual que uno vivo por defecto. Lo que se falsa
            # es la propiedad, no la rama inalcanzable (lente `F2` de `@qa`: cada
            # guarda necesita un caso que SÓLO ella cierre; ésta no lo tiene).
            propia = (fila is not None
                      and fila["principal_id"] == view.principal_id
                      and fila["lane"] == view.lane)
            if not propia:
                admin = CAP_SESSION_ADMIN in self._capacidades_locked(
                    con, view.principal_id)
                if not admin:
                    raise PolicyDenied(
                        "esa sesión no es tuya: revocar la de otro principal "
                        f"exige la capacidad `{CAP_SESSION_ADMIN}`")
            con.execute("UPDATE runtime_sessions SET revoked_at=?, revoke_reason=?"
                        " WHERE runtime_instance=? AND revoked_at IS NULL",
                        (self._clock(), reason, runtime_instance))

    def authenticate(self, token: str) -> SessionView | None:
        """Resuelve un token a identidad DERIVADA DEL SERVIDOR, o None.

        ⚠️ FUERA DE TRANSACCIÓN: sirve para responder `/whoami` y para descartar
        barato una petición muerta. **No autoriza una escritura**: entre este
        «sí» y la mutación cabe un reload o una revocación. Toda mutación
        autorizada revalida con `_authenticate_locked` dentro de su propia
        transacción.
        """
        with self._lectura() as (con, _):
            return _authenticate_locked(con, token, self._clock())

    # Seam de pruebas: se dispara ENTRE el chequeo barato y el `BEGIN IMMEDIATE`.
    # Está en el código de producción a propósito y sin disimulo: la ventana que
    # abre esa distancia no se puede falsar con `sleep` sin volver el test
    # temporal y flaky. Por defecto no hace nada y nadie lo pone salvo un test.
    _gancho_carrera = None

    _TELEMETRIA_RAZONES = {
        "SchemaTooNew": "schema_too_new",
        "JournalReadOnly": "read_only",
        "IdempotencyConflict": "idempotency_conflict",
        "FencingConflict": "fencing_conflict",
        "CauseRejected": "cause_rejected",
        "FencedPairInvalid": "fencing_conflict",
        "ReplayUnverifiable": "other",
        "PolicyDenied": "other",
        "AdmissionClosed": "other",
        "AdmissionConflict": "other",
        "SchemaIndeterminate": "other",
        "MigrationFailed": "other",
        "AttributionRejected": "attribution_rejected",
        "SubjectNotFound": "other",
        "ReceiptStateInvalid": "other",
        "OperationInvalid": "other",
        "CommandRevisionConflict": "revision_conflict",
        "DeliveryConflict": "other",
        "CommandTransitionInvalid": "other",
        "LedgerNotAllowed": "lane_mismatch",
        "LeaseConflict": "fencing_conflict",
        "PepperMismatch": "other",
        "SchemaMismatch": "other",
        "LifecycleConflict": "other",
        "PreflightRejected": "other",
        "PreflightUnstable": "other",
        "SchemaCorrupt": "other",
        "JournalNotInitialized": "other",
        "IdentityChanged": "attribution_rejected",
    }

    def _sensor_para_vista(self, view: SessionView):
        """Factory no fiable: sólo se usa si conserva la identidad server-side."""
        if self._sensor_factory is None:
            return None
        sensor = self._sensor_factory(view)
        if sensor is None:
            return None
        # Import local: M1 sigue pudiendo usarse sin configurar observabilidad.
        from telemetry_bridge import sensor_matches
        if not sensor_matches(
                sensor, principal=view.principal_id, role=view.role, lane=view.lane,
                runtime_instance=view.runtime_instance,
                credential_generation=view.generation):
            return None
        return sensor

    def _vista_durable_para_telemetria(self, token: str) -> tuple[SessionView | None, str]:
        """Recupera atribución conocida incluso tras expiry/revoke; nunca autoriza."""
        ident = self._identidad_para_auditoria(token)
        if ident is None:
            return None, "unknown_credential"
        if ident.get("revoked_at") is not None:
            reason = "revoked"
        elif float(ident["expires_at"]) <= self._clock():
            reason = "expired"
        elif int(ident["generation"]) != int(ident["current_generation"]):
            reason = "revoked"
        else:
            reason = "revoked"
        view = SessionView(
            ident["runtime_instance"], ident["principal_id"], ident["principal"],
            ident["role"], ident["lane"], float(ident["expires_at"]),
            int(ident["generation"]), ident.get("principal_source", ""), ())
        return view, reason

    def _detalle_telemetria(self, **detail: Any) -> None:
        self._telemetry_local.detail = detail

    def _consume_detalle_telemetria(self) -> dict:
        detail = getattr(self._telemetry_local, "detail", {})
        self._telemetry_local.detail = {}
        return detail

    def _telemetria(self, operacion: str, token: str, argumentos: Mapping[str, Any],
                    *, resultado=None, error: Exception | None = None) -> None:
        """Emite sólo vocabulario/identidad derivados; jamás texto del error o payload."""
        if self._sensor_factory is None:
            return
        try:
            view = self.authenticate(token)
            auth_reason = "revoked"
            if view is None:
                view, auth_reason = self._vista_durable_para_telemetria(token)
            sensor = self._sensor_para_vista(view) if view is not None else None
            if sensor is None:
                return
            if error is not None:
                reason = (auth_reason if isinstance(error, AuthError)
                          else self._TELEMETRIA_RAZONES.get(type(error).__name__, "other"))
                sensor.count("denials", reason=reason, outcome="failed",
                             subject_kind="event" if operacion == "accept_event" else "denial")
                sensor.log("policy.denied", operation=operacion, reason=reason)
                return
            if operacion == "accept_event":
                intent = argumentos.get("intent") or {}
                outcome = "retry" if getattr(resultado, "replayed", False) else "ok"
                if not getattr(resultado, "replayed", False):
                    sensor.count("events.accepted", verb=intent.get("verb", "inform"),
                                 kind=(intent.get("canonical_kind") or
                                       intent.get("kind", "AMEND")), outcome="ok")
                sensor.span(
                    "coordination.event.accept",
                    attributes={"event_id": resultado.event_id,
                                "receipt_id": resultado.receipt_id,
                                "replayed": bool(resultado.replayed),
                                "cause_count": len(argumentos.get("causes") or ()),
                                "outcome": outcome},
                    parent=argumentos.get("trace"))
            elif operacion == "claim_outbox" and resultado is not None:
                sensor.count("outbox.attempts", outcome="ok", state="pending")
            elif operacion in {"mark_materialized", "mark_indexed"}:
                state = {"mark_materialized": "materialized",
                         "mark_indexed": "indexed"}[operacion]
                sensor.count("receipts.transitions", state=state, outcome="ok")
                if operacion == "mark_materialized":
                    contexto = self._contexto_outbox_telemetria(argumentos.get("event_id"))
                    if contexto is not None:
                        sensor.outbox_span(
                            event_id=argumentos["event_id"],
                            effect_id=self._effect_id_telemetria(
                                argumentos["event_id"], argumentos.get("entry_eid")),
                            accept_context=contexto["trace"],
                            attempt=contexto["attempt"], outcome="materialized")
            elif operacion == "mark_delivered":
                detail = self._consume_detalle_telemetria()
                if detail.get("advanced"):
                    final = detail.get("state") == "delivered"
                    sensor.count("receipts.transitions",
                                 state="delivered" if final else "delivery_progress",
                                 outcome="ok" if final else "retry")
                    sensor.span("runtime.job", attributes={
                        "operation": "delivery_ack",
                        "outcome": "delivered" if final else "delivery_progress"})
            elif operacion == "mark_outbox_failed":
                sensor.count("outbox.attempts", outcome="failed", state="failed")
        except Exception:
            # Regla M3: un sensor nunca cambia el resultado del kernel.
            return

    def _contexto_outbox_telemetria(self, event_id: str | None) -> dict | None:
        if not event_id:
            return None
        with self._lectura() as (con, _):
            row = con.execute(
                "SELECT o.attempts, e.attestation FROM outbox o"
                " JOIN events e USING(event_id) WHERE o.event_id=?", (event_id,)
            ).fetchone()
        if row is None:
            return None
        attestation = json.loads(row["attestation"])
        trace = attestation.get("trace") if isinstance(attestation, dict) else None
        return {"attempt": int(row["attempts"]), "trace": trace or {}}

    def _effect_id_telemetria(self, event_id: str, proposed: Any) -> str:
        """Correlador HMAC: el worker no controla los bytes que salen al span."""
        message = ("llminbox-outbox-effect-v1\0" + event_id + "\0" + str(proposed)
                   ).encode("utf-8", "surrogatepass")
        return "eff_" + hmac.new(self._pepper, message, hashlib.sha256).hexdigest()[:32]

    def _tras_precheck(self) -> None:
        if self._gancho_carrera is not None:
            self._gancho_carrera()

    # ── eventos ──────────────────────────────────────────────────────────────
    @_audita
    def accept_event(self, token: str, *, idempotency_key: str,
                     intent: Mapping[str, Any], ledger: str | None = None,
                     causes: Sequence[str] = (),
                     external_causes: Sequence[Mapping[str, str]] = (),
                     fenced_resource: str | None = None,
                     fencing_token: int | None = None,
                     trace: Mapping[str, Any] | None = None) -> Acceptance:
        """Acepta un evento nativo. UNA transacción, o nada.

        `fenced_resource`/`fencing_token`: si el evento es una mutación
        autoritativa de un recurso arrendado, el fencing se valida DENTRO de esta
        misma transacción. Es la vía ejecutable de la regla; `check_fence()`
        suelto es diagnóstico y no protege nada por sí solo.

        En el MISMO `BEGIN IMMEDIATE` entran evento + recibo + transición
        `accepted` + item de outbox + registro de idempotencia. No hay reserva
        `in_flight` durable fuera de la transacción, así que no existe estado
        colgado que barrer: o está todo o no está nada.

        Misma clave + mismo cuerpo → devuelve el recibo ORIGINAL (`replayed`).
        Misma clave + cuerpo distinto → `IdempotencyConflict`, CERO mutación.
        """
        self._guard_mutable()
        # PRIMERO la sesión. Validando la forma antes, un token desconocido
        # distinguía «mi causa está mal» de «mi token no vale» — un oráculo
        # gratis sobre el contrato para quien no ha demostrado ser nadie.
        if self.authenticate(token) is None:      # barato, NO decide
            raise AuthError("se requiere sesión de runtime válida")
        # ── COTA DEL `intent` · ANTES de tocar nada durable ────────────────
        # Adjudicada por `@cpo` (03:57:55Z) y el contrato wire de
        # `@contratosbik` (03:58:19Z): `<= INTENT_MAX_BYTES` del JSON CANONICO en
        # UTF-8, INCLUSIVO.
        #
        # 🔴 POR QUE AQUI: DESPUES de autenticar y DENTRO de `@_audita`, no antes.
        # 🩸 Y CORRIJO LO QUE ESTE MISMO BLOQUE AFIRMABA: dije «no se ha escrito
        # nada» y era FALSO. Lo medi por FILAS y no por bytes del fichero —el
        # tamaño no se mueve porque SQLite reusa paginas— y el rechazo
        # autenticado deja su rastro ACOTADO, que es lo que la garantia 6 exige:
        #     events / outbox / idempotency ........ +0
        #     denials / receipts / receipt_transitions +1   (primer caso aislado)
        # El NO autenticado sigue dando `401` y CERO denial: sin identidad no hay
        # a quien colgarle el recibo.
        # Medido sobre `aab2080`: un `intent` de `5 MB` se ACEPTABA y el journal
        # crecia `+10.493.952 B` — el DOBLE de la entrada, porque `head`/`body`
        # se guardan DOS VECES (columna propia + dentro del `intent` canonico;
        # el `2,00x` lo midio `@contratosbik`). No es que el limite fuera alto:
        # es que NO EXISTIA, ni aqui ni en el DTO.
        #
        # Y DESPUES de autenticar, por el mismo motivo que la atribucion: el
        # tamaño del cuerpo no se le confirma a quien no ha demostrado ser nadie.
        #
        # ⚠️ Se mide con `_canonical`, LA MISMA funcion con la que se hashea el
        # intent (ADR `bdec479:114-115`), para que el cliente pueda PREDECIR el
        # rechazo. Cualquier otra unidad —caracteres, el objeto en memoria, el
        # `Content-Length`— convierte el limite en una loteria.
        # ── PRESUPUESTOS · las CUATRO superficies de esta puerta ──────────
        # El validador es UNO y va DESPUES de autenticar: el tamaño del cuerpo no
        # se le confirma a quien no ha demostrado ser nadie. Y dentro de
        # `@_audita`, asi que el rechazo deja su rastro acotado.
        # 🧊 Y A PARTIR DE AQUI SE TRABAJA CON EL SNAPSHOT: la gramatica, el
        # hash, el canonico y el INSERT leen la copia privada, nunca el objeto
        # del llamante. Sin esto, validar era una foto de algo que podia haber
        # cambiado para cuando se hashea.
        # 🔻 EL AGREGADO CANONICO de ESTA mutacion: lo comparten TODOS los
        # campos controlados por cliente de esta puerta. Se SUMA aqui y se
        # DECIDE al final — precedencia: forma, limites locales, agregado.
        # 🔻 UN CONTADOR DE NODOS POR OPERACION, DESDE LA ENTRADA. `@cpo`
        # (`09:47:16Z`): «el contador es UNO por peticion y atraviesa TODOS los
        # campos; NO se reinicia por superficie». Lo tenia compartido SOLO entre
        # las dos secuencias, con `intent` y `trace` contando aparte: `24.576`
        # por peticion. Y `@qa` midio por que el agregado de BYTES no lo tapaba
        # —`24.576` nulos son `98 KB`, `10x` por debajo del techo—: **cure el eje
        # de al lado y lo lei como cobertura del que faltaba.**
        presupuesto = [0]
        agregado = _AgregadoPeticion()
        intent = _congelar(intent, "intent", tope_bytes=PAYLOAD_MAX_BYTES,
                           nodos=presupuesto, agregado=agregado,
                           obligatorio=True)
        trace = _congelar(trace, "trace", tope_bytes=METADATO_MAX_BYTES,
                          nodos=presupuesto, agregado=agregado)
        causes = _congelar_secuencia(causes, "causes",
                                     tope_bytes=ELEMENTO_MAX_BYTES,
                                     nodos=presupuesto, agregado=agregado)
        external_causes = _congelar_secuencia(external_causes, "external_causes",
                                              tope_bytes=ELEMENTO_MAX_BYTES,
                                              nodos=presupuesto, agregado=agregado)
        _exige_agregado(agregado)     # ③ AL FINAL, tras forma y limites locales

        # 🔴 Y LA ATRIBUCIÓN VA AQUÍ, DESPUÉS. Estaba ARRIBA — encima del propio
        # párrafo que acaba de explicar por qué no debe estar arriba. Medido con
        # token inválido sobre `811f635`:
        #     intent LIMPIO            -> AuthError
        #     intent + `agent`         -> AttributionRejected
        # ⇒ ORÁCULO ejecutable: quien no ha demostrado ser nadie ENUMERA el
        # conjunto de reservadas por respuesta diferencial, una palabra por
        # intento. No es teórico y no lo estrené yo: sobre `9793f8a` ya filtraba
        # con `principal` y `role`; lo que hice fue ENSANCHARLO de `21` a `25`
        # claves al añadir las seis raíces. La deuda es vieja, el ensanche mío.
        # El mismo argumento que ya estaba escrito para las CAUSAS y para los
        # DESTINATARIOS, aplicado por fin a la puerta que lo tenía delante.
        self._sin_atribucion(intent, "intent")
        self._sin_atribucion(trace, "trace")
        # DESTINATARIOS: después de la sesión y antes del hash de idempotencia.
        # Las dos mitades de esa posición son deliberadas y cada una arregla algo
        # distinto:
        #   · DESPUÉS de autenticar, por el mismo motivo escrito arriba para las
        #     causas: sin sesión válida, un rechazo por censo es un ORÁCULO —
        #     quien no ha demostrado ser nadie podría enumerar el roster a base
        #     de probar nombres. Un test de este repo lo cazó al primer intento
        #     (esperaba `AuthError` y recibía `RecipientUnresolved`).
        #   · ANTES del hash, porque un rechazo que ya quemó la clave de
        #     idempotencia obliga al cliente a inventarse otra para reintentar lo
        #     correcto.
        _kind = self._validar_gramatica(intent)
        _dest_roles, _dest_lit, _dest_bcast = self._resolver_destinos(
            intent.get("to", []))
        # Y la forma de las causas antes del hash: el canónico las recorre, así
        # que una malformada reventaba ahí con un `ValueError` crudo —un 500 sin
        # auditar— antes de llegar a su rechazo.
        for c in external_causes:
            if not isinstance(c, Mapping) or not c.get("ledger") \
                    or not c.get("entry_eid"):
                raise CauseRejected(
                    "una causa externa necesita `ledger` y `entry_eid`")
        if fencing_token is not None and (isinstance(fencing_token, bool)
                                          or not isinstance(fencing_token, int)):
            # `True` ES un `int` en Python, así que `fencing_token=True` pasaba la
            # comprobación del par y luego se comparaba como `1` contra un token
            # real — el primer token de todo recurso. Un booleano mal puesto
            # abría la valla del primer dueño.
            raise FencedPairInvalid(
                f"`fencing_token` tiene que ser un entero, no {type(fencing_token).__name__}")
        if (fenced_resource is None) != (fencing_token is None):
            # ⑤ El par va junto o no va. Medio par es peor que ninguno: pedir
            # fencing sin número deja la mutación SIN valla creyendo que la
            # tiene, y dar número sin recurso valida contra nada.
            raise FencedPairInvalid(
                "`fenced_resource` y `fencing_token` van juntos o no van: "
                "medio par no protege y parece que sí")
        verb = str(intent.get("verb") or "inform")
        now = self._clock()
        occurred_at = _now_iso(now)
        self._tras_precheck()
        with self._tx() as con:
            # LA validación que autoriza, dentro de la transacción que muta.
            view = _authenticate_locked(con, token, self._clock())
            if view is None:
                raise AuthError(
                    "sesión invalidada entre la comprobación y la escritura "
                    "(revocación, vencimiento o recarga del mapa)")
            # DESTINO: se deriva del carril de la SESIÓN y se valida contra la
            # allowlist ANTES de escribir una sola fila. Sin esto, el `ledger`
            # es un parámetro del cliente y el aislamiento por carril se lo
            # salta cualquiera nombrando el fichero de otro.
            destino = self._ledger_derivado(view.lane, ledger)
            # ORDEN: autenticar · derivar · hash · REPLAY/CONFLICTO · y sólo con
            # clave NUEVA, validar la valla. Comprobando el fence antes, un
            # reintento idéntico de algo YA ACEPTADO fallaba porque la valla
            # había vencido entre medias — el cliente reintenta por no haber
            # recibido la respuesta, no por querer mutar otra vez, y castigarle
            # ahí convierte la idempotencia en una trampa.
            # EL HASH SE CALCULA AQUÍ: después de autenticar y de DERIVAR el
            # destino, y sobre el destino EFECTIVO. Calculándolo antes con el
            # `ledger` crudo, `ledger=None` y su equivalente explícito daban
            # hashes distintos, así que el mismo reintento escrito de las dos
            # formas se leía como dos peticiones.
            req_hash = _req_hash(REQ_HASH_V, intent=intent, ledger_raw=ledger,
                                 ledger_efectivo=destino, causes=causes,
                                 external_causes=external_causes,
                                 fenced_resource=fenced_resource,
                                 fencing_token=fencing_token)
            prev = con.execute(
                "SELECT req_hash, req_hash_v, event_id, receipt_id FROM idempotency"
                " WHERE principal_id=? AND lane=? AND verb=? AND key=?",
                (view.principal_id, view.lane, verb, idempotency_key)).fetchone()
            if prev is not None:
                # Una fila vieja se compara CON SU PROPIA fórmula, que se
                # conserva reconstruible. Devolver el original sin comparar
                # —mi primera versión— replayaba el recibo de otra petición
                # ante CUALQUIER cuerpo: idempotencia convertida en confusión.
                vieja = int(prev["req_hash_v"])
                if vieja != REQ_HASH_V:
                    # Con SU ledger: el de v1 es el crudo del argumento.
                    esperado = _req_hash(vieja, intent=intent, ledger_raw=ledger,
                                         ledger_efectivo=destino, causes=causes,
                                         external_causes=external_causes,
                                         fenced_resource=fenced_resource,
                                         fencing_token=fencing_token)
                else:
                    esperado = req_hash
                if prev["req_hash"] != esperado:
                    raise IdempotencyConflict(
                        "misma clave de idempotencia con cuerpo distinto")
                ev = con.execute("SELECT occurred_at, payload_sha FROM events"
                                 " WHERE event_id=?", (prev["event_id"],)).fetchone()
                return Acceptance(prev["event_id"], prev["receipt_id"], True,
                                  ev["occurred_at"], ev["payload_sha"])

            # ── BARRERA DE ADMISIÓN ────────────────────────────────────────
            # DESPUÉS del replay, y es una decisión, no un descuido: un reintento
            # de algo YA ACEPTADO devuelve su recibo aunque la puerta esté
            # cerrada. La barrera cierra la ENTRADA de escrituras nuevas, no la
            # RESPUESTA a una que ya entró: negar el replay obligaría al cliente
            # a inventarse otra clave para averiguar qué pasó con la primera, que
            # es exactamente la trampa que la idempotencia existe para no poner.
            # Y ANTES de la valla y de las causas: la puerta es la comprobación
            # más externa, y con ella cerrada no hay nada que validar.
            self._exigir_admision_locked(con, view.lane, "events.accept")

            # Las causas NATIVAS tienen que ser eventos nativos. Un hash de
            # bridge presentado como causa nativa se RECHAZA y se recibe
            # (ADR-001 §Commands, causality and supersession).
            if fenced_resource is not None:
                self._fence_locked(con, view, fenced_resource, fencing_token)
            # …y del MISMO CARRIL. Una causa cruzada convertiría el grafo causal
            # en un puente entre carriles: el aislamiento no puede depender de
            # que nadie cite hacia fuera.
            for c in causes:
                hay = con.execute("SELECT lane FROM events WHERE event_id=?",
                                  (c,)).fetchone()
                if hay is None:
                    raise CauseRejected(
                        f"`{c}` no es un evento nativo: la causalidad hacia material "
                        f"bridge va por `external_causes`, que no otorga autoridad")
                if hay["lane"] != view.lane:
                    raise CauseRejected(
                        f"`{c}` es de otro carril: la causalidad nativa no cruza carriles")

            event_id = _new_id("evt")
            payload_sha = _sha256(_canonical({
                "intent": dict(intent), "principal": view.principal, "role": view.role,
                "lane": view.lane, "runtime_instance": view.runtime_instance,
                "occurred_at": occurred_at}))
            # Correlación no autorizante y por aceptación. No entra en el hash de
            # idempotencia: un retry del mismo efecto puede nacer en otro span y
            # tiene que recuperar el receipt original, no crear otro evento.
            atestacion = dict(self._attestation)
            if trace:
                atestacion["trace"] = dict(trace)
            con.execute(
                "INSERT INTO events(event_id,lane,verb,kind,head,body,recipients,"
                "recipients_roles,recipients_broadcast,intent,"
                "principal_id,role,runtime_instance,occurred_at,payload_sha,attestation)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, view.lane, verb, _kind,
                 str(intent.get("head", "")), str(intent.get("body", "")),
                 _canonical(_dest_lit), _canonical(_dest_roles),
                 _canonical(_dest_bcast), _canonical(dict(intent)),
                 view.principal_id, view.role, view.runtime_instance, occurred_at,
                 payload_sha, _canonical(atestacion)))
            receipt_id = self._open_receipt(con, "event", event_id, view.principal_id,
                                            view.lane, "accepted", occurred_at)
            con.execute("INSERT INTO outbox(event_id,ledger,next_attempt,created_at)"
                        " VALUES(?,?,?,?)", (event_id, destino, now, occurred_at))
            for c in causes:
                con.execute("INSERT OR IGNORE INTO event_causes(event_id,cause_event_id)"
                            " VALUES(?,?)", (event_id, c))
            for c in external_causes:
                # Un `KeyError` aquí era un 500 sin auditar: el llamante manda la
                # forma, así que validarla es parte del contrato, no una
                # comodidad.
                if not isinstance(c, Mapping) or not c.get("ledger") \
                        or not c.get("entry_eid"):
                    raise CauseRejected(
                        "una causa externa necesita `ledger` y `entry_eid`")
                con.execute(
                    "INSERT OR IGNORE INTO external_causes(child_kind,child_id,ledger,"
                    "entry_eid,noted_at) VALUES('event',?,?,?,?)",
                    (event_id, c["ledger"], c["entry_eid"], occurred_at))
            con.execute(
                "INSERT INTO idempotency(principal_id,lane,verb,key,req_hash,"
                "req_hash_v,event_id,receipt_id,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (view.principal_id, view.lane, verb, idempotency_key, req_hash,
                 REQ_HASH_V, event_id, receipt_id, occurred_at))
        return Acceptance(event_id, receipt_id, False, occurred_at, payload_sha)

    def _ledger_derivado(self, lane: str, pedido: str | None) -> str:
        """Destino permitido para un carril. FAIL-CLOSED en los dos sentidos.

        Sin allowlist configurada no escribe NADIE: `None` no significa «todo
        vale», significa que nadie la configuró y por tanto nada está
        autorizado. Y con un solo ledger por carril el destino se DERIVA, así
        que el camino normal ni siquiera acepta el dato del cliente.
        """
        permitidos = self._lane_ledgers.get(lane)
        if not permitidos:
            raise LedgerNotAllowed(
                f"el carril `{lane}` no tiene ledgers permitidos: sin allowlist "
                f"no se escribe (fail-closed)")
        if pedido is None:
            if len(permitidos) == 1:
                return permitidos[0]
            raise LedgerNotAllowed(
                f"el carril `{lane}` tiene {len(permitidos)} destinos: hay que "
                f"nombrar uno")
        if pedido not in permitidos:
            raise LedgerNotAllowed(
                f"`{pedido}` no está permitido para el carril `{lane}`")
        return pedido

    # ── recibos ──────────────────────────────────────────────────────────────
    def _open_receipt(self, con: sqlite3.Connection, kind: str, subject_id: str,
                      principal_id: str | None, lane: str | None, state: str,
                      at: str) -> str:
        receipt_id = _new_id("rcp")
        con.execute(
            "INSERT INTO receipts(receipt_id,subject_kind,subject_id,principal_id,lane,"
            "current_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (receipt_id, kind, subject_id, principal_id, lane, state, at, at))
        con.execute(
            "INSERT INTO receipt_transitions(transition_id,receipt_id,seq,state,at)"
            " VALUES(?,?,1,?,?)", (_new_id("trn"), receipt_id, state, at))
        return receipt_id

    _ESTADOS_EVENTO = ("accepted", "materialized", "indexed", "delivered",
                       "delivery_progress", "materialization_failed",
                       "materialization_exhausted", "materialization_repaired")

    def _append_transition(self, receipt_id: str, state: str,
                           detail: Mapping[str, Any] | None = None) -> str:
        """Añade una transición. NUNCA sobrescribe la anterior.

        PRIVADO a propósito. Como API pública era un agujero: sin autenticar,
        aceptaba cualquier cadena como estado y escribía en el recibo sin tocar
        la tabla de dominio — o sea, permitía dejar `commands.state` y su recibo
        contando cosas distintas. Un recibo que diverge de lo que gobierna es
        peor que no tener recibo: se lee como prueba.
        """
        self._guard_mutable()
        if state not in self._ESTADOS_EVENTO:
            raise CommandTransitionInvalid(
                f"`{state}` no es un estado de recibo de evento")
        at = _now_iso(self._clock())
        with self._tx() as con:
            row = con.execute("SELECT MAX(seq) s FROM receipt_transitions"
                              " WHERE receipt_id=?", (receipt_id,)).fetchone()
            seq = (row["s"] or 0) + 1
            tid = _new_id("trn")
            con.execute("INSERT INTO receipt_transitions(transition_id,receipt_id,seq,"
                        "state,at,detail) VALUES(?,?,?,?,?,?)",
                        (tid, receipt_id, seq, state,
                         at, _canonical(dict(detail or {}))))
            con.execute("UPDATE receipts SET current_state=?, updated_at=?"
                        " WHERE receipt_id=?", (state, at, receipt_id))
        return tid

    # Las tres lecturas de recibo van AUTENTICADAS y acotadas por carril. Sin
    # sesión eran una superficie de lectura cruzada: cualquiera con acceso al
    # objeto leía el recibo de otro carril, y un recibo lleva quién escribió qué,
    # a quién y cuándo. Las versiones internas (`_…`) son para el propio módulo,
    # que ya está dentro de la frontera.
    def _receipt(self, receipt_id: str) -> dict | None:
        with self._lectura() as (con, _):
            row = con.execute("SELECT * FROM receipts WHERE receipt_id=?",
                              (receipt_id,)).fetchone()
            return dict(row) if row else None

    def _receipt_for_event(self, event_id: str) -> dict | None:
        with self._lectura() as (con, _):
            row = con.execute(
                "SELECT * FROM receipts WHERE subject_kind='event' AND subject_id=?",
                (event_id,)).fetchone()
            return dict(row) if row else None

    def _transitions(self, receipt_id: str) -> list[dict]:
        with self._lectura() as (con, _):
            return [dict(r) for r in con.execute(
                "SELECT * FROM receipt_transitions WHERE receipt_id=? ORDER BY seq",
                (receipt_id,))]

    @contextlib.contextmanager
    def _lectura(self):
        """Transacción de LECTURA (`BEGIN` diferida) con la sesión resuelta dentro.

        Diferida y no `IMMEDIATE`: da la instantánea consistente que hace falta
        para que auth y consulta vean lo mismo, y NO exige poder escribir — si no,
        leer dejaría de funcionar en el modo degradado.
        """
        # LA GARANTÍA CUBRE TODA OPERACIÓN SQLite, no sólo la apertura y la
        # mutación: una lectura corre sobre una conexión CACHEADA, y un
        # `dispose()`/re-foto de otra hebra le quita el respaldo por debajo.
        with self._cerrojo_escritor():
            con = self._connect()
            self._verificar_identidad_operacional(con)
            con.execute("BEGIN")
            try:
                yield con, None
            finally:
                # El COMMIT va DENTRO del cerrojo: soltarlo antes dejaría la
                # transacción viva sin exclusión frente al ciclo de vida, que es
                # justo la ventana que esto cierra.
                try:
                    self._verificar_identidad_operacional(con)
                    con.execute("COMMIT")
                except sqlite3.OperationalError:
                    pass

    def _leer_recibo(self, token: str, *, receipt_id=None, event_id=None) -> dict:
        """Lectura de recibo: autenticación y consulta EN LA MISMA transacción, y
        el carril DENTRO del SQL.

        Dos cosas que no son cosmética:

        · **El carril va en el `WHERE`.** Traer la fila entera y comparar en
          Python significa que el proceso YA leyó el recibo ajeno; que después
          decida no devolverlo protege al cliente, no al dato.
        · **Otro carril es INDISTINGUIBLE de un id inexistente.** Con errores
          distintos, quien prueba identificadores aprende cuáles existen en
          carriles que no son suyos — un oráculo de enumeración construido con
          el mensaje de error, que es donde nadie lo busca.

        Y la lectura va en transacción con la autenticación: entre un `SELECT` de
        sesión y otro de recibo cabe una revocación entera.
        """
        # Transacción de LECTURA (`BEGIN` diferida), no `IMMEDIATE`: da la
        # instantánea consistente que hace falta para que auth y consulta vean lo
        # mismo, y NO exige poder escribir — si no, leer un recibo dejaría de
        # funcionar en el modo degradado, que existe justo para conservar las
        # lecturas.
        with self._lectura() as (con, _):
            view = _authenticate_locked(con, token, self._clock())
            if view is None:
                raise AuthError("se requiere sesión de runtime válida")
            self._tras_precheck()      # seam: aquí se prueba el TOCTOU de lectura
            # `lane=?` ESTRICTO. El `OR lane IS NULL` era una puerta abierta a
            # todo recibo sin carril desde cualquier sesión: una fila con el campo
            # a nulo —por un camino nuevo, por una migración— se volvía legible
            # para toda la flota sin que nadie tocara esta consulta. Si alguna vez
            # hace falta leer lo que no tiene carril, eso es una API de operador
            # con su capacidad, no una relajación de ésta.
            if receipt_id is not None:
                fila = con.execute(
                    "SELECT * FROM receipts WHERE receipt_id=? AND lane=?",
                    (receipt_id, view.lane)).fetchone()
            else:
                fila = con.execute(
                    "SELECT * FROM receipts WHERE subject_kind='event'"
                    "   AND subject_id=? AND lane=?",
                    (event_id, view.lane)).fetchone()
            if fila is None:
                raise SubjectNotFound("no hay recibo con ese identificador")
            rc = dict(fila)
            rc["_transitions"] = [dict(r) for r in con.execute(
                "SELECT * FROM receipt_transitions WHERE receipt_id=? ORDER BY seq",
                (rc["receipt_id"],))]
            return rc

    # SIN `@_audita`, y no es un olvido: el auditor ESCRIBE el recibo del
    # rechazo, así que decorar una LECTURA hacía que un `SubjectNotFound` —el
    # caso más común, un id que no existe— intentara escribir. Sobre un journal
    # de sólo lectura eso convierte «no encontrado» en `JournalReadOnly`: el modo
    # degradado, que existe para conservar las lecturas, se rompía al fallar una.
    def receipt(self, token: str, receipt_id: str) -> dict:
        # `_transitions` es andamiaje interno del lector: sacarlo por la API
        # publicaría una clave con guion bajo que nadie documentó y que el día
        # que se quite rompe a quien se fió de ella.
        rc = self._leer_recibo(token, receipt_id=receipt_id)
        rc.pop("_transitions", None)
        return rc

    def receipt_for_event(self, token: str, event_id: str) -> dict:
        rc = self._leer_recibo(token, event_id=event_id)
        rc.pop("_transitions", None)
        return rc

    def transitions(self, token: str, receipt_id: str) -> list[dict]:
        """Transiciones de UN recibo del carril de quien pregunta.

        El `JOIN` con `receipts` va en ESTA consulta, no en una comprobación
        previa: leer las transiciones por `receipt_id` a secas después de haber
        validado en otra sentencia deja la puerta abierta a que alguien llame
        directo, y además reparte el filtro entre dos sitios que hay que acordarse
        de mantener juntos.
        """
        with self._lectura() as (con, _):
            view = _authenticate_locked(con, token, self._clock())
            if view is None:
                raise AuthError("se requiere sesión de runtime válida")
            self._tras_precheck()
            filas = con.execute(
                "SELECT t.* FROM receipt_transitions t"
                "  JOIN receipts r ON r.receipt_id = t.receipt_id"
                " WHERE r.receipt_id=? AND r.lane=? ORDER BY t.seq",
                (receipt_id, view.lane)).fetchall()
            if not filas and con.execute(
                    "SELECT 1 FROM receipts WHERE receipt_id=? AND lane=?",
                    (receipt_id, view.lane)).fetchone() is None:
                raise SubjectNotFound("no hay recibo con ese identificador")
            return [dict(f) for f in filas]

    # ── outbox ───────────────────────────────────────────────────────────────
    @_audita
    def claim_outbox(self, token: str, *, lease_s: int = 60) -> ProjectionJob | None:
        """Arrienda UN item pendiente. El arriendo va en su propia transacción y
        se SUELTA antes de que el trabajador toque el fichero: un `flock`+`fsync`
        dentro de una transacción de SQLite convierte la latencia de disco en
        bloqueo de toda la flota."""
        self._guard_mutable()
        if (type(lease_s) is not int
                or not 1 <= lease_s <= MAX_OUTBOX_LEASE_S):
            raise OperationInvalid(
                f"lease_s debe ser un entero entre 1 y {MAX_OUTBOX_LEASE_S}")
        with self._tx() as con:
            # La hora se toma DESPUÉS de adquirir el writer lock. Tomarla antes
            # permitía esperar por otra transacción y acuñar un lease que ya
            # estaba vencido cuando nacía. La misma foto autoriza la sesión,
            # selecciona el trabajo y calcula el vencimiento.
            now = self._clock()
            view = _authenticate_locked(con, token, now)
            if view is None:
                raise AuthError("se requiere sesión de runtime válida")
            self._exigir_capacidad_locked(con, view, CAP_OUTBOX_WORKER,
                                          "claim_outbox")
            # `lease_by` se DERIVA de la sesión. Como argumento era una etiqueta
            # que el llamante se pone a sí mismo: dos trabajadores podían decir
            # el mismo nombre, o uno podía decir el del otro, y el arriendo dejaba
            # de identificar a nadie. Y sólo se reclama trabajo del PROPIO carril.
            row = con.execute(
                "SELECT o.event_id, o.ledger, o.attempts FROM outbox o"
                "  JOIN events e USING (event_id)"
                " WHERE o.state='pending' AND o.next_attempt<=?"
                "   AND (o.lease_until IS NULL OR o.lease_until<?)"
                "   AND e.lane=?"
                " ORDER BY o.n LIMIT 1", (now, now, view.lane)).fetchone()
            if row is None:
                return None
            claim = _new_id("clm")
            con.execute("UPDATE outbox SET lease_until=?, lease_by=?, claim_token=?,"
                        " attempts=attempts+1 WHERE event_id=?",
                        (now + lease_s, view.runtime_instance, claim, row["event_id"]))
            ev = con.execute(
                "SELECT e.*, p.principal, r.receipt_id,"
                "       COALESCE((SELECT b.principal_source"
                "                   FROM credential_bindings b"
                "                  WHERE b.principal_id=e.principal_id"
                "                  ORDER BY b.bound_at LIMIT 1),"
                "                'derived_from_role') AS principal_source"
                "  FROM events e"
                "  JOIN principals p USING (principal_id)"
                "  JOIN receipts r ON r.subject_kind='event'"
                "       AND r.subject_id=e.event_id"
                " WHERE e.event_id=?", (row["event_id"],)).fetchone()
            attestation = json.loads(ev["attestation"])
        return ProjectionJob(
            event_id=row["event_id"], ledger=row["ledger"],
            attempts=row["attempts"] + 1, claim_token=claim,
            receipt_id=ev["receipt_id"],
            recipients=tuple(json.loads(ev["recipients"])),
            occurred_at=ev["occurred_at"], payload_sha256=ev["payload_sha"],
            attestation=attestation,
            trace=attestation.get("trace") if isinstance(attestation, dict) else None,
            principal_id=ev["principal_id"], principal=ev["principal"],
            principal_source=ev["principal_source"],
            role=ev["role"], lane=ev["lane"],
            runtime_instance=ev["runtime_instance"],
            intent=json.loads(ev["intent"]))

    # ── LOS TRES HECHOS QUE NO SON EL MISMO ─────────────────────────────────
    # `materialized` = el append aterrizó en el markdown.  Lo sabe el trabajador.
    # `indexed`      = el índice de búsqueda lo ve.        Lo sabe el indexador.
    # `delivered`    = un destinatario lo consumió.        Lo sabe el ACK.
    # Cada uno lo afirma quien MIDE su efecto, y ninguno se puede adelantar: la
    # cadena se comprueba, no se supone. Fundirlos era exactamente el falso verde
    # que `accepted != materialized` existe para impedir, un piso más abajo.
    # `delivered` NO está aquí: no es un paso más de una cadena lineal, es el
    # agregado de N acuses independientes. Ver `mark_delivered`.
    # Profundidad máxima que se INSPECCIONA. No es un ajuste de rendimiento: sin
    # tope, un cuerpo anidado a 10.000 niveles tumba el proceso por recursión en
    # vez de rechazarlo. Y el corte NO puede caer a «acepto lo que no pude
    # mirar»: pasado el tope se RECHAZA, porque lo que no se inspecciona entero
    # no entra en un registro durable.
    _PROFUNDIDAD_MAX = 12

    # Claves que pone el SERVIDOR. A cualquier profundidad: la auditoría midió
    # `intent={"meta":{"principal":"cto"}}` aceptado y ESCRITO en el registro
    # autoritativo. No eleva autoridad —la atribución real sigue saliendo de la
    # sesión— pero el ADR pide rechazo, no ignorado silencioso, y quien luego
    # renderice `intent` puede publicar un autor forjado.
    # Alias que NO son variantes tipográficas sino OTRA palabra para lo mismo.
    # `carril` y `rol` son el castellano que esta casa usa en todas partes —
    # `CARRIL_OBLIGATORIO`, `X-Llminbox-Carril`, `rol_de()`— así que un cliente
    # que los escriba no está probando el filtro: está escribiendo natural.
    # ⚠️ LOS VALORES VAN YA NORMALIZADOS. Mi primera versión mapeaba
    # `"actorid" -> "principal_id"`, o sea a la forma BONITA, y el conjunto
    # contra el que se compara está en forma NORMALIZADA (`principalid`): el
    # alias apuntaba a algo que no existe ahí, así que `actorId` COLABA. Un
    # alias mal apuntado es un fail-open que se lee como cobertura.
    # `_los_alias_apuntan_a_una_reservada_real` lo fija.
    #
    # 🔻 `agente` y `credencial` entran por el MISMO motivo que `carril` y `rol`:
    # son el castellano que esta casa escribe en todas partes —`agent=` es un
    # parámetro vivo del gateway y `credencial` sale en la prosa del ADR—, así
    # que un cliente que los mande no está probando el filtro, está escribiendo
    # natural. Apuntan a la forma NORMALIZADA de una reservada REAL (`agent`,
    # `credential`), que es lo que `_los_alias_apuntan_a_una_reservada_real`
    # exige: un alias que apunta a algo ausente del conjunto es un fail-open que
    # se lee como cobertura.
    _ALIAS_RESERVADAS = {"carril": "lane", "rol": "role", "actorid": "principalid",
                         "agente": "agent", "credencial": "credential"}

    @classmethod
    def _clave_norm(cls, k: Any) -> str:
        """LA autoridad de forma de una clave. Una, y pública por `clave_reservada`.

        Comparar `str(k) in RESERVADAS` mide la forma que YO escribí, no la que
        el cliente manda: `principalId`, `Role`, `carril`, `runtime-instance`,
        `PRINCIPAL`, `role_`, `'lane '` y `Runtime_Instance` pasaban las ocho
        (medido sobre el freeze `32c3646`, `8` de `8` aceptadas Y PERSISTIDAS).

        Se normaliza en cuatro ejes y luego se resuelve el alias:
          · NFKD y fuera los diacríticos  (`Lané` -> `lane`)
          · minúsculas                    (`PRINCIPAL`, `Role`)
          · fuera TODO lo que no sea `[a-z0-9]` — separadores, espacios y el
            camelCase se funden       (`principal_id` == `principalId` == `principal-id`)
          · alias de idioma               (`carril` -> `lane`, `rol` -> `role`)

        ⚠️ Es IGUALDAD sobre el normalizado, nunca subcadena ni prefijo: con
        `in` o `startswith`, `principal_count` y `roles` se rechazarían y la
        guarda rompería el uso legítimo. Ese ⊕ está en la suite y es la mitad
        que impide cerrar de más.
        """
        t = unicodedata.normalize("NFKD", str(k)).lower()
        t = "".join(c for c in t if not unicodedata.combining(c))
        t = re.sub(r"[^a-z0-9]+", "", t)
        return cls._ALIAS_RESERVADAS.get(t, t)

    @classmethod
    @functools.lru_cache(maxsize=1)
    def _reservadas_norm(cls) -> frozenset:
        """DERIVADO de `_RESERVADAS` con la MISMA función, y PEREZOSO.

        La primera versión rellenaba un atributo de clase al final del módulo.
        Funcionaba, y era un fail-OPEN esperando: si esa línea no llegara a
        ejecutarse —un `import` parcial, alguien moviéndola, un refactor— el
        conjunto quedaría VACÍO y `clave_reservada()` diría `False` a TODO, sin
        un solo error. Una guarda que se apaga en silencio es peor que no
        tenerla. Derivarlo aquí no puede quedarse a medias.
        """
        return frozenset(cls._clave_norm(k) for k in cls._RESERVADAS)

    # ── EL ALFABETO DE LAS CLAVES · `D19`, CERRADO ────────────────────────
    # Adjudicación de `@cpo` (`00:25:21Z`) y CIERRE de `@cto` (`00:38:45Z`) sobre
    # tres rulings convergentes. `@backend` implementa; la decisión no es mía.
    #
    # ┌ ① reservadas         -> ATTRIBUTION_REJECTED  (ES atribución)
    # │ ② fuera de [\x20-\x7E] tras NFKD -> KEY_CHARSET_REJECTED, código PROPIO
    # └ ③ el resto de ASCII imprimible se ACEPTA: `$ @ : / + , ;` y demás
    #
    # 🔻 LA LISTA DE 4 SEPARADORES QUEDA RETIRADA. Era mía y estaba de más:
    # contra ASCII no aportaba nada —el colapso de separadores ya lo hacía
    # `_clave_norm`— y rechazaba `8`-`9` formas estándar (`$schema`, `@type`,
    # `@context`, `@id`, `xml:lang`, `ns:campo`, `a/b`, `a+b`, `foo[0]`) con un
    # código que MENTÍA sobre el motivo.
    #
    # ⚖️ Y NO revoca el principio que me costó tres rondas —`@cpo` lo dice mejor
    # que yo y lo apunto para no volver a confundirlos—: **ASCII imprimible NO es
    # un predicado**. Son `95` caracteres, enumerables, y su definición **no puede
    # crecer**: ASCII está congelado desde 1968. El test de bolsillo sigue dando
    # SÍ — puedo imprimir el conjunto entero. Lo que SÍ sería un predicado
    # abierto es `str.isprintable()`, que le pregunta a la tabla de Unicode, y
    # queda fuera POR ESE MISMO ARGUMENTO.
    #
    # 🩸 Y el rango se escribe como RANGO, no como `c.isascii()`. Es la trampa que
    # `@cto` marcó como falsador obligatorio: `'a\tb'.isascii()` es `True`, así
    # que con `isascii()` el tabulador, el salto de línea y el `NUL` **entrarían**
    # y la implementación saldría VERDE con el hueco abierto. Lo que separa
    # «imprimible» de «ASCII» es justo ese brazo, y tiene sus tres tests.
    _ASCII_IMPRIMIBLE = frozenset(chr(c) for c in range(0x20, 0x7F))   # 95

    @classmethod
    def alfabeto_de_claves(cls) -> frozenset:
        """El conjunto vivo, público y ENUMERABLE. `95` elementos."""
        return cls._ASCII_IMPRIMIBLE

    # ── EL DETALLE QUE SALE DEL NÚCLEO · una sola puerta ──────────────────
    # Topes. La cardinalidad del texto NO la elige quien ataca: es la misma
    # doctrina que `REASON_CODES` aplica a los códigos, un piso más abajo.
    _CLAVE_EN_MENSAJE_MAX = 64      # el trozo de CLAVE
    _CONTEXTO_EN_MENSAJE_MAX = 96   # el trozo de CONTEXTO (`donde`)
    _DETALLE_MAX = DETALLE_MAX      # DERIVADA: una sola verdad, la del modulo
    _MARCA_TRUNCADO = MARCA_TRUNCADO   # DERIVADA, idem

    @classmethod
    def detalle_seguro(cls, texto: Any, *, tope: int | None = None) -> str:
        """DELEGA en `_saneado`, la del modulo. **No reimplementa nada.**

        🩸 Habia DOS sanitizadores —este y `_saneado`— con DOS pares de
        constantes que hoy coincidian por casualidad. Dos verdades es como
        `/append` acabo aceptando `8` de `12` tipos: el dia que alguien toque una
        y no la otra, la garantia se parte por la mitad y las dos se leen bien.
        Se conserva el nombre porque es API publica y lo citan tres freezes.
        """
        return _saneado(texto, tope)

    @classmethod
    def clave_para_mensaje(cls, k: Any) -> str:
        """La clave, para un mensaje. Delega en `detalle_seguro` con SU tope."""
        return cls.detalle_seguro(k, tope=cls._CLAVE_EN_MENSAJE_MAX)


    @classmethod
    def clave_fuera_del_alfabeto(cls, k: Any) -> str | None:
        """Devuelve el CARÁCTER ofensor, o `None` si la clave es admisible.

        🔴 EL AGUJERO QUE CIERRA, medido sobre `811f635` (`8` de `8` aceptadas Y
        PERSISTIDAS): `_clave_norm` quita todo lo que no sea `[a-z0-9]`, y esa
        clase **no distingue un SEPARADOR de una LETRA de otro alfabeto**. Una
        `а` cirílica (`U+0430`) no es un separador: es una letra que el ojo lee
        como `a`. Al borrarla, la clave DEJA DE CASAR y entra:

            'аgent'       -> 'gent'        'attributіon' -> 'attributon'
            'agеnt'       -> 'agnt'        'runtіme'     -> 'runtme'
            'аgente'      -> 'gente'       'credentіal'  -> 'credental'
            'рrincipal'   -> 'rincipal'    'credеncial'  -> 'credncial'

        ⚖️ **Y yo declaré este hueco como CERRADO POR IMPOSIBILIDAD**, con un test
        que exigía que `рrincipal` PASARA y una nota diciendo que cubrirlo
        «exige la tabla de confusables de UTS#39, que NO está en la stdlib».
        **Era falso, y de la peor manera: convertí el límite de MI enfoque en una
        ley del mundo.** No hace falta ninguna tabla de confusables — hace falta
        decir qué alfabeto se ADMITE. Eso es stdlib y son seis líneas. El test
        que fijaba el límite queda REVOCADO, no borrado en silencio.

        Se mira DESPUÉS de NFKD y de quitar diacríticos —y ANTES de que
        `_clave_norm` borre nada—, que es la única posición donde discrimina:
        `Lané`, `cañón`, `señal` y las formas de ancho completo (`ｎota`) pasan
        porque **decomponen a ASCII**; el carácter disfrazado se ve todavía
        entero. Un carácter que sobrevive a NFKD y no está en `_ASCII_IMPRIMIBLE` no
        entra, **sea lo que sea**: letra de otro alfabeto, símbolo, emoji,
        marca de formato invisible, **carácter de control ASCII** o algo que
        Unicode aún no ha inventado.

        ⚠️ **COSTE DECLARADO tras `D19`**: fuera de `[0x20-0x7E]` no entra nada.
        `+`, `:` y `/` **SÍ se admiten** —son ASCII imprimible— y esta nota decía
        lo contrario hasta `D19`; queda corregida. Lo que sigue fuera: `straße`
        (la `ß` no decompone en NFKD), `U+200B`, `U+2010`, y **todo ASCII de
        CONTROL** (`\t`, `\n`, `\x00`, `\x1b`, `DEL`), que es lo que separa
        «imprimible» de «ASCII» y el falsador obligatorio de `@cto`.
        """
        t = unicodedata.normalize("NFKD", str(k))
        t = "".join(c for c in t if not unicodedata.combining(c))
        for c in t:
            if c not in cls._ASCII_IMPRIMIBLE:
                return c
        return None

    @classmethod
    def clave_reservada(cls, k: Any) -> bool:
        """Pública a propósito. El gateway tiene HOY su propio `_alias` (medido
        por `@contratosbik`), y dos normalizaciones son dos verdades: la de
        arriba tiene que desaparecer consumiendo ésta, no convivir con ella."""
        return cls._clave_norm(k) in cls._reservadas_norm()

    _RESERVADAS = frozenset((
        "actor", "principal", "principal_id", "principal_source", "source",
        "role", "lane", "runtime_instance", "capabilities", "attestation",
        "command_id", "workstream_id", "revision", "state", "supersedes",
        "created_at", "attribution_status", "event_id", "receipt_id",
        "occurred_at", "payload_sha",
        # ── las CUATRO que faltaban, y por qué cada una ──────────────────────
        # El conjunto tenía la forma COMPUESTA de cada concepto y no la RAÍZ, y
        # la raíz es justo la palabra que alguien escribe a mano:
        #   · `actor` estaba, `agent` NO — y `agent` es el nombre que el gateway
        #     usa en `/inbox`, `/claim` y la tabla `cursors`. La palabra con la
        #     que este sistema NOMBRA a un sujeto no puede venir del cuerpo.
        #   · `attribution_status` estaba, `attribution` NO — se podía mandar el
        #     concepto entero mientras se filtraba sólo su campo de estado.
        #   · `runtime_instance` estaba, `runtime` NO.
        #   · `capabilities` estaba, `credential` NO — y la credencial es la
        #     ENTRADA de la que sale toda la identidad: es la más grave de las
        #     cuatro para dejar que la escriba el cliente.
        # No elevan autoridad (la atribución real sigue saliendo de la sesión),
        # pero el ADR pide RECHAZO y no ignorado silencioso, y quien renderice
        # `intent` después publica el autor que venga escrito ahí.
        "agent", "attribution", "runtime", "credential"))


    @classmethod
    def _sin_atribucion(cls, valor: Any, donde: str, prof: int = 0) -> None:
        """Rechaza atribución a CUALQUIER profundidad, listas incluidas.

        Las listas van explícitas porque son el agujero clásico de una recursión
        que sólo mira dicts: `{"meta": [{"role": "cto"}]}` se cuela entera.
        """
        # ⚖️ SEGUNDA GUARDA DE PROFUNDIDAD — DECLARADA **DEFENSA EN PROFUNDIDAD
        # REAL**, y con la medida que lo sostiene (encargo del operador; cierra
        # los dos abiertos de `@qa` sobre el acta de los supervivientes).
        #
        # La PRIMERA es la del congelador (`_congela_valor`, `prof_max` derivado
        # de ESTE MISMO simbolo por `D5`), y corre ANTES: `_congelar(intent…)`
        # esta en `accept_event` unas lineas por encima de `_sin_atribucion`.
        # Con las dos vivas, la ruta publica NO las distingue —misma clase, misma
        # `dimension`—, y de ahi salio la lectura anterior de que esta guarda solo
        # podia falsarse en aislado. **Era falsa**: neutralizando SOLO el
        # `prof_max` del congelador, la peticion SIGUE muriendo por la ruta
        # publica, y el `field` dice quien la mato:
        #
        #     sano                     ->  field='intent'                       (congelador, PLANO)
        #     congelador neutralizado  ->  field='intent.meta.n.n.n.n.n.n.n.n'  (aqui, CON CAMINO)
        #
        # ⇒ ALCANZABLE y falla CERRADO. **NO se elimina la redundancia**: el
        # contrato fija `profundidad 12 POR CAMPO`, no CUANTAS veces se comprueba;
        # las dos aplican EL MISMO limite del MISMO simbolo (no hay dos verdades
        # que puedan divergir); y quitarla dejaria el rechazo dependiendo de que
        # el congelador corra primero — eso es ORDEN DE LLAMADAS, no contrato.
        # ⛔ Ningun limite se toca. Falsadores en `SUITE_P1`:
        # `test_ALCANZABILIDAD_si_cae_la_PRIMERA_guarda_la_SEGUNDA_rechaza_por_RUTA_PUBLICA`
        # con su control negativo (las DOS apagadas ⇒ ENTRA) y su linea base
        # (sano ⇒ `field` PLANO). Mutantes: `MP1-2-profundidad-fail-open`,
        # `MP1-2-congelador-sin-profundidad`, `MP1-2-un-TERCERO-rechaza-el-cuerpo-hondo`.
        if prof > cls._PROFUNDIDAD_MAX:
            # Por el MISMO embudo que los otros dos. Este mensaje se me quedó
            # fuera y lo cazó el barrido, no yo: es exactamente el «campo de al
            # lado» que la función central existe para que no exista.
            # 🔻 `ResourceLimitExceeded` y NO `AttributionRejected`: la guarda existia y
            # MENTIA al rechazar — decia «la peticion traia su propia atribucion»
            # cuando lo que pasa es que ANIDA DEMASIADO y no hay ninguna
            # atribucion. Lo pide `@cto` en su §2 y es la misma clase que `D19`
            # cerro con `KeyCharsetRejected`.
            # 🔻 POR `_rle`, Y CON `field` CANONICO (hallazgo `@qa` §4 sobre
            # `bb5fad94`). Antes construia `ResourceLimitExceeded` a mano y
            # publicaba el CAMINO en `field` — fuera del enum cerrado, o sea
            # ampliando el contrato de error por accidente. `_rle` impone el
            # enum con `if`/`raise` (sobrevive a `python -O`) y es la UNICA
            # puerta por la que debe salir una RLE.
            # ⚖️ Un ATRIBUTO del contrato no puede llevar un valor que el
            # contrato no enumera. Y la ruta tampoco viaja por ningun otro canal:
            # ver el bloque de abajo y `_campo_raiz`.
            # 🩸 AQUI HABIA UN PARRAFO QUE AFIRMABA LO CONTRARIO —que la ruta
            # sobrevivia cambiando de canal— y el bloque de la linea siguiente ya
            # decia que se retira. **El fichero se contradecia en parrafos
            # CONSECUTIVOS**, residuo de la version en que el camino si viajaba.
            # Lo cazo `@qa` sobre `b02549b2`, NO mi `F-RUTA-5`: mi aguja era
            # case-sensitive y buscaba un verbo (`viaja`) donde ponia otro (`va`).
            # ⇒ el falsador se amplio a case-insensitive y a las dos formas del
            # verbo. **Una aguja que solo casa la variante que yo escribi mide mi
            # memoria, no el fichero.**
            # 🩸 EL CAMINO NO CABE, Y ESTA MEDIDO — no es que se me olvidara.
            # Primero intente meterlo en la cola (`camino=…`) y el mensaje salio
            # a `320` B CLAVADOS, o sea TRUNCADO, comiendose la cola fija de
            # `_rle`. La aritmetica, RECALCULADA termino a termino por `@cto`
            # (`RULING` sobre `d132f207`, R-2):
            #     `_rle` con cola VACIA ................... 218 B
            #     + cola SANO (81 B) ..... total 299 B ⇒ HOLGURA 21 B
            #     + cola 2a capa (96 B) .. total 314 B ⇒ MARGEN   6 B
            #     con el camino dentro ... total 403 B  > 320  ⇒ NO CABE
            # 🩸 Aqui decia **`11` B**, y ese numero NO lo produce ningun termino:
            # era residuo de una cola anterior que sobrevivio a su propio calculo
            # — un numero de otra poblacion viajando como si fuera del sujeto. Se
            # BORRA, no se re-deriva. (`_CONTEXTO_EN_MENSAJE_MAX = 96`; medido con
            # `campo="intent"`, `limit=12`, `seen_at_least=13`.)
            # Un diagnostico que SIEMPRE se
            # corta no es un diagnostico. ⇒ el camino se RETIRA del mensaje.
            #
            # 🔑 AL PRODUCTOR LO IDENTIFICA SU PROPIA FRASE, que es lo unico que
            # queda dentro del presupuesto y no toca nada del contrato: las dos
            # guardas de profundidad emiten `field`, `dimension`, `limit` y
            # `seen_at_least` IDENTICOS —**deben**, es el mismo limite del mismo
            # simbolo— y solo esta dice «segunda capa». La cola es prosa de
            # diagnostico; el contrato son los ATRIBUTOS.
            # ⚖️ Es mas debil que un atributo, y se dice: el contrato PROHIBE
            # distinguirlas por atributo, asi que la prosa es el unico canal
            # legitimo. Falsador: `SUITE_P1`,
            # `test_ALCANZABILIDAD_si_cae_la_PRIMERA_guarda_la_SEGUNDA_rechaza_por_RUTA_PUBLICA`.
            raise _rle("depth", _campo_raiz(donde), cls._PROFUNDIDAD_MAX, prof,
                       # ⚠️ COLA ACOTADA A OJO DE PRESUPUESTO, no a gusto: la
                       # HOLGURA desde el sano son `21` B (`299` de `320` ya
                       # gastados) y esta cola deja `6` B de MARGEN (`314`). Son
                       # DOS medidas distintas, no dos versiones de una. Una cola
                       # mas larga TRUNCA y se lleva por delante el aviso fijo de
                       # `_rle`, que NO es relleno: es la salvedad normativa de
                       # que `dimension` nombra UN limite. Falsador: `F-RUTA-3`.
                       "anida mas de lo que se inspecciona y lo no mirado no "
                       "entra; lo para la 2a capa, la de atribucion")
        if isinstance(valor, Mapping):
            for k_vivo, v in valor.items():
                # 🧊 SE CONGELA AQUÍ, UNA VEZ, ANTES DE VALIDAR NADA. A partir de
                # esta línea nadie vuelve a preguntarle al objeto qué es.
                #
                # 🔴 TOCTOU MEDIDO sobre `1f7c9b2`: una subclase de `str` cuyo
                # `__format__` devuelve una cosa mientras se valida y otra cuando
                # se pinta el mensaje inyectaba `LF` y `ESC` — el rechazo salía en
                # DOS líneas, tanto en `AttributionRejected` como en el contexto
                # `donde` de `KeyCharsetRejected`. El gancho real es `__format__`
                # y no `__str__`: un f-string sobre una subclase de `str` llama a
                # `__format__`, así que una sonda que sólo engancha `__str__` NO
                # lo ve (la mía no lo vio a la primera).
                # Con la lectura ÚNICA el atacante puede mentir una vez —y esa
                # mentira es la que se valida Y la que se pinta—, así que no hay
                # ventana entre las dos.
                k = str(k_vivo)
                # ⛔ ALFABETO ANTES QUE RESERVADA, y el orden importa: si se
                # mirara después, `аgent` ya habría normalizado a `gent` y
                # `clave_reservada` diría `False` con toda la razón. La guarda
                # tiene que ver la clave ANTES de que la normalización se coma la
                # letra que la disfraza.
                # `AttributionRejected` a propósito y no una clase nueva: el
                # motivo ES el mismo —una clave que puede ser atribución
                # disfrazada— y añadir una subclase movería el `31` y el mapa
                # `_POR_MOTIVO` que la pasarela va a IMPORTAR (`D10` de `@cto`).
                ofensor = cls.clave_fuera_del_alfabeto(k)
                if ofensor is not None:
                    raise KeyCharsetRejected(cls.detalle_seguro(
                        f"la clave {cls.clave_para_mensaje(k)} lleva "
                        f"{ascii(ofensor)} (U+{ord(ofensor):04X}), fuera del "
                        f"alfabeto admitido [0x20-0x7E] (ASCII imprimible) tras "
                        f"NFKD: un caracter de control no puede ir en una clave, y "
                        f"uno de otro alfabeto se lee igual que una reservada sin "
                        f"casar con ella. No entra, en "
                        f"{cls.detalle_seguro(donde, tope=cls._CONTEXTO_EN_MENSAJE_MAX)}"))
                if cls.clave_reservada(k):
                    raise AttributionRejected(cls.detalle_seguro(
                        f"la clave {cls.clave_para_mensaje(k)} la pone el "
                        f"servidor: una peticion que la trae se RECHAZA, no se "
                        f"ignora en silencio, en "
                        f"{cls.detalle_seguro(donde, tope=cls._CONTEXTO_EN_MENSAJE_MAX)}"))
                cls._sin_atribucion(v, f"{donde}.{k}", prof + 1)
        elif isinstance(valor, (list, tuple)):
            for i, v in enumerate(valor):
                cls._sin_atribucion(v, f"{donde}[{i}]", prof + 1)

    def _gramatica(self) -> "Grammar":
        if self._grammar is None:
            raise GrammarUnavailable(
                "sin autoridad de gramática inyectada (`grammar`) no puedo validar "
                "`kind`/`head`/`body`: aceptaría un registro DURABLE que no se "
                "puede proyectar. Cablea `Grammar(...)` con las funciones de "
                "`ledger_parse`, las mismas que usa `/append`")
        return self._grammar

    def _validar_gramatica(self, intent: Mapping[str, Any]) -> str:
        """Las CUATRO guardas de `/append`, por sus MISMAS funciones.

        Devuelve el `kind` CANÓNICO: guardar el crudo dejaría el filtro por tipo
        roto aunque la guarda existiera — `AMEND+MEDIDO` sale `tipo=None` Y
        `raw_tipo=None`, irrecuperable por filtro (medido por @infra y @sdet).
        """
        g = self._gramatica()
        semantic = ("canonical_kind" in intent or "kind_registry_rev" in intent)
        if semantic:
            canonical = intent.get("canonical_kind")
            rev = intent.get("kind_registry_rev")
            if "kind" in intent:
                raise GrammarRejected("kind legacy y canonical_kind no se pueden mezclar")
            if (g.canonical_agent_kind is None or type(canonical) is not str
                    or type(rev) is not int or isinstance(rev, bool)
                    or g.canonical_agent_kind(canonical, rev) != canonical):
                raise GrammarRejected(
                    "canonical_kind/kind_registry_rev no reproducen el registro inyectado")
            canonico = canonical
        else:
            canonico = g.canonical_kind(intent.get("kind"))
            if not canonico:
                raise GrammarRejected(
                    f"`kind`={intent.get('kind')!r} no es un tipo canónico del ledger: "
                    f"quedaría con `tipo=None` y `raw_tipo=None`, irrecuperable por filtro")
        head = str(intent.get("head", ""))
        if len(head) > g.head_max:
            raise GrammarRejected(
                f"`head` tiene {len(head)} caracteres y el máximo es {g.head_max}")
        if "\n" in head or "\r" in head:
            raise GrammarRejected(
                "`head` lleva un salto de línea: partiría la cabecera en dos")
        for campo in ("head", "body"):
            for i, linea in enumerate(str(intent.get(campo, "")).splitlines()):
                if g.opens_entry(linea):
                    raise GrammarRejected(
                        f"`{campo}` línea {i + 1} abre una cabecera de entrada "
                        f"({linea[:60]!r}): una sola llamada escribiría DOS entradas "
                        f"y la segunda llevaría la firma que tú escribas ahí. Si la "
                        f"estás citando, sángrala con un espacio, ponle '> ' delante "
                        f"o enciérrala en backticks")
        return canonico

    def _recurso(self, resource: str) -> str:
        """Normaliza para que pueda CHOCAR, y por la MISMA función que `/claim`.

        Sin esto `deploy` y `Deploy` son dos recursos con dos fencing tokens
        válidos: dos dueños del mismo trabajo, que es justo lo que el lease
        existe para impedir (medido por @contratosbik en la superficie nativa,
        no sólo en la compatibilidad).
        """
        norm = self._gramatica().normalize_resource(str(resource))
        if not norm:
            raise GrammarRejected(
                f"`{resource}` se normaliza a vacío: todos los nombres de puro "
                f"separador serían EL MISMO recurso, y uno vacío no nombra nada")
        return norm

    def _resolver_destinos(self, to: Sequence[Any]) -> tuple[list, list, list]:
        """`(roles_acusables, literales, difusiones)` — o se RECHAZA.

        El censo se INYECTA (`recipient_resolver`) y nunca se importa: este
        módulo no conoce `ledger_parse` ni `servicio`, y atarlo a ellos haría el
        journal indesplegable sin el servicio entero.

        FAIL-CLOSED en dos caras, y las dos existen por la misma medida de
        `@contratosbik` (`166` de `262` cabeceras vivas con ≥1 destinatario NO
        acusable ⇒ `delivery_progress` eterno):

          · sin censo  ⇒ no se puede prometer que el ACK sea alcanzable;
          · nombre que no resuelve ⇒ entrada que nadie podrá acusar nunca.

        En los dos casos se rechaza AL ACEPTAR, que es donde `/append` ya pone
        su `_indexable()`. Un rechazo tardío no evita la entrada huérfana.

        Los roles se DEDUPLICAN: `["backend","be"]` es UN destinatario. Sin esto
        el denominador del acuse sube por la puerta de al lado y `delivered`
        vuelve a ser inalcanzable — el mismo defecto por otro camino.
        """
        literales = [str(x) for x in to]
        if not literales:
            raise GrammarRejected(
                "`to` vacío: una entrada sin destinatario no la lee nadie")
        if self._recipient_resolver is None:
            raise RecipientUnresolved(
                "sin censo inyectado (`recipient_resolver`) no puedo resolver "
                "destinatarios: el ACK sería inalcanzable y el recibo se quedaría "
                "en `delivery_progress` para siempre. Inyecta el censo del "
                "despliegue o no mandes destinatarios")
        roles: list[str] = []
        difusion: list[str] = []
        for lit in literales:
            rol, es_difusion = self._recipient_resolver(lit)
            if es_difusion:
                difusion.append(lit)
                continue
            if not rol:
                raise RecipientUnresolved(
                    f"`{lit}` no resuelve en el censo y no es difusión: la entrada "
                    f"quedaría sin destinatario que pueda acusarla")
            if rol not in roles:
                roles.append(rol)
        return roles, literales, difusion

    _ORDEN = {"materialized": "accepted", "indexed": "materialized"}

    def _avanzar(self, token: str, event_id: str, estado: str, detalle: dict,
                 claim_token: str | None = None,
                 repair: ProjectionRepairEvidence | None = None) -> None:
        self._guard_mutable()
        with self._tx() as con:
            now = self._clock()
            at = _now_iso(now)
            view = _authenticate_locked(con, token, now)
            if view is None:
                raise AuthError("se requiere sesión de runtime válida")
            capacidad = CAP_OUTBOX_WORKER if estado == "materialized" else CAP_INDEXER
            self._exigir_capacidad_locked(con, view, capacidad,
                                          f"mark_{estado}")
            ev = con.execute("SELECT lane FROM events WHERE event_id=?",
                             (event_id,)).fetchone()
            if ev is None:
                raise SubjectNotFound(f"no existe el evento {event_id}")
            if ev["lane"] != view.lane:
                raise LedgerNotAllowed(
                    "el evento es de otro carril: tu sesión no lo gobierna")
            detalle = {**detalle, "by_principal": view.principal_id,
                       "by_runtime": view.runtime_instance}
            # TODO lo que decide se re-lee DENTRO de la transacción: el estado
            # previo y, para `materialized`, la ficha del arriendo. Comprobarlo
            # fuera dejaba la ventana en la que un trabajador RELEVADO vuelve y
            # marca lo que ya no es suyo.
            rc = con.execute("SELECT * FROM receipts WHERE subject_kind='event'"
                             " AND subject_id=?", (event_id,)).fetchone()
            if rc is None:
                raise SubjectNotFound(f"sin recibo para {event_id}")
            previo = self._ORDEN[estado]
            if rc["current_state"] != previo:
                raise ReceiptStateInvalid(
                    f"no puedo declarar `{estado}`: el recibo está en "
                    f"`{rc['current_state']}` y este paso exige `{previo}`")
            if estado == "materialized":
                ob = con.execute("SELECT claim_token, lease_until, ledger, lease_by"
                                 " FROM outbox WHERE event_id=?",
                                 (event_id,)).fetchone()
                if ob is None:
                    raise SubjectNotFound(f"sin item de outbox para {event_id}")
                if claim_token is None or ob["claim_token"] != claim_token:
                    raise FencingConflict(
                        "ficha de arriendo del outbox no vigente: te relevaron")
                # La ficha SOLA no basta: si se filtra, otro runtime la presenta
                # y marca trabajo ajeno. El arriendo tiene que ser de ESTA sesión.
                if ob["lease_by"] != view.runtime_instance:
                    raise FencingConflict(
                        "el arriendo es de otro runtime: la ficha no te hace dueño")
                if (ob["lease_until"] or 0) <= now:
                    raise FencingConflict(
                        "tu arriendo del outbox VENCIÓ: no puedes marcar nada")
                # El ledger tiene que ser el que el evento pidió. Sin esto, un
                # trabajador con un destino mal resuelto marca `materialized`
                # citando OTRO fichero, y el recibo diría que aterrizó donde
                # nadie escribió — un puntero de proyección falso, que es peor
                # que no tenerlo porque parece verificado.
                if ob["ledger"] != detalle.get("ledger"):
                    raise LedgerNotAllowed(
                        f"el ledger declarado ({detalle.get('ledger')!r}) no es el "
                        f"del outbox ({ob['ledger']!r})")
            seq = con.execute("SELECT MAX(seq) s FROM receipt_transitions"
                              " WHERE receipt_id=?", (rc["receipt_id"],)
                              ).fetchone()["s"] or 0
            if repair is not None:
                # Tipo EXACTO y llamada no virtual: una subclase no puede
                # sobrescribir ``to_detail`` para reabrir un canal de secretos
                # hacia la transición durable y visible al carril.
                if estado != "materialized" or type(
                        repair) is not ProjectionRepairEvidence:
                    raise OperationInvalid(
                        "repair sólo admite evidencia tipada al materializar")
                con.execute(
                    "INSERT INTO receipt_transitions(transition_id,receipt_id,seq,"
                    "state,at,detail) VALUES(?,?,?,?,?,?)",
                    (_new_id("trn"), rc["receipt_id"], seq + 1,
                     "materialization_repaired", at,
                     _canonical({**ProjectionRepairEvidence.to_detail(repair),
                                 "by_principal": view.principal_id,
                                 "by_runtime": view.runtime_instance})))
                seq += 1
            con.execute("INSERT INTO receipt_transitions(transition_id,receipt_id,seq,"
                        "state,at,detail) VALUES(?,?,?,?,?,?)",
                        (_new_id("trn"), rc["receipt_id"], seq + 1, estado, at,
                         _canonical(detalle)))
            con.execute("UPDATE receipts SET current_state=?, updated_at=?"
                        " WHERE receipt_id=?", (estado, at, rc["receipt_id"]))
            if estado == "indexed":
                # DIFUSIÓN: nadie puede acusar por `FLOTA`, así que esperar un
                # acuse es esperar para siempre — y `indexed` se lee como «va
                # llegando». El terminal se alcanza SOLO, aquí y en la misma
                # transacción: si dependiera de que alguien llame a
                # `mark_delivered`, dependería justo de la llamada que nadie
                # puede hacer. Medido por @contratosbik: 31 de 262 cabeceras
                # vivas del carril son pura difusión.
                self._cerrar_si_no_hay_acusables(con, event_id, rc["receipt_id"],
                                                 seq + 1, at)
            if estado == "materialized":
                # El outbox termina AQUÍ, en `materialized`. No en `delivered`:
                # el trabajador midió su propio efecto, no el de terceros.
                con.execute("UPDATE outbox SET state='materialized',"
                            " materialized_at=?, lease_until=NULL,"
                            " claim_token=NULL WHERE event_id=?", (at, event_id))

    @_audita
    def mark_materialized(self, token: str, event_id: str, *, entry_eid: str,
                          ledger: str, claim_token: str,
                          byte_off: int | None = None,
                          repair: ProjectionRepairEvidence | None = None) -> None:
        """El append aterrizó. `entry_eid` entra AQUÍ y sólo aquí: no existe en
        `accepted` y no es identidad en ninguna parte.

        Exige la `claim_token` que devolvió `claim_outbox` Y que el arriendo siga
        vivo. Un trabajador pausado cuyo arriendo venció y al que ya relevaron
        trae una ficha caduca: se le rechaza. Sin esto, el fencing existía para
        los leases y no para el outbox, que es donde de verdad se escribe.
        """
        self._avanzar(token, event_id, "materialized",
                      {"entry_eid": entry_eid, "ledger": ledger,
                       "byte_off_hint": byte_off}, claim_token=claim_token,
                      repair=repair)

    @_audita
    def mark_indexed(self, token: str, event_id: str, *,
                     index_ref: str | None = None) -> None:
        """El índice de búsqueda lo ve. Exige `materialized` previo y sesión: el
        sensor que lo afirma queda registrado en la transición."""
        self._avanzar(token, event_id, "indexed", {"index_ref": index_ref})

    def _acusables(self, con: sqlite3.Connection, event_id: str):
        """Roles que PUEDEN acusar este evento, o `None` si la fila es pre-v4.

        `None` y `[]` no son lo mismo y confundirlos es el defecto entero: `[]`
        afirma «no hay nadie que pueda acusar» (difusión pura, terminal), y
        `None` dice «esta fila se escribió sin censo y no lo sé». La segunda
        cae al camino legado; inventarle un veredicto sería fabricar entrega.
        """
        row = con.execute("SELECT recipients, recipients_roles FROM events"
                          " WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            return None
        if row["recipients_roles"] is None:
            return None
        return [str(x) for x in json.loads(row["recipients_roles"])]

    def _hubo_difusion(self, con: sqlite3.Connection, event_id: str) -> bool:
        """Le da su uso a `recipients_broadcast`, y no es decorativo: es lo único
        que separa «no iba dirigido a nadie» de «iba a la difusión»."""
        row = con.execute("SELECT recipients_broadcast FROM events WHERE event_id=?",
                          (event_id,)).fetchone()
        if row is None or row["recipients_broadcast"] is None:
            return False
        return bool(json.loads(row["recipients_broadcast"]))

    def _cerrar_si_no_hay_acusables(self, con: sqlite3.Connection, event_id: str,
                                    receipt_id: str, seq: int, at: str) -> None:
        acusables = self._acusables(con, event_id)
        if acusables is None or acusables:
            return
        # DOS motivos, no uno. Los dos casos acaban en el mismo estado y NO son
        # lo mismo, y el recibo es lo que va a citar quien audite: decir «todos
        # los destinos son de difusión» sobre un evento SIN destinos es una
        # afirmación falsa en el sitio donde nadie la va a re-derivar.
        motivo = ("sin destinatarios acusables: todos los destinos son de difusión"
                  if self._hubo_difusion(con, event_id) else
                  "sin destinatarios: la entrada no va dirigida a nadie")
        con.execute("INSERT INTO receipt_transitions(transition_id,receipt_id,"
                    "seq,state,at,detail) VALUES(?,?,?,?,?,?)",
                    (_new_id("trn"), receipt_id, seq + 1, "no_ack_expected", at,
                     _canonical({"motivo": motivo})))
        con.execute("UPDATE receipts SET current_state='no_ack_expected',"
                    " updated_at=? WHERE receipt_id=?", (at, receipt_id))

    # `no_ack_expected` es TERMINAL y no es un `delivered` disfrazado: dice que
    # nadie va a acusar, no que alguien recibió. Fundirlos haría que un evento
    # de difusión contase como entregado, que es la afirmación que este estado
    # existe para NO hacer.
    _ESTADOS_EVENTO_EXTRA = ("delivery_progress", "no_ack_expected")

    @_audita
    def mark_delivered(self, token: str, event_id: str, *,
                       ack_ref: str | None = None) -> str:
        """ACUSE DE UN DESTINATARIO. Devuelve el estado del recibo tras el acuse.

        El destinatario se DERIVA de la sesión (`view.role`) y se comprueba
        contra los destinatarios del evento. Como argumento era texto del
        cliente: `security` podía acusar por `cto`, y entonces «entregado» dejaba
        de significar que alguien lo recibió.

        `delivered` global sólo cuando han acusado TODOS los destinatarios
        explícitos. Antes queda `delivery_progress` en la historia y el
        `current_state` NO miente: sigue en `indexed`. Un acuse repetido del
        mismo receptor es idempotente — ni fila nueva ni transición nueva.
        """
        self._guard_mutable()
        at = _now_iso(self._clock())
        with self._tx() as con:
            view = _authenticate_locked(con, token, self._clock())
            if view is None:
                raise AuthError("se requiere sesión de runtime válida")
            ev = con.execute("SELECT lane, recipients FROM events WHERE event_id=?",
                             (event_id,)).fetchone()
            if ev is None:
                raise SubjectNotFound(f"no existe el evento {event_id}")
            if ev["lane"] != view.lane:
                raise LedgerNotAllowed("el evento es de otro carril")
            # EL ACUSE VA CONTRA EL ROL CANÓNICO, no contra el literal. El
            # literal es lo que el autor escribió (`→ backend`) y el rol es
            # quien tiene sesión (`be`): compararlos entre sí dejaba fuera al
            # destinatario legítimo en 204 de 695 tokens medidos.
            acusables = self._acusables(con, event_id)
            if acusables is None:                     # fila pre-v4: camino legado
                acusables = [str(x) for x in json.loads(ev["recipients"])]
            elif not acusables:
                raise DeliveryConflict(
                    "este evento no admite acuse: todos sus destinatarios son de "
                    "difusión y nadie ES la difusión")
            destinatarios = acusables
            if view.role not in destinatarios:
                raise DeliveryConflict(
                    f"tu rol no está entre los destinatarios de este evento")
            rc = con.execute("SELECT receipt_id, current_state FROM receipts"
                             " WHERE subject_kind='event' AND subject_id=?",
                             (event_id,)).fetchone()
            if rc is None:
                raise SubjectNotFound(f"sin recibo para {event_id}")
            if rc["current_state"] not in ("indexed", "delivered"):
                raise DeliveryConflict(
                    f"no se puede acusar en `{rc['current_state']}`: falta indexar")
            con.execute(
                "INSERT OR IGNORE INTO event_acks(event_id,recipient,principal_id,"
                "runtime_instance,ack_ref,at) VALUES(?,?,?,?,?,?)",
                (event_id, view.role, view.principal_id, view.runtime_instance,
                 ack_ref, at))
            if con.execute("SELECT changes() c").fetchone()["c"] == 0:
                self._detalle_telemetria(advanced=False, state=rc["current_state"])
                return rc["current_state"]      # replay: ni fila ni transición
            acusados = con.execute("SELECT COUNT(*) c FROM event_acks"
                                   " WHERE event_id=?", (event_id,)).fetchone()["c"]
            completo = acusados >= len(set(destinatarios))
            estado = "delivered" if completo else "delivery_progress"
            seq = con.execute("SELECT MAX(seq) s FROM receipt_transitions"
                              " WHERE receipt_id=?", (rc["receipt_id"],)
                              ).fetchone()["s"] or 0
            con.execute("INSERT INTO receipt_transitions(transition_id,receipt_id,"
                        "seq,state,at,detail) VALUES(?,?,?,?,?,?)",
                        (_new_id("trn"), rc["receipt_id"], seq + 1, estado, at,
                         _canonical({"recipient": view.role,
                                     "by_principal": view.principal_id,
                                     "by_runtime": view.runtime_instance,
                                     "ack_ref": ack_ref,
                                     "acked": acusados,
                                     "of": len(set(destinatarios))})))
            if completo:
                con.execute("UPDATE receipts SET current_state='delivered',"
                            " updated_at=? WHERE receipt_id=?",
                            (at, rc["receipt_id"]))
            self._detalle_telemetria(advanced=True, state=estado)
            return "delivered" if completo else rc["current_state"]

    @_audita
    def mark_outbox_failed(self, token: str, event_id: str, *, error: str,
                           claim_token: str) -> None:
        """Registra un intento FALLIDO de materializar. Exige ficha VIGENTE.

        Sin la ficha, un trabajador caducado al que ya relevaron podía reportar
        su fallo y de paso poner `lease_until=NULL` — o sea QUITARLE EL ARRIENDO
        al trabajador nuevo, que está a mitad de su intento. El rezagado no sólo
        se equivocaba de época: saboteaba al vivo, y por el camino de reportar un
        error, que es el que menos se audita.

        Deja una transición de recibo OBSERVABLE y NO bloquea el reintento: el
        `current_state` se queda en `accepted` a propósito, así que un
        `materialized` posterior sigue siendo legal. Un fallo que cerrara el
        recibo convertiría un problema transitorio en uno definitivo.
        """
        self._guard_mutable()
        with self._tx() as con:
            # Igual que en claim_outbox: el fencing se decide con una hora
            # medida bajo el lock, no con una foto que pudo caducar esperando.
            now = self._clock()
            at = _now_iso(now)
            view = _authenticate_locked(con, token, now)
            if view is None:
                raise AuthError("se requiere sesión de runtime válida")
            self._exigir_capacidad_locked(con, view, CAP_OUTBOX_WORKER,
                                          "mark_outbox_failed")
            ob = con.execute(
                "SELECT o.claim_token, o.lease_until, o.lease_by, e.lane"
                "  FROM outbox o JOIN events e USING (event_id)"
                " WHERE o.event_id=?", (event_id,)).fetchone()
            if ob is None:
                raise SubjectNotFound(f"sin item de outbox para {event_id}")
            if ob["lane"] != view.lane:
                raise LedgerNotAllowed("el evento es de otro carril")
            if ob["lease_by"] != view.runtime_instance:
                raise FencingConflict(
                    "el arriendo es de otro runtime: la ficha no te hace dueño")
            if ob["claim_token"] != claim_token:
                raise FencingConflict(
                    "ficha de arriendo no vigente: te relevaron, y reportar tu "
                    "fallo no puede tocar el arriendo de quien te sustituyó")
            if (ob["lease_until"] or 0) <= now:
                raise FencingConflict("tu arriendo del outbox VENCIÓ")
            intentos = con.execute("SELECT attempts FROM outbox WHERE event_id=?",
                                   (event_id,)).fetchone()["attempts"]
            agotado = intentos >= self._max_attempts
            # Backoff exponencial con techo, política INTERNA: el llamante no
            # elige cuánto esperar, porque el que falla en bucle es justo el que
            # pediría reintentar ya.
            espera = min(self._backoff_base_s * (2 ** max(0, intentos - 1)),
                         self._backoff_cap_s)
            error_code = (error if type(error) is str
                          and error in OUTBOX_FAILURE_CODES
                          else DEFAULT_OUTBOX_FAILURE_CODE)
            con.execute("UPDATE outbox SET last_error=?, next_attempt=?,"
                        " state=?, lease_until=NULL, claim_token=NULL"
                        " WHERE event_id=?",
                        (error_code, now + espera,
                         "failed" if agotado else "pending", event_id))
            rc = con.execute("SELECT receipt_id, current_state FROM receipts"
                             " WHERE subject_kind='event' AND subject_id=?",
                             (event_id,)).fetchone()
            if rc is not None:
                seq = con.execute("SELECT MAX(seq) s FROM receipt_transitions"
                                  " WHERE receipt_id=?", (rc["receipt_id"],)
                                  ).fetchone()["s"] or 0
                con.execute(
                    "INSERT INTO receipt_transitions(transition_id,receipt_id,seq,"
                    "state,at,detail) VALUES(?,?,?,?,?,?)",
                    (_new_id("trn"), rc["receipt_id"], seq + 1,
                     "materialization_exhausted" if agotado
                     else "materialization_failed", at,
                     _canonical({"error": error_code, "attempts": intentos,
                                 "retry_after_s": None if agotado else espera})))
                # `current_state` NO se toca: el intento fallido es HISTORIA, no
                # un estado terminal. Escribirlo aquí bloquearía el reintento.

    def _outbox_global(self, estados: tuple) -> int:
        """Cuenta SIN carril. Privado a propósito: la profundidad de la cola de
        un carril es información de ese carril, así que la forma pública exige
        sesión. Esto sólo lo usa la salud del PROCESO, que no sirve a nadie."""
        marcas = ",".join("?" * len(estados))
        with self._lectura() as (con, _):
            return con.execute(
                f"SELECT COUNT(*) c FROM outbox WHERE state IN ({marcas})",
                estados).fetchone()["c"]

    def _outbox_del_carril(self, token: str, estados: tuple) -> int:
        marcas = ",".join("?" * len(estados))
        with self._lectura() as (con, _):
            view = _authenticate_locked(con, token, self._clock())
            if view is None:
                raise AuthError("se requiere sesión de runtime válida")
            return con.execute(
                f"SELECT COUNT(*) c FROM outbox o JOIN events e USING (event_id)"
                f" WHERE o.state IN ({marcas}) AND e.lane=?",
                (*estados, view.lane)).fetchone()["c"]

    def pending_outbox(self, token: str) -> int:
        """Profundidad de la cola DEL CARRIL de la sesión."""
        return self._outbox_del_carril(token, ("pending",))

    def unresolved_outbox(self, token: str) -> int:
        """Lo que IMPIDE un rollback: `pending` + `failed`.

        Un item agotado NO se auto-abandona. Si el propio sistema lo sacara de la
        cuenta, la cola se «limpiaría» sola exactamente cuando algo está roto, y
        el rollback vería cero pendientes sobre escrituras aceptadas que nunca
        aterrizaron. Sacarlo de aquí es una decisión de un operador, con nombre.
        """
        return self._outbox_del_carril(token, ("pending", "failed"))

    def outbox_counts(self, token: str) -> OutboxCounts:
        """`pending` y `failed` del carril de la sesión, de UNA sola foto.

        Cuatro decisiones, ninguna cosmética:

        · **UNA transacción y UNA consulta AGREGADA para las dos cifras.** La
          autenticación y la capacidad conservan sus consultas propias; lo que
          no se divide en dos fotos es el conteo. El par ya se podía obtener
          llamando a `pending_outbox` y `unresolved_outbox`, pero cada una abre
          su propia lectura: entre las dos cabe un `claim`+`mark_outbox_failed`
          entero, y el `failed` que el llamante deduce restando describe un
          estado que la base nunca tuvo. Que las dos cifras salgan del MISMO
          `SELECT` no es una optimización — es la propiedad.
        · **El carril va en el `WHERE`, no en Python.** Misma doctrina que
          `_leer_recibo`: filtrar después de traer las filas significa que el
          proceso YA leyó la cola ajena, y que luego decida no enseñarla protege
          al cliente, no al dato.
        · **Autoriza con `outbox_worker` DENTRO de la transacción que lee.** La
          profundidad de la cola y su fondo de agotados es lo que un trabajador
          usa para decidir si sigue drenando; estar en el carril no es ser quien
          lo drena. Y la capacidad se relee aquí, no antes: una recarga del mapa
          entre la comprobación y la consulta dejaría pasar a quien acaba de
          perderla. Esto NO ensancha la superficie de información —las dos
          cifras ya eran legibles por `pending_outbox`/`unresolved_outbox`— sino
          que estrecha la autorización de la forma nueva. Las dos viejas se
          dejan como están: apretarlas rompería a sus llamantes vivos, y eso es
          una decisión de contrato que no cabe en este cambio.
        · **`CASE` y no `FILTER`.** No necesitamos elevar el suelo de SQLite
          para expresar esta suma; el `CASE` conserva el mismo contrato en las
          imágenes antiguas que todavía pueden ser destino de rollback.

        `abandoned` y `materialized` quedan fuera de las dos cifras por el mismo
        motivo por el que `unresolved_outbox` no los cuenta: son salidas
        resueltas de la cola, no cola.
        """
        with self._lectura() as (con, _):
            view = _authenticate_locked(con, token, self._clock())
            if view is None:
                raise AuthError("se requiere sesión de runtime válida")
            self._exigir_capacidad_locked(con, view, CAP_OUTBOX_WORKER,
                                          "outbox_counts")
            # El seam va DESPUÉS de la autenticación a propósito: la foto de la
            # transacción queda fijada por la lectura de la sesión, así que lo
            # que se prueba desde aquí es que el conteo pertenece a la MISMA
            # foto que autorizó, y no a la de un instante posterior.
            self._tras_precheck()
            fila = con.execute(
                "SELECT"
                "  SUM(CASE WHEN o.state='pending' THEN 1 ELSE 0 END) AS p,"
                "  SUM(CASE WHEN o.state='failed'  THEN 1 ELSE 0 END) AS f"
                "  FROM outbox o JOIN events e USING (event_id)"
                " WHERE e.lane=? AND o.state IN ('pending','failed')",
                (view.lane,)).fetchone()
            # `SUM` sobre cero filas da NULL, no 0: sin esto, un carril vacío
            # devolvía `None` y el primer `+` del llamante reventaba.
            pending = 0 if fila["p"] is None else fila["p"]
            failed = 0 if fila["f"] is None else fila["f"]
            return OutboxCounts(
                lane=view.lane, pending=pending, failed=failed)

    @staticmethod
    def _capacidades_locked(con: sqlite3.Connection, principal_id: str) -> set:
        """Capacidades VIGENTES del principal, leídas en la transacción que las
        usa. Fuera de ella, una recarga del mapa entre la comprobación y el
        efecto deja pasar a quien acaba de perderlas."""
        fila = con.execute(
            "SELECT capabilities FROM credential_bindings"
            " WHERE principal_id=? AND retired_at IS NULL", (principal_id,)).fetchone()
        return set(json.loads(fila["capabilities"])) if fila else set()

    @classmethod
    def _exigir_capacidad_locked(cls, con: sqlite3.Connection, view: SessionView,
                                 capacidad: str, operacion: str) -> None:
        """Autoriza con el mapa vivo dentro de la misma transacción del efecto."""
        if capacidad not in cls._capacidades_locked(con, view.principal_id):
            raise PolicyDenied(
                f"`{operacion}` exige la capacidad `{capacidad}`; el rol es sólo "
                "atribución y no concede permisos")

    def _operar_outbox(self, token: str, event_id: str, operacion: str,
                       reason: str) -> None:
        self._guard_mutable()
        if not reason:
            raise OperationInvalid("una operación manual necesita motivo")
        at = _now_iso(self._clock())
        # El MISMO seam que `accept_event`, y por el mismo motivo: sin él, la
        # ventana entre los chequeos baratos y el `BEGIN IMMEDIATE` de esta
        # puerta no se puede falsar sin un `sleep`. No hace nada en producción.
        self._tras_precheck()
        with self._tx() as con:
            view = _authenticate_locked(con, token, self._clock())
            if view is None:
                raise AuthError("se requiere sesión de runtime válida")
            # EL OPERADOR SE DERIVA DE LA SESIÓN. Como argumento era una firma
            # que se escribe uno mismo: el registro de quién abandonó una
            # escritura aceptada valdría exactamente lo que valga la honradez del
            # que la abandona, que es cero cuando hace falta el registro.
            if CAP_OUTBOX_OPERATOR not in self._capacidades_locked(
                    con, view.principal_id):
                raise PolicyDenied(
                    f"`{operacion}` exige la capacidad `{CAP_OUTBOX_OPERATOR}`: "
                    f"estar en el carril no es ser su operador")
            if operacion == "requeue":
                # Re-encolar es ADMITIR de nuevo trabajo en la cola, así que pasa
                # por la barrera. `abandon` NO pasa: es la vía de DRENAJE, y
                # cerrarla junto con la entrada dejaría el carril sin forma de
                # vaciarse — y entonces el `seal`, que exige `pending = failed =
                # 0`, sería inalcanzable por construcción.
                #
                # Va ANTES de leer el item: con la puerta cerrada, confirmar si
                # un `event_id` existe sería un oráculo gratis, y la barrera es
                # de `(carril, verbo)`, así que decidirla aquí no necesita saber
                # nada del sujeto.
                self._exigir_admision_locked(con, view.lane, "outbox.requeue")
            operator = view.principal_id
            row = con.execute("SELECT o.state, e.lane FROM outbox o"
                              "  JOIN events e USING (event_id)"
                              " WHERE o.event_id=?", (event_id,)).fetchone()
            if row is None:
                raise SubjectNotFound(f"sin item de outbox para {event_id}")
            if row["lane"] != view.lane:
                raise LedgerNotAllowed("el evento es de otro carril")
            if operacion == "requeue" and row["state"] != "failed":
                raise OperationInvalid(
                    f"sólo se re-encola lo `failed`; éste está en `{row['state']}`")
            if operacion == "abandon" and row["state"] not in ("failed", "pending"):
                raise OperationInvalid(
                    f"sólo se abandona lo vivo; éste está en `{row['state']}`")
            nuevo = "pending" if operacion == "requeue" else "abandoned"
            con.execute("UPDATE outbox SET state=?, attempts=?, next_attempt=?,"
                        " lease_until=NULL, claim_token=NULL WHERE event_id=?",
                        (nuevo, 0 if operacion == "requeue" else
                         con.execute("SELECT attempts FROM outbox WHERE event_id=?",
                                     (event_id,)).fetchone()["attempts"],
                         self._clock(), event_id))
            con.execute("INSERT INTO outbox_operations(operation_id,event_id,"
                        "operation,operator,lane,runtime_instance,reason,"
                        "from_state,at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (_new_id("opx"), event_id, operacion, operator,
                         view.lane, view.runtime_instance, reason,
                         row["state"], at))
            rc = con.execute("SELECT receipt_id FROM receipts WHERE"
                             " subject_kind='event' AND subject_id=?",
                             (event_id,)).fetchone()
            if rc is not None:
                seq = con.execute("SELECT MAX(seq) s FROM receipt_transitions"
                                  " WHERE receipt_id=?", (rc["receipt_id"],)
                                  ).fetchone()["s"] or 0
                con.execute("INSERT INTO receipt_transitions(transition_id,"
                            "receipt_id,seq,state,at,detail) VALUES(?,?,?,?,?,?)",
                            (_new_id("trn"), rc["receipt_id"], seq + 1,
                             f"outbox_{operacion}", at,
                             _canonical({"operator": operator, "reason": reason,
                                         "by_runtime": view.runtime_instance,
                                         "from_state": row["state"]})))

    @_audita
    def requeue_outbox(self, token: str, event_id: str, *, reason: str) -> None:
        """Devuelve a la cola un item agotado. Acto de operador AUTENTICADO."""
        self._operar_outbox(token, event_id, "requeue", reason)

    @_audita
    def abandon_outbox(self, token: str, event_id: str, *, reason: str) -> None:
        """Renuncia a materializar. Es el único camino que drena sin que el
        evento haya llegado al markdown — por eso lleva sesión, motivo y rastro."""
        self._operar_outbox(token, event_id, "abandon", reason)

    # ── barrera de admisión ──────────────────────────────────────────────────
    @staticmethod
    def _admision_locked(con: sqlite3.Connection, lane: str,
                         verb: str) -> tuple[str, int]:
        """Estado vigente de `(carril, verbo)`, LEÍDO EN LA TRANSACCIÓN QUE DECIDE.

        `(estado, epoch)`. Sin fila ⇒ `("closed", 0)`: la ausencia es cierre, y
        el `0` deja el primer epoch escrito en `1`.

        `ORDER BY epoch DESC LIMIT 1` va por la clave primaria `(lane, verb,
        epoch)`, así que la cabeza del historial se lee sin recorrerlo.
        """
        fila = con.execute(
            "SELECT state, epoch FROM admission_history"
            "  WHERE lane=? AND verb=? ORDER BY epoch DESC LIMIT 1",
            (lane, verb)).fetchone()
        if fila is None:
            return "closed", 0
        return fila["state"], int(fila["epoch"])

    @classmethod
    def _exigir_admision_locked(cls, con: sqlite3.Connection, lane: str,
                                verb: str) -> None:
        """LA comprobación que autoriza, dentro de la transacción que muta.

        Va aquí y NO en la pasarela, ni en un chequeo previo: entre una lectura
        de fuera y el `INSERT` cabe un `close` entero, y entonces la barrera
        sólo pararía a quien no compite. Es el mismo argumento que ya está
        escrito para `_authenticate_locked` y para `_fence_locked`.
        """
        estado, _ = cls._admision_locked(con, lane, verb)
        if estado != "open":
            raise AdmissionClosed(
                f"la admisión de `{verb}` está en `{estado}` para este carril: "
                f"la escritura no entra")

    def admission(self, token: str, verb: str) -> AdmissionState:
        """Lectura del estado vigente. NO autoriza nada.

        Como toda lectura fuera de transacción, entre este `open` y una
        escritura cabe un `close`. Sirve para decidir qué epoch pasarle a
        `seal_admission`, que es quien vuelve a comprobarlo donde importa.
        """
        if type(verb) is not str or verb not in ADMISSION_VERBS:
            raise OperationInvalid(
                "verbo fuera de la barrera de admisión")
        view = self.authenticate(token)
        if view is None:
            raise AuthError("se requiere sesión de runtime válida")
        with self._lectura() as (con, _):
            estado, epoch = self._admision_locked(con, view.lane, verb)
        return AdmissionState(view.lane, verb, estado, epoch)

    def admissions(self, token: str) -> tuple[AdmissionState, ...]:
        """Foto exacta de TODA la barrera del carril de la sesión.

        No se implementa llamando dos veces a :meth:`admission`: cada llamada
        abriría una transacción y entre ambas cabría una transición completa. La
        sesión se autentica y los dos verbos se leen dentro del MISMO ``BEGIN``;
        el carril sale de esa sesión y nunca de un argumento del llamante.
        """
        with self._lectura() as (con, _):
            view = _authenticate_locked(con, token, self._clock())
            if view is None:
                raise AuthError("se requiere sesión de runtime válida")
            self._exigir_capacidad_locked(
                con, view, CAP_ADMISSION_OPERATOR, "admissions")
            return tuple(
                AdmissionState(view.lane, verb, *self._admision_locked(
                    con, view.lane, verb))
                for verb in ADMISSION_VERBS
            )

    def admission_ready(self, lane: str) -> bool:
        """Foto interna y atómica de las dos puertas para readiness.

        No concede autoridad ni acepta un carril del cliente: el composition
        root entrega el carril derivado del principal worker V8.
        """
        if type(lane) is not str or not lane:
            return False
        with self._lectura() as (con, _):
            return all(
                self._admision_locked(con, lane, verb)[0] == "open"
                for verb in ADMISSION_VERBS
            )

    @classmethod
    def _rollback_status_locked(
            cls, con: sqlite3.Connection, view: SessionView,
            ) -> RollbackStatus:
        """Construye la foto de rollback sin abrir lecturas auxiliares.

        Esta función sólo se llama después de autenticar y autorizar en la
        MISMA transacción. En particular, no compone ``admissions()`` y
        ``outbox_counts()``: hacerlo daría dos fotos entre las que cabe un
        sellado, un claim o un fallo del proyector.
        """
        admissions = tuple(
            AdmissionState(view.lane, verb, *cls._admision_locked(
                con, view.lane, verb))
            for verb in ADMISSION_VERBS
        )
        row = con.execute(
            "SELECT"
            "  SUM(CASE WHEN o.state='pending' THEN 1 ELSE 0 END) AS p,"
            "  SUM(CASE WHEN o.state='failed'  THEN 1 ELSE 0 END) AS f"
            "  FROM outbox o JOIN events e USING (event_id)"
            " WHERE e.lane=? AND o.state IN ('pending','failed')",
            (view.lane,)).fetchone()
        version = con.execute(
            "SELECT v FROM meta WHERE k='durable_v'").fetchone()
        if version is None:
            raise SchemaIndeterminate(
                "el journal no declara durable_v; no certifico rollback")
        pending = 0 if row["p"] is None else row["p"]
        failed = 0 if row["f"] is None else row["f"]
        return RollbackStatus(
            lane=view.lane,
            admissions=admissions,
            outbox=OutboxCounts(view.lane, pending, failed),
            durable_v=int(version["v"]),
        )

    @classmethod
    def _database_rollback_status_locked(
            cls, con: sqlite3.Connection) -> _DatabaseRollbackStatus:
        """Certificado interno del fichero completo, bajo una sola transacción.

        Las lanes relevantes se derivan de TODA tabla del manifiesto v7 que
        tenga una columna ``lane``. No vienen de configuración ni del caller:
        una lane con un solo principal, recibo o historial sigue siendo parte
        de los bytes que el rollback va a reemplazar. ``_admision_locked``
        traduce ausencia a ``closed`` para servicio normal; aquí ese resultado
        NO certifica, porque sólo ``sealed`` explícito constituye consentimiento.
        """
        lanes = set()
        for table in sorted(
                table for table, columns in _COLUMNAS_V7.items()
                if "lane" in columns):
            lanes.update(
                row["lane"] for row in con.execute(
                    f"SELECT DISTINCT lane FROM {table}"
                    " WHERE lane IS NOT NULL AND lane<>''"))
        ordered_lanes = tuple(sorted(lanes))
        admissions = tuple(
            AdmissionState(lane, verb, *cls._admision_locked(con, lane, verb))
            for lane in ordered_lanes
            for verb in ADMISSION_VERBS
        )
        row = con.execute(
            "SELECT"
            " SUM(CASE WHEN state='pending' THEN 1 ELSE 0 END) AS p,"
            " SUM(CASE WHEN state='failed' THEN 1 ELSE 0 END) AS f"
            " FROM outbox WHERE state IN ('pending','failed')").fetchone()
        version = con.execute(
            "SELECT v FROM meta WHERE k='durable_v'").fetchone()
        if version is None:
            raise SchemaIndeterminate(
                "el journal no declara durable_v; no certifico rollback global")
        return _DatabaseRollbackStatus(
            lanes=ordered_lanes,
            admissions=admissions,
            pending=0 if row["p"] is None else int(row["p"]),
            failed=0 if row["f"] is None else int(row["f"]),
            durable_v=int(version["v"]),
        )

    @_audita
    def rollback_status(self, token: str) -> RollbackStatus:
        """Lee autorización, barreras y cola como una sola foto por carril.

        La capacidad debe ser el conjunto EXACTO ``{admission_operator}``.
        Tenerla junto a otro poder no convierte una credencial generalista en
        la identidad deliberadamente estrecha que certifica un rollback.
        """
        with self._lectura() as (con, _):
            view = _authenticate_locked(con, token, self._clock())
            if view is None:
                raise AuthError("se requiere sesión de operador válida")
            if self._capacidades_locked(con, view.principal_id) != {
                    CAP_ADMISSION_OPERATOR}:
                raise PolicyDenied(
                    "certificar rollback exige exactamente la capacidad "
                    f"`{CAP_ADMISSION_OPERATOR}`")
            return self._rollback_status_locked(con, view)

    @_audita
    def certify_rollback(self, token: str) -> RollbackCertificate:
        """Certifica sólo el estado terminal sellado y una cola vacía.

        ``sealed`` es terminal y su transición ya exige cero trabajo vivo. La
        comprobación se repite aquí, en una única transacción junto a la
        autenticación, porque un certificado no se apoya en una lectura previa
        del operador ni en contadores reunidos por el cliente.
        """
        with self._lectura() as (con, _):
            view = _authenticate_locked(con, token, self._clock())
            if view is None:
                raise AuthError("se requiere sesión de operador válida")
            if self._capacidades_locked(con, view.principal_id) != {
                    CAP_ADMISSION_OPERATOR}:
                raise PolicyDenied(
                    "certificar rollback exige exactamente la capacidad "
                    f"`{CAP_ADMISSION_OPERATOR}`")
            status = self._rollback_status_locked(con, view)
            if not status.certifiable:
                raise AdmissionConflict(
                    "rollback no certificable: ambas admisiones deben estar "
                    "sealed y pending/failed deben ser cero")
            return RollbackCertificate(
                lane=status.lane,
                admissions=status.admissions,
                outbox=status.outbox,
                durable_v=status.durable_v,
                certified_at=_now_iso(self._clock()),
            )

    @_audita
    def transition_admissions(
            self, token: str, *, target: str,
            expected_epochs: dict[str, int], reason_code: str,
            ) -> tuple[AdmissionState, ...]:
        """Mueve los DOS verbos del carril, con un único CAS y commit.

        La unidad operativa es el par cerrado ``ADMISSION_VERBS``. Validar o
        escribir verbo por verbo permitiría dejar una puerta abierta y la otra
        cerrada si el segundo CAS pierde. Aquí primero se autentica, autoriza,
        lee y compara el par completo; sólo después se escribe ninguna fila.

        ``expected_epochs`` es un ``dict`` exacto y obligatorio. Copiar sus dos
        enteros antes del seam evita que un diccionario mutable cambie entre la
        validación y el ``BEGIN IMMEDIATE``.
        """
        self._guard_mutable()
        if type(target) is not str or target not in ADMISSION_STATES:
            raise OperationInvalid(
                "el destino tiene que ser uno del vocabulario cerrado de la barrera")
        if (type(reason_code) is not str
                or reason_code not in ADMISSION_OPERATOR_REASON_CODES):
            raise OperationInvalid(
                "el motivo tiene que ser uno del vocabulario cerrado del operador")
        if type(expected_epochs) is not dict:
            raise OperationInvalid(
                "`expected_epochs` tiene que ser un objeto exacto por verbo")
        # Un ``dict`` exacto sigue siendo mutable desde otro hilo. La copia es
        # LA única lectura del objeto del caller: separar la foto de claves de
        # las lecturas de valores permitiría que un ``clear()`` entre ambas
        # escapara como ``KeyError`` crudo, fuera del contrato auditado.
        snapshot = expected_epochs.copy()
        keys = tuple(snapshot)
        if (any(type(key) is not str for key in keys)
                or len(keys) != len(ADMISSION_VERBS)
                or set(keys) != set(ADMISSION_VERBS)):
            raise OperationInvalid(
                "`expected_epochs` exige exactamente los dos verbos de admisión")
        for verb in ADMISSION_VERBS:
            epoch = snapshot[verb]
            if type(epoch) is not int or epoch < 0:
                raise OperationInvalid(
                    "cada epoch esperado tiene que ser un entero no negativo")

        at = _now_iso(self._clock())
        self._tras_precheck()
        with self._tx() as con:
            view = _authenticate_locked(con, token, self._clock())
            if view is None:
                raise AuthError(
                    "sesión invalidada entre la comprobación y la escritura")
            if CAP_ADMISSION_OPERATOR not in self._capacidades_locked(
                    con, view.principal_id):
                raise PolicyDenied(
                    f"mover la barrera exige la capacidad "
                    f"`{CAP_ADMISSION_OPERATOR}`: operar la cola no es cerrar el carril")

            current = tuple(
                AdmissionState(view.lane, verb, *self._admision_locked(
                    con, view.lane, verb))
                for verb in ADMISSION_VERBS
            )
            # LOS DOS CAS ANTES DE ESCRIBIR. En particular, que el segundo sea
            # stale no puede dejar ya insertada la transición del primero.
            for state in current:
                expected = snapshot[state.verb]
                if state.epoch != expected:
                    raise AdmissionConflict(
                        f"la barrera de `{state.verb}` va por el epoch "
                        f"{state.epoch} y la operación traía {expected}: hay una "
                        "transición que no viste")
            for state in current:
                # ``sealed`` tiene un conjunto de salidas vacío: terminal por el
                # mismo vocabulario que gobierna las APIs de un solo verbo.
                if target not in _TRANSICIONES_ADMISION[state.state]:
                    raise AdmissionConflict(
                        f"`{state.state}` no transita a `{target}` para "
                        f"`{state.verb}`")

            if target == "sealed":
                # UN solo COUNT para el CARRIL, no uno por verbo. Va dentro del
                # mismo BEGIN IMMEDIATE que inserta ambos sellos.
                unresolved = con.execute(
                    "SELECT COUNT(*) c FROM outbox o JOIN events e USING (event_id)"
                    "  WHERE o.state IN ('pending','failed') AND e.lane=?",
                    (view.lane,)).fetchone()["c"]
                if unresolved:
                    raise AdmissionConflict(
                        f"el carril tiene {unresolved} escrituras aceptadas sin "
                        "resolver: sellar aquí congelaría un cero que no existe")

            result = []
            for state in current:
                epoch = state.epoch + 1
                con.execute(
                    "INSERT INTO admission_history(lane,verb,epoch,state,origin,"
                    "operator,runtime_instance,reason_code,at)"
                    " VALUES(?,?,?,?,'operator',?,?,?,?)",
                    (view.lane, state.verb, epoch, target, view.principal_id,
                     view.runtime_instance, reason_code, at))
                result.append(AdmissionState(
                    view.lane, state.verb, target, epoch))
            return tuple(result)

    def _transicion_admision(self, token: str, verb: str, destino: str, *,
                             reason_code: str, expected_epoch: int | None,
                             exigir_drenado: bool) -> AdmissionState:
        self._guard_mutable()
        if type(verb) is not str or verb not in ADMISSION_VERBS:
            raise OperationInvalid("verbo fuera de la barrera de admisión")
        if (type(reason_code) is not str
                or reason_code not in ADMISSION_OPERATOR_REASON_CODES):
            raise OperationInvalid(
                "el motivo tiene que ser uno del vocabulario cerrado del operador")
        if expected_epoch is not None and (
                isinstance(expected_epoch, bool)
                or type(expected_epoch) is not int or expected_epoch < 0):
            raise OperationInvalid(
                "`expected_epoch` tiene que ser un entero no negativo")
        at = _now_iso(self._clock())
        self._tras_precheck()
        with self._tx() as con:
            view = _authenticate_locked(con, token, self._clock())
            if view is None:
                raise AuthError(
                    "sesión invalidada entre la comprobación y la escritura")
            # Capacidad DEDICADA, leída en la misma transacción que escribe: un
            # `reload_credential_map` entre comprobar y mover la puerta dejaría
            # cerrar a quien acaba de perder el permiso.
            if CAP_ADMISSION_OPERATOR not in self._capacidades_locked(
                    con, view.principal_id):
                raise PolicyDenied(
                    f"mover la barrera exige la capacidad "
                    f"`{CAP_ADMISSION_OPERATOR}`: operar la cola no es cerrar el carril")
            estado, epoch = self._admision_locked(con, view.lane, verb)
            if expected_epoch is not None and expected_epoch != epoch:
                # LA VALLA. Sin esto, dos operadores que leyeron el mismo estado
                # escriben dos transiciones y la segunda pisa una decisión que
                # no vio.
                raise AdmissionConflict(
                    f"la barrera va por el epoch {epoch} y la operación traía "
                    f"{expected_epoch}: hay una transición que no viste")
            if destino not in _TRANSICIONES_ADMISION[estado]:
                raise AdmissionConflict(
                    f"`{estado}` no transita a `{destino}`")
            if exigir_drenado:
                # EN LA MISMA TRANSACCIÓN que escribe el sello. Contarlo fuera
                # daría un cero cierto en el momento de contarlo y falso en el
                # de sellar — y un sello es justamente la afirmación de que ese
                # cero ya no se puede mover.
                sin_resolver = con.execute(
                    "SELECT COUNT(*) c FROM outbox o JOIN events e USING (event_id)"
                    "  WHERE o.state IN ('pending','failed') AND e.lane=?",
                    (view.lane,)).fetchone()["c"]
                if sin_resolver:
                    raise AdmissionConflict(
                        f"el carril tiene {sin_resolver} escrituras aceptadas sin "
                        f"resolver: sellar aquí congelaría un cero que no existe")
            con.execute(
                "INSERT INTO admission_history(lane,verb,epoch,state,origin,"
                "operator,runtime_instance,reason_code,at)"
                " VALUES(?,?,?,?,'operator',?,?,?,?)",
                (view.lane, verb, epoch + 1, destino, view.principal_id,
                 view.runtime_instance, reason_code, at))
        return AdmissionState(view.lane, verb, destino, epoch + 1)

    @_audita
    def open_admission(self, token: str, verb: str, *, reason_code: str,
                       expected_epoch: int | None = None) -> AdmissionState:
        """Abre la puerta de `(carril de la sesión, verbo)`. Acto de operador."""
        return self._transicion_admision(
            token, verb, "open", reason_code=reason_code,
            expected_epoch=expected_epoch, exigir_drenado=False)

    @_audita
    def close_admission(self, token: str, verb: str, *, reason_code: str,
                        expected_epoch: int | None = None) -> AdmissionState:
        """Cierra la entrada. El DRENADO sigue funcionando: ver `ADMISSION_VERBS`."""
        return self._transicion_admision(
            token, verb, "closed", reason_code=reason_code,
            expected_epoch=expected_epoch, exigir_drenado=False)

    @_audita
    def seal_admission(self, token: str, verb: str, *, reason_code: str,
                       expected_epoch: int) -> AdmissionState:
        """Sella. TERMINAL, y sólo desde `closed` con el carril drenado.

        `expected_epoch` es OBLIGATORIO aquí y opcional en los otros dos: es el
        único acto irreversible de la barrera, así que no se firma a ciegas.

        No se puede sellar desde `open`, y no es una restricción de estilo: una
        puerta abierta admite escrituras nuevas, así que el `pending = failed =
        0` que este acto congela podría dejar de ser cierto entre el `COUNT` y
        el `COMMIT`. Cerrar primero es lo que hace que el cero se pueda congelar.
        """
        if expected_epoch is None:
            # `None` significa «sin valla» en los otros dos verbos, así que
            # dejarlo pasar aquí desactivaría en silencio lo que este parámetro
            # existe para imponer — y sin ruido, porque la firma ya lo declara
            # obligatorio y nadie volvería a mirar.
            raise OperationInvalid(
                "`seal_admission` no acepta `expected_epoch=None`: el sello es "
                "el único acto irreversible y no se firma sin valla")
        return self._transicion_admision(
            token, verb, "sealed", reason_code=reason_code,
            expected_epoch=expected_epoch, exigir_drenado=True)

    # ── control plane de flota v7 ───────────────────────────────────────────
    @staticmethod
    def _records(value: Sequence[Mapping[str, Any]], keys: set[str],
                 field_name: str) -> tuple[dict[str, Any], ...]:
        if type(value) not in (list, tuple):
            raise OperationInvalid(f"{field_name} debe ser lista o tupla")
        out = []
        for item in value:
            if type(item) is not dict or set(item) != keys:
                raise OperationInvalid(
                    f"cada elemento de {field_name} exige exactamente {sorted(keys)}")
            # Copia profunda canónica: el llamante no puede cambiar lo validado
            # mientras esperamos BEGIN IMMEDIATE.
            try:
                out.append(json.loads(_canonical(item)))
            except (TypeError, ValueError) as exc:
                raise OperationInvalid(f"{field_name} no es JSON canónico") from exc
        return tuple(out)

    @staticmethod
    def _active_workload_locked(con: sqlite3.Connection, lane: str,
                                workload_id: str) -> sqlite3.Row | None:
        return con.execute(
            "SELECT w.* FROM expected_workloads w"
            " JOIN organization_revisions o ON o.lane=w.lane"
            "  AND o.revision=w.organization_revision AND o.active=1"
            " WHERE w.lane=? AND w.workload_id=?",
            (lane, workload_id)).fetchone()

    def _runtime_transition_locked(
            self, con: sqlite3.Connection, *, lane: str, workload: sqlite3.Row,
            principal_id: str, to_status: str, detector_state: str | None,
            cause_kind: str, cause_id: str, reason_code: str,
            observation_id: str | None = None,
            recovery_command_id: str | None = None,
            at: str | None = None) -> tuple[str, str, int]:
        current = con.execute(
            "SELECT * FROM runtime_status WHERE lane=? AND workload_id=?",
            (lane, workload["workload_id"])).fetchone()
        status_seq = (int(current["status_seq"]) if current else 0) + 1
        if status_seq > MAX_STATUS_SEQ:
            raise OperationInvalid("status_seq agotó el rango durable")
        transition_id = _new_id("rst")
        at = at or _now_iso(self._clock())
        receipt_id = self._open_receipt(
            con, "transition", transition_id, principal_id, lane, to_status, at)
        con.execute(
            "INSERT INTO runtime_status_transitions(transition_id,lane,"
            "organization_revision,workload_id,target_runtime_instance,"
            "target_generation,from_status,to_status,detector_state,status_seq,"
            "cause_kind,cause_id,reason_code,observation_id,recovery_command_id,"
            "receipt_id,at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (transition_id, lane, workload["organization_revision"],
             workload["workload_id"], workload["runtime_instance"],
             workload["credential_generation"],
             current["status"] if current else None, to_status, detector_state,
             status_seq, cause_kind, cause_id, reason_code, observation_id,
             recovery_command_id, receipt_id, at))
        con.execute(
            "INSERT INTO runtime_status(lane,workload_id,organization_revision,"
            "principal_id,role,runtime_instance,credential_generation,status,"
            "detector_state,status_seq,status_since,last_observed_at,cause_id,"
            "transition_id,receipt_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(lane,workload_id) DO UPDATE SET"
            " organization_revision=excluded.organization_revision,"
            " principal_id=excluded.principal_id,role=excluded.role,"
            " runtime_instance=excluded.runtime_instance,"
            " credential_generation=excluded.credential_generation,"
            " status=excluded.status,detector_state=excluded.detector_state,"
            " status_seq=excluded.status_seq,status_since=excluded.status_since,"
            " cause_id=excluded.cause_id,transition_id=excluded.transition_id,"
            " receipt_id=excluded.receipt_id",
            (lane, workload["workload_id"], workload["organization_revision"],
             workload["principal_id"], workload["role"],
             workload["runtime_instance"], workload["credential_generation"],
             to_status, detector_state, status_seq, at, None, cause_id,
             transition_id, receipt_id))
        return transition_id, receipt_id, status_seq

    @_audita
    def activate_organization(
            self, token: str, *, revision: int, source_sha256: str,
            attestation_state: str,
            roles: Sequence[Mapping[str, Any]],
            reports: Sequence[Mapping[str, Any]],
            reviewers: Sequence[Mapping[str, Any]],
            escalations: Sequence[Mapping[str, Any]],
            workloads: Sequence[Mapping[str, Any]]) -> int:
        """Valida el grafo completo y activa una única revisión lane-local."""
        self._guard_mutable()
        if type(revision) is not int or isinstance(revision, bool) or revision <= 0:
            raise OrganizationConflict("revision debe ser entero positivo")
        if type(source_sha256) is not str or re.fullmatch(
                r"[0-9a-f]{64}", source_sha256) is None:
            raise OrganizationConflict("source_sha256 no es canónico")
        if attestation_state not in ORGANIZATION_ATTESTATION_STATES:
            raise OrganizationConflict("attestation_state fuera del vocabulario")
        roles = self._records(roles, {"role", "layer", "policy_code"}, "roles")
        reports = self._records(reports, {"role", "reports_to"}, "reports")
        reviewers = self._records(
            reviewers, {"role", "reviewer_role"}, "reviewers")
        escalations = self._records(
            escalations, {"role", "trigger_code", "target_role"}, "escalations")
        workloads = self._records(
            workloads, {"workload_id", "role", "principal_id",
                        "runtime_instance", "credential_generation"}, "workloads")

        role_names = set()
        role_layers = {}
        for row in roles:
            role = _control_id(row["role"], "role")
            if role in role_names or type(row["layer"]) is not int or not (
                    0 <= row["layer"] <= 255):
                raise OrganizationConflict("rol repetido o layer inválido")
            if (row["policy_code"] is not None
                    and row["policy_code"] not in ORGANIZATION_POLICY_CODES):
                raise OrganizationConflict("policy_code fuera del vocabulario")
            role_names.add(role)
            role_layers[role] = row["layer"]
        if not role_names:
            raise OrganizationConflict("una organización activa necesita roles")
        roots = {role for role, layer in role_layers.items() if layer == 0}
        if len(roots) != 1:
            raise OrganizationConflict(
                "el organigrama completo exige exactamente una raíz layer=0")

        parents = {}
        for edge in reports:
            child = _control_id(edge["role"], "role")
            parent = _control_id(edge["reports_to"], "reports_to")
            if child not in role_names or parent not in role_names or child == parent:
                raise OrganizationConflict("reports_to propio o hacia rol desconocido")
            if child in parents:
                raise OrganizationConflict("un rol sólo puede tener un reports_to")
            parents[child] = parent
        root = next(iter(roots))
        if root in parents or any(
                role != root and role not in parents for role in role_names):
            raise OrganizationConflict(
                "el organigrama está incompleto: sólo la raíz carece de superior")
        for start in role_names:
            seen = set()
            node = start
            while node in parents:
                if node in seen:
                    raise OrganizationConflict("el organigrama contiene un ciclo")
                seen.add(node)
                node = parents[node]
        if any(role_layers[parent] >= role_layers[child]
               for child, parent in parents.items()):
            raise OrganizationConflict(
                "reports_to debe apuntar a una capa estrictamente superior")
        for edge in reviewers:
            if (edge["role"] not in role_names or edge["reviewer_role"] not in role_names
                    or edge["role"] == edge["reviewer_role"]):
                raise OrganizationConflict("reviewer propio o desconocido")
        for edge in escalations:
            if (edge["role"] not in role_names or edge["target_role"] not in role_names
                    or edge["role"] == edge["target_role"]
                    or edge["trigger_code"] not in ESCALATION_TRIGGER_CODES):
                raise OrganizationConflict("escalado inválido o desconocido")
        workload_ids = set()
        for workload in workloads:
            wid = _control_id(workload["workload_id"], "workload_id")
            if wid in workload_ids or workload["role"] not in role_names:
                raise OrganizationConflict("workload repetido o con rol desconocido")
            paired = ((workload["runtime_instance"] is None) ==
                      (workload["credential_generation"] is None))
            if not paired:
                raise OrganizationConflict("runtime y generation deben venir juntos")
            if (workload["credential_generation"] is not None and
                    (type(workload["credential_generation"]) is not int or
                     workload["credential_generation"] <= 0)):
                raise OrganizationConflict("credential_generation inválida")
            workload_ids.add(wid)

        at = _now_iso(self._clock())
        self._tras_precheck()
        with self._tx() as con:
            view = _authenticate_locked(con, token, self._clock())
            if view is None:
                raise AuthError("sesión invalidada antes de activar organización")
            self._exigir_capacidad_locked(
                con, view, CAP_ORGANIZATION_ACTIVATE, "organization.activate")
            top = con.execute(
                "SELECT MAX(revision) r FROM organization_revisions WHERE lane=?",
                (view.lane,)).fetchone()["r"]
            if top is not None and revision <= int(top):
                raise OrganizationConflict("revision no monótona")
            con.execute(
                "INSERT INTO organization_revisions(lane,revision,source_sha256,"
                "attestation_state,active,activated_by,activated_runtime,activated_at)"
                " VALUES(?,?,?,?,0,?,?,?)",
                (view.lane, revision, source_sha256, attestation_state,
                 view.principal_id, view.runtime_instance, at))
            for row in roles:
                con.execute(
                    "INSERT INTO organization_roles(lane,revision,role,layer,policy_code)"
                    " VALUES(?,?,?,?,?)",
                    (view.lane, revision, row["role"], row["layer"], row["policy_code"]))
            for row in reports:
                con.execute(
                    "INSERT INTO organization_reports(lane,revision,role,reports_to)"
                    " VALUES(?,?,?,?)",
                    (view.lane, revision, row["role"], row["reports_to"]))
            for row in reviewers:
                con.execute(
                    "INSERT INTO organization_reviewers(lane,revision,role,reviewer_role)"
                    " VALUES(?,?,?,?)",
                    (view.lane, revision, row["role"], row["reviewer_role"]))
            for row in escalations:
                con.execute(
                    "INSERT INTO organization_escalations(lane,revision,role,"
                    "trigger_code,target_role) VALUES(?,?,?,?,?)",
                    (view.lane, revision, row["role"], row["trigger_code"],
                     row["target_role"]))
            for row in workloads:
                if row["principal_id"] is not None:
                    principal = con.execute(
                        "SELECT role FROM principals WHERE lane=? AND principal_id=?",
                        (view.lane, row["principal_id"])).fetchone()
                    if principal is None or principal["role"] != row["role"]:
                        raise OrganizationConflict(
                            "principal de workload desconocido o de otro rol/carril")
                if row["runtime_instance"] is not None:
                    runtime = con.execute(
                        "SELECT principal_id,role FROM runtime_sessions WHERE lane=?"
                        " AND runtime_instance=? AND generation=?",
                        (view.lane, row["runtime_instance"],
                         row["credential_generation"])).fetchone()
                    if (runtime is None or runtime["role"] != row["role"] or
                            (row["principal_id"] is not None and
                             runtime["principal_id"] != row["principal_id"])):
                        raise OrganizationConflict(
                            "runtime de workload no casa con lane/rol/principal/generación")
                    if row["principal_id"] is None:
                        # La identidad sale de la runtime_session; nunca queda
                        # `fresh` con principal NULL por omisión del snapshot.
                        row["principal_id"] = runtime["principal_id"]
                con.execute(
                    "INSERT INTO expected_workloads(lane,organization_revision,"
                    "workload_id,role,principal_id,runtime_instance,"
                    "credential_generation) VALUES(?,?,?,?,?,?,?)",
                    (view.lane, revision, row["workload_id"], row["role"],
                     row["principal_id"], row["runtime_instance"],
                     row["credential_generation"]))
            con.execute("UPDATE organization_revisions SET active=0 WHERE lane=?",
                        (view.lane,))
            con.execute("UPDATE organization_revisions SET active=1"
                        " WHERE lane=? AND revision=?", (view.lane, revision))
            for row in workloads:
                workload = self._active_workload_locked(
                    con, view.lane, row["workload_id"])
                self._runtime_transition_locked(
                    con, lane=view.lane, workload=workload,
                    principal_id=view.principal_id, to_status="absent",
                    detector_state=None, cause_kind="organization",
                    cause_id=f"org:{revision}",
                    reason_code="ORGANIZATION_ACTIVATED", at=at)
                con.execute(
                    "UPDATE runtime_status SET last_observed_at=NULL WHERE lane=?"
                    " AND workload_id=?",
                    (view.lane, row["workload_id"]))
            # Las proyecciones de workloads retirados no se sirven como activos.
            if workload_ids:
                marks = ",".join("?" for _ in workload_ids)
                con.execute(
                    f"DELETE FROM runtime_status WHERE lane=? AND workload_id NOT IN ({marks})",
                    (view.lane, *sorted(workload_ids)))
            else:
                con.execute("DELETE FROM runtime_status WHERE lane=?", (view.lane,))
        return revision

    @_audita
    def record_runtime_observation(
            self, token: str, *, workload_id: str, runtime_instance: str,
            idempotency_key: str, supervisor_seq: int, observation_kind: str,
            reason_code: str, detector_state: str | None = None,
            cpu_millis: int | None = None, rss_bytes: int | None = None,
            heartbeat_age_ms: int | None = None, exit_code: int | None = None,
            recovery_command_id: str | None = None) -> RuntimeObservation:
        """Persiste observación+proyección+transición+recibo en un solo commit."""
        self._guard_mutable()
        workload_id = _control_id(workload_id, "workload_id")
        runtime_instance = _control_id(runtime_instance, "runtime_instance")
        idempotency_key = _control_id(idempotency_key, "idempotency_key")
        if type(supervisor_seq) is not int or isinstance(supervisor_seq, bool) or not (
                0 < supervisor_seq <= MAX_SUPERVISOR_SEQ):
            raise ObservationSequenceConflict("supervisor_seq fuera de rango")
        if observation_kind not in RUNTIME_OBSERVATION_KINDS:
            raise OperationInvalid("observation_kind fuera del vocabulario")
        if reason_code not in _OBSERVATION_REASONS[observation_kind]:
            raise OperationInvalid("reason_code no corresponde a observation_kind")
        if detector_state is not None and detector_state not in DETECTOR_STATES:
            raise OperationInvalid("detector_state fuera del vocabulario M3")
        metrics = {
            "cpu_millis": (cpu_millis, 0, MAX_CPU_MILLIS),
            "rss_bytes": (rss_bytes, 0, MAX_RSS_BYTES),
            "heartbeat_age_ms": (heartbeat_age_ms, 0, MAX_HEARTBEAT_AGE_MS),
            "exit_code": (exit_code, -(1 << 31), (1 << 31) - 1),
        }
        for name, (value, lower, upper) in metrics.items():
            if value is not None and (type(value) is not int or isinstance(value, bool)
                                      or not lower <= value <= upper):
                raise OperationInvalid(f"{name} fuera de rango")
        if observation_kind in {"recovery_succeeded", "recovery_failed"}:
            recovery_command_id = _control_id(
                recovery_command_id, "recovery_command_id")
        elif recovery_command_id is not None:
            raise OperationInvalid("sólo un resultado de recovery cita command_id")
        request = {
            "workload_id": workload_id, "runtime_instance": runtime_instance,
            "supervisor_seq": supervisor_seq, "observation_kind": observation_kind,
            "reason_code": reason_code, "detector_state": detector_state,
            "cpu_millis": cpu_millis, "rss_bytes": rss_bytes,
            "heartbeat_age_ms": heartbeat_age_ms, "exit_code": exit_code,
            "recovery_command_id": recovery_command_id,
        }
        req_hash = _sha256(_canonical(request))
        observed_at = _now_iso(self._clock())
        self._tras_precheck()
        with self._tx() as con:
            view = _authenticate_locked(con, token, self._clock())
            if view is None:
                raise AuthError("sesión invalidada antes de observar")
            self._exigir_capacidad_locked(
                con, view, CAP_RUNTIME_OBSERVE, "runtime.observe")
            previous = con.execute(
                "SELECT * FROM runtime_observations WHERE observer_principal=?"
                " AND lane=? AND verb='runtime.observe' AND idempotency_key=?",
                (view.principal_id, view.lane, idempotency_key)).fetchone()
            if previous is not None:
                if not hmac.compare_digest(previous["req_hash"], req_hash):
                    raise IdempotencyConflict(
                        "misma clave de observación con cuerpo distinto")
                transition = con.execute(
                    "SELECT transition_id,receipt_id,to_status"
                    " FROM runtime_status_transitions"
                    " WHERE observation_id=?", (previous["observation_id"],)).fetchone()
                return RuntimeObservation(
                    previous["observation_id"], view.lane, previous["workload_id"],
                    previous["target_runtime_instance"],
                    int(previous["target_generation"]),
                    int(previous["supervisor_seq"]),
                    (transition["to_status"] if transition else
                     _OBSERVATION_STATUS[previous["observation_kind"]]),
                    transition["transition_id"] if transition else None,
                    transition["receipt_id"] if transition else None, True,
                    previous["observed_at"])
            workload = self._active_workload_locked(con, view.lane, workload_id)
            if (workload is None or workload["runtime_instance"] != runtime_instance
                    or workload["credential_generation"] is None):
                raise SubjectNotFound("runtime target no existe en este carril")
            # Un sensor no certifica su propio proceso. Runtime es un CHECK
            # durable; principal necesita esta guarda porque el target principal
            # vive en expected_workloads y SQLite no permite un CHECK cross-table.
            if (view.runtime_instance == runtime_instance
                    or view.principal_id == workload["principal_id"]):
                raise OperationInvalid(
                    "el observer y el target deben ser identidades distintas")
            latest = con.execute(
                "SELECT MAX(supervisor_seq) s FROM runtime_observations"
                " WHERE observer_principal=? AND observer_runtime=?"
                " AND observer_generation=? AND lane=?"
                " AND target_runtime_instance=? AND target_generation=?",
                (view.principal_id, view.runtime_instance, view.generation,
                 view.lane, runtime_instance,
                 workload["credential_generation"])).fetchone()["s"]
            if latest is not None and supervisor_seq <= int(latest):
                raise ObservationSequenceConflict(
                    "supervisor_seq repetido o regresivo para esta generación target",
                    latest=int(latest))
            recovery = None
            if recovery_command_id is not None:
                recovery = con.execute(
                    "SELECT r.*,c.state AS command_state,s.status AS runtime_state,"
                    " t.recovery_command_id AS active_recovery_command"
                    " FROM runtime_recoveries r JOIN commands c"
                    " ON c.command_id=r.command_id JOIN runtime_status s"
                    " ON s.lane=r.lane AND s.workload_id=r.workload_id"
                    " JOIN runtime_status_transitions t"
                    " ON t.transition_id=s.transition_id"
                    " WHERE r.lane=? AND r.workload_id=?"
                    " AND r.organization_revision=?"
                    " AND r.target_runtime_instance=?"
                    " AND r.target_generation=? AND r.command_id=?",
                    (view.lane, workload_id, workload["organization_revision"],
                     runtime_instance, workload["credential_generation"],
                     recovery_command_id)).fetchone()
                if (recovery is None or recovery["command_state"] != "executing"
                        or recovery["runtime_state"] != "recovering"
                        or recovery["active_recovery_command"] != recovery_command_id):
                    raise RecoveryConflict(
                        "el command no es la recovery ejecutándose para este target")
            observation_id = _new_id("obs")
            con.execute(
                "INSERT INTO runtime_observations(observation_id,lane,"
                "organization_revision,workload_id,target_runtime_instance,"
                "target_generation,observer_principal,observer_runtime,"
                "observer_generation,verb,idempotency_key,req_hash,supervisor_seq,"
                "observation_kind,reason_code,detector_state,cpu_millis,rss_bytes,"
                "heartbeat_age_ms,exit_code,recovery_command_id,observed_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,'runtime.observe',?,?,?,?,?,?,?,?,?,?,?,?)",
                (observation_id, view.lane, workload["organization_revision"],
                 workload_id, runtime_instance, workload["credential_generation"],
                 view.principal_id, view.runtime_instance, view.generation,
                 idempotency_key, req_hash, supervisor_seq, observation_kind,
                 reason_code, detector_state, cpu_millis, rss_bytes,
                 heartbeat_age_ms, exit_code, recovery_command_id, observed_at))
            if recovery is not None:
                terminal = ("succeeded" if observation_kind == "recovery_succeeded"
                            else "failed")
                self._advance_command_receipt_locked(
                    con, recovery_command_id, terminal, observed_at)
            current = con.execute(
                "SELECT s.*,t.reason_code AS transition_reason,"
                " t.recovery_command_id AS current_recovery_command"
                " FROM runtime_status s"
                " JOIN runtime_status_transitions t USING (transition_id)"
                " WHERE s.lane=? AND s.workload_id=?",
                (view.lane, workload_id)).fetchone()
            desired = _OBSERVATION_STATUS[observation_kind]
            protected_cycle = (observation_kind == "cycle_ack" and current is not None
                               and current["status"] in {
                                   "stopped", "degraded", "recovering"})
            # `recovering` sólo se entra vía `request_runtime_recovery` aceptado
            # — SIEMPRE hay un command real detrás. Ninguna observación que no
            # sea el outcome tipado de ESE command puede sacarla de ahí: ni
            # `cycle_ack` (ya cubierto arriba), ni `started`/`exited`/
            # `resource_degraded`/`resource_recovered` — un recurso que vuelve
            # a nivel sano no es un command que se resolvió, y sacar la
            # proyección de `recovering` por esa vía deja al recovery manual
            # en curso sin testigo (hallazgo codex/cto, MARK:astra-review-1725-
            # 20260908 y MARK:astra-supervisor-729-followup-20260908). Fuera de
            # `recovering` (degraded/stopped/fresh/stale/…) esto no cambia nada:
            # `resource_recovered` sigue limpiando un `degraded` puro, sin
            # command de por medio, exactamente como hoy.
            protected_recovery = (current is not None
                                  and current["status"] == "recovering"
                                  and observation_kind not in {
                                      "recovery_succeeded", "recovery_failed"})
            if protected_cycle or protected_recovery:
                desired = current["status"]
            transition_id = receipt_id = None
            if (protected_cycle or protected_recovery or current is None
                    or current["status"] != desired
                    or current["transition_reason"] != reason_code
                    or current["detector_state"] != detector_state):
                # Una observación PROTEGIDA no es un outcome — su propio
                # `recovery_command_id` (casi siempre None) no debe pisar el
                # enlace que YA sostiene la transición vigente: el outcome que
                # llegue después lo busca en LA TRANSICIÓN ACTUAL
                # (`t.recovery_command_id AS active_recovery_command`, más
                # abajo en este mismo fichero), y una transición "protegida"
                # que lo borra deja al outcome válido sin cómo cerrar
                # (hallazgo codex, MARK:astra-guard-link-followup-20260908).
                # Conservar el enlace vigente en vez del de ESTA observación.
                enlace = (current["current_recovery_command"]
                         if (protected_cycle or protected_recovery)
                         and current is not None else recovery_command_id)
                transition_id, receipt_id, _ = self._runtime_transition_locked(
                    con, lane=view.lane, workload=workload,
                    principal_id=view.principal_id, to_status=desired,
                    detector_state=detector_state, cause_kind="observation",
                    cause_id=observation_id, reason_code=reason_code,
                    observation_id=observation_id,
                    recovery_command_id=enlace, at=observed_at)
            con.execute(
                "UPDATE runtime_status SET last_observed_at=? WHERE lane=?"
                " AND workload_id=?", (observed_at, view.lane, workload_id))
            return RuntimeObservation(
                observation_id, view.lane, workload_id, runtime_instance,
                int(workload["credential_generation"]), supervisor_seq, desired,
                transition_id, receipt_id, False, observed_at)

    @_audita
    def evaluate_runtime_deadlines(self, token: str, *, stale_after_s: int) -> int:
        """Timeout produce ``stale`` y nunca ``stopped``; el reloj es servidor."""
        self._guard_mutable()
        if type(stale_after_s) is not int or isinstance(stale_after_s, bool) or not (
                0 < stale_after_s <= 31 * 24 * 60 * 60):
            raise OperationInvalid("stale_after_s fuera de rango")
        now = self._clock()
        at = _now_iso(now)
        with self._tx() as con:
            view = _authenticate_locked(con, token, now)
            if view is None:
                raise AuthError("sesión invalidada antes de evaluar deadlines")
            self._exigir_capacidad_locked(
                con, view, CAP_RUNTIME_OBSERVE, "runtime.observe")
            candidates = con.execute(
                "SELECT s.*,w.* FROM runtime_status s"
                " JOIN expected_workloads w ON w.lane=s.lane"
                "  AND w.organization_revision=s.organization_revision"
                "  AND w.workload_id=s.workload_id"
                " WHERE s.lane=? AND s.status IN ('fresh','degraded')"
                " AND s.last_observed_at IS NOT NULL",
                (view.lane,)).fetchall()
            changed = 0
            for row in candidates:
                last = con.execute(
                    "SELECT MAX(rowid) r FROM runtime_observations WHERE lane=?"
                    " AND workload_id=? AND target_runtime_instance=?"
                    " AND target_generation=?",
                    (view.lane, row["workload_id"],
                     row["runtime_instance"], row["credential_generation"])
                ).fetchone()["r"]
                observed = con.execute(
                    "SELECT observed_at FROM runtime_observations WHERE rowid=?",
                    (last,)).fetchone()["observed_at"]
                observed_epoch = datetime.datetime.fromisoformat(
                    observed.replace("Z", "+00:00")).timestamp()
                if now - observed_epoch <= stale_after_s:
                    continue
                self._runtime_transition_locked(
                    con, lane=view.lane, workload=row,
                    principal_id=view.principal_id, to_status="stale",
                    detector_state=row["detector_state"], cause_kind="deadline",
                    cause_id=f"deadline:{row['status_seq']}",
                    reason_code="DEADLINE_EXCEEDED", at=at)
                changed += 1
            return changed

    def _revalidate_recovery_fence_locked(
            self, con: sqlite3.Connection, recovery: sqlite3.Row) -> None:
        """Revalida target, identidad solicitante y valla en la misma tx.

        El worker que recibe/arranca el command no tiene por qué ser dueño del
        lease. Por eso no se reutiliza `_fence_locked`, que compara contra la
        sesión del llamante: se compara contra la identidad durable que creó la
        recovery y se exige que tanto esa sesión como el lease sigan vigentes.
        """
        canonical = self._recurso(f"runtime/{recovery['workload_id']}")
        if recovery["fenced_resource"] != canonical:
            raise FencingConflict("recovery ligada a un recurso no canónico")
        workload = self._active_workload_locked(
            con, recovery["lane"], recovery["workload_id"])
        if (workload is None
                or int(workload["organization_revision"]) !=
                   int(recovery["organization_revision"])
                or workload["runtime_instance"] !=
                   recovery["target_runtime_instance"]
                or int(workload["credential_generation"]) !=
                   int(recovery["target_generation"])):
            raise RecoveryConflict("el target de recovery ya no es el activo")
        now = self._clock()
        requester = con.execute(
            "SELECT 1 FROM runtime_sessions WHERE lane=? AND principal_id=?"
            " AND runtime_instance=? AND generation=? AND revoked_at IS NULL"
            " AND expires_at>?",
            (recovery["lane"], recovery["requester_principal"],
             recovery["requester_runtime"], recovery["requester_generation"],
             now)).fetchone()
        if requester is None:
            raise FencingConflict("la identidad que adquirió la valla ya no está vigente")
        lease = con.execute(
            "SELECT * FROM leases WHERE lane=? AND resource=?",
            (recovery["lane"], canonical)).fetchone()
        if (lease is None or lease["released_at"] is not None
                or lease["expires_at"] <= now
                or lease["principal_id"] != recovery["requester_principal"]
                or lease["runtime_instance"] != recovery["requester_runtime"]
                or int(lease["fencing_token"]) != int(recovery["fencing_token"])):
            raise FencingConflict("la valla de recovery ya no es la vigente")

    def _advance_command_receipt_locked(
            self, con: sqlite3.Connection, command_id: str,
            new_state: str, at: str) -> str:
        """Mueve command y receipt como una sola verdad, sin detalle libre."""
        changed = con.execute(
            "UPDATE commands SET state=? WHERE command_id=?",
            (new_state, command_id)).rowcount
        if changed != 1:
            raise CommandTransitionInvalid(f"`{command_id}` no existe")
        receipt = con.execute(
            "SELECT receipt_id FROM receipts WHERE subject_kind='command'"
            " AND subject_id=?", (command_id,)).fetchone()
        if receipt is None:
            raise CommandTransitionInvalid(f"`{command_id}` sin recibo")
        seq = con.execute(
            "SELECT COALESCE(MAX(seq),0) s FROM receipt_transitions"
            " WHERE receipt_id=?", (receipt["receipt_id"],)).fetchone()["s"]
        transition_id = _new_id("trn")
        con.execute(
            "INSERT INTO receipt_transitions(transition_id,receipt_id,seq,state,"
            "at,detail) VALUES(?,?,?,?,?,NULL)",
            (transition_id, receipt["receipt_id"], int(seq) + 1, new_state, at))
        con.execute(
            "UPDATE receipts SET current_state=?,updated_at=? WHERE receipt_id=?",
            (new_state, at, receipt["receipt_id"]))
        return transition_id

    @_audita
    def request_runtime_recovery(
            self, token: str, *, workload_id: str, runtime_instance: str,
            idempotency_key: str, reason_code: str,
            action_code: str, fenced_resource: str,
            fencing_token: int) -> RuntimeRecovery:
        """Liga una recovery idempotente a un command y a su fencing exacto."""
        self._guard_mutable()
        workload_id = _control_id(workload_id, "workload_id")
        runtime_instance = _control_id(runtime_instance, "runtime_instance")
        idempotency_key = _control_id(idempotency_key, "idempotency_key")
        if reason_code not in RECOVERY_REASON_CODES:
            raise OperationInvalid("reason_code de recovery fuera del vocabulario")
        if action_code not in RECOVERY_ACTION_CODES:
            raise OperationInvalid("action_code de recovery fuera del vocabulario")
        if type(fencing_token) is not int or isinstance(fencing_token, bool) or fencing_token <= 0:
            raise RecoveryConflict("fencing_token debe ser entero positivo")
        fenced_resource = self._recurso(fenced_resource)
        request = {"workload_id": workload_id, "runtime_instance": runtime_instance,
                   "reason_code": reason_code, "action_code": action_code,
                   "fenced_resource": fenced_resource,
                   "fencing_token": fencing_token}
        req_hash = _sha256(_canonical(request))
        at = _now_iso(self._clock())
        self._tras_precheck()
        with self._tx() as con:
            view = _authenticate_locked(con, token, self._clock())
            if view is None:
                raise AuthError("sesión invalidada antes de recovery")
            self._exigir_capacidad_locked(
                con, view, CAP_RUNTIME_RECOVER, "runtime.recover")
            previous = con.execute(
                "SELECT * FROM runtime_recoveries WHERE requester_principal=?"
                " AND lane=? AND verb='runtime.recover' AND idempotency_key=?",
                (view.principal_id, view.lane, idempotency_key)).fetchone()
            if previous is not None:
                if not hmac.compare_digest(previous["req_hash"], req_hash):
                    raise IdempotencyConflict("misma clave de recovery con cuerpo distinto")
                transitions = con.execute(
                    "SELECT transition_id,receipt_id FROM runtime_status_transitions"
                    " WHERE recovery_command_id=? AND cause_kind='recovery'"
                    " AND to_status='recovering' ORDER BY status_seq,transition_id",
                    (previous["command_id"],)).fetchall()
                if len(transitions) != 1:
                    raise RecoveryConflict(
                        "recovery idempotente sin una aceptación durable inequívoca")
                transition = transitions[0]
                return RuntimeRecovery(
                    previous["recovery_id"], previous["command_id"], view.lane,
                    previous["workload_id"], previous["target_runtime_instance"],
                    int(previous["target_generation"]), transition["transition_id"],
                    transition["receipt_id"], True, previous["accepted_at"])
            workload = self._active_workload_locked(con, view.lane, workload_id)
            if (workload is None or workload["runtime_instance"] != runtime_instance
                    or workload["credential_generation"] is None):
                raise SubjectNotFound("runtime target no existe en este carril")
            canonical_resource = self._recurso(f"runtime/{workload_id}")
            if fenced_resource != canonical_resource:
                raise RecoveryConflict(
                    "fenced_resource debe identificar exactamente el workload target")
            self._fence_locked(con, view, fenced_resource, fencing_token)
            active = con.execute(
                "SELECT r.command_id FROM runtime_recoveries r JOIN commands c"
                " ON c.command_id=r.command_id WHERE r.lane=? AND r.workload_id=?"
                " AND r.target_generation=? AND c.state IN "
                "('accepted','received','executing') LIMIT 1",
                (view.lane, workload_id,
                 workload["credential_generation"])).fetchone()
            if active is not None:
                raise RecoveryConflict(
                    "la generación target ya tiene una recovery activa")
            workstream = f"runtime-recovery/{workload_id}"
            top = con.execute(
                "SELECT COALESCE(MAX(revision),0) r FROM commands WHERE lane=?"
                " AND workstream_id=?", (view.lane, workstream)).fetchone()["r"]
            revision = int(top) + 1
            command_id = _new_id("cmd")
            payload = _canonical({
                "action_code": action_code, "reason_code": reason_code,
                "workload_id": workload_id, "runtime_instance": runtime_instance,
                "target_generation": workload["credential_generation"],
                "fenced_resource": fenced_resource, "fencing_token": fencing_token,
            })
            con.execute(
                "INSERT INTO commands(command_id,workstream_id,revision,lane,"
                "principal_id,role,runtime_instance,attribution_status,payload,state,"
                "supersedes,created_at) VALUES(?,?,?,?,?,?,?,'verified',?,'accepted',NULL,?)",
                (command_id, workstream, revision, view.lane, view.principal_id,
                 view.role, view.runtime_instance, payload, at))
            self._open_receipt(
                con, "command", command_id, view.principal_id, view.lane,
                "accepted", at)
            recovery_id = _new_id("rcv")
            con.execute(
                "INSERT INTO runtime_recoveries(recovery_id,lane,"
                "organization_revision,workload_id,target_runtime_instance,"
                "target_generation,requester_principal,requester_runtime,"
                "requester_generation,verb,idempotency_key,req_hash,reason_code,"
                "action_code,fenced_resource,fencing_token,command_id,accepted_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,'runtime.recover',?,?,?,?,?,?,?,?)",
                (recovery_id, view.lane, workload["organization_revision"],
                 workload_id, runtime_instance, workload["credential_generation"],
                 view.principal_id, view.runtime_instance, view.generation,
                 idempotency_key, req_hash, reason_code, action_code,
                 fenced_resource, fencing_token, command_id, at))
            transition_id, receipt_id, _ = self._runtime_transition_locked(
                con, lane=view.lane, workload=workload,
                principal_id=view.principal_id, to_status="recovering",
                detector_state=None, cause_kind="recovery", cause_id=recovery_id,
                reason_code="RECOVERY_REQUESTED", recovery_command_id=command_id, at=at)
            return RuntimeRecovery(
                recovery_id, command_id, view.lane, workload_id, runtime_instance,
                int(workload["credential_generation"]), transition_id, receipt_id,
                False, at)

    @_audita
    def advance_runtime_recovery(
            self, token: str, command_id: str, new_state: str, *,
            workload_id: str, runtime_instance: str) -> str:
        """Única puerta no terminal para recovery: accepted→received→executing.

        Outcome no entra aquí: lo firma una observación tipada y en esa misma
        transacción terminaliza command+receipt+runtime_status+transition.

        `workload_id`/`runtime_instance` ATAN el command al target declarado
        por el llamante con la MISMA condición autoritativa que
        ``record_runtime_observation`` ya exige para el outcome — target
        activo de la revisión vigente (no uno viejo), y la transición ACTUAL
        de `runtime_status` debe seguir apuntando a ESTE command_id como su
        `recovery_command_id`. Sin esto, un carril con DOS recoveries en
        vuelo (A y B) podía avanzar el command de A mientras el llamante
        declaraba —y la acción externa actuaba sobre— el target B (hallazgo
        codex/cto, MARK:astra-review-1725-20260908); comparar sólo dos
        columnas de la fila histórica de `runtime_recoveries` tampoco cazaba
        un target que cambió de revisión/generación entretanto ni un command
        cuyo enlace ya no es el activo (MARK:astra-guard-link-followup-
        20260908) — de ahí el mismo JOIN completo que usa el outcome, no una
        versión más corta.
        """
        self._guard_mutable()
        command_id = _control_id(command_id, "command_id")
        workload_id = _control_id(workload_id, "workload_id")
        runtime_instance = _control_id(runtime_instance, "runtime_instance")
        if new_state not in {"received", "executing"}:
            raise CommandTransitionInvalid(
                "recovery sólo admite received/executing; outcome es tipado")
        at = _now_iso(self._clock())
        with self._tx() as con:
            view = _authenticate_locked(con, token, self._clock())
            if view is None:
                raise AuthError("sesión invalidada antes de avanzar recovery")
            self._exigir_capacidad_locked(
                con, view, CAP_COMMAND_WORKER, "advance_runtime_recovery")
            workload = self._active_workload_locked(con, view.lane, workload_id)
            if workload is None or workload["runtime_instance"] != runtime_instance:
                raise RecoveryConflict(
                    "el target declarado no es el activo de este carril")
            recovery = con.execute(
                "SELECT r.*,c.state AS command_state,s.status AS runtime_state,"
                " t.recovery_command_id AS active_recovery_command"
                " FROM runtime_recoveries r JOIN commands c"
                " ON c.command_id=r.command_id JOIN runtime_status s"
                " ON s.lane=r.lane AND s.workload_id=r.workload_id"
                " JOIN runtime_status_transitions t"
                " ON t.transition_id=s.transition_id"
                " WHERE r.lane=? AND r.workload_id=? AND r.organization_revision=?"
                " AND r.target_runtime_instance=? AND r.target_generation=?"
                " AND r.command_id=?",
                (view.lane, workload_id, workload["organization_revision"],
                 runtime_instance, workload["credential_generation"],
                 command_id)).fetchone()
            if (recovery is None or recovery["runtime_state"] != "recovering"
                    or recovery["active_recovery_command"] != command_id):
                raise RecoveryConflict(
                    "el command no es la recovery en vuelo para el target "
                    "declarado — binding workload/runtime/revisión no coincide")
            expected = "accepted" if new_state == "received" else "received"
            if recovery["command_state"] != expected:
                raise CommandTransitionInvalid(
                    f"recovery `{recovery['command_state']}` no admite `{new_state}`")
            self._revalidate_recovery_fence_locked(con, recovery)
            return self._advance_command_receipt_locked(
                con, command_id, new_state, at)

    def active_recovery(self, token: str, command_id: str) -> dict[str, Any]:
        """Lectura autoritativa lane-scoped de una recovery: un SNAPSHOT del
        binding command↔target↔acción, para que un ejecutor externo se
        rechace a sí mismo rápido ANTES de invocar su efecto (contrato
        propuesto por backend, MARK:astra-followup-1730-20260908 /
        recuperacion_manual.py).

        Un command ajeno o inexistente en este carril produce
        ``SubjectNotFound`` — no hay oráculo de enumeración cross-lane
        (ADR-002 Decisión 7). Existir en `runtime_recoveries` NO basta para
        ser ejecutable: eso lo dice ``vigente``.

        ``vigente`` es True SÓLO SI, LAS CUATRO: (1) el target ORIGINAL de
        esta recovery (revisión, runtime, generación — congelados al
        aceptar) sigue siendo el target ACTIVO del carril AHORA; (2)
        `command_state` está en la ALLOWLIST `{accepted, received,
        executing}` — no una denylist de `{succeeded, failed}`: el
        esquema admite SIETE estados (`:1366-1367`), y `cancelled`/
        `superseded` son tan terminales como los otros dos; excluir por
        lista blanca deja cualquier estado futuro fuera por defecto, no
        dentro por descuido — comprobado explícito, no inferido del
        estado runtime aunque hoy ambos se actualizan en la misma
        transacción; (3) el estado runtime AHORA es `recovering`; (4) la
        transición vigente sigue enlazando a ESTE `command_id`.

        ⚠️ **Lo que ``vigente`` NO comprueba — dicho explícito para que
        nadie lo lea de más** (hallazgo codex, MARK:astra-observer-9a71b1b-
        active-read-20260908, dos rondas): NO revalida la valla/lease
        (`advance_runtime_recovery` sí lo hace, dentro de su propia
        transacción, vía `_revalidate_recovery_fence_locked`) — esta
        lectura no toca leases. NO concede permiso ni capacidad de
        ejecutar nada: un `vigente=True` es un snapshot que puede quedar
        obsoleto en el instante siguiente a esta llamada, no una
        autorización. NO sustituye a `advance_runtime_recovery`/
        `record_runtime_observation`, que siguen siendo quienes deciden
        con autoridad, dentro de su propia transacción, en el momento del
        avance real — esto es sólo un pre-vuelo de rechazo rápido.

        ``runtime_state`` viaja `None` SÓLO en dos casos: el target se
        sustituyó (nueva revisión organizativa) o no existe proyección de
        `runtime_status` en absoluto para el workload. **NO es `None`
        cuando la recovery es terminal con el target sin sustituir** — ahí
        `runtime_state` lleva el valor real post-outcome (`fresh` tras
        `recovery_succeeded`, `degraded` tras `recovery_failed`); es
        `vigente` quien pasa a False, no `runtime_state` quien se vacía.
        """
        command_id = _control_id(command_id, "command_id")
        with self._lectura() as (con, _):
            view = _authenticate_locked(con, token, self._clock())
            if view is None:
                raise AuthError("se requiere sesión válida")
            self._exigir_capacidad_locked(
                con, view, CAP_COMMAND_WORKER, "runtime.recover")
            recovery = con.execute(
                "SELECT r.*,c.state AS command_state FROM runtime_recoveries r"
                " JOIN commands c ON c.command_id=r.command_id"
                " WHERE r.lane=? AND r.command_id=?",
                (view.lane, command_id)).fetchone()
            if recovery is None:
                raise SubjectNotFound(
                    "command no es una recovery de este carril")
            workload = self._active_workload_locked(
                con, view.lane, recovery["workload_id"])
            vigente = False
            runtime_state = None
            if (workload is not None
                    and workload["organization_revision"]
                    == recovery["organization_revision"]
                    and workload["runtime_instance"]
                    == recovery["target_runtime_instance"]
                    and workload["credential_generation"]
                    == recovery["target_generation"]):
                # El target ORIGINAL de esta recovery sigue siendo el activo
                # — sin esto, leer el estado runtime actual le atribuiría a
                # este command el destino de OTRO target que lo sustituyó.
                estado = con.execute(
                    "SELECT s.status,t.recovery_command_id"
                    " AS active_recovery_command FROM runtime_status s"
                    " JOIN runtime_status_transitions t USING (transition_id)"
                    " WHERE s.lane=? AND s.workload_id=?",
                    (view.lane, recovery["workload_id"])).fetchone()
                if estado is not None:
                    runtime_state = estado["status"]
                    # ALLOWLIST, no denylist (hallazgo codex,
                    # MARK:astra-observer-9a71b1b-active-read-20260908,
                    # 3ª ronda): `commands.state` tiene SIETE valores
                    # (`:1366-1367`), no dos — `cancelled`/`superseded` son
                    # tan terminales como `succeeded`/`failed`, y un
                    # `not in {"succeeded","failed"}` los habría dejado
                    # pasar como si siguieran en curso. Fail-closed: sólo
                    # los tres estados que SÍ progresan cuentan como
                    # vigentes; cualquier estado nuevo que el esquema
                    # admita mañana queda excluido por defecto, no incluido
                    # por descuido.
                    vigente = (recovery["command_state"] in
                              {"accepted", "received", "executing"}
                              and runtime_state == "recovering"
                              and estado["active_recovery_command"] == command_id)
            return {
                "command_id": recovery["command_id"],
                "workload_id": recovery["workload_id"],
                "runtime_instance": recovery["target_runtime_instance"],
                "target_generation": recovery["target_generation"],
                "action_code": recovery["action_code"],
                "fenced_resource": recovery["fenced_resource"],
                "fencing_token": recovery["fencing_token"],
                "state": recovery["command_state"],
                "runtime_state": runtime_state,
                "vigente": vigente,
            }

    def runtime_statuses(self, token: str) -> tuple[dict[str, Any], ...]:
        """Vista lane-scoped del estado activo; cross-lane equivale a ausencia."""
        with self._lectura() as (con, _):
            view = _authenticate_locked(con, token, self._clock())
            if view is None:
                raise AuthError("se requiere sesión válida")
            self._exigir_capacidad_locked(con, view, CAP_RUNTIME_READ, "runtime.read")
            return tuple(dict(row) for row in con.execute(
                "SELECT s.* FROM runtime_status s JOIN organization_revisions o"
                " ON o.lane=s.lane AND o.revision=s.organization_revision"
                " WHERE s.lane=? AND o.active=1 ORDER BY s.workload_id",
                (view.lane,)))

    def runtime_status(self, token: str, runtime_instance: str) -> dict[str, Any]:
        runtime_instance = _control_id(runtime_instance, "runtime_instance")
        with self._lectura() as (con, _):
            view = _authenticate_locked(con, token, self._clock())
            if view is None:
                raise AuthError("se requiere sesión válida")
            self._exigir_capacidad_locked(con, view, CAP_RUNTIME_READ, "runtime.read")
            row = con.execute(
                "SELECT s.* FROM runtime_status s JOIN organization_revisions o"
                " ON o.lane=s.lane AND o.revision=s.organization_revision"
                " WHERE s.lane=? AND s.runtime_instance=? AND o.active=1",
                (view.lane, runtime_instance)).fetchone()
            if row is None:
                raise SubjectNotFound("runtime target no existe en este carril")
            return dict(row)

    def organization(self, token: str) -> dict[str, Any]:
        """Snapshot autoritativo de una revisión; jamás mezcla `/organigrama`."""
        with self._lectura() as (con, _):
            view = _authenticate_locked(con, token, self._clock())
            if view is None:
                raise AuthError("se requiere sesión válida")
            self._exigir_capacidad_locked(
                con, view, CAP_ORGANIZATION_READ, "organization.read")
            revision = con.execute(
                "SELECT * FROM organization_revisions WHERE lane=? AND active=1",
                (view.lane,)).fetchone()
            if revision is None:
                raise SubjectNotFound("no existe organización activa en este carril")
            number = revision["revision"]
            def rows(table: str):
                return [dict(row) for row in con.execute(
                    f"SELECT * FROM {table} WHERE lane=? AND revision=? ORDER BY rowid",
                    (view.lane, number))]
            workloads = [dict(row) for row in con.execute(
                "SELECT * FROM expected_workloads WHERE lane=?"
                " AND organization_revision=? ORDER BY workload_id",
                (view.lane, number))]
            return {
                "authority": True, "revision": dict(revision),
                "roles": rows("organization_roles"),
                "reports": rows("organization_reports"),
                "reviewers": rows("organization_reviewers"),
                "escalations": rows("organization_escalations"),
                "workloads": workloads,
            }

    # ── leases y fencing ─────────────────────────────────────────────────────
    @_audita
    def acquire_lease(self, token: str, resource: str, *, ttl_s: int = 300) -> Lease:
        """Coge el recurso si está libre o VENCIDO. Cada adquisición o relevo
        INCREMENTA el token de fencing, que es monótono y no baja al soltar.

        El recurso se NORMALIZA en las cuatro puertas (`acquire`/`renew`/
        `release`/`check_fence`). Normalizar sólo aquí sería peor que no hacerlo:
        renovar por el literal diría «no tiene lease».
        """
        self._guard_mutable()
        literal, resource = str(resource), self._recurso(resource)
        if self.authenticate(token) is None:      # barato, NO decide
            raise AuthError("se requiere sesión de runtime válida")
        self._tras_precheck()
        with self._tx() as con:
            view = _authenticate_locked(con, token, self._clock())
            if view is None:
                raise AuthError(
                    "sesión invalidada entre la comprobación y la escritura")
            # ⏱️ EL RELOJ SE LEE AQUÍ, DENTRO DE LA TRANSACCIÓN Y TRAS EL LOCK.
            # Estaba antes de `_tras_precheck()` y del `BEGIN IMMEDIATE`, y entre
            # esa lectura y el `UPDATE` cabía el vencimiento ENTERO. Medido con
            # el seam (`ttl=100`, el hook avanza `+101`):
            #   renew   -> ACEPTABA, fencing 1->1, y devolvía expires_at=1000100
            #              con now=1000101: prometía continuidad Y entregaba una
            #              valla caducada al nacer.
            #   acquire -> el gemelo: veía el lease VIVO con el reloj viejo y
            #              NEGABA un relevo legítimo.
            # Es la misma clase que este fichero ya persigue en todas partes:
            # ninguna decisión se apoya en una segunda lectura de algo mutable —
            # aquí lo mutable era el TIEMPO, que es el que nadie vigila.
            now = self._clock()
            row = con.execute("SELECT * FROM leases WHERE lane=? AND resource=?",
                              (view.lane, resource)).fetchone()
            if row is not None and row["released_at"] is None and row["expires_at"] > now:
                if row["runtime_instance"] == view.runtime_instance:
                    con.execute("UPDATE leases SET expires_at=? WHERE lane=? AND"
                                " resource=?", (now + ttl_s, view.lane, resource))
                    return Lease(resource, view.lane, view.principal_id,
                                 view.runtime_instance, int(row["fencing_token"]),
                                 now + ttl_s)
                raise LeaseConflict(
                    f"`{resource}` tiene dueño vivo hasta {row['expires_at']}")
            token_n = (int(row["fencing_token"]) if row is not None else 0) + 1
            con.execute(
                "INSERT INTO leases(lane,resource,resource_literal,principal_id,"
                "runtime_instance,fencing_token,acquired_at,expires_at,released_at)"
                " VALUES(?,?,?,?,?,?,?,?,NULL)"
                " ON CONFLICT(lane,resource) DO UPDATE SET"
                " resource_literal=excluded.resource_literal,"
                " principal_id=excluded.principal_id,"
                " runtime_instance=excluded.runtime_instance,"
                " fencing_token=excluded.fencing_token,"
                " acquired_at=excluded.acquired_at, expires_at=excluded.expires_at,"
                " released_at=NULL",
                (view.lane, resource, literal, view.principal_id,
                 view.runtime_instance, token_n, now, now + ttl_s))
        return Lease(resource, view.lane, view.principal_id, view.runtime_instance,
                     token_n, now + ttl_s)

    @_audita
    def renew_lease(self, token: str, resource: str, *, ttl_s: int = 300) -> Lease:
        """Renovar CONSERVA el token, o FALLA. Nunca releva.

        ⚠️ ESTO ERA `return self.acquire_lease(...)`, y la delegación es el
        defecto: con el lease VENCIDO y nadie en medio, `acquire` te lo devolvía
        con el token `N+1` y el llamante creía haber conservado el suyo. Toda
        mutación en vuelo con el token viejo empieza a dar `409` y el operador
        diagnostica otra cosa. La palabra prometía continuidad y el efecto era
        un relevo (medido por @contratosbik, `A6`).

        Las TRES caras que ahora fallan, y ninguna toca el fencing:
          · vencido  ⇒ se te fue; vuelve a adquirir y asume que estrenas token.
          · ajeno    ⇒ no es tuyo; renovarlo sería robarlo sin relevo.
          · liberado ⇒ ya lo soltaste; «renovar» lo resucitaría en silencio.

        `acquire_lease` SIGUE relevando: la cura es de esta puerta, no de
        aquélla. Si `acquire` dejara de relevar, un dueño caído bloquearía el
        recurso para siempre — eso sería cambiar un defecto por otro peor.
        """
        self._guard_mutable()
        resource = self._recurso(resource)
        if self.authenticate(token) is None:      # barato, NO decide
            raise AuthError("se requiere sesión de runtime válida")
        self._tras_precheck()
        with self._tx() as con:
            view = _authenticate_locked(con, token, self._clock())
            if view is None:
                raise AuthError(
                    "sesión invalidada entre la comprobación y la escritura")
            # ⏱️ EL RELOJ SE LEE AQUÍ, DENTRO DE LA TRANSACCIÓN Y TRAS EL LOCK.
            # Estaba antes de `_tras_precheck()` y del `BEGIN IMMEDIATE`, y entre
            # esa lectura y el `UPDATE` cabía el vencimiento ENTERO. Medido con
            # el seam (`ttl=100`, el hook avanza `+101`):
            #   renew   -> ACEPTABA, fencing 1->1, y devolvía expires_at=1000100
            #              con now=1000101: prometía continuidad Y entregaba una
            #              valla caducada al nacer.
            #   acquire -> el gemelo: veía el lease VIVO con el reloj viejo y
            #              NEGABA un relevo legítimo.
            # Es la misma clase que este fichero ya persigue en todas partes:
            # ninguna decisión se apoya en una segunda lectura de algo mutable —
            # aquí lo mutable era el TIEMPO, que es el que nadie vigila.
            now = self._clock()
            row = con.execute("SELECT * FROM leases WHERE lane=? AND resource=?",
                              (view.lane, resource)).fetchone()
            if row is None:
                raise LeaseConflict(
                    f"`{resource}` no tiene lease en este carril: no hay nada que "
                    f"renovar. Adquiérelo con `acquire_lease`")
            if row["released_at"] is not None:
                raise LeaseConflict(
                    f"`{resource}` está liberado: renovarlo lo resucitaría sin "
                    f"que nadie lo haya adquirido. Vuelve a adquirirlo")
            if row["runtime_instance"] != view.runtime_instance:
                # Sin nombrar al dueño: quién tiene el recurso es información del
                # carril, y el mensaje de un rechazo no es sitio para repartirla.
                raise LeaseConflict(
                    f"`{resource}` no es de esta sesión de runtime: renovar lo "
                    f"ajeno sería relevar sin token nuevo")
            if row["expires_at"] <= now:
                raise LeaseConflict(
                    f"tu lease de `{resource}` VENCIÓ en {row['expires_at']}: no se "
                    f"renueva lo que ya no tienes. Adquiérelo de nuevo y asume que "
                    f"estrenas `fencing_token` — puede haberte relevado alguien")
            con.execute("UPDATE leases SET expires_at=? WHERE lane=? AND resource=?",
                        (now + ttl_s, view.lane, resource))
            return Lease(resource, view.lane, view.principal_id,
                         view.runtime_instance, int(row["fencing_token"]),
                         now + ttl_s)

    @_audita
    def release_lease(self, token: str, resource: str) -> None:
        """Soltar un lease AJENO FALLA; no es un no-op.

        Un `UPDATE` que no casa ninguna fila devuelve éxito, así que soltar lo de
        otro «funcionaba» y el que llamaba se iba convencido de haber liberado el
        recurso. Un no-op que se lee como éxito es peor que un error: nadie va a
        mirar dos veces algo que le dijo que sí.
        """
        self._guard_mutable()
        resource = self._recurso(resource)
        if self.authenticate(token) is None:      # barato, NO decide
            raise AuthError("se requiere sesión de runtime válida")
        self._tras_precheck()
        with self._tx() as con:
            view = _authenticate_locked(con, token, self._clock())
            if view is None:
                raise AuthError(
                    "sesión invalidada entre la comprobación y la escritura")
            row = con.execute("SELECT * FROM leases WHERE lane=? AND resource=?",
                              (view.lane, resource)).fetchone()
            if row is None or row["released_at"] is not None:
                raise LeaseConflict(
                    f"`{resource}` no tiene lease activo en el carril `{view.lane}`")
            if row["runtime_instance"] != view.runtime_instance:
                raise LeaseConflict(
                    f"`{resource}` es de otro runtime: soltarlo no es tuyo")
            con.execute("UPDATE leases SET released_at=? WHERE lane=? AND resource=?",
                        (self._clock(), view.lane, resource))

    def _fence_locked(self, con: sqlite3.Connection, view: SessionView,
                      resource: str, fencing_token: int) -> None:
        """Validación de fencing SOBRE UNA CONEXIÓN YA EN TRANSACCIÓN.

        Ésta es la forma que de verdad protege una mutación DEL JOURNAL: se
        comprueba y se muta bajo el mismo `BEGIN IMMEDIATE`, así que entre la
        comprobación y el efecto no cabe un relevo. `check_fence()` público es la
        variante suelta y tiene el límite escrito en su docstring.
        """
        # La normalización va AQUÍ y no en los llamadores: es el punto ÚNICO por
        # el que pasan `check_fence()` y el `fenced_resource` de `accept_event`.
        # Normalizar en cada llamador es cómo se acaba con uno que se olvidó.
        resource = self._recurso(resource)
        row = con.execute("SELECT * FROM leases WHERE lane=? AND resource=?",
                          (view.lane, resource)).fetchone()
        if row is None:
            raise FencingConflict(
                f"`{resource}` no tiene lease en el carril `{view.lane}`")
        if row["released_at"] is not None:
            raise FencingConflict(f"`{resource}` está liberado: no autoriza nada")
        if row["expires_at"] <= self._clock():
            raise FencingConflict(f"`{resource}` tiene el lease VENCIDO")
        if row["runtime_instance"] != view.runtime_instance:
            raise FencingConflict(
                f"`{resource}` lo tiene otro runtime: tu sesión no es el dueño")
        if int(fencing_token) != int(row["fencing_token"]):
            raise FencingConflict(
                f"token {fencing_token} != vigente {row['fencing_token']}")

    @_audita
    def check_fence(self, token: str, resource: str, fencing_token: int) -> None:
        """Puerta de una mutación autoritativa del recurso.

        ⚠️ LÍMITE, y va aquí porque quien llama tiene que saberlo: ESTA LLAMADA
        SUELTA NO HACE ATÓMICA UNA MUTACIÓN EXTERNA. Devuelve una respuesta sobre
        el instante en que se preguntó; entre ese instante y el efecto cabe un
        vencimiento y un relevo entero. Comprobar aquí y escribir después es un
        TOCTOU con cara de rigor.

        · Para mutaciones DEL JOURNAL: se valida DENTRO de la misma transacción
          que muta — usa `_fence_locked(con, view, ...)`, o pásale la conexión.
        · Para efectos EXTERNOS (markdown, ficheros, API de terceros): el fencing
          hay que verificarlo EN EL RECURSO DESTINO, no aquí. Un número validado
          en esta base no puede impedir que otro proceso ya haya escrito allí. El
          patrón del outbox es el ejemplo: la ficha de arriendo se comprueba en la
          MISMA transacción que registra el efecto.

        Exige sesión válida y acepta SÓLO la IGUALDAD con el token vigente de un
        lease ACTIVO del mismo carril y del mismo runtime. Cada condición cierra
        una vía distinta:

        · sesión         — sin ella, cualquiera que sepa el número pasa la valla.
        · igualdad       — `>=` dejaba pasar un token MAYOR, que nadie emitió: un
                           número inventado hacia arriba era la llave maestra.
        · lease activo   — vencido o liberado ya no autoriza; si no, el fencing
                           sólo ordenaría relevos y no pararía al pausado.
        · mismo carril   — la valla de un carril no vale en otro.
        · mismo runtime  — otra sesión del MISMO principal es otro dueño, y el
                           ADR lo dice explícitamente.
        """
        with self._lectura() as (con, _):
            view = _authenticate_locked(con, token, self._clock())
            if view is None:
                raise AuthError("se requiere sesión de runtime válida")
            self._fence_locked(con, view, resource, fencing_token)

    # ── comandos ─────────────────────────────────────────────────────────────
    @_audita
    def submit_command(self, token: str, *, workstream_id: str, revision: int,
                       payload: Mapping[str, Any],
                       causes: Sequence[str] = (),
                       external_causes: Sequence[Mapping[str, str]] = ()
                       ) -> tuple[str, str]:
        """Registra un comando. Devuelve `(command_id, state)`.

        Sólo la revisión MÁS ALTA aceptada y no cancelada puede ejecutarse. Un
        comando viejo que llega tarde se REGISTRA y se recibe como `superseded`:
        se guarda, se audita y no se ejecuta jamás.

        El `payload` NO puede traer atribución ni campos del sobre. `accept_event`
        ya los rechazaba y aquí se GUARDABAN y se ignoraban en silencio: el
        cliente los veía persistidos y podía leerlos de vuelta creyendo que
        significaban algo. Ignorar enseña a mandarlo, y el día que alguien lea el
        campo equivocado el agujero ya está escrito en todos los clientes
        (medido por @contratosbik, `A7`: `supersedes` lo DERIVA el servidor y un
        cliente que lo mandaba lo veía guardado).
        """
        self._guard_mutable()
        if self.authenticate(token) is None:      # barato, NO decide
            raise AuthError("se requiere sesión de runtime válida")
        # La MISMA inversión que `accept_event`, y se cura a la vez: curar una
        # puerta y dejar la gemela es dejar el oráculo con otro nombre. Medido:
        # `payload` limpio -> AuthError · `payload` con `credential` ->
        # AttributionRejected, con un token que no existe.
        # 🔻 ORDEN, y ahora IGUAL en las dos puertas. Estaba invertido:
        # `accept_event` congelaba antes de atribuir y `submit_command` atribuia
        # antes de congelar, asi que el MISMO documento con atribucion Y exceso
        # de profundidad daba `ATTRIBUTION_REJECTED` por una puerta y
        # `RESOURCE_LIMIT_EXCEEDED` por la otra. La atribucion mira el SNAPSHOT.
        # 🔻 EL AGREGADO CANONICO de ESTA mutacion: lo comparten TODOS los
        # campos controlados por cliente de esta puerta. Se SUMA aqui y se
        # DECIDE al final — precedencia: forma, limites locales, agregado.
        # 🔻 UN CONTADOR DE NODOS POR OPERACION, DESDE LA ENTRADA — misma
        # regla y mismo motivo que en `accept_event`.
        presupuesto = [0]
        agregado = _AgregadoPeticion()
        payload = _congelar(payload, "payload", tope_bytes=PAYLOAD_MAX_BYTES,
                            nodos=presupuesto, agregado=agregado,
                            obligatorio=True)
        causes = _congelar_secuencia(causes, "causes",
                                     tope_bytes=ELEMENTO_MAX_BYTES,
                                     nodos=presupuesto, agregado=agregado)
        external_causes = _congelar_secuencia(external_causes, "external_causes",
                                              tope_bytes=ELEMENTO_MAX_BYTES,
                                              nodos=presupuesto, agregado=agregado)
        _exige_agregado(agregado)     # ③ AL FINAL, tras forma y limites locales
        self._sin_atribucion(payload, "payload")
        at = _now_iso(self._clock())
        cid = _new_id("cmd")
        self._tras_precheck()
        with self._tx() as con:
            view = _authenticate_locked(con, token, self._clock())
            if view is None:
                raise AuthError(
                    "sesión invalidada entre la comprobación y la escritura")
            top = con.execute(
                "SELECT command_id, revision FROM commands"
                " WHERE lane=? AND workstream_id=?"
                "   AND state NOT IN ('cancelled','superseded')"
                " ORDER BY revision DESC LIMIT 1",
                (view.lane, workstream_id)).fetchone()
            # Una causa nativa puede ser un COMANDO o un EVENTO, y en los dos
            # casos del MISMO carril. Se resuelve el tipo aquí para poder
            # escribirla en su tabla con FK de verdad.
            tipadas: list[tuple[str, str]] = []
            for c in causes:
                cmd = con.execute("SELECT lane FROM commands WHERE command_id=?",
                                  (c,)).fetchone()
                ev = con.execute("SELECT lane FROM events WHERE event_id=?",
                                 (c,)).fetchone()
                if cmd is None and ev is None:
                    raise CauseRejected(
                        f"`{c}` no es un comando ni un evento nativo: el material "
                        f"bridge se cita por `external_causes`")
                lane_causa = cmd["lane"] if cmd is not None else ev["lane"]
                if lane_causa != view.lane:
                    raise CauseRejected(
                        f"`{c}` es de otro carril: la causalidad nativa no cruza carriles")
                tipadas.append(("command" if cmd is not None else "event", c))
            # Se comprueba ANTES de insertar y dentro del `BEGIN IMMEDIATE`, así
            # que el veredicto es determinista: no depende de qué escritor llegue
            # primero al UNIQUE.
            # `command_id` Y filtrado por `view.lane`: el id que viaja en el
            # rechazo es SIEMPRE del propio carril. Buscar por
            # `(workstream, revision)` sin el carril devolvería el del vecino y
            # convertiría el mensaje de error en una fuga de aislamiento — la
            # puerta que nadie audita porque «sólo es un texto de error».
            ya = con.execute("SELECT command_id FROM commands WHERE lane=?"
                             "  AND workstream_id=? AND revision=?",
                             (view.lane, workstream_id, int(revision))).fetchone()
            if ya is not None:
                e = CommandRevisionConflict(
                    f"la revisión {revision} de `{workstream_id}` ya existe en "
                    f"este carril")
                e.command_id = ya["command_id"]
                raise e
            if top is not None and int(top["revision"]) >= int(revision):
                state, supersedes = "superseded", None
            else:
                state, supersedes = "accepted", (top["command_id"] if top else None)
            con.execute(
                "INSERT INTO commands(command_id,workstream_id,revision,lane,"
                "principal_id,role,runtime_instance,attribution_status,payload,"
                "state,supersedes,created_at)"
                " VALUES(?,?,?,?,?,?,?,'verified',?,?,?,?)",
                (cid, workstream_id, int(revision), view.lane, view.principal_id,
                 view.role, view.runtime_instance, _canonical(dict(payload)),
                 state, supersedes, at))
            for tipo, c in tipadas:
                if tipo == "command":
                    con.execute("INSERT OR IGNORE INTO command_causes(command_id,"
                                "cause_command_id) VALUES(?,?)", (cid, c))
                else:
                    con.execute("INSERT OR IGNORE INTO command_event_causes(command_id,"
                                "cause_event_id) VALUES(?,?)", (cid, c))
            for c in external_causes:
                if not isinstance(c, Mapping) or not c.get("ledger") \
                        or not c.get("entry_eid"):
                    raise CauseRejected(
                        "una causa externa necesita `ledger` y `entry_eid`")
                # Material bridge citado por un COMANDO: tipado, sin FK hacia el
                # journal y con `authority='false'` en la propia fila. Citarlo no
                # lo asciende: es exactamente lo que el ADR pide poder hacer sin
                # que nadie confunda una cita con una autoridad.
                con.execute(
                    "INSERT OR IGNORE INTO external_causes(child_kind,child_id,"
                    "ledger,entry_eid,noted_at) VALUES('command',?,?,?,?)",
                    (cid, c["ledger"], c["entry_eid"], at))
            self._open_receipt(con, "command", cid, view.principal_id, view.lane,
                               state, at)
            if supersedes is not None:
                # Sólo se supersede lo que aún no arrancó ni terminó. Reescribir
                # un `succeeded` a `superseded` borraría que el trabajo SE HIZO.
                anterior = con.execute("SELECT state FROM commands WHERE command_id=?",
                                       (supersedes,)).fetchone()
                if anterior is None or anterior["state"] not in ("accepted", "received"):
                    supersedes = None
            if supersedes is not None:
                con.execute("UPDATE commands SET state='superseded' WHERE command_id=?",
                            (supersedes,))
                rc = con.execute("SELECT receipt_id FROM receipts WHERE"
                                 " subject_kind='command' AND subject_id=?",
                                 (supersedes,)).fetchone()
                if rc is not None:
                    seq = con.execute("SELECT MAX(seq) s FROM receipt_transitions"
                                      " WHERE receipt_id=?", (rc["receipt_id"],)
                                      ).fetchone()["s"] or 0
                    con.execute("INSERT INTO receipt_transitions(transition_id,receipt_id,"
                                "seq,state,at,detail) VALUES(?,?,?,?,?,?)",
                                (_new_id("trn"), rc["receipt_id"], seq + 1,
                                 "superseded", at, _canonical({"by": cid})))
                    con.execute("UPDATE receipts SET current_state='superseded',"
                                " updated_at=? WHERE receipt_id=?", (at, rc["receipt_id"]))
        return cid, state

    def may_execute(self, token: str, command_id: str) -> bool:
        """Sólo la revisión más alta viva de SU workstream EN SU CARRIL.

        El carril va en la consulta y no sólo en la tabla: sin él, la revisión 3
        del `ws` de otro carril bloquearía la 2 del tuyo.

        ⚠️ AHORA EXIGE SESIÓN, y el motivo no es el veredicto —que ya no cruzaba
        carriles— sino la EXISTENCIA: sin sujeto, un `command_id` de otro carril
        devolvía `False` igual que uno inventado, y eso es distinguible por otras
        vías (`@contratosbik`, `RULING` ④). Con sujeto, los dos son el mismo
        `SubjectNotFound` y no hay oráculo de enumeración.
        """
        with self._lectura() as (con, _):
            view = _authenticate_locked(con, token, self._clock())
            if view is None:
                raise AuthError("se requiere sesión de runtime válida")
            row = con.execute("SELECT lane, workstream_id, revision, state FROM commands"
                              " WHERE command_id=?", (command_id,)).fetchone()
            if row is None or row["lane"] != view.lane:
                # MISMO mensaje para «no existe» y «no es de tu carril».
                raise SubjectNotFound(f"no existe el comando {command_id}")
            # `executing` tampoco: ya arrancó; se pregunta si puede ARRANCAR.
            if row["state"] not in ("accepted", "received"):
                return False
            top = con.execute(
                "SELECT MAX(revision) r FROM commands WHERE lane=? AND workstream_id=?"
                " AND state NOT IN ('cancelled','superseded')",
                (row["lane"], row["workstream_id"])).fetchone()["r"]
            return top is not None and int(row["revision"]) == int(top)

    # ── auditoría centralizada de rechazos ───────────────────────────────────
    _POR_MOTIVO = {
        AuthError: "SESSION_INVALID",
        IdempotencyConflict: "IDEMPOTENCY_CONFLICT",
        CauseRejected: "CAUSE_REJECTED",
        FencingConflict: "FENCING_CONFLICT",
        LeaseConflict: "LEASE_CONFLICT",
        LedgerNotAllowed: "LEDGER_NOT_ALLOWED",
        FencedPairInvalid: "FENCED_PAIR_INVALID",
        CommandTransitionInvalid: "COMMAND_TRANSITION_INVALID",
        DeliveryConflict: "DELIVERY_CONFLICT",
        CommandRevisionConflict: "COMMAND_REVISION_CONFLICT",
        RecipientUnresolved: "RECIPIENT_UNRESOLVED",
        GrammarRejected: "GRAMMAR_REJECTED",
        GrammarUnavailable: "GRAMMAR_UNAVAILABLE",
        AttributionRejected: "ATTRIBUTION_REJECTED",
        KeyCharsetRejected: "KEY_CHARSET_REJECTED",
        ResourceLimitExceeded: "RESOURCE_LIMIT_EXCEEDED",
        SubjectNotFound: "SUBJECT_NOT_FOUND",
        ReceiptStateInvalid: "RECEIPT_STATE_INVALID",
        OperationInvalid: "OPERATION_INVALID",
        PolicyDenied: "POLICY_DENIED",
        AdmissionClosed: "ADMISSION_CLOSED",
        AdmissionConflict: "ADMISSION_CONFLICT",
        SchemaIndeterminate: "SCHEMA_INDETERMINATE",
        ReplayUnverifiable: "REPLAY_UNVERIFIABLE",
        ObservationSequenceConflict: "OBSERVATION_SEQUENCE_CONFLICT",
        OrganizationConflict: "ORGANIZATION_CONFLICT",
        RecoveryConflict: "RECOVERY_CONFLICT",
    }

    def _auditar_rechazo(self, token: str, exc: Exception) -> str | None:
        """Registra el rechazo DESPUÉS del rollback, nunca dentro.

        Dentro de la transacción que se va a deshacer, el registro se deshace
        con ella: el rechazo desaparecería justo en el caso que existe para
        dejar rastro. Y como es otra transacción, un fallo al auditar no puede
        convertir un rechazo limpio en un 500 — se traga, porque la respuesta al
        llamante ya está decidida.
        """
        motivo = None
        for tipo, code in self._POR_MOTIVO.items():
            if isinstance(exc, tipo):
                motivo = code
                break
        if motivo is None:
            # NO se vuelve sin auditar: el rechazo que nadie clasificó todavía es
            # exactamente el que interesa ver en el registro.
            motivo = "OPERATION_REJECTED"
        try:
            ident = self._identidad_para_auditoria(token)
            if ident is None:
                # Token que no resuelve a ninguna sesión: no puede crear filas de
                # coordinación. Cae al contador acotado de desconocidos.
                self.record_unknown_credential(token)
                return None
            return self._record_denial(ident["principal_id"], motivo,
                                       lane=ident["lane"],
                                       runtime_instance=ident["runtime_instance"])
        except (JournalError, sqlite3.Error):
            # SÓLO lo que puede fallar por el estado de la base. Un `except
            # Exception` se tragaba también los errores de programación del
            # propio auditor: el registro fallaba en silencio justo cuando había
            # algo que registrar.
            self._audit_suspended.set()
            return None

    def _identidad_para_auditoria(self, token: str) -> dict | None:
        """Atribución de un token AUNQUE ya no sea válido.

        Un token caducado o revocado sigue identificando a quien llamó, y ése es
        justo el caso que hay que poder auditar. `authenticate()` no sirve aquí
        porque devuelve None precisamente entonces.

        Si el journal no está en un estado LISTO no hay dónde mirar, y eso es un
        `None` —«sin sujeto»—, NO una excepción: auditar es lo último que puede
        convertirse en superficie de error. `JournalNotInitialized` es un fallo
        POR EL ESTADO DE LA BASE, que es exactamente la categoría que el `except`
        de arriba dice capturar; un `except Exception` seguiría prohibido, porque
        se tragaría los errores de programación del propio auditor.
        """
        try:
            with self._lectura() as (con, _):
                row = con.execute(
                    "SELECT s.runtime_instance, s.principal_id, p.principal, s.role, s.lane,"
                    " s.expires_at, s.revoked_at, s.generation,"
                    " COALESCE((SELECT b.principal_source FROM credential_bindings b"
                    "            WHERE b.principal_id=s.principal_id"
                    "            ORDER BY b.bound_at DESC LIMIT 1), '') principal_source,"
                    " COALESCE((SELECT CAST(v AS INTEGER) FROM meta WHERE k='generation'), 1)"
                    "   current_generation"
                    " FROM runtime_sessions s JOIN principals p USING(principal_id)"
                    " WHERE s.token_hash=?", (_sha256(token),)).fetchone()
                return dict(row) if row else None
        except (JournalNotInitialized, IdentityChanged):
            return None

    _CMD_MAQUINA = {
        "accepted":   {"received", "cancelled"},
        "received":   {"executing", "cancelled"},
        "executing":  {"succeeded", "failed", "cancelled"},
        "succeeded":  set(),
        "failed":     set(),
        "cancelled":  set(),
        "superseded": set(),
    }

    @_audita
    def advance_command(self, token: str, command_id: str, new_state: str, *,
                        detail: Mapping[str, Any] | None = None) -> str:
        """Avanza un comando: `commands.state` Y su recibo, en UNA transacción.

        Las dos escrituras van juntas porque son la misma verdad contada dos
        veces: si se pudieran hacer por separado —como permitía el
        `append_transition` público— el recibo diría `succeeded` mientras la
        tabla que gobierna la ejecución sigue en `accepted`, y el que audita
        creería el recibo.

        Máquina: accepted → received → executing → succeeded|failed, con
        `cancelled` alcanzable desde cualquier no terminal. `superseded` no se
        alcanza por aquí: lo pone la llegada de una revisión mayor.
        """
        self._guard_mutable()
        at = _now_iso(self._clock())
        with self._tx() as con:
            view = _authenticate_locked(con, token, self._clock())
            if view is None:
                raise AuthError(
                    "sesión invalidada entre la comprobación y la escritura")
            self._exigir_capacidad_locked(con, view, CAP_COMMAND_WORKER,
                                          "advance_command")
            # 🔻 A8 · AQUI, Y NO ARRIBA. Estaba ANTES de autenticar: un token
            # invalido con un `detail` de `5 KB` recibia `413` y **nunca llegaba
            # al `401`** — un oraculo de tamaño para quien no ha demostrado ser
            # nadie. Es la inversion exacta que `accept_event` ya cerro y que su
            # propio comentario predica.
            # 🔻 CORRECTIVO · `tope_bytes=None`: **SIN eje de bytes**. `detail` no
            # esta en la politica de rechazo — un `detail` grande AUTENTICADO
            # prospera TRUNCADO, no da `413`. Lo que si se conserva es lo que
            # ningun tope vuelve valido y reventaria el canonico con un
            # `ValueError` crudo: tipo no-JSON, clave no-texto, `NaN/Inf` y
            # CICLO — eso sigue siendo `422`, no `413`. Y el congelado sigue
            # dando el snapshot privado, que es lo que cierra el TOCTOU.
            #
            # ⚖️ POR QUE `depth`/`nodes` SIGUEN PUESTOS, Y POR QUE NO CONTRADICE
            # «no se rechaza por el TAMAÑO del diagnostico». Son dos cosas
            # distintas y la diferencia es la unidad:
            #   · `bytes` mide CUANTO OCUPA la explicacion. Rechazar por ahi es
            #     exactamente lo prohibido: la operacion moriria por el tamaño de
            #     su comentario. **RETIRADO.**
            #   · `depth`/`nodes` NO miden tamaño: acotan el TRABAJO que hay que
            #     hacer para poder copiar y canonicalizar. Un `detail` de `10^6`
            #     nodos no se rechaza «por gordo» — se rechaza porque recorrerlo
            #     entero es trabajo NO ACOTADO en la puerta, y sin recorrerlo no
            #     hay ni snapshot (TOCTOU abierto) ni deteccion de CICLO
            #     (`ValueError` crudo, `500` sin recibo).
            # 🔑 Y la prueba de que la frontera esta bien puesta es la del propio
            # `@cto`: «¿existe un valor de la constante que lo vuelva aceptable
            # SIN tocar el cuerpo?». Para `bytes` la respuesta era SI y por eso
            # era politica —y por eso sale—. Para `depth`/`nodes` tambien lo es,
            # asi que siguen siendo `413`: lo que cambia no es su clase, es que
            # **el eje del TAMAÑO se retira y los del TRABAJO se quedan**.
            # ⛔ Esto es lectura MIA de la orden, no de la orden misma: si el
            # AMEND quiere `detail` sin NINGUN eje, la cura es acotar el trabajo
            # de otra forma (limite de recursion propio + deteccion de ciclo sin
            # recorrido completo), no quitar los guardas y ya.
            # 🔻 CURA DEL HALLAZGO (encargo del operador: «cierra el hallazgo
            # dentro del alcance Core, con presupuesto global»). Era
            # `tope_bytes=None`, y ese `None` NO era «sin politica»: apagaba el
            # contador ENTERO. `suma()` de `_congela_valor` abre con
            # `if tope_bytes is None: return`, asi que `detail` era el UNICO
            # campo de estructura libre del nucleo SIN NINGUN eje de tamaño.
            # ⇒ `{"x": "A"*32_000_000}` son `2` nodos y profundidad `1`: pasaba
            # la puerta entera y `_payload_de_recibo` canonicalizaba los `32 MB`
            # para persistir `347 B`. El comentario de abajo decia que
            # `depth`/`nodes` «acotan el TRABAJO»: es FALSO para el eje
            # bytes-por-valor — `2` nodos no acotan `32 MB`.
            #
            # ⚖️ EL TOPE ES EL GLOBAL, Y ESA ES LA DIFERENCIA QUE IMPORTA:
            # `CANONICAL_REQUEST_MAX_BYTES` es el techo de CUALQUIER peticion
            # del nucleo, no una politica sobre el diagnostico. Lo que el
            # operador prohibio —y `MD1-detail-vuelve-a-rechazar` vigila— es
            # rechazar `detail` con `METADATO_MAX_BYTES` (`4 KiB`): ahi la
            # operacion moria por el TAMAÑO DE SU COMENTARIO. Con el techo
            # global un `detail` de `5 KiB` sigue PROSPERANDO y truncando a
            # `320`; lo unico que ya no entra es lo que ninguna peticion puede
            # traer. **Trunca igual; deja de trabajar gratis.**
            #
            # ⊕ MEDIDO, y el corte es INCREMENTAL (cota inferior O(1), no
            # materializa): `32 MB` pasaba en `0,056 s` y ahora da `413` en
            # `0,0001 s` — `560x`. `1 MB` sigue pasando. El caso del operador
            # (`5 KiB`) sigue truncado, no rechazado.
            # Falsador: `SUITE_D`, `test_el_detail_tiene_el_techo_GLOBAL_y_el_corte_es_INCREMENTAL`.
            detail = _congelar(detail, "detail",
                               tope_bytes=CANONICAL_REQUEST_MAX_BYTES)
            row = con.execute("SELECT lane, state FROM commands WHERE command_id=?",
                              (command_id,)).fetchone()
            if row is None:
                raise CommandTransitionInvalid(f"`{command_id}` no existe")
            if row["lane"] != view.lane:
                raise CommandTransitionInvalid(
                    "el comando es de otro carril")
            recovery = con.execute(
                "SELECT 1 FROM runtime_recoveries WHERE lane=? AND command_id=?",
                (view.lane, command_id)).fetchone()
            if recovery is not None:
                raise CommandTransitionInvalid(
                    "una recovery sólo avanza por advance_runtime_recovery y "
                    "terminaliza mediante outcome tipado")
            permitidos = self._CMD_MAQUINA.get(row["state"], set())
            if new_state not in permitidos:
                raise CommandTransitionInvalid(
                    f"`{row['state']}` -> `{new_state}` no está en la máquina"
                    f" (permitidos: {sorted(permitidos) or 'ninguno: es terminal'})")
            if new_state == "executing":
                # EL GATE DE ARRANQUE VA AQUÍ, DENTRO DE ESTA TRANSACCIÓN.
                # `may_execute()` es sólo lectura: entre su «sí» y este UPDATE
                # cabe otra revisión y otro arranque, así que como autorización
                # es un TOCTOU. Lo único que autoriza es esto, bajo el mismo
                # `BEGIN IMMEDIATE` que escribe.
                fila = con.execute(
                    "SELECT workstream_id, revision FROM commands WHERE command_id=?",
                    (command_id,)).fetchone()
                otro = con.execute(
                    "SELECT command_id FROM commands WHERE lane=? AND"
                    " workstream_id=? AND state='executing' AND command_id<>?",
                    (view.lane, fila["workstream_id"], command_id)).fetchone()
                if otro is not None:
                    # Y NO se le toca el estado al que ya está ejecutando: una
                    # revisión nueva no puede desalojar trabajo en curso, sólo
                    # esperar a que termine.
                    raise CommandTransitionInvalid(
                        f"`{otro['command_id']}` ya está ejecutando este "
                        f"workstream: no puede haber dos")
                tope = con.execute(
                    "SELECT MAX(revision) r FROM commands WHERE lane=? AND"
                    " workstream_id=? AND state IN ('accepted','received')",
                    (view.lane, fila["workstream_id"])).fetchone()["r"]
                # ⚠️ DEFENSA EN PROFUNDIDAD, NO FALSADA — medido, no supuesto:
                # quitar esta rama deja la suite entera en verde (96/96). Hoy es
                # inalcanzable porque la supersesión ya marca `superseded` a toda
                # revisión menor viva, y `superseded` no tiene transiciones
                # legales. Se queda por si la supersesión cambia, y se dice aquí
                # para que nadie la cuente como guarda probada.
                if tope is not None and int(fila["revision"]) != int(tope):
                    raise CommandTransitionInvalid(
                        f"la revisión {fila['revision']} no es la elegible más "
                        f"alta ({tope})")
            con.execute("UPDATE commands SET state=? WHERE command_id=?",
                        (new_state, command_id))
            rc = con.execute("SELECT receipt_id FROM receipts WHERE"
                             " subject_kind='command' AND subject_id=?",
                             (command_id,)).fetchone()
            if rc is None:
                raise CommandTransitionInvalid(f"`{command_id}` sin recibo")
            seq = con.execute("SELECT MAX(seq) s FROM receipt_transitions"
                              " WHERE receipt_id=?", (rc["receipt_id"],)
                              ).fetchone()["s"] or 0
            tid = _new_id("trn")
            # El `detail` del cliente se guarda BAJO `payload`, nunca a la altura
            # del actor: si compartieran nivel, un `by_principal` del llamante
            # pisaría al derivado y la transición firmaría con el nombre que él
            # eligiera.
            con.execute("INSERT INTO receipt_transitions(transition_id,receipt_id,"
                        "seq,state,at,detail) VALUES(?,?,?,?,?,?)",
                        (tid, rc["receipt_id"], seq + 1, new_state, at,
                         _canonical({"by_principal": view.principal_id,
                                     "by_role": view.role,
                                     "by_runtime": view.runtime_instance,
                                     **_payload_de_recibo(detail)})))
            con.execute("UPDATE receipts SET current_state=?, updated_at=?"
                        " WHERE receipt_id=?", (new_state, at, rc["receipt_id"]))
        return tid

    # ── denegaciones acotadas ────────────────────────────────────────────────
    def _bucket(self, ts: float) -> str:
        return str(int(ts // self._denial_bucket_s))

    def _record_denial(self, principal_id: str, reason: str, *,
                       lane: str | None = None,
                       runtime_instance: str | None = None) -> str:
        """Rechazo de una identidad CONOCIDA: siempre auditable, nunca ilimitado.

        Bajo la cuota, cada denegación tiene su recibo inmutable. Por encima, se
        cita UN recibo agregado por (principal, motivo, cubo) y se cuenta en
        sitio: una tormenta no puede forzar una fila durable por petición.
        """
        # 🧊 UNA lectura, y la MISMA que se comprueba, se imprime y se guarda.
        # `reason` viene del GATEWAY (`record_rejection` es su superficie), así
        # que es dato externo: una subclase de `str` cuyo `__format__` cambie
        # entre lecturas inyectaría en el mensaje aunque el `in` se hiciera con
        # el valor bueno. Con el congelado no queda ventana.
        motivo = str(reason)
        if motivo not in REASON_CODES:
            # `ValueError` SE MANTIENE —no cambia la clase—, pero su detalle pasa
            # por el MISMO saneador y el MISMO tope que el resto: `ValueError` no
            # hereda de `JournalError`, así que la garantía de la base no lo toca
            # y hay que traérsela a mano. Es el límite que llevaba dos freezes
            # DECLARADO y aquí queda cerrado.
            raise ValueError(_saneado(
                f"motivo {_saneado(motivo, DETALLE_MAX // 4)} fuera del "
                f"vocabulario cerrado: un motivo libre lleva el dato que lo "
                f"provoco y hace la agregacion inutil"))
        reason = motivo
        self._guard_mutable()
        now = self._clock()
        at, bucket = _now_iso(now), self._bucket(now)
        lane = lane or ""
        runtime_instance = runtime_instance or ""
        with self._tx() as con:
            # LA CUOTA VA POR (principal, carril, motivo, cubo) Y NUNCA POR
            # RUNTIME. Abrir sesiones es gratis: con el runtime en la clave, 200
            # sesiones son 200 cuotas y la cota deja de acotar — evasión, no
            # aislamiento. La fila individual SÍ conserva el runtime, porque para
            # ATRIBUIR sí hace falta; lo que no puede es gobernar el límite.
            n = con.execute("SELECT COUNT(*) c FROM denials WHERE principal_id=?"
                            " AND lane=? AND reason=? AND bucket=?",
                            (principal_id, lane, reason, bucket)).fetchone()["c"]
            if n < self._denial_quota:
                did = _new_id("dnl")
                rid = self._open_receipt(con, "denial", did, principal_id, lane,
                                         "denied", at)
                con.execute("INSERT INTO denials(denial_id,principal_id,lane,"
                            "runtime_instance,reason,bucket,receipt_id,at)"
                            " VALUES(?,?,?,?,?,?,?,?)",
                            (did, principal_id, lane, runtime_instance, reason,
                             bucket, rid, at))
                return rid
            agg = con.execute("SELECT receipt_id FROM denial_aggregates WHERE"
                              " principal_id=? AND lane=? AND reason=? AND bucket=?",
                              (principal_id, lane, reason, bucket)).fetchone()
            if agg is None:
                subject = f"{principal_id}:{lane}:{reason}:{bucket}"
                rid = self._open_receipt(con, "denial_aggregate", subject,
                                         principal_id, lane, "denied_aggregate", at)
                con.execute("INSERT INTO denial_aggregates(principal_id,lane,"
                            "reason,bucket,receipt_id,suppressed,first_at,last_at)"
                            " VALUES(?,?,?,?,?,1,?,?)",
                            (principal_id, lane, reason, bucket, rid, at, at))
                return rid
            con.execute("UPDATE denial_aggregates SET suppressed=suppressed+1,"
                        " last_at=? WHERE principal_id=? AND lane=? AND reason=?"
                        "   AND bucket=?",
                        (at, principal_id, lane, reason, bucket))
            return agg["receipt_id"]

    def record_unknown_credential(self, credential: str) -> None:
        """Credencial DESCONOCIDA: no crea ninguna fila de coordinación.

        Sólo incrementa un contador con COTA DURA de filas. Al llenarse, todo cae
        en una fila de desborde: tráfico no autenticado no puede hacer crecer la
        base durable sin límite (ADR-001 falsador 10).
        """
        try:
            self._guard_mutable()
        except (JournalReadOnly, SchemaTooNew, JournalNotInitialized):
            # Ni siquiera un contador: sobre una base de versión superior ésta era
            # la única escritura que quedaba abierta, y la disparaba cualquiera
            # SIN credencial.
            #
            # `JournalNotInitialized` entra en la lista con la máquina de estados
            # y NO es un caso nuevo: es el MISMO. Una base futura, corrupta o
            # simplemente no inicializada deja el estado en `NUEVO`, así que el
            # guard corta AHÍ y ya no llega a la comprobación de versión que antes
            # daba `SchemaTooNew`. La condición que importa —no escribir— no ha
            # cambiado; lo que cambió es cuál de los dos guardas la ve primero.
            # Dejarla fuera convertía el contador anónimo en una EXCEPCIÓN por la
            # ruta no autenticada: exactamente la superficie de error que este
            # método existe para no tener.
            return
        # HUELLA ROTATORIA (ADR-001: «rotating credential fingerprint»). El
        # cubo entra en el HMAC, así que la misma credencial da una huella
        # distinta en cada época: el contador sigue sirviendo para frenar una
        # ráfaga y deja de ser un identificador estable con el que seguir a nadie
        # a lo largo del tiempo. El secreto en claro no entra nunca.
        epoca = int(self._clock() // self._fingerprint_rotation_s)
        fp = hmac.new(self._pepper,
                      f"{epoca}:{credential}".encode("utf-8"),
                      hashlib.sha256).hexdigest()[:16]
        at = _now_iso(self._clock())
        try:
            with self._tx() as con:
                hay = con.execute("SELECT 1 FROM unknown_credentials WHERE fingerprint=?",
                                  (fp,)).fetchone()
                if hay is None:
                    n = con.execute("SELECT COUNT(*) c FROM unknown_credentials"
                                    ).fetchone()["c"]
                    if n >= self._unknown_rows_max:
                        fp = "overflow"
                con.execute(
                    "INSERT INTO unknown_credentials(fingerprint,count,first_at,last_at)"
                    " VALUES(?,1,?,?) ON CONFLICT(fingerprint) DO UPDATE SET"
                    " count=count+1, last_at=excluded.last_at", (fp, at, at))
        except JournalReadOnly:
            self._audit_suspended.set()
            return

    @_audita
    def record_rejection(self, token: str, reason: str) -> str | None:
        """Superficie para el GATEWAY: registrar un rechazo que ocurrió ARRIBA.

        Cerrada y acotada a propósito: el motivo tiene que estar en
        `REASON_CODES` —nada de texto libre, que es como la agregación deja de
        agregar—, la atribución se DERIVA del token y la cuota es la misma que
        la de los rechazos del propio journal. Existe para que una denegación de
        política del gateway (`POLICY_DENIED`) deje el mismo rastro que una de
        aquí y no un log suelto que nadie correlaciona.

        Devuelve el `receipt_id` del rechazo, o `None` si el token no resuelve.
        """
        motivo = str(reason)            # 🧊 idem: una lectura, la misma para todo
        if motivo not in REASON_CODES:
            raise ValueError(_saneado(
                f"motivo {_saneado(motivo, DETALLE_MAX // 4)} fuera del "
                f"vocabulario cerrado"))
        reason = motivo
        ident = self._identidad_para_auditoria(token)
        if ident is None:
            self.record_unknown_credential(token)
            return None
        return self._record_denial(ident["principal_id"], reason,
                                   lane=ident["lane"],
                                   runtime_instance=ident["runtime_instance"])

    # ── salud ────────────────────────────────────────────────────────────────
    def health(self) -> dict:  # noqa: D401
        """Salud SIN crear nada y sin mentir sobre el estado del ciclo."""
        with self._mutex:
            return self._health_locked()

    def _health_locked(self) -> dict:
        """Implementación con el estado del objeto congelado por ``_mutex``."""
        self._preflight()
        clase = self._veredicto[0]
        base = {"path": self.path, "estado_ciclo": self._estado,
                "durable_v_code": DURABLE_V,
                "audit_suspended": self._audit_suspended.is_set()}
        if clase == "nueva":
            return {**base, "initialized": False, "estado": "nueva",
                    "durable_v_stored": None, "writable": False,
                    "outbox_pending": 0, "pragmas": None,
                    "detalle": "la ruta no existe; nada creado para responder"}
        if self._estado not in (self.READY, self.READ_ONLY_READY):
            try:
                almacenada = self.stored_durable_v()
            except JournalError:
                almacenada = self._veredicto[1]
            return {**base, "initialized": False, "estado": clase,
                    "durable_v_stored": almacenada,
                    "schema_too_new": clase == "futura",
                    "writable": False, "outbox_pending": 0, "pragmas": None,
                    "detalle": self._veredicto[2] or
                               "clasificada y NO inicializada: leo de la copia"}
        stored = self.stored_durable_v()
        return {**base,
                "initialized": True,
                "estado": clase,
                "durable_v_stored": stored,
                "schema_too_new": stored is not None and stored > DURABLE_V,
                "writable": self._estado == self.READY,
                "outbox_pending": self._outbox_global(("pending",)) if stored else 0,
                "pragmas": self._assert_pragmas()}


def _translate_ro(e: sqlite3.OperationalError) -> Exception:
    """`attempt to write a readonly database` → error propio.

    La detección al conectar NO basta: una base ya en WAL contesta al
    `PRAGMA journal_mode=WAL` sin escribir, así que el volumen de sólo lectura
    puede no delatarse hasta la primera escritura de verdad. Se comprueba en las
    DOS posiciones a propósito.
    """
    msg = str(e).lower()
    if "readonly" in msg or "read-only" in msg or "unable to open database" in msg:
        return JournalReadOnly(str(e))
    return e
