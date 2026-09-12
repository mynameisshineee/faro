// C10 [spec design STATES-…-20260908 §2.3, notas qa N1 adoptadas #1280] — la
// invariante «intervalo de sondeo ≤ plazo stale» como caso de UNIDAD.
//   node web/src/lib/sondeo.test.mjs
//
// Los dos valores se leen de UNA configuración (`sondeo.ts`). El plazo stale
// del servidor aún NO viaja en el wire (medido: ni /organization ni la fila de
// revisión lo traen) — `PLAZO_STALE_NATIVO_MS` es una COTA DECLARADA del cliente,
// y así lo dice su comentario; cuando el wire lo sirva, se re-ancla ahí.
//
// El segundo bloque es el falsador de CABLEADO: que las constantes existan no
// basta — los 2 hooks nativos tienen que USAR el intervalo declarado. Sin ese
// bloque, renombrar la constante y no conectarla saldría verde.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import ts from "typescript";

const aqui = dirname(fileURLToPath(import.meta.url));
let malos = 0;
const caso = (nombre, real, esperado) => {
  const ok = real === esperado;
  if (!ok) { malos++; console.log(`  ✗ ${nombre}\n      dio:      ${JSON.stringify(real)}\n      esperado: ${JSON.stringify(esperado)}`); }
  else console.log(`  ✓ ${nombre}`);
};
const transpilar = (fichero) => {
  const src = readFileSync(join(aqui, fichero), "utf8");
  const { outputText, diagnostics } = ts.transpileModule(src, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
    reportDiagnostics: true,
  });
  if (diagnostics && diagnostics.length)
    throw new Error(`${fichero} no transpila limpio: ${diagnostics.map((d) => d.messageText).join("; ")}`);
  return outputText;
};

// ── C10: invariante intervalo ≤ plazo stale, ambos de configuración ─────────
const { SONDEO_NATIVO_MS, PLAZO_STALE_NATIVO_MS } = await import(
  "data:text/javascript," + encodeURIComponent(transpilar("sondeo.ts"))
);

caso("el intervalo de sondeo está declarado y es positivo",
  typeof SONDEO_NATIVO_MS === "number" && SONDEO_NATIVO_MS > 0, true);
caso("el plazo stale asumido está declarado y es positivo",
  typeof PLAZO_STALE_NATIVO_MS === "number" && PLAZO_STALE_NATIVO_MS > 0, true);
caso("C10: intervalo ≤ plazo stale (sondear más lento que el plazo es pintar dato rancio como fresco)",
  SONDEO_NATIVO_MS <= PLAZO_STALE_NATIVO_MS, true);

// ── cableado: los 2 hooks nativos usan el intervalo DECLARADO ────────────────
const queries = transpilar("queries.ts");
const usos = (queries.match(/refetchInterval:\s*SONDEO_NATIVO_MS/g) ?? []).length;
caso("los 2 hooks nativos sondean con la constante declarada (no un número suelto ni nada)", usos, 2);

console.log(malos ? `\n${malos} fallo(s)` : "\nsondeo: todo verde");
process.exit(malos ? 1 : 0);
