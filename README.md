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
   - SJR, quartile and h-index from SCImago (Scopus data).

   The source of every number is recorded. A missing value is shown as "n/p" (not published), never guessed.
5. **Applies your metric limits** (`--max-first-decision`, `--min-acceptance`). Journals that don't publish a metric are kept and flagged, or skipped with `--if-missing skip`.
6. **Scope review.** Claude rates the finalists 0–10 the way an editor would. Likely desk rejections drop down the ranking.
7. **Ranks the journals and writes `shortlist.md` and `shortlist.json`**, with reasons, metrics and sources for each one.
8. **Prepares the package** for the journal you pick:
   - reads the journal's author guidelines;
   - writes `checklist.md` with mismatches to fix (word limit, abstract length and so on) and the items required at submission;
   - drafts a cover letter (`.docx` and `.txt`);
   - writes `metadata.json` for the forms.
9. **Submission assistant.** It opens a real browser with a saved profile, so you log in yourself once (works with 2FA, CAPTCHA and university login). On each form page you press Enter in the terminal and it fills in the title, running title, abstract and keywords and uploads the manuscript and cover letter, matching fields by their labels. It works on ScholarOne, Editorial Manager, Elsevier, Snapp, MDPI, OJS and others, including forms inside frames.
   - It never overwrites a field that already has a value.
   - It never fills author or person-detail fields.
   - It **never clicks any button**, so it cannot submit anything. You move between pages and click Submit yourself.

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

1. drag in your manuscript;
2. tick your rules;
3. click **Find journals**;
4. click **Prepare submission** on the journal you want;
5. open the submission website and click **Fill this page** on each form.

You enter your Claude API key and details once under **Settings**. Results are saved in `~/Documents/JournalFit`.

## Use from the terminal

```bash
# 1. Rank journals
journalfit find paper.docx --no-oa --scopus --max-first-decision 90 --min-acceptance 20 --min-quartile Q2

# Only some publishers
journalfit find paper.docx --publisher springer,elsevier --no-oa --scopus

# 2. Build the package for one of them (rank number or name)
journalfit prepare paper.docx --pick 2

# 3. Open the browser assistant for that package
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
- **Submission portals** differ in how they lay out their forms. The assistant fills what it can recognise and tells you what it filled. Always check every page before submitting.

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
