"""Eine Sache aus einem Shop-Link erfassen (Daniel, 01.10.2026: „Kann die App auch einen
Amazon-Link entgegennehmen und daraus die Infos extrahieren?").

Weg: Die Amazon-App teilt einen Text wie „Wago 221-413 … https://amzn.eu/d/abc". Der Server
löst den Kurzlink auf, liest von der Produktseite Titel, Merkmale und das große Bild und
gibt das an die Bilderkennung -- dieselbe, die sonst Fotos ansieht.

Am 01.10.2026 geprüft: amazon.de liefert die Seite mit einem gewöhnlichen
Browser-Kennzeichen aus (kein Captcha). Das kann sich ändern; dann bleibt der Titel aus dem
geteilten Text, und das reicht für einen brauchbaren Vorschlag.

Andere Shops gehen über die üblichen og:-Angaben (og:title, og:image), die fast jede
Produktseite trägt.
"""
from __future__ import annotations
import html, re
from dataclasses import dataclass, field
import httpx

KENNUNG = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")
URL_MUSTER = re.compile(r"https?://[^\s<>\"']+")


@dataclass
class Produkt:
    url: str = ""
    titel: str = ""
    merkmale: list[str] = field(default_factory=list)
    bild_url: str = ""
    seite_gelesen: bool = False


def link_aus_text(text: str) -> tuple[str | None, str]:
    """Trennt den Link vom übrigen Text. Der Rest ist oft schon der Produkttitel."""
    m = URL_MUSTER.search(text or "")
    if not m:
        return None, (text or "").strip()
    url = m.group(0).rstrip(").,;")
    rest = (text[:m.start()] + " " + text[m.end():]).strip()
    # Füllsätze der Shop-Apps abschneiden („Schau dir das an:", „Check this out")
    rest = re.sub(r"^(schau dir (das|dies) an|sieh dir (das|dies) an|check (this|it) out)\s*[:!-]?\s*", "",
                  rest, flags=re.I).strip(" \n-–:")
    return url, rest


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def auswerten(seite: str, url: str = "") -> Produkt:
    p = Produkt(url=url)
    m = re.search(r'<span[^>]*id="productTitle"[^>]*>(.*?)</span>', seite, re.S)
    if m:
        p.titel = _text(m.group(1))
    if not p.titel:
        m = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]*)"', seite)
        if m:
            p.titel = _text(m.group(1))
    if not p.titel:
        m = re.search(r"<title[^>]*>(.*?)</title>", seite, re.S)
        if m:
            p.titel = re.sub(r"\s*[:|–-]\s*Amazon\.[a-z.]+.*$", "", _text(m.group(1)))
    m = re.search(r'id="feature-bullets".*?</ul>', seite, re.S)
    if m:
        p.merkmale = [t for t in (_text(x) for x in re.findall(
            r'<span class="a-list-item">(.*?)</span>', m.group(0), re.S)) if t][:6]
    for muster in (r'data-old-hires="(https://[^"]+)"', r'"hiRes":"(https://[^"]+)"',
                   r'<meta[^>]+property="og:image"[^>]+content="(https://[^"]+)"'):
        m = re.search(muster, seite)
        if m:
            p.bild_url = html.unescape(m.group(1))
            break
    p.seite_gelesen = bool(p.titel)
    return p


MAX_SEITE, MAX_BILD = 5 * 1024 * 1024, 15 * 1024 * 1024


def _nur_internet(anfrage: httpx.Request) -> None:
    """Der Server ruft nur Adressen im Internet ab -- vorher holte er jede Adresse, die man ihm als Shop-Link
    schickte, auch Geräte im eigenen Heimnetz (Durchsicht 03.10.2026). Gilt auch für jede Weiterleitung."""
    import ipaddress, socket
    host = anfrage.url.host
    try:
        adressen = {i[4][0] for i in socket.getaddrinfo(host, anfrage.url.port or 443)}
    except OSError:
        raise httpx.ConnectError(f"{host} nicht auflösbar")
    for a in adressen:
        ip = ipaddress.ip_address(a.split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise httpx.ConnectError(f"{host} liegt nicht im Internet")


def _lesen(c: httpx.Client, url: str, grenze: int) -> tuple[int, dict, bytes, str]:
    """Abruf mit Größengrenze -- eine riesige Antwort landete vorher ganz im Speicher."""
    with c.stream("GET", url) as r:
        daten = bytearray()
        for stueck in r.iter_bytes():
            daten += stueck
            if len(daten) > grenze:
                raise httpx.ReadError("Antwort zu groß")
        return r.status_code, dict(r.headers), bytes(daten), str(r.url)


def holen(text: str, client: httpx.Client | None = None) -> tuple[Produkt, bytes | None]:
    """Liest den Link. Gibt das Produkt und, wenn es eins gibt, die Bilddaten zurück.
    Wirft nie wegen der Seite -- dann bleibt eben der geteilte Text."""
    url, rest = link_aus_text(text)
    p = Produkt(url=url or "", titel=rest[:200])
    if not url:
        return p, None
    eigener = client is None
    c = client or httpx.Client(timeout=15, follow_redirects=True, event_hooks={"request": [_nur_internet]},
                               headers={"User-Agent": KENNUNG, "Accept-Language": "de-DE,de;q=0.9"})
    bild = None
    try:
        code, _, roh, endadresse = _lesen(c, url, MAX_SEITE)
        text_seite = roh.decode("utf-8", errors="replace")
        if code == 200 and "captcha" not in text_seite[:20000].lower():
            gelesen = auswerten(text_seite, endadresse)
            if gelesen.titel:
                gelesen.url = _kurz(endadresse)
                p = gelesen
        if p.bild_url:
            code, kopf, roh, _ = _lesen(c, p.bild_url, MAX_BILD)
            kopf = {k.lower(): v for k, v in kopf.items()}
            if code == 200 and kopf.get("content-type", "").startswith("image/"):
                bild = roh
    except httpx.HTTPError:
        pass
    finally:
        if eigener:
            c.close()
    return p, bild


def _kurz(url: str) -> str:
    """Amazon-Adressen auf /dp/<ASIN> kürzen -- der Rest ist Nachverfolgung."""
    m = re.search(r"https://(www\.)?amazon\.([a-z.]+)/.*?(?:dp|gp/product)/([A-Z0-9]{10})", url)
    if m:
        return f"https://www.amazon.{m.group(2)}/dp/{m.group(3)}"
    return url.split("?")[0]


def als_text(p: Produkt) -> str:
    teile = [f"Produktseite: {p.titel}"]
    if p.merkmale:
        teile.append("Merkmale:\n" + "\n".join("- " + m[:300] for m in p.merkmale))
    return "\n".join(teile)
