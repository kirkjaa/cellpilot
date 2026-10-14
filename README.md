# CellPilot

Which steps of a manual job could a cobot take over? Film a workstation with three time-aligned cameras, and CellPilot returns a feasibility study for an engineer to review: the steps with timings, a verdict on each step with evidence drawn on the footage, a self-check across the three views, a top-view layout of the station, a cobot checked by payload and reach against a six-arm catalogue, a cycle-time and payback estimate, and a printable study. The engineer approves or flags every step.

Built by team NODAL for the AI Builder Cup 2026, Manufacturing track. All code here was written during the hackathon.

## How it works

1. **Input:** three MP4 clips (`cam0`, `cam1`, `cam2`) and a `session.json` with each camera's time offset. Our reference rig is [NODAL M1](https://www.nodal3d.com/) (three global-shutter cameras started by one RF trigger, so the offsets come straight from the sync sidecars); any calibrated, time-aligned cameras work. The page's **Capture guide** (`#/guide`) shows where the cameras go, how the clips are aligned, what good footage looks like and how a session folder is made.
2. **Gemini 4 Argon on Vertex AI**, three passes, each held to a JSON schema, all sharing the same clips-and-context prefix so implicit caching applies:
   - pass A sees all three clips in one request and returns the steps, their timings on one shared clock, a verdict per step (automate, keep human, or needs a fixture) with two reasons that each cite a camera, a time and a box on that frame;
   - pass D (the verifier) sees the same clips and pass A's answer, corrects timings that are off by more than one second, and flags any verdict it disagrees with. It never changes a verdict;
   - pass C (the layout) estimates a top view in metres: bench size, bins, fixtures and tools, and where each step picks and places. It is optional: if it fails the study ships without a layout and says why.
3. **Our code** places the robot base and computes the needed reach from the layout, checks every arm in the catalogue by payload and reach and picks the smallest one that passes, estimates the robot's cycle from the step durations and a conservative speed factor, computes the payback (every input editable on the page), and records the engineer's sign-offs. Argon's raw answers are stored word for word, and each result names the model that produced it.

The bundled sample session uses synthetic test clips (a red square crossing a line) until the M1 kitting session is recorded; it is badged as such on the page.

## What the page shows

- **Sessions:** what a session is, the three-step flow, and how to bring your own footage.
- **Analysis:** the "N of M steps can be automated" banner; the three views in sync with the evidence boxes drawn over them; the timeline; each step with its verdicts, reasons (click to seek and see the box), verifier note and sign-off; the **Layout** sketch (bench, objects, pick-to-place arrows, the proposed base, the chosen arm's reach circle and the needed reach); the **Robot** card with every catalogue arm judged; the **Cycle time** card (where the cycle goes by step, and the robot estimate with an editable speed factor); **Payback**.
- **Printable study** (`#/s/<sid>/report`): one document with everything above and the sign-offs, for print or PDF. **Export JSON** gives the raw result and sign-offs.

Every estimate says so on screen: the layout is "estimated from video", the robot cycle is an "estimate", price bands are "indicative".

## Run it locally

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt -r requirements-dev.txt
.venv\Scripts\python -m pytest            # no network: Argon is stubbed
node --test web\logic.test.mjs            # the page's pure logic
.venv\Scripts\python -m app.main          # http://localhost:8080, store in .\data
```

Viewing the stored sample needs no credentials. A live run needs Vertex AI access through Application Default Credentials (`gcloud auth application-default login`) and `GOOGLE_CLOUD_PROJECT`. Settings: `CELLPILOT_MODEL` (default `gemini-4-argon`), `CELLPILOT_VIDEO_FPS` (default 1), `CELLPILOT_DAILY_LIVE_RUNS` (default 10), `CELLPILOT_MAX_RUNNING` (default 1), `CELLPILOT_STORE` (a folder, or `gs://bucket` in production). `web/dev/mock_server.py` serves the page with hand-written fixture data for front-end work.

To regenerate a sample's stored analysis (three paid Argon calls): `python -m app.pipeline <sid> --save-sample`.

## Deploy

One Cloud Run service: `deploy.ps1` builds from source and deploys with `--no-cpu-throttling --max-instances 1`, because each analysis runs in a background thread of the single instance. Results and sign-offs go to Cloud Storage. Clips over 15 MB in total must be served from the bucket (`CELLPILOT_STORE=gs://...`).

## The robot catalogue

Six cobots with the vendors' published payload and reach (links in `app/catalogue.py`): UR3e (3 kg, 500 mm), UFACTORY xArm 6 (5 kg, 700 mm), UR5e (5 kg, 850 mm), Franka FR3 (3 kg, 855 mm), Doosan M0609 (6 kg, 900 mm), UR10e (12.5 kg, 1300 mm). Needed payload = the heaviest part of the automatable steps + 1 kg for a two-finger gripper. Needed reach = the farthest pick or place of the automate and needs-fixture steps from the proposed base ÷ 0.85. The smallest arm that passes both checks is chosen; with no layout the reach is not checked and arms under 700 mm are not proposed. Price bands are rough list-price bands from public reseller listings, labelled indicative, never quotes.

## Honest limits

- The sample is analysed from stored results; "Run it live" re-analyses it with Argon, capped per day and shared by all visitors.
- The layout is the model's estimate from the footage, not a measurement; a measured layout from a calibration board seen by all three cameras is the next step. The robot cycle is step durations × a conservative factor, not a simulation.
- Argon's timings are only as precise as the frame sampling rate (0.5 s at 1 fps on our tests, 0.1 s at 5 fps); evidence boxes are the model's, on a 0–1000 grid.
- **No browser upload in this build.** A session is added by its folder on the server. The design, if it is added: `POST /api/sessions` (name, offsets), then one `PUT /api/sessions/{sid}/clips/{view}` per clip (Cloud Run caps a request at 32 MiB, so the three clips cannot share one request), stored in the bucket and served through `/media`; the analysis of an uploaded session counts against the same daily live-run cap. It was left out because clips of an unrelated scene would only produce an unusable study, and the time went to the layout, the robot check and the printable study instead.

## Licence

Apache-2.0 (see `LICENSE`). Third-party components are listed in `THIRD_PARTY_LICENSES.md`.
