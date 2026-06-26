from __future__ import annotations

import copy
import hashlib
import json
import logging
import posixpath
import re
import threading
import time
import zipfile
from io import BytesIO
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import requests
from telebot.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

if TYPE_CHECKING:
    from cardinal import Cardinal

NAME = "RPGM Translator"
VERSION = "1.0.0"
DESCRIPTION = "Извлечение, ручной и AI-перевод JSON/ZIP RPG Maker MV/MZ через OpenModel."
CREDITS = "@Littery + Devin"
UUID = "35f7afc0-545c-4f43-a155-5674229a87a7"
SETTINGS_PAGE = False

TRANSLATION_FORMAT = "rpgm-json-translations-v1"
INSTRUCTION = "Заполняйте только поле translation. Пустое translation оставляет исходный текст."
OPENMODEL_MESSAGES_URL = "https://api.openmodel.ai/v1/messages"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_SOURCE_LANGUAGE = "Auto"
DEFAULT_TARGET_LANGUAGE = "Russian"
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_ZIP_TOTAL_BYTES = 120 * 1024 * 1024
MAX_ZIP_FILES = 2000
MAX_BATCH_CHARS = 12000
MAX_BATCH_ITEMS = 45
CALLBACK_PREFIX = "rpgmt"

CACHE_DIR = Path("storage") / "cache" / "rpgm_translator"
LOG_DIR = Path("storage") / "logs"
CONFIG_PATH = CACHE_DIR / "config.json"
JOBS_DIR = CACHE_DIR / "jobs"

logger = logging.getLogger("FPC.rpgm_translator")
_state_lock = threading.RLock()
_user_states: dict[tuple[int, int], dict[str, Any]] = {}
_cardinal: Cardinal | None = None

JsonPath = tuple[str | int, ...]

TRANSLATABLE_KEYS = {
    "currencyUnit",
    "description",
    "displayName",
    "gameTitle",
    "message1",
    "message2",
    "message3",
    "message4",
    "name",
    "nickname",
    "note",
    "profile",
}

EVENT_STRING_PARAMETER_CODES = {
    320: (1,),
    324: (1,),
    325: (1,),
}

EVENT_LINE_CODES = {
    401: "dialogue",
    405: "scroll_text",
}

SKIPPED_KEYS = {
    "audio",
    "battleback1Name",
    "battleback2Name",
    "battlerName",
    "bgm",
    "bgs",
    "characterName",
    "faceName",
    "meta",
    "overlayName",
    "parallaxName",
    "pictureName",
    "se",
    "vehicle",
}


@dataclass(frozen=True)
class TranslationEntry:
    id: str
    path: str
    source: str
    translation: str
    kind: str
    context: str


@dataclass(frozen=True)
class MergeReport:
    applied: int
    skipped_empty: int
    skipped_same: int
    skipped_mismatch: int
    errors: list[str]


def ensure_dirs() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def default_config() -> dict[str, Any]:
    return {
        "api_key": "",
        "model": DEFAULT_MODEL,
        "source_language": DEFAULT_SOURCE_LANGUAGE,
        "target_language": DEFAULT_TARGET_LANGUAGE,
        "max_tokens": 4096,
        "request_timeout": 90,
        "retry_count": 3,
    }


def load_config() -> dict[str, Any]:
    ensure_dirs()
    config = default_config()
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                config.update(saved)
        except (OSError, json.JSONDecodeError):
            logger.exception("Failed to load RPGM Translator config")
    return config


def save_config(config: dict[str, Any]) -> None:
    ensure_dirs()
    safe_config = default_config()
    safe_config.update(config)
    CONFIG_PATH.write_text(json.dumps(safe_config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def get_state(message: Message) -> dict[str, Any] | None:
    with _state_lock:
        return _user_states.get((message.chat.id, message.from_user.id))


def set_state(chat_id: int, user_id: int, state: str, data: dict[str, Any] | None = None) -> None:
    with _state_lock:
        _user_states[(chat_id, user_id)] = {"state": state, "data": data or {}}


def clear_state(chat_id: int, user_id: int) -> None:
    with _state_lock:
        _user_states.pop((chat_id, user_id), None)


def is_authorized(c: Cardinal | None, user_id: int) -> bool:
    if c is None or c.telegram is None:
        return False
    return user_id in c.telegram.authorized_users


def get_bot(c: Cardinal):
    if c.telegram is None or c.telegram.bot is None:
        raise RuntimeError("Telegram bot is disabled")
    return c.telegram.bot


def main_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton("🤖 Автоперевод JSON/ZIP", callback_data=f"{CALLBACK_PREFIX}:auto"),
        InlineKeyboardButton("✍️ Ручной перевод JSON", callback_data=f"{CALLBACK_PREFIX}:manual"),
        InlineKeyboardButton("⚙️ API и модель", callback_data=f"{CALLBACK_PREFIX}:settings"),
        InlineKeyboardButton("ℹ️ Помощь", callback_data=f"{CALLBACK_PREFIX}:help"),
    )
    return keyboard


def settings_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton("🔑 Указать OpenModel API key", callback_data=f"{CALLBACK_PREFIX}:set_api"),
        InlineKeyboardButton("🧠 Указать модель", callback_data=f"{CALLBACK_PREFIX}:set_model"),
        InlineKeyboardButton("🌐 Язык перевода", callback_data=f"{CALLBACK_PREFIX}:set_target"),
        InlineKeyboardButton("↩️ Назад", callback_data=f"{CALLBACK_PREFIX}:menu"),
    )
    return keyboard


def config_summary(config: dict[str, Any]) -> str:
    key_state = "задан" if config.get("api_key") else "не задан"
    return (
        "<b>RPGM Translator</b>\n\n"
        f"API key: <b>{key_state}</b>\n"
        f"Модель: <code>{config.get('model') or DEFAULT_MODEL}</code>\n"
        f"Исходный язык: <code>{config.get('source_language') or DEFAULT_SOURCE_LANGUAGE}</code>\n"
        f"Язык перевода: <code>{config.get('target_language') or DEFAULT_TARGET_LANGUAGE}</code>\n\n"
        "Выберите режим работы."
    )


def help_text() -> str:
    return (
        "<b>Как пользоваться</b>\n\n"
        "<b>Автоперевод JSON/ZIP</b>: отправьте RPG Maker .json или .zip с JSON-файлами. "
        "Плагин переведёт извлечённые строки через OpenModel и вернёт готовый файл.\n\n"
        "<b>Ручной перевод JSON</b>: отправьте оригинальный .json, получите translations.json, "
        "заполните поля <code>translation</code> и отправьте файл обратно — плагин соберёт переведённый JSON.\n\n"
        "Сохраняются RPG Maker escape-коды вроде <code>\\V[1]</code>, <code>\\N[2]</code>, <code>\\C[3]</code>."
    )


def path_to_pointer(path: JsonPath) -> str:
    if not path:
        return ""
    parts: list[str] = []
    for item in path:
        value = str(item).replace("~", "~0").replace("/", "~1")
        parts.append(value)
    return "/" + "/".join(parts)


def pointer_to_path(pointer: str) -> JsonPath:
    if pointer == "":
        return ()
    if not pointer.startswith("/"):
        raise ValueError(f"Bad JSON pointer: {pointer}")
    parts: list[str | int] = []
    for raw in pointer[1:].split("/"):
        value = raw.replace("~1", "/").replace("~0", "~")
        parts.append(int(value) if value.isdigit() else value)
    return tuple(parts)


def read_path(data: Any, path: JsonPath) -> Any:
    current = data
    for part in path:
        if isinstance(part, int):
            if not isinstance(current, list):
                raise TypeError(f"Expected list at {path_to_pointer(path)}")
            current = current[part]
        else:
            if not isinstance(current, dict):
                raise TypeError(f"Expected object at {path_to_pointer(path)}")
            current = current[part]
    return current


def write_path(data: Any, path: JsonPath, value: Any) -> None:
    if not path:
        raise ValueError("Refusing to replace the whole JSON document")
    parent = read_path(data, path[:-1])
    last = path[-1]
    if isinstance(last, int):
        if not isinstance(parent, list):
            raise TypeError(f"Expected list at {path_to_pointer(path[:-1])}")
        parent[last] = value
    else:
        if not isinstance(parent, dict):
            raise TypeError(f"Expected object at {path_to_pointer(path[:-1])}")
        parent[last] = value


def stable_json_hash(data: Any) -> str:
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_json_bytes(raw: bytes) -> Any:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Файл должен быть в UTF-8/UTF-8-BOM.") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Некорректный JSON: строка {exc.lineno}, колонка {exc.colno}.") from exc


def dumps_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def extract_translation_package(data: Any, source_name: str = "source.json") -> dict[str, Any]:
    entries = extract_entries(data)
    return {
        "meta": {
            "format": TRANSLATION_FORMAT,
            "source_name": source_name,
            "source_hash": stable_json_hash(data),
            "entry_count": len(entries),
            "instruction": INSTRUCTION,
        },
        "entries": [asdict(entry) for entry in entries],
    }


def extract_entries(data: Any) -> list[TranslationEntry]:
    raw_entries: list[tuple[JsonPath, str, str, str]] = []
    walk_json(data, (), raw_entries)
    entries: list[TranslationEntry] = []
    for index, (path, source, kind, context) in enumerate(raw_entries, start=1):
        entries.append(
            TranslationEntry(
                id=f"{index:05d}",
                path=path_to_pointer(path),
                source=source,
                translation="",
                kind=kind,
                context=context,
            )
        )
    return entries


def merge_translation_package(original: Any, package: dict[str, Any]) -> tuple[Any, MergeReport]:
    merged = copy.deepcopy(original)
    errors: list[str] = []
    applied = 0
    skipped_empty = 0
    skipped_same = 0
    skipped_mismatch = 0

    meta = package.get("meta")
    if isinstance(meta, dict):
        package_format = meta.get("format")
        if package_format and package_format != TRANSLATION_FORMAT:
            errors.append(f"Неподдерживаемый формат перевода: {package_format}")
        source_hash = meta.get("source_hash")
        if source_hash and source_hash != stable_json_hash(original):
            errors.append("Хэш оригинала не совпадает: перевод сделан для другого JSON.")

    entries = package.get("entries")
    if not isinstance(entries, list):
        raise ValueError("В переводе должен быть список entries.")

    for index, item in enumerate(entries, start=1):
        if not isinstance(item, dict):
            errors.append(f"entries[{index}] не объект, пропущено.")
            continue

        pointer = item.get("path")
        source = item.get("source")
        translation = item.get("translation")
        if not isinstance(pointer, str) or not isinstance(source, str):
            errors.append(f"entries[{index}] без path/source, пропущено.")
            continue
        if not isinstance(translation, str) or translation == "":
            skipped_empty += 1
            continue

        try:
            path = pointer_to_path(pointer)
            current = read_path(merged, path)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            errors.append(f"{pointer}: путь не найден ({exc}).")
            continue

        if current != source:
            skipped_mismatch += 1
            errors.append(f"{pointer}: исходный текст не совпадает, строка пропущена.")
            continue
        if translation == source:
            skipped_same += 1
            continue

        write_path(merged, path, translation)
        applied += 1

    return merged, MergeReport(applied, skipped_empty, skipped_same, skipped_mismatch, errors)


def walk_json(data: Any, path: JsonPath, entries: list[tuple[JsonPath, str, str, str]]) -> None:
    if isinstance(data, dict):
        if is_event_command(data):
            collect_event_command(data, path, entries)
            return

        for key, value in data.items():
            child_path = (*path, key)
            if key in SKIPPED_KEYS:
                continue
            if isinstance(value, str) and should_extract_string(key, child_path, value):
                entries.append((child_path, value, key_to_kind(key, child_path), context_for(child_path)))
            else:
                walk_json(value, child_path, entries)
        return

    if isinstance(data, list):
        for index, value in enumerate(data):
            child_path = (*path, index)
            if isinstance(value, str) and should_extract_array_string(child_path, value):
                entries.append((child_path, value, "system_term", context_for(child_path)))
            else:
                walk_json(value, child_path, entries)


def is_event_command(data: dict[str, Any]) -> bool:
    return isinstance(data.get("code"), int) and isinstance(data.get("parameters"), list) and (
        "indent" in data or len(data) <= 4
    )


def collect_event_command(command: dict[str, Any], path: JsonPath, entries: list[tuple[JsonPath, str, str, str]]) -> None:
    code = command["code"]
    parameters = command["parameters"]

    if code in EVENT_LINE_CODES and parameters and isinstance(parameters[0], str):
        add_if_not_blank(entries, (*path, "parameters", 0), parameters[0], EVENT_LINE_CODES[code])
        return

    if code == 102 and parameters and isinstance(parameters[0], list):
        for index, choice in enumerate(parameters[0]):
            if isinstance(choice, str):
                add_if_not_blank(entries, (*path, "parameters", 0, index), choice, "choice")
        return

    if code == 402 and len(parameters) > 1 and isinstance(parameters[1], str):
        add_if_not_blank(entries, (*path, "parameters", 1), parameters[1], "choice_branch")
        return

    for parameter_index in EVENT_STRING_PARAMETER_CODES.get(code, ()):
        if len(parameters) > parameter_index and isinstance(parameters[parameter_index], str):
            add_if_not_blank(entries, (*path, "parameters", parameter_index), parameters[parameter_index], "event_parameter")


def add_if_not_blank(entries: list[tuple[JsonPath, str, str, str]], path: JsonPath, value: str, kind: str) -> None:
    if value.strip():
        entries.append((path, value, kind, context_for(path)))


def should_extract_string(key: str, path: JsonPath, value: str) -> bool:
    if not value.strip():
        return False
    if key == "name" and "events" in path:
        return False
    if key in TRANSLATABLE_KEYS:
        return True
    return "terms" in path


def should_extract_array_string(path: JsonPath, value: str) -> bool:
    return bool(value.strip()) and "terms" in path


def key_to_kind(key: str, path: JsonPath) -> str:
    if "terms" in path:
        return "system_term"
    if key in {"message1", "message2", "message3", "message4"}:
        return "battle_message"
    if key == "note":
        return "note"
    if key in {"description", "profile"}:
        return "description"
    return "database_text"


def context_for(path: JsonPath) -> str:
    if len(path) <= 1:
        return path_to_pointer(path)
    return path_to_pointer(path[:-1])


def clean_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-zА-Яа-я0-9._ -]+", "_", name).strip(". ")
    return cleaned or "file.json"


def job_dir() -> Path:
    ensure_dirs()
    path = JOBS_DIR / f"{int(time.time())}_{uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def chunk_entries(entries: list[dict[str, Any]]) -> list[list[dict[str, str]]]:
    chunks: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    current_chars = 0
    for item in entries:
        source = item.get("source")
        if not isinstance(source, str) or not source.strip():
            continue
        compact = {
            "id": str(item.get("id", "")),
            "source": source,
            "kind": str(item.get("kind", "")),
            "context": str(item.get("context", "")),
        }
        item_chars = len(source) + len(compact["context"]) + 40
        if current and (len(current) >= MAX_BATCH_ITEMS or current_chars + item_chars > MAX_BATCH_CHARS):
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(compact)
        current_chars += item_chars
    if current:
        chunks.append(current)
    return chunks


def build_translation_prompt(config: dict[str, Any], batch: list[dict[str, str]]) -> str:
    source_language = config.get("source_language") or DEFAULT_SOURCE_LANGUAGE
    target_language = config.get("target_language") or DEFAULT_TARGET_LANGUAGE
    payload = json.dumps(batch, ensure_ascii=False)
    return (
        "Translate RPG Maker MV/MZ game strings.\n"
        f"Source language: {source_language}. Target language: {target_language}.\n"
        "Rules:\n"
        "- Return only a valid JSON array, no markdown.\n"
        "- Every item must be {\"id\": string, \"translation\": string}.\n"
        "- Preserve RPG Maker escape codes and placeholders exactly: \\V[n], \\N[n], \\C[n], %1, {name}, <tags>.\n"
        "- Keep line breaks and speaker names where they matter.\n"
        "- Translate naturally for an RPG UI/dialogue context.\n\n"
        f"Input JSON array:\n{payload}"
    )


def parse_model_json(text: str) -> list[dict[str, Any]]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\[[\s\S]*\]", stripped)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, list):
        raise ValueError("OpenModel returned JSON, but not an array")
    result: list[dict[str, Any]] = []
    for item in parsed:
        if isinstance(item, dict):
            result.append(item)
    return result


def call_openmodel(config: dict[str, Any], prompt: str) -> str:
    api_key = str(config.get("api_key") or "").strip()
    if not api_key:
        raise ValueError("OpenModel API key не задан. Откройте меню API и модель.")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
    }
    body = {
        "model": str(config.get("model") or DEFAULT_MODEL),
        "max_tokens": int(config.get("max_tokens") or 4096),
        "messages": [{"role": "user", "content": prompt}],
    }
    timeout = int(config.get("request_timeout") or 90)
    retries = max(1, int(config.get("retry_count") or 3))
    last_error: Exception | None = None

    for attempt in range(retries):
        try:
            response = requests.post(OPENMODEL_MESSAGES_URL, headers=headers, json=body, timeout=timeout)
            if response.status_code in {429, 500, 502, 503, 504} and attempt + 1 < retries:
                time.sleep(2 + attempt * 2)
                continue
            if not response.ok:
                try:
                    details = response.json()
                except ValueError:
                    details = response.text[:500]
                raise RuntimeError(f"OpenModel HTTP {response.status_code}: {details}")
            payload = response.json()
            content = payload.get("content")
            if not isinstance(content, list) or not content:
                raise RuntimeError("OpenModel вернул пустой content")
            texts: list[str] = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                    texts.append(part["text"])
            if not texts:
                raise RuntimeError("OpenModel response не содержит text")
            return "\n".join(texts)
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(2 + attempt * 2)
                continue
            break

    if last_error is None:
        raise RuntimeError("OpenModel request failed")
    raise last_error


def auto_translate_package(package: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    translated = copy.deepcopy(package)
    entries = translated.get("entries")
    if not isinstance(entries, list):
        raise ValueError("В translation package нет списка entries")

    entry_by_id = {str(item.get("id")): item for item in entries if isinstance(item, dict)}
    for batch in chunk_entries(entries):
        prompt = build_translation_prompt(config, batch)
        response_text = call_openmodel(config, prompt)
        translated_items = parse_model_json(response_text)
        for item in translated_items:
            item_id = str(item.get("id", ""))
            translation = item.get("translation")
            if item_id in entry_by_id and isinstance(translation, str) and translation.strip():
                entry_by_id[item_id]["translation"] = translation
    return translated


def translate_json_bytes(raw: bytes, source_name: str, config: dict[str, Any]) -> tuple[bytes, MergeReport, int]:
    original = load_json_bytes(raw)
    package = extract_translation_package(original, source_name)
    entry_count = int(package["meta"]["entry_count"])
    if entry_count == 0:
        return dumps_json(original).encode("utf-8"), MergeReport(0, 0, 0, 0, []), 0
    translated_package = auto_translate_package(package, config)
    merged, report = merge_translation_package(original, translated_package)
    return dumps_json(merged).encode("utf-8"), report, entry_count


def safe_zip_name(name: str) -> str | None:
    normalized = posixpath.normpath(name.replace("\\", "/"))
    if normalized.startswith("../") or normalized == ".." or normalized.startswith("/"):
        return None
    return normalized


def translate_zip_bytes(raw: bytes, config: dict[str, Any], output_path: Path) -> tuple[int, int, list[str]]:
    translated_files = 0
    copied_files = 0
    errors: list[str] = []
    total_uncompressed = 0

    with zipfile.ZipFile(BytesIO(raw), "r") as source_zip, zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as target_zip:
        infos = source_zip.infolist()
        if len(infos) > MAX_ZIP_FILES:
            raise ValueError(f"В архиве слишком много файлов: {len(infos)} > {MAX_ZIP_FILES}")
        for info in infos:
            safe_name = safe_zip_name(info.filename)
            if safe_name is None:
                errors.append(f"Опасный путь пропущен: {info.filename}")
                continue
            total_uncompressed += info.file_size
            if total_uncompressed > MAX_ZIP_TOTAL_BYTES:
                raise ValueError("ZIP слишком большой после распаковки.")
            if info.is_dir():
                target_zip.writestr(info, b"")
                continue

            data = source_zip.read(info)
            if safe_name.lower().endswith(".json"):
                try:
                    translated_data, report, entry_count = translate_json_bytes(data, posixpath.basename(safe_name), config)
                    target_zip.writestr(safe_name, translated_data)
                    if entry_count:
                        translated_files += 1
                    else:
                        copied_files += 1
                    if report.errors:
                        errors.extend(f"{safe_name}: {error}" for error in report.errors[:5])
                except Exception as exc:
                    target_zip.writestr(safe_name, data)
                    copied_files += 1
                    errors.append(f"{safe_name}: {exc}")
            else:
                target_zip.writestr(safe_name, data)
                copied_files += 1
    return translated_files, copied_files, errors


def save_document(bot, message: Message, directory: Path) -> tuple[Path, str]:
    document = message.document
    if document is None:
        raise ValueError("Отправьте файл документом.")
    if document.file_size and document.file_size > MAX_FILE_BYTES:
        raise ValueError(f"Файл слишком большой: максимум {MAX_FILE_BYTES // 1024 // 1024} MB.")
    filename = clean_filename(document.file_name or "file.json")
    file_info = bot.get_file(document.file_id)
    data = bot.download_file(file_info.file_path)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"Файл слишком большой: максимум {MAX_FILE_BYTES // 1024 // 1024} MB.")
    path = directory / filename
    path.write_bytes(data)
    return path, filename


def send_document(bot, chat_id: int, path: Path, caption: str, visible_name: str | None = None) -> None:
    with path.open("rb") as file_obj:
        bot.send_document(chat_id, file_obj, visible_file_name=visible_name or path.name, caption=caption)


def report_text(report: MergeReport, entry_count: int) -> str:
    return (
        f"Строк найдено: {entry_count}\n"
        f"Применено: {report.applied}\n"
        f"Пустых: {report.skipped_empty}\n"
        f"Без изменений: {report.skipped_same}\n"
        f"Несовпадений: {report.skipped_mismatch}\n"
        f"Ошибок: {len(report.errors)}"
    )


def handle_command(message: Message) -> None:
    c = _cardinal
    if not is_authorized(c, message.from_user.id):
        return
    bot = get_bot(c)
    bot.send_message(message.chat.id, config_summary(load_config()), reply_markup=main_keyboard())


def handle_callback(call: CallbackQuery) -> None:
    c = _cardinal
    if not is_authorized(c, call.from_user.id):
        return
    bot = get_bot(c)
    action = call.data.split(":", 1)[1] if call.data and ":" in call.data else "menu"
    bot.answer_callback_query(call.id)

    if action == "menu":
        bot.edit_message_text(config_summary(load_config()), call.message.chat.id, call.message.message_id, reply_markup=main_keyboard())
        return
    if action == "help":
        bot.edit_message_text(help_text(), call.message.chat.id, call.message.message_id, reply_markup=main_keyboard())
        return
    if action == "settings":
        bot.edit_message_text(config_summary(load_config()), call.message.chat.id, call.message.message_id, reply_markup=settings_keyboard())
        return
    if action == "set_api":
        set_state(call.message.chat.id, call.from_user.id, "wait_api_key")
        bot.send_message(call.message.chat.id, "Отправьте OpenModel API key. Сообщение будет удалено, если Telegram позволит.")
        return
    if action == "set_model":
        set_state(call.message.chat.id, call.from_user.id, "wait_model")
        bot.send_message(call.message.chat.id, f"Отправьте model id. По умолчанию: {DEFAULT_MODEL}")
        return
    if action == "set_target":
        set_state(call.message.chat.id, call.from_user.id, "wait_target")
        bot.send_message(call.message.chat.id, "Отправьте язык перевода, например: Russian, English, Ukrainian.")
        return
    if action == "manual":
        set_state(call.message.chat.id, call.from_user.id, "wait_manual_original")
        bot.send_message(call.message.chat.id, "Отправьте оригинальный RPG Maker .json документом.")
        return
    if action == "auto":
        config = load_config()
        if not config.get("api_key"):
            bot.send_message(call.message.chat.id, "Сначала задайте OpenModel API key в меню API и модель.")
            return
        set_state(call.message.chat.id, call.from_user.id, "wait_auto_file")
        bot.send_message(call.message.chat.id, "Отправьте RPG Maker .json или .zip документом для автоперевода.")


def handle_state_message(message: Message) -> None:
    c = _cardinal
    if not is_authorized(c, message.from_user.id):
        return
    state = get_state(message)
    if state is None:
        return
    bot = get_bot(c)
    state_name = state["state"]

    if state_name in {"wait_api_key", "wait_model", "wait_target"}:
        handle_text_state(bot, message, state_name)
        return

    if message.document is None:
        bot.send_message(message.chat.id, "Нужно отправить файл документом.")
        return

    clear_state(message.chat.id, message.from_user.id)
    threading.Thread(target=process_document_state, args=(bot, message, state), daemon=True).start()


def handle_text_state(bot, message: Message, state_name: str) -> None:
    text = (message.text or "").strip()
    if not text:
        bot.send_message(message.chat.id, "Пустое значение не сохранено.")
        return

    config = load_config()
    if state_name == "wait_api_key":
        config["api_key"] = text
        reply = "OpenModel API key сохранён."
        try:
            bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            logger.debug("Failed to delete API key message", exc_info=True)
    elif state_name == "wait_model":
        config["model"] = text
        reply = f"Модель сохранена: <code>{text}</code>"
    else:
        config["target_language"] = text
        reply = f"Язык перевода сохранён: <code>{text}</code>"
    save_config(config)
    clear_state(message.chat.id, message.from_user.id)
    bot.send_message(message.chat.id, reply, reply_markup=settings_keyboard())


def process_document_state(bot, message: Message, state: dict[str, Any]) -> None:
    directory = job_dir()
    try:
        bot.send_chat_action(message.chat.id, "upload_document")
        source_path, filename = save_document(bot, message, directory)
        state_name = state["state"]
        if state_name == "wait_manual_original":
            process_manual_original(bot, message, source_path, filename, directory)
        elif state_name == "wait_manual_translation":
            original_path = Path(state["data"]["original_path"])
            process_manual_translation(bot, message, original_path, source_path, directory)
        elif state_name == "wait_auto_file":
            process_auto_file(bot, message, source_path, filename, directory)
    except Exception as exc:
        logger.exception("RPGM Translator failed")
        bot.send_message(message.chat.id, f"Ошибка: {exc}")


def process_manual_original(bot, message: Message, source_path: Path, filename: str, directory: Path) -> None:
    original = load_json_bytes(source_path.read_bytes())
    package = extract_translation_package(original, filename)
    package_path = directory / "translations.json"
    package_path.write_text(dumps_json(package), encoding="utf-8")
    set_state(
        message.chat.id,
        message.from_user.id,
        "wait_manual_translation",
        {"original_path": str(source_path), "source_name": filename},
    )
    send_document(
        bot,
        message.chat.id,
        package_path,
        f"Найдено строк: {package['meta']['entry_count']}. Заполните translation и отправьте этот файл обратно.",
        "translations.json",
    )


def process_manual_translation(bot, message: Message, original_path: Path, translation_path: Path, directory: Path) -> None:
    original = load_json_bytes(original_path.read_bytes())
    package = load_json_bytes(translation_path.read_bytes())
    merged, report = merge_translation_package(original, package)
    output_name = clean_filename(f"{original_path.stem}_translated.json")
    output_path = directory / output_name
    output_path.write_text(dumps_json(merged), encoding="utf-8")
    send_document(bot, message.chat.id, output_path, report_text(report, len(package.get("entries", []))), output_name)


def process_auto_file(bot, message: Message, source_path: Path, filename: str, directory: Path) -> None:
    config = load_config()
    suffix = source_path.suffix.lower()
    if suffix == ".json":
        translated_data, report, entry_count = translate_json_bytes(source_path.read_bytes(), filename, config)
        output_name = clean_filename(f"{source_path.stem}_translated.json")
        output_path = directory / output_name
        output_path.write_bytes(translated_data)
        send_document(bot, message.chat.id, output_path, report_text(report, entry_count), output_name)
        return

    if suffix == ".zip":
        output_name = clean_filename(f"{source_path.stem}_translated.zip")
        output_path = directory / output_name
        translated_files, copied_files, errors = translate_zip_bytes(source_path.read_bytes(), config, output_path)
        caption = f"Переведено JSON-файлов: {translated_files}\nСкопировано файлов: {copied_files}\nОшибок: {len(errors)}"
        if errors:
            error_path = directory / "translation_errors.txt"
            error_path.write_text("\n".join(errors[:200]), encoding="utf-8")
            caption += "\nПервые ошибки сохранены в storage/cache/rpgm_translator/jobs."
        send_document(bot, message.chat.id, output_path, caption, output_name)
        return

    raise ValueError("Поддерживаются только .json и .zip файлы.")


def has_plugin_state(message: Message) -> bool:
    return get_state(message) is not None


def prepare_storage(c: Cardinal) -> None:
    ensure_dirs()
    if not CONFIG_PATH.exists():
        save_config(default_config())


def init_plugin(c: Cardinal) -> None:
    global _cardinal
    _cardinal = c
    ensure_dirs()

    if c.telegram is None or c.telegram.bot is None:
        logger.warning("Telegram disabled; RPGM Translator UI is unavailable")
        return

    c.add_telegram_commands(UUID, [("rpgmtr", "RPGM JSON/ZIP переводчик", True)])
    c.telegram.msg_handler(handle_command, commands=["rpgmtr"])
    c.telegram.msg_handler(handle_state_message, func=has_plugin_state, content_types=["text", "document"])
    c.telegram.cbq_handler(handle_callback, lambda call: bool(call.data and call.data.startswith(f"{CALLBACK_PREFIX}:")))


def cleanup_on_stop(c: Cardinal | None = None) -> None:
    with _state_lock:
        _user_states.clear()


BIND_TO_PRE_INIT = [prepare_storage]
BIND_TO_POST_INIT = [init_plugin]
BIND_TO_PRE_STOP = [cleanup_on_stop]
BIND_TO_DELETE = cleanup_on_stop
