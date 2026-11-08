---
name: remiqora-audio
description: Generate game sound effects, ambience, instrumental music or songs as WAV assets through Rupert's local Remiqora. Use Stable Audio for sounds, or ACE-Step and YuE2 for music with optional lyrics.
---

1. Run `remiqora-audio --list-models` to discover installed model IDs. Remiqora must already be running. Use `--help` for flags and `--url` or `REMIQORA_URL` for an address other than the default `http://127.0.0.1:9010`.
2. Choose `small-sfx` for effects and ambience, `small-music` for instrumental music, or an installed ACE-Step/YuE2 model for music or songs. Installed Stable Audio `-base` models also work. **For ACE-Step or YuE2, read [music-engines.md](references/music-engines.md) before choosing the duration or lyrics.**
3. Run the command from the game's directory with the exact model ID, an English prompt, duration and a new `.wav` destination. The command waits and saves the finished WAV. A successful exit prints its absolute path to stdout; progress and errors go to stderr.

```bash
remiqora-audio --model small-sfx \
  --prompt "A futuristic laser shot, sharp attack, isolated, no music" \
  --duration 2 --output assets/audio/laser.wav

remiqora-audio --model acestep-v15-turbo \
  --prompt "Instrumental electronic exploration music, warm synths, gentle percussion, 100 BPM, no vocals" \
  --duration 30 --output assets/audio/exploration.wav
```

4. Audition the result through the game's audio path. Check loudness, timing and any loop seam. Report an asset as generated only after exit 0 and as suitable only after auditioning it.

The wrapper starts the selected engine if none is active. A different active engine blocks generation by default. Finish its jobs and coordinate with other clients before retrying with `--switch-engine`, which explicitly allows interrupting that engine. An engine already starting or stopping must finish first. Wrapper requests serialize per API URL; browser jobs and other clients do not share that lock. Engine changes stay active after completion.

Existing files are preserved. A complete, validated WAV is published atomically; temporary files are removed on failure. Choose a new filename for another variant. Output goes directly to the requested path, outside Remiqora's library. Model installation and the app's server lifecycle remain user-controlled.

On timeout or Ctrl+C, Stable Audio and ACE-Step attempt to cancel only their own job. Follow the YuE2 cancellation caveat in its reference. Report failures without automatically resubmitting. Exit codes are 0 for success, 1 for failure and 130 for Ctrl+C.

If the command is absent from PATH, use `python3 ~/.codex/skills/remiqora-audio/scripts/remiqora_audio.py` with the same arguments.
