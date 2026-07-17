from __future__ import annotations
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, TYPE_CHECKING
from uuid import uuid4, uuid5, NAMESPACE_DNS
import html
import http.server
import copy
import csv
import ipaddress
import base64
import json
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
import logging
import os
import re
import socketserver
import sqlite3
import threading
import time
from urllib.parse import urlencode, quote

import requests
import telebot
from telebot.types import InlineKeyboardMarkup as _InlineKeyboardMarkup, InlineKeyboardButton as _InlineKeyboardButton, Message, CallbackQuery, LabeledPrice


def _style_button_init(self, text, url=None, callback_data=None, web_app=None, switch_inline_query=None,
                       switch_inline_query_current_chat=None, callback_game=None, pay=None, login_url=None,
                       switch_inline_query_chosen_chat=None, copy_text=None, style=None,
                       icon_custom_emoji_id=None, **kwargs):
    _InlineKeyboardButton.__init__(self, text, url=url, callback_data=callback_data, web_app=web_app,
                                  switch_inline_query=switch_inline_query,
                                  switch_inline_query_current_chat=switch_inline_query_current_chat,
                                  callback_game=callback_game, pay=pay, login_url=login_url,
                                  switch_inline_query_chosen_chat=switch_inline_query_chosen_chat, copy_text=copy_text, **kwargs)
    self.style = style
    self.icon_custom_emoji_id = icon_custom_emoji_id


def _style_button_to_dict(self):
    d = _InlineKeyboardButton.to_dict(self)
    if getattr(self, 'style', None):
        d['style'] = self.style
    if getattr(self, 'icon_custom_emoji_id', None):
        d['icon_custom_emoji_id'] = self.icon_custom_emoji_id
    return d


class B(_InlineKeyboardButton):
    __init__ = _style_button_init
    to_dict = _style_button_to_dict


class K(_InlineKeyboardMarkup):
    def __init__(self, row_width=2, keyboard=None):
        super().__init__(keyboard=keyboard, row_width=row_width)

    def to_dict(self):
        flat = [button for row in self.keyboard for button in row]
        rows = [flat[i:i + self.row_width] for i in range(0, len(flat), self.row_width)]
        return {'inline_keyboard': [[button.to_dict() for button in row] for row in rows]}

if TYPE_CHECKING:
    from cardinal import Cardinal

NAME = "VPN Bot"
VERSION = "0.3.0"
DESCRIPTION = "Каркас VPN-бота: баланс, рефералы, подписки, пополнения, управление устройствами."
CREDITS = "@litterymust"
UUID = "d634e827-cf45-4565-8f84-7325d7fbee11"
SETTINGS_PAGE = False
BIND_TO_DELETE = None

logger = logging.getLogger("FPC.vpn_bot")

STORAGE_DIR = Path("storage/cache/vpn")
EXPORTS_DIR = STORAGE_DIR / "exports"
DB_FILE = STORAGE_DIR / "vpn_bot.db"

CB_PREFIX = "vpn:"

# Пороги безопасности (можно переопределять в /vpnadmin → Безопасность)
DEFAULT_SHARING_WINDOW = 300  # сек — окно одновременных подключений для анти-шаринга
DEFAULT_UNBIND_COOLDOWN = 7 * 86400  # сек — раз в 7 дней пользователь может отвязать устройство
MAX_CONNECTION_LOG = 2000

DEFAULT_PLANS = {
    "trial": {
        "name": "Пробный",
        "max_devices": 1,
        "prices": {},
    },
    "basic": {
        "name": "Базовый",
        "max_devices": 1,
        "prices": {"1": Decimal("100.00"), "3": Decimal("270.00"), "6": Decimal("500.00"), "12": Decimal("900.00")},
    },
    "family": {
        "name": "Семейный",
        "max_devices": 5,
        "prices": {"1": Decimal("250.00"), "3": Decimal("700.00"), "6": Decimal("1300.00"), "12": Decimal("2400.00")},
    },
    "corporate": {
        "name": "Корпоративный",
        "max_devices": -1,
        "prices": {"1": Decimal("500.00"), "3": Decimal("1400.00"), "6": Decimal("2600.00"), "12": Decimal("4800.00")},
    },
}

DURATIONS = ["1", "3", "6", "12"]
TRIAL_DAYS = 3

_user_bot_instance: "UserBot | None" = None


@dataclass
class ServerConfig:
    host_id: str = "main"
    name: str = "Основной"
    address: str = "vpn.example.com"
    port: int = 443
    public_key: str = ""
    short_id: str = ""
    server_name: str = ""
    flow: str = "xtls-rprx-vision"
    network: str = "tcp"
    security: str = "reality"
    fingerprint: str = "chrome"
    spider_x: str = "/"
    # 3X-UI панель
    panel_url: str = ""  # http://ip:2053 (без /panel)
    panel_path: str = "/panel"
    panel_username: str = "admin"
    panel_password: str = "admin"
    inbound_id: int = 1
    verify_ssl: bool = True
    sub_port: int = 2096
    subscription_url_base: str = ""  # хост для subscription URL, пусто = address
    temp_profile_enabled: bool = False
    temp_profile_port: int = 8081
    temp_inbound_id: int = 0


@dataclass
class Plan:
    id: str
    name: str
    max_devices: int
    prices: dict[str, Decimal]
    host_id: str = "main"

    @property
    def device_text(self) -> str:
        if self.max_devices == -1:
            return "Безлимит"
        return f"{self.max_devices} устройств"


@dataclass
class Subscription:
    sub_id: str
    user_id: int
    plan_id: str
    months: int
    client_uuid: str
    created_at: float
    expires_at: float
    devices: list[dict[str, Any]] = field(default_factory=list)
    active: bool = True
    frozen_until: float | None = None
    freeze_started: float | None = None
    warnings: dict[str, bool] = field(default_factory=dict)
    email: str = ""  # email клиента в 3X-UI
    xray_sub_id: str = ""  # subId для subscription URL
    xray_synced: bool = False  # успешно создан/обновлён в 3X-UI
    host_id: str = "main"

    @property
    def is_expired(self) -> bool:
        if self.frozen_until and time.time() < self.frozen_until:
            return False
        return time.time() > self.expires_at

    @property
    def effective_expires_at(self) -> float:
        if self.frozen_until and self.freeze_started:
            return self.expires_at + max(0.0, self.frozen_until - self.freeze_started)
        return self.expires_at


class VPNStorage:
    """Потокобезопасное JSON-хранилище плагина."""

    def __init__(self) -> None:
        STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()
        self._migrate_json_to_sqlite()
        self.config: dict[str, Any] = self._load_db("config", self._default_config)
        self._ensure_config_defaults(self.config, self._default_config())
        self._migrate_config()
        self.users: dict[str, Any] = self._load_db("users", self._default_users)
        self.subscriptions: dict[str, Any] = self._load_db("subscriptions", self._default_subscriptions)
        self.transactions: dict[str, Any] = self._load_db("transactions", self._default_transactions)
        self.referrals: dict[str, Any] = self._load_db("referrals", self._default_referrals)
        self.connections: dict[str, Any] = self._load_db("connections", self._default_connections)
        self._decimalize_loaded()

    def _decimalize_loaded(self) -> None:
        for u in self.users.values():
            for key in ("balance", "referral_balance", "total_spent"):
                if key in u:
                    u[key] = _to_dec(u[key])
        for tx in self.transactions.get("txs", {}).values():
            tx["amount"] = _to_dec(tx.get("amount"))
        for inv in self.referrals.get("crypto_invoices", {}).values():
            for key in ("amount", "rub_amount", "paid_amount"):
                if key in inv and inv[key] is not None:
                    inv[key] = _to_dec(inv[key])
        for e in self.referrals.get("earnings", {}).values():
            e["level1"] = _to_dec(e.get("level1", 0))
            e["level2"] = _to_dec(e.get("level2", 0))
        rates = self.config.get("rates", {})
        for key in ("USD", "TON"):
            if key in rates:
                rates[key] = _to_dec(rates[key])
        for r in self.config.get("withdrawal_requests", {}).values():
            r["amount"] = _to_dec(r.get("amount"))
        for p in self.config.get("plans", {}).values():
            prices = p.get("prices", {})
            for k, v in list(prices.items()):
                prices[k] = _to_dec(v)
        for promo in self.config.get("promocodes", {}).values():
            if "value" in promo and promo["value"] is not None:
                promo["value"] = _to_dec(promo["value"])

    def _ensure_config_defaults(self, current: dict[str, Any], defaults: dict[str, Any]) -> None:
        for key, value in defaults.items():
            if key not in current:
                current[key] = value
            elif isinstance(value, dict) and isinstance(current[key], dict):
                self._ensure_config_defaults(current[key], value)

    def _migrate_config(self) -> None:
        with self._lock:
            # migrate legacy single server to hosts and remove legacy key
            if self.config.get("server"):
                if not self.config.get("hosts"):
                    legacy = self.config["server"]
                    host_id = "main"
                    host = dict(legacy)
                    host.setdefault("host_id", host_id)
                    host.setdefault("name", "Основной")
                    self.config["hosts"] = {host_id: host}
                    self.config["default_host_id"] = host_id
                del self.config["server"]
                self.save_config()
            # ensure default host exists
            default = self.config.get("default_host_id", "main")
            hosts = self.config.setdefault("hosts", {})
            if default not in hosts:
                hosts[default] = asdict(ServerConfig(host_id=default, name="Основной"))
                self.save_config()

    def _default_config(self) -> dict[str, Any]:
        return {
            "hosts": {},
            "default_host_id": "main",
            "expired_cleanup_days": 5,
            "referral_first_discount": 0,
            "plans": copy.deepcopy(DEFAULT_PLANS),
            "promocodes": {},
            "activation_codes": {},
            "rates": {"USD": Decimal("0.00"), "TON": Decimal("0.00"), "updated_at": 0.0},
            "withdrawal_requests": {},
            "security": {
                "sharing_window": DEFAULT_SHARING_WINDOW,
                "unbind_cooldown": DEFAULT_UNBIND_COOLDOWN,
                "traffic_limit_gb": 0.0,  # 0 — без ограничения
                "alert_admin": True,
            },
            "admin_user_ids": [],
            "admin_notifications": {
                "new_user": True,
                "new_payment": True,
                "expiring_sub": True,
                "complaint": True,
            },
            "maintenance": False,
            "device_auth_port": 8080,
            "device_auth_token": "",
            "temp_profile_token": "",
            "user_bot_token": "",
            "channel_id": "",
            "support_id": "",
            "crypto_bot_token": "",
            "welcome": "Добро пожаловать в VPN-бот!",
            "support": "@support",
            "bot_username": "",
            "trial_days": TRIAL_DAYS,
            "welcome_media_file_id": "",
            "welcome_media_type": "photo",
            "menu_media_file_id": "",
            "menu_media_type": "photo",
        }

    def _default_users(self) -> dict[str, Any]:
        return {}

    def _default_subscriptions(self) -> dict[str, Any]:
        return {"subs": {}, "next_id": 1}

    def _default_transactions(self) -> dict[str, Any]:
        return {"txs": {}, "next_id": 1}

    def _default_connections(self) -> dict[str, Any]:
        return {"logs": [], "suspicious": []}

    def _default_referrals(self) -> dict[str, Any]:
        return {"paid_invoices": [], "earnings": {}, "crypto_invoices": {}}

    def _init_db(self) -> None:
        with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS kv (
                    name TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            conn.commit()

    def _load_db(self, name: str, default_factory) -> dict[str, Any]:
        try:
            with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
                row = conn.execute("SELECT data FROM kv WHERE name=?", (name,)).fetchone()
            if row:
                return json.loads(row[0])
        except Exception:
            logger.exception("Ошибка загрузки %s из SQLite", name)
        return default_factory()

    def _save_db(self, name: str, data: dict[str, Any]) -> None:
        text = json.dumps(data, ensure_ascii=False, default=_json_default)
        with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO kv (name, data, updated_at) VALUES (?, ?, ?)",
                (name, text, time.time()),
            )
            conn.commit()

    def _migrate_json_to_sqlite(self) -> None:
        """Переносит старые JSON-файлы в SQLite при первом запуске."""
        migration_map = {
            STORAGE_DIR / "config.json": "config",
            STORAGE_DIR / "users.json": "users",
            STORAGE_DIR / "subscriptions.json": "subscriptions",
            STORAGE_DIR / "transactions.json": "transactions",
            STORAGE_DIR / "referrals.json": "referrals",
            STORAGE_DIR / "connections.json": "connections",
        }
        for path, name in migration_map.items():
            if not path.exists():
                continue
            try:
                with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
                    existing = conn.execute("SELECT 1 FROM kv WHERE name=?", (name,)).fetchone()
                if existing:
                    continue
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._save_db(name, data)
                backup = path.with_name(f"{path.name}.migrated.{int(time.time())}")
                os.replace(path, backup)
                logger.info("Мигрирован %s -> SQLite (backup: %s)", path, backup)
            except Exception:
                logger.exception("Ошибка миграции %s", path)

    def save_config(self) -> None:
        with self._lock:
            self._save_db("config", self.config)

    def save_users(self) -> None:
        with self._lock:
            self._save_db("users", self.users)

    def save_subscriptions(self) -> None:
        with self._lock:
            self._save_db("subscriptions", self.subscriptions)

    def save_transactions(self) -> None:
        with self._lock:
            self._save_db("transactions", self.transactions)

    def save_connections(self) -> None:
        with self._lock:
            self._save_db("connections", self.connections)

    def save_referrals(self) -> None:
        with self._lock:
            self._save_db("referrals", self.referrals)

    def add_crypto_invoice(self, invoice_id: str | int, user_id: int, amount: Decimal, asset: str, rub_amount: Decimal | None = None) -> None:
        self.referrals["crypto_invoices"][str(invoice_id)] = {
            "user_id": user_id,
            "amount": amount,
            "asset": asset,
            "rub_amount": rub_amount,
            "status": "pending",
            "created_at": time.time(),
        }
        self.save_referrals()

    def get_crypto_invoice(self, invoice_id: str | int) -> dict[str, Any] | None:
        return self.referrals["crypto_invoices"].get(str(invoice_id))

    def mark_crypto_invoice(self, invoice_id: str | int, status: str, paid_amount: Decimal | None = None, paid_asset: str | None = None) -> dict[str, Any] | None:
        inv = self.referrals["crypto_invoices"].get(str(invoice_id))
        if inv:
            inv["status"] = status
            if paid_amount is not None:
                inv["paid_amount"] = paid_amount
            if paid_asset is not None:
                inv["paid_asset"] = paid_asset
            self.save_referrals()
        return inv

    def pending_crypto_invoices(self) -> list[tuple[str, dict[str, Any]]]:
        return [(iid, inv) for iid, inv in self.referrals["crypto_invoices"].items() if inv.get("status") == "pending"]

    def source_stats(self) -> dict[str, int]:
        stats: dict[str, int] = {}
        for u in self.users.values():
            src = u.get("source") or "direct"
            stats[src] = stats.get(src, 0) + 1
        return stats

    def server(self, host_id: str | None = None) -> ServerConfig:
        host = self.get_host(host_id or self.config.get("default_host_id", "main"))
        if not host:
            return ServerConfig()
        return host

    def default_host_id(self) -> str:
        return self.config.get("default_host_id", "main")

    def set_default_host(self, host_id: str) -> None:
        if host_id in self.config.get("hosts", {}):
            self.config["default_host_id"] = host_id
            self.save_config()

    def get_host(self, host_id: str | None = None) -> ServerConfig | None:
        host_id = host_id or self.config.get("default_host_id", "main")
        data = self.config.get("hosts", {}).get(host_id)
        if not data:
            return None
        return ServerConfig(**data)

    def set_host(self, server: ServerConfig) -> None:
        self.config.setdefault("hosts", {})[server.host_id] = {
            "host_id": server.host_id,
            "name": server.name,
            "address": server.address,
            "port": server.port,
            "public_key": server.public_key,
            "short_id": server.short_id,
            "server_name": server.server_name,
            "flow": server.flow,
            "network": server.network,
            "security": server.security,
            "fingerprint": server.fingerprint,
            "spider_x": server.spider_x,
            "panel_url": server.panel_url,
            "panel_path": server.panel_path,
            "panel_username": server.panel_username,
            "panel_password": server.panel_password,
            "inbound_id": server.inbound_id,
            "verify_ssl": server.verify_ssl,
            "sub_port": server.sub_port,
            "subscription_url_base": server.subscription_url_base,
            "temp_profile_enabled": server.temp_profile_enabled,
            "temp_profile_port": server.temp_profile_port,
            "temp_inbound_id": server.temp_inbound_id,
        }
        if not self.config.get("default_host_id"):
            self.config["default_host_id"] = server.host_id
        self.save_config()

    def delete_host(self, host_id: str) -> bool:
        hosts = self.config.get("hosts", {})
        if host_id in hosts and len(hosts) > 1:
            del hosts[host_id]
            if self.config.get("default_host_id") == host_id:
                self.config["default_host_id"] = next(iter(hosts), "main")
            self.save_config()
            return True
        return False

    def list_hosts(self) -> list[ServerConfig]:
        return [ServerConfig(**data) for data in self.config.get("hosts", {}).values()]

    def set_server(self, server: ServerConfig) -> None:
        self.set_host(server)

    def plans(self, host_id: str | None = None) -> dict[str, Plan]:
        result = {}
        for pid, pdata in self.config.get("plans", copy.deepcopy(DEFAULT_PLANS)).items():
            plan_host = pdata.get("host_id", "main")
            if host_id and plan_host != host_id:
                continue
            result[pid] = Plan(
                id=pid,
                name=pdata.get("name", pid),
                max_devices=int(pdata.get("max_devices", 1)),
                prices={str(k): _to_dec(v) for k, v in pdata.get("prices", {}).items()},
                host_id=plan_host,
            )
        return result

    def plan(self, plan_id: str) -> Plan | None:
        return self.plans().get(plan_id)

    def update_plan(self, plan_id: str, name: str | None = None, max_devices: int | None = None, prices: dict[str, Decimal] | None = None, host_id: str | None = None) -> None:
        plans = self.config.setdefault("plans", copy.deepcopy(DEFAULT_PLANS))
        plan_data = plans.setdefault(plan_id, {"name": plan_id, "max_devices": 1, "prices": {}, "host_id": "main"})
        if name is not None:
            plan_data["name"] = name
        if max_devices is not None:
            plan_data["max_devices"] = max_devices
        if prices is not None:
            plan_data["prices"] = {str(k): _to_dec(v) for k, v in prices.items()}
        if host_id is not None:
            plan_data["host_id"] = host_id
        self.save_config()

    def delete_plan(self, plan_id: str) -> None:
        plans = self.config.get("plans", {})
        if plan_id in plans and plan_id != "trial":
            del plans[plan_id]
            self.save_config()

    def add_plan(self, plan_id: str, name: str, max_devices: int, prices: dict[str, Decimal]) -> None:
        self.update_plan(plan_id, name, max_devices, prices)

    def get_promocode(self, code: str) -> dict[str, Any] | None:
        promos = self.config.get("promocodes", {})
        promo = promos.get(code.upper())
        if not promo:
            return None
        if promo.get("expires_at") and time.time() > promo["expires_at"]:
            return None
        if promo.get("uses", 0) >= promo.get("max_uses", 0):
            return None
        return promo

    def use_promocode(self, code: str) -> dict[str, Any] | None:
        promo = self.get_promocode(code)
        if not promo:
            return None
        promo["uses"] = promo.get("uses", 0) + 1
        self.save_config()
        return promo

    def create_promocode(self, code: str, discount_type: str, value: Decimal, max_uses: int, expires_at: float | None = None, plan_id: str | None = None) -> None:
        self.config.setdefault("promocodes", {})[code.upper()] = {
            "code": code.upper(),
            "discount_type": discount_type,
            "value": _to_dec(value),
            "max_uses": max_uses,
            "uses": 0,
            "expires_at": expires_at,
            "plan_id": plan_id,
        }
        self.save_config()

    def create_activation_code(self, code: str, plan_id: str, months: int, uses: int = 1, referrer_id: int | None = None) -> None:
        self.config.setdefault("activation_codes", {})[code.upper()] = {
            "code": code.upper(),
            "plan_id": plan_id,
            "months": months,
            "uses": uses,
            "used_by": [],
            "referrer_id": referrer_id,
        }
        self.save_config()

    def use_activation_code(self, code: str, user_id: int) -> dict[str, Any] | None:
        codes = self.config.get("activation_codes", {})
        ac = codes.get(code.upper())
        if not ac or ac.get("uses", 0) <= 0 or user_id in ac.get("used_by", []):
            return None
        ac["uses"] = ac.get("uses", 0) - 1
        ac.setdefault("used_by", []).append(user_id)
        self.save_config()
        return ac

    def delete_promocode(self, code: str) -> None:
        if code.upper() in self.config.get("promocodes", {}):
            del self.config["promocodes"][code.upper()]
            self.save_config()

    def delete_activation_code(self, code: str) -> None:
        if code.upper() in self.config.get("activation_codes", {}):
            del self.config["activation_codes"][code.upper()]
            self.save_config()

    def freeze_subscription(self, sub: Subscription, days: int) -> None:
        now = time.time()
        sub.freeze_started = now
        sub.frozen_until = now + days * 86400
        self.update_subscription(sub)
        XrayAPI.update_expiry(sub, enable=False)

    def unfreeze_subscription(self, sub: Subscription) -> None:
        if sub.frozen_until and sub.freeze_started and time.time() >= sub.frozen_until:
            sub.expires_at = sub.expires_at + max(0.0, sub.frozen_until - sub.freeze_started)
        sub.frozen_until = None
        sub.freeze_started = None
        self.update_subscription(sub)
        XrayAPI.add_or_update_client(sub, enable=True)

    def refund_subscription(self, sub: Subscription, refund_amount: Decimal) -> None:
        user = self.get_user(sub.user_id)
        refund_amount = _money_round(refund_amount)
        user["balance"] = _money_round(user["balance"] + refund_amount)
        self.update_user(user)
        self._add_transaction(sub.user_id, refund_amount, "refund", "admin", f"sub:{sub.sub_id}")
        sub.active = False
        self.update_subscription(sub)
        XrayAPI.remove_client(sub)

    def price(self, plan_id: str, months: int) -> Decimal | None:
        plan = self.plan(plan_id)
        if not plan:
            return None
        return plan.prices.get(str(months))

    def get_user(self, user_id: int, username: str = "", source: str = "direct") -> dict[str, Any]:
        key = str(user_id)
        if key not in self.users:
            self.users[key] = {
                "user_id": user_id,
                "username": username or "",
                "source": source,
                "settings": {"lang": "ru", "notifications": True, "auto_renew": True},
                "balance": Decimal("0.00"),
                "referral_balance": Decimal("0.00"),
                "referral_code": f"ref_{user_id}",
                "referred_by": None,
                "trial_used": False,
                "joined_at": time.time(),
                "channel_ok": False,
                "is_banned": False,
                "total_spent": Decimal("0.00"),
                "total_months": 0,
                "first_purchase_discount_used": False,
            }
            self.save_users()
        return self.users[key]

    def update_user(self, user: dict[str, Any]) -> None:
        self.users[str(user["user_id"])] = user
        self.save_users()

    def ban_user(self, user_id: int) -> None:
        user = self.get_user(user_id)
        user["is_banned"] = True
        self.update_user(user)

    def unban_user(self, user_id: int) -> None:
        user = self.get_user(user_id)
        user["is_banned"] = False
        self.update_user(user)

    def is_banned(self, user_id: int) -> bool:
        return bool(self.get_user(user_id).get("is_banned", False))

    def get_user_lang(self, user_id: int) -> str:
        return self.get_user(user_id).get("settings", {}).get("lang", "ru")

    def toggle_user_setting(self, user_id: int, key: str) -> Any:
        user = self.get_user(user_id)
        settings = user.setdefault("settings", {"lang": "ru", "notifications": True})
        if key == "lang":
            settings["lang"] = "en" if settings.get("lang", "ru") == "ru" else "ru"
        elif key == "notifications":
            settings["notifications"] = not settings.get("notifications", True)
        else:
            settings[key] = not settings.get(key, False)
        self.update_user(user)
        return settings[key]

    def get_transactions(self, user_id: int, limit: int = 20) -> list[dict[str, Any]]:
        txs = [tx for tx in self.transactions["txs"].values() if tx.get("user_id") == user_id]
        txs.sort(key=lambda x: x.get("created_at", 0), reverse=True)
        return txs[:limit]

    def _is_tx_duplicate(self, user_id: int, method: str, payload: str) -> bool:
        if not payload:
            return False
        for tx in self.transactions.get("txs", {}).values():
            if tx.get("user_id") == user_id and tx.get("method") == method and str(tx.get("payload")) == str(payload):
                return True
        return False

    def add_balance(self, user_id: int, amount: Decimal, source: str, method: str, payload: str = "", notify: bool = True) -> bool:
        with self._lock:
            if self._is_tx_duplicate(user_id, method, payload):
                return False
            user = self.get_user(user_id)
            amount = _money_round(amount)
            user["balance"] = _money_round(user["balance"] + amount)
            self.update_user(user)
            self._add_transaction(user_id, amount, "deposit", method, payload)
        if notify and amount > 0:
            self._notify_admins(f"Новый платёж: user {user_id} пополнил баланс на {_money_str(amount)}₽ ({method}).", "new_payment")
        return True

    def deduct_balance(self, user_id: int, amount: Decimal, method: str, payload: str = "") -> bool:
        with self._lock:
            user = self.get_user(user_id)
            amount = _money_round(amount)
            if user["balance"] < amount:
                return False
            user["balance"] = _money_round(user["balance"] - amount)
            self.update_user(user)
            self._add_transaction(user_id, -amount, "purchase", method, payload)
        return True

    def apply_referral_discount(self, user_id: int, base_price: Decimal) -> Decimal:
        with self._lock:
            user = self.get_user(user_id)
            base_price = _to_dec(base_price)
            if not user.get("referred_by") or user.get("first_purchase_discount_used"):
                return base_price
            discount = _to_dec(self.config.get("referral_first_discount", 0))
            if discount <= 0 or discount >= 100:
                return base_price
            return _money_round(base_price * (100 - discount) / 100)

    def mark_first_purchase_discount_used(self, user_id: int) -> None:
        with self._lock:
            user = self.get_user(user_id)
            if user.get("referred_by") and not user.get("first_purchase_discount_used"):
                user["first_purchase_discount_used"] = True
                self.update_user(user)

    def add_purchase_stats(self, user_id: int, amount: Decimal, months: int) -> None:
        with self._lock:
            user = self.get_user(user_id)
            amount = _to_dec(amount)
            user["total_spent"] = _money_round(user.get("total_spent", 0) + amount)
            user["total_months"] = int(user.get("total_months", 0)) + months
            self.update_user(user)

    def _add_transaction(self, user_id: int, amount: Decimal, tx_type: str, method: str, payload: str) -> None:
        with self._lock:
            tid = str(self.transactions["next_id"])
            self.transactions["next_id"] += 1
            self.transactions["txs"][tid] = {
                "id": tid,
                "user_id": user_id,
                "amount": _money_round(amount),
                "type": tx_type,
                "method": method,
                "payload": payload,
                "created_at": time.time(),
            }
            self.save_transactions()

    def _sub_from_dict(self, data: dict[str, Any]) -> Subscription:
        fields = {f for f in Subscription.__dataclass_fields__}
        filtered = {k: v for k, v in data.items() if k in fields}
        now = time.time()
        min_ts = 1577836800.0  # 2020-01-01
        max_ts = now + 10 * 365 * 86400
        created = filtered.get("created_at")
        if not isinstance(created, (int, float)) or created < min_ts or created > max_ts:
            filtered["created_at"] = now
        expires = filtered.get("expires_at")
        if not isinstance(expires, (int, float)) or expires < min_ts or expires > max_ts:
            filtered["expires_at"] = filtered["created_at"] + 3 * 86400
        return Subscription(**filtered)

    def active_subscriptions(self, user_id: int) -> list[Subscription]:
        out = []
        for sub in self.subscriptions["subs"].values():
            if sub.get("user_id") == user_id and sub.get("active") and not self._sub_from_dict(sub).is_expired:
                out.append(self._sub_from_dict(sub))
        out.sort(key=lambda s: s.expires_at, reverse=True)
        return out

    def active_subscriptions_all(self) -> list[Subscription]:
        out = []
        for sub in self.subscriptions["subs"].values():
            if sub.get("active") and not self._sub_from_dict(sub).is_expired:
                out.append(self._sub_from_dict(sub))
        out.sort(key=lambda s: s.expires_at, reverse=True)
        return out

    def get_subscription(self, sub_id: str) -> Subscription | None:
        data = self.subscriptions["subs"].get(sub_id)
        if not data:
            return None
        return self._sub_from_dict(data)

    def create_subscription(self, user_id: int, plan_id: str, months: int, is_trial: bool = False, host_id: str | None = None) -> Subscription:
        with self._lock:
            sid = str(self.subscriptions["next_id"])
            self.subscriptions["next_id"] += 1
        now = time.time()
        if is_trial:
            days = int(self.config.get("trial_days", TRIAL_DAYS))
            expires = now + days * 86400
            months = 0
        else:
            expires = now + months * 30 * 86400
        if not host_id:
            plan = self.plan(plan_id)
            host_id = plan.host_id if plan else self.default_host_id()
        email = f"vpn_{host_id}_{user_id}_{sid}"
        xray_sub_id = str(uuid5(NAMESPACE_DNS, email))
        sub = Subscription(
            sub_id=sid,
            user_id=user_id,
            plan_id=plan_id,
            months=months,
            client_uuid=str(uuid4()),
            created_at=now,
            expires_at=expires,
            devices=[],
            active=True,
            email=email,
            xray_sub_id=xray_sub_id,
            host_id=host_id,
        )
        self.subscriptions["subs"][sid] = asdict(sub)
        self.save_subscriptions()
        return sub

    def update_subscription(self, sub: Subscription) -> None:
        self.subscriptions["subs"][sub.sub_id] = asdict(sub)
        self.save_subscriptions()

    def create_withdrawal_request(self, user_id: int, amount: Decimal, card: str) -> str:
        with self._lock:
            req_id = str(self.config.setdefault("withdrawal_next_id", 1))
            self.config["withdrawal_next_id"] = int(req_id) + 1
        self.config.setdefault("withdrawal_requests", {})[req_id] = {
            "id": req_id,
            "user_id": user_id,
            "amount": _money_round(amount),
            "card": card,
            "status": "pending",
            "created_at": time.time(),
            "note": "",
        }
        self.save_config()
        return req_id

    def get_withdrawal_request(self, req_id: str) -> dict[str, Any] | None:
        return self.config.get("withdrawal_requests", {}).get(req_id)

    def update_withdrawal_request(self, req_id: str, status: str, note: str = "") -> dict[str, Any] | None:
        req = self.config.get("withdrawal_requests", {}).get(req_id)
        if not req:
            return None
        req["status"] = status
        req["note"] = note
        self.save_config()
        return req

    def withdrawal_requests(self, status: str | None = None) -> list[dict[str, Any]]:
        reqs = list(self.config.get("withdrawal_requests", {}).values())
        if status:
            reqs = [r for r in reqs if r.get("status") == status]
        reqs.sort(key=lambda r: r.get("created_at", 0), reverse=True)
        return reqs

    def security(self) -> dict[str, Any]:
        return self.config.get("security", {})

    def is_admin_user(self, user_id: int) -> bool:
        return user_id in self.config.get("admin_user_ids", [])

    def maintenance_mode(self) -> bool:
        return bool(self.config.get("maintenance", False))

    def max_devices_for(self, sub: Subscription) -> int:
        plan = self.plan(sub.plan_id)
        if not plan:
            return 1
        md = plan.max_devices
        return 9999 if md < 0 else max(1, md)

    def record_connection(self, sub_id: str, ip: str, user_agent: str, now: float, traffic_bytes: int = 0) -> dict[str, Any]:
        sub = self.get_subscription(sub_id)
        result = {"allowed": False, "reason": "unknown"}
        log = {"sub_id": sub_id, "ip": ip, "user_agent": user_agent, "timestamp": now, "allowed": False, "reason": "unknown", "traffic_bytes": traffic_bytes}
        if not sub or not sub.active or sub.is_expired:
            result = {"allowed": False, "reason": "subscription_inactive"}
        else:
            max_devices = self.max_devices_for(sub)
            ip = ip.strip()
            existing = [d for d in sub.devices if d.get("ip") == ip]
            if existing:
                dev = existing[0]
                dev["user_agent"] = user_agent
                dev["last_seen"] = now
                if traffic_bytes:
                    dev["traffic"] = dev.get("traffic", 0) + traffic_bytes
                self.update_subscription(sub)
                result = {"allowed": True, "reason": "ok", "device": dev}
            elif len(sub.devices) < max_devices:
                dev = {"ip": ip, "user_agent": user_agent, "first_seen": now, "last_seen": now, "traffic": traffic_bytes, "blocked": False}
                sub.devices.append(dev)
                self.update_subscription(sub)
                result = {"allowed": True, "reason": "registered", "device": dev}
            else:
                result = {"allowed": False, "reason": "device_limit"}

            # анти-шаринг: одновременные подключения с разных IP
            if result["allowed"]:
                window = self.security().get("sharing_window", DEFAULT_SHARING_WINDOW)
                recent_ips = set()
                for d in sub.devices:
                    if d.get("ip") and d.get("ip") != ip and d.get("last_seen") and now - d.get("last_seen", 0) < window:
                        recent_ips.add(d["ip"])
                if recent_ips:
                    result["warning"] = "sharing_suspected"
                    self._add_suspicious_event(sub.user_id, sub_id, ip, f"Одновременные подключения с IP: {', '.join(recent_ips)}")

            # лимит трафика
            traffic_limit_gb = self.security().get("traffic_limit_gb", 0.0)
            if traffic_limit_gb and result.get("allowed"):
                total_traffic = sum(d.get("traffic", 0) for d in sub.devices)
                if total_traffic > traffic_limit_gb * 1024 * 1024 * 1024:
                    result = {"allowed": False, "reason": "traffic_limit"}

        log["allowed"] = result["allowed"]
        log["reason"] = result["reason"]
        self._append_connection_log(log)
        return result

    def _add_suspicious_event(self, user_id: int, sub_id: str, ip: str, reason: str) -> None:
        evt = {"user_id": user_id, "sub_id": sub_id, "ip": ip, "reason": reason, "timestamp": time.time()}
        self.connections.setdefault("suspicious", []).append(evt)
        self.save_connections()
        if self.security().get("alert_admin", True):
            self._notify_admins(f"Подозрительная активность: user {user_id}, подписка #{sub_id}, IP {ip}\n{reason}")

    def _notify_admins(self, text: str, event_type: str | None = None) -> None:
        if event_type:
            if not self.config.get("admin_notifications", {}).get(event_type, True):
                return
        elif not self.security().get("alert_admin", True):
            return
        if not _user_bot_instance:
            return
        recipients = set()
        for uid in self.config.get("admin_user_ids", []):
            try:
                recipients.add(int(uid))
            except (ValueError, TypeError):
                pass
        support_id = self.config.get("support_id")
        if support_id:
            try:
                recipients.add(int(support_id))
            except (ValueError, TypeError):
                pass
        safe_text = _escape(text)
        for uid in recipients:
            try:
                _user_bot_instance.bot.send_message(uid, safe_text)
            except Exception:
                logger.exception("Failed to notify admin %s", uid)

    def _append_connection_log(self, log: dict[str, Any]) -> None:
        self.connections.setdefault("logs", []).append(log)
        if len(self.connections["logs"]) > MAX_CONNECTION_LOG:
            self.connections["logs"] = self.connections["logs"][-MAX_CONNECTION_LOG:]
        self.save_connections()

    def connection_logs(self, sub_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        logs = self.connections.get("logs", [])
        if sub_id:
            logs = [l for l in logs if l.get("sub_id") == sub_id]
        logs.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        return logs[:limit]

    def search_user(self, query: str) -> dict[str, Any] | None:
        q = query.strip()
        if q.startswith("@"):
            uname = q[1:].lower()
            for u in self.users.values():
                if (u.get("username") or "").lower() == uname:
                    return u
            return None
        if q.startswith("#"):
            try:
                sub_id = str(int(q[1:]))
                sub = self.subscriptions.get("subs", {}).get(sub_id)
                if sub:
                    return self.users.get(str(sub.get("user_id")))
            except (ValueError, TypeError):
                pass
            return None
        try:
            uid = int(q)
            return self.users.get(str(uid))
        except (ValueError, TypeError):
            return None

    def get_user_subscriptions(self, user_id: int) -> list[dict[str, Any]]:
        return [s for s in self.subscriptions.get("subs", {}).values() if s.get("user_id") == user_id]

    def get_user_referrals(self, user_id: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        level1 = [u for u in self.users.values() if u.get("referred_by") == user_id]
        level1_ids = {u.get("user_id") for u in level1}
        level2 = [u for u in self.users.values() if u.get("referred_by") in level1_ids]
        return level1, level2

    def broadcast_recipients(self, filter_type: str, source_value: str | None = None) -> list[int]:
        out = []
        for u in self.users.values():
            uid = int(u.get("user_id", 0))
            if not uid:
                continue
            if filter_type == "active":
                if not any(s.get("user_id") == uid and not self._sub_from_dict(s).is_expired for s in self.subscriptions.get("subs", {}).values()):
                    continue
            elif filter_type == "expired":
                subs = [s for s in self.subscriptions.get("subs", {}).values() if s.get("user_id") == uid]
                if not subs or any(not self._sub_from_dict(s).is_expired for s in subs):
                    continue
            elif filter_type == "source":
                if u.get("source") != source_value:
                    continue
            out.append(uid)
        return out

    def add_complaint(self, user_id: int, text: str) -> str:
        cid = str(int(time.time() * 1000))
        complaints = self.config.setdefault("complaints", [])
        complaints.append({"id": cid, "user_id": user_id, "text": text, "created_at": time.time(), "status": "open"})
        self.save_config()
        return cid

    def bulk_extend_active_subscriptions(self, days: int) -> int:
        now = time.time()
        count = 0
        for s in self.subscriptions.get("subs", {}).values():
            sub = self._sub_from_dict(s)
            if sub.active and not sub.is_expired:
                sub.expires_at += days * 86400
                self.update_subscription(sub)
                count += 1
        return count

    def bulk_add_balance(self, amount: Decimal) -> int:
        with self._lock:
            ts = str(int(time.time()))
            amount = _money_round(amount)
            count = 0
            for u in self.users.values():
                uid = int(u.get("user_id", 0))
                if not uid:
                    continue
                u["balance"] = _money_round(_to_dec(u.get("balance", 0)) + amount)
                tid = str(self.transactions["next_id"])
                self.transactions["next_id"] += 1
                self.transactions["txs"][tid] = {
                    "id": tid,
                    "user_id": uid,
                    "amount": amount,
                    "type": "deposit",
                    "method": "bulk",
                    "payload": f"admin:{ts}:{uid}",
                    "created_at": time.time(),
                }
                count += 1
            self.save_users()
            self.save_transactions()
        return count

    def export_csv(self, kind: str) -> Path:
        EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
        path = EXPORTS_DIR / f"{kind}.csv"
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f, delimiter=";")
            if kind == "users":
                writer.writerow(["user_id", "username", "source", "balance", "referral_balance", "trial_used", "joined_at", "referred_by"])
                for u in self.users.values():
                    writer.writerow([_csv_safe(u.get("user_id")), _csv_safe(u.get("username")), _csv_safe(u.get("source")), _csv_safe(u.get("balance")),
                                     _csv_safe(u.get("referral_balance")), _csv_safe(u.get("trial_used")),
                                     _csv_safe(_format_time(u.get("joined_at", 0))), _csv_safe(u.get("referred_by"))])
            elif kind == "subscriptions":
                writer.writerow(["sub_id", "user_id", "plan_id", "months", "created_at", "expires_at", "active", "frozen_until", "devices_count"])
                for s in self.subscriptions.get("subs", {}).values():
                    sub = self._sub_from_dict(s)
                    writer.writerow([_csv_safe(s.get("sub_id")), _csv_safe(s.get("user_id")), _csv_safe(s.get("plan_id")), _csv_safe(s.get("months")),
                                     _csv_safe(_format_time(s.get("created_at", 0))), _csv_safe(_format_time(s.get("expires_at", 0))),
                                     _csv_safe(s.get("active")), _csv_safe(s.get("frozen_until")), _csv_safe(len(s.get("devices", [])))])
            elif kind == "transactions":
                writer.writerow(["id", "user_id", "type", "amount", "method", "payload", "created_at"])
                for t in self.transactions.get("txs", {}).values():
                    writer.writerow([_csv_safe(t.get("id")), _csv_safe(t.get("user_id")), _csv_safe(t.get("type")), _csv_safe(t.get("amount")),
                                     _csv_safe(t.get("method")), _csv_safe(t.get("payload")), _csv_safe(_format_time(t.get("created_at", 0)))])
            elif kind == "connections":
                writer.writerow(["timestamp", "sub_id", "ip", "user_agent", "allowed", "reason", "traffic_bytes"])
                for l in self.connections.get("logs", []):
                    writer.writerow([_csv_safe(_format_time(l.get("timestamp", 0))), _csv_safe(l.get("sub_id")), _csv_safe(l.get("ip")),
                                     _csv_safe(l.get("user_agent")), _csv_safe(l.get("allowed")), _csv_safe(l.get("reason")), _csv_safe(l.get("traffic_bytes", 0))])
            elif kind == "complaints":
                writer.writerow(["id", "user_id", "text", "status", "created_at"])
                for c in self.config.get("complaints", []):
                    writer.writerow([_csv_safe(c.get("id")), _csv_safe(c.get("user_id")), _csv_safe(c.get("text")), _csv_safe(c.get("status")), _csv_safe(_format_time(c.get("created_at", 0)))])
        return path

    def unbind_device(self, sub_id: str, index: int, user_id: int) -> tuple[bool, str]:
        sub = self.get_subscription(sub_id)
        if not sub or sub.user_id != user_id:
            return False, "Подписка не найдена."
        if index < 0 or index >= len(sub.devices):
            return False, "Устройство не найдено."
        user = self.get_user(user_id)
        last_unbind = user.get("last_unbind_at", 0.0)
        cooldown = self.security().get("unbind_cooldown", DEFAULT_UNBIND_COOLDOWN)
        now = time.time()
        if now - last_unbind < cooldown:
            remaining = int(cooldown - (now - last_unbind))
            return False, f"Отвязать устройство можно через {remaining // 86400}д {remaining % 86400 // 3600}ч."
        del sub.devices[index]
        user["last_unbind_at"] = now
        self.update_subscription(sub)
        self.update_user(user)
        return True, "Устройство отвязано."

    def add_referral_earnings(self, user_id: int, amount: Decimal, from_user_id: int, level: int) -> None:
        with self._lock:
            user = self.get_user(user_id)
            amount = _money_round(amount)
            user["referral_balance"] = _money_round(user.get("referral_balance", 0) + amount)
            self.update_user(user)
            self._add_transaction(user_id, amount, "referral", f"level_{level}", f"from_{from_user_id}")
            key = str(user_id)
            self.referrals["earnings"].setdefault(key, {"level1": Decimal("0.00"), "level2": Decimal("0.00")})
            self.referrals["earnings"][key][f"level{level}"] = _money_round(self.referrals["earnings"][key][f"level{level}"] + amount)
            self.save_referrals()

    def process_referral_rewards(self, buyer_id: int, amount: Decimal) -> None:
        amount = _money_round(amount)
        if amount <= 0:
            return
        user = self.get_user(buyer_id)
        parent_id = user.get("referred_by")
        if parent_id:
            try:
                pid = int(parent_id)
                reward = _money_round(amount * Decimal("0.10"))
                if reward > 0 and self.get_user(pid):
                    self.add_referral_earnings(pid, reward, buyer_id, 1)
                    grandparent = self.get_user(pid).get("referred_by")
                    if grandparent:
                        gpid = int(grandparent)
                        reward2 = _money_round(amount * Decimal("0.05"))
                        if reward2 > 0 and self.get_user(gpid):
                            self.add_referral_earnings(gpid, reward2, buyer_id, 2)
            except (ValueError, TypeError):
                pass



def _escape(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _csv_safe(value: Any) -> str:
    s = str(value)
    if s.startswith(("=", "+", "-", "@", "\t", "\r", "\n")) or "\n" in s or "\r" in s:
        return "'" + s
    return s


CENTS = Decimal("0.01")

def _to_dec(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal("0")
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        s = value.strip().replace(",", ".")
        if not s:
            return Decimal("0")
        return Decimal(s)
    return Decimal(str(value))


def _money_round(value: Any) -> Decimal:
    return _to_dec(value).quantize(CENTS, rounding=ROUND_HALF_UP)


def _money_str(value: Any) -> str:
    return f"{_to_dec(value):.2f}"


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _format_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")


def _price_text(plan: Plan, months: str) -> str:
    price = plan.prices.get(months)
    return f"{_money_str(price)}₽" if price is not None else "?₽"


def format_subscription(sub: Subscription) -> str:
    plan = storage.plan(sub.plan_id)
    if sub.frozen_until and time.time() < sub.frozen_until:
        status = f"Заморожена до {_format_time(sub.frozen_until)}"
    else:
        status = "Активна" if sub.active and not sub.is_expired else "Истекла/неактивна"
    host = storage.get_host(sub.host_id) or storage.server()
    host_name = _escape(host.name if host else sub.host_id)
    lines = [
        f"<b>Подписка #{sub.sub_id}</b>",
        f"Тариф: {_escape(plan.name if plan else sub.plan_id)}",
        f"Сервер: {host_name}",
        f"Срок: {sub.months} мес." if sub.months else f"Пробный период",
        f"Статус: {status}",
        f"Действует до: {_format_time(sub.effective_expires_at)}",
        f"Устройств: {len(sub.devices)} / {_escape(plan.device_text if plan else '?')}",
    ]
    return "\n".join(lines)


# ==================== Payment integrations (skeleton) ====================


class CryptoBotAPI:
    """Реальная интеграция с @CryptoBot через HTTP API."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.base_url = "https://pay.crypt.bot/api/" if not token.startswith("test_") else "https://testnet-pay.crypt.bot/api/"

    def _headers(self) -> dict[str, str]:
        return {"Crypto-Pay-API-Token": self.token, "Content-Type": "application/json"}

    def create_invoice(self, user_id: int, amount: Decimal, asset: str = "USDT") -> dict[str, Any]:
        if not self.token:
            return {"ok": False, "error": "Crypto Bot token not set"}
        amount_str = str(amount)
        payload = json.dumps({"user_id": user_id, "amount": amount_str, "asset": asset})
        body = {
            "asset": asset,
            "amount": amount_str,
            "description": "Пополнение баланса VPN-бота",
            "hidden_message": "Спасибо за оплату!",
            "payload": payload,
            "allow_comments": False,
            "allow_anonymous": False,
        }
        try:
            r = requests.post(self.base_url + "createInvoice", headers=self._headers(), json=body, timeout=15)
            data = r.json()
            if not data.get("ok"):
                return {"ok": False, "error": data.get("error", {}).get("message", str(data))}
            return data.get("result", data)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_invoices(self, invoice_ids: list[str | int] | None = None, status: str | None = None) -> list[dict[str, Any]]:
        if not self.token:
            return []
        params: dict[str, Any] = {}
        if invoice_ids:
            params["invoice_ids"] = ",".join(str(i) for i in invoice_ids)
        if status:
            params["status"] = status
        try:
            r = requests.get(self.base_url + "getInvoices", headers=self._headers(), params=params, timeout=15)
            data = r.json()
            if not data.get("ok"):
                logger.error("CryptoBot getInvoices error: %s", data)
                return []
            return data.get("result", {}).get("items", [])
        except Exception:
            logger.exception("CryptoBot getInvoices failed")
            return []


class CryptoBotPoller:
    """Фоновый поток опроса статуса инвойсов Crypto Bot."""

    def __init__(self, bot: "UserBot") -> None:
        self.bot = bot
        self.api = CryptoBotAPI(storage.config.get("crypto_bot_token", ""))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if not storage.config.get("crypto_bot_token"):
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll()
            except Exception:
                logger.exception("CryptoBotPoller error")
            time.sleep(30)

    def _poll(self) -> None:
        pending = storage.pending_crypto_invoices()
        if not pending:
            return
        ids = [iid for iid, _ in pending]
        for inv in self.api.get_invoices(invoice_ids=ids):
            iid = str(inv.get("invoice_id"))
            status = inv.get("status")
            local = storage.get_crypto_invoice(iid)
            if not local or local.get("status") != "pending":
                continue
            if status == "paid":
                payload = inv.get("payload") or "{}"
                try:
                    info = json.loads(payload)
                except Exception:
                    info = {}
                uid = info.get("user_id") or local.get("user_id")
                paid_amount = _to_dec(inv.get("paid_amount") or inv.get("amount") or local.get("amount") or 0)
                paid_asset = inv.get("paid_asset") or local.get("asset")
                if uid and paid_amount:
                    rub_amount = local.get("rub_amount")
                    if not rub_amount:
                        rate = RatesFetcher().get_rate(paid_asset) or Decimal("0")
                        rub_amount = _money_round(paid_amount * rate) if rate else Decimal("0.00")
                    credited = storage.add_balance(int(uid), rub_amount, "Crypto Bot", f"cryptobot:{paid_asset}", iid)
                    if credited:
                        storage.mark_crypto_invoice(iid, "paid", paid_amount, paid_asset)
                        try:
                            self.bot.bot.send_message(int(uid), f"Баланс пополнен на {_money_str(rub_amount)}₽ (~{paid_amount} {paid_asset}) через Crypto Bot.")
                        except Exception:
                            pass
            elif status in ("expired", "cancelled"):
                storage.mark_crypto_invoice(iid, status)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)


class SubscriptionScheduler:
    """Фоновый поток автопродления, заморозки и уведомлений о подписках."""

    def __init__(self, bot: "UserBot") -> None:
        self.bot = bot
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                logger.exception("SubscriptionScheduler error")
            time.sleep(60)

    def _notify(self, user_id: int, text: str) -> None:
        user = storage.get_user(user_id)
        if not user.get("settings", {}).get("notifications", True):
            return
        try:
            self.bot.bot.send_message(user_id, text)
        except Exception:
            logger.exception("Failed to notify user %s", user_id)

    def _tick(self) -> None:
        now = time.time()
        # cleanup expired subscriptions after grace period
        grace = max(0, int(storage.config.get("expired_cleanup_days", 5))) * 86400
        for sub_id, data in list(storage.subscriptions["subs"].items()):
            sub = storage._sub_from_dict(data)
            if sub.is_expired and not sub.active and grace and (now - sub.expires_at) > grace:
                try:
                    XrayAPI.remove_client(sub)
                except Exception:
                    pass
                del storage.subscriptions["subs"][sub_id]
                storage.save_subscriptions()
                continue
        for sub_id, data in list(storage.subscriptions["subs"].items()):
            sub = storage._sub_from_dict(data)
            if not sub.active and not sub.frozen_until:
                continue
            # unfreeze
            if sub.frozen_until and now >= sub.frozen_until:
                storage.unfreeze_subscription(sub)
                self._notify(sub.user_id, f"Подписка #{sub.sub_id} разморожена. Действует до {_format_time(sub.expires_at)}.")
                continue
            if sub.frozen_until:
                continue
            remaining = sub.expires_at - now
            warnings = sub.warnings or {}
            # 24h warning
            if remaining <= 86400 and remaining > 3600 and not warnings.get("24h"):
                warnings["24h"] = True
                data["warnings"] = warnings
                storage.save_subscriptions()
                self._notify(sub.user_id, f"Подписка #{sub.sub_id} истекает через 24 часа. Пополните баланс для автопродления.")
                storage._notify_admins(f"Подписка #{sub.sub_id} (user {sub.user_id}) истекает через 24 часа.", "expiring_sub")
            # 1h warning
            elif remaining <= 3600 and remaining > 0 and not warnings.get("1h"):
                warnings["1h"] = True
                data["warnings"] = warnings
                storage.save_subscriptions()
                self._notify(sub.user_id, f"Подписка #{sub.sub_id} истекает через час. Пополните баланс.")
            # expired or renewal
            elif remaining <= 0 and sub.active:
                user = storage.get_user(sub.user_id)
                if not user or not user.get("settings", {}).get("auto_renew", True):
                    sub.active = False
                    data["active"] = False
                    storage.save_subscriptions()
                    XrayAPI.remove_client(sub)
                    self._notify(sub.user_id, f"Подписка #{sub.sub_id} истекла. Пополните баланс и продлите.")
                    continue
                # try renew same plan/months, fallback to 1 month
                plan = storage.plan(sub.plan_id)
                months = sub.months if sub.months else 1
                price = storage.price(sub.plan_id, months) if sub.months else None
                if price is None:
                    # try 1 month price
                    months = 1
                    price = storage.price(sub.plan_id, months)
                if price is None or user["balance"] < price:
                    sub.active = False
                    data["active"] = False
                    storage.save_subscriptions()
                    XrayAPI.remove_client(sub)
                    self._notify(sub.user_id, f"Подписка #{sub.sub_id} истекла. Недостаточно средств для автопродления.")
                    continue
                user["balance"] = _money_round(user["balance"] - price)
                storage.update_user(user)
                storage._add_transaction(sub.user_id, -price, "purchase", "autorenew", f"sub:{sub.sub_id}")
                sub.expires_at = sub.expires_at + months * 30 * 86400
                sub.warnings = {}
                sub.active = True
                storage.update_subscription(sub)
                storage.process_referral_rewards(sub.user_id, price)
                storage.add_purchase_stats(sub.user_id, price, months)
                XrayAPI.add_or_update_client(sub)
                self._notify(sub.user_id, f"Подписка #{sub.sub_id} автоматически продлена на {months} мес. Списано {_money_str(price)}₽.")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)


class RatesFetcher:
    """Фоновый поток автообновления курсов USD (ЦБ РФ) и TON (CoinGecko) к рублю."""

    CBR_URL = "https://www.cbr-xml-daily.ru/daily_json.js"
    COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price?ids=the-open-network&vs_currencies=rub"

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        self._update()
        while not self._stop.is_set():
            self._stop.wait(3600)
            if not self._stop.is_set():
                self._update()

    def _update(self) -> None:
        try:
            usd = self._fetch_usd()
            ton = self._fetch_ton()
            storage.config["rates"]["USD"] = usd
            storage.config["rates"]["TON"] = ton
            storage.config["rates"]["updated_at"] = time.time()
            storage.save_config()
            logger.info("Rates updated: USD=%s, TON=%s", usd, ton)
        except Exception:
            logger.exception("RatesFetcher failed")

    def _fetch_usd(self) -> Decimal:
        try:
            r = requests.get(self.CBR_URL, timeout=20)
            r.raise_for_status()
            data = r.json()
            return Decimal(str(data["Valute"]["USD"]["Value"]))
        except Exception:
            last = storage.config.get("rates", {}).get("USD")
            if last:
                return _to_dec(last)
            raise

    def _fetch_ton(self) -> Decimal:
        try:
            r = requests.get(self.COINGECKO_URL, timeout=20)
            r.raise_for_status()
            data = r.json()
            return Decimal(str(data["the-open-network"]["rub"]))
        except Exception:
            last = storage.config.get("rates", {}).get("TON")
            if last:
                return _to_dec(last)
            raise

    def get_rate(self, asset: str) -> Decimal | None:
        rates = storage.config.get("rates", {})
        if asset == "USDT":
            return _to_dec(rates.get("USD")) or None
        if asset == "TON":
            return _to_dec(rates.get("TON")) or None
        return None

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)


class DeviceAuthHandler(http.server.BaseHTTPRequestHandler):
    """HTTP-эндпоинт для регистрации подключений устройств."""

    def _json_response(self, status: int, data: dict[str, Any]) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    MAX_BODY_SIZE = 4096

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if not length or length > self.MAX_BODY_SIZE:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def _get_token(self) -> str:
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth.split(" ", 1)[1].strip()
        data = self._read_json() if self.path == "/connect" else {}
        return str(data.get("token", "")).strip()

    def do_POST(self) -> None:
        if self.path != "/connect":
            self._json_response(404, {"error": "not found"})
            return
        data = self._read_json()
        token = (self.headers.get("Authorization", "") or "").split(" ")[-1] if self.headers.get("Authorization", "").lower().startswith("bearer ") else str(data.get("token", "")).strip()
        expected = storage.config.get("device_auth_token", "")
        if not expected:
            self._json_response(503, {"allowed": False, "reason": "auth_not_configured"})
            return
        if token != expected:
            self._json_response(403, {"allowed": False, "reason": "invalid_token"})
            return
        sub_id = str(data.get("sub_id", "")).strip()
        ip = self.client_address[0]
        user_agent = str(data.get("user_agent", "")).strip() or "unknown"
        try:
            traffic_bytes = int(data.get("traffic_bytes", 0) or 0)
        except (ValueError, TypeError):
            traffic_bytes = 0
        if not sub_id:
            self._json_response(400, {"allowed": False, "reason": "missing sub_id"})
            return
        try:
            now = time.time()
            result = storage.record_connection(sub_id, ip, user_agent, now, traffic_bytes)
            self._json_response(200, result)
        except Exception:
            logger.exception("DeviceAuthHandler connect error")
            self._json_response(500, {"allowed": False, "reason": "server_error"})

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug(fmt, *args)


class DeviceAuthServer:
    """Фоновый HTTP-сервер авторизации устройств."""

    def __init__(self, port: int = 8080) -> None:
        self.port = port
        self._server: socketserver.ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        try:
            self._server = socketserver.ThreadingTCPServer(("0.0.0.0", self.port), DeviceAuthHandler)
        except OSError as e:
            logger.error("DeviceAuthServer cannot bind to port %s: %s", self.port, e)
            return
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        logger.info("DeviceAuthServer started on port %s", self.port)

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread:
            self._thread.join(timeout=5)


TEMP_SESSIONS: dict[str, dict[str, Any]] = {}
TEMP_LOCK = threading.RLock()
TEMP_LAST_CREATE: dict[str, float] = {}
TEMP_RATE_LIMIT_WINDOW = 3600
_temp_profile_server: "TempProfileServer | None" = None


class TempProfileHandler(http.server.BaseHTTPRequestHandler):
    """HTTP-эндпоинт для 30-минутных временных VLESS профилей."""

    def _text_response(self, status: int, text: str, content_type: str = "text/html; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, url: str) -> None:
        self.send_response(302)
        self.send_header("Location", url)
        self.end_headers()

    def _get_session_id(self) -> str | None:
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            kv = part.strip().split("=", 1)
            if len(kv) == 2 and kv[0] == "temp_session":
                return kv[1]
        return None

    def _set_session_cookie(self, session_id: str) -> None:
        self.send_header("Set-Cookie", f"temp_session={session_id}; Path=/; Max-Age=1800")

    @staticmethod
    def _get_query_token(path: str) -> str:
        if "?" not in path:
            return ""
        qs = path.split("?", 1)[1]
        for part in qs.split("&"):
            if part.startswith("t="):
                return part[2:]
        return ""

    def _check_temp_token(self) -> bool:
        token = self._get_query_token(self.path)
        expected = storage.config.get("temp_profile_token", "")
        return bool(expected and token and token == expected)

    def _create_temp_session(self) -> dict[str, Any]:
        ip = self.client_address[0]
        now = time.time()
        with TEMP_LOCK:
            last = TEMP_LAST_CREATE.get(ip, 0)
            if now - last < TEMP_RATE_LIMIT_WINDOW:
                return None
        server = storage.server()
        session_id = str(uuid4())
        email = f"temp_{session_id}"
        xray_sub_id = str(uuid5(NAMESPACE_DNS, email))
        client_uuid = str(uuid4())
        expires_at = now + 30 * 60
        inbound_id = server.temp_inbound_id if server.temp_inbound_id else server.inbound_id
        expiry_ms = XrayAPI._timestamp_to_xray_ms(expires_at)
        XrayAPI.add_or_update_client_raw(
            client_uuid=client_uuid,
            email=email,
            xray_sub_id=xray_sub_id,
            expiry_ms=expiry_ms,
            enable=True,
            inbound_id=inbound_id,
        )
        session = {
            "session_id": session_id,
            "email": email,
            "xray_sub_id": xray_sub_id,
            "client_uuid": client_uuid,
            "inbound_id": inbound_id,
            "expires_at": expires_at,
        }
        with TEMP_LOCK:
            TEMP_SESSIONS[session_id] = session
            TEMP_LAST_CREATE[ip] = now
        return session

    def _get_or_create_session(self) -> dict[str, Any] | None:
        session_id = self._get_session_id()
        with TEMP_LOCK:
            session = TEMP_SESSIONS.get(session_id) if session_id else None
        if session and session["expires_at"] > time.time():
            return session
        try:
            result = self._create_temp_session()
            if result is None:
                return None
            return result
        except Exception:
            logger.exception("TempProfileHandler create session error")
        return None

    def _render_page(self, session: dict[str, Any]) -> str:
        server = storage.server()
        vless_url_raw = generate_vless_url_raw(
            session["client_uuid"], session["email"], session["xray_sub_id"], server
        )
        sub_url_raw = generate_subscription_url_raw(session["xray_sub_id"], server)
        vless_url = html.escape(vless_url_raw, quote=True)
        sub_url = html.escape(sub_url_raw, quote=True)
        expires = datetime.fromtimestamp(session["expires_at"]).strftime("%H:%M:%S")
        qr_bytes = _generate_qr_code(vless_url_raw)
        qr_b64 = base64.b64encode(qr_bytes).decode("ascii") if qr_bytes else ""
        qr_img = f'<img src="data:image/png;base64,{qr_b64}" alt="QR" style="max-width:280px;">' if qr_b64 else ""
        return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>Пробный VPN 30 минут</title>
<style>
body {{ font-family: Arial, sans-serif; text-align: center; padding: 20px; background: #f5f5f5; }}
.box {{ background: #fff; border-radius: 12px; padding: 20px; max-width: 420px; margin: auto; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
a.button {{ display: inline-block; margin: 8px 4px; padding: 12px 18px; background: #28a745; color: #fff; text-decoration: none; border-radius: 8px; }}
a.link {{ display: block; margin: 10px 0; word-break: break-all; color: #007bff; }}
</style>
</head>
<body>
<div class="box">
<h1>Бесплатный VPN 30 минут</h1>
<p>Действует до: <b>{expires}</b></p>
{qr_img}
<br>
<a class="button" href="{vless_url}">Подключиться (VLESS)</a>
<a class="button" href="{sub_url}">Подписка</a>
<br>
<a class="link" href="{vless_url}">{vless_url}</a>
<p>1. Установите v2rayNG / V2Box / Streisand.<br>2. Отсканируйте QR или нажмите кнопку.</p>
</div>
</body>
</html>"""

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/health":
            self._text_response(200, "ok", "text/plain")
            return
        if path == "/favicon.ico":
            self.send_response(404)
            self.end_headers()
            return
        if path.startswith("/sub/"):
            self._serve_sub(path)
            return
        if not self._check_temp_token():
            self._text_response(403, "Доступ запрещён. Укажите корректный токен в параметре ?t=...")
            return
        session = self._get_or_create_session()
        if session is None:
            self._text_response(429, "Не удалось создать временный профиль. Проверьте настройки 3X-UI или превышен лимит запросов.")
            return
        html = self._render_page(session)
        self.send_response(200)
        self._set_session_cookie(session["session_id"])
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def _serve_sub(self, path: str) -> None:
        # /sub/<session_id> or use cookie
        session_id = self._get_session_id()
        with TEMP_LOCK:
            session = TEMP_SESSIONS.get(session_id) if session_id else None
        if not session:
            parts = path.split("/")
            session_id = parts[-1] if len(parts) >= 2 else ""
            with TEMP_LOCK:
                session = TEMP_SESSIONS.get(session_id)
        if not session or session["expires_at"] <= time.time():
            self._text_response(403, "Профиль истёк или не найден.", "text/plain")
            return
        server = storage.server()
        vless_url = generate_vless_url_raw(
            session["client_uuid"], session["email"], session["xray_sub_id"], server
        )
        text = f"{vless_url}\n"
        self._text_response(200, text, "text/plain; charset=utf-8")

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug(fmt, *args)


class TempProfileServer:
    """Фоновый HTTP-сервер временных 30-минутных профилей."""

    def __init__(self, port: int = 8081) -> None:
        self.port = port
        self._server: socketserver.ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None
        self._cleanup_thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        try:
            self._server = socketserver.ThreadingTCPServer(("0.0.0.0", self.port), TempProfileHandler)
        except OSError as e:
            logger.error("TempProfileServer cannot bind to port %s: %s", self.port, e)
            return
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._cleanup_thread.start()
        threading.Thread(target=self._cleanup_xray_temp, daemon=True).start()
        logger.info("TempProfileServer started on port %s", self.port)

    def _cleanup_xray_temp(self) -> None:
        try:
            XrayAPI.cleanup_temp_clients()
        except Exception:
            logger.exception("TempProfileServer initial Xray temp cleanup error")

    def _cleanup_loop(self) -> None:
        while not self._stop.is_set():
            self._cleanup_expired()
            self._stop.wait(60)

    def _cleanup_expired(self) -> None:
        now = time.time()
        with TEMP_LOCK:
            expired = [sid for sid, s in TEMP_SESSIONS.items() if s["expires_at"] <= now]
        for sid in expired:
            session = TEMP_SESSIONS.pop(sid, {})
            email = session.get("email")
            inbound_id = session.get("inbound_id")
            if email:
                try:
                    XrayAPI.remove_client_by_email(email, inbound_id)
                    logger.info("TempProfileServer removed expired profile %s", email)
                except Exception:
                    logger.exception("TempProfileServer cleanup error for %s", email)

    def stop(self) -> None:
        self._stop.set()
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread:
            self._thread.join(timeout=5)
        if self._cleanup_thread:
            self._cleanup_thread.join(timeout=5)


class StarsPayment:
    """Реальная отправка инвойса Telegram Stars."""

    @staticmethod
    def send_invoice(bot, chat_id: int, user_id: int, amount: int, title: str = "Пополнение", description: str = "Баланс VPN-бота", rub_amount: Decimal | None = None) -> bool:
        """Отправляет инвойс на указанное количество Telegram Stars."""
        payload = f"stars_{user_id}_{int(rub_amount or amount)}"
        try:
            bot.send_invoice(
                chat_id=chat_id,
                title=title,
                description=description,
                invoice_payload=payload,
                provider_token="",
                currency="XTR",
                prices=[LabeledPrice(label="Звёзды", amount=amount)],
                start_parameter="stars_deposit",
            )
            return True
        except Exception as e:
            logger.exception("Stars invoice creation failed: %s", e)
            return False


class XrayAPI:
    """Реальная синхронизация подписок с панелью 3X-UI через HTTP API."""

    @staticmethod
    def _panel_api_base(server: ServerConfig) -> str:
        base = server.panel_url.rstrip("/")
        path = (server.panel_path or "").strip("/")
        if path:
            return f"{base}/{path}"
        return base

    @staticmethod
    def _login(session: requests.Session, server: ServerConfig) -> bool:
        if not server.panel_url:
            return False
        url = f"{server.panel_url.rstrip('/')}/login"
        try:
            r = session.post(
                url,
                data={"username": server.panel_username, "password": server.panel_password},
                verify=server.verify_ssl,
                timeout=15,
            )
            if r.status_code != 200:
                logger.error("3X-UI login failed: %s", r.status_code)
                return False
            try:
                data = r.json()
                if data.get("success"):
                    return True
            except Exception:
                return "success" in r.text.lower()
        except Exception:
            logger.exception("3X-UI login error")
        return False

    @staticmethod
    def _get_inbound(session: requests.Session, server: ServerConfig, inbound_id: int) -> dict[str, Any] | None:
        url = f"{XrayAPI._panel_api_base(server)}/api/inbounds/get/{inbound_id}"
        try:
            r = session.get(url, verify=server.verify_ssl, timeout=15)
            if r.status_code != 200:
                logger.error("3X-UI get_inbound failed: %s", r.status_code)
                return None
            data = r.json()
            if data.get("success"):
                return data.get("obj")
        except Exception:
            logger.exception("3X-UI get_inbound error")
        return None

    @staticmethod
    def _update_inbound(session: requests.Session, server: ServerConfig, inbound_id: int, data: dict[str, Any]) -> bool:
        url = f"{XrayAPI._panel_api_base(server)}/api/inbounds/update/{inbound_id}"
        try:
            r = session.post(url, json=data, verify=server.verify_ssl, timeout=30)
            if r.status_code != 200:
                logger.error("3X-UI update_inbound failed: %s", r.status_code)
                return False
            try:
                resp = r.json()
                return resp.get("success", False)
            except Exception:
                return "success" in r.text.lower()
        except Exception:
            logger.exception("3X-UI update_inbound error")
        return False

    @staticmethod
    def _get_flow_from_inbound(inbound: dict[str, Any]) -> str:
        try:
            settings = json.loads(inbound.get("settings", "{}"))
            clients = settings.get("clients", [])
            if clients and clients[0].get("flow"):
                return clients[0]["flow"]
            stream = json.loads(inbound.get("streamSettings", "{}"))
            return stream.get("realitySettings", {}).get("flow", "")
        except Exception:
            return ""

    @staticmethod
    def _ensure_email_and_sub_id(sub: Subscription) -> None:
        if not sub.email:
            sub.email = f"vpn_{sub.user_id}_{sub.sub_id}"
        if not sub.xray_sub_id:
            sub.xray_sub_id = str(uuid5(NAMESPACE_DNS, sub.email))

    @staticmethod
    def _timestamp_to_xray_ms(ts: float) -> int:
        now = time.time()
        if ts <= now:
            return 1  # уже истекло, не бессрочно (0 в 3X-UI означает безлимит)
        ms = int(ts * 1000)
        if ms < 1577836800000 or ms > 2000000000000:
            return 1
        return ms

    @staticmethod
    def _expiry_ms(sub: Subscription) -> int:
        return XrayAPI._timestamp_to_xray_ms(sub.effective_expires_at)

    @staticmethod
    def _build_client_data(
        client_uuid: str,
        email: str,
        xray_sub_id: str,
        expiry_ms: int,
        enable: bool,
        server: ServerConfig,
        flow: str,
    ) -> dict[str, Any]:
        return {
            "id": client_uuid,
            "flow": flow,
            "email": email,
            "limitIp": 0,
            "totalGB": 0,
            "expiryTime": expiry_ms,
            "enable": enable,
            "tgId": "",
            "subId": xray_sub_id,
            "reset": 0,
            "fingerprint": server.fingerprint,
            "publicKey": server.public_key,
            "shortId": server.short_id,
            "spiderX": server.spider_x,
        }

    @staticmethod
    def _update_inbound_clients(
        session: requests.Session,
        server: ServerConfig,
        inbound_id: int,
        inbound: dict[str, Any],
        clients: list[dict[str, Any]],
    ) -> bool:
        try:
            settings = json.loads(inbound.get("settings", "{}"))
            settings["clients"] = clients
            update_data = {
                "up": inbound.get("up", 0),
                "down": inbound.get("down", 0),
                "total": inbound.get("total", 0),
                "remark": inbound.get("remark", ""),
                "enable": inbound.get("enable", True),
                "expiryTime": inbound.get("expiryTime", 0),
                "listen": inbound.get("listen", ""),
                "port": inbound.get("port", 0),
                "protocol": inbound.get("protocol", "vless"),
                "settings": json.dumps(settings, indent=2),
                "streamSettings": inbound.get("streamSettings", "{}"),
                "sniffing": inbound.get("sniffing", "{}"),
                "allocate": inbound.get("allocate", ""),
            }
            return XrayAPI._update_inbound(session, server, inbound_id, update_data)
        except Exception:
            logger.exception("3X-UI _update_inbound_clients error")
        return False

    @staticmethod
    def _get_host(host_id: str | None = None) -> ServerConfig | None:
        return storage.get_host(host_id) or storage.server()

    @staticmethod
    def get_clients(inbound_id: int | None = None, host_id: str | None = None) -> list[dict[str, Any]]:
        server = XrayAPI._get_host(host_id)
        if not server or not server.panel_url:
            return []
        if inbound_id is None:
            inbound_id = server.inbound_id
        session = requests.Session()
        if not XrayAPI._login(session, server):
            return []
        inbound = XrayAPI._get_inbound(session, server, inbound_id)
        if not inbound:
            return []
        try:
            settings = json.loads(inbound.get("settings", "{}"))
            return settings.get("clients", [])
        except Exception:
            logger.exception("3X-UI get_clients error")
        return []

    @staticmethod
    def add_or_update_client_raw(
        client_uuid: str,
        email: str,
        xray_sub_id: str,
        expiry_ms: int,
        enable: bool = True,
        inbound_id: int | None = None,
        host_id: str | None = None,
    ) -> bool:
        server = XrayAPI._get_host(host_id)
        if not server or not server.panel_url:
            return False
        if inbound_id is None:
            inbound_id = server.inbound_id
        session = requests.Session()
        if not XrayAPI._login(session, server):
            return False
        inbound = XrayAPI._get_inbound(session, server, inbound_id)
        if not inbound:
            return False
        try:
            settings = json.loads(inbound.get("settings", "{}"))
            clients = settings.get("clients", [])
            flow = XrayAPI._get_flow_from_inbound(inbound)
            client_data = XrayAPI._build_client_data(
                client_uuid, email, xray_sub_id, expiry_ms, enable, server, flow
            )
            updated = False
            for c in clients:
                if c.get("email") == email:
                    c.update(client_data)
                    updated = True
                    break
            if not updated:
                clients.append(client_data)
            return XrayAPI._update_inbound_clients(session, server, inbound_id, inbound, clients)
        except Exception:
            logger.exception("3X-UI add_or_update_client_raw error (email=%s)", email)
        return False

    @staticmethod
    def remove_client_by_email(email: str, inbound_id: int | None = None, host_id: str | None = None) -> bool:
        server = XrayAPI._get_host(host_id)
        if not server or not server.panel_url or not email:
            return False
        if inbound_id is None:
            inbound_id = server.inbound_id
        session = requests.Session()
        if not XrayAPI._login(session, server):
            return False
        inbound = XrayAPI._get_inbound(session, server, inbound_id)
        if not inbound:
            return False
        try:
            settings = json.loads(inbound.get("settings", "{}"))
            clients = settings.get("clients", [])
            new_clients = [c for c in clients if c.get("email") != email]
            if len(new_clients) == len(clients):
                return True
            return XrayAPI._update_inbound_clients(session, server, inbound_id, inbound, new_clients)
        except Exception:
            logger.exception("3X-UI remove_client_by_email error (email=%s)", email)
        return False

    @staticmethod
    def cleanup_temp_clients(host_id: str | None = None) -> int:
        server = XrayAPI._get_host(host_id)
        if not server or not server.panel_url:
            return 0
        session = requests.Session()
        if not XrayAPI._login(session, server):
            return 0
        inbound_id = server.inbound_id
        inbound = XrayAPI._get_inbound(session, server, inbound_id)
        if not inbound:
            return 0
        try:
            settings = json.loads(inbound.get("settings", "{}"))
            clients = settings.get("clients", [])
            new_clients = [c for c in clients if not str(c.get("email", "")).startswith("temp_")]
            removed = len(clients) - len(new_clients)
            if removed and XrayAPI._update_inbound_clients(session, server, inbound_id, inbound, new_clients):
                logger.info("3X-UI temp clients cleanup removed %s clients", removed)
                return removed
        except Exception:
            logger.exception("3X-UI cleanup_temp_clients error")
        return 0

    @staticmethod
    def add_or_update_client(sub: Subscription, enable: bool = True) -> bool:
        server = XrayAPI._get_host(sub.host_id)
        if not server or not server.panel_url:
            logger.info("3X-UI panel URL not configured, skipping client sync for sub #%s", sub.sub_id)
            return False
        XrayAPI._ensure_email_and_sub_id(sub)
        expiry_ms = XrayAPI._expiry_ms(sub) if enable else 0
        if XrayAPI.add_or_update_client_raw(
            sub.client_uuid,
            sub.email,
            sub.xray_sub_id,
            expiry_ms,
            enable,
            server.inbound_id,
            sub.host_id,
        ):
            sub.xray_synced = True
            storage.update_subscription(sub)
            logger.info("3X-UI client synced for sub #%s host=%s (email=%s)", sub.sub_id, sub.host_id, sub.email)
            return True
        return False

    @staticmethod
    def remove_client(sub: Subscription) -> bool:
        server = XrayAPI._get_host(sub.host_id)
        if not server or not server.panel_url or not sub.email:
            return False
        if XrayAPI.remove_client_by_email(sub.email, server.inbound_id, sub.host_id):
            sub.xray_synced = False
            storage.update_subscription(sub)
            logger.info("3X-UI client removed for sub #%s host=%s", sub.sub_id, sub.host_id)
            return True
        return False

    @staticmethod
    def update_expiry(sub: Subscription, enable: bool = True) -> bool:
        return XrayAPI.add_or_update_client(sub, enable=enable)

    @staticmethod
    def get_client_stats(sub: Subscription) -> dict[str, int]:
        server = XrayAPI._get_host(sub.host_id)
        if not server or not server.panel_url or not sub.email:
            return {"upload": 0, "download": 0}
        session = requests.Session()
        if not XrayAPI._login(session, server):
            return {"upload": 0, "download": 0}
        url = f"{XrayAPI._panel_api_base(server)}/api/inbounds/getClientTraffics/{sub.email}"
        try:
            r = session.get(url, verify=server.verify_ssl, timeout=15)
            if r.status_code == 200:
                data = r.json()
                if data.get("success"):
                    obj = data.get("obj") or {}
                    if isinstance(obj, dict):
                        return {"upload": int(obj.get("up", 0)), "download": int(obj.get("down", 0))}
        except Exception:
            logger.exception("3X-UI get_client_stats error for sub #%s", sub.sub_id)
        return {"upload": 0, "download": 0}

    @staticmethod
    def get_global_stats() -> dict[str, int]:
        server = storage.server()  # default host
        if not server.panel_url:
            return {"upload": 0, "download": 0}
        session = requests.Session()
        if not XrayAPI._login(session, server):
            return {"upload": 0, "download": 0}
        inbound = XrayAPI._get_inbound(session, server, server.inbound_id)
        if inbound:
            return {"upload": int(inbound.get("up", 0)), "download": int(inbound.get("down", 0))}
        return {"upload": 0, "download": 0}

    @staticmethod
    def sync_subscriptions() -> dict[str, Any]:
        """Сравнивает активные подписки в JSON и клиентов в 3X-UI (multi-host)."""
        result: dict[str, Any] = {"ok": [], "mismatch": [], "missing": [], "orphan": []}
        hosts = [h for h in storage.list_hosts() if h.panel_url]
        if not hosts:
            result["error"] = "3X-UI panel URL not configured"
            return result
        for host in hosts:
            clients = XrayAPI.get_clients(host.inbound_id, host.host_id)
            by_email = {c.get("email"): c for c in clients if c.get("email")}
            for sub in storage.active_subscriptions_all():
                if sub.host_id != host.host_id:
                    continue
                XrayAPI._ensure_email_and_sub_id(sub)
                client = by_email.pop(sub.email, None)
                if not client:
                    result["missing"].append({"sub_id": sub.sub_id, "email": sub.email, "host": host.host_id})
                    continue
                xray_ms = client.get("expiryTime", 0) or 0
                expected_ms = XrayAPI._expiry_ms(sub)
                if expected_ms and abs(xray_ms - expected_ms) > 60000:
                    result["mismatch"].append({
                        "sub_id": sub.sub_id,
                        "email": sub.email,
                        "host": host.host_id,
                        "xray": xray_ms,
                        "expected": expected_ms,
                    })
                else:
                    result["ok"].append({"sub_id": sub.sub_id, "email": sub.email, "host": host.host_id})
            result["orphan"].extend([{"email": email, "id": c.get("id"), "host": host.host_id} for email, c in by_email.items()])
        return result

    @staticmethod
    def fix_subscriptions() -> dict[str, Any]:
        """Добавляет/обновляет клиентов в 3X-UI по актуальным подпискам (multi-host)."""
        result = {"added": 0, "updated": 0, "failed": 0, "details": []}
        hosts = [h for h in storage.list_hosts() if h.panel_url]
        if not hosts:
            return result
        for sub in storage.active_subscriptions_all():
            XrayAPI._ensure_email_and_sub_id(sub)
            was_synced = sub.xray_synced
            ok = XrayAPI.add_or_update_client(sub)
            if ok:
                if was_synced:
                    result["updated"] += 1
                else:
                    result["added"] += 1
            else:
                result["failed"] += 1
        storage.save_subscriptions()
        return result


def generate_vless_url(sub: Subscription, server: ServerConfig) -> str:
    """Собирает vless:// ссылку по данным подписки и сервера."""
    return generate_vless_url_raw(
        client_uuid=sub.client_uuid,
        email=sub.email,
        xray_sub_id=sub.xray_sub_id,
        server=server,
    )


def generate_vless_url_raw(client_uuid: str, email: str, xray_sub_id: str, server: ServerConfig) -> str:
    """Собирает vless:// ссылку по сырым параметрам."""
    host = server.address
    sni = server.server_name or host
    remark = quote(f"{server.server_name or 'VPN'}-{email}", safe="")
    params = {
        "type": server.network,
        "security": server.security,
        "pbk": server.public_key,
        "fp": server.fingerprint,
        "sni": sni,
        "sid": server.short_id,
        "spx": server.spider_x,
    }
    if server.flow:
        params["flow"] = server.flow
    query = urlencode(params, safe="", quote_via=quote)
    return f"vless://{client_uuid}@{host}:{server.port}?{query}#{remark}"


def generate_subscription_url(sub: Subscription, server: ServerConfig) -> str:
    """Формирует URL подписки /sub/{subId}."""
    return generate_subscription_url_raw(sub.xray_sub_id, server)


def generate_subscription_url_raw(xray_sub_id: str, server: ServerConfig) -> str:
    """Формирует URL подписки /sub/{subId} по сырому subId."""
    base = server.subscription_url_base or server.address
    panel_url = server.panel_url or ""
    if panel_url.startswith("https://") or server.sub_port == 443:
        scheme = "https"
    else:
        scheme = "http"
    return f"{scheme}://{base}:{server.sub_port}/sub/{xray_sub_id}"


def _generate_qr_code(url: str) -> bytes | None:
    """Генерирует PNG QR-код, если установлен qrcode."""
    try:
        import qrcode
        from PIL import Image
        import io as _io
        qr = qrcode.QRCode(version=1, box_size=10, border=5)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = _io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


# ==================== User Bot ====================


class UserBot:
    """Отдельный Telegram-бот для пользователей VPN."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.bot = telebot.TeleBot(token, parse_mode="HTML", allow_sending_without_reply=True, num_threads=5)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._states: dict[int, dict[str, Any]] = {}
        self._state_lock = threading.RLock()
        self._channel_cache: dict[int, tuple[bool, float]] = {}
        self._crypto_poller: CryptoBotPoller | None = None
        self._scheduler: SubscriptionScheduler | None = None
        self._rates_fetcher: RatesFetcher | None = None
        self._device_auth_server: DeviceAuthServer | None = None
        self._temp_profile_server: TempProfileServer | None = None
        self._register_handlers()

    # ---- state helpers ----
    def set_state(self, user_id: int, state: str, data: dict[str, Any] | None = None) -> None:
        with self._state_lock:
            self._states[user_id] = {"state": state, "data": data or {}}

    def get_state(self, user_id: int) -> dict[str, Any] | None:
        with self._state_lock:
            return self._states.get(user_id)

    def check_state(self, user_id: int, state: str) -> bool:
        s = self.get_state(user_id)
        return s is not None and s.get("state") == state

    def clear_state(self, user_id: int) -> None:
        with self._state_lock:
            self._states.pop(user_id, None)

    def _send_captioned_media(self, chat_id: int, text: str, file_id: str, media_type: str, reply_markup=None) -> None:
        if media_type == "video":
            self.bot.send_video(chat_id, file_id, caption=text, reply_markup=reply_markup, parse_mode="HTML")
        elif media_type == "animation":
            self.bot.send_animation(chat_id, file_id, caption=text, reply_markup=reply_markup, parse_mode="HTML")
        else:
            self.bot.send_photo(chat_id, file_id, caption=text, reply_markup=reply_markup, parse_mode="HTML")

    def _send_menu_message(self, chat_id: int, text: str, reply_markup=None, prefer_welcome_media: bool = False) -> None:
        file_id = ""
        media_type = "photo"
        if prefer_welcome_media:
            file_id = storage.config.get("welcome_media_file_id", "")
            media_type = storage.config.get("welcome_media_type", "photo")
        if not file_id:
            file_id = storage.config.get("menu_media_file_id", "")
            media_type = storage.config.get("menu_media_type", "photo")
        if file_id:
            self._send_captioned_media(chat_id, text, file_id, media_type, reply_markup)
        else:
            self.bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode="HTML")

    # ---- handlers ----
    def _register_handlers(self) -> None:
        bot = self.bot

        @bot.message_handler(commands=["start", "vpn"])
        def cmd_start(m: Message):
            self._cmd_start(m)

        @bot.message_handler(func=lambda m: self.check_state(m.from_user.id, "deposit_crypto_rubles"))
        def on_deposit_crypto_amount(m: Message):
            self._on_deposit_crypto_amount(m)

        @bot.message_handler(func=lambda m: self.check_state(m.from_user.id, "deposit_stars_rubles"))
        def on_deposit_stars_amount(m: Message):
            self._on_deposit_stars_amount(m)

        @bot.message_handler(func=lambda m: self.check_state(m.from_user.id, "withdraw_amount"))
        def on_withdraw_amount(m: Message):
            self._on_withdraw_amount(m)

        @bot.message_handler(func=lambda m: self.check_state(m.from_user.id, "withdraw_card"))
        def on_withdraw_card(m: Message):
            self._on_withdraw_card(m)

        @bot.message_handler(func=lambda m: self.check_state(m.from_user.id, "complaint_text"))
        def on_complaint_text(m: Message):
            self._on_complaint_text(m)

        @bot.message_handler(func=lambda m: self.check_state(m.from_user.id, "add_device_ip"))
        def on_add_device_ip(m: Message):
            self._on_add_device_ip(m)

        @bot.message_handler(func=lambda m: self.check_state(m.from_user.id, "del_device_ip"))
        def on_del_device_ip(m: Message):
            self._on_del_device_ip(m)

        @bot.message_handler(func=lambda m: self.check_state(m.from_user.id, "enter_promo"))
        def on_promo_code(m: Message):
            self._on_promo_code(m)

        @bot.message_handler(func=lambda m: self.check_state(m.from_user.id, "enter_activation_code"))
        def on_activation_code(m: Message):
            self._on_activation_code(m)

        @bot.message_handler(func=lambda m: self.check_state(m.from_user.id, "enter_freeze_days"))
        def on_freeze_days(m: Message):
            self._on_freeze_days(m)

        @bot.pre_checkout_query_handler(func=lambda q: True)
        def pre_checkout(query):
            bot.answer_pre_checkout_query(query.id, ok=True)

        @bot.message_handler(content_types=["successful_payment"])
        def on_payment(m: Message):
            self._on_successful_payment(m)

        @bot.callback_query_handler(func=lambda c: c.data.startswith(CB_PREFIX))
        def cbq_router(c: CallbackQuery):
            try:
                self._handle_callback(c)
            except Exception:
                logger.exception("Ошибка обработки callback user-бота")
            finally:
                try:
                    bot.answer_callback_query(c.id)
                except Exception:
                    pass

    # ---- validation helpers ----
    @staticmethod
    def _is_valid_ip(text: str) -> bool:
        text = text.strip()
        try:
            ipaddress.ip_address(text)
            return True
        except ValueError:
            return False

    @staticmethod
    def _is_valid_amount(text: str) -> Decimal | None:
        text = text.strip().replace(",", ".")
        try:
            value = _to_dec(text)
            return value if value > 0 else None
        except (ValueError, InvalidOperation):
            return None

    @staticmethod
    def _is_valid_user_id(text: str) -> int | None:
        text = text.strip()
        if text.startswith("@"):
            return None
        try:
            uid = int(text)
            return uid if uid > 0 else None
        except ValueError:
            return None

    # ---- commands ----
    def _cmd_start(self, m: Message) -> None:
        user_id = m.from_user.id
        if self._check_maintenance(user_id, m.chat.id):
            return
        username = m.from_user.username or ""
        source, referred_by = self._parse_start_param(m, user_id)
        is_new = str(user_id) not in storage.users
        user = storage.get_user(user_id, username, source=source)
        if referred_by and not user.get("referred_by"):
            try:
                ref_id = int(referred_by)
                if ref_id != user_id and storage.get_user(ref_id):
                    user["referred_by"] = ref_id
            except (ValueError, TypeError):
                pass
        if username and not user.get("username"):
            user["username"] = username
        storage.update_user(user)
        if is_new:
            storage._notify_admins(f"Новый пользователь: {user_id} (@{username}) из источника {source}.", "new_user")
        if not self._channel_check(user_id, m.chat.id):
            return
        self._send_welcome(user_id, m.chat.id)

    def _parse_start_param(self, m: Message, user_id: int) -> tuple[str, int | None]:
        """Возвращает (source, referred_by). source='direct' если без параметра."""
        parts = m.text.split() if m.text else []
        if len(parts) < 2:
            return "direct", None
        param = parts[1].strip()
        if param.startswith("ref_"):
            try:
                return "referral", int(param.split("_", 1)[1])
            except (ValueError, TypeError):
                return "referral", None
        return param, None

    def _send_welcome(self, user_id: int, chat_id: int) -> None:
        """Отправляет приветственное медиа и главное/пробное меню."""
        user = storage.get_user(user_id)
        text = storage.config.get("welcome", "Добро пожаловать!")
        if not user.get("trial_used"):
            kb = K()
            kb.add(
                B("🎁 Активировать пробный период", callback_data=f"{CB_PREFIX}trial", style="success"),
                B("🏠 Главное меню", callback_data=f"{CB_PREFIX}main", style="primary"),
            )
            caption = f"{text}\n\nУ вас есть {storage.config.get('trial_days', TRIAL_DAYS)}-дневный пробный период."
            self._send_menu_message(chat_id, caption, reply_markup=kb, prefer_welcome_media=True)
        else:
            self._send_menu_message(chat_id, f"{text}\n\nВыберите раздел:", reply_markup=self._keyboard_main(), prefer_welcome_media=True)

    def _check_maintenance(self, user_id: int, chat_id: int) -> bool:
        if storage.is_banned(user_id):
            self.bot.send_message(chat_id, "Вы заблокированы и не можете использовать этого бота.")
            return True
        if not storage.maintenance_mode() or storage.is_admin_user(user_id):
            return False
        self.bot.send_message(chat_id, "Бот временно на обслуживании. Попробуйте позже.")
        return True

    def _send_welcome_media(self, chat_id: int) -> None:
        file_id = storage.config.get("welcome_media_file_id")
        media_type = storage.config.get("welcome_media_type", "photo")
        if not file_id:
            return
        try:
            if media_type == "video":
                self.bot.send_video(chat_id, file_id)
            elif media_type == "animation":
                self.bot.send_animation(chat_id, file_id)
            else:
                self.bot.send_photo(chat_id, file_id)
        except Exception:
            logger.exception("Не удалось отправить приветственное медиа")

    def _channel_check(self, user_id: int, chat_id: int, force: bool = False, message_id: int | None = None) -> bool:
        channel_id = storage.config.get("channel_id", "")
        if not channel_id:
            return True
        now = time.time()
        with self._state_lock:
            if force:
                self._channel_cache.pop(user_id, None)
            cached = self._channel_cache.get(user_id)
            if cached and (now - cached[1]) < 60:
                if cached[0]:
                    self._on_channel_ok(user_id, chat_id, message_id)
                else:
                    self._prompt_channel(user_id, chat_id, message_id)
                return cached[0]
        try:
            member = self.bot.get_chat_member(channel_id, user_id)
            ok = member.status in ("member", "administrator", "creator")
            with self._state_lock:
                self._channel_cache[user_id] = (ok, now)
            if ok:
                self._on_channel_ok(user_id, chat_id, message_id)
            else:
                self._prompt_channel(user_id, chat_id, message_id)
            return ok
        except Exception:
            logger.exception("Ошибка проверки подписки на канал")
        with self._state_lock:
            self._channel_cache[user_id] = (False, now)
        self._prompt_channel(user_id, chat_id, message_id)
        return False

    def _channel_keyboard(self, channel_id: str) -> K:
        kb = K(row_width=1)
        invite = self._get_channel_invite(channel_id)
        if invite:
            kb.add(B("👉 Перейти в канал", url=invite, style="success"))
        else:
            support = storage.config.get("support", "")
            if support and isinstance(support, str):
                support_user = support.lstrip("@")
                kb.add(B("🆘 Написать админу", url=f"https://t.me/{support_user}", style="danger"))
        kb.add(B("🔄 Проверить подписку", callback_data=f"{CB_PREFIX}check_channel", style="primary"))
        return kb

    def _prompt_channel(self, user_id: int, chat_id: int, message_id: int | None = None) -> None:
        channel_id = storage.config.get("channel_id", "")
        channel_text = f"<b>{_escape(channel_id)}</b>" if channel_id else "наш канал"
        text = f"<b>📢 Для использования бота необходимо подписаться на {channel_text}.</b>"
        kb = self._channel_keyboard(channel_id)
        if message_id is not None:
            try:
                self.bot.delete_message(chat_id, message_id)
            except Exception:
                pass
        self._send_menu_message(chat_id, text, reply_markup=kb, prefer_welcome_media=False)

    def _on_channel_ok(self, user_id: int, chat_id: int, message_id: int | None = None) -> None:
        user = storage.get_user(user_id)
        user["channel_ok"] = True
        storage.update_user(user)
        if message_id is not None:
            try:
                self.bot.delete_message(chat_id, message_id)
            except Exception:
                pass
        self._send_welcome(user_id, chat_id)

    def _get_channel_invite(self, channel_id: str) -> str | None:
        """Пытается получить рабочую ссылку-приглашение на канал."""
        if not channel_id:
            return None
        # Если админ сразу ввёл ссылку
        if isinstance(channel_id, str) and channel_id.startswith("https://"):
            return channel_id
        # Пробуем получить из get_chat
        try:
            chat = self.bot.get_chat(channel_id)
            if chat.invite_link:
                return chat.invite_link
            if chat.username:
                return f"https://t.me/{chat.username}"
        except Exception:
            pass
        # Пробуем экспортировать/создать invite-ссылку (бот должен быть админом)
        try:
            return self.bot.export_chat_invite_link(channel_id)
        except Exception:
            pass
        try:
            link = self.bot.create_chat_invite_link(channel_id)
            if link and link.invite_link:
                return link.invite_link
        except Exception:
            pass
        # Публичный канал по @username
        if isinstance(channel_id, str) and channel_id.startswith("@"):
            return f"https://t.me/{channel_id.lstrip('@')}"
        return None

    def _keyboard_main(self) -> K:
        kb = K()
        kb.row_width = 2
        kb.add(
            B("🧑 Профиль", callback_data=f"{CB_PREFIX}profile", style="success"),
            B("🛒 Купить подписку", callback_data=f"{CB_PREFIX}buy", style="success"),
            B("📱 Мои подписки", callback_data=f"{CB_PREFIX}my_subs", style="danger"),
            B("💰 Пополнить баланс", callback_data=f"{CB_PREFIX}deposit", style="danger"),
            B("🎁 Активировать код", callback_data=f"{CB_PREFIX}activate_code", style="success"),
            B("👥 Реферальная система", callback_data=f"{CB_PREFIX}referral", style="success"),
            B("❓ Помощь", callback_data=f"{CB_PREFIX}help", style="success"),
        )
        return kb

    # ---- callbacks ----
    def _handle_callback(self, c: CallbackQuery) -> None:
        user_id = c.from_user.id
        chat_id = c.message.chat.id
        data = c.data[len(CB_PREFIX):]
        parts = data.split(":")
        action = parts[0]
        args = parts[1:]

        if self._check_maintenance(user_id, chat_id):
            return
        if not self._channel_check(user_id, chat_id, message_id=c.message.message_id):
            return

        if action == "main":
            self.bot.edit_message_text("Главное меню:", chat_id, c.message.message_id, reply_markup=self._keyboard_main())
            return

        if action == "profile":
            self._profile_menu(user_id, chat_id, c.message.message_id)
            return

        if action == "help":
            self._help_menu(user_id, chat_id, c.message.message_id)
            return

        if action == "support":
            self._support_menu(user_id, chat_id, c.message.message_id)
            return

        if action == "complaint":
            self.set_state(user_id, "complaint_text", {})
            self.bot.edit_message_text("Опишите вашу жалобу/проблему:", chat_id, c.message.message_id,
                                       reply_markup=K().add(B("Отмена", callback_data=f"{CB_PREFIX}help")))
            return

        if action == "faq":
            self._faq_menu(user_id, chat_id, c.message.message_id)
            return

        if action == "history":
            self._history_menu(user_id, chat_id, c.message.message_id)
            return

        if action == "settings":
            self._settings_menu(user_id, chat_id, c.message.message_id)
            return

        if action == "toggle" and args:
            storage.toggle_user_setting(user_id, args[0])
            self._settings_menu(user_id, chat_id, c.message.message_id)
            return

        if action == "check_channel":
            if self._channel_check(user_id, chat_id, force=True, message_id=c.message.message_id):
                self.bot.answer_callback_query(c.id, "✅ Подписка подтверждена!", show_alert=True)
            else:
                self.bot.answer_callback_query(c.id, "❌ Подписка не найдена. Нажмите кнопку перехода и попробуйте снова.", show_alert=True)
            return

        if action == "trial":
            self._activate_trial(user_id, chat_id, c.message.message_id)
            return

        if action == "buy":
            self._buy_menu(chat_id, c.message.message_id)
            return

        if action == "host" and args:
            self._plan_menu(chat_id, c.message.message_id, args[0])
            return

        if action == "plan":
            plan_id = args[0]
            self._duration_menu(chat_id, c.message.message_id, plan_id)
            return

        if action == "duration":
            plan_id, months = args[0], args[1]
            self._confirm_purchase(user_id, chat_id, c.message.message_id, plan_id, months)
            return

        if action == "purchase":
            plan_id, months = args[0], int(args[1])
            self._purchase(user_id, chat_id, c.message.message_id, plan_id, months)
            return

        if action == "promo" and len(args) >= 2:
            plan_id, months = args[0], args[1]
            state_data = {"plan_id": plan_id, "months": months}
            state = self.get_state(user_id)
            if state and state.get("data", {}).get("message_id"):
                state_data["message_id"] = state["data"]["message_id"]
            self.set_state(user_id, "enter_promo", state_data)
            self.bot.edit_message_text("Отправьте промокод:", chat_id, c.message.message_id,
                                       reply_markup=K().add(B("Назад", callback_data=f"{CB_PREFIX}duration:{plan_id}:{months}")))
            return

        if action == "activate_code":
            self.set_state(user_id, "enter_activation_code", {})
            self.bot.edit_message_text("Отправьте код активации (подарочный или партнёрский):", chat_id, c.message.message_id,
                                       reply_markup=K().add(B("Отмена", callback_data=f"{CB_PREFIX}main")))
            return

        if action == "my_subs":
            self._my_subscriptions(user_id, chat_id, c.message.message_id)
            return

        if action == "sub_detail":
            sub_id = args[0]
            self._sub_detail(user_id, chat_id, c.message.message_id, sub_id)
            return

        if action == "sub_connect":
            sub_id = args[0]
            self._connect(user_id, chat_id, c.message.message_id, sub_id)
            return

        if action == "sub_stats":
            sub_id = args[0]
            self._sub_stats(user_id, chat_id, c.message.message_id, sub_id)
            return

        if action == "sub_renew":
            sub_id = args[0]
            self._renew_menu(user_id, chat_id, c.message.message_id, sub_id)
            return

        if action == "sub_freeze":
            sub_id = args[0]
            self.set_state(user_id, "enter_freeze_days", {"sub_id": sub_id})
            self.bot.edit_message_text("Отправьте количество дней заморозки:", chat_id, c.message.message_id,
                                       reply_markup=K().add(B("Отмена", callback_data=f"{CB_PREFIX}sub_detail:{sub_id}")))
            return

        if action == "renew_confirm":
            sub_id, months = args[0], int(args[1])
            self._renew(user_id, chat_id, c.message.message_id, sub_id, months)
            return

        if action == "sub_devices":
            sub_id = args[0]
            self._sub_devices(user_id, chat_id, c.message.message_id, sub_id)
            return

        if action == "add_device":
            sub_id = args[0]
            self.set_state(user_id, "add_device_ip", {"sub_id": sub_id})
            self.bot.edit_message_text("Отправьте IP-адрес устройства (например, 1.2.3.4):", chat_id, c.message.message_id,
                                       reply_markup=K().add(B("Отмена", callback_data=f"{CB_PREFIX}sub_detail:{sub_id}")))
            return

        if action == "del_device":
            sub_id = args[0]
            self.set_state(user_id, "del_device_ip", {"sub_id": sub_id})
            self.bot.edit_message_text("Отправьте IP-адрес устройства для удаления:", chat_id, c.message.message_id,
                                       reply_markup=K().add(B("Отмена", callback_data=f"{CB_PREFIX}sub_detail:{sub_id}")))
            return

        if action == "del_all_devices":
            sub_id = args[0]
            sub = storage.get_subscription(sub_id)
            if sub and sub.user_id == user_id:
                sub.devices = []
                storage.update_subscription(sub)
                self.bot.answer_callback_query(c.id, "Все устройства удалены.")
                self._sub_devices(user_id, chat_id, c.message.message_id, sub_id)
            return

        if action == "unbind_device" and len(args) >= 2:
            sub_id, idx = args[0], args[1]
            try:
                index = int(idx)
            except ValueError:
                return
            ok, text = storage.unbind_device(sub_id, index, user_id)
            self.bot.answer_callback_query(c.id, text)
            if ok:
                self._sub_devices(user_id, chat_id, c.message.message_id, sub_id)
            return

        if action == "deposit":
            self._deposit_menu(chat_id, c.message.message_id)
            return

        if action == "deposit_crypto":
            asset = args[0] if args else "USDT"
            rate = RatesFetcher().get_rate(asset)
            if not rate:
                self.bot.edit_message_text(f"Курс для {asset} ещё не загружен. Попробуйте через минуту.", chat_id, c.message.message_id,
                                           reply_markup=K().add(B("Назад", callback_data=f"{CB_PREFIX}deposit")))
                return
            self.set_state(user_id, "deposit_crypto_rubles", {"asset": asset, "message_id": c.message.message_id})
            self.bot.edit_message_text(f"Выберите сумму пополнения в рублях ({asset}):", chat_id, c.message.message_id,
                                       reply_markup=self._amount_keyboard(f"{CB_PREFIX}deposit_crypto", f"{CB_PREFIX}deposit", asset))
            return

        if action == "deposit_stars":
            self.set_state(user_id, "deposit_stars_rubles", {"message_id": c.message.message_id})
            self.bot.edit_message_text("Выберите сумму пополнения в рублях (Telegram Stars):", chat_id, c.message.message_id,
                                       reply_markup=self._amount_keyboard(f"{CB_PREFIX}deposit_stars", f"{CB_PREFIX}deposit"))
            return

        if action == "deposit_crypto_preset" and len(args) >= 2:
            asset, rub_str = args[0], args[1]
            if rub_str == "custom":
                self.set_state(user_id, "deposit_crypto_rubles", {"asset": asset, "message_id": c.message.message_id})
                self.bot.edit_message_text("Введите сумму пополнения в рублях:", chat_id, c.message.message_id,
                                           reply_markup=K().add(B("Отмена", callback_data=f"{CB_PREFIX}deposit")))
                return
            try:
                rub = int(rub_str)
            except ValueError:
                return
            self._create_crypto_invoice(user_id, chat_id, c.message.message_id, asset, rub)
            return

        if action == "deposit_stars_preset" and len(args) >= 1:
            rub_str = args[0]
            if rub_str == "custom":
                self.set_state(user_id, "deposit_stars_rubles", {"message_id": c.message.message_id})
                self.bot.edit_message_text("Введите сумму пополнения в рублях:", chat_id, c.message.message_id,
                                           reply_markup=K().add(B("Отмена", callback_data=f"{CB_PREFIX}deposit")))
                return
            try:
                rub = int(rub_str)
            except ValueError:
                return
            self._create_stars_invoice(user_id, chat_id, c.message.message_id, rub)
            return

        if action == "check_crypto":
            self.bot.edit_message_text("Оплатите счёт выше, затем нажмите «Проверить оплату».\n"
                                       "Если средства поступили, баланс обновится в течение минуты.",
                                       chat_id, c.message.message_id,
                                       reply_markup=K().add(B("Проверить оплату", callback_data=f"{CB_PREFIX}deposit"),
                                                          B("Главное меню", callback_data=f"{CB_PREFIX}main")))
            return

        if action == "referral":
            self._referral_menu(user_id, chat_id, c.message.message_id)
            return

        if action == "withdraw":
            user = storage.get_user(user_id)
            ref_balance = _to_dec(user.get("referral_balance", 0))
            if ref_balance < 3000:
                self.bot.edit_message_text(f"Минимальная сумма вывода 3000₽. Ваш реферальный баланс: {_money_str(ref_balance)}₽.",
                                           chat_id, c.message.message_id, reply_markup=self._keyboard_main())
                return
            self.set_state(user_id, "withdraw_amount", {"message_id": c.message.message_id})
            self.bot.edit_message_text(f"Введите сумму вывода (доступно {_money_str(ref_balance)}₽, минимум 3000₽):",
                                       chat_id, c.message.message_id,
                                       reply_markup=K().add(B("Отмена", callback_data=f"{CB_PREFIX}referral")))
            return

    # ---- actions ----
    def _activate_trial(self, user_id: int, chat_id: int, message_id: int) -> None:
        user = storage.get_user(user_id)
        if user.get("trial_used"):
            self.bot.edit_message_text("Пробный период уже использован.", chat_id, message_id,
                                       reply_markup=self._keyboard_main())
            return
        sub = storage.create_subscription(user_id, "trial", 0, is_trial=True)
        user["trial_used"] = True
        storage.update_user(user)
        storage._add_transaction(user_id, 0, "trial", "trial", f"sub_{sub.sub_id}")
        XrayAPI.add_or_update_client(sub)
        self.bot.edit_message_text(f"Пробный период активирован!\n{format_subscription(sub)}", chat_id, message_id,
                                   reply_markup=self._keyboard_main())

    def _buy_menu(self, chat_id: int, message_id: int) -> None:
        hosts = storage.list_hosts()
        if len(hosts) > 1:
            kb = K()
            for host in hosts:
                text = host.name or host.host_id
                if host.host_id == storage.default_host_id():
                    text = f"{text} (по умолч.)"
                kb.add(B(text, callback_data=f"{CB_PREFIX}host:{host.host_id}"))
            kb.add(B("Назад", callback_data=f"{CB_PREFIX}main"))
            self.bot.edit_message_text("Выберите сервер:", chat_id, message_id, reply_markup=kb)
        else:
            self._plan_menu(chat_id, message_id)

    def _plan_menu(self, chat_id: int, message_id: int, host_id: str | None = None) -> None:
        kb = K()
        for pid, plan in storage.plans(host_id).items():
            if pid == "trial":
                continue
            kb.add(B(plan.name, callback_data=f"{CB_PREFIX}plan:{pid}"))
        kb.add(B("Назад", callback_data=f"{CB_PREFIX}main"))
        self.bot.edit_message_text("Выберите тариф:", chat_id, message_id, reply_markup=kb)

    def _duration_menu(self, chat_id: int, message_id: int, plan_id: str) -> None:
        plan = storage.plan(plan_id)
        kb = K()
        for months in DURATIONS:
            if plan.prices.get(months) is not None:
                kb.add(B(f"{months} мес. — {_price_text(plan, months)}", callback_data=f"{CB_PREFIX}duration:{plan_id}:{months}"))
        kb.add(B("Назад", callback_data=f"{CB_PREFIX}buy"))
        self.bot.edit_message_text(f"Тариф: <b>{_escape(plan.name)}</b>\nУстройств: {plan.device_text}\n\nВыберите срок:",
                                   chat_id, message_id, reply_markup=kb)

    def _confirm_purchase(self, user_id: int, chat_id: int, message_id: int, plan_id: str, months: str, host_id: str | None = None) -> None:
        plan = storage.plan(plan_id)
        base_price = storage.price(plan_id, int(months))
        if base_price is None:
            self.bot.edit_message_text("Цена для выбранного срока не установлена.", chat_id, message_id,
                                       reply_markup=self._keyboard_main())
            return
        state = self.get_state(user_id)
        discount = Decimal("0.00")
        if state and state.get("state") in ("confirm_purchase", "enter_promo") and state.get("data", {}).get("plan_id") == plan_id and str(state.get("data", {}).get("months")) == str(months):
            discount = _to_dec(state["data"].get("discount", 0))
            message_id = message_id or state["data"].get("message_id")
        # apply referral first-purchase discount once
        ref_discount = storage.apply_referral_discount(user_id, base_price or Decimal("0.00")) if not discount else Decimal("0.00")
        if ref_discount and ref_discount < (base_price or Decimal("0.00")):
            discount = _money_round((base_price or Decimal("0.00")) - ref_discount)
        price = _money_round((base_price or Decimal("0.00")) - discount)
        user = storage.get_user(user_id)
        host = storage.get_host(plan.host_id) if plan and plan.host_id else storage.server()
        host_text = f"Сервер: {host.name}\n" if host and host.name and len(storage.list_hosts()) > 1 else ""
        price_text = f"<s>{_money_str(base_price)}₽</s> {_money_str(price)}₽ (скидка {_money_str(discount)}₽)" if discount else f"{_money_str(price)}₽"
        text = (f"<b>{_escape(plan.name)}</b>\n"
                f"{host_text}"
                f"Срок: {months} мес.\n"
                f"Цена: {price_text}\n"
                f"Ваш баланс: {_money_str(user['balance'])}₽\n\n"
                f"Подтвердите покупку:")
        kb = K()
        kb.add(B("Купить", callback_data=f"{CB_PREFIX}purchase:{plan_id}:{months}"))
        if not discount:
            kb.add(B("Применить промокод", callback_data=f"{CB_PREFIX}promo:{plan_id}:{months}"))
        if user["balance"] < price:
            kb.add(B("Пополнить баланс", callback_data=f"{CB_PREFIX}deposit"))
        kb.add(B("Назад", callback_data=f"{CB_PREFIX}plan:{plan_id}"))
        self.set_state(user_id, "confirm_purchase", {"plan_id": plan_id, "months": months, "discount": discount, "message_id": message_id, "host_id": plan.host_id if plan else host_id})
        if message_id:
            self.bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)
        else:
            self.bot.send_message(chat_id, text, reply_markup=kb)

    def _purchase(self, user_id: int, chat_id: int, message_id: int, plan_id: str, months: int) -> None:
        base_price = storage.price(plan_id, months)
        if base_price is None:
            self.bot.edit_message_text("Ошибка: тариф не найден.", chat_id, message_id, reply_markup=self._keyboard_main())
            return
        state = self.get_state(user_id)
        discount = Decimal("0.00")
        promo_code = None
        if state and state.get("state") == "confirm_purchase" and state.get("data", {}).get("plan_id") == plan_id and str(state.get("data", {}).get("months")) == str(months):
            discount = _to_dec(state["data"].get("discount", 0))
            promo_code = state["data"].get("promo_code")
        # referral first-purchase discount
        ref_discount = storage.apply_referral_discount(user_id, base_price) if not discount else Decimal("0.00")
        if ref_discount and ref_discount < base_price:
            discount = _money_round(base_price - ref_discount)
        price = _money_round(base_price - discount)
        user = storage.get_user(user_id)
        if user["balance"] < price:
            self.bot.edit_message_text("Недостаточно средств. Пополните баланс.", chat_id, message_id,
                                       reply_markup=K().add(B("Пополнить", callback_data=f"{CB_PREFIX}deposit"),
                                                          B("Назад", callback_data=f"{CB_PREFIX}main")))
            return
        if promo_code:
            storage.use_promocode(promo_code)
        deducted = storage.deduct_balance(user_id, price, "purchase", f"plan:{plan_id}:{months}" + (f" promo:{promo_code}" if promo_code else ""))
        if not deducted:
            self.bot.edit_message_text("Ошибка списания средств. Попробуйте ещё раз.", chat_id, message_id,
                                       reply_markup=self._keyboard_main())
            return
        sub = storage.create_subscription(user_id, plan_id, months)
        if not promo_code and ref_discount and ref_discount < base_price:
            storage.mark_first_purchase_discount_used(user_id)
        storage.process_referral_rewards(user_id, price)
        storage.add_purchase_stats(user_id, price, months)
        XrayAPI.add_or_update_client(sub)
        self.clear_state(user_id)
        self.bot.edit_message_text(f"Подписка оформлена!\n{format_subscription(sub)}", chat_id, message_id,
                                   reply_markup=self._keyboard_main())

    def _my_subscriptions(self, user_id: int, chat_id: int, message_id: int) -> None:
        subs = storage.active_subscriptions(user_id)
        if not subs:
            kb = K().add(B("Купить подписку", callback_data=f"{CB_PREFIX}buy")).add(B("Назад", callback_data=f"{CB_PREFIX}main"))
            self.bot.edit_message_text("У вас нет активных подписок.", chat_id, message_id, reply_markup=kb)
            return
        kb = K()
        for sub in subs:
            plan = storage.plan(sub.plan_id)
            kb.add(B(f"#{sub.sub_id} — {plan.name if plan else sub.plan_id}", callback_data=f"{CB_PREFIX}sub_detail:{sub.sub_id}"))
        kb.add(B("Назад", callback_data=f"{CB_PREFIX}main"))
        self.bot.edit_message_text("<b>Мои подписки</b>\n\nВыберите подписку:", chat_id, message_id, reply_markup=kb)

    def _sub_detail(self, user_id: int, chat_id: int, message_id: int, sub_id: str) -> None:
        sub = storage.get_subscription(sub_id)
        if not sub or sub.user_id != user_id:
            self.bot.edit_message_text("Подписка не найдена.", chat_id, message_id, reply_markup=self._keyboard_main())
            return
        kb = K()
        kb.add(B("Подключить", callback_data=f"{CB_PREFIX}sub_connect:{sub_id}"))
        kb.add(B("Статистика", callback_data=f"{CB_PREFIX}sub_stats:{sub_id}"))
        kb.add(B("Продлить", callback_data=f"{CB_PREFIX}sub_renew:{sub_id}"))
        kb.add(B("Устройства", callback_data=f"{CB_PREFIX}sub_devices:{sub_id}"))
        kb.add(B("Заморозка", callback_data=f"{CB_PREFIX}sub_freeze:{sub_id}"))
        kb.add(B("Назад", callback_data=f"{CB_PREFIX}my_subs"))
        self.bot.edit_message_text(format_subscription(sub), chat_id, message_id, reply_markup=kb)

    def _connect(self, user_id: int, chat_id: int, message_id: int, sub_id: str) -> None:
        sub = storage.get_subscription(sub_id)
        if not sub or sub.user_id != user_id:
            return
        if not sub.active or sub.is_expired:
            self.bot.edit_message_text("Подписка неактивна или истекла.", chat_id, message_id,
                                       reply_markup=self._keyboard_main())
            return
        server = storage.get_host(sub.host_id) or storage.server()
        if not server or not server.address or not server.public_key or not server.short_id:
            self.bot.edit_message_text("Сервер VPN не настроен. Обратитесь к администратору.", chat_id, message_id,
                                       reply_markup=self._keyboard_main())
            return
        # синхронизируем с 3X-UI, если настроена панель
        if server.panel_url and not sub.xray_synced:
            XrayAPI.add_or_update_client(sub)
            sub = storage.get_subscription(sub_id)
        vless_url = generate_vless_url(sub, server)
        sub_url = generate_subscription_url(sub, server)
        text = (
            f"<b>Подключение к VPN</b>\n\n"
            f"1. Установите приложение (v2rayNG, V2Box, Streisand и т.п.)\n"
            f"2. Отсканируйте QR-код или нажмите ссылку/подписку.\n\n"
            f"<b>VLESS ссылка:</b>\n<code>{_escape(vless_url)}</code>\n\n"
            f"<b>Подписка:</b>\n<code>{_escape(sub_url)}</code>"
        )
        qr = _generate_qr_code(vless_url)
        kb = K().add(B("Назад", callback_data=f"{CB_PREFIX}sub_detail:{sub_id}"))
        try:
            if qr:
                self.bot.send_photo(chat_id, qr, caption=text, reply_markup=kb)
            else:
                self.bot.send_message(chat_id, text, reply_markup=kb)
        except Exception:
            self.bot.send_message(chat_id, text, reply_markup=kb)

    def _sub_stats(self, user_id: int, chat_id: int, message_id: int, sub_id: str) -> None:
        sub = storage.get_subscription(sub_id)
        if not sub or sub.user_id != user_id:
            return
        stats = XrayAPI.get_client_stats(sub)
        upload = stats.get("upload", 0) / (1024 * 1024)
        download = stats.get("download", 0) / (1024 * 1024)
        text = (
            f"<b>Статистика #{sub.sub_id}</b>\n\n"
            f"🔼 Upload: {upload:.2f} MB\n"
            f"🔽 Download: {download:.2f} MB"
        )
        kb = K().add(B("Назад", callback_data=f"{CB_PREFIX}sub_detail:{sub_id}"))
        self.bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)

    def _renew_menu(self, user_id: int, chat_id: int, message_id: int, sub_id: str) -> None:
        sub = storage.get_subscription(sub_id)
        if not sub or sub.user_id != user_id:
            return
        plan = storage.plan(sub.plan_id)
        kb = K()
        for months in DURATIONS:
            if plan.prices.get(months) is not None:
                kb.add(B(f"+{months} мес. — {_price_text(plan, months)}", callback_data=f"{CB_PREFIX}renew_confirm:{sub_id}:{months}"))
        kb.add(B("Назад", callback_data=f"{CB_PREFIX}sub_detail:{sub_id}"))
        self.bot.edit_message_text("Выберите срок продления:", chat_id, message_id, reply_markup=kb)

    def _renew(self, user_id: int, chat_id: int, message_id: int, sub_id: str, months: int) -> None:
        sub = storage.get_subscription(sub_id)
        plan = storage.plan(sub.plan_id)
        if not sub or sub.user_id != user_id or not plan:
            return
        price = storage.price(sub.plan_id, months)
        if price is None:
            self.bot.edit_message_text("Цена для выбранного срока не установлена.", chat_id, message_id,
                                       reply_markup=self._keyboard_main())
            return
        user = storage.get_user(user_id)
        if user["balance"] < price:
            self.bot.edit_message_text("Недостаточно средств.", chat_id, message_id,
                                       reply_markup=K().add(B("Пополнить", callback_data=f"{CB_PREFIX}deposit")))
            return
        if not storage.deduct_balance(user_id, price, "renew", f"sub:{sub_id}:{months}"):
            self.bot.edit_message_text("Ошибка списания средств. Попробуйте ещё раз.", chat_id, message_id,
                                       reply_markup=self._keyboard_main())
            return
        now = time.time()
        base = max(now, sub.expires_at)
        sub.expires_at = base + months * 30 * 86400
        storage.update_subscription(sub)
        XrayAPI.update_expiry(sub)
        storage.process_referral_rewards(user_id, price)
        storage.add_purchase_stats(user_id, price, months)
        self.bot.edit_message_text(f"Подписка продлена!\n{format_subscription(sub)}", chat_id, message_id,
                                   reply_markup=self._keyboard_main())

    def _sub_devices(self, user_id: int, chat_id: int, message_id: int, sub_id: str) -> None:
        sub = storage.get_subscription(sub_id)
        if not sub or sub.user_id != user_id:
            return
        plan = storage.plan(sub.plan_id)
        lines = [f"<b>Устройства</b> ({len(sub.devices)} / {plan.device_text if plan else '?'})\n\nОтвязать устройство можно раз в {storage.security().get('unbind_cooldown', DEFAULT_UNBIND_COOLDOWN) // 86400} дней."]
        for i, d in enumerate(sub.devices, 1):
            ip = _escape(d.get('ip', ''))
            ua = _escape(d.get('user_agent', ''))
            seen = d.get('last_seen') or d.get('first_seen')
            ua_text = f" — {ua[:20]}" if ua else ""
            lines.append(f"{i}. {ip}{ua_text} (последнее: {datetime.fromtimestamp(seen).strftime('%d.%m.%Y %H:%M')})")
        if not sub.devices:
            lines.append("Устройств пока нет.")
        kb = K()
        for idx, d in enumerate(sub.devices):
            ip_label = _escape(d.get('ip', '')[:15])
            kb.add(B(f"Отвязать {ip_label}", callback_data=f"{CB_PREFIX}unbind_device:{sub_id}:{idx}"))
        kb.add(B("Добавить", callback_data=f"{CB_PREFIX}add_device:{sub_id}"))
        kb.add(B("Удалить все", callback_data=f"{CB_PREFIX}del_all_devices:{sub_id}"))
        kb.add(B("Назад", callback_data=f"{CB_PREFIX}sub_detail:{sub_id}"))
        self.bot.edit_message_text("\n".join(lines), chat_id, message_id, reply_markup=kb)

    def _on_add_device_ip(self, m: Message) -> None:
        user_id = m.from_user.id
        if self._check_maintenance(user_id, m.chat.id):
            return
        state = self.get_state(user_id)
        if not state:
            return
        sub_id = state["data"]["sub_id"]
        ip = m.text.strip()
        if not self._is_valid_ip(ip):
            self.bot.send_message(m.chat.id, "Введите корректный IP-адрес (например, 1.2.3.4).",
                                  reply_markup=self._keyboard_main())
            return
        self.clear_state(user_id)
        sub = storage.get_subscription(sub_id)
        if not sub or sub.user_id != user_id:
            self.bot.send_message(m.chat.id, "Подписка не найдена.", reply_markup=self._keyboard_main())
            return
        plan = storage.plan(sub.plan_id)
        if plan and plan.max_devices != -1 and len(sub.devices) >= plan.max_devices:
            self.bot.send_message(m.chat.id, f"Лимит устройств ({plan.device_text}) достигнут.",
                                  reply_markup=self._keyboard_main())
            return
        if any(d["ip"] == ip for d in sub.devices):
            self.bot.send_message(m.chat.id, "Это устройство уже добавлено.", reply_markup=self._keyboard_main())
            return
        sub.devices.append({"ip": ip, "first_seen": time.time(), "last_seen": time.time()})
        storage.update_subscription(sub)
        self.bot.send_message(m.chat.id, f"Устройство {ip} добавлено.", reply_markup=self._keyboard_main())

    def _on_del_device_ip(self, m: Message) -> None:
        user_id = m.from_user.id
        if self._check_maintenance(user_id, m.chat.id):
            return
        state = self.get_state(user_id)
        if not state:
            return
        sub_id = state["data"]["sub_id"]
        ip = m.text.strip()
        if not self._is_valid_ip(ip):
            self.bot.send_message(m.chat.id, "Введите корректный IP-адрес (например, 1.2.3.4).",
                                  reply_markup=self._keyboard_main())
            return
        self.clear_state(user_id)
        sub = storage.get_subscription(sub_id)
        if not sub or sub.user_id != user_id:
            self.bot.send_message(m.chat.id, "Подписка не найдена.", reply_markup=self._keyboard_main())
            return
        before = len(sub.devices)
        sub.devices = [d for d in sub.devices if d["ip"] != ip]
        storage.update_subscription(sub)
        if len(sub.devices) < before:
            self.bot.send_message(m.chat.id, f"Устройство {ip} удалено.", reply_markup=self._keyboard_main())
        else:
            self.bot.send_message(m.chat.id, f"Устройство {ip} не найдено.", reply_markup=self._keyboard_main())

    # ---- deposit ----
    def _deposit_menu(self, chat_id: int, message_id: int) -> None:
        user = storage.get_user(chat_id)  # chat_id == user_id in private
        rates = storage.config.get("rates", {})
        usd = rates.get("USD") or "н/д"
        ton = rates.get("TON") or "н/д"
        usd_text = _money_str(usd) if isinstance(usd, Decimal) else str(usd)
        ton_text = _money_str(ton) if isinstance(ton, Decimal) else str(ton)
        text = (f"<b>💳 Пополнение баланса</b>\n\n"
                f"💰 Текущий баланс: {_money_str(user['balance'])}₽\n"
                f"📈 Курс: USD {usd_text}₽, TON {ton_text}₽\n"
                f"⭐ Telegram Stars: 1.3 Stars = 1₽\n\n"
                f"Выберите способ:")
        kb = K()
        kb.add(B("💵 Crypto Bot USDT", callback_data=f"{CB_PREFIX}deposit_crypto:USDT"))
        kb.add(B("🌐 Crypto Bot TON", callback_data=f"{CB_PREFIX}deposit_crypto:TON"))
        kb.add(B("⭐ Telegram Stars", callback_data=f"{CB_PREFIX}deposit_stars"))
        kb.add(B("◀️ Назад", callback_data=f"{CB_PREFIX}main"))
        self.bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)

    def _amount_keyboard(self, callback_prefix: str, back_callback: str, asset: str | None = None) -> K:
        kb = K()
        for amount in [100, 200, 500, 1000]:
            cb = f"{callback_prefix}:{amount}" if asset is None else f"{callback_prefix}:{asset}:{amount}"
            kb.add(B(f"💵 {amount}₽", callback_data=cb))
        cb_custom = f"{callback_prefix}:custom" if asset is None else f"{callback_prefix}:{asset}:custom"
        kb.add(B("✏️ Ввести своё", callback_data=cb_custom))
        kb.add(B("◀️ Назад", callback_data=back_callback))
        return kb

    def _create_crypto_invoice(self, user_id: int, chat_id: int, message_id: int, asset: str, rub_amount: Decimal) -> None:
        token = storage.config.get("crypto_bot_token", "")
        if not token:
            self.bot.edit_message_text("Crypto Bot не настроен. Обратитесь к администратору.", chat_id, message_id,
                                       reply_markup=K().add(B("Назад", callback_data=f"{CB_PREFIX}deposit")))
            return
        rate = RatesFetcher().get_rate(asset)
        if not rate:
            self.bot.edit_message_text(f"Курс для {asset} ещё не загружен.", chat_id, message_id,
                                       reply_markup=K().add(B("Назад", callback_data=f"{CB_PREFIX}deposit")))
            return
        rub_amount = _money_round(rub_amount)
        amount = _to_dec(rub_amount / rate).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        if amount <= 0:
            self.bot.edit_message_text("Сумма слишком мала.", chat_id, message_id,
                                       reply_markup=K().add(B("Назад", callback_data=f"{CB_PREFIX}deposit")))
            return
        api = CryptoBotAPI(token)
        result = api.create_invoice(user_id, amount, asset)
        pay_url = result.get("pay_url")
        invoice_id = result.get("invoice_id")
        if pay_url and invoice_id:
            storage.add_crypto_invoice(invoice_id, user_id, amount, asset, rub_amount)
            kb = K()
            kb.add(B("Оплатить", url=pay_url))
            kb.add(B("Проверить оплату", callback_data=f"{CB_PREFIX}check_crypto"))
            kb.add(B("Главное меню", callback_data=f"{CB_PREFIX}main"))
            self.bot.edit_message_text(f"Счёт на {amount} {asset} (~{_money_str(rub_amount)}₽) создан. Оплатите по кнопке ниже.",
                                       chat_id, message_id, reply_markup=kb)
        else:
            error = result.get("error", "Не удалось создать счёт.")
            self.bot.edit_message_text(f"Ошибка: {error}", chat_id, message_id,
                                       reply_markup=K().add(B("Назад", callback_data=f"{CB_PREFIX}deposit")))

    def _on_deposit_crypto_amount(self, m: Message) -> None:
        user_id = m.from_user.id
        if self._check_maintenance(user_id, m.chat.id):
            return
        state = self.get_state(user_id)
        if not state:
            return
        rub_amount = self._is_valid_amount(m.text)
        if rub_amount is None:
            self.bot.send_message(m.chat.id, "Введите положительную числовую сумму.")
            return
        asset = state["data"].get("asset", "USDT")
        message_id = state["data"].get("message_id")
        self.clear_state(user_id)
        self._create_crypto_invoice(user_id, m.chat.id, message_id, asset, rub_amount)

    def _create_stars_invoice(self, user_id: int, chat_id: int, message_id: int, rub_amount: Decimal) -> None:
        rub_amount = _money_round(rub_amount)
        stars_amount = int((rub_amount * Decimal("1.3")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        if stars_amount <= 0:
            self.bot.edit_message_text("Сумма слишком мала.", chat_id, message_id,
                                       reply_markup=K().add(B("Назад", callback_data=f"{CB_PREFIX}deposit")))
            return
        title = f"Пополнение на {_money_str(rub_amount)}₽"
        if StarsPayment.send_invoice(self.bot, chat_id, user_id, stars_amount, title=title, rub_amount=rub_amount):
            self.bot.edit_message_text(f"Инвойс на {stars_amount} Stars (~{_money_str(rub_amount)}₽) отправлен. Оплатите его в этом чате.",
                                       chat_id, message_id,
                                       reply_markup=K().add(B("Главное меню", callback_data=f"{CB_PREFIX}main")))
        else:
            self.bot.edit_message_text("Не удалось создать Stars-инвойс. Проверьте настройки бота.",
                                       chat_id, message_id,
                                       reply_markup=K().add(B("Назад", callback_data=f"{CB_PREFIX}deposit")))

    def _on_deposit_stars_amount(self, m: Message) -> None:
        user_id = m.from_user.id
        if self._check_maintenance(user_id, m.chat.id):
            return
        state = self.get_state(user_id)
        if not state:
            return
        rub_amount = self._is_valid_amount(m.text)
        if rub_amount is None:
            self.bot.send_message(m.chat.id, "Введите положительную числовую сумму.")
            return
        message_id = state["data"].get("message_id")
        self.clear_state(user_id)
        self._create_stars_invoice(user_id, m.chat.id, message_id, rub_amount)

    def _on_promo_code(self, m: Message) -> None:
        user_id = m.from_user.id
        if self._check_maintenance(user_id, m.chat.id):
            return
        state = self.get_state(user_id)
        if not state:
            return
        plan_id = state["data"].get("plan_id")
        months = state["data"].get("months")
        message_id = state["data"].get("message_id")
        code = m.text.strip()
        promo = storage.get_promocode(code)
        if not promo:
            self.bot.send_message(m.chat.id, "Промокод недействителен или истёк.")
            return
        if promo.get("plan_id") and promo["plan_id"] != plan_id:
            self.bot.send_message(m.chat.id, "Промокод не подходит для выбранного тарифа.")
            return
        base_price = storage.price(plan_id, int(months))
        if promo["discount_type"] == "percent":
            discount = _money_round(base_price * _to_dec(promo["value"]) / 100)
        else:
            discount = _money_round(promo["value"])
        discount = min(discount, base_price)
        self.set_state(user_id, "confirm_purchase", {"plan_id": plan_id, "months": months, "discount": discount, "promo_code": code, "message_id": message_id})
        self._confirm_purchase(user_id, m.chat.id, message_id, plan_id, months)

    def _on_activation_code(self, m: Message) -> None:
        user_id = m.from_user.id
        if self._check_maintenance(user_id, m.chat.id):
            return
        code = m.text.strip().upper()
        ac = storage.use_activation_code(code, user_id)
        if not ac:
            self.bot.send_message(m.chat.id, "Код недействителен, уже использован или истёк.")
            return
        plan_id = ac["plan_id"]
        months = ac["months"]
        referrer_id = ac.get("referrer_id")
        if referrer_id:
            user = storage.get_user(user_id)
            if not user.get("referred_by"):
                user["referred_by"] = referrer_id
                storage.update_user(user)
            storage.process_referral_rewards(user_id, 0.0)
        sub = storage.create_subscription(user_id, plan_id, months)
        XrayAPI.add_or_update_client(sub)
        self.clear_state(user_id)
        self.bot.send_message(m.chat.id, f"Код активирован!\n{format_subscription(sub)}", reply_markup=self._keyboard_main())

    def _on_withdraw_amount(self, m: Message) -> None:
        user_id = m.from_user.id
        if self._check_maintenance(user_id, m.chat.id):
            return
        state = self.get_state(user_id)
        if not state:
            return
        amount = self._is_valid_amount(m.text)
        if amount is None:
            self.bot.send_message(m.chat.id, "Введите положительную числовую сумму.")
            return
        user = storage.get_user(user_id)
        ref_balance = _to_dec(user.get("referral_balance", 0))
        if amount < 3000:
            self.bot.send_message(m.chat.id, "Минимальная сумма вывода 3000₽.")
            return
        if amount > ref_balance:
            self.bot.send_message(m.chat.id, f"Недостаточно реферальных средств. Доступно: {_money_str(ref_balance)}₽.")
            return
        self.set_state(user_id, "withdraw_card", {"amount": amount, "message_id": state["data"].get("message_id")})
        self.bot.send_message(m.chat.id, "Введите номер карты/реквизиты для вывода:",
                              reply_markup=K().add(B("Отмена", callback_data=f"{CB_PREFIX}referral")))

    def _on_withdraw_card(self, m: Message) -> None:
        user_id = m.from_user.id
        if self._check_maintenance(user_id, m.chat.id):
            return
        state = self.get_state(user_id)
        if not state:
            return
        amount = state["data"].get("amount")
        card = m.text.strip()
        if not card:
            self.bot.send_message(m.chat.id, "Введите реквизиты.")
            return
        req_id = storage.create_withdrawal_request(user_id, amount, card)
        self.clear_state(user_id)
        self.bot.send_message(m.chat.id, f"Заявка #{req_id} на вывод {_money_str(amount)}₽ создана. Ожидайте подтверждения администратора.",
                              reply_markup=self._keyboard_main())

    def _on_freeze_days(self, m: Message) -> None:
        user_id = m.from_user.id
        if self._check_maintenance(user_id, m.chat.id):
            return
        state = self.get_state(user_id)
        if not state:
            return
        try:
            days = int(m.text.strip())
            sub_id = state["data"].get("sub_id")
            sub = storage.get_subscription(sub_id)
            if not sub or sub.user_id != user_id:
                self.bot.send_message(m.chat.id, "Подписка не найдена.")
                return
            if days <= 0:
                self.bot.send_message(m.chat.id, "Введите положительное число дней.")
                return
            storage.freeze_subscription(sub, days)
            self.clear_state(user_id)
            self.bot.send_message(m.chat.id, f"Подписка #{sub_id} заморожена на {days} дней.", reply_markup=self._keyboard_main())
        except ValueError:
            self.bot.send_message(m.chat.id, "Введите целое число дней.")

    def _on_complaint_text(self, m: Message) -> None:
        user_id = m.from_user.id
        if self._check_maintenance(user_id, m.chat.id):
            return
        text = m.text.strip()
        if not text:
            self.bot.send_message(m.chat.id, "Введите текст жалобы.")
            return
        cid = storage.add_complaint(user_id, text)
        storage._notify_admins(f"Жалоба #{cid} от user {user_id}:\n{text}", "complaint")
        self.clear_state(user_id)
        self.bot.send_message(m.chat.id, "Жалоба отправлена. Администратор рассмотрит её.", reply_markup=self._keyboard_main())

    def _on_successful_payment(self, m: Message) -> None:
        user_id = m.from_user.id
        if self._check_maintenance(user_id, m.chat.id):
            return
        payload = m.successful_payment.invoice_payload
        if payload.startswith("stars_"):
            try:
                _, uid_str, rub_str = payload.split("_")
                uid = int(uid_str)
                rub_amount = _to_dec(rub_str)
            except (ValueError, TypeError, InvalidOperation):
                return
            stars_amount = getattr(m.successful_payment, "total_amount", 0)
            payment_id = getattr(m.successful_payment, "telegram_payment_charge_id", payload) or payload
            credited = storage.add_balance(uid, rub_amount, "Telegram Stars", "stars", payment_id)
            if credited:
                self.bot.send_message(m.chat.id, f"Баланс пополнен на {_money_str(rub_amount)}₽ ({stars_amount} Stars) через Telegram Stars.",
                                      reply_markup=self._keyboard_main())
            else:
                self.bot.send_message(m.chat.id, "Этот платёж уже был обработан ранее.",
                                      reply_markup=self._keyboard_main())

    def _profile_menu(self, user_id: int, chat_id: int, message_id: int) -> None:
        user = storage.get_user(user_id)
        subs = storage.active_subscriptions(user_id)
        lines = [
            f"<b>🧑 Профиль</b>",
            f"🆔 ID: <code>{user_id}</code>",
            f"💰 Баланс: {_money_str(user.get('balance', 0))}₽",
        ]
        if subs:
            sub = subs[0]
            plan = storage.plan(sub.plan_id)
            lines.append(f"📱 Активная подписка: {_escape(plan.name if plan else sub.plan_id)} (до {_format_time(sub.expires_at)})")
            lines.append(f"📱 Устройств: {len(sub.devices)} / {_escape(plan.device_text if plan else '?')}")
        else:
            lines.append("📱 Активных подписок нет.")
        if user.get("trial_used"):
            lines.append("🎁 Пробный период использован.")
        total_spent = _to_dec(user.get("total_spent", 0))
        total_months = user.get("total_months", 0)
        if total_spent or total_months:
            lines.append(f"💳 Всего потрачено: {_money_str(total_spent)}₽ (куплено {total_months} мес.)")
        earnings = storage.referrals.get("earnings", {}).get(str(user_id), {"level1": Decimal("0.00"), "level2": Decimal("0.00")})
        total_earn = _to_dec(earnings.get("level1", 0)) + _to_dec(earnings.get("level2", 0))
        if total_earn:
            lines.append(f"👥 Заработано с рефералов: {_money_str(total_earn)}₽")
        kb = K()
        kb.add(B("📜 История операций", callback_data=f"{CB_PREFIX}history"))
        kb.add(B("⚙️ Настройки", callback_data=f"{CB_PREFIX}settings"))
        kb.add(B("◀️ Назад", callback_data=f"{CB_PREFIX}main"))
        self.bot.edit_message_text("\n".join(lines), chat_id, message_id, reply_markup=kb)

    def _history_menu(self, user_id: int, chat_id: int, message_id: int) -> None:
        txs = storage.get_transactions(user_id)
        if not txs:
            text = "📜 История операций пуста."
        else:
            lines = ["<b>📜 История операций</b> (последние 20):"]
            type_names = {
                "deposit": "💳 Пополнение",
                "purchase": "🛒 Списание",
                "referral": "👥 Реферал",
                "trial": "🎁 Пробный период",
            }
            for tx in txs:
                tname = type_names.get(tx.get("type"), tx.get("type", "?"))
                amount = tx.get("amount", 0)
                method = tx.get("method", "")
                date = _format_time(tx.get("created_at", 0))
                lines.append(f"{date} — {tname}: {amount:+.2f}₽ ({method})")
            text = "\n".join(lines)
        self.bot.edit_message_text(text, chat_id, message_id,
                                   reply_markup=K().add(B("◀️ Назад", callback_data=f"{CB_PREFIX}profile")))

    def _settings_menu(self, user_id: int, chat_id: int, message_id: int) -> None:
        user = storage.get_user(user_id)
        settings = user.get("settings", {"lang": "ru", "notifications": True, "auto_renew": True})
        lang = settings.get("lang", "ru")
        notif = "🟢 Вкл" if settings.get("notifications", True) else "🔴 Выкл"
        renew = "🟢 Вкл" if settings.get("auto_renew", True) else "🔴 Выкл"
        text = (f"<b>⚙️ Настройки</b>\n\n"
                f"🌐 Язык: {lang.upper()}\n"
                f"🔔 Уведомления: {notif}\n"
                f"🔄 Автопродление: {renew}")
        kb = K()
        kb.add(B(f"🌐 Язык: {lang.upper()}", callback_data=f"{CB_PREFIX}toggle:lang"))
        kb.add(B(f"🔔 Уведомления: {notif}", callback_data=f"{CB_PREFIX}toggle:notifications"))
        kb.add(B(f"🔄 Автопродление: {renew}", callback_data=f"{CB_PREFIX}toggle:auto_renew"))
        kb.add(B("◀️ Назад", callback_data=f"{CB_PREFIX}profile"))
        self.bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)

    def _help_menu(self, user_id: int, chat_id: int, message_id: int) -> None:
        text = "<b>❓ Помощь</b>\n\nВыберите раздел:"
        kb = K()
        kb.add(B("📖 FAQ", callback_data=f"{CB_PREFIX}faq"))
        kb.add(B("🆘 Поддержка", callback_data=f"{CB_PREFIX}support"))
        kb.add(B("📝 Пожаловаться", callback_data=f"{CB_PREFIX}complaint"))
        kb.add(B("◀️ Назад", callback_data=f"{CB_PREFIX}main"))
        self.bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)

    def _faq_menu(self, user_id: int, chat_id: int, message_id: int) -> None:
        faq = storage.config.get("faq_text")
        if not faq:
            faq = (
                "<b>📖 FAQ</b>\n\n"
                "1. Как подключиться?\nПосле покупки подписки вам выдаётся конфигурация для вашего устройства.\n\n"
                "2. Сколько устройств поддерживается?\n"
                "Зависит от тарифа: Базовый — 1, Семейный — 5, Корпоративный — безлимит.\n\n"
                "3. Как пополнить баланс?\nРаздел «Пополнить баланс» → Crypto Bot или Telegram Stars.\n\n"
                "4. Пробный период?\n3 дня, 1 устройство, один раз на аккаунт."
            )
        self.bot.edit_message_text(faq, chat_id, message_id,
                                   reply_markup=K().add(B("◀️ Назад", callback_data=f"{CB_PREFIX}help")))

    def _support_menu(self, user_id: int, chat_id: int, message_id: int) -> None:
        support = storage.config.get("support", "@support")
        text = f"<b>🆘 Поддержка</b>\n\nПо всем вопросам обращайтесь: {support}"
        self.bot.edit_message_text(text, chat_id, message_id,
                                   reply_markup=K().add(B("◀️ Назад", callback_data=f"{CB_PREFIX}help")))

    def _referral_menu(self, user_id: int, chat_id: int, message_id: int) -> None:
        bot_username = storage.config.get("bot_username", "")
        user = storage.get_user(user_id)
        link = f"https://t.me/{bot_username}?start=ref_{user_id}" if bot_username else f"Код: ref_{user_id}"
        level1 = [u for u in storage.users.values() if u.get("referred_by") == user_id]
        level2 = [u for u in storage.users.values() if u.get("referred_by") in {x["user_id"] for x in level1}]
        earnings = storage.referrals.get("earnings", {}).get(str(user_id), {"level1": Decimal("0.00"), "level2": Decimal("0.00")})
        ref_balance = _to_dec(user.get("referral_balance", 0))
        total_earn = _to_dec(earnings.get("level1", 0)) + _to_dec(earnings.get("level2", 0))
        text = (f"<b>👥 Реферальная система</b>\n\n"
                f"🔗 Ваша ссылка: {link}\n\n"
                f"👤 Рефералы 1 уровня: {len(level1)}\n"
                f"👥 Рефералы 2 уровня: {len(level2)}\n"
                f"💵 Заработано: {_money_str(total_earn)}₽\n"
                f"  1️⃣ уровень (10%): {_money_str(earnings.get('level1', 0))}₽\n"
                f"  2️⃣ уровень (5%): {_money_str(earnings.get('level2', 0))}₽\n\n"
                f"💰 Реферальный баланс: {_money_str(ref_balance)}₽\n"
                f"Минимум для вывода: 3000₽")
        kb = K()
        kb.add(B("💸 Вывести реферальные средства", callback_data=f"{CB_PREFIX}withdraw"))
        kb.add(B("◀️ Назад", callback_data=f"{CB_PREFIX}main"))
        self.bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)

    # ---- lifecycle ----
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        try:
            me = self.bot.get_me()
            if me and me.username:
                storage.config["bot_username"] = me.username
                storage.save_config()
        except Exception:
            logger.exception("Не удалось получить username user-бота")
        self._crypto_poller = CryptoBotPoller(self)
        self._crypto_poller.start()
        self._scheduler = SubscriptionScheduler(self)
        self._scheduler.start()
        self._rates_fetcher = RatesFetcher()
        self._rates_fetcher.start()
        self._device_auth_server = DeviceAuthServer(storage.config.get("device_auth_port", 8080))
        self._device_auth_server.start()
        server_cfg = storage.server()
        if server_cfg.temp_profile_enabled:
            self._temp_profile_server = TempProfileServer(server_cfg.temp_profile_port)
            self._temp_profile_server.start()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("User-бот запущен.")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                # skip_pending=False чтобы не терять successful_payment после рестарта
                self.bot.polling(non_stop=True, skip_pending=False, timeout=10, long_polling_timeout=5)
            except Exception:
                logger.exception("Ошибка polling user-бота")
                time.sleep(5)

    def stop(self) -> None:
        self._stop.set()
        self.bot.stop_polling()
        if self._crypto_poller:
            self._crypto_poller.stop()
        if self._scheduler:
            self._scheduler.stop()
        if self._rates_fetcher:
            self._rates_fetcher.stop()
        if self._device_auth_server:
            self._device_auth_server.stop()
        if self._temp_profile_server:
            self._temp_profile_server.stop()
        if self._thread:
            self._thread.join(timeout=10)


# ==================== Admin Panel (Cardinal bot) ====================


def is_admin(cardinal: Cardinal, user_id: int) -> bool:
    return cardinal.telegram and user_id in cardinal.telegram.authorized_users


def _start_user_bot(cardinal: Cardinal, chat_id: int | None = None) -> None:
    global _user_bot_instance
    token = storage.config.get("user_bot_token", "")
    if not token:
        if chat_id is not None:
            cardinal.telegram.bot.send_message(chat_id, "Токен user-бота не задан. Установите через /vpnadmin.")
        return
    if _user_bot_instance is not None:
        _user_bot_instance.stop()
    try:
        _user_bot_instance = UserBot(token)
        _user_bot_instance.start()
        if chat_id is not None:
            cardinal.telegram.bot.send_message(chat_id, "User-бот запущен.")
    except Exception as e:
        if chat_id is not None:
            cardinal.telegram.bot.send_message(chat_id, f"Ошибка запуска user-бота: {e}")


def init_plugin(cardinal: Cardinal, *args) -> None:
    tg = cardinal.telegram
    if not tg:
        return
    bot = tg.bot

    cardinal.add_telegram_commands(UUID, [
        ("vpnadmin", "Админ-панель VPN", True),
    ])

    def cmd_vpnadmin(m: Message):
        if not is_admin(cardinal, m.from_user.id):
            bot.send_message(m.chat.id, "Нет доступа.")
            return
        bot.send_message(m.chat.id, "<b>🛡 VPN Admin</b>\n\nВыберите раздел:", reply_markup=_admin_main_keyboard())

    tg.msg_handler(cmd_vpnadmin, commands=["vpnadmin"])

    def cbq_router(c: CallbackQuery):
        try:
            _handle_admin_callback(cardinal, c)
        except Exception:
            logger.exception("Ошибка обработки admin callback")
        finally:
            try:
                bot.answer_callback_query(c.id)
            except Exception:
                pass

    tg.cbq_handler(cbq_router, func=lambda c: c.data.startswith(CB_PREFIX))

    def state_set_token(m: Message):
        storage.config["user_bot_token"] = m.text.strip()
        storage.save_config()
        bot.send_message(m.chat.id, "Токен user-бота сохранен. Перезапустите user-бота.")
        tg.clear_state(m.chat.id, m.from_user.id)

    def state_set_channel(m: Message):
        storage.config["channel_id"] = m.text.strip()
        storage.save_config()
        bot.send_message(m.chat.id, "Канал сохранен.")
        tg.clear_state(m.chat.id, m.from_user.id)

    def state_set_support(m: Message):
        support = m.text.strip()
        storage.config["support_id"] = support
        try:
            uid = int(support)
            admin_ids = storage.config.setdefault("admin_user_ids", [])
            if uid not in admin_ids:
                admin_ids.append(uid)
        except (ValueError, TypeError):
            pass
        storage.save_config()
        bot.send_message(m.chat.id, "Админ поддержки сохранен.")
        tg.clear_state(m.chat.id, m.from_user.id)

    def state_set_crypto_token(m: Message):
        storage.config["crypto_bot_token"] = m.text.strip()
        storage.save_config()
        bot.send_message(m.chat.id, "Токен Crypto Bot сохранен.")
        tg.clear_state(m.chat.id, m.from_user.id)

    def state_set_server(m: Message):
        parts = m.text.strip().split()
        if len(parts) < 5:
            bot.send_message(
                m.chat.id,
                "Формат:\n"
                "<code>адрес порт public_key short_id server_name [panel_url panel_username panel_password inbound_id verify_ssl sub_port subscription_url_base]</code>\n\n"
                "Пример:\n"
                "<code>vpn.example.com 443 PUBKEY SHORTID teamdocs.su http://1.2.3.4:2053 admin admin 1 true 2096 sub.example.com</code>"
            )
            return
        try:
            server = storage.server()
            server.address = parts[0]
            server.port = int(parts[1])
            server.public_key = parts[2]
            server.short_id = parts[3]
            server.server_name = parts[4]
            if len(parts) >= 6:
                server.panel_url = parts[5]
            if len(parts) >= 7:
                server.panel_username = parts[6]
            if len(parts) >= 8:
                server.panel_password = parts[7]
            if len(parts) >= 9:
                server.inbound_id = int(parts[8])
            if len(parts) >= 10:
                server.verify_ssl = parts[9].lower() in ("true", "1", "yes", "on")
            if len(parts) >= 11:
                server.sub_port = int(parts[10])
            if len(parts) >= 12:
                server.subscription_url_base = parts[11]
            storage.set_server(server)
            bot.send_message(m.chat.id, "Сервер и 3X-UI сохранены.")
        except Exception as e:
            bot.send_message(m.chat.id, f"Ошибка: {e}")
        tg.clear_state(m.chat.id, m.from_user.id)

    def state_set_temp_profiles(m: Message):
        parts = m.text.strip().split()
        if len(parts) < 3:
            bot.send_message(m.chat.id, "Формат: <code>включено(true/false) порт inbound_id</code>\nПример: <code>true 8081 2</code>")
            return
        try:
            server = storage.server()
            server.temp_profile_enabled = parts[0].lower() in ("true", "1", "yes", "on")
            server.temp_profile_port = int(parts[1])
            server.temp_inbound_id = int(parts[2])
            storage.set_server(server)
            bot.send_message(m.chat.id, "Настройки временных профилей сохранены. Перезапустите user-бота.")
        except Exception as e:
            bot.send_message(m.chat.id, f"Ошибка: {e}")
        tg.clear_state(m.chat.id, m.from_user.id)

    def _set_media_from_message(m: Message, key_prefix: str, label: str) -> None:
        file_id = None
        media_type = "photo"
        if m.content_type == "photo" and m.photo:
            file_id = m.photo[-1].file_id
        elif m.content_type == "video" and m.video:
            file_id = m.video.file_id
            media_type = "video"
        elif m.content_type == "animation" and m.animation:
            file_id = m.animation.file_id
            media_type = "animation"
        if not file_id:
            bot.send_message(m.chat.id, "Отправьте фото, GIF (анимацию) или видео.")
            return
        storage.config[f"{key_prefix}_media_file_id"] = file_id
        storage.config[f"{key_prefix}_media_type"] = media_type
        storage.save_config()
        bot.send_message(m.chat.id, f"{label} {media_type} сохранено.")
        tg.clear_state(m.chat.id, m.from_user.id)

    def state_set_welcome_media(m: Message):
        _set_media_from_message(m, "welcome", "Приветственное")

    def state_set_menu_media(m: Message):
        _set_media_from_message(m, "menu", "Медиа меню")

    def state_set_faq_text(m: Message):
        storage.config["faq_text"] = m.text.strip() if m.text else ""
        storage.save_config()
        bot.send_message(m.chat.id, "Текст FAQ сохранен.")
        tg.clear_state(m.chat.id, m.from_user.id)

    def state_admin_plan_name(m: Message):
        state = tg.get_state(m.chat.id, m.from_user.id)
        if not state:
            return
        plan_id = state["data"].get("plan_id")
        storage.update_plan(plan_id, name=m.text.strip())
        bot.send_message(m.chat.id, f"Название тарифа {plan_id} обновлено.")
        tg.clear_state(m.chat.id, m.from_user.id)

    def state_admin_plan_devices(m: Message):
        state = tg.get_state(m.chat.id, m.from_user.id)
        if not state:
            return
        try:
            max_devices = int(m.text.strip())
            plan_id = state["data"].get("plan_id")
            storage.update_plan(plan_id, max_devices=max_devices)
            bot.send_message(m.chat.id, f"Лимит устройств для {plan_id} обновлён.")
        except ValueError:
            bot.send_message(m.chat.id, "Введите число.")
        tg.clear_state(m.chat.id, m.from_user.id)

    def state_admin_plan_price(m: Message):
        state = tg.get_state(m.chat.id, m.from_user.id)
        if not state:
            return
        try:
            price = _to_dec(m.text.strip().replace(",", "."))
            plan_id = state["data"].get("plan_id")
            months = state["data"].get("months")
            plan = storage.plan(plan_id)
            prices = {k: _to_dec(v) for k, v in (plan.prices.items() if plan else {})}
            prices[str(months)] = price
            storage.update_plan(plan_id, prices=prices)
            bot.send_message(m.chat.id, f"Цена для {months} мес. тарифа {plan_id} обновлена.")
        except (ValueError, InvalidOperation):
            bot.send_message(m.chat.id, "Введите число.")
        tg.clear_state(m.chat.id, m.from_user.id)

    def state_admin_plan_add_id(m: Message):
        plan_id = m.text.strip()
        if not plan_id or not plan_id.isalnum():
            bot.send_message(m.chat.id, "Введите корректный ID (латиница/цифры).")
            return
        tg.set_state(m.chat.id, m.message_id, m.from_user.id, f"{CB_PREFIX}admin_plan_add_name", {"plan_id": plan_id})
        bot.send_message(m.chat.id, "Отправьте название нового тарифа:")

    def state_admin_plan_add_name(m: Message):
        state = tg.get_state(m.chat.id, m.from_user.id)
        if not state:
            return
        plan_id = state["data"].get("plan_id")
        name = m.text.strip()
        state["data"]["name"] = name
        tg.set_state(m.chat.id, m.message_id, m.from_user.id, f"{CB_PREFIX}admin_plan_add_devices", state["data"])
        bot.send_message(m.chat.id, "Отправьте лимит устройств (-1 для безлимита):")

    def state_admin_plan_add_devices(m: Message):
        state = tg.get_state(m.chat.id, m.from_user.id)
        if not state:
            return
        try:
            max_devices = int(m.text.strip())
            state["data"]["max_devices"] = max_devices
            hosts = ", ".join(h.host_id for h in storage.list_hosts()) or "main"
            tg.set_state(m.chat.id, m.message_id, m.from_user.id, f"{CB_PREFIX}admin_plan_add_host", state["data"])
            bot.send_message(m.chat.id, f"Выберите сервер ({hosts}), по умолчанию <code>main</code>:")
        except ValueError:
            bot.send_message(m.chat.id, "Введите число.")

    def state_admin_plan_add_host(m: Message):
        state = tg.get_state(m.chat.id, m.from_user.id)
        if not state:
            return
        host_id = m.text.strip() or storage.default_host_id()
        if host_id not in storage.config.get("hosts", {}) and storage.list_hosts():
            bot.send_message(m.chat.id, "Сервер не найден. Введите существующий host_id.")
            return
        state["data"]["host_id"] = host_id
        tg.set_state(m.chat.id, m.message_id, m.from_user.id, f"{CB_PREFIX}admin_plan_add_prices", state["data"])
        bot.send_message(m.chat.id, f"Отправьте цены через пробел для {', '.join(DURATIONS)} мес. (например: 100 270 500 900):")

    def state_admin_plan_add_prices(m: Message):
        state = tg.get_state(m.chat.id, m.from_user.id)
        if not state:
            return
        parts = m.text.strip().split()
        if len(parts) != len(DURATIONS):
            bot.send_message(m.chat.id, f"Нужно {len(DURATIONS)} цены через пробел.")
            return
        try:
            prices = {str(DURATIONS[i]): _to_dec(parts[i].replace(",", ".")) for i in range(len(DURATIONS))}
            data = state["data"]
            storage.add_plan(data["plan_id"], data["name"], data["max_devices"], prices)
            storage.update_plan(data["plan_id"], host_id=data.get("host_id", storage.default_host_id()))
            bot.send_message(m.chat.id, f"Тариф {data['name']} добавлен.")
        except (ValueError, InvalidOperation):
            bot.send_message(m.chat.id, "Введите числовые цены.")
        tg.clear_state(m.chat.id, m.from_user.id)

    def state_admin_plan_host(m: Message):
        state = tg.get_state(m.chat.id, m.from_user.id)
        if not state:
            return
        plan_id = state["data"].get("plan_id")
        host_id = m.text.strip()
        if host_id not in storage.config.get("hosts", {}):
            bot.send_message(m.chat.id, "Сервер не найден. Введите существующий host_id.")
            return
        storage.update_plan(plan_id, host_id=host_id)
        bot.send_message(m.chat.id, f"Сервер для тарифа {plan_id} обновлён.")
        tg.clear_state(m.chat.id, m.from_user.id)

    def state_admin_promo_add(m: Message):
        parts = m.text.strip().split()
        if len(parts) < 4:
            bot.send_message(m.chat.id, "Формат: <code>КОД процент|фикс ЗНАЧЕНИЕ MAX_USES [PLAN_ID] [ЧАСЫ]</code>")
            return
        code = parts[0].upper()
        d_type = parts[1]
        if d_type not in ("percent", "фикс", "fixed"):
            bot.send_message(m.chat.id, "Тип скидки: percent или fixed/фикс.")
            return
        if d_type in ("фикс",):
            d_type = "fixed"
        try:
            value = _to_dec(parts[2].replace(",", "."))
            max_uses = int(parts[3])
        except (ValueError, InvalidOperation):
            bot.send_message(m.chat.id, "Значение и max_uses должны быть числами.")
            return
        plan_id = parts[4] if len(parts) >= 5 else None
        expires_at = None
        if len(parts) >= 6:
            try:
                expires_at = time.time() + float(parts[5]) * 3600
            except ValueError:
                bot.send_message(m.chat.id, "Часы должны быть числом.")
                return
        storage.create_promocode(code, d_type, value, max_uses, expires_at, plan_id)
        bot.send_message(m.chat.id, f"Промокод {code} создан.")
        tg.clear_state(m.chat.id, m.from_user.id)

    def state_admin_code_add(m: Message):
        parts = m.text.strip().split()
        if len(parts) < 4:
            bot.send_message(m.chat.id, "Формат: <code>КОД PLAN_ID МЕСЯЦЫ USES [REFERER_ID]</code>")
            return
        code = parts[0].upper()
        plan_id = parts[1]
        try:
            months = int(parts[2])
            uses = int(parts[3])
            referrer_id = int(parts[4]) if len(parts) >= 5 else None
        except ValueError:
            bot.send_message(m.chat.id, "Месяцы, uses и referer_id должны быть числами.")
            return
        storage.create_activation_code(code, plan_id, months, uses, referrer_id)
        bot.send_message(m.chat.id, f"Код {code} создан.")
        tg.clear_state(m.chat.id, m.from_user.id)

    def state_give_uid(m: Message):
        try:
            uid = int(m.text.strip())
            tg.set_state(m.chat.id, m.message_id, m.from_user.id, f"{CB_PREFIX}admin_give_plan", {"uid": uid})
            bot.send_message(m.chat.id, "Выберите тариф:", reply_markup=_admin_plans_keyboard("admin_give_plan"))
        except ValueError:
            bot.send_message(m.chat.id, "Введите числовой Telegram user_id.")

    def state_balance_uid(m: Message):
        try:
            uid = int(m.text.strip())
            tg.set_state(m.chat.id, m.message_id, m.from_user.id, f"{CB_PREFIX}admin_balance_amount", {"uid": uid})
            bot.send_message(m.chat.id, "Введите сумму (положительную — начислить, отрицательную — списать):")
        except ValueError:
            bot.send_message(m.chat.id, "Введите числовой Telegram user_id.")

    def state_balance_amount(m: Message):
        state = tg.get_state(m.chat.id, m.from_user.id)
        if not state:
            return
        try:
            amount = _to_dec(m.text.strip().replace(",", "."))
            uid = state["data"]["uid"]
            if amount > 0:
                storage.add_balance(uid, amount, "manual", "admin")
            else:
                storage.deduct_balance(uid, -amount, "manual", "admin")
            user = storage.get_user(uid)
            bot.send_message(m.chat.id, f"Баланс пользователя {uid}: {_money_str(user['balance'])}₽")
        except (ValueError, InvalidOperation):
            bot.send_message(m.chat.id, "Введите число.")
        tg.clear_state(m.chat.id, m.from_user.id)

    def state_device_ip(m: Message):
        state = tg.get_state(m.chat.id, m.from_user.id)
        if not state:
            return
        sub_id = state["data"]["sub_id"]
        ip = m.text.strip()
        tg.clear_state(m.chat.id, m.from_user.id)
        sub = storage.get_subscription(sub_id)
        if not sub:
            bot.send_message(m.chat.id, "Подписка не найдена.")
            return
        plan = storage.plan(sub.plan_id)
        if plan and plan.max_devices != -1 and len(sub.devices) >= plan.max_devices:
            bot.send_message(m.chat.id, f"Лимит устройств ({plan.device_text}) достигнут.")
            return
        sub.devices.append({"ip": ip, "first_seen": time.time(), "last_seen": time.time()})
        storage.update_subscription(sub)
        bot.send_message(m.chat.id, f"Устройство {ip} добавлено к подписке #{sub_id}.")

    def state_del_ip(m: Message):
        state = tg.get_state(m.chat.id, m.from_user.id)
        if not state:
            return
        sub_id = state["data"]["sub_id"]
        ip = m.text.strip()
        tg.clear_state(m.chat.id, m.from_user.id)
        sub = storage.get_subscription(sub_id)
        if not sub:
            bot.send_message(m.chat.id, "Подписка не найдена.")
            return
        before = len(sub.devices)
        sub.devices = [d for d in sub.devices if d["ip"] != ip]
        storage.update_subscription(sub)
        bot.send_message(m.chat.id, f"Устройство {ip} удалено." if len(sub.devices) < before else f"Устройство {ip} не найдено.")

    def state_admin_freeze_days(m: Message):
        state = tg.get_state(m.chat.id, m.from_user.id)
        if not state:
            return
        try:
            days = int(m.text.strip())
            sub_id = state["data"]["sub_id"]
            sub = storage.get_subscription(sub_id)
            if not sub:
                bot.send_message(m.chat.id, "Подписка не найдена.")
                return
            if days <= 0:
                bot.send_message(m.chat.id, "Введите положительное число дней.")
                return
            storage.freeze_subscription(sub, days)
            bot.send_message(m.chat.id, f"Подписка #{sub_id} заморожена на {days} дней.")
        except ValueError:
            bot.send_message(m.chat.id, "Введите целое число дней.")
        tg.clear_state(m.chat.id, m.from_user.id)

    def state_admin_refund_amount(m: Message):
        state = tg.get_state(m.chat.id, m.from_user.id)
        if not state:
            return
        try:
            amount = _to_dec(m.text.strip().replace(",", "."))
            sub_id = state["data"]["sub_id"]
            sub = storage.get_subscription(sub_id)
            if not sub:
                bot.send_message(m.chat.id, "Подписка не найдена.")
                return
            if amount <= 0:
                # auto-calc remaining value
                total = storage.price(sub.plan_id, sub.months) or Decimal("0.00")
                total_days = sub.months * 30 if sub.months else 3
                remaining_days = max(0, (sub.effective_expires_at - time.time()) / 86400)
                amount = _money_round(_to_dec(total) * _to_dec(remaining_days) / _to_dec(total_days))
            storage.refund_subscription(sub, amount)
            bot.send_message(m.chat.id, f"Возвращено {_money_str(amount)}₽ пользователю {sub.user_id} за подписку #{sub.sub_id}.")
            if _user_bot_instance:
                try:
                    _user_bot_instance.bot.send_message(sub.user_id, f"По подписке #{sub.sub_id} возвращено {_money_str(amount)}₽.")
                except Exception:
                    pass
        except (ValueError, InvalidOperation):
            bot.send_message(m.chat.id, "Введите число.")
        tg.clear_state(m.chat.id, m.from_user.id)

    def state_admin_security_window(m: Message):
        try:
            sec = storage.config.setdefault("security", {})
            sec["sharing_window"] = int(m.text.strip())
            storage.save_config()
            bot.send_message(m.chat.id, "Окно анти-шаринга обновлено.")
        except ValueError:
            bot.send_message(m.chat.id, "Введите целое число секунд.")
        tg.clear_state(m.chat.id, m.from_user.id)

    def state_admin_security_cooldown(m: Message):
        try:
            days = int(m.text.strip())
            sec = storage.config.setdefault("security", {})
            sec["unbind_cooldown"] = days * 86400
            storage.save_config()
            bot.send_message(m.chat.id, f"Кулдаун отвязки установлен на {days} дней.")
        except ValueError:
            bot.send_message(m.chat.id, "Введите целое число дней.")
        tg.clear_state(m.chat.id, m.from_user.id)

    def state_admin_security_traffic(m: Message):
        try:
            gb = float(m.text.strip().replace(",", "."))
            sec = storage.config.setdefault("security", {})
            sec["traffic_limit_gb"] = max(0.0, gb)
            storage.save_config()
            bot.send_message(m.chat.id, f"Лимит трафика установлен на {gb} ГБ (0 — без ограничения).")
        except ValueError:
            bot.send_message(m.chat.id, "Введите число.")
        tg.clear_state(m.chat.id, m.from_user.id)

    def state_admin_search_query(m: Message):
        query = m.text.strip()
        u = storage.search_user(query)
        if not u:
            bot.send_message(m.chat.id, "Пользователь не найден.")
            tg.clear_state(m.chat.id, m.from_user.id)
            return
        uid = int(u.get("user_id", 0))
        text, kb = _admin_user_card(uid)
        bot.send_message(m.chat.id, text, reply_markup=kb)
        tg.clear_state(m.chat.id, m.from_user.id)

    def state_admin_bulk_extend(m: Message):
        try:
            days = int(m.text.strip())
            count = storage.bulk_extend_active_subscriptions(days)
            bot.send_message(m.chat.id, f"{count} активных подписок продлено на {days} дней.")
        except ValueError:
            bot.send_message(m.chat.id, "Введите целое число дней.")
        tg.clear_state(m.chat.id, m.from_user.id)

    def state_admin_bulk_balance(m: Message):
        try:
            amount = _to_dec(m.text.strip().replace(",", "."))
            count = storage.bulk_add_balance(amount)
            bot.send_message(m.chat.id, f"{count} пользователям начислено {_money_str(amount)}₽.")
        except (ValueError, InvalidOperation):
            bot.send_message(m.chat.id, "Введите число.")
        tg.clear_state(m.chat.id, m.from_user.id)

    def state_admin_bulk_text(m: Message):
        state = tg.get_state(m.chat.id, m.from_user.id)
        if not state:
            return
        op = state["data"].get("op")
        filter_type = state["data"].get("filter_type")
        if filter_type == "source" and "source_value" not in state["data"]:
            state["data"]["source_value"] = m.text.strip()
            tg.set_state(m.chat.id, m.message_id, m.from_user.id, f"{CB_PREFIX}admin_bulk_text", state["data"])
            bot.send_message(m.chat.id, "✏️ Введите текст рассылки:")
            return
        text = m.text.strip()
        if op != "broadcast" or not text:
            bot.send_message(m.chat.id, "Ошибка рассылки.")
            tg.clear_state(m.chat.id, m.from_user.id)
            return
        state["data"]["text"] = text
        tg.set_state(m.chat.id, m.message_id, m.from_user.id, f"{CB_PREFIX}admin_bulk_photo", state["data"])
        bot.send_message(m.chat.id, "📎 Приложите фото к рассылке или отправьте '-' / 'пропустить' для отправки только текста:")

    def state_admin_bulk_photo(m: Message):
        state = tg.get_state(m.chat.id, m.from_user.id)
        if not state:
            return
        text = state["data"].get("text", "")
        filter_type = state["data"].get("filter_type")
        source_value = state["data"].get("source_value")
        photo_file_id = None
        if m.photo:
            photo_file_id = m.photo[-1].file_id
        elif m.text and m.text.strip() in ("-", "пропустить", "skip"):
            photo_file_id = None
        else:
            bot.send_message(m.chat.id, "📎 Приложите фото или отправьте '-' / 'пропустить'.")
            return
        if not _user_bot_instance:
            bot.send_message(m.chat.id, "User-бот не запущен. Сначала запустите его в /vpnadmin.")
            tg.clear_state(m.chat.id, m.from_user.id)
            return
        recipients = storage.broadcast_recipients(filter_type, source_value)
        sent = 0
        failed = 0
        safe_text = _escape(text)
        for uid in recipients:
            try:
                if photo_file_id:
                    _user_bot_instance.bot.send_photo(uid, photo=photo_file_id, caption=safe_text, parse_mode="HTML")
                else:
                    _user_bot_instance.bot.send_message(uid, safe_text, parse_mode="HTML")
                sent += 1
            except Exception:
                failed += 1
        bot.send_message(m.chat.id, f"✅ Рассылка завершена. Отправлено: {sent}, не удалось: {failed}.")
        tg.clear_state(m.chat.id, m.from_user.id)

    def _parse_host_line(parts: list[str], host_id: str) -> ServerConfig | None:
        if len(parts) < 6:
            return None
        try:
            server = ServerConfig(
                host_id=host_id,
                name=parts[0],
                address=parts[1],
                port=int(parts[2]),
                public_key=parts[3],
                short_id=parts[4],
                server_name=parts[5],
            )
            if len(parts) >= 9:
                server.panel_url = parts[6]
                server.panel_username = parts[7]
                server.panel_password = parts[8]
            if len(parts) >= 10:
                server.inbound_id = int(parts[9])
            if len(parts) >= 11:
                server.verify_ssl = parts[10].lower() in ("true", "1", "yes")
            if len(parts) >= 12:
                server.sub_port = int(parts[11])
            if len(parts) >= 13:
                server.subscription_url_base = parts[12]
            return server
        except (ValueError, TypeError):
            return None

    def state_admin_host_add(m: Message):
        host_id = m.text.strip()
        if not host_id or not host_id.isalnum():
            bot.send_message(m.chat.id, "Введите корректный host_id (латиница/цифры).")
            return
        if host_id in storage.config.get("hosts", {}):
            bot.send_message(m.chat.id, "Такой host_id уже существует.")
            return
        tg.set_state(m.chat.id, m.message_id, m.from_user.id, f"{CB_PREFIX}admin_host_data", {"host_id": host_id})
        bot.send_message(m.chat.id, "Отправьте полные данные сервера:\n"
                         "<code>name address port public_key short_id server_name [panel_url username password inbound_id verify_ssl sub_port subscription_url_base]</code>")

    def state_admin_host_data(m: Message):
        state = tg.get_state(m.chat.id, m.from_user.id)
        if not state:
            return
        host_id = state["data"].get("host_id")
        parts = m.text.strip().split()
        server = _parse_host_line(parts, host_id)
        if not server:
            bot.send_message(m.chat.id, "Недостаточно данных или неверный формат.")
            return
        storage.set_host(server)
        bot.send_message(m.chat.id, f"Сервер {host_id} добавлен.")
        tg.clear_state(m.chat.id, m.from_user.id)

    def state_admin_host_edit(m: Message):
        state = tg.get_state(m.chat.id, m.from_user.id)
        if not state:
            return
        host_id = state["data"].get("host_id")
        parts = m.text.strip().split()
        server = _parse_host_line(parts, host_id)
        if not server:
            bot.send_message(m.chat.id, "Недостаточно данных или неверный формат.")
            return
        storage.set_host(server)
        bot.send_message(m.chat.id, f"Сервер {host_id} обновлён.")
        tg.clear_state(m.chat.id, m.from_user.id)

    def state_admin_ban_user(m: Message):
        text = m.text.strip()
        u = storage.search_user(text)
        if not u:
            bot.send_message(m.chat.id, "Пользователь не найден.")
            tg.clear_state(m.chat.id, m.from_user.id)
            return
        uid = int(u["user_id"])
        if storage.is_banned(uid):
            storage.unban_user(uid)
            bot.send_message(m.chat.id, f"Пользователь {uid} разблокирован.")
        else:
            storage.ban_user(uid)
            bot.send_message(m.chat.id, f"Пользователь {uid} заблокирован.")
        tg.clear_state(m.chat.id, m.from_user.id)

    def state_admin_settings(m: Message):
        parts = m.text.strip().split()
        if len(parts) < 2:
            bot.send_message(m.chat.id, "Нужно два числа: <code>referral_discount cleanup_days</code>")
            return
        try:
            discount = float(parts[0].replace(",", "."))
            cleanup = int(parts[1])
            storage.config["referral_first_discount"] = max(0.0, min(100.0, discount))
            storage.config["expired_cleanup_days"] = max(0, cleanup)
            storage.save_config()
            bot.send_message(m.chat.id, "Настройки сохранены.")
        except ValueError:
            bot.send_message(m.chat.id, "Введите числа.")
        tg.clear_state(m.chat.id, m.from_user.id)

    def state_admin_user_msg_text(m: Message):
        state = tg.get_state(m.chat.id, m.from_user.id)
        if not state:
            return
        text = m.text.strip()
        if not text:
            bot.send_message(m.chat.id, "Текст не может быть пустым.")
            return
        state["data"]["text"] = text
        tg.set_state(m.chat.id, m.message_id, m.from_user.id, f"{CB_PREFIX}admin_user_msg_photo", state["data"])
        bot.send_message(m.chat.id, "📎 Приложите фото или отправьте '-' / 'пропустить' для отправки только текста:")

    def state_admin_user_msg_photo(m: Message):
        state = tg.get_state(m.chat.id, m.from_user.id)
        if not state:
            return
        target_uid = state["data"].get("target_uid")
        text = state["data"].get("text", "")
        photo_file_id = None
        if m.photo:
            photo_file_id = m.photo[-1].file_id
        elif m.text and m.text.strip() in ("-", "пропустить", "skip"):
            photo_file_id = None
        else:
            bot.send_message(m.chat.id, "📎 Приложите фото или отправьте '-' / 'пропустить'.")
            return
        if not _user_bot_instance:
            bot.send_message(m.chat.id, "User-бот не запущен. Сначала запустите его в /vpnadmin.")
            tg.clear_state(m.chat.id, m.from_user.id)
            return
        try:
            if photo_file_id:
                _user_bot_instance.bot.send_photo(target_uid, photo=photo_file_id, caption=_escape(text), parse_mode="HTML")
            else:
                _user_bot_instance.bot.send_message(target_uid, _escape(text), parse_mode="HTML")
            bot.send_message(m.chat.id, "✅ Сообщение отправлено.")
        except Exception:
            logger.exception("Failed to send message to user %s", target_uid)
            bot.send_message(m.chat.id, "❌ Не удалось отправить сообщение пользователю.")
        tg.clear_state(m.chat.id, m.from_user.id)

    tg.msg_handler(state_set_token, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}set_user_token"))
    tg.msg_handler(state_set_channel, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}set_channel"))
    tg.msg_handler(state_set_support, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}set_support"))
    tg.msg_handler(state_set_crypto_token, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}set_crypto_token"))
    tg.msg_handler(state_set_server, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}set_server"))
    tg.msg_handler(state_admin_host_add, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_host_add"))
    tg.msg_handler(state_admin_host_data, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_host_data"))
    tg.msg_handler(state_admin_host_edit, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_host_edit"))
    tg.msg_handler(state_set_temp_profiles, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}set_temp_profiles"))
    tg.msg_handler(state_set_welcome_media, content_types=["photo", "video", "animation", "text"], func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}set_welcome_media"))
    tg.msg_handler(state_set_menu_media, content_types=["photo", "video", "animation", "text"], func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}set_menu_media"))
    tg.msg_handler(state_set_faq_text, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}set_faq_text"))
    tg.msg_handler(state_admin_plan_name, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_plan_name"))
    tg.msg_handler(state_admin_plan_devices, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_plan_devices"))
    tg.msg_handler(state_admin_plan_price, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_plan_price"))
    tg.msg_handler(state_admin_plan_add_id, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_plan_add_id"))
    tg.msg_handler(state_admin_plan_add_name, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_plan_add_name"))
    tg.msg_handler(state_admin_plan_add_devices, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_plan_add_devices"))
    tg.msg_handler(state_admin_plan_add_host, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_plan_add_host"))
    tg.msg_handler(state_admin_plan_add_prices, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_plan_add_prices"))
    tg.msg_handler(state_admin_plan_host, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_plan_host"))
    tg.msg_handler(state_admin_promo_add, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_promo_add"))
    tg.msg_handler(state_admin_code_add, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_code_add"))
    tg.msg_handler(state_give_uid, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_give_uid"))
    tg.msg_handler(state_balance_uid, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_balance_uid"))
    tg.msg_handler(state_balance_amount, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_balance_amount"))
    tg.msg_handler(state_device_ip, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_device_ip"))
    tg.msg_handler(state_del_ip, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_del_ip"))
    tg.msg_handler(state_admin_freeze_days, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_freeze_days"))
    tg.msg_handler(state_admin_refund_amount, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_refund_amount"))
    tg.msg_handler(state_admin_security_window, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_security_window"))
    tg.msg_handler(state_admin_security_cooldown, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_security_cooldown"))
    tg.msg_handler(state_admin_security_traffic, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_security_traffic"))
    tg.msg_handler(state_admin_search_query, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_search_query"))
    tg.msg_handler(state_admin_ban_user, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_ban_user"))
    tg.msg_handler(state_admin_settings, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_settings"))
    tg.msg_handler(state_admin_bulk_extend, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_bulk_extend"))
    tg.msg_handler(state_admin_bulk_balance, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_bulk_balance"))
    tg.msg_handler(state_admin_bulk_text, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_bulk_text"))
    tg.msg_handler(state_admin_bulk_photo, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_bulk_photo"), content_types=["photo", "text"])
    tg.msg_handler(state_admin_user_msg_text, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_user_msg_text"))
    tg.msg_handler(state_admin_user_msg_photo, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_user_msg_photo"), content_types=["photo", "text"])

    token = storage.config.get("user_bot_token", "")
    if token:
        try:
            _start_user_bot(cardinal, None)
        except Exception:
            logger.exception("Не удалось автоматически запустить user-бота")


def _admin_main_keyboard() -> K:
    kb = K()
    kb.row_width = 2
    maintenance = storage.config.get("maintenance")
    maintenance_label = f"{'🟢' if maintenance else '🔴'} Тех. работы: {'Вкл' if maintenance else 'Выкл'}"
    kb.add(
        B("🔑 Токен user-бота", callback_data=f"{CB_PREFIX}admin:user_token", style="primary"),
        B("🔄 Перезапустить user-бота", callback_data=f"{CB_PREFIX}admin:start_bot", style="primary"),
        B("📢 Канал подписки", callback_data=f"{CB_PREFIX}admin:channel", style="success"),
        B("🆘 Админ поддержки", callback_data=f"{CB_PREFIX}admin:support", style="success"),
        B("🔐 Токен Crypto Bot", callback_data=f"{CB_PREFIX}admin:crypto_token", style="primary"),
        B("🖼 Приветственное медиа", callback_data=f"{CB_PREFIX}admin:welcome_media", style="success"),
        B("🖼 Медиа меню", callback_data=f"{CB_PREFIX}admin:menu_media", style="success"),
        B("📖 Текст FAQ", callback_data=f"{CB_PREFIX}admin:faq", style="success"),
        B("🖥 Серверы", callback_data=f"{CB_PREFIX}admin:hosts", style="primary"),
        B("📋 Планы и цены", callback_data=f"{CB_PREFIX}admin:plans", style="success"),
        B("🎟 Промокоды", callback_data=f"{CB_PREFIX}admin:promos", style="success"),
        B("🎁 Подарочные коды", callback_data=f"{CB_PREFIX}admin:codes", style="success"),
        B("🎁 Выдать подписку", callback_data=f"{CB_PREFIX}admin:give", style="success"),
        B("💳 Баланс пользователя", callback_data=f"{CB_PREFIX}admin:balance", style="primary"),
        B("👤 Пользователи", callback_data=f"{CB_PREFIX}admin:users", style="success"),
        B("🚫 Баны", callback_data=f"{CB_PREFIX}admin:bans", style="danger"),
        B("📄 Подписки", callback_data=f"{CB_PREFIX}admin:subs", style="success"),
        B("📊 Источники", callback_data=f"{CB_PREFIX}admin:sources", style="success"),
        B("💸 Выводы", callback_data=f"{CB_PREFIX}admin:withdrawals", style="primary"),
        B("🔒 Безопасность и логи", callback_data=f"{CB_PREFIX}admin:security", style="primary"),
        B("🔍 Поиск", callback_data=f"{CB_PREFIX}admin:search", style="success"),
        B("📡 Массовые операции", callback_data=f"{CB_PREFIX}admin:bulk", style="primary"),
        B("📤 Экспорт", callback_data=f"{CB_PREFIX}admin:export", style="success"),
        B("📈 Статистика", callback_data=f"{CB_PREFIX}admin:stats", style="success"),
        B("⚙️ Настройки", callback_data=f"{CB_PREFIX}admin:settings", style="primary"),
        B("🔔 Уведомления админу", callback_data=f"{CB_PREFIX}admin:notifications", style="success"),
        B("📝 Жалобы", callback_data=f"{CB_PREFIX}admin:complaints", style="danger"),
        B("🔄 Синхронизация Xray", callback_data=f"{CB_PREFIX}admin:sync_xray", style="primary"),
        B("⏳ Временные профили", callback_data=f"{CB_PREFIX}admin:temp", style="success"),
        B(maintenance_label, callback_data=f"{CB_PREFIX}admin:maintenance", style="danger" if not maintenance else "success"),
    )
    return kb


def _admin_plans_keyboard(action: str) -> K:
    kb = K()
    for pid, plan in storage.plans().items():
        if pid == "trial":
            continue
        kb.add(B(f"📋 {plan.name}", callback_data=f"{CB_PREFIX}{action}:{pid}"))
    return kb


def _admin_durations_keyboard(plan_id: str, action: str) -> K:
    plan = storage.plan(plan_id)
    kb = K()
    for months in DURATIONS:
        kb.add(B(f"🗓 {months} мес. — {_price_text(plan, months)}", callback_data=f"{CB_PREFIX}{action}:{plan_id}:{months}"))
    return kb


def _admin_user_card(target_uid: int) -> tuple[str, K]:
    user = storage.get_user(target_uid)
    if not user:
        return "Пользователь не найден.", K().add(B("Назад", callback_data=f"{CB_PREFIX}admin:main"))
    level1, level2 = storage.get_user_referrals(target_uid)
    subs = storage.get_user_subscriptions(target_uid)
    earnings = storage.referrals.get("earnings", {}).get(str(target_uid), {"level1": Decimal("0.00"), "level2": Decimal("0.00")})
    total_earn = _to_dec(earnings.get("level1", 0)) + _to_dec(earnings.get("level2", 0))
    lines = [
        f"<b>Пользователь {target_uid}</b>",
        f"@{_escape(user.get('username') or '—')}",
        f"Источник: {_escape(user.get('source', 'direct'))}",
        f"Баланс: {_money_str(user.get('balance', 0))}₽",
        f"Реферальный баланс: {_money_str(user.get('referral_balance', 0))}₽",
        f"Рефералы: {len(level1)} / {len(level2)} — заработок {_money_str(total_earn)}₽",
        f"Всего потрачено: {_money_str(user.get('total_spent', 0))}₽ (месяцев: {user.get('total_months', 0)})",
        f"Бан: {'да' if user.get('is_banned') else 'нет'}",
        f"Подписок: {len(subs)}",
    ]
    for s in subs:
        sub = storage._sub_from_dict(s)
        plan = storage.plan(s.get("plan_id"))
        status = "Активна" if s.get("active") and not sub.is_expired else "Истекла"
        lines.append(f"  #{s.get('sub_id')} {_escape(plan.name if plan else s.get('plan_id'))} — {status} до {_format_time(s.get('expires_at', 0))}")
    kb = K()
    for s in subs:
        kb.add(B(f"Подписка #{s.get('sub_id')}", callback_data=f"{CB_PREFIX}admin_sub:{s.get('sub_id')}"))
    ban_label = "🟢 Разблокировать" if user.get("is_banned") else "🔴 Заблокировать"
    kb.add(B(ban_label, callback_data=f"{CB_PREFIX}admin_ban:{target_uid}"))
    kb.add(B("💳 История платежей", callback_data=f"{CB_PREFIX}admin_user_payments:{target_uid}"))
    kb.add(B("✉️ Написать пользователю", callback_data=f"{CB_PREFIX}admin_user_msg:{target_uid}"))
    kb.add(B("◀️ Назад", callback_data=f"{CB_PREFIX}admin:main"))
    return "\n".join(lines), kb


def _admin_bulk_filter_keyboard(op: str) -> K:
    kb = K()
    for key, label in [("all", "🌍 Всем"), ("active", "✅ С активной подпиской"), ("expired", "❌ С истёкшей"), ("source", "🏷 По источнику")]:
        kb.add(B(label, callback_data=f"{CB_PREFIX}admin_bulk_filter:{op}:{key}"))
    kb.add(B("◀️ Назад", callback_data=f"{CB_PREFIX}admin:bulk"))
    return kb


def _handle_admin_callback(cardinal: Cardinal, c: CallbackQuery) -> None:
    tg = cardinal.telegram
    bot = tg.bot
    chat_id = c.message.chat.id
    user_id = c.from_user.id
    data = c.data[len(CB_PREFIX):]
    parts = data.split(":")
    action = parts[0]
    args = parts[1:]
    section = args[0] if action == "admin" and args else ""

    if not is_admin(cardinal, user_id):
        bot.send_message(chat_id, "Нет доступа.")
        return

    if action == "admin" and (not args or args[0] == "main"):
        bot.edit_message_text("<b>🛡 VPN Admin</b>\n\nВыберите раздел:", chat_id, c.message.message_id, reply_markup=_admin_main_keyboard())
        return

    if action == "admin":
        section = args[0]

        if section == "user_token":
            text = f"<b>Токен user-бота</b>\nТекущий: <code>{storage.config.get('user_bot_token') or 'не задан'}</code>\n\nОтправьте новый токен:"
            kb = K().add(B("Назад", callback_data=f"{CB_PREFIX}admin:main"))
            bot.edit_message_text(text, chat_id, c.message.message_id, reply_markup=kb)
            tg.set_state(chat_id, c.message.message_id, user_id, f"{CB_PREFIX}set_user_token")
            return

        if section == "start_bot":
            _start_user_bot(cardinal, chat_id)
            return

        if section == "channel":
            text = (f"<b>Канал подписки</b>\nТекущий: <code>{storage.config.get('channel_id') or 'не задан'}</code>\n\n"
                    f"Отправьте ID канала (@channel или -100...) или 0 чтобы отключить:")
            kb = K().add(B("Назад", callback_data=f"{CB_PREFIX}admin:main"))
            bot.edit_message_text(text, chat_id, c.message.message_id, reply_markup=kb)
            tg.set_state(chat_id, c.message.message_id, user_id, f"{CB_PREFIX}set_channel")
            return

        if section == "support":
            text = (f"<b>Админ поддержки</b>\nТекущий: <code>{storage.config.get('support_id') or 'не задан'}</code>\n\n"
                    f"Отправьте username (@admin) или user_id:")
            kb = K().add(B("Назад", callback_data=f"{CB_PREFIX}admin:main"))
            bot.edit_message_text(text, chat_id, c.message.message_id, reply_markup=kb)
            tg.set_state(chat_id, c.message.message_id, user_id, f"{CB_PREFIX}set_support")
            return

        if section == "crypto_token":
            text = f"<b>Токен Crypto Bot</b>\nТекущий: <code>{storage.config.get('crypto_bot_token') or 'не задан'}</code>\n\nОтправьте новый токен:"
            kb = K().add(B("Назад", callback_data=f"{CB_PREFIX}admin:main"))
            bot.edit_message_text(text, chat_id, c.message.message_id, reply_markup=kb)
            tg.set_state(chat_id, c.message.message_id, user_id, f"{CB_PREFIX}set_crypto_token")
            return

        if section == "welcome_media":
            file_id = storage.config.get("welcome_media_file_id")
            media_type = storage.config.get("welcome_media_type", "не задано")
            text = (f"<b>Приветственное медиа</b>\n"
                    f"Текущее: {media_type if file_id else 'не задано'}\n\n"
                    f"Отправьте фото, GIF или видео, которое будет показываться при первом /start.")
            kb = K()
            if file_id:
                kb.add(B("🗑 Удалить медиа", callback_data=f"{CB_PREFIX}admin:del_welcome_media", style="danger"))
            kb.add(B("◀️ Назад", callback_data=f"{CB_PREFIX}admin:main", style="primary"))
            bot.edit_message_text(text, chat_id, c.message.message_id, reply_markup=kb)
            tg.set_state(chat_id, c.message.message_id, user_id, f"{CB_PREFIX}set_welcome_media")
            return

        if section == "menu_media":
            file_id = storage.config.get("menu_media_file_id")
            media_type = storage.config.get("menu_media_type", "не задано")
            text = (f"<b>Медиа меню</b>\n"
                    f"Текущее: {media_type if file_id else 'не задано'}\n\n"
                    f"Отправьте фото, GIF или видео, которое будет показываться в меню бота (приветствие, проверка канала и т.д.).")
            kb = K()
            if file_id:
                kb.add(B("🗑 Удалить медиа", callback_data=f"{CB_PREFIX}admin:del_menu_media", style="danger"))
            kb.add(B("◀️ Назад", callback_data=f"{CB_PREFIX}admin:main", style="primary"))
            bot.edit_message_text(text, chat_id, c.message.message_id, reply_markup=kb)
            tg.set_state(chat_id, c.message.message_id, user_id, f"{CB_PREFIX}set_menu_media")
            return

        if section == "faq":
            text = (f"<b>Текст FAQ</b>\n\n"
                    f"Отправьте новый текст FAQ (поддерживается HTML-разметка):\n\n"
                    f"Текущий:\n{storage.config.get('faq_text') or 'стандартный'}")
            kb = K().add(B("Назад", callback_data=f"{CB_PREFIX}admin:main"))
            bot.edit_message_text(text, chat_id, c.message.message_id, reply_markup=kb)
            tg.set_state(chat_id, c.message.message_id, user_id, f"{CB_PREFIX}set_faq_text")
            return

        if section == "del_welcome_media":
            storage.config.pop("welcome_media_file_id", None)
            storage.config.pop("welcome_media_type", None)
            storage.save_config()
            bot.edit_message_text("Приветственное медиа удалено.", chat_id, c.message.message_id,
                                  reply_markup=_admin_main_keyboard())
            return

        if section == "del_menu_media":
            storage.config.pop("menu_media_file_id", None)
            storage.config.pop("menu_media_type", None)
            storage.save_config()
            bot.edit_message_text("Медиа меню удалено.", chat_id, c.message.message_id,
                                  reply_markup=_admin_main_keyboard())
            return

        if section == "hosts":
            hosts = storage.list_hosts()
            lines = ["<b>Серверы</b>"]
            for host in hosts:
                default = " (по умолч.)" if host.host_id == storage.default_host_id() else ""
                lines.append(f"{host.name or host.host_id}{default} — {host.address}:{host.port}")
            text = "\n".join(lines) if hosts else "Серверы не настроены."
            kb = K()
            for host in hosts:
                kb.add(B(host.name or host.host_id, callback_data=f"{CB_PREFIX}admin:host:{host.host_id}"))
            kb.add(B("Добавить сервер", callback_data=f"{CB_PREFIX}admin:host_add"))
            kb.add(B("Назад", callback_data=f"{CB_PREFIX}admin:main"))
            bot.edit_message_text(text, chat_id, c.message.message_id, reply_markup=kb)
            return

        if section == "host" and len(args) >= 2:
            host_id = args[1]
            host = storage.get_host(host_id)
            if not host:
                bot.edit_message_text("Сервер не найден.", chat_id, c.message.message_id,
                                      reply_markup=K().add(B("Назад", callback_data=f"{CB_PREFIX}admin:hosts")))
                return
            text = (f"<b>{host.name or host.host_id}</b>\n"
                    f"ID: <code>{host.host_id}</code>\n"
                    f"Адрес: <code>{host.address}</code>\n"
                    f"Порт: {host.port}\n"
                    f"publicKey: <code>{host.public_key}</code>\n"
                    f"shortId: <code>{host.short_id}</code>\n"
                    f"serverName: <code>{host.server_name}</code>\n"
                    f"flow: <code>{host.flow}</code> | fp: <code>{host.fingerprint}</code>\n"
                    f"spx: <code>{host.spider_x}</code>\n\n"
                    f"<b>3X-UI панель</b>\n"
                    f"URL: <code>{host.panel_url}</code>\n"
                    f"Inbound: {host.inbound_id} | verify SSL: {host.verify_ssl}\n"
                    f"Подписка: <code>{host.subscription_url_base or host.address}:{host.sub_port}/sub/{'...'}</code>")
            kb = K()
            kb.add(B("Изменить", callback_data=f"{CB_PREFIX}admin:host_edit:{host_id}"))
            if host_id != storage.default_host_id():
                kb.add(B("Сделать основным", callback_data=f"{CB_PREFIX}admin:host_default:{host_id}"))
            if len(storage.list_hosts()) > 1:
                kb.add(B("Удалить", callback_data=f"{CB_PREFIX}admin:host_delete:{host_id}"))
            kb.add(B("Назад", callback_data=f"{CB_PREFIX}admin:hosts"))
            bot.edit_message_text(text, chat_id, c.message.message_id, reply_markup=kb)
            return

        if section == "host_add":
            bot.edit_message_text("Отправьте ID нового сервера (латиницей, например nl1):", chat_id, c.message.message_id,
                                  reply_markup=K().add(B("Назад", callback_data=f"{CB_PREFIX}admin:hosts")))
            tg.set_state(chat_id, c.message.message_id, user_id, f"{CB_PREFIX}admin_host_add")
            return

        if section == "host_edit" and len(args) >= 2:
            host_id = args[1]
            bot.edit_message_text(
                f"Отправьте полные данные для сервера <code>{host_id}</code> одной строкой:\n"
                f"<code>host_id name address port public_key short_id server_name [panel_url username password inbound_id verify_ssl sub_port subscription_url_base]</code>",
                chat_id, c.message.message_id,
                reply_markup=K().add(B("Назад", callback_data=f"{CB_PREFIX}admin:hosts")))
            tg.set_state(chat_id, c.message.message_id, user_id, f"{CB_PREFIX}admin_host_edit", {"host_id": host_id})
            return

        if section == "host_default" and len(args) >= 2:
            storage.set_default_host(args[1])
            bot.edit_message_text("Основной сервер обновлён.", chat_id, c.message.message_id,
                                  reply_markup=_admin_main_keyboard())
            return

        if section == "host_delete" and len(args) >= 2:
            if storage.delete_host(args[1]):
                bot.edit_message_text("Сервер удалён.", chat_id, c.message.message_id,
                                      reply_markup=_admin_main_keyboard())
            else:
                bot.edit_message_text("Нельзя удалить единственный сервер или сервер не найден.", chat_id, c.message.message_id,
                                      reply_markup=K().add(B("Назад", callback_data=f"{CB_PREFIX}admin:hosts")))
            return

        if section == "plans":
            lines = ["<b>Текущие планы</b>"]
            for pid, plan in storage.plans().items():
                prices = ", ".join(f"{m}м:{plan.prices[m]}" for m in DURATIONS if m in plan.prices)
                host = storage.get_host(plan.host_id)
                host_name = host.name if host else plan.host_id
                lines.append(f"{plan.name} ({pid}) — {plan.device_text} — {prices} — сервер {host_name}")
            text = "\n".join(lines)
            kb = K()
            for pid in storage.plans():
                if pid == "trial":
                    continue
                kb.add(B(storage.plan(pid).name, callback_data=f"{CB_PREFIX}admin:plan:{pid}"))
            kb.add(B("Добавить тариф", callback_data=f"{CB_PREFIX}admin:plan_add"))
            kb.add(B("Назад", callback_data=f"{CB_PREFIX}admin:main"))
            bot.edit_message_text(text, chat_id, c.message.message_id, reply_markup=kb)
            return

        if section == "plan" and len(args) >= 2:
            plan_id = args[1]
            plan = storage.plan(plan_id)
            if not plan:
                bot.edit_message_text("Тариф не найден.", chat_id, c.message.message_id,
                                      reply_markup=K().add(B("Назад", callback_data=f"{CB_PREFIX}admin:plans")))
                return
            prices = ", ".join(f"{m}м:{plan.prices.get(m, '?')}" for m in DURATIONS)
            host = storage.get_host(plan.host_id)
            host_name = host.name if host else plan.host_id
            text = (f"<b>Тариф {plan.name} ({plan_id})</b>\n"
                    f"Устройств: {plan.device_text}\n"
                    f"Сервер: {host_name}\n"
                    f"Цены: {prices}")
            kb = K()
            kb.add(B("Изменить название", callback_data=f"{CB_PREFIX}admin:plan_name:{plan_id}"))
            kb.add(B("Изменить устройства", callback_data=f"{CB_PREFIX}admin:plan_devices:{plan_id}"))
            kb.add(B("Сменить сервер", callback_data=f"{CB_PREFIX}admin:plan_host:{plan_id}"))
            for m in DURATIONS:
                kb.add(B(f"Цена {m} мес.", callback_data=f"{CB_PREFIX}admin:plan_price:{plan_id}:{m}"))
            kb.add(B("Удалить", callback_data=f"{CB_PREFIX}admin:plan_delete:{plan_id}"))
            kb.add(B("Назад", callback_data=f"{CB_PREFIX}admin:plans"))
            bot.edit_message_text(text, chat_id, c.message.message_id, reply_markup=kb)
            return

        if section == "plan_name" and len(args) >= 2:
            plan_id = args[1]
            tg.set_state(chat_id, c.message.message_id, user_id, f"{CB_PREFIX}admin_plan_name", {"plan_id": plan_id})
            bot.edit_message_text("Отправьте новое название тарифа:", chat_id, c.message.message_id,
                                  reply_markup=K().add(B("Назад", callback_data=f"{CB_PREFIX}admin:plan:{plan_id}")))
            return

        if section == "plan_devices" and len(args) >= 2:
            plan_id = args[1]
            tg.set_state(chat_id, c.message.message_id, user_id, f"{CB_PREFIX}admin_plan_devices", {"plan_id": plan_id})
            bot.edit_message_text("Отправьте лимит устройств (число, -1 для безлимита):", chat_id, c.message.message_id,
                                  reply_markup=K().add(B("Назад", callback_data=f"{CB_PREFIX}admin:plan:{plan_id}")))
            return

        if section == "plan_price" and len(args) >= 3:
            plan_id, months = args[1], args[2]
            tg.set_state(chat_id, c.message.message_id, user_id, f"{CB_PREFIX}admin_plan_price", {"plan_id": plan_id, "months": months})
            bot.edit_message_text(f"Отправьте цену для {months} мес.:", chat_id, c.message.message_id,
                                  reply_markup=K().add(B("Назад", callback_data=f"{CB_PREFIX}admin:plan:{plan_id}")))
            return

        if section == "plan_host" and len(args) >= 2:
            plan_id = args[1]
            hosts = ", ".join(h.host_id for h in storage.list_hosts()) or "main"
            tg.set_state(chat_id, c.message.message_id, user_id, f"{CB_PREFIX}admin_plan_host", {"plan_id": plan_id})
            bot.edit_message_text(f"Отправьте host_id сервера ({hosts}):", chat_id, c.message.message_id,
                                  reply_markup=K().add(B("Назад", callback_data=f"{CB_PREFIX}admin:plan:{plan_id}")))
            return

        if section == "plan_delete" and len(args) >= 2:
            plan_id = args[1]
            storage.delete_plan(plan_id)
            bot.edit_message_text(f"Тариф {plan_id} удалён.", chat_id, c.message.message_id,
                                  reply_markup=_admin_main_keyboard())
            return

        if section == "plan_add":
            tg.set_state(chat_id, c.message.message_id, user_id, f"{CB_PREFIX}admin_plan_add_id", {})
            bot.edit_message_text("Отправьте ID нового тарифа (латиницей, например premium):", chat_id, c.message.message_id,
                                  reply_markup=K().add(B("Назад", callback_data=f"{CB_PREFIX}admin:plans")))
            return

        if section == "promos":
            promos = storage.config.get("promocodes", {})
            lines = ["<b>Промокоды</b>"]
            for code, p in promos.items():
                d_type = "процент" if p["discount_type"] == "percent" else "фикс"
                plan = f" ({p['plan_id']})" if p.get("plan_id") else ""
                exp = f", истекает {_format_time(p['expires_at'])}" if p.get("expires_at") else ""
                lines.append(f"{code}: {d_type} {p['value']}, исп. {p.get('uses',0)}/{p.get('max_uses',0)}{plan}{exp}")
            if len(lines) == 1:
                lines.append("Нет промокодов.")
            text = "\n".join(lines) + "\n\nФормат добавления:\n<code>КОД процент|фикс ЗНАЧЕНИЕ MAX_USES [PLAN_ID] [ЧАСЫ]</code>"
            kb = K()
            kb.add(B("Добавить", callback_data=f"{CB_PREFIX}admin:promo_add"))
            kb.add(B("Назад", callback_data=f"{CB_PREFIX}admin:main"))
            bot.edit_message_text(text, chat_id, c.message.message_id, reply_markup=kb)
            return

        if section == "promo_add":
            bot.edit_message_text("Отправьте промокод одной строкой:\n<code>КОД процент|фикс ЗНАЧЕНИЕ MAX_USES [PLAN_ID] [ЧАСЫ]</code>",
                                  chat_id, c.message.message_id,
                                  reply_markup=K().add(B("Назад", callback_data=f"{CB_PREFIX}admin:promos")))
            tg.set_state(chat_id, c.message.message_id, user_id, f"{CB_PREFIX}admin_promo_add")
            return

        if section == "codes":
            codes = storage.config.get("activation_codes", {})
            lines = ["<b>Подарочные/партнёрские коды</b>"]
            for code, ac in codes.items():
                ref = f", ref {ac.get('referrer_id')}" if ac.get('referrer_id') else ""
                lines.append(f"{code}: {ac['plan_id']} {ac['months']}мес., осталось {ac.get('uses',0)}{ref}")
            if len(lines) == 1:
                lines.append("Нет кодов.")
            text = "\n".join(lines) + "\n\nФормат добавления:\n<code>КОД PLAN_ID МЕСЯЦЫ USES [REFERER_ID]</code>"
            kb = K()
            kb.add(B("Добавить", callback_data=f"{CB_PREFIX}admin:code_add"))
            kb.add(B("Назад", callback_data=f"{CB_PREFIX}admin:main"))
            bot.edit_message_text(text, chat_id, c.message.message_id, reply_markup=kb)
            return

        if section == "code_add":
            bot.edit_message_text("Отправьте код активации одной строкой:\n<code>КОД PLAN_ID МЕСЯЦЫ USES [REFERER_ID]</code>",
                                  chat_id, c.message.message_id,
                                  reply_markup=K().add(B("Назад", callback_data=f"{CB_PREFIX}admin:codes")))
            tg.set_state(chat_id, c.message.message_id, user_id, f"{CB_PREFIX}admin_code_add")
            return

        if section == "give":
            bot.send_message(chat_id, "Введите Telegram user_id получателя:")
            tg.set_state(chat_id, c.message.message_id, user_id, f"{CB_PREFIX}admin_give_uid")
            return

        if section == "balance":
            bot.send_message(chat_id, "Введите Telegram user_id пользователя:")
            tg.set_state(chat_id, c.message.message_id, user_id, f"{CB_PREFIX}admin_balance_uid")
            return

        if section == "users":
            users = list(storage.users.values())
            if not users:
                text = "Пользователей пока нет."
            else:
                lines = [f"Всего: {len(users)}"]
                for u in users[:20]:
                    lines.append(f"{u['user_id']} — @{u['username'] or '?'} — баланс {_money_str(u['balance'])}₽")
                text = "\n".join(lines)
            kb = K().add(B("Назад", callback_data=f"{CB_PREFIX}admin:main"))
            bot.edit_message_text(text, chat_id, c.message.message_id, reply_markup=kb)
            return

        if section == "subs":
            subs = list(storage.subscriptions["subs"].values())
            if not subs:
                text = "Подписок пока нет."
            else:
                lines = [f"Всего: {len(subs)}"]
                for s in subs[:20]:
                    plan = storage.plan(s["plan_id"])
                    sub_obj = storage._sub_from_dict(s)
                    status = "Активна" if s.get("active") and not sub_obj.is_expired else "Истекла"
                    lines.append(f"#{s['sub_id']} — user {s['user_id']} — {_escape(plan.name if plan else s['plan_id'])} — {status}")
                text = "\n".join(lines)
            kb = K()
            for s in subs[:20]:
                kb.add(B(f"#{s['sub_id']}", callback_data=f"{CB_PREFIX}admin_sub:{s['sub_id']}"))
            kb.add(B("Назад", callback_data=f"{CB_PREFIX}admin:main"))
            bot.edit_message_text(text, chat_id, c.message.message_id, reply_markup=kb)
            return

        if section == "sources":
            stats = storage.source_stats()
            total = len(storage.users)
            direct = stats.pop("direct", 0)
            lines = [f"Всего пользователей: {total}", f"Без приписки (direct): {direct}"]
            for src, count in sorted(stats.items(), key=lambda x: -x[1]):
                lines.append(f"{_escape(src)}: {count}")
            if not lines[1:]:
                lines.append("Источников пока нет.")
            text = "\n".join(lines)
            kb = K().add(B("Назад", callback_data=f"{CB_PREFIX}admin:main"))
            bot.edit_message_text(text, chat_id, c.message.message_id, reply_markup=kb)
            return

        if section == "withdrawals":
            reqs = storage.withdrawal_requests(status="pending")
            lines = ["<b>Заявки на вывод</b>"]
            for r in reqs:
                u = storage.get_user(r["user_id"])
                uname = _escape(u.get("username") or "?")
                lines.append(f"#{r['id']} — user {r['user_id']} (@{uname}) — {_money_str(r['amount'])}₽ — {_escape(r.get('card',''))}")
            if len(lines) == 1:
                lines.append("Нет заявок.")
            kb = K()
            for r in reqs:
                kb.add(B(f"#{r['id']} {_money_str(r['amount'])}₽", callback_data=f"{CB_PREFIX}admin_withdraw:{r['id']}"))
            kb.add(B("Назад", callback_data=f"{CB_PREFIX}admin:main"))
            bot.edit_message_text("\n".join(lines), chat_id, c.message.message_id, reply_markup=kb)
            return

        if section == "security":
            sec = storage.security()
            text = (f"<b>Безопасность</b>\n\n"
                    f"Окно анти-шаринга: {sec.get('sharing_window', DEFAULT_SHARING_WINDOW)} сек\n"
                    f"Кулдаун отвязки: {sec.get('unbind_cooldown', DEFAULT_UNBIND_COOLDOWN) // 86400} дней\n"
                    f"Лимит трафика: {sec.get('traffic_limit_gb', 0.0) or 'нет'} ГБ\n"
                    f"Уведомления админу: {'Вкл' if sec.get('alert_admin', True) else 'Выкл'}")
            kb = K()
            kb.add(B("Окно шаринга", callback_data=f"{CB_PREFIX}admin_security:window"))
            kb.add(B("Кулдаун отвязки (дней)", callback_data=f"{CB_PREFIX}admin_security:cooldown"))
            kb.add(B("Лимит трафика (ГБ)", callback_data=f"{CB_PREFIX}admin_security:traffic"))
            kb.add(B("Уведомления", callback_data=f"{CB_PREFIX}admin_security:alert"))
            kb.add(B("Логи подключений", callback_data=f"{CB_PREFIX}admin_security:logs"))
            kb.add(B("Подозрительные события", callback_data=f"{CB_PREFIX}admin_security:suspicious"))
            kb.add(B("Назад", callback_data=f"{CB_PREFIX}admin:main"))
            bot.edit_message_text(text, chat_id, c.message.message_id, reply_markup=kb)
            return

        if section == "search":
            bot.send_message(chat_id, "Введите @username, user_id или #номер подписки:")
            tg.set_state(chat_id, c.message.message_id, user_id, f"{CB_PREFIX}admin_search_query")
            return

        if section == "bulk":
            text = "<b>Массовые операции</b>"
            kb = K()
            kb.add(B("Рассылка", callback_data=f"{CB_PREFIX}admin_bulk:broadcast"))
            kb.add(B("Продлить все активные", callback_data=f"{CB_PREFIX}admin_bulk:extend"))
            kb.add(B("Начислить баланс всем", callback_data=f"{CB_PREFIX}admin_bulk:balance"))
            kb.add(B("Назад", callback_data=f"{CB_PREFIX}admin:main"))
            bot.edit_message_text(text, chat_id, c.message.message_id, reply_markup=kb)
            return

        if section == "export":
            text = "<b>Экспорт CSV</b>\nВыберите файл:"
            kb = K()
            kb.add(B("Пользователи", callback_data=f"{CB_PREFIX}admin_export:users"))
            kb.add(B("Подписки", callback_data=f"{CB_PREFIX}admin_export:subscriptions"))
            kb.add(B("Транзакции", callback_data=f"{CB_PREFIX}admin_export:transactions"))
            kb.add(B("Подключения", callback_data=f"{CB_PREFIX}admin_export:connections"))
            kb.add(B("Жалобы", callback_data=f"{CB_PREFIX}admin_export:complaints"))
            kb.add(B("Назад", callback_data=f"{CB_PREFIX}admin:main"))
            bot.edit_message_text(text, chat_id, c.message.message_id, reply_markup=kb)
            return

        if section == "notifications":
            notif = storage.config.get("admin_notifications", {})
            text = "<b>Уведомления админу</b>"
            kb = K()
            for key, label in [("new_user", "Новый пользователь"), ("new_payment", "Новый платёж"),
                               ("expiring_sub", "Истекает подписка"), ("complaint", "Жалоба")]:
                status = "Вкл" if notif.get(key, True) else "Выкл"
                kb.add(B(f"{label}: {status}", callback_data=f"{CB_PREFIX}admin_notif:{key}"))
            kb.add(B("Назад", callback_data=f"{CB_PREFIX}admin:main"))
            bot.edit_message_text(text, chat_id, c.message.message_id, reply_markup=kb)
            return

        if section == "complaints":
            complaints = storage.config.get("complaints", [])
            if not complaints:
                text = "Жалоб пока нет."
            else:
                lines = ["<b>Жалобы</b>"]
                for comp in complaints[-20:]:
                    status = "✅" if comp.get("status") == "closed" else "🆕"
                    lines.append(f"{status} #{comp.get('id')} user {comp.get('user_id')} ({_format_time(comp.get('created_at'))}): {_escape(comp.get('text', '')[:80])}")
                text = "\n".join(lines)
            kb = K()
            for comp in complaints[-20:]:
                if comp.get("status") != "closed":
                    kb.add(B(f"Закрыть #{comp.get('id')}", callback_data=f"{CB_PREFIX}admin_close_complaint:{comp.get('id')}"))
            kb.add(B("Назад", callback_data=f"{CB_PREFIX}admin:main"))
            bot.edit_message_text(text, chat_id, c.message.message_id, reply_markup=kb)
            return

        if section == "sync_xray":
            text = "<b>Синхронизация подписок с 3X-UI</b>\n\nПроверка сверяет JSON-хранилище и клиентов в 3X-UI, исправление добавляет/обновляет недостающих."
            kb = K()
            kb.add(B("Проверить", callback_data=f"{CB_PREFIX}admin:sync_check"))
            kb.add(B("Исправить", callback_data=f"{CB_PREFIX}admin:sync_fix"))
            kb.add(B("Назад", callback_data=f"{CB_PREFIX}admin:main"))
            bot.edit_message_text(text, chat_id, c.message.message_id, reply_markup=kb)
            return

        if section == "sync_check":
            result = XrayAPI.sync_subscriptions()
            lines = ["<b>Проверка синхронизации</b>"]
            if "error" in result:
                lines.append(result["error"])
            else:
                lines.append(f"OK: {len(result['ok'])}")
                lines.append(f"Несовпадение: {len(result['mismatch'])}")
                for m in result["mismatch"][:5]:
                    lines.append(f"  #{m['sub_id']} {m['email']}: xray={m['xray']} expected={m['expected']}")
                lines.append(f"Отсутствуют в 3X-UI: {len(result['missing'])}")
                for m in result["missing"][:5]:
                    lines.append(f"  #{m['sub_id']} {m['email']}")
                lines.append(f"Лишние в 3X-UI: {len(result['orphan'])}")
                for o in result["orphan"][:5]:
                    lines.append(f"  {o['email']}")
            text = "\n".join(lines)[:3500]
            bot.edit_message_text(text, chat_id, c.message.message_id,
                                  reply_markup=K().add(B("Назад", callback_data=f"{CB_PREFIX}admin:sync_xray")))
            return

        if section == "sync_fix":
            result = XrayAPI.fix_subscriptions()
            text = (f"<b>Исправление синхронизации</b>\n\n"
                    f"Добавлено: {result['added']}\n"
                    f"Обновлено: {result['updated']}\n"
                    f"Ошибок: {result['failed']}")
            bot.edit_message_text(text, chat_id, c.message.message_id,
                                  reply_markup=K().add(B("Назад", callback_data=f"{CB_PREFIX}admin:sync_xray")))
            return

        if section == "temp":
            srv = storage.server()
            url = f"http://{srv.address}:{srv.temp_profile_port}/" if srv.temp_profile_enabled else "—"
            text = (f"<b>Временные профили (30 мин)</b>\n\n"
                    f"Статус: {'Вкл' if srv.temp_profile_enabled else 'Выкл'}\n"
                    f"Порт: {srv.temp_profile_port}\n"
                    f"Inbound: {srv.temp_inbound_id or 'основной'}\n"
                    f"URL: <code>{url}</code>\n\n"
                    f"После изменения перезапустите user-бота.")
            kb = K()
            toggle = "Выкл" if srv.temp_profile_enabled else "Вкл"
            kb.add(B(toggle, callback_data=f"{CB_PREFIX}admin:temp_toggle"))
            kb.add(B("Изменить", callback_data=f"{CB_PREFIX}admin:temp_edit"))
            kb.add(B("Назад", callback_data=f"{CB_PREFIX}admin:main"))
            bot.edit_message_text(text, chat_id, c.message.message_id, reply_markup=kb)
            return

        if section == "temp_toggle":
            srv = storage.server()
            srv.temp_profile_enabled = not srv.temp_profile_enabled
            storage.set_server(srv)
            status = "Вкл" if srv.temp_profile_enabled else "Выкл"
            bot.edit_message_text(f"Временные профили: {status}. Перезапустите user-бота.", chat_id, c.message.message_id,
                                  reply_markup=K().add(B("Назад", callback_data=f"{CB_PREFIX}admin:temp")))
            return

        if section == "temp_edit":
            srv = storage.server()
            text = (f"<b>Настройки временных профилей</b>\n\n"
                    f"Текущие: <code>{'true' if srv.temp_profile_enabled else 'false'} {srv.temp_profile_port} {srv.temp_inbound_id or 0}</code>\n\n"
                    f"Отправьте: <code>включено(true/false) порт inbound_id</code>\n"
                    f"0 в inbound_id означает использование основного inbound.")
            kb = K().add(B("Назад", callback_data=f"{CB_PREFIX}admin:temp"))
            bot.edit_message_text(text, chat_id, c.message.message_id, reply_markup=kb)
            tg.set_state(chat_id, c.message.message_id, user_id, f"{CB_PREFIX}set_temp_profiles")
            return

        if section == "maintenance":
            storage.config["maintenance"] = not storage.config.get("maintenance", False)
            storage.save_config()
            status = "Вкл" if storage.config.get("maintenance") else "Выкл"
            bot.edit_message_text(f"Режим технических работ: {status}", chat_id, c.message.message_id,
                                  reply_markup=_admin_main_keyboard())
            return

    if action == "admin_security" and len(args) >= 1:
        param = args[0]
        if param == "logs":
            logs = storage.connection_logs(limit=20)
            if not logs:
                text = "Логов подключений пока нет."
            else:
                lines = ["<b>Последние подключения</b>"]
                for l in logs:
                    lines.append(f"#{l.get('sub_id')} {_escape(l.get('ip'))} — {_escape(l.get('reason'))} — {'разрешено' if l.get('allowed') else 'запрещено'} ({_format_time(l.get('timestamp'))})")
                text = "\n".join(lines)
            bot.edit_message_text(text, chat_id, c.message.message_id,
                                  reply_markup=K().add(B("Назад", callback_data=f"{CB_PREFIX}admin:security")))
            return
        if param == "suspicious":
            evts = storage.connections.get("suspicious", [])[-20:]
            if not evts:
                text = "Подозрительных событий пока нет."
            else:
                lines = ["<b>Подозрительные события</b>"]
                for e in evts:
                    lines.append(f"user {e.get('user_id')} sub #{e.get('sub_id')} IP {_escape(e.get('ip'))} — {_escape(e.get('reason'))} ({_format_time(e.get('timestamp'))})")
                text = "\n".join(lines)
            bot.edit_message_text(text, chat_id, c.message.message_id,
                                  reply_markup=K().add(B("Назад", callback_data=f"{CB_PREFIX}admin:security")))
            return
        prompts = {
            "window": ("Введите окно анти-шаринга в секундах:", f"{CB_PREFIX}admin_security_window"),
            "cooldown": ("Введите кулдаун отвязки устройства в днях:", f"{CB_PREFIX}admin_security_cooldown"),
            "traffic": ("Введите лимит трафика в ГБ (0 — без ограничения):", f"{CB_PREFIX}admin_security_traffic"),
        }
        if param in prompts:
            text, state_key = prompts[param]
            bot.send_message(chat_id, text)
            tg.set_state(chat_id, c.message.message_id, user_id, state_key)
            return
        if param == "alert":
            sec = storage.config.setdefault("security", {})
            sec["alert_admin"] = not sec.get("alert_admin", True)
            storage.save_config()
            bot.edit_message_text(f"Уведомления админу: {'Вкл' if sec['alert_admin'] else 'Выкл'}", chat_id, c.message.message_id,
                                  reply_markup=K().add(B("Назад", callback_data=f"{CB_PREFIX}admin:security")))
            return

    if action == "admin_withdraw" and len(args) >= 1:
        req_id = args[0]
        req = storage.get_withdrawal_request(req_id)
        if not req:
            bot.send_message(chat_id, "Заявка не найдена.")
            return
        if req.get("status") != "pending":
            bot.edit_message_text(f"Заявка #{req_id} уже обработана: {req['status']}.", chat_id, c.message.message_id,
                                  reply_markup=K().add(B("Назад", callback_data=f"{CB_PREFIX}admin:withdrawals")))
            return
        text = (f"<b>Заявка на вывод #{req_id}</b>\n\n"
                f"Пользователь: {req['user_id']}\n"
                f"Сумма: {_money_str(req['amount'])}₽\n"
                f"Карта/реквизиты: {_escape(req.get('card',''))}")
        kb = K()
        kb.add(B("Подтвердить", callback_data=f"{CB_PREFIX}admin_withdraw_approve:{req_id}"))
        kb.add(B("Отклонить", callback_data=f"{CB_PREFIX}admin_withdraw_reject:{req_id}"))
        kb.add(B("Назад", callback_data=f"{CB_PREFIX}admin:withdrawals"))
        bot.edit_message_text(text, chat_id, c.message.message_id, reply_markup=kb)
        return

    if action == "admin_withdraw_approve" and len(args) >= 1:
        req_id = args[0]
        req = storage.get_withdrawal_request(req_id)
        if not req or req.get("status") != "pending":
            bot.send_message(chat_id, "Заявка не найдена или уже обработана.")
            return
        u = storage.get_user(req["user_id"])
        req_amount = _to_dec(req["amount"])
        if _to_dec(u.get("referral_balance", 0)) < req_amount:
            bot.send_message(chat_id, "У пользователя недостаточно реферальных средств.")
            return
        u["referral_balance"] = _money_round(_to_dec(u["referral_balance"]) - req_amount)
        storage.update_user(u)
        storage._add_transaction(req["user_id"], -req_amount, "withdrawal", "card", f"req:{req_id}")
        storage.update_withdrawal_request(req_id, "approved")
        bot.edit_message_text(f"Заявка #{req_id} на {_money_str(req_amount)}₽ подтверждена.", chat_id, c.message.message_id,
                              reply_markup=_admin_main_keyboard())
        if _user_bot_instance:
            try:
                _user_bot_instance.bot.send_message(req["user_id"], f"Заявка #{req_id} на вывод {_money_str(req_amount)}₽ подтверждена. Средства отправлены на {_escape(req.get('card',''))}.")
            except Exception:
                pass
        return

    if action == "admin_withdraw_reject" and len(args) >= 1:
        req_id = args[0]
        req = storage.get_withdrawal_request(req_id)
        if not req or req.get("status") != "pending":
            bot.send_message(chat_id, "Заявка не найдена или уже обработана.")
            return
        storage.update_withdrawal_request(req_id, "rejected")
        bot.edit_message_text(f"Заявка #{req_id} отклонена.", chat_id, c.message.message_id, reply_markup=_admin_main_keyboard())
        if _user_bot_instance:
            try:
                _user_bot_instance.bot.send_message(req["user_id"], f"Заявка #{req_id} на вывод {_money_str(req['amount'])}₽ отклонена.")
            except Exception:
                pass
        return

    if action == "admin_user" and len(args) >= 1:
        target_uid = int(args[0])
        text, kb = _admin_user_card(target_uid)
        bot.edit_message_text(text, chat_id, c.message.message_id, reply_markup=kb)
        return

    if action == "admin_user_payments" and len(args) >= 1:
        target_uid = int(args[0])
        txs = storage.get_transactions(target_uid)
        if not txs:
            text = "История платежей пуста."
        else:
            lines = [f"<b>История платежей user {target_uid}</b>"]
            for t in txs:
                lines.append(f"{_format_time(t.get('created_at', 0))} — {t.get('type')} {t.get('amount', 0):+.2f}₽ ({t.get('method', '')})")
            text = "\n".join(lines)
        bot.edit_message_text(text, chat_id, c.message.message_id,
                              reply_markup=K().add(B("◀️ Назад", callback_data=f"{CB_PREFIX}admin_user:{target_uid}")))
        return

    if action == "admin_user_msg" and len(args) >= 1:
        target_uid = int(args[0])
        tg.set_state(chat_id, c.message.message_id, user_id, f"{CB_PREFIX}admin_user_msg_text", {"target_uid": target_uid})
        bot.send_message(chat_id, "✏️ Введите текст сообщения для пользователя:")
        return

    if action == "admin_bulk" and len(args) >= 1:
        param = args[0]
        if param == "broadcast":
            bot.send_message(chat_id, "Выберите фильтр рассылки:",
                             reply_markup=_admin_bulk_filter_keyboard("broadcast"))
            return
        if param == "extend":
            bot.send_message(chat_id, "Введите количество дней продления для всех активных подписок:")
            tg.set_state(chat_id, c.message.message_id, user_id, f"{CB_PREFIX}admin_bulk_extend")
            return
        if param == "balance":
            bot.send_message(chat_id, "Введите сумму для начисления всем пользователям:")
            tg.set_state(chat_id, c.message.message_id, user_id, f"{CB_PREFIX}admin_bulk_balance")
            return

    if action == "admin_bulk_filter" and len(args) >= 2:
        op, filter_type = args[0], args[1]
        tg.set_state(chat_id, c.message.message_id, user_id, f"{CB_PREFIX}admin_bulk_text", {"op": op, "filter_type": filter_type})
        bot.send_message(chat_id, "Введите текст рассылки:")
        return

    if action == "admin_export" and len(args) >= 1:
        kind = args[0]
        try:
            path = storage.export_csv(kind)
            with open(path, "rb") as f:
                bot.send_document(chat_id, f, caption=f"{kind}.csv")
        except Exception as e:
            bot.send_message(chat_id, f"Ошибка экспорта: {e}")
        return

    if action == "admin_notif" and len(args) >= 1:
        key = args[0]
        notif = storage.config.setdefault("admin_notifications", {})
        notif[key] = not notif.get(key, True)
        storage.save_config()
        bot.edit_message_text(f"{key}: {'Вкл' if notif[key] else 'Выкл'}", chat_id, c.message.message_id,
                              reply_markup=K().add(B("Назад", callback_data=f"{CB_PREFIX}admin:notifications")))
        return

    if action == "admin_close_complaint" and len(args) >= 1:
        cid = args[0]
        for comp in storage.config.get("complaints", []):
            if comp.get("id") == cid:
                comp["status"] = "closed"
                break
        storage.save_config()
        bot.edit_message_text(f"Жалоба #{cid} закрыта.", chat_id, c.message.message_id,
                              reply_markup=K().add(B("Назад", callback_data=f"{CB_PREFIX}admin:complaints")))
        return

    if action == "admin_give_plan":
        plan_id = args[0]
        state = tg.get_state(chat_id, user_id)
        if not state or "uid" not in state.get("data", {}):
            bot.send_message(chat_id, "Сначала введите user_id через /vpnadmin → Выдать подписку.")
            return
        uid = state["data"]["uid"]
        tg.set_state(chat_id, c.message.message_id, user_id, f"{CB_PREFIX}admin_give_months", {"uid": uid, "plan_id": plan_id})
        bot.send_message(chat_id, "Выберите срок:", reply_markup=_admin_durations_keyboard(plan_id, "admin_give_months"))
        return

    if action == "admin_give_months":
        plan_id, months = args[0], int(args[1])
        state = tg.get_state(chat_id, user_id)
        if not state:
            bot.send_message(chat_id, "Сессия истекла.")
            return
        data = state.get("data", {})
        uid = data.get("uid")
        if not uid:
            bot.send_message(chat_id, "Сначала введите user_id через /vpnadmin → Выдать подписку.")
            return
        tg.clear_state(chat_id, user_id)
        sub = storage.create_subscription(uid, plan_id, months)
        XrayAPI.add_or_update_client(sub)
        bot.send_message(chat_id, f"Подписка #{sub.sub_id} выдана пользователю {uid}.")
        if _user_bot_instance:
            try:
                _user_bot_instance.bot.send_message(uid, f"Вам выдана подписка #{sub.sub_id}!\n{format_subscription(sub)}")
            except Exception:
                pass
        return

    if action == "admin_sub":
        sub_id = args[0]
        sub = storage.get_subscription(sub_id)
        if not sub:
            bot.send_message(chat_id, "Подписка не найдена.")
            return
        kb = K()
        kb.add(B("Добавить IP", callback_data=f"{CB_PREFIX}admin_add_ip:{sub_id}"))
        kb.add(B("Удалить IP", callback_data=f"{CB_PREFIX}admin_del_ip:{sub_id}"))
        kb.add(B("Удалить все IP", callback_data=f"{CB_PREFIX}admin_del_all_ip:{sub_id}"))
        kb.add(B("Заморозить", callback_data=f"{CB_PREFIX}admin_freeze:{sub_id}"))
        kb.add(B("Возврат", callback_data=f"{CB_PREFIX}admin_refund:{sub_id}"))
        kb.add(B("Назад", callback_data=f"{CB_PREFIX}admin:subs"))
        bot.edit_message_text(format_subscription(sub), chat_id, c.message.message_id, reply_markup=kb)
        return

    if action == "admin_freeze" and len(args) >= 1:
        sub_id = args[0]
        bot.send_message(chat_id, "Введите количество дней заморозки:")
        tg.set_state(chat_id, c.message.message_id, user_id, f"{CB_PREFIX}admin_freeze_days", {"sub_id": sub_id})
        return

    if action == "admin_refund" and len(args) >= 1:
        sub_id = args[0]
        bot.send_message(chat_id, "Введите сумму возврата (0 для авторасчёта):")
        tg.set_state(chat_id, c.message.message_id, user_id, f"{CB_PREFIX}admin_refund_amount", {"sub_id": sub_id})
        return

    if action == "admin_add_ip":
        sub_id = args[0]
        tg.set_state(chat_id, c.message.message_id, user_id, f"{CB_PREFIX}admin_device_ip", {"sub_id": sub_id})
        bot.send_message(chat_id, "Введите IP-адрес устройства:")
        return

    if action == "admin_del_ip":
        sub_id = args[0]
        tg.set_state(chat_id, c.message.message_id, user_id, f"{CB_PREFIX}admin_del_ip", {"sub_id": sub_id})
        bot.send_message(chat_id, "Введите IP-адрес для удаления:")
        return

    if action == "admin_del_all_ip":
        sub_id = args[0]
        sub = storage.get_subscription(sub_id)
        if sub:
            sub.devices = []
            storage.update_subscription(sub)
            bot.edit_message_text("Все устройства удалены.", chat_id, c.message.message_id,
                                  reply_markup=K().add(B("Назад", callback_data=f"{CB_PREFIX}admin:subs")))
        return

    if action == "admin_ban" and len(args) >= 1:
        target_uid = int(args[0])
        if storage.is_banned(target_uid):
            storage.unban_user(target_uid)
            bot.edit_message_text(f"Пользователь {target_uid} разблокирован.", chat_id, c.message.message_id,
                                  reply_markup=K().add(B("Назад", callback_data=f"{CB_PREFIX}admin:main")))
        else:
            storage.ban_user(target_uid)
            bot.edit_message_text(f"Пользователь {target_uid} заблокирован.", chat_id, c.message.message_id,
                                  reply_markup=K().add(B("Назад", callback_data=f"{CB_PREFIX}admin:main")))
        return

    if section == "bans":
        bot.edit_message_text("Введите <code>user_id</code> или <code>@username</code> для блокировки/разблокировки:",
                              chat_id, c.message.message_id,
                              reply_markup=K().add(B("Назад", callback_data=f"{CB_PREFIX}admin:main")))
        tg.set_state(chat_id, c.message.message_id, user_id, f"{CB_PREFIX}admin_ban_user")
        return

    if section == "stats":
        total_users = len(storage.users)
        total_subs = len(storage.subscriptions.get("subs", {}))
        active_subs = len(storage.active_subscriptions_all())
        total_spent = sum(_to_dec(u.get("total_spent", 0)) for u in storage.users.values())
        total_months = sum(u.get("total_months", 0) for u in storage.users.values())
        hosts = len(storage.list_hosts())
        text = (f"<b>Статистика</b>\n\n"
                f"Пользователей: {total_users}\n"
                f"Подписок: {total_subs}\n"
                f"Активных подписок: {active_subs}\n"
                f"Всего потрачено: {total_spent:.2f}₽\n"
                f"Всего куплено месяцев: {total_months}\n"
                f"Серверов: {hosts}")
        top = sorted(storage.users.values(), key=lambda u: _to_dec(u.get("total_spent", 0)), reverse=True)[:5]
        if top:
            text += "\n\n<b>Топ по тратам:</b>"
            for u in top:
                text += f"\n{u['user_id']} — {_to_dec(u.get('total_spent', 0)):.2f}₽"
        bot.edit_message_text(text, chat_id, c.message.message_id,
                              reply_markup=K().add(B("Назад", callback_data=f"{CB_PREFIX}admin:main")))
        return

    if section == "settings":
        discount = storage.config.get("referral_first_discount", 0)
        cleanup = storage.config.get("expired_cleanup_days", 5)
        text = (f"<b>Настройки</b>\n\n"
                f"Реферальная скидка на первую покупку: {discount}%\n"
                f"Автоудаление истёкших подписок через: {cleanup} дн.\n\n"
                f"Отправьте одной строкой: <code>referral_discount cleanup_days</code>")
        kb = K()
        kb.add(B("Назад", callback_data=f"{CB_PREFIX}admin:main"))
        bot.edit_message_text(text, chat_id, c.message.message_id, reply_markup=kb)
        tg.set_state(chat_id, c.message.message_id, user_id, f"{CB_PREFIX}admin_settings")
        return


def cleanup(cardinal: Cardinal, *args) -> None:
    storage.save_config()
    storage.save_users()
    storage.save_subscriptions()
    storage.save_transactions()
    storage.save_referrals()
    if _user_bot_instance:
        _user_bot_instance.stop()


storage = VPNStorage()


BIND_TO_PRE_INIT = [init_plugin]
BIND_TO_PRE_STOP = [cleanup]
