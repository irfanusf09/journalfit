# Handoff: JournalFit development notes

This file lets a new Claude Code session (for example one running on the owner's Mac)
continue work on JournalFit. The owner is not a programmer: give them plain-language,
step-by-step instructions, and prefer doing things yourself over asking them to type
commands.

## What JournalFit is

A tool that takes a manuscript, finds the best-fitting journals across all publishers,
checks journal metrics against the user's rules, prepares a submission package and fills
in the journal's submission website. The user always clicks the final Submit button; the
tool never clicks buttons on submission sites.

- Point-and-click app: `journalfit app` (local web page at http://127.0.0.1:8765, started
  by the `JournalFit.command` icon on the Desktop; create it with `journalfit app --shortcut`).
- Command line: `journalfit find|prepare|submit|assist|data` (see README.md).
- Tests: `pip install -e '.[dev,browser]' && pytest` (46 tests, all passing at handoff).

## Owner's setup

- MacBook Air, Python 3.15 from python.org, repo cloned at `~/journalfit` (public repo
  github.com/irfanusf09/journalfit). Install with `python3 -m pip install -e .`
- AI provider: DeepSeek (`deepseek-flash`), key saved in the app's Settings
  (`~/.journalfit/settings.json`, never print or commit it).
- SCImago journal list imported at `~/.journalfit/scimago.csv` (50,986 journals).
  SCImago blocks scripted downloads; the user downloads it via the site's "Download data".
- Results go to `~/Documents/JournalFit/<manuscript>/` with `shortlist.md`,
  `shortlist.json` and `diagnostics.txt`.
- Test manuscript: `Paper1_manuscript.docx` ("Five Dimensions of Everyday Financial
  Behaviour in Kashmir: Credit, Peers and Receptiveness to Saving Mechanisms").
  Typical rules: not open access, Scopus only, first decision <= 50 days,
  acceptance >= 12%, keep journals with unknown metrics.

## Architecture (journalfit/)

- `manuscript.py` parses DOCX/PDF/LaTeX/text.
- `sources/openalex.py` finds similar papers and their journals.
- `sources/indexing.py` handles Scopus status, quartile and SJR from SCImago (uses its
  Open Access and Coverage columns), plus the optional official Scopus list.
- `metrics/publishers.py` contains the per-publisher metric URLs and `PageFetcher`.
- `metrics/extract.py` holds the regexes for first-decision time, acceptance rate,
  IF and CiteScore, and records the matched sentence as evidence.
- `llm.py` covers the providers (Claude Haiku/Opus/mix, OpenAI, Gemini, DeepSeek with its
  Search and Fetch APIs), cost tracking and the pipeline prompts.
- `finder.py` runs the pipeline: AI lookups for the top N journals (20 for DeepSeek),
  a 90-day cache of real findings, a scope review of every finalist, and diagnostics.
- `prepare.py` builds the package (checklist, cover letter, supplementary files).
- `submit/assistant.py` fills forms by label and uploads files; it never clicks buttons.
- `app.py` and `web/index.html` make up the local web app.
- `trace.py` writes the per-run diagnostics.

## Ground rules agreed with the owner

- Respect sites that refuse automated access (HTTP 403, bot checks). Do not add
  techniques to get around bot protection (headed or off-screen browsers, fingerprint
  spoofing, fetching blocked pages through mirrors, CDNs or archives). The user agent is
  honest (`JournalFit/0.1`).
- Never guess metrics: unknown stays "not published", and every number records its source.
- Never auto-submit.

## State at handoff and next steps

The latest real run (with diagnostics) found metrics for 10 of the top 20 journals
(Springer pages, plus Elsevier pages read through DeepSeek Search/Fetch). Wiley, SAGE,
Emerald and INFORMS block automated access, so those stay "not published".

Next steps:
1. Run the app on the Mac with the test manuscript and check `diagnostics.txt`:
   - Check the evidence lines for the "1 day to first decision" values (Journal of
     Financial Services Marketing, Journal of Retailing and Consumer Services). They are
     probably genuine medians that include desk rejections.
   - Confirm the CiteScore year bug is gone (previously "citescore=2025").
2. Offered to the owner and awaiting a yes: an "Enter metrics" button in the app to type
   in numbers for blocked journals (Wiley and others). Those values would be stored in
   `~/.journalfit/metrics_overrides.csv`, which already works.
3. Try the full flow (Prepare, then Open submission website, then Fill this page) on a
   real journal portal and fix the field matching where needed.
