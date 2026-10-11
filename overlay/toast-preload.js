// Bridge for the notification popup window.
'use strict';

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('toasts', {
  onAdd: (fn) => ipcRenderer.on('toast:add', (_e, toast) => fn(toast)),
  onRemove: (fn) => ipcRenderer.on('toast:remove', (_e, id) => fn(id)),
  onClear: (fn) => ipcRenderer.on('toast:clear', () => fn()),
  open: (toast) => ipcRenderer.send('toast:open', toast),
  action: (toast, value) => ipcRenderer.send('toast:action', { ...toast, value }),
  resize: (height) => ipcRenderer.send('toast:resize', height),
});
