/** Divisor cronológico de "lo nuevo" — se apoya en el cursor que YA existe (lo
 *  avanza la CLI u otro agente); esta página solo lo lee y lo dibuja, nunca lo
 *  mueve [brief §5.6, §1.1].
 *
 *  Va como PASTILLA centrada sobre una regla, no como banda de color a todo lo
 *  ancho: la banda pesaba más que las entradas que separa, y lo que importa es lo
 *  que hay debajo de la línea, no la línea. */
export function Separador({ n }: { n: number }) {
  return (
    <div className="relative flex items-center justify-center py-2.5" role="separator">
      <span aria-hidden="true" className="absolute inset-x-4 h-px bg-lacre opacity-30" />
      <span className="relative rounded-full border border-lacre/35 bg-papel px-2.5 py-[3px] text-[11px] font-medium text-lacre shadow-[0_1px_2px_rgba(0,0,0,.04)]">
        {n} nueva{n === 1 ? "" : "s"} desde tu última visita
      </span>
    </div>
  );
}
