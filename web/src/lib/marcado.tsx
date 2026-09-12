import { Fragment, type ReactNode } from "react";

/** Markdown EN LÍNEA, subconjunto seguro, sin dependencia.
 *
 *  La flota escribe markdown y la interfaz lo estaba enseñando en crudo: en la
 *  captura que motivó esto se leían los asteriscos («— **tests LARGOS, DE MALA
 *  IDEA**») y las comillas invertidas escapadas dentro de los titulares. Un
 *  producto que se vende como «lee el markdown que ya escribís» y luego enseña la
 *  sintaxis se desmiente a sí mismo en la primera pantalla.
 *
 *  Se devuelven NODOS de React, no HTML: React escapa el texto por construcción,
 *  así que no hay superficie de inyección que proteger. Es la misma razón por la
 *  que `react-markdown` se retiró — con `rehype-raw` habría HTML de agente vivo en
 *  el DOM, y sin él no hacía falta la dependencia.
 *
 *  Lo que NO se hace, a propósito:
 *  - Bloques (títulos, listas, tablas): el titular es una línea; el cuerpo se
 *    muestra tal cual porque su forma ES información (una tabla alineada dice algo).
 *  - `_cursiva_`: `snake_case` es constante en texto de agentes y saldría en
 *    cursiva media frase. Sólo `*cursiva*`.
 *  - Enlaces PULSABLES: el texto lo escribe otro agente. Se le da forma de enlace
 *    para que se lea, y no se convierte en superficie de clic — un ledger no es
 *    sitio para estrenar el vector de phishing.
 */
const EN_LINEA = new RegExp(
  [
    "(`[^`\\n]+`)", // 1 código — primero: dentro no se interpreta nada más
    "(\\*\\*[^*\\n]+\\*\\*)", // 2 fuerte
    "(~~[^~\\n]+~~)", // 3 tachado
    "(\\*[^*\\n]+\\*)", // 4 énfasis
    "(@[\\w][\\w.-]{1,31})", // 5 mención
    "((?:https?://|www\\.)[^\\s<>()\\[\\]]+)", // 6 enlace (no pulsable)
  ].join("|"),
  "g",
);

export function Marcado({ children, className }: { children: string; className?: string }) {
  return <span className={className}>{trocear(children ?? "")}</span>;
}

export function trocear(texto: string): ReactNode[] {
  const salida: ReactNode[] = [];
  let ultimo = 0;
  let n = 0;
  EN_LINEA.lastIndex = 0;
  for (let m = EN_LINEA.exec(texto); m; m = EN_LINEA.exec(texto)) {
    if (m.index > ultimo) salida.push(texto.slice(ultimo, m.index));
    const k = `m${n++}`;
    const [, codigo, fuerte, tachado, enfasis, mencion, enlace] = m;
    if (codigo) {
      salida.push(
        <code
          key={k}
          className="rounded-[4px] border border-linea bg-alzado px-[.32em] py-[.06em] font-mono text-[.88em] text-tinta"
        >
          {codigo.slice(1, -1)}
        </code>,
      );
    } else if (fuerte) {
      salida.push(
        <strong key={k} className="font-semibold text-tinta">
          {fuerte.slice(2, -2)}
        </strong>,
      );
    } else if (tachado) {
      salida.push(
        <s key={k} className="text-apagado">
          {tachado.slice(2, -2)}
        </s>,
      );
    } else if (enfasis) {
      salida.push(<em key={k}>{enfasis.slice(1, -1)}</em>);
    } else if (mencion) {
      salida.push(
        <span
          key={k}
          className="rounded-[4px] bg-mencion px-[.3em] py-[.06em] font-medium text-mencion-t"
        >
          {mencion}
        </span>,
      );
    } else if (enlace) {
      salida.push(
        <span key={k} className="text-lacre underline decoration-dotted underline-offset-2" title={enlace}>
          {enlace.length > 48 ? `${enlace.slice(0, 45)}…` : enlace}
        </span>,
      );
    }
    ultimo = m.index + m[0].length;
  }
  if (ultimo < texto.length) salida.push(texto.slice(ultimo));
  return salida.map((x, i) => (typeof x === "string" ? <Fragment key={`t${i}`}>{x}</Fragment> : x));
}

/** ¿El cuerpo es CÓDIGO o es prosa? Decide si se pinta en monoespaciada.
 *
 *  Antes iba todo a un `<pre>` monoespaciado con borde, así que el bloque más
 *  pesado de cada fila era el CUERPO —lo menos importante— y el titular, que es
 *  el que viaja solo, quedaba debajo en peso visual. Jerarquía al revés.
 *  Se mira la forma, no el contenido: valla de código, sangría sostenida, tabla
 *  alineada o proporción alta de líneas cortas con símbolos. */
export function pareceCodigo(cuerpo: string): boolean {
  if (/^```|\n```/.test(cuerpo)) return true;
  const lineas = cuerpo.split("\n").filter((l) => l.trim());
  if (lineas.length < 2) return false;
  const sangradas = lineas.filter((l) => /^(\s{4,}|\t)/.test(l)).length;
  const tabla = lineas.filter((l) => l.includes("|")).length;
  return sangradas / lineas.length > 0.6 || tabla / lineas.length > 0.6;
}
