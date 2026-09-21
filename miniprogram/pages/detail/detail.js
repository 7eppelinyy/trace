// pages/detail/detail.js - 产业链全景推演与证据工作台
const { getEventDetail } = require('../../utils/api');
const { getMockDetailById } = require('../../utils/mock');
const app = getApp();

let loadGenerationCounter = 0;

Page({
  data: {
    eventId: '',
    event: null,
    loading: false,
    loadError: false,
    errorMessage: '',
    isDemo: false
  },

  async onLoad(options) {
    const id = (options && options.id) || '';
    const from = (options && options.from) || '';
    const currentGen = ++loadGenerationCounter;

    this.setData({ eventId: id, loading: true, loadError: false, errorMessage: '' });

    wx.setNavigationBarTitle({
      title: from === 'ask' ? '动态产业链推演图谱' : '事件证据与推演工作台'
    });

    let ev = null;

    // 1) 若来自推演对话页且存在动态生成的 customGraph
    if (from === 'ask' && app && app.globalData && app.globalData.customGraph) {
      const custom = app.globalData.customGraph;
      if (custom.chain && custom.chain.length > 0) {
        ev = {
          id: custom.id,
          version: 1,
          statusLabel: '情景推演',
          status: 'scenario',
          title: custom.title,
          summary: custom.summary,
          source_label: custom.source_label || 'Trace 产业链推演',
          securities: custom.ticker ? [{
            ticker: custom.ticker,
            name: custom.ticker,
            change_pct: null,
            direction: 'neutral',
            directionLabel: '研判标的'
          }] : [],
          chain: custom.chain,
          evidences: [],
          claims: [],
          uncertainties: ['推演结论基于通用产业链图谱与第一性原理假设，尚待真实事件公告证实'],
          next_checks: ['跟踪相关上市公司官方披露与定期财报指引'],
          revisions: [],
          ask_query: custom.ask_query || '继续追问细节',
          is_mock: false
        };
      }
    }

    // 2) 若未命中 customGraph 且为后端真实事件 ID (以 EVT- 开头)，从真实 API 加载
    if (!ev && id.startsWith('EVT-')) {
      try {
        const realData = await getEventDetail(id);
        if (currentGen !== loadGenerationCounter) {
          // 请求序号过期，放弃旧响应
          return;
        }
        if (realData) {
          ev = this.transformRealEvent(realData);
        } else {
          this.setData({
            loadError: true,
            errorMessage: '未检索到该事件详情或该事件已被归档',
            loading: false,
            event: null
          });
          return;
        }
      } catch (err) {
        if (currentGen !== loadGenerationCounter) return;
        console.warn('Load real event detail failed:', err);
        this.setData({
          loadError: true,
          errorMessage: '网络服务通信异常，无法获取事件推演图谱',
          loading: false,
          event: null
        });
        return;
      }
    }

    // 3) 若未命中真实事件且为已知预设 Demo ID
    if (!ev && id && !id.startsWith('EVT-')) {
      ev = getMockDetailById(id);
    }

    if (currentGen !== loadGenerationCounter) {
      return;
    }

    if (!ev) {
      this.setData({
        loadError: true,
        errorMessage: id ? `未检索到有效事件: ${id}` : '未指定有效事件 ID',
        loading: false,
        event: null
      });
      return;
    }

    this.setData({
      event: ev,
      isDemo: !!ev.is_mock,
      loading: false,
      loadError: false
    });
  },

  retryLoad() {
    this.onLoad({ id: this.data.eventId });
  },

  transformRealEvent(data) {
    const evidence = (data.evidences && data.evidences[0]) || {};
    const srcId = evidence.source_id || data.primary_source_id || data.first_source_id || '';
    const isOfficial = srcId && (
      srcId.includes('ir') ||
      srcId.includes('cninfo') ||
      srcId.includes('sec') ||
      srcId.includes('mofcom') ||
      srcId.includes('fed') ||
      srcId.includes('commerce') ||
      srcId.includes('miit')
    );
    const sourceLabel = isOfficial
      ? `${srcId || '官方信源'} · 官方披露`
      : `${srcId || '信源'} · 权威报道`;

    const statusMap = {
      official_confirmed: '官方披露证实',
      confirmed: '官方证实',
      partially_confirmed: '部分交叉证实',
      reported: '媒体报道/待验证',
      rumor: '市场传闻',
      superseded: '已撤回/被更正'
    };
    const statusLabel = statusMap[data.status] || (data.status === 'confirmed' ? '已核验' : '待证实');

    const impacts = data.impacts || [];
    const directImpacts = impacts.filter(i => i.directness === 'direct');
    const indirectImpacts = impacts.filter(i => i.directness === 'indirect' || i.directness === 'conditional');
    const topImpact = impacts[0] || {};

    // 映射标的（优先取后端返回的真实行情 chips）
    let securities = [];
    if (data.securities && data.securities.length > 0) {
      securities = data.securities.map(s => {
        const matchingImp = impacts.find(imp => (imp.security_id || '').includes(s.ticker));
        const dir = matchingImp ? matchingImp.direction : 'neutral';
        return {
          ticker: s.ticker,
          name: s.name || s.ticker,
          change_pct: s.change_pct,
          direction: dir,
          directionLabel: dir === 'bullish' ? '利好' : (dir === 'bearish' ? '利空' : '中性')
        };
      });
    } else {
      securities = impacts.slice(0, 4).map(imp => {
        const ticker = (imp.security_id || '').replace(/^SEC-[A-Z]+-/, '');
        const hasQuote = imp.change_pct !== undefined && imp.change_pct !== null && !isNaN(imp.change_pct);
        return {
          ticker: ticker,
          name: ticker,
          change_pct: hasQuote ? imp.change_pct : null,
          direction: imp.direction || 'neutral',
          directionLabel: imp.direction === 'bullish' ? '利好' : (imp.direction === 'bearish' ? '利空' : '中性')
        };
      });
    }

    // 动态构建传导图谱 (真实拓扑深度，不强凑 4 级)
    const chain = [];
    // Level 1: 事实诱因与信源
    chain.push({
      level: 1,
      title: `${statusLabel} · 事实诱因`,
      desc: `《${data.title}》。来源：${srcId || '权威披露'}，状态：${statusLabel}。`
    });

    // Level 2: 一级直接冲击
    const l2Desc = directImpacts.length > 0
      ? directImpacts.map(d => `${(d.security_id || '').replace(/^SEC-[A-Z]+-/, '')}: ${d.reason || '直接业务影响'}`).join('；')
      : (topImpact.reason || '标的在供应链核心节点承接直接供需或订单变动。');
    chain.push({
      level: 2,
      title: '核心标的一级直接冲击',
      desc: l2Desc
    });

    // Level 3: 若真实存在间接/条件性关联，展开一级拓扑外溢
    if (indirectImpacts.length > 0) {
      chain.push({
        level: 3,
        title: '产业链一级拓扑协同与外溢',
        desc: indirectImpacts.map(d => `${(d.security_id || '').replace(/^SEC-[A-Z]+-/, '')}: ${d.reason || '间接外溢影响'}`).join('；')
      });
    }

    // Level 4: 若传导深度达到 3 (存在 2-hop 传导链)
    if (data.transmission_depth >= 3) {
      const scoreVal = topImpact.final_score;
      const scoreStr = (scoreVal !== null && scoreVal !== undefined && !isNaN(scoreVal))
        ? `综合影响评分 ${scoreVal.toFixed(1)} 分。`
        : '';
      chain.push({
        level: 4,
        title: '二级扩散与替代传导',
        desc: `${scoreStr}上游材料设备与下游方案商排产分化，资本市场启动中长期多空定价与估值重构。`
      });
    }

    // 事实 Claims 格式化
    const claims = (data.claims || []).map(c => ({
      id: c.claim_id,
      kind: c.kind,
      kindLabel: c.kind === 'fact' ? '已核验事实' : (c.kind === 'inference' ? '产业链推断' : '情景假设'),
      text: c.text,
      assumptions: c.assumptions || [],
      counterEvidence: c.counter_evidence || []
    }));

    // 原文证据链接
    const evidences = (data.evidences || []).map(e => ({
      raw_item_id: e.raw_item_id,
      source_id: e.source_id,
      title: e.title || '官方原件',
      url: e.url || '',
      published_at: e.published_at ? e.published_at.slice(0, 16).replace('T', ' ') : '',
      role: e.role === 'primary' ? '核心信源' : (e.role === 'confirming' ? '确认信源' : '佐证信源')
    }));

    // 修订时间线
    const revisions = (data.revisions || []).map(r => ({
      version: r.version,
      type: r.revision_type || 'update',
      note: r.note || '信息更新',
      created_at: r.created_at ? r.created_at.slice(0, 16).replace('T', ' ') : ''
    }));

    return {
      id: data.event_id,
      version: data.version,
      status: data.status,
      statusLabel: statusLabel,
      title: data.title,
      summary: data.summary,
      source_label: sourceLabel,
      securities: securities,
      chain: chain,
      claims: claims,
      uncertainties: data.uncertainties || [],
      next_checks: data.next_checks || [],
      evidences: evidences,
      revisions: revisions,
      is_mock: false,
      ask_query: `深度评估“${data.title}”对核心标的之传导与反证条件`
    };
  },

  copyEvidenceUrl(e) {
    const url = e.currentTarget.dataset.url;
    if (!url) return;
    wx.setClipboardData({
      data: url,
      success() {
        wx.showToast({
          title: '已复制原文链接',
          icon: 'success',
          duration: 1500
        });
      }
    });
  },

  trackResearch() {
    if (this.data.event && !this.data.event.is_mock) wx.navigateTo({url: '/pages/research/research?eventId=' + encodeURIComponent(this.data.event.id)});
  },

  askAboutEvent() {
    wx.vibrateShort({ type: 'light' });
    const ev = this.data.event;
    const query = (ev && ev.ask_query) || '评估该事件对核心标的之影响';
    if (app && app.globalData) {
      app.globalData.pendingAskQuery = query;
      app.globalData.pendingAskEventId = ev ? ev.id : null;
      app.globalData.pendingAskEventVersion = ev ? ev.version : null;
    }
    wx.switchTab({
      url: '/pages/ask/ask'
    });
  },

  onShareAppMessage() {
    const ev = this.data.event;
    return {
      title: ev ? `【Trace 证据】${ev.title}` : 'Trace 重大事件智能雷达',
      path: `/pages/detail/detail?id=${this.data.eventId}`
    };
  },

  onShareTimeline() {
    const ev = this.data.event;
    return {
      title: ev ? `【产业链推演】${ev.title}` : 'Trace 重大事件智能雷达',
      query: `id=${this.data.eventId}`
    };
  }
});

