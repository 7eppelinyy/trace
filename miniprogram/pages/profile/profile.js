// pages/profile/profile.js - 投研设置与机构席位中心
const app = getApp();
const { getUserId, resetUserId, resetWatchlist, getPreferences, updatePreference } = require('../../utils/api');

const LOCAL_URL = 'http://127.0.0.1:8000/api/v1';
const CLOUD_URL = 'http://34.31.198.232:8000/api/v1';
const LAN_URL = 'http://172.20.10.3:8000/api/v1';

Page({
  data: {
    globalEnabled: true,
    alertThreshold: 7.0,
    quietStart: '22:00',
    quietEnd: '08:00',
    prefRevision: 1,
    cacheSize: 0,
    apiBaseDisplay: '34.31.198.232:8000',
    userIdDisplay: '',
    isOnline: true,
    latencyMs: 0,
    isTunnelMode: true
  },

  openResearch() { wx.navigateTo({url: '/pages/research/research'}); },

  onShow() {
    this.refreshEndpointDisplay();
    this.calcCacheSize();
    this.probeServerStatus();
    this.loadUserPreferences();
  },

  async loadUserPreferences() {
    try {
      const res = await getPreferences();
      if (res && res.global_preference) {
        const gp = res.global_preference;
        this.setData({
          globalEnabled: gp.enabled !== false,
          alertThreshold: gp.threshold || 7.0,
          quietStart: gp.quiet_start || '22:00',
          quietEnd: gp.quiet_end || '08:00',
          prefRevision: gp.revision || 1
        });
      }
    } catch (e) {
      console.warn('[Profile] loadUserPreferences error:', e);
    }
  },

  async toggleGlobalAlert() {
    const prev = this.data.globalEnabled;
    const next = !prev;
    this.setData({ globalEnabled: next });
    wx.vibrateShort({ type: 'light' });

    try {
      const res = await updatePreference({
        enabled: next,
        threshold: this.data.alertThreshold || 7.0,
        quietStart: this.data.quietStart || '22:00',
        quietEnd: this.data.quietEnd || '08:00',
        expectedRevision: this.data.prefRevision
      });
      if (res && res.revision) {
        this.setData({ prefRevision: res.revision });
      }
      wx.showToast({
        title: next ? '已开启全局强提醒' : '已暂停全局提醒',
        icon: 'none',
        duration: 1500
      });
    } catch (err) {
      // 失败回滚
      this.setData({ globalEnabled: prev });
      wx.showToast({
        title: '偏好保存失败，已恢复',
        icon: 'none',
        duration: 2000
      });
    }
  },

  calcCacheSize() {
    try {
      const res = wx.getStorageInfoSync();
      const sizeKb = (res && res.currentSize) || 0;
      this.setData({ cacheSize: sizeKb });
    } catch (e) {
      this.setData({ cacheSize: 0 });
    }
  },

  refreshEndpointDisplay() {
    const base = (app && app.globalData && app.globalData.apiBase) || CLOUD_URL;
    const isTunnel = base.includes('34.31.198.232') || base.includes('pinggy.net') || base.includes('https://');
    const display = base.replace(/^https?:\/\//, '').replace(/\/api\/v1\/?$/, '');
    const uid = getUserId();
    this.setData({
      apiBaseDisplay: display,
      isTunnelMode: isTunnel,
      userIdDisplay: uid,
      isOnline: app ? !!app.globalData.isOnline : true,
      latencyMs: app ? (app.globalData.latencyMs || 0) : 0
    });
  },

  probeServerStatus() {
    if (!app || !app.checkBackendHealth) return;
    app.checkBackendHealth((isOnline, activeBase, latency) => {
      this.refreshEndpointDisplay();
      if (isOnline && latency) {
        this.setData({ isOnline: true, latencyMs: latency });
      } else {
        this.setData({ isOnline: isOnline });
      }
    });
  },

  checkServer() {
    wx.showLoading({ title: '正在检测接口...' });
    const currentBase = (app && app.globalData && app.globalData.apiBase) || CLOUD_URL;
    const t0 = Date.now();

    wx.request({
      url: `${currentBase}/health`,
      method: 'GET',
      header: { 'bypass-tunnel-reminder': '1' },
      timeout: 4000,
      success: (res) => {
        wx.hideLoading();
        if (res.statusCode === 200) {
          const latency = Date.now() - t0;
          this.setData({ isOnline: true, latencyMs: latency });
          if (app && app.globalData) {
            app.globalData.isOnline = true;
            app.globalData.latencyMs = latency;
          }
          const body = (typeof res.data === 'object') ? res.data : {};
          const statusVal = body.status || 'OK';
          const sourcesCount = (body.sources && Array.isArray(body.sources)) ? body.sources.length : 14;
          wx.showModal({
            title: '后端服务在线',
            content: `已成功连接服务端点:\n${currentBase}\n\n• 往返延时: ${latency}ms\n• 状态: HTTP 200 (${statusVal})\n• 监测官方源池: ${sourcesCount} 个`,
            showCancel: false,
            confirmColor: '#0F172A'
          });
        } else {
          this._handleCheckFailure(currentBase, `状态码 ${res.statusCode}`);
        }
      },
      fail: (err) => {
        wx.hideLoading();
        this._handleCheckFailure(currentBase, (err && err.errMsg) || '连接超时');
      }
    });
  },

  _handleCheckFailure(currentBase, errorMsg) {
    const isDomainBlocked = errorMsg.includes('domain list') || errorMsg.includes('fail url not in domain list');
    if (isDomainBlocked) {
      wx.showModal({
        title: '需要放行域名校验',
        content: '检测到域名校验拦截。\n\n【解决方法】：\n• 开发者工具：右上角「详情」->「本地设置」-> 勾选「不校验合法域名、web-view、TLS版本以及HTTPS证书」\n• 手机真机：点击右上角「...」胶囊 ->「开发调试」->「打开调试」放行通信。',
        showCancel: false,
        confirmText: '我知道了',
        confirmColor: '#0F172A'
      });
      return;
    }

    const isCurrentlyLocal = currentBase.includes('127.0.0.1') || currentBase.includes('localhost');
    const altBase = isCurrentlyLocal ? LAN_URL : LOCAL_URL;
    const altLabel = isCurrentlyLocal ? '局域网直连 (172.20.10.3:8000)' : '本地开发服务 (127.0.0.1:8000)';

    wx.showModal({
      title: '当前端点未连通',
      content: `无法连接到 ${currentBase}\n原因: ${errorMsg}\n\n建议排查：\n1. 确认后端服务已运行 (uvicorn trace.api.app:app)\n2. 开发者工具确认勾选「不校验合法域名」\n3. 尝试切换至通道：【${altLabel}】`,
      confirmText: '切换备用',
      cancelText: '取消',
      confirmColor: '#0F172A',
      success: (mRes) => {
        if (mRes.confirm) {
          this._applyNewBase(altBase);
        }
      }
    });
  },

  toggleEndpointQuick() {
    const endpoints = [
      { label: '云端生产专线 (GCP 34.31.198.232)', url: CLOUD_URL },
      { label: '本地开发服务 (127.0.0.1:8000)', url: LOCAL_URL },
      { label: '局域网直连 (172.20.10.3:8000)', url: LAN_URL }
    ];
    wx.showActionSheet({
      itemList: endpoints.map(e => e.label),
      success: (res) => {
        const selected = endpoints[res.tapIndex];
        if (selected) {
          wx.vibrateShort({ type: 'light' });
          this._applyNewBase(selected.url, `已切换为 ${selected.label}`);
        }
      }
    });
  },

  _applyNewBase(newBase, toastMsg) {
    if (app && app.globalData) {
      app.globalData.apiBase = newBase;
    }
    wx.setStorageSync('apiBase', newBase);
    this.refreshEndpointDisplay();
    if (toastMsg) {
      wx.showToast({ title: toastMsg, icon: 'none' });
    }
    this.checkServer();
  },

  editApiBase() {
    const current = (app && app.globalData && app.globalData.apiBase) || CLOUD_URL;
    wx.showModal({
      title: '自定义 API 服务端点',
      editable: true,
      placeholderText: '例如: https://... 或 http://192.168.x.x:8000/api/v1',
      content: current,
      confirmText: '保存',
      cancelText: '取消',
      confirmColor: '#0F172A',
      success: (res) => {
        if (res.confirm && res.content) {
          let newBase = res.content.trim();
          if (!/^https?:\/\//i.test(newBase)) {
            newBase = `http://${newBase}`;
          }
          if (!/\/api\/v1\/?$/i.test(newBase)) {
            newBase = newBase.replace(/\/+$/, '') + '/api/v1';
          }
          this._applyNewBase(newBase);
        }
      }
    });
  },

  clearCache() {
    wx.showLoading({ title: '正在清理...' });
    try {
      // 清除非必要数据缓存，保留用户会话 ID 与 API 端点配置
      wx.removeStorageSync('cached_watchlist');
      if (app && app.globalData) {
        app.globalData.customGraph = null;
        app.globalData.pendingAskQuery = null;
      }
      this.calcCacheSize();
      wx.hideLoading();
      wx.showToast({ title: '已清除本地临时缓存', icon: 'success' });
    } catch (e) {
      wx.hideLoading();
      wx.showToast({ title: '清理失败', icon: 'none' });
    }
  },

  handleResetSession() {
    wx.showModal({
      title: '重置席位会话',
      content: '确定要重置当前设备会话 Token 吗？系统将分配全新的独立用户空间并自动初始化自选池。',
      confirmText: '重置',
      confirmColor: '#DC2626',
      success: (res) => {
        if (res.confirm) {
          const newUid = resetUserId();
          this.setData({ userIdDisplay: newUid });
          wx.showToast({ title: '已分配新席位 Token', icon: 'success' });
        }
      }
    });
  },

  handleResetWatchlist() {
    wx.showModal({
      title: '恢复官方核心池',
      content: '确定要将自选池重置为官方预置的核心标的池（美股+A股龙头）吗？',
      confirmText: '恢复',
      confirmColor: '#0F172A',
      success: async (res) => {
        if (res.confirm) {
          wx.showLoading({ title: '正在恢复...' });
          try {
            await resetWatchlist();
            wx.hideLoading();
            wx.showToast({ title: '已恢复官方核心池', icon: 'success' });
          } catch (e) {
            wx.hideLoading();
            wx.showToast({ title: '恢复失败，请重试', icon: 'none' });
          }
        }
      }
    });
  }
});
