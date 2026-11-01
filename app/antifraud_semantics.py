"""Bounded local phrase evidence. No score, permissions, DB, network or tools."""
import re
VERSION='n06-2026-10-31-v1'
_GROUPS={
 'safety_account':('安全账户','安全帐户','监管账户','清查账户'),
 'brush_return':('刷单','返现','返利','佣金任务','做任务返现'),
 'fake_customer_service':('客服联系您','官方客服','理赔客服','退款客服','售后客服','客服要求转账'),
 'police_impersonation':('公检法','公安局','检察院','法院传票','涉嫌洗钱','涉嫌犯罪'),
 'high_yield_guarantee':('保本','保本高收益','稳赚不赔','高收益低风险','零风险','高收益稳赚','投资导师','带你赚'),
 'verification_code':('验证码','动态码','短信码'),
 'unfreeze_money':('解冻金','解冻费','保证金','解冻资金','解冻账户','解除风控'),
 'urgency':('马上转账','立刻转账','赶紧转','否则','不然就','限时')}
_PATTERNS={
 'semantic_contradictory_yield':r'(?:保本.{0,16}高收益|高收益.{0,16}保本)',
 'semantic_verification_phishing':r'(?:验证码|动态码|短信码).{0,10}(?:报给|告诉|发给|交给|提供给)',
 'semantic_police_transfer_coerce':r'(?:公检法|公安局|检察院|法院).{0,20}转账',
 'semantic_unfreeze_transfer':r'(?:解冻.{0,20}转账|转账.{0,20}解冻)'}

def collect(text,legacy_rules=()):
 text=str(text)[:4000];hits=[]
 demand=bool(re.search(r'帮我转|我要转|让我转|要求我|要求.{0,12}(?:转|报给)|对方让|催我|准备转|要先转|转入',text))
 educational=bool(re.search(r'什么是|是什么意思|科普|识别案例|案例分析|教育示例|防骗知识|请解释|电话号码',text)) and not bool(re.search(r"帮我转|我要转|我准备转|正在催我|对方正要求",text))
 def add(code,start,end,eligible=True):
  if len(hits)>=96:return
  prefix=re.split(r'[，,。；;！!？?]',text[max(0,start-16):start])[-1]
  negated=bool(re.search(r'(?:不涉及|没有|并非|不是|拒绝|切勿|不要|不会|无需|从不)(?:把|将|向|提供|透露|索要|.{0,3})?$',prefix))
  status='negated' if negated else 'educational' if educational else 'active' if eligible else 'benign'
  hits.append({'code':code,'start':start,'end':end,'context':status})
 for name,words in _GROUPS.items():
  for word in words:
   for m in re.finditer(re.escape(word),text):
    eligible=name=='safety_account' or (name=='brush_return' and ('刷单' in text or '佣金任务' in text or '做任务返现' in text)) or (name=='high_yield_guarantee' and word not in {'保本','零风险'}) or (name in {'fake_customer_service','police_impersonation','unfreeze_money'} and demand)
    add('scam_phrase_'+name,m.start(),m.end(),eligible)
 for code,pattern in _PATTERNS.items():
  for m in re.finditer(pattern,text):add(code,m.start(),m.end())
 known={w for ws in _GROUPS.values() for w in ws}
 for index,rule in enumerate(legacy_rules):
  word=rule.get('keyword','')
  if not word or word in known or any(word in w for w in known):continue
  for m in re.finditer(re.escape(word),text):add('local_phrase_'+str(index),m.start(),m.end())
 return {'version':VERSION,'hits':hits,'active_codes':list(dict.fromkeys(h['code'] for h in hits if h['context']=='active')),'truncated':len(str(text))>=4000 or len(hits)>=96}
