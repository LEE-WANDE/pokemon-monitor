import logging
import threading

import requests as http
from flask import Flask, jsonify, render_template
from apscheduler.schedulers.background import BackgroundScheduler

from config import (
    PORT, CHECK_INTERVAL_MINUTES, NAVER_CHECK_INTERVAL_MINUTES,
    DISCORD_WEBHOOK_URL, TARGET_URL,
)
from database import (
    init_db, get_product_count, get_all_products,
    get_last_check, upsert_product, log_check,
)
from scraper import get_pokemonstore_products, get_naver_products, get_products

# ── 로깅 ──────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# 소스별 독립 락 — 포켓몬스토어와 네이버가 동시에 실행돼도 무방
_ps_lock    = threading.Lock()
_naver_lock = threading.Lock()
_manual_lock = threading.Lock()  # /api/check 수동 체크용


# ── Discord 알림 ───────────────────────────────────────────────────────────────

def send_discord(product: dict, badge: str):
    site_name  = product.get("site_name", "")
    site_label = f"[{site_name}] " if site_name else ""

    if badge == "new":
        title, color = f"🆕 신규 상품 등록! {site_label}", 0xE53935
    else:
        title, color = f"🔄 재입고 감지! {site_label}", 0x1E88E5

    embed = {
        "title":       title,
        "url":         product.get("url", TARGET_URL),
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
        r = http.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
        r.raise_for_status()
        logger.info("Discord 전송 완료: %s", product["name"])
    except Exception as e:
        logger.error("Discord 전송 실패: %s", e)


# ── 공통 상품 처리 ─────────────────────────────────────────────────────────────

def _process(products: list[dict], is_first_run: bool, tag: str) -> tuple[int, int]:
    new_cnt = restocked_cnt = 0
    for product in products:
        try:
            is_new, is_restocked = upsert_product(product)
            if is_first_run:
                continue
            if is_new:
                new_cnt += 1
                send_discord(product, "new")
            elif is_restocked:
                restocked_cnt += 1
                send_discord(product, "restocked")
        except Exception as e:
            logger.error("[%s] 상품 처리 오류 (%s): %s", tag, product.get("name", "?"), e)
    return new_cnt, restocked_cnt


# ── 포켓몬스토어 체크 (20분 주기) ─────────────────────────────────────────────

def run_pokemonstore_check():
    if not _ps_lock.acquire(blocking=False):
        logger.info("[포켓몬스토어] 이미 실행 중, 건너뜀")
        return
    try:
        logger.info("=== 포켓몬스토어 체크 시작 ===")
        is_first_run = get_product_count() == 0

        products, method = get_pokemonstore_products()

        if not products:
            log_check(False, f"[포켓몬스토어] 상품 없음 ({method})")
            logger.warning("[포켓몬스토어] 상품을 찾지 못했습니다.")
            return

        new_cnt, restocked_cnt = _process(products, is_first_run, "포켓몬스토어")

        if is_first_run:
            msg = f"[포켓몬스토어] 초기 로드: {len(products)}개 ({method})"
        else:
            msg = f"[포켓몬스토어] {len(products)}개 확인, 신규 {new_cnt}개, 재입고 {restocked_cnt}개"
        log_check(True, msg)
        logger.info(msg)

    except Exception as e:
        logger.error("[포켓몬스토어] 체크 실패: %s", e, exc_info=True)
        log_check(False, f"[포켓몬스토어] {e}")
    finally:
        _ps_lock.release()


# ── 네이버 체크 (60분 주기) ───────────────────────────────────────────────────

def run_naver_check():
    if not _naver_lock.acquire(blocking=False):
        logger.info("[네이버] 이미 실행 중, 건너뜀")
        return
    try:
        logger.info("=== 네이버 체크 시작 ===")
        is_first_run = get_product_count() == 0

        products, method = get_naver_products()

        # 429 등으로 전체 실패해도 프로그램은 계속 동작
        if not products:
            log_check(False, f"[네이버] 상품 없음 또는 수집 실패 — 다음 주기에 재시도 ({method})")
            logger.warning("[네이버] 상품 없음 또는 수집 실패. 60분 후 재시도.")
            return

        new_cnt, restocked_cnt = _process(products, is_first_run, "네이버")

        if is_first_run:
            msg = f"[네이버] 초기 로드: {len(products)}개 ({method})"
        else:
            msg = f"[네이버] {len(products)}개 확인, 신규 {new_cnt}개, 재입고 {restocked_cnt}개"
        log_check(True, msg)
        logger.info(msg)

    except Exception as e:
        # 429 포함 모든 예외를 조용히 처리 — 프로그램 계속 동작
        logger.error("[네이버] 체크 실패 (다음 주기에 재시도): %s", e, exc_info=True)
        log_check(False, f"[네이버] {e}")
    finally:
        _naver_lock.release()


# ── 수동 체크 (/api/check) ────────────────────────────────────────────────────

def run_check():
    """포켓몬스토어 + 네이버 전체 체크. 대시보드 '지금 체크' 버튼용."""
    if not _manual_lock.acquire(blocking=False):
        logger.info("[수동 체크] 이미 실행 중, 건너뜀")
        return
    try:
        logger.info("=== 수동 전체 체크 시작 ===")
        is_first_run = get_product_count() == 0

        products, method = get_products()

        if not products:
            log_check(False, f"상품 없음 ({method})")
            return

        new_cnt, restocked_cnt = _process(products, is_first_run, "수동")

        if is_first_run:
            msg = f"초기 로드: {len(products)}개 ({method})"
        else:
            msg = f"수동 체크: {len(products)}개, 신규 {new_cnt}개, 재입고 {restocked_cnt}개"
        log_check(True, msg)
        logger.info(msg)

    except Exception as e:
        logger.error("수동 체크 실패: %s", e, exc_info=True)
        log_check(False, str(e))
    finally:
        _manual_lock.release()


# ── Flask 라우트 ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/products")
def api_products():
    return jsonify(products=get_all_products(), last_check=get_last_check())


@app.route("/api/check", methods=["POST"])
def api_check():
    threading.Thread(target=run_check, daemon=True).start()
    return jsonify(status="started", message="체크를 시작했습니다.")


# ── 시작 ──────────────────────────────────────────────────────────────────────

def _startup():
    init_db()

    # 시작 시 전체 초기 로드 (포켓몬스토어 + 네이버 동시 — is_first_run 일괄 처리)
    logger.info("초기 전체 상품 체크 실행 중…")
    threading.Thread(target=run_check, daemon=True).start()

    scheduler = BackgroundScheduler(timezone="Asia/Seoul")

    # 포켓몬스토어: 20분 (기존 유지)
    scheduler.add_job(
        run_pokemonstore_check, "interval",
        minutes=CHECK_INTERVAL_MINUTES,
        id="ps_job",
    )

    # 네이버 3개 사이트: 60분 (부하 분산 + 429 완화)
    scheduler.add_job(
        run_naver_check, "interval",
        minutes=NAVER_CHECK_INTERVAL_MINUTES,
        id="naver_job",
    )

    scheduler.start()
    logger.info(
        "스케줄러 시작 — 포켓몬스토어: %d분, 네이버: %d분",
        CHECK_INTERVAL_MINUTES, NAVER_CHECK_INTERVAL_MINUTES,
    )


_startup()

if __name__ == "__main__":
    logger.info("서버 시작: http://localhost:%d", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
