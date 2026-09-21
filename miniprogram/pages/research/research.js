const {request, getUserId} = require('../../utils/api');

Page({
  data: {items: [], loading: false, saving: false, error: '', selected: null, shared: false,
    title: '', hypothesis: '', supporting: '', contradicting: '', userNotes: '', nextDate: '', sharePath: '',
    total: 0, hasMore: false, loadingMore: false},
  generation: 0,
  async onLoad(options = {}) {
    if (options.shareId && options.token) {
      this.setData({shared: true, loading: true});
      try {
        const item = await request({url: `/research/questions/${encodeURIComponent(options.shareId)}/share?token=${encodeURIComponent(options.token)}`, public: true});
        this.edit(item);
      } catch (_) { this.setData({error: '分享已过期、撤销或不存在'}); }
      finally { this.setData({loading: false}); }
      return;
    }
    if (options.eventId) {
      try {
        const draft = await request({url: '/research/questions/draft', method: 'POST', data: {event_id: options.eventId}});
        this.edit(draft);
      } catch (_) { this.setData({error: '无法读取事件，请稍后重试'}); }
    }
  },
  onShow() { if (!this.data.shared) this.load(); },
  onUnload() { this.generation += 1; },
  onReachBottom() {
    if (this.data.hasMore && !this.data.loadingMore && !this.data.loading && !this.data.shared) {
      this.loadMore();
    }
  },
  async load() {
    const generation = ++this.generation;
    this.setData({loading: true, error: ''});
    try {
      const response = await request({url: '/research/questions?limit=50&offset=0'});
      if (generation !== this.generation) return;
      const states = {tracking: '跟踪中', confirmed: '用户已验证', falsified: '用户已证伪', archived: '已归档'};
      const mapped = (response.items || []).map(item => ({...item, stateLabel: states[item.state] || item.state,
        evidenceCount: (item.matched_evidence_ids || []).length,
        due: item.state === 'tracking' && item.next_check_at && Date.parse(item.next_check_at) <= Date.now()}));
      this.setData({
        items: mapped,
        total: response.total !== undefined ? response.total : mapped.length,
        hasMore: !!response.has_more,
      });
    } catch (_) { if (generation === this.generation) this.setData({error: '加载失败。旧内容已保留，请重试。'}); }
    finally { if (generation === this.generation) this.setData({loading: false}); }
  },
  async loadMore() {
    if (!this.data.hasMore || this.data.loadingMore) return;
    const generation = this.generation;
    this.setData({loadingMore: true});
    try {
      const offset = this.data.items.length;
      const response = await request({url: `/research/questions?limit=50&offset=${offset}`});
      if (generation !== this.generation) return;
      const states = {tracking: '跟踪中', confirmed: '用户已验证', falsified: '用户已证伪', archived: '已归档'};
      const moreMapped = (response.items || []).map(item => ({...item, stateLabel: states[item.state] || item.state,
        evidenceCount: (item.matched_evidence_ids || []).length,
        due: item.state === 'tracking' && item.next_check_at && Date.parse(item.next_check_at) <= Date.now()}));
      this.setData({
        items: this.data.items.concat(moreMapped),
        total: response.total !== undefined ? response.total : (this.data.items.length + moreMapped.length),
        hasMore: !!response.has_more,
      });
    } catch (_) {
      this.setData({error: '加载更多失败，请重试'});
    } finally {
      this.setData({loadingMore: false});
    }
  },
  select(e) { const item = this.data.items.find(i => i.question_id === e.currentTarget.dataset.id); if (item) this.edit(item); },
  newQuestion() { this.edit({}); },
  edit(item) {
    this.setData({selected: item, title: item.title || '', hypothesis: item.hypothesis || '',
      supporting: (item.supporting_conditions || []).join('\n'), contradicting: (item.contradicting_conditions || []).join('\n'),
      userNotes: item.user_notes || '', nextDate: item.next_check_at ? item.next_check_at.slice(0,10) : '', sharePath: ''});
  },
  input(e) {
    const field = e.currentTarget.dataset.field;
    if (['title','hypothesis','supporting','contradicting','userNotes'].includes(field)) this.setData({[field]: e.detail.value});
  },
  date(e) { this.setData({nextDate: e.detail.value}); },
  async save(e) {
    if (this.data.shared || this.data.saving) return;
    const selected = this.data.selected || {};
    const state = e && e.currentTarget && e.currentTarget.dataset.state;
    if (!this.data.title.trim() || !this.data.hypothesis.trim()) {
      this.setData({error: '请填写标题和待验证假设'}); return;
    }
    const data = {title: this.data.title.trim(), hypothesis: this.data.hypothesis.trim(),
      supporting_conditions: this.data.supporting.split('\n').filter(Boolean),
      contradicting_conditions: this.data.contradicting.split('\n').filter(Boolean), user_notes: this.data.userNotes,
      next_check_at: this.data.nextDate ? this.data.nextDate + 'T00:00:00Z' : null};
    if (selected.question_id) { data.expected_revision = selected.revision; if (state) data.state = state; }
    else { data.event_id = selected.event_id || null; data.security_id = selected.security_id || null; }
    this.setData({saving: true, error: ''});
    try {
      const saved = await request({url: '/research/questions' + (selected.question_id ? '/' + selected.question_id : ''),
        method: selected.question_id ? 'PATCH' : 'POST', data});
      this.edit(saved); await this.load();
      wx.showToast({title: '已保存', icon: 'success'});
    } catch (err) {
      this.setData({error: err.statusCode === 409 ? '记录有新变化，请重新打开后复核；当前输入已保留。' : '保存失败，当前输入已保留。'});
    } finally { this.setData({saving: false}); }
  },
  openEvidence(e) {
    if (this.data.selected && this.data.selected.event_id) wx.navigateTo({url: '/pages/detail/detail?id=' + encodeURIComponent(this.data.selected.event_id)});
  },
  async exportAll() {
    try {
      const data = await request({url: '/research/export', timeout: 60000});
      const path = wx.env.USER_DATA_PATH + '/trace-research.json';
      wx.getFileSystemManager().writeFileSync(path, JSON.stringify(data,null,2), 'utf8');
      if (wx.shareFileMessage) wx.shareFileMessage({filePath: path, fileName: 'trace-research.json'});
      else wx.showToast({title: '文件已保存到小程序本地目录', icon: 'none'});
    } catch (_) { this.setData({error: '导出失败，请稍后重试'}); }
  },
  async enableShare() {
    if (!this.data.selected || !this.data.selected.question_id || this.data.shared) return;
    const id = this.data.selected.question_id;
    try {
      const share = await request({url: `/research/questions/${id}/share`, method: 'POST', data: {expires_in_days: 7}});
      this.setData({sharePath: `/pages/research/research?shareId=${encodeURIComponent(id)}&token=${encodeURIComponent(share.share_token)}`});
    } catch (_) { this.setData({error: '分享创建失败'}); }
  },
  async revokeShare() {
    try { await request({url: `/research/questions/${this.data.selected.question_id}/share`, method: 'DELETE'});
      this.setData({sharePath: ''}); wx.showToast({title: '该记录分享已撤销'});
    } catch (_) { this.setData({error: '撤销失败，请重试'}); }
  },
  onShareAppMessage() { return this.data.sharePath ? {title: this.data.title, path: this.data.sharePath} : {title: 'Trace', path: '/pages/index/index'}; }
});
