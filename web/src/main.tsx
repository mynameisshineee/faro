import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "@/App";
import "@/index.css";

// El índice se refresca solo cada 2 s en el servidor; en el cliente basta con
// revalidar al enfocar y cada pocos segundos. Nada de sondeo agresivo: un ledger
// vivo escribe ~20 entradas/hora, no 20 por segundo.
const qc = new QueryClient({
  defaultOptions: { queries: { staleTime: 4_000, refetchOnWindowFocus: true, retry: 1 } },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={qc}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
);
