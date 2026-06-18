import os

DISCORD_WEBHOOK_URL = os.environ.get(
    "DISCORD_WEBHOOK_URL",
    "https://discord.com/api/webhooks/1514563698491002942/veUZ8AefhAd90cc8N8GGkuyJ4QmDiApf5e26JmZ7WR1ZbXplqhWOpBrX_W2OsvH29pfd"
)

TARGET_URL = os.environ.get(
    "TARGET_URL",
    "https://www.pokemonstore.co.kr/pages/product/product-list.html?categoryNo=488359"
)

BASE_URL = "https://www.pokemonstore.co.kr"
CATEGORY_NO = "488359"

CHECK_INTERVAL_MINUTES       = int(os.environ.get("CHECK_INTERVAL",       "20"))
NAVER_CHECK_INTERVAL_MINUTES = int(os.environ.get("NAVER_CHECK_INTERVAL", "30"))
PORT = int(os.environ.get("PORT", "8080"))
# Railway 환경(RAILWAY_ENVIRONMENT 존재)이면 /data 볼륨 경로 사용,
# 로컬 개발이면 현재 디렉토리의 pokemon.db 사용
_on_railway = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_SERVICE_NAME"))
DB_PATH = os.environ.get("DB_PATH", "/data/pokemon.db" if _on_railway else "pokemon.db")
LOCK_FILE = "pokemon_monitor.lock"
