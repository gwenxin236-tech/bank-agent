#!/usr/bin/env python3
"""Authoring only: local Windows narration and H.264/AAC, no network or installs.

The application does not import this helper. It never records a microphone,
reads credentials, changes system settings, or sends text to a provider.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import wave


NARRATION = {
    '开场': '银伴，银行智能服务助手。平台授权，工具提供事实。',
    '绿色查询': '绿色查询自动执行。账户余额来自模拟银行工具，模型不能编造金额或交易结果。',
    '上下文与黄色确认': '先补齐收款人和金额，再核对原计划。领域智能体提供建议，用户明确确认后才执行这笔模拟转账。',
    '订阅查询': '订阅查询也需要黄色确认。状态刷新不预读订阅，确认后才读取，查询不会产生扣款。',
    '红色演示认证': '高风险转账需要认证，认证通过后仍须再次确认。本段使用固定演示验证码，不代表真实短信或人脸认证。',
    '卡片管理': '卡片挂失和解挂都经过红色认证。完成后重新查询卡片状态，不把界面提示当成银行事实，也不会扣款。',
    '生日联动': '生日任务先确认并锁定预算。鲜花提前两天，蛋糕提前一天，日期可分别指定。暂停和取消遵守任务状态，取消后仅执行已声明的解锁补偿。',
    '密码与TOTP': '可选身份模式使用本地密码和动态口令。画面是自动化验证，不代表真人或生产认证。登录状态保存在受保护的会话中。',
    '注入与审计': '试图绕过确认的提示被拦截。审计记录本次决策链路；有限攻击语料通过，不等于能够识别所有攻击。',
    '本地RAG与独立重排': '知识检索在本机生成向量，经过独立重排后给出来源引用。资料只供参考，不能授予交易权限。',
    '本地OCR与核对草稿': '本地模型识别合成图片。用户核对字段后只填入草稿，识别和核对都不会直接转账。',
    '受控动态DAG': '原业务步骤不能删。监督通过后撤销旧确认，新计划仍须确认，采纳不等于执行。',
    '自建双端交流': '用户端和自建坐席双向收发消息。本段由脚本操作，本地工单结单不会改变交易，也不是银行官方客服。',
    '支付受阻与交接': '超过日限额时保留原任务，提供核对与交接。不自动拆单，也不新增第二笔付款。',
    '交易异议跟进': '交易事实与用户主张分别保存。这里是本地核对工单，银行正式受理、保护措施和退款仍需各自核实。',
    '期限资金准备': '工具核对期限、保留资金和当前缺口。预计到账不作为确定可用资金，不会自动借款或赎回。',
    '结尾': '源码已提供评审入口。演示使用虚构资金，真人、异机和生产接入另行验收。',
}


def atoms(data, start=0, end=None):
    end = len(data) if end is None else end
    while start < end:
        if start + 8 > end:
            raise ValueError('truncated MP4 atom')
        size, kind = struct.unpack_from('>I4s', data, start)
        header = 8
        if size == 1:
            if start + 16 > end:
                raise ValueError('truncated extended atom')
            size, header = struct.unpack_from('>Q', data, start + 8)[0], 16
        if size == 0:
            size = end - start
        if size < header or start + size > end:
            raise ValueError('invalid MP4 atom size')
        yield kind, start, start + header, start + size
        start += size


def faststart(data):
    roots = list(atoms(data))
    if not roots or roots[0][0] != b'ftyp' or any(k == b'moof' for k, *_ in roots):
        raise ValueError('only unfragmented MP4 is supported')
    movies = [a for a in roots if a[0] == b'moov']
    media = [a for a in roots if a[0] == b'mdat']
    if len(movies) != 1 or not media:
        raise ValueError('one movie and media data required')
    _, moov, body, end = movies[0]
    first_media = min(a[1] for a in media)
    if moov < first_media:
        return data
    changed = bytearray(data[moov:end])
    delta, tables = end - moov, 0

    def adjust(lo, hi):
        nonlocal tables
        for kind, _, payload, stop in atoms(changed, lo, hi):
            if kind == b'mvex':
                raise ValueError('fragmented movie rejected')
            if kind in {b'trak', b'mdia', b'minf', b'stbl'}:
                adjust(payload, stop)
            elif kind in {b'stco', b'co64'}:
                if payload + 8 > stop:
                    raise ValueError('invalid chunk table')
                count = struct.unpack_from('>I', changed, payload + 4)[0]
                width, fmt = (4, '>I') if kind == b'stco' else (8, '>Q')
                if count > 1000000 or payload + 8 + count * width != stop:
                    raise ValueError('invalid chunk offsets')
                for i in range(count):
                    at = payload + 8 + i * width
                    old = struct.unpack_from(fmt, changed, at)[0]
                    new = old + delta if first_media <= old < moov else old
                    struct.pack_into(fmt, changed, at, new)
                tables += 1

    adjust(body - moov, end - moov)
    if not tables:
        raise ValueError('movie chunk offsets missing')
    result = data[:first_media] + bytes(changed) + data[first_media:moov] + data[end:]
    assert len(result) == len(data)
    return result


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def powershell(code, log, timeout=900):
    executable = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    # -Command executes the reviewed local code without changing execution policy.
    process = subprocess.run([str(executable), '-NoProfile', '-Command',
        "$ErrorActionPreference='Stop';[Console]::OutputEncoding=[Text.UTF8Encoding]::new();\n" + code],
        capture_output=True, timeout=timeout)
    Path(log).write_bytes(process.stdout + process.stderr)
    if process.returncode:
        raise RuntimeError('Native authoring failed; inspect ' + str(log))


WINRT = r'''
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null=[Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]
$null=[Windows.Media.Editing.MediaComposition,Windows.Media.Editing,ContentType=WindowsRuntime]
$null=[Windows.Media.Editing.MediaClip,Windows.Media.Editing,ContentType=WindowsRuntime]
$null=[Windows.Media.Editing.BackgroundAudioTrack,Windows.Media.Editing,ContentType=WindowsRuntime]
$null=[Windows.Media.MediaProperties.MediaEncodingProfile,Windows.Media,ContentType=WindowsRuntime]
function Await-Operation($operation,[Type]$resultType){
 $m=[System.WindowsRuntimeSystemExtensions].GetMethods()|Where-Object {$_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'}|Select-Object -First 1
 $t=$m.MakeGenericMethod($resultType).Invoke($null,@($operation));$t.Wait();return $t.Result
}
function Await-ProgressOperation($operation,[Type]$resultType){
 $m=[System.WindowsRuntimeSystemExtensions].GetMethods()|Where-Object {$_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and $_.GetGenericArguments().Count -eq 2 -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperationWithProgress`2'}|Select-Object -First 1
 $t=$m.MakeGenericMethod($resultType,[double]).Invoke($null,@($operation));$t.Wait();return $t.Result
}
'''


def finalize(source, scene_report, destination, work):
    if os.name != 'nt':
        raise ValueError('This optional authoring helper uses Windows built-in media components')
    source, destination, work = Path(source).resolve(), Path(destination).resolve(), Path(work).resolve()
    if destination.exists() or work.exists():
        raise ValueError('fresh output and work directory required')
    report = json.loads(Path(scene_report).read_text(encoding='utf-8-sig'))
    duration = report['video']['duration_seconds']
    scenes = report['scenes']
    if not 0 < duration <= 300 or not 1 <= len(scenes) <= 32:
        raise ValueError('invalid video timeline')
    work.mkdir(parents=True)
    offset = max(0, min(5, duration - report['elapsed_seconds']))
    items = [{'name': s['name'], 'text': NARRATION[s['name']],
              'start': s['start_seconds'] + offset + .35,
              'end': scenes[i + 1]['start_seconds'] + offset if i + 1 < len(scenes) else duration,
              'path': str(work / ('voice-' + str(i).zfill(2) + '.wav'))}
             for i, s in enumerate(scenes)]
    instructions = work / 'narration.json'
    instructions.write_bytes(json.dumps(items, ensure_ascii=False, indent=2).encode())
    code = '$items=Get-Content -LiteralPath ' + ps_quote(instructions) + r''' -Raw -Encoding UTF8|ConvertFrom-Json
$voice=New-Object -ComObject SAPI.SpVoice
$voice.Voice=($voice.GetVoices()|Where-Object {$_.GetDescription() -like '*Huihui*'}|Select-Object -First 1)
$voice.Rate=3
foreach($item in $items){
 $stream=New-Object -ComObject SAPI.SpFileStream
 $stream.Open($item.path,3,$false);$voice.AudioOutputStream=$stream
 try{$null=$voice.Speak($item.text)}finally{$stream.Close()}
}
'''
    powershell(code, work / 'narration.log', timeout=120)
    params, chunks = None, []
    for item in items:
        with wave.open(item['path']) as sound:
            actual = (sound.getnchannels(), sound.getsampwidth(), sound.getframerate())
            if params is not None and params != actual:
                raise ValueError('narration audio formats differ')
            params = actual
            content = sound.readframes(sound.getnframes())
            seconds = sound.getnframes() / sound.getframerate()
        if seconds <= 0 or item['start'] + seconds > item['end'] - .05:
            raise ValueError('Narration does not fit scene: ' + item['name'])
        item['speech_seconds'] = seconds
        chunks.append((int(item['start'] * params[2]), content))
    width = params[0] * params[1]
    pcm = bytearray(round(duration * params[2]) * width)
    for start, content in chunks:
        pcm[start * width:start * width + len(content)] = content
    narration = work / 'narration.wav'
    with wave.open(str(narration), 'wb') as audio:
        audio.setnchannels(params[0]); audio.setsampwidth(params[1]); audio.setframerate(params[2])
        audio.writeframes(pcm)
    native = work / 'native-h264-aac.mp4'
    code = WINRT
    for name, path in [('video', source), ('audio', narration)]:
        code += '$' + name + '=Await-Operation ([Windows.Storage.StorageFile]::GetFileFromPathAsync(' + ps_quote(path) + ')) ([Windows.Storage.StorageFile])\n'
    code += '$folder=Await-Operation ([Windows.Storage.StorageFolder]::GetFolderFromPathAsync(' + ps_quote(work) + ')) ([Windows.Storage.StorageFolder])\n'
    code += r'''
$dest=Await-Operation ($folder.CreateFileAsync('native-h264-aac.mp4',[Windows.Storage.CreationCollisionOption]::FailIfExists)) ([Windows.Storage.StorageFile])
$composition=New-Object Windows.Media.Editing.MediaComposition
$clip=Await-Operation ([Windows.Media.Editing.MediaClip]::CreateFromFileAsync($video)) ([Windows.Media.Editing.MediaClip])
[System.Collections.Generic.ICollection[Windows.Media.Editing.MediaClip]].GetMethod('Add').Invoke($composition.Clips,@($clip))
$track=Await-Operation ([Windows.Media.Editing.BackgroundAudioTrack]::CreateFromFileAsync($audio)) ([Windows.Media.Editing.BackgroundAudioTrack])
[System.Collections.Generic.ICollection[Windows.Media.Editing.BackgroundAudioTrack]].GetMethod('Add').Invoke($composition.BackgroundAudioTracks,@($track))
$profile=[Windows.Media.MediaProperties.MediaEncodingProfile]::CreateMp4([Windows.Media.MediaProperties.VideoEncodingQuality]::HD1080p)
$profile.Video.Width=1440;$profile.Video.Height=1000;$profile.Video.Bitrate=2000000
$profile.Video.FrameRate.Numerator=25;$profile.Video.FrameRate.Denominator=1
$profile.Audio.SampleRate=44100;$profile.Audio.ChannelCount=1;$profile.Audio.Bitrate=96000
$reason=Await-ProgressOperation ($composition.RenderToFileAsync($dest,[Windows.Media.Editing.MediaTrimmingPreference]::Precise,$profile)) ([Windows.Media.Transcoding.TranscodeFailureReason])
if($reason -ne [Windows.Media.Transcoding.TranscodeFailureReason]::None){throw 'Native encoding failed'}
'''
    powershell(code, work / 'encoding.log')
    encoded = native.read_bytes()
    if b'avc1' not in encoded or b'mp4a' not in encoded:
        raise ValueError('H.264 and AAC tracks required')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('xb') as stream:
        stream.write(faststart(encoded))
    result = {'status': 'PASS_LOCAL_ENCODING_PLAYBACK_CHECK_SEPARATE',
              'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
              'output_sha256': hashlib.sha256(destination.read_bytes()).hexdigest(),
              'codec': 'H.264/AVC + AAC', 'faststart': True, 'audio': True,
              'duration_seconds': duration, 'voice': 'Windows Huihui Desktop, local SAPI',
              'cloud_calls': 0, 'dependencies_installed': False, 'narration': items}
    (work / 'report.json').write_bytes(json.dumps(result, ensure_ascii=False, indent=2).encode())
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--scene-report', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    result = finalize(args.input, args.scene_report, args.output, args.work)
    print(json.dumps({k: result[k] for k in ('status', 'codec', 'audio', 'faststart', 'duration_seconds')}, ensure_ascii=True))
