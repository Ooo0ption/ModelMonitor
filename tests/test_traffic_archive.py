import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from traffic_archive import ArchiveWriter, archive_files, export_day, iter_records


class ArchiveTests(unittest.TestCase):
    def test_cross_day_rotation_binary_and_interrupted_tail(self):
        with tempfile.TemporaryDirectory() as folder:
            os.umask(0o077)
            root = Path(folder)
            writer = ArchiveWriter(root, 'test', rotate_bytes=150)
            before = 1793030399.0  # 2026-10-26 23:59:59 Hong Kong
            after = before + 2
            context = {'id': 'flow', 'url': 'https://example.test/stream', 'type': 'http',
                       'request': {'method': b'POST', 'headers': [[b'Cookie', b'private=test']]},
                       'response': {'status_code': 200, 'headers': []}}
            writer.write({'kind': 'http.response.body', 'flow_id': 'flow', 'time': before, 'offset': 0}, b'abc\x00', context)
            writer.write({'kind': 'http.response.body', 'flow_id': 'flow', 'time': after, 'offset': 4}, b'\xffend', context)
            writer.write({'kind': 'http.response.end', 'flow_id': 'flow', 'time': after, 'state': context}, context=context)
            writer.close()
            files = archive_files(root)
            self.assertGreaterEqual(len(files), 3)
            with files[-1].open('ab') as stream:
                stream.write(b'partial')
            warnings = []
            events = [r for p in files for r in iter_records(p, warnings)]
            self.assertTrue(warnings)
            self.assertEqual(b''.join(b for e, b in events if e['kind'].endswith('.body')), b'abc\x00\xffend')
            result = export_day(root, '2026-10-27')
            bodies = list(Path(result['path']).glob('*/response.body.bin'))
            self.assertEqual(len(bodies), 1)
            self.assertEqual(bodies[0].read_bytes(), b'abc\x00\xffend')
            self.assertTrue(result['warnings'])
            self.assertTrue(all(p.stat().st_mode & 0o077 == 0 for p in files))

    def test_checksum_detects_corruption(self):
        with tempfile.TemporaryDirectory() as folder:
            writer = ArchiveWriter(folder, 'integrity')
            writer.write({'kind': 'websocket.message'}, b'content')
            writer.close()
            path = archive_files(folder)[0]
            data = bytearray(path.read_bytes())
            data[-1] ^= 1
            path.write_bytes(data)
            with self.assertRaisesRegex(ValueError, 'checksum'):
                list(iter_records(path))

    def test_compressed_archives_remain_readable_while_recording(self):
        with tempfile.TemporaryDirectory() as folder:
            writer=ArchiveWriter(folder,'compressed',compress=True)
            body=b'lossless payload '*10000
            writer.write({'kind':'http.response.body','offset':0},body)
            path=archive_files(folder)[0]
            warnings=[]
            records=list(iter_records(path,warnings))
            self.assertEqual(records[0][1],body)
            self.assertFalse(warnings)  # Complete records do not require a final gzip footer.
            writer.close()
            warnings=[];records=list(iter_records(path,warnings))
            self.assertEqual(records[0][1],body);self.assertFalse(warnings)
            self.assertLess(path.stat().st_size,len(body)//5)
            with path.open('r+b') as stream:
                stream.seek(-8,2);stream.write(b'broken!!')
            with self.assertRaisesRegex(ValueError,'checksum'):list(iter_records(path))


if __name__ == '__main__':
    unittest.main()
