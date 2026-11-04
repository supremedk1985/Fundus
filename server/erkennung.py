"""Bilderkennung über die OpenAI-API (Entscheidung vom 01.10.2026).

Offline auf einem Heimserver ohne Grafikkarte wäre es zu langsam und zu ungenau: ein kleines
Bildmodell erkennt „Schraube", aber nicht „Senkkopfschraube M5 x 20 Torx" -- und genau
diese Genauigkeit macht die Suche später brauchbar.

Der Schlüssel liegt NUR auf dem Server, nie dauerhaft in der App: entweder in der Server-Datei
(OPENAI_API_KEY) oder -- seit 01.11.2026 -- in der App eingetragen und in der Datenbank des Servers
gespeichert (Tabelle `einstellungen`). Die Server-Datei geht vor. Fehlt beides, arbeitet alles andere
weiter; die App trägt dann von Hand ein.

DEMO=1 (der Demo-Server für Googles Prüfer): keine Anfrage an OpenAI, sondern eine feste Beispielantwort.

Seit 03.11.2026 optional ZUERST eine zweite Erkennung (CLAUDE_KI_URL + CLAUDE_KI_SCHLUESSEL, nur in Daniels compose.yaml):
ein Dienst im Heimnetz, der Claude fragt. Antwortet er nicht, meldet er einen Fehler oder ist die Antwort unbrauchbar
(kein Name), fragt der Server wie bisher OpenAI. Daniel: „… dich als KI nehmen … und nur wenn du nicht erreichbar bist
oder was nicht stimmt, dann die OpenAI." Probe an 20 seiner Fotos: Claude Sonnet 19 richtig, gpt-4.1-mini 11.
Ohne die beiden Variablen (öffentliche Fassung, Tests) ändert sich nichts.
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

    def __init__(self, meldung: str, status: int = 502, versuche: list | None = None):
        super().__init__(meldung)
        self.meldung, self.status = meldung, status
        self.versuche = versuche or []     # was vorher versucht wurde (Claude), für die Nutzungstabelle


def schluessel_der_datei() -> str:
    return (os.environ.get("OPENAI_API_KEY") or "").strip()


def schluessel() -> str:
    return schluessel_der_datei() or db.einstellung("openai_schluessel")


def quelle() -> str:
    """„datei" (Server-Datei, in der App nicht änderbar), „app" (in der App eingetragen) oder „"."""
    if schluessel_der_datei():
        return "datei"
    return "app" if db.einstellung("openai_schluessel") else ""


def claude_url() -> str:
    return (os.environ.get("CLAUDE_KI_URL") or "").strip()


def claude_modell() -> str:
    return (os.environ.get("CLAUDE_KI_MODELL") or "sonnet").strip()


def claude_eingerichtet() -> bool:
    return not demo() and bool(claude_url() and (os.environ.get("CLAUDE_KI_SCHLUESSEL") or "").strip())


def eingerichtet() -> bool:
    return demo() or bool(schluessel()) or claude_eingerichtet()


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
    "required": ["name", "beschreibung", "bereich", "menge", "woerter", "sicherheit", "merkmale"],
    "properties": {
        "name": {"type": "string"},
        "beschreibung": {"type": "string"},
        "bereich": {"type": "string"},
        "menge": {"type": "string"},
        "woerter": {"type": "array", "items": {"type": "string"}},
        "sicherheit": {"type": "string", "enum": ["hoch", "mittel", "niedrig"]},
        # Steckbrief (03.11.2026, Entwurf „Fundus 2"): technische Eckdaten als Paare
        "merkmale": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                "required": ["name", "wert"],
                                                "properties": {"name": {"type": "string"}, "wert": {"type": "string"}}}},
    },
}

MERKMALE_DE = ("- merkmale: 0 bis 6 technische Eckdaten als Paare name/wert, die beim Wiederfinden oder Bauen "
               "helfen (z. B. Typ: FS1000A, Frequenz: 433 MHz, Spannung: 3,3–5 V, Schnittstelle: I²C, Länge: 5 m, "
               "Gewinde: M10). Nur was sicher erkennbar ist -- nichts erfinden; lieber weglassen.")
MERKMALE_EN = ("- merkmale: 0 to 6 technical key facts as name/wert pairs that help to find or build with it "
               "(e.g. Type: FS1000A, Frequency: 433 MHz, Voltage: 3.3–5 V, Interface: I²C, Length: 5 m). "
               "Only what is clearly recognisable -- never invent; rather leave it out.")


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
        "- sicherheit: wie sicher du bei name bist.\n" + MERKMALE_DE
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
        "- sicherheit: how sure you are about name (hoch, mittel or niedrig).\n" + MERKMALE_EN
    )


def _demo_vorschlag(en: bool) -> tuple[dict, dict]:
    """Feste Antwort für den Demo-Server: keine Kosten, kein Schlüssel nötig. Steht so auch im Text für die Prüfer."""
    if en:
        v = {"name": "Lever connector, 3-pole (demo)", "beschreibung":
             "Demo server: every photo gets this sample answer. With your own server and OpenAI key the real "
             "recognition answers here.", "bereich": "Electrical", "menge": "about 10",
             "woerter": ["connector", "terminal block", "lever clamp", "wiring", "junction"], "sicherheit": "mittel",
             "merkmale": [{"name": "Poles", "wert": "3"}]}
    else:
        v = {"name": "Hebelklemme 3-polig (Demo)", "beschreibung":
             "Demo-Server: Jedes Foto bekommt diese Beispielantwort. Mit eigenem Server und OpenAI-Schlüssel "
             "antwortet hier die echte Erkennung.", "bereich": "Elektro", "menge": "ca. 10",
             "woerter": ["verbindungsklemme", "lüsterklemme", "hebelklemme", "verdrahten", "abzweig"],
             "sicherheit": "mittel", "merkmale": [{"name": "Pole", "wert": "3"}]}
    return v, {"modell": "demo", "tokens_ein": 0, "tokens_aus": 0}


def _bild_als_daten_url(pfad: str, kante: int = 1024) -> str:
    with Image.open(pfad) as b:
        b = b.convert("RGB")
        b.thumbnail((kante, kante))
        puffer = io.BytesIO()
        b.save(puffer, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(puffer.getvalue()).decode()


class _KeinClaude(Exception):
    """Claude hat nicht (brauchbar) geantwortet -- dann fragt erkennen() OpenAI."""

    def __init__(self, grund: str):
        super().__init__(grund)
        self.grund = grund


def _frage(hinweis: str, produkttext: str, en: bool) -> str:
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
    return frage


def bereinigen(v) -> dict:
    """Eine Antwort ohne festes Schema (Claude) in die Form der OpenAI-Antwort bringen -- oder ablehnen."""
    if not isinstance(v, dict):
        raise _KeinClaude("Antwort ist kein Objekt")
    name = str(v.get("name") or "").strip()
    if not name:
        raise _KeinClaude("Antwort ohne Namen")
    woerter = v.get("woerter") or []
    if isinstance(woerter, str):
        woerter = woerter.split(",")
    sicherheit = str(v.get("sicherheit") or "").strip().lower()
    return {"name": name[:120], "beschreibung": str(v.get("beschreibung") or "").strip()[:600],
            "bereich": str(v.get("bereich") or "").strip()[:40], "menge": str(v.get("menge") or "").strip()[:60],
            "woerter": [str(w) for w in woerter if str(w).strip()][:15],
            "sicherheit": sicherheit if sicherheit in ("hoch", "mittel", "niedrig") else "mittel",
            "merkmale": merkmale_glaetten(v.get("merkmale"))}


def merkmale_glaetten(roh) -> list[dict]:
    """Steckbrief-Paare säubern: [{name, wert}], höchstens 8, leere und doppelte Namen raus. Nimmt auch {name: wert}."""
    if isinstance(roh, dict):
        roh = [{"name": k, "wert": v} for k, v in roh.items()]
    if not isinstance(roh, list):
        return []
    aus, gesehen = [], set()
    for m in roh:
        if not isinstance(m, dict):
            continue
        n, w = " ".join(str(m.get("name") or "").split())[:40], " ".join(str(m.get("wert") or "").split())[:80]
        if n and w and n.casefold() not in gesehen:
            gesehen.add(n.casefold())
            aus.append({"name": n[:1].upper() + n[1:], "wert": w})
    return aus[:8]


def _claude_rufen(system: str, text: str, bilder: list[str], client: httpx.Client, zeit: float | None = None) -> tuple[object, dict]:
    """Eine Anfrage an den Claude-Dienst (CLAUDE_KI_URL). Gibt (antwort, nutzung) zurück oder wirft _KeinClaude."""
    zeit = zeit or float(os.environ.get("CLAUDE_KI_ZEIT") or 60)
    koerper = {"system": system, "text": text, "modell": claude_modell(), "zeitgrenze_s": zeit, "json": True, "bilder": bilder}
    try:
        r = client.post(claude_url(), json=koerper, timeout=zeit + 15,
                        headers={"X-KI-Schluessel": (os.environ.get("CLAUDE_KI_SCHLUESSEL") or "").strip()})
    except httpx.TimeoutException:
        raise _KeinClaude("keine Antwort in der Zeitgrenze")
    except httpx.HTTPError as e:
        raise _KeinClaude(f"nicht erreichbar ({type(e).__name__})")
    if r.status_code >= 400:
        try:
            grund = str(r.json().get("detail") or "")
        except ValueError:
            grund = ""
        raise _KeinClaude(f"HTTP {r.status_code} {grund}".strip())
    try:
        d = r.json()
    except ValueError:
        raise _KeinClaude("Antwort unlesbar")
    return d.get("antwort"), {"modell": str(d.get("modell") or f"claude-{claude_modell()}"),
                              "tokens_ein": int(d.get("tokens_ein") or 0), "tokens_aus": int(d.get("tokens_aus") or 0)}


def _claude(bildpfade: list[str], bereiche: list[str], frage: str, en: bool, client: httpx.Client) -> tuple[dict, dict]:
    system = anweisung(bereiche, en) + (
        "\n\nAntworte AUSSCHLIESSLICH mit einem JSON-Objekt, ohne Zaun und ohne Text davor oder danach, mit genau "
        "diesen Schlüsseln: name, beschreibung, bereich, menge, woerter (Liste), sicherheit (hoch, mittel oder niedrig), "
        "merkmale (Liste von Objekten mit name und wert).")
    antwort, nutzung = _claude_rufen(system, frage, [_bild_als_daten_url(p).split(",", 1)[1] for p in bildpfade], client)
    vorschlag = bereinigen(antwort)
    vorschlag["woerter"] = sorted({w.strip().lower() for w in vorschlag["woerter"] if w.strip()})
    return vorschlag, nutzung


def frage_json(system: str, text: str, schema: dict, name: str, client: httpx.Client | None = None,
               zeit: float = 150) -> tuple[dict, dict]:
    """Eine Textfrage mit JSON-Antwort (Bauen, Arten, Steckbriefe; 03.11.2026): erst Claude, sonst OpenAI mit festem
    Schema. Gibt (antwort, nutzung wie bei erkennen) zurück; Fehler als ErkennungsFehler."""
    if demo():
        raise ErkennungsFehler("Auf dem Demo-Server gibt es diese KI-Hilfe nicht.", 503)
    versuche: list[dict] = []
    eigener = client is None
    c = client or httpx.Client(timeout=zeit + 15)
    try:
        if claude_eingerichtet():
            try:
                antwort, nutzung = _claude_rufen(system + "\n\nAntworte AUSSCHLIESSLICH mit einem JSON-Objekt nach diesem "
                                                 "Schema, ohne Zaun und ohne Text davor oder danach:\n"
                                                 + json.dumps(schema, ensure_ascii=False), text, [], c, zeit)
                if not isinstance(antwort, dict):
                    raise _KeinClaude("Antwort ist kein Objekt")
                nutzung.update(versuche=[], rueckfall="")
                return antwort, nutzung
            except _KeinClaude as k:
                versuche.append({"modell": f"claude-{claude_modell()}", "grund": k.grund})
        if not schluessel():
            raise ErkennungsFehler("Die KI ist nicht eingerichtet oder gerade nicht erreichbar.", 503, versuche)
        anfrage = {"model": modell(), "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}],
                   "response_format": {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}}}
        try:
            r = c.post(API, json=anfrage, headers={"Authorization": f"Bearer {schluessel()}"}, timeout=zeit)
        except httpx.HTTPError:
            raise ErkennungsFehler("Der Server erreicht die KI gerade nicht (Internet?).", 502, versuche)
        if r.status_code >= 400:
            raise ErkennungsFehler(f"Die Bilderkennung meldet einen Fehler ({r.status_code}).", 502, versuche)
        d = r.json()
        try:
            antwort = json.loads(d["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError, ValueError):
            raise ErkennungsFehler("Die Antwort der Bilderkennung war unlesbar.", 502, versuche)
        n = d.get("usage") or {}
        return antwort, {"modell": d.get("model", modell()), "tokens_ein": int(n.get("prompt_tokens") or 0),
                         "tokens_aus": int(n.get("completion_tokens") or 0), "versuche": versuche,
                         "rueckfall": versuche[0]["grund"] if versuche else ""}
    finally:
        if eigener:
            c.close()


def erkennen(bildpfade: list[str], bereiche: list[str], hinweis: str = "",
             client: httpx.Client | None = None, produkttext: str = "", en: bool = False) -> tuple[dict, dict]:
    """Gibt (vorschlag, nutzung) zurück. nutzung = {modell, tokens_ein, tokens_aus, versuche, rueckfall}.
    `versuche`: gescheiterte Claude-Anfragen davor (für die Nutzungstabelle). `en`: die App läuft auf Englisch."""
    if demo():
        return _demo_vorschlag(en)
    frage = _frage(hinweis, produkttext, en)
    versuche: list[dict] = []
    eigener = client is None
    c = client or httpx.Client(timeout=60)
    try:
        if claude_eingerichtet():
            try:
                vorschlag, nutzung = _claude(bildpfade, bereiche, frage, en, c)
                nutzung.update(versuche=[], rueckfall="")
                return vorschlag, nutzung
            except _KeinClaude as k:
                versuche.append({"modell": f"claude-{claude_modell()}", "grund": k.grund})
        if not schluessel():
            if versuche:
                raise ErkennungsFehler("Claude antwortet gerade nicht, und OpenAI ist nicht eingerichtet. "
                                       "Gleich nochmal versuchen oder von Hand erfassen.", 503, versuche)
            raise ErkennungsFehler("Die Bilderkennung ist nicht eingerichtet: Es fehlt ein OpenAI-Schlüssel. Du kannst "
                                   "ihn in der App unter „Mehr“ → „Bilderkennung“ eintragen oder die Sache von Hand erfassen.", 503)
        try:
            vorschlag, nutzung = _openai(bildpfade, frage, bereiche, en, c)
        except ErkennungsFehler as f:
            f.versuche = versuche + f.versuche
            raise
        nutzung.update(versuche=versuche, rueckfall=versuche[0]["grund"] if versuche else "")
        return vorschlag, nutzung
    finally:
        if eigener:
            c.close()


def _openai(bildpfade: list[str], frage: str, bereiche: list[str], en: bool, c: httpx.Client) -> tuple[dict, dict]:
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
    try:
        r = c.post(API, json=anfrage, headers={"Authorization": f"Bearer {schluessel()}"}, timeout=60)
    except httpx.TimeoutException:
        raise ErkennungsFehler("Die Bilderkennung hat nicht rechtzeitig geantwortet. Nochmal versuchen?", 504)
    except httpx.HTTPError:
        raise ErkennungsFehler("Der Server erreicht die Bilderkennung gerade nicht (Internet?).", 502)
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
    vorschlag["merkmale"] = merkmale_glaetten(vorschlag.get("merkmale"))
    return vorschlag, {"modell": d.get("model", modell()),
                       "tokens_ein": int(nutzung.get("prompt_tokens") or 0),
                       "tokens_aus": int(nutzung.get("completion_tokens") or 0)}
