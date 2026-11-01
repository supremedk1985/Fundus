"""Gemeinsame Server-Teile der Haus-Apps (MachIchGleich, Fundus, Druckknopf): das Selbstupdate.

QUELLE: ~/HausApp/server/hausdienst.py (Gitea daniel/hausapp). Die Dienste tragen KOPIEN davon, weil jeder sein
eigenes Docker-Abbild baut und ein Abbild nichts außerhalb seines Ordners sehen kann. Nicht in den Kopien ändern,
sondern hier -- dann `~/HausApp/server/verteilen.sh` (kopiert und sagt, welche Dienste neu gebaut werden müssen).

Der Versionszettel <name>-version.json entsteht beim Ausliefern einer App (Gradle-Plugin der Haus-Apps): versionCode,
versionName und `neu` (der neueste Eintrag des Änderungsblatts). Der Server ergänzt, was nur er genau weiß: ob das APK
da ist, wie groß es ist und wann es entstand (Änderungszeit der Datei -- kann nicht auseinanderlaufen).
"""
import json
import os
from datetime import datetime, timezone


def fassung(zettel_pfad: str, apk_pfad: str) -> dict:
    """Was auf dem Server liegt. Ein fehlender oder kaputter Zettel heißt ehrlich „nichts vorhanden", kein 500."""
    try:
        with open(zettel_pfad, encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            raise ValueError("kein Objekt")
    except (OSError, ValueError):
        return {"vorhanden": False}
    d["vorhanden"] = os.path.exists(apk_pfad)
    if d["vorhanden"]:
        d["groesse"] = os.path.getsize(apk_pfad)
        d["gebaut"] = datetime.fromtimestamp(os.path.getmtime(apk_pfad), timezone.utc).astimezone().isoformat(timespec="minutes")
    else:
        d["groesse"] = 0
        d["gebaut"] = ""
    return d


def apk_antwort(apk_pfad: str, dateiname: str):
    """Das APK selbst. `Content-Encoding: identity`, damit keine Kompressions-Schicht die Länge verfälscht --
    die App prüft den Fortschritt gegen Content-Length."""
    from fastapi import HTTPException
    from fastapi.responses import FileResponse
    if not os.path.exists(apk_pfad):
        raise HTTPException(404, "Auf dem Server liegt gerade keine Fassung")
    return FileResponse(apk_pfad, media_type="application/vnd.android.package-archive", filename=dateiname,
                        headers={"Content-Encoding": "identity"})
