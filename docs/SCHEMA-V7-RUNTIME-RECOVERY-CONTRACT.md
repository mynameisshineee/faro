# V1.0 FLEET CONTINUATION SPEC (mínima)

## Alcance
Kernel de runtime/recovery para `coordination.py` en durable_v 7.

## Invariantes
1. `evaluate_runtime_deadlines` solo revisa observaciones del mismo `target_runtime_instance` y
   `target_generation` del workload activo.
2. Un `command` de recovery **no** puede avanzar por `advance_command`; su avance no terminal
   solo se permite por `advance_runtime_recovery` en estados `received` y `executing`.
3. El estado final de recovery (`recovery_succeeded`/`recovery_failed`) se fija por
   observación tipada y no por transiciones ad hoc del comando.
4. Un actor no puede observar su propio runtime objetivo en `record_runtime_observation`.

## Límites operativos
- `stale_after_s` debe ser entero positivo y acotado a 31 días.
- `observe` no emite `stale` desde `degraded` en mezcla de generaciones.
- `advance_runtime_recovery` acepta únicamente `received|executing`, con estado previo `accepted|received`.

## Falsadores mínimos
- F-RC1: dos `advance_command(..., "received")` para un `command_id` de recovery deben
  fallar con `CommandTransitionInvalid`.
- F-RD1: al degradar un workload por generación previa, `evaluate_runtime_deadlines`
  no debe evaluar observaciones de una generación histórica diferente.
- F-OBS1: un observer y target compartiendo misma identidad (`runtime_instance`) debe recibir
  `OperationInvalid`.
