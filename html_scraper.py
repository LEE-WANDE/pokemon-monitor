"""
카드마니아, TCG박스 HTML 스크래퍼 (requests + BeautifulSoup)

카드마니아  : Godomall — div.goods_list li 구조, 단일 페이지(현재 29개)
TCG박스     : Cafe24  — ul.prdList li.xans-record- 구조, 복수 페이지
공통        : 품절 = img[alt="품절"] 존재 여부
"""

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
        url = (_TB_BASE + href_el["href"]) if href_el else ""

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
