import logging
import threading

import requests as http
from flask import Flask, jsonify, render_template
from apscheduler.schedulers.background import BackgroundScheduler

from config import PORT, CHECK_INTERVAL_MINUTES, DISCORD_WEBHOOK_URL, TARGET_URL
from database import (
    init_db, get_product_count, get_all_products,
    get_last_check, upsert_product, log_check,
)
from scraper import get_products

# ── 로깅 (Railway는 stdout 수집) ───────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
_check_lock = threading.Lock()


# ── Discord 알림 ───────────────────────────────────────────────────────────────

def send_discord(product: dict, badge: str):
    if badge == "new":
        title, color = "🆕 신규 상품 등록!", 0xE53935
    else:
        title, color = "🔄 재입고 감지!", 0x1E88E5

    embed = {
        "title": title,
        "url": product.get("url", TARGET_URL),
        "description": f"**{product['name']}**",
        "color": color,
        "fields": [
            {"name": "💰 가격", "value": product.get("price", "—"), "inline": True},
            {"name": "📦 상태", "value": product.get("status", "판매중"), "inline": True},
        ],
    }
    if product.get("image_url"):
        embed["thumbnail"] = {"url": product["image_url"]}

    try:
        r = http.post(
            DISCORD_WEBHOOK_URL,
            json={"embeds": [embed]},
            timeout=10,
        )
        r.raise_for_status()
        logger.info("Discord 전송 완료: %s", product["name"])
    except Exception as e:
        logger.error("Discord 전송 실패: %s", e)


# ── 모니터링 체크 ──────────────────────────────────────────────────────────────

def run_check():
    if not _check_lock.acquire(blocking=False):
        logger.info("체크 진행 중, 건너뜀")
        return
    try:
        logger.info("=== 상품 체크 시작 ===")
        is_first_run = get_product_count() == 0

        products, method = get_products()

        if not products:
            log_check(False, f"상품 없음 (방식: {method})")
            logger.warning("상품을 찾지 못했습니다.")
            return

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
                logger.error("상품 처리 오류 (%s): %s", product.get("name", "?"), e)

        if is_first_run:
            msg = f"초기 로드 완료: {len(products)}개 상품 (방식: {method})"
        else:
            msg = (
                f"OK: 총 {len(products)}개 확인, "
                f"신규 {new_cnt}개, 재입고 {restocked_cnt}개 (방식: {method})"
            )
        log_check(True, msg)
        logger.info(msg)

    except Exception as e:
        logger.error("체크 실패: %s", e, exc_info=True)
        log_check(False, str(e))
    finally:
        _check_lock.release()


# ── Flask 라우트 ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/products")
def api_products():
    return jsonify(
        products=get_all_products(),
        last_check=get_last_check(),
    )


@app.route("/api/check", methods=["POST"])
def api_check():
    t = threading.Thread(target=run_check, daemon=True)
    t.start()
    return jsonify(status="started", message="체크를 시작했습니다.")


# ── 시작 (gunicorn import 시에도 실행됨) ─────────────────────────────────────

def _startup():
    init_db()
    logger.info("초기 상품 체크 실행 중...")
    threading.Thread(target=run_check, daemon=True).start()

    scheduler = BackgroundScheduler(timezone="Asia/Seoul")
    scheduler.add_job(
        run_check, "interval",
        minutes=CHECK_INTERVAL_MINUTES,
        id="check_job",
    )
    scheduler.start()
    logger.info("스케줄러 시작: %d분 간격", CHECK_INTERVAL_MINUTES)


_startup()

if __name__ == "__main__":
    logger.info("서버 시작: http://localhost:%d", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
