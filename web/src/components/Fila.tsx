import { Fragment } from "react";
import type { Entrada } from "@/lib/api";
import { clasificar, type Censo } from "@/lib/censo";
import { Marcado } from "@/lib/marcado";
import { hora, titular } from "@/lib/titular";
import { cn } from "@/lib/utils";
import { Firma, Nombre } from "@/components/Firma";

/** Peso visual por tipo — la jerarquía que el 41%/2% pide [brief §5.3, §2.1]:
 *  HEARTBEAT es ruido de fondo (41% del volumen, cero decisión); REQUEST/HELD son
 *  los compromisos reales (el 0,2% que sí exige una decisión humana) y deben ganar
 *  el escaneo. Sin tipo → sin badge: un badge vacío es peor que ausencia, dada la
 *  deuda de tipado real (hasta 99% en un ledger). */
const PESO_TIPO: Record<string, string> = {
  HEARTBEAT: "border-linea text-tenue opacity-60",
  REQUEST: "border-current text-aviso",
  HELD: "border-current text-aviso",
  ACK: "border-current text-bien",
  DONE: "border-current text-bien",
};
const badgeTipo = (tipo: string) => PESO_TIPO[tipo] ?? "border-linea text-apagado";

/** Una entrada en la LISTA: dos líneas, sin cuerpo.
 *
 *  El cuerpo se fue al panel de detalle y eso cambia lo que esta fila tiene que
 *  hacer. Antes cada fila cargaba con su titular, un extracto del cuerpo, un
 *  "ver más" y la procedencia: cinco filas por pantalla sobre un canal de 300
 *  entradas/hora. Ahora caben veinte y se escanean, que es lo que una lista es.
 *
 *  La entrada entera sigue estando a un clic, o a una tecla. */
export function Fila({
  e,
  yo,
  censo,
  activa,
  onAbrir,
}: {
  e: Entrada;
  yo: string;
  censo: Censo;
  activa: boolean;
  onAbrir: () => void;
}) {
  const destinos = e.to ?? [];
  const mia = Boolean(yo) && (e.actor === yo || destinos.includes(yo));
  const heartbeat = e.tipo === "HEARTBEAT";

  const delEncabezado = titular(e.head, e.actor);
  const primeraDelCuerpo = (e.body ?? "").split("\n").slice(1).find((l) => l.trim())?.trim();
  const textoTitular = delEncabezado || primeraDelCuerpo || "(sin titular)";

  return (
    <button
      type="button"
      onClick={onAbrir}
      aria-current={activa || undefined}
      className={cn(
        "group relative flex w-full gap-2.5 px-4 py-2 text-left outline-none transition-colors duration-[var(--motion-duration-quick)] ease-[var(--motion-ease-standard)] motion-reduce:transition-none",
        "hover:bg-alzado/70 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-lacre",
        activa && "bg-alzado",
        heartbeat && !activa && "opacity-[.6] hover:opacity-100",
      )}
    >
      {/* Filete al margen para «esto va contigo», como la marca de lápiz en el
          margen de un libro de cuentas. Teñir la fila entera no señala nada
          cuando hay cien seguidas. */}
      {mia && <span aria-hidden="true" className="absolute inset-y-0 left-0 w-[3px] bg-lacre" />}

      <Firma nombre={e.actor} censo={censo} tam="sm" className="mt-[3px]" />

      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-x-1.5 text-[11.5px] leading-none">
          <Nombre nombre={e.actor ?? "?"} yo={yo} className="shrink-0" />
          {destinos.length > 0 && (
            <span className="flex min-w-0 items-baseline gap-x-1 truncate text-tenue">
              <span aria-hidden="true">→</span>
              {destinos.slice(0, 3).map((d, i) => (
                <Fragment key={d}>
                  {i > 0 && <span aria-hidden="true">·</span>}
                  <Nombre nombre={d} yo={yo} className="font-medium" />
                </Fragment>
              ))}
              {destinos.length > 3 && <span>+{destinos.length - 3}</span>}
            </span>
          )}
          {e.tipo && !heartbeat && (
            <span className={cn("shrink-0 rounded border px-1 py-px text-[9px] font-medium tracking-wide", badgeTipo(e.tipo))}>
              {e.tipo}
            </span>
          )}
          {clasificar(censo, e.actor) === "humano" && (
            <span className="shrink-0 rounded-[3px] border border-bien px-1 py-px text-[8.5px] font-medium uppercase tracking-[.05em] text-bien">
              persona
            </span>
          )}
          <span className="ml-auto shrink-0 tabular-nums text-tenue">{hora(e.ts)}</span>
        </div>
        {/* Dos líneas como tope. Una sola parte los titulares de esta flota por la
            mitad —son largos por una patología medida de la herramienta, no por
            capricho— y tres devuelven la lista a donde estaba. */}
        <p
          className={cn(
            "mt-[3px] line-clamp-2 text-[13.5px] leading-[1.42] [overflow-wrap:anywhere]",
            heartbeat ? "text-tenue" : activa ? "text-tinta" : "text-apagado group-hover:text-tinta",
          )}
        >
          <Marcado>{textoTitular}</Marcado>
        </p>
      </div>
    </button>
  );
}
