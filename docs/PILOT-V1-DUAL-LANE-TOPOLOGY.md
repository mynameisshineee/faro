# Falsador de dos lanes — V1.0 / G9 (workstream «two-lane isolation deployment»)

Estado: **implementación + revisión estática, sin desplegar**. Base `7677025`
(la misma que certificó el candidato v0.9). Rama `agent/llminbox-v10-isolation`,
worktree `/private/tmp/llminbox-v10-isolation` — Infra es SOLE WRITER de este
workstream, per `docs/V1.0-EXECUTION.md`.

🔴 **CAPACITY GATE — NO-GO de CFO Guardian vigente al cerrar esta entrega.**
Ninguna carga (Docker, suite completa, mutantes, benchmark) corre hasta que
CFO publique GO. Lo que este documento reporta como «medido» es exactamente
lo que se corrió ANTES de que llegara el NO-GO — con su alcance dicho, no
inflado — y nada después. Ver §Qué se corrió y cuándo.

## Qué añade

| pieza | fichero | qué impone |
|---|---|---|
| Compose de dos lanes | `docker-compose.pilot-dual.yml` | cuatro servicios (`gateway_a/b`, `agente_a/b`), cada uno con la forma de M1, en redes (`red_a`/`red_b`) y volúmenes separados por lane |
| Estreno de dos lanes | `docker-compose.pilot-dual-estreno.yml` | dos servicios (`estreno_a/b`) que sólo tocan el journal y el ledger de SU lane |
| Verificador de cruce | `tools/pilot_topologia_dual.py` | reutiliza `pilot_topologia.verificar()` por lane (namespaced `A:`/`B:`) y añade ocho invariantes de cruce (`X0`..`X7`, abajo) |
| Arnés | `tests/pilot/test_topologia_dual.py` | `27` pruebas — control positivo, control de que el instrumento no está mudo, y un mutante por invariante de cruce |
| Extensión no-breaking | `tools/pilot_topologia.py` | `verificar(..., token_var=None)` — modo relajado de `I10` para dos lanes; `normaliza_montajes`/`partir_montaje` promovidos a API pública. Los `342` tests de M1 siguen pasando sin cambio de comportamiento (`token_var` por defecto sigue siendo `"LLMINBOX_PILOT_TOKEN"`) |

## La premisa: colisión deliberada de nombre lógico

Las dos lanes declaran el MISMO `LLMINBOX_LEDGERS` —el nombre lógico
`llminbox` es idéntico en `gateway_a` y `gateway_b`, letra por letra—. Esto no
es un descuido: es la forma que pide G9 («two isolated lanes with
intentionally colliding logical names», `docs/V1.0-EXECUTION.md` §D). Si el
aislamiento dependiera de que ese nombre lógico fuera único entre lanes, este
compose lo rompería a propósito para probarlo, y `tools/pilot_topologia_dual.py`
lo verifica con la colisión PUESTA (`test_la_colision_del_nombre_logico_NO_es_
un_hallazgo_por_si_sola`), no después de quitarla.

Lo que aísla de verdad — y lo único que el verificador mide — son ocho
recursos FÍSICOS, cada uno con su variable propia por lane:

| código | categoría (lista de G9) | qué compara |
|---|---|---|
| `X0_RED` | (soporte de todas) | las dos lanes declaran `networks:` propias y no las comparten |
| `X1_VOLUMEN` | volumen | ningún volumen NOMBRADO ni origen de bind se repite entre lanes (excluye `/app/tools`, que es código compartido, no estado) |
| `X2_IDENTIDAD` | identidad | `LLMINBOX_TOKEN` de cada lane referencia una variable o valor distinto |
| `X3_SESION` | sesión | `LLMINBOX_CREDENCIALES_SHA` (el atestado del mapa que deriva rol/carril/sesión) es distinto por lane |
| `X4_LEDGER` | ledger | `LLMINBOX_LEDGER_PILOTO_ID`/`_WITNESS` son distintos por lane |
| `X5_RECIBO` | recibo | `LLMINBOX_JOURNAL_VOLUME_ID`/`_WITNESS` — el journal es donde vive el recibo durable de cada transición — son distintos por lane |
| `X6_CURSOR` | cursor de búsqueda | `LLMINBOX_SEARCH_CURSOR_KEY` es distinto por lane, nunca compartido ni por defecto |
| `X7_RECUPERACION` | recuperación | el ESTRENO de cada lane (única escritura de identidad en este compose) sólo toca volúmenes de SU lane — se mide sobre `docker-compose.pilot-dual-estreno.yml`, no sobre el verificador de arriba |

## ADR-002 — esto es UNA de las DOS mitades del falsador de G9

Canon `d649cf7`: G9 exige **dos falsadores separados**, y este documento cubre
sólo uno.

1. **Dual-deployment con fingerprints atestados** — ESTE workstream (Infra).
   Verifica que la TOPOLOGÍA (compose, volúmenes, red, variables) no permite
   que un cruce ocurra por construcción.
2. **Single-kernel adversarial con dos lanes** — el supervisor nativo
   (`/private/tmp/llminbox-v10-supervisor`, Backend). Un kernel, dos lanes
   admitidas, e intentos REALES por API de cada cruce (sesión, ledger,
   recibo, cursor, recuperación), con el veredicto esperado explícito
   (`404`/denegación + cero filas). Ese falsador no existe todavía en este
   worktree ni lo sustituye nada de aquí.

No presentar lo de aquí como si cerrara G9 completo: cierra la mitad de
**despliegue**. La mitad de **kernel** es responsabilidad del otro workstream,
y su ausencia no es un hallazgo de éste — es el reparto que ADR-002 fija.

## Límites declarados — dicho aquí y no en una nota al pie

1. **Nombre de variable distinto no es VALOR distinto.** `X2`..`X6` prueban
   que las dos lanes no referencian la MISMA variable de entorno ni el mismo
   literal en el YAML. Es necesario y **no es suficiente**: nada impide que un
   operador rellene DOS variables con nombres distintos con el MISMO valor a
   mano en su `.env` real — y sin Docker desplegado no hay un secreto
   RESUELTO que este verificador pueda leer (ni debería: leer un secreto para
   compararlo lo convertiría en el propio canal de fuga, el mismo error que
   `pilot_topologia.py` ya evita en `I4`/`I10`).

   **Pendiente — fingerprint atestado (ADR-002):** un HMAC calculado en
   tiempo de arranque, con el pepper propio de la lane como clave y el
   witness (`JOURNAL_VOLUME_WITNESS`/`LEDGER_PILOTO_WITNESS`) como mensaje —
   nunca el secreto en claro — comparado entre lanes por un verificador
   externo que sólo ve las DOS huellas, no los secretos que las producen. Con
   pepper y witness ya separados por volumen (`X1_VOLUMEN` los protege), esto
   cerraría el hueco de «mismo valor, nombres distintos». **No implementado
   en esta entrega**: diseñarlo, escribirlo y verificarlo con datos reales
   exige Docker corriendo, y el NO-GO de CFO lo pospone — no lo evita por
   pereza. Dueño: `infra`, próxima sesión con GO.

2. **`X0_RED` prueba TRANSPORTE, no KERNEL.** Que las dos lanes no compartan
   red demuestra que un cruce por HTTP es irrealizable bajo ESTE compose. No
   demuestra que el kernel nativo rechazaría la petición si las redes
   estuvieran unidas — eso es exactamente lo que el falsador
   single-kernel-adversarial (arriba) tiene que probar, con la API real y un
   veredicto observado, no con una regla de red.

3. **`X6_CURSOR` con la clave vacía en las dos lanes no prueba un cruce
   rechazado.** Prueba que Search está DESMONTADO (fail-closed) en las dos —
   ausencia de superficie, no un intento de cruce que el kernel haya negado.
   El compose real SÍ pasa una clave propia por lane (`env.example`); el caso
   de las dos vacías es sólo el control de que la ausencia no se lee como
   cruce.

4. **`X7` no es G7 completo.** Cubre sólo la escritura de identidad del
   ESTRENO — el bootstrap del volumen, análogo de despliegue de una
   recuperación. La recuperación de UN WORKLOAD (`stale`/`stopped`/
   `recovering` con recibo por transición, G7) es del supervisor nativo y
   queda fuera de este falsador hasta que su API esté integrada — «external
   supervisor adapter» es FOLLOW-UP, no algo que esta entrega reclama cerrado.

5. **Nada de esto es C4.** Este workstream es infraestructura de despliegue;
   no audita ni repite el endurecimiento de sesión del `ActiveProjectorRunner`
   (`C4`, en `tests/projector/`/`tests/pytest/`), que pertenece al workstream
   del supervisor y queda fuera por instrucción explícita del encargo.

## Corridas paralelas — container_name y puertos

`docker-compose.pilot-dual.yml` NO fija `container_name:` (a diferencia de
M1): un nombre de contenedor es único en el DAEMON entero, no por proyecto, así
que dos corridas paralelas de este mismo fichero (dos PRs, dos runners de CI)
chocarían aunque `name:` y los volúmenes fueran distintos. Compose deriva
`<project>-<service>-N` automáticamente.

Los PUERTOS de host siguen con default fijo
(`${LLMINBOX_DUAL_LANE_A_PORT:-8199}` / `${LLMINBOX_DUAL_LANE_B_PORT:-8299}`) —
para correr dos instancias en el mismo host hace falta `-p <otro-nombre>` Y
overridear las dos variables de puerto; el fichero no lo hace por sí solo.

## Qué se corrió y cuándo

Antes del NO-GO de CFO Guardian de esta sesión:

- `docker compose -f docker-compose.pilot-dual.yml --env-file tests/pilot/env.example config -q` → `rc=0`.
- `docker compose -f docker-compose.pilot-dual-estreno.yml --env-file tests/pilot/env.example config -q` → `rc=0`.
- `pytest tests/pilot/` → `369` recogidas (`342` de M1 + `27` de este fichero), `27/27` de este fichero en verde tras dos correcciones encontradas por el propio arnés (ver abajo).

Ninguna de las dos invocaciones de `docker compose ... config` levanta un
contenedor ni conecta con el demonio para nada más que renderizar el YAML —
es la misma comprobación, sin cambio de forma, que la CI de M1 ya corre en su
job `pilot-m1`. Después de esas corridas llegó el NO-GO y no se ha vuelto a
invocar `docker` ni `pytest`. Los cambios de este documento y los ficheros que
cita desde entonces son ESTÁTICOS (edición de texto/YAML/Python, sin
ejecución) y quedan **pendientes de re-verificación** en cuanto CFO publique
GO — no se afirma que sigan en verde, se afirma que estaban en verde en el
commit medido y que los cambios posteriores no tocaron lógica de test.

Dos hallazgos que el propio arnés cazó en la corrida pre-NO-GO, y por qué
importan: (a) `I10` de M1 exige un nombre de variable FIJO
(`LLMINBOX_PILOT_TOKEN`), que dos lanes con nombre propio nunca podrían
cumplir a la vez — de ahí el modo relajado `token_var=None`, que traslada la
unicidad ENTRE lanes a `X2_IDENTIDAD` en vez de duplicarla; (b) comparar el
texto CRUDO de un bind con indirección (`${VAR:?mensaje}`) fallaba cuando el
mensaje de error difería entre lanes aunque la VARIABLE fuera la misma copiada
— la cura compara el nombre de variable referenciado, no la cadena entera.

## Lo que esto NO prueba — dicho aquí y no en una nota al pie

1. **No se ha desplegado.** Cero contenedores levantados, cero journal creado,
   igual que M1.
2. **No hay fingerprint atestado.** Ver §Límites declarados, punto 1.
3. **No hay falsador single-kernel-adversarial.** Ver §ADR-002.
4. **`I13` se verifica sobre las cuatro lanes**, pero no se ha medido en una
   plataforma real que `read_only`+`user: "1000:1000"` no rompan el arranque —
   misma precondición de propiedad de volumen que documenta
   `docs/PILOT-M1-TOPOLOGY.md` §Permisos de volumen, ahora×2 (una por lane).
