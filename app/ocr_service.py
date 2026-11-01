"""P04 local-only OCR. Recognition is an untrusted draft, never bank evidence."""
from __future__ import annotations
import hashlib,io,json,math,os,re,secrets,threading,time,unicodedata,warnings
from decimal import Decimal,InvalidOperation
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
MAX_IMAGE_BYTES=2*1024*1024
MAX_PIXELS=4_000_000
MODEL_FILES=('PP-OCRv6_det_small.onnx','PP-OCRv6_rec_small.onnx','ch_ppocr_mobile_v2.0_cls_mobile.onnx',
             'config.yaml','default_models.yaml','arch_config.yaml')
_ENGINES={};_ENGINE_LOCK=threading.Lock()

def parse_amount_cents(value):
    if not isinstance(value,str) or not re.fullmatch(r'[0-9]{1,9}(?:\.[0-9]{1,2})?',value):raise ValueError('invalid decimal amount')
    cents=int(Decimal(value)*100)
    if not 0<cents<=99_999_999_999:raise ValueError('amount outside draft bounds')
    return cents

def verify_ocr_assets(folder):
    folder=Path(folder);p=folder/'manifest.json'
    if folder.is_symlink() or p.is_symlink() or p.stat().st_size>65536:raise ValueError('invalid OCR manifest')
    from local_embeddings import _json
    data=_json(p.read_text(encoding='utf-8'))
    if (data.get('schema')!=1 or data.get('engine')!='rapidocr' or data.get('version')!='3.10.0' or
        set(data.get('files',{}))!=set(MODEL_FILES)):raise ValueError('unsupported OCR assets')
    for name,row in data['files'].items():
        target=folder/name
        if (target.is_symlink() or type(row.get('size')) is not int or not 0<row['size']<100_000_000 or
            target.stat().st_size!=row['size'] or hashlib.sha256(target.read_bytes()).hexdigest()!=row['sha256']):
            raise ValueError('OCR asset integrity failed')
    return data

def _redact(text):
    def numbers(match):
        raw=re.sub(r'\D','',match.group())
        return '***'+raw[-4:] if len(raw)>=11 else match.group()
    return re.sub(r'(?<!\d)(?:\d[ -]?){11,19}(?!\d)',numbers,text)

class OCRService:
    def __init__(self,*,enabled=None,folder=None,clock=time.time):
        self.enabled=(os.getenv('OCR_ENABLED','0')=='1') if enabled is None else enabled
        self.folder=Path(folder or os.getenv('OCR_MODEL_DIR',str(ROOT/'models/ocr'))).resolve()
        self.clock=clock;self._gate=threading.BoundedSemaphore(1);self._lock=threading.Lock();self._drafts={};self._reviewed={};self._engine=None

    @staticmethod
    def failure(reason):return {'ok':False,'reason':reason,'message':'图片未生成可用草稿，请核对提示或改为手工填写。','business_executed':False}

    def capabilities(self):
        return {'enabled':bool(self.enabled),'mode':'local_ocr','assets_present':(self.folder/'manifest.json').is_file(),
                'max_bytes':MAX_IMAGE_BYTES,'max_pixels':MAX_PIXELS,'formats':['image/png','image/jpeg'],
                'requires_review':True,'network':'none','image_persisted':False}

    def _fingerprint(self):return tuple((n,(self.folder/n).stat().st_size,(self.folder/n).stat().st_mtime_ns) for n in ('manifest.json',*MODEL_FILES))

    def _load(self):
        fingerprint=self._fingerprint();key=(str(self.folder),fingerprint)
        if self._engine:
            if self._engine['fingerprint']!=fingerprint:raise ValueError('OCR model changed')
            return self._engine
        with _ENGINE_LOCK:
            if key not in _ENGINES:
                verify_ocr_assets(self.folder)
                from rapidocr import RapidOCR
                engine=RapidOCR(params={'Global.model_root_dir':str(self.folder),'Global.log_level':'critical',
                    'Det.model_path':str(self.folder/MODEL_FILES[0]),'Rec.model_path':str(self.folder/MODEL_FILES[1]),
                    'Cls.model_path':str(self.folder/MODEL_FILES[2]),
                    'EngineConfig.onnxruntime.intra_op_num_threads':2,'EngineConfig.onnxruntime.inter_op_num_threads':1,
                    'EngineConfig.onnxruntime.use_cuda':False,'Global.return_word_box':False})
                _ENGINES[key]={'engine':engine,'fingerprint':fingerprint,'lock':threading.Lock()}
            self._engine=_ENGINES[key]
        return self._engine

    def _decode(self,raw,mime):
        if mime not in {'image/png','image/jpeg'}:raise ValueError('unsupported_format')
        if (mime=='image/png' and not raw.startswith(b'\x89PNG\r\n\x1a\n')) or (mime=='image/jpeg' and not raw.startswith(b'\xff\xd8\xff')):
            raise ValueError('signature_mismatch')
        from PIL import Image,ImageOps
        with warnings.catch_warnings():
            warnings.simplefilter('error',Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                width,height=image.size
                if (image.format not in {'PNG','JPEG'} or width*height>MAX_PIXELS or
                    not 30<=width<=4096 or not 30<=height<=4096 or getattr(image,'n_frames',1)!=1):raise ValueError('image_bounds')
                image.verify()
            with Image.open(io.BytesIO(raw)) as image:
                cleaned=ImageOps.exif_transpose(image).convert('RGB');cleaned.info.clear()
                width,height=cleaned.size
                return cleaned,width,height

    def _infer(self,image):
        core=self._load()
        with core['lock']:result=core['engine'](image)
        if self._fingerprint()!=core['fingerprint']:raise ValueError('OCR assets changed')
        texts=result.txts
        if texts is None:return []
        return [{'text':str(t),'confidence':float(c),'box':b.tolist()} for t,c,b in zip(texts,result.scores,result.boxes,strict=True)]

    def extract(self,raw,mime,owner,device,*,consent=False,trace_id=''):
        if not self.enabled:return self.failure('disabled')
        if consent is not True:return self.failure('consent_required')
        if not owner or not device:return self.failure('identity_required')
        if not isinstance(raw,bytes) or not 0<len(raw)<=MAX_IMAGE_BYTES:return self.failure('invalid_size')
        if not self._gate.acquire(blocking=False):return self.failure('busy')
        image=None
        try:
            image,width,height=self._decode(raw,mime);values=self._infer(image)
            if not isinstance(values,list) or len(values)>100:return self.failure('too_many_lines')
            lines=[];flags=set();amounts=[];recipients=[]
            for i,row in enumerate(values):
                text=unicodedata.normalize('NFKC',row['text']).strip();score=row['confidence'];box=row['box']
                if (not isinstance(text,str) or len(text)>500 or type(score) not in (float,int) or not math.isfinite(score) or not 0<=score<=1 or
                    not isinstance(box,list) or len(box)!=4 or any(not isinstance(p,list) or len(p)!=2 or
                    any(type(v) not in (int,float) or not math.isfinite(v) for v in p) or
                    not 0<=p[0]<=width or not 0<=p[1]<=height for p in box)):
                    return self.failure('invalid_engine_output')
                if re.search(r'sk-[A-Za-z0-9_-]{12,}|(?:验证码|密码|口令)\s*[:：=]?\s*[A-Za-z0-9]{4,}',text,re.I):return self.failure('secret_detected')
                redacted=_redact(text)
                if redacted!=text:flags.add('account_requires_verification')
                from knowledge_service import _unsafe
                if _unsafe(redacted):return self.failure('injection_detected')
                if score<.9:flags.add('low_confidence')
                lines.append({'text':redacted,'confidence':score,'box':box})
                foreign=bool(re.search(r'USD|HKD|美元|美金|港币|EUR|欧元',text,re.I))
                if foreign:flags.add('unsupported_currency')
                amount=re.search(r'(?:转账金额|付款金额|金额|人民币|CNY|RMB|¥|￥)\s*[:：]?\s*(\d{1,9}(?:\.\d{1,2})?)(?![\d.])',text,re.I)
                if amount and not foreign:
                    try:amounts.append({'value':parse_amount_cents(amount.group(1)),'line':i,'confidence':score})
                    except ValueError:flags.add('invalid_amount')
                recipient=re.search(r'(?:收款人|收款方|户名)\s*[:：]\s*([\u4e00-\u9fffA-Za-z· ]{1,32})',text)
                if recipient:recipients.append({'value':recipient.group(1).strip(),'line':i,'confidence':score})
            if not lines:return self.failure('no_text')
            distinct_amount={r['value'] for r in amounts};distinct_names={r['value'] for r in recipients}
            if len(distinct_amount)>1:flags.add('amount_conflict')
            if len(distinct_names)>1:flags.add('recipient_conflict')
            if not amounts or not recipients:flags.add('missing_fields')
            fields={'amount_cents':next(iter(distinct_amount)) if len(distinct_amount)==1 else None,
                    'recipient':next(iter(distinct_names)) if len(distinct_names)==1 else None}
            draft='ocr_'+secrets.token_hex(16);digest=hashlib.sha256(raw).hexdigest()
            with self._lock:
                self._drafts={k:v for k,v in self._drafts.items() if v['expires']>self.clock()}
                if len(self._drafts)>=32:return self.failure('draft_limit')
                self._drafts[draft]={'owner':owner,'device':device,'expires':self.clock()+300,'sha256':digest,'flags':sorted(flags),'source_trace_id':trace_id,'field_evidence':{kind:[{'line':v['line'],'confidence_milli':int(round(v['confidence']*1000)),'box':[[int(round(c)) for c in point] for point in lines[v['line']]['box']]} for v in candidates[:8]] for kind,candidates in [('amount',amounts),('recipient',recipients)]}}
            return {'ok':True,'mode':'local_ocr','draft_id':draft,'sha256':digest,'width':width,'height':height,'lines':lines,
                    'fields':fields,'candidates':{'amount':amounts,'recipient':recipients},'flags':sorted(flags),'expires_in_seconds':300,
                    'requires_review':True,'business_executed':False,'image_persisted':False}
        except ImportError:return self.failure('engine_unavailable')
        except (ValueError,OSError,Warning):return self.failure('invalid_image_or_model')
        except Exception:return self.failure('engine_unavailable')
        finally:
            if hasattr(image,'close'):image.close()
            self._gate.release()

    def review(self,draft_id,owner,device,*,recipient,amount,reviewed=False,acknowledged_flags=False):
        if reviewed is not True:raise ValueError('explicit review required')
        if not isinstance(recipient,str) or not re.fullmatch(r'[\u4e00-\u9fffA-Za-z· ]{1,32}',recipient):raise ValueError('invalid recipient')
        from knowledge_service import _unsafe
        if _unsafe(recipient) or re.search(r'忽略|绕过|验证码|密码|转账|取消|挂失',recipient):raise ValueError('invalid recipient')
        cents=parse_amount_cents(amount)
        with self._lock:
            saved=self._drafts.get(draft_id)
            if not saved or saved['owner']!=owner or saved['device']!=device or saved['expires']<=self.clock():raise PermissionError('invalid draft binding')
            if saved['flags'] and acknowledged_flags is not True:raise ValueError('resolve and acknowledge flags')
            if 'unsupported_currency' in saved['flags']:raise ValueError('foreign currency unsupported; use manual bank channel')
            self._reviewed={k:v for k,v in self._reviewed.items() if v['expires']>self.clock()}
            if len(self._reviewed)>=32:raise ValueError('review draft budget exceeded')
            self._drafts.pop(draft_id)
            token='ocr_review_'+secrets.token_hex(16);self._reviewed[token]={**saved,'expires':self.clock()+300,'reviewed_fields_sha256':hashlib.sha256((recipient.strip()+':'+str(cents)).encode()).hexdigest(),'reviewed_recipient':recipient.strip(),'reviewed_amount_cents':cents}
        return {'ok':True,'draft_text':f'给{recipient.strip()}转{cents//100}.{cents%100:02d}元','amount_cents':cents,'recipient':recipient.strip(),
                'review_token':token,'source_trace_id':saved['source_trace_id'],
                'sha256':saved['sha256'],'business_executed':False,'requires_platform_confirmation':True,
                'account_requires_verification':'account_requires_verification' in saved['flags']}

    def consume_review(self,token,owner,device):
        with self._lock:
            row=self._reviewed.get(token)
            if not row or row['owner']!=owner or row['device']!=device or row['expires']<=self.clock():raise PermissionError('invalid OCR evidence')
            self._reviewed.pop(token)
        return {k:row[k] for k in ('sha256','source_trace_id','reviewed_fields_sha256','reviewed_recipient','reviewed_amount_cents','field_evidence') if k in row}
