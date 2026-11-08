"""Regenerate assets/demo/clause-diff.gif from a real offline demo run.

The left panel is ``demo-raw-model-draft.md`` written by ``clauseanchor demo``:
the fixed candidate set, including one quote the model rewrote and one quote
filed under the wrong clause number. The right panel is the rendered 长文.md
from the same run, which contains only the claims that passed the anchor gate.
Nothing here is hand-typed prose: every string comes from the demo's artifacts.

Requires PyMuPDF (already a runtime dependency) and ffmpeg on PATH.

Run from the repository root:

    uv run python assets/demo/make_clause_diff.py

The command in the footer can be changed with --file/--out.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[2]
REAL_PDF = ROOT / "examples" / "real" / "sh_gov_investment_management.pdf"
OUT_DIR = ROOT / "assets" / "demo"
GIF_NAME = "clause-diff.gif"

PAGE_WIDTH = 960
PAGE_HEIGHT = 720
MARGIN = 24
GAP = 16
PANEL_TOP = 104
PANEL_BOTTOM = 656
COLUMN = (PAGE_WIDTH - 2 * MARGIN - GAP) // 2

INK = (0.12, 0.14, 0.16)
MUTED = (0.42, 0.45, 0.48)
PAPER = (0.984, 0.980, 0.969)
WHITE = (1.0, 1.0, 1.0)
LINE = (0.85, 0.87, 0.89)
RED = (0.706, 0.137, 0.094)
RED_BG = (0.992, 0.945, 0.937)
TEAL = (0.055, 0.40, 0.38)
TEAL_BG = (0.929, 0.968, 0.957)

FONT = "china-s"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", default=str(REAL_PDF), help="document for the demo run")
    parser.add_argument("--out", default=str(OUT_DIR / GIF_NAME), help="GIF output path")
    args = parser.parse_args()

    demo_dir = Path(tempfile.mkdtemp(prefix="clauseanchor-demo-"))
    try:
        run = _run_demo(Path(args.file), demo_dir)
        frames = _draw_frames(demo_dir, run)
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        _assemble(frames, target)
        print(f"{target}  {target.stat().st_size} bytes")
        print(
            f"accepted {run['accepted']} · rejected {run['rejected']} · "
            f"source {Path(args.file).name}"
        )
    finally:
        shutil.rmtree(demo_dir, ignore_errors=True)


def _run_demo(source: Path, out_dir: Path) -> dict:
    from clauseanchor.cli import main as cli_main

    code = cli_main(["demo", "--file", str(source), "-o", str(out_dir)])
    if code != 0:
        raise SystemExit(f"clauseanchor demo failed with exit code {code}")

    decisions = json.loads((out_dir / "decisions.json").read_text(encoding="utf-8"))
    accepted = [r for r in decisions["claims"] if r["verdict"] == "accepted"]
    rejected = [r for r in decisions["claims"] if r["verdict"] == "rejected"]
    return {
        "document": decisions["document"],
        "claims": decisions["claims"],
        "accepted": len(accepted),
        "rejected": len(rejected),
        "raw": (out_dir / "demo-raw-model-draft.md").read_text(encoding="utf-8"),
        "longform": (out_dir / "长文.md").read_text(encoding="utf-8"),
    }


def _draw_frames(demo_dir: Path, run: dict) -> list[Path]:
    raw_blocks = _raw_blocks(run["raw"])
    title, meta, sections = _longform_blocks(run["longform"])
    checks = _check_lines(run["claims"])
    doc_number = run["document"].get("doc_number") or ""

    frame_one = demo_dir / "panel1.png"
    page = _page()
    _brand(page)
    _panel(page, MARGIN, "裸模型稿 · 未经校验", RED, RED_BG)
    _draw_rect(page, MARGIN + 14, 158, COLUMN - 28, PANEL_BOTTOM - 158)
    _blocks(page, MARGIN + 26, 172, COLUMN - 52, raw_blocks, 11.5)
    _panel(
        page,
        MARGIN + COLUMN + GAP,
        "ClauseAnchor 稿 · 待核对",
        MUTED,
        (0.96, 0.96, 0.95),
    )
    _text(
        page,
        MARGIN + COLUMN + GAP + 14,
        182,
        COLUMN - 28,
        f"正在核对 {len(run['claims'])} 条候选",
        10.5,
        MUTED,
    )
    _text(
        page,
        MARGIN + COLUMN + GAP + 14,
        208,
        COLUMN - 28,
        "引文必须逐字出现在所引条款的原文里，条款号也必须能回指到同一条。",
        10.5,
        MUTED,
    )
    _footer(
        page,
        "同一份公报，模型直接写出的稿子。引文被改写、条款号错位，生效日期也是模型自报的。",
    )
    page.get_pixmap(dpi=72).save(frame_one)
    page.parent.close()

    frame_two = demo_dir / "panel2.png"
    page = _page()
    _brand(page)
    _panel(page, MARGIN, "裸模型稿 · 未经校验", RED, RED_BG)
    _draw_rect(page, MARGIN + 14, 158, COLUMN - 28, PANEL_BOTTOM - 158)
    _blocks(page, MARGIN + 26, 172, COLUMN - 52, raw_blocks, 11.5)
    _panel(page, MARGIN + COLUMN + GAP, "逐条核对 · 引文必须是原文逐字子串", TEAL, TEAL_BG)
    _check_list(page, MARGIN + COLUMN + GAP + 14, checks)
    _footer(
        page,
        "ClauseAnchor 只放行引文与条款号都能在原文里逐字找到的条目；其余条目连同错误一起挡在导出之前。",
    )
    page.get_pixmap(dpi=72).save(frame_two)
    page.parent.close()

    frame_three = demo_dir / "panel3.png"
    page = _page()
    _brand(page)
    _panel(page, MARGIN, "裸模型稿 · 未经校验", RED, RED_BG)
    _draw_rect(page, MARGIN + 14, 158, COLUMN - 28, PANEL_BOTTOM - 158)
    _blocks(page, MARGIN + 26, 172, COLUMN - 52, raw_blocks, 11.5)
    _panel(
        page,
        MARGIN + COLUMN + GAP,
        "ClauseAnchor 稿 · 引文已校验",
        TEAL,
        TEAL_BG,
    )
    _article(page, MARGIN + COLUMN + GAP + 14, title, meta, sections, doc_number)
    _footer(
        page,
        f"成品：{run['accepted']} 条结论，每条带第X条、页码、生效日期与原文引文；"
        f"{run['rejected']} 条未通过校验的候选不在成品里。",
    )
    page.get_pixmap(dpi=72).save(frame_three)
    page.parent.close()

    return _repeat_frames(frame_one, frame_two, frame_three, demo_dir)


def _repeat_frames(one: Path, two: Path, three: Path, directory: Path) -> list[Path]:
    """2 s on the raw draft, 3 s on the gate, 6 s on the finished pack."""
    plan = [(one, 2), (two, 3), (three, 6)]
    frames: list[Path] = []
    for source, seconds in plan:
        for _ in range(seconds):
            target = directory / f"frame{len(frames) + 1:02d}.png"
            shutil.copyfile(source, target)
            frames.append(target)
    return frames


def _assemble(frames: list[Path], target: Path) -> None:
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg is required to assemble the GIF")
    pattern = str(frames[0].parent / "frame%02d.png")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-framerate",
            "1",
            "-i",
            pattern,
            "-vf",
            "split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];"
            "[b][p]paletteuse=dither=bayer:bayer_scale=4",
            "-loop",
            "0",
            str(target),
        ],
        check=True,
    )


def _page() -> pymupdf.Page:
    document = pymupdf.open()
    page = document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    page.draw_rect(
        pymupdf.Rect(0, 0, PAGE_WIDTH, PAGE_HEIGHT),
        color=None,
        fill=PAPER,
        width=0,
    )
    return page


def _brand(page: pymupdf.Page) -> None:
    page.insert_text((MARGIN, 46), "ClauseAnchor", fontname="hebo", fontsize=21, color=INK)
    page.insert_text(
        (MARGIN, 68),
        "政策公报 → 每条结论都挂回原文条款的解读稿",
        fontname=FONT,
        fontsize=11,
        color=MUTED,
    )
    page.insert_text(
        (PAGE_WIDTH - MARGIN - 168, 46),
        "before / after",
        fontname="hebo",
        fontsize=10,
        color=MUTED,
    )
    page.draw_line(
        pymupdf.Point(MARGIN, 84),
        pymupdf.Point(PAGE_WIDTH - MARGIN, 84),
        color=LINE,
        width=0.8,
    )


def _panel(
    page: pymupdf.Page,
    x: int,
    label: str,
    accent: tuple[float, float, float],
    background: tuple[float, float, float],
) -> None:
    rect = pymupdf.Rect(x, PANEL_TOP, x + COLUMN, PANEL_BOTTOM)
    page.draw_rect(rect, color=LINE, fill=WHITE, width=0.8)
    header = pymupdf.Rect(x, PANEL_TOP, x + COLUMN, PANEL_TOP + 38)
    page.draw_rect(header, color=None, fill=background, width=0)
    page.draw_rect(
        pymupdf.Rect(x, PANEL_TOP, x + 3, PANEL_BOTTOM), color=None, fill=accent, width=0
    )
    page.insert_text(
        (x + 14, PANEL_TOP + 24),
        label,
        fontname=FONT,
        fontsize=11,
        color=accent,
    )


def _draw_rect(page: pymupdf.Page, x: int, y: int, width: int, height: int) -> None:
    page.draw_line(
        pymupdf.Point(x, y - 8),
        pymupdf.Point(x + width, y - 8),
        color=LINE,
        width=0.5,
    )


def _blocks(
    page: pymupdf.Page,
    x: int,
    y: int,
    width: int,
    blocks: list[tuple[str, str, tuple[float, float, float], float]],
    spacing: float = 8,
) -> None:
    for kind, text, color, size in blocks:
        if kind == "gap":
            y += spacing
            continue
        used = _text(page, x, y, width, text, size, color)
        y += used + (4 if kind == "label" else spacing)


def _text(
    page: pymupdf.Page,
    x: float,
    y: float,
    width: float,
    text: str,
    size: float,
    color: tuple[float, float, float],
) -> float:
    rect = pymupdf.Rect(x, y, x + width, PAGE_HEIGHT - 8)
    remaining = page.insert_textbox(
        rect,
        text,
        fontname=FONT,
        fontsize=size,
        color=color,
        lineheight=1.45,
    )
    if remaining < 0:
        return rect.height
    return rect.height - remaining


def _check_list(page: pymupdf.Page, x: int, checks: list[dict]) -> None:
    y = 172.0
    width = COLUMN - 28
    for check in checks:
        status = "通过" if check["ok"] else "拦下"
        color = TEAL if check["ok"] else RED
        head = f"{check['claim_id']}  {check['label']}"
        page.insert_textbox(
            pymupdf.Rect(x, y, x + width - 58, y + 40),
            head,
            fontname=FONT,
            fontsize=11.5,
            color=INK,
        )
        page.insert_text(
            (x + width - 54, y + 13),
            status,
            fontname=FONT,
            fontsize=10.5,
            color=color,
        )
        y += 26
        if not check["ok"]:
            used = _text(page, x, y, width, check["error"], 10, MUTED)
            y += used + 8
        else:
            y += 12

    y += 8
    page.draw_line(
        pymupdf.Point(x, y),
        pymupdf.Point(x + width, y),
        color=LINE,
        width=0.6,
    )
    y += 14
    _text(
        page,
        x,
        y,
        width,
        "校验方式：去掉空白、统一全角半角后，引文必须是所引条款原文的精确子串。"
        "改写一处，或把别条原文挂到本条，都会被拦下并指出条款号。",
        10,
        MUTED,
    )


def _article(
    page: pymupdf.Page,
    x: int,
    title: str,
    meta: str,
    sections: list[tuple[str, str, str, str]],
    doc_number: str,
) -> None:
    y = 166.0
    width = COLUMN - 28
    used = _text(page, x, y, width, title, 13, INK)
    y += used + 6
    used = _text(page, x, y, width, meta, 9.5, MUTED)
    y += used + 14

    for label, conclusion, quote, source in sections:
        page.insert_text((x, y + 11), label, fontname=FONT, fontsize=11, color=TEAL)
        y += 20
        used = _text(page, x, y, width, conclusion, 11, INK)
        y += used + 6
        used = _text(page, x, y, width, quote, 10, MUTED)
        y += used + 4
        used = _text(page, x, y, width, source, 9, MUTED)
        y += used + 14

    page.draw_line(
        pymupdf.Point(x, y),
        pymupdf.Point(x + width, y),
        color=LINE,
        width=0.6,
    )
    y += 12
    _text(
        page,
        x,
        y,
        width,
        f"依据 {doc_number} 整理，只做条文摘录与解读，不构成法律意见。",
        9,
        MUTED,
    )


def _footer(page: pymupdf.Page, text: str) -> None:
    page.draw_line(
        pymupdf.Point(MARGIN, 668),
        pymupdf.Point(PAGE_WIDTH - MARGIN, 668),
        color=LINE,
        width=0.8,
    )
    _text(page, MARGIN, 680, PAGE_WIDTH - 2 * MARGIN, text, 9.5, MUTED)


def _raw_blocks(markdown: str) -> list[tuple[str, str, tuple[float, float, float], float]]:
    """Flatten the raw draft; the date note lives in the frame footer instead."""
    blocks: list[tuple[str, str, tuple[float, float, float], float]] = []
    for line in markdown.splitlines():
        line = line.strip()
        if not line or line.startswith("# ") or line.startswith("> "):
            continue
        if line.startswith("## "):
            blocks.append(("label", line[3:], INK, 10))
        elif line.startswith("引文："):
            blocks.append(("body", line, MUTED, 9.5))
            blocks.append(("gap", "", MUTED, 7))
        else:
            blocks.append(("body", line, INK, 10))
    return blocks


def _longform_blocks(markdown: str) -> tuple[str, str, list[tuple[str, str, str, str]]]:
    lines = [line.rstrip() for line in markdown.splitlines()]
    title = next(line[2:] for line in lines if line.startswith("# "))
    meta = next(line[2:] for line in lines if line.startswith("> "))

    sections: list[tuple[str, str, str, str]] = []
    label = ""
    conclusion = ""
    quote = ""
    source = ""
    for line in lines:
        match = re.match(r"^## (第.+条)$", line)
        if match:
            if label:
                sections.append((label, conclusion, quote, source))
            label = match.group(1)
            conclusion = quote = source = ""
            continue
        if not label or not line or line.startswith("#") or line.startswith("> **"):
            continue
        if line.startswith("> 原文："):
            quote = line[2:]
        elif line.startswith("出处："):
            source = line
        elif not conclusion and not line.startswith(">"):
            conclusion = line
    if label:
        sections.append((label, conclusion, quote, source))
    return title, meta, sections[:2]


def _check_lines(claims: list[dict]) -> list[dict]:
    checks = []
    for record in claims:
        anchor = record.get("anchor") or {}
        checks.append(
            {
                "claim_id": record["claim_id"],
                "label": anchor.get("clause") or "".join(record["clause_ids"][0]),
                "ok": bool(anchor.get("ok")),
                "error": anchor.get("error") or "",
            }
        )
    return checks


if __name__ == "__main__":
    main()
