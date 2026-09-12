import type { Roster } from "@/lib/api";

/** El censo dice quién es un agente (y de qué humano responde), quién es un humano
 *  escribiendo directo, y qué nombres son destinos de difusión (equipo, todos…) en
 *  vez de una persona. Sin esto los tres se pintaban IGUAL — el hueco #3 del
 *  encargo: en un canal mixto humano+IA, de quién es el agente que habla es la
 *  información que más falta. Portado de `ui.html` (`cargarRoster`/`clasificar`). */
export type Clasificacion = "agente" | "humano" | "difusion" | null;

export type Censo = {
  difusion: Set<string>;
  humanos: Set<string>;
  agenteHumano: Map<string, string>;
};

export const CENSO_VACIO: Censo = { difusion: new Set(), humanos: new Set(), agenteHumano: new Map() };

/** Un roster ausente o vacío es un estado válido documentado [PROTOCOL.md §6] — no
 *  falla, degrada a "sin clasificar" para todo el mundo. */
export function construirCenso(roster: Roster | undefined): Censo {
  if (!roster) return CENSO_VACIO;
  const difusion = new Set((roster.difusion ?? []).map((x) => x.toLowerCase()));
  const humanos = new Set<string>();
  for (const h of roster.humanos ?? []) {
    if (h.nombre) humanos.add(h.nombre.toLowerCase());
    for (const a of h.alias ?? []) humanos.add(String(a).toLowerCase());
  }
  const agenteHumano = new Map<string, string>();
  for (const a of roster.agentes ?? []) {
    if (a.nombre && a.humano) agenteHumano.set(a.nombre.toLowerCase(), a.humano);
  }
  return { difusion, humanos, agenteHumano };
}

export function clasificar(censo: Censo, nombre: string | null | undefined): Clasificacion {
  if (!nombre) return null;
  const n = nombre.toLowerCase();
  if (censo.difusion.has(n)) return "difusion";
  if (censo.humanos.has(n)) return "humano";
  if (censo.agenteHumano.has(n)) return "agente";
  return null;
}

/** El humano que responde de este agente, si el censo lo resuelve. */
export function humanoDe(censo: Censo, nombre: string | null | undefined): string | undefined {
  if (!nombre) return undefined;
  return censo.agenteHumano.get(nombre.toLowerCase());
}
