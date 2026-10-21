#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
htreviews.org — максимальный парсер (brand, line, flavor).

Стратегии сбора вкусов:
  1. Прямой парсинг .new_flavors_item / .tobacco_list_item (brand+line+flavor в HTML).
  2. Перебор всех фильтров /tobaccos (теги × страны × крепости × сортировки).
  3. Страницы брендов и линеек + AJAX «Показать ещё».
  4. Fallback: поиск по буквам (если AJAX не найден и вкусов мало).
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
MAX_BRANDS = 0  # [+] лимит брендов за прогон (0 = без лимита)


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
    """Все непустые тексты вложенных <span> (служебные пропускаем)."""
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
    """Название — первый span. Ничего не переводим, берём как на банке."""
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
            s.setdefault("ajax_found", False)
            s.setdefault("ajax_endpoint", None)
            return s
        except Exception:
            pass
    return {
        "done_brands": [], "done_lines": [], "done_lists": [], "done_letters": [],
        "ajax_found": False, "ajax_endpoint": None,
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


# ---------- extraction из HTML-блоков ----------

def _extract_items_full(soup):
    """
    Извлекает из HTML-страницы все вкусы.
    Возвращает список dict: brand, brand_url, line, line_url, flavor, flavor_url, flavor_alt.
    """
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

    # 1) .new_flavors_item
    for item in soup.select(".new_flavors_item"):
        brand = brand_url = line = line_url = flavor = flavor_url = flavor_alt = None
        for a in item.find_all("a", href=True):
            classes = set(a.get("class") or [])
            href = normalize(a["href"])
            parts = path_parts(href)
            if len(parts) < 2 or parts[0] != "tobaccos":
                continue
            texts = _span_texts(a)
            if "info_name" in classes and len(parts) == 4:
                flavor_url = href
                flavor = _pick_name(texts, parts[3])
                flavor_alt = texts[1] if len(texts) > 1 else None
            elif "info_brand" in classes and len(parts) == 2:
                brand_url = href
                brand = _pick_name(texts, parts[1])
            elif "info_line" in classes and len(parts) == 3:
                line_url = href
                line = _pick_name(texts, parts[2])
        add(brand, brand_url, line, line_url, flavor, flavor_url, flavor_alt)

    # 2) .tobacco_list_item
    for item in soup.select(".tobacco_list_item"):
        brand = brand_url = line = line_url = flavor = flavor_url = flavor_alt = None
        for a in item.find_all("a", href=True):
            classes = set(a.get("class") or [])
            href = normalize(a["href"])
            parts = path_parts(href)
            if len(parts) < 2 or parts[0] != "tobaccos":
                continue
            texts = _span_texts(a)
            if "tobacco_list_item_slug" in classes and len(parts) == 4:
                flavor_url = href
                flavor = _pick_name(texts, parts[3])
                flavor_alt = texts[1] if len(texts) > 1 else None
            elif "tobacco_list_item_brand_slug" in classes and len(parts) == 2:
                brand_url = href
                brand = _pick_name(texts, parts[1])
            elif "tobacco_list_item_line_slug" in classes and len(parts) == 3:
                line_url = href
                line = _pick_name(texts, parts[2])
        add(brand, brand_url, line, line_url, flavor, flavor_url, flavor_alt)

    # 3) .last_reviews_item
    for item in soup.select(".last_reviews_item"):
        for a in item.find_all("a", href=True):
            parts = path_parts(a["href"])
            if len(parts) == 4 and parts[0] == "tobaccos":
                texts = _span_texts(a)
                add(None, None, None, None,
                    _pick_name(texts, parts[3]),
                    normalize(a["href"]),
                    texts[1] if len(texts) > 1 else None)
                break

    # 4) Fallback: все ссылки /tobaccos/b/l/f на странице
    for a in soup.find_all("a", href=True):
        parts = path_parts(a["href"])
        if len(parts) == 4 and parts[0] == "tobaccos":
            texts = _span_texts(a)
            add(None, None, None, None,
                _pick_name(texts, parts[3]),
                normalize(a["href"]),
                texts[1] if len(texts) > 1 else None)

    return results


# ---------- сбор брендов ----------

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
        else:
            old = brands[full]["name"]
            if name and CYRILLIC_RE.search(name) and not CYRILLIC_RE.search(old or ""):
                brands[full]["name"] = name
    return added


def _extract_brands_from_items(soup, brands):
    added = 0
    for item in soup.select(".tobacco_list_item, .new_flavors_item, .last_reviews_item"):
        for a in item.find_all("a", href=True):
            parts = path_parts(a["href"])
            if not (len(parts) == 2 and parts[0] == "tobaccos"
                    and parts[1] and parts[1] not in RESERVED):
                continue
            full = normalize(a["href"])
            name = _pick_name(_span_texts(a), parts[1])
            if full not in brands:
                brands[full] = {"name": name or parts[1], "slug": parts[1], "url": full}
                added += 1
            else:
                old = brands[full]["name"]
                if name and CYRILLIC_RE.search(name) and not CYRILLIC_RE.search(old or ""):
                    brands[full]["name"] = name
    return added


def _try_ajax_endpoints(brands):
    candidates = [
        "/htmx/load/brands?offset=20",
        "/htmx/load/brands?type=others&offset=20",
        "/htmx/load/tops_shortlist?entity=brands",
        "/htmx/load/trends?object=brands",
        "/htmx/load/tobaccos?type=brands&offset=20",
        "/htmx/load/tobaccos/brands?offset=20",
        "/htmx/load/more?entity=brands&offset=20",
    ]
    for path in candidates:
        if _stop_requested:
            return False
        try:
            text = fetch(BASE + path, retries=1, timeout=10)
            soup = BeautifulSoup(text, "lxml")
            added = _add_brands_from_soup(soup, brands)
            if added > 3:
                print(f"    ajax {path}: +{added} (total {len(brands)})")
                return True
        except Exception:
            pass
    return False


def _sweep_filters(brands):
    countries = ["", "13", "20", "29", "37", "41", "51", "64", "68", "69",
                 "70", "71", "80", "98", "100", "137", "142", "145", "151",
                 "188", "217", "218", "219"]
    for c in countries:
        if _stop_requested:
            return
        for s in ["desc", "asc"]:
            for t in ["position", "others"]:
                params = [f"s={s}", f"type={t}"]
                if c:
                    params.insert(0, f"c={c}")
                try:
                    soup = soup_of(f"{BASE}/tobaccos/brands?" + "&".join(params))
                    _add_brands_from_soup(soup, brands)
                except Exception:
                    pass
                time.sleep(0.08)
        print(f"    filters c={c or 'all'}: total {len(brands)}")


def _discover_via_tags(brands):
    print("    fetching tag list…")
    try:
        soup = soup_of(BASE + "/tobaccos")
    except Exception as e:
        print(f"    [!] {e}")
        return
    tag_ids = set()
    for inp in soup.find_all("input", attrs={"data-type": "tags"}):
        v = inp.get("value")
        if v:
            tag_ids.add(v)
    print(f"    found {len(tag_ids)} tags")
    for tag in tqdm(sorted(tag_ids), desc="Tags", leave=False):
        if _stop_requested:
            return
        try:
            s = soup_of(f"{BASE}/tobaccos?t={tag}")
            _extract_brands_from_items(s, brands)
        except Exception:
            pass
        time.sleep(0.08)
    print(f"    tags done, brands total: {len(brands)}")


def _sweep_other_pages(brands):
    for path in ["/tobaccos", "/tobaccos/new", "/tobaccos/lines",
                 "/hall-of-fame", "/reviews", "/compilation", "/"]:
        if _stop_requested:
            return
        try:
            soup = soup_of(BASE + path)
            added = _extract_brands_from_items(soup, brands)
            print(f"    {path}: +{added} (total {len(brands)})")
        except Exception as e:
            print(f"    {path}: {e}")


def discover_brands():
    brands = {}
    print("  [1/5] base pages…")
    for path in ["/tobaccos/brands", "/tobaccos/brands?type=others"]:
        try:
            soup = soup_of(BASE + path)
            added = _add_brands_from_soup(soup, brands)
            print(f"    {path}: +{added} (total {len(brands)})")
        except Exception as e:
            print(f"    {path}: {e}")

    print("  [2/5] AJAX endpoints…")
    _try_ajax_endpoints(brands)

    print("  [3/5] filter sweep…")
    _sweep_filters(brands)

    if len(brands) < 250:
        print("  [4/5] tag sweep…")
        _discover_via_tags(brands)

    if len(brands) < 250:
        print("  [5/5] other pages…")
        _sweep_other_pages(brands)

    print(f"▶ brands found: {len(brands)}")
    return list(brands.values())


# ---------- AJAX для вкусов ----------

def _probe_ajax_for_flavors(probe_url):
    try:
        soup = soup_of(probe_url)
    except Exception:
        return None

    container = soup.select_one(".tobacco_list_items")
    if not container:
        return None

    try:
        offset0 = int(container.get("data-offset") or 20)
        total = int(container.get("data-count") or 0)
    except Exception:
        return None

    if total <= offset0:
        return None

    parts = path_parts(probe_url)
    if len(parts) < 3:
        return None
    brand_slug, line_slug = parts[1], parts[2]

    known = set()
    for a in soup.find_all("a", href=True):
        pp = path_parts(a["href"])
        if (len(pp) == 4 and pp[0] == "tobaccos"
                and pp[1] == brand_slug and pp[2] == line_slug):
            known.add(pp[3])

    endpoints = [
        "/htmx/load/tobacco_list",
        "/htmx/load/list",
        "/htmx/load/items",
        "/htmx/load/tobaccos",
        "/htmx/load/more",
    ]
    base_variants = [
        {"brand": brand_slug, "line": line_slug},
        {"category": f"{brand_slug}/{line_slug}"},
        {"path": f"{brand_slug}/{line_slug}"},
    ]
    pager = [
        {"offset": offset0, "count": 100},
        {"offset": offset0, "count": 20},
        {"start": offset0, "length": 100},
    ]

    for ep in endpoints:
        for bv in base_variants:
            for pg in pager:
                if _stop_requested:
                    return None
                params = {**bv, **pg, "type": "tobaccos"}
                url = BASE + ep + "?" + "&".join(f"{k}={v}" for k, v in params.items())
                try:
                    text = fetch(url, timeout=12, referer=probe_url, retries=1)
                except Exception:
                    continue
                if not text or len(text) < 20:
                    continue
                try:
                    s = BeautifulSoup(text, "lxml")
                    new = 0
                    for a in s.find_all("a", href=True):
                        pp = path_parts(a["href"])
                        if (len(pp) == 4 and pp[0] == "tobaccos"
                                and pp[1] == brand_slug and pp[2] == line_slug
                                and pp[3] not in known):
                            new += 1
                    if new > 0:
                        print(f"    ✅ AJAX найден: {ep} (params={list(params.keys())}, +{new})")
                        return (ep, list(bv.keys()) + list(pg.keys()), "html")
                except Exception:
                    pass
    return None


def _load_more_ajax(probe_url, ajax_ep, ajax_keys, existing_urls, target_container):
    parts = path_parts(probe_url)
    if len(parts) < 3:
        return 0
    brand_slug, line_slug = parts[1], parts[2]

    try:
        offset = int(target_container.get("data-offset") or 20)
        total = int(target_container.get("data-count") or 0)
    except Exception:
        return 0
    if total <= offset:
        return 0

    added = 0
    step = 100
    while offset < total and not _stop_requested:
        if "brand" in ajax_keys or "line" in ajax_keys:
            p = {"brand": brand_slug, "line": line_slug}
        elif "category" in ajax_keys:
            p = {"category": f"{brand_slug}/{line_slug}"}
        elif "path" in ajax_keys:
            p = {"path": f"{brand_slug}/{line_slug}"}
        else:
            p = {}
        if "offset" in ajax_keys: p["offset"] = offset
        if "count" in ajax_keys: p["count"] = step
        if "start" in ajax_keys: p["start"] = offset
        if "length" in ajax_keys: p["length"] = step
        p["type"] = "tobaccos"
        url = BASE + ajax_ep + "?" + "&".join(f"{k}={v}" for k, v in p.items())
        try:
            text = fetch(url, timeout=15, referer=probe_url, retries=1)
            soup = BeautifulSoup(text, "lxml")
            items = _extract_items_full(soup)
            if not items:
                break
            got = 0
            for it in items:
                if append_item(it, existing_urls):
                    got += 1
            added += got
            if got == 0:
                break
            offset += step
        except Exception:
            break
        time.sleep(0.2)
    return added


# ---------- проход по страницам ----------

def _process_page(url, existing_urls, ajax_info=None):
    try:
        soup = soup_of(url)
    except Exception:
        return 0, False
    items = _extract_items_full(soup)
    added = 0
    for it in items:
        if append_item(it, existing_urls):
            added += 1

    ajax_used = False
    if ajax_info:
        container = soup.select_one(".tobacco_list_items")
        if container:
            ep, keys, _ = ajax_info
            extra = _load_more_ajax(url, ep, keys, existing_urls, container)
            if extra:
                added += extra
                ajax_used = True
    return added, ajax_used


def _sweep_flavor_lists(state, existing_urls):
    done_lists = set(state["done_lists"])
    total_new = 0

    try:
        soup = soup_of(BASE + "/tobaccos")
        tag_ids = sorted({
            inp.get("value") for inp in soup.find_all("input", attrs={"data-type": "tags"})
            if inp.get("value")
        })
    except Exception:
        tag_ids = []
    print(f"    tag list: {len(tag_ids)} tags")

    urls = []
    for s in ["desc", "asc"]:
        for ty in ["position", "discussed", "flavor"]:
            urls.append(f"{BASE}/tobaccos?s={s}&type={ty}")
    for c in ["13","20","29","37","41","51","64","68","69","70","71","80",
              "98","100","137","142","145","151","188","217","218","219"]:
        urls.append(f"{BASE}/tobaccos?c={c}")
    for k in range(1, 6):
        urls.append(f"{BASE}/tobaccos?k={k}")
    for t in tag_ids:
        urls.append(f"{BASE}/tobaccos?t={t}")
    for p in ["/tobaccos", "/tobaccos/new", "/hall-of-fame",
              "/compilation", "/reviews", "/"]:
        urls.append(BASE + p)

    print(f"    total list pages: {len(urls)}")
    ajax_info = None
    if state["ajax_found"] and state["ajax_endpoint"]:
        ajax_info = (state["ajax_endpoint"],
                     state.get("ajax_keys", ["brand","line","offset","count"]),
                     "html")

    for i, u in enumerate(tqdm(urls, desc="  lists", leave=False)):
        if _stop_requested:
            return total_new
        if u in done_lists:
            continue
        added, _ = _process_page(u, existing_urls, ajax_info=ajax_info)
        total_new += added
        done_lists.add(u)
        state["done_lists"] = sorted(done_lists)
        save_state(state)

    return total_new


def _sweep_brands_and_lines(brand_list, state, existing_urls):
    done_brands = set(state["done_brands"])
    done_lines = set(state["done_lines"])
    total_new = 0

    ajax_info = None
    if state["ajax_found"] and state["ajax_endpoint"]:
        ajax_info = (state["ajax_endpoint"],
                     state.get("ajax_keys", ["brand","line","offset","count"]),
                     "html")

    for brand in tqdm(brand_list, desc="Brands"):
        if _stop_requested:
            return total_new
        # [+] учёт --max-brands
        if MAX_BRANDS and len(done_brands) >= MAX_BRANDS:
            print(f"\n  ⏸ max-brands={MAX_BRANDS} reached, stopping.")
            break

        if brand["url"] not in done_brands:
            added, used = _process_page(brand["url"], existing_urls, ajax_info=ajax_info)
            total_new += added
            done_brands.add(brand["url"])
            state["done_brands"] = sorted(done_brands)
            save_state(state)
            time.sleep(0.15)

        try:
            soup = soup_of(brand["url"])
        except Exception:
            continue
        lines = set()
        for a in soup.find_all("a", href=True):
            pp = path_parts(a["href"])
            if len(pp) == 3 and pp[0] == "tobaccos" and pp[1] == brand["slug"]:
                lines.add(normalize(a["href"]))

        for line_url in lines:
            if _stop_requested:
                return total_new
            if line_url in done_lines:
                continue
            added, used = _process_page(line_url, existing_urls, ajax_info=ajax_info)
            total_new += added
            done_lines.add(line_url)
            state["done_lines"] = sorted(done_lines)
            save_state(state)
            time.sleep(0.15)

    return total_new


def _search_by_letters(state, existing_urls):
    letters = list("абвгдежзиклмнопрстуфхцчшщэюя")
    letters += list("abcdefghijklmnopqrstuvwxyz")
    letters += list("0123456789")
    done = set(state["done_letters"])
    total_new = 0
    for ch in tqdm(letters, desc="Search"):
        if _stop_requested:
            return total_new
        if ch in done:
            continue
        url = f"{BASE}/search/?text={ch}"
        try:
            soup = soup_of(url)
            items = _extract_items_full(soup)
            for it in items:
                if append_item(it, existing_urls):
                    total_new += 1
        except Exception:
            pass
        done.add(ch)
        state["done_letters"] = sorted(done)
        save_state(state)
        time.sleep(0.2)
    return total_new


# ---------- main ----------

def main():
    global MAX_BRANDS  # [+]

    ap = argparse.ArgumentParser()
    ap.add_argument("--delay", type=float, default=0.2)
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--consolidate-only", action="store_true")
    ap.add_argument("--force-search", action="store_true",
                    help="Принудительно прогнать fallback-поиск по буквам")
    # [+] поддержка --max-brands для совместимости с workflow
    ap.add_argument("--max-brands", type=int, default=0,
                    help="Ограничить число брендов за прогон (0 = все). Для отладки.")
    args = ap.parse_args()

    MAX_BRANDS = args.max_brands  # [+]

    if args.consolidate_only:
        items = consolidate()
        print(f"Consolidated {len(items)} items -> {OUTPUT_FILE}")
        return

    if args.reset:
        for p in (STATE_FILE, JSONL_FILE, OUTPUT_FILE):
            if p.exists():
                p.unlink()
        print("Reset done.")

    state = load_state()
    existing_urls = load_existing_urls()
    print(f"▶ Resume: {len(state['done_brands'])} brands, "
          f"{len(state['done_lists'])} lists, "
          f"{len(existing_urls)} flavors already")
    if MAX_BRANDS:
        print(f"▶ Limit: --max-brands={MAX_BRANDS}")

    print("▶ Discovering brands…")
    brand_list = discover_brands()
    print(f"  brands found: {len(brand_list)}")

    if not state["ajax_found"]:
        print("▶ Probing AJAX endpoint…")
        probe = None
        for brand in brand_list[:10]:
            try:
                soup = soup_of(brand["url"])
                for a in soup.find_all("a", href=True):
                    pp = path_parts(a["href"])
                    if (len(pp) == 3 and pp[0] == "tobaccos"
                            and pp[1] == brand["slug"]):
                        probe = normalize(a["href"])
                        break
                if probe:
                    break
            except Exception:
                continue
        if probe:
            print(f"  probe: {probe}")
            info = _probe_ajax_for_flavors(probe)
            if info:
                ep, keys, _ = info
                state["ajax_found"] = True
                state["ajax_endpoint"] = ep
                state["ajax_keys"] = keys
                save_state(state)
                print(f"  ✅ сохранено: endpoint={ep}, keys={keys}")
            else:
                print("  ❌ AJAX не найден, работаем без догрузки")

    print("▶ Sweeping list pages…")
    n1 = _sweep_flavor_lists(state, existing_urls)
    print(f"  +{n1} flavors, total={len(existing_urls)}")

    print("▶ Sweeping brands and lines…")
    n2 = _sweep_brands_and_lines(brand_list, state, existing_urls)
    print(f"  +{n2} flavors, total={len(existing_urls)}")

    if (not state["ajax_found"]) or args.force_search or len(existing_urls) < 5000:
        print("▶ Fallback: search by letters…")
        n3 = _search_by_letters(state, existing_urls)
        print(f"  +{n3} flavors, total={len(existing_urls)}")

    items = consolidate()
    print(f"\n✅ Done. total={len(items)} flavors")
    print(f"   output: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
