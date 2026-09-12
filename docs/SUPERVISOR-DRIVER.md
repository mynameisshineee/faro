# Supervisión externa de procesos

`supervisor_driver.py` observa procesos registrados y envía sus observaciones
al gateway nativo. Cada invocación ejecuta un número limitado de vueltas y
deja un registro local de la operación. Requiere una sesión del observador y
una organización activada en el gateway.

## Qué es

El CLI compone los módulos de supervisión del producto:

| Módulo (preexistente) | Papel en la composición |
|---|---|
| `RegistroDeSupervision` | registro EXPLÍCITO de pid+arranque; sin descubrimiento |
| `CiclosPorVinculo` (subclase con contexto) | un ciclo por vínculo, memoria entre vueltas |
| `SupervisorPeriodico` | vueltas CONTADAS; observa, no actúa |
| `TransporteSupervisor` | POST autenticado; 202-only; 409→resincronización |

## Uso

```sh
python3 supervisor_driver.py \
  --base-url http://127.0.0.1:8077 \
  --token-file ~/.llminbox.session.token \
  --objetivo rti-abc:4242 \
  --vueltas 4 \
  --intervalo-s 15 \
  --fichero-sesion /var/lib/supervisor/sesion.json
```

- `--token-file`: Bearer de sesión; fichero REGULAR con 600 EXACTO (se exige antes de usarlo).
- `--objetivo`: repetible, formato `RTI:PID`; el `workload_id` lo sirve el gateway.
- `--fichero-sesion`: libro operativo 0600; jamás autoridad.

Códigos de salida: `0` vueltas completadas sin fallos ni pendientes · `2`
precondición (auth, organización, objetivo, token) · `3` sesión expirada a
medio camino · `4` vueltas completadas con fallos acumulados, sensor
indisponible u observaciones sin resolver.

## Garantías de ciclo de vida

- **Termina solo**: vueltas agotadas o sesión expirada. No hay bucle infinito.
- **La memoria de ciclos muere con el proceso.** El nonce NO se hereda entre
  corridas (fabricar claves nuevas es lo que evita el replay silencioso) y la
  secuencia se rehidrata contra la autoridad por el `409
  OBSERVATION_SEQUENCE_CONFLICT` de la corrida siguiente. El fichero de sesión
  NO guarda seq, nonce ni token: sería una segunda base durable, prohibida por
  ADR-002. Es libro operativo (pid, arranques, vueltas, estado), 0600, escritura
  atómica, no autoridad.
- **Exit code honesto**: acabar las vueltas no es éxito; `4` declara lo que
  quedó en el aire.
- **Un pid retirado no se re-adopta**: si el arranque cambió, es OTRO proceso.
- **No actúa**: no hay kill, señal ni reinicio; el driver no tiene ese camino.

## Identidad: consultada, no declarada

El CLI no acepta `workload_id`, `lane` ni identidad de observador por bandera.
`whoami` y `GET /native/v1/runtimes/{rti}` las sirven; el operador sólo aporta
QUÉ pid mirar. El contexto autenticado `(observer_rti, observer_generation,
target_rti, target_generation)` se ata A LA VEZ a la secuencia y al transporte
en la composición (`CiclosPorVinculoConContexto`): una secuencia sin contexto y
un transporte con él se rechazan mutuamente antes de enviar.

## Prerrequisito nombrado (no fabricado)

Los objetivos deben existir en una revisión de organización **activada**
(`Journal.activate_organization`, entrada de operador). Si
`GET /runtimes/{rti}` responde 404, el driver falla cerrado nombrándolo: no
inventa permisos ni un endpoint público incompatible con ADR-002.

## Límites declarados

Una corrida finita es un **ensayo acotado**: no acredita supervisión continua
ni recuperación de varios días. Para operación continua hace falta un
lanzador externo que re-invoque el driver y una decisión (fuera de este parche)
sobre quién re-registra los pids tras un reinicio.

## Pruebas

`tests/pytest/test_supervisor_driver.py` — composición con contexto atado,
404→`activate_organization`, bootstrap sin `runtime_instance`, epoch+ISO en
`expires_at`, expiración→3, pérdida persistente→4, retiro sin re-adopción,
cero secretos en salida/sesión, contrato 0600 del token y límites de respuesta.
Las pruebas usan un transporte y un reloj controlados y no requieren un
gateway desplegado.
