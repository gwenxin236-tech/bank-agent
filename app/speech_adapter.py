"""Optional SiliconFlow ASR. Draft text only; no bank tools, disk audio or retries.
Production replacement: approved private ASR gateway, IAM, DLP and retention policy.
"""
from __future__ import annotations

import io
import http.client
import json
import os
import secrets
import urllib.error
import urllib.request
import wave
import hashlib
import re
import struct
import threading
import time

MAX_AUDIO_BYTES = 2 * 1024 * 1024
MAX_SECONDS = 30
ENDPOINT = 'https://api.siliconflow.cn/v1/audio/transcriptions'
MODEL = 'FunAudioLLM/SenseVoiceSmall'


def provider_retention_info():
    """Documented capability, not an independently verified erasure receipt."""
    return {'scope':'ASR_REQUEST','record_delete_api':'NOT_DOCUMENTED','deletion_verified':False,
        'disposal_basis':'PROVIDER_POLICY_DECLARATION','manual_contact':'contact@siliconflow.cn',
        'policy_date':'2026-07-30','checked_on':'2026-10-24',
        'policy_url':'https://docs.siliconflow.cn/docs/legals/privacy-policy',
        'asr_api_url':'https://docs.siliconflow.cn/docs/api/audio-transcriptions-post',
        'voice_style_delete_is_asr_delete':False}


def prepare_audio(audio):
    """Hash exact upload bytes, decoded samples and a metadata-free WAV separately.

    Input is already browser-resampled audio, NOT original microphone samples.
    No persistence, resampling, speaker identification or authenticity assertion.
    """
    if not isinstance(audio,bytes) or not 44<=len(audio)<=MAX_AUDIO_BYTES:raise ValueError('invalid audio size')
    if audio[:4]!=b'RIFF' or audio[8:12]!=b'WAVE' or struct.unpack_from('<I',audio,4)[0]!=len(audio)-8:
        raise ValueError('invalid WAV envelope')
    pos=12;data_sizes=[];formats=0
    while pos<len(audio):
        if pos+8>len(audio):raise ValueError('truncated chunk')
        kind=audio[pos:pos+4];size=struct.unpack_from('<I',audio,pos+4)[0];pos+=8
        if pos+size+(size%2)>len(audio):raise ValueError('truncated chunk')
        if kind==b'data':data_sizes.append(size)
        if kind==b'fmt ':formats+=1
        pos+=size+(size%2)
    if formats!=1 or len(data_sizes)!=1 or data_sizes[0]%2:raise ValueError('ambiguous audio chunks')
    try:
        with wave.open(io.BytesIO(audio),'rb') as f:
            frames=f.getnframes()
            if (f.getnchannels()!=1 or f.getsampwidth()!=2 or f.getframerate()!=16000 or
                f.getcomptype()!='NONE' or not 0<frames<=16000*MAX_SECONDS):raise ValueError('unsupported PCM format')
            pcm=f.readframes(frames)
            if len(pcm)!=frames*2 or len(pcm)!=data_sizes[0]:raise ValueError('truncated PCM')
    except (wave.Error,EOFError) as exc:raise ValueError('invalid WAV') from exc
    output=io.BytesIO()
    with wave.open(output,'wb') as f:
        f.setnchannels(1);f.setsampwidth(2);f.setframerate(16000);f.writeframes(pcm)
    normalized=output.getvalue()
    evidence={'schema':1,'algorithm':'SHA-256','upload_sha256':hashlib.sha256(audio).hexdigest(),'upload_bytes':len(audio),
        'pcm_sha256':hashlib.sha256(pcm).hexdigest(),'pcm_bytes':len(pcm),
        'normalized_wav_sha256':hashlib.sha256(normalized).hexdigest(),'normalized_wav_bytes':len(normalized),
        'normalization':'wav-pcm-s16le-mono-16000-v1','sample_rate_hz':16000,'channels':1,'sample_width_bytes':2,
        'frames':frames,'duration_ms':frames*1000//16000,'application_audio_persisted':False}
    return evidence,normalized


class VoiceEvidenceStore:
    """Only short-lived audit metadata. Never retains audio or transcript text."""
    def __init__(self,*,clock=time.time):
        self.clock=clock;self._items={};self._lock=threading.Lock()
    def _prune(self):
        self._items={k:v for k,v in self._items.items() if v['expires']>self.clock()}
    def issue(self,evidence,owner,device,trace):
        if not owner or not device or not trace:raise ValueError('missing evidence binding')
        hashes=('upload_sha256','pcm_sha256','normalized_wav_sha256')
        if any(not re.fullmatch('[0-9a-f]{64}',str(evidence.get(k,''))) for k in hashes):raise ValueError('invalid digest')
        with self._lock:
            self._prune()
            if len(self._items)>=64:raise ValueError('voice evidence budget exhausted')
            token='voice_'+secrets.token_hex(16)
            self._items[token]={'owner':owner,'device':device,'expires':self.clock()+300,'source_trace_id':trace,
                **{k:evidence[k] for k in hashes}}
            return token
    def consume(self,token,owner,device,*,reviewed=False):
        with self._lock:
            self._prune()
            row=self._items.get(token) if isinstance(token,str) else None
            if (reviewed is not True or not row or row['owner']!=owner or row['device']!=device):
                raise PermissionError('invalid voice evidence')
            self._items.pop(token)
        return {k:row[k] for k in ('source_trace_id','upload_sha256','pcm_sha256','normalized_wav_sha256')}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward credentials/recordings to a redirect target.


class SpeechAdapter:
    def __init__(self):
        self.key = os.environ.get('LLM_API_KEY', '').strip()
        self.enabled = (os.environ.get('VOICE_ENABLED') == '1'
                        and os.environ.get('BANK_AGENT_OFFLINE') != '1'
                        and os.environ.get('LLM_PROVIDER', '').lower() == 'siliconflow'
                        and bool(self.key))

    def capabilities(self):
        return {'enabled': self.enabled, 'provider': 'siliconflow', 'model': MODEL,
                'max_seconds': MAX_SECONDS, 'max_bytes': MAX_AUDIO_BYTES,
                'format': 'PCM WAV mono 16kHz', 'requires_consent': True,
                'evidence_enabled':True,'audio_evidence_schema':1,'application_audio_persisted':False,
                'provider_retention':provider_retention_info(),
                'cloud_consent_header':'X-Audio-Cloud-Consent','offline_mode':'local_evidence_only'}

    @staticmethod
    def failure(reason):
        return {'ok': False, 'reason': reason,
                'message': '暂时无法识别语音，请直接打字；尚未发送任何银行操作。'}

    def transcribe(self, audio):
        if not self.enabled:
            return self.failure('disabled')
        try:
            evidence,normalized=prepare_audio(audio)
        except ValueError:
            return self.failure('invalid_audio')
        boundary = 'bankvoice-' + secrets.token_hex(16)
        request_id='asr_'+secrets.token_hex(16)
        receipt={'request_id':request_id,'http_status':None,'response_trace_matches':False,'deletion_verified':False}
        def fail(reason):return dict(self.failure(reason),provider_receipt=dict(receipt))
        data = (f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{MODEL}\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="voice.wav"\r\n'
                'Content-Type: audio/wav\r\n\r\n').encode() + normalized + f'\r\n--{boundary}--\r\n'.encode()
        request = urllib.request.Request(ENDPOINT, data=data, headers={
            'Authorization': 'Bearer ' + self.key,
            'X-Trace-Id':request_id,
            'Content-Type': 'multipart/form-data; boundary=' + boundary,
            'Accept': 'application/json'}, method='POST')
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=20) as response:
                status=getattr(response,'status',None)
                receipt['http_status']=status if type(status) is int else None
                headers=getattr(response,'headers',{})
                echoed=headers.get('x-siliconcloud-trace-id')
                receipt['response_trace_matches']=isinstance(echoed,str) and echoed==request_id
                raw = response.read(65537)
            if len(raw) > 65536:
                return fail('response_too_large')
            result = json.loads(raw)
            text = result.get('text') if isinstance(result, dict) else None
            if not isinstance(text, str) or not text.strip() or len(text) > 2000:
                return fail('invalid_response')
            return {'ok': True, 'text': text.strip(), 'source': 'siliconflow_asr','audio_evidence':evidence,
                    'provider_receipt':dict(receipt)}
        except urllib.error.HTTPError as exc:
            receipt['http_status']=exc.code
            exc.close()
            return fail('upstream_unavailable')
        except (TimeoutError, urllib.error.URLError, OSError, http.client.HTTPException):
            return fail('upstream_unavailable')
        except (ValueError, UnicodeError):
            return fail('invalid_response')
