let records=[];
const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
const statuses={capturing:'正在采集',complete:'流读取完成',no_message_metadata:'未识别到消息元数据',
  capture_incomplete:'采集不完整',unsupported_transport:'不支持的响应格式',network_error:'请求失败'};
function render() {
  const root=document.getElementById('records');root.replaceChildren();
  document.getElementById('summary').textContent=`${records.length} 次请求 · 所有标签页 · 时间按浏览器本地时区显示`;
  for(const name of ['json','csv','clear']) document.getElementById(name).disabled=!records.length;
  if(!records.length) root.append(el('div','尚无记录。刷新 ChatGPT 页面，然后发送一条消息。','empty'));
  for(const r of [...records].reverse()) {
    const card=el('article');card.append(el('h2',`${new Date(r.time).toLocaleString()} · ${statuses[r.status]||r.status}`));
    const line=(label,value)=>{const n=el('div',undefined,'line');n.append(el('span',label,'label'),el('span',value,'value'));card.append(n);};
    line('Query 请求模型',r.requested_model||'未知');
    if(!r.responses.length) line('Response 模型','未知');
    r.responses.forEach((a,i)=>{
      const models=[...new Set([a.metadata.model_slug,a.metadata.model,a.message_fields.model_slug,a.message_fields.model].filter(Boolean))];
      line(`Response ${i+1}`,models.join(' / ')||'未知');
      if(a.metadata.default_model_slug) line('默认模型字段',a.metadata.default_model_slug);
      card.append(el('div',`回复 ID：${a.message_id}`,'meta'));
    });
    card.append(el('div',`提问 ID：${r.query_id||'未提供（可能为继续生成或重新生成）'}`,'meta'));
    const details=el('details');details.append(el('summary','查看原始字段与来源'),el('pre',JSON.stringify(r,null,2)));card.append(details);root.append(card);
  }
}
async function load(){const value=await chrome.storage.local.get('modelTrackerRecords');records=value.modelTrackerRecords||[];render();}
function download(type){
  const text=type==='csv'?ModelTrackerExport.csv(records):JSON.stringify({schema_version:1,exported_at:new Date().toISOString(),
    note:'Observed public fields only; not verified backend model identity. No conversation bodies stored.',records},null,2);
  const url=URL.createObjectURL(new Blob([text],{type:type==='csv'?'text/csv;charset=utf-8':'application/json'}));
  const a=el('a');a.href=url;a.download=`chatgpt-models-${new Date().toISOString().replace(/[:.]/g,'-')}.${type}`;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),10000);
}
document.getElementById('json').onclick=()=>download('json');document.getElementById('csv').onclick=()=>download('csv');
document.getElementById('clear').onclick=async()=>{if(confirm('清空此浏览器中的全部模型记录？')) {await chrome.runtime.sendMessage({type:'clear'});await load();}};
chrome.storage.onChanged.addListener((changes,area)=>{if(area==='local'&&changes.modelTrackerRecords)load();});
load().catch(()=>{document.getElementById('summary').textContent='读取失败，请关闭并重新打开插件。';});
