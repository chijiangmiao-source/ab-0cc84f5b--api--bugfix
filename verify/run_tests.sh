#!/usr/bin/env bash
# verify container entrypoint:
#   1. rule tests (exact rational engine)
#   2. build check (byte-compile all application sources)
#   3. HTTP smoke against the running reviewer service
# Exits non-zero (with a clear code) on any failure.
set -u

TARGET_URL="${TARGET_URL:-http://127.0.0.1:8080}"
FAIL=0

echo "== [1/3] rule tests =="
python -m pytest tests
RC=$?
if [ "$RC" -ne 0 ]; then
  echo "RULE TESTS FAILED ($RC)"
  FAIL=1
fi

echo "== [2/3] build check (byte compilation) =="
python -m compileall -q app verify
RC=$?
if [ "$RC" -ne 0 ]; then
  echo "BUILD CHECK FAILED ($RC)"
  FAIL=1
fi

echo "== [3/3] HTTP smoke against ${TARGET_URL} =="
python verify/smoke_http.py "$TARGET_URL"
RC=$?
if [ "$RC" -ne 0 ]; then
  echo "HTTP SMOKE FAILED ($RC)"
  FAIL=1
fi

if [ "$FAIL" -ne 0 ]; then
  echo "VERIFY: FAIL"
  exit 1
fi
echo "VERIFY: OK"
exit 0
