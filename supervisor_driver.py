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

`--continuo` es la opción EXPLÍCITA de operación sostenida: vueltas sin tope,
con la memoria de ciclos y pendientes viva EN EL PROCESO, renovación de sesión
al acercarse el plazo por el contrato REAL del gateway (`POST
/native/v1/sessions/refresh`, ttl 30..3600) y cierre ordenado ante
SIGINT/SIGTERM.

La renovación es ROTACIÓN — token nuevo, rti hijo NUEVO y el token previo
revocado en la misma transacción — pero el hijo CONSERVA principal, role,
lane y `generation` (medido en fuente, `coordination.refresh_session`): es
renovación autenticada de la MISMA autoridad, no un agente ajeno. Por eso el
camino normal la ADOPTA, con doble validación y sin soltar nada en el aire:

  1. recibo completo: token, runtime_instance, expires_at y generation;
  2. `whoami` CON EL TOKEN HIJO: mismo rti que el recibo, plazo presente y
     autoridad (principal/role/lane) IDÉNTICA a la consultada al arranque.
  3. Transición SÓLO sin peticiones en vuelo: si queda algo pendiente, la
     vuelta lo reanuda con los mismos bytes y clave bajo la sesión VIGENTE;
     si no puede resolverse antes de expirar, parada visible con los
     pendientes declarados — sin revocación anticipada ni olvido.
  4. Adopción: contextos y secuencias NUEVOS para el rti hijo (nonce nuevo:
     una secuencia jamás se reutiliza con identidad distinta), conservando
     los vínculos pid+arranque y el estado del sensor (las transiciones de
     degradación ya vistas no se pierden).

Si el recibo no se puede validar, el whoami del hijo no cuadra o la autoridad
cambia (principal/role/lane distintos), la corrida termina VISIBLE con lo
pendiente declarado. La sesión del supervisor no exige nueva activación del
organigrama por cada token renovado: la generación del observador que entra
en el contexto sale del RECIBO (y del whoami del hijo cuando el gateway la
expone, campo `generation` añadido a `_identity_wire` por el encargo
MARK:codex-supervisor-renovacion-real), no del estado del runtime propio.
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
RUTA_REFRESCO = "/native/v1/sessions/refresh"
# Contrato del refresh, MEDIDO en 2626de2 (`coordination.SessionRequest`):
# ttl_s ge=30 le=3600, defecto 900. La respuesta (`_session_wire`) trae la
# sesión HIJA con `generation` (no `credential_generation`) y, con el gateway
# actual, un `runtime_instance` NUEVO.
TTL_MIN, TTL_MAX = 30, 3600
MAX_MOTIVO_LINEA = 120
# Techo de lectura del token (menor ②a de security, MARK:security-revision-del-
# supervisor-2626de2): misma forma que la referencia de la casa
# (runtime_root: PEPPER_MAX_BYTES). Un Bearer de sesión sobra con mucho menos.
MAX_BYTES_TOKEN = 4096

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

    `estados_de` es la segunda extensión (renovación validada): al reconstruir
    los ciclos bajo el rti hijo, el ESTADO DEL SENSOR de cada vínculo
    (`CicloSupervisor.estado`, un `EstadoDelMuestreador`) se reintroduce por
    clave (rti, pid, arranque) para no perder las transiciones de degradación
    ya observadas. `estados()` es la contra: la instantánea que la renovación
    toma ANTES de soltar la fábrica vieja.
    """

    def __init__(self, transporte_de, contextos: dict[str, tuple],
                 estados_de: dict | None = None, **kw) -> None:
        super().__init__(transporte_de, **kw)
        self._contextos = contextos
        self._estados_de = estados_de or {}

    def estados(self) -> dict:
        """Instantánea clave → EstadoDelMuestreador de los ciclos vivos."""
        return {clave: entrada.ciclo.estado
                for clave, entrada in self._ciclos.items()}

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
                    umbral_rss_bytes=self._umbral,
                    estado=self._estados_de.pop(clave, None)),
                nonce=self._nonce_de())
            self._ciclos[clave] = estado
        estado.pasadas += 1
        clave_base = f"{v.runtime_instance}-{estado.nonce}-{estado.pasadas}"
        return estado.ciclo.ciclo(v.handle, clave_base=clave_base)


class Driver:
    """Una corrida FINITA sobre objetivos dados. Observa; no actúa."""

    def __init__(self, base_url: str, token: str, objetivos: list[tuple[str, int]], *,
                 fichero_sesion: str, vueltas: int | None = None, intervalo_s: float = 15.0,
                 timeout_s: float = 20.0, continuo: bool = False, ttl_s: int = 900,
                 margen_s: float = 120.0,
                 reloj: callable = time.time, espera: callable = time.sleep,
                 enviar: callable | None = None) -> None:
        # El periódico ya valida su intervalo, pero con ValueError TARDÍO y
        # tras el handshake; aquí muere ANTES de hablar con nadie. `nan` pasa
        # toda comparación e `inf` duerme para siempre: finito o no arranca.
        for nombre, valor in (("intervalo_s", intervalo_s), ("timeout_s", timeout_s),
                              ("margen_s", margen_s)):
            if isinstance(valor, bool) or not isinstance(valor, (int, float)) \
                    or not math.isfinite(valor) or valor <= 0:
                raise ValueError(f"{nombre} tiene que ser un número finito y positivo")
        # ttl del refresco: contrato del gateway (SessionRequest ge=30 le=3600).
        if type(ttl_s) is not int or not TTL_MIN <= ttl_s <= TTL_MAX:
            raise ValueError(
                f"ttl_s tiene que ser un entero en {TTL_MIN}..{TTL_MAX}")
        # El margen se compara contra el RESTANTE de la sesión, cuyo techo es
        # el ttl pedido en cada renovación: margen >= ttl haría que cada
        # adopción naciera ya "dentro" del margen — renovación en bucle.
        if margen_s >= ttl_s:
            raise ValueError(
                f"margen_s ({margen_s}) tiene que ser menor que ttl_s ({ttl_s}): "
                "el refresco debe quedar dentro del plazo que cada renovación pide")
        self._base = base_url.rstrip("/")
        self._token = token
        self._objetivos = objetivos            # [(runtime_instance, pid)] del operador
        self._fichero_sesion = fichero_sesion
        # API directa y CLI comparten el mismo valor por defecto. En modo
        # continuo el contador se ignora, pero conservar un entero aquí evita
        # que una instancia programática tenga un estado distinto al de
        # `main(--continuo)`.
        self._vueltas = 4 if vueltas is None else vueltas
        self._intervalo = intervalo_s
        self._timeout = timeout_s
        self._continuo = continuo
        self._ttl_s = ttl_s
        self._margen = margen_s
        self._reloj = reloj
        self._espera = espera
        self._enviar = enviar                  # inyectable: los tests guionizan el socket
        self.observer_rti: str | None = None
        self.deadline: float | None = None
        # Generación del OBSERVIDOR que entra en el contexto. Al arrancar sale
        # del runtime propio (credential_generation); tras una renovación
        # validada, del RECIBO — el hijo no exige activación de organigrama y
        # el estado del propio runtime no es la fuente correcta para su sesión.
        self._generacion_observador: int | None = None
        self._autoridad: tuple | None = None   # (principal, role, lane) del arranque
        self._autoridad_caps: frozenset | None = None  # conjunto exacto de capacidades
        self._transportes: list[T.TransporteSupervisor] = []
        self._estados_conservar: dict = {}     # sensor entre renovaciones
        self._ciclos = None                    # fábrica viva (para estados())
        self._parar = False                    # SIGINT/SIGTERM en continuo
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

    def consulta(self, ruta: str, token: str | None = None) -> tuple[int, dict]:
        """GET autenticado. Inyectable en tests; sin redirects (no se sigue 30x).
        `token` permite preguntar por una credencial que aún NO se adopta — el
        whoami del hijo de la renovación se hace con el token HIJO antes de
        tocar ningún estado del driver."""
        req = urllib.request.Request(f"{self._base}{ruta}")
        req.add_header("Authorization", f"Bearer {token or self._token}")
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

    def refresca(self, ttl_s: int) -> tuple[int, dict]:
        """POST /native/v1/sessions/refresh con el Bearer VIGENTE. Misma postura
        de red que `consulta`: sin redirects (un 30x reenviaría el Bearer),
        lectura acotada, sin propagar texto de red."""
        req = urllib.request.Request(f"{self._base}{RUTA_REFRESCO}",
                                     data=json.dumps({"ttl_s": ttl_s}).encode(),
                                     method="POST")
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.build_opener(T._SinRedirects()).open(
                    req, timeout=self._timeout) as r:
                return r.status, self._cuerpo_de(
                    r.read(T.MAX_CUERPO_RESPUESTA + 1))
        except urllib.error.HTTPError as exc:
            return exc.code, self._cuerpo_de(exc.read(T.MAX_CUERPO_RESPUESTA + 1))
        except (urllib.error.URLError, TimeoutError, OSError):
            raise PrecondicionFallida("sin respuesta del gateway") from None

    def _renueva(self, ciclos: CC.CiclosPorVinculo,
                 registro: RG.RegistroDeSupervision) -> tuple:
        """Renovación VALIDADA al acercarse el plazo. Contrato REAL (medido en
        2626de2 y endurecido por el REQUEST MARK:codex-supervisor-review-
        85cacb2): `refresh_session` ROTA — token nuevo, rti hijo NUEVO, token
        previo revocado en la misma transacción — y el hijo CONSERVA
        principal, role, lane, capacidades y generation. Eso se EXIGE, no se
        supone: el recibo (`_session_wire` del gateway) y el whoami del hijo
        se validan COMPLETOS — campos, tipos, autoridad inicial y plazos
        semánticos — y SÓLO después se adopta. → ("parada", estado, motivo)
        | ("ok", ciclos, periodico).

        Motivos ACOTADOS: estados HTTP y vocabulario propio; nunca cuerpo
        remoto. El token nuevo jamás va al libro ni a stdout.
        """
        try:
            status, recibo = self.refresca(self._ttl_s)
        except PrecondicionFallida:
            return ("parada", "sesion_terminada_sin_refresco",
                    "sin respuesta del gateway al refrescar")
        if status != 200:
            return ("parada", "sesion_terminada_sin_refresco",
                    f"refresh HTTP {status}")
        # ① EL RECIBO COMPLETO, tipado y contra la autoridad DEL ARRANQUE,
        # antes de tocar nada. Si el gateway ya rotó, el token previo está
        # revocado: no hay vuelta atrás y se declara tal cual.
        token = recibo.get("token")
        rti = recibo.get("runtime_instance")
        generacion = recibo.get("generation")
        if not isinstance(token, str) or not token.strip() \
                or not isinstance(rti, str) or not rti.strip() \
                or type(generacion) is not int or isinstance(generacion, bool):
            return ("parada", "identidad_rotada",
                    "recibo de refresco ilegible: la sesión previa ya no existe")
        try:
            deadline_recibo = _parse_expira(recibo.get("expires_at"))
        except PrecondicionFallida:
            return ("parada", "identidad_rotada", "expires_at del recibo ilegible")
        if deadline_recibo <= self._reloj():
            return ("parada", "identidad_rotada",
                    "el recibo trae un plazo ya vencido: no hay refresco vivo")
        if tuple(recibo.get(k) for k in ("principal", "role", "lane")) \
                != self._autoridad \
                or frozenset(recibo.get("capabilities") or ()) != self._autoridad_caps \
                or generacion != self._generacion_observador:
            # La generación CONSERVADA es parte de «misma autoridad»: un recibo
            # con otra generación no es renovación de la misma persona.
            return ("parada", "autoridad_incompatible",
                    "el recibo de renovación no reproduce la autoridad del "
                    "arranque (principal/role/lane, capacidades o generación)")
        # ② WHOAMI CON EL TOKEN HIJO (sin adoptarlo aún): el recibo declara y
        # el servidor confirma quién ES el token, con generation EXPLÍCITA —
        # sin fallback al recibo, que es lo que se está verificando.
        try:
            status, hijo = self.consulta(RUTA_WHOAMI, token=token)
        except PrecondicionFallida:
            return ("parada", "identidad_rotada",
                    "whoami del hijo sin respuesta; el token previo ya está revocado")
        if status != 200:
            return ("parada", "identidad_rotada", f"whoami del hijo HTTP {status}")
        if hijo.get("runtime_instance") != rti:
            return ("parada", "identidad_rotada",
                    "whoami del hijo no cuadra con el recibo")
        gen_hijo = hijo.get("generation")
        if type(gen_hijo) is not int or isinstance(gen_hijo, bool) \
                or gen_hijo != generacion:
            return ("parada", "identidad_rotada",
                    "el whoami del hijo no confirma la generación del recibo")
        if tuple(hijo.get(k) for k in ("principal", "role", "lane")) \
                != self._autoridad \
                or frozenset(hijo.get("capabilities") or ()) != self._autoridad_caps:
            return ("parada", "autoridad_incompatible",
                    "la sesión renovada sirve otra autoridad (principal/role/lane "
                    "o capacidades)")
        # ③ LOS PLAZOS, semánticos: parseados, FUTUROS e iguales recibo↔hijo.
        try:
            deadline_hijo = _parse_expira(hijo["expires_at"])
        except PrecondicionFallida:
            return ("parada", "identidad_rotada", "expires_at del hijo ilegible")
        if deadline_hijo <= self._reloj():
            return ("parada", "identidad_rotada", "el plazo confirmado ya venció")
        if deadline_hijo != deadline_recibo:
            return ("parada", "identidad_rotada",
                    "el plazo del whoami del hijo no cuadra con el recibo")
        # ── ADOPTAR: recibo y servidor dijeron lo mismo, con plazo futuro ──
        self._token = token
        self.observer_rti = rti
        self._generacion_observador = generacion
        self.deadline = deadline_hijo
        # Contextos NUEVOS para el nuevo observador (los objetivos no cambian:
        # mismos workloads activados, mismos pids registrados — jamás se
        # re-adopta un pid nuevo).
        contextos: dict[str, tuple] = {}
        try:
            for v in registro.vinculos():
                _workload, contexto = self._objetivo_servidor(v.runtime_instance)
                contextos[v.runtime_instance] = contexto
        except PrecondicionFallida as exc:
            return ("parada", "contextos_irreconstruibles", _acota(exc, 120))
        # El estado del sensor viaja a la fábrica nueva; los nonces no (una
        # secuencia jamás se reutiliza con identidad distinta).
        self._estados_conservar = ciclos.estados()
        self._transportes = []          # los transportes viejos llevan token muerto
        ciclos_nuevos, periodico_nuevo = self._monta(registro, contextos)
        return ("ok", ciclos_nuevos, periodico_nuevo)

    def _instala_senales(self) -> dict:
        """SIGINT/SIGTERM → bandera, sólo en continuo y en el hilo principal.
        La vuelta EN CURSO termina y el cierre es ordenado: sin matar envíos a
        medias y con el libro fuera de `en_marcha`. Devuelve los handlers
        previos: el driver los RESTAURA al salir (es un invitado, no el dueño
        del proceso)."""
        import signal
        import threading
        if threading.current_thread() is not threading.main_thread():
            return {}
        def _apunta(_signum, _frame):
            self._parar = True
        previos = {}
        for senal in (signal.SIGINT, signal.SIGTERM):
            previos[senal] = signal.getsignal(senal)
            signal.signal(senal, _apunta)
        return previos

    def _restaura_senales(self, previos: dict) -> None:
        import signal
        for senal, handler in previos.items():
            try:
                signal.signal(senal, handler)
            except (TypeError, ValueError, OSError):
                pass  # el handler previo ya no es reinstalable: no empeora la salida

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
        # La autoridad que la renovación tendrá que reproducir: principal,
        # role y lane CONSULTADOS (una renovación que sirva otros es otra
        # persona aunque traiga un recibo válido).
        self._autoridad = tuple(cuerpo.get(k) for k in ("principal", "role", "lane"))
        self._autoridad_caps = frozenset(cuerpo.get("capabilities") or ())
        status, propio = self.consulta(RUTA_RUNTIMES + urllib.parse.quote(rti, safe=""))
        if status != 200:
            raise PrecondicionFallida(
                f"el runtime del PROPIO observador no consta ({status}): falta la "
                "revisión de organización activada que lo declara")
        generacion = propio.get("credential_generation")
        if type(generacion) is not int or isinstance(generacion, bool):
            raise PrecondicionFallida(
                "el propio runtime no expone credential_generation")
        self._generacion_observador = generacion
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
        contexto = (self.observer_rti, self._generacion_observador, rti, generacion)
        return workload, contexto

    def _monta(self, registro: RG.RegistroDeSupervision,
               contextos: dict[str, tuple]) -> tuple:
        """Transportes + fábrica + periódico para los contextos dados. Se usa
        en el arranque y tras CADA renovación validada: identidad nueva ⇒
        transportes nuevos (token nuevo), secuencias nuevas (nonce nuevo),
        sensor conservado (`_estados_conservar` se consume aquí)."""
        def transporte_de(v: RG.Vinculo) -> T.TransporteSupervisor:
            t = T.TransporteSupervisor(self._base, self._token,
                                       contexto=contextos[v.runtime_instance],
                                       timeout=self._timeout, enviar=self._enviar)
            self._transportes.append(t)
            return t

        ciclos = CiclosPorVinculoConContexto(transporte_de, contextos,
                                             estados_de=self._estados_conservar)
        self._estados_conservar = {}
        periodico = SP.SupervisorPeriodico(registro, ciclos, intervalo_s=self._intervalo)
        return ciclos, periodico

    def corre(self) -> int:
        """Monta registro+transporte y corre: `vueltas` pasadas en finito, sin
        tope en continuo. Devuelve el exit code."""
        self.autentica()
        registro = RG.RegistroDeSupervision()
        contextos: dict[str, tuple] = {}
        for rti, pid in self._objetivos:
            workload, contexto = self._objetivo_servidor(rti)
            # `registra` adopta (pid, arranque) AHORA y puede fallar ruidosamente:
            # un vínculo a medias sería supervisión aparente.
            registro.registra(workload_id=workload, runtime_instance=rti, pid=pid)
            contextos[rti] = contexto
        ciclos, periodico = self._monta(registro, contextos)
        self._ciclos = ciclos

        previos = self._instala_senales() if self._continuo else {}
        try:
            return self._bucle(registro, ciclos, periodico)
        finally:
            if previos:
                self._restaura_senales(previos)

    def _bucle(self, registro: RG.RegistroDeSupervision,
               ciclos: CC.CiclosPorVinculo, periodico: SP.SupervisorPeriodico) -> int:
        """El bucle de vueltas. En continuo, la RENOVACIÓN VALIDADA reemplaza
        `ciclos` y `periodico` por fábrica nueva (misma autoridad confirmada,
        secuencias nuevas, sensor conservado) y el bucle sigue con ellos."""
        self._abre_sesion(registro)
        completadas = 0
        tope = math.inf if self._continuo else self._vueltas
        while completadas < tope:
            if self._parar:
                return self._para_por_senal(ciclos, registro, completadas)
            if self._continuo:
                if self._reloj() >= self.deadline:
                    # El margen no bastó — típicamente pendientes que no
                    # resolvieron a tiempo. Parada visible con lo declarado.
                    motivo = ("la sesión expiró con observaciones sin resolver: "
                              "se resuelven bajo la sesión vigente o se declaran, "
                              "nunca se rotan en el aire"
                              if ciclos.pendientes() else
                              "la sesión expiró sin renovación")
                    return self._para_sesion(ciclos, completadas,
                                             "sesion_terminada_sin_refresco", motivo)
                if self._reloj() + self._margen >= self.deadline:
                    if ciclos.pendientes():
                        # Regla de transición: SÓLO se renueva sin peticiones
                        # en vuelo. La vuelta reanuda lo pendiente con los
                        # mismos bytes y clave bajo la sesión VIGENTE; si no
                        # se resuelve antes de expirar, la parada de arriba
                        # declara. Sin revocación anticipada ni olvido.
                        pass
                    else:
                        observador_previo = self.observer_rti
                        veredicto = self._renueva(ciclos, registro)
                        if veredicto[0] == "parada":
                            return self._para_sesion(ciclos, completadas,
                                                     veredicto[1], veredicto[2])
                        ciclos, periodico = veredicto[1], veredicto[2]
                        self._ciclos = ciclos
                        print(f"REFRESCO: renovación validada y adoptada "
                              f"({observador_previo} → {self.observer_rti}, "
                              "misma autoridad y generación): contextos y "
                              "secuencias nuevos "
                              "(la secuencia no se reutiliza), sensor "
                              f"conservado · expira_en epoch {int(self.deadline)}",
                              flush=True)
            elif self._reloj() >= self.deadline:
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
            if completadas < tope and not self._parar:
                # En continuo la espera se parte en rodajas: la señal se nota
                # sin esperar el intervalo entero (PEP 475 reanudaría el sleep).
                fin = self._reloj() + max(0.0, self._intervalo - (self._reloj() - t0))
                if self._continuo:
                    # La espera cabe DENTRO del margen: el refresco es puntual,
                    # no una vuelta tarde. Y ya dentro del margen (p. ej. con
                    # pendientes que bloquean la renovación) el tope pasa a ser
                    # la EXPIRACIÓN: se duerme hasta ella en vez de girar en
                    # vacío entre vueltas.
                    tope_espera = self.deadline - self._margen
                    if self._reloj() >= tope_espera:
                        tope_espera = self.deadline
                    fin = min(fin, tope_espera)
                while not self._parar:
                    resto = fin - self._reloj()
                    if resto <= 0:
                        break
                    self._espera(min(resto, 0.5))

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
        else:
            codigo = EX_OK
        return codigo

    def _para_por_senal(self, ciclos: CC.CiclosPorVinculo,
                        registro: RG.RegistroDeSupervision,
                        completadas: int) -> int:
        """Cierre ordenado ante señal: la vuelta en curso YA terminó. El libro
        primero, el rc después (misma regla que el cierre normal)."""
        pendientes = ciclos.pendientes()
        incidencias = bool(pendientes or self.fallos_total or self.sensor_total)
        # El libro declara lo que quedó en el aire TAMBIÉN al cierre por señal
        # (misma honestidad que `_para_sesion`): la salida se pierde, el libro no.
        self._sesion["pendientes_al_cierre"] = pendientes
        self._cierra_sesion(completadas, "parado_por_senal")
        print(f"PARADA: señal recibida; cierre ordenado tras {completadas} "
              f"vueltas · vínculos vivos al cierre: {len(registro)} · retirados "
              f"en la corrida: {self.retirados_total} · fallos: "
              f"{self.fallos_total} · observaciones sin resolver: "
              f"{len(pendientes)}"
              + (f" {', '.join(pendientes)}" if pendientes else ""), flush=True)
        return EX_INCIDENCIAS if incidencias else EX_OK

    def _para_sesion(self, ciclos: CC.CiclosPorVinculo, completadas: int,
                     estado: str, motivo: str) -> int:
        """Parada VISIBLE de la sesión continua: renovación no validable,
        autoridad incompatible, contextos irreconstruibles o expiración con
        lo pendiente sin resolver. Lo pendiente queda declarado en salida Y
        libro — nunca descartado en silencio."""
        pendientes = ciclos.pendientes()
        self._sesion["pendientes_al_cierre"] = pendientes
        mensaje = (f"PARADA: {motivo} tras {completadas} vueltas; la sesión "
                   "termina y lo pendiente se declara, no se descarta. "
                   f"Observaciones sin resolver: {len(pendientes)}"
                   + (f" {', '.join(pendientes)}" if pendientes else ""))
        self._cierra_sesion(completadas, estado)
        print(mensaje, flush=True)
        return EX_SESION_EXPIRADA

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
                        "continuo": self._continuo,
                        "vueltas_pedidas": None if self._continuo else self._vueltas,
                        "vueltas_completadas": 0,
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
    """Contrato del fichero privado de token: REGULAR, 600 EXACTO, TUYO, no
    vacío, con techo de lectura EN BYTES.

    «No 600 exacto» incluye 0o400 (el bit de dueño no es el contrato) y 0o640
    (legible por el grupo). Un token legible por otros en una máquina
    multi-carril es la fuga más barata de cometer y la más cara de rastrear:
    se rechaza ANTES de usarlo. El chequeo de uid NO duplica el 0600: para un
    proceso no-root, un 0600 ajeno ya es ilegible; el uid cierra el único caso
    donde eso deja de valer — correr como root, donde el 0600 ajeno SÍ se lee.
    La lectura es binaria y ACOTADA (`MAX_BYTES_TOKEN + 1` BYTES, decodificada
    UTF-8 después): un techo aplicado en modo texto contaría CARACTERES y un
    token multibyte podría pasarse de bytes sin saltar la alarma (corrección
    del REQUEST MARK:codex-supervisor-renovacion-real). Toda incidencia es
    PrecondicionFallida: un traceback aquí puede llevar material de
    credenciales a la consola.
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
        if info.st_uid != os.geteuid():
            raise PrecondicionFallida(
                f"el fichero de token {ruta} pertenece a otro uid: no se "
                "adoptan credenciales ajenas")
        with os.fdopen(fd, "rb") as f:
            fd = None  # el contexto del fichero se encarga de cerrarlo
            crudo = f.read(MAX_BYTES_TOKEN + 1)
    except OSError:
        raise PrecondicionFallida(f"no puedo leer el token en {ruta}") from None
    finally:
        if fd is not None:
            os.close(fd)
    if len(crudo) > MAX_BYTES_TOKEN:
        raise PrecondicionFallida(
            f"el token en {ruta} excede el techo de lectura "
            f"({MAX_BYTES_TOKEN} bytes)")
    try:
        token = crudo.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise PrecondicionFallida(
            f"el token en {ruta} no es UTF-8 legible") from None
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
    p.add_argument("--vueltas", type=int, default=None)
    p.add_argument("--intervalo-s", type=float, default=15.0)
    p.add_argument("--timeout-s", type=float, default=20.0)
    p.add_argument("--continuo", action="store_true",
                   help="vueltas sin tope: renueva la sesión al acercarse el "
                        "plazo (rotación validada con whoami del hijo y "
                        "adoptada si es la misma autoridad) y cierra ordenado "
                        "ante SIGINT/SIGTERM; parada VISIBLE si la renovación "
                        "no se puede validar")
    p.add_argument("--ttl-s", type=int, default=900,
                   help="ttl_s pedido en el refresco (contrato: 30..3600)")
    p.add_argument("--refresco-margen-s", type=float, default=120.0,
                   help="antelación con la que se intenta el refresco")
    p.add_argument("--fichero-sesion", required=True)
    args = p.parse_args(argv)

    objetivos = []
    for crudo in args.objetivo:
        rti, _, pid = crudo.rpartition(":")
        if not rti or not pid.isdigit():
            print(f"objetivo ilegible {crudo!r}: formato RTI:PID", file=sys.stderr)
            return EX_PRECONDICION
        objetivos.append((rti, int(pid)))

    if args.continuo and args.vueltas is not None:
        print("--continuo y --vueltas son modos excluyentes", file=sys.stderr)
        return EX_PRECONDICION
    vueltas = 4 if args.vueltas is None else args.vueltas
    if vueltas < 1:
        print("hay que pedir al menos una vuelta", file=sys.stderr)
        return EX_PRECONDICION
    if args.ttl_s < TTL_MIN or args.ttl_s > TTL_MAX:
        print(f"--ttl-s tiene que estar en {TTL_MIN}..{TTL_MAX} "
              "(contrato del refresh del gateway)", file=sys.stderr)
        return EX_PRECONDICION

    try:
        token = lee_token(args.token_file)
        driver = Driver(args.base_url, token, objetivos,
                        fichero_sesion=args.fichero_sesion, vueltas=vueltas,
                        intervalo_s=args.intervalo_s, timeout_s=args.timeout_s,
                        continuo=args.continuo, ttl_s=args.ttl_s,
                        margen_s=args.refresco_margen_s)
        return driver.corre()
    except (PrecondicionFallida, ValueError) as exc:
        # ValueError: parámetros no finitos. Precondición, no traceback.
        print(f"PRECONDICIÓN: {exc}", file=sys.stderr)
        return EX_PRECONDICION


if __name__ == "__main__":
    sys.exit(main())
