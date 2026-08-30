const $ = (selector) => document.querySelector(selector);
let overview = null;
let activeTab = 'overview';
let selectedProjectId = '';
let projectQuery = '';
let contextThreadId = '';

const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#039;' }[char]));
const fmtTime = (value) => { if (!value) return '—'; try { return new Intl.DateTimeFormat('zh-CN', { month:'numeric', day:'numeric', hour:'2-digit', minute:'2-digit' }).format(new Date(value)); } catch { return String(value); } };
const fmtTokens = (value) => { const number = Number(value || 0); return number >= 1e6 ? `${(number / 1e6).toFixed(1)}M` : number >= 1e3 ? `${(number / 1e3).toFixed(1)}K` : number.toLocaleString(); };
const statusLabel = (status) => ({ usage_limited:'额度受限', active:'运行中', blocked:'已阻塞', paused:'已暂停', complete:'已完成', budget_limited:'预算受限' }[status] || '普通会话');
const api = async (path, options = {}) => { const response = await fetch(path, { headers:{ 'Content-Type':'application/json' }, ...options }); const data = await response.json(); if (!response.ok) throw new Error(data.error || data.detail || `HTTP ${response.status}`); return data; };
const toast = (message) => { const element = $('#toast'); element.textContent = message; element.classList.add('show'); clearTimeout(element._timer); element._timer = setTimeout(() => element.classList.remove('show'), 2800); };

function switchTab(tab, updateUrl = true) {
  if (!document.querySelector(`[data-panel="${tab}"]`)) tab = 'overview';
  activeTab = tab;
  document.querySelectorAll('.nav-item').forEach((item) => item.classList.toggle('active', item.dataset.tab === tab));
  document.querySelectorAll('.tab-panel').forEach((panel) => panel.classList.toggle('active', panel.dataset.panel === tab));
  const copy = {
    overview:['概览','查看任务与用量状态'], projects:['项目与会话','按工作目录查找并继续 Codex 会话'],
    tasks:['自动任务','安排定时或额度恢复后的继续操作'], quota:['用量','查看 Codex CLI 状态与额度窗口'],
    events:['活动','后台操作和检测记录'], settings:['设置','配置 CLI、扫描和通知'],
  };
  $('#page-title').textContent = copy[tab][0];
  $('#page-subtitle').textContent = copy[tab][1];
  if (updateUrl) history.replaceState(null, '', `#${tab}`);
}

function sourceState() {
  const usage = overview?.official_usage || {};
  const auth = overview?.inventory?.auth || {};
  if (usage.status === 'ok' && usage.credential_source === 'codex_cli') return { label:'Codex CLI', detail:'通过 CLI app-server 读取 /status 状态', tone:'good' };
  if (usage.status === 'ok') return { label:'OAuth 回退', detail:'CLI 状态不可用，已通过本机 OAuth 读取', tone:'good' };
  if (auth.kind === 'oauth') return { label:'OAuth 已登录', detail:'等待下一次用量检查', tone:'warn' };
  return { label:'本地状态', detail:'未连接可读取用量的来源', tone:'neutral' };
}

function renderMetrics() {
  const projects = overview.projects || [];
  const threads = overview.threads || [];
  const schedules = overview.schedules || [];
  const values = [
    ['项目', projects.length, '工作目录'],
    ['会话', threads.length, '最近记录'],
    ['进行中', threads.filter((item) => item.goal_status === 'active').length, `${overview.quota?.usage_limited || 0} 个额度受限`],
    ['自动任务', schedules.filter((item) => item.enabled).length, `${schedules.length} 个已配置`],
  ];
  $('#metrics').innerHTML = values.map(([label, value, detail]) => `<div class="metric"><span>${esc(label)}</span><strong>${esc(value)}</strong><small>${esc(detail)}</small></div>`).join('');
  $('#project-count').textContent = projects.length;
  $('#project-summary-count').textContent = `${projects.length} 个项目`;
  $('#last-sync').textContent = `更新于 ${fmtTime(new Date())}`;
  $('#sidebar-sync').textContent = `最近同步 ${fmtTime(new Date())}`;
  const platform = window.autoCodex?.platform;
  const trayName = platform === 'darwin' ? '菜单栏' : platform === 'win32' ? '通知区域' : platform === 'linux' ? '系统托盘' : '桌面托盘';
  $('#tray-location').textContent = `${trayName}可打开自动任务`;
}

function renderHero() {
  const threads = overview.threads || [];
  const active = threads.filter((item) => item.goal_status === 'active').length;
  const limited = overview.quota?.usage_limited || 0;
  const source = sourceState();
  $('#hero-title').textContent = limited ? `${limited} 个会话正在等待额度恢复` : active ? `${active} 个会话正在进行` : 'Codex 工作区已准备好';
  $('#hero-copy').textContent = `${source.label} · ${source.detail}。会话已按 ${overview.projects?.length || 0} 个项目归类。`;
  const primary = overview.official_usage?.windows?.[0];
  const used = Math.max(0, Math.min(100, Number(primary?.used_percent || 0)));
  $('#usage-ring').style.setProperty('--usage', used);
  $('#ring-value').textContent = primary ? `${used}%` : '—';
}

function renderOverviewLists() {
  const projects = overview.projects || [];
  $('#recent-projects').innerHTML = projects.length ? projects.slice(0, 5).map((project) => `<button class="compact-row open-project" data-id="${esc(project.id)}"><span class="row-icon">▦</span><span class="row-main"><strong>${esc(project.name)}</strong><span>${esc(project.cwd)}</span></span><span class="row-meta">${project.thread_count} 个会话</span></button>`).join('') : '<div class="empty">没有找到项目</div>';
  document.querySelectorAll('.open-project').forEach((button) => button.onclick = () => selectProject(button.dataset.id, true));
  const active = (overview.threads || []).filter((thread) => ['active','usage_limited'].includes(thread.goal_status));
  $('#active-tasks').innerHTML = active.length ? active.slice(0, 6).map((thread) => `<div class="compact-row"><span class="row-icon">${thread.goal_status === 'usage_limited' ? '!' : '›'}</span><span class="row-main"><strong>${esc(thread.title || '未命名会话')}</strong><span>${esc(thread.objective || thread.cwd || '')}</span></span><span class="badge ${thread.goal_status === 'usage_limited' ? 'warn' : 'good'}">${statusLabel(thread.goal_status)}</span><button class="mini-button continue-thread" data-id="${esc(thread.id)}">继续</button></div>`).join('') : '<div class="empty">目前没有进行中的会话</div>';
  document.querySelectorAll('.continue-thread').forEach((button) => button.onclick = () => openContinue(button.dataset.id));
}

function filteredProjects() {
  const query = projectQuery.toLowerCase();
  if (!query) return overview.projects || [];
  return (overview.projects || []).filter((project) => [project.name, project.cwd, ...(project.threads || []).map((thread) => thread.title)].some((value) => String(value || '').toLowerCase().includes(query)));
}

function selectProject(projectId, openTab = false) {
  const update = () => { selectedProjectId = projectId; if (openTab) switchTab('projects'); renderProjects(); };
  if (document.startViewTransition) document.startViewTransition(update);
  else update();
}

function renderProjects() {
  const projects = filteredProjects();
  if (!projects.some((project) => project.id === selectedProjectId)) selectedProjectId = projects[0]?.id || '';
  $('#project-list').innerHTML = projects.length ? projects.map((project) => `<button class="project-item ${project.id === selectedProjectId ? 'active' : ''}" data-id="${esc(project.id)}"><span class="project-item-icon">▦</span><span class="project-item-main"><strong>${esc(project.name)}</strong><span>${project.active ? `${project.active} 运行中 · ` : ''}${fmtTokens(project.tokens_used)} tokens</span></span><b>${project.thread_count}</b></button>`).join('') : '<div class="empty">没有匹配的项目</div>';
  document.querySelectorAll('.project-item').forEach((button) => button.onclick = () => selectProject(button.dataset.id));
  const project = projects.find((item) => item.id === selectedProjectId);
  if (!project) { $('#project-detail').innerHTML = '<div class="empty-state"><div class="empty-icon">▦</div><h3>没有匹配的项目</h3><p>尝试更改搜索条件。</p></div>'; return; }
  const threads = project.threads || [];
  $('#project-detail').innerHTML = `<header class="project-detail-header"><div><h2>${esc(project.name)}</h2><code title="${esc(project.cwd)}">${esc(project.cwd)}</code></div><div class="project-totals"><span>${project.thread_count} 个会话</span><span>${fmtTokens(project.tokens_used)} tokens</span></div></header><div class="thread-list">${threads.length ? threads.map((thread) => `<article class="thread-row ${thread.archived ? 'archived' : ''}" data-reactive-card data-thread-id="${esc(thread.id)}"><div class="thread-top"><button class="thread-summary-click" type="button" aria-label="打开 ${esc(thread.title || '会话')} 详情"><span class="thread-title" title="${esc(thread.title)}">${esc(thread.title || '未命名会话')}</span><span class="thread-subline"><span class="status-chip ${esc(thread.goal_status || '')}">${esc(statusLabel(thread.goal_status))}</span><span>${esc(thread.model || '默认模型')}</span><span>${fmtTokens(thread.tokens_used)} tokens</span><span>${esc(fmtTime(thread.updated_at))}</span>${thread.archived ? '<span>已归档</span>' : ''}</span><i class="thread-open-hint">查看详情&nbsp; ↗</i></button><div class="thread-actions"><button class="primary-button continue-thread" data-id="${esc(thread.id)}">继续</button><button class="secondary-button schedule-thread" data-id="${esc(thread.id)}">自动继续</button><button class="icon-button thread-more" data-id="${esc(thread.id)}">•••</button></div></div></article>`).join('') : '<div class="empty">项目中没有会话</div>'}</div>`;
  document.querySelectorAll('.thread-summary-click').forEach((button) => button.onclick = () => openThreadDetail(button.closest('.thread-row').dataset.threadId, button.closest('.thread-row'), project));
  document.querySelectorAll('.continue-thread').forEach((button) => button.onclick = () => openContinue(button.dataset.id));
  document.querySelectorAll('.schedule-thread').forEach((button) => button.onclick = () => openSchedule(button.dataset.id, button));
  document.querySelectorAll('.thread-more').forEach((button) => button.onclick = (event) => openThreadMenu(event, button.dataset.id));
}

function renderQuota() {
  const usage = overview.official_usage || {};
  const source = sourceState();
  const windows = usage.windows || [];
  $('#overview-source-detail').textContent = source.detail;
  $('#overview-quota-pill').className = `badge ${source.tone}`;
  $('#overview-quota-pill').textContent = source.label;
  $('#overview-quota-bars').innerHTML = windows.length ? windows.map((window) => { const used = Math.max(0, Math.min(100, Number(window.used_percent || 0))); return `<div class="quota-row"><span>${esc(window.name.replace('_', ' '))}</span><div class="quota-track"><i style="width:${used}%"></i></div><strong>${used}%</strong></div>`; }).join('') : '<div class="empty">暂无额度窗口</div>';
  $('#quota-windows').innerHTML = windows.length ? windows.map((window) => { const used = Math.max(0, Math.min(100, Number(window.used_percent || 0))); return `<article class="window-card"><header><span>${esc(window.name.replace('_', ' '))}</span><span>${used < 100 ? '可用' : '已用尽'}</span></header><strong>${used}% 已用</strong><div class="quota-track"><i style="width:${used}%"></i></div><footer>${window.reset_at ? `重置于 ${fmtTime(window.reset_at)}` : '未提供重置时间'}</footer></article>`; }).join('') : '<div class="empty">通过 CLI 刷新后显示额度窗口</div>';
  const cli = overview.inventory?.codex_cli_info || {};
  $('#cli-pill').className = `badge ${cli.found ? 'good' : 'bad'}`;
  $('#cli-pill').textContent = cli.found ? '已连接' : '未找到';
  $('#cli-summary').textContent = cli.found ? `${cli.version || 'Codex CLI'} · ${cli.source === 'manual' ? '手动路径' : '自动检测'}` : '没有在当前平台的常见位置找到 CLI';
  $('#cli-path').textContent = cli.path || '请在设置中指定目录';
  const auth = overview.inventory?.auth || {};
  $('#official-auth-summary').textContent = auth.kind === 'oauth' ? `已检测到官方 OAuth；当前优先使用 Codex CLI，失败时自动回退。` : '未发现官方 OAuth，CLI 状态仍可能可用。';
  $('#probe-pill').className = `badge ${usage.status === 'ok' ? 'good' : usage.status && usage.status !== 'not_checked' ? 'warn' : 'neutral'}`;
  $('#probe-pill').textContent = usage.status === 'ok' ? source.label : '未检查';
  const result = $('#official-result');
  if (usage.status && usage.status !== 'not_checked') { result.classList.remove('hidden'); result.textContent = usage.status === 'ok' ? `来源：${usage.credential_source || usage.auth_kind}\n套餐：${usage.plan_type || usage.data?.plan_type || '—'}\n检查：${usage.checked_at || '—'}` : `${usage.status}\n${usage.detail || ''}`; } else result.classList.add('hidden');
  renderProviderSummary();
}

function renderProviderSummary() {
  const config = overview.usage_config || {};
  const probe = overview.usage_probe || {};
  const pill = $('#overview-provider-pill');
  const detail = $('#overview-provider-detail');
  if (!pill || !detail) return;
  if (!config.enabled && !config.api_key_configured) {
    pill.className = 'badge neutral';
    pill.textContent = '未配置';
    detail.textContent = '可在用量页配置兼容的余额接口。';
    return;
  }
  if (probe.status === 'ok') {
    pill.className = 'badge good';
    pill.textContent = '已连接';
    detail.textContent = `余额：${probe.remaining ?? '—'} ${probe.unit || config.unit || ''} · 检查于 ${fmtTime(probe.checked_at)}`;
    return;
  }
  if (probe.status && probe.status !== 'not_checked' && probe.status !== 'not_configured') {
    pill.className = 'badge warn';
    pill.textContent = '检查失败';
    detail.textContent = `${probe.status}${probe.detail ? ` · ${probe.detail}` : ''}`;
    return;
  }
  pill.className = 'badge warn';
  pill.textContent = '等待检查';
  detail.textContent = '已配置，后台会按设定间隔刷新余额。';
}

function scheduleTriggerLabel(item) {
  if (item.kind === 'quota_recovered') return '额度恢复后';
  if (item.kind === 'interval') return `每 ${item.interval_minutes} 分钟`;
  return `指定时间 ${fmtTime(item.run_at)}`;
}

function scheduleState(item) {
  if (item.completed_at) return { label:'已完成', tone:'good' };
  if (item.waiting_for_quota) return { label:'等待额度', tone:'warn' };
  if (item.retry_pending) return { label:'等待重试', tone:'warn' };
  if (!item.enabled && item.blocked_reason) return { label:'已停用', tone:'bad' };
  if (!item.enabled) return { label:'已暂停', tone:'neutral' };
  return { label:'启用', tone:'good' };
}

function renderTasks() {
  const schedules = overview.schedules || [];
  const stats = [['启用中', schedules.filter((item) => item.enabled).length], ['等待额度', schedules.filter((item) => item.waiting_for_quota).length], ['等待重试', schedules.filter((item) => item.retry_pending).length], ['成功执行', schedules.reduce((sum, item) => sum + Number(item.run_count || 0), 0)]];
  $('#task-stats').innerHTML = stats.map(([label, value]) => `<div class="task-stat"><strong>${esc(value)}</strong><span>${esc(label)}</span></div>`).join('');
  $('#schedule-list').innerHTML = schedules.length ? schedules.map((item) => { const state = scheduleState(item); const attempts = Number(item.attempt_count ?? item.run_count ?? 0); const stateDetail = item.waiting_for_quota ? '额度恢复后立即重试' : item.retry_pending && item.next_attempt_at ? `下次重试 ${fmtTime(item.next_attempt_at)}` : item.blocked_reason || ''; return `<div class="schedule-item"><div class="schedule-main"><div class="schedule-title">${esc(item.name)}<span class="badge ${state.tone}">${state.label}</span></div><div class="schedule-meta">${esc(scheduleTriggerLabel(item))} · 成功 ${item.run_count || 0} 次 · 尝试 ${attempts} 次${stateDetail ? ` · ${esc(stateDetail)}` : ''}</div></div><div class="schedule-actions"><button class="mini-button toggle-schedule" data-id="${esc(item.id)}" data-enabled="${!item.enabled}">${item.enabled ? '暂停' : item.completed_at ? '重新运行' : '启用'}</button><button class="mini-button delete-schedule" data-id="${esc(item.id)}">删除</button></div></div>`; }).join('') : '<div class="empty">还没有自动任务</div>';
  document.querySelectorAll('.toggle-schedule').forEach((button) => button.onclick = async () => { try { await api('/api/schedules/toggle', { method:'POST', body:JSON.stringify({ id:button.dataset.id, enabled:button.dataset.enabled === 'true' }) }); toast('任务状态已更新'); await load(); } catch (error) { toast(error.message); } });
  document.querySelectorAll('.delete-schedule').forEach((button) => button.onclick = async () => { if (!await confirmDelete()) return; try { await api('/api/schedules/delete', { method:'POST', body:JSON.stringify({ id:button.dataset.id }) }); toast('自动任务已删除'); await load(); } catch (error) { toast(error.message); } });
}

function renderUsageConfig() {
  const config = overview.usage_config || {};
  const probe = overview.usage_probe || {};
  const form = $('#usage-form');
  form.base_url.value = config.base_url || '';
  form.path.value = config.path || '/v1/usage';
  form.unit.value = config.unit || 'USD';
  form.poll_minutes.value = config.poll_minutes || 5;
  $('#usage-pill').className = `badge ${config.api_key_configured ? 'good' : 'neutral'}`;
  $('#usage-pill').textContent = config.auto_from_codex ? '来自 Codex 配置' : config.api_key_configured ? '已配置' : '未配置';
  const result = $('#usage-result');
  if (probe.status && !['not_configured','not_checked'].includes(probe.status)) { result.classList.remove('hidden'); result.textContent = probe.status === 'ok' ? `余额：${probe.remaining ?? '—'} ${probe.unit || ''}\n检查：${probe.checked_at}` : `${probe.status}\n${probe.detail || ''}`; } else result.classList.add('hidden');
}

function renderSettings() {
  const settings = overview.settings || {};
  const form = $('#settings-form');
  form.codex_cli_path.value = settings.codex_cli_path || '';
  form.poll_seconds.value = settings.poll_seconds || 15;
  form.official_poll_minutes.value = settings.official_poll_minutes || 5;
  form.default_network_retries.value = settings.default_network_retries ?? 0;
  form.default_backoff_seconds.value = settings.default_backoff_seconds || 30;
  form.close_behavior.value = settings.close_behavior === 'quit' ? 'quit' : 'tray';
  form.notifications.checked = settings.notifications !== false;
  $('#codex-version').textContent = overview.inventory?.codex_version || 'CLI 未找到';
  const cli = overview.inventory?.codex_cli_info || {};
  $('#cli-detect-result').className = `inline-status ${cli.found ? 'good' : 'bad'}`;
  $('#cli-detect-result').textContent = cli.found ? `当前使用：${cli.path}` : '未自动找到 Codex CLI，请选择安装目录。';
}

function renderEvents() {
  const events = overview.events || [];
  $('#events').innerHTML = events.length ? events.map((event) => `<div class="event"><time>${esc(fmtTime(event.at))}</time><strong>${esc(event.message || event.kind || '活动')}</strong><span>${esc(event.ok === false ? '失败' : event.status || '')}</span></div>`).join('') : '<div class="empty">暂无活动</div>';
}

function renderInventory() {
  const files = overview.inventory?.files || [];
  $('#inventory-list').innerHTML = files.map((file) => `<div class="inventory-row ${file.exists ? '' : 'missing'}"><strong>${esc(file.label)}</strong><code title="${esc(file.path)}">${esc(file.path)}</code></div>`).join('');
  const config = overview.inventory?.config || [];
  $('#config-keys').innerHTML = config.filter((item) => item.key).map((item) => `<span>${esc(item.section === '(root)' ? item.key : `${item.section}.${item.key}`)}${item.sensitive ? ' · 脱敏' : ''}</span>`).join('');
}

function renderAll() { renderMetrics(); renderHero(); renderOverviewLists(); renderProjects(); renderQuota(); renderTasks(); renderUsageConfig(); renderSettings(); renderEvents(); renderInventory(); }
async function load() { try { overview = await api('/api/overview'); renderAll(); } catch (error) { $('#last-sync').textContent = '连接失败'; toast(error.message); } }

function threadById(id) { return (overview?.threads || []).find((thread) => thread.id === id); }
function showDialogFrom(dialog, origin) {
  const sourceRect = origin?.getBoundingClientRect ? origin.getBoundingClientRect() : origin;
  dialog.showModal();
  const targetRect = dialog.getBoundingClientRect();
  if (!sourceRect?.width || matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  const dx = sourceRect.left + sourceRect.width / 2 - (targetRect.left + targetRect.width / 2);
  const dy = sourceRect.top + sourceRect.height / 2 - (targetRect.top + targetRect.height / 2);
  const sx = Math.max(.12, Math.min(1, sourceRect.width / targetRect.width));
  const sy = Math.max(.08, Math.min(1, sourceRect.height / targetRect.height));
  dialog.animate([
    { opacity:0, transform:`translate(${dx}px, ${dy}px) scale(${sx}, ${sy})`, filter:'brightness(1.35)' },
    { opacity:1, transform:'translate(0, 0) scale(1)', filter:'brightness(1)' },
  ], { duration:380, easing:'cubic-bezier(.16,1,.3,1)' });
}
function openThreadDetail(threadId, origin, project) {
  const thread = threadById(threadId);
  if (!thread) return;
  const dialog = $('#thread-detail-dialog');
  dialog.dataset.threadId = threadId;
  $('#thread-detail-project').textContent = project?.name ? `${project.name} · 会话详情` : '会话详情';
  $('#thread-detail-title').textContent = thread.title || '未命名会话';
  $('#thread-detail-meta').innerHTML = `<span class="status-chip ${esc(thread.goal_status || '')}">${esc(statusLabel(thread.goal_status))}</span><span>${esc(thread.model || '默认模型')}</span><span>${fmtTokens(thread.tokens_used)} tokens</span><span>更新于 ${esc(fmtTime(thread.updated_at))}</span>${thread.archived ? '<span>已归档</span>' : ''}`;
  $('#thread-detail-objective').textContent = thread.objective || '这个会话没有设置持续目标。';
  $('#thread-detail-id').textContent = thread.id;
  showDialogFrom(dialog, origin);
}
function openContinue(threadId) { closeMenus(); const thread = threadById(threadId); const dialog = $('#continue-dialog'); dialog.querySelector('[name="thread_id"]').value = threadId; $('#continue-title').textContent = thread?.title || '发送后续消息'; dialog.showModal(); dialog.querySelector('textarea').focus(); }
function syncScheduleForm() {
  const form = $('#schedule-form');
  const kind = form.kind.value;
  $('#interval-field').classList.toggle('hidden', kind !== 'interval');
  $('#time-field').classList.toggle('hidden', kind !== 'at_time');
  $('#quota-trigger-help').classList.toggle('hidden', kind !== 'quota_recovered');
  form.interval_minutes.required = kind === 'interval';
  form.run_at.required = kind === 'at_time';
  $('#network-retry-field').classList.toggle('hidden', !form.retry_on_network.checked);
}

function updateScheduleThreads(preferredThreadId = '') {
  const projectSelect = $('#schedule-project');
  const select = $('#thread-select');
  const projects = overview?.projects || [];
  const project = projects.find((item) => String(item.id) === String(projectSelect.value));
  const threads = project ? (project.threads || []) : (overview?.threads || []);
  select.innerHTML = threads.map((thread) => `<option value="${esc(thread.id)}">${esc(thread.title || thread.id.slice(0, 12))} · ${esc(statusLabel(thread.goal_status))}</option>`).join('');
  select.disabled = !threads.length;
  if (!threads.length) return;
  const preferred = preferredThreadId && threads.some((thread) => thread.id === preferredThreadId);
  select.value = preferred ? preferredThreadId : threads[0].id;
}

function openSchedule(threadId = '', origin = null) {
  closeMenus();
  const form = $('#schedule-form');
  form.reset();
  const projects = overview.projects || [];
  const projectSelect = $('#schedule-project');
  projectSelect.innerHTML = projects.map((project) => `<option value="${esc(project.id)}">${esc(project.name)} · ${project.thread_count || (project.threads || []).length} 个对话</option>`).join('');
  if (!projects.length) {
    projectSelect.innerHTML = '<option value="">未找到分组</option>';
    projectSelect.disabled = true;
  } else {
    projectSelect.disabled = false;
    const projectForThread = threadId && projects.find((project) => (project.threads || []).some((thread) => thread.id === threadId));
    projectSelect.value = projectForThread?.id || projects[0].id;
  }
  updateScheduleThreads(threadId);
  if (!$('#thread-select').options.length) return toast('没有可用会话');
  const local = new Date(Date.now() + 5 * 60_000);
  local.setSeconds(0, 0);
  form.run_at.min = new Date(Date.now() - new Date().getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
  form.run_at.value = new Date(local.getTime() - local.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
  syncScheduleForm();
  showDialogFrom($('#schedule-dialog'), origin);
}

function openThreadMenu(event, threadId) { event.stopPropagation(); closeMenus(); contextThreadId = threadId; const menu = $('#thread-menu'); const rect = event.currentTarget.getBoundingClientRect(); menu.style.left = `${Math.min(innerWidth - 182, Math.max(8, rect.right - 170))}px`; menu.style.top = `${Math.min(innerHeight - 120, rect.bottom + 5)}px`; menu.classList.remove('hidden'); }
function closeMenus() { $('#thread-menu').classList.add('hidden'); }

async function confirmDelete() { const dialog = $('#confirm-dialog'); dialog.showModal(); return new Promise((resolve) => dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), { once:true })); }

async function detectCli() {
  const path = $('#codex-cli-path').value.trim();
  const result = $('#cli-detect-result');
  result.className = 'inline-status'; result.textContent = '正在检测…';
  try { const data = await api('/api/cli-detect', { method:'POST', body:JSON.stringify({ path }) }); result.className = 'inline-status good'; result.textContent = `已找到 ${data.cli.version || 'Codex CLI'}：${data.cli.path}`; toast('Codex CLI 可用'); }
  catch (error) { result.className = 'inline-status bad'; result.textContent = error.message; }
}

document.querySelectorAll('[data-tab]').forEach((button) => button.onclick = () => switchTab(button.dataset.tab));
document.querySelectorAll('[data-go-tab]').forEach((button) => button.onclick = () => switchTab(button.dataset.goTab));
window.autoCodex?.onMenuAction?.((action) => {
  if (['overview', 'projects', 'tasks', 'quota', 'events', 'settings'].includes(action)) return switchTab(action);
  if (action === 'new-task') return overview ? openSchedule('', $('#top-new-task')) : toast('数据仍在加载，请稍后再试');
  if (action === 'refresh') return load();
});
document.querySelectorAll('[data-close-dialog]').forEach((button) => button.onclick = () => document.getElementById(button.dataset.closeDialog).close());
$('#thread-detail-dialog').onclick = (event) => { if (event.target === event.currentTarget) event.currentTarget.close(); };
$('#detail-continue').onclick = () => { const dialog = $('#thread-detail-dialog'); const id = dialog.dataset.threadId; dialog.close(); openContinue(id); };
$('#detail-schedule').onclick = (event) => { const dialog = $('#thread-detail-dialog'); const id = dialog.dataset.threadId; const origin = event.currentTarget.getBoundingClientRect(); dialog.close(); openSchedule(id, origin); };
$('#copy-detail-thread-id').onclick = () => { const id = $('#thread-detail-dialog').dataset.threadId; navigator.clipboard?.writeText(id).then(() => toast('会话 ID 已复制')).catch(() => toast(id)); };
// Hover feedback is global; press feedback is intentionally narrower below.
// All visible surfaces can show the moving edge highlight, while only elements
// that navigate/open content receive the press-depth transform.
const reactiveCardSelector = '.sidebar, .surface-card, .metric, .window-card, .project-item, .thread-row, .compact-row, .schedule-item, .event, .context-menu button, .primary-button, .secondary-button, .danger-button, .icon-button, .mini-button, .nav-item, .brand, .service-state';
const reactiveFieldSelector = 'input:not([type="checkbox"]):not([type="hidden"]), select, textarea';
const tiltSurfaceSelector = '.project-item, .open-project, .thread-row';
const lightOnlySelector = 'button';
const edgeReactiveSelector = `${reactiveCardSelector}, ${reactiveFieldSelector}`;
let pendingPointerFrame = 0;
let activePressedComponent = null;
let activePressedRect = null;
function updatePressedComponent(card, clientX, clientY, stableRect = activePressedRect) {
  if (!card) return;
  const rect = stableRect || card.getBoundingClientRect();
  const localX = clientX - rect.left;
  const localY = clientY - rect.top;
  const nx = Math.max(-1, Math.min(1, (localX / Math.max(1, rect.width) - .5) * 2));
  const ny = Math.max(-1, Math.min(1, (localY / Math.max(1, rect.height) - .5) * 2));
  card.style.setProperty('--press-x', `${localX}px`);
  card.style.setProperty('--press-y', `${localY}px`);
  card.style.setProperty('--press-shift-x', `${(nx * 1.8).toFixed(2)}px`);
  card.style.setProperty('--press-shift-y', `${(ny * 1.1).toFixed(2)}px`);
  card.style.setProperty('--press-rotate-x', `${(-ny * 2.2).toFixed(2)}deg`);
  card.style.setProperty('--press-rotate-y', `${(nx * 2.2).toFixed(2)}deg`);
}
document.addEventListener('pointermove', (event) => {
  const { clientX, clientY } = event;
  updatePressedComponent(activePressedComponent, clientX, clientY);
  cancelAnimationFrame(pendingPointerFrame);
  pendingPointerFrame = requestAnimationFrame(() => {
    // Keep the moving edge highlight on every visible surface. Press depth is
    // handled independently below, so non-clickable cards still glow without
    // moving when the pointer passes over them.
    document.querySelectorAll(edgeReactiveSelector).forEach((card) => {
      const rect = card.getBoundingClientRect();
      const dx = Math.max(rect.left - clientX, 0, clientX - rect.right);
      const dy = Math.max(rect.top - clientY, 0, clientY - rect.bottom);
      const distance = Math.hypot(dx, dy);
      const strength = Math.max(0, Math.min(1, 1 - distance / 42));
      card.style.setProperty('--mouse-x', `${clientX - rect.left}px`);
      card.style.setProperty('--mouse-y', `${clientY - rect.top}px`);
      card.style.setProperty('--light-strength', strength.toFixed(3));
      card.style.setProperty('--edge-highlight', `rgba(166, 218, 255, ${strength.toFixed(3)})`);
    });
  });
});
document.addEventListener('pointerleave', () => document.querySelectorAll(edgeReactiveSelector).forEach((card) => { card.style.setProperty('--light-strength', '0'); card.style.setProperty('--edge-highlight', 'transparent'); }));
document.addEventListener('pointerdown', (event) => {
  const field = event.target.closest(reactiveFieldSelector);
  const button = event.target.closest('button');
  const navigableCard = event.target.closest(tiltSurfaceSelector);
  const cardOwnsClick = Boolean(navigableCard && button?.matches('.project-item, .open-project, .thread-summary-click'));
  const lightOnly = cardOwnsClick ? null : event.target.closest(lightOnlySelector);
  const card = field || lightOnly || (cardOwnsClick ? navigableCard : null);
  if (!card) return;
  activePressedComponent?.classList.remove('press-depth');
  activePressedComponent?.classList.remove('field-pressed');
  activePressedComponent?.classList.remove('press-light-only');
  activePressedRect = card.getBoundingClientRect();
  card.classList.add(field ? 'field-pressed' : lightOnly ? 'press-light-only' : 'press-depth');
  activePressedComponent = card;
  updatePressedComponent(card, event.clientX, event.clientY);
  try { card.setPointerCapture(event.pointerId); } catch {}
});
const releasePressedComponent = () => {
  const card = activePressedComponent;
  activePressedComponent = null;
  activePressedRect = null;
  card?.classList.remove('press-depth');
  card?.classList.remove('field-pressed');
  card?.classList.remove('press-light-only');
};
document.addEventListener('pointerup', releasePressedComponent);
document.addEventListener('pointercancel', releasePressedComponent);
document.addEventListener('click', (event) => {
  if (!event.target.closest('.context-menu, .thread-more')) closeMenus();
});
document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeMenus(); });
$('#thread-menu').querySelectorAll('button').forEach((button) => button.onclick = () => { const id = contextThreadId; closeMenus(); if (button.dataset.action === 'continue') openContinue(id); if (button.dataset.action === 'schedule') openSchedule(id); if (button.dataset.action === 'copy-id') navigator.clipboard?.writeText(id).then(() => toast('会话 ID 已复制')).catch(() => toast(id)); });

$('#project-search').oninput = (event) => { projectQuery = event.target.value.trim(); renderProjects(); };
$('#refresh').onclick = load;
$('#top-new-task').onclick = (event) => openSchedule('', event.currentTarget);
$('#new-schedule').onclick = (event) => openSchedule('', event.currentTarget);
$('#hero-check-quota').onclick = () => { switchTab('quota'); $('#cli-status-check').click(); };

$('#continue-form').onsubmit = async (event) => { event.preventDefault(); const data = Object.fromEntries(new FormData(event.target).entries()); try { const result = await api('/api/goals/resume', { method:'POST', body:JSON.stringify(data) }); $('#continue-dialog').close(); toast(result.unarchived ? '会话已取消归档并继续' : '消息已加入会话队列'); await load(); } catch (error) { toast(`继续失败：${error.message}`); } };

$('#schedule-kind').onchange = syncScheduleForm;
$('#schedule-project').onchange = () => updateScheduleThreads();
$('#retry-on-network').onchange = syncScheduleForm;
$('#schedule-form').onsubmit = async (event) => {
  event.preventDefault();
  const formData = new FormData(event.target);
  const data = Object.fromEntries(formData.entries());
  data.interval_minutes = data.kind === 'interval' ? Number(data.interval_minutes) : null;
  data.run_at = data.kind === 'at_time' ? data.run_at : null;
  data.token_budget = data.token_budget || null;
  data.price_budget_usd = data.price_budget_usd || null;
  data.price_per_1k_tokens = data.price_per_1k_tokens || null;
  data.max_attempts = formData.has('retry_on_network') ? Number(data.max_attempts || 0) : 0;
  data.retry_on_network = formData.has('retry_on_network');
  data.retry_on_quota = formData.has('retry_on_quota');
  try { await api('/api/schedules', { method:'POST', body:JSON.stringify(data) }); $('#schedule-dialog').close(); toast('自动任务已创建'); await load(); }
  catch (error) { toast(error.message); }
};

$('#cli-status-check').onclick = async () => { const button = $('#cli-status-check'); button.disabled = true; button.textContent = '读取中…'; try { const result = await api('/api/cli-status-check', { method:'POST', body:'{}' }); overview.official_usage = result.probe; renderHero(); renderQuota(); toast('CLI 用量状态已更新'); } catch (error) { toast(`CLI 状态失败：${error.message}`); } finally { button.disabled = false; button.textContent = '通过 CLI 刷新'; } };
$('#official-check').onclick = async () => { try { const result = await api('/api/official-usage-check', { method:'POST', body:'{}' }); overview.official_usage = result.probe; renderHero(); renderQuota(); toast(result.probe.credential_source === 'codex_cli' ? '已通过 Codex CLI 更新' : '已通过 OAuth 回退更新'); } catch (error) { toast(error.message); } };

$('#choose-cli-directory').onclick = async () => { if (!window.autoCodex?.pickCodexDirectory) return toast('浏览器模式下请直接粘贴 CLI 路径'); const path = await window.autoCodex.pickCodexDirectory(); if (path) { $('#codex-cli-path').value = path; await detectCli(); } };
$('#detect-cli').onclick = detectCli;
$('#settings-form').onsubmit = async (event) => { event.preventDefault(); const form = new FormData(event.target); const data = Object.fromEntries(form.entries()); data.poll_seconds = Number(data.poll_seconds || 15); data.official_poll_minutes = Number(data.official_poll_minutes || 5); data.default_network_retries = Number(data.default_network_retries || 0); data.default_backoff_seconds = Number(data.default_backoff_seconds || 30); data.notifications = form.has('notifications'); data.close_behavior = data.close_behavior === 'quit' ? 'quit' : 'tray'; try { await api('/api/settings', { method:'POST', body:JSON.stringify(data) }); await window.autoCodex?.setCloseBehavior?.(data.close_behavior); toast(data.close_behavior === 'tray' ? '设置已保存，关闭窗口后任务会继续在后台运行' : '设置已保存，关闭窗口后会退出应用'); await load(); } catch (error) { toast(error.message); } };

$('#usage-form').onsubmit = async (event) => { event.preventDefault(); const form = new FormData(event.target); const data = Object.fromEntries(form.entries()); data.enabled = true; data.poll_minutes = Number(data.poll_minutes || 5); try { await api('/api/usage-config', { method:'POST', body:JSON.stringify(data) }); toast('第三方探针已保存'); await load(); } catch (error) { toast(error.message); } };
$('#usage-check').onclick = async () => { try { const result = await api('/api/usage-check', { method:'POST', body:'{}' }); overview.usage_probe = result.probe; renderUsageConfig(); toast('连接检查完成'); } catch (error) { toast(error.message); } };

const initialTab = location.hash.replace('#', '');
switchTab(initialTab || 'overview', false);
load();
setInterval(load, 15000);
