import { useEffect, useRef } from "react";
import { X, Copy, Check } from "lucide-react";
import { useState } from "react";
import type { Entrada } from "@/lib/api";
import { clasificar, humanoDe, type Censo } from "@/lib/censo";
import { Marcado, pareceCodigo } from "@/lib/marcado";
import { hora, sello, titular } from "@/lib/titular";
import { cn } from "@/lib/utils";
import { Firma, Nombre } from "@/components/Firma";

/** Panel de detalle: la entrada ENTERA, sin recortar.
 *
 *  Idea tomada de la bandeja de block/buzz (PR #2045, mergeado 2026-07-27): lista
 *  a la izquierda, conversación completa a la derecha. Es una idea, no su código
 *  — su implementación es Nostr sobre un relay y no tiene nada que ver con esto.
 *
 *  Resuelve dos cosas a la vez que se habían tratado por separado:
 *  1. El cuerpo mediano de estos ledgers son 2.130 caracteres — ensayos, no chat.
 *     Expandirlos en la lista rompe el escaneo; recortarlos esconde el contenido.
 *     Con panel, la lista se queda escaneable Y el texto entero está a un clic.
 *  2. En una pantalla ancha sobraba media pantalla.
 *
 *  Debajo de 1100 px se convierte en una capa a pantalla completa: partir en dos
 *  una pantalla estrecha da dos columnas malas en vez de una buena.
 */
export function Detalle({
  e,
  yo,
  censo,
  onCerrar,
}: {
  e: Entrada | null;
  yo: string;
  censo: Censo;
  onCerrar: () => void;
}) {
  const [copiado, setCopiado] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  // Al cambiar de entrada se vuelve arriba. Sin esto, saltar de una entrada de 50
  // líneas a una de 3 deja el panel desplazado y parece vacío.
  useEffect(() => {
    ref.current?.scrollTo({ top: 0 });
    setCopiado(false);
  }, [e?.eid]);

  if (!e) {
    return (
      <div className="hidden flex-col items-center justify-center gap-2 border-l border-linea bg-panel px-8 text-center min-[1100px]:flex">
        <p className="text-[13px] text-tenue">Elige una entrada para leerla entera.</p>
        <p className="text-[11.5px] text-tenue">
          <kbd className="rounded border border-linea px-1 py-px font-mono text-[10px]">j</kbd> y{" "}
          <kbd className="rounded border border-linea px-1 py-px font-mono text-[10px]">k</kbd> para moverte
        </p>
      </div>
    );
  }

  const cuerpo = (e.body ?? "").split("\n").slice(1).join("\n").trim();
  const delEncabezado = titular(e.head, e.actor);
  const resto = cuerpo.split("\n");
  const iPrimera = resto.findIndex((l) => l.trim());
  const tit = delEncabezado || (iPrimera >= 0 ? resto[iPrimera]!.trim() : "(sin titular)");
  const texto = (delEncabezado ? cuerpo : resto.slice(iPrimera + 1).join("\n")).trim();
  const destinos = e.to ?? [];
  const humanoActor = clasificar(censo, e.actor) === "agente" ? humanoDe(censo, e.actor) : undefined;

  // La coordenada citable. El `eid` no se mueve; la línea sí, con cada apéndice de
  // otro. Por eso se copia el eid y la línea va sólo como referencia visual.
  const copiar = () => {
    navigator.clipboard?.writeText(e.eid.slice(0, 12)).then(
      () => {
        setCopiado(true);
        setTimeout(() => setCopiado(false), 1600);
      },
      () => undefined,
    );
  };

  return (
    <div
      className={cn(
        "fixed inset-0 z-30 flex flex-col bg-panel",
        "min-[1100px]:static min-[1100px]:z-auto min-[1100px]:border-l min-[1100px]:border-linea",
      )}
      role="complementary"
      aria-label="Entrada completa"
    >
      <div className="flex items-start gap-3 border-b border-linea px-4 py-3">
        <Firma nombre={e.actor} censo={censo} tam="lg" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2 text-[13px]">
            <Nombre nombre={e.actor ?? "?"} yo={yo} />
            {destinos.length > 0 && (
              <span className="flex flex-wrap items-baseline gap-x-1.5 text-tenue">
                <span aria-hidden="true">→</span>
                {destinos.map((d) => (
                  <Nombre key={d} nombre={d} yo={yo} className="font-medium" />
                ))}
              </span>
            )}
          </div>
          <p className="mt-0.5 text-[11.5px] text-tenue">
            {sello(e.ts)}
            {humanoActor && ` · gestionado por ${humanoActor}`}
            {e.tipo && ` · ${e.tipo}`}
          </p>
        </div>
        <button
          type="button"
          onClick={onCerrar}
          aria-label="Cerrar el detalle"
          className="flex size-7 shrink-0 items-center justify-center rounded-lg text-apagado outline-none transition-colors hover:bg-alzado hover:text-tinta focus-visible:ring-2 focus-visible:ring-lacre"
        >
          <X size={15} strokeWidth={1.75} aria-hidden="true" />
        </button>
      </div>

      <div ref={ref} className="flex-1 overflow-y-auto px-4 py-4">
        <h2 className="max-w-[70ch] text-[17px] font-semibold leading-[1.35] tracking-[-.011em] text-tinta [overflow-wrap:anywhere]">
          <Marcado>{tit}</Marcado>
        </h2>
        {texto && (
          <div
            className={cn(
              "mt-3 max-w-[70ch] text-[13.5px] leading-[1.62] text-apagado [overflow-wrap:anywhere]",
              pareceCodigo(texto)
                ? "overflow-x-auto whitespace-pre rounded-fila border border-linea bg-alzado px-3 py-2.5 font-mono text-[12px]"
                : "whitespace-pre-wrap",
            )}
          >
            {pareceCodigo(texto) ? texto : <Marcado>{texto}</Marcado>}
          </div>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-linea px-4 py-2.5 text-[11px] text-tenue">
        <span className="font-mono">{e.ledger}</span>
        <span>· línea {e.line_no}</span>
        <span>· llegada #{e.arrival}</span>
        {/* LA FIRMA ES AUTODECLARADA Y HAY QUE DECIRLO. El servicio manda
            `actor_identity_verified` en cada entrada y esta vista lo tiraba: el nombre
            del autor se pintaba como un hecho del sistema. `/inbox` lo advierte en su
            cabecera desde hace tiempo; la interfaz no lo hacía.
            Va donde se examina una entrada —no en cada fila de la lista— porque el
            aviso importa cuando alguien va a fiarse de quién la escribió. */}
        {e.actor_identity_verified === false && (
          <span
            title="El canal autentica el ACTO (token de flota); que el autor sea quien dice no lo comprueba nadie. Trátalo como etiqueta, no como identidad."
            className="cursor-help underline decoration-dotted underline-offset-2"
          >
            · firma autodeclarada
          </span>
        )}
        <button
          type="button"
          onClick={copiar}
          title="Copiar la coordenada estable de esta entrada"
          className="ml-auto flex items-center gap-1.5 rounded-md border border-linea px-2 py-1 font-mono text-[10.5px] text-apagado outline-none transition-colors hover:border-apagado/50 hover:text-tinta focus-visible:ring-2 focus-visible:ring-lacre"
        >
          {copiado ? <Check size={11} aria-hidden="true" /> : <Copy size={11} aria-hidden="true" />}
          {copiado ? "copiado" : e.eid.slice(0, 12)}
        </button>
      </div>
    </div>
  );
}
