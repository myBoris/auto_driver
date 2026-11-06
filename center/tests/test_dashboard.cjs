// 使用 Node 标准库验证页面事件与 WebRTC 生命周期；不连接真实浏览器、视频中继或手柄。
// 执行方式：node --test center/tests/test_dashboard.cjs
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');
const scripts = [...html.matchAll(/<script defer src="\/static\/([^"]+)"><\/script>/g)].map((match) => match[1]);
const controlNames = [...html.matchAll(/data-control="([^"]+)"/g)].map((match) => match[1]);
const names = ['throttle', 'left', 'brake', 'right', 'A', 'B', 'X', 'Y', 'LB', 'RB', 'BACK', 'START'];
const ok = (body = {}) => ({ ok: true, status: 200, json: async () => body });
const sdpResponse = (location = '/api/terminals/one/video/sessions/session-one') => ({ ok: true, status: 201, headers: new Map([['Location', location]]), text: async () => 'v=0\r\ns=answer\r\n' });
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; };
const flush = async () => { for (let i = 0; i < 5; i += 1) await new Promise((resolve) => setImmediate(resolve)); };

class Element {
  constructor(id = '', tag = 'div', type = '') {
    Object.assign(this, { id, tagName: tag.toUpperCase(), type, value: '', checked: false, disabled: false, hidden: false, textContent: '', dataset: {}, listeners: new Map(), attributes: new Map(), options: [] });
    const classes = new Set();
    this.classList = { toggle(name, active) { if (active) classes.add(name); else classes.delete(name); }, contains(name) { return classes.has(name); } };
  }
  addEventListener(name, callback) { const callbacks = this.listeners.get(name) || []; callbacks.push(callback); this.listeners.set(name, callbacks); }
  removeEventListener(name, callback) { this.listeners.set(name, (this.listeners.get(name) || []).filter((item) => item !== callback)); }
  setAttribute(name, value) { this.attributes.set(name, value); }
  removeAttribute(name) { this.attributes.delete(name); }
  closest() { return ['INPUT', 'SELECT', 'TEXTAREA'].includes(this.tagName) || this.contentEditable === 'true' ? this : null; }
  setPointerCapture(id) { this.pointerCapture = id; }
  replaceChildren() { this.options = []; }
  add(option) { this.options.push(option); }
  pause() { this.paused = true; }
  async play() { this.paused = false; }
  requestVideoFrameCallback(callback) { this.videoCallback = callback; return 1; }
  cancelVideoFrameCallback() { this.videoCallback = null; }
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
  const controls = controlNames.map((name) => { const element = new Element(name, 'button'); element.dataset.control = name; return element; });
  for (const [id, tag, type, value] of [['terminal', 'select', '', ''], ['keyboard', 'input', 'checkbox', ''], ['throttle-level', 'input', 'range', '60'], ['steering-level', 'input', 'range', '70']]) {
    const element = new Element(id, tag, type); element.value = value; elements.set(id, element);
  }
  document.getElementById = (id) => { if (!elements.has(id)) elements.set(id, new Element(id)); return elements.get(id); };
  document.querySelectorAll = () => controls;
  const state = { handler: null, terminals: [], video: { enabled: false, available: false, message: '测试中未启用视频' }, ice: 'complete', autoConnect: true, decoded: 0 };
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
  const context = vm.createContext({ document, window, Element, Option: class { constructor(text, value) { Object.assign(this, { text, value }); } }, AbortController, AbortSignal, URL, performance: { now: () => clock.now }, fetch,
    RTCPeerConnection: Peer, RTCRtpReceiver: { getCapabilities() { return { codecs: [{ mimeType: 'video/VP8' }, { mimeType: 'video/H264', sdpFmtpLine: 'packetization-mode=1' }] }; } }, MediaStream: class {},
    setTimeout(callback, ms) { const id = ++timerId; timers.set(id, { callback, ms, at: clock.now + ms }); return id; },
    clearTimeout(id) { timers.delete(id); },
    setInterval(callback, ms) { intervals.set(ms, callback); },
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
  return { state, context, window, document, controls, requestLog, emit, app, peers, get: document.getElementById,
    evaluate: (code) => vm.runInContext(code.replace(/\bheld\b/g, 'window.autoDriverDashboard.controls.held').replace(/\bselected\b/g, 'window.autoDriverDashboard.selected').replace(/\bcenterOnline\b/g, 'window.autoDriverDashboard.online').replace(/\bcenterFetch\(/g, 'window.autoDriverDashboard.api.fetch('), context),
    actions: () => requestLog.filter((request) => request.url.endsWith('/actions')),
    sessions: () => requestLog.filter((request) => request.url.endsWith('/video/whep')),
    releases: () => requestLog.filter((request) => request.url.endsWith('/release')),
    press(name, id = 1) { return emit(controls.find((button) => button.dataset.control === name), 'pointerdown', { pointerId: id }); },
    lift(name, id = 1, type = 'pointerup') { return emit(controls.find((button) => button.dataset.control === name), type, { pointerId: id }); },
    interval: (ms) => intervals.get(ms)(),
    advance(ms) { clock.now += ms; for (const [id, timer] of [...timers]) if (timer.at <= clock.now) { timers.delete(id); timer.callback(); } },
    dispose() { app.leaving = true; app.player.stop(); },
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
