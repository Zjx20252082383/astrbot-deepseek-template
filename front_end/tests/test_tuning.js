// node front_end/tests/test_tuning.js
// 验证页面原有脚本；使用内存 DOM/HTTP 替身，不启动服务。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '..', '识海心智观测台.html'), 'utf8');
const scripts = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)];
for (const [index, match] of scripts.entries()) {
  new vm.Script(match[1], { filename: `observatory-script-${index}.js` });
}
const helper = html.match(/  function appliedTuneValue\(payload\)\{[\s\S]*?\n  \}/)[0];
const sliders = html.slice(html.indexOf('  var SLIDER_SPECS='),
  html.indexOf('  /* ---- 初始化 + 切视图时刷新 ---- */'));
const closePosition = html.indexOf('    var close = ev.target.closest ? ev.target.closest("#cfg-close")');
const modal = html.slice(html.lastIndexOf('  document.addEventListener("click"', closePosition),
  html.indexOf('  if($("cfg-modal"))', closePosition));
const flush = () => new Promise(resolve => setImmediate(resolve));
const state = E => ({ status: 'ok', payload: { state: { psi: { E }, goals: {} } } });
const success = value => ({ status: 'ok', payload: { status: 'ok', value, state: state(value).payload.state } });

function deferred(queue, request) {
  return new Promise((resolve, reject) => queue.push({ ...request, resolve, reject }));
}

function harness() {
  const inputs = new Map(), elements = new Map(), handlers = {}, reads = [], writes = [], messages = [];
  const grid = { addEventListener: (name, handler) => { handlers[name] = handler; } };
  const element = id => {
    if (id === 'slider-grid') return grid;
    if (!elements.has(id)) elements.set(id, { textContent: '' });
    return elements.get(id);
  };
  const context = vm.createContext({
    document: {
      querySelector: selector => {
        const key = selector.match(/data-key="([^"]+)"/)[1];
        if (!inputs.has(key)) inputs.set(key, {
          dataset: {}, disabled: true, _value: '0.5',
          set value(v) { this._value = String(v); }, get value() { return this._value; },
          getAttribute: name => name === 'data-key' ? key : null,
        });
        return inputs.get(key);
      },
      querySelectorAll: () => [],
      addEventListener: (name, handler) => { handlers[name] = handler; },
    },
    $: element, CMD: '/api/control',
    jget: url => deferred(reads, { url }),
    jsend: (method, url, body) => deferred(writes, { method, url, body }),
    fetch: (url, options) => deferred(writes, { url, body: JSON.parse(options.body) }),
    toast: message => messages.push(message), renderState() {}, _renderState() {},
    setTimeout() {}, lastState: null,
  });
  vm.runInContext(helper + '\n' + sliders + '\n' + modal + '\nthis.init = initSliders;', context);
  return { inputs, handlers, reads, writes, messages, element, init: context.init };
}

async function initialized() {
  const h = harness();
  h.init();
  h.reads.shift().resolve(state(0.6));
  await flush();
  return h;
}

function change(h, value) {
  const input = h.inputs.get('E');
  input.value = value;
  h.handlers.change({ target: input });
  return input;
}

async function main() {
  let h = await initialized();
  let input = change(h, 0.97);
  assert.equal(input.disabled, true);
  assert.match(h.element('sv-E').textContent, /保存中/);
  assert.equal(h.messages.length, 0);
  h.writes.shift().resolve(success(0.8));
  await flush();
  assert.equal(input.value, '0.8');
  assert.equal(input.dataset.confirmed, '0.8');
  assert.equal(h.element('sv-E').textContent, '0.80');
  assert.equal(input.disabled, false);
  assert.match(h.messages.at(-1), /0.80.*已保存并生效/);
  console.log('PASS slider displays confirmed value after save');

  h = await initialized();
  input = change(h, 0.9);
  h.writes.shift().resolve({ status: 'ok', payload: { status: 'error', message: '请先启用心理插件' } });
  await flush();
  assert.equal(input.value, '0.6');
  assert.match(h.messages.at(-1), /写入失败.*启用心理插件/);
  assert.equal(h.messages.some(message => message.includes('已保存并生效')), false);
  h.reads.shift().resolve(state(0.6));
  await flush();
  assert.equal(input.disabled, false);
  console.log('PASS failed save restores confirmed value and reports error');

  h = await initialized();
  input = change(h, 0.9);
  h.writes.shift().reject(new Error('网络响应丢失'));
  await flush();
  h.reads.shift().resolve(state(0.9)); // 请求可能已保存，只有响应丢失。
  await flush();
  assert.equal(input.value, '0.9');
  assert.match(h.messages[0], /写入失败.*网络响应丢失/);
  console.log('PASS lost response reloads actual server state');

  h = await initialized();
  h.init();
  const stale = h.reads.shift();
  input = change(h, 0.9);
  h.writes.shift().resolve(success(0.9));
  await flush();
  stale.resolve(state(0.6));
  await flush();
  assert.equal(input.value, '0.9');
  console.log('PASS delayed old read cannot overwrite a successful edit');

  h = await initialized();
  input = change(h, 0.9);
  h.writes.shift().resolve({ status: 'ok', payload: { status: 'ok' } });
  await flush();
  assert.match(h.messages.at(-1), /未返回实际生效值/);
  assert.equal(input.value, '0.6');
  h.reads.shift().resolve(state(0.6));
  await flush();
  console.log('PASS old backend without confirmation is not reported as success');

  for (const failed of [false, true]) {
    h = await initialized();
    const current = { textContent: '0.6' }, field = { value: '9' };
    const row = { dataset: { key: 'E' }, querySelector: selector => selector === 'input' ? field : current };
    const button = { disabled: false, closest: () => row };
    h.handlers.click({ target: { closest: selector => selector === '.mrow .set' ? button : null } });
    assert.equal(button.disabled, true);
    const payload = failed ? { status: 'ok', payload: { status: 'error', message: '磁盘写入失败' } } : success(1);
    h.writes.shift().resolve({ json: () => Promise.resolve(payload) });
    await flush();
    assert.equal(button.disabled, false);
    assert.equal(String(current.textContent), failed ? '0.6' : '1');
    assert.match(h.messages.at(-1), failed ? /写入失败/ : /1.*已保存并生效/);
  }
  console.log('PASS numeric panel handles confirmed values and failed writes');
  console.log(`OK: ${scripts.length} inline scripts parsed; 7 interaction scenarios passed`);
}

main().catch(error => { console.error(error); process.exitCode = 1; });
