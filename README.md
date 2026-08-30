# Auto Codex Companion

项目地址：[github.com/stars2022/Auto-ChatGPT](https://github.com/stars2022/Auto-ChatGPT)

一个运行在本机的轻量 Codex/ChatGPT 辅助面板，专门处理两件事：

1. 按时间把消息加入已有 Codex 线程队列（使用 `codex queue`）。
2. 监测线程的 `usage_limited` 状态，或监测额度接口恢复后自动继续。

控制面板采用原生桌面软件风格：会话按工作目录归入项目，通过“项目 → 会话”主从视图进行查找；继续会话、创建自动任务和更多操作使用应用内对话框与菜单。用量页优先通过 Codex CLI 的 app-server 读取 `/status` 对应的机器可读额度，失败后再回退到官方 OAuth。

## 启动

```bash
python3 app.py
```

浏览器会打开 <http://127.0.0.1:8765>。不想自动打开浏览器时：

```bash
AUTOCODEX_OPEN_BROWSER=0 python3 app.py
```

## 桌面版 / 后台运行

桌面版由 Electron 包裹本地服务，窗口关闭后默认继续驻留系统托盘：macOS 显示在菜单栏右侧，Windows 显示在通知区域，Linux 使用桌面环境提供的托盘区域；托盘菜单会实时显示启用、等待额度和等待重试的任务数量，并可直接打开自动任务页。设置页的“关闭窗口时”可以改为“退出应用并停止后台任务”。开发环境先安装 Node.js 22+ 与 Python 3.10+，然后：

```bash
npm install
npm start
```

也可以不打开窗口，直接运行跨平台后台启动器：

```bash
python3 launcher.py start   # Windows: py -3 launcher.py start
python3 launcher.py status
python3 launcher.py stop
```

构建安装包：

```bash
npm run dist:mac     # .dmg + .zip（当前机器架构）
npm run dist:win     # Windows x64 NSIS 安装程序
npm run dist:linux   # x64 AppImage + .deb
```

产物会出现在 `dist/`。`.github/workflows/build.yml` 提供 macOS、Windows、Ubuntu 三平台构建；推送 `v*` 标签后会自动创建 GitHub Release 并上传安装包。

GitHub Actions 会在目标系统用 PyInstaller 生成原生后台二进制并嵌入安装包，因此正式发布的安装包不要求用户另装 Python。直接在另一平台交叉构建时若没有对应平台二进制，Electron 会回退到系统的 `python3`（Windows 为 `py -3`）。

可用环境变量：`CODEX_HOME`、`AUTOCODEX_DATA_DIR`、`AUTOCODEX_PORT`、`AUTOCODEX_POLL_SECONDS`。

## 额度规则

- **Codex CLI status**：优先启动当前 Codex CLI 的本地 app-server，并调用 `account/rateLimits/read`。这是交互式 `/status` 所用的机器可读状态，不需要抓取终端 ANSI 文本。
- **官方订阅回退**：CLI 状态不可用时，只有 `~/.codex/auth.json` 中出现 `auth_mode: "chatgpt"` 且有 `tokens.access_token` 才会请求 `https://chatgpt.com/backend-api/wham/usage`。请求会带 `ChatGPT-Account-Id`（如果凭据提供）并解析额度窗口。也会尝试 macOS Keychain 的 `Codex Auth` 项。
- **第三方提供商**：不会使用官方订阅端点。若检测到 `config.toml` 的 `[model_providers.custom] base_url` 与 bearer token（或 API key 模式的 `auth.json`），面板会自动把它作为第三方探针，默认请求 `${base_url}/v1/usage`，按 `remaining → quota.remaining → balance` 提取余额；也可以在面板里改路径并设置轮询间隔。
- **本地兜底**：没有可用接口时，仍会读取 `goals_1.sqlite` 的目标状态；`usage_limited` 变成其他状态时视为恢复信号。
- **任务预算**：每个计划可设置 token 上限、估算价格上限（USD）和 USD/1K token 单价。达到上限会自动停用计划并在事件流中说明原因。
- **失败恢复**：网络/超时/502/503/504 默认按指数退避持续重试（最大退避 6 小时；将最大重试次数设为大于 0 可限制次数）；429、quota、usage limit 会挂起计划，等待官方窗口、第三方余额或本地 `usage_limited` 状态恢复后继续。
- 探针错误只记录状态，不会删除上一次成功快照；密钥始终不返回给前端，也不会写入日志。

## Codex CLI 定位

应用先使用 `CODEX_CLI_PATH` 或设置页保存的位置，然后按当前平台查找：

- macOS：PATH、ChatGPT.app 内置 CLI、Homebrew 常见目录。
- Windows：PATH、npm 全局目录、ChatGPT 常见安装目录。
- Linux：PATH、`~/.local/bin`、`/usr/local/bin` 和 `/usr/bin`。

设置页允许选择 CLI 所在目录，也允许直接粘贴可执行文件完整路径；保存前会执行 `codex --version` 验证。

`/status` 通常是提供商的网页健康页，不是额度接口。当前本机 Pixel API 的 `/status` 返回 HTTP 200 HTML；真正的余额数据在 `/v1/usage`。

## 额度接口安全边界

官方订阅探针只接受 OAuth 凭据，使用固定的 `wham/usage` 端点、账户 ID 请求头和保守轮询，并将响应投影为脱敏的窗口摘要。第三方接口使用固定字段提取器，不执行任意 JavaScript，避免把远程脚本当成本地代码执行。

## 安全边界

- `auth.json` 只读取到内存，用于发起本地用户主动启用的请求；接口响应会做字段脱敏。
- 面板 API 只绑定 `127.0.0.1`，计划数据放在 `~/.autocodex/state.json`。
- 面板不会直接修改 Codex 的 SQLite 数据库；继续操作仅调用公开 CLI 子命令 `codex unarchive`（需要时）和 `codex queue`。
