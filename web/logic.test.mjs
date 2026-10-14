// Run: node --test web/logic.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  computePayback, automatedShare, driftAction, followerTarget, nextSeekLead,
  layoutTimeline, activeSegmentIndex, cycleAt, totalCycles, adjacentStart,
  bannerText, engineerLine, signoffSummary, stageStates, formatTime, formatClock, formatMoney,
} from './logic.js';

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
  assert.deepEqual(stageStates('running', 'verifier').map((s) => s.state), ['done', 'run', 'todo', 'todo']);
  assert.deepEqual(stageStates('queued', 'queued').map((s) => s.state), ['todo', 'todo', 'todo', 'todo']);
  assert.deepEqual(stageStates('done', 'done').map((s) => s.state), ['done', 'done', 'done', 'done']);
  assert.deepEqual(stageStates('failed', 'robot').map((s) => s.state), ['done', 'done', 'failed', 'todo']);
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
