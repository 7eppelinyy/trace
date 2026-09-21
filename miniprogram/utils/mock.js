// miniprogram/utils/mock.js - 离线高保真兜底数据

const MOCK_INDICES = [
  { name: '标普 500', code: 'SPX', price: 5618.2, change_pct: 1.23 },
  { name: '纳斯达克', code: 'IXIC', price: 17628.0, change_pct: 1.56 },
  { name: '道琼斯', code: 'DJI', price: 41503.0, change_pct: 0.89 },
  { name: '科创 50', code: 'STAR50', price: 824.1, change_pct: 2.05 }
];

const MOCK_WATCHLIST = [
  {
    security_id: 'SEC-US-NVDA',
    ticker: 'NVDA',
    market: 'US',
    company_name_zh: '英伟达',
    company_name_en: 'NVIDIA Corporation',
    last_price: 213.90,
    prev_close: 212.17,
    change_pct: 0.82,
    note: 'Blackwell 铜互联产线推演'
  },
  {
    security_id: 'SEC-US-MU',
    ticker: 'MU',
    market: 'US',
    company_name_zh: '美光科技',
    company_name_en: 'Micron Technology',
    last_price: 88.50,
    prev_close: 89.53,
    change_pct: -1.15,
    note: '大客户评估二供资质承压'
  },
  {
    security_id: 'SEC-CN-688981.SH',
    ticker: '688981.SH',
    market: 'CN',
    company_name_zh: '中芯国际',
    company_name_en: 'SMIC',
    last_price: 52.80,
    prev_close: 51.54,
    change_pct: 2.45,
    note: '先进封装与国产替代溢出'
  },
  {
    security_id: 'SEC-CN-300750.SZ',
    ticker: '300750.SZ',
    market: 'CN',
    company_name_zh: '宁德时代',
    company_name_en: 'CATL',
    last_price: 245.80,
    prev_close: 241.81,
    change_pct: 1.65,
    note: '全固态电池上游原料测试'
  },
  {
    security_id: 'SEC-US-SNDK',
    ticker: 'SNDK',
    market: 'US',
    company_name_zh: '闪迪',
    company_name_en: 'Sandisk Corporation',
    last_price: 1519.97,
    prev_close: 1530.90,
    change_pct: -0.71,
    note: '次年长单溢价中枢承压'
  }
];

const MOCK_EVENTS = [
  {
    event_id: 'apple-nand',
    title: '苹果评估多元化 NAND 闪存采购来源，拟分散主力供应商份额',
    summary: '大客户针对铠侠展开性能认证，压制次年高端合约毛利，利好亚太外包封测生态。',
    source: '路透社 · 14:02 EST',
    impact_level: '4 级传导',
    securities: [
      { ticker: 'MU', name: '美光', change_pct: -1.15 },
      { ticker: '688981', name: '中芯', change_pct: 2.45 }
    ]
  },
  {
    event_id: 'chip-export',
    title: '高算力芯片外延材料管制条例更新，特种气体审查趋严',
    summary: '先进制程电子化学品审查趋紧，强化本土材料厂商在晶圆产线二供验证逻辑。',
    source: '商务部公告 · 11:30 EST',
    impact_level: '3 级传导',
    securities: [
      { ticker: '688012', name: '安集', change_pct: 4.12 },
      { ticker: 'ASML', name: '阿斯麦', change_pct: -0.80 }
    ]
  },
  {
    event_id: 'tsmc-2nm',
    title: '台积电 2nm 试产良率超预期，量产时间表维持 2025 下半年',
    summary: '极紫外光刻与纳米片晶体管架构成熟度提升，高性能计算客户预订首批产能。',
    source: '行业通报 · 昨日 18:20',
    impact_level: '3 级传导',
    securities: [
      { ticker: 'TSM', name: '台积电', change_pct: 3.20 }
    ]
  }
];

const MOCK_DETAILS = {
  'apple-nand': {
    event_id: 'apple-nand',
    source_label: '路透社独家 · 2026-09-16 14:02 EST',
    title: '苹果据报道评估多元化 NAND 采购来源，分散主力供应商份额',
    summary: '大客户针对铠侠等第二梯队原厂展开规格验证，旨在降低对美系两家主力原厂的依赖。',
    securities: [
      { ticker: 'SNDK', change_pct: -2.40 },
      { ticker: 'MU', change_pct: -1.15 }
    ],
    chain: [
      {
        level: 1,
        title: '官方源：苹果送样认证二供资质',
        desc: '供应链议价权再平衡，次年大客户采购配额面临重新切分'
      },
      {
        level: 2,
        title: '上游：原厂合约毛利承压',
        desc: '美系主力两家原厂高端溢价空间受挤压，毛利率中枢预计下修 120~180bp'
      },
      {
        level: 3,
        title: '中游制造：外包封装与材料验证提速',
        desc: '二供扩大产能利用率，溢出效应强化亚太封测外包伙伴及特种电子化学品需求'
      },
      {
        level: 4,
        title: '资本市场标的映射',
        desc: '美股美光 MU / 闪迪 SNDK 承压；A 股先进封测及模组厂商迎二供替代估值溢价'
      }
    ],
    ask_query: '请深度评估苹果评估铠侠 NAND 闪存对美光科技与闪迪的冲击'
  },
  'chip-export': {
    event_id: 'chip-export',
    source_label: '商务部公告 · 2026-09-16 11:30 EST',
    title: '高算力芯片外延材料管制条例更新，特种气体审查趋严',
    summary: '先进制程电子化学品审查趋紧，强化本土材料厂商在晶圆产线二供验证逻辑。',
    securities: [
      { ticker: '688012', change_pct: 4.12 },
      { ticker: 'ASML', change_pct: -0.80 }
    ],
    chain: [
      {
        level: 1,
        title: '政策端：特种外延与电子气监管清单扩容',
        desc: '关键电子化学品进口审批周期延长至 60~90 个工作日'
      },
      {
        level: 2,
        title: '制造端：晶圆代工厂排产与原料库存警戒线拉升',
        desc: '先进制程晶圆厂特种气体备货周期从 3 个月上提至 6 个月'
      },
      {
        level: 3,
        title: '供应链：本土二供材料厂商准入考核开辟绿色通道',
        desc: '超高纯湿电子化学品、抛光液配方加速国产线验证与良率磨合'
      },
      {
        level: 4,
        title: '资本市场标的映射',
        desc: 'A 股半导体材料龙头（安集科技 688012 / 鼎龙股份）业绩预期上修，海外设备耗材受限'
      }
    ],
    ask_query: '该事件对 A 股半导体封装测试与材料设备板块有何传导映射？'
  },
  'tsmc-2nm': {
    event_id: 'tsmc-2nm',
    source_label: '行业通报 · 2026-09-15 18:20 CST',
    title: '台积电 2nm 试产良率超预期，量产时间表维持 2025 下半年',
    summary: '极紫外光刻与纳米片晶体管架构成熟度提升，高性能计算客户预订首批产能。',
    securities: [
      { ticker: 'TSM', change_pct: 3.20 },
      { ticker: 'NVDA', change_pct: 1.85 }
    ],
    chain: [
      {
        level: 1,
        title: '技术端：GAA 纳米片与背面供电（BSPDN）突破',
        desc: '新架构试产良率跨越 65% 商业化临界点，能效比提升 10%~15%'
      },
      {
        level: 2,
        title: '客户侧：云端 AI 与旗舰 SoC 大客户包揽首批配额',
        desc: '大客户提前 18 个月签订产能预订长约，锁死先进制程资本开支'
      },
      {
        level: 3,
        title: '设备上游：High-NA EUV 光刻机与新型刻蚀需求爆发',
        desc: '光刻机原厂（ASML）与前道沉积设备（应用材料）进入交付加速期'
      },
      {
        level: 4,
        title: '资本市场标的映射',
        desc: '台积电 TSM 领跑先进制程溢价；英伟达 NVDA 下一代架构算力兑现确定性增强'
      }
    ],
    ask_query: '台积电 2nm 良率突破对英伟达下一代 GPU 架构有何催化？'
  }
};

// 为所有预设 mock 数据显式注入 is_mock 属性
Object.keys(MOCK_DETAILS).forEach(key => {
  MOCK_DETAILS[key].is_mock = true;
  MOCK_DETAILS[key].mock_badge = '演示案例 · 仅供交互参考';
});

const MOCK_DETAIL_NAND = MOCK_DETAILS['apple-nand'];

function getMockDetailById(id) {
  if (!id || typeof id !== 'string') return null;
  // 真实事件 ID（如 EVT- 开头）禁止静默回退至 mock 案例
  if (id.startsWith('EVT-')) return null;
  return MOCK_DETAILS[id] || null;
}

module.exports = {
  MOCK_INDICES,
  MOCK_WATCHLIST,
  MOCK_EVENTS,
  MOCK_DETAILS,
  MOCK_DETAIL_NAND,
  getMockDetailById
};

