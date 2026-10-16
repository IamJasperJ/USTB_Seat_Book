// Admin console. Loaded before app.js and declares only functions: everything
// from app.js ($, api, toast, confirmDialog, taskRecord, ...) is resolved when
// a function runs, and app.js calls initAdmin() once it has finished loading.

const adminState = { users: [], you: null, selectedId: null, historyPage: 1, listSignature: null, detailSignature: null, timer: null };

const adminDailyPhase = {
  disabled: '未开启', scheduled: '已安排', waiting_login: '等待用户登录', blocked: '需要开启预约提交和保活',
  waiting_single_task: '等待单次任务结束', error: '调度异常',
};
const adminNumberFields = ['room_id', 'auto_retry_interval_seconds', 'poll_interval_seconds', 'keepalive_interval_seconds'];
const adminBoolFields = ['keepalive_enabled', 'submit_enabled', 'daily_auto_enabled'];

function adminSelected() {
  return adminState.users.find(u => u.id === adminState.selectedId) || null;
}

function hideAdminView() {
  $('admin-main').hidden = true;
  $('view-toggle').hidden = true;
}

function showView(view) {
  const admin = view === 'admin' && state.isAdmin;
  $('view-toggle').hidden = !state.isAdmin;
  $('view-toggle').textContent = admin ? '返回控制台' : '用户管理';
  $('app-main').hidden = admin;
  $('admin-main').hidden = !admin;
  if (admin) refreshAdmin();
}

async function refreshAdmin(quiet = false) {
  try {
    const result = await api('/api/admin/users');
    adminState.users = result.users;
    adminState.you = result.you;
    if (adminState.selectedId && !adminSelected()) adminState.selectedId = null;
    renderAdminUsers();
    if (adminState.selectedId) await loadAdminDetail(false);
    else renderAdminDetail(null);
  } catch (error) { if (!quiet) toast(error.message, 'error'); }
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function renderAdminUsers() {
  const signature = JSON.stringify([adminState.users, adminState.selectedId]);
  if (signature === adminState.listSignature) return;
  adminState.listSignature = signature;
  const list = $('admin-user-list');
  list.replaceChildren();
  for (const user of adminState.users) {
    const row = el('button', `admin-user-row${user.id === adminState.selectedId ? ' selected' : ''}`);
    row.type = 'button';
    const top = el('div', 'admin-user-top');
    top.append(el('strong', '', user.username));
    if (user.id === adminState.you) top.append(el('span', 'tag current', '你'));
    if (user.is_admin) top.append(el('span', 'tag automation', '管理员'));
    const meta = el('small', 'muted');
    const phase = user.task_running ? (automationPhases[user.task_state] || user.task_state) : '无运行中任务';
    meta.textContent = `${user.logged_in ? '已登录' : '未登录'} · 会话 ${user.sessions} · ${phase}${user.daily_enabled ? ' · 每日模式' : ''}`;
    row.append(top, meta);
    row.addEventListener('click', () => selectAdminUser(user.id));
    list.append(row);
  }
  if (!adminState.users.length) list.append(el('p', 'muted', '暂无用户'));
}

async function selectAdminUser(userId) {
  adminState.selectedId = userId;
  adminState.historyPage = 1;
  adminState.listSignature = null;
  renderAdminUsers();
  await loadAdminDetail(true);
}

async function loadAdminDetail(full) {
  const userId = adminState.selectedId;
  if (!userId) return;
  let detail;
  try { detail = await api(`/api/admin/users/${encodeURIComponent(userId)}`); }
  catch (error) { toast(error.message, 'error'); return; }
  if (adminState.selectedId !== userId) return;
  renderAdminDetail(detail, full);
  if (full) await loadAdminHistory(1);
}

function renderAdminDetail(detail, full = false) {
  $('admin-detail-empty').hidden = Boolean(detail);
  $('admin-detail').hidden = !detail;
  if (!detail) return;
  const { user, settings, automation, daily, sessions } = detail;
  const self = user.id === adminState.you;
  $('admin-detail-name').textContent = user.username;
  $('admin-detail-eyebrow').textContent = `${user.is_admin ? '管理员' : '普通用户'} · 注册于 ${taskTime(user.created_at)}`;
  const role = $('admin-toggle-role');
  role.textContent = user.is_admin ? '取消管理员' : '设为管理员';
  role.disabled = self;
  $('admin-delete-user').disabled = self;
  for (const button of [role, $('admin-delete-user')]) button.title = self ? '不能对自己执行此操作' : '';

  if (full) fillAdminForm(settings);

  $('admin-daily-status').textContent = daily.enabled
    ? `每日模式：${adminDailyPhase[daily.state] || daily.state}；下次触发 ${taskTime(daily.next_trigger_at)}，预约日期 ${daily.target_date || '—'}${daily.last_error ? `；${daily.last_error}` : ''}`
    : '每日模式未开启。';
  const task = $('admin-task');
  task.replaceChildren();
  task.className = automation.running ? 'seat-results' : 'seat-results empty';
  if (automation.running) task.append(taskRecord(automation));
  else task.textContent = '当前没有运行中的任务。';
  $('admin-stop-task').disabled = !automation.running && !daily.enabled;

  // Sessions are re-rendered only when they change: this refreshes on a timer,
  // and replacing a button under the cursor would swallow the click.
  const signature = JSON.stringify(sessions);
  if (signature !== adminState.detailSignature) {
    adminState.detailSignature = signature;
    const box = $('admin-sessions');
    box.replaceChildren();
    if (!sessions.length) box.append(el('p', 'muted', '该用户没有登录会话。'));
    for (const entry of sessions) box.append(adminSessionRow(entry, user));
  }
}

function adminSessionRow(entry, user) {
  const row = el('div', 'session-row');
  const info = el('div', 'session-row-info');
  const title = el('div', 'session-row-title');
  const [label, kind] = authStatusLabels[entry.status] || [entry.status, 'neutral'];
  title.append(el('span', `badge ${kind}`, label),
    el('span', '', entry.user_name ? `${entry.user_name}${entry.acc_no ? `（${entry.acc_no}）` : ''}` : '未完成图书馆登录'));
  if (entry.used_by_automation) title.append(el('span', 'tag automation', '自动预约中'));
  info.append(title, el('small', '', `创建于 ${taskTime(entry.created_at)} · …${entry.session_id.slice(-6)}`));
  const actions = el('div', 'session-row-actions');
  const remove = el('button', 'button danger-ghost small', '关闭');
  remove.type = 'button';
  remove.addEventListener('click', () => adminCloseSession(entry, user, remove));
  actions.append(remove);
  row.append(info, actions);
  return row;
}

function fillAdminForm(settings) {
  const form = $('admin-settings-form');
  for (const [name, value] of Object.entries(settings)) {
    const input = form.elements[name];
    if (!input) continue;
    if (input.type === 'checkbox') input.checked = Boolean(value);
    else input.value = Array.isArray(value) ? value.join(',') : (value ?? '');
  }
  $('admin-settings-message').textContent = '';
}

function adminFormPayload() {
  const form = $('admin-settings-form');
  const payload = {
    login_source: form.elements.login_source.value,
    room_name: form.elements.room_name.value.trim(),
    target_date: form.elements.target_date.value.trim(),
    seat_names: form.elements.seat_names.value.split(',').map(x => x.trim()).filter(Boolean),
    start_time: form.elements.start_time.value,
    end_time: form.elements.end_time.value,
    auto_trigger_time: form.elements.auto_trigger_time.value,
  };
  for (const name of adminNumberFields) payload[name] = Number(form.elements[name].value);
  for (const name of adminBoolFields) payload[name] = form.elements[name].checked;
  return payload;
}

async function saveAdminSettings(event) {
  event.preventDefault();
  const user = adminSelected();
  if (!user) return;
  const button = event.target.querySelector('button[type="submit"]');
  await withBusy(button, async () => {
    try {
      const result = await api(`/api/admin/users/${encodeURIComponent(user.id)}/settings`, { method: 'PUT', body: JSON.stringify(adminFormPayload()) });
      fillAdminForm(result.settings);
      toast(`已保存 ${user.username} 的设置`, 'success');
      adminState.detailSignature = null;
      await refreshAdmin(true);
    } catch (error) { $('admin-settings-message').textContent = error.message; toast(error.message, 'error'); }
  }, '保存中…');
}

async function adminStopTask() {
  const user = adminSelected();
  if (!user) return;
  if (!(await confirmDialog(`停止 ${user.username} 的预约任务？同时会关闭他的每日模式（否则调度器会马上重新安排）。`, { danger: true }))) return;
  try {
    await api(`/api/admin/users/${encodeURIComponent(user.id)}/automation/stop`, { method: 'POST' });
    toast('任务已停止，每日模式已关闭', 'success');
  } catch (error) { toast(error.message, 'error'); }
  await refreshAndRefill();
}

async function adminCloseSession(entry, user, button) {
  const warning = entry.used_by_automation ? '\n\n它正被自动预约任务使用，关闭后该任务会立即停止。' : '';
  if (!(await confirmDialog(`关闭 ${user.username} 的登录会话 …${entry.session_id.slice(-6)}？${warning}`, { danger: true }))) return;
  button.disabled = true;
  try {
    await api(`/api/admin/users/${encodeURIComponent(user.id)}/sessions/${encodeURIComponent(entry.session_id)}`, { method: 'DELETE' });
    toast('会话已关闭', 'success');
  } catch (error) { toast(error.message, 'error'); }
  adminState.detailSignature = null;
  await refreshAndRefill();
}

async function refreshAndRefill() {
  adminState.listSignature = null;
  adminState.detailSignature = null;
  await refreshAdmin(true);
  await loadAdminDetail(true);
}

async function adminToggleRole() {
  const user = adminSelected();
  if (!user) return;
  const makeAdmin = !user.is_admin;
  if (!(await confirmDialog(makeAdmin
    ? `把 ${user.username} 设为管理员？管理员可以查看所有用户、修改他们的设置并查看运行日志。`
    : `取消 ${user.username} 的管理员权限？`, { danger: makeAdmin }))) return;
  try {
    await api(`/api/admin/users/${encodeURIComponent(user.id)}/role`, { method: 'PUT', body: JSON.stringify({ is_admin: makeAdmin }) });
    toast(makeAdmin ? '已设为管理员' : '已取消管理员', 'success');
  } catch (error) { toast(error.message, 'error'); }
  adminState.listSignature = null;
  await refreshAdmin(true);
}

function randomPassword(length = 12) {
  const alphabet = 'abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789';  // no look-alikes
  const bytes = crypto.getRandomValues(new Uint8Array(length));
  return Array.from(bytes, b => alphabet[b % alphabet.length]).join('');
}

function promptDialog(message, { value = '', okLabel = '确认' } = {}) {
  return new Promise((resolve) => {
    const overlay = $('prompt-modal');
    const input = $('prompt-input');
    $('prompt-message').textContent = message;
    $('prompt-ok').textContent = okLabel;
    input.value = value;
    overlay.hidden = false;
    input.focus();
    input.select();
    const done = (result) => {
      overlay.hidden = true;
      $('prompt-form').removeEventListener('submit', onSubmit);
      $('prompt-cancel').removeEventListener('click', onCancel);
      overlay.removeEventListener('click', onOverlay);
      document.removeEventListener('keydown', onKey);
      resolve(result);
    };
    const onSubmit = (event) => { event.preventDefault(); done(input.value); };
    const onCancel = () => done(null);
    const onOverlay = (event) => { if (event.target === overlay) done(null); };
    const onKey = (event) => { if (event.key === 'Escape') done(null); };
    $('prompt-form').addEventListener('submit', onSubmit);
    $('prompt-cancel').addEventListener('click', onCancel);
    overlay.addEventListener('click', onOverlay);
    document.addEventListener('keydown', onKey);
  });
}

async function adminResetPassword() {
  const user = adminSelected();
  if (!user) return;
  const password = await promptDialog(
    `给 ${user.username} 设置新密码（至少 8 位，已预填一个随机密码）。确认后该用户的所有网页登录会立即失效，请把新密码告诉他。`,
    { value: randomPassword(), okLabel: '重置密码' });
  if (password === null) return;
  try {
    const result = await api(`/api/admin/users/${encodeURIComponent(user.id)}/password`, { method: 'POST', body: JSON.stringify({ password }) });
    toast(`密码已重置，已让 ${result.signed_out} 个网页登录失效`, 'success');
  } catch (error) { toast(error.message, 'error'); }
}

async function adminDeleteUser() {
  const user = adminSelected();
  if (!user) return;
  if (!(await confirmDialog(
    `删除用户 ${user.username}？\n\n他的账号、设置、登录会话和正在运行的任务都会被清除，该操作不可撤销。预约历史不会被删除，只是移到 logs/automation-history/deleted/ 保留。`,
    { danger: true }))) return;
  try {
    await api(`/api/admin/users/${encodeURIComponent(user.id)}`, { method: 'DELETE' });
    toast(`已删除用户 ${user.username}`, 'success');
    adminState.selectedId = null;
  } catch (error) { toast(error.message, 'error'); }
  adminState.listSignature = null;
  await refreshAdmin(true);
}

async function loadAdminHistory(page) {
  const userId = adminState.selectedId;
  if (!userId) return;
  let result;
  try { result = await api(`/api/admin/users/${encodeURIComponent(userId)}/history?page=${page}&page_size=10`); }
  catch (error) { $('admin-history').textContent = error.message; return; }
  if (adminState.selectedId !== userId) return;
  adminState.historyPage = result.page;
  const box = $('admin-history');
  box.replaceChildren();
  box.className = result.records.length ? 'seat-results' : 'seat-results empty';
  if (!result.records.length) box.textContent = '暂无历史记录。';
  else for (const record of result.records) box.append(taskRecord(record, true));
  const pages = Math.max(1, Math.ceil(result.total / result.page_size));
  $('admin-history-page').textContent = `第 ${result.page} / ${pages} 页 · 共 ${result.total} 条`;
  $('admin-history-prev').disabled = result.page <= 1;
  $('admin-history-next').disabled = result.page >= pages;
}

function initAdmin() {
  $('view-toggle').addEventListener('click', () => showView($('admin-main').hidden ? 'admin' : 'console'));
  $('admin-refresh').addEventListener('click', () => withBusy($('admin-refresh'), refreshAndRefill, '刷新中…'));
  $('admin-settings-form').addEventListener('submit', saveAdminSettings);
  $('admin-stop-task').addEventListener('click', adminStopTask);
  $('admin-toggle-role').addEventListener('click', adminToggleRole);
  $('admin-reset-password').addEventListener('click', adminResetPassword);
  $('admin-delete-user').addEventListener('click', adminDeleteUser);
  $('admin-history-prev').addEventListener('click', () => loadAdminHistory(adminState.historyPage - 1));
  $('admin-history-next').addEventListener('click', () => loadAdminHistory(adminState.historyPage + 1));
  setInterval(() => { if (!$('admin-main').hidden) refreshAdmin(true); }, 10000);
}
