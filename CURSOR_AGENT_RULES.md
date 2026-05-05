# Cursor Agent Rules for Kuvalda Blog Image Audit

## Goal
You are working inside a local project folder that contains a scraper and supporting documents for auditing images from the Kuvalda blog.

Your job is to run the project locally and produce a real pilot result for **5 to 8 articles**, not a mock result.

## Primary outcome
Produce a local export folder (**`kuvalda_master/`** by default) containing:
- downloaded images for selected articles
- **`kuvalda_image_report.html`** (основной просмотр, таблица в браузере)
- **`kuvalda_articles.csv`** (список статей для следующих прогонов / merge)
- `summary.json` and a short human summary

## Scope for this run
Do **not** start with the whole blog.
Run a pilot on **5 to 8 articles only**.
Prefer a diverse sample from different categories.

## Source of truth files
Read these files first and treat them as the project specification:
- `kuvalda_blog_audit_scraper.py`
- `CURSOR_RUNBOOK.md`
- `WEAVY_BATCH_WORKFLOW.md`
- `WEAVY_NODE_TEXTS_COPY_PASTE.md`
- `CLIENT_HANDOFF.md`
- `sample_manifest.csv`

## Required behavior
1. Inspect the whole folder before changing anything.
2. Create and activate a local Python virtual environment.
3. Install dependencies.
4. Run a pilot scrape for 5 to 8 articles.
5. If something breaks, fix the code and rerun.
6. Keep changes minimal and practical.
7. Do not stop at a plan. Execute.
8. If image classification is uncertain, mark the image as `review` and download it.
9. Keep folder structure clean and predictable.
10. At the end, provide a concise report with paths and counts.

## Business rules for image selection
Classify each image into one of these statuses:
- `include` — download and include for later AI replacement
- `review` — download and flag for human review
- `skip` — do not download for AI replacement, but keep it in the manifest if the scraper supports that

### Include
Use `include` when the image is likely a generic editorial, stock-like, neutral contextual, or general illustrative photo that could be replaced.
Typical examples:
- generic room interiors
- generic island / landscape / nature photo
- generic workers or context scene without clear product-brand dependence
- general lifestyle images used to support article meaning

### Review
Use `review` when there is doubt.
Examples:
- people in branded or semi-branded context but not fully clear
- unclear authorship
- event/reportage-like photo that may still be replaceable
- any case where skipping would risk missing a replaceable image

### Skip
Use `skip` when the image is not suitable for replacement.
Examples:
- clearly branded supplier product photos
- photos where a specific product model is the actual subject of the article
- author portfolio works or unique works belonging to the person featured in the article
- diagrams, tables, infographics, screenshots, schematics
- images with heavy readable text overlay
- instructional graphics where text is essential

## Folder output expectations
The result should be easy for a human to review and later upload into Weavy.
Prefer this kind of structure:
- `kuvalda_master/downloaded_images/NNN_<article-slug>/...`
- `kuvalda_master/kuvalda_articles.csv`
- `kuvalda_master/kuvalda_image_report.html`
- `kuvalda_master/summary.json`
- `kuvalda_master/pilot_report.md`

## Pilot article strategy
Choose 5 to 8 articles.
Try to include a mix such as:
- one general buying guide
- one new product article
- one author/story article
- one mixed article with both replaceable and non-replaceable images
- one event or reportage-like article

## Acceptance criteria
The task is complete only when all of the following are true:
1. The scraper has actually been run locally.
2. A pilot export folder exists.
3. Images were actually downloaded for `include` and `review` cases.
4. A manifest exists with enough metadata to understand what each file is.
5. The final report states:
   - which articles were processed
   - how many images were included, review, skipped
   - where the output folder is located
   - what code changes were made, if any

## Final report format
At the end, return a short report in this structure:

### Pilot run completed
- Articles processed: X
- Images downloaded: X
- Include: X
- Review: X
- Skip: X
- Output folder: `...`
- Code/files changed: `...`
- Remaining manual review points: `...`

## Important constraint
Do not attempt a full-blog crawl in the first pass unless explicitly asked after the pilot succeeds.
