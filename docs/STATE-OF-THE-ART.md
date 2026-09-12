# Estado del arte — kernel de coordinación para flotas de agentes

Fecha de corte: **2026-09-05**. Decisión objetivo: llminbox v0.9–1.0.

## Resumen ejecutivo

La oportunidad de llminbox no es construir otro chat, otro framework de agentes ni
otro motor de workflows. Es ocupar la capa que esos productos dejan entre sí: un
**kernel local-first de coordinación responsable**, independiente del modelo y del
harness, que atribuye cada escritura a una identidad de workload, conserva
causalidad e idempotencia, arbitra un único propietario activo, permite buscar
ledgers grandes y produce evidencia operacional.

La tesis de categoría es:

> Los frameworks deciden cómo piensa y actúa un agente; las interfaces deciden cómo
> conversa; los buses deciden cómo viaja un mensaje; llminbox debe decidir **quién
> tenía autoridad para hacer qué, en qué carril, con qué resultado durable y cómo se
> demuestra después**, sin obligar a abandonar los ledgers Markdown existentes.

Esta posición es estrecha y defendible. También impone una frontera: llminbox no
debe convertirse en el runtime que ejecuta agentes, en el producto de chat donde
viven humanos, ni en un broker distribuido generalista. Debe poder integrarse con
todos ellos.

## Cómo leer la evidencia

Cada comparación distingue deliberadamente:

- **HECHO DE FUENTE**: afirmación verificable en documentación o repositorio oficial.
- **HECHO LOCAL**: comportamiento o contrato documentado dentro de este repositorio.
- **INFERENCIA**: conclusión de producto o arquitectura de este análisis; no se
  atribuye al proyecto comparado.
- **NO VERIFICADO**: ausencia de evidencia suficiente. No significa que una
  capacidad no exista.

No se usan estrellas, número de contribuidores ni mensajes comerciales como prueba
de corrección. Tampoco se infiere una garantía de entrega, autoridad o aislamiento
a partir de que un producto emplee palabras como “agent”, “enterprise” o
“production-ready”.

## La categoría, separada en cinco capas

### 1. Espacios de colaboración humano–agente

Buzz y LobeHub hacen visible y usable la colaboración. Resuelven conversación,
canales, clientes y experiencia humana. Son referencias de interfaz e
interoperabilidad, no sustitutos automáticos del contrato de coordinación de
llminbox.

**INFERENCIA:** competir contra su superficie completa diluiría el producto. El
valor propio está debajo de la UI: identidad de workload, autoridad por verbo y
carril, recibos, leases, búsqueda y evidencia exportable.

### 2. Runtimes y frameworks multiagente

LangGraph, AutoGen y CrewAI describen agentes, grafos, equipos y ejecución. Su
unidad principal es el run, thread, graph, crew o task de una aplicación construida
con su SDK.

**INFERENCIA:** llminbox debe observar y coordinar runtimes heterogéneos sin pedir
que una flota sea reescrita en uno de esos frameworks. Un adaptador puede traducir
sus eventos al contrato llminbox; el modelo de autoridad no puede depender del
framework cliente.

### 3. Gateways y harnesses de agentes

OpenClaw integra runtime, workspaces, sesiones, canales y routing en un Gateway;
Buzz ofrece un harness ACP para conectar agentes al relay. Son referencias directas
para adaptadores, bootstrap y aislamiento de workspace.

**INFERENCIA:** llminbox debe aceptar que el runtime y el harness cambien. Debe
autenticar la instancia que cruza el gateway, no creer el nombre, rol o carril que
el agente escriba en prosa o en un payload.

### 4. Motores durables y buses de mensajes

Temporal y NATS JetStream aportan patrones maduros: historia durable, replay,
consumidores, acknowledgements, redelivery y control explícito de trabajo. Resuelven
un problema infraestructural más ancho que llminbox.

**INFERENCIA:** debemos adoptar sus invariantes, no necesariamente sus dependencias.
SQLite más un journal transaccional es una base proporcionada para el piloto de un
nodo. Migrar a un broker o motor externo solo se justifica con límites medidos de
volumen, disponibilidad o topología.

### 5. Coordinación Markdown/Git

tick-md y proyectos afines demuestran la demanda por coordinación legible,
local-first y portable. Git aporta historia y revisión, pero la coordinación de una
flota viva necesita además consumo incremental, identidad de ejecución, exclusión
temporal y latencia inferior al ciclo commit/pull/merge.

**INFERENCIA:** Markdown debe seguir siendo puente y exportación, no la autoridad
operacional de los verbos graduados a nativo. Esta separación permite adopción sin
migración abrupta y evita fingir que una cabecera de texto autentica a su autor.

## Comparación respaldada por fuentes

| Producto o estándar | Capa y hechos de fuente | Inferencia para llminbox | Decisión |
|---|---|---|---|
| **llminbox (base v0.9)** | **HECHO LOCAL:** indexa ledgers Markdown, ofrece inbox/cursor y conserva Markdown como fuente y fallback en la base actual. El plan v0.9 limita el producto a autoridad nativa, búsqueda, sensores, piloto Docker y release; no incluye chat nuevo, Kubernetes, vector DB ni framework generalista ([README](../README.md), [plan v0.9](V0.9-EXECUTION.md), [ADR de autoridad](ADR-001-NATIVE-AUTHORITY.md)). | El diferenciador no es “tener inbox”; es combinar compatibilidad incremental con garantías operacionales verificables. | **Construir** el kernel estrecho. |
| **Block Buzz** | **HECHO DE FUENTE:** su arquitectura incluye relay Nostr, auth, pub/sub, búsqueda, auditoría, workflow, clientes y superficie ACP. Los eventos Nostr se firman; el harness recomienda un keypair separado por agente ([VISION](https://github.com/block/buzz/blob/main/VISION.md), [AGENTS](https://github.com/block/buzz/blob/main/AGENTS.md), [buzz-acp](https://github.com/block/buzz/blob/main/crates/buzz-acp/README.md)). | Es el referente más cercano para identidad criptográfica, CLI agent-first y UX de colaboración. Su relay es el sistema de registro; adoptarlo como producto exige mover la conversación al relay, mientras llminbox debe poder aterrizar sobre ledgers existentes. | **Aprender e integrar**, no clonar ni sustituir. Mantener atribución en `NOTICE` cuando se transfiera una idea concreta. |
| **OpenClaw** | **HECHO DE FUENTE:** un Gateway puede alojar varios agentes aislados, cada uno con workspace, estado, auth y sesiones; los bindings enrutan canales/cuentas a agentes. Documenta procedencia de creación y árbol de agentes. El sandbox es opcional y no incluye el propio Gateway ([multi-agent routing](https://github.com/openclaw/openclaw/blob/main/docs/concepts/multi-agent.md), [runtime](https://github.com/openclaw/openclaw/blob/main/docs/concepts/agent.md), [sandboxing](https://github.com/openclaw/openclaw/blob/main/docs/gateway/sandboxing.md)). | Referencia para separación workspace/estado/routing y para una futura integración Gateway. No prueba por sí sola recibos idempotentes, fencing o autoridad por `(carril, verbo)` compatibles con el contrato llminbox. | **Integrar por adaptador**. No absorber runtime, prompt assembly, canales ni sandbox. |
| **LobeHub / LobeChat** | **HECHO DE FUENTE:** se presenta como espacio para crear y colaborar con agentes; incluye conversación ramificada, knowledge base, proveedores múltiples, plugins/MCP y self-hosting ([repositorio oficial](https://github.com/lobehub/lobe-chat)). | Es referencia de experiencia de usuario y ecosistema, no evidencia de un journal de autoridad operacional. **NO VERIFICADO:** garantías equivalentes a receipts, fencing o aislamiento por carril. | **Compatible aguas arriba** mediante API/MCP; no competir en UI generalista en 1.0. |
| **LangGraph** | **HECHO DE FUENTE:** runtime de orquestación de bajo nivel con durable execution, streaming, human-in-the-loop y persistencia. Sus checkpointers guardan estado por thread y soportan recuperación y time travel; su propia guía exige diseñar side effects idempotentes ante reejecución ([overview](https://docs.langchain.com/oss/python/langgraph/overview), [persistence](https://docs.langchain.com/oss/python/langgraph/persistence), [Functional API](https://docs.langchain.com/oss/python/langgraph/functional-api)). | Su checkpoint resuelve estado de una ejecución, no sustituye la autoridad cross-runtime de una flota heterogénea. `thread_id` tampoco debe confundirse con principal o instancia autenticada. | **Adaptar eventos**, reutilizar patrones de checkpoint/idempotencia; no adoptar el grafo como núcleo. |
| **Microsoft AutoGen Core** | **HECHO DE FUENTE:** usa un modelo actor, mensajería asíncrona, topics/subscriptions, identidad/lifecycle y telemetría. Su runtime distribuido tiene host y workers, pero la documentación estable lo marca explícitamente como experimental y sujeto a breaking changes ([Core](https://microsoft.github.io/autogen/stable/user-guide/core-user-guide/index.html), [distributed runtime](https://microsoft.github.io/autogen/stable/user-guide/core-user-guide/framework/distributed-agent-runtime.html)). | Buen patrón para mensajes tipados y frontera host/worker. La experimentalidad del runtime distribuido desaconseja acoplar el contrato público de llminbox a él. | **Adaptador opcional**; no dependencia del kernel. |
| **CrewAI** | **HECHO DE FUENTE:** modela agentes con roles, Crews, Tasks y procesos; Flows aporta control event-driven, estado, persistencia y resumibilidad. Su oferta gestionada añade despliegue y trazas ([documentación](https://docs.crewai.com/), [conceptos](https://docs.crewai.com/core-concepts/Agents), [AMP](https://docs.crewai.com/enterprise/introduction)). | “Rol” descriptivo no equivale a principal autenticado. Puede aportar eventos de ejecución, pero llminbox debe derivar identidad y política del servidor. | **Integrar como productor**, nunca aceptar rol/carril autodeclarado como autoridad. |
| **Temporal** | **HECHO DE FUENTE:** registra Commands y Events en una Event History que actúa como fuente de verdad; reconstruye estado por replay determinista y separa side effects externos en Activities ([Workflows](https://docs.temporal.io/workflows)). | Es la referencia fuerte para causalidad, historial y recuperación. Incorporarlo ahora sobredimensionaría un piloto single-node y convertiría llminbox en un workflow engine. | **Adoptar semántica**, evaluar integración futura para topologías distribuidas; no incluir en v0.9. |
| **NATS JetStream** | **HECHO DE FUENTE:** ofrece streams persistentes, consumidores pull, acknowledgement explícito, cursor y entrega por lotes o continua; la documentación trata tamaño de lote y trabajo perdido si muere el worker ([JetStream](https://docs.nats.io/learn/jetstream/), [pull consumers](https://docs.nats.io/learn/jetstream/pull-consumers)). | Buen referente para inbox durable, ACK y redelivery. Un ACK de transporte no demuestra que el efecto de negocio ocurrió una sola vez: llminbox necesita receipt e idempotencia en la misma transacción de dominio. | **Adoptar el modelo mental**; no introducir broker hasta que una prueba de carga/topología lo exija. |
| **tick-md** | **HECHO DE FUENTE:** coordina tareas con `TICK.md`, Git, CLI, dashboard y MCP; expone claim, dependencias, filtros y watch, con orientación local-first ([repositorio oficial](https://github.com/Purple-Horizons/tick-md)). | Confirma que Markdown/Git reduce lock-in y mejora inspección humana. Git commits no son por sí solos sesiones autenticadas, leases con fencing ni una cola de consumo de baja latencia. **NO VERIFICADO:** comportamiento bajo claims concurrentes o ledgers grandes. | **Mantener interoperabilidad textual**; comparar con falsadores antes de importar una primitiva. |
| **OpenTelemetry** | **HECHO DE FUENTE:** mantiene convenciones semánticas para GenAI, agent spans y MCP dentro de su especificación ([GenAI](https://opentelemetry.io/docs/specs/semconv/gen-ai/), [agent spans](https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-agent-spans/), [MCP](https://opentelemetry.io/docs/specs/semconv/gen-ai/mcp/)). | Es el estándar de salida adecuado, no la fuente de verdad de salud. Un proceso no debe declararse sano mediante su propia telemetría; los hechos durables y un supervisor externo deben sostener el estado. | **Integrar en M3**, versionar el mapping y aislar cambios de convenciones. |

## Decisiones al nivel de primitivas

La comparación de productos no basta para decidir qué garantiza el kernel. Estas
son las primitivas y el límite exacto que se adopta.

### Commit y semántica de entrega

- **HECHO DE FUENTE — SQLite:** en modo WAL, el commit queda representado por un
  registro de commit añadido al WAL; `synchronous=FULL` añade la sincronización del
  WAL después de cada commit y busca preservar durabilidad ACID frente a un corte
  eléctrico ([WAL](https://www.sqlite.org/wal.html),
  [`PRAGMA synchronous`](https://sqlite.org/pragma.html#pragma_synchronous)).
- **INFERENCIA DE PRODUCTO:** en v0.9, la respuesta exitosa de la transacción del
  journal bajo `WAL` + `FULL` es el **punto de linealización de la admisión**. Evento,
  idempotency key, receipt y fila de outbox deben quedar dentro de ese mismo commit.
  Ningún append Markdown, span ni ACK posterior puede redefinir si fue admitido.
- **HECHO DE FUENTE — versión SQLite:** la documentación de WAL registra un bug de
  carrera que afecta a versiones 3.7.0–3.51.2 bajo conexiones concurrentes y
  checkpoints; está corregido en 3.51.3 y en backports concretos
  ([WAL, §11](https://www.sqlite.org/wal.html#the_wal_reset_bug)).
- **INFERENCIA DE RELEASE:** M4/M5 deben atestiguar la versión SQLite del artefacto,
  exigir una versión corregida o backport documentado y ejecutar el falsador de
  concurrencia/checkpoint. Probar solo la versión del host no certifica la imagen.
- **INFERENCIA DE GARANTÍA:** llminbox promete **exactly-once admission** para la
  misma clave y el mismo cuerpo, no “exactly-once delivery” ni “exactly-once effect”.
  Proyecciones Markdown, exportación OTel y efectos externos son **at-least-once**;
  por eso necesitan identidad estable, reintento y consumidores idempotentes.

### Intención durable frente a side effects

- **HECHO DE FUENTE — Temporal:** la Event History registra eventos y comandos,
  funciona como fuente de verdad de una ejecución y permite reconstruir estado por
  replay; las interacciones con el exterior se encapsulan en Activities
  ([Event History](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/event-history/event-history.mdx),
  [Workflows](https://docs.temporal.io/workflows)).
- **INFERENCIA DE PRODUCTO:** el journal llminbox conserva intención, decisión y
  receipt; la outbox materializa efectos. Un crash entre ambos no cambia la decisión:
  deja trabajo reintentable. El patrón se adopta sin incorporar el runtime Temporal.
- **HECHO DE FUENTE — JetStream:** sus consumers mantienen estado de entrega,
  soportan acknowledgements y pueden redeliver cuando no llega el ACK
  ([fuente oficial](https://github.com/nats-io/nats.docs/blob/master/nats-concepts/jetstream/consumers.md)).
- **INFERENCIA DE PRODUCTO:** JetStream podría transportar entregas futuras, pero
  **nunca sería la autoridad** sobre principal, carril, fence o admisión. La autoridad
  permanece en el journal o en una implementación futura de su mismo contrato.

### Identidad, autorización y telemetría

- **HECHO DE FUENTE — SPIFFE:** define identidad de workload y documentos SVID,
  obtenidos a partir de attestation de nodo y workload
  ([conceptos oficiales](https://spiffe.io/docs/latest/spiffe/concepts/)).
- **INFERENCIA DE PRODUCTO:** SPIFFE/SPIRE es un candidato futuro para autenticar
  runtimes en topologías multinodo. La identidad autenticada sigue necesitando una
  política llminbox que la mapee a principal, rol, carril y verbos: **autenticación
  no es autorización**.
- **HECHO DE FUENTE — OpenTelemetry:** existen convenciones GenAI para spans de
  agente; la especificación también define señales generales de traces, metrics y
  logs ([agent spans](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-agent-spans.md),
  [GenAI](https://opentelemetry.io/docs/specs/semconv/gen-ai/)).
- **INFERENCIA DE PRODUCTO:** OTel es exportación observable, **nunca audit truth**.
  La verdad de admisión/denegación vive en el journal. Atributos de alta cardinalidad
  e inputs/outputs sensibles quedan fuera por defecto; el mapping usa allowlist,
  límites y versión explícita.

### Protocolos como adaptadores, no como núcleo

- **HECHO DE FUENTE:** CloudEvents especifica un envelope interoperable de eventos
  ([v1.0.2](https://github.com/cloudevents/spec/blob/v1.0.2/cloudevents/spec.md));
  MCP publica un protocolo versionado para intercambiar contexto y capacidades
  ([spec 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28)); A2A
  publica un protocolo de interoperabilidad entre agentes
  ([especificación](https://a2a-protocol.org/latest/specification/)).
- **INFERENCIA DE PRODUCTO:** CloudEvents, A2A y MCP son **adaptadores versionados**.
  Pueden transportar un evento o exponer una herramienta, pero no sustituyen receipt,
  idempotencia, lease/fence ni política por carril. Toda entrada se normaliza al
  contrato nativo y toda salida declara versión y pérdida de semántica, si la hay.

## Brechas que forman el producto

La comparación deja ocho brechas. Las seis primeras son el núcleo público; las dos
últimas evitan que el núcleo sea correcto pero impracticable.

1. **Identidad no autodeclarada.** Separar principal, rol, instancia y carril, y
   derivarlos de una credencial o workload identity validada por el servidor.
2. **Autoridad graduable.** Mover verbos a nativo por `(carril, verbo)` sin exigir
   una migración global ni permitir que el cliente se autopromueva.
3. **Entrega demostrable.** Un evento aceptado recibe identidad, causalidad,
   idempotency key y receipt durable; entrega, ACK y efecto son estados distintos.
4. **Propiedad exclusiva.** Un trabajo tiene un lease vigente y fencing monotónico;
   un dueño antiguo no puede escribir después de un takeover.
5. **Recuperación sin ambigüedad.** Retry y replay no pueden duplicar efectos ni
   revivir una orden superada por una revisión posterior.
6. **Lectura a escala y aislada.** FTS, filtros exactos, keyset pagination y cursores
   firmados sin cargar el ledger completo ni usar el texto buscado como frontera de
   seguridad.
7. **Salud observable desde fuera.** Distinguir proceso vivo, progreso, bloqueo,
   bucle y sensor mudo; limitar cardinalidad y contenido sensible.
8. **Adopción y salida.** CLI pequeña, Markdown import/export, Docker reproducible,
   migración reversible y evidencia de rollback.

## Fronteras de producto

### Adoptar dentro del kernel

- Journal append-only tipado con identidad, causalidad, revisión y supersession.
- Idempotencia de comando y mutación; receipt durable unido a la transacción de
  dominio.
- ACK por destinatario, outbox durable, reintentos acotados y estado poison/dead.
- Leases con caducidad y fencing monotónico.
- FTS5 con filtros SQL exactos, snippets, truncation explícita y keyset pagination.
- Sensores externos y export OpenTelemetry con vocabulario versionado.
- Migrations fail-closed, falsadores negativos y rollback con evidencia.

### Integrar, manteniendo una frontera

- **ACP/MCP/CLI/HTTP:** adaptadores finos; el transporte no decide identidad ni
  autoridad.
- **Buzz, OpenClaw y LobeHub:** destinos/orígenes de mensajes y superficies de
  operador; no bases de datos internas del kernel.
- **LangGraph, AutoGen y CrewAI:** productores de lifecycle/job/tool events y
  consumidores de comandos; nunca fuente de `principal`, `role` o `lane`.
- **OpenTelemetry Collector:** salida de traces, metrics y logs; una caída del
  exporter no revierte una mutación ya durable y sí expone degradación.
- **Temporal o NATS:** backends futuros detrás del contrato, si los benchmarks o la
  topología multinodo justifican el coste.

### Evitar explícitamente

- Un “Slack para agentes” o una nueva UI de chat como requisito del 1.0.
- Runtime de modelos, prompt builder, memoria cognitiva, marketplace de agentes o
  herramienta general de ejecución.
- Autoridad basada en nombre escrito, cabecera Markdown, prompt, claim del cliente o
  `traceparent`.
- Prometer “exactly once” para efectos externos. El contrato defendible es
  aceptación idempotente, estado durable y consumidores tolerantes a redelivery.
- Usar un LLM, una frase de ledger o la telemetría del propio agente como árbitro de
  salud, autorización o finalización.
- Vector DB por defecto para una necesidad que FTS5 y filtros exactos deben medir
  primero.
- Kubernetes, Redis, Postgres, NATS o Temporal en el camino mínimo del piloto sin un
  umbral medido que SQLite no cumpla.
- Cardinalidad no acotada o contenido de prompts/tools en métricas por defecto.
- Llamar “aislamiento” a routing de UI, nombres de canales o filtros de consulta.

## Consecuencias para M1–M5

| Hito | Implicación derivada del estado del arte | Evidencia de cierre |
|---|---|---|
| **M1 — autoridad** | Tomar de los sistemas de eventos la separación entre identidad, comando y resultado, pero hacer server-derived `principal/role/lane/runtime`. Añadir idempotencia, receipt, lease/fence, causalidad y supersession. Markdown queda `legacy_unverified` para verbos graduados. | `/whoami` no acepta identidad del body; credencial compartida no ejecuta verbos nativos; same-key/same-body devuelve el mismo receipt; same-key/different-body no muta; stale fence falla después de takeover; restart conserva estados. |
| **M2 — búsqueda** | Tratar búsqueda como índice descartable y no como autoridad. Inspirarse en FTS de relays sin adoptar su substrate. Mantener filtro exacto de carril/ledger fuera de `MATCH`; cursor HMAC ligado a filtros y generación. | Benchmarks reproducibles sobre ledger real congelado y corpus de 1M; recall fixture; límites p50/p95 publicados; mutantes de escape FTS, cross-lane, cursor adulterado y paginación sin huecos/duplicados. |
| **M3 — sensores** | Exportar OTel sin confundir observabilidad con verdad operacional. Gateway y supervisor externo emiten señales confiables; el agente no se autocertifica. Versionar atributos propios mientras maduran las convenciones GenAI. | KILL, SIGSTOP, proceso en loop, exporter caído, sensor mudo y restart producen estados distinguibles; cardinalidad está acotada; prompts y tool payloads no aparecen por defecto. |
| **M4 — Docker** | Aplicar la separación Gateway/workspace observada en OpenClaw y la frontera host/worker de AutoGen, con credencial por runtime. No montar ledgers en el contenedor del agente. | Entrega positiva y controles cross-lane/overmount/stale identity; recreate real, recuperación de crash, attestation ligada al contenedor nuevo y rollback binario+datos que no pierde outbox pendiente. |
| **M5 — release** | Publicar llminbox como kernel interoperable y pequeño, no como plataforma total. Hacer explícito qué se garantiza, qué queda legacy y cómo integrar runtimes/UI/brokers. | Protocolo versionado, threat model, guía de migración, matriz de compatibilidad, SBOM, imagen/dependencias reproducibles, changelog y falsadores ejecutados sobre artefacto de release. |

## Decisión de arquitectura resultante

Para v0.9/1.0, la arquitectura recomendada es:

1. **SQLite `coordination.sqlite` como autoridad single-node**, transaccional y
   migrable; el índice Markdown/FTS sigue siendo derivado.
2. **Gateway nativo pequeño** que autentica una credencial, emite una sesión y
   deriva principal, rol, runtime y carril.
3. **API/CLI tipada** para eventos, comandos, receipts, ACK, leases y búsqueda.
4. **Outbox/proyección Markdown** para compatibilidad y salida humana, sin devolver
   autoridad operacional al texto.
5. **Supervisor externo + OTel** para runtime health, dejando los hechos críticos en
   el journal aunque falle la exportación.
6. **Adaptadores**, no forks, para ACP/MCP, Buzz, OpenClaw, LangGraph, AutoGen y
   CrewAI.

La condición para revisar SQLite no es “tener muchos agentes”. Es demostrar uno de
estos límites: múltiples escritores en hosts distintos, objetivo de disponibilidad
que un nodo no satisface, throughput/latencia fuera del SLO medido, o necesidad real
de fan-out geográfico. Entonces se evaluará NATS/Temporal/Postgres detrás del mismo
contrato, sin cambiar la semántica pública.

## Riesgos y afirmaciones pendientes

- **LobeHub:** su superficie multiagente evoluciona con rapidez. Solo se verificó la
  descripción oficial de producto y repositorio; no se auditó su implementación de
  orquestación, durabilidad o aislamiento.
- **OpenClaw:** se verificaron routing, workspaces, sesiones, procedencia y límites
  declarados del sandbox; no se ejecutaron falsadores de claims concurrentes,
  receipts o cross-agent isolation.
- **tick-md:** se verificó la superficie publicada; no se benchmarkeó ni se probó
  concurrencia Git. No afirmar que “carece” de una garantía sin un test versionado.
- **AutoGen:** la propia documentación marca experimental el runtime distribuido;
  esta conclusión debe revisarse antes de publicar una integración soportada.
- **OpenTelemetry GenAI:** las convenciones y URLs han cambiado. El mapping de M3
  debe fijar versión y conservar un namespace propio para campos todavía no
  estables.
- **SQLite:** `WAL` + `FULL` define la semántica deseada, pero no basta sin fijar una
  versión libre del WAL-reset bug y verificar el VFS/filesystem del artefacto real.
- **NATS/Temporal:** esta investigación compara semántica documentada, no costes ni
  rendimiento en la carga BIK. Cualquier adopción requiere benchmark y runbook.
- **Buzz:** es la comparación más cercana, pero no se ejecutó un despliegue paralelo
  con la misma flota y corpus. Las diferencias operacionales son inferencias de
  arquitectura, no un benchmark comparativo.

## Alcance y método de la investigación

Investigación cerrada el **2026-09-05 (Europe/Madrid)**. Se consultaron fuentes
oficiales enlazadas directamente en este documento y los contratos locales de la
rama `codex/llminbox-v0.9` en `853507f`. La revisión fue documental; no incluyó
auditoría completa del código de terceros, pruebas de penetración, despliegues
comparativos ni mediciones de coste.

El universo no pretende cubrir los miles de repositorios etiquetados como
`multi-agent-systems`. La muestra es intencional y cubre un representante relevante
por capa: colaboración/relay (Buzz), UI (LobeHub), gateway/harness (OpenClaw),
frameworks (LangGraph, AutoGen, CrewAI), durable execution (Temporal), messaging
(NATS JetStream), coordinación textual (tick-md) y telemetría (OpenTelemetry).

Regla de actualización: antes de usar este documento para cambiar una dependencia o
publicar una comparación comercial, reabrir la fuente oficial afectada, fijar
versión/commit y convertir la afirmación crítica en un falsador local. Este texto es
un mapa de decisión, no una certificación permanente de terceros.
