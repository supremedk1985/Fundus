"""Meldungen des Servers auf Englisch (seit 01.11.2026, für die Fassung bei Google Play).

Der Server schreibt seine Meldungen weiter auf Deutsch. Schickt die App `Accept-Language: en…`, übersetzt
`uebersetzen()` sie auf dem Rückweg: feste Sätze über das Wörterbuch, Sätze mit eingesetzten Werten über ein
Muster. Ein Satz, der hier fehlt, bleibt deutsch -- das ist sichtbar, aber nicht falsch. Der Test
`test_alle_meldungen_haben_englisch` liest alle Meldungen aus dem Code und meldet fehlende.
"""
from __future__ import annotations
import re

FEST = {
    "Aus dem Link ließ sich nichts lesen. Du kannst die Sache von Hand eintragen.":
        "Nothing could be read from the link. You can enter the item by hand.",
    "Claude antwortet gerade nicht, und OpenAI ist nicht eingerichtet. Gleich nochmal versuchen oder von Hand erfassen.":
        "Claude is not answering right now and OpenAI is not set up. Try again shortly or enter the item by hand.",
    "Es fehlen Gruppe oder Sachen": "Group or items are missing",
    "ziel und quellen müssen Zahlen sein": "ziel and quellen must be numbers",
    "Eine Art braucht einen Namen": "A category needs a name",
    "Es gibt noch keine Sachen": "There are no items yet",
    "Beschreib kurz, was du bauen willst": "Describe briefly what you want to build",
    "Auf dem Demo-Server gibt es diese KI-Hilfe nicht.": "This AI help is not available on the demo server.",
    "Die KI ist nicht eingerichtet oder gerade nicht erreichbar.": "The AI is not set up or not reachable right now.",
    "Der Server erreicht die KI gerade nicht (Internet?).": "The server cannot reach the AI right now (internet?).",
    "Das Foto gehört nicht zu dieser Sache": "The photo does not belong to this item",
    "Das Foto gibt es auf dem Server nicht": "The photo does not exist on the server",
    "Das Foto ist größer als 25 MB": "The photo is larger than 25 MB",
    "Das Freistell-Modell fehlt auf dem Server": "The cut-out model is missing on the server",
    "Das Passwort stimmt nicht": "Wrong password",
    "Das Projekt braucht einen Namen": "The project needs a name",
    "Das Projekt gibt es nicht mehr": "The project no longer exists",
    "Das Regal braucht einen Namen": "The shelf needs a name",
    "Das Regal gibt es nicht": "The shelf does not exist",
    "Das Teil gibt es nicht mehr": "The part no longer exists",
    "Das ist kein lesbares Bild": "This is not a readable image",
    "Den Raum gibt es nicht": "The room does not exist",
    "Der Raum braucht einen Namen": "The room needs a name",
    "Die Anmeldung gilt nicht mehr. Bitte neu anmelden.": "Your sign-in has expired. Please sign in again.",
    "Die Menge hat keine Zahl, die man senken könnte": "The quantity has no number that could be reduced",
    "Die Sache braucht einen Namen": "The item needs a name",
    "Die Sache gibt es nicht mehr": "The item no longer exists",
    "Diesen Vorschlag gibt es nicht": "This suggestion does not exist",
    "Diesen Vorschlag gibt es nicht (mehr)": "This suggestion does not exist (any more)",
    "Die KI ist nicht eingerichtet.": "The AI is not set up.",
    "Der Name wurde inzwischen geändert -- der Vorschlag passt nicht mehr": "The name has been changed in the meantime -- the suggestion no longer fits",
    "Diesen Vorschlag gibt es nicht mehr": "This suggestion no longer exists",
    "Diesen Vorschlag gibt es nicht mehr -- bitte neu rechnen": "This suggestion no longer exists -- please compute it again",
    "Ein Foto gibt es auf dem Server nicht": "A photo does not exist on the server",
    "Ein Regal hat 1 bis 8 Reihen": "A shelf has 1 to 8 rows",
    "Ein Teil braucht eine Sache aus dem Bestand oder einen Namen": "A part needs an item from your stock or a name",
    "Es gibt noch keine Kisten": "There are no boxes yet",
    "Es wurde kein Link übergeben": "No link was given",
    "Etikettengröße außerhalb von 15–200 mm": "Label size outside 15–200 mm",
    "Foto gibt es nicht": "Photo does not exist",
    "Mindestens ein Stück": "At least one piece",
    "Nicht angemeldet": "Not signed in",
    "Nur im Heimnetz": "Only on the home network",
    "Ohne Foto gibt es nichts zu erkennen": "Without a photo there is nothing to recognize",
    "Regal, Reihe und Platz müssen Zahlen sein": "Shelf, row and place must be numbers",
    "Zu viele falsche Versuche. Bitte in einer Viertelstunde nochmal.":
        "Too many wrong attempts. Please try again in a quarter of an hour.",
    "aktion: raus, zurueck oder verbraucht": "aktion: raus, zurueck or verbraucht",
    "anzahl muss eine Zahl sein": "anzahl must be a number",
    "fotos muss eine Liste sein": "fotos must be a list",
    "grad muss 90, -90, 180 oder 270 sein": "grad must be 90, -90, 180 or 270",
    "sache_id fehlt": "sache_id is missing",
    "umfang ist offen, titel oder alle": "umfang is offen, titel or alle",
    "woerter muss eine Liste sein": "woerter must be a list",
    # Bilderkennung (erkennung.py)
    "Die Bilderkennung ist nicht eingerichtet: Es fehlt ein OpenAI-Schlüssel. Du kannst ihn in der App unter "
    "„Mehr“ → „Bilderkennung“ eintragen oder die Sache von Hand erfassen.":
        "Image recognition is not set up: an OpenAI key is missing. You can enter one in the app under "
        "“More” → “Image recognition”, or enter the item by hand.",
    "Die Bilderkennung hat nicht rechtzeitig geantwortet. Nochmal versuchen?":
        "Image recognition did not answer in time. Try again?",
    "Der Server erreicht die Bilderkennung gerade nicht (Internet?).":
        "The server cannot reach image recognition right now (internet?).",
    "Der OpenAI-Schlüssel wird abgelehnt.": "The OpenAI key is rejected.",
    "Das OpenAI-Guthaben ist aufgebraucht.": "The OpenAI credit is used up.",
    "Die Bilderkennung ist gerade überlastet. Gleich nochmal versuchen.":
        "Image recognition is overloaded right now. Try again in a moment.",
    "Die Antwort der Bilderkennung war unlesbar.": "The answer of image recognition was unreadable.",
    # Hinweise beim Shop-Link
    "Die Bilderkennung ist nicht eingerichtet; übernommen ist nur der Titel.":
        "Image recognition is not set up; only the title was taken over.",
    "Die Produktseite war nicht lesbar, es zählt nur der geteilte Text.":
        "The product page could not be read; only the shared text counts.",
    # Einstellungen der Bilderkennung
    "Im Demo-Betrieb lässt sich der Schlüssel nicht ändern.": "The key cannot be changed in demo mode.",
    "Das sieht nicht wie ein OpenAI-Schlüssel aus (er beginnt mit „sk-“).":
        "This does not look like an OpenAI key (it starts with “sk-”).",
    "Der Schlüssel ist gespeichert, ließ sich aber nicht prüfen (OpenAI nicht erreichbar).":
        "The key is saved but could not be checked (OpenAI not reachable).",
    "Der Schlüssel steht in der Server-Datei (.env) und lässt sich nur dort ändern.":
        "The key is set in the server file (.env) and can only be changed there.",
}

MUSTER = [
    (r"Kiste (.+) gibt es nicht", r"Box \1 does not exist"),
    (r"Reihe (\d+): Plätze und Höhe müssen Zahlen sein", r"Row \1: places and height must be numbers"),
    (r"Reihe (\d+): 1–6 Plätze nebeneinander, 1–4 Kisten übereinander",
     r"Row \1: 1–6 places side by side, 1–4 boxes on top of each other"),
    (r"(\w+) muss eine Zahl in Millimetern sein", r"\1 must be a number in millimeters"),
    (r"(\w+) \((\d+) mm\) ist keine Kistengröße", r"\1 (\2 mm) is not a box size"),
    (r"Den Raum „(.+)“ gibt es schon", r"The room “\1” already exists"),
    (r"Im Raum „(.+)“ stehen noch (\d+) Kisten\. Erst verschieben\.", r"There are still \2 boxes in the room “\1”. Move them first."),
    (r"Im Raum „(.+)“ steht noch das Regal „(.+)“\. Erst das Regal löschen\.",
     r"The shelf “\2” is still in the room “\1”. Delete the shelf first."),
    (r"Den Raum „(.+)“ gibt es nicht", r"The room “\1” does not exist"),
    (r"„(.+)“ hat (\d+) Reihen", r"“\1” has \2 rows"),
    (r"Reihe (\d+) hat (\d+) Plätze", r"Row \1 has \2 places"),
    (r"Auf diesem Platz ist kein Platz mehr \((\d+) übereinander\)", r"This place is full (\1 on top of each other)"),
    (r"Ein Regal „(.+)“ gibt es in diesem Raum schon", r"A shelf “\1” already exists in this room"),
    (r"(.+) stünde dann außerhalb des Regals \(Reihe (\d+), Platz (\d+)\)\. (.*)",
     r"\1 would then stand outside the shelf (row \2, place \3). Move it first."),
    (r"Im Regal „(.+)“ stehen noch (\d+) Kisten\. Erst umstellen\.", r"There are still \2 boxes on the shelf “\1”. Move them first."),
    (r"In (.+) liegen noch (\d+) Sachen\. Erst umräumen oder löschen\.", r"There are still \2 items in \1. Move or delete them first."),
    (r"Ungültige Einstellung: (.*)", r"Invalid setting: \1"),
    (r"Die Sache hat schon (\d+) Fotos -- erst eines entfernen", r"The item already has \1 photos -- remove one first"),
    (r"Zwischen 1 und (\d+)", r"Between 1 and \1"),
    (r"„(.+)“ ist nicht aufgebraucht \((.*)\) – löschen geht nur in der App\.",
     r"“\1” is not used up (\2) – it can only be deleted in the app."),
    (r"Die Bilderkennung meldet einen Fehler \((\d+)\)\.", r"Image recognition reports an error (\1)."),
]
_MUSTER = [(re.compile(a + r"\Z"), b) for a, b in MUSTER]


def englisch(sprache_kopf: str | None) -> bool:
    """`Accept-Language: en-US,en;q=0.9` → True. Deutsch, leer oder sonst etwas → False."""
    return (sprache_kopf or "").strip().lower().startswith("en")


def uebersetzen(text: str, en: bool) -> str:
    if not en or not isinstance(text, str):
        return text
    # Mehrere Hinweise können mit Leerzeichen aneinanderhängen (Shop-Link) -- erst das Ganze, dann satzweise
    if text in FEST:
        return FEST[text]
    for muster, ersatz in _MUSTER:
        if muster.match(text):
            return muster.sub(ersatz, text)
    teile = [t for t in FEST if t in text]
    for t in sorted(teile, key=len, reverse=True):
        text = text.replace(t, FEST[t])
    return text
