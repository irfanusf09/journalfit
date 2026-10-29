# journalfit

Give it a manuscript. It finds the journals that fit best (from any publisher), checks each journal's metrics against your rules, prepares the submission package, and fills in the submission website for **one** journal. You click Submit yourself.

## What it does

1. **Reads the manuscript** (DOCX, PDF, LaTeX, Markdown, TXT). It pulls out the title, abstract, keywords, references (with DOIs), word count, and number of figures and tables. Claude fills in anything the parser missed and writes literature-search queries.
2. **Finds candidate journals.** It searches OpenAlex (about 250M papers, all publishers) for papers similar to yours from the last N years. It scores each journal by text similarity to your paper, how many related papers the journal publishes, and how many of your references appeared in it.
3. **Applies your rules:** publisher include/exclude, open access (`--no-oa` keeps subscription and hybrid journals), Scopus indexing (including discontinued titles if you import the official list), minimum quartile, and maximum publication fee (APC).
4. **Collects metrics** for the shortlist:
   - time to first decision, time to acceptance, acceptance rate, Impact Factor and CiteScore, read from publisher pages (Elsevier, Springer Nature, Taylor & Francis, Wiley, MDPI, and the homepage for others);
   - a headless browser for sites that block plain requests;
   - Claude web lookup, restricted to official pages, for anything still missing;
   - SJR, quartile and h-index from SCImago (Scopus data);
   - submission-to-acceptance time from Crossref: the median of the received and accepted dates that publishers record for recent papers. This works even when a publisher's website blocks automated access.

   The source of every number is recorded. A missing value is shown as "n/p" (not published), never guessed.
5. **Applies your metric limits** (`--max-first-decision`, `--min-acceptance`). Journals that don't publish a metric are kept and flagged, or skipped with `--if-missing skip`.
6. **Scope review.** Claude rates the finalists 0–10 the way an editor would. Likely desk rejections drop down the ranking.
7. **Ranks the journals and writes `shortlist.md` and `shortlist.json`**, with reasons, metrics and sources for each one.
8. **Prepares the package** for the journal you pick, the way a careful author would:
   - reads the journal's author guidelines (review model, title page, highlights, separate figures, how many reviewers to suggest);
   - reads the **author list** from the manuscript's title block: names, affiliations split into department, institution, city and country, emails, ORCID iDs and the corresponding author. Co-authors are remembered for next time (`~/.journalfit/people.json`);
   - writes a **title page** with the authors and declarations;
   - for **double-blind** journals, makes an **anonymised manuscript**: the author block, acknowledgements, funding and contributions are removed, names, emails and institutions in the text are replaced by [blinded], and the Word file's author properties are cleared. References are left unchanged;
   - saves **separate figure files** from the Word file when the journal wants them, with their pixel size;
   - drafts **highlights** from the abstract when the journal requires them (for you to check);
   - finds the **funders and grant numbers** in your funding statement;
   - **suggests reviewers**: authors of the closest recent papers in Scopus journals with an established record (at least 15 papers, h-index 6 or more). Your own institutions, your co-authors and their co-authors are excluded, with at most one per institution and a spread over countries. OpenAlex has no email addresses, so you add an email for each reviewer you want to suggest;
   - writes `checklist.md` (must-do items, what was prepared, what is still needed from you), a cover letter (`.docx` and `.txt`) and `metadata.json`.

   Statements written in the manuscript are read whether they look like `Funding: ...` or `Funding. ...`. That includes the use of generative AI, acknowledgements and previous submission.
9. **Submission autopilot.** It opens a real browser with a saved profile, so you log in yourself once (works with 2FA, CAPTCHA and university login). From then on, each page is filled and the tool moves to the next step by itself:
   - it fills the title, running title, abstract, keywords and article type;
   - it adds **every author** (pressing *Add author* when more are needed) and the **suggested reviewers** that have an email;
   - it fills the statements, word, figure and table counts, funder and grant number, and highlights;
   - it answers **Yes/No questions** your statements settle clearly (submitted before? funding? competing interests?);
   - it uploads the manuscript (the anonymised copy for double-blind journals), title page, cover letter, highlights, figures and supplementary files, each with its own item type.

   It works on ScholarOne, Editorial Manager, Elsevier, Snapp, MDPI, OJS and others, including forms inside frames. Safety rules:
   - It only presses **Next/Continue** and **Add author/reviewer** buttons. Any button whose wording mentions submit, approve, build PDF, agree, accept, pay, delete, withdraw, transfer, cancel, back and similar is never pressed. Neither is a link to another site.
   - It **pauses and says why** when the site wants a login, a required box only you can fill is empty, the site shows an error, or it can't tell which button is Next. Fix it, then click **Resume** (or press Next yourself).
   - It **stops at the review / approval page**. You check the PDF and click Submit yourself.
   - It never overwrites a box that already has a value, and it never puts manuscript text in a person box or person details anywhere else.
   - Opposed-reviewer boxes, newsletters, transfer offers and other tick boxes are left alone.
   - Each submission gets `automation.json`, which records the share of boxes the tool filled (shown in the app as "Done by the tool so far").

## Install

```bash
pip install -e '.[browser]'
playwright install chromium
export ANTHROPIC_API_KEY=sk-ant-...      # for the Claude features (or use --no-llm)
export JOURNALFIT_EMAIL=you@university.edu   # optional: OpenAlex polite pool (faster, more reliable)
journalfit data update                    # downloads the SCImago/Scopus journal list (see below if blocked)
```

If `data update` is blocked, open https://www.scimagojr.com/journalrank.php in your browser, click **Download data**, then run `journalfit data import-scimago ~/Downloads/scimagojr*.csv`.

Optional: download Elsevier's free **Scopus Source List** (.xlsx) from the [Scopus content page](https://www.elsevier.com/products/scopus/content/content-policy-and-selection) and run `journalfit data import-scopus scopus.xlsx`. This adds checks for discontinued titles, plus CiteScore.

Optional: put your details in `~/.journalfit/author.json` (`{"name": ..., "email": ..., "affiliation": ...}`) for the cover letter.

## Easiest: the point-and-click app

```bash
journalfit app --shortcut   # once: puts a "JournalFit" icon on your Desktop
```

Double-click the icon and a page opens in your browser (it runs only on your computer). From there you:

1. drag in your manuscript, plus any supplementary files;
2. tick your rules;
3. click **Find journals**;
4. click **Prepare submission** on the journal you want;
5. open the submission website and click **Fill this page** on each form.

You enter your Claude API key and details once under **Settings**. Results are saved in `~/Documents/JournalFit`.

## AI providers

Pick one in the app's **Settings** (or with `--provider` on the command line). Each needs its own API key; a chat subscription such as Claude Pro or ChatGPT Plus does not include API access.

| Provider | Web search | Notes |
|---|---|---|
| `claude-haiku` (default) | yes | Cheap and fully automatic |
| `claude-mix` | yes | Claude Opus for scope check and cover letter, Haiku for metric lookups. Best quality |
| `claude-opus` | yes | Best quality, highest cost |
| `openai` | yes | Default model `gpt-5-mini`, which you can change in Settings |
| `gemini` | yes | Default model `gemini-flash-latest` |
| `deepseek` | yes, via DeepSeek's Search & Fetch API | Cheapest. Model `deepseek-flash`. Pages are searched, filtered to official publisher sites, then read |

Keys can also come from the environment variables `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY` and `DEEPSEEK_API_KEY`.

To keep costs down:
- AI metric lookups run only for the best 8 journals (`--lookup-top`).
- Lookup results are remembered for 90 days.

Every run ends with its token usage and an estimated cost.

## Use from the terminal

```bash
# 1. Rank journals
journalfit find paper.docx --no-oa --scopus --max-first-decision 90 --min-acceptance 20 --min-quartile Q2

# Only some publishers
journalfit find paper.docx --publisher springer,elsevier --no-oa --scopus

# 2. Build the package for one of them (rank number or name)
journalfit prepare paper.docx --pick 2

# 3. Open the submission autopilot for that package (--no-autopilot to press Next yourself)
journalfit assist submissions/paper/<journal>

# Or do it all in one command, for exactly one journal (top-ranked, or --pick):
journalfit submit paper.docx --no-oa --scopus --max-first-decision 90 --min-acceptance 20
```

Other options: `--top N`, `--years N`, `--max-apc USD`, `--exclude-publisher`, `--no-llm` (text matching and scraping only), `--no-web-lookup`, `--no-browser`.

### Metrics you know but the web doesn't show

Some journals only give their acceptance rate on request. Add these to `~/.journalfit/metrics_overrides.csv`, which always takes priority:

```csv
issn,days_to_first_decision,acceptance_rate,impact_factor,citescore,note
0022-2461,32,22,,,"editor email 2026-05"
```

## Limits

- **Impact Factor** (Clarivate JCR) has no free data feed. It appears only where the publisher shows it on the journal page.
- **Acceptance rates** are published by some publishers (Elsevier, Taylor & Francis, some Springer and Wiley journals) and not by many others. Use `--if-missing` to choose how journals without one are treated.
- **Publisher websites change.** If scraping a site stops working, the Claude web lookup takes over; the per-publisher URL rules live in `journalfit/metrics/publishers.py`.
- **Submission portals** differ in how they lay out their forms. The autopilot fills what it can recognise, pauses on anything it can't, and writes what it did on each page to `form_report.txt`. Always check every page before submitting.
- **Reviewer emails** are not in any free database, so you add them. A reviewer without an email is never entered.

## Data sources

- [OpenAlex](https://openalex.org) (CC0)
- [SCImago Journal Rank](https://www.scimagojr.com) (Scopus-based)
- Elsevier's Scopus Source List
- Publishers' own journal pages
- Claude (Anthropic API) for profiling, scope review, guidelines and the cover letter

## Tests

```bash
pip install -e '.[dev,browser]' && pytest
```
