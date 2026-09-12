#!/usr/bin/env bash
# Falsador barato del cableado `llmi post` -> role-contract. Sin red, DB ni Docker.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/home"
: > "$T/ledger.md"
printf '{"test":"%s"}\n' "$T/ledger.md" > "$T/mounts.json"

cat > "$T/roster.json" <<'EOF'
{"agentes":[{"nombre":"backend","rol":"be"},{"nombre":"qa","rol":"qa"}],"difusion":["FLOTA"]}
EOF
cat > "$T/be.yaml" <<'EOF'
id: be
communication:
  broadcast: false
  permitido: [DECISION_REQUEST, ESCALATION, CONSULT, HUMAN_INPUT_REQUEST, CRITICAL_ALERT]
  deprecado: [ACK, INGESTED, FYI_rutina, HEARTBEAT, STATUS, DELTA]
EOF
cat > "$T/be-broadcast.yaml" <<'EOF'
id: be
communication:
  broadcast: true
  permitido: [DECISION_REQUEST, ESCALATION, CONSULT, HUMAN_INPUT_REQUEST, CRITICAL_ALERT]
  deprecado: [ACK, INGESTED, FYI_rutina, HEARTBEAT, STATUS, DELTA]
EOF
cat > "$T/be-duplicate.yaml" <<'EOF'
id: be
communication:
  broadcast: false
  permitido: [CONSULT]
  deprecado: [ACK]
communication:
  broadcast: false
  permitido: [CONSULT]
  deprecado: [ACK]
EOF
cat > "$T/be-nested.yaml" <<'EOF'
id: be
communication:
  broadcast: false
  policy:
    permitido: [CONSULT]
  deprecado: [ACK]
EOF

run_post() {
  printf 'cuerpo\n' | env -u TMUX HOME="$T/home" LLMINBOX_TOKEN=test-token \
    LLMINBOX_SESSION_FILE="$T/no-session" LLMINBOX_ROSTER="$T/roster.json" \
    LLMINBOX_MESSAGE_POLICY="$1" LLMINBOX_ROLE_CONTRACT="$T/be.yaml" \
    "$ROOT/llmi" post backend qa "$2" titulo --dry-run 2>&1
}

fail=0
out="$(printf 'cuerpo\n' | env -u TMUX HOME="$T/home" LLMINBOX_TOKEN=test-token \
  LLMINBOX_SESSION_FILE="$T/no-session" LLMINBOX_MESSAGE_POLICY=off \
  "$ROOT/llmi" post backend qa ACK titulo --dry-run 2>&1)"; rc=$?
if [ "$rc" = 0 ] && grep -q 'simulacro' <<<"$out" \
                  && ! grep -q 'MESSAGE_POLICY' <<<"$out"; then
  echo '✓ off conserva el camino v0.9 sin exigir configuracion'
else
  echo "✗ off cambio el camino v0.9 (rc=$rc): $out"; fail=$((fail + 1))
fi

out="$(run_post enforce ACK)"; rc=$?
if [ "$rc" = 1 ] && grep -q 'MESSAGE_KIND_UNKNOWN' <<<"$out" \
                   && grep -q 'NO se ha enviado ni escrito nada' <<<"$out"; then
  echo '✓ enforce rechaza el tipo legacy antes de emitir'
else
  echo "✗ enforce dejo pasar ACK (rc=$rc): $out"; fail=$((fail + 1))
fi

out="$(run_post enforce CONSULT)"; rc=$?
if [ "$rc" = 0 ] && grep -q 'simulacro' <<<"$out" && grep -q 'tipo: CONSULT' <<<"$out"; then
  echo '✓ enforce acepta el acto permitido y llega al dry-run'
else
  echo "✗ enforce rechazo CONSULT (rc=$rc): $out"; fail=$((fail + 1))
fi

out="$(run_post advisory ACK)"; rc=$?
if [ "$rc" = 0 ] && grep -q 'MESSAGE_POLICY_ADVISORY' <<<"$out" \
                   && grep -q 'simulacro' <<<"$out"; then
  echo '✓ advisory avisa pero no bloquea'
else
  echo "✗ advisory no cumple (rc=$rc): $out"; fail=$((fail + 1))
fi

out="$(run_post typo CONSULT)"; rc=$?
if [ "$rc" = 1 ] && grep -q 'MESSAGE_POLICY_CONFIG_INVALID' <<<"$out"; then
  echo '✓ un modo mal escrito falla cerrado'
else
  echo "✗ el modo invalido no fallo cerrado (rc=$rc): $out"; fail=$((fail + 1))
fi

out="$(printf 'cuerpo\n' | env -u TMUX HOME="$T/home" LLMINBOX_TOKEN=test-token \
  LLMINBOX_SESSION_FILE="$T/no-session" LLMINBOX_ROSTER="$T/roster.json" \
  LLMINBOX_MESSAGE_POLICY=enforce LLMINBOX_ROLE_CONTRACT="$T/be.yaml" \
  "$ROOT/llmi" post backend FLOTA CRITICAL_ALERT titulo --dry-run 2>&1)"; rc=$?
if [ "$rc" = 1 ] && grep -q 'MESSAGE_BROADCAST_DENIED' <<<"$out"; then
  echo '✓ broadcast:false rechaza un grupo real del roster'
else
  echo "✗ broadcast:false dejo pasar FLOTA (rc=$rc): $out"; fail=$((fail + 1))
fi

out="$(printf 'cuerpo\n' | env -u TMUX HOME="$T/home" LLMINBOX_TOKEN=test-token \
  LLMINBOX_SESSION_FILE="$T/no-session" LLMINBOX_ROSTER="$T/roster.json" \
  LLMINBOX_MESSAGE_POLICY=enforce LLMINBOX_ROLE_CONTRACT="$T/be-broadcast.yaml" \
  "$ROOT/llmi" post backend FLOTA CRITICAL_ALERT titulo --dry-run 2>&1)"; rc=$?
if [ "$rc" = 0 ] && grep -q 'simulacro' <<<"$out"; then
  echo '✓ broadcast:true permite el mismo grupo real'
else
  echo "✗ broadcast:true rechazo FLOTA (rc=$rc): $out"; fail=$((fail + 1))
fi

for malformed in duplicate nested; do
  out="$(printf 'cuerpo\n' | env -u TMUX HOME="$T/home" LLMINBOX_TOKEN=test-token \
    LLMINBOX_SESSION_FILE="$T/no-session" LLMINBOX_ROSTER="$T/roster.json" \
    LLMINBOX_MESSAGE_POLICY=enforce LLMINBOX_ROLE_CONTRACT="$T/be-$malformed.yaml" \
    "$ROOT/llmi" post backend qa CONSULT titulo --dry-run 2>&1)"; rc=$?
  if [ "$rc" = 1 ] && grep -q 'MESSAGE_POLICY_UNAVAILABLE' <<<"$out"; then
    echo "✓ contrato $malformed falla cerrado"
  else
    echo "✗ contrato $malformed paso (rc=$rc): $out"; fail=$((fail + 1))
  fi
done

out="$(printf 'cuerpo durable\n' | env -u TMUX HOME="$T/home" LLMINBOX_TOKEN=test-token \
  LLMINBOX_SESSION_FILE="$T/no-session" LLMINBOX_ROSTER="$T/roster.json" \
  LLMINBOX_MESSAGE_POLICY=enforce LLMINBOX_ROLE_CONTRACT="$T/be.yaml" \
  LLMI_LEDGER=test LLMI_MOUNTS="$T/mounts.json" \
  "$ROOT/llmi" post backend qa CONSULT titulo --local 2>&1)"; rc=$?
if [ "$rc" = 0 ] && grep -q 'publicado en test' <<<"$out" \
                  && grep -q '· CONSULT]' "$T/ledger.md" \
                  && T_LEDGER="$T/ledger.md" ROOT="$ROOT" \
                     LLMINBOX_ROSTER="$T/roster.json" python3 - <<'PY'
import os, sys
sys.path.insert(0, os.environ["ROOT"])
import kind_registry as kr
import ledger_parse as lp
entry = lp.parse(os.environ["T_LEDGER"])[0][0]
assert entry.tipo is None
assert entry.raw_tipo == "CONSULT"
assert kr.materialize(entry.raw_tipo) == ("CONSULT", 1)
PY
then
  echo '✓ publicación real conserva CONSULT fuera de tipo y la materializa como r1'
else
  echo "✗ publicación Agent OS no preserva/materializa (rc=$rc): $out"; fail=$((fail + 1))
fi

# Ruta nativa: la politica usa /whoami y el wire lleva la pareja semantica, no
# el slot legacy. `curl` es el falso determinista de la suite, no hay red.
mkdir -p "$T/bin"
cp "$ROOT/tests/falso/curl" "$T/bin/curl"; chmod +x "$T/bin/curl"
cat > "$T/native.json" <<'EOF'
{"version":1,"lanes":{"demo":{"post":"native-required"}}}
EOF
cat > "$T/native-optional.json" <<'EOF'
{"version":1,"lanes":{"demo":{"post":"native"}}}
EOF
cat > "$T/session.json" <<'EOF'
{"token":"session-token","runtime_instance":"rti-1","expires_at":9999999999,"generation":1}
EOF
chmod 600 "$T/session.json"; : > "$T/native.log"
out="$(printf 'native body\n' | env -u TMUX PATH="$T/bin:$PATH" HOME="$T/home" \
  BIK_CARRIL=demo LLMINBOX_TOKEN=test-token LLMINBOX_SESSION_FILE="$T/session.json" \
  LLMINBOX_NATIVE_POLICY="$T/native.json" LLMINBOX_ROSTER="$T/roster.json" \
  LLMINBOX_MESSAGE_POLICY=enforce LLMINBOX_ROLE_CONTRACT="$T/be.yaml" \
  FK_LOG="$T/native.log" FK_HTTP=202 \
  FK_BODY='{"event_id":"evt-1","receipt_id":"rcp-1","replayed":false}' \
  FK_WHOAMI_HTTP=200 \
  FK_WHOAMI_BODY='{"principal":"backend-demo","role":"be","lane":"demo"}' \
  "$ROOT/llmi" post alguien-tecleado qa CONSULT titulo 2>&1)"; rc=$?
if [ "$rc" = 0 ] \
   && grep -q '/whoami M:GET' "$T/native.log" \
   && grep -q '/events M:POST' "$T/native.log" \
   && grep -q '"canonical_kind":"CONSULT"' "$T/native.log" \
   && grep -q '"kind_registry_rev":1' "$T/native.log" \
   && ! grep -q '"kind":"CONSULT"' "$T/native.log"; then
  echo '✓ nativo usa principal servidor y publica la pareja semantica explicita'
else
  echo "✗ nativo no transporto la pareja/identidad correcta (rc=$rc): $out"; fail=$((fail + 1))
  sed -n '1,8p' "$T/native.log"
fi

# Una clave explícita es literal: el canonicalizado nativo sólo sustituye las
# claves automáticas, nunca una decisión de replay del operador.
: > "$T/native-explicit.log"
out="$(printf 'native body\n' | env -u TMUX PATH="$T/bin:$PATH" HOME="$T/home" \
  BIK_CARRIL=demo LLMINBOX_TOKEN=test-token LLMINBOX_SESSION_FILE="$T/session.json" \
  LLMINBOX_NATIVE_POLICY="$T/native.json" LLMINBOX_ROSTER="$T/roster.json" \
  LLMINBOX_MESSAGE_POLICY=enforce LLMINBOX_ROLE_CONTRACT="$T/be.yaml" \
  FK_LOG="$T/native-explicit.log" FK_HTTP=202 \
  FK_BODY='{"event_id":"evt-key","receipt_id":"rcp-key","replayed":false}' \
  FK_WHOAMI_HTTP=200 \
  FK_WHOAMI_BODY='{"principal":"backend-demo","role":"be","lane":"demo"}' \
  "$ROOT/llmi" post alguien qa consult titulo --key literal-key 2>&1)"; rc=$?
if [ "$rc" = 0 ] && grep -q '/events .*Idempotency-Key: literal-key' \
                         "$T/native-explicit.log"; then
  echo '✓ --key nativa viaja literal y no se recalcula'
else
  echo "✗ --key nativa fue alterada (rc=$rc): $out"; fail=$((fail + 1))
fi

# `native` permite el escape explícito --local (native-required no). Debe conservar
# exactamente el bridge y no rozar /events, incluso con una key suministrada.
: > "$T/native-local.log"
out="$(printf 'bridge override body\n' | env -u TMUX PATH="$T/bin:$PATH" HOME="$T/home" \
  BIK_CARRIL=demo LLMINBOX_TOKEN=test-token LLMINBOX_SESSION_FILE="$T/session.json" \
  LLMINBOX_NATIVE_POLICY="$T/native-optional.json" LLMINBOX_ROSTER="$T/roster.json" \
  LLMINBOX_MESSAGE_POLICY=enforce LLMINBOX_ROLE_CONTRACT="$T/be.yaml" \
  LLMI_LEDGER=test LLMI_MOUNTS="$T/mounts.json" FK_LOG="$T/native-local.log" \
  "$ROOT/llmi" post backend qa CONSULT bridge-override --local \
  --key bridge-literal 2>&1)"; rc=$?
if [ "$rc" = 0 ] && grep -q 'publicado en test' <<<"$out" \
                  && grep -q 'bridge-override' "$T/ledger.md" \
                  && ! grep -q '/events ' "$T/native-local.log"; then
  echo '✓ native --local conserva bridge y no emite /events'
else
  echo "✗ native --local rompió el bridge (rc=$rc): $out"; fail=$((fail + 1))
fi

: > "$T/native-2.log"
printf 'native body\n' | env -u TMUX PATH="$T/bin:$PATH" HOME="$T/home" \
  BIK_CARRIL=demo LLMINBOX_TOKEN=test-token LLMINBOX_SESSION_FILE="$T/session.json" \
  LLMINBOX_NATIVE_POLICY="$T/native.json" LLMINBOX_ROSTER="$T/roster.json" \
  LLMINBOX_MESSAGE_POLICY=enforce LLMINBOX_ROLE_CONTRACT="$T/be.yaml" \
  FK_LOG="$T/native-2.log" FK_HTTP=202 \
  FK_BODY='{"event_id":"evt-1","receipt_id":"rcp-1","replayed":false}' \
  FK_WHOAMI_HTTP=200 \
  FK_WHOAMI_BODY='{"principal":"backend-demo","role":"be","lane":"demo"}' \
  "$ROOT/llmi" post otro-yo qa consult titulo >/dev/null 2>&1
: > "$T/native-3.log"
printf 'native body\n' | env -u TMUX PATH="$T/bin:$PATH" HOME="$T/home" \
  BIK_CARRIL=demo LLMINBOX_TOKEN=test-token LLMINBOX_SESSION_FILE="$T/session.json" \
  LLMINBOX_NATIVE_POLICY="$T/native.json" LLMINBOX_ROSTER="$T/roster.json" \
  LLMINBOX_MESSAGE_POLICY=enforce LLMINBOX_ROLE_CONTRACT="$T/be.yaml" \
  FK_LOG="$T/native-3.log" FK_HTTP=202 \
  FK_BODY='{"event_id":"evt-2","receipt_id":"rcp-2","replayed":false}' \
  FK_WHOAMI_HTTP=200 \
  FK_WHOAMI_BODY='{"principal":"backend-demo","role":"be","lane":"demo"}' \
  "$ROOT/llmi" post tercer-yo qa CONSULT titular-distinto >/dev/null 2>&1
: > "$T/native-4.log"
printf 'body distinto\n' | env -u TMUX PATH="$T/bin:$PATH" HOME="$T/home" \
  BIK_CARRIL=demo LLMINBOX_TOKEN=test-token LLMINBOX_SESSION_FILE="$T/session.json" \
  LLMINBOX_NATIVE_POLICY="$T/native.json" LLMINBOX_ROSTER="$T/roster.json" \
  LLMINBOX_MESSAGE_POLICY=enforce LLMINBOX_ROLE_CONTRACT="$T/be.yaml" \
  FK_LOG="$T/native-4.log" FK_HTTP=202 \
  FK_BODY='{"event_id":"evt-3","receipt_id":"rcp-3","replayed":false}' \
  FK_WHOAMI_HTTP=200 \
  FK_WHOAMI_BODY='{"principal":"backend-demo","role":"be","lane":"demo"}' \
  "$ROOT/llmi" post cuarto-yo qa CONSULT titulo >/dev/null 2>&1
if ROOT="$ROOT" python3 - "$T/native.log" "$T/native-2.log" "$T/native-3.log" "$T/native-4.log" <<'PY'
import re, sys
rows=[]
for path in sys.argv[1:]:
    event=next(line for line in open(path) if "/events " in line)
    rows.append((re.search(r"Idempotency-Key: ([^ ]+)", event).group(1),
                 event.split(" D:",1)[1].split(" CFG:",1)[0]))
assert rows[0] == rows[1], rows
assert rows[2][0] != rows[0][0] and rows[2][1] != rows[0][1], rows
assert rows[3][0] != rows[0][0] and rows[3][1] != rows[0][1], rows
PY
then
  echo '✓ clave nativa deriva del payload: ignora YO/case y cambia con titular/body'
else
  echo '✗ clave nativa aún deriva de identidad/lexema crudos'; fail=$((fail + 1))
fi

exit "$fail"
