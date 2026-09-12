# Topología del piloto M1 — lo que impone y lo que NO

Estado: entregado sin desplegar, `2026-09-05`. Base `485d2db`.
Correctiva sobre los `4` P1 y los `3` P2 de la auditoría de `@security`
(`MARK:security-auditoria-2911b13-go-integrar-nogo-desplegar`).

Esta pieza es la **topología Docker mínima** para que el journal nativo de
`ADR-001` pueda pilotarse en local sin apoyarse en el buen juicio de ningún
proceso. Todo lo que dice se comprueba en estático; nada de esto se ha
desplegado en esta entrega.

## Qué añade

| pieza | fichero | qué impone |
|---|---|---|
| Compose del piloto | `docker-compose.pilot.yml` | dos servicios (`gateway`, `agente`), volúmenes separados para journal, índice y secretos, un solo ledger RW |
| Guarda de arranque | `tools/pilot_preflight.py` | ⓐ volumen durable · ⓑ testigo de identidad · pepper por fichero · mapa atestado con capacidades · un ledger RW |
| Verificador estático | `tools/pilot_topologia.py` | las nueve invariantes del compose, como función pura sobre el dict |
| Arnés | `tests/pilot/` | `342` pruebas — el control positivo y **un mutante por invariante**, más activación, preflight del projector e inicialización segura de secretos |

## Las dos comprobaciones que son un `AND`

`ADR §Storage` pide que el arranque falle cerrado *«if `coordination.sqlite` is not
on the configured durable volume»*. Eso, solo, **no basta**: un volumen **nuevo,
vacío y con el nombre correcto** cumple esa frase y no contiene nada. Y no es
hipotético — el `2026-09-04` nació `1bb10f55e868_llminbox-data` de un
`docker compose` crudo desde el directorio de despliegue, porque el nombre del
volumen lo decide el *project name*.

- **ⓐ** compara el dispositivo del journal con el del raíz: si coinciden, el
  fichero está en el FS del contenedor y **se destruye en el `recreate`** que el
  propio ADR exige como gate de despliegue.
- **ⓑ** exige un testigo (`/journal/.volume-id`, `0444`) que case con
  `LLMINBOX_JOURNAL_VOLUME_ID`. Ausente ⇒ rojo; distinto ⇒ montaje cruzado.
  `--init` sólo estrena un volumen **sin journal**: con datos y sin testigo, se
  niega a adoptar un almacén de procedencia desconocida.

Ninguna cubre a la otra: **ⓑ sin ⓐ** deja pasar la ruta fuera del volumen;
**ⓐ sin ⓑ** deja pasar el volumen vacío bien nombrado.

## Por qué el `restart` es `"no"`

Un fail-closed con reinicio automático es un crashloop. Este repo pagó **11
reinicios** por una ruta de credencial mal pasada, y la flota lee «el servicio va
y viene» en vez de «el preflight dice que no». Un fallo de precondición se queda
quieto y visible.

## Cómo se corre lo que sí se ha corrido

```
python3 -m venv /private/tmp/llminbox-native-infra-venv
/private/tmp/llminbox-native-infra-venv/bin/pip install pyyaml pytest
/private/tmp/llminbox-native-infra-venv/bin/python -m pytest tests/pilot/ -q   # 302 passed
docker compose -f docker-compose.pilot.yml --env-file <tu .env.pilot> config -q
```

`tests/pilot/` tiene su propio `conftest.py` **vacío a propósito**: el de
`tests/pytest/` importa `fastapi` al cargarse, y comprobar un YAML no debe exigir
media aplicación.

## Lo que esto NO prueba — dicho aquí y no en una nota al pie

1. **No se ha desplegado.** Ni un contenedor levantado, ni un journal creado.
2. **La comprobación ⓐ se prueba con `stat` inyectado.** Se acredita la lógica de
   la comparación, **no** que Docker le dé al journal un `st_dev` distinto en esta
   plataforma. Esa mitad la cierra una instancia desplegada.
3. **El preflight se monta desde `./tools`**, no se copia en la imagen: el
   `Dockerfile` es superficie de `m4`/`m5` y esta entrega no lo toca. En imagen de
   release esto es un `COPY`.
4. **La imagen del agente va por etiqueta**, no por digest. El anclaje por digest
   ya está escrito en `m4-pilot`; repetirlo aquí fabricaría una divergencia.
5. **El gateway no consume el journal todavía.** `coordination.py` está integrado
   en `485d2db` y **no lo llama nadie** (`0` importaciones en `servicio.py`). Esta
   pieza prepara el sitio; la exposición es `P4`.

## `retries` NO gobierna el corte — la sonda corta al PRIMER rojo

Hallazgo de `@security` (`MARK:security-retries-no-aplica-al-corte-del-preflight`).
`readiness()` manda `SIGTERM` al PID 1 en el primer rojo: **no hay contador, ni estado, ni
«N rojos consecutivos»** (`grep -cE "consecutiv|contador|retries|racha"` sobre
`pilot_preflight.py` → `0`). `retries` sólo gobierna la ETIQUETA `unhealthy` de Docker,
porque **la sonda actúa en vez de informar**.

Con `retries: 2` el compose declaraba una tolerancia a fallos transitorios **que no existe**:
un `EIO` momentáneo ⇒ `SIGTERM` al PID 1 a los `30 s` de vida, y con `restart: "no"` al lado
el piloto queda **caído hasta que un humano lo mire**.

> **Se alinea la declaración con la conducta**: `retries: 1`. Cortar ante un rojo real es lo
> que se decidió y sigue en pie —*«Docker no mata un contenedor `unhealthy`»*—; lo que no
> puede seguir es que el fichero prometa una tolerancia que el código no da.

⚠️ **Lo que esto NO es**: no se ha añadido un contador de rojos consecutivos. Esa es la otra
cura posible (`⒜` de `@security`) y **queda para cuando haya piloto de verdad**: hoy costaría
estado en disco para una tolerancia que nadie ha pedido todavía.

## Permisos de volumen — PRECONDICIÓN NUEVA del endurecimiento `I13`

`gateway` y `estreno` pasan a correr `user: "1000:1000"` con `read_only: true`. Los volúmenes
nombrados **nacen `root:root`**, así que un proceso `uid 1000` no puede escribir en ellos.

**Antes del PRIMER estreno**, alinea la propiedad:

```bash
docker run --rm -v llminbox-pilot-m1_llminbox-pilot-journal:/v alpine chown -R 1000:1000 /v
docker run --rm -v llminbox-pilot-m1_llminbox-pilot-index:/v   alpine chown -R 1000:1000 /v
chown -R 1000:1000 "$LLMINBOX_PILOT_LEDGER_HOST"        # el bind del host
```

Si no se hace, **el estreno falla CERRADO** con un `EACCES` con nombre — no corrompe nada, y
ése es el lado bueno del error. `read_only` congela el sistema de ficheros de LA IMAGEN: los
volúmenes (`/journal`, `/data`, `/ledgers/llminbox`) se siguen escribiendo igual.

## DEUDA EXPLÍCITA — los dos secretos que siguen por `environment`

`@security` midió que **de tres secretos sólo uno tiene el tratamiento correcto**, y el motivo
está escrito en el propio compose dos líneas más arriba (*«El pepper, por FICHERO. Por entorno
lo lee `docker inspect`»*):

```
LLMINBOX_PEPPER_FILE   -> por FICHERO, montado ro          ✅
LLMINBOX_TOKEN         -> por `environment`                🔴 lo devuelve `docker inspect`
LLMINBOX_CREDENCIAL    -> por `environment` (en `agente`)  🔴 idem
```

**NO se cierra en esta correctiva, y el motivo es de dependencia, no de pereza**: la cura es
`LLMINBOX_TOKEN_FILE` / `LLMINBOX_CREDENCIAL_FILE` montados `ro`, y **eso exige que el Gateway
sepa leer esas dos variables por fichero**. Hoy no consta que lo soporte. Tocar el compose
antes que el código dejaría el piloto sin arrancar.

- **Dueño de la mitad que falta**: `@codex-llminbox` / `@backend` — el patrón `_FILE` ya está
  implementado para el pepper, así que es extenderlo, no inventarlo.
- **Dueño de la mitad de aquí**: `infra`, en cuanto la otra exista.
- ⚖️ **Cota, y es de `@security`**: `docker inspect` exige acceso al demonio, que ya es
  equivalente a root en el host. **No es escalada de privilegio.** Es inconsistencia con una
  defensa que este mismo fichero decidió que valía la pena, y residuo en `docker inspect`, en
  los logs de `compose` y en cualquier volcado de estado que alguien pegue en un ledger.

## Gate PENDIENTE de contrato — el invariante de punto de montaje

`_abrir_directorio` recorre la ruta componente a componente con
`openat(dirfd, parte, O_DIRECTORY|O_NOFOLLOW)`, así que **ningún componente de la
ruta puede ser un enlace simbólico** — ni el último ni los de en medio. Eso cierra
el hueco que @security midió (H1: componente INTERMEDIO con `O_NOFOLLOW` **abría**).

**Lo que NO cierra, y no se va a cerrar aquí:**

- `st_dev` **no separa** dos rutas del mismo sistema de ficheros. La comprobación ⓐ
  compara el `st_dev` del journal con el de `/`; eso distingue «hay algo montado»
  de «no hay nada», pero no distingue *un* volumen de *otra ruta del mismo volumen*.
- El invariante que sí lo separaría es el de **punto de montaje**: sobre el
  descriptor ya anclado, `fstat(fd)` frente a `stat("..", dir_fd=fd)` — si el
  dispositivo cambia al subir un nivel, `fd` ES un montaje.

**Por qué queda pendiente y no se implementa en esta correctiva:** es un cambio de
CONTRATO. Hoy el preflight acepta cualquier directorio que sea el almacén acordado;
con ese gate pasaría a **rechazar todo directorio que no sea un punto de montaje**,
lo que afecta a bancos, desarrollo local y a cualquier despliegue que use un bind a
un subdirectorio. Quién puede aceptar ese contrato es el dueño del protocolo, no
esta entrega.

**Condición de arranque del gate** (lo que hace falta ANTES de escribirlo):

1. El dueño del protocolo acepta que un almacén DEBE ser un punto de montaje.
2. Se enumera la población afectada (compose del piloto, del estreno, bancos y el
   arnés) y se mide cuántos dejarían de arrancar.
3. Se mide el comportamiento del invariante en las plataformas donde corre —Linux
   con volumen Docker real y macOS—, en vez de suponerlo. *(Una nota anterior
   afirmaba que en macOS daría falsos negativos por firmlinks de APFS; **no estaba
   medido** y se ha retirado, para que la ausencia del gate no se apoye en una cota
   inventada.)*

Hasta que ① se decida, esto no tiene dueño y **no es un P1 abierto**: es un gate sin
contrato.

## La frontera del token legacy — P1-4

`LLMINBOX_TOKEN` es obligatorio (el servicio arranca mudo sin él) y **su portador
dispara el fail-open de `exige_ser`** (`servicio.py:3494-3496`: sin identidad,
anota y deja pasar). El piloto existe para probar que no hay escrituras
operativas anónimas, así que heredar el token de la flota metería dentro justo la
credencial que las hace anónimas.

**La frontera, dicha entera:**

1. El gateway del piloto toma su token de **`LLMINBOX_PILOT_TOKEN`**, una variable
   distinta. Con `${LLMINBOX_TOKEN}` el de la flota entraría solo, heredado del
   entorno de quien despliega, sin que nadie escribiera una línea. `I10` lo
   comprueba en estático.
2. **Ese token no autoriza ninguna ruta nativa, y hoy eso es cierto POR AUSENCIA**:
   no hay rutas nativas en `servicio.py` (`0` de `/sessions`, `/whoami`, `/events`,
   `/receipts`, `/leases`, `/commands`). Un estado no es una garantía, así que
   `test_P1_4_FRONTERA_el_token_legacy_no_autoriza_ninguna_ruta_native` **se pone
   roja el día que `P4` cablee la primera** y obliga a decidir en vez de heredar.
3. Lo que el token legacy sigue autorizando son las rutas de hoy (`/append`,
   `/claim`, `/inbox/*/leido`, `/vigilancia/ack`). **Eso no lo cierra la
   topología**: es del middleware de autorización, y es de `P4`.
4. **Techo que no es mío y se cita**: `SECURITY.md` mide y publica que en Docker
   Desktop macOS *loopback no es aislamiento*, y que quien alcanza el demonio lee
   cualquier token con `docker inspect`. El aislamiento del piloto se prueba
   contra otro CONTENEDOR, nunca contra otro usuario del mismo demonio.

## El límite del testigo, declarado en vez de prometido — P2-1

El testigo v2 lleva `{id, nonce, nacido}` y lo que se compara es su **huella**
(`sha256` del fichero), no el id. El `nonce` nace en el estreno, así que **dos
volúmenes estrenados con el mismo `VOLUME_ID` no pueden estar los dos verdes**:
sólo uno casa con la huella declarada. Eso cierra el caso split-brain, que es
donde la detección de montaje cruzado tenía que servir.

**Lo que NO detecta, y se dice**: una **copia byte a byte** del testigo. Quien
puede copiarlo ya está dentro del volumen. Atarlo al dispositivo tampoco lo
cierra — **dos volúmenes nombrados del mismo motor comparten `st_dev`**, así que
ese control no discriminaría, y montar un control que no discrimina es peor que
no tenerlo: se lee como cobertura. Sin huella declarada el arranque **se para**;
no se degrada a comparar sólo el id.

## Un readiness rojo CORTA — y corta de más

`@security` lo nombró: **Docker no mata un contenedor `unhealthy`**. Con
`restart: "no"` al lado, un readiness rojo dejaba el servicio contestando con la
etiqueta puesta — fail-closed en el arranque y **advisory a partir de ahí**. Si el
mapa se sustituye, el testigo deja de casar o el ledger cambia de identidad
*después* de arrancar, seguir aceptando escrituras es lo que el piloto existe para
impedir.

Ahora `--readiness` en rojo manda `SIGTERM` al PID 1 y el contenedor se para
(y con `restart: "no"`, se queda parado y visible).

⚠️ **Corta ENTERO, también las lecturas**, y eso es más ancho que el ADR §23
(*journal-RO sirve lecturas y rechaza mutaciones*). La distinción fina vive en el
middleware del servicio y es de `P4`; desde fuera del proceso lo único que se
puede hacer sin mentir es parar. **Se elige parar y se dice.**

## Estrenar los dos testigos — el camino, entero y en orden

`@sdet` lo midió (⑤) y tenía razón: el fail-closed se hizo más fuerte y **el camino
para estrenarlo no estaba escrito**. Peor: el gateway exige las dos huellas con
`:?`, así que no se podía ni levantar para producirlas. Huevo y gallina.

```bash
# 0 · genera los dos identificadores (una vez, y guárdalos)
python3 tools/pilot_preflight.py --genera-id     # LLMINBOX_JOURNAL_VOLUME_ID
python3 tools/pilot_preflight.py --genera-id     # LLMINBOX_LEDGER_PILOTO_ID

# 1 · estrena. Fichero APARTE porque no exige las huellas que aún no existen.
#     Mismo `name:` que el piloto ⇒ los MISMOS volúmenes, no un juego paralelo.
docker compose -f docker-compose.pilot-estreno.yml run --rm estreno
#   -> ✅ estreno hecho. Declara estas dos líneas y arranca:
#      LLMINBOX_JOURNAL_VOLUME_WITNESS=<sha256>
#      LLMINBOX_LEDGER_PILOTO_WITNESS=<sha256>

# 2 · pega esas dos líneas en tu `.env.pilot` (que NO se commitea) y arranca
docker compose -f docker-compose.pilot.yml --env-file .env.pilot up -d
```

**Es idempotente**: con los testigos ya puestos no escribe nada, valida que son los
suyos y devuelve las mismas huellas. Un camino de estreno que sólo vale la primera
vez obliga a saber si ya se corrió, y ése es justo el dato que el operador no tiene
delante.

**Sin el paso 1, el arranque falla cerrado** — por el journal y, si sólo estrenaste
ése, por el ledger. Las dos mitades tienen su prueba
(`test_SDET_5_SIN_estreno_…`).

## Qué pasa —y qué NO— cuando el gateway se vuelve unhealthy DESPUÉS de arrancar

Esto se escribe porque «el healthcheck corre el mismo preflight» se lee como que
algo pasa cuando dice que no, y conviene ser exacto:

- **Docker no reinicia ni para un contenedor `unhealthy` por sí solo.** `unhealthy`
  es una etiqueta; nadie actúa sobre ella. Con `restart: "no"` tampoco hay
  reinicio, y `depends_on: service_healthy` **sólo gatea al agente al levantar**.
- **Lo que sí actúa es nuestra sonda**: `--readiness` en rojo manda `SIGTERM` al
  PID 1 y el contenedor se para. Eso lo hace el preflight, **no Docker**.
- **La ventana no es cero**: el healthcheck corre cada `interval` (`30s`), así que
  un pepper rotado o un mapa sustituido pueden servirse hasta media ventana. Y si
  alguien levanta la imagen **sin este compose**, no hay healthcheck y no actúa
  nadie.
- **Y cuando actúa, corta ENTERO**, también las lecturas. La distinción fina —
  *rechazar mutaciones y seguir sirviendo lecturas*, ADR `§23` — **no se puede hacer
  desde fuera del proceso**. Es del **middleware nativo** y es de `P4`: mientras no
  exista, la única honestidad disponible es parar.
