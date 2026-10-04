const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const path = require('path');
const {syncTabBar, setTabBarOverlay} = require('../utils/ui');
let component;
let keyboardListener;
let removedListener;
let navigation;
const wx = {
  getStorageSync: () => true,
  onKeyboardHeightChange: listener => { keyboardListener = listener; },
  offKeyboardHeightChange: listener => { removedListener = listener; },
  switchTab: options => { navigation = options; },
  showToast: () => {}
};
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../custom-tab-bar/index.js'),'utf8'),{
  Component: value => { component = value; }, wx,
  getCurrentPages: () => [{route: 'pages/ask/ask'}]
});
const bar = {data: JSON.parse(JSON.stringify(component.data)), setData(values) {Object.assign(this.data,values);}};
Object.assign(bar, component.methods);
component.lifetimes.attached.call(bar);
assert.strictEqual(bar.data.selected,1,'Direct entry must select current route');
assert.strictEqual(bar.data.reduceMotion,true,'Dock must respect device motion preference');
keyboardListener({height:300});
assert.strictEqual(bar.data.keyboardHidden,true);
keyboardListener({height:0});
assert.strictEqual(bar.data.keyboardHidden,false);
bar.switchTab({currentTarget:{dataset:{index:2}}});
assert.strictEqual(navigation.url,'/pages/watchlist/watchlist');
assert.strictEqual(bar.data.selected,2);
const pending = navigation;
bar.switchTab({currentTarget:{dataset:{index:3}}});
assert.strictEqual(navigation,pending,'Repeated taps must not start concurrent navigation');
pending.fail(); pending.complete();
assert.strictEqual(bar.data.selected,1,'Failed navigation must restore actual selection');
bar.switchTab({currentTarget:{dataset:{index:3}}});
assert.strictEqual(navigation.url,'/pages/profile/profile');
navigation.complete();
syncTabBar({data:{reduceMotion:false,showSearchSheet:true},getTabBar:()=>bar},2);
assert.strictEqual(bar.data.selected,2);
assert.strictEqual(bar.data.overlayHidden,true,'Search sheet must not compete with navigation');
setTabBarOverlay({getTabBar:()=>bar},false);
assert.strictEqual(bar.data.overlayHidden,false);
component.lifetimes.detached.call(bar);
assert.strictEqual(removedListener,keyboardListener,'Detached dock must unsubscribe keyboard listener');
console.log('Custom tabbar passed: route selection, failed navigation rollback, tap deduplication, keyboard, overlay and listener cleanup.');
