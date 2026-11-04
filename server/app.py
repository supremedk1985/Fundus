"""Fundus -- Server.

Wofür: Kleinteile, die selten gebraucht werden, liegen in gestapelten Kisten und Regalen.
Die App fotografiert eine Sache, dieser Server lässt sie erkennen (erkennung.py),
legt sie einer Kiste zu und findet sie später wieder -- mit Stapel und Höhe im
Stapel, damit man weiß, wie viele Kisten man abheben muss.

Der Server ist für das Heimnetz und für unterwegs gedacht (hinter einem Reverse Proxy mit https).
Deshalb verlangt JEDER Endpunkt außer /api/gesundheit und /api/anmelden ein Token (Kopfzeile X-Token).

Schalter (Umgebung):
- PASSWORT        Passwort beim allerersten Start (danach gespeichert)
- OPENAI_API_KEY  optional; sonst trägt man den Schlüssel in der App ein
- HAUSFUNKTIONEN=1  Küchen-Dashboard ohne Anmeldung aus dem Heimnetz und Selbstupdate der App vom Server --
                  nur für die Haus-Installation des Entwicklers, sonst aus (dann 404)
- DEMO=1          Demo-Server: Bilderkennung mit fester Beispielantwort, Schlüssel nicht änderbar
"""
from __future__ import annotations
import json, os, re, secrets, time, uuid
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from PIL import Image, ImageOps

import aufbereiten, db, erkennung, etikett, produktlink, suche, texte
from db import V

app = FastAPI(title="Fundus")
HAUS = os.environ.get("HAUSFUNKTIONEN", "") == "1"
if HAUS:
    # Das Küchen-Dashboard liegt auf Port 80 und fragt hier quer an -- erlaubt nur Seiten aus dem Heimnetz.
    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(CORSMiddleware, allow_origin_regex=r"https?://(192\.168\.178\.\d{1,3}|localhost|127\.0\.0\.1)(:\d+)?",
                       allow_methods=["GET", "POST"], allow_headers=["Content-Type"], allow_credentials=False)


@app.middleware("http")
async def nur_im_haus_vorhanden(request: Request, weiter):
    """Küchen-Dashboard (ohne Anmeldung!) und Selbstupdate gibt es nur mit HAUSFUNKTIONEN=1."""
    if not HAUS and (request.url.path.startswith("/api/kachel/") or request.url.path.startswith("/api/app/")):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    return await weiter(request)


def _en(request: Request | None) -> bool:
    return texte.englisch(request.headers.get("accept-language") if request else None)


@app.exception_handler(StarletteHTTPException)
async def meldung_in_der_sprache_der_app(request: Request, f: StarletteHTTPException):
    """Meldungen stehen im Code auf Deutsch; eine englische App bekommt sie übersetzt (texte.py)."""
    detail = texte.uebersetzen(f.detail, _en(request)) if isinstance(f.detail, str) else f.detail
    return JSONResponse({"detail": detail}, status_code=f.status_code, headers=getattr(f, "headers", None))

GRUNDFLAECHEN = [(600, 400), (400, 300), (300, 200)]


@app.on_event("startup")
def start() -> None:
    db.einrichten(os.environ.get("PASSWORT"))
    verwaiste_fotos_wegraeumen()
    alte_vorschlaege_wegraeumen()


# ---------------------------------------------------------------- Anmeldung

BREMSE_VERSUCHE, BREMSE_SEKUNDEN = 5, 15 * 60


def _absender(request: Request) -> str:
    weiter = request.headers.get("x-forwarded-for", "")
    return (weiter.split(",")[0].strip() if weiter else "") or (request.client.host if request.client else "?")


def angemeldet(x_token: str | None) -> None:
    if not x_token:
        raise HTTPException(401, "Nicht angemeldet")
    z = V.execute("SELECT token FROM anmeldungen WHERE token=?", (x_token,)).fetchone()
    if not z:
        raise HTTPException(401, "Die Anmeldung gilt nicht mehr. Bitte neu anmelden.")
    V.execute("UPDATE anmeldungen SET zuletzt=? WHERE token=?", (db.jetzt_iso(), x_token))
    V.commit()


@app.post("/api/anmelden")
def anmelden(daten: dict, request: Request):
    schluessel = _absender(request)
    grenze = time.time() - BREMSE_SEKUNDEN
    V.execute("DELETE FROM fehlversuche WHERE wann < ?", (grenze,))
    n = V.execute("SELECT COUNT(*) n FROM fehlversuche WHERE schluessel=?", (schluessel,)).fetchone()["n"]
    # Zweite Bremse über ALLE Absender: Die Absenderadresse kommt aus X-Forwarded-For, und wer im Heimnetz
    # direkt auf 8095 geht, kann die Kopfzeile selbst setzen und bekäme mit jedem Wert frische Versuche
    # (Durchsicht 03.10.2026). Es gibt nur EIN Konto -- 20 Fehlversuche in 15 Minuten sperren es kurz.
    gesamt = V.execute("SELECT COUNT(*) n FROM fehlversuche").fetchone()["n"]
    if n >= BREMSE_VERSUCHE or gesamt >= 4 * BREMSE_VERSUCHE:
        V.commit()
        raise HTTPException(429, "Zu viele falsche Versuche. Bitte in einer Viertelstunde nochmal.")
    z = V.execute("SELECT passwort FROM konto WHERE id=1").fetchone()
    stimmt = bool(z) and secrets.compare_digest(db.streuwert(str(daten.get("passwort") or "")), z["passwort"])
    if not stimmt:
        V.execute("INSERT INTO fehlversuche (schluessel, wann) VALUES (?, ?)", (schluessel, time.time()))
        V.commit()
        raise HTTPException(401, "Das Passwort stimmt nicht")
    geraet = (str(daten.get("geraet") or "").strip()[:60]) or None
    if geraet:
        V.execute("DELETE FROM anmeldungen WHERE geraet=?", (geraet,))
    token = db.neues_token()
    V.execute("INSERT INTO anmeldungen (token, geraet, angelegt_am, zuletzt) VALUES (?,?,?,?)",
              (token, geraet, db.jetzt_iso(), db.jetzt_iso()))
    V.commit()
    return {"token": token}


@app.post("/api/abmelden")
def abmelden(x_token: str = Header(None)):
    angemeldet(x_token)
    V.execute("DELETE FROM anmeldungen WHERE token=?", (x_token,))
    V.commit()
    return {"ok": True}


# ---------------------------------------------------------------- Kisten

def _kiste_json(z, anzahl: int | None = None) -> dict:
    darueber = 0
    regal = None
    if z["regal_id"]:
        # Im Regal zählt, was im selben Fach auf diesem Platz darüber steht
        r = V.execute("SELECT id, name FROM regale WHERE id=?", (z["regal_id"],)).fetchone()
        regal = {"id": r["id"], "name": r["name"]} if r else None
        darueber = V.execute("SELECT COUNT(*) n FROM kisten WHERE regal_id=? AND reihe=? AND platz=? AND ebene>?",
                             (z["regal_id"], z["reihe"], z["platz"], z["ebene"])).fetchone()["n"]
    elif z["stapel"]:
        darueber = V.execute("SELECT COUNT(*) n FROM kisten WHERE raum=? AND stapel=? AND position<?",
                             (z["raum"], z["stapel"], z["position"])).fetchone()["n"]
    if anzahl is None:
        anzahl = _anzahl_je_kiste().get(z["id"], 0)
    return {"code": z["code"], "raum": z["raum"], "stapel": z["stapel"], "position": z["position"], "darueber": darueber,
            "laenge": z["laenge"], "breite": z["breite"], "hoehe": z["hoehe"],
            "notiz": z["notiz"], "art": z["art"], "anzahl": anzahl, "voll": bool(z["voll"]),
            "regal": regal["id"] if regal else None, "regal_name": regal["name"] if regal else "",
            "reihe": z["reihe"] if regal else 0, "platz": z["platz"] if regal else 0, "ebene": z["ebene"] if regal else 0}


def _kiste(code: str):
    z = V.execute("SELECT * FROM kisten WHERE code=?", ((code or "").upper(),)).fetchone()
    if not z:
        raise HTTPException(404, f"Kiste {code} gibt es nicht")
    return z


def _naechster_code() -> str:
    nummern = [int(m.group(1)) for (c,) in V.execute("SELECT code FROM kisten").fetchall()
               if (m := re.fullmatch(r"K-(\d+)", c))]
    n = 1
    while n in nummern:
        n += 1
    return f"K-{n:02d}"


def _stapel_einreihen(kiste_id: int, raum: str, stapel: str, position: int) -> None:
    """Legt die Kiste an Stelle `position` (1 = oben) in den Stapel und nummeriert
    den ganzen Stapel neu durch -- so gibt es nie zwei Kisten auf Platz 2. Ein Stapel ist
    seit 03.10.2026 Raum + Name: „A" im Keller ist ein anderer als „A" in der Werkstatt."""
    andere = [z["id"] for z in V.execute(
        "SELECT id FROM kisten WHERE raum=? AND stapel=? AND id<>? ORDER BY position, id",
        (raum, stapel, kiste_id)).fetchall()]
    position = max(1, min(position, len(andere) + 1))
    andere.insert(position - 1, kiste_id)
    for i, kid in enumerate(andere, start=1):
        V.execute("UPDATE kisten SET raum=?, stapel=?, position=? WHERE id=?", (raum, stapel, i, kid))


def _stapel_nachruecken(raum: str, stapel: str) -> None:
    for i, z in enumerate(V.execute("SELECT id FROM kisten WHERE raum=? AND stapel=? ORDER BY position, id",
                                    (raum, stapel)).fetchall(), start=1):
        V.execute("UPDATE kisten SET position=? WHERE id=?", (i, z["id"]))


def _raum_name(wert) -> str:
    """Raumname aus der Anfrage; ein neuer Name legt den Raum gleich mit an (Reihenfolge: hinten)."""
    name = " ".join(str(wert or "").split())[:30]
    if name and not V.execute("SELECT 1 FROM raeume WHERE name=?", (name,)).fetchone():
        nr = V.execute("SELECT COALESCE(MAX(nr), 0) + 1 n FROM raeume").fetchone()["n"]
        V.execute("INSERT INTO raeume (name, nr) VALUES (?, ?)", (name, nr))
    return name


def _raeume() -> list[dict]:
    zahlen = {z["raum"]: z["n"] for z in V.execute("SELECT raum, COUNT(*) n FROM kisten GROUP BY raum").fetchall()}
    return [{"id": z["id"], "name": z["name"], "nr": z["nr"], "kisten": zahlen.get(z["name"], 0)}
            for z in V.execute("SELECT * FROM raeume ORDER BY nr, name").fetchall()]


def _reihen_lesen(wert) -> list[dict]:
    """Reihen eines Regals aus der Anfrage: 1–8 Reihen, je 1–6 Plätze nebeneinander und 1–4 Kisten übereinander."""
    if not isinstance(wert, list) or not 1 <= len(wert) <= 8:
        raise HTTPException(422, "Ein Regal hat 1 bis 8 Reihen")
    aus = []
    for i, r in enumerate(wert, start=1):
        try:
            breit, hoch = int(r.get("breit")), int(r.get("hoch") or 1)
        except (AttributeError, TypeError, ValueError):
            raise HTTPException(422, f"Reihe {i}: Plätze und Höhe müssen Zahlen sein")
        if not (1 <= breit <= 6 and 1 <= hoch <= 4):
            raise HTTPException(422, f"Reihe {i}: 1–6 Plätze nebeneinander, 1–4 Kisten übereinander")
        aus.append({"breit": breit, "hoch": hoch})
    return aus


def _regal(rid) -> object:
    r = V.execute("SELECT * FROM regale WHERE id=?", (rid,)).fetchone()
    if not r:
        raise HTTPException(404, "Das Regal gibt es nicht")
    return r


def _regale() -> list[dict]:
    zahlen = {z["regal_id"]: z["n"] for z in V.execute(
        "SELECT regal_id, COUNT(*) n FROM kisten WHERE regal_id IS NOT NULL GROUP BY regal_id").fetchall()}
    return [{"id": r["id"], "raum": r["raum"], "name": r["name"], "nr": r["nr"], "reihen": json.loads(r["reihen"]),
             "kisten": zahlen.get(r["id"], 0)}
            for r in V.execute("SELECT * FROM regale ORDER BY raum, nr, name").fetchall()]


def _fach_nachruecken(regal_id, reihe: int, platz: int) -> None:
    """Nimmt man eine Kiste aus einem Fach, rutschen die darüber nach unten -- Lücken gibt es nicht."""
    for i, z in enumerate(V.execute("SELECT id FROM kisten WHERE regal_id=? AND reihe=? AND platz=? ORDER BY ebene, id",
                                    (regal_id, reihe, platz)).fetchall(), start=1):
        V.execute("UPDATE kisten SET ebene=? WHERE id=?", (i, z["id"]))


def _aus_altem_ort(z) -> None:
    """Nachrücken am Ort, den die Kiste gerade verlassen hat (Stapel oder Regalfach)."""
    if z["regal_id"]:
        _fach_nachruecken(z["regal_id"], z["reihe"], z["platz"])
    elif z["stapel"]:
        _stapel_nachruecken(z["raum"], z["stapel"])


def _masse(daten: dict) -> tuple:
    werte = []
    for feld in ("laenge", "breite", "hoehe"):
        w = daten.get(feld)
        if w in (None, ""):
            werte.append(None)
            continue
        try:
            w = int(w)
        except (TypeError, ValueError):
            raise HTTPException(422, f"{feld} muss eine Zahl in Millimetern sein")
        if not 20 <= w <= 1500:
            raise HTTPException(422, f"{feld} ({w} mm) ist keine Kistengröße")
        werte.append(w)
    return tuple(werte)


@app.get("/api/kisten")
def kisten(x_token: str = Header(None)):
    angemeldet(x_token)
    zahlen = _anzahl_je_kiste()
    zeilen = V.execute("SELECT * FROM kisten ORDER BY raum, stapel, position, code").fetchall()
    return {"kisten": [_kiste_json(z, zahlen.get(z["id"], 0)) for z in zeilen], "raeume": _raeume(),
            "regale": _regale(),
            "grundflaechen": [{"laenge": l, "breite": b} for l, b in GRUNDFLAECHEN]}


@app.post("/api/kisten")
def kiste_anlegen(daten: dict, x_token: str = Header(None)):
    angemeldet(x_token)
    laenge, breite, hoehe = _masse(daten)
    stapel = str(daten.get("stapel") or "").strip().upper()[:12]
    raum = _raum_name(daten.get("raum"))
    code = _naechster_code()
    cur = V.execute("INSERT INTO kisten (code, raum, stapel, position, laenge, breite, hoehe, notiz, art, angelegt_am) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (code, raum, stapel, 1, laenge, breite, hoehe, str(daten.get("notiz") or "")[:200],
                     str(daten.get("art") or "").strip()[:40], db.jetzt_iso()))
    if stapel:
        _stapel_einreihen(cur.lastrowid, raum, stapel, int(daten.get("position") or 1))
    V.commit()
    return _kiste_json(_kiste(code))


@app.get("/api/raeume")
def raeume(x_token: str = Header(None)):
    angemeldet(x_token)
    return {"raeume": _raeume()}


@app.post("/api/raeume")
def raum_anlegen(daten: dict, x_token: str = Header(None)):
    angemeldet(x_token)
    name = " ".join(str(daten.get("name") or "").split())[:30]
    if not name:
        raise HTTPException(422, "Der Raum braucht einen Namen")
    if V.execute("SELECT 1 FROM raeume WHERE name=?", (name,)).fetchone():
        raise HTTPException(409, f"Den Raum „{name}“ gibt es schon")
    _raum_name(name)
    V.commit()
    return {"raeume": _raeume()}


@app.patch("/api/raeume/{rid}")
def raum_aendern(rid: int, daten: dict, x_token: str = Header(None)):
    """Umbenennen (zieht die Kisten mit) oder Reihenfolge ändern."""
    angemeldet(x_token)
    r = V.execute("SELECT * FROM raeume WHERE id=?", (rid,)).fetchone()
    if not r:
        raise HTTPException(404, "Den Raum gibt es nicht")
    if "name" in daten:
        name = " ".join(str(daten["name"] or "").split())[:30]
        if not name:
            raise HTTPException(422, "Der Raum braucht einen Namen")
        if name != r["name"] and V.execute("SELECT 1 FROM raeume WHERE name=?", (name,)).fetchone():
            raise HTTPException(409, f"Den Raum „{name}“ gibt es schon")
        V.execute("UPDATE raeume SET name=? WHERE id=?", (name, rid))
        V.execute("UPDATE kisten SET raum=? WHERE raum=?", (name, r["name"]))
        V.execute("UPDATE regale SET raum=? WHERE raum=?", (name, r["name"]))
    if "nr" in daten:
        # Neue Stelle in der Reihenfolge; die anderen rücken nach.
        ids = [z["id"] for z in V.execute("SELECT id FROM raeume WHERE id<>? ORDER BY nr, name", (rid,)).fetchall()]
        stelle = max(1, min(int(daten["nr"]), len(ids) + 1))
        ids.insert(stelle - 1, rid)
        for i, x in enumerate(ids, start=1):
            V.execute("UPDATE raeume SET nr=? WHERE id=?", (i, x))
    V.commit()
    return {"raeume": _raeume()}


@app.delete("/api/raeume/{rid}")
def raum_loeschen(rid: int, x_token: str = Header(None)):
    angemeldet(x_token)
    r = V.execute("SELECT * FROM raeume WHERE id=?", (rid,)).fetchone()
    if not r:
        raise HTTPException(404, "Den Raum gibt es nicht")
    n = V.execute("SELECT COUNT(*) n FROM kisten WHERE raum=?", (r["name"],)).fetchone()["n"]
    if n:
        raise HTTPException(409, f"Im Raum „{r['name']}“ stehen noch {n} Kisten. Erst verschieben.")
    g = V.execute("SELECT name FROM regale WHERE raum=?", (r["name"],)).fetchone()
    if g:
        raise HTTPException(409, f"Im Raum „{r['name']}“ steht noch das Regal „{g['name']}“. Erst das Regal löschen.")
    V.execute("DELETE FROM raeume WHERE id=?", (rid,))
    V.commit()
    return {"raeume": _raeume()}


@app.get("/api/kisten/{code}")
def kiste_lesen(code: str, x_token: str = Header(None)):
    angemeldet(x_token)
    z = _kiste(code)
    # Zuletzt hineingelegt zuerst (Daniel, 01.10.2026) -- was man gerade eingeräumt hat, will man oben sehen.
    sachen = V.execute("SELECT * FROM sachen WHERE kiste_id=? ORDER BY angelegt_am DESC, id DESC", (z["id"],)).fetchall()
    # Aufgebrauchtes zählt nicht als Inhalt, bleibt aber in der Liste (die App zeigt es getrennt als „nachkaufen")
    return {**_kiste_json(z, sum(1 for s in sachen if not _leer(s["menge"]))), "sachen": [_sache_json(s, z) for s in sachen]}


@app.patch("/api/kisten/{code}")
def kiste_aendern(code: str, daten: dict, x_token: str = Header(None)):
    angemeldet(x_token)
    z = _kiste(code)
    if any(f in daten for f in ("laenge", "breite", "hoehe")):
        alt = {"laenge": z["laenge"], "breite": z["breite"], "hoehe": z["hoehe"]}
        laenge, breite, hoehe = _masse({**alt, **daten})
        V.execute("UPDATE kisten SET laenge=?, breite=?, hoehe=? WHERE id=?", (laenge, breite, hoehe, z["id"]))
    if "notiz" in daten:
        V.execute("UPDATE kisten SET notiz=? WHERE id=?", (str(daten["notiz"] or "")[:200], z["id"]))
    if "art" in daten:
        V.execute("UPDATE kisten SET art=? WHERE id=?", (str(daten["art"] or "").strip()[:40], z["id"]))
    if "voll" in daten:
        V.execute("UPDATE kisten SET voll=? WHERE id=?", (1 if daten["voll"] is True else 0, z["id"]))
    if any(f in daten for f in ("stapel", "position", "raum", "regal")):
        # Auch das Verschieben per Ziehen in der App (03.10.2026) landet hier: Raum, Stapel und Platz auf einmal.
        _kiste_verschieben(z, daten)
    V.commit()
    return _kiste_json(_kiste(code))


def _kiste_verschieben(z, daten: dict, raeume_anlegen: bool = True) -> None:
    """Raum, Stapel und Platz einer Kiste setzen und beide Stapel nachnummerieren. Vom Küchentablet aus
    (`raeume_anlegen=False`) nur in Räume, die es schon gibt -- Räume legt man in der App an.
    Seit 05.10.2026 auch ins Regal: `regal` + `reihe` + `platz` (+ optional `ebene`, 1 = unten; ohne kommt sie obendrauf)."""
    if daten.get("regal"):
        _ins_regal(z, daten)
        return
    neu = str(daten.get("stapel", "" if z["regal_id"] else z["stapel"]) or "").strip().upper()[:12]
    if "raum" in daten:
        name = " ".join(str(daten["raum"] or "").split())[:30]
        if name and not raeume_anlegen and not V.execute("SELECT 1 FROM raeume WHERE name=?", (name,)).fetchone():
            raise HTTPException(422, f"Den Raum „{name}“ gibt es nicht")
        neu_raum = _raum_name(name)
    else:
        neu_raum = z["raum"]
    alt = (z["raum"], z["stapel"])
    if z["regal_id"]:
        V.execute("UPDATE kisten SET regal_id=NULL, reihe=0, platz=0, ebene=0 WHERE id=?", (z["id"],))
        _fach_nachruecken(z["regal_id"], z["reihe"], z["platz"])
    if neu:
        _stapel_einreihen(z["id"], neu_raum, neu, int(daten.get("position") or (1 if z["regal_id"] else z["position"]) or 1))
    else:
        V.execute("UPDATE kisten SET raum=?, stapel='', position=1 WHERE id=?", (neu_raum, z["id"]))
    if alt[1] and alt != (neu_raum, neu):
        _stapel_nachruecken(*alt)


def _ins_regal(z, daten: dict) -> None:
    try:
        rid, reihe, platz = int(daten["regal"]), int(daten.get("reihe") or 0), int(daten.get("platz") or 0)
    except (TypeError, ValueError):
        raise HTTPException(422, "Regal, Reihe und Platz müssen Zahlen sein")
    r = _regal(rid)
    reihen = json.loads(r["reihen"])
    if not 1 <= reihe <= len(reihen):
        raise HTTPException(422, f"„{r['name']}“ hat {len(reihen)} Reihen")
    if not 1 <= platz <= reihen[reihe - 1]["breit"]:
        raise HTTPException(422, f"Reihe {reihe} hat {reihen[reihe - 1]['breit']} Plätze")
    hoch = reihen[reihe - 1]["hoch"]
    andere = [x["id"] for x in V.execute(
        "SELECT id FROM kisten WHERE regal_id=? AND reihe=? AND platz=? AND id<>? ORDER BY ebene, id",
        (rid, reihe, platz, z["id"])).fetchall()]
    if len(andere) >= hoch:
        raise HTTPException(409, f"Auf diesem Platz ist kein Platz mehr ({hoch} übereinander)")
    ebene = max(1, min(int(daten.get("ebene") or len(andere) + 1), len(andere) + 1))
    if z["stapel"]:
        V.execute("UPDATE kisten SET stapel='', position=1 WHERE id=?", (z["id"],))
        _stapel_nachruecken(z["raum"], z["stapel"])
    andere.insert(ebene - 1, z["id"])
    V.execute("UPDATE kisten SET raum=?, stapel='', position=1, regal_id=?, reihe=?, platz=? WHERE id=?",
              (r["raum"], rid, reihe, platz, z["id"]))
    for i, kid in enumerate(andere, start=1):
        V.execute("UPDATE kisten SET ebene=? WHERE id=?", (i, kid))
    if z["regal_id"] and (z["regal_id"], z["reihe"], z["platz"]) != (rid, reihe, platz):
        _fach_nachruecken(z["regal_id"], z["reihe"], z["platz"])


# ---------------------------------------------------------------- Regale (05.10.2026)

@app.get("/api/regale")
def regale(x_token: str = Header(None)):
    angemeldet(x_token)
    return {"regale": _regale()}


@app.post("/api/regale")
def regal_anlegen(daten: dict, x_token: str = Header(None)):
    angemeldet(x_token)
    raum = _raum_name(daten.get("raum"))
    reihen = _reihen_lesen(daten.get("reihen"))
    name = " ".join(str(daten.get("name") or "").split())[:30]
    if not name:
        n = V.execute("SELECT COUNT(*) n FROM regale WHERE raum=?", (raum,)).fetchone()["n"]
        name = f"Regal {n + 1}"
    if V.execute("SELECT 1 FROM regale WHERE raum=? AND name=?", (raum, name)).fetchone():
        raise HTTPException(409, f"Ein Regal „{name}“ gibt es in diesem Raum schon")
    nr = V.execute("SELECT COALESCE(MAX(nr), 0) + 1 n FROM regale WHERE raum=?", (raum,)).fetchone()["n"]
    cur = V.execute("INSERT INTO regale (raum, name, nr, reihen) VALUES (?,?,?,?)", (raum, name, nr, json.dumps(reihen)))
    V.commit()
    return next(x for x in _regale() if x["id"] == cur.lastrowid)


@app.patch("/api/regale/{rid}")
def regal_aendern(rid: int, daten: dict, x_token: str = Header(None)):
    """Name, Reihen (nur so, dass jede Kiste ihren Platz behält) oder Reihenfolge im Raum."""
    angemeldet(x_token)
    r = _regal(rid)
    if "name" in daten:
        name = " ".join(str(daten["name"] or "").split())[:30]
        if not name:
            raise HTTPException(422, "Das Regal braucht einen Namen")
        if name != r["name"] and V.execute("SELECT 1 FROM regale WHERE raum=? AND name=?", (r["raum"], name)).fetchone():
            raise HTTPException(409, f"Ein Regal „{name}“ gibt es in diesem Raum schon")
        V.execute("UPDATE regale SET name=? WHERE id=?", (name, rid))
    if "reihen" in daten:
        reihen = _reihen_lesen(daten["reihen"])
        for k in V.execute("SELECT code, reihe, platz, ebene FROM kisten WHERE regal_id=?", (rid,)).fetchall():
            if k["reihe"] > len(reihen) or k["platz"] > reihen[k["reihe"] - 1]["breit"] or k["ebene"] > reihen[k["reihe"] - 1]["hoch"]:
                raise HTTPException(409, f"{k['code']} stünde dann außerhalb des Regals (Reihe {k['reihe']}, Platz {k['platz']}). "
                                         "Erst umstellen.")
        V.execute("UPDATE regale SET reihen=? WHERE id=?", (json.dumps(reihen), rid))
    if "nr" in daten:
        ids = [x["id"] for x in V.execute("SELECT id FROM regale WHERE raum=? AND id<>? ORDER BY nr, name",
                                         (r["raum"], rid)).fetchall()]
        stelle = max(1, min(int(daten["nr"]), len(ids) + 1))
        ids.insert(stelle - 1, rid)
        for i, x in enumerate(ids, start=1):
            V.execute("UPDATE regale SET nr=? WHERE id=?", (i, x))
    V.commit()
    return next(x for x in _regale() if x["id"] == rid)


@app.delete("/api/regale/{rid}")
def regal_loeschen(rid: int, x_token: str = Header(None)):
    angemeldet(x_token)
    r = _regal(rid)
    n = V.execute("SELECT COUNT(*) n FROM kisten WHERE regal_id=?", (rid,)).fetchone()["n"]
    if n:
        raise HTTPException(409, f"Im Regal „{r['name']}“ stehen noch {n} Kisten. Erst umstellen.")
    V.execute("DELETE FROM regale WHERE id=?", (rid,))
    V.commit()
    return {"regale": _regale()}


@app.delete("/api/kisten/{code}")
def kiste_loeschen(code: str, x_token: str = Header(None)):
    angemeldet(x_token)
    z = _kiste(code)
    n = V.execute("SELECT COUNT(*) n FROM sachen WHERE kiste_id=?", (z["id"],)).fetchone()["n"]
    if n:
        raise HTTPException(409, f"In {z['code']} liegen noch {n} Sachen. Erst umräumen oder löschen.")
    V.execute("DELETE FROM kisten WHERE id=?", (z["id"],))
    _aus_altem_ort(z)
    V.commit()
    return {"ok": True}


def _etikett_mass(breite: float, hoehe: float) -> tuple[float, float]:
    if not (15 <= breite <= 200 and 10 <= hoehe <= 200):
        raise HTTPException(422, "Etikettengröße außerhalb von 15–200 mm")
    return breite, hoehe


@app.get("/api/kisten/{code}/etikett.png")
def etikett_png(code: str, breite: float = 40, hoehe: float = 30, x_token: str = Header(None)):
    angemeldet(x_token)
    z = _kiste(code)
    b, h = _etikett_mass(breite, hoehe)
    return Response(etikett.als_png(z["code"], b, h), media_type="image/png",
                    headers={"Content-Disposition": f'inline; filename="Etikett {z["code"]}.png"'})


@app.get("/api/etiketten.pdf")
def etiketten_pdf(codes: str = "", breite: float = 40, hoehe: float = 30, x_token: str = Header(None)):
    angemeldet(x_token)
    b, h = _etikett_mass(breite, hoehe)
    liste = [c.strip().upper() for c in codes.split(",") if c.strip()] or \
        [r["code"] for r in V.execute("SELECT code FROM kisten ORDER BY code").fetchall()]
    if not liste:
        raise HTTPException(404, "Es gibt noch keine Kisten")
    liste = [_kiste(c)["code"] for c in liste]
    return Response(etikett.als_pdf(liste, b, h), media_type="application/pdf",
                    headers={"Content-Disposition": 'inline; filename="Etiketten.pdf"'})


# ---------------------------------------------------------------- Fotos

def _fotopfad(fid: str, klein: bool = False) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", fid or ""):
        raise HTTPException(404, "Foto gibt es nicht")
    return os.path.join(db.FOTOS, f"{fid}_klein.jpg" if klein else f"{fid}.jpg")


@app.post("/api/fotos")
def foto_hochladen(datei: UploadFile = File(...), x_token: str = Header(None)):
    angemeldet(x_token)
    # def statt async def: Pillow verkleinert hier, und auf der Ereignisschleife hielt das alle anderen
    # Antworten an (Durchsicht 03.10.2026). Höchstens 25 MB + 1 Byte in den Speicher.
    inhalt = datei.file.read(25 * 1024 * 1024 + 1)
    if len(inhalt) > 25 * 1024 * 1024:
        raise HTTPException(413, "Das Foto ist größer als 25 MB")
    fid = _foto_speichern(inhalt)
    _gelegentlich_aufraeumen()
    return {"id": fid}


_zuletzt_aufgeraeumt = [time.time()]


def _gelegentlich_aufraeumen() -> None:
    """Höchstens einmal am Tag verwaiste Fotos wegräumen -- vorher nur beim Start des Dienstes, und der
    läuft wochenlang durch (Durchsicht 03.10.2026)."""
    if time.time() - _zuletzt_aufgeraeumt[0] > 86400:
        _zuletzt_aufgeraeumt[0] = time.time()
        try:
            verwaiste_fotos_wegraeumen()
        except Exception as f:
            print(f"[fundus] Aufräumen gescheitert: {f}", flush=True)


@app.get("/api/fotos/{fid}")
def foto_lesen(fid: str, gross: int = 0, x_token: str = Header(None)):
    angemeldet(x_token)
    pfad = _fotopfad(fid, klein=not gross)
    if not os.path.exists(pfad):
        raise HTTPException(404, "Foto gibt es nicht")
    # Kein langes Zwischenspeichern mehr, seit Fotos gedreht werden können.
    return FileResponse(pfad, media_type="image/jpeg", headers={"Cache-Control": "private, no-cache"})


@app.post("/api/fotos/{fid}/drehen")
def foto_drehen(fid: str, daten: dict, x_token: str = Header(None)):
    """Dreht ein Foto dauerhaft (Daniel, 01.10.2026: „das Drehen soll auch gespeichert werden").
    Gedreht werden die Dateien selbst, groß und klein -- dann zeigt jede Ansicht es richtig herum.
    Die Antwort trägt eine neue `fassung`, damit die App ihren Zwischenspeicher verwirft."""
    angemeldet(x_token)
    grad = daten.get("grad")
    if grad not in (90, -90, 180, 270):
        raise HTTPException(422, "grad muss 90, -90, 180 oder 270 sein")
    pfade = [_fotopfad(fid), _fotopfad(fid, klein=True)]
    if not all(os.path.exists(p) for p in pfade):
        raise HTTPException(404, "Foto gibt es nicht")
    for p in pfade:
        with Image.open(p) as b:
            # PIL dreht gegen den Uhrzeigersinn; „90" heißt hier: im Uhrzeigersinn, wie der Knopf in der App.
            gedreht = b.rotate(-grad, expand=True)
        gedreht.save(p, "JPEG", quality=88)
    return {"id": fid, "fassung": int(os.path.getmtime(pfade[0]) * 1000)}


def verwaiste_fotos_wegraeumen(tage: int = 7) -> int:
    grenze = (datetime.now(timezone.utc) - timedelta(days=tage)).isoformat(timespec="seconds")
    alte = V.execute("SELECT id FROM fotos WHERE sache_id IS NULL AND angelegt_am < ?", (grenze,)).fetchall()
    for z in alte:
        for klein in (False, True):
            try:
                os.remove(_fotopfad(z["id"], klein))
            except OSError:
                pass
        # Auch die Freistell-Maske des Fotos (Bilder optimieren) -- sie blieb sonst für immer liegen.
        try:
            os.remove(os.path.join(db.DATEN, "masken", z["id"] + ".png"))
        except OSError:
            pass
        V.execute("DELETE FROM fotos WHERE id=?", (z["id"],))
    V.commit()
    return len(alte)


# ---------------------------------------------------------------- Bilder optimieren (02.10.2026)
# Immer nur ein VORSCHLAG: gerechnet wird in einen eigenen Ordner, ins Fotoalbum der Sache kommt
# das Bild erst mit „Übernehmen". Das Original bleibt immer in der Sache.

def _ordner(name: str) -> str:
    pfad = os.path.join(db.DATEN, name)
    os.makedirs(pfad, exist_ok=True)
    return pfad


def _vorschlagspfad(vid: str, endung: str = ".jpg") -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", vid or ""):
        raise HTTPException(404, "Diesen Vorschlag gibt es nicht")
    return os.path.join(_ordner("vorschlaege"), vid + endung)


def _original_von(fid: str) -> str:
    """Ein schon aufbereitetes Foto wird immer vom ORIGINAL aus neu gerechnet, nie vom Ergebnis."""
    z = V.execute("SELECT original FROM aufbereitung WHERE neu=?", (fid,)).fetchone()
    return z["original"] if z else fid


def alte_vorschlaege_wegraeumen(stunden: int = 24) -> None:
    grenze = time.time() - stunden * 3600
    ordner = _ordner("vorschlaege")
    for name in os.listdir(ordner):
        p = os.path.join(ordner, name)
        try:
            if os.path.getmtime(p) < grenze:
                os.remove(p)
        except OSError:
            pass


@app.post("/api/fotos/{fid}/vorschlag")
def foto_vorschlag(fid: str, daten: dict, x_token: str = Header(None)):
    """Rechnet einen Vorschlag. `daten`: zuschneiden, verbessern, freistellen, rahmen [x,y,seite] (Anteile),
    helligkeit, kontrast, schaerfe, pinsel [{art: dazu|weg, breite, punkte: [[x,y], …]}] (Anteile des Originals).
    `wie_zuletzt: true` nimmt die Einstellungen der letzten Übernahme dieses Fotos als Ausgang."""
    angemeldet(x_token)
    return _vorschlag_rechnen(fid, daten)


def _vorschlag_rechnen(fid: str, daten: dict) -> dict:
    original = _original_von(fid)
    pfad = _fotopfad(original)
    if not os.path.exists(pfad):
        raise HTTPException(404, "Foto gibt es nicht")
    if not aufbereiten.ISNET.vorhanden():
        raise HTTPException(503, "Das Freistell-Modell fehlt auf dem Server")
    einst = dict(daten or {})
    if einst.pop("wie_zuletzt", False):
        z = V.execute("SELECT einstellungen FROM aufbereitung WHERE original=? AND stand='uebernommen'",
                      (original,)).fetchone()
        if z:
            zuletzt = json.loads(z["einstellungen"] or "{}")
            zuletzt.update({k: v for k, v in einst.items() if v is not None})
            einst = zuletzt
    try:
        bild, benutzt = aufbereiten.vorschlag(pfad, os.path.join(_ordner("masken"), original + ".png"), einst)
    except (TypeError, ValueError) as f:
        raise HTTPException(422, f"Ungültige Einstellung: {f}")
    alte_vorschlaege_wegraeumen()
    vid = uuid.uuid4().hex
    aufbereiten.speichern_jpeg(bild, _vorschlagspfad(vid), 90)
    import cv2
    klein = cv2.resize(bild, (600, 600), interpolation=cv2.INTER_AREA) if bild.shape[0] > 600 else bild
    aufbereiten.speichern_jpeg(klein, _vorschlagspfad(vid, "_klein.jpg"), 85)
    with open(_vorschlagspfad(vid, ".json"), "w") as f:
        json.dump({"original": original, "einstellungen": benutzt}, f)
    return {"id": vid, "original": original, "einstellungen": benutzt}


@app.get("/api/vorschlaege/{vid}")
def vorschlag_lesen(vid: str, gross: int = 0, x_token: str = Header(None)):
    angemeldet(x_token)
    pfad = _vorschlagspfad(vid, ".jpg" if gross else "_klein.jpg")
    if not os.path.exists(pfad):
        raise HTTPException(404, "Diesen Vorschlag gibt es nicht mehr")
    return FileResponse(pfad, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@app.post("/api/vorschlaege/{vid}/uebernehmen")
def vorschlag_uebernehmen(vid: str, daten: dict, x_token: str = Header(None)):
    """Das aufbereitete Bild wird Titelbild der Sache, das Original bleibt gleich dahinter.
    Eine frühere Aufbereitung desselben Originals wird ersetzt, nicht verdoppelt."""
    angemeldet(x_token)
    try:
        sid = int(daten.get("sache_id"))
    except (TypeError, ValueError):
        raise HTTPException(422, "sache_id fehlt")
    return _vorschlag_uebernehmen(vid, sid)


def _vorschlag_uebernehmen(vid: str, sid: int) -> dict:
    try:
        with open(_vorschlagspfad(vid, ".json")) as f:
            meta = json.load(f)
        with open(_vorschlagspfad(vid), "rb") as f:
            inhalt = f.read()
    except OSError:
        raise HTTPException(404, "Diesen Vorschlag gibt es nicht mehr -- bitte neu rechnen")
    s = _sache(sid)
    original = meta["original"]
    liste = _fotoliste(s)
    alt = V.execute("SELECT neu FROM aufbereitung WHERE original=?", (original,)).fetchone()
    altes_ergebnis = alt["neu"] if alt else None
    if original not in liste and altes_ergebnis not in liste:
        raise HTTPException(422, "Das Foto gehört nicht zu dieser Sache")
    rest = [x for x in liste if x not in (original, altes_ergebnis)]
    if len(rest) + 2 > MAX_FOTOS:
        raise HTTPException(409, f"Die Sache hat schon {MAX_FOTOS} Fotos -- erst eines entfernen")
    neu = _foto_speichern(inhalt)
    reihe = [neu, original] + rest
    _fotos_setzen(sid, reihe)
    V.execute("INSERT INTO aufbereitung (original, neu, sache_id, stand, einstellungen, am) VALUES (?,?,?,?,?,?) "
              "ON CONFLICT(original) DO UPDATE SET neu=excluded.neu, sache_id=excluded.sache_id, "
              "stand=excluded.stand, einstellungen=excluded.einstellungen, am=excluded.am",
              (original, neu, sid, "uebernommen", json.dumps(meta["einstellungen"]), db.jetzt_iso()))
    V.execute("UPDATE sachen SET geaendert_am=? WHERE id=?", (db.jetzt_iso(), sid))
    V.commit()
    return _sache_json(_sache(sid))


@app.post("/api/fotos/{fid}/ueberspringen")
def foto_ueberspringen(fid: str, x_token: str = Header(None)):
    """„Überspringen" merkt sich das Foto, damit es nicht wieder unter „offen" erscheint."""
    angemeldet(x_token)
    original = _original_von(fid)
    z = V.execute("SELECT sache_id FROM fotos WHERE id=?", (original,)).fetchone()
    if not z:
        raise HTTPException(404, "Foto gibt es nicht")
    V.execute("INSERT INTO aufbereitung (original, neu, sache_id, stand, am) VALUES (?,NULL,?,?,?) "
              "ON CONFLICT(original) DO UPDATE SET stand=CASE WHEN stand='uebernommen' THEN stand ELSE "
              "'uebersprungen' END, am=excluded.am", (original, z["sache_id"], "uebersprungen", db.jetzt_iso()))
    V.commit()
    return {"ok": True}


@app.get("/api/aufbereitung")
def aufbereitung_stand(umfang: str = "offen", x_token: str = Header(None)):
    """Zahlen und Liste für „Bilder optimieren". Gezählt werden nur ORIGINALE (aufbereitete Fotos nicht).
    umfang: offen (Titelbilder ohne Aufbereitung, nicht übersprungen) · titel (alle Titelbilder) · alle."""
    angemeldet(x_token)
    if umfang not in ("offen", "titel", "alle"):
        raise HTTPException(422, "umfang ist offen, titel oder alle")
    stand = {z["original"]: z for z in V.execute("SELECT * FROM aufbereitung").fetchall()}
    ergebnisse = {z["neu"]: z["original"] for z in stand.values() if z["neu"]}
    originale, titel = [], []
    for s in V.execute("SELECT s.id, s.name, s.foto, s.fotos, k.code FROM sachen s JOIN kisten k ON k.id=s.kiste_id "
                       "ORDER BY s.angelegt_am DESC").fetchall():
        liste = _fotoliste(s)
        for i, fid in enumerate(liste):
            if fid in ergebnisse:
                if i == 0:
                    titel.append((ergebnisse[fid], s))
                continue
            originale.append((fid, s))
            if i == 0:
                titel.append((fid, s))
    titel_ids = {f for f, _ in titel}

    def zustand(fid):
        z = stand.get(fid)
        if not z:
            return "offen"
        if z["stand"] == "uebernommen":
            return "aufbereitet"
        return "uebersprungen"

    aufbereitet = sum(1 for f, _ in originale if zustand(f) == "aufbereitet")
    uebersprungen = sum(1 for f, _ in originale if zustand(f) == "uebersprungen")
    offen = [(f, s) for f, s in titel if zustand(f) == "offen"]
    auswahl = {"offen": offen, "titel": titel, "alle": originale}[umfang]
    return {"bilder": len(originale), "aufbereitet": aufbereitet, "uebersprungen": uebersprungen,
            "offen": len(offen), "titelbilder": len(titel), "modell": aufbereiten.ISNET.vorhanden(),
            "modell_geladen": aufbereiten.ISNET.geladen(),
            "liste": [{"foto": f, "sache_id": s["id"], "name": s["name"], "kiste": s["code"],
                       "titel": f in titel_ids, "zustand": zustand(f)} for f, s in auswahl]}


# ---------------------------------------------------------------- Erkennung

def _nutzung_merken(nutzung: dict | None, versuche: list[dict], fehler_modell: str = ""):
    """Jede Anfrage eine Zeile: gescheiterte Claude-Versuche (ok=0, Grund im Modellfeld nach „ · "), dann der Erfolg
    oder -- wenn auch OpenAI scheiterte -- dessen Fehlschlag."""
    for v in versuche:
        V.execute("INSERT INTO nutzung (wann, modell, ok) VALUES (?,?,0)", (db.jetzt_iso(), f"{v['modell']} · {v['grund']}"[:200]))
    if nutzung:
        V.execute("INSERT INTO nutzung (wann, modell, tokens_ein, tokens_aus, ok) VALUES (?,?,?,?,1)",
                  (db.jetzt_iso(), nutzung["modell"], nutzung["tokens_ein"], nutzung["tokens_aus"]))
    elif fehler_modell or not versuche:
        V.execute("INSERT INTO nutzung (wann, modell, ok) VALUES (?,?,0)", (db.jetzt_iso(), fehler_modell or erkennung.modell()))
    V.commit()


@app.post("/api/erkennen")
def erkennen(daten: dict, request: Request, x_token: str = Header(None)):
    angemeldet(x_token)
    ids = [str(i) for i in (daten.get("fotos") or [])][:3]
    if not ids:
        raise HTTPException(422, "Ohne Foto gibt es nichts zu erkennen")
    pfade = [_fotopfad(i) for i in ids]
    if not all(os.path.exists(p) for p in pfade):
        raise HTTPException(404, "Foto gibt es nicht")
    try:
        vorschlag, nutzung = erkennung.erkennen(pfade, _bereiche(), str(daten.get("hinweis") or "")[:200],
                                                en=_en(request))
    except erkennung.ErkennungsFehler as f:
        _nutzung_merken(None, f.versuche, fehler_modell=erkennung.modell() if erkennung.schluessel() else "")
        raise HTTPException(f.status, f.meldung)
    _nutzung_merken(nutzung, nutzung.get("versuche") or [])
    anfrage = " ".join([vorschlag.get("name", "")] + vorschlag.get("woerter", [])[:3])
    aehnliche = [s for p, s in _suchen(anfrage) if p >= 6][:3] if anfrage.strip() else []
    return {"vorschlag": vorschlag, "aehnliche": aehnliche, "gleiche": _gleiche([vorschlag.get("name", "")])}


def _foto_speichern(inhalt: bytes) -> str:
    import io
    try:
        bild = ImageOps.exif_transpose(Image.open(io.BytesIO(inhalt))).convert("RGB")
    except Exception:
        raise HTTPException(422, "Das ist kein lesbares Bild")
    fid = uuid.uuid4().hex
    gross = bild.copy()
    gross.thumbnail((2000, 2000))
    gross.save(_fotopfad(fid), "JPEG", quality=88)
    bild.thumbnail((400, 400))
    bild.save(_fotopfad(fid, klein=True), "JPEG", quality=82)
    V.execute("INSERT INTO fotos (id, angelegt_am) VALUES (?, ?)", (fid, db.jetzt_iso()))
    V.commit()
    return fid


@app.post("/api/aus-link")
def aus_link(daten: dict, request: Request, x_token: str = Header(None)):
    """Geteilter Text aus einer Shop-App (meist Titel + Kurzlink) → Vorschlag wie beim Foto."""
    angemeldet(x_token)
    text = str(daten.get("text") or "").strip()[:2000]
    if not text:
        raise HTTPException(422, "Es wurde kein Link übergeben")
    produkt, bild = produktlink.holen(text)
    if not produkt.titel:
        raise HTTPException(422, "Aus dem Link ließ sich nichts lesen. Du kannst die Sache von Hand eintragen.")
    fid = None
    if bild:
        try:
            fid = _foto_speichern(bild)
        except HTTPException:
            fid = None
    vorschlag = {"name": produkt.titel[:120], "beschreibung": "", "bereich": "", "menge": "", "woerter": [],
                 "sicherheit": "niedrig"}
    hinweis = ""
    if erkennung.eingerichtet():
        try:
            vorschlag, n = erkennung.erkennen([_fotopfad(fid)] if fid else [], _bereiche(),
                                              produkttext=produktlink.als_text(produkt), en=_en(request))
            _nutzung_merken(n, n.get("versuche") or [])
        except erkennung.ErkennungsFehler as f:
            _nutzung_merken(None, f.versuche, fehler_modell=erkennung.modell() if erkennung.schluessel() else "")
            hinweis = f.meldung
    else:
        hinweis = "Die Bilderkennung ist nicht eingerichtet; übernommen ist nur der Titel."
    if not produkt.seite_gelesen:
        hinweis = (hinweis + " " if hinweis else "") + "Die Produktseite war nicht lesbar, es zählt nur der geteilte Text."
    anfrage = " ".join([vorschlag.get("name", "")] + vorschlag.get("woerter", [])[:3])
    aehnliche = [s for p, s in _suchen(anfrage) if p >= 6][:3]
    gleiche = _gleiche([vorschlag.get("name", ""), produkt.titel], produkt.url or "")
    return {"vorschlag": vorschlag, "aehnliche": aehnliche, "gleiche": gleiche, "foto": fid, "link": produkt.url,
            "hinweis": texte.uebersetzen(hinweis, _en(request))}


@app.get("/api/nutzung")
def nutzung(x_token: str = Header(None)):
    """Wie oft wurde die Bilderkennung diesen Monat gefragt? Damit das Guthaben
    nicht unbemerkt schmilzt."""
    angemeldet(x_token)
    monat = datetime.now(timezone.utc).strftime("%Y-%m")
    z = V.execute("SELECT COUNT(*) n, COALESCE(SUM(ok),0) ok, COALESCE(SUM(tokens_ein),0) ein, "
                  "COALESCE(SUM(tokens_aus),0) aus FROM nutzung WHERE wann LIKE ?", (monat + "%",)).fetchone()
    je = V.execute("SELECT CASE WHEN instr(modell, ' · ') > 0 THEN substr(modell, 1, instr(modell, ' · ') - 1) "
                   "ELSE modell END m, COUNT(*) n, COALESCE(SUM(ok),0) ok FROM nutzung WHERE wann LIKE ? GROUP BY m "
                   "ORDER BY n DESC", (monat + "%",)).fetchall()
    rueckfaelle = V.execute("SELECT wann, modell FROM nutzung WHERE wann LIKE ? AND ok=0 AND modell LIKE 'claude-%' "
                            "ORDER BY wann DESC LIMIT 5", (monat + "%",)).fetchall()
    return {"monat": monat, "anfragen": z["n"], "erfolgreich": z["ok"], "tokens_ein": z["ein"],
            "tokens_aus": z["aus"], "eingerichtet": erkennung.eingerichtet(), "modell": erkennung.modell(),
            "claude": erkennung.claude_eingerichtet(), "claude_modell": erkennung.claude_modell(),
            "je_modell": [{"modell": r["m"], "anfragen": r["n"], "erfolgreich": r["ok"]} for r in je],
            "rueckfaelle": [{"wann": r["wann"], "grund": r["modell"].split(" · ", 1)[-1]} for r in rueckfaelle]}


# ---------------------------------------------------------------- Bilderkennung einstellen (01.11.2026)
# Wer den Server selbst betreibt, trägt seinen OpenAI-Schlüssel in der App ein, statt die Server-Datei zu
# bearbeiten. Der Schlüssel geht nur hinein, nie heraus: GET zeigt die letzten vier Zeichen.

def _ki_stand() -> dict:
    s = erkennung.schluessel()
    return {"eingerichtet": erkennung.eingerichtet(), "quelle": erkennung.quelle(), "demo": erkennung.demo(),
            "ende": s[-4:] if len(s) > 8 else "", "modell": erkennung.modell(),
            "vorgabe_modell": erkennung.VORGABE_MODELL,
            "claude": erkennung.claude_eingerichtet(), "claude_modell": erkennung.claude_modell()}


@app.get("/api/einstellungen/ki")
def ki_einstellungen(x_token: str = Header(None)):
    angemeldet(x_token)
    return _ki_stand()


@app.put("/api/einstellungen/ki")
def ki_einstellen(daten: dict, x_token: str = Header(None)):
    """{"schluessel": "sk-…"} setzt, {"schluessel": ""} löscht; {"modell": "…"} wählt das Modell ("" = Vorgabe)."""
    angemeldet(x_token)
    if erkennung.demo():
        raise HTTPException(403, "Im Demo-Betrieb lässt sich der Schlüssel nicht ändern.")
    hinweis = ""
    if "schluessel" in daten:
        if erkennung.quelle() == "datei":
            raise HTTPException(409, "Der Schlüssel steht in der Server-Datei (.env) und lässt sich nur dort ändern.")
        neu = str(daten.get("schluessel") or "").strip()
        if neu and (not neu.startswith("sk-") or len(neu) < 20 or len(neu) > 300 or any(c.isspace() for c in neu)):
            raise HTTPException(422, "Das sieht nicht wie ein OpenAI-Schlüssel aus (er beginnt mit „sk-“).")
        if neu:
            geprueft = erkennung.schluessel_pruefen(neu)
            if geprueft is False:
                raise HTTPException(422, "Der OpenAI-Schlüssel wird abgelehnt.")
            if geprueft is None:
                hinweis = "Der Schlüssel ist gespeichert, ließ sich aber nicht prüfen (OpenAI nicht erreichbar)."
        db.einstellung_setzen("openai_schluessel", neu)
    if "modell" in daten:
        m = str(daten.get("modell") or "").strip()
        if len(m) > 60 or not re.fullmatch(r"[A-Za-z0-9._:-]*", m):
            raise HTTPException(422, "Ungültige Einstellung: modell")
        db.einstellung_setzen("openai_modell", m)
    return {**_ki_stand(), "hinweis": hinweis}


# ---------------------------------------------------------------- Sachen

def bereich_glaetten(b: str) -> str:
    """„elektro" und „Elektro" sind derselbe Bereich (01.10.2026 standen beide im Katalog): erster Buchstabe groß,
    und gibt es den Bereich schon in anderer Schreibweise, dessen Schreibweise übernehmen."""
    b = " ".join((b or "").split())
    if not b:
        return ""
    b = b[0].upper() + b[1:]
    for (vorhanden,) in V.execute("SELECT DISTINCT bereich FROM sachen WHERE bereich<>''").fetchall():
        if vorhanden.casefold() == b.casefold():
            return vorhanden
    return b


def _bereiche() -> list[str]:
    return [z["bereich"] for z in V.execute(
        "SELECT bereich, COUNT(*) n FROM sachen WHERE bereich<>'' GROUP BY bereich ORDER BY n DESC").fetchall()]


def _fotoliste(s) -> list[str]:
    try:
        liste = json.loads(s["fotos"] or "[]")
    except (ValueError, IndexError, KeyError):
        liste = []
    if not liste and s["foto"]:
        liste = [s["foto"]]
    return liste


def _merkmale_lesen(s) -> list[dict]:
    try:
        return json.loads(s["merkmale"] or "[]")
    except (ValueError, IndexError, KeyError):
        return []


def _sache_json(s, kiste=None) -> dict:
    if kiste is None:
        kiste = V.execute("SELECT * FROM kisten WHERE id=?", (s["kiste_id"],)).fetchone()
    try:
        woerter = json.loads(s["woerter"] or "[]")
    except ValueError:
        woerter = []
    return {"id": s["id"], "name": s["name"], "beschreibung": s["beschreibung"], "bereich": s["bereich"],
            "menge": s["menge"], "woerter": woerter, "foto": s["foto"], "raus_seit": s["raus_seit"],
            # Aufgebraucht (Menge 0, Daniel 08.10.2026): liegt nicht mehr in der Kiste, steht auf der Nachkaufliste
            "leer": _leer(s["menge"]),
            "link": s["link"], "raus_anzahl": s["raus_anzahl"], "fotos": _fotoliste(s), "notiz": s["notiz"],
            "angelegt_am": s["angelegt_am"], "geaendert_am": s["geaendert_am"],
            "merkmale": _merkmale_lesen(s),
            "kiste": _kiste_json(kiste) if kiste else None,
            # Bilder optimieren: aufbereitetes Foto -> sein Original, damit die App „Original" dranschreiben kann
            "aufbereitet": {z["neu"]: z["original"] for z in V.execute(
                "SELECT neu, original FROM aufbereitung WHERE sache_id=? AND neu IS NOT NULL", (s["id"],)).fetchall()
                if z["neu"] in _fotoliste(s)}}


def _felder(daten: dict, pflicht: bool) -> dict:
    f = {}
    if pflicht or "name" in daten:
        name = str(daten.get("name") or "").strip()[:120]
        if not name:
            raise HTTPException(422, "Die Sache braucht einen Namen")
        f["name"] = name
    for feld, laenge in (("beschreibung", 500), ("bereich", 40), ("menge", 40), ("link", 300), ("notiz", 1000)):
        if feld in daten or pflicht:
            f[feld] = str(daten.get(feld) or "").strip()[:laenge]
    if "bereich" in f:
        f["bereich"] = bereich_glaetten(f["bereich"])
    if "merkmale" in daten:
        f["merkmale"] = json.dumps(erkennung.merkmale_glaetten(daten.get("merkmale")), ensure_ascii=False)
    if "woerter" in daten or pflicht:
        ws = daten.get("woerter") or []
        if not isinstance(ws, list):
            raise HTTPException(422, "woerter muss eine Liste sein")
        f["woerter"] = json.dumps(sorted({str(w).strip().lower()[:40] for w in ws if str(w).strip()})[:30],
                                  ensure_ascii=False)
    return f


MAX_FOTOS = 8


def _fotos_setzen(sache_id: int, ids: list) -> list[str]:
    """Setzt die Fotoliste einer Sache (Reihenfolge = Anzeige, erstes = Titelbild). Fotos, die herausfallen,
    werden verwaist und nach einer Woche weggeräumt."""
    if not isinstance(ids, list):
        raise HTTPException(422, "fotos muss eine Liste sein")
    liste = list(dict.fromkeys(str(i) for i in ids if i))[:MAX_FOTOS]
    for fid in liste:
        if not V.execute("SELECT 1 FROM fotos WHERE id=?", (fid,)).fetchone():
            raise HTTPException(422, "Ein Foto gibt es auf dem Server nicht")
    V.execute("UPDATE fotos SET sache_id=NULL WHERE sache_id=?", (sache_id,))
    for fid in liste:
        V.execute("UPDATE fotos SET sache_id=? WHERE id=?", (sache_id, fid))
    V.execute("UPDATE sachen SET fotos=?, foto=? WHERE id=?",
              (json.dumps(liste), liste[0] if liste else None, sache_id))
    return liste


def _foto_zuordnen(fid: str | None, sache_id: int) -> str | None:
    if not fid:
        return None
    if not V.execute("SELECT 1 FROM fotos WHERE id=?", (fid,)).fetchone():
        raise HTTPException(422, "Das Foto gibt es auf dem Server nicht")
    V.execute("UPDATE fotos SET sache_id=? WHERE id=?", (sache_id, fid))
    return fid


@app.post("/api/sachen")
def sache_anlegen(daten: dict, x_token: str = Header(None)):
    angemeldet(x_token)
    kiste = _kiste(str(daten.get("kiste") or ""))
    f = _felder(daten, pflicht=True)
    jetzt = db.jetzt_iso()
    cur = V.execute("INSERT INTO sachen (kiste_id, name, beschreibung, bereich, menge, woerter, link, notiz, angelegt_am, "
                    "geaendert_am, merkmale) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (kiste["id"], f["name"], f["beschreibung"], f["bereich"], f["menge"], f["woerter"], f["link"],
                     f["notiz"], jetzt, jetzt, f.get("merkmale")))
    if "fotos" in daten:
        _fotos_setzen(cur.lastrowid, daten["fotos"])
    elif daten.get("foto"):
        _fotos_setzen(cur.lastrowid, [daten["foto"]])
    V.commit()
    # Einräumen am Fließband (03.11.2026): Zuschnitt und Farbe macht der Server danach von selbst -- Freistellen
    # NICHT (Daniel: „Das Freistellen darf nicht dazugehören"; 38-mal abgeschaltet, 9-mal nachgebessert).
    if daten.get("aufbereiten") and (_fotoliste(V.execute("SELECT * FROM sachen WHERE id=?", (cur.lastrowid,)).fetchone())):
        nach_dem_anlegen_aufbereiten(cur.lastrowid)
    return _sache_json(V.execute("SELECT * FROM sachen WHERE id=?", (cur.lastrowid,)).fetchone())


def _sache(sid: int):
    s = V.execute("SELECT * FROM sachen WHERE id=?", (sid,)).fetchone()
    if not s:
        raise HTTPException(404, "Die Sache gibt es nicht mehr")
    return s


@app.get("/api/sachen/{sid}")
def sache_lesen(sid: int, x_token: str = Header(None)):
    angemeldet(x_token)
    return _sache_json(_sache(sid))


@app.patch("/api/sachen/{sid}")
def sache_aendern(sid: int, daten: dict, x_token: str = Header(None)):
    angemeldet(x_token)
    s = _sache(sid)
    f = _felder(daten, pflicht=False)
    if "kiste" in daten:
        f["kiste_id"] = _kiste(str(daten["kiste"]))["id"]
    if "raus" in daten:
        f["raus_seit"] = (s["raus_seit"] or db.jetzt_iso()) if daten["raus"] else None
        anzahl = daten.get("anzahl")
        if daten["raus"] and anzahl is not None:
            try:
                anzahl = int(anzahl)
            except (TypeError, ValueError):
                raise HTTPException(422, "anzahl muss eine Zahl sein")
            if anzahl < 1:
                raise HTTPException(422, "Mindestens ein Stück")
        f["raus_anzahl"] = anzahl if daten["raus"] else None
    if "fotos" in daten:
        _fotos_setzen(sid, daten["fotos"])
        f.setdefault("geaendert_am", db.jetzt_iso())
    elif "foto" in daten and daten["foto"] != s["foto"]:
        _fotos_setzen(sid, [daten["foto"]] + [x for x in _fotoliste(s) if x != daten["foto"]])
        f.setdefault("geaendert_am", db.jetzt_iso())
    if f:
        f["geaendert_am"] = db.jetzt_iso()
        V.execute(f"UPDATE sachen SET {', '.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), sid))
        V.commit()
    return _sache_json(_sache(sid))


@app.delete("/api/sachen/{sid}")
def sache_loeschen(sid: int, x_token: str = Header(None)):
    angemeldet(x_token)
    return _sache_entfernen(_sache(sid))


def _sache_entfernen(s) -> dict:
    sid = s["id"]
    V.execute("DELETE FROM sachen WHERE id=?", (sid,))
    # In Stücklisten bleibt das Teil stehen -- mit dem Namen der Sache als Text, nur ohne Verweis.
    V.execute("UPDATE projekt_teile SET text=CASE WHEN text='' THEN ? ELSE text END, sache_id=NULL WHERE sache_id=?",
              (s["name"], sid))
    # Das Foto bleibt eine Woche als verwaist liegen und verschwindet dann von selbst.
    V.execute("UPDATE fotos SET sache_id=NULL WHERE sache_id=?", (sid,))
    V.commit()
    return {"ok": True, "name": s["name"]}


def _suchen(anfrage: str) -> list[tuple[float, dict]]:
    kisten = {z["id"]: z for z in V.execute("SELECT * FROM kisten").fetchall()}
    sachen = [dict(z) for z in V.execute("SELECT * FROM sachen").fetchall()]
    ergebnis = []
    for punkte, s in suche.suchen(anfrage, sachen):
        j = _sache_json(s, kisten.get(s["kiste_id"]))
        j["punkte"] = punkte
        ergebnis.append((punkte, j))
    return ergebnis


_ASIN = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})", re.I)


def _linkschluessel(link: str) -> str:
    """Vergleichbarer Kern eines Shop-Links: bei Amazon die ASIN, sonst die Adresse ohne Abfrage und Schrägstrich."""
    link = (link or "").strip()
    if not link:
        return ""
    m = _ASIN.search(link)
    if m:
        return "asin:" + m.group(1).upper()
    return link.split("?")[0].split("#")[0].rstrip("/").lower()


def _gleiche(namen: list[str], link: str = "") -> list[dict]:
    """GENAU dieselbe Sache, nicht bloß ähnlich (Daniel, 02.10.): gleicher Shop-Link oder gleicher Name
    (Groß-/Kleinschreibung, Umlaute, Satzzeichen egal). Für das Auswahlfenster „zu den anderen oder neu?"."""
    ziele = {suche.normalisieren(n) for n in namen if n and suche.normalisieren(n)}
    # Seit 03.11.2026 auch dieselbe Typbezeichnung (RC522, FS1000A): Claude nannte ein RC522 „RFID-Modul RC522
    # (MFRC522, 13,56 MHz)" -- am Namen allein hätte das Einräumen das vorhandene in K-06 nicht gefunden.
    typen = set().union(*(_typen(n) for n in namen if n)) if namen else set()
    lk = _linkschluessel(link)
    kisten = {z["id"]: z for z in V.execute("SELECT * FROM kisten").fetchall()}
    genau, gleicher_typ = [], []
    for z in V.execute("SELECT * FROM sachen ORDER BY angelegt_am DESC").fetchall():
        if suche.normalisieren(z["name"]) in ziele or (lk and _linkschluessel(z["link"] or "") == lk):
            genau.append(_sache_json(z, kisten.get(z["kiste_id"])))
        elif typen and not _leer(z["menge"]) and typen & _typen(z["name"]):
            gleicher_typ.append(_sache_json(z, kisten.get(z["kiste_id"])))
    return (genau + gleicher_typ)[:5]


@app.get("/api/suche")
def suchen(q: str = "", x_token: str = Header(None)):
    angemeldet(x_token)
    if not q.strip():
        return {"treffer": []}
    return {"treffer": [s for _, s in _suchen(q)]}


@app.get("/api/sachen")
def alle_sachen(x_token: str = Header(None)):
    """Der Katalog: alle Sachen mit Kiste, nach Name. Bei ein paar hundert Einträgen in einem Rutsch."""
    angemeldet(x_token)
    kisten = {z["id"]: z for z in V.execute("SELECT * FROM kisten").fetchall()}
    zeilen = V.execute("SELECT * FROM sachen ORDER BY name COLLATE NOCASE").fetchall()
    return {"sachen": [_sache_json(s, kisten.get(s["kiste_id"])) for s in zeilen]}


@app.get("/api/uebersicht")
def uebersicht(x_token: str = Header(None)):
    angemeldet(x_token)
    zuletzt = V.execute("SELECT * FROM sachen ORDER BY angelegt_am DESC, id DESC LIMIT 8").fetchall()
    raus = V.execute("SELECT * FROM sachen WHERE raus_seit IS NOT NULL ORDER BY raus_seit").fetchall()
    leer = _leere_sachen()
    return {"anzahl_sachen": V.execute("SELECT COUNT(*) n FROM sachen").fetchone()["n"] - len(leer),
            # Nachkaufliste (08.10.2026): aufgebraucht, aber bewusst behalten
            "leer": [_sache_json(s) for s in leer],
            "kisten": kisten(x_token)["kisten"], "raeume": _raeume(), "regale": _regale(),
            "zuletzt": [_sache_json(s) for s in zuletzt],
            "raus": [_sache_json(s) for s in raus],
            "bereiche": _bereiche(),
            # Für „Stöbern nach Art" auf der Startseite (03.11.2026): Sachen je Bereich, ohne Aufgebrauchtes
            "bereiche_anzahl": _bereiche_anzahl(),
            "erkennung": erkennung.eingerichtet()}


@app.get("/api/bereiche")
def bereiche(x_token: str = Header(None)):
    angemeldet(x_token)
    return {"bereiche": _bereiche()}


# ---------------------------------------------------------------- Selbstupdate

import hausdienst  # noqa: E402  (gemeinsam mit den anderen Haus-Apps)

WEBROOT = "/webroot"
APK_DATEI = os.path.join(WEBROOT, "fundus.apk")
VERSIONS_DATEI = os.path.join(WEBROOT, "fundus-version.json")


# ---------------------------------------------------------------- Projekte mit Stückliste (03.10.2026)
# Daniel: „eine neue Seite Projekt, wo man eine Stückliste für ein Projekt machen kann."

def _projekt(pid: int):
    p = V.execute("SELECT * FROM projekte WHERE id=?", (pid,)).fetchone()
    if not p:
        raise HTTPException(404, "Das Projekt gibt es nicht mehr")
    return p


def _teil_json(t, sachen: dict, kisten: dict) -> dict:
    s = sachen.get(t["sache_id"]) if t["sache_id"] else None
    return {"id": t["id"], "sache_id": t["sache_id"], "text": t["text"], "menge": t["menge"], "notiz": t["notiz"],
            "link": t["link"], "erledigt": bool(t["erledigt"]), "nr": t["nr"],
            "name": s["name"] if s else t["text"],
            "sache": _sache_json(s, kisten.get(s["kiste_id"])) if s else None}


def _projekt_json(p, mit_teilen: bool = True) -> dict:
    teile = V.execute("SELECT * FROM projekt_teile WHERE projekt_id=? ORDER BY nr, id", (p["id"],)).fetchall()
    j = {"id": p["id"], "name": p["name"], "notiz": p["notiz"], "angelegt_am": p["angelegt_am"],
         "geaendert_am": p["geaendert_am"], "fertig": bool(p["fertig_am"]),
         "anzahl": len(teile), "erledigt": sum(1 for t in teile if t["erledigt"]),
         "fehlen": sum(1 for t in teile if not t["sache_id"] and not t["erledigt"])}
    if mit_teilen:
        ids = [t["sache_id"] for t in teile if t["sache_id"]]
        sachen = {z["id"]: z for z in V.execute(
            f"SELECT * FROM sachen WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()} if ids else {}
        kisten = {z["id"]: z for z in V.execute("SELECT * FROM kisten").fetchall()}
        j["teile"] = [_teil_json(t, sachen, kisten) for t in teile]
    return j


def _projekt_felder(daten: dict, pflicht: bool) -> dict:
    f = {}
    if pflicht or "name" in daten:
        name = str(daten.get("name") or "").strip()[:120]
        if not name:
            raise HTTPException(422, "Das Projekt braucht einen Namen")
        f["name"] = name
    if "notiz" in daten:
        f["notiz"] = str(daten.get("notiz") or "").strip()[:2000]
    if "fertig" in daten:
        f["fertig_am"] = db.jetzt_iso() if daten["fertig"] else None
    return f


def _teil_felder(daten: dict) -> dict:
    f = {}
    if "sache_id" in daten:
        sid = daten.get("sache_id")
        if sid is not None:
            _sache(int(sid))
        f["sache_id"] = int(sid) if sid is not None else None
    for feld, laenge in (("text", 160), ("menge", 40), ("notiz", 500), ("link", 300)):
        if feld in daten:
            f[feld] = str(daten.get(feld) or "").strip()[:laenge]
    if "erledigt" in daten:
        f["erledigt"] = 1 if daten["erledigt"] else 0
    return f


@app.get("/api/projekte")
def projekte(x_token: str = Header(None)):
    angemeldet(x_token)
    zeilen = V.execute("SELECT * FROM projekte ORDER BY fertig_am IS NOT NULL, geaendert_am DESC").fetchall()
    return {"projekte": [_projekt_json(p, mit_teilen=False) for p in zeilen]}


@app.post("/api/projekte")
def projekt_anlegen(daten: dict, x_token: str = Header(None)):
    angemeldet(x_token)
    f = _projekt_felder(daten, pflicht=True)
    jetzt_ = db.jetzt_iso()
    c = V.execute("INSERT INTO projekte (name, notiz, angelegt_am, geaendert_am) VALUES (?,?,?,?)",
                  (f["name"], f.get("notiz", ""), jetzt_, jetzt_))
    V.commit()
    return _projekt_json(_projekt(c.lastrowid))


@app.get("/api/projekte/{pid}")
def projekt_lesen(pid: int, x_token: str = Header(None)):
    angemeldet(x_token)
    return _projekt_json(_projekt(pid))


@app.patch("/api/projekte/{pid}")
def projekt_aendern(pid: int, daten: dict, x_token: str = Header(None)):
    angemeldet(x_token)
    _projekt(pid)
    f = _projekt_felder(daten, pflicht=False)
    if f:
        f["geaendert_am"] = db.jetzt_iso()
        V.execute(f"UPDATE projekte SET {', '.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), pid))
        V.commit()
    return _projekt_json(_projekt(pid))


@app.delete("/api/projekte/{pid}")
def projekt_loeschen(pid: int, x_token: str = Header(None)):
    angemeldet(x_token)
    p = _projekt(pid)
    V.execute("DELETE FROM projekt_teile WHERE projekt_id=?", (pid,))
    V.execute("DELETE FROM projekte WHERE id=?", (pid,))
    V.commit()
    return {"ok": True, "name": p["name"]}


@app.post("/api/projekte/{pid}/teile")
def teil_anlegen(pid: int, daten: dict, x_token: str = Header(None)):
    """Ein Teil: entweder `sache_id` (aus dem Bestand) oder `text` (noch nicht da, z. B. zu kaufen)."""
    angemeldet(x_token)
    _projekt(pid)
    f = _teil_felder(daten)
    if not f.get("sache_id") and not f.get("text"):
        raise HTTPException(422, "Ein Teil braucht eine Sache aus dem Bestand oder einen Namen")
    nr = (V.execute("SELECT MAX(nr) m FROM projekt_teile WHERE projekt_id=?", (pid,)).fetchone()["m"] or 0) + 1
    V.execute("INSERT INTO projekt_teile (projekt_id, sache_id, text, menge, notiz, link, erledigt, nr) VALUES (?,?,?,?,?,?,?,?)",
              (pid, f.get("sache_id"), f.get("text", ""), f.get("menge", ""), f.get("notiz", ""), f.get("link", ""),
               f.get("erledigt", 0), nr))
    V.execute("UPDATE projekte SET geaendert_am=? WHERE id=?", (db.jetzt_iso(), pid))
    V.commit()
    return _projekt_json(_projekt(pid))


def _teil(tid: int):
    t = V.execute("SELECT * FROM projekt_teile WHERE id=?", (tid,)).fetchone()
    if not t:
        raise HTTPException(404, "Das Teil gibt es nicht mehr")
    return t


@app.patch("/api/projektteile/{tid}")
def teil_aendern(tid: int, daten: dict, x_token: str = Header(None)):
    angemeldet(x_token)
    t = _teil(tid)
    f = _teil_felder(daten)
    if f:
        V.execute(f"UPDATE projekt_teile SET {', '.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), tid))
        V.execute("UPDATE projekte SET geaendert_am=? WHERE id=?", (db.jetzt_iso(), t["projekt_id"]))
        V.commit()
    return _projekt_json(_projekt(t["projekt_id"]))


@app.delete("/api/projektteile/{tid}")
def teil_loeschen(tid: int, x_token: str = Header(None)):
    angemeldet(x_token)
    t = _teil(tid)
    V.execute("DELETE FROM projekt_teile WHERE id=?", (tid,))
    V.execute("UPDATE projekte SET geaendert_am=? WHERE id=?", (db.jetzt_iso(), t["projekt_id"]))
    V.commit()
    return _projekt_json(_projekt(t["projekt_id"]))


# ---------------------------------------------------------------- Küchen-Dashboard (03.10.2026)
# Daniel: „auf dem ioBroker-Dashboard Werkstatt Organisator … suchen und so weiter. Nicht gedacht zum
# Hinzufügen." Das Küchentablet meldet sich nirgends an -- deshalb diese Endpunkte OHNE Token, aber NUR
# LESEND und NUR aus dem Haus: Caddy stempelt jede Anfrage von außen mit X-Von-Aussen (wie bei
# MachIchGleich), und dann gibt es hier 403. Fotos nur, wenn sie zu einer Sache gehören.

def _nur_im_haus(request: Request) -> None:
    if request.headers.get("x-von-aussen"):
        raise HTTPException(403, "Nur im Heimnetz")


@app.get("/api/kachel/fundus")
def kachel_fundus(request: Request):
    """Alles in einem Rutsch: Sachen (nach Name), Kisten mit Anzahl, Bereiche nach Häufigkeit."""
    _nur_im_haus(request)
    kisten = {z["id"]: z for z in V.execute("SELECT * FROM kisten").fetchall()}
    zahlen = _anzahl_je_kiste()
    zeilen = V.execute("SELECT * FROM sachen ORDER BY name COLLATE NOCASE").fetchall()
    return {"sachen": [_sache_json(s, kisten.get(s["kiste_id"])) for s in zeilen],
            "kisten": [_kiste_json(z, zahlen.get(z["id"], 0)) for z in
                       V.execute("SELECT * FROM kisten ORDER BY raum, stapel, position, code").fetchall()],
            "raeume": [r["name"] for r in _raeume()],
            "regale": _regale(),
            "bereiche": _bereiche()}


@app.get("/api/kachel/fundus/suche")
def kachel_suche(request: Request, q: str = ""):
    """Dieselbe Suche wie in der App (Tippfehler, Verwendungszweck: „Ding zum Abisolieren")."""
    _nur_im_haus(request)
    return {"treffer": [s for _, s in _suchen(q[:100])][:60] if q.strip() else []}


@app.get("/api/kachel/fundus/foto/{fid}")
def kachel_foto(fid: str, request: Request, gross: int = 0):
    _nur_im_haus(request)
    pfad = _fotopfad(fid, klein=not gross)
    if not V.execute("SELECT 1 FROM fotos WHERE id=? AND sache_id IS NOT NULL", (fid,)).fetchone() \
            or not os.path.exists(pfad):
        raise HTTPException(404, "Foto gibt es nicht")
    return FileResponse(pfad, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})


_ERSTE_ZAHL = re.compile(r"\d+")


def _leer(menge: str | None) -> bool:
    """Aufgebraucht: die erste Zahl im Mengentext ist 0 („0", „0 Stück"). Ohne Zahl („Rolle") nie leer.

    Daniel, 08.10.2026, nachdem ein RAM-Riegel mit „0 ×" in K-06 stand: „das macht kaum Sinn". Eine Sache mit
    Menge 0 liegt nicht mehr in der Kiste -- sie gehört auf die Nachkaufliste oder ganz weg. Deshalb zählt sie
    in keiner Kiste mit, und die Küche darf sie ohne Anmeldung entfernen (nur sie, siehe kachel_entfernen).
    """
    m = _ERSTE_ZAHL.search(menge or "")
    return bool(m) and int(m.group()) == 0


def _anzahl_je_kiste() -> dict[int, int]:
    """Sachen je Kiste -- OHNE die aufgebrauchten, die liegen nicht mehr drin."""
    zahlen: dict[int, int] = {}
    for z in V.execute("SELECT kiste_id, menge FROM sachen").fetchall():
        if not _leer(z["menge"]):
            zahlen[z["kiste_id"]] = zahlen.get(z["kiste_id"], 0) + 1
    return zahlen


def _leere_sachen() -> list:
    """Die Nachkaufliste: aufgebraucht, zuletzt geändert zuerst."""
    return [s for s in V.execute("SELECT * FROM sachen WHERE menge LIKE '%0%' ORDER BY geaendert_am DESC, id DESC").fetchall()
            if _leer(s["menge"])]


def _menge_setzen(menge: str, neu: int) -> str:
    """Wie Menge.setzen in der App: nur die erste Zahl ändern, den Rest („Stück", „ca.") stehen lassen."""
    m = _ERSTE_ZAHL.search(menge or "")
    return menge if not m else menge[:m.start()] + str(max(0, neu)) + menge[m.end():]


@app.post("/api/kachel/fundus/sachen/{sid}/entnahme")
def kachel_entnahme(sid: int, daten: dict, request: Request):
    """Entnehmen am Küchentablet (Daniel, 03.10.2026: „Entnahme von einzelnen Anzahl oder dem ganzen Stück").
    Dieselben drei Fälle wie in der App: `raus` (kommt wieder, mit Anzahl), `zurueck` (wieder in der Kiste),
    `verbraucht` (Menge um die Anzahl senken). Ohne Anmeldung -- das Tablet hat keine --, deshalb wie die
    übrigen Küchen-Endpunkte nur aus dem Haus; mehr als diese drei Handgriffe kann man hier nicht."""
    _nur_im_haus(request)
    s = _sache(sid)
    aktion = str(daten.get("aktion") or "")
    gesamt = _ERSTE_ZAHL.search(s["menge"] or "")
    gesamt = int(gesamt.group()) if gesamt else 1
    anzahl = daten.get("anzahl")
    if anzahl is not None:
        try:
            anzahl = int(anzahl)
        except (TypeError, ValueError):
            raise HTTPException(422, "anzahl muss eine Zahl sein")
        if anzahl < 1 or anzahl > gesamt:
            raise HTTPException(422, f"Zwischen 1 und {gesamt}")
    jetzt = db.jetzt_iso()
    if aktion == "raus":
        V.execute("UPDATE sachen SET raus_seit=?, raus_anzahl=?, geaendert_am=? WHERE id=?",
                  (s["raus_seit"] or jetzt, anzahl if gesamt > 1 else None, jetzt, sid))
    elif aktion == "zurueck":
        V.execute("UPDATE sachen SET raus_seit=NULL, raus_anzahl=NULL, geaendert_am=? WHERE id=?", (jetzt, sid))
    elif aktion == "verbraucht":
        if not _ERSTE_ZAHL.search(s["menge"] or ""):
            raise HTTPException(422, "Die Menge hat keine Zahl, die man senken könnte")
        V.execute("UPDATE sachen SET menge=?, geaendert_am=? WHERE id=?",
                  (_menge_setzen(s["menge"], gesamt - (anzahl or 1)), jetzt, sid))
    else:
        raise HTTPException(422, "aktion: raus, zurueck oder verbraucht")
    V.commit()
    return _sache_json(_sache(sid))


@app.post("/api/kachel/fundus/sachen/{sid}/entfernen")
def kachel_entfernen(sid: int, request: Request):
    """„Weg damit" am Küchentablet (Daniel, 08.10.2026) -- NUR für Aufgebrauchtes (Menge 0).

    Nach „Verbraucht" bis auf null fragt die Kachel: weg damit oder nachkaufen? Ohne Anmeldung darf die Küche
    deshalb genau das löschen, was ohnehin nicht mehr da ist; alles andere gibt 409 und bleibt der App mit
    Anmeldung vorbehalten. Von außen 403 wie bei jedem Küchen-Endpunkt.
    """
    _nur_im_haus(request)
    s = _sache(sid)
    if not _leer(s["menge"]):
        raise HTTPException(409, f"„{s['name']}“ ist nicht aufgebraucht ({s['menge']}) – löschen geht nur in der App.")
    return _sache_entfernen(s)


@app.post("/api/kachel/fundus/kisten/{code}/verschieben")
def kachel_verschieben(code: str, daten: dict, request: Request):
    """Kisten umstellen am Küchentablet (Daniel, 03.10.2026: „nach Entsperren oben rechts die Kiste verschieben").
    Ohne Anmeldung wie die Entnahme, nur aus dem Haus; das Schloss ist ein Schutz gegen Versehen auf dem Tablet,
    keine Zugangskontrolle. Nur Raum/Stapel/Platz -- keine neuen Räume, kein Löschen."""
    _nur_im_haus(request)
    z = _kiste(code)
    _kiste_verschieben(z, {k: daten[k] for k in ("raum", "stapel", "position") if k in daten}, raeume_anlegen=False)
    V.commit()
    return _kiste_json(_kiste(code))


@app.get("/api/app/version")
def app_version(x_token: str = Header(None)):
    angemeldet(x_token)
    # Gemeinsam mit MachIchGleich und Druckknopf (~/HausApp/server/hausdienst.py, seit 04.10.2026)
    return hausdienst.fassung(VERSIONS_DATEI, APK_DATEI)


@app.get("/api/app/apk")
def app_apk(x_token: str = Header(None)):
    angemeldet(x_token)
    return hausdienst.apk_antwort(APK_DATEI, "fundus.apk")


@app.get("/api/gesundheit")
def gesundheit():
    n = V.execute("SELECT COUNT(*) n FROM sachen").fetchone()["n"]
    # „dienst" lässt die App beim Eintragen der Adresse erkennen, dass dort wirklich ein Fundus-Server antwortet.
    return {"ok": True, "dienst": "fundus", "sachen": n, "erkennung": erkennung.eingerichtet(), "demo": erkennung.demo()}


# ================================================================ Fundus 2 (03.11.2026)
# Daniel: „Mach mal einen Entwurf für eine Überarbeitung von Fundus. Schau dir es tief an." → Entwurf mit Befund
# (claude.ai/artifact/32b7X8oq8SzV79X9FXpccw), dann: „bau das und dann setze die Entwürfe um". Hier der Server-Teil:
# Arten zählen, automatisch aufbereiten (ohne Freistellen), Aufräumliste mit „vielleicht doppelt", Zusammenlegen,
# neue Einteilung (Arten) per KI, Steckbriefe nachtragen, Bauen. Alles nur Ergänzungen -- eine ältere App läuft weiter.

import difflib, queue, threading


def _bereiche_anzahl() -> list[dict]:
    zahlen: dict[str, int] = {}
    for z in V.execute("SELECT bereich, menge FROM sachen").fetchall():
        if not _leer(z["menge"]):
            b = z["bereich"] or ""
            zahlen[b] = zahlen.get(b, 0) + 1
    return [{"name": b, "anzahl": n} for b, n in sorted(zahlen.items(), key=lambda x: (-x[1], x[0].casefold()))]


def _ki_nutzung(n: dict) -> None:
    _nutzung_merken(n, n.get("versuche") or [])


# ---------------------------------------------------------------- automatisch aufbereiten (ohne Freistellen)

_aufbereit_schlange: "queue.Queue[int]" = queue.Queue()
_aufbereit_faden: threading.Thread | None = None
_aufbereit_sperre = threading.Lock()
AUTO_EINSTELLUNGEN = {"zuschneiden": True, "verbessern": True, "freistellen": False}


def nach_dem_anlegen_aufbereiten(sid: int) -> None:
    global _aufbereit_faden
    if not aufbereiten.ISNET.vorhanden():
        return
    _aufbereit_schlange.put(sid)
    with _aufbereit_sperre:
        if _aufbereit_faden is None or not _aufbereit_faden.is_alive():
            _aufbereit_faden = threading.Thread(target=_aufbereit_arbeiter, name="aufbereiten", daemon=True)
            _aufbereit_faden.start()


def _aufbereit_arbeiter() -> None:
    while True:
        try:
            sid = _aufbereit_schlange.get(timeout=60)
        except queue.Empty:
            return
        try:
            s = V.execute("SELECT * FROM sachen WHERE id=?", (sid,)).fetchone()
            liste = _fotoliste(s) if s else []
            if liste and not V.execute("SELECT 1 FROM aufbereitung WHERE original=? OR neu=?", (liste[0], liste[0])).fetchone():
                v = _vorschlag_rechnen(liste[0], dict(AUTO_EINSTELLUNGEN))
                _vorschlag_uebernehmen(v["id"], sid)
        except Exception as f:          # ein Bild, das nicht klappt, darf die Schlange nicht anhalten
            print(f"aufbereiten {sid}: {type(f).__name__}: {f}", flush=True)
        finally:
            _aufbereit_schlange.task_done()


# ---------------------------------------------------------------- Aufräumliste

# Ohne Leerzeichen zwischen Buchstaben und Ziffern: „Klemme 221-413" ist keine Typbezeichnung „KLEMME221" (fasste sonst
# 221-412 und 221-413 als gleich zusammen); „HC-SR501", „FS1000A", „DS18B20" schon.
_TYP = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{2,6}-?\d{2,5}[A-Za-z]{0,3}\d{0,3})(?![A-Za-z0-9])")
# Familien statt Typen: zwei Sachen mit „ESP32" sind selten dieselbe (Dev Kit, Audio-Kit, LoRa-Tracker)
_FAMILIEN = {"ESP32", "ESP8266", "ESP01", "STM32", "RP2040", "ATMEGA328", "USB2", "USB3", "CAT5", "CAT6", "IP65",
             "IP67", "IP44", "IP20", "IP30"}


def _typen(name: str) -> set[str]:
    return {t.upper().replace("-", "").replace(" ", "") for t in _TYP.findall(name or "")} - _FAMILIEN


def vielleicht_doppelt(sachen: list[dict], verworfen: dict[str, str]) -> list[dict]:
    """Gruppen, die wohl dieselbe Sache mehrfach erfasst sind (Befund 03.11.: dreimal FS1000A in K-06).
    Gleich heißt: gleicher Name (normalisiert) -- oder gleiche Typbezeichnung UND (gleiche Kiste oder sehr
    ähnlicher Name). Aufgebrauchte zählen nicht mit. `verworfen`: schluessel → ids aus „Sind verschieden"."""
    lebend = [s for s in sachen if not _leer(s["menge"])]
    gruppen: dict[str, dict] = {}
    for s in lebend:
        for t in _typen(s["name"]):
            gruppen.setdefault("typ:" + t, {"grund": t, "ids": []})["ids"].append(s["id"])
        n = suche.normalisieren(s["name"])
        if n:
            gruppen.setdefault("name:" + n, {"grund": s["name"], "ids": []})["ids"].append(s["id"])
    nach_id = {s["id"]: s for s in lebend}
    fertig, belegt = [], set()
    for schluessel, g in sorted(gruppen.items(), key=lambda kv: (kv[0].startswith("typ:"), kv[0])):
        ids = sorted(set(g["ids"]))
        if len(ids) < 2:
            continue
        if schluessel.startswith("typ:"):
            # nur, wer mit einem anderen in derselben Kiste liegt oder fast gleich heißt
            def nah(a, b):
                return a["kiste_id"] == b["kiste_id"] or difflib.SequenceMatcher(
                    None, suche.normalisieren(a["name"]), suche.normalisieren(b["name"])).ratio() >= 0.8
            ids = [i for i in ids if any(nah(nach_id[i], nach_id[j]) for j in ids if j != i)]
            if len(ids) < 2:
                continue
        if tuple(ids) in belegt or verworfen.get(schluessel) == ",".join(map(str, ids)):
            continue
        belegt.add(tuple(ids))
        fertig.append({"schluessel": schluessel, "grund": g["grund"], "ids": ids})
    return fertig


@app.get("/api/aufraeumen")
def aufraeumen(x_token: str = Header(None)):
    """Was gerade unordentlich ist: vielleicht doppelt, Kisten ohne Platz oder ohne Art, draußen, aufgebraucht,
    und ob eine neue Einteilung sich lohnt (ein Bereich mit mehr als der Hälfte der Sachen)."""
    angemeldet(x_token)
    kisten = {z["id"]: z for z in V.execute("SELECT * FROM kisten").fetchall()}
    zeilen = V.execute("SELECT * FROM sachen").fetchall()
    verworfen = {z["schluessel"]: z["ids"] for z in V.execute("SELECT * FROM nicht_doppelt").fetchall()}
    nach_id = {z["id"]: z for z in zeilen}
    doppelt = [{"schluessel": g["schluessel"], "grund": g["grund"],
                "sachen": [_sache_json(nach_id[i], kisten.get(nach_id[i]["kiste_id"])) for i in g["ids"]]}
               for g in vielleicht_doppelt([dict(z) for z in zeilen], verworfen)]
    anzahl = _anzahl_je_kiste()
    ohne_platz = [_kiste_json(k, anzahl.get(k["id"], 0)) for k in sorted(kisten.values(), key=lambda k: k["code"])
                  if not k["stapel"] and not k["regal_id"]]
    ohne_art = [k["code"] for k in sorted(kisten.values(), key=lambda k: k["code"]) if not (k["art"] or "").strip()]
    raus = [_sache_json(s, kisten.get(s["kiste_id"])) for s in zeilen if s["raus_seit"]]
    leer = [_sache_json(s, kisten.get(s["kiste_id"])) for s in _leere_sachen()]
    ba = _bereiche_anzahl()
    gesamt = sum(b["anzahl"] for b in ba) or 1
    groesster = ba[0] if ba else {"name": "", "anzahl": 0}
    ohne_merkmale = V.execute("SELECT COUNT(*) n FROM sachen WHERE merkmale IS NULL").fetchone()["n"]
    return {"doppelt": doppelt, "ohne_platz": ohne_platz, "ohne_art": ohne_art, "raus": raus, "leer": leer,
            "arten": {"groesster": groesster["name"], "anteil": round(groesster["anzahl"] / gesamt, 2),
                      "lohnt": groesster["anzahl"] / gesamt > 0.5 and gesamt >= 20},
            "ohne_merkmale": ohne_merkmale, "merkmale_laeuft": _merkmale_stand["laeuft"]}


@app.post("/api/doppelt/verschieden")
def doppelt_verschieden(daten: dict, x_token: str = Header(None)):
    angemeldet(x_token)
    schluessel = str(daten.get("schluessel") or "").strip()[:200]
    ids = sorted({int(i) for i in (daten.get("ids") or [])})
    if not schluessel or len(ids) < 2:
        raise HTTPException(422, "Es fehlen Gruppe oder Sachen")
    V.execute("INSERT INTO nicht_doppelt (schluessel, ids, am) VALUES (?,?,?) ON CONFLICT(schluessel) DO UPDATE SET "
              "ids=excluded.ids, am=excluded.am", (schluessel, ",".join(map(str, ids)), db.jetzt_iso()))
    V.commit()
    return {"ok": True}


def menge_zusammen(mengen: list[str]) -> str:
    """„3" + „ca. 3" + „1 Stück" → „7 Stück": die ersten Zahlen addieren, die Einheit der ersten mit Einheit.
    Ohne Zahl zählt eine Sache als 1."""
    summe, einheit = 0, ""
    for m in mengen:
        z = _ERSTE_ZAHL.search(m or "")
        summe += int(z.group()) if z else 1
        rest = re.sub(r"(?i)\bca\.?\s*", "", (m or "")[z.end():] if z else (m or "")).strip()
        if not einheit and rest:
            einheit = rest
    return f"{summe} {einheit}".strip()


@app.post("/api/sachen/zusammenlegen")
def zusammenlegen(daten: dict, x_token: str = Header(None)):
    """{ziel, quellen: [...]} → aus mehreren Einträgen wird einer: Mengen addiert, Fotos (bis 8), Suchwörter und
    Steckbrief vereinigt, Notizen angehängt. Stücklisten zeigen danach auf das Ziel. Die Quellen sind weg."""
    angemeldet(x_token)
    try:
        ziel = int(daten.get("ziel"))
        quellen = [int(q) for q in (daten.get("quellen") or []) if int(q) != ziel]
    except (TypeError, ValueError):
        raise HTTPException(422, "ziel und quellen müssen Zahlen sein")
    if not quellen:
        raise HTTPException(422, "Es fehlen Gruppe oder Sachen")
    z = _sache(ziel)
    q = [_sache(i) for i in quellen]
    alle = [z] + q
    fotos = list(dict.fromkeys(f for s in alle for f in _fotoliste(s)))[:MAX_FOTOS]
    woerter = sorted({w for s in alle for w in json.loads(s["woerter"] or "[]")})[:30]
    merkmale = erkennung.merkmale_glaetten([m for s in alle for m in _merkmale_lesen(s)])
    notiz = "\n".join(dict.fromkeys(s["notiz"].strip() for s in alle if (s["notiz"] or "").strip()))[:1000]
    beschreibung = max((s["beschreibung"] or "" for s in alle), key=len)
    V.execute("UPDATE sachen SET menge=?, woerter=?, merkmale=?, notiz=?, beschreibung=?, link=CASE WHEN link='' THEN ? ELSE link END, "
              "geaendert_am=? WHERE id=?",
              (menge_zusammen([s["menge"] for s in alle])[:40], json.dumps(woerter, ensure_ascii=False),
               json.dumps(merkmale, ensure_ascii=False), notiz, beschreibung[:500],
               next((s["link"] for s in q if s["link"]), ""), db.jetzt_iso(), ziel))
    for s in q:
        V.execute("UPDATE projekt_teile SET sache_id=? WHERE sache_id=?", (ziel, s["id"]))
        V.execute("UPDATE aufbereitung SET sache_id=? WHERE sache_id=?", (ziel, s["id"]))
        V.execute("UPDATE fotos SET sache_id=NULL WHERE sache_id=?", (s["id"],))
        V.execute("DELETE FROM sachen WHERE id=?", (s["id"],))
    _fotos_setzen(ziel, fotos)
    V.commit()
    return _sache_json(_sache(ziel))


# ---------------------------------------------------------------- neue Einteilung (Arten)

ARTEN_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["arten"], "properties": {
    "arten": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["name", "ids"],
                                         "properties": {"name": {"type": "string"},
                                                        "ids": {"type": "array", "items": {"type": "integer"}}}}}}}


def _sicherung(anlass: str) -> str:
    """Kopie der Datenbank vor einer Massenänderung, nach data/sicherungen (sqlite-Backup, auch mit WAL richtig)."""
    import sqlite3
    ordner = os.path.join(db.DATEN, "sicherungen")
    os.makedirs(ordner, exist_ok=True)
    ziel = os.path.join(ordner, f"fundus-vor-{anlass}-{datetime.now():%Y%m%d-%H%M%S}.sqlite3")
    with sqlite3.connect(ziel) as kopie:
        V.v.backup(kopie)
    return os.path.basename(ziel)


@app.post("/api/arten/vorschlag")
def arten_vorschlag(request: Request, x_token: str = Header(None)):
    """Die KI ordnet alle Sachen in 8–14 Arten (Daniel bestätigt in der App, nichts wird hier geändert)."""
    angemeldet(x_token)
    zeilen = V.execute("SELECT id, name, bereich FROM sachen ORDER BY id").fetchall()
    if not zeilen:
        raise HTTPException(422, "Es gibt noch keine Sachen")
    en = _en(request)
    system = ("You sort the items of a private home workshop into 8 to 14 clear categories (in English). Many items are "
              "electronics hobby parts -- split those finely (e.g. Microcontrollers, Sensors, Displays, Radio & network, "
              "Power & batteries, Switches & relays, LEDs & lighting, Cables & connectors); everything else coarser "
              "(Tools, Fixings, Household & garden, …). Every id exactly once.") if en else (
              "Du teilst die Sachen einer privaten Heimwerker-Werkstatt in 8 bis 14 klare Arten ein (Deutsch, kurz, "
              "z. B. „Funk & Netz“). Viele Sachen sind Elektronik-Bastelteile -- die fein aufteilen (z. B. "
              "Mikrocontroller, Sensoren, Anzeigen, Funk & Netz, Strom & Akkus, Schalter & Relais, LEDs & Licht, "
              "Kabel & Stecker); alles andere gröber (Werkzeug, Befestigung, Haushalt & Garten, …). Jede id genau einmal.")
    text = "\n".join(f"{z['id']}|{z['name']}|{z['bereich']}" for z in zeilen)
    try:
        antwort, n = erkennung.frage_json(system, "id|name|bisheriger Bereich\n" + text, ARTEN_SCHEMA, "arten")
    except erkennung.ErkennungsFehler as f:
        _nutzung_merken(None, f.versuche, fehler_modell=erkennung.modell() if erkennung.schluessel() else "")
        raise HTTPException(f.status, f.meldung)
    _ki_nutzung(n)
    alle = {z["id"]: z for z in zeilen}
    gesehen: set[int] = set()
    arten = []
    for a in antwort.get("arten") or []:
        name = bereich_glaetten(str(a.get("name") or ""))[:40]
        ids = [int(i) for i in (a.get("ids") or []) if isinstance(i, (int, float, str)) and str(i).isdigit()
               and int(i) in alle and int(i) not in gesehen]
        gesehen.update(ids)
        if name and ids:
            arten.append({"name": name, "ids": ids, "beispiele": [alle[i]["name"] for i in ids[:3]]})
    vergessen = [i for i in alle if i not in gesehen]
    if vergessen:   # was die KI ausließ, behält seinen Bereich
        for i in vergessen:
            b = alle[i]["bereich"] or "Sonstiges"
            ziel = next((a for a in arten if a["name"] == b), None)
            if ziel is None:
                ziel = {"name": b, "ids": [], "beispiele": []}
                arten.append(ziel)
            ziel["ids"].append(i)
    arten.sort(key=lambda a: -len(a["ids"]))
    return {"arten": arten, "modell": n["modell"]}


@app.post("/api/arten/uebernehmen")
def arten_uebernehmen(daten: dict, x_token: str = Header(None)):
    """{arten: [{name, ids}]} → jeder Sache ihren neuen Bereich. Vorher eine Kopie der Datenbank."""
    angemeldet(x_token)
    arten = daten.get("arten") or []
    zuordnung: dict[int, str] = {}
    for a in arten:
        name = " ".join(str(a.get("name") or "").split())[:40]
        if not name:
            raise HTTPException(422, "Eine Art braucht einen Namen")
        for i in a.get("ids") or []:
            zuordnung[int(i)] = name[:1].upper() + name[1:]
    if not zuordnung:
        raise HTTPException(422, "Es fehlen Gruppe oder Sachen")
    sicherung = _sicherung("arten")
    geaendert = 0
    for i, name in zuordnung.items():
        c = V.execute("UPDATE sachen SET bereich=?, geaendert_am=? WHERE id=? AND bereich<>?", (name, db.jetzt_iso(), i, name))
        geaendert += c.rowcount
    V.commit()
    return {"geaendert": geaendert, "sicherung": sicherung, "bereiche_anzahl": _bereiche_anzahl()}


# ---------------------------------------------------------------- Steckbriefe nachtragen

MERKMALE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["sachen"], "properties": {
    "sachen": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["id", "merkmale"],
                                          "properties": {"id": {"type": "integer"},
                                                         "merkmale": erkennung.SCHEMA["properties"]["merkmale"]}}}}}
_merkmale_stand = {"laeuft": False, "fertig": 0, "fehler": ""}
_merkmale_sperre = threading.Lock()


def merkmale_nachtragen(paket: int = 20, en: bool = False) -> None:
    """Für alle Sachen ohne Steckbrief (merkmale NULL) in Paketen aus Name und Beschreibung -- ohne Foto, ohne
    Erfinden. Ein Paket, das scheitert, beendet den Lauf (die übrigen bleiben NULL und kommen beim nächsten dran)."""
    system = ("Für jede Sache: 0 bis 6 technische Eckdaten als name/wert, NUR aus Name und Beschreibung ablesbar "
              "(Typ, Frequenz, Spannung, Strom, Leistung, Schnittstelle, Länge, Gewinde, Körnung, Inhalt …). "
              "Nichts erfinden -- im Zweifel weglassen. Werte knapp (z. B. „433 MHz“).")
    try:
        while True:
            zeilen = V.execute("SELECT id, name, beschreibung FROM sachen WHERE merkmale IS NULL ORDER BY id LIMIT ?",
                               (paket,)).fetchall()
            if not zeilen:
                return
            text = "\n".join(f"{z['id']}|{z['name']}|{z['beschreibung']}" for z in zeilen)
            antwort, n = erkennung.frage_json(system, "id|name|beschreibung\n" + text, MERKMALE_SCHEMA, "merkmale")
            _ki_nutzung(n)
            gekommen = {}
            for e in antwort.get("sachen") or []:
                try:
                    gekommen[int(e.get("id"))] = erkennung.merkmale_glaetten(e.get("merkmale"))
                except (TypeError, ValueError):
                    pass
            for z in zeilen:   # auch ohne Antwort „gefragt" setzen, sonst kreist der Lauf um dieselbe Sache
                V.execute("UPDATE sachen SET merkmale=? WHERE id=? AND merkmale IS NULL",
                          (json.dumps(gekommen.get(z["id"], []), ensure_ascii=False), z["id"]))
            V.commit()
            _merkmale_stand["fertig"] += len(zeilen)
    except erkennung.ErkennungsFehler as f:
        _nutzung_merken(None, f.versuche, fehler_modell=erkennung.modell() if erkennung.schluessel() else "")
        _merkmale_stand["fehler"] = f.meldung
    except Exception as f:
        _merkmale_stand["fehler"] = f"{type(f).__name__}: {f}"[:200]
    finally:
        _merkmale_stand["laeuft"] = False


@app.post("/api/merkmale/nachtragen")
def merkmale_nachtragen_starten(x_token: str = Header(None)):
    angemeldet(x_token)
    with _merkmale_sperre:
        if not _merkmale_stand["laeuft"]:
            _merkmale_stand.update(laeuft=True, fertig=0, fehler="")
            threading.Thread(target=merkmale_nachtragen, name="merkmale", daemon=True).start()
    return merkmale_stand_lesen(x_token)


@app.get("/api/merkmale/stand")
def merkmale_stand_lesen(x_token: str = Header(None)):
    angemeldet(x_token)
    return {**_merkmale_stand, "offen": V.execute("SELECT COUNT(*) n FROM sachen WHERE merkmale IS NULL").fetchone()["n"]}


# ---------------------------------------------------------------- Bauen

BAUEN_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["titel", "teile", "fehlt", "hinweis"],
                "properties": {
                    "titel": {"type": "string"},
                    "teile": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                         "required": ["id", "menge", "wofuer"],
                                                         "properties": {"id": {"type": "integer"}, "menge": {"type": "string"},
                                                                        "wofuer": {"type": "string"}}}},
                    "fehlt": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                         "required": ["name", "menge", "wofuer"],
                                                         "properties": {"name": {"type": "string"}, "menge": {"type": "string"},
                                                                        "wofuer": {"type": "string"}}}},
                    "hinweis": {"type": "string"}}}


def _bauen_demo(idee: str, zeilen: list, en: bool) -> tuple[dict, dict]:
    """Demo-Server (Googles Prüfer, 04.11.2026): keine KI, sondern eine Beispiel-Stückliste aus dem Bestand -- Sachen,
    deren Name, Beschreibung oder Suchwörter einen Wortanfang der Idee enthalten, sonst drei aus der Elektrik. So ist der Ablauf bis „Als Projekt merken" zu sehen."""
    staemme = {w[:5] for w in re.findall(r"\w{3,}", idee.lower())}
    def text(z):
        return " ".join([z["name"], z["beschreibung"] or "", z["woerter"] or ""]).lower()
    passend = [z for z in zeilen if any(st in text(z) for st in staemme)][:4] or \
        sorted(zeilen, key=lambda z: "elektr" not in (z["bereich"] or "").lower() and "electr" not in (z["bereich"] or "").lower())[:3]
    antwort = {"titel": idee[:60] + (" (demo)" if en else " (Demo)"),
               "teile": [{"id": z["id"], "menge": "1", "wofuer": ""} for z in passend],
               "fehlt": [{"name": "Jumper wires" if en else "Steckbrücken", "menge": "10", "wofuer": ""}],
               "hinweis": ("Demo server: a sample parts list from the inventory. With your own server and AI key the "
                           "list fits your idea.") if en else ("Demo-Server: eine Beispiel-Stückliste aus dem Bestand. "
                           "Mit eigenem Server und KI-Schlüssel passt die Liste zu deiner Idee.")}
    return antwort, {"modell": "demo", "tokens_ein": 0, "tokens_aus": 0, "versuche": [], "rueckfall": ""}


@app.post("/api/bauen")
def bauen(daten: dict, request: Request, x_token: str = Header(None)):
    """{idee} → Stückliste aus dem Bestand + was fehlt. Nur ein Vorschlag; gemerkt wird erst mit /api/bauen/merken."""
    angemeldet(x_token)
    idee = " ".join(str(daten.get("idee") or "").split())[:500]
    if len(idee) < 3:
        raise HTTPException(422, "Beschreib kurz, was du bauen willst")
    kisten = {z["id"]: z for z in V.execute("SELECT * FROM kisten").fetchall()}
    zeilen = [z for z in V.execute("SELECT * FROM sachen ORDER BY id").fetchall() if not _leer(z["menge"])]
    def zeile(z):
        mm = "; ".join(f"{m['name']}: {m['wert']}" for m in _merkmale_lesen(z))
        return f"{z['id']}|{z['name']}|{z['menge'] or '?'}|{z['bereich']}|{mm}"
    en = _en(request)
    system = ("You help to plan a hobby project from the parts someone already has in their home workshop. Pick only "
              "parts from the list (by id) that are really needed, with the quantity needed. List what is missing "
              "separately (common parts only, no brand). titel: a short project name. hinweis: one sentence, "
              "e.g. a caveat. Answer in English.") if en else (
              "Du hilfst, ein Bastelprojekt aus dem zu planen, was jemand schon in seiner Werkstatt hat. Nimm aus der "
              "Liste (per id) nur Teile, die wirklich gebraucht werden, mit der nötigen Menge. Was fehlt, extra "
              "(gängige Teile, keine Marke). titel: kurzer Projektname. hinweis: ein Satz, z. B. eine Einschränkung. "
              "Antworte auf Deutsch.")
    text = f"Projekt: {idee}\n\nBestand (id|name|menge|art|eckdaten):\n" + "\n".join(zeile(z) for z in zeilen)
    try:
        if erkennung.demo():
            antwort, n = _bauen_demo(idee, zeilen, en)
        else:
            antwort, n = erkennung.frage_json(system, text, BAUEN_SCHEMA, "bauen")
    except erkennung.ErkennungsFehler as f:
        _nutzung_merken(None, f.versuche, fehler_modell=erkennung.modell() if erkennung.schluessel() else "")
        raise HTTPException(f.status, f.meldung)
    _ki_nutzung(n)
    nach_id = {z["id"]: z for z in zeilen}
    teile, gesehen = [], set()
    for t in antwort.get("teile") or []:
        try:
            i = int(t.get("id"))
        except (TypeError, ValueError):
            continue
        if i in nach_id and i not in gesehen:
            gesehen.add(i)
            teile.append({"sache": _sache_json(nach_id[i], kisten.get(nach_id[i]["kiste_id"])),
                          "menge": str(t.get("menge") or "")[:40], "wofuer": str(t.get("wofuer") or "")[:200]})
    fehlt = [{"name": str(f.get("name") or "")[:120], "menge": str(f.get("menge") or "")[:40],
              "wofuer": str(f.get("wofuer") or "")[:200]} for f in antwort.get("fehlt") or [] if str(f.get("name") or "").strip()]
    return {"titel": str(antwort.get("titel") or idee)[:120], "idee": idee, "teile": teile, "fehlt": fehlt[:20],
            "hinweis": str(antwort.get("hinweis") or "")[:300], "modell": n["modell"]}


@app.post("/api/bauen/merken")
def bauen_merken(daten: dict, x_token: str = Header(None)):
    """{name, notiz, teile: [{sache_id, menge, notiz}], fehlt: [{text, menge, notiz}]} → ein Projekt mit Stückliste."""
    angemeldet(x_token)
    f = _projekt_felder(daten, pflicht=True)
    jetzt_ = db.jetzt_iso()
    c = V.execute("INSERT INTO projekte (name, notiz, angelegt_am, geaendert_am) VALUES (?,?,?,?)",
                  (f["name"], f.get("notiz", ""), jetzt_, jetzt_))
    pid = c.lastrowid
    nr = 0
    for t in (daten.get("teile") or []) + (daten.get("fehlt") or []):
        tf = _teil_felder({k: v for k, v in t.items() if k in ("sache_id", "text", "menge", "notiz", "link")})
        if not tf.get("sache_id") and not tf.get("text"):
            continue
        nr += 1
        V.execute("INSERT INTO projekt_teile (projekt_id, sache_id, text, menge, notiz, link, erledigt, nr) VALUES (?,?,?,?,?,?,0,?)",
                  (pid, tf.get("sache_id"), tf.get("text", ""), tf.get("menge", ""), tf.get("notiz", ""), tf.get("link", ""), nr))
    V.commit()
    return _projekt_json(_projekt(pid))
