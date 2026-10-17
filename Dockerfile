FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080
WORKDIR /srv

# ffmpeg: "Make session" re-encodes a take's clips; the capture bridge runs inside this service for the cloud demo.
# git: fetches the two arm models the cobot twin uses from MuJoCo Menagerie (a declared dependency, not part of this repo).
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg git && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --filter=blob:none --sparse https://github.com/google-deepmind/mujoco_menagerie /opt/menagerie     && cd /opt/menagerie && git sparse-checkout set universal_robots_ur5e universal_robots_ur10e && rm -rf .git
ENV CELLPILOT_MENAGERIE=/opt/menagerie

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
