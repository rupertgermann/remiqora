import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from app.api import routes_yue2_instrumental as route


class InstrumentalAdapterTest(unittest.IsolatedAsyncioTestCase):
    def test_uses_the_adapter_option_advertised_by_the_installed_engine(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(route, "YUE2_DIR", Path(directory)):
            spec = Path(directory) / "model_specs" / "yue2.json"
            spec.parent.mkdir()
            for name, expected in [("lora", "yue2.lora"), ("ar_lora", "yue2.ar_lora"), ("model_gguf", None)]:
                with self.subTest(name=name):
                    spec.write_text(json.dumps({"options": {"session": [{"name": name}]}}), encoding="utf-8")
                    self.assertEqual(route._ar_lora_option(), expected)

    async def test_corrupt_download_is_discarded_then_concurrent_calls_share_verified_file(self):
        content = b"valid adapter"
        responses = iter([b"wrong adapter", content])
        requests = 0

        async def download(request: httpx.Request) -> httpx.Response:
            nonlocal requests
            requests += 1
            self.assertEqual(request.url, httpx.URL(route._URL))
            await asyncio.sleep(0)
            return httpx.Response(200, content=next(responses))

        clients = [httpx.AsyncClient(transport=httpx.MockTransport(download)) for _ in range(2)]
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(route, "DATA_DIR", Path(directory)),
            patch.object(route, "_SIZE", len(content)),
            patch.object(route, "_SHA256", hashlib.sha256(content).hexdigest()),
            patch.object(route, "_download_lock", asyncio.Lock()),
            patch.object(route.httpx, "AsyncClient", side_effect=clients),
        ):
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                await route._adapter_path()
            self.assertEqual(list((Path(directory) / "yue2").iterdir()), [])

            first, second = await asyncio.gather(route._adapter_path(), route._adapter_path())
            self.assertEqual(first, second)
            self.assertEqual(first.read_bytes(), content)
            self.assertEqual(requests, 2)
