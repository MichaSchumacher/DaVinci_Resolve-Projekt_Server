# Resolve Project Server

Selbst gehosteter Projektserver für DaVinci Resolve – PostgreSQL in Docker mit Web-UI
für Bibliotheken, Zugänge, Sicherungen und Versionswechsel. Aufbau angelehnt an den
WireBear Studio Server.

## Aufbau

```
Internet ──443──▶ frontend (Caddy, HTTPS + Web-UI)
                     │  internes Netz (kein Internetzugang, kein Port nach außen)
                     ▼
                  backend (API, Sicherungen, Versionswechsel)
                     │  Docker-API (docker exec)
                     ▼
Internet ─5432─▶ rps-postgres-<version>   ◀── wird vom Backend erzeugt
```

- **Frontend und Backend sprechen nur miteinander.** Das Backend hat keinen Port und
  hängt nur im internen Netz (`internal: true`).
- **Alle Daten liegen auf dem Host** unter `DATA_DIR`:

```
DATA_DIR/
├── postgres/13/      Datenverzeichnis je Hauptversion (bleibt beim Wechsel erhalten)
├── backups/db/…      Sicherungen je Bibliothek (.dump)
├── backups/full/…    Vollsicherungen inkl. Zugänge (.sql)
├── hooks/            eigene Skripte vor/nach Sicherungen
├── config/           Einstellungen, Konto, Zugänge der Bibliotheken, Logo
└── caddy/            Zertifikate
```

## Installation

```bash
git clone … /opt/resolve-project-server   # oder Ordner hochladen
cd /opt/resolve-project-server
cp .env.example .env
nano .env                                 # DATA_DIR, DOMAIN, Passwörter!
mkdir -p "$(grep ^DATA_DIR .env | cut -d= -f2)"
docker compose up -d --build
```

Danach `https://DEINE-DOMAIN` öffnen und mit `ADMIN_USER` / `ADMIN_PASSWORD` anmelden.
Das Passwort bitte gleich unter **Konto** ändern. Unter **Konfiguration** die
Zugangsadresse eintragen – vorher ist „Verbinden“ gesperrt.

### Router / Firewall

| Port | Am Router weiterleiten? | Zweck |
|------|------|-------|
| 80, 443 TCP | ja (ohne NPM) | Web-UI mit HTTPS und Let's-Encrypt-Zertifikat |
| 5432 TCP | ja | Verbindung der Resolve-Arbeitsplätze |
| 8543 TCP | **nein, nie** | Web-UI unverschlüsselt: für das lokale Netz und als Ziel für einen Reverse Proxy wie NPM |

Port 8543 ist „intern“ in dem Sinn, dass er nie direkt aus dem Internet erreichbar
sein darf. Ein Reverse Proxy im eigenen Netz darf ihn ansprechen – der Weg ins
Internet ist dann trotzdem verschlüsselt (siehe [Hinter NPM](#hinter-nginx-proxy-manager-npm)).

**Port 50059 (bis 50314)** gehört nicht zum Server: Resolve öffnet ihn auf jedem
Arbeitsplatz für die Zusammenarbeit, und die Rechner verbinden sich direkt
untereinander. Über das Internet klappt das zuverlässig nur, wenn alle
Arbeitsplätze im selben VPN sind. Die Seite **Zusammenarbeit** zeigt, welche Rechner
sich mit welcher Adresse angemeldet haben, und testet deren Erreichbarkeit.

Der DNS-Eintrag der Domain muss auf deine öffentliche IP zeigen. Zum lokalen Testen
ohne Domain: `DOMAIN=:80` und im Browser `http://SERVER-IP`.

## Hinter Nginx Proxy Manager (NPM)

Wenn auf dem Server oder im Netz schon NPM läuft, übernimmt NPM HTTPS und das
Zertifikat. Caddy öffnet dann kein 80/443 mehr, nur noch 8543:

```
Internet ──HTTPS 443──▶ NPM ──HTTP 8543 (nur im LAN)──▶ Web-UI
```

Am Router bleiben nur 443 (für NPM) und 5432 (für Resolve) offen.

### NPM auf einem anderen Rechner

In der `.env`:

```env
COMPOSE_FILE=docker-compose.yml:docker-compose.npm.yml
ADMIN_PORT_BIND=0.0.0.0
```

In NPM einen Proxy Host anlegen:

| Feld | Eintrag |
|---|---|
| Domain Names | `resolve.deinedomain.de` |
| Scheme / Forward Hostname / Port | `http` · LAN-IP dieses Servers · `8543` |
| Block Common Exploits | an |
| Cache Assets, Websockets Support | nicht nötig |
| SSL | Let's-Encrypt-Zertifikat anfordern, Force SSL und HTTP/2 an |

Wer es strenger will, erlaubt 8543 per Firewall nur von der IP des NPM-Rechners.

### NPM auf demselben Docker-Host

Dann sprechen NPM und Web-UI über ein gemeinsames Docker-Netz, und 8543 muss am
Host gar nicht offen sein. Name des NPM-Netzes herausfinden:

```bash
docker inspect <npm-container> -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}'
```

In der `.env`:

```env
COMPOSE_FILE=docker-compose.yml:docker-compose.npm.yml:docker-compose.npm-network.yml
NPM_NETWORK=npm_default        # Name aus dem Befehl oben
ADMIN_PORT_BIND=127.0.0.1
```

In NPM als Ziel eintragen: `http` · `rps-frontend` · `8543`, sonst wie oben.

### Was der NPM-Modus sonst noch tut

- Caddy holt kein eigenes Zertifikat mehr (`DOMAIN` wird intern ignoriert).
- Caddy übernimmt die echte Besucher-IP aus den Headern von NPM. Sonst sähe das
  Backend für alle Anfragen nur die IP von NPM, und die Sperre nach fünf
  Fehlanmeldungen träfe alle gemeinsam. Gefälschte `X-Forwarded-For`-Header aus dem
  Browser werden dabei ignoriert.
- Benötigt Docker Compose 2.24.4 oder neuer (`docker compose version`).

Danach `docker compose up -d`. Port 5432 wird nicht über NPM geleitet, sondern
weiterhin direkt am Router weitergeleitet. Läuft die Domain über Cloudflare mit
aktivem Proxy (orange Wolke), braucht Resolve eine zweite Subdomain ohne Proxy.

## Bibliotheken und Zugänge

„Neu anlegen“ erstellt eine Datenbank **und einen eigenen Benutzer mit Passwort**.
Dieser Benutzer darf sich nur mit seiner Bibliothek verbinden – wer einen Zugang
kennt, sieht die anderen Bibliotheken nicht. Namen: nur `A–Z a–z 0–9 _`, Leerzeichen
werden zu `_`.

Bibliotheken, die direkt in Resolve angelegt wurden, erscheinen ebenfalls und nutzen
zunächst den Admin-Zugang. Über „Erweitert → Eigenen Zugang einrichten“ bekommen sie
einen eigenen.

## Sicherungen

- Pro Bibliothek: manuell, stündlich, täglich oder wöchentlich.
- Nächtliche Vollsicherung des ganzen Servers (inkl. Benutzer und Passwörter).
- Aufräumen nach Alter; die neuesten N pro Bibliothek bleiben immer, angeheftete nie
  gelöscht. Sicherungen vor Löschen, Wiederherstellen und Versionswechsel werden
  automatisch angeheftet.
- Wiederherstellen als neue Bibliothek oder über die bestehende.

### Hooks

Unter `DATA_DIR/hooks/` liegen `pre-backup.example` und `post-backup.example`.
Kopieren/umbenennen in `pre-backup` bzw. `post-backup` und pro Bibliothek in der
Web-UI einschalten. Im Container stehen `sh`, `rsync`, `curl` und `ssh` bereit.
Beispiel post-backup:

```sh
#!/bin/sh
[ "$RPS_STATUS" = "ok" ] && rsync -a "$RPS_BACKUP_FILE" nas:/volume1/resolve-backups/
```

## PostgreSQL-Version wechseln

Unter **Server**. Ablauf: exakte Zeilenzahl jeder Tabelle erfassen → Vollsicherung und
Einzelsicherungen → neue Version startet intern ohne Port → Daten einspielen →
Zeile-für-Zeile-Vergleich → erst dann umschalten. Bei jeder Abweichung läuft die alte
Version weiter; ihr Datenverzeichnis bleibt zum Zurückwechseln erhalten.

Resolve 18–20: **PostgreSQL 13** (das installiert auch Blackmagics eigener Project
Server). Resolve ≤ 17: 11 oder 9.5. Neuere Versionen sind von Blackmagic nicht
freigegeben – vorher mit einer Kopie testen.

## Zugangsschlüssel (.resolvedbkey)

Das Format ist von Blackmagic nicht dokumentiert, die mitgelieferte Vorlage ist eine
**ungeprüfte Annahme**. Zuverlässig: Eine Bibliothek in Resolve verbinden, dort den
Zugangsschlüssel exportieren und unter **Konfiguration → Vorlage aus Datei lernen**
hochladen. Ist die Datei verschlüsselt, klappt das nicht – dann die Zugangsdaten aus
„Verbinden“ abtippen.

## Sicherheit

- Ein offener Port 5432 im Internet wird ständig abgeklopft. Starke Passwörter sind
  Pflicht. Sicherer: VPN (WireGuard/Tailscale) und `PG_BIND_ADDRESS` auf die VPN-IP.
- Das Backend hat Zugriff auf `/var/run/docker.sock` (nötig, um die Postgres-Version
  zu wechseln). Das entspricht Root-Rechten auf dem Host – deshalb ist es nur über die
  angemeldete Web-UI erreichbar.
- `PG_AUTH_METHOD` gilt für neu angelegte Datenverzeichnisse. `md5` ist am
  verträglichsten; `scram-sha-256` ist sicherer, bitte vorher mit Resolve testen.
- Web-UI: Anmeldung mit Sperre nach 5 Fehlversuchen, HTTPS, strenge
  Sicherheits-Header, Sitzungen enden bei Passwortänderung.

## Nützliche Befehle

```bash
docker compose logs -f backend          # Backend-Log
docker ps --filter label=managed-by=resolve-project-server
docker compose pull && docker compose up -d --build   # Update
```
