const { app, BrowserWindow, Menu, Tray, nativeImage, dialog, ipcMain } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const http = require('http');

const PORT = Number(process.env.AUTOCODEX_PORT || 8765);
let backend = null;
let win = null;
let tray = null;
let quitting = false;

function backendRoot() {
  return app.isPackaged ? path.join(process.resourcesPath, 'backend') : path.join(__dirname, '..');
}

function pythonCommand() {
  if (process.platform === 'win32') return process.env.AUTOCODEX_PYTHON || 'py';
  return process.env.AUTOCODEX_PYTHON || 'python3';
}

function bundledBackend() {
  if (!app.isPackaged) return null;
  const name = process.platform === 'win32' ? 'autocodex-backend.exe' : 'autocodex-backend';
  const candidate = path.join(process.resourcesPath, 'backend', name);
  return require('fs').existsSync(candidate) ? candidate : null;
}

function startBackend() {
  const root = backendRoot();
  const nativeBackend = bundledBackend();
  const command = nativeBackend || pythonCommand();
  const args = nativeBackend ? [] : [path.join(root, 'app.py')];
  backend = spawn(command, args, {
    cwd: root,
    env: { ...process.env, AUTOCODEX_OPEN_BROWSER: '0', AUTOCODEX_HOST: '127.0.0.1', AUTOCODEX_PORT: String(PORT) },
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
    detached: process.platform !== 'win32',
  });
  backend.stdout.on('data', (data) => console.log(`[backend] ${data}`));
  backend.stderr.on('data', (data) => console.error(`[backend] ${data}`));
  backend.on('error', (error) => {
    const hint = nativeBackend ? '请重新安装应用或查看日志。' : '请安装 Python 3.10+ 后重试。';
    dialog.showErrorBox('Auto Codex Companion', `无法启动后台服务：${error.message}\n${hint}`);
  });
}

function waitForBackend(attempts = 200) {
  return new Promise((resolve, reject) => {
    const check = () => {
      const req = http.get(`http://127.0.0.1:${PORT}/api/overview`, (res) => {
        res.resume();
        if (res.statusCode === 200) return resolve();
        retry();
      });
      req.on('error', retry);
      req.setTimeout(800, () => { req.destroy(); retry(); });
    };
    const retry = () => attempts-- > 0 ? setTimeout(check, 150) : reject(new Error('后台服务启动超时'));
    check();
  });
}

function createWindow() {
  const windowOptions = {
    width: 1320,
    height: 920,
    minWidth: 920,
    minHeight: 680,
    title: 'Auto Codex Companion',
    backgroundColor: '#17181b',
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, 'preload.js'),
    },
  };
  // Keep the native window controls, while letting the web UI draw the
  // title-bar content underneath them.
  if (process.platform === 'darwin') {
    windowOptions.titleBarStyle = 'hiddenInset';
    windowOptions.trafficLightPosition = { x: 18, y: 18 };
    windowOptions.vibrancy = 'under-window';
    windowOptions.visualEffectState = 'active';
  } else if (process.platform === 'win32') {
    windowOptions.titleBarStyle = 'hidden';
    windowOptions.titleBarOverlay = { color: '#00000000', symbolColor: '#68717d', height: 42 };
    windowOptions.backgroundMaterial = 'mica';
  }
  win = new BrowserWindow(windowOptions);
  installApplicationMenu();
  win.loadURL(`http://127.0.0.1:${PORT}`);
  win.on('close', (event) => {
    if (!quitting) { event.preventDefault(); win.hide(); }
  });
}

function sendMenuAction(action) {
  if (win && !win.isDestroyed()) win.webContents.send('app-menu-action', action);
}

function installApplicationMenu() {
  const navigation = [
    { label: '概览', accelerator: 'CmdOrCtrl+1', click: () => sendMenuAction('overview') },
    { label: '项目与会话', accelerator: 'CmdOrCtrl+2', click: () => sendMenuAction('projects') },
    { label: '自动任务', accelerator: 'CmdOrCtrl+3', click: () => sendMenuAction('tasks') },
    { label: '用量', accelerator: 'CmdOrCtrl+4', click: () => sendMenuAction('quota') },
    { label: '活动', accelerator: 'CmdOrCtrl+5', click: () => sendMenuAction('events') },
    { label: '设置', accelerator: 'CmdOrCtrl+,', click: () => sendMenuAction('settings') },
  ];
  const template = [
    ...(process.platform === 'darwin' ? [{
      label: app.name,
      submenu: [
        { role: 'about', label: `关于 ${app.name}` },
        { type: 'separator' },
        { role: 'hide', label: '隐藏' },
        { role: 'hideOthers', label: '隐藏其他' },
        { role: 'unhide', label: '显示全部' },
        { type: 'separator' },
        { role: 'quit', label: '退出' },
      ],
    }] : []),
    {
      label: '文件',
      submenu: [
        { label: '新建自动任务', accelerator: 'CmdOrCtrl+N', click: () => sendMenuAction('new-task') },
        { type: 'separator' },
        { role: 'close', label: '关闭窗口' },
      ],
    },
    { label: '编辑', submenu: [{ role: 'undo', label: '撤销' }, { role: 'redo', label: '重做' }, { type: 'separator' }, { role: 'cut', label: '剪切' }, { role: 'copy', label: '拷贝' }, { role: 'paste', label: '粘贴' }, { role: 'selectAll', label: '全选' }] },
    { label: '查看', submenu: [...navigation, { type: 'separator' }, { role: 'reload', label: '重新加载' }] },
    { label: '窗口', role: 'windowMenu' },
    { label: '帮助', submenu: [{ label: '打开设置与关于', click: () => sendMenuAction('settings') }] },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

ipcMain.handle('pick-codex-directory', async () => {
  const result = await dialog.showOpenDialog(win, {
    title: '选择 Codex CLI 所在目录',
    properties: ['openDirectory', 'createDirectory'],
    buttonLabel: '使用此目录',
  });
  return result.canceled ? null : result.filePaths[0];
});

function createTray() {
  tray = new Tray(nativeImage.createEmpty());
  tray.setToolTip('Auto Codex Companion');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: '打开控制面板', click: () => { win.show(); win.focus(); } },
    { label: '退出', click: () => { quitting = true; app.quit(); } },
  ]));
  tray.on('click', () => win.isVisible() ? win.hide() : win.show());
}

app.whenReady().then(async () => {
  startBackend();
  try { await waitForBackend(); createWindow(); createTray(); } catch (error) { dialog.showErrorBox('Auto Codex Companion', error.message); app.quit(); }
  app.on('activate', () => win?.show());
});

app.on('before-quit', () => {
  quitting = true;
  if (!backend || backend.killed) return;
  if (process.platform !== 'win32') {
    try { process.kill(-backend.pid, 'SIGTERM'); } catch { backend.kill(); }
  } else {
    backend.kill();
  }
});
app.on('window-all-closed', () => { /* keep the tray/background service alive */ });
