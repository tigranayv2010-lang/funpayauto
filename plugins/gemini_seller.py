from __future__ import annotations

import json
import logging
import os
import re
import time
from threading import Thread, Lock
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from cardinal import Cardinal

import requests
import telebot
from telebot.types import InlineKeyboardMarkup as K, InlineKeyboardButton as B
from FunPayAPI.updater.events import NewMessageEvent, NewOrderEvent
from FunPayAPI.common.enums import MessageTypes
from tg_bot import CBT, utils

# ─────────────────────────── Метаданные плагина ───────────────────────────────

NAME = "Gemini JIT Procurement Plugin"
VERSION = "3.1.0"
DESCRIPTION = (
    "Плагин динамической JIT-закупки ссылок Gemini.\n\n"
    "• Приоритетная очередь: сначала выдаёт загруженные вручную ссылки.\n"
    "• Если свои ссылки кончились — закупает у поставщика через API.\n"
    "• Автоматически проверяет наличие раз в 15 минут.\n"
    "• Скрывает лоты, если нет ни своих ссылок, ни остатков у поставщика.\n"
    "• Обработка заказов через NewOrderEvent (надёжно).\n"
    "• Защита от дублей, оверселлинга, блокирующих вызовов."
)
CREDITS = "@tigranayvvv"
UUID = "7b4c9e12-a5d6-4f88-bc13-9d7e6f4a2b01"
SETTINGS_PAGE = True

logger = logging.getLogger("FPC.gemini_jit")
LOGGER_PREFIX = "[GEMINI JIT]"

PLUGIN_FOLDER = f"storage/plugins/{UUID}/"
SETTINGS_FILE = f"{PLUGIN_FOLDER}settings.json"
LINKS_FILE = f"{PLUGIN_FOLDER}links.txt"

API_BASE_URL = "https://worker-production-53ca.up.railway.app/api"

# ─────────────────────────── Защита от дублей ──────────────────────────────────

_processed_orders: set[str] = set()
_processed_orders_lock = Lock()
_links_lock = Lock()
_last_known_stock_status: Optional[bool] = None


def _mark_order_processed(order_id: str) -> bool:
    with _processed_orders_lock:
        if order_id in _processed_orders:
            return False
        _processed_orders.add(order_id)
        if len(_processed_orders) > 1000:
            oldest = next(iter(_processed_orders))
            _processed_orders.discard(oldest)
        return True


# ─────────────────────────── Работа с локальными ссылками ─────────────────────


def get_links_count() -> int:
    """Возвращает количество доступных ссылок в файле."""
    if not os.path.exists(LINKS_FILE):
        return 0
    try:
        with open(LINKS_FILE, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
        return len(lines)
    except Exception:
        return 0


def take_link() -> Optional[str]:
    """Атомарно забирает одну ссылку (первую строку) из файла."""
    with _links_lock:
        if not os.path.exists(LINKS_FILE):
            return None
        try:
            with open(LINKS_FILE, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f if line.strip()]
            if not lines:
                return None

            link = lines[0]
            remaining = lines[1:]

            with open(LINKS_FILE, "w", encoding="utf-8") as f:
                f.write("\n".join(remaining))
                if remaining:
                    f.write("\n")

            return link
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка чтения файла ссылок: {e}")
            return None


def add_links(text: str) -> int:
    """Добавляет ссылки из текста в файл. Возвращает кол-во добавленных."""
    with _links_lock:
        new_lines = [line.strip() for line in text.strip().split("\n") if line.strip()]
        if not new_lines:
            return 0

        os.makedirs(PLUGIN_FOLDER, exist_ok=True)

        existing = []
        if os.path.exists(LINKS_FILE):
            try:
                with open(LINKS_FILE, "r", encoding="utf-8") as f:
                    existing = [l.strip() for l in f if l.strip()]
            except Exception:
                pass

        existing.extend(new_lines)
        with open(LINKS_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(existing) + "\n")

        return len(new_lines)


def clear_links() -> int:
    """Очищает файл ссылок. Возвращает кол-во удалённых."""
    with _links_lock:
        count = 0
        if os.path.exists(LINKS_FILE):
            try:
                with open(LINKS_FILE, "r", encoding="utf-8") as f:
                    count = len([l for l in f if l.strip()])
            except Exception:
                pass
            with open(LINKS_FILE, "w", encoding="utf-8") as f:
                pass
        return count


# ─────────────────────────── Настройки по умолчанию ────────────────────────────

DEFAULT_SETTINGS = {
    "enabled": True,
    "supplier_api_key": "",
    "target_product_id": 1,
    "funpay_lots": []
    # Формат лота: {"lot_id": "123", "price": "500", "title": "..."}
}

# ─────────────────────────── Callback / State константы ───────────────────────

CBT_MAIN = "gjt:main"
CBT_TOGGLE = "gjt:toggle"
CBT_SET_SUPPLIER_KEY = "gjt:set_skey"
CBT_SET_PRODUCT_ID = "gjt:set_pid"
CBT_ADD_LOT = "gjt:add_lot"
CBT_DEL_LOT_MENU = "gjt:del_menu"
CBT_DEL_LOT = "gjt:del_lot"
CBT_FORCE_SYNC = "gjt:sync"
CBT_TEST = "gjt:test"
CBT_ADD_LINKS = "gjt:add_lnk"
CBT_CLEAR_LINKS = "gjt:clr_lnk"

STATE_SET_SUPPLIER_KEY = f"{UUID}:state_skey"
STATE_SET_PRODUCT_ID = f"{UUID}:state_pid"
STATE_ADD_LOT = f"{UUID}:state_add_lot"
STATE_ADD_LINKS = f"{UUID}:state_add_lnk"

# ─────────────────────────── Regex для извлечения order_id ─────────────────────

ORDER_ID_RE = re.compile(r"#(\w+)")

# ─────────────────────────── Работа с настройками ──────────────────────────────


def load_settings() -> dict:
    if not os.path.exists(SETTINGS_FILE):
        return json.loads(json.dumps(DEFAULT_SETTINGS))
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return json.loads(json.dumps(DEFAULT_SETTINGS))

    if "funpay_lots" not in data:
        data["funpay_lots"] = []

    # Миграция v3.0 → v3.1: убираем поле mode из лотов
    for lot in data.get("funpay_lots", []):
        lot.pop("mode", None)

    for key, value in DEFAULT_SETTINGS.items():
        if key not in data:
            data[key] = json.loads(json.dumps(value))
    return data


def save_settings(settings: dict) -> None:
    os.makedirs(PLUGIN_FOLDER, exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


# ─────────────────────────── Интеграция с API Поставщика ───────────────────────


def get_supplier_data(settings: dict) -> tuple[int, float]:
    """Получает остаток товара и баланс реселлера у поставщика."""
    s_key = (settings.get("supplier_api_key") or "").strip()
    p_id = int(settings.get("target_product_id", 1))
    if not s_key:
        return 0, 0.0

    headers = {"X-API-Key": s_key, "Content-Type": "application/json"}
    stock, balance = 0, 0.0

    try:
        res_me = requests.get(f"{API_BASE_URL}/me", headers=headers, timeout=10)
        if res_me.status_code == 200:
            data_me = res_me.json()
            if data_me.get("ok"):
                balance = data_me.get("user", {}).get("balance", 0.0)

        res_prod = requests.get(f"{API_BASE_URL}/products", headers=headers, timeout=10)
        if res_prod.status_code == 200:
            data_prod = res_prod.json()
            if data_prod.get("ok"):
                for p in data_prod.get("products", []):
                    if p.get("id") == p_id:
                        stock = p.get("stock_count", 0)
                        break
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка запроса к API поставщика: {e}")

    return stock, balance


def buy_from_supplier(settings: dict) -> Optional[str]:
    """Покупает 1 товар у поставщика. Возвращает ссылку или None."""
    s_key = (settings.get("supplier_api_key") or "").strip()
    p_id = int(settings.get("target_product_id", 1))
    if not s_key:
        return None

    headers = {"X-API-Key": s_key, "Content-Type": "application/json"}
    payload = {"product_id": p_id, "quantity": 1}

    try:
        res = requests.post(f"{API_BASE_URL}/buy", headers=headers, json=payload, timeout=15)
        if res.status_code == 200:
            data = res.json()
            if data.get("ok"):
                items = data.get("items", [])
                if items:
                    return items[0]
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Критическая ошибка JIT-выкупа: {e}")
    return None


def shorten_url(long_url: str) -> str:
    """Сокращает URL через clck.ru. При ошибке возвращает оригинал."""
    try:
        res = requests.get("https://clck.ru/--", params={"url": long_url}, timeout=10)
        if res.status_code == 200:
            return res.text.strip()
    except Exception as e:
        logger.warning(f"{LOGGER_PREFIX} clck.ru недоступен: {e}")
    return long_url


# ─────────────────────────── Синхронизация с FunPay ────────────────────────────


def sync_funpay_status(cardinal: "Cardinal", settings: dict, force_stock: Optional[int] = None):
    """Обновляет количество и видимость всех привязанных лотов на FunPay."""
    global _last_known_stock_status
    lots = settings.get("funpay_lots", [])
    if not lots:
        return

    supplier_stock = force_stock if force_stock is not None else get_supplier_data(settings)[0]
    local_links = get_links_count()

    # Лот активен если есть свои ссылки ИЛИ есть остатки у поставщика
    total_available = local_links + supplier_stock
    quantity = min(total_available, 10) if total_available > 0 else 0
    current_status = quantity > 0

    for lot in lots:
        try:
            lot_fields = cardinal.account.get_lot_fields(int(lot["lot_id"]))
            lot_fields.price = float(lot["price"])
            lot_fields.amount = quantity
            lot_fields.active = current_status
            cardinal.account.save_offer(lot_fields)
            logger.info(f"{LOGGER_PREFIX} Лот {lot['lot_id']} синхронизирован. Свои: {local_links}, API: {supplier_stock}, qty: {quantity}")
            time.sleep(1.5)
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка обновления лота {lot['lot_id']}: {e}")

    # Уведомление в ТГ при смене статуса
    if _last_known_stock_status is not None and _last_known_stock_status != current_status:
        msg = (
            "🟢 <b>Gemini JIT:</b> Товар появился! Привязанные лоты активированы."
            if current_status else
            "🔴 <b>Gemini JIT:</b> Товар закончился (и свои ссылки, и у поставщика)! Лоты скрыты."
        )
        if getattr(cardinal, "telegram", None) and getattr(cardinal.telegram, "bot", None):
            for admin_id in cardinal.telegram.authorized_users:
                try:
                    cardinal.telegram.bot.send_message(admin_id, msg, parse_mode="HTML")
                except Exception:
                    pass

    _last_known_stock_status = current_status


# ─────────────────────────── Фоновый поток ───────────────────────────────────


def _stock_monitoring_thread(cardinal: "Cardinal"):
    time.sleep(10)
    while True:
        try:
            settings = load_settings()
            if settings.get("enabled", True) and settings.get("supplier_api_key"):
                sync_funpay_status(cardinal, settings)
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка в потоке мониторинга: {e}")
        time.sleep(900)


# ─────────────────────────── Ядро выдачи товара ──────────────────────────────


def _notify_admins(cardinal: "Cardinal", text: str):
    if getattr(cardinal, "telegram", None) and getattr(cardinal.telegram, "bot", None):
        for admin_id in cardinal.telegram.authorized_users:
            try:
                cardinal.telegram.bot.send_message(admin_id, text, parse_mode="HTML")
            except Exception:
                pass


def _find_matched_lot(lots: list, order=None, text: str = "") -> Optional[dict]:
    """Ищет подходящий лот по цене или lot_id."""
    # 1) По цене заказа
    if order is not None:
        order_price = getattr(order, "price", None)
        if order_price is not None:
            try:
                order_price_f = float(str(order_price).replace(",", "."))
            except (ValueError, TypeError):
                order_price_f = None

            if order_price_f is not None:
                for lot in lots:
                    try:
                        lot_price = float(str(lot["price"]).replace(",", "."))
                    except (ValueError, TypeError):
                        continue
                    if abs(lot_price - order_price_f) < 0.01:
                        return lot

    # 2) По lot_id в тексте
    search_text = text
    if order is not None and not search_text:
        search_text = str(getattr(order, "description", "") or "").lower()

    if search_text:
        for lot in lots:
            l_id = str(lot.get("lot_id", "")).strip()
            if l_id and re.search(r"\b" + re.escape(l_id) + r"\b", search_text):
                return lot

    # 3) Фолбэк по цене в тексте
    if search_text:
        for lot in lots:
            lot_price = str(lot.get("price", "")).strip()
            if lot_price and re.search(r"\b" + re.escape(lot_price) + r"\b", search_text):
                return lot

    return None


def _process_delivery(cardinal: "Cardinal", chat_id, chat_name: str, order_id: str, settings: dict):
    """Логика выдачи: свои ссылки → поставщик (фолбэк)."""
    logger.info(f"{LOGGER_PREFIX} Оплата Gemini (Заказ #{order_id}) → чат {chat_id}")

    raw_link = None
    source = ""

    # ── Приоритет 1: свои ссылки из файла ──
    local_link = take_link()
    if local_link:
        raw_link = local_link
        source = "local"
        remaining = get_links_count()
        logger.info(f"{LOGGER_PREFIX} Взята своя ссылка. Осталось: {remaining}")

        # Уведомляем если ссылки заканчиваются
        if remaining <= 3 and remaining > 0:
            _notify_admins(cardinal,
                f"⚠️ <b>Gemini JIT:</b> Осталось своих ссылок: <b>{remaining}</b>\n"
                f"Загрузите ещё через панель плагина!")
        elif remaining == 0:
            _notify_admins(cardinal,
                f"🔶 <b>Gemini JIT:</b> Свои ссылки закончились!\n"
                f"Следующие заказы пойдут через API поставщика.")

    # ── Приоритет 2: закупка у поставщика ──
    if not raw_link:
        raw_link = buy_from_supplier(settings)
        source = "supplier"
        if raw_link:
            logger.info(f"{LOGGER_PREFIX} Закуплена ссылка у поставщика для заказа #{order_id}")

    if not raw_link:
        error_text = (
            "Извините, произошла небольшая задержка генерации профиля. "
            "Продавец выдаст вам данные в чат в течение пары минут!"
        )
        cardinal.send_message(chat_id, error_text, chat_name)
        logger.warning(f"{LOGGER_PREFIX} Нет ссылок ни своих, ни у поставщика! Заказ #{order_id}")
        _notify_admins(cardinal,
            f"🔴 <b>Gemini JIT:</b> Заказ <b>#{order_id}</b> — ссылок нет!\n"
            f"Чат: {chat_id} ({chat_name})\n"
            f"Нет ни своих ссылок, ни остатков у поставщика.\n"
            f"⚠️ Выдайте товар вручную!")
        return

    short_link = shorten_url(raw_link)

    reply_message = (
        f"🎉 Спасибо за покупку! Ваш индивидуальный доступ сформирован.\n\n"
        f"🪐 Ссылка на профиль: {short_link}\n\n"
        f"🛠 Инструкция: Войдите в браузер в режиме Инкогнито с включенным качественным VPN "
        f"(США/Европа) и перейдите по ссылке выше для привязки подписки. Срок активации токена — 24 часа."
    )

    result = cardinal.send_message(chat_id, reply_message, chat_name)
    if result:
        logger.info(f"{LOGGER_PREFIX} Товар выдан в чат {chat_id}! (Заказ #{order_id}, источник: {source})")
    else:
        logger.error(f"{LOGGER_PREFIX} НЕ УДАЛОСЬ отправить в чат {chat_id}! (Заказ #{order_id})")
        _notify_admins(cardinal,
            f"🔴 <b>Gemini JIT КРИТИЧНО:</b> Ссылка получена, но НЕ доставлена!\n\n"
            f"Заказ: <b>#{order_id}</b>\nЧат: {chat_id} ({chat_name})\n"
            f"Ссылка: <code>{short_link}</code>\n\n"
            f"⚠️ Отправьте ссылку покупателю вручную!")


# ─────────────────────────── Обработчик заказов (PRIMARY) ────────────────────


def on_new_order(cardinal: "Cardinal", event: NewOrderEvent) -> None:
    def _worker():
        try:
            order = event.order
            order_id = str(getattr(order, "id", "")).strip().lstrip("#")
            if not order_id:
                return

            settings = load_settings()
            if not settings.get("enabled", True):
                return

            lots = settings.get("funpay_lots", [])
            if not lots:
                return

            matched_lot = _find_matched_lot(lots, order=order)
            if not matched_lot:
                return

            if not _mark_order_processed(order_id):
                logger.info(f"{LOGGER_PREFIX} Дубль заказа #{order_id}, пропускаем.")
                return

            chat_id = getattr(order, "chat_id", None)
            chat_name = getattr(order, "buyer_username", None)

            if not chat_id:
                logger.warning(f"{LOGGER_PREFIX} Не удалось определить chat_id для заказа #{order_id}")
                return

            _process_delivery(cardinal, chat_id, chat_name, order_id, settings)

        except Exception:
            logger.error(f"{LOGGER_PREFIX} Ошибка в обработчике заказа (NewOrderEvent)", exc_info=True)

    Thread(target=_worker, daemon=True).start()


# ─────────────────────────── Обработчик сообщений (FALLBACK) ─────────────────


def on_new_message(cardinal: "Cardinal", event: NewMessageEvent) -> None:
    def _worker():
        try:
            msg = event.message
            text = (getattr(msg, "text", None) or "").strip()
            chat_id = getattr(msg, "chat_id", None)
            chat_name = getattr(msg, "chat_name", None)

            if not chat_id or not text:
                return

            settings = load_settings()
            if not settings.get("enabled", True):
                return

            # ── Триггер «werty»: тест ──
            if text.lower().strip() == "werty":
                logger.info(f"{LOGGER_PREFIX} Триггер 'werty' в чате {chat_id}. Тест отправки...")
                test_url = "https://gemini.google.com/app"
                short = shorten_url(test_url)
                local_count = get_links_count()
                test_msg = (
                    f"🧪 Тестовая отправка Gemini JIT v3.1\n\n"
                    f"🪐 Ссылка: {short}\n"
                    f"🔗 Своих ссылок: {local_count}\n\n"
                    f"✅ Если вы видите это сообщение — доставка работает!"
                )
                result = cardinal.send_message(chat_id, test_msg, chat_name)
                if result:
                    logger.info(f"{LOGGER_PREFIX} Тест werty: сообщение доставлено в чат {chat_id}.")
                else:
                    logger.error(f"{LOGGER_PREFIX} Тест werty: НЕ удалось отправить в чат {chat_id}!")
                return

            msg_type = getattr(msg, "type", None)
            if msg_type != MessageTypes.ORDER_PURCHASED:
                return

            lots = settings.get("funpay_lots", [])
            if not lots:
                return

            matched_lot = _find_matched_lot(lots, text=text)
            if not matched_lot:
                return

            order_id_match = ORDER_ID_RE.search(text)
            order_id = order_id_match.group(1) if order_id_match else f"msg_{chat_id}_{int(time.time())}"

            if not _mark_order_processed(order_id):
                logger.info(f"{LOGGER_PREFIX} Дубль заказа #{order_id} (из сообщения), пропускаем.")
                return

            _process_delivery(cardinal, chat_id, chat_name, order_id, settings)

        except Exception:
            logger.error(f"{LOGGER_PREFIX} Ошибка в обработчике сообщений (fallback)", exc_info=True)

    Thread(target=_worker, daemon=True).start()


# ─────────────────────────── Telegram UI Интерфейс ─────────────────────────────


def main_menu_text(settings: dict) -> str:
    supplier_stock, balance = get_supplier_data(settings)
    local_links = get_links_count()
    s_key = settings.get("supplier_api_key", "")
    masked_key = f"{s_key[:4]}•••{s_key[-4:]}" if len(s_key) > 8 else "не задан"

    lots = settings.get("funpay_lots", [])
    if lots:
        lots_text = "\n".join([
            f"🔸 <b>{utils.escape(l.get('title', 'Без названия'))}</b>\n"
            f"   ID: <code>{l['lot_id']}</code> | 💵 {l['price']}₽"
            for l in lots
        ])
    else:
        lots_text = "🚫 Нет привязанных лотов"

    return (
        "<b>🤖 Gemini JIT Panel v3.1</b>\n\n"
        f"Статус: {'🟢 Активен' if settings.get('enabled', True) else '🔴 Отключен'}\n\n"
        f"🔑 API Поставщика: <code>{utils.escape(masked_key)}</code>\n"
        f"💰 Баланс: <code>{balance} $</code>\n\n"
        f"📦 <b>Наличие:</b>\n"
        f"  🔗 Свои ссылки: <b>{local_links} шт.</b> {'✅' if local_links > 0 else '➖'}\n"
        f"  🌐 У поставщика (ID {settings.get('target_product_id', 1)}): <b>{supplier_stock} шт.</b> {'✅' if supplier_stock > 0 else '➖'}\n"
        f"  📊 Итого: <b>{local_links + supplier_stock} шт.</b>\n\n"
        f"<i>Приоритет: сначала свои ссылки, потом поставщик.</i>\n\n"
        f"📦 <b>Привязанные лоты ({len(lots)} шт):</b>\n{lots_text}"
    )


def main_menu_kb(offset: int) -> K:
    settings = load_settings()
    kb = K(row_width=2)
    kb.add(B("🔴 Выключить" if settings.get("enabled", True) else "🟢 Включить", callback_data=f"{CBT_TOGGLE}:{offset}"))
    kb.row(B("🔑 API Поставщика", callback_data=f"{CBT_SET_SUPPLIER_KEY}:{offset}"), B("📦 ID Товара", callback_data=f"{CBT_SET_PRODUCT_ID}:{offset}"))

    # Управление лотами
    kb.row(B("➕ Добавить лот", callback_data=f"{CBT_ADD_LOT}:{offset}"), B("🗑 Удалить лот", callback_data=f"{CBT_DEL_LOT_MENU}:{offset}"))

    # Управление ссылками
    local_count = get_links_count()
    kb.row(B(f"🔗 Загрузить ссылки ({local_count})", callback_data=f"{CBT_ADD_LINKS}:{offset}"), B("🗑 Очистить ссылки", callback_data=f"{CBT_CLEAR_LINKS}:{offset}"))

    kb.row(B("🔄 Синхронизировать", callback_data=f"{CBT_FORCE_SYNC}:{offset}"), B("🧪 Тест", callback_data=f"{CBT_TEST}:{offset}"))
    kb.row(B("🔄 Обновить", callback_data=f"{CBT_MAIN}:{offset}"))
    kb.add(B("◀ Назад", callback_data=f"{CBT.EDIT_PLUGIN}:{UUID}:{offset}"))
    return kb


def del_lot_kb(offset: int) -> K:
    settings = load_settings()
    kb = K(row_width=1)
    lots = settings.get("funpay_lots", [])
    for i, lot in enumerate(lots):
        title = lot.get('title', lot['lot_id'])[:25]
        kb.add(B(f"🗑 {title} ({lot['lot_id']})", callback_data=f"{CBT_DEL_LOT}:{i}:{offset}"))
    kb.add(B("◀ Назад", callback_data=f"{CBT_MAIN}:{offset}"))
    return kb


# ─────────────────────────── Инициализация ─────────────────────────────────────


def init(cardinal: "Cardinal", *args):
    if not cardinal.telegram:
        return

    os.makedirs(PLUGIN_FOLDER, exist_ok=True)

    if not os.path.exists(LINKS_FILE):
        with open(LINKS_FILE, "w", encoding="utf-8") as f:
            pass

    tg = cardinal.telegram
    bot = tg.bot

    def _state_is(m, state_name: str) -> bool:
        data = tg.get_state(m.chat.id, m.from_user.id)
        return bool(data) and data.get("state") == state_name

    def render_main(chat_id, message_id, offset):
        settings = load_settings()
        bot.edit_message_text(main_menu_text(settings), chat_id, message_id, reply_markup=main_menu_kb(offset), parse_mode="HTML")

    def open_settings(c):
        offset = int(c.data.split(":")[2])
        render_main(c.message.chat.id, c.message.message_id, offset)
        bot.answer_callback_query(c.id)

    def toggle_plugin(c):
        offset = int(c.data.split(":")[-1])
        s = load_settings()
        s["enabled"] = not s.get("enabled", True)
        save_settings(s)
        render_main(c.message.chat.id, c.message.message_id, offset)
        bot.answer_callback_query(c.id, "Статус изменен")

    def force_sync(c):
        offset = int(c.data.split(":")[-1])
        s = load_settings()
        bot.answer_callback_query(c.id, "Синхронизация запущена! Ожидайте...")
        sync_funpay_status(cardinal, s)
        render_main(c.message.chat.id, c.message.message_id, offset)

    # ─── Тестирование пайплайна ───

    def run_test(c):
        offset = int(c.data.split(":")[-1])
        tg_chat_id = c.message.chat.id
        bot.answer_callback_query(c.id, "🧪 Тест запущен...")

        def _test_worker():
            lines = ["<b>🧪 Диагностика Gemini JIT v3.1</b>"]
            lines.append("<i>Безопасный режим — закупка НЕ производится</i>\n")
            all_ok = True

            settings = load_settings()

            # ── Шаг 1: API-ключ ──
            lines.append("─── <b>Шаг 1: API-ключ</b> ───")
            s_key = (settings.get("supplier_api_key") or "").strip()
            if not s_key:
                lines.append("⚠️ API-ключ поставщика не задан (фолбэк через API не будет работать)")
            else:
                masked = f"{s_key[:4]}•••{s_key[-4:]}" if len(s_key) > 8 else s_key[:4] + "•••"
                lines.append(f"✅ Ключ задан: <code>{utils.escape(masked)}</code>")

            # ── Шаг 2: Подключение к API (/me) ──
            lines.append("\n─── <b>Шаг 2: Подключение к API</b> ───")
            if not s_key:
                lines.append("⏭ Пропущено (нет ключа)")
            else:
                headers = {"X-API-Key": s_key, "Content-Type": "application/json"}
                t0 = time.time()
                try:
                    res_me = requests.get(f"{API_BASE_URL}/me", headers=headers, timeout=10)
                    api_ms = int((time.time() - t0) * 1000)
                    if res_me.status_code == 200:
                        data_me = res_me.json()
                        if data_me.get("ok"):
                            balance = data_me.get("user", {}).get("balance", 0.0)
                            lines.append(f"✅ Соединение OK ({api_ms} мс)")
                            lines.append(f"💰 Баланс реселлера: <code>{balance} $</code>")
                            if balance <= 0:
                                lines.append("⚠️ Баланс нулевой — закупка через API не пройдёт")
                        else:
                            lines.append(f"❌ API ошибка: <code>{utils.escape(str(data_me))}</code>")
                            all_ok = False
                    elif res_me.status_code == 401:
                        lines.append(f"❌ Неверный API-ключ (401, {api_ms} мс)")
                        all_ok = False
                    else:
                        lines.append(f"❌ HTTP {res_me.status_code} ({api_ms} мс)")
                        all_ok = False
                except Exception as e:
                    api_ms = int((time.time() - t0) * 1000)
                    lines.append(f"❌ Нет связи ({api_ms} мс): <code>{utils.escape(str(e))}</code>")

            # ── Шаг 3: Остатки ──
            lines.append("\n─── <b>Шаг 3: Наличие</b> ───")
            local_count = get_links_count()
            lines.append(f"🔗 Свои ссылки: <b>{local_count} шт.</b>")

            supplier_stock = 0
            if s_key:
                p_id = int(settings.get("target_product_id", 1))
                try:
                    headers = {"X-API-Key": s_key, "Content-Type": "application/json"}
                    res_prod = requests.get(f"{API_BASE_URL}/products", headers=headers, timeout=10)
                    if res_prod.status_code == 200:
                        data_prod = res_prod.json()
                        if data_prod.get("ok"):
                            for p in data_prod.get("products", []):
                                if p.get("id") == p_id:
                                    supplier_stock = p.get("stock_count", 0)
                                    break
                except Exception:
                    pass
                lines.append(f"🌐 У поставщика (ID={p_id}): <b>{supplier_stock} шт.</b>")
            else:
                lines.append("🌐 У поставщика: — (нет ключа)")

            total = local_count + supplier_stock
            lines.append(f"📊 Итого: <b>{total} шт.</b>")

            if total <= 0:
                lines.append("⚠️ Нет ни своих ссылок, ни у поставщика — лоты будут скрыты!")
                all_ok = False
            elif local_count > 0:
                lines.append(f"ℹ️ Первые {local_count} заказов — из своих ссылок")

            # ── Шаг 4: clck.ru ──
            lines.append("\n─── <b>Шаг 4: clck.ru</b> ───")
            test_url = "https://example.com/test"
            t0 = time.time()
            short = shorten_url(test_url)
            clck_ms = int((time.time() - t0) * 1000)
            if short != test_url:
                lines.append(f"✅ Сервис доступен ({clck_ms} мс)")
            else:
                lines.append(f"⚠️ clck.ru недоступен ({clck_ms} мс) — ссылки будут длинными")

            # ── Шаг 5: Лоты ──
            lines.append("\n─── <b>Шаг 5: Привязанные лоты</b> ───")
            lots = settings.get("funpay_lots", [])
            if lots:
                lines.append(f"✅ Привязано лотов: {len(lots)}")
                for lot in lots:
                    price_ok = True
                    try:
                        float(str(lot["price"]).replace(",", "."))
                    except (ValueError, TypeError):
                        price_ok = False
                        all_ok = False
                    status = "✅" if price_ok else "❌ цена!"
                    title = lot.get("title", lot["lot_id"])[:25]
                    lines.append(f"   🔸 {title} | {lot['price']}₽ {status}")
                q = min(total, 10) if total > 0 else 0
                lines.append(f"📦 Quantity на FunPay: <b>{q}</b>")
            else:
                lines.append("❌ Нет привязанных лотов!")
                all_ok = False

            # ── Итог ──
            lines.append("\n━━━━━━━━━━━━━━━━━━━━")
            if all_ok:
                lines.append("✅ <b>Все проверки пройдены!</b> Плагин готов к работе.")
            else:
                lines.append("⚠️ <b>Есть проблемы.</b> Исправьте пункты выше.")

            bot.send_message(tg_chat_id, "\n".join(lines), parse_mode="HTML")

        Thread(target=_test_worker, daemon=True).start()

    # Функции запроса ввода
    def ask_input(c, text, state_name, parse_mode=None):
        offset = int(c.data.split(":")[-1])
        sent = bot.send_message(c.message.chat.id, text, parse_mode=parse_mode)
        tg.set_state(c.message.chat.id, sent.message_id, c.from_user.id, state_name, {"offset": offset})

    def press_supplier_key(c):
        ask_input(c, "🔑 Введите X-API-Key реселлера из бота поставщика:", STATE_SET_SUPPLIER_KEY)
        bot.answer_callback_query(c.id)

    def press_product_id(c):
        ask_input(c, "📦 Введите ID продукта из базы поставщика (обычно 1):", STATE_SET_PRODUCT_ID)
        bot.answer_callback_query(c.id)

    def press_add_lot(c):
        text = (
            "➕ <b>Добавление новых лотов</b>\n\n"
            "Формат: <code>ID_Лота Цена</code>\n"
            "Можно несколько, каждый с новой строки.\n\n"
            "Пример:\n"
            "<code>72934354 186\n72934350 140\n72934347 122</code>"
        )
        offset = int(c.data.split(":")[-1])
        sent = bot.send_message(c.message.chat.id, text, parse_mode="HTML")
        tg.set_state(c.message.chat.id, sent.message_id, c.from_user.id, STATE_ADD_LOT, {"offset": offset})
        bot.answer_callback_query(c.id)

    def press_add_links(c):
        text = (
            "🔗 <b>Загрузка ссылок</b>\n\n"
            "Отправьте ссылки, каждую с новой строки.\n\n"
            "Пример:\n"
            "<code>https://gemini.google.com/...\nhttps://gemini.google.com/...</code>\n\n"
            "Или отправьте <b>.txt файл</b> со ссылками."
        )
        offset = int(c.data.split(":")[-1])
        sent = bot.send_message(c.message.chat.id, text, parse_mode="HTML")
        tg.set_state(c.message.chat.id, sent.message_id, c.from_user.id, STATE_ADD_LINKS, {"offset": offset})
        bot.answer_callback_query(c.id)

    def press_clear_links(c):
        offset = int(c.data.split(":")[-1])
        count = clear_links()
        bot.answer_callback_query(c.id, f"Удалено {count} ссылок")
        render_main(c.message.chat.id, c.message.message_id, offset)

    def open_del_lot_menu(c):
        offset = int(c.data.split(":")[-1])
        bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=del_lot_kb(offset))
        bot.answer_callback_query(c.id)

    def process_del_lot(c):
        parts = c.data.split(":")
        offset = int(parts[-1])
        lot_idx = int(parts[-2])
        s = load_settings()
        lots = s.get("funpay_lots", [])
        if 0 <= lot_idx < len(lots):
            deleted = lots.pop(lot_idx)
            save_settings(s)
            bot.answer_callback_query(c.id, f"Лот {deleted['lot_id']} удален!")
        else:
            bot.answer_callback_query(c.id, "Ошибка удаления.")
        render_main(c.message.chat.id, c.message.message_id, offset)

    # Регистрация хэндлеров кнопок
    tg.cbq_handler(lambda c: render_main(c.message.chat.id, c.message.message_id, int(c.data.split(":")[-1])), lambda c: c.data.startswith(f"{CBT_MAIN}:"))
    tg.cbq_handler(open_settings, lambda c: c.data.startswith(f"{CBT.PLUGIN_SETTINGS}:{UUID}:"))

    tg.cbq_handler(toggle_plugin, lambda c: c.data.startswith(f"{CBT_TOGGLE}:"))
    tg.cbq_handler(force_sync, lambda c: c.data.startswith(f"{CBT_FORCE_SYNC}:"))
    tg.cbq_handler(run_test, lambda c: c.data.startswith(f"{CBT_TEST}:"))

    tg.cbq_handler(press_supplier_key, lambda c: c.data.startswith(f"{CBT_SET_SUPPLIER_KEY}:"))
    tg.cbq_handler(press_product_id, lambda c: c.data.startswith(f"{CBT_SET_PRODUCT_ID}:"))
    tg.cbq_handler(press_add_lot, lambda c: c.data.startswith(f"{CBT_ADD_LOT}:"))
    tg.cbq_handler(press_add_links, lambda c: c.data.startswith(f"{CBT_ADD_LINKS}:"))
    tg.cbq_handler(press_clear_links, lambda c: c.data.startswith(f"{CBT_CLEAR_LINKS}:"))
    tg.cbq_handler(open_del_lot_menu, lambda c: c.data.startswith(f"{CBT_DEL_LOT_MENU}:"))
    tg.cbq_handler(process_del_lot, lambda c: c.data.startswith(f"{CBT_DEL_LOT}:"))

    # Логика сохранения параметров
    def save_simple_param(m, key_name, success_text, is_int=False):
        state = tg.get_state(m.chat.id, m.from_user.id)
        offset = state["data"].get("offset", 0)
        tg.clear_state(m.chat.id, m.from_user.id, True)

        value = m.text.strip()
        if is_int:
            try:
                value = int(value)
            except ValueError:
                bot.send_message(
                    m.chat.id,
                    "❌ Ошибка: нужно ввести целое число.",
                    reply_markup=main_menu_kb(offset),
                )
                return

        s = load_settings()
        s[key_name] = value
        save_settings(s)
        bot.send_message(m.chat.id, success_text, reply_markup=main_menu_kb(offset))

    def save_new_lot(m):
        state = tg.get_state(m.chat.id, m.from_user.id)
        offset = state["data"].get("offset", 0)
        tg.clear_state(m.chat.id, m.from_user.id, True)

        input_lines = m.text.strip().split('\n')
        s = load_settings()
        success_count = 0
        errors = []

        for line in input_lines:
            line = line.strip()
            if not line:
                continue

            parts = line.split()
            if len(parts) != 2:
                errors.append(f"❌ «{line}»: формат ID Цена.")
                continue

            lot_id, price = parts
            if not lot_id.isdigit():
                errors.append(f"❌ «{line}»: ID должен быть числом.")
                continue

            price_normalized = price.replace(",", ".")
            try:
                float(price_normalized)
            except ValueError:
                errors.append(f"❌ «{line}»: неверная цена.")
                continue

            try:
                lot_fields = cardinal.account.get_lot_fields(int(lot_id))
                lot_title = lot_fields.title_ru or lot_fields.title_en or "Без названия"
            except Exception:
                errors.append(f"❌ Лот {lot_id} не найден на вашем аккаунте.")
                continue

            s["funpay_lots"] = [l for l in s.get("funpay_lots", []) if str(l.get("lot_id")) != lot_id]
            s.setdefault("funpay_lots", []).append({
                "lot_id": lot_id,
                "price": price_normalized,
                "title": lot_title
            })
            success_count += 1

        save_settings(s)

        result_msg = f"✅ Успешно добавлено лотов: {success_count}\n"
        if errors:
            result_msg += "\n" + "\n".join(errors)
        bot.send_message(m.chat.id, result_msg, reply_markup=main_menu_kb(offset))

    def save_links(m):
        state = tg.get_state(m.chat.id, m.from_user.id)
        offset = state["data"].get("offset", 0)
        tg.clear_state(m.chat.id, m.from_user.id, True)

        links_text = ""

        if m.document:
            try:
                file_info = bot.get_file(m.document.file_id)
                downloaded = bot.download_file(file_info.file_path)
                links_text = downloaded.decode("utf-8", errors="ignore")
            except Exception as e:
                bot.send_message(m.chat.id, f"❌ Ошибка загрузки файла: {e}", reply_markup=main_menu_kb(offset))
                return
        elif m.text:
            links_text = m.text
        else:
            bot.send_message(m.chat.id, "❌ Отправьте текст или .txt файл.", reply_markup=main_menu_kb(offset))
            return

        count = add_links(links_text)
        total = get_links_count()
        bot.send_message(
            m.chat.id,
            f"✅ Загружено ссылок: <b>{count}</b>\n🔗 Всего в очереди: <b>{total}</b>",
            parse_mode="HTML",
            reply_markup=main_menu_kb(offset)
        )

    tg.msg_handler(lambda m: save_simple_param(m, "supplier_api_key", "✅ API Ключ поставщика сохранен."), func=lambda m: _state_is(m, STATE_SET_SUPPLIER_KEY))
    tg.msg_handler(lambda m: save_simple_param(m, "target_product_id", "✅ ID продукта успешно изменен.", is_int=True), func=lambda m: _state_is(m, STATE_SET_PRODUCT_ID))
    tg.msg_handler(save_new_lot, func=lambda m: _state_is(m, STATE_ADD_LOT))
    tg.msg_handler(save_links, func=lambda m: _state_is(m, STATE_ADD_LINKS), content_types=["text", "document"])

    # Старт демона мониторинга
    Thread(target=_stock_monitoring_thread, args=(cardinal,), daemon=True).start()
    logger.info(f"{LOGGER_PREFIX} v{VERSION} — Модуль JIT-выдачи запущен.")


BIND_TO_PRE_INIT = [init]
BIND_TO_NEW_ORDER = [on_new_order]
BIND_TO_NEW_MESSAGE = [on_new_message]
BIND_TO_DELETE = None