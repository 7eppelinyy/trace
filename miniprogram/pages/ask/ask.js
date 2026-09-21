// pages/ask/ask.js - 产业链 AI 深度推演与防破甲对话
const { askQuestion } = require('../../utils/api');

Page({
  data: {
    inputValue: '',
    thinking: false,
    scrollTarget: '',
    messages: [],
    pendingEventId: null,
    pendingEventVersion: null
  },

  onShow() {
    const app = getApp();
    if (app && app.globalData && app.globalData.pendingAskQuery) {
      const q = app.globalData.pendingAskQuery;
      const eventId = app.globalData.pendingAskEventId || null;
      const eventVersion = app.globalData.pendingAskEventVersion || null;
      app.globalData.pendingAskQuery = null;
      app.globalData.pendingAskEventId = null;
      app.globalData.pendingAskEventVersion = null;
      this.setData({
        inputValue: q,
        pendingEventId: eventId,
        pendingEventVersion: eventVersion
      }, () => {
        this.submitAsk();
      });
    }
  },

  onInput(e) {
    this.setData({ inputValue: e.detail.value });
  },

  askPreset(e) {
    if (this.data.thinking) return;
    const q = e.currentTarget.dataset.query;
    this.setData({ inputValue: q }, () => {
      this.submitAsk();
    });
  },

  detectTicker(query) {
    if (!query) return '';
    // A 股标准代码匹配 (600519.SH / 000001.SZ)
    const codeMatch = query.match(/\b(\d{6}\.(SH|SZ|BJ|SS))\b/i);
    if (codeMatch) return codeMatch[1].toUpperCase().replace('.SS', '.SH');

    // 纯 6 位数字代码
    const numMatch = query.match(/\b(\d{6})\b/);
    if (numMatch) {
      const c = numMatch[1];
      if (c.startsWith('60') || c.startsWith('68')) return `${c}.SH`;
      if (c.startsWith('00') || c.startsWith('30')) return `${c}.SZ`;
      if (c.startsWith('8') || c.startsWith('4') || c.startsWith('9')) return `${c}.BJ`;
    }

    // 常用龙头与高频标的
    if (/茅台/i.test(query)) return '600519.SH';
    if (/比亚迪|BYD/i.test(query)) return '002594.SZ';
    if (/特斯拉|TSLA/i.test(query)) return 'TSLA';
    if (/寒武纪/i.test(query)) return '688256.SH';
    if (/MU|美光/i.test(query)) return 'MU';
    if (/SNDK|闪迪/i.test(query)) return 'SNDK';
    if (/NVDA|英伟达/i.test(query)) return 'NVDA';
    if (/688981|中芯/i.test(query)) return '688981.SH';
    if (/300750|宁德/i.test(query)) return '300750.SZ';
    if (/TSM|台积电/i.test(query)) return 'TSM';
    if (/ASML/i.test(query)) return 'ASML';
    if (/AAPL|苹果/i.test(query)) return 'AAPL';
    if (/立讯精密/i.test(query)) return '002475.SZ';
    if (/沃尔核材/i.test(query)) return '002130.SZ';
    if (/浪潮信息/i.test(query)) return '000977.SZ';
    if (/工业富联/i.test(query)) return '601138.SH';
    if (/中际旭创/i.test(query)) return '300308.SZ';
    if (/新易盛/i.test(query)) return '300502.SZ';

    return '';
  },

  async submitAsk() {
    // 防重复提交与并发保护 (F24)
    if (this.data.thinking) return;

    const q = (this.data.inputValue || '').trim();
    if (!q) return;

    const eventId = this.data.pendingEventId;
    const eventVersion = this.data.pendingEventVersion;

    const userMsgId = Date.now();
    const newMsgList = this.data.messages.concat({
      id: userMsgId,
      role: 'user',
      content: q
    });

    this.setData({
      messages: newMsgList,
      inputValue: '',
      pendingEventId: null,
      pendingEventVersion: null,
      thinking: true,
      scrollTarget: 'msg-thinking'
    });

    const ticker = this.detectTicker(q);
    const history = this.data.messages
      .slice(-6)
      .map(m => ({ role: m.role, content: m.content }));

    const t0 = Date.now();

    try {
      const askOpts = {
        event_id: eventId,
        event_version: eventVersion,
        mode: 'evidence_answer'
      };
      const res = await askQuestion(ticker, q, history, askOpts);
      const duration = (res && res.duration_ms) || (Date.now() - t0);

      const status = (res && res.status) || 'ok';
      const isBlocked = status === 'jailbreak_blocked';
      const isOutOfDomain = status === 'out_of_domain';
      const isDegraded = status === 'degraded';
      const isInsufficientEvidence = status === 'insufficient_evidence';
      const isScenario = status === 'scenario_simulation';
      const isBudgetExhausted = status === 'budget_exhausted';
      const isError = status === 'error';

      const evidenceEvents = (res && res.evidence_events) || [];
      const hasEvidence = evidenceEvents.length > 0;

      let title = 'Trace 产业链深度研判';
      let metaBadge = hasEvidence
        ? `Trace 研判 · 耗时 ${duration}ms · 关联 ${evidenceEvents.length} 条事实证据`
        : `Trace 产业链拓扑研判 · 耗时 ${duration}ms`;
      let sourceLabel = hasEvidence ? '官方证据链与图谱拓扑' : '产业链拓扑推演';

      if (isBlocked) {
        title = '安全防御拦截';
        metaBadge = 'Trace 护栏 · 安全审计拦截';
        sourceLabel = '安全隔离保护';
      } else if (isOutOfDomain) {
        title = '金融领域边界引导';
        metaBadge = 'Trace 护栏 · 超出金融领域';
        sourceLabel = '领域边界保护';
      } else if (isBudgetExhausted) {
        title = '推演额度提醒';
        metaBadge = '成本保护 · 当日模型调用额度耗尽';
        sourceLabel = '离线规则推演';
      } else if (isScenario) {
        title = 'Trace 产业链情景推演';
        metaBadge = `情景模拟 · 基于拓扑第一性原理假设 · 耗时 ${duration}ms`;
        sourceLabel = '情景假设模拟（非既成事实）';
      } else if (isDegraded) {
        title = 'Trace 规则模板研判（降级模式）';
        metaBadge = '降级推演 · 大模型通道异常，基于本地知识库研判';
        sourceLabel = '本地规则库与拓扑';
      } else if (isInsufficientEvidence) {
        title = '本地事实证据不足';
        metaBadge = '证据约束 · 本地事件库暂无已核验突发事实证据';
        sourceLabel = '证据核验拒绝虚构';
      } else if (status === 'invalid_model_output') {
        title = '模型输出未通过事实核验';
        metaBadge = '校验拦截 · 模型输出不符合原文硬约束，已转为规则兜底';
        sourceLabel = '本地规则兜底';
      } else if (isError) {
        title = '推演服务通信异常';
        metaBadge = '服务异常 · 请检查网络与后端配置';
        sourceLabel = '诊断模式';
      }

      const answerContent = (res && (res.answer || res.text || res.content)) || '';
      let finalContent = answerContent.trim();
      let finalStatus = status;

      if (!finalContent) {
        finalStatus = 'insufficient_evidence';
        title = '暂无有效研判结果';
        metaBadge = '证据提示 · 未返回有效研判内容';
        finalContent = '推演引擎未检索到相关事实证据或未能生成有效推演结论，请尝试补充具体标的代码或更换提问角度。';
      }

      const aiMsgList = this.data.messages.concat({
        id: Date.now(),
        role: 'assistant',
        status: finalStatus,
        ticker: (res && res.ticker) || ticker || '宏观/产业图谱',
        duration: duration,
        title: title,
        metaBadge: metaBadge,
        sourceLabel: sourceLabel,
        content: finalContent,
        isSecurity: isBlocked || isOutOfDomain,
        isDegraded: isDegraded || isInsufficientEvidence || isBudgetExhausted || status === 'invalid_model_output' || finalStatus === 'insufficient_evidence',
        evidenceEvents: evidenceEvents,
        graphChain: (res && res.graph_chain) || []
      });

      this.setData({
        messages: aiMsgList,
        thinking: false,
        scrollTarget: 'scroll-bottom'
      });
    } catch (e) {
      const errItem = {
        id: Date.now(),
        role: 'assistant',
        status: 'network_error',
        ticker: ticker || '系统连接',
        title: '推演请求未完成',
        metaBadge: '网络或服务异常 · 可点击重试',
        sourceLabel: 'Trace 通道',
        content: '推演服务连接超时或网络异常，未消耗 AI 推演预算。请检查网络后点击下方重试。',
        isSecurity: false,
        isDegraded: true,
        isError: true,
        canRetry: true,
        retryQuery: q
      };
      this.setData({
        messages: this.data.messages.concat(errItem),
        thinking: false,
        scrollTarget: 'scroll-bottom'
      });
      wx.showToast({ title: '推演请求未完成', icon: 'none' });
    }
  },

  retryAsk(e) {
    if (this.data.thinking) return;
    const q = e.currentTarget.dataset.query;
    if (q) {
      this.setData({ inputValue: q }, () => {
        this.submitAsk();
      });
    }
  },

  clearChat() {
    this.setData({
      messages: []
    });
    wx.showToast({ title: '已清空推演上下文', icon: 'none' });
  },

  navigateToDetail(e) {
    const idx = e.currentTarget.dataset.index;
    const item = (idx !== undefined && this.data.messages[idx]) ? this.data.messages[idx] : null;
    const app = getApp();

    if (item && !item.isSecurity) {
      const evidenceId = (item.evidenceEvents && item.evidenceEvents.length > 0) ? item.evidenceEvents[0] : null;
      if (app && app.globalData) {
        app.globalData.customGraph = {
          id: evidenceId || `ask-${item.id}`,
          title: item.title || `${item.ticker} 产业链深度研判`,
          summary: (item.content || '').slice(0, 140) + ((item.content || '').length > 140 ? '...' : ''),
          source_label: 'Trace 产业链推演',
          ticker: item.ticker,
          chain: item.graphChain && item.graphChain.length > 0 ? item.graphChain : null,
          ask_query: `继续追问关于 ${item.ticker} 的产业链推演细节`
        };
      }
      const targetId = evidenceId || `ask-${item.id}`;
      wx.navigateTo({
        url: `/pages/detail/detail?from=ask&id=${targetId}`
      });
      return;
    }

    wx.showToast({
      title: '暂无可下钻的图谱路径',
      icon: 'none'
    });
  }
});
