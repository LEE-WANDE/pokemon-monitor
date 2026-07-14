"""
포켓몬 카드 재고 모니터링 — GitHub Actions 전용 통합 스크립트

크롤링 대상 (매 실행마다 전체 수집):
  1. 포켓몬스토어 (shop-api.e-ncp.com REST API)
  2. 카드마니아     (HTML — Godomall)
  3. TCG박스        (HTML — Cafe24)
  4. 옥션           (HTML — onclick setItemHistory 파싱)
  5. G마켓          (HTML — onclick setItemHistory 파싱)
  6. SSG            (Next.js __NEXT_DATA__ JSON)

필터:
  - "확장팩" or "하이클래스팩" 포함
  - "1팩", "카드세트" 포함 시 제외
  - 가격 20,000 ~ 55,000원

상태 저장:
  GitHub Actions는 서버가 없어 DB를 유지할 수 없으므로,
  data/state.json 에 이전 체크 결과를 저장하고 워크플로우가 매 실행 후 커밋한다.
  이번 실행 결과와 비교해 신규 등록 / 재입고를 감지해 디스코드로 알림한다.
"""

import html as html_lib
import json
import logging
import math
import os
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("monitor")

STATE_PATH = Path(__file__).parent / "data" / "state.json"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

PRICE_MIN = 20_000
PRICE_MAX = 55_000

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}

_RE_SET_ITEM = re.compile(
    r"setItemHistory\('([^']+)',\s*'([^']+)',\s*'([^']+)',\s*'([^']*)'",
)
_RE_PRICE = re.compile(r"[^\d]")


def _fetch(url: str, referer: str = "") -> BeautifulSoup:
    hdrs = dict(_HEADERS)
    if referer:
        hdrs["Referer"] = referer
    r = requests.get(url, headers=hdrs, timeout=15)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


# ── 1. 포켓몬스토어 ───────────────────────────────────────────────────────────

_PS_BASE_URL   = "https://www.pokemonstore.co.kr"
_PS_CATEGORY   = "488359"
_PS_API_SEARCH = "https://shop-api.e-ncp.com/products/search"
_PS_CLIENT_ID  = "HJGfZ5jPHZk3/PEOkm+/Qw=="
_PS_PAGE_SIZE  = 100


def _ps_headers() -> dict:
    return {
        "clientid":              _PS_CLIENT_ID,
        "version":               "1.0",
        "platform":              "PC",
        "content-type":          "application/json",
        "shop-by-authorization": "",
        "accept":                "application/json, text/plain, */*",
        "accept-language":       "ko-KR,ko;q=0.9",
        "origin":                _PS_BASE_URL,
        "referer":               _PS_BASE_URL + "/",
        "user-agent":            _HEADERS["User-Agent"],
    }


def _ps_parse_item(item: dict) -> dict | None:
    product_no = item.get("productNo") or item.get("no")
    if not product_no:
        return None

    name = html_lib.unescape((item.get("productName") or item.get("name") or "").strip())
    if not name:
        return None

    price_raw = item.get("salePrice") or item.get("price") or 0
    try:
        price_int = int(float(price_raw))
    except (TypeError, ValueError):
        price_int = 0

    is_sold_out = bool(
        item.get("isSoldOut")
        or (item.get("stockCnt") is not None and item.get("stockCnt") == 0)
        or item.get("saleStatus") == "SOLD_OUT"
    )
    status = "품절" if is_sold_out else "판매중"

    images = item.get("imageUrlInfo") or item.get("images") or []
    image_url = ""
    if images and isinstance(images[0], dict):
        raw = images[0].get("url") or images[0].get("imageUrl") or ""
        image_url = ("https:" + raw) if raw.startswith("//") else raw

    return {
        "product_id": str(product_no),
        "name":       name,
        "price":      f"{price_int:,}원",
        "price_int":  price_int,
        "status":     status,
        "url":        f"{_PS_BASE_URL}/pages/product/product-detail.html?productNo={product_no}",
        "image_url":  image_url,
        "site_name":  "포켓몬스토어",
    }


def _ps_fetch_page(page_number: int) -> tuple[list, int]:
    params = {
        "order.by":                "SALE_CNT",
        "order.direction":         "DESC",
        "filter.saleStatus":       "ALL_CONDITIONS",
        "filter.soldout":          "true",
        "filter.totalReviewCount": "true",
        "filter.keywords":         "",
        "categoryNos":             _PS_CATEGORY,
        "categoryNo":              _PS_CATEGORY,
        "pageSize":                str(_PS_PAGE_SIZE),
        "pageNumber":              str(page_number),
    }
    resp = requests.get(_PS_API_SEARCH, params=params, headers=_ps_headers(), timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data.get("items", []), int(data.get("totalCount", 0))


def get_pokemonstore_products() -> list[dict]:
    items_p1, total = _ps_fetch_page(1)
    if not items_p1 and total == 0:
        return []

    all_items = list(items_p1)
    total_pages = math.ceil(total / _PS_PAGE_SIZE)
    for pg in range(2, total_pages + 1):
        extra, _ = _ps_fetch_page(pg)
        all_items.extend(extra)

    seen: dict[str, dict] = {}
    for item in all_items:
        parsed = _ps_parse_item(item)
        if parsed:
            seen[parsed["product_id"]] = parsed
    logger.info("[포켓몬스토어] 수집 완료: 전체 %d개 중 %d개 파싱", total, len(seen))
    return list(seen.values())


# ── 2. 카드마니아 ─────────────────────────────────────────────────────────────

_CM_BASE = "https://www.cardmania2021.com"
_CM_LIST = f"{_CM_BASE}/goods/goods_list.php?cateCd=001001&sort=&pageNum=40"
_CM_HOME = _CM_BASE + "/"


def _parse_cardmania_page(soup: BeautifulSoup) -> list[dict]:
    products = []
    for item in soup.select("div.goods_list li"):
        name_a = item.select_one("div.item_tit_box a")
        if not name_a:
            continue
        name = name_a.get_text(strip=True)
        if not name:
            continue

        href = name_a.get("href", "")
        m = re.search(r"goodsNo=(\d+)", href)
        if not m:
            continue
        goods_no = m.group(1)

        price_span = item.select_one("strong.item_price span")
        price_text = price_span.get_text(strip=True) if price_span else ""
        price_num  = re.sub(r"[^\d]", "", price_text)
        price_int  = int(price_num) if price_num else 0

        soldout_img = item.select_one('img[alt="품절"]')
        status = "품절" if soldout_img else "판매중"

        img = item.select_one("img.middle")
        img_url = img["src"] if img else ""

        products.append({
            "product_id": f"cardmania_{goods_no}",
            "name":       name,
            "price":      f"{price_int:,}원",
            "price_int":  price_int,
            "status":     status,
            "url":        f"{_CM_BASE}/goods/goods_view.php?goodsNo={goods_no}",
            "image_url":  img_url,
            "site_name":  "카드마니아",
        })
    return products


def get_cardmania_products() -> list[dict]:
    all_products: list[dict] = []

    page = 1
    while True:
        url  = _CM_LIST if page == 1 else f"{_CM_LIST}&page={page}"
        soup = _fetch(url, _CM_BASE + "/")
        items = _parse_cardmania_page(soup)
        if not items:
            break
        all_products.extend(items)

        page_links = soup.select("div.pagination a[href]")
        next_pages = [
            int(a.get_text(strip=True))
            for a in page_links
            if a.get_text(strip=True).isdigit() and int(a.get_text(strip=True)) > page
        ]
        if not next_pages:
            break
        page += 1
        time.sleep(1.0)

    try:
        soup_home  = _fetch(_CM_HOME, _CM_BASE + "/")
        home_items = _parse_cardmania_page(soup_home)
        all_products.extend(home_items)
    except Exception as e:
        logger.warning("[카드마니아] 홈페이지 수집 실패 (계속 진행): %s", e)

    seen = {p["product_id"]: p for p in all_products}
    logger.info("[카드마니아] 수집 완료: %d개", len(seen))
    return list(seen.values())


# ── 3. TCG박스 ────────────────────────────────────────────────────────────────

_TB_BASE = "https://tcgbox.co.kr"
_TB_CAT  = f"{_TB_BASE}/category/%ED%99%95%EC%9E%A5%ED%8C%A9-BOX/191/"


def _parse_tcgbox_page(items) -> list[dict]:
    products = []
    for item in items:
        pid = item.get("id", "").replace("anchorBoxId_", "")
        if not pid:
            continue

        name = ""
        for span in item.select("div.name a span"):
            if "displaynone" in span.get("class", []):
                continue
            if span.find("span"):
                continue
            t = span.get_text(strip=True)
            if t and t not in ("상품명", ":"):
                name = t
                break
        if not name:
            continue

        desc      = item.select_one("div.description[ec-data-price]")
        price_int = int(desc["ec-data-price"]) if desc and desc.get("ec-data-price") else 0

        soldout_img = item.select_one('img[alt="품절"]')
        status = "품절" if soldout_img else "판매중"

        href_el = item.select_one("div.name a")
        if href_el:
            raw_href = href_el.get("href", "")
            _m = re.match(r"(/product/[^/]+/\d+)", raw_href)
            url = _TB_BASE + (_m.group(1) + "/" if _m else raw_href)
        else:
            url = ""

        img     = item.select_one(f"img#eListPrdImage{pid}_1")
        img_src = img.get("src", "") if img else ""
        img_url = ("https:" + img_src) if img_src.startswith("//") else img_src

        products.append({
            "product_id": f"tcgbox_{pid}",
            "name":       name,
            "price":      f"{price_int:,}원",
            "price_int":  price_int,
            "status":     status,
            "url":        url,
            "image_url":  img_url,
            "site_name":  "TCG박스",
        })
    return products


def get_tcgbox_products() -> list[dict]:
    all_products: list[dict] = []
    page = 1

    while True:
        url  = _TB_CAT if page == 1 else f"{_TB_CAT}?page={page}"
        soup = _fetch(url, _TB_BASE + "/")

        raw_items = [
            i for i in soup.select("ul.prdList li.xans-record-")
            if i.get("id", "").startswith("anchorBoxId_")
        ]
        if not raw_items:
            break

        all_products.extend(_parse_tcgbox_page(raw_items))

        paging_links = soup.select(".ec-base-paginate a, .xans-product-normalpaging a")
        next_pages = [
            int(a.get_text(strip=True))
            for a in paging_links
            if a.get_text(strip=True).isdigit() and int(a.get_text(strip=True)) > page
        ]
        if not next_pages:
            break
        page += 1
        time.sleep(1.0)

    seen = {p["product_id"]: p for p in all_products}
    logger.info("[TCG박스] 수집 완료: %d개", len(seen))
    return list(seen.values())


# ── 4. 옥션 ───────────────────────────────────────────────────────────────────

_AU_URL = "https://stores.auction.co.kr/pokemoncardgame"


def get_auction_products() -> list[dict]:
    soup = _fetch(_AU_URL, "https://www.auction.co.kr/")
    products = []
    for item in soup.select("div.prod_list ul.type1 li"):
        a = item.select_one("p.prd_img a[onclick]")
        if not a:
            continue
        m = _RE_SET_ITEM.search(a.get("onclick", ""))
        if not m:
            continue
        itemno, name, price_raw, img_raw = m.group(1), m.group(2), m.group(3), m.group(4)
        name = name.strip()
        try:
            price_int = int(float(price_raw))
        except (TypeError, ValueError):
            price_int = 0
        img_url = ("https:" + img_raw) if img_raw.startswith("//") else img_raw
        products.append({
            "product_id": f"auction_{itemno}",
            "name":       name,
            "price":      f"{price_int:,}원",
            "price_int":  price_int,
            "status":     "판매중",
            "url":        f"http://itempage3.auction.co.kr/DetailView.aspx?itemno={itemno}",
            "image_url":  img_url,
            "site_name":  "옥션",
        })

    seen = {p["product_id"]: p for p in products}
    logger.info("[옥션] 수집 완료: %d개", len(seen))
    return list(seen.values())


# ── 5. G마켓 ──────────────────────────────────────────────────────────────────

_GM_URL = "https://minishop.gmarket.co.kr/pokemoncard"


def get_gmarket_products() -> list[dict]:
    soup = _fetch(_GM_URL, "https://www.gmarket.co.kr/")
    products = []
    for a in soup.select("div.prod_list a[href*='goodsCode'][onclick]"):
        m = _RE_SET_ITEM.search(a.get("onclick", ""))
        if not m:
            continue
        goods_code, name, price_raw, img_raw = m.group(1), m.group(2), m.group(3), m.group(4)
        name = name.strip()
        try:
            price_int = int(float(price_raw))
        except (TypeError, ValueError):
            price_int = 0
        img_url = ("https:" + img_raw) if img_raw.startswith("//") else img_raw
        products.append({
            "product_id": f"gmarket_{goods_code}",
            "name":       name,
            "price":      f"{price_int:,}원",
            "price_int":  price_int,
            "status":     "판매중",
            "url":        f"https://item.gmarket.co.kr/Item?goodsCode={goods_code}",
            "image_url":  img_url,
            "site_name":  "G마켓",
        })

    seen = {p["product_id"]: p for p in products}
    logger.info("[G마켓] 수집 완료: %d개", len(seen))
    return list(seen.values())


# ── 6. SSG ────────────────────────────────────────────────────────────────────

_SSG_URL  = "https://www.ssg.com/sellerhome/pokemontcg/best.ssg"
_SSG_BASE = "https://www.ssg.com"


def _parse_ssg_price(text: str) -> int:
    num = _RE_PRICE.sub("", text or "")
    return int(num) if num else 0


def get_ssg_products() -> list[dict]:
    hdrs = dict(_HEADERS)
    hdrs["Referer"] = _SSG_BASE + "/"
    raw = requests.get(_SSG_URL, headers=hdrs, timeout=15)
    raw.raise_for_status()

    nd_m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        raw.text,
        re.DOTALL,
    )
    if not nd_m:
        raise RuntimeError("SSG __NEXT_DATA__ 없음")

    data = json.loads(nd_m.group(1))
    queries = (
        data.get("props", {})
        .get("pageProps", {})
        .get("dehydratedState", {})
        .get("queries", [])
    )

    products = []
    for q in queries:
        qd = q.get("state", {}).get("data", {})
        if not isinstance(qd, dict):
            continue
        result_list = qd.get("initialPage", {}).get("resultList", [])
        if not result_list:
            continue
        for item in result_list:
            item_id = item.get("itemId") or item.get("custKey", "")
            name    = (item.get("itemName") or "").strip()
            if not name or not item_id:
                continue

            price_info = item.get("priceInfo", {}) or {}
            price_str  = item.get("finalPrice") or price_info.get("primaryPrice") or "0"
            price_int  = _parse_ssg_price(str(price_str))

            is_soldout = (
                item.get("isDisableCartButton") is True
                or bool(item.get("soldOutMessage"))
            )
            status = "품절" if is_soldout else "판매중"

            products.append({
                "product_id": f"ssg_{item_id}",
                "name":       name,
                "price":      f"{price_int:,}원" if price_int else price_str,
                "price_int":  price_int,
                "status":     status,
                "url":        item.get("itemUrl", "") or item.get("itemDetailLink", ""),
                "image_url":  item.get("itemImgUrl", ""),
                "site_name":  "SSG",
            })
        break  # 첫 번째 resultList만 사용

    seen = {p["product_id"]: p for p in products}
    logger.info("[SSG] 수집 완료: %d개", len(seen))
    return list(seen.values())


# ── 공통 필터 ─────────────────────────────────────────────────────────────────

def _passes_filter(product: dict) -> bool:
    name = product.get("name", "")
    if "확장팩" not in name and "하이클래스팩" not in name:
        return False
    if "1팩" in name or "카드세트" in name:
        return False
    price_int = product.get("price_int", 0)
    return PRICE_MIN <= price_int <= PRICE_MAX


_SOURCES = [
    ("포켓몬스토어", get_pokemonstore_products),
    ("카드마니아",   get_cardmania_products),
    ("TCG박스",      get_tcgbox_products),
    ("옥션",         get_auction_products),
    ("G마켓",        get_gmarket_products),
    ("SSG",          get_ssg_products),
]


def collect_all() -> tuple[dict[str, dict], str]:
    """전체 사이트 수집 + 필터. 사이트별 예외는 로그만 남기고 나머지는 계속 수집."""
    combined: dict[str, dict] = {}
    summary: list[str] = []

    for site_name, fetch_fn in _SOURCES:
        try:
            raw = fetch_fn()
            filtered = [p for p in raw if _passes_filter(p)]
            for p in filtered:
                combined[p["product_id"]] = p
            summary.append(f"{site_name}:{len(filtered)}")
        except Exception as e:
            logger.error("[%s] 수집 실패: %s", site_name, e, exc_info=True)
            summary.append(f"{site_name}:오류")

    return combined, " | ".join(summary)


# ── 상태 저장/로드 ────────────────────────────────────────────────────────────

def load_state() -> dict[str, dict]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("state.json 로드 실패, 빈 상태로 시작: %s", e)
        return {}


def save_state(state: dict[str, dict]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


# ── 디스코드 알림 ─────────────────────────────────────────────────────────────

def send_discord(product: dict, badge: str) -> None:
    if not DISCORD_WEBHOOK_URL:
        logger.warning("DISCORD_WEBHOOK_URL 미설정 — 알림 건너뜀: %s", product["name"])
        return

    site_name  = product.get("site_name", "")
    site_label = f"[{site_name}] " if site_name else ""

    if badge == "new":
        title, color = f"🆕 신규 상품 등록! {site_label}", 0xE53935
    else:
        title, color = f"🔄 재입고 감지! {site_label}", 0x1E88E5

    embed = {
        "title":       title,
        "url":         product.get("url", ""),
        "description": f"**{product['name']}**",
        "color":       color,
        "fields": [
            {"name": "💰 가격", "value": product.get("price", "—"), "inline": True},
            {"name": "📦 상태", "value": product.get("status", "판매중"), "inline": True},
        ],
    }
    if site_name:
        embed["fields"].append({"name": "🏪 사이트", "value": site_name, "inline": True})
    if product.get("image_url"):
        embed["thumbnail"] = {"url": product["image_url"]}

    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
        if r.status_code == 429:
            retry_after = r.json().get("retry_after", 1)
            time.sleep(float(retry_after) + 0.5)
            r = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
        r.raise_for_status()
        logger.info("Discord 전송 완료: %s", product["name"])
    except Exception as e:
        logger.error("Discord 전송 실패 (%s): %s", product["name"], e)

    time.sleep(1.0)  # 웹훅 레이트리밋 방지


# ── 메인 ──────────────────────────────────────────────────────────────────────

def main() -> None:
    previous = load_state()
    is_first_run = len(previous) == 0

    current, summary = collect_all()

    if not current:
        logger.warning("수집된 상품이 없습니다 (%s). 상태를 변경하지 않고 종료합니다.", summary)
        return

    new_count = restocked_count = 0
    for product_id, product in current.items():
        prev = previous.get(product_id)
        if prev is None:
            if not is_first_run:
                send_discord(product, "new")
                new_count += 1
        else:
            was_sold_out  = prev.get("status") == "품절"
            now_available = product.get("status") == "판매중"
            if was_sold_out and now_available:
                send_discord(product, "restocked")
                restocked_count += 1

    merged = dict(previous)
    merged.update(current)
    save_state(merged)

    if is_first_run:
        logger.info("초기 로드 완료: %d개 (%s)", len(current), summary)
    else:
        logger.info(
            "체크 완료: %d개 확인, 신규 %d개, 재입고 %d개 (%s)",
            len(current), new_count, restocked_count, summary,
        )


if __name__ == "__main__":
    main()
