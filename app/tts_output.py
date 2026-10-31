"""Optional online speech output. No bank tools, disk audio or mandatory dependency."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading

VOICES = {'zh-CN-XiaoxiaoNeural': '晓晓 · 自然女声', 'zh-CN-YunxiNeural': '云希 · 清晰男声'}
MAX_BYTES = 2 * 1024 * 1024
_slots = threading.BoundedSemaphore(2)


def redact_speech(text):
    text = re.sub(r'sk-[A-Za-z0-9_-]+', '密钥已隐藏', text)
    text = re.sub(r'(?:密码|口令|验证码|动态码)\s*[:：=]?\s*[A-Za-z0-9]{4,}', '验证信息已隐藏', text)
    text = re.sub(r'\d+[＊*]+\d+|[＊*]+\d{2,}', '账号已隐藏', text)
    text = re.sub(r'\d{8,}', '账号已隐藏', text)
    return re.sub(r'[¥￥]?\s*\d[\d,.]*\s*元?', '金额或编号已隐藏', text)


class TTSOutput:
    def capabilities(self):
        enabled = os.getenv('TTS_ENABLED') == '1' and os.getenv('BANK_AGENT_OFFLINE') != '1'
        installed = importlib.util.find_spec('edge_tts') is not None
        return {'available': enabled and installed, 'provider': 'Microsoft Edge 在线语音（社区接入）',
                'voices': VOICES, 'cloud_redaction': True, 'audio_persisted': False,
                'reason': 'ready' if enabled and installed else 'disabled' if not enabled else 'missing_dependency'}

    def synthesize(self, text, *, consent=False, voice='zh-CN-XiaoxiaoNeural', rate=-8):
        def failure(reason):
            return {'ok': False, 'reason': reason, 'message': '自然语音暂不可用，请阅读页面文字或选择本机语音。'}
        if consent is not True:
            return failure('consent_required')
        if (not isinstance(text, str) or not 1 <= len(text.strip()) <= 600 or not isinstance(voice, str) or voice not in VOICES
                or type(rate) is not int or rate not in (-20, -8, 0, 10)):
            return failure('invalid_request')
        if not self.capabilities()['available']:
            return failure('unavailable')
        if not _slots.acquire(blocking=False):
            return failure('busy')
        try:
            payload = json.dumps({'text': redact_speech(text), 'voice': voice, 'rate': rate}, ensure_ascii=False).encode('utf-8')
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--worker'], input=payload,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=22,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            audio = result.stdout
            if result.returncode or not 32 < len(audio) <= MAX_BYTES or not (audio.startswith(b'ID3') or audio[0] == 255):
                return failure('provider_error')
            return {'ok': True, 'audio': audio}
        except (OSError, subprocess.TimeoutExpired):
            return failure('provider_unavailable')
        finally:
            _slots.release()


async def _worker(payload):
    import edge_tts  # Optional: imported only in an explicitly enabled, isolated worker.
    voice = payload['voice']; rate = payload['rate']; text = payload['text']
    if voice not in VOICES or type(rate) is not int or rate not in (-20, -8, 0, 10) or len(text) > 1800:
        raise ValueError('invalid request')
    data = bytearray()
    async for chunk in edge_tts.Communicate(text, voice, rate=f'{rate:+d}%').stream():
        if chunk['type'] == 'audio':
            data.extend(chunk['data'])
            if len(data) > MAX_BYTES:
                raise ValueError('audio too large')
    sys.stdout.buffer.write(data)


if __name__ == '__main__':
    try:
        payload = json.loads(sys.stdin.buffer.read(8192).decode('utf-8'))
        asyncio.run(asyncio.wait_for(_worker(payload), timeout=19))
    except Exception:
        sys.exit(1)
