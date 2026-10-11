// Electron main process: a frameless, transparent, always-on-top overlay.
//
// Hotkeys (globalShortcut fires on press only, so voice is a toggle):
//   Option+Space       show/hide the type-to-prompt overlay
//   Cmd+Shift+Space    start/stop a voice recording
'use strict';

const { app, BrowserWindow, globalShortcut, ipcMain, Notification, screen,
  session, systemPreferences } = require('electron');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const WIDTH = 560;
const PILL_HEIGHT = 72;
const MAX_HEIGHT = 640;

let win = null;

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
    y: workArea.y + 48,
    frame: false,
    transparent: true,
    resizable: false,
    movable: true,
    alwaysOnTop: true,
    skipTaskbar: true,
    show: false,
    hasShadow: false,
    vibrancy: 'hud',
    visualEffectState: 'active',
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
    if (/^https?:\/\//.test(url)) require('electron').shell.openExternal(url);
    return { action: 'deny' };
  });
}

function show(focus = true) {
  if (!win) return;
  win.showInactive();
  if (focus) win.focus();
  win.webContents.send('overlay:shown');
}

function toggle() {
  if (win.isVisible() && win.isFocused()) win.hide();
  else show(true);
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
  ipcMain.on('overlay:hide', () => win.hide());
  ipcMain.on('overlay:show', () => show(false));
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
