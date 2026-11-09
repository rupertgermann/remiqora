from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import DATA_DIR, MODELS, YUE2_DIR, yue2_specs
from ..orchestrator.manager import manager
from ..orchestrator.state import ModelStatus
from .routes_proxy import _client

router = APIRouter(prefix="/api/yue2")

_NAME = "ar_lora_inst_v3abc.bf16.safetensors"
_URL = (
    "https://huggingface.co/Mothersuperior/YuE2-instrumental-cot-full-loras"
    f"/resolve/947f2f4b28978b2b6c3e316e6a87925c76bf3c4b/{_NAME}"
)
_SIZE = 139_502_088
_SHA256 = "e408fd3148b75b1165f7ddbf63db575d83bb6402a0b5f876fcb767dbcb2c5414"
_download_lock = asyncio.Lock()


def _ar_lora_option() -> str | None:
    try:
        spec = json.loads((YUE2_DIR / "model_specs" / "yue2.json").read_text(encoding="utf-8"))
        names = {option["name"] for option in spec["options"]["session"]}
    except (OSError, ValueError, KeyError, TypeError):
        return None
    # The source-build pin predates the rename from lora to ar_lora.
    return next((f"yue2.{name}" for name in ("ar_lora", "lora") if name in names), None)


async def _adapter_path() -> Path:
    path = DATA_DIR / "yue2" / f"{_SHA256}-{_NAME}"
    async with _download_lock:
        if path.is_file() and path.stat().st_size == _SIZE:
            return path

        path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        temp_path = None
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
                async with client.stream("GET", _URL) as response:
                    response.raise_for_status()
                    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".part", delete=False) as output:
                        temp_path = Path(output.name)
                        async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
                            size += len(chunk)
                            if size > _SIZE:
                                raise ValueError("Unexpected YuE2 Instrumental LoRA size")
                            digest.update(chunk)
                            output.write(chunk)
            if size != _SIZE or digest.hexdigest() != _SHA256:
                raise ValueError("YuE2 Instrumental LoRA checksum mismatch")
            temp_path.replace(path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        return path


class LoadInstrumentalRequest(BaseModel):
    precision: Literal["q8_0", "q4_0"]


@router.post("/instrumental/load")
async def load_instrumental(request: LoadInstrumentalRequest) -> dict[str, str]:
    state = manager.state.models["yue2"]
    if state.status != ModelStatus.RUNNING:
        raise HTTPException(status_code=503, detail="YuE2 is not active")
    lora_option = _ar_lora_option()
    if lora_option is None:
        raise HTTPException(status_code=409, detail="YuE2 Instrumental requires an audio.cpp build with YuE2 AR LoRA support. Update the YuE2 engine and restart it.")
    try:
        adapter = await _adapter_path()
    except (httpx.HTTPError, OSError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"YuE2 Instrumental LoRA download failed: {exc}") from exc
    if state.status != ModelStatus.RUNNING:
        raise HTTPException(status_code=503, detail="YuE2 stopped while preparing the Instrumental LoRA")

    spec = yue2_specs()["yue2"]
    model_id = f"yue2-instrumental-{request.precision}"
    try:
        result = await _client.post(
            f"{MODELS['yue2'].proxy_target}/v1/models/load",
            json={
                **spec,
                "id": model_id,
                "load_options": {},
                "session_options": {
                    "yue2.model_gguf": f"yue2-3b-{request.precision}.gguf",
                    lora_option: str(adapter.resolve()),
                },
            },
            timeout=120,
        )
        result.raise_for_status()
        loaded = result.json()
        if not isinstance(loaded, dict) or loaded.get("loaded") is not True:
            raise ValueError("YuE2 did not load the instrumental session")
    except (httpx.HTTPError, ValueError) as exc:
        detail = exc.response.text if isinstance(exc, httpx.HTTPStatusError) else str(exc)
        raise HTTPException(status_code=502, detail=f"YuE2 Instrumental LoRA could not be loaded: {detail}") from exc
    return {"id": model_id}
