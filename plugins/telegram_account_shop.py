from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery

# --- метаданные плагина ---
NAME = "Telegram Account Shop"
VERSION = "1.0.0"
DESCRIPTION = "Telegram-магазин аккаунтов с админ-меню и автовыдачей через FunPay"
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

def _escape_html(text: str | None) -> str:
    if not text:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def _now() -> float:
    return time.time()

def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")

# --- хранилище ---
class AccountStorage:
    def __init__(self) -> None:
        STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._migrate()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(DB_FILE, check_same_thread=False)

    def _migrate(self) -> None:
        with self._lock, self._conn() as conn:
            conn.executescript(
                """
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
                    price REAL NOT NULL,
                    funpay_product TEXT,
                    is_active INTEGER DEFAULT 1,
                    sort_order INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category_id INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    status TEXT DEFAULT 'available',
                    purchase_id INTEGER,
                    sold_at REAL,
                    FOREIGN KEY (category_id) REFERENCES categories(id)
                );
                CREATE TABLE IF NOT EXISTS purchases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    category_id INTEGER NOT NULL,
                    account_ids TEXT,
                    quantity INTEGER NOT NULL,
                    total_price REAL NOT NULL,
                    status TEXT DEFAULT 'completed',
                    funpay_order_id TEXT,
                    created_at REAL,
                    delivered_at REAL
                );
                CREATE TABLE IF NOT EXISTS config (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )

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
        if row:
            user = dict(row)
            if username and user.get("username") != username:
                user["username"] = username
                self.update_user(user)
            return user
        user = {
            "user_id": user_id,
            "username": username or "",
            "balance": 0.0,
            "total_spent": 0.0,
            "is_admin": 0,
            "created_at": _now(),
        }
        self.update_user(user)
        return user

    def update_user(self, user: dict[str, Any]) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                """INSERT INTO users(user_id, username, balance, total_spent, is_admin, created_at)
                   VALUES(?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     username=excluded.username,
                     balance=excluded.balance,
                     total_spent=excluded.total_spent,
                     is_admin=excluded.is_admin,
                     created_at=excluded.created_at""",
                (user["user_id"], user.get("username", ""),
                 float(_money_round(user.get("balance", 0))),
                 float(_money_round(user.get("total_spent", 0))),
                 int(user.get("is_admin", 0)), user.get("created_at", _now()))
            )

    def add_balance(self, user_id: int, amount: Decimal) -> bool:
        user = self.get_user(user_id)
        user["balance"] = _to_dec(user.get("balance", 0)) + amount
        self.update_user(user)
        return True

    def deduct_balance(self, user_id: int, amount: Decimal) -> bool:
        user = self.get_user(user_id)
        new_balance = _to_dec(user.get("balance", 0)) - amount
        if new_balance < 0:
            return False
        user["balance"] = new_balance
        self.update_user(user)
        return True

    def set_admin(self, user_id: int, is_admin: bool = True) -> None:
        user = self.get_user(user_id)
        user["is_admin"] = 1 if is_admin else 0
        self.update_user(user)

    def get_admins(self) -> list[int]:
        with self._lock, self._conn() as conn:
            rows = conn.execute("SELECT user_id FROM users WHERE is_admin=1").fetchall()
        return [r[0] for r in rows]

    # categories
    def add_category(self, name: str, description: str, price: Decimal, funpay_product: str | None = None, sort_order: int = 0) -> int:
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO categories(name, description, price, funpay_product, is_active, sort_order)
                   VALUES(?, ?, ?, ?, 1, ?)""",
                (name, description, float(_money_round(price)), funpay_product, sort_order)
            )
            return cur.lastrowid

    def get_category(self, cat_id: int) -> dict[str, Any] | None:
        with self._lock, self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM categories WHERE id=?", (cat_id,)).fetchone()
        if not row:
            return None
        cat = dict(row)
        cat["available"] = self.count_available(cat_id)
        return cat

    def get_categories(self, active_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM categories"
        if active_only:
            sql += " WHERE is_active=1"
        sql += " ORDER BY sort_order, id"
        with self._lock, self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql).fetchall()
        result = [dict(r) for r in rows]
        for cat in result:
            cat["available"] = self.count_available(cat["id"])
        return result

    def update_category(self, cat_id: int, **fields: Any) -> None:
        allowed = {"name", "description", "price", "funpay_product", "is_active", "sort_order"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        if "price" in updates:
            updates["price"] = float(_money_round(updates["price"]))
        sets = ", ".join(f"{k}=?" for k in updates)
        with self._lock, self._conn() as conn:
            conn.execute(f"UPDATE categories SET {sets} WHERE id=?", (*updates.values(), cat_id))

    def delete_category(self, cat_id: int) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("DELETE FROM accounts WHERE category_id=?", (cat_id,))
            conn.execute("DELETE FROM categories WHERE id=?", (cat_id,))

    def count_available(self, cat_id: int) -> int:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM accounts WHERE category_id=? AND status='available'", (cat_id,)
            ).fetchone()
        return row[0] if row else 0

    # accounts
    def add_accounts(self, category_id: int, data_list: list[str]) -> int:
        if not data_list:
            return 0
        rows = [(category_id, d.strip()) for d in data_list if d.strip()]
        if not rows:
            return 0
        with self._lock, self._conn() as conn:
            conn.executemany(
                "INSERT INTO accounts(category_id, data, status) VALUES(?, ?, 'available')", rows
            )
            return conn.total_changes

    def reserve_accounts(self, category_id: int, quantity: int) -> list[tuple[int, str]]:
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("BEGIN EXCLUSIVE")
                rows = conn.execute(
                    """SELECT id, data FROM accounts
                       WHERE category_id=? AND status='available'
                       ORDER BY id LIMIT ?""",
                    (category_id, quantity)
                ).fetchall()
                if len(rows) < quantity:
                    conn.execute("ROLLBACK")
                    return []
                ids = [r[0] for r in rows]
                conn.executemany(
                    "UPDATE accounts SET status='reserved' WHERE id=?", [(i,) for i in ids]
                )
                conn.execute("COMMIT")
                return rows
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                conn.close()

    def mark_sold(self, account_ids: list[int], purchase_id: int) -> None:
        now = _now()
        with self._lock, self._conn() as conn:
            for aid in account_ids:
                conn.execute(
                    "UPDATE accounts SET status='sold', purchase_id=?, sold_at=? WHERE id=?",
                    (purchase_id, now, aid)
                )

    def release_reserved(self, account_ids: list[int]) -> None:
        with self._lock, self._conn() as conn:
            for aid in account_ids:
                conn.execute(
                    "UPDATE accounts SET status='available', purchase_id=NULL, sold_at=NULL WHERE id=?",
                    (aid,)
                )

    def get_accounts_by_purchase(self, purchase_id: int) -> list[tuple[int, str]]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT id, data FROM accounts WHERE purchase_id=? ORDER BY id", (purchase_id,)
            ).fetchall()
        return rows

    # purchases
    def create_purchase(self, user_id: int, username: str | None, category_id: int, account_ids: list[int],
                        quantity: int, total_price: Decimal, funpay_order_id: str | None = None) -> int:
        now = _now()
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO purchases(user_id, username, category_id, account_ids, quantity,
                                          total_price, status, funpay_order_id, created_at, delivered_at)
                   VALUES(?, ?, ?, ?, ?, ?, 'completed', ?, ?, ?)""",
                (user_id, username or "", category_id, json.dumps(account_ids), quantity,
                 float(_money_round(total_price)), funpay_order_id, now, now)
            )
            return cur.lastrowid

    def get_purchase(self, purchase_id: int) -> dict[str, Any] | None:
        with self._lock, self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM purchases WHERE id=?", (purchase_id,)).fetchone()
        if not row:
            return None
        purchase = dict(row)
        purchase["accounts"] = self.get_accounts_by_purchase(purchase_id)
        return purchase

    def get_purchase_by_funpay_order(self, funpay_order_id: str) -> dict[str, Any] | None:
        with self._lock, self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM purchases WHERE funpay_order_id=?", (funpay_order_id,)).fetchone()
        if not row:
            return None
        purchase = dict(row)
        purchase["accounts"] = self.get_accounts_by_purchase(purchase["id"])
        return purchase

    def get_user_purchases(self, user_id: int, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock, self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM purchases WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_recent_purchases(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock, self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM purchases ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_available_ids(self, cat_id: int) -> list[int]:
        with self._lock, self._conn() as conn:
            rows = conn.execute("SELECT id FROM accounts WHERE category_id=?", (cat_id,)).fetchall()
        return [r[0] for r in rows]

    # stats
    def get_stats(self) -> dict[str, Any]:
        with self._lock, self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
            available = conn.execute("SELECT COUNT(*) FROM accounts WHERE status='available'").fetchone()[0]
            sold = conn.execute("SELECT COUNT(*) FROM accounts WHERE status='sold'").fetchone()[0]
            revenue = conn.execute("SELECT COALESCE(SUM(total_price), 0) FROM purchases").fetchone()[0]
            users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        return {
            "total_accounts": total,
            "available": available,
            "sold": sold,
            "revenue": _money_round(revenue),
            "users": users,
        }

# --- Telegram-бот ---
class B(InlineKeyboardButton):
    def __init__(self, text, callback_data=None, **kwargs):
        super().__init__(text, callback_data=callback_data, **kwargs)

class K(InlineKeyboardMarkup):
    def __init__(self, row_width=1):
        super().__init__(row_width=row_width)

class AccountShopBot:
    def __init__(self, token: str, storage: AccountStorage, cardinal: Any | None = None) -> None:
        self.token = token
        self.storage = storage
        self.cardinal = cardinal
        self.bot = telebot.TeleBot(token, parse_mode="HTML", threaded=True)
        self.user_states: dict[int, dict[str, Any]] = {}
        self._setup_handlers()
        self._setup_password: str | None = None
        self._ensure_setup_password()

    def _ensure_setup_password(self) -> None:
        pwd = self.storage.get_config("setup_password")
        if not pwd:
            pwd = secrets.token_hex(4).upper()
            self.storage.set_config("setup_password", pwd)
        self._setup_password = pwd
        admins = self.storage.get_admins()
        cardinal_admins = self._cardinal_admin_ids()
        if not admins and not cardinal_admins:
            logger.info("[TelegramAccountShop] Нет администраторов. Код первичной настройки: %s", pwd)

    def _cardinal_admin_ids(self) -> set[int]:
        if not self.cardinal or not getattr(self.cardinal, "telegram", None):
            return set()
        return set(self.cardinal.telegram.authorized_users.keys())

    def _is_admin(self, user_id: int) -> bool:
        if self.storage.get_user(user_id).get("is_admin"):
            return True
        if user_id in self._cardinal_admin_ids():
            return True
        return False

    def _notify_admins(self, text: str) -> None:
        admin_ids = set(self.storage.get_admins()) | self._cardinal_admin_ids()
        for admin_id in admin_ids:
            try:
                self.bot.send_message(admin_id, text)
            except Exception:
                logger.exception("Ошибка уведомления админа %s", admin_id)

    # state helpers
    def _set_state(self, user_id: int, state: str, data: dict[str, Any] | None = None) -> None:
        self.user_states[user_id] = {"state": state, "data": data or {}}

    def _get_state(self, user_id: int) -> dict[str, Any] | None:
        return self.user_states.get(user_id)

    def _clear_state(self, user_id: int) -> None:
        self.user_states.pop(user_id, None)

    # keyboards
    def _main_keyboard(self, user_id: int) -> InlineKeyboardMarkup:
        kb = K(row_width=2)
        kb.add(B("Категории", callback_data=f"{CB}cats"), B("Мои покупки", callback_data=f"{CB}orders"))
        kb.add(B("Баланс", callback_data=f"{CB}balance"), B("Поддержка", callback_data=f"{CB}support"))
        if self._is_admin(user_id):
            kb.add(B("Админ-панель", callback_data=f"{CB}admin"))
        return kb

    def _admin_keyboard(self) -> InlineKeyboardMarkup:
        kb = K(row_width=2)
        kb.add(B("Категории", callback_data=f"{CB}admin_cats"), B("Товар/склад", callback_data=f"{CB}admin_stock"))
        kb.add(B("Покупки", callback_data=f"{CB}admin_purchases"), B("Пользователи", callback_data=f"{CB}admin_users"))
        kb.add(B("Статистика", callback_data=f"{CB}admin_stats"), B("Назад", callback_data=f"{CB}main"))
        return kb

    def _back_keyboard(self, callback: str) -> InlineKeyboardMarkup:
        kb = K()
        kb.add(B("Назад", callback_data=callback))
        return kb

    # handlers setup
    def _setup_handlers(self) -> None:
        @self.bot.message_handler(commands=["start"])
        def on_start(m: Message):
            self._main_menu(m.from_user.id, m.chat.id, m.from_user.username)

        @self.bot.message_handler(commands=["admin", "setup"])
        def on_admin_cmd(m: Message):
            text = (m.text or "").strip()
            if text.startswith("/setup "):
                parts = text.split(maxsplit=1)
                if len(parts) == 2 and parts[1].strip().upper() == self._setup_password:
                    self.storage.set_admin(m.from_user.id)
                    self.bot.send_message(m.chat.id, "Вы назначены администратором.", reply_markup=self._main_keyboard(m.from_user.id))
                    return
                self.bot.send_message(m.chat.id, "Неверный код настройки.")
                return
            if not self._is_admin(m.from_user.id):
                self.bot.send_message(m.chat.id, "Нет доступа.")
                return
            self._admin_menu(m.from_user.id, m.chat.id)

        @self.bot.message_handler(func=lambda m: True, content_types=["text"])
        def on_text(m: Message):
            self._on_text(m)

        @self.bot.callback_query_handler(func=lambda c: c.data.startswith(CB))
        def on_cb(c: CallbackQuery):
            self._on_callback(c)

    # run/stop
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

    # menus
    def _main_menu(self, user_id: int, chat_id: int, username: str | None = None) -> None:
        self.storage.get_user(user_id, username)
        text = "<b>Добро пожаловать в магазин Telegram-аккаунтов</b>\n\nВыберите раздел:"
        self._send_or_edit(chat_id, text, self._main_keyboard(user_id))

    def _admin_menu(self, user_id: int, chat_id: int) -> None:
        self._send_or_edit(chat_id, "<b>Админ-панель</b>", self._admin_keyboard())

    def _send_or_edit(self, chat_id: int, text: str, kb: InlineKeyboardMarkup, msg_id: int | None = None) -> None:
        if msg_id:
            try:
                self.bot.edit_message_text(text, chat_id, msg_id, reply_markup=kb)
                return
            except Exception:
                pass
        try:
            self.bot.send_message(chat_id, text, reply_markup=kb)
        except Exception:
            logger.exception("Ошибка отправки сообщения")

    def _show_categories(self, user_id: int, chat_id: int, msg_id: int | None = None) -> None:
        cats = self.storage.get_categories()
        if not cats:
            self._send_or_edit(chat_id, "Пока нет категорий.", self._back_keyboard(f"{CB}main"), msg_id)
            return
        kb = K()
        for cat in cats:
            kb.add(B(f"{cat['name']} — {cat['available']} шт. ({_money_str(cat['price'])}₽)",
                     callback_data=f"{CB}cat:{cat['id']}"))
        kb.add(B("Назад", callback_data=f"{CB}main"))
        self._send_or_edit(chat_id, "<b>Категории</b>\n\nВыберите товар:", kb, msg_id)

    def _show_category(self, user_id: int, chat_id: int, cat_id: int, msg_id: int | None = None) -> None:
        cat = self.storage.get_category(cat_id)
        if not cat:
            self._show_categories(user_id, chat_id, msg_id)
            return
        text = (f"<b>{_escape_html(cat['name'])}</b>\n"
                f"{_escape_html(cat.get('description') or '')}\n\n"
                f"Цена: <b>{_money_str(cat['price'])}₽</b>\n"
                f"В наличии: <b>{cat['available']}</b> шт.")
        kb = K()
        if cat["available"] > 0:
            kb.add(B("Купить", callback_data=f"{CB}buy:{cat['id']}"))
        kb.add(B("Назад", callback_data=f"{CB}cats"))
        self._send_or_edit(chat_id, text, kb, msg_id)

    def _buy(self, user_id: int, chat_id: int, cat_id: int) -> None:
        cat = self.storage.get_category(cat_id)
        if not cat:
            self.bot.send_message(chat_id, "Категория не найдена.")
            return
        user = self.storage.get_user(user_id)
        price = _to_dec(cat["price"])
        balance = _to_dec(user.get("balance", 0))
        if balance < price:
            self.bot.send_message(
                chat_id,
                f"Недостаточно средств.\nЦена: {_money_str(price)}₽\nБаланс: {_money_str(balance)}₽\n\nПополните баланс через поддержку.",
                reply_markup=self._main_keyboard(user_id)
            )
            return
        accounts = self.storage.reserve_accounts(cat_id, 1)
        if not accounts:
            self.bot.send_message(chat_id, "Товар закончился. Обратитесь в поддержку.", reply_markup=self._main_keyboard(user_id))
            return
        account_id, account_data = accounts[0]
        if not self.storage.deduct_balance(user_id, price):
            self.storage.release_reserved([account_id])
            self.bot.send_message(chat_id, "Не удалось списать средства. Попробуйте позже.", reply_markup=self._main_keyboard(user_id))
            return
        purchase_id = self.storage.create_purchase(
            user_id, user.get("username"), cat_id, [account_id], 1, price
        )
        self.storage.mark_sold([account_id], purchase_id)
        user["total_spent"] = _to_dec(user.get("total_spent", 0)) + price
        self.storage.update_user(user)
        text = (f"<b>Покупка #{purchase_id} оформлена!</b>\n\n"
                f"Категория: {_escape_html(cat['name'])}\n"
                f"Цена: {_money_str(price)}₽\n\n"
                f"<b>Данные аккаунта:</b>\n<pre>{_escape_html(account_data)}</pre>")
        self.bot.send_message(chat_id, text, reply_markup=self._main_keyboard(user_id))
        self._notify_admins(f"Продажа #{purchase_id} на {_money_str(price)}₽\nКатегория: {cat['name']}")

    def _show_orders(self, user_id: int, chat_id: int, msg_id: int | None = None) -> None:
        orders = self.storage.get_user_purchases(user_id, limit=20)
        if not orders:
            self._send_or_edit(chat_id, "У вас пока нет покупок.", self._back_keyboard(f"{CB}main"), msg_id)
            return
        kb = K()
        for order in orders:
            cat = self.storage.get_category(order["category_id"])
            name = cat["name"] if cat else "?"
            kb.add(B(f"#{order['id']} — {name} — {_money_str(order['total_price'])}₽",
                     callback_data=f"{CB}order:{order['id']}"))
        kb.add(B("Назад", callback_data=f"{CB}main"))
        self._send_or_edit(chat_id, "<b>Ваши покупки</b>", kb, msg_id)

    def _show_order(self, user_id: int, chat_id: int, purchase_id: int, msg_id: int | None = None) -> None:
        purchase = self.storage.get_purchase(purchase_id)
        if not purchase or purchase["user_id"] != user_id and not self._is_admin(user_id):
            self.bot.answer_callback_query(chat_id, "Покупка не найдена.") if msg_id else None
            return
        cat = self.storage.get_category(purchase["category_id"])
        lines = [f"<b>Покупка #{purchase['id']}</b>",
                 f"Категория: {_escape_html(cat['name'] if cat else '?')}",
                 f"Сумма: {_money_str(purchase['total_price'])}₽",
                 f"Дата: {_fmt_ts(purchase.get('created_at'))}",
                 "", "<b>Данные аккаунта(ов):</b>"]
        for _, data in purchase["accounts"]:
            lines.append(f"<pre>{_escape_html(data)}</pre>")
        kb = K()
        kb.add(B("Назад", callback_data=f"{CB}orders"))
        self._send_or_edit(chat_id, "\n".join(lines), kb, msg_id)

    def _show_balance(self, user_id: int, chat_id: int, msg_id: int | None = None) -> None:
        user = self.storage.get_user(user_id)
        text = (f"<b>Ваш баланс</b>: {_money_str(user.get('balance', 0))}₽\n"
                f"Всего потрачено: {_money_str(user.get('total_spent', 0))}₽\n\n"
                f"Для пополнения обратитесь в поддержку.")
        self._send_or_edit(chat_id, text, self._back_keyboard(f"{CB}main"), msg_id)

    def _show_support(self, user_id: int, chat_id: int) -> None:
        self._set_state(user_id, "support_message")
        self.bot.send_message(chat_id, "Опишите ваш вопрос/проблему одним сообщением:",
                              reply_markup=self._back_keyboard(f"{CB}main"))

    # admin flows
    def _admin_categories(self, user_id: int, chat_id: int, msg_id: int | None = None) -> None:
        cats = self.storage.get_categories(active_only=False)
        kb = K()
        for cat in cats:
            status = "✅" if cat["is_active"] else "🛑"
            kb.add(B(f"{status} {cat['name']} — {_money_str(cat['price'])}₽",
                     callback_data=f"{CB}admin_cat:{cat['id']}"))
        kb.add(B("➕ Добавить категорию", callback_data=f"{CB}admin_addcat"))
        kb.add(B("Назад", callback_data=f"{CB}admin"))
        self._send_or_edit(chat_id, "<b>Категории</b>", kb, msg_id)

    def _admin_category_detail(self, user_id: int, chat_id: int, cat_id: int, msg_id: int | None = None) -> None:
        cat = self.storage.get_category(cat_id)
        if not cat:
            self._admin_categories(user_id, chat_id, msg_id)
            return
        text = (f"<b>{_escape_html(cat['name'])}</b>\n"
                f"{_escape_html(cat.get('description') or '')}\n"
                f"Цена: {_money_str(cat['price'])}₽\n"
                f"FunPay товар: {_escape_html(cat.get('funpay_product') or '-')}\n"
                f"В наличии: {cat['available']}")
        kb = K()
        kb.add(B("Изменить цену", callback_data=f"{CB}admin_setprice:{cat_id}"))
        kb.add(B("➕ Добавить аккаунты", callback_data=f"{CB}admin_addacc:{cat_id}"))
        toggle_text = "Деактивировать" if cat["is_active"] else "Активировать"
        kb.add(B(toggle_text, callback_data=f"{CB}admin_togglecat:{cat_id}"))
        kb.add(B("Удалить", callback_data=f"{CB}admin_delcat:{cat_id}"))
        kb.add(B("Назад", callback_data=f"{CB}admin_cats"))
        self._send_or_edit(chat_id, text, kb, msg_id)

    def _admin_stock(self, user_id: int, chat_id: int, msg_id: int | None = None) -> None:
        cats = self.storage.get_categories(active_only=False)
        lines = ["<b>Склад</b>", ""]
        for cat in cats:
            total = len(self.storage.get_available_ids(cat["id"]))
            lines.append(f"{cat['name']}: {cat['available']} доступно / {total} всего")
        kb = K()
        kb.add(B("Назад", callback_data=f"{CB}admin"))
        self._send_or_edit(chat_id, "\n".join(lines), kb, msg_id)

    def _admin_purchases(self, user_id: int, chat_id: int, msg_id: int | None = None) -> None:
        purchases = self.storage.get_recent_purchases(20)
        if not purchases:
            self._send_or_edit(chat_id, "Пока нет покупок.", self._back_keyboard(f"{CB}admin"), msg_id)
            return
        kb = K()
        for p in purchases:
            cat = self.storage.get_category(p["category_id"])
            name = cat["name"] if cat else "?"
            user = p.get("username") or str(p["user_id"])
            kb.add(B(f"#{p['id']} {name} {_money_str(p['total_price'])}₽ ({user})",
                     callback_data=f"{CB}admin_order:{p['id']}"))
        kb.add(B("Назад", callback_data=f"{CB}admin"))
        self._send_or_edit(chat_id, "<b>Последние покупки</b>", kb, msg_id)

    def _admin_users(self, user_id: int, chat_id: int, msg_id: int | None = None) -> None:
        with self.storage._lock, self.storage._conn() as conn:
            rows = conn.execute("SELECT user_id, username, balance FROM users ORDER BY user_id DESC LIMIT 20").fetchall()
        if not rows:
            self._send_or_edit(chat_id, "Пока нет пользователей.", self._back_keyboard(f"{CB}admin"), msg_id)
            return
        kb = K()
        for uid, uname, balance in rows:
            label = f"{uname or uid} — {_money_str(balance)}₽"
            kb.add(B(label, callback_data=f"{CB}admin_user:{uid}"))
        kb.add(B("Назад", callback_data=f"{CB}admin"))
        self._send_or_edit(chat_id, "<b>Пользователи</b>", kb, msg_id)

    def _admin_user_detail(self, user_id: int, chat_id: int, target_id: int, msg_id: int | None = None) -> None:
        user = self.storage.get_user(target_id)
        text = (f"<b>Пользователь {target_id}</b>\n"
                f"Username: {_escape_html(user.get('username') or '-')}\n"
                f"Баланс: {_money_str(user.get('balance', 0))}₽\n"
                f"Потрачено: {_money_str(user.get('total_spent', 0))}₽\n"
                f"Админ: {'да' if user.get('is_admin') else 'нет'}")
        kb = K()
        kb.add(B("Пополнить баланс", callback_data=f"{CB}admin_addbal:{target_id}"))
        kb.add(B("Сделать админом" if not user.get('is_admin') else "Снять админа",
                 callback_data=f"{CB}admin_toggleadmin:{target_id}"))
        kb.add(B("Назад", callback_data=f"{CB}admin_users"))
        self._send_or_edit(chat_id, text, kb, msg_id)

    def _admin_stats(self, user_id: int, chat_id: int, msg_id: int | None = None) -> None:
        stats = self.storage.get_stats()
        text = (f"<b>Статистика</b>\n\n"
                f"Аккаунтов всего: {stats['total_accounts']}\n"
                f"В наличии: {stats['available']}\n"
                f"Продано: {stats['sold']}\n"
                f"Выручка: {_money_str(stats['revenue'])}₽\n"
                f"Пользователей: {stats['users']}")
        self._send_or_edit(chat_id, text, self._back_keyboard(f"{CB}admin"), msg_id)

    # callback dispatcher
    def _on_callback(self, c: CallbackQuery) -> None:
        user_id = c.from_user.id
        chat_id = c.message.chat.id
        msg_id = c.message.message_id
        data = c.data[len(CB):]
        parts = data.split(":")
        action = parts[0]
        args = parts[1:]

        if action == "main":
            self._main_menu(user_id, chat_id, c.from_user.username)
        elif action == "cats":
            self._show_categories(user_id, chat_id, msg_id)
        elif action == "cat" and args:
            self._show_category(user_id, chat_id, int(args[0]), msg_id)
        elif action == "buy" and args:
            self._buy(user_id, chat_id, int(args[0]))
        elif action == "orders":
            self._show_orders(user_id, chat_id, msg_id)
        elif action == "order" and args:
            self._show_order(user_id, chat_id, int(args[0]), msg_id)
        elif action == "balance":
            self._show_balance(user_id, chat_id, msg_id)
        elif action == "support":
            self._show_support(user_id, chat_id)
        elif action == "admin":
            if not self._is_admin(user_id):
                self.bot.answer_callback_query(c.id, "Нет доступа.")
                return
            self._admin_menu(user_id, chat_id)
        elif action == "admin_cats":
            self._admin_categories(user_id, chat_id, msg_id)
        elif action == "admin_cat" and args:
            self._admin_category_detail(user_id, chat_id, int(args[0]), msg_id)
        elif action == "admin_addcat":
            self._set_state(user_id, "add_cat_name")
            self.bot.edit_message_text("Введите название категории:", chat_id, msg_id,
                                       reply_markup=self._back_keyboard(f"{CB}admin_cats"))
        elif action == "admin_setprice" and args:
            self._set_state(user_id, "set_price", {"cat_id": int(args[0])})
            self.bot.edit_message_text("Введите новую цену:", chat_id, msg_id,
                                       reply_markup=self._back_keyboard(f"{CB}admin_cat:{args[0]}"))
        elif action == "admin_addacc" and args:
            self._set_state(user_id, "add_accounts", {"cat_id": int(args[0])})
            text = "Отправьте аккаунты для этой категории. Один аккаунт — одна строка."
            self.bot.edit_message_text(text, chat_id, msg_id,
                                       reply_markup=self._back_keyboard(f"{CB}admin_cat:{args[0]}"))
        elif action == "admin_togglecat" and args:
            cat = self.storage.get_category(int(args[0]))
            if cat:
                self.storage.update_category(int(args[0]), is_active=0 if cat["is_active"] else 1)
            self._admin_category_detail(user_id, chat_id, int(args[0]), msg_id)
        elif action == "admin_delcat" and args:
            self.storage.delete_category(int(args[0]))
            self._admin_categories(user_id, chat_id, msg_id)
        elif action == "admin_stock":
            self._admin_stock(user_id, chat_id, msg_id)
        elif action == "admin_purchases":
            self._admin_purchases(user_id, chat_id, msg_id)
        elif action == "admin_order" and args:
            self._show_order(user_id, chat_id, int(args[0]), msg_id)
        elif action == "admin_users":
            self._admin_users(user_id, chat_id, msg_id)
        elif action == "admin_user" and args:
            self._admin_user_detail(user_id, chat_id, int(args[0]), msg_id)
        elif action == "admin_addbal" and args:
            self._set_state(user_id, "add_balance", {"target_id": int(args[0])})
            self.bot.edit_message_text("Введите сумму для пополнения:", chat_id, msg_id,
                                       reply_markup=self._back_keyboard(f"{CB}admin_user:{args[0]}"))
        elif action == "admin_toggleadmin" and args:
            target = self.storage.get_user(int(args[0]))
            self.storage.set_admin(int(args[0]), not target.get("is_admin"))
            self._admin_user_detail(user_id, chat_id, int(args[0]), msg_id)
        elif action == "admin_stats":
            self._admin_stats(user_id, chat_id, msg_id)
        else:
            self.bot.answer_callback_query(c.id, "Неизвестная команда.")
            return
        try:
            self.bot.answer_callback_query(c.id)
        except Exception:
            pass

    # text dispatcher
    def _on_text(self, m: Message) -> None:
        user_id = m.from_user.id
        chat_id = m.chat.id
        state = self._get_state(user_id)
        if not state:
            self._main_menu(user_id, chat_id, m.from_user.username)
            return
        text = (m.text or "").strip()
        if text.lower() in ("/cancel", "отмена", "назад"):
            self._clear_state(user_id)
            self._main_menu(user_id, chat_id, m.from_user.username)
            return
        s = state["state"]
        data = state.get("data", {})

        if s == "support_message":
            self._clear_state(user_id)
            self.bot.send_message(chat_id, "Спасибо, сообщение отправлено в поддержку. Мы ответим вам скоро.",
                                  reply_markup=self._main_keyboard(user_id))
            self._notify_admins(f"Сообщение в поддержку от {m.from_user.username or user_id}:\n{text}")
            return

        if s == "add_cat_name":
            self._set_state(user_id, "add_cat_desc", {"name": text})
            self.bot.send_message(chat_id, "Введите описание категории (или '-'):")
            return
        if s == "add_cat_desc":
            self._set_state(user_id, "add_cat_price", {"name": data["name"], "desc": text})
            self.bot.send_message(chat_id, "Введите цену в ₽:")
            return
        if s == "add_cat_price":
            try:
                price = _money_round(Decimal(text.replace(",", ".")))
            except Exception:
                self.bot.send_message(chat_id, "Неверная цена. Введите число:")
                return
            self._set_state(user_id, "add_cat_fp", {"name": data["name"], "desc": data["desc"], "price": price})
            self.bot.send_message(chat_id, "Введите название товара на FunPay (или '-'):")
            return
        if s == "add_cat_fp":
            fp = text if text != "-" else None
            name, desc, price = data["name"], data["desc"], data["price"]
            if desc == "-":
                desc = ""
            cat_id = self.storage.add_category(name, desc, price, funpay_product=fp)
            self._clear_state(user_id)
            self.bot.send_message(chat_id, f"Категория #{cat_id} добавлена.", reply_markup=self._main_keyboard(user_id))
            self._admin_category_detail(user_id, chat_id, cat_id)
            return

        if s == "set_price":
            try:
                price = _money_round(Decimal(text.replace(",", ".")))
            except Exception:
                self.bot.send_message(chat_id, "Неверная цена. Введите число:")
                return
            self.storage.update_category(data["cat_id"], price=price)
            self._clear_state(user_id)
            self.bot.send_message(chat_id, "Цена обновлена.", reply_markup=self._main_keyboard(user_id))
            return

        if s == "add_accounts":
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            count = self.storage.add_accounts(data["cat_id"], lines)
            self._clear_state(user_id)
            self.bot.send_message(chat_id, f"Добавлено {count} аккаунтов.", reply_markup=self._main_keyboard(user_id))
            return

        if s == "add_balance":
            try:
                amount = _money_round(Decimal(text.replace(",", ".")))
            except Exception:
                self.bot.send_message(chat_id, "Неверная сумма. Введите число:")
                return
            self.storage.add_balance(data["target_id"], amount)
            self._clear_state(user_id)
            self.bot.send_message(chat_id, f"Баланс пользователя пополнен на {_money_str(amount)}₽.",
                                  reply_markup=self._main_keyboard(user_id))
            return

        self._main_menu(user_id, chat_id, m.from_user.username)

# --- интеграция с Cardinal ---
_shop_bot: AccountShopBot | None = None
_storage: AccountStorage | None = None


def _match_category(storage: AccountStorage, order: Any) -> dict[str, Any] | None:
    cats = storage.get_categories(active_only=True)
    sub = getattr(order, "subcategory_name", "") or ""
    desc = getattr(order, "description", "") or ""
    for cat in cats:
        fp = cat.get("funpay_product") or ""
        if fp and (fp.lower() in sub.lower() or fp.lower() in desc.lower()):
            return cat
    # fallback: category name in description
    for cat in cats:
        if cat["name"].lower() in desc.lower():
            return cat
    return None


def _deliver_funpay(cardinal: Any, order: Any) -> bool:
    if not _storage or not cardinal:
        return False
    status = getattr(order, "status", None)
    status_name = getattr(status, "name", None) or str(status)
    if status_name != "PAID":
        return False
    order_id = str(getattr(order, "id", ""))
    if order_id and _storage.get_purchase_by_funpay_order(order_id):
        return True
    cat = _match_category(_storage, order)
    if not cat:
        return False
    quantity = getattr(order, "amount", 1) or 1
    available = _storage.count_available(cat["id"])
    if available < quantity:
        try:
            msg = f"Заказ #{order.id} по {cat['name']}: недостаточно товара (нужно {quantity}, есть {available})."
            if _shop_bot:
                _shop_bot._notify_admins(msg)
            cardinal.send_message(order.chat_id,
                                  "К сожалению, товар временно закончился. Свяжитесь с продавцом.",
                                  order.buyer_username, watermark=False)
        except Exception:
            logger.exception("Ошибка уведомления о нехватке товара")
        return False
    accounts = _storage.reserve_accounts(cat["id"], quantity)
    if not accounts:
        return False
    account_ids = [r[0] for r in accounts]
    total_price = _to_dec(cat["price"]) * quantity
    purchase_id = _storage.create_purchase(
        int(order.buyer_id), order.buyer_username, cat["id"], account_ids, quantity,
        total_price, funpay_order_id=str(order.id)
    )
    _storage.mark_sold(account_ids, purchase_id)
    lines = [f"Спасибо за покупку! Заказ #{order.id}", f"Категория: {cat['name']}", "", "Данные аккаунта(ов):"]
    for _, data in accounts:
        lines.append(data)
    delivery_text = "\n".join(lines)
    try:
        cardinal.send_message(order.chat_id, delivery_text, order.buyer_username, watermark=False)
    except Exception:
        logger.exception("Ошибка доставки заказа #%s", order.id)
        _storage.release_reserved(account_ids)
        return False
    if _shop_bot:
        _shop_bot._notify_admins(f"FunPay заказ #{order.id} выдан. Категория: {cat['name']}, сумма: {_money_str(total_price)}₽")
    return True


def init_plugin(cardinal: Any) -> None:
    logger.info("[TelegramAccountShop] Инициализация")
    global _storage, _shop_bot
    _storage = AccountStorage()
    token = _storage.get_config("bot_token")
    if not token:
        logger.warning("[TelegramAccountShop] bot_token не задан. Создайте storage/cache/tg_account_shop/config.json с bot_token.")
        return
    _shop_bot = AccountShopBot(token, _storage, cardinal=cardinal)
    threading.Thread(target=_shop_bot.run, daemon=True, name="TgAccountShopBot").start()


def stop_plugin(cardinal: Any) -> None:
    global _shop_bot
    if _shop_bot:
        _shop_bot.stop()
        _shop_bot = None


def handle_new_order(cardinal: Any, e: Any) -> None:
    if not _shop_bot or not _storage:
        return
    _deliver_funpay(cardinal, e.order)


def handle_order_status_changed(cardinal: Any, e: Any) -> None:
    if not _shop_bot or not _storage:
        return
    _deliver_funpay(cardinal, e.order)


BIND_TO_POST_START = [init_plugin]
BIND_TO_PRE_STOP = [stop_plugin]
BIND_TO_NEW_ORDER = [handle_new_order]
BIND_TO_ORDER_STATUS_CHANGED = [handle_order_status_changed]
