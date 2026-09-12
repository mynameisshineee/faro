import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export const cn = (...i: ClassValue[]) => twMerge(clsx(i));

/** Tono determinista por nombre: el mismo agente sale siempre del mismo color en
 *  cualquier máquina, sin configurarlo. Se evita 80-150º (verdes) porque ahí viven
 *  los colores de estado, que significan otra cosa. */
export function tono(nombre: string | null | undefined): number {
  let h = 0;
  for (const c of nombre ?? "?") h = (h * 31 + c.codePointAt(0)!) % 360;
  return h > 80 && h < 150 ? (h + 90) % 360 : h;
}
