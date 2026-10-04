// Device-local appearance settings. Account and backend configuration stay separate.
function getUiSettings() {
  const app = typeof getApp === 'function' ? getApp() : null;
  const data = app && app.globalData || {};
  let reduceMotion = false;
  try { reduceMotion = !!wx.getStorageSync('trace_reduce_motion'); } catch (_) {}
  return {
    navInset: (data.statusBarHeight || 20) + (data.navBarHeight || 44),
    reduceMotion
  };
}
function setReduceMotion(enabled) { wx.setStorageSync('trace_reduce_motion', !!enabled); }
module.exports = { getUiSettings, setReduceMotion };
