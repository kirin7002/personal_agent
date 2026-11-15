/* Mobile projection only. Canonical conversation messages are the sole chat source. */
const $ = id => document.getElementById(id);
const activeStatuses = new Set(['running', 'awaiting_input', 'waiting']);
const labels = {running:'正在处理', awaiting_input:'等你补充', waiting:'已安排', generated:'模拟完成', completed:'已完成', partial:'部分完成', failed:'未能完成', cancelled:'已取消', expired:'已过期', interrupted:'已中断', unsupported:'暂不支持', pending:'已安排', paused:'已暂停', active:'已安排'};
const state = {messages:new Map(), tasks:[], plans:[], devices:[], device:null, owner:null, cursor:0, epoch:0, ready:false, online:true, busy:false, reply:null, pending:null, stream:null, retry:null, refreshTimer:null, voice:null, voiceGeneration:0, voiceStarting:false, voiceStatus:'stopped', tab:'activity', requestSequence:{}};
function readSaved(key) { try { return sessionStorage.getItem(key); } catch { return null; } }
function save(key, value) { try { value == null ? sessionStorage.removeItem(key) : sessionStorage.setItem(key, value); } catch {} }
function el(tag, className='', text='') { const node=document.createElement(tag); node.className=className; node.textContent=text; return node; }
function icon(name) { const svg=document.createElementNS('http://www.w3.org/2000/svg','svg'); const use=document.createElementNS(svg.namespaceURI,'use'); use.setAttribute('href',`#i-${name}`); svg.append(use); svg.setAttribute('aria-hidden','true'); return svg; }
function button(text, action, className='') { const node=el('button',className,text); node.type='button'; node.addEventListener('click',action); return node; }
function showError(id, message='') { $(id).textContent=message; $(id).hidden=!message; }
function toast(text) { $('toast').textContent=text; $('toast').hidden=false; clearTimeout(toast.timer); toast.timer=setTimeout(()=>$('toast').hidden=true,4000); }
async function api(path, body) {
  const response=await fetch(path,{method:body === undefined?'GET':'POST',credentials:'same-origin',headers:body === undefined?{}:{'Content-Type':'application/json'},body:body === undefined?undefined:JSON.stringify(body),signal:AbortSignal.timeout(15000)});
  const data=await response.json().catch(()=>({}));
  if(!response.ok) { const error=new Error(typeof data.detail==='string'?data.detail:'暂时没有连接成功，请稍后重试。'); error.status=response.status; throw error; }
  return data;
}
function setOnline(value) { state.online=value; $('offlineBanner').hidden=value; renderStatus(); }
function renderStatus() {
  const focused=state.tasks.find(t=>t.id===state.focusId);
  $('connectionStatus').textContent=!state.online?'连接已断开':state.voiceStatus==='connecting'?'正在连接语音…':state.voice?.connected?'语音已连接':focused&&activeStatuses.has(focused.status)?labels[focused.status]:state.ready?'随时在这里':state.pairingRequired?'待连接电脑':'正在连接…';
}
function upsert(message) {
  if(!message?.id || !['assistant','user'].includes(message.role)) return;
  const key=String(message.id), previous=state.messages.get(key);
  if(previous && Number(previous.version??0)>Number(message.version??0)) return;
  state.messages.set(key,{...previous,...message});
}
function readableResult(text) {
  // Remove only known success boilerplate. Keep qualifications, reasons and
  // negations even when they share a sentence with technical vocabulary.
  return String(text||'')
    .replace(/调用结构已(?:通过校验|校验通过|校验)[，,；;。\s]*/g,'')
    .replace(/\bSchema\s*已(?:通过校验|校验通过|通过)[，,；;。\s]*/gi,'')
    .replace(/\b(?:control|status|manual|catalog)\.[a-zA-Z0-9_.]+\b/g,'这项操作')
    .replace(/\bSchema\s+failure\b/gi,'参数校验失败')
    .replace(/\bSchema\s*(?:校验|验证)/gi,'参数校验')
    .replace(/\bSchema\b/gi,'参数校验')
    .replace(/\bJSON\b/g,'返回数据')
    .replace(/调用结构/g,'调整预览')
    .replace(/工具调用/g,'操作')
    .trim();
}
function messageText(message) {
  if(message.role==='user') return String(message.text||'');
  const kind=message.kind||message.raw_source?.kind||'';
  const data=message.raw_source?.data||{};
  const text=String(message.text||data.message||'');
  if(kind==='task.generated') {
    const calls=data.calls;
    let control='已模拟完成这次调整。';
    if(Array.isArray(calls) && calls.length===1) {
      const call=calls[0], args=call.arguments||{}, tool=call.tool||call.tool_id||call.name||'';
      const zones={all:'全部分区',driver:'主驾',passenger:'副驾',rear:'后排'};
      if(['control.climate.set_temperature','climate_set_temperature'].includes(tool) && zones[args.zone] && typeof args.temperature_c==='number' && Number.isFinite(args.temperature_c)) {
        control=`已模拟设置：${zones[args.zone]}${args.temperature_c}度。`;
      }
    }
    // Calls describe the control target, never a measured cabin temperature.
    // A mixed result's query and limitations must survive the concise projection.
    const query=text.match(/模拟查询结论[：:]\s*([\s\S]*)/);
    if(query) return `${control}\n模拟查询：${readableResult(query[1])||'结果暂未同步。'}`;
    if(data.outcome==='mixed') return `${control}\n${readableResult(text)||'模拟查询结果暂未同步。'}`;
    return control;
  }
  if(kind==='task.expired') return '这次请求已过期。需要的话再告诉我。';
  if(kind==='task.cancelled') return '好，这次不调整了。';
  // Never replace a partial/unsupported/query explanation with a generic status:
  // the unfinished part and its reason are part of the result, not diagnostics.
  return readableResult(text);
}
function renderMessages() {
  const scroller=$('conversation'); const nearBottom=scroller.scrollHeight-scroller.scrollTop-scroller.clientHeight<100; const top=scroller.scrollTop;
  const fragment=document.createDocumentFragment();
  const ordered=[...state.messages.values()].sort((a,b)=>Number.isFinite(Number(a.id))&&Number.isFinite(Number(b.id))?Number(a.id)-Number(b.id):(Number(a.created)||0)-(Number(b.created)||0));
  for(const message of ordered) {
    if(message.deleted) continue;
    const task=state.tasks.find(t=>t.id===message.task_id);
    const old=message.role==='assistant' && (message.superseded || message.valid===false || message.status==='superseded' || (message.task_revision!=null && task && Number(message.task_revision)<Number(task.revision)));
    const article=el('article',`message ${message.role}${old?' superseded':''}`); article.dataset.messageId=message.id;
    if(message.role==='assistant') article.append(el('p','message-author','小伴'));
    const isResult=message.role==='assistant' && /result|completed|generated|plan_created|plan_updated/.test(message.kind||'');
    const body=el('div',isResult?'result-card':'');
    if(isResult) { const caption=el('div','result-label'); caption.append(icon(message.plan_id?'spark':'check'),document.createTextNode(message.plan_id?'日常计划':'处理结果 · 模拟车辆')); body.append(caption); }
    body.append(el('div','bubble',messageText(message))); article.append(body);
    if(isResult && message.kind==='task.generated' && task && !old) body.append(el('p','message-note',task.goal));
    if(old) article.append(el('p','message-note','已更新，请以最新状态为准'));
    if(!old && message.role==='assistant' && task?.status==='awaiting_input' && /question|clarif/.test(message.kind||'')) {
      const options=Array.isArray(message.options)?message.options.map(o=>typeof o==='string'?o:o.label||o.text).filter(Boolean):['主驾','副驾','后排','全部'].filter(o=>(message.text||task.question||'').includes(o));
      const choices=el('div','choices');
      for(const option of options.slice(0,6)) choices.append(button(option,()=>sendText(option,{task_id:task.id,task_revision:task.revision}),'choice'));
      if(options.length) article.append(choices);
      article.append(el('p','message-note','等你补充 · 问题保留 5 分钟'));
    }
    if(message.plan_id && message.role==='assistant') body.append(button('查看计划',()=>openDrawer('plans'),'card-link'));
    fragment.append(article);
  }
  $('messages').replaceChildren(fragment); $('welcome').hidden=state.messages.size>0;
  if(nearBottom) scroller.scrollTop=scroller.scrollHeight; else scroller.scrollTop=top;
}
async function refreshMessages() {
  const epoch=state.epoch, seq=state.requestSequence.messages=(state.requestSequence.messages||0)+1;
  const result=await api('/api/conversation/messages');
  if(epoch!==state.epoch || seq!==state.requestSequence.messages) return;
  for(const message of result.messages||[]) upsert(message);
  if(!state.historyLoaded) { state.before=result.next_before; $('loadEarlier').hidden=!result.has_more; }
  // Snapshot refreshes must not skip undelivered SSE updates (including edits to
  // old messages outside the latest page). Only the initial snapshot sets a base.
  if(!state.hydrated) {state.cursor=Number(result.cursor)||0;state.hydrated=true;}
  renderMessages();
}
async function loadEarlier() {
  if(!state.before) return;
  const epoch=state.epoch, scroller=$('conversation'), height=scroller.scrollHeight, top=scroller.scrollTop;
  $('loadEarlier').disabled=true;
  try {
    const result=await api(`/api/conversation/messages?before=${encodeURIComponent(state.before)}`);
    if(epoch!==state.epoch)return;
    for(const message of result.messages||[])upsert(message);
    state.historyLoaded=true; state.before=result.next_before; $('loadEarlier').hidden=!result.has_more;
    renderMessages(); scroller.scrollTop=top+scroller.scrollHeight-height;
  } catch { toast('更早的对话暂时未能加载，请重试。'); }
  finally { $('loadEarlier').disabled=false; }
}
async function refreshTasks() {
  const epoch=state.epoch, seq=state.requestSequence.tasks=(state.requestSequence.tasks||0)+1;
  const result=await api('/api/tasks'); if(epoch!==state.epoch || seq!==state.requestSequence.tasks) return;
  state.tasks=Array.isArray(result)?result:result.tasks||[]; state.focusId=result.focus_task_id;
  renderStatus(); renderMessages(); renderActivity();
}
async function refreshPlans() {
  const epoch=state.epoch, seq=state.requestSequence.plans=(state.requestSequence.plans||0)+1;
  const result=await api('/api/plans'); if(epoch!==state.epoch || seq!==state.requestSequence.plans) return;
  state.plans=Array.isArray(result)?result:result.plans||[]; state.timezone=result.timezone; renderPlans();
}
async function refreshDevices() {
  const epoch=state.epoch;
  const result=await api('/api/devices'); if(epoch!==state.epoch) return;
  state.devices=Array.isArray(result)?result:result.devices||[]; renderDevices();
}
async function refreshOwner() { const epoch=state.epoch; const result=await api('/api/voice-owner'); if(epoch===state.epoch) state.owner=result&&Object.hasOwn(result,'owner')?result.owner:result; }
async function refreshAll() {
  const results=await Promise.allSettled([refreshMessages(),refreshTasks(),refreshPlans(),refreshDevices(),refreshOwner()]);
  if(results[0].status==='rejected') throw results[0].reason;
}
function receiveEvent(event) {
  let data; try { data=JSON.parse(event.data); } catch { return; }
  state.cursor=Math.max(state.cursor,Number(data.cursor||data.id||event.lastEventId)||0);
  if(data.kind==='voice.process')return;
  if(data.message) { upsert(data.message); renderMessages(); }
  else if(data.role) { upsert(data); renderMessages(); }
  clearTimeout(state.refreshTimer);
  state.refreshTimer=setTimeout(()=>{ Promise.allSettled([refreshTasks(),refreshPlans(),...(data.message||data.role?[]:[refreshMessages()])]); },180);
}
function connectStream() {
  state.stream?.close(); clearTimeout(state.retry);
  if(!state.ready || document.hidden) return;
  const stream=new EventSource(`/api/conversation/stream?after=${encodeURIComponent(state.cursor)}`); state.stream=stream;
  const epoch=state.epoch;
  const receive=event=>{if(epoch===state.epoch && stream===state.stream) receiveEvent(event);};
  stream.onopen=()=>{ if(stream===state.stream) setOnline(true); };
  stream.onmessage=receive;
  for(const type of ['upsert','message.upsert','conversation.message']) stream.addEventListener(type,receive);
  stream.addEventListener('revoked',()=>{if(stream===state.stream) revoked();});
  stream.onerror=()=>{ if(stream!==state.stream) return; stream.close(); setOnline(false); state.retry=setTimeout(reconnect,2500); };
}
function revoked() {
  stopVoice(); state.epoch++; state.ready=false; state.pairingRequired=true; state.revoked=true;
  state.stream?.close(); state.stream=null; clearTimeout(state.retry); clearTimeout(state.refreshTimer);
  state.messages.clear(); state.tasks=[]; state.plans=[]; state.devices=[]; state.cursor=0; state.hydrated=false; state.historyLoaded=false; state.before=null;
  state.pending=null; save('mobile.pending',null); setReply(null);
  renderMessages(); renderActivity(); renderPlans(); renderDevices(); $('loadEarlier').hidden=true; fitInput();
  $('resetDevice').hidden=false; $('openPairing').disabled=true; $('pairHintText').textContent='设备连接已失效，点这里重新连接';
  showError('sendError','设备连接已失效，请重新连接。'); renderStatus();
}
async function reconnect() {
  clearTimeout(state.retry);
  if(document.hidden || state.revoked) return;
  try { if(!state.ready) return await initialize(); await refreshAll(); setOnline(true); connectStream(); }
  catch { setOnline(false); state.retry=setTimeout(reconnect,5000); }
}
function fitInput() { const input=$('messageInput'); input.style.height='auto'; input.style.height=`${Math.min(input.scrollHeight,128)}px`; $('sendButton').disabled=!state.ready || state.busy || !input.value.trim(); }
function setReply(task) { state.reply=task?{task_id:task.id,task_revision:task.revision}:null; $('replyContext').hidden=!task; $('replyLabel').textContent=task?`回复：${task.goal}`:''; }
async function sendText(text, target=state.reply) {
  text=text.trim(); if(!text || state.busy || !state.ready) return;
  const identity=JSON.stringify({text,...(target||{})});
  if(!state.pending || state.pending.identity!==identity) state.pending={identity,payload:{text,event_id:crypto.randomUUID(),...(target||{})}};
  save('mobile.pending',JSON.stringify(state.pending)); state.busy=true; fitInput(); showError('sendError');
  const original=$('messageInput').value;
  try {
    await api('/api/conversation/input',state.pending.payload);
    state.pending=null; save('mobile.pending',null);
    if(original.trim()===text && $('messageInput').value===original) { $('messageInput').value=''; save('mobile.draft',null); }
    setReply(null);
    // Never synthesize a chat bubble from this endpoint's summary or voice text.
    try { await refreshAll(); setOnline(true); }
    catch { setOnline(false); toast('消息已发送，正在重新同步。'); state.retry=setTimeout(reconnect,2000); }
    $('conversation').scrollTop=$('conversation').scrollHeight;
  } catch(error) {
    if(!$('messageInput').value) { $('messageInput').value=text; save('mobile.draft',text); if(target) setReply(state.tasks.find(task=>task.id===target.task_id)); }
    showError('sendError',`${error.message} 输入已保留，可以重试。`); if(!error.status) setOnline(false);
  }
  finally { state.busy=false; fitInput(); }
}
function selectTab(tab) {
  state.tab=tab;
  for(const name of ['activity','plans','devices']) { const selected=name===tab; $(`tab-${name}`).setAttribute('aria-selected',String(selected)); $(`tab-${name}`).tabIndex=selected?0:-1; $(`panel-${name}`).hidden=!selected; }
}
async function openDrawer(tab='activity') {
  selectTab(tab); if(!$('activityDialog').open) $('activityDialog').showModal(); showError('drawerError');
  if(state.revoked || state.pairingRequired)return;
  try { await Promise.all([refreshTasks(),refreshPlans(),refreshDevices()]); }
  catch { showError('drawerError','暂时无法更新，显示的是上次同步的内容。'); }
}
function renderActivity() {
  const list=$('activityList'); list.replaceChildren();
  if(!state.tasks.length) list.append(el('p','empty-card','还没有待办的小事。\n在对话里告诉我你的需求吧。'));
  for(const task of state.tasks) {
    const card=el('article','activity-card'); card.append(el('h3','',task.goal),el('span',`status-pill ${activeStatuses.has(task.status)?'':'terminal'}`,labels[task.status]||'状态待更新'));
    if(task.status==='awaiting_input' && task.question) card.append(el('p','',task.question));
    else if(task.summary) {
      const canonical=[...state.messages.values()].reverse().find(m=>m.task_id===task.id&&m.role==='assistant'&&!m.superseded&&m.kind===`task.${task.status}`);
      card.append(el('p','',messageText(canonical||{role:'assistant',kind:`task.${task.status}`,text:task.summary})));
    }
    const actions=el('div','card-actions');
    if(task.status==='awaiting_input') actions.append(button('补充一下',()=>{ setReply(task); $('activityDialog').close(); $('messageInput').focus(); }));
    if(activeStatuses.has(task.status)) actions.append(button('取消这次任务',async()=>{ await sendText('取消这个任务',{task_id:task.id,task_revision:task.revision}); if(!$('sendError').hidden) showError('drawerError',$('sendError').textContent); }));
    else actions.append(button('再试一次',()=>{ $('activityDialog').close(); setReply(null); $('messageInput').value=task.goal; fitInput(); $('messageInput').focus(); }));
    card.append(actions); list.append(card);
  }
}
function scheduleLabel(plan) {
  const schedule=plan.schedule||{};
  if(schedule.frequency==='interval') return `每 ${schedule.minutes||schedule.interval_minutes||'?'} 分钟`;
  const days=['周一','周二','周三','周四','周五','周六','周日'];
  if(schedule.frequency==='weekly') return `${(schedule.weekdays||[]).map(d=>days[d]||'').join('、')} ${schedule.time||''}`;
  return `每天 ${schedule.time||''}`.trim();
}
function nextRun(plan) {
  if(!plan.next_run_at) return plan.status==='paused'?'恢复后，继续接下来的安排':'暂无下一次安排';
  const date=new Date(typeof plan.next_run_at==='number'?plan.next_run_at*1000:plan.next_run_at);
  try { return '下次 '+new Intl.DateTimeFormat('zh-CN',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',timeZone:plan.timezone||state.timezone||'Asia/Shanghai'}).format(date); } catch { return '下次执行时间待同步'; }
}
async function planAction(plan, action, controls) {
  for(const control of controls.querySelectorAll('button')) control.disabled=true;
  showError('drawerError');
  try { await api(`/api/plans/${encodeURIComponent(plan.id)}/action`,{action,revision:plan.revision}); await Promise.all([refreshPlans(),refreshMessages()]); toast(action==='pause'?'计划已暂停':action==='resume'?'计划已恢复':action==='cancel_once'?'仅取消这一次，后续计划保留':'整个计划已取消'); }
  catch(error) { showError('drawerError',error.message); await refreshPlans().catch(()=>{}); }
  finally { for(const control of controls.querySelectorAll('button')) control.disabled=false; }
}
function renderPlans() {
  const list=$('plansList'); list.replaceChildren();
  if(!state.plans.length) list.append(el('p','empty-card','还没有周期计划。\n试试“每天早上 8 点查车辆电量”。'));
  for(const plan of state.plans) {
    const card=el('article','plan-card'); card.dataset.planId=plan.id;
    card.append(el('h3','',plan.goal),el('span',`status-pill ${plan.status==='cancelled'?'terminal':''}`,labels[plan.status]||'已安排'),el('p','',`${scheduleLabel(plan)} · ${plan.timezone||state.timezone||'当地时间'}\n${nextRun(plan)}`));
    if(plan.status!=='cancelled') {
      const actions=el('div','card-actions'); const scope=el('div','plan-scope'); scope.hidden=true;
      actions.append(button(plan.status==='paused'?'恢复计划':'暂停计划',()=>planAction(plan,plan.status==='paused'?'resume':'pause',card)));
      actions.append(button('取消…',()=>{ scope.hidden=!scope.hidden; }));
      scope.append(el('p','','要取消这一次，还是整个计划？'));
      const options=el('div','card-actions'); options.append(button('只取消这一次',()=>planAction(plan,'cancel_once',card)),button('取消整个计划',()=>planAction(plan,'cancel_plan',card),'danger')); scope.append(options); card.append(actions,scope);
    }
    list.append(card);
  }
}
function renderDevices() {
  const paired=state.devices.filter(d=>!d.revoked && d.status!=='revoked').length>1 || state.device?.paired===true;
  $('pairHintText').textContent=paired?'对话已与电脑同步':'连接电脑，让对话接着聊';
  const list=$('devicesList'); list.replaceChildren();
  for(const device of state.devices) {
    const card=el('div','device-card'); card.append(el('span','',device.name||device.label||(device.role==='observer'?'电脑观察台':'对话设备')),el('span','status-pill',device.id===state.device?.id?'当前设备':device.revoked?'已断开':'已连接')); list.append(card);
  }
}
function openPairing() { $('activityDialog').close(); showError('pairError'); $('pairCode').value=''; $('pairDialog').showModal(); }
async function redeem(event) {
  event.preventDefault(); const code=$('pairCode').value.replace(/[\s-]/g,'').toUpperCase(); if(!/^[A-Z0-9]{12}$/.test(code)) return showError('pairError','请输入电脑显示的完整 12 位配对码。');
  $('pairSubmit').disabled=true; showError('pairError');
  try {
    stopVoice(); const result=await api('/api/pairing/redeem',{code});
    state.epoch++; state.stream?.close(); state.messages.clear(); state.tasks=[]; state.plans=[]; state.devices=[]; state.cursor=0; state.hydrated=false; state.historyLoaded=false; state.before=null; state.reply=null; state.pending=null; save('mobile.pending',null); $('loadEarlier').hidden=true;
    state.device=result.device||state.device; renderMessages(); setReply(null); $('pairDialog').close();
    await refreshAll(); state.ready=true; state.pairingRequired=false; showError('sendError'); setOnline(true); fitInput(); connectStream(); toast('已连接，对话会在两端同步。');
  } catch(error) { if($('pairDialog').open) showError('pairError',error.message); else { setOnline(false); showError('sendError','配对已完成，正在重新同步对话。'); state.retry=setTimeout(reconnect,2000); } }
  finally { $('pairSubmit').disabled=false; }
}
function voiceState(status, detail) {
  state.voiceStatus=status;
  const running=['connecting','connected'].includes(status);
  $('voiceStrip').hidden=!running; $('voiceButton').setAttribute('aria-pressed',String(running)); $('voiceButton').setAttribute('aria-label',running?'结束语音对话':'开始语音对话');
  $('voiceStatus').textContent=status==='connecting'?'正在连接语音…':'可以说话了，我在听';
  if(status==='busy') { if(!$('voiceDialog').open) $('voiceDialog').showModal(); }
  if(status==='released') toast('语音已在另一台设备继续。');
  renderStatus();
}
function stopVoice() { state.voiceGeneration++; state.voice?.stop(); state.voiceStarting=false; voiceState('stopped'); }
async function startVoice(force=false) {
  if(state.voiceStarting || state.voice?.connected || state.voiceStatus==='connecting') return;
  if(!state.ready) return toast('连接成功后，就可以开始语音。');
  showError('sendError'); state.voiceStarting=true;
  const generation=++state.voiceGeneration;
  const current=()=>generation===state.voiceGeneration && !document.hidden;
  try {
    await refreshOwner();
    if(!current()) return;
    if(!force && state.owner?.device_id && state.owner.device_id!==state.device?.id) { $('voiceDialog').showModal(); return; }
    if(!window.isSecureContext) throw new Error('语音需要安全连接。请使用受信任的 HTTPS 地址，文字仍可使用。');
    const {VoiceSession}=await import('/static/voice-client.js');
    if(!current()) return;
    state.voice=new VoiceSession({onState:(status,detail)=>{if(current())voiceState(status,detail);},onError:error=>{ if(!current())return; showError('sendError',error.message||'语音连接中断，请重新开始。'); stopVoice(); },onEvent:event=>{
      if(!current()) return;
      if(event.type==='speech.started' || event.type==='speech_started' || event.type==='input_audio_buffer.speech_started') $('voiceStatus').textContent='我在听，慢慢说';
      if(event.type==='assistant.delta') $('voiceStatus').textContent='小伴正在回应';
      if(event.type==='done') $('voiceStatus').textContent='可以继续说，我在听';
      if(/transcript|done|invalidate/.test(event.type||'')) { refreshMessages().catch(()=>{}); refreshTasks().catch(()=>{}); }
    }});
    await state.voice.start();
  } catch(error) { if(!current())return; stopVoice(); showError('sendError',error.name==='NotAllowedError'?'没有获得麦克风权限。允许访问后再试，也可以继续打字。':error.message||'暂时无法开始语音，请稍后重试。'); }
  finally { if(generation===state.voiceGeneration)state.voiceStarting=false; }
}
async function takeover() {
  $('takeoverVoice').disabled=true;
  try { await api('/api/voice/takeover',{}); $('voiceDialog').close(); await startVoice(true); }
  catch(error) { toast(error.message); }
  finally { $('takeoverVoice').disabled=false; }
}
function fitViewport() { const viewport=window.visualViewport; document.documentElement.style.setProperty('--app-height',`${viewport?viewport.height:window.innerHeight}px`); }
async function initialize() {
  try {
    const session=await api('/api/session',{name:'我的手机',kind:'mobile'}); state.device=session.device||session.device_public||null; state.pairingRequired=!!session.pairing_required; $('openPairing').disabled=false;
    if(session.pairing_required) { $('pairHintText').textContent='输入电脑配对码，连接共享对话'; showError('sendError','这台设备尚未配对，点下方入口连接电脑。'); setOnline(true); return; }
    await refreshAll(); state.ready=true; setOnline(true); fitInput(); connectStream();
  }
  catch(error) { if(error.status===401)revoked(); else {setOnline(false);showError('sendError','暂时没有连接成功，请点“重新连接”。草稿会保留。');} }
}
$('messageInput').value=readSaved('mobile.draft')||'';
try { state.pending=JSON.parse(readSaved('mobile.pending')); } catch {}
$('messageInput').addEventListener('input',()=>{save('mobile.draft',$('messageInput').value);fitInput();showError('sendError');});
$('messageInput').addEventListener('keydown',event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();sendText($('messageInput').value);}});
$('messageForm').addEventListener('submit',event=>{event.preventDefault();sendText($('messageInput').value);});
$('clearReply').addEventListener('click',()=>setReply(null));
$('loadEarlier').addEventListener('click',loadEarlier);
$('resetDevice').addEventListener('click',async()=>{
  $('resetDevice').disabled=true;
  try {await api('/api/session/reset',{});state.revoked=false;state.pairingRequired=false;$('resetDevice').hidden=true;await initialize();if(state.pairingRequired){$('activityDialog').close();openPairing();}}
  catch(error){showError('drawerError',error.message);}
  finally{$('resetDevice').disabled=false;}
});
$('openActivity').addEventListener('click',()=>openDrawer()); $('openMenu').addEventListener('click',()=>openDrawer());
for(const node of document.querySelectorAll('[data-close]')) node.addEventListener('click',()=>$(node.dataset.close).close());
for(const node of document.querySelectorAll('[data-tab]')) { node.addEventListener('click',()=>selectTab(node.dataset.tab)); node.addEventListener('keydown',event=>{if(!['ArrowLeft','ArrowRight'].includes(event.key))return;event.preventDefault();const names=['activity','plans','devices'];const name=names[(names.indexOf(state.tab)+(event.key==='ArrowRight'?1:2))%3];selectTab(name);$(`tab-${name}`).focus();}); }
for(const node of document.querySelectorAll('[data-suggestion]')) node.addEventListener('click',()=>{$('messageInput').value=node.dataset.suggestion;save('mobile.draft',$('messageInput').value);fitInput();$('messageInput').focus();});
$('newPlan').addEventListener('click',()=>{$('activityDialog').close();setReply(null);$('messageInput').value='每天早上8点查车辆电量';fitInput();$('messageInput').focus();});
$('pairHint').addEventListener('click',()=>state.pairingRequired&&!state.revoked?openPairing():openDrawer('devices')); $('openPairing').addEventListener('click',openPairing); $('pairForm').addEventListener('submit',redeem);
$('pairCode').addEventListener('input',()=>{$('pairCode').value=$('pairCode').value.replace(/[^A-Za-z0-9-]/g,'').toUpperCase();});
$('reconnect').addEventListener('click',()=>{showError('sendError');reconnect();});
$('voiceButton').addEventListener('click',()=>state.voice?.connected||state.voiceStarting||state.voiceStatus==='connecting'?stopVoice():startVoice()); $('stopVoice').addEventListener('click',stopVoice); $('takeoverVoice').addEventListener('click',takeover);
window.addEventListener('offline',()=>{setOnline(false);stopVoice();}); window.addEventListener('online',reconnect);
document.addEventListener('visibilitychange',()=>{if(document.hidden){stopVoice();state.stream?.close();clearTimeout(state.retry);}else reconnect();});
window.addEventListener('pagehide',()=>{stopVoice();state.stream?.close();clearTimeout(state.retry);});
window.visualViewport?.addEventListener('resize',fitViewport); window.addEventListener('resize',fitViewport);
fitViewport(); fitInput(); initialize();
