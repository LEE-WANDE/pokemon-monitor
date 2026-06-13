"""
네이버 스마트스토어 / 브랜드스토어 상품 스크래퍼

1차 방법: curl_cffi 브라우저 TLS 지문 모방 + 세션 워밍업 → __PRELOADED_STATE__ 파싱
2차 방법: 환경변수 NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 설정 시
          공식 Naver Shopping Open API 사용 (429 완전 우회, 무료 25,000회/일)

Railway 같은 데이터센터 IP에서는 1차 방법이 Naver IP 평판 차단으로 실패할 수 있음.
그 경우 2차 방법(공식 API) 사용 권장.
"""

import json
import logging
import os
import random
import re
import time
from urllib.parse import quote

import requests as stdlib_requests
from curl_cffi import requests as creq

logger = logging.getLogger(__name__)

# ── 공식 Naver Shopping API 인증 (선택) ─────────────────────────────────────
_NAVER_CLIENT_ID     = os.environ.get("NAVER_CLIENT_ID", "")
_NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")
_USE_OFFICIAL_API    = bool(_NAVER_CLIENT_ID and _NAVER_CLIENT_SECRET)

# ── curl_cffi 세션 ────────────────────────────────────────────────────────────
_SESS = creq.Session(impersonate="chrome120")
_SESSION_WARMED = False

_PAGE_SIZE = 40

_SOLD_OUT_STATUSES = {"OUTOFSTOCK", "SOLD_OUT", "SUSPENSION", "CLOSE", "DELETED"}
_STATE_KEYS        = ["categoryProducts", "searchProducts", "productSearch", "productList"]

# ── 재시도 설정 ───────────────────────────────────────────────────────────────
_MAX_RETRIES  = 3
_RETRY_BASE_S = 8   # 첫 번째 재시도 대기(초)


# ── 세션 워밍업 ───────────────────────────────────────────────────────────────

def _warm_up():
    """네이버 메인 → 쇼핑 메인 방문으로 쿠키/세션 초기화."""
    global _SESSION_WARMED
    if _SESSION_WARMED:
        return
    try:
        _SESS.get(
            "https://www.naver.com/",
            headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                     "Accept-Language": "ko-KR,ko;q=0.9"},
            timeout=10,
        )
        time.sleep(random.uniform(0.8, 1.5))
        _SESS.get(
            "https://shopping.naver.com/",
            headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                     "Accept-Language": "ko-KR,ko;q=0.9"},
            timeout=10,
        )
        _SESSION_WARMED = True
        logger.info("Naver 세션 워밍업 완료")
    except Exception as e:
        logger.warning("세션 워밍업 실패(계속 진행): %s", e)
        _SESSION_WARMED = True  # 실패해도 무한 재시도 방지


# ── HTML 스크래핑 헬퍼 ────────────────────────────────────────────────────────

def _browser_headers(referer: str) -> dict:
    return {
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,"
            "application/signed-exchange;v=b3;q=0.7"
        ),
        "Accept-Encoding":          "gzip, deflate, br",
        "Accept-Language":          "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control":            "max-age=0",
        "Referer":                  referer,
        "Sec-Ch-Ua":                '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        "Sec-Ch-Ua-Mobile":         "?0",
        "Sec-Ch-Ua-Platform":       '"Windows"',
        "Sec-Fetch-Dest":           "document",
        "Sec-Fetch-Mode":           "navigate",
        "Sec-Fetch-Site":           "same-origin",
        "Sec-Fetch-User":           "?1",
        "Upgrade-Insecure-Requests": "1",
    }


def _fetch_html(url: str, referer: str) -> str:
    _warm_up()
    headers = _browser_headers(referer)

    for attempt in range(_MAX_RETRIES):
        try:
            r = _SESS.get(url, headers=headers, timeout=25)

            if r.status_code == 429:
                wait = _RETRY_BASE_S * (2 ** attempt) + random.uniform(2, 5)
                logger.warning(
                    "429 차단 — %d초 후 재시도 (%d/%d): %s",
                    round(wait), attempt + 1, _MAX_RETRIES, url,
                )
                time.sleep(wait)
                continue

            r.raise_for_status()
            # 빈 HTML 또는 에러 페이지 감지
            if len(r.text) < 5000:
                logger.warning("응답이 너무 짧음 (%d bytes) — 차단 의심: %s", len(r.text), url)
            return r.text

        except creq.exceptions.RequestsError as e:
            if attempt < _MAX_RETRIES - 1:
                wait = _RETRY_BASE_S * (2 ** attempt)
                logger.warning("요청 오류 %s — %ds 후 재시도", e, wait)
                time.sleep(wait)
            else:
                raise

    raise RuntimeError(f"최대 재시도 초과: {url}")


def _extract_state(html: str) -> dict:
    m = re.search(r"window\.__PRELOADED_STATE__=(\{.*)", html)
    if not m:
        return {}
    raw = m.group(1)
    end = raw.find("</script>")
    if end > 0:
        raw = raw[:end]
    raw = re.sub(r"\bundefined\b", "null", raw)
    raw = re.sub(r"\bInfinity\b",  "null", raw)
    raw = re.sub(r"\bNaN\b",       "null", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("__PRELOADED_STATE__ JSON 파싱 실패: %s", e)
        return {}


def _find_products_section(state: dict) -> tuple[list, dict]:
    for key in _STATE_KEYS + ["keywordSearch"]:
        section = state.get(key)
        if not isinstance(section, dict):
            continue
        products = section.get("simpleProducts", [])
        if isinstance(products, list) and products:
            return products, section
        for variant_val in section.values():
            if not isinstance(variant_val, dict):
                continue
            products = variant_val.get("simpleProducts", [])
            if isinstance(products, list) and products:
                return products, variant_val
    return [], {}


def _parse_product(item: dict, site_name: str, channel_base_url: str) -> dict | None:
    product_no = item.get("productNo")
    if not product_no:
        return None

    name = (item.get("name") or item.get("dispName") or "").strip()
    if not name:
        return None

    price_raw = item.get("salePrice") or 0
    try:
        price_int = int(price_raw)
        price     = f"{price_int:,}원"
    except (TypeError, ValueError):
        price_int = 0
        price     = "0원"

    status_type = item.get("productStatusType", "")
    status      = "품절" if status_type in _SOLD_OUT_STATUSES else "판매중"

    channel     = item.get("channel") or {}
    channel_uid = channel.get("channelUid", "")
    product_url = (
        f"{channel_base_url}/{channel_uid}/products/{product_no}"
        if channel_uid else ""
    )

    return {
        "product_id": f"naver_{channel_uid}_{product_no}",
        "name":       name,
        "price":      price,
        "price_int":  price_int,
        "status":     status,
        "url":        product_url,
        "image_url":  item.get("representativeImageUrl") or "",
        "site_name":  site_name,
    }


def _scrape_html(
    site_name: str,
    page_url: str,
    channel_base_url: str,
    search_query: str = "",
) -> list[dict]:
    """HTML __PRELOADED_STATE__ 방식으로 상품 수집."""
    all_products: list[dict] = []
    page = 1

    while True:
        url = f"{page_url}?q={quote(search_query)}&cp={page}" if search_query \
              else f"{page_url}?cp={page}"

        logger.info("[%s] HTML 수집 페이지 %d: %s", site_name, page, url)
        try:
            html = _fetch_html(url, page_url)
        except Exception as e:
            logger.error("[%s] 수집 실패: %s", site_name, e)
            break

        state = _extract_state(html)
        if not state:
            logger.warning("[%s] __PRELOADED_STATE__ 없음", site_name)
            break

        products_raw, section_meta = _find_products_section(state)
        if not products_raw:
            logger.warning("[%s] 상품 없음 (페이지 %d)", site_name, page)
            break

        for item in products_raw:
            parsed = _parse_product(item, site_name, channel_base_url)
            if parsed:
                all_products.append(parsed)

        total     = section_meta.get("totalCount") or 0
        page_size = section_meta.get("pageSize") or _PAGE_SIZE
        if total <= page * page_size:
            break

        page += 1
        time.sleep(random.uniform(2, 4))  # 페이지 간 딜레이

    seen: dict[str, dict] = {}
    for p in all_products:
        seen[p["product_id"]] = p
    return list(seen.values())


# ── 공식 Naver Shopping Open API ─────────────────────────────────────────────
# 등록: https://developers.naver.com/apps/#/register
# Railway 환경변수에 NAVER_CLIENT_ID, NAVER_CLIENT_SECRET 추가
# 무료 한도: 25,000회/일 (20분 간격 × 4개 사이트 ≒ 288회/일)

_SHOPPING_API = "https://openapi.naver.com/v1/search/shop.json"

# 공식 API에서 각 스토어를 구분하는 mallName 키워드
_SITE_MALL_NAMES = {
    "네이버 브랜드스토어":           "포켓몬센터코리아",
    "네이버 스마트스토어(포켓몬TCG)": "포켓몬카드게임코리아공식스토어",
    "네이버 스마트스토어(몬콜레)":    "몬콜레코리아",
}

_NAVER_SOLD_OUT_STATUSES = {"품절", "판매중지"}


def _api_search_products(
    query: str,
    site_name: str,
    page_url: str,
) -> list[dict]:
    """
    Naver Shopping Open API로 상품 검색.
    mallName 필터로 특정 스토어 상품만 추출.
    """
    mall_filter = _SITE_MALL_NAMES.get(site_name, "")
    headers = {
        "X-Naver-Client-Id":     _NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": _NAVER_CLIENT_SECRET,
    }

    all_items = []
    start = 1
    display = 100  # 최대 100개/요청

    while True:
        params = {
            "query":   query,
            "display": display,
            "start":   start,
            "sort":    "sim",
        }
        try:
            r = stdlib_requests.get(_SHOPPING_API, params=params, headers=headers, timeout=15)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            logger.error("[%s] Naver Shopping API 오류: %s", site_name, e)
            break

        items = data.get("items", [])
        if not items:
            break

        # mallName 필터
        for item in items:
            if mall_filter and mall_filter not in item.get("mallName", ""):
                continue
            all_items.append(item)

        total = data.get("total", 0)
        if start + display > min(total, 1000):  # API 최대 1000개
            break
        start += display
        time.sleep(0.3)

    # 파싱
    products = []
    for item in all_items:
        title = re.sub(r"<[^>]+>", "", item.get("title", "")).strip()
        if not title:
            continue

        price_str = item.get("lprice") or item.get("hprice") or "0"
        try:
            price_int = int(price_str)
            price     = f"{price_int:,}원"
        except ValueError:
            price_int = 0
            price     = "0원"

        link = item.get("link", "")
        image = item.get("image", "")

        # productId 추출 (URL 기반)
        m = re.search(r"productId=(\d+)", link)
        prod_id = f"naver_api_{m.group(1)}" if m else f"naver_api_{hash(link)}"

        products.append({
            "product_id": prod_id,
            "name":       title,
            "price":      price,
            "price_int":  price_int,
            "status":     "판매중",
            "url":        link,
            "image_url":  image,
            "site_name":  site_name,
        })

    logger.info("[%s] Naver API 수집 완료: %d개", site_name, len(products))
    return products


# ── 공개 API ───────────────────────────────────────────────────────────────────

def get_naver_products(
    site_name:        str,
    page_url:         str,
    channel_base_url: str,
    search_query:     str = "",
) -> list[dict]:
    """
    상품 수집.
    NAVER_CLIENT_ID/SECRET 환경변수가 있으면 공식 API,
    없으면 HTML 파싱 방식 사용.
    """
    if _USE_OFFICIAL_API:
        logger.info("[%s] 공식 Naver Shopping API 사용", site_name)
        # 스토어별 검색어 설정 (공식 API는 전체 Naver 검색이므로 구체적 쿼리 필요)
        query = search_query or "포켓몬 확장팩"
        return _api_search_products(query, site_name, page_url)
    else:
        return _scrape_html(site_name, page_url, channel_base_url, search_query)
