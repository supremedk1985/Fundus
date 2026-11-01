import io, os, sys, tempfile
import pytest

# Die Haus-Teile (Küchen-Dashboard, Selbstupdate) sind in den meisten Tests dabei; test_oeffentlich.py schaltet sie ab.
os.environ["HAUSFUNKTIONEN"] = "1"

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture()
def client(monkeypatch):
    """Frischer Server mit leerer Datenbank in einem Wegwerf-Ordner. Die echte
    Bilderkennung wird NIE gefragt -- erkennung.erkennen ist ersetzt."""
    import db
    tmp = tempfile.mkdtemp()
    monkeypatch.setattr(db, "DATEN", tmp)
    monkeypatch.setattr(db, "PFAD", os.path.join(tmp, "fundus.sqlite3"))
    monkeypatch.setattr(db, "FOTOS", os.path.join(tmp, "fotos"))
    monkeypatch.setenv("PASSWORT", "geheim123")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEMO", raising=False)
    from fastapi.testclient import TestClient
    import app as servermodul
    with TestClient(servermodul.app) as c:
        token = c.post("/api/anmelden", json={"passwort": "geheim123", "geraet": "test"}).json()["token"]
        c.headers["X-Token"] = token
        yield c


def jpeg(farbe=(200, 120, 40), groesse=(800, 600)) -> bytes:
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", groesse, farbe).save(b, "JPEG")
    return b.getvalue()
