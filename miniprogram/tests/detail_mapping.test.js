// miniprogram/tests/detail_mapping.test.js
// 验证 T01 真实性契约：未知显示为未知，错误显示为错误，不得伪造数据与回退 mock
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

function loadPage(apiMock = {}) {
  let pageInstance;
  const app = { globalData: {} };
  const sandbox = {
    Page: p => { pageInstance = p; },
    getApp: () => app,
    wx: {
      setNavigationBarTitle: () => {},
      showToast: () => {},
      vibrateShort: () => {},
      switchTab: () => {},
      navigateTo: () => {}
    },
    require: modulePath => {
      if (modulePath.includes('utils/api')) {
        return Object.assign({
          getEventDetail: async (id) => null
        }, apiMock);
      }
      if (modulePath.includes('utils/mock')) {
        return require(path.join(__dirname, '../utils/mock.js'));
      }
      return {};
    },
    console
  };

  const code = fs.readFileSync(path.join(__dirname, '../pages/detail/detail.js'), 'utf8');
  vm.runInNewContext(code, sandbox);

  pageInstance.setData = function(obj, cb) {
    Object.assign(this.data, obj);
    if (typeof cb === 'function') cb();
  };

  return { page: pageInstance, app };
}

async function runTests() {
  console.log('=== Running T01 Detail Mapping & Authenticity Tests ===');

  // Test 1: A01 - 无报价时不得伪造 1.5%
  {
    const { page } = loadPage();
    const result = page.transformRealEvent({
      event_id: 'EVT-TEST-1',
      title: '测试事件',
      summary: '测试摘要',
      status: 'reported',
      evidences: [],
      impacts: [
        { security_id: 'SEC-US-NVDA', direction: 'bullish', directness: 'direct' }
      ]
    });

    assert.strictEqual(
      result.securities[0].change_pct,
      null,
      'A01 Failed: change_pct must be null when no quote exists, got ' + result.securities[0].change_pct
    );
    assert.strictEqual(
      result.securities[0].direction,
      'bullish',
      'A01 Failed: direction should be retained'
    );
    console.log('✔ A01 Passed: No fake change_pct generated for bullish impact without quote');
  }

  // Test 2: A02 - 缺失评分不得默认填入 7.0 分
  {
    const { page } = loadPage();
    const result = page.transformRealEvent({
      event_id: 'EVT-TEST-2',
      title: '测试事件',
      summary: '测试摘要',
      status: 'reported',
      transmission_depth: 3,
      evidences: [],
      impacts: [
        { security_id: 'SEC-US-NVDA', direction: 'bullish', directness: 'direct' }
      ]
    });

    const level4 = result.chain.find(c => c.level === 4);
    assert.ok(level4, 'Level 4 node must exist when transmission_depth >= 3');
    assert.ok(
      !level4.desc.includes('7.0 分'),
      'A02 Failed: Missing score must not default to 7.0 分, got: ' + level4.desc
    );
    console.log('✔ A02 Passed: Missing score does not default to 7.0');
  }

  // Test 3: A03 - reported / 无证据事件不得声称“官方源证实，已完成权威信源交叉校验”
  {
    const { page } = loadPage();
    const result = page.transformRealEvent({
      event_id: 'EVT-TEST-3',
      title: '媒体传闻测试',
      summary: '传闻摘要',
      status: 'reported',
      evidences: [],
      impacts: []
    });

    const level1 = result.chain.find(c => c.level === 1);
    assert.ok(level1, 'Level 1 node must exist');
    assert.ok(
      !level1.desc.includes('官方源证实'),
      'A03 Failed: Reported event must not claim 官方源证实, got: ' + level1.desc
    );
    assert.ok(
      !level1.desc.includes('已完成权威信源交叉校验'),
      'A03 Failed: Unconfirmed event must not claim 交叉校验, got: ' + level1.desc
    );
    console.log('✔ A03 Passed: Unconfirmed event does not claim official verification or cross check');
  }

  // Test 4: A04 - 真实事件失败不得静默降级为 Apple NAND
  {
    const { page } = loadPage({
      getEventDetail: async () => null // simulate 404 / network failure
    });

    await page.onLoad({ id: 'EVT-UNAVAILABLE' });

    assert.strictEqual(page.data.eventId, 'EVT-UNAVAILABLE', 'Event ID must be preserved');
    assert.strictEqual(page.data.loadError, true, 'loadError flag must be set to true');
    assert.strictEqual(page.data.event, null, 'event must be null when loading fails');
    console.log('✔ A04 Passed: Failed real event does not fallback to Apple NAND mock');
  }

  // Test 5: A05 - getMockDetailById 未知 ID 不得返回 Apple NAND
  {
    const { getMockDetailById } = require('../utils/mock');
    const unknown = getMockDetailById('nonexistent-id');
    assert.strictEqual(unknown, null, 'Unknown id must return null, not Apple NAND fallback');
    const nand = getMockDetailById('apple-nand');
    assert.strictEqual(nand.is_mock, true, 'Demo data must have is_mock flag');
    console.log('✔ A05 Passed: getMockDetailById does not silently fallback to Apple NAND');
  }

  // Test 6: A06 - 仅直接影响时图谱严格仅生成 2 级，不强凑 4 级 (F14)
  {
    const { page } = loadPage();
    const result = page.transformRealEvent({
      event_id: 'EVT-TEST-DIRECT',
      title: '直接影响事件',
      summary: '直接影响',
      status: 'confirmed',
      transmission_depth: 1,
      evidences: [
        { raw_item_id: 'RAW-1', source_id: 'src_sec_edgar', title: 'SEC公告', url: 'https://sec.gov/doc' }
      ],
      impacts: [
        { security_id: 'SEC-US-AAPL', direction: 'bullish', directness: 'direct', reason: '直接采购增量' }
      ]
    });

    assert.strictEqual(result.chain.length, 2, 'A06 Failed: chain must have exactly 2 levels for direct-only event');
    assert.strictEqual(result.chain[0].level, 1);
    assert.strictEqual(result.chain[1].level, 2);
    console.log('✔ A06 Passed: Direct-only event strictly outputs 2-level topology chain');
  }

  // Test 7: A07 - 事实与证据结构化抽取完整性 (Claims / Evidences / Uncertainties)
  {
    const { page } = loadPage();
    const result = page.transformRealEvent({
      event_id: 'EVT-TEST-RICH',
      version: 2,
      title: '多维证据事件',
      summary: '测试摘要',
      status: 'confirmed',
      transmission_depth: 2,
      evidences: [
        { raw_item_id: 'RAW-10', source_id: 'src_cninfo', title: '官方公告', url: 'https://cninfo.com.cn/10', role: 'primary' }
      ],
      impacts: [
        { security_id: 'SEC-CN-600519', direction: 'bullish', directness: 'direct', reason: '提价预期' },
        { security_id: 'SEC-CN-000858', direction: 'bullish', directness: 'indirect', reason: '行业协同' }
      ],
      claims: [
        { claim_id: 'C1', kind: 'fact', text: '出厂价上调' }
      ],
      uncertainties: ['实际终端动销转化率待观察'],
      next_checks: ['跟踪下一季度批价稳定性']
    });

    assert.strictEqual(result.version, 2, 'Version must be preserved');
    assert.strictEqual(result.statusLabel, '官方证实');
    assert.strictEqual(result.evidences.length, 1);
    assert.strictEqual(result.evidences[0].url, 'https://cninfo.com.cn/10');
    assert.strictEqual(result.claims.length, 1);
    assert.strictEqual(result.uncertainties.length, 1);
    assert.strictEqual(result.next_checks.length, 1);
    assert.strictEqual(result.chain.length, 3, '1 indirect impact should result in 3 levels (L1, L2, L3)');
    console.log('✔ A07 Passed: Rich evidence, claims, and uncertainties correctly mapped');
  }

  console.log('\n=== All Detail Mapping & Authenticity Tests Passed! ===\n');
}

runTests().catch(err => {
  console.error('\n❌ Test execution failed:\n', err);
  process.exit(1);
});
