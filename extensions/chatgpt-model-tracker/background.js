const KEY='modelTrackerRecords';
let queue=Promise.resolve();
const clean=v=>typeof v==='string'?v.slice(0,180):'';
const fields=o=>Object.fromEntries(['model','model_slug','default_model_slug']
  .filter(k=>clean(o?.[k])).map(k=>[k,clean(o[k])]));
// Page-bridge data is untrusted. Never accept commands, HTML, URLs, or body text.
chrome.runtime.onMessage.addListener((msg,sender,reply)=>{
  const fromPopup=!sender.tab && sender.url===chrome.runtime.getURL('popup.html');
  let fromPage=false;
  try { fromPage=!!sender.tab && ['https://chatgpt.com','https://chat.openai.com'].includes(new URL(sender.url).origin); } catch {}
  if (!fromPage && !fromPopup) return;
  if (fromPopup && msg.type!=='clear') return;
  if (fromPage && !['start','response','status'].includes(msg.type)) return;
  queue=queue.catch(()=>{}).then(async()=>{
    if(msg.type==='clear') { await chrome.storage.local.set({[KEY]:[]}); return; }
    const id=clean(msg.id); if(!id) return;
    const stored=await chrome.storage.local.get(KEY); const records=stored[KEY]||[];
    let record=records.find(r=>r.id===id && r.tab_id===sender.tab.id);
    if(msg.type==='start' && !record) {
      record={id,tab_id:sender.tab.id,time:new Date().toISOString(),requested_model:clean(msg.requested_model),
        query_id:clean(msg.query_id),conversation_id:clean(msg.conversation_id),status:'capturing',responses:[]};
      records.push(record);
    }
    if(!record) return; // Cleared in-flight requests stay cleared.
    if(msg.type==='response' && clean(msg.item?.message_id)) {
      const item={message_id:clean(msg.item.message_id),metadata:fields(msg.item.metadata),message_fields:fields(msg.item.message_fields)};
      const old=record.responses.findIndex(r=>r.message_id===item.message_id);
      if(old>=0) record.responses[old]=item;
      else if(record.responses.length<64) record.responses.push(item);
    }
    if(msg.type==='status') record.status=clean(msg.status);
    const retained=records.slice(-300);
    while(retained.length>1 && JSON.stringify(retained).length>2_000_000) retained.shift();
    await chrome.storage.local.set({[KEY]:retained});
  });
  queue.then(()=>reply({ok:true}),()=>reply({ok:false}));
  return true;
});
