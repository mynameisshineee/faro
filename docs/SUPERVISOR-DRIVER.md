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
precondición (auth, organización, objetivo, token, parámetros) · `3` sesión
expirada o sesión continua terminada sin poder renovar · `4` vueltas
completadas con fallos acumulados, sensor indisponible u observaciones sin
resolver (también en la parada por señal, si quedó algo en el aire).

## Operación continua (--continuo)

```sh
python3 supervisor_driver.py \
  --base-url http://127.0.0.1:8077 \
  --token-file ~/.llminbox.session.token \
  --objetivo rti-abc:4242 \
  --continuo \
  --fichero-sesion /var/lib/supervisor/sesion.json
```

La modalidad finita y sus códigos NO cambian; `--continuo` es una opción
explícita encima:

- **Vueltas sin tope**, con la memoria de ciclos y pendientes viva en el
  proceso. `--vueltas` y `--continuo` son excluyentes (se rechaza con `2`).
- **Renovación VALIDADA al acercarse el plazo** (`--refresco-margen-s`, 120 s
  por defecto, que tiene que ser MENOR que `--ttl-s`; 30..3600, 900 por
  defecto) por el contrato real del gateway: `POST
  /native/v1/sessions/refresh` con el Bearer vigente. El gateway ROTA la
  sesión (token nuevo, `runtime_instance` hijo nuevo, token previo revocado
  en la misma transacción) pero el hijo CONSERVA principal, role, lane,
  capacidades y `generation`: es renovación autenticada de la MISMA
  autoridad. Eso se EXIGE, no se supone: el driver la adopta tras validar DOS
  cosas —el recibo COMPLETO (token, rti, autoridad, conjunto exacto de
  capacidades, generación CONSERVADA respecto del arranque y plazo parseado y
  futuro) y un `whoami` CON EL TOKEN HIJO que confirme rti, generación
  explícita, autoridad, capacidades y el MISMO plazo— y sólo entonces adopta
  token/rti/plazo. Adoptar significa:
  contextos y secuencias NUEVOS para el rti hijo (nonce nuevo; una secuencia
  jamás se reutiliza con identidad distinta), conservando los vínculos
  pid+arranque y el estado del sensor (no se pierden transiciones de
  degradación ya vistas). No se exige nueva activación del organigrama por
  token renovado. El token vive sólo en memoria.
- **La transición jamás suelta nada en el aire**: sólo se renueva SIN
  peticiones en vuelo; lo pendiente se reanuda con los mismos bytes y clave
  bajo la sesión VIGENTE antes de rotar. Si no puede resolverse antes de
  expirar: parada visible, código `3`, pendientes declarados en salida y
  libro (`sesion_terminada_sin_refresco`), sin revocación anticipada ni
  olvido. Si el recibo no se valida (campos, tipos, plazos), el plazo está
  vencido o no cuadra recibo↔hijo, o el whoami del hijo falla o no confirma
  la generación: `identidad_rotada` (el token previo ya está revocado). Si la
  autoridad del hijo es otra (principal/role/lane, capacidades o generación
  distintos): `autoridad_incompatible`. Si
  los contextos no se pueden reconstruir (un objetivo dejó de constar):
  `contextos_irreconstruibles`. Todos con código `3`.
- **Cierre ordenado ante SIGINT/SIGTERM**: la vuelta en curso termina, los
  handlers previos se RESTAURAN al salir, el libro queda en
  `parado_por_senal` y el código es `4` si quedaron fallos, sensor caído u
  observaciones sin resolver; `0` si no.
- Sigue sin descubrir pids, sin reiniciar agentes y sin ejecutar
  recuperación: el modo continuo cambia CUÁNTO dura la mirada, no QUÉ hace.

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

La modalidad finita sigue siendo un **ensayo acotado**. El modo continuo
sostiene la mirada renovación tras renovación MIENTRAS la autoridad del
observador no cambie (principal/role/lane): una autoridad distinta —o una
renovación que no se pueda validar— termina la corrida visible con lo
pendiente declarado. Re-registrar pids tras un reinicio sigue siendo de quien
sabe que son suyos.

## Pruebas

`tests/pytest/test_supervisor_driver.py` — composición con contexto atado,
404→`activate_organization`, bootstrap sin `runtime_instance`, epoch+ISO en
`expires_at`, expiración→3, pérdida persistente→4, retiro sin re-adopción,
cero secretos en salida/sesión, contrato 0600 del token y límites de
respuesta; en continuo: dos renovaciones SIMULADAS seguidas (recibo y whoami
del hijo guionizados, generación conservada) con observación posterior y
secuencia nueva por identidad, recibo con OTRA generación→parada, recibo con
capacidades distintas→parada, recibo con plazo vencido→parada, plazos
recibo≠hijo→parada, hijo sin generación explícita→parada (sin fallback),
sensor conservado entre renovaciones, pendiente resuelto antes de rotar
(mismos bytes y clave bajo la sesión vigente), expiración con pendiente sin
resolver→parada visible, autoridad incompatible→parada, whoami del hijo
fallido→parada, señal→cierre ordenado con handlers restaurados (rc 4 con
fallo previo), margen >= ttl rechazado, ttl fuera de contrato,
`--continuo`+`--vueltas` excluyentes y techo de lectura del token EN BYTES.
Las pruebas de este fichero usan un transporte y un reloj controlados y NO
requieren un gateway desplegado: son SIMULACIÓN, y el paquete de aceptación
contra el gateway REAL (dos refrescos reales que rotan, observación posterior
en filas durables) vive en
`tests/native_gateway/test_supervisor_driver_renovacion_real.py`.
