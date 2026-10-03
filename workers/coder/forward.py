"""Tiny TCP forwarder (ADR-033): the only thing the sandbox can talk to.

Runs as a sidecar container attached to the per-run internal network *and* to the bridge, and
forwards every connection on its listen port to one fixed target (the control plane's LLM
proxy). Standard library only. Usage:

    python -m workers.coder.forward --listen 0.0.0.0:8080 --target host.docker.internal:8000
"""

from __future__ import annotations

import argparse
import asyncio
import sys

BUFFER = 64 * 1024


def _split(address: str) -> tuple[str, int]:
    host, _, port = address.rpartition(":")
    if not host or not port.isdigit():
        raise SystemExit(f"invalid address {address!r}; expected host:port")
    return host, int(port)


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(BUFFER):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        if not writer.is_closing():
            writer.close()


async def serve(listen: str, target: str) -> None:
    target_host, target_port = _split(target)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                target_host, target_port
            )
        except OSError:
            writer.close()
            return
        await asyncio.gather(_pump(reader, upstream_writer), _pump(upstream_reader, writer))

    host, port = _split(listen)
    server = await asyncio.start_server(handle, host, port)
    print(f"forwarding {listen} -> {target}", flush=True)
    async with server:
        await server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis-forward")
    parser.add_argument("--listen", default="0.0.0.0:8080")
    parser.add_argument("--target", required=True)
    args = parser.parse_args(argv)
    try:
        asyncio.run(serve(args.listen, args.target))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
