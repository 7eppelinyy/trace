Component({
  data: {
    selected: 0, reduceMotion: false, keyboardHidden: false, overlayHidden: false,
    tabs: [
      { path: '/pages/index/index', text: '雷达', icon: '/assets/icons/radar.png', active: '/assets/icons/radar-active.png' },
      { path: '/pages/ask/ask', text: '推演', icon: '/assets/icons/ask.png', active: '/assets/icons/ask-active.png' },
      { path: '/pages/watchlist/watchlist', text: '自选', icon: '/assets/icons/watchlist.png', active: '/assets/icons/watchlist-active.png' },
      { path: '/pages/profile/profile', text: '我的', icon: '/assets/icons/profile.png', active: '/assets/icons/profile-active.png' }
    ]
  },
  lifetimes: {
    attached() {
      const pages = getCurrentPages();
      const current = pages[pages.length - 1];
      const selected = current ? this.data.tabs.findIndex(tab => tab.path === '/' + current.route) : 0;
      let reduceMotion = false;
      try { reduceMotion = !!wx.getStorageSync('trace_reduce_motion'); } catch (_) {}
      this.setData({ selected: selected < 0 ? 0 : selected, reduceMotion });
      this._keyboardListener = event => this.setData({ keyboardHidden: event.height > 0 });
      if (typeof wx.onKeyboardHeightChange === 'function') wx.onKeyboardHeightChange(this._keyboardListener);
    },
    detached() {
      if (typeof wx.offKeyboardHeightChange === 'function' && this._keyboardListener) wx.offKeyboardHeightChange(this._keyboardListener);
    }
  },
  methods: {
    switchTab(event) {
      const index = Number(event.currentTarget.dataset.index);
      if (this._switching || index === this.data.selected || !this.data.tabs[index]) return;
      const previous = this.data.selected;
      this._switching = true;
      this.setData({ selected: index });
      wx.switchTab({
        url: this.data.tabs[index].path,
        fail: () => { this.setData({ selected: previous }); wx.showToast({ title: '切换未完成，请重试', icon: 'none' }); },
        complete: () => { this._switching = false; }
      });
    }
  }
});
