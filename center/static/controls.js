/* 驾驶控制模块：将鼠标、触控和键盘合成为完整手柄状态，并管理确认、合并与释放。 */
(() => {
  'use strict';
  const namespace = window.AutoDriver = window.AutoDriver || {};

  class DrivingControls {
    constructor({ api, getTerminalId, canControl, onError, onStatus }) {
      Object.assign(this, { api, getTerminalId, canControl, onError, onStatus });
      this.held = new Map();
      this.releases = [];
      this.pendingAction = null;
      this.running = false;
      this.buttons = [...document.querySelectorAll('[data-control]')];
      this.keyControls = new Map([['KeyW', 'throttle'], ['ArrowUp', 'throttle'], ['KeyS', 'brake'], ['ArrowDown', 'brake'], ['KeyA', 'left'], ['ArrowLeft', 'left'], ['KeyD', 'right'], ['ArrowRight', 'right']]);
      this.bind();
      this.renewalTimer = setInterval(() => { if (this.held.size) this.queueAction(); }, 100);
    }

    element(id) { return document.getElementById(id); }

    sync() {
      const active = new Set(this.held.values());
      for (const button of this.buttons) {
        button.disabled = !this.canControl();
        button.classList.toggle('held', active.has(button.dataset.control));
        button.setAttribute('aria-pressed', String(active.has(button.dataset.control)));
      }
      this.element('release').disabled = !this.canControl();
    }

    actionState() {
      const active = new Set(this.held.values());
      return {
        steering: ((active.has('right') ? 1 : 0) - (active.has('left') ? 1 : 0)) * Number(this.element('steering-level').value) / 100,
        throttle: active.has('throttle') ? Number(this.element('throttle-level').value) / 100 : 0,
        brake: active.has('brake') ? 1 : 0,
        buttons: [...active].filter((value) => !['throttle', 'brake', 'left', 'right'].includes(value)),
        duration_ms: 250,
      };
    }

    queueAction() {
      if (!this.canControl() || !this.held.size) return;
      // 同时最多等待一个动作确认；网络较慢时只保留最后的输入状态，避免执行过时的中间动作。
      this.pendingAction = { id: this.getTerminalId(), action: this.actionState(), release: false };
      void this.drain();
    }

    queueRelease(id = this.getTerminalId()) {
      if (!id) return;
      if (this.pendingAction?.id === id) this.pendingAction = null;
      if (!this.releases.some((work) => work.id === id)) this.releases.push({ id, release: true });
      void this.drain();
    }

    async drain() {
      if (this.running) return;
      this.running = true;
      try {
        while (this.releases.length || this.pendingAction) {
          // 释放优先于尚未发送的动作；已经发出的动作仍由终端的短时有效期兜底回中。
          const work = this.releases.shift() || this.pendingAction;
          if (!work.release) this.pendingAction = null;
          try {
            await this.api.request(`/api/terminals/${encodeURIComponent(work.id)}/${work.release ? 'release' : 'actions'}`, {
              method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(work.release ? {} : work.action), keepalive: work.release,
              retryGuard: () => work.release || (this.canControl() && this.getTerminalId() === work.id && this.held.size > 0 && JSON.stringify(this.actionState()) === JSON.stringify(work.action)),
            });
            if (this.getTerminalId() === work.id) this.onStatus(work.release ? '已释放' : '已确认');
          } catch (error) {
            if (error.superseded) continue;
            if (this.getTerminalId() === work.id) {
              this.onStatus('执行失败'); this.onError(`控制失败：${error.message}。终端会在指令到期后释放输入。`);
              this.held.clear(); this.sync();
            }
            if (!work.release) this.queueRelease(work.id);
          }
        }
      } finally { this.running = false; }
    }

    setHeld(source, control) {
      if (!this.canControl()) return;
      this.held.set(source, control); this.sync(); this.queueAction();
    }

    clearHeld(source) {
      if (!this.held.delete(source)) return;
      this.sync();
      if (this.held.size) this.queueAction(); else this.queueRelease();
    }

    stop() { this.held.clear(); this.sync(); this.queueRelease(); }

    isEditing(target, code) {
      if (!(target instanceof Element)) return false;
      const field = target.closest('input, select, textarea, [contenteditable="true"]');
      if (!field) return false;
      if (field.tagName === 'INPUT' && ['checkbox', 'radio', 'button', 'submit', 'reset'].includes(field.type)) return false;
      if (field.tagName === 'INPUT' && field.type === 'range') return ['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home', 'End', 'PageUp', 'PageDown'].includes(code);
      return true;
    }

    bind() {
      this.element('release').addEventListener('click', () => this.stop());
      this.element('keyboard').addEventListener('change', () => this.stop());
      for (const name of ['throttle', 'steering']) this.element(`${name}-level`).addEventListener('input', () => {
        this.element(`${name}-value`).textContent = `${this.element(`${name}-level`).value}%`; this.queueAction();
      });
      for (const button of this.buttons) {
        button.addEventListener('pointerdown', (event) => {
          if (event.button !== 0 || !this.canControl()) return;
          event.preventDefault(); button.setPointerCapture(event.pointerId);
          this.setHeld(`pointer:${event.pointerId}`, button.dataset.control);
        });
        const releasePointer = (event) => this.clearHeld(`pointer:${event.pointerId}`);
        for (const event of ['pointerup', 'pointercancel', 'lostpointercapture']) button.addEventListener(event, releasePointer);
        button.addEventListener('contextmenu', (event) => event.preventDefault());
        button.addEventListener('keydown', (event) => {
          if (event.code !== 'Enter' || !this.canControl()) return;
          event.preventDefault(); if (!event.repeat) this.setHeld(`button:${button.dataset.control}`, button.dataset.control);
        });
        button.addEventListener('keyup', (event) => {
          if (event.code !== 'Enter') return;
          event.preventDefault(); this.clearHeld(`button:${button.dataset.control}`);
        });
        button.addEventListener('blur', () => this.clearHeld(`button:${button.dataset.control}`));
      }
      window.addEventListener('keydown', (event) => {
        if (!this.canControl() || event.altKey || event.ctrlKey || event.metaKey) return;
        // Space 始终先处理，确保滑块、复选框或终端选择器获得焦点时也能紧急释放。
        if (event.code === 'Space') { event.preventDefault(); this.stop(); return; }
        if (this.isEditing(event.target, event.code) || !this.element('keyboard').checked || !this.keyControls.has(event.code)) return;
        event.preventDefault(); if (!event.repeat) this.setHeld(`key:${event.code}`, this.keyControls.get(event.code));
      });
      window.addEventListener('keyup', (event) => {
        if (this.held.has(`key:${event.code}`)) { event.preventDefault(); this.clearHeld(`key:${event.code}`); }
      });
      window.addEventListener('blur', () => this.stop());
    }
  }

  namespace.DrivingControls = DrivingControls;
})();
