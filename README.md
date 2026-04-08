# hls-restream-proxy

Lightweight HLS restream toolkit for self-hosted media servers (Jellyfin, Emby, Plex).

Many free IPTV/HLS sources require specific HTTP headers (User-Agent, Referer) that media servers don't send. This proxy sits between your media server and the upstream, injecting the required headers and rewriting m3u8 playlists so all segment requests also go through the proxy.

## Components

| File | Purpose |
|------|---------|
| `hls-proxy.py` | HTTP reverse proxy that adds headers to upstream HLS requests |
| `refresh-m3u.sh` | Scrapes source pages, extracts m3u8 URLs, writes M3U playlist |
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

1. `refresh-m3u.sh` scrapes source pages → extracts fresh m3u8 URLs → wraps them through the proxy → writes the M3U file
2. `hls-proxy.py` proxies all requests (playlists + segments) adding the required headers
3. Your media server reads the M3U file and plays streams through the proxy

## Requirements

- Python 3.8+ (stdlib only, no pip packages)
- bash, curl, grep (with PCRE / `-P`)

## Quick start

```bash
# 1. Clone
git clone https://github.com/pcruz1905/hls-restream-proxy.git
cd hls-restream-proxy

# 2. Configure channels
cp channels.conf.example channels.conf
# Edit channels.conf with your channels

# 3. Start the proxy
export HLS_PROXY_PORT=8089
export HLS_PROXY_REFERER="https://your-upstream-embed-domain.com/"
python3 hls-proxy.py &

# 4. Generate the M3U
export M3U_OUTPUT=/path/to/jellyfin/config/iptv.m3u
export HLS_PROXY_URL="http://YOUR_HOST_IP:8089"
bash refresh-m3u.sh

# 5. Add the M3U file as an M3U Tuner in your media server
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
| `HLS_PROXY_UA` | Chrome UA string | User-Agent sent to upstream |
| `HLS_PROXY_REFERER` | _(empty)_ | Referer header sent to upstream |
| `M3U_OUTPUT` | `/tmp/iptv.m3u` | Output M3U file path |
| `HLS_PROXY_URL` | `http://127.0.0.1:8089` | Proxy URL written into M3U |
| `CHANNELS_CONF` | `./channels.conf` | Path to channel config file |

## License

MIT
