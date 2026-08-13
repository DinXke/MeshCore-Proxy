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
  MCP_MAX_CLIENTS   maximum simultaneous clients            (default 32)
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
KEEPALIVE_S = float(os.environ.get("MCP_KEEPALIVE_S", "30"))
# Ruim bemeten: een node op een zwakke wifi-link kan er seconden over doen.
# Te snel afbreken en herverbinden maakt het op zo'n link juist erger.
HANDSHAKE_TIMEOUT_S = float(os.environ.get("MCP_HANDSHAKE_TIMEOUT_S", "30"))
MAX_SILENT_ROUNDS = int(os.environ.get("MCP_MAX_SILENT_ROUNDS", "3"))
MAX_RECONNECT_S = float(os.environ.get("MCP_MAX_RECONNECT_S", "15"))
HEALTH_PORT = int(os.environ.get("MCP_HEALTH_PORT", "5001"))
# Hoelang de node weg mag zijn voor clients losgekoppeld worden
NODE_DOWN_GRACE_S = float(os.environ.get("MCP_NODE_DOWN_GRACE_S", "60"))
# Minimale tussentijd tussen commando's naar de node. Meerdere clients samen
# (de HA-integratie opent er zelf al een handvol) kunnen een klein radio-apparaat
# overspoelen; met deze pacing krijgt de node dezelfde rustige stroom als bij
# één enkele client.
MIN_CMD_GAP_S = float(os.environ.get("MCP_MIN_CMD_GAP_S", "0.25"))

# Companion-protocol: frames zijn marker + LE16-lengte + payload.
# 0x3C ('<') client->node, 0x3E ('>') node->client.
CMD_APP_START = 0x01
CMD_GET_DEVICE_TIME = 0x05
PKT_SELF_INFO = 0x05  # antwoordtype op APP_START


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
        # korte vergrendeling: voorkomt dat frames van twee clients door
        # elkaar naar de node geschreven worden
        self.cmd_lock = asyncio.Lock()
        self._last_resp_t = 0.0
        self._last_upstream_tx = 0.0
        # De node beantwoordt APP_START maar één keer per TCP-verbinding. De
        # proxy doet die handshake zelf en bewaart het SELF_INFO-antwoord, om
        # daarmee de APP_START van elke client te beantwoorden.
        self.self_info_frame: bytes | None = None
        # nodegezondheid: antwoordt hij nog op onze frames?
        self._node_alive = False
        self._last_node_rx = 0.0
        self._silent_rounds = 0
        self._node_down_since: float | None = None

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
                self._node_down_since = None
                log.info("connected to node %s:%s", NODE_HOST, NODE_PORT)
                # meteen aanmelden, anders sluit de node de verbinding weer.
                # De bewaarde self_info blijft geldig tot de node een nieuwe
                # stuurt: zo kunnen clients ook tijdens een haperende
                # nodeverbinding gewoon aanmelden.
                self._node_alive = False
                await self._send_internal(APP_START)
                # Antwoordt de node niet op de handshake, dan is de firmware
                # vastgelopen: verbinding sluiten en opnieuw proberen. Een
                # verse TCP-sessie brengt een half-vastgelopen node meestal bij.
                asyncio.create_task(self._handshake_watchdog(writer))
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
                # Clients pas loskoppelen als de node echt langere tijd weg is.
                # Bij een haperende verbinding (zwakke wifi) is het beter dat
                # clients verbonden blijven: ze zien even geen data en kunnen
                # daarna gewoon verder, zonder herverbindingsstorm.
                if self._node_down_since is None:
                    self._node_down_since = asyncio.get_running_loop().time()
                elif (asyncio.get_running_loop().time() - self._node_down_since
                      > NODE_DOWN_GRACE_S and self.clients):
                    await self.drop_clients("node al langer dan "
                                            f"{NODE_DOWN_GRACE_S:.0f}s onbereikbaar")
                await asyncio.sleep(backoff)

    async def _handshake_watchdog(self, writer: asyncio.StreamWriter) -> None:
        """Sluit de nodeverbinding pas als de node ook na een herkansing niets
        terugstuurt. Op een zwakke link is geduld beter dan opnieuw verbinden.

        De watchdog bewaakt precies de verbinding waarvoor hij gestart is: een
        watchdog van een oudere poging mag nooit een nieuwe, gezonde
        verbinding afbreken."""
        await asyncio.sleep(HANDSHAKE_TIMEOUT_S / 2)
        if self._node_alive or self.up_writer is not writer:
            return
        log.info("nog geen antwoord op de handshake; nog één poging")
        await self._send_internal(APP_START)
        await asyncio.sleep(HANDSHAKE_TIMEOUT_S / 2)
        if self._node_alive or self.up_writer is not writer:
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
            loop = asyncio.get_running_loop()
            self._last_upstream_tx = loop.time()
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
        """Elk compleet nodeframe gaat naar alle verbonden clients; die matchen
        zelf wat bij hun eigen commando hoort, net zoals ze rechtstreeks op de
        node zouden doen. Eerdere versies wezen antwoorden toe aan 'de huidige
        vrager', maar dan kon een drukke client andermans antwoord inpikken of
        ging het antwoord verloren."""
        self._last_resp_t = asyncio.get_running_loop().time()
        # SELF_INFO (type 0x05) bewaren: hiermee beantwoorden we de APP_START
        # van clients, die de node zelf een tweede keer niet meer beantwoordt.
        if len(frame) >= 4 and frame[3] == PKT_SELF_INFO:
            if self.self_info_frame != frame:
                log.info("self_info van de node bewaard (%d bytes) — clients "
                         "kunnen aanmelden", len(frame))
            self.self_info_frame = frame
        await self.broadcast(frame)

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

    async def _exchange(self, writer: asyncio.StreamWriter, data: bytes,
                        expect_response: bool = True) -> None:
        """Stuur één commandoframe naar de node. De vergrendeling is kort en
        dient enkel om te voorkomen dat frames van twee clients door elkaar
        geschreven worden; op het antwoord wachten we niet — dat gaat via
        broadcast naar alle clients."""
        async with self.cmd_lock:
            up = self.up_writer
            if up is None:
                log.warning("commando genegeerd: geen verbinding met de node")
                return
            # commando's netjes spreiden zodat de node niet overspoeld raakt
            loop = asyncio.get_running_loop()
            gap = loop.time() - self._last_upstream_tx
            if gap < MIN_CMD_GAP_S:
                await asyncio.sleep(MIN_CMD_GAP_S - gap)
            try:
                self._last_upstream_tx = loop.time()
                up.write(data)
                await up.drain()
            except Exception as err:  # noqa: BLE001
                log.warning("doorsturen naar node mislukt: %s", err)

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
            buf = b""
            while True:
                data = await reader.read(CHUNK)
                if not data:
                    break
                meta = self.clients.get(writer)
                if meta is not None:
                    meta["last_tx"] = asyncio.get_running_loop().time()
                buf += data
                # Client -> node frames: 0x3C ('<') + lengte (LE16) + payload;
                # elk compleet commandoframe wordt als één exchange behandeld
                while True:
                    if len(buf) < 3:
                        break
                    if buf[0] != 0x3C:
                        nxt = buf.find(b"<", 1)
                        junk, buf = (buf, b"") if nxt < 0 else (buf[:nxt], buf[nxt:])
                        await self._exchange(writer, junk, expect_response=False)
                        continue
                    ln = buf[1] | (buf[2] << 8)
                    if len(buf) < 3 + ln:
                        break
                    frame, buf = buf[:3 + ln], buf[3 + ln:]
                    # APP_START zelf beantwoorden: de node doet dat maar één
                    # keer per verbinding en negeert die van clients.
                    if (len(frame) >= 4 and frame[3] == CMD_APP_START
                            and self.self_info_frame is not None):
                        log.debug("APP_START van %s beantwoord uit cache", host)
                        try:
                            writer.write(self.self_info_frame)
                            await writer.drain()
                        except Exception:  # noqa: BLE001
                            break
                        continue
                    await self._exchange(writer, frame)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.clients.pop(writer, None)
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
            log.info("client %s disconnected (%d left)", host, len(self.clients))


async def health_server(proxy: "Proxy") -> None:
    """Mini-HTTP-statuspagina: http://<host>:<health_port>/ geeft JSON met de
    toestand van de nodeverbinding en de clients. Handig om op afstand te zien
    of de node antwoordt zonder in de add-on-logs te moeten duiken."""
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await asyncio.wait_for(reader.readline(), timeout=5)
            loop = asyncio.get_running_loop()
            body = json.dumps({
                "node_host": f"{NODE_HOST}:{NODE_PORT}",
                "node_connected": proxy.up_writer is not None,
                "node_answering": proxy._node_alive,
                "seconds_since_node_data": (
                    None if not proxy._last_node_rx
                    else round(loop.time() - proxy._last_node_rx, 1)),
                "silent_keepalive_rounds": proxy._silent_rounds,
                "clients": [m["host"] for m in proxy.clients.values()],
                "client_count": len(proxy.clients),
                "max_clients": MAX_CLIENTS,
            }, indent=1).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         b"Content-Length: " + str(len(body)).encode() +
                         b"\r\nConnection: close\r\n\r\n" + body)
            await writer.drain()
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    server = await asyncio.start_server(handle, LISTEN_HOST, HEALTH_PORT)
    log.info("statuspagina op http://%s:%s/", LISTEN_HOST, HEALTH_PORT)
    async with server:
        await server.serve_forever()


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
        await asyncio.gather(server.serve_forever(), proxy.upstream_loop(),
                             proxy.keepalive_loop(), health_server(proxy))


if __name__ == "__main__":
    asyncio.run(main())
