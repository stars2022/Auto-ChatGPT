const $ = (selector) => document.querySelector(selector);
let overview = null;
let activeTab = 'overview';
let projectQuery = '';
let contextThreadId = '';
const expandedProjects = new Set();

const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#039;' }[c]));
const fmtTime = (value) => { if (!value) return '—'; try { return new Intl.DateTimeFormat('zh-CN', { month:'numeric', day:'numeric', hour:'2-digit', minute:'2-digit' }).format(new Date(value)); } catch { return value; } };
const fmtTokens = (value) => { const n = Number(value || 0); return n >= 1000000 ? `${(n / 1000000).toFixed(1)}M` : n >= 1000 ? `${(n / 1000).toFixed(1)}K` : n.toLocaleString(); };
const toast = (message) => { const el = $('#toast'); if (!el) return; el.textContent = message; el.classList.add('show'); setTimeout(() => el.classList.remove('show'), 2600); };
const statusClass = (status) => ['usage_limited','active','blocked','paused','complete'].includes(status) ? status : '';
const statusLabel = (status) => ({ usage_limited:'额度受限', active:'运行中', blocked:'已阻塞', paused:'已暂停', complete:'已完成', budget_limited:'预算受限' }[status] || status || '无目标');
const api = async (path, options = {}) => { const response = await fetch(path, { headers: { 'Content-Type':'application/json' }, ...options }); const data = await response.json(); if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`); return data; };

function switchTab(tab, updateUrl = true) {
  if (!document.querySelector(`[data-panel="${tab}"]`)) tab = 'overview';
  activeTab = tab;
  document.querySelectorAll('.nav-item').forEach((button) => button.classList.toggle('active', button.dataset.tab === tab));
  document.querySelectorAll('.tab-panel').forEach((panel) => panel.classList.toggle('active', panel.dataset.panel === tab));
  const titles = { overview:'运行总览', tasks:'任务与自动继续', quota:'额度与来源', settings:'设置', events:'事件记录' };
  $('#page-title').textContent = titles[tab] || titles.overview;
  if (updateUrl) history.replaceState(null, '', `#${tab}`);
}

function sourceState() {
  const auth = overview?.inventory?.auth || {};
  const official = overview?.official_usage || {};
  const thirdParty = overview?.usage_probe || {};
  if (auth.kind === 'oauth' && official.status === 'ok') return { label:'官方 OAuth', detail:'已连接官方订阅额度窗口', tone:'good', code:'OFFICIAL' };
  if (auth.kind === 'oauth') return { label:'官方 OAuth', detail:official.status === 'http_error' ? `官方接口返回 ${official.http_status}` : '已检测到 OAuth，等待额度检查', tone:'warn', code:'OAUTH' };
  if (thirdParty.status === 'ok') return { label:'第三方 API', detail:`余额 ${thirdParty.remaining ?? '—'} ${thirdParty.unit || ''}`, tone:'good', code:'API' };
  if (auth.kind === 'api_key' || overview?.usage_config?.api_key_configured) return { label:'API key', detail:'使用第三方额度探针或本地状态', tone:'neutral', code:'API KEY' };
  return { label:'本地状态', detail:'未配置可查询的额度接口', tone:'neutral', code:'LOCAL' };
}

function renderMetrics() {
  const threads = overview.threads || [];
  const schedules = overview.schedules || [];
  const tokens = overview.tokens || {};
  const q = overview.quota || {};
  const source = sourceState();
  const metrics = [
    ['THREADS', threads.length, '最近 100 个线程'],
    ['ACTIVE', threads.filter((t) => t.goal_status === 'active').length, '目标正在运行'],
    ['LIMITED', q.usage_limited || 0, q.usage_limited ? '等待额度恢复' : '当前无受限目标'],
    ['TASKS', schedules.filter((s) => s.enabled).length, '已启用自动任务'],
    ['TOKENS', fmtTokens(tokens.total_tokens), '累计线程用量'],
    ['SOURCE', source.label, source.detail],
  ];
  $('#metrics').innerHTML = metrics.map(([label, value, sub]) => `<div class="metric"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div><div class="sub">${esc(sub)}</div></div>`).join('');
  $('#last-sync').textContent = `刚刚同步 · ${fmtTime(new Date())}`;
  $('#sidebar-sync').textContent = `最近同步 ${fmtTime(new Date())}`;
  $('#poll-label').textContent = `${overview.settings?.poll_seconds || 15}s`;
  $('#codex-version').textContent = overview.inventory?.codex_version || 'codex CLI 未找到';
}

function renderHero() {
  const source = sourceState();
  const limited = overview.quota?.usage_limited || 0;
  const active = (overview.threads || []).filter((t) => t.goal_status === 'active').length;
  const tasks = (overview.schedules || []).filter((s) => s.enabled).length;
  $('#hero-title').textContent = limited ? `${limited} 个任务正在等待额度恢复` : active ? `${active} 个线程正在运行` : '后台已就绪，等待你的任务';
  $('#hero-copy').textContent = `${source.label} · ${source.detail}。当前有 ${tasks} 个自动任务，后台会持续监测并在可用时继续。`;
  $('#source-badge').textContent = source.code;
  $('#overview-source').textContent = source.label;
  $('#overview-source-detail').textContent = source.detail;
  const pill = $('#overview-quota-pill'); pill.className = `pill ${source.tone}`; pill.textContent = source.tone === 'good' ? '正常' : source.tone === 'warn' ? '需检查' : '本地监测';
}

function renderActiveTasks() {
  const activeThreads = (overview.threads || []).filter((t) => ['active','usage_limited','blocked'].includes(t.goal_status)).slice(0, 7);
  const schedules = (overview.schedules || []).filter((s) => s.enabled).slice(0, 7);
  const items = activeThreads.length ? activeThreads.map((t) => `<div class="active-task"><span class="task-dot ${t.goal_status === 'usage_limited' ? 'limited' : ''}"></span><div class="active-task-main"><div class="active-task-name" title="${esc(t.title)}">${esc(t.title || '未命名线程')}</div><div class="active-task-meta">${esc(statusLabel(t.goal_status))} · ${fmtTokens(t.tokens_used)} tokens · ${esc(t.model || '默认模型')}</div></div><button class="mini restore-goal" data-id="${esc(t.id)}">恢复目标</button></div>`).join('') : schedules.map((s) => `<div class="active-task"><span class="task-dot ${s.waiting_for_quota ? 'limited' : ''}"></span><div class="active-task-main"><div class="active-task-name">${esc(s.name)}</div><div class="active-task-meta">${esc(s.waiting_for_quota ? '等待额度恢复' : '自动任务已启用')} · ${esc(s.kind === 'interval' ? `每 ${s.interval_minutes} 分钟` : s.kind === 'at_time' ? `指定时间 ${fmtTime(s.run_at)}` : '额度恢复')}</div></div></div>`).join('');
  $('#active-tasks').innerHTML = items || '<div class="empty">暂无运行中的线程。添加一个自动任务开始。</div>';
  document.querySelectorAll('.restore-goal').forEach((button) => { button.onclick = () => resumeGoal(button.dataset.id); });
}

function projectMatches(project) {
  if (!projectQuery) return true;
  const needle = projectQuery.toLowerCase();
  return [project.name, project.cwd, ...(project.threads || []).flatMap((thread) => [thread.title, thread.id, thread.objective])]
    .some((value) => String(value || '').toLowerCase().includes(needle));
}

function projectThreadMarkup(thread) {
  const title = thread.title || '未命名线程';
  const status = statusLabel(thread.goal_status);
  const archived = Boolean(thread.archived);
  return `<div class="project-thread ${archived ? 'archived' : ''}">
    <span class="task-dot ${thread.goal_status === 'usage_limited' ? 'limited' : ''}"></span>
    <div class="project-thread-main"><div class="thread-title" title="${esc(title)}">${esc(title)}</div><div class="project-thread-meta"><span class="status ${statusClass(thread.goal_status)}">${esc(status)}</span><span>${fmtTokens(thread.tokens_used)} tokens</span><span>${esc(thread.model || '默认模型')}</span><span>${esc(fmtTime(thread.updated_at))}</span>${archived ? '<span class="archived-label">已归档</span>' : ''}</div></div>
    <div class="project-thread-actions"><button class="mini restore-goal" data-id="${esc(thread.id)}">恢复目标</button><button class="mini thread-more" data-id="${esc(thread.id)}" aria-label="更多操作">⋯</button></div>
  </div>`;
}

function renderProjects() {
  const projects = (overview?.projects || []).filter(projectMatches);
  const list = $('#project-list');
  if (!list) return;
  if (!projects.length) { list.innerHTML = '<div class="empty">没有匹配的项目或线程。</div>'; return; }
  const activeProject = projects.find((project) => (project.threads || []).some((thread) => ['active', 'usage_limited'].includes(thread.goal_status)));
  if (!expandedProjects.size && activeProject) expandedProjects.add(activeProject.id);
  list.innerHTML = projects.map((project) => {
    const isOpen = expandedProjects.has(project.id) || Boolean(projectQuery);
    const threads = projectQuery ? project.threads.filter((thread) => [thread.title, thread.id, thread.objective, thread.cwd].some((value) => String(value || '').toLowerCase().includes(projectQuery.toLowerCase()))) : project.threads;
    return `<details class="project-group" data-project-id="${esc(project.id)}" ${isOpen ? 'open' : ''}><summary><div class="project-summary"><span class="project-folder">⌂</span><div class="project-summary-main"><strong>${esc(project.name)}</strong><span title="${esc(project.cwd)}">${esc(project.cwd)}</span></div><div class="project-summary-stats"><span>${project.thread_count} 线程</span><span>${fmtTokens(project.tokens_used)} tokens</span>${project.active ? `<b class="project-active">${project.active} 运行中</b>` : ''}${project.limited ? `<b class="project-limited">${project.limited} 受限</b>` : ''}</div></div></summary><div class="project-threads">${threads.length ? threads.map(projectThreadMarkup).join('') : '<div class="empty">项目中没有匹配线程。</div>'}</div></details>`;
  }).join('');
  document.querySelectorAll('.project-group').forEach((group) => {
    group.addEventListener('toggle', () => group.open ? expandedProjects.add(group.dataset.projectId) : expandedProjects.delete(group.dataset.projectId));
  });
  document.querySelectorAll('.restore-goal').forEach((button) => { button.onclick = () => resumeGoal(button.dataset.id); });
  document.querySelectorAll('.thread-more').forEach((button) => { button.onclick = (event) => showContextMenu(event, button.dataset.id); });
}

function renderQuotaBars() {
  const windows = overview.official_usage?.windows || [];
  $('#overview-quota-bars').innerHTML = windows.length ? windows.slice(0, 3).map((w) => { const used = Math.min(100, Math.max(0, Number(w.used_percent || 0))); return `<div class="quota-bar-row"><span>${esc(w.name.replace('_', ' '))}</span><div class="quota-bar"><i style="width:${used}%"></i></div><strong>${used}%</strong></div>`; }).join('') : '<div class="muted small">暂无官方窗口数据</div>';
  $('#quota-windows').innerHTML = windows.length ? windows.map((w) => { const used = Math.min(100, Math.max(0, Number(w.used_percent || 0))); return `<article class="window-card"><header><span>${esc(w.name.replace('_', ' '))}</span><span>${used < 100 ? '可用' : '已用尽'}</span></header><div class="percent">${used}%</div><div class="quota-bar"><i style="width:${used}%"></i></div><div class="reset">${w.reset_at ? `预计重置 ${fmtTime(w.reset_at)}` : '未提供重置时间'}</div></article>`; }).join('') : '<div class="empty">官方 OAuth 检查后显示窗口。</div>';
}

function renderQuota() {
  const official = overview.official_usage || {};
  const auth = overview.inventory?.auth || {};
  const pill = $('#probe-pill'); pill.className = `pill ${official.status === 'ok' ? 'good' : official.status === 'http_error' ? 'warn' : 'neutral'}`; pill.textContent = official.status === 'ok' ? '已连接' : official.status === 'http_error' ? `HTTP ${official.http_status}` : '未检查';
  $('#quota-page-status').className = `pill ${official.status === 'ok' ? 'good' : official.status === 'http_error' ? 'warn' : 'neutral'}`; $('#quota-page-status').textContent = official.status === 'ok' ? '官方数据已更新' : auth.kind === 'api_key' ? 'API key 模式' : '等待检查';
  $('#official-auth-summary').textContent = auth.kind === 'oauth' ? `已检测到官方 OAuth（来源：${auth.source || '本机凭据'}）。账号标识仅用于请求，不会展示。` : auth.kind === 'api_key' ? '当前是 API key 模式，官方订阅端点不会接受此凭据。请使用第三方 API 探针或本地线程状态。' : '未发现官方 OAuth 凭据。登录 Codex 后可在这里检查订阅窗口。';
  const result = $('#official-result');
  if (official.status && official.status !== 'not_checked') { result.classList.remove('hidden'); result.textContent = official.status === 'ok' ? `接口：${official.endpoint}\n认证：${official.auth_kind}\n窗口：${(official.windows || []).map((w) => `${w.name} 已用 ${w.used_percent}%${w.reset_at ? ` · 重置 ${fmtTime(w.reset_at)}` : ''}`).join('；') || '接口成功但未返回窗口'}` : `接口：${official.endpoint || ''}\n状态：${official.status} ${official.http_status || ''}\n${JSON.stringify(official.detail || '').slice(0, 600)}`; }
  renderQuotaBars();
}

function renderThreads() {
  const rows = overview.threads || [];
  $('#threads').innerHTML = rows.length ? rows.slice(0, 60).map((t) => `<tr><td><div class="thread-title" title="${esc(t.title)}">${esc(t.title || '未命名线程')}</div><div class="thread-path" title="${esc(t.cwd)}">${esc(t.cwd || '')}</div></td><td><span class="status ${statusClass(t.goal_status)}">${esc(statusLabel(t.goal_status))}</span></td><td class="muted">${fmtTokens(t.tokens_used)}</td><td class="muted">${esc(t.model || '—')}</td><td class="muted">${esc(fmtTime(t.updated_at))}</td><td><button class="mini restore-goal" data-id="${esc(t.id)}">恢复目标</button></td></tr>`).join('') : '<tr><td colspan="6" class="empty">未找到线程数据库或线程记录。</td></tr>';
  document.querySelectorAll('.restore-goal').forEach((button) => { button.onclick = () => resumeGoal(button.dataset.id); });
}

function renderTasks() {
  const schedules = overview.schedules || [];
  const threads = overview.threads || [];
  const stats = [['启用中', schedules.filter((s) => s.enabled).length], ['等待额度', schedules.filter((s) => s.waiting_for_quota).length], ['无限重试', schedules.filter((s) => !s.max_attempts).length], ['线程 Tokens', fmtTokens((overview.tokens || {}).total_tokens)]];
  $('#task-stats').innerHTML = stats.map(([label, value]) => `<div class="task-stat"><strong>${esc(value)}</strong><span>${esc(label)}</span></div>`).join('');
  $('#schedule-list').innerHTML = schedules.length ? schedules.map((s) => `<div class="schedule-item"><div class="schedule-main"><div class="schedule-name">${esc(s.name)} <span class="pill ${s.enabled ? 'good' : 'neutral'}">${s.enabled ? '启用' : '暂停'}</span> ${s.waiting_for_quota ? '<span class="pill warn">等待额度</span>' : ''}</div><div class="schedule-meta">${esc(s.kind === 'quota_recovered' ? '额度恢复' : s.kind === 'interval' ? `每 ${s.interval_minutes} 分钟` : `指定时间 ${fmtTime(s.run_at)}`)} · ${s.run_count || 0} 次执行${s.token_budget ? ` · token ≤ ${Number(s.token_budget).toLocaleString()}` : ''}${s.price_budget_usd ? ` · 价格 ≤ $${s.price_budget_usd}` : ''}${s.last_result ? ` · ${s.last_result.ok ? '成功' : '失败'}` : ''} · ${s.max_attempts ? `失败最多重试 ${s.max_attempts} 次` : '网络失败无限重试'}</div>${s.blocked_reason ? `<div class="muted small">${esc(s.blocked_reason)}</div>` : ''}${s.next_attempt_at ? `<div class="muted small">下次重试：${esc(fmtTime(new Date(s.next_attempt_at)))}</div>` : ''}</div><div class="schedule-actions"><button class="mini toggle-schedule" data-id="${esc(s.id)}" data-enabled="${!s.enabled}">${s.enabled ? '暂停' : '启用'}</button><button class="mini delete-schedule" data-id="${esc(s.id)}">删除</button></div></div>`).join('') : '<div class="empty">还没有任务。点击“新建任务”开始。</div>';
  document.querySelectorAll('.toggle-schedule').forEach((button) => { button.onclick = async () => { try { await api('/api/schedules/toggle', { method:'POST', body:JSON.stringify({ id:button.dataset.id, enabled:button.dataset.enabled === 'true' }) }); toast('任务状态已更新'); load(); } catch (error) { toast(error.message); } }; });
  document.querySelectorAll('.delete-schedule').forEach((button) => { button.onclick = async () => { if (!confirm('删除这个任务？')) return; try { await api('/api/schedules/delete', { method:'POST', body:JSON.stringify({ id:button.dataset.id }) }); toast('任务已删除'); load(); } catch (error) { toast(error.message); } }; });
}

function renderEvents() { const rows = overview.events || []; $('#events').innerHTML = rows.length ? rows.map((e) => `<div class="event"><div class="event-time">${esc(fmtTime(e.at))}</div><div><span class="event-kind">${esc(e.kind || 'event')}</span>${esc(e.message || '')}</div>${e.detail ? `<div class="muted small">${esc(e.detail)}</div>` : ''}</div>`).join('') : '<div class="empty">暂无事件</div>'; }
function renderInventory() { const rows = overview.inventory?.files || []; $('#inventory-list').innerHTML = rows.map((f) => `<div class="inventory-row ${f.exists ? '' : 'missing'}"><div><div>${esc(f.label)} ${f.sensitive ? '· <span class="muted">敏感</span>' : ''}</div><div class="path" title="${esc(f.path)}">${esc(f.path)}</div></div><div class="size">${f.exists ? `${(f.size / 1024).toFixed(1)} KB` : '缺失'}</div></div>`).join(''); const cfg = overview.inventory?.config || []; $('#config-keys').innerHTML = cfg.map((k) => k.key ? `<span class="key ${k.sensitive ? 'sensitive' : ''}">${esc(k.section === '(root)' ? k.key : `${k.section}.${k.key}`)}${k.sensitive ? ' · 脱敏' : ''}</span>` : '').join(''); }
function renderUsageConfig() { const config = overview.usage_config || {}; const probe = overview.usage_probe || {}; const form = $('#usage-form'); form.base_url.value = config.base_url || ''; form.path.value = config.path || '/v1/usage'; form.unit.value = config.unit || 'USD'; form.poll_minutes.value = config.poll_minutes || 5; const pill = $('#usage-pill'); pill.className = `pill ${config.api_key_configured ? 'good' : 'neutral'}`; pill.textContent = config.auto_from_codex ? '从 Codex 配置' : config.api_key_configured ? '已配置' : '未配置'; const result = $('#usage-result'); if (probe.status && probe.status !== 'not_configured' && probe.status !== 'not_checked') { result.classList.remove('hidden'); const stale = probe.status === 'ok' ? '' : probe.last_good ? `\n上次成功余额：${probe.last_good.remaining ?? '—'} ${probe.last_good.unit || config.unit || ''}` : ''; result.textContent = probe.status === 'ok' ? `接口：${config.base_url}${config.path}\n余额：${probe.remaining ?? '—'} ${probe.unit || config.unit || ''}\n检查时间：${probe.checked_at}` : `接口状态：${probe.status}\n${probe.detail || ''}${stale}`; } }
function renderSettings() { const settings = overview.settings || {}; const form = $('#settings-form'); form.poll_seconds.value = settings.poll_seconds || 15; form.official_poll_minutes.value = settings.official_poll_minutes || 5; form.default_network_retries.value = settings.default_network_retries ?? 0; form.default_backoff_seconds.value = settings.default_backoff_seconds || 30; form.notifications.checked = settings.notifications !== false; }
function renderAll() { renderMetrics(); renderHero(); renderActiveTasks(); renderQuota(); renderProjects(); renderThreads(); renderTasks(); renderEvents(); renderInventory(); renderUsageConfig(); renderSettings(); }

async function load() { try { overview = await api('/api/overview'); renderAll(); } catch (error) { $('#last-sync').textContent = '连接失败'; toast(error.message); } }
async function resumeGoal(threadId) {
  closeFloatingMenus();
  const message = prompt('恢复目标时发送给线程的消息：', '继续之前的任务；先检查当前状态，再从上次停下的位置继续。');
  if (!message) return;
  try {
    const result = await api('/api/goals/resume', { method:'POST', body:JSON.stringify({ thread_id:threadId, message }) });
    toast(result.unarchived ? '线程已恢复并加入队列' : '目标已恢复并加入队列');
    load();
  } catch (error) { toast(`恢复失败：${error.message}`); }
}

function showContextMenu(event, threadId) {
  event.stopPropagation();
  closeFloatingMenus();
  contextThreadId = threadId;
  const menu = $('#task-context-menu');
  const rect = event.currentTarget.getBoundingClientRect();
  menu.style.left = `${Math.min(window.innerWidth - 190, Math.max(12, rect.right - 178))}px`;
  menu.style.top = `${Math.min(window.innerHeight - 150, rect.bottom + 6)}px`;
  menu.classList.remove('hidden');
}

function closeFloatingMenus() {
  document.querySelectorAll('.app-menu-popover, .floating-menu').forEach((menu) => menu.classList.add('hidden'));
}

function openSchedule(threadId = '') {
  closeFloatingMenus();
  const select = $('#thread-select');
  select.innerHTML = (overview.threads || []).filter((thread) => thread.id).map((thread) => `<option value="${esc(thread.id)}">${esc(thread.title || thread.id.slice(0, 12))} · ${esc(statusLabel(thread.goal_status))}</option>`).join('');
  if (!select.options.length) { toast('没有可用线程'); switchTab('tasks'); return; }
  if (threadId && [...select.options].some((option) => option.value === threadId)) select.value = threadId;
  $('#schedule-dialog').showModal();
}

function runMenuAction(action) {
  closeFloatingMenus();
  if (action === 'resume' && contextThreadId) return resumeGoal(contextThreadId);
  if (action === 'schedule') return openSchedule(contextThreadId);
  if (action === 'copy-id' && contextThreadId) {
    if (navigator.clipboard && navigator.clipboard.writeText) return navigator.clipboard.writeText(contextThreadId).then(() => toast('线程 ID 已复制')).catch(() => toast(contextThreadId));
    return toast(contextThreadId);
  }
  if (action === 'new-task') return openSchedule();
  if (action === 'check-official') { switchTab('quota'); return $('#official-check').click(); }
  if (action === 'scan-config') { switchTab('settings'); const detail = document.querySelector('.about-panel details'); if (detail) detail.open = true; return toast('配置清单已展开'); }
  if (action === 'refresh') return load();
  if (action === 'toggle-sidebar') { document.body.classList.toggle('sidebar-collapsed'); return; }
  if (action === 'about') { switchTab('settings'); return; }
  if (action === 'events') { switchTab('events'); return; }
  if (action === 'go-overview') return switchTab('overview');
  if (action === 'go-tasks') return switchTab('tasks');
  if (action === 'go-quota') return switchTab('quota');
}

document.querySelectorAll('[data-tab]').forEach((button) => { button.onclick = () => switchTab(button.dataset.tab); });
document.querySelectorAll('[data-go-tab]').forEach((button) => { button.onclick = () => switchTab(button.dataset.goTab); });
document.querySelectorAll('.app-menu-trigger').forEach((button) => {
  button.onclick = (event) => {
    event.stopPropagation();
    const target = document.getElementById(button.dataset.menu);
    const wasHidden = target.classList.contains('hidden');
    closeFloatingMenus();
    if (wasHidden) {
      const rect = button.getBoundingClientRect();
      target.style.left = `${Math.max(10, rect.left)}px`;
      target.style.top = `${rect.bottom + 6}px`;
      target.classList.remove('hidden');
    }
  };
});
document.querySelectorAll('.app-menu-popover button, .floating-menu button').forEach((button) => { button.onclick = () => runMenuAction(button.dataset.action); });
document.addEventListener('click', (event) => { if (!event.target.closest('.app-menu-popover, .floating-menu, .app-menu-trigger, .thread-more')) closeFloatingMenus(); });
document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeFloatingMenus(); });
$('#project-search').addEventListener('input', (event) => { projectQuery = event.target.value.trim(); renderProjects(); });
$('#refresh').onclick = load; $('#top-new-task').onclick = openSchedule; $('#hero-add-task').onclick = openSchedule; $('#quick-new-task').onclick = openSchedule; $('#new-schedule').onclick = openSchedule;
$('#hero-check-quota').onclick = () => { switchTab('quota'); $('#official-check').click(); }; $('#quick-check-official').onclick = () => { switchTab('quota'); $('#official-check').click(); }; $('#quick-scan').onclick = () => { switchTab('settings'); toast('配置清单已移至设置 → 关于与诊断'); document.querySelector('.about-panel details').open = true; };
$('#close-dialog').onclick = () => $('#schedule-dialog').close(); $('#cancel-dialog').onclick = () => $('#schedule-dialog').close(); $('#schedule-kind').onchange = (event) => { $('#interval-field').classList.toggle('hidden', event.target.value !== 'interval'); $('#time-field').classList.toggle('hidden', event.target.value !== 'at_time'); };
$('#schedule-form').onsubmit = async (event) => { event.preventDefault(); const form = new FormData(event.target); const data = Object.fromEntries(form.entries()); data.interval_minutes = Number(data.interval_minutes || 60); data.token_budget = data.token_budget || null; data.price_budget_usd = data.price_budget_usd || null; data.price_per_1k_tokens = data.price_per_1k_tokens || null; data.max_attempts = Number(data.max_attempts || 0); data.retry_on_network = form.has('retry_on_network'); data.retry_on_quota = form.has('retry_on_quota'); try { await api('/api/schedules', { method:'POST', body:JSON.stringify(data) }); $('#schedule-dialog').close(); toast('任务已创建'); event.target.reset(); load(); } catch (error) { toast(error.message); } };
$('#settings-form').onsubmit = async (event) => { event.preventDefault(); const form = new FormData(event.target); const data = Object.fromEntries(form.entries()); data.poll_seconds = Number(data.poll_seconds || 15); data.official_poll_minutes = Number(data.official_poll_minutes || 5); data.default_network_retries = Number(data.default_network_retries || 0); data.default_backoff_seconds = Number(data.default_backoff_seconds || 30); data.notifications = form.has('notifications'); try { await api('/api/settings', { method:'POST', body:JSON.stringify(data) }); toast('后台设置已保存'); load(); } catch (error) { toast(error.message); } };
$('#usage-form').onsubmit = async (event) => { event.preventDefault(); const form = new FormData(event.target); const data = Object.fromEntries(form.entries()); data.enabled = true; data.poll_minutes = Number(data.poll_minutes || 5); try { await api('/api/usage-config', { method:'POST', body:JSON.stringify(data) }); toast('探针已保存（密钥只存本机）'); load(); } catch (error) { toast(error.message); } };
$('#usage-check').onclick = async () => { try { const result = await api('/api/usage-check', { method:'POST', body:'{}' }); overview.usage_probe = result.probe; renderUsageConfig(); toast('第三方接口检查完成'); } catch (error) { toast(error.message); } };
$('#official-check').onclick = async () => { try { const result = await api('/api/official-usage-check', { method:'POST', body:'{}' }); overview.official_usage = result.probe; renderHero(); renderQuota(); toast('官方接口检查完成'); } catch (error) { toast(error.message); } };

const initialTab = location.hash.replace('#', ''); switchTab(initialTab || 'overview', false); load(); setInterval(load, 15000);
