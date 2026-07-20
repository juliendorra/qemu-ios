#!/usr/bin/env python3
"""Assert S5L8900 guest network milestones from an Ethernet PCAP."""

import argparse
import json
import struct
import sys
from pathlib import Path


def packets(path):
    data = path.read_bytes()
    if len(data) < 24:
        raise ValueError("truncated PCAP header")
    magic = data[:4]
    if magic == b"\xd4\xc3\xb2\xa1":
        endian = "<"
    elif magic == b"\xa1\xb2\xc3\xd4":
        endian = ">"
    else:
        raise ValueError("unsupported PCAP byte order or format")
    offset = 24
    while offset + 16 <= len(data):
        _, _, captured, _ = struct.unpack_from(endian + "IIII", data, offset)
        offset += 16
        if offset + captured > len(data):
            raise ValueError("truncated PCAP packet")
        yield data[offset:offset + captured]
        offset += captured


def inspect(path, proxy_port, proxy_slots):
    facts = {"packets": 0, "arp": 0, "dhcp": 0, "dns": 0,
             "tcp_80": 0, "tcp_443": 0, "tls_bridge_tcp": 0,
             "tls_records": 0}
    for frame in packets(path):
        facts["packets"] += 1
        if len(frame) < 14:
            continue
        ether_type = struct.unpack_from("!H", frame, 12)[0]
        if ether_type == 0x0806:
            facts["arp"] += 1
            continue
        if ether_type != 0x0800 or len(frame) < 34:
            continue
        ip = frame[14:]
        header = (ip[0] & 0x0f) * 4
        if header < 20 or len(ip) < header + 4:
            continue
        protocol = ip[9]
        transport = ip[header:]
        if protocol == 17 and len(transport) >= 8:
            source, destination = struct.unpack_from("!HH", transport, 0)
            if {source, destination} == {67, 68}:
                facts["dhcp"] += 1
            if source == 53 or destination == 53:
                facts["dns"] += 1
        elif protocol == 6 and len(transport) >= 20:
            source, destination = struct.unpack_from("!HH", transport, 0)
            if 80 in (source, destination):
                facts["tcp_80"] += 1
            if 443 in (source, destination):
                facts["tcp_443"] += 1
            bridge_ports = range(proxy_port, proxy_port + proxy_slots + 1)
            if 443 in (source, destination) or source in bridge_ports or \
                    destination in bridge_ports:
                if source in bridge_ports or destination in bridge_ports:
                    facts["tls_bridge_tcp"] += 1
                tcp_header = (transport[12] >> 4) * 4
                payload = transport[tcp_header:]
                if len(payload) >= 5 and payload[0] in (20, 21, 22, 23) and \
                        payload[1] == 3:
                    facts["tls_records"] += 1
    return facts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pcap", type=Path)
    parser.add_argument("--require-http", action="store_true")
    parser.add_argument("--require-https", action="store_true")
    parser.add_argument("--proxy-port", type=int, default=18443)
    parser.add_argument("--proxy-slots", type=int, default=64)
    args = parser.parse_args()
    facts = inspect(args.pcap, args.proxy_port, args.proxy_slots)
    required = {"arp": facts["arp"] > 0, "dhcp": facts["dhcp"] > 0,
                "dns": facts["dns"] > 0}
    if args.require_http:
        required["tcp_80"] = facts["tcp_80"] > 0
    if args.require_https:
        required["tls_bridge_tcp"] = (facts["tcp_443"] > 0 or
                                      facts["tls_bridge_tcp"] > 0)
        required["tls_records"] = facts["tls_records"] > 0
    report = {"pcap": str(args.pcap), "facts": facts,
              "required": required, "passed": all(required.values())}
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
