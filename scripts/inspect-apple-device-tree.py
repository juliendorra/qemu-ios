#!/usr/bin/env python3
"""Inspect legacy Apple flattened device-tree function-parent references.

Accepts either a raw tree or an IMG2 container.  Output is JSON so M68AP and
N45AP trees can be compared without manually decoding property blobs.
"""

from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path


def u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def payload(data: bytes) -> bytes:
    container_offset = 0 if data[:4] == b"2gmI" else data.find(b"2gmIertd")
    if container_offset < 0:
        return data
    length = u32(data, container_offset + 0x10)
    start = container_offset + 0x400
    if len(data) < start + length:
        raise ValueError("truncated IMG2 device-tree payload")
    return data[start:start + length]


def parse_tree(data: bytes) -> list[dict]:
    nodes = []

    def parse_node(offset: int, parent: str) -> int:
        start = offset
        if offset + 8 > len(data):
            raise ValueError(f"truncated node header at 0x{offset:x}")
        property_count, child_count = struct.unpack_from("<II", data, offset)
        offset += 8
        properties = {}
        for _ in range(property_count):
            if offset + 36 > len(data):
                raise ValueError(f"truncated property at 0x{offset:x}")
            name = data[offset:offset + 32].split(b"\0", 1)[0].decode(
                "latin1")
            length = u32(data, offset + 32)
            value_start = offset + 36
            value_end = value_start + length
            if value_end > len(data):
                raise ValueError(f"truncated property {name!r}")
            properties[name] = data[value_start:value_end]
            offset = value_start + ((length + 3) & ~3)
        name = properties.get("name", b"").split(b"\0", 1)[0].decode(
            "latin1") or "<root>"
        path = (parent + "/" + name).replace("//", "/")
        phandle_data = properties.get("AAPL,phandle", b"")
        phandle = u32(phandle_data, 0) if len(phandle_data) >= 4 else None
        nodes.append({"path": path, "offset": start, "phandle": phandle,
                      "properties": properties})
        for _ in range(child_count):
            offset = parse_node(offset, path)
        return offset

    end = parse_node(0, "")
    # Extracted IMG2 payloads can retain zero padding to the container's block
    # boundary. Reject non-zero trailing bytes, but do not mistake padding for
    # another flattened-tree node.
    if any(data[end:]):
        raise ValueError(f"tree ended at 0x{end:x}, payload is 0x{len(data):x}")
    return nodes


def inspect(path: Path) -> dict:
    tree = payload(path.read_bytes())
    nodes = parse_tree(tree)
    by_phandle = {node["phandle"]: node["path"] for node in nodes
                  if node["phandle"] is not None}
    functions = []
    for node in nodes:
        for name, value in node["properties"].items():
            if not name.startswith("function-") or len(value) < 4:
                continue
            parent_phandle = u32(value, 0)
            functions.append({
                "node": node["path"], "property": name,
                "parent_phandle": f"0x{parent_phandle:08x}",
                "parent_node": by_phandle.get(parent_phandle),
                "resolved": parent_phandle in by_phandle,
                "length": len(value), "data_hex": value.hex(),
            })
    return {"input": str(path), "payload_size": len(tree),
            "node_count": len(nodes), "functions": functions}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("trees", nargs="+", type=Path)
    args = parser.parse_args()
    print(json.dumps([inspect(path) for path in args.trees], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
