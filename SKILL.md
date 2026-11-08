---
name: clauseanchor
description: Turn a Chinese policy gazette (公报、红头文件、政策原文) into a publishable interpretation where every conclusion carries 第X条, page, effective date and a verbatim quote that is checked against the source. Use when the user has a policy PDF or text and wants a WeChat longform, 口播稿 and card HTML, or wants to verify that a draft's clause numbers and quotes match the original.
---

# ClauseAnchor

ClauseAnchor is a clause-anchored writing tool for Chinese policy texts. It parses
a gazette into 第X条 / 第X款 units, has a model draft conclusions with quotes,
lets the author accept, rewrite or reject each one, and exports only the claims
whose quote is a verbatim substring of the clause they cite and whose conclusion
names only clause numbers the claim verified. A claim that fails the check is
named by its 条款号 and never reaches an exported file.

The tool does not judge legal effect and does not write legal advice. It only
guarantees that a rendered quote can be walked back to the source text.

## When to use this skill

- The user supplies a policy PDF with a text layer (or UTF-8 / GB18030 text) and
  wants publishable interpretation, not a summary or a chat answer.
- The user asks whether a draft's 第X条 references, quotes or effective date
  match the original document.
- The user wants the same accepted claims in three formats: 公众号长文,
  视频号口播稿, and screenshot-ready card HTML.
- The user wants a per-claim review record (`decisions.json`) that can be
  replayed later without the model.

Do not use it for scanned PDFs, OCR, attachment tables, or legal conclusions.
v0.1 accepts text-layer PDFs and plain text only.

## The four-step loop

Run from the repository root (or anywhere after `pip install .`). Use
`uv run` inside a checkout; use `clauseanchor` directly when installed.

1. Parse the source and read the clause tree.

   ```bash
   uv run clauseanchor parse examples/sample_gongbao.txt
   uv run clauseanchor parse examples/real/sh_gov_investment_management.pdf --json
   ```

   The report prints the issuing organ, 发文字号, effective date, article and
   paragraph counts, then one line per clause with its 1-based page.

2. Draft candidates from the clause tree.

   ```bash
   export CLAUSEANCHOR_API_KEY=...        # or DEEPSEEK_API_KEY / OPENAI_API_KEY
   uv run clauseanchor draft examples/sample_gongbao.txt --model deepseek
   ```

   `--base-url` retargets any OpenAI-compatible endpoint; `--model` accepts a
   short alias (`deepseek`, `deepseek-r`, `qwen`) or a real model id. The model
   returns conclusions, clause paths and quotes. ClauseAnchor never repairs a
   quote on the model's behalf.

3. Review each claim in the terminal.

   Keys: `a` accept, `e` rewrite (new conclusion, then paste the true passage
   from the clause), `r` reject. The anchor line shows either
   `✓ 第三条（第 3 页）` or `✗ 第三条：引文不是该条原文的逐字子串…` (or the
   clause number the conclusion fabricated).

   The result is `decisions.json`: verdict, final conclusion, quote, asserted
   clause paths, clause snapshots with text digests, and the anchor result.

4. Export the pack.

   ```bash
   uv run clauseanchor render decisions.json -o out
   ```

   Writes `out/长文.md`, `out/koubo.md`, `out/cards.html`. Before writing
   anything, the renderer re-parses the source file named in `decisions.json`,
   checks its `doc_id` and every clause digest, and re-runs the quote check.
   A single failure blocks all three files.

## Commands

| Command | Purpose |
| --- | --- |
| `clauseanchor parse FILE [--json] [--resolve QUOTE]` | Clause tree, metadata and page mapping. `--resolve` checks one quote globally for exploration. |
| `clauseanchor draft FILE [--model] [--base-url] [--api-key] [--max-claims] [-o decisions.json]` | Generate candidates and run the per-claim review loop. |
| `clauseanchor draft --decisions FILE [-o FILE]` | Non-interactive replay: re-parse the source, re-check every claim, rewrite the anchor results. |
| `clauseanchor render decisions.json [-o out] [--templates DIR]` | Export accepted claims only. |
| `clauseanchor demo [--file FILE] [-o out]` | Offline end-to-end run on the bundled sample with a fixed candidate set; no key, no network. |

## Guarantees and invariants

- `Claim.quote` must be an exact substring of the clause text after whitespace
  removal and NFKC normalization (full-width/half-width unified). Labels are
  kept as written and are never rewritten to make a quote fit.
- A quote is checked only against the clauses the claim asserted. A quote that
  really lives in another clause fails instead of being silently re-pointed.
- Every `第X条` / `第X条Y款` named in a conclusion must be one of the claim's
  asserted clauses that exist in the document; a fabricated clause number in the
  prose blocks review and export even when the quote is real.
- `decisions.json` stores the source path, the document `doc_id` and a digest
  per clause snapshot. Render and replay re-parse the source and refuse on any
  mismatch, so editing the snapshot cannot manufacture a quote.
- Each rendered entry takes its label, page and effective date from the one
  clause the verified anchor matched, not from every clause a claim asserted.
- Rejected claims never reach a template.

## Configuration

| Variable | Meaning |
| --- | --- |
| `CLAUSEANCHOR_API_KEY` | Preferred API key for the OpenAI-compatible endpoint. |
| `DEEPSEEK_API_KEY` | Fallback key. |
| `OPENAI_API_KEY` | Fallback key. |
| `--base-url` | Endpoint, default `https://api.deepseek.com/v1`. |
| `--model` | Model id or alias, default `deepseek-chat`. |

`--templates DIR` swaps the three Jinja2/HTML templates without touching code.

## Files

- `src/clauseanchor/document.py` — PDF/text to `Clause` tree, pages, `char_span`,
  metadata, source identity and snapshot verification.
- `src/clauseanchor/candidates.py` — OpenAI-compatible provider and parsing of
  the model's JSON candidates.
- `src/clauseanchor/anchors.py` — normalization, the quote gate and the
  conclusion clause-number gate.
- `src/clauseanchor/review.py` — the `a/e/r` loop, `decisions.json`, replay.
- `src/clauseanchor/render.py` — accepted-only export through `templates/`.
- `src/clauseanchor/demo.py` — the offline `demo` command.

## Limits

- Text-layer PDFs and plain text only; no OCR.
- Fixed 第X条 / 第X款 grammar. Real gazettes vary by 发文机关; check `parse`
  output before drafting, and treat `注意` lines as work to verify.
- No attachment tables, no cross-document comparison, no 条款 revision tracking.
- No legal opinion. The output states this and links back to the source.
