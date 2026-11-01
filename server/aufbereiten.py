"""Bilder optimieren (Daniel, 02.10.2026): zuschneiden, verbessern, freistellen -- immer als Vorschlag.

Freistellen mit ISNet (isnet-general-use, dasselbe Modell wie in rembg), lokal auf dem Server über
onnxruntime. Keine Cloud, keine Kosten -- so meinte Daniel „ohne KI". GrabCut war die erste Wahl
und ließ an jedem Probebild Fetzen vom Untergrund stehen.

Speicher: Das Modell braucht beim Rechnen bis ~1,8 GiB. Darum
  * rechnet immer nur EIN Bild zur Zeit (Sperre),
  * wird es erst beim ersten Bedarf geladen und nach RUHE_SEKUNDEN ohne Arbeit wieder freigegeben,
  * wird die Maske je Foto als PNG zwischengespeichert -- Pinsel, Rahmen und Regler rechnen danach
    ohne das Modell, in Sekundenbruchteilen.
Am 02.10.2026 hat eine Probe mit einem größeren Modell (BiRefNet) ohne Speichergrenze den Heimserver lahmgelegt.
"""
from __future__ import annotations
import os, threading, time

import cv2
import numpy as np

MODELL = os.environ.get("ISNET_MODELL", "/modelle/isnet-general-use.onnx")
RUHE_SEKUNDEN = 300
FAEDEN = 3          # von 6 Kernen; der Rest bleibt für die übrigen Dienste
RAND = 0.12         # Anteil Rand um den Gegenstand beim Zuschnitt
SCHWELLE = 140      # Maske ab diesem Wert = Gegenstand (nimmt den grauen Schleier weg)
KLEINTEILE = 0.08   # Maskenteile unter 8 % der größten Fläche fallen weg
AUSGABE_MIN, AUSGABE_MAX = 800, 1600


class _Modell:
    def __init__(self) -> None:
        self._sperre = threading.Lock()
        self._sitzung = None
        self._zuletzt = 0.0
        self._wecker: threading.Timer | None = None

    def vorhanden(self) -> bool:
        return os.path.exists(MODELL)

    def maske(self, rgb: np.ndarray) -> np.ndarray:
        """Wahrscheinlichkeit Gegenstand 0..255 in der Größe des Bildes."""
        with self._sperre:
            if self._sitzung is None:
                import onnxruntime as ort
                o = ort.SessionOptions()
                o.intra_op_num_threads = FAEDEN
                o.inter_op_num_threads = 1
                o.enable_cpu_mem_arena = False
                self._sitzung = ort.InferenceSession(MODELL, o, providers=["CPUExecutionProvider"])
            h, w = rgb.shape[:2]
            klein = cv2.resize(rgb, (1024, 1024), interpolation=cv2.INTER_AREA).astype(np.float32)
            klein /= max(float(klein.max()), 1e-6)
            klein -= 0.5
            eingabe = klein.transpose(2, 0, 1)[None]
            name = self._sitzung.get_inputs()[0].name
            p = self._sitzung.run(None, {name: eingabe})[0][0, 0]
            p = (p - p.min()) / max(float(p.max() - p.min()), 1e-6)
            self._zuletzt = time.time()
            self._wecker_stellen()
            return cv2.resize((p * 255).astype(np.uint8), (w, h), interpolation=cv2.INTER_LINEAR)

    def _wecker_stellen(self) -> None:
        if self._wecker:
            self._wecker.cancel()
        self._wecker = threading.Timer(RUHE_SEKUNDEN + 1, self._freigeben)
        self._wecker.daemon = True
        self._wecker.start()

    def _freigeben(self) -> None:
        with self._sperre:
            if self._sitzung is not None and time.time() - self._zuletzt >= RUHE_SEKUNDEN:
                self._sitzung = None
                # Ohne das behält glibc die freigegebenen ~250 MB für den Prozess (gemessen 02.10.2026: 503 → 396 MB).
                import gc, ctypes
                gc.collect()
                try:
                    ctypes.CDLL("libc.so.6").malloc_trim(0)
                except OSError:
                    pass

    def geladen(self) -> bool:
        return self._sitzung is not None


ISNET = _Modell()


# ---------------------------------------------------------------- Maske

def maske_holen(bildpfad: str, maskenpfad: str) -> np.ndarray:
    """Rohmaske (0..255) des Fotos. Gilt nur, solange sie jünger ist als das Foto
    (ein gedrehtes Foto braucht eine neue)."""
    if os.path.exists(maskenpfad) and os.path.getmtime(maskenpfad) >= os.path.getmtime(bildpfad):
        m = cv2.imread(maskenpfad, cv2.IMREAD_GRAYSCALE)
        if m is not None:
            return m
    rgb = _lesen(bildpfad)
    m = ISNET.maske(rgb)
    os.makedirs(os.path.dirname(maskenpfad), exist_ok=True)
    cv2.imwrite(maskenpfad, m)
    return m


def maske_glaetten(roh: np.ndarray) -> np.ndarray:
    """Schwelle + nur die großen Teile behalten. Löcher bleiben Löcher (Griffloch der Waage)."""
    h = (roh > SCHWELLE).astype(np.uint8)
    n, lab, st, _ = cv2.connectedComponentsWithStats(h, 8)
    if n > 2:
        groesste = st[1:, cv2.CC_STAT_AREA].max()
        behalten = [i for i in range(1, n) if st[i, cv2.CC_STAT_AREA] >= KLEINTEILE * groesste]
        h = np.isin(lab, behalten).astype(np.uint8)
    return h * 255


def pinsel_anwenden(maske: np.ndarray, pinsel: list) -> np.ndarray:
    """Pinselstriche in Bildanteilen (0..1). „dazu" malt Gegenstand, „weg" malt Untergrund.
    `breite` ist ein Anteil der längeren Bildkante."""
    h, w = maske.shape[:2]
    m = maske.copy()
    for strich in pinsel or []:
        wert = 255 if strich.get("art") == "dazu" else 0
        dicke = max(2, int(float(strich.get("breite") or 0.04) * max(w, h)))
        pkt = [(int(float(x) * w), int(float(y) * h)) for x, y in (strich.get("punkte") or [])][:2000]
        if len(pkt) == 1:
            cv2.circle(m, pkt[0], dicke // 2, wert, -1)
        for a, b in zip(pkt, pkt[1:]):
            cv2.line(m, a, b, wert, dicke)
    return m


def rahmen_aus_maske(maske: np.ndarray) -> list[float]:
    """Quadratischer Rahmen um den Gegenstand mit RAND, als [x, y, seite] in Anteilen von
    Breite (x, seite) und Höhe (y). Darf über den Bildrand hinausragen (dort wird es weiß)."""
    h, w = maske.shape[:2]
    ys, xs = np.where(maske > 0)
    if len(xs) < 50:
        s = min(w, h)
        return [(w - s) / 2 / w, (h - s) / 2 / h, s / w]
    x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
    seite = max(x1 - x0, y1 - y0) * (1 + 2 * RAND)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return [(cx - seite / 2) / w, (cy - seite / 2) / h, seite / w]


# ---------------------------------------------------------------- Verbessern

def verbessern(rgb: np.ndarray, helligkeit: float = 0, kontrast: float = 0, schaerfe: float = 35,
               maske: np.ndarray | None = None) -> np.ndarray:
    """Milder Weißabgleich („Shades of Gray", p = 6), örtlicher Kontrast (CLAHE), Nachschärfen.
    Regler: helligkeit/kontrast -50..50 (0 = Auto), schaerfe 0..100 (35 = Auto).
    Bewusst NICHT die Grauwelt-Regel: an echten Fotos färbte sie ein blaues Relais auf Jeans türkis,
    und auf den Gegenstand allein gemessen machte sie die gelbe Kofferwaage grau-grün (02.10.2026).
    `maske` bleibt als Parameter, wird für den Weißabgleich aber nicht mehr gebraucht."""
    f = rgb.astype(np.float32) / 255
    norm = np.power(np.mean(np.power(f.reshape(-1, 3), 6), axis=0), 1 / 6)
    faktor = np.clip(1 + 0.7 * (norm.mean() / np.maximum(norm, 1e-3) - 1), 0.85, 1.2)
    bild = np.clip(f * faktor * 255, 0, 255).astype(np.uint8)

    lab = cv2.cvtColor(bild, cv2.COLOR_RGB2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=1.6 * (1 + max(kontrast, -40) / 60), tileGridSize=(8, 8))
    l = clahe.apply(l).astype(np.float32)
    if kontrast:
        l = (l - 128) * (1 + kontrast / 100) + 128
    l = l + helligkeit * 0.9
    lab = cv2.merge([np.clip(l, 0, 255).astype(np.uint8), a, b])
    bild = cv2.cvtColor(lab, cv2.COLOR_LAB2RGB)

    menge = max(0.0, min(float(schaerfe), 100.0)) / 100
    if menge > 0:
        weich = cv2.GaussianBlur(bild, (0, 0), 1.6)
        bild = cv2.addWeighted(bild, 1 + menge, weich, -menge, 0)
    return bild


# ---------------------------------------------------------------- Gesamtablauf

def vorschlag(bildpfad: str, maskenpfad: str, einst: dict) -> tuple[np.ndarray, dict]:
    """Rechnet den Vorschlag. `einst`: zuschneiden, verbessern, freistellen (bool), rahmen [x,y,seite]
    oder None (= aus der Maske), helligkeit, kontrast, schaerfe, pinsel. Gibt das Bild (RGB) und die
    tatsächlich benutzten Einstellungen samt Rahmen und den Schritten für die Anzeige zurück."""
    rgb = _lesen(bildpfad)
    h, w = rgb.shape[:2]
    zuschneiden = bool(einst.get("zuschneiden", True))
    besser = bool(einst.get("verbessern", True))
    frei = bool(einst.get("freistellen", True))
    pinsel = einst.get("pinsel") or []
    schritte: list[str] = []

    maske = None
    if zuschneiden or frei or besser:
        maske = pinsel_anwenden(maske_glaetten(maske_holen(bildpfad, maskenpfad)), pinsel)

    rahmen = einst.get("rahmen")
    if rahmen and len(rahmen) == 3:
        rahmen = [float(v) for v in rahmen]
        # Obergrenze: Ein unsinnig großer Rahmen hätte mit Freistellen ein riesiges Bild angelegt und den
        # Dienst am Speicher abstürzen lassen (Durchsicht 03.10.2026). Die App erlaubt höchstens 1,5.
        if not (0.02 <= rahmen[2] <= 3.0) or not all(-3.0 <= v <= 3.0 for v in rahmen[:2]):
            raise ValueError("Rahmen außerhalb des Bildes")
    elif zuschneiden:
        rahmen = rahmen_aus_maske(maske)
    else:
        rahmen = None

    if besser:
        rgb = verbessern(rgb, float(einst.get("helligkeit") or 0), float(einst.get("kontrast") or 0),
                         float(einst.get("schaerfe", 35) if einst.get("schaerfe") is not None else 35),
                         maske if frei else None)
        schritte += ["Weißabgleich", "Kontrast", "Geschärft"]

    if frei:
        alpha = cv2.GaussianBlur(maske.astype(np.float32) / 255, (5, 5), 1.2)[..., None]
        rgb = (rgb * alpha + 255 * (1 - alpha)).astype(np.uint8)
        schritte.append("Hintergrund weiß")

    if rahmen and not frei:
        # Ohne Freistellen gibt es außerhalb des Fotos nichts Sinnvolles: weiß wäre ein harter Kasten, die Randfarbe
        # fortzusetzen zog an der Kofferwaage Streifen (02.10.2026). Also den Rahmen ganz ins Foto rücken.
        s_px = min(rahmen[2] * w, w, h)
        x_px = min(max(rahmen[0] * w, 0), w - s_px)
        y_px = min(max(rahmen[1] * h, 0), h - s_px)
        rahmen = [x_px / w, y_px / h, s_px / w]

    if rahmen:
        x, y, s = int(rahmen[0] * w), int(rahmen[1] * h), max(8, int(rahmen[2] * w))
        tafel = np.full((s, s, 3), 255, np.uint8)
        xa, ya, xb, yb = max(x, 0), max(y, 0), min(x + s, w), min(y + s, h)
        if xb > xa and yb > ya:
            tafel[ya - y:yb - y, xa - x:xb - x] = rgb[ya:yb, xa:xb]
        rgb = tafel
        ziel = int(min(max(s, AUSGABE_MIN), AUSGABE_MAX))
        if ziel != s:
            rgb = cv2.resize(rgb, (ziel, ziel), interpolation=cv2.INTER_AREA if ziel < s else cv2.INTER_CUBIC)
        schritte = ["Zugeschnitten", "Zentriert"] + schritte

    benutzt = {"zuschneiden": zuschneiden, "verbessern": besser, "freistellen": frei, "rahmen": rahmen,
               "helligkeit": float(einst.get("helligkeit") or 0), "kontrast": float(einst.get("kontrast") or 0),
               "schaerfe": float(einst.get("schaerfe", 35) if einst.get("schaerfe") is not None else 35),
               "pinsel": pinsel, "schritte": schritte, "seitenverhaeltnis": w / h}
    return rgb, benutzt


def _lesen(pfad: str) -> np.ndarray:
    bgr = cv2.imread(pfad, cv2.IMREAD_COLOR)
    if bgr is None:
        raise FileNotFoundError(pfad)
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)


def speichern_jpeg(rgb: np.ndarray, pfad: str, qualitaet: int = 88) -> None:
    os.makedirs(os.path.dirname(pfad), exist_ok=True)
    cv2.imwrite(pfad, cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, qualitaet])
