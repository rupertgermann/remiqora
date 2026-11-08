# ACE-Step and YuE2

## ACE-Step

Select an exact installed checkpoint ID from `--list-models`, for example `acestep-v15-turbo` or `acestep-v15-base`. The wrapper uses 8 inference steps for Turbo and 50 for other checkpoints. It loads the requested checkpoint on demand without changing Remiqora's saved default.

Use 10 to 300 seconds. Omit `--lyrics` for an instrumental request. For a song, pass lyrics as one argument, optionally with section tags such as `[Verse]` and `[Chorus]`. Keep musical direction in `--prompt`.

```bash
remiqora-audio --model acestep-v15-base \
  --prompt "Bright orchestral adventure song, English vocals" \
  --lyrics $'[Verse]\nAcross the hills we ride\n[Chorus]\nHome beyond the tide' \
  --duration 30 --output assets/audio/adventure-song.wav
```

FFmpeg must be on PATH. ACE-Step can return float WAV; the wrapper converts it to PCM16 WAV before validation and publication.

## YuE2

Select an installed precision ID such as `yue2-q8_0` or `yue2-q4_0`. Each request explicitly loads the chosen precision, including when another precision was previously resident.

`--duration` is an **upper bound**, from 0.5 to 360 seconds. The wrapper budgets 25 semantic tokens per second and caps decoded audio at the requested duration. The model can finish earlier; the wrapper reports the actual saved length and adds no silence. Prefer Stable Audio or ACE-Step when game timing requires a specified length.

Omitted lyrics send `[Instrumental]` as a generation request. This is a prompt convention, not a guaranteed vocal suppression mode. For songs, use `--lyrics` with section tags and lyrics; keep style in `--prompt`.

```bash
remiqora-audio --model yue2-q8_0 \
  --prompt "Instrumental cinematic space exploration, slow evolving synth pads, no vocals" \
  --duration 30 --output assets/audio/space.wav
```

YuE2 runs one blocking native request. Its API has no per-job cancellation endpoint. A timeout or Ctrl+C ends the CLI wait, but server computation may continue. Check Remiqora before submitting another request or changing engines. The wrapper leaves that engine running.

The duration mapping follows the installed audio.cpp v0.8.1 pipeline: one semantic codec token yields one latent frame; its VAE decodes 1920 samples per frame at 48000 Hz. Source: [YuE2 pipeline](https://github.com/0xShug0/audio.cpp/blob/v0.8.1/src/models/yue2/pipeline.cpp) and [NAR runtime](https://github.com/0xShug0/audio.cpp/blob/v0.8.1/src/models/yue2/nar_runtime.cpp).
