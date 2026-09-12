"""NO-GO del auditor sobre `811f635`: la FORMA se validaba ANTES que la SESIÓN.

`accept_event` y `submit_command` llamaban a `_sin_atribucion(...)` encima del
`authenticate()` — en `accept_event`, encima del propio párrafo que explicaba por
qué eso no se hace. MEDIDO por mi mano sobre `811f635`, con un token que no
existe en ninguna parte:

    accept_event   intent LIMPIO                 -> AuthError
    accept_event   intent + `agent`              -> AttributionRejected
    accept_event   trace  + `runtime`            -> AttributionRejected
    submit_command payload LIMPIO                -> AuthError
    submit_command payload + `credential`        -> AttributionRejected

⇒ **ORÁCULO EJECUTABLE**: quien no ha demostrado ser nadie enumera el conjunto de
reservadas por respuesta diferencial, una palabra por intento. Y con él, la forma
del contrato: qué campos pone el servidor.

🩸 **No lo estrené yo, y decirlo importa para no inflar ni encoger el hallazgo.**
Sobre `9793f8a` ya filtraba —medido: `principal` y `role` daban
`AttributionRejected` con token inválido, `agent` y `credential` daban
`AuthError` porque aún no eran reservadas—. La deuda es vieja; lo que hice al
añadir las seis raíces fue **ENSANCHARLA de `21` a `25`** claves.

El censo del final es la parte que no caduca: fija el invariante sobre TODOS los
métodos públicos, no sobre los dos que el auditor nombró.
"""
from __future__ import annotations

import inspect
import re

import pytest

import coordination as C
from ._arnes import INTENT, journal, sesion

TOKEN_MUERTO = "token-que-no-existe-en-ninguna-parte"


@pytest.fixture()
def j(tmp_path):
    diario = journal(tmp_path)
    # Hay una sesión VÁLIDA viva a propósito: el ⊖ tiene que medir «este token no
    # vale», no «no hay ninguna sesión». Sin ella, el journal podría estar
    # rechazando por un motivo distinto del que se cree probar.
    sesion(diario, "c", principal="p", role="be")
    yield diario
    diario.close()


CUERPOS_SUCIOS = [
    ("intent", lambda d, k: d.accept_event(
        TOKEN_MUERTO, idempotency_key=f"k-{k}",
        intent={**INTENT, "meta": {k: "cto"}}, ledger="llminbox")),
    ("trace", lambda d, k: d.accept_event(
        TOKEN_MUERTO, idempotency_key=f"t-{k}", intent=INTENT,
        ledger="llminbox", trace={"s": {k: "cto"}})),
    ("payload", lambda d, k: d.submit_command(
        TOKEN_MUERTO, workstream_id="w", revision=1,
        payload={"o": "x", "meta": {k: "cto"}})),
]


@pytest.mark.parametrize("donde,verbo", CUERPOS_SUCIOS, ids=[c[0] for c in CUERPOS_SUCIOS])
@pytest.mark.parametrize("clave", ["agent", "credential", "runtime", "principal",
                                   "attribution", "role", "carril"])
def test_con_token_INVALIDO_un_cuerpo_sucio_da_AuthError_y_NO_delata_la_reservada(
        j, donde, verbo, clave):
    """El ⊖ del oráculo. `AttributionRejected` aquí sería la fuga."""
    with pytest.raises(C.AuthError) as e:
        verbo(j, clave)
    assert not isinstance(e.value, C.AttributionRejected)


@pytest.mark.parametrize("donde,verbo", CUERPOS_SUCIOS, ids=[c[0] for c in CUERPOS_SUCIOS])
def test_el_cuerpo_SUCIO_y_el_LIMPIO_dan_LA_MISMA_clase_de_error(j, donde, verbo):
    """El invariante que de verdad cierra el oráculo, y por eso NO se mide como
    «da AuthError» sino como IGUALDAD entre las dos ramas.

    Una guarda podría dar `AuthError` en los dos casos y aun así filtrar por el
    MENSAJE. Se comparan clase Y texto: sin diferencia observable no hay canal.
    """
    def limpio(d):
        if donde == "payload":
            return d.submit_command(TOKEN_MUERTO, workstream_id="w", revision=1,
                                    payload={"o": "x"})
        return d.accept_event(TOKEN_MUERTO, idempotency_key="limpio",
                              intent=INTENT, ledger="llminbox")

    with pytest.raises(C.JournalError) as sucio:
        verbo(j, "agent")
    with pytest.raises(C.JournalError) as puro:
        limpio(j)
    assert type(sucio.value) is type(puro.value)
    assert str(sucio.value) == str(puro.value), (
        "misma clase pero MENSAJE distinto: el oráculo sigue abierto por el texto")


@pytest.mark.parametrize("clave", ["agent", "principal", "credential"])
def test_OMEGA_con_token_VALIDO_la_reservada_SIGUE_rechazandose(tmp_path, clave):
    """⊕ El control que impide cerrar de más. Mover la guarda debajo de la sesión
    no puede APAGARLA: para quien SÍ es alguien, el rechazo por atribución tiene
    que seguir llegando — y es el que `A7` exige."""
    d = journal(tmp_path)
    s = sesion(d, "c", principal="p", role="be")
    with pytest.raises(C.AttributionRejected):
        d.accept_event(s.token, idempotency_key="k", intent={**INTENT, "meta": {clave: "x"}},
                       ledger="llminbox")
    with pytest.raises(C.AttributionRejected):
        d.submit_command(s.token, workstream_id="w", revision=1,
                         payload={"meta": {clave: "x"}})
    d.close()


def test_CENSO_ninguna_puerta_publica_valida_la_FORMA_antes_de_AUTENTICAR():
    """La parte que no caduca: el invariante sobre la POBLACIÓN, no sobre los dos
    métodos que el auditor nombró.

    Un test que sólo mirara `accept_event` y `submit_command` saldría verde el día
    que alguien escriba la tercera puerta con el mismo orden. La población es
    «todo método público de `Journal` cuyo primer parámetro es `token`», y se
    enumera del objeto vivo, no de una lista escrita a mano que envejece.

    ⊕ El propio censo trae su control: si el detector no encontrara NINGUNA
    llamada a `authenticate` en ningún método, saldría verde por vacío — por eso
    se exige que la población medida sea > 0 y que al menos una puerta conocida
    aparezca en ella.
    """
    FORMA = re.compile(r"_sin_atribucion|_validar_gramatica|_resolver_destinos")
    AUTH = re.compile(r"self\.authenticate\(|_authenticate_locked\(")

    poblacion, rotas = [], []
    for nombre, fn in vars(C.Journal).items():
        if nombre.startswith("_") or not callable(fn):
            continue
        try:
            params = list(inspect.signature(fn).parameters)
            fuente = inspect.getsource(fn).split("\n")
        except (TypeError, OSError, ValueError):
            continue
        if len(params) < 2 or params[1] != "token":
            continue
        i_forma = next((i for i, l in enumerate(fuente) if FORMA.search(l)), None)
        i_auth = next((i for i, l in enumerate(fuente) if AUTH.search(l)), None)
        if i_auth is None:
            continue
        poblacion.append(nombre)
        if i_forma is not None and i_forma < i_auth:
            rotas.append(nombre)

    assert len(poblacion) >= 10, (
        f"el censo sólo vio {len(poblacion)} puertas: el detector está roto y un "
        f"verde suyo no significaría nada")
    assert "accept_event" in poblacion and "submit_command" in poblacion, (
        "las dos puertas que el auditor midió no están en la población: el "
        "filtro no selecciona lo que cree")
    assert not rotas, (
        f"estas puertas validan la FORMA antes de AUTENTICAR y filtran un oráculo "
        f"del contrato a quien no ha demostrado ser nadie: {rotas}")
