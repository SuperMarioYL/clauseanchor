"""Draft claim candidates from a clause tree with an OpenAI-compatible model.

The model only proposes: a conclusion, the clauses it relies on, and a
verbatim quote. It never decides whether the quote is correct -- that is
``anchors.verify_quote`` against the clause the claim asserted -- and this
module never repairs a quote on the model's behalf. A quote that comes back
altered stays altered so the review and export gates can refuse it.

The chat call is an ordinary ``/chat/completions`` request, so ``--base-url``
retargets it at DeepSeek, Qwen (DashScope compatible mode), or any local
gateway. Tests inject a fake provider and never touch the network.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Protocol, Sequence

import httpx

from clauseanchor.document import Document

DEFAULT_MODEL = "deepseek-chat"
DEFAULT_BASE_URL = "https://api.deepseek.com/v1"

# Short aliases accepted on the command line; anything else is a real model id
# and is passed through untouched.
MODEL_ALIASES = {
    "deepseek": "deepseek-chat",
    "deepseek-r": "deepseek-reasoner",
    "qwen": "qwen-plus",
}

_API_KEY_ENV = ("CLAUSEANCHOR_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY")


class CandidateError(RuntimeError):
    """The model could not be called or its answer has no usable candidates."""


class ModelError(CandidateError):
    """The model endpoint failed or answered in an unexpected shape."""


@dataclass(frozen=True, slots=True)
class Claim:
    """The minimum writing unit: a conclusion, its clauses, and a quote.

    ``clause_ids`` holds clause paths as they appear in the clause tree, e.g.
    ``(("第三条",),)`` or ``(("第六条", "第一款"),)``. A path that does not
    exist in the document is kept as asserted: the gate must report it, not
    quietly drop it.
    """

    claim_id: str
    conclusion: str
    clause_ids: tuple[tuple[str, ...], ...]
    quote: str

    @property
    def label(self) -> str:
        return "、".join("".join(path) for path in self.clause_ids)


def resolve_model(model: str) -> str:
    """Map a short alias to a real model id; pass other ids through."""
    name = model.strip()
    return MODEL_ALIASES.get(name.lower(), name)


class Provider(Protocol):
    """Anything that can answer an OpenAI-style chat request."""

    def complete(self, messages: Sequence[dict[str, str]]) -> str: ...


@dataclass(slots=True)
class OpenAICompatibleProvider:
    """Chat call against any OpenAI-compatible endpoint."""

    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    api_key: str | None = None
    timeout: float = 60.0

    def __post_init__(self) -> None:
        self.model = resolve_model(self.model)
        self.api_key = _api_key(self.api_key)

    def complete(self, messages: Sequence[dict[str, str]]) -> str:
        url = self.base_url.rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = {
            "model": self.model,
            "messages": list(messages),
            "temperature": 0.2,
            "stream": False,
        }
        try:
            response = httpx.post(url, headers=headers, json=body, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise ModelError(f"调用模型接口失败：{url}（{exc}）") from exc
        if response.status_code >= 400:
            detail = " ".join(response.text.split())[:200]
            raise ModelError(f"模型接口返回 {response.status_code}：{detail}")
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise ModelError("模型接口没有返回 JSON。") from exc
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelError("模型接口响应缺少 choices[0].message.content。") from exc
        if not isinstance(content, str):
            raise ModelError("模型返回的 content 不是文本。")
        return content


def generate_claims(
    document: Document,
    provider: Provider,
    *,
    max_claims: int = 6,
) -> list[Claim]:
    """Ask the model for candidates and parse them into claims."""
    if max_claims < 1:
        raise CandidateError("候选条数至少为 1。")
    raw = provider.complete(build_messages(document, max_claims=max_claims))
    return parse_claims(raw, max_claims=max_claims)


def build_messages(document: Document, *, max_claims: int = 6) -> list[dict[str, str]]:
    articles = document.articles
    if not articles:
        raise CandidateError("没有识别到条款，无法生成候选。先用 parse 检查条款切分。")

    listing = "\n\n".join(
        f"[{''.join(clause.path)}]（第 {clause.page} 页）\n{clause.text}"
        for clause in articles
    )
    system = (
        "你是政策解读编辑的候选起草助手。"
        "你只输出一个 JSON 数组，不输出解释、Markdown 或代码块标记。"
    )
    user = (
        f"文件：{document.title or '（无标题）'}\n"
        f"发文机关：{document.issuer or '（未识别）'}\n"
        f"发文字号：{document.doc_number or '（未识别）'}\n"
        f"生效日期：{document.effective_date or '（未识别）'}\n\n"
        "以下是解析出的条款，方括号里是条款路径：\n\n"
        f"{listing}\n\n"
        + _INSTRUCTIONS.format(count=max_claims)
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


_INSTRUCTIONS = """请挑出 {count} 条最值得向读者解读的条款，每条写一句结论，并摘录一段原文。

硬性要求：
1. conclusion 是一句中文判断句，不超过 60 字。
2. clause_ids 是条款路径数组，至少一项，例如 [["第三条"]]；只能使用上面方括号里给出的路径。
3. quote 必须从该条款原文中逐字复制一段连续文字，不少于 12 个字（可以取自该条下的某一款）：不得改写、不得补字、不得跨条拼接。导出前会逐字校验，改写过的引文会被拦下。
4. 只输出一个 JSON 数组，每项形如 {{"conclusion": "...", "clause_ids": [["第三条"]], "quote": "..."}}。"""


def parse_claims(raw: str, *, max_claims: int | None = None) -> list[Claim]:
    """Turn the model's answer into claims without touching the quotes."""
    payload = _load_json_block(raw)
    if isinstance(payload, dict):
        for key in ("claims", "candidates", "items"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if not isinstance(payload, list):
        raise CandidateError("模型返回的 JSON 不是候选数组。")

    claims: list[Claim] = []
    for item in payload:
        if max_claims is not None and len(claims) >= max_claims:
            break
        if not isinstance(item, dict):
            continue
        conclusion = str(item.get("conclusion") or "").strip()
        quote = str(item.get("quote") or "").strip()
        clause_ids = _normalize_clause_ids(item.get("clause_ids"))
        if not conclusion or not quote or not clause_ids:
            continue
        claims.append(
            Claim(
                claim_id=f"c{len(claims) + 1}",
                conclusion=conclusion,
                clause_ids=clause_ids,
                quote=quote,
            )
        )
    if not claims:
        raise CandidateError("模型没有返回可用的候选（缺少 conclusion、quote 或 clause_ids）。")
    return claims


def _normalize_clause_ids(value: Any) -> tuple[tuple[str, ...], ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return ()
    paths: list[tuple[str, ...]] = []
    for item in value:
        if isinstance(item, str):
            label = item.strip()
            if label:
                paths.append((label,))
        elif isinstance(item, (list, tuple)):
            parts = tuple(str(part).strip() for part in item if str(part).strip())
            if parts:
                paths.append(parts)
    return tuple(paths)


def _load_json_block(text: str) -> Any:
    """Extract the first JSON value from a model answer that may be fenced."""
    stripped = text.strip()
    fence = re.match(r"^```[A-Za-z]*\s*(.*?)\s*```$", stripped, re.DOTALL)
    if fence:
        stripped = fence.group(1).strip()
    decoder = json.JSONDecoder()
    for index, character in enumerate(stripped):
        if character not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(stripped, index)
        except json.JSONDecodeError:
            continue
        return value
    raise CandidateError("模型返回的内容里没有可解析的 JSON。")


def _api_key(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    for name in _API_KEY_ENV:
        value = os.environ.get(name)
        if value:
            return value
    return None
