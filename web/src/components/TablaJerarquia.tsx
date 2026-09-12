import { useState } from "react";
import type { UseQueryResult } from "@tanstack/react-query";
import type { Organigrama } from "@/lib/api";
import { filasJerarquia, motivoOrganigramaVacio, rutaEscalado } from "@/lib/flota";

/** Copia el primer salto de una ruta de escalado al portapapeles. Acción REAL
 *  (utilidad del navegador, cero mutación en el servidor) — nunca un botón que
 *  finja invocar un verbo de escalado que no existe [ADR-002 Decisión 5/8:
 *  "console read-only... ningún control debe emitir POST a runtimes/*
 *  /recoveries ni ningún verbo mutador"]. */
function BotonEscalado({ destino }: { destino: string }) {
  const [estado, setEstado] = useState<"listo" | "copiado" | "sin-portapapeles">("listo");
  const copiar = async () => {
    try {
      await navigator.clipboard.writeText(destino);
      setEstado("copiado");
    } catch {
      setEstado("sin-portapapeles");
    }
    setTimeout(() => setEstado("listo"), 1600);
  };
  return (
    <button
      type="button"
      onClick={copiar}
      aria-label={`Copiar destinatario de escalado: ${destino}`}
      className="rounded-md border border-linea bg-panel px-2 py-1 text-[12px] text-tinta outline-none transition-colors duration-[var(--motion-duration-quick)] hover:border-lacre hover:text-lacre motion-reduce:transition-none focus-visible:ring-2 focus-visible:ring-lacre focus-visible:ring-offset-1"
    >
      {estado === "copiado" ? "copiado ✓" : estado === "sin-portapapeles" ? "sin portapapeles" : `copiar «${destino}»`}
      <span className="sr-only" role="status" aria-live="polite">
        {estado === "copiado" && "destinatario copiado al portapapeles"}
        {estado === "sin-portapapeles" && "el navegador no permitió copiar"}
      </span>
    </button>
  );
}

/** Badge de autoridad — el criterio visual que `ADR-002` deja abierto en su
 *  Decisión 6 ("si se muestra legado, la etiqueta va en el propio componente, no
 *  sólo en el payload" [design-audit-fleet-console-v1.0-beta-7677025.md §1(d)]).
 *  Va en CADA render de la sección, nunca sólo en un tooltip o en un aviso que
 *  pueda desplazarse fuera de vista. */
function InsigniaLegado() {
  return (
    <span
      title="Grafo de roles de /organigrama (v0.9). No es el grafo de responsabilidad nativo (G8) — ver ADR-002 Decisión 6."
      className="inline-flex items-center gap-1 rounded-full border border-linea bg-alzado px-2 py-0.5 text-[10.5px] font-semibold uppercase tracking-[.06em] text-tenue"
    >
      legado · authority:false
    </span>
  );
}

/** Organigrama LEGADO — jerarquía de ROLES tal como la sirve `/organigrama`
 *  [servicio.py::organigrama]. Es de ROLES y no de agentes a propósito: un rol
 *  puede tener cero, una o varias sesiones vivas, y esta tabla no inventa un
 *  vínculo agente↔rol que el propio endpoint no declara.
 *
 *  Tres estados reales, no una vacía disfrazando dos causas: cargando · error ·
 *  vacío (dos motivos distintos, servidos por el propio `/organigrama` en
 *  `aviso`) · rancio · con datos. `stale` es un campo real, no inventado. */
export function TablaJerarquia({ query }: { query: UseQueryResult<Organigrama> }) {
  if (query.isLoading) {
    return (
      <p role="status" className="p-6 text-center text-sm text-apagado">
        cargando organigrama legado…
      </p>
    );
  }
  if (query.isError) {
    return (
      <div role="alert" className="m-4 rounded-r border-l-[3px] border-mal bg-lacre-d px-3 py-2.5 text-sm text-tinta">
        <p className="font-medium">No se pudo leer el organigrama legado.</p>
        <p className="mt-1 text-[13px] text-apagado">
          {query.error instanceof Error ? query.error.message : "Error desconocido"} — reintenta con el botón de abajo.
        </p>
        <button
          type="button"
          onClick={() => query.refetch()}
          className="mt-2 rounded-md border border-linea bg-panel px-2.5 py-1.5 text-[12.5px] text-tinta outline-none transition-colors hover:border-lacre hover:text-lacre focus-visible:ring-2 focus-visible:ring-lacre focus-visible:ring-offset-1"
        >
          reintentar
        </button>
      </div>
    );
  }

  const org = query.data;
  if (!org) return null; // enabled=false (puerta cerrada) — nada que pintar todavía
  const motivoVacio = motivoOrganigramaVacio(org);
  const filas = filasJerarquia(org);

  return (
    <section aria-labelledby="titulo-organigrama" className="p-4">
      <div className="mb-2.5 flex flex-wrap items-center gap-x-3 gap-y-1">
        <h2 id="titulo-organigrama" className="text-[15px] font-semibold text-tinta">
          Organigrama
        </h2>
        <InsigniaLegado />
        <span className="text-[12px] text-tenue">{org.roles} rol(es)</span>
      </div>

      {org.stale && (
        <div role="status" className="mb-2.5 rounded-r border-l-[3px] border-aviso bg-lacre-d px-2.5 py-2 text-xs text-tinta [overflow-wrap:anywhere]">
          ⚠ Organigrama RANCIO: no se pudo releer la fuente firmada en la última petición.
          Esto es lo último bueno que se cargó — puede no seguir vigente.
        </div>
      )}

      {motivoVacio ? (
        <div className="max-w-[520px] rounded-lg border border-dashed border-linea bg-alzado px-4 py-6 text-center text-sm text-apagado">
          {motivoVacio === "sin-montar" ? (
            <p>
              Organigrama no montado (<code className="rounded border border-linea bg-panel px-1.5 py-px font-mono text-[12px] text-tinta">LLMINBOX_ROLES_ALIAS</code> vacío
              o ilegible). Esto NO significa que nadie reporte a nadie: significa que este
              servicio no lo sabe todavía.
            </p>
          ) : (
            <p>
              La fuente firmada se leyó, pero no trae el campo <code className="rounded border border-linea bg-panel px-1.5 py-px font-mono text-[12px] text-tinta">jerarquia</code> (o
              viene vacío). El montaje está bien; revisa el contenido del fichero.
            </p>
          )}
        </div>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-linea">
          <table className="w-full min-w-[640px] border-collapse text-[13px]">
            <caption className="sr-only">Jerarquía legada de roles: de quién depende cada uno, quién depende de él y ruta de escalado</caption>
            <thead>
              <tr className="border-b border-linea bg-alzado text-left text-[11px] uppercase tracking-[.06em] text-tenue">
                <th scope="col" className="px-3 py-2 font-semibold">Rol</th>
                <th scope="col" className="px-3 py-2 font-semibold">Reporta a</th>
                <th scope="col" className="px-3 py-2 font-semibold">Capa</th>
                <th scope="col" className="px-3 py-2 font-semibold">Responsables inferiores</th>
                <th scope="col" className="px-3 py-2 font-semibold">Escalar</th>
              </tr>
            </thead>
            <tbody>
              {filas.map((f) => {
                const ruta = rutaEscalado(org.jerarquia, f.rol);
                return (
                  <tr key={f.rol} className="border-b border-linea last:border-b-0 odd:bg-panel even:bg-papel">
                    <th scope="row" className="px-3 py-2 text-left font-mono font-medium text-tinta">{f.rol}</th>
                    <td className="px-3 py-2 text-apagado">{f.reportaA ?? "— (raíz)"}</td>
                    <td className="px-3 py-2 text-apagado">{f.capa ?? "—"}</td>
                    <td className="px-3 py-2 text-apagado">
                      {f.subordinados.length ? f.subordinados.join(", ") : "ninguno"}
                    </td>
                    <td className="px-3 py-2">
                      {ruta.length ? <BotonEscalado destino={ruta[0]!} /> : <span className="text-tenue">sin ruta</span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
