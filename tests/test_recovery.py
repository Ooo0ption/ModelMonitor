import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from recover_archive import recover_archive
from traffic_archive import ArchiveWriter,archive_files,iter_records


def encode(value):
    if isinstance(value,dict):data=b''.join(encode(k)+encode(v) for k,v in value.items());tag=b'}'
    elif isinstance(value,list):data=b''.join(encode(v) for v in value);tag=b']'
    elif isinstance(value,bool):data=b'true' if value else b'false';tag=b'!'
    elif isinstance(value,int):data=str(value).encode();tag=b'#'
    elif isinstance(value,float):data=str(value).encode();tag=b'^'
    elif isinstance(value,str):data=value.encode();tag=b';'
    elif isinstance(value,bytes):data=value;tag=b','
    elif value is None:data=b'';tag=b'~'
    else:raise TypeError(type(value))
    return str(len(data)).encode()+b':'+data+tag


class RecoveryTests(unittest.TestCase):
    def test_only_missing_bytes_and_messages_are_recovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);active=root/'active';stamp=1793097730.0
            writer=ArchiveWriter(active,'original')
            writer.write({'kind':'http.response.body','flow_id':'http','time':stamp,'offset':0},b'ab')
            writer.write({'kind':'websocket.message','flow_id':'ws','time':stamp,'message_index':0},b'old')
            writer.close()
            flows=[{'id':'http','type':'http','timestamp_created':stamp,'request':{'scheme':'http','host':'example.test','path':'/test'},
                    'response':{'content':b'abcdef','timestamp_start':stamp,'timestamp_end':stamp+2}},
                   {'id':'ws','type':'http','timestamp_created':stamp,'websocket':{'messages':[[1,False,b'old',stamp],[1,False,b'new',stamp+1]]}}]
            snapshot=root/'snapshot.mitm';snapshot.write_bytes(b''.join(encode(f) for f in flows))
            meta={'path':str(snapshot),'captured_at':stamp+3}
            report=recover_archive(meta,active,root/'restored')
            self.assertEqual(report['warnings'],[])
            records=[item for p in archive_files(root/'restored') for item in iter_records(p)]
            self.assertEqual([payload for event,payload in records if event['kind']=='http.response.body'],[b'cdef'])
            self.assertEqual([payload for event,payload in records if event['kind']=='websocket.message'],[b'new'])
            shutil.copytree(root/'restored/Captures',active/'Captures',dirs_exist_ok=True)
            again=recover_archive(meta,active,root/'again')
            self.assertEqual(again['counts'].get('http.response.body',0),0)
            self.assertEqual(again['counts'].get('websocket.message',0),0)

    def test_different_body_prefix_is_preserved_without_appending(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);active=root/'active';stamp=1793097730.0
            writer=ArchiveWriter(active,'original')
            writer.write({'kind':'http.response.body','flow_id':'flow','time':stamp,'offset':0},b'other');writer.close()
            snapshot=root/'snapshot.mitm';snapshot.write_bytes(encode({'id':'flow','type':'http','timestamp_created':stamp,'response':{'content':b'abcdef'}}))
            result=recover_archive({'path':str(snapshot),'captured_at':stamp},active,root/'restored')
            self.assertEqual(len(result['warnings']),1)
            self.assertEqual(result['counts'].get('http.response.body',0),0)
            self.assertEqual(next(iter_records(archive_files(active)[0]))[1],b'other')


if __name__=='__main__':unittest.main()
