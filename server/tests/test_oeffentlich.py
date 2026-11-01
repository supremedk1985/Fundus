"""Was die öffentliche Fassung braucht (01.11.2026): englische Meldungen, Haus-Teile abschaltbar,
OpenAI-Schlüssel aus der App, Demo-Betrieb."""
import ast, os, re

import httpx
import pytest

from conftest import jpeg

HIER = os.path.dirname(os.path.dirname(__file__))


def _feste_meldungen() -> set[str]:
    """Alle festen Texte in HTTPException(…) und ErkennungsFehler(…) -- auch über mehrere Zeilen verkettete."""
    gefunden = set()
    for name in ("app.py", "erkennung.py"):
        baum = ast.parse(open(os.path.join(HIER, name), encoding="utf-8").read())
        for k in ast.walk(baum):
            if isinstance(k, ast.Call) and getattr(k.func, "id", getattr(k.func, "attr", "")) in ("HTTPException", "ErkennungsFehler"):
                for a in k.args:
                    if isinstance(a, ast.Constant) and isinstance(a.value, str):
                        gefunden.add(a.value)
    return gefunden


def test_alle_meldungen_haben_englisch():
    import texte
    fehlen = [m for m in _feste_meldungen() if texte.uebersetzen(m, True) == m]
    assert not fehlen, fehlen


def test_meldung_folgt_der_sprache_der_app(client):
    assert client.get("/api/kisten/K-99").json()["detail"] == "Kiste K-99 gibt es nicht"
    r = client.get("/api/kisten/K-99", headers={"Accept-Language": "en-US,en;q=0.9"})
    assert r.status_code == 404 and r.json()["detail"] == "Box K-99 does not exist"
    r = client.post("/api/raeume", json={"name": ""}, headers={"Accept-Language": "en"})
    assert r.json()["detail"] == "The room needs a name"


def test_hausfunktionen_abschaltbar(client, monkeypatch):
    import app as servermodul
    assert client.get("/api/kachel/fundus").status_code == 200
    monkeypatch.setattr(servermodul, "HAUS", False)
    assert client.get("/api/kachel/fundus").status_code == 404
    assert client.get("/api/app/version").status_code == 404
    assert client.post("/api/kachel/fundus/sachen/1/entnahme", json={"aktion": "raus"}).status_code == 404
    assert client.get("/api/kisten").status_code == 200


def test_gesundheit_nennt_den_dienst(client):
    d = client.get("/api/gesundheit").json()
    assert d["dienst"] == "fundus" and d["demo"] is False


def test_schluessel_aus_der_app(client, monkeypatch):
    import erkennung
    assert client.get("/api/einstellungen/ki").json()["eingerichtet"] is False
    assert client.put("/api/einstellungen/ki", json={"schluessel": "geheim"}).status_code == 422
    monkeypatch.setattr(erkennung, "schluessel_pruefen", lambda w, client=None: False)
    r = client.put("/api/einstellungen/ki", json={"schluessel": "sk-falsch-aber-lang-genug-123"})
    assert r.status_code == 422 and "abgelehnt" in r.json()["detail"]
    monkeypatch.setattr(erkennung, "schluessel_pruefen", lambda w, client=None: True)
    r = client.put("/api/einstellungen/ki", json={"schluessel": "sk-richtig-und-lang-genug-WXYZ"})
    d = r.json()
    assert r.status_code == 200 and d["eingerichtet"] and d["quelle"] == "app" and d["ende"] == "WXYZ"
    # Der Schlüssel geht nur hinein, nie ganz heraus
    assert "sk-richtig" not in client.get("/api/einstellungen/ki").text
    assert erkennung.schluessel() == "sk-richtig-und-lang-genug-WXYZ"
    # … und wird von der Erkennung benutzt
    gesehen = {}

    def antwort(anfrage: httpx.Request):
        gesehen["auth"] = anfrage.headers["authorization"]
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"name":"x","beschreibung":"",'
                              '"bereich":"","menge":"","woerter":[],"sicherheit":"hoch"}'}}], "usage": {}})
    echt = erkennung.erkennen
    monkeypatch.setattr(erkennung, "erkennen", lambda p, b, h="", en=False: echt(
        p, b, h, client=httpx.Client(transport=httpx.MockTransport(antwort)), en=en))
    fid = client.post("/api/fotos", files={"datei": ("a.jpg", jpeg(), "image/jpeg")}).json()["id"]
    assert client.post("/api/erkennen", json={"fotos": [fid]}).status_code == 200
    assert gesehen["auth"] == "Bearer sk-richtig-und-lang-genug-WXYZ"
    # Modell wählbar, leer = Vorgabe
    assert client.put("/api/einstellungen/ki", json={"modell": "gpt-4.1"}).json()["modell"] == "gpt-4.1"
    assert client.put("/api/einstellungen/ki", json={"modell": "a b"}).status_code == 422
    assert client.put("/api/einstellungen/ki", json={"modell": ""}).json()["modell"] == erkennung.VORGABE_MODELL
    # Löschen
    assert client.put("/api/einstellungen/ki", json={"schluessel": ""}).json()["eingerichtet"] is False


def test_schluessel_der_server_datei_geht_vor(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-aus-der-datei-1234567890")
    d = client.get("/api/einstellungen/ki").json()
    assert d["quelle"] == "datei" and d["ende"] == "7890"
    assert client.put("/api/einstellungen/ki", json={"schluessel": "sk-andere-1234567890abc"}).status_code == 409


def test_englische_erkennung_fragt_englisch(client, monkeypatch):
    import erkennung
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    gesehen = {}

    def antwort(anfrage: httpx.Request):
        import json
        gesehen["body"] = json.loads(anfrage.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"name":"Screw","beschreibung":"",'
                              '"bereich":"Screws","menge":"","woerter":[],"sicherheit":"hoch"}'}}], "usage": {}})
    echt = erkennung.erkennen
    monkeypatch.setattr(erkennung, "erkennen", lambda p, b, h="", en=False: echt(
        p, b, h, client=httpx.Client(transport=httpx.MockTransport(antwort)), en=en))
    fid = client.post("/api/fotos", files={"datei": ("a.jpg", jpeg(), "image/jpeg")}).json()["id"]
    client.post("/api/erkennen", json={"fotos": [fid]}, headers={"Accept-Language": "en-GB"})
    assert "Answer in English" in gesehen["body"]["messages"][0]["content"]


def test_demo_betrieb(client, monkeypatch):
    monkeypatch.setenv("DEMO", "1")
    fid = client.post("/api/fotos", files={"datei": ("a.jpg", jpeg(), "image/jpeg")}).json()["id"]
    d = client.post("/api/erkennen", json={"fotos": [fid]}).json()
    assert "Demo" in d["vorschlag"]["name"]
    d = client.post("/api/erkennen", json={"fotos": [fid]}, headers={"Accept-Language": "en"}).json()
    assert "(demo)" in d["vorschlag"]["name"]
    r = client.put("/api/einstellungen/ki", json={"schluessel": "sk-irgendwas-1234567890"}, headers={"Accept-Language": "en"})
    assert r.status_code == 403 and r.json()["detail"] == "The key cannot be changed in demo mode."
    assert client.get("/api/gesundheit").json()["demo"] is True
