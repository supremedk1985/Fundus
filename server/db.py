"""Datenhaltung der Fundus: eine SQLite-Datei plus Fotos als Dateien.

Liegt im Container unter /daten (FUNDUS_DATEN) -- diesen Ordner als Volume einhängen
und mitsichern.
"""
from __future__ import annotations
import hashlib, os, secrets, sqlite3, threading
from datetime import datetime, timezone

DATEN = os.environ.get("FUNDUS_DATEN", "/daten")
PFAD = os.path.join(DATEN, "fundus.sqlite3")
FOTOS = os.path.join(DATEN, "fotos")

SCHEMA = """
CREATE TABLE IF NOT EXISTS konto (id INTEGER PRIMARY KEY CHECK (id = 1), passwort TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS anmeldungen (
  token TEXT PRIMARY KEY, geraet TEXT, angelegt_am TEXT NOT NULL, zuletzt TEXT NOT NULL
);
-- Eine Eurobox. `code` (K-07) steht auf dem Etikett und ändert sich nie.
-- `stapel` + `position` (1 = oben) sagen, wie viele Kisten man abheben muss.
-- Maße in Millimetern, Eurobox-Raster (600x400, 400x300, 300x200; Höhe frei).
CREATE TABLE IF NOT EXISTS kisten (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL UNIQUE,
  stapel TEXT NOT NULL DEFAULT '',
  position INTEGER NOT NULL DEFAULT 1,
  laenge INTEGER, breite INTEGER, hoehe INTEGER,
  notiz TEXT NOT NULL DEFAULT '',
  angelegt_am TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sachen (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kiste_id INTEGER NOT NULL REFERENCES kisten(id),
  name TEXT NOT NULL,
  beschreibung TEXT NOT NULL DEFAULT '',
  bereich TEXT NOT NULL DEFAULT '',
  menge TEXT NOT NULL DEFAULT '',
  woerter TEXT NOT NULL DEFAULT '[]',
  foto TEXT,
  raus_seit TEXT,
  angelegt_am TEXT NOT NULL,
  geaendert_am TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS i_sachen_kiste ON sachen(kiste_id);
-- Hochgeladene Fotos. `sache_id` bleibt leer, bis die Sache gespeichert ist;
-- verwaiste Fotos räumt der Server nach einer Woche weg.
CREATE TABLE IF NOT EXISTS fotos (
  id TEXT PRIMARY KEY, sache_id INTEGER, angelegt_am TEXT NOT NULL
);
-- Jede Anfrage an die Bilderkennung, damit man sieht, was sie kostet.
CREATE TABLE IF NOT EXISTS nutzung (
  id INTEGER PRIMARY KEY AUTOINCREMENT, wann TEXT NOT NULL, modell TEXT NOT NULL,
  tokens_ein INTEGER NOT NULL DEFAULT 0, tokens_aus INTEGER NOT NULL DEFAULT 0, ok INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS fehlversuche (schluessel TEXT NOT NULL, wann REAL NOT NULL);
-- Bilder optimieren (02.10.2026): je ORIGINALFOTO, ob es aufbereitet (neu = das aufbereitete Foto)
-- oder übersprungen wurde, und mit welchen Einstellungen -- damit „Anpassen" später dort weitermacht.
CREATE TABLE IF NOT EXISTS aufbereitung (
  original TEXT PRIMARY KEY, neu TEXT, sache_id INTEGER, stand TEXT NOT NULL,
  einstellungen TEXT NOT NULL DEFAULT '{}', am TEXT NOT NULL
);
-- Projekte mit Stückliste (Daniel, 03.10.2026). Ein Teil zeigt auf eine Sache im Bestand ODER ist freier
-- Text (noch zu kaufen). `erledigt` = bereitgelegt. Wird eine Sache gelöscht, bleibt das Teil mit ihrem Namen.
CREATE TABLE IF NOT EXISTS projekte (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, notiz TEXT NOT NULL DEFAULT '',
  angelegt_am TEXT NOT NULL, geaendert_am TEXT NOT NULL, fertig_am TEXT
);
CREATE TABLE IF NOT EXISTS projekt_teile (
  id INTEGER PRIMARY KEY AUTOINCREMENT, projekt_id INTEGER NOT NULL REFERENCES projekte(id) ON DELETE CASCADE,
  sache_id INTEGER, text TEXT NOT NULL DEFAULT '', menge TEXT NOT NULL DEFAULT '', notiz TEXT NOT NULL DEFAULT '',
  link TEXT NOT NULL DEFAULT '', erledigt INTEGER NOT NULL DEFAULT 0, nr INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS i_projekt_teile ON projekt_teile(projekt_id);
"""


def jetzt_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Das Salz heißt noch wie die App vor der Umbenennung in „Fundus" (03.10.2026) und MUSS so bleiben:
# der gespeicherte Passwort-Streuwert wurde damit gebildet, ein neues Salz sperrte jeden aus.
def streuwert(passwort: str, salz: str = "werkstatt") -> str:
    return hashlib.pbkdf2_hmac("sha256", passwort.encode(), salz.encode(), 120_000).hex()


def verbindung() -> sqlite3.Connection:
    os.makedirs(DATEN, exist_ok=True)
    v = sqlite3.connect(PFAD, timeout=10, check_same_thread=False)
    v.row_factory = sqlite3.Row
    v.execute("PRAGMA journal_mode=WAL")
    v.execute("PRAGMA foreign_keys=ON")
    return v


class JeThread:
    """Eine Verbindung je Thread -- FastAPI arbeitet mit einem Thread-Pool, und eine
    geteilte Verbindung gibt unter Last falsche Ergebnisse (bei MachIchGleich am
    13.09.2026 als „500 bei jedem x-ten Aufruf" aufgefallen)."""

    def __init__(self) -> None:
        self._lokal = threading.local()

    @property
    def v(self) -> sqlite3.Connection:
        if not hasattr(self._lokal, "verbindung") or self._lokal.pfad != PFAD:
            self._lokal.verbindung = verbindung()
            self._lokal.pfad = PFAD
        return self._lokal.verbindung

    def execute(self, *a):
        return self.v.execute(*a)

    def commit(self):
        self.v.commit()


V = JeThread()


def einrichten(erstpasswort: str | None) -> None:
    os.makedirs(FOTOS, exist_ok=True)
    V.v.executescript(SCHEMA)
    # Nachträglich dazugekommen (01.10.2026): der Shop-Link zum Nachkaufen.
    spalten = {z["name"] for z in V.execute("PRAGMA table_info(sachen)").fetchall()}
    if "link" not in spalten:
        V.execute("ALTER TABLE sachen ADD COLUMN link TEXT NOT NULL DEFAULT ''")
    # Wie viele Stück gerade draußen sind (Daniel, 01.10.2026: bei Menge > 1 die Anzahl abfragen).
    if "raus_anzahl" not in spalten:
        V.execute("ALTER TABLE sachen ADD COLUMN raus_anzahl INTEGER")
    # Mehrere Fotos je Sache (Daniel, 01.10.2026), Reihenfolge als JSON-Liste; das erste ist das Titelbild
    # und steht weiter auch in `foto`, damit alles, was nur ein Bild zeigt, unverändert bleibt.
    if "notiz" not in spalten:
        # Freie Notiz zur Sache (Daniel, 01.10.2026: Katalog „… Notiz hinzufügen")
        V.execute("ALTER TABLE sachen ADD COLUMN notiz TEXT NOT NULL DEFAULT ''")
    if "fotos" not in spalten:
        V.execute("ALTER TABLE sachen ADD COLUMN fotos TEXT NOT NULL DEFAULT '[]'")
        V.execute("UPDATE sachen SET fotos = json_array(foto) WHERE foto IS NOT NULL AND foto <> ''")
    # Kistenart (Daniel, 02.10.2026: „die Boxen sind alle IKEA UPPSNOFSAD"), damit die App sie wie im Regal zeigt
    kspalten = {z["name"] for z in V.execute("PRAGMA table_info(kisten)").fetchall()}
    if "art" not in kspalten:
        V.execute("ALTER TABLE kisten ADD COLUMN art TEXT NOT NULL DEFAULT ''")
    # Räume (Daniel, 03.10.2026: „ergänze Räume. In einem Raum kann man wie bisher verschiedene Stapel haben"):
    # ein Stapel gehört zu genau einem Raum, „A" darf es in jedem Raum geben. Leer = kein Raum zugeordnet.
    if "raum" not in kspalten:
        V.execute("ALTER TABLE kisten ADD COLUMN raum TEXT NOT NULL DEFAULT ''")
    # „Kiste ist voll" (Daniel, 05.10.2026): von Hand gesetzt, die App warnt dann beim Einräumen
    if "voll" not in kspalten:
        V.execute("ALTER TABLE kisten ADD COLUMN voll INTEGER NOT NULL DEFAULT 0")
    # Regale (Daniel, 05.10.2026): je Raum Regale mit Reihen; jede Reihe hat eigene Plätze nebeneinander und
    # Kisten übereinander. `reihen` = JSON [{"breit": 2, "hoch": 1}, ...], Reihe 1 = unten. Eine Kiste im Regal hat
    # regal_id + reihe + platz + ebene (1 = unten; `hoehe` ist schon die Kistenhöhe in mm), Stapel bleibt dann leer.
    V.execute("CREATE TABLE IF NOT EXISTS regale (id INTEGER PRIMARY KEY AUTOINCREMENT, raum TEXT NOT NULL DEFAULT '', "
              "name TEXT NOT NULL, nr INTEGER NOT NULL DEFAULT 0, reihen TEXT NOT NULL DEFAULT '[]', UNIQUE(raum, name))")
    for spalte in ("regal_id INTEGER", "reihe INTEGER NOT NULL DEFAULT 0", "platz INTEGER NOT NULL DEFAULT 0",
                   "ebene INTEGER NOT NULL DEFAULT 0"):
        if spalte.split()[0] not in kspalten:
            V.execute(f"ALTER TABLE kisten ADD COLUMN {spalte}")
    # Eigene Tabelle, damit ein Raum auch ohne Kiste existiert und eine feste Reihenfolge hat.
    V.execute("CREATE TABLE IF NOT EXISTS raeume (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, "
              "nr INTEGER NOT NULL DEFAULT 0)")
    # Bereiche einheitlich schreiben (erster Buchstabe groß), damit „elektro" nicht neben „Elektro" steht
    for z in V.execute("SELECT id, bereich FROM sachen WHERE bereich<>''").fetchall():
        b = z["bereich"].strip()
        neu = b[:1].upper() + b[1:]
        if neu != z["bereich"]:
            V.execute("UPDATE sachen SET bereich=? WHERE id=?", (neu, z["id"]))
    # Einstellungen, die man in der App ändern kann (01.11.2026): der OpenAI-Schlüssel und das Modell.
    V.execute("CREATE TABLE IF NOT EXISTS einstellungen (schluessel TEXT PRIMARY KEY, wert TEXT NOT NULL)")
    if erstpasswort and not V.execute("SELECT 1 FROM konto").fetchone():
        V.execute("INSERT INTO konto (id, passwort) VALUES (1, ?)", (streuwert(erstpasswort),))
    V.commit()


def neues_token() -> str:
    return secrets.token_urlsafe(32)


def einstellung(schluessel: str) -> str:
    z = V.execute("SELECT wert FROM einstellungen WHERE schluessel=?", (schluessel,)).fetchone()
    return z["wert"] if z else ""


def einstellung_setzen(schluessel: str, wert: str) -> None:
    if wert:
        V.execute("INSERT INTO einstellungen (schluessel, wert) VALUES (?, ?) "
                  "ON CONFLICT(schluessel) DO UPDATE SET wert=excluded.wert", (schluessel, wert))
    else:
        V.execute("DELETE FROM einstellungen WHERE schluessel=?", (schluessel,))
    V.commit()
