"""CLI/entrypoint del supervisor EXTERNO: compone los módulos existentes y nada más.

Lo que este fichero NO es: ni un producto nuevo ni un fixture. Es el invocador que
faltaba —la búsqueda de usos de `CiclosPorVinculo`/`SupervisorPeriodico` fuera de
tests sólo encontraba definiciones (verificado por `@codex-llminbox` y por mí sobre
`cd0fa47`)— y por eso es deliberadamente CORTO: cada línea que añade semántica
propia es una semántica que habría que certificar dos veces.

Composición (todo preexistente):
    RegistroDeSupervision   registro EXPLÍCITO de pid+arranque (sin descubrimiento)
    CiclosPorVinculo        UN ciclo por vínculo con memoria ENTRE vueltas
    SupervisorPeriodico     vueltas CONTADAS, observa; no actúa
    TransporteSupervisor    POST /native/v1/runtimes/{rti}/observations autenticado

🔑 La identidad no se DECLARA, se PREGUNTA al servidor. El CLI no acepta
`workload_id`, `lane` ni identidad de observador por bandera: salen de
`GET /native/v1/whoami` y `GET /native/v1/runtimes/{rti}` con el token de sesión.
Lo único que el operador aporta es QUÉ pid mirar — el registro explícito de
ADR-002 — y eso basta porque el vínculo se ata a (pid, arranque) capturado al
registrar. Identidad CONSULTADA (`whoami`) y credenciales DECLARADAS (el fichero
de token) quedan separadas por construcción: una credencial no dice quién es uno.

Ciclo de vida (lo que este driver GARANTIZA):
  · termina SOLO por vueltas agotadas o sesión expirada; no hay bucle infinito
  · al terminar, la memoria de ciclos TERMINA con el proceso: el nonce no se
    hereda (fabricar claves nuevas es lo que evita el replay silencioso) y la
    secuencia se rehidrata contra la autoridad por el `409` — por eso el
    fichero de sesión NO guarda seq ni nonce: sería una segunda base durable
  · el exit code refleja lo que pasó, no que acabara: `4` si quedan observaciones
    sin resolver o fallos acumulados, aunque las vueltas se hayan completado
  · un objetivo retirado durante la corrida NO se re-adopta por pid (es otro
    proceso; que lo registre quien sepa que lo es)
  · NADA mata, señala ni reinicia: este driver no tiene un camino que actúe

Prerrequisito NOMBRADO, no fabricado: los objetivos tienen que existir en una
revisión de organización ACTIVADA (`Journal.activate_organization`, entrada de
operador). Si `GET /runtimes/{rti}` no conoce el objetivo, el driver falla
cerrado indicándolo — no inventa permisos ni un endpoint público.

🔑 Una corrida finita es un ENSAYO ACOTADO: no acredita supervisión continua ni
recuperación de varios días. Lo dice la guía y lo dice el exit code.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import process_sampler as P
import supervisor_composicion as CC
import supervisor_periodico as SP
import supervisor_registro as RG
import supervisor_transport as T
from supervisor_adapter import SecuenciaSupervisor
from supervisor_runner import CicloSupervisor

RUTA_WHOAMI = "/native/v1/whoami"
RUTA_RUNTIMES = "/native/v1/runtimes/"
MAX_MOTIVO_LINEA = 120

EX_OK = 0
EX_PRECONDICION = 2
EX_SESION_EXPIRADA = 3
EX_INCIDENCIAS = 4


class PrecondicionFallida(Exception):
    """El entorno no está: auth, organización u objetivo. Se declara y para."""


def _acota(texto: str, tope: int = MAX_MOTIVO_LINEA) -> str:
    return " ".join(str(texto).split())[:tope]


def _parse_expira(valor) -> float:
    """`expires_at` del servidor a epoch. whoami lo sirve como NÚMERO epoch
    (medido por @codex-llminbox #3389); se acepta también ISO por si la
    proyección cambia, pero un valor ilegible NO se convierte en «sin plazo»:
    arrancar sin saber cuándo muere la sesión es fabricar la propia autoridad
    que digo no fabricar.
    """
    if type(valor) is int and not isinstance(valor, bool):
        return float(valor)
    if isinstance(valor, float) and valor == valor and valor not in (
            float("inf"), float("-inf")):
        return valor
    crudo = str(valor).strip()
    if crudo.endswith("Z"):
        crudo = crudo[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(crudo)
    except ValueError:
        raise PrecondicionFallida(
            f"expires_at ilegible ({_acota(valor, 40)}): no arranco sin plazo") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


class CiclosPorVinculoConContexto(CC.CiclosPorVinculo):
    """La MISMA fábrica con un cambio: la secuencia nace ATADA a la identidad.

    `CiclosPorVinculo` crea `SecuenciaSupervisor` sin `contexto`; el transporte
    configurado con contexto la rechaza ANTES de enviar (`exige_contexto`) —
    composición imposible sin atar ambas a la misma tupla (señalado por
    @codex-llminbox, MARK:codex-driver-puntos-de-continuidad).

    La única línea que cambia respecto a la fábrica heredada es el kwarg
    `contexto=`; se reimplementa SÓLO la rama de creación de `__call__` y se
    hereda todo lo demás (nonce por ciclo, `pendientes`, `retira`, reenvío de
    la petición en el aire). Que este subprocesso exista y sea tan pequeño es
    la señal de que la cura de verdad vive EN la fábrica; si sube a la rama
    principal, esta clase se borra sin tocar a nadie más.
    """

    def __init__(self, transporte_de, contextos: dict[str, tuple], **kw) -> None:
        super().__init__(transporte_de, **kw)
        self._contextos = contextos

    def __call__(self, v: RG.Vinculo):
        clave = self._clave(v)
        estado = self._ciclos.get(clave)
        if estado is None:
            estado = CC._Ciclo(
                ciclo=CicloSupervisor(
                    self._transporte_de(v),
                    SecuenciaSupervisor(en_frio=self._en_frio,
                                        contexto=self._contextos[v.runtime_instance]),
                    workload_id=v.workload_id,
                    runtime_instance=v.runtime_instance,
                    umbral_rss_bytes=self._umbral),
                nonce=self._nonce_de())
            self._ciclos[clave] = estado
        estado.pasadas += 1
        clave_base = f"{v.runtime_instance}-{estado.nonce}-{estado.pasadas}"
        return estado.ciclo.ciclo(v.handle, clave_base=clave_base)


class Driver:
    """Una corrida FINITA sobre objetivos dados. Observa; no actúa."""

    def __init__(self, base_url: str, token: str, objetivos: list[tuple[str, int]], *,
                 fichero_sesion: str, vueltas: int, intervalo_s: float,
                 timeout_s: float = 20.0,
                 reloj: callable = time.time, espera: callable = time.sleep,
                 enviar: callable | None = None) -> None:
        # El periódico ya valida su intervalo, pero con ValueError TARDÍO y
        # tras el handshake; aquí muere ANTES de hablar con nadie. `nan` pasa
        # toda comparación e `inf` duerme para siempre: finito o no arranca.
        for nombre, valor in (("intervalo_s", intervalo_s), ("timeout_s", timeout_s)):
            if isinstance(valor, bool) or not isinstance(valor, (int, float)) \
                    or not math.isfinite(valor) or valor <= 0:
                raise ValueError(f"{nombre} tiene que ser un número finito y positivo")
        self._base = base_url.rstrip("/")
        self._token = token
        self._objetivos = objetivos            # [(runtime_instance, pid)] del operador
        self._fichero_sesion = fichero_sesion
        self._vueltas = vueltas
        self._intervalo = intervalo_s
        self._timeout = timeout_s
        self._reloj = reloj
        self._espera = espera
        self._enviar = enviar                  # inyectable: los tests guionizan el socket
        self.observer_rti: str | None = None
        self.deadline: float | None = None
        self._generacion_propia: int | None = None
        self.fallos_total = 0
        self.retirados_total = 0
        self.sensor_total = 0

    # ── servidor: la ÚNICA fuente de identidad ─────────────────────────────
    @staticmethod
    def _cuerpo_de(crudo: bytes) -> dict:
        """JSON acotado a objeto. Un cuerpo ilegible NO se refleja: texto de
        otro no entra en mis mensajes (misma disciplina que el transporte)."""
        if len(crudo) > T.MAX_CUERPO_RESPUESTA:
            raise PrecondicionFallida("respuesta por encima del límite de lectura")
        try:
            cuerpo = json.loads(crudo or b"{}")
        except ValueError:
            raise PrecondicionFallida("el gateway no contestó JSON") from None
        if not isinstance(cuerpo, dict):
            raise PrecondicionFallida("la respuesta del gateway no es un objeto")
        return cuerpo

    def consulta(self, ruta: str) -> tuple[int, dict]:
        """GET autenticado. Inyectable en tests; sin redirects (no se sigue 30x)."""
        req = urllib.request.Request(f"{self._base}{ruta}")
        req.add_header("Authorization", f"Bearer {self._token}")
        try:
            with urllib.request.build_opener(T._SinRedirects()).open(
                    req, timeout=self._timeout) as r:
                return r.status, self._cuerpo_de(
                    r.read(T.MAX_CUERPO_RESPUESTA + 1))
        except urllib.error.HTTPError as exc:
            return exc.code, self._cuerpo_de(exc.read(T.MAX_CUERPO_RESPUESTA + 1))
        except (urllib.error.URLError, TimeoutError, OSError):
            # Sin texto del error: arrastra URL y a veces credencial.
            raise PrecondicionFallida("sin respuesta del gateway") from None

    def autentica(self) -> None:
        """whoami + propio runtime: identidad y plazo vienen del SERVIDOR."""
        status, cuerpo = self.consulta(RUTA_WHOAMI)
        if status != 200:
            raise PrecondicionFallida(
                f"whoami rechazado ({status}): se requiere sesión de runtime válida "
                "(POST /native/v1/sessions sobre la credencial bootstrap)")
        rti = cuerpo.get("runtime_instance")
        if not isinstance(rti, str) or not rti.strip():
            raise PrecondicionFallida(
                "whoami sin runtime_instance (¿credencial bootstrap?): el observador "
                "externo exige sesión; una credencial no declara identidad")
        if "expires_at" not in cuerpo:
            raise PrecondicionFallida("whoami sin expires_at: no arranco sin plazo")
        self.observer_rti = rti
        self.deadline = _parse_expira(cuerpo["expires_at"])
        status, propio = self.consulta(RUTA_RUNTIMES + urllib.parse.quote(rti, safe=""))
        if status != 200:
            raise PrecondicionFallida(
                f"el runtime del PROPIO observador no consta ({status}): falta la "
                "revisión de organización activada que lo declara")
        generacion = propio.get("credential_generation")
        if type(generacion) is not int or isinstance(generacion, bool):
            raise PrecondicionFallida(
                "el propio runtime no expone credential_generation")
        self._generacion_propia = generacion
        print(f"observador (consultado al servidor, no declarado): {rti} · "
              f"expira_en epoch {int(self.deadline)}", flush=True)

    def _objetivo_servidor(self, rti: str) -> tuple[str, tuple]:
        """workload_id y generación del objetivo DESDE el servidor (404 = parar)."""
        status, fila = self.consulta(RUTA_RUNTIMES + urllib.parse.quote(rti, safe=""))
        if status == 404:
            raise PrecondicionFallida(
                f"el objetivo {rti} no consta en el gateway: hace falta la entrada "
                "de operador (activate_organization) que declara ese workload; no "
                "lo fabrica el driver")
        if status != 200:
            raise PrecondicionFallida(f"runtime {rti} ilegible ({status})")
        workload = fila.get("workload_id")
        generacion = fila.get("credential_generation")
        if not isinstance(workload, str) or not workload.strip() or \
                type(generacion) is not int or isinstance(generacion, bool):
            raise PrecondicionFallida(f"runtime {rti} sin identidad servida completa")
        contexto = (self.observer_rti, self._generacion_propia, rti, generacion)
        return workload, contexto

    def corre(self) -> int:
        """Monta registro+transporte, corre `vueltas` pasadas, devuelve exit code."""
        self.autentica()
        registro = RG.RegistroDeSupervision()
        contextos: dict[str, tuple] = {}
        for rti, pid in self._objetivos:
            workload, contexto = self._objetivo_servidor(rti)
            # `registra` adopta (pid, arranque) AHORA y puede fallar ruidosamente:
            # un vínculo a medias sería supervisión aparente.
            registro.registra(workload_id=workload, runtime_instance=rti, pid=pid)
            contextos[rti] = contexto

        def transporte_de(v: RG.Vinculo) -> T.TransporteSupervisor:
            return T.TransporteSupervisor(self._base, self._token,
                                          contexto=contextos[v.runtime_instance],
                                          timeout=self._timeout, enviar=self._enviar)

        ciclos = CiclosPorVinculoConContexto(transporte_de, contextos)
        periodico = SP.SupervisorPeriodico(registro, ciclos, intervalo_s=self._intervalo)

        self._abre_sesion(registro)
        completadas = 0
        codigo = EX_OK
        for i in range(self._vueltas):
            if self._reloj() >= self.deadline:
                self._cierra_sesion(completadas, "sesion_expirada")
                print(f"PARADA: sesión expirada tras {completadas}/{self._vueltas} "
                      "vueltas; la memoria de ciclos termina con el proceso y una "
                      "nueva corrida rehidrata la secuencia contra el servidor (409).",
                      flush=True)
                return EX_SESION_EXPIRADA
            t0 = self._reloj()
            resultado = periodico.vuelta()
            completadas += 1
            self.fallos_total += len(resultado.fallos)
            self.retirados_total += len(resultado.retirados)
            self.sensor_total += len(resultado.sensor_caido)
            print(self._linea(completadas, resultado), flush=True)
            for rti, _motivo in resultado.retirados:
                # Contrato de la fábrica: soltar el ciclo del muerto — conservar
                # su secuencia sería guardar la identidad de un proceso que ya
                # no es el objetivo.
                ciclos.retira(rti)
            self._cierra_sesion(completadas, "en_marcha")
            if i + 1 < self._vueltas:
                self._espera(max(0.0, self._intervalo - (self._reloj() - t0)))

        pendientes = ciclos.pendientes()
        incidencias = bool(pendientes or self.fallos_total or self.sensor_total)
        # El libro dice la verdad ANTES de que el exit code la diga: si mirar
        # el rc exige abrir el libro, el libro ya tiene que estar escrito.
        self._cierra_sesion(completadas,
                            "completada_con_incidencias" if incidencias
                            else "completada")
        print(f"FIN: {completadas} vueltas · vínculos vivos al cierre: "
              f"{len(registro)} · retirados en la corrida: {self.retirados_total} "
              f"(NO se re-adoptan por pid) · sensor caído: {self.sensor_total} · "
              "fallos: "
              f"{self.fallos_total} · observaciones sin resolver: "
              f"{len(pendientes)}"
              + (f" {', '.join(pendientes)}" if pendientes else ""),
              flush=True)
        if incidencias:
            # Acabar las vueltas NO es éxito: ni lo que quedó en el aire ni un
            # instrumento caído se tapan con un 0 — sin sensor tampoco SE MIRA.
            codigo = EX_INCIDENCIAS
        return codigo

    @staticmethod
    def _linea(n: int, r: SP.ResultadoDeVuelta) -> str:
        """Una línea ACOTADA por vuelta. Sin token, sin cuerpo remoto, sin URL.
        Los motivos son vocabulario del muestreador, recotados por si crece."""
        obs = ",".join(r.observados) or "-"
        sensor = ",".join(f"{rti}:{_acota(m, 60)}" for rti, m in r.sensor_caido) or "-"
        retir = ",".join(f"{rti}:{_acota(m, 60)}" for rti, m in r.retirados) or "-"
        fallos = ",".join(f"{rti}:{tipo}" for rti, tipo in r.fallos) or "-"
        return (f"vuelta {n} · obs: {obs} · sensor_caido: {sensor} · "
                f"retirados: {retir} · fallos: {fallos}")

    # ── fichero de sesión: LIBRO OPERATIVO, no autoridad ───────────────────
    def _abre_sesion(self, registro: RG.RegistroDeSupervision) -> None:
        objetivos = [{"runtime_instance": v.runtime_instance, "pid": v.pid,
                      "arranque": v.handle.arranque, "workload_id": v.workload_id}
                     for v in registro.vinculos()]
        self._sesion = {"driver_pid": os.getpid(), "base_url": self._base,
                        "observer_rti": self.observer_rti,
                        "vueltas_pedidas": self._vueltas, "vueltas_completadas": 0,
                        "objetivos": objetivos, "estado": "en_marcha",
                        "iniciado_en": _iso(self._reloj())}
        self._escribe_sesion()

    def _cierra_sesion(self, completadas: int, estado: str) -> None:
        self._sesion["vueltas_completadas"] = completadas
        self._sesion["estado"] = estado
        self._sesion["actualizado_en"] = _iso(self._reloj())
        self._escribe_sesion()

    def _escribe_sesion(self) -> None:
        """600 y escritura atómica. Guarda OPERACIÓN (pid, plazos, vueltas);
        jamás seq/nonce/token — la única autoridad durable es el servidor."""
        import tempfile
        carpeta = os.path.dirname(os.path.abspath(self._fichero_sesion)) or "."
        os.makedirs(carpeta, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=carpeta, prefix=".sesion-")
        with os.fdopen(fd, "w") as f:
            json.dump(self._sesion, f, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._fichero_sesion)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def lee_token(ruta: str) -> str:
    """Contrato del fichero privado de token: REGULAR, 600 EXACTO, no vacío.

    «No 600 exacto» incluye 0o400 (el bit de dueño no es el contrato) y 0o640
    (legible por el grupo). Un token legible por otros en una máquina
    multi-carril es la fuga más barata de cometer y la más cara de rastrear:
    se rechaza ANTES de usarlo. Toda incidencia de lectura es PrecondicionFallida:
    un traceback aquí puede llevar material de credenciales a la consola.
    """
    fd = None
    try:
        # Validar el descriptor que se lee, no una consulta previa de la ruta.
        # NONBLOCK permite rechazar un fichero no regular sin esperar a un escritor.
        fd = os.open(ruta, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise PrecondicionFallida(f"{ruta} no es un fichero regular")
        modo = stat.S_IMODE(info.st_mode)
        if modo != 0o600:
            raise PrecondicionFallida(
                f"el fichero de token {ruta} tiene permisos {oct(modo)}: "
                "se exige 600 exacto")
        with os.fdopen(fd, "r", encoding="utf-8") as f:
            fd = None  # el contexto del fichero se encarga de cerrarlo
            token = f.read().strip()
    except (OSError, UnicodeError):
        raise PrecondicionFallida(f"no puedo leer el token en {ruta}") from None
    finally:
        if fd is not None:
            os.close(fd)
    if not token:
        raise PrecondicionFallida(f"token vacío en {ruta}")
    return token


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Supervisor externo (ADR-002): vueltas contadas sobre pids "
                    "REGISTRADOS. Observa; no actúa. La identidad la sirve el "
                    "gateway, no la bandera.")
    p.add_argument("--base-url", required=True)
    p.add_argument("--token-file", required=True,
                   help="fichero con el Bearer de sesión (600); nunca argv")
    p.add_argument("--objetivo", action="append", required=True, metavar="RTI:PID",
                   help="registro EXPLÍCITO; el workload_id lo sirve el gateway")
    p.add_argument("--vueltas", type=int, default=4)
    p.add_argument("--intervalo-s", type=float, default=15.0)
    p.add_argument("--timeout-s", type=float, default=20.0)
    p.add_argument("--fichero-sesion", required=True)
    args = p.parse_args(argv)

    objetivos = []
    for crudo in args.objetivo:
        rti, _, pid = crudo.rpartition(":")
        if not rti or not pid.isdigit():
            print(f"objetivo ilegible {crudo!r}: formato RTI:PID", file=sys.stderr)
            return EX_PRECONDICION
        objetivos.append((rti, int(pid)))

    if args.vueltas < 1:
        print("hay que pedir al menos una vuelta", file=sys.stderr)
        return EX_PRECONDICION

    try:
        token = lee_token(args.token_file)
        driver = Driver(args.base_url, token, objetivos,
                        fichero_sesion=args.fichero_sesion, vueltas=args.vueltas,
                        intervalo_s=args.intervalo_s, timeout_s=args.timeout_s)
        return driver.corre()
    except (PrecondicionFallida, ValueError) as exc:
        # ValueError: parámetros no finitos. Precondición, no traceback.
        print(f"PRECONDICIÓN: {exc}", file=sys.stderr)
        return EX_PRECONDICION


if __name__ == "__main__":
    sys.exit(main())
