from __future__ import annotations
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, TYPE_CHECKING
from uuid import uuid4
import http.server
import csv
import ipaddress
import json
import logging
import re
import socketserver
import threading
import time

import requests
import telebot
from telebot.types import InlineKeyboardMarkup as K, InlineKeyboardButton as B, Message, CallbackQuery, LabeledPrice

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
CONFIG_FILE = STORAGE_DIR / "config.json"
USERS_FILE = STORAGE_DIR / "users.json"
SUBS_FILE = STORAGE_DIR / "subscriptions.json"
TRANS_FILE = STORAGE_DIR / "transactions.json"
REFERRALS_FILE = STORAGE_DIR / "referrals.json"
CONNECTIONS_FILE = STORAGE_DIR / "connections.json"

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
        "prices": {"1": 100, "3": 270, "6": 500, "12": 900},
    },
    "family": {
        "name": "Семейный",
        "max_devices": 5,
        "prices": {"1": 250, "3": 700, "6": 1300, "12": 2400},
    },
    "corporate": {
        "name": "Корпоративный",
        "max_devices": -1,
        "prices": {"1": 500, "3": 1400, "6": 2600, "12": 4800},
    },
}

DURATIONS = ["1", "3", "6", "12"]
TRIAL_DAYS = 3

_user_bot_instance: "UserBot | None" = None


@dataclass
class ServerConfig:
    address: str = "vpn.example.com"
    port: int = 443
    public_key: str = ""
    short_id: str = ""
    server_name: str = ""
    flow: str = "xtls-rprx-vision"
    network: str = "tcp"
    security: str = "reality"
    fingerprint: str = "chrome"


@dataclass
class Plan:
    id: str
    name: str
    max_devices: int
    prices: dict[str, float]

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
        self.config: dict[str, Any] = self._load_json(CONFIG_FILE, self._default_config)
        self._ensure_config_defaults(self.config, self._default_config())
        self.users: dict[str, Any] = self._load_json(USERS_FILE, self._default_users)
        self.subscriptions: dict[str, Any] = self._load_json(SUBS_FILE, self._default_subscriptions)
        self.transactions: dict[str, Any] = self._load_json(TRANS_FILE, self._default_transactions)
        self.referrals: dict[str, Any] = self._load_json(REFERRALS_FILE, self._default_referrals)
        self.connections: dict[str, Any] = self._load_json(CONNECTIONS_FILE, self._default_connections)

    def _ensure_config_defaults(self, current: dict[str, Any], defaults: dict[str, Any]) -> None:
        for key, value in defaults.items():
            if key not in current:
                current[key] = value
            elif isinstance(value, dict) and isinstance(current[key], dict):
                self._ensure_config_defaults(current[key], value)

    def _default_config(self) -> dict[str, Any]:
        return {
            "server": asdict(ServerConfig()),
            "plans": DEFAULT_PLANS,
            "promocodes": {},
            "activation_codes": {},
            "rates": {"USD": 0.0, "TON": 0.0, "updated_at": 0.0},
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
            "user_bot_token": "",
            "channel_id": "",
            "support_id": "",
            "crypto_bot_token": "",
            "welcome": "Добро пожаловать в VPN-бот!",
            "support": "@support",
            "bot_username": "",
            "trial_days": TRIAL_DAYS,
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

    def _load_json(self, path: Path, default_factory) -> dict[str, Any]:
        if not path.exists():
            data = default_factory()
            self._save_json(path, data)
            return data
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            logger.exception("Ошибка загрузки %s", path)
            return default_factory()

    def _save_json(self, path: Path, data: dict[str, Any]) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def save_config(self) -> None:
        with self._lock:
            self._save_json(CONFIG_FILE, self.config)

    def save_users(self) -> None:
        with self._lock:
            self._save_json(USERS_FILE, self.users)

    def save_subscriptions(self) -> None:
        with self._lock:
            self._save_json(SUBS_FILE, self.subscriptions)

    def save_transactions(self) -> None:
        with self._lock:
            self._save_json(TRANS_FILE, self.transactions)

    def save_connections(self) -> None:
        with self._lock:
            self._save_json(CONNECTIONS_FILE, self.connections)

    def save_referrals(self) -> None:
        with self._lock:
            self._save_json(REFERRALS_FILE, self.referrals)

    def add_crypto_invoice(self, invoice_id: str | int, user_id: int, amount: float, asset: str, rub_amount: float | None = None) -> None:
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

    def mark_crypto_invoice(self, invoice_id: str | int, status: str, paid_amount: float | None = None, paid_asset: str | None = None) -> dict[str, Any] | None:
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

    def server(self) -> ServerConfig:
        return ServerConfig(**self.config.get("server", {}))

    def set_server(self, server: ServerConfig) -> None:
        self.config["server"] = {
            "address": server.address,
            "port": server.port,
            "public_key": server.public_key,
            "short_id": server.short_id,
            "server_name": server.server_name,
            "flow": server.flow,
            "network": server.network,
            "security": server.security,
            "fingerprint": server.fingerprint,
        }
        self.save_config()

    def plans(self) -> dict[str, Plan]:
        result = {}
        for pid, pdata in self.config.get("plans", DEFAULT_PLANS).items():
            result[pid] = Plan(
                id=pid,
                name=pdata.get("name", pid),
                max_devices=int(pdata.get("max_devices", 1)),
                prices={str(k): float(v) for k, v in pdata.get("prices", {}).items()},
            )
        return result

    def plan(self, plan_id: str) -> Plan | None:
        return self.plans().get(plan_id)

    def update_plan(self, plan_id: str, name: str | None = None, max_devices: int | None = None, prices: dict[str, float] | None = None) -> None:
        plans = self.config.setdefault("plans", DEFAULT_PLANS)
        plan_data = plans.setdefault(plan_id, {"name": plan_id, "max_devices": 1, "prices": {}})
        if name is not None:
            plan_data["name"] = name
        if max_devices is not None:
            plan_data["max_devices"] = max_devices
        if prices is not None:
            plan_data["prices"] = {str(k): float(v) for k, v in prices.items()}
        self.save_config()

    def delete_plan(self, plan_id: str) -> None:
        plans = self.config.get("plans", {})
        if plan_id in plans and plan_id != "trial":
            del plans[plan_id]
            self.save_config()

    def add_plan(self, plan_id: str, name: str, max_devices: int, prices: dict[str, float]) -> None:
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

    def create_promocode(self, code: str, discount_type: str, value: float, max_uses: int, expires_at: float | None = None, plan_id: str | None = None) -> None:
        self.config.setdefault("promocodes", {})[code.upper()] = {
            "code": code.upper(),
            "discount_type": discount_type,
            "value": value,
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

    def unfreeze_subscription(self, sub: Subscription) -> None:
        if sub.frozen_until and sub.freeze_started and time.time() >= sub.frozen_until:
            sub.expires_at = sub.expires_at + max(0.0, sub.frozen_until - sub.freeze_started)
        sub.frozen_until = None
        sub.freeze_started = None
        self.update_subscription(sub)

    def refund_subscription(self, sub: Subscription, refund_amount: float) -> None:
        user = self.get_user(sub.user_id)
        user["balance"] = round(user["balance"] + refund_amount, 2)
        self.update_user(user)
        self._add_transaction(sub.user_id, refund_amount, "refund", "admin", f"sub:{sub.sub_id}")
        sub.active = False
        self.update_subscription(sub)

    def price(self, plan_id: str, months: int) -> float | None:
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
                "balance": 0.0,
                "referral_balance": 0.0,
                "referral_code": f"ref_{user_id}",
                "referred_by": None,
                "trial_used": False,
                "joined_at": time.time(),
                "channel_ok": False,
            }
            self.save_users()
        return self.users[key]

    def update_user(self, user: dict[str, Any]) -> None:
        self.users[str(user["user_id"])] = user
        self.save_users()

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

    def add_balance(self, user_id: int, amount: float, source: str, method: str, payload: str = "") -> None:
        user = self.get_user(user_id)
        user["balance"] = round(user["balance"] + amount, 2)
        self.update_user(user)
        self._add_transaction(user_id, amount, "deposit", method, payload)
        if amount > 0:
            self._notify_admins(f"Новый платёж: user {user_id} пополнил баланс на {amount}₽ ({method}).", "new_payment")

    def deduct_balance(self, user_id: int, amount: float, method: str, payload: str = "") -> None:
        user = self.get_user(user_id)
        user["balance"] = round(user["balance"] - amount, 2)
        self.update_user(user)
        self._add_transaction(user_id, -amount, "purchase", method, payload)

    def _add_transaction(self, user_id: int, amount: float, tx_type: str, method: str, payload: str) -> None:
        with self._lock:
            tid = str(self.transactions["next_id"])
            self.transactions["next_id"] += 1
        self.transactions["txs"][tid] = {
            "id": tid,
            "user_id": user_id,
            "amount": amount,
            "type": tx_type,
            "method": method,
            "payload": payload,
            "created_at": time.time(),
        }
        self.save_transactions()

    def _sub_from_dict(self, data: dict[str, Any]) -> Subscription:
        fields = {f for f in Subscription.__dataclass_fields__}
        return Subscription(**{k: v for k, v in data.items() if k in fields})

    def active_subscriptions(self, user_id: int) -> list[Subscription]:
        out = []
        for sub in self.subscriptions["subs"].values():
            if sub.get("user_id") == user_id and sub.get("active") and not self._sub_from_dict(sub).is_expired:
                out.append(self._sub_from_dict(sub))
        out.sort(key=lambda s: s.expires_at, reverse=True)
        return out

    def get_subscription(self, sub_id: str) -> Subscription | None:
        data = self.subscriptions["subs"].get(sub_id)
        if not data:
            return None
        return self._sub_from_dict(data)

    def create_subscription(self, user_id: int, plan_id: str, months: int, is_trial: bool = False) -> Subscription:
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
        )
        self.subscriptions["subs"][sid] = asdict(sub)
        self.save_subscriptions()
        return sub

    def update_subscription(self, sub: Subscription) -> None:
        self.subscriptions["subs"][sub.sub_id] = asdict(sub)
        self.save_subscriptions()

    def create_withdrawal_request(self, user_id: int, amount: float, card: str) -> str:
        with self._lock:
            req_id = str(self.config.setdefault("withdrawal_next_id", 1))
            self.config["withdrawal_next_id"] = int(req_id) + 1
        self.config.setdefault("withdrawal_requests", {})[req_id] = {
            "id": req_id,
            "user_id": user_id,
            "amount": amount,
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
        for uid in recipients:
            try:
                _user_bot_instance.bot.send_message(uid, text)
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
            q = q[1:]
        try:
            sub_id = str(int(q))
            sub = self.subscriptions.get("subs", {}).get(sub_id)
            if sub:
                return self.users.get(str(sub.get("user_id")))
        except (ValueError, TypeError):
            pass
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

    def bulk_add_balance(self, amount: float) -> int:
        count = 0
        for u in self.users.values():
            uid = int(u.get("user_id", 0))
            if uid:
                self.add_balance(uid, amount, "bulk", "admin")
                count += 1
        return count

    def export_csv(self, kind: str) -> Path:
        EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
        path = EXPORTS_DIR / f"{kind}.csv"
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f, delimiter=";")
            if kind == "users":
                writer.writerow(["user_id", "username", "source", "balance", "referral_balance", "trial_used", "joined_at", "referred_by"])
                for u in self.users.values():
                    writer.writerow([u.get("user_id"), u.get("username"), u.get("source"), u.get("balance"),
                                     u.get("referral_balance"), u.get("trial_used"),
                                     _format_time(u.get("joined_at", 0)), u.get("referred_by")])
            elif kind == "subscriptions":
                writer.writerow(["sub_id", "user_id", "plan_id", "months", "created_at", "expires_at", "active", "frozen_until", "devices_count"])
                for s in self.subscriptions.get("subs", {}).values():
                    sub = self._sub_from_dict(s)
                    writer.writerow([s.get("sub_id"), s.get("user_id"), s.get("plan_id"), s.get("months"),
                                     _format_time(s.get("created_at", 0)), _format_time(s.get("expires_at", 0)),
                                     s.get("active"), s.get("frozen_until"), len(s.get("devices", []))])
            elif kind == "transactions":
                writer.writerow(["id", "user_id", "type", "amount", "method", "payload", "created_at"])
                for t in self.transactions.get("txs", {}).values():
                    writer.writerow([t.get("id"), t.get("user_id"), t.get("type"), t.get("amount"),
                                     t.get("method"), t.get("payload"), _format_time(t.get("created_at", 0))])
            elif kind == "connections":
                writer.writerow(["timestamp", "sub_id", "ip", "user_agent", "allowed", "reason", "traffic_bytes"])
                for l in self.connections.get("logs", []):
                    writer.writerow([_format_time(l.get("timestamp", 0)), l.get("sub_id"), l.get("ip"),
                                     l.get("user_agent"), l.get("allowed"), l.get("reason"), l.get("traffic_bytes", 0)])
            elif kind == "complaints":
                writer.writerow(["id", "user_id", "text", "status", "created_at"])
                for c in self.config.get("complaints", []):
                    writer.writerow([c.get("id"), c.get("user_id"), c.get("text"), c.get("status"), _format_time(c.get("created_at", 0))])
        return path

    def unbind_device(self, sub_id: str, ip: str, user_id: int) -> tuple[bool, str]:
        sub = self.get_subscription(sub_id)
        if not sub or sub.user_id != user_id:
            return False, "Подписка не найдена."
        user = self.get_user(user_id)
        last_unbind = user.get("last_unbind_at", 0.0)
        cooldown = self.security().get("unbind_cooldown", DEFAULT_UNBIND_COOLDOWN)
        now = time.time()
        if now - last_unbind < cooldown:
            remaining = int(cooldown - (now - last_unbind))
            return False, f"Отвязать устройство можно через {remaining // 86400}д {remaining % 86400 // 3600}ч."
        before = len(sub.devices)
        sub.devices = [d for d in sub.devices if d.get("ip") != ip]
        if len(sub.devices) == before:
            return False, "Устройство не найдено."
        user["last_unbind_at"] = now
        self.update_subscription(sub)
        self.update_user(user)
        return True, "Устройство отвязано."

    def add_referral_earnings(self, user_id: int, amount: float, from_user_id: int, level: int) -> None:
        user = self.get_user(user_id)
        user["referral_balance"] = round(user.get("referral_balance", 0.0) + amount, 2)
        self.update_user(user)
        self._add_transaction(user_id, amount, "referral", f"level_{level}", f"from_{from_user_id}")
        key = str(user_id)
        self.referrals["earnings"].setdefault(key, {"level1": 0.0, "level2": 0.0})
        self.referrals["earnings"][key][f"level{level}"] = round(self.referrals["earnings"][key][f"level{level}"] + amount, 2)
        self.save_referrals()

    def process_referral_rewards(self, buyer_id: int, amount: float) -> None:
        user = self.get_user(buyer_id)
        parent_id = user.get("referred_by")
        if parent_id:
            try:
                pid = int(parent_id)
                reward = round(amount * 0.10, 2)
                if reward > 0 and self.get_user(pid):
                    self.add_referral_earnings(pid, reward, buyer_id, 1)
                    grandparent = self.get_user(pid).get("referred_by")
                    if grandparent:
                        gpid = int(grandparent)
                        reward2 = round(amount * 0.05, 2)
                        if reward2 > 0 and self.get_user(gpid):
                            self.add_referral_earnings(gpid, reward2, buyer_id, 2)
            except (ValueError, TypeError):
                pass


storage = VPNStorage()


def _escape(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _format_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")


def _price_text(plan: Plan, months: str) -> str:
    price = plan.prices.get(months)
    return f"{price}₽" if price is not None else "?₽"


def format_subscription(sub: Subscription) -> str:
    plan = storage.plan(sub.plan_id)
    if sub.frozen_until and time.time() < sub.frozen_until:
        status = f"Заморожена до {_format_time(sub.frozen_until)}"
    else:
        status = "Активна" if sub.active and not sub.is_expired else "Истекла/неактивна"
    lines = [
        f"<b>Подписка #{sub.sub_id}</b>",
        f"Тариф: {plan.name if plan else sub.plan_id}",
        f"Срок: {sub.months} мес." if sub.months else f"Пробный период",
        f"Статус: {status}",
        f"Действует до: {_format_time(sub.effective_expires_at)}",
        f"Устройств: {len(sub.devices)} / {plan.device_text if plan else '?'}",
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

    def create_invoice(self, user_id: int, amount: float, asset: str = "USDT") -> dict[str, Any]:
        if not self.token:
            return {"ok": False, "error": "Crypto Bot token not set"}
        payload = json.dumps({"user_id": user_id, "amount": amount, "asset": asset})
        body = {
            "asset": asset,
            "amount": str(amount),
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
                paid_amount = float(inv.get("paid_amount") or inv.get("amount") or local.get("amount") or 0)
                paid_asset = inv.get("paid_asset") or local.get("asset")
                if uid and paid_amount:
                    rub_amount = local.get("rub_amount")
                    if not rub_amount:
                        rate = RatesFetcher().get_rate(paid_asset) or 0
                        rub_amount = round(paid_amount * rate, 2) if rate else 0.0
                    storage.add_balance(int(uid), rub_amount, "Crypto Bot", f"cryptobot:{paid_asset}", iid)
                    storage.mark_crypto_invoice(iid, "paid", paid_amount, paid_asset)
                    try:
                        self.bot.bot.send_message(int(uid), f"Баланс пополнен на {rub_amount}₽ (~{paid_amount} {paid_asset}) через Crypto Bot.")
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
                    self._notify(sub.user_id, f"Подписка #{sub.sub_id} истекла. Недостаточно средств для автопродления.")
                    continue
                user["balance"] = round(user["balance"] - price, 2)
                storage.update_user(user)
                storage._add_transaction(sub.user_id, -price, "purchase", "autorenew", f"sub:{sub.sub_id}")
                sub.expires_at = sub.expires_at + months * 30 * 86400
                sub.warnings = {}
                sub.active = True
                storage.update_subscription(sub)
                storage.process_referral_rewards(sub.user_id, price)
                XrayAPI.add_client(sub)
                self._notify(sub.user_id, f"Подписка #{sub.sub_id} автоматически продлена на {months} мес. Списано {price}₽.")

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

    def _fetch_usd(self) -> float:
        try:
            r = requests.get(self.CBR_URL, timeout=20)
            r.raise_for_status()
            data = r.json()
            return float(data["Valute"]["USD"]["Value"])
        except Exception:
            last = storage.config.get("rates", {}).get("USD")
            if last:
                return last
            raise

    def _fetch_ton(self) -> float:
        try:
            r = requests.get(self.COINGECKO_URL, timeout=20)
            r.raise_for_status()
            data = r.json()
            return float(data["the-open-network"]["rub"])
        except Exception:
            last = storage.config.get("rates", {}).get("TON")
            if last:
                return last
            raise

    def get_rate(self, asset: str) -> float | None:
        rates = storage.config.get("rates", {})
        if asset == "USDT":
            return rates.get("USD") or 0.0
        if asset == "TON":
            return rates.get("TON") or 0.0
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

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def do_POST(self) -> None:
        if self.path != "/connect":
            self._json_response(404, {"error": "not found"})
            return
        data = self._read_json()
        sub_id = str(data.get("sub_id", "")).strip()
        ip = str(data.get("ip", "")).strip()
        user_agent = str(data.get("user_agent", "")).strip() or "unknown"
        traffic_bytes = int(data.get("traffic_bytes", 0) or 0)
        if not sub_id or not ip:
            self._json_response(400, {"allowed": False, "reason": "missing sub_id or ip"})
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


class StarsPayment:
    """Реальная отправка инвойса Telegram Stars."""

    @staticmethod
    def send_invoice(bot, chat_id: int, user_id: int, amount: int, title: str = "Пополнение", description: str = "Баланс VPN-бота", rub_amount: float | None = None) -> bool:
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
    """Заглушка для будущей интеграции с Xray/3x-ui панелью."""

    @staticmethod
    def add_client(sub: Subscription) -> bool:
        logger.info("XrayAPI.add_client stub called for sub #%s", sub.sub_id)
        return True

    @staticmethod
    def remove_client(sub: Subscription) -> bool:
        logger.info("XrayAPI.remove_client stub called for sub #%s", sub.sub_id)
        return True


# ==================== User Bot ====================


class UserBot:
    """Отдельный Telegram-бот для пользователей VPN."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.bot = telebot.TeleBot(token, parse_mode="HTML", allow_sending_without_reply=True, num_threads=5)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._states: dict[int, dict[str, Any]] = {}
        self._crypto_poller: CryptoBotPoller | None = None
        self._scheduler: SubscriptionScheduler | None = None
        self._rates_fetcher: RatesFetcher | None = None
        self._device_auth_server: DeviceAuthServer | None = None
        self._register_handlers()

    # ---- state helpers ----
    def set_state(self, user_id: int, state: str, data: dict[str, Any] | None = None) -> None:
        self._states[user_id] = {"state": state, "data": data or {}}

    def get_state(self, user_id: int) -> dict[str, Any] | None:
        return self._states.get(user_id)

    def check_state(self, user_id: int, state: str) -> bool:
        s = self.get_state(user_id)
        return s is not None and s.get("state") == state

    def clear_state(self, user_id: int) -> None:
        self._states.pop(user_id, None)

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
            pass
        return re.match(r"^\d{1,3}(\.\d{1,3}){3}$", text) is not None

    @staticmethod
    def _is_valid_amount(text: str) -> float | None:
        text = text.strip().replace(",", ".")
        try:
            value = float(text)
            return value if value > 0 else None
        except ValueError:
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
                return param, int(param.split("_", 1)[1])
            except (ValueError, TypeError):
                return param, None
        return param, None

    def _send_welcome(self, user_id: int, chat_id: int) -> None:
        """Отправляет приветственное медиа и главное/пробное меню."""
        user = storage.get_user(user_id)
        self._send_welcome_media(chat_id)
        text = storage.config.get("welcome", "Добро пожаловать!")
        if not user.get("trial_used"):
            kb = K()
            kb.add(B("Активировать пробный период", callback_data=f"{CB_PREFIX}trial"))
            kb.add(B("Главное меню", callback_data=f"{CB_PREFIX}main"))
            self.bot.send_message(chat_id, f"{text}\n\nУ вас есть {storage.config.get('trial_days', TRIAL_DAYS)}-дневный пробный период.", reply_markup=kb)
        else:
            self.bot.send_message(chat_id, f"{text}\n\nВыберите раздел:", reply_markup=self._keyboard_main())

    def _check_maintenance(self, user_id: int, chat_id: int) -> bool:
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

    def _channel_check(self, user_id: int, chat_id: int) -> bool:
        channel_id = storage.config.get("channel_id", "")
        if not channel_id:
            return True
        try:
            member = self.bot.get_chat_member(channel_id, user_id)
            if member.status in ("member", "administrator", "creator"):
                user = storage.get_user(user_id)
                user["channel_ok"] = True
                storage.update_user(user)
                return True
        except Exception:
            pass
        kb = K()
        kb.add(B("Проверить подписку", callback_data=f"{CB_PREFIX}check_channel"))
        invite = None
        try:
            chat = self.bot.get_chat(channel_id)
            invite = chat.invite_link or (f"https://t.me/{chat.username}" if chat.username else None)
        except Exception:
            pass
        if not invite and isinstance(channel_id, str):
            if channel_id.startswith("@"):
                invite = f"https://t.me/{channel_id.lstrip('@')}"
            elif channel_id.startswith("https://"):
                invite = channel_id
        if invite:
            kb.add(B("Подписаться", url=invite))
        channel_text = f"<b>{channel_id}</b>" if channel_id else "наш канал"
        self.bot.send_message(chat_id, f"Для использования бота необходимо подписаться на {channel_text}.", reply_markup=kb)
        return False

    def _keyboard_main(self) -> K:
        kb = K()
        kb.add(B("Профиль", callback_data=f"{CB_PREFIX}profile"))
        kb.add(B("Купить подписку", callback_data=f"{CB_PREFIX}buy"))
        kb.add(B("Мои подписки", callback_data=f"{CB_PREFIX}my_subs"))
        kb.add(B("Пополнить баланс", callback_data=f"{CB_PREFIX}deposit"))
        kb.add(B("Активировать код", callback_data=f"{CB_PREFIX}activate_code"))
        kb.add(B("Реферальная система", callback_data=f"{CB_PREFIX}referral"))
        kb.add(B("Помощь", callback_data=f"{CB_PREFIX}help"))
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
        if not self._channel_check(user_id, chat_id):
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
            if self._channel_check(user_id, chat_id):
                self.bot.answer_callback_query(c.id, "Подписка подтверждена!")
                self._send_welcome(user_id, chat_id)
            else:
                self.bot.answer_callback_query(c.id, "Вы ещё не подписались на канал.")
            return

        if action == "trial":
            self._activate_trial(user_id, chat_id, c.message.message_id)
            return

        if action == "buy":
            self._buy_menu(chat_id, c.message.message_id)
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
            sub_id, ip = args[0], args[1]
            ok, text = storage.unbind_device(sub_id, ip, user_id)
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
            ref_balance = user.get("referral_balance", 0.0)
            if ref_balance < 3000:
                self.bot.edit_message_text(f"Минимальная сумма вывода 3000₽. Ваш реферальный баланс: {ref_balance}₽.",
                                           chat_id, c.message.message_id, reply_markup=self._keyboard_main())
                return
            self.set_state(user_id, "withdraw_amount", {"message_id": c.message.message_id})
            self.bot.edit_message_text(f"Введите сумму вывода (доступно {ref_balance}₽, минимум 3000₽):",
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
        XrayAPI.add_client(sub)
        self.bot.edit_message_text(f"Пробный период активирован!\n{format_subscription(sub)}", chat_id, message_id,
                                   reply_markup=self._keyboard_main())

    def _buy_menu(self, chat_id: int, message_id: int) -> None:
        kb = K()
        for pid, plan in storage.plans().items():
            if pid == "trial":
                continue
            kb.add(B(plan.name, callback_data=f"{CB_PREFIX}plan:{pid}"))
        kb.add(B("Назад", callback_data=f"{CB_PREFIX}main"))
        self.bot.edit_message_text("Выберите тариф:", chat_id, message_id, reply_markup=kb)

    def _duration_menu(self, chat_id: int, message_id: int, plan_id: str) -> None:
        plan = storage.plan(plan_id)
        kb = K()
        for months in DURATIONS:
            kb.add(B(f"{months} мес. — {_price_text(plan, months)}", callback_data=f"{CB_PREFIX}duration:{plan_id}:{months}"))
        kb.add(B("Назад", callback_data=f"{CB_PREFIX}buy"))
        self.bot.edit_message_text(f"Тариф: <b>{_escape(plan.name)}</b>\nУстройств: {plan.device_text}\n\nВыберите срок:",
                                   chat_id, message_id, reply_markup=kb)

    def _confirm_purchase(self, user_id: int, chat_id: int, message_id: int, plan_id: str, months: str) -> None:
        plan = storage.plan(plan_id)
        base_price = storage.price(plan_id, int(months))
        state = self.get_state(user_id)
        discount = 0.0
        if state and state.get("state") in ("confirm_purchase", "enter_promo") and state.get("data", {}).get("plan_id") == plan_id and str(state.get("data", {}).get("months")) == str(months):
            discount = state["data"].get("discount", 0.0)
            message_id = message_id or state["data"].get("message_id")
        price = round(base_price - discount, 2) if base_price else 0.0
        user = storage.get_user(user_id)
        price_text = f"<s>{base_price}₽</s> {price}₽ (скидка {discount}₽)" if discount else f"{price}₽"
        text = (f"<b>{_escape(plan.name)}</b>\n"
                f"Срок: {months} мес.\n"
                f"Цена: {price_text}\n"
                f"Ваш баланс: {user['balance']}₽\n\n"
                f"Подтвердите покупку:")
        kb = K()
        kb.add(B("Купить", callback_data=f"{CB_PREFIX}purchase:{plan_id}:{months}"))
        if not discount:
            kb.add(B("Применить промокод", callback_data=f"{CB_PREFIX}promo:{plan_id}:{months}"))
        if user["balance"] < price:
            kb.add(B("Пополнить баланс", callback_data=f"{CB_PREFIX}deposit"))
        kb.add(B("Назад", callback_data=f"{CB_PREFIX}plan:{plan_id}"))
        self.set_state(user_id, "confirm_purchase", {"plan_id": plan_id, "months": months, "discount": discount, "message_id": message_id})
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
        discount = 0.0
        promo_code = None
        if state and state.get("state") == "confirm_purchase" and state.get("data", {}).get("plan_id") == plan_id and str(state.get("data", {}).get("months")) == str(months):
            discount = state["data"].get("discount", 0.0)
            promo_code = state["data"].get("promo_code")
        price = round(base_price - discount, 2)
        user = storage.get_user(user_id)
        if user["balance"] < price:
            self.bot.edit_message_text("Недостаточно средств. Пополните баланс.", chat_id, message_id,
                                       reply_markup=K().add(B("Пополнить", callback_data=f"{CB_PREFIX}deposit"),
                                                          B("Назад", callback_data=f"{CB_PREFIX}main")))
            return
        if promo_code:
            storage.use_promocode(promo_code)
        storage.deduct_balance(user_id, price, "purchase", f"plan:{plan_id}:{months}" + (f" promo:{promo_code}" if promo_code else ""))
        sub = storage.create_subscription(user_id, plan_id, months)
        storage.process_referral_rewards(user_id, price)
        XrayAPI.add_client(sub)
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
        kb.add(B("Продлить", callback_data=f"{CB_PREFIX}sub_renew:{sub_id}"))
        kb.add(B("Устройства", callback_data=f"{CB_PREFIX}sub_devices:{sub_id}"))
        kb.add(B("Заморозка", callback_data=f"{CB_PREFIX}sub_freeze:{sub_id}"))
        kb.add(B("Назад", callback_data=f"{CB_PREFIX}my_subs"))
        self.bot.edit_message_text(format_subscription(sub), chat_id, message_id, reply_markup=kb)

    def _renew_menu(self, user_id: int, chat_id: int, message_id: int, sub_id: str) -> None:
        sub = storage.get_subscription(sub_id)
        if not sub or sub.user_id != user_id:
            return
        plan = storage.plan(sub.plan_id)
        kb = K()
        for months in DURATIONS:
            kb.add(B(f"+{months} мес. — {_price_text(plan, months)}", callback_data=f"{CB_PREFIX}renew_confirm:{sub_id}:{months}"))
        kb.add(B("Назад", callback_data=f"{CB_PREFIX}sub_detail:{sub_id}"))
        self.bot.edit_message_text("Выберите срок продления:", chat_id, message_id, reply_markup=kb)

    def _renew(self, user_id: int, chat_id: int, message_id: int, sub_id: str, months: int) -> None:
        sub = storage.get_subscription(sub_id)
        plan = storage.plan(sub.plan_id)
        if not sub or sub.user_id != user_id or not plan:
            return
        price = storage.price(sub.plan_id, months)
        user = storage.get_user(user_id)
        if user["balance"] < price:
            self.bot.edit_message_text("Недостаточно средств.", chat_id, message_id,
                                       reply_markup=K().add(B("Пополнить", callback_data=f"{CB_PREFIX}deposit")))
            return
        storage.deduct_balance(user_id, price, "renew", f"sub:{sub_id}:{months}")
        sub.expires_at = sub.expires_at + months * 30 * 86400
        storage.update_subscription(sub)
        storage.process_referral_rewards(user_id, price)
        self.bot.edit_message_text(f"Подписка продлена!\n{format_subscription(sub)}", chat_id, message_id,
                                   reply_markup=self._keyboard_main())

    def _sub_devices(self, user_id: int, chat_id: int, message_id: int, sub_id: str) -> None:
        sub = storage.get_subscription(sub_id)
        if not sub or sub.user_id != user_id:
            return
        plan = storage.plan(sub.plan_id)
        lines = [f"<b>Устройства</b> ({len(sub.devices)} / {plan.device_text if plan else '?'})\n\nОтвязать устройство можно раз в {storage.security().get('unbind_cooldown', DEFAULT_UNBIND_COOLDOWN) // 86400} дней."]
        for i, d in enumerate(sub.devices, 1):
            ua = d.get('user_agent', '')
            seen = d.get('last_seen') or d.get('first_seen')
            ua_text = f" — {ua[:20]}" if ua else ""
            lines.append(f"{i}. {d['ip']}{ua_text} (последнее: {datetime.fromtimestamp(seen).strftime('%d.%m.%Y %H:%M')})")
        if not sub.devices:
            lines.append("Устройств пока нет.")
        kb = K()
        for d in sub.devices:
            kb.add(B(f"Отвязать {d['ip']}", callback_data=f"{CB_PREFIX}unbind_device:{sub_id}:{d['ip']}"))
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
        text = (f"<b>Пополнение баланса</b>\n\n"
                f"Текущий баланс: {user['balance']}₽\n"
                f"Курс: USD {usd}₽, TON {ton}₽\n"
                f"Telegram Stars: 1.3 Stars = 1₽\n\n"
                f"Выберите способ:")
        kb = K()
        kb.add(B("Crypto Bot USDT", callback_data=f"{CB_PREFIX}deposit_crypto:USDT"))
        kb.add(B("Crypto Bot TON", callback_data=f"{CB_PREFIX}deposit_crypto:TON"))
        kb.add(B("Telegram Stars", callback_data=f"{CB_PREFIX}deposit_stars"))
        kb.add(B("Назад", callback_data=f"{CB_PREFIX}main"))
        self.bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)

    def _amount_keyboard(self, callback_prefix: str, back_callback: str, asset: str | None = None) -> K:
        kb = K()
        for amount in [100, 200, 500, 1000]:
            cb = f"{callback_prefix}:{amount}" if asset is None else f"{callback_prefix}:{asset}:{amount}"
            kb.add(B(f"{amount}₽", callback_data=cb))
        cb_custom = f"{callback_prefix}:custom" if asset is None else f"{callback_prefix}:{asset}:custom"
        kb.add(B("Ввести своё", callback_data=cb_custom))
        kb.add(B("Назад", callback_data=back_callback))
        return kb

    def _create_crypto_invoice(self, user_id: int, chat_id: int, message_id: int, asset: str, rub_amount: float) -> None:
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
        amount = round(rub_amount / rate, 6)
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
            self.bot.edit_message_text(f"Счёт на {amount} {asset} (~{rub_amount}₽) создан. Оплатите по кнопке ниже.",
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

    def _create_stars_invoice(self, user_id: int, chat_id: int, message_id: int, rub_amount: float) -> None:
        stars_amount = int(round(rub_amount * 1.3))
        if stars_amount <= 0:
            self.bot.edit_message_text("Сумма слишком мала.", chat_id, message_id,
                                       reply_markup=K().add(B("Назад", callback_data=f"{CB_PREFIX}deposit")))
            return
        title = f"Пополнение на {rub_amount}₽"
        if StarsPayment.send_invoice(self.bot, chat_id, user_id, stars_amount, title=title, rub_amount=rub_amount):
            self.bot.edit_message_text(f"Инвойс на {stars_amount} Stars (~{rub_amount}₽) отправлен. Оплатите его в этом чате.",
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
            discount = round(base_price * promo["value"] / 100, 2)
        else:
            discount = round(promo["value"], 2)
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
        XrayAPI.add_client(sub)
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
        ref_balance = user.get("referral_balance", 0.0)
        if amount < 3000:
            self.bot.send_message(m.chat.id, "Минимальная сумма вывода 3000₽.")
            return
        if amount > ref_balance:
            self.bot.send_message(m.chat.id, f"Недостаточно реферальных средств. Доступно: {ref_balance}₽.")
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
        self.bot.send_message(m.chat.id, f"Заявка #{req_id} на вывод {amount}₽ создана. Ожидайте подтверждения администратора.",
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
                rub_amount = int(rub_str)
            except (ValueError, TypeError):
                return
            stars_amount = getattr(m.successful_payment, "total_amount", 0)
            storage.add_balance(uid, rub_amount, "Telegram Stars", "stars", payload)
            self.bot.send_message(m.chat.id, f"Баланс пополнен на {rub_amount}₽ ({stars_amount} Stars) через Telegram Stars.",
                                  reply_markup=self._keyboard_main())

    def _profile_menu(self, user_id: int, chat_id: int, message_id: int) -> None:
        user = storage.get_user(user_id)
        subs = storage.active_subscriptions(user_id)
        lines = [
            f"<b>Профиль</b>",
            f"ID: <code>{user_id}</code>",
            f"Баланс: {user.get('balance', 0)}₽",
        ]
        if subs:
            sub = subs[0]
            plan = storage.plan(sub.plan_id)
            lines.append(f"Активная подписка: {plan.name if plan else sub.plan_id} (до {_format_time(sub.expires_at)})")
            lines.append(f"Устройств: {len(sub.devices)} / {plan.device_text if plan else '?'}")
        else:
            lines.append("Активных подписок нет.")
        if user.get("trial_used"):
            lines.append("Пробный период использован.")
        earnings = storage.referrals.get("earnings", {}).get(str(user_id), {"level1": 0.0, "level2": 0.0})
        total_earn = earnings.get("level1", 0.0) + earnings.get("level2", 0.0)
        if total_earn:
            lines.append(f"Заработано с рефералов: {total_earn}₽")
        kb = K()
        kb.add(B("История операций", callback_data=f"{CB_PREFIX}history"))
        kb.add(B("Настройки", callback_data=f"{CB_PREFIX}settings"))
        kb.add(B("Назад", callback_data=f"{CB_PREFIX}main"))
        self.bot.edit_message_text("\n".join(lines), chat_id, message_id, reply_markup=kb)

    def _history_menu(self, user_id: int, chat_id: int, message_id: int) -> None:
        txs = storage.get_transactions(user_id)
        if not txs:
            text = "История операций пуста."
        else:
            lines = ["<b>История операций</b> (последние 20):"]
            type_names = {
                "deposit": "Пополнение",
                "purchase": "Списание",
                "referral": "Реферал",
                "trial": "Пробный период",
            }
            for tx in txs:
                tname = type_names.get(tx.get("type"), tx.get("type", "?"))
                amount = tx.get("amount", 0)
                method = tx.get("method", "")
                date = _format_time(tx.get("created_at", 0))
                lines.append(f"{date} — {tname}: {amount:+.2f}₽ ({method})")
            text = "\n".join(lines)
        self.bot.edit_message_text(text, chat_id, message_id,
                                   reply_markup=K().add(B("Назад", callback_data=f"{CB_PREFIX}profile")))

    def _settings_menu(self, user_id: int, chat_id: int, message_id: int) -> None:
        user = storage.get_user(user_id)
        settings = user.get("settings", {"lang": "ru", "notifications": True, "auto_renew": True})
        lang = settings.get("lang", "ru")
        notif = "Вкл" if settings.get("notifications", True) else "Выкл"
        renew = "Вкл" if settings.get("auto_renew", True) else "Выкл"
        text = (f"<b>Настройки</b>\n\n"
                f"Язык: {lang.upper()}\n"
                f"Уведомления: {notif}\n"
                f"Автопродление: {renew}")
        kb = K()
        kb.add(B(f"Язык: {lang.upper()}", callback_data=f"{CB_PREFIX}toggle:lang"))
        kb.add(B(f"Уведомления: {notif}", callback_data=f"{CB_PREFIX}toggle:notifications"))
        kb.add(B(f"Автопродление: {renew}", callback_data=f"{CB_PREFIX}toggle:auto_renew"))
        kb.add(B("Назад", callback_data=f"{CB_PREFIX}profile"))
        self.bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)

    def _help_menu(self, user_id: int, chat_id: int, message_id: int) -> None:
        text = "<b>Помощь</b>\n\nВыберите раздел:"
        kb = K()
        kb.add(B("FAQ", callback_data=f"{CB_PREFIX}faq"))
        kb.add(B("Поддержка", callback_data=f"{CB_PREFIX}support"))
        kb.add(B("Пожаловаться", callback_data=f"{CB_PREFIX}complaint"))
        kb.add(B("Назад", callback_data=f"{CB_PREFIX}main"))
        self.bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)

    def _faq_menu(self, user_id: int, chat_id: int, message_id: int) -> None:
        faq = storage.config.get("faq_text")
        if not faq:
            faq = (
                "<b>FAQ</b>\n\n"
                "1. Как подключиться?\nПосле покупки подписки вам выдаётся конфигурация для вашего устройства.\n\n"
                "2. Сколько устройств поддерживается?\n"
                "Зависит от тарифа: Базовый — 1, Семейный — 5, Корпоративный — безлимит.\n\n"
                "3. Как пополнить баланс?\nРаздел «Пополнить баланс» → Crypto Bot или Telegram Stars.\n\n"
                "4. Пробный период?\n3 дня, 1 устройство, один раз на аккаунт."
            )
        self.bot.edit_message_text(faq, chat_id, message_id,
                                   reply_markup=K().add(B("Назад", callback_data=f"{CB_PREFIX}help")))

    def _support_menu(self, user_id: int, chat_id: int, message_id: int) -> None:
        support = storage.config.get("support", "@support")
        text = f"<b>Поддержка</b>\n\nПо всем вопросам обращайтесь: {support}"
        self.bot.edit_message_text(text, chat_id, message_id,
                                   reply_markup=K().add(B("Назад", callback_data=f"{CB_PREFIX}help")))

    def _referral_menu(self, user_id: int, chat_id: int, message_id: int) -> None:
        bot_username = storage.config.get("bot_username", "")
        user = storage.get_user(user_id)
        link = f"https://t.me/{bot_username}?start=ref_{user_id}" if bot_username else f"Код: ref_{user_id}"
        level1 = [u for u in storage.users.values() if u.get("referred_by") == user_id]
        level2 = [u for u in storage.users.values() if u.get("referred_by") in {x["user_id"] for x in level1}]
        earnings = storage.referrals.get("earnings", {}).get(str(user_id), {"level1": 0.0, "level2": 0.0})
        ref_balance = user.get("referral_balance", 0.0)
        text = (f"<b>Реферальная система</b>\n\n"
                f"Ваша ссылка: {link}\n\n"
                f"Рефералы 1 уровня: {len(level1)}\n"
                f"Рефералы 2 уровня: {len(level2)}\n"
                f"Заработано: {earnings.get('level1', 0) + earnings.get('level2', 0)}₽\n"
                f"  1 уровень (10%): {earnings.get('level1', 0)}₽\n"
                f"  2 уровень (5%): {earnings.get('level2', 0)}₽\n\n"
                f"Реферальный баланс: {ref_balance}₽\n"
                f"Минимум для вывода: 3000₽")
        kb = K()
        kb.add(B("Вывести реферальные средства", callback_data=f"{CB_PREFIX}withdraw"))
        kb.add(B("Назад", callback_data=f"{CB_PREFIX}main"))
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
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("User-бот запущен.")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.bot.polling(non_stop=True, skip_pending=True, timeout=10, long_polling_timeout=5)
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
        bot.send_message(m.chat.id, "<b>VPN Admin</b>", reply_markup=_admin_main_keyboard())

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
            bot.send_message(m.chat.id, "Формат: адрес порт public_key short_id server_name")
            return
        try:
            server = ServerConfig(
                address=parts[0],
                port=int(parts[1]),
                public_key=parts[2],
                short_id=parts[3],
                server_name=parts[4],
            )
            storage.set_server(server)
            bot.send_message(m.chat.id, "Сервер сохранен.")
        except Exception as e:
            bot.send_message(m.chat.id, f"Ошибка: {e}")
        tg.clear_state(m.chat.id, m.from_user.id)

    def state_set_welcome_media(m: Message):
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
        storage.config["welcome_media_file_id"] = file_id
        storage.config["welcome_media_type"] = media_type
        storage.save_config()
        bot.send_message(m.chat.id, f"Приветственное {media_type} сохранено.")
        tg.clear_state(m.chat.id, m.from_user.id)

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
            price = float(m.text.strip().replace(",", "."))
            plan_id = state["data"].get("plan_id")
            months = state["data"].get("months")
            plan = storage.plan(plan_id)
            prices = plan.prices if plan else {}
            prices[str(months)] = price
            storage.update_plan(plan_id, prices=prices)
            bot.send_message(m.chat.id, f"Цена для {months} мес. тарифа {plan_id} обновлена.")
        except ValueError:
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
            tg.set_state(m.chat.id, m.message_id, m.from_user.id, f"{CB_PREFIX}admin_plan_add_prices", state["data"])
            bot.send_message(m.chat.id, f"Отправьте цены через пробел для {', '.join(DURATIONS)} мес. (например: 100 270 500 900):")
        except ValueError:
            bot.send_message(m.chat.id, "Введите число.")

    def state_admin_plan_add_prices(m: Message):
        state = tg.get_state(m.chat.id, m.from_user.id)
        if not state:
            return
        parts = m.text.strip().split()
        if len(parts) != len(DURATIONS):
            bot.send_message(m.chat.id, f"Нужно {len(DURATIONS)} цены через пробел.")
            return
        try:
            prices = {str(DURATIONS[i]): float(parts[i].replace(",", ".")) for i in range(len(DURATIONS))}
            data = state["data"]
            storage.add_plan(data["plan_id"], data["name"], data["max_devices"], prices)
            bot.send_message(m.chat.id, f"Тариф {data['name']} добавлен.")
        except ValueError:
            bot.send_message(m.chat.id, "Введите числовые цены.")
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
            value = float(parts[2].replace(",", "."))
            max_uses = int(parts[3])
        except ValueError:
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
            amount = float(m.text.strip().replace(",", "."))
            uid = state["data"]["uid"]
            if amount > 0:
                storage.add_balance(uid, amount, "manual", "admin")
            else:
                storage.deduct_balance(uid, -amount, "manual", "admin")
            user = storage.get_user(uid)
            bot.send_message(m.chat.id, f"Баланс пользователя {uid}: {user['balance']}₽")
        except ValueError:
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
            amount = float(m.text.strip().replace(",", "."))
            sub_id = state["data"]["sub_id"]
            sub = storage.get_subscription(sub_id)
            if not sub:
                bot.send_message(m.chat.id, "Подписка не найдена.")
                return
            if amount <= 0:
                # auto-calc remaining value
                plan = storage.plan(sub.plan_id)
                total = storage.price(sub.plan_id, sub.months) or 0
                total_days = sub.months * 30 if sub.months else 3
                remaining_days = max(0, (sub.effective_expires_at - time.time()) / 86400)
                amount = round(total * remaining_days / total_days, 2)
            storage.refund_subscription(sub, amount)
            bot.send_message(m.chat.id, f"Возвращено {amount}₽ пользователю {sub.user_id} за подписку #{sub.sub_id}.")
            if _user_bot_instance:
                try:
                    _user_bot_instance.bot.send_message(sub.user_id, f"По подписке #{sub.sub_id} возвращено {amount}₽.")
                except Exception:
                    pass
        except ValueError:
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
            amount = float(m.text.strip().replace(",", "."))
            count = storage.bulk_add_balance(amount)
            bot.send_message(m.chat.id, f"{count} пользователям начислено {amount}₽.")
        except ValueError:
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
            bot.send_message(m.chat.id, "Введите текст рассылки:")
            return
        source_value = state["data"].get("source_value")
        text = m.text.strip()
        if op != "broadcast" or not text:
            bot.send_message(m.chat.id, "Ошибка рассылки.")
            tg.clear_state(m.chat.id, m.from_user.id)
            return
        sent = 0
        failed = 0
        now = time.time()
        for u in storage.users.values():
            uid = int(u.get("user_id", 0))
            if not uid:
                continue
            if filter_type == "active":
                if not any(s.get("user_id") == uid and not storage._sub_from_dict(s).is_expired for s in storage.subscriptions.get("subs", {}).values()):
                    continue
            elif filter_type == "expired":
                subs = [s for s in storage.subscriptions.get("subs", {}).values() if s.get("user_id") == uid]
                if not subs or any(not storage._sub_from_dict(s).is_expired for s in subs):
                    continue
            elif filter_type == "source":
                if u.get("source") != source_value:
                    continue
            try:
                _user_bot_instance.bot.send_message(uid, text)
                sent += 1
            except Exception:
                failed += 1
        bot.send_message(m.chat.id, f"Рассылка завершена. Отправлено: {sent}, не удалось: {failed}.")
        tg.clear_state(m.chat.id, m.from_user.id)

    tg.msg_handler(state_set_token, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}set_user_token"))
    tg.msg_handler(state_set_channel, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}set_channel"))
    tg.msg_handler(state_set_support, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}set_support"))
    tg.msg_handler(state_set_crypto_token, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}set_crypto_token"))
    tg.msg_handler(state_set_server, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}set_server"))
    tg.msg_handler(state_set_welcome_media, content_types=["photo", "video", "animation", "text"], func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}set_welcome_media"))
    tg.msg_handler(state_set_faq_text, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}set_faq_text"))
    tg.msg_handler(state_admin_plan_name, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_plan_name"))
    tg.msg_handler(state_admin_plan_devices, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_plan_devices"))
    tg.msg_handler(state_admin_plan_price, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_plan_price"))
    tg.msg_handler(state_admin_plan_add_id, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_plan_add_id"))
    tg.msg_handler(state_admin_plan_add_name, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_plan_add_name"))
    tg.msg_handler(state_admin_plan_add_devices, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_plan_add_devices"))
    tg.msg_handler(state_admin_plan_add_prices, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_plan_add_prices"))
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
    tg.msg_handler(state_admin_bulk_extend, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_bulk_extend"))
    tg.msg_handler(state_admin_bulk_balance, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_bulk_balance"))
    tg.msg_handler(state_admin_bulk_text, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, f"{CB_PREFIX}admin_bulk_text"))

    token = storage.config.get("user_bot_token", "")
    if token:
        try:
            _start_user_bot(cardinal, None)
        except Exception:
            logger.exception("Не удалось автоматически запустить user-бота")


def _admin_main_keyboard() -> K:
    kb = K()
    kb.add(B("Токен user-бота", callback_data=f"{CB_PREFIX}admin:user_token"))
    kb.add(B("Перезапустить user-бота", callback_data=f"{CB_PREFIX}admin:start_bot"))
    kb.add(B("Канал подписки", callback_data=f"{CB_PREFIX}admin:channel"))
    kb.add(B("Админ поддержки", callback_data=f"{CB_PREFIX}admin:support"))
    kb.add(B("Токен Crypto Bot", callback_data=f"{CB_PREFIX}admin:crypto_token"))
    kb.add(B("Приветственное медиа", callback_data=f"{CB_PREFIX}admin:welcome_media"))
    kb.add(B("Текст FAQ", callback_data=f"{CB_PREFIX}admin:faq"))
    kb.add(B("Настройки сервера", callback_data=f"{CB_PREFIX}admin:server"))
    kb.add(B("Планы и цены", callback_data=f"{CB_PREFIX}admin:plans"))
    kb.add(B("Промокоды", callback_data=f"{CB_PREFIX}admin:promos"))
    kb.add(B("Подарочные коды", callback_data=f"{CB_PREFIX}admin:codes"))
    kb.add(B("Выдать подписку", callback_data=f"{CB_PREFIX}admin:give"))
    kb.add(B("Баланс пользователя", callback_data=f"{CB_PREFIX}admin:balance"))
    kb.add(B("Пользователи", callback_data=f"{CB_PREFIX}admin:users"))
    kb.add(B("Подписки", callback_data=f"{CB_PREFIX}admin:subs"))
    kb.add(B("Источники", callback_data=f"{CB_PREFIX}admin:sources"))
    kb.add(B("Выводы", callback_data=f"{CB_PREFIX}admin:withdrawals"))
    kb.add(B("Безопасность и логи", callback_data=f"{CB_PREFIX}admin:security"))
    kb.add(B("Поиск", callback_data=f"{CB_PREFIX}admin:search"))
    kb.add(B("Массовые операции", callback_data=f"{CB_PREFIX}admin:bulk"))
    kb.add(B("Экспорт", callback_data=f"{CB_PREFIX}admin:export"))
    kb.add(B("Уведомления админу", callback_data=f"{CB_PREFIX}admin:notifications"))
    kb.add(B("Жалобы", callback_data=f"{CB_PREFIX}admin:complaints"))
    maintenance = "Вкл" if storage.config.get("maintenance") else "Выкл"
    kb.add(B(f"Тех. работы: {maintenance}", callback_data=f"{CB_PREFIX}admin:maintenance"))
    return kb


def _admin_plans_keyboard(action: str) -> K:
    kb = K()
    for pid, plan in storage.plans().items():
        if pid == "trial":
            continue
        kb.add(B(plan.name, callback_data=f"{CB_PREFIX}{action}:{pid}"))
    return kb


def _admin_durations_keyboard(plan_id: str, action: str) -> K:
    plan = storage.plan(plan_id)
    kb = K()
    for months in DURATIONS:
        kb.add(B(f"{months} мес. — {_price_text(plan, months)}", callback_data=f"{CB_PREFIX}{action}:{plan_id}:{months}"))
    return kb


def _admin_user_card(target_uid: int) -> tuple[str, K]:
    user = storage.get_user(target_uid)
    if not user:
        return "Пользователь не найден.", K().add(B("Назад", callback_data=f"{CB_PREFIX}admin:main"))
    level1, level2 = storage.get_user_referrals(target_uid)
    subs = storage.get_user_subscriptions(target_uid)
    earnings = storage.referrals.get("earnings", {}).get(str(target_uid), {"level1": 0.0, "level2": 0.0})
    lines = [
        f"<b>Пользователь {target_uid}</b>",
        f"@{user.get('username') or '—'}",
        f"Источник: {user.get('source', 'direct')}",
        f"Баланс: {user.get('balance', 0)}₽",
        f"Реферальный баланс: {user.get('referral_balance', 0)}₽",
        f"Рефералы: {len(level1)} / {len(level2)} — заработок {earnings.get('level1', 0) + earnings.get('level2', 0)}₽",
        f"Подписок: {len(subs)}",
    ]
    for s in subs:
        sub = storage._sub_from_dict(s)
        plan = storage.plan(s.get("plan_id"))
        status = "Активна" if s.get("active") and not sub.is_expired else "Истекла"
        lines.append(f"  #{s.get('sub_id')} {plan.name if plan else s.get('plan_id')} — {status} до {_format_time(s.get('expires_at', 0))}")
    kb = K()
    for s in subs:
        kb.add(B(f"Подписка #{s.get('sub_id')}", callback_data=f"{CB_PREFIX}admin_sub:{s.get('sub_id')}"))
    kb.add(B("История платежей", callback_data=f"{CB_PREFIX}admin_user_payments:{target_uid}"))
    kb.add(B("Назад", callback_data=f"{CB_PREFIX}admin:main"))
    return "\n".join(lines), kb


def _admin_bulk_filter_keyboard(op: str) -> K:
    kb = K()
    for key, label in [("all", "Всем"), ("active", "С активной подпиской"), ("expired", "С истёкшей"), ("source", "По источнику")]:
        kb.add(B(label, callback_data=f"{CB_PREFIX}admin_bulk_filter:{op}:{key}"))
    kb.add(B("Назад", callback_data=f"{CB_PREFIX}admin:bulk"))
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

    if not is_admin(cardinal, user_id):
        bot.send_message(chat_id, "Нет доступа.")
        return

    if action == "admin" and (not args or args[0] == "main"):
        bot.edit_message_text("<b>VPN Admin</b>", chat_id, c.message.message_id, reply_markup=_admin_main_keyboard())
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
                kb.add(B("Удалить медиа", callback_data=f"{CB_PREFIX}admin:del_welcome_media"))
            kb.add(B("Назад", callback_data=f"{CB_PREFIX}admin:main"))
            bot.edit_message_text(text, chat_id, c.message.message_id, reply_markup=kb)
            tg.set_state(chat_id, c.message.message_id, user_id, f"{CB_PREFIX}set_welcome_media")
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

        if section == "server":
            srv = storage.server()
            text = (f"<b>Настройки сервера (Xray)</b>\n"
                    f"Адрес: {srv.address}\n"
                    f"Порт: {srv.port}\n"
                    f"publicKey: <code>{srv.public_key}</code>\n"
                    f"shortId: <code>{srv.short_id}</code>\n"
                    f"serverName: {srv.server_name}\n\n"
                    f"Отправьте: <code>адрес порт public_key short_id server_name</code>")
            kb = K().add(B("Назад", callback_data=f"{CB_PREFIX}admin:main"))
            bot.edit_message_text(text, chat_id, c.message.message_id, reply_markup=kb)
            tg.set_state(chat_id, c.message.message_id, user_id, f"{CB_PREFIX}set_server")
            return

        if section == "plans":
            lines = ["<b>Текущие планы</b>"]
            for pid, plan in storage.plans().items():
                prices = ", ".join(f"{m}м:{plan.prices[m]}" for m in DURATIONS if m in plan.prices)
                lines.append(f"{plan.name} ({pid}) — {plan.device_text} — {prices}")
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
            text = (f"<b>Тариф {plan.name} ({plan_id})</b>\n"
                    f"Устройств: {plan.device_text}\n"
                    f"Цены: {prices}")
            kb = K()
            kb.add(B("Изменить название", callback_data=f"{CB_PREFIX}admin:plan_name:{plan_id}"))
            kb.add(B("Изменить устройства", callback_data=f"{CB_PREFIX}admin:plan_devices:{plan_id}"))
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
                    lines.append(f"{u['user_id']} — @{u['username'] or '?'} — баланс {u['balance']}₽")
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
                    lines.append(f"#{s['sub_id']} — user {s['user_id']} — {plan.name if plan else s['plan_id']} — {status}")
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
                lines.append(f"{src}: {count}")
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
                uname = u.get("username") or "?"
                lines.append(f"#{r['id']} — user {r['user_id']} (@{uname}) — {r['amount']}₽ — {r.get('card','')}")
            if len(lines) == 1:
                lines.append("Нет заявок.")
            kb = K()
            for r in reqs:
                kb.add(B(f"#{r['id']} {r['amount']}₽", callback_data=f"{CB_PREFIX}admin_withdraw:{r['id']}"))
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
                    lines.append(f"{status} #{comp.get('id')} user {comp.get('user_id')} ({_format_time(comp.get('created_at'))}): {comp.get('text', '')[:80]}")
                text = "\n".join(lines)
            kb = K()
            for comp in complaints[-20:]:
                if comp.get("status") != "closed":
                    kb.add(B(f"Закрыть #{comp.get('id')}", callback_data=f"{CB_PREFIX}admin_close_complaint:{comp.get('id')}"))
            kb.add(B("Назад", callback_data=f"{CB_PREFIX}admin:main"))
            bot.edit_message_text(text, chat_id, c.message.message_id, reply_markup=kb)
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
                    lines.append(f"#{l.get('sub_id')} {l.get('ip')} — {l.get('reason')} — {'разрешено' if l.get('allowed') else 'запрещено'} ({_format_time(l.get('timestamp'))})")
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
                    lines.append(f"user {e.get('user_id')} sub #{e.get('sub_id')} IP {e.get('ip')} — {e.get('reason')} ({_format_time(e.get('timestamp'))})")
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
                f"Сумма: {req['amount']}₽\n"
                f"Карта/реквизиты: {req.get('card','')}")
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
        if u.get("referral_balance", 0.0) < req["amount"]:
            bot.send_message(chat_id, "У пользователя недостаточно реферальных средств.")
            return
        u["referral_balance"] = round(u["referral_balance"] - req["amount"], 2)
        storage.update_user(u)
        storage._add_transaction(req["user_id"], -req["amount"], "withdrawal", "card", f"req:{req_id}")
        storage.update_withdrawal_request(req_id, "approved")
        bot.edit_message_text(f"Заявка #{req_id} на {req['amount']}₽ подтверждена.", chat_id, c.message.message_id,
                              reply_markup=_admin_main_keyboard())
        if _user_bot_instance:
            try:
                _user_bot_instance.bot.send_message(req["user_id"], f"Заявка #{req_id} на вывод {req['amount']}₽ подтверждена. Средства отправлены на {req.get('card','')}.")
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
                _user_bot_instance.bot.send_message(req["user_id"], f"Заявка #{req_id} на вывод {req['amount']}₽ отклонена.")
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
                              reply_markup=K().add(B("Назад", callback_data=f"{CB_PREFIX}admin_user:{target_uid}")))
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
        tg.clear_state(chat_id, user_id)
        sub = storage.create_subscription(uid, plan_id, months)
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


def handle_new_order(cardinal: Cardinal, e) -> None:
    """Авто-реакция на заказ FunPay с VPN-лотом."""
    try:
        order = e.order
        desc = (order.description or "").lower()
        if "vpn" not in desc:
            return
        chat_id = order.chat_id
        try:
            chat = cardinal.account.get_chat_by_name(order.buyer_username)
            if chat:
                chat_id = chat.id
        except Exception:
            pass
        msg = ("Спасибо за покупку VPN!\n\n"
               "Напишите нашему Telegram-боту и используйте команду /vpn.\n"
               f"Ваш ник FunPay: <code>{order.buyer_username}</code>")
        cardinal.send_message(chat_id, msg, order.buyer_username)
    except Exception:
        logger.exception("Ошибка обработки VPN-заказа")


def cleanup(cardinal: Cardinal, *args) -> None:
    storage.save_config()
    storage.save_users()
    storage.save_subscriptions()
    storage.save_transactions()
    storage.save_referrals()
    if _user_bot_instance:
        _user_bot_instance.stop()


BIND_TO_PRE_INIT = [init_plugin]
BIND_TO_NEW_ORDER = [handle_new_order]
BIND_TO_PRE_STOP = [cleanup]
