const assert=require('node:assert/strict');
const {SpeechPlayer,safeText,chooseLocal}=require('../app/static/speech-output.js');
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
