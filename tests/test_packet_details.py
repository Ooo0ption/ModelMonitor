import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from store import Store
from traffic_archive import ArchiveWriter
from packet_details import packet_detail, MessageLinks, PREVIEW_BYTES, format_json_prefix, text_preview


class PacketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root)
        self.writer = ArchiveWriter(self.root, 'test', rotate_bytes=1000)
        self.index = 0
        self.time = 1793030396.0
        self.state = {'id': 'ws', 'url': 'https://example.test/responses',
                      'request': {'method': b'GET', 'headers': [[b'Cookie', b'test-cookie']]},
                      'response': {'status_code': 101, 'headers': []}}

    def tearDown(self):
        self.writer.close()
        self.store.close()
        self.tmp.cleanup()

    def ws(self, event, client=False):
        key = f'ws:ws:{self.index}:{self.time}'
        self.writer.write({'kind': 'websocket.message', 'flow_id': 'ws', 'time': self.time,
                           'message_index': self.index, 'direction': 'client_to_server' if client else 'server_to_client'},
                          json.dumps(event).encode(), self.state)
        self.store.event(event, self.time, 'ws', client, key)
        self.index += 1
        self.time += 1
        return key

    def test_sequential_calls_are_isolated_and_cross_midnight(self):
        a = self.ws({'type': 'response.create', 'model': 'model-a', 'input': 'prompt-a'}, True)
        self.ws({'type': 'response.created', 'response': {'id': 'resp-a', 'model': 'model-a'}})
        self.ws({'type': 'response.output_text.delta', 'response_id': 'resp-a', 'delta': 'answer-a'})
        self.ws({'type': 'response.completed', 'response': {'id': 'resp-a', 'model': 'model-a', 'status': 'completed'}})
        self.ws({'type': 'response.create', 'model': 'model-b', 'input': 'prompt-b'}, True)
        self.ws({'type': 'response.created', 'response': {'id': 'resp-b', 'model': 'model-b'}})
        self.ws({'type': 'response.output_text.delta', 'response_id': 'resp-b', 'delta': 'answer-b'})
        self.ws({'type': 'response.completed', 'response': {'id': 'resp-b', 'status': 'completed'}})
        self.writer.close()
        for mode, rid in [('requests', a), ('responses', 'resp-a')]:
            detail = packet_detail(self.root, self.store, mode, rid)
            wire = '\n'.join(m['text'] for m in detail['messages'] if m['direction'] != 'connection')
            self.assertIn('prompt-a', wire)
            self.assertIn('answer-a', wire)
            self.assertNotIn('prompt-b', wire)
            self.assertNotIn('answer-b', wire)
            self.assertEqual(len([m for m in detail['messages'] if m['direction'] == 'response']), 3)
            self.assertEqual(detail['protocol_label'], 'WebSocket')
            self.assertEqual(detail['notice'], '')
            for message in detail['messages']:
                self.assertEqual(Path(message['path']).stat().st_mode & 0o077, 0)
        other = packet_detail(self.root, self.store, 'responses', 'resp-b')
        self.assertIn('prompt-b', '\n'.join(m['text'] for m in other['messages']))

    def test_interleaved_items_do_not_leak_other_response(self):
        self.ws({'type': 'response.create', 'model': 'a', 'input': 'prompt-a'}, True)
        self.ws({'type': 'response.create', 'model': 'b', 'input': 'prompt-b'}, True)
        self.ws({'type': 'response.created', 'response': {'id': 'a', 'model': 'a'}})
        # The item belongs to B, which is not yet in the active response set.
        self.ws({'type': 'response.output_text.delta', 'item_id': 'item-b', 'delta': 'secret-b'})
        self.ws({'type': 'response.created', 'response': {'id': 'b', 'model': 'b'}})
        self.ws({'type': 'response.output_text.delta', 'item_id': 'item-a', 'delta': 'answer-a'})
        self.ws({'type': 'response.completed', 'response': {'id': 'a', 'output': [{'id': 'item-a'}]}})
        self.ws({'type': 'response.completed', 'response': {'id': 'b', 'output': [{'id': 'item-b'}]}})
        self.writer.close()
        detail = packet_detail(self.root, self.store, 'responses', 'a')
        text = '\n'.join(m['text'] for m in detail['messages'])
        self.assertIn('answer-a', text)
        self.assertNotIn('secret-b', text)
        self.assertNotIn('prompt-b', text)
        self.assertNotIn('prompt-a', text)  # Both requests were pending; pairing would be a guess.
        self.assertIn('could not be linked', detail['notice'])
        detail = packet_detail(self.root, self.store, 'responses', 'b')
        self.assertIn('secret-b', '\n'.join(m['text'] for m in detail['messages']))

    def test_legacy_metadata_only_and_flow_link_backfill(self):
        folder = self.root / 'journals'
        folder.mkdir()
        rows = [
            {'key': 'legacy-request', 'time': self.time, 'flow_id': 'legacy', 'from_client': True,
             'event': {'type': 'response.create', 'model': 'old-model'}},
            {'key': 'legacy-response', 'time': self.time + 1, 'flow_id': 'legacy', 'from_client': False,
             'event': {'type': 'response.created', 'response': {'id': 'old-response', 'model': 'old-model'}}},
        ]
        (folder / 'old.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
        for r in rows:
            self.store.event(r['event'], r['time'], r['flow_id'], r['from_client'], r['key'])
        self.store.db.execute('DELETE FROM response_flows')
        self.store.db.commit()
        detail = packet_detail(self.root, self.store, 'responses', 'old-response')
        self.assertEqual(detail['protocol_label'], 'Saved metadata')
        self.assertIn('unavailable', detail['notice'])
        self.assertEqual(len(detail['messages']), 2)
        self.assertEqual(detail['flow_ids'], ['legacy'])

    def test_idless_events_are_not_assigned_when_another_request_is_pending(self):
        links = MessageLinks()
        links.add('ws', 'request-a', 'key-a', True, {'type': 'response.create', 'model': 'a'})
        links.add('ws', 'created-a', '', False, {'type': 'response.created', 'response': {'id': 'a'}})
        links.add('ws', 'request-b', 'key-b', True, {'type': 'response.create', 'model': 'b'})
        links.add('ws', 'ambiguous', '', False, {'type': 'response.output_text.delta', 'delta': 'uncertain'})
        links.add('ws', 'unrelated', '', False, {'type': {'other': 'protocol'}, 'item_id': {'invalid': 'id'}})
        selected, _, _ = links.select('responses', 'a')
        self.assertNotIn('ambiguous', selected)
        self.assertNotIn('unrelated', selected)

    def test_http_original_body_preserved_and_gzip_preview_decoded(self):
        request = {'model': 'model-http', 'input': 'whole prompt'}
        response = {'id': 'http-response', 'object': 'response', 'model': 'model-http', 'output': 'full answer'}
        state = {'id': 'http', 'url': 'https://example.test/v1/responses',
                 'request': {'method': b'POST', 'headers': [[b'Authorization', b'Bearer test-secret']]},
                 'response': {'status_code': 200, 'headers': [[b'Content-Encoding', b'gzip'], [b'Set-Cookie', b'a'], [b'Set-Cookie', b'b']]}}
        raw = gzip.compress(json.dumps(response).encode())
        self.writer.write({'kind': 'http.request.body', 'flow_id': 'http', 'offset': 0}, json.dumps(request).encode(), state)
        self.writer.write({'kind': 'http.response.body', 'flow_id': 'http', 'offset': 0}, raw[:12], state)
        self.writer.write({'kind': 'http.response.body', 'flow_id': 'http', 'offset': 12}, raw[12:], state)
        self.writer.close()
        self.store.event(request, self.time, 'http', True, 'http:http')
        self.store.event(response, self.time, 'http', False, 'http:http')
        detail = packet_detail(self.root, self.store, 'responses', 'http-response')
        body = next(m for m in detail['messages'] if m['label'] == 'Response body')
        self.assertIn('full answer', body['text'])
        self.assertEqual(Path(body['path']).read_bytes(), raw)
        self.assertEqual(detail['protocol_label'], 'HTTP')
        self.assertIn('Bearer test-secret', '\n'.join(m['text'] for m in detail['messages']))

    def test_large_content_has_complete_file_and_bounded_preview(self):
        content = 'X' * (PREVIEW_BYTES * 2)
        self.ws({'type': 'response.create', 'model': 'a', 'input': content}, True)
        self.ws({'type': 'response.created', 'response': {'id': 'large', 'model': 'a'}})
        self.writer.close()
        detail = packet_detail(self.root, self.store, 'responses', 'large')
        message = next(m for m in detail['messages'] if m['direction'] == 'request')
        self.assertTrue(message['truncated'])
        self.assertLessEqual(len(message['text']), PREVIEW_BYTES)
        self.assertTrue(message['text'].startswith('{\n  "type": "response.create",\n'))
        self.assertEqual(json.loads(Path(message['path']).read_text())['input'], content)

    def test_truncated_nested_response_is_indented_without_changing_strings(self):
        obj = {'type': 'response.in_progress', 'response': {'id': 'r', 'items': [1, {}, [], {'text': 'quote " comma, brace } slash \\ newline\n中文'}], 'large': 'Z' * PREVIEW_BYTES}}
        raw = json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode()
        text, shortened = text_preview(raw[:PREVIEW_BYTES + 1], True, len(raw))
        self.assertTrue(shortened)
        self.assertIn('\n  "response": {\n    "id": "r",\n', text)
        self.assertIn('quote \\" comma, brace } slash \\\\ newline\\n中文', text)
        self.assertLessEqual(len(text.encode()), PREVIEW_BYTES)

    def test_json_formatting_preserves_valid_values_and_empty_containers(self):
        obj = {'a': [{}, [], {'x': [1, True, None, 'a",b}c\\d']}], 'text': 'hello\nworld'}
        text, shortened = format_json_prefix(json.dumps(obj, separators=(',', ':')))
        self.assertFalse(shortened)
        self.assertEqual(json.loads(text), obj)
        self.assertIn('{}', text)
        self.assertIn('[]', text)
        repeated, _ = format_json_prefix('{"x":1,"x":2}')
        self.assertEqual(repeated.count('"x"'), 2)

    def test_indentation_expansion_is_bounded_and_reported(self):
        raw = json.dumps([0] * 20000, separators=(',', ':')).encode()
        self.assertLess(len(raw), PREVIEW_BYTES)
        text, shortened = text_preview(raw, False, len(raw))
        self.assertTrue(shortened)
        self.assertLessEqual(len(text.encode()), PREVIEW_BYTES)

    def test_utf8_cut_does_not_turn_text_into_binary(self):
        raw = b'{"text":"' + b'a' * (PREVIEW_BYTES - 10) + '中'.encode() + b'"}'
        text, shortened = text_preview(raw[:PREVIEW_BYTES + 1], True, len(raw))
        self.assertTrue(shortened)
        self.assertTrue(text.startswith('{\n  "text": "'))
        self.assertNotIn('Binary content', text)

    def test_controller_detail_and_status_json_contract(self):
        self.ws({'type': 'response.create', 'model': 'model-cli', 'input': 'private CLI test prompt'}, True)
        self.ws({'type': 'response.created', 'response': {'id': 'cli-response', 'model': 'model-cli'}})
        self.writer.close()
        script = Path(__file__).resolve().parents[1] / 'src/controller.py'
        env = dict(os.environ, MODELSCOPE_DATA_DIR=str(self.root),
                   MODELSCOPE_LOCATION_FILE=str(self.root / 'location.json'), PYTHONDONTWRITEBYTECODE='1')
        result = subprocess.run([sys.executable, str(script), 'packet-detail', '--mode', 'responses', '--id', 'cli-response'],
                                env=env, check=True, capture_output=True, text=True)
        detail = json.loads(result.stdout)
        self.assertTrue(detail['ok'])
        self.assertEqual(detail['record']['id'], 'cli-response')
        self.assertEqual(detail['record']['model'], 'model-cli')
        self.assertTrue(detail['record']['time'])
        for message in detail['messages']:
            self.assertTrue(all(key in message for key in ('id', 'label', 'direction', 'time', 'byte_count', 'text', 'truncated', 'path')))
        status = subprocess.run([sys.executable, str(script), 'status'], env=env, check=True, capture_output=True, text=True)
        snapshot = json.loads(status.stdout)
        self.assertEqual(snapshot['capture_issues'], [])
        self.assertNotIn('tls_warning', snapshot)


if __name__ == '__main__':
    unittest.main()
