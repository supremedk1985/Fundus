"""Bilderkennung über die OpenAI-API (Entscheidung vom 01.10.2026).

Offline auf einem Heimserver ohne Grafikkarte wäre es zu langsam und zu ungenau: ein kleines
Bildmodell erkennt „Schraube", aber nicht „Senkkopfschraube M5 x 20 Torx" -- und genau
diese Genauigkeit macht die Suche später brauchbar.

Der Schlüssel liegt NUR auf dem Server, nie dauerhaft in der App: entweder in der Server-Datei
(OPENAI_API_KEY) oder -- seit 01.11.2026 -- in der App eingetragen und in der Datenbank des Servers
gespeichert (Tabelle `einstellungen`). Die Server-Datei geht vor. Fehlt beides, arbeitet alles andere
weiter; die App trägt dann von Hand ein.

DEMO=1 (der Demo-Server für Googles Prüfer): keine Anfrage an OpenAI, sondern eine feste Beispielantwort.
"""
from __future__ import annotations
import base64, io, json, os
import httpx
from PIL import Image

import db

API = os.environ.get("OPENAI_API_URL", "https://api.openai.com/v1/chat/completions")
MODELLE_API = os.environ.get("OPENAI_MODELLE_URL", "https://api.openai.com/v1/models")
VORGABE_MODELL = "gpt-4.1-mini"


def demo() -> bool:
    return os.environ.get("DEMO", "") == "1"


def modell() -> str:
    return db.einstellung("openai_modell") or (os.environ.get("OPENAI_MODELL") or "").strip() or VORGABE_MODELL


class ErkennungsFehler(Exception):
    """Mit einer Meldung, die die App so anzeigen kann."""

    def __init__(self, meldung: str, status: int = 502):
        super().__init__(meldung)
        self.meldung, self.status = meldung, status


def schluessel_der_datei() -> str:
    return (os.environ.get("OPENAI_API_KEY") or "").strip()


def schluessel() -> str:
    return schluessel_der_datei() or db.einstellung("openai_schluessel")


def quelle() -> str:
    """„datei" (Server-Datei, in der App nicht änderbar), „app" (in der App eingetragen) oder „"."""
    if schluessel_der_datei():
        return "datei"
    return "app" if db.einstellung("openai_schluessel") else ""


def eingerichtet() -> bool:
    return demo() or bool(schluessel())


def schluessel_pruefen(wert: str, client: httpx.Client | None = None) -> bool | None:
    """True = OpenAI nimmt ihn an, False = abgelehnt (401), None = nicht prüfbar (Netz)."""
    eigener = client is None
    c = client or httpx.Client(timeout=15)
    try:
        r = c.get(MODELLE_API, headers={"Authorization": f"Bearer {wert}"})
    except httpx.HTTPError:
        return None
    finally:
        if eigener:
            c.close()
    if r.status_code in (401, 403):
        return False
    return True if r.status_code < 400 else None


SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "beschreibung", "bereich", "menge", "woerter", "sicherheit"],
    "properties": {
        "name": {"type": "string"},
        "beschreibung": {"type": "string"},
        "bereich": {"type": "string"},
        "menge": {"type": "string"},
        "woerter": {"type": "array", "items": {"type": "string"}},
        "sicherheit": {"type": "string", "enum": ["hoch", "mittel", "niedrig"]},
    },
}


def anweisung(bereiche: list[str], en: bool = False) -> str:
    if en:
        return anweisung_en(bereiche)
    vorhanden = ", ".join(bereiche) if bereiche else "(noch keine)"
    return (
        "Du hilfst, Kleinteile einer privaten Heimwerker-Werkstatt zu erfassen, die in "
        "Kisten eingelagert werden. Auf dem Foto liegt EINE Sache (oder eine Packung/ein "
        "Sortiment davon). Antworte auf Deutsch.\n"
        "- name: so genau wie erkennbar, mit Marke, Typ und Größe, wenn sichtbar "
        "(z. B. „Wago-Verbindungsklemme 221-413“, „Senkkopfschraube M5 × 20 Torx“). "
        "Kein Satz, höchstens 60 Zeichen.\n"
        "- beschreibung: ein kurzer Satz mit den wichtigsten Merkmalen (Maße, Material, Anschluss).\n"
        f"- bereich: der Oberbegriff. Nimm möglichst einen vorhandenen: {vorhanden}. "
        "Nur wenn keiner passt, einen neuen kurzen (z. B. Elektro, Schrauben, Werkzeug, "
        "Kleben, Sanitär, Holz, Fahrrad, Garten).\n"
        "- menge: ungefähre Stückzahl oder Packungsangabe, z. B. „ca. 25“, „1 Sortimentskasten“, "
        "„1“. Leer, wenn nicht schätzbar.\n"
        "- woerter: 5 bis 12 Suchwörter, klein geschrieben: Synonyme, umgangssprachliche "
        "Namen (z. B. „lüsterklemme“) UND wofür man es braucht (z. B. „abisolieren“, "
        "„kleben“). Keine Wörter, die schon wortgleich im Namen stehen.\n"
        "- sicherheit: wie sicher du bei name bist."
    )


def anweisung_en(bereiche: list[str]) -> str:
    vorhanden = ", ".join(bereiche) if bereiche else "(none yet)"
    return (
        "You help to record small parts of a private home workshop that are stored in boxes. "
        "The photo shows ONE thing (or a pack/assortment of it). Answer in English.\n"
        "- name: as precise as recognisable, with brand, type and size if visible "
        "(e.g. “Wago lever connector 221-413”, “Countersunk screw M5 × 20 Torx”). "
        "Not a sentence, at most 60 characters.\n"
        "- beschreibung: one short sentence with the most important features (size, material, connection).\n"
        f"- bereich: the category. Prefer an existing one: {vorhanden}. "
        "Only if none fits, a new short one (e.g. Electrical, Screws, Tools, Glue, Plumbing, Wood, Bicycle, Garden).\n"
        "- menge: approximate quantity or pack size, e.g. “about 25”, “1 assortment box”, “1”. "
        "Empty if it cannot be estimated.\n"
        "- woerter: 5 to 12 search words in lower case: synonyms, colloquial names (e.g. “terminal block”) "
        "AND what it is used for (e.g. “stripping”, “gluing”). No words that already appear in the name.\n"
        "- sicherheit: how sure you are about name (hoch, mittel or niedrig)."
    )


def _demo_vorschlag(en: bool) -> tuple[dict, dict]:
    """Feste Antwort für den Demo-Server: keine Kosten, kein Schlüssel nötig. Steht so auch im Text für die Prüfer."""
    if en:
        v = {"name": "Wago lever connector 221-413 (demo)", "beschreibung":
             "Demo server: every photo gets this sample answer. With your own server and OpenAI key the real "
             "recognition answers here.", "bereich": "Electrical", "menge": "about 10",
             "woerter": ["connector", "terminal block", "lever clamp", "wiring", "junction"], "sicherheit": "mittel"}
    else:
        v = {"name": "Wago-Hebelklemme 221-413 (Demo)", "beschreibung":
             "Demo-Server: Jedes Foto bekommt diese Beispielantwort. Mit eigenem Server und OpenAI-Schlüssel "
             "antwortet hier die echte Erkennung.", "bereich": "Elektro", "menge": "ca. 10",
             "woerter": ["verbindungsklemme", "lüsterklemme", "hebelklemme", "verdrahten", "abzweig"],
             "sicherheit": "mittel"}
    return v, {"modell": "demo", "tokens_ein": 0, "tokens_aus": 0}


def _bild_als_daten_url(pfad: str, kante: int = 1024) -> str:
    with Image.open(pfad) as b:
        b = b.convert("RGB")
        b.thumbnail((kante, kante))
        puffer = io.BytesIO()
        b.save(puffer, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(puffer.getvalue()).decode()


def erkennen(bildpfade: list[str], bereiche: list[str], hinweis: str = "",
             client: httpx.Client | None = None, produkttext: str = "", en: bool = False) -> tuple[dict, dict]:
    """Gibt (vorschlag, nutzung) zurück. nutzung = {modell, tokens_ein, tokens_aus}.
    `en`: die App läuft auf Englisch -- dann antwortet auch die Erkennung englisch."""
    if demo():
        return _demo_vorschlag(en)
    if not eingerichtet():
        raise ErkennungsFehler("Die Bilderkennung ist nicht eingerichtet: Es fehlt ein OpenAI-Schlüssel. Du kannst "
                               "ihn in der App unter „Mehr“ → „Bilderkennung“ eintragen oder die Sache von Hand erfassen.", 503)
    frage = "Was ist das?" + (f" Hinweis: {hinweis}" if hinweis else "")
    if en:
        frage = "What is this?" + (f" Hint: {hinweis}" if hinweis else "")
    if produkttext and en:
        frage = ("Record this item from its product page. Take over type, size and pack contents, leave out "
                 "advertising language. The quantity is the pack contents.\n\n" + produkttext)
    elif produkttext:
        # Aus einem Shop-Link: die Angaben der Produktseite zählen mehr als das Bild,
        # Werbesprache („QUALITÄT SEIT 1951") gehört nicht in die Beschreibung.
        frage = ("Erfasse diese Sache anhand ihrer Produktseite. Übernimm Typ, Größe und Packungsinhalt, "
                 "lass Werbesprache weg. Die Menge ist der Packungsinhalt.\n\n" + produkttext)
    inhalt: list[dict] = [{"type": "text", "text": frage}]
    for p in bildpfade:
        inhalt.append({"type": "image_url", "image_url": {"url": _bild_als_daten_url(p), "detail": "auto"}})
    anfrage = {
        "model": modell(),
        "messages": [{"role": "system", "content": anweisung(bereiche, en)},
                     {"role": "user", "content": inhalt}],
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "sache", "strict": True, "schema": SCHEMA}},
    }
    eigener = client is None
    c = client or httpx.Client(timeout=60)
    try:
        r = c.post(API, json=anfrage, headers={"Authorization": f"Bearer {schluessel()}"})
    except httpx.TimeoutException:
        raise ErkennungsFehler("Die Bilderkennung hat nicht rechtzeitig geantwortet. Nochmal versuchen?", 504)
    except httpx.HTTPError:
        raise ErkennungsFehler("Der Server erreicht die Bilderkennung gerade nicht (Internet?).", 502)
    finally:
        if eigener:
            c.close()
    if r.status_code == 401:
        raise ErkennungsFehler("Der OpenAI-Schlüssel wird abgelehnt.", 502)
    if r.status_code == 429:
        art = ""
        try:
            art = r.json().get("error", {}).get("code") or ""
        except ValueError:
            pass
        if art == "insufficient_quota":
            raise ErkennungsFehler("Das OpenAI-Guthaben ist aufgebraucht.", 502)
        raise ErkennungsFehler("Die Bilderkennung ist gerade überlastet. Gleich nochmal versuchen.", 503)
    if r.status_code >= 400:
        raise ErkennungsFehler(f"Die Bilderkennung meldet einen Fehler ({r.status_code}).", 502)
    d = r.json()
    try:
        vorschlag = json.loads(d["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError, ValueError):
        raise ErkennungsFehler("Die Antwort der Bilderkennung war unlesbar.", 502)
    nutzung = d.get("usage") or {}
    vorschlag["woerter"] = sorted({w.strip().lower() for w in vorschlag.get("woerter", []) if w.strip()})
    return vorschlag, {"modell": d.get("model", modell()),
                       "tokens_ein": int(nutzung.get("prompt_tokens") or 0),
                       "tokens_aus": int(nutzung.get("completion_tokens") or 0)}
