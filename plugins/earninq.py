from __future__ import annotations

import logging
import time
from datetime import datetime
from threading import Thread
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cardinal import Cardinal

from bs4 import BeautifulSoup
from FunPayAPI.account import Account

import telebot

NAME = "Earnings"
VERSION = "1.0.0"
DESCRIPTION = (
    "Показывает сумму заработка и количество выполненных заказов за указанный период.\n\n"
    "Использование: /earnings <часы>\n"
    "Примеры: /earnings 24 — за последние 24 часа\n"
    "         /earnings 168 — за последние 7 дней"
)
CREDITS = "@tigranayvvv"
UUID = "e0ed79f3-6924-4094-8fba-e925d8a27bec"
SETTINGS_PAGE = False

logger = logging.getLogger("FPC.earnings")
LOGGER_PREFIX = "[EARNINGS]"


def get_earnings_page(
    acc: Account,
    start_from: str,
    subcs: dict,
    locale,
    hours: float,
) -> tuple[str | None, list[dict], str, dict, bool]:
    """
    Возвращает (next_start_from, [{"amount": float, "id": str, "date": datetime}], locale, subcs, stop).
    stop=True означает что дошли до заказов старше нужного периода — дальше пагинировать не нужно.
    """
    attempts = 3
    while attempts:
        try:
            result = acc.get_sales(
                start_from=start_from or None,
                state="closed",   # закрытые (подтверждённые) заказы
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
    period_seconds = hours * 3600
    found = []
    stop = False

    for order in orders_raw:
        age = (datetime.now() - order.date).total_seconds()
        if age > period_seconds:
            # Все последующие заказы ещё старше — останавливаемся
            stop = True
            break

        # Парсим сумму из HTML
        parser = BeautifulSoup(order.html, "lxml")
        price_tag = parser.find("div", {"class": "tc-price"})
        if price_tag is None:
            price_tag = parser.find("span", {"class": "tc-price"})

        amount = 0.0
        if price_tag:
            raw = price_tag.text.strip().replace("\xa0", "").replace(" ", "")
            # Убираем всё кроме цифр, точки и запятой
            digits = ""
            for ch in raw:
                if ch.isdigit() or ch in ".,":
                    digits += ch
            digits = digits.replace(",", ".")
            try:
                amount = float(digits)
            except ValueError:
                amount = 0.0

        found.append({
            "id": order.id,
            "amount": amount,
            "date": order.date,
        })

    return result[0], found, result[2], result[3], stop


def get_all_earnings(acc: Account, hours: float) -> list[dict]:
    """Собирает все закрытые заказы за последние N часов."""
    start_from = ""
    all_orders = []
    locale = None
    subcs = None

    while start_from is not None:
        next_sf, page_orders, locale, subcs, stop = get_earnings_page(
            acc, start_from, subcs, locale, hours
        )
        all_orders.extend(page_orders)
        if stop:
            break
        start_from = next_sf
        time.sleep(1)

    return all_orders


def format_hours(hours: float) -> str:
    """Красиво форматирует период."""
    if hours < 1:
        mins = int(hours * 60)
        return f"{mins} мин"
    if hours % 24 == 0:
        days = int(hours // 24)
        if days == 1:
            return "1 день"
        if 2 <= days <= 4:
            return f"{days} дня"
        return f"{days} дней"
    h = int(hours)
    if h == 1:
        return "1 час"
    if 2 <= h <= 4:
        return f"{h} часа"
    return f"{h} часов"


def run_earnings(cardinal: Cardinal, bot: telebot.TeleBot, m: telebot.types.Message, hours: float):
    acc = cardinal.account
    period_label = format_hours(hours)

    status_msg = bot.reply_to(m, f"⏳ Считаю заработок за последние {period_label}…")

    try:
        orders = get_all_earnings(acc, hours)
    except Exception:
        logger.warning(f"{LOGGER_PREFIX} Ошибка при получении заказов")
        logger.debug("TRACEBACK", exc_info=True)
        bot.edit_message_text(
            "❌ Не удалось получить список заказов.",
            status_msg.chat.id, status_msg.message_id,
        )
        return

    if not orders:
        bot.edit_message_text(
            f"📭 За последние {period_label} закрытых заказов не найдено.",
            status_msg.chat.id, status_msg.message_id,
        )
        return

    total_amount = sum(o["amount"] for o in orders)
    total_count = len(orders)
    avg_amount = total_amount / total_count if total_count else 0

    # Считаем за час и за день для справки
    per_hour = total_amount / hours if hours > 0 else 0
    per_day = per_hour * 24

    text = (
        f"💰 <b>Заработок за последние {period_label}</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📦 Заказов выполнено: <b>{total_count}</b>\n"
        f"💵 Сумма: <b>{total_amount:.2f} ₽</b>\n"
        f"📊 Средний чек: <b>{avg_amount:.2f} ₽</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"⏱ В среднем в час: <b>{per_hour:.2f} ₽</b>\n"
        f"📅 Проекция на день: <b>{per_day:.2f} ₽</b>"
    )

    bot.edit_message_text(text, status_msg.chat.id, status_msg.message_id)
    logger.info(
        f"{LOGGER_PREFIX} Запрос за {period_label}: {total_count} заказов, {total_amount:.2f} ₽"
    )


def init(cardinal: Cardinal, *args):
    if not cardinal.telegram:
        return

    tg = cardinal.telegram
    bot = tg.bot
    acc = cardinal.account

    def cmd_earnings(m: telebot.types.Message):
        args = m.text.strip().split()

        # Парсим аргумент часов
        if len(args) < 2:
            bot.reply_to(
                m,
                "⚠️ Укажите количество часов.\n"
                "Пример: <code>/earnings 24</code>",
            )
            return

        try:
            hours = float(args[1].replace(",", "."))
            if hours <= 0:
                raise ValueError
        except ValueError:
            bot.reply_to(
                m,
                "⚠️ Некорректное значение. Укажите число часов больше 0.\n"
                "Пример: <code>/earnings 24</code>",
            )
            return

        if hours > 8760:  # больше года — скорее всего ошибка
            bot.reply_to(m, "⚠️ Максимальный период — 8760 часов (1 год).")
            return

        Thread(
            target=run_earnings,
            args=(cardinal, bot, m, hours),
            daemon=True,
        ).start()

    tg.msg_handler(cmd_earnings, commands=["earnings"])
    cardinal.add_telegram_commands(UUID, [
        ("earnings", "заработок за указанное кол-во часов. Пример: /earnings 24", True),
    ])

    logger.info(f"{LOGGER_PREFIX} Плагин инициализирован.")


BIND_TO_PRE_INIT = [init]
BIND_TO_DELETE = None
