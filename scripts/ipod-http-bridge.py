#!/usr/bin/env python3
"""Local HTTP compatibility bridge for S5L8900 device emulation."""

import argparse
import html
import ipaddress
import re
import socket
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.home()
        elif parsed.path == "/fetch":
            target = parse_qs(parsed.query).get("url", [""])[0]
            self.fetch(target)
        else:
            self.send_error(404)

    def home(self):
        device_name = html.escape(self.device_name)
        body = f"""<!doctype html>
<html><head><title>S5L8900 web bridge</title></head>
<body><h1>S5L8900 web bridge</h1>
<p>This host-side compatibility service lets old Safari on {device_name}
request modern HTTPS pages using the Mac's DNS and certificates. A separate
guest network transport is required to reach this service.</p>
<form action="/fetch" method="get">
<p><input name="url" value="https://example.com/" size="36">
<input type="submit" value="Open"></p></form>
<ul>
<li><a href="/fetch?url=http%3A%2F%2Fexample.com%2F">Example over HTTP</a></li>
<li><a href="/fetch?url=https%3A%2F%2Fjuliendorra.com%2F">juliendorra.com over HTTPS</a></li>
</ul></body></html>""".encode("utf-8")
        self.reply(200, "text/html; charset=utf-8", body)

    def fetch(self, target):
        try:
            public_target(target)
            request = Request(target, headers={
                "User-Agent": "S5L8900-HTTP-Bridge/1.0",
                "Accept-Encoding": "identity",
            })
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

        if content_type in ("text/html", "application/xhtml+xml"):
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
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--device-name", default="S5L8900 device")
    args = parser.parse_args()
    BridgeHandler.device_name = args.device_name
    server = BridgeServer(("127.0.0.1", args.port), BridgeHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
