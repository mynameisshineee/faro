"""Vueltas periódicas sobre los vínculos REGISTRADOS. Observa; no actúa.

Tres decisiones, y las tres son restricciones, no funciones:

1. **Sólo se recorre el registro.** No hay descubrimiento. Si un agente sano no
   está registrado, este bucle no lo ve — y esa es la propiedad, no una carencia.
2. **Una identidad cambiada RETIRA el vínculo; no se re-adopta por pid.**
   Re-adoptar el mismo número sería inferir que el proceso nuevo «es» el
   objetivo. Es otro proceso: que lo registre quien sepa que lo es.
3. **Sensor indisponible NO es ausencia.** Se conserva el vínculo y se declara
   el fallo del instrumento. Confundirlos es cómo un supervisor declara muerto
   a un proceso vivo al que no supo mirar.

Y una que no está: **no hay recuperación automática** (ADR-002). Este módulo no
tiene ningún camino que mate, señale ni reinicie nada; lo único que hace con un
objetivo es LEERLO. El día que alguien quiera actuar, tendrá que añadir el verbo
aquí y pasar por revisión, en vez de encontrárselo ya puesto.

El reloj y la espera se INYECTAN: un test que duerme mide la paciencia del CI,
no la periodicidad. Y no hay bucle infinito — las vueltas se piden contadas.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import process_sampler as P
from supervisor_registro import RegistroDeSupervision, Vinculo

MAX_MOTIVO = 200


def _motivo_propio(exc: Exception) -> str:
    """Motivo de una excepción MÍA (el muestreador): tipo + texto ACOTADO.

    El vocabulario de `process_sampler` es mío y corto, pero se recorta igual y
    se colapsan los blancos: un motivo que crece sin tope acaba en un registro.
    """
    detalle = " ".join(str(exc).split())[:MAX_MOTIVO]
    return f"{type(exc).__name__}: {detalle}" if detalle else type(exc).__name__


@dataclass
class ResultadoDeVuelta:
    """Qué pasó en una vuelta. Los fallos se CUENTAN, no se tragan.

    ⚠️ `fallos` guarda SÓLO el nombre del tipo, nunca el texto: ahí caen
    excepciones de terceros (transporte, callback ajeno) y ese texto puede
    arrastrar URL o credencial. Es la misma disciplina que `_enviar_http` ya
    aplica al negarse a propagar el texto de red — y que aquí me faltaba.
    `sensor_caido`/`retirados` sí llevan detalle porque son vocabulario del
    muestreador, mío y acotado.
    """
    observados: list[str] = field(default_factory=list)
    sensor_caido: list[tuple[str, str]] = field(default_factory=list)
    retirados: list[tuple[str, str]] = field(default_factory=list)
    fallos: list[tuple[str, str]] = field(default_factory=list)

    @property
    def hubo_incidencias(self) -> bool:
        return bool(self.sensor_caido or self.retirados or self.fallos)


class SupervisorPeriodico:
    """Recorre los vínculos registrados cada `intervalo_s`."""

    def __init__(self, registro: RegistroDeSupervision,
                 ciclo_de: Callable[[Vinculo], Any], *,
                 intervalo_s: float = 15.0,
                 reloj: Callable[[], float] = time.monotonic,
                 espera: Callable[[float], None] = time.sleep) -> None:
        # `<= 0` NO basta: `nan` lo pasa (toda comparación con nan es falsa) y
        # acaba en `sleep(nan)`; `inf` lo pasa y duerme para siempre; y `True`
        # lo pasa porque en Python un bool ES un int. Es la misma clase que
        # `type(v) is not int` cierra en la cota de RSS del banco — la elogié
        # allí y no la apliqué aquí.
        if isinstance(intervalo_s, bool) or not isinstance(intervalo_s, (int, float)):
            raise ValueError("el intervalo tiene que ser un número, no un bool")
        if not math.isfinite(intervalo_s) or intervalo_s <= 0:
            raise ValueError("el intervalo tiene que ser finito y positivo")
        self._registro = registro
        self._ciclo_de = ciclo_de
        self._intervalo = intervalo_s
        self._reloj = reloj
        self._espera = espera

    def vuelta(self) -> ResultadoDeVuelta:
        """UNA pasada. Un vínculo enfermo no puede impedir mirar a los demás.

        Se itera sobre una copia: `retirar` muta el registro y no se recorre una
        colección que cambia bajo los pies.
        """
        r = ResultadoDeVuelta()
        for v in self._registro.vinculos():
            try:
                self._ciclo_de(v)
            except P.IdentidadDeProcesoCambiada as exc:
                # El pid se reusó: el objetivo YA NO ESTÁ. Se retira el vínculo
                # y NO se vuelve a adoptar ese número por nuestra cuenta.
                self._registro.olvida(v.runtime_instance)
                r.retirados.append((v.runtime_instance, _motivo_propio(exc)))
            except P.ProcesoDesconocido as exc:
                # Ausencia CONFIRMADA del proceso adoptado. También retira: el
                # vínculo apuntaba a algo que ya no existe.
                self._registro.olvida(v.runtime_instance)
                r.retirados.append((v.runtime_instance, _motivo_propio(exc)))
            except P.SensorNoDisponible as exc:
                # NO es ausencia. El vínculo SE CONSERVA.
                r.sensor_caido.append((v.runtime_instance, _motivo_propio(exc)))
            except Exception as exc:
                # Transporte, callback ajeno, lo que sea: se anota SÓLO EL TIPO.
                # El texto es de otro y puede llevar URL o credencial dentro.
                # No se retira nada — esto no dice nada del proceso.
                r.fallos.append((v.runtime_instance, type(exc).__name__))
            else:
                r.observados.append(v.runtime_instance)
        return r

    def corre(self, vueltas: int) -> list[ResultadoDeVuelta]:
        """`vueltas` pasadas, esperando el resto del intervalo entre ellas.

        Se espera lo que FALTA (no el intervalo entero): si una vuelta tardó
        más que el intervalo, no se duerme nada y se dice con un 0.
        """
        if vueltas < 1:
            raise ValueError("hay que pedir al menos una vuelta")
        salidas = []
        for i in range(vueltas):
            t0 = self._reloj()
            salidas.append(self.vuelta())
            if i + 1 < vueltas:
                self._espera(max(0.0, self._intervalo - (self._reloj() - t0)))
        return salidas
