"""Kistenetikett: nur QR-Code und Nummer (Daniel, 01.10.2026).

Vorgabe sind Etiketten 40 x 30 mm (übliche Größe kleiner Etikettendrucker). Das Format
bleibt einstellbar. Bei fast quadratischen Etiketten steht die Nummer UNTER dem QR-Code,
bei langen daneben -- neben einem 26-mm-QR blieben auf 40 mm sonst nur 8 mm für die Nummer. Gerechnet wird
in 300 dpi; die PDF-Fassung trägt die echte Größe, damit kein Druckdialog skaliert.
"""
from __future__ import annotations
import io
import qrcode
from PIL import Image, ImageDraw, ImageFont

DPI = 300
SCHRIFT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
QR_PRAEFIX = "FUNDUS:"
# Bis 03.10.2026 hieß die App „Werkstatt-Kiste"; die schon geklebten Etiketten tragen dieses Präfix und gelten weiter.
ALTE_PRAEFIXE = ("WERKSTATT:",)


def qr_inhalt(code: str) -> str:
    """Was im QR-Code steht. Das Präfix sorgt dafür, dass die App fremde Codes
    (Paketaufkleber, Werbung) nicht für eine Kiste hält."""
    return QR_PRAEFIX + code


def code_aus_qr(text: str) -> str | None:
    t = (text or "").strip()
    for p in (QR_PRAEFIX, *ALTE_PRAEFIXE):
        if t.upper().startswith(p):
            return t[len(p):].strip().upper()
    return None


def _px(mm: float) -> int:
    return round(mm / 25.4 * DPI)


def _schrift(groesse: int):
    try:
        return ImageFont.truetype(SCHRIFT, groesse)
    except OSError:
        return ImageFont.load_default(groesse)


def bild(code: str, breite_mm: float = 40, hoehe_mm: float = 30) -> Image.Image:
    b, h = _px(breite_mm), _px(hoehe_mm)
    rand = _px(1.5)
    img = Image.new("L", (b, h), 255)
    q = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=2, box_size=10)
    q.add_data(qr_inhalt(code))
    q.make(fit=True)
    qr = q.make_image(fill_color="black", back_color="white").convert("L")
    zeichen = ImageDraw.Draw(img)
    untereinander = b / h < 1.7

    def einpassen(frei_b: int, frei_h: int):
        groesse = frei_h * 2
        while groesse > 10:
            f = _schrift(groesse)
            l, o, r, u = zeichen.textbbox((0, 0), code, font=f)
            if r - l <= frei_b and u - o <= frei_h:
                return f, (l, o, r, u)
            groesse -= 2
        f = _schrift(10)
        return f, zeichen.textbbox((0, 0), code, font=f)

    if untereinander:
        text_h = round(h * 0.24)
        seite = min(b - 2 * rand, h - 2 * rand - text_h)
        qr = qr.resize((seite, seite), Image.NEAREST)
        img.paste(qr, ((b - seite) // 2, rand))
        f, (l, o, r, u) = einpassen(b - 2 * rand, text_h)
        x = (b - (r - l)) // 2 - l
        y = rand + seite + (h - rand - seite - rand - (u - o)) // 2 - o
    else:
        seite = h - 2 * rand
        qr = qr.resize((seite, seite), Image.NEAREST)
        img.paste(qr, (rand, rand))
        frei_x = rand + seite + rand
        f, (l, o, r, u) = einpassen(b - frei_x - rand, h - 2 * rand)
        x = frei_x + (b - frei_x - rand - (r - l)) // 2 - l
        y = (h - (u - o)) // 2 - o
    zeichen.text((x, y), code, font=f, fill=0)
    return img


def als_png(code: str, breite_mm: float, hoehe_mm: float) -> bytes:
    puffer = io.BytesIO()
    bild(code, breite_mm, hoehe_mm).save(puffer, "PNG", dpi=(DPI, DPI))
    return puffer.getvalue()


def als_pdf(codes: list[str], breite_mm: float, hoehe_mm: float) -> bytes:
    """Eine Seite je Etikett, in Etikettengröße -- so druckt ein Etikettendrucker
    jede Seite auf genau ein Etikett."""
    seiten = [bild(c, breite_mm, hoehe_mm).convert("RGB") for c in codes]
    puffer = io.BytesIO()
    seiten[0].save(puffer, "PDF", resolution=DPI, save_all=True, append_images=seiten[1:])
    return puffer.getvalue()
