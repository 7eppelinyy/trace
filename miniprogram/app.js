// app.js - Trace Mini-Program Entry
const DEFAULT_LOCAL_ENDPOINT = 'http://127.0.0.1:8000/api/v1';
const DEFAULT_CLOUD_ENDPOINT = 'https://trace-api-319136-10-1495327666.sh.run.tcloudbase.com/api/v1';
const DEFAULT_LAN_ENDPOINT = 'http://172.20.10.3:8000/api/v1';

App({
  globalData: {
    apiBase: DEFAULT_LOCAL_ENDPOINT,
    localEndpoint: DEFAULT_LOCAL_ENDPOINT,
    cloudEndpoint: DEFAULT_CLOUD_ENDPOINT,
    lanEndpoint: DEFAULT_LAN_ENDPOINT,
    userId: 'user_default',
    systemInfo: null,
    isOnline: true,
    latencyMs: 0
  },

  onLaunch() {
    // 获取设备信息与微信右上角胶囊尺寸（适配顶部安全区与触觉反馈）
    let isDevTools = false;
    try {
      const sysInfo = wx.getSystemInfoSync();
      this.globalData.systemInfo = sysInfo;
      isDevTools = (sysInfo && sysInfo.platform === 'devtools');
      const menuButton = wx.getMenuButtonBoundingClientRect ? wx.getMenuButtonBoundingClientRect() : null;
      this.globalData.menuButton = menuButton;
      const statusBarHeight = (sysInfo && sysInfo.statusBarHeight) || 20;
      const navBarHeight = menuButton ? (menuButton.top - statusBarHeight) * 2 + menuButton.height : 44;
      this.globalData.statusBarHeight = statusBarHeight;
      this.globalData.navBarHeight = navBarHeight;
    } catch (e) {
      console.warn('getSystemInfoSync failed:', e);
    }

    // 优先读取本地存储配置的 API 服务端点
    const storedApiBase = wx.getStorageSync('apiBase');
    const isObsoleteTunnel = storedApiBase && (storedApiBase.includes('loca.lt') || storedApiBase.includes('ujjxx') || storedApiBase.includes('gkemt') || storedApiBase.includes('abkqh') || storedApiBase.includes('pinggy.net'));
    const isLoopback = storedApiBase && (storedApiBase.includes('127.0.0.1') || storedApiBase.includes('localhost'));

    // 在开发者工具中优先使用 127.0.0.1；在真机上默认使用 Google Cloud 生产专线
    if (storedApiBase && !isObsoleteTunnel && (isDevTools || !isLoopback)) {
      this.globalData.apiBase = storedApiBase;
    } else {
      const defaultEndpoint = isDevTools ? DEFAULT_LOCAL_ENDPOINT : DEFAULT_CLOUD_ENDPOINT;
      this.globalData.apiBase = defaultEndpoint;
      wx.setStorageSync('apiBase', defaultEndpoint);
    }

    // 初始化微信云托管能力（免域名/免备案内网直连）
    if (wx.cloud) {
      wx.cloud.init({
        env: 'trace-prod-d0g7s6tv2aba0f5cb',
        traceUser: true
      });
    }

    // 后台服务健康自检（支持智能主备自动切换）
    this.checkBackendHealth();
  },

  checkBackendHealth(callback) {
    const currentBase = this.globalData.apiBase;
    const t0 = Date.now();
    const isCloudHost = currentBase.includes('tcloudbase.com');

    const handleSuccess = (res) => {
      if (res.statusCode === 200) {
        this.globalData.isOnline = true;
        this.globalData.latencyMs = Date.now() - t0;
        console.log('[Trace] Connected to:', currentBase, 'Latency:', this.globalData.latencyMs, 'ms');
        if (typeof callback === 'function') callback(true, currentBase, this.globalData.latencyMs);
      } else {
        this._tryAlternativeEndpoint(callback);
      }
    };

    const handleFail = () => {
      this._tryAlternativeEndpoint(callback);
    };

    if (isCloudHost && wx.cloud && wx.cloud.callContainer) {
      wx.cloud.callContainer({
        config: {
          env: 'trace-prod-d0g7s6tv2aba0f5cb'
        },
        path: '/api/v1/health',
        method: 'GET',
        header: {
          'X-WX-SERVICE': 'trace-api',
          'content-type': 'application/json'
        },
        timeout: 8000,
        success: handleSuccess,
        fail: handleFail
      });
      return;
    }

    wx.request({
      url: `${currentBase}/health`,
      method: 'GET',
      header: { 'bypass-tunnel-reminder': '1' },
      timeout: 8000,
      success: handleSuccess,
      fail: handleFail
    });
  },

  _tryAlternativeEndpoint(callback) {
    const isDevTools = this.globalData.systemInfo && this.globalData.systemInfo.platform === 'devtools';
    // 若当前端点不可达，自动探测备选端点（本地环回、局域网与云端生产互为备援）
    let altBase = DEFAULT_CLOUD_ENDPOINT;
    if (this.globalData.apiBase === DEFAULT_LOCAL_ENDPOINT) {
      altBase = DEFAULT_CLOUD_ENDPOINT;
    } else if (this.globalData.apiBase === DEFAULT_CLOUD_ENDPOINT) {
      altBase = isDevTools ? DEFAULT_LOCAL_ENDPOINT : DEFAULT_LAN_ENDPOINT;
    } else {
      altBase = isDevTools ? DEFAULT_LOCAL_ENDPOINT : DEFAULT_CLOUD_ENDPOINT;
    }
    const t0 = Date.now();
    const isCloudHost = altBase.includes('tcloudbase.com');

    const handleSuccess = (res) => {
      if (res.statusCode === 200) {
        const latency = Date.now() - t0;
        this.globalData.isOnline = true;
        this.globalData.apiBase = altBase;
        this.globalData.latencyMs = latency;
        wx.setStorageSync('apiBase', altBase);
        console.log('[Trace] Auto failover switched to alternative endpoint:', altBase);
        if (typeof callback === 'function') callback(true, altBase, latency);
      } else {
        this.globalData.isOnline = false;
        if (typeof callback === 'function') callback(false);
      }
    };

    const handleFail = (err) => {
      this.globalData.isOnline = false;
      console.warn('[Trace] Both endpoints unreachable:', err);
      if (typeof callback === 'function') callback(false, null, 0, err);
    };

    if (isCloudHost && wx.cloud && wx.cloud.callContainer) {
      wx.cloud.callContainer({
        config: {
          env: 'trace-prod-d0g7s6tv2aba0f5cb'
        },
        path: '/api/v1/health',
        method: 'GET',
        header: {
          'X-WX-SERVICE': 'trace-api',
          'content-type': 'application/json'
        },
        timeout: 8000,
        success: handleSuccess,
        fail: handleFail
      });
      return;
    }

    wx.request({
      url: `${altBase}/health`,
      method: 'GET',
      header: { 'bypass-tunnel-reminder': '1' },
      timeout: 8000,
      success: handleSuccess,
      fail: handleFail
    });
  }
});
