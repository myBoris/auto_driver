/* 中心 API 与浏览器会话模块：统一处理同源 Cookie、超时和会话恢复，不接触终端配对口令。 */
(() => {
  'use strict';
  const namespace = window.AutoDriver = window.AutoDriver || {};

  class CenterApi {
    constructor() { this.sessionRecovery = null; }

    async recoverSession() {
      // 视频协商与手柄请求可能同时遇到中心重启；共享一次恢复请求，避免并发刷新 Cookie。
      if (!this.sessionRecovery) {
        this.sessionRecovery = (async () => {
          const controller = new AbortController();
          const timer = setTimeout(() => controller.abort(), 6000);
          try {
            const response = await fetch('/dashboard-session', { credentials: 'same-origin', cache: 'no-store', signal: controller.signal });
            if (!response.ok) throw new Error('中心会话暂时不可用');
          } finally { clearTimeout(timer); }
        })().finally(() => { this.sessionRecovery = null; });
      }
      return this.sessionRecovery;
    }

    async fetch(path, options = {}) {
      const { retryGuard, ...fetchOptions } = options;
      const request = { ...fetchOptions, credentials: 'same-origin', cache: 'no-store' };
      let response = await fetch(path, request);
      if (response.status === 401) {
        await this.recoverSession();
        // 恢复会话期间，用户可能已经松键或切换终端；旧动作和旧视频协商不能重新发送。
        if (request.signal?.aborted || (retryGuard && !retryGuard())) {
          const error = new Error('请求已被新的操作替代'); error.superseded = true; throw error;
        }
        response = await fetch(path, request);
      }
      return response;
    }

    async request(path, options = {}) {
      const { timeout = 6000, ...requestOptions } = options;
      const controller = new AbortController();
      // 生命周期取消和请求超时同时生效；取消视频协商也会中止尚未读完的 SDP 响应。
      const signal = requestOptions.signal ? AbortSignal.any([requestOptions.signal, controller.signal]) : controller.signal;
      const timer = setTimeout(() => controller.abort(), timeout);
      try {
        const response = await this.fetch(path, { ...requestOptions, signal });
        if (!response.ok) {
          let message = `请求失败（${response.status}）`;
          try { const body = await response.json(); message = body.error || body.message || message; } catch (_) { /* 代理错误页可能不是 JSON。 */ }
          const error = new Error(message); error.status = response.status; throw error;
        }
        return response;
      } finally { clearTimeout(timer); }
    }

    async json(path, options = {}) { return (await this.request(path, options)).json(); }
  }

  namespace.CenterApi = CenterApi;
})();
