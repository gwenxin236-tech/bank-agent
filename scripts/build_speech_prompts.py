"""Build public UI prompt audio once; never used in the bank request path.

Requires the separately approved optional edge-tts==7.3.1 package and network.
Only this fixed vocabulary is sent. No accounts, amounts or user text are inputs.
"""
import argparse
import asyncio
import base64
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VOICES = ('zh-CN-XiaoxiaoNeural', 'zh-CN-YunxiNeural')
PROMPTS = (
    '查询余额', '看消费', '转账', '看自动扣费', '消费体检', '家庭共管', '反诈守护', '卡片管理',
    '已拦截，请核实', '请验证身份', '验证通过，请确认办理', '已完成', '处理中',
    '结果待核实', '部分完成，请查看结果', '未完成，请查看原因', '请查看结果',
    '请补充信息', '请确认办理', '连接失败', '结果待核实，请勿重复办理', '暂时无法验证身份',
)


async def build(proxy):
    import edge_tts
    slots = asyncio.Semaphore(2)

    async def clip(voice, text):
        async with slots:
            audio = bytearray()
            async with asyncio.timeout(30):
                async for chunk in edge_tts.Communicate(text, voice, rate='+0%', proxy=proxy).stream():
                    if chunk['type'] == 'audio':
                        audio.extend(chunk['data'])
            if not 32 < len(audio) < 200000 or not (audio.startswith(b'ID3') or audio[0] == 255):
                raise ValueError('invalid generated audio')
            print(voice, text, len(audio), flush=True)
            return voice, text, {'mp3': base64.b64encode(audio).decode('ascii'),
                                 'sha256': hashlib.sha256(audio).hexdigest()}

    clips = await asyncio.gather(*(clip(v, t) for v in VOICES for t in PROMPTS))
    result = {'version': 1, 'source': 'Microsoft Edge speech via edge-tts 7.3.1',
              'scope': 'Public fixed UI phrases only; no user or bank data',
              'rate': 0, 'voices': {v: {} for v in VOICES}}
    for voice, text, data in clips:
        result['voices'][voice][text] = data
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--proxy', help='Optional existing HTTP proxy; not persisted in output')
    parser.add_argument('--output', type=Path, default=ROOT / 'app/static/speech-prompts.json')
    args = parser.parse_args()
    data = asyncio.run(build(args.proxy))
    args.output.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')
    print('Written', args.output, args.output.stat().st_size)
