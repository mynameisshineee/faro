// El front no leía ninguna cabecera, así que `x-filas-capadas` no la consumía nadie.
//   node web/src/lib/cuenta.test.mjs
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const aqui = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(aqui, "cuenta.ts"), "utf8");
const cuerpo = src.replace(
  "export function textoDeCuenta(n: number, capadas: number | null): string {",
  "function textoDeCuenta(n, capadas) {",
);
if (cuerpo.includes(": number"))
  throw new Error("el arnés ya no despoja los tipos: la firma cambió y esto no lo sigue");
const { textoDeCuenta } = await import(
  "data:text/javascript," + encodeURIComponent(cuerpo + "\nexport {textoDeCuenta};")
);

let malos = 0;
const caso = (nombre, real, esperado) => {
  const ok = esperado instanceof RegExp ? esperado.test(real) : real === esperado;
  if (!ok) { malos++; console.log(`  ✗ ${nombre}\n      dio: ${JSON.stringify(real)}`); }
  else console.log(`  ✓ ${nombre}`);
};

caso("sin recorte dice el número a secas", textoDeCuenta(37, null), "37 entradas");
caso("cero sin recorte no miente", textoDeCuenta(0, null), "0 entradas");

// ── LA AVERÍA QUE ESTO CURA ────────────────────────────────────────────────────
caso("con recorte lo DICE", textoDeCuenta(10, 10), /recortado/);
caso("y dice que hay más", textoDeCuenta(10, 10), /hay más/);
caso("el número recortado no se lee como el total",
  /^10 entradas$/.test(textoDeCuenta(10, 10)) ? "SE LEE COMO EL TOTAL" : "ok", "ok");

// ⊖ el 120 cableado: 120 sin recorte declarado NO puede marcarse como capado
caso("120 exactas sin cabecera de recorte no inventan un tope",
  textoDeCuenta(120, null), "120 entradas");

// ── ESTÁ CONECTADO, no sólo existe ─────────────────────────────────────────────
// La avería original no era que faltara la función: era que el backend servía
// `x-filas-capadas` y el front no leía NINGUNA cabecera. Una cura que dejara la función
// escrita y sin llamar sería el mismo defecto con más código.
const api = readFileSync(join(aqui, "api.ts"), "utf8");
const queries = readFileSync(join(aqui, "queries.ts"), "utf8");
const app = readFileSync(join(aqui, "..", "App.tsx"), "utf8");
caso("api.ts LEE la cabecera del recorte",
  /x-filas-capadas/.test(api) ? "ok" : "NO LA LEE", "ok");
caso("queries.ts usa la variante que la trae",
  /entradasConCorte/.test(queries) ? "ok" : "SIGUE CON api.entradas", "ok");
caso("App.tsx usa textoDeCuenta y no el 120 cableado",
  /textoDeCuenta/.test(app) && !/=== 120 \? "\+"/.test(app) ? "ok" : "SIGUE CABLEADO", "ok");

console.log(malos ? `\n${malos} fallo(s)` : "\ncuenta: todo verde");
process.exit(malos ? 1 : 0);
