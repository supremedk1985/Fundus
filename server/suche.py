"""Suche über die Sachen -- ohne Datenbank-Volltext, weil es um ein paar hundert
Zeilen geht und die Regeln hier lesbar bleiben sollen.

Was sie können muss (aus Daniels Alltag):
  * Umlaute egal: „heisskleber" findet „Heißklebepistole".
  * Wortanfänge reichen: „heisskleber" findet „Heißklebesticks" (gemeinsamer
    Anfang „heisskleb"), „düse" findet „Düsenaufsatz".
  * Füllwörter zählen nicht: „Ding zum Abisolieren" sucht nach „abisolieren".
    Dass das trifft, verdankt sich den Suchwörtern: die Bilderkennung schreibt
    zu jeder Sache auch hin, WOFÜR man sie braucht.
  * Ein Tippfehler darf durchgehen („abisoliern").
"""
from __future__ import annotations
import difflib, json, re

FUELLWOERTER = set("""
der die das den dem des ein eine einer eines einem einen und oder mit ohne fuer zum zur
von vom im in am an auf aus bei fuer ding dinger teil teile sache sachen zeug so sowas
was wo ist sind wie mein meine kleine kleiner kleines grosse grosser grosses
""".split())

_ERSATZ = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def normalisieren(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower().translate(_ERSATZ)).strip()


def woerter_von(text: str) -> list[str]:
    return [w for w in normalisieren(text).split() if w]


def suchbegriffe(anfrage: str) -> list[str]:
    ws = woerter_von(anfrage)
    ohne = [w for w in ws if w not in FUELLWOERTER]
    return ohne or ws


def passt(begriff: str, wort: str) -> float:
    """0 = gar nicht, 1 = genau. Zwischenwerte für Anfang, Teilwort, Tippfehler."""
    if begriff == wort:
        return 1.0
    if len(begriff) >= 2 and wort.startswith(begriff):
        return 0.9
    if len(begriff) >= 4 and begriff in wort:
        return 0.8
    # gemeinsamer Wortanfang: „heisskleber" ~ „heissklebesticks"
    n = 0
    for a, b in zip(begriff, wort):
        if a != b:
            break
        n += 1
    kuerzer = min(len(begriff), len(wort))
    if n >= 5 and n >= 0.75 * kuerzer:
        return 0.7
    if len(begriff) >= 5 and len(wort) >= 5:
        if difflib.SequenceMatcher(None, begriff, wort).ratio() >= 0.84:
            return 0.6
    return 0.0


def bewerten(begriffe: list[str], sache: dict) -> float:
    """Gewichtet: Name zählt am meisten, dann Suchwörter, dann der Rest."""
    if not begriffe:
        return 0.0
    try:
        such = json.loads(sache.get("woerter") or "[]")
    except ValueError:
        such = []
    felder = [
        (3.0, woerter_von(sache.get("name", ""))),
        (2.0, [w for s in such for w in woerter_von(s)]),
        (1.0, woerter_von(sache.get("bereich", "")) + woerter_von(sache.get("beschreibung", ""))
         + woerter_von(sache.get("notiz", "") or "")),
    ]
    summe, getroffen = 0.0, 0
    for b in begriffe:
        bester = 0.0
        for gewicht, ws in felder:
            for w in ws:
                p = passt(b, w)
                if p:
                    bester = max(bester, gewicht * p)
        if bester:
            getroffen += 1
            summe += bester
    if not getroffen:
        return 0.0
    # Wer ALLE Begriffe trifft, steht vor dem, der nur einen trifft.
    if getroffen == len(begriffe):
        summe += 2.0 * len(begriffe)
    return round(summe, 3)


def suchen(anfrage: str, sachen: list[dict], grenze: int = 30) -> list[tuple[float, dict]]:
    begriffe = suchbegriffe(anfrage)
    treffer = [(bewerten(begriffe, s), s) for s in sachen]
    treffer = [t for t in treffer if t[0] > 0]
    treffer.sort(key=lambda t: (-t[0], t[1].get("name", "")))
    return treffer[:grenze]
