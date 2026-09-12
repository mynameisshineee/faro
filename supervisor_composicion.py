"""Un `CicloSupervisor` POR VÍNCULO, creado una vez y conservado.

Es la pieza que faltaba entre el registro y la vuelta periódica, y su razón de
existir es una sola: **el ciclo no puede nacer de cero en cada pasada**. Si cada
vuelta construyera un `CicloSupervisor` nuevo, `_pendiente` y la secuencia
empezarían vacíos cada vez y la reanudación —lo único que impide inventar un
hecho encima de otro sin resolver— dejaría de existir sin que nada fallara.

🔑 La clave del ciclo es la IDENTIDAD del vínculo, no su nombre:
`(runtime_instance, pid, arranque)`. Así, si un objetivo se retira y alguien
registra OTRO proceso bajo el mismo `runtime_instance`, no hereda ni la
secuencia ni la observación pendiente del muerto — le toca un ciclo nuevo por
construcción, sin que nadie tenga que acordarse de limpiar.

Lo que NO decide este módulo: de dónde sale el `contexto` autenticado de cada
objetivo. Se pide al llamante (`transporte_de`), porque es contrato de
credenciales y no me lo invento.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any, Callable

from supervisor_adapter import SecuenciaSupervisor
from supervisor_registro import Vinculo
from supervisor_runner import CicloSupervisor
from supervisor_transport import TransporteSupervisor


@dataclass
class _Ciclo:
    """El ciclo de un vínculo con lo que lo identifica mientras vive."""
    ciclo: CicloSupervisor
    nonce: str
    pasadas: int = 0


class CiclosPorVinculo:
    """Fábrica con memoria: es el `ciclo_de` que consume `SupervisorPeriodico`.

    🔻 CORRIGE un defecto REAL de mi primera versión (hallazgo de
    `@codex-llminbox`, `MARK:astra-composicion-manual-review1837`). La clave era
    `f"{runtime_instance}-{pasada}"` y el contador **renace en 0** en dos casos
    normales: si se reconstruye la fábrica (reinicio) y si otro pid/arranque se
    registra bajo el MISMO `runtime_instance`.

    Por qué eso es grave y no cosmético: la PK de idempotencia del kernel es
    `(principal_id, lane, verb, key)` — **el target NO entra** (`:2240`,
    consulta `:6355`). Así que la primera observación del proceso NUEVO reusaba
    la clave de la primera del MUERTO contra el mismo observador y carril:
      · con cuerpo distinto -> `IdempotencyConflict`, el adaptador se atasca;
      · con cuerpo idéntico -> **replay silencioso** de una observación vieja,
        que vuelve como aceptada sin refrescar liveness. Un supervisor que
        informa de un proceso muerto como si acabara de mirarlo.

    ⇒ Cada ciclo nace con un `nonce` propio y ESTABLE mientras vive. Los
    reenvíos no lo tocan (el runner reusa la clave exacta que quedó en el aire),
    y la secuencia se resincroniza con la autoridad por el `409`, como ya hacía.
    """

    def __init__(self, transporte_de: Callable[[Vinculo], TransporteSupervisor], *,
                 umbral_rss_bytes: int | None = None,
                 en_frio: bool = True,
                 nonce_de: Callable[[], str] = lambda: secrets.token_hex(8)) -> None:
        self._transporte_de = transporte_de
        self._umbral = umbral_rss_bytes
        self._en_frio = en_frio
        # `nonce_de` inyectable SÓLO para que un test pueda fijarlo; el defecto
        # es aleatorio a propósito: derivarlo de la identidad haría que una
        # fábrica reconstruida sobre el MISMO proceso repitiera las claves, que
        # es justo uno de los dos casos que esto arregla.
        self._nonce_de = nonce_de
        # UN solo mapa: tres dicts paralelos con la misma clave son la forma que
        # hace que alguien (yo, hace dos minutos) añada el tercero y se olvide
        # de soltarlo en `retira`/`cierra`.
        self._ciclos: dict[tuple[str, int, str], _Ciclo] = {}

    @staticmethod
    def _clave(v: Vinculo) -> tuple[str, int, str]:
        return (v.runtime_instance, v.handle.pid, v.handle.arranque)

    def __call__(self, v: Vinculo) -> Any:
        clave = self._clave(v)
        estado = self._ciclos.get(clave)
        if estado is None:
            # Una secuencia POR OBJETIVO: `supervisor_seq` es monótona dentro de
            # su target, no global. Y un nonce POR CICLO: dos ciclos del mismo
            # observador nunca comparten espacio de claves.
            estado = _Ciclo(
                ciclo=CicloSupervisor(
                    self._transporte_de(v), SecuenciaSupervisor(en_frio=self._en_frio),
                    workload_id=v.workload_id, runtime_instance=v.runtime_instance,
                    umbral_rss_bytes=self._umbral),
                nonce=self._nonce_de())
            self._ciclos[clave] = estado
        estado.pasadas += 1
        # La clave identifica el INTENTO nuevo y lleva el nonce del ciclo. En
        # una reanudación el runner NO la usa: reenvía la que quedó en el aire.
        clave_base = f"{v.runtime_instance}-{estado.nonce}-{estado.pasadas}"
        return estado.ciclo.ciclo(v.handle, clave_base=clave_base)

    def ciclo_de(self, v: Vinculo) -> CicloSupervisor | None:
        """El ciclo vivo de ese vínculo, si ya se creó. Para inspección."""
        estado = self._ciclos.get(self._clave(v))
        return estado.ciclo if estado is not None else None

    def nonce_de(self, v: Vinculo) -> str | None:
        """El nonce de ese ciclo. Existe para que un test pueda exigir que
        CAMBIE entre ciclos y NO cambie entre pasadas del mismo."""
        estado = self._ciclos.get(self._clave(v))
        return estado.nonce if estado is not None else None

    def pendientes(self) -> tuple[str, ...]:
        """Objetivos con una observación enviada cuya aceptación no consta."""
        return tuple(sorted(rti for (rti, _p, _a), e in self._ciclos.items()
                            if e.ciclo.pendiente))

    def retira(self, runtime_instance: str) -> int:
        """Suelta los ciclos de ese objetivo. Devuelve cuántos soltó.

        Se llama con lo que la vuelta declaró en `retirados`: el registro ya no
        lo tiene, y conservar su ciclo sería guardar la secuencia de un muerto.
        """
        claves = [k for k in self._ciclos if k[0] == runtime_instance]
        for k in claves:
            self._ciclos.pop(k, None)
        return len(claves)

    def cierra(self) -> None:
        self._ciclos.clear()

    def __len__(self) -> int:
        return len(self._ciclos)
