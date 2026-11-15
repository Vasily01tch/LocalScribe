import {test} from 'node:test';
import assert from 'node:assert/strict';
import {TranscriptDrafts} from '../src/transcriptDrafts.ts';
const segment=(text:string)=>[{start:0,end:1,text}];

test('switching recordings preserves both pending edits', async()=>{
  const saved=new Map<string,string>();
  const drafts=new TranscriptDrafts(async(id,value)=>{saved.set(id,value[0].text)},()=>{},()=>{},10000);
  drafts.update('first',segment('правка 1')); drafts.update('second',segment('правка 2'));
  await drafts.flushAll();
  assert.equal(saved.get('first'),'правка 1'); assert.equal(saved.get('second'),'правка 2');
});
test('edits made while a save is in flight cannot be overwritten by it',async()=>{
  let release!:()=>void; const gate=new Promise<void>(r=>release=r); const writes:string[]=[];let notified='';
  const drafts=new TranscriptDrafts(async(_id,value)=>{writes.push(value[0].text);if(writes.length===1)await gate},(_id,value)=>{notified=value[0].text},()=>{},10000);
  drafts.update('a',segment('старый'));const saving=drafts.flush('a');
  drafts.update('a',segment('новый')); release();await saving;
  assert.deepEqual(writes,['старый','новый']);assert.equal(notified,'новый');assert.equal(drafts.get('a'),undefined);
});
test('a failed write retains the draft and can be retried',async()=>{
  let fails=true;const drafts=new TranscriptDrafts(async()=>{if(fails)throw Error('disk')},()=>{},()=>{},10000);
  drafts.update('a',segment('важно'));await assert.rejects(drafts.flush('a'));assert.equal(drafts.get('a')?.[0].text,'важно');
  fails=false;await drafts.flushAll();assert.equal(drafts.get('a'),undefined);
});
