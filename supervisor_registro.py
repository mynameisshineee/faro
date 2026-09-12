"""Registro EXPLÍCITO de procesos supervisados. La única entrada es un pid dado.

🔑 La propiedad que este módulo existe para sostener: **nunca se descubre a quién
supervisar**. No hay búsqueda por nombre, ni por línea de comandos, ni por
usuario, ni barrido de `/proc`. Alguien REGISTRA un pid concreto y sólo eso se
observa. Un agente sano que nadie registró es invisible para el supervisor, que
es la diferencia entre observar y entrometerse.

Por qué importa aquí y no es celo: inferir el pid por nombre es exactamente el
modo de fallo que mata al vecino. `pgrep python` en esta flota devuelve las
sesiones VIVAS de otros carriles. Un supervisor que resuelve objetivos por
nombre acaba muestreando —y algún día actuando sobre— procesos que no son suyos.
Por eso el registro se hace con un entero, y el vínculo se ata a la IDENTIDAD
(pid + arranque), no al pid a secas: un pid reciclado NO es el mismo objetivo.

El registro es también donde el fallo se declara pronto: si no se puede adoptar
—el proceso no existe, o el sensor no está disponible— **no se registra nada**.
Un vínculo a medias sería un objetivo que parece supervisado y no lo está.
"""
from __future__ import annotations

from dataclasses import dataclass

import process_sampler as P


class YaRegistrado(Exception):
    """Ese objetivo (o ese proceso) ya tiene vínculo: registrar dos veces el
    mismo proceso produciría dos observadores del mismo hecho."""


class NoRegistrado(Exception):
    """Se pidió algo sobre un `runtime_instance` que nadie registró."""


@dataclass(frozen=True)
class Vinculo:
    """Un objetivo supervisado: a QUIÉN se observa y con qué identidad."""
    workload_id: str
    runtime_instance: str
    handle: P.Handle

    @property
    def pid(self) -> int:
        return self.handle.pid


class RegistroDeSupervision:
    """Los objetivos vivos del supervisor. Sin descubrimiento, sin barridos."""

    def __init__(self) -> None:
        self._por_objetivo: dict[str, Vinculo] = {}

    def registra(self, *, workload_id: str, runtime_instance: str, pid: int) -> Vinculo:
        """Ata un objetivo a un pid CONCRETO, adoptando su identidad ahora.

        `adopta` puede levantar (`ProcesoDesconocido`, `SensorNoDisponible`) y se
        deja subir a propósito: quien registra tiene que enterarse de que no hay
        vínculo, en vez de recibir un registro vacío que parece supervisión.
        """
        if runtime_instance in self._por_objetivo:
            raise YaRegistrado(f"{runtime_instance} ya está registrado")
        for v in self._por_objetivo.values():
            if v.pid == pid:
                raise YaRegistrado(
                    f"el pid {pid} ya está registrado como {v.runtime_instance}")
        # La identidad se captura AQUÍ: a partir de ahora el vínculo es con ESTE
        # arranque, no con el número de pid.
        vinculo = Vinculo(workload_id=workload_id, runtime_instance=runtime_instance,
                          handle=P.adopta(pid))
        self._por_objetivo[runtime_instance] = vinculo
        return vinculo

    def olvida(self, runtime_instance: str) -> Vinculo:
        """Retira un vínculo. Es la ÚNICA forma de dejar de observar algo, y es
        un acto explícito: nada se des-registra solo por parecer inactivo."""
        try:
            return self._por_objetivo.pop(runtime_instance)
        except KeyError:
            raise NoRegistrado(runtime_instance) from None

    def vinculos(self) -> tuple[Vinculo, ...]:
        """Orden de registro, estable: una vuelta periódica no debe depender de
        cómo itere un dict entre versiones."""
        return tuple(self._por_objetivo.values())

    def __len__(self) -> int:
        return len(self._por_objetivo)

    def __contains__(self, runtime_instance: object) -> bool:
        return runtime_instance in self._por_objetivo
