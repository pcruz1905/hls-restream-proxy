# hls-restream-proxy

Lightweight HLS restream toolkit for self-hosted media servers.

Many free IPTV/HLS sources require specific HTTP headers (User-Agent, Referer) that media servers don't send. This proxy sits between your media server and the upstream, injecting the required headers and rewriting m3u8 playlists so all segment requests also go through the proxy.

## Components

| File | Purpose |
|------|---------|
| `hls-proxy.py` | HTTP reverse proxy that adds headers to upstream HLS requests |
| `refresh-m3u.sh` | Scrapes source pages, extracts m3u8 URLs, writes M3U playlist |
| `detect-headers.sh` | Auto-detects which HTTP headers a stream requires |
| `channels.conf` | Your channel list (slug, name, logo, group, source URL) |

## How it works

```
┌──────────┐     ┌───────────┐     ┌──────────────┐     ┌──────────┐
│ Jellyfin │────▶│ hls-proxy │────▶│ upstream HLS │────▶│ segments │
│          │     │ :8089     │     │ server       │     │ (.ts)    │
└──────────┘     └───────────┘     └──────────────┘     └──────────┘
                  adds headers:
                  • User-Agent
                  • Referer
```

1. `refresh-m3u.sh` generates an M3U file with stable `/channel/<slug>` URLs pointing to the proxy
2. When Jellyfin requests `/channel/sporttv1`, the proxy scrapes a fresh m3u8 URL on the fly (cached for 1 hour)
3. The proxy injects the required headers and rewrites the playlist so `.ts` segments also go through it
4. The proxy auto-learns the correct Referer for each upstream host — no manual configuration needed

**No more expired tokens** — the M3U URLs never change, the proxy handles token refresh transparently.

## Requirements

- Python 3.8+ (stdlib only, no pip packages)
- bash, curl, grep (with PCRE / `-P`)

## Quick start

### Docker (recommended)

```bash
git clone https://github.com/pcruz1905/hls-restream-proxy.git
cd hls-restream-proxy

cp channels.conf.example channels.conf
# Edit channels.conf with your channels

docker compose -f docker-compose.example.yml up -d
```

### Docker one-liner (no clone)

```bash
docker run -d --name hls-proxy \
  -p 8089:8089 \
  -v ./channels.conf:/app/channels.conf:ro \
  ghcr.io/pcruz1905/hls-restream-proxy:latest
```

### Manual (no Docker)

```bash
git clone https://github.com/pcruz1905/hls-restream-proxy.git
cd hls-restream-proxy

cp channels.conf.example channels.conf
# Edit channels.conf with your channels

python3 hls-proxy.py &

# Generate the M3U
export M3U_OUTPUT=/path/to/jellyfin/config/iptv.m3u
export HLS_PROXY_URL="http://YOUR_HOST_IP:8089"
bash refresh-m3u.sh

# Add the M3U file as an M3U Tuner in your media server
```

## Channel config format

`channels.conf` — one channel per line, pipe-delimited:

```
slug|Display Name|chno|logo_url|Group|source_page_url|mode|referer
```

- **mode**: `iframe` (default) — page has an iframe whose embed contains the m3u8. `direct` — page itself contains the m3u8 URL.
- **referer**: optional override for the Referer header when fetching the embed page.

See `channels.conf.example` for details.

## Systemd setup

User-level services are provided in `systemd/`:

```bash
# Copy units
cp systemd/*.service systemd/*.timer ~/.config/systemd/user/

# Edit paths in the service files, then:
systemctl --user daemon-reload
systemctl --user enable --now hls-proxy.service
systemctl --user enable --now refresh-m3u.timer

# Enable linger so services run without an active login session
sudo loginctl enable-linger $USER
```

The timer refreshes URLs every 4 hours by default (edit `OnUnitActiveSec` in the timer).

## Docker / container media servers

If your media server runs in Docker on a bridge network, the proxy URL must use the Docker gateway IP (not `127.0.0.1`). Find it with:

```bash
docker inspect <container> | grep Gateway
# Typically 172.x.0.1
```

Then set `HLS_PROXY_URL=http://172.x.0.1:8089`.

## Finding the required headers (User-Agent & Referer)

Every streaming site is different. Here's how to figure out which headers your upstream needs:

### Automatic detection (recommended)

Run the detector tool — it follows the iframe chain, tests every header combination on both the m3u8 playlist and .ts segments, and tells you exactly what you need:

```bash
./detect-headers.sh "https://streaming-site.com/channel.php"
```

Output:
```
=== HLS Header Detector ===

[1/4] Extracting iframe from page...
  iframe: https://embed-domain.com/embed/abc123
  embed host: https://embed-domain.com

[1/4] Extracting m3u8 from embed page...
  m3u8: https://cdn.example.com/hls/abc123.m3u8?s=token&e=123

[2/4] Testing header combinations on m3u8 playlist...
  403  no-UA
  200  UA
  200  UA+Ref(https://embed-domain.com/)

[3/4] Testing header combinations on .ts segments...
  403  no-UA
  403  UA
  200  UA+Ref(https://embed-domain.com/)

[4/4] Recommended configuration:
  User-Agent + Referer

  export HLS_PROXY_REFERER="https://embed-domain.com/"
```

You can also pass a direct m3u8 URL:
```bash
./detect-headers.sh "https://cdn.example.com/hls/stream.m3u8?token=xxx" --direct
```

### Manual detection

If the auto-detector can't find the m3u8 (some sites use heavy JS), use browser DevTools:

### Step 1: Open DevTools Network tab

1. Open the streaming site in Chrome/Firefox
2. Press `F12` → **Network** tab
3. Filter by `m3u8` or `media`
4. Play the stream — you'll see `.m3u8` and `.ts` requests appear

### Step 2: Find the m3u8 request

Click on the `.m3u8` request and look at the **Request Headers**. Note down:
- **User-Agent** — usually a standard browser UA
- **Referer** — this is the key one, usually the embed/iframe domain (not the main site)
- **Origin** — sometimes needed instead of Referer

### Step 3: Test with curl

```bash
# Get a fresh m3u8 URL from the Network tab, then test:

# Without headers (probably 403)
curl -o /dev/null -w "%{http_code}" "https://example.com/hls/stream.m3u8?token=xxx"

# With User-Agent only
curl -o /dev/null -w "%{http_code}" -A "Mozilla/5.0" "https://example.com/hls/stream.m3u8?token=xxx"

# With User-Agent + Referer
curl -o /dev/null -w "%{http_code}" -A "Mozilla/5.0" \
  -e "https://embed-domain.com/" \
  "https://example.com/hls/stream.m3u8?token=xxx"
```

Try each combination until you get `200`. That tells you which headers are required.

### Step 4: Test .ts segments too

The playlist (`.m3u8`) and segments (`.ts`) may need different headers. Grab a `.ts` URL from the playlist and repeat the curl test:

```bash
# Get a segment URL from the m3u8 content
curl -s -A "Mozilla/5.0" -e "https://embed-domain.com/" \
  "https://example.com/hls/stream.m3u8?token=xxx" | grep ".ts" | head -1

# Test that segment
curl -o /dev/null -w "%{http_code}" -A "Mozilla/5.0" \
  -e "https://embed-domain.com/" \
  "https://example.com/hls/segment-12345.ts"
```

### Step 5: Configure the proxy

Once you know the required headers:

```bash
export HLS_PROXY_UA="Mozilla/5.0 ..."        # usually the default is fine
export HLS_PROXY_REFERER="https://embed-domain.com/"  # the iframe/embed host
```

### Quick reference: common patterns

| Site pattern | Usually needs |
|-------------|---------------|
| Page → iframe → m3u8 | Referer = iframe embed domain |
| Direct m3u8 with token | User-Agent only |
| Cloudflare-protected | User-Agent + Referer + sometimes Origin |

### Tip: find the iframe chain automatically

Most sites follow this pattern: **page → iframe → embed page → m3u8**

```bash
# Extract the iframe src
curl -sL -A "Mozilla/5.0" "https://streaming-site.com/channel.php" \
  | grep -oP 'iframe\s+src="\K[^"]+'

# That gives you the embed domain for the Referer
```

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `HLS_PROXY_PORT` | `8089` | Proxy listen port |
| `HLS_PROXY_BIND` | `127.0.0.1` | Bind address (localhost only by default) |
| `HLS_PROXY_UA` | Chrome UA string | User-Agent sent to upstream |
| `HLS_PROXY_REFERER` | _(empty)_ | Fallback Referer header (auto-learned from /channel/) |
| `HLS_ALLOWED_IPS` | _(empty)_ | Comma-separated client IP allowlist (empty = all) |
| `CHANNELS_CONF` | `./channels.conf` | Path to channel config file |
| `HLS_CACHE_TTL` | `3600` | Seconds to cache scraped m3u8 URLs (per channel) |
| `M3U_OUTPUT` | `/tmp/iptv.m3u` | Output M3U file path |
| `HLS_PROXY_URL` | `http://127.0.0.1:8089` | Proxy URL written into M3U |

## Media server compatibility

| Media server | M3U support | How to use |
|-------------|------------|-----------|
| **Jellyfin** | Native | Add M3U file as a tuner in Live TV settings |
| **Channels DVR** | Native | Add as custom M3U source |
| **Plex** | Via proxy | Use [Threadfin](https://github.com/Threadfin/Threadfin) or [xTeVe](https://github.com/xteve-project/xTeVe) to expose M3U as a virtual tuner |
| **Emby** | Via proxy | Same as Plex — use Threadfin or xTeVe |
| **VLC** | Direct | `vlc http://YOUR_HOST:8089/channel/sporttv1` |
| **mpv** | Direct | `mpv http://YOUR_HOST:8089/channel/sporttv1` |
| **Any HLS player** | Direct | Point at `http://YOUR_HOST:8089/channel/<slug>` |

## FAQ

**Why not a Jellyfin plugin?**
Standalone scripts work with any media server and any player. No .NET dependency, no breakage when Jellyfin updates, and it also works with VLC, mpv, or any HLS-capable player.

**Does this add latency?**
No. The proxy is passthrough only — it forwards the exact same bytes from the upstream, no transcoding. The only added latency is the network hop through the proxy (typically <1ms on localhost).

**Does this work with Plex or Emby?**
Not directly — Plex and Emby don't read M3U files. You need [Threadfin](https://github.com/Threadfin/Threadfin) or [xTeVe](https://github.com/xteve-project/xTeVe) between this proxy and Plex/Emby. These tools make M3U sources appear as a local TV tuner (HDHomeRun) that Plex/Emby can use.

**Can I use this with VLC or mpv?**
Yes. The `/channel/<slug>` endpoint returns a standard HLS playlist. Any player that supports HLS can play it directly:
```bash
vlc http://localhost:8089/channel/sporttv1
mpv http://localhost:8089/channel/sporttv1
```

## See also

- [Threadfin](https://github.com/Threadfin/Threadfin) — M3U proxy for Plex/Jellyfin/Emby, makes M3U look like a local tuner
- [xTeVe](https://github.com/xteve-project/xTeVe) — M3U proxy for Plex DVR and Emby Live TV
- [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr) — IPTV stream management and distribution
- [Restreamer](https://github.com/datarhei/restreamer) — Full-featured self-hosted streaming server with web UI

## License

MIT
