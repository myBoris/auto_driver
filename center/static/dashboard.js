/* 页面编排模块：协调终端列表、视频生命周期与驾驶权限；具体网络、视频和输入逻辑由各自模块负责。 */
(() => {
  'use strict';
  const { CenterApi, DrivingControls, WebRtcPlayer } = window.AutoDriver;

  class Dashboard {
    constructor() {
      this.api = new CenterApi(); this.terminals = []; this.selected = ''; this.online = false;
      this.refreshRunning = false; this.leaving = false; this.connectionEpoch = 0;
      this.controls = new DrivingControls({ api: this.api, getTerminalId: () => this.selected,
        canControl: () => this.online && !!this.current()?.connected && !this.leaving && !document.hidden,
        onError: (message) => this.notice(message, true), onStatus: (message) => { this.element('command-status').textContent = message; },
      });
      this.player = new WebRtcPlayer({ api: this.api, video: this.element('frame'),
        onState: (state) => this.renderVideo(state), onMetrics: (metrics) => this.renderMetrics(metrics),
      });
      this.element('refresh').addEventListener('click', () => { void this.refresh(); });
      this.element('terminal').addEventListener('change', () => this.select(this.element('terminal').value));
      document.addEventListener('visibilitychange', () => {
        if (document.hidden) { this.controls.stop(); this.player.stop('paused'); }
        else { this.syncVideo(); void this.refresh(); }
      });
      window.addEventListener('pagehide', () => {
        this.controls.stop(); this.leaving = true; this.connectionEpoch += 1; this.player.stop('paused');
      });
      window.addEventListener('pageshow', () => {
        if (this.leaving) { this.leaving = false; this.online = false; this.renderTerminal(); void this.refresh(); }
      });
      this.refreshTimer = setInterval(() => { if (!document.hidden) void this.refresh(); }, 2000);
      this.renderTerminal(); this.renderVideo({ kind: 'idle', message: '终端上线后会自动建立视频连接。' });
      void this.refresh();
    }

    element(id) { return document.getElementById(id); }
    current() { return this.terminals.find((terminal) => terminal.terminal_id === this.selected); }
    notice(message, error = false) { this.element('notice').textContent = message; this.element('notice').classList.toggle('error', error); }

    renderTerminal() {
      const terminal = this.current();
      this.element('terminal-name').textContent = terminal?.name || '游戏画面';
      this.element('connection-status').classList.toggle('online', this.online);
      this.element('connection-text').textContent = this.online ? `${this.terminals.filter((item) => item.connected).length} 个终端在线` : '正在连接';
      const capture = terminal?.status?.capture || terminal?.capture || {};
      // 采集目标值单独显示，避免把终端设置的 60 FPS 当成浏览器实际播放结果。
      this.element('capture-fps').textContent = capture.fps ? `${Number(capture.fps).toFixed(0)} FPS` : '—';
      const status = terminal?.status || {};
      const mock = status.mock || capture.mock || terminal?.capabilities?.includes('mock');
      this.element('hardware-status').textContent = !terminal ? '等待终端连接' : !terminal.connected ? '终端已离线，等待自动重连' :
        [mock ? '模拟终端' : '已连接', status.capture?.error ? `采集：${status.capture.error}` : '', status.control?.error ? `手柄：${status.control.error}` : ''].filter(Boolean).join(' · ');
      this.controls.sync();
    }

    renderVideo({ kind, message }) {
      const labels = { idle: '未连接', paused: '已暂停', connecting: '正在建立视频', playing: 'WebRTC 直播', unavailable: '视频不可用', reconnecting: '正在重连' };
      this.element('stream-status').textContent = labels[kind] || '等待视频';
      const playing = kind === 'playing';
      this.element('frame').hidden = !playing;
      this.element('placeholder').hidden = playing;
      this.element('placeholder-title').textContent = kind === 'unavailable' ? '实时视频暂不可用' : kind === 'reconnecting' ? '正在恢复视频连接' : '等待游戏视频';
      this.element('placeholder-detail').textContent = message || '';
      if (!playing) {
        this.element('fps').textContent = '—'; this.element('resolution').textContent = '—';
        this.element('frame-time').textContent = kind === 'paused' ? '视频已暂停' : '尚未接收视频';
      }
    }

    renderMetrics({ fps, decoded, width, height, measurement }) {
      this.element('fps').textContent = fps === null ? '测量中' : `${fps.toFixed(1)} FPS`;
      this.element('resolution').textContent = width && height ? `${width} × ${height}` : '—';
      this.element('frame-time').textContent = `已解码 ${decoded} 帧 · ${measurement === 'presented' ? '按实际呈现测量' : '按实际解码测量'}`;
    }

    syncVideo(force = false) {
      if (this.leaving || document.hidden || !this.online || !this.current()?.connected) {
        if (this.player.terminal) this.player.stop(document.hidden || this.leaving ? 'paused' : 'idle');
        return;
      }
      if (force || this.player.terminal !== this.selected) this.player.start(this.selected);
    }

    select(id) {
      if (id === this.selected) { this.renderTerminal(); return; }
      this.controls.stop(); this.player.stop(); this.selected = id;
      this.element('command-status').textContent = '待命'; this.renderTerminal(); this.syncVideo();
    }

    async refresh() {
      if (this.refreshRunning || this.leaving) return;
      this.refreshRunning = true;
      const epoch = this.connectionEpoch;
      try {
        const body = await this.api.json('/api/terminals');
        if (epoch !== this.connectionEpoch || this.leaving) return;
        const wasOnline = this.online; const previousTerminal = this.current();
        const previous = this.selected;
        this.terminals = Array.isArray(body.terminals) ? body.terminals : []; this.online = true;
        const next = this.terminals.some((item) => item.terminal_id === previous && item.connected) ? previous : this.terminals.find((item) => item.connected)?.terminal_id || '';
        const selector = this.element('terminal'); selector.replaceChildren();
        if (!this.terminals.length) selector.add(new Option('等待终端上线', ''));
        for (const terminal of this.terminals) selector.add(new Option(`${terminal.name || terminal.terminal_id}${terminal.connected ? '' : '（离线）'}`, terminal.terminal_id));
        selector.disabled = !this.terminals.length; selector.value = next;
        this.select(next);
        const changedSession = previousTerminal?.session_id && this.current()?.session_id && previousTerminal.session_id !== this.current().session_id;
        this.syncVideo(previous === next && (!wasOnline || !previousTerminal?.connected || changedSession));
        if (!next) this.notice('等待游戏终端上线，上线后将自动连接视频。');
        else if (!wasOnline) this.notice('已接入游戏终端。实时视频与驾驶控制分别连接，训练程序可继续使用观察与动作 API。');
      } catch (error) {
        if (epoch !== this.connectionEpoch || this.leaving) return;
        this.online = false; this.controls.stop(); this.player.stop();
        this.notice(`连接中心失败：${error.message}，正在自动重试。`, true); this.renderTerminal();
      } finally { this.refreshRunning = false; }
    }
  }

  window.AutoDriver.Dashboard = Dashboard;
  // 保留可检查的实例，便于通过浏览器 getStats 诊断实际编码、解码与连接状态。
  window.autoDriverDashboard = new Dashboard();
})();
