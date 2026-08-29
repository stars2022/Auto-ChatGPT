# Auto Codex Companion

一个运行在本机的轻量 Codex/ChatGPT 辅助面板，专门处理两件事：

1. 按时间把消息加入已有 Codex 线程队列（使用 `codex queue`）。
2. 监测线程的 `usage_limited` 状态，或监测额度接口恢复后自动继续。

## 启动

```bash
python3 app.py
```

浏览器会打开 <http://127.0.0.1:8765>。不想自动打开浏览器时：

```bash
AUTOCODEX_OPEN_BROWSER=0 python3 app.py
```

## 桌面版 / 后台运行

桌面版由 Electron 包裹本地服务，窗口关闭后继续驻留托盘。开发环境先安装 Node.js 22+ 与 Python 3.10+，然后：

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

可用环境变量：`CODEX_HOME`、`AUTOCODEX_DATA_DIR`、`AUTOCODEX_PORT`、`AUTOCODEX_POLL_SECONDS`。

## 额度规则

- **官方订阅**：只有 `~/.codex/auth.json` 中出现 `auth_mode: "chatgpt"` 且有 `tokens.access_token` 时才会请求 `https://chatgpt.com/backend-api/wham/usage`。请求会带 `ChatGPT-Account-Id`（如果凭据提供）并按 5 小时/7 天/30 天窗口解析 `rate_limit.primary_window` 与 `secondary_window`。也会尝试 macOS Keychain 的 `Codex Auth` 项。
- **第三方提供商**：不会使用官方订阅端点。若检测到 `config.toml` 的 `[model_providers.custom] base_url` 和 API key 模式的 `auth.json`，面板会自动把它作为第三方探针，默认请求 `${base_url}/v1/usage`，按 `remaining → quota.remaining → balance` 提取余额；也可以在面板里改路径并设置轮询间隔。
- **本地兜底**：没有可用接口时，仍会读取 `goals_1.sqlite` 的目标状态；`usage_limited` 变成其他状态时视为恢复信号。
- **任务预算**：每个计划可设置 token 上限、估算价格上限（USD）和 USD/1K token 单价。达到上限会自动停用计划并在事件流中说明原因。
- **失败恢复**：网络/超时/502/503/504 会按指数退避自动重试；429、quota、usage limit 会挂起计划，等待官方窗口、第三方余额或本地 `usage_limited` 状态恢复后继续。
- 探针错误只记录状态，不会删除上一次成功快照；密钥始终不返回给前端，也不会写入日志。

`/status` 通常是提供商的网页健康页，不是额度接口。当前本机 Pixel API 的 `/status` 返回 HTTP 200 HTML；真正的余额数据在 `/v1/usage`。

## 与 cc-switch 的取舍

参考了 cc-switch 的官方 Codex 查询逻辑（OAuth-only、`wham/usage`、窗口映射、账户 ID header、保守轮询），但没有复制其代码，也没有执行任意 JavaScript extractor。第三方接口使用固定字段提取器，避免把远程脚本当成本地代码执行。

## 安全边界

- `auth.json` 只读取到内存，用于发起本地用户主动启用的请求；接口响应会做字段脱敏。
- 面板 API 只绑定 `127.0.0.1`，计划数据放在 `~/.autocodex/state.json`。
- 面板不会直接修改 Codex 的 SQLite 数据库；继续操作仅调用公开 CLI 子命令 `codex queue`。
