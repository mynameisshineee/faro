import { useId, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { borrarCredencialNativa, cambiarCredencialNativa, getCredencialNativa, revisionIdentidadNativa } from "@/lib/api";
import { retirarQueriesNativas } from "@/lib/queries-nativas";

/** Credencial de LECTURA del plano nativo: lo único que hace es permitir a esta
 *  consola leer la organización y el estado de los runtimes. Vive en SU clave
 *  de localStorage, jamás en la del token del canal [encargo astra runtime-root
 *  2026-09-08: sesión nativa separada del legacy].
 *
 *  Guardarla o borrarla sueltan la sesión abierta y suben el epoch
 *  (`cambiarCredencialNativa` / `borrarCredencialNativa`): ninguna respuesta de
 *  la identidad anterior llega a pintarse [bug de 955192c, encargo codex
 *  #1350-next]. El formulario es visible también con la conexión ya hecha:
 *  la credencial debe poder cambiarse o quitarse en cualquier momento.
 *
 *  Copy en lenguaje sencillo [codex process-review 2026-09-08]: nada de
 *  "bootstrap", nombres de permiso ni rutas HTTP visibles — el formulario
 *  explica QUÉ permite leer, y el veredicto de si la credencial vale lo da el
 *  servidor, no este componente. La pantalla DECLARA dónde queda la credencial
 *  (almacenamiento del navegador, persiste hasta el «Borrar») y qué NO persiste
 *  (la sesión, en memoria) [ruling cpo #2641, 2026-09-10]. */
export function CredencialNativa() {
  const queryClient = useQueryClient();
  const [valor, setValor] = useState("");
  const [guardada, setGuardada] = useState(() => Boolean(getCredencialNativa()));
  const idInput = useId();
  // RETIRAR el rastro de la identidad ANTERIOR, acotado [codex #1462]: el
  // límite se captura ANTES de mutar la credencial y la limpieza sólo toca
  // revisiones <= ese límite. El pintado limpio lo garantiza el CAMBIO de
  // clave (la revisión en la queryKey); limpiar por prefijo desnudo borraría
  // también la lectura de la identidad nueva que el re-render ya pudo
  // arrancar mientras la limpieza esperaba sus awaits.
  const conLimpiezaAcotada = (mutar: () => void) => {
    const hasta = revisionIdentidadNativa();
    mutar();
    void retirarQueriesNativas(queryClient, hasta);
  };
  return (
    <form
      className="mt-3 flex flex-col gap-2 border-t border-linea pt-3"
      onSubmit={(e) => {
        e.preventDefault();
        const v = valor.trim();
        conLimpiezaAcotada(() => cambiarCredencialNativa(v));
        setGuardada(true);
        setValor("");
      }}
    >
      <label htmlFor={idInput} className="text-[12.5px] font-medium text-tinta">
        Credencial de lectura {guardada && <span className="font-normal text-tenue">— guardada en este navegador</span>}
      </label>
      <div className="flex gap-2">
        <input
          id={idInput}
          type="password"
          autoComplete="off"
          placeholder="pega aquí tu credencial de lectura"
          value={valor}
          onChange={(e) => setValor(e.target.value)}
          className="min-w-0 flex-1 rounded-lg border border-linea bg-panel px-3 py-2 text-[16px] outline-none focus-visible:ring-2 focus-visible:ring-lacre focus-visible:ring-offset-1"
        />
        <button
          type="submit"
          disabled={!valor.trim()}
          className="rounded-lg border border-linea bg-panel px-3 py-2 text-[13px] font-medium text-tinta outline-none transition-colors duration-[var(--motion-duration-quick)] ease-[var(--motion-ease-standard)] motion-reduce:transition-none focus-visible:ring-2 focus-visible:ring-lacre focus-visible:ring-offset-1 disabled:opacity-40"
        >
          {guardada ? "Cambiar" : "Guardar"}
        </button>
        {guardada && (
          <button
            type="button"
            onClick={() => {
              conLimpiezaAcotada(() => borrarCredencialNativa());
              setGuardada(false);
            }}
            className="rounded-lg border border-linea bg-panel px-3 py-2 text-[13px] font-medium text-tinta outline-none transition-colors duration-[var(--motion-duration-quick)] ease-[var(--motion-ease-standard)] motion-reduce:transition-none focus-visible:ring-2 focus-visible:ring-lacre focus-visible:ring-offset-1"
          >
            Borrar
          </button>
        )}
      </div>
      <p className="text-[12px] text-tenue">
        Con ella esta consola puede leer qué organización está activa y cómo están los runtimes.
        La credencial queda guardada en el almacenamiento de este navegador
        y sigue ahí aunque cierres el navegador, hasta que la quites con el botón «Borrar»
        (que además cierra la sesión abierta). La sesión que abre, en cambio,
        es corta y vive sólo en memoria.
        No es el token del canal — ese vive en su propia casilla de la puerta y no se toca aquí.
      </p>
    </form>
  );
}
