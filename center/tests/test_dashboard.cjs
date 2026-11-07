// 使用 Node 标准库验证页面事件与 WebRTC 生命周期；不连接真实浏览器、视频中继或手柄。
// 执行方式：node --test center/tests/test_dashboard.cjs
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');
const scripts = [...html.matchAll(/<script defer src="\/static\/([^"]+)"><\/script>/g)].map((match) => match[1]);
const ok = (body = {}) => ({ ok: true, status: 200, json: async () => body });
const sdpResponse = (location = '/api/terminals/one/video/sessions/session-one') => ({ ok: true, status: 201, headers: new Map([['Location', location]]), text: async () => 'v=0\r\ns=answer\r\n' });
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; };
const flush = async () => { for (let i = 0; i < 5; i += 1) await new Promise((resolve) => setImmediate(resolve)); };

class Element {
  constructor(id = '', tag = 'div', type = '') {
    Object.assign(this, { id, tagName: tag.toUpperCase(), type, value: '', checked: false, disabled: false, hidden: false, textContent: '', dataset: {}, listeners: new Map(), attributes: new Map(), options: [], children: [], parentElement: null });
    const classes = new Set();
    this.classList = { toggle(name, active = !classes.has(name)) { if (active) classes.add(name); else classes.delete(name); }, contains(name) { return classes.has(name); }, add(...names) { names.forEach((name) => classes.add(name)); }, remove(...names) { names.forEach((name) => classes.delete(name)); } };
    const properties = new Map();
    this.style = { setProperty(name, value) { properties.set(name, value); }, getPropertyValue(name) { return properties.get(name) || ''; } };
  }
  addEventListener(name, callback) { const callbacks = this.listeners.get(name) || []; callbacks.push(callback); this.listeners.set(name, callbacks); }
  removeEventListener(name, callback) { this.listeners.set(name, (this.listeners.get(name) || []).filter((item) => item !== callback)); }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  removeAttribute(name) { this.attributes.delete(name); }
  matches(selector) {
    return selector.split(',').some((part) => {
      const query = part.trim();
      const tag = query.match(/^[a-z][\w-]*/i)?.[0];
      if (tag && this.tagName !== tag.toUpperCase()) return false;
      const id = query.match(/#([\w-]+)/)?.[1];
      if (id && this.id !== id) return false;
      for (const [, name] of query.matchAll(/\.([\w-]+)/g)) if (!this.classList.contains(name)) return false;
      for (const [, name, value] of query.matchAll(/\[([^\s=\]]+)(?:=["']?([^"'\]]+)["']?)?\]/g)) {
        if (!this.attributes.has(name) || (value !== undefined && this.getAttribute(name) !== value)) return false;
      }
      return true;
    });
  }
  closest(selector) { for (let node = this; node; node = node.parentElement) if (node.matches(selector)) return node; return null; }
  contains(other) { for (let node = other; node; node = node.parentElement) if (node === this) return true; return false; }
  querySelectorAll(selector) { return this.children.flatMap((child) => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  setPointerCapture(id) { this.pointerCapture = id; }
  hasPointerCapture(id) { return this.pointerCapture === id; }
  releasePointerCapture(id) { if (this.pointerCapture === id) this.pointerCapture = null; }
  getBoundingClientRect() { return { left: 0, top: 0, width: 200, height: 200, right: 200, bottom: 200 }; }
  focus() { this.ownerDocument.activeElement = this; }
  replaceChildren() { this.options = []; }
  add(option) { this.options.push(option); }
  pause() { this.paused = true; }
  async play() { this.paused = false; }
  requestVideoFrameCallback(callback) { this.videoCallback = callback; return 1; }
  cancelVideoFrameCallback() { this.videoCallback = null; }
}

function parsePage(document, elements) {
  const parents = [document];
  const voidTags = new Set(['area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr']);
  for (const match of html.matchAll(/<(\/?)([a-z][\w:-]*)([^<>]*?)>/gi)) {
    const [, closing, tag, source] = match;
    if (closing) {
      const index = parents.findLastIndex((node) => node.tagName === tag.toUpperCase());
      if (index > 0) parents.length = index;
      continue;
    }
    const attributes = [...source.matchAll(/([^\s=\/]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g)];
    const element = new Element(); element.tagName = tag.toUpperCase(); element.ownerDocument = document;
    for (const [, name, double, single, unquoted] of attributes) {
      const value = double ?? single ?? unquoted ?? '';
      element.setAttribute(name, value);
      if (['id', 'type', 'value', 'min', 'max', 'step'].includes(name)) element[name] = value;
      if (['checked', 'disabled', 'hidden'].includes(name)) element[name] = true;
      if (name === 'contenteditable') element.contentEditable = value;
      if (name.startsWith('data-')) element.dataset[name.slice(5).replace(/-([a-z])/g, (_, char) => char.toUpperCase())] = value;
      if (name === 'class') element.classList.add(...value.split(/\s+/));
    }
    element.parentElement = parents.at(-1); element.parentElement.children.push(element);
    if (element.id) elements.set(element.id, element);
    if (!voidTags.has(tag.toLowerCase()) && !source.endsWith('/')) parents.push(element);
  }
}

async function dashboard() {
  const elements = new Map();
  const requestLog = [];
  const timers = new Map();
  const intervals = new Map();
  const clock = { now: 1000 };
  let timerId = 0;
  const window = new Element('window');
  window.location = { origin: 'http://dashboard.test' };
  const document = new Element('document');
  document.hidden = false;
  parsePage(document, elements);
  const controls = document.querySelectorAll('[data-control]');
  document.getElementById = (id) => elements.get(id) || null;
  const state = { handler: null, terminals: [], video: { enabled: false, available: false, message: '测试中未启用视频' }, ice: 'complete', autoConnect: true, decoded: 0, focused: true, gamepads: [] };
  document.hasFocus = () => state.focused;
  const navigator = { getGamepads: () => state.gamepads };
  window.navigator = navigator;
  const peers = [];
  class Peer extends Element {
    constructor() { super('peer'); this.connectionState = 'new'; this.iceGatheringState = state.ice; peers.push(this); }
    addTransceiver(kind, options) { this.transceiver = { kind, options, setCodecPreferences(codecs) { this.codecs = codecs; } }; return this.transceiver; }
    async createOffer() { return { type: 'offer', sdp: 'v=0\r\ns=offer\r\n' }; }
    async setLocalDescription(value) { this.localDescription = value; }
    async setRemoteDescription(value) {
      this.remoteDescription = value;
      if (state.autoConnect) {
        this.connectionState = 'connected'; this.onconnectionstatechange?.();
        this.track = new Element('track'); this.ontrack?.({ streams: [{ id: 'media-stream' }], track: this.track });
      }
    }
    async getStats() { return new Map([['video', { type: 'inbound-rtp', kind: 'video', framesDecoded: state.decoded, frameWidth: 1280, frameHeight: 720 }]]); }
    close() { this.connectionState = 'closed'; this.closed = true; this.onconnectionstatechange?.(); }
    event(name) { for (const callback of this.listeners.get(name) || []) callback(); }
  }
  const fetch = async (url, options = {}) => {
    const request = { url, options, body: options.body && options.headers?.['Content-Type'] === 'application/json' ? JSON.parse(options.body) : options.body };
    requestLog.push(request);
    if (state.handler) {
      const result = state.handler(request);
      if (result !== undefined) return result;
    }
    if (url === '/api/terminals') return ok({ terminals: state.terminals });
    if (url.endsWith('/video')) return ok(state.video);
    if (url.endsWith('/video/whep')) return sdpResponse();
    return ok();
  };
  const context = vm.createContext({ document, window, navigator, Element, Option: class { constructor(text, value) { Object.assign(this, { text, value }); } }, AbortController, AbortSignal, URL, performance: { now: () => clock.now }, fetch,
    RTCPeerConnection: Peer, RTCRtpReceiver: { getCapabilities() { return { codecs: [{ mimeType: 'video/VP8' }, { mimeType: 'video/H264', sdpFmtpLine: 'packetization-mode=1' }] }; } }, MediaStream: class {},
    setTimeout(callback, ms) { const id = ++timerId; timers.set(id, { callback, ms, at: clock.now + ms }); return id; },
    clearTimeout(id) { timers.delete(id); },
    setInterval(callback, ms) { const id = ++timerId; intervals.set(id, { callback, ms }); return id; },
    clearInterval(id) { intervals.delete(id); },
  });
  for (const name of scripts) vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'static', name), 'utf8'), context, { filename: name });
  await flush();
  state.terminals = [{ terminal_id: 'one', name: 'One', connected: true }, { terminal_id: 'two', name: 'Two', connected: true }];
  const app = window.autoDriverDashboard;
  app.terminals = state.terminals; app.selected = 'one'; app.online = true; app.controls.sync();
  const emit = (target, type, fields = {}, bubble = true) => {
    const event = { target, button: 0, pointerId: 1, repeat: false, altKey: false, ctrlKey: false, metaKey: false, defaultPrevented: false, propagationStopped: false,
      preventDefault() { this.defaultPrevented = true; }, stopPropagation() { this.propagationStopped = true; }, ...fields };
    for (const handler of target.listeners.get(type) || []) handler(event);
    if (bubble && target !== window && !event.propagationStopped && ['keydown', 'keyup'].includes(type)) for (const handler of window.listeners.get(type) || []) handler(event);
    return event;
  };
  const control = (name, mode = app.controls.mode || 'keyboard') => controls.find((button) => button.dataset.control === name && (!button.dataset.inputMode || button.dataset.inputMode === mode));
  return { state, context, window, document, controls, requestLog, emit, app, peers, get: document.getElementById, control,
    evaluate: (code) => vm.runInContext(code.replace(/\bheld\b/g, 'window.autoDriverDashboard.controls.held').replace(/\bselected\b/g, 'window.autoDriverDashboard.selected').replace(/\bcenterOnline\b/g, 'window.autoDriverDashboard.online').replace(/\bcenterFetch\(/g, 'window.autoDriverDashboard.api.fetch('), context),
    actions: () => requestLog.filter((request) => request.url.endsWith('/actions')),
    sessions: () => requestLog.filter((request) => request.url.endsWith('/video/whep')),
    releases: () => requestLog.filter((request) => request.url.endsWith('/release')),
    press(name, id = 1) { return emit(control(name), 'pointerdown', { pointerId: id }); },
    lift(name, id = 1, type = 'pointerup') { return emit(control(name), type, { pointerId: id }); },
    mode(name) { emit(document.querySelector(`[data-mode="${name}"]`), 'click'); },
    interval(ms) { for (const timer of intervals.values()) if (timer.ms === ms) timer.callback(); },
    advance(ms) { clock.now += ms; for (const [id, timer] of [...timers]) if (timer.at <= clock.now) { timers.delete(id); timer.callback(); } },
    dispose() { app.leaving = true; app.player.stop(); },
  };
}

test('every rendered hold control produces its mode-specific full state and pointer release', async (t) => {
  const page = await dashboard();
  const controls = page.controls.map((button) => [button.dataset.inputMode, button.dataset.control]); page.dispose();
  assert.deepEqual([...new Set(controls.map(([mode]) => mode))].sort(), ['gamepad', 'keyboard', 'racing']);
  for (const [mode, name] of controls) await t.test(`${mode}: ${name}`, async () => {
    const h = await dashboard(); h.mode(mode); await flush();
    const releases = h.releases().length;
    h.press(name); await flush();
    assert.equal(h.actions().length, 1);
    const steering = mode === 'keyboard' ? .7 : 1;
    const throttle = mode === 'keyboard' ? .6 : 1;
    const expected = { steering: name === 'left' ? -steering : name === 'right' ? steering : 0, throttle: name === 'throttle' ? throttle : 0, brake: name === 'brake' ? 1 : 0, buttons: name === 'clutch' ? ['LB'] : ['throttle', 'left', 'brake', 'right'].includes(name) ? [] : [name], duration_ms: 250 };
    assert.deepEqual(h.actions()[0].body, expected);
    assert.equal(h.control(name).attributes.get('aria-pressed'), 'true');
    h.lift(name); await flush(); assert.equal(h.releases().length, releases + 1); assert.equal(h.evaluate('held.size'), 0); h.dispose();
  });
});

test('pointer cancel and lost capture release once; right button does not drive', async () => {
  const h = await dashboard();
  h.emit(h.controls[0], 'pointerdown', { button: 2 }); await flush(); assert.equal(h.actions().length, 0);
  h.press('B'); await flush(); h.lift('B', 1, 'pointercancel'); h.lift('B', 1, 'lostpointercapture'); await flush();
  assert.equal(h.releases().length, 1); assert.equal(h.evaluate('held.size'), 0); h.dispose();
});

test('multiple pointers compose axes/buttons and releasing one keeps other sources held', async () => {
  const h = await dashboard();
  h.press('throttle', 1); await flush(); h.press('left', 2); await flush(); h.press('B', 3); await flush();
  assert.deepEqual(h.actions().at(-1).body, { steering: -.7, throttle: .6, brake: 0, buttons: ['B'], duration_ms: 250 });
  h.press('right', 4); await flush(); assert.equal(h.actions().at(-1).body.steering, 0);
  h.lift('left', 2); await flush(); assert.equal(h.actions().at(-1).body.steering, .7); assert.equal(h.releases().length, 0);
  h.press('throttle', 5); await flush(); h.lift('throttle', 1); await flush(); assert.equal(h.actions().at(-1).body.throttle, .6);
  h.emit(h.window, 'blur'); await flush(); assert.equal(h.releases().length, 1); h.dispose();
});

test('keyboard strength sliders update their output and held full-state action', async () => {
  const h = await dashboard();
  h.get('throttle-level').value = '23'; h.emit(h.get('throttle-level'), 'input'); await flush(); assert.equal(h.actions().length, 0); assert.equal(h.get('throttle-value').textContent, '23%');
  h.press('throttle'); await flush(); h.press('left', 2); await flush();
  h.get('steering-level').value = '41'; h.emit(h.get('steering-level'), 'input'); await flush();
  assert.equal(h.get('steering-value').textContent, '41%'); assert.equal(h.actions().at(-1).body.throttle, .23); assert.equal(h.actions().at(-1).body.steering, -.41);
  h.get('brake-level').value = '37'; h.emit(h.get('brake-level'), 'input'); h.press('brake', 3); await flush();
  assert.equal(h.get('brake-value').textContent, '37%'); assert.equal(h.actions().at(-1).body.brake, .37);
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
    h.mode(button.dataset.inputMode); await flush();
    const count = h.actions().length; const down = h.emit(button, 'keydown', { code: 'Enter' }); await flush();
    assert.equal(h.actions().length, count + 1, button.dataset.control); assert.equal(down.defaultPrevented, true);
    h.emit(button, 'keydown', { code: 'Enter', repeat: true }); await flush(); assert.equal(h.actions().length, count + 1);
    h.emit(button, 'keyup', { code: 'Enter' }); await flush(); assert.equal(h.evaluate('held.size'), 0);
  }
  h.mode('keyboard'); await flush();
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
    h.document.hidden = false; h.emit(h.window, 'focus'); h.press('throttle'); await flush(); const count = h.releases().length;
    trigger(); await flush(); assert.equal(h.releases().length, count + 1); assert.equal(h.evaluate('held.size'), 0);
  }
  h.dispose();
});

test('held renewal is serialized, latest state coalesces, and release cancels unsent input', async () => {
  const h = await dashboard(); const first = deferred(); let delayed = false;
  h.state.handler = (request) => { if (request.url.endsWith('/actions') && !delayed) { delayed = true; return first.promise; } };
  h.press('throttle'); await flush(); h.press('left', 2); h.interval(100); h.press('B', 3); h.interval(100); await flush(); assert.equal(h.actions().length, 1);
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

const pedalPointer = (axis) => 100 + ['throttle', 'brake', 'clutch'].indexOf(axis);
const inputRange = (h, id, value) => { h.get(id).value = String(value); h.emit(h.get(id), 'input'); };
const setRange = (h, id, value) => {
  if (id.startsWith('pedal-')) {
    const axis = id.slice(6); const pointerId = pedalPointer(axis);
    if (!h.app.controls.pedalPointers.has(pointerId)) h.emit(h.get(id), 'pointerdown', { pointerId });
  }
  inputRange(h, id, value);
};
function physicalPad({ steering = 0, throttle = 0, brake = 0, pressed = [] } = {}) {
  return { id: 'Test standard controller', index: 0, connected: true, mapping: 'standard', axes: [steering, 0, 0, 0], buttons: Array.from({ length: 17 }, (_, index) => {
    const value = index === 6 ? brake : index === 7 ? throttle : pressed.includes(index) ? 1 : 0;
    return { pressed: value > .5, touched: value > 0, value };
  }) };
}

test('pedal sliders belong only to racing and reject input in keyboard and gamepad modes', async (t) => {
  for (const mode of ['keyboard', 'gamepad']) await t.test(mode, async () => {
    const h = await dashboard(); h.mode(mode); await flush(); const count = h.actions().length;
    for (const axis of ['throttle', 'brake', 'clutch']) {
      const input = h.get(`pedal-${axis}`);
      assert.equal(input.closest('[data-mode-panel]'), h.get('panel-racing'));
      assert.equal(input.disabled, true); assert.equal(input.closest('[data-mode-panel]').hidden, true);
      assert.equal(h.document.querySelector(`[data-pedal-reset="${axis}"]`).disabled, true);
      setRange(h, `pedal-${axis}`, 59);
    }
    await flush(); assert.equal(h.actions().length, count); assert.equal(h.app.controls.activeInput(), false);
    assert.deepEqual(JSON.parse(JSON.stringify(h.app.controls.manualPedals)), { throttle: 0, brake: 0, clutch: 0 });
    // 即使状态意外残留，其他模式也不消费赛车的手动踏板输入。
    h.app.controls.manualPedals = { throttle: .59, brake: .42, clutch: .31 };
    assert.equal(h.app.controls.activeInput(), false);
    assert.deepEqual(JSON.parse(JSON.stringify(h.app.controls.actionState())), { steering: 0, throttle: 0, brake: 0, buttons: [], duration_ms: 250 });
    h.app.controls.sync();
    for (const axis of ['throttle', 'brake', 'clutch']) assert.equal(h.get(`pedal-${axis}`).value, '0');
    h.dispose();
  });
  await t.test('racing holds exact strengths and independently releases each pedal', async () => {
    const h = await dashboard(); h.mode('racing'); await flush();
    setRange(h, 'pedal-throttle', 37); setRange(h, 'pedal-brake', 28); setRange(h, 'pedal-clutch', 61); await flush();
    assert.deepEqual(h.actions().at(-1).body, { steering: 0, throttle: .37, brake: .28, buttons: ['LB'], duration_ms: 250 });
    for (const [axis, value] of [['throttle', 37], ['brake', 28], ['clutch', 61]]) {
      assert.equal(h.get(`pedal-${axis}`).value, String(value));
      assert.equal(h.get(`pedal-${axis}-value`).textContent, `${value}%`);
      assert.equal(h.get(`pedal-${axis}`).style.getPropertyValue('--pedal-level'), `${value}%`);
    }
    assert.deepEqual(Object.keys(h.actions().at(-1).body).sort(), ['brake', 'buttons', 'duration_ms', 'steering', 'throttle']);
    const count = h.actions().length; h.interval(100); await flush();
    assert.equal(h.actions().length, count + 1, 'pedal renews only while held');
    h.emit(h.get('pedal-throttle'), 'pointerup', { pointerId: pedalPointer('throttle') }); await flush();
    assert.equal(h.actions().at(-1).body.throttle, 0); assert.equal(h.actions().at(-1).body.brake, .28);
    assert.equal(h.get('pedal-throttle').value, '0');
    h.emit(h.document.querySelector('[data-pedal-reset="clutch"]'), 'click'); await flush();
    assert.deepEqual(h.actions().at(-1).body.buttons, []); assert.equal(h.actions().at(-1).body.throttle, 0);
    setRange(h, 'pedal-throttle', 19); await flush();
    h.emit(h.document.querySelector('[data-pedal-reset="throttle"]'), 'click'); await flush();
    assert.equal(h.actions().at(-1).body.throttle, 0); assert.equal(h.actions().at(-1).body.brake, .28);
    const releases = h.releases().length;
    h.emit(h.document.querySelector('[data-pedal-reset="brake"]'), 'click'); await flush();
    assert.equal(h.releases().length, releases + 1); assert.equal(h.app.controls.activeInput(), false);
    h.dispose();
  });
});

test('every racing pedal springs to zero on pointer release, cancellation and lost capture', async (t) => {
  for (const axis of ['throttle', 'brake', 'clutch']) for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) await t.test(`${axis}/${type}`, async () => {
    const h = await dashboard(); h.mode('racing'); await flush(); const input = h.get(`pedal-${axis}`);
    const pointerId = pedalPointer(axis); const releases = h.releases().length;
    const down = h.emit(input, 'pointerdown', { pointerId });
    assert.equal(down.defaultPrevented, false, 'native range dragging must remain enabled');
    inputRange(h, input.id, 63); await flush();
    assert.equal(h.get(`pedal-${axis}-value`).textContent, '63%');
    h.emit(input, type, { pointerId }); await flush();
    assert.equal(input.value, '0'); assert.equal(h.get(`pedal-${axis}-value`).textContent, '0%');
    assert.equal(h.releases().length, releases + 1); assert.equal(h.app.controls.activeInput(), false);
    const count = h.actions().length;
    inputRange(h, input.id, 89); h.emit(input, 'change');
    h.emit(input, 'lostpointercapture', { pointerId }); h.interval(100); await flush();
    assert.equal(input.value, '0'); assert.equal(h.actions().length, count);
    assert.equal(h.releases().length, releases + 1); h.dispose();
  });
});

test('multiple pedal pointers survive focus changes and release only their own axis outside the controls', async () => {
  const h = await dashboard(); h.mode('racing'); await flush();
  setRange(h, 'racing-steering', -44); h.press('B', 7);
  setRange(h, 'pedal-throttle', 61); setRange(h, 'pedal-brake', 32); setRange(h, 'pedal-clutch', 48); await flush();
  h.emit(h.get('pedal-throttle'), 'blur'); h.emit(h.get('pedal-brake'), 'blur'); await flush();
  assert.equal(h.actions().at(-1).body.throttle, .61); assert.equal(h.actions().at(-1).body.brake, .32);
  h.emit(h.window, 'pointerup', { pointerId: 999 }); await flush(); assert.equal(h.get('pedal-clutch').value, '48');
  const releases = h.releases().length;
  h.emit(h.window, 'pointerup', { pointerId: pedalPointer('throttle') }); await flush();
  assert.deepEqual(h.actions().at(-1).body, { steering: -.44, throttle: 0, brake: .32, buttons: ['B', 'LB'], duration_ms: 250 });
  h.emit(h.window, 'pointercancel', { pointerId: pedalPointer('clutch') }); await flush();
  assert.deepEqual(h.actions().at(-1).body, { steering: -.44, throttle: 0, brake: .32, buttons: ['B'], duration_ms: 250 });
  h.emit(h.get('pedal-brake'), 'pointerup', { pointerId: pedalPointer('brake') }); await flush();
  assert.deepEqual(h.actions().at(-1).body, { steering: -.44, throttle: 0, brake: 0, buttons: ['B'], duration_ms: 250 });
  assert.equal(h.releases().length, releases, 'steering and shift remain held');
  h.emit(h.get('release'), 'click'); await flush(); h.dispose();
});

test('a pedal keeps its remaining pointer or keyboard source until the final source releases', async () => {
  const h = await dashboard(); h.mode('racing'); await flush(); const input = h.get('pedal-throttle');
  h.emit(input, 'pointerdown', { pointerId: 11 }); inputRange(h, input.id, 35);
  h.emit(input, 'pointerdown', { pointerId: 12 }); inputRange(h, input.id, 72);
  h.emit(input, 'keydown', { code: 'End' }); inputRange(h, input.id, 100); await flush();
  h.emit(input, 'pointerup', { pointerId: 11 }); await flush(); assert.equal(input.value, '100');
  h.emit(h.window, 'keyup', { code: 'End' }); await flush(); assert.equal(input.value, '100');
  h.emit(input, 'keydown', { code: 'ArrowDown' }); inputRange(h, input.id, 67);
  h.emit(input, 'blur'); await flush(); assert.equal(input.value, '67', 'touch focus change clears keys but keeps active pointer');
  assert.equal(h.app.controls.pedalKeys.size, 0);
  h.emit(h.window, 'pointerup', { pointerId: 12 }); await flush();
  assert.equal(input.value, '0'); assert.equal(h.app.controls.activeInput(), false); h.dispose();
});

test('native pedal range keys spring back on keyup or keyboard focus loss without accepting stale repeats', async (t) => {
  for (const code of ['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home', 'End', 'PageUp', 'PageDown']) await t.test(code, async () => {
    const h = await dashboard(); h.mode('racing'); await flush(); const input = h.get('pedal-brake');
    const down = h.emit(input, 'keydown', { code }); assert.equal(down.defaultPrevented, false);
    inputRange(h, input.id, code === 'Home' ? 0 : code === 'End' ? 100 : 39); await flush();
    assert.equal(input.value, code === 'Home' ? '0' : code === 'End' ? '100' : '39');
    h.emit(input, 'keydown', { code, repeat: true }); inputRange(h, input.id, 57); await flush();
    assert.equal(h.actions().at(-1).body.brake, .57);
    h.emit(h.get('terminal'), 'keyup', { code }); await flush();
    assert.equal(input.value, '0'); assert.equal(h.app.controls.pedalKeys.size, 0);
    const count = h.actions().length;
    h.emit(input, 'keydown', { code, repeat: true }); inputRange(h, input.id, 91); h.emit(input, 'change'); h.interval(100); await flush();
    assert.equal(input.value, '0'); assert.equal(h.actions().length, count);
    h.emit(input, 'keydown', { code }); inputRange(h, input.id, 26); await flush();
    h.emit(input, 'blur'); await flush(); assert.equal(input.value, '0'); assert.equal(h.app.controls.pedalKeys.size, 0);
    h.dispose();
  });
});

test('non-left pedal presses and input without a held source never apply pressure', async () => {
  const h = await dashboard(); h.mode('racing'); await flush(); const input = h.get('pedal-clutch');
  const count = h.actions().length;
  for (const button of [1, 2]) {
    h.emit(input, 'pointerdown', { button, pointerId: 6 }); inputRange(h, input.id, 85); await flush();
    assert.equal(input.value, '0'); assert.equal(h.app.controls.pedalPointers.size, 0);
  }
  h.emit(input, 'keydown', { code: 'End', ctrlKey: true }); inputRange(h, input.id, 100); await flush();
  assert.equal(input.value, '0'); assert.equal(h.actions().length, count); h.dispose();
});

test('keyboard clutch and held-key presets control strength without showing racing pedals', async () => {
  const h = await dashboard(); h.get('keyboard').checked = true;
  setRange(h, 'clutch-level', 35); h.emit(h.window, 'keydown', { code: 'KeyC' }); await flush();
  assert.deepEqual(h.actions().at(-1).body.buttons, ['LB']); assert.equal(h.get('clutch-value').textContent, '35%');
  setRange(h, 'clutch-level', 0); await flush(); assert.deepEqual(h.actions().at(-1).body.buttons, []);
  setRange(h, 'clutch-level', 72); await flush();
  assert.equal(h.get('clutch-value').textContent, '72%'); assert.equal(h.get('pedal-clutch').value, '0');
  assert.deepEqual(h.actions().at(-1).body.buttons, ['LB']);
  h.emit(h.get('terminal'), 'keyup', { code: 'KeyC' }); await flush();
  assert.equal(h.get('pedal-clutch').value, '0'); assert.equal(h.app.controls.activeInput(), false);
  h.press('throttle'); await flush(); setRange(h, 'throttle-level', 43); await flush();
  assert.equal(h.actions().at(-1).body.throttle, .43); assert.equal(h.get('throttle-value').textContent, '43%');
  assert.equal(h.get('pedal-throttle-value').textContent, '0%'); assert.equal(h.get('panel-racing').hidden, true);
  h.lift('throttle'); await flush(); h.dispose();
});

test('releasing screen keys preserves separately held keyboard throttle and clutch', async () => {
  const h = await dashboard(); h.get('keyboard').checked = true;
  h.emit(h.window, 'keydown', { code: 'KeyW' }); h.press('throttle'); await flush();
  assert.equal(h.actions().at(-1).body.throttle, .6);
  h.lift('throttle'); await flush();
  assert.equal(h.actions().at(-1).body.throttle, .6);
  h.emit(h.window, 'keyup', { code: 'KeyW' }); await flush(); assert.equal(h.app.controls.activeInput(), false);
  h.emit(h.window, 'keydown', { code: 'KeyC' }); h.press('clutch'); await flush();
  assert.deepEqual(h.actions().at(-1).body.buttons, ['LB']);
  h.lift('clutch'); await flush(); assert.deepEqual(h.actions().at(-1).body.buttons, ['LB']);
  h.emit(h.window, 'keyup', { code: 'KeyC' }); await flush(); assert.equal(h.app.controls.activeInput(), false);
  h.dispose();
});

test('physical triggers update gamepad readouts and preserve separately held screen controls', async () => {
  const h = await dashboard(); h.mode('gamepad'); h.state.gamepads = [physicalPad()]; h.interval(100); await flush();
  h.state.gamepads = [physicalPad({ throttle: .74, brake: .32, pressed: [0, 4] })]; h.interval(100); await flush();
  assert.equal(h.get('gamepad-throttle').textContent, '74%'); assert.equal(h.get('gamepad-brake').textContent, '32%');
  assert.equal(h.get('panel-racing').hidden, true); assert.equal(h.get('pedal-clutch').value, '0');
  assert.deepEqual(h.actions().at(-1).body.buttons.sort(), ['A', 'LB']);
  h.press('throttle'); h.press('LB', 2); await flush(); assert.equal(h.actions().at(-1).body.throttle, 1);
  h.lift('throttle'); h.lift('LB', 2); await flush(); assert.equal(h.actions().at(-1).body.throttle, .74);
  assert.deepEqual(h.actions().at(-1).body.buttons.sort(), ['A', 'LB']);
  h.press('LB', 2); await flush();
  h.state.gamepads = [physicalPad()]; h.interval(100); await flush();
  assert.equal(h.get('gamepad-throttle').textContent, '0%'); assert.equal(h.get('gamepad-brake').textContent, '0%');
  assert.deepEqual(h.actions().at(-1).body.buttons, ['LB']); assert.equal(h.actions().at(-1).body.throttle, 0);
  h.lift('LB', 2); await flush(); assert.equal(h.app.controls.activeInput(), false);
  h.dispose();
});

test('racing pedals clear on every lifecycle stop and never renew afterward', async (t) => {
  const scenarios = [
    ['racing', 'Space', (h) => h.emit(h.get('pedal-clutch'), 'keydown', { code: 'Space' })],
    ['racing', 'blur', (h) => h.emit(h.window, 'blur')],
    ['racing', 'mode', (h) => h.mode('keyboard')],
    ['racing', 'terminal', (h) => { h.get('terminal').value = 'two'; h.emit(h.get('terminal'), 'change'); }],
    ['racing', 'hidden', (h) => { h.document.hidden = true; h.emit(h.document, 'visibilitychange'); }],
    ['racing', 'pagehide', (h) => h.emit(h.window, 'pagehide')],
  ];
  for (const [mode, name, trigger] of scenarios) await t.test(`${mode}/${name}`, async () => {
    const h = await dashboard(); h.mode(mode); await flush();
    setRange(h, 'pedal-throttle', 47); setRange(h, 'pedal-brake', 31); setRange(h, 'pedal-clutch', 62); await flush();
    h.emit(h.get('pedal-clutch'), 'keydown', { code: 'End' });
    const count = h.actions().length; trigger(h); await flush(); h.interval(100); await flush();
    assert.equal(h.actions().length, count); assert.equal(h.app.controls.activeInput(), false);
    assert.equal(h.app.controls.pedalPointers.size, 0); assert.equal(h.app.controls.pedalKeys.size, 0);
    for (const axis of ['throttle', 'brake', 'clutch']) assert.equal(h.get(`pedal-${axis}`).value, '0');
    h.document.hidden = false; h.emit(h.window, 'focus'); h.emit(h.window, 'pageshow'); h.mode('racing'); await flush();
    for (const axis of ['throttle', 'brake', 'clutch']) {
      const input = h.get(`pedal-${axis}`);
      inputRange(h, input.id, 84); h.emit(input, 'change');
      h.emit(h.window, 'pointerup', { pointerId: pedalPointer(axis) });
    }
    h.emit(h.get('pedal-clutch'), 'keydown', { code: 'End', repeat: true }); inputRange(h, 'pedal-clutch', 100);
    h.emit(h.window, 'keyup', { code: 'End' }); h.interval(100); await flush();
    assert.equal(h.actions().length, count, 'old pointer input and held-key repeats cannot resume after a lifecycle stop');
    for (const axis of ['throttle', 'brake', 'clutch']) assert.equal(h.get(`pedal-${axis}`).value, '0');
    assert.ok(h.releases().some((request) => request.url === '/api/terminals/one/release')); h.dispose();
  });
});

test('a manual clutch released during cookie recovery cannot replay its LB action', async () => {
  const h = await dashboard(); h.mode('racing'); await flush(); const releases = h.releases().length;
  const recovery = deferred(); let attempts = 0;
  h.state.handler = (request) => {
    if (request.url === '/dashboard-session') return recovery.promise;
    if (request.url.endsWith('/actions')) { attempts += 1; return attempts === 1 ? { ok: false, status: 401 } : ok(); }
  };
  setRange(h, 'pedal-clutch', 58); await flush();
  h.emit(h.get('pedal-clutch'), 'pointerup', { pointerId: pedalPointer('clutch') }); await flush();
  inputRange(h, 'pedal-clutch', 77); h.emit(h.get('pedal-clutch'), 'change'); await flush();
  recovery.resolve(ok()); await flush();
  assert.equal(h.actions().length, 1); assert.equal(h.releases().length, releases + 1); assert.equal(h.get('pedal-clutch-value').textContent, '0%'); h.dispose();
});

test('compact status combines online counts, center connection and terminal selection', async () => {
  const h = await dashboard(); h.state.terminals.push({ terminal_id: 'off', name: 'Offline', connected: false });
  h.emit(h.get('refresh'), 'click'); await flush();
  assert.equal(h.get('online-count').textContent, '2'); assert.equal(h.get('terminal-count').textContent, '3');
  assert.match(h.get('connection-text').textContent, /中心/);
  assert.equal(h.get('terminal').options.length, 3); assert.equal(h.get('terminal').value, 'one');
  h.state.handler = (request) => request.url === '/api/terminals' ? Promise.reject(new Error('offline')) : undefined;
  h.emit(h.get('refresh'), 'click'); await flush();
  assert.equal(h.app.online, false); assert.equal(h.get('release').disabled, true);
  assert.equal(h.get('connection-status').classList.contains('online'), false); h.dispose();
});

test('changing modes releases held input, selects one panel and blocks inactive controls', async () => {
  const h = await dashboard(); h.get('keyboard').checked = true;
  h.press('throttle'); await flush(); const releases = h.releases().length;
  h.mode('racing'); await flush();
  assert.equal(h.releases().length, releases + 1); assert.equal(h.evaluate('held.size'), 0);
  for (const tab of h.document.querySelectorAll('[data-mode]')) assert.equal(tab.getAttribute('aria-selected'), String(tab.dataset.mode === 'racing'));
  for (const panel of h.document.querySelectorAll('[data-mode-panel]')) assert.equal(panel.hidden, panel.dataset.modePanel !== 'racing');
  const actions = h.actions().length;
  h.emit(h.control('throttle', 'keyboard'), 'pointerdown'); h.emit(h.window, 'keydown', { code: 'KeyW' }); await flush();
  assert.equal(h.actions().length, actions);
  h.mode('gamepad'); await flush(); h.press('throttle'); await flush();
  assert.equal(h.actions().at(-1).body.throttle, 1); h.dispose();
});

test('mode tabs support keyboard navigation and release the previous mode', async () => {
  const h = await dashboard(); const tabs = h.document.querySelectorAll('[data-mode]');
  h.press('throttle'); await flush();
  const event = h.emit(tabs[0], 'keydown', { code: 'ArrowRight', key: 'ArrowRight' }); await flush();
  assert.equal(event.defaultPrevented, true); assert.equal(h.app.controls.mode, 'gamepad'); assert.equal(h.evaluate('held.size'), 0);
  h.emit(tabs[1], 'keydown', { code: 'End', key: 'End' }); await flush(); assert.equal(h.app.controls.mode, 'racing');
  h.emit(tabs[2], 'keydown', { code: 'Home', key: 'Home' }); await flush(); assert.equal(h.app.controls.mode, 'keyboard'); h.dispose();
});

test('keyboard Q and E send downshift and upshift buttons and release after focus changes', async () => {
  const h = await dashboard(); h.get('keyboard').checked = true;
  for (const [code, button] of [['KeyQ', 'X'], ['KeyE', 'B']]) {
    h.emit(h.window, 'keydown', { code }); await flush(); assert.deepEqual(h.actions().at(-1).body.buttons, [button]);
    h.emit(h.get('terminal'), 'keyup', { code }); await flush(); assert.equal(h.evaluate('held.size'), 0);
  }
  h.dispose();
});

test('racing analog axes compose exact strengths, renew, shift and reset together', async () => {
  const h = await dashboard(); h.mode('racing'); await flush();
  setRange(h, 'racing-steering', -43); setRange(h, 'pedal-throttle', 28); setRange(h, 'pedal-brake', 17); await flush();
  assert.deepEqual(h.actions().at(-1).body, { steering: -.43, throttle: .28, brake: .17, buttons: [], duration_ms: 250 });
  assert.match(h.get('pedal-throttle-value').textContent, /28/); assert.match(h.get('pedal-brake-value').textContent, /17/);
  const count = h.actions().length; h.interval(100); await flush(); assert.equal(h.actions().length, count + 1);
  h.press('B'); await flush(); assert.deepEqual(h.actions().at(-1).body.buttons, ['B']);
  h.lift('B'); await flush(); assert.equal(h.actions().at(-1).body.throttle, .28); assert.deepEqual(h.actions().at(-1).body.buttons, []);
  h.emit(h.get('release'), 'click'); await flush();
  for (const id of ['racing-steering', 'pedal-throttle', 'pedal-brake']) assert.equal(Number(h.get(id).value), 0);
  const releasedCount = h.actions().length; h.interval(100); await flush(); assert.equal(h.actions().length, releasedCount); h.dispose();
});

test('racing wheel pointer drag springs back while an independent pedal is still held', async () => {
  const h = await dashboard(); h.mode('racing'); await flush(); setRange(h, 'pedal-throttle', 35); await flush();
  const wheel = h.get('racing-wheel');
  h.emit(wheel, 'pointerdown', { clientX: 100, clientY: 0, pointerId: 7 });
  h.emit(wheel, 'pointermove', { clientX: 200, clientY: 100, pointerId: 7 }); await flush();
  assert.ok(h.actions().at(-1).body.steering > 0); assert.ok(h.actions().at(-1).body.steering <= 1);
  assert.equal(h.actions().at(-1).body.throttle, .35);
  h.emit(wheel, 'pointerup', { pointerId: 7 }); await flush();
  assert.equal(h.actions().at(-1).body.steering, 0); assert.equal(h.actions().at(-1).body.throttle, .35);
  setRange(h, 'racing-steering', 75); h.emit(h.get('wheel-reset'), 'click'); await flush();
  assert.equal(h.actions().at(-1).body.steering, 0); assert.equal(h.actions().at(-1).body.throttle, .35);
  setRange(h, 'racing-steering', -60); h.emit(wheel, 'keydown', { code: 'Home', key: 'Home' }); await flush();
  assert.equal(h.actions().at(-1).body.steering, 0, 'Home must match the wheel label and recenter steering');
  assert.equal(h.actions().at(-1).body.throttle, .35); h.dispose();
});

test('racing axes clear on mode switch, terminal switch, blur and hidden page', async () => {
  for (const trigger of [
    (h) => h.mode('keyboard'),
    (h) => { h.get('terminal').value = 'two'; h.emit(h.get('terminal'), 'change'); },
    (h) => h.emit(h.window, 'blur'),
    (h) => { h.document.hidden = true; h.emit(h.document, 'visibilitychange'); },
  ]) {
    const h = await dashboard(); h.mode('racing'); await flush(); setRange(h, 'pedal-throttle', 65); await flush();
    const count = h.actions().length; trigger(h); await flush(); h.interval(100); await flush();
    assert.equal(h.actions().length, count); assert.equal(Number(h.get('pedal-throttle').value), 0);
    assert.ok(h.releases().some((request) => request.url === '/api/terminals/one/release')); h.dispose();
  }
});

test('mode switch during cookie recovery cancels the stale action', async () => {
  const h = await dashboard(); const recovery = deferred();
  h.state.handler = (request) => request.url === '/dashboard-session' ? recovery.promise : request.url.endsWith('/actions') ? { ok: false, status: 401 } : undefined;
  h.press('throttle'); await flush(); h.mode('racing'); recovery.resolve(ok()); await flush();
  assert.equal(h.actions().length, 1); assert.equal(h.evaluate('held.size'), 0); assert.equal(h.releases().length, 1); h.dispose();
});

test('a failed racing action clears held pedal sources and rejects later native input', async () => {
  const h = await dashboard(); h.mode('racing'); await flush(); const first = deferred();
  h.state.handler = (request) => request.url.endsWith('/actions') ? first.promise : undefined;
  setRange(h, 'pedal-throttle', 30); await flush(); setRange(h, 'pedal-throttle', 70); h.interval(100);
  first.resolve({ ok: false, status: 502, json: async () => ({ error: 'racing failure' }) }); await flush();
  assert.equal(h.actions().length, 1); assert.equal(Number(h.get('pedal-throttle').value), 0);
  assert.equal(h.app.controls.pedalPointers.size, 0); assert.equal(h.app.controls.pedalKeys.size, 0);
  inputRange(h, 'pedal-throttle', 93); h.emit(h.get('pedal-throttle'), 'change');
  h.emit(h.get('pedal-throttle'), 'pointerup', { pointerId: pedalPointer('throttle') }); await flush();
  assert.equal(Number(h.get('pedal-throttle').value), 0);
  const count = h.actions().length; h.interval(100); await flush(); assert.equal(h.actions().length, count);
  assert.match(h.get('notice').textContent, /racing failure/); h.dispose();
});

test('a delayed release failure preserves deliberate input started in the new mode', async () => {
  const h = await dashboard(); h.press('throttle'); await flush(); const release = deferred();
  h.state.handler = (request) => request.url.endsWith('/release') ? release.promise : undefined;
  h.mode('racing'); await flush(); setRange(h, 'pedal-throttle', 45); await flush();
  assert.equal(h.actions().length, 1, 'new input waits behind the older release');
  release.resolve({ ok: false, status: 500, json: async () => ({ error: 'old release failure' }) }); await flush();
  assert.equal(Number(h.get('pedal-throttle').value), 45); assert.equal(h.actions().at(-1).body.throttle, .45);
  assert.equal(h.get('notice').classList.contains('error'), false); h.dispose();
});

test('physical standard gamepad uses analog axes, triggers and buttons only in gamepad mode', async () => {
  const h = await dashboard(); h.state.gamepads = [physicalPad({ steering: .56, throttle: .62, brake: .19, pressed: [0, 5, 12] })];
  h.interval(100); await flush(); assert.equal(h.actions().length, 0);
  h.mode('gamepad'); await flush(); h.interval(100); await flush(); assert.equal(h.actions().length, 0, 'held controller must first return to neutral');
  h.state.gamepads = [physicalPad()]; h.interval(100); await flush();
  h.state.gamepads = [physicalPad({ steering: .56, throttle: .62, brake: .19, pressed: [0, 5, 12] })]; h.interval(100); await flush();
  const action = h.actions().at(-1).body;
  assert.ok(action.steering > 0 && action.steering < .56); assert.equal(action.throttle, .62); assert.equal(action.brake, .19);
  assert.deepEqual(action.buttons.sort(), ['A', 'DPAD_UP', 'RB']);
  assert.match(h.get('gamepad-name').textContent, /Test standard controller/);
  h.state.gamepads = [physicalPad({ steering: .08 })]; h.interval(100); await flush();
  assert.equal(h.app.controls.actionState().steering, 0); assert.ok(h.releases().length >= 1); h.dispose();
});

test('Space latches physical gamepad until all held controls return to neutral', async () => {
  const h = await dashboard(); h.mode('gamepad'); h.state.gamepads = [physicalPad()]; h.interval(100); await flush();
  h.state.gamepads = [physicalPad({ throttle: .8 })]; h.interval(100); await flush(); assert.equal(h.actions().at(-1).body.throttle, .8);
  h.emit(h.get('terminal'), 'keydown', { code: 'Space' }); await flush();
  const count = h.actions().length; h.interval(100); h.interval(100); await flush(); assert.equal(h.actions().length, count);
  h.state.gamepads = [physicalPad()]; h.interval(100); await flush();
  h.state.gamepads = [physicalPad({ throttle: .4 })]; h.interval(100); await flush(); assert.equal(h.actions().at(-1).body.throttle, .4); h.dispose();
});

test('held physical input does not resume across blur, terminal change or mode change', async () => {
  for (const trigger of [
    (h) => { h.emit(h.window, 'blur'); h.emit(h.window, 'focus'); },
    (h) => { h.get('terminal').value = 'two'; h.emit(h.get('terminal'), 'change'); },
    (h) => { h.mode('racing'); h.mode('gamepad'); },
  ]) {
    const h = await dashboard(); h.mode('gamepad'); h.state.gamepads = [physicalPad()]; h.interval(100); await flush();
    h.state.gamepads = [physicalPad({ throttle: .7 })]; h.interval(100); await flush();
    const count = h.actions().length; trigger(h); await flush(); h.interval(100); await flush();
    assert.equal(h.actions().length, count); assert.equal(h.app.controls.actionState().throttle, 0);
    assert.ok(h.releases().some((request) => request.url === '/api/terminals/one/release')); h.dispose();
  }
});

test('a replacement session for the same terminal releases keyboard, racing and physical input', async (t) => {
  for (const mode of ['keyboard', 'racing', 'gamepad']) await t.test(mode, async () => {
    const h = await dashboard(); h.state.terminals[0].session_id = 'old-session'; h.mode(mode); await flush();
    if (mode === 'keyboard') h.press('throttle');
    else if (mode === 'racing') setRange(h, 'pedal-throttle', 55);
    else {
      h.state.gamepads = [physicalPad()]; h.interval(100); await flush();
      h.state.gamepads = [physicalPad({ throttle: .55 })]; h.interval(100);
    }
    await flush(); const actions = h.actions().length; const releases = h.releases().length;
    h.state.terminals = [{ ...h.state.terminals[0], session_id: 'replacement-session' }, h.state.terminals[1]];
    h.emit(h.get('refresh'), 'click'); await flush(); h.interval(100); await flush();
    assert.equal(h.app.selected, 'one'); assert.equal(h.app.current().session_id, 'replacement-session');
    assert.equal(h.releases().length, releases + 1); assert.equal(h.actions().length, actions);
    assert.equal(h.evaluate('held.size'), 0); assert.equal(h.app.controls.actionState().throttle, 0);
    if (mode === 'racing') assert.equal(Number(h.get('pedal-throttle').value), 0);
    if (mode === 'gamepad') {
      h.state.gamepads = [physicalPad()]; h.interval(100); await flush();
      h.state.gamepads = [physicalPad({ throttle: .3 })]; h.interval(100); await flush();
      assert.equal(h.actions().at(-1).body.throttle, .3);
    }
    h.dispose();
  });
});

test('physical gamepad disconnect releases input and unsupported mapping never drives', async () => {
  const h = await dashboard(); h.mode('gamepad'); h.state.gamepads = [physicalPad()]; h.interval(100); await flush();
  h.state.gamepads = [physicalPad({ steering: -.8 })]; h.interval(100); await flush(); assert.ok(h.actions().at(-1).body.steering < 0);
  const releases = h.releases().length; h.state.gamepads = []; h.interval(100); await flush();
  assert.equal(h.releases().length, releases + 1); assert.equal(h.app.controls.actionState().steering, 0);
  const count = h.actions().length; h.state.gamepads = [{ ...physicalPad({ throttle: 1 }), mapping: '' }]; h.interval(100); await flush(); assert.equal(h.actions().length, count); h.dispose();
});

test('virtual gamepad remains usable when the browser cannot expose physical controllers', async () => {
  const h = await dashboard(); h.window.navigator.getGamepads = () => { throw new Error('Gamepad access unavailable'); };
  h.mode('gamepad'); h.interval(100); await flush();
  h.press('throttle'); await flush(); h.press('A', 2); await flush();
  assert.equal(h.actions().at(-1).body.throttle, 1); assert.deepEqual(h.actions().at(-1).body.buttons, ['A']);
  assert.equal(h.get('gamepad-throttle').textContent, '100%');
  h.press('left', 3); await flush(); assert.equal(h.get('gamepad-steering').textContent, '-100%');
  h.press('brake', 4); await flush(); assert.equal(h.get('gamepad-brake').textContent, '100%');
  h.emit(h.get('release'), 'click'); await flush(); assert.equal(h.evaluate('held.size'), 0); h.dispose();
});

function enableVideo(h) { h.state.video = { enabled: true, available: true, codec: 'H264', whep_url: '/api/terminals/one/video/whep' }; }

test('WHEP negotiates recvonly H264 and cleans up its session and peer', async () => {
  const h = await dashboard(); enableVideo(h); h.app.syncVideo(); await flush();
  assert.equal(h.peers.length, 1); const peer = h.peers[0];
  assert.equal(peer.transceiver.options.direction, 'recvonly'); assert.deepEqual([...peer.transceiver.codecs].map((codec) => codec.mimeType), ['video/H264']);
  assert.equal(h.sessions().length, 1); assert.equal(h.sessions()[0].options.headers['Content-Type'], 'application/sdp');
  assert.equal(peer.remoteDescription.type, 'answer'); assert.ok(h.get('frame').srcObject);
  h.dispose(); await flush(); assert.equal(peer.closed, true); assert.equal(h.get('frame').srcObject, null);
  assert.ok(h.requestLog.some((request) => request.options.method === 'DELETE' && request.url.includes('/video/sessions/')));
});

test('video unavailable is explicit and never falls back to JPEG polling', async () => {
  const h = await dashboard(); h.app.syncVideo(); await flush();
  assert.equal(h.peers.length, 0); assert.match(h.get('placeholder-detail').textContent, /测试中未启用视频/);
  assert.equal(h.requestLog.some((request) => /\/frame(?:\?|$)|\/stream(?:\?|$)/.test(request.url)), false); h.dispose();
});

test('ICE must complete before an offer is posted; timeout allows a later clean retry', async () => {
  const h = await dashboard(); enableVideo(h); h.state.ice = 'gathering'; h.app.syncVideo(); await flush();
  assert.equal(h.sessions().length, 0); h.advance(5000); await flush();
  assert.match(h.get('placeholder-detail').textContent, /ICE/); assert.equal(h.sessions().length, 0);
  h.state.ice = 'complete'; h.advance(1500); await flush(); assert.equal(h.sessions().length, 1); assert.equal(h.peers[0].closed, true); h.dispose();
});

test('ICE gathering resumes negotiation after the completion event', async () => {
  const h = await dashboard(); enableVideo(h); h.state.ice = 'gathering'; h.app.syncVideo(); await flush();
  const peer = h.peers[0]; assert.equal(h.sessions().length, 0);
  peer.iceGatheringState = 'complete'; peer.event('icegatheringstatechange'); await flush(); assert.equal(h.sessions().length, 1); h.dispose();
});

test('a late WHEP answer after selection cancellation deletes its old session', async () => {
  const h = await dashboard(); enableVideo(h); const answer = deferred();
  h.state.handler = (request) => request.url.endsWith('/video/whep') ? answer.promise : undefined;
  h.app.syncVideo(); await flush(); const peer = h.peers[0]; const pending = h.sessions()[0];
  h.app.select(''); answer.resolve(sdpResponse('/api/terminals/one/video/sessions/late')); await flush();
  assert.equal(pending.options.signal.aborted, true); assert.equal(peer.closed, true); assert.equal(peer.remoteDescription, undefined);
  assert.ok(h.requestLog.some((request) => request.options.method === 'DELETE' && request.url.endsWith('/late'))); h.dispose();
});

test('hidden/pagehide close video; visible/pageshow start a new peer', async () => {
  const h = await dashboard(); enableVideo(h); h.app.syncVideo(); await flush();
  const first = h.peers[0]; h.document.hidden = true; h.emit(h.document, 'visibilitychange'); await flush(); assert.equal(first.closed, true);
  h.document.hidden = false; h.emit(h.document, 'visibilitychange'); await flush(); assert.equal(h.peers.length, 2);
  h.emit(h.window, 'pagehide'); await flush(); assert.equal(h.peers[1].closed, true);
  h.emit(h.window, 'pageshow'); await flush(); assert.equal(h.app.online, true); assert.equal(h.peers.length, 3); h.dispose();
});

test('a changed terminal session replaces video even if connected status stays true', async () => {
  const h = await dashboard(); enableVideo(h); h.state.terminals[0].session_id = 'previous'; h.app.syncVideo(); await flush();
  const previous = h.peers[0]; h.state.terminals = [{ terminal_id: 'one', name: 'One', connected: true, session_id: 'replacement' }, h.state.terminals[1]];
  h.emit(h.get('refresh'), 'click'); await flush(); assert.equal(previous.closed, true); assert.equal(h.peers.length, 2); h.dispose();
});

test('failed peer reconnects and prolonged decoded-frame stall also reconnects', async () => {
  const h = await dashboard(); enableVideo(h); h.app.syncVideo(); await flush();
  h.peers[0].connectionState = 'failed'; h.peers[0].onconnectionstatechange(); h.advance(1500); await flush(); assert.equal(h.peers.length, 2);
  const next = h.peers[1]; h.advance(9000); await flush(); assert.match(h.get('placeholder-detail').textContent, /没有新帧/);
  h.advance(1500); await flush(); assert.equal(next.closed, true); assert.equal(h.peers.length, 3); h.dispose();
});

test('video FPS comes from presented frames while capture target remains separate', async () => {
  const h = await dashboard(); enableVideo(h); h.state.terminals[0].capture = { fps: 60 }; h.app.renderTerminal(); h.app.syncVideo(); await flush();
  h.get('frame').videoWidth = 1280; h.get('frame').videoHeight = 720;
  h.state.decoded = 24; h.get('frame').videoCallback(0, { presentedFrames: 24 }); h.advance(1000); await flush();
  assert.equal(h.get('fps').textContent, '24.0 FPS'); assert.equal(h.get('capture-fps').textContent, '60 FPS'); assert.equal(h.get('resolution').textContent, '1280 × 720');
  assert.match(h.get('frame-time').textContent, /24/); assert.equal(h.get('placeholder').hidden, true); h.dispose();
});

test('cancelling ICE gathering never posts an offer and removes the waiting peer', async () => {
  const h = await dashboard(); enableVideo(h); h.state.ice = 'gathering'; h.app.syncVideo(); await flush();
  const peer = h.peers[0]; h.app.player.stop(); await flush(); peer.iceGatheringState = 'complete'; peer.event('icegatheringstatechange'); await flush();
  assert.equal(peer.closed, true); assert.equal(h.sessions().length, 0); h.advance(6000); await flush(); assert.equal(h.peers.length, 1); h.dispose();
});

test('video metadata requests remain bounded even when they have a lifecycle signal', async () => {
  const h = await dashboard(); let aborted = false;
  h.state.handler = (request) => request.url.endsWith('/video') ? new Promise((_resolve, reject) => {
    request.options.signal.addEventListener('abort', () => { aborted = true; reject(Object.assign(new Error('timeout'), { name: 'AbortError' })); });
  }) : undefined;
  h.app.syncVideo(); await flush(); h.advance(6000); await flush();
  assert.equal(aborted, true); assert.equal(h.peers.length, 0); assert.match(h.get('placeholder-detail').textContent, /timeout/); h.dispose();
});

test('without presentation callbacks FPS uses measured decoded-frame deltas', async () => {
  const h = await dashboard(); enableVideo(h); h.get('frame').requestVideoFrameCallback = undefined;
  h.app.syncVideo(); await flush(); h.state.decoded = 17; h.advance(1000); await flush();
  assert.equal(h.get('fps').textContent, '17.0 FPS'); assert.match(h.get('frame-time').textContent, /实际解码/); h.dispose();
});

test('decoded first frame reveals hidden video before a presentation callback can fire', async () => {
  const h = await dashboard(); enableVideo(h); h.get('frame').hidden = true;
  h.app.syncVideo(); await flush(); h.state.decoded = 1; h.advance(1000); await flush();
  assert.equal(h.get('frame').hidden, false); assert.equal(h.get('placeholder').hidden, true);
  assert.equal(h.get('fps').textContent, '0.0 FPS', '解码首帧不能冒充实际呈现帧率');
  h.dispose();
});

test('ended video tracks schedule a new session rather than freezing on the last frame', async () => {
  const h = await dashboard(); enableVideo(h); h.app.syncVideo(); await flush();
  const peer = h.peers[0]; h.emit(peer.track, 'ended'); h.advance(1500); await flush();
  assert.equal(peer.closed, true); assert.equal(h.peers.length, 2); h.dispose();
});
