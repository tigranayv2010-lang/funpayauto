from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from threading import Thread
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cardinal import Cardinal

from bs4 import BeautifulSoup
from FunPayAPI.account import Account

import telebot
from telebot.types import InlineKeyboardMarkup as K, InlineKeyboardButton as B
from tg_bot import CBT, utils

# ─────────────────────────── Метаданные плагина ───────────────────────────────

NAME = "Notify Old Orders"
VERSION = "1.0.0"
DESCRIPTION = (
    "Отправляет заданное сообщение всем покупателям, у которых есть "
    "неподтверждённые заказы старше 24 часов.\n\n"
    "Команда /notify_old_orders запускает рассылку."
)
CREDITS = "@tigranayvvv"
UUID = "c5d77fc5-da00-4715-9735-c77cf9222f5e"
SETTINGS_PAGE = True

# ─────────────────────────── Константы ────────────────────────────────────────

PLUGIN_FOLDER = f"storage/plugins/{UUID}/"
SETTINGS_FILE = f"{PLUGIN_FOLDER}settings.json"
LOGGER_PREFIX = "[NOTIFY OLD ORDERS]"
logger = logging.getLogger("FPC.notify_old_orders")

# Callback-метки для inline-кнопок
CBT_OPEN_SETTINGS   = f"{UUID}:open_settings"
CBT_EDIT_MESSAGE    = f"{UUID}:edit_message"
CBT_TOGGLE_WATERMARK = f"{UUID}:toggle_watermark"
CBT_SAVE_MESSAGE    = f"{UUID}:save_message"

# Настройки по умолчанию
DEFAULT_SETTINGS = {
    "message": (
        "Здравствуйте! Ваш заказ ожидает подтверждения уже более 24 часов. "
        "Пожалуйста, подтвердите получение, если всё в порядке. "
        "Спасибо!"
    ),
    "watermark": True,
}

# ─────────────────────────── Работа с настройками ─────────────────────────────

def load_settings() -> dict:
    if not os.path.exists(SETTINGS_FILE):
        return DEFAULT_SETTINGS.copy()
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Добавляем недостающие ключи, если файл старый
        for k, v in DEFAULT_SETTINGS.items():
            data.setdefault(k, v)
        return data
    except Exception:
        logger.warning(f"{LOGGER_PREFIX} Не удалось прочитать настройки, используем дефолтные.")
        return DEFAULT_SETTINGS.copy()


def save_settings(settings: dict) -> None:
    os.makedirs(PLUGIN_FOLDER, exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


# ─────────────────────────── Работа с заказами ────────────────────────────────

def get_old_orders_page(acc: Account, start_from: str, subcs, locale):
    """Возвращает (next_start_from, [(order_id, buyer_username)], locale, subcs)."""
    attempts = 3
    while attempts:
        try:
            result = acc.get_sales(
                start_from=start_from or None,
                state="paid",
                locale=locale,
                sudcategories=subcs,
            )
            break
        except Exception:
            attempts -= 1
            time.sleep(1)
    else:
        raise RuntimeError("Не удалось получить заказы после нескольких попыток")

    orders_raw = result[1]
    old_orders = []
    for order in orders_raw:
        parser = BeautifulSoup(order.html, "lxml")
        time_text = parser.find("div", {"class": "tc-date-time"})
        if time_text and any(
            kw in time_text.text for kw in ["сегодня", "сьогодні", "today"]
        ):
            continue
        if (datetime.now() - order.date).total_seconds() < 3600 * 24:
            continue
        # Получаем username покупателя из объекта заказа
        buyer = getattr(order, "buyer_username", None) or getattr(order, "username", None)
        if buyer is None:
            buyer_tag = parser.find("a", {"class": "tc-user"})
            if buyer_tag:
                buyer = buyer_tag.text.strip()
        old_orders.append((order.id, buyer))

    return result[0], old_orders, result[2], result[3]


def get_all_old_orders(acc: Account) -> list[tuple[str, str | None]]:
    """Возвращает список (order_id, buyer_username) для заказов старше 24 ч."""
    start_from = ""
    all_old = []
    locale = None
    subcs = None
    while start_from is not None:
        next_sf, page_orders, locale, subcs = get_old_orders_page(
            acc, start_from, subcs, locale
        )
        all_old.extend(page_orders)
        start_from = next_sf
        time.sleep(1)
    return all_old


# ─────────────────────────── UI настроек ──────────────────────────────────────

def settings_keyboard(offset: int) -> K:
    settings = load_settings()
    watermark_label = "🟢 Водяной знак включён" if settings["watermark"] else "🔴 Водяной знак выключен"
    kb = K()
    kb.add(B("✏️ Изменить текст сообщения", callback_data=f"{CBT_EDIT_MESSAGE}:{offset}"))
    kb.add(B(watermark_label, callback_data=f"{CBT_TOGGLE_WATERMARK}:{offset}"))
    kb.add(B("◀ Назад", callback_data=f"{CBT.EDIT_PLUGIN}:{UUID}:{offset}"))
    return kb


def build_preview(settings: dict) -> str:
    msg = settings["message"]
    if settings["watermark"]:
        msg += "\n\n[ 🤍TigSam🖤 ]"
    return msg


# ─────────────────────────── Основная логика рассылки ─────────────────────────

def run_notify(cardinal: Cardinal, bot: telebot.TeleBot, reply_msg: telebot.types.Message) -> None:
    """Выполняется в отдельном потоке."""
    acc = cardinal.account
    settings = load_settings()

    status_msg = bot.reply_to(reply_msg, "⏳ Сканирую заказы старше 24 часов…")

    try:
        old_orders = get_all_old_orders(acc)
    except Exception:
        logger.warning(f"{LOGGER_PREFIX} Ошибка при получении заказов")
        logger.debug("TRACEBACK", exc_info=True)
        bot.edit_message_text(
            "❌ Не удалось получить список заказов.",
            status_msg.chat.id, status_msg.message_id,
        )
        return

    if not old_orders:
        bot.edit_message_text(
            "✅ Нет неподтверждённых заказов старше 24 часов.",
            status_msg.chat.id, status_msg.message_id,
        )
        return

    text_to_send = build_preview(settings)

    # Группируем заказы по покупателю, чтобы не спамить одному человеку
    buyers: dict[str, list[str]] = {}
    no_buyer = []
    for order_id, buyer in old_orders:
        if buyer:
            buyers.setdefault(buyer, []).append(order_id)
        else:
            no_buyer.append(order_id)

    total = len(buyers) + len(no_buyer)
    bot.edit_message_text(
        f"📨 Найдено заказов: {len(old_orders)} (уникальных покупателей: {len(buyers)}). "
        f"Начинаю рассылку…",
        status_msg.chat.id, status_msg.message_id,
    )

    sent = 0
    errors = 0

    # Отправляем сообщение каждому уникальному покупателю
    for buyer_username, order_ids in buyers.items():
        orders_list = ", ".join(f"#{oid}" for oid in order_ids)
        full_text = (
            f"{text_to_send}\n\n"
            f"Заказ(ы): {orders_list}"
        )
        try:
            # get_chat_by_name создаёт чат если его нет в кэше — работает даже без истории переписки
            chat = acc.get_chat_by_name(buyer_username)
            if chat is None:
                logger.warning(f"{LOGGER_PREFIX} Не удалось получить чат с {buyer_username}, пропускаем")
                errors += 1
                time.sleep(2)
                continue

            # cardinal.send_message — правильный метод, учитывает watermark и логирование Cardinal
            result = cardinal.send_message(chat.id, full_text, buyer_username, watermark=False)
            if result:
                sent += 1
                logger.info(f"{LOGGER_PREFIX} Отправлено сообщение покупателю {buyer_username} (chat_id={chat.id})")
            else:
                logger.warning(f"{LOGGER_PREFIX} send_message вернул None для {buyer_username}")
                errors += 1
        except Exception:
            logger.warning(f"{LOGGER_PREFIX} Ошибка отправки {buyer_username}")
            logger.debug("TRACEBACK", exc_info=True)
            errors += 1
        time.sleep(2)  # Пауза между отправками, чтобы не триггерить rate-limit FunPay

    result_text = (
        f"✅ Рассылка завершена!\n\n"
        f"📦 Всего старых заказов: {len(old_orders)}\n"
        f"👤 Уникальных покупателей: {len(buyers)}\n"
        f"✉️ Успешно отправлено: {sent}\n"
        f"❌ Ошибок: {errors}"
    )
    if no_buyer:
        result_text += f"\n⚠️ Заказы без определённого покупателя: {len(no_buyer)}"

    bot.edit_message_text(result_text, status_msg.chat.id, status_msg.message_id)


# ─────────────────────────── Инициализация плагина ────────────────────────────

def init(cardinal: Cardinal, *args):
    if not cardinal.telegram:
        return

    os.makedirs(PLUGIN_FOLDER, exist_ok=True)

    tg = cardinal.telegram
    bot = tg.bot

    # ── Команда /notify_old_orders ──────────────────────────────────────────

    def cmd_notify(m: telebot.types.Message):
        Thread(target=run_notify, args=(cardinal, bot, m), daemon=True).start()

    tg.msg_handler(cmd_notify, commands=["notify_old_orders"])
    cardinal.add_telegram_commands(UUID, [
        ("notify_old_orders", "отправить напоминание покупателям с заказами >24ч", True),
    ])

    # ── Страница настроек (SETTINGS_PAGE = True) ────────────────────────────

    def open_settings(c: telebot.types.CallbackQuery):
        offset = int(c.data.split(":")[2])
        settings = load_settings()
        preview = utils.escape(build_preview(settings))
        text = (
            f"<b>⚙️ Настройки плагина Notify Old Orders</b>\n\n"
            f"<b>Текущий текст сообщения:</b>\n"
            f"<i>{preview}</i>"
        )
        bot.edit_message_text(
            text,
            c.message.chat.id, c.message.message_id,
            reply_markup=settings_keyboard(offset),
        )
        bot.answer_callback_query(c.id)

    def toggle_watermark(c: telebot.types.CallbackQuery):
        offset = int(c.data.split(":")[2])
        settings = load_settings()
        settings["watermark"] = not settings["watermark"]
        save_settings(settings)
        preview = utils.escape(build_preview(settings))
        text = (
            f"<b>⚙️ Настройки плагина Notify Old Orders</b>\n\n"
            f"<b>Текущий текст сообщения:</b>\n"
            f"<i>{preview}</i>"
        )
        bot.edit_message_text(
            text,
            c.message.chat.id, c.message.message_id,
            reply_markup=settings_keyboard(offset),
        )
        bot.answer_callback_query(c.id, "Водяной знак обновлён!")

    def ask_edit_message(c: telebot.types.CallbackQuery):
        offset = int(c.data.split(":")[2])
        sent = bot.send_message(
            c.message.chat.id,
            "✏️ Введите новый текст сообщения для рассылки:\n"
            "(отправьте /cancel для отмены)",
        )
        tg.set_state(
            c.message.chat.id, sent.message_id, c.from_user.id,
            CBT_SAVE_MESSAGE, {"offset": offset},
        )
        bot.answer_callback_query(c.id)

    def save_new_message(m: telebot.types.Message):
        state_data = tg.get_state(m.chat.id, m.from_user.id)
        if not state_data or state_data.get("state") != CBT_SAVE_MESSAGE:
            return
        offset = state_data["data"].get("offset", 0)
        tg.clear_state(m.chat.id, m.from_user.id, True)

        new_text = m.text.strip()
        if new_text == "/cancel":
            bot.send_message(m.chat.id, "❌ Редактирование отменено.")
            return

        settings = load_settings()
        settings["message"] = new_text
        save_settings(settings)

        preview = utils.escape(build_preview(settings))
        bot.send_message(
            m.chat.id,
            f"✅ Текст сообщения обновлён!\n\n"
            f"<b>Предпросмотр:</b>\n<i>{preview}</i>\n\n"
            f"Вернитесь в настройки плагина через меню.",
        )

    # Регистрируем callback-хэндлеры
    tg.cbq_handler(open_settings,     lambda c: c.data.startswith(f"{CBT.PLUGIN_SETTINGS}:{UUID}:"))
    tg.cbq_handler(toggle_watermark,  lambda c: c.data.startswith(f"{CBT_TOGGLE_WATERMARK}:"))
    tg.cbq_handler(ask_edit_message,  lambda c: c.data.startswith(f"{CBT_EDIT_MESSAGE}:"))

    # Хэндлер для приёма нового текста
    tg.msg_handler(save_new_message, func=lambda m: (
        tg.get_state(m.chat.id, m.from_user.id) or {}
    ).get("state") == CBT_SAVE_MESSAGE)

    logger.info(f"{LOGGER_PREFIX} Плагин инициализирован.")


BIND_TO_PRE_INIT = [init]
BIND_TO_DELETE = None
