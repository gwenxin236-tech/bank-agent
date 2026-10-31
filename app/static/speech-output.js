/* Playback only. Never calls a bank action endpoint. */
(function (root) {
  'use strict';
  const SAMPLE = '看消费';
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
    constructor(env) { this.env=env; this.generation=0; this.audio=null; this.url=null; this.pending=null; this.last=''; this.lastAt=0; this.utterances=[]; }
    stop() {
      this.generation++; this.pending?.abort(); this.pending=null;
      if(this.audio){this.audio.pause();this.audio.removeAttribute('src');this.audio.load();this.audio=null;}
      if(this.url){this.env.URL.revokeObjectURL(this.url);this.url=null;}
      this.env.speechSynthesis?.cancel(); this.utterances=[];
    }
    async speak(text, options={}) {
      text=normalize(options.cloud || options.privacy ? safeText(text) : text);
      if(!text)return;
      if(!options.force && this.last===text && Date.now()-this.lastAt<1800)return;
      this.stop();const generation=this.generation;this.last=text;this.lastAt=Date.now();
      const status=message=>{if(generation===this.generation)this.env.status(message);};
      if(options.cloud){
        const controller=new AbortController();this.pending=controller;
        const timeout=setTimeout(()=>controller.abort(),24000);
        try{
          status('正在生成自然语音…');
          const response=await this.env.fetch('/api/v1/voice/synthesize',{method:'POST',headers:{...this.env.headers(),'Content-Type':'application/json'},
            body:JSON.stringify({text:text.slice(0,600),consent:true,voice:options.voice,rate:options.rate}),signal:controller.signal});
          if(!response.ok)throw Error('speech unavailable');
          const blob=await response.blob();if(generation!==this.generation)return;
          this.url=this.env.URL.createObjectURL(blob);const audio=this.audio=new this.env.Audio(this.url);
          audio.onended=()=>{if(generation===this.generation){this.stop();this.env.status('播放完毕');}};
          audio.onerror=()=>{if(generation===this.generation){this.stop();this.env.status('音频播放失败，请重试试听或阅读文字。');}};
          await audio.play();status('正在播放自然语音');
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
  root.BankSpeech={SpeechPlayer,safeText,normalize,chooseLocal,briefResponse,SAMPLE};
  if(typeof module!=='undefined')module.exports=root.BankSpeech;
})(typeof window==='undefined'?globalThis:window);
