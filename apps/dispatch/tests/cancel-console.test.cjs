const test=require('node:test');
const assert=require('node:assert/strict');
const {cancellationController}=require('../static/dispatch/console.js');
const cancelled={order_no:'staff-fixture',status:'已取消'};
test('cancel lost reply is recovered by reading the same order',async()=>{
  let sends=0,reads=0;
  const ctl=cancellationController({orderNo:'staff-fixture',send:async reason=>{sends++;assert.equal(reason,'老板取消');throw new Error('lost')},query:async()=>{reads++;return cancelled}});
  assert.equal(await ctl.submit(' 老板取消 '),cancelled);assert.equal(sends,1);assert.equal(reads,1);
});
test('double-click cancels only once',async()=>{
  let done,sends=0;const wait=new Promise(resolve=>done=resolve);
  const ctl=cancellationController({orderNo:'staff-fixture',send:async()=>{sends++;await wait},query:async()=>cancelled});
  const first=ctl.submit('老板取消');await assert.rejects(ctl.submit('老板取消'),/正在取消/);done();await first;assert.equal(sends,1);
});
test('unreadable or mismatched state is not cancellation success',async()=>{
  for(const query of [async()=>{throw new Error('lost read')},async()=>({...cancelled,order_no:'wrong-order'}),async()=>({order_no:'staff-fixture',status:'待接单'})]){
    const ctl=cancellationController({orderNo:'staff-fixture',send:async()=>{},query});await assert.rejects(ctl.submit('取消'),/待确认/);
  }
});
test('business refusal displays the backend restriction, without claiming refund',async()=>{
  const error=Object.assign(new Error('已开打，不能直接取消'),{status:409});
  const ctl=cancellationController({orderNo:'staff-fixture',send:async()=>{throw error},query:async()=>({order_no:'staff-fixture',status:'进行中'})});
  await assert.rejects(ctl.submit('取消'),/已开打/);
});
test('empty or long reasons never send',async()=>{
  let sends=0;const ctl=cancellationController({send:async()=>sends++,query:async()=>cancelled});
  await assert.rejects(ctl.submit(' '),/取消原因/);await assert.rejects(ctl.submit('字'.repeat(201)),/200/);assert.equal(sends,0);
});
