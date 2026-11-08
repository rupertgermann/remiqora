import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from sa3_server.engine import Engine


class LocalEncoderTest(unittest.TestCase):
    def test_base_checkpoint_loads_its_own_encoder_without_changing_cached_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshots" / "revision"
            snapshot.mkdir(parents=True)
            blob = root / "blobs" / "weights"
            blob.parent.mkdir()
            blob.touch()
            checkpoint = snapshot / "model.safetensors"
            checkpoint.symlink_to(blob)
            config = {"model": {"conditioning": {"configs": [
                {"type": "t5gemma", "config": {
                    "repo_id": "stabilityai/stable-audio-3-small-music",
                    "subfolder": "t5gemma-b-b-ul2",
                }},
                {"type": "number", "config": {"min_val": 0}},
            ]}}}
            config_path = snapshot / "model_config.json"
            config_path.write_text(json.dumps(config))
            loaded = Mock()
            with (
                patch("sa3_server.engine.installed_models", return_value={"small-music-base": str(checkpoint)}),
                patch.object(Engine, "_disable_flash_attention_if_unusable"),
                patch("sa3_server.engine.torch.cuda.is_available", return_value=False),
                patch("stable_audio_3.loading_utils.load_diffusion_cond", return_value=loaded) as load,
                patch("stable_audio_3.StableAudioModel") as wrapper,
            ):
                engine = Engine(root / "output", device="mps")
                result = engine.load("small-music-base")
                passed = load.call_args.args[0]
                encoder = passed["model"]["conditioning"]["configs"][0]["config"]
                self.assertEqual(str(snapshot), encoder["model_path"])
                self.assertEqual("t5gemma-b-b-ul2", encoder["subfolder"])
                self.assertEqual(config, json.loads(config_path.read_text()))
                load.assert_called_once_with(passed, str(checkpoint), device="mps", model_half=False)
                self.assertFalse(loaded.use_lora)
                self.assertEqual([], loaded.lora_names)
                wrapper.assert_called_once_with(loaded, passed, "mps", False)
                self.assertIs(result, wrapper.return_value)
                self.assertIs(engine.load("small-music-base"), result)


if __name__ == "__main__":
    unittest.main()
