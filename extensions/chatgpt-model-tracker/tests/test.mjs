import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';
const src=name=>readFileSync(new URL('../'+name,import.meta.url),'utf8');
function parser(){const c=vm.createContext({});vm.runInContext(src('parser.js'),c);return c.ModelTrackerParser;}
const frame=o=>'data: '+JSON.stringify(o)+'\r\n\r\n';
const message=(id='a',metadata={model_slug:'model-returned'})=>({message:{id,author:{role:'assistant'},metadata,content:{parts:['SECRET BODY model: fake']}}});
test('SSE split at every possible boundary, CRLF and no content leakage',()=>{
 const input=frame(message())+'data: [DONE]\r\n\r\n';
 for(let i=0;i<input.length;i++) {const events=[];const p=new (parser())(e=>events.push(e));p.push(input.slice(0,i));p.push(input.slice(i),true);assert.equal(events.length,1);assert.equal(events[0].metadata.model_slug,'model-returned');assert.ok(!JSON.stringify(events).includes('SECRET'));}
});
test('wrapper and delta fields; default is kept separate; deduplication',()=>{
 const events=[];const p=new (parser())(e=>events.push(e));p.object({v:message('a',{})});
 p.object({p:'/message/metadata/model_slug',o:'replace',v:'routed-model'});
 p.object({p:'/message/metadata/default_model_slug',o:'add',v:'selected-default'});
 p.object({p:'/message/metadata/model_slug',o:'replace',v:'routed-model'});
 assert.equal(events.length,3);assert.equal(events.at(-1).metadata.model_slug,'routed-model');assert.equal(events.at(-1).metadata.default_model_slug,'selected-default');
});
test('unknown and malformed data do not become invented models',()=>{
 const events=[];const p=new (parser())(e=>events.push(e));p.push('data: broken\n\ndata: [DONE]\n\n');p.object(message('a',{}));
 assert.equal(Object.keys(events[0].metadata).length,0);
 p.object({message:{id:'tool',author:{role:'tool'}}});p.object({p:'/message/metadata/model_slug',o:'replace',v:'wrong'});assert.equal(events.length,1);
});
test('separate assistant messages and unfinished last frame',()=>{
 const events=[];const p=new (parser())(e=>events.push(e));p.push(frame(message('a'))+frame(message('b')).trim(),true);assert.deepEqual(events.map(e=>e.message_id),['a','b']);
});
test('oversized frame is stopped',()=>{assert.throws(()=>new (parser())(()=>{}).push('x'.repeat(2_000_001)));});
test('CSV has UTF-8 BOM, escaping, formula protection and unknown cells',()=>{
 const c=vm.createContext({});vm.runInContext(src('export.js'),c);
 const r={id:'r',query_id:'q',requested_model:'=1+1',responses:[{message_id:'a',metadata:{model_slug:'x,"y'},message_fields:{}}]};
 const csv=c.ModelTrackerExport.csv([r]);assert.ok(csv.startsWith('\uFEFF'));assert.ok(csv.includes('"\'=1+1"'));assert.ok(csv.includes('"x,""y"'));assert.equal(c.ModelTrackerExport.rows([{...r,responses:[]}]).length,1);
});
test('fetch interception preserves original readable response and request association',async()=>{
 const events=[];const input=frame(message());
 const c=vm.createContext({URL,Request,Response,TextDecoder,crypto,location:{origin:'https://chatgpt.com',href:'https://chatgpt.com/'},
   fetch:()=>Promise.resolve(new Response(input,{headers:{'content-type':'text/event-stream'}})),postMessage:e=>events.push(e.payload)});
 c.window=c;vm.runInContext(src('parser.js'),c);vm.runInContext(src('hook.js'),c);
 const response=await c.fetch('/backend-api/conversation',{method:'POST',body:JSON.stringify({model:'auto',messages:[{id:'query',author:{role:'user'},content:{parts:['PRIVATE']}}]})});
 assert.equal(await response.text(),input);
 for(let i=0;i<100&&!events.some(e=>e.type==='status');i++)await new Promise(r=>setTimeout(r,2));
 assert.equal(events[0].type,'start');assert.equal(events[0].query_id,'query');assert.equal(events[0].requested_model,'auto');
 assert.equal(events.find(e=>e.type==='response').item.metadata.model_slug,'model-returned');assert.equal(events.at(-1).status,'complete');assert.ok(!JSON.stringify(events).includes('PRIVATE'));
 const n=events.length;await c.fetch('https://example.org/backend-api/conversation',{method:'POST'});assert.equal(events.length,n);
});
test('background serializes concurrent updates and clear prevents resurrection',async()=>{
 let listener,stored={};const c=vm.createContext({URL,chrome:{runtime:{getURL:p=>'chrome-extension://test/'+p,onMessage:{addListener:f=>listener=f}},storage:{local:{get:async()=>structuredClone(stored),set:async v=>{await new Promise(r=>setTimeout(r,1));stored=structuredClone(v);}}}}});
 vm.runInContext(src('background.js'),c);
 const page={tab:{id:1},url:'https://chatgpt.com/'};
 const send=(m,s=page)=>new Promise(resolve=>{const ret=listener(m,s,resolve);if(ret!==true)resolve();});
 await Promise.all([send({type:'start',id:'a',requested_model:'auto'}),send({type:'start',id:'b'}),send({type:'response',id:'a',item:{message_id:'m',metadata:{model_slug:'returned',secret:'PRIVATE'}}})]);
 assert.equal(stored.modelTrackerRecords.length,2);assert.equal(stored.modelTrackerRecords[0].responses.length,1);assert.ok(!JSON.stringify(stored).includes('PRIVATE'));
 await send({type:'clear'},page);assert.equal(stored.modelTrackerRecords.length,2);
 await send({type:'clear'},{url:'chrome-extension://test/popup.html'});await send({type:'status',id:'a',status:'complete'});assert.equal(stored.modelTrackerRecords.length,0);
});
