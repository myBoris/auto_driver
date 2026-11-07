/* 只读订阅终端确认的执行状态；不把外部状态加入本地输入或续发驾驶指令。 */
(() => {
  'use strict';
  const namespace = window.AutoDriver = window.AutoDriver || {};
  const statuses = ['idle', 'active', 'released', 'expired', 'unconfirmed', 'disconnected'];
  const buttons = new Set(['A', 'B', 'X', 'Y', 'LB', 'RB', 'BACK', 'START', 'GUIDE', 'LTHUMB', 'RTHUMB', 'DPAD_UP', 'DPAD_DOWN', 'DPAD_LEFT', 'DPAD_RIGHT']);
  const finite = (value, low, high) => typeof value === 'number' && Number.isFinite(value) && value >= low && value <= high;
  const neutral = () => ({ steering: 0, throttle: 0, brake: 0, buttons: [], duration_ms: 0 });

  class ControlMonitor {
    constructor({ api, getContext, onState }) {
      Object.assign(this, { api, getContext, onState });
      this.key = ''; this.active = false; this.generation = 0; this.revision = -1;
      this.timer = null; this.expiryTimer = null; this.controller = null; this.inFlight = false;
      this.expiresAt = 0; this.destroyed = false; this.suspended = false; this.listeners = [];
      this.listen(document, 'visibilitychange', () => this.sync());
      this.listen(window, 'pagehide', () => { this.suspended = true; this.sync(); });
      this.listen(window, 'pageshow', () => { this.suspended = false; this.sync(); });
      this.sync();
    }

    listen(target, type, listener) { target.addEventListener(type, listener); this.listeners.push([target, type, listener]); }
    context() { return this.getContext() || {}; }
    contextKey(context = this.context()) { return `${context.terminalId || ''}/${context.sessionId || ''}`; }
    usable() { return !this.destroyed && !this.suspended && !document.hidden; }
    current(generation) { return this.usable() && this.active && generation === this.generation && this.key === this.contextKey(); }

    invalidate() {
      this.generation += 1; this.revision = -1; this.expiresAt = 0;
      this.controller?.abort(); this.controller = null;
      if (this.timer !== null) clearTimeout(this.timer); this.timer = null;
      if (this.expiryTimer !== null) clearTimeout(this.expiryTimer); this.expiryTimer = null;
      this.onState(null);
    }

    sync() {
      if (this.destroyed) return;
      const context = this.context(); const key = this.contextKey(context);
      const active = this.usable() && !!context.connected && !!context.terminalId && !!context.sessionId;
      if (key !== this.key || active !== this.active) { this.invalidate(); this.key = key; this.active = active; }
      if (this.active && this.timer === null && !this.inFlight) void this.poll(this.generation);
    }

    schedule(delay) {
      if (!this.current(this.generation) || this.timer !== null) return;
      const generation = this.generation;
      this.timer = setTimeout(() => { this.timer = null; void this.poll(generation); }, delay);
    }

    static valid(snapshot) {
      const state = snapshot?.state;
      return !!state && Number.isSafeInteger(snapshot.revision) && snapshot.revision >= 0
        && typeof snapshot.connected === 'boolean' && statuses.includes(snapshot.status)
        && finite(snapshot.remaining_ms, 0, 5000)
        && finite(state.steering, -1, 1) && finite(state.throttle, 0, 1) && finite(state.brake, 0, 1)
        && Number.isInteger(state.duration_ms) && state.duration_ms >= 0 && state.duration_ms <= 5000
        && Array.isArray(state.buttons) && state.buttons.length <= buttons.size && state.buttons.every(button => buttons.has(button));
    }

    apply(snapshot, context) {
      if (!ControlMonitor.valid(snapshot) || snapshot.terminal_id !== context.terminalId || snapshot.session_id !== context.sessionId) throw new Error('控制状态格式或终端会话不匹配');
      if (snapshot.revision < this.revision) return;
      const now = performance.now();
      // remaining_ms 是服务端生成响应时的剩余时间；长轮询等待不能计作指令已执行时间。
      let expiresAt = now + snapshot.remaining_ms;
      if (snapshot.revision === this.revision && this.expiresAt) expiresAt = Math.min(expiresAt, this.expiresAt);
      this.revision = snapshot.revision; this.expiresAt = expiresAt;
      if (this.expiryTimer !== null) clearTimeout(this.expiryTimer); this.expiryTimer = null;
      if (snapshot.status === 'active' && expiresAt <= now) {
        this.onState({ ...snapshot, status: 'expired', state: neutral(), remaining_ms: 0 }); return;
      }
      this.onState(snapshot);
      if (snapshot.status === 'active') {
        const generation = this.generation; const revision = this.revision;
        this.expiryTimer = setTimeout(() => {
          this.expiryTimer = null;
          if (this.current(generation) && this.revision === revision) this.onState({ ...snapshot, status: 'expired', state: neutral(), remaining_ms: 0 });
        }, Math.max(1, expiresAt - now));
      }
    }

    async poll(generation) {
      if (!this.current(generation) || this.inFlight) return;
      const context = this.context(); const controller = new AbortController();
      this.controller = controller; this.inFlight = true; let delay = 0;
      try {
        const query = `session_id=${encodeURIComponent(context.sessionId)}&timeout=20${this.revision >= 0 ? `&after=${this.revision}` : ''}`;
        const snapshot = await this.api.json(`/api/terminals/${encodeURIComponent(context.terminalId)}/control?${query}`, {
          timeout: 25000, signal: controller.signal, retryGuard: () => this.current(generation),
        });
        if (this.current(generation)) {
          this.apply(snapshot, context);
          if (!snapshot.connected) delay = 1000;
        }
      } catch (error) {
        delay = 1000;
        if (this.current(generation)) {
          if (this.expiryTimer !== null) clearTimeout(this.expiryTimer); this.expiryTimer = null;
          this.onState(null);
        }
      } finally {
        this.inFlight = false;
        if (this.controller === controller) this.controller = null;
        if (this.current(this.generation)) this.schedule(generation === this.generation ? delay : 0);
      }
    }

    destroy() {
      this.destroyed = true; this.active = false; this.invalidate();
      for (const [target, type, listener] of this.listeners) target.removeEventListener(type, listener);
      this.listeners = [];
    }
  }

  namespace.ControlMonitor = ControlMonitor;
})();
