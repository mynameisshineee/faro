"""Muestreo EXTERNO de un proceso real -> observaciones typadas.

Complementa al transporte: éste sabe HABLAR con el gateway, esto sabe MIRAR un
proceso. El transporte solo no es supervisión operativa.

Reglas que lo definen, y ninguna es de estilo:

  · La identidad de un proceso es `(pid, arranque)`, NUNCA el pid solo. Los pid
    se reciclan; un pid vivo con OTRO arranque es OTRO proceso, y reportarlo
    como el mismo es exactamente cómo un supervisor acredita vida ajena.
  · NADA de heurística de texto de terminal: se leen contadores de `ps`, no
    líneas de log. Una prosa no es un estado.
  · Este módulo NO mata, NO reinicia y NO toca ningún proceso: sólo `ps`.
    No hay ruta a kill/señal ni siquiera para el proceso de ensayo — el que lo
    crea es quien lo termina.
  · Las medidas salen typadas y acotadas o no salen: `cpu_millis`, `rss_bytes`.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from supervisor_adapter import (
    MAX_CPU_MILLIS,
    MAX_RSS_BYTES,
    Observacion,
    ObservacionInvalida,
    valida,
)


class ProcesoDesconocido(Exception):
    """No hay proceso con ese pid, o ya no es el mismo."""


class IdentidadDeProcesoCambiada(ProcesoDesconocido):
    """El pid existe pero su arranque no coincide: es OTRO proceso (reuso)."""


class SensorNoDisponible(Exception):
    """No se pudo LEER. Distinto de «el proceso no está».

    Confundirlos es acreditar una parada que nadie observó: un timeout de `ps`
    o una fila ilegible NO son un proceso muerto.
    """


MAXCOMLEN = 16
PROC_PIDTBSDINFO = 3          # sys/proc_info.h:723, leído del SDK instalado


class _ProcBsdInfo(ctypes.Structure):
    """`struct proc_bsdinfo` de `sys/proc_info.h`, copiado del header real.

    El orden importa: `pbi_start_tvsec`/`pbi_start_tvusec` sólo caen donde deben
    si TODO lo anterior está declarado. Por eso se transcribe entero y se
    comprueba el tamaño contra lo que devuelve `proc_pidinfo`.
    """
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32), ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32), ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32), ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32), ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32), ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32), ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * MAXCOMLEN),
        ("pbi_name", ctypes.c_char * (2 * MAXCOMLEN)),
        ("pbi_nfiles", ctypes.c_uint32), ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32), ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32), ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


def _libproc():
    """`libproc` por ctypes de la stdlib: sin dependencia nueva y sin compilar C."""
    if sys.platform != "darwin":
        return None
    try:
        ruta = ctypes.util.find_library("proc")
        lib = ctypes.CDLL(ruta or "libproc.dylib", use_errno=True)
        lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                     ctypes.c_void_p, ctypes.c_int]
        lib.proc_pidinfo.restype = ctypes.c_int
        return lib
    except (OSError, AttributeError):
        return None


_LIBPROC = _libproc()


def _arranque_libproc(pid: int) -> str | None:
    """Arranque de macOS; sólo ESRCH acredita ausencia del proceso."""
    _valida_pid(pid)
    if _LIBPROC is None:
        raise SensorNoDisponible("libproc no está disponible")
    info = _ProcBsdInfo()
    ctypes.set_errno(0)
    escrito = _LIBPROC.proc_pidinfo(pid, PROC_PIDTBSDINFO, 0,
                                    ctypes.byref(info), ctypes.sizeof(info))
    if escrito <= 0:
        codigo = ctypes.get_errno()
        if codigo == errno.ESRCH:
            return None
        raise SensorNoDisponible(
            f"proc_pidinfo no pudo leer pid {pid} (errno={codigo})")
    if escrito != ctypes.sizeof(info):
        # ABI distinta de la que transcribí: NO se interpretan los bytes.
        raise SensorNoDisponible(
            f"proc_pidinfo devolvió {escrito} B y el struct mide "
            f"{ctypes.sizeof(info)} B: ABI no verificada")
    if (info.pbi_pid != pid or info.pbi_start_tvsec <= 0
            or info.pbi_start_tvusec >= 1_000_000):
        raise SensorNoDisponible("proc_pidinfo devolvió una identidad inválida")
    return f"{info.pbi_start_tvsec}.{info.pbi_start_tvusec:06d}"


def _resolucion_de_arranque() -> float:
    """Unidad del sensor configurado; no acredita disponibilidad ni unicidad."""
    if sys.platform.startswith("linux"):
        return 1.0 / os.sysconf("SC_CLK_TCK")
    if sys.platform == "darwin":
        return 1e-6
    return 1.0


def _arranque_proc(pid: int) -> str | None:
    """Campo 22 de procfs. Permisos, montaje y parseo son errores del sensor."""
    _valida_pid(pid)
    if not Path("/proc/self/stat").is_file():
        raise SensorNoDisponible("procfs no está disponible")
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            datos = fh.read(16_384)
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ESRCH}:
            return None
        raise SensorNoDisponible(f"procfs no pudo leer pid {pid}") from exc
    try:
        if len(datos) == 16_384:
            raise ValueError("fila truncada")
        cabecera, resto = datos.rsplit(b")", 1)
        if int(cabecera.split(b" (", 1)[0]) != pid:
            raise ValueError("pid distinto")
        valor = resto.split()[19]
        if not valor.isdigit():
            raise ValueError("arranque inválido")
        return str(int(valor))
    except (IndexError, ValueError) as exc:
        raise SensorNoDisponible(f"procfs devolvió una identidad ilegible para pid {pid}") from exc


PRECISION_ARRANQUE_S = _resolucion_de_arranque()

# Unidad del backend configurado. La disponibilidad se comprueba al adoptar
# y muestrear; esta constante no certifica identidad ni ausencia de reuso de PID.
RESOLUCION_SUBSEGUNDO_CONFIGURADA = PRECISION_ARRANQUE_S < 1.0


def _valida_pid(pid: int) -> None:
    if type(pid) is not int or not 0 < pid <= (1 << 31) - 1:
        raise ValueError("pid inválido")


@dataclass(frozen=True)
class Handle:
    """Identidad estable de un proceso: pid + su instante de arranque."""
    pid: int
    arranque: str          # opaco: SÓLO se compara, nunca se interpreta
    backend: str = "ps"    # "proc" | "libproc" | "ps" — de QUÉ sensor salió

    # 🔻 El `backend` no es decorativo: sin él, `a31f695` guardaba los ticks de
    # /proc y luego comparaba el `lstart` humano de `ps` contra ellos. Dos
    # formatos que NUNCA coinciden ⇒ en Linux TODO proceso vivo acababa en
    # `IdentidadDeProcesoCambiada`. Un identificador opaco sólo se puede
    # comparar contra otro del MISMO sensor.

    # El backend ps conserva resolución de segundos. La adopción de producción
    # usa procfs en Linux y libproc en macOS, sin degradarse a ps por un error.

    def __post_init__(self) -> None:
        _valida_pid(self.pid)
        if not isinstance(self.arranque, str) or not self.arranque.strip():
            raise ValueError("arranque inválido")
        if self.backend not in BACKENDS:
            raise ValueError(f"backend desconocido: {self.backend!r}")


@dataclass(frozen=True)
class Muestra:
    viva: bool
    cpu_millis: int | None = None
    rss_bytes: int | None = None
    exit_code: int | None = None

    def __post_init__(self) -> None:
        if type(self.viva) is not bool:
            raise SensorNoDisponible("estado de proceso inválido")
        for nombre, minimo, maximo in (
                ("cpu_millis", 0, MAX_CPU_MILLIS),
                ("rss_bytes", 0, MAX_RSS_BYTES),
                ("exit_code", -(1 << 31), (1 << 31) - 1)):
            valor = getattr(self, nombre)
            if valor is not None and (type(valor) is not int or not minimo <= valor <= maximo):
                raise SensorNoDisponible(f"medida {nombre} inválida")


def _ps(pid: int, campos: str) -> str | None:
    """Lee contadores del sistema. Sin shell, sin texto de terminal.

    `None` = ausencia CONFIRMADA por `ps`. Si no se pudo leer, LEVANTA
    `SensorNoDisponible`: una avería del sensor no acredita una parada.
    """
    _valida_pid(pid)
    try:
        salida = subprocess.run(
            ["ps", "-p", str(pid), "-o", campos],
            capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired as exc:
        raise SensorNoDisponible(f"`ps` no respondió para pid {pid}") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise SensorNoDisponible("no se pudo ejecutar `ps`") from exc
    # Firma MEDIDA en esta plataforma (Darwin), no supuesta:
    #   ausencia      rc=1 · stdout VACÍO · stderr VACÍO
    #   campo inválido rc=1 · stdout con la lista de keywords · stderr con el ps:
    #   vivo          rc=0 · fila
    # ⇒ el rc por sí solo NO distingue: un error de opciones también da 1.
    if salida.returncode != 0:
        if (salida.returncode == 1 and not salida.stdout.strip()
                and not salida.stderr.strip()):
            return None                 # ausencia CONFIRMADA
        raise SensorNoDisponible(
            f"`ps` falló para pid {pid} (rc={salida.returncode}): no acredita "
            "ausencia")                 # rc≠1, o con diagnóstico: sensor, no parada
    if salida.stderr.strip():
        raise SensorNoDisponible(f"`ps` devolvió un diagnóstico para pid {pid}")
    if not salida.stdout.strip():
        raise SensorNoDisponible(
            f"`ps` devolvió rc=0 sin filas para pid {pid}")
    # Los campos van con `=` (p.ej. `lstart=`), así que `ps` NO imprime
    # cabecera: descartar la primera línea se comía el único dato. Medido al
    # ejecutarlo — compilaba igual de bien roto.
    lineas = [l for l in salida.stdout.splitlines() if l.strip()]
    if len(lineas) != 1:
        raise SensorNoDisponible(f"`ps` devolvió varias filas para pid {pid}")
    return lineas[0].strip()


BACKENDS = ("proc", "libproc", "ps")


def _arranque_por_backend(pid: int, backend: str) -> str | None:
    """Lee el arranque con UN sensor concreto. Nunca cae a otro en silencio.

    Si el sensor que adoptó el handle deja de estar disponible, eso es
    `SensorNoDisponible` — cambiar de formato por detrás produciría exactamente
    la comparación imposible que rompía Linux.
    """
    if backend == "proc":
        return _arranque_proc(pid)
    if backend == "libproc":
        return _arranque_libproc(pid)
    if backend == "ps":
        return _ps(pid, "lstart=")
    raise SensorNoDisponible("backend de proceso desconocido")


def _backend_de_plataforma() -> str:
    if sys.platform.startswith("linux"):
        return "proc"
    if sys.platform == "darwin":
        return "libproc"
    raise SensorNoDisponible("plataforma sin sensor de identidad admitido")


def adopta(pid: int) -> Handle:
    """Toma la identidad del sensor de la plataforma, sin fallback por error."""
    _valida_pid(pid)
    backend = _backend_de_plataforma()
    arranque = _arranque_por_backend(pid, backend)
    if arranque is None:
        raise ProcesoDesconocido(f"no hay proceso con pid {pid}")
    return Handle(pid=pid, arranque=arranque, backend=backend)


def _tiempo_a_millis(tiempo: str) -> int | None:
    """`ps -o time=` -> milisegundos int acotados, o `None` si no se entiende.

    Formatos observados: `0:00.03` (macOS, con FRACCIÓN), `12:34:56`,
    `2-03:04:05` (días con guion). La versión anterior fallaba con la fracción
    —`int("00.03")` revienta— y plegaba los días como si fueran otro grupo de
    60, o sea contaba un día como 60 horas.
    """
    if not isinstance(tiempo, str) or not tiempo.strip() or len(tiempo) > 64:
        return None
    texto = tiempo.strip()
    if not re.fullmatch(r"(?:[0-9]+-)?[0-9]+(?::[0-9]+){0,2}(?:\.[0-9]+)?", texto):
        return None
    dias = 0
    con_dias = "-" in texto
    if con_dias:
        cabeza, _, texto = texto.partition("-")
        try:
            dias = int(cabeza)
        except ValueError:
            return None
        if dias < 0:
            return None
    partes = texto.split(":")
    if not 1 <= len(partes) <= 3:
        return None
    try:
        segundos = Decimal(partes[-1])
        for p in partes[:-1]:
            if "." in p:
                return None               # sólo el último grupo lleva fracción
        minutos = int(partes[-2]) if len(partes) >= 2 else 0
        horas = int(partes[-3]) if len(partes) == 3 else 0
    except ValueError:
        return None
    if (len(partes) > 1 and segundos >= 60
            or len(partes) == 3 and minutos >= 60
            or con_dias and (len(partes) != 3 or horas >= 24)):
        return None
    total = ((dias * 24 + horas) * 60 + minutos) * 60 + segundos
    millis = int(total * 1000)
    if millis > MAX_CPU_MILLIS:
        # 🩸 NO se recorta. `MAX_CPU_MILLIS` son ~16,7 min: cualquier proceso
        # longevo lo supera, y devolver el tope reportaría 16,7 min de CPU para
        # algo que lleva días — un número LEGAL, plausible e INDISTINGUIBLE de
        # una medida real. El campo es opcional: mejor no medir que medir mal.
        return None
    return millis


def muestrea(h: Handle) -> Muestra:
    """Una lectura. NO decide estado: devuelve hechos typados.

    Si el pid existe con OTRO arranque, levanta en vez de devolver una muestra:
    confundir un pid reciclado con el original es acreditar vida de un proceso
    que ya no está.
    """
    # Identidad ANTES de medir, con el MISMO sensor que adoptó el handle.
    antes = _arranque_por_backend(h.pid, h.backend)
    if antes is None:
        return Muestra(viva=False)
    if antes != h.arranque:
        raise IdentidadDeProcesoCambiada(
            f"pid {h.pid} existe pero con otro arranque: es otro proceso")

    fila = _ps(h.pid, "stat=,time=,rss=")
    if fila is None:
        return Muestra(viva=False)
    partes = fila.split()
    if len(partes) != 3 or partes[0][0] not in "RSDTtIWZX":
        # Fila ilegible: el sensor falló, el proceso NO está acreditado muerto.
        raise SensorNoDisponible(f"fila de `ps` no interpretable para pid {h.pid}")
    estado_proceso, tiempo, rss = partes

    # Identidad DESPUÉS: si cambió entre ambas lecturas, las medidas pueden ser
    # de un pid ya reciclado. Se rechaza la muestra en vez de atribuírselas a A.
    despues = _arranque_por_backend(h.pid, h.backend)
    if despues is None or despues != h.arranque:
        raise IdentidadDeProcesoCambiada(
            f"pid {h.pid} cambió de identidad mientras se medía: la muestra no "
            "se atribuye")
    if estado_proceso[0] in {"Z", "X"}:
        return Muestra(viva=False)  # proceso terminado aunque su padre no lo haya recogido
    try:
        crudo_rss = int(rss)
        if crudo_rss < 0:
            raise ValueError("rss negativo")              # medida imposible
        rss_bytes = crudo_rss * 1024                      # ps da KiB
        if rss_bytes > MAX_RSS_BYTES:
            rss_bytes = None                              # mismo criterio que CPU
    except ValueError:
        rss_bytes = None
    return Muestra(viva=True, cpu_millis=_tiempo_a_millis(tiempo),
                   rss_bytes=rss_bytes)


class EstadoDelMuestreador:
    """Degradaciones ACEPTADAS por el Journal, por objetivo.

    🔻 La versión anterior era un `bool` global y no cerraba lo que su docstring
    afirmaba. Escenario que la rompía, tal cual lo describió `@codex-llminbox`:
    degrado A -> el operador pide recovery de A (estado `recovering`) -> baja el
    RSS -> yo emitía `resource_recovered` porque el bool seguía `True` -> el
    kernel lo pasa a `fresh` y **una recuperación manual PENDIENTE queda
    marcada como éxito que nadie ejecutó**. Además el mismo bool servía para el
    objetivo B.

    Tres cambios: la clave es `(workload_id, runtime_instance)`, sólo se
    registra lo que el Journal ACEPTÓ (no lo que se intentó), y se guarda el
    umbral con el que se degradó para no "recuperar" contra otro distinto.

    ⚠️ COTA QUE NO CIERRO YO: entre mi lectura y mi escritura puede intercalarse
    una recuperación manual. Un GET previo no elimina esa carrera — la
    protección atómica es del Journal y está en manos de `@cto`. Aquí se reduce
    la ventana y se declara; no se afirma cerrada.
    """

    def __init__(self) -> None:
        self._degradados: dict[tuple[str, str], int | None] = {}

    @staticmethod
    def _clave(obs: Observacion) -> tuple[str, str]:
        return (obs.workload_id, obs.runtime_instance)

    def registra_aceptada(self, obs: Observacion, *,
                          umbral_rss_bytes: int | None = None) -> None:
        """SÓLO tras un 202 del Journal. Registrar intentos falsearía el ciclo."""
        clave = self._clave(obs)
        if obs.observation_kind == "resource_degraded":
            self._degradados[clave] = umbral_rss_bytes
        elif obs.observation_kind in ("resource_recovered", "exited"):
            self._degradados.pop(clave, None)

    def degradado_por_mi(self, workload_id: str, runtime_instance: str,
                         *, umbral_rss_bytes: int | None) -> bool:
        clave = (workload_id, runtime_instance)
        if clave not in self._degradados:
            return False
        # Recuperar contra un umbral distinto del que degradó no acredita nada.
        return self._degradados[clave] == umbral_rss_bytes


def observacion_de(muestra: Muestra, *, workload_id: str, runtime_instance: str,
                   idempotency_key: str, seq: int,
                   umbral_rss_bytes: int | None = None,
                   estado: "EstadoDelMuestreador | None" = None) -> Observacion:
    """Traduce una muestra al vocabulario cerrado. Sin prosa, sin inventar.

    Una ausencia o terminación confirmada admite `exit_code=None`: el sensor
    normalmente no es el padre del proceso. Nunca se fabrica un código cero
    para declarar una salida limpia sin haberla observado.
    """
    if not muestra.viva:
        # El kernel admite `exit_code` NULL (`coordination.py:1739-1740`), así
        # que una ausencia CONFIRMADA por `ps` sí acredita `exited` sin código.
        # Lo que sigue prohibido es INVENTAR un `0`: eso declararía una salida
        # limpia que nadie miró. `None` dice «se fue»; `0` diría «se fue bien».
        # Y la ausencia sólo llega aquí si `ps` la confirmó: una avería del
        # sensor levanta `SensorNoDisponible` antes (hallazgo 1).
        return valida(Observacion(
            workload_id=workload_id, runtime_instance=runtime_instance,
            idempotency_key=idempotency_key, supervisor_seq=seq,
            observation_kind="exited", reason_code="PROCESS_EXITED",
            exit_code=muestra.exit_code))

    if (umbral_rss_bytes is not None and muestra.rss_bytes is not None
            and muestra.rss_bytes > umbral_rss_bytes):
        return valida(Observacion(
            workload_id=workload_id, runtime_instance=runtime_instance,
            idempotency_key=idempotency_key, supervisor_seq=seq,
            observation_kind="resource_degraded", reason_code="MEMORY_SATURATED",
            cpu_millis=muestra.cpu_millis, rss_bytes=muestra.rss_bytes))

    if (estado is not None and muestra.rss_bytes is not None
            and estado.degradado_por_mi(workload_id, runtime_instance,
                                        umbral_rss_bytes=umbral_rss_bytes)):
        # Con RSS DESCONOCIDA no se emite recuperación: sería declarar que el
        # recurso bajó sin haberlo medido.
        # El recurso bajó y la degradación era MÍA: cierro mi propio ciclo.
        return valida(Observacion(
            workload_id=workload_id, runtime_instance=runtime_instance,
            idempotency_key=idempotency_key, supervisor_seq=seq,
            observation_kind="resource_recovered",
            reason_code="HEARTBEAT_RECOVERED",
            cpu_millis=muestra.cpu_millis, rss_bytes=muestra.rss_bytes))

    return valida(Observacion(
        workload_id=workload_id, runtime_instance=runtime_instance,
        idempotency_key=idempotency_key, supervisor_seq=seq,
        observation_kind="cycle_ack", reason_code="PROCESS_PRESENT",
        cpu_millis=muestra.cpu_millis, rss_bytes=muestra.rss_bytes))
