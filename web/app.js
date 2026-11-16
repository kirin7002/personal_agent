import { VoiceSession } from './voice-client.js';
document.documentElement.dataset.timelineVersion = '20261116.2';
const $ = (id) => document.getElementById(id);
const state = { catalog: null, config: null, tasks: [], selected: null, tab: 'output', cursor: 0, voice: null, busy: false, initialized: false, catalogPath: [], catalogOffset: 0, selectedToolSchema: null, pendingImport: null, replyTask: null, replyRevision: null, replyManual: false, forceNewTask: false, ignoredReplies: new Set(), replyAmbiguous: false, pollPromise: null, events: null };
const labels = { running: '处理中', awaiting_input: '等待补充', waiting: '等待时间 / 外部结果', generated: '模拟成功', completed: '查询完成', partial: '部分完成', failed: '未完成', interrupted: '已中断', cancelled: '已取消', expired: '已过期', unsupported: '不支持' };
const activeStatuses = ['running', 'awaiting_input', 'waiting'];
const revisableStatuses = ['running', 'awaiting_input'];
const terminalStatuses = ['generated', 'completed', 'partial', 'failed', 'interrupted', 'cancelled', 'expired', 'unsupported'];
Object.assign(state, { reviseTask: null, reviseRevision: null, reviseAmbiguous: false, pendingChange: null, followupTask: null, followupRevision: null, pendingFollowup: null, parentTask: null, pendingNewTask: null });
Object.assign(state, { focusTask: null, dialog: null, pendingInput: null, expiredBinding: null, plans: [], planTimezone: '', planBusy: false, editingPlan: null, pendingPlan: null });
const changeLabels = { accepted: '已接收', pending: '等待处理', started: '已提交新目标', needs_input: '等待补充', unconfirmed: '送达未确认', applied: '已生效', rejected: '未接受', superseded: '已被后续改口替代', failed: '未生效' };
let toastTimer;
function toast(text) { $('toast').textContent = text; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, 5000); }
async function api(path, body) {
  if (path !== '/api/session/reset' && (state.sessionInvalid || state.pairingRequired)) throw new Error(state.sessionInvalid ? '设备授权已失效，请显式重新初始化设备' : '请先在连接设置中配对设备');
  const options = body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const response = await fetch(path, options);
  const data = await response.json();
  if (response.status === 401) invalidateDevice();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求参数或服务状态不正确');
  return data;
}
function invalidateDevice() {
  state.sessionInvalid = true; state.voice?.stop(); state.events?.close(); state.conversationStream?.close(); clearTimeout(state.conversationRetry);
  $('deviceRecovery').hidden = false; $('sendButton').disabled = true; $('voiceButton').disabled = true;
  $('syncStatus').textContent = '设备授权已失效';
}
function element(tag, className, text) { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node; }
function nearBottom() { const el = $('chat'); return el.scrollHeight-el.scrollTop-el.clientHeight < 100; }
function scrollChat(force = false) { if (force || nearBottom()) $('chat').scrollTop = $('chat').scrollHeight; }
Object.assign(state, { device: null, messages: new Map(), messageNodes: new Map(), messageCursor: 0, conversationStream: null, voiceOwner: null, voiceStepCursors: new Map() });
function captureTimelineScroll() {
  const chat = $('chat'), viewport = chat.getBoundingClientRect();
  const anchor = [...$('conversationMessages').children].find(node => {
    const rect = node.getBoundingClientRect(); return rect.bottom > viewport.top && rect.top < viewport.bottom;
  });
  return { follow: nearBottom(), top: chat.scrollTop, anchor, offset: anchor ? anchor.getBoundingClientRect().top - viewport.top : 0 };
}
function moveTimelineCard(container, card, before = null) {
  const scrolled = [card, ...card.querySelectorAll('*')].filter(node => node.scrollTop || node.scrollLeft)
    .map(node => ({ node, top: node.scrollTop, left: node.scrollLeft }));
  container.insertBefore(card, before);
  // Reparenting keeps <details> state, but Chrome resets descendant scroll offsets.
  for (const { node, top, left } of scrolled) { node.scrollTop = top; node.scrollLeft = left; }
}
function reconcileTimeline(position = captureTimelineScroll()) {
  const container = $('conversationMessages');
  const messages = [...container.querySelectorAll(':scope > [data-message-id]')];
  const latestUser = new Map(), byTask = new Map();
  for (const node of messages) {
    const message = state.messages.get(Number(node.dataset.messageId));
    if (!message?.task_id) continue;
    if (!byTask.has(message.task_id)) byTask.set(message.task_id, []);
    byTask.get(message.task_id).push({ node, message });
    if (message.role === 'user') latestUser.set(message.task_id, node);
  }
  const lastMessage = messages.at(-1);
  const tasks = new Map(state.tasks.map(task => [task.id, task]));
  for (const card of container.querySelectorAll(':scope > .task[data-task-id]')) {
    const task = tasks.get(card.dataset.taskId), related = byTask.get(card.dataset.taskId) || [];
    const user = latestUser.get(card.dataset.taskId);
    const userId = Number(user?.dataset.messageId || 0);
    const currentRevision = message => message.task_revision == null || !task || Number(message.task_revision) === Number(task.revision);
    const feedbackKind = task?.status === 'awaiting_input' ? 'task.question' : `task.${task?.status}`;
    const feedback = related.find(({ message }) => message.role !== 'user' && message.kind === feedbackKind &&
      currentRevision(message) && !message.superseded && message.id > userId);
    const acknowledgement = related.filter(({ message }) => message.role !== 'user' &&
      ['reply', 'voice_reply'].includes(message.kind) && currentRevision(message) && !message.superseded && message.id > userId).at(-1);
    // A card owns execution details; source-event messages own question/result text.
    // Place the process after acceptance and before its result, not a completed
    // result card followed by an older "working on it" acknowledgement.
    const before = feedback?.node || (!user && !acknowledgement ? related.find(({ message }) => message.kind?.startsWith('task.'))?.node : null);
    const after = acknowledgement?.node || user;
    const description = card.querySelector('.task-description');
    if (description) description.hidden = related.some(({ message }) => message.role !== 'user' &&
      message.kind === feedbackKind && currentRevision(message));
    for (const history of card.querySelectorAll('.task-history')) {
      history.hidden = related.some(({ message }) => message.role === history.dataset.historyRole && message.text === history.dataset.historyText);
    }
    card.dataset.anchorMessage = (before || after)?.dataset.messageId || '';
    if (before) {
      if (card.nextSibling !== before) moveTimelineCard(container, card, before);
    } else if (after) {
      if (after.nextSibling !== card) moveTimelineCard(container, card, after.nextSibling);
    } else if (lastMessage && (lastMessage.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_PRECEDING)) {
      // Only genuinely unreferenced cards use the tail. Imported legacy tasks
      // with source events stay beside their own history, not below new input.
      moveTimelineCard(container, card);
    }
  }
  if (position.follow) scrollChat(true);
  else if (position.anchor?.isConnected) {
    $('chat').scrollTop += position.anchor.getBoundingClientRect().top - $('chat').getBoundingClientRect().top - position.offset;
  } else $('chat').scrollTop = position.top;
}
function upsertMessage(record) {
  if (!record || !Number.isSafeInteger(Number(record.id)) || Number(record.id) <= 0 || !['user', 'assistant', 'system'].includes(record.role)) return;
  record = { ...record, id: Number(record.id) };
  const previous = state.messages.get(record.id);
  if (previous && Number(previous.version || 0) >= Number(record.version || 0)) { reconcileTimeline(); return; }
  const position = captureTimelineScroll();
  state.messages.set(record.id, record);
  let wrapper = state.messageNodes.get(record.id);
  if (!wrapper) {
    wrapper = element('article', 'conversation-message'); wrapper.dataset.messageId = record.id; state.messageNodes.set(record.id, wrapper);
    // Replayed gaps can arrive after a newer snapshot; stable numeric IDs preserve chronology.
    const successor = [...$('conversationMessages').querySelectorAll(':scope > [data-message-id]')].find(node => Number(node.dataset.messageId) > Number(record.id));
    $('conversationMessages').insertBefore(wrapper, successor || null);
  }
  wrapper.replaceChildren(); wrapper.dataset.version = record.version ?? 0;
  if (record.task_id) wrapper.dataset.messageTask = record.task_id; else delete wrapper.dataset.messageTask;
  wrapper.classList.toggle('superseded', Boolean(record.superseded));
  const meta = element('div', 'message-meta', `${record.role === 'user' ? '你' : '助理'} · ${record.source === 'mobile' || record.device_id && record.device_id !== state.device?.id ? '手机 / 其他设备' : record.device_id ? '电脑' : '共享会话'}${record.source === 'voice' || record.kind === 'transcript' ? ' · 语音' : ''}${record.superseded ? ' · 已失效' : ''}`);
  const row = element('div', record.role === 'user' ? 'user' : 'assistant');
  if (record.role !== 'user') row.append(element('span', 'avatar', 'p'));
  row.append(element('div', 'bubble', record.text || '')); wrapper.append(meta, row);
  $('welcome').hidden = true; reconcileTimeline(position);
}
async function refreshMessages() {
  if (state.sessionInvalid) return;
  if (state.messagesPromise) return state.messagesPromise;
  state.messagesPromise = (async () => {
    const first = await api('/api/conversation/messages');
    let page = first; const records = [...(first.messages || [])], seen = new Set();
    if (!state.messagesHydrated) {
      while (page.has_more && page.next_before != null && !seen.has(page.next_before)) {
        seen.add(page.next_before);
        page = await api(`/api/conversation/messages?before=${encodeURIComponent(page.next_before)}`);
        records.unshift(...(page.messages || []));
      }
    }
    if (state.sessionInvalid) return;
    for (const record of records) upsertMessage(record);
    for (const event of first.process_events || []) renderSharedProcess(event);
    // A latest-page snapshot cannot acknowledge unseen SSE events or older edits.
    if (!state.messagesHydrated) state.messageCursor = Math.max(state.messageCursor, Number(first.cursor) || 0);
    state.messagesHydrated = true;
    $('syncStatus').textContent = '对话已同步';
  })();
  try { await state.messagesPromise; } finally { state.messagesPromise = null; }
}
function startConversationStream() {
  if (state.sessionInvalid) return;
  clearTimeout(state.conversationRetry); state.conversationStream?.close();
  const stream = new EventSource(`/api/conversation/stream?after=${encodeURIComponent(state.messageCursor)}`);
  state.conversationStream = stream;
  stream.onopen = () => { if (stream === state.conversationStream && !state.sessionInvalid) $('syncStatus').textContent = '对话实时同步'; };
  stream.addEventListener('revoked', () => { if (stream === state.conversationStream) invalidateDevice(); });
  stream.onmessage = event => {
    if (state.sessionInvalid || stream !== state.conversationStream) return;
    try {
      const data = JSON.parse(event.data); upsertMessage(data.message);
      renderSharedProcess(data);
      state.messageCursor = Math.max(state.messageCursor, Number(data.cursor ?? data.seq ?? data.id) || 0);
    }
    catch { $('syncStatus').textContent = '同步数据异常，等待重试'; }
  };
  stream.onerror = () => {
    if (state.sessionInvalid || stream !== state.conversationStream) return;
    stream.close(); $('syncStatus').textContent = '连接中断，正在重连';
    state.conversationRetry = setTimeout(async () => { await refreshMessages().catch(() => {}); startConversationStream(); }, 1500);
  };
}
function openDrawer(id) { if (!$(id).open) { $(id).showModal(); if (id === 'plansDialog') startPlanRefresh(); } }
function showOutput() {
  const task = state.tasks.find(t => t.id === state.selected);
  const value = state.tab === 'schema' ? (state.selectedToolSchema || state.catalog?.output_schema) : task?.output;
  $('emptyState').hidden = !!value; $('jsonOutput').hidden = !value;
  $('jsonCode').textContent = value ? JSON.stringify(value, null, 2) : '';
  $('copyButton').disabled = !value;
  $('previewState').textContent = state.tab === 'schema' ? '参数与结果协议' : task ? labels[task.status] : '等待生成';
  $('previewSource').textContent = task?.source === 'fixture' ? '预置数据 · 未调用模型' : '示例 Schema · 非真实车型';
  $('validationNote').textContent = task?.status === 'generated' ? 'Schema 校验通过 · 已模拟下发成功' : task?.status === 'completed' ? '模拟查询已完成 · 未执行车控' : '全部结果来自模拟配置';
  if (state.tab === 'output' && task?.superseded_result_ids?.length) $('validationNote').textContent = '含已失效的旧预览，不计入当前任务结果。' + $('validationNote').textContent;
}
function selectTask(id) { state.selected = id; state.tab = 'output'; document.querySelectorAll('[data-tab]').forEach(b => b.classList.toggle('active', b.dataset.tab === state.tab)); showOutput(); openDrawer('previewDialog'); }
function duration(ms) { if (ms < 1) return '<1 ms'; return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms/1000).toFixed(2)} s`; }
function timer(ms, started, cls = '') { const n = element('span', cls, duration(ms ?? Math.max(0, Date.now()-started*1000))); if (ms == null) n.dataset.timerStart = started; return n; }
function updateTimers() { document.querySelectorAll('[data-timer-start]').forEach(n => { n.textContent = duration(Math.max(0, Date.now()-Number(n.dataset.timerStart)*1000)); }); }
function replyKey(task) { return `${task.id}:${task.revision}`; }
function setReply(task = null, { manual = true, focus = true } = {}) {
  if (task) clearTaskContext();
  if (task && manual) { state.forceNewTask = false; state.expiredBinding = null; }
  state.replyTask = task?.id || null; state.replyRevision = task?.revision ?? null; state.replyManual = Boolean(task && manual);
  state.replyAmbiguous = false; $('replyChoices').hidden = true; $('replyChoices').replaceChildren();
  $('replyContext').hidden = !task;
  $('replyLabel').textContent = task ? `当前任务：${task.goal}${task.question ? ' · '+task.question : ''}` : '';
  $('message').placeholder = task?.status === 'awaiting_input' ? '回答当前任务的问题，或描述新需求…' : '描述你的车辆需求…';
  if (task && focus) $('message').focus();
}
function startNewTask() {
  clearTaskContext();
  state.forceNewTask = true; state.expiredBinding = null;
  for (const task of state.tasks.filter(t => t.status === 'awaiting_input')) state.ignoredReplies.add(replyKey(task));
  setReply(null, { focus: false }); $('message').placeholder = '输入新的车辆需求…';
}
function syncReplyContext() {
  const bound = state.reviseTask || state.followupTask || state.replyTask;
  if (bound && state.tasks.find(t => t.id === bound)?.status === 'expired') {
    clearTaskContext(); setReply(null, { focus: false });
    state.expiredBinding = bound;
  }
  if (state.forceNewTask || state.reviseTask || state.reviseAmbiguous || state.followupTask || state.replyAmbiguous || state.replyManual) return;
  if (state.dialog?.question && (!state.dialog.expires || state.dialog.expires * 1000 > Date.now())) {
    setReply(null, { focus: false }); $('replyContext').hidden = false; $('replyLabel').textContent = state.dialog.question;
    $('message').placeholder = '补充这次确认所需的信息…'; return;
  }
  const focused = state.tasks.find(t => t.id === state.focusTask && t.status === 'awaiting_input');
  // Only the server's user-interaction focus may supply an automatic context.
  setReply(focused || null, { manual: false, focus: false });
}
function chooseInputTask(candidates, prompt) {
  clearTaskContext(); setReply(null, { focus: false }); state.replyAmbiguous = true;
  $('replyContext').hidden = false; $('replyChoices').hidden = false;
  $('replyLabel').textContent = prompt || '请选择对应的任务';
  for (const candidate of candidates) {
    const id = candidate.task_id || candidate.id;
    if (!id) continue;
    const task = { ...state.tasks.find(t => t.id === id), ...candidate, id };
    task.revision = candidate.task_revision ?? task.revision;
    const button = element('button', 'reply-choice', task.goal || candidate.label || candidate.message || '历史任务');
    button.type = 'button'; button.onclick = () => { if (!state.busy) setReply(task); };
    $('replyChoices').append(button);
  }
}
function clearTaskContext() {
  state.reviseTask = null; state.reviseRevision = null; state.reviseAmbiguous = false; state.pendingChange = null;
  state.followupTask = null; state.followupRevision = null; state.pendingFollowup = null; state.parentTask = null; state.pendingNewTask = null;
  $('reviseContext').hidden = true; $('reviseChoices').replaceChildren();
  $('sendButton').textContent = '发送 ↗';
}
function setFollowup(task) {
  clearTaskContext(); setReply(null, { focus: false }); state.forceNewTask = false; state.expiredBinding = null;
  state.followupTask = task.id; state.followupRevision = task.revision;
  $('reviseContext').hidden = false;
  $('reviseLabel').textContent = `完成后追加：${task.goal}`;
  $('message').placeholder = '输入这个任务完成后要做的事…';
  $('sendButton').textContent = '追加 ↗'; $('message').focus();
}
function newTaskFrom(task) {
  startNewTask(); state.parentTask = task.id;
  $('reviseContext').hidden = false;
  $('reviseLabel').textContent = `新任务，接续：${task.goal}`;
  $('message').placeholder = '填写完整的新需求…';
  if (!$('message').value.trim()) $('message').value = task.goal;
  $('message').focus();
}
function setRevise(task, { focus = true } = {}) {
  clearTaskContext(); setReply(null, { focus: false }); state.forceNewTask = false; state.expiredBinding = null;
  state.reviseTask = task.id; state.reviseRevision = task.revision;
  $('reviseContext').hidden = false;
  $('reviseLabel').textContent = `改口此任务：${task.goal}`;
  $('message').placeholder = '输入新的要求，修改这个正在处理的任务…';
  $('sendButton').textContent = '提交改口 ↗';
  if (focus) $('message').focus();
}
function isSimpleRevision(text) {
  // Only short, object-free corrections qualify. Full requests stay new requests.
  return /^(?:改成|改为|换成)\s*\d+(?:\.\d+)?\s*(?:度|℃|°C|%|％|档|挡)(?:吧|。|！|!)?$/i.test(text);
}
function renderProcess(task, wasOpen, expanded = new Set()) {
  const process = task.process || { steps: [], timing: {} }, details = element('details', 'process navigation-trace');
  details.open = wasOpen ?? activeStatuses.includes(task.status);
  const summary = element('summary'), active = process.steps.find(s => s.live);
  summary.append(element('span', 'process-title', active ? `处理过程 · ${active.title}` : `处理过程 · ${process.steps.length} 步`));
  summary.append(timer(process.timing.total_ms, task.created, 'total-time')); details.append(summary);
  const body = element('div', 'process-body');
  for (const step of process.steps) {
    const row = element('div', `process-step ${step.status}`); row.dataset.stepId = step.id;
    row.append(element('span', 'step-icon', step.live ? '◌' : step.status === 'error' ? '×' : step.duration_kind === 'boundary_wait' ? '·' : '✓'));
    const main = element('div', 'step-main'); main.append(element('strong', '', step.title));
    const type = { measured_local: '本地实测', measured_tool: '工具实测', boundary_wait: '模型 / 网络 / 调度区间', stage_interval: '阶段区间 · 含模型等待', human_wait: '用户等待', external_wait: '等待时间 / 外部结果', timer_wait: '等待时间', instant: '事件' }[step.duration_kind];
    if (step.observation) {
      for (const [key,label] of [['action','操作'],['input','输入'],['output','结果']]) {
        if (!step.observation[key]) continue;
        const line = element('p', 'step-fact'); line.append(element('b', '', label+'：'), document.createTextNode(step.observation[key])); main.append(line);
      }
      if (step.observation.data) {
        const data = element('details', 'step-data'); data.dataset.stepDetail = step.id; data.open = expanded.has(step.id);
        data.append(element('summary', '', '查看本步数据'), element('pre', '', step.observation.data)); main.append(data);
      }
      main.append(element('small','step-time-kind',type));
    } else main.append(element('p', '', [step.detail, type].filter(Boolean).join(' · ')));
    row.append(main);
    row.append(step.duration_kind === 'instant' ? element('span', 'step-duration', '—') : timer(step.duration_ms, step.started_at, 'step-duration')); body.append(row);
  }
  body.append(element('p', 'timing-notice', '执行摘要来自实际工具事件。等待区间包含模型、网络与调度；不代表纯模型推理耗时。'));
  details.append(body); return details;
}
function renderTasks() {
  $('taskCount').textContent = state.tasks.length;
  const container = $('conversationMessages'), position = captureTimelineScroll();
  if (state.tasks.length) $('welcome').hidden = true;
  for (const task of [...state.tasks].reverse()) {
    let row = container.querySelector(`[data-task-id="${task.id}"]`);
    const fingerprint = JSON.stringify(task) + Boolean(state.voice?.connected) + (task.id === state.focusTask);
    if (row?.dataset.fingerprint === fingerprint) continue;
    const wasOpen = row?.querySelector('.process')?.open;
    const expanded = new Set([...(row?.querySelectorAll('[data-step-detail][open]') || [])].map(n => n.dataset.stepDetail));
    const detailScroll = [...(row?.querySelectorAll('[data-step-detail] pre') || [])].map(node => ({
      id: node.parentElement.dataset.stepDetail, top: node.scrollTop, left: node.scrollLeft,
    }));
    if (!row) { row = element('article', 'task'); row.dataset.taskId = task.id; row.dataset.voice = String(Boolean(state.voice?.connected)); container.append(row); }
    row.dataset.fingerprint = fingerprint; row.replaceChildren();
    // Canonical messages own chat bubbles; this card owns execution details.
    const assistant = element('div', 'assistant'), content = element('div', 'assistant-content');
    assistant.append(element('span', 'avatar', 'p'), content); row.append(assistant);
    const meta = element('div', 'agent-meta'); meta.append(element('span', '', 'Personal Agent'), element('small', '', task.source === 'fixture' ? '预置示例' : 'HERMES'), element('span', `task-status ${task.status}`, labels[task.status] || task.status)); content.append(meta);
    content.append(element('p', 'task-context', `任务目标：${task.goal}`));
    if (task.latest_change) {
      const change = task.latest_change;
      content.append(element('p', 'task-change', `改口${change.text ? `：${change.text}` : ''} · ${changeLabels[change.status] || '处理中'}`));
    }
    if (task.followups?.length) {
      const queued = element('ul', 'task-followups');
      const statuses = { queued: '等待原任务完成', pending: '等待原任务完成', started: '已开始', launched: '已开始', completed: '已完成', cancelled: '已取消', failed: '未完成' };
      for (const item of task.followups) queued.append(element('li', '', `完成后追加：${item.text} · ${statuses[item.status] || '已收到'}`));
      content.append(queued);
    }
    for (const item of task.history || []) {
      for (const [field, role, label] of [['question', 'assistant', '追问'], ['answer', 'user', '你的补充']]) {
        if (!item[field]) continue;
        const history = element('p', 'task-context task-history', `${label}：${item[field]}`);
        history.dataset.historyRole = role; history.dataset.historyText = item[field]; content.append(history);
      }
    }
    content.append(renderProcess(task, wasOpen, expanded));
    const detail = task.question || task.summary;
    if (detail && (task.question || !state.voice?.connected || row.dataset.voice !== 'true')) content.append(element('p', 'task-description', detail));
    if (task.results?.length) {
      const chips = element('div', 'tool-chips');
      const superseded = new Set(task.superseded_result_ids || []);
      for (const r of task.results) {
        const old = superseded.has(r.result_id);
        const chip = element('span', `tool-chip ${old ? 'superseded' : r.status}`, `${old ? '—' : r.status === 'error' ? '×' : '✓'} ${r.tool_id} · ${old ? '旧预览 · 已失效' : r.status === 'prepared' ? '参数已校验' : r.status === 'returned_mock' ? '模拟查询' : r.error?.code || '失败'}`);
        if (old) chip.title = '已被后续改口替代，不计入当前任务结果';
        chips.append(chip);
      }
      content.append(chips);
    }
    if (task.id === state.focusTask && activeStatuses.includes(task.status)) meta.append(element('small', 'focus-badge', '当前任务'));
    const actions = element('div', 'task-actions');
    if (['running', 'waiting'].includes(task.status)) { const focus = element('button', 'text-button', '选择此任务'); focus.onclick = () => { if (!state.busy) setReply(task); }; actions.append(focus); }
    if (revisableStatuses.includes(task.status)) { const revise = element('button', 'text-button revise-button', '改口'); revise.type = 'button'; revise.onclick = () => { if (!state.busy) setRevise(task); }; actions.append(revise); }
    if (task.status === 'awaiting_input') { const reply = element('button', 'text-button', '补充信息 →'); reply.onclick = () => setReply(task); actions.append(reply); }
    if (task.output) { const view = element('button', 'text-button', '查看结构 ↗'); view.onclick = () => selectTask(task.id); actions.append(view); }
    if (activeStatuses.includes(task.status)) {
      const followup = element('button', 'text-button', '完成后追加'); followup.type = 'button'; followup.onclick = () => { if (!state.busy) setFollowup(task); }; actions.append(followup);
      const cancel = element('button', 'text-button', '取消任务'); cancel.onclick = async () => { if (state.busy) return; try { await api(`/api/tasks/${task.id}/cancel`, {}); if (state.replyTask === task.id) setReply(); await poll(); } catch (e) { toast(e.message); } }; actions.append(cancel);
    } else if (terminalStatuses.includes(task.status)) {
      const next = element('button', 'text-button', '基于此新建任务'); next.type = 'button'; next.onclick = () => { if (!state.busy) newTaskFrom(task); }; actions.append(next);
    }
    content.append(actions);
    const preload = task.navigation?.filter(n => ['catalog.preloaded', 'catalog.preload_skipped'].includes(n.kind)).at(-1);
    if (preload) content.append(element('div', 'task-context', `${preload.kind === 'catalog.preloaded' ? '候选已预加载' : '按需发现工具'} · ${state.catalog?.profiles.find(p => p.id === task.profile_id)?.title || task.profile_id}`));
    for (const saved of detailScroll) {
      const node = row.querySelector(`[data-step-detail="${CSS.escape(saved.id)}"] pre`);
      if (node) { node.scrollTop = saved.top; node.scrollLeft = saved.left; }
    }
  }
  syncReplyContext();
  updateTimers(); reconcileTimeline(position);
}
async function poll() {
  if (state.pollPromise) return state.pollPromise;
  state.pollPromise = (async () => {
    const result = await api('/api/tasks'); state.tasks = result.tasks; state.focusTask = result.focus_task_id ?? null; state.dialog = result.dialog || null;
    if (!state.selected && state.tasks.length) state.selected = state.tasks[0].id;
    renderTasks(); showOutput(); state.initialized = true;
  })();
  try { await state.pollPromise; } finally { state.pollPromise = null; }
}
async function pollPlanEvents() {
  if (state.planEventsPromise) return state.planEventsPromise;
  state.planEventsPromise = (async () => {
    const previous = state.planEventsCursor || 0;
    const { events } = await api(`/api/plan-events?after=${previous}`);
    for (const event of [...events].sort((a, b) => a.id - b.id)) {
      if (event.id <= (state.planEventsCursor || 0)) continue;
      state.planEventsCursor = event.id;
      if (!state.planEventsInitialized || !event.notify || !['plan.missed', 'plan.skipped'].includes(event.kind)) continue;
      refreshMessages().catch(() => {});
    }
    state.planEventsInitialized = true;
  })();
  try { await state.planEventsPromise; } finally { state.planEventsPromise = null; }
}
function startEvents(cursor) {
  state.events?.close(); state.events = new EventSource(`/api/events/stream?after=${cursor}`);
  let refresh;
  state.events.onmessage = event => {
    const data = JSON.parse(event.data); state.cursor = Math.max(state.cursor, data.id);
    clearTimeout(refresh); refresh = setTimeout(async () => { try { if (state.pollPromise) await state.pollPromise; await poll(); } catch {} }, 40);
  };
}
async function loadExample(name) {
  try { const task = await api(`/api/examples/${name}`, {}); await poll(); selectTask(task.id); }
  catch (e) { toast(e.message); }
}
async function renderCatalog(path = state.catalogPath, offset = 0) {
  state.catalogPath = path; state.catalogOffset = offset;
  const node = await api(`/api/catalog/tree?path=${encodeURIComponent(path.join('.'))}&offset=${offset}`);
  $('catalog').replaceChildren(); $('catalogBreadcrumbs').replaceChildren(); $('catalogPagination').replaceChildren();
  const root = element('button', 'text-button', '全部能力'); root.onclick = () => renderCatalog([]).catch(e => toast(e.message)); $('catalogBreadcrumbs').append(root);
  path.forEach((name, index) => { const button = element('button', 'text-button', ` / ${name}`); button.onclick = () => renderCatalog(path.slice(0, index + 1)).catch(e => toast(e.message)); $('catalogBreadcrumbs').append(button); });
  for (const item of node.items) {
    const card = element('div', 'catalog-tool'); card.append(element('strong', '', `${item.node_type === 'directory' ? '▸ ' : ''}${item.title}`));
    card.append(element('code', '', item.id || item.path.join('.')));
    card.append(element('p', 'catalog-description', item.description));
    if (item.node_type === 'directory') {
      const open = element('button', 'text-button', '浏览下级 →'); open.onclick = () => renderCatalog(item.path).catch(e => toast(e.message)); card.append(open);
    } else {
      const use = element('button', 'text-button', '填入指令'); use.onclick = async () => { try { const d = await api(`/api/catalog/tools/${encodeURIComponent(item.id)}`); startNewTask(); $('message').value = d.tools[0].examples[0].instruction; $('catalogDialog').close(); $('message').focus(); } catch (e) { toast(e.message); } };
      const example = element('button', 'text-button', '预置验证'); example.onclick = () => loadExample(item.id);
      const schema = element('button', 'text-button', '查看 Schema'); schema.onclick = async () => { try { const d = await api(`/api/catalog/tools/${encodeURIComponent(item.id)}`); state.selectedToolSchema = d.tools[0]; state.tab = 'schema'; document.querySelectorAll('[data-tab]').forEach(b => b.classList.toggle('active', b.dataset.tab === 'schema')); showOutput(); openDrawer('previewDialog'); } catch (e) { toast(e.message); } };
      card.append(use, example, schema);
    }
    $('catalog').append(card);
  }
  if (!node.items.length) $('catalog').append(element('div', 'task-empty', '此目录没有可用工具'));
  if (offset) { const back = element('button', 'text-button', '上一页'); back.onclick = () => renderCatalog(path, Math.max(0, offset - 30)); $('catalogPagination').append(back); }
  if (node.next_offset !== null) { const next = element('button', 'text-button', '下一页'); next.onclick = () => renderCatalog(path, node.next_offset); $('catalogPagination').append(next); }
}
async function refreshCatalog() {
  state.catalog = await api('/api/catalog');
  const profile = await api('/api/session/profile');
  $('profileSelect').replaceChildren();
  for (const p of state.catalog.profiles) { const option = element('option', '', p.title); option.value = p.id; $('profileSelect').append(option); }
  $('profileSelect').value = profile.profile_id;
  $('scenarioSelect').replaceChildren();
  for (const s of state.catalog.scenarios) { const option = element('option', '', `${s.id} · ${s.instruction}`); option.value = s.id; $('scenarioSelect').append(option); }
  const counts = state.catalog.counts;
  $('catalogCount').textContent = `${counts.control + counts.manual + counts.status} 项`;
  $('catalogSummary').textContent = `车控 ${counts.control} · 车书 ${counts.manual} · 车况 ${counts.status}`;
  $('suggestions').replaceChildren();
  for (const s of state.catalog.scenarios.filter(s => s.kind === 'control').slice(0, 4)) {
    const chip = element('button', 'chip', s.instruction); chip.onclick = () => { startNewTask(); $('message').value = s.instruction; $('message').focus(); }; $('suggestions').append(chip);
  }
  state.selectedToolSchema = null;
  await renderCatalog([]); showOutput();
}
$('composer').onsubmit = async event => {
  event.preventDefault();
  const text = $('message').value.trim(); if (!text || state.busy || state.sessionInvalid) return;
  const draft = $('message').value;
  const manual = state.replyManual ? { id: state.replyTask, revision: state.replyRevision } : null;
  state.busy = true; $('sendButton').disabled = true;
  try {
    if (state.pollPromise) await state.pollPromise;
    await poll();
    if (state.expiredBinding) { state.expiredBinding = null; toast('原任务已过期，草稿已保留；请描述新需求或重新选择任务'); return; }
    if (state.replyAmbiguous || state.reviseAmbiguous) { toast('请选择对应的任务，或切换新任务'); return; }
    if (state.followupTask) {
      const id = state.followupTask, revision = state.followupRevision;
      const original = state.tasks.find(t => t.id === id);
      if (!original || !activeStatuses.includes(original.status) || original.revision !== revision) {
        toast('原任务状态已变化；草稿已保留，请重新选择“完成后追加”或“基于此新建任务”'); return;
      }
      if (!state.pendingFollowup || state.pendingFollowup.id !== id || state.pendingFollowup.revision !== revision || state.pendingFollowup.text !== text) {
        state.pendingFollowup = { id, revision, text, event_id: crypto.randomUUID() };
      }
      const result = await api(`/api/tasks/${id}/followup`, { text, task_revision: revision, event_id: state.pendingFollowup.event_id });
      if ((result.status !== 'queued' && result.queued !== true) || result.task_id !== id || result.event_id !== state.pendingFollowup.event_id) {
        throw new Error('尚未确认追加已排队，草稿已保留，请重试');
      }
      clearTaskContext(); renderTasks(); showOutput();
      await poll().catch(() => {});
      toast('已追加，当前任务完成后处理');
    } else if (state.reviseTask) {
      const id = state.reviseTask, revision = state.reviseRevision;
      const original = state.tasks.find(t => t.id === id);
      if (!original || !revisableStatuses.includes(original.status) || original.revision !== revision) {
        toast('原任务状态或版本已变化；草稿已保留，请重新选择“改口”、补充信息或新任务'); return;
      }
      // Reuse the change ID after a network failure so retrying cannot apply twice.
      if (!state.pendingChange || state.pendingChange.id !== id || state.pendingChange.revision !== revision || state.pendingChange.text !== text) {
        state.pendingChange = { id, revision, text, change_id: crypto.randomUUID() };
      }
      const task = await api(`/api/tasks/${id}/revise`, { text, task_revision: revision, change_id: state.pendingChange.change_id });
      state.tasks = state.tasks.map(t => t.id === task.id ? task : t); state.selected = task.id;
      if (['rejected', 'unconfirmed', 'failed'].includes(task.latest_change?.status)) {
        renderTasks(); showOutput(); toast(task.summary || `改口${changeLabels[task.latest_change.status]}，草稿已保留`); return;
      }
      clearTaskContext(); renderTasks(); showOutput();
    } else if (!state.forceNewTask && !state.parentTask) {
      if (manual) {
        const current = state.tasks.find(t => t.id === manual.id);
        if (current && (current.revision !== manual.revision || current.status === 'expired')) {
          toast('所选任务已变化，草稿已保留，请重新选择'); return;
        }
      }
      const target = manual ? { task_id: manual.id, task_revision: manual.revision } : {};
      const signature = JSON.stringify({ text, ...target });
      if (state.pendingInput?.signature !== signature) state.pendingInput = { signature, event_id: crypto.randomUUID() };
      const result = await api('/api/conversation/input', { text, event_id: state.pendingInput.event_id, ...target });
      if (result.task?.id) { state.tasks = [result.task, ...state.tasks.filter(t => t.id !== result.task.id)]; state.selected = result.task.id; }
      if (result.candidates?.length) {
        chooseInputTask(result.candidates, result.message); renderTasks(); showOutput(); return;
      }
      if (['error', 'failed', 'rejected', 'conflict', 'expired'].includes(result.status) || ['rejected', 'unconfirmed', 'failed'].includes(result.task?.latest_change?.status)) {
        renderTasks(); showOutput(); toast(result.message || '请求未生效，草稿已保留'); return;
      }
      state.pendingInput = null; setReply(null, { focus: false });
      renderTasks(); showOutput();
      await poll().catch(() => {});
      if (result.kind === 'plans' || result.plans?.length) { openDrawer('plansDialog'); await loadPlans().catch(plansError); }
      else if (result.plan && $('plansDialog').open) await loadPlans().catch(plansError);
    } else {
      if (state.parentTask && isSimpleRevision(text)) { toast('请填写完整的新需求，例如“把空调设为21度”；草稿已保留'); return; }
      if (!state.pendingNewTask || state.pendingNewTask.goal !== text || state.pendingNewTask.parent !== state.parentTask) {
        state.pendingNewTask = { goal: text, parent: state.parentTask, request_key: crypto.randomUUID() };
      }
      const task = await api('/api/tasks', { goal: text, request_key: state.pendingNewTask.request_key, ...(state.parentTask ? { parent_task_id: state.parentTask } : {}) });
      state.selected = task.id; state.tasks = [task, ...state.tasks.filter(t => t.id !== task.id)];
      clearTaskContext(); renderTasks(); showOutput();
      await poll().catch(() => toast('任务已创建，列表稍后更新'));
    }
    await refreshMessages().catch(() => { $('syncStatus').textContent = '消息已发送，等待同步'; });
    state.forceNewTask = false;
    if ($('message').value === draft) $('message').value = '';
    syncReplyContext(); scrollChat(true);
  } catch (error) { toast(error.message); }
  finally { state.busy = false; $('sendButton').disabled = Boolean(state.sessionInvalid); }
};
$('clearReply').onclick = $('clearRevise').onclick = () => { if (!state.busy) { startNewTask(); $('message').focus(); } };
$('openCatalog').onclick = $('openScenarios').onclick = () => openDrawer('catalogDialog');
$('openPreview').onclick = () => { showOutput(); openDrawer('previewDialog'); };
$('focusChat').onclick = () => { $('message').focus(); scrollChat(true); };
document.querySelectorAll('[data-close]').forEach(b => b.onclick = () => $(b.dataset.close).close());
$('message').onkeydown = event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); $('composer').requestSubmit(); } };
document.querySelectorAll('[data-tab]').forEach(button => button.onclick = () => { state.tab = button.dataset.tab; document.querySelectorAll('[data-tab]').forEach(b => b.classList.toggle('active', b === button)); showOutput(); });
$('copyButton').onclick = async () => { try { await navigator.clipboard.writeText($('jsonCode').textContent); toast('已复制调用结构'); } catch { toast('浏览器未允许复制，可在结构区手动选择文本'); } };
$('loadExample').onclick = () => loadExample('control.suspension.set_height');
$('profileSelect').onchange = async () => { try { await api('/api/session/profile', { profile_id: $('profileSelect').value }); toast('模拟配置已切换，仅影响新任务'); } catch (e) { toast(e.message); } };
$('useScenario').onclick = async () => { try { const s = state.catalog.scenarios.find(x => x.id === $('scenarioSelect').value); if (!s) return; await api('/api/session/profile', { profile_id: s.profile_id }); $('profileSelect').value = s.profile_id; startNewTask(); $('message').value = s.instruction; $('catalogDialog').close(); $('message').focus(); } catch (e) { toast(e.message); } };
$('reloadCatalog').onclick = async () => { try { await api('/api/catalog/reload', {}); await refreshCatalog(); toast('配置已重新加载，已有任务继续使用原快照'); } catch (e) { toast(e.message); } };
$('importCatalog').onclick = () => $('importFile').click();
$('importFile').onchange = async () => {
  const file = $('importFile').files[0]; if (!file) return;
  try {
    if (file.size > 4 * 1024 * 1024) throw new Error('配置文件不能超过 4 MiB');
    const payload = JSON.parse(await file.text());
    state.pendingImport = await api('/api/catalog/import/preview', { payload, mode: $('importMode').value });
    $('importDiff').textContent = JSON.stringify({ counts: state.pendingImport.counts, changes: state.pendingImport.diff }, null, 2);
    $('importPreview').hidden = false;
  } catch (e) { toast(`导入未生效：${e.message}`); }
  finally { $('importFile').value = ''; }
};
$('cancelImport').onclick = () => { state.pendingImport = null; $('importPreview').hidden = true; };
$('applyImport').onclick = async () => {
  if (!state.pendingImport) return;
  $('applyImport').disabled = true;
  try { await api('/api/catalog/import/apply', { preview_id: state.pendingImport.preview_id, base_revision: state.pendingImport.base_revision }); $('importPreview').hidden = true; state.pendingImport = null; await refreshCatalog(); toast('配置已应用；新任务使用新目录'); }
  catch (e) { toast(e.message); }
  finally { $('applyImport').disabled = false; }
};

const weekdayNames = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
const planLabels = { active: '进行中', paused: '已暂停', cancelled: '已取消', completed: '已完成', running: '处理中', awaiting_input: '等待补充', waiting: '等待时间 / 外部结果', expired: '已过期', generated: '模拟成功', pending: '待执行', scheduled: '待执行', started: '已开始', claimed: '准备执行', created: '已创建任务', skipped: '已跳过', missed: '错过执行', failed: '未完成', interrupted: '已中断' };
function planTimeLabel(epoch, timezone = state.planTimezone) {
  if (epoch == null) return '未安排';
  try { return new Intl.DateTimeFormat('zh-CN', { timeZone: timezone, dateStyle: 'short', timeStyle: 'short', hour12: false }).format(new Date(epoch * 1000)); }
  catch { return '时间暂不可用'; }
}
function scheduleLabel(schedule) {
  if (!schedule) return '时间待确认';
  if (schedule.frequency === 'interval') return schedule.seconds % 60 === 0 ? `每 ${schedule.seconds / 60} 分钟` : `每 ${schedule.seconds} 秒`;
  const days = schedule.weekdays || [];
  const frequency = schedule.frequency === 'daily' ? '每天' : `每周 ${days.map(d => weekdayNames[d]).filter(Boolean).join('、')}`;
  return `${frequency} ${schedule.time || '时间待确认'}`;
}
function readPlanDraft() {
  const frequency = $('planFrequency').value;
  if (frequency === 'interval') {
    const seconds = $('planMinutes').value === '' && state.editingPlan?.intervalSeconds ? state.editingPlan.intervalSeconds : Number($('planMinutes').value) * 60;
    return { goal: $('planGoal').value.trim(), schedule: { frequency, seconds, timezone: state.editingPlan?.timezone || state.planTimezone } };
  }
  return { goal: $('planGoal').value.trim(), schedule: { frequency: frequency === 'daily' ? 'daily' : 'weekly', time: $('planTime').value,
    weekdays: frequency === 'daily' ? [] : frequency === 'weekdays' ? [0, 1, 2, 3, 4] : [...$('planWeekdays').querySelectorAll('input:checked')].map(n => Number(n.value)) } };
}
function syncPlanScheduleFields() {
  const interval = $('planFrequency').value === 'interval';
  $('planTimeField').hidden = interval; $('planIntervalField').hidden = !interval;
  $('planTime').disabled = interval || state.planBusy; $('planTime').required = !interval;
  $('planMinutes').disabled = !interval || state.planBusy; $('planMinutes').required = interval && !state.editingPlan?.intervalSeconds;
  $('planIntervalNote').hidden = !interval || !state.editingPlan?.intervalSeconds;
  $('planIntervalNote').textContent = state.editingPlan?.intervalSeconds ? `当前间隔为 ${state.editingPlan.intervalSeconds} 秒。留空保留；填写整数分钟可修改。` : '';
  $('planWeekdays').hidden = $('planFrequency').value !== 'weekly';
}
function previewPlan() {
  syncPlanScheduleFields();
  const draft = readPlanDraft();
  const intervalValid = Number.isSafeInteger(draft.schedule.seconds) && draft.schedule.seconds >= 60 && $('planMinutes').checkValidity();
  $('planPreview').textContent = draft.goal && (draft.schedule.frequency === 'interval' ? intervalValid : draft.schedule.time) ? `${draft.goal}\n${scheduleLabel(draft.schedule)} · ${state.editingPlan?.timezone || state.planTimezone || '等待读取当地时区'}` : '填写目标和具体时间或间隔后确认创建。';
}
function resetPlanForm() {
  state.editingPlan = null; state.pendingPlan = null; $('planForm').reset();
  $('planFormTitle').textContent = '新建计划'; $('savePlan').textContent = '创建计划';
  $('cancelPlanEdit').hidden = true; $('planError').hidden = true; previewPlan();
}
function editPlan(plan) {
  if (state.planBusy) return;
  state.editingPlan = { id: plan.id, revision: plan.revision, timezone: plan.timezone || plan.schedule.timezone,
    intervalSeconds: plan.schedule.frequency === 'interval' && plan.schedule.seconds % 60 !== 0 ? plan.schedule.seconds : null }; state.pendingPlan = null;
  $('planGoal').value = plan.goal; $('planTime').value = plan.schedule.time || '';
  $('planMinutes').value = plan.schedule.frequency === 'interval' && plan.schedule.seconds % 60 === 0 ? plan.schedule.seconds / 60 : '';
  $('planFrequency').value = plan.schedule.frequency;
  $('planWeekdays').querySelectorAll('input').forEach(n => { n.checked = (plan.schedule.weekdays || []).includes(Number(n.value)); });
  $('planFormTitle').textContent = '修改计划'; $('savePlan').textContent = '保存修改';
  $('cancelPlanEdit').hidden = false; $('planError').hidden = true; previewPlan(); $('planGoal').focus();
}
function setPlanBusy(busy) {
  state.planBusy = busy;
  $('planForm').querySelectorAll('button,input,textarea,select').forEach(n => { n.disabled = busy; });
  $('refreshPlans').disabled = busy;
  $('plansList').querySelectorAll('button,select').forEach(n => { n.disabled = busy || n.dataset.unavailable === 'true'; });
  syncPlanScheduleFields();
}
function canRefreshPlans() { return $('plansDialog').open && !state.editingPlan && !state.planBusy; }
function startPlanRefresh() {
  clearTimeout(state.planRefreshTimer);
  if (!$('plansDialog').open) return;
  state.planRefreshTimer = setTimeout(async () => {
    try { if (canRefreshPlans() && !state.plansLoading) await loadPlans({ automatic: true }); }
    catch (error) { if (canRefreshPlans()) plansError(error); }
    finally { startPlanRefresh(); }
  }, 2000);
}
async function loadPlans({ automatic = false } = {}) {
  if (automatic && (!canRefreshPlans() || state.plansLoading)) return;
  const generation = state.plansLoadGeneration = (state.plansLoadGeneration || 0) + 1;
  state.plansLoading = (state.plansLoading || 0) + 1;
  try {
    const result = await api('/api/plans');
    if (generation !== state.plansLoadGeneration || (automatic && !canRefreshPlans())) return;
    state.plans = result.plans; state.planTimezone = result.timezone;
    $('planTimezone').textContent = `当地时区：${state.planTimezone}`; $('plansError').hidden = true;
    renderPlans(); previewPlan();
  } finally { state.plansLoading--; }
}
function plansError(error) { $('plansError').textContent = error.message; $('plansError').hidden = false; }
async function planAction(plan, action, extra = {}) {
  if (state.planBusy) return;
  setPlanBusy(true);
  try {
    await api(`/api/plans/${encodeURIComponent(plan.id)}/action`, { action, revision: plan.revision, ...extra });
    await loadPlans(); await poll().catch(() => {});
  } catch (error) { plansError(error); }
  finally { setPlanBusy(false); }
}
function renderPlans() {
  const fingerprint = JSON.stringify([state.plans, state.planTimezone]);
  if ($('plansList').dataset.fingerprint === fingerprint) return;
  const expanded = new Map([...$('plansList').querySelectorAll('.plan-card')].map(card => [card.dataset.planId, {
    history: card.querySelector('.plan-history')?.open, cancel: card.querySelector('.plan-cancel')?.open }]));
  const scrollTop = $('plansDialog').scrollTop;
  $('plansList').dataset.fingerprint = fingerprint;
  $('plansList').replaceChildren();
  if (!state.plans.length) $('plansList').append(element('p', 'plan-note', '还没有周期计划。'));
  for (const plan of state.plans) {
    const card = element('article', 'plan-card'); card.dataset.planId = plan.id;
    card.append(element('h3', '', plan.goal), element('p', 'plan-note', `${planLabels[plan.status] || '状态待更新'} · ${scheduleLabel(plan.schedule)} · ${plan.timezone || state.planTimezone}\n下次：${planTimeLabel(plan.next_run_at, plan.timezone || state.planTimezone)}`));
    const actions = element('div', 'plan-actions');
    if (plan.status !== 'cancelled') {
      for (const [label, action] of [[plan.status === 'paused' ? '恢复' : '暂停', plan.status === 'paused' ? 'resume' : 'pause']]) {
        const button = element('button', 'text-button', label); button.type = 'button'; button.title = action === 'pause' ? '暂停后续安排，并停止当前这次执行' : '只安排之后的执行，不补跑暂停期间的次数'; button.onclick = () => planAction(plan, action); actions.append(button);
      }
      const edit = element('button', 'text-button', '修改'); edit.type = 'button'; edit.onclick = () => editPlan(plan); actions.append(edit);
      card.append(actions);
      const cancel = element('details', 'plan-cancel'); cancel.open = expanded.get(plan.id)?.cancel || false; cancel.append(element('summary', '', '取消…'));
      const occurrences = plan.runs || [];
      const active = occurrences.filter(o => activeStatuses.includes(o.task_status || o.status));
      const description = active.length > 1 ? '有多次未结束执行，请先在任务记录中明确需要取消的任务。' : active.length ? `只取消当前这次执行（${planTimeLabel(active[0].scheduled_at, plan.timezone || state.planTimezone)}），后续周期保留。` : `只跳过下一次（${planTimeLabel(plan.next_run_at, plan.timezone || state.planTimezone)}），后续周期保留。`;
      cancel.append(element('p', 'plan-note', description));
      const choices = element('div', 'plan-actions'), once = element('button', 'secondary', '只取消这一次'), all = element('button', 'secondary', '取消整个计划');
      once.type = all.type = 'button';
      once.dataset.unavailable = String(active.length > 1 || (!active.length && plan.next_run_at == null)); once.disabled = once.dataset.unavailable === 'true';
      once.onclick = () => planAction(plan, 'cancel_once');
      all.onclick = () => planAction(plan, 'cancel_plan'); choices.append(once, all); cancel.append(choices, element('p', 'plan-note', '取消整个计划将停止当前执行和后续安排，已有记录保留。')); card.append(cancel);
    }
    const history = element('details', 'plan-history'); history.open = expanded.get(plan.id)?.history || false; history.append(element('summary', '', `执行历史 · ${(plan.runs || []).length} 次`));
    const list = element('ol');
    for (const occurrence of plan.runs || []) {
      const row = element('li', '', `${planTimeLabel(occurrence.scheduled_at, plan.timezone || state.planTimezone)} · ${planLabels[occurrence.task_status || occurrence.status] || '状态待更新'}${occurrence.reason ? ' · '+({late_beyond_grace:'超过触发时间，未补跑', previous_occurrence_active:'上次执行尚未结束', cancel_once:'已取消这一次'}[occurrence.reason] || occurrence.reason) : ''}`);
      if (occurrence.task_id && state.tasks.some(t => t.id === occurrence.task_id)) {
        const view = element('button', 'text-button', '查看任务'); view.type = 'button'; view.onclick = () => { $('plansDialog').close(); document.querySelector(`[data-task-id="${CSS.escape(occurrence.task_id)}"]`)?.scrollIntoView({ block: 'center' }); }; row.append(document.createTextNode(' '), view);
      }
      list.append(row);
    }
    history.append(list); card.append(history); $('plansList').append(card);
  }
  setPlanBusy(state.planBusy);
  $('plansDialog').scrollTop = scrollTop;
}
$('plansDialog').addEventListener('close', () => clearTimeout(state.planRefreshTimer));
$('openPlans').onclick = () => { openDrawer('plansDialog'); loadPlans().catch(plansError); };
$('refreshPlans').onclick = () => loadPlans().catch(plansError);
$('planForm').oninput = previewPlan;
$('cancelPlanEdit').onclick = resetPlanForm;
$('planForm').onsubmit = async event => {
  event.preventDefault(); if (state.planBusy) return;
  const draft = readPlanDraft(); $('planError').hidden = true;
  const validTime = draft.schedule.frequency === 'interval' ? Number.isSafeInteger(draft.schedule.seconds) && draft.schedule.seconds >= 60 && $('planMinutes').checkValidity() : Boolean(draft.schedule.time);
  if (!state.planTimezone || !draft.goal || !validTime || (draft.schedule.frequency === 'weekly' && !draft.schedule.weekdays.length)) {
    $('planError').textContent = '请填写目标、具体时间和星期，或至少 1 分钟的整数间隔，并确认当地时区已加载。'; $('planError').hidden = false; return;
  }
  const editing = state.editingPlan;
  const signature = JSON.stringify({ ...draft, editing });
  if (state.pendingPlan?.signature !== signature) state.pendingPlan = { signature, request_key: crypto.randomUUID() };
  setPlanBusy(true);
  try {
    if (editing) await api(`/api/plans/${encodeURIComponent(editing.id)}/action`, { action: 'edit', revision: editing.revision, ...draft });
    else await api('/api/plans', { ...draft, request_key: state.pendingPlan.request_key });
    resetPlanForm(); toast(editing ? '计划已更新' : '计划已创建');
    await loadPlans().catch(plansError);
  } catch (error) { $('planError').textContent = error.message; $('planError').hidden = false; }
  finally { setPlanBusy(false); }
};

function renderSharedProcess(event) {
  if (event.kind === 'voice.process' && event.payload?.step) voiceStep(event.payload.step, event.seq ?? event.id ?? event.cursor, event.payload.device_name || '共享设备');
}
function voiceStep(step, sequence = null, deviceName = null) {
  const key = `${step.connection_id}:${step.id}`;
  if (sequence != null) {
    if (Number(sequence) <= (state.voiceStepCursors.get(key) ?? -1)) return;
    state.voiceStepCursors.set(key, Number(sequence));
  }
  let trace = document.querySelector(`[data-voice-connection="${step.connection_id}"]`);
  const follow = nearBottom();
  if (!trace) {
    trace = element('details', 'voice-process process'); trace.dataset.voiceConnection = step.connection_id; trace.open = true;
    trace.append(element('summary', '', 'S2S 过程 · 当前语音连接'), element('div', 'process-body'));
    trace.append(element('p','voice-timing-note','各阶段为实际事件的观测区间，可重叠；完整工具处理见对应任务。'));
    $('conversationMessages').append(trace); $('welcome').hidden = true;
  }
  if (deviceName && trace.dataset.ended !== 'true') trace.querySelector('summary').textContent = `S2S 过程 · ${deviceName} · 过程记录`;
  let row = [...trace.querySelectorAll('[data-voice-step]')].find(n => n.dataset.voiceStep === step.id);
  if (!row) { row=element('div','process-step'); row.dataset.voiceStep=step.id; trace.querySelector('.process-body').append(row); }
  const fingerprint = JSON.stringify(step); if (row.dataset.fingerprint === fingerprint) return; row.dataset.fingerprint = fingerprint;
  const body=trace.querySelector('.process-body'), followSteps=body.scrollHeight-body.scrollTop-body.clientHeight<60;
  row.className = `process-step ${step.status}`; row.replaceChildren();
  row.append(element('span','step-icon',step.status === 'running' ? '◌' : step.status === 'error' ? '×' : step.status === 'interrupted' ? '—' : '✓'));
  const main=element('div','step-main'); main.append(element('strong','',step.title),element('p','step-fact',step.detail || ''));
  if (step.task_id) {
    const link=element('button','text-button','查看对应任务过程 ↓');
    link.onclick=()=>{ const task=document.querySelector(`[data-task-id="${step.task_id}"]`); if(task) {task.querySelector('.process').open=true;task.scrollIntoView({block:'start'});} }; main.append(link);
  }
  row.append(main,step.duration_ms === 0 ? element('span','step-duration','—') : timer(step.duration_ms,step.started_at,'step-duration'));
  if (followSteps) body.scrollTop=body.scrollHeight;
  if (follow) scrollChat(true);
}

function endVoiceTrace(connectionId) {
  const trace = [...document.querySelectorAll('[data-voice-connection]')].find(node => node.dataset.voiceConnection === connectionId);
  if (!trace) return;
  trace.dataset.ended = 'true'; trace.querySelector('summary').textContent = 'S2S 过程 · 连接已结束';
  trace.querySelectorAll('.process-step.running').forEach(row => {
    row.classList.remove('running'); row.querySelector('.step-main').append(element('p', 'step-fact', '连接已结束，此步骤未确认完成'));
    row.querySelectorAll('[data-timer-start]').forEach(node => node.removeAttribute('data-timer-start'));
  });
}
function renderVoiceOwner() {
  const owner = state.voiceOwner, other = Boolean(owner && (owner.device_id || owner.id) !== state.device?.id);
  $('voiceOwnerLabel').textContent = owner ? (other ? `${owner.name || (owner.kind === 'mobile' ? '手机' : '其他设备')}正在语音通话` : '当前电脑正在语音通话') : '暂无设备使用语音';
  $('takeoverVoice').hidden = !other; $('takeoverVoice').disabled = Boolean(state.voiceStarting);
}
async function refreshVoiceOwner() {
  if (state.sessionInvalid) return;
  const result = await api('/api/voice-owner'); state.voiceOwner = result.owner ?? result.voice_owner ?? (result.device_id ? result : null); renderVoiceOwner();
}
async function startDesktopVoice({ takeover = false } = {}) {
  if (state.voiceStarting || state.sessionInvalid) return;
  if (!state.config?.voice_configured) { toast('先在本机 .env 配置百炼 API Key。密钥不会发送给浏览器。'); return; }
  state.voiceStarting = true; $('voiceButton').disabled = true; renderVoiceOwner();
  try {
    if (takeover) await api('/api/voice/takeover', {});
    else {
      await refreshVoiceOwner();
      if (state.voiceOwner && (state.voiceOwner.device_id || state.voiceOwner.id) !== state.device?.id) { toast('其他设备正在通话；如需切换，请点击“接管语音”'); return; }
    }
    if (document.hidden) return;
    const voice = new VoiceSession({
      onEvent(event) {
        if (event.type === 'voice.process') { voice.connectionId = event.step.connection_id; voiceStep(event.step); }
        if (event.type === 'error' || event.type === 'notice') toast(event.message);
        if (['user.transcript', 'response.done', 'voice.invalidate'].includes(event.type)) refreshMessages().catch(() => {});
      },
      onError(error) { toast(error.message); },
      onState(status, detail = {}) {
        const active = ['connecting', 'connected'].includes(status);
        $('voiceButton').setAttribute('aria-pressed', String(active));
        $('voiceButton').textContent = status === 'connecting' ? '◉ 连接中…' : status === 'connected' ? '◉ 结束语音' : '◉ 开始语音';
        $('channelBadge').textContent = status === 'connected' ? 'S2S + 文字' : '文字入口';
        $('voiceStatus').textContent = status === 'connected' ? '已连接' : status === 'connecting' ? '连接中' : status === 'busy' ? '其他设备通话中' : status === 'released' ? '语音已交接' : '已配置 · 未连接';
        if (!active) { endVoiceTrace(voice.connectionId); if (state.voice === voice) state.voice = null; }
        if (status === 'busy' || status === 'released') toast(detail.message || (status === 'busy' ? '语音已被其他设备占用' : '语音已交给其他设备'));
        if (status !== 'connecting') refreshVoiceOwner().catch(() => {});
        renderTasks();
      },
    });
    state.voice = voice; await voice.start();
  } catch (error) { state.voice?.stop(); toast(error.name === 'NotAllowedError' ? '麦克风权限未开启' : error.message); }
  finally { state.voiceStarting = false; $('voiceButton').disabled = Boolean(state.sessionInvalid); renderVoiceOwner(); }
}
$('voiceButton').onclick = () => { if (state.voice) state.voice.stop(); else startDesktopVoice(); };
$('takeoverVoice').onclick = () => startDesktopVoice({ takeover: true });
window.addEventListener('pagehide', () => { state.voice?.stop(); state.events?.close(); state.conversationStream?.close(); clearTimeout(state.conversationRetry); });
document.addEventListener('visibilitychange', () => {
  if (document.hidden) state.voice?.stop();
  else if (state.initialized && !state.sessionInvalid) { refreshMessages().then(startConversationStream).catch(() => {}); refreshVoiceOwner().catch(() => {}); }
});
async function refreshDevices() {
  const result = await api('/api/devices'); $('deviceList').replaceChildren();
  for (const device of result.devices || []) {
    const current = device.id === (result.current_device_id || state.device?.id) || device.current || device.is_current;
    const row = element('li', 'device-row');
    row.append(element('span', '', `${device.name || device.label || (device.kind === 'mobile' ? '手机' : '设备')}${current ? ' · 当前设备' : ''}${device.revoked_at || device.revoked ? ' · 已解除' : ''}`));
    if (!current && !device.revoked_at && !device.revoked) {
      const revoke = element('button', 'text-button', '解除连接'); revoke.type = 'button';
      revoke.onclick = async () => {
        revoke.disabled = true;
        try { const response = await fetch(`/api/devices/${encodeURIComponent(device.id)}`, { method: 'DELETE' }); if (!response.ok) { if (response.status === 401) invalidateDevice(); const result = await response.json(); throw new Error(result.detail || '解除失败'); } await refreshDevices(); await refreshVoiceOwner(); }
        catch (error) { toast(error.message); revoke.disabled = false; }
      }; row.append(revoke);
    }
    $('deviceList').append(row);
  }
}
function updatePairingExpiry() {
  if (!state.pairingExpires) return;
  const remaining = Math.max(0, Math.ceil((state.pairingExpires - Date.now()) / 1000));
  $('pairingExpiry').textContent = remaining ? `配对码剩余 ${Math.ceil(remaining / 60)} 分钟有效，仅可使用一次` : '配对码已过期，请重新生成';
  if (!remaining) { $('pairingCode').textContent = '已过期'; $('pairingLink').removeAttribute('href'); $('pairingLink').hidden = true; clearInterval(state.pairingTimer); }
}
$('openPhone').onclick = () => { openDrawer('phoneDialog'); refreshDevices().catch(error => toast(error.message)); refreshVoiceOwner().catch(error => toast(error.message)); };
$('refreshDevices').onclick = () => refreshDevices().catch(error => toast(error.message));
$('createPairing').onclick = async () => {
  $('createPairing').disabled = true;
  try {
    const result = await api('/api/pairing/code', {});
    $('pairingCode').textContent = result.code; $('pairingResult').hidden = false;
    const raw = result.expires_at; state.pairingExpires = typeof raw === 'number' ? (raw < 1e12 ? raw * 1000 : raw) : Date.parse(raw);
    const url = new URL(result.mobile_url || '/mobile', location.origin);
    if (!['http:', 'https:'].includes(url.protocol)) throw new Error('手机连接地址无效');
    $('pairingLink').href = url.href; $('pairingLink').textContent = url.href; $('pairingLink').hidden = false;
    $('pairingTrust').hidden = true; $('pairingTrustLink').removeAttribute('href');
    if (result.trust_url) {
      const trust = new URL(result.trust_url, location.origin);
      if (['http:', 'https:'].includes(trust.protocol)) { $('pairingTrustLink').href = trust.href; $('pairingTrust').hidden = false; }
    }
    clearInterval(state.pairingTimer); updatePairingExpiry(); state.pairingTimer = setInterval(updatePairingExpiry, 1000);
  } catch (error) { toast(error.message); }
  finally { $('createPairing').disabled = false; }
};
async function init() {
  try {
    const session = await api('/api/session', { name: '电脑观察台', kind: 'desktop' }); state.device = session.device || null;
    if (session.pairing_required) { state.pairingRequired = true; $('pairingRequired').hidden = false; $('sendButton').disabled = true; $('voiceButton').disabled = true; $('syncStatus').textContent = '请先配对设备'; return; }
    state.config = await api('/api/config');
    $('hermesStatus').textContent = state.config.hermes_configured ? '已配置 · 待验证' : '未配置';
    $('voiceStatus').textContent = state.config.voice_configured ? '已配置 · 待连接' : '未配置';
    $('hermesDot').classList.toggle('ready', state.config.hermes_configured); $('voiceDot').classList.toggle('ready', state.config.voice_configured);
    if (state.config.hermes_configured) {
      api('/api/hermes-health').then(health => {
        $('hermesStatus').textContent = health.ready ? 'API 已连接' : 'API 未连接';
        $('hermesDot').classList.toggle('ready', health.ready);
        $('hermesStatus').title = state.config.backend_model;
      }).catch(() => { $('hermesStatus').textContent = '连接检查失败'; });
    }
    await refreshCatalog();
    await refreshMessages().catch(() => { if (!state.sessionInvalid) $('syncStatus').textContent = '对话同步暂不可用'; }); startConversationStream();
    await refreshVoiceOwner().catch(() => {});
    const { cursor } = await api('/api/events/cursor'); await poll(); startEvents(cursor);
    await pollPlanEvents().catch(() => {});
    setInterval(() => { if (!state.sessionInvalid) pollPlanEvents().catch(() => {}); }, 2000);
    setInterval(updateTimers, 100);
    setInterval(() => { if (state.sessionInvalid) return; poll().catch(() => {}); refreshMessages().catch(() => { if (!state.sessionInvalid) $('syncStatus').textContent = '对话同步暂不可用'; }); refreshVoiceOwner().catch(() => {}); if ($('phoneDialog').open) refreshDevices().catch(() => {}); }, 5000);
  } catch (e) { toast(`初始化失败：${e.message}`); }
}
$('resetDevice').onclick = async () => {
  $('resetDevice').disabled = true;
  try { await api('/api/session/reset', {}); location.reload(); }
  catch (error) { toast(error.message); $('resetDevice').disabled = false; }
};
init();
