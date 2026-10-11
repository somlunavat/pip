// Electron main process: a frameless, transparent, always-on-top overlay.
//
// Hotkeys (globalShortcut fires on press only, so voice is a toggle):
//   Option+Space       show/hide the type-to-prompt overlay
//   Cmd+Shift+Space    start/stop a voice recording
//   Esc                hide (registered only while the overlay is visible)
//
// The overlay never hides by itself (not on focus loss, not on a timer). Besides
// Esc and the hotkey, it closes on the pill's close button or a double-click
// outside it, which bin/clickwatch reports while the overlay is visible.
'use strict';

const { app, BrowserWindow, clipboard, globalShortcut, ipcMain, screen,
  session, shell, systemPreferences } = require('electron');
const { spawn } = require('child_process');
const fs = require('fs');
const net = require('net');
const os = require('os');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const WIDTH = 640;
const PILL_HEIGHT = 120;
const MAX_HEIGHT = 760;

let win = null;
let toastWin = null; // notification popups, top-right; never takes focus
let clickwatch = null;
const TOAST_WIDTH = 420;
const CLICKWATCH = path.join(__dirname, 'bin', 'clickwatch');
const LISTENER_APP = path.join(__dirname, 'bin', 'PipListener.app');
const WAKE_SOCKET = path.join(os.tmpdir(), `pip-wake-${process.getuid()}.sock`);
let wakeServer = null;
let wakeLink = null;

function readToken() {
  if (process.env.PIP_TOKEN) return process.env.PIP_TOKEN.trim();
  try {
    return fs.readFileSync(path.join(ROOT, 'data', 'overlay_token'), 'utf8').trim();
  } catch {
    return '';
  }
}

function createWindow() {
  const { workArea } = screen.getPrimaryDisplay();
  win = new BrowserWindow({
    width: WIDTH,
    height: PILL_HEIGHT,
    x: Math.round(workArea.x + (workArea.width - WIDTH) / 2),
    y: workArea.y + 36,
    frame: false,
    transparent: true,
    resizable: false,
    movable: true,
    alwaysOnTop: true,
    skipTaskbar: true,
    show: false,
    hasShadow: false,
    backgroundColor: '#00000000',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  win.setAlwaysOnTop(true, 'floating');
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  win.loadFile(path.join(__dirname, 'index.html'));

  // Never navigate away or open new windows from the overlay; links go to the browser.
  win.webContents.on('will-navigate', (e) => e.preventDefault());
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (isHttp(url)) shell.openExternal(url);
    return { action: 'deny' };
  });
}

// Popups float top-right like macOS notifications. focusable: false keeps them from
// stealing focus from whatever you're doing; acceptFirstMouse lets one click land.
function createToastWindow() {
  const { workArea } = screen.getPrimaryDisplay();
  toastWin = new BrowserWindow({
    width: TOAST_WIDTH,
    height: 10,
    x: workArea.x + workArea.width - TOAST_WIDTH - 6,
    y: workArea.y + 6,
    frame: false,
    transparent: true,
    resizable: false,
    movable: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    focusable: false,
    acceptFirstMouse: true,
    show: false,
    hasShadow: false,
    backgroundColor: '#00000000',
    webPreferences: {
      preload: path.join(__dirname, 'toast-preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  toastWin.setAlwaysOnTop(true, 'pop-up-menu');
  toastWin.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  toastWin.loadFile(path.join(__dirname, 'toast.html'));
  toastWin.webContents.on('will-navigate', (e) => e.preventDefault());
}

function isHttp(url) {
  try {
    return ['http:', 'https:'].includes(new URL(String(url)).protocol);
  } catch {
    return false;
  }
}

function show(focus = true) {
  if (!win) return;
  win.showInactive();
  if (focus) {
    app.focus({ steal: true });
    win.focus();
  }
  win.webContents.send('overlay:shown');
}

function hide() {
  if (win && win.isVisible()) win.hide();
}

function toggle() {
  if (win.isVisible() && win.isFocused()) hide();
  else show(true);
}

// Watch for double-clicks in other apps only while the overlay is on screen.
function startClickwatch() {
  if (clickwatch || !fs.existsSync(CLICKWATCH)) return;
  clickwatch = spawn(CLICKWATCH, [], { stdio: ['pipe', 'pipe', 'inherit'] });
  let buffer = '';
  clickwatch.stdout.on('data', (chunk) => {
    buffer += chunk;
    const lines = buffer.split('\n');
    buffer = lines.pop();
    for (const line of lines) {
      const [kind, x, y] = line.trim().split(' ');
      if (kind === 'dbl' && !insideWindow(Number(x), Number(y))) hide();
    }
  });
  clickwatch.on('exit', () => { clickwatch = null; });
}

function stopClickwatch() {
  if (clickwatch) clickwatch.kill();
  clickwatch = null;
}

function insideWindow(x, y) {
  const b = win.getBounds();
  return x >= b.x && x <= b.x + b.width && y >= b.y && y <= b.y + b.height;
}

// "Hey Pip": PipListener.app (on-device speech) connects back over a Unix socket.
// It is launched with `open` so it is its own app to macOS and owns its Microphone
// and Speech Recognition permissions.
function startWake() {
  if (wakeServer || !fs.existsSync(LISTENER_APP)) {
    if (!fs.existsSync(LISTENER_APP)) win.webContents.send('overlay:wake', { phase: 'missing' });
    return;
  }
  fs.rmSync(WAKE_SOCKET, { force: true });
  wakeServer = net.createServer((sock) => {
    wakeLink?.destroy();
    wakeLink = sock;
    let buffer = '';
    sock.setEncoding('utf8');
    sock.on('data', (chunk) => {
      buffer += chunk;
      const lines = buffer.split('\n');
      buffer = lines.pop();
      for (const line of lines) onWakeLine(line.trim());
    });
    sock.on('close', () => { if (wakeLink === sock) wakeLink = null; });
  });
  wakeServer.listen(WAKE_SOCKET, () => {
    fs.chmodSync(WAKE_SOCKET, 0o600);
    spawn('/usr/bin/open', ['-g', '-n', '-a', LISTENER_APP, '--args', '--socket', WAKE_SOCKET],
      { stdio: 'ignore', detached: true }).unref();
  });
}

function stopWake() {
  wakeLink?.write('quit\n');
  wakeLink?.destroy();
  wakeLink = null;
  wakeServer?.close();
  wakeServer = null;
  fs.rmSync(WAKE_SOCKET, { force: true });
}

function onWakeLine(line) {
  const [phase, ...rest] = line.split(' ');
  const text = rest.join(' ');
  if (phase === 'wake') show(true);
  if (['ready', 'wake', 'command', 'cancel', 'error'].includes(phase)) {
    win.webContents.send('overlay:wake', { phase, text });
  }
}

function wireClosing() {
  // Esc belongs to other apps except while the overlay is on screen.
  win.on('show', () => {
    if (!globalShortcut.isRegistered('Escape')) globalShortcut.register('Escape', hide);
    startClickwatch();
    toastWin?.webContents.send('toast:clear'); // the overlay now shows what they said
  });
  win.on('hide', () => {
    if (globalShortcut.isRegistered('Escape')) globalShortcut.unregister('Escape');
    stopClickwatch();
    win.webContents.send('overlay:hidden');
  });
}

app.whenReady().then(async () => {
  if (process.platform === 'darwin') {
    app.dock.hide();
    await systemPreferences.askForMediaAccess('microphone').catch(() => false);
  }
  // Only the microphone is ever granted, and only to our own page.
  session.defaultSession.setPermissionRequestHandler((wc, permission, cb) => {
    cb(permission === 'media' && wc === win?.webContents);
  });

  createWindow();
  createToastWindow();
  wireClosing();

  globalShortcut.register('Alt+Space', toggle);
  globalShortcut.register('CommandOrControl+Shift+Space', () => {
    show(false);
    win.webContents.send('overlay:toggle-record');
  });

  ipcMain.handle('overlay:config', () => ({
    token: readToken(),
    port: Number(process.env.PIP_PORT || 8779),
  }));
  ipcMain.on('overlay:resize', (_e, height) => {
    const h = Math.max(PILL_HEIGHT, Math.min(MAX_HEIGHT, Math.round(height)));
    const [w] = win.getSize();
    win.setSize(w, h, true);
  });
  ipcMain.on('overlay:hide', () => hide());
  ipcMain.on('overlay:set-wake', (_e, on) => (on ? startWake() : stopWake()));
  ipcMain.on('overlay:show', () => show(false));
  ipcMain.on('overlay:open-urls', (_e, urls) => {
    for (const url of (Array.isArray(urls) ? urls : []).slice(0, 5)) {
      if (isHttp(url)) shell.openExternal(url);
    }
  });
  ipcMain.on('overlay:copy', (_e, text) => clipboard.writeText(String(text).slice(0, 10000)));
  ipcMain.on('overlay:show-file', (_e, target) => {
    // Only files inside the home folder (worker downloads land in ~/Downloads/Pip).
    const resolved = path.resolve(String(target));
    if (resolved.startsWith(app.getPath('home') + path.sep) && fs.existsSync(resolved)) {
      shell.showItemInFolder(resolved);
    }
  });
  // The overlay decides what deserves a popup (only while it is hidden).
  ipcMain.on('overlay:toast', (_e, toast) => {
    if (win.isVisible()) return;
    toastWin.webContents.send('toast:add', toast);
  });
  ipcMain.on('overlay:toast-remove', (_e, id) => toastWin.webContents.send('toast:remove', id));
  ipcMain.on('toast:resize', (_e, height) => {
    const h = Math.round(Number(height) || 0);
    if (h <= 0) {
      toastWin.hide();
      return;
    }
    toastWin.setSize(TOAST_WIDTH, Math.min(h, 760));
    if (!toastWin.isVisible()) toastWin.showInactive();
  });
  // Clicking a popup brings up the overlay on that task.
  ipcMain.on('toast:open', (_e, toast) => {
    show(true);
    win.webContents.send('overlay:focus-task', toast);
  });
  // Buttons on a popup (Useful / Not useful) answer without opening the overlay.
  ipcMain.on('toast:action', (_e, toast) => win.webContents.send('overlay:toast-action', toast));
});

app.on('will-quit', () => {
  globalShortcut.unregisterAll();
  stopClickwatch();
  stopWake();
});
app.on('window-all-closed', (e) => e.preventDefault());
