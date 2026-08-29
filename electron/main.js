const { app, BrowserWindow, Menu, Tray, nativeImage, dialog } = require('electron');
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
  });
  backend.stdout.on('data', (data) => console.log(`[backend] ${data}`));
  backend.stderr.on('data', (data) => console.error(`[backend] ${data}`));
  backend.on('error', (error) => {
    const hint = nativeBackend ? '请重新安装应用或查看日志。' : '请安装 Python 3.10+ 后重试。';
    dialog.showErrorBox('Auto Codex Companion', `无法启动后台服务：${error.message}\n${hint}`);
  });
}

function waitForBackend(attempts = 80) {
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
    backgroundColor: '#0b0e13',
    webPreferences: { contextIsolation: true, nodeIntegration: false },
  };
  // Blend the app surface into the macOS title bar.  The web UI owns the
  // navigation/menu chrome while the native traffic lights remain available.
  if (process.platform === 'darwin') {
    windowOptions.titleBarStyle = 'hiddenInset';
    windowOptions.trafficLightPosition = { x: 18, y: 18 };
  }
  win = new BrowserWindow(windowOptions);
  // The top-level menu is intentionally drawn by the app so it behaves the
  // same way on macOS, Windows and Linux. The tray keeps its own context menu.
  Menu.setApplicationMenu(null);
  win.loadURL(`http://127.0.0.1:${PORT}`);
  win.on('close', (event) => {
    if (!quitting) { event.preventDefault(); win.hide(); }
  });
}

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

app.on('before-quit', () => { quitting = true; if (backend && !backend.killed) backend.kill(); });
app.on('window-all-closed', () => { /* keep the tray/background service alive */ });
