import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from conftest import jpeg


def kiste(c, **k):
    r = c.post("/api/kisten", json=k)
    assert r.status_code == 200, r.text
    return r.json()


def sache(c, kiste_code, name, **k):
    r = c.post("/api/sachen", json={"kiste": kiste_code, "name": name, **k})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- Anmeldung

def test_ohne_token_nichts(client):
    for pfad in ("/api/kisten", "/api/uebersicht", "/api/suche?q=x", "/api/nutzung", "/api/app/version"):
        r = client.get(pfad, headers={"X-Token": ""})
        assert r.status_code == 401, pfad
    assert client.get("/api/gesundheit", headers={"X-Token": ""}).status_code == 200


def test_falsches_passwort_und_bremse(client):
    for _ in range(5):
        assert client.post("/api/anmelden", json={"passwort": "falsch"},
                           headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 401
    # Danach auch das RICHTIGE Passwort nicht mehr -- sonst wäre Raten nur langsamer.
    r = client.post("/api/anmelden", json={"passwort": "geheim123"}, headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 429
    # Ein anderer Absender ist davon nicht betroffen.
    assert client.post("/api/anmelden", json={"passwort": "geheim123"},
                       headers={"X-Forwarded-For": "198.51.100.1"}).status_code == 200


def test_abmelden_macht_token_ungueltig(client):
    assert client.post("/api/abmelden").status_code == 200
    assert client.get("/api/kisten").status_code == 401


def test_gleiches_geraet_ersetzt_anmeldung(client):
    t1 = client.post("/api/anmelden", json={"passwort": "geheim123", "geraet": "S22"}).json()["token"]
    t2 = client.post("/api/anmelden", json={"passwort": "geheim123", "geraet": "S22"}).json()["token"]
    assert client.get("/api/kisten", headers={"X-Token": t1}).status_code == 401
    assert client.get("/api/kisten", headers={"X-Token": t2}).status_code == 200


# ---------------------------------------------------------------- Kisten

def test_kisten_nummern_fortlaufend_und_luecke_fuellen(client):
    assert kiste(client)["code"] == "K-01"
    assert kiste(client)["code"] == "K-02"
    assert kiste(client)["code"] == "K-03"
    assert client.delete("/api/kisten/K-02").status_code == 200
    assert kiste(client)["code"] == "K-02"


def test_masse_eurobox(client):
    k = kiste(client, laenge=600, breite=400, hoehe=220)
    assert (k["laenge"], k["breite"], k["hoehe"]) == (600, 400, 220)
    assert client.post("/api/kisten", json={"hoehe": "hoch"}).status_code == 422
    assert client.post("/api/kisten", json={"hoehe": 5}).status_code == 422
    k = client.patch(f"/api/kisten/{k['code']}", json={"hoehe": 320}).json()
    assert (k["laenge"], k["breite"], k["hoehe"]) == (600, 400, 320)
    assert client.get("/api/kisten").json()["grundflaechen"][0] == {"laenge": 600, "breite": 400}


def test_stapel_neue_kiste_kommt_oben_drauf(client):
    a = kiste(client, stapel="a")
    b = kiste(client, stapel="A")
    assert b["stapel"] == "A" and b["position"] == 1 and b["darueber"] == 0
    assert client.get(f"/api/kisten/{a['code']}").json()["position"] == 2
    assert client.get(f"/api/kisten/{a['code']}").json()["darueber"] == 1


def test_stapel_umsetzen_nummeriert_beide_stapel_neu(client):
    k1, k2, k3 = (kiste(client, stapel="A", position=99) for _ in range(3))  # immer unten anhängen
    pos = {k["code"]: k["position"] for k in client.get("/api/kisten").json()["kisten"]}
    assert pos == {"K-01": 1, "K-02": 2, "K-03": 3}
    kiste(client, stapel="B")
    # K-01 von A oben nach B an Platz 2
    r = client.patch("/api/kisten/K-01", json={"stapel": "B", "position": 2}).json()
    assert r["stapel"] == "B" and r["position"] == 2 and r["darueber"] == 1
    alle = {k["code"]: (k["stapel"], k["position"]) for k in client.get("/api/kisten").json()["kisten"]}
    assert alle == {"K-02": ("A", 1), "K-03": ("A", 2), "K-04": ("B", 1), "K-01": ("B", 2)}


def test_volle_kiste_laesst_sich_nicht_loeschen(client):
    k = kiste(client)
    sache(client, k["code"], "Kabelbinder")
    r = client.delete(f"/api/kisten/{k['code']}")
    assert r.status_code == 409 and "1 Sachen" in r.json()["detail"]


def test_unbekannte_kiste(client):
    assert client.get("/api/kisten/K-99").status_code == 404
    assert client.post("/api/sachen", json={"kiste": "K-99", "name": "x"}).status_code == 404


# ---------------------------------------------------------------- Etikett

def test_etikett_png_und_qr_lesbar(client):
    k = kiste(client)
    r = client.get(f"/api/kisten/{k['code']}/etikett.png")
    assert r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n"
    from PIL import Image
    import io
    b = Image.open(io.BytesIO(r.content))
    # 40 x 30 mm bei 300 dpi
    assert b.size == (472, 354)
    breit = client.get(f"/api/kisten/{k['code']}/etikett.png?breite=62&hoehe=29")
    assert Image.open(io.BytesIO(breit.content)).size == (732, 343)
    assert client.get(f"/api/kisten/{k['code']}/etikett.png?breite=5").status_code == 422


def test_etiketten_pdf_eine_seite_je_kiste(client):
    kiste(client); kiste(client); kiste(client)
    r = client.get("/api/etiketten.pdf?codes=K-01,K-03")
    assert r.status_code == 200 and r.content[:4] == b"%PDF"
    assert r.content.count(b"/Type /Page") - r.content.count(b"/Type /Pages") == 2
    assert client.get("/api/etiketten.pdf?codes=K-09").status_code == 404


def test_qr_inhalt_und_rueckweg():
    import etikett
    assert etikett.code_aus_qr(etikett.qr_inhalt("K-07")) == "K-07"
    assert etikett.qr_inhalt("K-07") == "FUNDUS:K-07"
    assert etikett.code_aus_qr("fundus:k-07") == "K-07"
    # alte, schon geklebte Etiketten aus der Zeit als „Werkstatt-Kiste"
    assert etikett.code_aus_qr("WERKSTATT:K-07") == "K-07"
    assert etikett.code_aus_qr("https://dhl.de/123") is None


# ---------------------------------------------------------------- Fotos

def test_foto_hochladen_klein_und_gross(client):
    r = client.post("/api/fotos", files={"datei": ("a.jpg", jpeg(groesse=(4000, 3000)), "image/jpeg")})
    fid = r.json()["id"]
    from PIL import Image
    import io
    klein = Image.open(io.BytesIO(client.get(f"/api/fotos/{fid}").content))
    gross = Image.open(io.BytesIO(client.get(f"/api/fotos/{fid}?gross=1").content))
    assert max(klein.size) == 400 and max(gross.size) == 2000


def test_foto_drehen_bleibt_gespeichert(client):
    from PIL import Image
    import io
    fid = client.post("/api/fotos", files={"datei": ("a.jpg", jpeg(groesse=(800, 400)), "image/jpeg")}).json()["id"]
    r = client.post(f"/api/fotos/{fid}/drehen", json={"grad": 90})
    assert r.status_code == 200 and r.json()["fassung"]
    gross = Image.open(io.BytesIO(client.get(f"/api/fotos/{fid}?gross=1").content))
    klein = Image.open(io.BytesIO(client.get(f"/api/fotos/{fid}").content))
    assert gross.size == (400, 800) and klein.size[0] < klein.size[1]
    assert client.post(f"/api/fotos/{fid}/drehen", json={"grad": 45}).status_code == 422
    assert client.post("/api/fotos/" + "0" * 32 + "/drehen", json={"grad": 90}).status_code == 404


def test_kein_bild_und_fremde_pfade(client):
    assert client.post("/api/fotos", files={"datei": ("a.jpg", b"kein bild", "image/jpeg")}).status_code == 422
    assert client.get("/api/fotos/..%2F..%2Fetc%2Fpasswd").status_code == 404
    assert client.get("/api/fotos/" + "0" * 32).status_code == 404


def test_verwaiste_fotos_werden_weggeraeumt(client):
    import app as servermodul, db
    fid = client.post("/api/fotos", files={"datei": ("a.jpg", jpeg(), "image/jpeg")}).json()["id"]
    k = kiste(client)
    behalten = client.post("/api/fotos", files={"datei": ("b.jpg", jpeg(), "image/jpeg")}).json()["id"]
    sache(client, k["code"], "Mit Foto", foto=behalten)
    db.V.execute("UPDATE fotos SET angelegt_am='2020-01-01T00:00:00+00:00'")
    db.V.commit()
    assert servermodul.verwaiste_fotos_wegraeumen() == 1
    assert client.get(f"/api/fotos/{fid}").status_code == 404
    assert client.get(f"/api/fotos/{behalten}").status_code == 200


# ---------------------------------------------------------------- Sachen

def test_sache_anlegen_aendern_raus_zurueck(client):
    k = kiste(client, stapel="B")
    s = sache(client, k["code"], "  Wago 221-413 ", woerter=["Klemme", "klemme", " Hebel "], menge="ca. 25")
    assert s["name"] == "Wago 221-413" and s["woerter"] == ["hebel", "klemme"]
    assert s["kiste"]["code"] == k["code"] and s["raus_seit"] is None
    r = client.patch(f"/api/sachen/{s['id']}", json={"raus": True}).json()
    seit = r["raus_seit"]
    assert seit
    # Ein zweites „raus" überschreibt den Zeitpunkt nicht.
    assert client.patch(f"/api/sachen/{s['id']}", json={"raus": True}).json()["raus_seit"] == seit
    assert [x["id"] for x in client.get("/api/uebersicht").json()["raus"]] == [s["id"]]
    assert client.patch(f"/api/sachen/{s['id']}", json={"raus": False}).json()["raus_seit"] is None


def test_raus_mit_anzahl(client):
    k = kiste(client)
    s = sache(client, k["code"], "Schraubzwingen", menge="4")
    r = client.patch(f"/api/sachen/{s['id']}", json={"raus": True, "anzahl": 2}).json()
    assert r["raus_seit"] and r["raus_anzahl"] == 2
    assert client.patch(f"/api/sachen/{s['id']}", json={"raus": True, "anzahl": 0}).status_code == 422
    r = client.patch(f"/api/sachen/{s['id']}", json={"raus": False}).json()
    assert r["raus_seit"] is None and r["raus_anzahl"] is None
    # ohne Anzahl (Einzelstück) bleibt das Feld leer
    assert client.patch(f"/api/sachen/{s['id']}", json={"raus": True}).json()["raus_anzahl"] is None


def test_kiste_zeigt_zuletzt_hinzugefuegtes_zuerst(client):
    k = kiste(client)
    for n in ("Zange", "Akkuschrauber", "Maßband"):
        sache(client, k["code"], n)
    assert [s["name"] for s in client.get(f"/api/kisten/{k['code']}").json()["sachen"]] == ["Maßband", "Akkuschrauber", "Zange"]


def test_bereich_eine_schreibweise(client):
    k = kiste(client)
    sache(client, k["code"], "A", bereich="Elektro")
    assert sache(client, k["code"], "B", bereich="elektro ")["bereich"] == "Elektro"
    assert sache(client, k["code"], "C", bereich="ELEKTRO")["bereich"] == "Elektro"
    assert sache(client, k["code"], "D", bereich="werkzeug")["bereich"] == "Werkzeug"
    assert client.get("/api/bereiche").json()["bereiche"] == ["Elektro", "Werkzeug"]


def test_sache_umraeumen(client):
    a, b = kiste(client), kiste(client)
    s = sache(client, a["code"], "Lötzinn")
    assert client.patch(f"/api/sachen/{s['id']}", json={"kiste": b["code"]}).json()["kiste"]["code"] == b["code"]
    assert client.get(f"/api/kisten/{a['code']}").json()["anzahl"] == 0


def test_sache_ohne_name(client):
    k = kiste(client)
    assert client.post("/api/sachen", json={"kiste": k["code"], "name": "  "}).status_code == 422
    s = sache(client, k["code"], "x")
    assert client.patch(f"/api/sachen/{s['id']}", json={"name": ""}).status_code == 422


def test_sache_loeschen(client):
    k = kiste(client)
    s = sache(client, k["code"], "Weg damit")
    assert client.delete(f"/api/sachen/{s['id']}").json()["name"] == "Weg damit"
    assert client.get(f"/api/sachen/{s['id']}").status_code == 404


def test_mehrere_fotos_und_titelbild(client):
    k = kiste(client)
    a, b, c = (client.post("/api/fotos", files={"datei": (f"{i}.jpg", jpeg(), "image/jpeg")}).json()["id"] for i in range(3))
    s = sache(client, k["code"], "Lötstation", fotos=[a, b])
    assert s["fotos"] == [a, b] and s["foto"] == a
    # ein drittes dazu, dann b zum Titelbild
    s = client.patch(f"/api/sachen/{s['id']}", json={"fotos": [a, b, c]}).json()
    assert s["fotos"] == [a, b, c]
    s = client.patch(f"/api/sachen/{s['id']}", json={"fotos": [b, a, c]}).json()
    assert s["foto"] == b and s["fotos"] == [b, a, c]
    # Kurzform „foto" stellt das Bild nach vorn und behält die anderen
    s = client.patch(f"/api/sachen/{s['id']}", json={"foto": c}).json()
    assert s["fotos"] == [c, b, a]
    # eins herausnehmen: es wird verwaist (und später weggeräumt)
    s = client.patch(f"/api/sachen/{s['id']}", json={"fotos": [c, b]}).json()
    import db
    assert db.V.execute("SELECT sache_id FROM fotos WHERE id=?", (a,)).fetchone()["sache_id"] is None
    assert client.patch(f"/api/sachen/{s['id']}", json={"fotos": ["f" * 32]}).status_code == 422
    assert client.patch(f"/api/sachen/{s['id']}", json={"fotos": "x"}).status_code == 422


def test_alte_sache_mit_einem_foto_zeigt_liste(client):
    k = kiste(client)
    fid = client.post("/api/fotos", files={"datei": ("a.jpg", jpeg(), "image/jpeg")}).json()["id"]
    s = sache(client, k["code"], "Alt", foto=fid)
    assert s["fotos"] == [fid]


def test_foto_muss_existieren(client):
    k = kiste(client)
    r = client.post("/api/sachen", json={"kiste": k["code"], "name": "x", "foto": "a" * 32})
    assert r.status_code == 422


# ---------------------------------------------------------------- Suche

def test_suche_mit_kiste_und_stapelhoehe(client):
    oben = kiste(client, stapel="C")
    unten = kiste(client, stapel="C", position=2)
    sache(client, unten["code"], "Düsenaufsatz Heißklebepistole", woerter=["kleben", "heißkleber"])
    sache(client, unten["code"], "Heißklebesticks 11 mm")
    sache(client, oben["code"], "Wago-Klemmen")
    t = client.get("/api/suche", params={"q": "heisskleber düse"}).json()["treffer"]
    assert t[0]["name"] == "Düsenaufsatz Heißklebepistole"
    assert t[0]["kiste"]["code"] == unten["code"] and t[0]["kiste"]["darueber"] == 1
    assert "Heißklebesticks 11 mm" in [x["name"] for x in t]
    assert "Wago-Klemmen" not in [x["name"] for x in t]
    assert client.get("/api/suche", params={"q": "   "}).json()["treffer"] == []


def test_katalog_und_notiz(client):
    k = kiste(client, stapel="A")
    sache(client, k["code"], "Zange")
    s = sache(client, k["code"], "Akkuschrauber", notiz="Ladegerät liegt im Regal")
    kat = client.get("/api/sachen").json()["sachen"]
    assert [x["name"] for x in kat] == ["Akkuschrauber", "Zange"] and kat[0]["kiste"]["code"] == k["code"]
    assert kat[0]["notiz"] == "Ladegerät liegt im Regal"
    assert client.patch(f"/api/sachen/{s['id']}", json={"notiz": "Akku defekt"}).json()["notiz"] == "Akku defekt"
    # die Notiz wird mit durchsucht
    assert [x["name"] for x in client.get("/api/suche", params={"q": "defekt"}).json()["treffer"]] == ["Akkuschrauber"]


def test_uebersicht(client):
    k = kiste(client)
    sache(client, k["code"], "Erstes", bereich="Elektro")
    sache(client, k["code"], "Zweites", bereich="Elektro")
    u = client.get("/api/uebersicht").json()
    assert u["anzahl_sachen"] == 2 and u["bereiche"] == ["Elektro"] and u["erkennung"] is False
    assert u["zuletzt"][0]["name"] == "Zweites"
    assert u["kisten"][0]["anzahl"] == 2


# ---------------------------------------------------------------- Erkennung

def test_erkennung_ohne_schluessel_sagt_es_klar(client):
    fid = client.post("/api/fotos", files={"datei": ("a.jpg", jpeg(), "image/jpeg")}).json()["id"]
    r = client.post("/api/erkennen", json={"fotos": [fid]})
    assert r.status_code == 503 and "von Hand" in r.json()["detail"]
    assert client.get("/api/nutzung").json()["anfragen"] == 1


def test_erkennung_mit_attrappe_und_aehnlichen(client, monkeypatch):
    import erkennung
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    gesehen = {}

    def antwort(request: httpx.Request):
        body = json.loads(request.content)
        gesehen["body"] = body
        gesehen["auth"] = request.headers["authorization"]
        inhalt = {"name": "Wago-Verbindungsklemme 221-413", "beschreibung": "3-Leiter mit Hebel",
                  "bereich": "Elektro", "menge": "ca. 25",
                  "woerter": ["Lüsterklemme", "klemme", " Klemme "], "sicherheit": "hoch"}
        return httpx.Response(200, json={"model": "gpt-test", "choices": [{"message": {"content": json.dumps(inhalt)}}],
                                         "usage": {"prompt_tokens": 900, "completion_tokens": 80}})

    echt = erkennung.erkennen
    monkeypatch.setattr(erkennung, "erkennen",
                        lambda p, b, h="", en=False: echt(p, b, h, client=httpx.Client(transport=httpx.MockTransport(antwort)), en=en))
    k = kiste(client)
    sache(client, k["code"], "Wago-Verbindungsklemme 221-412", bereich="Elektro")
    fid = client.post("/api/fotos", files={"datei": ("a.jpg", jpeg(), "image/jpeg")}).json()["id"]
    r = client.post("/api/erkennen", json={"fotos": [fid]})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["vorschlag"]["woerter"] == ["klemme", "lüsterklemme"]
    assert d["aehnliche"][0]["name"] == "Wago-Verbindungsklemme 221-412"
    assert gesehen["auth"] == "Bearer sk-test"
    # Vorhandene Bereiche gehen in die Anweisung, damit die Einteilung einheitlich bleibt.
    assert "Elektro" in gesehen["body"]["messages"][0]["content"]
    bild = gesehen["body"]["messages"][1]["content"][1]["image_url"]["url"]
    assert bild.startswith("data:image/jpeg;base64,")
    n = client.get("/api/nutzung").json()
    assert n["erfolgreich"] == 1 and n["tokens_ein"] == 900


@pytest.mark.parametrize("status,body,teil", [
    (401, {"error": {"code": "invalid_api_key"}}, "abgelehnt"),
    (429, {"error": {"code": "insufficient_quota"}}, "Guthaben"),
    (429, {"error": {"code": "rate_limit_exceeded"}}, "überlastet"),
    (500, {}, "Fehler (500)"),
])
def test_erkennung_fehler_verstaendlich(client, monkeypatch, status, body, teil):
    import erkennung
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    echt = erkennung.erkennen
    transport = httpx.MockTransport(lambda r: httpx.Response(status, json=body))
    monkeypatch.setattr(erkennung, "erkennen", lambda p, b, h="", en=False: echt(p, b, h, client=httpx.Client(transport=transport), en=en))
    fid = client.post("/api/fotos", files={"datei": ("a.jpg", jpeg(), "image/jpeg")}).json()["id"]
    r = client.post("/api/erkennen", json={"fotos": [fid]})
    assert r.status_code >= 500 and teil in r.json()["detail"]


def test_erkennen_ohne_foto(client):
    assert client.post("/api/erkennen", json={"fotos": []}).status_code == 422
    assert client.post("/api/erkennen", json={"fotos": ["f" * 32]}).status_code == 404


# ---------------------------------------------------------------- Gleichzeitig

def test_viele_gleichzeitige_aufrufe(client):
    """Was der Reihe nach grün ist, ist nicht gleichzeitig grün (MachIchGleich, 13.09.)."""
    k = kiste(client)
    for i in range(10):
        sache(client, k["code"], f"Teil {i}")
    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(lambda _: client.get("/api/uebersicht").status_code, range(40)))
    assert set(codes) == {200}


def test_kistenart(client):
    """Daniel, 02.10.: alle Kisten sind IKEA UPPSNOFSAD -- die Art steht an der Kiste."""
    k = client.post("/api/kisten", json={"laenge": 350, "breite": 250, "hoehe": 140, "art": "IKEA UPPSNOFSAD"}).json()
    assert k["art"] == "IKEA UPPSNOFSAD" and (k["laenge"], k["breite"], k["hoehe"]) == (350, 250, 140)
    assert client.patch(f"/api/kisten/{k['code']}", json={"art": ""}).json()["art"] == ""
    assert client.post("/api/kisten", json={}).json()["art"] == ""


def test_bremse_greift_auch_bei_wechselnder_absenderkopfzeile(client):
    """Durchsicht 03.10.2026: Mit selbst gesetztem X-Forwarded-For gab es vorher je Wert frische Versuche."""
    client.headers.pop("X-Token", None)
    for i in range(20):
        client.post("/api/anmelden", json={"passwort": "falsch"}, headers={"X-Forwarded-For": f"10.0.0.{i}"})
    r = client.post("/api/anmelden", json={"passwort": "geheim123"}, headers={"X-Forwarded-For": "10.0.1.1"})
    assert r.status_code == 429


def test_kueche_liest_ohne_anmeldung_aber_nur_im_haus(client):
    """Küchen-Dashboard (03.10.2026): lesend ohne Token, von außen (Caddy-Stempel) 403."""
    client.post("/api/kisten", json={"stapel": "A"})
    fid = client.post("/api/fotos", files={"datei": ("a.jpg", jpeg(), "image/jpeg")}).json()["id"]
    client.post("/api/sachen", json={"name": "Abisolierzange", "kiste": "K-01", "fotos": [fid],
                                      "woerter": ["abisolieren"]})
    client.headers.pop("X-Token")
    d = client.get("/api/kachel/fundus").json()
    assert d["sachen"][0]["name"] == "Abisolierzange" and d["kisten"][0]["code"] == "K-01"
    assert client.get("/api/kachel/fundus/suche?q=abisolieren").json()["treffer"][0]["name"] == "Abisolierzange"
    assert client.get(f"/api/kachel/fundus/foto/{fid}").status_code == 200
    aussen = {"X-Von-Aussen": "1"}
    assert client.get("/api/kachel/fundus", headers=aussen).status_code == 403
    assert client.get(f"/api/kachel/fundus/foto/{fid}", headers=aussen).status_code == 403
    # Schreiben geht ohne Token weiterhin nicht
    assert client.post("/api/sachen", json={"name": "x", "kiste": "K-01"}).status_code == 401


def test_kueche_entnimmt_ohne_anmeldung_nur_im_haus(client):
    """Entnahme am Küchentablet (03.10.2026): raus mit Anzahl, zurück, verbraucht senkt die Menge."""
    client.post("/api/kisten", json={"stapel": "A"})
    sch = client.post("/api/sachen", json={"name": "Schraube M5", "kiste": "K-01", "menge": "50 Stück"}).json()
    zange = client.post("/api/sachen", json={"name": "Zange", "kiste": "K-01"}).json()
    client.headers.pop("X-Token")
    url = lambda s: f"/api/kachel/fundus/sachen/{s['id']}/entnahme"
    d = client.post(url(sch), json={"aktion": "raus", "anzahl": 4}).json()
    assert d["raus_seit"] and d["raus_anzahl"] == 4
    d = client.post(url(sch), json={"aktion": "zurueck"}).json()
    assert d["raus_seit"] is None and d["raus_anzahl"] is None
    assert client.post(url(sch), json={"aktion": "verbraucht", "anzahl": 10}).json()["menge"] == "40 Stück"
    assert client.post(url(sch), json={"aktion": "verbraucht", "anzahl": 41}).status_code == 422
    # Ein einzelnes Stück ohne Zahl: raus ohne Anzahl, verbrauchen geht nicht
    d = client.post(url(zange), json={"aktion": "raus"}).json()
    assert d["raus_seit"] and d["raus_anzahl"] is None
    assert client.post(url(zange), json={"aktion": "verbraucht"}).status_code == 422
    assert client.post(url(zange), json={"aktion": "loeschen"}).status_code == 422
    assert client.post(url(zange), json={"aktion": "zurueck"}, headers={"X-Von-Aussen": "1"}).status_code == 403


def test_aufgebraucht_zaehlt_nicht_und_kueche_darf_nur_leeres_entfernen(client):
    """Menge 0 (Daniel, 08.10.2026: „das macht kaum Sinn"): leer-Kennzeichen, fehlt in der Kistenzählung,
    steht in der Übersicht als Nachkaufliste; die Küche entfernt ohne Anmeldung NUR Aufgebrauchtes."""
    client.post("/api/kisten", json={"stapel": "A"})
    riegel = client.post("/api/sachen", json={"name": "RAM-Riegel", "kiste": "K-01", "menge": "1"}).json()
    rolle = client.post("/api/sachen", json={"name": "Lötzinn", "kiste": "K-01", "menge": "Rolle"}).json()
    schr = client.post("/api/sachen", json={"name": "Schraube", "kiste": "K-01", "menge": "10 Stück"}).json()
    assert [x["leer"] for x in (riegel, rolle, schr)] == [False, False, False]
    assert client.get("/api/kisten/K-01").json()["anzahl"] == 3
    token = client.headers.pop("X-Token")
    url = lambda s, was: f"/api/kachel/fundus/sachen/{s['id']}/{was}"
    # bis auf null verbrauchen -> leer, aus der Kistenzählung raus, Sache selbst bleibt (Nachkaufliste)
    d = client.post(url(riegel, "entnahme"), json={"aktion": "verbraucht"}).json()
    assert d["menge"] == "0" and d["leer"] is True
    k = client.get("/api/kachel/fundus").json()
    assert k["kisten"][0]["anzahl"] == 2 and {x["name"]: x["leer"] for x in k["sachen"]}["RAM-Riegel"] is True
    # nicht Aufgebrauchtes darf die Küche nicht entfernen; von außen gar nichts
    assert client.post(url(schr, "entfernen")).status_code == 409
    assert client.post(url(rolle, "entfernen")).status_code == 409
    assert client.post(url(riegel, "entfernen"), headers={"X-Von-Aussen": "1"}).status_code == 403
    # mit Anmeldung: Übersicht zählt ohne das Leere und führt es als Nachkaufliste; Kiste zählt 2, listet aber 3
    client.headers["X-Token"] = token
    u = client.get("/api/uebersicht").json()
    assert u["anzahl_sachen"] == 2 and [x["name"] for x in u["leer"]] == ["RAM-Riegel"]
    kiste = client.get("/api/kisten/K-01").json()
    assert kiste["anzahl"] == 2 and len(kiste["sachen"]) == 3
    client.headers.pop("X-Token")
    # „Weg damit"
    assert client.post(url(riegel, "entfernen")).json() == {"ok": True, "name": "RAM-Riegel"}
    assert client.post(url(riegel, "entfernen")).status_code == 404
    assert len(client.get("/api/kachel/fundus").json()["sachen"]) == 2


def test_raeume_und_verschieben(client):
    """Räume (03.10.2026): „A" gibt es je Raum; Verschieben setzt Raum, Stapel und Platz auf einmal."""
    for _ in range(3):
        client.post("/api/kisten", json={"stapel": "A", "position": 9999})          # K-01..K-03 ohne Raum
    k4 = client.post("/api/kisten", json={"raum": "Keller", "stapel": "a"}).json()  # legt den Raum an
    assert (k4["raum"], k4["stapel"], k4["darueber"]) == ("Keller", "A", 0)
    r = client.get("/api/raeume").json()["raeume"]
    assert [(x["name"], x["kisten"]) for x in r] == [("Keller", 1)]
    # K-02 in den Keller, Stapel A, ganz unten: der alte Stapel rückt nach, der neue zählt mit
    k = client.patch("/api/kisten/K-02", json={"raum": "Keller", "stapel": "A", "position": 9999}).json()
    assert (k["raum"], k["position"], k["darueber"]) == ("Keller", 2, 1)
    alle = {x["code"]: x for x in client.get("/api/kisten").json()["kisten"]}
    assert (alle["K-01"]["position"], alle["K-03"]["position"], alle["K-03"]["darueber"]) == (1, 2, 1)
    # Umbenennen zieht die Kisten mit, Löschen nur leer
    rid = r[0]["id"]
    client.patch(f"/api/raeume/{rid}", json={"name": "Keller links"})
    assert client.get("/api/kisten/K-04").json()["raum"] == "Keller links"
    assert client.delete(f"/api/raeume/{rid}").status_code == 409
    assert client.post("/api/raeume", json={"name": "Garage"}).status_code == 200
    assert client.post("/api/raeume", json={"name": "Garage"}).status_code == 409
    # Reihenfolge: Garage nach vorn
    gid = [x for x in client.get("/api/raeume").json()["raeume"] if x["name"] == "Garage"][0]["id"]
    namen = [x["name"] for x in client.patch(f"/api/raeume/{gid}", json={"nr": 1}).json()["raeume"]]
    assert namen == ["Garage", "Keller links"]
    assert client.delete(f"/api/raeume/{gid}").status_code == 200
    # ohne Stapel, aber im Raum
    k = client.patch("/api/kisten/K-01", json={"raum": "Keller links", "stapel": ""}).json()
    assert (k["raum"], k["stapel"]) == ("Keller links", "")


def test_kueche_verschiebt_nur_im_haus_und_nur_in_bekannte_raeume(client):
    for _ in range(2):
        client.post("/api/kisten", json={"stapel": "A", "position": 9999})
    client.post("/api/raeume", json={"name": "Keller"})
    client.headers.pop("X-Token")
    url = "/api/kachel/fundus/kisten/K-02/verschieben"
    k = client.post(url, json={"raum": "Keller", "stapel": "B", "position": 1}).json()
    assert (k["raum"], k["stapel"], k["position"]) == ("Keller", "B", 1)
    assert client.post(url, json={"raum": "Dachboden", "stapel": "A"}).status_code == 422
    assert client.post(url, json={"raum": "", "stapel": "A", "position": 1}, headers={"X-Von-Aussen": "1"}).status_code == 403
    k = client.post(url, json={"raum": "", "stapel": "A", "position": 1}).json()
    assert (k["raum"], k["stapel"], k["position"], k["darueber"]) == ("", "A", 1, 0)


def test_projekt_mit_stueckliste(client):
    """Projekte (03.10.2026): Teile aus dem Bestand mit Ort, freie Teile zum Kaufen, bereitlegen, löschen."""
    client.post("/api/kisten", json={"stapel": "A"})
    s = client.post("/api/sachen", json={"name": "ESP8266 NodeMCU", "kiste": "K-01", "menge": "19"}).json()
    p = client.post("/api/projekte", json={"name": "Wetterstation"}).json()
    assert p["anzahl"] == 0
    p = client.post(f"/api/projekte/{p['id']}/teile", json={"sache_id": s["id"], "menge": "1"}).json()
    p = client.post(f"/api/projekte/{p['id']}/teile", json={"text": "BME280 Sensor", "menge": "1",
                                                          "link": "https://www.amazon.de/dp/B0000"}).json()
    assert [t["name"] for t in p["teile"]] == ["ESP8266 NodeMCU", "BME280 Sensor"]
    assert p["teile"][0]["sache"]["kiste"]["code"] == "K-01" and p["teile"][1]["sache"] is None
    assert (p["anzahl"], p["fehlen"], p["erledigt"]) == (2, 1, 0)
    p = client.patch(f"/api/projektteile/{p['teile'][0]['id']}", json={"erledigt": True}).json()
    assert p["erledigt"] == 1
    assert client.post(f"/api/projekte/{p['id']}/teile", json={}).status_code == 422
    # Sache gelöscht → Teil bleibt mit Namen
    client.delete(f"/api/sachen/{s['id']}")
    p = client.get(f"/api/projekte/{p['id']}").json()
    assert p["teile"][0]["name"] == "ESP8266 NodeMCU" and p["teile"][0]["sache"] is None
    assert client.get("/api/projekte").json()["projekte"][0]["name"] == "Wetterstation"
    assert client.delete(f"/api/projekte/{p['id']}").json()["ok"]
    assert client.get("/api/projekte").json()["projekte"] == []


def test_kiste_voll(client):
    """Daniel, 05.10.2026: man soll markieren können, dass eine Kiste voll ist."""
    k = client.post("/api/kisten", json={"stapel": "A"}).json()
    assert k["voll"] is False
    assert client.patch(f"/api/kisten/{k['code']}", json={"voll": True}).json()["voll"] is True
    assert client.get(f"/api/kisten/{k['code']}").json()["voll"] is True
    assert client.get("/api/kisten").json()["kisten"][0]["voll"] is True
    # Andere Änderungen lassen die Markierung stehen, nur ein ausdrückliches false nimmt sie weg
    assert client.patch(f"/api/kisten/{k['code']}", json={"notiz": "Schrauben"}).json()["voll"] is True
    assert client.patch(f"/api/kisten/{k['code']}", json={"voll": False}).json()["voll"] is False
    client.headers.pop("X-Token")
    assert "voll" in client.get("/api/kachel/fundus").json()["kisten"][0]


def test_regale_mit_faechern_und_stapeln(client):
    """Daniel, 05.10.2026: Regale mit Reihen; je Reihe eigene Plätze nebeneinander und Kisten übereinander."""
    for _ in range(4):
        client.post("/api/kisten", json={"raum": "Werkstatt", "stapel": "A"})
    # 422 bei unsinnigen Reihen
    assert client.post("/api/regale", json={"raum": "Werkstatt", "reihen": []}).status_code == 422
    assert client.post("/api/regale", json={"raum": "Werkstatt", "reihen": [{"breit": 9, "hoch": 1}]}).status_code == 422
    r = client.post("/api/regale", json={"raum": "Werkstatt", "reihen": [{"breit": 2, "hoch": 2}, {"breit": 3, "hoch": 1}]}).json()
    assert r["name"] == "Regal 1" and r["reihen"][0] == {"breit": 2, "hoch": 2} and r["kisten"] == 0
    rid = r["id"]
    # K-01 (oben auf A) ins Regal, Reihe 1 Platz 1: Stapel A rückt nach
    k = client.patch("/api/kisten/K-01", json={"regal": rid, "reihe": 1, "platz": 1}).json()
    assert (k["regal"], k["regal_name"], k["reihe"], k["platz"], k["ebene"], k["stapel"]) == (rid, "Regal 1", 1, 1, 1, "")
    assert [x["position"] for x in client.get("/api/kisten").json()["kisten"] if x["stapel"] == "A"] == [1, 2, 3]
    # K-02 obendrauf, K-03 darunter einschieben -> K-03 unten, K-01 Mitte? Nein: hoch=2, also voll nach zwei
    assert client.patch("/api/kisten/K-02", json={"regal": rid, "reihe": 1, "platz": 1}).json()["ebene"] == 2
    assert client.get("/api/kisten/K-01").json()["darueber"] == 1
    voll = client.patch("/api/kisten/K-03", json={"regal": rid, "reihe": 1, "platz": 1})
    assert voll.status_code == 409 and "kein Platz" in voll.json()["detail"]
    # außerhalb
    assert client.patch("/api/kisten/K-03", json={"regal": rid, "reihe": 2, "platz": 4}).status_code == 422
    assert client.patch("/api/kisten/K-03", json={"regal": rid, "reihe": 3, "platz": 1}).status_code == 422
    # K-01 (unten) raus in Stapel B: K-02 rutscht nach unten
    k = client.patch("/api/kisten/K-01", json={"stapel": "B"}).json()
    assert (k["regal"], k["stapel"], k["raum"]) == (None, "B", "Werkstatt")
    assert client.get("/api/kisten/K-02").json()["ebene"] == 1
    # unten einschieben
    assert client.patch("/api/kisten/K-03", json={"regal": rid, "reihe": 1, "platz": 1, "ebene": 1}).json()["ebene"] == 1
    assert client.get("/api/kisten/K-02").json()["ebene"] == 2
    # Andere Felder ändern lässt die Kiste im Regal
    assert client.patch("/api/kisten/K-02", json={"notiz": "x", "voll": True}).json()["regal"] == rid
    # Reihen verkleinern, wo Kisten stehen -> 409; Löschen mit Kisten -> 409; Raumlöschen mit Regal -> 409
    assert client.patch(f"/api/regale/{rid}", json={"reihen": [{"breit": 2, "hoch": 1}]}).status_code == 409
    assert client.delete(f"/api/regale/{rid}").status_code == 409
    raum = next(x for x in client.get("/api/raeume").json()["raeume"] if x["name"] == "Werkstatt")
    # Umbenennen des Raums zieht das Regal mit
    client.patch(f"/api/raeume/{raum['id']}", json={"name": "Keller"})
    assert client.get("/api/regale").json()["regale"][0]["raum"] == "Keller"
    assert client.get("/api/kisten/K-02").json()["raum"] == "Keller"
    # Kiste löschen (leer) rückt das Fach nach
    client.delete("/api/kisten/K-03")
    assert client.get("/api/kisten/K-02").json()["ebene"] == 1
    # Übersicht und Küchenkachel kennen die Regale
    assert client.get("/api/uebersicht").json()["regale"][0]["kisten"] == 1
    client.headers.pop("X-Token")
    d = client.get("/api/kachel/fundus").json()
    assert d["regale"][0]["name"] == "Regal 1"
    # Küche verschiebt die Regalkiste auf einen Stapel: sie verlässt das Regal
    assert client.post("/api/kachel/fundus/kisten/K-02/verschieben", json={"raum": "Keller", "stapel": "A"}).json()["regal"] is None
