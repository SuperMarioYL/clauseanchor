"""Render the publishing pack from accepted claims only.

The gate is re-checked here rather than trusted from decisions.json: the source
document named in decisions.json is re-parsed, every clause snapshot must match
it by digest, and every accepted claim's quote must be a verbatim substring of
the clause it asserts. The same gate also requires every 第X条 / 第X条Y款 named
in a claim's conclusion to be one of that claim's verified clauses, so a real
quote cannot carry a fabricated 条款号 into the pack. Each rendered item takes
its label, page and effective date from the one clause the verified anchor
matched, never from the full list of clauses a claim asserted. Rejected claims
never reach a template.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from clauseanchor.anchors import Anchor, AnchorError, verify_claim
from clauseanchor.review import DecisionError, bind_source

LONGFORM_TEMPLATE = "wechat_longform.j2"
KOUBO_TEMPLATE = "koubo_script.j2"
CARDS_TEMPLATE = "cards.html"
LONGFORM_OUTPUT = "长文.md"
KOUBO_OUTPUT = "koubo.md"
CARDS_OUTPUT = "cards.html"


class RenderError(RuntimeError):
    """The pack cannot be rendered."""


class ExportBlocked(RenderError):
    """An accepted claim's quote or prose clause number does not verify."""


@dataclass(frozen=True, slots=True)
class RenderResult:
    outputs: tuple[Path, ...]
    accepted: int
    rejected: int


def render(
    decisions: dict[str, Any],
    out_dir: str | Path,
    *,
    templates_dir: str | Path | None = None,
) -> RenderResult:
    """Write 长文.md, koubo.md and cards.html from the accepted claims."""
    accepted = [
        record
        for record in decisions.get("claims", [])
        if record.get("verdict") == "accepted"
    ]
    if not accepted:
        raise ExportBlocked("没有已采纳的条目：先用 draft 逐条采纳，再导出。")

    anchors = verify_accepted(decisions)

    environment = _environment(templates_dir)
    context = _context(decisions, accepted, anchors)
    out = Path(out_dir)
    outputs = (
        _write(
            out / LONGFORM_OUTPUT,
            environment.get_template(LONGFORM_TEMPLATE).render(context),
        ),
        _write(
            out / KOUBO_OUTPUT,
            environment.get_template(KOUBO_TEMPLATE).render(context),
        ),
        _write(
            out / CARDS_OUTPUT,
            environment.get_template(CARDS_TEMPLATE).render(context),
        ),
    )
    total = len(decisions.get("claims", []))
    return RenderResult(outputs=outputs, accepted=len(accepted), rejected=total - len(accepted))


def verify_accepted(decisions: dict[str, Any]) -> list[Anchor]:
    """Verify the source binding, every accepted quote and every prose 条款号.

    Raises ExportBlocked listing every failure, each prefixed with the claim id
    and naming the asserted 条款号 (or the fabricated one a conclusion named).
    Returns one anchor per accepted claim, in the same order as the accepted
    records.
    """
    try:
        clauses = bind_source(decisions)
    except DecisionError as exc:
        raise ExportBlocked(f"导出被拦下：{exc}") from exc

    anchors: list[Anchor] = []
    failures: list[str] = []
    for record in decisions.get("claims", []):
        if record.get("verdict") != "accepted":
            continue
        paths = [tuple(path) for path in record.get("clause_ids", [])]
        try:
            anchors.append(
                verify_claim(
                    clauses,
                    paths,
                    record["quote"],
                    record.get("conclusion", ""),
                )
            )
        except AnchorError as exc:
            failures.append(f"{record.get('claim_id', '?')}：{exc}")
    if failures:
        raise ExportBlocked(
            "导出被拦下：以下已采纳条目的引文或结论中的条款号未通过校验，"
            "请回到 draft 用 e 修正后再导出。\n  · " + "\n  · ".join(failures)
        )
    return anchors


def _environment(templates_dir: str | Path | None) -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_resolve_templates(templates_dir))),
        autoescape=select_autoescape(enabled_extensions=("html",), default_for_string=False),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


def _resolve_templates(templates_dir: str | Path | None) -> Path:
    if templates_dir is not None:
        directory = Path(templates_dir)
        if not directory.is_dir():
            raise RenderError(f"模板目录不存在：{directory}")
        return directory
    module = Path(__file__).resolve()
    for directory in (module.parent / "templates", module.parents[2] / "templates"):
        if directory.is_dir():
            return directory
    raise RenderError("找不到 templates/ 目录；请从仓库根目录运行，或用 --templates 指定。")


def _context(
    decisions: dict[str, Any],
    accepted: list[dict[str, Any]],
    anchors: list[Anchor],
) -> dict[str, Any]:
    document = decisions.get("document") or {}
    items = [
        _item(record, anchor, document)
        for record, anchor in zip(accepted, anchors)
    ]

    meta: list[str] = []
    if document.get("issuer"):
        meta.append(document["issuer"])
    if document.get("doc_number"):
        meta.append(document["doc_number"])
    if document.get("effective_date"):
        meta.append(f"自 {document['effective_date']} 起施行")
    elif document.get("effective_date_note"):
        meta.append(document["effective_date_note"])

    return {
        "title": document.get("title") or "政策解读",
        "meta_line": " · ".join(meta),
        "doc_reference": document.get("doc_number")
        or document.get("title")
        or "公开发布的政策原文",
        "doc_number": document.get("doc_number"),
        "effective_date": document.get("effective_date"),
        "claims": items,
        "highlights": items[:3],
    }


def _item(
    record: dict[str, Any],
    anchor: Anchor,
    document: dict[str, Any],
) -> dict[str, Any]:
    """One rendered entry, addressed by the clause the anchor actually matched.

    A claim may assert several clauses; only the one holding the verified quote
    can supply the label, page and effective date, so the pack never presents
    an unchecked 条款号 as if the quote came from it.
    """
    clause = anchor.clause
    return {
        "label": clause.label,
        "conclusion": record.get("conclusion", ""),
        "quote": record.get("quote", ""),
        "page_text": f"第 {clause.page} 页",
        "effective_date": clause.effective_date or document.get("effective_date"),
    }


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path
