import { useId, useState } from "react";
import { api, setToken } from "@/lib/api";

/** Puerta. El token no viaja en el HTML servido: lo teclea la persona una vez y
 *  queda en SU navegador. `/health` no pide token (lo necesita el healthcheck del
 *  contenedor), así que para validar se prueba contra `/stat`, que sí lo exige.
 *
 *  a11y: ninguna de las dos versiones previas (ui.html ni el skeleton React) tenía
 *  <label> en el input — solo placeholder, que un lector de pantalla no siempre
 *  expone como nombre accesible [brief §7]. Se añade aquí. */
export function Puerta({ onEntrar }: { onEntrar: () => void }) {
  const [valor, setValor] = useState("");
  const [error, setError] = useState("");
  const idToken = useId();
  const idError = useId();
  return (
    <div className="grid h-full place-items-center p-6">
      <form
        className="flex w-full max-w-sm flex-col gap-3"
        onSubmit={async (e) => {
          e.preventDefault();
          setToken(valor.trim());
          try {
            await api.estado();
            onEntrar();
          } catch {
            setError("Ese token no vale.");
          }
        }}
      >
        <h1 className="font-mono text-lg">llminbox</h1>
        <p className="text-sm text-apagado">
          Pega el contenido de <code className="font-mono">~/.llminbox.token</code>. Se guarda
          en este navegador y sólo viaja a <code className="font-mono">127.0.0.1</code>.
        </p>
        <label htmlFor={idToken} className="sr-only">
          Token
        </label>
        <input
          id={idToken}
          type="password"
          autoComplete="off"
          placeholder="token"
          required
          value={valor}
          onChange={(e) => setValor(e.target.value)}
          aria-describedby={error ? idError : undefined}
          aria-invalid={error ? true : undefined}
          className="rounded-lg border border-linea bg-panel px-3 py-2 text-[16px] outline-none focus-visible:ring-2 focus-visible:ring-lacre focus-visible:ring-offset-1"
        />
        <button
          type="submit"
          className="rounded-lg bg-lacre px-3 py-2 font-medium text-papel outline-none transition-colors duration-[var(--motion-duration-quick)] ease-[var(--motion-ease-standard)] motion-reduce:transition-none focus-visible:ring-2 focus-visible:ring-lacre focus-visible:ring-offset-1"
        >
          Entrar
        </button>
        {error && (
          <p id={idError} role="alert" className="text-sm text-mal">
            {error}
          </p>
        )}
      </form>
    </div>
  );
}
