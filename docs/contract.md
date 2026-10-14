# CellPilot contract (lean build)

The server (`app/`) and the page (`web/`) are built in parallel against this file. Change it only by agreement.

## Shape of the system

* One Cloud Run service (FastAPI). It serves the page from `web/`, the API under `/api/`, and runs each analysis in a background thread (deploy with `--no-cpu-throttling --max-instances 1`).
* Sample sessions ship inside the image under `samples/<sid>/`, with their clips and a stored analysis, so judges get an instant result at no cost.
* Live analyses and sign-offs are JSON files in a store: a local folder in development (`CELLPILOT_STORE=./data`), a Cloud Storage bucket in production (`CELLPILOT_STORE=gs://<bucket>`).
* Gemini 4 Argon on Vertex AI: `genai.Client(enterprise=True, project=$GOOGLE_CLOUD_PROJECT, location="global")`, one long-lived client, model from `CELLPILOT_MODEL` (default `gemini-4-argon`). Credentials only from the environment.
* Three Argon calls per analysis, all with the same clips-and-context prefix (so implicit caching applies): **A** task graph and verdicts (thinking HIGH), **D** verifier (HIGH), **C** layout estimate (MEDIUM). Call C is optional: if it fails, the run still finishes with `layout: null` and `layout_error` set.

## Files

```
samples/<sid>/session.json
samples/<sid>/cam0.mp4 cam1.mp4 cam2.mp4        720p, constant 30 fps, keyframe every second
samples/<sid>/calibration.json                    optional in tier A
samples/<sid>/analyses/<aid>.json                 the stored result (live=false)
<store>/sessions/<sid>/analyses/<aid>.json        live runs
<store>/limits/<YYYY-MM-DD>.json                  {"count": n}
```

### session.json

```json
{
  "id": "kitting-01",
  "name": "Kitting station, 9 cycles",
  "kind": "sample",
  "recorded": "2026-10-15",
  "synthetic": false,
  "clips": [
    {"view": "cam0", "file": "cam0.mp4", "fps": 30, "width": 1280, "height": 720, "duration_s": 168.0, "offset_s": 0.0},
    {"view": "cam1", "file": "cam1.mp4", "fps": 30, "width": 1280, "height": 720, "duration_s": 168.0, "offset_s": 0.2},
    {"view": "cam2", "file": "cam2.mp4", "fps": 30, "width": 1280, "height": 720, "duration_s": 168.0, "offset_s": 0.45}
  ]
}
```

`offset_s` is the cam0 time at which that camera's clip starts. All times in results are cam0 seconds. `synthetic: true` marks development sessions; the page shows a "synthetic test clips" badge for them.

### analysis (`<aid>.json`)

```json
{
  "id": "a-20261015-071200-ab12",
  "session_id": "kitting-01",
  "status": "queued | running | done | failed",
  "stage": "queued | steps | verifier | layout | robot | payback | done",
  "live": true,
  "model": "gemini-4-argon",
  "started_at": "ISO 8601", "finished_at": "ISO 8601 or null",
  "error": null,
  "usage": {"prompt_tokens": 0, "output_tokens": 0, "seconds": 0.0},
  "result": null,
  "signoffs": {}
}
```

`result` (present when `status` is `done`):

```json
{
  "summary": "one paragraph",
  "cycles": 9,
  "cycle_time_s": 38.4,
  "steps": [
    {
      "id": "s1",
      "name": "Pick bracket",
      "verdict": "automate | keep_human | needs_fixture",
      "reasons": [{"text": "...", "view": "cam1", "t_s": 12.0, "box": [380, 120, 620, 330]}],
      "occurrences": [{"cycle": 1, "start_s": 3.1, "end_s": 7.3}],
      "parts": ["bracket"],
      "confidence": 0.8,
      "mean_duration_s": 4.2,
      "verifier": {"agree": true, "verdict_agree": true, "timing_delta_s": 0.3, "note": ""},
      "preflagged": false
    }
  ],
  "parts": [{"name": "bracket", "mass_g": 120, "size_mm": [80, 40, 3], "confidence": 0.6}],
  "counts": {"automate": 4, "keep_human": 2, "needs_fixture": 1, "total": 7},
  "automated_share": 0.55,
  "robot": {
    "model": "xArm 6", "vendor": "UFACTORY", "gripper": "two-finger parallel gripper (Robotiq 2F-85 class), 1 kg allowance",
    "source": "https://...", "payload_kg": 5.0, "reach_mm": 700,
    "needed_payload_kg": 1.12, "needed_reach_mm": 526, "payload_fits": true, "reach_fits": true, "fits": true,
    "reach_basis": "layout estimated from video (Argon call C)",
    "price_band": {"low": 10000, "high": 14000, "currency": "USD", "label": "indicative"},
    "why": "...",
    "alternatives": [{"model": "UR3e", "vendor": "Universal Robots", "payload_kg": 3.0, "reach_mm": 500,
                      "payload_fits": true, "reach_fits": false, "fits": false, "chosen": false,
                      "price_band": {...}, "note": "reach short by 26 mm", "source": "https://..."}]
  },
  "roi_defaults": {"currency": "SGD", "wage_per_h": 14.0, "shifts": 2, "hours_per_shift": 8, "days_per_year": 250,
                   "robot_price": 40000, "system_multiple": 3.0, "grant_share": 0.5, "grant_cap": 30000},
  "layout": {
    "source": "estimated from video by Argon (call C): ...", "frame": "bench frame: ...",
    "bench": {"length_m": 1.2, "width_m": 0.6}, "operator": {"x_m": 0.6, "y_m": -0.35},
    "objects": [{"name": "bin A", "kind": "bin | fixture | tool | part_source | part_destination | other",
                 "x_m": 0.2, "y_m": 0.4, "length_m": 0.2, "width_m": 0.2, "step_ids": ["s1"], "confidence": 0.7}],
    "anchors": [{"step_id": "s1", "name": "Pick bracket", "verdict": "automate", "kind": "pick | place", "x_m": 0.2, "y_m": 0.4}],
    "robot_base": {"x_m": 0.6, "y_m": 0.6, "rule": "..."},
    "needed_reach_m": 0.526, "reach_rule": "...", "confidence": 0.6, "notes": "..."
  },
  "layout_error": null,
  "cycle_estimate": {"human_cycle_s": 38.4, "automated_steps_s": 14.2, "robot_cycle_s": 18.5, "operator_cycle_s": 20.1,
                     "unassigned_s": 4.1, "parallel_cycle_s": 24.2, "speed_factor": 1.3, "method": "..."}
}
```

* `automated_share` = sum of `mean_duration_s` of `automate` steps ÷ `cycle_time_s`.
* `preflagged` is true when the verifier disagrees with the verdict or the timing differs by more than 1 s (timing over 1 s is corrected in `occurrences`; the verdict is never changed).
* `signoffs`: `{"s1": {"decision": "approved | flagged", "note": "", "engineer": "", "at": "ISO 8601"}}`.
* `reasons[].box` is `[ymin, xmin, ymax, xmax]` on a 0–1000 grid over that camera's frame, or `[]`; the page draws it over the video near `t_s`. Results made before the box existed have no `box` key: treat it as `[]`.
* `layout` (optional, may be null) is Argon's top-view estimate in a **bench frame**: origin at the bench's front-left corner as the operator sees it, x to the operator's right along the front edge, y away from the operator, metres; the operator stands at y < 0. Our code places `robot_base` on the back edge or an end at the point that minimises the farthest pick or place of the `automate` and `needs_fixture` steps, and sets `needed_reach_m` = that distance ÷ 0.85. Everything in it is labelled an estimate on the page. `layout_error` says why it is missing.
* `robot`: the catalogue has six arms (`app/catalogue.py`, vendor figures with source links). The chosen arm is the first in ascending-reach order that carries `needed_payload_kg` (heaviest automatable part + 1 kg gripper allowance) and, when `needed_reach_mm` is known, reaches it; with no layout the reach is not checked (`reach_fits: null`) and arms under 700 mm are not proposed. `fits` = payload fits and reach not known to fail. `price_band` is an indicative list-price band, not a quote. `alternatives` judges every arm. The first-build fields (`model, payload_kg, reach_mm, needed_payload_kg, fits, why`) keep their meaning; everything else is optional.
* `cycle_estimate` (optional): `robot_cycle_s = automated_steps_s × speed_factor` (1.3, conservative, no motion planning); `parallel_cycle_s = max(robot_cycle_s, operator_cycle_s + unassigned_s)`. The page recomputes it when the factor is edited (same formula in `web/logic.js`).

### Payback (same formula in Python and JavaScript)

```
annual_labour  = wage_per_h × shifts × hours_per_shift × days_per_year
annual_savings = annual_labour × automated_share
investment     = robot_price × system_multiple
grant          = min(grant_share × investment, grant_cap)
payback_months = (investment − grant) ÷ annual_savings × 12      (null if annual_savings is 0)
```

The page shows the inputs as editable fields and recomputes on change; it labels the robot price as indicative and cites the 3 to 5 × system multiple (Automation World; Fraunhofer IPA) and the PSG grant (Enterprise Singapore).

## API

| Method and path | Purpose | Response |
|---|---|---|
| `GET /api/health` | liveness | `{"ok": true}` |
| `GET /api/sessions` | list sessions | `[{"id", "name", "kind", "synthetic", "clips": 3, "duration_s"}]` |
| `GET /api/sessions/{sid}` | session, clip URLs, latest analysis | `{"session": {...}, "clip_urls": {"cam0": "/media/<sid>/cam0.mp4", ...}, "latest": analysis or null, "live_runs_left": n}` |
| `POST /api/sessions/{sid}/analyses` | start a live analysis (queues only; returns at once) | `202 {"id"}`, or `429 {"detail": "Daily limit reached"}` |
| `GET /api/sessions/{sid}/analyses/{aid}` | poll every 5 s | analysis |
| `PUT /api/sessions/{sid}/analyses/{aid}/signoffs/{step_id}` | body `{"decision", "note", "engineer"}` | analysis |
| `GET /api/sessions/{sid}/analyses/{aid}/export` | download result + sign-offs | JSON attachment |
| `GET /media/{sid}/{file}` | clips, with HTTP range support for seeking | video/mp4 |

Page routes (hash, no server round trip): `#/` sessions, `#/guide` the capture guide, `#/s/{sid}` the analysis, `#/s/{sid}/report[/{aid}]` the printable study (print or save as PDF from the browser).

The latest analysis for a sample is its stored one (with any sign-offs); a visitor's live run is shown to that visitor by its id, so a stray live result never replaces the curated sample for later visitors. A session with no stored analysis shows its newest finished live run. Daily live-run cap: `CELLPILOT_DAILY_LIVE_RUNS` (default 10); at most `CELLPILOT_MAX_RUNNING` (default 1) runs at once, and a second click during a run joins it.
