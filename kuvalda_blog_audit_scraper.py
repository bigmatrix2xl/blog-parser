#!/usr/bin/env python3
"""
Kuvalda blog image audit scraper.

Purpose
-------
1. Crawl https://www.kuvalda.ru/blog/ and paginated /blog/page-N/ pages.
2. Collect article URLs + preview images.
3. Parse each article and collect article-body images.
4. Classify each image into include / review / skip using conservative rules:
   - if uncertain -> review (and download)
5. Download include/review images into folders by article (перед статьёй папка `NNN_slug` удаляется целиком и заполняется заново).
6. Produce `kuvalda_articles.csv` (список статей для следующих прогонов) и **только HTML**-отчёт по картинкам.

The script is intentionally conservative because the business rule is:
"if unsure, download and flag for review".

Dependencies
------------
pip install cloudscraper beautifulsoup4 pillow lxml
Optional OCR:
  pip install pytesseract
  and install the Tesseract binary separately.

Example
-------
python kuvalda_blog_audit_scraper.py --out-dir ./kuvalda_export --max-pages 200 --delay 1.0

Одна статья по URL (без обхода ленты):

python kuvalda_blog_audit_scraper.py --out-dir ./out \\
  --article "https://www.kuvalda.ru/blog/articles/polz/gid-po-viboru-kondicionera-dlya-doma.html"
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import html
import io
import json
import re
import shutil
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urljoin, urlparse, urlunparse

try:
    import cloudscraper  # type: ignore
except Exception:
    cloudscraper = None

from bs4 import BeautifulSoup, Tag  # type: ignore
from PIL import Image

try:
    import pytesseract  # type: ignore
except Exception:
    pytesseract = None

BASE_URL = "https://www.kuvalda.ru"
BLOG_ROOT = f"{BASE_URL}/blog/"
ARTICLE_RE = re.compile(r"/blog/articles/[^\s\"'#?]+\.html(?:$|[?#])")
IMG_EXT_RE = re.compile(r"\.(?:jpg|jpeg|png|webp)(?:$|\?)", re.I)
MODEL_TOKEN_RE = re.compile(r"\b(?:[A-ZА-Я]{2,}[\-_/]?[A-Z0-9]{1,}|[A-Z]{1,}\d{2,}[A-Z0-9\-]*)\b")
AUTHOR_TITLE_RE = re.compile(
    r"\b(от\s+[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+)?|сварщик|мастер|резьба|скульптор|дизайнер|художник)\b",
    re.I,
)
TEXT_HEAVY_HINT_RE = re.compile(r"\b(таблица|схема|инфограф|сравни|режим|обозначени|формула)\b", re.I)
LOOSE_IMG_URL_IN_ATTR_RE = re.compile(
    r"https?://[^\s\"'<>]+\.(?:jpg|jpeg|png|webp)(?:\?[^\s\"'<>]*)?",
    re.I,
)
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")
LAYOUT_SKIP_HEADING_RE = re.compile(
    r"энергоэффективност|ключевое отличие инвертор|уровень шума|сравнива.*шум|маркировк",
    re.I,
)
LAYOUT_SKIP_SECTION_RE = re.compile(r"производительност.*площад|площад.*комнат", re.I)
LAYOUT_REVIEW_SECTION_RE = re.compile(r"основные типы", re.I)
LAYOUT_INCLUDE_SECTION_RE = re.compile(r"кондиционер в каждую комнату", re.I)
OCR_SKIP_BODY_MIN_WORDS = 18

BRANDS = {
    "makita", "husqvarna", "patriot", "dewalt", "bosch", "fubag", "jileks",
    "dzhileks", "okt", "oktan", "testo", "stihl", "karcher", "a-ipower",
    "intereskol", "интерскол", "husq", "haier", "fubag", "джилекс", "макита",
    "патриот", "хускварна", "деволт", "бош", "керхер", "тесто",     "maktec",
    "haier",
    "хайер",
}

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}


@dataclass
class ArticleMeta:
    article_url: str
    title: str = ""
    category_slug: str = ""
    article_slug: str = ""
    article_index: int = 0
    intro_context: str = ""
    preview_image_url: str = ""
    discovery_page: str = ""
    notes: str = ""


def slugify(text: str) -> str:
    text = text.lower().strip()
    replace_map = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
        "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
        "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
        "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    }
    out = []
    for ch in text:
        if ch.isascii() and (ch.isalnum() or ch in "-_"):
            out.append(ch)
        elif ch in replace_map:
            out.append(replace_map[ch])
        else:
            out.append("-")
    slug = re.sub(r"-+", "-", "".join(out)).strip("-")
    return slug or "item"


class Fetcher:
    def __init__(self, delay: float = 1.0, timeout: int = 30):
        self.delay = delay
        self.timeout = timeout
        if cloudscraper is None:
            raise RuntimeError(
                "cloudscraper is not installed. Install it with: pip install cloudscraper"
            )
        self.scraper = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows", "mobile": False})
        self.scraper.headers.update(DEFAULT_HEADERS)

    def get_text(self, url: str) -> Optional[str]:
        try:
            resp = self.scraper.get(url, timeout=self.timeout)
            time.sleep(self.delay)
            if resp.status_code >= 400:
                return None
            return resp.text
        except Exception:
            return None

    def get_bytes(self, url: str) -> Optional[bytes]:
        try:
            resp = self.scraper.get(url, timeout=self.timeout, stream=True)
            time.sleep(self.delay)
            if resp.status_code >= 400:
                return None
            return resp.content
        except Exception:
            return None


def choose_main_article_node(soup: BeautifulSoup) -> Tag:
    # Kuvalda blog (2024+): longreads use `.static-page`; guides use `div.section.container`.
    static_page = soup.select_one(".static-page")
    if static_page and len(static_page.get_text(" ", strip=True)) > 80:
        return static_page
    section_main = soup.select_one("div.section.container")
    if section_main and len(section_main.select("img")) > 0:
        return section_main

    candidates: list[tuple[int, Tag]] = []
    selectors = [
        "article",
        "main",
        "[itemprop='articleBody']",
        ".article",
        ".article-content",
        ".content",
        ".post-content",
        ".blog-detail",
        ".detail-text",
        ".static-page__block-content",
    ]
    for sel in selectors:
        for node in soup.select(sel):
            text_len = len(node.get_text(" ", strip=True))
            img_count = len(node.select("img"))
            score = text_len + img_count * 250
            candidates.append((score, node))
    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]
    return soup.body or soup


def canonical_image_url(u: str) -> str:
    """Один ключ для одинакового файла при разном query/fragment (?v=…) в URL."""
    p = urlparse(u)
    scheme = (p.scheme or "https").lower()
    netloc = (p.netloc or "").lower()
    path = p.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return urlunparse((scheme, netloc, path, "", "", ""))


def should_ignore_image_url(url: str) -> bool:
    """Служебные картинки ленты/шаблона (аватар), не контент статьи."""
    if not url:
        return True
    u = url.lower()
    return "kuvalda-avatar" in u or "/kuvalda-avatar." in u


def extract_weavy_article_context(main_node: Tag, soup: BeautifulSoup, title: str, min_sentences: int = 5, max_sentences: int = 10) -> str:
    """5–10 осмысловых предложений из тела статьи для контекста в Weavy."""
    parts: list[str] = []
    for node in main_node.select("p, li, div.static-text__description, div.static-text"):
        txt = normalize_ws(node.get_text(" ", strip=True))
        if len(txt) < 40:
            continue
        if txt in parts:
            continue
        parts.append(txt)
        if len(parts) >= 35:
            break
    blob = " ".join(parts)
    if len(blob) < 120:
        blob = normalize_ws(main_node.get_text(" ", strip=True))[:8000]
    raw_sents = [normalize_ws(s) for s in SENTENCE_SPLIT_RE.split(blob) if normalize_ws(s)]
    sentences: list[str] = []
    for s in raw_sents:
        if len(s) < 25:
            continue
        sentences.append(s)
        if len(sentences) >= max_sentences:
            break
    if len(sentences) < min_sentences:
        desc = soup.find("meta", attrs={"name": "description"})
        extra = normalize_ws(desc.get("content", "")) if desc else ""
        if extra and extra not in " ".join(sentences):
            for s in SENTENCE_SPLIT_RE.split(extra):
                s = normalize_ws(s)
                if len(s) >= 25:
                    sentences.append(s)
                if len(sentences) >= max_sentences:
                    break
    if not sentences:
        return title
    return " ".join(sentences[:max_sentences])


def designer_status_ru(decision: str, local_path: Optional[Path]) -> str:
    """Статусы: не скачиваем / скачано / скачано — проверить."""
    has_file = bool(local_path and local_path.exists())
    if decision == "skip":
        return "Не скачиваем"
    if decision == "include" and has_file:
        return "Скачано"
    if decision == "review" and has_file:
        return "Скачано — проверить"
    if decision in ("include", "review") and not has_file:
        return "Не удалось скачать"
    return "Не скачиваем"


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def listing_thumb_from_blog_anchor(a: Tag) -> str:
    """Превью карточки на /blog/: у Kuvalda это <a class=\"preview__image\" data-background=\"...jpg\"> без <img>."""
    if not isinstance(a, Tag):
        return ""
    for attr in ("data-background", "data-bg", "data-image", "data-src"):
        val = a.get(attr)
        if isinstance(val, str) and IMG_EXT_RE.search(val):
            v = val.strip()
            if v.startswith("//"):
                return "https:" + v
            return v
    style = a.get("style") or ""
    sm = re.search(r"url\(\s*['\"]?([^'\")\s]+)", style, re.I)
    if sm:
        u = sm.group(1).strip()
        if IMG_EXT_RE.search(u):
            return u
    return ""


def iter_article_urls_from_listing(soup: BeautifulSoup) -> Iterable[tuple[str, Optional[str]]]:
    seen: set[str] = set()
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if not href:
            continue
        full = urljoin(BASE_URL, href)
        if not ARTICLE_RE.search(full):
            continue
        full = full.split("#", 1)[0]
        if full in seen:
            continue
        seen.add(full)
        preview_img = listing_thumb_from_blog_anchor(a) or ""
        img = a.find("img") if isinstance(a, Tag) else None
        if not preview_img and img is not None:
            preview_img = best_img_src_loose(img) or ""
        if not preview_img:
            parent = a
            for _ in range(8):
                img = parent.find("img") if isinstance(parent, Tag) else None
                if img is not None:
                    preview_img = best_img_src_loose(img)
                    if preview_img:
                        break
                parent = parent.parent  # type: ignore[assignment]
                if parent is None:
                    break
        if preview_img:
            preview_img = urljoin(BASE_URL, preview_img)
        yield full, preview_img or None



def best_img_src(img: Tag) -> str:
    for attr in ("src", "data-src", "data-original", "data-lazy-src"):
        val = img.get(attr)
        if val and IMG_EXT_RE.search(val):
            return val
    srcset = img.get("srcset") or img.get("data-srcset")
    if srcset:
        first = srcset.split(",")[0].strip().split(" ")[0]
        if IMG_EXT_RE.search(first):
            return first
    return ""


def best_img_src_loose(img: Tag) -> str:
    """Как best_img_src, плюс поиск URL картинки в любых data-* (лента блога)."""
    s = best_img_src(img)
    if s:
        return s
    for _attr, val in img.attrs.items():
        if not isinstance(val, str):
            continue
        if IMG_EXT_RE.search(val):
            if val.startswith("//"):
                return "https:" + val
            if val.startswith("http"):
                return val
        m = LOOSE_IMG_URL_IN_ATTR_RE.search(val)
        if m:
            return m.group(0)
    return ""


def kit_relative_display_path(path_str: str) -> str:
    """Путь от каталога `kuvalda_automation_kit/` для отображения в HTML."""
    if not path_str:
        return ""
    s = str(Path(path_str).resolve()).replace("\\", "/")
    marker = "kuvalda_automation_kit/"
    idx = s.find(marker)
    if idx >= 0:
        return s[idx:]
    return s


def attach_flow_context(main: Tag, records: list[dict[str, str]]) -> None:
    """Контекст по порядку блоков в div.page: заголовок раздела, пустой абзац перед картинкой, следующий заголовок."""
    page = main.select_one("div.page") or main
    seq = list(page.find_all(["h2", "h3", "h4", "p", "img"], recursive=True))
    last_heading = ""
    prev_was_empty_p = False
    url_to_flow: dict[str, dict[str, str | bool]] = {}

    for i, tag in enumerate(seq):
        if tag.name in ("h2", "h3", "h4"):
            last_heading = normalize_ws(tag.get_text(" ", strip=True))
            prev_was_empty_p = False
        elif tag.name == "p":
            t = normalize_ws(tag.get_text(" ", strip=True))
            prev_was_empty_p = len(t) < 5
        elif tag.name == "img":
            src = best_img_src(tag)
            if not src:
                continue
            full = urljoin(BASE_URL, src)
            next_heading = ""
            for j in range(i + 1, min(i + 18, len(seq))):
                if seq[j].name in ("h2", "h3", "h4"):
                    nh = normalize_ws(seq[j].get_text(" ", strip=True))
                    if len(nh) > 2:
                        next_heading = nh
                        break
            url_to_flow[full] = {
                "flow_last_heading": last_heading,
                "flow_empty_p_before": prev_was_empty_p,
                "flow_next_heading": next_heading,
            }
            prev_was_empty_p = False

    for rec in records:
        if rec.get("role") != "body":
            continue
        u = rec.get("url", "")
        flow = url_to_flow.get(u, {})
        rec["flow_last_heading"] = str(flow.get("flow_last_heading", "") or "")
        rec["flow_empty_p_before"] = bool(flow.get("flow_empty_p_before", False))
        rec["flow_next_heading"] = str(flow.get("flow_next_heading", "") or "")


def classify_layout_flow(img: dict[str, str]) -> Optional[str]:
    """Эвристики по вёрстке longread: инфографика/таблицы → skip; явный сток блока → include; сомнительно → review."""
    if img.get("role") != "body":
        return None
    lh = (img.get("flow_last_heading") or "").strip()
    nh = (img.get("flow_next_heading") or "").strip()
    empty_p = bool(img.get("flow_empty_p_before"))

    if LAYOUT_SKIP_HEADING_RE.search(lh):
        return "skip"
    if LAYOUT_SKIP_SECTION_RE.search(lh):
        return "skip"
    if empty_p and re.search(r"осуш", lh, re.I) and LAYOUT_SKIP_SECTION_RE.search(nh):
        return "skip"
    if LAYOUT_INCLUDE_SECTION_RE.search(lh):
        return "include"
    if LAYOUT_REVIEW_SECTION_RE.search(lh):
        return "review"
    return None


def parse_article(fetcher: Fetcher, meta: ArticleMeta) -> tuple[ArticleMeta, list[dict[str, str]]]:
    html_text = fetcher.get_text(meta.article_url)
    if not html_text:
        raise RuntimeError(f"Could not fetch article: {meta.article_url}")
    soup = BeautifulSoup(html_text, "lxml")
    title = ""
    h1 = soup.find("h1")
    if h1:
        title = normalize_ws(h1.get_text(" ", strip=True))
    if not title:
        title = normalize_ws(soup.title.get_text(" ", strip=True)) if soup.title else meta.article_slug
    parsed = urlparse(meta.article_url)
    parts = [p for p in parsed.path.split("/") if p]
    category_slug = parts[2] if len(parts) >= 4 else meta.category_slug
    article_slug = parts[-1].replace(".html", "") if parts else meta.article_slug
    meta.title = title
    meta.category_slug = category_slug
    meta.article_slug = article_slug
    if should_ignore_image_url(meta.preview_image_url):
        meta.preview_image_url = ""
    main = choose_main_article_node(soup)
    meta.intro_context = extract_weavy_article_context(main, soup, title)

    og_image = ""
    for key in (("property", "og:image"), ("name", "og:image"), ("property", "twitter:image")):
        tag = soup.find("meta", attrs={key[0]: key[1]})
        if tag and tag.get("content"):
            og_image = urljoin(BASE_URL, tag["content"])
            break
    if should_ignore_image_url(og_image):
        og_image = ""
    listing_preview = (meta.preview_image_url or "").strip()
    if should_ignore_image_url(listing_preview):
        listing_preview = ""

    records: list[dict[str, str]] = []
    seen_imgs: set[str] = set()

    # Всегда отдельная строка отчёта: превью карточки с ленты /blog/ (URL не в seen — может совпасть с фото в статье).
    if listing_preview:
        records.append(
            {
                "role": "listing_blog",
                "url": listing_preview,
                "alt": title,
                "context": "",
            }
        )

    # Превью со страницы статьи (og), если отличается от ленты.
    preview_specs: list[tuple[str, str]] = []
    if og_image and og_image != listing_preview:
        preview_specs.append((og_image, "Превью (og:image) на странице статьи"))
    elif not listing_preview and og_image:
        preview_specs.append((og_image, "Превью (og:image) на странице статьи"))
    for purl, pctx in preview_specs:
        if should_ignore_image_url(purl):
            continue
        can = canonical_image_url(purl)
        if can in seen_imgs:
            continue
        seen_imgs.add(can)
        records.append({"role": "preview", "url": purl, "alt": title, "context": pctx})

    order = 0
    for img in main.select("img"):
        src = best_img_src(img)
        if not src:
            continue
        src = urljoin(BASE_URL, src)
        if should_ignore_image_url(src):
            continue
        src_can = canonical_image_url(src)
        if src_can in seen_imgs:
            continue
        if not IMG_EXT_RE.search(src):
            continue
        seen_imgs.add(src_can)
        order += 1
        alt = normalize_ws(img.get("alt", ""))
        parent_text = ""
        parent = img.parent
        depth = 0
        while isinstance(parent, Tag) and depth < 3:
            txt = normalize_ws(parent.get_text(" ", strip=True))
            if txt and txt != alt:
                parent_text = txt[:300]
                break
            parent = parent.parent
            depth += 1
        records.append({
            "role": "body",
            "url": src,
            "alt": alt,
            "context": parent_text,
        })
    attach_flow_context(main, records)
    return meta, records



def detect_brand_tokens(*chunks: str) -> tuple[list[str], list[str]]:
    text = " ".join(normalize_ws(c).lower() for c in chunks if c)
    brand_hits = sorted({b for b in BRANDS if b in text})
    model_hits = sorted(set(MODEL_TOKEN_RE.findall(" ".join(chunks))))
    return brand_hits, model_hits



def ocr_excerpt_from_pil(pil_img: Image.Image) -> tuple[str, int]:
    if pytesseract is None:
        return "", 0
    try:
        text = pytesseract.image_to_string(pil_img, lang="rus+eng")
        cleaned = normalize_ws(text)
        if not cleaned:
            return "", 0
        score = len(re.findall(r"\w+", cleaned))
        return cleaned[:240], score
    except Exception:
        return "", 0


def ocr_excerpt(image_path: Path) -> tuple[str, int]:
    if pytesseract is None:
        return "", 0
    try:
        with Image.open(image_path) as pil_img:
            return ocr_excerpt_from_pil(pil_img)
    except Exception:
        return "", 0


def classify_image(
    meta: ArticleMeta,
    img_url: str,
    role: str,
    alt_text: str,
    context_text: str,
    local_path: Optional[Path],
    *,
    ocr_word_score: int = 0,
) -> tuple[str, str, str, str, str, int]:
    title = meta.title
    article_text = f"{title} {meta.intro_context} {alt_text} {context_text} {img_url}"
    brand_hits, model_hits = detect_brand_tokens(title, meta.intro_context, alt_text, context_text, img_url)
    ocr_text, text_score = ("", 0)
    if local_path and local_path.exists():
        ocr_text, text_score = ocr_excerpt(local_path)
        ocr_brand_hits, ocr_model_hits = detect_brand_tokens(ocr_text)
        brand_hits = sorted(set(brand_hits + ocr_brand_hits))
        model_hits = sorted(set(model_hits + ocr_model_hits))
    elif ocr_word_score > 0:
        text_score = ocr_word_score

    article_brand_heavy = bool(brand_hits) or len(model_hits) >= 2
    newinstr_like = meta.category_slug == "newinstr"
    author_story = meta.category_slug == "zolotye_ruky" or bool(AUTHOR_TITLE_RE.search(title))
    text_hint = bool(TEXT_HEAVY_HINT_RE.search(article_text))
    has_obvious_text_overlay = text_score >= 8 or text_hint
    ocr_text_heavy = role == "body" and text_score >= OCR_SKIP_BODY_MIN_WORDS

    # Rule 1: text-heavy diagrams/tables/infographics — только для картинок из тела статьи.
    if (has_obvious_text_overlay or ocr_text_heavy) and role == "body":
        return (
            "skip",
            "text-heavy / infographic / table; poor target for AI remake",
            "high",
            ", ".join(brand_hits),
            ", ".join(model_hits),
            text_score,
        )

    # Rule 2: obvious branded product/supplier photo -> skip.
    if newinstr_like and article_brand_heavy:
        return (
            "skip",
            "brand/model-heavy supplier or product article; keep original source image",
            "high",
            ", ".join(brand_hits),
            ", ".join(model_hits),
            text_score,
        )

    if article_brand_heavy and len(model_hits) >= 1 and role == "body":
        return (
            "skip",
            "obvious branded product/model signals in title/alt/OCR",
            "medium",
            ", ".join(brand_hits),
            ", ".join(model_hits),
            text_score,
        )

    # Rule 3: author portfolio / original works -> mostly skip, but uncertain => review.
    if author_story:
        if brand_hits or model_hits:
            return (
                "review",
                "author/story article but mixed commercial signals; download for manual check",
                "low",
                ", ".join(brand_hits),
                ", ".join(model_hits),
                text_score,
            )
        return (
            "review",
            "author/story article; likely original portfolio/work image, requires human review",
            "low",
            ", ".join(brand_hits),
            ", ".join(model_hits),
            text_score,
        )

    # Rule 4: generic editorial/lifestyle/stock-like -> include.
    if not brand_hits and not model_hits and text_score <= 2:
        return (
            "include",
            "generic non-branded editorial / stock-like image",
            "medium",
            "",
            "",
            text_score,
        )

    # Rule 5: uncertainty -> review and download.
    return (
        "review",
        "uncertain signals; conservative download for manual review",
        "low",
        ", ".join(brand_hits),
        ", ".join(model_hits),
        text_score,
    )



def save_image(fetcher: Fetcher, url: str, target_path: Path) -> bool:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists() and target_path.stat().st_size > 0:
        return True
    data = fetcher.get_bytes(url)
    if not data:
        return False
    target_path.write_bytes(data)
    return True



def site_image_basename(image_url: str) -> str:
    """Имя файла как в URL пути CDN (без префикса порядка и без суффикса _review)."""
    raw = Path(urlparse(image_url).path).name.strip()
    if raw and raw not in (".", ".."):
        return raw
    digest = hashlib.sha256(image_url.encode("utf-8")).hexdigest()[:12]
    return f"image_{digest}.jpg"


def disambiguate_filename(preferred: str, used_lower: set[str]) -> str:
    """Если имя уже занято в папке статьи, добавляет _2, _3 … перед расширением."""
    p = Path(preferred)
    stem, suf = p.stem, p.suffix or ".jpg"
    candidate = f"{stem}{suf}"
    n = 2
    while candidate.lower() in used_lower:
        candidate = f"{stem}_{n}{suf}"
        n += 1
    used_lower.add(candidate.lower())
    return candidate


def build_image_path(
    out_dir: Path,
    article_index: int,
    article_slug: str,
    image_url: str,
    used_lower: set[str],
) -> Path:
    base = site_image_basename(image_url)
    name = disambiguate_filename(base, used_lower)
    folder = f"{article_index:03d}_{article_slug}"
    return out_dir / "downloaded_images" / folder / name


_ARTICLE_IMG_DIR_SLUG_RE = re.compile(r"^\d{3}_(.+)$")


def purge_download_folders_for_slugs(download_root: Path, slugs: set[str]) -> int:
    """Удаляет каталоги NNN_slug, если slug входит в текущий прогон (убирает дубликаты 001_slug vs 008_slug)."""
    if not download_root.exists() or not slugs:
        return 0
    removed = 0
    for entry in sorted(download_root.iterdir()):
        if not entry.is_dir():
            continue
        m = _ARTICLE_IMG_DIR_SLUG_RE.match(entry.name)
        if not m:
            continue
        if m.group(1) in slugs:
            shutil.rmtree(entry)
            removed += 1
    return removed


def read_article_urls_from_manifests(paths: list[Path]) -> list[str]:
    """Читает URL статей из CSV (новые или старые заголовки колонок)."""
    urls: list[str] = []
    for path in paths:
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                u = ""
                for key in ("Ссылка на статью", "article_url", "Article URL"):
                    u = (row.get(key) or "").strip()
                    if u:
                        break
                if u:
                    urls.append(u)
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def normalize_manifest_article_url(raw: str) -> str:
    u = (raw or "").strip()
    if not u:
        return ""
    return urljoin(BASE_URL, u).split("#", 1)[0]


class IncrementalPreserveError(RuntimeError):
    pass


def read_article_manifest_lookup_first_win(paths: list[Path]) -> dict[str, dict[str, str]]:
    """Строки статей из CSV; при дубле URL сохраняется первое вхождение (как в read_article_urls_from_manifests)."""
    seen: set[str] = set()
    by_norm: dict[str, dict[str, str]] = {}
    for path in paths:
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                u = ""
                for key in ("Ссылка на статью", "article_url", "Article URL"):
                    u = (row.get(key) or "").strip()
                    if u:
                        break
                if not u:
                    continue
                nu = normalize_manifest_article_url(u)
                if nu in seen:
                    continue
                seen.add(nu)
                by_norm[nu] = dict(row)
    return by_norm


def manifest_article_rows_stable_order(urls_normalized: list[str], by_norm: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    missing = [u for u in urls_normalized if u not in by_norm]
    if missing:
        raise IncrementalPreserveError(
            f"В CSV манифеста нет {len(missing)} URL из списка «старых» статей для инкрементального режима "
            "(первые три): {missing[:3]!r}"
        )
    return [dict(by_norm[u]) for u in urls_normalized]


def designer_rows_stable_from_snapshot(out_dir: Path, urls_normalized_stable: list[str]) -> list[dict[str, Any]]:
    """Строки отчёта из снимка в порядке stable URL; локальные пути не трогаем."""
    snapshot_rows = load_report_snapshot(out_dir)
    by_article: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in snapshot_rows:
        nu = normalize_manifest_article_url(str(r.get("article_url", "")))
        if nu:
            by_article[nu].append(r)
    missing = [u for u in urls_normalized_stable if u not in by_article]
    if missing:
        raise IncrementalPreserveError(
            f"В {REPORT_SNAPSHOT_FILENAME} нет данных для {len(missing)} статей инкремента "
            "(сделайте полный прогон с --full-reprocess или восстановите снимок). Примеры URL: {missing[:2]!r}"
        )
    out: list[dict[str, Any]] = []
    for u in urls_normalized_stable:
        block = sorted(
            by_article[u],
            key=lambda rr: int(str(rr.get("order", "0") or "0").split(".", 1)[0] or "0"),
        )
        out.extend(block)
    return out


def tally_classification_from_designer_rows(rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    t = {"include": 0, "review": 0, "skip": 0}
    for r in rows:
        sr = str(r.get("status_ru", "")).strip()
        if sr == "Скачано":
            t["include"] += 1
        elif "проверить" in sr:
            t["review"] += 1
        else:
            t["skip"] += 1
    return t


def read_overrides(path: Optional[Path]) -> dict[str, dict[str, str]]:
    if not path or not path.exists():
        return {}
    overrides: dict[str, dict[str, str]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            url = row.get("image_url", "").strip()
            if not url:
                continue
            overrides[url] = row
    return overrides



def discover_articles(
    fetcher: Fetcher,
    max_pages: int = 200,
    max_articles: Optional[int] = None,
) -> list[ArticleMeta]:
    discovered: dict[str, ArticleMeta] = {}
    empty_streak = 0
    for page_num in range(1, max_pages + 1):
        if max_articles is not None and len(discovered) >= max_articles:
            break
        page_url = BLOG_ROOT if page_num == 1 else urljoin(BLOG_ROOT, f"page-{page_num}/")
        html_text = fetcher.get_text(page_url)
        if not html_text:
            empty_streak += 1
            if empty_streak >= 3:
                break
            continue
        soup = BeautifulSoup(html_text, "lxml")
        found_this_page = 0
        for article_url, preview_image in iter_article_urls_from_listing(soup):
            if max_articles is not None and len(discovered) >= max_articles:
                break
            if article_url not in discovered:
                parts = [p for p in urlparse(article_url).path.split("/") if p]
                article_slug = parts[-1].replace(".html", "") if parts else slugify(article_url)
                category_slug = parts[2] if len(parts) >= 4 else ""
                discovered[article_url] = ArticleMeta(
                    article_url=article_url,
                    preview_image_url=preview_image or "",
                    discovery_page=page_url,
                    category_slug=category_slug,
                    article_slug=article_slug,
                )
                found_this_page += 1
        if found_this_page == 0:
            empty_streak += 1
            if empty_streak >= 3:
                break
        else:
            empty_streak = 0
    return list(discovered.values())


def discover_articles_on_listing_pages(fetcher: Fetcher, page_nums: Iterable[int]) -> list[ArticleMeta]:
    """URL статей с указанных страниц ленты (1 = /blog/, 2+ = /blog/page-N/), порядок: N по возрастанию, внутри — как в HTML."""
    nums = sorted({int(p) for p in page_nums if int(p) >= 1})
    out: list[ArticleMeta] = []
    seen: set[str] = set()
    for page_num in nums:
        page_url = BLOG_ROOT if page_num == 1 else urljoin(BLOG_ROOT, f"page-{page_num}/")
        html_text = fetcher.get_text(page_url)
        if not html_text:
            print(f"[append-listing-pages] Пустой ответ или ошибка: {page_url}", file=sys.stderr)
            continue
        soup = BeautifulSoup(html_text, "lxml")
        n_before = len(out)
        for article_url, preview_image in iter_article_urls_from_listing(soup):
            if article_url in seen:
                continue
            seen.add(article_url)
            parts = [p for p in urlparse(article_url).path.split("/") if p]
            article_slug = parts[-1].replace(".html", "") if parts else slugify(article_url)
            category_slug = parts[2] if len(parts) >= 4 else ""
            out.append(
                ArticleMeta(
                    article_url=article_url,
                    preview_image_url=preview_image or "",
                    discovery_page=page_url,
                    category_slug=category_slug,
                    article_slug=article_slug,
                )
            )
        added = len(out) - n_before
        print(f"[append-listing-pages] Стр. ленты {page_num} ({page_url}): добавлено новых статей — {added}", file=sys.stderr)
    print(f"[append-listing-pages] Итого уникальных статей с выбранных страниц: {len(out)}", file=sys.stderr)
    return out


def enrich_articles_with_listing_previews(
    fetcher: Fetcher,
    articles: list[ArticleMeta],
    max_listing_pages: int = 40,
) -> None:
    """Если у статьи нет превью (например, URL только из merge), подставляем картинку с ленты /blog/."""
    need = {a.article_url for a in articles if not (a.preview_image_url or "").strip()}
    if not need:
        return
    found: dict[str, str] = {}
    empty_streak = 0
    for page_num in range(1, max(1, max_listing_pages) + 1):
        if len(found) >= len(need):
            break
        page_url = BLOG_ROOT if page_num == 1 else urljoin(BLOG_ROOT, f"page-{page_num}/")
        html_text = fetcher.get_text(page_url)
        if not html_text:
            empty_streak += 1
            if empty_streak >= 3:
                break
            continue
        empty_streak = 0
        soup = BeautifulSoup(html_text, "lxml")
        for article_url, preview_image in iter_article_urls_from_listing(soup):
            if article_url in need and preview_image and article_url not in found:
                full_pi = urljoin(BASE_URL, preview_image)
                if not should_ignore_image_url(full_pi):
                    found[article_url] = full_pi
    for a in articles:
        if not (a.preview_image_url or "").strip() and a.article_url in found:
            a.preview_image_url = found[a.article_url]


def _html_status_row_class(status_ru: str) -> str:
    """Класс на всю строку таблицы (светлый фон)."""
    if status_ru == "Скачано":
        return "row-ok"
    if "проверить" in status_ru:
        return "row-review"
    return "row-skip"


def image_source_label(role: str, context: str) -> str:
    if role == "listing_blog":
        return "В ленте блога"
    if role != "preview":
        return "В статье"
    ctx = context or ""
    if "og:image" in ctx:
        return "Превью (страницы)"
    return "Превью"


def b64_utf8(text: str) -> str:
    return base64.b64encode((text or "").encode("utf-8")).decode("ascii")


# Иконка «копировать» (как в запросе), inline в кнопке.
COPY_ICON_SVG = (
    '<svg class="copy-ic" width="18" height="18" viewBox="0 0 24 24" fill="none" '
    'xmlns="http://www.w3.org/2000/svg" aria-hidden="true">'
    '<path d="M7.5 3H14.6C16.8402 3 17.9603 3 18.816 3.43597C19.5686 3.81947 20.1805 4.43139 20.564 5.18404C21 6.03969 21 7.15979 21 9.4V16.5M6.2 21H14.3C15.4201 21 15.9802 21 16.408 20.782C16.7843 20.5903 17.0903 20.2843 17.282 19.908C17.5 19.4802 17.5 18.9201 17.5 17.8V9.7C17.5 8.57989 17.5 8.01984 17.282 7.59202C17.0903 7.21569 16.7843 6.90973 16.408 6.71799C15.9802 6.5 15.4201 6.5 14.3 6.5H6.2C5.0799 6.5 4.51984 6.5 4.09202 6.71799C3.71569 6.90973 3.40973 7.21569 3.21799 7.59202C3 8.01984 3 8.57989 3 9.7V17.8C3 18.9201 3 19.4802 3.21799 19.908C3.40973 20.2843 3.71569 20.5903 4.09202 20.782C4.51984 21 5.0799 21 6.2 21Z" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
    "</svg>"
)

REPORT_SNAPSHOT_FILENAME = "kuvalda_image_report_snapshot.json"
SNAPSHOT_VERSION = 1


def designer_row_for_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in row.items() if k != "_local_path_obj"}
    lp = row.get("_local_path_obj")
    if isinstance(lp, Path):
        try:
            out["_local_path_str"] = str(lp.resolve())
        except Exception:
            out["_local_path_str"] = str(lp)
    else:
        out["_local_path_str"] = ""
    return out


def designer_rows_from_snapshot(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Восстанавливает строки для write_html_report из JSON-снимка."""
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        s = (d.pop("_local_path_str", None) or "").strip()
        d["_local_path_obj"] = Path(s) if s else None
        out.append(d)
    return out


def write_report_snapshot(out_dir: Path, designer_rows: list[dict[str, Any]]) -> Path:
    path = out_dir / REPORT_SNAPSHOT_FILENAME
    payload = {
        "snapshot_version": SNAPSHOT_VERSION,
        "rows": [designer_row_for_snapshot(r) for r in designer_rows],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_report_snapshot(out_dir: Path) -> list[dict[str, Any]]:
    path = out_dir / REPORT_SNAPSHOT_FILENAME
    if not path.exists():
        raise FileNotFoundError(f"Нет снимка отчёта: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("rows")
    if not isinstance(rows, list):
        raise ValueError("Снимок повреждён: нет поля rows")
    return designer_rows_from_snapshot(rows)


def help_dialog_statuses_block_html() -> str:
    """Блок «папки и статусы» для справки (клиентская выжимка)."""
    return (
        '<h3 class="help-sub">Папки и имена файлов</h3>'
        "<ul>"
        "<li>Рядом с отчётом лежит папка <strong>downloaded_images</strong>. "
        "У каждой статьи — своя подпапка (номер в прогоне + короткое имя из URL).</li>"
        "<li>В подпапке лежат <strong>только те картинки, которые уже скачали</strong> как кандидаты на работу.</li>"
        "<li><strong>Имя файла</strong> на диске совпадает с именем в ссылке на сайте (как на CDN).</li>"
        "</ul>"
        '<h3 class="help-sub">Статусы в таблице</h3>'
        "<ul>"
        "<li><strong>Скачано</strong> (зелёная строка) — кандидат на замену; файл уже в папке.</li>"
        "<li><strong>Скачано — проверить</strong> (розовая) — кандидат, но возможны авторское фото или спорный кадр; "
        "перед использованием лучше глазами.</li>"
        "<li><strong>Не скачиваем</strong> (серая) — скрипт намеренно не качает: много текста на картинке, схема/инфографика, "
        "жёстко «технический» блок гайда или явный каталожный снимок с моделью. Оригинал на сайте — по ссылке в колонке "
        "«Картинка на сайте» (откроется в браузере).</li>"
        "<li><strong>Не удалось скачать</strong> — решено качать скриптом, но файл не получен (сеть/сайт); оригинал по той же ссылке на CDN.</li>"
        "</ul>"
        "<p class=\"help-note\">В таблице нет отдельной кнопки «скачать»: колонка «Картинка на сайте» — это ссылка на изображение "
        "на CDN; откройте её и при необходимости сохраните через браузер.</p>"
    )


def report_html_only(out_dir: Path) -> Path:
    """Пересобрать kuvalda_image_report.html из снимка, без сети и без скачивания."""
    rows = load_report_snapshot(out_dir)
    return write_html_report(out_dir, rows)


def upgrade_existing_image_report_html(html_path: Path) -> None:
    """
    Обновить существующий kuvalda_image_report.html: актуальная справка и стили для длинных URL;
    убрать устаревшие кнопки/ссылки «скачать» и связанные скрипты (без пересканирования статей).
    """
    if not html_path.exists():
        raise FileNotFoundError(str(html_path))
    raw = html_path.read_text(encoding="utf-8")
    soup = BeautifulSoup(raw, "html.parser")

    head = soup.head
    if head:
        if not soup.find("style", id="kuvalda-report-extra-v1"):
            style_tag = soup.new_tag("style", id="kuvalda-report-extra-v1")
            style_tag.string = (
                "td a { word-break: break-all; } "
                ".copy-wrap .url-cell-text { flex: 1; min-width: 0; } .url-cell-text a { word-break: break-all; } "
                ".help-dlg { max-width: 640px; } .help-sub { font-size: 0.95rem; margin: 14px 0 6px 0; } "
                ".help-note { font-size: 12px; margin: 10px 0 0 0; line-height: 1.5; }"
            )
            head.append(style_tag)

    help_btn = soup.find(id="help-open")
    hb_text = help_btn.get_text("", strip=True) if help_btn else ""
    if help_btn and ("Справка по отчёту" in hb_text or hb_text.startswith("Справка по")):
        help_btn.clear()
        help_btn.append("Справка")

    dlg = soup.find("dialog", id="help-dialog")
    if dlg:
        inner = dlg.find(class_="help-dlg-inner")
        if inner:
            for stale in inner.select("#kuvalda-help-status-block"):
                stale.decompose()
            for stale_sub in inner.select('h2[data-kuvalda-insert="after-status"]'):
                stale_sub.decompose()
            first_h2 = inner.find("h2")
            first_ul = inner.find("ul")
            blk_html = '<div id="kuvalda-help-status-block">' + help_dialog_statuses_block_html() + "</div>"
            block = BeautifulSoup(blk_html, "html.parser")
            blk = block.find(id="kuvalda-help-status-block")
            if first_h2 and first_ul and blk:
                first_h2.clear()
                first_h2.append("Как пользоваться этим файлом")
                sub = soup.new_tag("h2", attrs={"class": "help-sub", "data-kuvalda-insert": "after-status"})
                sub.string = "Как читать таблицу"
                sub["style"] = "margin-top:14px"
                first_ul.insert_before(blk)
                first_ul.insert_before(sub)

    for hint in soup.select(".report-proxy-hint"):
        hint.decompose()
    for tag in soup.find_all("script", id=re.compile(r"^kuvalda-dl-save-script-v\d*$")):
        tag.decompose()
    for el in soup.select("button.icon-action-btn.dl-img-btn"):
        el.decompose()
    for el in soup.select("a.icon-action-btn.dl-via-proxy"):
        el.decompose()

    for table in soup.select("div.group table"):
        tbody = table.find("tbody")
        if not tbody:
            continue
        for tr in tbody.find_all("tr", recursive=False):
            tds = tr.find_all("td", recursive=False)
            if len(tds) < 8:
                continue
            url_td = tds[6]
            wrap = url_td.find("div", class_="copy-wrap")
            if isinstance(wrap, Tag):
                span = wrap.find("span", class_="url-cell-text")
                if isinstance(span, Tag):
                    inner_a = span.find("a", href=True)
                    if inner_a:
                        n_children = sum(1 for ch in wrap.children if getattr(ch, "name", None))
                        if n_children == 1:
                            inner_a.extract()
                            url_td.clear()
                            url_td.append(inner_a)

    html_path.write_text(str(soup), encoding="utf-8")


def write_html_report(out_dir: Path, designer_rows: list[dict[str, Any]]) -> Path:
    """Широкая HTML-таблица по статьям (как ранний designer sample): превью, контекст, пути, статус цветом."""
    html_path = out_dir / "kuvalda_image_report.html"
    rows_by_article: list[tuple[int, str, str, list[dict[str, Any]]]] = []
    current_url = ""
    current_title = ""
    current_idx = 0
    bucket: list[dict[str, Any]] = []
    for row in designer_rows:
        au = row.get("article_url", "")
        at = row.get("article_title", "")
        aidx = int(row.get("_article_index") or 0)
        if au != current_url:
            if bucket:
                rows_by_article.append((current_idx, current_url, current_title, bucket))
            current_url = au
            current_title = at
            current_idx = aidx
            bucket = []
        bucket.append(row)
    if bucket:
        rows_by_article.append((current_idx, current_url, current_title, bucket))

    style = """
    body { font-family: system-ui, sans-serif; margin: 16px; }
    .topbar { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; margin-bottom: 12px; }
    h1 { font-size: 1.2rem; margin: 0; }
    .help-open { padding: 6px 12px; border-radius: 6px; border: 1px solid #1f4e78; background: #fff; color: #1f4e78; font-size: 13px; cursor: pointer; }
    .help-open:hover { background: #e7eef5; }
    .group { margin-bottom: 28px; border: 1px solid #ccc; border-radius: 6px; overflow: hidden; }
    .group-head { background: #1f4e78; color: #fff; padding: 10px 12px; font-weight: 600; }
    .group-head a { color: #cfe8ff; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; table-layout: fixed; }
    th, td { border: 1px solid #ddd; padding: 8px; vertical-align: top; word-wrap: break-word; }
    th { background: #e7eef5; text-align: left; }
    col.col-n { width: 2.6em; }
    col.col-src { width: 7.5em; }
    col.col-thumb { width: 7.2em; }
    col.col-title { width: 11%; }
    col.col-ctx { width: 32%; }
    col.col-path { width: 15%; }
    col.col-url { width: 13%; }
    col.col-stat { width: 6.5em; }
    th.col-n, td.col-n { width: 2.6em; padding: 6px 4px; text-align: center; white-space: nowrap; font-variant-numeric: tabular-nums; }
    td.ctx { white-space: pre-wrap; }
    td.col-title { vertical-align: top; }
    td.path { font-size: 12px; font-family: ui-monospace, monospace; }
    td.path a { word-break: break-all; }
    table tbody td:nth-of-type(7) a { word-break: break-all; }
    img.thumb { max-width: 100px; max-height: 76px; display: block; object-fit: contain; cursor: zoom-in; }
    .img-lightbox { position: fixed; inset: 0; z-index: 10000; display: none; align-items: center; justify-content: center; padding: 20px; box-sizing: border-box; }
    .img-lightbox.open { display: flex; }
    .img-lightbox-bg { position: absolute; inset: 0; background: rgba(0,0,0,0.78); cursor: zoom-out; }
    .img-lightbox-pic { position: relative; z-index: 1; max-width: min(96vw, 1400px); max-height: 92vh; width: auto; height: auto; object-fit: contain; box-shadow: 0 12px 48px rgba(0,0,0,0.5); border-radius: 4px; cursor: zoom-out; }
    tr.row-ok { background: #eef8ef; }
    tr.row-review { background: #fff0f0; }
    tr.row-skip { background: #f3f4f6; }
    tr.row-ok td, tr.row-review td, tr.row-skip td { border-color: #e0e0e0; }
    a { color: #0b57d0; }
    .copy-wrap { display: flex; align-items: flex-start; gap: 8px; justify-content: space-between; }
    .copy-wrap .ctx-text { flex: 1; min-width: 0; }
    .copy-btn { flex: 0 0 auto; padding: 4px; cursor: pointer; border: 1px solid #bbb; border-radius: 4px; background: #fafafa; color: #333; display: inline-flex; align-items: center; justify-content: center; line-height: 0; }
    .copy-btn:hover { background: #eee; }
    .copy-btn .copy-ic { display: block; }
    .help-dlg { max-width: 640px; padding: 0; border: none; border-radius: 10px; box-shadow: 0 8px 32px rgba(0,0,0,0.2); }
    .help-dlg::backdrop { background: rgba(0,0,0,0.35); }
    .help-dlg-inner { padding: 16px 18px; }
    .help-dlg h2 { margin: 0 0 12px 0; font-size: 1.05rem; }
    .help-dlg ul { margin: 0; padding-left: 1.2em; line-height: 1.55; font-size: 13px; }
    .help-dlg .help-sub { font-size: 0.95rem; margin: 14px 0 6px 0; }
    .help-dlg .help-note { font-size: 12px; margin: 10px 0 0 0; line-height: 1.5; }
    .help-dlg .close-row { margin-top: 14px; text-align: right; }
    """

    parts = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Kuvalda — изображения</title>",
        f"<style>{style}</style></head><body>",
        '<div class="topbar"><h1>Обзор изображений по статьям</h1>',
        '<button type="button" class="help-open" id="help-open">Справка</button></div>',
    ]
    for aidx, au, atitle, group_rows in rows_by_article:
        esc_t = html.escape(atitle or "")
        link = html.escape(au or "", quote=True)
        parts.append('<div class="group">')
        parts.append(
            f'<div class="group-head">Статья {aidx}: {esc_t} &nbsp;|&nbsp; '
            f'<a href="{link}" target="_blank" rel="noopener">Открыть на сайте</a></div>'
        )
        parts.append(
            "<table><colgroup>"
            '<col class="col-n"><col class="col-src"><col class="col-thumb"><col class="col-title"><col class="col-ctx">'
            '<col class="col-path"><col class="col-url"><col class="col-stat">'
            "</colgroup><thead><tr>"
        )
        headers = (
            "№",
            "Источник",
            "Превью",
            "Название статьи",
            "Контекст статьи",
            "Файл",
            "Картинка на сайте",
            "Статус",
        )
        h_classes = ("col-n", "col-src", "", "", "ctx", "path", "", "")
        for h, cls in zip(headers, h_classes):
            c = f' class="{cls}"' if cls else ""
            parts.append(f"<th{c}>{html.escape(h)}</th>")
        parts.append("</tr></thead><tbody>")
        for ri, row in enumerate(group_rows):
            iu = row.get("image_url", "") or ""
            if iu:
                img_tag = (
                    f'<img class="thumb" src="{html.escape(iu, quote=True)}" alt="" '
                    'referrerpolicy="no-referrer" loading="lazy" title="Показать крупнее">'
                )
            else:
                img_tag = "—"
            stat = row.get("status_ru", "") or ""
            row_cls = _html_status_row_class(stat)
            lp = row.get("_local_path_obj")
            path_display = row.get("path_for_display") or ""
            path_cell: str
            if isinstance(lp, Path) and lp.exists():
                abs_p = lp.resolve()
                file_uri = html.escape(abs_p.as_uri(), quote=True)
                esc_disp = html.escape(path_display or str(lp))
                path_cell = f'<a href="{file_uri}">{esc_disp}</a>'
            else:
                path_cell = html.escape(path_display or "—")
            src_lbl = html.escape(row.get("image_source_label") or "В статье")
            parts.append(f'<tr class="{row_cls}">')
            parts.append(f'<td class="col-n">{html.escape(str(row.get("order", "")))}</td>')
            parts.append(f'<td class="col-src">{src_lbl}</td>')
            parts.append(f"<td>{img_tag}</td>")
            if ri == 0:
                title_raw = row.get("article_title", "") or ""
                tb = b64_utf8(title_raw)
                parts.append(
                    '<td class="col-title"><div class="copy-wrap"><span>'
                    f"{html.escape(title_raw)}</span>"
                    f'<button type="button" class="copy-btn" data-b64="{html.escape(tb)}" '
                    f'title="Копировать название статьи">{COPY_ICON_SVG}</button></div></td>'
                )
                # В первой строке всегда показываем вводный контекст статьи, даже если
                # первая картинка — превью из ленты блога (раньше здесь ставили «—»).
                ctx = row.get("weavy_context", "") or ""
                cb = b64_utf8(ctx)
                parts.append(
                    '<td class="ctx"><div class="copy-wrap"><span class="ctx-text">'
                    f"{html.escape(ctx)}</span>"
                    f'<button type="button" class="copy-btn" data-b64="{html.escape(cb)}" '
                    f'title="Копировать контекст статьи">{COPY_ICON_SVG}</button></div></td>'
                )
            else:
                parts.append('<td class="col-title"></td>')
                parts.append('<td class="ctx"></td>')
            parts.append(f'<td class="path">{path_cell}</td>')
            if iu:
                esc_url_attr = html.escape(iu, quote=True)
                esc_url_txt = html.escape(iu)
                parts.append(
                    f'<td><a href="{esc_url_attr}" target="_blank" rel="noopener">{esc_url_txt}</a></td>'
                )
            else:
                parts.append("<td>—</td>")
            parts.append(f"<td>{html.escape(stat)}</td>")
            parts.append("</tr>")
        parts.append("</tbody></table></div>")

    help_dialog = (
        '<dialog id="help-dialog" class="help-dlg"><div class="help-dlg-inner">'
        '<h2>Как пользоваться этим файлом</h2>'
        f'<div id="kuvalda-help-status-block">{help_dialog_statuses_block_html()}</div>'
        '<h2 class="help-sub" data-kuvalda-insert="after-status" style="margin-top:14px">Как читать таблицу</h2><ul>'
        "<li><strong>Источник</strong> — «В ленте блога» — превью карточки со страницы "
        "<a href=\"https://www.kuvalda.ru/blog/\" target=\"_blank\" rel=\"noopener\">kuvalda.ru/blog</a> (отдельная строка); "
        "«Превью (страницы)» — og:image со страницы статьи, если он отличается от ленты; "
        "«В статье» — картинка из текста статьи. Вводный контекст статьи в таблице всегда в первой строке блока, "
        "в том числе если первая строка — превью «В ленте блога».</li>"
        "<li><strong>Превью</strong> — миниатюра с сайта; клик по превью открывает то же изображение крупно поверх страницы (закрыть: клик по фону или по картинке, или Escape).</li>"
        "<li><strong>Название и контекст статьи</strong> — заполняются только в первой строке блока статьи "
        "(у остальных картинок те же колонки пустые, чтобы цвет строки совпадал со статусом каждой фото).</li>"
        "<li><strong>Копирование</strong> — иконки у первой строки статьи: у названия и у контекста (как у дизайна прежней версии).</li>"
        "<li><strong>Файл</strong> — если картинка уже скачана: путь и ссылка file:// на локальный файл.</li>"
        "<li><strong>Картинка на сайте</strong> — прямой URL изображения на CDN; по ссылке открывается в браузере (сохранить файл — через меню браузера, если нужно).</li>"
        "<li><strong>Статус и цвет строки</strong> — зелёный: скачано как подходящее фото; розовый: скачано, нужна проверка; "
        "серый: не скачиваем.</li>"
        "</ul><div class=\"close-row\"><button type=\"button\" class=\"help-open\" id=\"help-close\">Закрыть</button></div>"
        "</div></dialog>"
    )
    help_script = (
        "<script>(function(){function b64ToUtf8(b64){var bin=atob(b64);var bytes=new Uint8Array(bin.length);"
        "for(var i=0;i<bin.length;i++)bytes[i]=bin.charCodeAt(i);return new TextDecoder('utf-8').decode(bytes);}"
        "document.addEventListener('click',function(e){var btn=e.target.closest('button.copy-btn[data-b64]');if(!btn)return;"
        "var b64=btn.getAttribute('data-b64');if(!b64)return;var t=b64ToUtf8(b64);var lab=btn.innerHTML;"
        "if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(t).then(function(){"
        "btn.innerHTML='<span style=\\'font-size:12px\\'>✓</span>';setTimeout(function(){btn.innerHTML=lab;},800);}).catch(function(){prompt('Скопируйте вручную (Ctrl/Cmd+C):',t);});}"
        "else{prompt('Скопируйте вручную (Ctrl/Cmd+C):',t);}});var dlg=document.getElementById('help-dialog');"
        "var ob=document.getElementById('help-open');var cb=document.getElementById('help-close');"
        "if(ob&&dlg)ob.addEventListener('click',function(){dlg.showModal();});"
        "if(cb&&dlg)cb.addEventListener('click',function(){dlg.close();});})();</script>"
    )
    parts.append(help_dialog)
    parts.append(
        '<div id="img-lightbox" class="img-lightbox" aria-hidden="true">'
        '<div class="img-lightbox-bg" id="img-lightbox-bg"></div>'
        '<img class="img-lightbox-pic" id="img-lightbox-img" src="" alt="">'
        "</div>"
    )
    parts.append(help_script)
    parts.append(
        "<script>(function(){var lb=document.getElementById('img-lightbox');"
        "var lbi=document.getElementById('img-lightbox-img');"
        "function closeLb(){if(!lb)return;lb.classList.remove('open');lb.setAttribute('aria-hidden','true');if(lbi)lbi.removeAttribute('src');}"
        "function openLb(src){if(!lb||!lbi||!src)return;lbi.setAttribute('src',src);lb.classList.add('open');lb.setAttribute('aria-hidden','false');}"
        "document.addEventListener('click',function(e){var t=e.target;"
        "if(t&&t.tagName==='IMG'&&t.classList.contains('thumb')){e.preventDefault();openLb(t.currentSrc||t.getAttribute('src'));return;}"
        "if(t&&(t.id==='img-lightbox-bg'||t.classList.contains('img-lightbox-bg')||t.classList.contains('img-lightbox-pic'))){closeLb();}});"
        "document.addEventListener('keydown',function(e){if(e.key==='Escape')closeLb();});})();</script>"
    )
    parts.append("</body></html>")
    html_path.write_text("\n".join(parts), encoding="utf-8")
    return html_path


def articles_from_explicit_urls(urls: list[str]) -> list[ArticleMeta]:
    """Статьи по прямым ссылкам (без обхода ленты блога)."""
    out: list[ArticleMeta] = []
    for raw in urls:
        u = (raw or "").strip()
        if not u:
            continue
        full = urljoin(BASE_URL, u)
        parsed = urlparse(full)
        parts = [p for p in parsed.path.split("/") if p]
        article_slug = parts[-1].replace(".html", "") if parts else slugify(full)
        category_slug = parts[2] if len(parts) >= 4 else ""
        out.append(
            ArticleMeta(
                article_url=full,
                article_slug=article_slug,
                category_slug=category_slug,
            )
        )
    return out


def write_csv(rows: list[dict[str, str]], path: Path) -> None:
    if not rows:
        return
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)



def process_site(args: argparse.Namespace) -> None:
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    fetcher = Fetcher(delay=args.delay, timeout=args.timeout)
    overrides = read_overrides(Path(args.overrides) if args.overrides else None)

    merge_paths = [Path(x.strip()) for x in getattr(args, "merge_manifests", []) or [] if (x or "").strip()]
    merged_urls = read_article_urls_from_manifests(merge_paths)
    explicit = [u for u in getattr(args, "articles", []) or [] if (u or "").strip()]
    append_pages = getattr(args, "append_listing_pages", None)
    append_page_nums = list(append_pages) if append_pages else []

    all_explicit: list[str] = []
    seen_u: set[str] = set()
    for u in merged_urls + explicit:
        if u and u not in seen_u:
            seen_u.add(u)
            all_explicit.append(u)
    tail_before_append = len(all_explicit)

    if append_page_nums:
        appended_from_listing: list[str] = []
        for lap_meta in discover_articles_on_listing_pages(fetcher, append_page_nums):
            u = lap_meta.article_url
            if u not in seen_u:
                seen_u.add(u)
                appended_from_listing.append(u)
        if merged_urls or explicit:
            all_explicit.extend(appended_from_listing)
            print(
                f"[1/4] После основного списка добавлено {len(appended_from_listing)} URL со страниц ленты {append_page_nums}",
                file=sys.stderr,
            )
        else:
            all_explicit.extend(appended_from_listing)

    use_incremental_append = (
        bool(append_page_nums)
        and bool(merge_paths)
        and (out_dir / REPORT_SNAPSHOT_FILENAME).exists()
        and tail_before_append > 0
        and not getattr(args, "full_reprocess", False)
    )
    stable_norm = [normalize_manifest_article_url(u) for u in all_explicit[:tail_before_append]]
    new_raw_urls = all_explicit[tail_before_append:] if append_page_nums else []

    preserved_designer: list[dict[str, Any]] = []
    preserved_manifest: list[dict[str, str]] = []

    articles_to_process: list[ArticleMeta] = []

    if use_incremental_append:
        try:
            manifest_lookup = read_article_manifest_lookup_first_win(merge_paths)
            preserved_designer = designer_rows_stable_from_snapshot(out_dir, stable_norm)
            preserved_manifest = manifest_article_rows_stable_order(stable_norm, manifest_lookup)
        except IncrementalPreserveError as exc:
            print(
                f"[incremental-append] Ошибка: {exc}. "
                "Запустите с --full-reprocess для полной пересборки или проверьте CSV и kuvalda_image_report_snapshot.json.",
                file=sys.stderr,
            )
            raise SystemExit(2) from exc
        articles_to_process = articles_from_explicit_urls(new_raw_urls)
        print(
            f"[incremental-append] Режим: старых статей (без сети) — {len(stable_norm)}, новых обработать — {len(articles_to_process)}",
            file=sys.stderr,
        )

    elif all_explicit:
        print("[1/4] Article URLs from merge / --article / --append-listing-pages...", file=sys.stderr)
        articles_to_process = articles_from_explicit_urls(all_explicit)
        print(
            f"Articles to process: {len(articles_to_process)} "
            f"(из CSV/флагов: {tail_before_append}, новые с ленты: {len(all_explicit) - tail_before_append})",
            file=sys.stderr,
        )
    else:
        print("[1/4] Discovering article URLs...", file=sys.stderr)
        articles_to_process = discover_articles(
            fetcher,
            max_pages=args.max_pages,
            max_articles=args.max_articles,
        )
        print(f"Discovered {len(articles_to_process)} article URLs", file=sys.stderr)

    max_page_hint = getattr(args, "max_pages", 200) or 200
    if append_page_nums:
        max_page_hint = max(max_page_hint, max(append_page_nums))
    enrich_pages = min(max(max_page_hint, 1), 50)
    enrich_articles_with_listing_previews(fetcher, articles_to_process, max_listing_pages=enrich_pages)

    dl_root = out_dir / "downloaded_images"
    dl_root.mkdir(parents=True, exist_ok=True)
    if getattr(args, "purge_stale_slug_folders", True) and not use_incremental_append:
        batch_slugs = {a.article_slug for a in articles_to_process if getattr(a, "article_slug", "")}
        n_rm = purge_download_folders_for_slugs(dl_root, batch_slugs)
        if n_rm:
            print(f"[purge] Удалено папок со старыми номерами (дубликаты NNN_slug): {n_rm}", file=sys.stderr)
    elif use_incremental_append:
        print("[incremental-append] Очистка purge и старые папки не трогаются.", file=sys.stderr)

    article_rows: list[dict[str, str]] = list(preserved_manifest) if use_incremental_append else []
    designer_rows: list[dict[str, Any]] = list(preserved_designer) if use_incremental_append else []

    article_index_base = (
        max(int(r["_article_index"]) for r in preserved_designer) + 1
        if (use_incremental_append and preserved_designer)
        else 1
    )

    total_articles_in_run = article_index_base + len(articles_to_process) - 1 if articles_to_process else article_index_base - 1

    for i_rel, meta in enumerate(articles_to_process):
        idx = article_index_base + i_rel
        meta.article_index = idx
        print(
            f"[{idx}/{max(total_articles_in_run, idx)} новая статья {i_rel + 1}/{len(articles_to_process)}] {meta.article_url}",
            file=sys.stderr,
        )
        try:
            meta, images = parse_article(fetcher, meta)
        except Exception as exc:
            article_rows.append({
                "Название статьи": meta.title or meta.article_slug,
                "Ссылка на статью": meta.article_url,
                "Контекст статьи": "",
                "Статус": f"ошибка загрузки: {exc}",
            })
            continue

        article_rows.append({
            "Название статьи": meta.title,
            "Ссылка на статью": meta.article_url,
            "Контекст статьи": meta.intro_context,
            "Статус": "ok",
        })

        # Перед скачиванием картинок статьи полностью очищаем её папку — без смеси старых/новых имён.
        article_img_dir = out_dir / "downloaded_images" / f"{meta.article_index:03d}_{meta.article_slug}"
        if article_img_dir.exists():
            shutil.rmtree(article_img_dir)
        used_lower: set[str] = set()
        bytes_to_path: dict[bytes, Path] = {}

        per_article_order = 0
        for order, img in enumerate(images, start=1):
            img_url = img["url"]

            layout_decision = classify_layout_flow(img)
            image_bytes: Optional[bytes] = None
            ocr_words = 0

            if layout_decision == "skip":
                decision = "skip"
            elif layout_decision in ("include", "review"):
                decision = layout_decision
            else:
                image_bytes = fetcher.get_bytes(img_url)
                ocr_words = 0
                if image_bytes:
                    try:
                        pil_im = Image.open(io.BytesIO(image_bytes))
                        _, ocr_words = ocr_excerpt_from_pil(pil_im)
                    except Exception:
                        ocr_words = 0
                decision, _r, _c, _bh, _mh, _ts = classify_image(
                    meta=meta,
                    img_url=img_url,
                    role=img["role"],
                    alt_text=img.get("alt", ""),
                    context_text=img.get("context", ""),
                    local_path=None,
                    ocr_word_score=ocr_words,
                )

            if img_url in overrides:
                override = overrides[img_url]
                decision = override.get("decision", decision) or decision

            will_download = decision in ("include", "review")
            if img_url in overrides and overrides[img_url].get("decision") == "skip":
                will_download = False

            local_path: Optional[Path] = None
            if will_download:
                if image_bytes is None:
                    image_bytes = fetcher.get_bytes(img_url)
                if image_bytes:
                    fprint = hashlib.sha256(image_bytes).digest()
                    if fprint in bytes_to_path:
                        local_path = bytes_to_path[fprint]
                    else:
                        target_path = build_image_path(
                            out_dir,
                            meta.article_index,
                            meta.article_slug,
                            img_url,
                            used_lower,
                        )
                        target_path.parent.mkdir(parents=True, exist_ok=True)
                        target_path.write_bytes(image_bytes)
                        local_path = target_path
                        bytes_to_path[fprint] = target_path

            if decision == "skip" and local_path and local_path.exists() and args.delete_skipped:
                try:
                    local_path.unlink()
                    local_path = None
                except Exception:
                    pass

            per_article_order += 1
            path_display = kit_relative_display_path(str(local_path)) if local_path else ""
            status_ru = designer_status_ru(decision, local_path)
            role_s = str(img.get("role", ""))
            src_lbl = image_source_label(role_s, str(img.get("context", "")))
            suppress_ctx = "1" if role_s == "listing_blog" else "0"
            designer_rows.append(
                {
                    "order": str(per_article_order),
                    "article_index_str": str(meta.article_index),
                    "article_title": meta.title,
                    "image_source_label": src_lbl,
                    "suppress_context": suppress_ctx,
                    "weavy_context": meta.intro_context,
                    "article_url": meta.article_url,
                    "local_path": str(local_path) if local_path else "",
                    "path_for_display": path_display,
                    "image_url": img_url,
                    "status_ru": status_ru,
                    "_article_url": meta.article_url,
                    "_article_index": meta.article_index,
                    "_local_path_obj": local_path,
                }
            )

    write_csv(article_rows, out_dir / "kuvalda_articles.csv")
    html_path = write_html_report(out_dir, designer_rows)
    write_report_snapshot(out_dir, designer_rows)

    tally_cls = tally_classification_from_designer_rows(designer_rows)

    downloaded_count = sum(
        1
        for r in designer_rows
        if r.get("local_path") and str(r["local_path"]).strip() and Path(str(r["local_path"])).exists()
    )
    downloaded_weavy = sum(
        1
        for r in designer_rows
        if r.get("status_ru") in ("Скачано", "Скачано — проверить")
    )
    summary = {
        "articles": len(article_rows),
        "images_total": len(designer_rows),
        "include": tally_cls["include"],
        "review": tally_cls["review"],
        "skip": tally_cls["skip"],
        "downloaded_files": downloaded_count,
        "downloaded_include_review": downloaded_weavy,
        "articles_csv": str(out_dir / "kuvalda_articles.csv"),
        "html_report": str(html_path),
        "downloaded_dir": str(out_dir / "downloaded_images"),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    pilot_lines = [
        "# Kuvalda blog image audit — pilot run",
        "",
        f"- Output directory: `{out_dir}`",
        f"- Articles in manifest: {summary['articles']}",
        f"- Images (rows): {summary['images_total']}",
        f"- Downloaded files on disk (all decisions): {downloaded_count}",
        f"- Downloaded for Weavy (include + review): {downloaded_weavy}",
        f"- include / review / skip: {summary['include']} / {summary['review']} / {summary['skip']}",
        f"- HTML report: `{summary.get('html_report', '')}`",
        "",
        "## Article URLs processed",
        "",
    ]
    for row in article_rows:
        if row.get("Статус") == "ok" or row.get("Ссылка на статью") or row.get("article_url"):
            title = row.get("Название статьи") or row.get("article_title", "")
            link = row.get("Ссылка на статью") or row.get("article_url", "")
            pilot_lines.append(f"- {title} — {link}")
    (out_dir / "pilot_report.md").write_text("\n".join(pilot_lines), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))



def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape Kuvalda blog images into review buckets.")
    parser.add_argument(
        "--out-dir",
        default="./kuvalda_master",
        help="Каталог экспорта (по умолчанию ./kuvalda_master)",
    )
    parser.add_argument("--max-pages", type=int, default=200, help="How many /blog/page-N/ pages to try")
    parser.add_argument(
        "--max-articles",
        type=int,
        default=None,
        help="Pilot/stop early: collect at most this many articles from listing pages, then stop discovery",
    )
    parser.add_argument("--delay", type=float, default=1.0, help="Delay between HTTP requests")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout in seconds")
    parser.add_argument(
        "--overrides",
        default="",
        help="Optional CSV with columns image_url,decision,decision_reason,confidence",
    )
    parser.add_argument(
        "--delete-skipped",
        action="store_true",
        help="Delete files that were downloaded for classification but ended as skip",
    )
    parser.add_argument(
        "--article",
        action="append",
        default=[],
        dest="articles",
        metavar="URL",
        help="Явный URL статьи (флаг можно повторить). Если задан хотя бы один — лента блога не обходится, только эти статьи.",
    )
    parser.add_argument(
        "--merge-manifests",
        nargs="*",
        default=[],
        dest="merge_manifests",
        metavar="CSV",
        help="CSV со статьями (kuvalda_articles.csv): объединить URL с --article, без дублей; затем только эти статьи.",
    )
    parser.add_argument(
        "--append-listing-pages",
        nargs="+",
        type=int,
        dest="append_listing_pages",
        metavar="N",
        help="Забрать новые URL с /blog/page-N/ (N≥2) или главной ленты при N=1; дописать в конец после --merge-manifests / --article без дублей по URL.",
    )
    parser.add_argument(
        "--full-reprocess",
        action="store_true",
        dest="full_reprocess",
        help="Полная пересборка: игнорировать инкремент при --merge-manifests + --append-listing-pages, очистить дубликаты NNN_slug (purge) и заново обработать весь объединённый список URL.",
    )
    parser.add_argument(
        "--no-purge-stale-slug-folders",
        action="store_true",
        help="Не удалять все папки NNN_slug перед прогоном (оставить старые номера других прогонов).",
    )
    parser.add_argument(
        "--upgrade-html-report",
        action="store_true",
        help="Только обновить справку в kuvalda_image_report.html и убрать устаревший UI (без сети, папки не трогаются).",
    )
    parser.add_argument(
        "--report-html-only",
        action="store_true",
        help=f"Только пересобрать HTML из `{REPORT_SNAPSHOT_FILENAME}` после полного прогона (без скачивания статей).",
    )
    args = parser.parse_args()
    out_dir_resolved = Path(args.out_dir).resolve()
    if args.report_html_only and args.upgrade_html_report:
        parser.error("Нельзя одновременно --report-html-only и --upgrade-html-report")
    if args.report_html_only:
        path = report_html_only(out_dir_resolved)
        print(f"Отчёт пересобран: {path}", file=sys.stderr)
        return
    if args.upgrade_html_report:
        html_p = out_dir_resolved / "kuvalda_image_report.html"
        upgrade_existing_image_report_html(html_p)
        print(f"HTML обновлён (patch): {html_p}", file=sys.stderr)
        return
    args.purge_stale_slug_folders = not getattr(args, "no_purge_stale_slug_folders", False)
    process_site(args)


if __name__ == "__main__":
    main()
