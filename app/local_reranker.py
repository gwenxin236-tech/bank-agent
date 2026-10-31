"""Optional offline BGE cross-encoder. No model hub, URL fetch or bank tools."""
from contextvars import ContextVar
import hashlib,json,math,os,re,threading,time
from pathlib import Path
from local_embeddings import MODEL_FILES,_json

MODEL_ID='BAAI/bge-reranker-base'
ROOT=Path(__file__).resolve().parents[1]
_CORES={}
_LOCK=threading.Lock()


def verify_reranker(folder):
    folder=Path(folder);manifest=folder/'manifest.json'
    if folder.is_symlink() or manifest.is_symlink() or manifest.stat().st_size>65536:
        raise ValueError('invalid model manifest')
    data=_json(manifest.read_text(encoding='utf-8'))
    if (data.get('schema')!=1 or data.get('model')!=MODEL_ID or data.get('max_tokens')!=512 or
        not re.fullmatch('[0-9a-f]{40}',data.get('revision','')) or set(data.get('files',{}))!=set(MODEL_FILES)):
        raise ValueError('unsupported reranker')
    for name,entry in data['files'].items():
        p=folder/name
        if (p.is_symlink() or type(entry.get('size')) is not int or not 0<entry['size']<=400_000_000 or
            p.stat().st_size!=entry['size'] or not re.fullmatch('[0-9a-f]{64}',entry.get('sha256',''))):
            raise ValueError('invalid model inventory')
        digest=hashlib.sha256()
        with p.open('rb') as stream:
            for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
        if digest.hexdigest()!=entry['sha256']:raise ValueError('model digest mismatch')
    return data


class LocalReranker:
    transport='local'
    def __init__(self,folder):
        self.folder=Path(folder).resolve();self._core=None;self._gate=threading.BoundedSemaphore(1)
        self._evidence=ContextVar('rerank_evidence',default=None)
        self.model=MODEL_ID+':int8'
        try:
            p=self.folder/'manifest.json'
            if p.stat().st_size<=65536:self.model+=':'+hashlib.sha256(p.read_bytes()).hexdigest()[:16]
        except OSError:pass

    @classmethod
    def from_env(cls):
        if os.getenv('RAG_USE_RERANKER','0')!='1':return None
        return cls(os.getenv('RAG_RERANKER_DIR',str(ROOT/'models/reranker')))

    @property
    def evidence(self):return dict(self._evidence.get() or {'attempted':False,'reason':'disabled'})

    @staticmethod
    def probability(value):
        if type(value) not in (int,float) or not math.isfinite(value):raise ValueError('invalid logit')
        return 1/(1+math.exp(-value)) if value>=0 else math.exp(value)/(1+math.exp(value))

    def _fingerprint(self):
        return tuple((n,(self.folder/n).stat().st_size,(self.folder/n).stat().st_mtime_ns) for n in ('manifest.json',*MODEL_FILES))

    def _load(self):
        fingerprint=self._fingerprint()
        if self._core:
            if self._core['fingerprint']!=fingerprint:raise ValueError('model changed')
            return self._core
        with _LOCK:
            key=(str(self.folder),fingerprint)
            if key not in _CORES:
                meta=verify_reranker(self.folder)
                import numpy as np
                import onnxruntime as ort
                from tokenizers import Tokenizer
                ort.disable_telemetry_events()
                options=ort.SessionOptions();options.intra_op_num_threads=2;options.inter_op_num_threads=1;options.log_severity_level=3
                model=(self.folder/'model_quantized.onnx').read_bytes();tok=(self.folder/'tokenizer.json').read_bytes()
                for name,raw in [('model_quantized.onnx',model),('tokenizer.json',tok)]:
                    if hashlib.sha256(raw).hexdigest()!=meta['files'][name]['sha256']:raise ValueError('changed model')
                session=ort.InferenceSession(model,sess_options=options,providers=['CPUExecutionProvider'])
                tokenizer=Tokenizer.from_str(tok.decode('utf-8'));tokenizer.no_truncation();tokenizer.enable_padding(pad_id=1,pad_token='<pad>')
                if {x.name for x in session.get_inputs()}-{'input_ids','attention_mask','token_type_ids'}:raise ValueError('unknown model inputs')
                _CORES[key]={'np':np,'session':session,'tokenizer':tokenizer,'fingerprint':fingerprint,'lock':threading.Lock()}
            self._core=_CORES[key]
        return self._core

    def score(self,query,documents):
        from knowledge_service import _private,_unsafe
        evidence={'attempted':False,'reason':'invalid_input','transport':'local','model':self.model}
        self._evidence.set(evidence)
        if (not isinstance(query,str) or not 0<len(query)<=400 or _private(query) or _unsafe(query) or
            not isinstance(documents,list) or not 1<=len(documents)<=8 or
            any(not isinstance(d,str) or not 0<len(d)<=480 or _private(d) or _unsafe(d) for d in documents)):
            return None
        if not self._gate.acquire(blocking=False):
            self._evidence.set(dict(evidence,reason='busy'));return None
        started=time.monotonic()
        try:
            core=self._load();np=core['np'];scores=[]
            with core['lock']:
                for start in range(0,len(documents),2):
                    batch=documents[start:start+2]
                    tokens=core['tokenizer'].encode_batch([(query,d) for d in batch])
                    if any(len(t.ids)>512 for t in tokens):
                        evidence['reason']='token_limit';return None
                    feeds={'input_ids':np.array([t.ids for t in tokens],dtype=np.int64),
                           'attention_mask':np.array([t.attention_mask for t in tokens],dtype=np.int64),
                           'token_type_ids':np.array([t.type_ids for t in tokens],dtype=np.int64)}
                    names={i.name for i in core['session'].get_inputs()};evidence['attempted']=True
                    logits=core['session'].run(None,{k:v for k,v in feeds.items() if k in names})[0]
                    if logits.shape not in {(len(batch),),(len(batch),1)}:raise ValueError('invalid logits')
                    scores.extend(self.probability(float(v)) for v in logits.reshape(-1))
            if self._fingerprint()!=core['fingerprint']:raise ValueError('changed model')
            evidence.update(reason='accepted',candidates=len(documents),truncated=False)
            return scores
        except Exception:
            evidence['reason']='unavailable';return None
        finally:
            evidence['elapsed_ms']=round((time.monotonic()-started)*1000)
            self._evidence.set(evidence);self._gate.release()
