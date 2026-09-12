import type { UseQueryResult } from "@tanstack/react-query";
import type { OrganizacionNativa, SeamNativo } from "@/lib/api";
import { filasOrganizacionNativa } from "@/lib/flota";

/** Grafo de responsabilidad NATIVO [ADR-002 Decisión 6] — el sucesor
 *  autoritativo del organigrama legado, cuando `GET /native/v1/organization`
 *  exista. Hasta entonces se muestra `disponible:false` como estado honesto,
 *  nunca fundido con `/organigrama` [Decisión 6: *"they do not merge the legacy
 *  endpoint into an authoritative view"*] — este componente no importa nada de
 *  `TablaJerarquia`, ni al revés. Falta explícitamente en la primera versión de
 *  este console; añadido tras el finding directo de `codex-llminbox` sobre
 *  74ffbe7 ("no muestra... grafo de responsabilidad"). */
export function TablaOrganizacionNativa({ query }: { query: UseQueryResult<SeamNativo<OrganizacionNativa>> }) {
  if (query.isLoading) {
    return (
      <p role="status" className="p-6 text-center text-sm text-apagado">
        cargando grafo de responsabilidad nativo…
      </p>
    );
  }
  if (query.isError) {
    return (
      <div role="alert" className="m-4 rounded-r border-l-[3px] border-mal bg-lacre-d px-3 py-2.5 text-sm text-tinta">
        {query.error instanceof Error ? query.error.message : "Error desconocido"}
      </div>
    );
  }

  const seam = query.data;
  if (!seam) return null; // enabled=false — nada que pintar todavía

  return (
    <section aria-labelledby="titulo-org-nativa" className="p-4">
      <div className="mb-2.5 flex flex-wrap items-center gap-x-3 gap-y-1">
        <h2 id="titulo-org-nativa" className="text-[15px] font-semibold text-tinta">
          Grafo de responsabilidad
        </h2>
        <span
          title="El sucesor autoritativo del organigrama legado — ADR-002 Decisión 6."
          className="inline-flex items-center gap-1 rounded-full border border-bien/40 bg-bien/10 px-2 py-0.5 text-[10.5px] font-semibold uppercase tracking-[.06em] text-bien"
        >
          nativo · G8
        </span>
      </div>

      {!seam.disponible ? (
        <div role="status" className="max-w-[640px] rounded-lg border border-dashed border-linea bg-alzado px-4 py-6 text-sm text-apagado">
          {seam.ausente ? (
            // D12 — ausencia tipada [contrato codex #1343]: el servidor está sano
            // y responde que NO hay revisión activa (404 SUBJECT_NOT_FOUND). Es
            // su respuesta, no una avería — por eso no se encabeza "No disponible".
            <p className="font-medium text-tinta">Sin organización activa.</p>
          ) : (
            <p className="font-medium text-tinta">No disponible todavía.</p>
          )}
          <p className="mt-1.5 [overflow-wrap:anywhere]">{seam.motivo}</p>
        </div>
      ) : seam.datos.roles.length === 0 ? (
        <div role="status" className="max-w-[640px] rounded-lg border border-dashed border-linea bg-alzado px-4 py-6 text-sm text-apagado">
          El endpoint nativo respondió y la revisión activa no declara ningún rol todavía. Es un
          dato real — distinto de "no disponible".
        </div>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-linea">
          <table className="w-full min-w-[760px] border-collapse text-[13px]">
            <caption className="sr-only">
              Grafo de responsabilidad nativo: rol, responsable superior, revisores, rutas de
              escalado, capa y responsables inferiores
            </caption>
            <thead>
              <tr className="border-b border-linea bg-alzado text-left text-[11px] uppercase tracking-[.06em] text-tenue">
                <th scope="col" className="px-3 py-2 font-semibold">Rol</th>
                <th scope="col" className="px-3 py-2 font-semibold">Reporta a</th>
                <th scope="col" className="px-3 py-2 font-semibold">Revisores</th>
                <th scope="col" className="px-3 py-2 font-semibold">Rutas de escalado</th>
                <th scope="col" className="px-3 py-2 font-semibold">Capa</th>
                <th scope="col" className="px-3 py-2 font-semibold">Responsables inferiores</th>
              </tr>
            </thead>
            <tbody>
              {filasOrganizacionNativa(seam.datos).map((f) => (
                <tr key={f.rol} className="border-b border-linea last:border-b-0 odd:bg-panel even:bg-papel">
                  <th scope="row" className="px-3 py-2 text-left font-mono font-medium text-tinta">{f.rol}</th>
                  <td className="px-3 py-2 text-apagado">{f.reportaA ?? "— (raíz)"}</td>
                  <td className="px-3 py-2 text-apagado">{f.revisores.length ? f.revisores.join(", ") : "ninguno"}</td>
                  <td className="px-3 py-2 text-apagado">{f.rutasEscalado.length ? f.rutasEscalado.map((e) => `${e.trigger_code}→${e.target_role}`).join(", ") : "ninguna"}</td>
                  <td className="px-3 py-2 text-apagado">{f.capa ?? "—"}</td>
                  <td className="px-3 py-2 text-apagado">{f.subordinados.length ? f.subordinados.join(", ") : "ninguno"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
