# Changelog

## 1.7.2

- **Eerlijke beurtverdeling**: een client hield de lijn tot 8 s bezet wanneer
  de node niet antwoordde, waardoor andere clients (en de validatie van de
  meshcore-integratie) een time-out kregen. Nu wacht een client hoogstens 2 s
  op zijn beurt en gaat zijn commando er daarna sowieso door; het
  responsvenster is 3 s.
- Verouderde interne antwoorden worden bij elk clientcommando opgeruimd, zodat
  ze nooit een clientantwoord kunnen wegfilteren.

## 1.7.1

- Oplopende wachttijd tussen mislukte verbindingspogingen (1 s -> max 15 s):
  een node met een zwakke of vastgelopen netwerkstack wordt niet langer elke
  seconde bestookt
- Inactieve clientsessies worden periodiek opgeruimd, zodat de slots niet
  dichtslibben met verweesde verbindingen

## 1.7.0

- **Statuspagina** op poort 5001 (JSON): laat zien of de node verbonden is,
  of hij antwoordt, hoelang geleden er data kwam en welke clients er hangen.
  Zo is een probleem op afstand te zien zonder in de logs te duiken.

## 1.6.0

- **Zelfherstel bij een vastgelopen node.** De companion-firmware kan in een
  toestand raken waarin ze TCP nog accepteert maar niets meer beantwoordt.
  De proxy detecteert dat nu: geen antwoord op de handshake (10 s) of twee
  keepalives op rij zonder antwoord -> verbinding sluiten en opnieuw
  opbouwen. Een verse TCP-sessie brengt zo'n node meestal weer bij.
- Duidelijke logmeldingen over de nodegezondheid ("node antwoordt", "node
  reageert niet meer").

## 1.5.1

- Responsvenster van 2 s naar 8 s: een trage of net herstarte node antwoordt
  soms pas na seconden, waardoor antwoorden de client niet bereikten
- Interne handshake/keepalive-antwoorden worden alleen nog binnen 5 s na het
  verzenden geslikt (voorkomt dat late clientantwoorden verdwijnen)
- Bij verlies van de nodeverbinding worden alle clientsessies gesloten; ze
  verbinden vanzelf opnieuw zodra de node er weer is (voorkomt dichtslibben)
- Standaard `max_clients` van 8 naar 32 (meshcore-ha opent er zelf al 4-8)

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
