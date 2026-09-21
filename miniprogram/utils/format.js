// miniprogram/utils/format.js - 数据与格式排印规范

/**
 * 格式化股票与指数报价
 */
function formatPrice(price, market = 'US') {
  if (price === null || price === undefined || isNaN(price)) return '--';
  const num = Number(price);
  const currency = market === 'CN' ? '¥' : '$';
  return `${currency}${num.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/**
 * 格式化大盘指数点位
 */
function formatIndexPoint(price) {
  if (price === null || price === undefined || isNaN(price)) return '--';
  const num = Number(price);
  return num.toLocaleString('en-US', { minimumFractionDigits: 1, maximumFractionDigits: 2 });
}

/**
 * 格式化涨跌百分比 (带正负号)
 */
function formatPct(pct) {
  if (pct === null || pct === undefined || isNaN(pct)) return '--';
  const num = Number(pct);
  const sign = num > 0 ? '+' : '';
  return `${sign}${num.toFixed(2)}%`;
}

/**
 * 判断是否为上涨/平盘
 */
function isPositive(pct) {
  return Number(pct || 0) >= 0;
}

/**
 * 标的代码极简展示（如 688981.SH -> 688981）
 */
function shortTicker(ticker) {
  if (!ticker) return '';
  return ticker.includes('.') ? ticker.split('.')[0] : ticker;
}

module.exports = {
  formatPrice,
  formatIndexPoint,
  formatPct,
  isPositive,
  shortTicker
};
