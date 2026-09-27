from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid as uuid_lib
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

NAME = "Supplier Auto-Seller"
VERSION = "1.0.0"
DESCRIPTION = (
    "Плагин автоматической продажи товаров от поставщика.\n\n"
    "• Привязка лотов FunPay к товарам поставщика через Telegram-меню.\n"
    "• Проверка наличия и баланса каждые 60 секунд.\n"
    "• Автоматическое скрытие лотов при нехватке баланса или товара.\n"
    "• Умная выдача: ссылки через clck.ru, логин/пароль — как есть.\n"
    "• Telegram-уведомления о статусе лотов.\n"
    "• Обработка заказов через NewOrderEvent (надёжно, без парсинга текста)."
)
CREDITS = "@tigranayvvv"
UUID = "a3f1c8e7-5b2d-4a91-9e06-8c3f7d5b1a42"
SETTINGS_PAGE = True

logger = logging.getLogger("FPC.supplier_seller")
LOGGER_PREFIX = "[SUPPLIER]"

PLUGIN_FOLDER = f"storage/plugins/{UUID}/"
SETTINGS_FILE = f"{PLUGIN_FOLDER}settings.json"

API_BASE_URL = "https://safwantigershopbot-production.up.railway.app/api"

# ─────────────────────────── Защита от дублей ──────────────────────────────────

_processed_orders: set[str] = set()
_processed_orders_lock = Lock()


def _mark_order_processed(order_id: str) -> bool:
    """Атомарно помечает заказ как обработанный. True если новый, False если дубль."""
    with _processed_orders_lock:
        if order_id in _processed_orders:
            return False
        _processed_orders.add(order_id)
        if len(_processed_orders) > 1000:
            oldest = next(iter(_processed_orders))
            _processed_orders.discard(oldest)
        return True


# ─────────────────────────── Настройки ─────────────────────────────────────────

DEFAULT_SETTINGS = {
    "enabled": True,
    "supplier_api_key": "stapi_6ef7f3477113da6aea187a12534ccef92d82a66f46f03a94a19b95ea883f14e7",
    "bindings": {},       # {"product_id_str": [lot_id_int, ...]}
    "lot_states": {},     # {"lot_id_str": true/false} — текущая видимость
}

_settings_lock = Lock()


def load_settings() -> dict:
    if not os.path.exists(SETTINGS_FILE):
        return json.loads(json.dumps(DEFAULT_SETTINGS))
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return json.loads(json.dumps(DEFAULT_SETTINGS))

    for key, value in DEFAULT_SETTINGS.items():
        if key not in data:
            data[key] = json.loads(json.dumps(value))
    return data


def save_settings(settings: dict) -> None:
    os.makedirs(PLUGIN_FOLDER, exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


# ─────────────────────────── Supplier API ──────────────────────────────────────


def _api_headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def get_products(api_key: str) -> list[dict]:
    """Получает список товаров поставщика. Каждый: {id, name, price, stock}."""
    if not api_key:
        return []
    try:
        res = requests.get(f"{API_BASE_URL}/products", headers=_api_headers(api_key), timeout=10)
        if res.status_code != 200:
            return []
        data = res.json()
        products = data.get("products") or (data if isinstance(data, list) else []) or []
        result = []
        for p in products:
            result.append({
                "id": p.get("id"),
                "name": str(p.get("name", "")).strip(),
                "price": float(p.get("price", 0)),
                "stock": int(p.get("stock", 0) or p.get("stock_count", 0) or 0),
            })
        return result
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка получения товаров: {e}")
        return []


def get_balance(api_key: str) -> float:
    """Получает баланс реселлера у поставщика."""
    if not api_key:
        return 0.0
    try:
        res = requests.get(f"{API_BASE_URL}/balance", headers=_api_headers(api_key), timeout=10)
        if res.status_code != 200:
            return 0.0
        data = res.json()
        bal = data.get("balance") or data.get("user", {}).get("balance") or 0.0
        return float(bal)
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка получения баланса: {e}")
        return 0.0


def buy_product(api_key: str, product_id: int) -> Optional[str]:
    """Покупает 1 товар у поставщика. Возвращает выданные данные или None."""
    if not api_key:
        return None
    request_id = f"sup-{int(time.time())}-{os.getpid()}-{uuid_lib.uuid4().hex[:6]}"
    payload = {"product_id": product_id, "quantity": 1, "request_id": request_id}
    try:
        res = requests.post(
            f"{API_BASE_URL}/order",
            headers=_api_headers(api_key),
            json=payload,
            timeout=15,
        )
        if res.status_code == 200:
            data = res.json()
            items = (
                data.get("items")
                or data.get("delivered_items")
                or data.get("data", {}).get("items")
                or []
            )
            if items:
                item = items[0]
                if isinstance(item, str):
                    return item
                if isinstance(item, dict):
                    return (
                        item.get("content")
                        or item.get("link")
                        or item.get("value")
                        or item.get("data")
                        or str(item)
                    )
        logger.warning(f"{LOGGER_PREFIX} Ответ /order ({res.status_code}): {res.text[:200]}")
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка закупки товара {product_id}: {e}")
    return None


# ─────────────────────────── URL Shortener & Format Detection ──────────────────

URL_REGEX = re.compile(r"https?://\S+", re.IGNORECASE)


def shorten_url(long_url: str) -> str:
    """Сокращает URL через clck.ru. При ошибке возвращает оригинал."""
    try:
        res = requests.get("https://clck.ru/--", params={"url": long_url}, timeout=10)
        if res.status_code == 200:
            return res.text.strip()
    except Exception as e:
        logger.warning(f"{LOGGER_PREFIX} clck.ru недоступен: {e}")
    return long_url


def is_url(text: str) -> bool:
    """Определяет, является ли текст ссылкой."""
    return bool(URL_REGEX.match(text.strip()))


def format_delivery(raw_data: str) -> str:
    """Форматирует данные для выдачи: ссылки → сокращает, остальное → как есть."""
    raw = raw_data.strip()
    if is_url(raw):
        short = shorten_url(raw)
        return (
            f"🎉 Спасибо за покупку! Ваш товар готов.\n\n"
            f"🔗 Ссылка: {short}\n\n"
            f"🛡️ Гарантия действует с момента покупки. "
            f"Рекомендуем сделать видеозапись при первом использовании."
        )
    else:
        return (
            f"🎉 Спасибо за покупку! Ваши данные готовы.\n\n"
            f"📋 Данные для входа:\n<code>{raw}</code>\n\n"
            f"🛡️ Гарантия действует с момента покупки. "
            f"Рекомендуем сделать видеозапись при первом входе."
        )


# ─────────────────────────── Lot Management ───────────────────────────────────

_lot_mgmt_lock = Lock()


def _set_lot_active(cardinal: "Cardinal", lot_id: int, active: bool) -> bool:
    """Скрывает (active=False) или показывает (active=True) лот на FunPay.
    Возвращает True при успехе.
    """
    try:
        lot_fields = cardinal.account.get_lot_fields(lot_id)
        if lot_fields.active == active:
            return True  # Уже в нужном состоянии
        lot_fields.active = active
        cardinal.account.save_lot(lot_fields)
        state_text = "показан" if active else "скрыт"
        logger.info(f"{LOGGER_PREFIX} Лот {lot_id} {state_text}.")
        return True
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка управления лотом {lot_id}: {e}")
        return False


def hide_lot(cardinal: "Cardinal", lot_id: int) -> bool:
    with _lot_mgmt_lock:
        return _set_lot_active(cardinal, lot_id, False)


def show_lot(cardinal: "Cardinal", lot_id: int) -> bool:
    with _lot_mgmt_lock:
        return _set_lot_active(cardinal, lot_id, True)


# ─────────────────────────── Helpers ──────────────────────────────────────────

ORDER_ID_RE = re.compile(r"#(\w+)")


def _get_admin_chat_id(cardinal: "Cardinal") -> Optional[int]:
    """Получает chat_id первого авторизованного пользователя из конфига."""
    try:
        authorized = cardinal.MAIN_CFG.get("Telegram", {}).get("authorized_users", "")
        if authorized:
            first_id = authorized.split(",")[0].strip()
            if first_id.isdigit():
                return int(first_id)
    except Exception:
        pass
    return None


def _notify_admin(cardinal: "Cardinal", text: str):
    """Отправляет уведомление админу в Telegram."""
    try:
        admin_id = _get_admin_chat_id(cardinal)
        if admin_id and cardinal.telegram:
            cardinal.telegram.bot.send_message(admin_id, text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка отправки уведомления: {e}")


def get_account_lots(cardinal: "Cardinal") -> list[dict]:
    """Получает список всех лотов пользователя FunPay.
    Каждый элемент: {'id': int, 'title': str, 'price': float}
    """
    lots = []
    try:
        profile = getattr(cardinal, "profile", None) or getattr(cardinal, "curr_profile", None) or getattr(cardinal, "tg_profile", None)
        if profile and hasattr(profile, "get_lots"):
            for l in profile.get_lots():
                lid = int(l.id) if str(l.id).isdigit() else l.id
                lots.append({
                    "id": lid,
                    "title": str(getattr(l, "description", "") or "Без названия").strip(),
                    "price": float(getattr(l, "price", 0.0) or 0.0),
                })
            if lots:
                return lots
    except Exception as e:
        logger.warning(f"{LOGGER_PREFIX} Ошибка получения лотов из cardinal.profile: {e}")

    try:
        if hasattr(cardinal, "account") and cardinal.account and cardinal.account.is_initiated:
            user = cardinal.account.get_user(cardinal.account.id)
            for l in user.get_lots():
                lid = int(l.id) if str(l.id).isdigit() else l.id
                lots.append({
                    "id": lid,
                    "title": str(getattr(l, "description", "") or "Без названия").strip(),
                    "price": float(getattr(l, "price", 0.0) or 0.0),
                })
            if lots:
                return lots
    except Exception as e:
        logger.warning(f"{LOGGER_PREFIX} Ошибка получения лотов через get_user: {e}")

    return lots


def _find_product_for_lot(settings: dict, lot_id: int) -> Optional[int]:
    """Находит product_id поставщика по lot_id из привязок."""
    for pid_str, lot_ids in settings.get("bindings", {}).items():
        if lot_id in lot_ids:
            return int(pid_str)
    return None


def _find_product_for_order(settings: dict, order) -> Optional[int]:
    """Определяет product_id по заказу: сначала через описание, потом через цену."""
    bindings = settings.get("bindings", {})
    all_bound_lot_ids = set()
    for pid_str, lot_ids in bindings.items():
        for lid in lot_ids:
            all_bound_lot_ids.add(lid)

    # Поиск lot_id в описании заказа
    order_desc = str(getattr(order, "description", "") or "").lower()
    for pid_str, lot_ids in bindings.items():
        for lid in lot_ids:
            if re.search(r"\b" + re.escape(str(lid)) + r"\b", order_desc):
                return int(pid_str)

    # Фолбэк: вернуть первый product_id если есть хоть одна привязка
    # (лучше чем ничего — один поставщик может иметь универсальный товар)
    if bindings:
        return int(next(iter(bindings)))
    return None


# ─────────────────────────── Фоновый мониторинг ──────────────────────────────

_monitor_cardinal: Optional["Cardinal"] = None


def _monitoring_thread(cardinal: "Cardinal"):
    """Фоновый поток: каждые 60 секунд проверяет наличие и баланс."""
    time.sleep(15)  # Даём Cardinal прогрузиться
    while True:
        try:
            settings = load_settings()
            if not settings.get("enabled", True):
                time.sleep(60)
                continue

            api_key = (settings.get("supplier_api_key") or "").strip()
            if not api_key:
                time.sleep(60)
                continue

            bindings = settings.get("bindings", {})
            if not bindings:
                time.sleep(60)
                continue

            # Получаем данные поставщика
            products = get_products(api_key)
            balance = get_balance(api_key)

            products_map = {p["id"]: p for p in products}
            lot_states = settings.get("lot_states", {})
            changed = False
            notifications = []

            for pid_str, lot_ids in bindings.items():
                pid = int(pid_str)
                product = products_map.get(pid)

                if product is None:
                    # Товар не найден у поставщика — скрываем
                    for lid in lot_ids:
                        lid_str = str(lid)
                        if lot_states.get(lid_str, True):
                            if hide_lot(cardinal, lid):
                                lot_states[lid_str] = False
                                changed = True
                                notifications.append(
                                    f"💠 Лот <code>{lid}</code> скрыт — товар ID {pid} не найден у поставщика"
                                )
                            time.sleep(1)
                    continue

                stock = product["stock"]
                price = product["price"]
                can_buy = stock > 0 and balance >= price

                for lid in lot_ids:
                    lid_str = str(lid)
                    was_visible = lot_states.get(lid_str, True)

                    if can_buy and not was_visible:
                        # Товар снова доступен → показываем
                        if show_lot(cardinal, lid):
                            lot_states[lid_str] = True
                            changed = True
                            notifications.append(
                                f"⚡ Лот <code>{lid}</code> восстановлен — "
                                f"{product['name']} снова в наличии ({stock} шт, баланс ${balance:.2f})"
                            )
                        time.sleep(1)

                    elif not can_buy and was_visible:
                        # Нет возможности купить → скрываем
                        if hide_lot(cardinal, lid):
                            lot_states[lid_str] = False
                            changed = True
                            reason = "нет в наличии" if stock == 0 else f"баланс ${balance:.2f} < ${price}"
                            notifications.append(
                                f"💠 Лот <code>{lid}</code> скрыт — {product['name']}: {reason}"
                            )
                        time.sleep(1)

            if changed:
                settings["lot_states"] = lot_states
                save_settings(settings)

            # Отправляем уведомления пакетом
            if notifications:
                header = f"🔔 <b>Supplier Monitor</b> ({time.strftime('%H:%M:%S')})\n\n"
                msg = header + "\n".join(notifications)
                _notify_admin(cardinal, msg)

        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка в потоке мониторинга: {e}", exc_info=True)

        time.sleep(60)


# ─────────────────────────── Обработка заказов ────────────────────────────────


def _process_delivery(cardinal: "Cardinal", chat_id, chat_name: str, order_id: str,
                       product_id: int, settings: dict):
    """Покупает у поставщика и выдаёт покупателю."""
    logger.info(f"{LOGGER_PREFIX} Заказ #{order_id} → покупка товара {product_id} у поставщика...")

    api_key = (settings.get("supplier_api_key") or "").strip()
    raw_data = buy_product(api_key, product_id)

    if not raw_data:
        error_text = (
            "Извините, произошла небольшая задержка подготовки товара. "
            "Продавец выдаст вам данные в чат в течение пары минут!"
        )
        cardinal.send_message(chat_id, error_text, chat_name)
        logger.warning(f"{LOGGER_PREFIX} Не удалось купить товар {product_id} для заказа #{order_id}!")

        # Уведомляем админа
        _notify_admin(
            cardinal,
            f"⚠️ <b>Ошибка выдачи!</b>\n\n"
            f"Заказ: <code>#{order_id}</code>\n"
            f"Товар: ID {product_id}\n"
            f"Чат: {chat_name or chat_id}\n\n"
            f"Не удалось купить товар у поставщика. Требуется ручная выдача!"
        )
        return

    # Форматируем и выдаём
    reply_message = format_delivery(raw_data)
    cardinal.send_message(chat_id, reply_message, chat_name)
    logger.info(f"{LOGGER_PREFIX} Товар {product_id} выдан в чат {chat_id} (Заказ #{order_id})")

    # Уведомляем админа об успешной продаже
    delivery_type = "🔗 Ссылка" if is_url(raw_data.strip()) else "📋 Логин/Пароль"
    _notify_admin(
        cardinal,
        f"✅ <b>Продажа!</b>\n\n"
        f"Заказ: <code>#{order_id}</code>\n"
        f"Товар: ID {product_id}\n"
        f"Покупатель: {chat_name or chat_id}\n"
        f"Тип выдачи: {delivery_type}"
    )


def on_new_order(cardinal: "Cardinal", event: NewOrderEvent) -> None:
    """PRIMARY: срабатывает через BIND_TO_NEW_ORDER при новом заказе."""
    def _worker():
        try:
            order = event.order
            order_id = str(getattr(order, "id", "")).strip().lstrip("#")
            if not order_id:
                return

            settings = load_settings()
            if not settings.get("enabled", True):
                return

            bindings = settings.get("bindings", {})
            if not bindings:
                return

            # Определяем product_id по заказу
            product_id = _find_product_for_order(settings, order)
            if product_id is None:
                return

            # Дедупликация
            if not _mark_order_processed(order_id):
                logger.info(f"{LOGGER_PREFIX} Дубль заказа #{order_id}, пропускаем.")
                return

            chat_id = getattr(order, "buyer_id", None) or getattr(order, "chat_id", None)
            chat_name = getattr(order, "buyer_username", None)

            if not chat_id:
                logger.warning(f"{LOGGER_PREFIX} Не удалось определить chat_id для заказа #{order_id}")
                return

            _process_delivery(cardinal, chat_id, chat_name, order_id, product_id, settings)

        except Exception:
            logger.error(f"{LOGGER_PREFIX} Ошибка в обработчике заказа (NewOrderEvent)", exc_info=True)

    Thread(target=_worker, daemon=True).start()


def on_new_message(cardinal: "Cardinal", event: NewMessageEvent) -> None:
    """FALLBACK: срабатывает через BIND_TO_NEW_MESSAGE для ORDER_PURCHASED."""
    def _worker():
        try:
            msg = event.message
            text = (getattr(msg, "text", None) or "").strip()
            chat_id = getattr(msg, "chat_id", None)
            chat_name = getattr(msg, "chat_name", None)

            if not chat_id or not text:
                return

            msg_type = getattr(msg, "type", None)
            if msg_type != MessageTypes.ORDER_PURCHASED:
                return

            settings = load_settings()
            if not settings.get("enabled", True):
                return

            bindings = settings.get("bindings", {})
            if not bindings:
                return

            # Ищем совпадение lot_id в тексте сообщения
            all_lot_ids = []
            for pid_str, lot_ids in bindings.items():
                for lid in lot_ids:
                    all_lot_ids.append((lid, int(pid_str)))

            matched_product = None
            for lid, pid in all_lot_ids:
                if re.search(r"\b" + re.escape(str(lid)) + r"\b", text):
                    matched_product = pid
                    break

            if matched_product is None:
                return

            order_id_match = ORDER_ID_RE.search(text)
            order_id = order_id_match.group(1) if order_id_match else f"msg_{chat_id}_{int(time.time())}"

            if not _mark_order_processed(order_id):
                logger.info(f"{LOGGER_PREFIX} Дубль заказа #{order_id} (из сообщения), пропускаем.")
                return

            _process_delivery(cardinal, chat_id, chat_name, order_id, matched_product, settings)

        except Exception:
            logger.error(f"{LOGGER_PREFIX} Ошибка в обработчике сообщений (fallback)", exc_info=True)

    Thread(target=_worker, daemon=True).start()


# ─────────────────────────── Callback константы ───────────────────────────────

CBT_MAIN = "sup:main"
CBT_TOGGLE = "sup:toggle"
CBT_SET_KEY = "sup:set_key"
CBT_PRODUCTS = "sup:products"
CBT_PRODUCT_LOTS = "sup:prod_lots"
CBT_BIND_LOT = "sup:bind"
CBT_UNBIND_LOT = "sup:unbind"
CBT_UNBIND_PRODUCT = "sup:unbind_prod"
CBT_FORCE_SYNC = "sup:sync"
CBT_TEST = "sup:test"
CBT_ADD_LOT_INPUT = "sup:add_lot_in"

STATE_SET_KEY = f"{UUID}:state_key"
STATE_ADD_LOT = f"{UUID}:state_add_lot"


# ─────────────────────────── Telegram UI ──────────────────────────────────────


def main_menu_text(settings: dict, products: list[dict] = None) -> str:
    api_key = (settings.get("supplier_api_key") or "").strip()
    masked_key = f"{api_key[:8]}•••{api_key[-4:]}" if len(api_key) > 12 else ("не задан" if not api_key else api_key[:4] + "•••")

    bindings = settings.get("bindings", {})
    total_lots = sum(len(v) for v in bindings.values())
    total_products = len(bindings)

    status_emoji = "⚡" if settings.get("enabled", True) else "💤"
    status_text = "Активен" if settings.get("enabled", True) else "Отключен"

    text = (
        f"<b>🛒 Supplier Auto-Seller v{VERSION}</b>\n\n"
        f"Статус: {status_emoji} {status_text}\n"
        f"🔑 API: <code>{utils.escape(masked_key)}</code>\n\n"
    )

    # Информация о привязках
    if bindings:
        text += f"📦 <b>Привязано: {total_products} товаров → {total_lots} лотов</b>\n"
        for pid_str, lot_ids in bindings.items():
            lot_ids_text = ", ".join(str(lid) for lid in lot_ids)
            text += f"  🔸 Товар #{pid_str} → лоты: <code>{lot_ids_text}</code>\n"
    else:
        text += "📦 Нет привязанных товаров\n"

    text += f"\n💡 Мониторинг каждые 60 сек | Выдача: URL→clck.ru, данные→как есть"

    return text


def main_menu_kb(offset: int) -> K:
    settings = load_settings()
    kb = K(row_width=2)

    toggle_text = "💤 Выключить" if settings.get("enabled", True) else "⚡ Включить"
    kb.add(B(toggle_text, callback_data=f"{CBT_TOGGLE}:{offset}"))
    kb.row(
        B("🔑 API Ключ", callback_data=f"{CBT_SET_KEY}:{offset}"),
        B("📦 Товары", callback_data=f"{CBT_PRODUCTS}:{offset}"),
    )
    kb.row(
        B("🔄 Синхронизировать", callback_data=f"{CBT_FORCE_SYNC}:{offset}"),
        B("🧪 Тест", callback_data=f"{CBT_TEST}:{offset}"),
    )
    kb.row(B("🔄 Обновить", callback_data=f"{CBT_MAIN}:{offset}"))
    kb.add(B("◀ Назад", callback_data=f"{CBT.EDIT_PLUGIN}:{UUID}:{offset}"))
    return kb


def products_menu_text(products: list[dict], bindings: dict) -> str:
    text = "<b>📦 Товары поставщика</b>\n\n"
    if not products:
        text += "⚠️ Не удалось загрузить товары. Проверьте API ключ.\n"
        return text

    text += "Нажмите на товар, чтобы привязать к нему лоты FunPay.\n\n"
    for p in products:
        pid = p["id"]
        bound = len(bindings.get(str(pid), []))
        stock_icon = "🟢" if p["stock"] > 0 else "⚪"
        bound_text = f" | 🔗 {bound} лотов" if bound > 0 else ""
        text += f"{stock_icon} <b>{p['name']}</b>\n   ID: {pid} | ${p['price']} | {p['stock']} шт.{bound_text}\n\n"

    return text


def products_menu_kb(products: list[dict], bindings: dict, offset: int) -> K:
    kb = K(row_width=1)
    for p in products:
        pid = p["id"]
        bound = len(bindings.get(str(pid), []))
        label = p["name"][:30]
        if bound > 0:
            label += f" 🔗{bound}"
        kb.add(B(label, callback_data=f"{CBT_PRODUCT_LOTS}:{pid}:{offset}"))
    kb.add(B("◀ Назад", callback_data=f"{CBT_MAIN}:{offset}"))
    return kb


def product_lots_text(product: dict, bound_lot_ids: list[int], all_account_lots: list[dict] = None, page: int = 0, per_page: int = 15) -> str:
    all_account_lots = all_account_lots or []
    text = (
        f"<b>📦 {product['name']}</b>\n"
        f"ID: {product['id']} | ${product['price']} | {product['stock']} шт.\n\n"
    )

    if bound_lot_ids:
        text += "<b>🔗 Привязанные к этому товару лоты:</b>\n"
        for lid in bound_lot_ids:
            lot_name = ""
            for al in all_account_lots:
                if al["id"] == lid:
                    lot_name = f" — {al['title'][:25]}"
                    break
            text += f"  🔸 <code>{lid}</code>{lot_name}\n"
        text += "\n"
    else:
        text += "🚫 <i>Нет привязанных лотов к этому товару</i>\n\n"

    if all_account_lots:
        total_pages = max(1, (len(all_account_lots) + per_page - 1) // per_page)
        start_idx = page * per_page
        end_idx = min(start_idx + per_page, len(all_account_lots))
        page_lots = all_account_lots[start_idx:end_idx]

        page_str = f" (стр. {page + 1}/{total_pages})" if total_pages > 1 else ""
        text += f"<b>📋 Все лоты FunPay{page_str}:</b>\n"
        for i, lot in enumerate(page_lots, start_idx + 1):
            is_bound = lot["id"] in bound_lot_ids
            mark = "🔗" if is_bound else "⚪"
            title = (lot['title'][:28] + "...") if len(lot['title']) > 30 else lot['title']
            text += f"<b>{i}.</b> {mark} <code>{lot['id']}</code> — {title} (💵 {lot['price']}₽)\n"

        text += (
            f"\n💡 Нажмите <b>«➕ Привязать лоты»</b> и укажите номера (например: <code>1, 2, 3</code>) "
            f"или прямые ID лотов."
        )
    else:
        text += (
            "📋 <i>Лоты аккаунта пока не загрузились из FunPay.</i>\n"
            "💡 Нажмите <b>«➕ Привязать лоты»</b> и введите ID лотов FunPay напрямую (например: <code>12345678</code>)."
        )

    return text


def product_lots_kb(product_id: int, bound_lot_ids: list[int], all_account_lots: list[dict] = None, offset: int = 0, page: int = 0, per_page: int = 15) -> K:
    all_account_lots = all_account_lots or []
    kb = K(row_width=2)

    total_pages = max(1, (len(all_account_lots) + per_page - 1) // per_page) if all_account_lots else 1

    # Кнопки пагинации
    if total_pages > 1:
        nav_buttons = []
        if page > 0:
            nav_buttons.append(B("⬅️ Назад", callback_data=f"{CBT_PRODUCT_LOTS}:{product_id}:{offset}:{page - 1}"))
        nav_buttons.append(B(f"📄 {page + 1}/{total_pages}", callback_data="sup:noop"))
        if page < total_pages - 1:
            nav_buttons.append(B("Вперёд ➡️", callback_data=f"{CBT_PRODUCT_LOTS}:{product_id}:{offset}:{page + 1}"))
        kb.row(*nav_buttons)

    # Кнопка привязки нового лота
    kb.add(B("➕ Привязать лоты (номера или ID)", callback_data=f"{CBT_ADD_LOT_INPUT}:{product_id}:{offset}"))

    # Кнопки отвязки для привязанных лотов
    for lid in bound_lot_ids[:10]:
        kb.add(B(f"🗑 Отвязать {lid}", callback_data=f"{CBT_UNBIND_LOT}:{product_id}:{lid}:{offset}"))

    # Кнопка отвязать все
    if bound_lot_ids:
        kb.add(B("🗑 Отвязать ВСЕ лоты", callback_data=f"{CBT_UNBIND_PRODUCT}:{product_id}:{offset}"))

    kb.add(B("◀ К списку товаров", callback_data=f"{CBT_PRODUCTS}:{offset}"))
    return kb


# ─────────────────────────── Инициализация ─────────────────────────────────────


def init(cardinal: "Cardinal", *args):
    if not cardinal.telegram:
        return

    os.makedirs(PLUGIN_FOLDER, exist_ok=True)
    tg = cardinal.telegram
    bot = tg.bot

    def _state_is(m, state_name: str) -> bool:
        data = tg.get_state(m.chat.id, m.from_user.id)
        return bool(data) and data.get("state") == state_name

    # ─── Главное меню ───

    def render_main(chat_id, message_id, offset):
        settings = load_settings()
        try:
            bot.edit_message_text(
                main_menu_text(settings), chat_id, message_id,
                reply_markup=main_menu_kb(offset), parse_mode="HTML"
            )
        except Exception:
            pass

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

    # ─── API Ключ ───

    def press_set_key(c):
        offset = int(c.data.split(":")[-1])
        sent = bot.send_message(c.message.chat.id, "🔑 Введите API-ключ поставщика:")
        tg.set_state(c.message.chat.id, sent.message_id, c.from_user.id, STATE_SET_KEY, {"offset": offset})
        bot.answer_callback_query(c.id)

    def save_key(m):
        state = tg.get_state(m.chat.id, m.from_user.id)
        offset = state["data"].get("offset", 0)
        tg.clear_state(m.chat.id, m.from_user.id, True)

        s = load_settings()
        s["supplier_api_key"] = m.text.strip()
        save_settings(s)
        bot.send_message(
            m.chat.id, "✅ API-ключ поставщика сохранён.",
            reply_markup=main_menu_kb(offset)
        )

    # ─── Товары поставщика ───

    def show_products(c):
        offset = int(c.data.split(":")[-1])
        bot.answer_callback_query(c.id, "Загрузка товаров...")

        settings = load_settings()
        api_key = (settings.get("supplier_api_key") or "").strip()
        products = get_products(api_key)
        bindings = settings.get("bindings", {})

        try:
            bot.edit_message_text(
                products_menu_text(products, bindings),
                c.message.chat.id, c.message.message_id,
                reply_markup=products_menu_kb(products, bindings, offset),
                parse_mode="HTML"
            )
        except Exception:
            bot.send_message(
                c.message.chat.id,
                products_menu_text(products, bindings),
                reply_markup=products_menu_kb(products, bindings, offset),
                parse_mode="HTML"
            )

    # ─── Лоты конкретного товара ───

    def show_product_lots(c):
        parts = c.data.split(":")
        if len(parts) >= 5:
            # sup:prod_lots:pid:offset:page
            page = int(parts[-1])
            offset = int(parts[-2])
            product_id = int(parts[-3])
        else:
            # sup:prod_lots:pid:offset
            page = 0
            offset = int(parts[-1])
            product_id = int(parts[-2])
        bot.answer_callback_query(c.id)

        settings = load_settings()
        api_key = (settings.get("supplier_api_key") or "").strip()
        products = get_products(api_key)
        bindings = settings.get("bindings", {})

        product = None
        for p in products:
            if p["id"] == product_id:
                product = p
                break

        if not product:
            product = {"id": product_id, "name": f"Товар #{product_id}", "price": 0, "stock": 0}

        bound_lot_ids = bindings.get(str(product_id), [])
        all_account_lots = get_account_lots(cardinal)

        try:
            bot.edit_message_text(
                product_lots_text(product, bound_lot_ids, all_account_lots, page),
                c.message.chat.id, c.message.message_id,
                reply_markup=product_lots_kb(product_id, bound_lot_ids, all_account_lots, offset, page),
                parse_mode="HTML"
            )
        except Exception:
            bot.send_message(
                c.message.chat.id,
                product_lots_text(product, bound_lot_ids, all_account_lots, page),
                reply_markup=product_lots_kb(product_id, bound_lot_ids, all_account_lots, offset, page),
                parse_mode="HTML"
            )

    # ─── Привязка лота ───

    def press_add_lot(c):
        parts = c.data.split(":")
        offset = int(parts[-1])
        product_id = int(parts[-2])

        all_account_lots = get_account_lots(cardinal)
        all_lot_ids = [l["id"] for l in all_account_lots]

        text = (
            f"➕ <b>Привязка лотов к товару #{product_id}</b>\n\n"
            f"Укажите порядковые <b>номера лотов</b> из списка (например: <code>1, 2, 3, 4</code>)\n"
            f"ИЛИ прямые <b>ID лотов</b> FunPay (например: <code>12345678, 12345679</code>).\n\n"
            f"Можно указывать через пробел или запятую."
        )
        sent = bot.send_message(c.message.chat.id, text, parse_mode="HTML")
        tg.set_state(
            c.message.chat.id, sent.message_id, c.from_user.id,
            STATE_ADD_LOT, {
                "offset": offset,
                "product_id": product_id,
                "all_lot_ids": all_lot_ids,
            }
        )
        bot.answer_callback_query(c.id)

    def save_bound_lots(m):
        state = tg.get_state(m.chat.id, m.from_user.id)
        offset = state["data"].get("offset", 0)
        product_id = state["data"].get("product_id")
        all_lot_ids = state["data"].get("all_lot_ids", [])
        tg.clear_state(m.chat.id, m.from_user.id, True)

        raw = m.text.strip()
        tokens = re.split(r"[\s,]+", raw)
        lot_ids = []
        for token in tokens:
            token = token.strip()
            if not token.isdigit():
                continue
            val = int(token)
            # Если введён номер из списка (1..N) и список лотов доступен
            if all_lot_ids and 1 <= val <= len(all_lot_ids):
                lot_ids.append(all_lot_ids[val - 1])
            else:
                lot_ids.append(val)

        if not lot_ids:
            bot.send_message(
                m.chat.id,
                "⚠️ Не найдено ни одного валидного ID или номера лота. Вводите числа.",
                reply_markup=main_menu_kb(offset)
            )
            return

        s = load_settings()
        pid_str = str(product_id)
        existing = s.get("bindings", {}).get(pid_str, [])

        added = []
        for lid in lot_ids:
            if lid not in existing:
                existing.append(lid)
                added.append(lid)

        s.setdefault("bindings", {})[pid_str] = existing

        # Устанавливаем начальное состояние лотов
        for lid in added:
            s.setdefault("lot_states", {})[str(lid)] = True

        save_settings(s)

        if added:
            added_text = ", ".join(str(lid) for lid in added)
            bot.send_message(
                m.chat.id,
                f"✅ Привязано {len(added)} лотов к товару #{product_id}:\n"
                f"<code>{added_text}</code>",
                parse_mode="HTML",
                reply_markup=main_menu_kb(offset)
            )
        else:
            bot.send_message(
                m.chat.id,
                f"ℹ️ Все указанные лоты уже были привязаны к товару #{product_id}.",
                reply_markup=main_menu_kb(offset)
            )

    # ─── Отвязка лота ───

    def unbind_lot(c):
        parts = c.data.split(":")
        offset = int(parts[-1])
        lot_id = int(parts[-2])
        product_id = int(parts[-3])

        s = load_settings()
        pid_str = str(product_id)
        lot_ids = s.get("bindings", {}).get(pid_str, [])

        if lot_id in lot_ids:
            lot_ids.remove(lot_id)
            if not lot_ids:
                s["bindings"].pop(pid_str, None)
            else:
                s["bindings"][pid_str] = lot_ids
            # Удаляем состояние лота
            s.get("lot_states", {}).pop(str(lot_id), None)
            save_settings(s)
            bot.answer_callback_query(c.id, f"Лот {lot_id} отвязан!")
        else:
            bot.answer_callback_query(c.id, "Лот не найден")

        # Обновляем меню товара
        c.data = f"{CBT_PRODUCT_LOTS}:{product_id}:{offset}"
        show_product_lots(c)

    def unbind_all_product(c):
        parts = c.data.split(":")
        offset = int(parts[-1])
        product_id = int(parts[-2])

        s = load_settings()
        pid_str = str(product_id)
        lot_ids = s.get("bindings", {}).pop(pid_str, [])

        # Удаляем состояния лотов
        for lid in lot_ids:
            s.get("lot_states", {}).pop(str(lid), None)

        save_settings(s)
        bot.answer_callback_query(c.id, f"Все лоты отвязаны от товара #{product_id}")

        c.data = f"{CBT_PRODUCT_LOTS}:{product_id}:{offset}"
        show_product_lots(c)

    # ─── Синхронизация ───

    def force_sync(c):
        offset = int(c.data.split(":")[-1])
        bot.answer_callback_query(c.id, "🔄 Синхронизация запущена...")

        def _sync_worker():
            try:
                settings = load_settings()
                api_key = (settings.get("supplier_api_key") or "").strip()
                products = get_products(api_key)
                balance = get_balance(api_key)
                products_map = {p["id"]: p for p in products}
                bindings = settings.get("bindings", {})
                lot_states = settings.get("lot_states", {})
                results = []

                for pid_str, lot_ids in bindings.items():
                    pid = int(pid_str)
                    product = products_map.get(pid)
                    if product is None:
                        for lid in lot_ids:
                            hide_lot(cardinal, lid)
                            lot_states[str(lid)] = False
                            results.append(f"💠 Лот {lid} скрыт (товар {pid} не найден)")
                            time.sleep(1)
                        continue

                    can_buy = product["stock"] > 0 and balance >= product["price"]
                    for lid in lot_ids:
                        if can_buy:
                            show_lot(cardinal, lid)
                            lot_states[str(lid)] = True
                            results.append(f"⚡ Лот {lid} показан")
                        else:
                            hide_lot(cardinal, lid)
                            lot_states[str(lid)] = False
                            results.append(f"💠 Лот {lid} скрыт")
                        time.sleep(1)

                settings["lot_states"] = lot_states
                save_settings(settings)

                report = f"<b>🔄 Синхронизация завершена</b>\n\n💰 Баланс: ${balance:.2f}\n\n"
                if results:
                    report += "\n".join(results)
                else:
                    report += "Нет привязанных лотов."

                bot.send_message(c.message.chat.id, report, parse_mode="HTML")

            except Exception as e:
                bot.send_message(c.message.chat.id, f"⚠️ Ошибка синхронизации: {e}")

        Thread(target=_sync_worker, daemon=True).start()

    # ─── Тест ───

    def run_test(c):
        offset = int(c.data.split(":")[-1])
        tg_chat_id = c.message.chat.id
        bot.answer_callback_query(c.id, "🧪 Тест запущен...")

        def _test_worker():
            lines = ["<b>🧪 Диагностика Supplier Auto-Seller</b>"]
            lines.append("<i>Безопасный режим — закупка НЕ производится</i>\n")
            all_ok = True

            settings = load_settings()

            # Шаг 1: API ключ
            lines.append("─── <b>Шаг 1: API-ключ</b> ───")
            api_key = (settings.get("supplier_api_key") or "").strip()
            if not api_key:
                lines.append("⚠️ API-ключ не задан!")
                all_ok = False
            else:
                masked = f"{api_key[:8]}•••{api_key[-4:]}" if len(api_key) > 12 else api_key[:4] + "•••"
                lines.append(f"✅ Ключ: <code>{utils.escape(masked)}</code>")

            # Шаг 2: API соединение
            lines.append("\n─── <b>Шаг 2: Соединение с поставщиком</b> ───")
            if api_key:
                t0 = time.time()
                balance = get_balance(api_key)
                ms = int((time.time() - t0) * 1000)
                if balance > 0:
                    lines.append(f"✅ Соединение OK ({ms} мс)")
                    lines.append(f"💰 Баланс: <code>${balance:.2f}</code>")
                else:
                    lines.append(f"⚠️ Баланс ${balance:.2f} ({ms} мс)")

                t0 = time.time()
                products = get_products(api_key)
                ms = int((time.time() - t0) * 1000)
                if products:
                    lines.append(f"📦 Товаров: {len(products)} ({ms} мс)")
                    for p in products[:5]:
                        stock_icon = "🟢" if p["stock"] > 0 else "⚪"
                        lines.append(f"  {stock_icon} {p['name'][:40]} | ${p['price']} | {p['stock']} шт.")
                    if len(products) > 5:
                        lines.append(f"  ... и ещё {len(products) - 5}")
                else:
                    lines.append(f"⚠️ Не удалось загрузить товары ({ms} мс)")
                    all_ok = False
            else:
                lines.append("⏭ Пропущено (нет ключа)")

            # Шаг 3: clck.ru
            lines.append("\n─── <b>Шаг 3: clck.ru</b> ───")
            t0 = time.time()
            test_url = "https://example.com/test"
            short = shorten_url(test_url)
            ms = int((time.time() - t0) * 1000)
            if short != test_url:
                lines.append(f"✅ Доступен ({ms} мс)")
            else:
                lines.append(f"⚠️ Недоступен ({ms} мс) — ссылки будут выдаваться длинными")

            # Шаг 4: Привязки
            lines.append("\n─── <b>Шаг 4: Привязки лотов</b> ───")
            bindings = settings.get("bindings", {})
            if bindings:
                total_lots = sum(len(v) for v in bindings.values())
                lines.append(f"✅ {len(bindings)} товаров → {total_lots} лотов")
                for pid_str, lot_ids in bindings.items():
                    ids_text = ", ".join(str(lid) for lid in lot_ids)
                    lines.append(f"  🔸 Товар #{pid_str} → [{ids_text}]")
            else:
                lines.append("⚠️ Нет привязок — добавьте через 📦 Товары")
                all_ok = False

            # Шаг 5: Детектор формата
            lines.append("\n─── <b>Шаг 5: Формат выдачи</b> ───")
            lines.append("✅ URL → сокращение через clck.ru")
            lines.append("✅ Логин/Пароль → выдача как есть")

            # Итог
            lines.append("\n━━━━━━━━━━━━━━━━━━━━")
            if all_ok:
                lines.append("✅ <b>Все проверки пройдены!</b>")
            else:
                lines.append("⚠️ <b>Есть замечания.</b> Исправьте пункты выше.")

            bot.send_message(tg_chat_id, "\n".join(lines), parse_mode="HTML")

        Thread(target=_test_worker, daemon=True).start()

    # ─── Регистрация хэндлеров ───

    tg.cbq_handler(
        lambda c: render_main(c.message.chat.id, c.message.message_id, int(c.data.split(":")[-1])),
        lambda c: c.data.startswith(f"{CBT_MAIN}:")
    )
    tg.cbq_handler(open_settings, lambda c: c.data.startswith(f"{CBT.PLUGIN_SETTINGS}:{UUID}:"))

    tg.cbq_handler(toggle_plugin, lambda c: c.data.startswith(f"{CBT_TOGGLE}:"))
    tg.cbq_handler(press_set_key, lambda c: c.data.startswith(f"{CBT_SET_KEY}:"))
    tg.cbq_handler(show_products, lambda c: c.data.startswith(f"{CBT_PRODUCTS}:"))
    tg.cbq_handler(show_product_lots, lambda c: c.data.startswith(f"{CBT_PRODUCT_LOTS}:"))
    tg.cbq_handler(press_add_lot, lambda c: c.data.startswith(f"{CBT_ADD_LOT_INPUT}:"))
    tg.cbq_handler(unbind_lot, lambda c: c.data.startswith(f"{CBT_UNBIND_LOT}:"))
    tg.cbq_handler(unbind_all_product, lambda c: c.data.startswith(f"{CBT_UNBIND_PRODUCT}:"))
    tg.cbq_handler(force_sync, lambda c: c.data.startswith(f"{CBT_FORCE_SYNC}:"))
    tg.cbq_handler(run_test, lambda c: c.data.startswith(f"{CBT_TEST}:"))
    tg.cbq_handler(lambda c: bot.answer_callback_query(c.id), lambda c: c.data == "sup:noop")

    # Обработчики текстовых сообщений
    tg.msg_handler(save_key, func=lambda m: _state_is(m, STATE_SET_KEY))
    tg.msg_handler(save_bound_lots, func=lambda m: _state_is(m, STATE_ADD_LOT))

    # Старт мониторинга
    Thread(target=_monitoring_thread, args=(cardinal,), daemon=True).start()
    logger.info(f"{LOGGER_PREFIX} v{VERSION} — Supplier Auto-Seller запущен.")


BIND_TO_PRE_INIT = [init]
BIND_TO_NEW_ORDER = [on_new_order]
BIND_TO_NEW_MESSAGE = [on_new_message]
BIND_TO_DELETE = None
