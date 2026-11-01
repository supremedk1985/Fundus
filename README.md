# Fundus

**Find your small parts again — self-hosted.** Fundus keeps track of screws, cables, connectors, spare parts and rarely
used tools stored in boxes. Take a photo with the Android app, put the thing in a box, and months later Fundus tells you
the room, the shelf or stack, the spot — and how many boxes you have to lift.

This repository is the **Fundus server**. The Android app is on Google Play ("Fundus: Find Your Small Parts") and talks
only to the server address you enter. There is no account with the developer and no cloud: your data stays on your server.

*Deutsch weiter unten.*

## What the server does

- Stores boxes, rooms, stacks and shelves, items with photos, quantities, notes and projects with parts lists (SQLite + files)
- Search by name or by purpose ("something for stripping wires")
- QR labels for the boxes as PNG or as one PDF for any label printer
- Optional **image recognition** with ChatGPT (OpenAI) using **your own API key** — a few cents per 100 photos
- **Shop links**: share a product link from a shop app, the server reads title, photo and details
- **Optimize photos**: crop, brighten and remove the background, computed locally with the ISNet model (no cloud)

## Setup (Docker)

Requirements: Docker with Compose, about 1.5 GB disk for the image, 2 GB RAM free while removing backgrounds
(tested on x86-64; ARM64 such as a Raspberry Pi 4/5 with 4 GB should work but is untested).

```sh
git clone https://github.com/supremedk1985/Fundus.git
cd Fundus
cp .env.example .env        # set PASSWORT to a long password
docker compose up -d --build
curl http://localhost:8095/api/gesundheit    # {"ok":true,"dienst":"fundus",...}
```

In the app, enter the server address (for example `192.168.1.20:8095` at home) and the password.

### Access from outside your home

Put the server behind a reverse proxy with HTTPS (Caddy, Traefik, nginx, or a tunnel such as Tailscale) and enter
that address in the app, e.g. `https://fundus.example.com`. Every endpoint except `/api/gesundheit` and `/api/anmelden`
requires the token you get when signing in; after five wrong passwords from one address the server pauses for 15 minutes.
Plain `http://` is fine on your own Wi-Fi; over the internet always use HTTPS — the app warns you otherwise.

Example `Caddyfile`:

```
fundus.example.com {
    reverse_proxy 127.0.0.1:8095
}
```

### Image recognition

Without a key you enter items by hand; everything else works. To enable recognition, create an API key at
<https://platform.openai.com/api-keys> and either enter it in the app (**More → Image recognition**; it is stored only on
your server) or set `OPENAI_API_KEY` in `.env`. The key in `.env` takes precedence and cannot be changed from the app.
Default model is `gpt-4.1-mini`; another one can be chosen in the app. Each recognition sends the photo and the names of
your categories from your server to OpenAI.

### Backup and update

Everything lives in `./data` (database `fundus.sqlite3`, photos). Back up that folder; for a consistent copy of a running
server use `sqlite3 data/fundus.sqlite3 ".backup backup.sqlite3"`.

Update: `git pull && docker compose up -d --build`. The database is migrated automatically on start.

### Settings

| Variable | |
|---|---|
| `PASSWORT` | Password, used on the first start only. To change it later, stop the server, delete the row in table `konto` and start again with a new `PASSWORT`. |
| `OPENAI_API_KEY` | Optional, see above |
| `OPENAI_MODELL` | Optional default model |
| `TZ` | Time zone |

## API

FastAPI; interactive documentation at `http://<server>:8095/docs`. Names of endpoints and fields are German
(`/api/kisten` = boxes, `/api/sachen` = items, `/api/suche?q=` = search). Send `Accept-Language: en` to get messages in English.

## Development

```sh
cd server
docker build -t fundus-server:dev .
docker run --rm -v "$PWD":/app -w /app fundus-server:dev python -m pytest -q
```

## License

[GNU AGPL-3.0](LICENSE). The ISNet model (`isnet-general-use`, downloaded during the image build) is published by its
authors under the Apache License 2.0.

---

# Fundus (Deutsch)

**Kleinteile wiederfinden — auf dem eigenen Server.** Fundus merkt sich Schrauben, Kabel, Klemmen, Ersatzteile und selten
gebrauchtes Werkzeug in Kisten. Mit der Android-App ein Foto machen, in die Kiste legen — und Monate später sagt Fundus
Raum, Regal oder Stapel, Platz und wie viele Kisten man abheben muss.

Dieses Repository ist der **Fundus-Server**. Die App gibt es bei Google Play („Fundus – Kleinteile finden“); sie spricht
nur mit der Serveradresse, die du einträgst. Kein Konto beim Entwickler, keine Cloud.

## Einrichten (Docker)

```sh
git clone https://github.com/supremedk1985/Fundus.git
cd Fundus
cp .env.example .env        # PASSWORT auf ein langes Passwort setzen
docker compose up -d --build
```

In der App die Adresse (zuhause z. B. `192.168.1.20:8095`) und das Passwort eintragen. Für unterwegs den Server hinter
einen Reverse Proxy mit HTTPS stellen (Beispiel oben) und diese Adresse eintragen.

**Bilderkennung:** OpenAI-Schlüssel unter <https://platform.openai.com/api-keys> anlegen und in der App unter
**Mehr → Bilderkennung** eintragen (wird nur auf deinem Server gespeichert) oder als `OPENAI_API_KEY` in die `.env`.
Ohne Schlüssel trägst du Sachen von Hand ein.

**Sichern:** alles liegt in `./data`. **Aktualisieren:** `git pull && docker compose up -d --build`.

Lizenz: GNU AGPL-3.0.
