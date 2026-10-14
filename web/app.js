// CellPilot page. Plain ES module: no framework, no build step.
// Talks only to the API in docs/contract.md; all arithmetic lives in logic.js.
import {
  STAGES as STAGE_NAMES, VERDICT_LABEL, computePayback, automatedShare, followerTarget, driftAction, nextSeekLead,
  layoutTimeline, activeSegmentIndex, cycleAt, totalCycles, adjacentStart,
  bannerPieces, bannerText, engineerLine, signoffSummary, stageStates, formatTime, formatClock, formatMoney,
  cycleBreakdown, robotCycle, evidenceAt, contentRect, boxRect, layoutScene, formatMetres,
} from './logic.js';

const POLL_MS = 5000;
const DROP_MS = 400;
const SPEEDS = [0.5, 1, 2];
const ENGINEER_KEY = 'cellpilot.engineer';
const runKey = (sid) => `cellpilot.run.${sid}`;

const main = document.getElementById('main');

/* ---------------------------------------------------------------- helpers */

function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style') Object.assign(el.style, v);
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : String(v));
  }
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : String(c));
  }
  return el;
}

// localStorage / sessionStorage can throw (private mode, blocked cookies): never let that break the page.
const storage = {
  get(area, key) {
    try { return window[area].getItem(key); } catch { return null; }
  },
  set(area, key, value) {
    try {
      if (value == null) window[area].removeItem(key);
      else window[area].setItem(key, value);
    } catch { /* storage unavailable: the page works without it */ }
  },
};

const num = (x) => (x == null || x === '' ? NaN : Number(x));
const fmtN = (x, digits = 2) => (Number.isFinite(x) ? x.toLocaleString('en-US', { maximumFractionDigits: digits }) : '–');
const reducedMotion = () => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;

function fmtDateTime(iso) {
  const d = new Date(iso);
  if (!iso || Number.isNaN(d.getTime())) return '';
  return d.toLocaleString(undefined, { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function fmtSeconds(s) {
  if (!Number.isFinite(s)) return '';
  return s < 90 ? `${Math.round(s)} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
}

async function api(path, { method = 'GET', body } = {}) {
  let res;
  try {
    res = await fetch(path, {
      method,
      cache: 'no-store',
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    return { ok: false, status: 0, data: null, error: 'Could not reach the server. Check the connection and try again.' };
  }
  let data = null;
  try {
    const text = await res.text();
    data = text ? JSON.parse(text) : null;
  } catch { data = null; }
  return { ok: res.ok, status: res.status, data, error: res.ok ? null : errorText(data, res.status) };
}

function errorText(data, status) {
  const d = data?.detail;
  if (typeof d === 'string') return d;
  if (Array.isArray(d) && d.length) return d.map((x) => x?.msg || JSON.stringify(x)).join('; ');
  return `The server answered HTTP ${status}.`;
}

const backLink = (href = '#/', text = '← Sessions') => h('a', { class: 'back', href }, text);
const estimateBadge = (text = 'estimated from video') => h('span', {
  class: 'badge est', title: 'An estimate by the model from the footage, not a measurement',
}, text);
const errorBox = (msg, retry) => h('div', { class: 'error', role: 'alert' },
  h('p', null, msg), retry ? h('button', { type: 'button', class: 'btn', onclick: retry }, 'Try again') : null);
const verdictBadge = (v) => h('span', { class: `vb v-${v}` }, (VERDICT_LABEL[v] || v || 'unknown').toUpperCase());
const syntheticBadge = () => h('span', {
  class: 'badge warn', title: 'A development session made from synthetic test clips, not footage of a real job',
}, 'synthetic test clips');

function verifierSummary(v) {
  if (!v) return 'not checked by verifier'; // the server skipped the verifier for this step (it is pre-flagged)
  const parts = [];
  if (v.verdict_agree === false) parts.push('verifier disputes the verdict');
  const dt = num(v.timing_delta_s);
  if (Math.abs(dt) > 1) parts.push(`verifier timing differs ${Math.abs(dt).toFixed(1)} s (corrected)`);
  if (!parts.length) parts.push(v.agree === false ? 'verifier disagrees' : 'verifier agrees');
  return parts.join(', ');
}

function fitLine(have, need, unit, basis) {
  if (!Number.isFinite(have)) return '–';
  const text = `${fmtN(have)} ${unit}`;
  if (!Number.isFinite(need)) return [text, basis ? h('span', { class: 'muted' }, ` · ${basis}`) : null];
  const ok = have >= need;
  return [text, ` · needs ${fmtN(need)} ${unit} `,
    h('span', { class: ok ? 'good' : 'bad', role: 'img', 'aria-label': ok ? 'enough' : 'not enough' }, ok ? '✓' : '✗'),
    basis ? h('span', { class: 'muted' }, ` · ${basis}`) : null];
}

const check = (ok) => (ok == null ? h('span', { class: 'muted' }, '–')
  : h('span', { class: ok ? 'good' : 'bad', role: 'img', 'aria-label': ok ? 'yes' : 'no' }, ok ? '✓' : '✗'));

function priceText(band) {
  if (!band || !Number.isFinite(num(band.low)) || !Number.isFinite(num(band.high))) return '';
  const k = (n) => `${Math.round(n / 1000)}k`;
  return `${band.currency || 'USD'} ${k(num(band.low))}–${k(num(band.high))}`;
}

/* ------------------------------------------------- shared result renderers */

const KIND_LABEL = {
  bin: 'bin', fixture: 'fixture', tool: 'tool', part_source: 'parts in', part_destination: 'parts out', other: '',
};

function svg(tag, attrs, ...children) {
  const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v != null) el.setAttribute(k, String(v));
  for (const c of children.flat(Infinity)) if (c != null && c !== false) el.append(c instanceof Node ? c : String(c));
  return el;
}

/** Top-view sketch of the station from the result's layout, with the chosen robot's reach. */
function drawLayout(layout, robot) {
  const sc = layoutScene(layout, num(robot?.reach_mm));
  if (!sc) return null;
  const [vx, vy, vw, vh] = sc.viewBox;
  const fs = Math.max(28, Math.round(vw / 30)); // label size in scene mm
  const root = svg('svg', {
    class: 'layout-svg', viewBox: `${vx} ${vy} ${vw} ${vh}`, role: 'img', preserveAspectRatio: 'xMidYMid meet',
    'aria-label': `Top view of the station: a ${formatMetres(layout.bench?.length_m, layout.bench?.width_m)} bench, `
      + `${sc.objects.length} objects, the operator in front and the proposed robot base at the back`,
  });
  if (sc.reach) root.append(svg('circle', { class: 'reach', cx: sc.reach.cx, cy: sc.reach.cy, r: sc.reach.r }));
  if (sc.needed) root.append(svg('circle', { class: 'needed', cx: sc.needed.cx, cy: sc.needed.cy, r: sc.needed.r }));
  root.append(svg('rect', { class: 'bench', x: sc.bench.x, y: sc.bench.y, width: sc.bench.w, height: sc.bench.h, rx: 12 }));
  root.append(svg('text', { class: 'lbl bench-lbl', x: sc.bench.x + 20, y: sc.bench.y - 14, 'font-size': fs },
    `bench ${formatMetres(layout.bench?.length_m, layout.bench?.width_m)}`));
  for (const o of sc.objects) {
    const kind = KIND_LABEL[o.kind] ?? '';
    const text = o.name + (kind && kind !== o.name ? ` · ${kind}` : '');
    const inside = o.w >= text.length * fs * 0.6 && o.h >= fs * 1.6; // else the label sits under the object
    root.append(svg('g', { class: `obj k-${o.kind}` },
      svg('rect', { x: o.x, y: o.y, width: o.w, height: o.h, rx: 8 }),
      svg('text', { class: 'lbl', x: o.x + o.w / 2, y: inside ? o.y + o.h / 2 + fs * 0.35 : o.y + o.h + fs * 1.05,
        'text-anchor': 'middle', 'font-size': fs }, text)));
  }
  for (const a of sc.arrows) {
    root.append(svg('line', { class: `arrow v-${a.verdict}`, x1: a.x1, y1: a.y1, x2: a.x2, y2: a.y2, 'marker-end': 'url(#arrowhead)' }));
  }
  root.append(svg('defs', null, svg('marker', { id: 'arrowhead', markerWidth: 8, markerHeight: 8, refX: 6, refY: 4, orient: 'auto', markerUnits: 'strokeWidth' },
    svg('path', { d: 'M0,0 L8,4 L0,8 z', class: 'arrowhead' }))));
  for (const a of sc.anchors) {
    root.append(svg('g', { class: `anchor v-${a.verdict} ${a.kind}` },
      svg('circle', { cx: a.cx, cy: a.cy, r: 28 }),
      svg('title', null, `${a.name}: ${a.kind}`)));
  }
  root.append(svg('g', { class: 'operator' },
    svg('circle', { cx: sc.operator.cx, cy: sc.operator.cy, r: sc.operator.r }),
    svg('text', { class: 'lbl', x: sc.operator.cx, y: sc.operator.cy + fs * 0.35, 'text-anchor': 'middle', 'font-size': fs }, 'operator')));
  if (sc.base) {
    root.append(svg('g', { class: 'base' },
      svg('rect', { x: sc.base.cx - 60, y: sc.base.cy - 60, width: 120, height: 120, rx: 14 }),
      svg('text', { class: 'lbl', x: sc.base.cx, y: sc.base.cy - 80, 'text-anchor': 'middle', 'font-size': fs },
        `${robot?.model || 'robot'} base`)));
  }
  return root;
}

function layoutLegend(layout, robot) {
  const items = [
    ['sw reach', `${robot?.model || 'robot'} reach ${fmtN(num(robot?.reach_mm), 0)} mm`],
    ['sw needed', layout?.needed_reach_m != null ? `needed reach ${fmtN(num(layout.needed_reach_m) * 1000, 0)} mm` : 'needed reach: no robot pick or place'],
    ['sw pick', 'pick'], ['sw place', 'place'],
  ];
  return h('p', { class: 'layout-legend' }, items.map(([cls, text]) => h('span', null, h('i', { class: cls, 'aria-hidden': 'true' }), ` ${text}`)));
}

/** Every catalogue arm as judged for this job. */
function robotTable(robot) {
  const alts = Array.isArray(robot?.alternatives) ? robot.alternatives : [];
  if (!alts.length) return null;
  const reachKnown = Number.isFinite(num(robot.needed_reach_mm));
  const head = h('tr', null, h('th', null, 'Arm'), h('th', null, 'Payload'), h('th', null, 'Reach'),
    h('th', { title: 'Indicative list-price band from public reseller listings; not a quote' }, 'Price*'),
    h('th', null, 'Verdict'));
  const rows = alts.map((o) => h('tr', { class: o.chosen ? 'chosen' : (o.fits ? '' : 'nofit') },
    h('td', null, h('b', null, o.model), o.vendor ? h('div', { class: 'muted vendor' }, o.vendor) : null),
    h('td', { class: 'num' }, `${fmtN(num(o.payload_kg))} kg `, check(o.payload_fits)),
    h('td', { class: 'num' }, `${fmtN(num(o.reach_mm), 0)} mm `, reachKnown ? check(o.reach_fits) : h('span', { class: 'muted', title: 'No layout: reach not checked' }, '–')),
    h('td', { class: 'num' }, priceText(o.price_band) || '–'),
    h('td', { class: 'note' }, o.chosen ? h('b', null, 'chosen') : o.note || (o.fits ? 'fits' : 'does not fit'))));
  return h('table', { class: 'alts' }, h('thead', null, head), h('tbody', null, rows));
}

function cycleBar(result) {
  const b = cycleBreakdown(result);
  if (!(b.cycle_s > 0)) return h('p', { class: 'muted small' }, 'No measured cycle time.');
  const bar = h('div', { class: 'cycle-bar', role: 'img', 'aria-label': `Cycle of ${b.cycle_s.toFixed(1)} s split by step` },
    b.items.map((it) => h('span', {
      class: `seg-bar v-${it.verdict}`, style: { width: `${it.pct}%` },
      title: `${it.index + 1} · ${it.name}: ${it.seconds.toFixed(1)} s (${Math.round(it.pct)} %)`,
    })),
    b.unassigned_s > 0.05 ? h('span', { class: 'seg-bar unassigned', style: { width: `${b.unassigned_pct}%` },
      title: `Not covered by any step: ${b.unassigned_s.toFixed(1)} s` }) : null);
  const list = h('ol', { class: 'cycle-list' },
    b.items.map((it) => h('li', null,
      h('i', { class: `dot v-${it.verdict}`, 'aria-hidden': 'true' }),
      h('span', { class: 'cl-name' }, `${it.index + 1} · ${it.name}`),
      h('span', { class: 'cl-num' }, `${it.seconds.toFixed(1)} s · ${Math.round(it.pct)} %`))),
    b.unassigned_s > 0.05 ? h('li', { class: 'muted' }, h('i', { class: 'dot unassigned', 'aria-hidden': 'true' }),
      h('span', { class: 'cl-name' }, 'not covered by a step'),
      h('span', { class: 'cl-num' }, `${b.unassigned_s.toFixed(1)} s · ${Math.round(b.unassigned_pct)} %`)) : null);
  return [bar, list];
}

/* ---------------------------------------------------------------- router */

let current = null;

function route() {
  const hash = location.hash || '#/';
  if (!hash.startsWith('#/')) return;
  current?.destroy();
  document.body.classList.remove('printing');
  const dec = (x) => { try { return decodeURIComponent(x); } catch { return x; } };
  // #/s/<sid>, or #/session/<sid> so a screenshot tool can open an analysis directly;
  // #/s/<sid>/report[/<aid>] is the printable study; #/guide the capture guide.
  const m = /^#\/(?:s|session)\/([^/?#]+)(?:\/(report)(?:\/([^/?#]+))?)?/.exec(hash);
  if (m && m[2] === 'report') current = new ReportView(dec(m[1]), m[3] ? dec(m[3]) : null);
  else if (m) current = new AnalysisView(dec(m[1]));
  else if (/^#\/guide/.test(hash)) current = new GuideView();
  else current = new SessionsView();
  window.scrollTo(0, 0);
}

/* ---------------------------------------------------------------- S1 sessions */

class SessionsView {
  constructor() {
    this.alive = true;
    document.title = 'CellPilot · Sessions';
    this.body = h('div', null, h('p', { class: 'muted' }, 'Loading sessions…'));
    const flow = h('ol', { class: 'flow', 'aria-label': 'How CellPilot works' },
      h('li', null, h('b', null, '1 · Film'), ' three cameras around the bench, started together, while the operator works 8 to 10 cycles.'),
      h('li', null, h('b', null, '2 · Analyse'), ' Gemini 4 Argon sees all three views at once: steps on one clock, a verdict per step with evidence, a second pass that checks it, a top-view layout.'),
      h('li', null, h('b', null, '3 · Decide'), ' our code checks a cobot by payload and reach, estimates the cycle and the payback; the engineer approves or flags each step and prints the study.'));
    main.replaceChildren(
      h('h1', { class: 'page-title' }, 'Sessions'),
      h('p', { class: 'lead muted' },
        'A session is one recording of a manual job: three MP4 clips, one per camera, and the time offset of each clip on a shared clock. Open one to see its study.'),
      flow,
      this.body,
      h('section', { class: 'card byo' },
        h('h2', null, 'Bring your own footage'),
        h('p', null, 'Three static cameras, started together, all seeing the whole bench and both hands. The ',
          h('a', { href: '#/guide' }, 'capture guide'), ' shows where to put them, how the clips are aligned, and what a session folder looks like.'),
        h('p', { class: 'small muted' },
          'In this build a session is added to the server by its folder (clips plus session.json) and analysed with “Run it live”. '
          + 'Browser upload is not in this build: each clip would have to be posted on its own under Cloud Run’s 32 MiB request cap and count against the daily live-run budget, and an unrelated clip would only produce an unusable study. The README lists the design.')),
    );
    this.load();
  }

  async load() {
    this.body.replaceChildren(h('p', { class: 'muted' }, 'Loading sessions…'));
    const r = await api('/api/sessions');
    if (!this.alive) return;
    if (!r.ok || !Array.isArray(r.data)) {
      this.body.replaceChildren(errorBox(r.error || 'Unexpected answer from the server.', () => this.load()));
      return;
    }
    if (!r.data.length) {
      this.body.replaceChildren(h('p', { class: 'muted' }, 'No sessions yet.'));
      return;
    }
    this.body.replaceChildren(h('ul', { class: 'sessions' }, r.data.map((s) => {
      const href = `#/s/${encodeURIComponent(s.id)}`;
      const name = s.name || s.id;
      return h('li', { class: 'card session' },
        h('div', { class: 'session-text' },
          h('h2', { class: 'session-name' }, h('a', { href }, name)),
          h('p', { class: 'session-meta' },
            s.kind === 'sample' ? h('span', { class: 'badge' }, 'Sample') : null,
            s.synthetic ? syntheticBadge() : null,
            h('span', { class: 'muted' },
              [`${s.clips ?? '?'} clips`, formatClock(num(s.duration_s)), s.id].join(' · '))),
          s.synthetic ? h('p', { class: 'small muted' },
            'Development clips: a red square crossing a line, filmed by three synthetic cameras with real offsets. '
            + 'They exercise every stage of the pipeline; the first kitting session recorded with the M1 rig replaces them.') : null),
        h('a', { class: 'btn primary', href, 'aria-label': `Open ${name}` }, 'Open'));
    })));
  }

  destroy() { this.alive = false; }
}

/* ---------------------------------------------------------------- video sync */

// cam0 is the master. Each follower's position is master time - offset_s (clamped at 0).
// On every master frame the follower's drift decides: leave it, nudge its rate, or seek.
class SyncPlayer {
  constructor(master, followers, onTick) {
    this.master = master;
    this.followers = followers.map((f) => ({ ...f, drift: 0, action: 'none', seeks: 0, lead: 0, pendingSeek: false }));
    this.onTick = onTick;
    this.rate = 1;
    this.frame = null;
    this.useRvfc = typeof master.requestVideoFrameCallback === 'function';
    this.ac = new AbortController();
    const on = (type, fn) => master.addEventListener(type, fn, { signal: this.ac.signal });
    on('play', () => this.schedule());
    on('pause', () => { // also fires when the master reaches its end
      for (const f of this.followers) f.el.pause();
      this.alignAll();
      this.tick();
    });
    on('seeked', () => { if (this.master.paused) this.alignAll(); this.tick(); });
    on('waiting', () => { for (const f of this.followers) f.el.pause(); }); // the frame loop restarts them
    on('loadedmetadata', () => this.tick());
    on('timeupdate', () => { if (this.master.paused) this.tick(); });
  }

  get playing() { return !this.master.paused; }

  play() {
    const m = this.master;
    if (m.ended) m.currentTime = 0;
    const t = m.currentTime;
    m.playbackRate = this.rate;
    const starters = [];
    for (const f of this.followers) { // seek the others first...
      const { t: ft, hold } = followerTarget(t, f.offset, f.el.duration);
      f.el.playbackRate = this.rate;
      if (Math.abs(f.el.currentTime - ft) > 0.001) f.el.currentTime = ft;
      if (!hold) starters.push(f.el);
    }
    // ...then start all of them in the same tick.
    m.play().catch(() => {});
    for (const el of starters) el.play().catch(() => {});
  }

  pause() {
    this.master.pause();
    for (const f of this.followers) f.el.pause();
  }

  toggle() { if (this.master.paused) this.play(); else this.pause(); }

  seek(t) {
    const d = this.master.duration;
    const target = Math.max(0, Number.isFinite(d) ? Math.min(t, d) : t);
    this.master.currentTime = target;
    this.alignAll(target);
    this.tick();
  }

  setRate(r) {
    this.rate = r;
    this.master.playbackRate = r;
    for (const f of this.followers) f.el.playbackRate = r;
  }

  alignAll(t = this.master.currentTime) {
    for (const f of this.followers) {
      const { t: ft } = followerTarget(t, f.offset, f.el.duration);
      if (Math.abs(f.el.currentTime - ft) > 0.001) f.el.currentTime = ft;
      f.el.playbackRate = this.rate;
      f.drift = 0;
    }
  }

  schedule() {
    if (this.frame || !this.ac || this.ac.signal.aborted) return;
    if (this.useRvfc) this.frame = { rvfc: true, id: this.master.requestVideoFrameCallback(() => this.onFrame()) };
    else this.frame = { rvfc: false, id: requestAnimationFrame(() => this.onFrame()) };
  }

  onFrame() {
    this.frame = null;
    if (this.ac.signal.aborted) return;
    if (!this.master.paused && !this.master.seeking) this.correct();
    this.tick();
    if (!this.master.paused) this.schedule();
  }

  correct() {
    const t = this.master.currentTime;
    for (const f of this.followers) {
      const el = f.el;
      if (el.seeking || el.readyState < 2) continue;
      const { t: target, hold } = followerTarget(t, f.offset, el.duration);
      if (hold) { // before this camera's clip starts (or after it ends): sit still
        if (!el.paused) el.pause();
        if (Math.abs(el.currentTime - target) > 0.08) el.currentTime = target;
        f.drift = 0;
        f.action = 'hold';
        continue;
      }
      if (el.paused) { // its clip just started, or it stalled: put it in place and start it
        el.currentTime = target + f.lead * this.rate;
        el.playbackRate = this.rate;
        el.play().catch(() => {});
        f.pendingSeek = true;
        f.action = 'start';
        continue;
      }
      const drift = el.currentTime - target;
      if (f.pendingSeek) { f.pendingSeek = false; f.lead = nextSeekLead(f.lead, drift); }
      const d = driftAction(drift, this.rate);
      f.drift = drift;
      f.action = d.action;
      if (d.action === 'seek') {
        el.currentTime = target + f.lead * this.rate;
        el.playbackRate = this.rate;
        f.seeks += 1;
        f.pendingSeek = true;
      } else if (el.playbackRate !== d.rate) {
        el.playbackRate = d.rate;
      }
    }
  }

  tick() { this.onTick(this.master.currentTime); }

  destroy() {
    this.ac.abort();
    if (this.frame) {
      if (this.frame.rvfc) this.master.cancelVideoFrameCallback?.(this.frame.id);
      else cancelAnimationFrame(this.frame.id);
      this.frame = null;
    }
    for (const el of [this.master, ...this.followers.map((f) => f.el)]) {
      el.pause();
      el.removeAttribute('src');
      el.load(); // stops the download
    }
  }
}

/* ---------------------------------------------------------------- S2-S5 analysis */

class AnalysisView {
  constructor(sid) {
    this.sid = sid;
    this.alive = true;
    this.timers = new Set();
    this.raf = 0;
    this.player = null;
    this.analysis = null;
    this.segments = [];
    this.segEls = [];
    this.activeSeg = -1;
    this.activeStep = null;
    this.stepRows = new Map();
    this.soEls = new Map();
    this.run = null;
    this.lastDebug = 0;
    this.lastSec = -1;
    this.deferredAnimate = null;
    this.overlays = {};
    this.videoEls = {};
    this.pinned = null;      // a reason box held on screen after a cite click
    this.evidenceKey = '';   // what is drawn now, to skip redundant redraws
    this.onResize = () => this.renderEvidence(this.now(), true);
    addEventListener('resize', this.onResize);
    document.title = 'CellPilot';
    main.replaceChildren(backLink(), h('p', { class: 'muted' }, 'Loading session…'));
    this.load();
  }

  base() { return `/api/sessions/${encodeURIComponent(this.sid)}`; }

  async load() {
    const r = await api(this.base());
    if (!this.alive) return;
    if (!r.ok || !r.data?.session) {
      const missing = r.status === 404;
      main.replaceChildren(backLink(), errorBox(
        missing ? `There is no session called “${this.sid}”.` : (r.error || 'Unexpected answer from the server.'),
        missing ? null : () => this.load()));
      return;
    }
    this.session = r.data.session;
    this.clipUrls = r.data.clip_urls || {};
    this.liveLeft = Math.max(0, Number(r.data.live_runs_left) || 0);
    this.build();
    const latest = r.data.latest;
    if (latest) this.showAnalysis(latest, { animate: true });
    else this.showNoAnalysis();
    // A live run started before a reload: pick up its progress again.
    const pending = storage.get('sessionStorage', runKey(this.sid));
    if (pending && pending !== latest?.id) this.resumeRun(pending);
    else storage.set('sessionStorage', runKey(this.sid), null);
  }

  /* ----- layout, built once per session; result panels re-render inside it */

  build() {
    const s = this.session;
    document.title = `CellPilot · ${s.name || s.id}`;
    const all = Array.isArray(s.clips) ? s.clips : [];
    const clips = [...all.filter((c) => c.view === 'cam0'), ...all.filter((c) => c.view !== 'cam0')];
    this.duration = num(clips[0]?.duration_s) || 0;

    const head = h('header', { class: 'session-head' },
      backLink(),
      h('h1', { class: 'page-title' }, s.name || s.id),
      h('p', { class: 'session-meta' },
        s.kind === 'sample' ? h('span', { class: 'badge' }, 'Sample') : null,
        s.synthetic ? syntheticBadge() : null,
        h('span', { class: 'muted' }, [
          s.recorded ? `recorded ${s.recorded}` : null,
          `${clips.length} cameras`,
          this.duration ? formatClock(this.duration) : null,
        ].filter(Boolean).join(' · '))));

    // Banner: the "N of M" moment, the engineer line, and the actions.
    this.bannerMain = h('h2', { class: 'banner-main' });
    this.bannerSub = h('p', { class: 'banner-sub', 'aria-live': 'polite' });
    this.summary = h('p', { class: 'summary' });
    this.runBtn = h('button', { type: 'button', class: 'btn primary', onclick: () => this.startRun() });
    this.reportLink = h('a', { class: 'btn', hidden: true, title: 'A one-page study to print or save as PDF' }, 'Printable study');
    this.exportLink = h('a', { class: 'btn', download: '', hidden: true, title: 'The result and the sign-offs as JSON' }, 'Export JSON');
    const banner = h('section', { class: 'banner card', 'aria-label': 'Result' },
      h('div', { class: 'banner-text' }, this.bannerMain, this.bannerSub, this.summary),
      h('div', { class: 'actions' }, this.runBtn, this.reportLink, this.exportLink));

    // Three videos.
    this.figures = {};
    const videos = clips.map((c, i) => {
      const v = document.createElement('video');
      v.muted = true;
      v.defaultMuted = true;
      v.playsInline = true;
      v.setAttribute('muted', '');
      v.setAttribute('playsinline', '');
      v.preload = 'auto';
      v.disablePictureInPicture = true;
      v.setAttribute('aria-label', `${c.view} camera view`);
      const url = this.clipUrls[c.view];
      if (url) v.src = url;
      const note = h('span', { class: 'vid-note' }, url ? '' : ' · clip missing');
      v.addEventListener('error', () => { note.textContent = ' · could not load the clip'; });
      v.addEventListener('click', () => this.player?.toggle());
      v.addEventListener('loadedmetadata', () => this.renderEvidence(this.now(), true));
      const offset = num(c.offset_s) || 0;
      // The overlay sits exactly over the video and carries the evidence boxes.
      this.overlays[c.view] = h('div', { class: 'overlay', 'aria-hidden': 'true' });
      this.videoEls[c.view] = v;
      this.figures[c.view] = h('figure', { class: 'vid' },
        h('div', { class: 'vid-box' }, v, this.overlays[c.view]),
        h('figcaption', null, h('b', null, c.view),
          i === 0 ? ' · master clock' : ` · starts at cam0 ${offset.toFixed(2)} s`, note));
      return { el: v, view: c.view, offset };
    });
    this.vids = h('div', { class: 'vids' }, Object.values(this.figures));

    // Transport.
    this.playBtn = h('button', { type: 'button', class: 'btn play', 'aria-label': 'Play', onclick: () => this.player?.toggle() }, '▶');
    // Both outputs change every frame while playing: keep screen readers quiet.
    this.timeOut = h('output', { class: 'time', title: 'cam0 time', 'aria-live': 'off' }, '');
    const prev = h('button', { type: 'button', class: 'btn', 'aria-label': 'Previous step', onclick: () => this.jump(-1) }, '◀ step');
    const next = h('button', { type: 'button', class: 'btn', 'aria-label': 'Next step', onclick: () => this.jump(1) }, 'step ▶');
    this.speedBtns = SPEEDS.map((r) => h('button', {
      type: 'button', class: 'btn', 'aria-pressed': String(r === 1), onclick: () => this.setSpeed(r),
    }, `${r}×`));
    const speed = h('div', { class: 'speed', role: 'group', 'aria-label': 'Playback speed' }, this.speedBtns);
    this.cycleOut = h('output', { class: 'cycle', 'aria-live': 'off' }, '');
    this.debugBox = h('input', { type: 'checkbox', id: 'sync-debug' });
    this.debugBox.addEventListener('change', () => {
      this.driftOut.hidden = !this.debugBox.checked;
      this.renderDrift(true);
    });
    // The drift readout is for us, not for judges: it shows only with ?debug in the URL.
    const debug = h('label', { class: 'debug-toggle', for: 'sync-debug', hidden: !location.search.includes('debug') },
      this.debugBox, 'sync debug');
    this.driftOut = h('output', { class: 'drift', hidden: true, 'aria-label': 'Measured drift of each camera against cam0' });
    this.evidenceBox = h('input', { type: 'checkbox', id: 'evidence-toggle', checked: true });
    this.evidenceBox.addEventListener('change', () => this.renderEvidence(this.now(), true));
    const evidence = h('label', { class: 'debug-toggle', for: 'evidence-toggle',
      title: 'Draw the box each reason cites over its camera while the playhead is within 0.75 s of it' },
      this.evidenceBox, 'evidence boxes');
    const transport = h('div', { class: 'transport' },
      this.playBtn, this.timeOut, h('span', { class: 'nav' }, prev, next), speed, this.cycleOut, evidence, debug);

    // Timeline: segments (click to seek) over a native range input for keyboard and screen readers.
    this.track = h('div', { class: 'track' });
    this.playhead = h('div', { class: 'playhead', 'aria-hidden': 'true' });
    this.track.addEventListener('click', (e) => {
      const rect = this.track.getBoundingClientRect();
      if (rect.width > 0 && this.duration > 0) this.seek(((e.clientX - rect.left) / rect.width) * this.duration);
    });
    this.scrub = h('input', {
      type: 'range', class: 'scrub', min: '0', max: String(this.duration || 0), step: '0.1', value: '0',
      'aria-label': 'Position in cam0 time',
    });
    this.scrub.addEventListener('input', () => { this.scrubbing = true; this.seek(Number(this.scrub.value)); });
    this.scrub.addEventListener('change', () => { this.scrubbing = false; });
    const legend = h('p', { class: 'legend' },
      verdictBadge('automate'), verdictBadge('needs_fixture'), verdictBadge('keep_human'),
      h('span', { class: 'legend-dash' }, 'dashed = verifier disputes'));
    const timeline = h('section', { class: 'timeline', 'aria-label': 'Steps over time' }, this.track, this.scrub, legend);

    this.runStatus = h('div', { class: 'run-status' });
    this.runAnnounce = h('p', { class: 'sr-only', role: 'status' });

    // Steps (S3), robot (S4) and payback (S5).
    this.engineer = h('input', {
      id: 'engineer', type: 'text', autocomplete: 'name', maxlength: '80', placeholder: 'Your name',
      value: storage.get('localStorage', ENGINEER_KEY) || '',
    });
    this.engineer.addEventListener('input', () => storage.set('localStorage', ENGINEER_KEY, this.engineer.value.trim() || null));
    this.stepList = h('ol', { class: 'steps' });
    const howto = h('details', { class: 'howto' },
      h('summary', null, 'How to read the verdicts and the verifier'),
      h('dl', { class: 'howto-list' },
        h('dt', null, verdictBadge('automate')), h('dd', null, 'A two-finger parallel gripper on a 6-axis cobot could do it with no tactile judgement.'),
        h('dt', null, verdictBadge('needs_fixture')), h('dd', null, 'A cobot could do it once a fixture holds or presents the part in a known position.'),
        h('dt', null, verdictBadge('keep_human')), h('dd', null, 'Needs human judgement, touch or dexterity.'),
        h('dt', null, h('span', { class: 'badge pre-badge' }, 'pre-flagged')),
        h('dd', null, 'A second model pass (the verifier) saw the same three clips and disputes the verdict or the timing by more than 1 s, or did not report on the step. Timing over 1 s is corrected; the verdict is never changed by the verifier, only by you.'),
        h('dt', null, h('span', { class: 'cite' }, 'cam1 at 00:12.0')),
        h('dd', null, 'Evidence: click to seek all three views to that moment; the camera flashes and the box the model cited is drawn on it.')));
    const stepsPanel = h('section', { class: 'card steps-panel', 'aria-labelledby': 'steps-h' },
      h('h2', { id: 'steps-h' }, 'Steps'),
      h('div', { class: 'engineer-field' }, h('label', { for: 'engineer' }, 'Engineer signing off'), this.engineer),
      howto,
      this.stepList);
    this.layoutCard = h('section', { class: 'card layout-card', 'aria-labelledby': 'layout-h' });
    this.robotCard = h('section', { class: 'card robot', 'aria-labelledby': 'robot-h' });
    this.cycleCard = h('section', { class: 'card cycle-card', 'aria-labelledby': 'cycle-h' });
    this.payCard = h('section', { class: 'card payback-card', 'aria-labelledby': 'pay-h' });
    this.foot = h('p', { class: 'foot' });

    main.replaceChildren(head, banner, this.vids, transport, this.driftOut, timeline, this.runStatus, this.runAnnounce,
      h('div', { class: 'cols' }, stepsPanel,
        h('div', { class: 'side' }, this.layoutCard, this.robotCard, this.cycleCard, this.payCard)),
      this.foot);

    if (videos.length) {
      const master = videos[0].el;
      this.player = new SyncPlayer(master, videos.slice(1), (t) => this.onTick(t));
      master.addEventListener('play', () => this.setPlaying(true));
      master.addEventListener('pause', () => this.setPlaying(false));
      master.addEventListener('loadedmetadata', () => this.onMasterMetadata());
    }
    this.updateRunButton();
    this.onTick(0);
  }

  /* ----- playback */

  now() { return this.player ? this.player.master.currentTime : 0; }

  seek(t) { this.player?.seek(t); }

  seekAndShow(t, view, reason) {
    this.seek(t);
    const r = this.vids.getBoundingClientRect();
    if (r.bottom < 0 || r.top > window.innerHeight) {
      this.vids.scrollIntoView({ behavior: reducedMotion() ? 'auto' : 'smooth', block: 'start' });
    }
    const fig = this.figures[view];
    if (fig) {
      fig.classList.remove('flash');
      void fig.offsetWidth; // restart the animation
      fig.classList.add('flash');
      setTimeout(() => fig.classList.remove('flash'), 1300);
    }
    // Hold the cited box on screen for a moment even if the playhead lands a little off.
    if (reason && Array.isArray(reason.box) && reason.box.length === 4) {
      this.pinned = { view, box: reason.box, text: reason.text, verdict: reason.verdict, name: reason.name };
      clearTimeout(this.pinTimer);
      this.pinTimer = setTimeout(() => { this.pinned = null; this.renderEvidence(this.now(), true); }, 4000);
    }
    this.renderEvidence(t, true);
  }

  /** Draw the boxes of the reasons near the playhead over their cameras (plus a pinned one). */
  renderEvidence(t, force = false) {
    const steps = this.analysis?.result?.steps;
    const on = this.evidenceBox?.checked;
    let items = on && steps ? evidenceAt(steps, t) : [];
    if (this.pinned) items = [this.pinned, ...items.filter((e) => !(e.view === this.pinned.view && e.box.join() === this.pinned.box.join()))];
    const key = items.map((e) => `${e.view}:${e.box.join(',')}`).join('|');
    if (!force && key === this.evidenceKey) return;
    this.evidenceKey = key;
    for (const [view, overlay] of Object.entries(this.overlays)) {
      const mine = items.filter((e) => e.view === view);
      const v = this.videoEls[view];
      const rect = contentRect(overlay.clientWidth, overlay.clientHeight, v?.videoWidth, v?.videoHeight);
      overlay.replaceChildren(...mine.map((e) => {
        const b = boxRect(e.box, rect);
        return h('div', { class: `ev-box v-${e.verdict || ''}`, style: { left: `${b.left}px`, top: `${b.top}px`, width: `${b.width}px`, height: `${b.height}px` } },
          h('span', { class: 'ev-label' }, e.name || e.text || ''));
      }));
    }
  }

  jump(dir) {
    const s = adjacentStart(this.segments, this.now(), dir);
    if (s != null) this.seek(s);
  }

  setSpeed(r) {
    this.player?.setRate(r);
    SPEEDS.forEach((x, i) => this.speedBtns[i].setAttribute('aria-pressed', String(x === r)));
  }

  setPlaying(p) {
    this.playBtn.textContent = p ? '❚❚' : '▶';
    this.playBtn.setAttribute('aria-label', p ? 'Pause' : 'Play');
  }

  onMasterMetadata() {
    const d = this.player.master.duration;
    if (Number.isFinite(d) && d > 0 && (!(this.duration > 0) || Math.abs(d - this.duration) > 0.5)) {
      this.duration = d;
      this.scrub.max = String(d);
      if (this.analysis?.result && this.deferredAnimate == null) this.renderTimeline(false);
    }
    if (this.deferredAnimate != null && this.duration > 0) this.layoutAndAnimate(this.deferredAnimate);
    this.onTick(this.now());
  }

  onTick(t) {
    const d = this.duration;
    this.timeOut.textContent = `${formatTime(t)} / ${formatTime(d)}`;
    this.playhead.style.left = `${d > 0 ? Math.min(100, (t / d) * 100) : 0}%`;
    if (!this.scrubbing) this.scrub.value = String(t);
    const sec = Math.floor(t);
    if (sec !== this.lastSec) {
      this.lastSec = sec;
      this.scrub.setAttribute('aria-valuetext', formatTime(t));
    }
    const i = activeSegmentIndex(this.segments, t);
    if (i !== this.activeSeg) {
      this.segEls[this.activeSeg]?.classList.remove('active');
      this.segEls[i]?.classList.add('active');
      this.activeSeg = i;
      const stepId = i === -1 ? null : this.segments[i].step_id;
      if (stepId !== this.activeStep) {
        this.stepRows.get(this.activeStep)?.classList.remove('current');
        this.stepRows.get(stepId)?.classList.add('current');
        this.activeStep = stepId;
      }
    }
    const total = totalCycles(this.analysis?.result, this.segments);
    this.cycleOut.textContent = total ? `cycle ${cycleAt(this.segments, t) ?? '–'} of ${total}` : '';
    this.renderEvidence(t);
    if (this.debugBox.checked) this.renderDrift(false);
  }

  renderDrift(force) {
    if (!this.player || this.driftOut.hidden) return;
    const now = performance.now();
    if (!force && now - this.lastDebug < 250) return;
    this.lastDebug = now;
    const ms = (d) => `${d >= 0 ? '+' : '−'}${Math.abs(d * 1000).toFixed(0)} ms`;
    const parts = this.player.followers.map((f) =>
      `${f.view} ${f.action === 'hold' ? 'waiting for its start' : ms(f.drift)} · rate ${f.el.playbackRate.toFixed(2)} · ${f.action} · seeks ${f.seeks}`);
    parts.push(this.player.useRvfc ? 'per frame (requestVideoFrameCallback)' : 'per animation frame (fallback)');
    this.driftOut.textContent = parts.join('  |  ');
  }

  /* ----- showing an analysis */

  showAnalysis(a, { animate }) {
    if (a.status === 'done' && a.result) {
      this.analysis = a;
      this.renderResult(animate);
    } else if (a.status === 'queued' || a.status === 'running') {
      if (!this.analysis) this.showNoAnalysis();
      this.resumeRun(a.id, a);
    } else {
      if (!this.analysis) this.showNoAnalysis();
      if (a.status === 'failed') this.showFailure(a, 'Analysis');
      else this.setRunStatus(h('p', { class: 'error', role: 'alert' }, 'The analysis finished without a result.'));
    }
  }

  // A failed analysis keeps the stage where it stopped; the server's error reads "Failed during …".
  showFailure(a, what) {
    const stopped = STAGE_NAMES.includes(a.stage);
    this.setRunStatus(
      stopped ? h('div', { class: 'stages' }, h('b', { class: 'stage-label' }, `${what} failed`),
        this.stageChips(stageStates('failed', a.stage))) : null,
      h('p', { class: 'error', role: 'alert' }, a.error || `${what} failed; the server gave no reason.`));
  }

  stageChips(states) {
    const chips = [];
    states.forEach(({ name, state }, i) => {
      if (i) chips.push(h('span', { class: 'arrow', 'aria-hidden': 'true' }, '→'));
      const mark = state === 'done' ? ' ✓' : state === 'run' ? ' …' : state === 'failed' ? ' ✗' : '';
      chips.push(h('span', { class: `stage ${state}` }, `${name}${mark}`));
    });
    return chips;
  }

  showNoAnalysis() {
    this.clearAnimations();
    this.bannerMain.textContent = 'No analysis yet for this session.';
    this.bannerSub.textContent = '';
    this.summary.textContent = 'Run it live to have the clips analysed.';
    this.stepList.replaceChildren(h('li', { class: 'muted' }, 'Steps appear here after an analysis.'));
    this.layoutCard.replaceChildren(h('h2', { id: 'layout-h' }, 'Layout'), h('p', { class: 'muted' }, 'A top view of the station, estimated by the analysis.'));
    this.robotCard.replaceChildren(h('h2', { id: 'robot-h' }, 'Robot'), h('p', { class: 'muted' }, 'Chosen by the analysis.'));
    this.cycleCard.replaceChildren(h('h2', { id: 'cycle-h' }, 'Cycle time'), h('p', { class: 'muted' }, 'Measured from the analysis.'));
    this.payCard.replaceChildren(h('h2', { id: 'pay-h' }, 'Payback'), h('p', { class: 'muted' }, 'Calculated from the analysis.'));
    this.foot.textContent = '';
    this.exportLink.hidden = true;
    this.reportLink.hidden = true;
    this.renderEvidence(0, true);
  }

  renderResult(animate) {
    this.clearAnimations();
    const a = this.analysis;
    const r = a.result;
    this.summary.textContent = r.summary || '';
    this.renderSteps();
    this.renderLayout();
    this.renderRobot();
    this.renderCycle();
    this.renderPayback();
    this.renderFoot();
    this.exportLink.href = `${this.base()}/analyses/${encodeURIComponent(a.id)}/export`;
    this.exportLink.hidden = false;
    this.reportLink.href = `#/s/${encodeURIComponent(this.sid)}/report/${encodeURIComponent(a.id)}`;
    this.reportLink.hidden = false;
    this.renderEvidence(this.now(), true);
    if (this.duration > 0) {
      this.layoutAndAnimate(animate);
    } else { // the timeline needs the cam0 duration: wait for the video's metadata
      this.deferredAnimate = animate;
      this.setBanner(r.counts);
    }
  }

  clearAnimations() {
    for (const id of this.timers) clearTimeout(id);
    this.timers.clear();
    cancelAnimationFrame(this.raf);
  }

  later(ms, fn) {
    const id = setTimeout(() => { this.timers.delete(id); if (this.alive) fn(); }, ms);
    this.timers.add(id);
  }

  // Steps drop onto the timeline one by one, 400 ms apart; then the banner counts up.
  layoutAndAnimate(animate) {
    this.deferredAnimate = null;
    const r = this.analysis.result;
    const anim = animate && !reducedMotion();
    this.renderTimeline(anim);
    if (!anim) { this.setBanner(r.counts); return; }
    const steps = r.steps || [];
    this.setBanner({ ...r.counts, automate: 0, keep_human: 0, needs_fixture: 0 }, true);
    steps.forEach((_, i) => this.later(i * DROP_MS, () => {
      for (const el of this.segEls) {
        if (el.dataset.stepIndex === String(i)) { el.classList.remove('pending'); el.classList.add('drop'); }
      }
    }));
    this.later(Math.max(0, steps.length - 1) * DROP_MS + 350, () => this.countUp(r.counts));
  }

  countUp(counts) {
    const keys = ['automate', 'keep_human', 'needs_fixture'];
    const t0 = performance.now();
    const dur = 900;
    const frame = (now) => {
      if (!this.alive) return;
      const p = Math.min(1, (now - t0) / dur);
      const e = 1 - (1 - p) ** 3;
      const c = { ...counts };
      for (const k of keys) c[k] = Math.round((Number(counts?.[k]) || 0) * e);
      this.setBanner(c, p < 1);
      if (p < 1) this.raf = requestAnimationFrame(frame);
    };
    this.raf = requestAnimationFrame(frame);
  }

  setBanner(counts, counting = false) {
    this.bannerMain.classList.toggle('counting', counting);
    this.bannerMain.replaceChildren(...bannerPieces(counts).map((p) =>
      (p.kind ? h('strong', { class: `n v-${p.kind}` }, p.text) : p.text)));
    this.updateEngineerLine();
  }

  updateEngineerLine() {
    const r = this.analysis?.result;
    this.bannerSub.textContent = r ? engineerLine(r.steps, this.analysis.signoffs) : '';
  }

  renderTimeline(pending) {
    const r = this.analysis?.result;
    const { segments, lanes } = layoutTimeline(r?.steps, this.duration);
    this.segments = segments;
    this.track.style.setProperty('--lanes', String(lanes));
    this.segEls = segments.map((s) => {
      const label = `${s.name}, cycle ${s.cycle ?? '?'}, ${formatTime(s.start_s)} to ${formatTime(s.end_s)}, `
        + `${VERDICT_LABEL[s.verdict] || s.verdict}${s.preflagged ? ', pre-flagged: the verifier disputes it' : ''}`;
      const el = h('button', {
        type: 'button', tabindex: '-1', title: label, 'aria-label': label,
        class: `seg v-${s.verdict}${s.preflagged ? ' pre' : ''}${pending ? ' pending' : ''}`,
        dataset: { stepIndex: String(s.step_index) },
        style: { left: `${s.left_pct}%`, width: `${s.width_pct}%`, top: `calc(${s.lane} * var(--lane-h))` },
      }, h('span', null, s.name));
      el.addEventListener('click', (e) => { e.stopPropagation(); this.seek(s.start_s); });
      return el;
    });
    this.track.replaceChildren(...this.segEls, this.playhead);
    this.activeSeg = -1;
    this.stepRows.get(this.activeStep)?.classList.remove('current');
    this.activeStep = null;
    this.onTick(this.now());
  }

  /* ----- S3 steps and sign-off */

  renderSteps() {
    const steps = this.analysis.result.steps || [];
    this.stepRows = new Map();
    this.soEls = new Map();
    this.activeStep = null;
    if (!steps.length) {
      this.stepList.replaceChildren(h('li', { class: 'muted' }, 'The analysis found no steps.'));
      return;
    }
    this.stepList.replaceChildren(...steps.map((s, i) => this.stepRow(s, i)));
    this.updateSignoffs();
  }

  stepRow(s, i) {
    const occ = Array.isArray(s.occurrences) ? s.occurrences : [];
    const first = occ.reduce((m, o) => Math.min(m, Number.isFinite(num(o.start_s)) ? num(o.start_s) : Infinity), Infinity);
    const name = `${i + 1} · ${s.name}`;
    const title = h('h3', { class: 'step-title' }, Number.isFinite(first)
      ? h('button', { type: 'button', class: 'linkish', title: `Play from ${formatTime(first)}`, onclick: () => this.seekAndShow(first) }, name)
      : name);
    const badges = h('span', { class: 'badges' }, verdictBadge(s.verdict),
      s.preflagged ? h('span', { class: 'badge pre-badge' }, 'pre-flagged') : null);
    const mean = num(s.mean_duration_s);
    const conf = num(s.confidence);
    const meta = [
      Number.isFinite(mean) ? `${mean.toFixed(1)} s mean` : null,
      `${occ.length} occurrence${occ.length === 1 ? '' : 's'}`,
      Number.isFinite(conf) ? `confidence ${conf.toFixed(2)}` : null,
      verifierSummary(s.verifier),
      Array.isArray(s.parts) && s.parts.length ? `parts: ${s.parts.join(', ')}` : null,
    ].filter(Boolean).join(' · ');
    const reasons = Array.isArray(s.reasons) && s.reasons.length
      ? h('ul', { class: 'reasons' }, s.reasons.map((rs) => {
        const t = num(rs.t_s);
        const view = rs.view || 'cam0';
        const boxed = Array.isArray(rs.box) && rs.box.length === 4;
        return h('li', null, rs.text || '', Number.isFinite(t) ? [' ', h('button', {
          type: 'button', class: `cite${boxed ? ' boxed' : ''}`,
          'aria-label': `Show ${view} at ${formatTime(t)}${boxed ? ', with the cited box drawn on it' : ''}`,
          title: boxed ? 'Seek there and draw the box the model cited' : 'Seek there',
          onclick: () => this.seekAndShow(t, view, { ...rs, verdict: s.verdict, name: s.name }),
        }, `${view} at ${formatTime(t)}`, boxed ? h('span', { class: 'box-mark', 'aria-hidden': 'true' }, ' ▢') : null)] : null);
      }))
      : null;
    const vnote = s.verifier?.note ? h('p', { class: 'vnote' }, h('b', null, 'Verifier: '), s.verifier.note) : null;

    const noteIn = h('input', {
      type: 'text', class: 'so-note', maxlength: '500', placeholder: 'Note (optional)',
      'aria-label': `Note for step ${name}`, value: this.analysis.signoffs?.[s.id]?.note || '',
    });
    const approve = h('button', {
      type: 'button', class: 'btn ok', 'aria-pressed': 'false', 'aria-label': `Approve step ${name}`,
      onclick: () => this.signoff(s.id, 'approved'),
    }, '✓ Approve');
    const flag = h('button', {
      type: 'button', class: 'btn flag', 'aria-pressed': 'false', 'aria-label': `Flag step ${name}`,
      onclick: () => this.signoff(s.id, 'flagged'),
    }, '⚑ Flag');
    const status = h('p', { class: 'so-status' });
    const err = h('p', { class: 'so-error', role: 'alert' });
    const row = h('li', { class: `steprow${s.preflagged ? ' pre' : ''}`, dataset: { step: s.id } },
      h('div', { class: 'top' }, title, badges),
      h('p', { class: 'meta' }, meta),
      reasons, vnote,
      h('div', { class: 'signoff' }, status, h('div', { class: 'so-actions' }, noteIn, approve, flag), err));
    this.stepRows.set(s.id, row);
    this.soEls.set(s.id, { row, noteIn, approve, flag, status, err });
    return row;
  }

  updateSignoffs() {
    const so = this.analysis?.signoffs || {};
    for (const [id, els] of this.soEls) {
      const d = so[id];
      els.approve.setAttribute('aria-pressed', String(d?.decision === 'approved'));
      els.flag.setAttribute('aria-pressed', String(d?.decision === 'flagged'));
      els.row.classList.toggle('approved', d?.decision === 'approved');
      els.row.classList.toggle('flagged', d?.decision === 'flagged');
      els.status.textContent = d
        ? [d.decision === 'approved' ? 'Approved' : 'Flagged',
          d.engineer ? `by ${d.engineer}` : null,
          d.at ? `· ${fmtDateTime(d.at)}` : null,
          d.note ? `· “${d.note}”` : null].filter(Boolean).join(' ')
        : 'Not signed off yet';
    }
    this.updateEngineerLine();
  }

  async signoff(stepId, decision) {
    const els = this.soEls.get(stepId);
    if (!els) return;
    els.err.textContent = '';
    const engineer = this.engineer.value.trim();
    if (!engineer) {
      els.err.textContent = 'Type your name in “Engineer signing off” first.';
      this.engineer.focus();
      return;
    }
    const a = this.analysis;
    els.approve.disabled = true;
    els.flag.disabled = true;
    const r = await api(
      `${this.base()}/analyses/${encodeURIComponent(a.id)}/signoffs/${encodeURIComponent(stepId)}`,
      { method: 'PUT', body: { decision, note: els.noteIn.value.trim(), engineer } });
    els.approve.disabled = false;
    els.flag.disabled = false;
    if (!this.alive || this.analysis?.id !== a.id) return;
    if (!r.ok || !r.data) {
      els.err.textContent = `Not saved: ${r.error || 'no answer from the server'}`;
      return;
    }
    // Re-render from the server's copy of the analysis.
    this.analysis = r.data.result ? r.data : { ...this.analysis, signoffs: r.data.signoffs || {} };
    this.updateSignoffs();
  }

  /* ----- S4 layout, robot, cycle */

  renderLayout() {
    const r = this.analysis.result;
    const lay = r.layout;
    const body = [];
    if (!lay) {
      body.push(h('p', { class: 'muted' }, 'No layout for this result.'),
        r.layout_error ? h('p', { class: 'small muted' }, r.layout_error) : null);
    } else {
      const pic = drawLayout(lay, r.robot);
      body.push(h('div', { class: 'layout-wrap' }, pic), layoutLegend(lay, r.robot));
      const conf = num(lay.confidence);
      body.push(h('p', { class: 'small muted' },
        `Top view, bench frame. Bench ${formatMetres(lay.bench?.length_m, lay.bench?.width_m)}; `,
        `${(lay.objects || []).length} object${(lay.objects || []).length === 1 ? '' : 's'}, `,
        `${(lay.anchors || []).length} pick or place point${(lay.anchors || []).length === 1 ? '' : 's'}`,
        Number.isFinite(conf) ? `; scale confidence ${conf.toFixed(2)}` : '', '. ',
        lay.notes ? h('span', null, lay.notes, ' ') : null));
      body.push(h('p', { class: 'fine' },
        'Positions and sizes are the model’s estimate from the three views, not measurements. ',
        lay.robot_base ? `Robot base: ${lay.robot_base.rule}. ` : '',
        lay.reach_rule ? `Needed reach: ${lay.reach_rule}. ` : '',
        'A measured layout from a calibration board is the next step.'));
    }
    this.layoutCard.replaceChildren(h('h2', { id: 'layout-h' }, 'Layout ', lay ? estimateBadge() : null), ...body.filter(Boolean));
  }

  renderRobot() {
    const rb = this.analysis.result.robot;
    const body = [];
    if (!rb) {
      body.push(h('p', { class: 'muted' }, 'No robot was chosen for this result.'));
    } else {
      body.push(h('p', { class: 'robot-model' }, h('b', null, rb.model || 'Unnamed robot'),
        rb.vendor ? h('span', { class: 'muted' }, ` ${rb.vendor}`) : null, ' ',
        rb.fits === true ? h('span', { class: 'badge ok' }, 'passes the checks') : null,
        rb.fits === false ? h('span', { class: 'badge bad' }, 'does not fit') : null));
      const reachNeed = num(rb.needed_reach_mm);
      body.push(h('dl', { class: 'kv' },
        h('dt', null, 'Payload'), h('dd', null, fitLine(num(rb.payload_kg), num(rb.needed_payload_kg), 'kg')),
        h('dt', null, 'Reach'), h('dd', null, fitLine(num(rb.reach_mm), reachNeed, 'mm',
          Number.isFinite(reachNeed) ? 'from the estimated layout' : (rb.reach_basis || 'not checked'))),
        rb.gripper ? [h('dt', null, 'Gripper'), h('dd', null, rb.gripper)] : null,
        rb.price_band ? [h('dt', null, 'Price'), h('dd', null, `${priceText(rb.price_band)} `,
          h('span', { class: 'badge', title: 'Rough list-price band from public reseller listings; not a quote' }, 'indicative'))] : null));
      const table = robotTable(rb);
      if (table) body.push(h('h3', { class: 'sub' }, 'All arms in the catalogue'), h('div', { class: 'table-wrap' }, table));
      if (rb.why) body.push(h('details', { class: 'why-d' }, h('summary', null, 'Why this one'), h('p', { class: 'why' }, rb.why)));
      body.push(h('p', { class: 'fine' },
        'Payload and reach are the vendors’ published figures',
        rb.source ? [' (', h('a', { href: rb.source, target: '_blank', rel: 'noopener' }, 'source'), ')'] : null,
        '. The rule: the smallest arm that carries the heaviest automatable part plus a 1 kg gripper allowance and reaches every pick and place within 85 % of its rated reach. Price bands are indicative, not quotes.'));
    }
    this.robotCard.replaceChildren(h('h2', { id: 'robot-h' }, 'Robot'), ...body);
  }

  renderCycle() {
    const r = this.analysis.result;
    const ce = r.cycle_estimate;
    const factor0 = Number.isFinite(num(ce?.speed_factor)) ? num(ce.speed_factor) : 1.3;
    const body = [];
    const cycle = num(r.cycle_time_s);
    body.push(h('p', { class: 'small' },
      cycle > 0 ? [h('b', null, `${cycle.toFixed(1)} s per cycle`), ` today, measured over ${r.cycles || '?'} cycle${r.cycles === 1 ? '' : 's'} as the spacing of cycle starts.`]
        : 'No measured cycle time.'));
    body.push(...[].concat(cycleBar(r)));
    this.factorIn = h('input', { id: 'speed-factor', type: 'number', inputmode: 'decimal', min: '0.5', max: '5', step: '0.1', value: String(factor0) });
    this.cycleOutEl = h('dl', { class: 'kv est-out' });
    this.factorIn.addEventListener('input', () => this.recomputeCycle());
    body.push(h('h3', { class: 'sub' }, 'With a cobot ', estimateBadge('estimate')),
      h('div', { class: 'factor-row' }, h('label', { for: 'speed-factor' }, 'Robot speed factor (× the operator’s time)'), this.factorIn),
      this.cycleOutEl,
      h('p', { class: 'fine' }, ce?.method || 'Robot time = the operator’s time on the automate steps × the factor; no robot motion was planned or simulated.'));
    this.cycleCard.replaceChildren(h('h2', { id: 'cycle-h' }, 'Cycle time'), ...body);
    this.recomputeCycle();
  }

  recomputeCycle() {
    const r = this.analysis?.result;
    const est = robotCycle(r, this.factorIn?.value);
    const bad = !est;
    this.factorIn?.setAttribute('aria-invalid', String(bad));
    const s = (x) => (bad ? '–' : `${x.toFixed(1)} s`);
    this.cycleOutEl?.replaceChildren(
      h('dt', null, 'Robot, its steps'), h('dd', null, bad ? '–' : `${s(est.robot_cycle_s)} (${est.automated_steps_s.toFixed(1)} s × ${est.speed_factor})`),
      h('dt', null, 'Operator, the rest'), h('dd', null, bad ? '–' : `${s(est.operator_cycle_s)}${est.unassigned_s > 0.05 ? ` + ${est.unassigned_s.toFixed(1)} s uncovered` : ''}`),
      h('dt', null, 'Side by side'), h('dd', null, h('b', null, s(bad ? 0 : est.parallel_cycle_s)),
        bad || !(est.human_cycle_s > 0) ? '' : ` vs ${est.human_cycle_s.toFixed(1)} s today`));
  }

  /* ----- S5 payback */

  renderPayback() {
    const r = this.analysis.result;
    const d = r.roi_defaults || {};
    const cur = d.currency || 'SGD';
    this.roiCurrency = cur;
    this.roiShare = automatedShare(r);
    const fields = [
      ['wage_per_h', `Wage per hour (${cur})`, '0.5', 1],
      ['shifts', 'Shifts a day', '1', 1],
      ['hours_per_shift', 'Hours per shift', '0.5', 1],
      ['days_per_year', 'Working days a year', '1', 1],
      ['robot_price', `Robot price (${cur}, indicative)`, '1000', 1],
      ['system_multiple', 'System cost multiple (×)', '0.1', 1],
      ['grant_share', 'Grant share (%)', '5', 100],
      ['grant_cap', `Grant cap (${cur})`, '1000', 1],
    ];
    this.roiInputs = {};
    const grid = h('div', { class: 'pay-grid' }, fields.map(([key, label, step, scale]) => {
      const id = `roi-${key}`;
      const v = num(d[key]);
      const def = Number.isFinite(v) ? String(+(v * scale).toFixed(6)) : '';
      const input = h('input', {
        id, type: 'number', inputmode: 'decimal', min: '0', max: scale === 100 ? '100' : '1000000000', step, value: def,
      });
      input.addEventListener('input', () => this.recompute());
      this.roiInputs[key] = { input, scale, def };
      return h('div', { class: 'field' }, h('label', { for: id }, label), input);
    }));
    const cycle = num(r.cycle_time_s);
    const shareLine = h('p', { class: 'small' },
      `Automatable share of the cycle: ${Math.round(this.roiShare * 100)} %`,
      cycle > 0 ? ` (${(this.roiShare * cycle).toFixed(1)} s of a ${cycle.toFixed(1)} s cycle${r.cycles ? `, ${r.cycles} cycles` : ''})` : '',
      ', from the analysis.');
    this.payOut = h('dl', { class: 'kv pay-out' });
    this.payResult = h('p', { class: 'payback-result', 'aria-live': 'polite' });
    const reset = h('button', { type: 'button', class: 'btn', onclick: () => this.resetRoi() }, 'Reset to defaults');
    const fine = h('p', { class: 'fine' },
      'Investment = robot price × system multiple: a complete robot cell typically costs 3 to 5 times the robot itself '
      + '(Automation World; Fraunhofer IPA). Grant: Singapore’s Productivity Solutions Grant, PSG (Enterprise Singapore); '
      + 'share and cap are editable, check the current terms. Robot price indicative.');
    this.payCard.replaceChildren(h('h2', { id: 'pay-h' }, 'Payback'), shareLine, grid, this.payOut,
      h('div', { class: 'pay-foot' }, this.payResult, reset), fine);
    this.recompute();
  }

  resetRoi() {
    for (const { input, def } of Object.values(this.roiInputs)) input.value = def;
    this.recompute();
  }

  recompute() {
    const vals = {};
    let bad = false;
    for (const [k, { input, scale }] of Object.entries(this.roiInputs)) {
      const raw = input.value.trim();
      const v = raw === '' ? NaN : Number(raw) / scale;
      const invalid = !Number.isFinite(v) || v < 0 || v > (k === 'grant_share' ? 1 : 1e9);
      input.setAttribute('aria-invalid', String(invalid));
      bad ||= invalid;
      vals[k] = v;
    }
    const p = computePayback(vals, this.roiShare);
    const m = (n) => (bad ? '–' : formatMoney(n, this.roiCurrency));
    this.payOut.replaceChildren(
      h('dt', null, 'Investment'), h('dd', null, m(p.investment)),
      h('dt', null, 'Grant'), h('dd', null, bad ? '–' : `− ${m(p.grant)}`),
      h('dt', null, 'Net investment'), h('dd', null, m(p.net_investment)),
      h('dt', null, 'Labour cost a year'), h('dd', null, m(p.annual_labour)),
      h('dt', null, 'Savings a year'), h('dd', null, m(p.annual_savings)));
    this.payResult.textContent = bad
      ? 'Check the highlighted fields.'
      : p.payback_months == null
        ? 'No payback: the annual savings are zero.'
        : `Payback ≈ ${p.payback_months.toFixed(1)} months`;
    this.payResult.classList.toggle('none', bad || p.payback_months == null);
  }

  /* ----- footer and export */

  renderFoot() {
    const a = this.analysis;
    const u = a.usage || {};
    const pt = num(u.prompt_tokens);
    const ot = num(u.output_tokens);
    const secs = num(u.seconds);
    const bits = [
      a.live ? 'Live run' : 'Stored sample result',
      a.model ? `model ${a.model}` : null,
      Number.isFinite(pt) ? `${fmtN(pt, 0)} prompt tokens` : null,
      Number.isFinite(ot) ? `${fmtN(ot, 0)} output tokens (incl. thinking)` : null,
      Number.isFinite(secs) ? fmtSeconds(secs) : null,
      a.finished_at ? `finished ${fmtDateTime(a.finished_at)}` : null,
      a.id ? `analysis ${a.id}` : null,
    ].filter(Boolean);
    this.foot.replaceChildren(h('span', { class: `src ${a.live ? 'live' : 'stored'}` }, bits[0]), ` · ${bits.slice(1).join(' · ')}`);
  }

  /* ----- live run */

  updateRunButton() {
    if (!this.runBtn) return;
    const running = Boolean(this.run);
    this.runBtn.disabled = running || this.liveLeft <= 0;
    this.runBtn.textContent = running
      ? 'Live run in progress…'
      : this.liveLeft > 0 ? `Run it live · ${this.liveLeft} left today` : 'No live runs left today';
  }

  setRunStatus(...nodes) { this.runStatus.replaceChildren(...nodes.flat().filter((n) => n != null && n !== false)); }

  announce(text) { if (this.runAnnounce.textContent !== text) this.runAnnounce.textContent = text; }

  async startRun() {
    if (this.run || !this.alive) return;
    this.runBtn.disabled = true;
    this.setRunStatus(h('p', { class: 'muted' }, 'Starting a live run…'));
    const r = await api(`${this.base()}/analyses`, { method: 'POST' });
    if (!this.alive) return;
    if (r.status === 429) {
      this.liveLeft = 0;
      this.updateRunButton();
      this.setRunStatus(h('p', { class: 'error', role: 'alert' }, r.error));
      return;
    }
    if (!r.ok || !r.data?.id) {
      this.updateRunButton();
      this.setRunStatus(h('p', { class: 'error', role: 'alert' },
        `Could not start a live run: ${r.error || 'the server did not return an analysis id'}`));
      return;
    }
    this.liveLeft = Math.max(0, this.liveLeft - 1);
    this.beginRun(r.data.id, Date.now());
  }

  resumeRun(aid, snapshot) {
    if (this.run?.aid === aid) return;
    if (this.run) return;
    this.beginRun(aid, null, snapshot);
  }

  beginRun(aid, clientStart, snapshot) {
    this.run = {
      aid, clientStart,
      serverStart: snapshot?.started_at || null,
      status: snapshot?.status || 'queued',
      stage: snapshot?.stage || 'queued',
      note: null,
    };
    storage.set('sessionStorage', runKey(this.sid), aid);
    this.updateRunButton();
    this.renderRunProgress();
    this.elapsedTimer = setInterval(() => this.renderRunProgress(), 1000);
    this.poll();
  }

  async poll() {
    const run = this.run;
    if (!run || !this.alive) return;
    const r = await api(`${this.base()}/analyses/${encodeURIComponent(run.aid)}`);
    if (!this.alive || this.run !== run) return;
    if (r.ok && r.data) {
      const a = r.data;
      run.status = a.status;
      run.stage = a.stage;
      run.serverStart = a.started_at || run.serverStart;
      run.note = null;
      if (a.status === 'done') {
        const took = this.elapsedText(run);
        this.endRun();
        if (a.result) {
          this.setRunStatus(h('p', { class: 'ok-msg' }, `Live run finished${took ? ` in ${took}` : ''}.`));
          this.announce('Live run finished. Showing the new result.');
          this.showAnalysis(a, { animate: true });
          this.bannerMain.scrollIntoView({ behavior: reducedMotion() ? 'auto' : 'smooth', block: 'center' });
        } else {
          this.setRunStatus(h('p', { class: 'error', role: 'alert' }, 'The live run finished without a result.'));
        }
        return;
      }
      if (a.status === 'failed') {
        this.endRun();
        this.showFailure(a, 'Live run');
        this.announce('The live run failed.');
        return;
      }
    } else if (r.status === 404) {
      this.endRun();
      this.setRunStatus(h('p', { class: 'error', role: 'alert' }, 'The live run could not be found on the server.'));
      return;
    } else {
      run.note = `Could not check progress (${r.error}). Trying again.`;
    }
    this.renderRunProgress();
    this.pollTimer = setTimeout(() => this.poll(), POLL_MS);
  }

  endRun() {
    clearTimeout(this.pollTimer);
    clearInterval(this.elapsedTimer);
    storage.set('sessionStorage', runKey(this.sid), null);
    this.run = null;
    this.updateRunButton();
  }

  elapsedText(run) {
    const start = run.clientStart ?? Date.parse(run.serverStart);
    return Number.isFinite(start) ? formatClock(Math.max(0, (Date.now() - start) / 1000)) : '';
  }

  renderRunProgress() {
    const run = this.run;
    if (!run) return;
    const queued = run.status === 'queued' || run.stage === 'queued';
    const elapsed = this.elapsedText(run);
    this.setRunStatus(
      h('div', { class: 'stages' },
        h('b', { class: 'stage-label' }, queued ? 'Live run queued' : 'Live run'),
        this.stageChips(stageStates(run.status, run.stage)),
        elapsed ? h('span', { class: 'elapsed' }, elapsed) : null),
      run.note ? h('p', { class: 'muted small' }, run.note) : null,
      h('p', { class: 'muted small' }, 'A live run takes a few minutes. The result on screen stays until the new one arrives.'));
    this.announce(queued ? 'Live run queued.' : `Live run: ${run.stage}.`);
  }

  destroy() {
    this.alive = false;
    this.clearAnimations();
    clearTimeout(this.pollTimer);
    clearTimeout(this.pinTimer);
    clearInterval(this.elapsedTimer);
    removeEventListener('resize', this.onResize);
    this.player?.destroy();
  }
}

/* ---------------------------------------------------------------- report */

// The printable study: one document a judge or a manager can read without the player.
class ReportView {
  constructor(sid, aid) {
    this.sid = sid;
    this.aid = aid;
    this.alive = true;
    document.title = 'CellPilot · Study';
    main.replaceChildren(backLink(`#/s/${encodeURIComponent(sid)}`, '← Back to the analysis'), h('p', { class: 'muted' }, 'Loading the study…'));
    this.load();
  }

  async load() {
    const base = `/api/sessions/${encodeURIComponent(this.sid)}`;
    const r = await api(base);
    if (!this.alive) return;
    if (!r.ok || !r.data?.session) {
      main.replaceChildren(backLink(), errorBox(r.status === 404 ? `There is no session called “${this.sid}”.` : (r.error || 'Unexpected answer from the server.')));
      return;
    }
    let a = r.data.latest;
    if (this.aid && this.aid !== a?.id) {
      const ra = await api(`${base}/analyses/${encodeURIComponent(this.aid)}`);
      if (!this.alive) return;
      a = ra.ok ? ra.data : null;
    }
    if (!a || a.status !== 'done' || !a.result) {
      main.replaceChildren(backLink(`#/s/${encodeURIComponent(this.sid)}`, '← Back to the analysis'),
        errorBox('This analysis has no finished result to print.'));
      return;
    }
    this.render(r.data.session, a);
  }

  render(s, a) {
    const r = a.result;
    document.title = `CellPilot · Study · ${s.name || s.id}`;
    document.body.classList.add('printing');
    const back = `#/s/${encodeURIComponent(this.sid)}`;
    const so = a.signoffs || {};
    const tally = signoffSummary(r.steps, so);
    const ce = r.cycle_estimate;
    const pay = computePayback(r.roi_defaults || {}, automatedShare(r));
    const cur = r.roi_defaults?.currency || 'SGD';
    const clips = Array.isArray(s.clips) ? s.clips : [];

    const stepRows = (r.steps || []).map((st, i) => {
      const d = so[st.id];
      const occ = Array.isArray(st.occurrences) ? st.occurrences : [];
      return h('tr', { class: st.preflagged ? 'pre' : '' },
        h('td', null, h('b', null, `${i + 1} · ${st.name}`),
          h('div', { class: 'small muted' }, `${occ.length} occurrence${occ.length === 1 ? '' : 's'} · mean ${fmtN(num(st.mean_duration_s), 1)} s · confidence ${fmtN(num(st.confidence))}`),
          h('ul', { class: 'reasons small' }, (st.reasons || []).map((rs) => h('li', null, rs.text, ' ', h('span', { class: 'cite' }, `${rs.view} at ${formatTime(num(rs.t_s))}`)))),
          st.verifier?.note ? h('div', { class: 'small' }, h('b', null, 'Verifier: '), st.verifier.note) : null),
        h('td', null, verdictBadge(st.verdict), st.preflagged ? [' ', h('span', { class: 'badge pre-badge' }, 'pre-flagged')] : null,
          h('div', { class: 'small muted' }, verifierSummary(st.verifier))),
        h('td', null, d ? [h('b', { class: d.decision === 'approved' ? 'good' : 'bad' }, d.decision === 'approved' ? 'Approved' : 'Flagged'),
          h('div', { class: 'small muted' }, [d.engineer, d.at ? fmtDateTime(d.at) : null].filter(Boolean).join(' · ')),
          d.note ? h('div', { class: 'small' }, `“${d.note}”`) : null] : h('span', { class: 'muted' }, 'not signed off')));
    });

    const parts = (r.parts || []).map((p) => `${p.name} (~${fmtN(num(p.mass_g), 0)} g, ${(p.size_mm || []).map((x) => fmtN(num(x), 0)).join('×')} mm)`).join('; ');
    const lay = r.layout;
    const rb = r.robot;

    main.replaceChildren(
      h('div', { class: 'report-actions no-print' },
        backLink(back, '← Back to the analysis'),
        h('button', { type: 'button', class: 'btn primary', onclick: () => window.print() }, 'Print or save as PDF'),
        h('a', { class: 'btn', download: '', href: `/api/sessions/${encodeURIComponent(this.sid)}/analyses/${encodeURIComponent(a.id)}/export` }, 'Export JSON')),
      h('article', { class: 'report' },
        h('header', { class: 'report-head' },
          h('p', { class: 'kicker' }, 'CellPilot feasibility study'),
          h('h1', null, s.name || s.id),
          h('p', { class: 'muted' }, [
            s.recorded ? `recorded ${s.recorded}` : null, `${clips.length} cameras`,
            clips[0] ? formatClock(num(clips[0].duration_s)) : null, s.synthetic ? 'synthetic test clips' : null,
            a.live ? 'live run' : 'stored result', a.model ? `model ${a.model}` : null,
            a.finished_at ? `analysed ${fmtDateTime(a.finished_at)}` : null].filter(Boolean).join(' · '))),
        h('section', { class: 'report-banner' },
          h('h2', null, bannerText(r.counts)),
          h('p', null, h('b', null, `Engineer: ${tally.approved} of ${tally.total} approved${tally.flagged ? `, ${tally.flagged} flagged` : ''}`),
            tally.approved + tally.flagged < tally.total ? ` · ${tally.total - tally.approved - tally.flagged} not yet signed off` : ''),
          h('p', null, r.summary || '')),
        h('section', null, h('h2', null, 'Steps, evidence and sign-off'),
          h('table', { class: 'report-steps' },
            h('thead', null, h('tr', null, h('th', null, 'Step and evidence'), h('th', null, 'Verdict'), h('th', null, 'Sign-off'))),
            h('tbody', null, stepRows)),
          parts ? h('p', { class: 'small muted' }, 'Parts seen (estimated): ', parts) : null),
        h('section', { class: 'report-two' },
          h('div', null, h('h2', null, 'Layout ', lay ? estimateBadge() : null),
            lay ? [h('div', { class: 'layout-wrap' }, drawLayout(lay, rb)), layoutLegend(lay, rb),
              h('p', { class: 'small muted' }, `Bench ${formatMetres(lay.bench?.length_m, lay.bench?.width_m)}; scale confidence ${fmtN(num(lay.confidence))}. ${lay.notes || ''} Positions are the model’s estimate from the three views, not measurements.`)]
              : h('p', { class: 'muted' }, r.layout_error || 'No layout for this result.')),
          h('div', null, h('h2', null, 'Robot'),
            rb ? [h('p', null, h('b', null, rb.model), rb.vendor ? ` (${rb.vendor})` : '', rb.fits === false ? ' · does not fit' : ' · passes the checks'),
              h('dl', { class: 'kv' },
                h('dt', null, 'Payload'), h('dd', null, fitLine(num(rb.payload_kg), num(rb.needed_payload_kg), 'kg')),
                h('dt', null, 'Reach'), h('dd', null, fitLine(num(rb.reach_mm), num(rb.needed_reach_mm), 'mm', Number.isFinite(num(rb.needed_reach_mm)) ? 'estimated layout' : (rb.reach_basis || 'not checked'))),
                rb.price_band ? [h('dt', null, 'Price'), h('dd', null, `${priceText(rb.price_band)} (indicative)`)] : null),
              h('div', { class: 'table-wrap' }, robotTable(rb)),
              h('p', { class: 'small' }, rb.why)] : h('p', { class: 'muted' }, 'No robot chosen.'))),
        h('section', { class: 'report-two' },
          h('div', null, h('h2', null, 'Cycle time'),
            h('p', null, h('b', null, `${fmtN(num(r.cycle_time_s), 1)} s`), ` per cycle today over ${r.cycles || '?'} cycles; automatable share ${Math.round(automatedShare(r) * 100)} %.`),
            ...[].concat(cycleBar(r)),
            ce ? h('p', { class: 'small' }, h('b', null, 'With a cobot (estimate): '),
              `robot ${fmtN(num(ce.robot_cycle_s), 1)} s on its steps (× ${ce.speed_factor}), operator ${fmtN(num(ce.operator_cycle_s), 1)} s, side by side ${fmtN(num(ce.parallel_cycle_s), 1)} s. ${ce.method}`) : null),
          h('div', null, h('h2', null, 'Payback at the default inputs'),
            h('dl', { class: 'kv' },
              h('dt', null, 'Inputs'), h('dd', null, `${cur} ${fmtN(num(r.roi_defaults?.wage_per_h))}/h · ${r.roi_defaults?.shifts} × ${r.roi_defaults?.hours_per_shift} h · ${r.roi_defaults?.days_per_year} days · robot ${formatMoney(num(r.roi_defaults?.robot_price), cur)} (indicative) × ${r.roi_defaults?.system_multiple} · grant ${Math.round(num(r.roi_defaults?.grant_share) * 100)} % capped at ${formatMoney(num(r.roi_defaults?.grant_cap), cur)}`),
              h('dt', null, 'Investment'), h('dd', null, formatMoney(pay.investment, cur)),
              h('dt', null, 'Grant'), h('dd', null, `− ${formatMoney(pay.grant, cur)}`),
              h('dt', null, 'Savings a year'), h('dd', null, formatMoney(pay.annual_savings, cur)),
              h('dt', null, 'Payback'), h('dd', null, h('b', null, pay.payback_months == null ? 'none (no savings)' : `≈ ${pay.payback_months.toFixed(1)} months`))),
            h('p', { class: 'fine' }, 'Investment = robot price × system multiple (a complete cell costs 3 to 5 × the robot: Automation World; Fraunhofer IPA). Grant: Singapore’s Productivity Solutions Grant (Enterprise Singapore); check the current terms. Edit every input on the analysis page.'))),
        h('footer', { class: 'report-foot' },
          `Analysis ${a.id} · model ${a.model || '?'} · ${fmtN(num(a.usage?.prompt_tokens), 0)} prompt and ${fmtN(num(a.usage?.output_tokens), 0)} output tokens · ${fmtSeconds(num(a.usage?.seconds))} · `,
          'Every verdict, time and reason comes from the model’s stored answer; the robot choice, cycle and payback arithmetic are the app’s code. ',
          'Printed ', fmtDateTime(new Date().toISOString()), '.')));
  }

  destroy() { this.alive = false; document.body.classList.remove('printing'); }
}

/* ----------------------------------------------------------------- guide */

// The capture guide: how footage gets in, drawn in SVG, no images.
class GuideView {
  constructor() {
    this.alive = true;
    document.title = 'CellPilot · Capture guide';
    main.replaceChildren(backLink(), h('h1', { class: 'page-title' }, 'Capture guide'),
      h('p', { class: 'lead muted' }, 'What CellPilot needs from the shop floor: three static cameras around one bench, started together, and 8 to 10 cycles of the real job.'),
      h('div', { class: 'guide' },
        h('section', { class: 'card' }, h('h2', null, '1 · Where the cameras go'), h('div', { class: 'guide-pic' }, GuideView.placement()),
          h('ul', null,
            h('li', null, h('b', null, 'cam0'), ' faces the operator from across the bench, about 1.5 to 2 m away and 1.6 to 1.8 m high, tilted down: it sees the hands, the parts and the bins. It is the master clock.'),
            h('li', null, h('b', null, 'cam1'), ' and ', h('b', null, 'cam2'), ' stand to the left and right, roughly 45° off cam0, at the same distance. Three views from different sides let the verifier check each step and let the model place objects on the bench.'),
            h('li', null, 'Every camera must see the whole bench top and both hands all the time. Nothing between a camera and the bench; no one walking through.'),
            h('li', null, 'Fix each camera on a tripod or a clamp. A camera that moves breaks the layout estimate.'))),
        h('section', { class: 'card' }, h('h2', null, '2 · Start them together'),
          h('p', null, 'All three clips must share one clock. The result times are ', h('b', null, 'cam0 seconds'), '; each other clip carries ', h('code', null, 'offset_s'), ', the cam0 time at which it starts.'),
          h('ul', null,
            h('li', null, h('b', null, 'NODAL M1'), ' (our reference rig): one RF trigger starts every camera within a frame, and each camera records how long after the trigger its first frame came. The offsets are those delays, differenced against cam0, read straight from the sidecars: no clapper, no guessing.'),
            h('li', null, h('b', null, 'Other cameras:'), ' start recording on all three, then clap once (or flash a light) in view of every camera. Find the clap’s frame in each clip; the offset of a clip is the clap’s time in cam0 minus its time in that clip. Write the offsets into session.json. Accuracy of one frame (33 ms at 30 fps) is enough.')),
          h('pre', { class: 'code' }, GuideView.SESSION_JSON)),
        h('section', { class: 'card' }, h('h2', null, '3 · What good footage looks like'),
          h('ul', null,
            h('li', null, '8 to 10 complete cycles of the real job, by the usual operator, at the usual pace. Short clips (one or two cycles) give unreliable cycle times.'),
            h('li', null, 'Even light, no strong backlight; parts and bins visible, not hidden under the hands for the whole step.'),
            h('li', null, '720p at a constant 30 fps is plenty; the model samples 1 to 5 frames a second. Keep the three clips the same length to within a few seconds.'),
            h('li', null, 'Something of known size in view (a bin, a ruler, the part itself) helps the layout scale. A calibration board photographed by all three cameras is the next step for a measured layout.'))),
        h('section', { class: 'card' }, h('h2', null, '4 · Make the session folder'),
          h('p', null, 'A session is a folder with the three clips and a session.json. Re-encode each clip to 720p, constant 30 fps, a keyframe every second, no audio, so the three views scrub together in the browser:'),
          h('pre', { class: 'code' }, 'ffmpeg -i raw.mp4 -vf scale=-2:720 -r 30 -c:v libx264 -crf 20 -g 30 -keyint_min 30 -sc_threshold 0 -pix_fmt yuv420p -an -movflags +faststart cam0.mp4'),
          h('p', null, 'Put the folder under ', h('code', null, 'samples/<id>/'), ' on the server (it ships inside the image), open it on the sessions page and run it live. From an M1 session, a converter reads the sync sidecars and writes this folder with the offsets in one go.'),
          h('p', { class: 'small muted' }, 'Clips over 15 MB in total are sent to the model from the Cloud Storage bucket rather than inline.'))));
  }

  static SESSION_JSON = `{
  "id": "kitting-01",
  "name": "Kitting station, 9 cycles",
  "kind": "sample",
  "recorded": "2026-10-16",
  "synthetic": false,
  "clips": [
    {"view": "cam0", "file": "cam0.mp4", "fps": 30, "width": 1280, "height": 720, "duration_s": 168.0, "offset_s": 0.0},
    {"view": "cam1", "file": "cam1.mp4", "fps": 30, "width": 1280, "height": 720, "duration_s": 168.0, "offset_s": 0.2},
    {"view": "cam2", "file": "cam2.mp4", "fps": 30, "width": 1280, "height": 720, "duration_s": 168.0, "offset_s": 0.45}
  ]
}`;

  /** Top view: the bench, the operator, three cameras on an arc across the bench. */
  static placement() {
    const root = svg('svg', { class: 'guide-svg', viewBox: '0 0 720 420', role: 'img',
      'aria-label': 'Top view: the operator stands in front of the bench; cam0 faces the operator from across the bench, cam1 and cam2 are 45 degrees to each side, all about 2 metres away' });
    // Each camera's cone points at the bench centre (360, 255).
    const cam = (x, y, name, note) => svg('g', { class: 'g-cam', transform: `translate(${x} ${y})` },
      svg('g', { transform: `rotate(${(Math.atan2(255 - y, 360 - x) * 180) / Math.PI})` },
        svg('path', { class: 'cone', d: 'M0,0 L170,-60 L170,60 z' }),
        svg('rect', { x: -14, y: -11, width: 28, height: 22, rx: 4 })),
      svg('text', { x: 0, y: 36, 'text-anchor': 'middle', class: 'lbl' }, name),
      note ? svg('text', { x: 0, y: 52, 'text-anchor': 'middle', class: 'lbl small' }, note) : null);
    root.append(
      svg('rect', { class: 'bench', x: 220, y: 200, width: 280, height: 110, rx: 10 }),
      svg('text', { x: 360, y: 262, 'text-anchor': 'middle', class: 'lbl' }, 'bench 1.2 × 0.6 m'),
      svg('rect', { class: 'binr', x: 236, y: 212, width: 52, height: 40, rx: 6 }), svg('text', { x: 262, y: 237, 'text-anchor': 'middle', class: 'lbl small' }, 'bins'),
      svg('rect', { class: 'binr', x: 432, y: 212, width: 52, height: 40, rx: 6 }), svg('text', { x: 458, y: 237, 'text-anchor': 'middle', class: 'lbl small' }, 'tray'),
      svg('g', { class: 'operator' }, svg('circle', { cx: 360, cy: 360, r: 30 }), svg('text', { x: 360, y: 365, 'text-anchor': 'middle', class: 'lbl' }, 'operator')),
      cam(360, 60, 'cam0', 'master clock · across the bench, high'),
      cam(110, 150, 'cam1', '45° left'),
      cam(610, 150, 'cam2', '45° right'),
      svg('path', { class: 'dim', d: 'M360,95 L360,195' }), svg('text', { x: 372, y: 150, class: 'lbl small' }, '1.5–2 m'),
    );
    return root;
  }

  destroy() { this.alive = false; }
}

addEventListener('hashchange', route);
route();
