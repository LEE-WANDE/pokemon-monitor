"""
포켓몬 스토어 (pokemonstore.co.kr) 상품 스크래퍼

플랫폼: NHN Commerce (shopby / e-ncp.com)
방식:   shop-api.e-ncp.com REST API 직접 호출 (Playwright 불필요)
"""

import html as html_lib
import logging
import math

import requests

from config import BASE_URL, CATEGORY_NO

logger = logging.getLogger(__name__)

# ── API 상수 ───────────────────────────────────────────────────────────────────

_API_SEARCH = "https://shop-api.e-ncp.com/products/search"
_CLIENT_ID  = "HJGfZ5jPHZk3/PEOkm+/Qw=="
_PAGE_SIZE  = 100   # API 허용 최대치로 설정해 요청 횟수 최소화


def _headers() -> dict:
    return {
        "clientid":             _CLIENT_ID,
        "version":              "1.0",
        "platform":             "PC",
        "content-type":         "application/json",
        "shop-by-authorization": "",
        "accept":               "application/json, text/plain, */*",
        "accept-language":      "ko-KR,ko;q=0.9",
        "origin":               BASE_URL,
        "referer":              BASE_URL + "/",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }


# ── 파싱 헬퍼 ─────────────────────────────────────────────────────────────────

def _parse_item(item: dict) -> dict | None:
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
        price = f"{int(float(price_raw)):,}원"
    except (TypeError, ValueError):
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
        "status":     status,
        "url":        product_url,
        "image_url":  image_url,
    }


# ── 메인 API 호출 ─────────────────────────────────────────────────────────────

def _fetch_page(page_number: int, page_size: int = _PAGE_SIZE) -> tuple[list, int]:
    """한 페이지 분량의 상품을 반환. (items, totalCount)"""
    params = {
        "order.by":                "SALE_CNT",
        "order.direction":         "DESC",
        "filter.saleStatus":       "ALL_CONDITIONS",
        "filter.soldout":          "true",
        "filter.totalReviewCount": "true",
        "filter.keywords":         "",
        "categoryNos":             CATEGORY_NO,
        "categoryNo":              CATEGORY_NO,
        "pageSize":                str(page_size),
        "pageNumber":              str(page_number),
    }
    resp = requests.get(_API_SEARCH, params=params, headers=_headers(), timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data.get("items", []), int(data.get("totalCount", 0))


def get_products() -> tuple[list[dict], str]:
    """
    모든 상품을 가져온다.
    Returns: (products, method)  — method는 항상 'api'
    """
    logger.info("NHN Commerce API 호출 시작 (categoryNo=%s)…", CATEGORY_NO)

    try:
        # 1페이지 + 전체 수 확인
        items_p1, total = _fetch_page(1)
        if not items_p1 and total == 0:
            logger.warning("API 응답에 상품이 없습니다.")
            return [], "api"

        all_items = list(items_p1)

        # 페이지가 더 있으면 추가 호출
        total_pages = math.ceil(total / _PAGE_SIZE)
        for pg in range(2, total_pages + 1):
            extra, _ = _fetch_page(pg)
            all_items.extend(extra)

        # 파싱
        products = []
        for item in all_items:
            parsed = _parse_item(item)
            if parsed:
                products.append(parsed)

        # product_id 중복 제거
        seen: dict[str, dict] = {}
        for p in products:
            seen[p["product_id"]] = p
        products = list(seen.values())

        logger.info(
            "API 완료: 전체 %d개 중 %d개 파싱 성공", total, len(products)
        )
        return products, "api"

    except requests.HTTPError as e:
        logger.error("API HTTP 오류 (%s): %s", e.response.status_code, e)
        return [], "api_error"
    except Exception as e:
        logger.error("API 오류: %s", e, exc_info=True)
        return [], "api_error"
