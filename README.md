# MeshCore Proxy

**Share one MeshCore WiFi node with multiple companion clients — at the same time.**

MeshCore companion firmware accepts only **one TCP client at a time**. Connect
Home Assistant to your node and your phone app is locked out; connect the app
and Home Assistant loses its link. MeshCore Proxy solves this: it holds the
single connection to the node and fans it out to every client that connects.

```
                          ┌────────────────────────────┐
   ┌──────────────┐       │       MeshCore Proxy       │◄──── Home Assistant (meshcore-ha)
   │ MeshCore     │◄─────►│                            │◄──── MeshCore app (phone)
   │ WiFi node    │  TCP  │  1 node ── N clients       │◄──── meshcore-cli
   └──────────────┘       └────────────────────────────┘
```

- **Commands** from any client are forwarded to the node, serialised so frames
  from different clients never interleave.
- **Everything the node sends** is broadcast to all connected clients.
- Clients need **zero changes** — the proxy speaks the exact same raw TCP
  transport as the node itself. Point any MeshCore client at the proxy's
  address instead of the node's and it just works.

Inspired by [rgregg/meshcore-proxy](https://github.com/rgregg/meshcore-proxy),
which does the same for USB/BLE-connected radios. This project is the
counterpart for **WiFi/TCP nodes**, packaged as a Home Assistant add-on and as
a standalone service.

---

## Contents

- [Installation](#installation)
  - [Home Assistant add-on (recommended)](#home-assistant-add-on-recommended)
  - [Docker](#docker)
  - [Standalone (systemd)](#standalone-systemd)
- [Connecting your clients](#connecting-your-clients)
- [Configuration reference](#configuration-reference)
- [Security](#security)
- [How it works](#how-it-works)
- [Limitations & caveats](#limitations--caveats)
- [Troubleshooting](#troubleshooting)
- [License](#license)

---

## Installation

### Home Assistant add-on (recommended)

Requires Home Assistant OS or a Supervised installation.

1. **Settings → Add-ons → Add-on store → ⋮ (top right) → Repositories**
2. Add this repository:
   ```
   https://github.com/DinXke/MeshCore-Proxy
   ```
3. Refresh the store — **MeshCore Proxy** appears at the bottom. Install it
   (the image is built locally, this takes a minute).
4. Open the **Configuration** tab and set `node_host` to the IP address of
   your MeshCore WiFi node.
5. **Start** the add-on (enable *Start on boot* and *Watchdog*).

The proxy now listens on port `5000` of your Home Assistant machine. The host
port can be changed on the add-on's *Network* section if `5000` is taken.

### Docker

```bash
docker run -d --name mc-proxy \
  -p 5000:5000 \
  -e MCP_NODE_HOST=192.168.1.50 \
  -e MCP_ALLOWED_IPS="192.168.1.0/24" \
  --restart unless-stopped \
  python:3.12-alpine \
  sh -c "wget -qO /mc_proxy.py https://raw.githubusercontent.com/DinXke/MeshCore-Proxy/main/mc-proxy/mc_proxy.py && python3 /mc_proxy.py"
```

For a reproducible setup, build your own image from `mc-proxy/Dockerfile`
instead of fetching the script at container start.

### Standalone (systemd)

The proxy is a single-file Python script with **no dependencies** (Python
3.11+, standard library only).

```bash
sudo mkdir -p /opt/mc-proxy
sudo curl -o /opt/mc-proxy/mc_proxy.py \
  https://raw.githubusercontent.com/DinXke/MeshCore-Proxy/main/mc-proxy/mc_proxy.py

sudo tee /etc/systemd/system/mc-proxy.service > /dev/null <<'EOF'
[Unit]
Description=MeshCore Proxy
After=network.target

[Service]
DynamicUser=yes
Environment=MCP_NODE_HOST=192.168.1.50
Environment=MCP_ALLOWED_IPS=192.168.1.0/24
ExecStart=/usr/bin/python3 /opt/mc-proxy/mc_proxy.py
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable --now mc-proxy
```

---

## Connecting your clients

Replace the node's address with the proxy's address everywhere:

| Client | Setting |
|---|---|
| **meshcore-ha** (Home Assistant integration) | Connection type **TCP**, host `127.0.0.1`, port `5000` (when the proxy runs as add-on on the same machine) |
| **MeshCore app** (iOS/Android) | Add a WiFi node with the **proxy host's IP**, port `5000` |
| **meshcore-cli** | `meshcore-cli -t <proxy-ip> -P 5000` |

> ⚠️ Make sure **nothing connects to the node directly** anymore. The node
> accepts only one client: a client that bypasses the proxy will fight with
> the proxy over that single slot and both will see an unstable connection.

---

## Configuration reference

As add-on options (Configuration tab) or environment variables:

| Add-on option | Env variable | Default | Description |
|---|---|---|---|
| `node_host` | `MCP_NODE_HOST` | — (required) | IP/hostname of the MeshCore WiFi node |
| `node_port` | `MCP_NODE_PORT` | `5000` | TCP port of the node |
| — | `MCP_LISTEN_HOST` | `0.0.0.0` | Interface to listen on (standalone only) |
| *Network section* | `MCP_LISTEN_PORT` | `5000` | Port the proxy listens on |
| `allowed_ips` | `MCP_ALLOWED_IPS` | *(empty)* | Allow-list of client IPs/CIDRs, e.g. `["192.168.1.0/24", "10.0.0.5"]`. Empty = every client is accepted |
| `max_clients` | `MCP_MAX_CLIENTS` | `4` | Maximum simultaneous clients |
| `log_level` | `MCP_LOG_LEVEL` | `info` | `debug`, `info` or `warning` |
| — | `MCP_RECONNECT_S` | `1` | Seconds between node reconnect attempts |

---

## Security

Read this before exposing the proxy anywhere.

**The MeshCore companion TCP protocol has no authentication and no
encryption.** That is a property of the protocol itself, not of this proxy:
anyone who can open a TCP connection to the node — or to this proxy — has
full control over the radio (send messages, log in to repeaters, change
settings). Treat the proxy port like you treat the node itself.

What the proxy adds on top:

- **Client allow-list** (`allowed_ips`): only listed IPs/CIDRs may connect.
  Set this. A sensible value is your trusted LAN subnet, or better, the exact
  addresses of Home Assistant and your phone.
- **Connection cap** (`max_clients`): limits resource use and accidental
  fan-out.
- **No privileges**: the add-on runs without host network, without extra
  capabilities, and maps a single TCP port; the standalone unit file uses
  `DynamicUser` and systemd sandboxing.

What you must do yourself:

- **Never port-forward the proxy to the internet.** No exceptions — the
  protocol cannot be secured with a password. If you need remote access, use
  a VPN (WireGuard, Tailscale) into your LAN.
- Keep the proxy and node on a **trusted VLAN** if you segment your network,
  and use `allowed_ips` to restrict which devices may cross into it.
- The proxy intentionally does **not** log or inspect payload contents;
  traffic passes through unmodified.

---

## How it works

The MeshCore TCP transport is a **raw byte stream**: unlike the serial
transport (which frames with `0x3C`/`0x3E` markers and length headers), the
TCP transport sends protocol frames as-is and relies on the client's protocol
parser to find frame boundaries.

That makes a fan-out proxy straightforward:

- **Node → clients**: every chunk read from the node is written to all
  connected clients. Each client's own protocol parser handles boundary
  detection, exactly as if it were talking to the node directly.
- **Clients → node**: chunks are forwarded under a lock, so a command from
  one client is never interleaved *inside* another client's bytes. Commands
  are small (< 300 bytes) and sent as single writes by all known client
  libraries, so chunk boundaries equal frame boundaries in practice.
- **Reconnect**: if the node drops the connection (reboot, WiFi hiccup, OTA
  update), the proxy reconnects every second. Clients stay connected to the
  proxy the whole time and simply miss the frames the node did not send.

Responses from the node are broadcast to *all* clients, including clients
that did not send the corresponding command. MeshCore clients tolerate this:
unexpected frames are parsed and either update local state or are ignored.

## Limitations & caveats

- **Message sync is destructive.** In the companion protocol a client *pops*
  waiting messages off the node's queue. With two clients connected, whichever
  syncs first consumes the message — a chat message will appear in Home
  Assistant *or* in your app, not both. For telemetry, status and management
  this makes no difference. This limitation applies to every shared-connection
  approach, it is inherent to the protocol.
- **One upstream node** per proxy instance. Run multiple instances (with
  different listen ports) for multiple nodes.
- Do not run **two proxies** against the same node, and do not let any client
  connect to the node directly while the proxy runs — the node's single slot
  will bounce between them.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Add-on log: `node (nog) niet bereikbaar` repeatedly | Another client is still connected directly to the node (the node allows only one). Point that client at the proxy, or power-cycle the node. Also check `node_host`. |
| Client connects but sees no data | Check the add-on log: is the proxy connected to the node? A client that connected before the proxy acquired the node simply has to wait for that (max a few seconds). |
| `client geweigerd (niet in allow-list)` in the log | The client's IP is not covered by `allowed_ips`. Add its IP or subnet. |
| Chat messages missing in the app | See *Limitations*: another connected client (usually Home Assistant) consumed them first. |
| Everything is slow/flaky | Make sure nothing else (old direct config, second proxy) still connects to the node; watch the log for repeated `node connection lost`. |

## License

[MIT](LICENSE) — © DinXke. Not affiliated with the MeshCore project.
