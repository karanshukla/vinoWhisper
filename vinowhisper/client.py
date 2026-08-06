"""HTTP client for the transcription server."""

import codecs
import time

import numpy as np
import requests

from . import config


class TranscriptionClient:
    def __init__(self, base_url: str | None = None) -> None:
        # Resolved at call time, not captured as a default argument: a default
        # of config.SERVER_URL would bind at import and quietly ignore anything
        # that changes the config afterwards.
        self._base_url = base_url or config.SERVER_URL
        self._session = requests.Session()

    def wait_ready(self) -> dict:
        """Block through a cold start and return the server's health payload.

        Worth doing before the caption loop rather than letting the first
        transcribe absorb it: the server is socket-activated, so the first
        request in blocks through the whole NPU model load (~10-30s) and that
        is otherwise indistinguishable from the captions just being broken.
        """
        response = self._session.get(
            f"{self._base_url}/health",
            timeout=(config.CONNECT_TIMEOUT_S, config.MODEL_LOAD_TIMEOUT_S),
        )
        response.raise_for_status()
        return response.json()

    def transcribe(self, samples: np.ndarray) -> tuple[str, float | None]:
        """Transcribe one window. Returns (transcript, seconds_to_first_piece).

        The response is a token stream, so the first-piece timing is the honest
        measure of how fast the model is responding — the total is dominated by
        how many tokens the window's speech decodes into.
        """
        started_at = time.monotonic()
        response = self._session.post(
            f"{self._base_url}/transcribe",
            data=samples.astype("<f4", copy=False).tobytes(),
            headers={"Content-Type": "application/octet-stream"},
            stream=True,
            timeout=(config.CONNECT_TIMEOUT_S, config.REQUEST_TIMEOUT_S),
        )
        response.raise_for_status()

        # Incremental decoder, not chunk.decode() per chunk. Whisper emits
        # multi-byte characters routinely (curly quotes, em dashes, ellipses),
        # and a chunk boundary lands mid-character sooner or later — decoding
        # each chunk independently raises UnicodeDecodeError when it does. The
        # old chunk_size=1 read made that a certainty rather than a race, since
        # every multi-byte character was split into single bytes by
        # construction.
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
