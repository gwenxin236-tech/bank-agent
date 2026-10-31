"""Optional CPU-only ONNX embedding. Reads local files; no download/API code.

User-approved P03 offline extra; importing this module remains stdlib-only.
"""
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time

MODEL_FILES = ('model_quantized.onnx', 'tokenizer.json', 'config.json',
               'tokenizer_config.json', 'MODEL_README.md', 'LICENSE.txt')
MODEL_ID = 'BAAI/bge-small-zh-v1.5'
QUERY_PREFIX = '为这个句子生成表示以用于检索相关文章：'
_CORES = {}
_CORE_LOCK = threading.Lock()


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: raise ValueError('duplicate key')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs)


def verify_model(folder):
    """Exact allowlist + hashes. Integrity protection, not publisher signature."""
    folder = Path(folder)
    manifest_path = folder / 'manifest.json'
    if folder.is_symlink() or manifest_path.is_symlink(): raise ValueError('linked model')
    if manifest_path.stat().st_size > 65536: raise ValueError('manifest too large')
    manifest = _json(manifest_path.read_text(encoding='utf-8'))
    if (manifest.get('schema') != 1 or manifest.get('model') != MODEL_ID or
        type(manifest.get('dimension')) is not int or manifest['dimension'] != 512 or
        type(manifest.get('max_tokens')) is not int or manifest['max_tokens'] != 512 or
        not re.fullmatch('[0-9a-f]{40}', manifest.get('revision', '')) or
        set(manifest.get('files', {})) != set(MODEL_FILES)):
        raise ValueError('unsupported model manifest')
    for name, entry in manifest['files'].items():
        path = folder / name
        if (path.is_symlink() or not path.is_file() or type(entry.get('size')) is not int or
            not 0 < entry['size'] <= 150_000_000 or path.stat().st_size != entry['size'] or
            not re.fullmatch('[0-9a-f]{64}', entry.get('sha256', ''))):
            raise ValueError('model inventory mismatch')
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
            raise ValueError('model digest mismatch')
    return manifest


def _fingerprint(folder):
    return tuple((name, (folder / name).stat().st_size, (folder / name).stat().st_mtime_ns)
                 for name in ('manifest.json', *MODEL_FILES))


class LocalEmbeddingClient:
    transport = 'local'
    MODEL_FILES = MODEL_FILES
    # Local small BGE and cloud M3 have different score distributions.
    similarity_threshold = 0.50

    def __init__(self, folder):
        self.folder = Path(folder).resolve()
        self.model = MODEL_ID + ':onnx-int8'
        try:
            manifest_path = self.folder / 'manifest.json'
            if manifest_path.stat().st_size <= 65536:
                self.model += ':' + hashlib.sha256(manifest_path.read_bytes()).hexdigest()[:16]
        except OSError:
            pass
        self._evidence = ContextVar('local_embedding_evidence', default=None)
        self._core = None
        self._failed = False

    @property
    def enabled(self):
        return not self._failed and (self.folder / 'manifest.json').is_file()

    @property
    def evidence(self):
        return dict(self._evidence.get() or {'attempted': False, 'reason': 'not_requested', 'transport': 'local'})

    def _load(self):
        current = _fingerprint(self.folder)
        if self._core is not None:
            if self._core['fingerprint'] != current: raise ValueError('model changed; reload required')
            return self._core
        with _CORE_LOCK:
            key = (str(self.folder), current)
            if key not in _CORES:
                manifest = verify_model(self.folder)
                # Optional imports only after explicit local configuration/model discovery.
                import numpy as np
                import onnxruntime as ort
                from tokenizers import Tokenizer
                ort.disable_telemetry_events()
                options = ort.SessionOptions()
                options.intra_op_num_threads = 2
                options.inter_op_num_threads = 1
                options.log_severity_level = 3
                options.enable_mem_pattern = True
                raw = (self.folder / 'model_quantized.onnx').read_bytes()
                tokenizer_raw = (self.folder / 'tokenizer.json').read_bytes()
                for name, value in [('model_quantized.onnx', raw), ('tokenizer.json', tokenizer_raw)]:
                    if hashlib.sha256(value).hexdigest() != manifest['files'][name]['sha256']:
                        raise ValueError('model changed during load')
                # bytes input forbids implicit external-data files / model hub loading.
                session = ort.InferenceSession(raw, sess_options=options, providers=['CPUExecutionProvider'])
                tokenizer = Tokenizer.from_str(tokenizer_raw.decode('utf-8'))
                tokenizer.no_truncation()
                tokenizer.enable_padding(pad_id=0, pad_token='[PAD]')
                if {v.name for v in session.get_inputs()} - {'input_ids', 'attention_mask', 'token_type_ids'}:
                    raise ValueError('unexpected model inputs')
                _CORES[key] = {'fingerprint': current, 'session': session, 'tokenizer': tokenizer,
                               'np': np, 'lock': threading.Lock(),
                               'revision': manifest['revision'],
                               'model_sha256': manifest['files']['model_quantized.onnx']['sha256']}
            self._core = _CORES[key]
        return self._core

    def embed_query(self, text):
        return self._embed([text], query=True)

    def embed(self, texts):
        return self._embed(texts, query=False)

    def _embed(self, texts, *, query):
        # Runtime import avoids a module cycle and reuses the existing privacy gate.
        from knowledge_service import EmbeddingClient, _private, _unsafe
        evidence = {'attempted': False, 'reason': 'invalid_input', 'transport': 'local'}
        self._evidence.set(evidence)
        if (not isinstance(texts, list) or not 1 <= len(texts) <= 16 or
            any(not isinstance(t, str) or not 0 < len(t) <= 2000 or _private(t) or _unsafe(t) for t in texts) or
            sum(len(t) for t in texts) > 16000): return None
        started = time.monotonic()
        try:
            core = self._load()
            np = core['np']
            with core['lock']:
                inputs = [QUERY_PREFIX + t if query else t for t in texts]
                tokens = core['tokenizer'].encode_batch(inputs)
                if any(len(t.ids)>512 for t in tokens):
                    evidence['reason']='local_input_too_long'
                    return None
                feeds = {'input_ids': np.array([t.ids for t in tokens], dtype=np.int64),
                         'attention_mask': np.array([t.attention_mask for t in tokens], dtype=np.int64),
                         'token_type_ids': np.array([t.type_ids for t in tokens], dtype=np.int64)}
                expected = {v.name for v in core['session'].get_inputs()}
                evidence['attempted'] = True
                outputs = core['session'].run(None, {k: v for k, v in feeds.items() if k in expected})
                # BGE uses the first (CLS) token, followed by L2 normalization.
                values = outputs[0]
                if values.ndim == 3: values = values[:, 0, :]
                if values.shape != (len(texts), 512): raise ValueError('unexpected embedding shape')
                vectors = [EmbeddingClient.vector(row.tolist()) for row in values]
            if _fingerprint(self.folder) != core['fingerprint']: raise ValueError('model changed')
            evidence.update(reason='accepted', dimension=512, revision=core['revision'],
                            model_sha256=core['model_sha256'], query_prefix=query)
            return vectors
        except Exception:
            # Missing optional runtime / corrupt model must not expose raw paths or inputs.
            self._failed = True
            evidence['reason'] = 'local_model_unavailable'
            return None
        finally:
            evidence['elapsed_ms'] = max(0, int((time.monotonic() - started) * 1000))
            self._evidence.set(evidence)
