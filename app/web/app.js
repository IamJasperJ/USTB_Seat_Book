const state = { sessionId: null, config: null, seats: [], timeOptions: [], areaOptions: [], seatRefreshTimer: null, dirty: false, reservationPage: 1, reservations: [], reservationQuery: null, lastAuthStatus: null, selectedTargetDate: null, automationHistoryPage: 1, automationHistorySignature: null, sessionListSignature: null, isAdmin: false, roomSeats: [], pickedSeatNames: [], manualSeatEntry: false };

const $ = (id) => document.getElementById(id);
const api = async (url, options = {}) => {
  const response = await fetch(url, { headers: { 'Content-Type': 'application/json', ...(options.headers || {}) }, ...options });
  if (response.status === 401 && url !== '/api/account/me') showAccountGate('login');
  // A gateway/proxy in front of the app can answer with its own HTML error
  // page instead of JSON; never surface that raw body as if it were ours.
  const isJson = (response.headers.get('content-type') || '').includes('application/json');
  const text = await response.text();
  let body = null;
  if (isJson) {
    try { body = text ? JSON.parse(text) : null; } catch (_) { body = null; }
  }
  if (!response.ok) {
    throw new Error(body?.detail || (isJson && text ? text : `请求失败（HTTP ${response.status}），服务可能暂时不可用`));
  }
  return body;
};

function toast(message, kind = 'info') {
  const el = $('toast');
  el.textContent = message;
  el.className = `toast ${kind}`;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, 3500);
}

function confirmDialog(message, { danger = false } = {}) {
  return new Promise((resolve) => {
    const overlay = $('confirm-modal');
    const okBtn = $('confirm-modal-ok');
    const cancelBtn = $('confirm-modal-cancel');
    $('confirm-modal-message').textContent = message;
    okBtn.className = `button ${danger ? 'danger' : 'primary'}`;
    overlay.hidden = false;
    okBtn.focus();
    const cleanup = (result) => {
      overlay.hidden = true;
      okBtn.removeEventListener('click', onOk);
      cancelBtn.removeEventListener('click', onCancel);
      overlay.removeEventListener('click', onOverlay);
      document.removeEventListener('keydown', onKey);
      resolve(result);
    };
    const onOk = () => cleanup(true);
    const onCancel = () => cleanup(false);
    const onOverlay = (event) => { if (event.target === overlay) cleanup(false); };
    const onKey = (event) => { if (event.key === 'Escape') cleanup(false); };
    okBtn.addEventListener('click', onOk);
    cancelBtn.addEventListener('click', onCancel);
    overlay.addEventListener('click', onOverlay);
    document.addEventListener('keydown', onKey);
  });
}

// Disables the button and swaps its label for the duration of fn, then always
// restores both — only for buttons whose enabled state nothing else manages.
async function withBusy(button, fn, busyText = '处理中…') {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = busyText;
  try {
    return await fn();
  } finally {
    button.textContent = original;
    button.disabled = false;
  }
}

function setBadge(id, text, kind = 'neutral') {
  const el = $(id); el.textContent = text; el.className = `badge ${kind}`;
}

function updateAutomationButton() {
  const schoolReady = state.lastAuthStatus === 'authenticated'
    || (state.lastAuthStatus === 'sso_authenticated' && state.config?.keepalive_enabled);
  $('start-automation').disabled = state.dailyEnabled || !state.sessionId || !state.config?.submit_enabled || !state.selectedTargetDate || !schoolReady;
  const form = $('config-form');
  const dailyReady = state.lastAuthStatus === 'authenticated'
    || (state.lastAuthStatus === 'sso_authenticated' && form.elements.keepalive_enabled.checked);
  $('start-daily').disabled = state.dailyEnabled || !state.sessionId || !dailyReady
    || !form.elements.submit_enabled.checked || !form.elements.keepalive_enabled.checked;
}

function beijingDate(offset = 0) {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(new Date());
  const value = (type) => parts.find(part => part.type === type).value;
  const midnight = Date.UTC(Number(value('year')), Number(value('month')) - 1, Number(value('day')) + offset);
  return new Date(midnight).toISOString().slice(0, 10);
}

function syncTargetDateChoices() {
  const select = $('config-form').elements.target_date;
  const today = beijingDate();
  const tomorrow = beijingDate(1);
  const dayAfterTomorrow = beijingDate(2);
  select.querySelector('[value="today"]').textContent = `今天（${today}）`;
  select.querySelector('[value="tomorrow"]').textContent = `明天（${tomorrow}）`;
  select.querySelector('[value="day_after_tomorrow"]').textContent = `后天（${dayAfterTomorrow}）`;
  select.querySelector('[value="fixed"]')?.remove();
  if (state.selectedTargetDate === today) select.value = 'today';
  else if (state.selectedTargetDate === tomorrow) select.value = 'tomorrow';
  else if (state.selectedTargetDate === dayAfterTomorrow) select.value = 'day_after_tomorrow';
  else if (state.selectedTargetDate) {
    const fixed = new Option(`已固定：${state.selectedTargetDate}（请重新选择）`, 'fixed', false, true);
    fixed.disabled = true;
    select.add(fixed);
  } else select.value = '';
  $('target-date-preview').textContent = state.selectedTargetDate
    ? `预约日期：${state.selectedTargetDate}${select.value === 'fixed' ? '；已不在今天/明天/后天范围，请重新选择' : ''}`
    : '旧配置尚未固定日期，请选择今天、明天或后天';
  $('target-date').textContent = state.selectedTargetDate || '—';
  updateAutomationButton();
}

function setSession(id) {
  clearTimeout(state.authPollTimer);
  state.sessionId = id;
  state.lastAuthStatus = null;
  updateAutomationButton();
  if (id) sessionStorage.setItem('ustb_auth_session_id', id);
  else sessionStorage.removeItem('ustb_auth_session_id');
  $('session-id').textContent = id || '—';
  $('close-session').disabled = !id;
  $('query-seats').disabled = !id;
  $('query-my-reservations').disabled = !id;
  if (!id) {
    state.reservations = [];
    state.reservationQuery = null;
    $('my-reservations').className = 'seat-results empty';
    $('my-reservations').textContent = '登录后可查询自己的预约。';
    $('reservations-prev').disabled = true;
    $('reservations-next').disabled = true;
  }
  restartSeatRefreshTimer();
}

function fillConfig(config) {
  state.config = config;
  state.dailyEnabled = Boolean(config.daily_auto_enabled);
  const form = $('config-form');
  for (const [name, value] of Object.entries(config)) {
    const input = form.elements[name];
    if (!input) continue;
    if (input.type === 'checkbox') input.checked = Boolean(value);
    else if (input.tagName === 'SELECT' && name === 'room_id') {
      const options = state.areaOptions.length
        ? state.areaOptions
        : [{ value: String(value ?? ''), label: config.room_name || String(value ?? '') }];
      setSelectOptions(input, options, String(value ?? ''));
    }
    else if (input.tagName === 'SELECT' && (name === 'start_time' || name === 'end_time' || name === 'target_date')) continue;
    else if (name === 'seat_names') {
      state.pickedSeatNames = Array.isArray(value) ? [...value] : [];
      input.value = state.pickedSeatNames.join(',');
    }
    else input.value = Array.isArray(value) ? value.join(',') : value ?? '';
  }
  state.selectedTargetDate = /^\d{4}-\d{2}-\d{2}$/.test(config.target_date) ? config.target_date : null;
  syncTargetDateChoices();
  updateTimeSelects(state.timeOptions, config.start_time, config.end_time);
  $('target-room').textContent = config.room_name || '—';
  setBadge('config-badge', '已加载', 'success');
  updateAutomationButton();
  restartSeatRefreshTimer();
  renderSeatChips();
  updateSeatFieldVisibility();
}

function setSelectOptions(select, options, selected) {
  const unique = [];
  for (const option of options) if (!unique.some(x => x.value === option.value)) unique.push(option);
  select.innerHTML = unique.map(option => `<option value="${option.value}">${option.label || option.value}</option>`).join('');
  if (selected && unique.some(option => option.value === selected)) select.value = selected;
}

// Both time fields use the same renderer so their options always come from
// the server's resvRule/openTimes data rather than browser-local guesses.
const TimeSelect = {
  render(select, values, selected) {
    setSelectOptions(select, values.map(value => ({ value, label: value })), selected);
  },
};

function clockTime(minutes) {
  return `${String(Math.floor(minutes / 60)).padStart(2, '0')}:${String(minutes % 60).padStart(2, '0')}`;
}

const fallbackTimes = Array.from({ length: 288 }, (_, index) => clockTime(index * 5));

function endChoices(start, options, preferredEnd = '') {
  if (options.length) return options.find(option => option.start === start)?.ends || [];
  const values = fallbackTimes.filter(value => value > start);
  if (/^([01]\d|2[0-3]):[0-5]\d$/.test(preferredEnd) && preferredEnd > start && !values.includes(preferredEnd)) {
    values.push(preferredEnd);
    values.sort();
  }
  return values;
}

function syncSelectedTime() {
  if (!state.config) return;
  const start = document.querySelector('[name="start_time"]').value;
  const end = document.querySelector('[name="end_time"]').value;
  state.config.start_time = start;
  state.config.end_time = end;
  $('target-time').textContent = `${start || '—'} - ${end || '—'}`;
}

function updateTimeSelects(timeOptions, preferredStart, preferredEnd) {
  state.timeOptions = timeOptions || [];
  const starts = state.timeOptions.length
    ? state.timeOptions.map(option => option.start)
    : fallbackTimes.slice(0, -1);
  if (!state.timeOptions.length && /^([01]\d|2[0-3]):[0-5]\d$/.test(preferredStart)
      && preferredStart < '23:59' && !starts.includes(preferredStart)) {
    starts.push(preferredStart);
    starts.sort();
  }
  const startSelect = document.querySelector('[name="start_time"]');
  const endSelect = document.querySelector('[name="end_time"]');
  TimeSelect.render(startSelect, starts, preferredStart);
  TimeSelect.render(endSelect, endChoices(startSelect.value, state.timeOptions, preferredEnd), preferredEnd);
  $('time-hint').textContent = state.timeOptions.length
    ? '已按查询到的座位规则限制时间；修改后请保存参数。'
    : '查询座位前可按 5 分钟步长设置；查询后会显示该座位的实际可选范围。';
  syncSelectedTime();
}

function flattenAreas(nodes, prefix = '') {
  const result = [];
  for (const node of nodes || []) {
    const label = prefix && node.name !== prefix ? `${prefix} / ${node.name}` : (node.label || node.name);
    const counts = Number.isFinite(node.remain) && Number.isFinite(node.total) ? `（${node.remain} / ${node.total} 空闲）` : '';
    result.push({ value: String(node.id), label: label + counts, name: node.name });
    if (node.children?.length) result.push(...flattenAreas(node.children, label));
  }
  return result;
}

async function loadAreas() {
  if (!state.sessionId) return;
  try {
    const result = await api(`/api/areas?auth_session_id=${encodeURIComponent(state.sessionId)}`);
    state.areaOptions = flattenAreas(result.areas || []);
    const select = $('room-select');
    const configuredId = String(state.config?.room_id ?? '');
    const configuredName = String(state.config?.room_name ?? '');
    const selectedOption = state.areaOptions.find(option => option.value === configuredId)
      || state.areaOptions.find(option => option.name === configuredName);
    setSelectOptions(select, state.areaOptions, selectedOption?.value || '');
    if (select.value) {
      const selected = state.areaOptions.find(option => option.value === select.value);
      if (selected) {
        $('config-form').elements.room_name.value = selected.name;
        state.config.room_id = Number(selected.value);
        state.config.room_name = selected.name;
        await loadRoomSeats(selected.value);
      }
    }
    $('config-message').textContent = `已读取 ${state.areaOptions.length} 个可选楼层 / 区域`;
  } catch (error) {
    $('config-message').textContent = `区域读取失败：${error.message}`;
  }
}

async function loadRoomSeats(roomId) {
  if (!state.sessionId || !roomId) return;
  try {
    const result = await api(`/api/rooms/${roomId}/seats?auth_session_id=${encodeURIComponent(state.sessionId)}`);
    state.roomSeats = result.seats || [];
  } catch (error) {
    state.roomSeats = [];
    $('config-message').textContent = `座位地图读取失败：${error.message}`;
  }
  renderSeatPicker();
  updateTimeFromPickedSeats();
}

function updateSeatFieldVisibility() {
  const manual = state.manualSeatEntry;
  $('seat-names-manual').hidden = !manual;
  $('toggle-manual-seats').textContent = manual ? '改用地图选座' : '手动输入座位名';
  $('seat-chips').hidden = manual || state.pickedSeatNames.length === 0;
  $('seat-picker-empty').hidden = manual || state.roomSeats.length > 0;
  $('seat-picker').hidden = manual || state.roomSeats.length === 0;
  $('seat-legend').hidden = manual || state.roomSeats.length === 0;
}

function renderSeatChips() {
  const container = $('seat-chips');
  container.replaceChildren();
  state.pickedSeatNames.forEach((name, index) => {
    const chip = document.createElement('span');
    chip.className = 'seat-chip';
    const idx = document.createElement('span');
    idx.className = 'chip-index';
    idx.textContent = String(index + 1);
    const label = document.createElement('span');
    label.textContent = name;
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.setAttribute('aria-label', `移除 ${name}`);
    remove.textContent = '×';
    remove.addEventListener('click', () => setPickedSeatNames(state.pickedSeatNames.filter(n => n !== name)));
    chip.append(idx, label, remove);
    container.append(chip);
  });
}

function updateTimeFromPickedSeats() {
  const seat = state.roomSeats.find(s => s.dev_name === state.pickedSeatNames[0]);
  const startSelect = document.querySelector('[name="start_time"]');
  const endSelect = document.querySelector('[name="end_time"]');
  updateTimeSelects(seat?.time_options || [], startSelect.value, endSelect.value);
}

function setPickedSeatNames(names, { markDirty = true } = {}) {
  state.pickedSeatNames = [...names];
  $('seat-names-manual').value = state.pickedSeatNames.join(',');
  renderSeatChips();
  renderSeatPicker();
  updateTimeFromPickedSeats();
  updateSeatFieldVisibility();
  if (markDirty) state.dirty = true;
}

function toggleSeatSelection(name) {
  const exists = state.pickedSeatNames.includes(name);
  setPickedSeatNames(exists ? state.pickedSeatNames.filter(n => n !== name) : [...state.pickedSeatNames, name]);
}

function renderSeatPicker() {
  const seats = state.roomSeats;
  const map = $('seat-picker');
  map.replaceChildren();
  updateSeatFieldVisibility();
  if (!seats.length) return;
  const withCoord = seats.filter(s => s.coordinate);
  if (!withCoord.length) {
    const note = document.createElement('p');
    note.className = 'muted';
    note.style.margin = '0';
    note.textContent = '该区域未提供座位地图坐标，请点击“手动输入座位名”。';
    map.append(note);
    return;
  }
  const xs = withCoord.map(s => s.coordinate.x);
  const ys = withCoord.map(s => s.coordinate.y);
  const minX = Math.min(...xs), maxX = Math.max(...xs);
  const minY = Math.min(...ys), maxY = Math.max(...ys);
  const spanX = Math.max(1e-6, maxX - minX);
  const spanY = Math.max(1e-6, maxY - minY);
  const mapEl = document.createElement('div');
  mapEl.className = 'seat-picker-map';
  for (const seat of withCoord) {
    const free = seat.open_state === 1 && seat.dev_status === 0 && !seat.reservations?.length;
    const closed = !(seat.open_state === 1 && seat.dev_status === 0);
    const selected = state.pickedSeatNames.includes(seat.dev_name);
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = `seat-dot-btn ${selected ? 'selected' : closed ? 'closed' : free ? 'free' : 'booked'}`;
    btn.style.left = `${((seat.coordinate.x - minX) / spanX) * 92 + 4}%`;
    btn.style.top = `${((seat.coordinate.y - minY) / spanY) * 85 + 7}%`;
    btn.title = `${seat.dev_name}${seat.powered ? '（带电）' : ''}${closed ? '（不开放）' : free ? '（空闲）' : '（已占用）'}`;
    if (selected) btn.textContent = String(state.pickedSeatNames.indexOf(seat.dev_name) + 1);
    if (seat.powered) {
      const mark = document.createElement('span');
      mark.className = 'power-mark';
      mark.textContent = '⚡';
      btn.append(mark);
    }
    btn.addEventListener('click', () => toggleSeatSelection(seat.dev_name));
    mapEl.append(btn);
  }
  map.append(mapEl);
}

function restartSeatRefreshTimer() {
  if (state.seatRefreshTimer) clearInterval(state.seatRefreshTimer);
  state.seatRefreshTimer = null;
  if (!state.sessionId || !state.config || state.lastAuthStatus !== 'authenticated') return;
  const seconds = Math.max(5, Number(state.config.poll_interval_seconds || 30));
  state.seatRefreshTimer = setInterval(() => {
    if (!document.hidden && state.sessionId && !state.dirty) querySeats(true);
  }, seconds * 1000);
}

async function loadConfig() {
  const config = await api('/api/config');
  fillConfig(config);
}

function currentConfigPayload(forDaily = false) {
  const form = $('config-form');
  const roomIdText = form.elements.room_id.value.trim();
  if (!roomIdText || Number(roomIdText) <= 0) {
    throw new Error('楼层 / 区域尚未加载完成，请先登录并点击“刷新楼层 / 区域”');
  }
  if (!forDaily && (!state.selectedTargetDate || ![beijingDate(), beijingDate(1), beijingDate(2)].includes(state.selectedTargetDate))) {
    throw new Error('请重新选择“今天”“明天”或“后天”，确认显示的具体预约日期');
  }
  return {
    login_source: form.elements.login_source.value,
    room_name: form.elements.room_name.value,
    room_id: Number(roomIdText),
    target_date: state.selectedTargetDate || state.config?.target_date || 'tomorrow',
    seat_names: form.elements.seat_names.value.split(',').map(x => x.trim()).filter(Boolean),
    start_time: form.elements.start_time.value,
    end_time: form.elements.end_time.value,
    poll_interval_seconds: Number(form.elements.poll_interval_seconds.value),
    keepalive_interval_seconds: Number(form.elements.keepalive_interval_seconds.value),
    auto_trigger_time: form.elements.auto_trigger_time.value,
    auto_retry_interval_seconds: Number(form.elements.auto_retry_interval_seconds.value),
    keepalive_enabled: form.elements.keepalive_enabled.checked,
    submit_enabled: form.elements.submit_enabled.checked,
  };
}

async function persistConfig(showMessage = true, forDaily = false) {
  const payload = currentConfigPayload(forDaily);
  const saved = await api('/api/config', { method: 'PUT', body: JSON.stringify(payload) });
  fillConfig(saved);
  state.dirty = false;
  if (showMessage) $('config-message').textContent = '已保存到 config.json';
  return saved;
}

async function saveConfig(event) {
  event.preventDefault();
  try {
    await persistConfig(true, state.dailyEnabled);
    toast('预约参数已保存', 'success');
  } catch (error) { $('config-message').textContent = error.message; toast(error.message, 'error'); }
}

async function createSession() {
  try {
    const result = await api('/api/auth/sessions', { method: 'POST' });
    setSession(result.session_id);
    $('qr-image').src = `${result.qr_url}?t=${Date.now()}`;
    $('qr-image').hidden = false;
    $('qr-placeholder').hidden = true;
    setBadge('auth-badge', '等待扫码', 'warning');
    $('auth-message').textContent = '请使用微信扫描二维码，页面会自动检测登录结果。';
    pollAuth();
    toast('二维码已生成', 'success');
  } catch (error) { toast(error.message, 'error'); setBadge('auth-badge', '错误', 'error'); }
  finally { await loadSessionList(); }
}

const authStatusLabels = {
  authenticated: ['已登录', 'success'], sso_authenticated: ['学校已登录', 'warning'],
  library_scan_required: ['图书馆需扫码', 'warning'], waiting_scan: ['等待扫码', 'warning'],
  callback_received: ['处理中', 'warning'], starting: ['启动中', 'neutral'],
  expired: ['已过期', 'error'], error: ['错误', 'error'], closed: ['已关闭', 'neutral'],
};
const QR_PENDING_STATES = ['waiting_scan', 'callback_received', 'library_scan_required'];

async function pollAuth() {
  clearTimeout(state.authPollTimer);
  if (!state.sessionId) return;
  const sessionId = state.sessionId;
  try {
    const status = await api(`/api/auth/sessions/${encodeURIComponent(sessionId)}`);
    if (state.sessionId !== sessionId) return;
    const previousAuthStatus = state.lastAuthStatus;
    state.lastAuthStatus = status.status;
    updateAutomationButton();
    const [text, kind] = authStatusLabels[status.status] || [status.status, 'neutral'];
    setBadge('auth-badge', text, kind);
    if (status.user) {
      $('user-acc').textContent = status.user.acc_no || '—';
      $('user-name').textContent = status.user.user_name || '已认证用户';
    }
    if (status.status === 'authenticated') {
      $('auth-message').textContent = '登录成功，浏览器上下文已建立；保活任务会按配置运行。';
      $('query-seats').disabled = false;
      if (previousAuthStatus !== 'authenticated') {
        restartSeatRefreshTimer();
        await loadAreas();
      }
      state.authPollTimer = setTimeout(pollAuth, 10000);
      return;
    }
    if (status.status === 'sso_authenticated') {
      $('auth-message').textContent = `${status.library_error || '学校统一登录已完成，正在等待图书馆恢复。'} 已开启保活和预约提交时，可先启动自动预约。`;
      $('query-seats').disabled = true;
      if (previousAuthStatus === 'authenticated') restartSeatRefreshTimer();
    }
    if (status.status === 'library_scan_required') {
      $('auth-message').textContent = '学校统一登录已完成；图书馆仍要求单独扫码，请扫描新的二维码。';
      if (previousAuthStatus !== 'library_scan_required') $('qr-image').src = `/api/auth/sessions/${state.sessionId}/qr?t=${Date.now()}`;
    }
    if (status.status === 'error') $('auth-message').textContent = status.error || '登录页面无法打开，请重新生成二维码。';
    if (status.status === 'expired' || status.status === 'closed') {
      setSession(null);
      return;
    }
    if (status.status !== 'expired' && status.status !== 'error' && status.status !== 'closed') {
      state.authPollTimer = setTimeout(pollAuth, status.status === 'sso_authenticated' ? 10000 : 2500);
    }
  } catch (error) {
    if (state.sessionId !== sessionId) return;
    $('auth-message').textContent = error.message;
    if (error.message.includes('auth session not found')) setSession(null);
    else if (state.sessionId) state.authPollTimer = setTimeout(pollAuth, 5000);
  }
}

function clearCurrentSessionUI() {
  setSession(null);
  $('qr-image').hidden = true;
  $('qr-placeholder').hidden = false;
  setBadge('auth-badge', '未登录', 'neutral');
  $('user-name').textContent = '—';
  $('user-acc').textContent = '—';
}

async function closeSession() {
  if (!state.sessionId) return;
  try {
    const result = await api(`/api/auth/sessions/${state.sessionId}`, { method: 'DELETE' });
    if (result?.automation_stopped) {
      toast('会话已关闭，使用它的自动预约任务已停止', 'success');
      await loadAutomationStatus();
    }
  } catch (error) { toast(error.message, 'error'); }
  clearCurrentSessionUI();
  await loadSessionList();
}

async function loadSessionList() {
  let result;
  try { result = await api('/api/auth/sessions'); }
  catch (error) {
    state.sessionListSignature = null;
    $('session-list').replaceChildren(Object.assign(document.createElement('p'), { className: 'muted', textContent: `会话列表读取失败：${error.message}` }));
    return;
  }
  const sessions = Array.isArray(result?.sessions) ? result.sessions : [];
  const capacity = result?.capacity;
  // Re-render only on change: this refreshes every few seconds, and
  // replacing the buttons mid-click would swallow the click.
  const signature = JSON.stringify([sessions, capacity, state.sessionId]);
  if (signature === state.sessionListSignature) return;
  state.sessionListSignature = signature;
  renderSessionList(sessions, capacity);
}

function renderSessionList(sessions, capacity) {
  const capacityEl = $('session-capacity');
  const hint = $('session-capacity-hint');
  const full = capacity && capacity.in_use >= capacity.max;
  capacityEl.textContent = capacity ? `全系统浏览器会话 ${capacity.in_use} / ${capacity.max}` : '';
  capacityEl.classList.toggle('full', Boolean(full));
  hint.hidden = !full;
  if (full) {
    hint.textContent = sessions.length
      ? '会话已满，无法生成新二维码。请先删除下面不再使用的会话。'
      : '会话已满，占用的会话都属于其他账号，暂时无法生成新二维码；请联系管理员调高 max_browser_sessions。';
  }
  const list = $('session-list');
  list.replaceChildren();
  if (!sessions.length) {
    list.append(Object.assign(document.createElement('p'), { className: 'muted', textContent: '暂无会话。点击“生成二维码”开始登录。' }));
    return;
  }
  for (const entry of sessions) {
    const current = entry.session_id === state.sessionId;
    const row = document.createElement('div');
    row.className = `session-row${current ? ' current' : ''}`;
    const info = document.createElement('div');
    info.className = 'session-row-info';
    const title = document.createElement('div');
    title.className = 'session-row-title';
    const [label, kind] = authStatusLabels[entry.status] || [entry.status, 'neutral'];
    const badge = Object.assign(document.createElement('span'), { className: `badge ${kind}`, textContent: label });
    const name = document.createElement('span');
    name.textContent = entry.user_name ? `${entry.user_name}${entry.acc_no ? `（${entry.acc_no}）` : ''}` : '未完成图书馆登录';
    title.append(badge, name);
    if (current) title.append(Object.assign(document.createElement('span'), { className: 'tag current', textContent: '当前' }));
    if (entry.used_by_automation) title.append(Object.assign(document.createElement('span'), { className: 'tag automation', textContent: '自动预约中' }));
    const meta = document.createElement('small');
    meta.textContent = `创建于 ${taskTime(entry.created_at)} · …${entry.session_id.slice(-6)}`;
    info.append(title, meta);
    const actions = document.createElement('div');
    actions.className = 'session-row-actions';
    if (!current && !['expired', 'error', 'closed'].includes(entry.status)) {
      const use = Object.assign(document.createElement('button'), { type: 'button', className: 'button ghost small', textContent: '使用' });
      use.addEventListener('click', () => useSession(entry));
      actions.append(use);
    }
    const remove = Object.assign(document.createElement('button'), { type: 'button', className: 'button danger-ghost small', textContent: '删除' });
    remove.addEventListener('click', () => deleteSession(entry, remove));
    actions.append(remove);
    row.append(info, actions);
    list.append(row);
  }
}

async function useSession(entry) {
  setSession(entry.session_id);
  $('backend-session-picker').hidden = true;
  $('user-name').textContent = '—';
  $('user-acc').textContent = '—';
  const needsQr = QR_PENDING_STATES.includes(entry.status);
  $('qr-image').hidden = !needsQr;
  $('qr-placeholder').hidden = needsQr;
  if (needsQr) $('qr-image').src = `/api/auth/sessions/${encodeURIComponent(entry.session_id)}/qr?t=${Date.now()}`;
  await pollAuth();
  await loadSessionList();
}

async function deleteSession(entry, button) {
  const warning = entry.used_by_automation
    ? '\n\n这个会话正被自动预约任务使用，删除后该任务会立即停止；如果开着每日模式，它会改用你的其他有效会话，没有的话会等待重新扫码。'
    : '';
  if (!(await confirmDialog(`删除登录会话 …${entry.session_id.slice(-6)}？${warning}`, { danger: true }))) return;
  button.disabled = true;
  button.textContent = '删除中…';
  try {
    const result = await api(`/api/auth/sessions/${encodeURIComponent(entry.session_id)}`, { method: 'DELETE' });
    if (entry.session_id === state.sessionId) clearCurrentSessionUI();
    toast(result?.automation_stopped ? '会话已删除，使用它的自动预约任务已停止' : '会话已删除', 'success');
    if (result?.automation_stopped) await loadAutomationStatus();
  } catch (error) { toast(error.message, 'error'); }
  finally {
    state.sessionListSignature = null;  // force a re-render even if nothing changed, to restore the button
    await loadSessionList();
  }
}

function renderSeats(result) {
  state.seats = result.seats || [];
  $('target-date').textContent = result.target_date || '—';
  $('target-room').textContent = result.room_name || '—';
  const previousStart = document.querySelector('[name="start_time"]').value;
  const previousEnd = document.querySelector('[name="end_time"]').value;
  updateTimeSelects(result.time_options || state.seats[0]?.time_options || [], previousStart, previousEnd);
  if (previousStart !== state.config.start_time || previousEnd !== state.config.end_time) {
    state.dirty = true;
    $('config-message').textContent = '原时间不在该座位的可选范围内，已显示可选时间；请保存参数。';
  }
  const container = $('seat-results');
  if (!state.seats.length) { container.className = 'seat-results empty'; container.textContent = '没有找到配置中的座位。'; return; }
  container.className = 'seat-results';
  container.innerHTML = state.seats.map(seat => {
    const free = seat.open_state === 1 && seat.dev_status === 0 && !seat.reservations?.length;
    return `<div class="seat-item"><div><strong>${seat.dev_name}</strong><small>devId: ${seat.dev_id}</small></div><div><small>开放</small>${seat.open_times?.map(x => `${x.openStartTime}-${x.openEndTime}`).join(', ') || '—'}</div><div><small>当前预约</small>${seat.reservations?.length ? '有预约' : '无'}</div><div><span class="badge ${free ? 'success' : 'warning'}">${free ? '可选' : '需检查'}</span></div></div>`;
  }).join('');
  $('submit-reservation').disabled = !state.config.submit_enabled || !state.seats.length;
  updateAutomationButton();
}

async function querySeats(silent = false) {
  if (!state.sessionId) return toast('请先完成 SSO 登录');
  try {
    if (!silent) {
      await persistConfig(false);
      setBadge('seat-badge', '查询中', 'warning');
    }
    renderSeats(await api(`/api/seats?auth_session_id=${encodeURIComponent(state.sessionId)}`));
    setBadge('seat-badge', '查询完成', 'success');
  }
  catch (error) { setBadge('seat-badge', '查询失败', 'error'); if (!silent) toast(error.message); }
}

document.querySelector('[name="start_time"]').addEventListener('change', () => {
  const start = document.querySelector('[name="start_time"]').value;
  const endSelect = document.querySelector('[name="end_time"]');
  TimeSelect.render(endSelect, endChoices(start, state.timeOptions, endSelect.value), endSelect.value);
  syncSelectedTime();
  state.dirty = true;
});

document.querySelector('[name="end_time"]').addEventListener('change', () => {
  syncSelectedTime();
  state.dirty = true;
});

$('config-form').elements.target_date.addEventListener('change', () => {
  state.timeOptions = [];
  updateTimeSelects([], document.querySelector('[name="start_time"]').value, document.querySelector('[name="end_time"]').value);
  state.dirty = true;
});

$('seat-names-manual').addEventListener('change', (event) => {
  const names = event.target.value.split(',').map(x => x.trim()).filter(Boolean);
  setPickedSeatNames(names);
});

$('toggle-manual-seats').addEventListener('click', () => {
  state.manualSeatEntry = !state.manualSeatEntry;
  updateSeatFieldVisibility();
  if (state.manualSeatEntry) $('seat-names-manual').focus();
});

$('room-select').addEventListener('change', async () => {
  const select = $('room-select');
  const selected = state.areaOptions.find(option => option.value === select.value);
  if (!selected) return;
  $('config-form').elements.room_name.value = selected.name;
  state.config.room_id = Number(selected.value);
  state.config.room_name = selected.name;
  $('target-room').textContent = selected.name;
  state.dirty = true;
  // A seat from the previous room almost certainly doesn't exist in the new
  // one; starting the picker empty avoids silently keeping a stale target.
  setPickedSeatNames([]);
  select.disabled = true;
  try { await loadRoomSeats(selected.value); } finally { select.disabled = false; }
});

async function submitReservation() {
  if (!state.sessionId) return toast('请先完成 SSO 登录', 'error');
  if (!(await confirmDialog('确认保存当前页面参数并提交预约？程序会只提交一次。', { danger: true }))) return;
  try {
    await persistConfig(false);
    const result = await api('/api/reservations/submit', { method: 'POST', body: JSON.stringify({ auth_session_id: state.sessionId, confirm: true }) });
    toast(result.success ? '预约提交成功' : `预约失败：${result.message || '未知原因'}`, result.success ? 'success' : 'error');
    setBadge('seat-badge', result.success ? '预约成功' : '预约失败', result.success ? 'success' : 'error');
    if (result.success) await queryMyReservations(1);
  } catch (error) { toast(error.message, 'error'); }
}

function renderMyReservations(result) {
  state.reservations = result.records || [];
  state.reservationPage = result.page;
  state.reservationQuery = { day: result.day, page: result.page };
  const container = $('my-reservations');
  container.replaceChildren();
  if (!state.reservations.length) {
    container.className = 'seat-results empty';
    container.textContent = '该日期范围内没有预约。';
  } else {
    container.className = 'seat-results';
    for (const record of state.reservations) {
      const entry = document.createElement('div');
      entry.className = 'reservation-entry';
      const info = document.createElement('div');
      const seat = record.devices?.map(device => device.name || '未知座位').join('、') || '座位信息未返回';
      const room = record.devices?.map(device => device.room).filter(Boolean).join('、') || '区域未返回';
      const title = document.createElement('strong');
      title.textContent = `${seat} · ${room}`;
      const time = document.createElement('small');
      time.textContent = `${record.begin_time || '—'} 至 ${record.end_time || '—'}`;
      const status = document.createElement('small');
      status.textContent = `预约编号 ${record.resv_id ?? '—'} · ${record.cancellable ? '可取消' : `不可取消（状态 ${record.status}）`}`;
      info.append(title, time, status);
      entry.append(info);
      if (record.cancellable) {
        const cancel = document.createElement('button');
        cancel.type = 'button';
        cancel.className = 'button danger';
        cancel.textContent = '取消预约';
        cancel.addEventListener('click', () => cancelMyReservation(record));
        entry.append(cancel);
      }
      container.append(entry);
    }
  }
  const count = Number(result.count) || 0;
  $('reservations-page').textContent = `第 ${result.page} 页 · 共 ${count} 条`;
  $('reservations-prev').disabled = result.page <= 1;
  $('reservations-next').disabled = result.page * result.page_size >= count;
}

async function queryMyReservations(page = 1) {
  if (!state.sessionId) return toast('请先完成 SSO 登录');
  const day = $('reservation-day').value;
  if (!['today', 'tomorrow'].includes(day)) return toast('请选择今天或明天');
  try {
    setBadge('my-reservations-badge', '查询中', 'warning');
    const params = new URLSearchParams({ auth_session_id: state.sessionId, day, page: String(page) });
    const result = await api(`/api/reservations/mine?${params}`);
    renderMyReservations(result);
    setBadge('my-reservations-badge', '查询完成', 'success');
  } catch (error) {
    setBadge('my-reservations-badge', '查询失败', 'error');
    toast(error.message, 'error');
  }
}

async function cancelMyReservation(record) {
  if (!state.reservationQuery || !record.uuid) return;
  const seat = record.devices?.map(device => device.name).filter(Boolean).join('、') || '该座位';
  if (!(await confirmDialog(`确定取消 ${seat}（${record.begin_time} 至 ${record.end_time}）的预约吗？此操作不可撤销。`, { danger: true }))) return;
  try {
    const result = await api('/api/reservations/cancel', {
      method: 'POST',
      body: JSON.stringify({ auth_session_id: state.sessionId, uuid: record.uuid, ...state.reservationQuery, confirm: true }),
    });
    toast(result.message || '取消预约成功', 'success');
    await queryMyReservations(state.reservationPage);
  } catch (error) { toast(error.message, 'error'); }
}

const automationPhases = {
  idle: '未启动', scheduled: '已安排', waiting_trigger: '等待北京时间触发',
  waiting_library: '等待图书馆恢复并获取会话', attempting: '正在查询和提交',
  retry_wait: '失败后等待重试', success: '预约成功', failed: '预约失败',
  auth_required: '需要重新扫码', blocked: '提交未开启', stopped: '已停止',
  interrupted: '服务重启中断', error: '程序异常',
  expired: '当天窗口已结束',
};

function taskTime(value) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false });
}

const taskStateClass = {
  success: 'state-success',
  failed: 'state-error', error: 'state-error', auth_required: 'state-error',
  attempting: 'state-active',
  scheduled: 'state-waiting', waiting_trigger: 'state-waiting', waiting_library: 'state-waiting', retry_wait: 'state-waiting', blocked: 'state-waiting',
};

function taskRecord(record, history = false) {
  const entry = document.createElement(history ? 'details' : 'div');
  entry.className = `task-record ${taskStateClass[record.state] || ''}`;
  const title = document.createElement(history ? 'summary' : 'div');
  title.className = 'task-record-head';
  const titleText = document.createElement('span');
  titleText.className = 'task-record-title';
  titleText.textContent = `${record.target_date || '未设置日期'} · ${record.room_name || '未设置区域'}`;
  const stateBadge = document.createElement('span');
  const badgeKind = { success: 'success', failed: 'error', error: 'error', auth_required: 'error', attempting: 'warning' }[record.state] || 'neutral';
  stateBadge.className = `badge ${badgeKind}`;
  stateBadge.textContent = automationPhases[record.state] || record.state;
  title.append(titleText, stateBadge);
  const fields = document.createElement('dl');
  fields.className = 'task-fields';
  const values = [
    ['预约模式', record.mode === 'daily' ? '每日自动' : '单次预约'],
    ['任务编号', record.task_id], ['状态', automationPhases[record.state] || record.state],
    ['预约日期', record.target_date], ['楼层 / 区域', record.room_name],
    ['候选座位', record.seat_names?.join('、')], ['预约时段', `${record.start_time || '—'} — ${record.end_time || '—'}`],
    ['触发时间（北京时间）', taskTime(record.next_trigger_at)], ['失败重试间隔', `${record.retry_interval_seconds ?? '—'} 秒`],
    ['任务创建时间', taskTime(record.started_at)], ['任务结束时间', taskTime(record.finished_at)],
    ['尝试次数', String(record.attempts ?? 0)], ['最后尝试时间', taskTime(record.last_attempt_at)],
    ['预约成功座位', record.reserved_seat], ['最近失败原因', record.last_error],
  ];
  for (const [label, value] of values) {
    const cell = document.createElement('div');
    const name = document.createElement('dt'); name.textContent = label;
    const text = document.createElement('dd'); text.textContent = value || '—';
    cell.append(name, text); fields.append(cell);
  }
  entry.append(title, fields);
  return entry;
}

async function loadAutomationHistory(page = state.automationHistoryPage) {
  try {
    const result = await api(`/api/automation/history?page=${page}&page_size=10`);
    state.automationHistoryPage = result.page;
    const container = $('automation-history');
    const signature = JSON.stringify(result);
    if (signature !== state.automationHistorySignature) {
      state.automationHistorySignature = signature;
      container.replaceChildren();
      container.className = result.records.length ? 'seat-results' : 'seat-results empty';
      if (!result.records.length) container.textContent = '暂无历史记录。此功能启用前的任务仍可在服务器运行日志中查看。';
      else for (const record of result.records) container.append(taskRecord(record, true));
    }
    const pages = Math.max(1, Math.ceil(result.total / result.page_size));
    $('automation-history-page').textContent = `第 ${result.page} / ${pages} 页 · 共 ${result.total} 条`;
    $('automation-history-prev').disabled = result.page <= 1;
    $('automation-history-next').disabled = result.page >= pages;
  } catch (error) { $('automation-history').textContent = error.message; state.automationHistorySignature = null; }
}

async function loadAutomationStatus() {
  try {
    const status = await api('/api/automation');
    const daily = status.daily || { enabled: false, state: 'disabled' };
    state.dailyEnabled = daily.enabled;
    const dailyPhase = { disabled: '未开启', scheduled: '已安排', waiting_login: '等待扫码登录',
      blocked: '请开启提交和保活', waiting_single_task: '等待单次任务结束', error: '调度异常' }[daily.state] || daily.state;
    $('daily-status').textContent = daily.enabled
      ? `每日模式：${dailyPhase}；下次触发 ${taskTime(daily.next_trigger_at)}，预约日期 ${daily.target_date || '—'}${daily.last_error ? `；${daily.last_error}` : ''}`
      : '每日模式未开启。';
    $('stop-daily').disabled = !daily.enabled;
    updateAutomationButton();
    const phase = automationPhases[status.state] || status.state;
    const text = status.running
      ? `自动预约运行中：${phase}，目标日期 ${status.target_date || '未设置'}，第 ${status.attempts} 次尝试，${status.next_trigger_at ? `下次触发 ${status.next_trigger_at}，` : ''}重试间隔 ${status.retry_interval_seconds} 秒${status.last_error ? `；${status.last_error}` : ''}。`
      : `自动预约未运行：${phase}${status.last_error ? `；${status.last_error}` : ''}`;
    $('automation-status').textContent = text;
    const current = $('automation-current');
    current.replaceChildren();
    current.className = status.running ? 'seat-results' : 'seat-results empty';
    if (status.running) current.append(taskRecord(status));
    else current.textContent = daily.enabled ? '每日模式已开启，正在安排下一次任务；需要登录时请完成扫码。' : '当前没有运行中的自动预约任务。已结束的任务请查看下方历史记录。';
    $('stop-automation').disabled = !status.running;
    await loadAutomationHistory();
  } catch (error) { $('automation-status').textContent = error.message; }
}

async function startAutomation() {
  if (!state.sessionId) return toast('请先完成 SSO 登录', 'error');
  if (!(await confirmDialog(`确认保存参数并启动自动预约？目标日期固定为 ${state.selectedTargetDate || '未选择'}；将等待下一次北京时间触发，图书馆恢复后尝试预约。`))) return;
  $('start-automation').disabled = true;
  try {
    await persistConfig(false);
    await api('/api/automation/start', { method: 'POST', body: JSON.stringify({ auth_session_id: state.sessionId }) });
    toast('自动预约已启动', 'success');
  } catch (error) { toast(error.message, 'error'); }
  finally { await loadAutomationStatus(); }
}

async function stopAutomation() {
  $('stop-automation').disabled = true;
  try { await api('/api/automation/stop', { method: 'POST' }); toast('自动预约已停止', 'success'); }
  catch (error) { toast(error.message, 'error'); }
  finally { await loadAutomationStatus(); }
}

async function startDaily() {
  if (!state.sessionId) return toast('请先完成扫码登录', 'error');
  const form = $('config-form');
  if (!(await confirmDialog(`开启每日预约？每天北京时间 ${form.elements.auto_trigger_time.value}，预约次日的 ${form.elements.seat_names.value}，时段 ${form.elements.start_time.value} 至 ${form.elements.end_time.value}。`))) return;
  $('start-daily').disabled = true;
  try {
    await persistConfig(false, true);
    await api('/api/automation/daily/start', { method: 'POST', body: JSON.stringify({ auth_session_id: state.sessionId }) });
    toast('每日模式已开启', 'success');
  } catch (error) { toast(error.message, 'error'); }
  finally { await loadAutomationStatus(); }
}

async function stopDaily() {
  $('stop-daily').disabled = true;
  try {
    await api('/api/automation/daily/stop', { method: 'POST' });
    toast('每日模式已关闭', 'success');
  } catch (error) { toast(error.message, 'error'); }
  finally { await loadAutomationStatus(); }
}

async function loadRuntime() {
  try {
    const status = await api('/api/status');
    $('runtime-summary').innerHTML = `<span>服务：${status.service}</span><span>认证会话：${status.auth_sessions}</span><span>预约提交：${status.submit_enabled ? '已开启' : '已关闭'}</span><span>保活：${status.keepalive_enabled ? '已开启' : '已关闭'}</span>`;
    setBadge('service-badge', '运行中', 'success');
    return status;
  } catch (error) { setBadge('service-badge', '离线', 'error'); }
}

function recoverBackendSession(runtime) {
  if (!runtime || !Array.isArray(runtime.sessions)) return;
  const live = runtime.sessions.filter(s => !['expired', 'closed', 'error'].includes(s.status));
  const ready = live.filter(s => ['authenticated', 'sso_authenticated', 'library_scan_required'].includes(s.status));
  const picker = $('backend-session-picker');
  picker.hidden = true;
  // Keep an intentionally selected pending QR; don't replace it with another login.
  if (live.some(s => s.session_id === state.sessionId)) return;
  setSession(null);
  const taskSession = runtime.automation?.running
    ? ready.find(s => s.session_id === runtime.automation.session_id) : null;
  const selected = taskSession || (ready.length === 1 ? ready[0] : null);
  if (selected) {
    setSession(selected.session_id);
    $('qr-image').hidden = true;
    $('qr-placeholder').hidden = false;
    $('auth-message').textContent = '已恢复后台已有登录会话，正在确认状态，无需重新扫码。';
  } else if (ready.length > 1) {
    const select = $('backend-session-select');
    select.replaceChildren(new Option('请选择已有会话', ''));
    for (const session of ready) {
      const label = session.status === 'authenticated' ? '图书馆已登录' : '学校已登录';
      select.add(new Option(`${label} · ${session.session_id.slice(-6)}`, session.session_id));
    }
    picker.hidden = false;
    setBadge('auth-badge', '请选择会话', 'warning');
    $('auth-message').textContent = '后台有多个有效会话，请选择要使用的会话，无需重新扫码。';
  } else {
    setBadge('auth-badge', '未登录', 'neutral');
    $('user-name').textContent = '—';
    $('user-acc').textContent = '—';
  }
}

async function loadLogs() {
  try { const result = await api('/api/logs?limit=200'); $('logs').textContent = result.logs.length ? result.logs.map(x => `${x.time} [${x.level}] ${x.message}`).join('\n') : '暂无日志'; }
  catch (error) { $('logs').textContent = error.message; }
}

async function refreshAll() {
  const results = await Promise.all([loadConfig(), loadRuntime(), loadLogs(), loadAutomationStatus()]);
  recoverBackendSession(results[1]);
  await loadSessionList();
  if (state.sessionId) await pollAuth();
}

function showAccountGate(mode = 'login') {
  $('initial-loading').hidden = true;
  $('app-main').hidden = true;
  $('account-gate').hidden = false;
  $('account-info').hidden = true;
  $('account-logout').hidden = true;
  $('login-form').hidden = mode !== 'login';
  $('register-form').hidden = mode !== 'register';
  $('account-gate-title').textContent = mode === 'login' ? '登录' : '注册';
  const activeForm = mode === 'login' ? $('login-form') : $('register-form');
  activeForm.elements.username.focus();
}

function applyAdminVisibility() {
  $('refresh-logs').hidden = !state.isAdmin;
  $('logs').hidden = !state.isAdmin;
  $('logs-admin-only').hidden = state.isAdmin;
}

function bootApp(me) {
  state.isAdmin = Boolean(me.is_admin);
  $('initial-loading').hidden = true;
  $('account-gate').hidden = true;
  $('app-main').hidden = false;
  $('account-info').hidden = false;
  $('account-logout').hidden = false;
  $('account-username').textContent = me.username;
  applyAdminVisibility();
  setSession(sessionStorage.getItem('ustb_auth_session_id'));
  refreshAll();
}

async function checkAccount() {
  try {
    const me = await api('/api/account/me');
    bootApp(me);
  } catch (_) {
    showAccountGate('login');
  }
}

async function submitAccountForm(form, endpoint, messageEl) {
  const payload = { username: form.elements.username.value.trim(), password: form.elements.password.value };
  messageEl.textContent = '';
  const button = form.querySelector('button[type="submit"]');
  await withBusy(button, async () => {
    try {
      const me = await api(endpoint, { method: 'POST', body: JSON.stringify(payload) });
      bootApp(me);
    } catch (error) { messageEl.textContent = error.message; }
  }, button.textContent + '…');
}

$('login-form').addEventListener('submit', (event) => {
  event.preventDefault();
  submitAccountForm(event.target, '/api/account/login', $('login-message'));
});
$('register-form').addEventListener('submit', (event) => {
  event.preventDefault();
  submitAccountForm(event.target, '/api/account/register', $('register-message'));
});
$('show-register').addEventListener('click', () => showAccountGate('register'));
$('show-login').addEventListener('click', () => showAccountGate('login'));
for (const toggle of document.querySelectorAll('.password-toggle')) {
  toggle.addEventListener('click', () => {
    const input = toggle.previousElementSibling;
    const showing = input.type === 'text';
    input.type = showing ? 'password' : 'text';
    toggle.setAttribute('aria-label', showing ? '显示密码' : '隐藏密码');
    toggle.textContent = showing ? '👁' : '🙈';
  });
}
$('account-logout').addEventListener('click', async () => {
  try { await api('/api/account/logout', { method: 'POST' }); } catch (_) { /* ignore */ }
  setSession(null);
  showAccountGate('login');
  toast('已退出登录');
});

$('config-form').addEventListener('submit', saveConfig);
$('config-form').addEventListener('change', updateAutomationButton);
$('config-form').elements.target_date.addEventListener('change', (event) => {
  if (['today', 'tomorrow', 'day_after_tomorrow'].includes(event.target.value)) {
    state.selectedTargetDate = beijingDate({ today: 0, tomorrow: 1, day_after_tomorrow: 2 }[event.target.value]);
    state.dirty = true;
    syncTargetDateChoices();
  }
});
$('create-session').addEventListener('click', () => withBusy($('create-session'), createSession, '生成中…'));
$('close-session').addEventListener('click', async () => {
  // closeSession() already sets this button's final disabled state via
  // setSession(null); only the busy label is ours to restore here.
  const button = $('close-session');
  const original = button.textContent;
  button.disabled = true;
  button.textContent = '关闭中…';
  try { await closeSession(); } finally { button.textContent = original; }
});
$('backend-session-select').addEventListener('change', async (event) => {
  if (!event.target.value) return;
  setSession(event.target.value);
  $('backend-session-picker').hidden = true;
  await pollAuth();
});
$('query-seats').addEventListener('click', () => withBusy($('query-seats'), () => querySeats(false), '查询中…'));
$('query-my-reservations').addEventListener('click', () => withBusy($('query-my-reservations'), () => queryMyReservations(1), '查询中…'));
$('reservations-prev').addEventListener('click', () => queryMyReservations(state.reservationPage - 1));
$('reservations-next').addEventListener('click', () => queryMyReservations(state.reservationPage + 1));
$('submit-reservation').addEventListener('click', () => withBusy($('submit-reservation'), submitReservation, '提交中…'));
$('start-automation').addEventListener('click', startAutomation);
$('stop-automation').addEventListener('click', stopAutomation);
$('start-daily').addEventListener('click', startDaily);
$('stop-daily').addEventListener('click', stopDaily);
$('refresh-automation').addEventListener('click', () => withBusy($('refresh-automation'), () => loadAutomationStatus(), '刷新中…'));
$('automation-history-prev').addEventListener('click', () => loadAutomationHistory(state.automationHistoryPage - 1));
$('automation-history-next').addEventListener('click', () => loadAutomationHistory(state.automationHistoryPage + 1));
$('refresh-logs').addEventListener('click', () => withBusy($('refresh-logs'), loadLogs, '刷新中…'));
$('refresh-areas').addEventListener('click', () => withBusy($('refresh-areas'), loadAreas, '读取中…'));
$('refresh-all').addEventListener('click', () => withBusy($('refresh-all'), refreshAll, '刷新中…'));

// Reflect service reachability on the badge even before login resolves;
// loadRuntime() takes over with richer detail once an account is in.
(async () => {
  try { const ok = (await fetch('/healthz')).ok; setBadge('service-badge', ok ? '运行中' : '离线', ok ? 'success' : 'error'); }
  catch (_) { setBadge('service-badge', '离线', 'error'); }
})();
checkAccount();
setInterval(() => { if (!$('account-gate').hidden) return; loadAutomationStatus(); loadSessionList(); }, 5000);
setInterval(() => { if (!$('account-gate').hidden) return; syncTargetDateChoices(); }, 60000);
