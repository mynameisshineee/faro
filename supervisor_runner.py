"""Une muestreo -> envío -> aceptación. Es donde `registra_aceptada` se gana el nombre.

🔻 Cierra dos defectos MÍOS, ambos de la misma familia: la prosa decía una cosa
y el código hacía otra.

  1. `registra_aceptada` existía y **nadie la llamaba** desde el camino real —
     igual que `exige_contexto`. Aquí se invoca justo después de que
     `observar()` retorne, cosa que sólo pasa tras validar un `202` contra
     `RuntimeObservation`.
  2. El docstring afirmaba reanudar lo pendiente antes de muestrear, y `ciclo()`
     **siempre muestreaba** y construía una observación NUEVA. Si el RSS o la
     CPU cambiaban tras dos pérdidas, el payload nuevo chocaba con el pendiente
     (`PeticionPendiente`) y el ciclo quedaba **atascado sin salida**: cada
     vuelta volvía a medir métricas distintas. Reproducido antes de corregirlo.

⇒ Regla de este runner: **una observación sin resolver se reanuda ANTES de mirar
el proceso**, se registra UNA vez cuando se acepta, y la muestra siguiente es de
otro ciclo. No se mezcla el hecho que quedó en el aire con el que viene.
"""
from __future__ import annotations

from typing import Any

import process_sampler as P
from supervisor_adapter import Observacion, SecuenciaSupervisor
from supervisor_transport import TransporteSupervisor


class CicloSupervisor:
    """Un ciclo de supervisión sobre UN objetivo."""

    def __init__(self, transporte: TransporteSupervisor, seq: SecuenciaSupervisor,
                 *, workload_id: str, runtime_instance: str,
                 umbral_rss_bytes: int | None = None,
                 estado: P.EstadoDelMuestreador | None = None) -> None:
        self._t = transporte
        self._seq = seq
        self._workload_id = workload_id
        self._runtime_instance = runtime_instance
        self._umbral = umbral_rss_bytes
        self._estado = estado if estado is not None else P.EstadoDelMuestreador()
        # Observación EXACTA que se envió y cuya aceptación no consta, con su
        # clave. En memoria: no es autoridad durable, es no perder de vista lo
        # que quedó sin resolver.
        self._pendiente: tuple[Observacion, str] | None = None

    @property
    def estado(self) -> P.EstadoDelMuestreador:
        return self._estado

    @property
    def pendiente(self) -> bool:
        return self._pendiente is not None

    def ciclo(self, handle: P.Handle, *, clave_base: str,
              exit_code: int | None = None) -> dict[str, Any]:
        """Reanuda lo pendiente si lo hay; si no, mide y envía.

        No captura excepciones para "seguir": una pérdida, un conflicto o una
        identidad cambiada suben. Lo único que hace es RECORDAR lo que quedó sin
        resolver, para que el ciclo siguiente lo reanude en vez de inventar un
        hecho nuevo encima.
        """
        if self._pendiente is not None:
            obs, clave = self._pendiente
            # NO se muestrea: el proceso puede haber cambiado, y mezclar esa
            # medida nueva con el envío viejo es justo lo que atascaba el ciclo.
            return self._enviar_y_registrar(obs, clave)

        muestra = P.muestrea(handle)
        if not muestra.viva and exit_code is not None:
            muestra = P.Muestra(viva=False, exit_code=exit_code)

        obs = P.observacion_de(
            muestra, workload_id=self._workload_id,
            runtime_instance=self._runtime_instance,
            idempotency_key=clave_base, seq=1,   # la seq real la pone el transporte
            umbral_rss_bytes=self._umbral, estado=self._estado)
        return self._enviar_y_registrar(obs, clave_base)

    def _enviar_y_registrar(self, obs: Observacion, clave: str) -> dict[str, Any]:
        try:
            respuesta = self._t.observar(obs, self._seq, clave_base=clave)
        except Exception:
            # El transporte es quien sabe si la petición sigue en el aire: un
            # rechazo definitivo la cierra, una pérdida o un 202 inválido no.
            # Se consulta en vez de deducirlo del tipo de excepción.
            self._pendiente = (obs, clave) if self._t.pendiente else None
            raise

        # ⬇️ AQUÍ, y en ningún otro sitio: `observar` sólo retorna tras validar
        # el 202. Se registra la observación ORIGINAL, una sola vez.
        self._estado.registra_aceptada(obs, umbral_rss_bytes=self._umbral)
        self._pendiente = None
        return respuesta
