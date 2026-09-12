#!/usr/bin/env python3
"""Pasarela nativa FALSA, con perfiles, para falsar el arnés E2E.

Por qué existe: el arnés E2E dice «16 ausentes, 0 incompatibles» contra un servicio
sin pasarela. Eso NO acredita que sepa detectar una incompatibilidad — sólo que sabe
ver un 404. Un arnés que jamás ha visto un gateway no puede afirmar nada sobre lo que
haría con uno, y un auditor lo cazó exactamente ahí: con un stub roto daba 17 verdes.

Perfiles:
  conforme  cumple el ADR en lo que el arnés sabe medir  -> el arnés debe salir VERDE
  roto      whoami/auth/propuestas 500 y un replay con `replayed:true` SIN ids
            -> el arnés DEBE marcar INCOMPAT, y si sale verde es que no discrimina
  ausente   404 en todo menos /health -> el estado real de hoy
  auth403   /whoami rechaza AMBOS esquemas con 403 -> el arnes NO puede decir verde

Registra CADA petición (método y ruta) en el fichero de log, que es lo que permite
afirmar «no se emitió ni un POST» en vez de suponerlo.
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

PREFIJO = "/native/v1"
PERFIL = sys.argv[1]
PUERTO = int(sys.argv[2])
LOG = sys.argv[3]

# El replay tiene que citar los MISMOS ids (ADR §136). El perfil `roto` devuelve el
# flag y NO los ids: es la forma exacta de mentir que el arnés tiene que cazar.
# 🩸 IDs ESTATICOS = ARNES CIEGO. Con `evt_1`/`rcp_1` fijos, «el replay cita los MISMOS
# ids» pasaba aunque el servidor no emitiera nada: dos intentos DISTINTOS tambien daban
# los mismos. Ahora los emite el servidor, uno por aceptacion, asi que el aserto separa
# «los conserva en el replay» de «los repite siempre».
_SERIE = [0]
VISTOS = {}
SESIONES = set()
SESION_SERIE = [0]


def _nuevos_ids():
    _SERIE[0] += 1
    return {"event_id": "evt_%d_%d" % (id(_SERIE) % 9973, _SERIE[0]),
            "receipt_id": "rcp_%d_%d" % (id(_SERIE) % 9973, _SERIE[0])}


# La credencial de workload que este stub reconoce. Configurable para poder montar el
# CONTROL NEGATIVO (credencial de workload INCORRECTA) sin tocar el codigo del stub.
WL_CRED = os.environ.get("LLMI_STUB_WORKLOAD", "wl-credencial")


def _bearer(cabeceras):
    """(esquema_ok, token). El legado NO cuenta: D8 lo rechaza en rutas nativas."""
    aut = cabeceras.get("Authorization") or ""
    esquema, _, tok = aut.partition(" ")
    return (esquema.lower() == "bearer" and bool(tok.strip())), tok.strip()


class H(BaseHTTPRequestHandler):
    def log_message(self, *_a):
        pass

    def _reg(self):
        clave = self.headers.get("Idempotency-Key") or ""
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write("%s %s%s\n" % (self.command, self.path.split("?")[0],
                                    (" Idempotency-Key: %s" % clave) if clave else ""))

    def _responde(self, codigo, cuerpo=None):
        # D11 dice PLANO. El perfil `roto` ENVUELVE los errores a proposito: sin ese
        # discriminante, «el cuerpo es plano» no lo puede acreditar nadie — pasaria igual
        # con un arnes que no mirase la forma.
        if PERFIL == "roto" and codigo >= 400 and isinstance(cuerpo, dict) \
                and "error" not in cuerpo:
            cuerpo = {"error": cuerpo}
        crudo = b"" if cuerpo is None else json.dumps(cuerpo).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(crudo)))
        self.end_headers()
        if crudo:
            self.wfile.write(crudo)

    def _cuerpo(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except Exception:
            return {}

    def _ruta(self):
        """Ruta SIN el prefijo nativo. La pasarela real sirve bajo `/native/v1` (D9) y
        `/health` sigue en la raiz con las 28 legado: el stub tiene que reproducir esa
        division o el arnes pasaria por el motivo equivocado."""
        r = self.path.split("?")[0]
        if r.startswith(PREFIJO):
            return r[len(PREFIJO):] or "/"
        return r

    def do_GET(self):
        self._reg()
        ruta = self._ruta()
        if self.path.split("?")[0] == "/health":   # legado, en la RAIZ, nunca con prefijo
            return self._responde(200, {"ok": True})
        if PERFIL == "ausente":
            return self._responde(404, {"detail": "Not Found"})
        if ruta == "/whoami":
            if PERFIL == "roto":
                return self._responde(500, {"detail": "boom"})
            if PERFIL == "auth403":
                # El caso que el arnes daba por VERDE: rechaza LOS DOS esquemas con 403.
                # Con 403/403 no hay evidencia de cual admite, y «no lo se» no es conforme.
                return self._responde(403, {"code": "POLICY_DENIED"})
            # EL ESQUEMA IMPORTA: sin esto, el falsador del esquema no prueba nada porque
            # el stub contestaria 200 a cualquier cabecera.
            aut = self.headers.get("Authorization") or ""
            esquema, _, tok = aut.partition(" ")
            if not aut:
                return self._responde(401, {"code": "SESSION_INVALID"})
            if esquema.lower() != "bearer":
                return self._responde(401, {"code": "SESSION_INVALID"})
            # Sólo se admiten credenciales CONOCIDAS: la de workload y los tokens de
            # sesion que este stub emite. Con «cualquier Bearer vale», una credencial
            # invalida daba 200 y la sonda del cuerpo de ERROR no llegaba a ningun error.
            if tok.strip() != WL_CRED and tok.strip() not in SESIONES:
                # Esquema BIEN, credencial mala: 401. Es la respuesta correcta y por eso
                # NO acredita el esquema — un gateway de otro esquema diria 401 igual.
                return self._responde(401, {"code": "SESSION_INVALID"})
            identidad = {"principal": "fe", "role": "fe", "lane": "pruebas"}
            if tok.strip() in SESIONES:
                identidad["runtime_instance"] = "rti-1"
            return self._responde(200, identidad)
        if ruta.startswith("/receipts/") or ruta.endswith("/receipt"):
            if PERFIL == "roto":
                return self._responde(500, {"detail": "boom"})
            ids = _nuevos_ids()
            return self._responde(200, dict(
                ids, state="accepted", subject_type="event",
                subject_id=ids["event_id"],
            ))
        # Rutas de POST sondeadas con GET: 405 = existe. Es la sonda que NO muta.
        if ruta in ("/sessions", "/sessions/refresh", "/events"):
            return self._responde(405, {"detail": "Method Not Allowed"})
        if ruta.startswith("/leases/"):
            if PERFIL == "roto":
                return self._responde(500, {"detail": "boom"})
            return self._responde(405, {"detail": "Method Not Allowed"})
        return self._responde(404, {"detail": "Not Found"})

    def do_POST(self):
        self._reg()
        ruta = self._ruta()
        cuerpo = self._cuerpo()
        if PERFIL == "ausente":
            return self._responde(404, {"detail": "Not Found"})
        if ruta == "/sessions":
            # BOOTSTRAP: SOLO aqui vale la credencial de WORKLOAD, por Bearer. Ni el token
            # compartido (D8 lo cierra) ni un token de sesion.
            ok, tok = _bearer(self.headers)
            # 🩸 P2 (auditor): esto era `if not ok or tok.startswith("ses-")`, o sea
            # «vale CUALQUIER Bearer que no sea de sesion». Con eso, una credencial de
            # workload EQUIVOCADA abria sesion con 201 y el falsador del bootstrap no
            # discriminaba nada: media que el CLI manda *un* Bearer, no que mande *el*
            # correcto. Ahora se compara con la credencial CONOCIDA.
            if not ok or tok != WL_CRED:
                return self._responde(401, {"code": "RUNTIME_CREDENTIAL_REQUIRED",
                                            "detail": "se requiere Authorization: Bearer con credencial de workload"})
            SESION_SERIE[0] += 1
            token = "ses-t%d" % SESION_SERIE[0]
            SESIONES.add(token)
            return self._responde(201, {"token": token, "runtime_instance": "rti-1",
                                        "expires_at": "2099-01-01T00:00:00Z", "generation": 1})
        if ruta == "/sessions/refresh":
            # ROTAR es otra cosa: `refresh_session(token)` rota UNA sesion concreta, asi que
            # exige el token de SESION. La credencial de workload NO identifica cual rotar y
            # aqui da 401 — sin este brazo, el arnes no podria distinguir las dos puertas.
            ok, tok = _bearer(self.headers)
            if not ok or tok not in SESIONES:
                return self._responde(401, {"code": "SESSION_INVALID",
                                            "detail": "refresh rota una sesion concreta: se requiere su token"})
            SESIONES.remove(tok)
            SESION_SERIE[0] += 1
            nuevo = "ses-r%d" % SESION_SERIE[0]
            SESIONES.add(nuevo)
            return self._responde(200, {"token": nuevo, "runtime_instance": "rti-1",
                                        "expires_at": "2099-01-01T00:00:00Z", "generation": 2})
        if ruta == "/events":
            # MUTACION: exige token de SESION por Bearer (los de sesion llevan el prefijo
            # `ses-`). Ni legado ni credencial de workload.
            ok, tok = _bearer(self.headers)
            if not ok or tok not in SESIONES:
                return self._responde(401, {"code": "SESSION_INVALID",
                                            "detail": "se requiere Authorization: Bearer con token de sesion"})
            requeridos = {"type", "verb", "to", "kind", "head", "body"}
            if not requeridos.issubset(cuerpo) or "intent" in cuerpo:
                return self._responde(422, {"code": "INVALID_BODY"})
            # La atribución en el cuerpo se RECHAZA (ADR): el perfil roto la acepta.
            if any(k in cuerpo for k in ("principal", "actor", "role", "lane")):
                if PERFIL == "roto":
                    return self._responde(202, _nuevos_ids())
                return self._responde(422, {"code": "ATTRIBUTION_REJECTED"})
            clave = self.headers.get("Idempotency-Key") or ""
            huella = json.dumps(cuerpo, sort_keys=True)
            if clave in VISTOS:
                previos, huella_previa = VISTOS[clave]
                if huella_previa != huella:
                    return self._responde(409, {"code": "IDEMPOTENCY_CONFLICT"})
                if PERFIL == "roto":
                    # El flag SIN los ids: miente en la forma que mas cuesta ver.
                    return self._responde(200, {"replayed": True})
                # ADR (bdec479): el replay cita los MISMOS ids con 200.
                return self._responde(200, dict(previos, replayed=True))
            ids = _nuevos_ids()
            VISTOS[clave] = (ids, huella)
            # ADR (bdec479): la aceptacion es 202, no 201 — `201 Created` prometeria un
            # recurso proyectado que todavia no existe.
            return self._responde(202, dict(ids, replayed=False))
        if ruta.startswith("/leases/"):
            if PERFIL == "roto":
                return self._responde(500, {"detail": "boom"})
            return self._responde(201, {"fencing_token": 1, "expires_at": "2099"})
        return self._responde(404, {"detail": "Not Found"})

    def do_PUT(self):
        self._reg()
        if PERFIL == "ausente":
            return self._responde(404, {"detail": "Not Found"})
        if PERFIL == "roto":
            return self._responde(500, {"detail": "boom"})
        return self._responde(200, {"fencing_token": 1})

    def do_DELETE(self):
        self._reg()
        if PERFIL == "ausente":
            return self._responde(404, {"detail": "Not Found"})
        if PERFIL == "roto":
            return self._responde(500, {"detail": "boom"})
        ok, tok = _bearer(self.headers)
        if not ok or tok not in SESIONES:
            return self._responde(401, {"code": "SESSION_INVALID"})
        if self._ruta() == "/sessions/current":
            SESIONES.remove(tok)
            return self._responde(204)
        if self._ruta().startswith("/leases/"):
            return self._responde(204)
        return self._responde(404, {"code": "SUBJECT_NOT_FOUND"})


if __name__ == "__main__":
    open(LOG, "w", encoding="utf-8").close()
    HTTPServer(("127.0.0.1", PUERTO), H).serve_forever()
