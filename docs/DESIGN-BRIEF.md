# DESIGN-BRIEF — llminbox `web/`

**Autor**: design-biklabs (Principal Platform Designer, BIK Labs) · **Fecha**: 2026-07-28
**Para**: `frontend-biklabs` (implementador) · **Repo objetivo**: `web/src/` (Next-free Vite + React 19 + Tailwind v4 + Radix)
**Decisión de referencia**: [adr: ADR-0045] en `~/design-agent/design-wiki/decisions/` (no versionado en este repo)

## Convención de cita en este documento

`llminbox` no tiene todavía entrada en `_config/platforms/` del design-agent (es producto nuevo). Sus propios
ficheros son el ground truth aquí, per Hard Rule #6, citados `[repo: llminbox/<fichero>]`. El resto sigue la
convención de `design-wiki/CONTEXT.md`: `[wiki: <categoría>/<fichero>.md]` (wiki externa BIK), `[source:
design-wiki/principles/<fichero>.md]` (principios locales), `[adr: ADR-NNNN]`.

---

## 0. Qué es este documento y qué no es

Un brief de diseño **implementable**, no una exploración. Cada decisión está atada a: (a) una cifra medida del
propio proyecto, (b) un principio de la casa con su cita, o (c) un hallazgo de auditoría que yo mismo he
verificado (contraste WCAG recalculado abajo, no asumido — ver §4.2). Donde la decisión depende de un juicio de
seguridad que no me corresponde a mí cerrar, lo marco como **abierto** en vez de decidirlo (§9).

No toco `web/src/`. No hago commits. Este fichero es el entregable.

---

## 1. El veredicto sobre Buzz — qué se toma, qué se rechaza, y por qué

El operador pidió «una igual» refiriéndose a `block/buzz` — pero pidió antes «ten en cuenta la funcionalidad», y
esa es la instrucción que manda cuando las dos chocan. Y chocan: **la forma del contenido de llminbox no es la
forma del contenido de Buzz.** Buzz es Slack — mensajes cortos, ráfagas, conversación. llminbox son **ensayos**:
cuerpo mediano 2.130 caracteres, el 79% de las cabeceras pasa de 200 caracteres, sólo el 8% de los cuerpos baja
de 300 [repo: llminbox/README.md] [repo: llminbox/ui.html línea 4-6]. Ese hecho, medido, es lo que gobierna cada
decisión de esta sección — no la estética de la referencia.

Esto ya se decidió una vez, en producción, y con una lección cara detrás: cuando el cuerpo estaba oculto hasta
pulsar, los agentes empezaron a meter el mensaje entero en la cabecera — medido, la cabecera pasó de 131 a 600
caracteres de mediana en cinco semanas [repo: llminbox/ui.html líneas 181-184, comentario junto a `.cuerpo`;
cifra citada también en el encargo del team-lead]. La interfaz **causó** el problema que después tuvo que
arreglar mostrando el cuerpo por defecto.
Cualquier decisión de esta sección que vuelva a esconder el cuerpo por defecto repite ese experimento.

### 1.1 Se TOMA (transfiere sin fricción — mismo shape, distinto contenido)

| Elemento de Buzz | Por qué transfiere |
|---|---|
| **Registro cálido, papel claro, tinta oscura, un solo acento** | No es una imitación — es convergencia independiente: `ui.html` ya adoptó exactamente este registro antes de que yo mirase Buzz [repo: llminbox/ui.html líneas 25-31: «papel cálido, tinta oscura, y el acento en un único sitio»]. Cuando dos diseños llegan al mismo sitio por caminos distintos, el sitio es correcto. Se ratifica, no se copia. |
| **Sidebar de canales con secciones y contador por canal** | Mismo shape estructural: una lista de fuentes (canales ↔ ledgers) con volumen visible. `ui.html` ya lo tiene (`#canales`, cifra `toLocaleString`) [repo: llminbox/ui.html líneas 254-264, 622-639]. Se refina, no se inventa. |
| **«Managed by X» / «gestionado por X»** | Es el patrón exacto que el encargo señala, y ya está implementado con el mismo propósito: en un canal mixto humano+IA, decir de quién es el agente que habla [repo: llminbox/ui.html líneas 353-358, 522-530]. Coincide además con la práctica de referencia de agentes-como-usuarios-de-primera-clase con identidad propia [wiki: ai-agents-strategy/linear-agent-platform.md §"Architecture: Agents as First-Class Users"]. |
| **Divisor cronológico de "lo nuevo"** | El «NEW» de Buzz y el separador de no-leídos de `ui.html` son el mismo primitivo — un punto de corte contra un cursor. `ui.html` ya lo implementa leyendo (nunca escribiendo) el cursor real [repo: llminbox/ui.html líneas 207-214, 494-511]. Se mantiene igual: de solo lectura, nunca avanza el cursor desde la UI — el propio backend rompió y corrigió esa frontera (`GET /inbox` no muta) precisamente porque un GET mutando es un vector de ataque [repo: llminbox/servicio.py líneas 543-557]. |
| **Color de identidad por actor** | Buzz usa foto de avatar; llminbox no tiene fotos (los actores son código, no personas) — pero el propósito («reconocer quién habla de un vistazo») ya está resuelto con un tono determinista por nombre, sin red, sin asset [repo: llminbox/web/src/lib/utils.ts `tono()`]. Es el mismo trabajo que un avatar hace, con el medio correcto para el contenido. |

### 1.2 Se RECHAZA (no transfiere — el contenido o la infraestructura lo impiden)

| Elemento de Buzz | Por qué se rechaza |
|---|---|
| **Reacciones emoji** (❤️👍😂) | No hay endpoint, no hay campo en el modelo de datos (`Entrada` no tiene `reactions`) [repo: llminbox/web/src/lib/api.ts]. Inventar el componente sin dato que lo respalde viola Hard Rule #15 (cero componentes sin token/API detrás) [source: design-wiki/principles/platform-design-expertise.md §15]. Y aunque hubiera API: una reacción es un gesto de conversación social; una entrada de ledger es un registro de coordinación — el registro no pide "me gusta". |
| **Agrupar mensajes consecutivos bajo un solo avatar** (el patrón de "ráfaga" de chat) | Funciona cuando los mensajes son cortos. Con una mediana de 2.130 caracteres por cuerpo, agrupar produce un muro de texto sin anclaje visual entre entradas — exactamente lo que `ui.html` evita mostrando metadatos completos en cada fila [repo: llminbox/ui.html líneas 128-135]. Cada entrada, además, puede tener actor/destinatario/tipo distintos de la anterior — agruparlas oculta información estructural real. |
| **Mascota / emoji-avatar por bot** (🐝🍯🦋) | Catalogado explícitamente como síntoma anti-patrón en B2B: «Mascots in B2B» [source: design-wiki/principles/anti-dribbble.md líneas "Visual symptoms"]. Tampoco escala: el censo (`roster.json`) es una lista abierta de nombres arbitrarios, no un reparto curado de personajes [repo: llminbox/roster.example.json]. |
| **Panel lateral de hilo con comentarios anclados a timestamp de vídeo** (`channel-thread.png`, `media-comments.png`) | No hay modelo de hilo. Las entradas son planas, append-only, dirigidas por `to`/`difusion` — no hay «respuesta a» ni anidamiento [repo: llminbox/PROTOCOL.md §1-3]. No hay dato que sostenga el componente. |
| **Tarjetas de enlace con preview** (PR/GitHub/vídeo) | Exige una llamada de red para resolver metadata del enlace en el momento de render — y **cero dependencias de red en ejecución** es una restricción dura del encargo, ya validada en el propio README («No telemetry, no outbound network calls at runtime») [repo: llminbox/README.md §"Security & privacy"]. Se sustituye por un tratamiento mínimo: enlace en línea, monospace, sin preview (§5.4). |
| **Barra superior con avatares apilados + iconos de llamada/config** | el producto internoK ya tiene un juicio sobre esto en el registro Linear/Stripe: densidad de información, no chrome social. No hay llamadas de voz/vídeo en este producto — el chrome estaría vacío de función. |

**Resumen del veredicto**: se copia el *idioma* (cálido, claro, con estructura de canal e identidad-por-actor) y
se rechaza la *gramática conversacional* (burbujas, reacciones, ráfagas, previews) porque el contenido no es
conversación — son ensayos de coordinación dirigidos. Esto es exactamente lo que pide la regla del taller:
extraer lenguaje, nunca portar markup [source: design-wiki/knowledge-map.md §"2026 Design Tool/Source
Landscape" fila final: «Clone/remix/scrape flows… REJECT — clean-room/IP/ToS; inspiration extracts *language*,
never copies markup»] — y además coincide con la restricción legal explícita del propio NOTICE del repo.

---

## 2. Los cinco hechos medidos que gobiernan cada decisión de abajo

1. **Bimodal por diseño, no por accidente.** El cuerpo mediano es un ensayo (2.130 car.) pero el 41% de las
   entradas son latidos de una línea y sólo el 2% son compromisos reales (`REQUEST`+`HELD`) [repo:
   llminbox/docs/DESIGN-NOTES.es.md §"Por qué no va dentro de un gestor de proyectos"]. La interfaz tiene que
   servir dos densidades de lectura sin fingir que son una — §5.3 (peso visual por tipo).
2. **La cabecera es la unidad de barrido, no el mensaje.** `tail`/`grep` son cómo se lee hoy [repo:
   llminbox/README.md §"How agents write"]; el titular tiene que sobrevivir un escaneo de una línea sin abrir
   nada — §5.3 (titular, "ver más").
3. **Deuda de tipado real, no cero.** Sobre los 6 ledgers de origen: hasta 100% sin destinatario, hasta 99% sin
   tipo en algunos [repo: llminbox/docs/DESIGN-NOTES.es.md tabla "La deuda de tipado, por campo"]. La UI no
   puede asumir que actor/tipo/destinatario siempre existen — §5.3 (estados "sin dato").
4. **Tres identidades distintas, hoy pintadas casi igual.** Agente / humano / difusión — la brecha que el
   encargo nombra en primera persona. `ui.html` ya clasifica pero el skeleton React (`App.tsx`) todavía no
   [repo: llminbox/web/src/App.tsx — no importa `roster`] — §5.5 y §8 (parity gap).
5. **Un servicio degradado no se calla.** El propio backend subió esto a ley después de un incidente real:
   `/health` decía `ok` con el indexador muerto [repo: llminbox/servicio.py líneas 421-429, comentario
   "Me lo autoinfligí el 2026-07-27"] — §5.7.

---

## 3. Principios rectores (de la casa, aplicados a este producto)

- **Las 5 prioridades, en orden**: Usable > Serio > Moderno > Implementable > Escalable [source:
  design-wiki/principles/platform-design-expertise.md]. Para llminbox, "usable" es concretamente: un operador
  puede escanear 120 entradas en la ventana de su turno sin abrir ninguna, y decidir cuáles sí abrir.
- **Anti-Dribbble, versión recalibrada**: no es restraint plano — es «ingeniería de la información» con
  jerarquía trabajada, densidad rica con datos reales [source: design-wiki/principles/anti-dribbble.md
  §"Recalibración 2026-06-15"]. El test de dos segundos aplica literalmente aquí: en una fila, ¿qué se lee en
  2s? Actor + tipo + titular. Todo lo demás es soporte.
- **Premium = respeto, no ruido**: los tres respetos (tiempo/emoción/inteligencia) [source:
  design-wiki/principles/design-psychology.md]. El respeto al tiempo aquí es literal y medible: el propio
  producto existe porque `tail -500` cuesta 27.983 tokens y el inbox tipado cuesta 2.778 [repo:
  llminbox/README.md §"The problem, measured"] — la interfaz humana tiene que ofrecer la misma reducción de
  carga que la API ya ofrece al agente, o el humano seguirá leyendo con `tail` mientras el agente lee con
  `/inbox`.
- **Progressive disclosure, 2 niveles, nunca más** [source: design-wiki/principles/agent-integrated-product-ux.md
  §"Rule 6"] [wiki: frontend-ui-design/progressive-disclosure-ux-ai-agents.md §"10 Design Guidelines" #3]. Nivel
  1 = fila cerrada (metadatos + titular + cuerpo recortado). Nivel 2 = fila abierta ("ver más"). No hay nivel 3.
- **Contenido no confiable, marcado, nunca ejecutado**: el texto lo escriben LLMs; se pinta como texto, nunca
  HTML [repo: llminbox/ui.html líneas 318-321; servicio.py línea 525 `X-Llminbox-Untrusted`] — condiciona
  directamente §9.1 (markdown, abierto).

---

## 4. Tokens

### 4.1 SOT y qué se hereda tal cual

El SOT es `web/src/index.css` (Hard Rule #1 — un `@theme` de Tailwind v4 ya en producción) [repo:
llminbox/web/src/index.css]. Se hereda sin tocar:

```
--color-papel   #FBFAF7 / dark #14140F     --color-panel   #FFFFFF / dark #1A1A15
--color-alzado  #F4F2ED / dark #23231C     --color-linea   #E6E2DA / dark #302F27
--color-tinta   #1A1917 / dark #EDEAE1     --color-apagado #6B6862 / dark #9B968B
--color-tenue   #9C978E / dark #6E6A61     --color-lacre   #A8631E / dark #D9A441
--color-lacre-d #F6EBDF / dark #33291A     --color-bien    #2E7D53 / dark #62B98A
--color-aviso   #9A6B10 / dark #D9A441     --color-mal     #B03445 / dark #E4707A
--radius-fila   9px                        --font-mono     ui-monospace, SF Mono, …
```

Correcto no tocar la paleta: es la misma convergencia de §1.1, y el radio de 9px (ni el `borderRadius2XLarge`
consumer de Fluent, ni un radio de 2px cortante) ya está en el rango que la casa recomienda para superficies
B2B densas [source: design-wiki/principles/fluent2-b2b-density.md §"REJECT" — 4-6px inputs/modales; 9px en un
producto de una sola tarjeta-fila es coherente con ese registro, no lo contradice].

### 4.2 Auditoría de contraste — verificada, no asumida (hallazgos reales, 3 fixes)

Recalculé el contraste real (fórmula WCAG de luminancia relativa) de los pares que la UI ya usa para texto, en
vez de asumir que "cálido y suave" pasa AA por diseño. **3 pares fallan hoy**:

| Par | Ratio medido | Uso actual | Umbral que le aplica | Resultado |
|---|---:|---|---|---|
| `--tenue` sobre `--papel` (claro) | **2.78:1** | timestamps (`.cuando`), texto de ayuda en estados vacíos | 4.5:1 (texto pequeño) | **FALLA** |
| `--aviso` sobre `--papel` (claro) | **4.49:1** | badge de tipo `REQUEST`/`HELD`, ~10px | 4.5:1 (texto pequeño) | **FALLA por el margen mínimo** |
| `--lacre` sobre `--lacre-d` (claro) | **4.00:1** | chip `difusion`, 9.5px | 4.5:1 (texto pequeño) | **FALLA** |

Todos los pares equivalentes en modo oscuro pasan con margen (7.7–8.2:1) — el problema es sólo claro, que es
justamente **"el que menos probado está"** por instrucción explícita del encargo. Confirma la sospecha con
números en vez de dejarla en intuición.

**Fixes propuestos (cambio de token o de uso, no invención de hex nuevo salvo uno):**

1. **`--tenue` deja de usarse para texto legible.** Se reserva para elementos genuinamente decorativos
   (divisores, iconos inertes, placeholder de disabled). Cualquier texto que un humano tenga que leer
   (timestamps, meta secundaria, ayuda en estado vacío) usa `--apagado` (5.32:1, pasa) en su lugar. Es un
   cambio de *uso*, cero tokens nuevos.
2. **Chip `difusion`: texto pasa de `--lacre` a `--tinta` sobre `--lacre-d`.** 14.95:1, pasa con margen enorme.
   `--lacre-d` sigue siendo el wash de fondo; `--lacre` se reserva para texto sobre `--papel`/`--panel`, donde
   sí pasa (4.50–4.70:1).
3. **`--aviso` se oscurece ligeramente**: de `#9A6B10` a `#8A5E0E` → 5.46:1, margen cómodo. Es el único cambio
   de valor de token propuesto, y sólo en modo claro (el oscuro ya está bien). Requiere ADR de extensión de
   paleta si se adopta [source: design-wiki/principles/token-first.md §"When to extend the allowlist"] —
   documentado como parte de [adr: ADR-0045].

**Nota sobre `--linea` (1.24:1)**: no es un fallo — es un divisor de fila pasivo, no un límite de componente
interactivo bajo WCAG 1.4.11 (una fila se distingue por su contenido, no sólo por el borde). Pero cualquier
elemento **interactivo** que dependa únicamente de `--linea` para marcar su límite (el campo de búsqueda, el
`<select>` de tipo, el borde del chip "ver más" si se le da borde) necesita un borde de fuerza `--apagado` o
una sombra, nunca sólo `--linea` — se especifica por componente en §5.

### 4.3 Tokens nuevos a registrar (motion — hoy no existe ninguno)

No hay contrato de motion en el proyecto — un solo `transition: background .12s` suelto en `ui.html` [repo:
llminbox/ui.html línea 246]. Se propone un contrato mínimo de 3 valores, honesto con el registro
(terminal-adyacente, no coreografiado), todo detrás de `prefers-reduced-motion`:

```css
@theme {
  --motion-duration-quick:  120ms;  /* hover, press — fila, botón */
  --motion-duration-normal: 180ms;  /* expandir/colapsar cuerpo ("ver más") */
  --motion-duration-drawer: 220ms;  /* menú móvil, entrada/salida */
  --motion-ease-standard: cubic-bezier(.2, 0, 0, 1);
}
@media (prefers-reduced-motion: reduce) {
  * { transition-duration: 0.01ms !important; animation-duration: 0.01ms !important; }
}
```

Todas las transiciones animan `background-color`, `opacity` o `transform` — nunca `height`/`width` en layout
(el colapso de cuerpo usa `max-height` hoy, que es barato para el rango usado, ~460px máx, pero si se nota
jank en contenido muy largo, migrar a `grid-template-rows: 0fr → 1fr` es el reemplazo compositor-friendly de
`max-height` sin medir alto en JS). Regla dura heredada: nunca `transition: all` [wiki:
frontend-ui-design/web-interface-guidelines-vercel.md §"Anti-patrones"].

---

## 5. Anatomía de componentes

### 5.1 Shell — layout de dos columnas

```
┌──────────────┬─────────────────────────────────────────┐
│ aside 244px  │ header (filtros, 44-52px)                │
│ (sidebar)    ├───────────────────────────────────────────┤
│              │ banner de salud (condicional, 0 o 1 fila) │
│              ├───────────────────────────────────────────┤
│              │ flujo de entradas (scroll propio)         │
└──────────────┴─────────────────────────────────────────┘
```

- Desktop (≥760px): grid `244px 1fr`, ya implementado [repo: llminbox/web/src/App.tsx].
- Mobile (<760px): sidebar colapsa a drawer `position: fixed`, fondo semitransparente cerrando al tap-fuera y
  con `Escape` — patrón ya resuelto en `ui.html` [repo: llminbox/ui.html líneas 596-602] y ausente hoy en
  `App.tsx`. Es 1 de los 6 gaps de paridad — §8.
- Viewport primario: 1280–1920 desktop, per el registro de internal-tools B2B [source:
  design-wiki/principles/platform-design-expertise.md §17] — este es un panel de operador, no un producto
  mobile-first. Mobile es *consulta ocasional*, no el flujo principal.

### 5.2 Sidebar

| Zona | Contenido | Estado |
|---|---|---|
| Marca | `llminbox` (mono) + resumen (`N entradas · M ledgers`, o `0 ledgers configurados`) | Texto en `--tinta`, resumen en `--apagado` (nunca `--tenue`, fix §4.2) |
| Lista de ledgers | Botón por ledger: nombre + contador tabular a la derecha. `todos` siempre primero | Activo: fondo `--lacre-d`, texto `--tinta`. Hover: `--alzado`. Foco: anillo `--lacre` 2px, `outline-offset` 1px |
| "Leer como" | `<select>` nativo con todos los actores vistos (agentes + destinatarios) | Persrun to `localStorage`; controla qué cursor se lee para el separador de no-leídos (§5.6) |

Contador: `font-variant-numeric: tabular-nums` obligatorio — es una cifra que se compara entre filas, cae
directamente bajo la regla dura de la casa [wiki: frontend-ui-design/web-interface-guidelines-vercel.md
§"Contenido y Accesibilidad" — `MUST: font-variant-numeric: tabular-nums for number comparisons`]. Ya aplicado
en `ui.html`, ausente en `App.tsx` — gap.

### 5.3 Fila de entrada — el componente central

Esto es donde vive el 90% de la decisión de diseño. Anatomía, de arriba a abajo:

```
[●nombre-actor]  [· gestionado por X | persona | difusión]  [→ destino₁ destino₂]  [TIPO]         [hora]
Titular de la entrada, hasta 2 líneas, luego clamp — font-size 15.5px, peso 450
┌─────────────────────────────────────────────────────────────────────────┐
│ cuerpo recortado a 5.1em (~3 líneas), degradado al final si hay más     │
└─────────────────────────────────────────────────────────────────────────┘
[ver más]
ledger · llegada #N · línea M                                          (pie, 10.5px, --apagado)
```

**Decisión núcleo, y su falsador**: el cuerpo se muestra SIEMPRE, recortado — nunca oculto tras un clic. Esto
ya es la corrección de la patología medida en §1 (inflación de cabecera 131→600 car.). **Falsador**: si se
oculta el cuerpo por defecto y en 5 semanas de uso real la mediana de longitud de cabecera vuelve a subir, la
decisión estaba mal — exactamente el patrón que ya ocurrió una vez, así que el falsador tiene precedente
directo, no es hipotético.

**Peso visual por tipo — la jerarquía que el 41%/2% pide (§2.1)**:

| Tipo | Tratamiento | Razonamiento |
|---|---|---|
| `HEARTBEAT` | Badge y fila a opacidad reducida (`opacity: .5` en el badge, considerar la fila entera a `--apagado` en vez de `--tinta` para el titular) | 41% del volumen, cero decisión — es ruido de fondo que confirma vida, no reclama atención [repo: llminbox/docs/DESIGN-NOTES.es.md §"Por qué no va dentro de un gestor de proyectos"] |
| `REQUEST`, `HELD` | Badge en `--aviso` (oscurecido, §4.2), borde `currentColor` — el color más "caliente" disponible fuera del acento | Son los compromisos reales — el 0,2% que sí requiere una decisión humana. Deben ganar el escaneo, no perderlo entre latidos |
| `ACK`, `DONE` | Badge en `--bien` | Cierre de un compromiso — señal positiva, distinta de un latido |
| `FYI`, `PRODUCED`, `INGESTED`, `AMEND`, `DELTA` | Badge neutro, borde `--linea`, texto `--apagado` | Informativo, ni ruido ni urgente |
| **Sin tipo** (`tipo = null`) | Sin badge — un badge vacío o "—" es peor que ausencia; la deuda de tipado (§2, hasta 99% en un ledger) es demasiado alta para tratarla como excepción rara | No inventar un estado "desconocido" ruidoso — el vacío ya comunica "esta entrada no está tipada" a quien lee varias filas seguidas |

**Fila "mía"**: cuando `YO` (el actor elegido en "leer como") es el actor o está en destinatarios, la fila lleva
`box-shadow: inset 3px 0 0 var(--color-lacre)` + fondo `--lacre-d` al 4% (claro, ya en `ui.html`) — nótese la
tensión con el "left-rail rule": una franja lateral está catalogada como patrón AI-slop *salvo que cargue
semántica de estado* [source: design-wiki/principles/anti-dribbble.md §"Left-rail rule"]. Aquí sí carga
semántica real (mío/no-mío, un estado binario verificable) — pasa el test porque la franja es borrable-sin-perder-información
FALSO: si se borra, se pierde justo el dato "esto es mío" — así que la franja está justificada, no es decoración.

**Estados de actor/destinatario faltante**: cuando `actor` es `null` (deuda de tipado real, §2), la fila muestra
`?` en vez de inventar un nombre — ya resuelto en `ui.html` (`r.actor || "?"`) [repo: llminbox/ui.html línea
519]. Mantener: no hay "adivinar el nombre" en el protocolo tampoco [repo: llminbox/PROTOCOL.md §5 "El
tokenizer nunca guesses"] — la UI hereda la misma honestidad, nunca rellena con un guess.

**"Ver más" — únicamente si hay más**: el umbral ya está bien calibrado en el código existente (>3 líneas o
>260 caracteres) [repo: llminbox/web/src/App.tsx `largo`] — un botón que no hace nada es ruido [repo:
llminbox/ui.html línea 566 comentario]. Mantener tal cual.

### 5.4 Enlaces dentro del cuerpo (sustituto de las tarjetas de Buzz, rechazadas en §1.2)

Si §9.1 aprueba renderizado Markdown limitado: un enlace (`[texto](url)` o URL pelada detectada por
`remark-gfm`) se pinta como texto monospace subrayado al hover, color `--lacre`, con un icono `↗` de 12px al
final (Lucide `ArrowUpRight`, ya en el árbol de dependencias vía `lucide-react`) — nunca una tarjeta con
preview (motivo: cero red en runtime, §1.2). `target="_blank" rel="noreferrer"`.

### 5.5 Chip de identidad — agente / humano / difusión (el hueco #3 nombrado en el encargo)

| Clasificación | Tratamiento visual | Fuente del dato |
|---|---|---|
| **Agente** | Punto de color determinista (`tono()`) + nombre en mono, semibold. Si el censo resuelve el humano responsable, sufijo `· gestionado por {humano}` en `--apagado`, 11px | `roster.agentes[].humano` vía `GET /roster` [repo: llminbox/servicio.py líneas 471-495] |
| **Humano** | Sin punto de color (o punto neutro `--linea`) + badge de texto `persona`, borde `currentColor` en `--bien` | `roster.humanos[]` (con sus `alias`) |
| **Difusión** (`equipo`, `todos`) | Chip de fondo sólido `--lacre-d`, texto `--tinta` (fix §4.2), sin punto — es un destino, no un hablante | `roster.difusion[]` |
| **Sin clasificar** (nombre no está en `roster.json`) | Como agente por defecto pero sin sufijo "gestionado por" — nunca oculta el nombre | Censo vacío es un estado válido documentado [repo: llminbox/PROTOCOL.md §6 "Un roster vacío es un estado válido"] |

Esto ya está resuelto en `ui.html` (`clasificar()`, función completa) [repo: llminbox/ui.html líneas 374-381] y
**falta por completo en `App.tsx`** — es el gap de paridad más importante, porque es el hueco #3 que el propio
encargo señala explícitamente como el que más falta. Prioridad 1 en el handoff (§8).

**Destinatarios llevan el mismo tono que cuando hablan** — permite seguir un hilo entre dos agentes sin leer
nombres, patrón ya implementado [repo: llminbox/ui.html líneas 538-539]. Mantener.

### 5.6 Separador de no-leídos

De solo lectura (§1.1) — lee `GET /cursor/{agente}`, nunca llama al `POST` que avanza [repo:
llminbox/web/src/lib/api.ts `bandeja()`, comentario "OJO: es de LECTURA"]. Visual: banda de ancho completo,
fondo `--lacre-d`, texto `--lacre`, con línea horizontal `currentColor` a cada lado (`::before`/`::after`) —
ya implementado en `ui.html`, ausente en `App.tsx`. Texto: `"{N} nueva{s} desde tu última visita"`. Si TODO lo
cargado es nuevo, el separador va arriba (no hay "antes" que contrastar) — la lógica exacta ya escrita
[repo: llminbox/ui.html líneas 494-511] se porta tal cual, es correcta.

### 5.7 Banner de salud degradada

Condicional, vive entre el header y el flujo, `role="status"` (no `alert` — no es un error del usuario, es un
estado del sistema que se anuncia mas no interrumpe) [source: design-wiki/principles/aria-apg-patterns.md
§"Alert / Live region"]. Dos causas distintas, mismo componente, texto distinto:

- Indexador no al día: *"⚠ El indexador no está al día ({error}). Lo que ves puede no reflejar el fichero."*
- Entradas desaparecidas: *"⚠ {N} entrada(s) que estuvieron y ya no están…"*

Ya implementado en `ui.html` con la lógica correcta (borde izquierdo `--warn`/`--aviso`, fondo `--lacre-d`)
[repo: llminbox/ui.html líneas 645-651] — falta en `App.tsx`. El principio que lo sostiene ya es ley de la casa
para superficies con agentes: **un servicio degradado no se ve normal nunca** [source:
design-wiki/principles/agent-integrated-product-ux.md — "Rule 5, Fast acknowledgment"; y el propio incidente
real del proyecto, §2 punto 5].

### 5.8 Los tres estados vacíos (nunca uno solo)

| Estado | Cuándo | Copy (ya escrito, portar) |
|---|---|---|
| **Sin ledgers** | `LLMINBOX_LEDGERS` vacío | "No hay ningún ledger conectado" + qué hacer (`LLMINBOX_LEDGERS` + `llmi up`) |
| **Ledger sin entradas** | Ledger montado, 0 filas | "Ledger(s) conectados, todavía sin entradas" + tranquilidad de que el indexador sondea solo |
| **Filtro sin resultados** | Hay entradas, los filtros activos no matchean nada | "Nada con estos filtros" + botón "quitar filtros" |

La lógica de cuál aplica ya está resuelta y es más sutil de lo que parece: mira el ledger **seleccionado**, no
el total global, porque un ledger de 0 entradas entre otros con datos NO es lo mismo que "sin filtro" —
`ui.html` ya corrigió este bug específico [repo: llminbox/ui.html líneas 473-484, comentario "medido en vivo"].
Portar la función `motivoVacio()` completa, no reinventarla.

### 5.9 Puerta (token gate) y filtros — sin cambios de fondo

La puerta ya sigue el patrón correcto (token nunca en el HTML servido, se prueba contra `/stat` que sí exige
token porque `/health` no lo exige) [repo: llminbox/web/src/App.tsx `Puerta`]. Filtros (`buscar`, `tipo`, `solo
lo mío`) ya en `ui.html`, ausentes en `App.tsx` — gap de paridad, no de diseño (la forma ya es correcta:
`<input type="search">` + `<select>` nativo + checkbox, sin inventar combobox custom donde el nativo basta —
coherente con Hard Rule #18, cero componente inventado donde uno nativo cierra el caso).

---

## 6. Motion — aplicación por componente

| Interacción | Token | Propiedad |
|---|---|---|
| Hover de fila / botón | `--motion-duration-quick` | `background-color` |
| Expandir/colapsar cuerpo ("ver más") | `--motion-duration-normal` | `max-height` (o `grid-template-rows`, §4.3) |
| Entrada/salida del drawer móvil | `--motion-duration-drawer` | `transform: translateX()` — nunca `left`/`width` [wiki: frontend-ui-design/web-interface-guidelines-vercel.md §"Animación" — NEVER animate layout props] |
| Fondo del drawer (scrim) | `--motion-duration-drawer` | `opacity` |

Todo detrás de `prefers-reduced-motion: reduce` (§4.3). Sin excepciones — no hay ningún momento "de marca" en
este producto que justifique una excepción (a diferencia de un hero de landing).

---

## 7. Accesibilidad — mapeo ARIA APG por componente

| Componente | Patrón APG | Detalle obligatorio |
|---|---|---|
| Lista de ledgers (sidebar) | Navegación, no `listbox` (no hay selección múltiple, es navegación a una vista) | `<nav>` + botones reales, `aria-current="true"` en el activo — ya así en `ui.html` |
| "Leer como" | `<select>` nativo | Usar el elemento nativo, NO un combobox custom — un `<select>` ya da teclado + AT gratis; inventar uno viola implementabilidad sin beneficio |
| Filtro de tipo | `<select>` nativo | Igual que arriba |
| "Ver más" | Disclosure | `<button aria-expanded={abierta} aria-controls={idDelCuerpo}>` — falta el `aria-expanded`/`aria-controls` en el `App.tsx` actual, añadir |
| Separador de no-leídos | Contenido informativo, no interactivo | No es un `live region` (no aparece dinámicamente tras carga en el sentido de WCAG — se pinta con el resto). No necesita rol especial, sólo texto legible |
| Banner de salud degradada | `role="status"` (aviso, no error bloqueante) | Debe estar en el DOM condicionalmente montado — si se inyecta tras la carga inicial y se quiere que se anuncie, confirmar que el `role="status"` esté presente desde el primer render del contenedor, no añadido después (contenido pre-cargado no se anuncia) [source: design-wiki/principles/aria-apg-patterns.md §"Top gotchas"] |
| Drawer móvil | Dialog no-modal-ligero (no bloquea el resto de la página con `aria-modal`, pero sí gestiona foco) | Al abrir: foco al primer enlace del drawer. `Escape` cierra (ya implementado). Al cerrar: foco vuelve al botón hamburguesa (verificar que `App.tsx` lo haga — `ui.html` sí lo hace vía `$("#togglemenu").onclick`) |
| Puerta (token) | Formulario simple | Ya correcto: `<label>` implícito vía `placeholder` es insuficiente — falta `<label>` explícito o `aria-label="token"` en el input, hoy sólo tiene `placeholder` [repo: llminbox/web/src/App.tsx `Puerta`] — **gap de a11y a corregir**, no sólo de paridad |
| Foco visible | Transversal | `:focus-visible` con anillo `--lacre` 2px + `outline-offset` 1px en TODOS los elementos interactivos — ya definido como regla global en `ui.html` [repo: llminbox/ui.html líneas 96-98], portar igual a Tailwind (`focus-visible:ring-2 focus-visible:ring-lacre focus-visible:ring-offset-1`) |

Contraste: aplicar los 3 fixes de §4.2 antes de dar por buena cualquier verificación con axe-core en stage-06 —
axe no cazará el par `--aviso`/`--papel` (4.49 vs 4.5 es un fallo real, no de instrumento).

---

## 8. Mapa de paridad — lo que `ui.html` ya resolvió y `App.tsx` todavía no

`ui.html` es una implementación vanilla previa, ya en uso, con decisiones de diseño correctas y verificadas en
producción (el propio código lleva los comentarios de por qué). El skeleton React actual (`App.tsx`) es un
puerto parcial — el encargo lo llama correctamente "esqueleto funcional, no un diseño". Esta tabla es la lista
de no-regresión para `frontend-biklabs`: nada de esto es nuevo diseño, es **no perder lo que ya funcionaba**.

| Función | `ui.html` (vanilla) | `App.tsx` (React actual) | Prioridad de recuperación |
|---|:---:|:---:|---|
| Cuerpo visible por defecto, recortado | ✅ | ✅ | — (ya paritario) |
| "Ver más" sólo si hay más | ✅ | ✅ | — |
| Clasificación agente/humano/difusión (`roster`) | ✅ | ❌ | **1 — es el hueco #3 del encargo** |
| Separador de no-leídos (cursor real) | ✅ | ❌ | **2 — hueco #1 del encargo (la bandeja)** |
| Los 3 estados vacíos distintos | ✅ | ❌ | **3 — hueco #6 del encargo** |
| Banner de salud degradada | ✅ | ❌ | **4 — hueco #7 del encargo** |
| Filtros (buscar / tipo / solo lo mío) | ✅ | ❌ | 5 |
| Badge de tipo con color semántico | ✅ (sin el fix de contraste) | ❌ (sin badge en absoluto) | 5, junto con §4.2 |
| Menú móvil / drawer con foco gestionado | ✅ | ❌ | 6 |
| `tabular-nums` en contadores | ✅ | ❌ | 6 (trivial, una clase Tailwind) |
| `<label>` accesible en el input de token | ❌ (tampoco lo tenía) | ❌ | 6 — nuevo, ninguna de las dos versiones lo resolvió |

---

## 9. Abierto — no lo decido yo

### 9.1 Renderizado del cuerpo: ¿texto plano o Markdown limitado?

El repo declara una regla de confianza explícita y repetida: el texto de las entradas «se pinta como texto,
nunca como HTML» [repo: llminbox/ui.html líneas 318-321; PROTOCOL.md; el propio encargo del team-lead la repite
como restricción dura]. Hoy se cumple con `textContent`/JSX-texto plano en un `<pre>`.

Pero `web/package.json` ya declara `react-markdown` + `remark-gfm` como dependencias [repo:
llminbox/web/package.json] — alguien anticipó render Markdown, que mejoraría mucho la lectura de cuerpos con
listas, tablas y `código en línea` (el propio `DESIGN-NOTES.es.md` está lleno de esa sintaxis). `react-markdown`
sin el plugin `rehype-raw` **no ejecuta HTML embebido** — un `<script>` literal en un cuerpo se pinta como texto
inerte, no se interpreta. Eso es compatible en espíritu con "nunca HTML", pero es una lectura, no un hecho
cerrado — y toca directamente la frontera de confianza que el proyecto ha protegido con más cuidado que ninguna
otra (token, mounts `:ro`, el propio marcador `X-Llminbox-Untrusted`).

**Mi recomendación de diseño** (no de seguridad): si se activa, GFM mínimo — código en línea, bloques de
código, listas, negrita/cursiva, enlaces (tratamiento §5.4) — **sin** `rehype-raw`, y la marca de "contenido no
confiable" se mantiene visible igual. Pero el visto bueno de que esto no abre una superficie no es mío que dar
— **pido explícitamente sign-off de seguridad antes de que `frontend-biklabs` lo active**, dado que toca
exactamente la frontera que el propio repo más ha reforzado.

### 9.2 Tema: ¿sigue el sistema (como hoy) o se añade toggle manual?

Hoy el tema es 100% `prefers-color-scheme`, sin control manual [repo: llminbox/web/src/index.css]. El encargo
pide "los dos cuidados", no pide explícitamente un selector. Mi recomendación: mantener sólo-sistema — es
menos superficie, coherente con el registro de herramienta autohospedada, y ya "los dos" se cuidan con las
correcciones de §4.2. Si el operador quiere control manual explícito, es una adición pequeña (persistir en
`localStorage`, clase en `<html>`) pero cambia el CSS de `@media` a `[data-tema]` — lo dejo como decisión de
producto, no la fuerzo.

---

## 10. Resumen para `frontend-biklabs`

1. Registrar los 3 fixes de contraste (§4.2) y el contrato de motion (§4.3) en `index.css` — es lo primero,
   todo lo demás depende de esos tokens.
2. Recuperar la tabla de paridad (§8) en el orden dado — son 6 funciones que ya existían y se perdieron en el
   puerto a React, no diseño nuevo.
3. Construir la fila de entrada (§5.3) con el peso visual por tipo — es el componente central y el que más
   valor de jerarquía aporta sobre lo que ya hay.
4. Antes de activar Markdown (§9.1), traer sign-off de seguridad — no bloquea el resto del trabajo, pero no se
   activa sin esa conversación.
5. Verificación visual obligatoria en ambos temas antes de dar por hecho (per el propio contrato de
   `frontend-biklabs`) — el claro es el que este brief más ha tenido que corregir; no asumir que "ya estaba
   bien" sin volver a medir tras los cambios de §4.2.
