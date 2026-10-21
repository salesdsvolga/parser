#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
htreviews.org — парсер (brand, line, flavor) через API /postData.
"""

import argparse
import json
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

BASE = "https://htreviews.org"
STATE_FILE = Path("state.json")
JSONL_FILE = Path("data.jsonl")
OUTPUT_FILE = Path("htreviews_full.json")
RESERVED = {"brands", "lines", "new", "now", "hall-of-fame", "reviews", "compilation"}
CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://htreviews.org/",
}

_session = requests.Session()
_session.headers.update(HEADERS)
_stop_requested = False
MAX_BRANDS = 0


def _handle_signal(signum, frame):
    global _stop_requested
    if _stop_requested:
        print("\n⛔ Force exit.")
        sys.exit(130)
    _stop_requested = True
    print("\n⏸ Graceful stop — saving…")


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ---------- helpers ----------

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fetch(url, retries=3, backoff=1.5, timeout=30, referer=None):
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    last = None
    for attempt in range(1, retries + 1):
        try:
            r = _session.get(url, timeout=timeout, headers=headers)
            if r.status_code == 200:
                return r.text
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r.text
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(backoff ** attempt)
    raise RuntimeError(f"fetch failed {url}: {last}")


def soup_of(url, referer=None):
    return BeautifulSoup(fetch(url, referer=referer), "lxml")


def text_of(node):
    if not node:
        return None
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip() or None


def normalize(url):
    return urljoin(BASE + "/", url).split("#")[0].split("?")[0].rstrip("/")


def path_parts(url):
    return urlparse(normalize(url)).path.strip("/").split("/")


def _span_texts(link_node, skip_classes=("country", "date_info", "extra")):
    spans = link_node.find_all("span", recursive=False)
    texts = []
    for sp in spans:
        classes = set(sp.get("class") or [])
        if classes & set(skip_classes):
            continue
        t = text_of(sp)
        if t:
            texts.append(t)
    return texts


def _pick_name(texts, fallback):
    return texts[0] if texts else fallback


# ---------- persistence ----------

def atomic_write_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_state():
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
            for k in ("done_brands", "done_lines", "done_lists", "done_letters"):
                s.setdefault(k, [])
            return s
        except Exception:
            pass
    return {
        "done_brands": [], "done_lines": [], "done_lists": [], "done_letters": [],
        "started_at": now_iso(),
    }


def save_state(state):
    state["updated_at"] = now_iso()
    atomic_write_json(STATE_FILE, state)


def load_existing_urls():
    urls = set()
    if JSONL_FILE.exists():
        with open(JSONL_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if obj.get("flavor_url"):
                        urls.add(obj["flavor_url"])
                except Exception:
                    pass
    return urls


def append_item(item, existing_urls):
    fu = item.get("flavor_url")
    if not fu or fu in existing_urls:
        return False
    with open(JSONL_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")
        f.flush()
    existing_urls.add(fu)
    return True


def consolidate():
    items, seen = [], set()
    if JSONL_FILE.exists():
        with open(JSONL_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                fu = obj.get("flavor_url")
                if not fu or fu in seen:
                    continue
                seen.add(fu)
                items.append(obj)
    items.sort(key=lambda x: (
        (x.get("brand") or "").lower(),
        (x.get("line") or "").lower(),
        (x.get("flavor") or "").lower(),
    ))
    atomic_write_json(OUTPUT_FILE, items)
    return items


# ---------- API ----------

def _post_api(action, data, referer=None):
    """POST-запрос к /postData. Возвращает JSON или None."""
    try:
        r = _session.post(
            BASE + "/postData",
            json={"action": action, "data": data},
            headers={
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": referer or BASE + "/",
            },
            timeout=25,
        )
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def api_to_record(it):
    slug = (it.get("slug") or "").strip("/")
    parts = slug.split("/")
    if len(parts) != 3:
        return None
    brand_slug, line_slug, flavor_slug = parts
    tags = []
    for t in (it.get("tags") or []):
        if isinstance(t, dict):
            name = t.get("name")
            if name and name not in tags:
                tags.append(name)
    return {
        "brand": it.get("brand") or brand_slug,
        "brand_url": f"{BASE}/tobaccos/{brand_slug}",
        "line": it.get("line") or line_slug,
        "line_url": f"{BASE}/tobaccos/{brand_slug}/{line_slug}",
        "flavor": it.get("name") or flavor_slug,
        "flavor_url": f"{BASE}/tobaccos/{slug}",
        "flavor_alt": it.get("alt_name"),
        "rating": it.get("rating"),
        "reviews_count": int(it["reviews"]) if str(it.get("reviews", "")).isdigit() else None,
        "strength": it.get("strength"),
        "status": it.get("status"),
        "tags": tags,
    }


def _fetch_all_flavors_via_api(line_id, referer):
    """Загружает ВСЕ вкусы линейки через API, по 100 штук."""
    items = []
    offset = 0
    limit = 100
    while not _stop_requested:
        data = {
            "id": line_id,
            "limit": limit,
            "offset": offset,
            "sort": {"s": "rating", "d": "desc"}
        }
        chunk = _post_api("objectByLine", data, referer=referer)
        if not isinstance(chunk, list):
            break
        items.extend(chunk)
        if len(chunk) < limit:
            break
        offset += limit
        time.sleep(0.15)
    return items


# ---------- HTML fallback (когда API не сработал) ----------

def _extract_items_full(soup):
    results = []
    seen_local = set()

    def add(brand, brand_url, line, line_url, flavor, flavor_url, flavor_alt):
        if not flavor_url or flavor_url in seen_local:
            return
        parts = path_parts(flavor_url)
        if len(parts) == 4 and parts[0] == "tobaccos":
            if not brand_url:
                brand_url = normalize(f"{BASE}/tobaccos/{parts[1]}")
            if not brand:
                brand = parts[1]
            if not line_url:
                line_url = normalize(f"{BASE}/tobaccos/{parts[1]}/{parts[2]}")
            if not line:
                line = parts[2]
            if not flavor:
                flavor = parts[3]
        else:
            return
        seen_local.add(flavor_url)
        results.append({
            "brand": brand, "brand_url": brand_url,
            "line": line, "line_url": line_url,
            "flavor": flavor, "flavor_url": flavor_url,
            "flavor_alt": flavor_alt,
        })

    for a in soup.find_all("a", href=True):
        parts = path_parts(a["href"])
        if len(parts) == 4 and parts[0] == "tobaccos":
            texts = _span_texts(a)
            add(None, None, None, None,
                _pick_name(texts, parts[3]),
                normalize(a["href"]),
                texts[1] if len(texts) > 1 else None)
    return results


def _process_html_page(url, existing_urls):
    try:
        soup = soup_of(url)
    except Exception:
        return 0
    items = _extract_items_full(soup)
    added = 0
    for it in items:
        if append_item(it, existing_urls):
            added += 1
    return added


# ---------- brand discovery ----------

def _add_brands_from_soup(soup, brands):
    added = 0
    for a in soup.find_all("a", href=True):
        parts = path_parts(a["href"])
        if not (len(parts) == 2 and parts[0] == "tobaccos"
                and parts[1] and parts[1] not in RESERVED):
            continue
        full = normalize(a["href"])
        name = _pick_name(_span_texts(a), parts[1])
        if full not in brands:
            brands[full] = {"name": name or parts[1], "slug": parts[1], "url": full}
            added += 1
    return added


def _discover_via_tags(brands):
    print("    fetching tag list…")
    try:
        soup = soup_of(BASE + "/tobaccos")
    except Exception:
        return
    tag_ids = set()
    for inp in soup.find_all("input", attrs={"data-type": "tags"}):
        v = inp.get("value")
        if v:
            tag_ids.add(v)
    for tag in tqdm(sorted(tag_ids), desc="Tags", leave=False):
        if _stop_requested:
            return
        try:
            s = soup_of(f"{BASE}/tobaccos?t={tag}")
            _add_brands_from_soup(s, brands)
        except Exception:
            pass
        time.sleep(0.08)


def discover_brands():
    brands = {}
    print("  [1/3] base pages…")
    for path in ["/tobaccos/brands", "/tobaccos/brands?type=others"]:
        try:
            soup = soup_of(BASE + path)
            _add_brands_from_soup(soup, brands)
        except Exception:
            pass

    print("  [2/3] tag sweep…")
    _discover_via_tags(brands)

    print("  [3/3] other pages…")
    for path in ["/tobaccos", "/tobaccos/new", "/hall-of-fame", "/"]:
        try:
            soup = soup_of(BASE + path)
            _add_brands_from_soup(soup, brands)
        except Exception:
            pass

    print(f"▶ brands found: {len(brands)}")
    return list(brands.values())


# ---------- brand / line / flavor extraction ----------

def _find_line_id_in_page(soup, line_url):
    """Ищет ID линейки в HTML страницы (data-id)."""
    # Ищем контейнер с данными
    for sel in (".tobacco_list_items[data-id]", ".tobacco_list_wrapper[data-id]"):
        el = soup.select_one(sel)
        if el and el.get("data-id") and str(el["data-id"]).isdigit():
            return str(el["data-id"])
    # Альтернатива: ищем скрипты с настройками
    for script in soup.find_all("script"):
        if script.string and "line_id" in script.string:
            m = re.search(r'line_id["\']?\s*:\s*["\']?(\d+)', script.string)
            if m:
                return m.group(1)
    return None


def _sweep_brands_and_lines(brand_list, state, existing_urls):
    done_brands = set(state["done_brands"])
    done_lines = set(state["done_lines"])
    total_new = 0
    api_works = False

    for brand in tqdm(brand_list, desc="Brands"):
        if _stop_requested:
            return total_new
        if MAX_BRANDS and len(done_brands) >= MAX_BRANDS:
            print(f"\n  ⏸ max-brands={MAX_BRANDS} reached.")
            break

        try:
            soup = soup_of(brand["url"])
        except Exception:
            continue

        # Собираем линейки
        line_urls = set()
        for a in soup.find_all("a", href=True):
            pp = path_parts(a["href"])
            if len(pp) == 3 and pp[0] == "tobaccos" and pp[1] == brand["slug"]:
                line_urls.add(normalize(a["href"]))

        if not line_urls:
            done_brands.add(brand["url"])
            state["done_brands"] = sorted(done_brands)
            save_state(state)
            continue

        for line_url in line_urls:
            if _stop_requested:
                return total_new
            if line_url in done_lines:
                continue

            pp = path_parts(line_url)
            line_slug = pp[2]
            
            try:
                line_soup = soup_of(line_url)
            except Exception:
                continue

            line_id = _find_line_id_in_page(line_soup, line_url)
            got = 0

            # Стратегия 1: API
            if line_id:
                items = _fetch_all_flavors_via_api(line_id, referer=line_url)
                if items:
                    api_works = True
                    for it in items:
                        rec = api_to_record(it)
                        if rec and append_item(rec, existing_urls):
                            got += 1
                    
            # Стратегия 2: HTML fallback
            if got == 0:
                html_items = _extract_items_full(line_soup)
                for it in html_items:
                    if append_item(it, existing_urls):
                        got += 1

            total_new += got
            done_lines.add(line_url)
            state["done_lines"] = sorted(done_lines)
            save_state(state)
            time.sleep(0.15)

        done_brands.add(brand["url"])
        state["done_brands"] = sorted(done_brands)
        save_state(state)

    return total_new


# ---------- main ----------

def main():
    global MAX_BRANDS

    ap = argparse.ArgumentParser()
    ap.add_argument("--delay", type=float, default=0.15)
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--consolidate-only", action="store_true")
    ap.add_argument("--max-brands", type=int, default=0)
    args = ap.parse_args()

    MAX_BRANDS = args.max_brands

    if args.consolidate_only:
        items = consolidate()
        print(f"Consolidated {len(items)} items -> {OUTPUT_FILE}")
        return

    if args.reset:
        for p in (STATE_FILE, JSONL_FILE, OUTPUT_FILE):
            if p.exists():
                p.unlink()

    state = load_state()
    existing_urls = load_existing_urls()
    print(f"▶ Resume: {len(state['done_brands'])} brands, {len(existing_urls)} flavors already")

    print("▶ Discovering brands…")
    brand_list = discover_brands()

    print("▶ Sweeping brands and lines…")
    n = _sweep_brands_and_lines(brand_list, state, existing_urls)
    print(f"  +{n} flavors, total={len(existing_urls)}")

    items = consolidate()
    print(f"\n✅ Done. total={len(items)} flavors")
    print(f"   output: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
