(() => {
  const columns=['time','request_id','query_id','response_id','requested_model','response_model_slug',
    'response_model','default_model_slug','message_model_slug','message_model','status','conversation_id'];
  function rows(records) {
    return records.flatMap(r=>(r.responses.length?r.responses:[{}]).map(a=>({
      time:r.time,request_id:r.id,query_id:r.query_id,response_id:a.message_id||'',requested_model:r.requested_model,
      response_model_slug:a.metadata?.model_slug||'',response_model:a.metadata?.model||'',
      default_model_slug:a.metadata?.default_model_slug||'',message_model_slug:a.message_fields?.model_slug||'',
      message_model:a.message_fields?.model||'',status:r.status,conversation_id:r.conversation_id
    })));
  }
  function csv(records) {
    const quote=v=>'"'+String(v??'').replace(/^[\s]*[=+@-]/,m=>"'"+m).replace(/"/g,'""')+'"';
    return '\uFEFF'+[columns,...rows(records).map(r=>columns.map(k=>r[k]))].map(row=>row.map(quote).join(',')).join('\r\n');
  }
  globalThis.ModelTrackerExport={rows,csv};
})();
