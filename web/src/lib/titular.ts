import type { Estado } from "@/lib/api";

/** La cabecera repite en prosa lo que ya sale como metadatos («[🔎📊qa · DIAGNÓSTICO…»
 *  cuando la fila de meta ya dice «qa →»). Enseñar dos veces lo mismo es lo que hace
 *  ilegible una lista de miles de ensayos, así que se pela por capas: corchete,
 *  emoji, la ETIQUETA de tipo si abre la cabecera, el propio actor, la flecha con
 *  sus destinos, el corchete de cierre suelto, y el sello. Portado tal cual de
 *  `ui.html` (`titular`) — verificado contra 1.619 entradas reales de los 6
 *  ledgers: 811 cabeceras rotas → 207, cero casos donde el resultado empeorase.
 *  No reintentar tras pelar el actor: una versión que lo hacía se comía
 *  "ACK"/"DONE"/"HANDOFF" cuando eran texto libre genuino, no la etiqueta. */
const TIPOS_ETIQUETA =
  "HEARTBEAT|CARRY-FORWARD|CLAIM|CANON|CERT|DONE|RESP|AVISO|MSG|ASK|ACK|STATUS|INFO|HANDOFF|" +
  "PRODUCED|INGESTED|FYI|REQUEST|HELD|AMEND|DELTA";
const ETIQUETAS = new RegExp(`^(?:${TIPOS_ETIQUETA})(?:[/\u00b7-]\\s*(?:${TIPOS_ETIQUETA}))*\\b\\s*[:\u00b7\u2014\u2013-]?\\s*`, "i");
const EMOJI = /^[\p{Extended_Pictographic}\p{Emoji_Presentation}\ufe0f\u200d\s\u00b7\u2014\u2013-]+/u;
const SELLO = /^\d{4}-\d{2}-\d{2}T?[\d:]*Z?\s*[:\u00b7\u2014\u2013-]?\s*/;
// Todo lo que quede ANTES del corchete que cierra la cabecera: el corchete suelto,
// la cola de un nombre partido (`deploy-bik` casa dentro de `deploy-bik.eus`), la
// ruta con barra (`[CLAIM x / FICHERO.md]`). No puede haber un `[` por medio, para
// no comerse un `[algo]` legitimo del titular, y tiene que estar cerca.
const CIERRE = /^[^[\]\n]{0,120}\]\s*/;
const GRUPO = /^\[[^\]\n]{0,200}\]\s*/;
// La ruta entera hasta su cierre. 600 y no 160: medida la mediana de estas rutas
// sobre las 37.666 cabeceras reales son 269 caracteres y llegan a 586 — la flota
// mete la lista de destinatarios Y un comentario entre parentesis dentro del
// corchete. Con el tope viejo, 2.447 titulares empezaban por «→ ».
const RUTA = /^(?:\u2192|->|\u2227)[^\]]{0,600}?\]\s*/;
// La ruta SIN corchete que la cierre: la convención del spoke es
// `## <sello> · actor → destino · TIPO — titular`, sin corchetes en ninguna parte.
// Se come la flecha, la lista de destinatarios y —si viene— el tipo con su guion,
// que es exactamente lo que la fila ya muestra al lado. Sin esto, 373 entradas
// abrían por «wiki-vault · PRODUCED —» teniendo su tesis justo detrás.
const FLECHA = new RegExp(
  `^(?:\u2192|->|\u2227)\\s*[\\w.@/+-]+(?:\\s*[\u00b7,+\u2227]\\s*[\\w.@/+-]+)*` +
  `\\s*(?:[\u00b7:]\\s*(?:${TIPOS_ETIQUETA})\\b)?\\s*[\u2014\u2013-]?\\s*`, "i");
// Y la flecha suelta, cuando detrás no hay un nombre sino texto: se quita el
// símbolo y se deja empezar por palabra.
const FLECHA_SOLA = /^(?:\u2192|->|\u2227)\s*/;
// La cola de la convención del spoke: `## <sello> · actor → destino · TIPO`, sin
// titular. Anclada a los DOS extremos, así que sólo casa cuando lo que queda es
// exactamente destinatario y tipo — o sea, cuando la cabecera era puro metadato y
// el titular hay que sacarlo del cuerpo.
const SOLO_RUTA = new RegExp(`^[\\w.\\-]+\\s*[\u00b7:]\\s*(?:${TIPOS_ETIQUETA})\\s*$`, "i");

/** El TITULAR de una entrada: su tesis, sin los metadatos que la fila ya muestra.
 *
 *  La cabecera repite en prosa lo que sale al lado como actor, destino y tipo
 *  («[🔎📊qa · DIAGNOSTICO…» cuando la fila ya dice «qa →»). Ensenar dos veces lo
 *  mismo es lo que hace ilegible una lista de miles de ensayos.
 *
 *  Se pela en PUNTO FIJO —cada regla se reintenta hasta que ninguna muerde— y no
 *  en un orden fijo. El orden fijo fue el error: la flota escribe
 *  `## [actor <sello>] titular`, `## <sello> — [TIPO a→b] titular`,
 *  `### [TIPO actor] [otro] <sello> — titular` y alguna mas, y cada permutacion
 *  nueva pedia otra regla a mano en la posicion justa. Medido sobre las 37.666
 *  cabeceras reales del deposito de origen, esa cadena ordenada dejaba el 13% de
 *  los titulares empezando por basura («] », «→ », «[bikeus] », «>TODOS] »).
 *
 *  El actor se pela SOLO en la primera vuelta: dentro del texto libre, «ACK» o
 *  «DONE» pueden ser la primera palabra de verdad, y reintentar ahi se los comia.
 *
 *  Devuelve cadena vacia cuando la cabecera era solo metadatos. Quien llama decide
 *  el respaldo — la primera linea del cuerpo lo hace mejor que repetir la cabecera.
 */
export function titular(cabecera: string, actor: string | null): string {
  let t = (cabecera ?? "").replace(/^#{2,3}\s*/, "");
  const esc = actor ? actor.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") : null;
  // El guion separador no puede ser el de una flecha ASCII: con `[-]?` a secas,
  // pelar el actor de `[MSG cfo-guardian->TODOS]` se comia el `-` y dejaba
  // `>TODOS]`, que ya no casa la regla de ruta.
  const ACTOR = esc ? new RegExp(`^${esc}\\b\\s*(?:[:\u00b7\u2014\u2013]|-(?!>))?\\s*`, "i") : null;

  // Las reglas de ESTRUCTURA llevan presupuesto; las de adorno, no. Sin el
  // presupuesto el punto fijo se pasa de largo: una vez pelada la cabecera sigue
  // buscando corchetes DENTRO del titular y se come el texto hasta el siguiente.
  // Medido: 34 titulares quedaban en trozos («(compliant)», «GO Alber», «MONITOR»)
  // — pocos, pero cada uno es contenido real perdido, que es peor que un simbolo
  // de mas al principio. Una cabecera no tiene tres rutas ni cuatro corchetes.
  // ETIQUETAS entra en el presupuesto con UNA sola oportunidad, y esa es la
  // lección cara: los nombres de tipo (STATUS, RESP, ACK, DONE) también son
  // palabras corrientes. Reintentarla convirtió el titular real
  // «status: RESP (compliant)» en «(compliant)», comiéndose dos palabras de
  // verdad. La versión anterior de este fichero ya lo advertía en su comentario
  // —"no reintentar tras pelar el actor"— y al pasar a punto fijo me llevé por
  // delante la guarda junto con el orden. El presupuesto la devuelve sin perder
  // lo que el punto fijo arregla: la etiqueta puede aparecer en la vuelta 2, tras
  // el sello y el corchete, pero muerde UNA vez.
  const cupo = { ruta: 1, cierre: 2, grupo: 2, etiqueta: 1, actor: 1 };
  const gasta = (k: keyof typeof cupo, rx: RegExp) => {
    if (cupo[k] <= 0) return;
    const y = t.replace(rx, "");
    if (y !== t) {
      cupo[k]--;
      t = y;
    }
  };

  for (let vuelta = 0; vuelta < 6; vuelta++) {
    const antes = t;
    t = t.replace(/^\[\s*/, "");
    t = t.replace(EMOJI, "");
    gasta("etiqueta", ETIQUETAS);
    if (ACTOR) gasta("actor", ACTOR);
    // Cualquier cierre estructural termina el metadato. Antes sólo lo hacía el
    // sello, y `### [qa] ACK recibido, arranco` perdía su primera palabra: fuera
    // del corchete, «ACK» es texto. La frontera no es una lista de palabras, es
    // el sitio donde la cabecera deja de describirse a sí misma.
    const cierraMeta = cupo.ruta + cupo.cierre + cupo.grupo;
    gasta("ruta", RUTA);
    gasta("cierre", CIERRE);
    gasta("grupo", GRUPO);
    if (cupo.ruta + cupo.cierre + cupo.grupo !== cierraMeta) cupo.etiqueta = 0;
    const conSello = t.replace(SELLO, "");
    // EL SELLO ES LA FRONTERA. En todas las convenciones vivas de estos ledgers el
    // sello de hora es el ÚLTIMO metadato: lo que va detrás ya es la tesis. En
    // cuanto se pela, se cierra el cupo de etiquetas — si no, un titular que empieza
    // por «status: RESP (compliant)» pierde sus dos primeras palabras porque STATUS
    // y RESP son también nombres de tipo. Saber DÓNDE acaba el metadato vale más
    // que cualquier lista de palabras que se pueda escribir.
    if (conSello !== t) {
      t = conSello;
      cupo.etiqueta = 0;
    }
    t = t.replace(FLECHA, "").replace(FLECHA_SOLA, "");
    if (SOLO_RUTA.test(t)) t = "";
    if (t === antes) break;
  }
  return t.trim();
}

/** Hora relativa: hoy → HH:MM, esta semana → "Nd", si no, fecha corta. Portado de
 *  `ui.html` (`hora`) — mejora sobre el slice(11,16) del skeleton, que vuelve
 *  ilegible cualquier entrada de más de un día (un ledger de 37.000 entradas no
 *  es todo de hoy). */
/** El indexador guarda el sello SIN zona (`2026-08-22T18:30:00`), así que hay que
 *  añadir la `Z` para leerlo como UTC. Pero añadirla a ciegas rompe cualquier sello
 *  que YA la traiga —o que traiga un offset—: `…00ZZ` y `…+02:00Z` son ambos
 *  `Invalid Date`, y con la guarda nueva se pintarían como «sello ilegible», que es
 *  peor que el fallo original porque parece deliberado.
 *
 *  Medido hoy: 0 de 71.770 sellos traen zona, así que esto no arregla nada visible.
 *  Está por lo otro — el día que el troceador deje de pelarla, la UI marcaría el
 *  corpus ENTERO como ilegible y el motivo no se vería por ninguna parte. */
const utc = (ts: string): Date =>
  new Date(/(?:Z|[+-]\d{2}:?\d{2})$/.test(ts) ? ts : `${ts}Z`);

export function hora(ts: string | null): string {
  if (!ts) return "·";
  const d = utc(ts);
  // Un sello con FORMA de ISO pero imposible —`9999-99-99T99:99:99`, `2026-13-45`,
  // los dos vivos en `64bis-wiki-archivo`— hace `NaN` aquí, y la UI pintaba
  // «Invalid Date»: se lee como un fallo de la herramienta cuando es un sello que
  // su autor escribió mal. Decirlo por su nombre no esconde el dato, lo señala.
  if (Number.isNaN(d.getTime())) return "sello ilegible";
  const dias = Math.floor((Date.now() - d.getTime()) / 864e5);
  if (dias === 0) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  // `dias > 0`, no sólo `< 7`: un sello en el FUTURO da negativo y esto imprimía
  // «-57d», que no es ni una fecha ni un plazo. Hay 6 en el corpus vivo y uno es
  // legítimo (alguien fechó una entrada en octubre). Que caiga a fecha corta.
  if (dias > 0 && dias < 7) return `${dias}d`;
  return d.toLocaleDateString([], { day: "2-digit", month: "short" });
}

/** Sello largo, para el panel de lectura. Existe para que NO haya dos formateadores
 *  de fecha: `Detalle.tsx` llamaba a `new Date()` por su cuenta y por eso enseñaba
 *  «Invalid Date» aunque `hora()` ya supiera tratar un sello imposible. Dos
 *  autoridades sobre el mismo dato divergen — es el defecto que llminbox acaba de
 *  cerrar en el publicador con `canonical_tipo`, y aquí estaba en pequeño. */
export function sello(ts: string | null): string {
  if (!ts) return "sin sello";
  const d = utc(ts);
  if (Number.isNaN(d.getTime())) return `sello ilegible (${ts})`;
  return d.toLocaleString("es");
}

export type MotivoVacio = "sin-ledgers" | "sin-entradas" | "filtro-vacio";

/** Cuál de los tres estados vacíos aplica. Mira el ledger SELECCIONADO, no el total
 *  global: un ledger de 0 entradas entre otros con datos no es "filtro vacío" — bug
 *  específico ya cazado y corregido en `ui.html` (comentario "medido en vivo").
 *  Portar la función completa, no reinventarla [brief §5.8]. */
export function motivoVacio(args: {
  ledgersConfigurados: number;
  filtrosActivos: boolean;
  estado: Estado[];
  ledgerSeleccionado: string;
}): MotivoVacio {
  if (args.ledgersConfigurados === 0) return "sin-ledgers";
  const relevantes = args.ledgerSeleccionado
    ? args.estado.filter((x) => x.ledger === args.ledgerSeleccionado)
    : args.estado;
  const totalRelevante = relevantes.reduce((a, x) => a + x.entradas, 0);
  if (!args.filtrosActivos && totalRelevante === 0) return "sin-entradas";
  return "filtro-vacio";
}
