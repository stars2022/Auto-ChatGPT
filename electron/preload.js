const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('autoCodex', {
  platform: process.platform,
  pickCodexDirectory: () => ipcRenderer.invoke('pick-codex-directory'),
  setCloseBehavior: (behavior) => ipcRenderer.invoke('set-close-behavior', behavior),
  onMenuAction: (callback) => {
    if (typeof callback !== 'function') return () => {};
    const listener = (_event, action) => callback(action);
    ipcRenderer.on('app-menu-action', listener);
    return () => ipcRenderer.removeListener('app-menu-action', listener);
  },
});
