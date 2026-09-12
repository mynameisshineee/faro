import { useId } from "react";
import { Menu, Search } from "lucide-react";
import { cn } from "@/lib/utils";

/** Cabecera: título del ledger activo, filtros (búsqueda / tipo / solo lo mío),
 *  contador y el hamburguesa móvil [brief §5.9]. Filtros nativos a propósito —
 *  `<select>`/`<input>` ya dan teclado + AT gratis; inventar un combobox no suma
 *  nada aquí [brief §7]. */
export function Header({
  ledger,
  tipos,
  buscar,
  onBuscarChange,
  tipo,
  onTipoChange,
  soloMias,
  onSoloMiasChange,
  cuenta,
  deshabilitado,
  menuAbierto,
  onAbrirMenu,
}: {
  ledger: string;
  tipos: string[];
  buscar: string;
  onBuscarChange: (v: string) => void;
  tipo: string;
  onTipoChange: (v: string) => void;
  soloMias: boolean;
  onSoloMiasChange: (v: boolean) => void;
  cuenta: string;
  deshabilitado: boolean;
  menuAbierto: boolean;
  onAbrirMenu: () => void;
}) {
  const idBuscar = useId();
  const idTipo = useId();
  const control =
    "rounded-lg border border-linea bg-papel px-2.5 py-[6px] text-[13px] text-tinta outline-none transition-colors duration-[var(--motion-duration-quick)] hover:border-apagado/50 focus-visible:ring-2 focus-visible:ring-lacre focus-visible:ring-offset-1 disabled:opacity-50 motion-reduce:transition-none";

  return (
    <header className="flex flex-wrap items-center gap-2 border-b border-linea bg-panel px-4 py-2.5">
      <button
        id="abrir-menu"
        type="button"
        onClick={onAbrirMenu}
        aria-label="Abrir la lista de ledgers"
        aria-expanded={menuAbierto}
        aria-controls="barra-lateral"
        className="flex size-[30px] flex-none items-center justify-center rounded-lg border border-linea bg-alzado text-tinta outline-none focus-visible:ring-2 focus-visible:ring-lacre focus-visible:ring-offset-1 min-[760px]:hidden"
      >
        <Menu size={16} strokeWidth={1.5} aria-hidden="true" />
      </button>

      <div className="mr-1 min-w-0">
        <h1 className="truncate text-[15px] font-semibold tracking-[-.012em] text-tinta">{ledger || "todos"}</h1>
        {cuenta && <p className="text-[11px] tabular-nums text-tenue">{cuenta}</p>}
      </div>
      <span className="flex-1" />

      <label htmlFor={idBuscar} className="sr-only">
        Buscar en el cuerpo
      </label>
      <div className="relative">
        <Search
          size={13}
          strokeWidth={1.75}
          aria-hidden="true"
          className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-tenue"
        />
        <input
          id={idBuscar}
          type="search"
          placeholder="buscar en el cuerpo…"
          value={buscar}
          onChange={(e) => onBuscarChange(e.target.value)}
          disabled={deshabilitado}
          className={cn(control, "min-w-[168px] max-w-full pl-7")}
        />
      </div>

      <label htmlFor={idTipo} className="sr-only">
        Filtrar por tipo
      </label>
      <select
        id={idTipo}
        value={tipo}
        onChange={(e) => onTipoChange(e.target.value)}
        disabled={deshabilitado}
        className={cn(control, "cursor-pointer")}
      >
        <option value="">todos los tipos</option>
        {tipos.map((t) => (
          <option key={t} value={t}>
            {t}
          </option>
        ))}
      </select>

      {/* Interruptor, no casilla suelta: es el filtro que contesta la pregunta del
          producto («¿qué es MÍO?»), y una casilla de sistema al lado de dos
          controles con forma propia parecía un resto de formulario. */}
      <label
        className={cn(
          "flex cursor-pointer select-none items-center gap-1.5 rounded-lg border px-2.5 py-[6px] text-[12.5px] transition-colors duration-[var(--motion-duration-quick)] motion-reduce:transition-none",
          soloMias ? "border-lacre bg-lacre-d font-medium text-lacre" : "border-linea text-apagado hover:border-apagado/50",
          deshabilitado && "pointer-events-none opacity-50",
        )}
      >
        <input
          type="checkbox"
          checked={soloMias}
          onChange={(e) => onSoloMiasChange(e.target.checked)}
          disabled={deshabilitado}
          className="sr-only"
        />
        <span
          aria-hidden="true"
          className={cn("size-[7px] rounded-full transition-colors", soloMias ? "bg-lacre" : "bg-linea")}
        />
        solo lo mío
      </label>
    </header>
  );
}
