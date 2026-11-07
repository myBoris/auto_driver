/* 驾驶控制模块：本地输入使用同一动作队列，终端执行状态仅用于展示。 */
(() => {
  'use strict';
  const namespace = window.AutoDriver = window.AutoDriver || {};
  const axes = ['steering', 'throttle', 'brake'];
  const pedals = ['throttle', 'brake', 'clutch'];
  const directions = [...pedals, 'left', 'right'];
  const modes = ['keyboard', 'gamepad', 'racing'];
  const rangeKeys = ['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home', 'End', 'PageUp', 'PageDown'];
  const gamepadButtons = ['A', 'B', 'X', 'Y', 'LB', 'RB', null, null, 'BACK', 'START', 'LTHUMB', 'RTHUMB', 'DPAD_UP', 'DPAD_DOWN', 'DPAD_LEFT', 'DPAD_RIGHT', 'GUIDE'];
  const neutral = () => ({ steering: 0, throttle: 0, brake: 0, buttons: [] });
  const releasedPedals = () => ({ throttle: 0, brake: 0, clutch: 0 });
  const clamp = (value, low, high) => Math.min(high, Math.max(low, Number(value) || 0));
  const nonzero = (state) => axes.some((axis) => Math.abs(state[axis]) > .001) || state.buttons.length > 0;

  class DrivingControls {
    constructor({ api, getTerminalId, canControl, onError, onStatus }) {
      Object.assign(this, { api, getTerminalId, canControl, onError, onStatus });
      this.mode = 'keyboard'; this.held = new Map();
      this.racing = neutral(); this.gamepadState = neutral();
      this.manualPedals = releasedPedals();
      this.pedalPointers = new Map(); this.pedalKeys = new Map();
      this.steeringPointers = new Map(); this.steeringKeys = new Map(); this.steeringValues = new Map(); this.steeringSerial = 0;
      this.gamepadBlocked = true; this.gamepadIndex = null; this.gamepadId = null;
      this.focused = typeof document.hasFocus !== 'function' || document.hasFocus();
      this.wheelPointer = null; this.inputEpoch = 0;
      this.releases = []; this.pendingAction = null; this.running = false;
      this.observedState = null; this.observedSuppression = null;
      this.buttons = [...document.querySelectorAll('[data-control]')];
      this.tabs = [...document.querySelectorAll('[data-mode]')];
      this.panels = [...document.querySelectorAll('[data-mode-panel]')];
      this.pedalResets = [...document.querySelectorAll('[data-pedal-reset]')];
      this.keyControls = new Map([['KeyW', 'throttle'], ['ArrowUp', 'throttle'], ['KeyS', 'brake'], ['ArrowDown', 'brake'], ['KeyC', 'clutch'], ['KeyA', 'left'], ['ArrowLeft', 'left'], ['KeyD', 'right'], ['ArrowRight', 'right'], ['KeyQ', 'X'], ['KeyE', 'B']]);
      this.bind(); this.sync();
      this.renewalTimer = setInterval(() => { const changed = this.pollGamepad(); if (!changed && this.activeInput()) this.queueAction(); }, 100);
    }

    element(id) { return document.getElementById(id); }
    text(id, value) { const element = this.element(id); if (element) element.textContent = value; }
    available() { return this.canControl() && this.focused; }
    activeInput() { return this.held.size > 0 || (this.mode === 'racing' && (pedals.some((axis) => this.manualPedals[axis] > .001) || nonzero(this.racing))) || (this.mode === 'gamepad' && nonzero(this.gamepadState)); }
    buttonMode(button) { return button.dataset.inputMode || 'keyboard'; }

    observationKey(snapshot) { return `${snapshot.terminal_id}\u0000${snapshot.session_id}`; }

    suppressObservedState() {
      if (this.observedState) {
        this.observedSuppression = { key: this.observationKey(this.observedState), revision: this.observedState.revision };
        this.observedState = null;
      }
    }

    localInputEngaged() {
      return this.activeInput() || this.steeringPointers.size > 0 || this.steeringKeys.size > 0 || this.pedalPointers.size > 0 || this.pedalKeys.size > 0;
    }

    setObservedState(snapshot) {
      if (!snapshot) { this.observedState = null; this.sync(); return; }
      const key = this.observationKey(snapshot);
      const revision = Number(snapshot.revision) || 0;
      if (this.observedSuppression?.key === key && revision <= this.observedSuppression.revision) {
        this.observedState = null; this.sync(); return;
      }
      const state = snapshot.state || neutral();
      this.observedState = {
        ...snapshot, revision,
        state: { steering: clamp(state.steering, -1, 1), throttle: clamp(state.throttle, 0, 1), brake: clamp(state.brake, 0, 1), buttons: Array.isArray(state.buttons) ? [...state.buttons] : [] },
      };
      // 本地操作期间仍接收版本号，但不得在松手后恢复先前观察到的按下状态。
      if (this.localInputEngaged()) this.suppressObservedState();
      this.sync();
    }

    executionLabel(state) {
      const steering = Math.round(state.steering * 100);
      const parts = [steering ? `${steering < 0 ? '左' : '右'} ${Math.abs(steering)}%` : '居中', `油门 ${Math.round(state.throttle * 100)}%`, `刹车 ${Math.round(state.brake * 100)}%`];
      if (state.buttons.includes('LB')) parts.push('离合 100%');
      if (state.buttons.includes('X')) parts.push('降挡');
      if (state.buttons.includes('B')) parts.push('升挡');
      return parts.join(' · ');
    }

    sync() {
      const local = this.localInputEngaged();
      const observation = !local ? this.observedState : null;
      const observed = observation ? (observation.status === 'active' && observation.connected !== false ? observation.state : neutral()) : null;
      const active = new Set(observed ? observed.buttons : [...this.held.values(), ...(this.mode === 'gamepad' ? this.gamepadState.buttons : [])]);
      if (observed) {
        if (observed.steering < -.001) active.add('left');
        if (observed.steering > .001) active.add('right');
        if (observed.throttle > .001) active.add('throttle');
        if (observed.brake > .001) active.add('brake');
        if (observed.buttons.includes('LB')) active.add('clutch');
      }
      const available = this.available();
      const pedalState = observed ? { throttle: observed.throttle, brake: observed.brake, clutch: observed.buttons.includes('LB') ? 1 : 0 } : this.mode === 'racing' ? this.pedalState() : releasedPedals();
      const gamepadAction = observed || (this.mode === 'gamepad' ? this.actionState() : neutral());
      const racingSteering = observed ? observed.steering : this.racing.steering;
      this.element('driving-controls')?.classList.toggle('observing-execution', Boolean(observation));
      const execution = this.element('control-execution');
      if (execution) {
        execution.dataset.state = local ? 'local' : observation?.status || 'waiting';
        const status = { idle: '输入已归零', released: '已释放', expired: '指令已到期', unconfirmed: '执行未确认', disconnected: '终端离线' }[observation?.status];
        execution.textContent = local ? `本地操作：${this.executionLabel(this.actionState())}` : observation?.status === 'active' ? `终端执行：${this.executionLabel(observed)}` : `终端执行：${status || '等待状态'}`;
        execution.title = execution.textContent;
      }
      for (const button of this.buttons) {
        const selected = this.buttonMode(button) === this.mode;
        button.disabled = !available || !selected;
        button.classList.toggle('held', selected && active.has(button.dataset.control));
        button.classList.toggle('observed', Boolean(observation) && selected && active.has(button.dataset.control));
        button.setAttribute('aria-pressed', String(selected && active.has(button.dataset.control)));
      }
      for (const tab of this.tabs) {
        const selected = tab.dataset.mode === this.mode;
        tab.setAttribute('aria-selected', String(selected)); tab.tabIndex = selected ? 0 : -1;
        tab.classList.toggle('active', selected);
      }
      for (const panel of this.panels) panel.hidden = panel.dataset.modePanel !== this.mode;
      for (const axis of [...axes, 'clutch']) {
        const setting = this.element(`${axis}-level`);
        if (setting) setting.disabled = !available || this.mode !== 'keyboard';
        if (axis !== 'clutch') this.text(`gamepad-${axis}`, `${Math.round(gamepadAction[axis] * 100)}%`);
      }
      for (const axis of pedals) {
        const input = this.element(`pedal-${axis}`);
        const percent = Math.round(pedalState[axis] * 100);
        if (input) {
          input.disabled = !available || this.mode !== 'racing'; input.value = String(percent);
          input.setAttribute('aria-valuetext', `${percent}%`);
          input.style?.setProperty('--pedal-level', `${percent}%`);
          const pedal = input.closest('.pedal');
          pedal?.style?.setProperty('--pedal-level', `${percent}%`);
          pedal?.classList.toggle('pressed', percent > 0);
        }
        this.text(`pedal-${axis}-value`, `${percent}%`);
      }
      for (const reset of this.pedalResets) reset.disabled = !available || this.mode !== 'racing' || !this.manualPedals[reset.dataset.pedalReset];
      const steeringPercent = Math.round(racingSteering * 100);
      const steeringLabel = steeringPercent ? `${steeringPercent < 0 ? '左' : '右'} ${Math.abs(steeringPercent)}%` : '居中';
      const steeringInput = this.element('racing-steering');
      if (steeringInput) {
        steeringInput.disabled = !available || this.mode !== 'racing'; steeringInput.value = String(steeringPercent);
        steeringInput.style?.setProperty('--steering-fill-start', `${Math.min(50, 50 + steeringPercent / 2)}%`);
        steeringInput.style?.setProperty('--steering-fill-end', `${Math.max(50, 50 + steeringPercent / 2)}%`);
        steeringInput.setAttribute('aria-valuetext', steeringLabel);
      }
      this.text('racing-steering-value', steeringLabel);
      const keyboard = this.element('keyboard');
      if (keyboard) keyboard.disabled = !available || this.mode !== 'keyboard';
      const wheel = this.element('racing-wheel');
      if (wheel) {
        wheel.disabled = !available || this.mode !== 'racing';
        wheel.style?.setProperty('--wheel-angle', `${racingSteering * 135}deg`);
        wheel.setAttribute('aria-valuenow', String(steeringPercent));
        wheel.setAttribute('aria-valuetext', steeringLabel);
        wheel.classList.toggle('held', this.wheelPointer !== null);
      }
      const reset = this.element('wheel-reset');
      if (reset) reset.disabled = !available || this.mode !== 'racing';
      this.element('release').disabled = !this.canControl();
      this.text('control-mode-status', { keyboard: '键盘模式', gamepad: '手柄模式', racing: '赛车模拟' }[this.mode]);
    }

    pedalState() {
      const active = new Set(this.held.values());
      const physical = this.mode === 'gamepad' ? this.gamepadState : neutral();
      const state = this.mode === 'racing' ? { ...this.manualPedals } : releasedPedals();
      for (const axis of pedals) {
        const heldLevel = active.has(axis) ? (this.mode === 'keyboard' ? clamp(this.element(`${axis}-level`)?.value ?? 100, 0, 100) / 100 : 1) : 0;
        const physicalLevel = axis === 'clutch' ? (physical.buttons.includes('LB') || active.has('LB') ? 1 : 0) : physical[axis];
        // 实体手柄与屏幕按键同时操作时保留较大的力度；赛车模式使用手动踏板。
        state[axis] = Math.max(state[axis], heldLevel, physicalLevel);
      }
      return state;
    }

    actionState() {
      const active = new Set(this.held.values());
      const digitalSteering = (active.has('right') ? 1 : 0) - (active.has('left') ? 1 : 0);
      const state = this.mode === 'racing' ? { ...this.racing } : this.mode === 'gamepad' ? { ...this.gamepadState } : neutral();
      if (this.mode !== 'racing') {
        const keyboard = this.mode === 'keyboard';
        const level = (name) => keyboard ? clamp(this.element(`${name}-level`)?.value ?? 100, 0, 100) / 100 : 1;
        if (active.has('left') || active.has('right')) state.steering = digitalSteering * level('steering');
      }
      const pedalState = this.pedalState();
      state.throttle = pedalState.throttle; state.brake = pedalState.brake;
      state.buttons = [...new Set([...state.buttons, ...[...active].filter((value) => !directions.includes(value))])];
      // 当前 Xbox 360 协议只有两个模拟扳机；游戏的离合使用 LB 开关，力度仅保留在踏板界面。
      if (pedalState.clutch > 0 && !state.buttons.includes('LB')) state.buttons.push('LB');
      state.duration_ms = 250;
      return state;
    }

    queueAction() {
      if (!this.available() || !this.activeInput()) return;
      // 最多等待一条确认；慢网络只保留最后状态，释放始终优先。
      this.pendingAction = { id: this.getTerminalId(), action: this.actionState(), epoch: this.inputEpoch, release: false };
      void this.drain();
    }

    queueRelease(id = this.getTerminalId()) {
      if (!id) return;
      if (this.pendingAction?.id === id) this.pendingAction = null;
      if (!this.releases.some((work) => work.id === id)) this.releases.push({ id, epoch: this.inputEpoch, release: true });
      void this.drain();
    }

    async drain() {
      if (this.running) return;
      this.running = true;
      try {
        while (this.releases.length || this.pendingAction) {
          const work = this.releases.shift() || this.pendingAction;
          if (!work.release) this.pendingAction = null;
          try {
            await this.api.request(`/api/terminals/${encodeURIComponent(work.id)}/${work.release ? 'release' : 'actions'}`, {
              method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(work.release ? {} : work.action), keepalive: work.release,
              retryGuard: () => work.release || (work.epoch === this.inputEpoch && this.available() && this.getTerminalId() === work.id && this.activeInput() && JSON.stringify(this.actionState()) === JSON.stringify(work.action)),
            });
            if (this.getTerminalId() === work.id && work.epoch === this.inputEpoch && (!work.release || !this.activeInput())) this.onStatus(work.release ? '已释放' : '已确认');
          } catch (error) {
            if (error.superseded) continue;
            if (this.getTerminalId() === work.id && work.epoch === this.inputEpoch && (!work.release || !this.activeInput())) {
              this.onStatus('执行失败'); this.onError(`控制失败：${error.message}。终端会在指令到期后释放输入。`);
              this.clearInput(); this.sync();
              if (!work.release) this.queueRelease(work.id);
            }
          }
        }
      } finally { this.running = false; }
    }

    setHeld(source, control, mode = this.mode) {
      if (!this.available() || mode !== this.mode) return;
      this.suppressObservedState();
      this.held.set(source, control); this.sync(); this.queueAction();
    }

    inputChanged() { this.suppressObservedState(); this.sync(); if (this.activeInput()) this.queueAction(); else this.queueRelease(); }
    clearHeld(source) { if (this.held.delete(source)) this.inputChanged(); }

    clearInput() {
      this.inputEpoch += 1;
      this.held.clear(); this.racing = neutral(); this.gamepadState = neutral(); this.pendingAction = null;
      this.manualPedals = releasedPedals();
      this.clearPedalSources();
      this.clearSteeringSources();
      this.gamepadBlocked = true; this.wheelPointer = null;
    }

    stop(force = false) {
      const release = force || this.activeInput() || Boolean(this.pendingAction);
      if (release) this.suppressObservedState();
      this.clearInput(); this.sync();
      if (release) this.queueRelease();
    }

    switchMode(mode) {
      if (!modes.includes(mode) || mode === this.mode) return;
      this.stop(); this.mode = mode; this.sync();
      if (mode === 'gamepad') this.pollGamepad();
    }

    pollGamepad() {
      if (this.mode !== 'gamepad') return;
      let pads;
      try { pads = typeof navigator !== 'undefined' && typeof navigator.getGamepads === 'function' ? [...navigator.getGamepads()] : null; } catch (_) { pads = null; }
      const pad = pads?.find((item) => item && item.connected !== false && item.mapping === 'standard');
      const deviceName = pad?.id || (pads ? '连接实体手柄后按任意键识别' : '实体手柄需浏览器支持');
      this.text('gamepad-name', deviceName);
      this.element('gamepad-name').title = deviceName;
      if (!pad) {
        this.text('gamepad-status', pads?.some(Boolean) ? '使用屏幕手柄' : '屏幕手柄已就绪');
        this.gamepadBlocked = true; this.gamepadIndex = null; this.gamepadId = null;
        if (nonzero(this.gamepadState)) { this.gamepadState = neutral(); this.inputChanged(); return true; }
        return;
      }
      if (pad.index !== this.gamepadIndex || pad.id !== this.gamepadId) {
        this.gamepadIndex = pad.index; this.gamepadId = pad.id; this.gamepadBlocked = true;
        if (nonzero(this.gamepadState)) { this.gamepadState = neutral(); this.inputChanged(); }
      }
      const rawSteering = clamp(pad.axes?.[0], -1, 1);
      const steering = Math.abs(rawSteering) <= .12 ? 0 : Math.sign(rawSteering) * (Math.abs(rawSteering) - .12) / .88;
      const value = (index) => clamp(pad.buttons?.[index]?.value ?? (pad.buttons?.[index]?.pressed ? 1 : 0), 0, 1);
      const state = { steering, throttle: value(7), brake: value(6), buttons: gamepadButtons.filter((name, index) => name && (pad.buttons?.[index]?.pressed || value(index) > .5)) };
      // 紧急停止、失焦及切换终端后，实体手柄必须先回中。
      if (!this.available() || document.hidden || (typeof document.hasFocus === 'function' && !document.hasFocus())) {
        this.gamepadBlocked = true;
        if (nonzero(this.gamepadState)) { this.gamepadState = neutral(); this.inputChanged(); }
        this.text('gamepad-status', '手柄输入已暂停'); return;
      }
      if (this.gamepadBlocked) {
        if (nonzero(state)) { this.text('gamepad-status', '请松开按键并让摇杆回中'); return; }
        this.gamepadBlocked = false;
      }
      this.text('gamepad-status', '实体手柄已连接');
      const changed = JSON.stringify(state) !== JSON.stringify(this.gamepadState);
      this.gamepadState = state;
      if (changed) this.inputChanged();
      return changed;
    }

    setRacingAxis(axis, value, source = 'slider') {
      if (axis !== 'steering' || this.mode !== 'racing' || !this.available()) return;
      if (!this.steeringEngaged(source)) { this.sync(); return; }
      this.steeringValues.set(source, { value: clamp(value, -1, 1), order: ++this.steeringSerial });
      this.refreshSteering();
    }

    steeringElement(source) { return this.element(source === 'wheel' ? 'racing-wheel' : 'racing-steering'); }
    steeringEngaged(source) { return [...this.steeringPointers.values(), ...this.steeringKeys.values()].includes(source); }

    refreshSteering() {
      const held = [...this.steeringValues].filter(([source]) => this.steeringEngaged(source)).sort((a, b) => b[1].order - a[1].order);
      const value = held[0]?.[1].value || 0;
      const changed = this.racing.steering !== value;
      this.racing.steering = value;
      if (changed) this.inputChanged(); else this.sync();
    }

    beginSteering(source) {
      this.suppressObservedState();
      if (!this.steeringEngaged(source)) this.steeringValues.set(source, { value: this.racing.steering, order: ++this.steeringSerial });
    }

    clearSteeringSources(source) {
      const captured = [...this.steeringPointers].filter(([, name]) => !source || name === source);
      for (const [id] of captured) this.steeringPointers.delete(id);
      for (const [code, name] of this.steeringKeys) if (!source || name === source) this.steeringKeys.delete(code);
      if (!source || source === 'wheel') this.wheelPointer = null;
      for (const name of ['slider', 'wheel']) if (!source || name === source) this.steeringValues.delete(name);
      for (const [id, name] of captured) {
        const input = this.steeringElement(name);
        if (input?.hasPointerCapture?.(id)) input.releasePointerCapture(id);
      }
    }

    releaseSteeringPointer(event) {
      const source = this.steeringPointers.get(event.pointerId);
      if (!source) return;
      this.steeringPointers.delete(event.pointerId);
      if (source === 'wheel' && this.wheelPointer?.id === event.pointerId) this.wheelPointer = null;
      if (!this.steeringEngaged(source)) this.steeringValues.delete(source);
      this.refreshSteering();
    }

    blurSteering(source) {
      for (const [code, name] of this.steeringKeys) if (name === source) this.steeringKeys.delete(code);
      if (!this.steeringEngaged(source)) this.steeringValues.delete(source);
      this.refreshSteering();
    }

    bindSteeringSlider() {
      const input = this.element('racing-steering');
      if (input) {
        input.addEventListener('pointerdown', (event) => {
          if (event.button !== 0 || this.mode !== 'racing' || !this.available()) return;
          this.beginSteering('slider'); this.steeringPointers.set(event.pointerId, 'slider');
          input.setPointerCapture(event.pointerId);
        });
        for (const name of ['pointerup', 'pointercancel', 'lostpointercapture']) input.addEventListener(name, (event) => this.releaseSteeringPointer(event));
        input.addEventListener('keydown', (event) => {
          if (!rangeKeys.includes(event.code) || event.altKey || event.ctrlKey || event.metaKey || this.mode !== 'racing' || !this.available()) return;
          if (event.repeat && this.steeringKeys.get(event.code) !== 'slider') { event.preventDefault(); return; }
          this.beginSteering('slider'); this.steeringKeys.set(event.code, 'slider');
        });
        input.addEventListener('input', () => this.setRacingAxis('steering', Number(input.value) / 100));
        input.addEventListener('change', () => { if (!this.steeringEngaged('slider')) this.sync(); });
        input.addEventListener('blur', () => this.blurSteering('slider'));
      }
      for (const name of ['pointerup', 'pointercancel']) window.addEventListener(name, (event) => this.releaseSteeringPointer(event));
      window.addEventListener('keyup', (event) => {
        const source = this.steeringKeys.get(event.code);
        if (!source) return;
        this.steeringKeys.delete(event.code);
        if (!this.steeringEngaged(source)) this.steeringValues.delete(source);
        this.refreshSteering();
      });
    }

    setManualPedal(axis, value) {
      if (!pedals.includes(axis) || this.mode !== 'racing' || !this.available()) return;
      // 原生 range 在松手后还可能派发 change/input；只有仍按住的来源可以加力。
      if (!this.pedalEngaged(axis)) { this.sync(); return; }
      this.manualPedals[axis] = clamp(value, 0, 1); this.inputChanged();
    }

    pedalEngaged(axis) { return [...this.pedalPointers.values(), ...this.pedalKeys.values()].includes(axis); }

    clearPedalSources(axis) {
      const captured = [...this.pedalPointers].filter(([, name]) => !axis || name === axis);
      for (const [id] of captured) this.pedalPointers.delete(id);
      for (const [code, name] of this.pedalKeys) if (!axis || name === axis) this.pedalKeys.delete(code);
      for (const [id, name] of captured) {
        const input = this.element(`pedal-${name}`);
        if (input?.hasPointerCapture?.(id)) input.releasePointerCapture(id);
      }
    }

    releasePedal(axis) {
      this.clearPedalSources(axis);
      const changed = this.manualPedals[axis] !== 0;
      this.manualPedals[axis] = 0;
      if (changed) this.inputChanged(); else this.sync();
    }

    releasePedalPointer(event) {
      const axis = this.pedalPointers.get(event.pointerId);
      if (!axis) return;
      this.pedalPointers.delete(event.pointerId);
      if (!this.pedalEngaged(axis)) this.releasePedal(axis);
    }

    bindPedals() {
      for (const axis of pedals) {
        const input = this.element(`pedal-${axis}`);
        if (!input) continue;
        input.addEventListener('pointerdown', (event) => {
          if (event.button !== 0 || this.mode !== 'racing' || !this.available()) return;
          // 保留浏览器的原生拖动与焦点行为，捕获指针保证控件外松手也可释放。
          this.suppressObservedState();
          this.pedalPointers.set(event.pointerId, axis);
          input.setPointerCapture(event.pointerId);
        });
        for (const name of ['pointerup', 'pointercancel', 'lostpointercapture']) input.addEventListener(name, (event) => this.releasePedalPointer(event));
        input.addEventListener('keydown', (event) => {
          if (!rangeKeys.includes(event.code) || event.altKey || event.ctrlKey || event.metaKey || this.mode !== 'racing' || !this.available()) return;
          if (event.repeat && this.pedalKeys.get(event.code) !== axis) { event.preventDefault(); return; }
          this.suppressObservedState();
          this.pedalKeys.set(event.code, axis);
        });
        input.addEventListener('input', () => this.setManualPedal(axis, Number(input.value) / 100));
        input.addEventListener('change', () => { if (!this.pedalEngaged(axis)) this.sync(); });
        input.addEventListener('blur', () => {
          // 第二根手指会转移焦点；仍按住的指针继续有效，键盘来源随焦点释放。
          for (const [code, name] of this.pedalKeys) if (name === axis) this.pedalKeys.delete(code);
          if (!this.pedalEngaged(axis)) this.releasePedal(axis);
        });
      }
      for (const name of ['pointerup', 'pointercancel']) window.addEventListener(name, (event) => this.releasePedalPointer(event));
      window.addEventListener('keyup', (event) => {
        const axis = this.pedalKeys.get(event.code);
        if (!axis) return;
        this.pedalKeys.delete(event.code);
        if (!this.pedalEngaged(axis)) this.releasePedal(axis);
      });
      for (const reset of this.pedalResets) reset.addEventListener('click', () => this.releasePedal(reset.dataset.pedalReset));
    }

    wheelAngle(event) {
      const bounds = this.element('racing-wheel').getBoundingClientRect();
      return Math.atan2(event.clientY - bounds.top - bounds.height / 2, event.clientX - bounds.left - bounds.width / 2) * 180 / Math.PI;
    }

    bindWheel() {
      const wheel = this.element('racing-wheel');
      if (!wheel) return;
      wheel.addEventListener('pointerdown', (event) => {
        if (event.button !== 0 || this.mode !== 'racing' || !this.available() || this.wheelPointer) return;
        event.preventDefault(); wheel.setPointerCapture(event.pointerId);
        this.beginSteering('wheel'); this.steeringPointers.set(event.pointerId, 'wheel');
        this.wheelPointer = { id: event.pointerId, angle: this.wheelAngle(event) }; this.sync();
      });
      wheel.addEventListener('pointermove', (event) => {
        if (this.wheelPointer?.id !== event.pointerId || this.mode !== 'racing' || !this.available()) return;
        event.preventDefault(); const angle = this.wheelAngle(event);
        const delta = ((angle - this.wheelPointer.angle + 540) % 360) - 180;
        this.wheelPointer.angle = angle; this.setRacingAxis('steering', (this.steeringValues.get('wheel')?.value || 0) + delta / 135, 'wheel');
      });
      const release = (event) => {
        this.releaseSteeringPointer(event);
      };
      for (const name of ['pointerup', 'pointercancel', 'lostpointercapture']) wheel.addEventListener(name, release);
      wheel.addEventListener('contextmenu', (event) => event.preventDefault());
      wheel.addEventListener('keydown', (event) => {
        if (this.mode !== 'racing' || !this.available() || event.altKey || event.ctrlKey || event.metaKey || !rangeKeys.includes(event.code)) return;
        if (event.repeat && this.steeringKeys.get(event.code) !== 'wheel') { event.preventDefault(); event.stopPropagation(); return; }
        event.preventDefault(); event.stopPropagation();
        this.beginSteering('wheel'); this.steeringKeys.set(event.code, 'wheel');
        const step = ['PageUp', 'PageDown'].includes(event.code) ? .1 : .05;
        const direction = ['ArrowLeft', 'ArrowDown', 'PageDown'].includes(event.code) ? -1 : 1;
        this.setRacingAxis('steering', event.code === 'Home' ? 0 : event.code === 'End' ? 1 : (this.steeringValues.get('wheel')?.value || 0) + direction * step, 'wheel');
      });
      wheel.addEventListener('blur', () => this.blurSteering('wheel'));
      this.element('wheel-reset')?.addEventListener('click', () => { this.clearSteeringSources(); this.refreshSteering(); });
    }

    isEditing(target, code) {
      if (!(target instanceof Element)) return false;
      const field = target.closest('input, select, textarea, [contenteditable="true"]');
      if (!field) return false;
      if (field.tagName === 'INPUT' && ['checkbox', 'radio', 'button', 'submit', 'reset'].includes(field.type)) return false;
      if (field.tagName === 'INPUT' && field.type === 'range') return ['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home', 'End', 'PageUp', 'PageDown'].includes(code);
      return true;
    }

    bind() {
      this.element('release').addEventListener('click', () => this.stop(true));
      this.element('keyboard')?.addEventListener('change', () => this.stop());
      for (const axis of [...axes, 'clutch']) {
        this.element(`${axis}-level`)?.addEventListener('input', () => { this.text(`${axis}-value`, `${this.element(`${axis}-level`).value}%`); this.sync(); this.queueAction(); });
      }
      this.bindSteeringSlider();
      this.bindPedals();
      for (const tab of this.tabs) {
        tab.addEventListener('click', () => this.switchMode(tab.dataset.mode));
        tab.addEventListener('keydown', (event) => {
          if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.code)) return;
          event.preventDefault(); event.stopPropagation(); const index = modes.indexOf(tab.dataset.mode);
          const next = event.code === 'Home' ? 0 : event.code === 'End' ? modes.length - 1 : (index + (event.code === 'ArrowRight' ? 1 : modes.length - 1)) % modes.length;
          this.switchMode(modes[next]); this.tabs.find((item) => item.dataset.mode === modes[next])?.focus();
        });
      }
      this.buttons.forEach((button, index) => {
        const mode = this.buttonMode(button);
        button.addEventListener('pointerdown', (event) => {
          if (event.button !== 0 || !this.available() || mode !== this.mode) return;
          event.preventDefault(); button.setPointerCapture(event.pointerId); this.setHeld(`pointer:${event.pointerId}`, button.dataset.control, mode);
        });
        const releasePointer = (event) => this.clearHeld(`pointer:${event.pointerId}`);
        for (const name of ['pointerup', 'pointercancel', 'lostpointercapture']) button.addEventListener(name, releasePointer);
        button.addEventListener('contextmenu', (event) => event.preventDefault());
        button.addEventListener('keydown', (event) => {
          if (event.code !== 'Enter' || !this.available() || mode !== this.mode) return;
          event.preventDefault(); if (!event.repeat) this.setHeld(`button:${index}`, button.dataset.control, mode);
        });
        button.addEventListener('keyup', (event) => { if (event.code === 'Enter') { event.preventDefault(); this.clearHeld(`button:${index}`); } });
        button.addEventListener('blur', () => this.clearHeld(`button:${index}`));
      });
      this.bindWheel();
      window.addEventListener('keydown', (event) => {
        if (!this.available() || event.altKey || event.ctrlKey || event.metaKey) return;
        // Space 在所有模式、输入框焦点下均可释放。
        if (event.code === 'Space') { event.preventDefault(); this.stop(true); return; }
        if (this.mode !== 'keyboard' || this.isEditing(event.target, event.code) || !this.element('keyboard')?.checked || !this.keyControls.has(event.code)) return;
        event.preventDefault(); if (!event.repeat) this.setHeld(`key:${event.code}`, this.keyControls.get(event.code));
      });
      window.addEventListener('keyup', (event) => { if (this.held.has(`key:${event.code}`)) { event.preventDefault(); this.clearHeld(`key:${event.code}`); } });
      window.addEventListener('blur', () => { this.focused = false; this.stop(); });
      window.addEventListener('focus', () => { this.focused = true; this.sync(); });
      window.addEventListener('gamepadconnected', () => this.pollGamepad());
      window.addEventListener('gamepaddisconnected', () => this.pollGamepad());
    }
  }

  namespace.DrivingControls = DrivingControls;
})();
