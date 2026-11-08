#!/usr/bin/env python3
"""Generate a WAV through Remiqora's installed Stable Audio, ACE-Step or YuE2 models."""
import argparse
import base64
import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import wave


def positive_seconds(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


class Client:
    def __init__(self, args):
        self.base = args.url.rstrip('/')
        self.deadline = time.monotonic() + args.timeout

    def remaining(self):
        seconds = self.deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError("generation timed out")
        return seconds

    def request(self, path, data=None, *, form=False, timeout=None):
        headers = {}
        body = None
        if data is not None:
            body = (urllib.parse.urlencode(data) if form else json.dumps(data)).encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
        req = urllib.request.Request(self.base + path, data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=min(timeout or self.remaining(), self.remaining())) as response:
            result = json.load(response)
        if isinstance(result, dict) and 'code' in result and 'data' in result:
            if result.get('error') or result['code'] != 200:
                raise ValueError(result.get('error') or f"engine error {result['code']}")
            return result['data']
        return result

    def pause(self):
        time.sleep(min(2, self.remaining()))

    def cancel(self, path, data):
        with contextlib.suppress(Exception):
            req = urllib.request.Request(self.base + path, data=json.dumps(data).encode(),
                                         headers={'Content-Type': 'application/json'})
            urllib.request.urlopen(req, timeout=5).close()


def installed_models(client):
    features = client.request('/api/system/features')
    models = {name: 'stable_audio' for name in features.get('stable_audio_models', [])}
    if features.get('ace_step'):
        engines = client.request('/api/engines')
        for name in engines['engines']['ace_step']['choices']['dit_model']:
            models[name] = 'ace_step'
    for precision in features.get('yue2_precisions', []):
        models[f'yue2-{precision}'] = 'yue2'
    return models


def stable_audio(client, args):
    model = next((m for m in client.request('/api/sa3/v1/models') if m['id'] == args.model), None)
    if not model or not model['available']:
        raise ValueError(f"model '{args.model}' cannot run on this machine")
    if not 0.5 <= args.duration <= model['max_seconds']:
        raise ValueError(f"duration must be 0.5..{model['max_seconds']} seconds for {args.model}")
    job = client.request('/api/sa3/v1/jobs', {
        'task': 'text', 'model': args.model, 'prompt': args.prompt,
        'duration': args.duration, 'batch_size': 1,
        'steps': 50 if model['base'] else 8, 'cfg_scale': 7 if model['base'] else 1,
    }, form=True)
    path = f"/api/sa3/v1/jobs/{urllib.parse.quote(job['job_id'], safe='')}"
    print(f"Remiqora job {job['job_id']}", file=sys.stderr)
    try:
        while job['status'] in ('queued', 'running'):
            client.pause()
            job = client.request(path)
        if job['status'] != 'done':
            raise ValueError(job.get('error') or f"job {job['status']}")
        file = job['files'][0]
        if not file.startswith('/v1/files/'):
            raise ValueError('the server returned an unexpected audio URL')
        return '/api/sa3' + file
    except BaseException:
        if job['status'] in ('queued', 'running'):
            client.cancel(path + '/cancel', {})
        raise


def ace_step(client, args):
    inventory = client.request('/api/ace/v1/model_inventory')
    if args.model not in [model['name'] for model in inventory['models']]:
        raise ValueError(f"ACE-Step cannot load {args.model}")
    job = client.request('/api/ace/release_task', {
        'model': args.model, 'prompt': args.prompt, 'lyrics': args.lyrics or '',
        'audio_duration': args.duration, 'audio_format': 'wav', 'batch_size': 1,
        'inference_steps': 8 if 'turbo' in args.model else 50,
    })
    task_id = job['task_id']
    print(f'Remiqora job {task_id}', file=sys.stderr)
    status = 0
    try:
        while status == 0:
            client.pause()
            result = client.request('/api/ace/query_result', {'task_id_list': [task_id]})[0]
            status = result['status']
        records = json.loads(result['result'])
        if status != 1:
            raise ValueError(records[0].get('error') or 'ACE-Step generation failed')
        file = records[0]['file']
        if not file.startswith('/v1/audio?'):
            raise ValueError('the server returned an unexpected audio URL')
        return '/api/ace' + file
    except BaseException:
        if status == 0:
            client.cancel('/api/ace/cancel_task', {'task_id': task_id})
        raise


def yue2(client, args):
    spec = client.request('/api/orchestrator/config')['yue2_specs']['yue2']
    client.request('/api/yue2/v1/models/load', {
        **spec, 'load_options': {},
        'session_options': {'yue2.model_gguf': f"yue2-3b-{args.model.removeprefix('yue2-')}.gguf"},
    })
    print('Remiqora YuE2 generation (duration is an upper bound)', file=sys.stderr)
    try:
        result = client.request('/api/yue2/v1/tasks/run', {
            'model': spec['id'], 'request': {
                'lyrics': args.lyrics or '[Instrumental]', 'seed': secrets.randbits(31),
                'options': {'style': args.prompt, 'cot': 'off', 'semantic_min_tokens': 0,
                            'semantic_max_tokens': math.ceil(args.duration * 25), 'num_inference_steps': 8},
            },
        })
    except BaseException:
        print('YuE2 has no per-job cancellation API; server computation may continue.', file=sys.stderr)
        raise
    return base64.b64decode(result['audio'], validate=True)


def validate_wav(path, client):
    with wave.open(str(path), 'rb') as audio:
        expected = audio.getnframes() * audio.getnchannels() * audio.getsampwidth()
        received = 0
        while frames := audio.readframes(65536):
            client.remaining()
            received += len(frames)
        if expected == 0 or received != expected:
            raise ValueError('the server returned an empty or incomplete WAV')
        return audio.getnframes() / audio.getframerate()


def save_audio(client, args, engine, source, output):
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent, prefix='.remiqora-') as directory:
        raw = Path(directory) / 'raw.wav'
        if isinstance(source, bytes):
            raw.write_bytes(source)
        else:
            with urllib.request.urlopen(client.base + source, timeout=client.remaining()) as response, raw.open('wb') as target:
                while chunk := response.read1(65536):
                    client.remaining()
                    target.write(chunk)
        ready = raw
        if engine == 'ace_step':
            ready = Path(directory) / 'pcm.wav'
            subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-xerror',
                            '-err_detect', 'explode', '-i', str(raw), '-t', str(args.duration),
                            '-c:a', 'pcm_s16le', str(ready)], check=True, timeout=client.remaining())
        seconds = validate_wav(ready, client)
        if engine == 'yue2' and seconds > args.duration:
            ready = Path(directory) / 'trimmed.wav'
            with wave.open(str(raw), 'rb') as audio, wave.open(str(ready), 'wb') as target:
                target.setparams(audio.getparams())
                target.writeframes(audio.readframes(math.floor(args.duration * audio.getframerate())))
            seconds = validate_wav(ready, client)
        client.remaining()
        # A hard link publishes the complete file without replacing an existing destination.
        os.link(ready, output)
        print(f'Saved {seconds:.3f}s WAV', file=sys.stderr)


def generate(args):
    client = Client(args)
    output = args.output.expanduser().absolute()
    if output.suffix.lower() != '.wav':
        raise ValueError('the output filename must end in .wav')
    if not args.prompt.strip():
        raise ValueError('the prompt must not be empty')
    key = hashlib.sha256(client.base.encode()).hexdigest()[:16]
    lock_path = Path(tempfile.gettempdir()) / f'remiqora-audio-{os.getuid()}-{key}.lock'
    with lock_path.open('a') as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(min(0.2, client.remaining()))
        if os.path.lexists(output):
            raise FileExistsError(f'output already exists: {output}')
        models = installed_models(client)
        if args.model not in models:
            raise ValueError(f"model '{args.model}' is not installed; use --list-models")
        engine = models[args.model]
        if engine == 'stable_audio' and args.lyrics:
            raise ValueError('Stable Audio does not accept lyrics; choose ACE-Step or YuE2')
        if engine == 'ace_step':
            if not 10 <= args.duration <= 300:
                raise ValueError('ACE-Step duration must be 10..300 seconds')
            if not shutil.which('ffmpeg'):
                raise ValueError('ACE-Step WAV conversion requires ffmpeg on PATH')
        if engine == 'yue2' and not 0.5 <= args.duration <= 360:
            raise ValueError('YuE2 duration must be 0.5..360 seconds (upper bound)')
        status = client.request('/api/orchestrator/status')
        if any(model['status'] in ('starting', 'stopping') for model in status['models'].values()):
            raise ValueError('an engine is starting or stopping; wait for it to finish and retry')
        if status['active_model'] not in (None, engine) and not args.switch_engine:
            raise ValueError(f"{status['active_model']} is active; finish its jobs, then use --switch-engine to replace it")
        client.request('/api/orchestrator/switch', {'model': engine})
        source = {'stable_audio': stable_audio, 'ace_step': ace_step, 'yue2': yue2}[engine](client, args)
        save_audio(client, args, engine, source, output)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--list-models', action='store_true', help='List installed model IDs without starting an engine')
    parser.add_argument('--model', help='Installed model ID from --list-models')
    parser.add_argument('--prompt', help='Describe the sound or music, preferably in English')
    parser.add_argument('--duration', type=positive_seconds, metavar='SECONDS', help='YuE2 uses this as an upper bound')
    parser.add_argument('--output', type=Path, metavar='FILE.wav')
    parser.add_argument('--lyrics', help='Optional lyrics for ACE-Step or YuE2; omitted means instrumental request')
    parser.add_argument('--switch-engine', action='store_true', help='Allow replacing a different active engine, interrupting its jobs')
    parser.add_argument('--url', default=os.environ.get('REMIQORA_URL', 'http://127.0.0.1:9010'))
    parser.add_argument('--timeout', type=positive_seconds, default=1800, metavar='SECONDS', help='Total wait limit including queue and startup (default: 1800)')
    args = parser.parse_args(argv)
    if not args.list_models and any(getattr(args, name) is None for name in ('model', 'prompt', 'duration', 'output')):
        parser.error('--model, --prompt, --duration and --output are required for generation')
    try:
        if args.list_models:
            for model, engine in installed_models(Client(args)).items():
                print(f'{model}\t{engine}')
        else:
            print(generate(args))
        return 0
    except KeyboardInterrupt:
        print('Cancelled.', file=sys.stderr)
        return 130
    except urllib.error.HTTPError as error:
        print(f"Remiqora HTTP {error.code}: {error.read(2000).decode(errors='replace')}", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, IndexError, EOFError, wave.Error, subprocess.SubprocessError) as error:
        print(f'Remiqora: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
