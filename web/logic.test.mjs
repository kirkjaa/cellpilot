// Run: node --test web/logic.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  computePayback, automatedShare, driftAction, followerTarget, nextSeekLead,
  layoutTimeline, activeSegmentIndex, cycleAt, totalCycles, adjacentStart,
  bannerText, engineerLine, signoffSummary, stageStates, formatTime, formatClock, formatMoney,
  cycleBreakdown, robotCycle, evidenceAt, contentRect, boxRect, layoutScene, formatMetres, STAGES,
  offsetRows, formatMs, takeStages, takeCameraLines, batteryText, cameraBadge, suggestSessionId, takeProgress, TAKE_STAGES,
  featuredSession, summaryTiles, verdictFilterCounts, stepMatchesFilter, cellLanes, notesByStep,
} from './logic.js';
import { SCENARIOS, RECORDING_RULES, scenarioById } from './scenarios.js';

const near = (a, b, eps = 1e-9) => assert.ok(Math.abs(a - b) < eps, `${a} != ${b}`);

// The roi_defaults example from docs/contract.md.
const ROI = {
  currency: 'SGD', wage_per_h: 14.0, shifts: 2, hours_per_shift: 8, days_per_year: 250,
  robot_price: 40000, system_multiple: 3.0, grant_share: 0.5, grant_cap: 30000,
};

test('payback follows the contract formula (grant capped)', () => {
  const p = computePayback(ROI, 0.55);
  assert.equal(p.annual_labour, 56000);
  near(p.annual_savings, 30800);
  assert.equal(p.investment, 120000);
  assert.equal(p.grant, 30000); // min(0.5 * 120000, 30000)
  assert.equal(p.net_investment, 90000);
  near(p.payback_months, (90000 / 30800) * 12);
});

test('payback uses the grant share when it is below the cap', () => {
  const p = computePayback({ ...ROI, grant_share: 0.2 }, 0.5);
  assert.equal(p.grant, 24000);
  near(p.payback_months, ((120000 - 24000) / 28000) * 12);
});

test('payback is null when annual savings are zero', () => {
  assert.equal(computePayback(ROI, 0).payback_months, null);
  assert.equal(computePayback({ ...ROI, wage_per_h: 0 }, 0.5).payback_months, null);
});

test('payback is null when an input is not a number', () => {
  assert.equal(computePayback({ ...ROI, robot_price: NaN }, 0.5).payback_months, null);
  assert.equal(computePayback({ ...ROI, shifts: undefined }, 0.5).payback_months, null);
});

test('payback accepts numeric strings from input fields', () => {
  const p = computePayback({ ...ROI, wage_per_h: '14', robot_price: '40000' }, 0.55);
  near(p.payback_months, (90000 / 30800) * 12);
});

test('automated share comes from the result, else from automate steps over cycle time', () => {
  assert.equal(automatedShare({ automated_share: 0.55 }), 0.55);
  const r = {
    cycle_time_s: 10,
    steps: [
      { verdict: 'automate', mean_duration_s: 2 },
      { verdict: 'keep_human', mean_duration_s: 5 },
      { verdict: 'automate', mean_duration_s: 3 },
    ],
  };
  near(automatedShare(r), 0.5);
  assert.equal(automatedShare({ steps: [] }), 0);
});

test('drift under 15 ms is left alone', () => {
  assert.deepEqual(driftAction(0, 1), { action: 'none', rate: 1 });
  assert.deepEqual(driftAction(0.0149, 1), { action: 'none', rate: 1 });
  assert.deepEqual(driftAction(-0.0149, 2), { action: 'none', rate: 2 });
});

test('drift of 15 to 80 ms nudges the rate 5 % toward the master', () => {
  const ahead = driftAction(0.04, 1);
  assert.equal(ahead.action, 'nudge');
  near(ahead.rate, 0.95);
  const behind = driftAction(-0.04, 1);
  assert.equal(behind.action, 'nudge');
  near(behind.rate, 1.05);
  assert.equal(driftAction(0.015, 1).action, 'nudge');
  assert.equal(driftAction(0.08, 1).action, 'nudge');
  near(driftAction(-0.05, 2).rate, 2.1); // relative to the chosen speed
  near(driftAction(0.05, 0.5).rate, 0.475);
});

test('drift over 80 ms seeks', () => {
  assert.deepEqual(driftAction(0.0801, 1), { action: 'seek', rate: 1 });
  assert.deepEqual(driftAction(-0.5, 2), { action: 'seek', rate: 2 });
  assert.equal(driftAction(NaN, 1).action, 'seek');
});

test('follower target is master time minus offset, clamped at 0', () => {
  assert.deepEqual(followerTarget(5, 0.2, 10), { t: 4.8, hold: false });
  assert.deepEqual(followerTarget(0.1, 0.45, 10), { t: 0, hold: true });
  assert.deepEqual(followerTarget(0.45, 0.45, 10), { t: 0, hold: false });
  assert.deepEqual(followerTarget(10.6, 0.45, 10), { t: 10, hold: true });
  assert.deepEqual(followerTarget(3, undefined, NaN), { t: 3, hold: false });
});

test('seek lead adapts toward the seek latency and stays bounded', () => {
  let lead = 0;
  for (let i = 0; i < 20; i++) lead = nextSeekLead(lead, lead - 0.1); // seeks take 100 ms
  near(lead, 0.1, 1e-3);
  assert.equal(nextSeekLead(0, 0.3), 0);
  assert.equal(nextSeekLead(0.2, -1), 0.25);
  assert.equal(nextSeekLead(0.1, NaN), 0.1);
});

const STEPS = [
  { id: 's1', name: 'Pick', verdict: 'automate', preflagged: false,
    occurrences: [{ cycle: 1, start_s: 1, end_s: 3 }, { cycle: 2, start_s: 6, end_s: 8 }] },
  { id: 's2', name: 'Screw', verdict: 'needs_fixture', preflagged: true,
    occurrences: [{ cycle: 1, start_s: 3, end_s: 5 }, { cycle: 2, start_s: 8, end_s: 12 }] },
  { id: 's3', name: 'Check', verdict: 'keep_human', preflagged: false,
    occurrences: [{ cycle: 1, start_s: 4, end_s: 4.5 }, { cycle: 2, start_s: 7, end_s: 7 }] },
];

test('timeline layout positions occurrences over the cam0 duration', () => {
  const { segments, lanes } = layoutTimeline(STEPS, 10);
  // s3 cycle 2 has zero length and is dropped; s2 cycle 2 is clamped to 10 s.
  assert.equal(segments.length, 5);
  assert.deepEqual(segments.map((s) => s.start_s), [1, 3, 4, 6, 8]);
  const first = segments[0];
  assert.equal(first.step_id, 's1');
  near(first.left_pct, 10);
  near(first.width_pct, 20);
  const last = segments[4];
  assert.equal(last.end_s, 10);
  near(last.width_pct, 20);
  assert.equal(segments[1].preflagged, true);
  assert.equal(segments[1].verdict, 'needs_fixture');
  // s3 (4-4.5) overlaps s2 (3-5): it gets a second lane.
  assert.equal(lanes, 2);
  assert.equal(segments[2].lane, 1);
  assert.equal(segments[3].lane, 0);
});

test('timeline layout is empty without a duration', () => {
  assert.deepEqual(layoutTimeline(STEPS, 0), { segments: [], lanes: 1 });
  assert.deepEqual(layoutTimeline(null, 10), { segments: [], lanes: 1 });
  assert.equal(layoutTimeline([{ id: 'x', occurrences: [{ start_s: 'a', end_s: 2 }] }], 10).segments.length, 0);
});

test('active segment, cycle and step navigation follow the playhead', () => {
  const { segments } = layoutTimeline(STEPS, 10);
  assert.equal(activeSegmentIndex(segments, 0.5), -1);
  assert.equal(segments[activeSegmentIndex(segments, 2)].step_id, 's1');
  assert.equal(segments[activeSegmentIndex(segments, 4.2)].step_id, 's3'); // latest start wins
  assert.equal(activeSegmentIndex(segments, 5.5), -1);
  assert.equal(cycleAt(segments, 0.5), null);
  assert.equal(cycleAt(segments, 5.5), 1);
  assert.equal(cycleAt(segments, 6.5), 2);
  assert.equal(totalCycles({ cycles: 9 }, segments), 9);
  assert.equal(totalCycles({}, segments), 2);
  assert.equal(adjacentStart(segments, 2, 1), 3);
  assert.equal(adjacentStart(segments, 8.5, 1), null);
  assert.equal(adjacentStart(segments, 6.2, -1), 4);
  assert.equal(adjacentStart(segments, 1.2, -1), 0);
});

test('banner text', () => {
  assert.equal(
    bannerText({ automate: 4, keep_human: 2, needs_fixture: 1, total: 7 }),
    '4 of 7 steps can be automated · 2 stay human · 1 needs a fixture',
  );
  assert.equal(
    bannerText({ automate: 0, keep_human: 1, needs_fixture: 0, total: 1 }),
    '0 of 1 step can be automated · 1 stays human · 0 need a fixture',
  );
  assert.equal(bannerText(undefined), '0 of 0 steps can be automated · 0 stay human · 0 need a fixture');
});

test('engineer line counts approvals over the result steps only', () => {
  const signoffs = {
    s1: { decision: 'approved' }, s2: { decision: 'flagged' }, s9: { decision: 'approved' },
  };
  assert.deepEqual(signoffSummary(STEPS, signoffs), { approved: 1, flagged: 1, total: 3 });
  assert.equal(engineerLine(STEPS, signoffs), 'Engineer: 1 of 3 approved · 1 flagged');
  assert.equal(engineerLine(STEPS, {}), 'Engineer: 0 of 3 approved');
});

test('stage chips for a live run', () => {
  assert.deepEqual(STAGES, ['steps', 'verifier', 'layout', 'robot', 'payback', 'notes']);
  assert.deepEqual(stageStates('running', 'verifier').map((s) => s.state), ['done', 'run', 'todo', 'todo', 'todo', 'todo']);
  assert.deepEqual(stageStates('queued', 'queued').map((s) => s.state), ['todo', 'todo', 'todo', 'todo', 'todo', 'todo']);
  assert.deepEqual(stageStates('done', 'done').map((s) => s.state), ['done', 'done', 'done', 'done', 'done', 'done']);
  assert.deepEqual(stageStates('failed', 'robot').map((s) => s.state), ['done', 'done', 'done', 'failed', 'todo', 'todo']);
});

test('formatting', () => {
  assert.equal(formatTime(0), '00:00.0');
  assert.equal(formatTime(75.34), '01:15.3');
  assert.equal(formatTime(59.96), '01:00.0');
  assert.equal(formatTime(NaN), '--:--.-');
  assert.equal(formatClock(168), '2:48');
  assert.equal(formatMoney(120000, 'SGD'), 'SGD 120,000');
  assert.equal(formatMoney(NaN), '–');
});

/* ---- additions: cycle, evidence, layout */

const RESULT = {
  cycle_time_s: 10, cycles: 3,
  steps: [
    { id: 's1', name: 'Pick', verdict: 'automate', mean_duration_s: 4,
      reasons: [{ text: 'seen', view: 'cam0', t_s: 1.0, box: [100, 200, 300, 400] }, { text: 'no box', view: 'cam1', t_s: 1.2, box: [] }] },
    { id: 's2', name: 'Screw', verdict: 'keep_human', mean_duration_s: 3,
      reasons: [{ text: 'hands', view: 'cam2', t_s: 6.0, box: [0, 0, 1000, 1000] }] },
    { id: 's3', name: 'Place', verdict: 'needs_fixture', mean_duration_s: 1, reasons: [] },
  ],
};

test('cycle breakdown shares the measured cycle out and keeps the unassigned rest', () => {
  const b = cycleBreakdown(RESULT);
  assert.equal(b.cycle_s, 10);
  assert.deepEqual(b.items.map((i) => [i.step_id, i.seconds, i.pct]), [['s1', 4, 40], ['s2', 3, 30], ['s3', 1, 10]]);
  assert.equal(b.unassigned_s, 2);
  near(b.unassigned_pct, 20);
  const none = cycleBreakdown({ steps: RESULT.steps });
  assert.equal(none.cycle_s, 0);
  assert.ok(none.items.every((i) => i.pct === 0));
});

test('robot cycle matches the server arithmetic and follows the factor', () => {
  const e = robotCycle(RESULT, 1.3);
  assert.equal(e.automated_steps_s, 4);
  near(e.robot_cycle_s, 5.2);
  assert.equal(e.operator_cycle_s, 4);
  assert.equal(e.unassigned_s, 2);
  assert.equal(e.parallel_cycle_s, 6); // max(5.2, 4 + 2)
  near(robotCycle(RESULT, 2).parallel_cycle_s, 8); // the robot becomes the bottleneck
  assert.equal(robotCycle(RESULT, 0), null);
  assert.equal(robotCycle(RESULT, 'x'), null);
  assert.equal(robotCycle({ steps: [] }, 1.3), null);
});

test('evidence near the playhead, only reasons with a box', () => {
  const at1 = evidenceAt(RESULT.steps, 1.1);
  assert.deepEqual(at1.map((e) => [e.view, e.step_id, e.verdict]), [['cam0', 's1', 'automate']]);
  assert.deepEqual(at1[0].box, [100, 200, 300, 400]);
  assert.equal(evidenceAt(RESULT.steps, 2.0).length, 0); // 1.0 is more than 0.75 s away
  assert.equal(evidenceAt(RESULT.steps, 6.7).length, 1);
  assert.equal(evidenceAt(RESULT.steps, 6.7, 0.5).length, 0);
  assert.equal(evidenceAt(null, 1).length, 0);
});

test('box rectangles account for letterboxing', () => {
  // a 16:9 video in a 400 x 300 element: 400 x 225 centred vertically
  const rect = contentRect(400, 300, 1280, 720);
  assert.deepEqual(rect, { x: 0, y: 37.5, w: 400, h: 225 });
  const b = boxRect([100, 200, 300, 400], rect);
  near(b.left, 80); near(b.top, 37.5 + 22.5); near(b.width, 80); near(b.height, 45);
  assert.deepEqual(contentRect(400, 225, 0, 0), { x: 0, y: 0, w: 400, h: 225 }); // no metadata yet: whole element
  const clamped = boxRect([-50, 0, 2000, 1000], { x: 0, y: 0, w: 100, h: 100 });
  assert.deepEqual(clamped, { left: 0, top: 0, width: 100, height: 100 });
});

const LAYOUT = {
  bench: { length_m: 1.2, width_m: 0.6 }, operator: { x_m: 0.6, y_m: -0.35 },
  objects: [{ name: 'bin A', kind: 'bin', x_m: 0.2, y_m: 0.4, length_m: 0.2, width_m: 0.2, step_ids: ['s1'], confidence: 0.7 }],
  anchors: [
    { step_id: 's1', name: 'Pick', verdict: 'automate', kind: 'pick', x_m: 0.2, y_m: 0.4 },
    { step_id: 's1', name: 'Pick', verdict: 'automate', kind: 'place', x_m: 0.6, y_m: 0.2 },
    { step_id: 's2', name: 'Screw', verdict: 'keep_human', kind: 'pick', x_m: 0.9, y_m: 0.1 },
  ],
  robot_base: { x_m: 0.6, y_m: 0.6, rule: 'back edge' }, needed_reach_m: 0.526,
};

test('layout scene flips y so the bench sits above the operator and pairs pick with place', () => {
  const sc = layoutScene(LAYOUT, 700);
  assert.deepEqual(sc.bench, { x: 0, y: -600, w: 1200, h: 600 });
  assert.deepEqual(sc.operator, { cx: 600, cy: 350, r: 225 });
  assert.deepEqual(sc.objects[0], { name: 'bin A', kind: 'bin', step_ids: ['s1'], confidence: 0.7, x: 100, y: -500, w: 200, h: 200 });
  assert.equal(sc.anchors.length, 3);
  assert.deepEqual(sc.arrows, [{ step_id: 's1', name: 'Pick', verdict: 'automate', x1: 200, y1: -400, x2: 600, y2: -200 }]);
  assert.deepEqual(sc.base, { cx: 600, cy: -600 });
  assert.deepEqual(sc.reach, { cx: 600, cy: -600, r: 700 });
  assert.deepEqual(sc.needed, { cx: 600, cy: -600, r: 526 });
  // the view box covers the bench, the operator and the reach circle, which is let run at most
  // 300 mm past the bench: here it spans x -100..1300 and y -1300 (clamped to -900)..100
  const [x, y, w, h] = sc.viewBox;
  assert.ok(x <= -100 - 120 && x + w >= 1300 + 120, `x ${x} w ${w}`);
  assert.ok(y <= -900 - 120 && y + h >= 575 + 120, `y ${y} h ${h}`);
  assert.ok(y > -1300, 'the circle does not stretch the sketch to its full radius');
  assert.equal(layoutScene(null, 700), null);
  assert.equal(layoutScene({ bench: { length_m: 1, width_m: 1 } }, 0).reach, null);
});

test('metres formatting', () => {
  assert.equal(formatMetres(1.2, 0.6), '1.2 × 0.6 m');
  assert.equal(formatMetres(0.526), '0.53 m');
  assert.equal(formatMetres(undefined, 0.6), '? × 0.6 m');
});

/* ---- additions: capture studio */

// The check the bridge computed on the emulator take of 15 October (delays after the RF edge).
const CHECK = {
  ref: 'rx-cam2', order: ['rx-cam2', 'rx-cam1', 'rx-cam3'], fps: 30,
  delays_ms: { 'rx-cam1': 10.366, 'rx-cam2': 0.806, 'rx-cam3': 30.7 },
};

test('offset rows: cam0 is the earliest camera, offsets are delay differences, spread within a frame', () => {
  const o = offsetRows(CHECK);
  assert.deepEqual(o.rows.map((r) => [r.view, r.id]), [['cam0', 'rx-cam2'], ['cam1', 'rx-cam1'], ['cam2', 'rx-cam3']]);
  near(o.rows[0].offset_ms, 0);
  near(o.rows[1].offset_ms, 9.56);
  near(o.rows[2].offset_ms, 29.894);
  near(o.spread_ms, 29.894);
  near(o.frame_ms, 1000 / 30);
  assert.equal(o.ok, true);
  // bars are scaled to one frame when every delay is shorter than a frame
  near(o.rows[2].pct, (30.7 / (1000 / 30)) * 100);
  near(o.frame_pct, 100);
});

test('offset rows without an order or ref derive them from the delays; a long delay fails the check', () => {
  const o = offsetRows({ delays_ms: { a: 50, b: 2, c: 20 }, fps: 30 });
  assert.equal(o.ref, 'b');
  assert.deepEqual(o.rows.map((r) => r.id), ['b', 'c', 'a']);
  assert.equal(o.ok, false);
  near(o.spread_ms, 48);
  near(o.rows[2].pct, 100); // the longest delay fills the bar; the frame mark sits inside
  near(o.frame_pct, (1000 / 30 / 50) * 100);
  assert.deepEqual(offsetRows(null).rows, []);
  assert.equal(offsetRows({ delays_ms: { a: 'x' } }).ok, false);
});

test('milliseconds are signed with one decimal', () => {
  assert.equal(formatMs(9.56), '+9.6 ms');
  assert.equal(formatMs(0), '0.0 ms');
  assert.equal(formatMs(-0.04), '0.0 ms');
  assert.equal(formatMs(-12.34), '−12.3 ms');
  assert.equal(formatMs(NaN), '–');
});

test('take stages follow the bridge states', () => {
  const states = (t) => takeStages(t).map((s) => s.state);
  assert.deepEqual(TAKE_STAGES, ['armed', 'recording', 'stopped', 'offloaded']);
  assert.deepEqual(states(null), ['todo', 'todo', 'todo', 'todo']);
  assert.deepEqual(states({ state: 'arming' }), ['run', 'todo', 'todo', 'todo']);
  assert.deepEqual(states({ state: 'armed' }), ['done', 'run', 'todo', 'todo']);
  assert.deepEqual(states({ state: 'recording' }), ['done', 'run', 'todo', 'todo']);
  assert.deepEqual(states({ state: 'stopped', offloaded: false }), ['done', 'done', 'done', 'run']);
  assert.deepEqual(states({ state: 'stopped', offloaded: false, check_error: 'not yet' }), ['done', 'done', 'done', 'failed']);
  assert.deepEqual(states({ state: 'stopped', offloaded: true }), ['done', 'done', 'done', 'done']);
  assert.deepEqual(states({ state: 'failed', fired_at: null }), ['failed', 'todo', 'todo', 'todo']);
  assert.deepEqual(states({ state: 'failed', fired_at: 1, cameras: {} }), ['done', 'failed', 'todo', 'todo']);
  assert.deepEqual(states({ state: 'failed', fired_at: 1, cameras: { a: { started: true } } }), ['done', 'done', 'failed', 'todo']);
});

test('camera lines of a take', () => {
  const take = {
    state: 'recording', armed: ['rx-cam1', 'rx-cam2', 'rx-cam3'], not_armed: { 'rx-cam4': 'offline' },
    cameras: { 'rx-cam1': { started: true, stopped: true, duration_s: 10.2, frames: 306 }, 'rx-cam2': { started: true } },
  };
  const lines = takeCameraLines(take);
  assert.deepEqual(lines.map((l) => [l.id, l.kind]), [['rx-cam1', 'done'], ['rx-cam2', 'run'], ['rx-cam3', 'todo'], ['rx-cam4', 'failed']]);
  assert.equal(lines[0].text, 'rx-cam1 · stopped · 10.2 s · 306 frames');
  assert.equal(lines[1].text, 'rx-cam2 · recording');
  assert.equal(lines[2].text, 'rx-cam3 · armed, waiting for the trigger');
  assert.equal(lines[3].text, 'rx-cam4 · not armed: offline');
  assert.equal(takeCameraLines({ state: 'failed', armed: ['x'], cameras: {} })[0].text, 'x · did not start');
  assert.deepEqual(takeCameraLines(null), []);
});

test('battery text and camera badges', () => {
  assert.equal(batteryText({ bars: 4, full: true, charging: false }), 'full');
  assert.equal(batteryText({ bars: 2, full: false, charging: true }), '2 of 4 bars, charging');
  assert.equal(batteryText(null), 'no battery reading');
  assert.deepEqual(cameraBadge({ online: false }), { text: 'offline', kind: 'bad' });
  assert.deepEqual(cameraBadge({ online: true, state: 'live', armed: null }), { text: 'live', kind: 'ok' });
  assert.deepEqual(cameraBadge({ online: true, state: 'live', armed: { clip: 'take_001' } }), { text: 'armed', kind: 'warn' });
  assert.deepEqual(cameraBadge({ online: true, state: 'recording' }), { text: 'recording', kind: 'warn' });
  assert.deepEqual(cameraBadge({ online: true, state: 'error' }), { text: 'camera error', kind: 'bad' });
  assert.deepEqual(cameraBadge({ online: true, state: null }), { text: 'connecting', kind: 'warn' });
});

test('session id suggestion and take progress', () => {
  assert.equal(suggestSessionId('Kitting station, 9 cycles', new Date(2026, 9, 15)), 'kitting-station-9-cycles-20261015');
  assert.equal(suggestSessionId('', new Date(2026, 9, 15)), 'take-20261015');
  assert.equal(takeProgress({ state: 'recording', started_at: 100, seconds: 10 }, 105), 0.5);
  assert.equal(takeProgress({ state: 'recording', started_at: 100, seconds: 10 }, 130), 1);
  assert.equal(takeProgress({ state: 'stopped' }, 0), 1);
  assert.equal(takeProgress({ state: 'armed' }, 0), 0);
  assert.equal(takeProgress(null, 0), 0);
});

test('featuredSession prefers a real sample, then any sample, then anything', () => {
  const syn = { id: 'dev', kind: 'sample', synthetic: true };
  const real = { id: 'kit', kind: 'sample', synthetic: false };
  const up = { id: 'u', kind: 'upload', synthetic: false };
  assert.equal(featuredSession([syn, real]).id, 'kit');
  assert.equal(featuredSession([syn]).id, 'dev');
  assert.equal(featuredSession([up, syn]).id, 'dev');
  assert.equal(featuredSession([up]).id, 'u');
  assert.equal(featuredSession([]), null);
  assert.equal(featuredSession(undefined), null);
});

test('summaryTiles gives the four headline results from a result', () => {
  const result = {
    counts: { automate: 2, keep_human: 1, needs_fixture: 0, total: 3 },
    cycle_time_s: 10,
    steps: [
      { verdict: 'automate', mean_duration_s: 4 }, { verdict: 'automate', mean_duration_s: 2 },
      { verdict: 'keep_human', mean_duration_s: 3 },
    ],
    robot: { model: 'xArm 6', fits: true, payload_kg: 5, reach_mm: 700 },
    cycle_estimate: { speed_factor: 1.3 },
    roi_defaults: ROI,
    automated_share: 0.6,
  };
  const t = summaryTiles(result);
  assert.deepEqual(t.map((x) => x.key), ['steps', 'robot', 'cycle', 'payback']);
  assert.equal(t[0].value, '2 of 3');
  assert.equal(t[0].kind, 'automate');
  assert.equal(t[1].value, 'xArm 6');
  assert.equal(t[1].detail, '5 kg payload · 700 mm reach');
  assert.equal(t[2].value, '7.8 s'); // 6 s of automate steps × 1.3, against 3 s + 1 s left to the operator
  assert.equal(t[2].detail, '10.0 s by hand today');
  assert.equal(t[3].value, '≈ 32 months'); // 90,000 net over 33,600 a year
  assert.equal(t[3].detail, 'grant SGD 30,000 applied');
  // a slower robot and the engineer's own inputs
  assert.equal(summaryTiles(result, { factor: 2 })[2].value, '12.0 s');
  assert.equal(summaryTiles(result, { roi: { ...ROI, grant_share: 0 } })[3].value, '≈ 43 months');
});

test('summaryTiles degrades to dashes without a robot, a cycle or payback inputs', () => {
  const t = summaryTiles({});
  assert.equal(t[0].value, '0 of 0');
  assert.equal(t[0].kind, null);
  assert.equal(t[1].value, '–');
  assert.equal(t[2].value, '–');
  assert.equal(t[3].value, '–');
  const noFit = summaryTiles({ robot: { model: 'UR3e', fits: false } });
  assert.equal(noFit[1].kind, 'keep_human');
});

test('verdict filter counts and matching', () => {
  const steps = [
    { verdict: 'automate' }, { verdict: 'automate', preflagged: true }, { verdict: 'keep_human' },
    { verdict: 'needs_fixture', preflagged: true },
  ];
  assert.deepEqual(verdictFilterCounts(steps), { all: 4, automate: 2, needs_fixture: 1, keep_human: 1, flagged: 2 });
  assert.deepEqual(verdictFilterCounts(undefined), { all: 0, automate: 0, needs_fixture: 0, keep_human: 0, flagged: 0 });
  assert.equal(steps.filter((s) => stepMatchesFilter(s, 'all')).length, 4);
  assert.equal(steps.filter((s) => stepMatchesFilter(s, 'automate')).length, 2);
  assert.equal(steps.filter((s) => stepMatchesFilter(s, 'flagged')).length, 2);
  assert.equal(steps.filter((s) => stepMatchesFilter(s, 'keep_human')).length, 1);
  assert.equal(stepMatchesFilter({ verdict: 'automate' }, undefined), true);
});

test('cellLanes splits one cycle between the cobot and the operator', () => {
  const result = {
    cycle_time_s: 10,
    steps: [
      { name: 'Pick', verdict: 'automate', mean_duration_s: 4 },
      { name: 'Screw', verdict: 'needs_fixture', mean_duration_s: 2 },
      { name: 'Place', verdict: 'automate', mean_duration_s: 2 },
      { name: 'Check', verdict: 'keep_human', mean_duration_s: 1 },
    ],
  };
  const lanes = cellLanes(result, 1.5);
  // cobot: (4 + 2) × 1.5 = 9 s; operator: 2 + 1 + 1 s uncovered = 4 s; side by side = 9 s
  assert.equal(lanes.total_s, 9);
  assert.equal(lanes.robot_s, 9);
  assert.equal(lanes.operator_s, 4);
  assert.deepEqual(lanes.robot.map((x) => x.name), ['Pick', 'Place']);
  assert.deepEqual(lanes.operator.map((x) => x.name), ['Screw', 'Check', 'not covered by a step']);
  near(lanes.robot[0].pct, (6 / 9) * 100);
  near(lanes.operator[2].seconds, 1);
  near(lanes.robot_idle_pct, 0);
  near(lanes.operator_idle_pct, (5 / 9) * 100);
  assert.equal(cellLanes({ steps: [] }, 1.3), null);
  assert.equal(cellLanes(result, 0), null);
});

test('the scenarios are complete and distinct', () => {
  assert.ok(SCENARIOS.length >= 5);
  assert.equal(new Set(SCENARIOS.map((s) => s.id)).size, SCENARIOS.length);
  for (const s of SCENARIOS) {
    assert.match(s.id, /^[a-z0-9-]{2,40}$/);
    assert.ok(s.name && s.short && s.where && s.why && s.cameras && s.expect, s.id);
    assert.ok(s.cycle.length >= 4 && s.cycle.length <= 6, `${s.id}: ${s.cycle.length} steps`);
    assert.ok(s.props.length >= 3, s.id);
    assert.ok(s.brief.length >= 80 && s.brief.length <= 600, `${s.id}: brief ${s.brief.length}`);
    assert.ok(!/automate|fixture|keep human/i.test(s.brief), `${s.id}: the brief must not lead the model`);
  }
  assert.equal(scenarioById('kitting').name, 'Kitting station');
  assert.equal(scenarioById('nope'), null);
  assert.ok(RECORDING_RULES.length >= 3);
});

test('notesByStep indexes the recommendations by step id', () => {
  const notes = { step_notes: [{ id: 's1', recommendation: 'Parallel gripper.', fixture: '' }, { id: 's3', recommendation: 'A nest.', fixture: 'tray nest' }, { recommendation: 'no id' }] };
  const m = notesByStep(notes);
  assert.equal(m.size, 2);
  assert.equal(m.get('s3').fixture, 'tray nest');
  assert.equal(notesByStep(null).size, 0);
  assert.equal(notesByStep({}).size, 0);
});
