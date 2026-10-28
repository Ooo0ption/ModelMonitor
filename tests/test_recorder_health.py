import importlib.util
import json
import os
import socket
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

SRC=Path(__file__).resolve().parents[1]/'src'
sys.path.insert(0,str(SRC))
import controller
from traffic_archive import archive_files, iter_records


class RecorderHealthTests(unittest.TestCase):
    def test_restart_reuses_a_closed_port_but_not_a_live_listener(self):
        listener=socket.socket()
        listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        listener.bind(('127.0.0.1',0));listener.listen()
        port=listener.getsockname()[1]
        self.assertFalse(controller.available(port))
        client=socket.create_connection(('127.0.0.1',port))
        accepted,_=listener.accept();accepted.close()
        client.recv(1);client.close();listener.close()
        self.assertTrue(controller.available(port))

    def test_reload_in_same_proxy_keeps_old_archives(self):
        ctx=types.SimpleNamespace(options=types.SimpleNamespace(update=lambda **_:None))
        with mock.patch.dict(sys.modules,{'mitmproxy':types.SimpleNamespace(ctx=ctx)}):
            spec=importlib.util.spec_from_file_location('isolated_full_capture',SRC/'full_capture.py')
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ,MODELSCOPE_DATA_DIR=folder,MODELSCOPE_CAPTURE_SESSION='same-worker'):
            first=module.FullCapture();first.running();first.done()
            original=archive_files(folder)[0].read_bytes()
            second=module.FullCapture();second.running();second.done()
            files=archive_files(folder)
            self.assertEqual(len(files),2)
            self.assertIn(original,[path.read_bytes() for path in files])
            self.assertNotEqual(first.writer.session,second.writer.session)
            self.assertEqual(json.loads((Path(folder)/'archive-health.json').read_text())['session'],'same-worker')

    def test_removed_script_is_not_reported_as_recording(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(controller,'DATA',Path(folder)):
            (Path(folder)/'archive-health.json').write_text(json.dumps({'ready':True,'session':'worker'}))
            response=mock.MagicMock();response.__enter__.return_value.read.return_value=b'{"scripts":{"value":[]}}'
            opener=mock.Mock();opener.open.return_value=response
            with mock.patch.object(controller.urllib.request,'build_opener',return_value=opener):
                recording,error=controller.recorder_status({'web_token':'synthetic'},{'running':True,'web_port':18832})
            self.assertFalse(recording);self.assertIn('removed',error)

    def test_runtime_snapshot_survives_source_replacement(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'source';source.mkdir()
            for name in controller.CAPTURE_FILES:(source/name).write_text('original')
            with mock.patch.object(controller,'DATA',root/'data'),mock.patch.object(controller,'ROOT',source):
                script=controller.snapshot_capture_runtime('worker')
            (source/'capture_addon.py').unlink()
            self.assertEqual(script.read_text(),'original')
            self.assertEqual(script.parent.stat().st_mode & 0o777,0o700)


if __name__=='__main__':unittest.main()
