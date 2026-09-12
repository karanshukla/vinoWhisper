import codecs
import time

import numpy as np
import requests

from . import config


class TranscriptionClient:
    def __init__(self, base_url: str | None = None) -> None:
        # Not a default argument, which would bind config.SERVER_URL at import.
        self._base_url = base_url or config.SERVER_URL
        self._session = requests.Session()

    def wait_ready(self) -> dict:
        response = self._session.get(
            f"{self._base_url}/health",
            timeout=(config.CONNECT_TIMEOUT_S, config.MODEL_LOAD_TIMEOUT_S),
        )
        response.raise_for_status()
        return response.json()

    def transcribe(self, samples: np.ndarray) -> tuple[str, float | None]:
        started_at = time.monotonic()
        response = self._session.post(
            f"{self._base_url}/transcribe",
            data=samples.astype("<f4", copy=False).tobytes(),
            headers={"Content-Type": "application/octet-stream"},
            stream=True,
            timeout=(config.CONNECT_TIMEOUT_S, config.REQUEST_TIMEOUT_S),
        )
        response.raise_for_status()

        # Incremental: a chunk boundary can split a multi-byte character.
        decoder = codecs.getincrementaldecoder("utf-8")()
        first_piece_s: float | None = None
        parts: list[str] = []
        for chunk in response.iter_content(chunk_size=256):
            if not chunk:
                continue
            if first_piece_s is None:
                first_piece_s = time.monotonic() - started_at
            parts.append(decoder.decode(chunk))
        parts.append(decoder.decode(b"", final=True))

        return "".join(parts), first_piece_s
