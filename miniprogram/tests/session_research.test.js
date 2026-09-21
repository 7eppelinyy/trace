const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const storage = new Map();
const calls = [];
const app = {globalData: {apiBase: 'https://example.invalid/api/v1'}};
const wx = {
  getStorageSync: k => storage.get(k),
  setStorageSync: (k,v) => storage.set(k,v),
  removeStorageSync: k => storage.delete(k),
  request(options) {
    calls.push(options);
    if (options.url.endsWith('/auth/session') && options.method === 'POST') {
      options.success({
        statusCode: 200,
        data: {
          session_token: 'server-token-1',
          refresh_token: 'refresh-token-1',
          user_id: 'usr-server',
          expires_at: new Date(Date.now() + 3600000).toISOString()
        }
      });
    } else if (options.url.endsWith('/auth/session/refresh') && options.method === 'POST') {
      assert.strictEqual(options.data.refresh_token, 'refresh-token-1');
      options.success({
        statusCode: 200,
        data: {
          session_token: 'server-token-2',
          refresh_token: 'refresh-token-2',
          user_id: 'usr-server',
          expires_at: '2099-01-01T00:00:00Z'
        }
      });
    } else if (options.url.endsWith('/auth/session') && options.method === 'DELETE') {
      if (options.success) options.success({ statusCode: 200, data: { ok: true } });
    } else if (options.success) {
      options.success({statusCode: 200, data: {items: []}});
    }
  }
};
const moduleObject = {exports: {}};
vm.runInNewContext(fs.readFileSync('miniprogram/utils/api.js','utf8'),
  {module: moduleObject, require: () => ({}), getApp: () => app, wx, console, setTimeout, clearTimeout});

(async () => {
  const api = moduleObject.exports;
  
  // 1. Initial auth calls deduplicated to single session issuance
  await Promise.all([api.request({url:'/watchlist'}), api.request({url:'/preferences'})]);
  const authCalls = calls.filter(c => c.url.endsWith('/auth/session') && c.method === 'POST');
  assert.strictEqual(authCalls.length, 1);
  assert.strictEqual(authCalls[0].data.grant_type, 'guest');
  assert.strictEqual(authCalls[0].data.user_id, undefined);
  assert.strictEqual(api.getUserId(), 'usr-server');

  // 2. Bearer token sent on protected endpoints
  const protectedCall = calls.find(c => c.url.endsWith('/watchlist'));
  assert.strictEqual(protectedCall.header.Authorization, 'Bearer server-token-1');
  assert.strictEqual(protectedCall.header['X-User-ID'], undefined);

  // 3. Cross backend rejection
  await assert.rejects(api.request({url:'https://other.invalid/private'}), /Cross-backend/);

  // 4. Public endpoint without token
  const before = calls.length;
  await api.request({url:'/research/questions/q/share?token=public-capability', public:true});
  assert.strictEqual(calls.length, before+1);
  assert.strictEqual(calls[calls.length-1].header.Authorization, undefined);

  // 5. Session refresh lifecycle: simulated expired token triggers refresh
  // Make access token expired in storage
  const sKey = 'trace_session:https://example.invalid/api/v1';
  const sess = storage.get(sKey);
  assert.ok(sess && sess.refresh_token);
  sess.expires_at = '2020-01-01T00:00:00Z'; // expired
  storage.set(sKey, sess);

  // Concurrent requests encountering expired session coordinate on single refresh
  const [resA, resB] = await Promise.all([
    api.request({url:'/watchlist'}),
    api.request({url:'/watchlist'})
  ]);
  const refreshCalls = calls.filter(c => c.url.endsWith('/auth/session/refresh'));
  assert.strictEqual(refreshCalls.length, 1, 'Concurrent expired requests must trigger only one refresh');
  assert.strictEqual(api.getUserId(), 'usr-server', 'User ID must remain unchanged after refresh');
  const updatedSess = storage.get(sKey);
  assert.strictEqual(updatedSess.session_token, 'server-token-2');
  assert.strictEqual(updatedSess.refresh_token, 'refresh-token-2');

  // 6. User scoped watchlist cache check
  await api.getWatchlist();
  const scopedWlKey = 'cached_watchlist:https://example.invalid/api/v1:usr-server';
  assert.ok(storage.has(scopedWlKey), 'Watchlist cache must be scoped by backend and user ID');

  // 7. Reset clears user session and scoped caches
  api.resetUserId();
  assert.strictEqual(storage.has(sKey), false);
  assert.strictEqual(storage.has(scopedWlKey), false);

  // 8. Research page integration test
  let page;
  const backend = [];
  const requests = async options => {
    backend.push(options);
    if (options.url.endsWith('/draft')) return {event_id:'EVT-test',title:'Research',hypothesis:'Hypothesis',supporting_conditions:[],contradicting_conditions:[]};
    if (options.method === 'POST') return {question_id:'q',revision:1,...options.data};
    return {items:[]};
  };
  vm.runInNewContext(fs.readFileSync('miniprogram/pages/research/research.js','utf8'), {
    Page: p => page=p, require: () => ({request:requests,getUserId:()=> 'usr-server'}), console,
    wx: {showToast:()=>{}}, Date
  });
  page.setData = function(data) {Object.assign(this.data,data);};
  await page.onLoad({eventId:'EVT-test'});
  assert.strictEqual(page.data.selected.event_id,'EVT-test');
  await page.save();
  const create = backend.find(c => c.url === '/research/questions' && c.method === 'POST');
  assert.strictEqual(create.data.event_id,'EVT-test');
  assert.strictEqual(page.data.selected.question_id,'q');
  assert.strictEqual(page.data.selected.revision,1);
  assert.strictEqual(page.data.sharePath,'');

  console.log('Session, token refresh, and research client contracts passed (device guest, token rotation, cache isolation, public capabilities).');
})().catch(err => {console.error(err);process.exitCode=1;});
