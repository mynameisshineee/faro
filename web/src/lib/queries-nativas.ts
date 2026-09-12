import type { Query, QueryClient } from "@tanstack/react-query";

/** Prefijos de las DOS queries nativas — UNA fuente, compartida por los hooks
 *  (`queries.ts`), por el formulario (`CredencialNativa`) y por los builders
 *  de abajo. Rutas fijas del endpoint: jamás viaja la credencial en la clave. */
export const CLAVE_ORG_NATIVA = ["native-organization"] as const;
export const CLAVE_RUNTIMES_NATIVOS = ["native-runtimes"] as const;

/** Claves CON la revisión de identidad (el epoch de `api.ts`: un entero no
 *  secreto). Al cambiar la credencial la revisión sube ⇒ la clave CAMBIA ⇒ el
 *  observador del hook abandona la query de la identidad anterior y levanta
 *  la de la nueva [codex #1445: `removeQueries` destruye en silencio y no
 *  vacía el resultado de un observador activo — sin cambio de clave, la
 *  pantalla seguiría enseñando A con el mapa vacío]. */
export const claveOrganizacionNativa = (revision: number) =>
  [...CLAVE_ORG_NATIVA, revision] as const;
export const claveRuntimesNativos = (revision: number) =>
  [...CLAVE_RUNTIMES_NATIVOS, revision] as const;

/** Al cambiar o borrar la credencial se CANCELA lo que siga volando de la
 *  identidad anterior y se RETIRA de la caché — pero SOLO el rastro de
 *  revisiones `<= hastaRevision` [codex #1462]: por prefijo desnudo la limpieza
 *  borraría también la lectura de la identidad NUEVA que el re-render ya pudo
 *  arrancar mientras la limpieza esperaba. El límite se captura ANTES de mutar
 *  la credencial, jamás después de un await. El pintado limpio NO depende de
 *  esto — lo garantiza el cambio de clave de arriba; esto saca el rastro muerto
 *  para que ninguna query de una identidad anterior pueda re-servirse. */
export async function retirarQueriesNativas(qc: QueryClient, hastaRevision: number): Promise<void> {
  const delRastroAnterior = (q: Query) =>
    (q.queryKey[0] === CLAVE_ORG_NATIVA[0] || q.queryKey[0] === CLAVE_RUNTIMES_NATIVOS[0])
    && typeof q.queryKey[1] === "number"
    && (q.queryKey[1] as number) <= hastaRevision;
  await qc.cancelQueries({ predicate: delRastroAnterior });
  qc.removeQueries({ predicate: delRastroAnterior });
}
