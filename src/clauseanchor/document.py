"""Load a policy document and split it into a tree of 条/款 clauses.

A clause is not a chunk: it is a citation unit that keeps the legal address
("第三条", "第二款"), the 1-based page it starts on, and the character span it
occupies in the extracted document text.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from bisect import bisect_right
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

_NUMERAL = "0-9０-９一二三四五六七八九十百千零〇两"
_GAP = r"[ \t\u3000]*"

# A clause marker may start a line or follow the previous clause's full stop.
_MARKER_RE = re.compile(
    rf"(?m)(?:^|(?<=。))[ \t\u3000]*(?P<label>第{_GAP}(?P<num>[{_NUMERAL}]+){_GAP}(?P<kind>[条款]))"
)
_DOC_NUMBER_RE = re.compile(
    r"(?P<org>[\u4e00-\u9fa5]{1,12})[ \t]*"
    r"[〔\[（(][ \t]*(?P<year>(?:19|20)\d{2})[ \t]*[〕\]）)][ \t]*"
    r"第?[ \t]*(?P<seq>\d{1,4})[ \t]*号"
)
_EFFECTIVE_RE = re.compile(
    r"自[ \t]*(?P<year>(?:19|20)\d{2})[ \t]*年[ \t]*(?P<month>\d{1,2})[ \t]*月"
    r"[ \t]*(?P<day>\d{1,2})[ \t]*日[ \t]*起[ \t]*(?:施行|实施|执行|生效)"
)
_SELF_EFFECTIVE_RE = re.compile(
    r"自[ \t]*(?:本(?:通知|办法|措施|规定|意见|决定|方案|细则)|印发|发布|公布)"
    r"[ \t]*之日(?:起)?[ \t]*(?:施行|实施|执行|生效)"
)
_EFFECTIVE_HINT_RE = re.compile(r"起[ \t]*(?:施行|实施|执行|生效)")
_EXPLICIT_ISSUER_RE = re.compile(r"发文机关[:：][ \t]*(?P<name>[\u4e00-\u9fa5]{2,30})")
_ISSUER_LINE_RE = re.compile(
    r"^[\u4e00-\u9fa5]{2,24}(?:人民政府办公厅|人民政府|委员会|管理局|厅|局|办公室)$"
)
# A 署名 or 印发 line can name the organ's 处/室/科 rather than the organ itself
# ("上海市药品监督管理局综合和规划财务处"). Keep this apart from
# _ISSUER_LINE_RE: on the document tail it is a signature, not the issuer.
_SIGNATURE_LINE_RE = re.compile(
    r"^[\u4e00-\u9fa5]{2,32}(?:人民政府办公厅|人民政府|管理委员会|委员会|"
    r"管理局|局|厅|办公室|处|室|科)$"
)
_ISSUER_FILE_HEADER_RE = re.compile(
    r"^([\u4e00-\u9fa5]{2,24}?(?:人民政府办公厅|人民政府|管理委员会|委员会|管理局|局|厅|办公室))文件$"
)
_ISSUER_ORDER_RE = re.compile(
    r"^([\u4e00-\u9fa5]{2,24}?(?:人民政府|管理委员会|委员会|局|厅|办公室))令$"
)

_TITLE_SUFFIXES = (
    "通知",
    "意见",
    "办法",
    "措施",
    "决定",
    "公告",
    "通告",
    "规定",
    "方案",
    "细则",
    "批复",
    "函",
)
_TITLE_ISSUER_SUFFIX = re.compile(r"(?:人民政府|人民政府办公厅|管理委员会|委员会|厅|局|办公室)$")

_TAIL_PREFIXES = ("附件", "附表", "抄送")
# A 抄送 block wraps across physical lines, so walk back this many lines to the
# line that starts with 抄送 instead of relying on the last line alone.
_TAIL_BLOCK_SCAN = 6
_DATE_LINE_RE = re.compile(r"^\d{4}[ \t]*年[ \t]*\d{1,2}[ \t]*月[ \t]*\d{1,2}[ \t]*日$")
_IMPRINT_DATE_RE = re.compile(
    r"^\d{4}[ \t]*年[ \t]*\d{1,2}[ \t]*月[ \t]*\d{1,2}[ \t]*日印发$"
)

# A gazette head wraps a long title across physical lines and prints the organ
# names on their own lines above it.
_TITLE_SCAN_LINES = 20
_TITLE_MAX_FRAGMENTS = 4
_TITLE_MAX_CHARS = 120
_TITLE_PUNCTUATION_TAIL = ("。", "：", ":", "，", ",")
_TITLE_QUOTE_FIXES = str.maketrans({"«": "《", "»": "》"})
# Running page numbers: "— 3 —", "第 3 页", "3/8". Bare digits are left alone
# because they can be legitimate body text.
_PAGE_NUMBER_LINE_RE = re.compile(
    r"^(?:[—–\-－]{1,2}[ \t]*[0-9０-９]{1,4}[ \t]*[—–\-－]{1,2}"
    r"|第[ \t]*[0-9０-９]{1,4}[ \t]*页(?:[ \t]*共[ \t]*[0-9０-９]{1,4}[ \t]*页)?"
    r"|[0-9０-９]{1,4}[ \t]*/[ \t]*[0-9０-９]{1,4})$"
)
_RUNNING_LINE_MAX_CHARS = 40

ARTICLE = "article"
PARAGRAPH = "paragraph"


class DocumentError(ValueError):
    """The document cannot be read or has no usable text layer."""


def clause_digest(text: str) -> str:
    """Stable digest of a clause's exact text.

    decisions.json stores it with each clause snapshot so a later replay can
    prove the snapshot was not edited after review.
    """
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class Clause:
    """One node of the clause tree."""

    doc_id: str
    path: tuple[str, ...]
    text: str
    page: int
    char_span: tuple[int, int]
    effective_date: str | None
    issuer: str | None
    doc_number: str | None
    level: str

    @property
    def label(self) -> str:
        return "".join(self.path)

    @property
    def body(self) -> str:
        """Clause text without its leading 第X条/第X款 marker."""
        match = re.match(rf"第\s*[{_NUMERAL}]+\s*[条款]", self.text)
        if not match:
            return self.text
        return self.text[match.end() :].lstrip(" \t\u3000\n")

    @property
    def depth(self) -> int:
        return len(self.path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "path": list(self.path),
            "label": self.label,
            "level": self.level,
            "text": self.text,
            "digest": clause_digest(self.text),
            "page": self.page,
            "char_span": list(self.char_span),
            "effective_date": self.effective_date,
            "issuer": self.issuer,
            "doc_number": self.doc_number,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Clause":
        """Rebuild a clause from its ``to_dict`` snapshot.

        decisions.json stores the clauses a claim relies on, so rendering can
        re-verify quotes without re-parsing the source PDF.
        """
        return cls(
            doc_id=str(data["doc_id"]),
            path=tuple(str(part) for part in data["path"]),
            text=str(data["text"]),
            page=int(data.get("page", 1)),
            char_span=tuple(int(position) for position in data.get("char_span", (0, 0))),
            effective_date=data.get("effective_date"),
            issuer=data.get("issuer"),
            doc_number=data.get("doc_number"),
            level=str(data.get("level", ARTICLE)),
        )


@dataclass(frozen=True, slots=True)
class Document:
    doc_id: str
    source: str
    source_type: str
    title: str | None
    text: str
    page_starts: tuple[int, ...]
    clauses: tuple[Clause, ...]
    preamble: str
    issuer: str | None
    doc_number: str | None
    effective_date: str | None
    effective_date_note: str | None
    notices: tuple[str, ...]

    @property
    def page_count(self) -> int:
        return len(self.page_starts)

    @property
    def articles(self) -> tuple[Clause, ...]:
        return tuple(clause for clause in self.clauses if clause.level == ARTICLE)

    @property
    def paragraphs(self) -> tuple[Clause, ...]:
        return tuple(clause for clause in self.clauses if clause.level == PARAGRAPH)

    def page_at(self, offset: int) -> int:
        """1-based page number containing a character offset."""
        return bisect_right(self.page_starts, offset)

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "source": self.source,
            "source_type": self.source_type,
            "title": self.title,
            "issuer": self.issuer,
            "doc_number": self.doc_number,
            "effective_date": self.effective_date,
            "effective_date_note": self.effective_date_note,
            "page_count": self.page_count,
            "article_count": len(self.articles),
            "paragraph_count": len(self.paragraphs),
            "preamble": self.preamble,
            "notices": list(self.notices),
            "clauses": [clause.to_dict() for clause in self.clauses],
        }


def load(path: str | Path) -> Document:
    """Read a PDF or plain-text policy document into a clause tree."""
    source = Path(path)
    if not source.exists():
        raise DocumentError(f"文件不存在：{source}")
    if source.is_dir():
        raise DocumentError(f"这是一个目录，不是文件：{source}")

    if source.suffix.lower() == ".pdf":
        text, notices = _read_pdf(source)
        return parse_text(text, source=str(source), source_type="pdf", notices=notices)

    text, notices = _read_text(source)
    return parse_text(text, source=str(source), source_type="text", notices=notices)


def parse_text(
    text: str,
    *,
    source: str = "<text>",
    source_type: str = "text",
    notices: tuple[str, ...] = (),
) -> Document:
    """Split extracted text into clauses. Pages are separated by form feeds."""
    text, starts = _paginate(text)
    doc_id = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    issuer, doc_number, effective_date, effective_date_note, meta_notices = _extract_metadata(text)

    markers = [
        (match.start("label"), match.group("kind"), _label(match.group("label")))
        for match in _MARKER_RE.finditer(text)
    ]

    def clause(start: int, end: int, path: tuple[str, ...], level: str) -> Clause:
        begin, finish = _trim_span(text, start, end)
        if end >= len(text):
            # The document tail is not clause text: 附件 lists, 抄送 lines and the
            # issuing organ's signature follow the last article.
            finish = _strip_tail_blocks(text, begin, finish)
        return Clause(
            doc_id=doc_id,
            path=path,
            text=text[begin:finish],
            page=bisect_right(starts, begin),
            char_span=(begin, finish),
            effective_date=effective_date,
            issuer=issuer,
            doc_number=doc_number,
            level=level,
        )

    clauses: list[Clause] = []
    index = 0
    while index < len(markers):
        start, kind, label = markers[index]
        if kind == "条":
            next_article = index + 1
            while next_article < len(markers) and markers[next_article][1] != "条":
                next_article += 1
            article_end = markers[next_article][0] if next_article < len(markers) else len(text)
            clauses.append(clause(start, article_end, (label,), ARTICLE))
            for position in range(index + 1, next_article):
                paragraph_start, _, paragraph_label = markers[position]
                paragraph_end = (
                    markers[position + 1][0]
                    if position + 1 < len(markers)
                    else len(text)
                )
                clauses.append(
                    clause(paragraph_start, paragraph_end, (label, paragraph_label), PARAGRAPH)
                )
            index = next_article
        else:
            paragraph_end = markers[index + 1][0] if index + 1 < len(markers) else len(text)
            clauses.append(clause(start, paragraph_end, (label,), PARAGRAPH))
            index += 1

    preamble_end = markers[0][0] if markers else len(text)
    preamble_start, preamble_finish = _trim_span(text, 0, preamble_end)

    all_notices = list(notices) + list(meta_notices)
    if not clauses:
        all_notices.append("未识别到「第X条」结构，条款树为空。")

    return Document(
        doc_id=doc_id,
        source=source,
        source_type=source_type,
        title=_extract_title(text),
        text=text,
        page_starts=starts,
        clauses=tuple(clauses),
        preamble=text[preamble_start:preamble_finish],
        issuer=issuer,
        doc_number=doc_number,
        effective_date=effective_date,
        effective_date_note=effective_date_note,
        notices=tuple(all_notices),
    )


def load_source(source: str | Path, expected_doc_id: str) -> Document:
    """Re-read the file a decisions.json was made from and prove it is the same.

    Replay and render must not trust the clause snapshots stored inside
    decisions.json: re-reading the source is what makes an edited snapshot
    unable to produce a fabricated quote.
    """
    if not source or str(source) == "<text>":
        raise DocumentError(
            "decisions.json 没有保存可重读的源文件路径；请从源文件重新运行 draft。"
        )
    path = Path(source)
    if not path.exists():
        raise DocumentError(
            f"找不到 decisions.json 记录的源文件：{path}。"
            "请在同一路径放回原文件，或从源文件重新运行 draft。"
        )
    document = load(path)
    if document.doc_id != expected_doc_id:
        raise DocumentError(
            f"源文件 {path} 的内容与 decisions.json 记录的 doc_id 不一致"
            f"（记录 {expected_doc_id}，当前 {document.doc_id}）；"
            "文件已被改动，请重新运行 draft。"
        )
    return document


def verify_snapshot(
    doc_id: str,
    clauses: Mapping[tuple[str, ...], Clause],
    snapshot: Mapping[str, Any],
) -> Clause:
    """Prove one clause snapshot is the unmodified source clause.

    Checks both directions: the snapshot must carry a digest that matches its
    own text, and that digest must match the clause re-parsed from the source
    document. Returns the source clause so callers use the source's page and
    effective date rather than the snapshot's.
    """
    try:
        stored = Clause.from_dict(snapshot)
    except (KeyError, TypeError, ValueError) as exc:
        raise DocumentError(f"条款快照不完整：{exc}") from exc

    if stored.doc_id != doc_id:
        raise DocumentError(f"{stored.label}：快照的 doc_id 与源文件不一致。")
    digest = str(snapshot.get("digest") or "")
    if not digest:
        raise DocumentError(f"{stored.label}：快照缺少正文 digest，无法复核。")
    if digest != clause_digest(stored.text):
        raise DocumentError(
            f"{stored.label}：快照正文与自带 digest 不符，decisions.json 已被改动。"
        )
    source_clause = clauses.get(stored.path)
    if source_clause is None:
        raise DocumentError(f"{stored.label}：源文件中不存在该条款。")
    if digest != clause_digest(source_clause.text):
        raise DocumentError(f"{stored.label}：快照正文与源文件原文不一致。")
    return source_clause


def _read_text(path: Path) -> tuple[str, tuple[str, ...]]:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding), ()
        except UnicodeDecodeError:
            continue
    raise DocumentError(f"无法识别文本编码（已尝试 UTF-8 与 GB18030）：{path}")


def _read_pdf(path: Path) -> tuple[str, tuple[str, ...]]:
    try:
        import pymupdf
    except ImportError:  # PyMuPDF < 1.24 only exposes the legacy name
        try:
            import fitz as pymupdf  # type: ignore[no-redef]
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise DocumentError("解析 PDF 需要 pymupdf，请先运行 `uv sync`。") from exc

    try:
        with pymupdf.open(path) as pdf:
            if pdf.needs_pass:
                raise DocumentError(f"PDF 已加密，无法读取：{path}")
            page_texts = [page.get_text("text") for page in pdf]
    except DocumentError:
        raise
    except Exception as exc:
        raise DocumentError(f"无法打开 PDF：{path}（{exc}）") from exc

    if not any(page_text.strip() for page_text in page_texts):
        raise DocumentError("PDF 没有文字层：v0.1 不支持扫描件 OCR，请先做文字识别。")

    notices = tuple(
        f"第 {number} 页没有文字层，已跳过。"
        for number, page_text in enumerate(page_texts, start=1)
        if not page_text.strip()
    )
    return "\f".join(page_texts), notices


def _paginate(text: str) -> tuple[str, tuple[int, ...]]:
    """Join pages with form feeds and record where each page starts.

    Running page numbers and repeated header/footer lines are removed before
    joining so they cannot be swallowed into a clause that spans a page break.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    pages = _strip_running_lines(text.split("\f"))
    chunks: list[str] = []
    starts: list[int] = []
    cursor = 0
    for index, page in enumerate(pages):
        starts.append(cursor)
        chunk = page if index == len(pages) - 1 or page.endswith("\n") else page + "\n"
        chunks.append(chunk)
        cursor += len(chunk)
    return "".join(chunks), tuple(starts)


def _strip_running_lines(pages: list[str]) -> list[str]:
    """Drop page numbers and running titles from the per-page head and tail."""
    lines_per_page: list[list[str]] = []
    for page in pages:
        lines = page.splitlines()
        _drop_page_number_block(lines, from_start=True)
        _drop_page_number_block(lines, from_start=False)
        lines_per_page.append(lines)

    if len(lines_per_page) >= 2:
        _drop_repeated_edge_lines(lines_per_page, from_start=True)
        _drop_repeated_edge_lines(lines_per_page, from_start=False)

    return ["\n".join(lines) for lines in lines_per_page]


def _drop_page_number_block(lines: list[str], *, from_start: bool) -> None:
    """Remove one page-number block (one to three physical lines) at an edge."""
    for _ in range(2):
        # Blank edge lines would hide the block; they carry no content either.
        while lines:
            edge = 0 if from_start else len(lines) - 1
            if lines[edge].strip():
                break
            del lines[edge]
        if not lines:
            return

        removed = False
        for size in (1, 2, 3):
            if size > len(lines):
                break
            positions = (
                list(range(size))
                if from_start
                else list(range(len(lines) - 1, len(lines) - 1 - size, -1))
            )
            if any(not lines[position].strip() for position in positions):
                break
            joined = "".join(lines[position].strip() for position in positions)
            if _PAGE_NUMBER_LINE_RE.match(joined):
                for position in sorted(positions, reverse=True):
                    del lines[position]
                removed = True
                break
        if not removed:
            return


def _drop_repeated_edge_lines(lines_per_page: list[list[str]], *, from_start: bool) -> None:
    """Remove a short line repeated in the same edge position on most pages."""
    edges = [
        _edge_line(lines, from_start=from_start) for lines in lines_per_page
    ]
    counts = Counter(line for line in edges if line)
    threshold = max(2, (len(lines_per_page) + 1) // 2)
    repeated = {
        line for line, count in counts.items() if count >= threshold and _is_running_line(line)
    }
    if not repeated:
        return
    for lines, edge in zip(lines_per_page, edges):
        if edge in repeated:
            positions = range(len(lines)) if from_start else range(len(lines) - 1, -1, -1)
            for position in positions:
                if lines[position].strip():
                    del lines[position]
                    break


def _edge_line(lines: list[str], *, from_start: bool) -> str:
    positions = range(len(lines)) if from_start else range(len(lines) - 1, -1, -1)
    for position in positions:
        line = lines[position].strip()
        if line:
            return line
    return ""


def _is_running_line(line: str) -> bool:
    return (
        len(line) <= _RUNNING_LINE_MAX_CHARS
        and not _MARKER_RE.match(line)
        and not line.endswith(("。", "，", ",", ".", ";", "；"))
    )


def _label(raw: str) -> str:
    return re.sub(r"\s+", "", raw)


def _trim_span(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start] in " \t\u3000\n":
        start += 1
    while end > start and text[end - 1] in " \t\u3000\n":
        end -= 1
    return start, end


def _strip_tail_blocks(text: str, start: int, end: int) -> int:
    """Drop trailing 附件/抄送 blocks, the signature and its date.

    They follow the last clause on the document tail but are not part of it.
    Works for both extracted PDF text (no blank lines) and plain text. A 抄送
    block may wrap across physical lines, so its continuation lines are removed
    together with the line that starts with 抄送.
    """
    segment = text[start:end].rstrip(" \t\u3000\n")
    while True:
        lines = segment.splitlines()
        if not lines:
            # The whole span looked like a tail block; keep it rather than return empty text.
            return end
        cut = _tail_block_start(lines)
        if cut is None or cut == 0:
            # No tail block here, or the whole span is one; keep it rather than
            # empty the clause.
            return start + len(segment)
        segment = "\n".join(lines[:cut]).rstrip(" \t\u3000\n")


def _tail_block_start(lines: list[str]) -> int | None:
    """Index of the first line of the trailing 文尾 block, if there is one."""
    bottom = len(lines)
    while bottom > 0 and not lines[bottom - 1].strip():
        bottom -= 1
    index = bottom
    while index > 0:
        if _is_tail_line(lines[index - 1].strip()):
            index -= 1
            continue
        header = _wrapped_tail_header(lines, index)
        if header is not None:
            return header
        break
    return index if index < bottom else None


def _wrapped_tail_header(lines: list[str], stop: int) -> int | None:
    """Find the 抄送/附件 line a wrapped continuation line belongs to.

    Scanning up from the bottom, the last physical line of a wrapped 抄送 block
    does not itself start with 抄送. Walk back a few lines to its header so the
    whole block is removed at once; stop if a clause marker appears first.
    """
    for position in range(stop - 1, max(stop - 1 - _TAIL_BLOCK_SCAN, -1), -1):
        line = lines[position].strip()
        if not line:
            continue
        if line.startswith(_TAIL_PREFIXES):
            return position
        if _MARKER_RE.match(line):
            return None
    return None


def _is_tail_line(line: str) -> bool:
    normalized = unicodedata.normalize("NFKC", line)
    return bool(
        line.startswith(_TAIL_PREFIXES)
        or _DATE_LINE_RE.match(normalized)
        or _IMPRINT_DATE_RE.match(normalized)
        or _PAGE_NUMBER_LINE_RE.match(normalized)
        or _ISSUER_LINE_RE.match(line)
        or _SIGNATURE_LINE_RE.match(line)
    )


def _extract_title(text: str) -> str | None:
    """Join a wrapped title and keep the issuing organ names printed above it.

    Real gazette heads set the organ names on their own lines and break a long
    title mid-phrase ("...登记档案" / "查阅办法》的通知"). Reading one physical
    line either finds no title or returns half of it, so append following lines
    until the title ends with a document suffix, stopping at a 发文字号 or
    主送机关 line so body text is never swallowed.
    """
    lines = [raw_line.strip() for raw_line in text.splitlines()[:_TITLE_SCAN_LINES]]
    for position, line in enumerate(lines):
        if "关于" not in line or len(line) < 8 or not _is_title_line(line):
            continue
        title = line
        for fragment in lines[position + 1 : position + 1 + _TITLE_MAX_FRAGMENTS]:
            if title.endswith(_TITLE_SUFFIXES):
                break
            if not fragment or not _is_title_line(fragment):
                break
            if len(title) + len(fragment) > _TITLE_MAX_CHARS:
                break
            title += fragment
        if not title.endswith(_TITLE_SUFFIXES):
            continue
        return _with_issuer_names(lines, position, title.translate(_TITLE_QUOTE_FIXES))
    return None


def _is_title_line(line: str) -> bool:
    """A line that can be part of a title rather than the 主送机关 or 发文字号."""
    normalized = unicodedata.normalize("NFKC", line)
    return (
        len(line) <= _TITLE_MAX_CHARS
        and not line.endswith(_TITLE_PUNCTUATION_TAIL)
        and not _DOC_NUMBER_RE.match(normalized)
        and not _DATE_LINE_RE.match(normalized)
        and not _IMPRINT_DATE_RE.match(normalized)
        and not _MARKER_RE.match(line)
    )


def _with_issuer_names(lines: list[str], position: int, title: str) -> str:
    """Prefix the organ names in the unbroken block directly above the title."""
    names: list[str] = []
    for back in range(position - 1, -1, -1):
        candidate = lines[back]
        if not candidate:
            continue
        if (
            len(candidate) > 30
            or "关于" in candidate
            or not _TITLE_ISSUER_SUFFIX.search(candidate)
        ):
            break
        names.append(candidate)
    prefix = [name for name in reversed(names) if not title.startswith(name)]
    if not prefix:
        return title
    return " ".join(prefix) + title


def _extract_metadata(
    text: str,
) -> tuple[str | None, str | None, str | None, str | None, tuple[str, ...]]:
    notices: list[str] = []

    # Real gazettes often use full-width digits in 发文字号 and dates.
    normalized = unicodedata.normalize("NFKC", text)

    issuer = None
    explicit = _EXPLICIT_ISSUER_RE.search(normalized)
    if explicit:
        issuer = explicit.group("name")
    else:
        issuer = _find_issuer(normalized)

    doc_number = None
    number_match = _DOC_NUMBER_RE.search(normalized)
    if number_match:
        doc_number = (
            f"{number_match.group('org')}〔{number_match.group('year')}〕"
            f"{int(number_match.group('seq'))}号"
        )

    effective_date: str | None = None
    effective_date_note: str | None = None
    date_match = _EFFECTIVE_RE.search(normalized)
    if date_match:
        try:
            parsed = date(
                int(date_match.group("year")),
                int(date_match.group("month")),
                int(date_match.group("day")),
            )
        except ValueError:
            notices.append(f"原文施行日期不合法：{date_match.group(0)}")
        else:
            effective_date = parsed.isoformat()
    else:
        self_match = _SELF_EFFECTIVE_RE.search(normalized)
        if self_match:
            effective_date_note = f"原文为「{self_match.group(0)}」，未给出具体日期"
        elif _EFFECTIVE_HINT_RE.search(normalized):
            notices.append("文本提到施行日期，但未能解析出具体日期，请人工核对。")

    return issuer, doc_number, effective_date, effective_date_note, tuple(notices)


def _find_issuer(text: str) -> str | None:
    """Pick the issuing organ from a red header ("X文件" / "X令") or a signature line."""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        match = _ISSUER_FILE_HEADER_RE.match(line) or _ISSUER_ORDER_RE.match(line)
        if match:
            return match.group(1)
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if _ISSUER_LINE_RE.match(line):
            return line
    return None
