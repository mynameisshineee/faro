# Comprueba los textos revisados contra los inputs exactos. La salida conserva
# el alcance de colección; no certifica el cierre del bundle ni autoriza release.
FROM python:3.13-slim@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91 AS notices
COPY tools/check-third-party-texts.py /check-third-party-texts.py
COPY requirements.in requirements.lock /notice-inputs/
COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml /notice-inputs/web/
COPY third_party/ /third_party/
RUN python3 /check-third-party-texts.py --input-root /notice-inputs --notices-root /third_party

# ── etapa 1: la interfaz ──────────────────────────────────────────────────────
# Se compila DENTRO de la imagen, así que quien la usa sigue haciendo
# `docker compose up` y nada más — no necesita Node, ni pnpm, ni saber que esto
# es React. El autohospedaje no se pierde por tener una etapa de compilación:
# se perdería por depender de un CDN en tiempo de ejecución, y no hay ninguno.
# BASE POR DIGEST, no por tag. `node:24-slim` es una etiqueta MÓVIL: dos builds del
# mismo commit en dos días distintos podían traer bases distintas, y entonces
# «reproducible» no era una propiedad del commit sino del calendario. El digest es de
# la lista de manifiestos, así que sigue sirviendo para arm64 y amd64.
# Resuelto el 2026-09-05 con `docker image inspect --format '{{.RepoDigests}}'`.
FROM node:24-slim@sha256:3638d9a6fe4030bd716be989438248074489337ba3275657f93595428be4fc03 AS web
WORKDIR /web
ARG PNPM_VERSION=10.33.2
ARG PNPM_SHA512=a90faf6feeab71ad6c6e57f94e0fe1a12f5dcc22cd754db40ae9593eb6a3e0b6b12e3540218bb37ae083404b1f2ce6db2a4121e979829b4aff94b99f49da1cf8
# Corepack bundled in the base can reject its own integrity-suffixed descriptor.
# Fetch one exact tarball, verify it before execution, then install it locally.
RUN npm pack "pnpm@$PNPM_VERSION" --pack-destination /tmp \
 && echo "$PNPM_SHA512  /tmp/pnpm-$PNPM_VERSION.tgz" | sha512sum -c - \
 && npm install --global --ignore-scripts "/tmp/pnpm-$PNPM_VERSION.tgz" \
 && rm -f "/tmp/pnpm-$PNPM_VERSION.tgz"
COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm build

# ── etapa 2: el servicio ──────────────────────────────────────────────────────
# BASE POR DIGEST (mismo motivo que arriba). Y aquí importa MÁS: de esta base sale el
# SQLite del servicio, que `requirements.lock` no cubre — un cambio de base puede
# mover la biblioteca sin mover una sola línea del lock. Por eso el artefacto la
# atestigua abajo.
FROM python:3.13-slim@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91

ARG VCS_REF=unknown
LABEL org.opencontainers.image.source="https://github.com/mynameisshineee/faro" \
      org.opencontainers.image.revision="$VCS_REF" \
      org.opencontainers.image.licenses="Apache-2.0"

# Sin compilador ni ruedas nativas: fastapi + uvicorn + pydantic bastan, y el
# índice va sobre el sqlite3 de la stdlib. Imagen pequeña = arranque rápido =
# el servicio se puede matar y levantar sin ceremonia (propiedad T5).
# DEPENDENCIAS CON HASH. Antes: `fastapi==0.121.*`, `uvicorn[standard]==0.39.*` y
# `pydantic==2.*` —este último flotando en TODO el 2.x—, resueltos contra PyPI en el
# instante del build. `--require-hashes` obliga a que TODA dependencia, también las
# transitivas, venga fijada y verificada: si una sola no trae hash, pip se niega en vez
# de resolver por su cuenta. `requirements.in` declara las actualizaciones deliberadas;
# nunca participa directamente en una build de release.
COPY requirements.lock /app/requirements.lock
# `pip` compila por defecto bytecode pyc basado en la hora de extracción de cada
# wheel. Dos instalaciones de los mismos bytes producían por ello capas distintas.
# Se instala sin compilar y se genera después bytecode PEP 552 hash-based: el
# contenido depende de la fuente, no del reloj del builder.
RUN pip install --no-cache-dir --no-compile --require-hashes -r /app/requirements.lock \
 && python3 -m compileall --invalidation-mode=checked-hash -q /usr/local/lib/python3.13/site-packages

WORKDIR /app
# Todos los módulos locales alcanzables desde el entrypoint tienen que viajar en la
# imagen. `tests/pytest/test_docker_local_imports.py` deriva este cierre de imports por
# AST y lo contrasta con COPY: añadir un import local a `servicio.py` (también si vive
# dentro de una función, como el import diferido de búsqueda) sin añadir su módulo aquí
# rompe el gate antes de publicar una imagen que sólo falla al ejercer esa ruta.
COPY ledger_parse.py kind_registry.py observability.py search_contract.py search_cursor.py search_store.py telemetry_bridge.py servicio.py ui.html atestigua_artefacto.py /app/
# C4-prep INERTE: empaqueta el composition root nativo y su frontera de
# autoridad, pero no cambia el CMD. Esto permite probar la imagen candidata sin
# activar el runtime ni la politica native-required por accidente.
COPY coordination.py native_gateway.py operator_admission.py runtime_root.py fleet_deadline_runner.py /app/
# EL CIERRE DEL RUNNER, COMPLETO — y `projector.py` FALTABA.
# `projector_runner.py` viajaba solo, y su cierre de imports es EL MISMO FICHERO
# (0 imports locales: hoy es el Null Object `disabled`), asi que el gate del cierre
# salia verde sin mirar al proyector real. `projector.py` —el que materializa el
# outbox al ledger, ejercitado por `tests/projector` en CI— NO estaba en ningun
# COPY: la imagen no podia importarlo. Cerrarlo AHORA, con el runner inerte, es lo
# que evita descubrirlo el dia que se active un modo distinto de `disabled`.
# Su cierre local es {coordination, observability, telemetry_bridge}, ya presentes.
COPY projector.py projector_runner.py journal_projection_backend.py /app/
COPY LICENSE NOTICE THIRD_PARTY_NOTICES.md /usr/share/doc/llminbox/
COPY --from=notices /third_party/ /usr/share/doc/llminbox/third-party/
# La interfaz compilada en la etapa anterior. `ui.html` se conserva como
# respaldo sin build — si algún día la etapa de Node falla, el servicio sigue
# sirviendo una interfaz usable en vez de una página en blanco.
COPY --from=web /static /app/static

# No corre como root: el bind-mount de los ledgers es de escritura y un servicio
# que puede reescribir el canon de la flota no necesita además ser root.
# EL ARTEFACTO SE ATESTIGUA A SÍ MISMO, en el build y no en el despliegue: versión de
# SQLite, VFS por defecto MEDIDO (o declarado no medible), FTS5 por efecto, opciones de
# compilación, versiones instaladas y digest del lock. Queda `0444` — nadie lo reescribe
# en caliente, así que lo que `/health` publique describe la imagen, no el turno.
RUN python3 /app/atestigua_artefacto.py /app/ARTEFACTO.json

# Sólo el volumen reconstruible es escribible. ``0444`` no protege un fichero
# si su directorio pertenece al proceso: podría hacer unlink/rename y publicar
# después un ARTEFACTO.json falso desde `/health`. `/app` permanece root:root.
RUN useradd -u 1000 -m llmi && mkdir -p /data && chown llmi /data
USER llmi

# Falsador EN LA IMAGEN, bajo la identidad que va a servir tráfico. Si un COPY
# futuro usa --chown, o alguien vuelve a entregar /app al runtime, el build se
# pone rojo antes de producir una candidata mutable.
RUN test -w /data \
    && test ! -w /app \
    && test ! -w /app/ARTEFACTO.json \
    && test ! -w /app/projector.py \
    && touch /tmp/mv-control-source \
    && mv /tmp/mv-control-source /tmp/mv-control-destination \
    && test -f /tmp/mv-control-destination \
    && touch /tmp/forged-artefacto /tmp/forged-projector \
    && ! mv -f /tmp/forged-artefacto /app/ARTEFACTO.json \
    && ! mv -f /tmp/forged-projector /app/projector.py \
    && test -f /tmp/forged-artefacto \
    && test -f /tmp/forged-projector \
    && rm -f /tmp/mv-control-destination \
        /tmp/forged-artefacto /tmp/forged-projector

EXPOSE 8077
HEALTHCHECK --interval=30s --timeout=3s --start-period=40s \
  CMD python3 -c "import json,urllib.request,sys; d=json.load(urllib.request.urlopen('http://127.0.0.1:8077/health',timeout=2)); sys.exit(0 if d.get('ok') else 1)"

CMD ["uvicorn", "servicio:app", "--host", "0.0.0.0", "--port", "8077", "--log-level", "warning"]
