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
git clone https://github.com/YOUR_USER/hls-restream-proxy.git
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
slug|Display Name|logo_url|Group|source_page_url|mode|referer
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
