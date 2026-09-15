# G8 · Activador de organización — guía de operación

> Entrada **PRIVADA de operador** para activar el organigrama y sus workloads
> (ADR-002, Decisión 6). No hay endpoint de mutación de agentes, no hay segundo
> servidor ni projector, y esta herramienta no crea bases, no migra ni liga
> credenciales. Implementación: `tools/activa_organizacion.py` · Falsadores:
> `tests/pilot/test_activa_organizacion.py`, incluido en el job piloto de CI.

## Cuándo NO usar esto

- La base **no existe** o está en v6 → el activador lo rechaza (rc=3). Crear o
  migrar es la operación deliberada del canon `docs/V1.0-SCHEMA-V7-MIGRATION.md`
  (fotografía, cerrojos, digest, evidencia), nunca un efecto lateral de activar.
- La credencial del operador **no está ligada** en `credential_bindings` →
  ligarla es otra operación de operador (`bind_credential` / el mapa de
  arranque del despliegue). Este CLI no llama a `bind_credential`.
- El snapshot no es de esta casa (revision, attestation_state o claves fuera
  del vocabulario cerrado) → rechazo rc=3, fail closed.

## Prerrequisitos (los cuatro ficheros)

| Fichero | Condición |
|---|---|
| `--db` | Base v7 **existente** del journal (`durable_v=7`). |
| `--pepper-file` | ≥ **32 bytes útiles tras `strip()`** (regla del arranque). |
| `--snapshot` | JSON con vocabulario cerrado: `revision` (int > 0), `attestation_state` ∈ {attested, stale, unattested}, `roles`, `reports`, `reviewers`, `escalations`, `workloads`. Claves EXACTAS por elemento (`_records`); cualquier clave ajena al vocabulario rechaza. |
| `--credencial-file` | Credencial **ya ligada** con `organization.activate` (y `organization.read` si quieres el recibo de lectura nativa). Identidad, carril y permisos salen SIEMPRE de la ligadura viva: nada de esto se toma del JSON. |

Forma del snapshot (los cinco registros con sus claves exactas):

```
roles      : {role, layer, policy_code}                       layer int 0..255
reports    : {role, reports_to}                               hijo→superior estricto
reviewers  : {role, reviewer_role}
escalations: {role, trigger_code ∈ {BLOCKED,REVIEW_REQUIRED,INCIDENT,RECOVERY_FAILED}, target_role}
workloads  : {workload_id, role, principal_id, runtime_instance, credential_generation}
             (runtime_instance y credential_generation van JUNTOS o los dos a null)
policy_code ∈ {STANDARD, REVIEW_REQUIRED, HUMAN_APPROVAL_REQUIRED} | null
```

El grafo exige exactamente **una raíz layer=0**, todo rol no-raíz con
`reports_to`, capas estrictamente crecientes, sin ciclos. La revisión es
**monótona por carril** (la impone el kernel: repetir o retroceder = rc=1).

## Ejecución

```
python3 tools/activa_organizacion.py \
    --db /ruta/coordination.sqlite \
    --pepper-file /ruta/pepper.bin \
    --snapshot /ruta/org-snapshot.json \
    --credencial-file /ruta/credencial.operador
```

Recibo por stdout (JSON): `activacion` (revision, `source_sha256_canonico`,
`snapshot_archivo_sha256`, cota del digest) + `organizacion_activa` = **lectura
nativa** `Journal.organization(token)` del estado activo. Sin
`organization.read` en la ligadura, el recibo trae `lectura_nativa: denegada`
y SIN `organizacion_activa`: la activación ya es durable (rc=0 igualmente) y
la lectura nativa es una capacidad aparte — no se simula.

**Dos cifras distintas, deliberadamente:** `source_sha256_canonico` es el
SHA-256 de la serialización canónica (claves ordenadas, separadores compactos) —
es el que viaja al kernel y es estable ante reordenación y espacio en blanco.
`snapshot_archivo_sha256` es del fichero en bruto, para trazabilidad de soporte.
No son intercambiables.

## Códigos de salida

| rc | Significado | Ejemplos |
|---|---|---|
| 0 | Activado (recibo JSON); la activación es durable | con `organization.read`: `activacion` + `organizacion_activa` (lectura nativa) · sin él: `activacion` + `lectura_nativa: denegada` |
| 1 | El **kernel** negó la operación | `PolicyDenied` (sin capacidad), `AuthError` (credencial no ligada/retirada), `OrganizationConflict` (revisión no monótona, grafo inválido), `JournalReadOnly` (ventana congelada) |
| 2 | Uso incorrecto del CLI | argparse |
| 3 | Precondición **local** fallida | ficheros ausentes/ilegibles (permisos, E/S), credencial no UTF-8, pepper corto, base inexistente/sin sello/v6/futura, snapshot con clave o vocabulario ajeno o `attestation_state` de tipo no textual, `OpenModeRestricted` |

`OpenModeRestricted` (rc=3) es la garantía de producto: el journal se abre con
`open_mode="solo_existente_v7"` y el **kernel** rechaza crear o migrar dentro
de la decisión que gobierna `initialize()` — bajo el cerrojo de ciclo de vida,
sobre la clasificación recién fotografiada, **antes** de retener la instantánea
v6 y **antes** de la sonda WAL. El modo por defecto de todos los demás llamantes
no cambia (`"estandar"`), y `tests/pilot/test_activa_organizacion.py::test_
garantia_en_la_decision_bajo_cerrojo_y_preservacion_del_original` falsifica las
dos caras: el original queda byte a byte al rechazar, y el modo estándar sigue
creando y migrando. Nota documentada: un intento rechazado puede dejar el
fichero `.lifecycle` vacío del journal (cerrojo del ciclo de vida, comportamiento
estándar de cualquier `initialize()`); el `.sqlite` no se toca.

Ventana congelada (`volumen sin cerrojo`): el kernel entra en
`READ_ONLY_READY` y el activador muere después en `_guard_mutable` (rc=1,
`JournalReadOnly`) — sin creación ni migración en ese camino tampoco.

## Limitaciones declaradas

1. **Alcance de la verificación.** El job piloto de CI ejecuta
   `tests/pilot/test_activa_organizacion.py` con bases y credenciales sintéticas.
   El recibo de cada corrida debe citar su SHA exacto y el resultado de los
   casos; una inspección de fuente no acredita ejecución. Esta suite no prueba
   la activación de una instalación desplegada ni una flota por red.
2. **El espejo de vocabularios es local.** `CLAVES_SECCIONES` del CLI replica
   las claves exactas de `_records`; el kernel revalida todo dentro de su
   transacción y su rechazo manda (el CLI adelanta el fallo, no es autoridad).
3. **Recibo nativo condicionado.** Sin `organization.read` en la ligadura, la
   activación es igualmente durable pero el recibo declara
   `lectura_nativa: denegada` — no se simula la lectura.
4. **Una lane por llamada.** El carril lo fija el carril de la credencial del
   operador (`view.lane`); activar N carriles = N llamadas con credenciales de
   sus carriles.
5. **No es supervisor.** El backend conserva el supervisor runtime; este CLI no
   duplica recovery ni observación (sólo declara workloads esperados).
