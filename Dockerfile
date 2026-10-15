FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080
WORKDIR /srv

# ffmpeg: "Make session" re-encodes a take's clips; the capture bridge runs inside this service for the cloud demo.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY web/ web/
COPY capture/ capture/
COPY samples/ samples/

# samples/ stays writable so a session made in the cloud demo appears under Studies (it lasts as long as the instance).
RUN useradd --system --no-create-home app && chown -R app:app /srv/samples
USER app

# Cloud Run sets $PORT; the shell form expands it.
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
