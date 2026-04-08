#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# detect-headers.sh — Auto-detect which HTTP headers an HLS stream requires
#
# Usage:
#   ./detect-headers.sh "https://streaming-site.com/channel.php"
#   ./detect-headers.sh "https://example.com/hls/stream.m3u8?token=xxx" --direct
#
# The script will:
#   1. Follow the iframe chain to find the m3u8 URL
#   2. Test header combinations (User-Agent, Referer, Origin)
#   3. Test .ts segment access with the same combos
#   4. Print the exact env vars you need for hls-proxy.py
# =============================================================================

URL="${1:-}"
MODE="${2:-}"
UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

if [[ -z "$URL" ]]; then
  echo "Usage: $0 <page_url> [--direct]"
  echo ""
  echo "  <page_url>   URL of the streaming page or direct m3u8 URL"
  echo "  --direct     Skip iframe extraction, treat URL as m3u8 directly"
  exit 1
fi

echo -e "${BOLD}=== HLS Header Detector ===${NC}"
echo ""

# --- Step 1: Extract m3u8 URL ---
M3U8_URL=""
IFRAME_URL=""
EMBED_HOST=""
PAGE_HOST=$(echo "$URL" | grep -oP 'https?://[^/]+')

if [[ "$MODE" == "--direct" ]]; then
  echo -e "${CYAN}[1/4]${NC} Using direct m3u8 URL"
  M3U8_URL="$URL"
else
  echo -e "${CYAN}[1/4]${NC} Extracting iframe from page..."
  IFRAME_URL=$(curl -sL --max-time 15 -A "$UA" "$URL" \
    | grep -oP 'iframe\s+src="\K[^"]+' | head -1)

  if [[ -z "$IFRAME_URL" ]]; then
    echo -e "${YELLOW}  No iframe found. Checking page directly for m3u8...${NC}"
    M3U8_URL=$(curl -sL --max-time 15 -A "$UA" "$URL" \
      | grep -oP "https?://[^\"\x27\s]+\.m3u8[^\"\x27\s]*" | head -1)
  else
    EMBED_HOST=$(echo "$IFRAME_URL" | grep -oP 'https?://[^/]+')
    echo -e "  iframe: ${BOLD}$IFRAME_URL${NC}"
    echo -e "  embed host: ${BOLD}$EMBED_HOST${NC}"
    echo ""
    echo -e "${CYAN}[1/4]${NC} Extracting m3u8 from embed page..."
    M3U8_URL=$(curl -sL --max-time 15 -A "$UA" -e "${PAGE_HOST}/" "$IFRAME_URL" \
      | grep -oP "https?://[^\"\x27\s]+\.m3u8[^\"\x27\s]*" | head -1)
  fi
fi

if [[ -z "$M3U8_URL" ]]; then
  echo -e "${RED}  FAILED: Could not find m3u8 URL${NC}"
  echo "  The site may use JavaScript-only loading. Try browser DevTools instead."
  exit 1
fi

M3U8_HOST=$(echo "$M3U8_URL" | grep -oP 'https?://[^/]+')
M3U8_BASE=$(echo "$M3U8_URL" | sed 's|/[^/]*$|/|')
echo -e "  m3u8: ${BOLD}${M3U8_URL:0:100}...${NC}"
echo ""

# --- Step 2: Test header combos on m3u8 ---
echo -e "${CYAN}[2/4]${NC} Testing header combinations on m3u8 playlist..."
echo ""

# Build candidate referers
REFERERS=("")
[[ -n "$EMBED_HOST" ]] && REFERERS+=("${EMBED_HOST}/")
[[ -n "$PAGE_HOST" && "$PAGE_HOST" != "$EMBED_HOST" ]] && REFERERS+=("${PAGE_HOST}/")
[[ -n "$M3U8_HOST" && "$M3U8_HOST" != "$EMBED_HOST" && "$M3U8_HOST" != "$PAGE_HOST" ]] && REFERERS+=("${M3U8_HOST}/")

declare -A M3U8_RESULTS=()

for ua_flag in "none" "chrome"; do
  for ref in "${REFERERS[@]}"; do
    ARGS=(-sL --max-time 10 -o /dev/null -w "%{http_code}")
    LABEL=""

    if [[ "$ua_flag" == "chrome" ]]; then
      ARGS+=(-A "$UA")
      LABEL="UA"
    else
      LABEL="no-UA"
    fi

    if [[ -n "$ref" ]]; then
      ARGS+=(-e "$ref")
      LABEL+="+Ref(${ref})"
    fi

    CODE=$(curl "${ARGS[@]}" "$M3U8_URL" 2>/dev/null || echo "000")

    if [[ "$CODE" == "200" ]]; then
      echo -e "  ${GREEN}$CODE${NC}  $LABEL"
      M3U8_RESULTS["$LABEL"]="$CODE"
    else
      echo -e "  ${RED}$CODE${NC}  $LABEL"
    fi
  done
done

echo ""

# --- Step 3: Get a .ts segment and test ---
echo -e "${CYAN}[3/4]${NC} Testing header combinations on .ts segments..."
echo ""

# Fetch playlist with best known working combo
BEST_UA=""
BEST_REF=""
for ref in "${REFERERS[@]}"; do
  PLAYLIST=$(curl -sL --max-time 10 -A "$UA" ${ref:+-e "$ref"} "$M3U8_URL" 2>/dev/null)
  if echo "$PLAYLIST" | grep -q "#EXTM3U"; then
    BEST_UA="$UA"
    BEST_REF="$ref"
    break
  fi
done

TS_LINE=$(echo "$PLAYLIST" | grep -v "^#" | grep -v "^$" | head -1)

if [[ -z "$TS_LINE" ]]; then
  echo -e "${YELLOW}  Could not extract .ts segment from playlist${NC}"
else
  if [[ "$TS_LINE" == http* ]]; then
    TS_URL="$TS_LINE"
  else
    TS_URL="${M3U8_BASE}${TS_LINE}"
  fi
  echo -e "  segment: ${BOLD}${TS_URL:0:90}...${NC}"
  echo ""

  for ua_flag in "none" "chrome"; do
    for ref in "${REFERERS[@]}"; do
      ARGS=(-sL --max-time 10 -o /dev/null -w "%{http_code}")
      LABEL=""

      if [[ "$ua_flag" == "chrome" ]]; then
        ARGS+=(-A "$UA")
        LABEL="UA"
      else
        LABEL="no-UA"
      fi

      if [[ -n "$ref" ]]; then
        ARGS+=(-e "$ref")
        LABEL+="+Ref(${ref})"
      fi

      CODE=$(curl "${ARGS[@]}" "$TS_URL" 2>/dev/null || echo "000")

      if [[ "$CODE" == "200" ]]; then
        echo -e "  ${GREEN}$CODE${NC}  $LABEL"
      elif [[ "$CODE" == "404" ]]; then
        # 404 on .ts = headers accepted but segment rotated (live stream)
        echo -e "  ${YELLOW}$CODE${NC}  $LABEL  (headers OK — segment rotated)"
      else
        echo -e "  ${RED}$CODE${NC}  $LABEL"
      fi
    done
  done
fi

echo ""

# --- Step 4: Recommendation ---
echo -e "${CYAN}[4/4]${NC} ${BOLD}Recommended configuration:${NC}"
echo ""

# Find minimal working combo (prefer least headers)
NEED_UA=false
NEED_REF=""

# Check if no headers works
CODE_NONE=$(curl -sL --max-time 10 -o /dev/null -w "%{http_code}" "$M3U8_URL" 2>/dev/null || echo "000")
if [[ "$CODE_NONE" == "200" ]]; then
  echo -e "  ${GREEN}No special headers needed! Stream is open.${NC}"
  echo ""
  echo "  export HLS_PROXY_REFERER=\"\""
  exit 0
fi

# Check UA only
CODE_UA=$(curl -sL --max-time 10 -o /dev/null -w "%{http_code}" -A "$UA" "$M3U8_URL" 2>/dev/null || echo "000")
if [[ "$CODE_UA" == "200" ]]; then
  NEED_UA=true
  # Check if segments also work with UA only
  if [[ -n "${TS_URL:-}" ]]; then
    CODE_TS=$(curl -sL --max-time 10 -o /dev/null -w "%{http_code}" -A "$UA" "$TS_URL" 2>/dev/null || echo "000")
    if [[ "$CODE_TS" == "200" || "$CODE_TS" == "404" ]]; then
      echo -e "  ${GREEN}User-Agent only — no Referer needed${NC}"
      echo ""
      echo "  export HLS_PROXY_REFERER=\"\""
      exit 0
    fi
  else
    echo -e "  ${GREEN}User-Agent only — no Referer needed${NC}"
    echo ""
    echo "  export HLS_PROXY_REFERER=\"\""
    exit 0
  fi
fi

# Check UA + each referer
for ref in "${REFERERS[@]}"; do
  [[ -z "$ref" ]] && continue
  CODE=$(curl -sL --max-time 10 -o /dev/null -w "%{http_code}" -A "$UA" -e "$ref" "$M3U8_URL" 2>/dev/null || echo "000")
  if [[ "$CODE" == "200" ]]; then
    # Also check segments (404 = headers accepted, segment rotated on live stream)
    if [[ -n "${TS_URL:-}" ]]; then
      CODE_TS=$(curl -sL --max-time 10 -o /dev/null -w "%{http_code}" -A "$UA" -e "$ref" "$TS_URL" 2>/dev/null || echo "000")
      if [[ "$CODE_TS" == "200" || "$CODE_TS" == "404" ]]; then
        NEED_REF="$ref"
        break
      fi
    else
      NEED_REF="$ref"
      break
    fi
  fi
done

if [[ -n "$NEED_REF" ]]; then
  echo -e "  ${GREEN}User-Agent + Referer${NC}"
  echo ""
  echo -e "  ${BOLD}Copy these to start the proxy:${NC}"
  echo ""
  echo "  export HLS_PROXY_REFERER=\"$NEED_REF\""
else
  echo -e "  ${RED}Could not find a working header combination automatically.${NC}"
  echo "  Try browser DevTools (F12 → Network → filter m3u8) to inspect headers manually."
fi

echo ""
echo -e "  ${BOLD}m3u8 URL:${NC} $M3U8_URL"
[[ -n "$EMBED_HOST" ]] && echo -e "  ${BOLD}Embed host:${NC} $EMBED_HOST"
