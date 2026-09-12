import type {
  CargaEsperadaNativa,
  EntradaJerarquia,
  ObservacionRuntimeNativa,
  Organigrama,
  OrganizacionNativa,
  RutaEscaladoNativa,
  RuntimeStatus,
  SeamNativo,
} from "@/lib/api";

/** Lógica pura de la consola de flota. Dos secciones DELIBERADAMENTE separadas —
 *  nunca fundidas en un solo modelo — porque `ADR-002-FLEET-CONTROL-PLANE.md`
 *  Decisión 6 lo exige por su nombre: *"Native clients and the v1 console use
 *  `GET /native/v1/organization`; they do not merge the legacy endpoint into an
 *  authoritative view."*
 *
 *   · LEGADO   (`/organigrama`, `authority:false`): grafo de ROLES tal cual lo
 *     sirve v0.9. Útil como referencia, nunca como prueba de G7/G8.
 *   · NATIVO   (`/native/v1/*`, `RuntimeStatus`): el contrato G7-G10 real. Hoy
 *     siempre `disponible:false` — no existe ningún endpoint en este checkout
 *     (verificado, no supuesto) — y esa ausencia se muestra como estado de
 *     primera clase, no como tabla vacía. */

// ─── LEGADO — /organigrama, authority:false ─────────────────────────────────

export type FilaJerarquia = {
  rol: string;
  reportaA: string | null;
  capa: string | null;
  gatea: string[];
  criteriosDe: string | null;
  /** Inversa de `reporta_a`: quién reporta A este rol. No la sirve el backend —
   *  `jerarquia` sólo declara el sentido "hacia arriba"; el "hacia abajo" hay que
   *  derivarlo invirtiendo el mapa una vez por cada refresco. */
  subordinados: string[];
};

function comoLista(v: string | string[] | null | undefined): string[] {
  if (Array.isArray(v)) return v.filter((x): x is string => Boolean(x));
  return v ? [v] : [];
}

/** Filas del grafo LEGADO, con "responsable superior" (`reportaA`) y
 *  "responsables inferiores" (`subordinados`) — el segundo es el único dato de
 *  los dos que no viene servido: se calcula recorriendo el mapa una vez. Es un
 *  grafo de ROLES, nunca de agentes: no hay campo de identidad aquí y no se le
 *  añade uno client-side (ver más abajo, "sin inferencia de vínculo"). */
export function filasJerarquia(org: Organigrama | undefined): FilaJerarquia[] {
  const j: Record<string, EntradaJerarquia> = org?.jerarquia ?? {};
  const roles = Object.keys(j);
  const subordinadosPorRol = new Map<string, string[]>();
  for (const rol of roles) {
    const padre = j[rol]?.reporta_a;
    if (!padre) continue;
    const lista = subordinadosPorRol.get(padre) ?? [];
    lista.push(rol);
    subordinadosPorRol.set(padre, lista);
  }
  return roles
    .map((rol) => {
      const e = j[rol] ?? {};
      return {
        rol,
        reportaA: e.reporta_a ?? null,
        capa: e.capa ?? null,
        gatea: comoLista(e.gatea),
        criteriosDe: e.criterios_de ?? null,
        subordinados: (subordinadosPorRol.get(rol) ?? []).sort(),
      };
    })
    .sort((a, b) => a.rol.localeCompare(b.rol));
}

/** Dos causas distintas de "sin jerarquía", y el propio servicio ya las separa
 *  en `aviso` [servicio.py::organigrama]: sin montar (no hay fuente firmada) o
 *  montada sin el campo. Confundirlas manda a revisar el montaje cuando el
 *  problema es el CONTENIDO, o al revés — el mismo defecto que `motivoSalud`
 *  existe para no repetir con `/health`. */
export type MotivoOrganigramaVacio = "sin-montar" | "sin-jerarquia";

export function motivoOrganigramaVacio(org: Organigrama): MotivoOrganigramaVacio | null {
  if (org.roles > 0) return null;
  return org.source_sha256 === null ? "sin-montar" : "sin-jerarquia";
}

/** Ruta de escalado completa desde un ROL: no sólo el responsable inmediato, la
 *  cadena entera hasta que alguien ya no reporte a nadie en el mapa. Con guarda
 *  de ciclo — `jerarquia` la edita una persona a mano [PROTOCOL.md], y un ciclo
 *  ahí sería indistinguible de "el organigrama no termina" sin este tope.
 *
 *  Nótese lo que esto NO hace: no resuelve a qué AGENTE pertenece un rol. Esa es
 *  la línea que `ADR-002` traza en Decisión 6 y que un cruce por coincidencia de
 *  nombre borraría en silencio — ver la nota de "sin inferencia de vínculo" más
 *  abajo. */
export function rutaEscalado(
  jerarquia: Record<string, EntradaJerarquia>,
  rolInicial: string,
  tope = 12,
): string[] {
  const ruta: string[] = [];
  const vistos = new Set<string>([rolInicial]);
  let actual = rolInicial;
  while (ruta.length < tope) {
    const siguiente = jerarquia[actual]?.reporta_a;
    if (!siguiente || vistos.has(siguiente)) break;
    ruta.push(siguiente);
    vistos.add(siguiente);
    actual = siguiente;
  }
  return ruta;
}

// ─── NATIVO — /native/v1/*, RuntimeStatus, hoy siempre no-disponible ────────

/** Estado del PANEL nativo (no de una fila): tres causas que no se pueden
 *  confundir sin fabricar un "verde" que el propio dato no sostiene [ADR-002
 *  Decisión 8, y el finding `qa-amend2-...` que nombra exactamente este riesgo]:
 *
 *   · `no-disponible` — el seam dice que el endpoint no existe. "No sé" —
 *     nunca se lee como "cero fallos".
 *   · `vacio`         — el endpoint respondió y NO hay absolutamente nada que
 *     pintar: cero roles en la organización activa Y cero runtimes observados
 *     (ni siquiera uno huérfano). Es un dato real, distinto del anterior.
 *   · `con-datos`     — hay filas que pintar, INCLUIDA la vacancia total: una
 *     organización con roles y cero runtimes observados produce N filas
 *     `absent` (una por rol), no una tabla en blanco. `roles.length === 0`
 *     por sí solo NO basta para `vacio` — con roles vacíos pero runtimes
 *     huérfanos presentes, esos huérfanos siguen siendo filas que pintar
 *     [`filasRuntimesNativos` los conserva; ver su comentario].
 *
 *  🔴 Corregido tras `codex-llminbox` (`MARK:codex-console-f018122-absent-
 *  parcial-y-cinco-p1-abiertos`): la versión anterior devolvía `vacio` en
 *  cuanto `runtimes.datos.length === 0`, aunque la organización tuviera roles
 *  — la vacancia TOTAL (cero runtimes en absoluto) mostraba el mensaje
 *  genérico de "sin datos" en vez de la tabla de N filas `absent` que la
 *  vacancia PARCIAL ya mostraba desde `f018122`. Dos formas de la MISMA causa
 *  tratadas como estados distintos era la inconsistencia real.
 *
 *  `sin-organizacion` es el 404 tipado `SUBJECT_NOT_FOUND` [D12, contrato
 *  codex #1343]: el servidor está SANO y responde que no hay revisión de
 *  organización activa. Respuesta, no avería — por eso no cae en
 *  `no-disponible`, y tampoco en `vacio` (ése afirma "respondió y no hay
 *  nada que pintar"; aquí ni siquiera hay revisión contra la que cruzar). */
export type EstadoPanelNativo = "no-disponible" | "sin-organizacion" | "vacio" | "con-datos";

export function estadoPanelNativo(
  organizacion: SeamNativo<Pick<OrganizacionNativa, "roles">> | undefined,
  runtimes: SeamNativo<ObservacionRuntimeNativa[]> | undefined,
): EstadoPanelNativo | null {
  if (!organizacion || !runtimes) return null; // loading todavía sin resolver — lo decide isLoading
  if (!organizacion.disponible && organizacion.ausente) return "sin-organizacion";
  if (!organizacion.disponible || !runtimes.disponible) return "no-disponible";
  if (organizacion.datos.roles.length === 0 && runtimes.datos.length === 0) return "vacio";
  return "con-datos";
}

/** Motivo por el que el panel no tiene datos que enseñar — sólo tiene sentido
 *  cuando `estadoPanelNativo` dio `no-disponible`; el motivo real es el mismo
 *  para los dos seams hoy (ninguno de los dos endpoints existe), pero se
 *  comprueban los dos por separado para el día en que dejen de coincidir. */
export function motivoPanelNoDisponible(
  organizacion: SeamNativo<unknown> | undefined,
  runtimes: SeamNativo<ObservacionRuntimeNativa[]> | undefined,
): string | null {
  if (organizacion && !organizacion.disponible) return organizacion.motivo;
  if (runtimes && !runtimes.disponible) return runtimes.motivo;
  return null;
}

/** Estado de la TABLA de runtimes, decidido CON SUS PROPIOS datos primero —
 *  cura E8b [sdet: 401 persistente en organization ARRASTRÓ al panel vecino,
 *  0 filas con credencial activa]: la shared `estadoPanelNativo` mezcla los
 *  dos seams y una avería del org borra las filas que runtimes SÍ tiene. El
 *  aislamiento no inventa datos: si runtimes trae observaciones, se pintan
 *  (las que no cruzan con workloads ya se listan aparte en
 *  `filasRuntimesNativos` — "dato, no ruido"); si NO trae, el estado lo
 *  decide el vecino igual que antes (incluida la cara2 de E6c: banner con el
 *  motivo del org). La tabla de organización nunca usó esta función: deriva
 *  lo suyo de su propio seam. */
export function estadoTablaRuntimes(
  organizacion: SeamNativo<Pick<OrganizacionNativa, "roles">> | undefined,
  runtimes: SeamNativo<ObservacionRuntimeNativa[]> | undefined,
): EstadoPanelNativo | null {
  if (!runtimes) return null; // loading todavía sin resolver — lo decide isLoading
  if (!runtimes.disponible) return "no-disponible"; // avería PROPIA
  if (runtimes.datos.length > 0) return "con-datos"; // AISLAMIENTO E8b: filas propias pese al vecino caído
  // Sin datos propios: el vecino decide (misma tabla de causas que `estadoPanelNativo`).
  if (!organizacion) return null;
  if (!organizacion.disponible && organizacion.ausente) return "sin-organizacion";
  if (!organizacion.disponible) return "no-disponible";
  if (organizacion.datos.roles.length === 0) return "vacio";
  return "con-datos"; // org sana, cero observaciones: filas `absent` por workload
}

/** Fila para pintar — puede ser una observación real o un `absent` INFERIDO por
 *  esta función, nunca por el componente. `runtime_instance` es `null` sólo en
 *  el segundo caso: no hay instancia que nombrar cuando nunca se ha visto una
 *  (o es una vacante real — la fila absent REAL del servidor PUEDE llevar
 *  vínculo: CHECK unidireccional, ver la doc del tipo en api.ts). */
export type FilaRuntimeNativa = Omit<ObservacionRuntimeNativa, "runtime_instance" | "workload_id"> & {
  runtime_instance: string | null;
  workload_id: string | null;
};

/** Cruza organización × runtimes — el hallazgo de `design-review-console-
 *  74ffbe7-ship-con-1-hallazgo-adelante`: iterar SÓLO `runtimes.datos` deja caer
 *  en silencio lo que la organización espera y nunca se ha observado — el caso
 *  que ADR-002 Decisión 1 define para `absent`, "an expected workload in the
 *  active organisation revision [que] has no active runtime binding or has
 *  never been observed". No se infiere de un id desconocido (eso seguiría
 *  prohibido): se infiere de los WORKLOADS que la propia organización activa
 *  declara esperar. Las observaciones con un workload que la organización NO
 *  reconoce no se descartan — son información, no ruido, y se listan aparte. */
/** Plantilla común de la fila `absent` inferida — D8 [RULING cto #1252]: lane y
 *  número de revisión se HEREDAN de `organizacion.revision`, nunca de un runtime
 *  que no existe. Sin runtime, no hay generación de credencial ni secuencia de
 *  estado que citar — `0`/`null`, nunca un valor plausible (`status_seq: 0` es
 *  "no ha habido observación", no "la primera observación fue la 0"). */
function filaAbsent(
  revision: Pick<OrganizacionNativa, "revision">["revision"],
  role: string,
  workload_id: string | null,
): FilaRuntimeNativa {
  return {
    runtime_instance: null,
    workload_id,
    lane: revision.lane,
    principal: null,
    role,
    credential_generation: null,
    status: "absent",
    detector_state: null,
    status_seq: 0,
    status_since: null,
    last_observed_at: null,
    cause_id: null,
    transition_id: null,
    receipt_id: null,
    organization_revision: revision.revision,
  };
}

export function filasRuntimesNativos(
  organizacion: Pick<OrganizacionNativa, "revision" | "roles" | "workloads">,
  runtimes: ObservacionRuntimeNativa[],
): FilaRuntimeNativa[] {
  // ── D7 [encargo codex #1308 + contrato codex #1343] — cruce por WORKLOAD,
  // camino ÚNICO: el gateway sirve `workloads[]` SIEMPRE (aunque vacío) y
  // `workload_id` OBLIGATORIO por fila [#1343, verificado cto #1348]. La unidad
  // de la ausencia es el workload (`runtime_status` PK (lane, workload_id);
  // `expected_workloads` declara cuántos espera cada rol) — un rol con 2
  // workloads esperados y 1 observación produce DOS filas. `workloads: []` con
  // roles presentes NO genera absent: un rol sin workloads esperados no se
  // espera en ejecución. No hay cruce por ROL: sería una segunda semántica que
  // ningún wire real alcanza.
  const filas: FilaRuntimeNativa[] = [];
  const workloads: CargaEsperadaNativa[] = organizacion.workloads ?? [];
  const esperados = new Set(workloads.map((w) => w.workload_id));
  const observadasPorWorkload = new Map<string, ObservacionRuntimeNativa>();
  const sinCargaEsperada: ObservacionRuntimeNativa[] = [];
  for (const r of runtimes) {
    if (r.workload_id && esperados.has(r.workload_id)) observadasPorWorkload.set(r.workload_id, r);
    else sinCargaEsperada.push(r);
  }
  for (const w of workloads) {
    const obs = observadasPorWorkload.get(w.workload_id);
    filas.push(obs ?? filaAbsent(organizacion.revision, w.role, w.workload_id));
  }
  // Observaciones reales sin workload declarado, o con uno que la organización
  // activa NO espera: dato, no ruido — se listan igual, nunca se pierden.
  filas.push(...sinCargaEsperada.map((o) => ({ ...o, workload_id: o.workload_id ?? null })));

  return filas.sort(
    (a, b) =>
      (a.role ?? "").localeCompare(b.role ?? "") ||
      (a.workload_id ?? "").localeCompare(b.workload_id ?? "") ||
      (a.runtime_instance ?? "").localeCompare(b.runtime_instance ?? ""),
  );
}

/** Marca "última comprobación" del bloque NATIVO — F1/F2 de la review de design
 *  (`MARK:design-review-console-61160d5-816b383-ship-2-hallazgos-no-bloqueantes`).
 *  F1: declara su ALCANCE («estado nativo») para que nadie la lea sobre las
 *  secciones LEGADO de debajo, que NO sondean. F2: una marca congelada en
 *  background (el refetchInterval pausa sin foco) no se lee como reciente si
 *  vuelve a caber en el reloj — lleva FECHA cuando no es de hoy. */
export function marcaComprobacion(dataUpdatedAt: number | undefined, ahora: number): string {
  const prefijo = "estado nativo — última comprobación: ";
  if (!dataUpdatedAt) return `${prefijo}aún ninguna`;
  const esHoy = (() => {
    const a = new Date(dataUpdatedAt);
    const h = new Date(ahora);
    return a.getFullYear() === h.getFullYear() && a.getMonth() === h.getMonth() && a.getDate() === h.getDate();
  })();
  const hora = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" }).format(dataUpdatedAt);
  return esHoy
    ? `${prefijo}${hora}`
    : `${prefijo}${new Intl.DateTimeFormat(undefined, { day: "2-digit", month: "2-digit" }).format(dataUpdatedAt)} ${hora}`;
}

export type FilaOrganizacionNativa = {
  rol: string;
  reportaA: string | null;
  revisores: string[];
  // D6: estructuradas — {trigger_code, target_role} tal cual llegan; la tabla
  // decide cómo pintarlas. `capa` (D5) viaja como INT del wire.
  rutasEscalado: RutaEscaladoNativa[];
  capa: number | null;
  subordinados: string[];
};

/** Filas del grafo NATIVO — mismo cálculo de "responsables inferiores" que el
 *  legado (`filasJerarquia`), aplicado al tipo autoritativo cuando exista. No es
 *  código compartido con el legado a propósito: son dos familias de tipos que
 *  `ADR-002` Decisión 6 exige mantener separadas, y una función genérica sobre
 *  "algo con reports_to" acoplaría de nuevo lo que la separación de tipos
 *  existe para impedir. */
export function filasOrganizacionNativa(org: Pick<OrganizacionNativa, "roles"> | undefined): FilaOrganizacionNativa[] {
  const roles = org?.roles ?? [];
  const subordinadosPorRol = new Map<string, string[]>();
  for (const r of roles) {
    if (!r.reports_to) continue;
    const lista = subordinadosPorRol.get(r.reports_to) ?? [];
    lista.push(r.role);
    subordinadosPorRol.set(r.reports_to, lista);
  }
  return roles
    .map((r) => ({
      rol: r.role,
      reportaA: r.reports_to,
      revisores: [...r.reviewers],
      rutasEscalado: [...r.escalation_routes],
      capa: r.layer,
      subordinados: (subordinadosPorRol.get(r.role) ?? []).sort(),
    }))
    .sort((a, b) => a.rol.localeCompare(b.rol));
}

/** Vocabulario CERRADO de 6 valores [ADR-002 Decisión 1] — un único sitio para
 *  no repetir las etiquetas en cada componente. `absent` se pinta igual de
 *  "neutro" que "vacío": la ADR es explícita en que NUNCA se infiere de un id
 *  desconocido, así que la etiqueta no debe sonar a fallo. */
export const ETIQUETA_RUNTIME_STATUS: Record<RuntimeStatus, string> = {
  absent: "ausente",
  fresh: "fresco",
  stale: "rancio",
  degraded: "degradado",
  stopped: "detenido",
  recovering: "recuperando",
};

/** Color por estado — separado de la etiqueta para que un test pueda comprobar
 *  el vocabulario cerrado sin acoplarse a los tokens Tailwind. */
export const COLOR_RUNTIME_STATUS: Record<RuntimeStatus, string> = {
  absent: "text-tenue",
  fresh: "text-bien",
  stale: "text-aviso",
  degraded: "text-mal",
  stopped: "text-mal",
  recovering: "text-aviso",
};
