/* 只读画面辅助：消费外部 AI 结果；演示数据只在本页生成，不发送任何驾驶指令。 */
(() => {
  'use strict';
  const namespace = window.AutoDriver = window.AutoDriver || {};
  const finite = (value, min, max) => typeof value === 'number' && Number.isFinite(value) && value >= min && value <= max;
  const points = (value, minimum) => Array.isArray(value) && value.length >= minimum && value.length <= 512 && value.every((point) => Array.isArray(point) && point.length === 2 && point.every((n) => finite(n, 0, 1)));
  const pointText = (value) => value.map(([x, y]) => `${x * 1000},${y * 1000}`).join(' ');
  const statuses = { waiting: '等待 AI 上传结果', unavailable: 'AI 结果暂不可用', stale: 'AI 结果已过期', no_road: '当前画面未识别到道路' };

  class PerceptionOverlay {
    constructor({ api, video, stage, getContext, pollDelay = 500 }) {
      Object.assign(this, { api, video, stage, getContext, pollDelay });
      this.elements = Object.fromEntries(['overlay-road', 'overlay-direction', 'overlay-demo', 'perception-overlay', 'perception-road', 'perception-direction-line', 'perception-arrow', 'perception-hud', 'perception-status', 'perception-direction', 'perception-metrics', 'perception-source', 'perception-demo-badge'].map((id) => [id, document.getElementById(id)]));
      this.generation = 0; this.key = ''; this.active = false; this.inFlight = false; this.controller = null;
      this.timer = null; this.expiryTimer = null; this.demoTimer = null; this.demo = false; this.demoIndex = 0;
      this.result = null; this.lastVersion = 0; this.lastSequence = null; this.expiresAt = 0; this.status = null; this.destroyed = false; this.suspended = false;
      this.listeners = [];
      this.listen(this.element('overlay-road'), 'change', () => this.sync());
      this.listen(this.element('overlay-direction'), 'change', () => this.sync());
      this.listen(this.element('overlay-demo'), 'click', () => this.setDemo(!this.demo));
      this.listen(window, 'pagehide', () => { this.suspended = true; this.sync(); });
      this.listen(window, 'pageshow', () => { this.suspended = false; this.sync(); });
      this.listen(document, 'visibilitychange', () => this.sync());
      for (const name of ['loadedmetadata', 'resize']) this.listen(video, name, () => this.renderCurrent());
      this.listen(window, 'resize', () => this.renderCurrent());
      this.listen(document, 'fullscreenchange', () => this.renderCurrent());
      if (typeof ResizeObserver === 'function' && stage) { this.observer = new ResizeObserver(() => this.renderCurrent()); this.observer.observe(stage); }
      this.sync();
    }

    element(id) { return this.elements[id]; }
    text(id, value) { const node = this.element(id); if (node) node.textContent = value; }
    hide(id, hidden) {
      const node = this.element(id);
      if (node) { node.hidden = hidden; if (hidden) node.setAttribute('hidden', ''); else node.removeAttribute('hidden'); }
    }
    listen(target, event, callback) { if (target?.addEventListener) { target.addEventListener(event, callback); this.listeners.push([target, event, callback]); } }
    context() { return this.getContext?.() || {}; }
    enabled() { return !!(this.element('overlay-road')?.checked || this.element('overlay-direction')?.checked); }
    // 画面叠加是只读展示；切到发送脚本或截图工具时继续更新，标签页隐藏才暂停。
    usable() { return !this.destroyed && !this.suspended && !document.hidden && this.enabled(); }
    current(generation) { return generation === this.generation && this.active && this.usable(); }

    invalidate() {
      this.generation += 1;
      if (this.timer !== null) clearTimeout(this.timer); this.timer = null;
      this.clearResult();
      this.controller?.abort(); this.controller = null;
      this.lastVersion = 0; this.lastSequence = null; this.expiresAt = 0; this.status = null;
      this.clearGraphics();
    }

    clearResult() {
      if (this.expiryTimer !== null) clearTimeout(this.expiryTimer); this.expiryTimer = null;
      // expiresAt 保留为同一上传版本的期限水位；重复回包不能重新获得完整 TTL。
      this.result = null;
    }

    sync() {
      if (this.destroyed) return;
      const context = this.context();
      const key = `${context.terminalId || ''}/${context.sessionId || ''}`;
      const active = this.usable() && !!context.playing && !!context.terminalId && !!context.sessionId;
      if (key !== this.key || active !== this.active) {
        this.invalidate(); this.key = key; this.active = active;
      }
      if (!this.usable()) {
        this.stopDemoTimer(); this.clearGraphics(); this.hide('perception-hud', true); return;
      }
      this.renderCurrent();
      if (this.active && this.timer === null && !this.inFlight) void this.poll(this.generation);
      this.scheduleDemo();
    }

    schedule(delay = this.pollDelay) {
      if (!this.current(this.generation) || this.timer !== null) return;
      const generation = this.generation;
      this.timer = setTimeout(() => { this.timer = null; void this.poll(generation); }, delay);
    }

    async poll(generation) {
      if (!this.current(generation) || this.inFlight) return;
      const context = this.context(); const controller = new AbortController();
      const requestStartedAt = performance.now();
      this.controller = controller; this.inFlight = true;
      try {
        const body = await this.api.json(`/api/terminals/${encodeURIComponent(context.terminalId)}/perception`, { signal: controller.signal, retryGuard: () => this.current(generation) });
        if (this.current(generation)) this.applyResult(body, context, Math.max(0, performance.now() - requestStartedAt));
      } catch (error) {
        if (this.current(generation) && error.name !== 'AbortError') {
          this.clearResult();
          if (this.demo) this.renderCurrent(); else this.showStatus('unavailable', statuses.unavailable);
        }
      } finally {
        this.inFlight = false;
        if (this.controller === controller) this.controller = null;
        if (this.current(this.generation)) this.schedule(generation === this.generation ? this.pollDelay : 0);
      }
    }

    static validResult(body) {
      if (!body || !['ready', 'no_road'].includes(body.status) || !finite(body.image?.width, 1, 32768) || !finite(body.image?.height, 1, 32768)
          || !finite(body.received_at, 0, 1e20) || !finite(body.age_ms, 0, 60000) || !finite(body.ttl_ms, 1, 60000)) return false;
      if (body.frame?.sequence != null && (!Number.isSafeInteger(body.frame.sequence) || body.frame.sequence < 0)) return false;
      if (body.inference_ms != null && !finite(body.inference_ms, 0, 60000)) return false;
      if (body.status === 'no_road' && body.road != null) return false;
      if (body.road != null && (!points(body.road.polygon, 3) || !points(body.road.centerline, 2) || !finite(body.road.confidence, 0, 1))) return false;
      if (body.prediction != null && (!finite(body.prediction.steering, -1, 1) || !finite(body.prediction.confidence, 0, 1) || !['model', 'external', 'road_geometry', 'demo'].includes(body.prediction.source))) return false;
      return !!(body.road || body.prediction);
    }

    applyResult(body, context = this.context(), requestElapsed = 0) {
      if (!body || body.terminal_id !== context.terminalId || body.session_id !== context.sessionId) {
        this.clearResult(); if (this.demo) this.renderCurrent(); else this.showStatus('waiting', '等待当前终端的 AI 结果'); return;
      }
      if (body.status !== 'ready' && !(body.status === 'no_road' && body.prediction != null)) {
        this.clearResult();
        if (this.demo) this.renderCurrent(); else this.showStatus(body.status, statuses[body.status] || statuses.unavailable);
        return;
      }
      if (!PerceptionOverlay.validResult(body)) {
        this.clearResult(); if (this.demo) this.renderCurrent(); else this.showStatus('unavailable', 'AI 结果格式无效'); return;
      }
      const sequence = body.frame?.sequence;
      if (body.received_at < this.lastVersion || (sequence != null && this.lastSequence != null && sequence < this.lastSequence)) return;
      const now = performance.now();
      // age_ms 在中心生成；保守扣除完整请求往返，避免慢回包把过期结果重新画出来。
      let expiresAt = now + Math.min(10000, body.ttl_ms) - body.age_ms - Math.max(0, requestElapsed);
      // 对同一次上传重复轮询不会延长有效期；同一帧可有后续更新，按中心接收时间区分。
      if (body.received_at === this.lastVersion && this.expiresAt) expiresAt = Math.min(expiresAt, this.expiresAt);
      if (expiresAt <= now) {
        this.clearResult(); if (this.demo) this.renderCurrent(); else this.showStatus('stale', statuses.stale); return;
      }
      this.lastVersion = body.received_at; if (sequence != null) this.lastSequence = sequence;
      this.expiresAt = expiresAt; this.result = body;
      if (this.demo) { this.demo = false; this.updateDemoButton(); this.stopDemoTimer(); }
      if (this.expiryTimer !== null) clearTimeout(this.expiryTimer);
      const generation = this.generation; const version = body.received_at;
      this.expiryTimer = setTimeout(() => {
        this.expiryTimer = null;
        if (this.current(generation) && this.lastVersion === version) {
          this.clearResult();
          if (this.demo) this.renderCurrent(); else this.showStatus('stale', statuses.stale);
        }
      }, Math.max(1, expiresAt - now));
      this.renderCurrent();
    }

    clearGraphics() {
      this.hide('perception-overlay', true);
      for (const id of ['perception-road', 'perception-direction-line', 'perception-arrow']) this.hide(id, true);
    }

    showStatus(state, message) {
      this.status = { state, message };
      this.clearGraphics();
      const hud = this.element('perception-hud'); if (hud) hud.dataset.state = state;
      this.hide('perception-hud', !this.usable() || !this.context().playing);
      this.text('perception-status', message); this.hide('perception-status', false);
      for (const id of ['perception-direction', 'perception-metrics', 'perception-source', 'perception-demo-badge']) this.hide(id, true);
    }

    fit(image, demo = false) {
      if (!this.stage || !this.video || !this.element('perception-overlay')) return null;
      const bounds = this.stage.getBoundingClientRect();
      if (!bounds.width || !bounds.height) return null;
      const sourceAspect = image.width / image.height;
      const videoAspect = this.video.videoWidth > 0 && this.video.videoHeight > 0 ? this.video.videoWidth / this.video.videoHeight : sourceAspect;
      if (!demo && Math.abs(videoAspect / sourceAspect - 1) > .03) return null;
      const width = Math.min(bounds.width, bounds.height * videoAspect); const height = width / videoAspect;
      const overlay = this.element('perception-overlay');
      for (const [name, value] of Object.entries({ left: (bounds.width - width) / 2, top: (bounds.height - height) / 2, width, height })) overlay.style.setProperty(name, `${value}px`);
      return { width, height };
    }

    renderCurrent() {
      if (!this.usable()) return;
      if (this.demo) { this.render(this.demoResult(), true, true); return; }
      if (this.result && performance.now() < this.expiresAt) {
        const demo = this.result.prediction?.source === 'demo' || (typeof this.result.model?.id === 'string' && this.result.model.id.startsWith('demo-'));
        this.render(this.result, demo); return;
      }
      if (this.result) { this.clearResult(); this.showStatus('stale', statuses.stale); return; }
      this.showStatus(this.status?.state || 'waiting', this.status?.message || (this.active ? statuses.waiting : '视频连接后显示 AI 结果'));
    }

    render(body, demo, localDemo = false) {
      const dimensions = this.fit(body.image, demo);
      if (!dimensions) { this.showStatus('waiting', '画面比例已变化，等待新结果'); return; }
      const roadOn = !!this.element('overlay-road')?.checked && !!body.road;
      const directionOn = !!this.element('overlay-direction')?.checked && !!body.prediction;
      this.hide('perception-overlay', !roadOn && !directionOn);
      this.hide('perception-road', !roadOn);
      if (roadOn) {
        this.element('perception-road')?.setAttribute('points', pointText(body.road.polygon));
      }
      this.hide('perception-direction-line', !directionOn); this.hide('perception-arrow', !directionOn);
      if (directionOn) {
        // 模型预测与道路中心线是两个独立结果；箭头始终按 steering 绘制。
        const centerline = body.road?.centerline ? [...body.road.centerline].sort((a, b) => b[1] - a[1]) : null;
        const near = centerline?.[0] || [.5, .74];
        const anchor = [Math.max(.22, Math.min(.78, near[0])), Math.max(.2, Math.min(.9, near[1]))];
        const length = centerline ? Math.max(.1, Math.min(.28, (anchor[1] - centerline.at(-1)[1]) * .65)) : .24;
        const steer = body.prediction.steering;
        const limited = (value) => Math.max(.02, Math.min(.98, value));
        const line = [[anchor[0], anchor[1]], [limited(anchor[0] + steer * .08), limited(anchor[1] - length * .45)], [limited(anchor[0] + steer * .2), limited(anchor[1] - length)]];
        this.element('perception-direction-line')?.setAttribute('points', pointText(line));
        const tip = line.at(-1); const previous = line.at(-2);
        const angle = Math.atan2((tip[1] - previous[1]) * dimensions.height, (tip[0] - previous[0]) * dimensions.width);
        const size = Math.max(8, Math.min(15, dimensions.width / 45));
        const head = [angle - .55, angle + .55].map((direction) => [(tip[0] - Math.cos(direction) * size / dimensions.width) * 1000, (tip[1] - Math.sin(direction) * size / dimensions.height) * 1000]);
        this.element('perception-arrow')?.setAttribute('d', `M ${head[0].join(' ')} L ${tip[0] * 1000} ${tip[1] * 1000} L ${head[1].join(' ')}`);
      }
      const hud = this.element('perception-hud'); if (hud) hud.dataset.state = demo ? 'demo' : 'ready';
      this.hide('perception-hud', false); this.hide('perception-demo-badge', !demo);
      this.text('perception-status', demo ? localDemo ? '本地示意 · 不发送控制指令' : '上传的演示结果' : body.status === 'no_road' ? '未识别道路 · 方向预测独立显示' : body.road ? '道路区域已识别' : '已接收方向预测');
      this.hide('perception-status', false); this.hide('perception-direction', !directionOn);
      if (directionOn) {
        const steering = body.prediction.steering;
        const label = steering < -.08 ? `↖ 左转 ${Math.round(Math.abs(steering) * 100)}%` : steering > .08 ? `↗ 右转 ${Math.round(steering * 100)}%` : '↑ 直行';
        this.text('perception-direction', `${demo ? '示意方向' : body.prediction.source === 'road_geometry' ? '建议方向' : 'AI 预测方向'} · ${label}`);
      }
      const confidence = body.road?.confidence ?? body.prediction?.confidence;
      const metrics = demo ? localDemo ? '固定道路样例 · 每 3 秒切换方向' : '示意数据 · 等待模型接入' : [`${body.road ? '道路' : '预测'}置信 ${Math.round(confidence * 100)}%`, body.inference_ms == null ? '' : `推理 ${Math.round(body.inference_ms)} ms`].filter(Boolean).join(' · ');
      this.text('perception-metrics', metrics); this.hide('perception-metrics', false);
      this.hide('perception-source', !directionOn && !(demo && roadOn));
      if (demo && (roadOn || directionOn)) this.text('perception-source', '演示数据，不代表实时检测结果');
      else if (directionOn) this.text('perception-source', body.prediction.source === 'road_geometry' ? '根据道路走势推算' : body.prediction.source === 'external' ? '外部上传的预测结果' : '模型上传的预测结果');
      if (hud) hud.title = typeof body.model?.label === 'string' ? body.model.label.slice(0, 160) : '';
    }

    demoResult() {
      const steering = [0, .4, -.35][this.demoIndex % 3];
      const centerline = Array.from({ length: 9 }, (_, index) => { const t = index / 8; return [.5 + .045 * t * t, .64 - t * .17]; });
      const sides = centerline.map(([x, y], index) => { const halfWidth = .24 - .21 * index / 8; return [[x - halfWidth, y], [x + halfWidth, y]]; });
      return { image: { width: this.video?.videoWidth || 1600, height: this.video?.videoHeight || 900 }, road: { polygon: [...sides.map(([left]) => left), ...sides.map(([, right]) => right).reverse()], centerline, confidence: 1 }, prediction: { steering, confidence: 1, source: 'demo' } };
    }

    updateDemoButton() {
      const button = this.element('overlay-demo'); if (button) { button.setAttribute('aria-pressed', String(this.demo)); button.textContent = this.demo ? '退出演示' : '演示'; }
    }
    stopDemoTimer() { if (this.demoTimer !== null) clearTimeout(this.demoTimer); this.demoTimer = null; }
    scheduleDemo() {
      if (!this.demo || !this.usable() || this.demoTimer !== null) return;
      this.demoTimer = setTimeout(() => { this.demoTimer = null; this.demoIndex += 1; this.renderCurrent(); this.scheduleDemo(); }, 3000);
    }
    setDemo(enabled) { this.demo = !!enabled; this.demoIndex = 0; this.updateDemoButton(); this.stopDemoTimer(); this.sync(); }
    destroy() {
      this.destroyed = true; this.active = false; this.invalidate(); this.stopDemoTimer(); this.observer?.disconnect();
      for (const [target, event, callback] of this.listeners) target.removeEventListener(event, callback);
      this.listeners = []; this.hide('perception-hud', true);
    }
  }

  namespace.PerceptionOverlay = PerceptionOverlay;
})();
