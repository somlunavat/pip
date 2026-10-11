// Overlay renderer: Pip's states, the task pill, result/plan/alert cards, and voice.
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const pill = $('#pill');
const pipImg = $('#pip');
const input = $('#prompt');
const titleEl = $('#title');
const subtitleEl = $('#subtitle');
const stack = $('#stack');
const micBtn = $('#mic');
const stopBtn = $('#stop');

const PATHS = {
  nimble: ['Nimble', 'nimble'], worker: ['Agent37 worker', 'worker'], mac: ['Mac', 'mac'],
  watcher: ['Watcher', ''], fast: ['Fast reply', ''], chrome: ['Chrome', ''],
};
// Apps a plan's approval-gated steps touch, for "It touches Calendar only."
const APPS = {
  create_reminder: 'Reminders', create_note: 'Notes', create_calendar_event: 'Calendar',
  run_shortcut: 'Shortcuts', type_text: 'the front app', save_text_file: 'Documents/Pip',
  quit_app: 'an open app',
};
const PIP_STATES = ['idle', 'listening', 'thinking', 'working', 'alert', 'done'];

let ws = null;
let reconnectDelay = 500;
let recorder = null;
let pipTimer = null;
const tasks = new Map(); // task_id -> { card, steps, seen:Set, title }
let pendingCard = null;  // card created before the server assigns a task id
let pendingTitle = '';
let currentTaskId = null; // the task the pill is showing; Stop applies to it

// -- Pip -----------------------------------------------------------------------

function setPip(state, holdMs = 0) {
  if (!PIP_STATES.includes(state)) state = 'idle';
  clearTimeout(pipTimer);
  if (pipImg.dataset.state !== state) {
    pipImg.dataset.state = state;
    pipImg.src = `assets/pip/pip-${state}.svg`;
  }
  if (holdMs) pipTimer = setTimeout(() => setPip(busy() ? 'working' : 'idle'), holdMs);
}

function busy() {
  return [...tasks.values()].some((t) => !t.done);
}

// Stop shows while the task in the pill is still running.
function updateStop() {
  const task = currentTaskId && tasks.get(currentTaskId);
  stopBtn.hidden = !(task && !task.done);
}

stopBtn.addEventListener('click', () => {
  if (!currentTaskId) return;
  send({ type: 'stop', task_id: currentTaskId });
  stopBtn.hidden = true;
  setSubtitle('Stopping…');
});

// -- closing ---------------------------------------------------------------------
// The overlay never closes by itself. It closes only on a clear user action:
// Esc, the close button, the hotkey, or a double-click outside it (the main
// process watches other apps; double-clicks on our own empty margin are below).

const pendingPlans = new Set();

function resetForNextRequest() {
  if (busy() || pendingPlans.size) return;
  for (const card of [...stack.children]) {
    if (!card.classList.contains('pending')) card.remove();
  }
  for (const [id, task] of tasks) if (task.done) tasks.delete(id);
  input.value = '';
  pill.dataset.mode = 'input';
  setPip('idle');
  fit();
}

// -- Pill ------------------------------------------------------------------------

function showInput() {
  pill.dataset.mode = 'input';
  input.focus();
  fit();
}

function showTask(title, subtitle) {
  pill.dataset.mode = 'task';
  titleEl.textContent = title;
  subtitleEl.textContent = subtitle || '';
}

function setSubtitle(text) {
  subtitleEl.textContent = text || '';
}

const shell = $('#shell');
// A double-click on the transparent margin around the cards is outside the box.
document.addEventListener('dblclick', (e) => {
  if (e.target === document.body || e.target === shell || e.target === stack) window.pip.hide();
});
$('#close').addEventListener('click', () => window.pip.hide());

pill.addEventListener('mousedown', (e) => {
  if (pill.dataset.mode === 'task' && e.target.closest('.task')) showInput();
});

// -- layout ----------------------------------------------------------------------

function fit() {
  requestAnimationFrame(() => window.pip.resize(document.body.scrollHeight));
}

function fromTemplate(id) {
  return document.getElementById(id).content.firstElementChild.cloneNode(true);
}

function trimStack(max = 3) {
  while (stack.children.length > max) stack.lastElementChild.remove();
}

function hostname(url) {
  try { return new URL(url).hostname.replace(/^www\./, ''); } catch { return url; }
}

// -- connection ----------------------------------------------------------------------

async function connect() {
  const { token, port } = await window.pip.config();
  if (!token) {
    showTask('Pip is offline', 'Start the orchestrator: python -m orchestrator');
    setTimeout(connect, 2000);
    return;
  }
  ws = new WebSocket(`ws://127.0.0.1:${port}/ws?token=${encodeURIComponent(token)}`);
  ws.onopen = () => {
    reconnectDelay = 500;
    if (pill.dataset.mode === 'task' && !busy()) showInput();
    setPip('idle');
  };
  ws.onmessage = (ev) => handle(JSON.parse(ev.data));
  ws.onclose = () => {
    showTask('Reconnecting…', 'Waiting for the orchestrator');
    setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 8000);
  };
}

function send(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(msg));
    return true;
  }
  showTask('Not connected', 'Start the orchestrator, then try again');
  return false;
}

// -- tasks ------------------------------------------------------------------------------

function startRequest(title) {
  const card = fromTemplate('tpl-result');
  card.classList.add('pending');
  stack.prepend(card);
  trimStack();
  pendingCard = card;
  pendingTitle = title;
  showTask(title, 'Thinking…');
  setPip('thinking');
  fit();
}

function taskFor(taskId) {
  if (tasks.has(taskId)) return tasks.get(taskId);
  let card = pendingCard;
  const title = pendingTitle;
  pendingCard = null;
  if (!card) {
    card = fromTemplate('tpl-result');
    card.classList.add('pending');
    stack.prepend(card);
    trimStack();
  }
  const task = { card, steps: 0, seen: new Set(), running: new Map(), finished: 0, title,
    done: false };
  tasks.set(taskId, task);
  currentTaskId = taskId;
  updateStop();
  return task;
}

function chip(text, cls = '') {
  const el = document.createElement('span');
  el.className = `chip ${cls}`.trim();
  el.textContent = text;
  return el;
}

function renderResponse(msg) {
  const task = taskFor(msg.task_id);
  task.done = true;
  updateStop();
  const { card } = task;
  card.classList.remove('pending');

  const answer = msg.answer || { headline: msg.text, summary: '', rows: [] };
  $('.headline', card).textContent = answer.headline;
  $('.summary', card).textContent = answer.summary || '';

  const rows = $('.rows', card);
  rows.replaceChildren();
  for (const row of answer.rows || []) {
    const li = document.createElement('li');
    const label = document.createElement('span');
    label.className = 'label';
    label.textContent = row.label;
    const value = document.createElement('span');
    value.className = 'value';
    value.textContent = row.value || '';
    li.append(label, value);
    if (row.url) {
      li.dataset.url = row.url;
      li.title = row.url;
    }
    rows.appendChild(li);
  }

  const chips = $('.chips', card);
  chips.replaceChildren();
  for (const p of msg.paths || []) {
    const [name, cls] = PATHS[p] || [p, ''];
    chips.appendChild(chip(name, cls));
  }
  if (msg.elapsed_ms != null) chips.appendChild(chip(`${(msg.elapsed_ms / 1000).toFixed(1)} s`));
  const sources = (msg.sources || []).map((s) => s.url);
  if (sources.length) chips.appendChild(chip(`${sources.length} source${sources.length > 1 ? 's' : ''}`));
  const files = msg.files || [];
  if (files.length) chips.appendChild(chip(`${files.length} file${files.length > 1 ? 's' : ''}`, 'worker'));
  $('[data-act="files"]', card).hidden = !files.length;
  card.dataset.folder = files[0] || '';

  const brief = msg.display === 'pill';
  card.classList.toggle('brief', brief);
  $('.foot', card).hidden = brief && !sources.length && !files.length;
  $('[data-act="open"]', card).hidden = !sources.length;
  card.dataset.sources = JSON.stringify(sources);
  card.dataset.copy = [answer.headline, answer.summary,
    ...(answer.rows || []).map((r) => `${r.label}: ${r.value}`)].filter(Boolean).join('\n');

  showTask(task.title || answer.headline,
    msg.opened ? `Done · ${msg.opened.replace(/^opened /, 'opened ')}` : 'Done');
  setPip('done', 2500);
  // Shown as a notification only if the overlay was closed while this ran.
  window.pip.notify('Pip', answer.headline);
}

stack.addEventListener('click', (e) => {
  const row = e.target.closest('.rows li[data-url]');
  if (row) { window.pip.openUrls([row.dataset.url]); return; }
  const act = e.target.closest('.link')?.dataset.act;
  const card = e.target.closest('.card.result');
  if (!act || !card) return;
  if (act === 'copy') {
    window.pip.copy(card.dataset.copy || '');
    e.target.textContent = 'Copied';
    setTimeout(() => { e.target.textContent = 'Copy'; }, 1200);
  } else if (act === 'open') {
    window.pip.openUrls(JSON.parse(card.dataset.sources || '[]'));
  } else if (act === 'files') {
    window.pip.showFile(card.dataset.folder);
  }
});

function renderPlan(msg) {
  const task = taskFor(msg.task_id);
  const card = fromTemplate('tpl-plan');
  $('.headline', card).textContent = msg.summary || 'Proceed with these steps?';
  const apps = [...new Set(msg.milestones.map((m) => APPS[m.tool]).filter(Boolean))];
  $('.touches', card).textContent = apps.length
    ? `Pip wants to do this. It touches ${apps.join(' and ')} only.`
    : 'Pip wants to do this.';
  const steps = $('.steps', card);
  for (const m of msg.milestones) {
    const li = document.createElement('li');
    const goal = document.createElement('span');
    goal.className = 'goal';
    goal.textContent = m.goal;
    const kind = document.createElement('span');
    kind.className = 'kind';
    kind.textContent = m.kind;
    li.append(goal, kind);
    steps.appendChild(li);
  }
  card.addEventListener('click', (e) => {
    const act = e.target.closest('[data-act]')?.dataset.act;
    if (!act) return;
    send({ type: act, task_id: msg.task_id });
    card.remove();
    pendingPlans.delete(msg.task_id);
    setSubtitle(act === 'approve' ? 'Working…' : 'Cancelled');
    setPip(act === 'approve' ? 'working' : 'idle');
    fit();
  });
  task.card.before(card);
  pendingPlans.add(msg.task_id);
  setSubtitle('Waiting for your OK');
  setPip('thinking');
  window.pip.show();
}

// -- permission setup ---------------------------------------------------------------
// Shown on first run while anything is missing, and whenever you type "setup".
// Every grant is a macOS prompt the user answers; Pip can't grant itself anything.

let setupCard = null;
let setupWanted = false;

function firstRun() {
  try { return !localStorage.getItem('pip.setupSeen'); } catch { return false; }
}

function markSetupSeen() {
  try { localStorage.setItem('pip.setupSeen', '1'); } catch { /* private mode */ }
}

function renderPermissions(msg) {
  const missing = msg.items.filter((i) => i.status !== 'granted');
  if (!setupCard && !(setupWanted || (firstRun() && missing.length))) return;
  setupWanted = false;
  if (!setupCard) {
    setupCard = fromTemplate('tpl-setup');
    setupCard.addEventListener('click', onSetupClick);
    stack.prepend(setupCard);
    window.pip.show();
  }
  $('.touches', setupCard).textContent = missing.length
    ? `Pip asks macOS for each one and you approve the prompt. Granted to ${msg.holder}.`
    : `All set. Pip has everything it can use, granted to ${msg.holder}.`;
  const list = $('.perms', setupCard);
  list.replaceChildren();
  for (const item of msg.items) {
    const li = document.createElement('li');
    const what = document.createElement('div');
    what.className = 'what';
    const name = document.createElement('div');
    name.className = 'name';
    name.textContent = item.name;
    const why = document.createElement('div');
    why.className = 'why';
    why.textContent = item.why;
    what.append(name, why);
    li.append(what);
    if (item.status === 'granted') {
      const state = document.createElement('span');
      state.className = 'state granted';
      state.textContent = 'Allowed';
      li.append(state);
    } else {
      const btn = document.createElement('button');
      btn.className = 'grant';
      btn.dataset.id = item.id;
      btn.textContent = item.status === 'denied' ? 'Open Settings' : 'Grant';
      li.append(btn);
    }
    list.appendChild(li);
  }
  const grantAll = $('[data-act="grant-all"]', setupCard);
  grantAll.textContent = missing.length ? 'Grant all' : 'Done';
  grantAll.disabled = false;
  fit();
}

function onSetupClick(e) {
  const one = e.target.closest('.grant');
  const act = e.target.closest('[data-act]')?.dataset.act;
  if (one) {
    one.disabled = true;
    one.textContent = 'Waiting…';
    send({ type: 'grant', ids: [one.dataset.id] });
  } else if (act === 'grant-all' && e.target.textContent !== 'Done') {
    e.target.disabled = true;
    e.target.textContent = 'Answer the prompts…';
    send({ type: 'grant' });
  } else if (act) {
    markSetupSeen();
    setupCard.remove();
    setupCard = null;
    fit();
  }
}

function openSetup() {
  setupWanted = true;
  send({ type: 'permissions' });
}

function renderAlert(msg) {
  const card = fromTemplate('tpl-alert');
  $('.alert-title', card).textContent = `Change on ${hostname(msg.target)}`;
  const score = typeof msg.score === 'number' ? ` Change score ${msg.score.toFixed(2)}.` : '';
  $('.alert-body', card).textContent = `${msg.description}.${score}`.replace('..', '.');
  card.addEventListener('click', (e) => {
    const useful = e.target.closest('[data-useful]')?.dataset.useful;
    if (useful === undefined) return;
    send({ type: 'feedback', change_id: msg.change_id, useful: useful === 'true' });
    const note = document.createElement('div');
    note.className = 'done-note';
    note.textContent = useful === 'true' ? 'Thanks. Pip will show more like this.'
      : 'Got it. Fewer like this.';
    $('.buttons', card).replaceWith(note);
    setTimeout(() => { card.remove(); fit(); }, 1600);
  });
  stack.prepend(card);
  trimStack();
  setPip('alert', 3000);
  window.pip.notify(`Pip: change on ${hostname(msg.target)}`, msg.description);
}

// "Step 2 of 3 · Search kayak.com · Nimble", or for work split across workers,
// "2 of 5 done · 3 running on Agent37".
function progressText(task, msg) {
  const workers = [...task.running.values()].filter((p) => p === 'worker').length;
  if (task.steps > 1 && workers > 1) {
    return `${task.finished} of ${task.steps} done · ${workers} running on Agent37`;
  }
  if (!msg) return `${task.finished} of ${task.steps} done`;
  const [pathName] = PATHS[msg.path] || [msg.path || ''];
  const count = task.steps > 1 ? `Step ${Math.min(task.seen.size, task.steps)} of ${task.steps} · ` : '';
  return `${count}${msg.label}${pathName ? ` · ${pathName}` : ''}`;
}

// -- server events ----------------------------------------------------------------------

function handle(msg) {
  switch (msg.type) {
    case 'hello':
      send({ type: 'permissions' });
      break;
    case 'permissions':
      renderPermissions(msg);
      break;
    case 'transcript':
      pendingTitle = msg.text;
      showTask(msg.text, 'Thinking…');
      break;
    case 'thinking':
      if (msg.task_id) taskFor(msg.task_id);
      setPip('thinking');
      if (msg.label) setSubtitle(msg.label);
      break;
    case 'ack': {
      const task = taskFor(msg.task_id);
      task.steps = msg.steps || 0;
      setSubtitle(msg.text);
      setPip('working');
      break;
    }
    case 'doing': {
      const task = taskFor(msg.task_id);
      if (msg.milestone_id) {
        task.seen.add(msg.milestone_id);
        task.running.set(msg.milestone_id, msg.path);
      }
      setSubtitle(progressText(task, msg));
      setPip('working');
      break;
    }
    case 'milestone_done': {
      const task = taskFor(msg.task_id);
      if (task.running.delete(msg.milestone_id)) task.finished += 1;
      if (task.running.size) setSubtitle(progressText(task));
      break;
    }
    case 'plan':
      renderPlan(msg);
      break;
    case 'response':
      renderResponse(msg);
      break;
    case 'alert':
      renderAlert(msg);
      break;
    case 'error': {
      if (msg.task_id) {
        const task = taskFor(msg.task_id);
        task.done = true;
        updateStop();
        task.card.classList.remove('pending');
        task.card.classList.add('brief');
        $('.headline', task.card).textContent = 'Something went wrong';
        $('.summary', task.card).textContent = msg.message;
        $('.foot', task.card).hidden = true;
      }
      setSubtitle(msg.message);
      setPip('alert', 2500);
      break;
    }
    default:
      break;
  }
  fit();
}

// -- input --------------------------------------------------------------------------------

input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && /^(setup|permissions)$/i.test(input.value.trim())) {
    input.value = '';
    openSetup();
    return;
  }
  if (e.key === 'Enter' && input.value.trim()) {
    const text = input.value.trim();
    if (send({ type: 'request', text })) {
      input.value = '';
      startRequest(text);
    }
  }
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') window.pip.hide();
});

window.pip.onShown(() => {
  if (pill.dataset.mode === 'input' || !busy()) showInput();
  input.select();
  fit();
});

// Like Siri, the next summon starts fresh; running tasks and plans are kept.
window.pip.onHidden(resetForNextRequest);

// -- voice: toggle recording, send audio to Gemini via the orchestrator ------------------------

function pickMime() {
  for (const m of ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4']) {
    if (MediaRecorder.isTypeSupported(m)) return m;
  }
  return '';
}

async function toggleRecord() {
  if (recorder && recorder.state === 'recording') {
    recorder.stop();
    return;
  }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    showTask('Microphone is off', 'Allow it in System Settings > Privacy & Security > Microphone');
    setPip('alert', 2500);
    return;
  }
  const mimeType = pickMime();
  recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
  const chunks = [];
  recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);
  recorder.onstop = async () => {
    stream.getTracks().forEach((t) => t.stop());
    micBtn.classList.remove('on');
    const blob = new Blob(chunks, { type: recorder.mimeType });
    const buf = new Uint8Array(await blob.arrayBuffer());
    let bin = '';
    for (let i = 0; i < buf.length; i += 0x8000) {
      bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
    }
    // Gemini wants the base type without codec parameters.
    const mime = (recorder.mimeType || 'audio/webm').split(';')[0];
    if (send({ type: 'request', audio: btoa(bin), mime })) startRequest('Listening…');
    setSubtitle('Transcribing…');
  };
  recorder.start();
  micBtn.classList.add('on');
  showTask('Listening…', 'Press ⌘⇧Space again when you’re done');
  setPip('listening');
}

micBtn.addEventListener('click', toggleRecord);
window.pip.onToggleRecord(toggleRecord);

connect();
fit();
