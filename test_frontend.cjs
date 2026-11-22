const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('index.html', 'utf8');
const match = html.match(/<script>([\s\S]*)<\/script>/);
assert.ok(match, 'index.html должен содержать script');
const script = match[1];

assert.match(script, /nextListSignature!==listSignature/);
assert.match(script, /nextDetailSignature===detailSignature\)return/);
assert.match(script, /details\.open=detailsOpen/);
assert.match(script, /detailOpenByMeeting/);
assert.match(script, /setInterval\(refresh,3000\)/);
assert.match(script, /\/resummarize/);
assert.match(script, /Ссылка на видеозапись/);
assert.match(script, /input\.id='cloudUrl'/);
assert.match(script, /\/api\/import-cloud/);
assert.match(script, /e\.status===409/);
assert.match(script, /Эта запись уже обработана/);
assert.match(script, /Открыть готовый конспект/);
assert.match(script, /openMeeting\(meetingId\)/);

(async()=>{
class Element {
  constructor(tag, id = '') {
    this.tag = tag;
    this.id = id;
    this.children = [];
    this.textContent = '';
    this.value = '';
    this.hidden = false;
    this.disabled = false;
    this.style = {};
  }

  closest() { return this.parent; }
  append(...items) {
    for (const item of items) {
      if (typeof item === 'string') this.textContent += item;
      else { item.parent = this; this.children.push(item); }
    }
  }
  querySelector() { return null; }
  addEventListener() {}
}

const fileCard = new Element('section', 'file-card');
const nodes = {
  file: new Element('input', 'file'),
  send: new Element('button', 'send'),
  message: new Element('p', 'message'),
  meetings: new Element('div', 'meetings'),
  detail: new Element('section', 'detail'),
};
nodes.file.parent = fileCard;
const calls = [];
const documentMock = {
  getElementById(id) {
    if (nodes[id]) return nodes[id];
    return fileCard.children.find(child => child.id === id) || null;
  },
  createElement(tag) { return new Element(tag); },
  querySelectorAll() { return []; },
};

async function fakeFetch(url) {
  calls.push(url);
  if (url === '/api/meetings') return {ok: true, status: 200, json: async () => []};
  if (url === '/api/import-cloud') return {
    ok: false,
    status: 409,
    json: async () => ({detail: 'Эта запись уже импортирована (ID: abc123, статус: Готово).' }),
  };
  if (url === '/api/meetings/abc123') return {
    ok: true,
    status: 200,
    json: async () => ({id: 'abc123', name: 'saved.mp4', created_at: '2026-11-22', source: 'Из облака', status: 'Готово', error: null, transcript: 'text', summary: {brief: 'ready', decisions: [], tasks: [], questions: []}}),
  };
  throw new Error(`Unexpected URL: ${url}`);
}

const context = {
  document: documentMock,
  fetch: fakeFetch,
  setInterval() {},
  console,
  Error,
  Map,
  JSON,
  String,
};
vm.runInNewContext(script, context);
await new Promise(resolve => setImmediate(resolve));
const cloudInput = fileCard.children.find(child => child.id === 'cloudUrl');
const cloudButton = fileCard.children.find(child => child.id === 'cloudSend');
cloudInput.value = 'https://drive.google.com/file/d/abc123/view';
await cloudButton.onclick();
assert.equal(nodes.message.textContent, 'Эта запись уже обработана ');
const openButton = nodes.message.children.find(child => child.textContent === 'Открыть готовый конспект');
assert.ok(openButton, 'должна отображаться кнопка открытия готовой записи');
await openButton.onclick();
assert.ok(calls.includes('/api/meetings/abc123'), 'кнопка должна открыть существующую встречу');

console.log('frontend regression checks: OK');
})();
