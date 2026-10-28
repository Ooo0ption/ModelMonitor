import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from compact_captures import prepare
from traffic_archive import ArchiveWriter, MAGIC, HEADER, archive_files, iter_records


class CompactionTests(unittest.TestCase):
    def test_closed_archives_are_copied_and_other_model_bytes_survive(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root / 'source'; out = root / 'filtered'
            writer = ArchiveWriter(source, 'old')
            originals = []; index = 0
            for model in ('gpt-6-astra', 'gpt-6-luna'):
                for client, value in ((True, {'type':'response.create','model':model,'input':model+'-PRIVATE'}),
                                      (False, {'type':'response.created','response':{'id':model,'model':model}}),
                                      (False, {'type':'response.completed','response':{'id':model,'model':model,'output':[model+'-PRIVATE']}})):
                    payload = json.dumps(value).encode()
                    event = {'kind':'websocket.message','flow_id':'shared','message_index':index,'time':1793100000+index,
                             'direction':'client_to_server' if client else 'server_to_client'}
                    writer.write(event, payload, {'id':'shared','type':'http','websocket':{}})
                    if model == 'gpt-6-luna': originals.append(payload)
                    index += 1
            writer.close()
            before = {p:hashlib.sha256(p.read_bytes()).hexdigest() for p in archive_files(source)}
            active = ArchiveWriter(source, 'live'); active.write({'kind':'session.start'}, b'LIVE'); active.close()
            (source/'archive-health.json').write_text(json.dumps({'archive_session':'live'}))
            report = prepare(source, out)
            self.assertEqual(report['protected_payloads_verified'], 3)
            self.assertEqual(report['excluded_active_session'], 'live')
            payloads = [p for f in archive_files(out) for e,p in iter_records(f)]
            self.assertTrue(all(p in payloads for p in originals))
            self.assertNotIn(b'gpt-6-astra-PRIVATE', b''.join(payloads))
            self.assertNotIn(b'LIVE', payloads)
            for path, digest in before.items(): self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
            again = root/'again'; prepare(out, again)
            again_payloads = [p for f in archive_files(again) for e,p in iter_records(f)]
            self.assertTrue(all(p in again_payloads for p in originals))
            self.assertEqual([p for p in payloads if p], [p for p in again_payloads if p])

    def test_partial_gzip_record_header_is_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'partial.trace.gz'
            compressor = zlib.compressobj(wbits=31)
            path.write_bytes(compressor.compress(MAGIC + HEADER.pack(1, 1)[:5]) + compressor.flush(zlib.Z_SYNC_FLUSH))
            warnings = []
            self.assertEqual(list(iter_records(path, warnings)), [])
            self.assertTrue(warnings)


if __name__ == '__main__': unittest.main()
