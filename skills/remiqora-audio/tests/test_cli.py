import base64
import io
import struct
import time
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs
import wave

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/remiqora_audio.py'
with io.BytesIO() as buffer:
    with wave.open(buffer, 'wb') as audio:
        audio.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        audio.writeframes(b'\x01\x00' * 8000)
    WAV = buffer.getvalue()

FLOAT_WAV = (b'RIFF' + struct.pack('<I', 36 + 4 * 8000) + b'WAVEfmt ' +
             struct.pack('<IHHIIHH', 16, 3, 1, 8000, 32000, 4, 32) + b'data' +
             struct.pack('<I', 4 * 8000) + struct.pack('<f', 0.1) * 8000)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, data, status=200):
        body = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        api = self.server
        api.calls.append(('GET', self.path))
        if self.path == '/api/system/features':
            self.reply({'stable_audio_models': ['small-sfx', 'small-music-base'], 'ace_step': True, 'yue2_precisions': ['q8_0', 'q4_0']})
        elif self.path == '/api/engines':
            self.reply({'engines': {'ace_step': {'choices': {'dit_model': ['acestep-v15-turbo', 'acestep-v15-base']}}}})
        elif self.path == '/api/ace/v1/model_inventory':
            self.reply({'code': 200, 'data': {'models': [{'name': 'acestep-v15-turbo'}, {'name': 'acestep-v15-base'}]}})
        elif self.path == '/api/orchestrator/config':
            self.reply({'yue2_specs': {'yue2': {'id': 'yue2', 'path': '/models', 'family': 'yue2', 'task': 'gen', 'mode': 'offline'}}})
        elif self.path.startswith('/api/ace/v1/audio?'):
            self.reply({'bad-audio': b'not a wav', 'truncated-audio': FLOAT_WAV[:44]}.get(api.mode, FLOAT_WAV))
        elif self.path == '/api/orchestrator/status':
            active = 'yue2' if api.mode == 'other-engine' else None
            self.reply({'active_model': active, 'models': {'yue2': {'status': 'running' if active else 'stopped'}}})
        elif self.path == '/api/sa3/v1/models':
            self.reply([{'id': name, 'available': True, 'base': name.endswith('-base'), 'max_seconds': 120}
                        for name in ('small-sfx', 'small-music-base')])
        elif self.path.startswith('/api/sa3/v1/jobs/'):
            if api.mode == 'timeout':
                self.reply({'status': 'running'})
            elif api.mode == 'failure':
                api.active_job = None
                self.reply({'status': 'failed', 'error': 'generation failed'})
            else:
                api.active_job = None
                self.reply({'status': 'done', 'files': ['/v1/files/test.wav']})
        elif self.path == '/api/sa3/v1/files/test.wav':
            self.reply({'bad-audio': b'not a wav', 'truncated-audio': WAV[:44], 'empty-audio': b''}.get(api.mode, WAV))
        else:
            self.reply({'error': 'unknown path'}, 404)

    def do_POST(self):
        api = self.server
        body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        api.calls.append(('POST', self.path))
        if self.path == '/api/ace/release_task':
            api.submitted.append(json.loads(body))
            self.reply({'code': 200, 'data': {'task_id': 'ace-1'}})
        elif self.path == '/api/ace/query_result':
            status = {'timeout': 0, 'failure': 2}.get(api.mode, 1)
            self.reply({'code': 200, 'data': [{'status': status, 'result': json.dumps([
                {'file': '/v1/audio?path=test.wav', 'error': 'generation failed' if status == 2 else None}])}]})
        elif self.path == '/api/ace/cancel_task':
            api.submitted.append(json.loads(body))
            self.reply({'code': 200, 'data': {}})
        elif self.path == '/api/yue2/v1/models/load':
            api.submitted.append(json.loads(body))
            self.reply({})
        elif self.path == '/api/yue2/v1/tasks/run':
            api.submitted.append(json.loads(body))
            if api.mode == 'timeout':
                time.sleep(0.8)
            try:
                self.reply({'audio': 'bad!' if api.mode == 'bad-audio' else base64.b64encode(WAV).decode()})
            except BrokenPipeError:
                pass
        elif self.path.endswith('/cancel'):
            api.active_job = None
            self.reply({})
        elif self.path == '/api/orchestrator/switch':
            self.reply({})
        elif self.path == '/api/sa3/v1/jobs':
            if api.active_job is not None:
                self.reply({'error': 'overlapping job'}, 409)
                return
            api.submitted.append(parse_qs(body.decode()))
            api.active_job = str(len(api.submitted))
            self.reply({'job_id': api.active_job, 'status': 'queued'})
        else:
            self.reply({'error': 'unknown path'}, 404)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.mode = 'success'
        self.server.calls = []
        self.server.submitted = []
        self.server.active_job = None
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()

    def command(self, output='assets/test.wav', *extra):
        return [sys.executable, str(SCRIPT), '--model', 'small-music-base', '--prompt', 'Warm synths, no vocals',
                '--duration', '1', '--output', str(self.root / output),
                '--url', f'http://127.0.0.1:{self.server.server_port}', *extra]

    def run_cli(self, output='assets/test.wav', *extra):
        return subprocess.run(self.command(output, *extra), capture_output=True, text=True, timeout=15)

    def test_waits_and_saves_a_valid_wav_with_base_model_settings(self):
        result = self.run_cli('assets/space name.wav')
        self.assertEqual(0, result.returncode, result.stderr)
        target = self.root / 'assets/space name.wav'
        self.assertEqual(WAV, target.read_bytes())
        self.assertEqual(str(target), result.stdout.strip())
        self.assertEqual(['50'], self.server.submitted[0]['steps'])
        self.assertEqual(['7'], self.server.submitted[0]['cfg_scale'])
        self.assertEqual(['1.0'], self.server.submitted[0]['duration'])

    def test_existing_output_is_preserved_without_api_requests(self):
        target = self.root / 'existing.wav'
        target.write_bytes(b'keep me')
        result = self.run_cli('existing.wav')
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(b'keep me', target.read_bytes())
        self.assertEqual([], self.server.calls)

    def test_other_engine_is_not_stopped(self):
        self.server.mode = 'other-engine'
        result = self.run_cli()
        self.assertNotEqual(0, result.returncode)
        self.assertIn('yue2 is active', result.stderr)
        self.assertFalse(any(method == 'POST' for method, _ in self.server.calls))

    def test_failed_generation_does_not_create_output(self):
        self.server.mode = 'failure'
        result = self.run_cli()
        self.assertNotEqual(0, result.returncode)
        self.assertIn('generation failed', result.stderr)
        self.assertFalse((self.root / 'assets/test.wav').exists())

    def test_timeout_cancels_its_job(self):
        self.server.mode = 'timeout'
        result = self.run_cli('timeout.wav', '--timeout', '0.3')
        self.assertNotEqual(0, result.returncode)
        self.assertIn('timed out', result.stderr)
        self.assertIn(('POST', '/api/sa3/v1/jobs/1/cancel'), self.server.calls)
        self.assertFalse((self.root / 'timeout.wav').exists())

    def test_invalid_audio_leaves_no_partial_file(self):
        for mode in ('bad-audio', 'truncated-audio', 'empty-audio'):
            with self.subTest(mode=mode):
                self.server.mode = mode
                result = self.run_cli()
                self.assertNotEqual(0, result.returncode)
                self.assertNotIn('Traceback', result.stderr)
                self.assertEqual([], list((self.root / 'assets').iterdir()))

    def test_two_processes_generate_in_sequence(self):
        first = subprocess.Popen(self.command('first.wav'), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        second = subprocess.Popen(self.command('second.wav'), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for process in (first, second):
            _, errors = process.communicate(timeout=15)
            self.assertEqual(0, process.returncode, errors)
        self.assertEqual(2, len(self.server.submitted))
        self.assertEqual(WAV, (self.root / 'first.wav').read_bytes())
        self.assertEqual(WAV, (self.root / 'second.wav').read_bytes())

    def test_list_models_is_read_only(self):
        result = subprocess.run([sys.executable, str(SCRIPT), '--list-models', '--url',
                                 f'http://127.0.0.1:{self.server.server_port}'], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        for model in ('small-sfx', 'acestep-v15-base', 'yue2-q4_0'):
            self.assertIn(model, result.stdout)
        self.assertFalse(any(method == 'POST' for method, _ in self.server.calls))

    def test_explicit_switch_allows_other_engine(self):
        self.server.mode = 'other-engine'
        result = self.run_cli('switch.wav', '--switch-engine')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(('POST', '/api/orchestrator/switch'), self.server.calls)

    def test_missing_model_and_invalid_options_do_not_switch(self):
        for options in (('--model', 'missing'), ('--lyrics', 'words'),
                        ('--model', 'acestep-v15-base', '--duration', '1'),
                        ('--model', 'yue2-q8_0', '--duration', '361')):
            with self.subTest(options=options):
                self.server.calls.clear()
                result = self.run_cli('invalid.wav', *options)
                self.assertNotEqual(0, result.returncode)
                self.assertFalse(any(method == 'POST' for method, _ in self.server.calls))

    def test_ace_lyrics_model_and_float_wav_conversion(self):
        for model, steps in (('acestep-v15-turbo', 8), ('acestep-v15-base', 50)):
            result = self.run_cli(model + '.wav', '--model', model, '--duration', '10', '--lyrics', '[Verse]\nHello')
            self.assertEqual(0, result.returncode, result.stderr)
            submitted = self.server.submitted[-1]
            self.assertEqual(model, submitted['model'])
            self.assertEqual(steps, submitted['inference_steps'])
            self.assertEqual('[Verse]\nHello', submitted['lyrics'])
            with wave.open(str(self.root / (model + '.wav')), 'rb') as audio:
                self.assertEqual(2, audio.getsampwidth())
                self.assertEqual(8000, audio.getnframes())

    def test_ace_invalid_audio_is_not_published(self):
        for mode in ('bad-audio', 'truncated-audio'):
            self.server.mode = mode
            result = self.run_cli('ace-bad.wav', '--model', 'acestep-v15-turbo', '--duration', '10')
            self.assertNotEqual(0, result.returncode)
            self.assertFalse((self.root / 'ace-bad.wav').exists())
            self.assertEqual([], list(self.root.glob('.remiqora-*')))

    def test_ace_timeout_cancels_only_own_task_and_failure_is_reported(self):
        for mode in ('timeout', 'failure'):
            self.server.mode = mode
            self.server.calls.clear()
            result = self.run_cli('ace.wav', '--model', 'acestep-v15-turbo', '--duration', '10',
                                  '--timeout', '0.3' if mode == 'timeout' else '5')
            self.assertNotEqual(0, result.returncode)
            self.assertFalse((self.root / 'ace.wav').exists())
            if mode == 'timeout':
                self.assertIn(('POST', '/api/ace/cancel_task'), self.server.calls)
                self.assertEqual({'task_id': 'ace-1'}, self.server.submitted[-1])
            else:
                self.assertIn('generation failed', result.stderr)

    def test_yue_precision_lyrics_duration_cap_and_early_end(self):
        for precision, duration in (('q4_0', '0.5'), ('q8_0', '2')):
            result = self.run_cli(precision + '.wav', '--model', 'yue2-' + precision, '--duration', duration,
                                  '--lyrics', '[Verse] Hello')
            self.assertEqual(0, result.returncode, result.stderr)
            load, task = self.server.submitted[-2:]
            self.assertEqual({'yue2.model_gguf': f'yue2-3b-{precision}.gguf'}, load['session_options'])
            self.assertEqual('[Verse] Hello', task['request']['lyrics'])
            self.assertIsInstance(task['request']['seed'], int)
            self.assertTrue(0 <= task['request']['seed'] < 2**31)
            self.assertEqual(int(float(duration) * 25 + 0.5), task['request']['options']['semantic_max_tokens'])
            with wave.open(str(self.root / (precision + '.wav')), 'rb') as audio:
                self.assertEqual(4000 if duration == '0.5' else 8000, audio.getnframes())

    def test_yue_timeout_warns_without_stopping_and_bad_base64_fails(self):
        for mode in ('timeout', 'bad-audio'):
            self.server.mode = mode
            self.server.calls.clear()
            result = self.run_cli('yue.wav', '--model', 'yue2-q8_0', '--timeout', '0.3')
            self.assertNotEqual(0, result.returncode)
            self.assertNotIn('Traceback', result.stderr)
            self.assertFalse((self.root / 'yue.wav').exists())
            if mode == 'timeout':
                self.assertIn('may continue', result.stderr)
            self.assertFalse(any('cancel' in path or 'stop' in path for _, path in self.server.calls))


if __name__ == '__main__':
    unittest.main()
