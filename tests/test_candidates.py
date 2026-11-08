"""Offline tests for the model boundary: provider, JSON parsing and CLI draft.

The only network traffic is a stub HTTP server bound to 127.0.0.1, which speaks
the same ``/chat/completions`` shape as a real OpenAI-compatible endpoint.
"""

from __future__ import annotations

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Sequence

import pytest

from clauseanchor.candidates import (
    CandidateError,
    OpenAICompatibleProvider,
    generate_claims,
    parse_claims,
    resolve_model,
)
from clauseanchor.cli import main
from clauseanchor.document import load

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "sample_gongbao.txt"
QUOTE_1 = "对首次采购云服务的中小企业，按照实际支出的百分之三十给予补助"


class _FakeProvider:
    """A Provider that returns a canned answer and records the prompt."""

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.messages: list[dict[str, str]] = []

    def complete(self, messages: Sequence[dict[str, str]]) -> str:
        self.messages = list(messages)
        return self.answer


def test_generate_claims_parses_a_fenced_answer_from_the_provider() -> None:
    document = load(SAMPLE)
    answer = json.dumps(
        [
            {
                "conclusion": "首次采购云服务可获三成补助。",
                "clause_ids": [["第一条"]],
                "quote": QUOTE_1,
            }
        ],
        ensure_ascii=False,
    )
    provider = _FakeProvider(f"```json\n{answer}\n```")

    claims = generate_claims(document, provider, max_claims=3)

    assert [claim.claim_id for claim in claims] == ["c1"]
    assert claims[0].clause_ids == (("第一条",),)
    assert claims[0].quote == QUOTE_1
    # The prompt really contains the parsed clause listing.
    assert "第一条" in provider.messages[-1]["content"]


def test_parse_claims_reads_a_wrapped_list_and_skips_incomplete_items() -> None:
    raw = json.dumps(
        {
            "claims": [
                {
                    "conclusion": "甲",
                    "clause_ids": [["第一条"]],
                    "quote": "一段足够长的原文引文",
                },
                {"conclusion": "", "clause_ids": [["第二条"]], "quote": "被跳过"},
                {
                    "conclusion": "丙",
                    "clause_ids": ["第三条"],
                    "quote": "另一段足够长的原文引文",
                },
            ]
        },
        ensure_ascii=False,
    )

    claims = parse_claims(raw, max_claims=2)

    assert [claim.claim_id for claim in claims] == ["c1", "c2"]
    assert claims[0].clause_ids == (("第一条",),)
    assert claims[1].clause_ids == (("第三条",),)


def test_parse_claims_rejects_an_answer_without_json() -> None:
    with pytest.raises(CandidateError, match="JSON"):
        parse_claims("这里没有任何 JSON")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("deepseek", "deepseek-chat"),
        ("DeepSeek-R", "deepseek-reasoner"),
        (" qwen ", "qwen-plus"),
        ("deepseek-chat", "deepseek-chat"),
        ("custom/model-7b", "custom/model-7b"),
    ],
)
def test_resolve_model_maps_aliases_and_passes_other_ids(
    name: str, expected: str
) -> None:
    assert resolve_model(name) == expected


def test_provider_resolves_its_model_alias() -> None:
    provider = OpenAICompatibleProvider(model="qwen", api_key="test-key")

    assert provider.model == "qwen-plus"
    assert provider.api_key == "test-key"


def test_cli_draft_against_a_local_http_stub(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    answer = json.dumps(
        [
            {
                "conclusion": "首次采购云服务可获三成补助。",
                "clause_ids": [["第一条"]],
                "quote": QUOTE_1,
            }
        ],
        ensure_ascii=False,
    )
    seen: dict[str, object] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server API
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            seen["path"] = self.path
            seen["model"] = body["model"]
            payload = json.dumps(
                {"choices": [{"message": {"content": answer}}]}, ensure_ascii=False
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr("sys.stdin", io.StringIO("a\n"))
    out = tmp_path / "decisions.json"
    try:
        code = main(
            [
                "draft",
                str(SAMPLE),
                "--model",
                "deepseek",
                "--base-url",
                f"http://127.0.0.1:{server.server_port}/v1",
                "-o",
                str(out),
            ]
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert code == 0
    assert seen["path"] == "/v1/chat/completions"
    assert seen["model"] == "deepseek-chat"
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["model"] == "deepseek-chat"
    record = payload["claims"][0]
    assert record["verdict"] == "accepted"
    assert record["anchor"]["ok"] is True
