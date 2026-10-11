// Electron main process: a frameless, transparent, always-on-top overlay.
//
// Hotkeys (globalShortcut fires on press only, so voice is a toggle):
//   Option+Space       show/hide the type-to-prompt overlay
//   Cmd+Shift+Space    start/stop a voice recording
//   Esc                hide (registered only while the overlay is visible)
//
// Like Siri, the overlay also goes away when you click anywhere else, and a few
// seconds after an answer. A pending approval or a recording keeps it open.
'use strict';

const { app, BrowserWindow, clipboard, globalShortcut, ipcMain, Notification, screen,
  session, shell, systemPreferences } = require('electron');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const WIDTH = 640;
const PILL_HEIGHT = 120;
const MAX_HEIGHT = 760;

let win = null;
let pinned = false;    // a plan awaits approval, or a recording is in progress
let lingerUntil = 0;   // ignore blur until then (results just opened in the browser)

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
  if (win && win.isVisible() && !pinned) win.hide();
}

function toggle() {
  if (win.isVisible() && win.isFocused()) hide();
  else show(true);
}

function wireDismissal() {
  // Clicking anywhere outside the overlay takes focus away from it: hide, like Siri.
  win.on('blur', () => {
    if (Date.now() < lingerUntil) return;
    hide();
  });
  // Esc belongs to other apps except while the overlay is on screen.
  win.on('show', () => {
    if (!globalShortcut.isRegistered('Escape')) globalShortcut.register('Escape', hide);
  });
  win.on('hide', () => {
    if (globalShortcut.isRegistered('Escape')) globalShortcut.unregister('Escape');
    lingerUntil = 0;
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
  wireDismissal();

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
  ipcMain.on('overlay:pin', (_e, value) => { pinned = Boolean(value); });
  ipcMain.on('overlay:linger', (_e, ms) => {
    lingerUntil = Date.now() + Math.max(0, Math.min(Number(ms) || 0, 15000));
  });
  ipcMain.on('overlay:show', () => show(false));
  ipcMain.on('overlay:open-urls', (_e, urls) => {
    for (const url of (Array.isArray(urls) ? urls : []).slice(0, 5)) {
      if (isHttp(url)) shell.openExternal(url);
    }
  });
  ipcMain.on('overlay:copy', (_e, text) => clipboard.writeText(String(text).slice(0, 10000)));
  ipcMain.on('overlay:notify', (_e, { title, body }) => {
    // Alerts surface as a macOS notification when the overlay is hidden.
    if (!win.isVisible() && Notification.isSupported()) {
      const n = new Notification({ title: String(title), body: String(body).slice(0, 240) });
      n.on('click', () => show(true));
      n.show();
    }
  });
});

app.on('will-quit', () => globalShortcut.unregisterAll());
app.on('window-all-closed', (e) => e.preventDefault());
