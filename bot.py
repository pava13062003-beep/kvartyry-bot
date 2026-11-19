"""Шукає нові 1-кімнатні квартири в оренду на rieltor.ua по вибраних ЖК
і надсилає їх у Telegram. Запускається GitHub Actions за розкладом."""
import json
import os
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---- НАЛАШТУВАННЯ ----------------------------------------------------------
MAX_USD = 620          # верхня межа ціни, $
MIN_USD = 350          # нижня межа (щоб відсіяти «ціни-приманки» і кімнати)
UAH_PER_USD = 45.6
USD_PER_EUR = 1.08

# ЖК на rieltor.ua: назва -> id (з адреси сторінки ...-n<ID>/)
COMPLEXES = {
    "Парус City": 17563,
    "Парус Park": 15577,
    "Safe Town": 19382,
    "HELGA": 19937,
    "Ренуар": 14703,
    "Місто Трав": 16590,
    "Фамілія": 14798,
    "Галицький Лев": 15881,
    "Greenville House": 16854,
    "Quoroom": 19305,
}

# Вулиці, по яких шукати ВСІ 1-кімнатні новобудови (не тільки ЖК вище).
# Пишемо корінь слова, маленькими літерами.
STREETS = [
    "кульпарківськ",    # Парус, Safe Town, Місто Трав, Фамілія
    "щирецьк",          # Well Home, Quoroom
    "княгині ольги",    # HELGA, Ренуар, Greenville House
    "перфецьк",         # біля Ашану
    "пулюя",            # Галицький Лев
    "трускавецьк",      # Continent
    "виговськ",         # Залізничний р-н
]
MIN_BUILD_YEAR = 2012   # «новобудова» = рік побудови від цього (або слово «новобудова»)
CITY_PAGES = 20         # скільки сторінок загального списку 1-кімнатних Львова гортати

# Не показувати, якщо в тексті є щось із цього
EXCLUDE_WORDS = ["сокільники", "подобов", "погодин", "кімната в квартирі"]
# -----------------------------------------------------------------------------

SEEN_FILE = Path(__file__).with_name("seen.json")
BASE = "https://rieltor.ua"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
    "Accept-Language": "uk-UA,uk;q=0.9",
}
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")


def complex_url(name: str, cid: int) -> str:
    return f"{BASE}/lvov/flats-rent/{requests.utils.quote(name).replace('%20', '+')}-n{cid}/"


def fetch(url: str) -> str | None:
    for attempt in range(3):
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code == 200:
            return r.text
        if r.status_code == 429:          # сайт просить пригальмувати
            time.sleep(30 * (attempt + 1))
            continue
        print(f"  {url} -> HTTP {r.status_code}")
        return None
    return None


def price_usd(text: str) -> int | None:
    m = re.search(r"(\d{1,3}(?: \d{3})*)\s*(\$|грн|€)\s*/\s*міс", text)
    if not m:
        return None
    value = int(re.sub(r"\D", "", m.group(1)))
    cur = m.group(2)
    if cur == "грн":
        return round(value / UAH_PER_USD)
    if cur == "€":
        return round(value * USD_PER_EUR)
    return value


def parse(html: str, complex_name: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for card in soup.select(".catalog-card"):
        a = card.select_one('a[href*="/flats-rent/view/"]')
        if not a:
            continue
        href = a["href"]
        url = href if href.startswith("http") else BASE + href
        m_id = re.search(r"/view/(\d+)", url)
        if not m_id:
            continue
        text = re.sub(r"\s+", " ", card.get_text(" ")).strip()
        # ціна: спершу окремий елемент ціни (у тексті картки перед ціною стоїть лічильник фото)
        price_el = card.select_one('[class*="price"]')
        price_src = re.sub(r"\s+", " ", price_el.get_text(" ")) if price_el else text
        price_raw = re.search(r"\d{1,3}(?: \d{3})*\s*(?:\$|грн|€)\s*/\s*міс", price_src)
        area = re.search(r"(\d+(?:[.,]\d+)?)\s*/[^м]*м²", text)
        floor = re.search(r"поверх\s*(\d+)\s*з\s*(\d+)", text)
        street = card.select_one('a[href*="/flats-rent/"][href*="vul"], .catalog-card-address')
        items.append({
            "id": m_id.group(1),
            "url": url,
            "complex": complex_name,
            "text": text,
            "rooms1": "1 кімната" in text,
            "usd": price_usd(price_raw.group(0)) if price_raw else None,
            "price_raw": price_raw.group(0).strip() if price_raw else "?",
            "area": area.group(1) if area else "?",
            "floor": f"{floor.group(1)} з {floor.group(2)}" if floor else "?",
            "street": street.get_text(" ", strip=True) if street else "",
            "no_fee": "без комісії" in text.lower(),
        })
    return items


def is_new_building(text: str) -> bool:
    low = text.lower()
    if "новобудов" in low or " жк " in f" {low} ":
        return True
    m = re.search(r"(\d{4}) рік побудови", text)
    return bool(m and int(m.group(1)) >= MIN_BUILD_YEAR)


def on_my_street(text: str) -> bool:
    low = text.lower()
    return any(s in low for s in STREETS)


def good(it: dict) -> bool:
    if not it["rooms1"] or it["usd"] is None:
        return False
    if it["complex"] == "Львів" and not (on_my_street(it["text"]) and is_new_building(it["text"])):
        return False
    if not (MIN_USD <= it["usd"] <= MAX_USD):
        return False
    low = it["text"].lower()
    return not any(w in low for w in EXCLUDE_WORDS)


def send(msg: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        print("[TG не налаштовано]\n" + msg)
        return
    requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        data={"chat_id": TG_CHAT_ID, "text": msg, "disable_web_page_preview": "false"},
        timeout=30,
    )
    time.sleep(1)


def fmt(it: dict) -> str:
    fee = "✅ без комісії" if it["no_fee"] else "комісія: уточнити"
    if it.get("owner"):
        fee += " · 👤 власник"
    snippet = it["text"]
    # короткий шматок опису після дати
    m = re.search(r"(сьогодні|вчора|\d+ (?:дн|тиж|міс)\S*\s*тому)\s*(.*)", snippet)
    desc = (m.group(2) if m else "")[:220]
    if not desc and it["complex"] == "ЛУН":
        desc = it["text"].split(it["street"], 1)[-1].strip()[:220] if it["street"] else it["text"][:220]
    title = it["complex"]
    if title == "ЛУН":
        title = f"{it['street']} (через ЛУН, {it['site']})"
    elif title == "Львів":
        addr = re.search(r"([А-ЯІЇЄҐ][а-яіїєґ'’\- ]+ (?:вул|просп|пл)\.,?\s*[\w\-/]*)", it["text"])
        title = (addr.group(1) if addr else "Львів") + (f" ({it['zk_name']})" if it.get("zk_name") else "")
    return (
        f"🏠 {title} — {it['price_raw']} (≈${it['usd']})\n"
        f"{it['area']} м², поверх {it['floor']}\n"
        f"{fee}\n"
        f"{desc}…\n"
        f"{it['url']}"
    )


# ---- ЛУН (lun.ua) ------------------------------------------------------------
LUN_PAGES = 30          # сторінок загального списку 1-кімнатних Львова на ЛУН (24 на сторінці)


def lun_cards(html: str) -> list[dict]:
    """Дістає картки оголошень з даних сторінки ЛУН (Next.js)."""
    parts = [json.loads('"' + m.group(1) + '"')
             for m in re.finditer(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', html)]
    s = "".join(parts)
    out, dec = {}, json.JSONDecoder()
    for m in re.finditer(r'\{"id":\d+,"groupId"', s):
        try:
            o, _ = dec.raw_decode(s, m.start())
        except ValueError:
            continue
        if "urlRaw" in o:
            out[o["id"]] = o
    return list(out.values())


def lun_item(o: dict) -> dict:
    cur = (o.get("currency") or "usd").lower()
    price = o.get("price") or 0
    usd = round(price / UAH_PER_USD) if cur == "uah" else round(price * USD_PER_EUR) if cur == "eur" else price
    sym = {"usd": "$", "uah": "грн", "eur": "€"}.get(cur, cur)
    url = o.get("urlRaw") or ""
    rid = re.search(r"rieltor\.ua/.*/view/(\d+)", url)
    text = " ".join(str(x) for x in (o.get("geo"), o.get("header"), o.get("text")) if x)
    return {
        "id": rid.group(1) if rid else f"lun{o.get('groupId') or o['id']}",
        "group": f"lun{o.get('groupId') or o['id']}",
        "url": url,
        "complex": "ЛУН",
        "text": text,
        "rooms1": o.get("roomCount") == 1,
        "usd": usd if price else None,
        "price_raw": f"{price:,} {sym}/міс".replace(",", " "),
        "area": o.get("areaTotal") or "?",
        "floor": f"{o.get('floor')} з {o.get('floorCount')}",
        "street": o.get("header") or "",
        "no_fee": "без комісії" in text.lower() or "без посередник" in text.lower(),
        "owner": bool(o.get("isOwner")),
        "year": o.get("builtYear"),
        "site": (o.get("site") or {}).get("displayName", ""),
    }


def lun_good(it: dict) -> bool:
    if not it["rooms1"] or it["usd"] is None or not (MIN_USD <= it["usd"] <= MAX_USD):
        return False
    low = it["text"].lower()
    if any(w in low for w in EXCLUDE_WORDS) or not on_my_street(it["text"]):
        return False
    if it["year"]:
        return it["year"] >= MIN_BUILD_YEAR
    return "новобуд" in low or "жк" in low


def scan_lun(seen: set, found: list) -> None:
    ids = set()
    for page in range(1, LUN_PAGES + 1):
        url = "https://lun.ua/rent/lviv/flats-odnokimnatni" + (f"?page={page}" if page > 1 else "")
        html = fetch(url)
        time.sleep(5)
        if not html:
            break
        new = [lun_item(o) for o in lun_cards(html)]
        new = [it for it in new if it["group"] not in ids]
        if not new:
            break
        for it in new:
            ids.add(it["group"])
            if it["id"] in seen or it["group"] in seen:
                continue
            if lun_good(it):
                seen.update({it["id"], it["group"]})
                found.append(it)
    print(f"ЛУН: {len(ids)} оголошень")


def main() -> None:
    seen = set(json.loads(SEEN_FILE.read_text())) if SEEN_FILE.exists() else set()
    first_run = not seen
    found = []
    for name, cid in COMPLEXES.items():
        items, ids = [], set()
        for page in range(1, 4):           # сайт сортує не за датою, тому до 3 сторінок
            url = complex_url(name, cid) + (f"?page={page}" if page > 1 else "")
            html = fetch(url)
            time.sleep(6)                  # пауза, щоб не отримати 429
            if not html:
                break
            new = [it for it in parse(html, name) if it["id"] not in ids]
            if not new:
                break
            ids.update(it["id"] for it in new)
            items += new
            if len(new) < 20:
                break
        print(f"{name}: {len(items)} оголошень")
        for it in items:
            if it["id"] in seen:
                continue
            seen.add(it["id"])
            if good(it):
                found.append(it)

    # Загальний список усіх 1-кімнатних у Львові -> фільтр по вулицях
    city_ids = set()
    for page in range(1, CITY_PAGES + 1):
        url = f"{BASE}/lvov/flats-rent/1-room/" + (f"?page={page}" if page > 1 else "")
        html = fetch(url)
        time.sleep(6)
        if not html:
            break
        new = [it for it in parse(html, "Львів") if it["id"] not in city_ids]
        if not new:
            break
        city_ids.update(it["id"] for it in new)
        for it in new:
            if it["id"] in seen:
                continue
            if not it["rooms1"]:
                continue
            # запам'ятовуємо тільки ті, що на моїх вулицях (інші не важливі)
            if on_my_street(it["text"]):
                seen.add(it["id"])
                m = re.search(r"ЖК ([^\d,]{2,30}?) \d кімнат", it["text"])
                if m:
                    it["complex"] = "Львів"  # залишаємо мітку для фільтра
                    it["zk_name"] = "ЖК " + m.group(1).strip()
                if good(it):
                    found.append(it)
    print(f"Загальний список: {len(city_ids)} оголошень")

    scan_lun(seen, found)

    found.sort(key=lambda x: (not x["no_fee"], x["usd"]))
    if first_run and found:
        send(f"Бот запущено. Зараз підходить {len(found)} квартир(и):")
    for it in found:
        send(fmt(it))
    print(f"Нових підходящих: {len(found)}")
    SEEN_FILE.write_text(json.dumps(sorted(seen)))


if __name__ == "__main__":
    main()
