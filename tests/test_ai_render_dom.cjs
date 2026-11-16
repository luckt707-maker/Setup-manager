// Model replies and project data must land in the dashboard as text, never HTML.
// Run: node tests/test_ai_render_dom.cjs <project root>   (needs jsdom)
const fs = require('fs');
const assert = require('node:assert/strict');
const {JSDOM} = require('jsdom');
(async () => {
  const html = fs.readFileSync(process.argv[2] + '/static/index.html', 'utf8');
  const dom = new JSDOM(html, {url: 'http://127.0.0.1:8010/', runScripts: 'outside-only'});
  const win = dom.window;
  const doc = win.document;
  const script = fs.readFileSync(process.argv[2] + '/static/dashboard.js', 'utf8').split('(async()=>{try{cfg=await api')[0];
  const xss = '<img src=x onerror="window.pwned=1"><b id="bold">x</b>';
  let reply = {};
  win.fetch = async () => ({ok: true, json: async () => reply});
  win.eval(script + "\ncur='/synthetic';");
  const noHtml = (el, what) => assert.equal(el.querySelector('img,b#bold,script'), null, what + ' became HTML');

  reply = {reply: 'Try this: ' + xss};
  doc.getElementById('msg').value = 'hello';
  doc.getElementById('send').click();
  await new Promise(r => setTimeout(r, 20));
  noHtml(doc.getElementById('chat'), 'chat reply');
  assert.ok(doc.getElementById('chat').textContent.includes('<img src=x'));

  win.renderSecurityReport({
    findings: [{rule: xss, severity: 'high', file: xss, line: xss, message: xss}],
    categories: {[xss]: {count: 1, worst_severity: 'high'}},
    risk_level: xss, files_scanned: 1, total_findings: 1,
  });
  noHtml(doc.getElementById('securityBody'), 'security report');

  reply = {python_missing: [xss], node_missing: [xss], problems: [{title: xss, detail: xss, severity: 'error'}]};
  await win.diagnoseDependencies();
  noHtml(doc.getElementById('depsResults'), 'dependency report');

  win.fetch = async () => ({ok: false, status: 500, json: async () => ({detail: xss})});
  await win.diagnoseDependencies();
  noHtml(doc.getElementById('depsResults'), 'error message');
  assert.equal(win.pwned, undefined);
  console.log('PASS: model replies, scan findings and dependency names render as text');
  win.close();
})().catch(e => { console.error(e.stack || e.message); process.exitCode = 1; });
