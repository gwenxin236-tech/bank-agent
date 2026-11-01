/* Playback only. Never calls a bank action endpoint. */
(function (root) {
  'use strict';
  const SAMPLE = '看消费';
  const FIXED_PROMPTS = ['查询余额','看消费','转账','看自动扣费','消费体检','家庭共管','反诈守护','卡片管理',
    '已拦截，请核实','请验证身份','验证通过，请确认办理','已完成','处理中','结果待核实',
    '部分完成，请查看结果','未完成，请查看原因','请查看结果','请补充信息','请确认办理',
    '连接失败','结果待核实，请勿重复办理','暂时无法验证身份'];
  // Exact UI aliases only: never replace a character inside a name or account text.
  function pronounce(text) { return ({'查余额':'查询余额','转一笔钱':'转账'})[text] || text; }
  const PREFERENCES_KEY = 'bankSpeechPreferences.v1';
  function preferences(value={}) {
    return {enabled:typeof value?.enabled==='boolean'?value.enabled:true,
      cloud:value?.cloud===true,
      voice:['zh-CN-XiaoxiaoNeural','zh-CN-YunxiNeural'].includes(value?.voice)?value.voice:'zh-CN-XiaoxiaoNeural',
      rate:[-20,-8,0,10].includes(value?.rate)?value.rate:-8};
  }
  function loadPreferences(storage) {
    try{return preferences(JSON.parse(storage?.getItem(PREFERENCES_KEY) || '{}'));}
    catch(_){return preferences();}
  }
  function savePreferences(storage, value) {
    try{if(!storage)return false;storage.setItem(PREFERENCES_KEY,JSON.stringify(preferences(value)));return true;}
    catch(_){return false;}
  }
  function briefResponse(data) {
    if(data.blocked)return '已拦截，请核实';
    if(data.needs_auth)return '请验证身份';
    if(data.credential_id && data.ok)return '验证通过，请确认办理';
    const result=data.result_view || data.presentation?.standard?.result_view;
    if(result)return ({SUCCESS:'已完成',PROCESSING:'处理中',UNKNOWN:'结果待核实',
      PARTIAL:'部分完成，请查看结果',FAILED:'未完成，请查看原因'})[result.status] || '请查看结果';
    if(data.ok===false)return '未完成，请查看原因';
    if(data.awaiting_details)return '请补充信息';
    if((data.plan_view || data.presentation?.standard?.plan_view)?.requires_confirmation)return '请确认办理';
    return '';
  }
  function safeText(text) {
    return String(text || '').replace(/sk-[A-Za-z0-9_-]+/g,'密钥已隐藏')
      .replace(/(?:密码|口令|验证码|动态码)\s*[:：=]?\s*[A-Za-z0-9]{4,}/g,'验证信息已隐藏')
      .replace(/\d+[＊*]+\d+|[＊*]+\d{2,}|\d{8,}/g,'账号已隐藏')
      .replace(/[¥￥]?\s*\d[\d,.]*\s*元?/g,'金额或编号已隐藏');
  }
  function normalize(text) {
    return String(text || '').replace(/[*#_`]/g,'').replace(/[·•]/g,'，').replace(/\s+/g,' ').trim();
  }
  function chooseLocal(voices) {
    return voices.filter(v => v.localService && /^zh[-_](CN|Hans)/i.test(v.lang))
      .sort((a,b) => Number(/Xiaoxiao|晓晓|Natural|Neural/i.test(b.name))-Number(/Xiaoxiao|晓晓|Natural|Neural/i.test(a.name)))[0];
  }
  class SpeechPlayer {
    constructor(env) { this.env=env; this.generation=0; this.audio=null; this.url=null; this.pending=null; this.last=''; this.lastAt=0; this.utterances=[]; this.promptBlobs=new Map();this.promptAudio=new Map();this.promptLoading=null;this.promptsReady=false;this.prepared=false; }
    installPrompts(pack) {
      if(pack?.version!==1 || pack.rate!==0)throw Error('invalid prompt pack');
      const blobs=new Map();
      for(const voice of ['zh-CN-XiaoxiaoNeural','zh-CN-YunxiNeural'])for(const text of FIXED_PROMPTS){
        const encoded=pack.voices?.[voice]?.[text]?.mp3;
        if(typeof encoded!=='string' || encoded.length>280000 || !/^[A-Za-z0-9+/]+=*$/.test(encoded))throw Error('invalid prompt audio');
        const bytes=Uint8Array.from(atob(encoded),c=>c.charCodeAt(0));
        if(bytes.length<32 || !(bytes[0]===255 || (bytes[0]===73 && bytes[1]===68 && bytes[2]===51)))throw Error('invalid mp3');
        blobs.set(voice+'|'+text,new Blob([bytes],{type:'audio/mpeg'}));
      }
      this.promptBlobs=blobs;this.promptsReady=true;
      if(this.env.preloadAudio && !this.promptAudio.size)for(const [key,blob] of blobs){
        const url=this.env.URL.createObjectURL(blob),audio=new this.env.Audio(url);
        audio.preload='auto';audio.load();this.promptAudio.set(key,{audio,url});
      }
    }
    preparePrompts() {
      if(this.promptsReady)return Promise.resolve(true);
      if(this.promptLoading)return this.promptLoading;
      const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),1500);
      this.promptLoading=(async()=>{
        try{
          const response=await this.env.fetch('/static/speech-prompts.json',{signal:controller.signal});
          if(!response.ok)throw Error('prompt pack unavailable');
          this.installPrompts(await response.json());return true;
        }catch(_){return false;}
        finally{clearTimeout(timer);this.promptLoading=null;}
      })();
      return this.promptLoading;
    }
    stop() {
      this.generation++; this.pending?.abort(); this.pending=null;
      if(this.audio){
        this.audio.onended=null;this.audio.onerror=null;this.audio.pause();
        if(this.prepared)this.audio.currentTime=0;
        else{this.audio.removeAttribute('src');this.audio.load();}
        this.audio=null;this.prepared=false;
      }
      if(this.url){this.env.URL.revokeObjectURL(this.url);this.url=null;}
      this.env.speechSynthesis?.cancel(); this.utterances=[]; this.last='';
    }
    async playBlob(blob, generation, label, rate=1, prepared=null) {
      if(generation!==this.generation)return;
      let audio;
      if(prepared){audio=this.audio=prepared.audio;this.prepared=true;audio.currentTime=0;}
      else{this.url=this.env.URL.createObjectURL(blob);audio=this.audio=new this.env.Audio(this.url);}
      audio.playbackRate=rate;audio.preservesPitch=true;
      audio.onended=()=>{if(generation===this.generation){this.stop();this.env.status('播放完毕');}};
      audio.onerror=()=>{if(generation===this.generation){this.stop();this.env.status('音频播放失败，请重试试听或阅读文字。');}};
      await audio.play();if(generation===this.generation)this.env.status(label);
    }
    async speak(text, options={}) {
      text=pronounce(normalize(options.cloud || options.privacy ? safeText(text) : text));
      if(!text)return;
      if(!options.force && this.last===text && Date.now()-this.lastAt<1800)return;
      this.stop();const generation=this.generation;this.last=text;this.lastAt=Date.now();
      const status=message=>{if(generation===this.generation)this.env.status(message);};
      if(FIXED_PROMPTS.includes(text)){
        if(this.promptLoading && !this.promptsReady)await this.promptLoading;
        if(generation!==this.generation)return;
        const voice=preferences(options).voice,blob=this.promptBlobs.get(voice+'|'+text);
        if(blob){
          try{await this.playBlob(blob,generation,'正在播放本地自然语音',1+preferences(options).rate/100,this.promptAudio.get(voice+'|'+text));}
          catch(_){if(generation===this.generation){this.stop();this.env.status('音频播放失败，请点击试听或阅读文字。');}}
          return;
        }
      }
      if(options.cloud){
        const controller=new AbortController();this.pending=controller;
        const timeout=setTimeout(()=>controller.abort(),24000);
        try{
          status('正在生成自然语音…');
          const response=await this.env.fetch('/api/v1/voice/synthesize',{method:'POST',headers:{...this.env.headers(),'Content-Type':'application/json'},
            body:JSON.stringify({text:text.slice(0,600),consent:true,voice:options.voice,rate:options.rate}),signal:controller.signal});
          if(!response.ok)throw Error('speech unavailable');
          const blob=await response.blob();if(generation!==this.generation)return;
          await this.playBlob(blob,generation,'正在播放自然语音');
        }catch(_){if(generation===this.generation){this.stop();this.env.status('自然语音暂不可用。请阅读文字，或关闭联网语音后使用本机音色。');}}
        finally{clearTimeout(timeout);if(this.pending===controller)this.pending=null;}
      }else{
        const voice=chooseLocal(this.env.speechSynthesis?.getVoices() || []);
        if(!voice || !this.env.Utterance){status('未检测到本机普通话音色，请启用联网语音或阅读文字。');return;}
        const chunks=text.match(/[^。！？；]+[。！？；]?/g)||[text];
        const playNext=()=>{
          if(generation!==this.generation)return;
          const chunk=chunks.shift();if(!chunk){this.utterances=[];status('播放完毕');return;}
          const utterance=new this.env.Utterance(chunk);this.utterances.push(utterance);
          utterance.lang='zh-CN';utterance.voice=voice;utterance.rate=1+(options.rate ?? -8)/100;utterance.pitch=1;
          utterance.onend=playNext;utterance.onerror=()=>status('本机语音不可用，请阅读页面文字。');
          status('正在播放本机普通话');this.env.speechSynthesis.speak(utterance);
        };playNext();
      }
    }
  }
  root.BankSpeech={SpeechPlayer,safeText,normalize,chooseLocal,briefResponse,SAMPLE,loadPreferences,savePreferences,pronounce,FIXED_PROMPTS};
  if(typeof module!=='undefined')module.exports=root.BankSpeech;
})(typeof window==='undefined'?globalThis:window);
