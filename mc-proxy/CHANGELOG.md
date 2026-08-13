# Changelog

## 1.1.0

- Client-allowlist (`allowed_ips`, IP's of CIDR's) — aanbevolen om in te stellen
- Maximum aantal gelijktijdige clients (`max_clients`, standaard 4)
- Instelbaar logniveau (`log_level`)
- Draait zonder host-netwerk: enkel poort 5000/tcp wordt gemapt (aanpasbaar
  via de netwerksectie van de add-on)
- `node_host` verplicht bij de eerste start, met duidelijke foutmelding

## 1.0.0

- Eerste versie: TCP-fanout-proxy voor MeshCore WiFi-nodes; meerdere
  companions delen één node, met automatische herverbinding.
