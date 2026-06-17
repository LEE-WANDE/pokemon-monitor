"""
네이버 스마트스토어 / 브랜드스토어 상품 스크래퍼

방식: curl_cffi 브라우저 TLS 지문 모방 → HTML 내 window.__PRELOADED_STATE__ 파싱
429 발생 시: 지수 백오프 최대 3회 재시도 후 예외 발생 (호출 측에서 로그 처리)
"""

import json
import logging
import random
import re
import time
from urllib.parse import quote, urlparse

from curl_cffi import requests as creq

logger = logging.getLogger(__name__)

# ── 세션 (프로세스 당 1개, Chrome 지문 모방) ────────────────────────────────
_SESS           = creq.Session(impersonate="chrome120")
_SESSION_WARMED = False

_PAGE_SIZE = 40

_SOLD_OUT_STATUSES = {"OUTOFSTOCK", "SOLD_OUT", "SUSPENSION", "CLOSE", "DELETED"}
_STATE_KEYS        = ["categoryProducts", "searchProducts", "productSearch", "productList"]

_MAX_RETRIES  = 3
_RETRY_BASE_S = 8


# ── 세션 워밍업 ───────────────────────────────────────────────────────────────

def _warm_up():
    global _SESSION_WARMED
    if _SESSION_WARMED:
        return
    _SESSION_WARMED = True  # 먼저 True로 설정해 재진입 방지
    try:
        _SESS.get(
            "https://www.naver.com/",
            headers={"Accept": "text/html,*/*;q=0.8", "Accept-Language": "ko-KR,ko;q=0.9"},
            timeout=10,
        )
        time.sleep(random.uniform(0.8, 1.5))
        _SESS.get(
            "https://shopping.naver.com/",
            headers={"Accept": "text/html,*/*;q=0.8", "Accept-Language": "ko-KR,ko;q=0.9"},
            timeout=10,
        )
        logger.info("Naver 세션 워밍업 완료")
    except Exception as e:
        logger.warning("세션 워밍업 실패(계속 진행): %s", e)


# ── HTTP 헬퍼 ─────────────────────────────────────────────────────────────────

def _browser_headers(referer: str) -> dict:
    return {
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,"
            "application/signed-exchange;v=b3;q=0.7"
        ),
        "Accept-Encoding":           "gzip, deflate, br",
        "Accept-Language":           "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control":             "max-age=0",
        "Referer":                   referer,
        "Sec-Ch-Ua":                 '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        "Sec-Ch-Ua-Mobile":          "?0",
        "Sec-Ch-Ua-Platform":        '"Windows"',
        "Sec-Fetch-Dest":            "document",
        "Sec-Fetch-Mode":            "navigate",
        "Sec-Fetch-Site":            "same-origin",
        "Sec-Fetch-User":            "?1",
        "Upgrade-Insecure-Requests": "1",
    }


def _fetch_html(url: str, referer: str) -> str:
    """브라우저 헤더로 HTML 취득. 429 시 지수 백오프 재시도."""
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
            return r.text

        except creq.exceptions.RequestsError as e:
            if attempt < _MAX_RETRIES - 1:
                wait = _RETRY_BASE_S * (2 ** attempt)
                logger.warning("요청 오류 %s — %ds 후 재시도", e, wait)
                time.sleep(wait)
            else:
                raise

    raise RuntimeError(f"429 최대 재시도 초과: {url}")


# ── 파싱 헬퍼 ─────────────────────────────────────────────────────────────────

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
    """state 에서 simpleProducts 리스트와 메타데이터 반환.
    카테고리: state[key].simpleProducts
    검색:     state[key][variant].simpleProducts  (keywordSearch.A 구조)
    """
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


def _parse_product(item: dict, site_name: str, channel_base_url: str, channel_path: str) -> dict | None:
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
    # channel_path는 page_url에서 추출한 스토어 경로(예: "pokemon", "pokemontcg")
    # channelUid는 내부 해시값이라 URL에 사용하면 404 발생
    product_url = (
        f"{channel_base_url}/{channel_path}/products/{product_no}"
        if channel_path else ""
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


# ── 공개 API ───────────────────────────────────────────────────────────────────

def get_naver_products(
    site_name:        str,
    page_url:         str,
    channel_base_url: str,
    search_query:     str = "",
) -> list[dict]:
    """
    네이버 스토어 상품 목록 반환.
    429 포함 오류 발생 시 예외를 그대로 올림 → 호출 측에서 로그 처리.
    """
    all_products: list[dict] = []
    page = 1

    # page_url 경로 첫 세그먼트를 스토어 식별자로 사용
    # 예: brand.naver.com/pokemon/category/... → "pokemon"
    #     smartstore.naver.com/pokemontcg/category/... → "pokemontcg"
    _path_parts  = urlparse(page_url).path.strip("/").split("/")
    channel_path = _path_parts[0] if _path_parts else ""

    while True:
        url = (
            f"{page_url}?q={quote(search_query)}&cp={page}"
            if search_query
            else f"{page_url}?cp={page}"
        )

        logger.info("[%s] HTML 수집 페이지 %d: %s", site_name, page, url)
        html = _fetch_html(url, page_url)  # 실패 시 예외 발생

        state = _extract_state(html)
        if not state:
            logger.warning("[%s] __PRELOADED_STATE__ 없음", site_name)
            break

        products_raw, section_meta = _find_products_section(state)
        if not products_raw:
            logger.warning("[%s] 상품 없음 (페이지 %d)", site_name, page)
            break

        for item in products_raw:
            parsed = _parse_product(item, site_name, channel_base_url, channel_path)
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

    logger.info("[%s] 수집 완료: %d개", site_name, len(seen))
    return list(seen.values())
