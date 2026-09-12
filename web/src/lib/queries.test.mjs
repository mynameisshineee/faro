// [codex #1445 · MARK:astra-console-observer-activo-1804-20260908] — el caché
// borrado NO prueba la pantalla limpia: `QueryCache.remove` destruye en
// silencio y no vacía el resultado de un OBSERVADOR ACTIVO. Aquí se prueba con
// QueryClient + QueryObserver REALES: A visible en dos observadores, cambio de
// identidad de verdad, resultado observado vacío/cargando ANTES de liberar B,
// org B servida con runtimes B retenido (la mezcla org B × runtimes A es el
// defecto), B/B, y la respuesta vieja no vuelve.
//
// La cura estructural: las queryKeys incluyen la REVISIÓN de identidad (entero
// no secreto, jamás la credencial) y los hooks la leen con useSyncExternalStore —
// al cambiar la credencial la clave CAMBIA, el observador abandona la query de A
// y levanta la de B. `retirarQueriesNativas` limpia el rastro muerto de la caché.
//
//   node web/src/lib/queries.test.mjs
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import ts from "typescript";
import { QueryClient, QueryObserver, CancelledError } from "@tanstack/react-query";

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

// localStorage shim ANTES de importar api.ts (cambiar/borrar tocan storage).
if (!globalThis.localStorage) {
  const bolsa = new Map();
  globalThis.localStorage = {
    getItem: (k) => (bolsa.has(k) ? bolsa.get(k) : null),
    setItem: (k, v) => bolsa.set(k, String(v)),
    removeItem: (k) => bolsa.delete(k),
  };
}

const { retirarQueriesNativas, claveOrganizacionNativa, claveRuntimesNativos } = await import(
  "data:text/javascript," + encodeURIComponent(transpilar("queries-nativas.ts"))
);
const { cambiarCredencialNativa, borrarCredencialNativa, revisionIdentidadNativa, suscribirseIdentidadNativa } = await import(
  "data:text/javascript," + encodeURIComponent(transpilar("api.ts"))
);

// ── 1 · OBSERVADORES REALES: la pantalla suelta A aunque B tarde ────────────
{
  const qc = new QueryClient();
  let abrirOrgA, abrirOrgB, abrirRtsB;
  const orgA = new Promise((res) => { abrirOrgA = res; });
  const orgB = new Promise((res) => { abrirOrgB = res; });
  const rtsB = new Promise((res) => { abrirRtsB = res; });
  const r0 = revisionIdentidadNativa();
  const obsOrg = new QueryObserver(qc, { queryKey: claveOrganizacionNativa(r0), queryFn: () => orgA });
  const obsRts = new QueryObserver(qc, { queryKey: claveRuntimesNativos(r0), queryFn: () => orgA });
  const sinVer1 = obsOrg.subscribe(() => {});
  const sinVer2 = obsRts.subscribe(() => {});
  const vueloOrgA = qc.fetchQuery({ queryKey: claveOrganizacionNativa(r0), queryFn: () => orgA });
  const vueloRtsA = qc.fetchQuery({ queryKey: claveRuntimesNativos(r0), queryFn: () => orgA });
  abrirOrgA({ marca: "A" });
  await vueloOrgA;
  await vueloRtsA;
  await new Promise((r) => setTimeout(r, 0));
  caso("A visible en DOS observadores activos (el estado que ya está en pantalla)",
    obsOrg.getCurrentResult().data?.marca === "A" && obsRts.getCurrentResult().data?.marca === "A", true);

  // cambio de identidad REAL: revisión sube, la clave cambia, el observador
  // abandona la query vieja (setOptions es lo que hace el hook al re-renderear).
  const rLimite = revisionIdentidadNativa(); // límite capturado ANTES de mutar [codex #1462]
  cambiarCredencialNativa("cred-B");
  let peticionesOrgB = 0, peticionesRtsB = 0;
  obsOrg.setOptions({ queryKey: claveOrganizacionNativa(revisionIdentidadNativa()), queryFn: () => { peticionesOrgB++; return orgB; } });
  obsRts.setOptions({ queryKey: claveRuntimesNativos(revisionIdentidadNativa()), queryFn: () => { peticionesRtsB++; return rtsB; } });
  await new Promise((r) => setTimeout(r, 0));
  caso("cambio de identidad: el observador activo SUELTA A — resultado observado vacío/cargando aunque B no llegue",
    obsOrg.getCurrentResult().data === undefined && obsRts.getCurrentResult().data === undefined, true);
  caso("la lectura de B ARRANCA sin esperar el intervalo de sondeo",
    peticionesOrgB === 1 && peticionesRtsB === 1, true);

  // La limpieza corre CON B ya en vuelo — el orden real de la UI: el render que
  // arranca B no espera a que la limpieza termine [codex #1462].
  await retirarQueriesNativas(qc, rLimite);
  const qOrgB = qc.getQueryCache().find({ queryKey: claveOrganizacionNativa(revisionIdentidadNativa()) });
  const qRtsB = qc.getQueryCache().find({ queryKey: claveRuntimesNativos(revisionIdentidadNativa()) });
  caso("la limpieza acotada NO cancela ni borra la lectura B en vuelo (arrancada antes de que la limpieza termine)",
    Boolean(qOrgB) && Boolean(qRtsB) && qOrgB.state.fetchStatus === "fetching" && qRtsB.state.fetchStatus === "fetching"
      && peticionesOrgB === 1 && peticionesRtsB === 1, true);
  caso("la limpieza acotada SÍ retira el rastro de A de la caché",
    qc.getQueryData(claveOrganizacionNativa(r0)) === undefined && qc.getQueryData(claveRuntimesNativos(r0)) === undefined, true);

  // org B liberada PRIMERO; runtimes B sigue retenido: la mezcla prohibida es
  // ver org B × runtimes A — aquí el tercero debe ser vacío/cargando.
  abrirOrgB({ marca: "B" });
  await new Promise((r) => setTimeout(r, 0));
  caso("org B pintada mientras runtimes B sigue pendiente — NUNCA runtimes A",
    obsOrg.getCurrentResult().data?.marca === "B" && obsRts.getCurrentResult().data === undefined, true);
  abrirRtsB([{ marca: "B" }]);
  await new Promise((r) => setTimeout(r, 0));
  caso("B/B completo en pantalla",
    obsOrg.getCurrentResult().data?.marca === "B" && obsRts.getCurrentResult().data?.[0]?.marca === "B", true);

  // la respuesta de la identidad vieja NO vuelve: refetch del prefijo viejo
  // (incluiría revisiones retiradas si sobrevivieran) deja B en pantalla.
  await qc.refetchQueries({ queryKey: ["native-organization"] });
  await qc.refetchQueries({ queryKey: ["native-runtimes"] });
  await new Promise((r) => setTimeout(r, 0));
  caso("la respuesta vieja no vuelve: los observadores siguen en B",
    obsOrg.getCurrentResult().data?.marca === "B" && obsRts.getCurrentResult().data?.[0]?.marca === "B", true);
  sinVer1(); sinVer2();
}

// ── 1-bis · una respuesta A que llega TARDE no repuebla caché ni pantalla ────
{
  const qc = new QueryClient();
  let abrirATardia;
  const aTardia = new Promise((res) => { abrirATardia = res; });
  const r0 = revisionIdentidadNativa();
  const obs = new QueryObserver(qc, { queryKey: claveOrganizacionNativa(r0), queryFn: () => aTardia });
  const sinVer = obs.subscribe(() => {});
  const vuelo = qc.fetchQuery({ queryKey: claveOrganizacionNativa(r0), queryFn: () => aTardia }); // en vuelo, SIN resolver
  // el manejador de rejection se ata ANTES de cancelar: el CancelledError llega
  // aquí mientras el test cruza un macrotick — sin atador previo, unhandledRejection.
  const finPromesa = vuelo.then(() => "resolvio", (e) => (e instanceof CancelledError ? "CancelledError" : `otro: ${e?.name ?? e}`));
  const limite = revisionIdentidadNativa();
  cambiarCredencialNativa("cred-C");
  await retirarQueriesNativas(qc, limite);
  abrirATardia({ marca: "A-TARDIA" }); // la respuesta A llega DESPUÉS de la limpieza
  await new Promise((r) => setTimeout(r, 0));
  const fin = await finPromesa;
  caso("la respuesta A que llega TARDE no repuebla la caché (cancelada en vuelo, rastro retirado)",
    fin === "CancelledError" && qc.getQueryData(claveOrganizacionNativa(r0)) === undefined, true);
  caso("…ni vuelve a la pantalla", obs.getCurrentResult().data === undefined, true);
  sinVer();
}

// ── 1-ter · BORRAR con observador activo: la vista suelta A ─────────────────
{
  const qc = new QueryClient();
  let abrirOrgD;
  const orgD = new Promise((res) => { abrirOrgD = res; });
  const r0 = revisionIdentidadNativa();
  const obs = new QueryObserver(qc, { queryKey: claveOrganizacionNativa(r0), queryFn: () => orgD });
  const sinVer = obs.subscribe(() => {});
  const vuelo = qc.fetchQuery({ queryKey: claveOrganizacionNativa(r0), queryFn: () => orgD });
  abrirOrgD({ marca: "A" });
  await vuelo;
  await new Promise((r) => setTimeout(r, 0));
  caso("BORRAR: A visible en el observador antes", obs.getCurrentResult().data?.marca === "A", true);
  const limite = revisionIdentidadNativa();
  borrarCredencialNativa();
  // lo que hace el hook al re-renderear tras BORRAR: clave nueva, SIN lectura
  // (enabled:false — sin credencial no hay petición, banner F5).
  obs.setOptions({ queryKey: claveOrganizacionNativa(revisionIdentidadNativa()), queryFn: () => new Promise(() => {}), enabled: false });
  await new Promise((r) => setTimeout(r, 0));
  await retirarQueriesNativas(qc, limite);
  caso("BORRAR con observador activo: la pantalla suelta A y el rastro viejo sale de caché",
    obs.getCurrentResult().data === undefined && qc.getQueryData(claveOrganizacionNativa(r0)) === undefined, true);
  sinVer();
}

// ── 2 · la lectura de A en vuelo se CANCELA al retirar ──────────────────────
{
  const qc = new QueryClient();
  const r = revisionIdentidadNativa();
  const vuelo = qc.fetchQuery({ queryKey: claveOrganizacionNativa(r), queryFn: () => new Promise(() => {}) });
  const finPromesa = vuelo.then(
    () => "resolvió",
    (e) => (e instanceof CancelledError ? "CancelledError" : `otro: ${e?.name ?? e}`),
  );
  await retirarQueriesNativas(qc, r);
  const fin = await finPromesa;
  caso("cambio de credencial: la lectura de A en vuelo se CANCELA (no puede aterrizar tarde bajo B)",
    fin, "CancelledError");
}

// ── 3 · las claves llevan la revisión — ruta fija + entero, jamás credencial ─
{
  const clave = claveOrganizacionNativa(7);
  const claveRts = claveRuntimesNativos(7);
  caso("las claves son ruta fija + REVISIÓN entera (no secreta, cambia con la identidad)",
    JSON.stringify(clave) === '["native-organization",7]' && JSON.stringify(claveRts) === '["native-runtimes",7]'
      && claveOrganizacionNativa(8).join("") !== clave.join(""), true);
  const sus = suscribirseIdentidadNativa(() => {});
  caso("la suscripción devuelve su desuscripción y la revisión es un entero del estado de api.ts",
    typeof sus === "function" && Number.isInteger(revisionIdentidadNativa()), true);
  sus();
}

// ── 4 · cableado: los hooks leen la revisión reactivamente y usan la fuente ──
const queries = transpilar("queries.ts");
caso("los 2 hooks nativos leen la revisión con useSyncExternalStore (el cambio de identidad re-renderea)",
  (queries.match(/useSyncExternalStore\(/g) ?? []).length, 2);
caso("los 2 hooks nativos construyen la clave con la fuente compartida y la revisión",
  (queries.match(/queryKey: clave(OrganizacionNativa|RuntimesNativos)\(revision\)/g) ?? []).length, 2);
caso("no queda ningún literal de clave nativa propio en queries.ts (deriva = cruce A/B otra vez)",
  (queries.match(/queryKey: \[?["']native-/g) ?? []).length, 0);

const srcCredencial = readFileSync(join(aqui, "..", "components", "CredencialNativa.tsx"), "utf8");
caso("CredencialNativa RETIRA las queries nativas (limpia el rastro muerto) y no invalida",
  srcCredencial.includes("retirarQueriesNativas(") && !srcCredencial.includes("invalidateQueries"), true);
caso("AMBOS botones consumen UN mismo camino: límite capturado antes de mutar, limpieza acotada",
  (srcCredencial.match(/conLimpiezaAcotada\(/g) ?? []).length === 2
    && (srcCredencial.match(/retirarQueriesNativas\(queryClient, hasta\)/g) ?? []).length === 1
    && srcCredencial.indexOf("const hasta = revisionIdentidadNativa()") < srcCredencial.indexOf("mutar()"), true);
caso("el comentario de CredencialNativa ya no promete retiro 'al momento' (el pintado limpio es del cambio de clave)",
  !srcCredencial.includes("al momento"), true);

console.log(malos ? `\n${malos} fallo(s)` : "\nqueries: todo verde");
process.exit(malos ? 1 : 0);
