"""Offline checks for the zero-credit conversational lane."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from veda.agent import local_chat


class _Response:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def main() -> None:
    tags = _Response({"models": [{"name": "qwen2.5:7b"}]})
    answer = _Response({
        "model": "qwen2.5:7b",
        "message": {"content": "Why did the crane blush? It saw the site plans."},
    })
    with (mock.patch.object(local_chat.httpx, "get", return_value=tags),
          mock.patch.object(local_chat.httpx, "post", return_value=answer)):
        assert local_chat.health()["ok"] is True
        result = local_chat.reply("Tell me a joke", [])
    assert result.model == "qwen2.5:7b"
    assert result.answer.startswith("Why did")
    print("local zero-credit chat smoke test: PASS")


if __name__ == "__main__":
    main()
