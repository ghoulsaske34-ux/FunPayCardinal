from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import queue
import re
import secrets
import shutil
import sqlite3
import threading
import time
import uuid
import zipfile
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import requests
import telebot
from telethon.sync import TelegramClient
from telethon import events, connection
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PasswordHashInvalidError,
)
from telethon.sessions import StringSession
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery, LabeledPrice, BotCommand

try:
    from tg_bot import CBT as CBT_FPC
except Exception:
    CBT_FPC = None

try:
    import phonenumbers
except Exception:
    phonenumbers = None

# --- метаданные плагина ---
NAME = "Telegram Account Shop"
VERSION = "2.1.0"
DESCRIPTION = "Telegram-магазин аккаунтов с автовыдачей SMS/кодов через Telethon"
CREDITS = "@ghoulsaske34"
UUID = "85d48499-79da-462e-af82-9feb2c399d7c"
SETTINGS_PAGE = True
BIND_TO_DELETE = None

# --- константы ---
CB = "tgshop:"
CB_ADMIN = "tgsa:"
STORAGE_DIR = Path("storage/cache/tg_account_shop")
SESSIONS_DIR = STORAGE_DIR / "sessions"
DB_FILE = STORAGE_DIR / "shop.db"
CONFIG_FILE = STORAGE_DIR / "config.json"
logger = logging.getLogger("telegram_account_shop")

LISTEN_MINUTES = 5
STATUS_AVAILABLE = "available"
STATUS_SOLD = "sold"
STATUS_LISTENING = "listening"

DEFAULT_STARS_PER_RUB = 1.3
DEFAULT_USDT_TO_RUB = 90.0
DEFAULT_TON_TO_RUB = 300.0

PLATEGA_BASE = "https://app.platega.io"
PLATEGA_PAYFORM = "https://pay.platega.io"
PLATEGA_SBP = 2
PLATEGA_SIG_KEY = "9mp^mz)]{[%nf|j6ga]k}t|?1siul(68"
PLATEGA_SIG_IV = "3A6QgvggIDssBA=="

# --- утилиты ---
def _to_dec(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0.00")

def _money_round(value: Decimal | Any) -> Decimal:
    return _to_dec(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

def _money_str(value: Decimal | Any) -> str:
    return str(_money_round(value))

def _now() -> float:
    return time.time()

def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")

def _extract_codes(text: str | None) -> list[str]:
    if not text:
        return []
    return re.findall(r"\b\d{4,6}\b", text)

_COUNTRY_FALLBACK = {
    "1": "США/Канада",
    "44": "Великобритания",
    "49": "Германия",
    "7": "Россия",
    "77": "Казахстан",
    "374": "Армения",
    "375": "Беларусь",
    "380": "Украина",
    "371": "Латвия",
    "370": "Литва",
    "372": "Эстония",
    "48": "Польша",
    "33": "Франция",
    "39": "Италия",
    "34": "Испания",
    "90": "Турция",
    "81": "Япония",
    "82": "Южная Корея",
    "86": "Китай",
    "91": "Индия",
    "62": "Индонезия",
    "60": "Малайзия",
    "65": "Сингапур",
    "66": "Таиланд",
    "84": "Вьетнам",
    "63": "Филиппины",
    "55": "Бразилия",
    "52": "Мексика",
    "54": "Аргентина",
    "971": "ОАЭ",
    "966": "Саудовская Аравия",
    "20": "Египет",
    "27": "ЮАР",
    "31": "Нидерланды",
    "41": "Швейцария",
    "43": "Австрия",
    "46": "Швеция",
    "47": "Норвегия",
    "45": "Дания",
    "358": "Финляндия",
    "30": "Греция",
    "36": "Венгрия",
    "40": "Румыния",
    "385": "Хорватия",
    "386": "Словения",
    "420": "Чехия",
    "421": "Словакия",
}

# Build reverse map from localized country name to calling code / region.
_NAME_TO_CODE: dict[str, str] = {}
_NAME_TO_REGION: dict[str, str] = {}

def _build_country_meta_map() -> None:
    if not phonenumbers:
        return
    try:
        from phonenumbers.geocoder import description_for_number
        for region_tuple in phonenumbers.COUNTRY_CODE_TO_REGION_CODE.values():
            for region in region_tuple:
                try:
                    n = phonenumbers.example_number_for_type(region, phonenumbers.PhoneNumberType.MOBILE)
                    if not n:
                        continue
                    desc = description_for_number(n, "ru")
                    if not desc:
                        continue
                    cc = str(n.country_code)
                    if desc not in _NAME_TO_CODE:
                        _NAME_TO_CODE[desc] = cc
                        _NAME_TO_REGION[desc] = region
                except Exception:
                    pass
    except Exception:
        pass

_build_country_meta_map()

def _detect_country(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    if not digits:
        return "Другое"
    if phonenumbers:
        try:
            parsed = phonenumbers.parse("+" + digits, None)
            # get country name if available
            from phonenumbers.geocoder import description_for_number
            desc = description_for_number(parsed, "ru")
            if desc:
                return desc
        except Exception:
            pass
    for l in (5, 4, 3, 2, 1):
        prefix = digits[:l]
        if prefix in _COUNTRY_FALLBACK:
            return _COUNTRY_FALLBACK[prefix]
    return "Другое"

def _parse_proxy(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    text = text.strip()
    if text.lower() in ("нет", "не", "no", "none", "-", "пропустить"):
        return None
    # support t.me/proxy?server=...&port=...&secret=...
    m = re.search(r"[?&]server=([^&]+).*?port=(\d+).*?secret=([A-Za-z0-9+/=]+)", text)
    if m:
        return {"scheme": "mtproto", "hostname": m.group(1), "port": int(m.group(2)), "secret": m.group(3)}
    # mtproto://server:port/secret
    m = re.match(r"mtproto://([^:/]+):(\d+)(?:/([A-Za-z0-9+/=]+))?", text, re.I)
    if m:
        return {"scheme": "mtproto", "hostname": m.group(1), "port": int(m.group(2)), "secret": m.group(3) or ""}
    # socks5://user:pass@host:port, http://host:port, etc.
    m = re.match(r"(socks4|socks5|http|https)://(?:(?:([^:@]+):([^:@]+))@)?([^:/\s]+):(\d+)", text, re.I)
    if m:
        return {
            "scheme": m.group(1).lower().replace("https", "http"),
            "username": m.group(2) or None,
            "password": m.group(3) or None,
            "hostname": m.group(4),
            "port": int(m.group(5)),
        }
    # host:port scheme
    m = re.match(r"(socks4|socks5|http)\s+([^:/\s]+):(\d+)", text, re.I)
    if m:
        return {"scheme": m.group(1).lower(), "hostname": m.group(2), "port": int(m.group(3)), "username": None, "password": None}
    # IPv4 host:port without scheme -> assume socks5
    m = re.match(r"([^:/\s]+):(\d+)", text)
    if m:
        return {"scheme": "socks5", "hostname": m.group(1), "port": int(m.group(2)), "username": None, "password": None}
    return None

def _country_meta(name: str) -> tuple[str, str]:
    """Return (phone_code, flag_emoji) for a country/category name."""
    code = _NAME_TO_CODE.get(name, "")
    region = _NAME_TO_REGION.get(name, "")
    if code and region:
        flag = "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in region.upper())
        return f"+{code}", flag
    if code:
        return f"+{code}", ""
    return "", ""

# --- хранилище ---
class AccountStorage:
    def __init__(self) -> None:
        STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._migrate()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _migrate(self) -> None:
        with self._lock, self._conn() as conn:
            conn.executescript(
                """
                PRAGMA user_version = 3;
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    balance REAL DEFAULT 0,
                    total_spent REAL DEFAULT 0,
                    is_admin INTEGER DEFAULT 0,
                    created_at REAL
                );
                CREATE TABLE IF NOT EXISTS categories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    description TEXT,
                    price REAL NOT NULL DEFAULT 0,
                    is_active INTEGER DEFAULT 1,
                    sort_order INTEGER DEFAULT 0,
                    created_at REAL
                );
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category_id INTEGER NOT NULL,
                    phone TEXT NOT NULL,
                    session_string TEXT NOT NULL,
                    proxy TEXT,
                    status TEXT DEFAULT 'available',
                    buyer_id INTEGER,
                    purchased_at REAL,
                    expires_at REAL,
                    last_code TEXT,
                    last_code_at REAL,
                    created_at REAL
                );
                CREATE TABLE IF NOT EXISTS purchases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    account_id INTEGER NOT NULL,
                    price REAL NOT NULL,
                    status TEXT DEFAULT 'completed',
                    created_at REAL,
                    delivered_at REAL
                );
                CREATE TABLE IF NOT EXISTS deposits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    method TEXT,
                    amount_rub REAL NOT NULL,
                    foreign_amount TEXT,
                    status TEXT DEFAULT 'pending',
                    payload TEXT,
                    external_id TEXT,
                    created_at REAL,
                    paid_at REAL
                );
                CREATE TABLE IF NOT EXISTS config (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                CREATE TABLE IF NOT EXISTS review_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    purchase_id INTEGER,
                    status TEXT DEFAULT 'pending',
                    text TEXT,
                    created_at REAL,
                    moderated_at REAL
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    text TEXT NOT NULL,
                    created_at REAL
                );
                """
            )
            # удалить устаревшие столбцы/таблицы при необходимости
            account_cols = [
                ("proxy", "ALTER TABLE accounts ADD COLUMN proxy TEXT"),
                ("buyer_id", "ALTER TABLE accounts ADD COLUMN buyer_id INTEGER"),
                ("purchased_at", "ALTER TABLE accounts ADD COLUMN purchased_at REAL"),
                ("expires_at", "ALTER TABLE accounts ADD COLUMN expires_at REAL"),
                ("last_code", "ALTER TABLE accounts ADD COLUMN last_code TEXT"),
                ("last_code_at", "ALTER TABLE accounts ADD COLUMN last_code_at REAL"),
            ]
            for col, sql in account_cols:
                try:
                    conn.execute(f"SELECT {col} FROM accounts LIMIT 1")
                except Exception:
                    conn.execute(sql)
            category_cols = [
                ("is_active", "ALTER TABLE categories ADD COLUMN is_active INTEGER DEFAULT 1"),
                ("sort_order", "ALTER TABLE categories ADD COLUMN sort_order INTEGER DEFAULT 0"),
            ]
            for col, sql in category_cols:
                try:
                    conn.execute(f"SELECT {col} FROM categories LIMIT 1")
                except Exception:
                    conn.execute(sql)
            try:
                conn.execute("SELECT funpay_product FROM categories LIMIT 1")
                conn.execute("DROP TABLE categories")
                conn.executescript("""
                    CREATE TABLE categories (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE,
                        description TEXT,
                        price REAL NOT NULL DEFAULT 0,
                        is_active INTEGER DEFAULT 1,
                        sort_order INTEGER DEFAULT 0,
                        created_at REAL
                    );
                """)
            except Exception:
                pass
            # Восстановить buyer_id/purchased_at из purchases, если были сброшены
            try:
                conn.execute("""
                    UPDATE accounts
                    SET buyer_id = (
                        SELECT user_id FROM purchases WHERE purchases.account_id = accounts.id ORDER BY id DESC LIMIT 1
                    ),
                    purchased_at = (
                        SELECT created_at FROM purchases WHERE purchases.account_id = accounts.id ORDER BY id DESC LIMIT 1
                    )
                    WHERE buyer_id IS NULL AND status IN ('sold', 'listening')
                """)
            except Exception:
                pass

    # config
    def get_config(self, key: str, default: Any = None) -> Any:
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row[0])
        except Exception:
            return row[0]

    def set_config(self, key: str, value: Any) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO config(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value) if not isinstance(value, str) else value)
            )

    # users
    def get_user(self, user_id: int, username: str | None = None) -> dict[str, Any]:
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
            if not row:
                conn.execute(
                    "INSERT OR IGNORE INTO users(user_id, username, created_at) VALUES(?, ?, ?)",
                    (user_id, username or "", _now())
                )
                row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return dict(row)

    def set_admin(self, user_id: int) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO users(user_id, is_admin) VALUES(?, 1) ON CONFLICT(user_id) DO UPDATE SET is_admin=1",
                (user_id,)
            )

    def get_admins(self) -> list[int]:
        with self._lock, self._conn() as conn:
            rows = conn.execute("SELECT user_id FROM users WHERE is_admin=1").fetchall()
        return [r[0] for r in rows]

    def update_balance(self, user_id: int, amount: Decimal) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "UPDATE users SET balance = balance + ? WHERE user_id=?",
                (float(amount), user_id)
            )

    def add_spent(self, user_id: int, amount: Decimal) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "UPDATE users SET total_spent = total_spent + ? WHERE user_id=?",
                (float(amount), user_id)
            )

    # categories
    def get_category(self, category_id: int) -> dict[str, Any] | None:
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT * FROM categories WHERE id=?", (category_id,)).fetchone()
        return dict(row) if row else None

    def get_category_by_name(self, name: str) -> dict[str, Any] | None:
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT * FROM categories WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

    def get_categories(self) -> list[dict[str, Any]]:
        with self._lock, self._conn() as conn:
            rows = conn.execute("SELECT * FROM categories WHERE is_active=1 ORDER BY sort_order, id").fetchall()
        return [dict(r) for r in rows]

    def ensure_category(self, name: str, price: float = 0.0) -> dict[str, Any]:
        with self._lock, self._conn() as conn:
            existing = conn.execute("SELECT * FROM categories WHERE name=?", (name,)).fetchone()
            if existing:
                d = dict(existing)
                if d.get("is_active") == 0:
                    conn.execute(
                        "UPDATE categories SET is_active=1, price=MAX(price, ?) WHERE id=?",
                        (float(price or 0), d["id"])
                    )
                    d = dict(conn.execute("SELECT * FROM categories WHERE id=?", (d["id"],)).fetchone())
                return d
            cur = conn.execute(
                "INSERT INTO categories(name, price, created_at) VALUES(?, ?, ?)",
                (name, float(price), _now())
            )
            row = conn.execute("SELECT * FROM categories WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)

    def update_category_price(self, category_id: int, price: float) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("UPDATE categories SET price=? WHERE id=?", (float(price), category_id))

    def delete_category(self, category_id: int) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("DELETE FROM categories WHERE id=?", (category_id,))

    def cleanup_empty_category(self, category_id: int) -> None:
        with self._lock, self._conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM accounts WHERE category_id=?",
                (category_id,)
            ).fetchone()[0]
            if count == 0:
                conn.execute("UPDATE categories SET is_active=0 WHERE id=?", (category_id,))

    # accounts
    def add_account(self, category_id: int, phone: str, session_string: str, proxy: dict[str, Any] | None = None) -> int:
        proxy_json = json.dumps(proxy, ensure_ascii=False) if proxy else None
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO accounts(category_id, phone, session_string, proxy, status, created_at) VALUES(?, ?, ?, ?, ?, ?)",
                (category_id, phone, session_string, proxy_json, STATUS_AVAILABLE, _now())
            )
            return cur.lastrowid

    def get_account(self, account_id: int) -> dict[str, Any] | None:
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if row:
            d = dict(row)
            try:
                d["proxy"] = json.loads(d["proxy"]) if d.get("proxy") else None
            except Exception:
                d["proxy"] = None
            return d
        return None

    def get_available_account(self, category_id: int) -> dict[str, Any] | None:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM accounts WHERE category_id=? AND status=? ORDER BY id LIMIT 1",
                (category_id, STATUS_AVAILABLE)
            ).fetchone()
        return dict(row) if row else None

    def get_accounts(self, category_id: int | None = None, status: str | None = None) -> list[dict[str, Any]]:
        with self._lock, self._conn() as conn:
            query = "SELECT * FROM accounts WHERE 1=1"
            params: list[Any] = []
            if category_id is not None:
                query += " AND category_id=?"
                params.append(category_id)
            if status is not None:
                query += " AND status=?"
                params.append(status)
            query += " ORDER BY id"
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_user_accounts(self, user_id: int) -> list[dict[str, Any]]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM accounts WHERE buyer_id=? ORDER BY purchased_at DESC",
                (user_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def update_account_status(self, account_id: int, status: str, buyer_id: int | None = None,
                              purchased_at: float | None = None, expires_at: float | None = None) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "UPDATE accounts SET status=?, buyer_id=?, purchased_at=?, expires_at=? WHERE id=?",
                (status, buyer_id, purchased_at, expires_at, account_id)
            )

    def update_account_code(self, account_id: int, code: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "UPDATE accounts SET last_code=?, last_code_at=? WHERE id=?",
                (code, _now(), account_id)
            )

    def delete_account(self, account_id: int) -> dict[str, Any] | None:
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            if row:
                conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
            return dict(row) if row else None

    # purchases
    def add_purchase(self, user_id: int, account_id: int, price: float) -> int:
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO purchases(user_id, account_id, price, created_at, delivered_at) VALUES(?, ?, ?, ?, ?)",
                (user_id, account_id, float(price), _now(), _now())
            )
            return cur.lastrowid

    def purchase_accounts(self, user_id: int, category_id: int, qty: int) -> tuple[list[dict[str, Any]], Decimal] | tuple[None, None]:
        """Atomically reserve up to `qty` accounts and charge the user."""
        with self._lock, self._conn() as conn:
            user = dict(conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone() or {})
            cat = dict(conn.execute("SELECT * FROM categories WHERE id=?", (category_id,)).fetchone() or {})
            if not cat:
                return None, None
            price = _to_dec(cat.get("price", 0))
            total = _money_round(price * qty)
            balance = _to_dec(user.get("balance", 0))
            if balance < total:
                return None, None
            rows = conn.execute(
                "SELECT * FROM accounts WHERE category_id=? AND status=? ORDER BY id LIMIT ?",
                (category_id, STATUS_AVAILABLE, qty)
            ).fetchall()
            if len(rows) < qty:
                return None, None
            new_balance = float(balance - total)
            conn.execute("UPDATE users SET balance=?, total_spent=total_spent+? WHERE user_id=?", (new_balance, float(total), user_id))
            accounts = []
            now = _now()
            for row in rows:
                acc = dict(row)
                conn.execute(
                    "UPDATE accounts SET status=?, buyer_id=?, purchased_at=? WHERE id=?",
                    (STATUS_SOLD, user_id, now, acc["id"])
                )
                cur = conn.execute(
                    "INSERT INTO purchases(user_id, account_id, price, created_at, delivered_at) VALUES(?, ?, ?, ?, ?)",
                    (user_id, acc["id"], float(price), now, now)
                )
                acc["purchase_id"] = cur.lastrowid
                try:
                    acc["proxy"] = json.loads(acc["proxy"]) if acc.get("proxy") else None
                except Exception:
                    acc["proxy"] = None
                accounts.append(acc)
        return accounts, total

    def get_purchases(self, user_id: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._conn() as conn:
            if user_id:
                rows = conn.execute(
                    "SELECT * FROM purchases WHERE user_id=? ORDER BY id DESC LIMIT ?",
                    (user_id, limit)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM purchases ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # deposits
    def add_deposit(self, user_id: int, method: str, amount_rub: float, payload: str | None = None,
                    external_id: str | None = None, foreign_amount: str | None = None) -> int:
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO deposits(user_id, method, amount_rub, payload, external_id, foreign_amount, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (user_id, method, float(amount_rub), payload, external_id, foreign_amount, _now())
            )
            return cur.lastrowid

    def get_deposit_by_payload(self, payload: str) -> dict[str, Any] | None:
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT * FROM deposits WHERE payload=?", (payload,)).fetchone()
        return dict(row) if row else None

    def get_deposit_by_external(self, external_id: str) -> dict[str, Any] | None:
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT * FROM deposits WHERE external_id=?", (external_id,)).fetchone()
        return dict(row) if row else None

    def confirm_deposit(self, deposit_id: int) -> None:
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT * FROM deposits WHERE id=?", (deposit_id,)).fetchone()
            if row and dict(row).get("status") != "paid":
                conn.execute("UPDATE deposits SET status='paid', paid_at=? WHERE id=?", (_now(), deposit_id))
                conn.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (float(dict(row)["amount_rub"]), dict(row)["user_id"]))

    def get_user_deposits(self, user_id: int, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM deposits WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (user_id, limit)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_all_deposits(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM deposits ORDER BY id DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # stats
    def get_stats(self) -> dict[str, Any]:
        with self._lock, self._conn() as conn:
            total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            total_accounts = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
            available = conn.execute(
                "SELECT COUNT(*) FROM accounts WHERE status=?", (STATUS_AVAILABLE,)
            ).fetchone()[0]
            sold = conn.execute("SELECT COUNT(*) FROM accounts WHERE status!=?", (STATUS_AVAILABLE,)).fetchone()[0]
            revenue = conn.execute("SELECT COALESCE(SUM(price), 0) FROM purchases").fetchone()[0]
            deposits = conn.execute("SELECT COALESCE(SUM(amount_rub), 0) FROM deposits WHERE status='paid'").fetchone()[0]
        return {
            "users": total_users,
            "accounts": total_accounts,
            "available": available,
            "sold": sold,
            "revenue": float(revenue),
            "deposits": float(deposits),
        }

    def add_review_request(self, user_id: int, purchase_id: int | None = None) -> int:
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO review_requests(user_id, purchase_id, status, created_at) VALUES(?, ?, ?, ?)",
                (user_id, purchase_id, "pending", _now())
            )
            return cur.lastrowid

    def get_review_request(self, request_id: int) -> dict[str, Any] | None:
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT * FROM review_requests WHERE id=?", (request_id,)).fetchone()
        return dict(row) if row else None

    def save_review_text(self, request_id: int, text: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("UPDATE review_requests SET text=? WHERE id=?", (text, request_id))

    def approve_review(self, request_id: int) -> dict[str, Any] | None:
        with self._lock, self._conn() as conn:
            req = conn.execute("SELECT * FROM review_requests WHERE id=?", (request_id,)).fetchone()
            if not req:
                return None
            req = dict(req)
            if req.get("status") == "published":
                return req
            conn.execute("UPDATE review_requests SET status='published', moderated_at=? WHERE id=?", (_now(), request_id))
            conn.execute(
                "INSERT INTO reviews(user_id, username, text, created_at) VALUES(?, ?, ?, ?)",
                (req["user_id"], req.get("username") or "", req.get("text") or "", _now())
            )
            return req

    def decline_review(self, request_id: int) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("UPDATE review_requests SET status='declined', moderated_at=? WHERE id=?", (_now(), request_id))

    def get_reviews(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._conn() as conn:
            rows = conn.execute("SELECT * FROM reviews ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

_storage: AccountStorage | None = None
_shop_bot: "AccountShopBot" | None = None

# --- прослушка кодов через Telethon ---
_active_clients: dict[int, TelegramClient] = {}
_active_timers: dict[int, threading.Timer] = {}
_listener_lock = threading.Lock()

def _api_config() -> tuple[int, str]:
    api_id = _storage.get_config("api_id") if _storage else None
    api_hash = _storage.get_config("api_hash") if _storage else None
    if not api_id or not api_hash:
        return 0, ""
    return int(api_id), str(api_hash)

def _proxy_for_telethon(proxy: dict[str, Any] | None) -> dict[str, Any] | tuple[str, int, str] | None:
    if not proxy:
        return None
    scheme = (proxy.get("scheme") or "").lower()
    if scheme == "mtproto":
        secret = proxy.get("secret") or ""
        return (proxy["hostname"], proxy["port"], secret)
    return {
        "proxy_type": scheme,
        "addr": proxy["hostname"],
        "port": proxy["port"],
        "username": proxy.get("username"),
        "password": proxy.get("password"),
        "rdns": True,
    }

def _get_default_proxy() -> dict[str, Any] | None:
    return _parse_proxy(_storage.get_config("default_proxy")) if _storage else None

def _ensure_event_loop() -> None:
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

def _build_handler(account_id: int, buyer_id: int, bot: telebot.TeleBot):
    async def handler(event):
        msg = event.message
        text = getattr(msg, "message", None) or getattr(msg, "caption", None) or ""
        codes = _extract_codes(text)
        if not codes:
            return
        code = codes[0]
        if _storage:
            _storage.update_account_code(account_id, code)
        try:
            bot.send_message(buyer_id, f"📩 Новый код для номера:\n<code>{code}</code>", parse_mode="HTML")
        except Exception:
            logger.exception("Ошибка отправки кода покупателю %s", buyer_id)
    return handler

def _disconnect_client(client: TelegramClient) -> None:
    try:
        if client.loop and client.loop.is_running():
            asyncio.run_coroutine_threadsafe(client.disconnect(), client.loop)
        else:
            client.disconnect()
    except Exception:
        pass

def _stop_listener(account_id: int, account_id_int: int) -> None:
    with _listener_lock:
        client = _active_clients.pop(account_id_int, None)
        timer = _active_timers.pop(account_id_int, None)
    if timer:
        timer.cancel()
    if client:
        _disconnect_client(client)
    if _storage:
        account = _storage.get_account(account_id_int)
        if account:
            _storage.update_account_status(
                account_id_int, STATUS_SOLD,
                buyer_id=account.get("buyer_id"),
                purchased_at=account.get("purchased_at"),
            )
            _storage.cleanup_empty_category(account["category_id"])

def _telethon_client(account: dict[str, Any], api_id: int, api_hash: str) -> TelegramClient:
    proxy = _proxy_for_telethon(account.get("proxy"))
    kwargs: dict[str, Any] = {"api_id": api_id, "api_hash": api_hash}
    if proxy and isinstance(proxy, tuple):
        kwargs["connection"] = connection.ConnectionTcpMTProxyRandomizedIntermediate
        kwargs["proxy"] = proxy
    elif proxy:
        kwargs["proxy"] = proxy
    return TelegramClient(StringSession(account["session_string"]), **kwargs)

def _login_client(proxy: dict[str, Any] | None, api_id: int, api_hash: str) -> TelegramClient:
    proxy_t = _proxy_for_telethon(proxy)
    kwargs: dict[str, Any] = {"api_id": api_id, "api_hash": api_hash}
    if proxy_t and isinstance(proxy_t, tuple):
        kwargs["connection"] = connection.ConnectionTcpMTProxyRandomizedIntermediate
        kwargs["proxy"] = proxy_t
    elif proxy_t:
        kwargs["proxy"] = proxy_t
    return TelegramClient(StringSession(), **kwargs)

def _login_worker(
    chat_id: int,
    user_id: int,
    phone: str,
    proxy: dict[str, Any] | None,
    api_id: int,
    api_hash: str,
    bot: telebot.TeleBot,
    storage: AccountStorage,
    user_states: dict[int, dict[str, Any]],
) -> None:
    _ensure_event_loop()
    client = None
    try:
        if not phone:
            raise ValueError("Номер телефона не указан")
        client = _login_client(proxy, api_id, api_hash)
        client.connect()
        sent = client.send_code_request(phone)
        phone_code_hash = sent.phone_code_hash
        bot.send_message(chat_id, "📩 Код отправлен на номер. Введите его:")
        q: queue.Queue = user_states[user_id]["login_queue"]
        code = q.get()
        if code is None:
            raise RuntimeError("Отменено")
        try:
            client.sign_in(phone, code, phone_code_hash=phone_code_hash)
        except SessionPasswordNeededError:
            bot.send_message(chat_id, "🔐 Аккаунт защищён облачным паролем. Введите его:")
            password = q.get()
            if password is None or password in ("", "-", "нет", "no", "none"):
                raise RuntimeError("Облачный пароль не введён")
            try:
                client.sign_in(password=password)
            except PasswordHashInvalidError:
                raise RuntimeError("Неверный облачный пароль")
        except PhoneCodeInvalidError:
            raise RuntimeError("Неверный код подтверждения")
        except PhoneCodeExpiredError:
            raise RuntimeError("Код подтверждения истёк. Начните заново.")
        session_string = client.session.save()
        try:
            me = client.get_me()
            phone = me.phone or phone
        except Exception:
            pass
        client.disconnect()
        cat_name = _detect_country(phone)
        cat = storage.ensure_category(cat_name)
        if cat.get("price", 0) == 0:
            user_states[user_id] = {
                "state": "admin_add_phone", "step": "set_price",
                "phone": phone, "session_string": session_string, "proxy": proxy,
                "category_name": cat_name, "category_id": cat["id"],
            }
            bot.send_message(chat_id, f"🆕 Новая категория: {cat_name}.\n💰 Введите цену (₽):")
        else:
            storage.add_account(cat["id"], phone, session_string, proxy=proxy)
            user_states.pop(user_id, None)
            bot.send_message(chat_id, f"✅ Аккаунт {phone} добавлен в категорию {cat_name}.")
    except Exception as e:
        err_text = f"{type(e).__name__}: {e}"
        logger.exception("login worker error")
        if client:
            try:
                client.disconnect()
            except Exception:
                pass
        user_states.pop(user_id, None)
        bot.send_message(chat_id, f"❌ Ошибка авторизации: <code>{err_text[:400]}</code>\n\nПроверьте номер, код, пароль и прокси.", parse_mode="HTML")

def start_listener(account_id: int, buyer_id: int, bot: telebot.TeleBot) -> str:
    if _storage is None:
        return "storage_not_ready"
    account = _storage.get_account(account_id)
    if not account:
        return "account_not_found"
    api_id, api_hash = _api_config()
    if not api_id or not api_hash:
        return "no_api_config"

    with _listener_lock:
        # остановить предыдущий если есть
        old = _active_clients.pop(account_id, None)
        if old:
            try:
                old.disconnect()
            except Exception:
                pass
        timer = _active_timers.pop(account_id, None)
        if timer:
            timer.cancel()

    def run_client() -> None:
        _ensure_event_loop()
        client = _telethon_client(account, api_id, api_hash)
        client.add_event_handler(_build_handler(account_id, buyer_id, bot), events.NewMessage(incoming=True))
        with _listener_lock:
            _active_clients[account_id] = client
        try:
            client.start()
            client.run_until_disconnected()
        except Exception:
            logger.exception("Ошибка прослушки аккаунта %s", account_id)
        finally:
            _stop_listener(account_id, account_id)

    thread = threading.Thread(target=run_client, daemon=True, name=f"TgShopListen-{account_id}")
    thread.start()

    expires = _now() + LISTEN_MINUTES * 60
    _storage.update_account_status(
        account_id, STATUS_LISTENING,
        buyer_id=account.get("buyer_id") or buyer_id,
        purchased_at=account.get("purchased_at") or _now(),
        expires_at=expires,
    )

    # таймер авто-остановки
    t = threading.Timer(LISTEN_MINUTES * 60, lambda: _stop_listener(account_id, account_id))
    t.daemon = True
    t.start()
    with _listener_lock:
        _active_timers[account_id] = t
    return "ok"

def get_latest_code(account_id: int) -> str | None:
    if _storage is None:
        return None
    account = _storage.get_account(account_id)
    if not account:
        return None

    # если уже есть прослушка или кэш - отдать кэш
    cached = account.get("last_code")
    cached_at = account.get("last_code_at")
    if cached and cached_at and (_now() - cached_at) < 300:
        return cached

    api_id, api_hash = _api_config()
    if not api_id or not api_hash:
        return cached

    def fetch() -> str | None:
        client = _telethon_client(account, api_id, api_hash)
        best_code: str | None = None
        best_time: float = 0
        try:
            client.start()
            for dialog in client.iter_dialogs(limit=30):
                msg = dialog.message
                if not msg or not msg.date:
                    continue
                ts = msg.date.timestamp()
                text = getattr(msg, "message", None) or getattr(msg, "caption", None) or ""
                codes = _extract_codes(text)
                if codes and ts > best_time:
                    best_code = codes[0]
                    best_time = ts
            client.disconnect()
        except Exception:
            logger.exception("Ошибка получения кода для аккаунта %s", account_id)
            try:
                client.disconnect()
            except Exception:
                pass
        return best_code

    result: list[str | None] = [None]
    def run() -> None:
        _ensure_event_loop()
        result[0] = fetch()
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=25)

    code = result[0] or cached
    if code:
        _storage.update_account_code(account_id, code)
    return code

def stop_listener(account_id: int) -> None:
    _stop_listener(account_id, account_id)

# --- Telegram-бот ---
class AccountShopBot:
    def __init__(self, token: str, storage: AccountStorage, cardinal: Any | None = None) -> None:
        self.storage = storage
        self.bot = telebot.TeleBot(token, parse_mode="HTML")
        self.cardinal = cardinal
        self.user_states: dict[int, dict[str, Any]] = {}
        self._setup_password: str | None = None
        self.bot_username: str = ""
        self._load_bot_username()
        self._ensure_setup_password()
        self._setup_handlers()
        try:
            self.bot.set_my_commands([
                BotCommand("start", "Главное меню"),
                BotCommand("profile", "Мой профиль"),
                BotCommand("support", "Поддержка"),
            ])
        except Exception:
            pass

    def _load_bot_username(self) -> None:
        try:
            me = self.bot.get_me()
            if me and me.username:
                self.bot_username = me.username
                self.storage.set_config("bot_username", me.username)
        except Exception:
            self.bot_username = self.storage.get_config("bot_username") or ""

    def _ensure_setup_password(self) -> None:
        pwd = self.storage.get_config("setup_password")
        if not pwd:
            pwd = secrets.token_hex(4).upper()
            self.storage.set_config("setup_password", pwd)
        self._setup_password = pwd
        admins = self.storage.get_admins()
        cardinal_admins = self._cardinal_admin_ids()
        if not admins and not cardinal_admins:
            logger.warning("[TelegramAccountShop] Нет администраторов. Код первичной настройки: %s", pwd)

    def _cardinal_admin_ids(self) -> set[int]:
        if not self.cardinal or not hasattr(self.cardinal, "telegram"):
            return set()
        try:
            ids = self.cardinal.telegram.admins
            return set(ids) if ids else set()
        except Exception:
            return set()

    def _is_admin(self, user_id: int) -> bool:
        if user_id in self._cardinal_admin_ids():
            return True
        user = self.storage.get_user(user_id)
        return bool(user.get("is_admin"))

    def _notify_admins(self, text: str) -> None:
        for admin_id in self.storage.get_admins():
            try:
                self.bot.send_message(admin_id, text)
            except Exception:
                pass
        for admin_id in self._cardinal_admin_ids():
            try:
                self.bot.send_message(admin_id, text)
            except Exception:
                pass

    def _get_photo(self, key: str) -> str | None:
        """Return local file path or URL for a configured photo (main/buy/deposit/category)."""
        photo = self.storage.get_config(f"photo_{key}") or ""
        if not photo:
            return None
        photo = photo.strip()
        if photo.startswith(("http://", "https://")):
            return photo
        if os.path.isabs(photo) and os.path.exists(photo):
            return photo
        rel = os.path.join(os.path.dirname(DB_FILE), "photos", photo)
        if os.path.exists(rel):
            return rel
        return photo

    def _send_or_edit(
        self,
        chat_id: int,
        text: str,
        keyboard: InlineKeyboardMarkup | None,
        message_id: int | None,
        photo: str | None = None,
        parse_mode: str = "HTML",
    ) -> Message | None:
        """Send a new message or edit existing. Supports both text and photo messages."""
        # Try to edit existing message
        if message_id:
            try:
                if photo:
                    return self.bot.edit_message_caption(
                        caption=text,
                        chat_id=chat_id,
                        message_id=message_id,
                        reply_markup=keyboard,
                        parse_mode=parse_mode,
                    )
                return self.bot.edit_message_text(
                    text=text,
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=keyboard,
                    parse_mode=parse_mode,
                )
            except Exception:
                # If the existing message type differs (e.g. text vs photo),
                # delete it and send a new one.
                try:
                    self.bot.delete_message(chat_id, message_id)
                except Exception:
                    pass
                message_id = None
        if photo:
            try:
                photo_arg = photo
                if not photo_arg.startswith(("http://", "https://")) and os.path.isfile(photo_arg):
                    photo_arg = open(photo_arg, "rb")
                return self.bot.send_photo(
                    chat_id,
                    photo=photo_arg,
                    caption=text,
                    reply_markup=keyboard,
                    parse_mode=parse_mode,
                )
            except Exception:
                pass
        return self.bot.send_message(
            chat_id,
            text,
            reply_markup=keyboard,
            parse_mode=parse_mode,
        )

    # keyboards
    def _main_keyboard(self, user_id: int) -> InlineKeyboardMarkup:
        kb = InlineKeyboardMarkup(row_width=1)
        reviews_url = self.storage.get_config("reviews_channel")
        if reviews_url:
            if reviews_url.startswith("@"):
                reviews_url = f"https://t.me/{reviews_url[1:]}"
            elif not reviews_url.startswith(("http://", "https://")):
                reviews_url = f"https://t.me/{reviews_url}"
        support = self.storage.get_config("support_contact") or ""
        if support and not support.startswith(("http://", "https://", "tg://")):
            support = f"https://t.me/{support.lstrip('@')}"
        kb.add(
            InlineKeyboardButton("📲 Купить аккаунт", callback_data=f"{CB}buy", style="success"),
            InlineKeyboardButton("🗂️ Мои аккаунты", callback_data=f"{CB}my_purchases"),
            InlineKeyboardButton("💳 Пополнить баланс", callback_data=f"{CB}deposit"),
        )
        if reviews_url:
            kb.add(InlineKeyboardButton("📕 Отзывы", url=reviews_url))
        if support:
            kb.add(InlineKeyboardButton("❔ Помощь", url=support, style="danger"))
        else:
            kb.add(InlineKeyboardButton("❔ Помощь", callback_data=f"{CB}support", style="danger"))
        return kb

    def _main_text(self, user_id: int, first_name: str | None = None) -> str:
        user = self.storage.get_user(user_id)
        purchases = self.storage.get_purchases(user_id)
        name = first_name or user.get("username") or "друг"
        return (
            f"Здравствуйте, {name}! ☻\n"
            f"Ваш профиль:\n"
            f"❶ ID Профиля: <code>{user_id}</code>\n"
            f"❷ Баланс: {_money_str(user.get('balance', 0))}₽\n"
            f"❸ Всего потрачено: {_money_str(user.get('total_spent', 0))}₽\n"
            f"❹ Покупок: {len(purchases)}\n\n"
            f"➡︎ <a href=\"https://t.me/{self.bot_username}?start=buy\">Купить аккаунт</a>\n"
            f"➡︎ <a href=\"https://t.me/{self.bot_username}?start=deposit\">Пополнить баланс</a>"
        )

    def _admin_keyboard(self) -> InlineKeyboardMarkup:
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(
            InlineKeyboardButton("➕ Добавить аккаунт", callback_data=f"{CB}admin_add"),
            InlineKeyboardButton("📋 Список аккаунтов", callback_data=f"{CB}admin_accounts"),
            InlineKeyboardButton("🗂 Категории и цены", callback_data=f"{CB}admin_categories"),
            InlineKeyboardButton("✅ Подтвердить платёж", callback_data=f"{CB}admin_confirm_payment"),
            InlineKeyboardButton("💳 Ручное пополнение", callback_data=f"{CB}admin_topup"),
            InlineKeyboardButton("📊 Статистика", callback_data=f"{CB}admin_stats"),
            InlineKeyboardButton("🔙 Назад", callback_data=f"{CB}main"),
        )
        return kb

    def _show_admin_menu(self, user_id: int, chat_id: int, message_id: int | None = None) -> None:
        if not self._is_admin(user_id):
            self.bot.send_message(chat_id, "Нет доступа.")
            return
        text = "🛠 Админ-меню"
        if message_id:
            self.bot.edit_message_text(text, chat_id, message_id, reply_markup=self._admin_keyboard())
        else:
            self.bot.send_message(chat_id, text, reply_markup=self._admin_keyboard())

    def _back_keyboard(self, payload: str) -> InlineKeyboardMarkup:
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("🔙 Назад", callback_data=payload))
        return kb

    # handlers setup
    def _setup_handlers(self) -> None:
        @self.bot.message_handler(commands=["start"])
        def on_start(m: Message) -> None:
            try:
                self.storage.get_user(m.from_user.id, m.from_user.username)
                arg = (m.text or "").strip().split()[1:2]
                param = arg[0] if arg else ""
                if param == "buy":
                    self._show_categories(m.from_user.id, m.chat.id, None)
                elif param == "deposit":
                    self._show_deposit_menu(m.from_user.id, m.chat.id, None)
                elif param == "admin":
                    self._show_admin_menu(m.from_user.id, m.chat.id, None)
                else:
                    text = self._main_text(m.from_user.id, m.from_user.first_name)
                    self._send_or_edit(m.chat.id, text, self._main_keyboard(m.from_user.id), None, photo=self._get_photo("main"))
            except Exception:
                logger.exception("[TelegramAccountShop] Ошибка /start")

        @self.bot.message_handler(commands=["profile"])
        def on_profile(m: Message) -> None:
            self._show_profile(m.from_user.id, m.chat.id)

        @self.bot.message_handler(commands=["setup"])
        def on_setup(m: Message) -> None:
            text = m.text or ""
            parts = text.split(maxsplit=1)
            if len(parts) != 2 or parts[1].strip().upper() != (self._setup_password or ""):
                self.bot.send_message(m.chat.id, "❌ Неверный код настройки.")
                return
            self.storage.set_admin(m.from_user.id)
            self._send_or_edit(m.chat.id, "✅ Вы назначены администратором.", self._main_keyboard(m.from_user.id), None, photo=self._get_photo("main"))

        @self.bot.message_handler(func=lambda m: self.user_states.get(m.from_user.id, {}).get("state") is not None)
        def on_state(m: Message) -> None:
            state = self.user_states.get(m.from_user.id, {})
            handler = state.get("state")
            dispatch = {
                "admin_add": self._handle_admin_add,
                "admin_add_phone": self._handle_admin_add_phone,
                "admin_add_session_text": self._handle_admin_add_session_text,
                "admin_add_custom": self._handle_admin_add_custom,
                "admin_price": self._handle_admin_price,
                "admin_topup": self._handle_admin_topup,
                "admin_confirm_payment": self._handle_admin_confirm_payment,
                "admin_create_category": self._handle_admin_create_category,
                "support": self._handle_support,
                "deposit_amount": self._handle_deposit_amount,
                "review": self._handle_review_text,
            }
            fn = dispatch.get(handler)
            if fn:
                try:
                    fn(m)
                except Exception:
                    logger.exception("[TelegramAccountShop] Ошибка state handler %s", handler)
                    self._send_or_edit(m.chat.id, "❌ Произошла ошибка. Попробуйте снова.", self._main_keyboard(m.from_user.id), None, photo=self._get_photo("main"))
                    self.user_states.pop(m.from_user.id, None)
            else:
                self.user_states.pop(m.from_user.id, None)

        @self.bot.message_handler(content_types=["document"])
        def on_document(m: Message) -> None:
            state = self.user_states.get(m.from_user.id, {})
            if state.get("state") == "admin_upload_session":
                self._handle_upload_session(m)
            else:
                self.bot.send_message(m.chat.id, "📎 Файл не ожидался. Выберите действие в меню.", reply_markup=self._main_keyboard(m.from_user.id))

        @self.bot.callback_query_handler(func=lambda c: c.data.startswith(CB))
        def on_callback(c: CallbackQuery) -> None:
            try:
                self._handle_callback(c)
            except Exception:
                logger.exception("[TelegramAccountShop] Ошибка callback")
                try:
                    self.bot.answer_callback_query(c.id, "Ошибка обработки")
                except Exception:
                    pass

        @self.bot.pre_checkout_query_handler(func=lambda q: True)
        def on_pre_checkout(q) -> None:
            try:
                self.bot.answer_pre_checkout_query(q.id, ok=True)
            except Exception:
                logger.exception("pre_checkout error")

        @self.bot.message_handler(content_types=["successful_payment"])
        def on_successful_payment(m: Message) -> None:
            try:
                self._handle_successful_payment(m)
            except Exception:
                logger.exception("successful_payment error")

    def _handle_support(self, m: Message) -> None:
        self.user_states.pop(m.from_user.id, None)
        support = self.storage.get_config("support_contact") or ""
        if support:
            username = support.lstrip("@")
            text = f"🆘 Напишите в поддержку: <a href=\"https://t.me/{username}\">@{username}</a>"
            self._send_or_edit(m.chat.id, text, self._main_keyboard(m.from_user.id), None)
            return
        self._notify_admins(f"🆘 Поддержка от @{m.from_user.username or m.from_user.id} (ID {m.from_user.id}):\n{m.text}")
        self._send_or_edit(m.chat.id, "✅ Сообщение отправлено администратору.", self._main_keyboard(m.from_user.id), None, photo=self._get_photo("main"))

    # callback dispatcher
    def _handle_callback(self, c: CallbackQuery) -> None:
        data = c.data[len(CB):]
        parts = data.split(":", 3)
        action = parts[0]

        if action == "main":
            text = self._main_text(c.from_user.id, c.from_user.first_name)
            self._send_or_edit(c.message.chat.id, text, self._main_keyboard(c.from_user.id), c.message.message_id, photo=self._get_photo("main"))

        elif action == "buy":
            self._show_categories(c.from_user.id, c.message.chat.id, c.message.message_id)
        elif action == "category":
            self._show_quantity_selector(int(parts[1]), c.from_user.id, c.message.chat.id, c.message.message_id)
        elif action == "qty":
            self._handle_quantity(c)
        elif action == "purchase":
            self._purchase(int(parts[1]), c.from_user.id, c.message.chat.id, c.message.message_id, qty=int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1)
        elif action == "my_purchases":
            self._show_my_purchases(c.from_user.id, c.message.chat.id, c.message.message_id)
        elif action == "account":
            self._show_purchase_account(int(parts[1]), c)
        elif action == "get_code":
            self._get_code(int(parts[1]), c)
        elif action == "listen":
            self._listen(int(parts[1]), c)
        elif action == "stop_listen":
            self._stop_listen(int(parts[1]), c)

        elif action == "profile":
            self._show_profile(c.from_user.id, c.message.chat.id, c.message.message_id)
        elif action == "support":
            support = self.storage.get_config("support_contact") or ""
            if support:
                username = support.lstrip("@")
                text = f"🆘 Напишите в поддержку: <a href=\"https://t.me/{username}\">@{username}</a>"
                self._send_or_edit(c.message.chat.id, text, self._main_keyboard(c.from_user.id), c.message.message_id)
            else:
                self.user_states[c.from_user.id] = {"state": "support"}
                self._send_or_edit(c.message.chat.id, "🆘 Опишите проблему. Мы ответим в ближайшее время.", None, c.message.message_id)

        elif action == "deposit":
            self._show_deposit_menu(c.from_user.id, c.message.chat.id, c.message.message_id)
        elif action == "deposit_amount":
            self._on_deposit_amount(c)
        elif action == "deposit_custom":
            self._on_deposit_custom(c)
        elif action == "deposit_method":
            self._on_deposit_method(c)
        elif action == "deposit_crypto":
            self._on_deposit_crypto(c)
        elif action == "deposit_check":
            self._handle_deposit_check(c)
        elif action == "review_start":
            self._start_review(int(parts[1]), c)
        elif action == "review_publish":
            self._publish_review(int(parts[1]))
            self.bot.answer_callback_query(c.id, "Отзыв опубликован")
            self.bot.edit_message_text("✅ Отзыв опубликован", c.message.chat.id, c.message.message_id)
        elif action == "review_decline":
            self._decline_review(int(parts[1]))
            self.bot.answer_callback_query(c.id, "Отзыв отклонён")
            self.bot.edit_message_text("❌ Отзыв отклонён", c.message.chat.id, c.message.message_id)
        elif action == "noop":
            self.bot.answer_callback_query(c.id, "Отправьте ответным сообщением")

        elif action == "admin":
            self._show_admin_menu(c.from_user.id, c.message.chat.id, c.message.message_id)
        elif action == "admin_add":
            self._show_admin_add_menu(c.message.chat.id, c.message.message_id)
        elif action == "admin_add_phone":
            self.user_states[c.from_user.id] = {"state": "admin_add_phone", "step": "phone"}
            self.bot.edit_message_text(
                "📞 Введите номер телефона аккаунта (международный формат, например +79001234567):",
                c.message.chat.id, c.message.message_id
            )
        elif action == "admin_add_session_text":
            self.user_states[c.from_user.id] = {"state": "admin_add_session_text", "step": "input", "proxy": _get_default_proxy()}
            self.bot.edit_message_text(
                "📋 Введите аккаунты по одному на строке.\n"
                "Формат: <code>phone|session_string</code>\n"
                "Или с категорией: <code>Категория: phone|session_string</code>\n"
                "Категории создадутся автоматически по коду страны.",
                c.message.chat.id, c.message.message_id, parse_mode="HTML"
            )
        elif action == "admin_upload_session":
            self.user_states[c.from_user.id] = {"state": "admin_upload_session", "step": "file", "proxy": _get_default_proxy()}
            self.bot.edit_message_text(
                "📎 Отправьте .session файл или ZIP архив с .session файлами.",
                c.message.chat.id, c.message.message_id
            )
        elif action == "admin_add_custom":
            self.user_states[c.from_user.id] = {"state": "admin_add_custom", "step": "category", "proxy": _get_default_proxy()}
            self.bot.edit_message_text(
                "🗂 Введите название существующей или новой категории:",
                c.message.chat.id, c.message.message_id
            )
        elif action == "admin_accounts":
            self._admin_accounts(c.from_user.id, c.message.chat.id, c.message.message_id)
        elif action == "admin_accounts_cat":
            self._admin_accounts_cat(int(parts[1]), c.message.chat.id, c.message.message_id)
        elif action == "admin_delete_acc":
            self._admin_delete_account(int(parts[1]), c.message.chat.id, c.message.message_id)
        elif action == "admin_categories":
            self._admin_categories(c.message.chat.id, c.message.message_id)
        elif action == "admin_set_price":
            self.user_states[c.from_user.id] = {"state": "admin_price", "category_id": int(parts[1])}
            self.bot.edit_message_text("💰 Введите новую цену:", c.message.chat.id, c.message.message_id)
        elif action == "admin_delete_cat":
            self._admin_delete_category(int(parts[1]), c.message.chat.id, c.message.message_id)
        elif action == "admin_create_category":
            self.user_states[c.from_user.id] = {"state": "admin_create_category", "step": "name"}
            self.bot.edit_message_text("🆕 Введите название новой категории:", c.message.chat.id, c.message.message_id)
        elif action == "admin_add_to_cat":
            self.user_states[c.from_user.id] = {"state": "admin_add_custom", "step": "account", "category_id": int(parts[1])}
            self.bot.edit_message_text(
                "📋 Введите аккаунт: <code>phone|session_string</code>\n"
                "Или несколько — по одному на строке.",
                c.message.chat.id, c.message.message_id, parse_mode="HTML"
            )
        elif action == "admin_topup":
            self.user_states[c.from_user.id] = {"state": "admin_topup", "step": "user_id"}
            self.bot.edit_message_text("Введите ID пользователя для пополнения:", c.message.chat.id, c.message.message_id)
        elif action == "admin_confirm_payment":
            self.user_states[c.from_user.id] = {"state": "admin_confirm_payment", "step": "id"}
            self.bot.edit_message_text("Введите ID депозита для подтверждения:", c.message.chat.id, c.message.message_id)
        elif action == "admin_stats":
            stats = self.storage.get_stats()
            text = (
                f"📊 Статистика\n"
                f"👥 Пользователей: {stats['users']}\n"
                f"📱 Аккаунтов: {stats['accounts']} (свободно: {stats['available']}, продано: {stats['sold']})\n"
                f"💸 Продажи: {_money_str(stats['revenue'])}₽\n"
                f"💰 Пополнения: {_money_str(stats['deposits'])}₽"
            )
            self.bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=self._admin_keyboard())

    # user flows
    def _show_profile(self, user_id: int, chat_id: int, message_id: int | None = None) -> None:
        user = self.storage.get_user(user_id)
        purchases = self.storage.get_purchases(user_id)
        deposits = self.storage.get_user_deposits(user_id)
        text = (
            f"👤 Профиль\n\n"
            f"🆔 ID: <code>{user_id}</code>\n"
            f"💰 Баланс: {_money_str(user.get('balance', 0))}₽\n"
            f"💸 Всего потрачено: {_money_str(user.get('total_spent', 0))}₽\n"
            f"🛒 Покупок: {len(purchases)}\n"
            f"💳 Пополнений: {len([d for d in deposits if d.get('status') == 'paid'])}"
        )
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(InlineKeyboardButton("💳 Пополнить баланс", callback_data=f"{CB}deposit"))
        kb.add(InlineKeyboardButton("🟢 Купить аккаунт", callback_data=f"{CB}buy"))
        kb.add(InlineKeyboardButton("🔙 Назад", callback_data=f"{CB}main"))
        self._send_or_edit(chat_id, text, kb, message_id, photo=self._get_photo("profile"))

    def _show_deposit_menu(self, user_id: int, chat_id: int, message_id: int | None = None) -> None:
        text = "💳 Выберите сумму пополнения:"
        kb = InlineKeyboardMarkup(row_width=3)
        amounts = [100, 200, 500, 1000, 2000, 5000]
        kb.add(*[InlineKeyboardButton(f"{a}₽", callback_data=f"{CB}deposit_amount:{a}") for a in amounts])
        kb.add(InlineKeyboardButton("📝 Своя сумма", callback_data=f"{CB}deposit_custom"))
        kb.add(InlineKeyboardButton("🔙 Назад", callback_data=f"{CB}main"))
        self._send_or_edit(chat_id, text, kb, message_id, photo=self._get_photo("deposit"))

    def _category_button_text(self, cat: dict[str, Any], available: int) -> str:
        code, flag = _country_meta(cat["name"])
        name = " ".join(part for part in (code, flag, cat["name"]) if part)
        return f"{name} – {_money_str(cat['price'])}₽ ({available} шт.)"

    def _show_categories(self, user_id: int, chat_id: int, message_id: int | None = None) -> None:
        categories = self.storage.get_categories()
        user = self.storage.get_user(user_id)
        if not categories:
            text = "😔 Пока нет доступных аккаунтов. Зайдите позже."
            kb = self._back_keyboard(f"{CB}main")
            self._send_or_edit(chat_id, text, kb, message_id, photo=self._get_photo("buy"))
            return
        text = f"🗂 Выберите страну/категорию:\n💰 Ваш баланс: {_money_str(user.get('balance', 0))}₽"
        kb = InlineKeyboardMarkup(row_width=1)
        for cat in categories:
            available = len(self.storage.get_accounts(cat["id"], STATUS_AVAILABLE))
            if available <= 0:
                continue
            kb.add(InlineKeyboardButton(
                self._category_button_text(cat, available),
                callback_data=f"{CB}category:{cat['id']}"
            ))
        if not kb.keyboard:
            text = "😔 Пока нет доступных аккаунтов. Зайдите позже."
            kb = self._back_keyboard(f"{CB}main")
        else:
            kb.add(InlineKeyboardButton("🔙 Назад", callback_data=f"{CB}main"))
        self._send_or_edit(chat_id, text, kb, message_id, photo=self._get_photo("buy"))

    def _quantity_text(self, cat: dict[str, Any], qty: str, available: int, user: dict[str, Any]) -> str:
        code, flag = _country_meta(cat["name"])
        name = " ".join(part for part in (code, flag, cat["name"]) if part)
        try:
            q = max(1, int(qty or "0"))
        except Exception:
            q = 0
        total = _money_round(_to_dec(cat["price"]) * q) if q else _to_dec(0)
        return (
            "➖ Покупка ➖\n\n"
            f"📦 Товар: {name}\n"
            f"💰 Цена: {_money_str(cat['price'])}₽\n"
            f"🛒 Доступно: {available} шт\n\n"
            f"📊 Количество: {q}\n"
            f"💸 Итого: {_money_str(total)}₽\n\n"
            "Введите количество товаров для покупки:"
        )

    def _quantity_keyboard(self, category_id: int, qty: str, available: int) -> InlineKeyboardMarkup:
        kb = InlineKeyboardMarkup(row_width=3)
        for row in (("1", "2", "3"), ("4", "5", "6"), ("7", "8", "9")):
            kb.add(*[InlineKeyboardButton(n, callback_data=f"{CB}qty:{category_id}:{n}") for n in row])
        kb.add(
            InlineKeyboardButton("CLEAR", callback_data=f"{CB}qty:{category_id}:clear"),
            InlineKeyboardButton("0", callback_data=f"{CB}qty:{category_id}:0"),
            InlineKeyboardButton("✅ OK", callback_data=f"{CB}qty:{category_id}:ok"),
        )
        kb.add(
            InlineKeyboardButton("🌍 К категориям", callback_data=f"{CB}buy"),
            InlineKeyboardButton("❌ Закрыть", callback_data=f"{CB}main"),
        )
        return kb

    def _show_quantity_selector(self, category_id: int, user_id: int, chat_id: int, message_id: int, qty: str = "0") -> None:
        cat = self.storage.get_category(category_id)
        if not cat:
            self._send_or_edit(chat_id, "❌ Категория не найдена.", self._back_keyboard(f"{CB}buy"), message_id, photo=self._get_photo("buy"))
            return
        available = len(self.storage.get_accounts(category_id, STATUS_AVAILABLE))
        user = self.storage.get_user(user_id)
        self.user_states[user_id] = {"state": "select_quantity", "category_id": category_id, "qty": qty}
        text = self._quantity_text(cat, qty, available, user)
        kb = self._quantity_keyboard(category_id, qty, available)
        self._send_or_edit(chat_id, text, kb, message_id, photo=self._get_photo("category"))

    def _handle_quantity(self, c: CallbackQuery) -> None:
        data = c.data[len(CB):]
        parts = data.split(":", 3)
        if len(parts) < 3:
            self.bot.answer_callback_query(c.id, "Ошибка")
            return
        category_id = int(parts[1])
        cmd = parts[2]
        state = self.user_states.get(c.from_user.id, {})
        if state.get("state") != "select_quantity" or state.get("category_id") != category_id:
            self.bot.answer_callback_query(c.id, "Сессия устарела")
            return
        qty = str(state.get("qty", "0"))
        if cmd == "clear":
            qty = "0"
        elif cmd == "ok":
            try:
                q = max(1, int(qty or "0"))
            except Exception:
                q = 1
            self.bot.answer_callback_query(c.id, "Обрабатываю...")
            self._purchase(category_id, c.from_user.id, c.message.chat.id, c.message.message_id, qty=q)
            return
        else:
            if not cmd.isdigit():
                self.bot.answer_callback_query(c.id, "Некорректный ввод")
                return
            qty = (qty + cmd) if qty != "0" else cmd
            # limit length to avoid overflow
            if len(qty) > 5:
                qty = qty[-5:]
        state["qty"] = qty
        self._show_quantity_selector(category_id, c.from_user.id, c.message.chat.id, c.message.message_id, qty=qty)

    def _purchase(self, category_id: int, user_id: int, chat_id: int, message_id: int, qty: int = 1) -> None:
        self.user_states.pop(user_id, None)
        cat = self.storage.get_category(category_id)
        if not cat:
            self._send_or_edit(chat_id, "❌ Категория не найдена.", self._back_keyboard(f"{CB}buy"), message_id, photo=self._get_photo("buy"))
            return
        if qty < 1:
            self._send_or_edit(chat_id, "❌ Некорректное количество.", self._back_keyboard(f"{CB}buy"), message_id, photo=self._get_photo("buy"))
            return
        available = len(self.storage.get_accounts(category_id, STATUS_AVAILABLE))
        if qty > available:
            text = f"😔 Недостаточно аккаунтов. Доступно: {available} шт."
            self._send_or_edit(chat_id, text, self._back_keyboard(f"{CB}buy"), message_id, photo=self._get_photo("buy"))
            return

        accounts, total = self.storage.purchase_accounts(user_id, category_id, qty)
        if not accounts:
            text = "😔 Не удалось совершить покупку. Проверьте баланс или наличие аккаунтов."
            self._send_or_edit(chat_id, text, self._back_keyboard(f"{CB}buy"), message_id, photo=self._get_photo("buy"))
            return

        if qty == 1:
            account = accounts[0]
            text = "✅ Вы купили аккаунт!\n\n" + self._purchase_account_text(account)
            kb = InlineKeyboardMarkup(row_width=1)
            kb.add(
                InlineKeyboardButton("🔄 Получить код", callback_data=f"{CB}get_code:{account['id']}"),
                InlineKeyboardButton(f"👂 Прослушка {LISTEN_MINUTES} мин", callback_data=f"{CB}listen:{account['id']}"),
                InlineKeyboardButton("🔙 Мои покупки", callback_data=f"{CB}my_purchases"),
            )
        else:
            text = f"✅ Вы купили {qty} аккаунтов на {_money_str(total)}₽!\n\n"
            for account in accounts:
                text += f"📞 <code>{account['phone']}</code>\n"
            kb = InlineKeyboardMarkup(row_width=1)
            for account in accounts[:10]:
                kb.add(InlineKeyboardButton(f"🔄 {account['phone']}", callback_data=f"{CB}get_code:{account['id']}"))
            kb.add(InlineKeyboardButton("🔙 Мои покупки", callback_data=f"{CB}my_purchases"))

        self._send_or_edit(chat_id, text, kb, message_id, photo=self._get_photo("buy"))
        for account in accounts:
            threading.Timer(120.0, self._ask_for_review, args=(user_id, account["purchase_id"] if "purchase_id" in account else None)).start()

    def _ask_for_review(self, user_id: int, purchase_id: int) -> None:
        try:
            req_id = self.storage.add_review_request(user_id, purchase_id)
            kb = InlineKeyboardMarkup(row_width=1)
            kb.add(InlineKeyboardButton("Оставить отзыв", callback_data=f"{CB}review_start:{req_id}"))
            self.bot.send_message(user_id, "Если не трудно оставьте отзыв!", reply_markup=kb)
        except Exception:
            logger.exception("Review request failed")

    def _show_my_purchases(self, user_id: int, chat_id: int, message_id: int | None = None) -> None:
        accounts = self.storage.get_user_accounts(user_id)
        if not accounts:
            text = "📭 У вас пока нет купленных аккаунтов."
            kb = self._back_keyboard(f"{CB}main")
        else:
            text = "📱 Ваши покупки. Нажмите на номер, чтобы получить код или включить прослушку."
            kb = InlineKeyboardMarkup(row_width=1)
            for acc in accounts:
                kb.add(InlineKeyboardButton(f"📞 {acc['phone']}", callback_data=f"{CB}account:{acc['id']}"))
            kb.add(InlineKeyboardButton("🔙 Назад", callback_data=f"{CB}main"))
        self._send_or_edit(chat_id, text, kb, message_id, photo=self._get_photo("purchases"))

    def _purchase_account_text(self, account: dict[str, Any]) -> str:
        cat = self.storage.get_category(account.get("category_id")) or {}
        cat_name = cat.get("name", "—")
        price = cat.get("price", 0)
        code, flag = _country_meta(cat_name)
        country = " ".join(part for part in (code, flag, cat_name) if part)
        return (
            "📱 Ваш аккаунт\n\n"
            f"📞 Номер: <code>{account['phone']}</code>\n"
            f"🌍 Страна: {country}\n"
            f"💰 Цена: {_money_str(price)}₽\n"
            f"📊 Статус: {account['status']}"
        )

    def _show_purchase_account(self, account_id: int, c: CallbackQuery) -> None:
        account = self.storage.get_account(account_id)
        if not account or account.get("buyer_id") != c.from_user.id:
            self.bot.answer_callback_query(c.id, "Аккаунт не найден")
            return
        text = self._purchase_account_text(account)
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(
            InlineKeyboardButton("🔄 Получить код", callback_data=f"{CB}get_code:{account_id}"),
            InlineKeyboardButton(f"👂 Прослушка {LISTEN_MINUTES} мин", callback_data=f"{CB}listen:{account_id}"),
            InlineKeyboardButton("🔙 Мои покупки", callback_data=f"{CB}my_purchases"),
        )
        self.bot.answer_callback_query(c.id)
        self.bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb, parse_mode="HTML")

    def _start_review(self, request_id: int, c: CallbackQuery) -> None:
        req = self.storage.get_review_request(request_id)
        chat_id = c.message.chat.id
        message_id = c.message.message_id
        if not req or req.get("user_id") != c.from_user.id or req.get("status") != "pending":
            self.bot.answer_callback_query(c.id, "Запрос не найден")
            return
        self.user_states[c.from_user.id] = {"state": "review", "review_request_id": request_id}
        self.bot.answer_callback_query(c.id, "📝 Напишите отзыв")
        self.bot.edit_message_text("📝 Напишите ваш отзыв одним сообщением.", chat_id, message_id)

    def _handle_review_text(self, m: Message) -> None:
        state = self.user_states.get(m.from_user.id, {})
        request_id = state.get("review_request_id")
        text = (m.text or "").strip()
        if not text or len(text) < 3:
            self.bot.send_message(m.chat.id, "❌ Отзыв слишком короткий.")
            return
        self.storage.save_review_text(request_id, text)
        self.user_states.pop(m.from_user.id, None)
        self.bot.send_message(m.chat.id, "✅ Спасибо! Ваш отзыв отправлен на модерацию.")
        self._send_review_for_moderation(request_id, m.from_user.id, text)

    def _send_review_for_moderation(self, request_id: int, user_id: int, text: str) -> None:
        user = self.storage.get_user(user_id)
        username = user.get("username") or str(user_id)
        mod_text = (
            f"📝 Новый отзыв\n"
            f"От: @{username} (ID {user_id})\n"
            f"Текст: {text}\n\n"
            f"Для публикации: /publish_review {request_id}\n"
            f"Для отклонения: /decline_review {request_id}"
        )
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton("✅ Опубликовать", callback_data=f"{CB}review_publish:{request_id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"{CB}review_decline:{request_id}"),
        )
        moderation_chat = self.storage.get_config("reviews_moderation_chat_id")
        if moderation_chat:
            try:
                self.bot.send_message(int(moderation_chat), mod_text, reply_markup=kb)
                return
            except Exception:
                pass
        self._notify_admins(mod_text)

    def _publish_review(self, request_id: int) -> None:
        req = self.storage.approve_review(request_id)
        if not req:
            return
        user_id = req["user_id"]
        text = req.get("text") or ""
        user = self.storage.get_user(user_id)
        username = user.get("username") or "Покупатель"
        channel = self.storage.get_config("reviews_channel")
        if channel:
            try:
                self.bot.send_message(int(channel), f"📕 Отзыв от @{username}:\n{text}")
            except Exception:
                try:
                    self.bot.send_message(str(channel), f"📕 Отзыв от @{username}:\n{text}")
                except Exception:
                    pass
        try:
            self.bot.send_message(user_id, "✅ Ваш отзыв опубликован!")
        except Exception:
            pass

    def _decline_review(self, request_id: int) -> None:
        req = self.storage.get_review_request(request_id)
        if not req:
            return
        self.storage.decline_review(request_id)
        try:
            self.bot.send_message(req["user_id"], "❌ Ваш отзыв не прошёл модерацию.")
        except Exception:
            pass

    def _get_code(self, account_id: int, c: CallbackQuery) -> None:
        account = self.storage.get_account(account_id)
        chat_id = c.message.chat.id
        message_id = c.message.message_id
        if not account or account.get("buyer_id") != c.from_user.id:
            self.bot.answer_callback_query(c.id, "Аккаунт не найден")
            return
        self.bot.answer_callback_query(c.id, "⏳ Получаю код...")
        self.bot.edit_message_text("⏳ Получаю код...", chat_id, message_id)
        code = get_latest_code(account_id)
        if code:
            text = f"📞 Номер: <code>{account['phone']}</code>\n🔢 Код: <code>{code}</code>"
        else:
            text = f"📞 Номер: <code>{account['phone']}</code>\n😔 Код пока не пришёл. Попробуйте позже или включите прослушку."
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(
            InlineKeyboardButton("🔄 Обновить", callback_data=f"{CB}get_code:{account_id}"),
            InlineKeyboardButton(f"👂 Прослушка {LISTEN_MINUTES} мин", callback_data=f"{CB}listen:{account_id}"),
            InlineKeyboardButton("🔙 К аккаунту", callback_data=f"{CB}account:{account_id}"),
            InlineKeyboardButton("🔙 Мои покупки", callback_data=f"{CB}my_purchases"),
        )
        self.bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)

    def _listen(self, account_id: int, c: CallbackQuery) -> None:
        account = self.storage.get_account(account_id)
        chat_id = c.message.chat.id
        message_id = c.message.message_id
        if not account or account.get("buyer_id") != c.from_user.id:
            self.bot.answer_callback_query(c.id, "Аккаунт не найден")
            return
        self.bot.answer_callback_query(c.id, "⏳ Запускаю прослушку...")
        self.bot.edit_message_text("⏳ Запускаю прослушку...", chat_id, message_id)
        result = start_listener(account_id, c.from_user.id, self.bot)
        if result == "no_api_config":
            text = "❌ API ID/API Hash не настроены. Обратитесь к администратору."
        elif result == "ok":
            text = (
                f"👂 Прослушка аккаунта <code>{account['phone']}</code> запущена на {LISTEN_MINUTES} мин.\n"
                "Все коды будут приходить сюда автоматически."
            )
        else:
            text = "❌ Не удалось запустить прослушку."
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(InlineKeyboardButton("🛑 Остановить", callback_data=f"{CB}stop_listen:{account_id}"))
        kb.add(InlineKeyboardButton("🔙 К аккаунту", callback_data=f"{CB}account:{account_id}"))
        kb.add(InlineKeyboardButton("🔙 Мои покупки", callback_data=f"{CB}my_purchases"))
        self.bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)

    def _stop_listen(self, account_id: int, c: CallbackQuery) -> None:
        account = self.storage.get_account(account_id)
        chat_id = c.message.chat.id
        message_id = c.message.message_id
        if not account or account.get("buyer_id") != c.from_user.id:
            self.bot.answer_callback_query(c.id, "Аккаунт не найден")
            return
        self.bot.answer_callback_query(c.id, "⏳ Останавливаю...")
        stop_listener(account_id)
        text = f"🛑 Прослушка аккаунта <code>{account['phone']}</code> остановлена.\n\n" + self._purchase_account_text(account)
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(
            InlineKeyboardButton("🔄 Получить код", callback_data=f"{CB}get_code:{account_id}"),
            InlineKeyboardButton(f"👂 Прослушка {LISTEN_MINUTES} мин", callback_data=f"{CB}listen:{account_id}"),
            InlineKeyboardButton("🔙 Мои покупки", callback_data=f"{CB}my_purchases"),
        )
        self.bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)

    # deposit flows
    def _on_deposit_amount(self, c: CallbackQuery) -> None:
        parts = c.data[len(CB):].split(":")
        try:
            amount_rub = float(int(parts[1]))
        except Exception:
            amount_rub = 0.0
        self.user_states[c.from_user.id] = {"state": "deposit_method_select", "amount_rub": amount_rub}
        self._show_deposit_methods(c.from_user.id, c.message.chat.id, c.message.message_id, amount_rub)
        self.bot.answer_callback_query(c.id)

    def _on_deposit_custom(self, c: CallbackQuery) -> None:
        self.user_states[c.from_user.id] = {"state": "deposit_amount"}
        self.bot.edit_message_text(
            "📝 Введите сумму пополнения в рублях:",
            c.message.chat.id, c.message.message_id,
            reply_markup=self._back_keyboard(f"{CB}deposit"),
        )
        self.bot.answer_callback_query(c.id)

    def _show_deposit_methods(self, user_id: int, chat_id: int, message_id: int | None, amount_rub: float) -> None:
        text = f"💳 Пополнение на {_money_str(amount_rub)}₽\n\nВыберите способ оплаты:"
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(
            InlineKeyboardButton("⭐ Telegram Stars", callback_data=f"{CB}deposit_method:stars"),
            InlineKeyboardButton("💎 Crypto Bot", callback_data=f"{CB}deposit_method:crypto"),
            InlineKeyboardButton("🏦 СБП", callback_data=f"{CB}deposit_method:sbp"),
            InlineKeyboardButton("🔙 Назад", callback_data=f"{CB}deposit"),
        )
        if message_id:
            self.bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)
        else:
            self.bot.send_message(chat_id, text, reply_markup=kb)

    def _on_deposit_method(self, c: CallbackQuery) -> None:
        parts = c.data[len(CB):].split(":")
        method = parts[1] if len(parts) > 1 else "sbp"
        state = self.user_states.get(c.from_user.id, {})
        amount_rub = state.get("amount_rub", 0)
        if not amount_rub:
            self.bot.answer_callback_query(c.id, "Сначала выберите сумму")
            return
        if method == "stars":
            self.bot.edit_message_text(
                "⭐ Счёт Stars отправлен. Оплатите его в сообщении ниже.",
                c.message.chat.id, c.message.message_id,
                reply_markup=self._back_keyboard(f"{CB}main"),
            )
            self._send_stars_invoice(c.message.chat.id, c.from_user.id, amount_rub)
        elif method == "crypto":
            self._show_crypto_assets(c, amount_rub)
        elif method == "sbp":
            self._send_sbp_payment(c.message.chat.id, c.from_user.id, amount_rub, c.message.message_id)
        self.bot.answer_callback_query(c.id)

    def _handle_deposit_amount(self, m: Message) -> None:
        try:
            amount = _to_dec(m.text)
        except Exception:
            self.bot.send_message(m.chat.id, "❌ Введите число.")
            return
        amount_rub = float(amount)
        self.user_states[m.from_user.id] = {"state": "deposit_method_select", "amount_rub": amount_rub}
        self._show_deposit_methods(m.from_user.id, m.chat.id, None, amount_rub)

    def _send_sbp_payment(self, chat_id: int, user_id: int, amount_rub: float, message_id: int | None = None) -> None:
        if not self.storage.get_config("platega_secret") or not self.storage.get_config("platega_merchant_id"):
            text = "❌ СБП не настроен. Обратитесь к администратору."
            if message_id:
                self.bot.edit_message_text(text, chat_id, message_id, reply_markup=self._back_keyboard(f"{CB}main"))
            else:
                self.bot.send_message(chat_id, text, reply_markup=self._back_keyboard(f"{CB}main"))
            self.user_states.pop(user_id, None)
            return
        tx = self._create_platega_transaction(user_id, amount_rub)
        if not tx:
            text = "❌ Не удалось создать платёж. Попробуйте позже."
            if message_id:
                self.bot.edit_message_text(text, chat_id, message_id, reply_markup=self._back_keyboard(f"{CB}main"))
            else:
                self.bot.send_message(chat_id, text, reply_markup=self._back_keyboard(f"{CB}main"))
            self.user_states.pop(user_id, None)
            return
        transaction_id = tx.get("transactionId")
        redirect = tx.get("redirect") or tx.get("url") or ""
        qr = tx.get("qr") or ""
        if transaction_id and not qr:
            set_resp = self._set_platega_payment_method(transaction_id)
            qr = (set_resp or {}).get("qr") or ""
        pay_url = qr or redirect or ""
        text = (
            f"🏦 Оплатите {_money_str(amount_rub)}₽ по СБП\n\n"
            f"Ссылка/QR:\n<code>{qr or pay_url}</code>\n\n"
            f"После оплаты нажмите «Проверить оплату» или дождитесь автоматического зачисления."
        )
        kb = InlineKeyboardMarkup(row_width=1)
        if pay_url:
            kb.add(InlineKeyboardButton("🔗 Перейти к оплате", url=pay_url))
        if transaction_id:
            kb.add(InlineKeyboardButton("🔄 Проверить оплату", callback_data=f"{CB}deposit_check:{transaction_id}"))
        kb.add(InlineKeyboardButton("🔙 Назад", callback_data=f"{CB}main"))
        if message_id:
            self.bot.edit_message_text(text, chat_id, message_id, reply_markup=kb, parse_mode="HTML")
        else:
            self.bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")
        self.user_states.pop(user_id, None)
        if transaction_id:
            threading.Thread(target=self._poll_platega, args=(transaction_id, user_id, amount_rub), daemon=True, name=f"PlategaPoll-{transaction_id}").start()

    def _handle_deposit_check(self, c: CallbackQuery) -> None:
        parts = c.data[len(CB):].split(":")
        transaction_id = parts[1] if len(parts) > 1 else ""
        if not transaction_id:
            self.bot.answer_callback_query(c.id, "Неизвестный платёж")
            return
        data = self._get_platega_transaction(transaction_id)
        if not data:
            self.bot.answer_callback_query(c.id, "Не удалось получить статус. Попробуйте позже.")
            return
        status = (data.get("status") or "").upper()
        if status == "CONFIRMED":
            deposit = self.storage.get_deposit_by_external(transaction_id)
            if deposit and deposit.get("status") != "paid":
                self.storage.confirm_deposit(deposit["id"])
                try:
                    self.bot.send_message(
                        c.message.chat.id,
                        f"✅ Оплата получена. Баланс пополнен на {_money_str(deposit['amount_rub'])}₽.",
                        reply_markup=self._main_keyboard(c.from_user.id),
                    )
                except Exception:
                    pass
                self._notify_admins(f"💰 СБП пополнение от {c.from_user.id} на {_money_str(deposit['amount_rub'])}₽")
            self.bot.answer_callback_query(c.id, "✅ Оплата получена!")
        elif status in ("CANCELED", "FAILED", "EXPIRED"):
            self.bot.answer_callback_query(c.id, "❌ Платёж не был завершён.")
        else:
            self.bot.answer_callback_query(c.id, "⏳ Платёж ещё не получен. Попробуйте позже.")

    def _send_stars_invoice(self, chat_id: int, user_id: int, amount_rub: float) -> None:
        rate = self.storage.get_config("stars_per_rub") or DEFAULT_STARS_PER_RUB
        stars_amount = max(1, int(amount_rub * float(rate)))
        title = f"Пополнение баланса на {amount_rub}₽"
        description = f"После оплаты {_money_str(amount_rub)}₽ будут зачислены на ваш баланс."
        payload = f"stars_{user_id}_{uuid.uuid4().hex[:8]}"
        self.storage.add_deposit(user_id, "stars", amount_rub, payload=payload)
        prices = [LabeledPrice(label="XTR", amount=stars_amount)]
        self.bot.send_invoice(
            chat_id,
            title,
            description,
            payload,
            "",
            "XTR",
            prices,
            start_parameter=payload,
        )
        self.user_states.pop(user_id, None)

    def _handle_successful_payment(self, m: Message) -> None:
        payload = m.successful_payment.invoice_payload
        deposit = self.storage.get_deposit_by_payload(payload)
        if deposit:
            self.storage.confirm_deposit(deposit["id"])
            self.bot.send_message(
                m.chat.id,
                f"✅ Оплата получена. Баланс пополнен на {_money_str(deposit['amount_rub'])}₽.",
                reply_markup=self._main_keyboard(m.from_user.id),
            )
            self._notify_admins(
                f"💰 Пополнение Stars от {m.from_user.id} на {_money_str(deposit['amount_rub'])}₽"
            )
        else:
            self.bot.send_message(m.chat.id, "✅ Оплата получена. Обратитесь в поддержку.")

    def _show_crypto_assets(self, c: CallbackQuery, amount_rub: float) -> None:
        text = f"💎 Выберите валюту для пополнения на {_money_str(amount_rub)}₽:"
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton("USDT", callback_data=f"{CB}deposit_crypto:USDT"),
            InlineKeyboardButton("TON", callback_data=f"{CB}deposit_crypto:TON"),
        )
        kb.add(InlineKeyboardButton("🔙 Назад", callback_data=f"{CB}deposit"))
        self.bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb)

    def _on_deposit_crypto(self, c: CallbackQuery) -> None:
        parts = c.data[len(CB):].split(":")
        asset = parts[1] if len(parts) > 1 else "USDT"
        if asset not in ("USDT", "TON"):
            asset = "USDT"
        state = self.user_states.get(c.from_user.id, {})
        amount_rub = state.get("amount_rub", 0)
        if not amount_rub:
            self.bot.answer_callback_query(c.id, "Сначала выберите сумму")
            return
        if not self.storage.get_config("crypto_bot_token"):
            self.bot.edit_message_text(
                "❌ Crypto Bot не настроен. Обратитесь к администратору.",
                c.message.chat.id, c.message.message_id,
                reply_markup=self._back_keyboard(f"{CB}deposit"),
            )
            self.user_states.pop(c.from_user.id, None)
            return
        rate = self._get_crypto_rate(asset)
        if not rate:
            self.bot.answer_callback_query(c.id, "Не удалось получить курс. Попробуйте позже.")
            return
        foreign = round(amount_rub / float(rate), 6)
        payload = f"crypto_{c.from_user.id}_{uuid.uuid4().hex[:8]}"
        description = f"Пополнение баланса на {amount_rub}₽"
        invoice = self._create_crypto_invoice(amount_rub, asset, description, payload)
        if not invoice:
            self.bot.edit_message_text(
                "❌ Не удалось создать счёт в Crypto Bot.",
                c.message.chat.id, c.message.message_id,
                reply_markup=self._back_keyboard(f"{CB}deposit"),
            )
            self.user_states.pop(c.from_user.id, None)
            return
        invoice_id = invoice.get("invoice_id")
        pay_url = invoice.get("bot_invoice_url") or invoice.get("pay_url")
        if not pay_url:
            hash_ = invoice.get("hash") or str(invoice_id)
            crypto_bot_name = self.storage.get_config("crypto_bot_name") or "CryptoBot"
            pay_url = f"https://t.me/{crypto_bot_name}?start={hash_}"
        self.storage.add_deposit(c.from_user.id, "crypto", amount_rub, payload=payload, external_id=str(invoice_id), foreign_amount=f"{foreign} {asset}")
        text = (
            f"💎 Счёт Crypto Bot создан.\n"
            f"Сумма: {amount_rub}₽ (~{foreign} {asset})\n\n"
            f"Перейдите по ссылке и оплатите:\n{pay_url}"
        )
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(InlineKeyboardButton("🔗 Перейти к оплате", url=pay_url))
        kb.add(InlineKeyboardButton("🔙 Назад", callback_data=f"{CB}deposit"))
        self.bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb, disable_web_page_preview=True)
        self.user_states.pop(c.from_user.id, None)
        self.bot.answer_callback_query(c.id)

    def _get_crypto_exchange_rates(self) -> dict[str, float]:
        token = self.storage.get_config("crypto_bot_token")
        if not token:
            return {}
        data = self._crypto_api_call("getExchangeRates", token)
        if not data or not data.get("ok"):
            logger.error("CryptoBot getExchangeRates error: %s", data)
            return {}
        rates = {}
        for item in data.get("result", []):
            if item.get("target") == "RUB":
                source = item.get("source")
                try:
                    rates[source] = float(item.get("rate", "0"))
                except Exception:
                    pass
        for asset, rate in rates.items():
            if asset in ("USDT", "TON"):
                self.storage.set_config(f"{asset.lower()}_to_rub", rate)
        return rates

    def _get_crypto_rate(self, asset: str) -> float | None:
        rates = self._get_crypto_exchange_rates()
        if rates and asset in rates:
            return rates[asset]
        fallback = self.storage.get_config(f"{asset.lower()}_to_rub")
        if fallback:
            try:
                return float(fallback)
            except Exception:
                pass
        return {"USDT": DEFAULT_USDT_TO_RUB, "TON": DEFAULT_TON_TO_RUB}.get(asset)

    def _create_crypto_invoice(self, amount_rub: float, asset: str, description: str, payload: str) -> dict | None:
        token = self.storage.get_config("crypto_bot_token")
        if not token:
            return None
        rate = self._get_crypto_rate(asset)
        if not rate:
            return None
        amount = round(amount_rub / float(rate), 6)
        try:
            resp = requests.post(
                "https://pay.crypt.bot/api/createInvoice",
                headers={"Crypto-Pay-API-Token": token},
                json={
                    "currency_type": "crypto",
                    "asset": asset,
                    "amount": str(amount),
                    "description": description,
                    "payload": payload,
                },
                timeout=20,
            )
            data = resp.json()
            if data.get("ok"):
                return data.get("result")
            logger.error("CryptoBot createInvoice error: %s", data)
        except Exception:
            logger.exception("CryptoBot createInvoice")
        return None

    def _crypto_api_call(self, method: str, token: str, params: dict | None = None) -> dict | None:
        try:
            url = f"https://pay.crypt.bot/api/{method}"
            resp = requests.get(url, headers={"Crypto-Pay-API-Token": token}, params=params or {}, timeout=20)
            return resp.json()
        except Exception:
            logger.exception("CryptoBot api call")
            return None

    def _start_crypto_polling(self) -> None:
        def poll() -> None:
            while True:
                try:
                    token = self.storage.get_config("crypto_bot_token") if self.storage else None
                    if token:
                        self._poll_crypto_invoices(token)
                except Exception:
                    logger.exception("crypto polling")
                time.sleep(30)
        threading.Thread(target=poll, daemon=True, name="CryptoShopPoll").start()

    def _poll_crypto_invoices(self, token: str) -> None:
        data = self._crypto_api_call("getInvoices", token, {"status": "paid", "count": 100})
        if not data or not data.get("ok"):
            return
        for inv in data.get("result", {}).get("items", []):
            ext = str(inv.get("invoice_id"))
            payload = inv.get("payload")
            deposit = self.storage.get_deposit_by_external(ext) or self.storage.get_deposit_by_payload(payload)
            if deposit and deposit.get("status") != "paid":
                self.storage.confirm_deposit(deposit["id"])
                try:
                    self.bot.send_message(deposit["user_id"], f"💰 Баланс пополнен на {_money_str(deposit['amount_rub'])}₽")
                except Exception:
                    pass
                self._notify_admins(f"💎 Crypto Bot пополнение от {deposit['user_id']} на {_money_str(deposit['amount_rub'])}₽")

    def _platega_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-MerchantId": self.storage.get_config("platega_merchant_id") or "",
            "X-Secret": self.storage.get_config("platega_secret") or "",
        }

    def _platega_signature(self) -> str:
        merchant = self.storage.get_config("platega_merchant_id") or ""
        now = int(time.time())
        ms = int(time.time() * 1000)
        raw = f"{now}:{merchant}:{PLATEGA_SIG_KEY}:{PLATEGA_SIG_IV}:{ms}"
        return base64.b64encode(raw.encode()).decode()

    def _create_platega_transaction(self, user_id: int, amount_rub: float) -> dict[str, Any] | None:
        payload = f"platega_{user_id}_{uuid.uuid4().hex[:8]}"
        body = {
            "paymentMethod": PLATEGA_SBP,
            "paymentDetails": {"amount": float(amount_rub), "currency": "RUB"},
            "description": "Пополнение баланса PepeVPN",
            "return": "https://pepevpn.site/payment/success",
            "failedUrl": "https://pepevpn.site/payment/fail",
            "payload": payload,
            "metadata": {"userId": str(user_id), "userName": ""},
        }
        try:
            resp = requests.post(
                f"{PLATEGA_BASE}/transaction/process",
                headers=self._platega_headers(),
                json=body,
                timeout=30,
            )
            data = resp.json()
            if resp.ok and data.get("transactionId"):
                self.storage.add_deposit(user_id, "platega", amount_rub, payload=payload, external_id=data.get("transactionId"))
                return data
            logger.error("Platega create error: %s %s", resp.status_code, data)
        except Exception:
            logger.exception("Platega create transaction")
        return None

    def _get_platega_transaction(self, transaction_id: str) -> dict[str, Any] | None:
        try:
            resp = requests.get(
                f"{PLATEGA_BASE}/transaction/{transaction_id}",
                headers=self._platega_headers(),
                timeout=20,
            )
            if resp.ok:
                return resp.json()
            logger.error("Platega status error: %s %s", resp.status_code, resp.text)
        except Exception:
            logger.exception("Platega get transaction")
        return None

    def _set_platega_payment_method(self, transaction_id: str) -> dict[str, Any] | None:
        merchant = self.storage.get_config("platega_merchant_id") or ""
        headers = {
            "Content-Type": "application/json",
            "X-MerchantId": merchant,
            "signature": self._platega_signature(),
            "Referer": f"{PLATEGA_PAYFORM}/sbp-qr?id={transaction_id}&mh={merchant}",
        }
        try:
            resp = requests.post(
                f"{PLATEGA_BASE}/v2/transaction/{transaction_id}/set",
                headers=headers,
                json={"paymentMethod": PLATEGA_SBP},
                timeout=30,
            )
            if resp.ok:
                return resp.json()
            logger.error("Platega set method error: %s %s", resp.status_code, resp.text)
        except Exception:
            logger.exception("Platega set payment method")
        return None

    def _poll_platega(self, transaction_id: str, user_id: int, amount_rub: float) -> None:
        checked = 0
        while checked < 180:
            time.sleep(10)
            checked += 1
            data = self._get_platega_transaction(transaction_id)
            if not data:
                continue
            status = data.get("status", "").upper()
            if status == "CONFIRMED":
                deposit = self.storage.get_deposit_by_external(transaction_id)
                if deposit and deposit.get("status") != "paid":
                    self.storage.confirm_deposit(deposit["id"])
                    try:
                        self.bot.send_message(
                            user_id,
                            f"✅ Оплата получена. Баланс пополнен на {_money_str(amount_rub)}₽.",
                            reply_markup=self._main_keyboard(user_id),
                        )
                    except Exception:
                        pass
                break
            if status in ("CANCELED", "FAILED", "EXPIRED"):
                try:
                    self.bot.send_message(user_id, "❌ Платёж не был завершён.", reply_markup=self._main_keyboard(user_id))
                except Exception:
                    pass
                break

    # admin flows
    def _show_admin_add_menu(self, chat_id: int, message_id: int | None = None) -> None:
        text = "➕ Выберите способ добавления аккаунта:"
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(
            InlineKeyboardButton("📞 По номеру + код", callback_data=f"{CB}admin_add_phone"),
            InlineKeyboardButton("📋 Вставить session_string", callback_data=f"{CB}admin_add_session_text"),
            InlineKeyboardButton("📎 Загрузить .session / ZIP", callback_data=f"{CB}admin_upload_session"),
            InlineKeyboardButton("🗂 В кастомную категорию", callback_data=f"{CB}admin_add_custom"),
            InlineKeyboardButton("🔙 Назад", callback_data=f"{CB}admin"),
        )
        if message_id:
            self.bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)
        else:
            self.bot.send_message(chat_id, text, reply_markup=kb)

    def _handle_admin_add(self, m: Message) -> None:
        # legacy / fallback handler; now individual states
        self.user_states.pop(m.from_user.id, None)
        self.bot.send_message(m.chat.id, "Используйте админ-меню для добавления аккаунтов.", reply_markup=self._admin_keyboard())

    def _handle_admin_add_session_text(self, m: Message) -> None:
        state = self.user_states.get(m.from_user.id, {})
        step = state.get("step")
        if step == "price":
            self._save_price_then_accounts(
                m.from_user.id, m.chat.id, m.text or "0",
                state.get("created_categories", []), state.get("pending_lines", [])
            )
            return

        lines = (m.text or "").strip().splitlines()
        errors: list[str] = []
        created_categories: list[str] = []
        pending_lines: list[tuple[str, str, str]] = []  # cat, phone, session

        for line in lines:
            line = line.strip()
            if not line:
                continue
            cat: str | None = None
            data = line
            if ":" in line:
                maybe_cat, maybe_data = line.split(":", 1)
                maybe_cat = maybe_cat.strip()
                maybe_data = maybe_data.strip()
                if maybe_cat and "|" in maybe_data:
                    cat = maybe_cat
                    data = maybe_data
            if "|" not in data:
                errors.append(f"Пропущено (нет |): {line}")
                continue
            phone, session = data.split("|", 1)
            phone = phone.strip()
            session = session.strip()
            if not phone or not session:
                errors.append(f"Пустой телефон/сессия: {line}")
                continue
            if cat is None:
                cat = _detect_country(phone)
            cat_row = self.storage.ensure_category(cat)
            if cat_row.get("price", 0) == 0 and cat not in created_categories:
                created_categories.append(cat)
            pending_lines.append((cat, phone, session))

        if created_categories:
            self.user_states[m.from_user.id] = {
                "state": "admin_add_session_text", "step": "price",
                "created_categories": created_categories, "pending_lines": pending_lines,
                "errors": errors, "proxy": state.get("proxy"),
            }
            self.bot.send_message(
                m.chat.id,
                f"🆕 Новые категории: {', '.join(created_categories)}.\n"
                f"Введите цену для них (одна цена на все новые категории, ₽):",
                reply_markup=self._back_keyboard(f"{CB}admin"),
            )
            return

        added = self._add_pending_lines(m.from_user.id, pending_lines)
        msg = f"✅ Добавлено аккаунтов: {added}\n"
        if errors:
            msg += f"⚠️ Ошибок: {len(errors)}\n" + "\n".join(errors[:10])
        self.user_states.pop(m.from_user.id, None)
        self.bot.send_message(m.chat.id, msg, reply_markup=self._admin_keyboard())

    def _save_price_then_accounts(self, user_id: int, chat_id: int, price_text: str, created_categories: list[str], pending_lines: list[tuple[str, str, str]]) -> None:
        try:
            price = _to_dec(price_text)
        except Exception:
            self.bot.send_message(chat_id, "❌ Введите число.", reply_markup=self._admin_keyboard())
            return
        for cat_name in created_categories:
            cat = self.storage.get_category_by_name(cat_name)
            if cat:
                self.storage.update_category_price(cat["id"], float(price))
        added = self._add_pending_lines(user_id, pending_lines)
        self.user_states.pop(user_id, None)
        self.bot.send_message(chat_id, f"✅ Добавлено аккаунтов: {added}. Цена {price}₽ установлена.", reply_markup=self._admin_keyboard())

    def _add_pending_lines(self, user_id: int, pending_lines: list[tuple[str, str, str]]) -> int:
        user_state = self.user_states.get(user_id, {})
        proxy = user_state.get("proxy")
        added = 0
        for cat, phone, session in pending_lines:
            cat_row = self.storage.ensure_category(cat)
            self.storage.add_account(cat_row["id"], phone, session, proxy=proxy)
            added += 1
        return added

    def _handle_admin_add_custom(self, m: Message) -> None:
        state = self.user_states.get(m.from_user.id, {})
        step = state.get("step")
        if step == "category":
            cat_name = (m.text or "").strip()
            if not cat_name:
                cat_name = "Другое"
            cat = self.storage.ensure_category(cat_name)
            if cat.get("price", 0) == 0:
                self.user_states[m.from_user.id] = {"state": "admin_add_custom", "step": "price", "category_name": cat_name}
                self.bot.send_message(m.chat.id, "💰 Эта категория новая. Введите цену (₽):")
                return
            self.user_states[m.from_user.id] = {"state": "admin_add_custom", "step": "account", "category_name": cat_name, "category_id": cat["id"]}
            self.bot.send_message(
                m.chat.id,
                "📋 Введите аккаунт: <code>phone|session_string</code>\n"
                "Или несколько — по одному на строке.",
                parse_mode="HTML",
            )
            return
        if step == "price":
            try:
                price = _to_dec(m.text)
            except Exception:
                self.bot.send_message(m.chat.id, "❌ Введите число.")
                return
            cat_name = state.get("category_name", "Другое")
            cat = self.storage.ensure_category(cat_name)
            self.storage.update_category_price(cat["id"], float(price))
            self.user_states[m.from_user.id] = {"state": "admin_add_custom", "step": "account", "category_name": cat_name, "category_id": cat["id"]}
            self.bot.send_message(
                m.chat.id,
                "📋 Введите аккаунт: <code>phone|session_string</code>\n"
                "Или несколько — по одному на строке.",
                parse_mode="HTML",
            )
            return
        if step == "account":
            category_id = state.get("category_id")
            lines = (m.text or "").strip().splitlines()
            added = 0
            for line in lines:
                line = line.strip()
                if not line or "|" not in line:
                    continue
                phone, session = line.split("|", 1)
                phone = phone.strip()
                session = session.strip()
                if not phone or not session:
                    continue
                self.storage.add_account(category_id, phone, session, proxy=state.get("proxy"))
                added += 1
            self.user_states.pop(m.from_user.id, None)
            self.bot.send_message(m.chat.id, f"✅ Добавлено аккаунтов: {added}", reply_markup=self._admin_keyboard())

    def _handle_admin_add_phone(self, m: Message) -> None:
        state = self.user_states.get(m.from_user.id, {})
        step = state.get("step")
        api_id, api_hash = _api_config()
        if not api_id or not api_hash:
            self.bot.send_message(m.chat.id, "❌ API ID/API Hash не настроены.")
            self.user_states.pop(m.from_user.id, None)
            return

        if step in (None, "phone"):
            phone = (m.text or m.caption or "").strip()
            if not phone and getattr(m, "contact", None):
                phone = (m.contact.phone_number or "").strip()
            if not phone:
                self.bot.send_message(m.chat.id, "❌ Введите номер телефона.")
                return
            proxy_text = None
            if "|" in phone:
                phone, proxy_text = phone.split("|", 1)
                phone = phone.strip()
                proxy_text = proxy_text.strip()
            proxy = _parse_proxy(proxy_text) or _get_default_proxy()
            q: queue.Queue = queue.Queue()
            self.user_states[m.from_user.id] = {"state": "admin_add_phone", "step": "code", "phone": phone, "proxy": proxy, "login_queue": q}
            threading.Thread(
                target=_login_worker,
                args=(m.chat.id, m.from_user.id, phone, proxy, api_id, api_hash, self.bot, self.storage, self.user_states),
                daemon=True,
            ).start()
            return

        if step == "code":
            code = (m.text or m.caption or "").strip()
            q = state.get("login_queue")
            if q:
                q.put(code)
                self.user_states[m.from_user.id] = {**state, "step": "password"}
            else:
                self.user_states.pop(m.from_user.id, None)
            return

        if step == "password":
            password = (m.text or m.caption or "").strip()
            q = state.get("login_queue")
            if q:
                q.put(password)
            self.user_states.pop(m.from_user.id, None)
            return

        if step == "set_price":
            try:
                price = _to_dec(m.text)
            except Exception:
                self.bot.send_message(m.chat.id, "❌ Введите число.", reply_markup=self._back_keyboard(f"{CB}admin"))
                return
            category_id = state.get("category_id")
            if category_id:
                self.storage.update_category_price(category_id, float(price))
            phone = state.get("phone")
            session_string = state.get("session_string")
            proxy = state.get("proxy")
            if phone and session_string:
                self.storage.add_account(category_id, phone, session_string, proxy=proxy)
                self.bot.send_message(
                    m.chat.id,
                    f"✅ Аккаунт {phone} добавлен. Цена {price}₽ установлена.",
                    reply_markup=self._admin_keyboard(),
                )
            else:
                self.bot.send_message(m.chat.id, f"✅ Цена {price}₽ установлена.", reply_markup=self._admin_keyboard())
            self.user_states.pop(m.from_user.id, None)

    def _handle_admin_price(self, m: Message) -> None:
        state = self.user_states.get(m.from_user.id, {})
        category_id = state.get("category_id")
        if state.get("step") == "set_price":
            try:
                price = _to_dec(m.text)
            except Exception:
                self.bot.send_message(m.chat.id, "❌ Введите число.", reply_markup=self._back_keyboard(f"{CB}admin"))
                return
            if category_id:
                self.storage.update_category_price(category_id, float(price))
            phone = state.get("phone")
            session_string = state.get("session_string")
            proxy = state.get("proxy")
            if phone and session_string:
                self.storage.add_account(category_id, phone, session_string, proxy=proxy)
                self.bot.send_message(
                    m.chat.id,
                    f"✅ Аккаунт {phone} добавлен. Цена {price}₽ установлена.",
                    reply_markup=self._admin_keyboard(),
                )
            else:
                self.bot.send_message(m.chat.id, f"✅ Цена {price}₽ установлена.", reply_markup=self._admin_keyboard())
            self.user_states.pop(m.from_user.id, None)
            return
        try:
            price = _to_dec(m.text)
        except Exception:
            self.bot.send_message(m.chat.id, "❌ Введите число.", reply_markup=self._back_keyboard(f"{CB}admin_categories"))
            return
        if category_id:
            self.storage.update_category_price(category_id, float(price))
        self.user_states.pop(m.from_user.id, None)
        self.bot.send_message(m.chat.id, "✅ Цена обновлена.", reply_markup=self._admin_keyboard())

    def _handle_admin_topup(self, m: Message) -> None:
        state = self.user_states.get(m.from_user.id, {})
        step = state.get("step")
        if step == "user_id":
            try:
                target = int(m.text.strip())
            except Exception:
                self.bot.send_message(m.chat.id, "❌ Введите ID пользователя.")
                return
            self.user_states[m.from_user.id] = {"state": "admin_topup", "step": "amount", "target": target}
            self.bot.send_message(m.chat.id, "Введите сумму пополнения:")
            return
        if step == "amount":
            try:
                amount = _to_dec(m.text)
            except Exception:
                self.bot.send_message(m.chat.id, "❌ Введите сумму.")
                return
            target = state.get("target")
            if target:
                self.storage.update_balance(target, amount)
                try:
                    self.bot.send_message(target, f"💰 Ваш баланс пополнен на {_money_str(amount)}₽")
                except Exception:
                    pass
            self.user_states.pop(m.from_user.id, None)
            self.bot.send_message(m.chat.id, "✅ Баланс пополнен.", reply_markup=self._admin_keyboard())

    def _handle_admin_confirm_payment(self, m: Message) -> None:
        try:
            deposit_id = int(m.text.strip())
        except Exception:
            self.bot.send_message(m.chat.id, "❌ Введите ID депозита.")
            return
        deposit = self.storage.get_deposit_by_external(str(deposit_id))
        if not deposit:
            deposit = self.storage.get_deposit_by_payload(str(deposit_id))
        if not deposit:
            self.bot.send_message(m.chat.id, "❌ Депозит не найден.")
            return
        self.storage.confirm_deposit(deposit["id"])
        try:
            self.bot.send_message(deposit["user_id"], f"💰 Ваш баланс пополнен на {_money_str(deposit['amount_rub'])}₽")
        except Exception:
            pass
        self.user_states.pop(m.from_user.id, None)
        self.bot.send_message(m.chat.id, "✅ Депозит подтверждён.", reply_markup=self._admin_keyboard())

    def _handle_admin_create_category(self, m: Message) -> None:
        state = self.user_states.get(m.from_user.id, {})
        step = state.get("step")
        if step == "name":
            name = (m.text or "").strip()
            if not name:
                self.bot.send_message(m.chat.id, "❌ Введите название.")
                return
            self.user_states[m.from_user.id] = {"state": "admin_create_category", "step": "price", "name": name}
            self.bot.send_message(m.chat.id, "💰 Введите цену (₽):")
            return
        if step == "price":
            try:
                price = _to_dec(m.text)
            except Exception:
                self.bot.send_message(m.chat.id, "❌ Введите число.")
                return
            name = state.get("name", "Другое")
            cat = self.storage.ensure_category(name, float(price))
            self.user_states.pop(m.from_user.id, None)
            self.bot.send_message(m.chat.id, f"✅ Категория {cat['name']} создана.", reply_markup=self._admin_keyboard())

    def _handle_upload_session(self, m: Message) -> None:
        _ensure_event_loop()
        state = self.user_states.get(m.from_user.id, {})
        if not m.document:
            self.bot.send_message(m.chat.id, "❌ Отправьте файл.")
            return
        file_name = m.document.file_name or ""
        try:
            file_info = self.bot.get_file(m.document.file_id)
            downloaded = self.bot.download_file(file_info.file_path)
        except Exception:
            logger.exception("download session file")
            self.bot.send_message(m.chat.id, "❌ Не удалось скачать файл.")
            return
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        temp_dir = SESSIONS_DIR / f"upload_{m.from_user.id}_{int(_now())}"
        temp_dir.mkdir(parents=True, exist_ok=True)
        saved_path = temp_dir / file_name
        saved_path.write_bytes(downloaded)

        files: list[Path] = []
        if file_name.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(saved_path, "r") as zf:
                    zf.extractall(temp_dir)
                for p in temp_dir.rglob("*.session"):
                    if not p.name.endswith("-journal"):
                        files.append(p)
            except Exception:
                self.bot.send_message(m.chat.id, "❌ Не удалось распаковать ZIP.")
                return
        elif file_name.lower().endswith(".session") and not file_name.endswith("-journal"):
            files.append(saved_path)
        else:
            self.bot.send_message(m.chat.id, "❌ Нужен .session файл или ZIP.")
            return

        api_id, api_hash = _api_config()
        if not api_id or not api_hash:
            self.bot.send_message(m.chat.id, "❌ API ID/API Hash не настроены.")
            self.user_states.pop(m.from_user.id, None)
            return

        proxy = state.get("proxy")
        added = 0
        errors: list[str] = []
        for path in files:
            try:
                # use a stable unique name per file
                session_name = f"sess_{path.stem}_{int(_now())}"
                target = SESSIONS_DIR / f"{session_name}.session"
                shutil.copy(path, target)
                client = TelegramClient(str(target), api_id=api_id, api_hash=api_hash, proxy=_proxy_for_telethon(proxy))
                client.connect()
                if not client.is_user_authorized():
                    raise RuntimeError("session not authorized")
                session_string = StringSession.save(client.session)
                try:
                    me = client.get_me()
                    phone = me.phone or path.stem
                except Exception:
                    phone = path.stem
                client.disconnect()
                cat_name = _detect_country(str(phone))
                cat = self.storage.ensure_category(cat_name)
                self.storage.add_account(cat["id"], str(phone), session_string, proxy=proxy)
                added += 1
                try:
                    target.unlink()
                except Exception:
                    pass
            except Exception as e:
                errors.append(f"{path.name}: {e}")

        try:
            shutil.rmtree(temp_dir)
        except Exception:
            pass

        self.user_states.pop(m.from_user.id, None)
        msg = f"✅ Добавлено аккаунтов из файлов: {added}\n"
        if errors:
            msg += f"⚠️ Ошибок: {len(errors)}\n" + "\n".join(errors[:10])
        self.bot.send_message(m.chat.id, msg, reply_markup=self._admin_keyboard())

    def _admin_accounts(self, user_id: int, chat_id: int, message_id: int) -> None:
        categories = self.storage.get_categories()
        if not categories:
            self.bot.edit_message_text("😔 Нет категорий.", chat_id, message_id, reply_markup=self._admin_keyboard())
            return
        kb = InlineKeyboardMarkup(row_width=1)
        for cat in categories:
            available = len(self.storage.get_accounts(cat["id"], STATUS_AVAILABLE))
            kb.add(InlineKeyboardButton(f"{cat['name']} ({available} шт)", callback_data=f"{CB}admin_accounts_cat:{cat['id']}"))
        kb.add(InlineKeyboardButton("🔙 Назад", callback_data=f"{CB}admin"))
        self.bot.edit_message_text("🗂 Выберите категорию:", chat_id, message_id, reply_markup=kb)

    def _admin_accounts_cat(self, category_id: int, chat_id: int, message_id: int) -> None:
        cat = self.storage.get_category(category_id)
        accounts = self.storage.get_accounts(category_id)
        if not cat:
            self.bot.edit_message_text("❌ Категория не найдена.", chat_id, message_id, reply_markup=self._admin_keyboard())
            return
        if not accounts:
            text = f"😔 В категории {cat['name']} нет аккаунтов."
            kb = self._back_keyboard(f"{CB}admin_accounts")
        else:
            text = f"📋 {cat['name']} — {_money_str(cat['price'])}₽\n\n"
            kb = InlineKeyboardMarkup(row_width=1)
            for acc in accounts:
                status = "✅" if acc["status"] == STATUS_AVAILABLE else "🛒"
                kb.add(InlineKeyboardButton(
                    f"{status} {acc['phone']} ({acc['status']})",
                    callback_data=f"{CB}admin_delete_acc:{acc['id']}"
                ))
            kb.add(InlineKeyboardButton("🔙 Назад", callback_data=f"{CB}admin_accounts"))
        self.bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)

    def _admin_delete_account(self, account_id: int, chat_id: int, message_id: int) -> None:
        account = self.storage.delete_account(account_id)
        if account:
            self.storage.cleanup_empty_category(account["category_id"])
            text = f"✅ Аккаунт {account['phone']} удалён."
        else:
            text = "❌ Аккаунт не найден."
        self.bot.edit_message_text(text, chat_id, message_id, reply_markup=self._admin_keyboard())

    def _admin_categories(self, chat_id: int, message_id: int) -> None:
        categories = self.storage.get_categories()
        if not categories:
            text = "😔 Нет категорий."
            kb = InlineKeyboardMarkup(row_width=1)
            kb.add(InlineKeyboardButton("➕ Создать категорию", callback_data=f"{CB}admin_create_category"))
            kb.add(InlineKeyboardButton("🔙 Назад", callback_data=f"{CB}admin"))
        else:
            text = "🗂 Категории:\n\n"
            kb = InlineKeyboardMarkup(row_width=1)
            for cat in categories:
                available = len(self.storage.get_accounts(cat["id"], STATUS_AVAILABLE))
                text += f"• {cat['name']} — {_money_str(cat['price'])}₽ ({available} свободно)\n"
                kb.add(InlineKeyboardButton(
                    f"💰 Цена {cat['name']}", callback_data=f"{CB}admin_set_price:{cat['id']}"
                ))
                kb.add(InlineKeyboardButton(
                    f"➕ Добавить в {cat['name']}", callback_data=f"{CB}admin_add_to_cat:{cat['id']}"
                ))
                kb.add(InlineKeyboardButton(
                    f"🗑 Удалить {cat['name']}", callback_data=f"{CB}admin_delete_cat:{cat['id']}"
                ))
            kb.add(InlineKeyboardButton("➕ Создать категорию", callback_data=f"{CB}admin_create_category"))
            kb.add(InlineKeyboardButton("🔙 Назад", callback_data=f"{CB}admin"))
        self.bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)

    def _admin_delete_category(self, category_id: int, chat_id: int, message_id: int) -> None:
        self.storage.delete_category(category_id)
        self.bot.edit_message_text("✅ Категория удалена.", chat_id, message_id, reply_markup=self._admin_keyboard())

    def run(self) -> None:
        logger.info("[TelegramAccountShop] Запуск polling")
        try:
            self.bot.infinity_polling(timeout=60, long_polling_timeout=30)
        except Exception:
            logger.exception("Ошибка polling")

    def stop(self) -> None:
        try:
            self.bot.stop_polling()
        except Exception:
            pass

# --- интеграция с Cardinal ---
def init_plugin(cardinal: Any) -> None:
    global _storage, _shop_bot
    _storage = AccountStorage()
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            for k, v in cfg.items():
                if _storage.get_config(k) is None:
                    _storage.set_config(k, v)
        except Exception:
            logger.exception("Ошибка чтения config.json")

    token = _storage.get_config("bot_token")
    if not token:
        logger.warning("[TelegramAccountShop] bot_token не задан. Создайте storage/cache/tg_account_shop/config.json с bot_token.")
        return

    logger.info("[TelegramAccountShop] Инициализация бота")
    _shop_bot = AccountShopBot(token, _storage, cardinal=cardinal)

    # Регистрация админ-панели в Cardinal ПУ
    try:
        import tg_account_shop_admin
        tg_account_shop_admin.register(cardinal, _storage, _shop_bot, UUID)
    except Exception:
        logger.exception("Ошибка регистрации админ-панели в Cardinal")


def start_plugin(cardinal: Any) -> None:
    if _shop_bot:
        threading.Thread(target=_shop_bot.run, daemon=True, name="TgAccountShopBot").start()


def stop_plugin(cardinal: Any) -> None:
    global _shop_bot
    for client in list(_active_clients.values()):
        _disconnect_client(client)
    _active_clients.clear()
    if _shop_bot:
        _shop_bot.stop()
        _shop_bot = None

BIND_TO_PRE_INIT = [init_plugin]
BIND_TO_POST_START = [start_plugin]
BIND_TO_PRE_STOP = [stop_plugin]
