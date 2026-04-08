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

# Cache: slug -> {m3u8_url, embed_host, fetched_at}
_channel_cache = {}
# Maps upstream host -> referer (learned from /channel/ scrapes)
_referer_map = {}


def _load_channels():
    """Load channel config: slug -> source_page_url."""
    channels = {}
    conf = CHANNELS_CONF
    if not conf:
        for p in [os.path.join(os.path.dirname(__file__), "channels.conf"), "/etc/hls-proxy/channels.conf"]:
            if os.path.exists(p):
                conf = p
                break
    if not conf or not os.path.exists(conf):
        return channels
    with open(conf) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            if len(parts) >= 6:
                channels[parts[0]] = parts[5]  # slug -> source_url
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

        # /channel/<slug> — resolve fresh m3u8 on the fly
        if parsed.path.startswith("/channel/"):
            slug = parsed.path.split("/channel/", 1)[1].strip("/")
            self._handle_channel(slug)
            return

        if parsed.path != "/proxy":
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

    def _handle_channel(self, slug):
        """Resolve a fresh m3u8 for a channel and proxy it."""
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
        return f"/proxy?url={urllib.parse.quote(full, safe='')}"

    def log_message(self, format, *args):
        if args and "200" not in str(args[0]) and "206" not in str(args[0]):
            super().log_message(format, *args)


def main():
    server = http.server.HTTPServer((BIND_ADDR, PORT), HLSProxyHandler)
    print(f"[hls-proxy] Listening on {BIND_ADDR}:{PORT}")
    if ALLOWED_IPS:
        print(f"[hls-proxy] Allowed IPs: {', '.join(ALLOWED_IPS)}")
    else:
        print(f"[hls-proxy] WARNING: No IP allowlist set (HLS_ALLOWED_IPS). All clients accepted.")
    print(f"[hls-proxy] Endpoints:")
    print(f"  /proxy?url=<encoded_url>  — proxy with headers")
    print(f"  /channel/<slug>           — auto-resolve fresh m3u8")
    print(f"  /health                   — health check")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[hls-proxy] Stopped")
        server.server_close()


if __name__ == "__main__":
    main()
