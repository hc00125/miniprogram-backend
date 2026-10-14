const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const file=path.join(__dirname,'../static/dispatch/console.js');
test('lost create response is reconciled by the original request, never a new order',async()=>{
  assert.ok(fs.existsSync(file),'客服网页脚本尚未实现');
  const {submissionController}=require(file);
  let stored=null, sends=0, reads=0;
  const ctl=submissionController({save:v=>stored=v,load:()=>stored,
    send:async body=>{sends++;throw new Error('response lost')},
    query:async key=>{reads++;assert.equal(key,'original');return {order_no:'EXAMPLE-ORDER'}}});
  const result=await ctl.submit({request_key:'original',customer_ref:'customer:1'});
  assert.equal(result.order_no,'EXAMPLE-ORDER');assert.equal(sends,1);assert.equal(reads,1);assert.equal(stored,null);
});
