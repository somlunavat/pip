// Narrow bridge between the sandboxed renderer and the main process.
'use strict';

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('pip', {
  config: () => ipcRenderer.invoke('overlay:config'),
  resize: (height) => ipcRenderer.send('overlay:resize', height),
  hide: () => ipcRenderer.send('overlay:hide'),
  pin: (value) => ipcRenderer.send('overlay:pin', value),
  linger: (ms) => ipcRenderer.send('overlay:linger', ms),
  show: () => ipcRenderer.send('overlay:show'),
  notify: (title, body) => ipcRenderer.send('overlay:notify', { title, body }),
  openUrls: (urls) => ipcRenderer.send('overlay:open-urls', urls),
  copy: (text) => ipcRenderer.send('overlay:copy', text),
  onShown: (fn) => ipcRenderer.on('overlay:shown', () => fn()),
  onHidden: (fn) => ipcRenderer.on('overlay:hidden', () => fn()),
  onToggleRecord: (fn) => ipcRenderer.on('overlay:toggle-record', () => fn()),
});
