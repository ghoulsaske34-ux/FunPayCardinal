from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import telebot
from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery

# --- метаданные плагина ---
NAME = "Telegram Account Shop"
VERSION = "2.0.0"
DESCRIPTION = "Telegram-магазин номеров с автовыдачей SMS/кодов через Pyrogram"
CREDITS = "@ghoulsaske34"
UUID = "85d48499-79da-462e-af82-9feb2c399d7c"
SETTINGS_PAGE = False
BIND_TO_DELETE = None

# --- константы ---
CB = "tgshop:"
STORAGE_DIR = Path("storage/cache/tg_account_shop")
DB_FILE = STORAGE_DIR / "shop.db"
CONFIG_FILE = STORAGE_DIR / "config.json"
logger = logging.getLogger("telegram_account_shop")

LISTEN_MINUTES = 5
STATUS_AVAILABLE = "available"
STATUS_SOLD = "sold"
STATUS_LISTENING = "listening"

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
                PRAGMA user_version = 2;
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
                CREATE TABLE IF NOT EXISTS config (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )
            # очистить старые таблицы если они имеют устаревшую схему
            try:
                conn.execute("SELECT data FROM accounts LIMIT 1")
                conn.execute("DROP TABLE accounts")
                conn.execute("""
                    CREATE TABLE accounts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        category_id INTEGER NOT NULL,
                        phone TEXT NOT NULL,
                        session_string TEXT NOT NULL,
                        status TEXT DEFAULT 'available',
                        buyer_id INTEGER,
                        purchased_at REAL,
                        expires_at REAL,
                        last_code TEXT,
                        last_code_at REAL,
                        created_at REAL
                    )
                """)
            except Exception:
                pass
            try:
                conn.execute("SELECT funpay_product FROM categories LIMIT 1")
                conn.execute("DROP TABLE categories")
                conn.execute("""
                    CREATE TABLE categories (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE,
                        description TEXT,
                        price REAL NOT NULL DEFAULT 0,
                        is_active INTEGER DEFAULT 1,
                        sort_order INTEGER DEFAULT 0,
                        created_at REAL
                    )
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
                return dict(existing)
            conn.execute(
                "INSERT INTO categories(name, price, created_at) VALUES(?, ?, ?)",
                (name, float(price), _now())
            )
            row = conn.execute("SELECT * FROM categories WHERE id=?", (conn.lastrowid,)).fetchone()
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
                conn.execute("DELETE FROM categories WHERE id=?", (category_id,))

    # accounts
    def add_account(self, category_id: int, phone: str, session_string: str) -> int:
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO accounts(category_id, phone, session_string, status, created_at) VALUES(?, ?, ?, ?, ?)",
                (category_id, phone, session_string, STATUS_AVAILABLE, _now())
            )
            return cur.lastrowid

    def get_account(self, account_id: int) -> dict[str, Any] | None:
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        return dict(row) if row else None

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
        return {
            "users": total_users,
            "accounts": total_accounts,
            "available": available,
            "sold": sold,
            "revenue": float(revenue),
        }

_storage: AccountStorage | None = None
_shop_bot: "AccountShopBot" | None = None

# --- прослушка кодов через Pyrogram ---
_active_clients: dict[int, Client] = {}
_active_timers: dict[int, threading.Timer] = {}
_listener_lock = threading.Lock()

def _pyrogram_config() -> tuple[int, str]:
    api_id = _storage.get_config("api_id") if _storage else None
    api_hash = _storage.get_config("api_hash") if _storage else None
    if not api_id or not api_hash:
        return 0, ""
    return int(api_id), str(api_hash)

def _build_handler(account_id: int, buyer_id: int, bot: telebot.TeleBot) -> Any:
    def handler(client: Client, message: Any) -> None:
        codes = _extract_codes(message.text or message.caption or "")
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

def _stop_listener(account_id: int, account_id_int: int) -> None:
    with _listener_lock:
        client = _active_clients.pop(account_id_int, None)
        timer = _active_timers.pop(account_id_int, None)
    if timer:
        timer.cancel()
    if client:
        try:
            client.stop()
        except Exception:
            pass
    if _storage:
        _storage.update_account_status(account_id_int, STATUS_SOLD)
        account = _storage.get_account(account_id_int)
        if account:
            _storage.cleanup_empty_category(account["category_id"])

def start_listener(account_id: int, buyer_id: int, bot: telebot.TeleBot) -> str:
    if _storage is None:
        return "storage_not_ready"
    account = _storage.get_account(account_id)
    if not account:
        return "account_not_found"
    api_id, api_hash = _pyrogram_config()
    if not api_id or not api_hash:
        return "no_api_config"

    with _listener_lock:
        # остановить предыдущий если есть
        old = _active_clients.pop(account_id, None)
        if old:
            try:
                old.stop()
            except Exception:
                pass
        timer = _active_timers.pop(account_id, None)
        if timer:
            timer.cancel()

    def run_client() -> None:
        client = Client(
            f"acc_{account_id}",
            api_id=api_id,
            api_hash=api_hash,
            session_string=account["session_string"],
            in_memory=True,
            no_updates=False,
            skip_updates=True,
        )
        client.add_handler(MessageHandler(_build_handler(account_id, buyer_id, bot), filters.text))
        with _listener_lock:
            _active_clients[account_id] = client
        try:
            client.run()
        except Exception:
            logger.exception("Ошибка прослушки аккаунта %s", account_id)
        finally:
            _stop_listener(account_id, account_id)

    thread = threading.Thread(target=run_client, daemon=True, name=f"TgShopListen-{account_id}")
    thread.start()

    expires = _now() + LISTEN_MINUTES * 60
    _storage.update_account_status(account_id, STATUS_LISTENING, expires_at=expires)

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

    api_id, api_hash = _pyrogram_config()
    if not api_id or not api_hash:
        return cached

    def fetch() -> str | None:
        client = Client(
            f"acc_{account_id}_fetch",
            api_id=api_id,
            api_hash=api_hash,
            session_string=account["session_string"],
            in_memory=True,
            no_updates=True,
        )
        best_code: str | None = None
        best_time: float = 0
        try:
            client.start()
            for dialog in client.get_dialogs(limit=30):
                msg = dialog.top_message
                if not msg or not msg.date:
                    continue
                ts = msg.date.timestamp()
                codes = _extract_codes(msg.text or msg.caption or "")
                if codes and ts > best_time:
                    best_code = codes[0]
                    best_time = ts
            client.stop()
        except Exception:
            logger.exception("Ошибка получения кода для аккаунта %s", account_id)
            try:
                client.stop()
            except Exception:
                pass
        return best_code

    thread = threading.Thread(target=lambda: None, daemon=True)
    result: list[str | None] = [None]
    def run() -> None:
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
        self._ensure_setup_password()
        self._setup_handlers()
        try:
            self.bot.set_my_commands([
                telebot.types.BotCommand("start", "Главное меню"),
                telebot.types.BotCommand("balance", "Мой баланс"),
                telebot.types.BotCommand("support", "Поддержка"),
            ])
        except Exception:
            pass

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

    # keyboards
    def _main_keyboard(self, user_id: int) -> InlineKeyboardMarkup:
        kb = InlineKeyboardMarkup(row_width=2)
        if self._is_admin(user_id):
            kb.add(
                InlineKeyboardButton("🛒 Купить номер", callback_data=f"{CB}buy"),
                InlineKeyboardButton("📱 Мои номера", callback_data=f"{CB}my_numbers"),
                InlineKeyboardButton("💰 Баланс", callback_data=f"{CB}balance"),
                InlineKeyboardButton("🛠 Админка", callback_data=f"{CB}admin"),
            )
        else:
            kb.add(
                InlineKeyboardButton("🛒 Купить номер", callback_data=f"{CB}buy"),
                InlineKeyboardButton("📱 Мои номера", callback_data=f"{CB}my_numbers"),
                InlineKeyboardButton("💰 Баланс", callback_data=f"{CB}balance"),
                InlineKeyboardButton("🆘 Поддержка", callback_data=f"{CB}support"),
            )
        return kb

    def _admin_keyboard(self) -> InlineKeyboardMarkup:
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton("➕ Добавить аккаунты", callback_data=f"{CB}admin_add"),
            InlineKeyboardButton("📋 Список аккаунтов", callback_data=f"{CB}admin_accounts"),
            InlineKeyboardButton("🗂 Категории/цены", callback_data=f"{CB}admin_categories"),
            InlineKeyboardButton("💳 Пополнить баланс", callback_data=f"{CB}admin_topup"),
            InlineKeyboardButton("📊 Статистика", callback_data=f"{CB}admin_stats"),
            InlineKeyboardButton("🔙 Назад", callback_data=f"{CB}main"),
        )
        return kb

    def _back_keyboard(self, payload: str) -> InlineKeyboardMarkup:
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("🔙 Назад", callback_data=payload))
        return kb

    # handlers setup
    def _setup_handlers(self) -> None:
        @self.bot.message_handler(commands=["start"])
        def on_start(m: Message) -> None:
            try:
                user = self.storage.get_user(m.from_user.id, m.from_user.username)
                text = (
                    f"👋 Привет, {m.from_user.first_name}!\n"
                    f"💰 Баланс: {_money_str(user.get('balance', 0))}₽\n\n"
                    "Выберите действие:"
                )
                self.bot.send_message(m.chat.id, text, reply_markup=self._main_keyboard(m.from_user.id))
            except Exception:
                logger.exception("[TelegramAccountShop] Ошибка /start")

        @self.bot.message_handler(commands=["setup"])
        def on_setup(m: Message) -> None:
            text = m.text or ""
            parts = text.split(maxsplit=1)
            if len(parts) != 2 or parts[1].strip().upper() != (self._setup_password or ""):
                self.bot.send_message(m.chat.id, "❌ Неверный код настройки.")
                return
            self.storage.set_admin(m.from_user.id)
            self.bot.send_message(m.chat.id, "✅ Вы назначены администратором.", reply_markup=self._main_keyboard(m.from_user.id))

        @self.bot.message_handler(func=lambda m: self.user_states.get(m.from_user.id, {}).get("state") is not None)
        def on_state(m: Message) -> None:
            state = self.user_states.get(m.from_user.id, {})
            handler = state.get("state")
            if handler == "admin_add":
                self._handle_admin_add(m)
            elif handler == "admin_price":
                self._handle_admin_price(m)
            elif handler == "admin_topup":
                self._handle_admin_topup(m)
            elif handler == "support":
                self._handle_support(m)
            else:
                self.user_states.pop(m.from_user.id, None)

        @self.bot.callback_query_handler(func=lambda c: c.data.startswith(CB))
        def on_callback(c: CallbackQuery) -> None:
            self._handle_callback(c)

    # admin flows
    def _handle_admin_add(self, m: Message) -> None:
        state = self.user_states.get(m.from_user.id, {})
        step = state.get("step")

        if step == "category":
            default_cat = (m.text or "").strip()
            self.user_states[m.from_user.id] = {"state": "admin_add", "step": "accounts", "default_cat": default_cat}
            self.bot.send_message(
                m.chat.id,
                "Введите аккаунты: по одному на строке.\n"
                "Формат: <code>phone|session_string</code>\n"
                "Или с категорией: <code>Категория: phone|session_string</code>\n"
                "Категории создадутся/удалятся автоматически.",
                parse_mode="HTML",
            )
            return

        if step == "accounts":
            default_cat = state.get("default_cat", "Другое")
            lines = (m.text or "").strip().splitlines()
            added = 0
            errors: list[str] = []
            created_categories: set[str] = set()
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                cat = default_cat
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
                category = self.storage.ensure_category(cat)
                created_categories.add(cat)
                self.storage.add_account(category["id"], phone, session)
                added += 1

            for cat_name in created_categories:
                cat = self.storage.get_category_by_name(cat_name)
                if cat and cat.get("price", 0) == 0:
                    self.storage.update_category_price(cat["id"], 0)

            self.user_states.pop(m.from_user.id, None)
            msg = f"✅ Добавлено аккаунтов: {added}\n"
            if errors:
                msg += f"⚠️ Ошибок: {len(errors)}\n" + "\n".join(errors[:10])
            self.bot.send_message(m.chat.id, msg, reply_markup=self._admin_keyboard())

    def _handle_admin_price(self, m: Message) -> None:
        state = self.user_states.get(m.from_user.id, {})
        category_id = state.get("category_id")
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

    def _handle_support(self, m: Message) -> None:
        self.user_states.pop(m.from_user.id, None)
        self._notify_admins(f"🆘 Поддержка от @{m.from_user.username or m.from_user.id} (ID {m.from_user.id}):\n{m.text}")
        self.bot.send_message(m.chat.id, "✅ Сообщение отправлено администратору.", reply_markup=self._main_keyboard(m.from_user.id))

    # callback dispatcher
    def _handle_callback(self, c: CallbackQuery) -> None:
        data = c.data[len(CB):]
        parts = data.split(":", 3)
        action = parts[0]

        if action == "main":
            user = self.storage.get_user(c.from_user.id)
            text = (
                f"👋 Привет, {c.from_user.first_name}!\n"
                f"💰 Баланс: {_money_str(user.get('balance', 0))}₽\n\n"
                "Выберите действие:"
            )
            self.bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=self._main_keyboard(c.from_user.id))

        elif action == "buy":
            self._show_categories(c.from_user.id, c.message.chat.id, c.message.message_id)
        elif action == "category":
            self._show_category(int(parts[1]), c.from_user.id, c.message.chat.id, c.message.message_id)
        elif action == "purchase":
            self._purchase(int(parts[1]), c.from_user.id, c.message.chat.id, c.message.message_id)
        elif action == "my_numbers":
            self._show_my_numbers(c.from_user.id, c.message.chat.id, c.message.message_id)
        elif action == "get_code":
            self._get_code(int(parts[1]), c.from_user.id, c.message.chat.id, c.message.message_id)
        elif action == "listen":
            self._listen(int(parts[1]), c.from_user.id, c.message.chat.id, c.message.message_id)
        elif action == "stop_listen":
            self._stop_listen(int(parts[1]), c.from_user.id, c.message.chat.id, c.message.message_id)

        elif action == "balance":
            user = self.storage.get_user(c.from_user.id)
            text = f"💰 Ваш баланс: {_money_str(user.get('balance', 0))}₽\n💸 Всего потрачено: {_money_str(user.get('total_spent', 0))}₽"
            self.bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=self._back_keyboard(f"{CB}main"))
        elif action == "support":
            self.user_states[c.from_user.id] = {"state": "support"}
            self.bot.edit_message_text("🆘 Опишите проблему:", c.message.chat.id, c.message.message_id)

        elif action == "admin":
            if not self._is_admin(c.from_user.id):
                self.bot.answer_callback_query(c.id, "Нет доступа")
                return
            self.bot.edit_message_text("🛠 Админ-меню", c.message.chat.id, c.message.message_id, reply_markup=self._admin_keyboard())
        elif action == "admin_add":
            self.user_states[c.from_user.id] = {"state": "admin_add", "step": "category"}
            self.bot.edit_message_text(
                "Введите название категории по умолчанию (или оставьте пустым для 'Другое'):",
                c.message.chat.id, c.message.message_id
            )
        elif action == "admin_accounts":
            self._admin_accounts(c.from_user.id, c.message.chat.id, c.message.message_id)
        elif action == "admin_delete_acc":
            self._admin_delete_account(int(parts[1]), c.message.chat.id, c.message.message_id)
        elif action == "admin_categories":
            self._admin_categories(c.message.chat.id, c.message.message_id)
        elif action == "admin_set_price":
            self.user_states[c.from_user.id] = {"state": "admin_price", "category_id": int(parts[1])}
            self.bot.edit_message_text("Введите новую цену:", c.message.chat.id, c.message.message_id)
        elif action == "admin_delete_cat":
            self._admin_delete_category(int(parts[1]), c.message.chat.id, c.message.message_id)
        elif action == "admin_topup":
            self.user_states[c.from_user.id] = {"state": "admin_topup", "step": "user_id"}
            self.bot.edit_message_text("Введите ID пользователя для пополнения:", c.message.chat.id, c.message.message_id)
        elif action == "admin_stats":
            stats = self.storage.get_stats()
            text = (
                f"📊 Статистика\n"
                f"👥 Пользователей: {stats['users']}\n"
                f"📱 Аккаунтов: {stats['accounts']} (свободно: {stats['available']}, продано: {stats['sold']})\n"
                f"💸 Выручка: {_money_str(stats['revenue'])}₽"
            )
            self.bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=self._admin_keyboard())

    # user flows
    def _show_categories(self, user_id: int, chat_id: int, message_id: int | None = None) -> None:
        categories = self.storage.get_categories()
        if not categories:
            text = "😔 Пока нет доступных номеров."
            kb = self._back_keyboard(f"{CB}main")
        else:
            text = "🗂 Выберите категорию:"
            kb = InlineKeyboardMarkup(row_width=2)
            for cat in categories:
                available = len(self.storage.get_accounts(cat["id"], STATUS_AVAILABLE))
                kb.add(InlineKeyboardButton(
                    f"{cat['name']} — {_money_str(cat['price'])}₽ ({available})",
                    callback_data=f"{CB}category:{cat['id']}"
                ))
            kb.add(InlineKeyboardButton("🔙 Назад", callback_data=f"{CB}main"))
        if message_id:
            self.bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)
        else:
            self.bot.send_message(chat_id, text, reply_markup=kb)

    def _show_category(self, category_id: int, user_id: int, chat_id: int, message_id: int) -> None:
        cat = self.storage.get_category(category_id)
        if not cat:
            self.bot.edit_message_text("❌ Категория не найдена.", chat_id, message_id, reply_markup=self._back_keyboard(f"{CB}buy"))
            return
        available = len(self.storage.get_accounts(category_id, STATUS_AVAILABLE))
        user = self.storage.get_user(user_id)
        text = (
            f"📱 {cat['name']}\n"
            f"💰 Цена: {_money_str(cat['price'])}₽\n"
            f"📦 Доступно: {available}\n"
            f"💳 Ваш баланс: {_money_str(user.get('balance', 0))}₽"
        )
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("🛒 Купить", callback_data=f"{CB}purchase:{category_id}"))
        kb.add(InlineKeyboardButton("🔙 Назад", callback_data=f"{CB}buy"))
        self.bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)

    def _purchase(self, category_id: int, user_id: int, chat_id: int, message_id: int) -> None:
        cat = self.storage.get_category(category_id)
        if not cat:
            self.bot.edit_message_text("❌ Категория не найдена.", chat_id, message_id, reply_markup=self._back_keyboard(f"{CB}buy"))
            return
        user = self.storage.get_user(user_id)
        balance = _to_dec(user.get("balance", 0))
        price = _to_dec(cat["price"])
        if balance < price:
            self.bot.edit_message_text(
                f"❌ Недостаточно средств. Пополните баланс через администратора.\n💰 Нужно: {_money_str(price)}₽",
                chat_id, message_id, reply_markup=self._back_keyboard(f"{CB}buy")
            )
            return
        account = self.storage.get_available_account(category_id)
        if not account:
            self.bot.edit_message_text("😔 В этой категории закончились номера.", chat_id, message_id, reply_markup=self._back_keyboard(f"{CB}buy"))
            return

        self.storage.update_balance(user_id, -price)
        self.storage.add_spent(user_id, price)
        self.storage.update_account_status(account["id"], STATUS_SOLD, user_id, _now())
        self.storage.add_purchase(user_id, account["id"], float(price))

        text = (
            f"✅ Вы купили номер!\n\n"
            f"📞 Номер: <code>{account['phone']}</code>\n"
            f"🆔 ID аккаунта: <code>{account['id']}</code>\n\n"
            "Используйте кнопки ниже для получения кодов."
        )
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton("🔄 Получить код", callback_data=f"{CB}get_code:{account['id']}"),
            InlineKeyboardButton(f"👂 Прослушка {LISTEN_MINUTES} мин", callback_data=f"{CB}listen:{account['id']}"),
            InlineKeyboardButton("🔙 Мои номера", callback_data=f"{CB}my_numbers"),
        )
        self.bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)

    def _show_my_numbers(self, user_id: int, chat_id: int, message_id: int | None = None) -> None:
        accounts = self.storage.get_user_accounts(user_id)
        if not accounts:
            text = "📭 У вас пока нет купленных номеров."
            kb = self._back_keyboard(f"{CB}main")
        else:
            text = "📱 Ваши номера:\n\n"
            for acc in accounts:
                text += f"📞 <code>{acc['phone']}</code> — {acc['status']}"
                if acc.get("last_code"):
                    text += f"\n   Последний код: <code>{acc['last_code']}</code> ({_fmt_ts(acc.get('last_code_at'))})"
                text += "\n\n"
            kb = InlineKeyboardMarkup(row_width=2)
            for acc in accounts:
                kb.add(
                    InlineKeyboardButton(f"🔄 {acc['phone']}", callback_data=f"{CB}get_code:{acc['id']}"),
                    InlineKeyboardButton(f"👂 {acc['phone']}", callback_data=f"{CB}listen:{acc['id']}")
                )
            kb.add(InlineKeyboardButton("🔙 Назад", callback_data=f"{CB}main"))
        if message_id:
            self.bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)
        else:
            self.bot.send_message(chat_id, text, reply_markup=kb)

    def _get_code(self, account_id: int, user_id: int, chat_id: int, message_id: int) -> None:
        account = self.storage.get_account(account_id)
        if not account or account.get("buyer_id") != user_id:
            self.bot.answer_callback_query(message_id, "Номер не найден")
            return
        self.bot.edit_message_text("⏳ Получаю код...", chat_id, message_id)
        code = get_latest_code(account_id)
        if code:
            text = f"📞 Номер: <code>{account['phone']}</code>\n🔢 Код: <code>{code}</code>"
        else:
            text = f"📞 Номер: <code>{account['phone']}</code>\n😔 Код пока не пришёл. Попробуйте позже или включите прослушку."
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton("🔄 Обновить", callback_data=f"{CB}get_code:{account_id}"),
            InlineKeyboardButton(f"👂 Прослушка {LISTEN_MINUTES} мин", callback_data=f"{CB}listen:{account_id}"),
            InlineKeyboardButton("🔙 Мои номера", callback_data=f"{CB}my_numbers"),
        )
        self.bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)

    def _listen(self, account_id: int, user_id: int, chat_id: int, message_id: int) -> None:
        account = self.storage.get_account(account_id)
        if not account or account.get("buyer_id") != user_id:
            self.bot.answer_callback_query(message_id, "Номер не найден")
            return
        self.bot.edit_message_text("⏳ Запускаю прослушку...", chat_id, message_id)
        result = start_listener(account_id, user_id, self.bot)
        if result == "no_api_config":
            text = "❌ API ID/API Hash не настроены. Обратитесь к администратору."
        elif result == "ok":
            text = (
                f"👂 Прослушка номера <code>{account['phone']}</code> запущена на {LISTEN_MINUTES} мин.\n"
                "Все коды будут приходить сюда автоматически."
            )
        else:
            text = "❌ Не удалось запустить прослушку."
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("🛑 Остановить", callback_data=f"{CB}stop_listen:{account_id}"))
        kb.add(InlineKeyboardButton("🔙 Мои номера", callback_data=f"{CB}my_numbers"))
        self.bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)

    def _stop_listen(self, account_id: int, user_id: int, chat_id: int, message_id: int) -> None:
        account = self.storage.get_account(account_id)
        if not account or account.get("buyer_id") != user_id:
            self.bot.answer_callback_query(message_id, "Номер не найден")
            return
        stop_listener(account_id)
        self.bot.edit_message_text(
            f"🛑 Прослушка номера <code>{account['phone']}</code> остановлена.",
            chat_id, message_id, reply_markup=self._back_keyboard(f"{CB}my_numbers")
        )

    # admin flows
    def _admin_accounts(self, user_id: int, chat_id: int, message_id: int) -> None:
        categories = self.storage.get_categories()
        if not categories:
            self.bot.edit_message_text("😔 Нет категорий.", chat_id, message_id, reply_markup=self._admin_keyboard())
            return
        kb = InlineKeyboardMarkup()
        for cat in categories:
            kb.add(InlineKeyboardButton(cat["name"], callback_data=f"{CB}admin_accounts_cat:{cat['id']}"))
        kb.add(InlineKeyboardButton("🔙 Назад", callback_data=f"{CB}admin"))
        self.bot.edit_message_text("🗂 Выберите категорию:", chat_id, message_id, reply_markup=kb)

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
            kb = self._admin_keyboard()
        else:
            text = "🗂 Категории:\n\n"
            kb = InlineKeyboardMarkup(row_width=1)
            for cat in categories:
                available = len(self.storage.get_accounts(cat["id"], STATUS_AVAILABLE))
                text += f"• {cat['name']} — {_money_str(cat['price'])}₽ ({available} свободно)\n"
                kb.add(InlineKeyboardButton(
                    f"💰 Цена {cat['name']}", callback_data=f"{CB}admin_set_price:{cat['id']}"
                ), InlineKeyboardButton(
                    f"🗑 Удалить {cat['name']}", callback_data=f"{CB}admin_delete_cat:{cat['id']}"
                ))
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

    # лениво стартуем Pyrogram clients
    logger.info("[TelegramAccountShop] Инициализация бота")
    _shop_bot = AccountShopBot(token, _storage, cardinal=cardinal)
    threading.Thread(target=_shop_bot.run, daemon=True, name="TgAccountShopBot").start()

def stop_plugin(cardinal: Any) -> None:
    global _shop_bot
    for client in list(_active_clients.values()):
        try:
            client.stop()
        except Exception:
            pass
    _active_clients.clear()
    if _shop_bot:
        _shop_bot.stop()
        _shop_bot = None

BIND_TO_POST_START = [init_plugin]
BIND_TO_PRE_STOP = [stop_plugin]
