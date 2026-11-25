const assert = require('node:assert/strict');
const downloads = require('../web/downloads.js');
const node = () => ({hidden:true, disabled:false, listeners:{}, attributes:{}, value:0, textContent:'', clicks:0,
  setAttribute(key, value) {this.attributes[key] = value;}, addEventListener(name, handler) {this.listeners[name] = handler;}, click() {this.clicks++;}});
const base = {id:'a'.repeat(32), label:'Test run', state:'queued', phase:'Preparing ZIP', percent:0,
  bytes_done:0, bytes_total:1000, files_done:0, files_total:2, current_file:''};
function setup(responses) {
  const calls = [], pending = [], saved = new Map();
  const elements = Object.fromEntries(['panel','heading','progress','status','currentFile','ready','cancel','startAll','list'].map(name => [name,node()]));
  const controller = downloads.create({...elements,
    api:async (url, body) => {calls.push({url,body}); const next = responses.shift(); if (next instanceof Error) throw next; return next;},
    storage:{setItem:(key,v) => saved.set(key,v), getItem:key => saved.get(key), removeItem:key => saved.delete(key)},
    timer:fn => {pending.push(fn); return fn;}, stopTimer:fn => {const i = pending.indexOf(fn); if(i >= 0)pending.splice(i,1);}});
  return {controller, elements, calls, pending, saved};
}
async function check() {
  const first = setup([{...base}, {...base,state:'running',percent:40,bytes_done:400,current_file:'run_BINARY.csv'},
    {...base,state:'paused',phase:'wrong worker phase',percent:40},
    {...base,state:'complete',percent:100,size:700,url:'/api/archives/'+base.id+'/download'}]);
  await first.controller.start({session:'session1'});
  assert.equal(first.elements.panel.hidden, false); assert.equal(first.elements.progress.value,0);
  assert.equal(first.calls[0].url,'/api/archives'); assert.deepEqual(first.calls[0].body,{session:'session1'});
  await first.pending.shift()();
  assert.equal(first.elements.progress.value,40); assert.equal(first.elements.currentFile.textContent,'run_BINARY.csv');
  await first.controller.start({all:true}); assert.equal(first.calls.length,2); // Never spawn a duplicate conversion.
  await first.pending.shift()(); assert.match(first.elements.status.textContent,/Paused for recording/);
  await first.pending.shift()();
  assert.equal(first.elements.progress.value,100); assert.equal(first.elements.ready.hidden,false);
  assert.equal(first.elements.ready.clicks,1); assert.equal(first.elements.cancel.hidden,true);
  assert.equal(first.pending.length,0); // Native link download: no fetch-to-Blob for a large ZIP.
  assert.equal(first.calls.length,4);
  const cancelled = setup([{...base}, {...base,state:'cancelled',percent:10}]);
  await cancelled.elements.startAll.onclick();
  assert.deepEqual(cancelled.calls[0].body,{all:true, ld_only:true});
  assert.equal(cancelled.elements.panel.hidden,false);
  await cancelled.elements.cancel.onclick();
  assert.match(cancelled.calls[1].url,/\/cancel$/); assert.equal(cancelled.elements.cancel.hidden,true);
  assert.equal(cancelled.pending.length,0); assert.equal(cancelled.saved.size,0);
  const retry = setup([{...base}, new Error('Panel offline'), {...base,state:'complete',percent:100,size:42,url:'/ready'}]);
  await retry.controller.start({all:true}); await retry.pending.shift()();
  assert.match(retry.elements.status.textContent,/retrying/); assert.equal(retry.pending.length,1);
  await retry.pending.shift()(); assert.equal(retry.elements.ready.clicks,1);
  const failure = setup([new Error('Not enough ZIP storage')]);
  await failure.controller.start({all:true}); assert.match(failure.elements.status.textContent,/Not enough ZIP storage/);
  assert.equal(failure.elements.startAll.disabled,false); assert.equal(failure.pending.length,0);
  assert.equal(downloads.active('complete'),false);
  console.log('ZIP progress, capture pause, cancellation, duplicate prevention, reconnect and native large-file download checks passed');
}
check().catch(error => {console.error(error); process.exitCode=1;});
