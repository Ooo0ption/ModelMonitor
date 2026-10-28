import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import cleanup_storage as cleanup
from audit_cleanup import audit
from traffic_archive import ArchiveWriter,archive_files,iter_records
from store import Store


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();root=Path(self.temp.name).resolve()
        self.source=root/'data';self.project=root/'project';self.work=root/'work'
        self.source.mkdir();self.project.mkdir()
        self.backup=self.project/'backups/capture-recovery-test'
        writer=ArchiveWriter(self.source,'old');store=Store(self.source);index=0
        for model in ('gpt-6-astra','gpt-6-luna'):
            for client,value in ((True,{'type':'response.create','model':model,'input':'PRIVATE-'+model}),
                                 (False,{'type':'response.created','response':{'id':model,'model':model}}),
                                 (False,{'type':'response.completed','response':{'id':model,'model':model,'status':'completed','output':['PRIVATE-'+model]}})):
                stamp=1793100000.0+index
                event={'kind':'websocket.message','flow_id':'shared','time':stamp,'message_index':index,
                       'direction':'client_to_server' if client else 'server_to_client'}
                writer.write(event,json.dumps(value).encode(),{'id':'shared','type':'http','websocket':{}})
                store.event(value,stamp,'shared',client,f'shared:ws:{index}:{stamp}');index+=1
        writer.close();store.close()
        shutil.copytree(self.source/'Captures',self.backup/'archive/Captures')
        (self.source/'archive-health.json').write_text(json.dumps({'ready':False,'archive_session':'old'}))
        self.cache=self.source/'Exports/2026-10-27/packet-example';self.cache.mkdir(parents=True)
        (self.cache/'preview.txt').write_text('regenerable preview')
        self.guard=mock.patch.object(cleanup,'proxy_is_running',return_value=False);self.guard.start()

    def tearDown(self):self.guard.stop();self.temp.cleanup()

    def test_validated_cleanup_preserves_counts_and_other_model_payloads(self):
        counts=cleanup.db_info(self.source);plan=cleanup.prepare(self.source,self.project,self.work)
        receipt=cleanup.apply(plan)
        payloads=b''.join(p for f in archive_files(self.source) for e,p in iter_records(f))
        self.assertIn(b'PRIVATE-gpt-6-luna',payloads);self.assertNotIn(b'PRIVATE-gpt-6-astra',payloads)
        self.assertEqual(cleanup.db_info(self.source),counts)
        self.assertFalse(self.backup.exists());self.assertFalse(self.cache.exists())
        self.assertEqual(json.loads((receipt/'transaction.json').read_text())['phase'],'complete')
        self.assertFalse((self.work/'filtered/Captures').exists())

    def test_replacement_failure_rolls_back_before_deleting_any_original(self):
        original={p.relative_to(self.source):p.read_bytes() for p in archive_files(self.source)}
        plan=cleanup.prepare(self.source,self.project,self.work)
        real=cleanup.assert_tree;counter=0
        def check(path,expected):
            nonlocal counter
            if path==self.source/'Captures':
                counter+=1
                if counter==2:raise ValueError('Synthetic replacement validation failure')
            return real(path,expected)
        with mock.patch.object(cleanup,'assert_tree',side_effect=check):
            with self.assertRaisesRegex(ValueError,'Synthetic'):cleanup.apply(plan)
        for path,payload in original.items():self.assertEqual((self.source/path).read_bytes(),payload)
        self.assertTrue(self.backup.exists());self.assertTrue(self.cache.exists())

    def test_unique_recovery_message_blocks_deletion(self):
        writer=ArchiveWriter(self.backup/'archive','unique')
        writer.write({'kind':'websocket.message','flow_id':'unique','time':1793100000.,'message_index':0,'direction':'server_to_client'},b'KEEP UNIQUE')
        writer.close()
        with self.assertRaisesRegex(ValueError,'not proven'):cleanup.prepare(self.source,self.project,self.work)
        self.assertTrue(self.backup.exists());self.assertTrue(archive_files(self.source))

    def test_recovery_http_ranges_match_across_original_chunk_boundaries(self):
        writer=ArchiveWriter(self.source,'body')
        for offset,payload in ((0,b'abcd'),(4,b'efgh')):
            writer.write({'kind':'http.response.body','flow_id':'http','time':1793100000.,'offset':offset},payload)
        writer.close()
        writer=ArchiveWriter(self.backup/'archive','body-range')
        writer.write({'kind':'http.response.body','flow_id':'http','time':1793100000.,'offset':2},b'cdef')
        writer.close()
        result=audit(self.source,[self.backup],self.work/'audit.json')
        self.assertTrue(result['safe_to_remove_recovery_copies'])
        self.assertEqual(result['verified_http_byte_ranges'],1)


if __name__=='__main__':unittest.main()
