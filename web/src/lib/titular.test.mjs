// Casos del pelador de titulares, cada uno con la convención real que lo produjo
// y —donde lo hubo— el fallo que lo puso aquí. Se corre con:
//   node web/src/lib/titular.test.mjs
// No necesita marco de tests: importa el fichero real quitándole las anotaciones
// de tipo, para que no exista una segunda copia de la lógica que pueda derivar.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const aqui = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(aqui, "titular.ts"), "utf8");
const cuerpo = src
  .slice(src.indexOf("const TIPOS_ETIQUETA"), src.indexOf("/** Hora relativa"))
  .replace("export function titular(cabecera: string, actor: string | null): string", "function titular(cabecera, actor)")
  .replace("const gasta = (k: keyof typeof cupo, rx: RegExp) =>", "const gasta = (k, rx) =>");
const { titular } = await import("data:text/javascript," + encodeURIComponent(cuerpo + "\nexport {titular};"));

// ── `hora()`, que hasta ahora NO tenía ni un caso ────────────────────────────
// El slice de arriba corta EN `/** Hora relativa`, así que esta función quedaba
// fuera del arnés entero. Se extrae aparte y con `Date.now` fijado: una prueba de
// tiempo relativo que dependa del reloj de quien la corre se pone roja sola en
// diciembre y verde en enero, y nadie sabe por qué.
const cuerpoHora = src
  .slice(src.indexOf("const utc = (ts"), src.indexOf("export type MotivoVacio"))
  .replace("const utc = (ts: string): Date =>", "const utc = (ts) =>")
  .replace("export function hora(ts: string | null): string", "function hora(ts)")
  .replace("export function sello(ts: string | null): string", "function sello(ts)");
const AHORA = Date.parse("2026-08-22T20:00:00Z");
const _now = Date.now;
Date.now = () => AHORA;
const { hora, sello } = await import("data:text/javascript," + encodeURIComponent(cuerpoHora + "\nexport {hora, sello};"));

const CASOS_HORA = [
  [null, "·", "sin sello: el 13% del corpus (9.375 entradas) no lo trae"],
  // Sin fijar el formato: `hora()` usa el locale del SISTEMA, así que asertar
  // "20:30" mide la máquina del que corre el test, no el código. Me salió rojo por
  // eso en la primera pasada (el runner devolvió "08:30 PM"). Se comprueba la
  // PROPIEDAD —hoy da una hora, no una fecha— no la grafía.
  ["2026-08-22T18:30:00", "HORA_DE_HOY", "hoy: da una hora, no una fecha ni un plazo"],
  ["2026-08-19T10:00:00", "3d", "esta semana: días relativos"],

  // ① EL DEFECTO: `dias < 7` es cierto también para -57. Una entrada fechada en el
  // futuro imprimía «-57d», que no es una fecha ni un plazo. Hay 6 sellos
  // posteriores a ahora en el corpus vivo, y uno es legítimo (2026-10-17).
  // ⚠️ AFIRMACIÓN EXACTA, no ausencia. La versión anterior sólo exigía «que no
  // empiece por -», y eso lo pasa cualquier cadena plausible: lo probé mutando
  // `hora()` para que devolviera "3d" ante un futuro y EL TEST SOBREVIVIÓ.
  // Un falsador que sólo prohíbe una forma no comprueba el valor. El valor exacto
  // es la fecha corta DEL SISTEMA: 23:30Z cae en el día 17 en UTC y en el 18 en
  // Europe/Madrid. El producto no fija `timeZone`, por lo que el test tampoco
  // puede fijarla a escondidas. Calcula el oráculo local con la API nativa y luego
  // compara la cadena completa; un mutante que devuelva "3d" sigue muriendo.
  ["2026-10-17T23:30:00", "FUTURO_FECHA_LOCAL", "futuro: fecha corta local, NO un plazo negativo"],

  // ② EL OTRO: sellos con FORMA ISO pero imposibles. `new Date` los rechaza y la
  // UI pintaba «Invalid Date», que se lee como fallo de la herramienta en vez de
  // como lo que es: un sello que el autor escribió mal.
  ["9999-99-99T99:99:99", "sello ilegible", "sello imposible: existe en 64bis-wiki-archivo"],
  ["2026-13-45T99:99:99", "sello ilegible", "mes 13, día 45: también existe en el corpus"],

  // La guarda del sufijo: los tres deben dar EL MISMO instante. Hoy no hay ninguno
  // con zona en el corpus (0 de 71.770) — está para el día que el troceador deje
  // de pelarla, que marcaría el corpus entero como ilegible sin avisar.
  ["2026-08-22T19:59:00Z", "HORA_DE_HOY_59", "sello que YA trae Z: no se le añade otra"],
];

// `sello()` es la MISMA autoridad de formato que `hora()`, no otra. Si alguien
// vuelve a poner un `new Date()` suelto en un componente, estos tres casos siguen
// verdes y el defecto vuelve — por eso el falsador que importa es que el panel
// LLAME a esto, y está cubierto por el `grep` de abajo.
let malosFecha = 0;
const CASOS_SELLO = [
  [null, /^sin sello$/, "sin sello: no puede decir «Invalid Date»"],
  // Incluye EL VALOR, para que quien lo vea sepa qué corregir en su ledger.
  ["9999-99-99T99:99:99", /^sello ilegible \(9999-99-99T99:99:99\)$/, "lo nombra Y lo cita"],
  ["2026-08-22T18:30:00", /^22\/8\/2026/, "sello bueno: fecha completa"],
  ["2026-08-22T18:30:00Z", /^22\/8\/2026/, "con Z: MISMO instante, no ilegible"],
];
for (const [ts, rx, porque] of CASOS_SELLO) {
  const got = sello(ts);
  if (!rx.test(got) || /Invalid/i.test(got)) {
    console.log(`  ✗ sello(${JSON.stringify(ts)}) → ${JSON.stringify(got)}`);
    console.log(`     esperado: ${rx} · ${porque}`);
    malosFecha++;
  }
}

let malosHora = 0;
for (const [ts, esperado, porque] of CASOS_HORA) {
  const got = hora(ts);
  let ok;
  if (esperado === "FUTURO_FECHA_LOCAL") {
    const fechaLocal = new Date(`${ts}Z`).toLocaleDateString([], { day: "2-digit", month: "short" });
    ok = got === fechaLocal && !/d$/.test(got) && !/^-/.test(got);
  }
  else if (esperado === "HORA_DE_HOY") ok = /30/.test(got) && !/^-/.test(got) && !/Invalid/i.test(got) && !/d$/.test(got);
  else if (esperado === "HORA_DE_HOY_59") ok = /59/.test(got) && !/Invalid/i.test(got) && !/d$/.test(got);
  else ok = got === esperado;
  if (!ok) { malosHora++; console.log(`  ✗ hora(${JSON.stringify(ts)}) → ${JSON.stringify(got)}`);
             console.log(`     esperado: ${esperado} · ${porque}`); }
}
console.log(malosHora ? `hora: ${malosHora} rojo(s)` : `hora: ${CASOS_HORA.length} casos verdes`);
Date.now = _now;
malosFecha += malosHora;

const CASOS = [
  // [cabecera, actor, esperado, por qué está aquí]
  ["### [alice → bob · PRODUCED] 2026-07-27T10:15:00Z — pagos: endpoint listo", "alice",
   "pagos: endpoint listo", "la convención dominante, 94% del corpus"],

  ["## [wiki-vault 2026-07-28T01:34:01Z] 🌙 EN VUELO — mandato del operador", "wiki-vault",
   "EN VUELO — mandato del operador", "el corchete cierra DESPUÉS del sello: dejaba «] 🌙…»"],

  ["### [MSG cfo-guardian->TODOS] 2026-06-18 — alcance ampliado", "cfo-guardian",
   "alcance ampliado", "el separador se comía el «-» de «->» y dejaba «>TODOS]…»"],

  ["### [CLAIM deploy-bik.eus] [bikeus] 2026-07-09T22:15:29Z — deploy fix copy", "deploy-bik.eus",
   "deploy fix copy", "DOS grupos entre corchetes seguidos: dejaba «[bikeus]…»"],

  ["## 2026-05-25 — [STATUS BE→FE] ADR-022 firma flip COMPLETE", "be",
   "ADR-022 firma flip COMPLETE", "el sello va DELANTE del corchete: orden invertido"],

  ["### [marketing -> cpo (RESP HOLD signup: carril limpio)] 2026-07-13T16:39:30Z — status: RESP (compliant)", "marketing",
   "status: RESP (compliant)", "STATUS y RESP son palabras reales aquí, no etiquetas"],

  ["### [cpo → marketing ∧ bikeus (nota larga entre paréntesis que ocupa bastante más de ciento sesenta caracteres para comprobar que el tope no se queda corto y la ruta se pela entera igual)] 2026-07-10T14:09:55Z — el titular de verdad", "cpo",
   "el titular de verdad", "rutas de mediana 269 caracteres: el tope de 160 las partía"],

  ["## 2026-07-28T10:30:00Z \u00b7 transcribo \u2192 wiki-vault \u00b7 PRODUCED \u2014 rescate de 11 v\u00eddeos", "transcribo",
   "rescate de 11 v\u00eddeos", "ruta del spoke SIN corchete: abr\u00eda por «wiki-vault \u00b7 PRODUCED \u2014»"],

  ["## 2026-07-06T11:45Z · transcribo → wiki-vault · PRODUCED", "transcribo",
   "", "cabecera de PURO metadato: vacío, y quien llama usa el cuerpo"],

  ["### [qa] ACK recibido, arranco", "qa",
   "ACK recibido, arranco", "ACK abre texto libre y NO debe pelarse como etiqueta"],
];

let fallos = 0;
for (const [head, actor, esperado, porque] of CASOS) {
  const dado = titular(head, actor);
  if (dado === esperado) {
    console.log(`  ✓ ${porque}`);
  } else {
    fallos++;
    console.log(`  ✗ ${porque}`);
    console.log(`      cabecera: ${JSON.stringify(head.slice(0, 96))}`);
    console.log(`      esperado: ${JSON.stringify(esperado)}`);
    console.log(`      obtenido: ${JSON.stringify(dado)}`);
  }
}

// CONTROL POSITIVO: si el pelador devolviera siempre la cabecera en crudo —el fallo
// más plausible al tocarlo— los casos de arriba fallarían. Se comprueba que al menos
// uno depende de verdad de pelar algo, para que este fichero no pueda dar verde
// midiendo la nada.
const crudo = titular("### [alice → bob · PRODUCED] 2026-07-27T10:15:00Z — pagos: endpoint listo", "alice");
if (crudo.includes("alice") || crudo.includes("2026-")) {
  console.log("  ✗ control positivo: el pelador no está pelando nada");
  fallos++;
} else {
  console.log("  ✓ control positivo: el resultado no conserva actor ni sello");
}

console.log(fallos === 0 ? `\ntitulares: ${CASOS.length + 1} casos, todo verde` : `\ntitulares: ${fallos} fallo(s)`);
// ⚠️ `fallos` SOLO cuenta los de `titular()`. Los de fecha viven en `malosFecha`, y
// olvidarlos aquí es lo que convirtió esta prueba en teatro durante una pasada:
// los seis casos nuevos IMPRIMÍAN su rojo y el proceso salía 0, así que los cuatro
// mutantes de `hora()`/`sello()` sobrevivieron. Un test que no puede fallar no es
// un test — se cazó mutando, no leyendo.
process.exit(fallos === 0 && malosFecha === 0 ? 0 : 1);
