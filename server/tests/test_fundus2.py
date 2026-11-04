"""Fundus 2 (03.11.2026): Arten zählen, Aufräumliste, Zusammenlegen, neue Einteilung, Steckbriefe, Bauen, automatisch
aufbereiten. Die KI ist überall ersetzt (frage_json bzw. MockTransport) -- kein Test fragt Claude oder OpenAI echt."""
import json, os
import httpx
import pytest
from conftest import jpeg


def kiste(client, **k):
    return client.post("/api/kisten", json={"laenge": 350, "breite": 250, "hoehe": 140, **k}).json()


def sache(client, code, name, **k):
    r = client.post("/api/sachen", json={"kiste": code, "name": name, **k})
    assert r.status_code == 200, r.text
    return r.json()


def foto(client, farbe=(200, 120, 40)):
    return client.post("/api/fotos", files={"datei": ("a.jpg", jpeg(farbe), "image/jpeg")}).json()["id"]


# ---------------------------------------------------------------- Steckbrief und Arten zählen

def test_steckbrief_wird_gespeichert_und_geglaettet(client):
    k = kiste(client)
    s = sache(client, k["code"], "FS1000A Funkmodul", merkmale=[{"name": "frequenz", "wert": " 433  MHz "},
                                                               {"name": "Frequenz", "wert": "doppelt"}, {"name": "", "wert": "x"}])
    assert s["merkmale"] == [{"name": "Frequenz", "wert": "433 MHz"}]
    s2 = client.patch(f"/api/sachen/{s['id']}", json={"merkmale": {"Typ": "FS1000A"}}).json()
    assert s2["merkmale"] == [{"name": "Typ", "wert": "FS1000A"}]
    assert sache(client, k["code"], "ohne")["merkmale"] == []


def test_arten_zaehlen_ohne_aufgebrauchtes(client):
    k = kiste(client)
    sache(client, k["code"], "A", bereich="Sensoren", menge="2")
    sache(client, k["code"], "B", bereich="Sensoren")
    sache(client, k["code"], "C", bereich="Werkzeug")
    sache(client, k["code"], "D", bereich="Werkzeug", menge="0 Stück")
    assert client.get("/api/uebersicht").json()["bereiche_anzahl"] == [{"name": "Sensoren", "anzahl": 2},
                                                                       {"name": "Werkzeug", "anzahl": 1}]


# ---------------------------------------------------------------- Aufräumliste

def test_vielleicht_doppelt_nach_befund(client):
    k6, k4 = kiste(client), kiste(client)
    a = sache(client, k6["code"], "FS1000A 433 MHz Funkmodul", menge="3")
    b = sache(client, k6["code"], "FS1000A Funkmodul Sender und Empfänger", menge="ca. 3")
    c = sache(client, k6["code"], "FS1000A Funk-Sender-Modul", menge="1")
    sache(client, k4["code"], "ESP32 Dev Kit WiFi Mikrocontroller Modul")          # Familie, nicht Typ
    sache(client, k6["code"], "ESP32-Audio-Kit V2.2")
    sache(client, k6["code"], "WS2812b RGB-LED mit 4 Pins")                        # gleicher Typ, andere Kiste,
    sache(client, k4["code"], "WS2812B-64 RGB-LED Matrix")                         # ganz anderer Name
    d1 = sache(client, k4["code"], "Lötzinn 0,5 mm")
    d2 = sache(client, k6["code"], "Lötzinn, 0,5 mm")                              # gleicher Name, andere Kiste
    sache(client, k6["code"], "FS1000A leer", menge="0")                           # aufgebraucht zählt nicht
    d = client.get("/api/aufraeumen").json()
    gruppen = {g["schluessel"]: sorted(s["id"] for s in g["sachen"]) for g in d["doppelt"]}
    assert gruppen == {"typ:FS1000A": sorted([a["id"], b["id"], c["id"]]), "name:loetzinn 0 5 mm": sorted([d1["id"], d2["id"]])}
    # „Sind verschieden" blendet aus …
    assert client.post("/api/doppelt/verschieden", json={"schluessel": "typ:FS1000A",
                                                         "ids": [a["id"], b["id"], c["id"]]}).status_code == 200
    assert "typ:FS1000A" not in {g["schluessel"] for g in client.get("/api/aufraeumen").json()["doppelt"]}
    # … bis ein weiterer dazukommt
    sache(client, k6["code"], "FS1000A Sender")
    assert "typ:FS1000A" in {g["schluessel"] for g in client.get("/api/aufraeumen").json()["doppelt"]}


def test_aufraeumen_orte_raus_leer_arten(client):
    k1 = kiste(client, raum="Werkstatt", stapel="A", art="IKEA SAMLA 11 l")
    k2 = kiste(client, raum="Keller")                         # ohne Stapel, ohne Regal, ohne Art
    s = sache(client, k1["code"], "Lötkolben")
    client.patch(f"/api/sachen/{s['id']}", json={"raus": True})
    sache(client, k2["code"], "Sekundenkleber", menge="0")
    for i in range(20):
        sache(client, k2["code"], f"Teil {i}", bereich="Elektro")
    d = client.get("/api/aufraeumen").json()
    assert [k["code"] for k in d["ohne_platz"]] == [k2["code"]] and d["ohne_platz"][0]["anzahl"] == 20
    assert d["ohne_art"] == [k2["code"]]
    assert [x["name"] for x in d["raus"]] == ["Lötkolben"] and [x["name"] for x in d["leer"]] == ["Sekundenkleber"]
    assert d["arten"]["groesster"] == "Elektro" and d["arten"]["lohnt"] is True
    assert d["ohne_merkmale"] == 22


# ---------------------------------------------------------------- Zusammenlegen

def test_menge_zusammen():
    import app
    assert app.menge_zusammen(["3", "ca. 3", "1 Stück"]) == "7 Stück"
    assert app.menge_zusammen(["Rolle", "2 Rollen"]) == "3 Rolle"
    assert app.menge_zusammen(["", ""]) == "2"


def test_zusammenlegen(client):
    k = kiste(client)
    f1, f2, f3 = foto(client), foto(client, (10, 200, 10)), foto(client, (10, 10, 200))
    a = sache(client, k["code"], "FS1000A 433 MHz Funkmodul", menge="3", fotos=[f1], woerter=["funk"],
              merkmale=[{"name": "Frequenz", "wert": "433 MHz"}])
    b = sache(client, k["code"], "FS1000A Funkmodul", menge="ca. 3", fotos=[f2, f3], woerter=["sender"], notiz="eins kaputt",
              merkmale=[{"name": "Typ", "wert": "FS1000A"}])
    p = client.post("/api/projekte", json={"name": "Funkklingel"}).json()
    client.post(f"/api/projekte/{p['id']}/teile", json={"sache_id": b["id"]})
    r = client.post("/api/sachen/zusammenlegen", json={"ziel": a["id"], "quellen": [b["id"]]})
    assert r.status_code == 200, r.text
    z = r.json()
    assert z["menge"] == "6" and z["fotos"] == [f1, f2, f3] and z["woerter"] == ["funk", "sender"]
    assert z["notiz"] == "eins kaputt" and {m["name"] for m in z["merkmale"]} == {"Frequenz", "Typ"}
    assert client.get(f"/api/sachen/{b['id']}").status_code == 404
    assert client.get(f"/api/projekte/{p['id']}").json()["teile"][0]["sache_id"] == a["id"]
    assert client.post("/api/sachen/zusammenlegen", json={"ziel": a["id"], "quellen": [a["id"]]}).status_code == 422


# ---------------------------------------------------------------- neue Einteilung

def test_arten_vorschlag_und_uebernehmen(client, monkeypatch):
    import erkennung, db
    k = kiste(client)
    ids = [sache(client, k["code"], n, bereich="Elektro")["id"] for n in ("ESP32 Dev Kit", "DS18B20 Sensor", "Abisolierzange")]

    def fake(system, text, schema, name, **kw):
        assert name == "arten" and "ESP32 Dev Kit" in text
        return {"arten": [{"name": "mikrocontroller", "ids": [ids[0], 999]}, {"name": "Sensoren", "ids": [ids[1], ids[0]]}]}, \
               {"modell": "claude-sonnet", "tokens_ein": 10, "tokens_aus": 5, "versuche": []}
    monkeypatch.setattr(erkennung, "frage_json", fake)
    v = client.post("/api/arten/vorschlag").json()
    # unbekannte und doppelte ids fallen raus, Vergessenes behält seinen Bereich
    assert [(a["name"], a["ids"]) for a in v["arten"]] == [("Mikrocontroller", [ids[0]]), ("Sensoren", [ids[1]]),
                                                          ("Elektro", [ids[2]])]
    r = client.post("/api/arten/uebernehmen", json={"arten": [{"name": a["name"], "ids": a["ids"]} for a in v["arten"]]}).json()
    assert r["geaendert"] == 2 and os.path.exists(os.path.join(db.DATEN, "sicherungen", r["sicherung"]))
    assert client.get(f"/api/sachen/{ids[1]}").json()["bereich"] == "Sensoren"
    assert client.get("/api/nutzung").json()["je_modell"][0]["modell"] == "claude-sonnet"


# ---------------------------------------------------------------- Steckbriefe nachtragen

def test_merkmale_nachtragen(client, monkeypatch):
    import erkennung, app
    k = kiste(client)
    a = sache(client, k["code"], "FS1000A Funkmodul", beschreibung="433 MHz")
    b = sache(client, k["code"], "Schraubendreher")
    gefragt = []

    def fake(system, text, schema, name, **kw):
        gefragt.append(text)
        return {"sachen": [{"id": a["id"], "merkmale": [{"name": "Frequenz", "wert": "433 MHz"}]}]}, \
               {"modell": "claude-sonnet", "tokens_ein": 1, "tokens_aus": 1, "versuche": []}
    monkeypatch.setattr(erkennung, "frage_json", fake)
    assert client.get("/api/merkmale/stand").json()["offen"] == 2
    app._merkmale_stand.update(laeuft=True)
    app.merkmale_nachtragen()            # direkt, nicht im Faden
    assert client.get(f"/api/sachen/{a['id']}").json()["merkmale"] == [{"name": "Frequenz", "wert": "433 MHz"}]
    assert client.get(f"/api/sachen/{b['id']}").json()["merkmale"] == []      # gefragt, nichts Sicheres
    st = client.get("/api/merkmale/stand").json()
    assert st["offen"] == 0 and st["fertig"] == 2 and not st["laeuft"] and len(gefragt) == 1


# ---------------------------------------------------------------- Bauen

def test_bauen_und_merken(client, monkeypatch):
    import erkennung
    k = kiste(client, raum="Keller")
    esp = sache(client, k["code"], "ESP32 Dev Kit", menge="4")
    sensor = sache(client, k["code"], "Kapazitiver Bodenfeuchtesensor v1.2", menge="4")
    leer = sache(client, k["code"], "Akku 18650", menge="0")

    def fake(system, text, schema, name, **kw):
        assert name == "bauen" and "Bodenfeuchte-Melder" in text and "Akku 18650" not in text
        return {"titel": "Bodenfeuchte-Melder", "hinweis": "Gehäuse fehlt.",
                "teile": [{"id": esp["id"], "menge": "1", "wofuer": "Steuerung"}, {"id": sensor["id"], "menge": "1", "wofuer": "Messen"},
                          {"id": 9999, "menge": "1", "wofuer": "gibt es nicht"}, {"id": esp["id"], "menge": "2", "wofuer": "doppelt"}],
                "fehlt": [{"name": "18650-Akku", "menge": "1", "wofuer": "Strom"}, {"name": " ", "menge": "", "wofuer": ""}]}, \
               {"modell": "claude-sonnet", "tokens_ein": 1, "tokens_aus": 1, "versuche": []}
    monkeypatch.setattr(erkennung, "frage_json", fake)
    assert client.post("/api/bauen", json={"idee": "x"}).status_code == 422
    v = client.post("/api/bauen", json={"idee": "Bodenfeuchte-Melder für die Terrasse"}).json()
    assert [t["sache"]["id"] for t in v["teile"]] == [esp["id"], sensor["id"]] and v["teile"][0]["sache"]["kiste"]["code"] == k["code"]
    assert [f["name"] for f in v["fehlt"]] == ["18650-Akku"] and v["hinweis"] == "Gehäuse fehlt."
    p = client.post("/api/bauen/merken", json={"name": v["titel"], "notiz": v["idee"],
                                               "teile": [{"sache_id": t["sache"]["id"], "menge": t["menge"], "notiz": t["wofuer"]} for t in v["teile"]],
                                               "fehlt": [{"text": f["name"], "menge": f["menge"], "notiz": f["wofuer"]} for f in v["fehlt"]]}).json()
    assert p["name"] == "Bodenfeuchte-Melder" and p["anzahl"] == 3 and p["fehlen"] == 1
    assert [t["name"] for t in p["teile"]] == ["ESP32 Dev Kit", "Kapazitiver Bodenfeuchtesensor v1.2", "18650-Akku"]


# ---------------------------------------------------------------- KI-Frage: Claude zuerst, OpenAI als Rückfall

def test_frage_json_rueckfall(client, monkeypatch):
    import erkennung
    monkeypatch.setenv("CLAUDE_KI_URL", "http://ki.test/api/ki/claude")
    monkeypatch.setenv("CLAUDE_KI_SCHLUESSEL", "s")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("DEMO", raising=False)
    gesehen = []

    def antwort(r: httpx.Request):
        gesehen.append(r.url.host)
        if r.url.host == "ki.test":
            assert "Schema" in json.loads(r.content)["system"] and json.loads(r.content)["bilder"] == []
            return httpx.Response(200, json={"antwort": "kein Objekt"})
        return httpx.Response(200, json={"model": "gpt-test", "choices": [{"message": {"content": '{"x": 1}'}}],
                                         "usage": {"prompt_tokens": 3, "completion_tokens": 1}})
    c = httpx.Client(transport=httpx.MockTransport(antwort))
    a, n = erkennung.frage_json("sys", "text", {"type": "object"}, "probe", client=c)
    assert a == {"x": 1} and n["modell"] == "gpt-test" and gesehen == ["ki.test", "api.openai.com"]
    assert "kein Objekt" in n["rueckfall"]


# ---------------------------------------------------------------- automatisch aufbereiten, ohne Freistellen

def test_aufbereiten_nach_dem_anlegen(client):
    import app, aufbereiten
    if not aufbereiten.ISNET.vorhanden():
        pytest.skip("ohne Freistell-Modell (nur im Abbild)")
    k = kiste(client)
    f = foto(client)
    s = sache(client, k["code"], "Probe", fotos=[f], aufbereiten=True)
    app._aufbereit_schlange.join()
    s = client.get(f"/api/sachen/{s['id']}").json()
    assert len(s["fotos"]) == 2 and s["fotos"][1] == f and s["aufbereitet"] == {s["fotos"][0]: f}
    z = app.V.execute("SELECT einstellungen FROM aufbereitung WHERE original=?", (f,)).fetchone()
    assert json.loads(z["einstellungen"])["freistellen"] is False
    # ohne den Wunsch bleibt das Foto, wie es ist
    s2 = sache(client, k["code"], "Probe 2", fotos=[foto(client)])
    app._aufbereit_schlange.join()
    assert len(client.get(f"/api/sachen/{s2['id']}").json()["fotos"]) == 1


def test_gleiche_auch_ueber_den_typ(client, monkeypatch):
    """Einräumen: „Liegt schon in K-06" auch, wenn die Erkennung den Namen anders formuliert (Emulator 03.11.)."""
    import erkennung
    k = kiste(client)
    alt = sache(client, k["code"], "RFID-Modul RC522")
    sache(client, k["code"], "ESP32 Dev Kit")
    monkeypatch.setattr(erkennung, "erkennen", lambda p, b, h="", en=False: (
        {"name": "RFID-Modul RC522 (MFRC522, 13,56 MHz)", "beschreibung": "", "bereich": "Elektro", "menge": "1",
         "woerter": [], "sicherheit": "hoch", "merkmale": []}, {"modell": "x", "tokens_ein": 0, "tokens_aus": 0, "versuche": []}))
    r = client.post("/api/erkennen", json={"fotos": [foto(client)]}).json()
    assert [g["id"] for g in r["gleiche"]] == [alt["id"]], [g["name"] for g in r["gleiche"]]


def test_bauen_demo_ohne_ki(client, monkeypatch):
    """Demo-Server (04.11.): Bauen zeigt eine Beispiel-Stückliste aus dem Bestand, ohne eine KI zu fragen."""
    import erkennung
    monkeypatch.setenv("DEMO", "1")
    monkeypatch.setattr(erkennung, "frage_json", lambda *a, **k: (_ for _ in ()).throw(AssertionError("keine KI im Demo")))
    k = kiste(client)
    led = sache(client, k["code"], "LED strip warm white", menge="2")
    sache(client, k["code"], "Screws M3", menge="50")
    v = client.post("/api/bauen", json={"idee": "Shelf lighting with LED"}, headers={"Accept-Language": "en"}).json()
    assert [t["sache"]["id"] for t in v["teile"]] == [led["id"]] and v["titel"].endswith("(demo)") and v["modell"] == "demo"
    assert v["fehlt"] and "Demo server" in v["hinweis"]


# ---------------------------------------------------------------- 04.11.: Wortreihenfolge, unsichtbare Zeichen, Namen prüfen

def test_doppelt_auch_bei_anderer_wortreihenfolge_aber_nicht_bei_anderen_zahlen(client):
    k1, k2 = kiste(client), kiste(client)
    a = sache(client, k1["code"], "Solarkabel 4 mm², 3 m mit MC4-Steckern")
    b = sache(client, k2["code"], "Solarkabel mit MC4-Steckern 4mm² 3m")
    sache(client, k1["code"], "Lenkrolle mit Bremse 50 mm")
    sache(client, k2["code"], "Lenkrolle mit Bremse 75 mm")
    gruppen = [[s["id"] for s in g["sachen"]] for g in client.get("/api/aufraeumen").json()["doppelt"]]
    assert gruppen == [[a["id"], b["id"]]]


def test_weiche_trennstriche_fliegen_raus(client):
    k = kiste(client)
    s = sache(client, k["code"], "Perlator Wasser­spar­strah­ler M18,5", beschreibung="a​b", woerter=["spar­strahl"])
    assert s["name"] == "Perlator Wassersparstrahler M18,5" and s["beschreibung"] == "ab" and s["woerter"] == ["sparstrahl"]


def test_namen_pruefen_vorschlagen_uebernehmen_behalten(client, monkeypatch):
    import app, erkennung
    k = kiste(client)
    f = foto(client)
    falsch = sache(client, k["code"], "SH3X Infrarot Temperatursensor", fotos=[f])
    richtig = sache(client, k["code"], "RFID-Modul RC522")
    noch = sache(client, k["code"], "DollaTek Mikrocontroller Modul 5V Max 25V")
    gesehen = {}

    def fake(system, text, schema, name, **kw):
        assert name == "namen" and "Bisheriger Eintrag" in text
        gesehen[text.split("\n")[1]] = kw.get("bildpfade")
        if "SH3X" in text:
            return {"passt": False, "name": "SHT3x Temperatur- und Feuchtesensor", "beschreibung": "I²C-Sensor",
                    "grund": "SHT3x misst Temperatur und Feuchte, nicht Infrarot", "sicherheit": "hoch"}, \
                   {"modell": "claude-sonnet", "tokens_ein": 1, "tokens_aus": 1, "versuche": []}
        if "DollaTek" in text:
            return {"passt": False, "name": "Spannungssensor 0–25 V", "beschreibung": "", "grund": "Spannungsteiler",
                    "sicherheit": "mittel"}, {"modell": "claude-sonnet", "tokens_ein": 1, "tokens_aus": 1, "versuche": []}
        return {"passt": True, "name": "", "beschreibung": "", "grund": "", "sicherheit": "hoch"}, \
               {"modell": "claude-sonnet", "tokens_ein": 1, "tokens_aus": 1, "versuche": []}
    monkeypatch.setattr(erkennung, "frage_json", fake)
    monkeypatch.setattr(erkennung, "eingerichtet", lambda: True)
    app._namen_stand.update(laeuft=True, fertig=0, gesamt=0, fehler="")
    app.namen_pruefen(faeden=1)
    assert len(gesehen["Name: SH3X Infrarot Temperatursensor"]) == 1 and gesehen["Name: RFID-Modul RC522"] == []
    st = client.get("/api/namen/stand").json()
    assert st["ungeprueft"] == 0 and st["geprueft"] == 3 and not st["laeuft"] and st["fertig"] == 3
    auf = client.get("/api/aufraeumen").json()
    assert auf["namen_offen"] == 2 and auf["namen_ungeprueft"] == 0 and auf["namen_laeuft"] is False
    assert [(v["sache"]["id"], v["name"]) for v in st["offen"]] == [
        (falsch["id"], "SHT3x Temperatur- und Feuchtesensor"), (noch["id"], "Spannungssensor 0–25 V")]
    neu = client.post(f"/api/namen/{falsch['id']}/uebernehmen").json()
    assert neu["name"] == "SHT3x Temperatur- und Feuchtesensor" and neu["beschreibung"] == "I²C-Sensor"
    assert client.post(f"/api/namen/{noch['id']}/behalten").json() == {"ok": True}
    assert client.post(f"/api/namen/{richtig['id']}/uebernehmen").status_code == 404
    st = client.get("/api/namen/stand").json()
    assert st["offen"] == [] and st["ungeprueft"] == 0
    # wer den Namen selbst ändert, macht die Sache wieder ungeprüft
    client.patch(f"/api/sachen/{richtig['id']}", json={"name": "RFID-Lesemodul RC522"})
    assert client.get("/api/namen/stand").json()["ungeprueft"] == 1


def test_namen_pruefen_nicht_im_demo(client, monkeypatch):
    monkeypatch.setenv("DEMO", "1")
    assert client.post("/api/namen/pruefen").status_code == 503
