// One-off: browser playback validation for the finalized competition video.
import fs from 'node:fs/promises';
import {createReadStream} from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createRequire} from 'node:module';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(import.meta.url);
const {chromium} = require(path.join(root, '.qa-tools/node_modules/playwright'));
const video = process.argv[2];
if (!video) throw Error('usage: node verify_video_playback.mjs <mp4>');
const stat = await fs.stat(video);

const server = http.createServer(async (req, res) => {
  const range = req.headers.range;
  if (range) {
    const match = range.match(/bytes=(\d*)-(\d*)/);
    let start = match[1] ? Number(match[1]) : 0;
    let end = match[2] ? Number(match[2]) : stat.size - 1;
    if (start >= stat.size || end >= stat.size || start > end) {
      res.writeHead(416, {'Content-Range': `bytes */${stat.size}`});
      return res.end();
    }
    res.writeHead(206, {'Content-Type': 'video/mp4', 'Content-Range': `bytes ${start}-${end}/${stat.size}`,
                        'Content-Length': end - start + 1, 'Accept-Ranges': 'bytes'});
    return createReadStream(video, {start, end}).pipe(res);
  }
  res.writeHead(200, {'Content-Type': 'video/mp4', 'Content-Length': stat.size, 'Accept-Ranges': 'bytes'});
  createReadStream(video).pipe(res);
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const port = server.address().port;

const browser = await chromium.launch({headless: true});
const page = await browser.newPage({viewport: {width: 1280, height: 800}});
const errors = [];
page.on('pageerror', e => errors.push(e.message));
await page.setContent('<video id="v" src="http://127.0.0.1:' + port + '/demo.mp4" controls preload="auto"></video>');
const info = await page.evaluate(async () => {
  const v = document.getElementById('v');
  await new Promise((resolve, reject) => {
    if (v.readyState >= 1) return resolve();
    v.addEventListener('loadedmetadata', resolve, {once: true});
    v.addEventListener('error', () => reject(Error('video load error')), {once: true});
    setTimeout(() => reject(Error('metadata timeout')), 20000);
  });
  v.currentTime = Math.min(130, v.duration / 2);
  await v.play().catch(e => { throw Error('play failed: ' + e.message); });
  await new Promise(r => setTimeout(r, 2500));
  const played = {playing_time: v.currentTime, paused_during_play: v.paused};
  v.pause();
  return {duration: v.duration, readyState: v.readyState, paused: v.paused,
          videoWidth: v.videoWidth, videoHeight: v.videoHeight, ...played};
});
const seeked = await page.evaluate(async () => {
  const v = document.getElementById('v');
  const target = v.duration - 1;
  v.currentTime = target;
  await new Promise(r => v.addEventListener('seeked', r, {once: true}));
  await new Promise(r => setTimeout(r, 300));
  return {seek_ok: Math.abs(v.currentTime - target) < 1.5, seeked_time: v.currentTime};
});
await browser.close();
server.close();
const report = {file: path.basename(video), bytes: stat.size, sha256: (await import('node:crypto')).createHash('sha256')
  .update(await fs.readFile(video)).digest('hex'), ...info, ...seeked, page_errors: errors,
  status: (errors.length === 0 && info.readyState === 4 && info.playing_time > 0 && !info.paused_during_play && seeked.seek_ok) ? 'PASS' : 'FAIL'};
await fs.writeFile(path.join(root, 'data/verification/yinban-video-20261101/playback-check.json'),
  JSON.stringify(report, null, 2) + '\n');
console.log(JSON.stringify(report, null, 1));
if (report.status !== 'PASS') process.exit(1);
