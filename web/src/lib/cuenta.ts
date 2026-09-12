/** Cuántas entradas se enseñan, y si eso es TODO lo que hay o todo lo que se dio.
 *
 *  El backend ya distingue las dos cosas: sirve `x-filas-capadas: N` cuando ha recortado
 *  la lista, con un comentario que dice para qué existe la cabecera —«distinguir *esto es
 *  todo lo que hay* de *esto es todo lo que te doy*»—. El front no leía NINGUNA cabecera
 *  de respuesta, así que la cabecera existía y no la llamaba nadie.
 *
 *  Y el sustituto era peor que nada: `${n}${n === 120 ? "+" : ""}` comparaba contra un 120
 *  cableado que ni siquiera es el `limit` que se pide (`queries.ts` pide 400 en un sitio y
 *  120 en otro). Medido el 2026-09-02: con `limit=400&cuerpo=true` el servicio devuelve 10
 *  filas y la interfaz decía «10 entradas», a secas, habiendo decenas de miles. El lector
 *  concluye que el bus está vacío.
 */
export function textoDeCuenta(n: number, capadas: number | null): string {
  if (capadas == null) return `${n} entradas`;
  // El «+» no basta y por eso va el número: «10+» no dice si faltan dos o veinte mil, y
  // esta interfaz existe para leer un bus donde faltar veinte mil es lo normal.
  return `${n} entradas (recortado a ${capadas} — hay más)`;
}
