/** Configuración de sondeo de los seams nativos — D10 [spec design
 *  STATES-ausente-rancio-vacancia-vs-proceso-muerto-20260908 §2.3, casos C10;
 *  notas de qa N1 adoptadas en MARK:design-adopto-tres-notas-qa-c10-n2-n3].
 *
 *  Un solo sitio, porque C10 es una invariante ENTRE los dos valores: sondear
 *  más lento que el plazo stale es pintar dato rancio con pinta de fresco. El
 *  test (`sondeo.test.mjs`) la cobra: sube el intervalo sin subir el plazo y
 *  sale rojo.
 *
 *  ⚠️ COTA DECLARADA: el plazo stale del SERVIDOR aún no viaja en el wire
 *  (medido sobre 61160d5: ni `/native/v1/organization` ni su fila de revisión
 *  lo traen — `freshness` es estado, no plazo). Cuando Backend lo sirva, esta
 *  constante se re-ancla al valor servido; hasta entonces es la cota con la
 *  que el cliente se compromete, no un dato del servidor.
 */

/** Cada cuánto los 2 hooks nativos refetchan por sí solos (además del
 *  refetch-on-focus). Los stubs no tocan la red, así que el coste del sondeo
 *  mientras `disponible:false` es cero; el día que sirvan, 30 s es cadencia de
 *  panel, no de supervisor. */
export const SONDEO_NATIVO_MS = 30_000;

/** Plazo stale asumido del runtime (ADR-002 Decisión 1: stale = «el plazo se
 *  superó»). Ver la cota declarada arriba. */
export const PLAZO_STALE_NATIVO_MS = 300_000;
