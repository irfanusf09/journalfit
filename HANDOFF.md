# Handoff: JournalFit development notes

This file lets a new Claude Code session (for example one running on the owner's Mac)
continue work on JournalFit. The owner is not a programmer: give them plain-language,
step-by-step instructions, and prefer doing things yourself over asking them to type
commands.

## What JournalFit is

A tool that takes a manuscript, finds the best-fitting journals across all publishers,
checks journal metrics against the user's rules, prepares a submission package (authors,
title page, anonymised copy, figures, highlights, suggested reviewers) and fills in the
journal's submission website step by step (autopilot). The user always clicks the final
Submit button; the tool only presses Next/Continue and Add author/reviewer buttons.

- Point-and-click app: `journalfit app` (local web page at http://127.0.0.1:8765, started
  by the `JournalFit.command` icon on the Desktop; create it with `journalfit app --shortcut`).
- Command line: `journalfit find|prepare|submit|assist|data` (see README.md).
- Tests: `pip install -e '.[dev,browser]' && pytest` (all passing at handoff).

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
- `sources/crossref.py` computes the median submission-to-acceptance days from the
  received and accepted dates publishers record in Crossref. It works even when the
  journal's website blocks the tool.
- `finder.py` runs the pipeline: AI lookups for the top N journals (20 for DeepSeek),
  a 90-day cache of real findings, a scope review of every finalist, and diagnostics.
- `prepare.py` builds the package (checklist, cover letter, supplementary files).
- `submit/navigator.py`: the autopilot. `FORBIDDEN` (submit, approve, build PDF, agree, pay, delete,
  ...) always wins over `NEXT`/`ADD_PERSON`; it stops at the review page and pauses with a reason.
- `submit/people.py`: author/reviewer records and how person boxes are matched (one block per person).
- `authors.py` reads the title block; `files.py` makes the title page, anonymised copy, figures and
  highlights; `reviewers.py` suggests reviewers from OpenAlex.
- `submit/assistant.py` fills forms and uploads files; `Driver` is the page-watching loop shared by the
  app, the CLI and the tests. It handles
  item-type uploads (Editorial Manager, ScholarOne), rich-text editors, lists, option groups
  and tick boxes. Standard confirmations are ticked only if the author switched them on.
- `submit/ai_mapper.py`: the AI reads each form page and says which prepared item goes in which
  box. It never writes text itself, and the hard rules in assistant.py still apply.
- `app.py` and `web/index.html` make up the local web app.
- `trace.py` writes the per-run diagnostics.

## Ground rules agreed with the owner

- Respect sites that refuse automated access (HTTP 403, bot checks). Do not add
  techniques to get around bot protection (headed or off-screen browsers, fingerprint
  spoofing, fetching blocked pages through mirrors, CDNs or archives). The user agent is
  honest (`JournalFit/0.1`).
- Never guess metrics: unknown stays "not published", and every number records its source.
- Never auto-submit. The autopilot may press only Next/Continue and Add author/reviewer buttons
  (agreed 2026-10-30); it stops at the review/approval page.
- Person boxes are filled only from the author's own records (authors.json, reviewers.json), never guessed.
- Tests never touch the real ~/.journalfit (tests/conftest.py).
- tests/portal is a 6-step practice submission site; test_autopilot.py drives it end to end.

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
2. Done: "Enter metrics" button on each journal card (saves to `~/.journalfit/metrics_overrides.csv`), plus an "Open metrics page" link.
3. Done: when DeepSeek search finds an official page it can't open, the search result's
   summary is used (it often contains the numbers).
4. Try the full flow (Prepare, then Open submission website, then Fill this page) on a
   real journal portal and fix the field matching where needed.
