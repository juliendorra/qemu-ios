#!/usr/bin/env python3
"""Local HTTP compatibility bridge for S5L8900 device emulation."""

import argparse
import html
import ipaddress
import json
import re
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urljoin, urlparse
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    Request,
    build_opener,
)


MAX_RESPONSE = 8 * 1024 * 1024
LINK_RE = re.compile(rb"(?i)(href|src|action)=(['\"])(.*?)\2")


def public_target(url):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("only public http:// and https:// URLs are allowed")
    default_port = 443 if parsed.scheme == "https" else 80
    for item in socket.getaddrinfo(parsed.hostname, parsed.port or default_port,
                                   type=socket.SOCK_STREAM):
        address = ipaddress.ip_address(item[4][0])
        if not address.is_global:
            raise ValueError("local and private destinations are blocked")
    return parsed


def bridge_url(url):
    return "/fetch?url=" + quote(url, safe="")


class PublicRedirectHandler(HTTPRedirectHandler):
    """Reject redirects that leave the public HTTP(S) address space."""

    def redirect_request(self, request, file_pointer, code, message, headers,
                         new_url):
        new_url = urljoin(request.full_url, new_url)
        public_target(new_url)
        return super().redirect_request(
            request, file_pointer, code, message, headers, new_url)


class BridgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    device_name = "S5L8900 device"
    bridge_port = 18080
    event_log = None
    event_log_lock = threading.Lock()
    redirect_url = "https://example.com/"

    def do_GET(self):
        parsed = urlparse(self.path)
        self.record_event("http-request", path=parsed.path,
                          host=self.headers.get("Host", ""))
        if parsed.scheme in ("http", "https") and parsed.netloc:
            self.fetch(self.path, rewrite=False)
        elif parsed.path == "/":
            self.home()
        elif parsed.path == "/redirect":
            self.redirect()
        elif parsed.path == "/proxy.pac":
            self.proxy_pac()
        elif parsed.path == "/fetch":
            target = parse_qs(parsed.query).get("url", [""])[0]
            self.fetch(target, rewrite=True)
        else:
            self.send_error(404)

    def record_event(self, event, **fields):
        if self.event_log is None:
            return
        record = {"event": event, "time_unix": time.time(), **fields}
        line = json.dumps(record, sort_keys=True) + "\n"
        with self.event_log_lock:
            with self.event_log.open("a", encoding="utf-8") as stream:
                stream.write(line)

    def redirect(self):
        body = b""
        self.send_response(302)
        self.send_header("Location", self.redirect_url)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.record_event("http-redirect", path="/redirect",
                          location=self.redirect_url)

    def do_CONNECT(self):
        body = (b"HTTPS proxying is not enabled yet. The guest must trust "
                b"the bridge CA before TLS can be terminated safely.\n")
        self.reply(501, "text/plain; charset=utf-8", body)

    def home(self):
        device_name = html.escape(self.device_name)
        body = f"""<!doctype html>
<html><head><title>S5L8900 web bridge</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0,
maximum-scale=1.0, user-scalable=no">
<style>
body {{ margin: 0; padding: 14px; font: 16px Helvetica, Arial, sans-serif;
       color: #222; background: #fff; }}
h1 {{ margin: 0 0 12px; font-size: 24px; }}
p {{ margin: 0 0 14px; line-height: 1.35; }}
input.url {{ display: block; box-sizing: border-box; width: 100%;
             margin-bottom: 8px; padding: 9px; font-size: 16px; }}
input.open {{ padding: 8px 18px; font-size: 16px; }}
li {{ margin: 10px 0; }}
</style></head>
<body><h1>S5L8900 web bridge</h1>
<p>This host-side compatibility service lets old Safari on {device_name}
request modern HTTPS pages using the Mac's DNS and certificates. A separate
guest network transport is required to reach this service.</p>
<form action="/fetch" method="get">
<p><input class="url" name="url" value="https://example.com/">
<input class="open" type="submit" value="Open"></p></form>
<ul>
<li><a href="/fetch?url=http%3A%2F%2Fexample.com%2F">Example over HTTP</a></li>
<li><a href="/fetch?url=https%3A%2F%2Fjuliendorra.com%2F">juliendorra.com over HTTPS</a></li>
<li><a href="/redirect">Transparent HTTP to HTTPS redirect test</a></li>
<li><a href="/proxy.pac">Automatic proxy configuration</a></li>
</ul></body></html>""".encode("utf-8")
        self.reply(200, "text/html; charset=utf-8", body)

    def proxy_pac(self):
        body = ("function FindProxyForURL(url, host) {\n"
                f'    return "PROXY 10.0.2.2:{self.bridge_port}";\n'
                "}\n").encode("ascii")
        self.reply(200, "application/x-ns-proxy-autoconfig", body)

    def fetch(self, target, rewrite):
        try:
            public_target(target)
            headers = {
                "User-Agent": "S5L8900-HTTP-Bridge/1.0",
                "Accept-Encoding": "identity",
            }
            for name in ("Accept", "Accept-Language", "Cookie", "Referer",
                         "User-Agent"):
                if self.headers.get(name):
                    headers[name] = self.headers[name]
            request = Request(target, headers=headers)
            opener = build_opener(
                PublicRedirectHandler(),
                HTTPSHandler(context=ssl.create_default_context()),
            )
            with opener.open(request, timeout=20) as response:
                final_url = response.geturl()
                public_target(final_url)
                content_type = response.headers.get_content_type()
                charset = response.headers.get_content_charset() or "utf-8"
                body = response.read(MAX_RESPONSE + 1)
                if len(body) > MAX_RESPONSE:
                    raise ValueError("page exceeds the 8 MiB bridge limit")
        except (HTTPError, URLError, OSError, ValueError) as error:
            message = html.escape(str(error)).encode()
            self.reply(502, "text/html; charset=utf-8",
                       b"<h1>Bridge error</h1><p>" + message + b"</p>")
            return

        if rewrite and content_type in ("text/html", "application/xhtml+xml"):
            body = self.rewrite_html(body, final_url, charset)
            content_type = "text/html; charset=utf-8"
        self.reply(200, content_type, body)

    @staticmethod
    def rewrite_html(body, base_url, charset):
        def replace(match):
            raw = match.group(3)
            try:
                value = raw.decode(charset, errors="replace")
                absolute = urljoin(base_url, html.unescape(value))
                if urlparse(absolute).scheme not in ("http", "https"):
                    return match.group(0)
                rewritten = bridge_url(absolute).encode()
                return (match.group(1) + b"=" + match.group(2) + rewritten +
                        match.group(2))
            except (UnicodeError, ValueError):
                return match.group(0)

        return LINK_RE.sub(replace, body).decode(
            charset, errors="replace").encode("utf-8")

    def reply(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--device-name", default="S5L8900 device")
    parser.add_argument("--event-log", type=Path,
                        help="append metadata-only JSONL request events")
    parser.add_argument("--redirect-url", default="https://example.com/")
    args = parser.parse_args()
    BridgeHandler.device_name = args.device_name
    BridgeHandler.bridge_port = args.port
    BridgeHandler.event_log = args.event_log
    BridgeHandler.redirect_url = args.redirect_url
    if args.event_log:
        args.event_log.parent.mkdir(parents=True, exist_ok=True)
    server = BridgeServer(("127.0.0.1", args.port), BridgeHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
