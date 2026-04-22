#!/usr/bin/env python3
"""
HLS reverse proxy that injects custom HTTP headers (User-Agent, Referer, etc.)
into upstream requests. Rewrites m3u8 playlists so media players fetch all
segments through the proxy — no client-side header configuration needed.

Supports /channel/<slug> endpoints that auto-scrape fresh m3u8 URLs on the fly,
so media servers never see expired tokens.

Zero dependencies — stdlib only (Python 3.8+).
"""

import http.server
import urllib.request
import urllib.parse
import re
import os
import time


PORT = int(os.environ.get("HLS_PROXY_PORT", "8089"))
BIND_ADDR = os.environ.get("HLS_PROXY_BIND", "127.0.0.1")  # localhost only by default
UPSTREAM_UA = os.environ.get(
    "HLS_PROXY_UA",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
)
UPSTREAM_REFERER = os.environ.get("HLS_PROXY_REFERER", "")
CHANNELS_CONF = os.environ.get("CHANNELS_CONF", "")
CACHE_TTL = int(os.environ.get("HLS_CACHE_TTL", "3600"))  # 1 hour default
# Comma-separated list of allowed client IPs (empty = allow all)
ALLOWED_IPS = set(filter(None, os.environ.get("HLS_ALLOWED_IPS", "").split(",")))
# Default BANDWIDTH (bits/sec) advertised in the master playlist when a channel
# has no explicit value. Set to avoid Jellyfin's ~20 Mbps default guess on
# single-variant media playlists, which forces transcoding on bandwidth-limited
# clients. 0 / unset = do not emit a master wrapper.
DEFAULT_BANDWIDTH = int(os.environ.get("HLS_DEFAULT_BANDWIDTH", "0")) or 0
# Optional upstream M3U playlist (e.g. Dispatcharr) — if set, the proxy fetches
# this URL and auto-registers every #EXTINF entry as a literal-mode channel, so
# /playlist.m3u can be handed to the media player with DEFAULT_BANDWIDTH applied
# to each stream.
UPSTREAM_M3U_URL = os.environ.get("HLS_UPSTREAM_M3U_URL", "")
UPSTREAM_M3U_REFERER = os.environ.get("HLS_UPSTREAM_M3U_REFERER", "")
UPSTREAM_M3U_TTL = int(os.environ.get("HLS_UPSTREAM_M3U_TTL", str(CACHE_TTL)))

# Cache: slug -> {m3u8_url, embed_host, fetched_at}
_channel_cache = {}
# Maps upstream host -> referer (learned from /channel/ scrapes + literal mode preload)
_referer_map = {}
# Per-channel declared bandwidth (bits/sec) from channels.conf field 9
_channel_bandwidth = {}
# Per-channel extraction mode from channels.conf field 7 ("iframe" | "direct" | "literal")
_channel_mode = {}
# Per-channel declared referer from channels.conf field 8 (used by literal mode)
_channel_referer = {}
# Parsed upstream M3U: list of (slug, extinf_line, url)
_upstream_m3u = []
_upstream_m3u_fetched_at = 0.0
# Per-channel #EXTINF line built from channels.conf (slug -> extinf)
_channel_extinf = {}


def _slugify(s: str) -> str:
    """Reduce a string to a URL-safe channel slug."""
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", s).strip("-").lower()
    return s or "channel"


def _refresh_upstream_m3u() -> None:
    """Fetch and parse UPSTREAM_M3U_URL, registering entries as literal channels.

    Cached for UPSTREAM_M3U_TTL seconds. On fetch failure the previous entries
    are retained so transient upstream errors don't wipe the channel list.
    """
    global _upstream_m3u, _upstream_m3u_fetched_at
    if not UPSTREAM_M3U_URL:
        return
    now = time.time()
    if _upstream_m3u and (now - _upstream_m3u_fetched_at) < UPSTREAM_M3U_TTL:
        return

    try:
        req = urllib.request.Request(UPSTREAM_M3U_URL, headers={"User-Agent": UPSTREAM_UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[hls-proxy] Failed to fetch upstream M3U: {e}")
        return

    entries = []
    seen = set()
    current_extinf = None
    idx = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            current_extinf = line
            continue
        if line.startswith("#"):
            continue
        if current_extinf is None:
            continue

        idx += 1
        tvg_id = re.search(r'tvg-id="([^"]*)"', current_extinf)
        name_match = re.search(r",\s*(.+)$", current_extinf)
        base = ""
        if tvg_id and tvg_id.group(1).strip():
            base = tvg_id.group(1).strip()
        elif name_match and name_match.group(1).strip():
            base = name_match.group(1).strip()
        else:
            base = f"ch{idx}"
        slug = _slugify(base)
        orig_slug = slug
        n = 1
        while slug in seen:
            n += 1
            slug = f"{orig_slug}-{n}"
        seen.add(slug)
        entries.append((slug, current_extinf, line))

        _channel_mode[slug] = "literal"
        if UPSTREAM_M3U_REFERER:
            _channel_referer[slug] = UPSTREAM_M3U_REFERER
        # Invalidate stale cache so a refreshed upstream URL replaces the old one
        _channel_cache.pop(slug, None)
        host_match = re.match(r"https?://[^/]+", line)
        if host_match:
            _referer_map.setdefault(host_match.group(0), UPSTREAM_M3U_REFERER)

        current_extinf = None

    if entries:
        _upstream_m3u = entries
        _upstream_m3u_fetched_at = now
        print(f"[hls-proxy] Loaded {len(entries)} channels from upstream M3U")


def _load_channels():
    """Load channel config: slug -> source_page_url."""
    channels = {}
    conf = CHANNELS_CONF
    if not conf:
        for p in [os.path.join(os.path.dirname(__file__), "channels.conf"), "/etc/hls-proxy/channels.conf"]:
            if os.path.exists(p):
                conf = p
                break
    if conf and os.path.exists(conf):
        with open(conf) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("|")
                if len(parts) >= 6:
                    slug = parts[0]
                    name = parts[1].strip() if len(parts) >= 2 else slug
                    chno = parts[2].strip() if len(parts) >= 3 else ""
                    logo = parts[3].strip() if len(parts) >= 4 else ""
                    group = parts[4].strip() if len(parts) >= 5 else ""
                    source_url = parts[5]
                    channels[slug] = source_url
                    extinf_attrs = [f'tvg-id="{slug}"']
                    if chno:
                        extinf_attrs.append(f'tvg-chno="{chno}"')
                    if logo:
                        extinf_attrs.append(f'tvg-logo="{logo}"')
                    if group:
                        extinf_attrs.append(f'group-title="{group}"')
                    _channel_extinf[slug] = f'#EXTINF:-1 {" ".join(extinf_attrs)},{name or slug}'
                    mode = parts[6].strip().lower() if len(parts) >= 7 and parts[6].strip() else "iframe"
                    referer = parts[7].strip() if len(parts) >= 8 and parts[7].strip() else ""
                    _channel_mode[slug] = mode
                    if referer:
                        _channel_referer[slug] = referer
                    # Literal mode: source_url IS the m3u8. Pre-seed _referer_map so
                    # /proxy?url=... requests for this upstream host use the right
                    # Referer without needing a successful /channel/ scrape first.
                    if mode == "literal" and referer:
                        upstream_host = re.match(r"https?://[^/]+", source_url)
                        if upstream_host:
                            _referer_map[upstream_host.group(0)] = referer
                    # Optional 9th field: declared BANDWIDTH in bits/sec
                    if len(parts) >= 9 and parts[8].strip():
                        try:
                            _channel_bandwidth[slug] = int(parts[8].strip())
                        except ValueError:
                            pass

    # Overlay entries from the upstream M3U (if configured). File entries win
    # on slug collisions so the user can override a specific channel by hand.
    _refresh_upstream_m3u()
    for slug, _extinf, url in _upstream_m3u:
        channels.setdefault(slug, url)

    return channels


def _scrape_m3u8(source_url):
    """Scrape a source page to get the fresh m3u8 URL and embed host."""
    # Step 1: get iframe
    req = urllib.request.Request(source_url, headers={"User-Agent": UPSTREAM_UA})
    with urllib.request.urlopen(req, timeout=15) as resp:
        html = resp.read().decode("utf-8", errors="replace")

    iframe = ""
    for line in html.splitlines():
        m = re.search(r'iframe\s+src="([^"]+)"', line)
        if m:
            iframe = m.group(1)
            break

    if not iframe:
        return None, None

    embed_host = re.match(r"https?://[^/]+", iframe)
    embed_host = embed_host.group(0) if embed_host else ""

    # Step 2: get m3u8 from embed page
    page_host = re.match(r"https?://[^/]+", source_url)
    referer = (page_host.group(0) + "/") if page_host else ""
    req = urllib.request.Request(iframe, headers={"User-Agent": UPSTREAM_UA, "Referer": referer})
    with urllib.request.urlopen(req, timeout=15) as resp:
        html = resp.read().decode("utf-8", errors="replace")

    m3u8 = ""
    for m in re.finditer(r"https?://[^\"'\s]+\.m3u8[^\"'\s]*", html):
        m3u8 = m.group(0)
        break

    return m3u8, embed_host


def _get_channel_m3u8(slug):
    """Get a fresh m3u8 URL for a channel, using cache if still valid."""
    now = time.time()
    cached = _channel_cache.get(slug)
    if cached and (now - cached["fetched_at"]) < CACHE_TTL:
        return cached["m3u8_url"], cached["embed_host"]

    channels = _load_channels()
    source_url = channels.get(slug)
    if not source_url:
        return None, None

    # Literal mode: source_url is the m3u8 itself; skip scraping. Treat the
    # declared referer as the "embed host" proxy so downstream /proxy calls use it.
    if _channel_mode.get(slug) == "literal":
        referer = _channel_referer.get(slug, "")
        embed_host = referer.rstrip("/") if referer else ""
        _channel_cache[slug] = {"m3u8_url": source_url, "embed_host": embed_host, "fetched_at": now}
        return source_url, embed_host

    m3u8, embed_host = _scrape_m3u8(source_url)
    if m3u8:
        _channel_cache[slug] = {"m3u8_url": m3u8, "embed_host": embed_host, "fetched_at": now}
        # Learn the referer for this upstream host so /proxy requests use it
        upstream_host = re.match(r"https?://[^/]+", m3u8)
        if upstream_host and embed_host:
            _referer_map[upstream_host.group(0)] = embed_host + "/"
    return m3u8, embed_host


class HLSProxyHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        # IP allowlist check
        if ALLOWED_IPS:
            client_ip = self.client_address[0].removeprefix("::ffff:")
            if client_ip not in ALLOWED_IPS:
                self.send_error(403, "Forbidden")
                return

        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
            return

        if parsed.path == "/":
            self._handle_index()
            return

        if parsed.path == "/playlist.m3u":
            self._handle_playlist()
            return

        # /channel/<slug>              — master playlist (if bandwidth declared)
        # /channel/<slug>/media.m3u8   — underlying media playlist
        if parsed.path.startswith("/channel/"):
            tail = parsed.path[len("/channel/"):].strip("/").split("/", 1)
            slug = tail[0]
            is_media_request = len(tail) > 1 and tail[1] == "media.m3u8"
            self._handle_channel(slug, is_media_request=is_media_request)
            return

        # Accept /proxy and /proxy.<ext>. The extension suffix is appended when
        # rewriting segment URLs so ffmpeg's HLS demuxer accepts them under its
        # allowed_segment_extensions whitelist (default: ts,m4s,mp4,aac,m3u8,…).
        if not (parsed.path == "/proxy" or parsed.path.startswith("/proxy.")):
            self.send_error(404)
            return

        params = urllib.parse.parse_qs(parsed.query)
        upstream_url = params.get("url", [None])[0]

        if not upstream_url:
            self.send_error(400, "Missing ?url= parameter")
            return

        try:
            # Only proxy to known upstream hosts (learned from /channel/ scrapes)
            # Prevents abuse as an open proxy
            upstream_host = re.match(r"https?://[^/]+", upstream_url)
            if _referer_map and upstream_host and upstream_host.group(0) not in _referer_map:
                self.send_error(403, "Unknown upstream host")
                return

            # Use learned referer from /channel/ scrapes, fall back to env var
            referer = UPSTREAM_REFERER
            if upstream_host and upstream_host.group(0) in _referer_map:
                referer = _referer_map[upstream_host.group(0)]

            headers = {"User-Agent": UPSTREAM_UA}
            if referer:
                headers["Referer"] = referer

            req = urllib.request.Request(upstream_url, headers=headers)
            resp = urllib.request.urlopen(req, timeout=15)
            content_type = resp.headers.get("Content-Type", "application/octet-stream")
            is_playlist = upstream_url.endswith(".m3u8") or "mpegurl" in content_type.lower()

            if is_playlist:
                # Playlists are small — read fully to rewrite URLs
                content = resp.read()
                resp.close()
                if b"#EXTM3U" in content:
                    content = self._rewrite_playlist(content, upstream_url)
                    content_type = "application/vnd.apple.mpegurl"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(content)
            else:
                # Segments (.ts) — stream chunk-by-chunk, never buffer fully
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-cache")
                cl = resp.headers.get("Content-Length")
                if cl:
                    self.send_header("Content-Length", cl)
                self.end_headers()
                try:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                finally:
                    resp.close()

        except Exception as e:
            self.send_error(502, f"Upstream error: {e}")

    def _handle_index(self):
        """Plain-text landing page describing available endpoints."""
        host = self.headers.get("Host", f"{BIND_ADDR}:{PORT}").split("/", 1)[0]
        upstream = "configured" if UPSTREAM_M3U_URL else "not set (HLS_UPSTREAM_M3U_URL)"
        channels_loaded = len(_upstream_m3u)
        body = (
            f"hls-restream-proxy\n"
            f"==================\n\n"
            f"Upstream M3U:   {upstream}\n"
            f"Channels loaded: {channels_loaded}\n\n"
            f"Endpoints:\n"
            f"  http://{host}/playlist.m3u           — M3U for your media player\n"
            f"  http://{host}/channel/<slug>         — single channel (master playlist)\n"
            f"  http://{host}/channel/<slug>/media.m3u8\n"
            f"  http://{host}/health                 — health check\n"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def _handle_playlist(self):
        """Emit a rewritten M3U pointing at /channel/<slug> for each upstream entry.

        Used when HLS_UPSTREAM_M3U_URL is configured — hand this URL to the
        media player so every stream is proxied and receives the declared
        BANDWIDTH wrapper.
        """
        # Refresh both sources so the playlist reflects current state
        channels = _load_channels()
        host_header = self.headers.get("Host", f"{BIND_ADDR}:{PORT}")
        # Strip any path/query an attacker might smuggle via Host (defense in depth)
        host_header = host_header.split("/", 1)[0]
        lines = ["#EXTM3U"]
        emitted = set()

        # channels.conf entries first (user-curated order)
        for slug in channels:
            if slug in emitted or slug not in _channel_extinf:
                continue
            lines.append(_channel_extinf[slug])
            lines.append(f"http://{host_header}/channel/{slug}")
            emitted.add(slug)

        # Then upstream-M3U entries that weren't overridden by channels.conf
        for slug, extinf, _url in _upstream_m3u:
            if slug in emitted:
                continue
            lines.append(extinf)
            lines.append(f"http://{host_header}/channel/{slug}")
            emitted.add(slug)

        if not emitted:
            self.send_error(
                404,
                "No channels configured (set HLS_UPSTREAM_M3U_URL or populate channels.conf)",
            )
            return

        body = ("\n".join(lines) + "\n").encode("utf-8")
        self.send_response(200)
        # audio/x-mpegurl marks this as a playlist (list of channels), not an
        # HLS media playlist. application/vnd.apple.mpegurl triggers browsers
        # and VLC to try single-stream HLS playback instead of parsing entries.
        self.send_header("Content-Type", "audio/x-mpegurl")
        self.send_header("Content-Disposition", 'attachment; filename="playlist.m3u"')
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _handle_channel(self, slug, is_media_request: bool = False):
        """Resolve a fresh m3u8 for a channel and proxy it.

        When a channel has a declared BANDWIDTH (either via channels.conf
        field 9 or HLS_DEFAULT_BANDWIDTH) and the upstream is a single-variant
        media playlist, the /channel/<slug> response is wrapped in a thin
        master playlist carrying that BANDWIDTH. This prevents Jellyfin from
        falling back to its ~20 Mbps default guess and force-transcoding on
        bandwidth-capped clients.
        """
        m3u8_url, embed_host = _get_channel_m3u8(slug)
        if not m3u8_url:
            self.send_error(404, f"Channel not found or scrape failed: {slug}")
            return

        try:
            referer = (embed_host + "/") if embed_host else UPSTREAM_REFERER
            req = urllib.request.Request(m3u8_url, headers={
                "User-Agent": UPSTREAM_UA,
                "Referer": referer,
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                content = resp.read()

            bandwidth = _channel_bandwidth.get(slug) or DEFAULT_BANDWIDTH
            is_master = b"#EXT-X-STREAM-INF" in content

            if bandwidth and not is_master and not is_media_request:
                master = (
                    "#EXTM3U\n"
                    f"#EXT-X-STREAM-INF:BANDWIDTH={bandwidth}\n"
                    f"/channel/{slug}/media.m3u8\n"
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(master)
                return

            content = self._rewrite_playlist(content, m3u8_url)

            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(content)

        except Exception as e:
            self.send_error(502, f"Upstream error: {e}")

    def _rewrite_playlist(self, content: bytes, playlist_url: str) -> bytes:
        """Rewrite URLs in m3u8 playlists to route through this proxy."""
        text = content.decode("utf-8", errors="replace")
        base_url = playlist_url.rsplit("/", 1)[0] + "/"
        lines = text.splitlines()
        result = []

        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                if "URI=" in line:
                    line = re.sub(
                        r'URI="([^"]+)"',
                        lambda m: f'URI="{self._proxy_url(m.group(1), base_url)}"',
                        line,
                    )
                result.append(line)
            else:
                result.append(self._proxy_url(line, base_url))

        return "\n".join(result).encode("utf-8")

    def _proxy_url(self, url: str, base_url: str) -> str:
        if url.startswith("http://") or url.startswith("https://"):
            full = url
        else:
            full = base_url + url
        # Mirror the upstream file extension onto the proxy path. FFmpeg's HLS
        # demuxer checks the path (not the query) against allowed_segment_extensions,
        # so /proxy?url=... gets rejected while /proxy.ts?url=... is accepted.
        upstream_path = urllib.parse.urlparse(full).path
        ext_match = re.search(r"\.([A-Za-z0-9]{1,5})$", upstream_path)
        ext = f".{ext_match.group(1).lower()}" if ext_match else ".ts"
        return f"/proxy{ext}?url={urllib.parse.quote(full, safe='')}"

    def log_message(self, format, *args):
        if args and "200" not in str(args[0]) and "206" not in str(args[0]):
            super().log_message(format, *args)


def main():
    # Preload channels (including upstream M3U) so /playlist.m3u works on first request.
    _load_channels()
    server = http.server.HTTPServer((BIND_ADDR, PORT), HLSProxyHandler)
    print(f"[hls-proxy] Listening on {BIND_ADDR}:{PORT}")
    if ALLOWED_IPS:
        print(f"[hls-proxy] Allowed IPs: {', '.join(ALLOWED_IPS)}")
    else:
        print(f"[hls-proxy] WARNING: No IP allowlist set (HLS_ALLOWED_IPS). All clients accepted.")
    if UPSTREAM_M3U_URL:
        print(f"[hls-proxy] Upstream M3U: {UPSTREAM_M3U_URL} (refresh every {UPSTREAM_M3U_TTL}s)")
    print(f"[hls-proxy] Endpoints:")
    print(f"  /proxy?url=<encoded_url>       — proxy with headers")
    print(f"  /channel/<slug>                — auto-resolve fresh m3u8 (master if bandwidth set)")
    print(f"  /channel/<slug>/media.m3u8     — underlying media playlist")
    print(f"  /playlist.m3u                  — rewritten M3U from upstream (if configured)")
    print(f"  /health                        — health check")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[hls-proxy] Stopped")
        server.server_close()


if __name__ == "__main__":
    main()
