# CellPilot contract (lean build)

The server (`app/`) and the page (`web/`) are built in parallel against this file. Change it only by agreement.

## Shape of the system

* One Cloud Run service (FastAPI). It serves the page from `web/`, the API under `/api/`, and runs each analysis in a background thread (deploy with `--no-cpu-throttling --max-instances 1`).
* Sample sessions ship inside the image under `samples/<sid>/`, with their clips and a stored analysis, so judges get an instant result at no cost.
* Live analyses and sign-offs are JSON files in a store: a local folder in development (`CELLPILOT_STORE=./data`), a Cloud Storage bucket in production (`CELLPILOT_STORE=gs://<bucket>`).
* Gemini 4 Argon on Gemini Enterprise Agent Platform (formerly Vertex AI): `genai.Client(enterprise=True, project=$GOOGLE_CLOUD_PROJECT, location="global")`, one long-lived client, model from `CELLPILOT_MODEL` (default `gemini-4-argon`). Credentials only from the environment.
* Three Argon calls per analysis, all with the same clips-and-context prefix (so implicit caching applies): **A** task graph and verdicts (thinking HIGH), **D** verifier (HIGH), **C** layout estimate (MEDIUM). Call C is optional: if it fails, the run still finishes with `layout: null` and `layout_error` set.
* One more call per analysis, **E** the engineer's notes, goes to a general Gemini model (`CELLPILOT_NOTES_MODEL`, default `gemini-3.8-flash`) with the assembled result as text and no clips; it returns `notes` (for_the_owner, step_notes, risks, next_steps, questions) and the result records `notes_model`. Optional like C: on failure `notes: null` and `notes_error` set.

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

Page routes (hash, no server round trip): `#/` sessions, `#/guide` the capture guide, `#/capture` the capture studio, `#/s/{sid}` the analysis, `#/s/{sid}/report[/{aid}]` the printable study (print or save as PDF from the browser).

The latest analysis for a sample is its stored one (with any sign-offs); a visitor's live run is shown to that visitor by its id, so a stray live result never replaces the curated sample for later visitors. A session with no stored analysis shows its newest finished live run. Daily live-run cap: `CELLPILOT_DAILY_LIVE_RUNS` (default 10); at most `CELLPILOT_MAX_RUNNING` (default 1) runs at once, and a second click during a run joins it.

## Capture bridge

A second, local service (`capture/`, `python -m capture.bridge`, **127.0.0.1:8095**, never deployed) between the page and a NODAL M1 rig. It holds one WebSocket per camera node and one to the rig controller, speaking their protocol (one JSON envelope `{"type", "payload", "id", "wall", "mono", "v": "4.0.0"}` per text frame, acks matched by `id`; binary preview frames). The controller owns the RF dongle: the bridge arms and fires only through the controller's `cmd.fire` and never opens a serial port. Config: `capture.json` (`cameras: [{id, url}]`, `controller: {url}`, `offload_dir`, `samples_dir`, `preset`, `allow_lan`); non-localhost addresses are refused unless `allow_lan` is true.

The page (`#/capture`) probes `GET http://127.0.0.1:8095/api/capture/health` with a 2.5 s timeout. If a bridge answers it shows the studio; otherwise (the cloud link, or a PC without the bridge) it shows the capture guide under a note that says the studio runs on the PC next to the rig. The bridge answers CORS for localhost origins only (the page is served by the main app on another port).

| Method and path | Purpose | Response |
|---|---|---|
| `GET /api/capture/health` | is a bridge here | `{"ok": true, "bridge": "cellpilot-capture", "version", "cameras", "cameras_online", "controller_online", "config", "ffmpeg"}` |
| `GET /api/capture/config` | the loaded config | `{"cameras": [{"id", "url"}], "controller_url", "offload_dir", "samples_dir", "preset", "allow_lan", "path"}` |
| `GET /api/capture/cameras` | one entry per configured camera, from its last heartbeat | `[{"id", "url", "online", "error", "state", "preset", "armed", "battery": {"bars": 0..4, "charging", "full"}, "chrony_offset_ms", "rf_ok", "simulated", "capture": {"dequeued", "dropped", "expected_per_sec", "last_frame_age_s", "pump_alive"}, "heartbeat_age_s", "preview_age_s", "preview_frames", "connects"}]` |
| `POST /api/capture/cameras/ping` | `cmd.ping` to every camera | `{"<id>": {"rtt_ms", "wall_offset_ms", "state"} or {"error"}}` |
| `GET /api/capture/cameras/{id}/preview.jpg` | the newest preview frame | image/jpeg (`X-Preview-Seq`, `X-Preview-Mono-Ns`), 404 before the first frame |
| `GET /api/capture/cameras/{id}/preview.mjpeg` | the frames as they arrive | multipart/x-mixed-replace (an `<img>` plays it) |
| `POST /api/capture/preview` | body `{"on", "fps": 4, "max_width": 640}`: `cmd.start_preview` / `cmd.stop_preview` to every camera | `{"preview", "nodes": {"<id>": {"ok", "reason", "payload"}}}` |
| `POST /api/capture/configure` | body `{"preset"}`: `cmd.configure` to every camera (20 s) | `{"preset", "nodes"}` |
| `GET /api/capture/presets` | presets the page offers | `["1080p30", "720p30", ...]` |
| `GET /api/capture/controller` | the controller link and its `cmd.list_nodes` | `{"url", "online", "error", "nodes"}` |
| `POST /api/capture/takes` | body `{"name", "seconds": 3..900}`: one take through the controller's `cmd.fire {session, clip, max_seconds}` | `202` take; `409` when a take is in progress or the controller is offline |
| `GET /api/capture/takes[/{id}]` | poll every 0.5 s | take (below) |
| `POST /api/capture/takes/{id}/stop` | `cmd.stop` through the controller | take |
| `POST /api/capture/takes/{id}/check` | run the offset check again | take; `409` while the clips are not in the offload folder |
| `POST /api/capture/check` | body `{"folder", "clip?", "ref?"}`: the offset check on any offload folder (a take recorded with NODAL Capture) | `{"folder", "check"}` |
| `GET /api/capture/folders` | takes already on this PC | `{"offload_dir", "folders": {"<session>": ["take_001", ...]}}` |
| `POST /api/capture/sessions` | body `{"sid", "name", "take_id" or "folder" (+ "clip"), "ref?", "overwrite?"}`: convert to `<samples_dir>/<sid>/` in a thread | `202` job; `409` when the id exists (unless `overwrite`) or the take is not stopped |
| `GET /api/capture/sessions[/{id}]` | poll every 1 s | job `{"id", "status": "queued \| running \| done \| failed", "sid", "name", "folder", "clip", "out_dir", "error", "log": [], "session", "url": "#/s/<sid>"}` |

### take

```json
{"id": "t-20261015-153414-d4ba", "name": "kitting", "session": "kitting", "clip": "take_001", "seconds": 20,
 "state": "arming | armed | recording | stopped | failed", "error": null,
 "created_at": 0, "fired_at": 0, "started_at": 0, "stopped_at": 0, "elapsed_s": 10.3,
 "edge_id": 1, "rf_acked": true, "armed": ["rx-cam1", "rx-cam2", "rx-cam3"], "not_armed": {},
 "cameras": {"rx-cam1": {"started": true, "stopped": true, "path": "...", "frames": 306, "duration_s": 10.2, "bytes": 0}},
 "offloaded": true, "folder": "<offload_dir>/kitting", "check": {...}, "check_error": null}
```

`session` is the take's name cleaned to `[A-Za-z0-9_-]` (the folder on the cameras), `clip` is `take_NNN` numbered after the takes already in the offload folder. The take is `armed` when the controller's fire ack arrives, `recording` on the first camera's `evt.recording_started`, `stopped` when every camera that started has sent `evt.recording_stopped`; it fails if no camera starts within 6 s of the trigger. After the stop the bridge looks for `<clip>_<node>.{mp4,jsonl}` in the camera's reported path, then `<offload_dir>/<session>`, then `<offload_dir>`, for 20 s; `offloaded` becomes true together with `check`. With the real cameras the files arrive only after an offload, so "Check" and "Make session" can be run later.

### check

```json
{"ref": "rx-cam2", "order": ["rx-cam2", "rx-cam1", "rx-cam3"], "fps": 30,
 "delays_ms": {"rx-cam1": 10.366, "rx-cam2": 0.806, "rx-cam3": 30.7},
 "offsets_s": {"rx-cam2": 0.0, "rx-cam1": 0.00956, "rx-cam3": 0.029894},
 "views": {"rx-cam2": "cam0", "rx-cam1": "cam1", "rx-cam3": "cam2"},
 "spread_ms": 29.894, "frame_ms": 33.333, "within_a_frame": true, "missing": []}
```

`delay = first_frame.mono_ns − edge_mono_ns` from each camera's sidecar, both on that camera's own monotonic clock (the cameras have no RTC; wall clocks are never used). `ref` is the earliest camera (or the one asked for) and becomes `cam0`; the others follow by delay; `offset_s = (delay − delay_ref) / 1e9` is exactly the `offset_s` written to `session.json`. The session folder also gets `sources.json` (which node became which view, the sidecar paths and this check).
