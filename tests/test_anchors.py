"""End-to-end tests for parsing, the anchor gate, review and rendering.

Everything runs offline: the model is a scripted candidate list, and the only
files touched are the checked-in samples, a generated text-layer PDF, and
``tmp_path``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from clauseanchor.anchors import AnchorError, verify_quote
from clauseanchor.candidates import Claim
from clauseanchor.cli import main
from clauseanchor.document import (
    DocumentError,
    clause_digest,
    load,
    parse_text,
    verify_snapshot,
)
from clauseanchor.render import ExportBlocked, render
from clauseanchor.review import (
    DecisionError,
    bind_source,
    replay_decisions,
    run_review,
    save_decisions,
)

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "sample_gongbao.txt"
REAL_DIR = ROOT / "examples" / "real"
MULTIPAGE_PDF = ROOT / "tests" / "fixtures" / "multipage_gongbao.pdf"

QUOTE_1 = "对首次采购云服务的中小企业，按照实际支出的百分之三十给予补助"
QUOTE_3 = "对通过省级验收的数字化车间、智能工厂，分别给予一次性奖励"
QUOTE_12 = "本措施自2026年9月1日起施行，有效期三年"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _silent(_: str) -> None:
    pass


def _reader(*answers: str):
    queue = list(answers)

    def read(prompt: str) -> str:
        if not queue:
            raise EOFError
        return queue.pop(0)

    return read


def _clause(document, label: str):
    for clause in document.clauses:
        if clause.label == label:
            return clause
    raise AssertionError(f"missing clause {label}")


def _claim(
    claim_id: str,
    conclusion: str,
    labels: tuple[str, ...],
    quote: str,
) -> Claim:
    return Claim(
        claim_id=claim_id,
        conclusion=conclusion,
        clause_ids=tuple((label,) for label in labels),
        quote=quote,
    )


def _review(document, claims, *answers: str) -> dict:
    return run_review(
        document,
        claims,
        model="test-model",
        input_fn=_reader(*answers),
        output=_silent,
    )


def _good_payload():
    document = load(SAMPLE)
    claims = [
        _claim("c1", "首次采购云服务可获三成补助。", ("第一条",), QUOTE_1),
        _claim("c2", "数字化车间通过省级验收可获一次性奖励。", ("第三条",), QUOTE_3),
    ]
    return document, _review(document, claims, "a", "a")


# --------------------------------------------------------------------------- #
# m1 — clause splitting, page mapping, header/footer stripping
# --------------------------------------------------------------------------- #


def test_sample_splits_articles_and_paragraphs() -> None:
    document = load(SAMPLE)

    assert document.doc_id.startswith("sha256:")
    assert document.issuer == "示例省人民政府"
    assert document.doc_number == "示例省政发〔2026〕15号"
    assert document.effective_date == "2026-09-01"
    assert document.title == "示例省人民政府关于促进中小企业数字化转型的若干措施"
    assert len(document.articles) == 12
    assert len(document.paragraphs) == 6

    article = _clause(document, "第六条")
    assert article.level == "article"
    assert article.path == ("第六条",)
    assert article.page == 1
    assert article.text.startswith("第六条　开展数字化转型标杆培育")
    assert article.body.startswith("开展数字化转型标杆培育")

    paragraph = _clause(document, "第六条第一款")
    assert paragraph.level == "paragraph"
    assert paragraph.path == ("第六条", "第一款")
    assert paragraph.body.startswith("对入选标杆的企业")

    # The last article must not swallow the 附件 line or the signature/date.
    last = _clause(document, "第十二条")
    assert "附件" not in last.text
    assert "2026年7月20日" not in last.text
    assert document.text[last.char_span[0] : last.char_span[1]] == last.text


def test_multipage_pdf_page_mapping_and_running_lines() -> None:
    document = load(MULTIPAGE_PDF)

    assert document.source_type == "pdf"
    assert document.page_count == 3
    assert document.doc_number == "示例省政发〔2026〕9号"
    assert document.effective_date == "2027-01-01"
    assert len(document.articles) == 5

    labels = [clause.label for clause in document.clauses]
    assert labels == ["第一条", "第二条", "第三条", "第四条", "第五条"]
    assert [clause.page for clause in document.clauses] == [1, 1, 2, 2, 3]

    # A clause that crosses the page break keeps its body, without the running
    # header or the page-number footer.
    second = _clause(document, "第二条")
    assert "继续第二条的规定" in second.text
    for clause in document.clauses:
        assert "示例省人民政府公报" not in clause.text
        assert "第 1 页" not in clause.text
        assert "第 2 页" not in clause.text
        assert "第 3 页" not in clause.text

    for clause in document.clauses:
        assert document.page_at(clause.char_span[0]) == clause.page
        assert document.text[clause.char_span[0] : clause.char_span[1]] == clause.text


def test_parse_text_strips_repeated_headers_and_page_numbers() -> None:
    text = (
        "示例省人民政府公报\n第一条　第一页正文。\n— 1 —\n"
        "\f示例省人民政府公报\n第二条　第二页正文。\n— 2 —\n"
        "\f示例省人民政府公报\n第三条　第三页正文。\n— 3 —\n"
    )
    document = parse_text(text)

    assert [clause.label for clause in document.clauses] == [
        "第一条",
        "第二条",
        "第三条",
    ]
    assert [clause.page for clause in document.clauses] == [1, 2, 3]
    assert all("示例省人民政府公报" not in c.text for c in document.clauses)
    assert all("— 1 —" not in c.text for c in document.clauses)


@pytest.mark.parametrize(
    ("name", "articles", "doc_number", "effective"),
    [
        ("sh_market_reg_archive_lookup.pdf", 16, "沪市监规范〔2026〕7号", "2027-01-01"),
        ("sh_drug_watch_management.pdf", 41, "沪药监规〔2026〕3号", "2026-08-01"),
        ("sh_gov_investment_management.pdf", 37, "沪府规〔2026〕5号", "2026-08-01"),
    ],
)
def test_real_text_layer_pdfs(
    name: str, articles: int, doc_number: str, effective: str
) -> None:
    document = load(REAL_DIR / name)

    assert document.source_type == "pdf"
    assert document.issuer
    assert document.doc_number == doc_number
    assert document.effective_date == effective
    assert len(document.articles) == articles
    assert len(document.paragraphs) == 0

    pages = [clause.page for clause in document.articles]
    assert pages == sorted(pages)
    assert pages[0] >= 2  # the gazette head and 通知 come first

    for clause in document.articles:
        assert clause.path[0].startswith("第")
        assert clause.path[0].endswith("条")
        assert clause.body.strip()
        assert document.text[clause.char_span[0] : clause.char_span[1]] == clause.text
        assert document.page_at(clause.char_span[0]) == clause.page
        assert clause.effective_date == effective


@pytest.mark.parametrize(
    "name",
    [
        "sh_market_reg_archive_lookup.pdf",
        "sh_drug_watch_management.pdf",
        "sh_gov_investment_management.pdf",
    ],
)
def test_real_pdf_last_article_stops_before_the_tail_block(name: str) -> None:
    last = load(REAL_DIR / name).articles[-1]

    # A real 公文 tail carries 抄送, a 署名 and the 印发 date. None of it may
    # leak into the last article: the article must end at a sentence terminator.
    assert last.text.rstrip().endswith(("。", ".", "！", "!", "？", "?", "；", ";"))
    assert "抄送" not in last.text
    for line in last.text.splitlines():
        assert not line.strip().endswith(("处", "室")), line


def test_missing_file_and_scanned_pdf_are_reported(tmp_path: Path) -> None:
    with pytest.raises(DocumentError):
        load(tmp_path / "nope.pdf")

    empty = tmp_path / "blank.pdf"
    import pymupdf

    document = pymupdf.open()
    document.new_page()
    document.save(empty)
    document.close()
    with pytest.raises(DocumentError, match="文字层"):
        load(empty)


# --------------------------------------------------------------------------- #
# m2 — the anchor gate, e-repair, forged decisions
# --------------------------------------------------------------------------- #


def test_quote_must_be_verbatim_after_normalization() -> None:
    document = load(SAMPLE)
    clause = _clause(document, "第一条")

    # Whitespace and full-width/half-width differences do not defeat the gate.
    spaced = "对首次采购云服务的中小企业，按照实际支出的百分之三十给予补助"
    assert verify_quote(clause, spaced)

    with pytest.raises(AnchorError):
        verify_quote(clause, spaced + "全部")


def test_altered_quote_is_blocked_and_names_the_clause(tmp_path: Path) -> None:
    document = load(SAMPLE)
    altered = QUOTE_3 + "（模型补写）"
    payload = _review(
        document,
        [_claim("c1", "结论", ("第三条",), altered)],
        "a",
    )

    record = payload["claims"][0]
    assert record["verdict"] == "accepted"
    assert record["anchor"]["ok"] is False
    assert "第三条" in record["anchor"]["error"]

    with pytest.raises(ExportBlocked) as excinfo:
        render(payload, tmp_path / "out")

    message = str(excinfo.value)
    assert "第三条" in message
    assert "c1" in message
    assert not (tmp_path / "out" / "长文.md").exists()
    assert not (tmp_path / "out" / "cards.html").exists()


def test_wrong_clause_number_is_named(tmp_path: Path) -> None:
    document = load(SAMPLE)
    payload = _review(
        document,
        [_claim("c1", "结论", ("第六条",), QUOTE_3)],
        "a",
    )

    assert payload["claims"][0]["anchor"]["ok"] is False
    assert "第六条" in payload["claims"][0]["anchor"]["error"]

    with pytest.raises(ExportBlocked, match="第六条"):
        render(payload, tmp_path / "out")


def test_fabricated_clause_number_in_conclusion_blocks_review_and_export(
    tmp_path: Path,
) -> None:
    document = load(SAMPLE)
    conclusion = "依据第九十九条规定，首次采购云服务的企业可获三成补助。"
    payload = _review(
        document,
        [_claim("c1", conclusion, ("第一条",), QUOTE_1)],
        "a",
    )

    record = payload["claims"][0]
    assert record["verdict"] == "accepted"
    assert record["anchor"]["ok"] is False
    assert "第九十九条" in record["anchor"]["error"]

    with pytest.raises(ExportBlocked) as excinfo:
        render(payload, tmp_path / "out")

    message = str(excinfo.value)
    assert "c1" in message
    assert "第九十九条" in message
    # Nothing may be written: the fabricated number never reaches 长文.md.
    assert not (tmp_path / "out" / "长文.md").exists()
    assert not (tmp_path / "out" / "cards.html").exists()


def test_conclusion_naming_its_own_verified_clause_passes(tmp_path: Path) -> None:
    document = load(SAMPLE)
    conclusion = "依据第一条规定，首次采购云服务的企业可获三成补助。"
    payload = _review(
        document,
        [_claim("c1", conclusion, ("第一条",), QUOTE_1)],
        "a",
    )

    assert payload["claims"][0]["anchor"]["ok"] is True
    result = render(payload, tmp_path / "out")
    assert result.accepted == 1
    longform = (tmp_path / "out" / "长文.md").read_text(encoding="utf-8")
    assert conclusion in longform


def test_clause_number_inside_the_quote_is_not_gated(tmp_path: Path) -> None:
    source = tmp_path / "sample.txt"
    source.write_text(
        "示例省人民政府\n第一条　依据第九十九条规定，对首次采购云服务的中小企业给予补助。\n",
        encoding="utf-8",
    )
    document = load(source)
    conclusion = "首次采购云服务的中小企业可获补助。"
    payload = _review(
        document,
        [_claim("c1", conclusion, ("第一条",), "依据第九十九条规定")],
        "a",
    )

    assert payload["claims"][0]["anchor"]["ok"] is True
    result = render(payload, tmp_path / "out")
    assert result.accepted == 1
    # The number survives only as part of the verbatim quote, which is allowed.
    assert "第九十九条" in (tmp_path / "out" / "长文.md").read_text(encoding="utf-8")


def test_e_repair_replaces_the_quote_and_unblocks_export(tmp_path: Path) -> None:
    document = load(SAMPLE)
    altered = QUOTE_3 + "（模型补写）"
    payload = _review(
        document,
        [_claim("c1", "数字化车间与智能工厂通过省级验收后可获一次性奖励。", ("第三条",), altered)],
        "e",
        "",  # keep the conclusion
        QUOTE_3,  # paste the true passage
        "a",
    )

    record = payload["claims"][0]
    assert record["quote"] == QUOTE_3
    assert record["anchor"]["ok"] is True
    assert record["anchor"]["clause"] == "第三条"

    result = render(payload, tmp_path / "out")
    assert result.accepted == 1
    text = (tmp_path / "out" / "长文.md").read_text(encoding="utf-8")
    assert QUOTE_3 in text


def test_forged_anchor_ok_is_rechecked(tmp_path: Path) -> None:
    _, payload = _good_payload()
    record = payload["claims"][0]
    record["quote"] = QUOTE_1 + "全部"
    record["anchor"] = {
        "ok": True,
        "clause": "第一条",
        "path": ["第一条"],
        "page": 1,
        "matched_text": QUOTE_1,
        "error": None,
    }

    with pytest.raises(ExportBlocked, match="第一条"):
        render(payload, tmp_path / "out")


def test_tampered_snapshot_text_is_blocked(tmp_path: Path) -> None:
    _, payload = _good_payload()
    record = payload["claims"][0]
    record["quote"] = QUOTE_1 + "全部"
    record["clauses"][0]["text"] += "全部"

    with pytest.raises(ExportBlocked, match="digest") as excinfo:
        render(payload, tmp_path / "out")
    assert "c1" in str(excinfo.value)


def test_snapshot_rehashed_to_forged_text_is_blocked(tmp_path: Path) -> None:
    _, payload = _good_payload()
    record = payload["claims"][0]
    forged = QUOTE_1 + "全部"
    record["quote"] = forged
    record["clauses"][0]["text"] = "第一条　" + forged
    record["clauses"][0]["digest"] = clause_digest(record["clauses"][0]["text"])

    with pytest.raises(ExportBlocked, match="源文件原文"):
        render(payload, tmp_path / "out")


def test_snapshot_for_another_doc_id_is_blocked() -> None:
    _, payload = _good_payload()
    snapshot = payload["claims"][0]["clauses"][0]
    snapshot["doc_id"] = "sha256:0000000000000000"
    source = load(SAMPLE)
    clauses = {clause.path: clause for clause in source.clauses}

    with pytest.raises(DocumentError, match="doc_id"):
        verify_snapshot(source.doc_id, clauses, snapshot)


def test_edited_source_file_blocks_render(tmp_path: Path) -> None:
    source = tmp_path / "sample.txt"
    shutil.copyfile(SAMPLE, source)
    document = load(source)
    payload = _review(
        document,
        [_claim("c1", "结论", ("第一条",), QUOTE_1)],
        "a",
    )
    render(payload, tmp_path / "first")

    source.write_text(
        source.read_text(encoding="utf-8") + "\n追加的一行\n", encoding="utf-8"
    )
    with pytest.raises(ExportBlocked, match="doc_id"):
        render(payload, tmp_path / "second")


def test_missing_source_blocks_render(tmp_path: Path) -> None:
    source = tmp_path / "sample.txt"
    shutil.copyfile(SAMPLE, source)
    document = load(source)
    payload = _review(
        document,
        [_claim("c1", "结论", ("第一条",), QUOTE_1)],
        "a",
    )
    source.unlink()

    with pytest.raises(ExportBlocked, match="找不到"):
        render(payload, tmp_path / "out")


def test_replay_reanchors_against_the_source(tmp_path: Path) -> None:
    document = load(SAMPLE)
    payload = _review(
        document,
        [_claim("c1", "结论", ("第一条",), QUOTE_1)],
        "a",
    )
    decisions = save_decisions(payload, tmp_path / "decisions.json")

    replayed = replay_decisions(json.loads(decisions.read_text(encoding="utf-8")))
    assert replayed["claims"][0]["anchor"]["ok"] is True
    assert replayed["claims"][0]["anchor"]["clause"] == "第一条"

    payload["claims"][0]["clauses"][0]["text"] += "伪造"
    with pytest.raises(DecisionError, match="digest"):
        replay_decisions(payload)


def test_replay_blocks_a_fabricated_clause_number_in_the_conclusion() -> None:
    _, payload = _good_payload()
    payload["claims"][0]["conclusion"] = "依据第九十九条规定，首次采购云服务可获三成补助。"
    payload["claims"][0]["anchor"] = {"ok": True, "clause": "第一条", "error": None}

    replayed = replay_decisions(payload)

    record = replayed["claims"][0]
    assert record["anchor"]["ok"] is False
    assert "第九十九条" in record["anchor"]["error"]


def test_bind_source_requires_a_document_record() -> None:
    with pytest.raises(DecisionError):
        bind_source({"claims": []})


# --------------------------------------------------------------------------- #
# m3 — export only accepted claims, in three formats
# --------------------------------------------------------------------------- #


def test_render_exports_only_accepted_claims(tmp_path: Path) -> None:
    document = load(SAMPLE)
    rejected_conclusion = "被剔除的结论：本措施永久有效，且不需要核对条款。"
    claims = [
        _claim("c1", "首次采购云服务可获三成补助。", ("第一条",), QUOTE_1),
        _claim("c2", "数字化车间通过省级验收可获一次性奖励。", ("第三条",), QUOTE_3),
        _claim("c3", rejected_conclusion, ("第十二条",), QUOTE_12),
    ]
    payload = _review(document, claims, "a", "a", "r")
    assert [r["verdict"] for r in payload["claims"]] == [
        "accepted",
        "accepted",
        "rejected",
    ]

    result = render(payload, tmp_path / "out")
    assert result.accepted == 2
    assert result.rejected == 1
    assert {path.name for path in result.outputs} == {"长文.md", "koubo.md", "cards.html"}

    longform = (tmp_path / "out" / "长文.md").read_text(encoding="utf-8")
    koubo = (tmp_path / "out" / "koubo.md").read_text(encoding="utf-8")
    cards = (tmp_path / "out" / "cards.html").read_text(encoding="utf-8")

    for text in (longform, koubo, cards):
        assert "首次采购云服务可获三成补助。" in text
        assert "数字化车间通过省级验收可获一次性奖励。" in text
        assert rejected_conclusion not in text
        assert "永久有效" not in text

    assert longform.startswith("# 示例省人民政府关于促进中小企业数字化转型的若干措施")
    assert "## 第一条" in longform
    assert "## 第三条" in longform
    assert "出处：第一条（第 1 页），自 2026-09-01 起施行。" in longform
    assert f"> 原文：“{QUOTE_1}”" in longform

    assert koubo.startswith("【口播稿】")
    assert "（第 1 段 · 第一条）" in koubo
    assert "（第 2 段 · 第三条）" in koubo
    # The conclusion and the quote are separate lines; they must not run
    # together into one sentence.
    assert "首次采购云服务可获三成补助。\n原文是这么写的" in koubo

    assert cards.count('<article class="card">') == 2
    assert "<!doctype html>" in cards
    assert "自 2026-09-01 起施行" in cards


def test_render_addresses_only_the_clause_the_anchor_matched(tmp_path: Path) -> None:
    document = load(SAMPLE)
    claim = _claim("c1", "结论", ("第三条", "第五条"), QUOTE_3)
    payload = _review(document, [claim], "a")
    assert len(payload["claims"][0]["clauses"]) == 2
    assert payload["claims"][0]["anchor"]["clause"] == "第三条"

    render(payload, tmp_path / "out")
    longform = (tmp_path / "out" / "长文.md").read_text(encoding="utf-8")
    cards = (tmp_path / "out" / "cards.html").read_text(encoding="utf-8")

    assert "## 第三条" in longform
    assert "第五条" not in longform
    assert "第五条" not in cards


def test_render_refuses_a_missing_asserted_clause(tmp_path: Path) -> None:
    document = load(SAMPLE)
    payload = _review(
        document,
        [_claim("c1", "结论", ("第三条", "第九十九条"), QUOTE_3)],
        "a",
    )
    assert payload["claims"][0]["anchor"]["ok"] is True

    with pytest.raises(ExportBlocked, match="第九十九条"):
        render(payload, tmp_path / "out")


def test_accepted_claim_must_keep_a_snapshot_for_every_asserted_clause(
    tmp_path: Path,
) -> None:
    _, payload = _good_payload()
    record = payload["claims"][0]
    record["clause_ids"] = [["第一条"], ["第三条"]]
    record["clauses"] = [
        snapshot for snapshot in record["clauses"] if snapshot["path"] == ["第一条"]
    ]

    with pytest.raises(ExportBlocked, match="缺少快照"):
        render(payload, tmp_path / "out")


def test_rejected_claim_with_unknown_clause_does_not_block_export(
    tmp_path: Path,
) -> None:
    document = load(SAMPLE)
    claims = [
        _claim("c1", "首次采购云服务可获三成补助。", ("第一条",), QUOTE_1),
        _claim("c2", "这条被剔除，引用了不存在的条款。", ("第九十九条",), "随便一段引文"),
    ]
    payload = _review(document, claims, "a", "r")

    result = render(payload, tmp_path / "out")
    assert result.accepted == 1
    assert "第九十九条" not in (tmp_path / "out" / "长文.md").read_text(
        encoding="utf-8"
    )


def test_render_requires_at_least_one_accepted_claim(tmp_path: Path) -> None:
    document = load(SAMPLE)
    payload = _review(
        document,
        [_claim("c1", "结论", ("第一条",), QUOTE_1)],
        "r",
    )
    with pytest.raises(ExportBlocked, match="没有已采纳"):
        render(payload, tmp_path / "out")


# --------------------------------------------------------------------------- #
# m4 — one-command offline demo
# --------------------------------------------------------------------------- #


def test_demo_runs_the_full_flow_offline(tmp_path: Path) -> None:
    assert main(["demo", "-o", str(tmp_path)]) == 0

    names = {path.name for path in tmp_path.iterdir()}
    assert {
        "decisions.json",
        "长文.md",
        "koubo.md",
        "cards.html",
        "demo-raw-model-draft.md",
    } <= names

    payload = json.loads((tmp_path / "decisions.json").read_text(encoding="utf-8"))
    verdicts = [record["verdict"] for record in payload["claims"]]
    assert verdicts.count("accepted") >= 1
    assert verdicts.count("rejected") >= 1
    for record in payload["claims"]:
        if record["verdict"] == "accepted":
            assert record["anchor"]["ok"] is True
        else:
            assert record["anchor"]["ok"] is False

    raw = (tmp_path / "demo-raw-model-draft.md").read_text(encoding="utf-8")
    assert "未经任何校验" in raw
    assert "2027-01-01" in raw

    longform = (tmp_path / "长文.md").read_text(encoding="utf-8")
    assert "自 2026-09-01 起施行" in longform
