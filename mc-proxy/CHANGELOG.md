# Changelog

## 1.5.0

- **Eigen handshake + keepalive naar de node.** De node sluit verbindingen
  die zich niet aanmelden of te lang stil zijn; de proxy stuurt nu meteen na
  het verbinden een APP_START en daarna elke 20 s een keepalive. Zonder dit
  viel de nodeverbinding continu weg en verdwenen clientcommando's in een
  dode socket (getest: twee gelijktijdige clients krijgen nu allebei een
  correcte handshake en eigen commando-antwoorden).
- Antwoorden op de interne handshake/keepalive worden geslikt, niet naar
  clients gestuurd.
- Duidelijke waarschuwing in het log wanneer een commando niet doorgestuurd
  kan worden omdat de node onbereikbaar is.

## 1.4.0

- **Echte frame-parsing**: het TCP-transport blijkt wél framing te gebruiken
  (0x3C/0x3E + 16-bit lengte + payload). De proxy parseert nu complete
  frames in beide richtingen; het pakkettype (offset 3) bepaalt de routering:
  responses naar de vrager, pushes naar iedereen. Eerdere versies keken naar
  de framemarker en routeerden daardoor alles verkeerd.

## 1.3.0

- **Exchange-serialisatie**: één command/response-uitwisseling tegelijk over
  de node; zolang een commando loopt gaan alle responseframes gegarandeerd
  naar de vrager (stiltedetectie voor meerdelige antwoorden, max 2 s).
  Lost handshake-races op wanneer meerdere clients (of meerdere verbindingen
  van dezelfde integratie) tegelijk commando's sturen.

## 1.2.0

- **Slimme routering**: command-responses van de node gaan alleen nog naar de
  client die het commando stuurde; push-frames (adverts, inkomende berichten,
  eerste byte >= 0x80) gaan naar alle clients. Voorheen kreeg elke client
  andermans antwoorden te zien, waardoor sommige clients (o.a. de
  meshcore-integratie) in een reconnect-storm belandden.

## 1.1.3

- Verdringing bij volle client-slots gebeurt alleen nog bij sessies die
  >60 s niets meer stuurden; actieve verbindingen (de meshcore-integratie
  gebruikt er meerdere tegelijk) blijven onaangeroerd
- Standaard `max_clients` verhoogd van 4 naar 8

## 1.1.2

- Bij het bereiken van `max_clients` wordt de oudste verbinding vervangen
  in plaats van de nieuwe geweigerd — gestrande sessies (bv. agressieve
  reconnects van een client) verstoppen de proxy niet meer.

## 1.1.1

- Verbindingen vanaf de Home Assistant-host (localhost/docker-gateway) worden
  altijd toegelaten, ook met een ingestelde allow-list — de poortmapping laat
  die binnenkomen met het interne gateway-adres als bron.

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
