# Cursor runbook: Kuvalda blog image audit + Weavy prep

## Goal
Use the files in this folder to produce a complete local export of selected Kuvalda blog images for later replacement in Weavy.

Deliverables:
1. A working local crawl/export of the Kuvalda blog.
2. Downloaded images under `downloaded_images/NNN_article-slug/` (номер статьи в прогоне).
3. **`kuvalda_image_report.html`** — основной просмотр: широкая таблица, превью, статусы цветом.
4. **`kuvalda_articles.csv`** — список обработанных статей (для `--merge-manifests` и следующих батчей).
5. `summary.json`, `pilot_report.md` — краткая сводка.
6. Conservative classification of each image into `include`, `review`, or `skip`.

## Source files in this folder
- `kuvalda_blog_audit_scraper.py` — main scraper/audit script
- `sample_manifest.csv` — пример структуры (справочно)
- `WEAVY_BATCH_WORKFLOW.md` — recommended Weavy batch workflow
- `WEAVY_NODE_TEXTS_COPY_PASTE.md` — copy/paste node texts
- `CLIENT_HANDOFF.md` — recommendations for client delivery

## Business rules
The script and final output must follow these rules.

### `include`
Download and prepare for Weavy when the image is likely a generic editorial / stock-like / context image.
Examples:
- neutral interiors
- generic lifestyle photos
- non-branded scenic/context photos
- uncertain images where it is safer to review manually later

### `review`
Download and flag for manual review when uncertain.
If unsure, prefer `review` over `skip`.
Examples:
- mixed scenes with people and tools
- possible supplier/editorial photos without obvious text overlay
- images where origin is unclear
- cases where some branding may be present but not definitive

### `skip`
Do not download for Weavy replacement when the image is clearly unsuitable.
Examples:
- branded product/supplier images where the specific product/model matters
- images of author works / portfolio pieces where the article is about that creator's work
- tables, schemas, infographics, or images with heavy text overlay
- images where text in the picture is essential to meaning

## Required output structure
Каталог по умолчанию — **`kuvalda_master/`** (или `--out-dir`):

```text
kuvalda_master/
  downloaded_images/
    NNN_article-slug/
      001_....jpg
      007_...._review.jpg   # суффикс _review — «на проверку»
  kuvalda_articles.csv
  kuvalda_image_report.html
  summary.json
  pilot_report.md
```

## What Cursor should do
1. Inspect this folder.
2. Open and understand `kuvalda_blog_audit_scraper.py`.
3. Create a virtual environment if needed.
4. Install dependencies.
5. Run the scraper.
6. If the site blocks requests or HTML selectors fail, improve the scraper conservatively.
7. Re-run until output files are produced successfully.
8. Validate that HTML and `kuvalda_articles.csv` contain enough context (название, URL статьи, ссылки на картинки, путь к файлу, статус).
9. Give a short summary of:
   - number of articles found
   - number of images found
   - include/review/skip counts
   - any failures or pages that need re-run

## Commands Cursor can use
Typical setup:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install cloudscraper beautifulsoup4 pillow lxml
```

Optional OCR support:

```bash
pip install pytesseract
```

Run command:

```bash
python kuvalda_blog_audit_scraper.py --out-dir ./kuvalda_master --max-pages 200 --delay 1.0
```

Одна или несколько статей по прямым URL (без обхода ленты блога):

```bash
python kuvalda_blog_audit_scraper.py --out-dir ./kuvalda_master --article "https://www.kuvalda.ru/blog/articles/polz/gid-po-viboru-kondicionera-dlya-doma.html"
```

Просмотр: откройте **`kuvalda_image_report.html`** в браузере (таблица по статьям, цвет статуса: зелёный «Скачано», розовый «Скачано — проверить», серый «Не скачиваем» / ошибка). В колонке **«Картинка на сайте»** — ссылка на оригинал на CDN (открыть в новой вкладке; отдельной кнопки «скачать» в отчёте нет).

Собрать экспорт из одного или нескольких `kuvalda_articles.csv` (URL без дублей):

```bash
python kuvalda_blog_audit_scraper.py --out-dir ./kuvalda_master \
  --merge-manifests ./kuvalda_master/kuvalda_articles.csv
```

Картинки сохраняются в `downloaded_images/NNN_slug-stati/`. Для longread-гайдов (`div.page`) инфографика под соответствующими заголовками — **Не скачиваем** без файла на диске.

### Следующий батч (10–20 статей) — без запуска сейчас, шаблон команды

Скрапер уже умеет ограничивать ленту: `--max-articles 20` вместе с `--max-pages` (например 3–5 страниц ленты). Пример **когда попросите**:

```bash
source .venv/bin/activate
python kuvalda_blog_audit_scraper.py --out-dir ./kuvalda_master --max-pages 5 --max-articles 18 --delay 1.0
```

После прогона обновятся `kuvalda_master/kuvalda_articles.csv`, HTML и папки `downloaded_images/`. При необходимости дописать статьи к уже накопленному списку — сохраните старый `kuvalda_articles.csv`, объедините URL вручную или вторым `--merge-manifests` из копии.

More careful run:

```bash
python kuvalda_blog_audit_scraper.py --out-dir ./kuvalda_master --max-pages 200 --delay 1.5 --delete-skipped
```

## If something fails
Cursor should not stop at the first error.
It should:
- inspect traceback or logs
- patch selectors, parsing logic, or networking logic
- retry with conservative changes
- explain exactly what changed

## Acceptance criteria
The task is complete only when all of the following are true:
- scraper runs locally without crashing
- export directory is created (по умолчанию `kuvalda_master/`)
- images are downloaded for `include` and `review`
- **`kuvalda_image_report.html`** is created (таблица для просмотра)
- **`kuvalda_articles.csv`** is created (список статей)
- `summary.json` (и при необходимости `pilot_report.md`)
- output is organized by `NNN_article-slug/`
- uncertain cases default to `review`
- a short operator summary is written at the end

## Weavy preparation requirements
For each manifest row that is `include` or `review`, keep/populate:
- `article_title`
- `article_url`
- `category_slug`
- `article_slug`
- `image_role`
- `image_url`
- `local_path`
- `decision`
- `decision_reason`
- `weavy_title`
- `weavy_context`
- `weavy_goal`

These will be used later in the Weavy flow described in `WEAVY_BATCH_WORKFLOW.md`.

## Final report format Cursor should provide
At the end, Cursor should answer in this structure:

```text
Done.

Export folder:
<path>

Articles found: <n>
Images found: <n>
Downloaded: <n>
Include: <n>
Review: <n>
Skip: <n>

Main fixes made:
- ...
- ...

Files produced:
- ...
- ...

Needs manual review:
- ...
```

## Suggested prompt to give Cursor
Use the files in this folder as the source of truth. Inspect the scraper and docs, set up the environment, run the export locally, fix any scraper issues you encounter, and keep going until you produce a complete export folder with images, **`kuvalda_image_report.html`**, **`kuvalda_articles.csv`**, and summary. If classification is uncertain, default to `review` and download the image. Do not simplify the task into a mock result. I need a real runnable result on my machine.
