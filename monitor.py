"""
포켓몬 카드 재고 모니터링 — GitHub Actions 전용 통합 스크립트

크롤링 대상 (매 실행마다 전체 수집):
  1. 포켓몬스토어 (shop-api.e-ncp.com REST API)
  2. 카드마니아     (HTML — Godomall)
  3. TCG박스        (HTML — Cafe24)
  4. 옥션           (HTML — onclick setItemHistory 파싱)
  5. G마켓          (HTML — onclick setItemHistory 파싱)
  6. SSG            (Next.js __NEXT_DATA__ JSON)
  7. 네이버 스마트스토어 × 3 (플러스디스트리뷰션 / 토이벤져스 / 문구달)
     — 네이버 쇼핑 검색 API(openapi.naver.com)로 키워드 검색 후 mallName으로 필터링
     — smartstore.naver.com 직접 크롤링은 로그인 리다이렉트로 차단되어 API 방식으로 전환

필터:
  - "확장팩" or "하이클래스팩" 포함
  - "1팩", "카드세트" 포함 시 제외
  - 가격 20,000 ~ 45,000원

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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("monitor")

STATE_PATH = Path(__file__).parent / "data" / "state.json"
DOCS_PATH  = Path(__file__).parent / "docs" / "index.html"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
NAVER_CLIENT_ID = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")

PRICE_MIN = 20_000
PRICE_MAX = 45_000

_CHECK_INTERVAL_MINUTES = 20
_EVENT_BADGE_WINDOW = timedelta(hours=24)
_ACTIONS_URL = "https://github.com/LEE-WANDE/pokemon-monitor/actions/workflows/monitor.yml"

_SITE_COLORS = {
    "포켓몬스토어":                        "#0a1e3f",
    "카드마니아":                          "#dc2626",
    "TCG박스":                             "#7c3aed",
    "옥션":                                "#0d9488",
    "G마켓":                               "#f97316",
    "SSG":                                 "#2563eb",
    "네이버 스마트스토어(플러스디스트리뷰션)": "#16a34a",
    "네이버 스마트스토어(토이벤져스)":        "#16a34a",
    "네이버 스마트스토어(문구달)":            "#16a34a",
}

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


# ── 7. 네이버 스마트스토어 (쇼핑 검색 API) ────────────────────────────────────
# 방식: 네이버 쇼핑 검색 API(openapi.naver.com/v1/search/shop.json)로
#       스토어별 검색어를 검색한 뒤, 응답의 mallName이 해당 스토어와 일치하는
#       상품만 추려낸다. smartstore.naver.com 직접 크롤링은 로그인 리다이렉트로
#       차단되어(도메인 전체에 걸린 봇 차단으로 추정) 검색 API로 전환.
#
# 제한사항:
#   - 검색 API는 "네이버쇼핑에 노출된" 상품만 반환하므로 스토어의 전체 카테고리
#     재고와 100% 일치하지 않을 수 있음 (검색 노출 여부에 좌우됨)
#   - 품절 여부 필드를 제공하지 않아 모든 상품을 판매중으로 간주함
#     → 재입고 감지는 사실상 불가, "신규 노출 감지"로 동작

_NAVER_API_URL = "https://openapi.naver.com/v1/search/shop.json"
_NAVER_PAGES_TO_SCAN = 3   # display=100 기준 최대 300개까지 검색 결과 스캔
_NAVER_TAG_RE = re.compile(r"</?b>")

# mall_name 검증 결과 (2026-07-25):
#   - 토이 벤져스: 검색 결과에 mallName="토이 벤져스"(공백 포함)로 실제 확인됨
#   - 플러스디스트리뷰션 / 문구달: "포켓몬 카드 확장팩"/"포켓몬카드 확장팩" 검색 결과를
#     sim·date 정렬 + API 최대 조회 한도(1,000건)까지 스캔해도 두 스토어의 상품이
#     전혀 노출되지 않음 → mallName 미확인 (아래는 상호명 그대로 넣어둔 추정값).
#     스토어가 가격비교 노출을 켜지 않았거나 포켓몬 카드 상품을 아직 등록하지
#     않았을 가능성이 큼. 노출되면 다음 실행부터 자동으로 잡힘.
_NAVER_STORES = [
    {
        "site_name": "네이버 스마트스토어(플러스디스트리뷰션)",
        "mall_name": "플러스디스트리뷰션",
        "query":     "포켓몬 카드 확장팩",
    },
    {
        "site_name": "네이버 스마트스토어(토이벤져스)",
        "mall_name": "토이 벤져스",  # 실제 mallName은 공백 포함 (API 조회로 확인)
        "query":     "포켓몬 카드 확장팩",
    },
    {
        "site_name": "네이버 스마트스토어(문구달)",
        "mall_name": "문구달",
        "query":     "포켓몬 카드 확장팩",
    },
]


def _naver_api_search(query: str, start: int, display: int = 100) -> list[dict]:
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        raise RuntimeError("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 미설정")

    headers = {
        "X-Naver-Client-Id":     NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    params = {"query": query, "display": display, "start": start, "sort": "sim"}
    r = requests.get(_NAVER_API_URL, headers=headers, params=params, timeout=15)
    r.raise_for_status()
    return r.json().get("items", [])


def _naver_parse_item(item: dict, site_name: str) -> dict | None:
    product_id = item.get("productId")
    if not product_id:
        return None

    name = html_lib.unescape(_NAVER_TAG_RE.sub("", item.get("title", ""))).strip()
    if not name:
        return None

    try:
        price_int = int(item.get("lprice") or 0)
    except (TypeError, ValueError):
        price_int = 0

    return {
        "product_id": f"naver_{product_id}",
        "name":       name,
        "price":      f"{price_int:,}원",
        "price_int":  price_int,
        # 쇼핑 검색 API는 품절 여부를 제공하지 않음 — 노출되면 판매중으로 간주
        "status":     "판매중",
        "url":        item.get("link", ""),
        "image_url":  item.get("image", ""),
        "site_name":  site_name,
    }


def _make_naver_fetch_fn(cfg: dict):
    def _fetch_fn() -> list[dict]:
        site_name = cfg["site_name"]
        mall_name = cfg["mall_name"]
        query     = cfg["query"]

        matched: dict[str, dict] = {}
        for i in range(_NAVER_PAGES_TO_SCAN):
            start = i * 100 + 1
            items = _naver_api_search(query, start=start)
            if not items:
                break
            for item in items:
                if item.get("mallName") != mall_name:
                    continue
                parsed = _naver_parse_item(item, site_name)
                if parsed:
                    matched[parsed["product_id"]] = parsed
            if len(items) < 100:
                break
            time.sleep(0.3)

        logger.info("[%s] 수집 완료: %d개 (mallName=%s)", site_name, len(matched), mall_name)
        return list(matched.values())
    return _fetch_fn


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
] + [
    (cfg["site_name"], _make_naver_fetch_fn(cfg))
    for cfg in _NAVER_STORES
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


# ── GitHub Pages 대시보드 생성 ─────────────────────────────────────────────────

def _is_recent_event(event_at: str | None) -> bool:
    if not event_at:
        return False
    try:
        dt = datetime.fromisoformat(event_at)
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - dt) < _EVENT_BADGE_WINDOW


def generate_dashboard(state: dict[str, dict], last_checked_iso: str, next_check_iso: str) -> str:
    products = list(state.values())
    products.sort(key=lambda p: (
        0 if (p.get("last_event") in ("new", "restocked") and _is_recent_event(p.get("event_at"))) else 1,
        0 if p.get("status") == "판매중" else 1,
        p.get("site_name", ""),
        -(p.get("price_int") or 0),
    ))

    slim = [
        {
            "name":       p.get("name", ""),
            "price":      p.get("price", ""),
            "status":     p.get("status", "판매중"),
            "url":        p.get("url", ""),
            "image_url":  p.get("image_url", ""),
            "site_name":  p.get("site_name", ""),
            "last_event": p.get("last_event"),
            "event_at":   p.get("event_at"),
        }
        for p in products
    ]

    html_out = _DASHBOARD_TEMPLATE
    html_out = html_out.replace("__PRODUCTS_JSON__", json.dumps(slim, ensure_ascii=False))
    html_out = html_out.replace("__SITE_COLORS_JSON__", json.dumps(_SITE_COLORS, ensure_ascii=False))
    html_out = html_out.replace("__LAST_CHECKED__", last_checked_iso or "")
    html_out = html_out.replace("__NEXT_CHECK__", next_check_iso or "")
    html_out = html_out.replace("__ACTIONS_URL__", _ACTIONS_URL)
    return html_out


_DASHBOARD_TEMPLATE = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>포켓몬 카드 모니터</title>
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans KR", sans-serif;
    background: #f4f4f6;
    color: #1a1a1a;
  }
  header {
    background: linear-gradient(135deg, #0f0c29 0%, #302b63 50%, #24243e 100%);
    color: #fff;
    padding: 32px 24px 28px;
  }
  .header-inner {
    max-width: 1200px;
    margin: 0 auto;
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 16px;
  }
  .title-row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  h1 { margin: 0; font-size: 1.5rem; font-weight: 700; }
  .live-badge {
    display: inline-flex; align-items: center; gap: 6px;
    background: rgba(255,255,255,0.12);
    padding: 4px 10px; border-radius: 999px; font-size: 0.72rem; font-weight: 700;
    letter-spacing: 0.06em;
  }
  .live-dot { width: 8px; height: 8px; border-radius: 50%; background: #4ade80; animation: pulse 1.6s infinite; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.25; } }
  .subtitle { margin-top: 6px; font-size: 0.82rem; color: rgba(255,255,255,0.65); }
  .check-btn {
    background: rgba(255,255,255,0.12);
    border: 1px solid rgba(255,255,255,0.28);
    color: #fff; padding: 10px 18px; border-radius: 10px; font-size: 0.85rem; font-weight: 600;
    cursor: pointer; text-decoration: none; display: inline-flex; align-items: center; gap: 6px;
    transition: background 0.15s;
    white-space: nowrap;
  }
  .check-btn:hover { background: rgba(255,255,255,0.24); }

  main { max-width: 1200px; margin: 0 auto; padding: 24px; }

  .summary { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-bottom: 22px; }
  .summary-card {
    background: #fff; border-radius: 14px; padding: 16px 8px; text-align: center;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06);
  }
  .summary-card .num { font-size: 1.5rem; font-weight: 700; }
  .summary-card .label { font-size: 0.75rem; color: #6b7280; margin-top: 4px; }
  .summary-card.total .num      { color: #24243e; }
  .summary-card.active .num     { color: #16a34a; }
  .summary-card.new .num        { color: #dc2626; }
  .summary-card.restocked .num  { color: #2563eb; }
  .summary-card.soldout .num    { color: #9ca3af; }

  .filters { display: flex; gap: 8px; margin-bottom: 20px; flex-wrap: wrap; }
  .filter-btn {
    border: 1px solid #d1d5db; background: #fff; padding: 8px 16px; border-radius: 999px;
    font-size: 0.82rem; font-weight: 500; cursor: pointer; color: #374151; transition: all .15s;
  }
  .filter-btn.active { background: #24243e; color: #fff; border-color: #24243e; }

  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); gap: 16px; }
  .card {
    background: #fff; border-radius: 14px; overflow: hidden; cursor: pointer;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06); position: relative; transition: transform .15s, box-shadow .15s;
    display: flex; flex-direction: column;
  }
  .card:hover { transform: translateY(-3px); box-shadow: 0 10px 24px rgba(0,0,0,0.12); }
  .card.soldout { opacity: 0.5; }
  .thumb { width: 100%; aspect-ratio: 1 / 1; object-fit: cover; background: #eee; display: block; }
  .thumb.placeholder { display: flex; align-items: center; justify-content: center; font-size: 2.2rem; color: #ccc; }
  .site-tag {
    position: absolute; top: 8px; left: 8px; font-size: 0.66rem; font-weight: 700; color: #fff;
    padding: 3px 8px; border-radius: 6px; z-index: 2; max-width: 75%;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .badge {
    position: absolute; top: 8px; right: 8px; font-size: 0.66rem; font-weight: 800; color: #fff;
    padding: 3px 8px; border-radius: 6px; z-index: 2;
  }
  .badge.new        { background: #dc2626; }
  .badge.restocked  { background: #2563eb; }
  .card-body { padding: 12px; display: flex; flex-direction: column; gap: 6px; flex: 1; }
  .card-name { font-size: 0.85rem; font-weight: 600; line-height: 1.35; min-height: 2.3em; }
  .card-price { font-size: 1rem; font-weight: 700; color: #111; }
  .status-pill {
    font-size: 0.7rem; font-weight: 700; padding: 2px 9px; border-radius: 999px; width: fit-content;
  }
  .status-pill.available { background: #dcfce7; color: #166534; }
  .status-pill.soldout    { background: #e5e7eb; color: #4b5563; }

  footer { text-align: center; padding: 28px 16px 44px; color: #6b7280; font-size: 0.82rem; }
  .footer-live { display: inline-flex; align-items: center; gap: 8px; margin-bottom: 6px; }
  .footer-dot { width: 8px; height: 8px; border-radius: 50%; background: #16a34a; animation: pulse 1.6s infinite; }
  .empty { text-align: center; padding: 60px 0; color: #999; }

  @media (max-width: 720px) {
    .summary { grid-template-columns: repeat(2, 1fr); }
    h1 { font-size: 1.25rem; }
  }
</style>
</head>
<body>

<header>
  <div class="header-inner">
    <div>
      <div class="title-row">
        <h1>🎴 포켓몬 카드 모니터</h1>
        <span class="live-badge"><span class="live-dot"></span>LIVE</span>
      </div>
      <div class="subtitle" id="lastChecked">마지막 체크: -</div>
    </div>
    <a class="check-btn" href="__ACTIONS_URL__" target="_blank" rel="noopener">🔄 지금 체크</a>
  </div>
</header>

<main>
  <div class="summary">
    <div class="summary-card total"><div class="num" id="cntTotal">0</div><div class="label">전체</div></div>
    <div class="summary-card active"><div class="num" id="cntActive">0</div><div class="label">판매중</div></div>
    <div class="summary-card new"><div class="num" id="cntNew">0</div><div class="label">신규</div></div>
    <div class="summary-card restocked"><div class="num" id="cntRestocked">0</div><div class="label">재입고</div></div>
    <div class="summary-card soldout"><div class="num" id="cntSoldout">0</div><div class="label">품절</div></div>
  </div>

  <div class="filters">
    <button class="filter-btn active" data-filter="all">전체</button>
    <button class="filter-btn" data-filter="new">신규</button>
    <button class="filter-btn" data-filter="restocked">재입고</button>
    <button class="filter-btn" data-filter="available">판매중</button>
    <button class="filter-btn" data-filter="soldout">품절</button>
  </div>

  <div class="grid" id="grid"></div>
  <div class="empty" id="emptyMsg" style="display:none;">조건에 맞는 상품이 없습니다.</div>
</main>

<footer>
  <div class="footer-live"><span class="footer-dot"></span>모니터링 중 · 20분마다 자동 체크</div>
  <div id="countdown">다음 체크까지 계산 중…</div>
</footer>

<script>
const PRODUCTS = __PRODUCTS_JSON__;
const SITE_COLORS = __SITE_COLORS_JSON__;
const LAST_CHECKED = "__LAST_CHECKED__";
const NEXT_CHECK = "__NEXT_CHECK__";
const EVENT_WINDOW_MS = 24 * 60 * 60 * 1000;

function isRecent(iso) {
  if (!iso) return false;
  return (Date.now() - new Date(iso).getTime()) < EVENT_WINDOW_MS;
}

function escapeHtml(s) {
  const div = document.createElement('div');
  div.textContent = s || '';
  return div.innerHTML;
}

function render(filter) {
  const grid = document.getElementById('grid');
  grid.innerHTML = '';
  let shown = 0;

  for (const p of PRODUCTS) {
    const isNew = p.last_event === 'new' && isRecent(p.event_at);
    const isRestocked = p.last_event === 'restocked' && isRecent(p.event_at);
    const isSoldout = p.status === '품절';

    if (filter === 'new' && !isNew) continue;
    if (filter === 'restocked' && !isRestocked) continue;
    if (filter === 'available' && isSoldout) continue;
    if (filter === 'soldout' && !isSoldout) continue;

    shown++;
    const card = document.createElement('div');
    card.className = 'card' + (isSoldout ? ' soldout' : '');
    if (p.url) {
      card.addEventListener('click', () => window.open(p.url, '_blank', 'noopener'));
    }

    const color = SITE_COLORS[p.site_name] || '#475569';
    const badge = isNew
      ? '<div class="badge new">NEW</div>'
      : (isRestocked ? '<div class="badge restocked">재입고</div>' : '');
    const img = p.image_url
      ? '<img class="thumb" src="' + p.image_url + '" loading="lazy" onerror="this.outerHTML=\'<div class=&quot;thumb placeholder&quot;>🎴</div>\'">'
      : '<div class="thumb placeholder">🎴</div>';

    card.innerHTML =
      '<div class="site-tag" style="background:' + color + '">' + escapeHtml(p.site_name) + '</div>' +
      badge + img +
      '<div class="card-body">' +
        '<div class="card-name">' + escapeHtml(p.name) + '</div>' +
        '<div class="card-price">' + escapeHtml(p.price) + '</div>' +
        '<div class="status-pill ' + (isSoldout ? 'soldout' : 'available') + '">' + escapeHtml(p.status) + '</div>' +
      '</div>';

    grid.appendChild(card);
  }

  document.getElementById('emptyMsg').style.display = shown === 0 ? 'block' : 'none';
}

function updateSummary() {
  const total = PRODUCTS.length;
  const soldout = PRODUCTS.filter(p => p.status === '품절').length;
  const active = total - soldout;
  const newCount = PRODUCTS.filter(p => p.last_event === 'new' && isRecent(p.event_at)).length;
  const restockedCount = PRODUCTS.filter(p => p.last_event === 'restocked' && isRecent(p.event_at)).length;

  document.getElementById('cntTotal').textContent = total;
  document.getElementById('cntActive').textContent = active;
  document.getElementById('cntNew').textContent = newCount;
  document.getElementById('cntRestocked').textContent = restockedCount;
  document.getElementById('cntSoldout').textContent = soldout;
}

document.querySelectorAll('.filter-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    render(btn.dataset.filter);
  });
});

function fmtDateTime(iso) {
  if (!iso) return '-';
  try {
    return new Date(iso).toLocaleString('ko-KR', { hour12: false });
  } catch (e) {
    return '-';
  }
}

document.getElementById('lastChecked').textContent = '마지막 체크: ' + fmtDateTime(LAST_CHECKED);

function updateCountdown() {
  const el = document.getElementById('countdown');
  if (!NEXT_CHECK) { el.textContent = ''; return; }
  const diff = new Date(NEXT_CHECK).getTime() - Date.now();
  if (diff <= 0) {
    el.textContent = '곧 체크 예정 (GitHub Actions 대기열 상황에 따라 지연될 수 있음)';
    return;
  }
  const m = Math.floor(diff / 60000);
  const s = Math.floor((diff % 60000) / 1000);
  el.textContent = '다음 체크까지 약 ' + m + '분 ' + s + '초';
}

updateSummary();
render('all');
updateCountdown();
setInterval(updateCountdown, 1000);
setTimeout(() => location.reload(), 5 * 60 * 1000);
</script>
</body>
</html>
"""


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

    now_iso = datetime.now(timezone.utc).isoformat()

    new_count = restocked_count = 0
    for product_id, product in current.items():
        prev = previous.get(product_id)
        if prev is None:
            product["first_seen"] = now_iso
            if not is_first_run:
                product["last_event"] = "new"
                product["event_at"] = now_iso
                send_discord(product, "new")
                new_count += 1
            else:
                product["last_event"] = None
                product["event_at"] = None
        else:
            product["first_seen"] = prev.get("first_seen", now_iso)
            was_sold_out  = prev.get("status") == "품절"
            now_available = product.get("status") == "판매중"
            if was_sold_out and now_available:
                product["last_event"] = "restocked"
                product["event_at"] = now_iso
                send_discord(product, "restocked")
                restocked_count += 1
            else:
                # 이벤트 없음 — 이전 신규/재입고 배지를 24시간 표시 동안 유지
                product["last_event"] = prev.get("last_event")
                product["event_at"] = prev.get("event_at")

    merged = dict(previous)
    merged.update(current)
    save_state(merged)

    next_check_iso = (datetime.now(timezone.utc) + timedelta(minutes=_CHECK_INTERVAL_MINUTES)).isoformat()
    DOCS_PATH.parent.mkdir(parents=True, exist_ok=True)
    DOCS_PATH.write_text(generate_dashboard(merged, now_iso, next_check_iso), encoding="utf-8")

    if is_first_run:
        logger.info("초기 로드 완료: %d개 (%s)", len(current), summary)
    else:
        logger.info(
            "체크 완료: %d개 확인, 신규 %d개, 재입고 %d개 (%s)",
            len(current), new_count, restocked_count, summary,
        )


if __name__ == "__main__":
    main()
