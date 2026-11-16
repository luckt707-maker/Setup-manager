// Opening a folder shows the safety reminder; it can scan, be dismissed,
// and be hidden for one folder. The folder name must stay plain text.
// Run: node tests/test_safety_prompt_dom.cjs <project root>   (needs jsdom)
const fs = require('fs');
const assert = require('node:assert/strict');
const {JSDOM} = require('jsdom');
(async () => {
  const html = fs.readFileSync(process.argv[2] + '/static/index.html', 'utf8');
  const modalHtml = html.match(/<dialog id="safetyModal"[\s\S]*?<\/dialog>/)[0];
  assert.doesNotMatch(modalHtml, /\son\w+=/, 'the pop-up must not use inline handlers');

  const dom = new JSDOM(html, {url: 'http://127.0.0.1:8010/', runScripts: 'outside-only'});
  const win = dom.window;
  const doc = win.document;
  const script = fs.readFileSync(process.argv[2] + '/static/dashboard.js', 'utf8').split('(async()=>{try{cfg=await api')[0];
  const calls = [];
  let folderId = 'id-1';
  win.fetch = async (url, opt) => {
    calls.push({url: String(url), body: opt && opt.body ? JSON.parse(opt.body) : null});
    let data = {};
    if (String(url).startsWith('/projects')) data = [];
    if (url === '/settings/safe-root') data = {safe_root: '/root-two'};
    if (url === '/security-scan') data = {findings: [], files_scanned: 0, malware_scan: {status: 'n/a'}};
    if (url === '/analyze') data = {detected: [], folder_id: folderId};
    return {ok: true, json: async () => data};
  };
  win.confirm = () => true;
  win.eval(script);
  // The rest of the page build is not under test here.
  for (const name of ['buildRun', 'buildSetup', 'buildUtilities', 'refreshGuide', 'statusBar']) win[name] = async () => {};
  const modal = doc.getElementById('safetyModal');
  const isOpen = () => modal.hasAttribute('open');
  const wait = () => new Promise(r => setTimeout(r, 20));

  const evil = '/p/<img src=x onerror="window.pwned=1">';
  await win.select(evil);
  assert.ok(isOpen(), 'opening a project shows the reminder');
  assert.equal(doc.getElementById('safetyFolder').textContent, evil);
  assert.equal(modal.querySelector('img'), null, 'folder name became HTML');
  assert.match(modal.textContent, /not bulletproof/);
  assert.match(modal.textContent, /project's own scripts are not/);
  assert.match(modal.textContent, /Only install or run projects you trust/);

  doc.getElementById('safetyLater').click();
  assert.ok(!isOpen(), 'Not now closes it');
  await win.select(evil);
  assert.ok(isOpen(), 'it comes back without the checkbox');

  doc.getElementById('safetyDontShow').checked = true;
  doc.getElementById('safetyLater').click();
  await win.select(evil);
  assert.ok(!isOpen(), "Don't show again is remembered for that folder");
  folderId = 'id-2';  // the folder was deleted and cloned again
  await win.select(evil);
  assert.ok(isOpen(), 'a re-cloned folder gets the reminder again');
  doc.getElementById('safetyLater').click();

  await win.select('/p/two');
  assert.ok(isOpen(), 'another folder still gets the reminder');
  doc.getElementById('safetyScan').click();
  await wait();
  assert.ok(!isOpen());
  const scan = calls.find(c => c.url === '/security-scan');
  assert.ok(scan, 'Run safety scan now starts the scan');
  assert.equal(scan.body.path, '/p/two');

  calls.length = 0;
  doc.getElementById('securityScanBtn').click();  // the re-scan button still works
  await wait();
  assert.ok(calls.find(c => c.url === '/security-scan'), 're-scan runs again');

  doc.getElementById('manual').value = '/root-two';
  doc.getElementById('setroot').click();
  await wait();
  assert.ok(isOpen(), 'choosing the projects root shows the reminder');
  assert.equal(doc.getElementById('safetyFolder').textContent, '/root-two');
  calls.length = 0;
  doc.getElementById('safetyScan').click();
  await wait();
  assert.equal(calls.find(c => c.url === '/security-scan').body.path, '/root-two');
  assert.equal(win.pwned, undefined);
  console.log('PASS: the safety reminder shows, scans, dismisses and remembers per folder');
  win.close();
})().catch(e => { console.error(e.stack || e.message); process.exitCode = 1; });
