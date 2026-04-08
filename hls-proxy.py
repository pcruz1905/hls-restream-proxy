#!/usr/bin/env python3
"""
HLS reverse proxy that injects custom HTTP headers (User-Agent, Referer, etc.)
into upstream requests. Rewrites m3u8 playlists so media players fetch all
segments through the proxy — no client-side header configuration needed.

Zero dependencies — stdlib only (Python 3.8+).
"""

import http.server
import urllib.request
import urllib.parse
import re
import os


PORT = int(os.environ.get("HLS_PROXY_PORT", "8089"))
UPSTREAM_UA = os.environ.get(
    "HLS_PROXY_UA",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
)
UPSTREAM_REFERER = os.environ.get("HLS_PROXY_REFERER", "")


class HLSProxyHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
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
            headers = {"User-Agent": UPSTREAM_UA}
            if UPSTREAM_REFERER:
                headers["Referer"] = UPSTREAM_REFERER

            req = urllib.request.Request(upstream_url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                content = resp.read()
                content_type = resp.headers.get("Content-Type", "application/octet-stream")

            if b"#EXTM3U" in content or upstream_url.endswith(".m3u8") or "mpegurl" in content_type.lower():
                content = self._rewrite_playlist(content, upstream_url)
                content_type = "application/vnd.apple.mpegurl"

            self.send_response(200)
            self.send_header("Content-Type", content_type)
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
    server = http.server.HTTPServer(("0.0.0.0", PORT), HLSProxyHandler)
    print(f"[hls-proxy] Listening on port {PORT}")
    print(f"[hls-proxy] Usage: http://<host>:{PORT}/proxy?url=<encoded_m3u8_url>")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[hls-proxy] Stopped")
        server.server_close()


if __name__ == "__main__":
    main()
