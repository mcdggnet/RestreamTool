#!/usr/bin/env bash
set -e

BASE="http://localhost:8080"
PASS=0
FAIL=0

ok()   { echo "[PASS] $1"; PASS=$((PASS+1)); }
fail() { echo "[FAIL] $1"; FAIL=$((FAIL+1)); }

check() {
  local label="$1" expected="$2"
  shift 2
  local body
  body=$(curl -sf "$@") || { fail "$label (request failed)"; return; }
  if echo "$body" | grep -q "$expected"; then
    ok "$label"
  else
    fail "$label — expected '$expected' in: $body"
  fi
}

echo "=== Restream Tool Tests ==="
echo

# Root
check "GET /" "video_hls" "$BASE/"

# Status — no stream
check "GET /status (idle)" '"streaming":false' "$BASE/status"

# Set a stream
check "POST /stream" '"status":"ok"' \
  -X POST "$BASE/stream" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.twitch.tv/monstercat"}'

# Status — streaming
check "GET /status (streaming)" '"streaming":true' "$BASE/status"

# Wait a few seconds for HLS segments to appear
echo
echo "Waiting 8s for HLS segments to generate..."
sleep 8

# Check m3u8 files exist and are non-empty
for path in /hls/stream.m3u8 /audio/stream.m3u8; do
  label="$path exists"
  body=$(curl -sf "$BASE$path") || { fail "$label (request failed)"; continue; }
  if [ -n "$body" ]; then ok "$label"; else fail "$label (empty)"; fi
done

# Check a .ts segment is reachable (grab first segment name from playlist)
SEG=$(curl -sf "$BASE/hls/stream.m3u8" | grep '\.ts' | head -1)
if [ -n "$SEG" ]; then
  curl -sf -o /dev/null "$BASE/hls/$SEG" && ok "HLS .ts segment reachable ($SEG)" || fail "HLS .ts segment reachable ($SEG)"
else
  fail "No .ts segment found in playlist"
fi

# Clear stream
check "DELETE /stream" '"status":"ok"' -X DELETE "$BASE/stream"

# Status back to idle
check "GET /status (idle again)" '"streaming":false' "$BASE/status"

echo
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
