const $ = (s) => document.querySelector(s);
// Public offline demo identity, NOT a production login credential.
// Replace with the authenticated IAM session; never put an operator token here.
const demoSessionHeaders = {'Authorization': 'Bearer demo-user-session'};
let identityMode='DEMO';
async function loadIdentity(){
  try{
    const cap=await(await fetch('/api/v1/identity/capabilities')).json();identityMode=cap.mode;
    if(identityMode!=='SELF_HOSTED')return;
    delete demoSessionHeaders.Authorization;
    const r=await fetch('/api/v1/identity/me');const me=await r.json();
    if(!r.ok||!me.ok){location.assign('/identity/login');return;}
    const bar=document.createElement('section');bar.id='identitySession';bar.style.cssText='padding:12px;background:#edf4ec;margin:8px;border-radius:8px';
    const label=document.createElement('strong');label.textContent=`已核验本机身份：${me.user_id} · 密码与动态码；未取得银行机构认证。`;
    const logout=document.createElement('button');logout.type='button';logout.textContent='退出登录';logout.id='identityLogout';
    logout.addEventListener('click',async()=>{try{const r=await fetch('/api/v1/identity/logout',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});if(r.ok)location.assign('/identity/login');else showToast('退出结果未确认，请重试');}catch(_){showToast('退出结果未确认，请重试');}});
    bar.append(label,logout);document.body.prepend(bar);$('#demoOtpHint').textContent='请使用身份验证器中下一枚未使用的动态码。';
    await loadFamilyPermissions();
  }catch(_){/* The server still enforces identity when metadata cannot be loaded. */}
}
// Separate each page's unfinished draft; no dialogue is stored in the browser.
const conversationId = 'web-' + Array.from(crypto.getRandomValues(new Uint8Array(16)),
  value => value.toString(16).padStart(2, '0')).join('');
let currentPlan = null;
const dynamicTaskViews=new Map();
let currentResultView = null;
let busy = false;
let transferDraft = null;
let ocrReviewToken = null;
let voiceSourceToken = null;
function readPreference(key) { try { return localStorage.getItem(key); } catch (_) { return null; } }
function savePreference(key, value) { try { localStorage.setItem(key, value); } catch (_) {} }
let elderMode = readPreference('elderMode') !== 'false';
let focusMode = readPreference('focusMode') === null ? elderMode : readPreference('focusMode') !== 'false';
let voicePrivacy = readPreference('voicePrivacy') !== 'false';
let currentDisplay = null;
let supportBusy = false;
let supportRequestId = null;
let supportCaseStatus = null;
let liveSupportCase=null,liveSupportCursor=0,liveSupportTimer=null,liveSupportPolling=false,liveSupportPending=null,liveSupportClosed=false;
const liveSupportMessages=new Map();
const cardDisplayKey = 'bankAgent:demo-user:cardDisplay:v1';
let displayCards = null, cardPreferenceNotice = '';
let hiddenCards = new Set();
try {
  const saved = JSON.parse(readPreference(cardDisplayKey) || '[]');
  if (!Array.isArray(saved) || saved.length > 100 || saved.some(x=>typeof x!=='string' || !/^[A-Za-z0-9_-]{1,128}$/.test(x))) throw Error('invalid preference');
  hiddenCards = new Set(saved);
} catch (_) { cardPreferenceNotice = '本地显示偏好损坏，已按全部显示处理。'; }
const log = $('#chatLog');
let amountsHidden = readPreference('bankAgent:hideAmounts') === 'true';
let accountSnapshot = null;
function renderAccountAmounts() {
  const d = accountSnapshot;
  $('#balanceValue').textContent = amountsHidden ? '••••••' : d ? money(d.balance?.available_cents) : '读取中…';
  $('#balancePrivacy').textContent = amountsHidden ? '显示' : '隐藏';
  $('#balancePrivacy').setAttribute('aria-label', amountsHidden ? '显示账户金额' : '隐藏账户金额');
  $('#balancePrivacy').setAttribute('aria-pressed', String(amountsHidden));
  const daily=d && d.daily_transfer_cents, limit=d && d.daily_transfer_limit_cents;
  const valid=Number.isSafeInteger(daily)&&Number.isSafeInteger(limit)&&limit>0;
  $('#limitValue').textContent = amountsHidden ? '••••••' : valid ? money(Math.max(0,limit-daily)) : '—';
  $('#limitHint').textContent = amountsHidden ? '账户金额已隐藏' : valid ? `今天已用 ${money(daily)} / ${money(limit)}` : '额度暂不可用';
  $('#limitProgress').style.width = !amountsHidden&&valid ? `${Math.min(100,daily*100/limit)}%` : '0%';
}
$('#balancePrivacy').addEventListener('click',()=>{
  amountsHidden=!amountsHidden;savePreference('bankAgent:hideAmounts',String(amountsHidden));renderAccountAmounts();
});
renderAccountAmounts();

// Move the live controls into a native modal: no cloned plans or duplicate actions.
const focusDialog = $('#focusDialog');
let focusOrigin = null, focusNodes = [], focusBackdropDown = false;
function applyFocusMode() {
  $('#focusToggle').textContent = focusMode ? '大字聚焦：开' : '大字聚焦：关';
  $('#focusToggle').setAttribute('aria-pressed', String(focusMode));
  $('#settingsFocusToggle').checked = focusMode;
  $('#focusEntryHint').textContent = focusMode ? '点一个功能，单独放大看；点周边空白返回' : '也可以在下面用自己的话说';
}
function openFocus(button) {
  if (!focusMode || focusDialog.open || busy) return;
  focusOrigin = button;
  $('#focusTitle').textContent = button.querySelector('b').textContent.trim();
  for (const row of log.children) row.classList.add('focus-history');
  currentPlan = null; currentResultView = null; currentDisplay = null;
  renderFraudWarning();
  $('#planPanel').innerHTML = '<div class="empty-state"><h3>正在读取，请稍候…</h3></div>';
  const move = (selector, target) => {
    const node = $(selector), placeholder = document.createComment('focus-home');
    node.before(placeholder); target.append(node); focusNodes.push({node, placeholder});
  };
  for (const selector of ['#elderFraudAlert', '#assistant', '#handoffCardPanel']) move(selector, $('#focusContent'));
  for (const selector of ['#customerServiceBtn', '#customerServiceStatus', '#newSupportRequestBtn', '#newSupportHint']) move(selector, $('#focusHelp'));
  for (const selector of ['#authModal', '#auditDrawer', '#toast', '#a11yStatus']) move(selector, focusDialog);
  document.body.classList.add('focus-open');
  focusDialog.showModal(); focusDialog.scrollTop = 0; $('#focusClose').focus();
}
function closeFocus() {
  if (focusDialog.open) focusDialog.close();
  if (!focusNodes.length) return;
  speechPlayer.stop();
  for (const {node, placeholder} of focusNodes) placeholder.replaceWith(node);
  focusNodes = [];
  for (const row of log.children) row.classList.remove('focus-history');
  document.body.classList.remove('focus-open');
  focusOrigin?.focus({preventScroll:true}); focusOrigin = null;
}
$('#focusClose').addEventListener('click', closeFocus);
focusDialog.addEventListener('close', closeFocus);
focusDialog.addEventListener('cancel', event => {
  event.preventDefault();
  if (!$('#authModal').classList.contains('hidden')) { $('#authModal').classList.add('hidden'); $('#confirmBtn')?.focus(); }
  else if (!$('#auditDrawer').classList.contains('hidden')) { $('#auditDrawer').classList.add('hidden'); $('#focusClose').focus(); }
  else closeFocus();
});
function outsideFocus(event) {
  const r = focusDialog.getBoundingClientRect();
  return event.target === focusDialog && (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom);
}
focusDialog.addEventListener('pointerdown', event => { focusBackdropDown = outsideFocus(event); });
focusDialog.addEventListener('pointercancel', () => { focusBackdropDown = false; });
focusDialog.addEventListener('click', event => {
  if (focusBackdropDown && outsideFocus(event)) closeFocus();
  focusBackdropDown = false;
});
$('#focusToggle').addEventListener('click', () => { focusMode = !focusMode; savePreference('focusMode', String(focusMode)); applyFocusMode(); });
$('#settingsFocusToggle').addEventListener('change', event => { focusMode = event.target.checked; savePreference('focusMode', String(focusMode)); applyFocusMode(); });
applyFocusMode();

function applyElderMode() {
  document.body.classList.toggle('elder-mode', elderMode);
  const btn = $('#elderToggle');
  btn.textContent = elderMode ? '长辈模式：开' : '长辈模式：关';
  btn.setAttribute('aria-pressed', String(elderMode));
  $('#settingsElderToggle').checked = elderMode;
  document.querySelectorAll('.bubble[data-standard]').forEach(bubble => {
    bubble.textContent = elderMode ? bubble.dataset.plain : bubble.dataset.standard;
  });
  if (currentDisplay) renderCurrentDisplay();
  renderFraudWarning();
}
function addMessage(text, who='bot', plainText=null) {
  document.body.classList.add('has-conversation');
  const row = document.createElement('div');
  row.className = `message ${who}`;
  row.innerHTML = `<div class="avatar">${who === 'bot' ? 'L' : '你'}</div><div><div class="bubble"></div><div class="time">刚刚</div></div>`;
  const bubble = row.querySelector('.bubble');
  if (who === 'bot') { bubble.dataset.standard = text || ''; bubble.dataset.plain = plainText ?? text ?? ''; }
  bubble.textContent = who === 'bot' && elderMode ? bubble.dataset.plain : text || '';
  log.appendChild(row); log.scrollTop = log.scrollHeight;
}
function escapeHtml(value) { const s=String(value ?? ''); return s.replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function riskClass(level) { return ['green','yellow','red'].includes(level) ? level : 'yellow'; }
function riskMeta(level, phase) {
  const normalized = riskClass(level);
  const labels = {green:'绿色', yellow:'黄色', red:'红色'};
  const icons = {green:'✓', yellow:'!', red:'⚠'};
  const label = labels[normalized];
  return {className: normalized, icon: icons[normalized], text: `${label} · ${phase}`, aria: `风险等级：${label}；状态：${phase}`};
}
function speechStorage(){try{return window.localStorage;}catch(_){return null;}}
const speechPreferences = BankSpeech.loadPreferences(speechStorage());
$('#speechEnabled').checked=speechPreferences.enabled;
$('#speechCloud').checked=speechPreferences.cloud;
$('#speechVoice').value=speechPreferences.voice;
$('#speechRate').value=String(speechPreferences.rate);
const speechPlayer = new BankSpeech.SpeechPlayer({
  fetch: (...args)=>fetch(...args), Audio, URL, preloadAudio:true, speechSynthesis:window.speechSynthesis,
  Utterance:window.SpeechSynthesisUtterance, headers:()=>demoSessionHeaders,
  status:text=>{ $('#speechStatus').textContent=text; }
});
// Only bundled public phrases are preloaded. No synthesis, user text or bank calls.
speechPlayer.preparePrompts();
function speechSafe(text) { return voicePrivacy ? BankSpeech.safeText(text) : String(text ?? ''); }
function announceA11y(text, speak = true) {
  const spoken = speechSafe(text);
  const node = $('#a11yStatus'); if(node)node.textContent=spoken;
  if(speak && $('#speechEnabled').checked) speechPlayer.speak(spoken, {
    cloud:$('#speechCloud').checked, privacy:voicePrivacy,
    voice:$('#speechVoice').value, rate:Number($('#speechRate').value)
  });
}
$('#speechPreview').addEventListener('click',()=>speechPlayer.speak(BankSpeech.SAMPLE, {
  cloud:$('#speechCloud').checked, privacy:true, force:true,
  voice:$('#speechVoice').value, rate:Number($('#speechRate').value)
}));
$('#speechStop').addEventListener('click',()=>{speechPlayer.stop();$('#speechStatus').textContent='已停止播报';});
for(const id of ['speechEnabled','speechCloud','speechVoice','speechRate'])
  $('#'+id).addEventListener('change',()=>{
    speechPlayer.stop();
    const saved=BankSpeech.savePreferences(speechStorage(),{enabled:$('#speechEnabled').checked,
      cloud:$('#speechCloud').checked,voice:$('#speechVoice').value,rate:Number($('#speechRate').value)});
    $('#speechStatus').textContent=saved?'语音设置已保存，刷新后继续生效。':'浏览器无法保存设置，仅本次页面有效。';
  });
for(const id of ['settingsVoicePrivacy','voicePrivacyToggle'])
  $('#'+id).addEventListener('change',()=>{speechPlayer.stop();$('#speechStatus').textContent='隐私设置已更新。';});
window.addEventListener('pagehide',()=>speechPlayer.stop());
fetch('/api/v1/voice/output-capabilities').then(r=>r.json()).then(data=>{
  // Keep the saved source editable even when the provider is unavailable.
  $('#speechCloud').disabled=false;
  $('#speechStatus').textContent=$('#speechCloud').checked
    ?(data.available?'已恢复自然语音设置，可点击试听。':'自然语音暂不可用，已保留您的选择，不会自动切换音源。')
    :(data.available?'常用提示使用本地自然语音；其他内容可启用联网播报。':'常用提示使用本地自然语音；其他内容使用本机普通话。');
}).catch(()=>{$('#speechCloud').disabled=false;$('#speechStatus').textContent=$('#speechCloud').checked
  ?'自然语音暂不可用，已保留您的选择，不会自动切换音源。':'常用提示使用本地自然语音；其他内容可使用本机普通话。';});
function applyVoicePrivacy() {
  const toggle = $('#voicePrivacyToggle');
  if (!toggle) return;
  toggle.checked = voicePrivacy;
  toggle.setAttribute('aria-checked', String(voicePrivacy));
  $('#settingsVoicePrivacy').checked = voicePrivacy;
}
function renderCardDisplay() {
  const list = $('#visibleCardList'); list.replaceChildren();
  if (!displayCards) { $('#cardDisplayStatus').textContent = '暂时无法读取卡片来源，请刷新账户后重试；没有修改卡片状态。'; return; }
  const hiddenCount = displayCards.filter(c=>hiddenCards.has(c.id)).length;
  $('#cardDisplayStatus').textContent = `${cardPreferenceNotice}当前隐藏${hiddenCount}张。仅影响本页卡片列表，不挂失、不注销。`;
  for (const card of displayCards) {
    const hidden = hiddenCards.has(card.id);
    if (hidden && !$('#showHiddenCards').checked) continue;
    const row = document.createElement('li');
    row.append(document.createTextNode(`${card.name} · 尾号${card.last4} · ${card.status}${hidden?'（已隐藏）':''}`));
    const button = document.createElement('button'); button.type = 'button'; button.className = 'ghost';
    button.textContent = `${hidden?'恢复显示':'仅隐藏'}${card.name}`;
    button.addEventListener('click',()=>{
      if (hidden) hiddenCards.delete(card.id); else hiddenCards.add(card.id);
      // Store IDs only. No balance, card state or authority lives in preferences.
      try { localStorage.setItem(cardDisplayKey,JSON.stringify([...hiddenCards])); cardPreferenceNotice = ''; }
      catch (_) { cardPreferenceNotice = '浏览器无法保存，修改仅本次页面有效。'; }
      renderCardDisplay();
      const next = list.querySelector('button'); (next || $('#showHiddenCards')).focus();
    });
    row.append(button); list.append(row);
  }
}
function updateSupportControls() {
  $('#newSupportRequestBtn').disabled = supportBusy || supportCaseStatus !== 'LOCAL_CLOSED';
}
let settingsOrigin = null;
function openSettings() {
  settingsOrigin = document.activeElement;
  $('#settingsElderToggle').checked = elderMode;
  $('#settingsVoicePrivacy').checked = voicePrivacy;
  renderCardDisplay(); $('#settingsDialog').showModal();
}
function restoreSettingsFocus() { (settingsOrigin?.isConnected ? settingsOrigin : $('#settingsBtn')).focus(); }
function closeSettings() { $('#settingsDialog').close(); restoreSettingsFocus(); }
window.__a11yAnnounce = announceA11y;
function friendlyRisk(level, protective=false) { return protective ? '已拦截保护' : ({green:'可以直接办理',yellow:'请你确认后办理',red:'需要再次验证'}[level] || '请你确认后办理'); }
function friendlyReason(level) { return ({green:'这是查询，不会改变账户里的钱。',yellow:'这是会改变账户状态的操作，先请你核对清楚。',red:'这是更重要的操作，除了核对，还需要再验证一次。'}[level] || '请你核对清楚后再继续。'); }
function setBusy(value) { busy=value; $('#sendBtn').disabled=value; $('#voiceBtn').disabled=value; $('#messageInput').disabled=value; document.querySelectorAll('#confirmBtn,#cancelBtn,.domain-choice button').forEach(button=>button.disabled=value); $('#typing').classList.toggle('hidden', !value); $('#assistantStatus').textContent=value?'正在处理':'在线'; }
function showToast(text) { const el=$('#toast'); el.textContent=text; el.classList.remove('hidden'); clearTimeout(window.toastTimer); window.toastTimer=setTimeout(()=>el.classList.add('hidden'),3200); }
function money(cents) { if (!Number.isInteger(cents)) return '—'; return `¥${(cents/100).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2})}`; }
function renderReceipt(receipt) {
  if (!receipt || !Array.isArray(receipt.items)) return '';
  const itemHtml = receipt.items.map(item => `<li class="receipt-item receipt-${escapeHtml(item.status||'UNKNOWN')}"><span class="receipt-status">${escapeHtml(item.status_label||'请核验')}</span><div><b>${escapeHtml(item.label||item.tool||item.node_id||'任务步骤')}</b><small>${escapeHtml(item.detail||'')}</small></div></li>`).join('');
  const compensationHtml = (receipt.compensations||[]).length ? `<div class="receipt-compensations"><b>补偿动作</b><ul>${receipt.compensations.map(item => `<li class="receipt-item receipt-${escapeHtml(item.status||'UNKNOWN')}"><span class="receipt-status">${escapeHtml(item.status_label||'请核验')}</span><div><b>${escapeHtml(item.label||'补偿动作')}</b><small>${escapeHtml(item.detail||'')}</small></div></li>`).join('')}</ul></div>` : '';
  return `<section class="receipt-list" aria-label="逐项办理回执"><h4>逐项办理回执</h4><ul>${itemHtml||'<li>暂无可展示的步骤</li>'}</ul>${compensationHtml}</section>`;
}

function renderPaymentRecovery(recovery) {
  if (!recovery || recovery.status==='ready' || !Array.isArray(recovery.blockers)) return '';
  const labels={recheck_facts:'重新核对资料',human_handoff:'联系人工核对',clarify:'补齐并核对资料',
    adjust_amount:'明确新的金额后重新确认',adjust_budget:'核对并调整预算安排',defer:'暂缓办理'};
  const blockers=recovery.blockers.map(x=>`<li>${escapeHtml(x.message||'请核对办理资料')}</li>`).join('');
  const choices=(recovery.user_options||[]).filter(x=>labels[x.action]).map(x=>`<li>${escapeHtml(labels[x.action])}</li>`).join('');
  return `<section id="paymentRecovery" aria-label="支付待核对事项"><b>支付待核对事项</b><ul>${blockers}</ul>${choices?`<p>处理建议：</p><ul>${choices}</ul>`:''}<p>${escapeHtml(recovery.notice||'继续办理前请重新核对。')}</p></section>`;
}

function renderPaymentEffect(summary) {
  if(!summary?.scope)return '';
  const expires=new Date(summary.expires_at);const expiry=Number.isNaN(expires.getTime())?'请重新核对':expires.toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'});
  const constraints=summary.constraints||{};const reserve=Number.isInteger(constraints.reserve_cents)?`<p>至少保留：${escapeHtml(money(constraints.reserve_cents))}；平台安全底线仍适用。</p>`:'';const deadline=constraints.deadline?`<p>最晚提交（北京时间）：${escapeHtml(new Date(constraints.deadline).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'}))}；不代表到账承诺。</p>`:'';
  return `<section id="paymentEffectScope" aria-label="本次动作范围"><b>本次动作范围</b><p>确认有效至（北京时间）：${escapeHtml(expiry)}</p><p>${escapeHtml(summary.scope)}：${escapeHtml(summary.duration)}</p><p>${escapeHtml(summary.reversal)}</p><p>${escapeHtml(summary.notice)}</p>${reserve}${deadline}</section>`;
}

function renderPlan(planView, resultView=null) {
  if (!planView) return;
  currentPlan=planView; currentResultView=resultView;
  const panel=$('#planPanel');
  const steps=(planView.steps||[]).map(step=>`<li class="step-${escapeHtml(step.status||'pending')}"><span class="step-dot">${step.status==='completed'?'✓':escapeHtml(step.index)}</span><div class="step-info"><b>${escapeHtml(step.title)}</b><small>${escapeHtml(step.description||'')} · ${escapeHtml(step.status_label||'')}</small></div></li>`).join('');
  const details=(planView.details||[]).map(x=>`<li>${escapeHtml(x)}</li>`).join('');
  const resultHtml=resultView?`<div class="result ${resultView.ok===false?'result-failed':''}"><b>${escapeHtml(resultView.title||'办理结果')}</b><p>${escapeHtml(resultView.user_message||'已处理。')}</p>${renderReceipt(resultView.receipt)}</div>`:'';
  const actions=planView.requires_confirmation&&planView.action_token&&!resultView?`<div class="plan-actions"><button class="confirm" id="confirmBtn" ${busy?'disabled':''}>${planView.requires_strong_auth?'确认并再次验证':'确认办理'}</button><button class="cancel" id="cancelBtn" ${busy?'disabled':''}>先不办理</button></div>`:'';
  const phaseLabel = planView.protective_block ? '已拦截保护'
    : planView.awaiting_details ? '请补充信息'
    : planView.deferred ? '待人工核实'
    : resultView ? ({SUCCESS:'已完成', PROCESSING:'银行确认中', UNKNOWN:'结果核验中', PARTIAL:'部分完成', FAILED:'未完成'}[resultView.status] || '请查看办理结果')
    : !planView.requires_confirmation ? '暂未办理' : friendlyRisk(planView.risk_level);
  const risk = riskMeta(planView.risk_level, phaseLabel);
  const reason = /^既有权限规则[：:]\s*(green|yellow|red)$/.test(planView.risk_reason || '')
    ? (planView.requires_strong_auth ? '请核对办理内容，并完成身份验证。' : planView.requires_confirmation ? '请核对办理内容，确认后继续。' : '本次仅查询，不会改变账户状态。')
    : planView.risk_reason || friendlyReason(planView.risk_level);
  panel.innerHTML=`<div class="plan-title"><span>${escapeHtml(elderMode?'这次要做的事':'本次操作计划')}</span><span class="risk ${risk.className}" role="status" aria-label="${escapeHtml(risk.aria)}"><span class="risk-icon" aria-hidden="true">${risk.icon}</span><span>${escapeHtml(risk.text)}</span></span></div><div class="plan-summary">${escapeHtml(planView.plain_summary || planView.summary || '')}</div><div class="risk-reason">${escapeHtml(reason)}</div>${details?`<ul class="plan-details">${details}</ul>`:''}${renderPaymentRecovery(planView.payment_recovery)}${renderPaymentEffect(planView.effect_summary)}${renderDomainReviews(planView.domain_reviews)}${renderSourceMaterials(planView.source_materials)}${elderMode?`<details class="technical-steps"><summary>查看办理步骤</summary><ol class="steps">${steps}</ol></details>`:`<ol class="steps">${steps}</ol>`}${planView.requires_confirmation&&!resultView?`<div class="confirmation-hint">${escapeHtml(planView.confirmation_hint||'请核对后再继续。')}</div>`:''}${actions}${planView.awaiting_details?'<button class="details-action" id="transferDetailsBtn">补充转账信息</button>':''}${resultHtml}`;
  announceA11y(`${phaseLabel}。${resultView?.user_message || planView.plain_summary || planView.summary || ''}`, false);
  $('#transferDetailsBtn')?.addEventListener('click',()=>openTransferDialog(transferDraft));
  $('#confirmBtn')?.addEventListener('click',confirmPlan); $('#cancelBtn')?.addEventListener('click',cancelPlan);
  renderDomainGuidance(planView.domain_guidance, panel);
  renderDynamicTask(planView,resultView);
}

function renderDomainGuidance(guidance, panel) {
  if (!guidance) return;
  const section=document.createElement('section');section.className='domain-guidance';section.id='domainGuidance';
  section.setAttribute('aria-label',guidance.title || '办理核验');
  const title=document.createElement('h3');title.textContent=guidance.title;section.append(title);
  const phase=document.createElement('p');phase.className='domain-phase';phase.textContent=guidance.phase;section.append(phase);
  const list=document.createElement('ul');
  for(const note of guidance.notes||[]){const li=document.createElement('li');li.textContent=note;list.append(li);}section.append(list);
  for(const choice of guidance.choices||[]){
    const row=document.createElement('div');row.className='domain-choice';
    const label=document.createElement('strong');label.textContent=choice.label;
    const detail=document.createElement('span');detail.textContent=choice.detail;row.append(label,detail);
    if(choice.request){const button=document.createElement('button');button.type='button';button.textContent=guidance.domain==='subscription'?'申请停止扣费':choice.request.startsWith('解挂')?'申请解挂':'申请挂失';button.disabled=busy;
      button.addEventListener('click',()=>send(choice.request,{displayText:(guidance.domain==='subscription'?'申请停止自动扣费：':choice.request.startsWith('解挂')?'申请解挂：':'申请挂失：')+choice.label}));row.append(button);}
    section.append(row);
  }
  if((guidance.choices||[]).length){const foot=document.createElement('p');foot.className='domain-phase';foot.textContent='选择只会创建新计划，仍需确认；卡片操作还需身份验证。';section.append(foot);}
  panel.append(section);
}

function drawDynamicGraph(container,nodes){
  const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');
  const byId=new Map(nodes.map(n=>[n.node_id,n])),level=new Map();
  for(let pass=0;pass<nodes.length;pass++)for(const n of nodes)if(!level.has(n.node_id) && n.depends_on.every(d=>level.has(d)))level.set(n.node_id,n.depends_on.length?1+Math.max(...n.depends_on.map(d=>level.get(d))):0);
  const groups=new Map();for(const n of nodes){const l=level.get(n.node_id)||0;if(!groups.has(l))groups.set(l,[]);groups.get(l).push(n);}
  const height=Math.max(140,Math.max(...[...groups.values()].map(g=>g.length))*80+40),width=(Math.max(...level.values())+1)*175+10,positions=new Map();
  for(const [l,g] of groups)g.forEach((n,i)=>positions.set(n.node_id,{x:l*175+90,y:(i+1)*height/(g.length+1)}));
  svg.setAttribute('viewBox',`0 0 ${width} ${height}`);svg.style.width='100%';svg.style.height='auto';svg.setAttribute('role','img');svg.setAttribute('aria-label','任务依赖图：箭头表示必须先完成的步骤');
  function el(tag,attrs,text){const e=document.createElementNS(ns,tag);for(const[k,v]of Object.entries(attrs))e.setAttribute(k,String(v));if(text)e.textContent=text;return e;}
  const defs=el('defs',{}),marker=el('marker',{id:'dynamic-arrow',viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:6,markerHeight:6,orient:'auto-start-reverse'});marker.append(el('path',{d:'M 0 0 L 10 5 L 0 10 z',fill:'#557865'}));defs.append(marker);svg.append(defs);
  for(const n of nodes)for(const dep of n.depends_on){const a=positions.get(dep),b=positions.get(n.node_id);if(a&&b)svg.append(el('path',{d:`M ${a.x+71} ${a.y} C ${a.x+95} ${a.y},${b.x-95} ${b.y},${b.x-73} ${b.y}`,fill:'none',stroke:'#557865','stroke-width':1.5,'marker-end':'url(#dynamic-arrow)'}));}
  for(const n of nodes){const p=positions.get(n.node_id);svg.append(el('rect',{x:p.x-71,y:p.y-23,width:142,height:46,rx:9,fill:n.status==='succeeded'?'#dbeedd':'#f0f6f2',stroke:'#527d65'}));const text=n.label || (n.node_type==='human_gate'?'重新核对':n.node_type==='delay'?'等待指定时间':n.tool);svg.append(el('text',{x:p.x,y:p.y-1,'text-anchor':'middle','font-size':12,fill:'#17291f'},text.length>11?text.slice(0,11)+'…':text));svg.append(el('text',{x:p.x,y:p.y+15,'text-anchor':'middle','font-size':10,fill:'#476451'},{pending:'待办理',ready:'条件待满足',running:n.node_type==='human_gate'?'等你确认':'核验中',succeeded:'已完成',failed:'失败',skipped:'未执行',compensated:'已回退'}[n.status]||n.status));}
  container.append(svg);
  const list=document.createElement('ol');for(const n of nodes){const li=document.createElement('li');li.textContent=`${n.label || n.tool || '人工核对'}${n.run_at?'：'+n.run_at:''}；前置：${n.depends_on.map(id=>byId.get(id)?.label || id).join('、') || '无'}`;list.append(li);}container.append(list);
}

async function refreshDynamicTask(planId){
  const view=dynamicTaskViews.get(planId);if(!view?.dag_id)return;
  try{const r=await fetch('/api/dag/state?dag_id='+encodeURIComponent(view.dag_id),{headers:demoSessionHeaders});const d=await r.json();if(!d.ok)return;
    const previous=new Map((view.nodes||[]).map(n=>[n.node_id,n]));view.nodes=d.nodes.map(n=>({...previous.get(n.node_id),...n}));view.status=d.status;
    if(currentPlan?.plan_id===planId)renderCurrentDisplay();
  }catch(_){}
}

function renderDynamicTask(planView,resultView){
  const view=dynamicTaskViews.get(planView.plan_id);
  if((!planView.requires_confirmation || resultView) && !view)return;
  const area=document.createElement('details');area.id='dynamicDagPanel';area.open=!!view;
  const summary=document.createElement('summary');summary.textContent='查看或调整本次任务安排';area.append(summary);
  const hint=document.createElement('p');hint.textContent='只编排本次已核对的操作和受权检查，不改金额、收款人或权限。采纳安排不会办理业务，需再按新卡确认。';area.append(hint);
  const feedback=document.createElement('p');feedback.setAttribute('role','status');area.append(feedback);
  function button(id,text,handler){const b=document.createElement('button');b.id=id;b.type='button';b.className='ghost';b.textContent=text;b.addEventListener('click',handler);area.append(b);return b;}
  if(planView.requires_confirmation && !resultView)button('dynamicDagPropose','生成任务安排',async()=>{
    if(busy)return;setBusy(true);try{const d=await postAgent('/api/dag/propose',{plan_id:planView.plan_id});if(!d.ok)throw Error(d.message||'暂不能编排');dynamicTaskViews.set(planView.plan_id,{proposal:d.proposal,nodes:d.nodes});if(dynamicTaskViews.size>64)dynamicTaskViews.delete(dynamicTaskViews.keys().next().value);renderCurrentDisplay();}catch(e){feedback.textContent=e.message;}finally{setBusy(false);}
  });
  if(planView.requires_confirmation && !resultView)button('crossDomainDagPropose','生成财务核查＋支付安排',async()=>{
    if(busy)return;setBusy(true);try{const d=await postAgent('/api/dag/propose',{plan_id:planView.plan_id,mode:'cross_domain'});if(!d.ok)throw Error(d.message||'本账户或后端暂不支持跨域资金核查');dynamicTaskViews.set(planView.plan_id,{proposal:d.proposal,nodes:d.nodes});renderCurrentDisplay();}catch(e){feedback.textContent=e.message;}finally{setBusy(false);}
  });
  if(view){const picture=document.createElement('div');picture.id='dynamicDagGraph';drawDynamicGraph(picture,view.nodes||[]);area.append(picture);}
  if(view?.proposal && planView.requires_confirmation && !resultView)button('dynamicDagAdopt','采纳安排，再核对新确认卡',async()=>{
    if(busy)return;setBusy(true);try{const d=await postAgent('/api/dag/adopt',{plan_id:planView.plan_id,action_token:planView.action_token,proposal:view.proposal});if(!d.ok)throw Error(d.message||'安排未通过检查');dynamicTaskViews.set(planView.plan_id,{nodes:d.dynamic_graph,dag_id:d.dag_id,status:'waiting_human'});receiveAgentResponse(d);}catch(e){feedback.textContent=e.message;}finally{setBusy(false);}
  });
  async function control(action,decision){try{const d=await postAgent('/api/dag/'+action,{dag_id:view.dag_id,action_token:planView.action_token,...(decision?{decision}: {})});if(!d.ok)throw Error(d.message||'当前状态不能操作');feedback.textContent=d.message||'已记录';if(d.plan_view)receiveAgentResponse(d);else if(d.status==='cancelled'||d.status==='handoff'){currentDisplay.standard.plan_view={...currentDisplay.standard.plan_view,requires_confirmation:false,action_token:''};currentDisplay.plain.plan_view={...currentDisplay.plain.plan_view,requires_confirmation:false,action_token:''};renderCurrentDisplay();}await refreshDynamicTask(planView.plan_id);}catch(e){feedback.textContent=e.message;}}
  if(view?.dag_id && !['completed','failed','cancelled','handoff'].includes(view.status)){
    button('dynamicDagPause','请求暂停',()=>control('pause'));
    if(resultView || !planView.requires_confirmation)button('dynamicDagResume','继续前重新确认',()=>control(view.status==='paused'?'resume':'approve'));
    button('dynamicDagHandoff','转人工核对',()=>control('approve','handoff'));
    button('dynamicDagCancel','取消剩余任务',()=>control('cancel'));
  }
  $('#planPanel').append(area);
}

function renderHandoffCard(card) {
  const panel = $('#handoffCardPanel');
  if (!panel) return;
  if (!card) { panel.classList.add('hidden'); return; }
  const block = (title, content) => `<section class="handoff-block"><h3>${escapeHtml(title)}</h3>${content}</section>`;
  const dict = (value) => Object.entries(value || {}).filter(([k,v]) => v !== '' && v != null).map(([k,v]) => `<p><b>${escapeHtml(k)}：</b>${escapeHtml(typeof v === 'object' ? JSON.stringify(v) : v)}</p>`).join('') || '<p>暂无</p>';
  const list = (value, fallback='暂无') => Array.isArray(value) && value.length ? `<ul>${value.map(item => `<li>${escapeHtml(typeof item === 'object' ? JSON.stringify(item) : item)}</li>`).join('')}</ul>` : `<p>${escapeHtml(fallback)}</p>`;
  panel.classList.remove('hidden');
  $('#handoffCardContent').innerHTML = `<div class="handoff-grid">${block('用户目标', `<p>${escapeHtml(card.user_goal || '未明确')}</p>`)}${block('已核实信息', dict(card.verified_information))}${block('授权范围', dict(card.authorization_scope))}${block('已执行步骤', list((card.executed_steps || []).map(item => `${item.step_id || '步骤'} · ${item.status || '未知'}`)))}${block('业务与交易标识', dict(card.business_identifiers))}${block('待处理事项', list(card.pending_items, '由人工确认下一步'))}${block('必要上下文（已脱敏）', dict(card.redacted_context))}</div>`;
}

function renderFraudWarning() {
  const warning = currentDisplay?.warning;
  const alert = $('#elderFraudAlert');
  alert.classList.toggle('hidden', !(elderMode && warning));
  if (!warning) return;
  $('#elderFraudTitle').textContent = warning.title;
  $('#elderFraudExplanation').textContent = warning.explanation;
  $('#elderOfficialHelp').textContent = warning.official_help;
  $('#elderHotlineHelp').textContent = warning.hotline_help;
}
function renderCurrentDisplay() {
  if (!currentDisplay) return;
  const view = elderMode ? currentDisplay.plain : currentDisplay.standard;
  if (view.plan_view) renderPlan(view.plan_view, view.result_view || null);
  renderFraudWarning();
}
function receiveAgentResponse(data, updatePlan=true, speak=true, allowDialogs=true) {
  if(data.business_task)refreshBusinessTasks();
  renderHandoffCard(data.handoff_card || data.presentation?.standard?.handoff_card || null);
  const standard = data.presentation?.standard || data;
  const plain = data.presentation?.plain || data;
  if (standard.message) addMessage(standard.message, 'bot', plain.message);
  if (standard.result_view?.user_message && standard.result_view.user_message !== standard.message)
    addMessage(standard.result_view.user_message, 'bot', plain.result_view?.user_message);
  if (!updatePlan || data.error_code === 'AUTH_FAILED') return;
  currentDisplay = {standard, plain, warning:data.presentation?.fraud_warning || null};
  if (standard.plan_view) renderCurrentDisplay();
  else if (data.blocked || data.ok === false) {
    currentPlan = null; currentResultView = null;
    $('#authModal').classList.add('hidden');
    $('#planPanel').innerHTML = '<div class="empty-state"><h3>这次没有办理</h3><p>请查看左侧说明，或点击“一键人工客服”。</p></div>';
  }
  renderFraudWarning();
  if (speak) announceA11y(BankSpeech.briefResponse(data));
  if (elderMode && currentDisplay.warning) $('#elderFraudAlert').focus();
  if (data.awaiting_details && data.transfer_draft) {
    transferDraft = data.transfer_draft;
    if (allowDialogs && !data.conversation_followup) openTransferDialog(transferDraft);
  }
  if(standard.plan_view && dynamicTaskViews.has(standard.plan_view.plan_id))refreshDynamicTask(standard.plan_view.plan_id);
}
async function postAgent(path, body) {
  if(identityMode==='SELF_HOSTED'){body={...body};delete body.user_id;delete body.device_id;}
  const response = await fetch(path, {method:'POST', headers:{...demoSessionHeaders, 'Content-Type':'application/json'}, body:JSON.stringify({...body, elder_mode:elderMode})});
  return response.json();
}
async function requestCustomerService() {
  if (supportBusy) return;
  supportBusy = true;
  updateSupportControls();
  const buttons = [$('#customerServiceBtn'), $('#fraudHelpBtn')];
  buttons.forEach(b => { b.disabled = true; });
  $('#customerServiceStatus').textContent = '正在提交客服请求，请稍等；尚未接通真人客服。';
  try {
    // Keep the retry key across page reloads; a timeout must not create a second request.
    if (!supportRequestId) {
      try { supportRequestId = sessionStorage.getItem('supportRequestId'); } catch (_) {}
      if (!supportRequestId) supportRequestId = 'help_' + Array.from(crypto.getRandomValues(new Uint8Array(16)), n => n.toString(16).padStart(2,'0')).join('');
      try { sessionStorage.setItem('supportRequestId', supportRequestId); } catch (_) {}
    }
    const data = await postAgent('/api/v1/customer-service', {request_id:supportRequestId, user_id:'demo-user', device_id:'demo-device'});
    if (!data.ok || data.status !== 'LOCAL_QUEUED') throw new Error(data.message || '无法确认客服请求是否已记录。');
    $('#customerServiceStatus').textContent = data.message;
    $('#customerServiceStatus').dataset.eventId = data.event_id;
    if (data.handoff_case) showLocalHandoffStatus(data.handoff_case);
    receiveAgentResponse(data, false);
  } catch (_) {
    $('#customerServiceStatus').textContent = '暂时无法确认客服请求是否已记录。您可以再次点击重试，或通过银行卡背面、银行官方 App 联系客服。';
  } finally {
    supportBusy = false;
    buttons.forEach(b => { b.disabled = false; });
    updateSupportControls();
  }
}

function showLocalHandoffStatus(item) {
  const status = $('#customerServiceStatus');
  status.dataset.caseId = item.case_id;
  supportCaseStatus = item.status;
  updateSupportControls();
  const states = {LOCAL_QUEUED:'本地工单待领取', LOCAL_CLAIMED:'本地演示坐席已领取', LOCAL_CLOSED:'本地工单已记录处理结论'};
  const conclusions = {GUIDANCE_PROVIDED:'已记录操作指引', REFERRED_OFFICIAL:'请联系银行官方客服', NEEDS_RECONCILIATION:'交易仍需核对，不能视为成功'};
  status.textContent = `${states[item.status] || '工单状态待核对'}${item.resolution ? '：'+(conclusions[item.resolution] || '待核对') : ''}。银行官方：尚未接通真人客服；下方仅为自建演示会话，领取或结单不代表银行交易完成。`;
  ensureLiveSupport(item.case_id);
  let refresh = $('#handoffStatusRefresh');
  if (!refresh) {
    refresh = document.createElement('button');
    refresh.id = 'handoffStatusRefresh';
    refresh.type = 'button';
    refresh.className = 'customer-service';
    refresh.textContent = '查询本地接管进度';
    status.insertAdjacentElement('afterend', refresh);
    refresh.addEventListener('click', async () => {
      refresh.disabled = true;
      try {
        const data = await postAgent('/api/v1/handoff/status', {case_id:status.dataset.caseId});
        if (!data.ok) throw new Error('unavailable');
        showLocalHandoffStatus(data.case);
      } catch (_) {
        status.textContent = '暂时无法核对本地接管进度；未自动重做业务，请稍后查询或联系官方客服。';
        supportCaseStatus = null; updateSupportControls();
      } finally { refresh.disabled = false; }
    });
  }
}

function livePendingKey(){return 'supportPending:'+liveSupportCase;}
function loadLivePending(){try{const p=JSON.parse(sessionStorage.getItem(livePendingKey())||'null');return p&&typeof p.id==='string'&&typeof p.hash==='string'?p:null;}catch(_){return null;}}
function clearLivePending(){liveSupportPending=null;try{sessionStorage.removeItem(livePendingKey());}catch(_){}}
async function supportTextHash(text){return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(text))),n=>n.toString(16).padStart(2,'0')).join('');}
function renderLiveMessages(rows,peerUpto){
  for(const message of liveSupportMessages.values())if(message.sender_role==='user')message.peer_displayed=message.seq<=peerUpto;
  for(const message of rows){liveSupportMessages.set(message.seq,message);if(liveSupportPending&&message.sender_role==='user'&&message.client_message_id===liveSupportPending.id)clearLivePending();}
  const list=$('#liveSupportMessages');list.replaceChildren();
  for(const message of [...liveSupportMessages.values()].sort((a,b)=>a.seq-b.seq)){
    const row=document.createElement('li');row.style.cssText='margin:10px 0;padding:10px;background:#fff;border-radius:8px;overflow-wrap:anywhere';
    const who=document.createElement('strong');who.textContent=message.sender_role==='user'?'你':'自建坐席';const text=document.createElement('p');text.style.whiteSpace='pre-wrap';text.textContent=message.text;
    const receipt=document.createElement('small');receipt.textContent=message.peer_displayed?'对方页面已显示（不代表已阅读或核账）':'服务器已保存，未核对对方显示';row.append(who,text,receipt);list.append(row);
  }
}
async function pollLiveSupport(){
  if(!liveSupportCase||liveSupportPolling)return;liveSupportPolling=true;const caseId=liveSupportCase;
  try{
    let data;
    do{
      data=await postAgent('/api/v1/support/poll',{case_id:caseId,after_seq:liveSupportCursor});if(!data.ok)throw Error(data.message||'对话暂不可核验');if(liveSupportCase!==caseId)return;
      renderLiveMessages(data.messages,data.peer_displayed_upto);liveSupportCursor=data.next_cursor;
      if(data.messages.length){const ack=await postAgent('/api/v1/support/ack',{case_id:caseId,upto_seq:data.next_cursor});if(!ack.ok)throw Error('页面显示回执暂未确认');}
    }while(data.more);
    liveSupportClosed=data.case_status==='LOCAL_CLOSED';$('#liveSupportSend').disabled=liveSupportClosed;
    $('#liveSupportConnection').textContent=liveSupportClosed?'本工单已结单，可查看历史；新问题请发起新一轮求助。':data.operator_page_online?'坐席页面在线，可交流；不是银行官方客服。':'消息可先保存，尚未核对到在线坐席页面。';
  }catch(e){if(liveSupportCase===caseId)$('#liveSupportConnection').textContent='对话连接未确认：'+e.message+'；未重做任何银行业务。';}
  finally{liveSupportPolling=false;}
}
function ensureLiveSupport(caseId){
  if(!caseId||liveSupportCase===caseId)return;
  clearInterval(liveSupportTimer);liveSupportCase=caseId;liveSupportCursor=0;liveSupportClosed=false;liveSupportMessages.clear();liveSupportPending=loadLivePending();
  let panel=$('#liveSupportPanel');if(!panel){panel=document.createElement('section');panel.id='liveSupportPanel';panel.style.cssText='margin:14px 0;padding:14px;background:#edf4ec;border:1px solid #c7d8c8;border-radius:12px;max-width:100%;box-sizing:border-box';$('#customerServiceStatus').insertAdjacentElement('afterend',panel);}
  panel.innerHTML='<h3>与自建坐席交流</h3><p id="liveSupportConnection" role="status" aria-live="polite">正在查询对话</p><ul id="liveSupportMessages" style="list-style:none;padding:0;max-height:300px;overflow:auto" aria-live="polite"></ul><label for="liveSupportText">仅发送核对问题，不提供密码、验证码、密钥或账号</label><textarea id="liveSupportText" maxlength="2000" style="width:100%;min-height:90px;box-sizing:border-box"></textarea><button id="liveSupportSend" type="button">发送给坐席</button><button id="liveSupportRefresh" type="button">刷新对话</button><p><small>此处仅交流，不办理交易。业务操作仍需返回助手核对计划并确认。<a href="/support/operator" target="_blank" rel="noopener">坐席工作台（需运维会话）</a></small></p>';
  $('#liveSupportRefresh').addEventListener('click',pollLiveSupport);
  $('#liveSupportSend').addEventListener('click',async()=>{
    const text=$('#liveSupportText').value.trim();if(!text||liveSupportClosed)return;const button=$('#liveSupportSend');button.disabled=true;const selectedCase=liveSupportCase;
    try{
      const hash=await supportTextHash(text);if(liveSupportPending&&liveSupportPending.hash!==hash)throw Error('上一条结果未知，请先刷新核对，或保留原文重试');
      if(!liveSupportPending){liveSupportPending={id:'user_'+Array.from(crypto.getRandomValues(new Uint8Array(16)),n=>n.toString(16).padStart(2,'0')).join(''),hash};try{sessionStorage.setItem(livePendingKey(),JSON.stringify(liveSupportPending));}catch(_){}}
      const result=await postAgent('/api/v1/support/send',{case_id:selectedCase,message_id:liveSupportPending.id,text});if(!result.ok)throw Error(result.message||'发送未确认');if(selectedCase!==liveSupportCase)return;
      clearLivePending();$('#liveSupportText').value='';await pollLiveSupport();
    }catch(e){$('#liveSupportConnection').textContent='发送未确认，原消息编号保留：'+e.message;}
    finally{button.disabled=liveSupportClosed;}
  });
  pollLiveSupport();liveSupportTimer=setInterval(()=>{if(document.visibilityState==='visible')pollLiveSupport();},2500);
}
window.addEventListener('pagehide',()=>clearInterval(liveSupportTimer));

async function openTransferDialog(draft = {}) {
  draft = draft || {};
  const dialog = $('#transferDialog');
  $('#transferReview').disabled = true;
  $('#transferRecipient').innerHTML = '<option value="">请选择收款人和账户</option>';
  const cents = draft.amount_cents;
  $('#transferAmount').value = Number.isSafeInteger(cents) && cents > 0
    ? `${Math.trunc(cents / 100)}.${String(cents % 100).padStart(2, '0')}` : '';
  $('#transferNote').value = draft.note || '';
  $('#transferRequestHint').textContent = '正在读取已核验的演示账户…';
  $('#recipientDisambiguation').classList.add('hidden');
  $('#recipientDisambiguation').textContent = '';
  if (!dialog.open) dialog.showModal();
  try {
    const response = await fetch('/api/state', {headers:demoSessionHeaders});
    const state = await response.json();
    if (!response.ok || !state.ok || !Array.isArray(state.transfer_recipients)) throw new Error();
    const recipients = state.transfer_recipients;
    const candidates = Array.isArray(state.transfer_recipient_candidates) ? state.transfer_recipient_candidates : [];
    if (draft.beneficiary && candidates.filter(candidate => candidate.name === draft.beneficiary).length > 1) {
      const matches = candidates.filter(candidate => candidate.name === draft.beneficiary);
      $('#recipientDisambiguation').innerHTML = `<strong>找到 ${matches.length} 位同名收款人，请补充手机号后四位：</strong><ul>${matches.map(candidate => `<li>${escapeHtml(candidate.name)} · ${escapeHtml(candidate.account_label)}</li>`).join('')}</ul>`;
      $('#recipientDisambiguation').classList.remove('hidden');
    }
    for (const recipient of recipients) {
      const option = document.createElement('option');
      option.value = recipient.name;
      option.textContent = `${recipient.name} · 手机号映射账户 ${recipient.account_label}`;
      $('#transferRecipient').appendChild(option);
    }
    const selected = !draft.account_requires_verification && recipients.some(r => r.name === draft.beneficiary);
    $('#transferRecipient').value = selected ? draft.beneficiary : '';
    $('#transferRequestHint').textContent = draft.account_requires_verification
      ? '你提供的账户号码尚未核验，不会自动替换为同名联系人的其他账户。当前只能由你重新选择演示账户。'
      : draft.beneficiary && !selected
      ? `你提到的“${draft.beneficiary}”尚未绑定已核验账户，不会自动替换为其他收款人。`
      : '收款人和金额由你决定。下一步仍需核对计划并确认，必要时完成身份验证。';
    $('#transferReview').disabled = recipients.length === 0;
    announceA11y($('#transferRequestHint').textContent, false);
  } catch (_) {
    $('#transferRequestHint').textContent = '暂时无法读取收款账户，请稍后重试。尚未创建转账，也没有扣款。';
    announceA11y($('#transferRequestHint').textContent, false);
  }
}
$('#transferClose').addEventListener('click',()=>$('#transferDialog').close());
$('#transferForm').addEventListener('submit',event=>{
  event.preventDefault();
  if (busy || $('#transferReview').disabled) return;
  const amount = $('#transferAmount').value.trim();
  const recipient = $('#transferRecipient').value;
  // Keep decimal input as text; the trusted parser converts it to integer cents.
  if (!recipient || !/^\d+(?:\.\d{1,2})?$/.test(amount) || !/[1-9]/.test(amount)) {
    $('#transferRequestHint').textContent = '请选择收款账户，并输入大于 0、最多两位小数的金额。';
    announceA11y($('#transferRequestHint').textContent, false);
    return;
  }
  const note = $('#transferNote').value.trim();
  $('#transferDialog').close();
  send(`给${recipient}转${amount}元${note ? '，备注' + note : ''}`);
});

async function send(msg, {openTransferForm = false, displayText = msg, speechLabel = ''} = {}) {
  if (!msg || busy) return;
  const startedInFocus = $('#focusDialog').open;
  speechPlayer.stop();
  if (speechLabel) announceA11y(speechLabel);
  addMessage(displayText,'user'); $('#messageInput').value=''; setBusy(true);
  try {
    const ocrToken=ocrReviewToken;ocrReviewToken=null;
    const voiceToken=voiceSourceToken;voiceSourceToken=null;
    const d=await postAgent('/api/chat', {message:msg,channel:'web',user_id:'demo-user',conversation_id:conversationId,
      ...(ocrToken ? {ocr_review_token:ocrToken} : {}),
      ...(voiceToken ? {voice_source_token:voiceToken,voice_reviewed:true} : {})});
    $('#traceLabel').textContent=d.trace_id?'本次服务已记录':'已拦截';
    const stillFocused = !startedInFocus || $('#focusDialog').open;
    receiveAgentResponse(d, true, (!speechLabel || d.ok === false || d.blocked === true) && stillFocused, stillFocused);
    if (openTransferForm && d.conversation_followup && (!startedInFocus || $('#focusDialog').open)) await openTransferDialog(d.transfer_draft);
    if(d.blocked) showToast('这次没有办理，请查看说明');
    await refreshState();
  } catch(e) {
    addMessage('现在连接不上银行小助手，请检查服务是否已启动。'); announceA11y('连接失败'); showToast('连接失败');
    if (startedInFocus && !currentPlan) $('#planPanel').innerHTML = '<div class="empty-state"><h3>连接失败</h3><p>暂时未取得办理结果。请稍后重试。</p></div>';
  }
  finally { setBusy(false); }
}
async function confirmPlan() {
  if(!currentPlan || busy) return; setBusy(true);
  const startedInFocus = focusDialog.open;
  try {
    const d=await postAgent('/api/confirm', {plan_id:currentPlan.plan_id,action_token:currentPlan.action_token});
    receiveAgentResponse(d);
    if(d.needs_auth){if(startedInFocus && !focusDialog.open)return;if(d.auth_challenge?.method==='totp')$('#authDescription').textContent='为保障您的账户安全，请输入身份验证器中未使用的动态码。请勿向他人提供动态码。验证通过后，请核对办理内容并确认提交。'; $('#authModal').classList.remove('hidden'); $('#otpInput').focus(); return; }
    await refreshState();
  } catch(e){ announceA11y('结果待核实，请勿重复办理'); showToast('暂时无法确认办理结果，请先查看服务记录，不要重复转账'); } finally { setBusy(false); }
}
async function cancelPlan() {
  if(!currentPlan || busy) return; setBusy(true);
  try {
    const d=await postAgent('/api/cancel', {plan_id:currentPlan.plan_id,action_token:currentPlan.action_token});
    receiveAgentResponse(d);
    if (d.ok) {
      $('#planPanel').innerHTML='<div class="empty-state"><div class="orbit">✓</div><h3>这次没有办理</h3><p>你可以继续告诉我下一件事。</p></div>';
      currentPlan=null; currentResultView=null; currentDisplay=null; renderFraudWarning();
    }
  } catch(e){showToast('取消结果尚未确认，请查看服务记录');} finally{setBusy(false);}
}
async function verify() {
  if(!currentPlan || busy) return;
  const btn=$('#verifyBtn'); btn.disabled=true; setBusy(true);
  try {
    const d=await postAgent('/api/verify', {plan_id:currentPlan.plan_id,action_token:currentPlan.action_token,code:$('#otpInput').value});
    receiveAgentResponse(d);
    if(d.ok){ $('#authModal').classList.add('hidden'); $('#otpInput').value=''; showToast('身份验证已通过，请核对办理内容并确认提交'); }
    else if(d.breaker?.locked){ showToast('安全保护已锁定，请联系人工客服'); }
  } catch(e) { announceA11y('暂时无法验证身份'); showToast('暂时无法核对身份，请稍后重试或联系人工客服'); }
  finally { btn.disabled=false; setBusy(false); }
}
async function refreshState(){
  try {
    const response = await fetch('/api/state', {headers:demoSessionHeaders});
    const d = await response.json();
    if (!response.ok || !d.ok) { displayCards = null; accountSnapshot={}; renderAccountAmounts(); renderCardDisplay(); return; }
    displayCards = Array.isArray(d.cards?.items) ? d.cards.items : null;
    renderCardDisplay();
    accountSnapshot=d;renderAccountAmounts();
    $('#demoOtpHint').textContent = d.auth_method==='totp' ? '请使用身份验证器中下一枚未使用的动态码。' : d.demo_otp ? `演示验证码：${d.demo_otp}` : '请向演示管理员获取验证码';
  } catch(e){ displayCards = null; accountSnapshot={}; renderAccountAmounts(); renderCardDisplay(); }
}
async function loadCapabilities(){ try{ const d=await (await fetch('/api/capabilities')).json(); const c=d.capabilities||{}; $('#modelMode').textContent=c.llm_enabled?'已接入可选模型':'离线规则模式'; $('#modeDetail').textContent=c.llm_enabled?`已启用 ${c.model_mode}，模型只帮助理解你的话`:'不需要 API Key 也可以完成演示'; }catch(e){} }

// Microphone is an explicit, optional cloud input channel, never an action tool.
let voiceGeneration = 0, voiceSession = null, voiceController = null;
let voiceAvailable = false, voicePending = false, voiceCloudEnabled = false;
const voiceDialog = $('#voiceDialog');
let lastCloudVoiceTrace=null;
const voiceProviderInfo=document.createElement('details');
const voiceProviderSummary=document.createElement('summary');voiceProviderSummary.textContent='供应商处理与删除说明';
const voiceProviderText=document.createElement('p');voiceProviderText.textContent='供应商声明推理后销毁业务数据；未找到逐条ASR录音删除接口。音色删除接口不适用于ASR。';
const voicePolicyLink=document.createElement('a');voicePolicyLink.href='https://docs.siliconflow.cn/docs/legals/privacy-policy';voicePolicyLink.target='_blank';voicePolicyLink.rel='noopener noreferrer';voicePolicyLink.textContent='查看供应商隐私政策';
const voiceDeletionButton=document.createElement('button');voiceDeletionButton.type='button';voiceDeletionButton.className='ghost';voiceDeletionButton.textContent='生成上次云识别的处理/删除申请草稿（不发送）';voiceDeletionButton.disabled=true;
const voiceDeletionDraft=document.createElement('textarea');voiceDeletionDraft.readOnly=true;voiceDeletionDraft.hidden=true;voiceDeletionDraft.setAttribute('aria-label','供应商申请草稿，尚未发送');
voiceProviderInfo.append(voiceProviderSummary,voiceProviderText,voicePolicyLink,voiceDeletionButton,voiceDeletionDraft);voiceDialog.append(voiceProviderInfo);
voiceDeletionButton.addEventListener('click',async()=>{
  if(!lastCloudVoiceTrace)return;
  try{
    const r=await fetch('/api/v1/voice/deletion-request',{method:'POST',headers:{...demoSessionHeaders,'Content-Type':'application/json'},body:JSON.stringify({trace_id:lastCloudVoiceTrace})});
    const d=await r.json();if(!r.ok || !d.ok)throw new Error(d.message || '申请草稿不可用');
    voiceDeletionDraft.value=d.draft || d.message;voiceDeletionDraft.hidden=false;
    $('#voiceDialogStatus').textContent='仅生成申请草稿，尚未发送给供应商，尚未获得删除确认。';
  }catch(e){$('#voiceDialogStatus').textContent=e.message;}
});
function clearVoiceSamples(session) {
  if (!session) return;
  for (const chunk of session.chunks) chunk.fill(0);
  session.chunks.length=0;session.frames=0;
}
function releaseMicrophone(session) {
  if (!session) return;
  clearTimeout(session.timer);
  session.processor.onaudioprocess = null;
  session.processor.disconnect(); session.source.disconnect(); session.gain.disconnect();
  session.stream.getTracks().forEach(track => track.stop());
  session.context.close().catch(() => {});
}
function cancelVoice() {
  voiceGeneration++;
  releaseMicrophone(voiceSession);clearVoiceSamples(voiceSession);voiceSession = null;
  voiceController?.abort(); voiceController = null; voicePending = false;
  voiceDialog.close(); $('#voiceBtn').focus();
}
async function startVoiceInput() {
  if (busy) return;
  const generation = ++voiceGeneration;
  $('#voiceConsent').checked = false; $('#voiceConsent').disabled = false; $('#voiceStart').disabled = true;
  $('#voiceStart').classList.remove('hidden'); $('#voiceStop').classList.add('hidden');
  $('#voiceDialogStatus').textContent = '正在检查语音服务…';
  voiceAvailable = false;voiceCloudEnabled=false; voiceDialog.showModal();
  try {
    const response = await fetch('/api/v1/voice/capabilities');
    const caps = await response.json();
    if (generation !== voiceGeneration) return;
    voiceCloudEnabled=caps.enabled===true;
    voiceAvailable = response.ok && (voiceCloudEnabled || caps.evidence_enabled===true) && !!navigator.mediaDevices?.getUserMedia && !!(window.AudioContext || window.webkitAudioContext);
    $('#voiceConsentText').textContent=voiceCloudEnabled?'我同意本次录音在本机处理，并将去元数据后的录音发送至硅基流动识别':'我同意本次录音仅在本机计算音频摘要，不发送云端';
    $('#voiceDialogStatus').textContent = voiceAvailable ? (voiceCloudEnabled?'准备好了。勾选同意后开始，最长录制 30 秒。':'离线音频证据模式：只计算摘要，不转写、不发送云端。最长30秒。') : '云端语音未启用，或浏览器不支持录音。请直接打字；管理员可按大模型接入说明配置。';
    $('#voiceStart').disabled = !voiceAvailable || !$('#voiceConsent').checked;
  } catch (_) { if (generation === voiceGeneration) $('#voiceDialogStatus').textContent = '语音服务暂不可用，请直接打字。'; }
}
function encodeVoiceWav(chunks, rate) {
  const size = chunks.reduce((n,c)=>n+c.length,0);
  const samples = new Float32Array(size); let offset=0;
  for (const chunk of chunks) { samples.set(chunk,offset); offset+=chunk.length; }
  const count=Math.floor(size*16000/rate), buffer=new ArrayBuffer(44+count*2), view=new DataView(buffer);
  const text=(at,value)=>{for(let i=0;i<value.length;i++)view.setUint8(at+i,value.charCodeAt(i));};
  text(0,'RIFF');view.setUint32(4,36+count*2,true);text(8,'WAVE');text(12,'fmt ');
  view.setUint32(16,16,true);view.setUint16(20,1,true);view.setUint16(22,1,true);
  view.setUint32(24,16000,true);view.setUint32(28,32000,true);view.setUint16(32,2,true);view.setUint16(34,16,true);
  text(36,'data');view.setUint32(40,count*2,true);
  for(let i=0;i<count;i++){
    const start=Math.floor(i*rate/16000),end=Math.min(size,Math.max(start+1,Math.floor((i+1)*rate/16000)));
    let sum=0;for(let j=start;j<end;j++)sum+=samples[j];
    const sample=Math.max(-1,Math.min(1,sum/(end-start)));
    view.setInt16(44+i*2,Math.round(sample*(sample<0?32768:32767)),true);
  }
  samples.fill(0);
  return new Blob([buffer],{type:'audio/wav'});
}
async function beginRecording() {
  if (!voiceAvailable || !$('#voiceConsent').checked || voicePending || voiceSession) return;
  const generation=voiceGeneration, originalText=$('#messageInput').value;
  voicePending=true; $('#voiceStart').disabled=true; $('#voiceConsent').disabled=true;
  $('#voiceDialogStatus').textContent='请允许浏览器使用麦克风…';
  let stream, context;
  try {
    context=new (window.AudioContext || window.webkitAudioContext)();
    await context.resume();
    stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true}});
    if(generation!==voiceGeneration){stream.getTracks().forEach(t=>t.stop());await context.close();return;}
    const source=context.createMediaStreamSource(stream),processor=context.createScriptProcessor(4096,1,1),gain=context.createGain();
    gain.gain.value=0; // Capture without feeding the microphone back to speakers.
    const session={stream,context,source,processor,gain,chunks:[],frames:0,originalText,generation,cloudConsent:voiceCloudEnabled};
    voiceSession=session;
    processor.onaudioprocess=event=>{
      if(voiceSession!==session)return;
      const chunk=event.inputBuffer.getChannelData(0);
      const remaining=Math.max(0,Math.floor(context.sampleRate*30)-session.frames);
      const copy=new Float32Array(chunk.subarray(0,remaining));
      session.chunks.push(copy);session.frames+=copy.length;
      $('#voiceDialogStatus').textContent=`正在聆听 · ${Math.floor(session.frames/context.sampleRate)} / 30 秒。说完请点“完成录音”。`;
      if(!remaining || session.frames>=context.sampleRate*30)finishRecording();
    };
    source.connect(processor);processor.connect(gain);gain.connect(context.destination);
    session.timer=setTimeout(finishRecording,30000);
    $('#voiceStart').classList.add('hidden');$('#voiceStop').classList.remove('hidden');$('#voiceStop').disabled=false;
    $('#voiceDialogStatus').textContent='正在聆听，请说出您的需求…';$('#voiceStop').focus();
  } catch (_) {
    stream?.getTracks().forEach(t=>t.stop());context?.close().catch(()=>{});
    if(generation===voiceGeneration){$('#voiceDialogStatus').textContent='无法使用麦克风。请检查浏览器权限，或直接打字。';$('#voiceStart').disabled=false;}
  } finally { if(generation===voiceGeneration){voicePending=false;$('#voiceConsent').disabled=false;} }
}
async function finishRecording() {
  const session=voiceSession;if(!session)return;
  voiceSession=null;releaseMicrophone(session);
  $('#voiceStop').disabled=true;$('#voiceDialogStatus').textContent='录音已结束，正在识别。尚未发送银行操作…';
  voicePending=true;voiceController=new AbortController();
  const controller=voiceController,timer=setTimeout(()=>controller.abort(),25000);
  let audioBlob=null;
  try {
    audioBlob=encodeVoiceWav(session.chunks,session.context.sampleRate);clearVoiceSamples(session);
    const response=await fetch('/api/v1/voice/transcribe',{method:'POST',headers:{...demoSessionHeaders,'Content-Type':'audio/wav','X-Audio-Consent':'yes',...(session.cloudConsent?{'X-Audio-Cloud-Consent':'yes'}:{})},body:audioBlob,signal:controller.signal});
    const result=await response.json();if(session.generation!==voiceGeneration)return;
    if(result.provider_receipt?.request_id && result.trace_id){lastCloudVoiceTrace=result.trace_id;voiceDeletionButton.disabled=false;}
    if(response.ok && result.ok && result.mode==='local_evidence_only'){
      $('#voiceDialogStatus').textContent=`本机音频摘要已生成：${String(result.audio_evidence?.upload_sha256 || '').slice(0,12)}…；采样缓冲已清理。未发送云端、未转写、未办理业务。`;
      $('#voiceStatus').textContent='离线音频证据已生成，未转写。请手工填写需求。';
      return;
    }
    if(!response.ok || !result.ok || typeof result.text!=='string')throw new Error('asr_failed');
    if($('#messageInput').value!==session.originalText)throw new Error('draft_changed');
    $('#messageInput').value=result.text;
    voiceSourceToken=result.voice_source_token || null;ocrReviewToken=null;
    $('#voiceStatus').textContent='语音已转为文字。请核对金额、收款人，修改无误后再点发送。';
    voiceDialog.close();$('#messageInput').focus();
  } catch (_) {
    if(session.generation===voiceGeneration)$('#voiceDialogStatus').textContent='没有完成识别，或输入文字已被修改。已保留原输入，请关闭后打字或重新录音。';
  } finally {
    clearVoiceSamples(session);audioBlob=null;
    clearTimeout(timer);
    if(session.generation===voiceGeneration){voicePending=false;voiceController=null;}
  }
}
$('#voiceConsent').addEventListener('change',()=>{
  if(!$('#voiceConsent').checked && (voicePending || voiceSession)){cancelVoice();return;}
  $('#voiceStart').disabled=!voiceAvailable || !$('#voiceConsent').checked || voicePending;
});
$('#voicePrivacyToggle').addEventListener('change', event=>{ voicePrivacy=event.target.checked; savePreference('voicePrivacy',String(voicePrivacy)); applyVoicePrivacy(); announceA11y(voicePrivacy?'已开启语音隐私保护，金额和账号不会被朗读。':'已关闭语音隐私保护，请确认周围环境安全。', false); });
$('#voiceStart').addEventListener('click',beginRecording);
$('#voiceStop').addEventListener('click',finishRecording);
$('#voiceCancel').addEventListener('click',cancelVoice);
voiceDialog.addEventListener('cancel',e=>{e.preventDefault();cancelVoice();});
window.addEventListener('pagehide',cancelVoice);

$('#composer').addEventListener('submit',e=>{e.preventDefault();send($('#messageInput').value.trim());});
document.querySelectorAll('.quick').forEach(b=>b.addEventListener('click',()=>{
  if (busy) return;
  openFocus(b);
  if (!focusDialog.open) $('#assistant').scrollIntoView({block:'start'});
  send(b.dataset.msg, {openTransferForm:b.dataset.msg === '我要转账', speechLabel:b.querySelector('b').textContent.trim()});
}));
$('#elderToggle').addEventListener('click',()=>{ elderMode=!elderMode; savePreference('elderMode',String(elderMode)); applyElderMode(); });
$('#customerServiceBtn').addEventListener('click', requestCustomerService);
$('#newSupportRequestBtn').addEventListener('click',()=>{
  if (supportBusy || supportCaseStatus !== 'LOCAL_CLOSED') return;
  // Only an explicitly closed local case enables a new round; unknown replies retry.
  supportRequestId = 'help_' + Array.from(crypto.getRandomValues(new Uint8Array(16)), n=>n.toString(16).padStart(2,'0')).join('');
  try { sessionStorage.setItem('supportRequestId',supportRequestId); } catch (_) {}
  supportCaseStatus = null;
  delete $('#customerServiceStatus').dataset.caseId;
  delete $('#customerServiceStatus').dataset.eventId;
  $('#handoffStatusRefresh')?.remove();
  $('#handoffCardPanel').classList.add('hidden');
  updateSupportControls(); requestCustomerService();
});
$('#settingsBtn').addEventListener('click',openSettings);
$('#mobileSettings').addEventListener('click',openSettings);
$('#settingsClose').addEventListener('click',closeSettings);
$('#settingsDialog').addEventListener('cancel',restoreSettingsFocus);
$('#showHiddenCards').addEventListener('change',renderCardDisplay);
$('#settingsElderToggle').addEventListener('change',e=>{elderMode=e.target.checked;savePreference('elderMode',String(elderMode));applyElderMode();});
$('#settingsVoicePrivacy').addEventListener('change',e=>{voicePrivacy=e.target.checked;savePreference('voicePrivacy',String(voicePrivacy));applyVoicePrivacy();});
document.querySelectorAll('[data-settings-target]').forEach(button=>button.addEventListener('click',()=>{
  const target=$('#'+button.dataset.settingsTarget); closeSettings();
  if (!document.body.classList.contains('show-technical')) $('#advancedToggle').click();
  target.tabIndex=-1;target.scrollIntoView({block:'start'});target.focus();
}));
$('#settingsVoiceBtn').addEventListener('click',()=>{closeSettings();startVoiceInput();});
$('#settingsAuditBtn').addEventListener('click',()=>{closeSettings();$('#auditBtn').click();});
$('#settingsSupportBtn').addEventListener('click',()=>{closeSettings();requestCustomerService();});
$('#fraudHelpBtn').addEventListener('click', requestCustomerService);
$('#advancedToggle').addEventListener('click', () => {
  const expanded = document.body.classList.toggle('show-technical');
  $('#advancedToggle').setAttribute('aria-expanded', String(expanded));
  $('#advancedToggle').textContent = expanded ? '收起服务记录与安全演示' : '查看服务记录与安全演示';
});
$('#voiceBtn').addEventListener('click',startVoiceInput);
$('#closeModal').addEventListener('click',()=>$('#authModal').classList.add('hidden')); $('#cancelAuth').addEventListener('click',()=>$('#authModal').classList.add('hidden')); $('#verifyBtn').addEventListener('click',verify); $('#refreshState').addEventListener('click',refreshState);
$('#resetBtn').addEventListener('click',async()=>{
  try { const d=await postAgent('/api/reset-demo', {}); showToast(d.ok?'安全状态已重置':(d.message||'请联系受信人工运维人员')); if(d.ok) refreshState(); }
  catch(e) { showToast('重置结果尚未确认，请联系人工运维人员'); }
});
async function loadAuditReplay(traceId) {
  const summary = $('#auditSummary');
  const list = $('#auditList');
  const value = String(traceId || '').trim();
  if (!value) { summary.textContent = '请先输入 trace_id。'; list.innerHTML = ''; return; }
  summary.textContent = '正在读取审计链…';
  const response = await fetch(`/api/v1/audit/events?trace_id=${encodeURIComponent(value)}`, {headers:demoSessionHeaders});
  const data = await response.json();
  if (!response.ok || !data.ok) { summary.textContent = data.message || '审计链读取失败。'; list.innerHTML = ''; return; }
  summary.textContent = data.count ? `共 ${data.count} 个事件，已按 seq 升序排列。` : '没有找到这个 trace_id 的审计事件。';
  list.innerHTML = (data.events || []).map((x, index) => `<div class="audit-item ${x.event_type === 'fraud.alert' ? 'audit-danger' : x.event_type === 'auth.challenged' ? 'audit-auth' : ''}"><div class="audit-event-title"><span class="audit-seq">${index + 1}</span><b>${escapeHtml(x.event_type)}</b></div><small>seq ${escapeHtml(String(x.seq ?? ''))} · ${escapeHtml(x.created_at || '')} · ${escapeHtml(x.risk_level || '')}</small><pre>${escapeHtml(JSON.stringify(x.payload || {}, null, 2))}</pre></div>`).join('') || '<p>暂无服务记录。</p>';
}
$('#auditBtn').addEventListener('click',()=>{ $('#auditDrawer').classList.remove('hidden'); const trace = currentPlan && currentPlan.trace_id; if (trace) { $('#auditTraceInput').value = trace; loadAuditReplay(trace); } else { $('#auditSummary').textContent = '请输入本次任务的 trace_id。'; $('#auditList').innerHTML = ''; } });
$('#auditReplayForm').addEventListener('submit', (event)=>{ event.preventDefault(); loadAuditReplay($('#auditTraceInput').value); });
$('#closeAudit').addEventListener('click',()=>$('#auditDrawer').classList.add('hidden'));
const now=new Date(); $('#todayLabel').textContent=`${now.getFullYear()}年${now.getMonth()+1}月${now.getDate()}日`; applyElderMode(); applyVoicePrivacy(); loadIdentity().finally(()=>{refreshState();loadCapabilities();});


// C6: only server-side isolated fixtures execute attacks; render all text as untrusted.
let attackMatrixReport = null;
let attackMatrixRunning = false;
const attackCategoryLabels = {
  prompt_injection: 'Prompt 注入', business_logic: '业务逻辑',
  authorization_bypass: '权限绕过', tool_overreach: '工具越权',
  data_leakage: '数据泄露', transaction_state_deception: '交易状态欺骗'
};
function renderAttackMatrix(rows, completed = false) {
  $('#attackMatrixBody').innerHTML = rows.map(row => {
    const verdict = !completed ? '待运行' : row.passed ? '已阻断 · 验证通过' : row.blocked ? '已阻断 · 证据未通过' : '未通过 · 需检查';
    const events = completed ? (row.actual_audit_events || []).map(escapeHtml).join('<br>') || '未记录（不通过）' : `预期：${escapeHtml(row.expected_audit_event)}`;
    const evidence = completed ? `<details><summary>查看证据与断言</summary><pre>${escapeHtml(JSON.stringify({trace_id: row.trace_id, checks: row.checks, response_evidence: row.response_evidence, transactions: row.transactions, audit_evidence: row.audit_evidence}, null, 2))}</pre></details>` : '';
    return `<tr><td><b>${escapeHtml(attackCategoryLabels[row.category] || row.category)}</b><small>${escapeHtml(row.id)}</small><div>${escapeHtml(row.attack_input)}</div></td><td>${escapeHtml(row.expected_behavior)}</td><td>${completed ? escapeHtml(row.actual_behavior) : '尚未执行'}${evidence}</td><td class="attack-events">${events}</td><td><span class="${!completed ? 'attack-pending' : row.passed ? 'attack-pass' : 'attack-fail'}">${verdict}</span></td></tr>`;
  }).join('');
}
async function loadAttackMatrix() {
  try {
    const response = await fetch('/api/v1/security/attack-matrix');
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.message || '矩阵加载失败');
    if (attackMatrixRunning || attackMatrixReport) return;
    renderAttackMatrix(data.attacks || []);
    $('#attackMatrixSummary').textContent = `已加载 ${data.attacks.length} 条攻击，尚未执行。点击“一键跑全部攻击”现场验证。`;
  } catch (error) {
    if (!attackMatrixRunning && !attackMatrixReport) $('#attackMatrixSummary').textContent = `加载失败：${error.message}。仍可点击按钮重新运行。`;
  }
}
async function runAttackMatrix() {
  if (attackMatrixRunning) return;
  attackMatrixRunning = true;
  attackMatrixReport = null;
  const button = $('#runAttackMatrixBtn');
  button.disabled = true;
  $('#downloadAttackReportBtn').disabled = true;
  $('#attackMatrixSummary').textContent = '正在隔离环境逐条执行攻击并校验账务及审计证据…';
  $('#attackMatrixBody').innerHTML = '<tr><td colspan="5">正在执行，请等待本次实际结果…</td></tr>';
  try {
    const response = await fetch('/api/v1/security/attack-matrix/run', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
    const data = await response.json();
    if (!response.ok || !Array.isArray(data.results)) throw new Error(data.message || '演练请求失败');
    attackMatrixReport = data;
    renderAttackMatrix(data.results, true);
    $('#attackMatrixSummary').textContent = `${data.passed}/${data.total} 项验证通过，${data.failed} 项未通过。${data.ok ? '全部攻击均已安全处理，并匹配预期审计事件。' : '发现未通过项，请展开证据检查，不能视为验收通过。'} 报告时间：${data.generated_at}`;
    $('#downloadAttackReportBtn').disabled = false;
  } catch (error) {
    $('#attackMatrixSummary').textContent = `演练失败：${error.message}。本次没有有效报告，请重试。`;
    $('#attackMatrixBody').innerHTML = '<tr><td colspan="5">执行失败，不展示旧结果或预设通过状态。</td></tr>';
  } finally {
    attackMatrixRunning = false;
    button.disabled = false;
  }
}
$('#runAttackMatrixBtn').addEventListener('click', runAttackMatrix);
$('#downloadAttackReportBtn').addEventListener('click', () => {
  if (!attackMatrixReport) return;
  const blob = new Blob([JSON.stringify(attackMatrixReport, null, 2)], {type: 'application/json;charset=utf-8'});
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url; link.download = 'c6-attack-matrix-report.json'; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
loadAttackMatrix();


// P04: only a human-reviewed draft can enter the existing composer/transfer form.
let ocrDraft = null;
const ocrFlagText = {low_confidence:'有低置信度文字，请逐项核对',amount_conflict:'识别到不同金额，请手工确定唯一金额',recipient_conflict:'识别到不同收款人，请手工确定',missing_fields:'缺少收款人或金额，请补填',account_requires_verification:'图片账户未核验；不得自动替换为同名演示账户，后续需重新选择',unsupported_currency:'当前仅支持人民币草稿，请改用手工官方渠道',invalid_amount:'金额格式未识别，请核对'};
function updateOcrButtons() {
  $('#ocrExtract').disabled = !$('#ocrFile').files.length || !$('#ocrConsent').checked;
  $('#ocrReview').disabled = !ocrDraft || !$('#ocrReviewed').checked || !$('#ocrRecipient').value.trim() || !$('#ocrAmount').value.trim() || (ocrDraft.flags.length>0 && !$('#ocrFlagsAck').checked);
}
$('#ocrBtn').addEventListener('click',async()=>{
  $('#ocrStatus').textContent='正在检查本地识别功能…';$('#ocrDialog').showModal();
  try { const r=await fetch('/api/v1/ocr/capabilities');const d=await r.json();$('#ocrStatus').textContent=d.enabled && d.assets_present?'请选择图片并确认本次本机识别。':'本地图片识别尚未配置，可先手工填写。'; } catch(_){$('#ocrStatus').textContent='暂时无法检查本地识别功能。';}
});
for (const id of ['ocrFile','ocrConsent','ocrReviewed','ocrFlagsAck','ocrRecipient','ocrAmount']) $('#'+id).addEventListener('input',updateOcrButtons);
$('#ocrFile').addEventListener('change',()=>{ocrDraft=null;$('#ocrResult').hidden=true;updateOcrButtons();});
$('#ocrClose').addEventListener('click',()=>$('#ocrDialog').close());
$('#ocrDialog').addEventListener('close',()=>{ocrDraft=null;$('#ocrFile').value='';$('#ocrConsent').checked=false;$('#ocrReviewed').checked=false;$('#ocrFlagsAck').checked=false;$('#ocrPreview').removeAttribute('src');$('#ocrBoxes').replaceChildren();$('#ocrLines').replaceChildren();$('#ocrRecipient').value='';$('#ocrAmount').value='';$('#ocrResult').hidden=true;updateOcrButtons();});
$('#ocrExtract').addEventListener('click',async()=>{
  const file=$('#ocrFile').files[0];if(!file || !$('#ocrConsent').checked)return;
  if(!['image/png','image/jpeg'].includes(file.type) || file.size>2*1024*1024){$('#ocrStatus').textContent='请选择不超过2MB的PNG或JPEG图片。';return;}
  $('#ocrExtract').disabled=true;ocrDraft=null;$('#ocrStatus').textContent='正在本机识别，请稍候…';
  try {
    const r=await fetch('/api/v1/ocr/extract',{method:'POST',headers:{...demoSessionHeaders,'Content-Type':file.type,'X-OCR-Consent':'yes'},body:file});const d=await r.json();
    if(!r.ok || !d.ok)throw new Error(d.reason || '识别失败');
    ocrDraft=d;$('#ocrResult').hidden=false;$('#ocrReviewed').checked=false;$('#ocrFlagsAck').checked=false;
    $('#ocrRecipient').value=d.fields.recipient || '';
    const cents=d.fields.amount_cents;$('#ocrAmount').value=Number.isSafeInteger(cents)?`${Math.trunc(cents/100)}.${String(cents%100).padStart(2,'0')}`:'';
    $('#ocrAckLabel').hidden=!d.flags.length;$('#ocrFlags').replaceChildren();$('#ocrLines').replaceChildren();$('#ocrBoxes').replaceChildren();
    $('#ocrBoxes').setAttribute('viewBox',`0 0 ${d.width} ${d.height}`);
    for(const flag of d.flags){const li=document.createElement('li');li.textContent=ocrFlagText[flag] || '请核对识别提示';$('#ocrFlags').append(li);}
    for(const line of d.lines){const li=document.createElement('li');li.textContent=`${line.text} · 置信度${Math.round(line.confidence*100)}%`;$('#ocrLines').append(li);const polygon=document.createElementNS('http://www.w3.org/2000/svg','polygon');polygon.setAttribute('points',line.box.map(p=>p.join(',')).join(' '));$('#ocrBoxes').append(polygon);}
    const reader=new FileReader();reader.onload=()=>{if($('#ocrDialog').open && ocrDraft===d)$('#ocrPreview').src=reader.result;};reader.readAsDataURL(file);
    $('#ocrStatus').textContent='请对照框选位置核对字段。识别和填写草稿都不会办理业务。';
  }catch(e){$('#ocrStatus').textContent=`未生成草稿：${e.message}。可修改图片或手工填写。`;}
  finally{updateOcrButtons();}
});
$('#ocrReview').addEventListener('click',async()=>{
  if($('#ocrReview').disabled || !ocrDraft)return;$('#ocrReview').disabled=true;
  try{
    const r=await fetch('/api/v1/ocr/review',{method:'POST',headers:{...demoSessionHeaders,'Content-Type':'application/json'},body:JSON.stringify({draft_id:ocrDraft.draft_id,recipient:$('#ocrRecipient').value.trim(),amount:$('#ocrAmount').value.trim(),reviewed:$('#ocrReviewed').checked,acknowledged_flags:$('#ocrFlagsAck').checked})});const d=await r.json();
    if(!r.ok || !d.ok)throw new Error(d.reason || '请重新核对字段');
    ocrReviewToken=d.review_token;voiceSourceToken=null;$('#ocrDialog').close();
    if(d.account_requires_verification)await openTransferDialog({beneficiary:d.recipient,amount_cents:d.amount_cents,account_requires_verification:true});
    else{$('#messageInput').value=d.draft_text;$('#messageInput').focus();}
    announceA11y('已填写草稿，尚未办理。请核对后自行发送。',false);
  }catch(e){$('#ocrStatus').textContent=`未填写草稿：${e.message}`;updateOcrButtons();}
});
// N03: durable read-only task history. Loading a task never restores an action token.
const businessTaskStatus={waiting_confirmation:'等待本人确认',blocked:'需要核对',needs_clarification:'等待补齐资料',
  processing:'银行确认中',unknown:'结果待核验',recovery_required:'需要人工核对',succeeded:'已核实完成',failed:'未完成',
  reversed:'已确认冲正',paused:'已暂停',cancelled:'已取消',deferred:'已暂缓保护',expired:'确认已过期',handoff:'人工处理中'};
function styleBusinessTaskButton(button){
  button.className='business-task-button';Object.assign(button.style,{display:'inline-block',width:'auto',minWidth:'0',maxWidth:'100%',whiteSpace:'normal',padding:'10px 16px',margin:'6px 6px 6px 0',background:'#edf4ec',color:'#174e3a',border:'1px solid #c7d9cf',borderRadius:'10px',fontWeight:'600',lineHeight:'1.6'});
}
function businessTasksPanel(){
  let panel=$('#businessTasks');if(panel)return panel;
  panel=document.createElement('section');panel.id='businessTasks';panel.className='side-card';panel.style.gridColumn='1 / -1';panel.style.overflowWrap='anywhere';
  panel.innerHTML='<h2>我的办理任务</h2><p>查看已保存的本机演示任务不会继续办理业务。</p><button type="button" id="refreshBusinessTasks">刷新任务</button><p id="businessTasksNotice" role="status"></p><div id="businessTaskList"></div><div id="businessTaskDetail" aria-live="polite"></div>';
  $('#planPanel').after(panel);styleBusinessTaskButton($('#refreshBusinessTasks'));$('#refreshBusinessTasks').addEventListener('click',()=>refreshBusinessTasks());return panel;
}
async function showBusinessTask(taskId){
  try{const r=await fetch('/api/v1/business-tasks/'+encodeURIComponent(taskId),{headers:demoSessionHeaders});const d=await r.json();if(!r.ok||!d.ok)throw Error('当前会话无法查看该任务');
    const t=d.task,goal=t.goal||{},detail=$('#businessTaskDetail');
    const blockers=(t.blockers||[]).map(x=>`<li>${escapeHtml(x.message)}</li>`).join('');
    const stamp=v=>{const d=new Date(v);return Number.isNaN(d.getTime())?'待核实':d.toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'});};
    const names={available_funds:'可用资金',daily_limit:'日累计额度',beneficiary:'收款账户',card_status:'付款卡状态',authentication:'确认与验证',budget:'预算安排'};
    const facts=(t.facts||[]).filter(x=>names[x.check]).map(x=>`<li>${escapeHtml(names[x.check])}：${escapeHtml({satisfied:'当时已核对',missing:'当时未满足',unknown:'当时未核实'}[x.status]||'待核实')} · ${escapeHtml(stamp(x.observed_at))}</li>`).join('');
    const constraints=Number.isInteger(t.constraints?.security_buffer_cents)?`<p>当时安全缓冲：${escapeHtml(money(t.constraints.security_buffer_cents))}；已确认支出：${escapeHtml(money(t.constraints.confirmed_expenses_cents))}。</p>`:'';
    const history=(t.timeline||[]).map(x=>`<li>${escapeHtml(businessTaskStatus[x.status]||'待核对')} · ${escapeHtml(stamp(x.timestamp))}</li>`).join('');
    const receipts=(t.receipts||[]).map(x=>`<li>交易记录：${escapeHtml(x.transaction_id||x.bank_request_id||x.request_id||'待核对')}</li>`).join('');
    detail.innerHTML=`<h3>任务详情</h3><p>任务编号：${escapeHtml(t.task_id)}</p><p>收款对象：${escapeHtml(goal.beneficiary||'待补齐')} · 金额：${escapeHtml(money(goal.amount_cents))}</p><p>当前状态：${escapeHtml(businessTaskStatus[t.status]||'待核对')}</p>${constraints}<p>下一步由${t.responsible_party==='user'?'本人核对':'平台跟踪或人工核对'}；${t.authorization.strong_auth_required?'需要本人确认及强验证':'需要本人按计划核对确认'}。</p>${blockers?`<ul>${blockers}</ul>`:''}${facts?`<p>当时支付预检资料（北京时间，继续前重新核对）：</p><ul>${facts}</ul>`:''}<p>办理进度：</p><ol>${history}</ol>${receipts?`<p>结果凭证：</p><ul>${receipts}</ul>`:''}<p>查看记录不会确认转账；如需继续，仍须重新核对和确认。</p>`;
    detail.insertAdjacentHTML('beforeend',renderSourceMaterials(t.source_materials));
    renderPaymentTaskActions(t,detail);
    if(['blocked','needs_clarification','unknown','processing','recovery_required'].includes(t.status)){
      const handoff=document.createElement('button');handoff.type='button';handoff.id='paymentTaskHandoff';handoff.textContent='提交本任务的人工核查事项';styleBusinessTaskButton(handoff);detail.append(handoff);
      handoff.addEventListener('click',async()=>{handoff.disabled=true;try{const d=await postAgent('/api/v1/business-tasks/handoff',{task_id:t.task_id});if(!d.ok)throw Error(d.message);renderHandoffCard(d.handoff_card);$('#businessTasksNotice').textContent='本地人工事项已记录，尚未完成银行业务。';}catch(e){$('#businessTasksNotice').textContent=e.message;}finally{handoff.disabled=false;}});
    }

  }catch(e){$('#businessTasksNotice').textContent=e.message;}
}
async function refreshBusinessTasks(){
  businessTasksPanel();try{
    const r=await fetch('/api/v1/business-tasks',{headers:demoSessionHeaders});const d=await r.json();if(!r.ok||!d.ok)throw Error('暂不能读取当前会话的任务');
    const list=$('#businessTaskList');list.replaceChildren();for(const t of d.items||[]){const b=document.createElement('button');b.type='button';styleBusinessTaskButton(b);b.dataset.businessTask=t.task_id;b.textContent=`${t.goal.beneficiary||'待补齐对象'} · ${money(t.goal.amount_cents)} · ${businessTaskStatus[t.status]||'待核对'}`;b.addEventListener('click',()=>showBusinessTask(t.task_id));list.append(b);}
    $('#businessTasksNotice').textContent=d.items.length?'已读取当前会话的任务；点击查看进度。':'当前暂无已保存的支付任务。';
  }catch(e){$('#businessTasksNotice').textContent=e.message;}
}
refreshBusinessTasks();

// N05: explicit reprepare returns a new card; it never calls confirm/verify.
function taskMoneyInput(cents){return Number.isSafeInteger(cents)?`${Math.trunc(cents/100)}.${String(cents%100).padStart(2,'0')}`:'';}
function taskMoneyCents(value){
  if(!/^\d+(?:\.\d{1,2})?$/.test(value))throw Error('金额请填写非负数字，最多两位小数');
  const [whole,fraction='']=value.split('.');const cents=BigInt(whole)*100n+BigInt(fraction.padEnd(2,'0'));
  if(cents>BigInt(Number.MAX_SAFE_INTEGER))throw Error('金额过大，请重新核对');return Number(cents);
}
function renderPaymentTaskActions(t,detail){
  if(t.intent!=='transfer')return;
  const query=['processing','unknown','recovery_required'].includes(t.status);
  const resumable=['blocked','needs_clarification','waiting_confirmation','expired'].includes(t.status);
  if(!query&&!resumable)return;
  const box=document.createElement('fieldset');box.style.marginTop='16px';box.style.padding='16px';box.style.border='1px solid #c7d9cf';box.style.borderRadius='12px';
  box.innerHTML=query?'<legend>核对原请求</legend><p>结果未知不等于失败；此处只查询原记录，不会重新提交。</p><button type="button" id="taskRecheckBtn">查询原请求</button>':`<legend>重新核对支付任务</legend><p>仅准备新的确认卡，不会转账。最晚提交时间不代表银行到账承诺；留空的约束保留原设置。</p><label style="display:block">收款人 <input id="taskResumeBeneficiary" maxlength="80" value="${escapeHtml(t.goal.beneficiary||'')}" style="max-width:100%"></label><label style="display:block">金额（元） <input id="taskResumeAmount" inputmode="decimal" value="${escapeHtml(taskMoneyInput(t.goal.amount_cents))}" style="max-width:100%"></label><label style="display:block">备注 <input id="taskResumeNote" maxlength="500" value="${escapeHtml(t.goal.note||'')}" style="max-width:100%"></label><label style="display:block">至少保留（元；不能降低平台底线） <input id="taskResumeReserve" inputmode="decimal" value="${escapeHtml(taskMoneyInput(t.constraints.reserve_cents))}" style="max-width:100%"></label><label style="display:block">最晚提交时间（按本机时区输入） <input id="taskResumeDeadline" type="datetime-local" style="max-width:100%"></label>${t.constraints.deadline?`<p>原最晚提交时间：${escapeHtml(new Date(t.constraints.deadline).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'}))}（北京时间）</p>`:''}<button type="button" id="taskResumeBtn">重新核对并生成确认卡</button>`;
  box.querySelectorAll('label').forEach(label=>{label.style.marginBottom='10px';const input=label.querySelector('input');if(input)Object.assign(input.style,{display:'block',width:'100%',padding:'8px',border:'1px solid #c7d9cf',borderRadius:'8px',marginTop:'4px'});});
  const status=document.createElement('p');status.id='taskResumeStatus';status.setAttribute('role','status');box.append(status);detail.append(box);
  const button=box.querySelector('button');styleBusinessTaskButton(button);button.style.marginTop='16px';button.style.padding='12px 18px';button.style.background='#0b3b31';button.style.color='white';button.style.borderRadius='10px';button.addEventListener('click',async()=>{
    if(busy||button.disabled)return;button.disabled=true;
    try{
      let changes={};if(!query){
        const amount=taskMoneyCents($('#taskResumeAmount').value.trim());if(amount<=0)throw Error('转账金额必须大于零');
        changes={amount_cents:amount,beneficiary:$('#taskResumeBeneficiary').value.trim()};
        // A public history note can be redacted; preserve the private original unless edited.
        const note=$('#taskResumeNote').value;if(note!==(t.goal.note||''))changes.note=note;
        const reserve=$('#taskResumeReserve').value.trim();if(reserve)changes.reserve_cents=taskMoneyCents(reserve);
        const deadline=$('#taskResumeDeadline').value;if(deadline){const date=new Date(deadline);if(Number.isNaN(date.getTime()))throw Error('请核对时间');changes.deadline=date.toISOString();}
      }
      const d=await postAgent('/api/v1/business-tasks/'+(query?'recheck':'resume'),{task_id:t.task_id,expected_version:t.version,changes});
      if(!d.reprepared&&!d.query_only)throw Error(d.message||'暂时不能继续，请刷新任务');
      status.textContent=d.message||'已核对，尚未办理';if(d.plan_view)receiveAgentResponse(d);await refreshBusinessTasks();await showBusinessTask(t.task_id);
    }catch(e){status.textContent=e.message;}finally{button.disabled=false;}
  });
}

// N08 owner-approved local family scopes; no seeded grants become IAM permissions.
async function loadFamilyPermissions(){
  if(identityMode!=='SELF_HOSTED')return;
  const r=await fetch('/api/v1/family/permissions');const d=await r.json();if(!r.ok||!d.ok||!d.items?.length)return;
  let panel=$('#familyPermissions');if(!panel){panel=document.createElement('section');panel.id='familyPermissions';panel.className='side-card';businessTasksPanel().after(panel);}
  panel.innerHTML='<h2>家庭授权与协助</h2><p>仅本机虚构账户。阅读、风险提醒和协助起草分别核准；付款仍由账户本人认证、最终确认。</p>';
  for(const record of d.items){
    const box=document.createElement('fieldset');box.style.cssText='padding:16px;margin:12px 0;border:1px solid #c7d9cf;border-radius:12px';box.dataset.familyGrant=record.grant_id;
    const consent=record.consent;box.innerHTML=`<legend>${escapeHtml(record.subject_id)} ← ${escapeHtml(record.guardian_id)}</legend><p>权限版本：${consent.version}</p>`;
    if(record.is_owner){
      for(const [key,label] of [['read','允许阅读账户资料'],['notify','允许接收最小风险提醒'],['assist','允许协助起草（含阅读）']]){const row=document.createElement('label');row.style.display='block';const input=document.createElement('input');input.type='checkbox';input.dataset.familyScope=key;input.checked=consent[key];row.append(input,document.createTextNode(label));box.append(row);}
      const code=document.createElement('input');code.type='password';code.inputMode='numeric';code.autocomplete='one-time-code';code.maxLength=6;code.setAttribute('aria-label','核准家庭授权的动态码');code.placeholder='本人下一枚动态码';box.append(code);
      const save=document.createElement('button');save.type='button';save.dataset.familySave='';save.textContent='核准或撤回所选权限';styleBusinessTaskButton(save);box.append(save);
      const notice=document.createElement('p');notice.setAttribute('role','status');box.append(notice);
      save.addEventListener('click',async()=>{save.disabled=true;try{const permissions=Object.fromEntries([...box.querySelectorAll('[data-family-scope]')].map(x=>[x.dataset.familyScope,x.checked]));const result=await postAgent('/api/v1/family/permissions',{grant_id:record.grant_id,expected_version:consent.version,permissions,code:code.value});code.value='';notice.textContent=result.message;if(result.ok)await loadFamilyPermissions();}catch(_){notice.textContent='结果尚不能确认，请刷新权限记录，不要重复提交业务。';}finally{save.disabled=false;}});
    }else{
      const input=document.createElement('input');input.setAttribute('aria-label','家庭协助需求');input.placeholder='例如：给妈妈转1元';input.maxLength=2000;input.style.width='100%';box.append(input);
      const draft=document.createElement('button');draft.type='button';draft.dataset.familyDraft='';draft.textContent='按授权生成协助草稿';styleBusinessTaskButton(draft);box.append(draft);
      draft.addEventListener('click',async()=>{draft.disabled=true;try{const result=await postAgent('/api/v1/family/chat',{message:input.value,subject_id:record.subject_id,mode:'assist'});receiveAgentResponse(result);}finally{draft.disabled=false;}});
    }
    panel.append(box);
  }
  const label=document.createElement('label');label.textContent='本人核对协助草稿的任务编号';const task=document.createElement('input');task.id='familyReviewTask';task.maxLength=128;label.append(task);panel.append(label);
  const review=document.createElement('button');review.id='familyReviewBtn';review.type='button';review.textContent='本人读取并核对协助草稿';styleBusinessTaskButton(review);panel.append(review);
  review.addEventListener('click',async()=>{review.disabled=true;try{receiveAgentResponse(await postAgent('/api/v1/family/review',{task_id:task.value.trim()}));await refreshBusinessTasks();}finally{review.disabled=false;}});
}

function renderDomainReviews(reviews){
  if(!reviews||!Object.keys(reviews).length)return '';
  const labels={payment:'支付与交易',antifraud:'反诈风控',finance_health:'财务健康',family:'家庭授权',subscription:'订阅资料',card:'卡片资料'};
  const rows=Object.entries(reviews).filter(([key])=>labels[key]).map(([key,value])=>`<li>${escapeHtml(labels[key])}：${value.status==='ready'?'资料已核对':'存在待核实或待选择事项'}，${Number.isInteger(value.fact_count)?value.fact_count:0}项平台事实。<small>${escapeHtml((value.sources||[]).join('；'))}</small></li>`).join('');
  return `<details id="domainReviewDetails"><summary>领域核查依据</summary><p>领域报告是核查建议，不是授权、受理或到账证明。</p><ul>${rows}</ul></details>`;
}

// N12: local service workflows preserve independent bank, user and human statuses.
const serviceReasons={not_authorized:'用户表示不是本人授权',unfamiliar_merchant:'不认识商户名称',duplicate_charge:'怀疑重复扣款',refund_pending:'退款尚未到账'};
const localCaseStatus={LOCAL_QUEUED:'待人工领取',LOCAL_CLAIMED:'本地人工已接收',LOCAL_CLOSED:'本地已结单；银行问题仍待核实'};
const serviceRequestKeys=new Map();let serviceRequestCounter=0;
function serviceRequestId(kind,body){const key=JSON.stringify([kind,body]);if(!serviceRequestKeys.has(key))serviceRequestKeys.set(key,conversationId+'-s'+(++serviceRequestCounter));return serviceRequestKeys.get(key);}
function servicePanel(){
  let p=$('#serviceTasksPanel');if(p)return p;p=document.createElement('section');p.id='serviceTasksPanel';p.className='side-card';
  p.innerHTML='<h2>交易异议与资金准备</h2><p>仅本地演示服务记录。银行交易事实与用户主张分开；保护、人工接收和退款分别核对。</p><p id="serviceNotice" role="status"></p><div class="service-forms"><fieldset><legend>记录交易异议</legend><label>选择本人账单记录<select id="serviceTransaction"><option value="">请先核对交易</option></select></label><label>你的主张<select id="serviceReason"><option value="not_authorized">这笔交易不是本人授权</option><option value="unfamiliar_merchant">不认识商户名称</option><option value="duplicate_charge">怀疑重复扣款</option><option value="refund_pending">退款尚未到账</option></select></label><button type="button" id="openDisputeBtn">保存异议记录</button><p>本步骤不挂失、不退款，不等于银行正式争议受理。需要保护卡片时，请在原卡片流程核对对象并本人确认。</p></fieldset><fieldset><legend>期限资金准备</legend><label>准备金额（元）<input id="serviceFundingAmount" inputmode="decimal"></label><label>至少保留（元）<input id="serviceFundingReserve" inputmode="decimal" value="1000"></label><label>最晚提交时间（本机时区）<input id="serviceFundingDeadline" type="datetime-local"></label><p>本方案不借款，不自动赎回，不会自动办理付款。预计到账不等于可用资金。</p><button type="button" id="openFundingBtn">核对并保存资金方案</button></fieldset></div><button type="button" id="refreshServiceTasks">刷新服务记录</button><div id="serviceTaskList"></div><div id="serviceTaskDetail" aria-live="polite"></div>';
  businessTasksPanel().after(p);
  for(const b of p.querySelectorAll('button'))styleBusinessTaskButton(b);
  $('#openDisputeBtn').addEventListener('click',()=>openServiceTask('dispute'));
  $('#openFundingBtn').addEventListener('click',()=>openServiceTask('funding'));
  $('#refreshServiceTasks').addEventListener('click',()=>loadServiceTasks());return p;
}
async function openServiceTask(kind){
  const button=$(kind==='dispute'?'#openDisputeBtn':'#openFundingBtn');if(button.disabled||busy)return;button.disabled=true;
  try{
    let body;if(kind==='dispute'){if(!$('#serviceTransaction').value)throw Error('请先选择并核对交易');body={transaction_id:$('#serviceTransaction').value,reason:$('#serviceReason').value};}
    else{const date=new Date($('#serviceFundingDeadline').value);if(Number.isNaN(date.getTime()))throw Error('请填写含明确时间的提交期限');body={target_cents:taskMoneyCents($('#serviceFundingAmount').value.trim()),reserve_cents:taskMoneyCents($('#serviceFundingReserve').value.trim()),deadline:date.toISOString()};}
    const d=await postAgent('/api/v1/service-tasks/'+kind,{...body,request_id:serviceRequestId(kind,body)});if(!d.ok)throw Error(d.message||'记录尚不能确认');$('#serviceNotice').textContent='仅本地服务记录已保存；没有办理付款、保护或退款。';await loadServiceTasks();await showServiceTask(d.service_task.task_id);
  }catch(e){$('#serviceNotice').textContent=e.message;}finally{button.disabled=false;}
}
async function showServiceTask(id){
  try{const r=await fetch('/api/v1/service-tasks/'+encodeURIComponent(id),{headers:demoSessionHeaders});const d=await r.json();if(!r.ok||!d.task)throw Error('当前会话无法查看该服务记录');const t=d.task;const detail=$('#serviceTaskDetail');
    const time=v=>{const date=new Date(v);return Number.isNaN(date.getTime())?'待核实':date.toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'});};
    const facts=t.kind==='dispute'?`<p>账单事实：${escapeHtml(t.bank_fact.merchant)} · ${escapeHtml(money(t.bank_fact.amount_cents))} · ${escapeHtml(t.bank_fact.status)}</p><p>用户主张：${escapeHtml(serviceReasons[t.user_claim]||'待核实')}。账单存在此记录不能否定用户异议。</p><p>保护操作：须通过原卡片流程独立核对及本人确认。</p><p>银行正式案件号：尚未取得；退款尚未核实。</p>`:`<p>资金目标：${escapeHtml(money(t.goal.target_cents))}；至少保留：${escapeHtml(money(t.goal.reserve_cents))}；不借款。</p><p>最晚提交：${escapeHtml(time(t.goal.deadline))}（北京时间，不是到账承诺）。</p><p>当前可安排：${escapeHtml(money(t.funding_plan.spendable_now_cents))}；缺口：${escapeHtml(money(t.funding_plan.shortfall_cents))}；状态：${escapeHtml({ready_now:'本次核对可满足',shortfall:'存在资金缺口',needs_review:'资金事实待核实',deadline_expired:'期限已过'}[t.funding_plan.status]||'待核实')}。</p><p>不会自动办理付款；预计收入、未接入资产不计作确定可用。</p>`;
    detail.innerHTML=`<h3>${t.kind==='dispute'?'交易异议跟进':'期限资金方案'}</h3><p>本地任务编号：${escapeHtml(t.task_id)}</p>${facts}<p>事实核对时间：${escapeHtml(time(t.bank_fact?.observed_at||t.funding_plan?.observed_at))}（北京时间）</p><p>人工进度：${escapeHtml(t.local_case?localCaseStatus[t.local_case.status]||'待核实':'尚未提交人工核查事项')}</p><p>当前责任：${t.local_case?.status==='LOCAL_CLAIMED'?'本地坐席核对，银行结果仍待核实':'本人核对或联系正式银行渠道'}。</p><p>${escapeHtml(t.notice)}</p><ol>${(t.timeline||[]).map(x=>`<li>${escapeHtml({dispute_recorded:'保存异议记录',funding_prepared:'保存资金方案',handoff_requested:'提交本地人工事项',facts_rechecked:'重新核对事实'}[x.event]||'更新记录')} · ${escapeHtml(time(x.observed_at))}</li>`).join('')}</ol>`;
    detail.insertAdjacentHTML('beforeend',renderSourceMaterials(t.sources));
    const sourceLabel=document.createElement('label');sourceLabel.textContent='补充已核对材料的来源编号（仅参考）';const sourceInput=document.createElement('input');sourceInput.id='serviceSourceReference';sourceInput.maxLength=128;sourceLabel.append(sourceInput);detail.append(sourceLabel);
    const attach=document.createElement('button');attach.type='button';attach.id='serviceAttachSource';attach.textContent='关联已有资料引用';styleBusinessTaskButton(attach);detail.append(attach);
    const notice=document.createElement('p');notice.setAttribute('role','status');detail.append(notice);
    attach.addEventListener('click',async()=>{attach.disabled=true;try{const result=await postAgent('/api/v1/service-tasks/evidence',{task_id:t.task_id,source_id:sourceInput.value.trim(),expected_version:t.version});if(!result.ok)throw Error(result.message);await showServiceTask(t.task_id);}catch(e){notice.textContent=e.message;}finally{attach.disabled=false;}});
    const action=(id,text,kind)=>{const b=document.createElement('button');b.id=id;b.type='button';b.textContent=text;styleBusinessTaskButton(b);detail.append(b);b.addEventListener('click',async()=>{b.disabled=true;try{const result=await postAgent('/api/v1/service-tasks/'+kind,{task_id:t.task_id,...(kind==='recheck'?{expected_version:t.version}:{})});if(!result.ok)throw Error(result.message);await showServiceTask(t.task_id);}catch(e){notice.textContent=e.message;}finally{b.disabled=false;}});};
    action('serviceRecheckBtn','重新核对事实','recheck');if(!t.local_case)action('serviceHandoffBtn','提交本地人工核查事项','handoff');else{const b=document.createElement('button');b.type='button';b.textContent='刷新人工进度';styleBusinessTaskButton(b);b.addEventListener('click',()=>showServiceTask(t.task_id));detail.append(b);}
  }catch(e){$('#serviceNotice').textContent=e.message;}
}
async function loadServiceTasks(){
  servicePanel();try{const r=await fetch('/api/v1/service-tasks',{headers:demoSessionHeaders});const d=await r.json();if(!r.ok||!d.ok)throw Error('服务任务只面向已接入资料的本人演示账户开放');const list=$('#serviceTaskList');list.replaceChildren();for(const t of d.items){const b=document.createElement('button');b.type='button';styleBusinessTaskButton(b);b.dataset.serviceTask=t.task_id;b.textContent=(t.kind==='dispute'?'交易异议':'资金准备')+' · '+(t.local_case?localCaseStatus[t.local_case.status]:'已保存，待本人核对');b.addEventListener('click',()=>showServiceTask(t.task_id));list.append(b);}
    const state=await(await fetch('/api/state',{headers:demoSessionHeaders})).json();const select=$('#serviceTransaction');if(select.options.length===1)for(const item of state.transactions?.items||[]){const option=document.createElement('option');option.value=item.id;option.textContent=`${item.date||''} · ${item.merchant||'待核实商户'} · ${money(item.amount_cents)}`;select.append(option);}
  }catch(e){$('#serviceNotice').textContent=e.message;}
}
loadServiceTasks();

function renderSourceMaterials(sources){
 if(!sources?.length)return '';
 const rows=sources.map(source=>{const e=source.evidence||{},kind={rag:'知识引用',ocr:'人工核对的图片草稿',voice:'人工核对的语音草稿'}[source.kind]||'资料';
 const references=(e.citations||[]).map(c=>`${c.source_path}:L${c.line_start} · ${c.version}`).join('；');
 const fields=Object.entries(e.field_evidence||{}).map(([name,values])=>`${name==='amount'?'金额':'收款人'}位置：${values.map(v=>`第${v.line+1}行／置信度${(v.confidence_milli/10).toFixed(1)}%`).join('、')}`).join('；');
 return `<li>${escapeHtml(kind)} · ${escapeHtml(source.source_id)}<p>${source.current_access===false?'资料已失效或权限已变化，请重新核对。':escapeHtml(references||fields||'仅保存文件／音频摘要，未留存原始材料。')}${e.draft_changed===true?' 草稿已修改，以当前确认卡为准。':''}</p></li>`;}).join('');
 return `<details class="source-materials"><summary>查看资料来源留证</summary><p>材料仅作参考，不代表已授权、已受理、已保护或已退款。</p><ul>${rows}</ul></details>`;
}
