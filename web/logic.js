// CellPilot page logic: pure functions, no DOM.
// Imported by app.js in the browser and by logic.test.mjs under `node --test`.

export const STAGES = ['steps', 'verifier', 'robot', 'payback'];

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
