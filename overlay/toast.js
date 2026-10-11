// Notification popups. Each one: { id, state, title, body, actions:[{label, value}],
// sticky, taskId, changeId, kind }. Clicking a popup opens the overlay on its task;
// action buttons answer without opening it. Non-sticky popups leave after a while
// (paused while hovered); sticky ones (plans, watcher alerts) stay until handled.
'use strict';

const list = document.getElementById('toasts');
const tpl = document.getElementById('tpl-toast');
const MAX = 3;
const LIFETIME_MS = 9000;
const items = new Map(); // id -> { el, toast, timer, born }

function fit() {
  requestAnimationFrame(() => window.toasts.resize(items.size ? document.body.scrollHeight : 0));
}

function ago(born) {
  const s = Math.round((Date.now() - born) / 1000);
  if (s < 45) return 'now';
  const m = Math.round(s / 60);
  return m < 60 ? `${m}m ago` : `${Math.round(m / 60)}h ago`;
}

function remove(id) {
  const item = items.get(id);
  if (!item) return;
  clearTimeout(item.timer);
  items.delete(id);
  item.el.classList.add('leaving');
  setTimeout(() => { item.el.remove(); fit(); }, 180);
}

function arm(id) {
  const item = items.get(id);
  if (!item || item.toast.sticky) return;
  clearTimeout(item.timer);
  item.timer = setTimeout(() => remove(id), LIFETIME_MS);
}

function add(toast) {
  if (items.has(toast.id)) remove(toast.id); // replace, e.g. plan -> finished
  const el = tpl.content.firstElementChild.cloneNode(true);
  el.querySelector('.app-icon img').src = `assets/pip/pip-${toast.state || 'idle'}.svg`;
  el.querySelector('.title').textContent = toast.title;
  el.querySelector('.body').textContent = toast.body || '';
  const actions = el.querySelector('.actions');
  for (const a of toast.actions || []) {
    const b = document.createElement('button');
    b.textContent = a.label;
    b.dataset.value = a.value;
    actions.appendChild(b);
  }
  el.addEventListener('click', (e) => {
    if (e.target.closest('.dismiss')) {
      remove(toast.id);
    } else if (e.target.closest('.actions button')) {
      window.toasts.action(toast, e.target.closest('button').dataset.value);
      remove(toast.id);
    } else {
      window.toasts.open(toast);
      remove(toast.id);
    }
  });
  el.addEventListener('mouseenter', () => clearTimeout(items.get(toast.id)?.timer));
  el.addEventListener('mouseleave', () => arm(toast.id));

  const item = { el, toast, timer: null, born: Date.now() };
  items.set(toast.id, item);
  list.prepend(el);
  arm(toast.id);
  // Oldest non-sticky popups give way first.
  while (items.size > MAX) {
    const victim = [...items.values()].reverse().find((i) => !i.toast.sticky)
      || [...items.values()].pop();
    remove(victim.toast.id);
  }
  fit();
}

setInterval(() => {
  for (const item of items.values()) item.el.querySelector('.when').textContent = ago(item.born);
}, 15000);

window.toasts.onAdd(add);
window.toasts.onRemove(remove);
window.toasts.onClear(() => {
  for (const [id, item] of items) if (!item.toast.sticky) remove(id);
});
