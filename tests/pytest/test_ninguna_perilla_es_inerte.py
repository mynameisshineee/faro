"""Una variable que el código lee y que compose no pasa es una perilla que no gira.

Lo cazó EJECUTAR un rollback en vez de afirmarlo. Le había dicho a @harness «el mecanismo
está construido; encenderlo es un `LLMINBOX_WIP_GLOBAL`». Puse el flag, arranqué, y
`/claim` seguía sin declarar `wip`: la variable nunca llegaba al contenedor. Lo que
construí era inalcanzable desde donde se opera.

Y no era una: eran DOCE. Entre ellas `LLMINBOX_TOPE_EJECUTA`, o sea que el 3→1 por owner
que el operador tendría que autorizar TAMPOCO habría funcionado — se habría autorizado un
cambio, se habría puesto la variable, y el tope habría seguido en 3 sin que nadie lo viera,
porque el arranque no protesta por una variable que ignora.

Es la misma clase que ya me mordió con `llmi up` dejando de construir y con
`LLMINBOX_POLICY_DIR` sin sobrevivir a la shell: el código está bien y no está CONECTADO.

Este test fija la clase, no las doce instancias. La excepción existe pero tiene que
DECLARARSE con su motivo: lo que no vale es que una perilla nueva nazca muerta en silencio.
"""
from __future__ import annotations
import pathlib
import re

# Variables que el código lee y que compose NO pasa A PROPÓSITO, cada una con su razón.
# Añadir aquí es una decisión que se escribe; olvidarse de compose, no.
NO_SE_PASAN = {
    "LLMINBOX_POLICY": "el defecto `/_shared_refs/fleet-operating-policy.json` ES la ruta "
                       "de dentro del contenedor; lo que se configura desde fuera es el "
                       "MONTAJE (LLMINBOX_POLICY_DIR), no la ruta interna",
    "LLMINBOX_DESTILADOR": "nombre de rol con defecto estable; cambiarlo desde el entorno "
                           "renombraría un rol del censo sin tocar roster.json, que es "
                           "justo la divergencia que no queremos",
}


def _vars_del_codigo() -> set[str]:
    """CUALQUIER literal `LLMINBOX_*` del fichero, no sólo los leídos en línea.

    Mi primera versión buscaba `os.environ.get("LLMINBOX_X")` y por eso era CIEGA a las
    que se pasan como argumento a un helper — `_bandera_env("LLMINBOX_CARRIL_OBLIGATORIO")`
    lee `os.environ.get(nombre)`, con `nombre` como variable. Dos variables quedaban fuera
    del censo, y una de ellas era huérfana de verdad.

    O sea que el detector que escribí para cazar perillas inertes tenía EXACTAMENTE el
    agujero que existía para tapar, y su control positivo no lo veía porque `LLMINBOX_DB`
    sí se lee en línea. Un control positivo comprueba que el detector no está muerto; no
    comprueba que su ALCANCE sea el que crees.

    La forma amplia tiene un precio declarado: cuenta también los literales de comentarios
    y mensajes de error. Es el lado bueno del que equivocarse — un falso positivo se
    resuelve declarando la excepción con su motivo; un falso negativo es una perilla muerta
    que nadie ve.
    """
    t = pathlib.Path("servicio.py").read_text()
    return set(re.findall(r'["\'](LLMINBOX_[A-Z_]+)["\']', t))


def _vars_de_compose() -> set[str]:
    t = pathlib.Path("docker-compose.yml").read_text()
    return set(re.findall(r'^\s+(LLMINBOX_[A-Z_]+):', t, re.M))


def test_el_detector_VE_las_que_se_pasan_a_un_helper():
    """⊖ del alcance, que es distinto de «no está muerto».

    `LLMINBOX_CARRIL_OBLIGATORIO` se lee vía `_bandera_env(nombre)`. Si el detector vuelve
    a mirar sólo `os.environ.get("LITERAL")`, esta variable desaparece del censo y con ella
    cualquier perilla futura que use un helper — que es el patrón que este fichero mismo
    recomienda para validar config.
    """
    cod = _vars_del_codigo()
    assert "LLMINBOX_CARRIL_OBLIGATORIO" in cod, (
        "el detector no ve las variables pasadas como argumento a un helper: es ciego "
        "justo donde el repo tiene la validación de config")


def test_los_detectores_no_estan_muertos():
    """⊕ CONTROL POSITIVO, primero. Si los dos regex fallaran, los conjuntos saldrían
    vacíos y «no falta ninguna» sería verdad por vacuidad — un verde que no significa
    nada, que es la avería que este fichero entero persigue."""
    cod, comp = _vars_del_codigo(), _vars_de_compose()
    assert "LLMINBOX_DB" in cod, "el detector del código no encuentra ni LLMINBOX_DB"
    assert "LLMINBOX_DB" in comp, "el detector de compose no encuentra ni LLMINBOX_DB"
    assert len(cod) > 10 and len(comp) > 10, (cod, comp)


def test_ninguna_perilla_nace_muerta():
    """⊖: añadir un `os.environ.get("LLMINBOX_LO_QUE_SEA")` sin pasarlo por compose."""
    huerfanas = _vars_del_codigo() - _vars_de_compose() - set(NO_SE_PASAN)
    assert not huerfanas, (
        f"el código lee estas variables y compose no las pasa: {sorted(huerfanas)}. "
        f"En el contenedor son INERTES: se pueden poner, el arranque no protesta, y no "
        f"hacen nada. Pásalas en docker-compose.yml, o declara aquí por qué no, con su "
        f"motivo")


def test_la_clave_de_cursor_viaja_sin_secreto_ni_default_compartido():
    """El cableado no puede convertir una perilla inerte en una clave pública."""
    t = pathlib.Path("docker-compose.yml").read_text()
    m = re.search(r"^\s+LLMINBOX_SEARCH_CURSOR_KEY:\s*(.+)$", t, re.M)
    assert m, "la clave de cursor no llega al contenedor"
    assert m.group(1).strip().strip('"\'') == "${LLMINBOX_SEARCH_CURSOR_KEY:-}", (
        "la clave debe venir sólo del entorno del despliegue, vacía si falta; "
        "un literal o default quedaría compartido por todos los clones")


def test_las_excepciones_estan_JUSTIFICADAS_y_vivas():
    """⊖ de la lista de excepciones: una entrada que ya no exista en el código la deja de
    proteger a nadie y disfraza de decisión lo que es residuo."""
    cod = _vars_del_codigo()
    for v, razon in NO_SE_PASAN.items():
        assert v in cod, f"{v} está exceptuada pero el código ya no la lee: borra la excepción"
        assert len(razon) > 40, f"{v} exceptuada sin motivo de verdad"


def test_no_se_pasa_lo_que_nadie_lee(): 
    """La otra dirección. Una variable en compose que servicio.py no lee es una promesa al
    operador que nadie cumple: la pone, no pasa nada, y no hay error. Se permite si otro
    fichero del repo la lee (llmi, el front, otro servicio), pero eso hay que probarlo."""
    import subprocess
    sobra = _vars_de_compose() - _vars_del_codigo()
    huerfanas = []
    for v in sorted(sobra):
        r = subprocess.run(["grep", "-rl", v, "--include=*.py", "--include=*.ts",
                            "--include=*.tsx", "--include=*.sh", "."],
                           capture_output=True, text=True)
        otros = [f for f in r.stdout.split() if "docker-compose" not in f
                 and "test_ninguna_perilla" not in f]
        if not otros:
            huerfanas.append(v)
    assert not huerfanas, (
        f"compose promete estas variables y nadie las lee: {huerfanas}. El operador puede "
        f"ponerlas y no pasa nada, sin error")
