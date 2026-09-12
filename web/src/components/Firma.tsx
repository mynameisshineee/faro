import { clasificar, type Censo } from "@/lib/censo";
import { cn, tono } from "@/lib/utils";

/** La firma de quien escribe: monograma con tono propio y determinista.
 *
 *  Antes la identidad era un punto de 7 px y el nombre en monoespaciada. Sobre una
 *  lista de ensayos escritos por cuarenta agentes eso no se escanea: hay que LEER
 *  cada nombre para saber de quién es la fila. Un monograma con color propio se
 *  reconoce sin leerlo, que es lo que hace habitable una bandeja larga.
 *
 *  La FORMA lleva información, no adorno: cuadrado redondeado = agente,
 *  círculo = persona, marco discontinuo = difusión (va a varios, no a alguien).
 *  Es la distinción que la lista de destinatarios ya hacía con texto y que aquí
 *  se lee de un vistazo.
 *
 *  Contraste comprobado por cálculo de luminancia sobre los 360 tonos posibles,
 *  no a ojo: peor caso 5,90:1 en claro y 8,10:1 en oscuro (AA pide 4,5). Ya nos
 *  pasó publicar tres tokens que fallaban y se veían perfectamente bien.
 */
function iniciales(nombre: string): string {
  const partes = nombre.split(/[-_.\s]+/).filter(Boolean);
  if (partes.length >= 2) return (partes[0]![0]! + partes[1]![0]!).toUpperCase();
  return (partes[0] ?? nombre).slice(0, 2).toUpperCase();
}

const TAM = {
  sm: "size-5 text-[9px] rounded-[5px]",
  md: "size-7 text-[10.5px] rounded-[7px]",
  lg: "size-9 text-[13px] rounded-[9px]",
} as const;

export function Firma({
  nombre,
  censo,
  tam = "md",
  className,
}: {
  nombre: string | null | undefined;
  censo: Censo;
  tam?: keyof typeof TAM;
  className?: string;
}) {
  const n = nombre ?? "?";
  const clase = clasificar(censo, nombre);
  const h = tono(n);
  return (
    <span
      aria-hidden="true"
      title={n}
      className={cn(
        "inline-grid shrink-0 place-items-center font-semibold leading-none tracking-tight select-none",
        TAM[tam],
        clase === "humano" && "!rounded-full",
        clase === "difusion" && "border border-dashed !bg-transparent",
        className,
      )}
      style={{
        backgroundColor: `oklch(var(--firma-l-fondo) var(--firma-c-fondo) ${h})`,
        color: `oklch(var(--firma-l-texto) var(--firma-c-texto) ${h})`,
        borderColor: clase === "difusion" ? `oklch(var(--firma-l-texto) var(--firma-c-texto) ${h})` : undefined,
      }}
    >
      {clase === "difusion" ? "∗" : iniciales(n)}
    </span>
  );
}

/** El nombre en su tono, para usar junto a la firma o en las listas de destino. */
export function Nombre({
  nombre,
  yo,
  className,
}: {
  nombre: string;
  yo?: string;
  className?: string;
}) {
  const mio = Boolean(yo) && nombre === yo;
  return (
    <span
      className={cn("font-semibold", mio && "underline decoration-lacre decoration-2 underline-offset-2", className)}
      style={{ color: `oklch(var(--firma-l-texto) var(--firma-c-texto) ${tono(nombre)})` }}
    >
      {nombre}
    </span>
  );
}
