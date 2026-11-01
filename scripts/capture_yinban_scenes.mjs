// One-off: re-capture three document screenshots against the renamed (银伴) UI.
// Offline Mock, stdlib server only; no AI model imports, no cloud calls.
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {fileURLToPath} from 'node:url';
import {createRequire} from 'node:module';
import {spawn} from 'node:child_process';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const outDir = path.join(root, 'data/verification/rename-yinban-20261101/scenes');
await fs.mkdir(outDir, {recursive: true});
const require = createRequire(import.meta.url);
const {chromium} = require(path.join(root, '.qa-tools/node_modules/playwright'));

const db = await fs.mkdtemp(path.join(os.tmpdir(), 'yinban-shots-'));
const bootstrap = `import sys,threading,os
sys.path.insert(0,os.path.join(os.getcwd(),'app'))
import banking_agent as ba
srv=ba.ThreadingHTTPServer(('127.0.0.1',0),ba.Handler)
t=threading.Thread(target=srv.serve_forever,daemon=True);t.start()
print('PORT='+str(srv.server_port),flush=True)
sys.stdin.readline()
srv.shutdown();srv.server_close();t.join()
ba.runtime.recovery_scheduler.stop();ba.runtime.bank.close();ba.runtime.audit.db.close()
`;
const safeEnv = {...process.env};
for (const name of Object.keys(safeEnv)) if (name.endsWith('_API_KEY') || name.endsWith('_ACCESS_TOKEN')) delete safeEnv[name];
const server = spawn('python', ['-u', '-c', bootstrap], {cwd: root, env: {...safeEnv,
  BANK_AGENT_DB_PATH: path.join(db, 'demo.sqlite3'), BANK_AGENT_OFFLINE: '1', BANK_SERVICE_MODE: 'mock',
  APP_ENV: 'demo', VOICE_ENABLED: '0', OCR_ENABLED: '0', RAG_USE_MODEL: '0', RAG_USE_RERANKER: '0',
  IDENTITY_MODE: 'demo', PYTHONDONTWRITEBYTECODE: '1', PYTHONIOENCODING: 'utf-8'}, stdio: ['pipe', 'pipe', 'pipe']});
let logs = '';
server.stderr.on('data', b => logs += b.toString('utf8'));
const port = await new Promise((resolve, reject) => {
  let output = '';
  const timer = setTimeout(() => reject(Error('server startup timeout ' + logs)), 30000);
  server.stdout.on('data', b => { output += b.toString('utf8'); const m = output.match(/PORT=(\d+)/); if (m) { clearTimeout(timer); resolve(Number(m[1])); } });
  server.once('exit', code => { clearTimeout(timer); reject(Error('server exit ' + code + ' ' + logs)); });
});
const base = 'http://127.0.0.1:' + port;
const report = {status: 'FAIL', captures: [], errors: []};
let browser, context;
const headers = {Authorization: 'Bearer demo-user-session'};
try {
  browser = await chromium.launch({headless: true});
  context = await browser.newContext({viewport: {width: 1440, height: 1000}});
  const page = await context.newPage();
  page.on('pageerror', e => report.errors.push(e.message));
  const external = [];
  await context.route('**/*', route => {
    if (!new Set([base]).has(new URL(route.request().url()).origin)) { external.push(route.request().url()); return route.abort(); }
    return route.continue();
  });
  await page.addInitScript(() => { localStorage.setItem('elderMode', 'false'); localStorage.setItem('voicePrivacy', 'true'); });
  await page.goto(base);
  await page.waitForFunction(() => !document.querySelector('#balanceValue').textContent.includes('读取'));
  const state = async () => await (await page.request.get(base + '/api/state', {headers})).json();
  const send = async (text) => {
    await page.locator('#messageInput').fill(text);
    const response = page.waitForResponse(r => r.url() === base + '/api/chat' && r.request().method() === 'POST', {timeout: 60000});
    await page.locator('#sendBtn').click();
    const data = await (await response).json();
    await page.waitForFunction(() => !document.querySelector('#sendBtn').disabled);
    return data;
  };
  const confirm = async () => {
    await page.locator('#confirmBtn').evaluate(el => el.scrollIntoView({block: 'center', behavior: 'instant'}));
    const response = page.waitForResponse(r => r.url() === base + '/api/confirm');
    await page.locator('#confirmBtn').click();
    const data = await (await response).json();
    await page.waitForTimeout(500);
    return data;
  };
  const installCaption = async () => {
    await page.addStyleTag({content: 'body{padding-bottom:128px!important} #t16caption{position:fixed;left:0;right:0;bottom:0;z-index:99999;background:#103c36;color:#fff;padding:18px 38px;font:24px/1.45 "Microsoft YaHei",sans-serif;min-height:84px;box-sizing:border-box;border-top:4px solid #91c5b4;pointer-events:none} #t16caption small{font-size:17px;color:#bde1d5;display:block;margin-bottom:4px}'});
    await page.evaluate(() => { const node = document.createElement('div'); node.id = 't16caption'; document.body.appendChild(node); });
  };
  const caption = async (title, text) => {
    await page.locator('#t16caption').evaluate((node, {title, text}) => {
      node.replaceChildren();
      const label = document.createElement('small'); label.textContent = '离线 Mock 实录 · ' + title;
      const body = document.createElement('span'); body.textContent = text; node.append(label, body);
    }, {title, text});
  };
  const shot = async (name) => { const p = path.join(outDir, name + '.png'); await page.screenshot({path: p}); report.captures.push({name, file: path.relative(root, p)}); };

  // 1. Homepage (plain, no caption) — replaces t20 scenes/homepage.png
  await shot('homepage');

  // Guarantee at least one completed transaction exists for the dispute scene.
  const before = (await state()).balance.available_cents;
  await send('给妈妈转账');
  const yellow = await send('200元');
  if (yellow.plan && yellow.plan.risk_level === 'yellow') {
    const paid = await confirm();
    if (!paid.result || paid.result.status !== 'SUCCESS') throw Error('warm-up transfer failed');
  }
  report.warmup_debit = before - (await state()).balance.available_cents;

  // 2. B scenario: dispute follow-up — replaces n16 recording/15-dispute.png
  await installCaption();
  await page.locator('#serviceTasksPanel').scrollIntoViewIfNeeded();
  await page.locator('#serviceTransaction').selectOption({index: 1});
  const disputeResponse = page.waitForResponse(r => r.url().endsWith('/api/v1/service-tasks/dispute'));
  await page.locator('#openDisputeBtn').click();
  const dispute = (await (await disputeResponse).json()).service_task;
  if (dispute.official_case_id !== null) throw Error('dispute should not have official case');
  await page.locator('#serviceHandoffBtn').click();
  await page.locator('#serviceTaskDetail').getByText('待人工领取', {exact: false}).waitFor();
  await caption('B · 银行事实与用户主张分离', '选择本人的交易并记录异议；这里只有本地工单，正式银行案件号、保护措施和退款分别待核实。');
  await page.locator('#serviceTaskDetail').scrollIntoViewIfNeeded();
  await page.waitForTimeout(600);
  await shot('15-dispute');

  // 3. C scenario: funding plan — replaces n16 recording/16-funding.png
  await page.locator('#serviceFundingAmount').fill('300000');
  await page.locator('#serviceFundingReserve').fill('10000');
  await page.locator('#serviceFundingDeadline').fill('2026-11-05T18:00');
  const fundingResponse = page.waitForResponse(r => r.url().endsWith('/api/v1/service-tasks/funding'));
  await page.locator('#openFundingBtn').click();
  const funding = (await (await fundingResponse).json()).service_task;
  if (!(funding.constraints.no_borrow && funding.goal.target_cents === 30000000)) throw Error('funding constraints mismatch');
  await page.locator('#serviceTaskDetail').getByText(funding.task_id, {exact: false}).waitFor();
  await page.locator('#serviceRecheckBtn').click();
  await page.locator('#serviceTaskDetail').getByText('不会自动办理付款', {exact: false}).waitFor();
  await caption('C · 期限、保留额与不借款', '目标30万元并至少保留1万元。工具核对当前资金并显示缺口；预计到账不当可用资金，不自动借款或赎回。');
  await page.locator('#serviceTaskDetail').scrollIntoViewIfNeeded();
  await page.waitForTimeout(600);
  await shot('16-funding');

  if (external.length) report.errors.push('external requests: ' + external.join(', '));
  if (!report.errors.length) report.status = 'PASS';
} catch (error) {
  report.error = String(error);
  throw error;
} finally {
  if (context) await context.close().catch(() => {});
  if (browser) await browser.close().catch(() => {});
  server.stdin.end('\n');
  await fs.writeFile(path.join(outDir, 'capture-report.json'), JSON.stringify(report, null, 2)).catch(() => {});
  console.log('Capture:', report.status, outDir);
}
