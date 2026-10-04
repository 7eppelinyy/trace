// pages/watchlist/watchlist.js - 自选监控与阈值预警
const { getWatchlist, addWatchlist, removeWatchlist, clearWatchlist, resetWatchlist, searchWatchlist, getPreferences, updatePreference } = require('../../utils/api');
const { formatPrice, formatPct, isPositive, shortTicker } = require('../../utils/format');
const { getUiSettings } = require('../../utils/ui');

Page({
  data: {
    watchlist: [],
    filteredList: [],
    currentMarket: 'all',
    usCount: 0,
    cnCount: 0,
    showSearchSheet: false,
    searchQuery: '',
    searchResults: [],
    searchFocused: false,
    navInset: 64, reduceMotion: false, editing: false,
    filterQuery: '', sheetClosing: false, draggingIndex: -1
  },

  searchTimer: null,

  // 滑动触控临时变量
  touchStartX: 0,
  touchStartY: 0,
  activeSwipeIdx: -1,

  onLoad() {
    this.setData(getUiSettings());
    this.loadData();
  },

  onShow() {
    this.setData(getUiSettings());
    this.loadData();
  },
  onUnload() { clearTimeout(this.searchTimer); clearTimeout(this._sheetCloseTimer); },
  toggleEditing() { this.closeAllSwipes(); this.setData({ editing: !this.data.editing }); },
  onFilterInput(e) { this.setData({ filterQuery: e.detail.value }); this.applyFilter(this.data.currentMarket); },

  onPullDownRefresh() {
    this.loadData(() => {
      wx.stopPullDownRefresh();
    });
  },

  async loadData(callback) {
    try {
      const [rawList, prefs] = await Promise.all([
        getWatchlist(),
        getPreferences().catch(() => null)
      ]);

      let overrideMap = {};
      let globalEnabled = true;
      if (prefs) {
        if (prefs.global_preference) {
          globalEnabled = prefs.global_preference.enabled !== false;
        }
        if (Array.isArray(prefs.overrides)) {
          prefs.overrides.forEach(o => {
            if (o.security_id) {
              overrideMap[o.security_id] = o.enabled !== false;
            }
          });
        }
      }

      const formatted = (rawList || []).map(item => ({
        ...item,
        shortCode: shortTicker(item.ticker),
        formattedPrice: formatPrice(item.last_price, item.market),
        formattedPct: formatPct(item.change_pct),
        isUp: isPositive(item.change_pct),
        hasQuote: item.last_price !== null && item.last_price !== undefined && item.change_pct !== null && item.change_pct !== undefined && Number.isFinite(Number(item.last_price)) && Number.isFinite(Number(item.change_pct)),
        pushEnabled: overrideMap[item.security_id] !== undefined ? overrideMap[item.security_id] : globalEnabled,
        offsetX: 0
      }));

      const usCount = formatted.filter(x => x.market === 'US').length;
      const cnCount = formatted.filter(x => x.market === 'CN').length;

      this.setData({
        watchlist: formatted,
        usCount,
        cnCount
      }, () => {
        this.applyFilter(this.data.currentMarket);
      });
    } catch (e) {
      console.error('[Watchlist] load data error:', e);
    } finally {
      if (typeof callback === 'function') callback();
    }
  },

  switchMarket(e) {
    const market = e.currentTarget.dataset.market;
    this.setData({ currentMarket: market });
    this.closeAllSwipes();
    this.applyFilter(market);
  },

  applyFilter(market) {
    let list = this.data.watchlist;
    if (market !== 'all') {
      list = list.filter(item => item.market === market);
    }
    const query = this.data.filterQuery.trim().toLowerCase();
    if (query) list = list.filter(item => [item.ticker, item.company_name_zh, item.company_name_en].some(value => value && value.toLowerCase().includes(query)));
    this.setData({ filteredList: list });
  },

  // ---------------- 滑动操作 (Swipe to Delete / Pin) ----------------
  onTouchStart(e) {
    if (e.touches.length !== 1) return;
    this.touchStartX = e.touches[0].clientX;
    this.touchStartY = e.touches[0].clientY;
    const idx = e.currentTarget.dataset.index;
    this.touchStartOffset = (this.data.filteredList[idx] || {}).offsetX || 0;
    this.touchAxis = null;
    this._rpxRatio = 750 / (wx.getSystemInfoSync().windowWidth || 375);

    // 如果之前有展开的卡片且不是当前卡片，先收起
    if (this.activeSwipeIdx !== -1 && this.activeSwipeIdx !== idx) {
      this.closeAllSwipes();
    }
  },

  onTouchMove(e) {
    if (e.touches.length !== 1) return;
    const currentX = e.touches[0].clientX;
    const currentY = e.touches[0].clientY;
    const dx = currentX - this.touchStartX;
    const dy = currentY - this.touchStartY;

    if (!this.touchAxis && Math.max(Math.abs(dx), Math.abs(dy)) > 8) {
      this.touchAxis = Math.abs(dx) > Math.abs(dy) ? 'horizontal' : 'vertical';
    }
    if (this.touchAxis === 'horizontal') {
      const idx = e.currentTarget.dataset.index;
      const list = this.data.filteredList;
      if (!list[idx]) return;

      // 转换为 rpx (基于屏幕宽度)
      let offsetRpx = this.touchStartOffset + dx * this._rpxRatio;

      // 限制左滑最大展开 240rpx，右滑不超过 0
      if (offsetRpx < -240) offsetRpx = -240;
      if (offsetRpx > 0) offsetRpx = 0;

      list[idx].offsetX = offsetRpx;
      this.setData({ [`filteredList[${idx}].offsetX`]: offsetRpx, draggingIndex: idx });
    }
  },

  onTouchEnd(e) {
    this.setData({ draggingIndex: -1 });
    if (this.touchAxis !== 'horizontal') return;
    const idx = e.currentTarget.dataset.index;
    const list = this.data.filteredList;
    const item = list[idx];
    if (!item) return;

    if (item.offsetX < -90) {
      item.offsetX = -240;
      this.activeSwipeIdx = idx;
    } else {
      item.offsetX = 0;
      if (this.activeSwipeIdx === idx) this.activeSwipeIdx = -1;
    }

    this.setData({ filteredList: list });
  },

  closeAllSwipes() {
    const list = this.data.filteredList.map(it => ({ ...it, offsetX: 0 }));
    this.activeSwipeIdx = -1;
    this.setData({ filteredList: list });
  },

  async deleteTicker(e) {
    wx.vibrateShort({ type: 'medium' });
    const idx = e.currentTarget.dataset.index;
    const list = this.data.filteredList;
    const target = list[idx];
    if (!target) return;

    // 乐观更新全局与当前视图
    const prevWatchlist = this.data.watchlist;
    const updatedWatchlist = this.data.watchlist.filter(x => x.ticker !== target.ticker);
    const usCount = updatedWatchlist.filter(x => x.market === 'US').length;
    const cnCount = updatedWatchlist.filter(x => x.market === 'CN').length;

    this.setData({
      watchlist: updatedWatchlist,
      usCount,
      cnCount
    }, () => {
      this.closeAllSwipes();
      this.applyFilter(this.data.currentMarket);
    });

    wx.setStorageSync('cached_watchlist', updatedWatchlist);

    wx.showToast({
      title: `已移除 ${target.shortCode}`,
      icon: 'none',
      duration: 1200
    });

    try {
      await removeWatchlist(target.ticker);
    } catch (err) {
      console.error('[Watchlist] removeWatchlist remote error:', err);
      // 若后端持久化失败则回滚状态
      this.setData({ watchlist: prevWatchlist }, () => {
        this.applyFilter(this.data.currentMarket);
      });
      wx.setStorageSync('cached_watchlist', prevWatchlist);
      wx.showToast({ title: '网络异常，标的移除未同步', icon: 'none' });
    }
  },

  promptClearWatchlist() {
    wx.showModal({
      title: '清空自选',
      content: '确定要清空全部自选监控标的吗？（清空后可随时重新录入或恢复默认池）',
      confirmText: '清空',
      confirmColor: '#DC2626',
      success: async (res) => {
        if (res.confirm) {
          wx.showLoading({ title: '正在清空...' });
          try {
            await clearWatchlist();
            wx.hideLoading();
            this.setData({
              watchlist: [],
              filteredList: [],
              usCount: 0,
              cnCount: 0
            });
            wx.showToast({ title: '已清空自选池', icon: 'success' });
          } catch (e) {
            wx.hideLoading();
            wx.showToast({ title: '清空失败，请重试', icon: 'none' });
          }
        }
      }
    });
  },

  promptResetWatchlist() {
    wx.showModal({
      title: '恢复默认池',
      content: '确定要重置并恢复官方核心自选池（美股+A股半导体与AI龙头）吗？',
      confirmText: '恢复',
      confirmColor: '#0F172A',
      success: async (res) => {
        if (res.confirm) {
          wx.showLoading({ title: '正在恢复...' });
          try {
            await resetWatchlist();
            wx.hideLoading();
            this.loadData();
            wx.showToast({ title: '已恢复官方核心池', icon: 'success' });
          } catch (e) {
            wx.hideLoading();
            wx.showToast({ title: '恢复失败，请重试', icon: 'none' });
          }
        }
      }
    });
  },

  pinTicker(e) {
    wx.vibrateShort({ type: 'light' });
    const idx = e.currentTarget.dataset.index;
    const list = this.data.filteredList;
    const target = list[idx];
    if (!target) return;

    // 移动到头部
    const remaining = this.data.watchlist.filter(x => x.ticker !== target.ticker);
    target.offsetX = 0;
    const updatedWatchlist = [target].concat(remaining);

    this.setData({
      watchlist: updatedWatchlist
    }, () => {
      this.closeAllSwipes();
      this.applyFilter(this.data.currentMarket);
    });

    wx.showToast({
      title: `已置顶 ${target.shortCode}`,
      icon: 'none',
      duration: 1400
    });
  },

  async togglePush(e) {
    const idx = e.currentTarget.dataset.index;
    const list = this.data.filteredList;
    const item = list[idx];
    if (!item) return;

    const prevEnabled = item.pushEnabled !== false;
    const nextEnabled = !prevEnabled;

    // 乐观更新界面状态
    item.pushEnabled = nextEnabled;
    this.setData({ filteredList: list });
    wx.vibrateShort({ type: 'light' });

    try {
      await updatePreference({
        securityId: item.security_id,
        enabled: nextEnabled,
        threshold: 7.0
      });
      wx.showToast({
        title: nextEnabled ? `${item.shortCode || item.ticker} 强提醒已开启` : `${item.shortCode || item.ticker} 提醒已暂停`,
        icon: 'none',
        duration: 1500
      });
    } catch (err) {
      // 失败回滚
      item.pushEnabled = prevEnabled;
      this.setData({ filteredList: list });
      wx.showToast({
        title: '提醒偏好保存失败，已恢复',
        icon: 'none',
        duration: 2000
      });
    }
  },

  openAddModal() {
    clearTimeout(this._sheetCloseTimer);
    this.setData({
      sheetClosing: false,
      showSearchSheet: true,
      searchFocused: true,
      searchQuery: '',
      searchResults: []
    });
  },

  closeSearchModal() {
    if (this.data.sheetClosing) return;
    this.setData({ sheetClosing: true, searchFocused: false });
    this._sheetCloseTimer = setTimeout(() => {
      this.setData({ showSearchSheet: false, sheetClosing: false });
    }, this.data.reduceMotion ? 0 : 200);
  },

  clearSearchQuery() {
    this.setData({
      searchQuery: '',
      searchResults: []
    });
  },

  onSearchInput(e) {
    const val = e.detail.value;
    this.setData({ searchQuery: val });
    if (this.searchTimer) {
      clearTimeout(this.searchTimer);
    }
    if (!val || !val.trim()) {
      this.setData({ searchResults: [] });
      return;
    }
    this.searchTimer = setTimeout(() => {
      this.doSearch(val.trim());
    }, 250);
  },

  searchGen: 0,

  async doSearch(query) {
    const currentGen = ++this.searchGen;
    try {
      const rawList = await searchWatchlist(query);
      if (currentGen !== this.searchGen) {
        // 丢弃过期的乱序响应，防止旧结果覆盖新输入 (F13/T11)
        return;
      }
      const results = (rawList || []).map(item => ({
        ...item,
        formattedPrice: formatPrice(item.last_price, item.market)
      }));
      this.setData({ searchResults: results });
    } catch (err) {
      if (currentGen === this.searchGen) {
        console.error('[Watchlist] search error:', err);
      }
    }
  },

  onUnload() {
    this.searchGen++;
    if (this.searchTimer) {
      clearTimeout(this.searchTimer);
    }
  },

  tapSuggestChip(e) {
    const ticker = e.currentTarget.dataset.ticker;
    if (!ticker) return;
    this.setData({ searchQuery: ticker });
    this.doSearch(ticker);
  },

  onSearchConfirm() {
    if (this.data.searchQuery) {
      this.doSearch(this.data.searchQuery.trim());
    }
  },

  directAddQueryTicker() {
    const q = (this.data.searchQuery || '').trim().toUpperCase();
    if (!q) return;
    this.confirmAddSearchItem({
      currentTarget: {
        dataset: { ticker: q, name: q, watched: false }
      }
    });
  },

  async confirmAddSearchItem(e) {
    const { ticker, watched, name } = e.currentTarget.dataset;
    if (!ticker) return;
    if (watched) {
      wx.showToast({ title: '已在自选监控中', icon: 'none' });
      return;
    }
    wx.vibrateShort({ type: 'light' });
    wx.showLoading({ title: '正在加入...' });
    try {
      const newEntry = await addWatchlist(ticker, name || '');
      wx.hideLoading();
      wx.showToast({ title: '已加入监控', icon: 'success' });

      // 1. 更新搜索列表中该项状态
      const updatedResults = this.data.searchResults.map(it => {
        if (it.ticker === ticker) {
          return { ...it, is_watched: true };
        }
        return it;
      });
      this.setData({ searchResults: updatedResults });

      // 2. 乐观更新当前自选列表，无需等待全局重查即可瞬时呈现
      const market = (newEntry && newEntry.market) || (ticker.includes('.SH') || ticker.includes('.SZ') || ticker.includes('.SS') ? 'CN' : 'US');
      const newItem = {
        security_id: (newEntry && newEntry.security_id) || `SEC-${market}-${ticker}`,
        ticker: (newEntry && newEntry.ticker) || ticker,
        market: market,
        company_name_zh: (newEntry && newEntry.company_name_zh) || name || ticker,
        company_name_en: (newEntry && newEntry.company_name_en) || ticker,
        last_price: newEntry && newEntry.last_price !== undefined ? newEntry.last_price : null,
        change_pct: newEntry && newEntry.change_pct !== undefined ? newEntry.change_pct : null,
        shortCode: shortTicker(ticker),
        formattedPrice: formatPrice(newEntry ? newEntry.last_price : null, market),
        formattedPct: formatPct(newEntry ? newEntry.change_pct : null),
        isUp: isPositive(newEntry ? newEntry.change_pct : null),
        pushEnabled: true,
        offsetX: 0
      };

      const currentWl = this.data.watchlist.filter(x => x.ticker !== ticker);
      currentWl.unshift(newItem);

      // 写入持久缓存
      wx.setStorageSync('cached_watchlist', currentWl);

      // 自动切到“全部”标签，确保用户新增的标的立即可见
      this.setData({
        watchlist: currentWl,
        currentMarket: 'all',
        usCount: currentWl.filter(x => x.market === 'US').length,
        cnCount: currentWl.filter(x => x.market === 'CN').length
      }, () => {
        this.applyFilter('all');
      });

      // 3. 500ms 后自动关闭搜索弹窗，顺畅返回自选主屏
      setTimeout(() => {
        this.closeSearchModal();
      }, 500);

      // 4. 后台静默重刷服务端权威行情
      this.loadData();
    } catch (err) {
      wx.hideLoading();
      wx.showToast({ title: '添加失败，请重试', icon: 'none' });
    }
  },

  navigateToDetail(e) {
    const ticker = e.currentTarget.dataset.ticker;
    if (ticker) {
      const app = getApp();
      if (app && app.globalData) {
        app.globalData.pendingAskQuery = `深度评估 ${ticker} 当前产业链地位与重大事件影响`;
      }
      wx.switchTab({
        url: '/pages/ask/ask'
      });
    }
  },

  stopBubble() {}
});
