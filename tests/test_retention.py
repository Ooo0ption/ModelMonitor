import gzip
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from retention import SelectiveArchiveWriter, summary_model
from traffic_archive import archive_files,iter_records
from store import Store
from packet_details import packet_detail


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.writer=SelectiveArchiveWriter(self.root,'test');self.index=0;self.raw=[]
        self.context={'id':'ws','type':'http','url':'https://example.test/responses','websocket':{}}

    def tearDown(self):self.writer.close();self.temp.cleanup()

    def ws(self,value,client=False):
        payload=json.dumps(value).encode();event={'kind':'websocket.message','flow_id':'ws','time':1793100000.0+self.index,
            'message_index':self.index,'opcode':1,'direction':'client_to_server' if client else 'server_to_client'}
        self.index+=1;self.writer.write(event,payload,self.context);self.raw.append((event,payload));return event,payload

    def records(self):
        return [item for path in archive_files(self.root) for item in iter_records(path)]

    def test_rules_are_model_fields_not_prompt_substrings(self):
        self.assertTrue(summary_model('gpt-6-astra'));self.assertTrue(summary_model('gpt-6.1-astra'))
        self.assertTrue(summary_model('codex-auto-review'));self.assertFalse(summary_model('gpt-6-luna'))
        self.assertFalse(summary_model('a prompt that mentions gpt-6-astra'));self.assertFalse(summary_model(None))

    def test_astra_summary_and_luna_full_on_the_same_connection(self):
        request,_=self.ws({'type':'response.create','model':'gpt-6-astra','input':'PRIVATE-ASTRA-PROMPT'},True)
        self.ws({'type':'response.created','response':{'id':'astra','model':'gpt-6-astra','status':'in_progress','instructions':'PRIVATE-ASTRA-INSTRUCTIONS'}})
        self.ws({'type':'response.output_item.added','item':{'id':'item-a','type':'function_call','arguments':'PRIVATE-ASTRA-ARGUMENTS'}})
        for _ in range(30):self.ws({'type':'response.function_call_arguments.delta','item_id':'item-a','delta':'PRIVATE-ASTRA-DELTA'})
        self.ws({'type':'response.completed','response':{'id':'astra','model':'gpt-6-astra','status':'completed','output':[{'id':'item-a','text':'PRIVATE-ASTRA-OUTPUT'}],'usage':{'input_tokens':20,'output_tokens':30}}})
        full=[]
        full.append(self.ws({'type':'response.create','model':'gpt-6-luna','input':'KEEP-LUNA-PROMPT'},True)[1])
        full.append(self.ws({'type':'response.created','response':{'id':'luna','model':'gpt-6-luna'}})[1])
        full.append(self.ws({'type':'response.output_text.delta','response_id':'luna','delta':'KEEP-LUNA-DELTA'})[1])
        full.append(self.ws({'type':'response.completed','response':{'id':'luna','model':'gpt-6-luna','status':'completed','output':['KEEP-LUNA-OUTPUT']}})[1])
        self.writer.close();records=self.records();payloads=[p for e,p in records if e['kind']=='websocket.message']
        self.assertTrue(all(p in payloads for p in full));self.assertNotIn(b'PRIVATE-ASTRA',b''.join(p for e,p in records))
        self.assertLess(len(payloads),10)
        completion=next(json.loads(p) for e,p in records if p and json.loads(p).get('type')=='response.completed' and json.loads(p).get('response',{}).get('id')=='astra')
        self.assertEqual(completion['response']['usage']['output_tokens'],30)
        self.assertEqual(completion['_capture_summary']['event_count'],33)
        store=Store(self.root)
        try:
            for event,payload in self.raw:store.event(json.loads(payload),event['time'],'ws',event['direction']=='client_to_server',f"ws:ws:{event['message_index']}:{event['time']}")
            detail=packet_detail(self.root,store,'responses','astra')
            self.assertIn('Summary-only storage',detail['notice']);self.assertNotIn('PRIVATE-ASTRA',json.dumps(detail))
            detail=packet_detail(self.root,store,'responses','luna')
            self.assertNotIn('Summary-only storage',detail['notice']);self.assertIn('KEEP-LUNA',json.dumps(detail))
        finally:store.close()

    def test_response_model_overrides_requested_model_without_losing_request(self):
        original=self.ws({'type':'response.create','model':'gpt-6-astra','input':'KEEP-REQUEST'},True)[1]
        self.ws({'type':'response.created','response':{'id':'different','model':'gpt-6-luna'}})
        self.writer.close();self.assertIn(original,[p for e,p in self.records()])

    def test_ambiguous_and_unknown_requests_remain_full(self):
        one=self.ws({'type':'response.create','model':'gpt-6-astra','input':'KEEP-AMBIGUOUS'},True)[1]
        two=self.ws({'type':'response.create','model':'gpt-6-luna','input':'KEEP-LUNA'},True)[1]
        self.ws({'type':'response.created','response':{'id':'one','model':'gpt-6-astra'}})
        self.writer.close();payloads=[p for e,p in self.records()]
        self.assertIn(one,payloads);self.assertIn(two,payloads)

    def test_crash_pending_requests_are_recovered_losslessly(self):
        original=self.ws({'type':'response.create','model':'gpt-6-astra','input':'KEEP-UNRESOLVED'},True)[1]
        for queues in self.writer.pending.values():
            for queue in queues:queue.file.close()
        self.writer.pending.clear();self.writer.close()
        self.writer=SelectiveArchiveWriter(self.root,'after-crash');self.writer.close()
        self.assertIn(original,[p for e,p in self.records()]);self.assertFalse(list((self.root/'Pending').glob('*.trace')))

    def http(self,model,compressed=False):
        fid='http';ctx={'id':fid,'type':'http','url':'https://example.test/v1/responses?secret=PRIVATE',
            'request':{'method':b'POST','path':b'/v1/responses','headers':[[b'Authorization',b'PRIVATE-HEADER']]},
            'response':{'status_code':200,'headers':[[b'Content-Type',b'text/event-stream']]}}
        if compressed:ctx['response']['headers'].append([b'Content-Encoding',b'gzip'])
        request=json.dumps({'model':model,'input':'HTTP-PRIVATE-PROMPT'}).encode()
        values=[{'type':'response.created','response':{'id':'http-response','model':model,'instructions':'HTTP-PRIVATE-INSTRUCTIONS'}},
                {'type':'response.output_text.delta','response_id':'http-response','delta':'HTTP-PRIVATE-OUTPUT'},
                {'type':'response.completed','response':{'id':'http-response','model':model,'status':'completed','output':['HTTP-PRIVATE-OUTPUT'],'usage':{'total_tokens':42}}}]
        response=b''.join(b'data: '+json.dumps(v).encode()+b'\n\n' for v in values)
        if compressed:response=gzip.compress(response)
        def write(kind,payload=b'',**extra):self.writer.write(dict(kind=kind,flow_id=fid,time=1793100000.0,state=ctx,**extra),payload,ctx)
        write('http.request.headers');write('http.request.body',request,offset=0);write('http.request.end');write('http.response.headers')
        for offset in range(0,len(response),19):write('http.response.body',response[offset:offset+19],offset=offset)
        write('http.response.end');self.writer.close();return request,response

    def test_http_auto_review_gzip_sse_is_summary_only(self):
        self.http('codex-auto-review',True);records=self.records()
        self.assertNotIn(b'HTTP-PRIVATE',b''.join(p for e,p in records))
        self.assertFalse(any(e['kind'].endswith('.body') for e,p in records))
        self.assertEqual(sum(e['kind']=='http.response.summary' for e,p in records),1)
        self.assertNotIn('PRIVATE-HEADER',json.dumps([e for e,p in records]))
        response=next(json.loads(p) for e,p in records if e['kind']=='http.response.summary')
        self.assertEqual(response['response']['usage']['total_tokens'],42)

    def test_http_other_models_retain_exact_original_compressed_bytes(self):
        request,response=self.http('gpt-6-luna',True);records=self.records()
        self.assertEqual(b''.join(p for e,p in records if e['kind']=='http.request.body'),request)
        self.assertEqual(b''.join(p for e,p in records if e['kind']=='http.response.body'),response)


if __name__=='__main__':unittest.main()
