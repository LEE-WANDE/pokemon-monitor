import sqlite3
import threading
from config import DB_PATH

_lock = threading.Lock()


def _connect():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS products (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id   TEXT    NOT NULL UNIQUE,
                name         TEXT    NOT NULL,
                price        TEXT    DEFAULT '',
                status       TEXT    DEFAULT '판매중',
                url          TEXT    DEFAULT '',
                image_url    TEXT    DEFAULT '',
                first_seen   TEXT    DEFAULT (datetime('now', 'localtime')),
                last_checked TEXT    DEFAULT (datetime('now', 'localtime')),
                is_new       INTEGER DEFAULT 0,
                is_restocked INTEGER DEFAULT 0,
                detected_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS check_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                checked_at TEXT    DEFAULT (datetime('now', 'localtime')),
                success    INTEGER DEFAULT 1,
                message    TEXT    DEFAULT ''
            );
        """)


def get_product_count():
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]


def get_all_products():
    with _connect() as conn:
        rows = conn.execute("""
            SELECT * FROM products
            ORDER BY
                CASE WHEN is_new = 1 OR is_restocked = 1 THEN 0 ELSE 1 END,
                detected_at DESC,
                first_seen  DESC
        """).fetchall()
        return [dict(r) for r in rows]


def get_last_check():
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM check_log ORDER BY checked_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def upsert_product(product: dict):
    """Insert or update a product. Returns (is_new, is_restocked)."""
    with _lock:
        with _connect() as conn:
            existing = conn.execute(
                "SELECT * FROM products WHERE product_id = ?",
                (product["product_id"],),
            ).fetchone()

            if existing is None:
                conn.execute(
                    """
                    INSERT INTO products
                        (product_id, name, price, status, url, image_url,
                         is_new, is_restocked, detected_at)
                    VALUES (?, ?, ?, ?, ?, ?, 1, 0, datetime('now', 'localtime'))
                    """,
                    (
                        product["product_id"],
                        product["name"],
                        product.get("price", ""),
                        product.get("status", "판매중"),
                        product.get("url", ""),
                        product.get("image_url", ""),
                    ),
                )
                return True, False

            existing = dict(existing)
            was_sold_out = existing["status"] == "품절"
            now_available = product.get("status", "판매중") == "판매중"
            is_restocked = was_sold_out and now_available

            conn.execute(
                """
                UPDATE products SET
                    name         = ?,
                    price        = ?,
                    status       = ?,
                    url          = ?,
                    image_url    = ?,
                    last_checked = datetime('now', 'localtime'),
                    is_new       = 0,
                    is_restocked = ?,
                    detected_at  = CASE WHEN ? = 1
                                        THEN datetime('now', 'localtime')
                                        ELSE detected_at
                                   END
                WHERE product_id = ?
                """,
                (
                    product["name"],
                    product.get("price", ""),
                    product.get("status", "판매중"),
                    product.get("url", ""),
                    product.get("image_url", ""),
                    1 if is_restocked else 0,
                    1 if is_restocked else 0,
                    product["product_id"],
                ),
            )
            return False, is_restocked


def log_check(success: bool, message: str = ""):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO check_log (success, message) VALUES (?, ?)",
            (1 if success else 0, message),
        )
