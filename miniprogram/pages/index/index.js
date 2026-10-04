// pages/index/index.js - 全网官方源事件雷达
const { getIndices, getEvents } = require('../../utils/api');
const { formatIndexPoint, formatPct, isPositive } = require('../../utils/format');
const { getUiSettings, syncTabBar } = require('../../utils/ui');

Page({
  data: {
    indices: [],
    heroEvent: null,
    events: [],
    loading: false,
    navInset: 64, reduceMotion: false, todayLabel: '', displayEvents: []
  },

  onLoad() {
    const now = new Date();
    this.setData({ ...getUiSettings(), todayLabel: `${now.getMonth() + 1}月${now.getDate()}日` });
    this.loadAllData();
  },
  onShow() { this.setData(getUiSettings()); syncTabBar(this, 0); },

  onPullDownRefresh() {
    this.loadAllData(() => {
      wx.stopPullDownRefresh();
    });
  },

  async loadAllData(callback) {
    this.setData({ loading: true });
    try {
      const [rawIndices, rawEvents] = await Promise.all([
        getIndices(),
        getEvents(6)
      ]);

      const formattedIndices = (rawIndices || []).map(item => {
        const hasPrice = item.price !== null && item.price !== undefined && !isNaN(item.price);
        const hasPct = item.change_pct !== null && item.change_pct !== undefined && !isNaN(item.change_pct);
        let statusBadge = '';
        if (item.status === 'stale') statusBadge = '已过期';
        else if (item.status === 'delayed') statusBadge = '延时';
        else if (item.status === 'unknown_timestamp' || item.status === 'invalid_timestamp') statusBadge = '时间未核验';
        else if (item.status === 'cached') statusBadge = '缓存';
        else if (item.status === 'unavailable' || !hasPrice) statusBadge = '暂无';
        else if (item.status === 'mock') statusBadge = '演示';

        return {
          name: item.name,
          code: item.code,
          status: item.status || 'real',
          statusBadge: statusBadge,
          hasQuote: hasPrice && hasPct,
          formattedPrice: formatIndexPoint(item.price),
          formattedPct: hasPct ? formatPct(item.change_pct) : '--',
          isUp: hasPct ? isPositive(item.change_pct) : false
        };
      });

      const heroEvent = (rawEvents && rawEvents.length > 0) ? rawEvents[0] : null;
      const remainingEvents = (rawEvents && rawEvents.length > 1) ? rawEvents.slice(1) : (rawEvents || []);

      this.setData({
        indices: formattedIndices,
        heroEvent: heroEvent,
        events: remainingEvents,
        displayEvents: (rawEvents || []).map(item => {
          const seen = item.first_seen_at ? new Date(item.first_seen_at) : null;
          const valid = seen && !isNaN(seen.getTime());
          return { ...item, collectedTime: valid ? `${seen.getMonth() + 1}/${seen.getDate()} ${String(seen.getHours()).padStart(2,'0')}:${String(seen.getMinutes()).padStart(2,'0')}` : '' };
        })
      });
    } catch (e) {
      console.error('[Index] load data error:', e);
    } finally {
      this.setData({ loading: false });
      if (typeof callback === 'function') callback();
    }
  },

  onManualRefresh() {
    wx.vibrateShort({ type: 'light' });
    wx.showLoading({ title: '同步最新事件中...', mask: true });
    this.loadAllData(() => {
      wx.hideLoading();
      const now = new Date();
      const timeStr = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;
      wx.showToast({
        title: `已更新至 ${timeStr}`,
        icon: 'none',
        duration: 1500
      });
    });
  },

  navigateToDetail(e) {
    const id = e.currentTarget.dataset.id;
    if (!id) return;
    wx.navigateTo({
      url: `/pages/detail/detail?id=${id}`
    });
  },

  onShareAppMessage() {
    return {
      title: 'Trace — 美股+A股重大事件智能雷达与产业链推演',
      path: '/pages/index/index'
    };
  }
});
