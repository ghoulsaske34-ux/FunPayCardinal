# noplug
"""
Админ-панель Telegram Account Shop для Cardinal ПУ.
Загружается основным плагином telegram_account_shop.py.
"""
from __future__ import annotations

import html
import logging
import re
import threading
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

try:
    from phonenumbers import parse as pn_parse, region_code_for_country_code
    from phonenumbers.geocoder import description_for_number
    PHONENUMBERS = True
except Exception:
    PHONENUMBERS = False

try:
    from pyrogram import Client
    from pyrogram.errors import SessionPasswordNeeded
except Exception:
    Client = None
    SessionPasswordNeeded = None

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery

try:
    from tg_bot import CBT as CBT_FPC
except Exception:
    CBT_FPC = None

logger = logging.getLogger("telegram_account_shop.admin")

# --- константы ---
CB_ADMIN = "tgsa:"
CBA_MAIN = f"{CB_ADMIN}main"
CBA_CATEGORIES = f"{CB_ADMIN}categories"
CBA_SET_PRICE = f"{CB_ADMIN}set_price:"
CBA_DEL_CAT = f"{CB_ADMIN}del_cat:"
CBA_CONFIRM_DEL_CAT = f"{CB_ADMIN}confirm_del:"
CBA_ADD_CATEGORY = f"{CB_ADMIN}add_category"
CBA_ADD_ACCOUNTS = f"{CB_ADMIN}add_accounts"
CBA_ADD_PHONE = f"{CB_ADMIN}add_phone"
CBA_LIST_ACCOUNTS = f"{CB_ADMIN}list_accounts"
CBA_ACCOUNTS_BY_CAT = f"{CB_ADMIN}acc_by_cat:"
CBA_DEPOSITS = f"{CB_ADMIN}deposits"
CBA_STATS = f"{CB_ADMIN}stats"
CBA_SETTINGS = f"{CB_ADMIN}settings"
CBA_EDIT_CFG = f"{CB_ADMIN}edit_cfg:"

STATE_PREFIX = "tgsa_state_"
STATE_SET_PRICE = f"{STATE_PREFIX}set_price"
STATE_ADD_CATEGORY_NAME = f"{STATE_PREFIX}add_cat_name"
STATE_ADD_CATEGORY_PRICE = f"{STATE_PREFIX}add_cat_price"
STATE_ADD_ACCOUNTS = f"{STATE_PREFIX}add_accounts"
STATE_ADD_ACCOUNTS_PRICE = f"{STATE_PREFIX}add_accounts_price"
STATE_ADD_PHONE = f"{STATE_PREFIX}add_phone"
STATE_EDIT_CFG = f"{STATE_PREFIX}edit_cfg"

STATUS_AVAILABLE = "available"

# сокращённый fallback для определения страны по коду
_COUNTRY_FALLBACK: dict[str, str] = {
    "7": "Россия",
    "1": "США/Канада",
    "44": "Великобритания",
    "49": "Германия",
    "33": "Франция",
    "39": "Италия",
    "34": "Испания",
    "90": "Турция",
    "380": "Украина",
    "375": "Беларусь",
    "77": "Казахстан",
    "996": "Киргизия",
    "992": "Таджикистан",
    "998": "Узбекистан",
    "993": "Туркменистан",
    "48": "Польша",
    "420": "Чехия",
    "421": "Словакия",
    "36": "Венгрия",
    "40": "Румыния",
    "359": "Болгария",
    "386": "Словения",
    "385": "Хорватия",
    "381": "Сербия",
    "30": "Греция",
    "31": "Нидерланды",
    "32": "Бельгия",
    "46": "Швеция",
    "47": "Норвегия",
    "45": "Дания",
    "358": "Финляндия",
    "372": "Эстония",
    "371": "Латвия",
    "370": "Литва",
    "372": "Эстония",
    "41": "Швейцария",
    "43": "Австрия",
    "81": "Япония",
    "82": "Южная Корея",
    "86": "Китай",
    "65": "Сингапур",
    "66": "Таиланд",
    "84": "Вьетнам",
    "62": "Индонезия",
    "60": "Малайзия",
    "63": "Филиппины",
    "91": "Индия",
    "92": "Пакистан",
    "98": "Иран",
    "971": "ОАЭ",
    "966": "Саудовская Аравия",
    "20": "Египет",
    "27": "ЮАР",
    "55": "Бразилия",
    "52": "Мексика",
    "54": "Аргентина",
    "56": "Чили",
    "57": "Колумбия",
    "58": "Венесуэла",
    "51": "Перу",
}


def _to_dec(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("Invalid decimal")


def _detect_country(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    if not digits:
        return "Другое"
    if PHONENUMBERS:
        try:
            parsed = pn_parse("+" + digits, None)
            desc = description_for_number(parsed, "ru")
            if desc:
                return desc
        except Exception:
            pass
    for length in (5, 4, 3, 2, 1):
        prefix = digits[:length]
        if prefix in _COUNTRY_FALLBACK:
            return _COUNTRY_FALLBACK[prefix]
    return "Другое"


def _parse_proxy(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    text = text.strip()
    if text.lower() in ("нет", "не", "no", "none", "-", "пропустить"):
        return None
    # t.me/proxy?server=...&port=...&secret=...
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


def _proxy_for_pyrogram(proxy: dict[str, Any] | None) -> dict[str, Any] | None:
    if not proxy:
        return None
    scheme = (proxy.get("scheme") or "").upper()
    if scheme == "MTPROTO":
        logger.warning("MTProto-прокси не поддерживается Pyrogram, подключение будет без прокси")
        return None
    pyro_proxy = dict(proxy)
    pyro_proxy["scheme"] = scheme
    if pyro_proxy.get("username") is None:
        pyro_proxy.pop("username", None)
    if pyro_proxy.get("password") is None:
        pyro_proxy.pop("password", None)
    return pyro_proxy


class ShopAdminPanel:
    def __init__(self, cardinal: Any, storage: Any, shop_bot: Any, uuid: str) -> None:
        self.cardinal = cardinal
        self.storage = storage
        self.shop_bot = shop_bot
        self.uuid = uuid
        self.tg = cardinal.telegram
        self.bot = cardinal.telegram.bot

    def register(self) -> None:
        if CBT_FPC and self.tg:
            self.tg.cbq_handler(
                self.open_settings,
                lambda c: c.data.startswith(f"{CBT_FPC.PLUGIN_SETTINGS}:{self.uuid}:"),
            )
            self.tg.cbq_handler(self.on_callback, lambda c: c.data.startswith(CB_ADMIN))
            self.tg.msg_handler(
                self.on_message,
                content_types=["text", "document"],
                func=lambda m: self._in_admin_state(m),
            )
            try:
                self.cardinal.add_telegram_commands(
                    self.uuid,
                    [
                        ("shopstart", "Главное меню", False),
                        ("shopprofile", "Мой профиль", False),
                        ("shopsupport", "Поддержка", False),
                        ("shopsetup", "Код администратора", False),
                    ],
                )
            except Exception:
                logger.exception("Ошибка регистрации команд в Cardinal")

    # --- helpers ---
    def _authorized(self, user_id: int) -> bool:
        return user_id in self.tg.authorized_users

    def _in_admin_state(self, m: Message) -> bool:
        s = self.tg.get_state(m.chat.id, m.from_user.id)
        return s is not None and s.get("state", "").startswith(STATE_PREFIX)

    def _back_kb(self, data: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup().add(InlineKeyboardButton("🔙 Назад", callback_data=data))

    def _main_text(self) -> str:
        parts = ["<b>Telegram Account Shop — админ-панель</b>"]
        if self.shop_bot and getattr(self.shop_bot, "bot_username", ""):
            parts.append(f"Бот: @{html.escape(self.shop_bot.bot_username)}")
        if self.shop_bot and getattr(self.shop_bot, "_setup_password", ""):
            parts.append(f"Код настройки: <code>{html.escape(self.shop_bot._setup_password)}</code>")
        return "\n\n".join(parts)

    def _main_kb(self) -> InlineKeyboardMarkup:
        kb = InlineKeyboardMarkup(row_width=1)
        back_data = f"{CBT_FPC.PLUGINS_LIST}:0" if CBT_FPC else CBA_MAIN
        kb.add(
            InlineKeyboardButton("📁 Категории и цены", callback_data=CBA_CATEGORIES),
            InlineKeyboardButton("➕ Добавить аккаунты", callback_data=CBA_ADD_ACCOUNTS),
            InlineKeyboardButton("📞 Добавить по номеру", callback_data=CBA_ADD_PHONE),
            InlineKeyboardButton("📦 Список аккаунтов", callback_data=CBA_LIST_ACCOUNTS),
            InlineKeyboardButton("💰 Пополнения", callback_data=CBA_DEPOSITS),
            InlineKeyboardButton("📊 Статистика", callback_data=CBA_STATS),
            InlineKeyboardButton("⚙️ Настройки", callback_data=CBA_SETTINGS),
            InlineKeyboardButton("🔙 Назад", callback_data=back_data),
        )
        return kb

    # --- handlers ---
    def open_settings(self, c: CallbackQuery) -> None:
        if not self._authorized(c.from_user.id):
            self.bot.answer_callback_query(c.id, "Нет доступа", show_alert=True)
            return
        try:
            if c.message.text is None:
                self.bot.send_message(
                    c.message.chat.id, self._main_text(), reply_markup=self._main_kb(), parse_mode="HTML"
                )
            else:
                self.bot.edit_message_text(
                    self._main_text(), c.message.chat.id, c.message.id,
                    reply_markup=self._main_kb(), parse_mode="HTML",
                )
        except Exception:
            logger.exception("open_settings error")
        try:
            self.bot.answer_callback_query(c.id)
        except Exception:
            pass

    def on_callback(self, c: CallbackQuery) -> None:
        if not self._authorized(c.from_user.id):
            self.bot.answer_callback_query(c.id, "Нет доступа", show_alert=True)
            return
        data = c.data[len(CB_ADMIN):]
        parts = data.split(":", 2)
        action = parts[0]
        arg1 = parts[1] if len(parts) > 1 else ""
        try:
            if action == "main":
                self.open_settings(c)
            elif action == "categories":
                self.show_categories(c)
            elif action == "set_price":
                self.set_price_start(c, int(arg1))
            elif action == "del_cat":
                self.del_cat_start(c, int(arg1))
            elif action == "confirm_del":
                self.confirm_del_cat(c, int(arg1))
            elif action == "add_category":
                self.add_category_start(c)
            elif action == "add_accounts":
                self.add_accounts_start(c)
            elif action == "add_phone":
                self.add_phone_start(c)
            elif action == "list_accounts":
                self.list_categories_for_accounts(c)
            elif action == "acc_by_cat":
                self.show_accounts_by_cat(c, int(arg1))
            elif action == "deposits":
                self.show_deposits(c)
            elif action == "stats":
                self.show_stats(c)
            elif action == "settings":
                self.show_settings(c)
            elif action == "edit_cfg":
                self.edit_config_start(c, arg1)
        except Exception:
            logger.exception("admin callback error: %s", c.data)
        try:
            self.bot.answer_callback_query(c.id)
        except Exception:
            pass

    def on_message(self, m: Message) -> None:
        if not self._authorized(m.from_user.id):
            return
        s = self.tg.get_state(m.chat.id, m.from_user.id)
        if not s:
            return
        state = s["state"]
        try:
            if m.content_type == "document":
                self.bot.send_message(m.chat.id, "❌ В этом режиме нужно отправить текст.")
                return
            if state == STATE_SET_PRICE:
                self.save_set_price(m)
            elif state == STATE_ADD_CATEGORY_NAME:
                self.save_category_name(m)
            elif state == STATE_ADD_CATEGORY_PRICE:
                self.save_category_price(m)
            elif state == STATE_ADD_ACCOUNTS:
                self.add_accounts_text(m)
            elif state == STATE_ADD_ACCOUNTS_PRICE:
                self.add_accounts_price(m)
            elif state == STATE_ADD_PHONE:
                self.add_phone_text(m)
            elif state == STATE_EDIT_CFG:
                self.save_config(m)
        except Exception:
            logger.exception("admin message error")

    # --- categories ---
    def show_categories(self, c: CallbackQuery) -> None:
        cats = self.storage.get_categories()
        text = "<b>📁 Категории и цены</b>\n\n"
        if not cats:
            text += "Пока нет категорий."
        kb = InlineKeyboardMarkup(row_width=1)
        for cat in cats:
            text += f"• {html.escape(cat['name'])} — {cat['price']}₽\n"
            kb.add(InlineKeyboardButton(f"✏️ {html.escape(cat['name'])} — {cat['price']}₽", callback_data=f"{CBA_SET_PRICE}{cat['id']}"))
            kb.add(InlineKeyboardButton(f"🗑 {html.escape(cat['name'])}", callback_data=f"{CBA_DEL_CAT}{cat['id']}"))
        kb.add(InlineKeyboardButton("➕ Создать категорию", callback_data=CBA_ADD_CATEGORY))
        kb.add(InlineKeyboardButton("🔙 Назад", callback_data=CBA_MAIN))
        self.bot.edit_message_text(text, c.message.chat.id, c.message.id, reply_markup=kb, parse_mode="HTML")

    def set_price_start(self, c: CallbackQuery, cat_id: int) -> None:
        cat = self.storage.get_category(cat_id)
        if not cat:
            return
        self.tg.set_state(c.message.chat.id, c.message.id, c.from_user.id, STATE_SET_PRICE, {"category_id": cat_id})
        text = f"✏️ {html.escape(cat['name'])}\nВведите новую цену (₽):"
        kb = InlineKeyboardMarkup().add(InlineKeyboardButton("🔙 Отмена", callback_data=CBA_CATEGORIES))
        self.bot.edit_message_text(text, c.message.chat.id, c.message.id, reply_markup=kb, parse_mode="HTML")

    def save_set_price(self, m: Message) -> None:
        s = self.tg.get_state(m.chat.id, m.from_user.id)
        if not s:
            return
        cat_id = s["data"]["category_id"]
        try:
            price = _to_dec(m.text)
        except Exception:
            self.bot.send_message(m.chat.id, "❌ Введите число.")
            return
        self.storage.update_category_price(cat_id, float(price))
        self.tg.clear_state(m.chat.id, m.from_user.id)
        self.bot.send_message(m.chat.id, "✅ Цена обновлена.", reply_markup=self._back_kb(CBA_CATEGORIES))

    def del_cat_start(self, c: CallbackQuery, cat_id: int) -> None:
        cat = self.storage.get_category(cat_id)
        if not cat:
            return
        text = f"🗑 Удалить категорию <b>{html.escape(cat['name'])}</b> и все её аккаунты?"
        kb = InlineKeyboardMarkup(row_width=2).add(
            InlineKeyboardButton("✅ Да", callback_data=f"{CBA_CONFIRM_DEL_CAT}{cat_id}"),
            InlineKeyboardButton("❌ Нет", callback_data=CBA_CATEGORIES),
        )
        self.bot.edit_message_text(text, c.message.chat.id, c.message.id, reply_markup=kb, parse_mode="HTML")

    def confirm_del_cat(self, c: CallbackQuery, cat_id: int) -> None:
        for acc in self.storage.get_accounts(category_id=cat_id):
            self.storage.delete_account(acc["id"])
        self.storage.delete_category(cat_id)
        self.show_categories(c)

    def add_category_start(self, c: CallbackQuery) -> None:
        self.tg.set_state(c.message.chat.id, c.message.id, c.from_user.id, STATE_ADD_CATEGORY_NAME, {})
        text = "➕ Введите название новой категории:"
        kb = InlineKeyboardMarkup().add(InlineKeyboardButton("🔙 Отмена", callback_data=CBA_CATEGORIES))
        self.bot.edit_message_text(text, c.message.chat.id, c.message.id, reply_markup=kb, parse_mode="HTML")

    def save_category_name(self, m: Message) -> None:
        name = (m.text or "").strip()
        if not name:
            self.bot.send_message(m.chat.id, "❌ Введите название.")
            return
        self.tg.set_state(m.chat.id, m.id, m.from_user.id, STATE_ADD_CATEGORY_PRICE, {"name": name})
        text = f"💰 Введите цену для категории <b>{html.escape(name)}</b> (₽):"
        self.bot.send_message(m.chat.id, text, parse_mode="HTML", reply_markup=self._back_kb(CBA_CATEGORIES))

    def save_category_price(self, m: Message) -> None:
        s = self.tg.get_state(m.chat.id, m.from_user.id)
        if not s:
            return
        name = s["data"]["name"]
        try:
            price = _to_dec(m.text)
        except Exception:
            self.bot.send_message(m.chat.id, "❌ Введите число.")
            return
        self.storage.ensure_category(name, float(price))
        self.tg.clear_state(m.chat.id, m.from_user.id)
        self.bot.send_message(m.chat.id, f"✅ Категория <b>{html.escape(name)}</b> создана.", parse_mode="HTML", reply_markup=self._back_kb(CBA_CATEGORIES))

    # --- accounts ---
    def add_accounts_start(self, c: CallbackQuery) -> None:
        self.tg.set_state(c.message.chat.id, c.message.id, c.from_user.id, STATE_ADD_ACCOUNTS, {})
        text = (
            "➕ Отправьте аккаунты в формате:\n\n"
            "<code>+79001234567|session_string</code>\n\n"
            "или с категорией:\n"
            "<code>Россия: +79001234567|session_string</code>\n\n"
            "Можно несколько строк. Новым категориям будет предложена одна цена."
        )
        kb = InlineKeyboardMarkup().add(InlineKeyboardButton("🔙 Отмена", callback_data=CBA_CATEGORIES))
        self.bot.edit_message_text(text, c.message.chat.id, c.message.id, reply_markup=kb, parse_mode="HTML")

    def add_accounts_text(self, m: Message) -> None:
        text = (m.text or "").strip()
        lines = text.splitlines()
        errors: list[str] = []
        created: list[str] = []
        pending: list[dict[str, str]] = []
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
                errors.append(f"Нет |: {line[:40]}")
                continue
            phone, session = data.split("|", 1)
            phone = phone.strip()
            session = session.strip()
            if not phone or not session:
                errors.append(f"Пустое поле: {line[:40]}")
                continue
            if cat is None:
                cat = _detect_country(phone)
            cat_row = self.storage.ensure_category(cat)
            if cat_row.get("price", 0) == 0 and cat not in created:
                created.append(cat)
            pending.append({"cat": cat, "phone": phone, "session": session})
        if created:
            self.tg.set_state(m.chat.id, m.id, m.from_user.id, STATE_ADD_ACCOUNTS_PRICE, {"pending": pending, "created": created, "errors": errors})
            text = f"🆕 Новые категории: {', '.join(created)}.\nВведите одну цену для всех новых категорий (₽):"
            self.bot.send_message(m.chat.id, text, reply_markup=self._back_kb(CBA_CATEGORIES))
            return
        added = self._flush_pending(pending)
        self.tg.clear_state(m.chat.id, m.from_user.id)
        msg = f"✅ Добавлено аккаунтов: {added}\n"
        if errors:
            msg += f"⚠️ Ошибок: {len(errors)}\n" + "\n".join(errors[:10])
        self.bot.send_message(m.chat.id, msg, reply_markup=self._back_kb(CBA_CATEGORIES))

    def add_accounts_price(self, m: Message) -> None:
        s = self.tg.get_state(m.chat.id, m.from_user.id)
        if not s:
            return
        try:
            price = _to_dec(m.text)
        except Exception:
            self.bot.send_message(m.chat.id, "❌ Введите число.")
            return
        created = s["data"]["created"]
        pending = s["data"]["pending"]
        errors = s["data"].get("errors", [])
        for cat_name in created:
            cat_row = self.storage.get_category_by_name(cat_name) or self.storage.ensure_category(cat_name)
            self.storage.update_category_price(cat_row["id"], float(price))
        added = self._flush_pending(pending)
        self.tg.clear_state(m.chat.id, m.from_user.id)
        msg = f"✅ Добавлено {added} аккаунтов. Цена {price}₽ установлена для {len(created)} категорий.\n"
        if errors:
            msg += f"⚠️ Ошибок: {len(errors)}\n" + "\n".join(errors[:10])
        self.bot.send_message(m.chat.id, msg, reply_markup=self._back_kb(CBA_CATEGORIES))

    def _flush_pending(self, pending: list[dict[str, str]]) -> int:
        added = 0
        for item in pending:
            cat_row = self.storage.ensure_category(item["cat"])
            self.storage.add_account(cat_row["id"], item["phone"], item["session"])
            added += 1
        return added

    def list_categories_for_accounts(self, c: CallbackQuery) -> None:
        cats = self.storage.get_categories()
        text = "<b>📦 Аккаунты по категориям</b>\n\n"
        if not cats:
            text += "Нет категорий."
        kb = InlineKeyboardMarkup(row_width=1)
        for cat in cats:
            accounts = self.storage.get_accounts(category_id=cat["id"])
            available = sum(1 for a in accounts if a["status"] == STATUS_AVAILABLE)
            text += f"• {html.escape(cat['name'])}: {available} свободно / {len(accounts)} всего\n"
            kb.add(InlineKeyboardButton(f"{html.escape(cat['name'])} ({available}/{len(accounts)})", callback_data=f"{CBA_ACCOUNTS_BY_CAT}{cat['id']}"))
        kb.add(InlineKeyboardButton("🔙 Назад", callback_data=CBA_MAIN))
        self.bot.edit_message_text(text, c.message.chat.id, c.message.id, reply_markup=kb, parse_mode="HTML")

    def show_accounts_by_cat(self, c: CallbackQuery, cat_id: int) -> None:
        cat = self.storage.get_category(cat_id)
        if not cat:
            return
        accounts = self.storage.get_accounts(category_id=cat_id)
        text = f"<b>{html.escape(cat['name'])}</b>\n\n"
        if not accounts:
            text += "Нет аккаунтов."
        else:
            for a in accounts[:50]:
                text += f"ID {a['id']} | <code>{a['phone']}</code> | {a['status']}\n"
            if len(accounts) > 50:
                text += f"... и ещё {len(accounts) - 50}\n"
        kb = InlineKeyboardMarkup().add(InlineKeyboardButton("🔙 Назад", callback_data=CBA_LIST_ACCOUNTS))
        self.bot.edit_message_text(text, c.message.chat.id, c.message.id, reply_markup=kb, parse_mode="HTML")

    # --- deposits / stats ---
    def show_deposits(self, c: CallbackQuery) -> None:
        deposits = self.storage.get_all_deposits(30)
        text = "<b>💰 Последние пополнения</b>\n\n"
        if not deposits:
            text += "Пока нет пополнений."
        else:
            for d in deposits:
                text += f"#{d['id']} | ID {d['user_id']} | {d['method']} | {d['amount_rub']}₽ | {d.get('status', '')}\n"
        kb = InlineKeyboardMarkup().add(InlineKeyboardButton("🔙 Назад", callback_data=CBA_MAIN))
        self.bot.edit_message_text(text, c.message.chat.id, c.message.id, reply_markup=kb, parse_mode="HTML")

    def show_stats(self, c: CallbackQuery) -> None:
        stats = self.storage.get_stats()
        text = (
            f"<b>📊 Статистика</b>\n\n"
            f"Пользователей: {stats['users']}\n"
            f"Аккаунтов всего: {stats['accounts']}\n"
            f"Свободно: {stats['available']}\n"
            f"Продано: {stats['sold']}\n"
            f"Выручка: {stats['revenue']}₽\n"
            f"Пополнений: {stats['deposits']}₽"
        )
        kb = InlineKeyboardMarkup().add(InlineKeyboardButton("🔙 Назад", callback_data=CBA_MAIN))
        self.bot.edit_message_text(text, c.message.chat.id, c.message.id, reply_markup=kb, parse_mode="HTML")

    # --- settings ---
    CFG_LABELS: dict[str, str] = {
        "support_contact": "Контакт поддержки",
        "reviews_channel": "Канал отзывов",
        "reviews_moderation_chat_id": "ID чата модерации",
        "bot_token": "Токен бота",
        "api_id": "App api_id",
        "api_hash": "App api_hash",
        "default_proxy": "Общий прокси (SOCKS/HTTP/MTProto)",
        "crypto_bot_token": "Токен Crypto Bot",
        "crypto_bot_name": "Юзернейм Crypto Bot",
        "platega_merchant_id": "Platega merchant ID",
    }

    def show_settings(self, c: CallbackQuery) -> None:
        text = "<b>⚙️ Настройки</b>\n\n"
        kb = InlineKeyboardMarkup(row_width=1)
        for key, label in self.CFG_LABELS.items():
            val = self.storage.get_config(key, "")
            if key in ("bot_token", "crypto_bot_token") and val and isinstance(val, str) and len(val) > 12:
                val = val[:8] + "..." + val[-4:]
            if key == "platega_merchant_id" and val and isinstance(val, str) and len(val) > 12:
                val = val[:8] + "..." + val[-8:]
            if key == "api_hash" and val and isinstance(val, str) and len(val) > 12:
                val = val[:8] + "..." + val[-4:]
            if key == "default_proxy" and val and isinstance(val, str) and len(val) > 30:
                val = val[:27] + "..."
            text += f"{label}: <code>{html.escape(str(val))}</code>\n"
            kb.add(InlineKeyboardButton(f"✏️ {label}", callback_data=f"{CBA_EDIT_CFG}{key}"))
        kb.add(InlineKeyboardButton("🔙 Назад", callback_data=CBA_MAIN))
        self.bot.edit_message_text(text, c.message.chat.id, c.message.id, reply_markup=kb, parse_mode="HTML")

    def edit_config_start(self, c: CallbackQuery, key: str) -> None:
        label = self.CFG_LABELS.get(key, key)
        self.tg.set_state(c.message.chat.id, c.message.id, c.from_user.id, STATE_EDIT_CFG, {"key": key})
        text = f"✏️ Введите значение для <b>{html.escape(label)}</b>:"
        kb = InlineKeyboardMarkup().add(InlineKeyboardButton("🔙 Отмена", callback_data=CBA_SETTINGS))
        self.bot.edit_message_text(text, c.message.chat.id, c.message.id, reply_markup=kb, parse_mode="HTML")

    def save_config(self, m: Message) -> None:
        s = self.tg.get_state(m.chat.id, m.from_user.id)
        if not s:
            return
        key = s["data"]["key"]
        value = (m.text or "").strip()
        self.storage.set_config(key, value)
        self.tg.clear_state(m.chat.id, m.from_user.id)
        label = self.CFG_LABELS.get(key, key)
        self.bot.send_message(m.chat.id, f"✅ <b>{html.escape(label)}</b> обновлено.", parse_mode="HTML", reply_markup=self._back_kb(CBA_SETTINGS))

    # --- pyrogram login helpers ---
    def _pyrogram_config(self) -> tuple[int, str]:
        api_id = self.storage.get_config("api_id")
        api_hash = self.storage.get_config("api_hash")
        if not api_id or not api_hash:
            return 0, ""
        return int(api_id), str(api_hash)

    def _get_default_proxy(self) -> dict[str, Any] | None:
        return _parse_proxy(self.storage.get_config("default_proxy"))

    # --- add account by phone + code ---
    def add_phone_start(self, c: CallbackQuery) -> None:
        self.tg.set_state(c.message.chat.id, c.message.id, c.from_user.id, STATE_ADD_PHONE, {"step": "phone"})
        text = "📞 Введите номер телефона аккаунта (международный формат, например +79001234567).\n\nМожно сразу указать прокси через |: <code>+79001234567|socks5 1.2.3.4:1080</code>\nЕсли прокси не указан, используется общий из настроек."
        kb = InlineKeyboardMarkup().add(InlineKeyboardButton("🔙 Отмена", callback_data=CBA_MAIN))
        self.bot.edit_message_text(text, c.message.chat.id, c.message.id, reply_markup=kb, parse_mode="HTML")

    def add_phone_text(self, m: Message) -> None:
        s = self.tg.get_state(m.chat.id, m.from_user.id)
        if not s:
            return
        data = s["data"]
        step = data.get("step")
        api_id, api_hash = self._pyrogram_config()
        if not api_id or not api_hash:
            self.bot.send_message(m.chat.id, "❌ Сначала задайте api_id и api_hash в настройках плагина.", reply_markup=self._back_kb(CBA_MAIN))
            self.tg.clear_state(m.chat.id, m.from_user.id)
            return
        if not Client:
            self.bot.send_message(m.chat.id, "❌ Pyrogram не установлен.", reply_markup=self._back_kb(CBA_MAIN))
            self.tg.clear_state(m.chat.id, m.from_user.id)
            return

        if step == "phone":
            phone = (m.text or "").strip()
            if not phone:
                self.bot.send_message(m.chat.id, "❌ Введите номер телефона.")
                return
            proxy_text = None
            if "|" in phone:
                phone, proxy_text = phone.split("|", 1)
                phone = phone.strip()
                proxy_text = proxy_text.strip()
            proxy = _parse_proxy(proxy_text) or self._get_default_proxy()
            if proxy and (proxy.get("scheme") or "").lower() == "mtproto":
                self.bot.send_message(m.chat.id, "⚠️ Pyrogram не поддерживает MTProto-прокси. Авторизация будет попытана без прокси.")
            self.tg.set_state(m.chat.id, s["mid"], m.from_user.id, STATE_ADD_PHONE, {
                **data, "step": "code", "phone": phone, "proxy": proxy,
            })
            threading.Thread(
                target=self._send_phone_code,
                args=(m.chat.id, m.from_user.id, phone, proxy, api_id, api_hash),
                daemon=True,
            ).start()
            return

        if step == "code":
            code = (m.text or "").strip()
            self.tg.set_state(m.chat.id, s["mid"], m.from_user.id, STATE_ADD_PHONE, {
                **data, "step": "password", "code": code,
            })
            self.bot.send_message(m.chat.id, "🔐 Если есть облачный пароль — введите его.\nЕсли нет — отправьте '-' или 'нет'.")
            return

        if step == "password":
            password = (m.text or "").strip()
            if password.lower() in ("", "-", "нет", "no", "none"):
                password = None
            phone = data.get("phone")
            code = data.get("code")
            proxy = data.get("proxy")
            self.tg.set_state(m.chat.id, s["mid"], m.from_user.id, STATE_ADD_PHONE, {
                **data, "step": "login", "password": password,
            })
            threading.Thread(
                target=self._login_phone_account,
                args=(m.chat.id, m.from_user.id, phone, code, password, proxy, api_id, api_hash),
                daemon=True,
            ).start()
            return

        if step == "set_price":
            try:
                price = _to_dec(m.text)
            except Exception:
                self.bot.send_message(m.chat.id, "❌ Введите число.")
                return
            category_id = data.get("category_id")
            if category_id:
                self.storage.update_category_price(category_id, float(price))
            phone = data.get("phone")
            session_string = data.get("session_string")
            proxy = data.get("proxy")
            if phone and session_string and category_id:
                self.storage.add_account(category_id, phone, session_string, proxy=proxy)
                self.bot.send_message(m.chat.id, f"✅ Аккаунт {phone} добавлен. Цена {price}₽ установлена.", reply_markup=self._back_kb(CBA_MAIN))
            else:
                self.bot.send_message(m.chat.id, f"✅ Цена {price}₽ установлена.", reply_markup=self._back_kb(CBA_MAIN))
            self.tg.clear_state(m.chat.id, m.from_user.id)

    def _send_phone_code(self, chat_id: int, user_id: int, phone: str, proxy: dict[str, Any] | None, api_id: int, api_hash: str) -> None:
        client = None
        try:
            client = Client(
                f"admin_login_{user_id}_{int(time.time())}",
                api_id=api_id,
                api_hash=api_hash,
                phone_number=phone,
                proxy=_proxy_for_pyrogram(proxy),
                in_memory=True,
                no_updates=True,
            )
            client.connect()
            sent = client.send_code(phone)
            s = self.tg.get_state(chat_id, user_id)
            if s:
                data = s["data"]
                data["phone_code_hash"] = sent.phone_code_hash
                data["client"] = client
                self.tg.set_state(chat_id, s["mid"], user_id, STATE_ADD_PHONE, data)
            self.bot.send_message(chat_id, "📩 Код отправлен на номер. Введите его:")
            client.disconnect()
        except Exception:
            logger.exception("admin send_code error")
            if client:
                try:
                    client.disconnect()
                except Exception:
                    pass
            self.bot.send_message(chat_id, "❌ Не удалось отправить код. Проверьте номер и прокси.")
            self.tg.clear_state(chat_id, user_id)

    def _login_phone_account(self, chat_id: int, user_id: int, phone: str, code: str, password: str | None,
                             proxy: dict[str, Any] | None, api_id: int, api_hash: str) -> None:
        client = None
        try:
            s = self.tg.get_state(chat_id, user_id)
            client = s["data"].get("client") if s else None
            if not client:
                client = Client(
                    f"admin_login_{user_id}_{int(time.time())}",
                    api_id=api_id,
                    api_hash=api_hash,
                    phone_number=phone,
                    proxy=_proxy_for_pyrogram(proxy),
                    in_memory=True,
                    no_updates=True,
                )
            client.connect()
            phone_code_hash = s["data"].get("phone_code_hash") if s else None
            try:
                client.sign_in(phone, phone_code_hash, code)
            except SessionPasswordNeeded:
                if not password:
                    raise
                client.check_password(password)
            session_string = client.export_session_string()
            try:
                me = client.get_me()
                phone = me.phone_number or phone
            except Exception:
                pass
            client.disconnect()
            cat_name = _detect_country(phone)
            cat = self.storage.ensure_category(cat_name)
            if cat.get("price", 0) == 0:
                new_data = {
                    "step": "set_price", "phone": phone, "session_string": session_string,
                    "proxy": proxy, "category_name": cat_name, "category_id": cat["id"],
                }
                if s:
                    self.tg.set_state(chat_id, s["mid"], user_id, STATE_ADD_PHONE, new_data)
                self.bot.send_message(
                    chat_id,
                    f"🆕 Новая категория: {cat_name}.\n💰 Введите цену (₽):",
                    reply_markup=self._back_kb(CBA_MAIN),
                )
                return
            self.storage.add_account(cat["id"], phone, session_string, proxy=proxy)
            self.tg.clear_state(chat_id, user_id)
            self.bot.send_message(chat_id, f"✅ Аккаунт {phone} добавлен в категорию {cat_name}.", reply_markup=self._back_kb(CBA_MAIN))
        except Exception:
            logger.exception("admin login_phone_account error")
            if client:
                try:
                    client.disconnect()
                except Exception:
                    pass
            self.tg.clear_state(chat_id, user_id)
            self.bot.send_message(chat_id, "❌ Ошибка авторизации. Проверьте код, пароль и прокси.", reply_markup=self._back_kb(CBA_MAIN))


def register(cardinal: Any, storage: Any, shop_bot: Any, uuid: str) -> None:
    panel = ShopAdminPanel(cardinal, storage, shop_bot, uuid)
    panel.register()
