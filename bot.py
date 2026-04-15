#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging
import random
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ================== КОНФИГУРАЦИЯ ==================
TOKEN = "7991920232:AAEKMDzj0s4L8U81pNK4EVpeEazn0UoJYv0"

DB_PATH = "alcometr.db"

COOLDOWN_SECONDS = 5 * 60          # 5 минут
MAX_MESSAGE_AGE = 10               # игнорировать сообщения старше 10 сек

BASE_VOLUME = 0.5                  # базовая порция алкоголя, л
VOLUME_INCREMENT = 0.05            # прирост за каждое употребление

BOTTLE_DROP_CHANCE = 0.25          # шанс выпадения бутылки при "алко"

CASINO_WIN_CHANCE = 0.5            # шанс выигрыша в казино на одну бутылку
CASINO_BASE_WIN = 0.5              # базовый выигрыш литров
CASINO_BONUS_PER_DRINK = 0.05      # бонус за каждое предыдущее употребление

SECRET_COOLDOWN_DAYS = 2           # секретную бутылку можно получать раз в 2 дня
SECRET_ATTEMPT_TIMEOUT = 60        # время (сек) между попытками секретной команды

# Начальные данные (username, литры, бутылки)
INITIAL_USERS = [
    ("Nas_tiusk_a", 12.75, 1),
    ("Posysidon", 6.30, 2),
    ("zerlib", 4.55, 0),
    ("Pr1nsexxg", 3.00, 0),
    ("marianckiu", 1.65, 0),
    ("ngkilj", 0.50, 0),
    ("Ppdastr01", 0.50, 0),
    ("Dimas?", 0.50, 0),
]

# ================== БАЗА ДАННЫХ ==================
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                total_volume REAL DEFAULT 0,
                drink_count INTEGER DEFAULT 0,
                last_drink_time INTEGER DEFAULT 0,
                bottles INTEGER DEFAULT 0,
                last_secret_time INTEGER DEFAULT 0,
                secret_attempts INTEGER DEFAULT 0
            )
        """)
        # Добавляем новые поля, если таблица уже существовала
        try:
            conn.execute("ALTER TABLE users ADD COLUMN bottles INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE users ADD COLUMN last_secret_time INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE users ADD COLUMN secret_attempts INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass

        # Проверяем, есть ли уже пользователи
        cur = conn.execute("SELECT COUNT(*) FROM users").fetchone()
        if cur[0] == 0:
            for idx, (uname, vol, btl) in enumerate(INITIAL_USERS, start=1):
                conn.execute("""
                    INSERT INTO users (user_id, username, total_volume, bottles, drink_count, last_drink_time)
                    VALUES (?, ?, ?, ?, 0, 0)
                """, (-idx, uname, vol, btl))
        conn.commit()

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def get_user(user_id: int):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()

def update_user_alco(user_id: int, username: str, added_volume: float, bottle_gained: bool):
    now = int(time.time())
    with get_db() as conn:
        cur = conn.execute(
            "SELECT drink_count, bottles FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if cur is None:
            drink_count = 0
            bottles = 1 if bottle_gained else 0
            conn.execute("""
                INSERT INTO users (user_id, username, total_volume, drink_count, last_drink_time, bottles)
                VALUES (?, ?, ?, 1, ?, ?)
            """, (user_id, username, added_volume, now, bottles))
        else:
            drink_count = cur["drink_count"]
            bottles = cur["bottles"] + (1 if bottle_gained else 0)
            conn.execute("""
                UPDATE users SET
                    username = ?,
                    total_volume = total_volume + ?,
                    drink_count = drink_count + 1,
                    last_drink_time = ?,
                    bottles = ?
                WHERE user_id = ?
            """, (username, added_volume, now, bottles, user_id))
        conn.commit()
        row = conn.execute(
            "SELECT total_volume, bottles FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row["total_volume"], row["bottles"]

def update_user_casino(user_id: int, username: str, bet: int, won_count: int, liters_won: float):
    with get_db() as conn:
        conn.execute("""
            UPDATE users SET
                username = ?,
                total_volume = total_volume + ?,
                bottles = bottles - ?
            WHERE user_id = ?
        """, (username, liters_won, bet, user_id))
        conn.commit()
        row = conn.execute(
            "SELECT total_volume, bottles FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row["total_volume"], row["bottles"]

def transfer_bottles(sender_id: int, receiver_id: int, amount: int) -> tuple[bool, str]:
    with get_db() as conn:
        sender = conn.execute(
            "SELECT username, bottles FROM users WHERE user_id = ?", (sender_id,)
        ).fetchone()
        if not sender:
            return False, "У тебя нет профиля. Сначала используй команду 'алко'."
        if sender["bottles"] < amount:
            return False, f"У тебя только {sender['bottles']} бутылок 🍾, не хватает."

        receiver = conn.execute(
            "SELECT user_id FROM users WHERE user_id = ?", (receiver_id,)
        ).fetchone()
        if not receiver:
            return False, "Получатель не найден в базе. Пусть сначала сыграет в 'алко'."

        conn.execute(
            "UPDATE users SET bottles = bottles - ? WHERE user_id = ?",
            (amount, sender_id)
        )
        conn.execute(
            "UPDATE users SET bottles = bottles + ? WHERE user_id = ?",
            (amount, receiver_id)
        )
        conn.commit()
        return True, ""

def get_top_users(limit=20):
    with get_db() as conn:
        return conn.execute(
            "SELECT username, total_volume FROM users WHERE user_id > 0 ORDER BY total_volume DESC LIMIT ?",
            (limit,)
        ).fetchall()

def update_secret_attempt(user_id: int, username: str) -> tuple[str, bool]:
    now = int(time.time())
    two_days = SECRET_COOLDOWN_DAYS * 24 * 3600
    with get_db() as conn:
        cur = conn.execute(
            "SELECT last_secret_time, secret_attempts, bottles FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()
        if cur is None:
            conn.execute("""
                INSERT INTO users (user_id, username, last_secret_time, secret_attempts, bottles)
                VALUES (?, ?, ?, 1, 0)
            """, (user_id, username, now))
            conn.commit()
            return "🙅‍♂️ нет я не даю бутылки", False

        last_time = cur["last_secret_time"]
        attempts = cur["secret_attempts"]

        # Проверяем кулдаун 2 дня
        if last_time > 0 and (now - last_time) < two_days:
            remaining = two_days - (now - last_time)
            days = remaining // (24 * 3600)
            hours = (remaining % (24 * 3600)) // 3600
            return f"😮‍💨 я давал, подожди ещё {days} дн. {hours} ч.", False

        # Если прошло больше 2 дней, сбрасываем попытки
        if last_time > 0 and (now - last_time) >= two_days:
            attempts = 0

        if attempts == 0:
            conn.execute("""
                UPDATE users SET
                    last_secret_time = ?,
                    secret_attempts = 1
                WHERE user_id = ?
            """, (now, user_id))
            conn.commit()
            return "🙅‍♂️ нет я не даю бутылки", False
        else:
            if now - last_time > SECRET_ATTEMPT_TIMEOUT:
                conn.execute("""
                    UPDATE users SET
                        last_secret_time = ?,
                        secret_attempts = 1
                    WHERE user_id = ?
                """, (now, user_id))
                conn.commit()
                return "🙅‍♂️ нет я не даю бутылки", False
            else:
                conn.execute("""
                    UPDATE users SET
                        last_secret_time = ?,
                        secret_attempts = 0,
                        bottles = bottles + 1
                    WHERE user_id = ?
                """, (now, user_id))
                conn.commit()
                return f"{username}, разве ты не отстанешь?\nДа ну ладно, на\n{username} выдано 1 бутылка 🍾", True

# ================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==================
def format_username(user) -> str:
    if user.username:
        return f"@{user.username}"
    return user.first_name

def can_drink(last_drink_time: int) -> tuple:
    now = int(time.time())
    diff = now - last_drink_time
    if diff >= COOLDOWN_SECONDS or last_drink_time == 0:
        return True, 0
    return False, COOLDOWN_SECONDS - diff

def calculate_added_volume(drink_count: int) -> float:
    return BASE_VOLUME + VOLUME_INCREMENT * drink_count

def is_message_too_old(update: Update) -> bool:
    if not update.message or not update.message.date:
        return False
    msg_time = update.message.date.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    age = (now - msg_time).total_seconds()
    return age > MAX_MESSAGE_AGE

def calculate_casino_win(drink_count: int) -> float:
    return CASINO_BASE_WIN + CASINO_BONUS_PER_DRINK * drink_count

# ================== ОБРАБОТЧИКИ КОМАНД ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = format_username(user)
    text = (
        f"🍺 {username}, добро пожаловать в алкогольный игровой бот!\n"
        "Напиши <b>помощь</b> чтобы увидеть команды\n"
        "Напиши <b>топ алко</b> чтобы увидеть топ\n"
        "Напиши <b>алко</b> чтобы начать"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🍺 Доступные команды:\n"
        "<b>алко</b> – отметить, что выпил (кулдаун 5 минут)\n"
        "<b>топ алко</b> – показать топ-20 алкоголиков\n"
        "<b>казино N</b> – поставить N бутылок, шанс выиграть литры\n"
        "<b>Б N</b> (реплаем) – передать N бутылок другому игроку\n"
        "<b>бот</b> – Алкоголь тут🍺\n"
        "<b>помощь</b> – эта справка\n"
        "/start, /help – тоже работают"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def alco_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_message_too_old(update):
        logging.info(f"Ignored old message from {update.effective_user.id}")
        return

    user = update.effective_user
    user_id = user.id
    username = format_username(user)

    db_user = get_user(user_id)
    if db_user:
        can, remaining = can_drink(db_user["last_drink_time"])
        if not can:
            minutes = remaining // 60
            seconds = remaining % 60
            await update.message.reply_text(
                f"⏳ {username}, ты уже выпил! Подожди ещё {minutes} мин {seconds} сек."
            )
            return
        drink_count = db_user["drink_count"]
    else:
        drink_count = 0

    added = calculate_added_volume(drink_count)
    bottle_gained = random.random() < BOTTLE_DROP_CHANCE

    total, bottles = update_user_alco(
        user_id,
        username.lstrip('@') if user.username else username,
        added,
        bottle_gained
    )

    response = (
        f"{username}, ты выпил(а) {added:.2f} л. алкоголя 🍺.\n"
        f"Выпито всего – {total:.2f} л."
    )
    if bottle_gained:
        response += (
            f"\nТакже выбита одна бутылка алкоголя 🍾\n"
            f"Используй команду \"казино N\" чтобы сыграть."
        )
    await update.message.reply_text(response)

async def top_alco_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_message_too_old(update):
        return

    top = get_top_users(20)
    if not top:
        await update.message.reply_text("🍺 Пока никто не пил. Будь первым!")
        return

    lines = ["🍺 Топ 20 алкоголиков:"]
    for row in top:
        name = row["username"] if row["username"] else "аноним"
        lines.append(f"@{name} выпито {row['total_volume']:.2f} л")
    await update.message.reply_text("\n".join(lines))

async def bot_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Алкоголь тут🍺")

async def casino_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_message_too_old(update):
        return

    user = update.effective_user
    user_id = user.id
    username = format_username(user)

    text = update.message.text.strip()
    match = re.match(r'^(?i)казино\s+(\d+)$', text)
    if not match:
        await update.message.reply_text("Укажи сколько бутылок поставить, например: казино 2")
        return

    bet = int(match.group(1))
    if bet <= 0:
        await update.message.reply_text("Количество бутылок должно быть больше нуля.")
        return

    db_user = get_user(user_id)
    if not db_user:
        await update.message.reply_text("У тебя нет бутылок. Сначала используй команду \"алко\" и попробуй выбить бутылку.")
        return

    if db_user["bottles"] < bet:
        await update.message.reply_text(f"У тебя только {db_user['bottles']} бутылок 🍾, не хватает.")
        return

    drink_count = db_user["drink_count"]
    won_count = 0
    liters_won = 0.0

    for _ in range(bet):
        if random.random() < CASINO_WIN_CHANCE:
            won_count += 1
            liters_won += calculate_casino_win(drink_count)

    new_total, new_bottles = update_user_casino(user_id, username, bet, won_count, liters_won)

    if won_count == 0:
        response = (
            f"🙅‍♂️ {username}, тебе не повезло — ты профукал все бутылки. Может, в следующий раз повезёт?\n"
            f"Баланс литров: {new_total:.2f} л\n"
            f"Бутылок: {new_bottles} 🍾"
        )
    else:
        response = (
            f"🪙 {username}, ты выиграл! 🏆\n"
            f"Поставлено бутылок: {bet} 🍾\n"
        )
        if won_count < bet:
            response += f"Из них сыграло: {won_count}\n"
        response += (
            f"Получено литров: {liters_won:.2f} л\n"
            f"Баланс литров: {new_total:.2f} л\n"
            f"Осталось бутылок: {new_bottles} 🍾"
        )

    await update.message.reply_text(response)

async def transfer_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_message_too_old(update):
        return

    if not update.message.reply_to_message:
        await update.message.reply_text("Эту команду нужно отправлять ответом на сообщение получателя.")
        return

    sender = update.effective_user
    sender_id = sender.id
    sender_name = format_username(sender)

    receiver = update.message.reply_to_message.from_user
    receiver_id = receiver.id
    receiver_name = format_username(receiver)

    if sender_id == receiver_id:
        await update.message.reply_text("Нельзя передать бутылки самому себе.")
        return

    text = update.message.text.strip()
    match = re.match(r'^(?i)Б\s+(\d+)$', text)
    if not match:
        await update.message.reply_text("Формат: Б <количество> (например, Б 2)")
        return

    amount = int(match.group(1))
    if amount <= 0:
        await update.message.reply_text("Количество должно быть больше нуля.")
        return

    success, error_msg = transfer_bottles(sender_id, receiver_id, amount)
    if not success:
        await update.message.reply_text(error_msg)
        return

    await update.message.reply_text(
        f"🍺 {sender_name} передал {receiver_name} {amount} бутылок 🍾"
    )

async def secret_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_message_too_old(update):
        return

    user = update.effective_user
    user_id = user.id
    username = format_username(user)

    response, given = update_secret_attempt(user_id, username.lstrip('@') if user.username else username)
    await update.message.reply_text(response)

# ================== ФИЛЬТРЫ ==================
ALCO_FILTER = filters.Regex(r'(?i)^алко$')
TOP_FILTER = filters.Regex(r'(?i)^топ алко$')
BOT_FILTER = filters.Regex(r'(?i)^бот$')
HELP_FILTER = filters.Regex(r'(?i)^помощь$')
CASINO_FILTER = filters.Regex(r'(?i)^казино\s+\d+$')
TRANSFER_FILTER = filters.Regex(r'(?i)^Б\s+\d+$') & filters.REPLY
SECRET_FILTER = filters.Regex(r'(?i)^Бот пожалуйста дай бутылку$')

# ================== ЗАПУСК ==================
def main():
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )
    init_db()

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))

    app.add_handler(MessageHandler(ALCO_FILTER, alco_command))
    app.add_handler(MessageHandler(TOP_FILTER, top_alco_command))
    app.add_handler(MessageHandler(BOT_FILTER, bot_response))
    app.add_handler(MessageHandler(HELP_FILTER, help_cmd))
    app.add_handler(MessageHandler(CASINO_FILTER, casino_command))
    app.add_handler(MessageHandler(TRANSFER_FILTER, transfer_command))
    app.add_handler(MessageHandler(SECRET_FILTER, secret_command))

    print("Бот запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()
