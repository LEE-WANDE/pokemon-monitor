"""
네이버 스마트스토어 / 브랜드스토어 상품 스크래퍼

방식: curl_cffi 로 브라우저 TLS 지문 모방 → HTML 내 window.__PRELOADED_STATE__ 파싱
페이지네이션: ?cp={page} 파라미터
"""

import json
import logging
import re

from curl_cffi import requests as creq

logger = logging.getLogger(__name__)

# ── 세션 (프로세스 당 1개, chrome 지문 모방) ────────────────────────────────────
_SESS = creq.Session(impersonate="chrome120")

_PAGE_SIZE = 40  # 네이버 스토어 기본 페이지 크기

_SOLD_OUT_STATUSES = {"OUTOFSTOCK", "SOLD_OUT", "SUSPENSION", "CLOSE", "DELETED"}

# ── 상태 키 우선 순위 (카테고리 → 검색 → 기타) ────────────────────────────────
_STATE_KEYS = ["categoryProducts", "searchProducts", "productSearch", "productList"]


# ── 내부 헬퍼 ─────────────────────────────────────────────────────────────────

def _extract_state(html: str) -> dict:
    m = re.search(r"window\.__PRELOADED_STATE__=(\{.*)", html)
    if not m:
        return {}
    raw = m.group(1)
    end = raw.find("</script>")
    if end > 0:
        raw = raw[:end]
    # JS 리터럴 → 유효 JSON
    raw = re.sub(r"\bundefined\b", "null", raw)
    raw = re.sub(r"\bInfinity\b",  "null", raw)
    raw = re.sub(r"\bNaN\b",       "null", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("__PRELOADED_STATE__ JSON 파싱 실패: %s", e)
        return {}


def _find_products_section(state: dict) -> tuple[list, dict]:
    """state 에서 simpleProducts 리스트와 메타데이터(totalCount 등) 반환.

    카테고리: state[key].simpleProducts
    검색:     state[key][variant].simpleProducts  (A/B 테스트 variant 구조)
    """
    for key in _STATE_KEYS + ["keywordSearch"]:
        section = state.get(key)
        if not isinstance(section, dict):
            continue
        # 직접 simpleProducts 있는 경우
        products = section.get("simpleProducts", [])
        if isinstance(products, list) and products:
            return products, section
        # variant(A/B) 구조: {"A": {"simpleProducts": [...], "totalCount": N}}
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
        price = f"{price_int:,}원"
    except (TypeError, ValueError):
        price_int = 0
        price = "0원"

    status_type = item.get("productStatusType", "")
    status = "품절" if status_type in _SOLD_OUT_STATUSES else "판매중"

    channel    = item.get("channel") or {}
    channel_uid = channel.get("channelUid", "")
    product_url = (
        f"{channel_base_url}/{channel_uid}/products/{product_no}"
        if channel_uid else ""
    )

    image_url = item.get("representativeImageUrl") or ""

    return {
        "product_id": f"naver_{channel_uid}_{product_no}",
        "name":       name,
        "price":      price,
        "price_int":  price_int,
        "status":     status,
        "url":        product_url,
        "image_url":  image_url,
        "site_name":  site_name,
    }


def _fetch_html(url: str, referer: str) -> str:
    headers = {
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9",
        "Referer":         referer,
    }
    r = _SESS.get(url, headers=headers, timeout=25)
    r.raise_for_status()
    return r.text


# ── 공개 API ───────────────────────────────────────────────────────────────────

def get_naver_products(
    site_name:        str,
    page_url:         str,
    channel_base_url: str,
    search_query:     str = "",
) -> list[dict]:
    """
    네이버 스토어 상품 목록 반환.

    Args:
        site_name        : 표시할 사이트 이름 (예: "네이버 브랜드스토어")
        page_url         : 카테고리/검색 URL (cp 파라미터 제외)
        channel_base_url : "https://smartstore.naver.com" 또는 "https://brand.naver.com"
        search_query     : 검색어 (검색 URL일 때 사용, 인코딩 전 원문)
    """
    all_products: list[dict] = []
    page = 1

    while True:
        if search_query:
            url = f"{page_url}?q={search_query}&cp={page}"
        else:
            url = f"{page_url}?cp={page}"

        logger.info("[%s] 페이지 %d 수집 중: %s", site_name, page, url)
        try:
            html = _fetch_html(url, page_url)
        except Exception as e:
            logger.error("[%s] 페이지 %d 수집 실패: %s", site_name, page, e)
            break

        state = _extract_state(html)
        if not state:
            logger.warning("[%s] __PRELOADED_STATE__ 없음 (페이지 %d)", site_name, page)
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

    # product_id 중복 제거 (마지막 항목 유지)
    seen: dict[str, dict] = {}
    for p in all_products:
        seen[p["product_id"]] = p

    logger.info("[%s] 수집 완료: %d개", site_name, len(seen))
    return list(seen.values())
