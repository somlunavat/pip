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

const { app, BrowserWindow, clipboard, globalShortcut, ipcMain, Notification, screen,
  session, shell, systemPreferences } = require('electron');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const WIDTH = 640;
const PILL_HEIGHT = 120;
const MAX_HEIGHT = 760;

let win = null;
let clickwatch = null;
const CLICKWATCH = path.join(__dirname, 'bin', 'clickwatch');

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

function wireClosing() {
  // Esc belongs to other apps except while the overlay is on screen.
  win.on('show', () => {
    if (!globalShortcut.isRegistered('Escape')) globalShortcut.register('Escape', hide);
    startClickwatch();
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
  ipcMain.on('overlay:notify', (_e, { title, body }) => {
    // Alerts surface as a macOS notification when the overlay is hidden.
    if (!win.isVisible() && Notification.isSupported()) {
      const n = new Notification({ title: String(title), body: String(body).slice(0, 240) });
      n.on('click', () => show(true));
      n.show();
    }
  });
});

app.on('will-quit', () => {
  globalShortcut.unregisterAll();
  stopClickwatch();
});
app.on('window-all-closed', (e) => e.preventDefault());
