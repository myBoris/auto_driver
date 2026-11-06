/* WebRTC 视频模块：负责 H.264/WHEP 协商、连接回收、断流重连，以及实际播放帧率测量。 */
(() => {
  'use strict';
  const namespace = window.AutoDriver = window.AutoDriver || {};

  class WebRtcPlayer {
    constructor({ api, video, onState = () => {}, onMetrics = () => {}, iceTimeout = 5000, retryDelay = 1500, stallTimeout = 8000 }) {
      Object.assign(this, { api, video, onState, onMetrics, iceTimeout, retryDelay, stallTimeout });
      this.generation = 0; this.terminal = ''; this.peer = null; this.sessionUrl = null; this.controller = null;
      this.retryTimer = null; this.metricsTimer = null; this.frameCallback = null;
      this.presented = 0; this.lastFrameAt = 0; this.measurement = null; this.metricsActive = false;
    }

    current(generation) { return generation === this.generation && !!this.terminal && !document.hidden; }

    state(kind, message) {
      if (this.lastState?.kind === kind && this.lastState.message === message) return;
      this.lastState = { kind, message }; this.onState(this.lastState);
    }

    start(terminal) {
      this.stop(null);
      this.terminal = terminal;
      if (!terminal || document.hidden) return;
      const generation = this.generation;
      this.state('connecting', '正在建立 H.264 视频连接……');
      void this.connect(generation);
    }

    stop(kind = 'idle') {
      // 每次切换或暂停都改变代次；已经返回但属于旧终端的协商结果不能再挂到当前视频上。
      this.generation += 1; this.terminal = '';
      if (this.controller) this.controller.abort(); this.controller = null;
      if (this.retryTimer !== null) clearTimeout(this.retryTimer); this.retryTimer = null;
      if (this.metricsTimer !== null) clearTimeout(this.metricsTimer); this.metricsTimer = null;
      if (this.frameCallback !== null && this.video.cancelVideoFrameCallback) this.video.cancelVideoFrameCallback(this.frameCallback);
      this.frameCallback = null; this.metricsActive = false;
      const peer = this.peer; this.peer = null;
      if (peer) peer.close();
      this.video.pause(); this.video.srcObject = null;
      const session = this.sessionUrl; this.sessionUrl = null;
      if (session) void this.deleteSession(session);
      this.presented = 0; this.measurement = null;
      if (kind) this.state(kind, kind === 'paused' ? '页面已暂停视频，返回后自动恢复。' : '等待游戏终端视频。');
    }

    async deleteSession(url) {
      try { await this.api.request(url, { method: 'DELETE', keepalive: true }); } catch (_) { /* 中心或中继重启后，旧会话可能已经不存在。 */ }
    }

    localUrl(value) {
      const url = new URL(value, window.location.origin);
      if (url.origin !== window.location.origin) throw new Error('视频协商地址必须与中心同源');
      return url.pathname + url.search;
    }

    async connect(generation) {
      const terminal = this.terminal;
      const controller = new AbortController(); this.controller = controller;
      let peer = null; let createdSession = null;
      const current = () => this.current(generation) && !controller.signal.aborted;
      try {
        if (typeof RTCPeerConnection === 'undefined') throw new Error('当前浏览器不支持 WebRTC，请使用新版 Edge 或 Chrome。');
        const metadata = await this.api.json(`/api/terminals/${encodeURIComponent(terminal)}/video`, { signal: controller.signal, retryGuard: current });
        if (!current()) return;
        if (!metadata.enabled || !metadata.available) {
          const reasons = { publisher_starting: '终端正在启动视频，请稍候。', publisher_error: '终端的视频编码或推流失败，请检查终端状态。' };
          this.state('unavailable', metadata.message || reasons[metadata.reason] || (metadata.enabled ? '终端尚未提供 H.264 视频，请检查终端的视频采集与编码状态。' : '中心尚未启用实时视频服务。'));
          this.scheduleReconnect(generation, null, 3000); return;
        }
        const endpoint = this.localUrl(metadata.whep_url);
        peer = new RTCPeerConnection({ iceServers: metadata.ice_servers || [] }); this.peer = peer;
        const transceiver = peer.addTransceiver('video', { direction: 'recvonly' });
        const codecs = typeof RTCRtpReceiver !== 'undefined' ? RTCRtpReceiver.getCapabilities('video')?.codecs || [] : [];
        const h264 = codecs.filter((codec) => codec.mimeType.toLowerCase() === 'video/h264');
        if (!h264.length || !transceiver.setCodecPreferences) throw new Error('当前浏览器未提供 H.264 WebRTC 解码能力。');
        transceiver.setCodecPreferences(h264);
        peer.ontrack = (event) => {
          if (!current()) return;
          this.video.srcObject = event.streams[0] || new MediaStream([event.track]);
          void this.video.play().catch(() => { if (current()) this.state('unavailable', '浏览器未能自动播放视频，请检查页面的自动播放设置。'); });
          event.track.addEventListener('ended', () => this.scheduleReconnect(generation, '视频流已结束，正在重新连接……'));
          event.track.addEventListener('mute', () => this.scheduleReconnect(generation, '视频流暂时中断，正在等待恢复……', 3000));
          event.track.addEventListener('unmute', () => { if (current() && this.retryTimer !== null) { clearTimeout(this.retryTimer); this.retryTimer = null; } });
          this.beginMetrics(generation, peer);
        };
        peer.onconnectionstatechange = () => {
          if (!current()) return;
          if (peer.connectionState === 'connected') {
            if (this.retryTimer !== null) clearTimeout(this.retryTimer); this.retryTimer = null;
            this.beginMetrics(generation, peer);
          } else if (['failed', 'disconnected', 'closed'].includes(peer.connectionState)) this.scheduleReconnect(generation, '视频连接中断，正在重新连接……');
        };
        await peer.setLocalDescription(await peer.createOffer());
        // WHEP 使用一次完整 SDP 交换；先等待 ICE 候选收集结束，并给取消与超时留出明确出口。
        await this.waitForIce(peer, controller.signal);
        if (!current()) return;
        const response = await this.api.request(endpoint, {
          method: 'POST', headers: { 'Content-Type': 'application/sdp' }, body: peer.localDescription.sdp,
          signal: controller.signal, retryGuard: current,
        });
        const location = response.headers.get('Location');
        if (location) createdSession = this.localUrl(location);
        if (!current()) { if (createdSession) void this.deleteSession(createdSession); return; }
        if (response.status !== 201 || !createdSession) throw new Error('中心未返回有效的视频会话');
        this.sessionUrl = createdSession;
        const answer = await response.text();
        if (!current()) return;
        await peer.setRemoteDescription({ type: 'answer', sdp: answer });
      } catch (error) {
        if (!current()) return;
        this.state('unavailable', error.message || '视频连接失败');
        this.scheduleReconnect(generation, null);
      } finally {
        // 旧协商不持有新的播放器资源；即使取消后才收到服务器 Location，也会删除对应旧会话。
        if (!current() && peer && peer !== this.peer) peer.close();
      }
    }

    waitForIce(peer, signal) {
      return new Promise((resolve, reject) => {
        let timer = null;
        const cleanup = () => { if (timer !== null) clearTimeout(timer); peer.removeEventListener('icegatheringstatechange', changed); signal.removeEventListener('abort', aborted); };
        const changed = () => { if (peer.iceGatheringState === 'complete') { cleanup(); resolve(); } };
        const aborted = () => { cleanup(); reject(Object.assign(new Error('视频协商已取消'), { name: 'AbortError' })); };
        if (signal.aborted) { aborted(); return; }
        if (peer.iceGatheringState === 'complete') { resolve(); return; }
        peer.addEventListener('icegatheringstatechange', changed); signal.addEventListener('abort', aborted, { once: true });
        timer = setTimeout(() => { cleanup(); reject(new Error('ICE 候选收集超时，请检查网络连接。')); }, this.iceTimeout);
        changed();
      });
    }

    scheduleReconnect(generation, message, delay = this.retryDelay) {
      if (!this.current(generation) || this.retryTimer !== null) return;
      if (message) this.state('reconnecting', message);
      this.retryTimer = setTimeout(() => {
        this.retryTimer = null;
        if (this.current(generation)) this.start(this.terminal);
      }, delay);
    }

    beginMetrics(generation, peer) {
      if (this.metricsActive || !this.current(generation)) return;
      this.metricsActive = true; this.lastFrameAt = performance.now();
      const frame = (_now, metadata) => {
        if (!this.current(generation) || this.peer !== peer) return;
        if (metadata.presentedFrames !== this.presented) this.lastFrameAt = performance.now();
        this.presented = metadata.presentedFrames;
        this.state('playing', 'H.264 视频正在播放');
        this.frameCallback = this.video.requestVideoFrameCallback(frame);
      };
      if (this.video.requestVideoFrameCallback) this.frameCallback = this.video.requestVideoFrameCallback(frame);
      void this.measure(generation, peer);
    }

    async measure(generation, peer) {
      if (!this.current(generation) || this.peer !== peer) return;
      try {
        const reports = await peer.getStats();
        if (!this.current(generation) || this.peer !== peer) return;
        let inbound = null;
        reports.forEach((report) => { if (report.type === 'inbound-rtp' && (report.kind === 'video' || report.mediaType === 'video')) inbound = report; });
        const now = performance.now();
        const decoded = inbound?.framesDecoded || 0;
        const presented = this.video.requestVideoFrameCallback ? this.presented : decoded;
        const previous = this.measurement;
        if (previous && decoded > previous.decoded) this.lastFrameAt = now;
        // 播放帧率来自浏览器实际呈现回调；缺少该 API 时使用解码计数差值，不采用终端配置值。
        const fps = previous ? Math.max(0, (presented - previous.presented) * 1000 / Math.max(1, now - previous.at)) : null;
        this.measurement = { at: now, presented, decoded };
        this.onMetrics({ fps, decoded, presented, width: this.video.videoWidth || inbound?.frameWidth || 0, height: this.video.videoHeight || inbound?.frameHeight || 0,
          framesDropped: inbound?.framesDropped || 0, measurement: this.video.requestVideoFrameCallback ? 'presented' : 'decoded' });
        // 已解码首帧即可显示 video；display:none 可能抑制呈现回调，不能反过来等待它才显示。
        if (decoded > 0) this.state('playing', 'H.264 视频正在播放');
        // 某些中继重启不会立刻改变 peer 状态；持续无新帧也需要重新协商，防止停在最后一帧。
        if (now - this.lastFrameAt > this.stallTimeout) this.scheduleReconnect(generation, '视频长时间没有新帧，正在重新连接……');
      } catch (_) { /* 关闭连接时的统计失败由连接生命周期处理，不打断控制与终端状态刷新。 */ }
      if (this.current(generation) && this.peer === peer) this.metricsTimer = setTimeout(() => { void this.measure(generation, peer); }, 1000);
    }
  }

  namespace.WebRtcPlayer = WebRtcPlayer;
})();
