#!/usr/bin/env python3
"""Restart the managed proxy with the current retention rules; preserve history."""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import controller
from retention import POLICY


def activate():
    settings = controller.config()
    state = controller.rpc('status')
    health_path = controller.DATA / 'archive-health.json'
    health = json.loads(health_path.read_text()) if health_path.exists() else {}
    if state.get('running'):
        ready, error = controller.recorder_status(settings, state)
        if not ready:
            raise RuntimeError(error + ' Preserve retained flows with the restore tool before restarting.')
        if health.get('storage_policy') == POLICY:
            print('The current storage rules are already active. No restart needed.')
            return state
        # Validate files before disturbing the working proxy. Keep the same ports
        # so existing clients can reconnect without a configuration change.
        settings = dict(settings, proxy_port=state['proxy_port'], web_port=state['web_port'])
        controller.proxy_command(settings, state['proxy_port'], state['web_port'])
        for name in controller.CAPTURE_FILES:
            if not (controller.ROOT / name).is_file():
                raise RuntimeError('Missing recorder file: ' + name)
        print('Restarting the managed proxy on its current ports...', flush=True)
        controller.rpc('stop')
        for _ in range(150):
            if not controller.rpc('status').get('running') and all(controller.available(state[k]) for k in ('proxy_port', 'web_port')):
                break
            time.sleep(.1)
        else:
            raise RuntimeError('The old proxy has not released its ports. Retry after it finishes stopping.')
    fresh = controller.start(settings)
    if state.get('running') and fresh['proxy_port'] != state['proxy_port']:
        raise RuntimeError('The original port became occupied. Relaunch the target through Model Monitor.')
    for _ in range(30):
        health = json.loads(health_path.read_text()) if health_path.exists() else {}
        ready, error = controller.recorder_status(settings, fresh)
        if ready and health.get('storage_policy') == POLICY:
            print('Active: Astra and codex-auto-review summaries; all other and unclassified traffic in full.')
            print('New archives use lossless gzip compression. Existing archives and backups are unchanged.')
            print('Data folder: ' + str(controller.DATA))
            return fresh
        time.sleep(.2)
    raise RuntimeError(error or 'The new recorder policy could not be verified. Check Model Monitor before continuing.')


if __name__ == '__main__':
    try:
        activate()
    except Exception as error:
        print('Activation did not finish: ' + str(error), file=sys.stderr)
        sys.exit(1)
