# Notas de diseño (versión larga, castellano)

> Este documento es la versión extendida de lo que el `README.md` resume. Vive
> aparte porque es largo, y porque el README de un proyecto público debe poder
> leerse en dos minutos. Aquí sí caben los rodeos, los dos intentos fallidos y
> el razonamiento completo detrás de cada decisión — es la parte del proyecto
> con más medición real por línea, y por eso se conserva entera en vez de
> resumirse a una lista de features.
>
> Nombres de proyectos internos y de personas se han generalizado; los números
> medidos son los reales del despliegue de origen (2026-07-27).

## Por qué existe

El ledger más grande del despliegue de origen son 54 MB / 23.491 entradas /
13,5 M tokens ≈ 68 ventanas de contexto (asumiendo una ventana de 200K
tokens). La flota que lo escribe lo lee con `tail` (13.169 invocaciones) y
`grep` (8.733), y escribe con `>>` (5.276).

Un `tail -500` cuesta 27.983 tokens, trae 49 entradas y cubre del orden de 10
minutos de historia al ritmo de escritura de pico medido (303 entradas/hora).
En 5.580 de 6.432 relecturas consecutivas de una misma sesión habían
aparecido entradas fuera de esa ventana. Y no existía la pregunta *«¿qué hay
para mí desde la última vez que miré?»*.

`GET /inbox/<agente>` contesta esa pregunta en 2.778 tokens — un orden de
magnitud menos — porque son las entradas dirigidas a ese agente, no las
últimas 500 líneas de lo que sea.

### Lo que NO era el problema, aunque lo parecía

Medirlo antes de diseñar cambió el diagnóstico dos veces:

- **No era E/S.** Leer los 54 MB enteros del disco: 26 ms. `tail -500`: 20
  ms. El coste es de contexto, no de disco.
- **No era corrupción de escritura.** Se buscó entrelazado en las 23.491
  entradas del ledger más grande (cabeceras dentro de un bloque de código
  abierto, paridad de vallas rota): cero casos. El `flock` del escritor
  validador cierra un riesgo teórico, no repara un daño observado.
- **No era el repositorio git.** Los cientos de MB de `.git` del ledger más
  grande son objetos sueltos; los arregla un `git gc`, no una arquitectura
  nueva.

## Lo que destapó al indexar: la cadena de hashes

Se probó, se midió y se retiró. Merece contarse entera porque es la lección
más cara de la sesión de diseño:

1. La primera versión del trocador reconocía cabeceras `## ` y la flota
   escribe `### [`. Selló 2.076 de 37.259 entradas —el 5,6%— y el aviso de
   arranque decía «cadena íntegra» sin decir el denominador. En el segundo
   ledger en volumen selló 1 de 13.091.
2. Arreglado el trocador, subió al 99,98%. Pero la revisión adversarial
   encontró que **la cadena era inerte**: `verify` recalculaba
   `sha256(prev + sha_guardado)` — la misma fórmula sobre los mismos datos
   que usó el sellador — así que cuadraba siempre pasara lo que pasara en el
   markdown. Todo el poder de detección venía de comparar el hash vivo
   contra el guardado. Sólo podía cazar corrupción del propio SQLite.
3. Y al probar el escenario de equipo, resultó además **incorrecta por
   diseño**: el merge de git conserva las entradas pero no el orden, y una
   cadena por posición reporta eso como manipulación masiva.

Para el ledger compartido, la cadena la pone git: cada commit apunta a su
padre y al hash del árbol, y se firma por persona con `ssh-keygen -Y sign`
sin instalar nada. Ojo con lo que git tampoco da solo: un `push --force`
reescribe la rama igual (comprobado). Eso lo impide una regla de rama
protegida en el servidor, no este servicio.

Lo que sí se comprueba aquí es la invariante que git no cubre: un ledger de
sólo-apéndice no pierde entradas. Cada entrada se identifica por el hash de
su contenido; si una que estuvo deja de estar, se marca `ausente` — no se
borra la fila, o se borraría el hallazgo con ella — y `verify` la nombra con
su línea y su cabecera.

**La deuda de tipado, por campo**, sobre los seis ledgers del despliegue de
origen:

| Ledger | entradas | sin tipo | sin destinatario | sin hora |
|---|---:|---:|---:|---:|
| Ledger 1 (el mayor) | 23.491 | 75% | 30% | 12% |
| Ledger 2 | 13.094 | 99% | 96% | 11% |
| Ledger 3 | 173 | 96% | 9% | 1% |
| Ledger 4 | 22 | 100% | 100% | 36% |
| Ledger 5 | 51 | 50% | 1% | 9% |
| Ledger 6 (spoke) | 370 | 17% | 1% | 53% |

El ledger 6 (el spoke) es el único cerca del contrato, y es el único que
tenía el protocolo de escritura documentado desde el principio.

## Decisiones de diseño, cada una con su motivo

**Índice derivado, no fuente de verdad.** No guarda nada que no esté en el
markdown. La base de datos se puede borrar entera y se reconstruye en ~12 s.
Eso es lo que hace el rollback trivial: apagar el servicio y todo el mundo
sigue con `tail` y `>>`.

**Se valida en el borde de INDEXADO, no solo en el de escritura.** Idea
tomada de `buzz-relay` (ver `NOTICE`): repite el mismo chequeo en dos sitios
a propósito, porque una ruta se salta el pipeline principal. Aquí igual y
peor: los appends reales van ~134 transcripciones por `>>` crudo contra ~16
por el escritor validador. Si el único validador fuese la ruta de escritura,
el 89% del tráfico entraría sin mirar y el servicio se vería impecable sin
validar casi nada. El endpoint de lint valida lo que se indexa, venga por
donde venga.

**Sondeo, no inotify.** La primera hipótesis —«inotify no atraviesa el
montaje de macOS»— resultó **falsa** al medirla: los eventos del host sí
llegan en Docker Desktop reciente. Se sondea igualmente por tres razones que
sí se sostienen: un bind-mount de fichero se ata al inodo y una sustitución
deja de emitir eventos para siempre; a 2 s de latencia sobre ~2
entradas/minuto no hay nada que ganar; y no depende de una conducta de
virtiofs que cambia entre versiones de Docker Desktop.

**Montajes fichero a fichero y `:ro`.** Montar el directorio home metería
claves SSH, configuración de otros agentes y almacenamiento en la nube
personal dentro del contenedor para ahorrar unas líneas de YAML. Verificado
en vivo: un intento de escribir contra un montaje de sólo lectura lo
rechazó el propio kernel con `Errno 30`.

**Token obligatorio, y el binding a loopback NO era suficiente.** La primera
versión razonaba que publicar sólo en `127.0.0.1` aislaba el servicio. Es
falso en Docker Desktop para macOS, y está comprobado en vivo: un contenedor
en una red Docker sin ninguna relación declarada con este servicio llegó al
endpoint de estado por `host.docker.internal` con HTTP 200 y leyó el canon
entero. En la máquina de origen corrían a la vez el stack de una herramienta
de gestión de proyectos de terceros (8 contenedores), otro servicio interno
con datos financieros y su base de datos. Ahora todo el API va tras un token
de cabecera (permisos 0600 en disco) y falla cerrado: sin token no se sirve
nada. Es la elección correcta precisamente porque el servicio caído no
bloquea a nadie — un servicio mudo cuesta un `tail`; uno abierto cuesta el
canon de coordinación entero. Sólo el endpoint de salud queda sin token, y
por eso ya no devuelve datos (antes servía rutas y tamaños de los 6
ledgers).

**`GET /inbox` no avanza el cursor.** Lo hacía, y era un verbo `safe` por
especificación HTTP mutando estado: desplazaba el cursor de cualquier agente
nombrado en la URL sin comprobar que quien llama fuese ese agente. Una
imagen `<img src="…/inbox/agente-x">` en cualquier página abierta en la
misma máquina le habría vaciado la bandeja a otro agente, y un GET no
dispara preflight CORS, así que el token de cabecera tampoco lo habría
salvado. Marcar leído es ahora un POST explícito con el punto exacto hasta
el que se leyó — y si la sesión se cae entre leer y marcar, se pierde un
ciclo, no un mensaje.

**El contenido servido lleva marca de no confiable.** Los ledgers los
escriben LLMs con texto libre, y el endpoint de bandeja es el único punto
donde ese texto llega automáticamente a otro LLM sin que nadie lo ojee. No
es un agujero nuevo — el `tail` crudo ya lo tenía — pero sí el primer sitio
donde se centraliza el aviso.

**Un indexador muerto no se reporta sano.** Un fallo en la guarda de
rotación dejó el barrido muerto en bucle mientras el endpoint de salud
seguía diciendo «ok» y los appends nuevos no entraban. Ahora el estado real
del indexador sube al endpoint de salud.

## Falsadores (los doce corridos sobre el despliegue de origen, 2026-07-27)

| # | Qué prueba | Resultado |
|---|---|---|
| T1 | el contenedor ve appends del host | detectados a 0,5 s; inotify **sí** atraviesa el montaje (la hipótesis inicial estaba equivocada) |
| T2 | el ingest cuadra con un recuento independiente | 6/6 ledgers exactos vs `grep`; bytes host = bytes contenedor |
| T3 | coste de una consulta tipada vs `tail` | 27.983 → 2.778 tokens |
| T4 | 40 appends concurrentes | 40/40, vallas balanceadas, 0 entrelazado, 100% tipadas |
| T5 | matar el servicio no rompe a nadie | `tail`, `grep` y el escritor validador intactos; reinicio en 2 s |
| T6 | **el verde puede ponerse rojo** | una palabra cambiada en una entrada sellada → la nombra por entrada, línea y cabecera |
| T7 | rotación / truncado del fichero | **falló** — índice fosilizado sirviendo entradas inexistentes. Rehecho con identidad por contenido |
| T8 | contenedor ajeno sin token | 401 (antes: 200 con el canon entero) |
| T9 | el GET de la bandeja no muta | dos GET seguidos devuelven lo mismo; sólo el POST avanza |
| T10 | cabecera anormalmente larga | rechazada (422) |
| T11 | **dos humanos, un ledger, git de por medio** | la bandeja trae las entradas dirigidas pese al reordenado del merge; una nueva aparece sola |
| T12 | borrado de entradas tras el merge | las nombra una a una, con cuándo se vieron y cuándo se fueron |

T6 es el que importa: un verde que no puede ponerse rojo no es una
comprobación.

Tres de los seis primeros falsadores pasaron a la primera y dieron
**confianza falsa**: la revisión adversarial encontró después dos bugs
reales (fosilización del índice por rotación, bloqueo del event loop de
poco más de 5 s) que ninguno de los seis primeros ejercitaba. Y al
arreglarlos se introdujeron dos más, cazados por los falsadores nuevos: un
error en la propia guarda de rotación (una consulta SQL no traía la columna
que el código esperaba leer) y un testigo de rotación basado en el hash de
la última entrada — que cambia en cada append y habría forzado un
reindexado completo cada 2 s.

## Equipo: varios humanos, cada uno con sus agentes

Probado con dos clones de git y cuatro agentes escribiendo a la vez. Funciona,
y obligó a rehacer tres supuestos que sólo se sostenían con una máquina:

**El número de entrada no sirve como identidad.** El merge de unión
(`*.md merge=union` en `.gitattributes`) conserva las entradas intactas y
produce el fichero byte a byte idéntico en todas las máquinas, pero no el
orden: las entradas ajenas aterrizan por delante y una entrada tardía cayó
en mitad del fichero tras el merge. De ahí la identidad por contenido y el
cursor por orden de llegada local — una entrada mezclada en medio recibe una
llegada nueva y aparece en la bandeja de quien no la había visto.

**El censo tenía que salir del código.** La lista de agentes era una
constante en el propio código: con un humano vale, con varios cada alta
obliga a redesplegar. Ahora es un fichero JSON (`roster.json`, ignorado por
git — cópialo de `roster.example.json`), con el humano responsable de cada
agente y un campo de clave vacío — ahí entra la firma por agente el día que
el ledger tenga que servir de evidencia y no sólo de coordinación, sin
rehacer el formato ni reindexar.

**Y el montaje depende de cómo se escribe cada ledger.** Git no apendiza:
escribe un fichero nuevo y lo renombra encima. Un bind-mount de fichero se
ata al inodo, así que tras cada `git pull` el contenedor podía seguir
leyendo el fichero viejo — el host tenía más entradas de las que el
contenedor veía. Los ledgers que viven en un repo de git van montados por
directorio; los que sólo se escriben con `>>` (que conserva el inodo) van
fichero a fichero, para no meter el directorio home entero en el
contenedor. El detector de deriva de montaje compara bytes host↔contenedor
como red de seguridad.

Lo que falta no es código: dónde vive el repositorio compartido, y si cada
persona corre su propio servicio contra su copia local (lo simple, y lo que
está probado) o hay una instancia central. La regla de rama protegida que
impide el `push --force` la pone el servidor git, no este servicio.

## Por qué no va dentro de un gestor de proyectos

Se planteó integrarlo en un PM y se midió antes de decidir: de las 37.260
entradas del despliegue de origen, 83 son compromisos reales (tipo
"solicitud" + tipo "retenido") — el 0,2%. El 41% son latidos y el 14%
avisos. Un PM modela compromisos: algo con dueño, estado y un «hecho». El
ledger modela enunciados: cosas que se dijeron y que no se cierran nunca.
Integrarlos habría creado 37.000 tareas de las cuales 83 son tareas de
verdad.

El puente útil, si algún día se construye, va al revés y es pequeño: esos 83
compromisos se convierten en tareas del PM —que es lo único que aporta lo
que aquí falta, un estado de cerrado— y los otros 37.177 se quedan donde
están. No antes de que la disciplina de tipado suba de forma sustancial
sobre el ~40% actual.

## La cola de destilación: por qué el registro va al ledger

El destilador no es un modo del servicio: es un agente del censo con bandeja
propia. Los demás le DIRIGEN lo que merece ser canon; nadie clasifica 37.000
entradas. El mecanismo ya existía entero — sólo faltaba una pieza.

### `escucha`: leer un flujo sin consumirlo

Un agente puede recibir, además de lo suyo, el correo dirigido a otro nombre.
Parece un alias y no lo es: **el cursor sigue siendo suyo**. El destilador lee
las 751 entradas dirigidas a `wiki-vault` sin vaciarle la bandeja a la sesión
que responde como `wiki-vault`.

Sin ese campo sólo quedaban dos salidas, las dos malas: compartir cursor (los
dos lectores se consumen el correo) o nombrar al destilador explícitamente en
cada entrada — o sea pedirle a los demás que cambien cómo escriben, que es lo
único que este proyecto promete no hacer.

No tiene nada de específico de destilar. Cualquier segundo lector de un flujo
—un auditor, un resumidor, quien sustituye a alguien de vacaciones— es la
misma forma.

### El registro va al ledger, no a esta base de datos

Cerrar un ítem es una línea apendizada al ledger:

```
### [destilador → wiki-vault · INGESTED] <sello> — destilada
[destilado: <eid> → <kb>:<ruta>]
```

Tres razones, y la tercera es la que decide:

1. Todo lo demás de esta base se reconstruye del markdown en 2,2 s. Una tabla
   de destilados sería el único dato no reconstruible: el único que se pierde
   de verdad si alguien tira el volumen de Docker.
2. El ledger va en git, así que la procedencia hereda su cadena de hashes y la
   firma por persona, sin que este servicio custodie nada.
3. Es la tesis del producto aplicada a sí mismo. Si el destilador necesitara
   una base aparte para dejar constancia de su trabajo, «el markdown que ya
   escribís es el canon» sería falso justo donde más se mira.

La forma `[destilado: <eid> → NO: motivo]` cierra lo que se juzgó y NO es
canon. Sin ella un acuse de rutina no puede salir nunca de la cola, la cola no
baja de cero y se convierte en una luz roja permanente — que se aprende a
ignorar, como cualquier alarma que no se puede apagar. El juicio «esto no va a
la wiki» es un resultado del trabajo, no su ausencia.

Se empareja por PREFIJO del `eid`: quien cita escribe 12 caracteres, no 64.
Con igualdad exacta lo ya hecho volvería a salir como pendiente, que es el
fallo caro — trabajo repetido y dos páginas del mismo hecho.

### El apunte no puede alimentar su propia cola

La entrada que registra un destilado va dirigida a `wiki-vault` —tiene que ir,
es el acuse— así que **vuelve a entrar como pendiente**. La cola se alimentaba
sola y nunca bajaba de uno. Salió en la primera pasada del falsador, con la
cuenta de destiladas ya en 1: la marca se leía perfectamente y aun así el
pendiente no bajaba.

El criterio para excluirla es LLEVAR LA MARCA, no llamarse destilador: quien
firme el apunte da igual, y así el mismo patrón decide las dos caras sin
inventar un segundo concepto que se pueda desincronizar del primero.

### Un nombre del censo es un espacio de nombres

Se dio de alta al destilador como `canon`. La primera indexación le atribuyó
**46 entradas** — todas prosa: «STRING CANON FINAL», «tu canon de las 12:16».
La palabra sale 3.906 veces en el corpus y nadie le había escrito nunca.
`destilador` sale cero.

`/lint` delata ahora esa clase de nombre por la RAZÓN entre menciones dentro
del texto y usos como actor o destinatario. Un agente de verdad recibe correo
en proporción a lo que se le nombra; una palabra corriente se nombra
muchísimo y no recibe nada. No hace falta diccionario y funciona en cualquier
idioma — que es lo que se necesita en un repo público.

La primera versión de esa guarda acusó a un principal humano de tener 0 entradas
dirigidas siendo el destinatario número uno con 15.174: el índice guarda el
nombre canónico (en mayúsculas) y la comparación usaba la caja del censo. El
detector tenía el defecto que perseguía. Los destinos de difusión («equipo»,
«FLOTA») son palabras corrientes A PROPÓSITO y se excluyen — diciendo cuántos.

### Lo que se decidió no indexar, con la medida que lo sostiene

La cola manual de destilación de esta flota (`WIKI-QUEUE.md`) no se indexa.
Medido con el troceador real sobre sus diez versiones en git: 90,3% de las
entradas idénticas entre versiones, 6,9% CRECIERON —el cierre se apendiza, así
que el texto viejo es prefijo del nuevo— y 2,8% son ediciones de verdad.
Indexarla dispararía la alarma de «entrada desaparecida» en cada cierre, y una
alarma que salta con el uso normal enseña a ignorar la alarma.

El dato que importa es otro: había 751 entradas dirigidas a `wiki-vault`
dentro de los ledgers contra 167 ítems abiertos en el fichero de cola. **La
cola ya existía dentro del ledger.** El fichero aparte es el artefacto
compensatorio, igual que `tail -N`.
