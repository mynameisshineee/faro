import type { UseQueryResult } from "@tanstack/react-query";
import { ErrorNativo } from "@/lib/api";
import type { ObservacionRuntimeNativa, OrganizacionNativa, SeamNativo } from "@/lib/api";
import {
  COLOR_RUNTIME_STATUS,
  ETIQUETA_RUNTIME_STATUS,
  estadoTablaRuntimes,
  filasRuntimesNativos,
  motivoPanelNoDisponible,
} from "@/lib/flota";
import { cn } from "@/lib/utils";
import { CredencialNativa } from "@/components/CredencialNativa";

/** Estado por agente/runtime — el contrato G7-G10 real de `ADR-002-FLEET-
 *  CONTROL-PLANE.md`, servido por `GET /native/v1/organization` + `GET
 *  /native/v1/runtimes`. CONECTADO [encargo astra runtime-root 2026-09-08]:
 *  las cinco rutas sirven vía `runtime_root.build_app` (155/155 en 5eefaa4) y
 *  los seams ya hacen la petición con sesión nativa propia — la cabecera
 *  "nativo · G7-G10" acompaña ahora a datos de verdad, no a un stub.
 *
 *  Los 8 estados de "console truthfulness" [ADR-002 Decisión 8] — no los 5 del
 *  encargo original, que la propia ADR amplía y cierra: loading · unavailable ·
 *  unobserved · stale · degraded · stopped · recovering · request-failure. Los
 *  cuatro primeros son de PANEL (esta función decide entre ellos); los cuatro
 *  últimos son de FILA (`RuntimeStatus`, más `absent`/`fresh` que no tienen
 *  hueco en la lista de la ADR porque no son evidencia de fallo). */
export function TablaRuntimesNativos({
  organizacion,
  runtimes,
}: {
  organizacion: UseQueryResult<SeamNativo<OrganizacionNativa>>;
  runtimes: UseQueryResult<SeamNativo<ObservacionRuntimeNativa[]>>;
}) {
  // El formulario de credencial NO vive en ninguna rama de estado [codex
  // followup 17:30]: en carga, en error, sin organización, vacío y con
  // datos hay que poder cambiarla o quitarla sin recargar — UNA instancia
  // al pie del panel, fuera de los retornos de estado.
  const cuerpo = (() => {
    if (organizacion.isLoading || runtimes.isLoading) {
      return (
        <p role="status" className="p-6 text-center text-sm text-apagado">
          cargando estado nativo…
        </p>
      );
    }
    // "request failure" — la query falla cuando `queryFn` lanza: red caída, HTTP
    // 5xx, sesión rechazada o forma no reconocida (fail-closed). El mensaje del
    // error del seam trae el motivo concreto.
    if (organizacion.isError || runtimes.isError) {
      const err = organizacion.error ?? runtimes.error;
      // F5 [design #1441]: sin credencial no hubo red que fallara — ausencia
      // esperada de configuración, no avería. Banner punteado, sin alert ni rojo.
      if (err instanceof ErrorNativo && err.code === "SIN_CREDENCIAL") {
        return (
          <div role="status" className="max-w-[640px] rounded-lg border border-dashed border-linea bg-alzado px-4 py-6 text-sm text-apagado">
            <p className="font-medium text-tinta">Este panel no tiene credencial de lectura todavía.</p>
            <p className="mt-1.5">Pégala abajo — se guarda aparte del token del canal.</p>
          </div>
        );
      }
      return (
        <div role="alert" className="m-4 rounded-r border-l-[3px] border-mal bg-lacre-d px-3 py-2.5 text-sm text-tinta">
          <p className="font-medium">Fallo de red consultando el estado nativo.</p>
          <p className="mt-1 text-[13px] text-apagado">{err instanceof Error ? err.message : "Error desconocido"}</p>
        </div>
      );
    }

    const org = organizacion.data;
    const rts = runtimes.data;
    const estadoPanel = estadoTablaRuntimes(org, rts); // E8b: estado PROPIO de esta tabla — la avería del org no borra filas ajenas
    return (
      <>
      {estadoPanel === "no-disponible" && (
        <div role="status" className="max-w-[640px] rounded-lg border border-dashed border-linea bg-alzado px-4 py-6 text-sm text-apagado">
          <p className="font-medium text-tinta">No disponible.</p>
          <p className="mt-1.5 [overflow-wrap:anywhere]">
            {motivoPanelNoDisponible(org, rts) ?? "Los endpoints nativos no respondieron."}
          </p>
          <p className="mt-1.5">
            Esto NO significa que la flota esté sana: significa que este panel no tiene fuente
            ahora mismo. Un array vacío no sería evidencia de cero fallos — por eso este estado se
            muestra por separado, en vez de una tabla en blanco.
          </p>
        </div>
      )}

      {estadoPanel === "sin-organizacion" && (
        <div role="status" className="max-w-[640px] rounded-lg border border-dashed border-linea bg-alzado px-4 py-6 text-sm text-apagado">
          <p className="font-medium text-tinta">Sin organización activa.</p>
          <p className="mt-1.5 [overflow-wrap:anywhere]">
            {org && !org.disponible ? org.motivo : ""}
          </p>
          <p className="mt-1.5">
            El servidor nativo está sano y RESPONDIÓ: no hay revisión de organización activa
            (404 SUBJECT_NOT_FOUND). Es una ausencia tipada — dato real, distinto de "no
            disponible". Sin revisión activa no hay cruce posible: aunque hubiera runtimes
            observados, no hay organización contra la que cruzarlos.
          </p>
        </div>
      )}

      {estadoPanel === "vacio" && (
        <div role="status" className="max-w-[640px] rounded-lg border border-dashed border-linea bg-alzado px-4 py-6 text-sm text-apagado">
          La organización activa existe y hoy no declara roles ni runtimes que enseñar.
          Es un dato real — distinto de "no disponible" y de "sin organización".
        </div>
      )}

      {estadoPanel === "con-datos" && org?.disponible && rts?.disponible && (
        <div className="overflow-x-auto rounded-lg border border-linea">
          <table className="w-full min-w-[1180px] border-collapse text-[13px]">
            <caption className="sr-only">
              Estado operacional por runtime: lane, principal, rol, estado, último dato observado,
              causa, transición, recibo y revisión de organización
            </caption>
            {/* La credencial se cambia o se quita TAMBIÉN con la conexión hecha
             *  [encargo codex #1350-next] — no sólo cuando el panel está vacío. */}
            <thead>
              <tr className="border-b border-linea bg-alzado text-left text-[11px] uppercase tracking-[.06em] text-tenue">
                <th scope="col" className="px-3 py-2 font-semibold">Runtime</th>
                <th scope="col" className="px-3 py-2 font-semibold">Lane</th>
                <th scope="col" className="px-3 py-2 font-semibold">Principal</th>
                <th scope="col" className="px-3 py-2 font-semibold">Rol</th>
                <th scope="col" className="px-3 py-2 font-semibold">Estado</th>
                <th scope="col" className="px-3 py-2 font-semibold">Desde</th>
                <th scope="col" className="px-3 py-2 font-semibold">Último observado</th>
                <th scope="col" className="px-3 py-2 font-semibold">Causa</th>
                <th scope="col" className="px-3 py-2 font-semibold">Transición</th>
                <th scope="col" className="px-3 py-2 font-semibold">Recibo</th>
                <th scope="col" className="px-3 py-2 font-semibold">Revisión org.</th>
              </tr>
            </thead>
            <tbody>
              {filasRuntimesNativos(org.datos, rts.datos).map((f, i) => (
                <tr key={`${f.role ?? "sin-rol"}:${f.runtime_instance ?? i}`} className="border-b border-linea last:border-b-0 odd:bg-panel even:bg-papel">
                  <th scope="row" className="px-3 py-2 text-left font-mono font-medium text-tinta">
                    {f.runtime_instance ?? <span className="font-sans italic text-tenue">sin runtime observado</span>}
                  </th>
                  <td className="px-3 py-2 font-mono text-apagado">{f.lane}</td>
                  <td className="px-3 py-2 text-apagado">{f.principal ?? "—"}</td>
                  <td className="px-3 py-2 text-apagado">{f.role ?? "—"}</td>
                  <td className="px-3 py-2">
                    <span className={cn("font-medium", COLOR_RUNTIME_STATUS[f.status])}>
                      {ETIQUETA_RUNTIME_STATUS[f.status]}
                    </span>
                    {f.detector_state && (
                      <p className="mt-0.5 text-[11px] text-tenue">detector M3: {f.detector_state}</p>
                    )}
                  </td>
                  <td className="px-3 py-2 tabular-nums text-apagado">{f.status_since ?? "—"}</td>
                  <td className="px-3 py-2 tabular-nums text-apagado">{f.last_observed_at ?? "—"}</td>
                  <td className="px-3 py-2 font-mono text-[12px] text-apagado">{f.cause_id ?? "—"}</td>
                  <td className="px-3 py-2 font-mono text-[12px] text-apagado">{f.transition_id ?? "—"}</td>
                  <td className="px-3 py-2 font-mono text-[12px] text-apagado">{f.receipt_id ?? "—"}</td>
                  <td className="px-3 py-2 tabular-nums text-apagado">{f.organization_revision}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      </>
    );
  })();

  return (
    <section aria-labelledby="titulo-runtimes" className="p-4">
      <div className="mb-2.5 flex flex-wrap items-center gap-x-3 gap-y-1">
        <h2 id="titulo-runtimes" className="text-[15px] font-semibold text-tinta">
          Estado por agente
        </h2>
        <span
          title="Grafo de responsabilidad y estado de runtime nativos — G7/G8, ADR-002 Decisiones 1 y 6."
          className="inline-flex items-center gap-1 rounded-full border border-bien/40 bg-bien/10 px-2 py-0.5 text-[10.5px] font-semibold uppercase tracking-[.06em] text-bien"
        >
          nativo · G7-G10
        </span>
      </div>
      {cuerpo}
      <details className="mt-3">
        <summary className="cursor-pointer text-[12px] font-medium text-tenue hover:text-tinta">
          Credencial de lectura
        </summary>
        <CredencialNativa />
      </details>
    </section>
  );
}
