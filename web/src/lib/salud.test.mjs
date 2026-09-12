// El banner atribuía CUALQUIER `ok:false` al indexador. `ok` es falso por tres causas
// distintas [servicio.py:3268] y sólo una era el indexador.
//   node web/src/lib/salud.test.mjs
// Importa el fichero real quitándole la firma tipada, para que no exista una segunda
// copia de la lógica que pueda derivar.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const aqui = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(aqui, "salud.ts"), "utf8");
// Se quita el import de tipos y la firma tipada. Si esta sustitución dejara de casar,
// el `cuerpo` seguiría siendo TS válido pero no JS y el import fallaría RUIDOSAMENTE —
// que es lo que se quiere: un arnés que se rompe en silencio no prueba nada.
const cuerpo = src
  .replace(/^import type .*\n\n/, "")
  .replace(
    "export function motivoSalud(salud: Salud | undefined, desaparecidas: number): string | null {",
    "function motivoSalud(salud, desaparecidas) {",
  );
if (cuerpo.includes(": Salud") || cuerpo.includes("import type"))
  throw new Error("el arnés ya no despoja los tipos: la firma cambió y esto no lo sigue");
const { motivoSalud } = await import(
  "data:text/javascript," + encodeURIComponent(cuerpo + "\nexport {motivoSalud};")
);

let malos = 0;
const caso = (nombre, real, esperado) => {
  const ok = esperado instanceof RegExp ? esperado.test(real ?? "") : real === esperado;
  if (!ok) { malos++; console.log(`  ✗ ${nombre}\n      dio: ${JSON.stringify(real)}`); }
  else console.log(`  ✓ ${nombre}`);
};

const sano = { ok: true, rotos: null, solo_lectura: null, indexador: { error: null } };

caso("sano no dice nada", motivoSalud(sano, 0), null);
caso("sin salud todavía, no inventa", motivoSalud(undefined, 0), null);

// ── LA AVERÍA QUE ESTO CURA ────────────────────────────────────────────────────
caso("sólo-lectura NO se atribuye al indexador",
  motivoSalud({ ...sano, ok: false, solo_lectura: "DatabaseError: disk I/O" }, 0),
  /SÓLO LECTURA/);
caso("y dice que las escrituras se pierden, que es lo accionable",
  motivoSalud({ ...sano, ok: false, solo_lectura: "x" }, 0),
  /nada de lo que escribas se guarda/);
caso("un ledger roto NO se atribuye al indexador",
  motivoSalud({ ...sano, ok: false, rotos: { "crm-pm": "no existe" } }, 0),
  /1 ledger sin indexar \(crm-pm\)/);
caso("y dice QUÉ bandejas están ciegas y que el resto no",
  motivoSalud({ ...sano, ok: false, rotos: { a: "x", b: "y" } }, 0),
  /2 ledgers sin indexar \(a, b\).*el resto no/);

// ── el caso que sí era del indexador, que tiene que seguir funcionando ─────────
caso("el indexador desfasado sigue nombrándose",
  motivoSalud({ ...sano, ok: false, indexador: { error: "timeout" } }, 0),
  /indexador no está al día \(timeout\)/);

// ── precedencia: se nombra lo que impide MÁS ──────────────────────────────────
caso("sólo-lectura manda sobre ledger roto",
  motivoSalud({ ok: false, solo_lectura: "x", rotos: { a: "y" }, indexador: { error: "z" } }, 0),
  /SÓLO LECTURA/);
caso("las desapariciones mandan sobre todo: acusan al DATO, no al servicio",
  motivoSalud({ ok: false, solo_lectura: "x", rotos: { a: "y" }, indexador: { error: "z" } }, 3),
  /3 entradas que estuvieron/);
caso("una desaparición, en singular",
  motivoSalud(sano, 1), /^1 entrada que estuvo/);

// ⊖ EL QUE IMPORTA: que el mensaje del indexador no vuelva a tragárselo todo.
const soloLectura = motivoSalud({ ...sano, ok: false, solo_lectura: "x" }, 0);
caso("con sólo-lectura el mensaje NO habla del indexador",
  /indexador/.test(soloLectura) ? "HABLA DEL INDEXADOR" : "ok", "ok");
const conRotos = motivoSalud({ ...sano, ok: false, rotos: { a: "x" } }, 0);
caso("con un ledger roto el mensaje NO habla del indexador",
  /indexador/.test(conRotos) ? "HABLA DEL INDEXADOR" : "ok", "ok");

console.log(malos ? `\n${malos} fallo(s)` : "\nsalud: todo verde");
process.exit(malos ? 1 : 0);
