(() => {
  const MARK = '__chatgptModelTracker01';
  if (window[MARK]) return;
  window[MARK] = true;
  const originalFetch = window.fetch;
  const clean = v => typeof v === 'string' ? v.slice(0,180) : '';
  const send = payload => window.postMessage({source:MARK,payload}, location.origin);
  function target(input, init) {
    try {
      const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url, location.href);
      const method = (init?.method || input?.method || 'GET').toUpperCase();
      return url.origin === location.origin && method === 'POST' &&
        /^\/backend-api\/(?:f\/)?conversation\/?$/.test(url.pathname);
    } catch { return false; }
  }
  async function requestBody(input, init) {
    if (typeof init?.body === 'string') return JSON.parse(init.body);
    if (input instanceof Request && init?.body === undefined) return JSON.parse(await input.clone().text());
    return {};
  }
  async function observe(response, id) {
    let reader;
    try {
      if (!response.ok) { response.body?.cancel().catch(()=>{}); send({type:'status',id,status:`http_${response.status}`}); return; }
      const ct = response.headers.get('content-type') || '';
      if (!ct.includes('text/event-stream') && !ct.includes('application/json')) {
        response.body?.cancel().catch(()=>{});
        send({type:'status',id,status:'unsupported_transport'}); return;
      }
      const copy = response;
      const parser = new ModelTrackerParser(item => send({type:'response',id,item}));
      if (ct.includes('text/event-stream')) {
        reader = copy.body.getReader();
        const decoder = new TextDecoder(); let bytes=0;
        while (true) {
          const {done,value} = await reader.read();
          if (done) break;
          bytes += value.byteLength;
          if (bytes > 32_000_000) throw new Error('stream_limit');
          parser.push(decoder.decode(value,{stream:true}));
        }
        parser.push(decoder.decode(),true);
      } else {
        // Limit JSON copying as well; no body is retained after parsing.
        reader = copy.body.getReader(); const decoder = new TextDecoder(); let json='';
        while (true) {
          const {done,value} = await reader.read(); if(done) break;
          json += decoder.decode(value,{stream:true});
          if(json.length > 2_000_000) throw new Error('json_limit');
        }
        parser.object(JSON.parse(json+decoder.decode()));
      }
      send({type:'status',id,status:parser.seen.size ? 'complete' : 'no_message_metadata'});
    } catch { send({type:'status',id,status:'capture_incomplete'}); }
    finally { if(reader) { reader.cancel().catch(()=>{}); } }
  }
  window.fetch = function(input, init) {
    if (!target(input,init)) return Reflect.apply(originalFetch,this,arguments);
    const id=crypto.randomUUID();
    // Start metadata extraction before the original Request body can be consumed.
    const body=requestBody(input,init).catch(()=>({}));
    const start=body.then(b => {
      const users=Array.isArray(b.messages) ? b.messages.filter(m=>m?.author?.role==='user') : [];
      send({type:'start',id,time:new Date().toISOString(),requested_model:clean(b.model),
        query_id:clean(users.at(-1)?.id),conversation_id:clean(b.conversation_id)});
    });
    let pending;
    try { pending=Reflect.apply(originalFetch,this,arguments); }
    catch(error) { start.then(()=>send({type:'status',id,status:'network_error'})); throw error; }
    pending.then(response => {
      // Clone synchronously before the page's fetch continuation locks its body.
      let copy;
      try { copy=response.clone(); } catch { return start.then(()=>send({type:'status',id,status:'capture_incomplete'})); }
      return start.then(()=>observe(copy,id));
    },
      () => start.then(()=>send({type:'status',id,status:'network_error'}))).catch(()=>{});
    return pending;
  };
})();
