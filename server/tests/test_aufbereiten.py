"""Bilder optimieren: Vorschlag, Übernehmen, Überspringen, Zahlen. Das Modell wird NIE geladen --
ISNET.maske ist durch eine feste Maske ersetzt (ein Rechteck in der Bildmitte)."""
import io
import numpy as np
import pytest
from PIL import Image

import aufbereiten


@pytest.fixture(autouse=True)
def falsches_modell(monkeypatch):
    def maske(rgb):
        h, w = rgb.shape[:2]
        m = np.zeros((h, w), np.uint8)
        m[h // 4: h // 2, w // 3: w // 2] = 255      # Gegenstand oben links der Mitte
        m[5:15, 5:15] = 255                           # Krümel: muss wegfallen
        return m
    monkeypatch.setattr(aufbereiten.ISNET, "maske", maske)
    monkeypatch.setattr(aufbereiten.ISNET, "vorhanden", lambda: True)


def _bild(groesse=(800, 600)):
    a = np.full((groesse[1], groesse[0], 3), (150, 120, 80), np.uint8)
    b = io.BytesIO()
    Image.fromarray(a).save(b, "JPEG")
    return b.getvalue()


def _sache_mit_foto(client, n=1):
    client.post("/api/kisten", json={"stapel": "A"})
    ids = [client.post("/api/fotos", files={"datei": ("a.jpg", _bild(), "image/jpeg")}).json()["id"] for _ in range(n)]
    s = client.post("/api/sachen", json={"name": "Relais", "kiste": "K-01", "fotos": ids}).json()
    return s, ids


def test_vorschlag_schneidet_um_den_gegenstand(client):
    s, (fid,) = _sache_mit_foto(client)
    r = client.post(f"/api/fotos/{fid}/vorschlag", json={})
    assert r.status_code == 200, r.text
    e = r.json()["einstellungen"]
    x, y, seite = e["rahmen"]
    # Gegenstand: x 1/3..1/2, y 1/4..1/2 -> Rahmen liegt drumherum, nicht um den Krümel bei 5..15 px
    assert 0.25 < x < 1 / 3 and 0.0 < y < 0.25 and 0.15 < seite < 0.3
    assert e["schritte"][:2] == ["Zugeschnitten", "Zentriert"] and "Hintergrund weiß" in e["schritte"]
    bild = Image.open(io.BytesIO(client.get(f"/api/vorschlaege/{r.json()['id']}?gross=1").content))
    assert bild.size[0] == bild.size[1] >= aufbereiten.AUSGABE_MIN
    # Freigestellt: die Ecke ist weiß
    assert min(bild.getpixel((3, 3))) > 245


def test_pinsel_weg_nimmt_teil_der_maske(client):
    s, (fid,) = _sache_mit_foto(client)
    ohne = client.post(f"/api/fotos/{fid}/vorschlag", json={}).json()["einstellungen"]["rahmen"]
    # Die untere Hälfte des Gegenstands wegmalen -> die lange Seite (Höhe) schrumpft, der Rahmen auch
    strich = {"art": "weg", "breite": 0.1, "punkte": [[0.3, 0.45], [0.55, 0.45]]}
    mit = client.post(f"/api/fotos/{fid}/vorschlag", json={"pinsel": [strich]}).json()["einstellungen"]["rahmen"]
    assert mit[2] < ohne[2]


def test_eigener_rahmen_und_ohne_freistellen(client):
    s, (fid,) = _sache_mit_foto(client)
    r = client.post(f"/api/fotos/{fid}/vorschlag",
                    json={"rahmen": [0.0, 0.0, 0.5], "freistellen": False, "verbessern": False}).json()
    assert r["einstellungen"]["rahmen"] == [0.0, 0.0, 0.5]
    assert "Hintergrund weiß" not in r["einstellungen"]["schritte"]
    # Ohne Freistellen wird ein Rahmen, der übers Foto ragt, ins Foto gerückt (keine Streifen am Rand)
    r = client.post(f"/api/fotos/{fid}/vorschlag", json={"rahmen": [0.9, -0.2, 0.9], "freistellen": False}).json()
    x, y, seite = r["einstellungen"]["rahmen"]
    assert x >= 0 and y >= 0 and x + seite <= 1 + 1e-9 and y * 600 + seite * 800 <= 600 + 1e-6


def test_uebernehmen_macht_titelbild_und_behaelt_original(client):
    s, (a, b) = _sache_mit_foto(client, 2)
    vid = client.post(f"/api/fotos/{a}/vorschlag", json={}).json()["id"]
    neu = client.post(f"/api/vorschlaege/{vid}/uebernehmen", json={"sache_id": s["id"]}).json()
    assert neu["fotos"][1:] == [a, b] and neu["foto"] == neu["fotos"][0] not in (a, b)
    assert neu["aufbereitet"] == {neu["fotos"][0]: a}
    # Noch einmal aufbereiten (vom ERGEBNIS aus): ersetzt die alte Fassung, keine Verdopplung
    erg = neu["fotos"][0]
    r = client.post(f"/api/fotos/{erg}/vorschlag", json={"wie_zuletzt": True, "kontrast": 20}).json()
    assert r["original"] == a and r["einstellungen"]["kontrast"] == 20
    zwei = client.post(f"/api/vorschlaege/{r['id']}/uebernehmen", json={"sache_id": s["id"]}).json()
    assert len(zwei["fotos"]) == 3 and zwei["fotos"][1:] == [a, b] and erg not in zwei["fotos"]


def test_zahlen_offen_aufbereitet_uebersprungen(client):
    s1, (a,) = _sache_mit_foto(client)
    s2 = client.post("/api/sachen", json={"name": "Waage", "kiste": "K-01",
                                          "fotos": [client.post("/api/fotos", files={"datei": ("b.jpg", _bild(), "image/jpeg")}).json()["id"]]}).json()
    z = client.get("/api/aufbereitung").json()
    assert (z["bilder"], z["offen"], z["aufbereitet"]) == (2, 2, 0)
    vid = client.post(f"/api/fotos/{a}/vorschlag", json={}).json()["id"]
    client.post(f"/api/vorschlaege/{vid}/uebernehmen", json={"sache_id": s1["id"]})
    client.post(f"/api/fotos/{s2['foto']}/ueberspringen")
    z = client.get("/api/aufbereitung").json()
    # Das aufbereitete Bild zählt NICHT als weiteres Bild
    assert (z["bilder"], z["offen"], z["aufbereitet"], z["uebersprungen"]) == (2, 0, 1, 1)
    assert z["liste"] == []
    alle = client.get("/api/aufbereitung?umfang=alle").json()["liste"]
    assert {x["zustand"] for x in alle} == {"aufbereitet", "uebersprungen"}


def test_fremde_sache_und_volle_sache(client):
    s1, (a,) = _sache_mit_foto(client)
    s2 = client.post("/api/sachen", json={"name": "Anderes", "kiste": "K-01"}).json()
    vid = client.post(f"/api/fotos/{a}/vorschlag", json={}).json()["id"]
    assert client.post(f"/api/vorschlaege/{vid}/uebernehmen", json={"sache_id": s2["id"]}).status_code == 422
    voll, ids = _sache_mit_foto(client, 8)
    vid = client.post(f"/api/fotos/{ids[0]}/vorschlag", json={}).json()["id"]
    assert client.post(f"/api/vorschlaege/{vid}/uebernehmen", json={"sache_id": voll["id"]}).status_code == 409


def test_maske_wird_zwischengespeichert(client, monkeypatch):
    s, (fid,) = _sache_mit_foto(client)
    client.post(f"/api/fotos/{fid}/vorschlag", json={})
    monkeypatch.setattr(aufbereiten.ISNET, "maske", lambda rgb: (_ for _ in ()).throw(AssertionError("neu gerechnet")))
    assert client.post(f"/api/fotos/{fid}/vorschlag", json={"kontrast": 10}).status_code == 200


def test_ohne_anmeldung(client):
    client.headers.pop("X-Token")
    assert client.get("/api/aufbereitung").status_code == 401


def test_unsinniger_rahmen_wird_abgewiesen(client):
    """Durchsicht 03.10.2026: ohne Obergrenze legte ein riesiger Rahmen ein riesiges Bild an."""
    s, (fid,) = _sache_mit_foto(client)
    r = client.post(f"/api/fotos/{fid}/vorschlag", json={"rahmen": [0, 0, 1000]})
    assert r.status_code == 422
