/**
 * 派猫猫旅行 · 接口抓包辅助脚本（改进版）
 * ==========================================
 * 改进点：
 *   1. 排除第三方遥测/统计域名噪声（如 galileotelemetry.tencent.com /collect），不再误标。
 *   2. 精准区分「触发派发」[CAT-TRAVEL] 与「领取」[CAT-CLAIM]，不再把领取误标成 TRAVEL。
 *
 * 用法：
 *   1. WorkBuddy 桌面端打开「成长计划」页面。
 *   2. F12 / Ctrl+Shift+I 打开开发者工具 → Console。
 *   3. 把本段整段粘贴进去，回车运行。
 *   4. 点击「派猫猫旅行」按钮 → 控制台打印 [CAT-TRAVEL] 段（method=POST，真实 url）。
 *   5. 旅行到达后点击「领取积分」→ 控制台打印 [CAT-CLAIM] 段。
 *   6. 把对应段落发给 AI，回填到 cat_travel.py 的 CONFIG。
 *
 * 本脚本仅在 DevTools 控制台运行，不修改文件、不发送额外请求。
 */
(function () {
  'use strict';

  // 第三方遥测/统计域名或路径片段，一律忽略，避免噪声
  const BLOCKED = ['galileotelemetry.tencent.com', 'aegis', '/collect'];

  function isBlocked(url) {
    const u = (url || '').toLowerCase();
    return BLOCKED.some((b) => u.includes(b));
  }

  // 分类：返回 'TRAVEL'（触发派发）或 'CLAIM'（领取）或 null（忽略）
  function classify(url, method) {
    const u = (url || '').toLowerCase();
    const m = (method || 'GET').toUpperCase();
    if (isBlocked(u)) return null;
    // 领取：url 以 /claim 结尾，或含 travel/claim
    if (/\/claim(\?|#|$)/.test(u) || u.indexOf('travel/claim') !== -1) return 'CLAIM';
    // 触发派发：含 travel 且为 POST，且不是 status/claim
    if (u.indexOf('travel') !== -1 && m === 'POST' && u.indexOf('status') === -1 && u.indexOf('claim') === -1) {
      return 'TRAVEL';
    }
    return null;
  }

  function logRequest(label, method, url, requestBody, responseBody, headers) {
    console.log('\n%c' + label, 'color:#fff;background:#f0a93c;padding:2px 6px;border-radius:4px;font-weight:bold;');
    console.log('method:', method);
    console.log('url:', url);
    console.log('headers:', headers ? JSON.stringify(headers, null, 2) : '(无法读取)');
    console.log('requestBody:', requestBody);
    console.log('responseBody:', responseBody);
  }

  function normalizeHeaders(h) {
    if (!h) return null;
    try {
      if (typeof Headers !== 'undefined' && h instanceof Headers) {
        const o = {};
        h.forEach((v, k) => { o[k] = v; });
        return o;
      }
      if (typeof h === 'object') return h;
    } catch (e) {}
    return String(h);
  }

  // 拦截原生 fetch
  const originalFetch = window.fetch;
  window.fetch = async function (...args) {
    const resource = args[0];
    const init = args[1] || {};
    const url = typeof resource === 'string' ? resource : (resource && resource.url);
    const method = init.method || 'GET';
    const reqBody = init.body;
    const headers = init.headers;
    try {
      const response = await originalFetch.apply(this, args);
      response.clone().text().then((text) => {
        const label = classify(url, method);
        if (label) {
          logRequest('[CAT-' + label + '] ' + (label === 'TRAVEL' ? '旅行触发接口' : '领奖接口'),
            method, url, reqBody, text, normalizeHeaders(headers));
        }
      }).catch(() => {});
      return response;
    } catch (err) {
      throw err;
    }
  };

  // 拦截 XMLHttpRequest
  const originalOpen = XMLHttpRequest.prototype.open;
  const originalSend = XMLHttpRequest.prototype.send;
  const originalSetRequestHeader = XMLHttpRequest.prototype.setRequestHeader;

  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this._catMethod = method;
    this._catUrl = url;
    this._catHeaders = {};
    return originalOpen.apply(this, [method, url, ...rest]);
  };

  XMLHttpRequest.prototype.setRequestHeader = function (name, value) {
    if (this._catHeaders) this._catHeaders[name] = value;
    return originalSetRequestHeader.apply(this, [name, value]);
  };

  XMLHttpRequest.prototype.send = function (body) {
    this._catBody = body;
    const xhr = this;
    const originalOnReady = this.onreadystatechange;
    this.onreadystatechange = function () {
      if (xhr.readyState === 4) {
        const label = classify(xhr._catUrl, xhr._catMethod);
        if (label) {
          logRequest('[CAT-' + label + '] ' + (label === 'TRAVEL' ? '旅行触发接口' : '领奖接口'),
            xhr._catMethod, xhr._catUrl, xhr._catBody, xhr.responseText, xhr._catHeaders);
        }
      }
      if (originalOnReady) originalOnReady.apply(this, arguments);
    };
    return originalSend.apply(this, [body]);
  };

  console.log('%c[派猫猫旅行抓包助手] 已启动，请点击「派猫猫旅行」和「领取积分」按钮。', 'color:#2d1b4e;background:#ffd97a;padding:4px 8px;border-radius:4px;font-weight:bold;');
})();
