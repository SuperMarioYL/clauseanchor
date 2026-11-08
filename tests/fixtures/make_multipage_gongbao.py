"""Regenerate the multi-page text-layer PDF fixture.

The fixture deliberately repeats a running header and a page-number footer on
every page, and starts 第三条 in the middle of a line after "。", so the parser's
page-header stripping and same-line marker handling stay covered in-repo.

Run from the repository root:

    uv run python tests/fixtures/make_multipage_gongbao.py
"""

from __future__ import annotations

from pathlib import Path

import pymupdf

RUNNING_HEADER = "示例省人民政府公报"
PAGES = [
    [
        "示例省人民政府文件",
        "示例省政发〔2026〕9号",
        "第一条　为了验证多页文本层解析，本条规定第一项内容。",
        "第二条　本条跨越分页，其正文应当完整保留在同一个条款内，",
    ],
    [
        "继续第二条的规定，不得被页眉页脚污染。第三条　本条从同一行的句号之后开始。",
        "第四条　本条用于验证页码对应关系。",
    ],
    ["第五条　本办法自2027年1月1日起施行。"],
]


def main() -> None:
    document = pymupdf.open()
    for number, body in enumerate(PAGES, start=1):
        page = document.new_page()
        page.insert_text((50, 45), RUNNING_HEADER, fontname="china-s", fontsize=11)
        y = 90
        for line in body:
            page.insert_text((50, y), line, fontname="china-s", fontsize=11)
            y += 22
        page.insert_text((50, 800), f"第 {number} 页", fontname="china-s", fontsize=11)

    output = Path(__file__).with_name("multipage_gongbao.pdf")
    document.save(output)
    document.close()
    print(output)


if __name__ == "__main__":
    main()
