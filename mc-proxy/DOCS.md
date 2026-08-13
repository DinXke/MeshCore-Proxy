# MeshCore Proxy

MeshCore companion firmware accepts only **one TCP client at a time**. This
add-on holds that single connection to your WiFi node and shares it with as
many clients as you like:

```
WiFi node  <-->  MeshCore Proxy (this add-on)  <-->  meshcore-ha integration (127.0.0.1:5000)
                                               <-->  MeshCore app (your-HA-IP:5000)
                                               <-->  meshcore-cli
```

## Setup

1. Set `node_host` to the IP of your MeshCore WiFi node and start the add-on.
2. Point the **meshcore integration** at TCP host `127.0.0.1`, port `5000`.
3. Connect the **MeshCore app** to your Home Assistant machine's IP, port `5000`.
4. Make sure nothing connects to the node directly anymore — the node has
   only one slot, and a direct client will fight the proxy over it.

## Options

| Option | Default | Description |
|---|---|---|
| `node_host` | — (required) | IP/hostname of the MeshCore WiFi node |
| `node_port` | `5000` | TCP port of the node |
| `allowed_ips` | `[]` | Allow-list of client IPs/CIDRs (e.g. `192.168.1.0/24`). Empty = every client allowed. **Recommended to set.** |
| `max_clients` | `4` | Maximum simultaneous clients |
| `log_level` | `info` | `debug` / `info` / `warning` |

The listen port can be changed in the add-on's **Network** section.

## Security

The MeshCore TCP protocol has **no authentication or encryption** — anyone
who can reach this port controls your radio. Keep it inside your trusted
network, set `allowed_ips`, and **never** port-forward it to the internet
(use a VPN for remote access).

## Good to know

- Message sync is destructive in the companion protocol: with several clients
  connected, a chat message is consumed by whichever client syncs first. It
  will show up in one client, not all. Telemetry and management are unaffected.
- If the node reboots, the proxy reconnects automatically within seconds;
  clients stay connected to the proxy throughout.

Full documentation: https://github.com/DinXke/MeshCore-Proxy
