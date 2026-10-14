// CellPilot page. Plain ES module: no framework, no build step.
// Talks only to the API in docs/contract.md; all arithmetic lives in logic.js.
import {
  STAGES as STAGE_NAMES, VERDICT_LABEL, computePayback, automatedShare, followerTarget, driftAction, nextSeekLead,
  layoutTimeline, activeSegmentIndex, cycleAt, totalCycles, adjacentStart,
  bannerPieces, engineerLine, stageStates, formatTime, formatClock, formatMoney,
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

const backLink = () => h('a', { class: 'back', href: '#/' }, '← Sessions');
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

function fitLine(have, need, unit) {
  if (!Number.isFinite(have)) return '–';
  const text = `${fmtN(have)} ${unit}`;
  if (!Number.isFinite(need)) return text;
  const ok = have >= need;
  return [text, ` · needs ${fmtN(need)} ${unit} `,
    h('span', { class: ok ? 'good' : 'bad', role: 'img', 'aria-label': ok ? 'enough' : 'not enough' }, ok ? '✓' : '✗')];
}

/* ---------------------------------------------------------------- router */

let current = null;

function route() {
  const hash = location.hash || '#/';
  if (!hash.startsWith('#/')) return;
  current?.destroy();
  // #/s/<sid>, or #/session/<sid> so a screenshot tool can open an analysis directly.
  const m = /^#\/(?:s|session)\/([^/?#]+)/.exec(hash);
  let sid = null;
  if (m) { try { sid = decodeURIComponent(m[1]); } catch { sid = m[1]; } }
  current = sid != null ? new AnalysisView(sid) : new SessionsView();
  window.scrollTo(0, 0);
}

/* ---------------------------------------------------------------- S1 sessions */

class SessionsView {
  constructor() {
    this.alive = true;
    document.title = 'CellPilot · Sessions';
    this.body = h('div', null, h('p', { class: 'muted' }, 'Loading sessions…'));
    main.replaceChildren(
      h('h1', { class: 'page-title' }, 'Sessions'),
      h('p', { class: 'lead muted' },
        'Three synchronised cameras film a manual job. Gemini splits it into steps and says which ones a robot can take over; an engineer signs each step off.'),
      this.body,
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
              [`${s.clips ?? '?'} clips`, formatClock(num(s.duration_s)), s.id].join(' · ')))),
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
    this.exportLink = h('a', { class: 'btn', download: '', hidden: true }, 'Export result and sign-offs');
    const banner = h('section', { class: 'banner card', 'aria-label': 'Result' },
      h('div', { class: 'banner-text' }, this.bannerMain, this.bannerSub, this.summary),
      h('div', { class: 'actions' }, this.runBtn, this.exportLink));

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
      const offset = num(c.offset_s) || 0;
      this.figures[c.view] = h('figure', { class: 'vid' }, v,
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
    const transport = h('div', { class: 'transport' },
      this.playBtn, this.timeOut, h('span', { class: 'nav' }, prev, next), speed, this.cycleOut, debug);

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
    const stepsPanel = h('section', { class: 'card steps-panel', 'aria-labelledby': 'steps-h' },
      h('h2', { id: 'steps-h' }, 'Steps'),
      h('div', { class: 'engineer-field' }, h('label', { for: 'engineer' }, 'Engineer signing off'), this.engineer),
      this.stepList);
    this.robotCard = h('section', { class: 'card robot', 'aria-labelledby': 'robot-h' });
    this.payCard = h('section', { class: 'card payback-card', 'aria-labelledby': 'pay-h' });
    this.foot = h('p', { class: 'foot' });

    main.replaceChildren(head, banner, this.vids, transport, this.driftOut, timeline, this.runStatus, this.runAnnounce,
      h('div', { class: 'cols' }, stepsPanel, h('div', { class: 'side' }, this.robotCard, this.payCard)),
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

  seekAndShow(t, view) {
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
    this.robotCard.replaceChildren(h('h2', { id: 'robot-h' }, 'Robot'), h('p', { class: 'muted' }, 'Chosen by the analysis.'));
    this.payCard.replaceChildren(h('h2', { id: 'pay-h' }, 'Payback'), h('p', { class: 'muted' }, 'Calculated from the analysis.'));
    this.foot.textContent = '';
    this.exportLink.hidden = true;
  }

  renderResult(animate) {
    this.clearAnimations();
    const a = this.analysis;
    const r = a.result;
    this.summary.textContent = r.summary || '';
    this.renderSteps();
    this.renderRobot();
    this.renderPayback();
    this.renderFoot();
    this.exportLink.href = `${this.base()}/analyses/${encodeURIComponent(a.id)}/export`;
    this.exportLink.hidden = false;
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
        return h('li', null, rs.text || '', Number.isFinite(t) ? [' ', h('button', {
          type: 'button', class: 'cite', 'aria-label': `Show ${view} at ${formatTime(t)}`,
          onclick: () => this.seekAndShow(t, view),
        }, `${view} at ${formatTime(t)}`)] : null);
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

  /* ----- S4 robot */

  renderRobot() {
    const rb = this.analysis.result.robot;
    const body = [];
    if (!rb) {
      body.push(h('p', { class: 'muted' }, 'No robot was chosen for this result.'));
    } else {
      body.push(h('p', { class: 'robot-model' }, h('b', null, rb.model || 'Unnamed robot'), ' ',
        rb.fits === true ? h('span', { class: 'badge ok' }, 'fits the job') : null,
        rb.fits === false ? h('span', { class: 'badge bad' }, 'does not fit') : null));
      body.push(h('dl', { class: 'kv' },
        h('dt', null, 'Payload'), h('dd', null, fitLine(num(rb.payload_kg), num(rb.needed_payload_kg), 'kg')),
        h('dt', null, 'Reach'), h('dd', null, fitLine(num(rb.reach_mm), num(rb.needed_reach_mm), 'mm'))));
      if (rb.why) body.push(h('p', { class: 'why' }, rb.why));
    }
    this.robotCard.replaceChildren(h('h2', { id: 'robot-h' }, 'Robot'), ...body);
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
    clearInterval(this.elapsedTimer);
    this.player?.destroy();
  }
}

addEventListener('hashchange', route);
route();
