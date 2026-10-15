/* Offline checks for honest admin profile-check diagnostics. */
const fs=require('fs');
const vm=require('vm');
const assert=require('assert');
const path=require('path');

const src=fs.readFileSync(path.join(__dirname,'../static/js/app.js'),'utf8');
const match=src.match(/function profileCheckText\(a\)\{[\s\S]*?\n\}/);
assert(match,'admin profile result formatter must exist');
const formatter=vm.runInNewContext(`${match[0]}; profileCheckText`);
assert.strictEqual(formatter({profile_check_status:'unchanged',profile_check_message:'Server profile unchanged'}),
  'Server profile unchanged: Server profile unchanged');
assert.strictEqual(formatter({profile_check_status:'error',profile_check_message:'GetLoginData failed (HTTP 503).'}),
  'Profile check failed: GetLoginData failed (HTTP 503).');
assert.strictEqual(formatter({profile_check_status:'changed',profile_check_message:'Profile changed'}),
  'Server profile changed: Profile changed');
assert.strictEqual(formatter({profile_check_status:'never',profile_check_message:''}),
  'Not checked yet');
assert(src.includes("detailItem('Profile check result',profileCheckText(a))"),
  'account drawer must show the latest profile-check result and safe reason');
assert(src.includes('a.profile_check_message'),
  'drawer render signature must refresh when the profile-check reason changes');
console.log('Admin profile diagnostics: changed, unchanged, failed and never-checked states ✓');
