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

CHECK_INTERVAL_MINUTES = int(os.environ.get("CHECK_INTERVAL", "10"))
PORT = int(os.environ.get("PORT", "8080"))
DB_PATH = os.environ.get("DB_PATH", "pokemon.db")
LOCK_FILE = "pokemon_monitor.lock"
