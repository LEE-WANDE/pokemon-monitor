"""
통합 상품 스크래퍼

1. 포켓몬스토어 (shop-api.e-ncp.com REST API)
2. 네이버 브랜드스토어 · 스마트스토어 × 2  (HTML __PRELOADED_STATE__ 파싱)

수집 후 공통 필터 적용:
  - 상품명에 "박스" AND "확장팩" 모두 포함
  - 상품명에 "1팩" OR "카드세트" 포함 시 제외
  - 가격 범위: 20,000원 ~ 55,000원
"""

import html as html_lib
import logging
import math
import re

import requests

import naver_scraper
from config import BASE_URL, CATEGORY_NO

logger = logging.getLogger(__name__)

# ── 필터 설정 ────────────────────────────────────────────────────────────────
_PRICE_MIN = 20_000
_PRICE_MAX = 55_000

# ── 네이버 사이트 목록 ────────────────────────────────────────────────────────
_NAVER_SITES = [
    {
        "site_name":        "네이버 브랜드스토어",
        "page_url":         "https://brand.naver.com/pokemon/category/7d4ef8ffe7ca4427b42a1a61751656e4",
        "channel_base_url": "https://brand.naver.com",
        "search_query":     "",
    },
    {
        "site_name":        "네이버 스마트스토어(포켓몬TCG)",
        "page_url":         "https://smartstore.naver.com/pokemontcg/category/d0c0dbc072f34aa5bfb50edfc6210441",
        "channel_base_url": "https://smartstore.naver.com",
        "search_query":     "",
    },
    {
        "site_name":        "네이버 스마트스토어(몬콜레)",
        "page_url":         "https://smartstore.naver.com/moncolle_korea/search",
        "channel_base_url": "https://smartstore.naver.com",
        "search_query":     "포켓몬카드",
    },
]

# ── 포켓몬스토어 API 상수 ─────────────────────────────────────────────────────
_API_SEARCH = "https://shop-api.e-ncp.com/products/search"
_CLIENT_ID  = "HJGfZ5jPHZk3/PEOkm+/Qw=="
_PAGE_SIZE  = 100


def _ps_headers() -> dict:
    return {
        "clientid":              _CLIENT_ID,
        "version":               "1.0",
        "platform":              "PC",
        "content-type":          "application/json",
        "shop-by-authorization": "",
        "accept":                "application/json, text/plain, */*",
        "accept-language":       "ko-KR,ko;q=0.9",
        "origin":                BASE_URL,
        "referer":               BASE_URL + "/",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }


def _ps_parse_item(item: dict) -> dict | None:
    product_no = item.get("productNo") or item.get("no")
    if not product_no:
        return None

    name = html_lib.unescape(
        (item.get("productName") or item.get("name") or "").strip()
    )
    if not name:
        return None

    price_raw = item.get("salePrice") or item.get("price") or 0
    try:
        price_int = int(float(price_raw))
        price = f"{price_int:,}원"
    except (TypeError, ValueError):
        price_int = 0
        price = "0원"

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

    product_url = (
        f"{BASE_URL}/pages/product/product-detail.html?productNo={product_no}"
    )

    return {
        "product_id": str(product_no),
        "name":       name,
        "price":      price,
        "price_int":  price_int,
        "status":     status,
        "url":        product_url,
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
        "categoryNos":             CATEGORY_NO,
        "categoryNo":              CATEGORY_NO,
        "pageSize":                str(_PAGE_SIZE),
        "pageNumber":              str(page_number),
    }
    resp = requests.get(_API_SEARCH, params=params, headers=_ps_headers(), timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data.get("items", []), int(data.get("totalCount", 0))


def _get_pokemonstore_products() -> list[dict]:
    logger.info("포켓몬스토어 API 호출 (categoryNo=%s)…", CATEGORY_NO)
    items_p1, total = _ps_fetch_page(1)
    if not items_p1 and total == 0:
        return []

    all_items = list(items_p1)
    total_pages = math.ceil(total / _PAGE_SIZE)
    for pg in range(2, total_pages + 1):
        extra, _ = _ps_fetch_page(pg)
        all_items.extend(extra)

    products = []
    for item in all_items:
        parsed = _ps_parse_item(item)
        if parsed:
            products.append(parsed)

    seen: dict[str, dict] = {}
    for p in products:
        seen[p["product_id"]] = p
    result = list(seen.values())

    logger.info("포켓몬스토어 완료: 전체 %d개 중 %d개 파싱", total, len(result))
    return result


# ── 공통 필터 ─────────────────────────────────────────────────────────────────

def _passes_filter(product: dict) -> bool:
    name = product.get("name", "")
    if "박스" not in name or "확장팩" not in name:
        return False
    if "1팩" in name or "카드세트" in name:
        return False
    price_int = product.get("price_int", 0)
    return _PRICE_MIN <= price_int <= _PRICE_MAX


# ── 공개 API ─────────────────────────────────────────────────────────────────

def get_products() -> tuple[list[dict], str]:
    """
    모든 사이트 상품 수집 후 필터 적용.
    Returns (filtered_products, summary_string)
    """
    raw_all: list[dict] = []
    errors:  list[str]  = []
    counts:  list[str]  = []

    # 1. 포켓몬스토어
    try:
        ps = _get_pokemonstore_products()
        raw_all.extend(ps)
        counts.append(f"포켓몬스토어:{len(ps)}")
    except Exception as e:
        logger.error("포켓몬스토어 수집 실패: %s", e, exc_info=True)
        errors.append(f"포켓몬스토어:{e}")

    # 2. 네이버 3개 사이트
    for cfg in _NAVER_SITES:
        try:
            items = naver_scraper.get_naver_products(**cfg)
            raw_all.extend(items)
            counts.append(f"{cfg['site_name']}:{len(items)}")
        except Exception as e:
            logger.error("[%s] 수집 실패: %s", cfg["site_name"], e, exc_info=True)
            errors.append(f"{cfg['site_name']}:{e}")

    # 3. 필터
    filtered = [p for p in raw_all if _passes_filter(p)]

    summary = ", ".join(counts)
    if errors:
        summary += " | 오류: " + ", ".join(errors)
    method = f"api+html (원본:{len(raw_all)} → 필터후:{len(filtered)}) [{summary}]"

    return filtered, method
