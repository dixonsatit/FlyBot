"""Relay the robot's ports from this machine's LAN to the cluster (e.g. over a VPN).

The robot can't run a VPN, so at home it talks to this machine instead:

    python tools/lan_relay.py --target 192.168.3.17 --ports 31883 31880

MQTT (31883) and the dashboard / voice API (31880) are forwarded byte for byte; set the
robot's home MQTT_HOST and VOICE_URL to this machine's LAN address.
"""
from __future__ import annotations

import argparse
import asyncio
import logging

log = logging.getLogger("lan_relay")


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()


def handler(target: str, port: int):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            up_reader, up_writer = await asyncio.wait_for(asyncio.open_connection(target, port), 10)
        except (OSError, asyncio.TimeoutError) as e:
            log.warning("%s -> %s:%d failed: %s", peer, target, port, e)
            writer.close()
            return
        log.info("%s -> %s:%d", peer, target, port)
        await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))

    return handle


async def main(target: str, ports: list[int], bind: str) -> None:
    servers = [await asyncio.start_server(handler(target, p), bind, p) for p in ports]
    log.info("relaying %s ports %s to %s", bind, ports, target)
    await asyncio.gather(*(s.serve_forever() for s in servers))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--target", default="192.168.3.17")
    ap.add_argument("--ports", type=int, nargs="+", default=[31883, 31880])
    ap.add_argument("--bind", default="0.0.0.0")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    asyncio.run(main(args.target, args.ports, args.bind))
