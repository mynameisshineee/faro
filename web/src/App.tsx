import { Fragment, useEffect, useRef, useState } from "react";
import { getToken, type Entrada } from "@/lib/api";
import { construirCenso } from "@/lib/censo";
import { calcularSeparador } from "@/lib/separador";
import { textoDeCuenta } from "@/lib/cuenta";
import { motivoVacio } from "@/lib/titular";
import { useCursores, useEntradas, useEstado, useMuestra, useRoster, useSalud } from "@/lib/queries";
import { Puerta } from "@/components/Puerta";
import { Sidebar } from "@/components/Sidebar";
import { Header } from "@/components/Header";
import { BannerSalud } from "@/components/BannerSalud";
import { EstadoVacio } from "@/components/EstadoVacio";
import { Separador } from "@/components/Separador";
import { Fila } from "@/components/Fila";
import { Detalle } from "@/components/Detalle";
import { Flota } from "@/components/Flota";
import { cn } from "@/lib/utils";

type Vista = "bandeja" | "flota";

export default function App() {
  const [entrado, setEntrado] = useState(Boolean(getToken()));
  const [vista, setVista] = useState<Vista>("bandeja");
  const [ledger, setLedger] = useState("");
  const [yo, setYo] = useState(localStorage.getItem("llminbox.yo") ?? "");
  const [buscar, setBuscar] = useState("");
  const [buscarDebounced, setBuscarDebounced] = useState("");
  const [tipo, setTipo] = useState("");
  const [soloMias, setSoloMias] = useState(false);
  const [menuAbierto, setMenuAbierto] = useState(false);
  // La entrada abierta se guarda por `eid` y no por índice: la lista se refresca
  // sola cada pocos segundos y con un índice el panel cambiaría de contenido
  // debajo del que lee en cuanto llega una entrada nueva por delante.
  const [abiertaEid, setAbiertaEid] = useState<string | null>(null);
  const sidebarRef = useRef<HTMLElement>(null);
  const listaRef = useRef<HTMLDivElement>(null);

  // Búsqueda debounced 260ms — mismo umbral que ui.html, para no disparar una
  // consulta por cada tecla sobre un ledger de miles de entradas.
  useEffect(() => {
    const t = setTimeout(() => setBuscarDebounced(buscar), 260);
    return () => clearTimeout(t);
  }, [buscar]);

  const salud = useSalud(entrado);
  const estado = useEstado(entrado);
  const roster = useRoster(entrado);
  const ledgersConfigurados = estado.data?.length ?? 0;
  const muestra = useMuestra(entrado && ledgersConfigurados > 0);
  const cursores = useCursores(yo, entrado);
  const filas = useEntradas({ ledger, q: buscarDebounced, tipo, soloMias, yo }, entrado);

  const abrirMenu = () => setMenuAbierto(true);
  const cerrarMenu = () => {
    setMenuAbierto(false);
    document.getElementById("abrir-menu")?.focus();
  };
  useEffect(() => {
    if (menuAbierto) sidebarRef.current?.querySelector<HTMLElement>("button, a, select, input")?.focus();
  }, [menuAbierto]);
  useEffect(() => {
    if (!entrado || !menuAbierto) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") cerrarMenu();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entrado, menuAbierto]);

  // Teclado: j/k para moverse, Esc para cerrar. Un lector de logs se recorre con
  // las manos en el teclado; obligar al ratón para bajar una fila es lo que hace
  // que una lista larga se abandone.
  const entradas = filas.data?.datos;
  const listaFilas = entradas ?? [];
  useEffect(() => {
    if (!entrado) return;
    const onKey = (ev: KeyboardEvent) => {
      // Nunca dentro de un campo: 'j' en el buscador tiene que escribir una jota.
      const t = ev.target as HTMLElement | null;
      if (t && (t.tagName === "INPUT" || t.tagName === "SELECT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
      if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
      if (ev.key === "Escape") return setAbiertaEid(null);
      if (ev.key !== "j" && ev.key !== "k") return;
      if (listaFilas.length === 0) return;
      ev.preventDefault();
      const i = listaFilas.findIndex((x: Entrada) => x.eid === abiertaEid);
      const siguiente = ev.key === "j" ? Math.min(i + 1, listaFilas.length - 1) : Math.max(i - 1, 0);
      // Sin selección, 'j' abre la primera y 'k' la última: entrar por el extremo
      // que corresponde al sentido de la tecla evita el salto raro al primer uso.
      const destino = i === -1 ? (ev.key === "j" ? 0 : listaFilas.length - 1) : siguiente;
      const elegida = listaFilas[destino];
      if (!elegida) return;
      setAbiertaEid(elegida.eid);
      listaRef.current
        ?.querySelector(`[data-eid="${elegida.eid}"]`)
        ?.scrollIntoView({ block: "nearest" });
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [entrado, listaFilas, abiertaEid]);

  if (!entrado) return <Puerta onEntrar={() => setEntrado(true)} />;

  const censo = construirCenso(roster.data);
  const tipos = [...new Set((muestra.data ?? []).map((x) => x.tipo).filter((t): t is string => Boolean(t)))].sort();
  const agentes = [
    ...new Set((muestra.data ?? []).flatMap((x) => [x.actor, ...(x.to ?? [])]).filter((a): a is string => Boolean(a))),
  ].sort();
  const filtrosActivos = buscar !== "" || tipo !== "" || soloMias;
  // El `=== 120` cableado no era el `limit` que se pide (120 aquí, 400 en la muestra) y
  // sobre todo NO era el recorte real: el servicio recorta por presupuesto de bytes y
  // devolvía 10 filas donde se pedían 400. Ahora se lee lo que él declara.
  const cuentaTexto = entradas ? textoDeCuenta(entradas.length, filas.data!.capadas) : "";
  const { marcas, n: nuevas } = calcularSeparador(entradas ?? [], yo, cursores.data);

  const seleccionarLedger = (l: string) => {
    setLedger(l);
    cerrarMenu();
  };
  const cambiarYo = (nombre: string) => {
    setYo(nombre);
    localStorage.setItem("llminbox.yo", nombre);
  };
  const quitarFiltros = () => {
    setBuscar("");
    setBuscarDebounced("");
    setTipo("");
    setSoloMias(false);
  };

  return (
    <div className="grid h-full min-[760px]:grid-cols-[252px_1fr]">
      {menuAbierto && (
        <button
          type="button"
          aria-label="Cerrar el menú"
          onClick={cerrarMenu}
          className="fixed inset-0 z-[15] cursor-pointer border-0 bg-black/45 p-0 outline-none transition-opacity duration-[var(--motion-duration-drawer)] ease-[var(--motion-ease-standard)] motion-reduce:transition-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-lacre min-[760px]:hidden"
        />
      )}
      <Sidebar
        ref={sidebarRef}
        ledgers={estado.data ?? []}
        totalLedgers={ledgersConfigurados}
        ledgerActivo={ledger}
        onSeleccionar={seleccionarLedger}
        agentes={agentes}
        yo={yo}
        onCambiarYo={cambiarYo}
        abierta={menuAbierto}
        censo={censo}
      />
      <main className="flex flex-col overflow-hidden">
        {vista === "bandeja" && (
          <Header
            ledger={ledger}
            tipos={tipos}
            buscar={buscar}
            onBuscarChange={setBuscar}
            tipo={tipo}
            onTipoChange={setTipo}
            soloMias={soloMias}
            onSoloMiasChange={setSoloMias}
            cuenta={cuentaTexto}
            deshabilitado={ledgersConfigurados === 0}
            menuAbierto={menuAbierto}
            onAbrirMenu={abrirMenu}
          />
        )}
        {/* Dos vistas, no dos apps: comparten Puerta, token y censo. "Flota" es
            estrecha a propósito [encargo: "consola operacional ESTRECHA"] — no
            reimplementa el shell de la bandeja, sólo cuelga otro panel de él. */}
        <div role="tablist" aria-label="Vista" className="flex gap-1 border-b border-linea bg-panel px-4 pt-1">
          {(["bandeja", "flota"] as const).map((v) => (
            <button
              key={v}
              type="button"
              role="tab"
              id={`tab-${v}`}
              aria-selected={vista === v}
              aria-controls={`panel-${v}`}
              onClick={() => setVista(v)}
              className={cn(
                "rounded-t-md border border-b-0 px-3 py-1.5 text-[12.5px] outline-none transition-colors duration-[var(--motion-duration-quick)] motion-reduce:transition-none focus-visible:ring-2 focus-visible:ring-lacre focus-visible:ring-offset-1",
                vista === v
                  ? "border-linea bg-alzado font-medium text-tinta"
                  : "border-transparent text-apagado hover:text-tinta",
              )}
            >
              {v === "bandeja" ? "Bandeja" : "Flota"}
            </button>
          ))}
        </div>

        {vista === "flota" ? (
          <div id="panel-flota" role="tabpanel" aria-labelledby="tab-flota" className="flex min-h-0 flex-1 flex-col">
            <Flota entrado={entrado} />
          </div>
        ) : (
          <div id="panel-bandeja" role="tabpanel" aria-labelledby="tab-bandeja" className="flex min-h-0 flex-1 flex-col">
            <BannerSalud salud={salud.data} estado={estado.data} />
            {/* Lista + detalle. La lista se queda escaneable y el ensayo entero —el
                cuerpo mediano de estos ledgers son 2.130 caracteres— tiene su sitio a
                la derecha. Por debajo de 1.100 px hay una sola columna y el detalle
                pasa a capa: partir en dos una pantalla estrecha da dos columnas malas
                en vez de una buena. */}
            <div className="grid min-h-0 flex-1 min-[1100px]:grid-cols-[minmax(340px,440px)_1fr]">
              <div ref={listaRef} className="min-w-0 overflow-y-auto">
                {filas.isLoading ? (
                  <p role="status" className="p-10 text-center text-sm text-apagado">
                    cargando…
                  </p>
                ) : !entradas || entradas.length === 0 ? (
                  <EstadoVacio
                    motivo={motivoVacio({
                      ledgersConfigurados,
                      filtrosActivos,
                      estado: estado.data ?? [],
                      ledgerSeleccionado: ledger,
                    })}
                    onQuitarFiltros={quitarFiltros}
                  />
                ) : (
                  entradas.map((e: Entrada, i: number) => (
                    <Fragment key={`${e.ledger}:${e.eid}`}>
                      {(marcas[i] ?? false) && <Separador n={nuevas} />}
                      <div data-eid={e.eid}>
                        <Fila
                          e={e}
                          yo={yo}
                          censo={censo}
                          activa={e.eid === abiertaEid}
                          onAbrir={() => setAbiertaEid(e.eid === abiertaEid ? null : e.eid)}
                        />
                      </div>
                    </Fragment>
                  ))
                )}
              </div>
              <Detalle
                e={listaFilas.find((x: Entrada) => x.eid === abiertaEid) ?? null}
                yo={yo}
                censo={censo}
                onCerrar={() => setAbiertaEid(null)}
              />
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
