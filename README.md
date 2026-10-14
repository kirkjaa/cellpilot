# CellPilot

Which steps of a manual job could a cobot take over? Film a workstation with three time-aligned cameras, and CellPilot returns a feasibility study for an engineer to review: the steps with timings, a verdict on each step with evidence from the footage, a self-check across the three views, a cobot chosen by payload, and a payback estimate. The engineer approves or flags every step.

Built by team NODAL for the AI Builder Cup 2026, Manufacturing track. All code here was written during the hackathon.

## How it works

1. **Input:** three MP4 clips (`cam0`, `cam1`, `cam2`) and a `session.json` with each camera's time offset. Our reference rig is [NODAL M1](https://www.nodal3d.com/); any calibrated, time-aligned cameras work.
2. **Gemini 4 Argon on Vertex AI**, two passes, each held to a JSON schema:
   - pass 1 sees all three clips in one request and returns the steps, their timings on one shared clock, and a verdict per step (automate, keep human, or needs a fixture) with two reasons that cite a camera and a time;
   - pass 2 (the verifier) sees the same clips and pass 1's answer, corrects timings that are off by more than one second, and flags any verdict it disagrees with. It never changes a verdict.
3. **Our code** chooses a cobot from a small catalogue by payload, computes the cycle time and the payback (every input editable on the page), and records the engineer's sign-offs. Argon's raw answers are stored word for word, and each result names the model that produced it.

The bundled sample session uses synthetic test clips (a red square crossing a line) until the M1 kitting session is recorded; it is badged as such on the page.

## Run it locally

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt -r requirements-dev.txt
.venv\Scripts\python -m pytest            # no network: Argon is stubbed
.venv\Scripts\python -m app.main          # http://localhost:8080, store in .\data
```

Viewing the stored sample needs no credentials. A live run needs Vertex AI access through Application Default Credentials (`gcloud auth application-default login`) and `GOOGLE_CLOUD_PROJECT`. Settings: `CELLPILOT_MODEL` (default `gemini-4-argon`), `CELLPILOT_VIDEO_FPS` (default 1), `CELLPILOT_DAILY_LIVE_RUNS` (default 10), `CELLPILOT_MAX_RUNNING` (default 1), `CELLPILOT_STORE` (a folder, or `gs://bucket` in production).

## Deploy

One Cloud Run service: `deploy.ps1` builds from source and deploys with `--no-cpu-throttling --max-instances 1`, because each analysis runs in a background thread of the single instance. Results and sign-offs go to Cloud Storage. Clips over 15 MB in total must be served from the bucket (`CELLPILOT_STORE=gs://...`).

## Honest limits

- The sample is analysed from stored results; "Run it live" re-analyses it with Argon, capped per day and shared by all visitors.
- Robot choice is by payload only; reach is shown but not checked. A measured 3D layout and a simulated cobot are the next steps and are not in this build.
- Argon's timings are only as precise as the frame sampling rate (0.5 s at 1 fps on our tests, 0.1 s at 5 fps).

## Licence

Apache-2.0 (see `LICENSE`). Third-party components are listed in `THIRD_PARTY_LICENSES.md`.
