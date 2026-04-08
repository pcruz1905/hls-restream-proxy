#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# refresh-m3u.sh — Scrape live m3u8 URLs and write a Jellyfin-ready M3U playlist
#
# How it works:
#   1. For each channel, fetches the source page and extracts an iframe src
#   2. Fetches the iframe/embed page and extracts the m3u8 URL
#   3. Wraps URLs through the local HLS proxy so the media player gets
#      proper headers (User-Agent, Referer)
#   4. Writes the M3U file with channel metadata (name, logo, group)
#
# Configure channels in channels.conf (see channels.conf.example).
# Run on a cron/timer to keep tokens fresh.
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF_FILE="${CHANNELS_CONF:-${SCRIPT_DIR}/channels.conf}"

# --- Defaults (override via environment) ---
M3U_FILE="${M3U_OUTPUT:-/tmp/iptv.m3u}"
PROXY_HOST="${HLS_PROXY_URL:-http://127.0.0.1:8089}"
UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
LOG_TAG="[refresh-m3u]"

# --- Load channel list ---
if [[ ! -f "$CONF_FILE" ]]; then
  echo "$LOG_TAG ERROR: Channel config not found: $CONF_FILE" >&2
  echo "$LOG_TAG Copy channels.conf.example to channels.conf and edit it." >&2
  exit 1
fi

# --- Extract m3u8 from a source page ---
# Supports two extraction modes:
#   iframe  — page contains <iframe src="...">, embed page contains m3u8
#   direct  — page itself contains the m3u8 URL
extract_m3u8() {
  local page_url="$1"
  local mode="${2:-iframe}"
  local referer="${3:-}"

  if [[ "$mode" == "iframe" ]]; then
    local iframe
    iframe=$(curl -sL --max-time 15 -A "$UA" "$page_url" \
      | grep -oP 'iframe\s+src="\K[^"]+' | head -1)

    [[ -z "$iframe" ]] && return 1

    local ref="${referer:-$(echo "$page_url" | grep -oP 'https?://[^/]+')}"
    curl -sL --max-time 15 -A "$UA" -e "${ref}/" "$iframe" \
      | grep -oP "https?://[^\"\x27\s]+\.m3u8[^\"\x27\s]*" | head -1

  elif [[ "$mode" == "direct" ]]; then
    curl -sL --max-time 15 -A "$UA" "$page_url" \
      | grep -oP "https?://[^\"\x27\s]+\.m3u8[^\"\x27\s]*" | head -1
  fi
}

# --- Main ---
echo "$LOG_TAG Starting refresh at $(date)"

OUTPUT="#EXTM3U"
SUCCESS=0
FAIL=0

while IFS='|' read -r slug name chno logo group source_url mode referer; do
  # Skip comments and blank lines
  [[ -z "$slug" || "$slug" == \#* ]] && continue

  mode="${mode:-iframe}"
  referer="${referer:-}"
  echo -n "$LOG_TAG $name ($slug)... "

  m3u8=$(extract_m3u8 "$source_url" "$mode" "$referer" 2>&1) && rc=0 || rc=$?

  if [[ $rc -eq 0 && -n "$m3u8" && "$m3u8" == http* ]]; then
    echo "OK"
    encoded=$(python3 -c "import urllib.parse; print(urllib.parse.quote('${m3u8}', safe=''))")
    OUTPUT+=$'\n\n'"#EXTINF:-1 tvg-id=\"${slug}\" tvg-chno=\"${chno}\" tvg-name=\"${name}\" tvg-logo=\"${logo}\" group-title=\"${group}\",${name}"
    OUTPUT+=$'\n'"${PROXY_HOST}/proxy?url=${encoded}"
    SUCCESS=$((SUCCESS + 1))
  else
    echo "FAILED"
    FAIL=$((FAIL + 1))
  fi

  sleep 1
done < "$CONF_FILE"

echo "$LOG_TAG Results: $SUCCESS OK, $FAIL failed"

if [[ $SUCCESS -eq 0 ]]; then
  echo "$LOG_TAG ERROR: All channels failed, keeping old m3u file" >&2
  exit 1
fi

echo "$OUTPUT" > "$M3U_FILE"
echo "$LOG_TAG Written $M3U_FILE with $SUCCESS channels"
echo "$LOG_TAG Done at $(date)"
