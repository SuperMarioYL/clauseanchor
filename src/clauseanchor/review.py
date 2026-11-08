"""Review claim candidates one by one and record the decisions.

The author accepts (a), rewrites (e) or rejects (r) each claim. The resulting
decisions.json is the only input the renderer reads, and it carries, per claim,
the verdict, the final conclusion and quote, the clause paths, a snapshot of
the cited clauses, and the anchor check result at the time of review.

``e`` lets the author paste the true passage from the clause, which is the
intended way to repair a quote the model altered. Nothing in this module
rewrites a quote silently.
"""

from __future__ import annotations

import copy
import json
import textwrap
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from clauseanchor.anchors import AnchorError, verify_claim
from clauseanchor.candidates import Claim
from clauseanchor.document import (
    Clause,
    Document,
    DocumentError,
    load_source,
    verify_snapshot,
)

DECISIONS_SCHEMA = "clauseanchor.decisions"
DECISIONS_VERSION = 1
ACCEPTED = "accepted"
REJECTED = "rejected"
VERDICTS = (ACCEPTED, REJECTED)

Display = Callable[[str], None]
Reader = Callable[[str], str]


class DecisionError(ValueError):
    """decisions.json cannot be read, validated or replayed."""


class _EndOfInput(Exception):
    """The reader hit EOF; treat the rest of the review as rejected."""


def run_review(
    document: Document,
    claims: Sequence[Claim],
    *,
    model: str,
    input_fn: Reader | None = None,
    output: Display | None = None,
) -> dict[str, Any]:
    """Run the per-claim loop and return the decisions.json payload.

    ``input_fn``/``output`` default to the builtins and are looked up when the
    review starts, so the loop can be driven without a terminal.
    """
    input_fn = input_fn or input
    output = output or print
    clauses = {clause.path: clause for clause in document.clauses}
    output(f"共 {len(claims)} 条候选：逐条按 a 采纳 / e 改写 / r 剔除。")

    records: list[dict[str, Any]] = []
    for position, claim in enumerate(claims, start=1):
        _show_claim(claim, position, len(claims), clauses, output)
        try:
            verdict, reviewed = _ask_verdict(claim, clauses, input_fn, output)
        except _EndOfInput:
            output("输入结束：剩余候选按剔除处理。")
            records.extend(_record(rest, REJECTED, clauses) for rest in claims[position - 1 :])
            break
        records.append(_record(reviewed, verdict, clauses))

    return {
        "schema": DECISIONS_SCHEMA,
        "version": DECISIONS_VERSION,
        "document": _document_record(document),
        "model": model,
        "claims": records,
    }


def save_decisions(payload: dict[str, Any], path: str | Path) -> Path:
    """Write decisions.json as UTF-8 JSON."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target


def load_decisions(path: str | Path) -> dict[str, Any]:
    """Read and validate a decisions.json file."""
    target = Path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DecisionError(f"读不到 {target}：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise DecisionError(f"{target} 不是合法 JSON：{exc}") from exc
    _validate(payload)
    return payload


def replay_decisions(payload: dict[str, Any]) -> dict[str, Any]:
    """Re-run every anchor check against the re-parsed source document.

    The source named in decisions.json must still be present and unchanged:
    replay re-reads it, proves the doc_id and every clause snapshot digest, and
    then verifies each claim's quote against the source text. Without the
    source there is nothing to compare a snapshot to, so replay refuses rather
    than trusting an editable copy of the text.
    """
    replayed = copy.deepcopy(payload)
    clauses = bind_source(replayed)
    for record in replayed["claims"]:
        claim = Claim(
            claim_id=str(record["claim_id"]),
            conclusion=str(record["conclusion"]),
            clause_ids=tuple(tuple(path) for path in record["clause_ids"]),
            quote=str(record["quote"]),
        )
        record["anchor"] = _anchor_record(claim, clauses)
    return replayed


def bind_source(payload: dict[str, Any]) -> dict[tuple[str, ...], Clause]:
    """Re-parse the source document and verify every clause snapshot.

    Returns the source clauses by path. Raises DecisionError when the source is
    missing or changed, or when any snapshot's text no longer matches the
    document, so a hand-edited decisions.json cannot render a fabricated quote.
    """
    document = payload.get("document")
    if not isinstance(document, dict):
        raise DecisionError("decisions.json 缺少 document 记录，无法复核源文件。")
    source = document.get("source")
    doc_id = document.get("doc_id")
    if not isinstance(source, str) or not isinstance(doc_id, str) or not doc_id:
        raise DecisionError("decisions.json 没有保存源文件路径或 doc_id；请重新 draft。")

    try:
        parsed = load_source(source, doc_id)
    except DocumentError as exc:
        raise DecisionError(str(exc)) from exc

    clauses = {clause.path: clause for clause in parsed.clauses}
    for record in payload.get("claims", []):
        snapshots = record.get("clauses") or []
        if record.get("verdict") == ACCEPTED:
            _require_snapshots(record, snapshots, clauses)
        for snapshot in snapshots:
            try:
                verify_snapshot(doc_id, clauses, snapshot)
            except DocumentError as exc:
                raise DecisionError(f"{record.get('claim_id', '?')}：{exc}") from exc
    return clauses


def _require_snapshots(
    record: dict[str, Any],
    snapshots: list[dict[str, Any]],
    clauses: dict[tuple[str, ...], Clause],
) -> None:
    """An accepted claim must name existing clauses, each with a snapshot.

    The renderer prints only the clause its anchor matched, but the claim's
    other asserted labels are part of the record. Requiring a verified snapshot
    for each of them means no asserted clause can be silently dropped or
    invented between review and export.
    """
    claim_id = record.get("claim_id", "?")
    asserted = [tuple(path) for path in record.get("clause_ids") or []]
    missing = ["".join(path) for path in asserted if path not in clauses]
    if missing:
        raise DecisionError(
            f"{claim_id}：文档中不存在这些条款：" + "、".join(missing)
        )
    covered = {tuple(snapshot.get("path") or ()) for snapshot in snapshots}
    uncovered = ["".join(path) for path in asserted if path not in covered]
    if uncovered:
        raise DecisionError(
            f"{claim_id}：这些条款缺少快照，decisions.json 不完整："
            + "、".join(uncovered)
        )


def _ask_verdict(
    claim: Claim,
    clauses: dict[tuple[str, ...], Clause],
    input_fn: Reader,
    output: Display,
) -> tuple[str, Claim]:
    while True:
        answer = _read(input_fn, "  a 采纳 / e 改写 / r 剔除 > ").lower()
        if answer == "a":
            return ACCEPTED, claim
        if answer == "r":
            return REJECTED, claim
        if answer == "e":
            conclusion = _read(input_fn, "  新结论（回车保留当前结论）> ")
            quote = _read(input_fn, "  新引文（回车保留；直接粘贴原文片段可修复锚点）> ")
            reviewed = replace(
                claim,
                conclusion=conclusion or claim.conclusion,
                quote=quote or claim.quote,
            )
            output(f"  锚点  {_anchor_status(reviewed, clauses)[1]}")
            while True:
                answer = _read(input_fn, "  a 采纳 / r 剔除 > ").lower()
                if answer == "a":
                    return ACCEPTED, reviewed
                if answer == "r":
                    return REJECTED, reviewed
                output("  请输入 a 或 r。")
        output("  请输入 a、e 或 r。")


def _read(input_fn: Reader, prompt: str) -> str:
    try:
        return input_fn(prompt).strip()
    except EOFError as exc:
        raise _EndOfInput from exc


def _show_claim(
    claim: Claim,
    position: int,
    total: int,
    clauses: dict[tuple[str, ...], Clause],
    output: Display,
) -> None:
    resolved = [clauses[path] for path in claim.clause_ids if path in clauses]
    header = f"[{position}/{total}] {claim.label or '（未引用条款）'}"
    pages = sorted({clause.page for clause in resolved})
    if pages:
        header += " · 第 " + "、".join(str(page) for page in pages) + " 页"
    if resolved and resolved[0].effective_date:
        header += f" · 生效 {resolved[0].effective_date}"
    output("")
    output(header)
    if resolved:
        for clause in resolved:
            output(f"  原文  {_wrap(clause.text)}")
    else:
        output("  原文  （文档中没有找到引用到的条款）")
    output(f"  结论  {_wrap(claim.conclusion)}")
    output(f"  引文  “{_wrap(claim.quote)}”")
    output(f"  锚点  {_anchor_status(claim, clauses)[1]}")


def _wrap(text: str, width: int = 66, indent: str = "        ") -> str:
    lines = textwrap.wrap(text, width=width) or [""]
    return ("\n" + indent).join(lines)


def _anchor_status(
    claim: Claim, clauses: dict[tuple[str, ...], Clause]
) -> tuple[bool, str]:
    try:
        anchor = verify_claim(clauses, claim.clause_ids, claim.quote, claim.conclusion)
    except AnchorError as exc:
        return False, f"✗ {exc}"
    return True, f"✓ {anchor.clause.label}（第 {anchor.clause.page} 页）"


def _record(
    claim: Claim,
    verdict: str,
    clauses: dict[tuple[str, ...], Clause],
) -> dict[str, Any]:
    return {
        "claim_id": claim.claim_id,
        "verdict": verdict,
        "conclusion": claim.conclusion,
        "clause_ids": [list(path) for path in claim.clause_ids],
        "quote": claim.quote,
        "clauses": [
            clauses[path].to_dict() for path in claim.clause_ids if path in clauses
        ],
        "anchor": _anchor_record(claim, clauses),
    }


def _anchor_record(
    claim: Claim, clauses: dict[tuple[str, ...], Clause]
) -> dict[str, Any]:
    """The gate result stored alongside a claim; the renderer re-checks it."""
    try:
        anchor = verify_claim(clauses, claim.clause_ids, claim.quote, claim.conclusion)
    except AnchorError as exc:
        return {
            "ok": False,
            "clause": claim.label or None,
            "path": None,
            "page": None,
            "matched_text": None,
            "error": str(exc),
        }
    return {
        "ok": True,
        "clause": anchor.clause.label,
        "path": list(anchor.clause.path),
        "page": anchor.clause.page,
        "matched_text": anchor.matched_text,
        "error": None,
    }


def _document_record(document: Document) -> dict[str, Any]:
    return {
        "doc_id": document.doc_id,
        "source": document.source,
        "title": document.title,
        "issuer": document.issuer,
        "doc_number": document.doc_number,
        "effective_date": document.effective_date,
        "effective_date_note": document.effective_date_note,
        "article_count": len(document.articles),
        "paragraph_count": len(document.paragraphs),
    }


def _validate(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise DecisionError("decisions.json 的顶层必须是对象。")
    if payload.get("schema") != DECISIONS_SCHEMA:
        raise DecisionError(f"decisions.json 缺少 schema={DECISIONS_SCHEMA}。")
    if payload.get("version") != DECISIONS_VERSION:
        raise DecisionError(
            f"decisions.json 版本 {payload.get('version')!r} 不受支持，"
            f"当前支持 {DECISIONS_VERSION}。"
        )
    claims = payload.get("claims")
    if not isinstance(claims, list):
        raise DecisionError("decisions.json 缺少 claims 数组。")
    for record in claims:
        if not isinstance(record, dict):
            raise DecisionError("claims 里的每一项都必须是对象。")
        claim_id = record.get("claim_id", "?")
        if not isinstance(claim_id, str):
            raise DecisionError("claim_id 必须是字符串。")
        if not isinstance(record.get("conclusion"), str):
            raise DecisionError(f"claim {claim_id} 缺少 conclusion。")
        if record.get("verdict") not in VERDICTS:
            raise DecisionError(f"claim {claim_id} 的 verdict 必须是 accepted 或 rejected。")
        if not isinstance(record.get("quote"), str):
            raise DecisionError(f"claim {claim_id} 缺少 quote。")
        clause_ids = record.get("clause_ids")
        if (
            not isinstance(clause_ids, list)
            or not clause_ids
            or not all(isinstance(path, list) and path for path in clause_ids)
        ):
            raise DecisionError(f"claim {claim_id} 的 clause_ids 必须是非空路径数组。")
        if not isinstance(record.get("clauses"), list):
            raise DecisionError(f"claim {claim_id} 缺少 clauses 快照。")
