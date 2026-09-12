// La interfaz tiene que DECIR que la firma es autodeclarada.
//
// El servicio manda `actor_identity_verified: false` y `actor_provenance:
// "self_declared"` en CADA entrada, y su comentario explica por qué: «un campo
// estructurado de un índice consultable se lee como hecho del sistema mucho más que una
// firma al pie. Sin desmentido, se lee como afirmado».
//
// MEDIDO el 2026-09-04: el front tenía CERO apariciones de esos dos campos en todo su
// código, así que pintaba el `actor` como un hecho. `/inbox` sí lo advierte en su
// cabecera de texto; la interfaz no.
//
// Se comprueba sobre el FUENTE y no sobre el DOM porque no hay arnés de render aquí —
// y lo que hay que impedir es que alguien vuelva a tirar el campo, no un píxel.
import { readFileSync } from "node:fs";

let fallos = 0;
const ok = (m) => console.log(`  ✓ ${m}`);
const mal = (m, esp, obt) => { console.log(`  ✗ ${m}\n     esperado: ${esp} · obtenido: ${obt}`); fallos++; };

const api = readFileSync(new URL("./api.ts", import.meta.url), "utf8");
const det = readFileSync(new URL("../components/Detalle.tsx", import.meta.url), "utf8");

api.includes("actor_identity_verified")
  ? ok("el tipo Entrada declara el desmentido de atribución")
  : mal("Entrada declara actor_identity_verified", "declarado",
        "ausente: el campo llega y TypeScript lo esconde");

det.includes("actor_identity_verified")
  ? ok("el detalle LEE el desmentido")
  : mal("el detalle lee actor_identity_verified", "leído", "ausente: se pinta el actor como hecho");

/^[\s\S]*actor_identity_verified === false[\s\S]*$/.test(det)
  ? ok("y sólo avisa cuando el servicio dice que NO está verificada")
  : mal("el aviso va condicionado", "=== false",
        "incondicional: avisaría también de una firma que sí estuviera verificada");

// ⊖ de no pasarse: el aviso NO puede estar en la fila de la lista. Sale en 99.780
// entradas y convierte una advertencia en ruido de fondo, que es como se deja de leer.
const fila = readFileSync(new URL("../components/Fila.tsx", import.meta.url), "utf8");
!fila.includes("autodeclarada")
  ? ok("⊖ y NO se repite en cada fila de la lista (sería ruido, no aviso)")
  : mal("el aviso no va en la fila", "ausente en Fila.tsx", "presente: 99.780 veces deja de leerse");

console.log(fallos === 0 ? "\natribución: todo verde" : `\natribución: ${fallos} fallo(s)`);
process.exit(fallos > 0 ? 1 : 0);
