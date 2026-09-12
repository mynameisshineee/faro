import type { Cursores, Entrada } from "@/lib/api";

/** Dónde insertar el separador de no-leídos. Cada fila se compara contra el
 *  cursor de SU PROPIO ledger — `arrival` es un contador local por ledger, no
 *  comparable entre ledgers distintos. Solo activo con un agente elegido en "leer
 *  como" y su cursor cargado. Si TODO lo cargado es nuevo, no hay "antes" que
 *  contrastar dentro de esta ventana: se marca arriba en vez de partir la lista en
 *  medio de la nada [brief §5.6; ui.html `pintar`]. */
export function calcularSeparador(
  filas: Entrada[],
  yo: string,
  cursores: Cursores | undefined,
): { marcas: boolean[]; n: number } {
  const marcas = filas.map(() => false);
  if (!yo || !cursores) return { marcas, n: 0 };
  const c = cursores;
  const esNueva = (r: Entrada) => r.arrival > (c[r.ledger] ?? -1);
  const n = filas.filter(esNueva).length;
  if (n === 0) return { marcas, n };
  if (n === filas.length) {
    marcas[0] = true;
    return { marcas, n };
  }
  for (let i = 0; i < filas.length; i++) {
    const actual = filas[i];
    if (actual && !esNueva(actual)) {
      marcas[i] = true;
      break;
    }
  }
  return { marcas, n };
}
