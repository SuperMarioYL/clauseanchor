"""Resolve a verbatim quote back to the clause that contains it.

This module is the gate between a draft and an export. A quote is accepted when
it matches the clause text after whitespace removal and full-width/half-width
unification (NFKC). Clause labels are kept as written; they are never rewritten
to make a quote fit.

A quote must also be long enough to be evidence (at least
``MIN_QUOTE_CHARS`` normalized characters) and must not consist only of
punctuation or filler words. ``verify_quote(clause, quote)`` binds a quote to
the one clause a claim asserts. ``verify_claim_quote`` applies that same check
to every clause a claim cites and names them all when none matches.
``verify_conclusion_clauses`` closes the other half of the gate: a conclusion
may only name 第X条 / 第X条Y款 labels belonging to the clauses the claim
asserted and that exist in the document. ``verify_claim`` runs both checks --
the quote can be real while the prose is not. ``resolve()`` is a global lookup
for exploration only: it refuses a quote that lands in more than one distinct
clause. The m2 review gate and the m3 export gate must never call ``resolve()``;
a claim's quote is only valid for the clauses the claim itself asserted.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Iterator

from clauseanchor.document import Clause, Document

MIN_QUOTE_CHARS = 8

# Punctuation plus common function words. A quote made only of these carries no
# citable content even when it is long enough by character count.
_FILLER_CHARS = frozenset(
    "。，、；：？！“”‘’（）《》〈〉【】〔〕…—·.,;:?!()[]{}<>\"'\u3000 \t\r\n"
    "的了吗呢吧啊呀哦和与及或等之其该本"
)


class AnchorError(ValueError):
    """A quote is not a verbatim substring of the clause it was attached to."""


@dataclass(frozen=True, slots=True)
class Anchor:
    """A verified quote: which clause it hits and where, in both coordinate spaces."""

    clause: Clause
    quote: str
    matched_text: str
    clause_span: tuple[int, int]
    document_span: tuple[int, int]

    @property
    def path(self) -> tuple[str, ...]:
        return self.clause.path

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.clause.doc_id,
            "path": list(self.clause.path),
            "label": self.clause.label,
            "page": self.clause.page,
            "quote": self.quote,
            "matched_text": self.matched_text,
            "clause_span": list(self.clause_span),
            "document_span": list(self.document_span),
        }


def normalize(text: str) -> str:
    """Comparison-only form: NFKC per character, whitespace dropped."""
    return "".join(character for character, _ in _normalized_chars(text))


def _label_key(text: str) -> str:
    """A clause label in comparison form (the same normalization as a quote)."""
    return normalize(text)


def verify_quote(clause: Clause, quote: str) -> tuple[int, int]:
    """Return the quote's [start, end) span inside `clause.text`.

    Raises AnchorError when the quote is too short, is only punctuation/filler,
    or is not an exact substring after normalization. This is the gate m2 must
    use: it binds the quote to the clause the model asserted, so a quote that
    belongs to a different clause fails instead of being silently re-pointed.
    """
    needle = _validate_quote(quote, clause.label)

    haystack, mapping = _normalize_with_map(clause.text)
    position = haystack.find(needle)
    if position < 0:
        raise AnchorError(
            f"{clause.label}：引文不是该条原文的逐字子串"
            "（去空白、统一全角半角后仍不匹配）。"
        )
    return mapping[position], mapping[position + len(needle) - 1] + 1


def verify_claim_quote(
    clauses: Mapping[tuple[str, ...], Clause],
    clause_ids: Iterable[tuple[str, ...]],
    quote: str,
) -> Anchor:
    """Bind a claim's quote to one of the clauses the claim cites.

    This is the m2/m3 gate. It checks the quote only against clauses the claim
    asserted, in the order asserted, and returns the first clause that contains
    it verbatim. A quote that really lives in another clause therefore fails
    instead of being silently re-pointed. When nothing matches, the error names
    every asserted clause -- including ones missing from the document -- so the
    author knows which 条款号 to fix.
    """
    paths = [tuple(path) for path in clause_ids]
    if not paths:
        raise AnchorError("该结论没有引用任何条款。")

    failures: list[str] = []
    for path in paths:
        clause = clauses.get(path)
        if clause is None:
            failures.append(f"{''.join(path)}：文档中不存在该条款。")
            continue
        try:
            span = verify_quote(clause, quote)
        except AnchorError as exc:
            failures.append(str(exc))
            continue
        return Anchor(
            clause=clause,
            quote=quote,
            matched_text=clause.text[span[0] : span[1]],
            clause_span=span,
            document_span=(clause.char_span[0] + span[0], clause.char_span[0] + span[1]),
        )
    raise AnchorError("；".join(failure.rstrip("。") for failure in failures) + "。")


# A clause reference written in free prose: "第九十九条", "第六条第一款".
# The numerals are the same set document.py accepts when splitting clauses.
_MENTION_RE = re.compile(
    r"第[ \t\u3000]*"
    r"(?P<article>[0-9０-９一二三四五六七八九十百千零〇两]+)[ \t\u3000]*条"
    r"(?:[ \t\u3000]*第[ \t\u3000]*"
    r"(?P<paragraph>[0-9０-９一二三四五六七八九十百千零〇两]+)[ \t\u3000]*款)?"
)


def clause_mentions(text: str) -> list[str]:
    """Every 第X条 / 第X条Y款 reference written in ``text``, in order.

    Labels are returned in the clause tree's form (whitespace removed, NFKC),
    so they can be compared to ``Clause.label`` / a clause path directly.
    """
    mentions: list[str] = []
    for match in _MENTION_RE.finditer(text):
        label = f"第{match.group('article')}条"
        if match.group("paragraph"):
            label += f"第{match.group('paragraph')}款"
        label = _label_key(label)
        if label not in mentions:
            mentions.append(label)
    return mentions


def verify_conclusion_clauses(
    conclusion: str,
    clause_ids: Iterable[tuple[str, ...]],
    clauses: Mapping[tuple[str, ...], Clause],
) -> None:
    """Require every clause number named in the conclusion to be a verified one.

    A conclusion may only name 第X条 / 第X条Y款 labels that belong to a clause
    the claim asserted *and* that exists in the document. A fabricated
    "依据第九十九条规定" therefore cannot ride along on a quote that happens to
    be real. Only the conclusion is scanned; the quote is evidence and is
    checked by ``verify_claim_quote`` instead.
    """
    verified = [tuple(path) for path in clause_ids if tuple(path) in clauses]
    allowed = {_label_key(path[0]) for path in verified}
    allowed.update(_label_key("".join(path)) for path in verified)

    offending: list[str] = []
    for label in clause_mentions(conclusion):
        if label not in allowed and label not in offending:
            offending.append(label)
    if offending:
        raise AnchorError(
            "结论中提到的条款号不在本条已校验的条款内：" + "、".join(offending)
        )


def verify_claim(
    clauses: Mapping[tuple[str, ...], Clause],
    clause_ids: Iterable[tuple[str, ...]],
    quote: str,
    conclusion: str,
) -> Anchor:
    """The full gate for one claim: prose clause numbers, then the quote.

    The conclusion is checked first so a fabricated 条款号 is always named even
    when the quote is also wrong. Both the m2 review loop and the m3 export gate
    call this, so a claim cannot pass one path and fail the other.
    """
    paths = [tuple(path) for path in clause_ids]
    verify_conclusion_clauses(conclusion, paths, clauses)
    return verify_claim_quote(clauses, paths, quote)


def find_matches(
    document: Document,
    quote: str,
    *,
    within: tuple[str, ...] | None = None,
) -> list[Anchor]:
    """All clauses the quote verifies against, most specific first."""
    matches: list[Anchor] = []
    for clause in document.clauses:
        if within is not None and clause.path != within:
            continue
        try:
            span = verify_quote(clause, quote)
        except AnchorError:
            continue
        start = clause.char_span[0] + span[0]
        matches.append(
            Anchor(
                clause=clause,
                quote=quote,
                matched_text=clause.text[span[0] : span[1]],
                clause_span=span,
                document_span=(start, clause.char_span[0] + span[1]),
            )
        )

    matches.sort(
        key=lambda anchor: (
            -anchor.clause.depth,
            anchor.document_span[0],
            anchor.clause.char_span[1] - anchor.clause.char_span[0],
        )
    )
    return matches


def resolve(
    document: Document,
    quote: str,
    *,
    within: tuple[str, ...] | None = None,
) -> Anchor:
    """Resolve a quote to one clause, or raise AnchorError.

    This global lookup is for exploration only. It rejects short or filler-only
    quotes, and it is a hard error when the quote verifies against more than one
    distinct clause path (for example the same organ name in 第二条 and 第六条):
    silently picking one would let in exactly the re-pointing this tool exists
    to prevent. The m2 gate must not call this; it must call
    ``verify_quote(asserted_clause, quote)``.
    """
    _validate_quote(quote)
    matches = find_matches(document, quote, within=within)
    if matches:
        paths = _distinct_paths(matches)
        if len(paths) > 1:
            labels = "、".join("".join(path) for path in paths)
            raise AnchorError(
                f"引文同时命中多个不同条款：{labels}。"
                "请指定条款后改用 verify_quote(clause, quote) 校验。"
            )
        return matches[0]
    if within is not None:
        raise AnchorError(f"{''.join(within)}：引文不是该条原文的逐字子串。")
    snippet = quote.strip().replace("\n", " ")
    if len(snippet) > 40:
        snippet = snippet[:40] + "…"
    raise AnchorError(f"引文未匹配到任何条款：{snippet or '（空引文）'}")


def _validate_quote(quote: str, label: str | None = None) -> str:
    """Normalize a quote and enforce the evidence floor."""
    needle = normalize(quote)
    prefix = f"{label}：" if label else ""
    if not needle:
        raise AnchorError(f"{prefix}引文为空或只含空白。")
    if len(needle) < MIN_QUOTE_CHARS:
        raise AnchorError(
            f"{prefix}引文过短：归一化后只有 {len(needle)} 个字符，"
            f"至少需要 {MIN_QUOTE_CHARS} 个。"
        )
    if not any(character not in _FILLER_CHARS for character in needle):
        raise AnchorError(f"{prefix}引文只含标点或虚词，不能作为锚点。")
    return needle


def _distinct_paths(matches: list[Anchor]) -> list[tuple[str, ...]]:
    """Distinct clause locations, collapsing an article and its own paragraph."""
    ordered: list[tuple[str, ...]] = []
    for match in matches:
        path = match.clause.path
        if path not in ordered:
            ordered.append(path)
    return [
        path
        for path in ordered
        if not any(path != other and other[: len(path)] == path for other in ordered)
    ]


def _normalized_chars(text: str) -> Iterator[tuple[str, int]]:
    for index, character in enumerate(text):
        for part in unicodedata.normalize("NFKC", character):
            if part.isspace():
                continue
            yield part, index


def _normalize_with_map(text: str) -> tuple[str, list[int]]:
    characters: list[str] = []
    mapping: list[int] = []
    for character, index in _normalized_chars(text):
        characters.append(character)
        mapping.append(index)
    return "".join(characters), mapping
