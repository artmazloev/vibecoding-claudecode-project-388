#!/usr/bin/env python3
"""Автономный прогон трекера цен для GitHub Actions.

Полный цикл БЕЗ Claude/MCP, только стандартная библиотека Python:
  1. обойти зашитый список URL, для каждого извлечь цену
     ({ regular_price, sale_price, has_credit }) — та же логика, что в
     скилле extract-price;
  2. собрать единую таблицу прогона;
  3. прочитать предыдущий прогон из приватного репозитория tracker-data
     (через GitHub Contents API);
  4. посчитать diff и оставить только значимые изменения (правила из
     KNOWLEDGE.md: порог цены >= 1%, скидка, рассрочка, состав списка);
  5. сохранить свежий прогон в tracker-data как YYYY-MM-DD.json;
  6. отправить короткую сводку в Telegram через send.py.

Переменные окружения:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  — для отправки (см. send.py);
  GH_DATA_TOKEN                         — PAT с доступом на запись в tracker-data;
  TRACKER_DATA_REPO                     — "owner/repo", по умолчанию из константы.
"""
import os
import re
import sys
import json
import base64
import datetime
import urllib.request
import urllib.error

# Каталог скрипта -> корень репозитория (чтобы импортировать send.py).
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from send import send as telegram_send  # noqa: E402

# --- Конфигурация -----------------------------------------------------------

TRACKER_DATA_REPO = os.environ.get("TRACKER_DATA_REPO", "artmazloev/tracker-data")

# Отслеживаемый список URL (тот же, что зашит в скилл tracker).
URLS = [
    "https://www.detmir.ru/product/index/id/4576880/",
    "https://www.detmir.ru/product/index/id/4576776/",
    "https://www.detmir.ru/product/index/id/4066114/",
    "https://www.coffee-butik.ru/katalog/kofe/sublimirovannyj-rastvorimyj-kofe/p-gold/",
]

PRICE_THRESHOLD = 0.01  # 1% — порог значимости цены из KNOWLEDGE.md
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

# --- extract-price ----------------------------------------------------------

def to_number(raw):
    """'1 299,00 ₽' -> 1299.0; мусор -> None."""
    if raw is None:
        return None
    s = re.sub(r"[₽руб.RUBp]", "", str(raw), flags=re.IGNORECASE)
    s = re.sub(r"[\s   ']", "", s)
    s = s.replace(",", ".")
    s = re.sub(r"[^0-9.]", "", s)
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def fetch_html(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ru-RU"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, "replace")


def _jsonld_price(html):
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    ):
        try:
            data = json.loads(m.group(1).strip())
        except (ValueError, TypeError):
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                t = node.get("@type", "")
                is_product = t == "Product" or (isinstance(t, list) and "Product" in t)
                if is_product and node.get("offers"):
                    offers = node["offers"]
                    offers = offers if isinstance(offers, list) else [offers]
                    for of in offers:
                        if isinstance(of, dict) and of.get("price") is not None:
                            return of["price"]
                stack.extend(node.values())
    return None


def _meta_price(html):
    for pat in (
        r'<meta[^>]+itemprop=["\']price["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+property=["\']product:price:amount["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+property=["\']og:price:amount["\'][^>]+content=["\']([^"\']+)["\']',
    ):
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def _struck_old_price(html):
    """Зачёркнутая старая цена: <del>..</del> или класс *old*/*strike*."""
    for m in re.finditer(r"<(del|s)[^>]*>(.*?)</\1>", html, re.DOTALL | re.IGNORECASE):
        num = to_number(re.sub(r"<[^>]+>", "", m.group(2)))
        if num is not None:
            return num
    m = re.search(r'<[^>]+class=["\'][^"\']*(?:old|strike)[^"\']*["\'][^>]*>(.*?)</',
                  html, re.DOTALL | re.IGNORECASE)
    if m:
        return to_number(re.sub(r"<[^>]+>", "", m.group(1)))
    return None


CREDIT_RE = re.compile(r"рассрочк|в кредит|частями|сплит|podeli|подели|/\s*мес", re.IGNORECASE)


def extract_price(url):
    """Один URL -> { regular_price, sale_price, has_credit }."""
    result = {"regular_price": None, "sale_price": None, "has_credit": False}
    try:
        html = fetch_html(url)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as err:
        result["error"] = "fetch_failed: %s" % err
        return result

    text = re.sub(r"<[^>]+>", " ", html)
    result["has_credit"] = bool(CREDIT_RE.search(text))

    current = to_number(_jsonld_price(html))
    if current is None:
        current = to_number(_meta_price(html))
    old = _struck_old_price(html)

    if old is not None and current is not None and old != current:
        result["regular_price"] = max(old, current)
        result["sale_price"] = min(old, current)
    else:
        result["regular_price"] = current
        result["sale_price"] = None

    if result["regular_price"] is None:
        result["error"] = "price_not_found"
    return result


# --- tracker: сбор таблицы ---------------------------------------------------

def run_batch():
    rows = []
    for url in URLS:
        price = extract_price(url)
        row = {"url": url}
        row.update({k: price[k] for k in ("regular_price", "sale_price", "has_credit")})
        if "error" in price:
            row["error"] = price["error"]
        rows.append(row)
    return rows


# --- tracker-data через GitHub Contents API ---------------------------------

API = "https://api.github.com"


def _api(path, method="GET", body=None, token=None):
    url = API + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "price-tracker-automation")
    if token:
        req.add_header("Authorization", "Bearer %s" % token)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode("utf-8", "replace") or "{}")


DATE_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")


def load_previous_run(token, today):
    """Вернуть (rows, date) последнего прогона до сегодняшнего, либо (None, None)."""
    status, listing = _api("/repos/%s/contents/" % TRACKER_DATA_REPO, token=token)
    if status != 200 or not isinstance(listing, list):
        return None, None
    dates = []
    for item in listing:
        m = DATE_FILE_RE.match(item.get("name", ""))
        if m and m.group(1) != today:
            dates.append(m.group(1))
    if not dates:
        return None, None
    prev_date = max(dates)
    status, blob = _api(
        "/repos/%s/contents/%s.json" % (TRACKER_DATA_REPO, prev_date), token=token)
    if status != 200:
        return None, None
    content = base64.b64decode(blob["content"]).decode("utf-8")
    return json.loads(content), prev_date


def save_run(token, today, rows):
    path = "/repos/%s/contents/%s.json" % (TRACKER_DATA_REPO, today)
    # SHA существующего файла (если сегодня уже был прогон) — чтобы обновить.
    status, existing = _api(path, token=token)
    sha = existing.get("sha") if status == 200 else None
    body = {
        "message": "run: %s" % today,
        "content": base64.b64encode(
            (json.dumps(rows, ensure_ascii=False, indent=2) + "\n").encode()).decode(),
    }
    if sha:
        body["sha"] = sha
    status, _ = _api(path, method="PUT", body=body, token=token)
    return 200 <= status < 300


# --- diff + значимость (KNOWLEDGE.md) ---------------------------------------

def _pct(old, new):
    return (new - old) / old if old else None


def compute_significant(prev_rows, curr_rows):
    prev = {r["url"]: r for r in prev_rows}
    changes = []
    for c in curr_rows:
        p = prev.get(c["url"])
        if p is None:
            changes.append({"url": c["url"], "kind": "added", "row": c})
            continue
        if c.get("regular_price") is None:
            # снятие цены не удалось — не доверяем строке, пропускаем,
            # чтобы не слать ложные «скидка снята» / «рассрочка пропала».
            continue
        items = []
        # regular_price — порог. Сравниваем только когда обе цены известны:
        # если текущую цену снять не удалось (None), это сбой снятия, а не
        # изменение цены — не шумим и не падаем.
        pr, cr = p.get("regular_price"), c.get("regular_price")
        if pr is not None and cr is not None and pr != cr:
            rel = _pct(pr, cr)
            if rel is None or abs(rel) >= PRICE_THRESHOLD:
                items.append(("price", pr, cr, rel))
        # sale_price
        ps, cs = p.get("sale_price"), c.get("sale_price")
        if ps is None and cs is not None:
            items.append(("sale_new", None, cs, None))
        elif ps is not None and cs is None:
            items.append(("sale_gone", ps, None, None))
        elif ps is not None and cs is not None and ps != cs:
            rel = _pct(ps, cs)
            if abs(rel) >= PRICE_THRESHOLD:
                items.append(("sale", ps, cs, rel))
        # has_credit — любое изменение
        if bool(p.get("has_credit")) != bool(c.get("has_credit")):
            items.append(("credit", p.get("has_credit"), c.get("has_credit"), None))
        if items:
            changes.append({"url": c["url"], "kind": "changed", "items": items, "row": c})
    curr_urls = {c["url"] for c in curr_rows}
    for p in prev_rows:
        if p["url"] not in curr_urls:
            changes.append({"url": p["url"], "kind": "removed", "row": p})
    return changes


# --- форматирование сводки для Telegram -------------------------------------

def short_name(url):
    slug = [s for s in url.rstrip("/").split("/") if s]
    tail = slug[-1] if slug else url
    return tail[:40]


def money(v):
    if v is None:
        return "—"
    v = int(v) if float(v).is_integer() else v
    return "{:,}".format(v).replace(",", " ")


def format_summary(changes, first_run, curr_rows):
    if first_run:
        lines = ["📸 Первый прогон — снимок цен", ""]
        for r in curr_rows:
            price = r.get("sale_price") or r.get("regular_price")
            credit = " 💳" if r.get("has_credit") else ""
            lines.append("🔻 %s — %s ₽%s" % (short_name(r["url"]), money(price), credit))
        return "\n".join(lines)

    if not changes:
        return "Значимых изменений цен нет"

    lines = ["💰 Значимые изменения цен", ""]
    for ch in changes:
        name = short_name(ch["url"])
        if ch["kind"] == "added":
            lines.append("🆕 %s" % name)
        elif ch["kind"] == "removed":
            lines.append("❌ %s" % name)
        else:
            credit = " 💳" if ch["row"].get("has_credit") else ""
            parts = []
            for kind, old, new, rel in ch["items"]:
                if kind in ("price", "sale"):
                    arrow = "🔻" if (rel is not None and rel < 0) else "🔺"
                    parts.append("%s %s ₽ / %s ₽" % (arrow, money(old), money(new)))
                elif kind == "sale_new":
                    parts.append("🔻 скидка → %s ₽" % money(new))
                elif kind == "sale_gone":
                    parts.append("🔺 скидка %s ₽ снята" % money(old))
                elif kind == "credit":
                    parts.append("рассрочка %s → %s" % (
                        "да" if old else "нет", "да" if new else "нет"))
            lines.append("%s — %s%s" % (name, "; ".join(parts), credit))
    return "\n".join(lines)


# --- главный сценарий -------------------------------------------------------

def main():
    token = os.environ.get("GH_DATA_TOKEN")
    if not token:
        sys.stderr.write("Нет GH_DATA_TOKEN — некуда сохранять прогон в tracker-data.\n")
        return 1

    today = datetime.date.today().isoformat()
    curr_rows = run_batch()
    print("Прогон %s: снято %d товаров" % (today, len(curr_rows)))
    print(json.dumps(curr_rows, ensure_ascii=False, indent=2))

    # Если не удалось снять ни одной цены (например, сайты блокируют раннер) —
    # не портим историю нулевым прогоном: предупреждаем и выходим без сохранения.
    if all(r.get("regular_price") is None for r in curr_rows):
        print("Ни одной цены не снято — прогон не сохраняем.")
        telegram_send("⚠️ Прогон трекера не удался: цены не сняты "
                      "(сайты недоступны с раннера).")
        return 0

    prev_rows, prev_date = load_previous_run(token, today)
    first_run = prev_rows is None
    if first_run:
        print("Предыдущего прогона нет — это первый прогон (снимок).")
        changes = []
    else:
        print("Сравнение с прогоном %s" % prev_date)
        changes = compute_significant(prev_rows, curr_rows)
        print("Значимых изменений: %d" % len(changes))

    if not save_run(token, today, curr_rows):
        sys.stderr.write("Не удалось сохранить прогон в tracker-data.\n")
        return 1
    print("Прогон сохранён в %s/%s.json" % (TRACKER_DATA_REPO, today))

    summary = format_summary(changes, first_run, curr_rows)
    print("--- сводка ---\n%s" % summary)
    rc = telegram_send(summary)
    return rc


if __name__ == "__main__":
    sys.exit(main())
