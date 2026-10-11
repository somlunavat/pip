// Overlay renderer: pill states, streaming cards, plan gate, alerts, and voice.
'use strict';

const $ = (sel) => document.querySelector(sel);
const pill = $('#pill');
const input = $('#prompt');
const statusEl = $('#status');
const stack = $('#stack');
const micBtn = $('#mic');

const STATE_LABELS = { idle: '', listening: 'Listening…', thinking: 'Thinking…', doing: 'Working…' };
const PATH_LABELS = { nimble: 'Nimble', worker: 'Agent37 worker', mac: 'Mac', watcher: 'Watcher',
  fast: 'Fast reply', plan: 'Planner', chrome: 'Chrome' };

let ws = null;
let reconnectDelay = 500;
const cards = new Map(); // task_id -> card element
let pendingTask = null;  // card shown before the server assigns a task id
let recorder = null;

// -- layout ----------------------------------------------------------------

function fit() {
  requestAnimationFrame(() => window.pip.resize(document.body.scrollHeight + 8));
}

function setState(state, label) {
  pill.dataset.state = state;
  statusEl.textContent = label ?? STATE_LABELS[state] ?? '';
}

function el(tplId) {
  return document.getElementById(tplId).content.firstElementChild.cloneNode(true);
}

function trimStack(max = 4) {
  while (stack.children.length > max) stack.lastElementChild.remove();
}

// -- connection --------------------------------------------------------------

async function connect() {
  const { token, port } = await window.pip.config();
  if (!token) {
    setState('idle', 'No token — start the orchestrator first');
    setTimeout(connect, 2000);
    return;
  }
  ws = new WebSocket(`ws://127.0.0.1:${port}/ws?token=${encodeURIComponent(token)}`);
  ws.onopen = () => { reconnectDelay = 500; setState('idle'); };
  ws.onmessage = (ev) => handle(JSON.parse(ev.data));
  ws.onclose = () => {
    setState('idle', 'Reconnecting…');
    setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 8000);
  };
}

function send(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(msg));
    return true;
  }
  setState('idle', 'Not connected');
  return false;
}

// -- cards ---------------------------------------------------------------------

function cardFor(taskId) {
  if (taskId && cards.has(taskId)) return cards.get(taskId);
  let card = pendingTask;
  pendingTask = null;
  if (!card) {
    card = el('tpl-card');
    stack.prepend(card);
    trimStack();
  }
  if (taskId) cards.set(taskId, card);
  return card;
}

function newRequestCard(text) {
  const card = el('tpl-card');
  card.querySelector('.progress').textContent = text;
  card.classList.add('pending');
  stack.prepend(card);
  trimStack();
  pendingTask = card;
  fit();
}

function addProgress(card, label, path) {
  const line = document.createElement('div');
  line.className = 'line';
  line.textContent = `${PATH_LABELS[path] || path || ''} · ${label}`;
  const progress = card.querySelector('.progress');
  progress.appendChild(line);
  while (progress.children.length > 5) progress.firstElementChild.remove();
}

function renderResponse(msg) {
  const card = cardFor(msg.task_id);
  card.classList.remove('pending');
  card.querySelector('.progress').replaceChildren();
  card.querySelector('.body').innerHTML = window.renderMarkdown(msg.text);
  const list = card.querySelector('.sources');
  list.replaceChildren();
  for (const s of msg.sources || []) {
    const li = document.createElement('li');
    const a = document.createElement('a');
    a.href = s.url;
    a.target = '_blank';
    a.rel = 'noreferrer';
    a.textContent = s.title || new URL(s.url).hostname;
    li.appendChild(a);
    list.appendChild(li);
  }
  const meta = [];
  if (msg.elapsed_ms != null) meta.push(`${(msg.elapsed_ms / 1000).toFixed(1)}s`);
  if (msg.paths?.length) meta.push(msg.paths.map((p) => PATH_LABELS[p] || p).join(' + '));
  card.querySelector('.meta').textContent = meta.join(' · ');
  card.classList.toggle('pill-only', msg.display === 'pill');
}

function renderPlan(msg) {
  const card = el('tpl-plan');
  card.querySelector('.summary').textContent = msg.summary || '';
  const steps = card.querySelector('.steps');
  for (const m of msg.milestones) {
    const li = document.createElement('li');
    li.textContent = m.tool ? `${m.goal} (${m.tool})` : m.goal;
    steps.appendChild(li);
  }
  card.addEventListener('click', (e) => {
    const act = e.target.dataset?.act;
    if (!act) return;
    send({ type: act, task_id: msg.task_id });
    card.remove();
    fit();
  });
  stack.prepend(card);
  window.pip.show();
}

function renderAlert(msg) {
  const card = el('tpl-alert');
  try { card.querySelector('.target').textContent = new URL(msg.target).hostname; } catch {
    card.querySelector('.target').textContent = msg.target;
  }
  card.querySelector('.desc').textContent = msg.description;
  card.addEventListener('click', (e) => {
    const useful = e.target.dataset?.useful;
    if (useful === undefined) return;
    send({ type: 'feedback', change_id: msg.change_id, useful: useful === 'true' });
    card.querySelector('.actions').textContent = useful === 'true' ? 'Thanks — noted.' : 'Got it — fewer like this.';
    setTimeout(() => { card.remove(); fit(); }, 1500);
  });
  stack.prepend(card);
  trimStack();
  window.pip.notify('pip: change detected', msg.description);
}

// -- server events ---------------------------------------------------------------

function handle(msg) {
  switch (msg.type) {
    case 'hello':
      break;
    case 'thinking':
      setState('thinking', msg.label);
      if (msg.task_id) cardFor(msg.task_id);
      break;
    case 'transcript':
      if (pendingTask) pendingTask.querySelector('.progress').textContent = `“${msg.text}”`;
      break;
    case 'ack':
      setState('doing', msg.text);
      break;
    case 'doing':
      setState('doing');
      addProgress(cardFor(msg.task_id), msg.label, msg.path);
      break;
    case 'plan':
      setState('idle', 'Waiting for approval');
      renderPlan(msg);
      break;
    case 'response':
      setState('idle');
      renderResponse(msg);
      break;
    case 'alert':
      renderAlert(msg);
      break;
    case 'error':
      setState('idle', 'Something went wrong');
      if (msg.task_id || pendingTask) {
        const card = cardFor(msg.task_id);
        card.classList.remove('pending');
        card.querySelector('.body').textContent = msg.message;
      }
      break;
    default:
      break;
  }
  fit();
}

// -- input -------------------------------------------------------------------------

input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && input.value.trim()) {
    const text = input.value.trim();
    if (send({ type: 'request', text })) {
      newRequestCard(text);
      input.value = '';
      setState('thinking');
    }
  } else if (e.key === 'Escape') {
    window.pip.hide();
  }
});

window.pip.onShown(() => { input.focus(); input.select(); fit(); });

// -- voice: toggle recording, send audio to Gemini via the orchestrator -------------

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
    setState('idle', 'Microphone permission denied');
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
    if (send({ type: 'request', audio: btoa(bin), mime })) {
      newRequestCard('🎙 Transcribing…');
      setState('thinking', 'Transcribing…');
    }
  };
  recorder.start();
  micBtn.classList.add('on');
  setState('listening');
}

micBtn.addEventListener('click', toggleRecord);
window.pip.onToggleRecord(toggleRecord);

connect();
fit();
