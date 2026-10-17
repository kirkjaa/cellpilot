// CellPilot page logic: pure functions, no DOM.
// Imported by app.js in the browser and by logic.test.mjs under `node --test`.

export const STAGES = ['steps', 'verifier', 'layout', 'robot', 'payback', 'notes'];

export const VERDICT_LABEL = {
  automate: 'Automate',
  needs_fixture: 'Needs fixture',
  keep_human: 'Keep human',
};

// Drift thresholds for keeping the follower videos on the master (cam0).
export const DRIFT_LEAVE_S = 0.015; // under 15 ms: leave it
export const DRIFT_SEEK_S = 0.080;  // over 80 ms: hard seek
export const NUDGE = 0.05;          // in between: playbackRate 1 +/- 5 %

const clamp = (x, lo, hi) => Math.min(Math.max(x, lo), hi);

/**
 * Payback, exactly as docs/contract.md:
 *   annual_labour  = wage_per_h * shifts * hours_per_shift * days_per_year
 *   annual_savings = annual_labour * automated_share
 *   investment     = robot_price * system_multiple
 *   grant          = min(grant_share * investment, grant_cap)
 *   payback_months = (investment - grant) / annual_savings * 12   (null if annual_savings is 0)
 * Any input that is not a number makes payback_months null as well.
 */
export function computePayback(inputs, automatedShare) {
  const n = (k) => Number(inputs?.[k]);
  const annual_labour = n('wage_per_h') * n('shifts') * n('hours_per_shift') * n('days_per_year');
  const annual_savings = annual_labour * Number(automatedShare);
  const investment = n('robot_price') * n('system_multiple');
  const grant = Math.min(n('grant_share') * investment, n('grant_cap'));
  const net_investment = investment - grant;
  let payback_months = null;
  if (annual_savings !== 0 && Number.isFinite(annual_savings)) {
    const m = (net_investment / annual_savings) * 12;
    payback_months = Number.isFinite(m) ? m : null;
  }
  return { annual_labour, annual_savings, investment, grant, net_investment, payback_months };
}

/** automated_share from the result; recomputed from the steps only if the server left it out. */
export function automatedShare(result) {
  if (Number.isFinite(result?.automated_share)) return result.automated_share;
  const cycle = Number(result?.cycle_time_s);
  if (!(cycle > 0)) return 0;
  const auto = (result.steps || [])
    .filter((s) => s.verdict === 'automate')
    .reduce((sum, s) => sum + (Number(s.mean_duration_s) || 0), 0);
  return auto / cycle;
}

/**
 * Where a follower clip should be for a given master (cam0) time.
 * offset_s is the cam0 time at which the follower's clip starts, so its own time is
 * master - offset, clamped at 0. `hold` is true while the clip has not started yet
 * (or has run out), when the follower should sit still instead of playing.
 */
export function followerTarget(masterT, offset_s, duration_s) {
  const t = masterT - (Number(offset_s) || 0);
  if (!(t >= 0)) return { t: 0, hold: true };
  if (duration_s > 0 && t >= duration_s) return { t: duration_s, hold: true };
  return { t, hold: false };
}

/**
 * Drift correction decision for one follower.
 * drift_s = follower time - where it should be (positive: follower is ahead).
 * baseRate is the master's playback rate (the speed control).
 *   |drift| < 15 ms      -> none  (play at baseRate)
 *   15 ms <= |drift| <= 80 ms -> nudge (baseRate * 0.95 when ahead, * 1.05 when behind)
 *   |drift| > 80 ms      -> seek
 */
export function driftAction(drift_s, baseRate = 1) {
  const a = Math.abs(drift_s);
  if (!Number.isFinite(a)) return { action: 'seek', rate: baseRate };
  if (a < DRIFT_LEAVE_S) return { action: 'none', rate: baseRate };
  if (a <= DRIFT_SEEK_S) {
    return { action: 'nudge', rate: baseRate * (drift_s > 0 ? 1 - NUDGE : 1 + NUDGE) };
  }
  return { action: 'seek', rate: baseRate };
}

/**
 * A playing video lands behind its target after a hard seek, by about the time the
 * seek took. Seeking `lead` seconds ahead compensates; this adapts the lead from the
 * drift measured on the first frame after a seek (negative drift = landed behind).
 */
export function nextSeekLead(lead, postSeekDrift) {
  if (!Number.isFinite(postSeekDrift)) return lead;
  return clamp(lead - 0.5 * postSeekDrift, 0, 0.25);
}

/**
 * Timeline layout: one segment per occurrence, positioned over the cam0 duration.
 * Returns { segments, lanes }. Each segment has left_pct/width_pct (0-100) and a lane,
 * so overlapping occurrences sit on separate rows instead of hiding each other.
 */
export function layoutTimeline(steps, duration_s) {
  if (!(duration_s > 0) || !Array.isArray(steps)) return { segments: [], lanes: 1 };
  const segments = [];
  steps.forEach((step, step_index) => {
    for (const occ of step.occurrences || []) {
      const start_s = clamp(Number(occ.start_s), 0, duration_s);
      const end_s = clamp(Number(occ.end_s), 0, duration_s);
      if (!(end_s > start_s)) continue;
      segments.push({
        step_id: step.id,
        step_index,
        name: step.name,
        verdict: step.verdict,
        preflagged: Boolean(step.preflagged),
        cycle: occ.cycle,
        start_s,
        end_s,
        left_pct: (start_s / duration_s) * 100,
        width_pct: ((end_s - start_s) / duration_s) * 100,
      });
    }
  });
  segments.sort((a, b) => a.start_s - b.start_s || a.step_index - b.step_index);
  const laneEnds = [];
  for (const seg of segments) {
    let lane = laneEnds.findIndex((end) => end <= seg.start_s + 1e-9);
    if (lane === -1) {
      lane = laneEnds.length;
      laneEnds.push(seg.end_s);
    } else {
      laneEnds[lane] = seg.end_s;
    }
    seg.lane = lane;
  }
  return { segments, lanes: Math.max(1, laneEnds.length) };
}

/** Index of the segment under the playhead (latest-starting one if several), or -1. */
export function activeSegmentIndex(segments, t) {
  let found = -1;
  for (let i = 0; i < segments.length; i++) {
    const s = segments[i];
    if (s.start_s > t) break;
    if (t < s.end_s) found = i;
  }
  return found;
}

/** Cycle shown by the transport: the active occurrence's, else the last one started; null before any. */
export function cycleAt(segments, t) {
  const i = activeSegmentIndex(segments, t);
  if (i !== -1) return segments[i].cycle ?? null;
  let cycle = null;
  for (const s of segments) {
    if (s.start_s > t) break;
    cycle = s.cycle ?? cycle;
  }
  return cycle;
}

export function totalCycles(result, segments = []) {
  if (Number(result?.cycles) > 0) return Number(result.cycles);
  return segments.reduce((m, s) => Math.max(m, Number(s.cycle) || 0), 0);
}

/**
 * Previous/next step start for the transport buttons. dir > 0: first start after t.
 * dir < 0: last start more than 0.5 s before t (so a double press walks back), or 0.
 */
export function adjacentStart(segments, t, dir) {
  const starts = [...new Set(segments.map((s) => s.start_s))].sort((a, b) => a - b);
  if (dir > 0) return starts.find((s) => s > t + 0.05) ?? null;
  const before = starts.filter((s) => s < t - 0.5);
  return before.length ? before[before.length - 1] : 0;
}

function normCounts(counts) {
  const n = (k) => Math.max(0, Math.round(Number(counts?.[k]) || 0));
  return { automate: n('automate'), keep_human: n('keep_human'), needs_fixture: n('needs_fixture'), total: n('total') };
}

/** Banner pieces: "N of M steps can be automated · X stay human · Y need a fixture". */
export function bannerPieces(counts) {
  const c = normCounts(counts);
  return [
    { text: `${c.automate} of ${c.total}`, kind: 'automate' },
    { text: ` step${c.total === 1 ? '' : 's'} can be automated · ` },
    { text: String(c.keep_human), kind: 'keep_human' },
    { text: c.keep_human === 1 ? ' stays human · ' : ' stay human · ' },
    { text: String(c.needs_fixture), kind: 'needs_fixture' },
    { text: c.needs_fixture === 1 ? ' needs a fixture' : ' need a fixture' },
  ];
}

export function bannerText(counts) {
  return bannerPieces(counts).map((p) => p.text).join('');
}

/** Sign-off tally over the result's steps (sign-offs for unknown step ids are ignored). */
export function signoffSummary(steps, signoffs) {
  let approved = 0;
  let flagged = 0;
  for (const s of steps || []) {
    const d = signoffs?.[s.id]?.decision;
    if (d === 'approved') approved++;
    else if (d === 'flagged') flagged++;
  }
  return { approved, flagged, total: (steps || []).length };
}

export function engineerLine(steps, signoffs) {
  const { approved, flagged, total } = signoffSummary(steps, signoffs);
  return `Engineer: ${approved} of ${total} approved${flagged ? ` · ${flagged} flagged` : ''}`;
}

/** Progress chips for a live run: [{name, state: done | run | failed | todo}]. */
export function stageStates(status, stage) {
  if (status === 'done' || stage === 'done') return STAGES.map((name) => ({ name, state: 'done' }));
  const idx = STAGES.indexOf(stage);
  return STAGES.map((name, i) => ({
    name,
    state: idx === -1 ? 'todo' : i < idx ? 'done' : i > idx ? 'todo' : status === 'failed' ? 'failed' : 'run',
  }));
}

/** 75.3 -> "01:15.3" */
export function formatTime(s) {
  if (!Number.isFinite(s)) return '--:--.-';
  const tenths = Math.round(Math.max(0, s) * 10);
  const m = Math.floor(tenths / 600);
  const rest = (tenths % 600) / 10;
  return `${String(m).padStart(2, '0')}:${rest.toFixed(1).padStart(4, '0')}`;
}

/** 168 -> "2:48" */
export function formatClock(s) {
  if (!Number.isFinite(s)) return '–';
  const total = Math.round(Math.max(0, s));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
}

export function formatMoney(n, currency = 'SGD') {
  if (!Number.isFinite(n)) return '–';
  return `${currency} ${Math.round(n).toLocaleString('en-US')}`;
}

/* ------------------------------------------------------------------ cycle */

/** Where the measured cycle goes: one item per step (mean duration, share) plus the unassigned rest. */
export function cycleBreakdown(result) {
  const cycle = Number(result?.cycle_time_s);
  const ok = cycle > 0;
  let used = 0;
  const items = (result?.steps || []).map((s, i) => {
    const seconds = Math.max(0, Number(s.mean_duration_s) || 0);
    used += seconds;
    return { step_id: s.id, index: i, name: s.name, verdict: s.verdict, seconds, pct: ok ? (seconds / cycle) * 100 : 0 };
  });
  const unassigned_s = ok ? Math.max(0, cycle - used) : 0;
  return { cycle_s: ok ? cycle : 0, items, unassigned_s, unassigned_pct: ok ? (unassigned_s / cycle) * 100 : 0 };
}

/**
 * The robot cycle estimate, the same arithmetic as app/catalogue.py so the page can
 * recompute it when the engineer changes the speed factor:
 *   robot    = (sum of automate step means) * factor
 *   operator = sum of the other step means
 *   parallel = max(robot, operator + unassigned)
 * Returns null without a measured cycle or a usable factor.
 */
export function robotCycle(result, factor) {
  const f = Number(factor);
  const cycle = Number(result?.cycle_time_s);
  if (!(cycle > 0) || !Number.isFinite(f) || f <= 0) return null;
  let auto = 0;
  let other = 0;
  for (const s of result.steps || []) {
    const d = Math.max(0, Number(s.mean_duration_s) || 0);
    if (s.verdict === 'automate') auto += d; else other += d;
  }
  const unassigned_s = Math.max(0, cycle - auto - other);
  const robot_cycle_s = auto * f;
  return {
    human_cycle_s: cycle, automated_steps_s: auto, robot_cycle_s, operator_cycle_s: other, unassigned_s,
    parallel_cycle_s: Math.max(robot_cycle_s, other + unassigned_s), speed_factor: f,
  };
}

/* --------------------------------------------------------------- evidence */

export const EVIDENCE_WINDOW_S = 0.75;

/** Reasons with a box whose time is within `win` seconds of t: what to draw over the videos now. */
export function evidenceAt(steps, t, win = EVIDENCE_WINDOW_S) {
  const out = [];
  (steps || []).forEach((s, index) => {
    for (const r of s.reasons || []) {
      const box = Array.isArray(r.box) && r.box.length === 4 ? r.box.map(Number) : null;
      if (!box || box.some((v) => !Number.isFinite(v))) continue;
      const ts = Number(r.t_s);
      if (!Number.isFinite(ts) || Math.abs(ts - t) > win) continue;
      out.push({ view: r.view, box, step_id: s.id, index, name: s.name, verdict: s.verdict, text: r.text, t_s: ts });
    }
  });
  return out;
}

/** The rectangle the video frame occupies inside a `contain`-fitted element (letterboxing). */
export function contentRect(elW, elH, vidW, vidH) {
  if (!(vidW > 0) || !(vidH > 0) || !(elW > 0) || !(elH > 0)) return { x: 0, y: 0, w: elW || 0, h: elH || 0 };
  const scale = Math.min(elW / vidW, elH / vidH);
  const w = vidW * scale;
  const h = vidH * scale;
  return { x: (elW - w) / 2, y: (elH - h) / 2, w, h };
}

/** A [ymin, xmin, ymax, xmax] box on the 0-1000 grid -> pixel rectangle inside `rect`. */
export function boxRect(box, rect) {
  const [y0, x0, y1, x1] = box.map((v) => clamp(Number(v) / 1000, 0, 1));
  return {
    left: rect.x + x0 * rect.w, top: rect.y + y0 * rect.h,
    width: Math.max(0, x1 - x0) * rect.w, height: Math.max(0, y1 - y0) * rect.h,
  };
}

/* ----------------------------------------------------------------- layout */

export const OPERATOR_RADIUS_MM = 225; // an adult's shoulder width is about 0.45 m
export const SCENE_PAD_MM = 120;
export const REACH_OVERHANG_MM = 300;  // how far past the bench the reach circle may widen the sketch

/**
 * Top-view scene in millimetres for the SVG, from the result's layout (bench frame: x to the
 * operator's right, y away from the operator) and the chosen robot's reach. SVG y points down,
 * so scene y = -bench y: the bench sits above the operator on screen.
 * Returns null without a layout.
 */
export function layoutScene(layout, reach_mm) {
  if (!layout?.bench) return null;
  const mm = (m) => Math.round((Number(m) || 0) * 1000);
  const L = Math.max(1, mm(layout.bench.length_m));
  const W = Math.max(1, mm(layout.bench.width_m));
  const bench = { x: 0, y: -W, w: L, h: W };
  const op = layout.operator || {};
  const operator = { cx: mm(op.x_m ?? layout.bench.length_m / 2), cy: -mm(op.y_m ?? -0.35), r: OPERATOR_RADIUS_MM };
  const objects = (layout.objects || []).map((o) => ({
    name: o.name, kind: o.kind, step_ids: o.step_ids || [], confidence: Number(o.confidence) || 0,
    x: mm(o.x_m) - mm(o.length_m) / 2, y: -mm(o.y_m) - mm(o.width_m) / 2,
    w: Math.max(10, mm(o.length_m)), h: Math.max(10, mm(o.width_m)),
  }));
  const anchors = (layout.anchors || []).map((a) => ({
    cx: mm(a.x_m), cy: -mm(a.y_m), kind: a.kind, verdict: a.verdict, step_id: a.step_id, name: a.name,
  }));
  const arrows = [];
  const byStep = new Map();
  for (const a of anchors) {
    const e = byStep.get(a.step_id) || {};
    e[a.kind] = a;
    e.verdict = a.verdict;
    e.name = a.name;
    byStep.set(a.step_id, e);
  }
  for (const [step_id, e] of byStep) {
    if (e.pick && e.place) {
      arrows.push({ step_id, name: e.name, verdict: e.verdict, x1: e.pick.cx, y1: e.pick.cy, x2: e.place.cx, y2: e.place.cy });
    }
  }
  let base = null;
  let reach = null;
  let needed = null;
  if (layout.robot_base) {
    base = { cx: mm(layout.robot_base.x_m), cy: -mm(layout.robot_base.y_m) };
    if (Number(reach_mm) > 0) reach = { ...base, r: Math.round(Number(reach_mm)) };
    if (Number(layout.needed_reach_m) > 0) needed = { ...base, r: mm(layout.needed_reach_m) };
  }
  // Bounds over everything drawn, plus padding. The reach circles may run off the sketch
  // (they are clipped by the viewBox) so a long arm does not shrink the bench to a stamp.
  let minX = bench.x; let minY = bench.y; let maxX = bench.x + bench.w; let maxY = bench.y + bench.h;
  const grow = (x0, y0, x1, y1) => { minX = Math.min(minX, x0); minY = Math.min(minY, y0); maxX = Math.max(maxX, x1); maxY = Math.max(maxY, y1); };
  grow(operator.cx - operator.r, operator.cy - operator.r, operator.cx + operator.r, operator.cy + operator.r);
  for (const o of objects) grow(o.x, o.y, o.x + o.w, o.y + o.h);
  for (const a of anchors) grow(a.cx - 40, a.cy - 40, a.cx + 40, a.cy + 40);
  for (const c of [reach, needed]) {
    if (!c) continue;
    grow(Math.max(c.cx - c.r, bench.x - REACH_OVERHANG_MM), Math.max(c.cy - c.r, bench.y - REACH_OVERHANG_MM),
      Math.min(c.cx + c.r, bench.x + bench.w + REACH_OVERHANG_MM), Math.min(c.cy + c.r, bench.y + bench.h + REACH_OVERHANG_MM));
  }
  const viewBox = [minX - SCENE_PAD_MM, minY - SCENE_PAD_MM, maxX - minX + 2 * SCENE_PAD_MM, maxY - minY + 2 * SCENE_PAD_MM];
  return { bench, operator, objects, anchors, arrows, base, reach, needed, viewBox };
}

/** "1.2 × 0.6 m" */
export function formatMetres(a, b) {
  const f = (x) => (Number.isFinite(x) ? (Math.round(x * 100) / 100).toString() : '?');
  return b == null ? `${f(a)} m` : `${f(a)} × ${f(b)} m`;
}

/* ---------------------------------------------------------------- capture */

export const TAKE_STAGES = ['armed', 'recording', 'stopped', 'offloaded'];

/**
 * The offset check as the capture page draws it, from the bridge's check object
 * ({delays_ms: {node: ms}, fps, ref, order}). One row per camera in cam0.. order: its delay
 * after the RF edge, its offset against the reference (cam0), and a bar width in % of the
 * longer of the longest delay and one frame. `ok` when the spread is within one frame.
 */
export function offsetRows(check) {
  const delays = check?.delays_ms || {};
  const ids = Object.keys(delays).filter((k) => Number.isFinite(Number(delays[k])));
  const fps = Number(check?.fps) > 0 ? Number(check.fps) : 30;
  const frame_ms = 1000 / fps;
  if (!ids.length) return { rows: [], ref: null, spread_ms: 0, frame_ms, frame_pct: 100, ok: false };
  const d = (id) => Number(delays[id]);
  const ref = ids.includes(check?.ref) ? check.ref : ids.reduce((a, b) => (d(b) < d(a) ? b : a));
  const rest = ids.filter((id) => id !== ref).sort((a, b) => d(a) - d(b) || a.localeCompare(b));
  const given = Array.isArray(check?.order) && check.order.length === ids.length && check.order.every((id) => ids.includes(id));
  const order = given ? check.order : [ref, ...rest];
  const min = Math.min(...ids.map(d));
  const max = Math.max(...ids.map(d));
  const scale = Math.max(max, frame_ms, 1e-9);
  const rows = order.map((id, i) => ({
    id, view: `cam${i}`, delay_ms: d(id), offset_ms: d(id) - d(ref), pct: (d(id) / scale) * 100,
  }));
  const spread_ms = max - min;
  return { rows, ref, spread_ms, frame_ms, frame_pct: (frame_ms / scale) * 100, ok: spread_ms <= frame_ms + 1e-6 };
}

/** "+9.6 ms" (always signed, one decimal). */
export function formatMs(ms) {
  if (!Number.isFinite(ms)) return '–';
  const v = Math.round(ms * 10) / 10;
  return `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(1)} ms`;
}

/**
 * Chips for a take: armed → recording → stopped → offloaded, each done | run | failed | todo.
 * `take` is the bridge's take record (state: arming | armed | recording | stopped | failed).
 */
export function takeStages(take) {
  const mk = (states) => TAKE_STAGES.map((name, i) => ({ name, state: states[i] }));
  if (!take) return mk(['todo', 'todo', 'todo', 'todo']);
  switch (take.state) {
    case 'arming': return mk(['run', 'todo', 'todo', 'todo']);
    case 'armed': return mk(['done', 'run', 'todo', 'todo']);
    case 'recording': return mk(['done', 'run', 'todo', 'todo']);
    case 'stopped':
      return mk(['done', 'done', 'done', take.offloaded ? 'done' : take.check_error ? 'failed' : 'run']);
    case 'failed': {
      const started = Object.values(take.cameras || {}).some((c) => c && c.started);
      if (!take.fired_at) return mk(['failed', 'todo', 'todo', 'todo']);
      if (!started) return mk(['done', 'failed', 'todo', 'todo']);
      return mk(['done', 'done', 'failed', 'todo']);
    }
    default: return mk(['todo', 'todo', 'todo', 'todo']);
  }
}

/** One line per camera of a take: "rx-cam1 · recording", "rx-cam2 · stopped · 10.2 s · 306 frames". */
export function takeCameraLines(take) {
  const cams = take?.cameras || {};
  const ids = [...new Set([...(take?.armed || []), ...Object.keys(cams)])];
  const lines = ids.map((id) => {
    const c = cams[id] || {};
    const bits = [id];
    if (c.stopped) {
      bits.push('stopped');
      if (Number.isFinite(Number(c.duration_s))) bits.push(`${Number(c.duration_s).toFixed(1)} s`);
      if (Number.isFinite(Number(c.frames))) bits.push(`${c.frames} frames`);
    } else if (c.started) bits.push('recording');
    else bits.push(take?.state === 'failed' ? 'did not start' : 'armed, waiting for the trigger');
    return { id, text: bits.join(' · '), kind: c.stopped ? 'done' : c.started ? 'run' : 'todo' };
  });
  for (const [id, why] of Object.entries(take?.not_armed || {})) {
    lines.push({ id, text: `${id} · not armed: ${why}`, kind: 'failed' });
  }
  return lines;
}

/** "4 bars, charging" / "2 of 4 bars" / "no battery reading". */
export function batteryText(b) {
  if (!b || !Number.isFinite(Number(b.bars))) return 'no battery reading';
  const bars = Math.max(0, Math.min(4, Math.round(Number(b.bars))));
  const text = bars === 4 && b.full ? 'full' : `${bars} of 4 bars`;
  return b.charging ? `${text}, charging` : text;
}

/** The badge for a camera card: {text, kind: ok | warn | bad}. */
export function cameraBadge(c) {
  if (!c) return { text: 'unknown', kind: 'bad' };
  if (!c.online) return { text: 'offline', kind: 'bad' };
  if (c.state === 'recording') return { text: 'recording', kind: 'warn' };
  if (c.armed) return { text: 'armed', kind: 'warn' };
  if (c.state === 'live') return { text: 'live', kind: 'ok' };
  if (c.state === 'configuring') return { text: 'configuring', kind: 'warn' };
  if (c.state === 'error') return { text: 'camera error', kind: 'bad' };
  return { text: c.state ? String(c.state) : 'connecting', kind: 'warn' };
}

/** A session id from the take name and a date: "kitting-20261015". */
export function suggestSessionId(name, date = new Date()) {
  const stem = String(name || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40) || 'take';
  const d = date instanceof Date && !Number.isNaN(date.getTime()) ? date : new Date();
  const ymd = `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, '0')}${String(d.getDate()).padStart(2, '0')}`;
  return `${stem}-${ymd}`;
}

/** How far a recording take is through its seconds, 0..1 (1 once stopped). */
export function takeProgress(take, nowS) {
  if (!take) return 0;
  if (take.state === 'stopped') return 1;
  if (take.state !== 'recording' || !Number.isFinite(Number(take.started_at)) || !(Number(take.seconds) > 0)) return 0;
  const elapsed = nowS - Number(take.started_at);
  return Math.max(0, Math.min(1, elapsed / Number(take.seconds)));
}

/* ------------------------------------------------------------- front door */

/** The study the landing page opens first: a real sample before a synthetic one, then anything. */
export function featuredSession(sessions) {
  const list = Array.isArray(sessions) ? sessions : [];
  return list.find((s) => s.kind === 'sample' && !s.synthetic)
    || list.find((s) => s.kind === 'sample')
    || list.find((s) => !s.synthetic)
    || list[0] || null;
}

/**
 * The four headline results of a study, for the tiles under the banner: the steps a cobot could
 * take, the arm that fits, the robot cycle, and the payback. `factor` overrides the speed factor
 * and `roi` the payback inputs; both default to the result's own values.
 */
export function summaryTiles(result, { factor, roi } = {}) {
  const r = result || {};
  const c = normCounts(r.counts);
  const tiles = [{
    key: 'steps',
    kind: c.automate > 0 ? 'automate' : (c.total ? 'keep_human' : null),
    value: `${c.automate} of ${c.total}`,
    label: c.total === 1 ? 'step a cobot could take' : 'steps a cobot could take',
    detail: `${c.needs_fixture} with a fixture · ${c.keep_human} stay human`,
  }];
  const rb = r.robot;
  if (rb && rb.model) {
    const spec = [
      Number.isFinite(Number(rb.payload_kg)) ? `${rb.payload_kg} kg payload` : null,
      Number.isFinite(Number(rb.reach_mm)) ? `${rb.reach_mm} mm reach` : null,
    ].filter(Boolean).join(' · ');
    tiles.push({
      key: 'robot', kind: rb.fits === false ? 'keep_human' : 'automate', value: rb.model,
      label: rb.fits === false ? 'closest arm, but it does not fit' : 'smallest arm that fits', detail: spec,
    });
  } else {
    tiles.push({ key: 'robot', kind: null, value: '–', label: 'no arm chosen', detail: '' });
  }
  const f0 = Number(r.cycle_estimate?.speed_factor);
  const f = Number.isFinite(Number(factor)) && Number(factor) > 0 ? Number(factor) : (Number.isFinite(f0) ? f0 : 1.3);
  const cy = robotCycle(r, f);
  tiles.push(cy ? {
    key: 'cycle', kind: null, value: `${cy.parallel_cycle_s.toFixed(1)} s`,
    label: 'cycle with a cobot, estimated', detail: `${cy.human_cycle_s.toFixed(1)} s by hand today`,
  } : { key: 'cycle', kind: null, value: '–', label: 'no measured cycle', detail: '' });
  const inputs = roi || r.roi_defaults;
  const p = inputs ? computePayback(inputs, automatedShare(r)) : null;
  tiles.push(p && p.payback_months != null ? {
    key: 'payback', kind: 'automate', value: `≈ ${Math.round(p.payback_months)} months`,
    label: 'payback at the default inputs', detail: `grant ${formatMoney(p.grant, inputs.currency || 'SGD')} applied`,
  } : { key: 'payback', kind: null, value: '–', label: 'no payback figure', detail: '' });
  return tiles;
}

/* ------------------------------------------------------------ engineer view */

/** Counts for the step filter chips: every step, by verdict, and the ones the check pass flagged. */
export function verdictFilterCounts(steps) {
  const c = { all: 0, automate: 0, needs_fixture: 0, keep_human: 0, flagged: 0 };
  for (const s of steps || []) {
    c.all++;
    if (s.verdict in c) c[s.verdict]++;
    if (s.preflagged) c.flagged++;
  }
  return c;
}

export function stepMatchesFilter(step, filter) {
  if (!filter || filter === 'all') return true;
  if (filter === 'flagged') return Boolean(step?.preflagged);
  return step?.verdict === filter;
}

/**
 * The proposed cell as two lanes over one cycle: what the cobot does (automate steps × the speed factor)
 * and what the operator keeps (the other steps plus the time no step covers). Percentages are of the
 * longer lane, the side-by-side cycle. Null without a measured cycle.
 */
export function cellLanes(result, factor) {
  const est = robotCycle(result, factor);
  if (!est) return null;
  const total = Math.max(est.parallel_cycle_s, 1e-6);
  const robot = [];
  const operator = [];
  (result.steps || []).forEach((s, index) => {
    const d = Math.max(0, Number(s.mean_duration_s) || 0);
    if (s.verdict === 'automate') {
      const seconds = d * est.speed_factor;
      robot.push({ index, name: s.name, verdict: s.verdict, seconds, pct: (seconds / total) * 100 });
    } else {
      operator.push({ index, name: s.name, verdict: s.verdict, seconds: d, pct: (d / total) * 100 });
    }
  });
  if (est.unassigned_s > 1e-6) {
    operator.push({ index: -1, name: 'not covered by a step', verdict: 'unassigned', seconds: est.unassigned_s,
      pct: (est.unassigned_s / total) * 100 });
  }
  const operator_s = est.operator_cycle_s + est.unassigned_s;
  return {
    total_s: est.parallel_cycle_s, human_cycle_s: est.human_cycle_s, speed_factor: est.speed_factor,
    robot, operator, robot_s: est.robot_cycle_s, operator_s,
    robot_idle_pct: Math.max(0, (1 - est.robot_cycle_s / total) * 100),
    operator_idle_pct: Math.max(0, (1 - operator_s / total) * 100),
  };
}

/** The engineer's notes by step id (an empty map without notes). */
export function notesByStep(notes) {
  const m = new Map();
  for (const n of notes?.step_notes || []) if (n && n.id) m.set(n.id, n);
  return m;
}

/** What the bridge is wired to: 'console' behind the emulator console, 'emulator' when every camera address is localhost, 'live' otherwise, 'none' without cameras. */
export function rigKind(cameras, console = null) {
  if (console && console.url) return 'console';
  const list = (cameras || []).map((c) => String(c?.url || ''));
  if (!list.length) return 'none';
  const host = (u) => { try { return new URL(u).hostname.toLowerCase(); } catch { return ''; } };
  const loopback = (hst) => hst === 'localhost' || hst === '127.0.0.1' || hst === '::1' || hst === '[::1]';
  return list.every((u) => loopback(host(u))) ? 'emulator' : 'live';
}

/**
 * Keep the layout sketch's labels legible. Each label is {x, y, w, anchor, movable}: x, y its SVG
 * position in scene mm (y the baseline), w its estimated width, anchor 'middle' or 'start'. Every
 * label is clamped into the viewBox; a movable one is nudged down a line at a time when it would
 * overlap a label placed before it (fixed labels first, then top to bottom). Mutates and returns labels.
 */
export function placeLabels(labels, viewBox, fs) {
  const [vx, vy, vw, vh] = viewBox;
  const pad = fs * 0.2;
  const box = (l) => {
    const x1 = l.anchor === 'start' ? l.x : l.x - l.w / 2;
    return { x1, x2: x1 + l.w, y1: l.y - fs * 0.85, y2: l.y + fs * 0.3 };
  };
  const hits = (a, b) => a.x1 < b.x2 && b.x1 < a.x2 && a.y1 < b.y2 && b.y1 < a.y2;
  const clampX = (l) => {
    const half = l.anchor === 'start' ? 0 : l.w / 2;
    l.x = Math.min(Math.max(l.x, vx + pad + half), vx + vw - pad - (l.anchor === 'start' ? l.w : half));
  };
  const placed = [];
  const order = [...labels.filter((l) => !l.movable), ...labels.filter((l) => l.movable).sort((a, b) => a.y - b.y || a.x - b.x)];
  for (const l of order) {
    clampX(l);
    if (l.movable) {
      for (let n = 0; n < 6 && placed.some((p) => hits(box(l), p)); n++) l.y += fs * 1.2;
      l.y = Math.min(l.y, vy + vh - fs * 0.3);
    }
    placed.push(box(l));
  }
  return labels;
}
