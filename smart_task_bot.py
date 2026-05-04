import asyncio
import logging
import re
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

import dateparser
import sqlite3

# ========== НАСТРОЙКИ ==========
TOKEN = "8747838026:AAG3hBpJLiiIMNRBSnaWo05pfb15aNZ8jlY"  # замените на ваш реальный токен

bot = Bot(token=TOKEN)
dp = Dispatcher()

# ========== БАЗА ДАННЫХ ==========
def init_db():
    conn = sqlite3.connect("tasks.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            text TEXT,
            deadline TIMESTAMP,
            done BOOLEAN DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def add_task(user_id, text, deadline=None):
    conn = sqlite3.connect("tasks.db")
    c = conn.cursor()
    c.execute("INSERT INTO tasks (user_id, text, deadline) VALUES (?, ?, ?)",
              (user_id, text, deadline))
    conn.commit()
    task_id = c.lastrowid
    conn.close()
    return task_id

def get_active_tasks(user_id):
    conn = sqlite3.connect("tasks.db")
    c = conn.cursor()
    c.execute("SELECT id, text, deadline FROM tasks WHERE user_id=? AND done=0 ORDER BY deadline ASC NULLS LAST, created_at DESC", (user_id,))
    tasks = c.fetchall()
    conn.close()
    return tasks

def get_completed_tasks(user_id):
    conn = sqlite3.connect("tasks.db")
    c = conn.cursor()
    c.execute("SELECT id, text, deadline FROM tasks WHERE user_id=? AND done=1 ORDER BY created_at DESC LIMIT 10", (user_id,))
    tasks = c.fetchall()
    conn.close()
    return tasks

def mark_done(task_id, user_id):
    conn = sqlite3.connect("tasks.db")
    c = conn.cursor()
    c.execute("UPDATE tasks SET done=1 WHERE id=? AND user_id=?", (task_id, user_id))
    conn.commit()
    conn.close()

def delete_task(task_id, user_id):
    conn = sqlite3.connect("tasks.db")
    c = conn.cursor()
    c.execute("DELETE FROM tasks WHERE id=? AND user_id=?", (task_id, user_id))
    conn.commit()
    conn.close()

def get_task_text(task_id, user_id):
    conn = sqlite3.connect("tasks.db")
    c = conn.cursor()
    c.execute("SELECT text FROM tasks WHERE id=? AND user_id=?", (task_id, user_id))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

# ========== ПЛАНИРОВЩИК НАПОМИНАНИЙ ==========
scheduler = AsyncIOScheduler()

async def send_reminder(user_id, task_text):
    await bot.send_message(user_id, f"🔔 НАПОМИНАНИЕ!\nЗадача «{task_text}» должна быть выполнена сейчас или уже просрочена!")

def schedule_reminder(user_id, task_id, task_text, deadline_dt):
    remind_before = deadline_dt - timedelta(minutes=5)
    if remind_before > datetime.now():
        scheduler.add_job(send_reminder, DateTrigger(run_date=remind_before),
                          args=[user_id, task_text], id=f"remind_{task_id}_before")
    if deadline_dt > datetime.now():
        scheduler.add_job(send_reminder, DateTrigger(run_date=deadline_dt),
                          args=[user_id, task_text], id=f"remind_{task_id}_exact")

# ========== ИСПРАВЛЕННЫЙ ПАРСИНГ ДАТЫ ==========
def parse_deadline(text):
    """
    Распознаёт фразы:
    - сделать что-то в 13:00 (сегодня)
    - сделать что-то сегодня в 13:00
    - сделать что-то завтра в 13:00
    - сделать что-то послезавтра
    - через 2 часа
    - через 30 минут
    """
    now = datetime.now()
    text_lower = text.lower()
    
    # 1. "через X часов/минут"
    match = re.search(r'через\s+(\d+)\s*(час|часов|минут|минуты|мин)', text_lower)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        if 'час' in unit:
            delta = timedelta(hours=amount)
        else:
            delta = timedelta(minutes=amount)
        deadline = now + delta
        cleaned = re.sub(r'через\s+\d+\s*(час|часов|минут|минуты|мин)', '', text, flags=re.IGNORECASE).strip()
        if not cleaned:
            cleaned = "Задача"
        return cleaned, deadline

    # 2. Определяем день (сегодня, завтра, послезавтра)
    day_delta = 0
    if 'послезавтра' in text_lower:
        day_delta = 2
        keyword = 'послезавтра'
    elif 'завтра' in text_lower:
        day_delta = 1
        keyword = 'завтра'
    elif 'сегодня' in text_lower:
        day_delta = 0
        keyword = 'сегодня'
    else:
        # Если нет слов "сегодня/завтра/послезавтра", пробуем найти время без даты
        # и считаем, что это сегодня
        day_delta = 0
        keyword = None

    # Дата без времени
    base_date = now + timedelta(days=day_delta)

    # Удаляем ключевое слово из текста
    if keyword:
        cleaned = re.sub(r'\b' + keyword + r'\b', '', text, flags=re.IGNORECASE).strip()
    else:
        cleaned = text

    # 3. Ищем время в формате "в 13:00" или просто "13:00"
    time_match = re.search(r'\b(\d{1,2}):(\d{2})\b', text)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
        deadline = base_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
        # Удаляем найденное время из текста
        cleaned = re.sub(r'\d{1,2}:\d{2}', '', cleaned).strip()
    else:
        # Если время не указано, ставим 23:59
        deadline = base_date.replace(hour=23, minute=59, second=0, microsecond=0)

    # Если дедлайн уже прошёл (например, "сегодня в 5 утра" а сейчас 14:00), переносим на завтра
    if deadline < now:
        deadline += timedelta(days=1)

    if not cleaned:
        cleaned = "Задача"
    return cleaned, deadline

# ========== FSM ДЛЯ ДОБАВЛЕНИЯ ЗАДАЧ ==========
class AddTask(StatesGroup):
    waiting_text = State()
    waiting_deadline = State()

# ========== КЛАВИАТУРЫ ==========
def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить задачу", callback_data="add")],
        [InlineKeyboardButton(text="📋 Мои задачи", callback_data="list")],
        [InlineKeyboardButton(text="✅ Выполненные", callback_data="completed")],
        [InlineKeyboardButton(text="🗑 Удалить задачу", callback_data="delete_menu")]
    ])

def task_actions_kb(task_id):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Выполнить", callback_data=f"done_{task_id}")
    builder.button(text="❌ Удалить", callback_data=f"remove_{task_id}")
    builder.adjust(2)
    return builder.as_markup()

def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В главное меню", callback_data="main")]])

# ========== ОБРАБОТЧИКИ ==========
@dp.message(Command("start"))
async def start(msg: types.Message):
    await msg.answer(
        "🌟 Привет! Я умный менеджер задач.\n\n"
        "Просто напиши задачу с датой и временем, например:\n"
        "• выпить таблетку в 13:00\n"
        "• купить хлеб завтра в 19:00\n"
        "• сделать отчёт через 2 часа\n\n"
        "Если не указать время – поставлю 23:59.\n"
        "Также можно использовать кнопки.",
        reply_markup=main_kb()
    )

@dp.callback_query(lambda c: c.data == "main")
async def back_main(call: types.CallbackQuery):
    await call.message.edit_text("Главное меню:", reply_markup=main_kb())
    await call.answer()

@dp.message(lambda msg: msg.text and not msg.text.startswith('/'))
async def smart_add(msg: types.Message, state: FSMContext):
    text = msg.text
    cleaned, deadline = parse_deadline(text)
    if deadline:
        task_id = add_task(msg.from_user.id, cleaned, deadline)
        schedule_reminder(msg.from_user.id, task_id, cleaned, deadline)
        await msg.answer(
            f"✅ Задача добавлена!\n"
            f"📝 {cleaned}\n"
            f"⏰ {deadline.strftime('%d.%m.%Y %H:%M')}\n"
            f"🔔 Напомню за 5 минут."
        )
    else:
        await msg.answer("❌ Не распознал дату. Попробуйте: 'завтра в 15:00', 'через 2 часа' или 'в 13:00'.\nИли нажмите кнопку '➕ Добавить задачу' для ручного ввода.")
        await state.set_state(AddTask.waiting_text)
        await state.update_data(task_text=text)

@dp.callback_query(lambda c: c.data == "add")
async def manual_add_start(call: types.CallbackQuery, state: FSMContext):
    await call.message.edit_text("Напишите текст задачи (без даты):")
    await state.set_state(AddTask.waiting_text)
    await call.answer()

@dp.message(AddTask.waiting_text)
async def manual_text(msg: types.Message, state: FSMContext):
    await state.update_data(task_text=msg.text)
    await msg.answer("Теперь укажите дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ\nНапример: 31.12.2025 18:00")
    await state.set_state(AddTask.waiting_deadline)

@dp.message(AddTask.waiting_deadline)
async def manual_deadline(msg: types.Message, state: FSMContext):
    if msg.text.lower() == "/cancel":
        await state.clear()
        await msg.answer("Отменено.", reply_markup=main_kb())
        return
    try:
        deadline = datetime.strptime(msg.text, "%d.%m.%Y %H:%M")
        if deadline < datetime.now():
            await msg.answer("Дедлайн не может быть в прошлом. Введите будущую дату.")
            return
        data = await state.get_data()
        task_text = data["task_text"]
        user_id = msg.from_user.id
        task_id = add_task(user_id, task_text, deadline)
        schedule_reminder(user_id, task_id, task_text, deadline)
        await msg.answer(f"✅ Задача добавлена!\n📝 {task_text}\n⏰ {deadline.strftime('%d.%m.%Y %H:%M')}")
        await state.clear()
        await msg.answer("Главное меню:", reply_markup=main_kb())
    except ValueError:
        await msg.answer("Неверный формат. Попробуйте ДД.ММ.ГГГГ ЧЧ:ММ (например, 31.12.2025 18:00)")

@dp.callback_query(lambda c: c.data == "list")
async def list_active(call: types.CallbackQuery):
    tasks = get_active_tasks(call.from_user.id)
    if not tasks:
        await call.message.edit_text("📭 Нет активных задач.", reply_markup=back_kb())
        await call.answer()
        return
    await call.message.delete()
    for tid, text, deadline in tasks:
        deadline_str = ""
        if deadline:
            dt = datetime.fromisoformat(deadline)
            deadline_str = f"\n⏰ {dt.strftime('%d.%m.%Y %H:%M')}"
        await call.message.answer(f"📌 {text}{deadline_str}", reply_markup=task_actions_kb(tid))
    await call.message.answer("🔙 В меню", reply_markup=back_kb())
    await call.answer()

@dp.callback_query(lambda c: c.data == "completed")
async def list_completed(call: types.CallbackQuery):
    tasks = get_completed_tasks(call.from_user.id)
    if not tasks:
        await call.message.edit_text("📭 Нет выполненных задач.", reply_markup=back_kb())
        await call.answer()
        return
    text = "✅ *Выполненные задачи:*\n\n"
    for tid, ttext, deadline in tasks:
        text += f"• {ttext}\n"
    await call.message.edit_text(text, parse_mode="Markdown", reply_markup=back_kb())
    await call.answer()

@dp.callback_query(lambda c: c.data.startswith("done_"))
async def complete_task(call: types.CallbackQuery):
    task_id = int(call.data.split("_")[1])
    mark_done(task_id, call.from_user.id)
    await call.answer("✅ Задача выполнена!")
    await call.message.delete()

@dp.callback_query(lambda c: c.data.startswith("remove_"))
async def remove_task(call: types.CallbackQuery):
    task_id = int(call.data.split("_")[1])
    delete_task(task_id, call.from_user.id)
    await call.answer("🗑 Задача удалена")
    await call.message.delete()

@dp.callback_query(lambda c: c.data == "delete_menu")
async def delete_menu(call: types.CallbackQuery):
    tasks = get_active_tasks(call.from_user.id)
    if not tasks:
        await call.answer("Нет задач для удаления", show_alert=True)
        return
    builder = InlineKeyboardBuilder()
    for tid, text, deadline in tasks:
        builder.button(text=f"❌ {text[:30]}", callback_data=f"remove_{tid}")
    builder.adjust(1)
    builder.button(text="🔙 Назад", callback_data="main")
    await call.message.edit_text("Выберите задачу для удаления:", reply_markup=builder.as_markup())
    await call.answer()

# ========== ЗАПУСК ==========
async def on_startup():
    init_db()
    scheduler.start()
    # Восстановить напоминания после перезапуска
    conn = sqlite3.connect("tasks.db")
    c = conn.cursor()
    c.execute("SELECT id, user_id, text, deadline FROM tasks WHERE done=0 AND deadline IS NOT NULL AND deadline > datetime('now')")
    rows = c.fetchall()
    for tid, uid, txt, deadline_str in rows:
        dt = datetime.fromisoformat(deadline_str)
        schedule_reminder(uid, tid, txt, dt)
    conn.close()
    logging.basicConfig(level=logging.INFO)
    print("Бот запущен")

async def main():
    await on_startup()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())