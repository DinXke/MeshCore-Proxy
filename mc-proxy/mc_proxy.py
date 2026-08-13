#!/usr/bin/env python3
"""MeshCore Proxy — TCP fan-out proxy for MeshCore companion radios over WiFi.

The MeshCore companion firmware accepts only ONE TCP client at a time. This
proxy holds that single connection and lets multiple clients (the Home
Assistant integration, the MeshCore app, meshcore-cli) share it:

    WiFi node <--- proxy ---> Home Assistant integration
                        \\--> MeshCore app
                        \\--> meshcore-cli

Data flow:
- client -> node: forwarded per received chunk, serialised with a lock so
  frames from different clients never interleave mid-frame;
- node -> clients: every chunk is broadcast to all connected clients. The
  MeshCore TCP transport is a raw byte stream without extra framing; clients
  detect frame boundaries at the protocol level themselves.

Security:
- optional client allow-list (MCP_ALLOWED_IPS, comma-separated IPs/CIDRs);
- connection cap (MCP_MAX_CLIENTS);
- the MeshCore TCP protocol itself has NO authentication or encryption —
  never expose this port outside a trusted network. See the README.

Configuration is taken from environment variables:
  MCP_NODE_HOST     IP/hostname of the MeshCore WiFi node   (required)
  MCP_NODE_PORT     TCP port of the node                    (default 5000)
  MCP_LISTEN_HOST   interface to listen on                  (default 0.0.0.0)
  MCP_LISTEN_PORT   port to listen on                       (default 5000)
  MCP_ALLOWED_IPS   comma-separated IPs/CIDRs; empty = all  (default empty)
  MCP_MAX_CLIENTS   maximum simultaneous clients            (default 4)
  MCP_RECONNECT_S   seconds between node reconnect attempts (default 1)
  MCP_LOG_LEVEL     debug / info / warning                  (default info)
"""
import asyncio
import ipaddress
import logging
import os
import sys

NODE_HOST = os.environ.get("MCP_NODE_HOST", "")
NODE_PORT = int(os.environ.get("MCP_NODE_PORT", "5000"))
LISTEN_HOST = os.environ.get("MCP_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("MCP_LISTEN_PORT", "5000"))
MAX_CLIENTS = int(os.environ.get("MCP_MAX_CLIENTS", "8"))
IDLE_EVICT_S = float(os.environ.get("MCP_IDLE_EVICT_S", "60"))
RECONNECT_S = float(os.environ.get("MCP_RECONNECT_S", "1"))
CHUNK = 4096

log = logging.getLogger("mc-proxy")


def parse_allowed(raw: str):
    """Parse the allow-list; invalid entries are rejected loudly at startup."""
    networks = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            networks.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            log.error("Ongeldige allow-list entry: %r", part)
            sys.exit(1)
    return networks


ALLOWED = parse_allowed(os.environ.get("MCP_ALLOWED_IPS", ""))


def _host_gateway_ips() -> set[str]:
    """Localhost + de default gateway van de container. Verbindingen vanaf de
    Home Assistant-host komen door de Docker-poortmapping binnen met het
    gateway-adres als bron; die horen altijd toegelaten te zijn."""
    ips = {"127.0.0.1", "::1"}
    try:
        with open("/proc/net/route", encoding="ascii") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == "00000000" and parts[2] != "00000000":
                    raw = int(parts[2], 16).to_bytes(4, "little")
                    ips.add(str(ipaddress.ip_address(raw)))
    except (OSError, ValueError):
        pass
    return ips


ALWAYS_ALLOWED = _host_gateway_ips()


def client_allowed(host: str) -> bool:
    if not ALLOWED or host in ALWAYS_ALLOWED:
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(addr in net for net in ALLOWED)


class Proxy:
    def __init__(self) -> None:
        # dict behoudt invoegvolgorde; per client ook het laatste zendmoment,
        # zodat we bij een volle bak alleen echt inactieve sessies vervangen
        self.clients: dict[asyncio.StreamWriter, dict] = {}
        self.up_writer: asyncio.StreamWriter | None = None
        self.write_lock = asyncio.Lock()
        self._was_connected = False
        # de client die het laatst een commando stuurde: responses gaan alleen
        # daarheen; push-frames (eerste byte >= 0x80) gaan naar iedereen
        self.last_commander: asyncio.StreamWriter | None = None

    async def upstream_loop(self) -> None:
        """Keep the single node connection alive; reconnect on loss."""
        while True:
            try:
                reader, writer = await asyncio.open_connection(NODE_HOST, NODE_PORT)
                self.up_writer = writer
                self._was_connected = True
                log.info("connected to node %s:%s", NODE_HOST, NODE_PORT)
                while True:
                    data = await reader.read(CHUNK)
                    if not data:
                        raise ConnectionError("node closed the connection")
                    await self.dispatch(data)
            except Exception as err:  # noqa: BLE001
                level = logging.WARNING if self._was_connected else logging.DEBUG
                log.log(level, "node connection lost (%s); retry in %ss", err, RECONNECT_S)
                self._was_connected = False
                if self.up_writer is not None:
                    try:
                        self.up_writer.close()
                    except Exception:  # noqa: BLE001
                        pass
                self.up_writer = None
                await asyncio.sleep(RECONNECT_S)

    async def dispatch(self, data: bytes) -> None:
        """Routeer node-data: push-frames (eerste byte >= 0x80, bv. adverts en
        inkomende berichten) naar alle clients; command-responses alleen naar
        de client die het laatst een commando stuurde. Zo raakt geen enkele
        client in de war van andermans antwoorden."""
        if data and data[0] < 0x80:
            target = self.last_commander
            if target is not None and target in self.clients:
                try:
                    target.write(data)
                    await target.drain()
                    return
                except Exception:  # noqa: BLE001
                    self.clients.pop(target, None)
        await self.broadcast(data)

    async def broadcast(self, data: bytes) -> None:
        dead = []
        for w in list(self.clients):
            try:
                w.write(data)
                await w.drain()
            except Exception:  # noqa: BLE001
                dead.append(w)
        for w in dead:
            self.clients.pop(w, None)

    async def handle_client(self, reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        host = peer[0] if peer else "?"
        if not client_allowed(host):
            log.warning("client %s geweigerd (niet in allow-list)", host)
            writer.close()
            return
        loop = asyncio.get_running_loop()
        if len(self.clients) >= MAX_CLIENTS:
            # vervang alleen een sessie die al IDLE_EVICT_S niets meer stuurde;
            # actieve verbindingen (bv. van de meshcore-integratie) blijven staan
            now = loop.time()
            idle = [(w, m) for w, m in self.clients.items()
                    if now - m["last_tx"] > IDLE_EVICT_S]
            if idle:
                victim, meta = idle[0]
                log.warning("max %d clients: inactieve sessie (%s, %.0fs stil) "
                            "vervangen door %s", MAX_CLIENTS, meta["host"],
                            now - meta["last_tx"], host)
                self.clients.pop(victim, None)
                try:
                    victim.close()
                except Exception:  # noqa: BLE001
                    pass
            else:
                log.warning("client %s geweigerd (%d actieve clients, geen inactieve)",
                            host, len(self.clients))
                writer.close()
                return
        self.clients[writer] = {"host": host, "last_tx": loop.time()}
        log.info("client %s connected (%d active)", host, len(self.clients))
        try:
            while True:
                data = await reader.read(CHUNK)
                if not data:
                    break
                meta = self.clients.get(writer)
                if meta is not None:
                    meta["last_tx"] = asyncio.get_running_loop().time()
                async with self.write_lock:
                    self.last_commander = writer
                    up = self.up_writer
                    if up is not None:
                        up.write(data)
                        await up.drain()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.clients.pop(writer, None)
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
            log.info("client %s disconnected (%d left)", host, len(self.clients))


async def main() -> None:
    level = getattr(logging, os.environ.get("MCP_LOG_LEVEL", "info").upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s")
    if not NODE_HOST:
        log.error("MCP_NODE_HOST is verplicht (IP van je MeshCore WiFi-node)")
        sys.exit(1)
    proxy = Proxy()
    server = await asyncio.start_server(proxy.handle_client, LISTEN_HOST, LISTEN_PORT)
    log.info("mc-proxy listening on %s:%s — node: %s:%s — allow-list: %s — max clients: %d",
             LISTEN_HOST, LISTEN_PORT, NODE_HOST, NODE_PORT,
             ", ".join(str(n) for n in ALLOWED) or "iedereen", MAX_CLIENTS)
    if ALLOWED:
        log.info("altijd toegelaten (host/gateway): %s", ", ".join(sorted(ALWAYS_ALLOWED)))
    async with server:
        await asyncio.gather(server.serve_forever(), proxy.upstream_loop())


if __name__ == "__main__":
    asyncio.run(main())
