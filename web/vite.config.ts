import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwind from "@tailwindcss/vite";
import path from "node:path";

export default defineConfig({
  plugins: [react(), tailwind()],
  resolve: { alias: { "@": path.resolve(import.meta.dirname, "src") } },
  // Se compila DENTRO de la imagen y el servicio lo sirve como estático: el
  // usuario sigue haciendo `docker compose up` y nada más. La compilación es del
  // que construye la imagen, no del que la usa.
  build: { outDir: "../static", emptyOutDir: true },
  server: { proxy: { "/health": "http://127.0.0.1:8077", "/stat": "http://127.0.0.1:8077",
                     "/entries": "http://127.0.0.1:8077", "/inbox": "http://127.0.0.1:8077",
                     "/lint": "http://127.0.0.1:8077", "/roster": "http://127.0.0.1:8077",
                     "/cursor": "http://127.0.0.1:8077", "/organigrama": "http://127.0.0.1:8077" } },
});
