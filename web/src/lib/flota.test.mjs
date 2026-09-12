// Lógica pura de la consola de flota: jerarquía legada, ruta de escalado y el
// estado del panel nativo (G7-G10, ADR-002-FLEET-CONTROL-PLANE.md).
//   node web/src/lib/flota.test.mjs
//
// A diferencia de `salud.test.mjs`/`titular.test.mjs` (que pelan los tipos con
// una sustitución de texto), aquí se usa el `typescript` YA instalado como
// devDependency para transpilar: `flota.ts` usa genéricos (`Map<string,
// string[]>`) e imports de tipo con varios símbolos, que una sustitución de
// cadena tendría que perseguir uno a uno y cualquiera nuevo rompería el arnés
// en silencio. La herramienta que ya sabe parsear TypeScript es más barata que
// una regex que reaprende TypeScript.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import ts from "typescript";

const aqui = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(aqui, "flota.ts"), "utf8");
const { outputText, diagnostics } = ts.transpileModule(src, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  reportDiagnostics: true,
});
if (diagnostics && diagnostics.length)
  throw new Error(`flota.ts no transpila limpio: ${diagnostics.map((d) => d.messageText).join("; ")}`);
// Si el import de tipos sobreviviera a la transpilación, el `import()` de abajo
// fallaría RUIDOSAMENTE al no encontrar el módulo "@/lib/api" — que es lo que se
// quiere: un arnés que se rompe en silencio no prueba nada.
if (outputText.includes('from "@/lib/api"'))
  throw new Error("el import de tipos sobrevivió a la transpilación: revisa flota.ts");

const {
  filasJerarquia,
  motivoOrganigramaVacio,
  rutaEscalado,
  estadoPanelNativo,
  estadoTablaRuntimes,
  motivoPanelNoDisponible,
  filasRuntimesNativos,
  filasOrganizacionNativa,
  marcaComprobacion,
  ETIQUETA_RUNTIME_STATUS,
  COLOR_RUNTIME_STATUS,
} = await import("data:text/javascript," + encodeURIComponent(outputText));

let malos = 0;
const caso = (nombre, real, esperado) => {
  const realStr = JSON.stringify(real);
  const esperadoStr = JSON.stringify(esperado);
  if (realStr !== esperadoStr) {
    malos++;
    console.log(`  ✗ ${nombre}\n      dio:      ${realStr}\n      esperado: ${esperadoStr}`);
  } else console.log(`  ✓ ${nombre}`);
};

// ── LEGADO — filasJerarquia ──────────────────────────────────────────────────
const ORG_BASE = {
  revision: null, source_sha256: "abc", loaded_sha256: "abc", cargado_en: "t", stale: false,
  jerarquia: {
    cto: { reporta_a: "OPERADOR", capa: "c-suite" },
    be: { reporta_a: "cto", capa: "ejecucion" },
    fe: { reporta_a: "cto", capa: "ejecucion", gatea: "design" },
    qa: { reporta_a: "cto", capa: "gate", gatea: ["be", "fe"] },
  },
  roles: 4, aviso: null,
};

{
  const filas = filasJerarquia(ORG_BASE);
  caso("orden alfabético por rol", filas.map((f) => f.rol), ["be", "cto", "fe", "qa"]);
  const ctoFila = filas.find((f) => f.rol === "cto");
  caso("subordinados es la INVERSA de reporta_a, no un campo servido", ctoFila.subordinados, ["be", "fe", "qa"]);
  caso("un rol sin subordinados da lista vacía, no null", filas.find((f) => f.rol === "be").subordinados, []);
  caso("gatea string suelto se normaliza a lista de 1", filas.find((f) => f.rol === "fe").gatea, ["design"]);
  caso("gatea ya-lista se conserva", filas.find((f) => f.rol === "qa").gatea, ["be", "fe"]);
  caso("gatea ausente da lista vacía, no undefined", filas.find((f) => f.rol === "cto").gatea, []);
  caso("reportaA de la raíz es el string tal cual (OPERADOR no es un rol de este mapa)", ctoFila.reportaA, "OPERADOR");
  caso("filasJerarquia NO trae ningún campo de agente/identidad (sin inferencia de vínculo)",
    Object.keys(ctoFila).sort(), ["capa", "criteriosDe", "gatea", "reportaA", "rol", "subordinados"]);
}

caso("jerarquía vacía da lista vacía, no revienta", filasJerarquia({ ...ORG_BASE, jerarquia: {}, roles: 0 }), []);
caso("org undefined (loading) da lista vacía", filasJerarquia(undefined), []);

// ── motivoOrganigramaVacio — dos causas, y el servicio ya las distingue ──────
caso("con roles > 0 no hay motivo", motivoOrganigramaVacio(ORG_BASE), null);
caso("source_sha256 null ⇒ sin-montar (no hay fuente firmada)",
  motivoOrganigramaVacio({ ...ORG_BASE, jerarquia: {}, roles: 0, source_sha256: null }), "sin-montar");
caso("source_sha256 presente pero jerarquia vacía ⇒ sin-jerarquia (montada, campo vacío)",
  motivoOrganigramaVacio({ ...ORG_BASE, jerarquia: {}, roles: 0, source_sha256: "abc" }), "sin-jerarquia");

// ── rutaEscalado — cadena completa, no sólo el superior inmediato ───────────
caso("cadena completa hasta que alguien no reporta a nadie más EN EL MAPA",
  rutaEscalado(ORG_BASE.jerarquia, "be"), ["cto", "OPERADOR"]);
caso("un rol sin reporta_a da ruta vacía", rutaEscalado({ x: {} }, "x"), []);
caso("un rol ausente del mapa da ruta vacía, no revienta", rutaEscalado(ORG_BASE.jerarquia, "no-existe"), []);
{
  const ciclo = { a: { reporta_a: "b" }, b: { reporta_a: "a" } };
  caso("un ciclo se corta en vez de repetir para siempre", rutaEscalado(ciclo, "a"), ["b"]);
}
{
  const tope = { a: { reporta_a: "b" }, b: { reporta_a: "c" }, c: { reporta_a: "d" } };
  caso("el tope acota la ruta aunque no haya ciclo", rutaEscalado(tope, "a", 2), ["b", "c"]);
}

// ── NATIVO — estadoPanelNativo: tres causas que no se pueden confundir ──────
const NO_DISP_ORG = { disponible: false, motivo: "org no existe" };
const NO_DISP_RT = { disponible: false, motivo: "runtimes no existe" };
const ORG_OK_VACIA = { disponible: true, datos: { roles: [] } };
const ORG_OK_CON_ROLES = { disponible: true, datos: { roles: [{ role: "cto" }] } };
const RT_OK_VACIA = { disponible: true, datos: [] };
const RT_OK_CON_FILAS = { disponible: true, datos: [{ runtime_instance: "r1" }] };

caso("loading (queries aún sin resolver) da null, no un estado inventado",
  estadoPanelNativo(undefined, undefined), null);
caso("hoy: los dos seams en false ⇒ no-disponible", estadoPanelNativo(NO_DISP_ORG, NO_DISP_RT), "no-disponible");
caso("SÓLO org no disponible ya basta para no-disponible (no hace falta que fallen los dos)",
  estadoPanelNativo(NO_DISP_ORG, RT_OK_CON_FILAS), "no-disponible");
caso("SÓLO runtimes no disponible también basta",
  estadoPanelNativo(ORG_OK_CON_ROLES, NO_DISP_RT), "no-disponible");
caso("los dos disponibles pero sin roles/runtimes ⇒ vacio, NO no-disponible (son causas distintas)",
  estadoPanelNativo(ORG_OK_VACIA, RT_OK_VACIA), "vacio");
caso("org CON roles pero cero runtimes observados es con-datos, NO vacio — [codex-console-f018122-absent-parcial]: la vacancia total produce N filas absent, igual que la parcial desde f018122, no una tabla en blanco",
  estadoPanelNativo(ORG_OK_CON_ROLES, RT_OK_VACIA), "con-datos");
caso("SÓLO vacio cuando NO hay ni roles ni runtimes — nada de lo que derivar ni una fila absent",
  estadoPanelNativo({ disponible: true, datos: { roles: [] } }, { disponible: true, datos: [] }), "vacio");
caso("org sin roles pero con un runtime HUÉRFANO no es vacio: el huérfano es una fila real",
  estadoPanelNativo(ORG_OK_VACIA, RT_OK_CON_FILAS), "con-datos");
caso("los dos disponibles y con filas ⇒ con-datos",
  estadoPanelNativo(ORG_OK_CON_ROLES, RT_OK_CON_FILAS), "con-datos");

caso("motivoPanelNoDisponible cita el motivo real del seam que falló",
  motivoPanelNoDisponible(NO_DISP_ORG, RT_OK_CON_FILAS), "org no existe");
caso("si ambos disponibles, no hay motivo que dar", motivoPanelNoDisponible(ORG_OK_CON_ROLES, RT_OK_CON_FILAS), null);

// ── D12 — ausencia tipada de organización [encargo astra runtime-root +
// contrato codex #1343]: el servidor respondió 404 SUBJECT_NOT_FOUND («no hay
// revisión activa»). Es una RESPUESTA, no una avería — estado propio, nunca
// mezclado con "no-disponible". ──────────────────────────────────────────────
const ORG_AUSENTE = { disponible: false, motivo: "404 SUBJECT_NOT_FOUND: sin revisión activa", ausente: true };
caso("org AUSENTE (404 tipado) ⇒ panel sin-organizacion",
  estadoPanelNativo(ORG_AUSENTE, RT_OK_VACIA), "sin-organizacion");
caso("org AUSENTE con runtimes observados TAMBIÉN sin-organizacion (sin revisión activa no hay cruce posible)",
  estadoPanelNativo(ORG_AUSENTE, RT_OK_CON_FILAS), "sin-organizacion");
caso("org no-disponible SIN ausente sigue siendo no-disponible",
  estadoPanelNativo(NO_DISP_ORG, RT_OK_VACIA), "no-disponible");
caso("el motivo tipado de la org ausente se lee igual que el de cualquier fallo",
  motivoPanelNoDisponible(ORG_AUSENTE, RT_OK_VACIA), "404 SUBJECT_NOT_FOUND: sin revisión activa");

// ── E8b — AISLAMIENTO de panel: la tabla de runtimes decide CON SUS PROPIOS
// datos [sdet E8b: 401 PERSISTENTE forzado sólo en organization ARRASTRÓ al
// panel vecino — 0 filas lane-astra con credencial A activa; los otros tres
// asertos del paso PASARON ⇒ el 401 se pinta bien en su panel Y ADEMÁS se
// lleva al vecino sano. Sólo el org debe caer. La shared estadoPanelNativo
// borra el estado del vecino con la avería de uno — por eso la tabla pasa a
// derivar SU estado aquí, y la del org sigue la suya (ya aislada).] ──────────
caso("E8b AISLAMIENTO: org no-disponible (401) pero runtimes CON filas ⇒ con-datos — las filas propias sobreviven a la avería del vecino",
  estadoTablaRuntimes(NO_DISP_ORG, RT_OK_CON_FILAS), "con-datos");
caso("E8b: runtimes disponible SIN datos + org no-disponible ⇒ no-disponible — cara2 E6c (design #1691): banner con el motivo del org",
  estadoTablaRuntimes(NO_DISP_ORG, RT_OK_VACIA), "no-disponible");
caso("avería PROPIA de runtimes ⇒ no-disponible, caiga quien caiga el org",
  estadoTablaRuntimes(ORG_OK_CON_ROLES, NO_DISP_RT), "no-disponible");
caso("sin datos propios + org AUSENTE tipada ⇒ sin-organizacion (D12: sin revisión activa no hay cruce posible)",
  estadoTablaRuntimes(ORG_AUSENTE, RT_OK_VACIA), "sin-organizacion");
caso("sin datos propios + org vacía ⇒ vacio",
  estadoTablaRuntimes(ORG_OK_VACIA, RT_OK_VACIA), "vacio");
caso("sin datos propios + org sana con roles ⇒ con-datos (filas absent por workload — vacancia total es tabla, no mensaje)",
  estadoTablaRuntimes(ORG_OK_CON_ROLES, RT_OK_VACIA), "con-datos");
caso("loading (runtimes sin resolver) ⇒ null, no un estado inventado",
  estadoTablaRuntimes(ORG_OK_CON_ROLES, undefined), null);

// ── filasRuntimesNativos — cruce organización × runtimes, no sólo runtimes ──
// [design-review-console-74ffbe7-ship-con-1-hallazgo-adelante]: iterar sólo
// runtimes.datos deja caer en silencio un rol esperado sin runtime observado
// — el caso exacto de `absent` (ADR-002 Decisión 1). Estos casos son el
// falsador que ese finding pide, escrito ANTES de que el endpoint real exista.
// D8 [RULING cto #1252]: la organización nativa viaja como { authority, revision
// (OBJETO con lane/revision/…), roles } — lane y número de revisión viven DENTRO
// de `revision`, no sueltos arriba.
const ORG_TRES_ROLES = {
  revision: { lane: "llminbox", revision: 7 },
  roles: [{ role: "cto" }, { role: "be" }, { role: "fe" }],
};

// ── D7 [encargo codex #1308 + contrato codex #1343] — la unidad de la
// ausencia es el WORKLOAD, no el rol. El servidor keys runtime_status por
// (lane, workload_id) y expected_workloads declara cuántos espera cada rol
// [coordination.py:1609]; el gateway sirve `workloads[]` SIEMPRE y
// `workload_id` OBLIGATORIO por fila [#1343, verificado cto #1348]. Un rol con
// 2 workloads esperados y 1 observación produce DOS filas — el cruce por ROL
// anterior habría escondido al segundo bajo la observación del primero. Sin
// fallback: `workloads` (aunque VACÍO) es la única clave del cruce — un rol
// sin workloads esperados no se espera en ejecución y NO genera absent.
const ORG_TRES_WORKLOADS = {
  revision: { lane: "llminbox", revision: 7 },
  roles: [{ role: "cto" }, { role: "be" }, { role: "fe" }],
  workloads: [
    { workload_id: "cto-w1", role: "cto" },
    { workload_id: "be-w1", role: "be" },
    { workload_id: "fe-w1", role: "fe" },
  ],
};

{
  const obsBE = { workload_id: "be-w1", runtime_instance: "be-1", lane: "llminbox", principal: "p1",
    role: "be", credential_generation: 2, status: "fresh", detector_state: null, status_seq: 5,
    status_since: "t", last_observed_at: "t",
    cause_id: null, transition_id: "tr1", receipt_id: "rec1", organization_revision: 7 };
  const filas = filasRuntimesNativos(ORG_TRES_WORKLOADS, [obsBE]);
  caso("un workload CON observación real produce su fila TAL CUAL (no se sustituye)",
    filas.find((f) => f.workload_id === "be-w1"), obsBE);
  const cto = filas.find((f) => f.workload_id === "cto-w1");
  caso("un workload esperado SIN observación aparece como absent PROPIO, no desaparece",
    { role: cto.role, status: cto.status, runtime_instance: cto.runtime_instance, workload_id: cto.workload_id },
    { role: "cto", status: "absent", runtime_instance: null, workload_id: "cto-w1" });
  caso("la fila absent inferida hereda lane/revision de la ORGANIZACIÓN, no de un runtime que no existe",
    { lane: cto.lane, organization_revision: cto.organization_revision }, { lane: "llminbox", organization_revision: 7 });
  caso("la fila absent inferida NO inventa generación de credencial ni secuencia de estado",
    { credential_generation: cto.credential_generation, status_seq: cto.status_seq }, { credential_generation: null, status_seq: 0 });
  caso("ningún workload esperado se pierde: tantas filas como workloads cuando no hay huérfanos",
    filas.length, ORG_TRES_WORKLOADS.workloads.length);
}

{
  // Dos workloads del MISMO rol: la observación de uno no tapa al otro.
  const ORG_DOS_DEL_MISMO_ROL = {
    revision: { lane: "llminbox", revision: 9 },
    roles: [{ role: "be" }],
    workloads: [{ workload_id: "be-w1", role: "be" }, { workload_id: "be-w2", role: "be" }],
  };
  const obsW1 = { workload_id: "be-w1", runtime_instance: "be-1", lane: "llminbox", principal: "p1",
    role: "be", credential_generation: 2, status: "fresh", detector_state: null, status_seq: 4,
    status_since: "t", last_observed_at: "t", cause_id: null, transition_id: "tr1",
    receipt_id: "rec1", organization_revision: 9 };
  const filas = filasRuntimesNativos(ORG_DOS_DEL_MISMO_ROL, [obsW1]);
  caso("el SEGUNDO workload del mismo rol es absent PROPIO, no lo tapa la observación del primero",
    filas.find((f) => f.workload_id === "be-w2").status, "absent");
  caso("y el observado sigue siendo su fila real", filas.find((f) => f.workload_id === "be-w1"), obsW1);
}

{
  const huerfano = { workload_id: "wl-fantasma", runtime_instance: "misterioso-1", lane: "llminbox",
    principal: null, role: "rol-fantasma", credential_generation: null, status: "fresh",
    detector_state: null, status_seq: 1, status_since: null, last_observed_at: null,
    cause_id: null, transition_id: null, receipt_id: null, organization_revision: 7 };
  const filas = filasRuntimesNativos(ORG_TRES_WORKLOADS, [huerfano]);
  caso("una observación con workload_id que la organización NO espera se lista igual (dato, no ruido)",
    filas.some((f) => f.workload_id === "wl-fantasma"), true);
  caso("y los 3 workloads esperados SIGUEN saliendo (absent los no observados) aparte del huérfano",
    filas.filter((f) => f.status === "absent").length, 3);
}

caso("workloads VACÍO con roles: NINGÚN absent — un rol sin workloads esperados no se espera en ejecución",
  filasRuntimesNativos({ revision: { lane: "x", revision: 1 }, roles: [{ role: "cto" }], workloads: [] }, []), []);
caso("organización sin roles, sin workloads y sin runtimes da tabla vacía, no revienta",
  filasRuntimesNativos({ revision: { lane: "x", revision: 1 }, roles: [], workloads: [] }, []), []);
caso("un runtime sin `role` declarado se lista aparte, nunca se pierde",
  filasRuntimesNativos({ revision: { lane: "x", revision: 1 }, roles: [], workloads: [] },
    [{ workload_id: "wz", runtime_instance: "sin-rol-1", lane: "x", principal: null, role: null, status: "fresh",
       detector_state: null, status_since: null, last_observed_at: null, cause_id: null,
       transition_id: null, receipt_id: null, organization_revision: 1 }]).length,
  1);

// ── filasOrganizacionNativa — grafo NATIVO, misma inversión que el legado ───
{
  // D5/D6 [RULING cto #1252]: `layer` INT en el wire (la etiqueta la pinta el
  // cliente) y `escalation_routes` ESTRUCTURADAS ({trigger_code, target_role}) —
  // el string[] plano perdía trigger_code.
  const orgNativa = {
    revision: { lane: "llminbox", revision: 3 },
    roles: [
      { role: "cto", reports_to: null, reviewers: [], escalation_routes: [], layer: 1 },
      { role: "be", reports_to: "cto", reviewers: ["qa"], escalation_routes: [{ trigger_code: "BLOCKED", target_role: "cto" }], layer: 3 },
      { role: "fe", reports_to: "cto", reviewers: [], escalation_routes: [{ trigger_code: "REVIEW_REQUIRED", target_role: "cto" }], layer: 3 },
    ],
  };
  const filas = filasOrganizacionNativa(orgNativa);
  caso("orden alfabético por rol", filas.map((f) => f.rol), ["be", "cto", "fe"]);
  const cto = filas.find((f) => f.rol === "cto");
  caso("subordinados nativos también son la INVERSA de reports_to, no un campo servido",
    cto.subordinados, ["be", "fe"]);
  caso("revisores/rutas de escalado ESTRUCTURADAS se conservan tal cual llegan (D6), capa INT (D5)",
    filas.find((f) => f.rol === "be"), { rol: "be", reportaA: "cto", revisores: ["qa"], rutasEscalado: [{ trigger_code: "BLOCKED", target_role: "cto" }], capa: 3, subordinados: [] });
}
caso("filasOrganizacionNativa sin organización (loading/no-disponible) da lista vacía, no revienta",
  filasOrganizacionNativa(undefined), []);
caso("filasOrganizacionNativa con 0 roles da lista vacía", filasOrganizacionNativa({ roles: [] }), []);

// ── el vocabulario cerrado de 6 valores está completo y sin huecos ──────────
const SEIS = ["absent", "fresh", "stale", "degraded", "stopped", "recovering"];
caso("ETIQUETA_RUNTIME_STATUS cubre los 6 valores, ni uno más ni uno menos",
  Object.keys(ETIQUETA_RUNTIME_STATUS).sort(), [...SEIS].sort());
caso("COLOR_RUNTIME_STATUS cubre los 6 valores, ni uno más ni uno menos",
  Object.keys(COLOR_RUNTIME_STATUS).sort(), [...SEIS].sort());
caso("fresh y absent tienen etiqueta DISTINTA (no son sinónimos — ADR-002 Decisión 1)",
  ETIQUETA_RUNTIME_STATUS.fresh === ETIQUETA_RUNTIME_STATUS.absent ? "iguales" : "distintas", "distintas");

// ── marcaComprobacion — F1/F2 de design-review-console-61160d5-816b383 ──────
// F1: la marca declara su ALCANCE («estado nativo —») para no leerse sobre las
// secciones LEGADO de debajo, que NO sondean. F2: una marca congelada en
// background no se lee como reciente si vuelve a caber en el reloj — lleva
// FECHA cuando no es de hoy. Los esperados usan los MISMOS llamadas Intl que la
// implementación, así el test no depende del locale del entorno.
{
  const hora = (t) => new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" }).format(t);
  const diaMes = (t) => new Intl.DateTimeFormat(undefined, { day: "2-digit", month: "2-digit" }).format(t);
  const ahora = new Date("2026-09-08T15:30:00");
  const hoyMismo = new Date("2026-09-08T14:59:00").getTime();
  const ayer = new Date("2026-09-07T23:59:00").getTime();

  caso("F1: sin ninguna comprobación el texto declara ALCANCE nativo y no inventa hora",
    marcaComprobacion(undefined, ahora.getTime()), "estado nativo — última comprobación: aún ninguna");
  caso("F1: la marca de hoy lleva el prefijo de alcance nativo",
    marcaComprobacion(hoyMismo, ahora.getTime()).startsWith("estado nativo — última comprobación: "), true);
  caso("la marca de hoy es solo la hora",
    marcaComprobacion(hoyMismo, ahora.getTime()), `estado nativo — última comprobación: ${hora(hoyMismo)}`);
  caso("F2: la marca de AYER lleva fecha, no se lee como reciente al volver a caber en el reloj",
    marcaComprobacion(ayer, ahora.getTime()), `estado nativo — última comprobación: ${diaMes(ayer)} ${hora(ayer)}`);
}

console.log(malos ? `\n${malos} fallo(s)` : "\nflota: todo verde");
process.exit(malos ? 1 : 0);
