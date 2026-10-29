# linkedin-pulse

**An autonomous agent that monitors LinkedIn for new jobs in the background, evaluates them against your experience pool, and emails you a tailored one-page resume for every strong match.**

Job hunting usually means constantly refreshing job boards, skimming dozens of postings that turn out to want "5+ years" or a security clearance, and manually rewriting the same resume over and over. `linkedin-pulse` runs that entire loop for you on a heartbeat: every 30 minutes, it pulls the newest postings, screens them, scores them against your experience with Claude, and for every strong match, builds a custom one-page resume and sends it straight to your inbox with the job link and match breakdown. 

*(Note: Auto-applying is temporarily omitted due to LinkedIn’s strict anti-automation risk policies).*

> 🏆 Built for [c0mpiled-16: UCSD](https://luma.com/compiled-im3b) (October 24, 2026) as a hackathon submission.
>
> [Demo video](https://drive.google.com/file/d/1X1NKJcGOmuTrfUIz9EKLBgnB4PFBJIvD/view?usp=sharing)

---

## What you get

This system continuously monitors newly posted jobs on LinkedIn in the background, automatically selects and combines items from your experience pool to generate a resume tailored to the exact JD, and automatically sends an email notification with the generated resume attached to prompt your application.

```
Subject:    [Pulse 93] Software Development Engineer - 2026 (US) @ Amazon
Attachment: resume_Amazon.pdf

Software Development Engineer - 2026 (US) @ Amazon
Seattle, WA | posted 2026-10-22
https://www.linkedin.com/jobs/view/software-development-engineer-2026-us-at-amazon-4455784915

Match score: 93/100
- The candidate meets every basic qualification: multiple required programming languages,
  data structure and algorithm work, and a B.S. in Computer Science in progress.
- Experience goes well beyond the SDE-I baseline, including internships at Amazon Web
  Services and Stripe, plus cloud, CI/CD, and on-call work, so the level fits.
- No security clearance, citizenship requirement, or visa restriction appears in the
  posting, so no cap applies.

Matched: Go/Python/Java/C++, data structures and algorithms, AWS, PostgreSQL/DynamoDB, CI/CD, ...
Missing: -

Tailored resume attached.
```

## How it works

```mermaid
flowchart TB
    HB([heartbeat every 30 min]) --> E[entry node: search LinkedIn,<br/>drop jobs already seen]
    E -- no new jobs --> Z([sleep])
    E -- new jobs --> F1

    subgraph JA[Job agent]
        F1[age + title filter] --> F2[fetch JD, keyword pre-filter]
        F2 --> S[Claude: match score 0-100 with reasons]
        S -- below 70 --> R[record and skip]
    end

    S -- 70 or higher --> SEL

    subgraph RA[Resume agent]
        SEL[Claude: pick components for this JD] --> C[compile with pdflatex,<br/>measure page fill]
        C -- too empty or overflows --> FIT[tighten layout, or<br/>Claude adds / drops components]
        FIT --> C
    end

    C -- exactly one full page --> M[email: job link, score, reasons, PDF]
```

- **Job agent.** New postings pass through a multi-stage funnel ordered from cheap to expensive: free filters on age and title first, a keyword check on the job description, and finally a Claude call that scores the match from 0 to 100 while explaining why. Hard blockers (like required security clearances) cap the score immediately.
- **Resume agent.** Your profile is structured as a catalog of pre-written components or blocks stored in your experience pool. For each strong match, Claude intelligently selects the most relevant blocks, and a loop compiles the LaTeX resume, adjusting the layout until it fits **exactly one page**. Bullets are never artificially rewritten, ensuring factual integrity.
- **Delivery.** Each qualified match triggers an email containing the direct job link, score, detailed reasoning, and the compiled PDF. Every job is cached to memory, ensuring subsequent heartbeats only process fresh postings.

## Results from a real run

One heartbeat across three search queries, processing 45 new postings:

| Pipeline Stage | Jobs Count |
|---|---|
| Rejected by age/title (pre-fetch) | 17 |
| Rejected by keyword pre-filter | 4 |
| Scored below 70 by Claude | 10 |
| **Matched: tailored resume built and emailed** | **14** |

Total Claude API spend for the run, including all 14 custom resumes: **about $0.04**.

## Quick start

**Requirements:** Python 3.11+, a LaTeX distribution with `pdflatex` (macOS: `brew install --cask basictex`), and an Anthropic API key.

```bash
pip install -e .
```

```bash
cp .env.example .env
```

```bash
cp config.example.yaml config.yaml
```

Insert your `ANTHROPIC_API_KEY` into `.env`, then run a single heartbeat test:

```bash
python main.py --once
```

Remove `--once` to keep the agent running in the background. Search queries, filters, score thresholds, and models are configured in `config.yaml`, your local copy of `config.example.yaml`. It is gitignored, as are the profiles in `assets/candidates/` other than the `john-wang` example, so personal details stay out of the repo. Without a `config.yaml`, the example config is used.

Each heartbeat runs every search query (6-8 generated from the resume by default) in every entry of `search.locations` (the whole United States by default), paging newest first until `search.max_jobs` (1000, the most LinkedIn's guest API returns for one search) or until a whole page was already seen on an earlier run, so a steady heartbeat only fetches a page or two per search. LinkedIn's guest API ignores the experience-level and remote filters, so seniority is handled by the title filter and Claude's scoring instead. 

By default, emails are written to `work/<candidate>/outbox/` (dry run). To send real emails, set `email.dry_run: false` and fill in your SMTP settings in `.env`.

### Multiple candidates

Each candidate has their own profile, recipient and job history, so one instance can hunt for several people at once. List them under `candidates` in `config.yaml`:

```yaml
candidates:
  john-wang:
    profile: assets/candidates/john-wang   # resume.json + components/<key>.tex
    email_to: john.wang@example.com
  jane-doe:
    profile: assets/candidates/jane-doe
    email_to: jane@example.com
    queries: ["Data Engineer New Grad"]    # optional; default: generated from the resume
```

To add someone, import their resume PDF (below), or copy `assets/candidates/john-wang/` and edit their content blocks in `components/` and the index in `resume.json`. Every candidate gets their own search keywords, scoring and tailored resumes, and their state stays under `work/<name>/` (`jobs.json`, `keywords.json`, `outbox/`, `resumes/`). A job one candidate already saw is still new to another. Emails are tagged `[Pulse <score> | <name>]` and carry an `X-Pulse-Candidate` header, so a shared inbox can filter them.

To run only some candidates, pass `--candidate`, which can be repeated:

```bash
python main.py --once --candidate john-wang
```

### Tags

While scoring, Claude also tags each job with its type (`27NG`, `27Summer`, `27Intern`, `PartTime`; the year is the one the posting targets) and any explicit work-authorization restriction (`CitizenOnly`, `NoSponsor`). Tags appear at the top of the email body and in `jobs.json`. To filter on them, set `match.tags` in `config.yaml`, or `tags` on a candidate to override it:

```yaml
match:
  tags:
    include: ["*NG", "*Summer"]   # keep only these job types (case-insensitive globs); empty keeps all
    exclude: ["CitizenOnly"]      # drop jobs with any of these tags
```

### Web UI

```bash
python -m pulse.web
```

Open http://127.0.0.1:8765 to see every recorded job in a table: score, tags, pipeline outcome, a link to the posting and the tailored resume PDF, plus the match reasons and job description under "details". You can set your own status for each job (`new`, `applied`, `oa`, `interviewing`, `offer`, `rejected`, `skipped`) and keep notes. These go to `work/<candidate>/tracking.json`, separate from the pipeline's `jobs.json`, so the UI can stay open while the heartbeat runs. The server has no auth and listens on localhost by default. Change that with `--host` / `--port`.

### Importing a resume PDF

```bash
python -m pulse.importer jane.pdf --name jane-doe
```

Claude reads the PDF and extracts plain text into a fixed schema, copying every bullet verbatim. The LaTeX is then rendered from the templates in `assets/component_templates/`, so imported profiles have the same structure as hand-written ones. Sections other than work experience (leadership, awards, publications, ...) become projects.

The new profile is installed to `assets/candidates/<name>/` only after these checks pass:
- Every bullet parses back from its `.tex` file.
- All components compile together. A preview is written to `work/<name>/import/preview/`.

Bullets that can't be found in the PDF's own text are reported as warnings to review. This check needs Ghostscript and is skipped without it. An existing profile is never overwritten, and `config.yaml` is not modified: the importer prints the entry to add.


## Team

- Yuchen Xiong
- Yinyi Liu
