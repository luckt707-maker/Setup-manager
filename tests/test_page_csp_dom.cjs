// The page must start with its script loaded from /static (no inline code).
// 4.7's privacy card and the file watch must work and render as text, and the
// file watch must be off until started, poll only while running and visible,
// show the "it can slow your PC" banner, and remind after 30 minutes.
// Run: node tests/test_page_csp_dom.cjs <project root>   (needs jsdom)
const fs = require('fs');
const assert = require('node:assert/strict');
const {JSDOM, VirtualConsole, requestInterceptor} = require('jsdom');
(async () => {
  const root = process.argv[2];
  const html = fs.readFileSync(root + '/static/index.html', 'utf8');
  const tags = html.match(/<script\b[^>]*>/g);
  assert.equal(tags.length, 1);
  const src = tags[0].match(/^<script src="(\/static\/dashboard\.js(?:\?v=[\w.-]+)?)">$/);
  assert.ok(src, 'the only script is /static/dashboard.js: ' + tags[0]);
  assert.doesNotMatch(html, /<[a-z][^>]*\son[a-z]+\s*=/i, 'inline handler in the page');
  const scriptUrl = 'http://127.0.0.1:8010' + src[1];

  // Serve the script from disk; nothing else may be fetched.
  const requested = [];
  const loader = requestInterceptor(request => {
    requested.push(request.url);
    if (request.url !== scriptUrl) return new Response('', {status: 404});
    return new Response(fs.readFileSync(root + '/static/dashboard.js', 'utf8'),
      {headers: {'Content-Type': 'application/javascript'}});
  });
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', e => errors.push(e));
  const xss = '<img src=x onerror="window.pwned=1">';
  const calls = [];
  const watch = {running: false, started_at: null, running_seconds: 0, auto_stopped: false,
    summary: 'The file watch is off. It uses no CPU until you start it.', alerts: []};
  const intervals = new Map();
  let hidden = false;
  const dom = new JSDOM(html, {
    url: 'http://127.0.0.1:8010/', runScripts: 'dangerously', resources: {interceptors: [loader]}, virtualConsole,
    beforeParse(win) {
      win.fetch = async (url, opt) => {
        const method = (opt && opt.method) || 'GET';
        calls.push({url: String(url), method, headers: (opt && opt.headers) || {}});
        let data = {};
        if (url === '/api/config') data = {version: 'test', safe_root: '/p', roots: []};
        if (String(url).startsWith('/projects') || String(url).startsWith('/tasks')) data = [];
        if (url === '/privacy-check') data = {summary: 'Summary ' + xss,
          findings: [{status: 'warn', title: xss, where: xss, detail: xss}], checklist: [xss]};
        if (url === '/file-watch/start') Object.assign(watch, {running: true, started_at: 't1', running_seconds: 0,
          summary: 'Watch ' + xss, alerts: [{id: 'a1', kind: 'file', when: 'already', title: xss, where: xss, detail: xss}]});
        if (url === '/file-watch/stop') Object.assign(watch, {running: false, started_at: null, auto_stopped: false,
          summary: 'The file watch is off.'});
        if (String(url).startsWith('/file-watch')) data = JSON.parse(JSON.stringify(watch));
        return {ok: true, status: 200, json: async () => data};
      };
      // Record the 8-second poll timer instead of waiting for it.
      let next = 1;
      win.setInterval = (fn, ms) => { const id = next++; intervals.set(id, {fn, ms}); return id; };
      win.clearInterval = id => intervals.delete(id);
      Object.defineProperty(win.document, 'hidden', {configurable: true, get: () => hidden});
      win.HTMLElement.prototype.scrollIntoView = () => {};
    },
  });
  const win = dom.window;
  const doc = win.document;
  const wait = (ms = 30) => new Promise(r => setTimeout(r, ms));
  await new Promise(r => win.addEventListener('load', r));
  await wait(50);
  assert.deepEqual(requested, [scriptUrl]);
  assert.equal(errors.length, 0, errors.map(e => e.message + ' ' + (e.detail && e.detail.stack)).join('\n'));
  const config = calls.find(c => c.url === '/api/config');
  assert.ok(config, 'the dashboard script started');
  assert.equal(config.headers['X-Setup-Token'], '__SM_TOKEN__', 'the token comes from the meta tag');

  // --- File watch: off on load, read once, no poll timer ---
  const watchPolls = () => calls.filter(c => c.url === '/file-watch').length;
  const pollTimers = () => [...intervals.values()].filter(t => t.ms === 8000).length;
  const banner = doc.getElementById('fileWatchBanner');
  const toggle = doc.getElementById('fileWatchToggle');
  assert.equal(watchPolls(), 1, 'the page reads the watch once on load');
  assert.equal(pollTimers(), 0, 'no polling while the watch is off');
  assert.ok(banner.hidden, 'no banner while off');
  assert.equal(toggle.textContent, 'Start watching');
  assert.ok(!calls.some(c => c.url === '/file-watch/start'), 'the watch never starts by itself');

  // --- Start ---
  toggle.click();
  await wait();
  const start = calls.find(c => c.url === '/file-watch/start');
  assert.ok(start && start.method === 'POST' && start.headers['X-Setup-Token'], 'start is a POST with the token');
  assert.ok(!banner.hidden, 'the banner shows while running');
  assert.match(banner.textContent, /File watch is running — it checks every 8 seconds and can slow down your PC\. Stop it when you're done\./);
  assert.equal(toggle.textContent, 'Stop watching');
  assert.equal(pollTimers(), 1, 'polls while running');
  const alertsBox = doc.getElementById('fileWatchAlerts');
  assert.ok(alertsBox.textContent.includes('Already here: <img'), 'file watch alerts are shown');
  assert.equal(alertsBox.querySelector('img'), null, 'file watch alert became HTML');
  assert.ok(doc.getElementById('fileWatchStatus').textContent.includes('Watch <img'));

  // --- Hidden page: polling pauses ---
  hidden = true;
  doc.dispatchEvent(new win.Event('visibilitychange'));
  assert.equal(pollTimers(), 0, 'no polling while the page is hidden');
  // The server stopped it while the page was away.
  Object.assign(watch, {running: false, started_at: null, auto_stopped: true,
    stopped_reason: 'The Setup Manager page was closed or hidden, so the watch stopped to keep your PC fast.',
    summary: 'Stopped automatically. The Setup Manager page was closed or hidden.'});
  hidden = false;
  doc.dispatchEvent(new win.Event('visibilitychange'));
  await wait();
  assert.match(doc.getElementById('fileWatchStatus').textContent, /Stopped automatically/);
  assert.ok(banner.hidden);
  assert.equal(toggle.textContent, 'Start watching');
  assert.equal(pollTimers(), 0);

  // --- 30-minute reminder, then stop from the banner ---
  toggle.click();
  await wait();
  const reminder = doc.getElementById('fileWatchReminder');
  assert.ok(!reminder.hasAttribute('open'), 'no reminder at the start');
  watch.running_seconds = 30 * 60;
  for (const t of intervals.values()) if (t.ms === 8000) await t.fn();
  await wait();
  assert.ok(reminder.hasAttribute('open'), 'a reminder after 30 minutes');
  assert.match(reminder.textContent, /slow down your PC/);
  doc.getElementById('fileWatchKeep').click();
  assert.ok(!reminder.hasAttribute('open'));
  for (const t of intervals.values()) if (t.ms === 8000) await t.fn();
  await wait();
  assert.ok(!reminder.hasAttribute('open'), 'the reminder shows once per run');
  doc.getElementById('fileWatchBannerStop').click();
  await wait();
  const stop = calls.find(c => c.url === '/file-watch/stop');
  assert.ok(stop && stop.method === 'POST' && stop.headers['X-Setup-Token'], 'stop is a POST with the token');
  assert.ok(banner.hidden);
  assert.equal(pollTimers(), 0, 'polling ends with the watch');

  // --- Privacy card ---
  doc.getElementById('privacyBtn').click();
  await wait();
  const box = doc.getElementById('privacyResults');
  assert.ok(box.textContent.includes('Summary <img'), 'privacy results are shown');
  assert.equal(box.querySelector('img'), null, 'privacy results became HTML');
  assert.equal(win.pwned, undefined);
  assert.equal(doc.getElementById('privacyBtn').textContent, 'Check this PC');
  console.log('PASS: page runs from /static/dashboard.js; file watch is off until started, polls only while running and visible, shows the banner and reminder; privacy card renders as text');
  win.close();
})().catch(e => { console.error(e.stack || e.message); process.exitCode = 1; });
