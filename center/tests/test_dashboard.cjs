// Browser event regressions using only Node's standard library.
// These tests exercise dashboard handlers, not a browser or a real gamepad.
// Run: node --test center/tests/test_dashboard.cjs
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const controlNames = [...html.matchAll(/data-control="([^"]+)"/g)].map((match) => match[1]);
const names = ['throttle', 'left', 'brake', 'right', 'A', 'B', 'X', 'Y', 'LB', 'RB', 'BACK', 'START'];
const ok = (body = {}) => ({ ok: true, status: 200, json: async () => body });
const frameResponse = (sequence) => ({ ok: true, status: 200, blob: async () => new Blob(['jpeg'], { type: 'image/jpeg' }), headers: new Map([['X-Frame-Sequence', String(sequence)], ['X-Frame-Timestamp', '1700000000']]) });
const framePath = (url) => new URL(url, 'http://dashboard.test').pathname.endsWith('/frame');
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; };
const flush = async () => { for (let i = 0; i < 5; i += 1) await new Promise((resolve) => setImmediate(resolve)); };

class Element {
  constructor(id = '', tag = 'div', type = '') {
    Object.assign(this, { id, tagName: tag.toUpperCase(), type, value: '', checked: false, disabled: false, hidden: false, textContent: '', dataset: {}, listeners: new Map(), attributes: new Map(), options: [] });
    const classes = new Set();
    this.classList = { toggle(name, active) { if (active) classes.add(name); else classes.delete(name); }, contains(name) { return classes.has(name); } };
  }
  addEventListener(name, callback) { const callbacks = this.listeners.get(name) || []; callbacks.push(callback); this.listeners.set(name, callbacks); }
  setAttribute(name, value) { this.attributes.set(name, value); }
  removeAttribute(name) { this.attributes.delete(name); }
  closest() { return ['INPUT', 'SELECT', 'TEXTAREA'].includes(this.tagName) || this.contentEditable === 'true' ? this : null; }
  setPointerCapture(id) { this.pointerCapture = id; }
  replaceChildren() { this.options = []; }
  add(option) { this.options.push(option); }
}

async function dashboard() {
  const elements = new Map();
  const requestLog = [];
  const timers = new Map();
  const intervals = new Map();
  let timerId = 0;
  const window = new Element('window');
  const document = new Element('document');
  document.hidden = false;
  const controls = controlNames.map((name) => { const element = new Element(name, 'button'); element.dataset.control = name; return element; });
  for (const [id, tag, type, value] of [['terminal', 'select', '', ''], ['keyboard', 'input', 'checkbox', ''], ['throttle-level', 'input', 'range', '60'], ['steering-level', 'input', 'range', '70']]) {
    const element = new Element(id, tag, type); element.value = value; elements.set(id, element);
  }
  document.getElementById = (id) => { if (!elements.has(id)) elements.set(id, new Element(id)); return elements.get(id); };
  document.querySelectorAll = () => controls;
  const state = { handler: null, terminals: [] };
  const fetch = async (url, options = {}) => {
    const request = { url, options, body: options.body ? JSON.parse(options.body) : undefined };
    requestLog.push(request);
    if (state.handler) {
      const result = state.handler(request);
      if (result !== undefined) return result;
    }
    if (url === '/api/terminals') return ok({ terminals: state.terminals });
    if (framePath(url)) return new Promise((resolve, reject) => {
      const abort = () => reject(Object.assign(new Error('Aborted'), { name: 'AbortError' }));
      if (options.signal?.aborted) abort(); else options.signal?.addEventListener('abort', abort, { once: true });
    });
    return ok();
  };
  const context = vm.createContext({ document, window, Element, Option: class { constructor(text, value) { Object.assign(this, { text, value }); } }, AbortController, URL, performance, fetch,
    setTimeout(callback, ms) { const id = ++timerId; timers.set(id, { callback, ms }); return id; },
    clearTimeout(id) { timers.delete(id); },
    setInterval(callback, ms) { intervals.set(ms, callback); },
  });
  vm.runInContext(script, context, { filename: 'dashboard-inline.js' });
  await flush();
  state.terminals = [{ terminal_id: 'one', name: 'One', connected: true }, { terminal_id: 'two', name: 'Two', connected: true }];
  context.seed = state.terminals;
  vm.runInContext("terminals=seed;selected='one';centerOnline=true;syncControls();", context);
  const emit = (target, type, fields = {}, bubble = true) => {
    const event = { target, button: 0, pointerId: 1, repeat: false, altKey: false, ctrlKey: false, metaKey: false, defaultPrevented: false, propagationStopped: false,
      preventDefault() { this.defaultPrevented = true; }, stopPropagation() { this.propagationStopped = true; }, ...fields };
    for (const handler of target.listeners.get(type) || []) handler(event);
    if (bubble && target !== window && !event.propagationStopped && ['keydown', 'keyup'].includes(type)) for (const handler of window.listeners.get(type) || []) handler(event);
    return event;
  };
  return { state, context, window, document, controls, requestLog, emit, get: document.getElementById, evaluate: (code) => vm.runInContext(code, context),
    actions: () => requestLog.filter((request) => request.url.endsWith('/actions')),
    frames: () => requestLog.filter((request) => framePath(request.url)),
    releases: () => requestLog.filter((request) => request.url.endsWith('/release')),
    press(name, id = 1) { return emit(controls.find((button) => button.dataset.control === name), 'pointerdown', { pointerId: id }); },
    lift(name, id = 1, type = 'pointerup') { return emit(controls.find((button) => button.dataset.control === name), type, { pointerId: id }); },
    interval: (ms) => intervals.get(ms)(),
    dispose() { vm.runInContext('pageLeaving=true;selectionEpoch+=1;connectionEpoch+=1;if(frameAbort)frameAbort.abort();', context); },
  };
}

test('all twelve rendered hold controls produce their full-state action and pointer release', async (t) => {
  assert.deepEqual(controlNames, names);
  for (const name of names) await t.test(name, async () => {
    const h = await dashboard();
    h.press(name); await flush();
    assert.equal(h.actions().length, 1);
    const expected = { steering: name === 'left' ? -.7 : name === 'right' ? .7 : 0, throttle: name === 'throttle' ? .6 : 0, brake: name === 'brake' ? 1 : 0, buttons: ['throttle', 'left', 'brake', 'right'].includes(name) ? [] : [name], duration_ms: 250 };
    assert.deepEqual(h.actions()[0].body, expected);
    assert.equal(h.controls.find((button) => button.dataset.control === name).attributes.get('aria-pressed'), 'true');
    h.lift(name); await flush(); assert.equal(h.releases().length, 1); assert.equal(h.evaluate('held.size'), 0); h.dispose();
  });
});

test('pointer cancel and lost capture release once; right button does not drive', async () => {
  const h = await dashboard();
  h.emit(h.controls[0], 'pointerdown', { button: 2 }); await flush(); assert.equal(h.actions().length, 0);
  h.press('A'); await flush(); h.lift('A', 1, 'pointercancel'); h.lift('A', 1, 'lostpointercapture'); await flush();
  assert.equal(h.releases().length, 1); assert.equal(h.evaluate('held.size'), 0); h.dispose();
});

test('multiple pointers compose axes/buttons and releasing one keeps other sources held', async () => {
  const h = await dashboard();
  h.press('throttle', 1); await flush(); h.press('left', 2); await flush(); h.press('A', 3); await flush();
  assert.deepEqual(h.actions().at(-1).body, { steering: -.7, throttle: .6, brake: 0, buttons: ['A'], duration_ms: 250 });
  h.press('right', 4); await flush(); assert.equal(h.actions().at(-1).body.steering, 0);
  h.lift('left', 2); await flush(); assert.equal(h.actions().at(-1).body.steering, .7); assert.equal(h.releases().length, 0);
  h.press('throttle', 5); await flush(); h.lift('throttle', 1); await flush(); assert.equal(h.actions().at(-1).body.throttle, .6);
  h.emit(h.window, 'blur'); await flush(); assert.equal(h.releases().length, 1); h.dispose();
});

test('both sliders update their output and the held full-state action', async () => {
  const h = await dashboard();
  h.get('throttle-level').value = '23'; h.emit(h.get('throttle-level'), 'input'); await flush(); assert.equal(h.actions().length, 0); assert.equal(h.get('throttle-value').textContent, '23%');
  h.press('throttle'); await flush(); h.press('left', 2); await flush();
  h.get('steering-level').value = '41'; h.emit(h.get('steering-level'), 'input'); await flush();
  assert.equal(h.get('steering-value').textContent, '41%'); assert.equal(h.actions().at(-1).body.throttle, .23); assert.equal(h.actions().at(-1).body.steering, -.41);
  h.dispose();
});

test('keyboard checkbox immediately permits WASD while focused', async () => {
  const h = await dashboard();
  h.emit(h.window, 'keydown', { code: 'KeyW' }); await flush(); assert.equal(h.actions().length, 0);
  h.get('keyboard').checked = true; h.emit(h.get('keyboard'), 'change'); await flush();
  const down = h.emit(h.get('keyboard'), 'keydown', { code: 'KeyW' }); await flush();
  assert.equal(h.actions().length, 1); assert.equal(h.actions()[0].body.throttle, .6); assert.equal(down.defaultPrevented, true);
  h.emit(h.get('keyboard'), 'keyup', { code: 'KeyW' }); await flush(); assert.equal(h.evaluate('held.size'), 0); h.dispose();
});

test('range focus preserves arrow adjustment but allows WASD; selector/text focus suppresses driving', async () => {
  const h = await dashboard(); h.get('keyboard').checked = true;
  h.emit(h.get('steering-level'), 'keydown', { code: 'ArrowLeft' }); await flush(); assert.equal(h.actions().length, 0);
  h.emit(h.get('steering-level'), 'keydown', { code: 'KeyA' }); await flush(); assert.equal(h.actions().at(-1)?.body.steering, -.7);
  h.emit(h.get('terminal'), 'keyup', { code: 'KeyA' }); await flush(); assert.equal(h.evaluate('held.size'), 0);
  for (const target of [h.get('terminal'), new Element('text', 'input', 'text'), new Element('area', 'textarea')]) {
    const count = h.actions().length; h.emit(target, 'keydown', { code: 'KeyW' }); await flush(); assert.equal(h.actions().length, count);
  }
  h.dispose();
});

test('Space releases even when a checkbox, range or selector has focus', async () => {
  const h = await dashboard();
  for (const target of [h.get('keyboard'), h.get('throttle-level'), h.get('terminal')]) {
    h.press('throttle'); await flush(); const count = h.releases().length;
    const event = h.emit(target, 'keydown', { code: 'Space' }); await flush();
    assert.equal(h.releases().length, count + 1); assert.equal(h.evaluate('held.size'), 0); assert.equal(event.defaultPrevented, true);
  }
  h.dispose();
});

test('each control supports holding Enter and releasing Enter or button focus', async () => {
  const h = await dashboard();
  for (const button of h.controls) {
    const count = h.actions().length; const down = h.emit(button, 'keydown', { code: 'Enter' }); await flush();
    assert.equal(h.actions().length, count + 1, button.dataset.control); assert.equal(down.defaultPrevented, true);
    h.emit(button, 'keydown', { code: 'Enter', repeat: true }); await flush(); assert.equal(h.actions().length, count + 1);
    h.emit(button, 'keyup', { code: 'Enter' }); await flush(); assert.equal(h.evaluate('held.size'), 0);
  }
  h.emit(h.controls[0], 'keydown', { code: 'Enter' }); await flush(); h.emit(h.controls[0], 'blur'); await flush(); assert.equal(h.evaluate('held.size'), 0); h.dispose();
});

test('all WASD and arrow mappings release after focus changes; disabling keyboard clears input', async () => {
  const h = await dashboard(); h.get('keyboard').checked = true;
  for (const [code, field, value] of [['KeyW', 'throttle', .6], ['ArrowUp', 'throttle', .6], ['KeyS', 'brake', 1], ['ArrowDown', 'brake', 1], ['KeyA', 'steering', -.7], ['ArrowLeft', 'steering', -.7], ['KeyD', 'steering', .7], ['ArrowRight', 'steering', .7]]) {
    h.emit(h.window, 'keydown', { code }); await flush(); assert.equal(h.actions().at(-1).body[field], value);
    h.emit(h.get('terminal'), 'keyup', { code }); await flush(); assert.equal(h.evaluate('held.size'), 0);
  }
  h.emit(h.window, 'keydown', { code: 'KeyW' }); await flush(); h.get('keyboard').checked = false; h.emit(h.get('keyboard'), 'change'); await flush(); assert.equal(h.evaluate('held.size'), 0); h.dispose();
});

test('refresh and selection work; changing terminal releases old terminal without transferring held action', async () => {
  const h = await dashboard(); const count = h.requestLog.filter((request) => request.url === '/api/terminals').length;
  h.emit(h.get('refresh'), 'click'); await flush(); assert.equal(h.requestLog.filter((request) => request.url === '/api/terminals').length, count + 1);
  h.press('throttle'); await flush(); const actionCount = h.actions().length;
  h.get('terminal').value = 'two'; h.emit(h.get('terminal'), 'change'); await flush();
  assert.equal(h.releases().at(-1).url, '/api/terminals/one/release'); assert.equal(h.actions().length, actionCount); assert.equal(h.evaluate('selected'), 'two'); assert.equal(h.evaluate('held.size'), 0); h.dispose();
});

test('release button, window blur and hidden page all clear held input', async () => {
  const h = await dashboard();
  for (const trigger of [() => h.emit(h.get('release'), 'click'), () => h.emit(h.window, 'blur'), () => { h.document.hidden = true; h.emit(h.document, 'visibilitychange'); }]) {
    h.document.hidden = false; h.press('throttle'); await flush(); const count = h.releases().length;
    trigger(); await flush(); assert.equal(h.releases().length, count + 1); assert.equal(h.evaluate('held.size'), 0);
  }
  h.dispose();
});

test('held renewal is serialized, latest state coalesces, and release cancels unsent input', async () => {
  const h = await dashboard(); const first = deferred(); let delayed = false;
  h.state.handler = (request) => { if (request.url.endsWith('/actions') && !delayed) { delayed = true; return first.promise; } };
  h.press('throttle'); await flush(); h.press('left', 2); h.interval(100); h.press('A', 3); h.interval(100); await flush(); assert.equal(h.actions().length, 1);
  h.emit(h.get('release'), 'click'); first.resolve(ok()); await flush(); assert.equal(h.actions().length, 1); assert.equal(h.releases().length, 1); h.dispose();
});

test('failed action drops the newer backlog and sends one release', async () => {
  const h = await dashboard(); const first = deferred(); let delayed = false;
  h.state.handler = (request) => { if (request.url.endsWith('/actions') && !delayed) { delayed = true; return first.promise; } };
  h.press('throttle'); await flush(); h.press('left', 2); h.interval(100);
  first.resolve({ ok: false, status: 500, json: async () => ({ error: 'test failure' }) }); await flush();
  assert.equal(h.actions().length, 1); assert.equal(h.releases().length, 1); assert.equal(h.evaluate('held.size'), 0); assert.match(h.get('notice').textContent, /test failure/); h.dispose();
});

test('concurrent401 responses share one cookie recovery; browser sends no bearer credentials', async () => {
  const h = await dashboard(); const recovery = deferred(); const attempts = new Map();
  h.state.handler = (request) => {
    if (request.url === '/dashboard-session') return recovery.promise;
    if (request.url.startsWith('/api/probe')) { const n = (attempts.get(request.url) || 0) + 1; attempts.set(request.url, n); return n === 1 ? { ok: false, status: 401 } : ok(); }
  };
  const pending = h.evaluate("Promise.all([centerFetch('/api/probe-one'),centerFetch('/api/probe-two')])"); await flush();
  assert.equal(h.requestLog.filter((request) => request.url === '/dashboard-session').length, 1); recovery.resolve(ok()); await pending;
  assert.equal(attempts.get('/api/probe-one'), 2); assert.equal(attempts.get('/api/probe-two'), 2);
  for (const request of h.requestLog) { assert.equal(request.options.credentials, 'same-origin'); assert.equal(request.options.headers?.Authorization, undefined); }
  h.dispose();
});

test('an action released during cookie recovery is not retried afterward', async () => {
  const h = await dashboard(); const recovery = deferred(); let attempts = 0;
  h.state.handler = (request) => {
    if (request.url === '/dashboard-session') return recovery.promise;
    if (request.url.endsWith('/actions')) { attempts += 1; return attempts === 1 ? { ok: false, status: 401 } : ok(); }
  };
  h.press('throttle'); await flush(); h.lift('throttle'); await flush();
  recovery.resolve(ok()); await flush();
  assert.equal(h.actions().length, 1, 'the rejected action should not be replayed after the pointer was released');
  assert.equal(h.releases().length, 1); assert.equal(h.evaluate('held.size'), 0); h.dispose();
});

test('cookie recovery retries a still-held action and preserves later release', async () => {
  const h = await dashboard(); let attempts = 0;
  h.state.handler = (request) => {
    if (request.url.endsWith('/actions')) { attempts += 1; return attempts === 1 ? { ok: false, status: 401 } : ok(); }
  };
  h.press('throttle'); await flush(); assert.equal(h.actions().length, 2); assert.equal(h.evaluate('held.size'), 1);
  h.lift('throttle'); await flush(); assert.equal(h.releases().length, 1); h.dispose();
});

test('cookie recovery sends the latest changed state instead of replaying the old state', async () => {
  const h = await dashboard(); const recovery = deferred(); let attempts = 0;
  h.state.handler = (request) => {
    if (request.url === '/dashboard-session') return recovery.promise;
    if (request.url.endsWith('/actions')) { attempts += 1; return attempts === 1 ? { ok: false, status: 401 } : ok(); }
  };
  h.press('throttle'); await flush(); h.press('left', 2); await flush(); recovery.resolve(ok()); await flush();
  assert.equal(h.actions().length, 2); assert.equal(h.actions()[1].body.steering, -.7); assert.equal(h.evaluate('held.size'), 2); h.dispose();
});

test('restoring page from browser history restarts polling for same terminal', async () => {
  const h = await dashboard(); h.emit(h.window, 'pagehide'); h.emit(h.window, 'pageshow'); await flush();
  assert.equal(h.evaluate('centerOnline'), true); assert.ok(h.frames().some((request) => request.url.startsWith('/api/terminals/one/frame?'))); h.dispose();
});

test('frame loop waits for fresh sequence without duplicate requests or a fixed display timer', async () => {
  const h = await dashboard(); const first = deferred(); let count = 0;
  h.state.handler = (request) => { if (framePath(request.url) && ++count === 1) return first.promise; };
  h.evaluate('void pollFrames(selectionEpoch,connectionEpoch)'); await flush();
  assert.equal(h.frames().length, 1); assert.equal(new URL(h.frames()[0].url, 'http://dashboard.test').searchParams.get('timeout'), '3');
  h.interval(100); await flush(); assert.equal(h.frames().length, 1, 'a pending frame request should not be duplicated');
  first.resolve(frameResponse(41)); await flush();
  assert.equal(h.frames().length, 2, 'next frame should be requested immediately after receiving a frame');
  assert.equal(new URL(h.frames()[1].url, 'http://dashboard.test').searchParams.get('after'), '41');
  assert.equal(h.get('sequence').textContent, '41');
  h.dispose(); await flush(); assert.equal(h.frames()[1].options.signal.aborted, true);
});

test('terminal selection cancels its pending long poll and starts the new terminal without an old cursor', async () => {
  const h = await dashboard(); h.evaluate('void pollFrames(selectionEpoch,connectionEpoch)'); await flush();
  const previous = h.frames()[0]; h.get('terminal').value = 'two'; h.emit(h.get('terminal'), 'change'); await flush();
  assert.equal(previous.options.signal.aborted, true); assert.equal(h.frames().length, 2);
  const next = new URL(h.frames()[1].url, 'http://dashboard.test'); assert.equal(next.pathname, '/api/terminals/two/frame'); assert.equal(next.searchParams.has('after'), false); h.dispose();
});

test('a replacement session resets the frame cursor even if terminal online status never changes', async () => {
  const h = await dashboard(); h.state.terminals[0].session_id = 'previous';
  const first = deferred(); let count = 0;
  h.state.handler = (request) => { if (framePath(request.url) && ++count === 1) return first.promise; };
  h.evaluate('void pollFrames(selectionEpoch,connectionEpoch)'); await flush(); first.resolve(frameResponse(900)); await flush();
  const pending = h.frames().at(-1); assert.equal(new URL(pending.url, 'http://dashboard.test').searchParams.get('after'), '900');
  h.state.terminals = [{ terminal_id: 'one', name: 'One', connected: true, session_id: 'replacement' }, h.state.terminals[1]];
  h.emit(h.get('refresh'), 'click'); await flush();
  assert.equal(pending.options.signal.aborted, true); assert.equal(h.frames().length, 3);
  assert.equal(new URL(h.frames().at(-1).url, 'http://dashboard.test').searchParams.has('after'), false); h.dispose();
});
