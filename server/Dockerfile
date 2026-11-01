# Fundus: FastAPI + SQLite, Fotos als Dateien. Die Schrift (DejaVu)
# braucht nur das Etikett -- im schlanken Abbild ist sonst keine dabei.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Freistell-Modell für „Bilder optimieren" (02.10.2026): ISNet aus den rembg-Freigaben, 179 MB.
# Beim Bauen geladen und per MD5 geprüft -- zur Laufzeit lädt der Server nichts aus dem Netz.
RUN mkdir -p /modelle && python -c "import hashlib,urllib.request; \
u='https://github.com/danielgatis/rembg/releases/download/v0.0.0/isnet-general-use.onnx'; \
d=urllib.request.urlopen(u, timeout=300).read(); \
h=hashlib.md5(d).hexdigest(); assert h=='fc16ebd8b0c10d971d3513d564d01e29', h; \
open('/modelle/isnet-general-use.onnx','wb').write(d)"
COPY . /app
ENV PYTHONUNBUFFERED=1
EXPOSE 8095
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8095"]
