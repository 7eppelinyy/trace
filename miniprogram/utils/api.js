// miniprogram/utils/api.js - 统一 API 客户端与离线容灾封装

const { MOCK_INDICES, MOCK_WATCHLIST, MOCK_EVENTS } = require('./mock');

const CLOUD_ENV_ID = 'trace-prod-d0g7s6tv2aba0f5cb';
const CLOUD_SERVICE_NAME = 'trace-api';

const app = getApp();
function getBaseUrl() {
  const currentApp = getApp();
  return (currentApp && currentApp.globalData && currentApp.globalData.apiBase) || 'http://127.0.0.1:8000/api/v1';
}

/**
 * 统一网络传输底层适配层 (自动适配微信云托管内网容器直连与标准 HTTP)
 * 1. 当目标包含微信云托管 (tcloudbase.com) 时，自动使用 wx.cloud.callContainer 进行内网通信（免配置域名、免 ICP 备案）。
 * 2. 其它情况（如本地 127.0.0.1 联调）自动走标准 wx.request。
 */
function httpTransport(options) {
  const url = options.url || '';
  const isCloudHost = url.includes('tcloudbase.com') || (wx.getStorageSync('apiBase') || '').includes('tcloudbase.com');

  if (isCloudHost && typeof wx !== 'undefined' && wx.cloud && typeof wx.cloud.callContainer === 'function') {
    let path = url;
    try {
      const match = url.match(/^https?:\/\/[^\/]+(\/.*)$/);
      if (match) {
        path = match[1];
      }
    } catch (_) {}
    if (!path.startsWith('/')) {
      path = '/' + path;
    }

    const header = Object.assign({
      'X-WX-SERVICE': CLOUD_SERVICE_NAME,
      'content-type': 'application/json'
    }, options.header || {});

    return wx.cloud.callContainer({
      config: {
        env: CLOUD_ENV_ID
      },
      path: path,
      method: (options.method || 'GET').toUpperCase(),
      header: header,
      data: options.data,
      timeout: options.timeout || 15000,
      success(res) {
        if (typeof options.success === 'function') {
          options.success(res);
        }
      },
      fail(err) {
        console.warn('[CloudContainer] Request failed:', path, err);
        if (typeof options.fail === 'function') {
          options.fail(err);
        }
      }
    });
  }

  return wx.request(options);
}

// A device guest is a server-issued account, not a fabricated local identity.
// Credentials are scoped to the configured backend and never forwarded elsewhere.
function sessionKey() { return 'trace_session:' + getBaseUrl(); }
function getUserId() {
  const session = wx.getStorageSync(sessionKey());
  return session && session.user_id ? session.user_id : '';
}

function userScopedKey(prefix) {
  const uid = getUserId();
  return uid ? `${prefix}:${getBaseUrl()}:${uid}` : `${prefix}:${getBaseUrl()}`;
}

let sessionPromise = null;
let refreshPromise = null;
let sessionGeneration = 0;

function resetUserId() {
  sessionGeneration += 1;
  sessionPromise = null;
  refreshPromise = null;
  const key = sessionKey();
  const session = wx.getStorageSync(key);
  const oldUid = session && session.user_id ? session.user_id : '';
  const base = getBaseUrl();
  if (session && session.session_token) {
    httpTransport({
      url: base + '/auth/session',
      method: 'DELETE',
      header: { Authorization: 'Bearer ' + session.session_token },
      data: session.refresh_token ? { refresh_token: session.refresh_token } : {}
    });
  }
  wx.removeStorageSync(key);
  if (oldUid) {
    wx.removeStorageSync(`cached_watchlist:${base}:${oldUid}`);
  }
  wx.removeStorageSync('cached_watchlist');
  wx.removeStorageSync('trace_session_token');
  wx.removeStorageSync('trace_user_id');
  return '';
}

async function refreshSessionToken() {
  const key = sessionKey();
  const existing = wx.getStorageSync(key);
  if (!existing || !existing.refresh_token) {
    throw { statusCode: 401, data: { detail: '设备会话已失效且无可用刷新凭据。请在设置中明确重置访客会话；重置会建立独立的新账户。' } };
  }
  if (refreshPromise) return refreshPromise;

  const generation = sessionGeneration;
  const base = getBaseUrl();
  const pending = new Promise((resolve, reject) => {
    httpTransport({
      url: base + '/auth/session/refresh',
      method: 'POST',
      data: { refresh_token: existing.refresh_token },
      header: { 'Content-Type': 'application/json' },
      timeout: 10000,
      success(res) {
        if (res.statusCode < 200 || res.statusCode >= 300 || !res.data || !res.data.session_token) {
          reject(res);
          return;
        }
        if (generation !== sessionGeneration || base !== getBaseUrl()) {
          reject({ statusCode: 409, data: { detail: 'Session context changed during refresh' } });
          return;
        }
        const updated = Object.assign({}, existing, res.data);
        wx.setStorageSync(key, updated);
        resolve(updated.session_token);
      },
      fail: reject
    });
  });

  refreshPromise = pending;
  try {
    return await pending;
  } finally {
    if (refreshPromise === pending) refreshPromise = null;
  }
}

async function getSessionToken(forceRefresh = false) {
  const key = sessionKey();
  const existing = wx.getStorageSync(key);
  if (existing && existing.session_token) {
    const isExpired = existing.expires_at && Date.parse(existing.expires_at) <= Date.now();
    if (forceRefresh || isExpired) {
      if (existing.refresh_token) {
        return await refreshSessionToken();
      }
      // Never silently replace an expired account with an empty new account.
      throw { statusCode: 401, data: { detail: '设备会话已失效且无可用刷新凭据。请在设置中明确重置访客会话；重置会建立独立的新账户。' } };
    }
    return existing.session_token;
  }
  if (sessionPromise) return sessionPromise;
  const generation = sessionGeneration;
  const base = getBaseUrl();
  const pending = new Promise((resolve, reject) => {
    httpTransport({
      url: base + '/auth/session',
      method: 'POST',
      data: { grant_type: 'guest' },
      header: { 'Content-Type': 'application/json' },
      timeout: 10000,
      success(res) {
        if (res.statusCode < 200 || res.statusCode >= 300 || !res.data || !res.data.session_token) {
          reject(res);
          return;
        }
        if (generation !== sessionGeneration || base !== getBaseUrl()) {
          reject({ statusCode: 409, data: { detail: 'Session context changed' } });
          return;
        }
        wx.setStorageSync(key, res.data);
        resolve(res.data.session_token);
      },
      fail: reject
    });
  });
  sessionPromise = pending;
  try {
    return await pending;
  } finally {
    if (sessionPromise === pending) sessionPromise = null;
  }
}

const inFlightRequests = new Map();

/**
 * 封装通用 wx.request Promise (自动携带 Bearer Token，支持 401 自动重协商，支持 GET 幂等去重合并)
 */
async function request(options) {
  const method = (options.method || 'GET').toUpperCase();
  const baseUrl = getBaseUrl();
  const url = options.url.startsWith('http') ? options.url : `${baseUrl}${options.url}`;

  // 幂等 GET 请求合并去重 (F13/T11: 消除连点或并发重复拉取)
  const isGet = method === 'GET';
  if (!url.startsWith(baseUrl + '/')) throw new Error('Cross-backend requests are not allowed');
  const reqKey = isGet ? `${baseUrl}:${getUserId()}:${sessionGeneration}:${url}:${JSON.stringify(options.data || {})}` : null;
  if (reqKey && inFlightRequests.has(reqKey)) {
    return inFlightRequests.get(reqKey);
  }

  const p = (async () => {
    let token = null;
    if (!options.public && !url.includes('/auth/session')) {
      token = await getSessionToken();
    }

    const makeReq = (t) => {
      return new Promise((resolve, reject) => {
        const header = Object.assign({
          'Content-Type': 'application/json',
          'bypass-tunnel-reminder': '1'
        }, options.header || {});

        if (t) {
          header['Authorization'] = `Bearer ${t}`;
        }

        httpTransport({
          url,
          method: options.method || 'GET',
          data: options.data,
          header,
          timeout: options.timeout || 15000,
          success(res) {
            if (res.statusCode >= 200 && res.statusCode < 300) {
              let data = res.data;
              if (typeof data === 'string') {
                try {
                  data = JSON.parse(data);
                } catch (_) {}
              }
              resolve(data);
            } else {
              reject(res);
            }
          },
          fail(err) {
            reject(err);
          }
        });
      });
    };

    try {
      return await makeReq(token);
    } catch (err) {
      if (err && err.statusCode === 401 && !options._retry && !url.includes('/auth/session')) {
        options._retry = true;
        try {
          const freshToken = await refreshSessionToken();
          if (freshToken) {
            return await makeReq(freshToken);
          }
        } catch (refreshErr) {
          throw refreshErr;
        }
      }
      throw err;
    }
  })();

  if (reqKey) {
    inFlightRequests.set(reqKey, p);
    const clear = () => { if (inFlightRequests.get(reqKey) === p) inFlightRequests.delete(reqKey); };
    p.then(clear, clear);
  }

  return p;
}

/**
 * 获取实时大盘指数 (SPX, IXIC, DJI, STAR50)
 */
async function getIndices() {
  try {
    const data = await request({ url: '/market/indices', timeout: 8000 });
    if (Array.isArray(data) && data.length > 0) {
      return data;
    }
    return [];
  } catch (e) {
    console.warn('[API] getIndices failed:', e);
    return [];
  }
}

/**
 * 获取自选标的列表及最新行情（带强本地缓存防擦除机制）
 */
async function getWatchlist() {
  const wlKey = userScopedKey('cached_watchlist');
  try {
    const data = await request({ url: '/watchlist', timeout: 20000 });
    if (data && Array.isArray(data.items)) {
      // 服务端返回有效列表（包括合法的空列表 []），真实持久化
      wx.setStorageSync(wlKey, data.items);
      return data.items;
    }
    const cached = wx.getStorageSync(wlKey);
    if (Array.isArray(cached)) {
      return cached;
    }
    return [];
  } catch (e) {
    console.warn('[API] getWatchlist failed, fallback to local cache:', e);
    const cached = wx.getStorageSync(wlKey);
    if (Array.isArray(cached)) {
      return cached;
    }
    return [];
  }
}

/**
 * 将标的加入自选
 */
async function addWatchlist(ticker, companyNameZh = '') {
  return request({
    url: '/watchlist',
    method: 'POST',
    data: { ticker, company_name_zh: companyNameZh },
    timeout: 10000
  });
}

/**
 * 从自选中移除标的
 */
async function removeWatchlist(ticker) {
  return request({
    url: `/watchlist/${encodeURIComponent(ticker)}`,
    method: 'DELETE',
    timeout: 8000
  });
}

/**
 * 清空当前用户全部自选标的
 */
async function clearWatchlist() {
  wx.removeStorageSync(userScopedKey('cached_watchlist'));
  return request({
    url: '/watchlist',
    method: 'DELETE',
    timeout: 8000
  });
}

/**
 * 恢复官方核心自选池
 */
async function resetWatchlist() {
  const data = await request({
    url: '/watchlist/reset',
    method: 'POST',
    timeout: 10000
  });
  if (data && Array.isArray(data.items)) {
    wx.setStorageSync(userScopedKey('cached_watchlist'), data.items);
    return data.items;
  }
  return [];
}

/**
 * 调用 Trace Graph 推理引擎进行归因问答与金融深度推演
 */
async function askQuestion(ticker, question, history = [], options = {}) {
  try {
    const payload = {
      ticker: ticker || '',
      question,
      history,
      mode: options.mode || 'evidence_answer',
      event_id: options.event_id || null,
      event_version: options.event_version || null
    };
    let res = await request({
      url: '/ask',
      method: 'POST',
      data: payload,
      timeout: 60000
    });
    if (typeof res === 'string') {
      try {
        res = JSON.parse(res);
      } catch (_) {}
    }
    if (res && !res.answer) {
      res.answer = res.text || res.content || res.result || '';
    }
    return res;
  } catch (e) {
    console.error('[API] askQuestion error:', e);
    const detail = (e && e.data && e.data.detail) || (e && e.errMsg) || '推演连接超时';
    const baseUrl = getBaseUrl();
    return {
      ticker: ticker || '系统诊断',
      question,
      answer: `大模型推演服务通信异常（${detail}）。\n\n当前服务端点：${baseUrl}\n\n建议排查方案：\n1. 前往【我的/设置】页面，点击“⚡ 检测后端 API 连接状态”或“🔄 切换通道”\n2. 微信开发者工具中勾选“不校验合法域名”\n3. 若使用局域网 IP，请确保手机与电脑连入同一 Wi-Fi`,
      evidence_events: [],
      graph_chain: [],
      status: 'error',
      duration_ms: 0
    };
  }
}

const SOURCE_NAME_MAP = {
  src_cninfo: '巨潮资讯 · 官方披露',
  src_digitimes: '电子时报 · 产业独家',
  src_sec_edgar: '美国 SEC · 官方披露',
  src_fed: '美联储 · 权威声明',
  src_commerce: '美国商务部 · 监管公报',
  src_mofcom: '中国商务部 · 官方公告',
  src_miit: '工信部 · 政策公报',
  src_eetimes: 'EE Times · 产业快报',
  src_nvidia_ir: '英伟达官方 · IR 发布',
  src_micron_ir: '美光科技 · 原厂公告',
  src_sndk_ir: '闪迪/西部数据 · 官方公告',
  src_jin10: '金十 VIP · 机构快讯',
  src_bis: '美国 BIS · 出口管制',
  src_federal_register: '联邦公报 · 监管规则'
};

/**
 * 获取官方源事件流
 */
async function getEvents(opts = 6) {
  let page = 1;
  let limit = 6;
  let market = null;
  let minScore = null;
  let snapshotTs = null;
  let cursor = null;

  if (typeof opts === 'number') {
    limit = opts;
  } else if (typeof opts === 'object' && opts !== null) {
    page = opts.page || 1;
    limit = opts.limit || 6;
    market = opts.market || null;
    minScore = opts.minScore || opts.min_score || null;
    snapshotTs = opts.snapshotTs || opts.snapshot_ts || null;
    cursor = opts.cursor || null;
  }

  let qs = [`page=${page}`, `limit=${limit}`];
  if (market) qs.push(`market=${encodeURIComponent(market)}`);
  if (minScore !== null && minScore !== undefined) qs.push(`min_score=${minScore}`);
  if (snapshotTs) qs.push(`snapshot_ts=${encodeURIComponent(snapshotTs)}`);
  if (cursor) qs.push(`cursor=${encodeURIComponent(cursor)}`);

  try {
    const data = await request({ url: `/events?${qs.join('&')}`, timeout: 8000 });
    if (data && Array.isArray(data.items)) {
      const mapped = data.items.map(item => {
        const sourceLabel = SOURCE_NAME_MAP[item.first_source_id] || (item.first_source_id ? `${item.first_source_id}` : '行业披露');
        const depth = item.transmission_depth || (item.directness === 'indirect' ? 2 : 1);
        const depthLabel = depth === 1 ? '1 级直接传导' : `${depth} 级链式传导`;
        const count = item.impacts_count || (item.impacted_securities ? item.impacted_securities.length : 0);
        const countLabel = `涉及 ${count} 只标的`;
        return {
          event_id: item.event_id,
          title: item.title,
          summary: item.summary,
          source: sourceLabel,
          transmission_depth: depth,
          impacts_count: count,
          impact_level: `${depthLabel} · ${countLabel}`,
          securities: (item.securities && item.securities.length > 0)
            ? item.securities.map(s => ({
                ticker: s.ticker,
                name: s.name || s.ticker,
                change_pct: (s.change_pct !== null && s.change_pct !== undefined) ? s.change_pct : null
              }))
            : (item.impacted_securities || []).map(s => {
                const t = s.replace('SEC-US-', '').replace('SEC-CN-', '');
                return { ticker: t, name: t, change_pct: null };
              })
        };
      });
      mapped.pagination = data.pagination;
      return mapped;
    }
    const empty = [];
    empty.pagination = data ? data.pagination : null;
    return empty;
  } catch (e) {
    console.warn('[API] getEvents error:', e);
    const fallback = [];
    fallback.pagination = null;
    return fallback;
  }
}

/**
 * 获取单个事件的全景详情与证据拓扑
 */
async function getEventDetail(id) {
  try {
    const data = await request({ url: `/events/${id}`, timeout: 6000 });
    return data;
  } catch (e) {
    console.warn('[API] getEventDetail fallback:', e);
    return null;
  }
}

/**
 * 全市场股票模糊智能搜索（支持中文公司名、代码、拼音）
 */
async function searchWatchlist(query) {
  try {
    const data = await request({
      url: `/watchlist/search?q=${encodeURIComponent(query)}`,
      timeout: 5000
    });
    return (data && data.items) ? data.items : [];
  } catch (e) {
    console.warn('[API] searchWatchlist fallback:', e);
    return [];
  }
}

/**
 * 获取当前用户的全局提醒偏好及单标的覆盖
 */
async function getPreferences() {
  try {
    const data = await request({ url: '/preferences', timeout: 5000 });
    return data;
  } catch (e) {
    console.warn('[API] getPreferences fallback:', e);
    return null;
  }
}

/**
 * 更新用户提醒偏好（支持 expected_revision 乐观并发锁）
 */
async function updatePreference({ securityId = null, threshold = 7.0, enabled = true, quietStart = null, quietEnd = null, channel = 'all', expectedRevision = null } = {}) {
  return await request({
    url: '/preferences',
    method: 'PUT',
    data: {
      security_id: securityId,
      threshold,
      enabled,
      quiet_start: quietStart,
      quiet_end: quietEnd,
      channel,
      expected_revision: expectedRevision
    },
    timeout: 5000
  });
}

module.exports = {
  httpTransport,
  request,
  getUserId,
  resetUserId,
  refreshSessionToken,
  getIndices,
  getWatchlist,
  addWatchlist,
  removeWatchlist,
  clearWatchlist,
  resetWatchlist,
  searchWatchlist,
  askQuestion,
  getEvents,
  getEventDetail,
  getPreferences,
  updatePreference
};
