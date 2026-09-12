"""Transporte: reinicio, replay por respuesta perdida, conflicto concurrente, ajeno.

Doble del servidor, sin red: el `enviar` inyectado devuelve `(status, cuerpo)` o
levanta `RespuestaPerdida`. Lo que se prueba es el PROTOCOLO del cliente —clave,
cuerpo y secuencia—, no el gateway, que tiene sus propias pruebas.

⚠️ Cota declarada: estos dobles NO acreditan el comportamiento del Journal real.
La corrida contra Journal+HTTP la hace `@sdet` en su turno serial.
"""
from __future__ import annotations

import pytest

import supervisor_adapter as A
import supervisor_transport as T


def _obs(seq=1):
    return A.Observacion(
        workload_id="w1", runtime_instance="r-target", idempotency_key="ignorada",
        supervisor_seq=seq, observation_kind="cycle_ack",
        reason_code="PROCESS_PRESENT")


def _ok(seq=1, **cambios):
    base = {"observation_id": "obs-1", "workload_id": "w1",
            "runtime_instance": "r-target", "supervisor_seq": seq,
            "replayed": False}
    base.update(cambios)
    return (202, base)

class Servidor:
    """Doble que registra cada intento: (clave, supervisor_seq del cuerpo)."""

    def __init__(self, guion):
        self.guion = list(guion)
        self.intentos = []

    def __call__(self, url, cuerpo, clave):
        self.intentos.append((clave, cuerpo["supervisor_seq"]))
        accion = self.guion.pop(0)
        if isinstance(accion, Exception):
            raise accion
        return accion


# ── arranque en frío ─────────────────────────────────────────────────────────

def test_primera_propuesta_es_uno_y_pasa_sin_historial():
    srv = Servidor([_ok(seq=1, observation_id="o1")])
    seq = A.SecuenciaSupervisor(en_frio=True)
    t = T.TransporteSupervisor("http://x", "tok", enviar=srv)
    r = t.observar(_obs(), seq, clave_base="k")
    assert r["observation_id"] == "o1"
    assert srv.intentos == [("k-0", 1)], "la primera propuesta tiene que ser seq=1"
    assert seq.provisional is False, "un 202 confirma la posición"


# ── conflicto de secuencia: clave NUEVA porque el cuerpo cambia ──────────────

def test_conflicto_reintenta_una_vez_con_max_mas_uno_y_CLAVE_NUEVA():
    srv = Servidor([
        (409, {"code": T.CODIGO_CONFLICTO_SECUENCIA, "max_supervisor_seq": 41}),
        _ok(seq=42, observation_id="o2"),
    ])
    seq = A.SecuenciaSupervisor(en_frio=True)
    t = T.TransporteSupervisor("http://x", "tok", enviar=srv)
    t.observar(_obs(), seq, clave_base="k")
    claves = [c for c, _ in srv.intentos]
    seqs = [s for _, s in srv.intentos]
    assert seqs == [1, 42], "el retry va con max+1"
    assert claves[0] != claves[1], (
        "el cuerpo cambió (seq distinta): reusar la clave daría conflicto de "
        "bytes en D4 en vez de una observación nueva")


def test_segundo_conflicto_queda_visible_y_no_hay_bucle():
    srv = Servidor([
        (409, {"code": T.CODIGO_CONFLICTO_SECUENCIA, "max_supervisor_seq": 41}),
        (409, {"code": T.CODIGO_CONFLICTO_SECUENCIA, "max_supervisor_seq": 99}),
    ])
    seq = A.SecuenciaSupervisor(en_frio=True)
    t = T.TransporteSupervisor("http://x", "tok", enviar=srv)
    with pytest.raises(A.ConflictoConcurrente, match="concurrencia"):
        t.observar(_obs(), seq, clave_base="k")
    assert len(srv.intentos) == 2, "exactamente un reintento, no un bucle"


@pytest.mark.parametrize("valor", [None, "41", True, 0, -1])
def test_conflicto_sin_max_valido_no_adivina(valor):
    srv = Servidor([(409, {"code": T.CODIGO_CONFLICTO_SECUENCIA,
                           "max_supervisor_seq": valor})])
    seq = A.SecuenciaSupervisor(en_frio=True)
    t = T.TransporteSupervisor("http://x", "tok", enviar=srv)
    with pytest.raises(A.ObservacionInvalida, match="no se adivina"):
        t.observar(_obs(), seq, clave_base="k")


def test_nunca_se_lee_status_seq():
    """Un 409 que trajera status_seq en vez del campo bueno NO debe curar nada."""
    srv = Servidor([(409, {"code": T.CODIGO_CONFLICTO_SECUENCIA, "status_seq": 41})])
    seq = A.SecuenciaSupervisor(en_frio=True)
    t = T.TransporteSupervisor("http://x", "tok", enviar=srv)
    with pytest.raises(A.ObservacionInvalida, match="no se adivina"):
        t.observar(_obs(), seq, clave_base="k")


# ── respuesta perdida: MISMA clave y MISMO cuerpo ────────────────────────────

def test_respuesta_perdida_reintenta_con_misma_clave_y_mismo_cuerpo():
    srv = Servidor([
        T.RespuestaPerdida("timeout"),
        _ok(seq=1, observation_id="o3", replayed=True),
    ])
    seq = A.SecuenciaSupervisor(en_frio=True)
    t = T.TransporteSupervisor("http://x", "tok", enviar=srv)
    r = t.observar(_obs(), seq, clave_base="k")
    assert srv.intentos[0] == srv.intentos[1], (
        "una respuesta perdida PUDO aplicarse: cambiar clave o cuerpo escribiría "
        "dos veces en vez de dejar que D4 devuelva la misma observación")
    assert r["replayed"] is True


def test_perdida_no_avanza_la_secuencia_antes_de_resolver():
    srv = Servidor([
        T.RespuestaPerdida("timeout"),
        _ok(seq=1, observation_id="o4", replayed=True),
    ])
    seq = A.SecuenciaSupervisor(en_frio=True)
    t = T.TransporteSupervisor("http://x", "tok", enviar=srv)
    t.observar(_obs(), seq, clave_base="k")
    assert [s for _, s in srv.intentos] == [1, 1], "no se avanza antes de resolver"


# ── reinicio del proceso con la MISMA identidad ──────────────────────────────

def test_proceso_reiniciado_aprende_del_409_en_vez_de_quedar_mudo():
    """El defecto original: reiniciar y volver a 1 dejaba el adaptador mudo.

    Ahora el 409 enseña el máximo y el reintento entra por encima.
    """
    srv = Servidor([
        (409, {"code": T.CODIGO_CONFLICTO_SECUENCIA, "max_supervisor_seq": 500}),
        _ok(seq=501, observation_id="o5"),
    ])
    reiniciada = A.SecuenciaSupervisor(en_frio=True)   # arranca "en 1", como tras un reinicio
    t = T.TransporteSupervisor("http://x", "tok", enviar=srv)
    t.observar(_obs(), reiniciada, clave_base="k")
    assert [s for _, s in srv.intentos] == [1, 501]


# ── target ajeno ─────────────────────────────────────────────────────────────

def test_target_ajeno_o_inexistente_se_propaga_sin_reintento():
    """404 tipado: no es desfase de secuencia, no se cura reintentando."""
    srv = Servidor([(404, {"code": T.CODIGO_SUJETO_AUSENTE})])
    seq = A.SecuenciaSupervisor(en_frio=True)
    t = T.TransporteSupervisor("http://x", "tok", enviar=srv)
    with pytest.raises(T.ErrorDelGateway) as exc:
        t.observar(_obs(), seq, clave_base="k")
    assert exc.value.status == 404
    assert len(srv.intentos) == 1, "un target ajeno no se reintenta"


# ── el cuerpo no lleva lo que va en ruta/cabecera ────────────────────────────

def test_el_cuerpo_no_lleva_runtime_instance_ni_la_clave():
    srv = Servidor([_ok(seq=1, observation_id="o6")])
    seq = A.SecuenciaSupervisor(en_frio=True)
    t = T.TransporteSupervisor("http://x", "tok", enviar=srv)

    capturado = {}
    def espia(url, cuerpo, clave):
        capturado.update(cuerpo=cuerpo, url=url)
        return srv(url, cuerpo, clave)

    t._enviar = espia
    t.observar(_obs(), seq, clave_base="k")
    assert "runtime_instance" not in capturado["cuerpo"], "va en la RUTA"
    assert "idempotency_key" not in capturado["cuerpo"], "va en la CABECERA"
    assert capturado["url"].endswith("/runtimes/r-target/observations")


# ── DOS pérdidas seguidas: la petición queda PENDIENTE, no se avanza ─────────

def test_dos_perdidas_dejan_la_peticion_pendiente_sin_avanzar():
    """🔻 MARK:astra-transport-pending-20260908. Antes, la 2ª pérdida salía y la
    llamada siguiente avanzaba `siguiente()` otra vez: duplicaba el hecho o
    chocaba en D4. Ahora queda pendiente y nada avanza."""
    srv = Servidor([T.RespuestaPerdida("t1"), T.RespuestaPerdida("t2")])
    seq = A.SecuenciaSupervisor(en_frio=True)
    t = T.TransporteSupervisor("http://x", "tok", enviar=srv)
    with pytest.raises(T.RespuestaPerdida):
        t.observar(_obs(), seq, clave_base="k")
    assert t.pendiente is True
    assert [s for _, s in srv.intentos] == [1, 1], "no avanza ni entre intentos"


def test_tras_dos_perdidas_la_reanudacion_usa_mismos_bytes_y_UNA_sola_observacion():
    srv = Servidor([
        T.RespuestaPerdida("t1"), T.RespuestaPerdida("t2"),
        _ok(seq=1, observation_id="unica", replayed=True),
    ])
    seq = A.SecuenciaSupervisor(en_frio=True)
    t = T.TransporteSupervisor("http://x", "tok", enviar=srv)
    with pytest.raises(T.RespuestaPerdida):
        t.observar(_obs(), seq, clave_base="k")
    r = t.observar(_obs(), seq, clave_base="k")        # reanuda
    assert r["observation_id"] == "unica"
    assert len(set(srv.intentos)) == 1, (
        "los 3 envíos tienen que ser el MISMO (clave, cuerpo): una sola observación")
    assert t.pendiente is False


def test_observacion_distinta_con_pendiente_se_rechaza():
    srv = Servidor([T.RespuestaPerdida("t1"), T.RespuestaPerdida("t2")])
    seq = A.SecuenciaSupervisor(en_frio=True)
    t = T.TransporteSupervisor("http://x", "tok", enviar=srv)
    with pytest.raises(T.RespuestaPerdida):
        t.observar(_obs(), seq, clave_base="k")
    otra = A.Observacion(
        workload_id="w1", runtime_instance="r-target", idempotency_key="x",
        supervisor_seq=1, observation_kind="exited", reason_code="PROCESS_EXITED",
        exit_code=0)
    with pytest.raises(T.PeticionPendiente, match="sin resolver"):
        t.observar(otra, seq, clave_base="k")


# ── endurecimiento de red ────────────────────────────────────────────────────

def test_solo_202_acredita_no_cualquier_2xx():
    srv = Servidor([(200, {"observation_id": "x", "supervisor_seq": 1})])
    seq = A.SecuenciaSupervisor(en_frio=True)
    t = T.TransporteSupervisor("http://x", "tok", enviar=srv)
    with pytest.raises(T.RespuestaInvalida, match="sólo 202"):
        t.observar(_obs(), seq, clave_base="k")


def test_202_sin_cuerpo_de_observacion_no_acredita():
    srv = Servidor([(202, {"ok": True})])
    seq = A.SecuenciaSupervisor(en_frio=True)
    t = T.TransporteSupervisor("http://x", "tok", enviar=srv)
    with pytest.raises(T.RespuestaInvalida, match="202 sin campos de observación"):
        t.observar(_obs(), seq, clave_base="k")
    assert t.pendiente is True, "una respuesta inválida no acredita aceptación"


def test_el_id_se_codifica_como_segmento_de_url():
    srv = Servidor([_ok(seq=1, observation_id="o", runtime_instance="r/../otro")])
    seq = A.SecuenciaSupervisor(en_frio=True)
    t = T.TransporteSupervisor("http://x", "tok", enviar=srv)
    capturado = {}
    def espia(url, cuerpo, clave):
        capturado["url"] = url
        return srv(url, cuerpo, clave)
    t._enviar = espia
    obs = A.Observacion(
        workload_id="w1", runtime_instance="r/../otro", idempotency_key="k",
        supervisor_seq=1, observation_kind="cycle_ack",
        reason_code="PROCESS_PRESENT")
    t.observar(obs, seq, clave_base="k")
    assert "r/../otro" not in capturado["url"], "el id no se interpola crudo"
    assert "r%2F..%2Fotro" in capturado["url"]


def test_la_perdida_no_propaga_texto_de_red():
    """El texto de URLError arrastra URL y a veces credencial: no viaja."""
    srv = Servidor([T.RespuestaPerdida("https://user:secreto@host/x timeout"),
                    T.RespuestaPerdida("https://user:secreto@host/x timeout")])
    seq = A.SecuenciaSupervisor(en_frio=True)
    t = T.TransporteSupervisor("http://x", "tok", enviar=srv)
    with pytest.raises(T.RespuestaPerdida) as exc:
        t.observar(_obs(), seq, clave_base="k")
    # el doble inyecta el texto; lo que se comprueba es que el transporte REAL
    # construye su propia excepción sin texto de red (ver _enviar_http)
    import inspect
    fuente = inspect.getsource(T.TransporteSupervisor._enviar_http)
    assert 'RespuestaPerdida("sin respuesta del gateway")' in fuente
    assert "from None" in fuente, "no se encadena el error original"


# ── validación ESTRICTA del 202 (no nominal) ─────────────────────────────────


@pytest.mark.parametrize("cambio,motivo", [
    ({"observation_id": None}, "cadena no vacía"),
    ({"observation_id": "  "}, "cadena no vacía"),
    ({"supervisor_seq": True}, "entero que se envió"),
    ({"supervisor_seq": 99}, "entero que se envió"),
    ({"runtime_instance": "otro"}, "no es el target"),
    ({"workload_id": "otro"}, "no es el enviado"),
    ({"replayed": "si"}, "booleano"),
])
def test_202_mal_formado_no_acredita_y_CONSERVA_pendiente(cambio, motivo):
    """🔻 MARK:astra-transport-response-review-20260908 §1. La comprobación
    nominal aceptaba observation_id=None y supervisor_seq=True, y encima
    limpiaba la incertidumbre."""
    srv = Servidor([_ok(**cambio)])
    seq = A.SecuenciaSupervisor(en_frio=True)
    t = T.TransporteSupervisor("http://x", "tok", enviar=srv)
    with pytest.raises(T.RespuestaInvalida, match=motivo):
        t.observar(_obs(), seq, clave_base="k")
    assert t.pendiente is True, "un 202 inválido NO resuelve la incertidumbre"


def test_202_valido_si_acredita():
    srv = Servidor([_ok()])
    seq = A.SecuenciaSupervisor(en_frio=True)
    t = T.TransporteSupervisor("http://x", "tok", enviar=srv)
    assert t.observar(_obs(), seq, clave_base="k")["observation_id"] == "obs-1"
    assert t.pendiente is False


def test_el_error_no_refleja_el_cuerpo_remoto():
    """Reflejar claves ajenas mete texto de otro en mis logs."""
    srv = Servidor([(202, {"inyectado": "<script>", "otra": "x"})])
    seq = A.SecuenciaSupervisor(en_frio=True)
    t = T.TransporteSupervisor("http://x", "tok", enviar=srv)
    with pytest.raises(T.RespuestaInvalida) as exc:
        t.observar(_obs(), seq, clave_base="k")
    assert "inyectado" not in str(exc.value) and "<script>" not in str(exc.value)


# ── identidad acoplada de verdad al transporte ───────────────────────────────

def test_el_transporte_EXIGE_el_contexto_antes_de_reservar():
    """§2: `exige_contexto` estaba definido, testeado y NADIE lo llamaba."""
    ctx = ("rti-1", 3, "r-target", 7)
    srv = Servidor([])                       # no debe llegar a enviar nada
    seq = A.SecuenciaSupervisor(en_frio=True, contexto=("rti-OTRO", 3, "r-target", 7))
    t = T.TransporteSupervisor("http://x", "tok", contexto=ctx, enviar=srv)
    with pytest.raises(A.IdentidadCambiada):
        t.observar(_obs(), seq, clave_base="k")
    assert srv.intentos == [], "ni siquiera se envió"


def test_sin_contexto_se_declara_no_verificado_en_vez_de_llamarlo_protegido():
    t = T.TransporteSupervisor("http://x", "tok")
    assert t.identidad_verificada is False


def test_el_error_de_identidad_no_publica_valores():
    ctx = ("rti-secreto-1", 3, "target-secreto", 7)
    seq = A.SecuenciaSupervisor(en_frio=True, contexto=ctx)
    with pytest.raises(A.IdentidadCambiada) as exc:
        seq.exige_contexto(("rti-secreto-2", 3, "target-secreto", 7))
    assert "secreto" not in str(exc.value), "los nombres bastan, los valores no viajan"


def test_la_reanudacion_exige_LA_MISMA_secuencia():
    srv = Servidor([T.RespuestaPerdida("t1"), T.RespuestaPerdida("t2")])
    seq = A.SecuenciaSupervisor(en_frio=True)
    t = T.TransporteSupervisor("http://x", "tok", enviar=srv)
    with pytest.raises(T.RespuestaPerdida):
        t.observar(_obs(), seq, clave_base="k")
    otra_equivalente = A.SecuenciaSupervisor(en_frio=True)
    with pytest.raises(T.PeticionPendiente):
        t.observar(_obs(), otra_equivalente, clave_base="k")


def test_secuencia_SIN_contexto_se_rechaza_si_el_transporte_afirma_identidad():
    """El borde: `identidad_verificada` decía True con una secuencia suelta."""
    srv = Servidor([])
    seq = A.SecuenciaSupervisor(en_frio=True)          # sin contexto
    t = T.TransporteSupervisor("http://x", "tok",
                               contexto=("rti-1", 3, "r-target", 7), enviar=srv)
    assert t.identidad_verificada is True
    with pytest.raises(A.IdentidadCambiada, match="no lleva contexto"):
        t.observar(_obs(), seq, clave_base="k")
    assert srv.intentos == [], "CERO envíos"


def test_camino_sin_contexto_sigue_permitido_pero_declarado_no_verificado():
    srv = Servidor([_ok()])
    seq = A.SecuenciaSupervisor(en_frio=True)
    t = T.TransporteSupervisor("http://x", "tok", enviar=srv)   # sin contexto
    assert t.identidad_verificada is False
    t.observar(_obs(), seq, clave_base="k")                     # compatibilidad
    assert len(srv.intentos) == 1
