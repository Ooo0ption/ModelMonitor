(() => {
  let records=[], timer;
  window.addEventListener('message',event=>{
    if(event.source!==window || event.origin!==location.origin || event.data?.source!=='__chatgptModelTracker01') return;
    const p=event.data.payload;
    if(!p || !['start','response','status'].includes(p.type)) return;
    try { chrome.runtime.sendMessage(p).catch(()=>{}); } catch {}
  });
  function schedule() { clearTimeout(timer); timer=setTimeout(draw,300); }
  function draw() {
    const map=new Map();
    for(const r of records) {
      if(r.query_id) map.set(r.query_id,`请求模型：${r.requested_model||'未知'}`);
      for(const a of r.responses) {
        const values=[a.metadata.model_slug,a.metadata.model,a.message_fields.model_slug,a.message_fields.model].filter(Boolean);
        map.set(a.message_id,`回复模型字段：${[...new Set(values)].join(' / ')||'未知'}`);
      }
    }
    for(const node of document.querySelectorAll('[data-message-id]')) {
      const text=map.get(node.getAttribute('data-message-id'));
      let badge=node.querySelector(':scope > [data-model-tracker-badge]');
      if(!text) { if(badge) badge.remove(); continue; }
      if(!badge) {
        badge=document.createElement('span'); badge.setAttribute('data-model-tracker-badge','');
        badge.style.cssText='display:block;font:11px/1.7 system-ui;color:#628d83;margin:5px 0;overflow-wrap:anywhere';
        badge.title='来自请求 / 回复公开字段；不是独立验证的后台模型身份。';
        node.appendChild(badge);
      }
      if(badge.textContent!==text) badge.textContent=text;
    }
  }
  chrome.storage.local.get('modelTrackerRecords').then(v=>{records=v.modelTrackerRecords||[];schedule();}).catch(()=>{});
  chrome.storage.onChanged.addListener((changes,area)=>{
    if(area==='local' && changes.modelTrackerRecords) { records=changes.modelTrackerRecords.newValue||[];schedule(); }
  });
  const observer=new MutationObserver(schedule);
  observer.observe(document,{childList:true,subtree:true});
})();
