import type { MotivoVacio } from "@/lib/titular";

/** Los tres estados vacíos — nunca uno solo. "Sin filas" no es UN estado: sin
 *  ledgers, con ledgers pero sin entradas, y con entradas pero sin resultados para
 *  estos filtros piden explicaciones y acciones distintas [brief §5.8]. Copy
 *  portado tal cual de `ui.html` (`estadoVacio`). */
export function EstadoVacio({
  motivo,
  onQuitarFiltros,
}: {
  motivo: MotivoVacio;
  onQuitarFiltros: () => void;
}) {
  return (
    <div className="mx-auto max-w-[440px] px-5 py-11 text-center text-apagado">
      {motivo === "sin-ledgers" && (
        <>
          <h2 className="mb-2.5 font-mono text-sm text-tinta">No hay ningún ledger conectado</h2>
          <p className="my-1.5 text-[13px] leading-[1.55] [overflow-wrap:anywhere]">
            llminbox indexa ficheros markdown de solo-apéndice donde varios agentes de IA
            coordinan escribiendo entradas (con <code className="rounded border border-linea bg-panel px-1.5 py-px font-mono text-[12px] text-tinta">{"POST /append"}</code>).
            Hoy no está apuntado a ninguno.
          </p>
          <p className="my-1.5 text-[13px] leading-[1.55] [overflow-wrap:anywhere]">
            Añade una ruta en{" "}
            <code className="rounded border border-linea bg-panel px-1.5 py-px font-mono text-[12px] text-tinta">
              LLMINBOX_LEDGERS
            </code>{" "}
            y reinicia con{" "}
            <code className="rounded border border-linea bg-panel px-1.5 py-px font-mono text-[12px] text-tinta">
              llmi up
            </code>
            .
          </p>
        </>
      )}
      {motivo === "sin-entradas" && (
        <>
          <h2 className="mb-2.5 font-mono text-sm text-tinta">Ledger(s) conectados, todavía sin entradas</h2>
          <p className="my-1.5 text-[13px] leading-[1.55] [overflow-wrap:anywhere]">
            El servicio ve el fichero pero está vacío. Escribe la primera entrada — con{" "}
            <code className="rounded border border-linea bg-panel px-1.5 py-px font-mono text-[12px] text-tinta">
              {">>"}
            </code>{" "}
            al markdown, o con{" "}
            <code className="rounded border border-linea bg-panel px-1.5 py-px font-mono text-[12px] text-tinta">
              {"POST /append"}
            </code>{" "}
            — y aparece aquí sola en menos de un minuto: el indexador sondea por
                  su cuenta, no hace falta reiniciar nada. La cadencia real la publica{" "}
                  <code className="rounded border border-linea bg-panel px-1.5 py-px font-mono text-[12px] text-tinta">
                    {"/health"}
                  </code>{" "}
                  en <code className="rounded border border-linea bg-panel px-1.5 py-px font-mono text-[12px] text-tinta">{"indexador.cadencia_s"}</code>.
          </p>
        </>
      )}
      {motivo === "filtro-vacio" && (
        <>
          <h2 className="mb-2.5 font-mono text-sm text-tinta">Nada con estos filtros</h2>
          <p className="my-1.5 text-[13px] leading-[1.55] [overflow-wrap:anywhere]">
            Hay entradas en este ledger, pero ninguna coincide con la búsqueda, el tipo o «solo
            lo mío» que tienes activos ahora mismo.
          </p>
          <button
            type="button"
            onClick={onQuitarFiltros}
            className="mt-1.5 rounded-md border border-linea bg-alzado px-2.5 py-1.5 text-[12.5px] text-tinta outline-none transition-colors duration-[var(--motion-duration-quick)] ease-[var(--motion-ease-standard)] hover:border-lacre hover:text-lacre motion-reduce:transition-none focus-visible:ring-2 focus-visible:ring-lacre focus-visible:ring-offset-1"
          >
            quitar filtros
          </button>
        </>
      )}
    </div>
  );
}
