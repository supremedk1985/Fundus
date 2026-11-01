from suche import bewerten, normalisieren, passt, suchbegriffe, suchen


def s(name, woerter=(), bereich="", beschreibung=""):
    import json
    return {"name": name, "woerter": json.dumps(list(woerter)), "bereich": bereich, "beschreibung": beschreibung}


def test_umlaute_und_zeichen():
    assert normalisieren("Heißklebe-Pistole (groß)") == "heissklebe pistole gross"
    assert normalisieren("Düse") == "duese"


def test_fuellwoerter_fallen_weg():
    assert suchbegriffe("das Ding zum Abisolieren") == ["abisolieren"]
    # Besteht die Anfrage NUR aus Füllwörtern, wird trotzdem gesucht.
    assert suchbegriffe("das ding") == ["das", "ding"]


def test_passt_stufen():
    assert passt("wago", "wago") == 1.0
    assert passt("due", "duesenaufsatz") == 0.9
    assert passt("kleb", "heissklebepistole") == 0.8
    assert passt("heisskleber", "heissklebesticks") == 0.7
    assert passt("abisoliern", "abisolieren") > 0
    assert passt("schraube", "schrumpfschlauch") == 0.0


def test_verwendungszweck_findet_werkzeug():
    zange = s("Abisolierzange Knipex", woerter=["abisolieren", "kabel", "zange"], bereich="Werkzeug")
    klemme = s("Wago-Klemme 221", woerter=["klemme", "kabel verbinden"], bereich="Elektro")
    t = suchen("Ding zum Abisolieren", [klemme, zange])
    assert [x[1]["name"] for x in t] == ["Abisolierzange Knipex"]


def test_alle_begriffe_vor_einem():
    a = s("Schrauben M5", woerter=["schraube"])
    b = s("Senkkopfschraube M5 x 20 Torx", woerter=["schraube", "torx"])
    t = suchen("schraube torx", [a, b])
    assert t[0][1]["name"].startswith("Senkkopf")


def test_name_wiegt_mehr_als_beschreibung():
    a = s("Kabelbinder schwarz", beschreibung="200 mm")
    b = s("Werkzeugtasche", beschreibung="mit Kabelbinder-Fach")
    t = suchen("kabelbinder", [b, a])
    assert t[0][1]["name"] == "Kabelbinder schwarz"
    assert bewerten(["kabelbinder"], b) < bewerten(["kabelbinder"], a)


def test_kaputte_woerter_spalte_bricht_nichts():
    x = {"name": "Säge", "woerter": "{kaputt", "bereich": "", "beschreibung": ""}
    assert suchen("säge", [x])[0][1]["name"] == "Säge"
