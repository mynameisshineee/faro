import type { Salud } from "@/lib/api";

/** Por qué el servicio NO está `ok` — y la respuesta no siempre es el indexador.
 *
 *  `/health` calcula `ok` así [servicio.py:3268]:
 *      ok = sano and not ROTOS and not SOLO_LECTURA["activo"]
 *
 *  Son TRES causas, y el banner las atribuía todas a la primera: con la base en
 *  sólo-lectura o con un ledger roto decía «El indexador no está al día (sin detalle)»,
 *  y quien lo leía se iba a mirar el indexador, que estaba perfecto. Un aviso que nombra
 *  la causa equivocada es peor que no avisar: manda a alguien a arreglar lo que funciona
 *  mientras lo que falla sigue fallando.
 *
 *  ORDEN DE PRECEDENCIA, y no es arbitrario: se nombra primero lo que impide MÁS cosas.
 *  Sólo-lectura mata las escrituras de todo el mundo; un ledger roto ciega una bandeja;
 *  el indexador desfasado sólo retrasa. Y las desapariciones van por delante de todo
 *  porque son la única que acusa a los DATOS y no al servicio.
 */
export function motivoSalud(salud: Salud | undefined, desaparecidas: number): string | null {
  if (desaparecidas > 0)
    // La concordancia va por el VERBO también. El mensaje original decía «1 entrada que
    // estuvieron y ya no están» — venía de pluralizar sólo el sustantivo, y lo cazó el
    // primer caso de test que puso el número 1.
    return desaparecidas === 1
      ? "1 entrada que estuvo y ya no está. Un ledger de sólo-apéndice no pierde entradas: mira \`llmi verify\`."
      : `${desaparecidas} entradas que estuvieron y ya no están. Un ledger de sólo-apéndice no pierde entradas: mira \`llmi verify\`.`;
  if (!salud || salud.ok) return null;
  if (salud.solo_lectura)
    return `El índice está en SÓLO LECTURA (${salud.solo_lectura}). Puedes leer, pero nada de lo que escribas se guarda.`;
  const rotos = salud.rotos ? Object.keys(salud.rotos) : [];
  if (rotos.length)
    return `${rotos.length} ledger${rotos.length === 1 ? "" : "s"} sin indexar (${rotos.slice(0, 3).join(", ")}${rotos.length > 3 ? "…" : ""}). Esas bandejas están ciegas, el resto no.`;
  // Y sólo aquí, el indexador. Con su detalle si lo hay: el «sin detalle» de antes salía
  // en los tres casos, así que no distinguía «no sé qué le pasa» de «no es esto».
  return `El indexador no está al día (${salud.indexador?.error ?? "sin detalle"}). Lo que ves puede no reflejar el fichero.`;
}
