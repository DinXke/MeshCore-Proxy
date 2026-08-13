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
import json
import logging
import os
import sys

NODE_HOST = os.environ.get("MCP_NODE_HOST", "")
NODE_PORT = int(os.environ.get("MCP_NODE_PORT", "5000"))
LISTEN_HOST = os.environ.get("MCP_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("MCP_LISTEN_PORT", "5000"))
MAX_CLIENTS = int(os.environ.get("MCP_MAX_CLIENTS", "32"))
IDLE_EVICT_S = float(os.environ.get("MCP_IDLE_EVICT_S", "60"))
KEEPALIVE_S = float(os.environ.get("MCP_KEEPALIVE_S", "20"))
# Hoe lang een client de responsestroom exclusief krijgt na zijn commando.
# Ruim genomen: een trage of net herstarte node antwoordt soms pas na seconden.
RESP_TIMEOUT_S = float(os.environ.get("MCP_RESP_TIMEOUT_S", "3"))
RESP_QUIET_S = float(os.environ.get("MCP_RESP_QUIET_S", "0.15"))
# Hoe lang een client hoogstens op zijn beurt wacht; daarna gaat zijn
# commando er zonder exclusiviteit door (beter dan een time-out).
LOCK_WAIT_S = float(os.environ.get("MCP_LOCK_WAIT_S", "2"))
HANDSHAKE_TIMEOUT_S = float(os.environ.get("MCP_HANDSHAKE_TIMEOUT_S", "10"))
MAX_SILENT_ROUNDS = int(os.environ.get("MCP_MAX_SILENT_ROUNDS", "2"))
MAX_RECONNECT_S = float(os.environ.get("MCP_MAX_RECONNECT_S", "15"))
HEALTH_PORT = int(os.environ.get("MCP_HEALTH_PORT", "5001"))

# Companion-protocol: frames zijn marker + LE16-lengte + payload.
# 0x3C ('<') client->node, 0x3E ('>') node->client.
CMD_APP_START = 0x01
CMD_GET_DEVICE_TIME = 0x05


def frame(payload: bytes) -> bytes:
    return b"<" + len(payload).to_bytes(2, "little") + payload


# De proxy meldt zich zelf aan bij de node; zonder deze handshake sluit de
# node de verbinding weer (dat is precies waarom een 'stille' proxy faalt).
APP_START = frame(bytes([CMD_APP_START, 0x03]) + b"      " + b"mcproxy")
DEVICE_TIME = frame(bytes([CMD_GET_DEVICE_TIME]))
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
        self._was_connected = False
        # korte vergrendeling zodat frames van twee clients niet
        # door elkaar naar de node geschreven worden
        self.cmd_lock = asyncio.Lock()
        self._last_resp_t = 0.0
        self._last_upstream_tx = 0.0
        # nodegezondheid: antwoordt hij nog op onze frames?
        self._node_alive = False
        self._last_node_rx = 0.0
        self._silent_rounds = 0

    async def upstream_loop(self) -> None:
        """Keep the single node connection alive; reconnect on loss. Bij
        aanhoudend falen loopt de wachttijd op, zodat een zieke node niet
        elke seconde bestookt wordt."""
        backoff = RECONNECT_S
        while True:
            try:
                reader, writer = await asyncio.open_connection(NODE_HOST, NODE_PORT)
                self.up_writer = writer
                self._was_connected = True
                backoff = RECONNECT_S
                log.info("connected to node %s:%s", NODE_HOST, NODE_PORT)
                # meteen aanmelden, anders sluit de node de verbinding weer
                self._node_alive = False
                await self._send_internal(APP_START)
                # Antwoordt de node niet op de handshake, dan is de firmware
                # vastgelopen: verbinding sluiten en opnieuw proberen. Een
                # verse TCP-sessie brengt een half-vastgelopen node meestal bij.
                asyncio.create_task(self._handshake_watchdog())
                buf = b""
                while True:
                    data = await reader.read(CHUNK)
                    if not data:
                        raise ConnectionError("node closed the connection")
                    if not self._node_alive:
                        log.info("node antwoordt — verbinding is gezond")
                    self._node_alive = True
                    self._silent_rounds = 0
                    self._last_node_rx = asyncio.get_running_loop().time()
                    buf += data
                    # Node -> client frames: 0x3E ('>') + lengte (LE16) + payload
                    while True:
                        if len(buf) < 3:
                            break
                        if buf[0] != 0x3E:
                            # onbekende bytes: geef door en hersynchroniseer
                            nxt = buf.find(b">", 1)
                            junk, buf = (buf, b"") if nxt < 0 else (buf[:nxt], buf[nxt:])
                            log.debug("onbekende node-bytes (%d) doorgestuurd", len(junk))
                            await self.broadcast(junk)
                            continue
                        ln = buf[1] | (buf[2] << 8)
                        if len(buf) < 3 + ln:
                            break
                        frame, buf = buf[:3 + ln], buf[3 + ln:]
                        await self.dispatch(frame)
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
                # Zonder node zijn clientsessies waardeloos: sluit ze zodat
                # niemand op een dode lijn wacht en slots niet dichtslibben.
                await self.drop_clients("nodeverbinding weg")
                await asyncio.sleep(backoff)

    async def _handshake_watchdog(self) -> None:
        """Sluit de nodeverbinding als er na de handshake niets terugkomt."""
        await asyncio.sleep(HANDSHAKE_TIMEOUT_S)
        if self._node_alive or self.up_writer is None:
            return
        log.warning("node antwoordt niet op de handshake (firmware vastgelopen?); "
                    "verbinding opnieuw opbouwen")
        try:
            self.up_writer.close()
        except Exception:  # noqa: BLE001
            pass

    async def drop_clients(self, reason: str) -> None:
        if not self.clients:
            return
        log.info("alle %d clientverbindingen gesloten (%s)", len(self.clients), reason)
        for w in list(self.clients):
            try:
                w.close()
            except Exception:  # noqa: BLE001
                pass
        self.clients.clear()

    async def _send_internal(self, data: bytes) -> None:
        """Stuur een eigen frame (handshake/keepalive) naar de node; het
        antwoord wordt geslikt in plaats van naar clients gestuurd."""
        up = self.up_writer
        if up is None:
            return
        try:
            self._last_upstream_tx = asyncio.get_running_loop().time()
            up.write(data)
            await up.drain()
        except Exception:  # noqa: BLE001
            pass

    async def keepalive_loop(self) -> None:
        """Houd de nodeverbinding warm; een stille verbinding wordt door de
        node gesloten."""
        while True:
            await asyncio.sleep(KEEPALIVE_S / 2)
            loop = asyncio.get_running_loop()
            if self.up_writer is None:
                continue
            if loop.time() - self._last_upstream_tx < KEEPALIVE_S:
                continue
            if self.cmd_lock.locked():
                continue
            # inactieve sessies opruimen zodat slots niet dichtslibben
            now = asyncio.get_running_loop().time()
            for w, m in list(self.clients.items()):
                if now - m["last_tx"] > IDLE_EVICT_S * 3:
                    log.info("inactieve client %s opgeruimd", m["host"])
                    self.clients.pop(w, None)
                    try:
                        w.close()
                    except Exception:  # noqa: BLE001
                        pass
            before = self._last_node_rx
            async with self.cmd_lock:
                await self._send_internal(DEVICE_TIME)
            await asyncio.sleep(HANDSHAKE_TIMEOUT_S)
            if self._last_node_rx > before:
                continue
            self._silent_rounds += 1
            log.warning("node antwoordde niet op de keepalive (%d/%d)",
                        self._silent_rounds, MAX_SILENT_ROUNDS)
            if self._silent_rounds >= MAX_SILENT_ROUNDS and self.up_writer is not None:
                log.warning("node reageert niet meer; verbinding opnieuw opbouwen")
                self._silent_rounds = 0
                try:
                    self.up_writer.close()
                except Exception:  # noqa: BLE001
                    pass

    async def dispatch(self, frame: bytes) -> None:
        """Elk nodeframe gaat naar alle verbonden clients. Clients matchen zelf
        wat bij hun eigen commando hoort; een frame dat ze niet verwachten
        negeren ze. Dit is bewust simpel: eerdere versies probeerden
        antwoorden aan één vrager toe te wijzen, waarbij een drukke client
        andermans antwoord kon inpikken of het antwoord verloren ging."""
        self._last_resp_t = asyncio.get_running_loop().time()
        await self.broadcast(frame)

    async def _exchange(self, writer: asyncio.StreamWriter, data: bytes,
                        expect_response: bool = True) -> None:
        """Stuur één commandoframe naar de node. De vergrendeling is kort en
        dient enkel om te voorkomen dat frames van twee clients door elkaar
        geschreven worden; op het antwoord wachten we niet (dat gaat via
        broadcast naar alle clients)."""
        async with self.cmd_lock:
            up = self.up_writer
            if up is None:
                log.warning("commando genegeerd: geen verbinding met de node")
                return
            try:
                self._last_upstream_tx = asyncio.get_running_loop().time()
                up.write(data)
                await up.drain()
            except Exception as err:  # noqa: BLE001
                log.warning("doorsturen naar node mislukt: %s", err)


