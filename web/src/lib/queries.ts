import { useQuery } from "@tanstack/react-query";
import { useSyncExternalStore } from "react";
import { api, revisionIdentidadNativa, suscribirseIdentidadNativa } from "@/lib/api";
import { claveOrganizacionNativa, claveRuntimesNativos } from "@/lib/queries-nativas";
import { SONDEO_NATIVO_MS } from "@/lib/sondeo";

/** Hooks TQ — un fichero, porque la app es una sola vista. queryKey = ruta del
 *  endpoint como prefijo, convención ya establecida en el skeleton (`["stat"]`). */

export function useSalud(entrado: boolean) {
  return useQuery({ queryKey: ["health"], queryFn: api.salud, enabled: entrado });
}

export function useEstado(entrado: boolean) {
  return useQuery({ queryKey: ["stat"], queryFn: api.estado, enabled: entrado });
}

export function useRoster(entrado: boolean) {
  return useQuery({ queryKey: ["roster"], queryFn: api.roster, enabled: entrado });
}

/** Legado, `authority:false` — ver `Organigrama` en `api.ts`. */
export function useOrganigrama(entrado: boolean) {
  return useQuery({ queryKey: ["organigrama"], queryFn: api.organigrama, enabled: entrado });
}

/** Los dos seams nativos [ADR-002 Decisión 5] — CONECTADOS al gateway
 *  (`runtime_root.build_app`, contrato e1f0e2a). `staleTime: 0` sigue: el estado
 *  de la flota es efímero y cada ciclo de sondeo debe volver a preguntar, nunca
 *  servir del cache una organización o un runtime que ya cambiaron.
 *
 *  `refetchInterval`: mitad del cliente de D10 [design §2.3, C10] — sin él el
 *  "sondeo" era refetch-on-focus y el estado nativo envejecía en silencio.
 *  La invariante intervalo ≤ plazo stale vive en `sondeo.ts` y la cobra su
 *  test. */
export function useOrganizacionNativa(entrado: boolean) {
  // La revisión de identidad en la clave [codex #1445]: al cambiar la
  // credencial la clave CAMBIA, el observador abandona la query de la
  // identidad anterior y levanta la nueva — la pantalla nunca mezcla org B
  // con runtimes A aunque B tarde en responder.
  const revision = useSyncExternalStore(suscribirseIdentidadNativa, revisionIdentidadNativa);
  return useQuery({
    queryKey: claveOrganizacionNativa(revision),
    queryFn: api.organizacionNativa,
    enabled: entrado,
    staleTime: 0,
    refetchInterval: SONDEO_NATIVO_MS,
  });
}
export function useRuntimesNativos(entrado: boolean) {
  const revision = useSyncExternalStore(suscribirseIdentidadNativa, revisionIdentidadNativa);
  return useQuery({
    queryKey: claveRuntimesNativos(revision),
    queryFn: api.runtimesNativos,
    enabled: entrado,
    staleTime: 0,
    refetchInterval: SONDEO_NATIVO_MS,
  });
}

/** Solo lectura — nunca el POST que avanza el cursor (ver `api.ts`). Deshabilitado
 *  sin agente elegido en "leer como": sin YO no hay separador que pintar. */
export function useCursores(agente: string, entrado: boolean) {
  return useQuery({
    queryKey: ["cursor", agente],
    queryFn: () => api.cursor(agente),
    enabled: entrado && Boolean(agente),
  });
}

export type FiltrosEntradas = {
  ledger: string;
  q: string;
  tipo: string;
  soloMias: boolean;
  yo: string;
};

export function useEntradas(f: FiltrosEntradas, entrado: boolean) {
  return useQuery({
    queryKey: ["entries", f],
    // `entradasConCorte` y no `entradas`: el servicio declara en `x-filas-capadas` cuándo
    // ha recortado la lista, y sin leer la cabecera la interfaz enseñaba «10 entradas»
    // habiendo decenas de miles. La distinción existía en el backend y moría en el
    // transporte — el mecanismo estaba, no lo llamaba nadie.
    queryFn: () =>
      api.entradasConCorte({
        ledger: f.ledger || undefined,
        limit: 120,
        cuerpo: true,
        q: f.q || undefined,
        tipo: f.tipo || undefined,
        to: f.soloMias && f.yo ? f.yo : undefined,
      }),
    enabled: entrado,
  });
}

/** Muestra amplia, independiente de los filtros activos — sirve solo para poblar
 *  las opciones de "tipo" y "leer como". Si se derivasen de la lista ya filtrada
 *  (como hacía el skeleton), filtrar por ledger encoge esas opciones y el valor
 *  elegido en "leer como" puede dejar de tener <option> — bug ya resuelto en
 *  `ui.html`, que muestrea 400 aparte al arrancar. */
export function useMuestra(entrado: boolean) {
  return useQuery({
    queryKey: ["entries-muestra"],
    queryFn: () => api.entradas({ limit: 400 }),
    enabled: entrado,
  });
}
