"""
HTML 스크래퍼 모음 (requests + BeautifulSoup)

카드마니아  : Godomall — div.goods_list li 구조, 단일 페이지
TCG박스     : Cafe24  — ul.prdList li.xans-record- 구조, 복수 페이지
옥션        : div.prod_list ul.type1 li — onclick setItemHistory 파싱
G마켓       : div.prod_list ul.brand_default li — onclick setItemHistory 파싱
SSG         : Next.js __NEXT_DATA__ JSON (best.ssg) — 단일 페이지(24개)
11번가      : JS 렌더링 전용(AJAX totalCount=0) → 미지원

공통 품절 감지:
  카드마니아/TCG박스: img[alt="품절"]
  옥션/G마켓: 품절 상품은 목록에서 제외됨 (없으면 판매중으로 간주)
  SSG: isDisableCartButton=True or soldOutMessage 비어 있지 않음
"""

import json
import logging
import random
import re
import time

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}


def _fetch(url: str, referer: str = "") -> BeautifulSoup:
    hdrs = dict(_HEADERS)
    if referer:
        hdrs["Referer"] = referer
    r = requests.get(url, headers=hdrs, timeout=15)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


# ── 카드마니아 ─────────────────────────────────────────────────────────────────

_CM_BASE = "https://www.cardmania2021.com"
_CM_LIST = f"{_CM_BASE}/goods/goods_list.php?cateCd=001001&sort=&pageNum=40"


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

        img     = item.select_one("img.middle")
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

        # 현재 페이지 이후 숫자 링크가 있으면 계속, 없으면 종료
        page_links = soup.select("div.pagination a[href]")
        next_pages = [
            int(a.get_text(strip=True))
            for a in page_links
            if a.get_text(strip=True).isdigit()
               and int(a.get_text(strip=True)) > page
        ]
        if not next_pages:
            break
        page += 1
        time.sleep(random.uniform(1.0, 2.0))

    seen = {p["product_id"]: p for p in all_products}
    logger.info("[카드마니아] 수집 완료: %d개", len(seen))
    return list(seen.values())


# ── TCG박스 ───────────────────────────────────────────────────────────────────

_TB_BASE = "https://tcgbox.co.kr"
_TB_CAT  = f"{_TB_BASE}/category/%ED%99%95%EC%9E%A5%ED%8C%A9-BOX/191/"


def _parse_tcgbox_page(items) -> list[dict]:
    products = []
    for item in items:
        pid = item.get("id", "").replace("anchorBoxId_", "")
        if not pid:
            continue

        # 이름: displaynone 아닌 span 중 자식 span 없는 실제 텍스트
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
            # /product/{slug}/{id}/category/.../display/.../ → /product/{slug}/{id}/
            _m = re.match(r"(/product/[^/]+/\d+)", raw_href)
            url = _TB_BASE + (_m.group(1) + "/" if _m else raw_href)
        else:
            url = ""

        img    = item.select_one(f"img#eListPrdImage{pid}_1")
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

        # 현재 페이지 이후 숫자 링크 확인
        paging_links = soup.select(".ec-base-paginate a, .xans-product-normalpaging a")
        next_pages = [
            int(a.get_text(strip=True))
            for a in paging_links
            if a.get_text(strip=True).isdigit()
               and int(a.get_text(strip=True)) > page
        ]
        if not next_pages:
            break
        page += 1
        time.sleep(random.uniform(1.0, 2.0))

    seen = {p["product_id"]: p for p in all_products}
    logger.info("[TCG박스] 수집 완료: %d개", len(seen))
    return list(seen.values())


# ── 옥션 ──────────────────────────────────────────────────────────────────────
# 구조: div.prod_list > ul.type1 > li.normal.{itemno}
# 데이터: onclick setItemHistory(itemno, name, price, imgUrl, ...)
# 페이지: 단일 페이지(~30개), 페이지네이션 없음
# 품절: 목록에서 자동 제외되므로 표시된 상품은 모두 판매중

_AU_URL = "https://stores.auction.co.kr/pokemoncardgame"

_RE_SET_ITEM = re.compile(
    r"setItemHistory\('([^']+)',\s*'([^']+)',\s*'([^']+)',\s*'([^']*)'",
)


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


# ── G마켓 ─────────────────────────────────────────────────────────────────────
# 구조: div.prod_list > ul.brand_default > li > p.prd_img > a[onclick]
# 데이터: onclick setItemHistory(goodsCode, name, price, imgUrl, ...)
# 페이지: 단일 페이지(~15개), 페이지네이션 없음
# 품절: 목록에서 자동 제외

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


# ── SSG ───────────────────────────────────────────────────────────────────────
# 구조: Next.js __NEXT_DATA__ JSON → queries[*].state.data.initialPage.resultList
# 페이지: best.ssg 첫 페이지(24개/48개) — moreUrl은 내부 API로 외부 접근 불가
# 품절: isDisableCartButton=True 또는 soldOutMessage 비어 있지 않음

_SSG_URL  = "https://www.ssg.com/sellerhome/pokemontcg/best.ssg"
_SSG_BASE = "https://www.ssg.com"
_RE_PRICE = re.compile(r"[^\d]")


def _parse_ssg_price(text: str) -> int:
    num = _RE_PRICE.sub("", text or "")
    return int(num) if num else 0


def get_ssg_products() -> list[dict]:
    hdrs = dict(_HEADERS)
    hdrs["Referer"] = _SSG_BASE + "/"
    raw = requests.get(_SSG_URL, headers=hdrs, timeout=15)
    raw.raise_for_status()
    raw_html = raw.text

    nd_m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        raw_html,
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
            item_id  = item.get("itemId") or item.get("custKey", "")
            name     = (item.get("itemName") or "").strip()
            if not name or not item_id:
                continue

            price_info = item.get("priceInfo", {}) or {}
            price_str  = (
                item.get("finalPrice")
                or price_info.get("primaryPrice")
                or "0"
            )
            price_int = _parse_ssg_price(str(price_str))

            is_soldout = (
                item.get("isDisableCartButton") is True
                or bool(item.get("soldOutMessage"))
            )
            status = "품절" if is_soldout else "판매중"

            img_url  = item.get("itemImgUrl", "")
            item_url = item.get("itemUrl", "") or item.get("itemDetailLink", "")

            products.append({
                "product_id": f"ssg_{item_id}",
                "name":       name,
                "price":      f"{price_int:,}원" if price_int else price_str,
                "price_int":  price_int,
                "status":     status,
                "url":        item_url,
                "image_url":  img_url,
                "site_name":  "SSG",
            })
        break  # 첫 번째 resultList만 사용

    seen = {p["product_id"]: p for p in products}
    logger.info("[SSG] 수집 완료: %d개", len(seen))
    return list(seen.values())
