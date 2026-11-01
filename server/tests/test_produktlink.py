"""Der Link-Weg, ohne echtes Amazon: die Seite ist nachgestellt (Aufbau wie amazon.de am 01.10.2026)."""
import io, json
import httpx
from PIL import Image
import produktlink as pl

SEITE = """<html><head><title>Wago 221-413 : Amazon.de: Baumarkt</title>
<meta property="og:image" content="https://m.media-amazon.com/images/I/klein.jpg"></head><body>
<span id="productTitle" class="a-size-large">   Wago 221-413 Verbindungsklemme 3-Leiter, 50 Stück  </span>
<img id="landingImage" data-old-hires="https://m.media-amazon.com/images/I/gross._SL1500_.jpg">
<div id="feature-bullets"><ul>
<li><span class="a-list-item"> EINFACHE HANDHABUNG: 3 Klemmstellen &amp; Prüföffnung </span></li>
<li><span class="a-list-item"> LIEFERUMFANG: 50 x Hebelklemme </span></li>
</ul></div></body></html>"""


def jpeg():
    b = io.BytesIO(); Image.new("RGB", (300, 300), (240, 140, 40)).save(b, "JPEG"); return b.getvalue()


def test_link_aus_geteiltem_text():
    url, rest = pl.link_aus_text("Schau dir das an: Wago 221-413 Klemme https://amzn.eu/d/abc12XY")
    assert url == "https://amzn.eu/d/abc12XY" and rest == "Wago 221-413 Klemme"
    assert pl.link_aus_text("nur text")[0] is None


def test_seite_auswerten():
    p = pl.auswerten(SEITE)
    assert p.titel == "Wago 221-413 Verbindungsklemme 3-Leiter, 50 Stück"
    assert p.bild_url.endswith("gross._SL1500_.jpg")
    assert p.merkmale == ["EINFACHE HANDHABUNG: 3 Klemmstellen & Prüföffnung", "LIEFERUMFANG: 50 x Hebelklemme"]


def test_andere_shops_ueber_og_angaben():
    p = pl.auswerten('<meta property="og:title" content="Bosch Bitsatz 32-tlg"><meta property="og:image" content="https://x/b.jpg">')
    assert p.titel == "Bosch Bitsatz 32-tlg" and p.bild_url == "https://x/b.jpg"


def test_holen_folgt_kurzlink_und_kuerzt_adresse():
    def antwort(r: httpx.Request):
        if r.url.host == "amzn.eu":
            return httpx.Response(301, headers={"Location": "https://www.amazon.de/Wago-221/dp/B00JB3U9CG?ref=abc&th=1"})
        if r.url.path.startswith("/images/"):
            return httpx.Response(200, content=jpeg(), headers={"content-type": "image/jpeg"})
        return httpx.Response(200, text=SEITE)
    c = httpx.Client(transport=httpx.MockTransport(antwort), follow_redirects=True)
    p, bild = pl.holen("Wago https://amzn.eu/d/abc", client=c)
    assert p.url == "https://www.amazon.de/dp/B00JB3U9CG"
    assert p.seite_gelesen and bild[:2] == b"\xff\xd8"


def test_captcha_seite_laesst_den_geteilten_titel_stehen():
    c = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<form action='/errors/validateCaptcha'>")))
    p, bild = pl.holen("Schau dir das an: Abisolierzange Knipex https://amzn.eu/d/x", client=c)
    assert p.titel == "Abisolierzange Knipex" and not p.seite_gelesen and bild is None


def test_netzfehler_bricht_nichts():
    def kaputt(r):
        raise httpx.ConnectError("weg")
    p, bild = pl.holen("Lötzinn https://amzn.eu/d/x", client=httpx.Client(transport=httpx.MockTransport(kaputt)))
    assert p.titel == "Lötzinn" and bild is None


def test_endpunkt_ohne_erkennung(client, monkeypatch):
    p = pl.Produkt(url="https://www.amazon.de/dp/B00JB3U9CG", titel="Wago 221-413 Klemme", seite_gelesen=True)
    monkeypatch.setattr(pl, "holen", lambda text: (p, jpeg()))
    d = client.post("/api/aus-link", json={"text": "https://amzn.eu/d/x"}).json()
    assert d["vorschlag"]["name"] == "Wago 221-413 Klemme" and d["foto"] and "nicht eingerichtet" in d["hinweis"]
    assert client.get(f"/api/fotos/{d['foto']}").status_code == 200
    kiste = client.post("/api/kisten", json={}).json()
    s = client.post("/api/sachen", json={"kiste": kiste["code"], "name": "Wago", "foto": d["foto"], "link": d["link"]}).json()
    assert s["link"] == "https://www.amazon.de/dp/B00JB3U9CG"


def test_endpunkt_mit_erkennung_gibt_produkttext_mit(client, monkeypatch):
    import erkennung
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    p = pl.Produkt(url="u", titel="Wago 221-413", merkmale=["LIEFERUMFANG: 50 x"], seite_gelesen=True)
    monkeypatch.setattr(pl, "holen", lambda text: (p, None))
    gesehen = {}
    def antwort(r):
        gesehen["body"] = json.loads(r.content)
        inhalt = {"name": "Wago-Klemme 221-413", "beschreibung": "3-Leiter", "bereich": "Elektro", "menge": "50",
                  "woerter": ["klemme"], "sicherheit": "hoch"}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(inhalt)}}], "usage": {}})
    echt = erkennung.erkennen
    monkeypatch.setattr(erkennung, "erkennen", lambda pf, b, h="", produkttext="", en=False: echt(
        pf, b, h, client=httpx.Client(transport=httpx.MockTransport(antwort)), produkttext=produkttext, en=en))
    d = client.post("/api/aus-link", json={"text": "x https://amzn.eu/d/x"}).json()
    assert d["vorschlag"]["menge"] == "50" and d["foto"] is None and d["hinweis"] == ""
    frage = gesehen["body"]["messages"][1]["content"][0]["text"]
    assert "LIEFERUMFANG" in frage and len(gesehen["body"]["messages"][1]["content"]) == 1


def test_endpunkt_leer(client, monkeypatch):
    assert client.post("/api/aus-link", json={"text": ""}).status_code == 422
    monkeypatch.setattr(pl, "holen", lambda text: (pl.Produkt(), None))
    assert client.post("/api/aus-link", json={"text": "https://amzn.eu/d/x"}).status_code == 422


def test_gleiche_sache_ueber_link_und_namen(client, monkeypatch):
    """Daniel, 02.10.: genau dieselbe Sache → Auswahlfenster. Gleich heißt: gleiche ASIN oder gleicher Name."""
    kiste = client.post("/api/kisten", json={}).json()
    alt = client.post("/api/sachen", json={"kiste": kiste["code"], "name": "Wago Klemme 221-413", "menge": "20",
                                           "link": "https://www.amazon.de/dp/B00JB3U9CG"}).json()
    client.post("/api/sachen", json={"kiste": kiste["code"], "name": "Wago Klemme 221-412"})
    # gleicher Artikel, anderer Titel, Link mit Nachverfolgung
    p = pl.Produkt(url="https://www.amazon.de/Wago-Klemmen/dp/b00jb3u9cg?ref=x", titel="WAGO 3-fach", seite_gelesen=True)
    monkeypatch.setattr(pl, "holen", lambda text: (p, None))
    d = client.post("/api/aus-link", json={"text": "https://amzn.eu/d/x"}).json()
    assert [s["id"] for s in d["gleiche"]] == [alt["id"]] and d["gleiche"][0]["kiste"]["code"] == kiste["code"]
    # gleicher Name, nur Schreibweise anders, kein Link
    p2 = pl.Produkt(url="", titel="wago klemme 221–413", seite_gelesen=True)
    monkeypatch.setattr(pl, "holen", lambda text: (p2, None))
    d = client.post("/api/aus-link", json={"text": "wago klemme 221–413"}).json()
    assert [s["id"] for s in d["gleiche"]] == [alt["id"]]
    # nur ähnlich → kein Auswahlfenster
    p3 = pl.Produkt(url="https://www.amazon.de/dp/B000000001", titel="Wago Klemme 221-415", seite_gelesen=True)
    monkeypatch.setattr(pl, "holen", lambda text: (p3, None))
    d = client.post("/api/aus-link", json={"text": "https://amzn.eu/d/y"}).json()
    assert d["gleiche"] == []


def test_heimnetz_adressen_werden_nicht_abgerufen():
    """Durchsicht 03.10.2026: Der Shop-Link darf keine Geräte im Heimnetz abfragen."""
    import httpx, pytest
    for adresse in ("http://192.168.1.10/", "http://127.0.0.1:8095/", "http://localhost/"):
        with pytest.raises(httpx.ConnectError):
            pl._nur_internet(httpx.Request("GET", adresse))
