"""Run the bundled sample through the whole pipeline without a network call.

The demo stands in for the two things v0.1 does not automate: the model and the
author. A fixed candidate set plays the model, including two deliberately wrong
claims (an altered quote and a quote filed under the wrong clause), and a fixed
rule plays the author: accept a claim only when its quote verifies, reject it
otherwise. The run still goes through the real review loop, the real export
gate and the real templates, and it writes two artifacts worth comparing:

- ``decisions.json`` and the three rendered files, from accepted claims only;
- ``demo-raw-model-draft.md``, what the model said before any checking.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from clauseanchor.anchors import AnchorError, verify_claim, verify_quote
from clauseanchor.candidates import CandidateError, Claim
from clauseanchor.document import Clause, Document, load
from clauseanchor.render import render
from clauseanchor.review import save_decisions, run_review

DEMO_MODEL = "demo-offline"
# The fake model's own guess. It is deliberately not the parsed effective date:
# the demo shows that the pack takes the date from the document, not from prose.
DEMO_MODEL_DATE = "2027-01-01"
RAW_DRAFT_NAME = "demo-raw-model-draft.md"
MIN_QUOTE_CHARS = 14
MAX_QUOTE_CHARS = 60
MAX_CONCLUSION_CHARS = 60

Display = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class DemoResult:
    document: Document
    decisions_path: Path
    outputs: tuple[Path, ...]
    raw_draft: Path
    accepted: tuple[str, ...]
    rejected: tuple[str, ...]


def sample_path() -> Path:
    """Locate the bundled sample in a checkout or in an installed wheel."""
    module = Path(__file__).resolve()
    for candidate in (
        module.parent / "examples" / "sample_gongbao.txt",
        module.parents[2] / "examples" / "sample_gongbao.txt",
    ):
        if candidate.is_file():
            return candidate
    raise CandidateError("找不到内置样例 examples/sample_gongbao.txt。")


def run_demo(
    path: str | Path,
    out_dir: str | Path,
    *,
    output: Display | None = None,
) -> DemoResult:
    """Parse, propose, review, export. No model call, no API key, no network."""
    output = output or print
    document = load(path)
    claims = build_claims(document)
    clauses = {clause.path: clause for clause in document.clauses}

    payload = run_review(
        document,
        claims,
        model=DEMO_MODEL,
        input_fn=_ScriptedReader(_answers(claims, clauses)),
        output=output,
    )

    out = Path(out_dir)
    decisions_path = save_decisions(payload, out / "decisions.json")
    result = render(payload, out)

    raw_path = out / RAW_DRAFT_NAME
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(_raw_draft(document, claims), encoding="utf-8")

    accepted: list[str] = []
    rejected: list[str] = []
    for record in payload["claims"]:
        label = (record.get("anchor") or {}).get("clause") or "、".join(
            "".join(path) for path in record["clause_ids"]
        )
        entry = f"{record['claim_id']} {label}"
        if record["verdict"] == "accepted":
            accepted.append(entry)
        else:
            rejected.append(entry)

    return DemoResult(
        document=document,
        decisions_path=decisions_path,
        outputs=result.outputs,
        raw_draft=raw_path,
        accepted=tuple(accepted),
        rejected=tuple(rejected),
    )


def build_claims(document: Document) -> list[Claim]:
    """A fixed candidate set derived from the document's own clauses.

    Two candidates are valid. One has its quote altered mid-sentence, the way a
    model smooths a passage; one asserts a clause number that does not contain
    the quote at all. Both must be blocked before export.
    """
    articles = [
        clause
        for clause in document.articles
        if len(_compact(clause.body)) >= MIN_QUOTE_CHARS + 6
    ]
    if not articles:
        raise CandidateError("文档里的条款太短，无法生成演示候选。")

    picks = _spread(articles)
    claims = [
        _claim(1, picks[0], _quote(picks[0])),
    ]
    if len(picks) >= 2:
        claims.append(_claim(2, picks[1], _quote(picks[1])))

    last = picks[-1]
    altered = _mutate(_quote(last), last)
    claims.append(_claim(len(claims) + 1, last, altered))

    if len(picks) >= 2:
        borrowed = _quote(last)
        try:
            verify_quote(picks[1], borrowed)
        except AnchorError:
            # The model filed another clause's text under this clause number.
            claims.append(
                _claim(
                    len(claims) + 1,
                    picks[1],
                    borrowed,
                    conclusion_clause=last,
                )
            )

    return claims


def _claim(
    number: int,
    clause: Clause,
    quote: str,
    *,
    conclusion_clause: Clause | None = None,
) -> Claim:
    return Claim(
        claim_id=f"c{number}",
        conclusion=_conclusion(conclusion_clause or clause),
        clause_ids=(clause.path,),
        quote=quote,
    )


def _conclusion(clause: Clause) -> str:
    """The longest sentence of the clause, as a stand-in author would write it."""
    sentence = max(_sentences(_compact(clause.body)), key=len)
    if len(sentence) > MAX_CONCLUSION_CHARS:
        sentence = sentence[:MAX_CONCLUSION_CHARS] + "……"
    return sentence


def _quote(clause: Clause) -> str:
    """A citable passage, cut at the last sentence or clause break in range."""
    body = _compact(clause.body)
    for marks in (_TERMINATORS, _CLAUSE_BREAKS):
        positions = [
            position
            for position in _bounds(body, marks)
            if 24 <= position <= MAX_QUOTE_CHARS
        ]
        if positions:
            return body[: positions[-1]]
    return body[:48]


def _mutate(quote: str, clause: Clause) -> str:
    middle = max(MIN_QUOTE_CHARS // 2, len(quote) // 2)
    altered = quote[:middle] + "以及其他相关事项" + quote[middle:]
    try:
        verify_quote(clause, altered)
    except AnchorError:
        return altered

    fallback = quote + "，经研究决定"
    try:
        verify_quote(clause, fallback)
    except AnchorError:
        return fallback
    raise CandidateError("无法构造演示用的改写引文。")


def _answers(
    claims: list[Claim], clauses: dict[tuple[str, ...], Clause]
) -> list[str]:
    answers: list[str] = []
    for claim in claims:
        try:
            verify_claim(clauses, claim.clause_ids, claim.quote, claim.conclusion)
        except AnchorError:
            answers.append("r")
        else:
            answers.append("a")
    return answers


def _raw_draft(document: Document, claims: list[Claim]) -> str:
    effective = document.effective_date or "（未识别）"
    lines = [
        "# 模型原稿（未经任何校验）",
        "",
        f"> 示例模型自报生效日期：{DEMO_MODEL_DATE}。"
        f"ClauseAnchor 不采信模型给出的日期，成品使用解析结果 {effective}。",
        "",
    ]
    for claim in claims:
        lines.extend(
            [
                f"## [{claim.claim_id}] {claim.label}",
                "",
                claim.conclusion,
                "",
                f"引文：“{claim.quote}”",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _compact(text: str) -> str:
    """Collapse whitespace so PDF line breaks do not split a quote."""
    return "".join(text.split())


_TERMINATORS = "。."
_CLAUSE_BREAKS = "，,、；;"


def _bounds(text: str, marks: str) -> list[int]:
    """End offsets (exclusive) of passages ending in one of ``marks``."""
    return [
        position + 1
        for position, character in enumerate(text)
        if character in marks
    ]


def _sentences(text: str) -> list[str]:
    bounds = _bounds(text, _TERMINATORS)
    if not bounds:
        return [text]
    sentences: list[str] = []
    start = 0
    for end in bounds:
        sentences.append(text[start:end])
        start = end
    if start < len(text):
        sentences.append(text[start:])
    return sentences


def _spread(items: list[Clause]) -> list[Clause]:
    """First, middle and last third: three visibly different, clean articles.

    The document tail (signature, 抄送) follows the last article in some real
    PDFs, so the demo picks from the first two thirds instead of the end.
    """
    if len(items) <= 3:
        return list(items)
    return [items[index] for index in (0, len(items) // 3, (2 * len(items)) // 3)]


class _ScriptedReader:
    """Answers the review prompts from a fixed queue."""

    def __init__(self, answers: Iterable[str]) -> None:
        self._answers = deque(answers)

    def __call__(self, prompt: str) -> str:
        if not self._answers:
            raise EOFError
        return self._answers.popleft()
