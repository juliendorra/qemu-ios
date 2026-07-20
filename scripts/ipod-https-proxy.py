#!/usr/bin/env python3
"""Transparent, local-only legacy TLS bridge for iPhone OS 1 Safari.

The guest-facing socket permits only SSLv3/TLS 1.0 and the RSA suites the
2007 SecureTransport client offers.  Each accepted connection is terminated
locally with a per-host certificate signed by the private per-install bridge
CA.  A separate default Python TLS client connection is then made upstream,
with certificate and hostname verification left enabled.

No HTTP payload, header, cookie, URL, or credential is logged.  The proof log
contains only TLS metadata needed to distinguish the legacy and modern legs.
"""

import argparse
import hashlib
import ipaddress
import json
import socket
import socketserver
import ssl
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from ipod_tls_common import LEAF_CIPHERS, leaf_certificate, normalize_hostname


def public_addresses(hostname):
    addresses = []
    for item in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM):
        address = ipaddress.ip_address(item[4][0])
        if not address.is_global:
            raise ValueError(f"non-public upstream address rejected for {hostname}")
        addresses.append(str(address))
    if not addresses:
        raise ValueError(f"no upstream address for {hostname}")
    return sorted(set(addresses))


def legacy_context(certificate, key):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.set_ciphers(LEAF_CIPHERS)
    if hasattr(ssl, "OP_NO_SSLv3"):
        context.options &= ~ssl.OP_NO_SSLv3
    if hasattr(ssl, "OP_NO_TLSv1"):
        context.options &= ~ssl.OP_NO_TLSv1
    if hasattr(ssl, "OP_NO_TICKET"):
        # Context switching in the SNI callback must not switch session-ticket
        # keys underneath a TLS 1.0 client.
        context.options |= ssl.OP_NO_TICKET
    context.minimum_version = ssl.TLSVersion.SSLv3
    context.maximum_version = ssl.TLSVersion.TLSv1
    context.load_cert_chain(str(certificate), str(key))
    return context


class TLSBridge:
    def __init__(self, state_dir, proof_log, base_port, slots):
        self.state_dir = Path(state_dir)
        self.proof_log = Path(proof_log)
        self.base_port = base_port
        self.slots = slots
        self.contexts = {}
        self.context_lock = threading.Lock()
        self.log_lock = threading.Lock()
        self.target_condition = threading.Condition()
        self.port_targets = {}
        fallback, key, _ = leaf_certificate(self.state_dir, "bridge.invalid")
        self.base_context = legacy_context(fallback, key)
        self.base_context.set_servername_callback(self._select_certificate)

    def _context_for(self, hostname):
        hostname = normalize_hostname(hostname)
        with self.context_lock:
            context = self.contexts.get(hostname)
            if context is None:
                certificate, key, _ = leaf_certificate(self.state_dir, hostname)
                context = legacy_context(certificate, key)
                self.contexts[hostname] = context
            return context

    def _select_certificate(self, ssl_socket, server_name, _initial_context):
        if not server_name:
            return
        hostname = normalize_hostname(server_name)
        ssl_socket.context = self._context_for(hostname)
        ssl_socket.s5l8900_server_name = hostname

    def record(self, event):
        event = dict(event)
        event["time"] = datetime.now(timezone.utc).isoformat()
        line = json.dumps(event, sort_keys=True)
        with self.log_lock:
            self.proof_log.parent.mkdir(parents=True, exist_ok=True)
            with self.proof_log.open("a", encoding="utf-8") as output:
                output.write(line + "\n")

    def update_target(self, proxy_port, hostname, original_address):
        if not self.base_port < proxy_port <= self.base_port + self.slots:
            raise ValueError(f"proxy slot port out of range: {proxy_port}")
        hostname = normalize_hostname(hostname)
        address = ipaddress.ip_address(original_address)
        if not address.is_global:
            raise ValueError(f"non-public original address rejected: {address}")
        with self.target_condition:
            self.port_targets[proxy_port] = (
                hostname,
                str(address),
                time.monotonic(),
            )
            self.target_condition.notify_all()
        self.record({
            "event": "tls-target-mapped",
            "proxy_port": proxy_port,
            "hostname": hostname,
            "original_address": str(address),
        })

    def target_for_port(self, proxy_port, timeout=2.0):
        if proxy_port == self.base_port:
            return None
        deadline = time.monotonic() + timeout
        with self.target_condition:
            while True:
                target = self.port_targets.get(proxy_port)
                if target and time.monotonic() - target[2] < 5.0:
                    return target[:2]
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.target_condition.wait(remaining)

    def serve_connection(self, raw_client, peer, proxy_port):
        client = None
        upstream = None
        hostname = None
        try:
            mapped_target = self.target_for_port(proxy_port)
            if mapped_target:
                hostname, original_address = mapped_target
                context = self._context_for(hostname)
            else:
                original_address = None
                context = self.base_context
            client = context.wrap_socket(raw_client, server_side=True)
            hostname = hostname or getattr(client, "s5l8900_server_name", None)
            if not hostname:
                raise ValueError(
                    "no DNS target metadata and the legacy client did not send SNI"
                )
            public_addresses(hostname)
            upstream_raw = socket.create_connection((hostname, 443), timeout=20)
            upstream_context = ssl.create_default_context()
            upstream = upstream_context.wrap_socket(
                upstream_raw, server_hostname=hostname
            )
            peer_cert = upstream.getpeercert(binary_form=True)
            self.record(
                {
                    "event": "tls-bridge-established",
                    "hostname": hostname,
                    "original_address": original_address,
                    "proxy_port": proxy_port,
                    "guest_peer": peer[0],
                    "guest_tls": client.version(),
                    "guest_cipher": client.cipher()[0],
                    "upstream_tls": upstream.version(),
                    "upstream_cipher": upstream.cipher()[0],
                    "upstream_certificate_sha256": hashlib.sha256(peer_cert).hexdigest(),
                    "upstream_verification": "CERT_REQUIRED+check_hostname",
                }
            )
            relay_pair(client, upstream)
        except Exception as error:
            self.record(
                {
                    "event": "tls-bridge-failed",
                    "hostname": hostname,
                    "guest_peer": peer[0],
                    "error_type": type(error).__name__,
                    "error": str(error),
                }
            )
        finally:
            for stream in (client, upstream, raw_client):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass


def relay_pair(left, right):
    errors = []

    def copy(source, destination):
        try:
            while True:
                data = source.recv(64 * 1024)
                if not data:
                    break
                destination.sendall(data)
        except (OSError, ssl.SSLError) as error:
            errors.append(error)
        finally:
            try:
                destination.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    threads = [
        threading.Thread(target=copy, args=(left, right), daemon=True),
        threading.Thread(target=copy, args=(right, left), daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


class BridgeHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.server.bridge.serve_connection(
            self.request, self.client_address, self.server.server_address[1]
        )


class BridgeServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, handler, bridge):
        self.bridge = bridge
        super().__init__(address, handler)


class TargetControlHandler(socketserver.BaseRequestHandler):
    def handle(self):
        message = self.request[0].decode("ascii", errors="strict").strip()
        proxy_port, hostname, original_address = message.split("\t")
        self.server.bridge.update_target(
            int(proxy_port), hostname, original_address
        )


class TargetControlServer(socketserver.ThreadingUDPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, handler, bridge):
        self.bridge = bridge
        super().__init__(address, handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18443)
    parser.add_argument("--slots", type=int, default=64)
    parser.add_argument("--control-port", type=int)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--proof-log", type=Path)
    args = parser.parse_args()
    if args.slots < 1 or args.port + args.slots > 65535:
        parser.error("--port plus --slots must fit in TCP port space")
    control_port = args.control_port or args.port - 1
    if not 0 < control_port <= 65535:
        parser.error("invalid control port")
    proof_log = args.proof_log or args.state / "https-proof.jsonl"
    bridge = TLSBridge(args.state, proof_log, args.port, args.slots)
    servers = [
        BridgeServer(("127.0.0.1", port), BridgeHandler, bridge)
        for port in range(args.port, args.port + args.slots + 1)
    ]
    control = TargetControlServer(
        ("127.0.0.1", control_port), TargetControlHandler, bridge
    )
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in servers
    ]
    control_thread = threading.Thread(target=control.serve_forever, daemon=True)
    bridge.record(
        {
            "event": "tls-bridge-listening",
            "address": "127.0.0.1",
            "port": args.port,
            "port_end": args.port + args.slots,
            "control_port": control_port,
            "guest_protocols": "SSLv3-TLSv1.0",
            "guest_ciphers": LEAF_CIPHERS,
            "payload_logging": False,
        }
    )
    for thread in threads:
        thread.start()
    control_thread.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        control.shutdown()
        control.server_close()
        for server in servers:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
