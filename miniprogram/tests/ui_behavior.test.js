const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const path = require('path');
const { presentAnswer } = require('../utils/answer');

function loadPage(name, api = {}) {
  let page;
  const intervals = new Set();
  let tick = 0;
  const app = { globalData: { statusBarHeight: 44, navBarHeight: 44 } };
  const wx = { getStorageSync: () => false, getSystemInfoSync: () => ({ windowWidth: 375 }),
    showToast: () => {}, vibrateShort: () => {}, setClipboardData: () => {}, getAccountInfoSync: () => null };
  const context = { Page: value => page = value, getApp: () => app, wx, console,
    Date, setTimeout, clearTimeout,
    setInterval: () => { const id = ++tick; intervals.add(id); return id; },
    clearInterval: id => intervals.delete(id),
    require: modulePath => {
      if (modulePath.includes('utils/api')) return api;
      if (modulePath.includes('utils/')) return require(path.join(__dirname, '..', 'utils', path.basename(modulePath)));
      throw new Error('Unexpected module: ' + modulePath);
    }
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'pages', name, name + '.js'), 'utf8'), context);
  page.data = JSON.parse(JSON.stringify(page.data));
  page.setData = function(values, done) {
    Object.entries(values).forEach(([key, value]) => {
      const match = key.match(/^filteredList\[(\d+)\]\.offsetX$/);
      if (match) this.data.filteredList[Number(match[1])].offsetX = value;
      else this.data[key] = value;
    });
    if (done) done();
  };
  return { page, intervals };
}

(async () => {
  const presented = presentAnswer({ answer: '正文\n\n待核验的问题：\n收入结构\n\n公司资料来源：\n链接', claims: [
    {kind: 'inference', text: '条件推断', assumptions: ['条件一', '条件二'], quote: '逐字证据', counter_evidence: ['反证']}
  ], citations: ['https://www.sec.gov/a', 'https://www.sec.gov/a', 'javascript:alert(1)'] });
  assert.deepStrictEqual(presented.answerClaims[0].conditions, ['条件一', '条件二']);
  assert.strictEqual(presented.answerClaims[0].supportQuote, '逐字证据');
  assert.strictEqual(presented.answerClaims[0].counterEvidence[0], '反证');
  assert.strictEqual(presented.answerSources.length, 1);
  assert.strictEqual(presented.answerSources[0].label, 'sec.gov');
  assert.ok(presented.nextChecksText.includes('收入结构'));
  assert.ok(!presented.nextChecksText.includes('公司资料来源'));
  assert.strictEqual(presentAnswer({answer: '发生错误'}).answerClaims.length, 0);

  let resolveAnswer;
  let receivedOptions;
  const {page: ask, intervals} = loadPage('ask', {askQuestion: (_, __, ___, options) => { receivedOptions = options; return new Promise(resolve => resolveAnswer = resolve); }});
  ask.data.inputValue = '美光与闪迪有什么区别？';
  ask.data.pendingEventId = 'EVT-pinned';
  ask.data.pendingEventVersion = 4;
  const waiting = ask.submitAsk();
  assert.strictEqual(intervals.size, 1);
  const messageCount = ask.data.messages.length;
  ask.clearChat();
  assert.strictEqual(ask.data.messages.length, messageCount, 'Pending question must not silently vanish');
  ask._followAnswer = false;
  resolveAnswer({status: 'ok', mode: 'background', answer: '比较结果', claims: [{kind: 'background', text: '比较结果'}], citations: []});
  await waiting;
  assert.strictEqual(intervals.size, 0, 'Completion must clear elapsed timer');
  assert.strictEqual(receivedOptions.event_id, 'EVT-pinned');
  assert.strictEqual(ask.data.messages.at(-1).title, '业务与原理');
  assert.strictEqual(ask.data.messages.at(-1).answerClaims[0].label, '背景解释');
  assert.strictEqual(ask.data.scrollTarget, '', 'Reading earlier content must not be interrupted');
  assert.strictEqual(ask.data.showNewAnswerHint, true);
  ask.jumpToLatest();
  assert.strictEqual(ask.data.showNewAnswerHint, false);
  ask.onKeyboardChange({detail:{height:300}});
  assert.strictEqual(ask.data.keyboardVisible,true);
  ask.onHide();
  assert.strictEqual(ask.data.keyboardVisible,false,'Cached page must not retain keyboard layout after leaving');
  ask.toggleAnswerDetails({currentTarget: {dataset: {index: 1}}});
  assert.strictEqual(ask.data.messages[1].detailsExpanded, true);

  const failed = loadPage('ask', {askQuestion: async () => ({status: 'model_unavailable', mode: 'background', answer: '暂时不可用'})}).page;
  failed.data.inputValue = '分析美光';
  failed.data.pendingEventId = 'EVT-context';
  failed.data.pendingEventVersion = 3;
  await failed.submitAsk();
  assert.strictEqual(failed.data.messages.at(-1).title, '服务暂时不可用');
  assert.strictEqual(failed.data.messages.at(-1).canRetry, true);
  let retried = false;
  failed.submitAsk = () => { retried = true; };
  failed.retryAsk({currentTarget: {dataset: {index: 1, query: '分析美光'}}});
  assert.ok(retried);
  assert.strictEqual(failed.data.pendingEventId, 'EVT-context', 'Retry must keep event anchor');
  assert.strictEqual(failed.data.pendingEventVersion, 3);

  const watch = loadPage('watchlist').page;
  watch.data.watchlist = [{ticker: 'MU', market: 'US', company_name_zh: '美光', offsetX: 0}, {ticker: 'SNDK', market: 'US', company_name_zh: '闪迪', offsetX: 0}];
  watch.onFilterInput({detail: {value: '闪迪'}});
  assert.strictEqual(watch.data.filteredList.length, 1);
  assert.strictEqual(watch.data.filteredList[0].ticker, 'SNDK');
  watch.data.filterQuery = '';
  watch.applyFilter('all');
  const gesture = (x, y) => ({touches: [{clientX: x, clientY: y}], currentTarget: {dataset: {index: 0}}});
  watch.onTouchStart(gesture(200, 100)); watch.onTouchMove(gesture(180, 160)); watch.onTouchEnd(gesture(180, 160));
  assert.strictEqual(watch.data.filteredList[0].offsetX, 0, 'Vertical scroll must not open actions');
  watch.onTouchStart(gesture(200, 100)); watch.onTouchMove(gesture(120, 105)); watch.onTouchEnd(gesture(120, 105));
  assert.strictEqual(watch.data.filteredList[0].offsetX, -240);
  watch.onTouchStart(gesture(100, 100)); watch.onTouchMove(gesture(200, 101)); watch.onTouchEnd(gesture(200, 101));
  assert.strictEqual(watch.data.filteredList[0].offsetX, 0, 'Open row must close smoothly with right drag');
  console.log('UI behavior passed: evidence preservation, typed status, timer cleanup, event retry, search and gesture direction.');
})().catch(error => { console.error(error); process.exit(1); });
