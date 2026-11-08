# Changelog

All notable changes to ClauseAnchor are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
semantic versioning.

## [0.1.0] - 2026-11-09

### Added

- `clauseanchor parse` for text-layer PDFs and UTF-8 / GB18030 plain text:
  splits 第X条 / 第X款 units, keeps the 1-based page and character span of every
  clause, strips running headers and page-number footers, and extracts 发文机关,
  发文字号 and the effective date. `--json` dumps the full tree including
  `char_span`; `--resolve QUOTE` checks one quote against the document.
- `clauseanchor draft`: an OpenAI-compatible candidate pass (`--base-url`,
  `--model`, short aliases `deepseek` / `deepseek-r` / `qwen`) followed by a
  terminal per-claim review with `a` accept, `e` rewrite, `r` reject. Decisions
  are written to `decisions.json`.
- `clauseanchor draft --decisions FILE`: non-interactive replay that re-parses
  the source and re-checks every claim without calling a model.
- The anchor gate: a claim's quote must be an exact substring of an asserted
  clause after whitespace removal and NFKC normalization; short or filler-only
  quotes are refused. Every 第X条 / 第X条Y款 named in the conclusion must also be
  one of the claim's asserted clauses that exist in the document, so a real
  quote cannot carry a fabricated 条款号. A failing claim is reported by its
  条款号.
- Source binding for `decisions.json`: the document `doc_id` and a per-clause
  text digest are stored, and render/replay re-parse the source file and refuse
  if either mismatches.
- `clauseanchor render` writes `长文.md`, `koubo.md` and `cards.html` from
  accepted claims only. Each entry takes its label, page and effective date from
  the clause the verified anchor matched. One failed claim blocks the whole
  export.
- `clauseanchor demo`: an offline, key-free run of the full loop on the bundled
  sample with a deliberately altered quote and a misplaced clause number, plus
  `demo-raw-model-draft.md` for the before/after comparison.
- `SKILL.md` so Claude Code can run the parse → candidate → review → export
  loop as an installable Skill.
- Three checked-in real Shanghai policy PDFs under `examples/real/`, with source
  URLs and parse results recorded in `examples/real/SOURCES.md`.
- Tests cover clause splitting, page mapping, header/footer stripping, the
  text-layer PDF fixture and the three real PDFs; altered quotes, wrong clause
  numbers, fabricated clause numbers in conclusions, `e` repair, forged
  `anchor.ok`, snapshot tampering, doc-id mismatch and missing sources; the
  provider/JSON boundary, model aliases and a `draft` run against a local HTTP
  stub; and accepted-only rendering across all three formats.
