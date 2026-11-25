#!/usr/bin/env bash
# Smoke-test a built helper with a clean environment and a throwaway HOME.
# Never touches the real config, state, or Keychain secrets.
# Usage: smoke-helper.sh <path-to-helper-executable>
set -euo pipefail

HELPER="$1"
SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT

run() {
  local request="$1" expected_exit="$2"
  local output status=0
  output="$(printf '%s' "$request" | env -i HOME="$SANDBOX" PATH=/usr/bin:/bin \
    "$HELPER" --config "$SANDBOX/config.toml" rpc 2>"$SANDBOX/stderr")" || status=$?
  if [[ $status -ne $expected_exit ]]; then
    echo "FAIL exit $status (want $expected_exit) for $request" >&2
    cat "$SANDBOX/stderr" >&2
    exit 1
  fi
  /usr/bin/python3 -c 'import json,sys; r=json.loads(sys.argv[1]); assert r["protocol"]==1, r; print(json.dumps(r)[:240])' "$output"
}

run '{"protocol":1,"command":"hello"}' 0
run '{"protocol":1,"command":"status"}' 2
run 'garbage' 5
run '{"protocol":1,"command":"config.set","params":{"patch":{"bridge":{"username":"smoke","port":1143,"security_mode":"starttls"}}}}' 0
run '{"protocol":1,"command":"stats","params":{"days":7}}' 0
run '{"protocol":1,"command":"worker.control","params":{"action":"pause"}}' 0

hello="$(printf '{"protocol":1,"command":"hello"}' | env -i HOME="$SANDBOX" "$HELPER" rpc 2>/dev/null)"
/usr/bin/python3 -c '
import json, sys
r = json.loads(sys.argv[1])["result"]
assert r["frozen"] is True, r
assert r["keychain_backend"] == "keyring.backends.macOS", r
print("frozen helper", r["helper_version"], "python", r["python"])' "$hello"

# The CLI must work from the bundle too.
env -i HOME="$SANDBOX" "$HELPER" --help >/dev/null
echo "Smoke test passed."
