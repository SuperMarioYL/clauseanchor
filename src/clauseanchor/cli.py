"""Command line entry point for ClauseAnchor."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from clauseanchor.anchors import Anchor, AnchorError, resolve
from clauseanchor.candidates import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    CandidateError,
    OpenAICompatibleProvider,
    generate_claims,
    resolve_model,
)
from clauseanchor.demo import DEMO_MODEL_DATE, run_demo, sample_path
from clauseanchor.document import Document, DocumentError, load
from clauseanchor.render import RenderError, render
from clauseanchor.review import (
    DecisionError,
    load_decisions,
    replay_decisions,
    run_review,
    save_decisions,
)

DEFAULT_DECISIONS = "decisions.json"
DEFAULT_OUT_DIR = "out"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="clauseanchor",
        description="把政策公报解析成带条款号、页码与字符位置的条款树，"
        "逐条取舍结论候选，再出公众号长文、口播稿与图卡。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    parse = subparsers.add_parser("parse", help="解析 PDF 或纯文本，输出条款树")
    parse.add_argument("file", help="公报文件（有文字层的 PDF，或 UTF-8/GB18030 纯文本）")
    parse.add_argument("--json", action="store_true", help="输出完整 JSON，含每条的 char_span")
    parse.add_argument(
        "--resolve",
        metavar="QUOTE",
        help="校验一段原文引文锚定到哪一条；引文不是原文逐字子串时返回非零退出码",
    )

    draft = subparsers.add_parser(
        "draft", help="生成条款候选并逐条采纳/改写/剔除，写出 decisions.json"
    )
    draft.add_argument(
        "file",
        nargs="?",
        help="公报文件（有文字层的 PDF，或 UTF-8/GB18030 纯文本）",
    )
    draft.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="OpenAI 兼容模型 id 或短别名（deepseek、deepseek-r、qwen），"
        f"默认 {DEFAULT_MODEL}",
    )
    draft.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"OpenAI 兼容接口地址，默认 {DEFAULT_BASE_URL}",
    )
    draft.add_argument(
        "--api-key",
        default=None,
        help="接口密钥；默认读 CLAUSEANCHOR_API_KEY、DEEPSEEK_API_KEY 或 OPENAI_API_KEY",
    )
    draft.add_argument(
        "--max-claims", type=int, default=6, help="请求模型起草的候选条数，默认 6"
    )
    draft.add_argument(
        "-o", "--out", default=DEFAULT_DECISIONS, help=f"决定文件，默认 {DEFAULT_DECISIONS}"
    )
    draft.add_argument(
        "--decisions",
        metavar="FILE",
        help="非交互重放：读取已有 decisions.json，重跑引文校验后写到 --out，不调用模型",
    )

    render_parser = subparsers.add_parser(
        "render", help="只渲染已采纳条目；引文校验不过则拒绝导出"
    )
    render_parser.add_argument("decisions", help="draft 写出的 decisions.json")
    render_parser.add_argument(
        "-o", "--out", default=DEFAULT_OUT_DIR, help=f"输出目录，默认 {DEFAULT_OUT_DIR}/"
    )
    render_parser.add_argument(
        "--templates", default=None, help="模板目录，默认使用仓库自带的 templates/"
    )

    demo_parser = subparsers.add_parser(
        "demo", help="用内置样例离线跑完解析→候选→取舍→出稿，不需要密钥"
    )
    demo_parser.add_argument(
        "--file",
        default=None,
        help="改用其他公报文件；默认使用仓库内置样例 examples/sample_gongbao.txt",
    )
    demo_parser.add_argument(
        "-o", "--out", default=DEFAULT_OUT_DIR, help=f"输出目录，默认 {DEFAULT_OUT_DIR}/"
    )

    args = parser.parse_args(argv)

    if args.command == "parse":
        return _parse_command(args)
    if args.command == "draft":
        return _draft_command(args)
    if args.command == "render":
        return _render_command(args)
    if args.command == "demo":
        return _demo_command(args)
    parser.error(f"未知命令：{args.command}")
    return 2


def _parse_command(args: argparse.Namespace) -> int:
    try:
        document = load(args.file)
    except (DocumentError, OSError) as exc:
        print(f"clauseanchor: {exc}", file=sys.stderr)
        return 1

    anchor: Anchor | None = None
    anchor_error: str | None = None
    if args.resolve:
        try:
            anchor = resolve(document, args.resolve)
        except AnchorError as exc:
            anchor_error = str(exc)

    if args.json:
        payload: dict[str, Any] = document.to_dict()
        if args.resolve:
            payload["anchor"] = anchor.to_dict() if anchor else None
            payload["anchor_error"] = anchor_error
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        _print_report(document, anchor, anchor_error)

    if anchor_error:
        print(f"clauseanchor: {anchor_error}", file=sys.stderr)
        return 1
    return 0


def _draft_command(args: argparse.Namespace) -> int:
    if args.decisions:
        return _replay_command(args)
    if not args.file:
        print("clauseanchor: draft 需要一份公报文件（或 --decisions 重放）。", file=sys.stderr)
        return 2
    if args.max_claims < 1:
        print("clauseanchor: --max-claims 至少为 1。", file=sys.stderr)
        return 2

    try:
        document = load(args.file)
    except (DocumentError, OSError) as exc:
        print(f"clauseanchor: {exc}", file=sys.stderr)
        return 1

    print(_parse_summary(document))
    for notice in document.notices:
        print(f"注意  {notice}")

    model = resolve_model(args.model)
    provider = OpenAICompatibleProvider(
        model=model, base_url=args.base_url, api_key=args.api_key
    )
    try:
        claims = generate_claims(document, provider, max_claims=args.max_claims)
    except CandidateError as exc:
        print(f"clauseanchor: {exc}", file=sys.stderr)
        return 1

    payload = run_review(document, claims, model=model)
    target = save_decisions(payload, args.out)
    print(_decision_summary(payload))
    print(f"决定写入 {target}")
    return 0


def _replay_command(args: argparse.Namespace) -> int:
    try:
        payload = replay_decisions(load_decisions(args.decisions))
    except DecisionError as exc:
        print(f"clauseanchor: {exc}", file=sys.stderr)
        return 1
    target = save_decisions(payload, args.out)
    print("已按当前引文重跑校验（未调用模型）")
    print(_decision_summary(payload))
    print(f"决定写入 {target}")
    return 0


def _render_command(args: argparse.Namespace) -> int:
    try:
        payload = load_decisions(args.decisions)
        result = render(payload, args.out, templates_dir=args.templates)
    except (DecisionError, RenderError, OSError) as exc:
        print(f"clauseanchor: {exc}", file=sys.stderr)
        return 1

    print(f"采纳 {result.accepted} 条 · 剔除 {result.rejected} 条，已输出：")
    for path in result.outputs:
        print(f"  {path}")
    return 0


def _demo_command(args: argparse.Namespace) -> int:
    try:
        path = Path(args.file) if args.file else sample_path()
    except CandidateError as exc:
        print(f"clauseanchor: {exc}", file=sys.stderr)
        return 1

    print("离线演示：固定候选 + 固定取舍规则，不联网、不读取密钥。")
    print(f"样例  {path}")
    try:
        result = run_demo(path, args.out)
    except (DocumentError, CandidateError, DecisionError, RenderError, OSError) as exc:
        print(f"clauseanchor: {exc}", file=sys.stderr)
        return 1

    document = result.document
    print(_parse_summary(document))
    print(
        f"模型自报生效日期 {DEMO_MODEL_DATE}；"
        f"成品使用解析结果 {document.effective_date or '（未识别）'}"
    )
    print(f"采纳 {len(result.accepted)} 条：{'、'.join(result.accepted) or '（无）'}")
    print(f"拦下 {len(result.rejected)} 条：{'、'.join(result.rejected) or '（无）'}")
    print(f"决定写入 {result.decisions_path}")
    print(f"模型原稿（未校验）{result.raw_draft}")
    print("已输出：")
    for output in result.outputs:
        print(f"  {output}")
    return 0


def _parse_summary(document: Document) -> str:
    effective = document.effective_date or document.effective_date_note or "（未识别）"
    return (
        f"识别 {len(document.articles)} 条 · {len(document.paragraphs)} 款 · "
        f"生效日期 {effective}"
    )


def _decision_summary(payload: dict[str, Any]) -> str:
    claims = payload["claims"]
    accepted = [record for record in claims if record["verdict"] == "accepted"]
    broken = [record for record in accepted if not record["anchor"]["ok"]]
    lines = [f"采纳 {len(accepted)} 条 · 剔除 {len(claims) - len(accepted)} 条"]
    if broken:
        labels = "、".join(record["anchor"].get("clause") or record["claim_id"] for record in broken)
        lines.append(f"未通过校验（render 会被拦下）：{labels}")
    return "\n".join(lines)


def _print_report(
    document: Document,
    anchor: Anchor | None,
    anchor_error: str | None,
) -> None:
    print(f"{document.source}  ({document.source_type} · {document.page_count} 页)")
    if document.title:
        print(document.title)
    print(f"发文机关  {document.issuer or '（未识别）'}")
    print(f"发文字号  {document.doc_number or '（未识别）'}")
    if document.effective_date:
        print(f"生效日期  {document.effective_date}")
    elif document.effective_date_note:
        print(f"生效日期  {document.effective_date_note}")
    else:
        print("生效日期  （未识别）")
    print(f"条款      {len(document.articles)} 条 · {len(document.paragraphs)} 款")
    for notice in document.notices:
        print(f"注意      {notice}")
    print()

    for clause in document.clauses:
        indent = "    " if clause.depth > 1 else ""
        text = " ".join(clause.body.split())
        if len(text) > 48:
            text = text[:48] + "…"
        print(f"第{clause.page}页  {indent}{clause.label}  {text}")

    if anchor is not None:
        print()
        print(
            f"引文锚定  {anchor.clause.label} · 第{anchor.clause.page}页 · "
            f"char {anchor.document_span[0]}-{anchor.document_span[1]}"
        )
        print(f"原文      {anchor.matched_text}")
    elif anchor_error:
        print()
        print(f"引文校验失败  {anchor_error}")


if __name__ == "__main__":
    sys.exit(main())
