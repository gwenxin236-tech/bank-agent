const assert=require('node:assert/strict');
const {SpeechPlayer,safeText,chooseLocal,briefResponse,loadPreferences,savePreferences}=require('../app/static/speech-output.js');
const stored=new Map();const storage={getItem:k=>stored.get(k),setItem:(k,v)=>stored.set(k,v)};
assert.equal(savePreferences(storage,{enabled:true,cloud:true,voice:'zh-CN-YunxiNeural',rate:0}),true);
assert.deepEqual(loadPreferences(storage),{enabled:true,cloud:true,voice:'zh-CN-YunxiNeural',rate:0});
savePreferences(storage,{enabled:false,cloud:false,voice:'zh-CN-XiaoxiaoNeural',rate:-8});
assert.equal(loadPreferences(storage).cloud,false,'explicit local choice must survive reload');
assert.equal(loadPreferences(storage).enabled,false);
assert.deepEqual(loadPreferences({getItem:()=>'{bad'}),{enabled:true,cloud:false,voice:'zh-CN-XiaoxiaoNeural',rate:-8});
assert.equal(loadPreferences({getItem:()=>JSON.stringify({cloud:'true',rate:99,voice:'unexpected'})}).cloud,false);
assert.equal(loadPreferences({getItem(){throw Error('blocked')}}).rate,-8);
assert.equal(savePreferences({setItem(){throw Error('blocked')}},{}),false);
assert.equal(briefResponse({needs_auth:true,message:'大段验证解释'}),'请验证身份');
assert.equal(briefResponse({result_view:{status:'SUCCESS',user_message:'当前可用余额为5000元，更多说明'}}),'已完成');
assert.equal(briefResponse({result_view:{status:'UNKNOWN'}}),'结果待核实');
assert.equal(briefResponse({result_view:{status:'PARTIAL'}}),'部分完成，请查看结果');
assert.equal(briefResponse({plan_view:{requires_confirmation:true}}),'请确认办理');
assert.equal(briefResponse({message:'很长的说明文字'}),'');
const local={lang:'zh-CN',localService:true,name:'中文'};
assert.equal(chooseLocal([{lang:'en-US',localService:true}, {...local,localService:false},local]),local);
assert.ok(!safeText('卡号6222021234567890123，验证码123456，￥5,000.00元').match(/622202|123456|5,000/));
function setup(){
  const history=[],revoked=[],statuses=[],requests=[];
  class Audio{constructor(url){this.url=url;history.push(this);}play(){this.played=true;return Promise.resolve();}pause(){this.paused=true;}removeAttribute(){}load(){}}
  const env={Audio,URL:{createObjectURL:()=> 'blob:demo',revokeObjectURL:u=>revoked.push(u)},
    speechSynthesis:{cancel(){},getVoices:()=>[local],speak:u=>history.push(u)},Utterance:class{constructor(text){this.text=text;}},
    headers:()=>({}),status:s=>statuses.push(s),fetch:async(u,o)=>{requests.push(o);return{ok:true,blob:async()=>({})};}};
  return{env,history,revoked,statuses,requests,player:new SpeechPlayer(env)};
}
(async()=>{
  // Execute the actual send handler: a shortcut speaks its label once, and its
  // successful response must not replace it with a long banking explanation.
  const vm=require('node:vm'),fs=require('node:fs');
  const source=fs.readFileSync(require('node:path').join(__dirname,'../app/static/app.js'),'utf8');
  const said=[],received=[],posted=[];
  const context={busy:false,speechPlayer:{stop(){}},announceA11y:t=>said.push(t),addMessage(){},
    $:()=>({value:'',textContent:''}),setBusy:v=>{context.busy=v;},ocrReviewToken:null,voiceSourceToken:null,
    conversationId:'test',postAgent:async(path,body)=>{posted.push({path,body});return{ok:true,result_view:{status:'SUCCESS',user_message:'很长的消费说明'}};},
    receiveAgentResponse:(...args)=>received.push(args),refreshState:async()=>{},showToast(){}};
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('async function send('),source.indexOf('async function confirmPlan(')),context);
  await context.send('帮我看看最近的消费',{speechLabel:'看消费'});
  assert.deepEqual(said,['看消费']);assert.equal(received[0][2],false);
  assert.equal(posted.length,1);assert.equal(posted[0].body.message,'帮我看看最近的消费');
  context.busy=true;await context.send('查余额',{speechLabel:'查余额'});assert.equal(said.length,1);
  const a=setup();await a.player.speak('核对金额5000元。',{cloud:true,voice:'zh-CN-XiaoxiaoNeural',rate:-8});
  assert.ok(!a.requests[0].body.includes('5000'));assert.equal(a.history.length,1);
  a.player.stop();assert.ok(a.history[0].paused);assert.deepEqual(a.revoked,['blob:demo']);
  const b=setup();let resolve;b.env.fetch=()=>new Promise(r=>resolve=r);
  const pending=b.player.speak('旧结果',{cloud:true});b.player.stop();resolve({ok:true,blob:async()=>({})});await pending;
  assert.equal(b.history.length,0,'stale responses must never play');
  const c=setup();c.env.fetch=async()=>{throw Error('private');};await c.player.speak('核对',{cloud:true});
  assert.equal(c.history.length,0,'cloud failures must not silently send text elsewhere');
  assert.ok(c.statuses.at(-1).includes('暂不可用'));
  const d=setup();await d.player.speak('第一句。第二句。',{rate:0});assert.equal(d.history.length,1);
  assert.equal(d.history[0].rate,1);d.history[0].onend();assert.equal(d.history.length,2);
  d.player.stop();d.history[1].onend();assert.equal(d.history.length,2);
  const e=setup();await e.player.speak('同一句。');await e.player.speak('同一句。');assert.equal(e.history.length,1);
  console.log('Speech playback: privacy, locale, stop, stale response, errors, rate and deduplication passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
