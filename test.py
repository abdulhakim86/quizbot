import asyncio
import logging
import json
import time
import csv
import io
import re
from datetime import datetime, timedelta

import aiosqlite
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramAPIError
from aiogram.types import FSInputFile, BufferedInputFile

# ==========================================
# 1. НАСТРОЙКИ БОТА И БАЗЫ ДАННЫХ
# ==========================================
BOT_TOKEN = "8268765014:AAFCbLjxw0vMAqOJKLOcWiEIWfNHX9OxcVM"
ADMIN_ID = 770794055 # ❗️ ВСТАВЬ СЮДА СВОЙ TELEGRAM ID ❗️

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

DB_NAME = "medisphere.db"

# Структура курсов и предметов (Ключи без нижних подчеркиваний для защиты от багов)
COURSES = {
    "c1": {
        "name": "1 курс",
        "subs": {"anat": "Нормальная анатомия", "histgen": "Общая гистология"}
    },
    "c2": {
        "name": "2 курс",
        "subs": {"anat": "Нормальная анатомия", "phys": "Нормальная физиология", "bio": "Биохимия", "histspec": "Частная гистология", "micro": "Микробиология"}
    },
    "c3": {
        "name": "3 курс",
        "subs": {"pat": "Патология", "farm": "Фармакология", "prop": "Пропедевтика", "ohita": "ОХиТА"}
    }
}

# ==========================================
# 2. СОСТОЯНИЯ FSM
# ==========================================
class UserState(StatesGroup):
    waiting_for_support_msg = State()

class RegState(StatesGroup):
    waiting_for_name = State()

class EditProfileState(StatesGroup):
    waiting_for_name = State()

class AdminState(StatesGroup):
    # Создание теста
    waiting_for_course = State()
    waiting_for_subject = State()
    waiting_for_q_text = State()
    waiting_for_q_opts = State()
    waiting_for_q_correct = State()
    waiting_for_q_expl = State()
    waiting_for_test_title = State()
    
    # Управление
    waiting_for_broadcast = State()
    waiting_for_q_del_id = State()
    waiting_for_db_file = State()
    waiting_for_support_reply = State()
    
    # Редактирование теста
    waiting_for_edit_test_id = State()
    waiting_for_new_test_title = State()
    waiting_for_del_question_id = State()
    
    # Умное редактирование вопроса
    waiting_for_edit_q_id = State()
    waiting_for_edit_q_text_photo = State()
    waiting_for_edit_q_opts = State()
    waiting_for_edit_q_correct = State()
    waiting_for_edit_q_expl = State()
    
class QuizState(StatesGroup):
    active_test = State()

# ==========================================
# 3. БАЗА ДАННЫХ И МИГРАЦИИ
# ==========================================
def get_last_saturday_timestamp():
    now = datetime.now()
    days_since_saturday = (now.weekday() - 5) % 7
    last_saturday = now - timedelta(days=days_since_saturday)
    last_saturday = last_saturday.replace(hour=0, minute=0, second=0, microsecond=0)
    return last_saturday.timestamp()

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                score INTEGER DEFAULT 0,
                answered_questions TEXT DEFAULT '[]'
            )
        ''')
        try: await db.execute("ALTER TABLE users ADD COLUMN weekly_score INTEGER DEFAULT 0")
        except: pass
        try: await db.execute("ALTER TABLE users ADD COLUMN last_active_timestamp REAL DEFAULT 0")
        except: pass
        try: await db.execute("ALTER TABLE users ADD COLUMN question_start_time REAL DEFAULT 0")
        except: pass
        try: await db.execute("ALTER TABLE users ADD COLUMN last_message_id INTEGER DEFAULT 0")
        except: pass
        try: await db.execute("ALTER TABLE users ADD COLUMN total_response_time REAL DEFAULT 0")
        except: pass
        try: await db.execute("ALTER TABLE users ADD COLUMN is_registered INTEGER DEFAULT 0")
        except: pass
        try: await db.execute("ALTER TABLE users ADD COLUMN course TEXT DEFAULT NULL")
        except: pass
        try: await db.execute("ALTER TABLE users ADD COLUMN completed_tests TEXT DEFAULT '[]'")
        except: pass

        await db.execute('''
            CREATE TABLE IF NOT EXISTS tests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                course TEXT,
                subject TEXT,
                is_published INTEGER DEFAULT 0
            )
        ''')

        await db.execute('''
            CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT,
                options TEXT,
                correct_index INTEGER,
                explanation TEXT
            )
        ''')
        try: await db.execute("ALTER TABLE questions ADD COLUMN is_published INTEGER DEFAULT 1")
        except: pass
        try: await db.execute("ALTER TABLE questions ADD COLUMN photo_id TEXT DEFAULT NULL")
        except: pass
        try: await db.execute("ALTER TABLE questions ADD COLUMN test_id INTEGER DEFAULT 0")
        except: pass
        await db.commit()

def get_rank(score):
    if score < 500: return "Студент 📚"
    elif score < 1000: return "Интерн 🩺"
    elif score < 1500: return "Магистр 🎓"
    else: return "Главврач МедиСферы 🏆"

def parse_json_list(val):
    if not val: return []
    try: return json.loads(val)
    except json.JSONDecodeError: return []

# ==========================================
# 4. ДВИЖОК «ЕДИНОГО ОКНА»
# ==========================================
async def safe_update_ui(user_id: int, text: str, reply_markup=None, photo_id=None):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT last_message_id FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        last_msg_id = row[0] if row else 0

    new_msg = None
    if photo_id and len(text) > 1000:
        text = text[:950] + "...\n\n<i>(Текст сокращен из-за лимита Telegram)</i>"

    try:
        if last_msg_id:
            if photo_id:
                try: await bot.delete_message(chat_id=user_id, message_id=last_msg_id)
                except: pass
                new_msg = await bot.send_photo(chat_id=user_id, photo=photo_id, caption=text, reply_markup=reply_markup)
            else:
                try:
                    new_msg = await bot.edit_message_text(text=text, chat_id=user_id, message_id=last_msg_id, reply_markup=reply_markup)
                except TelegramAPIError:
                    try: await bot.delete_message(chat_id=user_id, message_id=last_msg_id)
                    except: pass
                    new_msg = await bot.send_message(chat_id=user_id, text=text, reply_markup=reply_markup)
        else:
            if photo_id:
                new_msg = await bot.send_photo(chat_id=user_id, photo=photo_id, caption=text, reply_markup=reply_markup)
            else:
                new_msg = await bot.send_message(chat_id=user_id, text=text, reply_markup=reply_markup)

        if new_msg:
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute("UPDATE users SET last_message_id = ? WHERE user_id = ?", (new_msg.message_id, user_id))
                await db.commit()
    except Exception as e:
        logging.error(f"UI update error: {e}")

async def init_user(user_id: int, username: str):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT score FROM users WHERE user_id = ?", (user_id,))
        if not await cursor.fetchone():
            await db.execute("INSERT INTO users (user_id, username, answered_questions, completed_tests) VALUES (?, ?, '[]', '[]')", (user_id, username))
            await db.commit()

async def check_is_registered(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT is_registered FROM users WHERE user_id = ? AND is_registered = 1", (user_id,))
        return await cursor.fetchone() is not None

# ==========================================
# 5. ГЛАВНОЕ МЕНЮ И РЕГИСТРАЦИЯ
# ==========================================
@dp.message(Command("start", "profile", "top", "info", "quiz"))
async def handle_user_commands(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.first_name
    if user_id != ADMIN_ID:
        try: await message.delete()
        except: pass

    await init_user(user_id, username)
    is_reg = await check_is_registered(user_id)
    if not is_reg:
        await render_registration_start(user_id)
        return

    cmd = message.text.split()[0].lower()
    await state.clear()

    if cmd == "/start": await render_main_menu(user_id)
    elif cmd == "/profile": await render_profile(user_id)
    elif cmd == "/top": await render_top(user_id)
    elif cmd == "/info": await render_info(user_id)
    elif cmd == "/quiz": await nav_courses_msg(user_id)

async def render_registration_start(user_id: int):
    text = "👋 Добро пожаловать в бот <b>МедиСферы</b>!\n\nДля дальнейшего пользования ботом, пожалуйста, пройдите короткую регистрацию."
    builder = InlineKeyboardBuilder()
    builder.button(text="📝 Пройти регистрацию", callback_data="start_reg")
    await safe_update_ui(user_id, text, builder.as_markup())

@dp.callback_query(F.data == "start_reg")
async def start_reg_flow(callback: types.CallbackQuery, state: FSMContext):
    text = "Отлично! Введите ваше имя (или никнейм).\n\n<i>*Пояснение: ваше имя будет отображаться онлайн в лидерборде.</i>"
    await safe_update_ui(callback.from_user.id, text)
    await state.set_state(RegState.waiting_for_name)
    await callback.answer()

@dp.message(RegState.waiting_for_name)
async def process_reg_name(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    try: await message.delete()
    except: pass
    
    name = message.text.strip()
    if not re.match(r'^[A-Za-zА-Яа-яЁё0-9\s\-_]+$', name):
        text = "❌ <b>Недопустимые символы.</b>\nПожалуйста, используйте только обычные буквы, цифры и пробелы.\n\nВведите ваше имя:"
        await safe_update_ui(user_id, text)
        return
        
    await state.update_data(reg_name=name)
    text = "Здорово! Теперь укажите, на каком курсе вы учитесь:"
    builder = InlineKeyboardBuilder()
    builder.button(text="1 курс", callback_data="reg_course_1")
    builder.button(text="2 курс", callback_data="reg_course_2")
    builder.button(text="3 курс", callback_data="reg_course_3")
    builder.button(text="4 и +", callback_data="reg_course_4+")
    builder.adjust(2, 2)
    await safe_update_ui(user_id, text, builder.as_markup())

@dp.callback_query(F.data.startswith("reg_course_"))
async def process_reg_course(callback: types.CallbackQuery, state: FSMContext):
    course = callback.data.split("_")[2]
    data = await state.get_data()
    name = data.get('reg_name')
    user_id = callback.from_user.id
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET username = ?, course = ?, is_registered = 1 WHERE user_id = ?", (name, course, user_id))
        await db.commit()
        
    await state.clear()
    await render_main_menu(user_id, prefix="✅ <b>Супер! Вы успешно прошли регистрацию.</b>\n\n")
    await callback.answer()

# ==========================================
# 6. НАВИГАЦИЯ И ВЫБОР КВИЗА
# ==========================================
async def render_main_menu(user_id: int, prefix: str = ""):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT username FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        username = row[0] if row else "Врач"
        
    text = f"{prefix}Привет, <b>{username}</b>! Добро пожаловать в бот <b>МедиСферы</b>. 🧬\n\nВыбери нужный раздел:"
    builder = InlineKeyboardBuilder()
    builder.button(text="🧠 Выбрать квиз", callback_data="menu_courses")
    builder.button(text="👤 Мой профиль", callback_data="menu_profile")
    builder.button(text="🏆 Лидерборд", callback_data="menu_top")
    builder.button(text="ℹ️ Информация", callback_data="menu_info")
    builder.button(text="📩 Связь с админом", callback_data="menu_support")
    builder.adjust(1, 2, 2)
    await safe_update_ui(user_id, text, builder.as_markup())

@dp.callback_query(F.data == "menu_main")
async def nav_main_menu(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await render_main_menu(callback.from_user.id)
    await callback.answer()

async def nav_courses_msg(user_id: int):
    text = "📚 <b>Выберите нужный вам курс:</b>👇"
    builder = InlineKeyboardBuilder()
    for c_id, c_data in COURSES.items():
        builder.button(text=f"Тесты для {c_data['name'].lower()[:-1]}а", callback_data=f"sel_course_{c_id}")
    builder.button(text="🔙 В главное меню", callback_data="menu_main")
    builder.adjust(1)
    await safe_update_ui(user_id, text, builder.as_markup())

@dp.callback_query(F.data == "menu_courses")
async def nav_courses_cb(callback: types.CallbackQuery):
    await nav_courses_msg(callback.from_user.id)
    await callback.answer()

@dp.callback_query(F.data.startswith("sel_course_"))
async def nav_subjects(callback: types.CallbackQuery):
    c_id = callback.data.split("_")[2]
    c_data = COURSES.get(c_id)
    if not c_data: return
    
    text = f"🧬 <b>{c_data['name']}: Выберите предмет</b>👇"
    builder = InlineKeyboardBuilder()
    for s_id, s_name in c_data['subs'].items():
        builder.button(text=s_name, callback_data=f"sel_subj_{c_id}_{s_id}")
    builder.button(text="🔙 К выбору курса", callback_data="menu_courses")
    builder.adjust(1)
    await safe_update_ui(callback.from_user.id, text, builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("sel_subj_"))
async def nav_tests(callback: types.CallbackQuery):
    _, _, c_id, s_id = callback.data.split("_")
    user_id = callback.from_user.id
    subj_name = COURSES[c_id]['subs'][s_id]
    
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT completed_tests FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        completed = parse_json_list(row[0]) if row else []
        
        cursor = await db.execute("SELECT id, title FROM tests WHERE course = ? AND subject = ? AND is_published = 1", (c_id, s_id))
        tests = await cursor.fetchall()
        
    text = f"📝 <b>Предмет: {subj_name}</b>\nВыберите тест для прохождения:\n<i>(✅ - уже пройдено, баллы не начисляются)</i>"
    builder = InlineKeyboardBuilder()
    
    if not tests:
        text = f"📝 <b>Предмет: {subj_name}</b>\n\nПока здесь нет доступных тестов. Админ скоро их добавит!"
    else:
        for t_id, t_title in tests:
            mark = "✅ " if t_id in completed else "🎯 "
            builder.button(text=f"{mark}{t_title}", callback_data=f"start_test_{t_id}")
            
    builder.button(text="🔙 К предметам", callback_data=f"sel_course_{c_id}")
    builder.adjust(1)
    await safe_update_ui(user_id, text, builder.as_markup())
    await callback.answer()

# ==========================================
# 7. ДВИЖОК ТЕСТИРОВАНИЯ (СЕССИИ)
# ==========================================
@dp.callback_query(F.data.startswith("start_test_"))
async def start_test(callback: types.CallbackQuery, state: FSMContext):
    t_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id
    
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT completed_tests FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        completed = parse_json_list(row[0]) if row else []
        
        cursor = await db.execute("SELECT id, text, options, correct_index, explanation, photo_id FROM questions WHERE test_id = ?", (t_id,))
        questions = await cursor.fetchall()
        
        cursor = await db.execute("SELECT title FROM tests WHERE id = ?", (t_id,))
        t_row = await cursor.fetchone()
        test_title = t_row[0] if t_row else "Тест"
        
    if not questions:
        await callback.answer("Этот тест пока пуст!", show_alert=True)
        return
        
    is_retake = t_id in completed
    
    await state.set_state(QuizState.active_test)
    await state.update_data(
        test_id=t_id,
        test_title=test_title,
        questions=questions,
        current_idx=0,
        is_retake=is_retake,
        correct_count=0
    )
    
    if is_retake:
        await callback.answer("Режим тренировки: баллы за этот тест не начисляются.", show_alert=True)
        
    await send_quiz_question(user_id, state)
    await callback.answer()

async def send_quiz_question(user_id: int, state: FSMContext):
    data = await state.get_data()
    questions = data.get('questions', [])
    current_idx = data.get('current_idx', 0)
    
    if current_idx >= len(questions):
        await finish_test(user_id, state)
        return
        
    q = questions[current_idx]
    q_id, text, opts_json, _, _, photo_id = q
    options = json.loads(opts_json)
    
    q_num = current_idx + 1
    total_q = len(questions)
    
    text_ui = f"📝 <b>{data['test_title']}</b> (Вопрос {q_num}/{total_q})\n\n🧩 {text}\n\n⏱ <i>У вас есть 20 секунд!</i>"
    if data['is_retake']:
         text_ui += "\n🔄 <i>Режим тренировки</i>"
         
    builder = InlineKeyboardBuilder()
    for idx, option in enumerate(options):
        builder.button(text=option, callback_data=f"qans_{idx}")
    builder.button(text="❌ Прервать тест", callback_data="menu_courses")
    builder.adjust(1)
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET question_start_time = ? WHERE user_id = ?", (time.time(), user_id))
        await db.commit()

    await safe_update_ui(user_id, text_ui, builder.as_markup(), photo_id)

@dp.callback_query(QuizState.active_test, F.data.startswith("qans_"))
async def process_quiz_answer(callback: types.CallbackQuery, state: FSMContext):
    selected_idx = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    
    data = await state.get_data()
    questions = data['questions']
    current_idx = data['current_idx']
    is_retake = data['is_retake']
    
    q = questions[current_idx]
    _, text, opts_json, correct_idx, expl, photo_id = q
    options = json.loads(opts_json)
    
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT question_start_time, total_response_time, answered_questions FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        q_start_time, total_time, answered_json = row[0], row[1], row[2]
        answered = parse_json_list(answered_json)
        
        time_taken = time.time() - q_start_time
        is_timeout = time_taken > 20
        is_correct = (selected_idx == correct_idx) and not is_timeout
        
        if is_correct:
            await state.update_data(correct_count=data['correct_count'] + 1)
            
        score_add = 10 if (is_correct and not is_retake) else 0
        new_total_time = total_time + time_taken
        
        if not is_retake:
            current_ts, last_sat_ts = time.time(), get_last_saturday_timestamp()
            cursor = await db.execute("SELECT weekly_score, last_active_timestamp FROM users WHERE user_id = ?", (user_id,))
            u_data = await cursor.fetchone()
            current_weekly, last_active = u_data if u_data else (0, 0)
            new_weekly = score_add if last_active < last_sat_ts else current_weekly + score_add
            
            await db.execute(
                "UPDATE users SET score = score + ?, weekly_score = ?, last_active_timestamp = ?, total_response_time = ? WHERE user_id = ?", 
                (score_add, new_weekly, current_ts, new_total_time, user_id)
            )
            q_id = q[0]
            if q_id not in answered:
                answered.append(q_id)
                await db.execute("UPDATE users SET answered_questions = ? WHERE user_id = ?", (json.dumps(answered), user_id))
            await db.commit()

    result_text = f"🧩 <b>Вопрос:</b> {text}\n\n"
    if is_timeout:
        result_text += f"⏳ <b>Время вышло!</b> ({int(time_taken)} сек).\nВаш ответ: <i>{options[selected_idx]}</i>\nПравильный ответ: <i>{options[correct_idx]}</i>\n"
    else:
        result_text += f"Ваш ответ: <i>{options[selected_idx]}</i>\n"
        if is_correct:
            pts = "+10 баллов" if not is_retake else "Тренировка"
            result_text += f"⏱ Отвечено за {int(time_taken)} сек.\n\n✅ <b>Верно! ({pts})</b>\n"
        else:
            result_text += f"\n❌ <b>Ошибка!</b> Правильный ответ: <i>{options[correct_idx]}</i>\n"
        
    result_text += f"\n📖 <b>Разбор:</b> {expl}"
    
    await state.update_data(current_idx=current_idx + 1)
    
    builder = InlineKeyboardBuilder()
    builder.button(text="Дальше ➡️", callback_data="quiz_next")
    builder.adjust(1)
    
    await safe_update_ui(user_id, result_text, builder.as_markup(), photo_id)
    await callback.answer()

@dp.callback_query(QuizState.active_test, F.data == "quiz_next")
async def process_quiz_next(callback: types.CallbackQuery, state: FSMContext):
    await send_quiz_question(callback.from_user.id, state)
    await callback.answer()

async def finish_test(user_id: int, state: FSMContext):
    data = await state.get_data()
    t_id = data['test_id']
    total = len(data['questions'])
    correct = data['correct_count']
    is_retake = data['is_retake']
    
    text = f"🏁 <b>Тест «{data['test_title']}» завершен!</b>\n\n📊 Ваш результат: <b>{correct} из {total}</b> верных ответов."
    if not is_retake:
        pts = correct * 10
        text += f"\nЗаработано баллов: <b>+{pts}</b>"
        
        async with aiosqlite.connect(DB_NAME) as db:
            cursor = await db.execute("SELECT completed_tests FROM users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            completed = parse_json_list(row[0]) if row else []
            if t_id not in completed:
                completed.append(t_id)
                await db.execute("UPDATE users SET completed_tests = ? WHERE user_id = ?", (json.dumps(completed), user_id))
                await db.commit()
                
    await state.clear()
    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 К выбору тестов", callback_data="menu_courses")
    builder.button(text="🏠 Главное меню", callback_data="menu_main")
    builder.adjust(1)
    await safe_update_ui(user_id, text, builder.as_markup())

@dp.callback_query(F.data.startswith("qans_") | (F.data == "quiz_next"))
async def catch_lost_session(callback: types.CallbackQuery):
    await callback.answer("Сессия устарела. Пожалуйста, запустите тест заново.", show_alert=True)
    await render_main_menu(callback.from_user.id)

# ==========================================
# 8. ПРОФИЛЬ, ТОП И ИНФО
# ==========================================
@dp.callback_query(F.data == "menu_profile")
async def nav_profile(callback: types.CallbackQuery):
    await render_profile(callback.from_user.id)
    await callback.answer()

async def render_profile(user_id: int):
    last_sat_ts = get_last_saturday_timestamp()
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT username, score, weekly_score, last_active_timestamp, answered_questions, total_response_time, course FROM users WHERE user_id = ?", (user_id,))
        user = await cursor.fetchone()
        
        if user:
            username, score, weekly_score, last_active, answered_json, total_time, course = user
            if last_active < last_sat_ts: weekly_score = 0
            
            cursor = await db.execute("SELECT COUNT(*) FROM users WHERE score > ?", (score,))
            position = (await cursor.fetchone())[0] + 1
            cursor = await db.execute("SELECT COUNT(*) FROM users WHERE is_registered = 1")
            total_users = (await cursor.fetchone())[0]
            
            answered = len(parse_json_list(answered_json))
            avg_time = (total_time / answered) if answered > 0 else 0
            
            text = (
                f"👤 <b>Профиль: {username}</b>\n"
                f"🎓 Курс: <b>{course if course else 'Не указан'}</b>\n\n"
                f"🔹 Статус: {get_rank(score)}\n"
                f"🔹 Баллы (всё время): <b>{score}</b>\n"
                f"🔹 Баллы (неделя): <b>{weekly_score}</b>\n\n"
                f"📊 Ваше место: <b>{position} из {total_users}</b>\n"
                f"⏱ Ср. время ответа: <b>{avg_time:.2f} сек.</b>"
            )
        else:
            text = "Ошибка профиля."
        
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Изменить данные", callback_data="edit_profile_menu")
    builder.button(text="🔙 В главное меню", callback_data="menu_main")
    builder.adjust(1)
    await safe_update_ui(user_id, text, builder.as_markup())

@dp.callback_query(F.data == "edit_profile_menu")
async def edit_profile_menu(callback: types.CallbackQuery):
    text = "⚙️ <b>Что вы хотите изменить?</b>"
    builder = InlineKeyboardBuilder()
    builder.button(text="Имя (Никнейм)", callback_data="edit_name")
    builder.button(text="Курс", callback_data="edit_course")
    builder.button(text="🔙 Назад", callback_data="menu_profile")
    builder.adjust(2, 1)
    await safe_update_ui(callback.from_user.id, text, builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data == "edit_name")
async def edit_name_start(callback: types.CallbackQuery, state: FSMContext):
    text = "Введите новое имя (только буквы, цифры и пробелы):"
    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Отмена", callback_data="menu_profile")
    await safe_update_ui(callback.from_user.id, text, builder.as_markup())
    await state.set_state(EditProfileState.waiting_for_name)
    await callback.answer()

@dp.message(EditProfileState.waiting_for_name)
async def process_edit_name(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    try: await message.delete()
    except: pass
    name = message.text.strip()
    if not re.match(r'^[A-Za-zА-Яа-яЁё0-9\s\-_]+$', name):
        text = "❌ <b>Недопустимые символы.</b>\nВведите новое имя:"
        builder = InlineKeyboardBuilder()
        builder.button(text="🔙 Отмена", callback_data="menu_profile")
        await safe_update_ui(user_id, text, builder.as_markup())
        return
        
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET username = ? WHERE user_id = ?", (name, user_id))
        await db.commit()
    await state.clear()
    await render_profile(user_id)

@dp.callback_query(F.data == "edit_course")
async def edit_course_start(callback: types.CallbackQuery):
    text = "Укажите ваш текущий курс:"
    builder = InlineKeyboardBuilder()
    builder.button(text="1 курс", callback_data="save_course_1")
    builder.button(text="2 курс", callback_data="save_course_2")
    builder.button(text="3 курс", callback_data="save_course_3")
    builder.button(text="4 и +", callback_data="save_course_4+")
    builder.button(text="🔙 Отмена", callback_data="menu_profile")
    builder.adjust(2, 2, 1)
    await safe_update_ui(callback.from_user.id, text, builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("save_course_"))
async def process_save_course(callback: types.CallbackQuery):
    course = callback.data.split("_")[2]
    user_id = callback.from_user.id
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET course = ? WHERE user_id = ?", (course, user_id))
        await db.commit()
    await render_profile(user_id)
    await callback.answer()

@dp.callback_query(F.data == "menu_top")
async def nav_top(callback: types.CallbackQuery):
    await render_top(callback.from_user.id)
    await callback.answer()

async def render_top(user_id: int):
    last_sat_ts = get_last_saturday_timestamp()
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT username, score, answered_questions, total_response_time FROM users WHERE is_registered = 1 ORDER BY score DESC LIMIT 10")
        top_all = await cursor.fetchall()
        
        cursor = await db.execute("SELECT username, weekly_score, answered_questions, total_response_time FROM users WHERE last_active_timestamp >= ? AND weekly_score > 0 AND is_registered = 1 ORDER BY weekly_score DESC LIMIT 10", (last_sat_ts,))
        top_weekly = await cursor.fetchall()
        
    text = "🏆 <b>ТОП-10 Врачей (За всё время):</b>\n\n"
    for i, (username, score, ans_json, t_time) in enumerate(top_all, 1):
        ans_count = len(parse_json_list(ans_json))
        avg = (t_time / ans_count) if ans_count > 0 else 0
        medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else "👨‍⚕️"
        text += f"{medal} <b>{username}</b> — {score} баллов <i>(ср. {avg:.2f} сек)</i>\n"
        
    text += "\n🔥 <b>ТОП-10 За неделю:</b>\n\n"
    if top_weekly:
        for i, (username, score, ans_json, t_time) in enumerate(top_weekly, 1):
            ans_count = len(parse_json_list(ans_json))
            avg = (t_time / ans_count) if ans_count > 0 else 0
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else "🔥"
            text += f"{medal} <b>{username}</b> — {score} баллов <i>(ср. {avg:.2f} сек)</i>\n"
    else:
        text += "<i>На этой неделе рейтинг еще пуст.</i>\n"

    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 В главное меню", callback_data="menu_main")
    await safe_update_ui(user_id, text, builder.as_markup())

@dp.callback_query(F.data == "menu_info")
async def nav_info(callback: types.CallbackQuery):
    await render_info(callback.from_user.id)
    await callback.answer()

async def render_info(user_id: int):
    text = (
        "ℹ️ <b>О боте МедиСферы</b>\n\n"
        "Этот бот — мощный инструмент для проверки медицинских знаний.\n\n"
        "<b>Система баллов:</b>\n"
        "✅ Правильный ответ: +10 баллов\n"
        "⏱ Лимит времени на вопрос: 20 секунд. Не успели — ответ не засчитывается.\n"
        "🔄 Повторное прохождение тестов баллов не дает (режим тренировки).\n\n"
        "<b>Статусы:</b>\n"
        "📚 Студент — до 500 баллов\n"
        "🩺 Интерн — 500-1000 баллов\n"
        "🎓 Магистр — 1000-1500 баллов\n"
        "🏆 Главврач — от 1500 баллов\n\n"
        "Недельный рейтинг обнуляется каждую субботу в 00:00."
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 В главное меню", callback_data="menu_main")
    await safe_update_ui(user_id, text, builder.as_markup())

# ==========================================
# 9. СВЯЗЬ С АДМИНОМ (LIVEGRAM)
# ==========================================
@dp.callback_query(F.data == "menu_support")
async def handle_support_start(callback: types.CallbackQuery, state: FSMContext):
    text = "📩 <b>Связь с админом</b>\n\nНапишите ваше сообщение (вопрос, предложение) прямо в чат. Админ ответит вам здесь же."
    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Назад", callback_data="menu_main")
    await safe_update_ui(callback.from_user.id, text, builder.as_markup())
    await state.set_state(UserState.waiting_for_support_msg)
    await callback.answer()

@dp.message(UserState.waiting_for_support_msg)
async def process_support_msg(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    try: await message.delete()
    except: pass
    
    admin_text = f"📩 <b>Обращение от:</b>\nID: <code>{user_id}</code>\nИмя: {message.from_user.first_name}\n\n<b>Текст:</b>\n{message.text}"
    builder = InlineKeyboardBuilder()
    builder.button(text="✍️ Ответить", callback_data=f"reply_to_{user_id}")
    
    try:
        await bot.send_message(ADMIN_ID, admin_text, reply_markup=builder.as_markup())
        await safe_update_ui(user_id, "✅ <b>Сообщение отправлено!</b>\nОтвет придет в этот чат.", InlineKeyboardBuilder().button(text="🔙 В главное меню", callback_data="menu_main").as_markup())
    except Exception:
        await safe_update_ui(user_id, "❌ Произошла ошибка. Попробуйте позже.", InlineKeyboardBuilder().button(text="🔙 В главное меню", callback_data="menu_main").as_markup())
    await state.clear()

@dp.callback_query(F.data.startswith("reply_to_"))
async def admin_reply_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    user_id = callback.data.split("_")[2]
    await state.update_data(reply_to_user=user_id)
    await callback.message.answer(f"Напиши ответ для пользователя {user_id}:")
    await state.set_state(AdminState.waiting_for_support_reply)
    await callback.answer()

@dp.message(AdminState.waiting_for_support_reply)
async def admin_reply_process(message: types.Message, state: FSMContext):
    target_id = (await state.get_data()).get('reply_to_user')
    text_for_user = f"👨‍⚕️ <b>Ответ от Администратора:</b>\n\n{message.html_text}"
    try:
        await bot.send_message(target_id, text_for_user)
        await message.answer("✅ Ответ доставлен!")
    except Exception:
        await message.answer("❌ Ошибка доставки.")
    await state.clear()

# ==========================================
# 10. АДМИН-ПАНЕЛЬ
# ==========================================
def get_admin_main_kb():
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Создать тест", callback_data="admin_create_test")
    builder.button(text="📝 Черновики", callback_data="admin_drafts")
    builder.button(text="👀 Просмотр (опублик.)", callback_data="admin_published")
    builder.button(text="📊 Статистика", callback_data="admin_stats")
    builder.button(text="📢 Рассылка", callback_data="admin_broadcast")
    builder.button(text="💾 Скачать БД", callback_data="admin_export_db")
    builder.button(text="📥 Загрузить БД", callback_data="admin_import_db")
    builder.button(text="📑 Экспорт (CSV)", callback_data="admin_export_csv")
    builder.button(text="⚠️ Сбросить рейтинг", callback_data="admin_clear_ranks_start")
    builder.adjust(1, 2, 2, 2, 2)
    return builder.as_markup()

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    try: await message.delete()
    except: pass
    await message.answer("🛠 <b>Панель управления МедиСферой:</b>", reply_markup=get_admin_main_kb())

@dp.callback_query(F.data == "admin_main")
async def back_to_admin_main(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("🛠 <b>Панель управления:</b>", reply_markup=get_admin_main_kb())

# --- СОЗДАНИЕ ТЕСТА ---
@dp.callback_query(F.data == "admin_create_test")
async def admin_create_test(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    text = "📚 <b>Шаг 1: Выберите курс для нового теста</b>"
    builder = InlineKeyboardBuilder()
    for c_id, c_data in COURSES.items():
        builder.button(text=c_data['name'], callback_data=f"add_c_{c_id}")
    builder.button(text="🔙 Отмена", callback_data="admin_main")
    builder.adjust(3, 1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await state.set_state(AdminState.waiting_for_course)

@dp.callback_query(AdminState.waiting_for_course, F.data.startswith("add_c_"))
async def admin_add_course(callback: types.CallbackQuery, state: FSMContext):
    c_id = callback.data.split("_")[2]
    await state.update_data(course_id=c_id, pending_questions=[])
    
    text = "🧬 <b>Шаг 2: Выберите предмет</b>"
    builder = InlineKeyboardBuilder()
    for s_id, s_name in COURSES[c_id]['subs'].items():
        builder.button(text=s_name, callback_data=f"add_s_{s_id}")
    builder.button(text="🔙 Отмена", callback_data="admin_main")
    builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await state.set_state(AdminState.waiting_for_subject)

@dp.callback_query(AdminState.waiting_for_subject, F.data.startswith("add_s_"))
async def admin_add_subject(callback: types.CallbackQuery, state: FSMContext):
    s_id = callback.data.split("_")[2]
    await state.update_data(subj_id=s_id)
    await prompt_next_question(callback.message, state)

async def prompt_next_question(message: types.Message, state: FSMContext):
    data = await state.get_data()
    q_num = len(data.get('pending_questions', [])) + 1
    await message.answer(f"📝 <b>Вопрос №{q_num}</b>\nОтправьте текст вопроса.\n<i>(Если нужна картинка — отправьте её, а текст напишите в подписи)</i>")
    await state.set_state(AdminState.waiting_for_q_text)

@dp.message(AdminState.waiting_for_q_text)
async def admin_q_text(message: types.Message, state: FSMContext):
    text = message.html_text or message.caption
    photo_id = message.photo[-1].file_id if message.photo else None
    await state.update_data(cur_text=text, cur_photo=photo_id)
    await message.answer("Отправьте <b>варианты ответов</b> (каждый с новой строки):")
    await state.set_state(AdminState.waiting_for_q_opts)

@dp.message(AdminState.waiting_for_q_opts)
async def admin_q_opts(message: types.Message, state: FSMContext):
    opts = [opt.strip() for opt in message.text.split('\n') if opt.strip()]
    if len(opts) < 2: return await message.answer("Нужно минимум 2 варианта!")
    await state.update_data(cur_opts=opts)
    
    builder = InlineKeyboardBuilder()
    for idx, opt in enumerate(opts):
        builder.button(text=opt, callback_data=f"add_corr_{idx}")
    builder.adjust(1)
    await message.answer("Выбери <b>правильный</b> ответ:", reply_markup=builder.as_markup())
    await state.set_state(AdminState.waiting_for_q_correct)

@dp.callback_query(AdminState.waiting_for_q_correct, F.data.startswith("add_corr_"))
async def admin_q_correct(callback: types.CallbackQuery, state: FSMContext):
    idx = int(callback.data.split("_")[2])
    await state.update_data(cur_corr=idx)
    await callback.message.edit_text("Отправь <b>разбор/объяснение</b>:")
    await state.set_state(AdminState.waiting_for_q_expl)

@dp.message(AdminState.waiting_for_q_expl)
async def admin_q_expl(message: types.Message, state: FSMContext):
    data = await state.get_data()
    pq = data.get('pending_questions', [])
    pq.append({
        'text': data['cur_text'], 'options': json.dumps(data['cur_opts']),
        'correct': data['cur_corr'], 'expl': message.html_text, 'photo': data['cur_photo']
    })
    await state.update_data(pending_questions=pq)
    
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить еще вопрос", callback_data="add_more_q")
    builder.button(text="✅ Завершить создание", callback_data="finish_test_creation")
    builder.adjust(1)
    await message.answer(f"В тесте уже {len(pq)} вопросов.", reply_markup=builder.as_markup())

@dp.callback_query(F.data == "add_more_q")
async def admin_add_more(callback: types.CallbackQuery, state: FSMContext):
    await prompt_next_question(callback.message, state)
    await callback.answer()

@dp.callback_query(F.data == "finish_test_creation")
async def admin_finish_test(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Отлично! Напиши <b>название этого теста</b>\n<i>(Например: Миология 1: Мышцы спины и туловища)</i>:")
    await state.set_state(AdminState.waiting_for_test_title)

@dp.message(AdminState.waiting_for_test_title)
async def save_test_to_db(message: types.Message, state: FSMContext):
    title = message.text.strip()
    data = await state.get_data()
    c_id, s_id, pq = data['course_id'], data['subj_id'], data['pending_questions']
    
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("INSERT INTO tests (title, course, subject, is_published) VALUES (?, ?, ?, 0)", (title, c_id, s_id))
        test_id = cursor.lastrowid
        
        for q in pq:
            await db.execute(
                "INSERT INTO questions (text, options, correct_index, explanation, photo_id, is_published, test_id) VALUES (?, ?, ?, ?, ?, 0, ?)",
                (q['text'], q['options'], q['correct'], q['expl'], q['photo'], test_id)
            )
        await db.commit()
        
    await message.answer(f"✅ Тест «{title}» ({len(pq)} вопросов) сохранен в <b>Черновики</b>!", reply_markup=get_admin_main_kb())
    await state.clear()

# --- ЧЕРНОВИКИ И ПУБЛИКАЦИЯ ---
@dp.callback_query(F.data == "admin_drafts")
async def admin_drafts(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT id, title, course, subject FROM tests WHERE is_published = 0")
        tests = await cursor.fetchall()

    if not tests:
        return await callback.message.edit_text("📝 Черновиков нет.", reply_markup=InlineKeyboardBuilder().button(text="🔙 Назад", callback_data="admin_main").as_markup())

    text = "📝 <b>Черновики тестов:</b>\n\n"
    for t_id, title, c, s in tests:
        text += f"<b>ID {t_id}</b>: {title} <i>({c}->{s})</i>\n"

    builder = InlineKeyboardBuilder()
    builder.button(text="🚀 Опубликовать все", callback_data="admin_publish_all")
    builder.button(text="✏️ Редактировать", callback_data="admin_edit_test")
    builder.button(text="🗑 Удалить тест", callback_data="admin_del_test")
    builder.button(text="🔙 В админ-меню", callback_data="admin_main")
    builder.adjust(1, 2, 1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup())

@dp.callback_query(F.data == "admin_published")
async def admin_published(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT id, title FROM tests WHERE is_published = 1")
        tests = await cursor.fetchall()

    if not tests:
        return await callback.message.edit_text("✅ Опубликованных тестов нет.", reply_markup=InlineKeyboardBuilder().button(text="🔙 Назад", callback_data="admin_main").as_markup())

    text = "✅ <b>Опубликованные тесты:</b>\n\n"
    for t_id, title in tests:
        text += f"<b>ID {t_id}</b>: {title}\n"

    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Редактировать", callback_data="admin_edit_test")
    builder.button(text="🗑 Удалить тест", callback_data="admin_del_test")
    builder.button(text="🔙 В админ-меню", callback_data="admin_main")
    builder.adjust(2, 1)
    
    if len(text) > 4000:
        await callback.message.delete()
        await callback.message.answer(text[:4000], reply_markup=builder.as_markup())
    else:
        await callback.message.edit_text(text, reply_markup=builder.as_markup())

@dp.callback_query(F.data == "admin_publish_all")
async def admin_publish_all(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT id, title, course, subject FROM tests WHERE is_published = 0")
        drafts = await cursor.fetchall()
        
        if not drafts: return await callback.answer("Нет тестов.", show_alert=True)
        
        notification_text = "🔥 <b>Доступны новые тесты!</b>\n\n"
        for _, title, c_id, s_id in drafts:
            c_name = COURSES.get(c_id, {}).get("name", "Курс")
            s_name = COURSES.get(c_id, {}).get("subs", {}).get(s_id, "Предмет")
            notification_text += f"🎓 <b>{c_name}</b> | 🧬 <b>{s_name}</b>\n📝 <i>{title}</i>\n\n"
            
        notification_text += "Заходите в раздел <b>Выбрать квиз</b> и проверяйте знания! ⏱"
        
        await db.execute("UPDATE tests SET is_published = 1 WHERE is_published = 0")
        await db.execute("UPDATE questions SET is_published = 1 WHERE is_published = 0")
        await db.commit()
        
        cursor = await db.execute("SELECT user_id FROM users WHERE is_registered = 1")
        users = await cursor.fetchall()
        
    builder = InlineKeyboardBuilder()
    builder.button(text="🧠 Выбрать квиз", callback_data="menu_courses")
    
    await callback.message.edit_text(f"🚀 Опубликовано {len(drafts)} тестов. Рассылаю...", reply_markup=InlineKeyboardBuilder().button(text="🔙 Назад", callback_data="admin_main").as_markup())
    
    success = 0
    for (uid,) in users:
        try:
            await bot.send_message(uid, notification_text, reply_markup=builder.as_markup())
            success += 1
        except: pass
        await asyncio.sleep(0.05)
    await callback.message.answer(f"✅ Доставлено: {success}/{len(users)}")

# --- УМНОЕ РЕДАКТИРОВАНИЕ ТЕСТА И ВОПРОСОВ ---
async def show_edit_test_menu(target_msg: types.Message, t_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT title FROM tests WHERE id = ?", (t_id,))
        res = await cursor.fetchone()
        
    if not res:
        await target_msg.answer("❌ Тест не найден.")
        return
        
    text = f"🛠 <b>Редактор теста ID {t_id}</b>\nНазвание: {res[0]}\n\nЧто сделать?"
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Изменить название", callback_data="edit_t_title")
    builder.button(text="📝 Изменить вопрос", callback_data="edit_t_edit_q")
    builder.button(text="🗑 Удалить вопрос", callback_data="edit_t_del_q_start")
    builder.button(text="🔙 В админку", callback_data="admin_main")
    builder.adjust(1)
    
    if isinstance(target_msg, types.Message) and not hasattr(target_msg, 'edit_text_callable'):
        await target_msg.answer(text, reply_markup=builder.as_markup())
    else:
        await target_msg.edit_text(text, reply_markup=builder.as_markup())

@dp.callback_query(F.data == "admin_edit_test")
async def admin_edit_test_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введи <b>ID теста</b> для редактирования:", reply_markup=InlineKeyboardBuilder().button(text="🔙 Назад", callback_data="admin_main").as_markup())
    await state.set_state(AdminState.waiting_for_edit_test_id)

@dp.message(AdminState.waiting_for_edit_test_id)
async def process_edit_test_id(message: types.Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("Только числа!")
    t_id = int(message.text)
    await state.update_data(edit_test_id=t_id)
    # Искусственно добавляем флаг, чтобы знать, что это обычный message
    await show_edit_test_menu(message, t_id)

@dp.callback_query(F.data == "back_to_edit_test")
async def back_to_edit_test_cb(callback: types.CallbackQuery, state: FSMContext):
    t_id = (await state.get_data()).get('edit_test_id')
    await show_edit_test_menu(callback.message, t_id)
    await callback.answer()

@dp.callback_query(F.data == "edit_t_title")
async def edit_t_title_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введи новое название теста:", reply_markup=InlineKeyboardBuilder().button(text="🔙 Отмена", callback_data="back_to_edit_test").as_markup())
    await state.set_state(AdminState.waiting_for_new_test_title)

@dp.message(AdminState.waiting_for_new_test_title)
async def save_new_t_title(message: types.Message, state: FSMContext):
    t_id = (await state.get_data()).get('edit_test_id')
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE tests SET title = ? WHERE id = ?", (message.text, t_id))
        await db.commit()
    await show_edit_test_menu(message, t_id)

# --- НОВЫЙ БЛОК: РЕДАКТИРОВАНИЕ ВОПРОСА ---
@dp.callback_query(F.data == "edit_t_edit_q")
async def edit_t_edit_q_start(callback: types.CallbackQuery, state: FSMContext):
    t_id = (await state.get_data()).get('edit_test_id')
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT id, text FROM questions WHERE test_id = ?", (t_id,))
        qs = await cursor.fetchall()
        
    if not qs:
        return await callback.message.edit_text("В этом тесте пока нет вопросов.", reply_markup=InlineKeyboardBuilder().button(text="🔙 Назад", callback_data="back_to_edit_test").as_markup())
        
    text = "📚 <b>Список вопросов в этом тесте:</b>\n\n"
    for q_id, q_text in qs:
        short = q_text[:30] + "..." if len(q_text) > 30 else q_text
        text += f"ID {q_id}: {short}\n"
        
    text += "\nВведи <b>ID вопроса</b>, который хочешь изменить:"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardBuilder().button(text="🔙 Назад", callback_data="back_to_edit_test").as_markup())
    await state.set_state(AdminState.waiting_for_edit_q_id)

@dp.message(AdminState.waiting_for_edit_q_id)
async def process_edit_q_id(message: types.Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("Только числовое ID!")
    q_id = int(message.text)
    t_id = (await state.get_data()).get('edit_test_id')
    
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT id FROM questions WHERE id = ? AND test_id = ?", (q_id, t_id))
        if not await cursor.fetchone():
            return await message.answer("❌ Вопрос с таким ID не найден в этом тесте.", reply_markup=InlineKeyboardBuilder().button(text="🔙 Назад", callback_data="back_to_edit_test").as_markup())
            
    await state.update_data(edit_q_id=q_id)
    await show_q_edit_menu(message, q_id)

async def show_q_edit_menu(target_msg: types.Message, q_id: int):
    text = f"⚙️ <b>Редактирование вопроса ID {q_id}</b>\nЧто хотите изменить?"
    builder = InlineKeyboardBuilder()
    builder.button(text="🖼 Вопрос/Картинку", callback_data="q_edit_text")
    builder.button(text="✅ Ответы", callback_data="q_edit_ans")
    builder.button(text="📖 Объяснение", callback_data="q_edit_expl")
    builder.button(text="🔙 К списку вопросов", callback_data="edit_t_edit_q")
    builder.adjust(1)
    
    if isinstance(target_msg, types.Message) and not hasattr(target_msg, 'edit_text_callable'):
        await target_msg.answer(text, reply_markup=builder.as_markup())
    else:
        await target_msg.edit_text(text, reply_markup=builder.as_markup())

@dp.callback_query(F.data == "back_to_q_edit_menu")
async def back_to_q_edit_menu_cb(callback: types.CallbackQuery, state: FSMContext):
    q_id = (await state.get_data()).get('edit_q_id')
    await show_q_edit_menu(callback.message, q_id)

@dp.callback_query(F.data == "q_edit_text")
async def q_edit_text_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Отправь новый <b>текст вопроса</b>.\n<i>(Если нужна картинка — прикрепи её и напиши текст в подписи)</i>", reply_markup=InlineKeyboardBuilder().button(text="🔙 Отмена", callback_data="back_to_q_edit_menu").as_markup())
    await state.set_state(AdminState.waiting_for_edit_q_text_photo)

@dp.message(AdminState.waiting_for_edit_q_text_photo)
async def process_q_edit_text_photo(message: types.Message, state: FSMContext):
    q_id = (await state.get_data()).get('edit_q_id')
    text = message.html_text or message.caption
    photo_id = message.photo[-1].file_id if message.photo else None
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE questions SET text = ?, photo_id = ? WHERE id = ?", (text, photo_id, q_id))
        await db.commit()
    await message.answer("✅ Текст/картинка вопроса обновлены!")
    await show_q_edit_menu(message, q_id)

@dp.callback_query(F.data == "q_edit_ans")
async def q_edit_ans_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Отправь новые <b>варианты ответов</b> (каждый с новой строки):", reply_markup=InlineKeyboardBuilder().button(text="🔙 Отмена", callback_data="back_to_q_edit_menu").as_markup())
    await state.set_state(AdminState.waiting_for_edit_q_opts)

@dp.message(AdminState.waiting_for_edit_q_opts)
async def process_q_edit_opts(message: types.Message, state: FSMContext):
    opts = [opt.strip() for opt in message.text.split('\n') if opt.strip()]
    if len(opts) < 2: return await message.answer("Минимум 2 варианта!")
    await state.update_data(edit_q_opts_temp=opts)
    
    builder = InlineKeyboardBuilder()
    for idx, opt in enumerate(opts):
        builder.button(text=opt, callback_data=f"edit_corr_{idx}")
    builder.adjust(1)
    await message.answer("Выбери <b>правильный</b> ответ из новых:", reply_markup=builder.as_markup())
    await state.set_state(AdminState.waiting_for_edit_q_correct)

@dp.callback_query(AdminState.waiting_for_edit_q_correct, F.data.startswith("edit_corr_"))
async def process_q_edit_correct(callback: types.CallbackQuery, state: FSMContext):
    idx = int(callback.data.split("_")[2])
    data = await state.get_data()
    q_id = data['edit_q_id']
    opts_json = json.dumps(data['edit_q_opts_temp'])
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE questions SET options = ?, correct_index = ? WHERE id = ?", (opts_json, idx, q_id))
        await db.commit()
        
    await callback.message.edit_text("✅ Варианты и правильный ответ обновлены!")
    await show_q_edit_menu(callback.message, q_id)

@dp.callback_query(F.data == "q_edit_expl")
async def q_edit_expl_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Отправь новое <b>объяснение/разбор</b>:", reply_markup=InlineKeyboardBuilder().button(text="🔙 Отмена", callback_data="back_to_q_edit_menu").as_markup())
    await state.set_state(AdminState.waiting_for_edit_q_expl)

@dp.message(AdminState.waiting_for_edit_q_expl)
async def process_q_edit_expl(message: types.Message, state: FSMContext):
    q_id = (await state.get_data()).get('edit_q_id')
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE questions SET explanation = ? WHERE id = ?", (message.html_text, q_id))
        await db.commit()
    await message.answer("✅ Объяснение обновлено!")
    await show_q_edit_menu(message, q_id)

# --- УДАЛЕНИЕ ВОПРОСА ВНУТРИ ТЕСТА ---
@dp.callback_query(F.data == "edit_t_del_q_start")
async def edit_t_del_q_start(callback: types.CallbackQuery, state: FSMContext):
    t_id = (await state.get_data()).get('edit_test_id')
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT id, text FROM questions WHERE test_id = ?", (t_id,))
        qs = await cursor.fetchall()
        
    text = "📚 <b>Список вопросов в этом тесте:</b>\n\n"
    for q_id, q_text in qs:
        short = q_text[:30] + "..." if len(q_text) > 30 else q_text
        text += f"ID {q_id}: {short}\n"
        
    text += "\nВведи <b>ID вопроса</b> для его удаления:"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardBuilder().button(text="🔙 Отмена", callback_data="back_to_edit_test").as_markup())
    await state.set_state(AdminState.waiting_for_del_question_id)

@dp.message(AdminState.waiting_for_del_question_id)
async def process_del_q(message: types.Message, state: FSMContext):
    q_id = message.text
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM questions WHERE id = ?", (q_id,))
        await db.commit()
    await message.answer("✅ Вопрос удален.")
    t_id = (await state.get_data()).get('edit_test_id')
    await show_edit_test_menu(message, t_id)

@dp.callback_query(F.data == "admin_del_test")
async def admin_del_test_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("🗑 Введи <b>ID теста</b> для удаления (удалится тест и все его вопросы):", reply_markup=InlineKeyboardBuilder().button(text="🔙 Назад", callback_data="admin_main").as_markup())
    await state.set_state(AdminState.waiting_for_q_del_id)

@dp.message(AdminState.waiting_for_q_del_id)
async def admin_del_test_process(message: types.Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("Только ID (число).")
    t_id = int(message.text)
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM questions WHERE test_id = ?", (t_id,))
        cursor = await db.execute("DELETE FROM tests WHERE id = ?", (t_id,))
        await db.commit()
        resp = f"✅ Тест ID {t_id} и его вопросы удалены." if cursor.rowcount > 0 else "❌ Тест не найден."
    await message.answer(resp, reply_markup=get_admin_main_kb())
    await state.clear()

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    async with aiosqlite.connect(DB_NAME) as db:
        users_c = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
        tests_pub = (await (await db.execute("SELECT COUNT(*) FROM tests WHERE is_published=1")).fetchone())[0]
        tests_draft = (await (await db.execute("SELECT COUNT(*) FROM tests WHERE is_published=0")).fetchone())[0]
    await callback.message.edit_text(f"📊 <b>Статистика:</b>\n👥 Юзеров: {users_c}\n✅ Тестов (паков) опубликовано: {tests_pub}\n📝 Тестов в черновиках: {tests_draft}", reply_markup=InlineKeyboardBuilder().button(text="🔙 Назад", callback_data="admin_main").as_markup())

@dp.callback_query(F.data == "admin_export_db")
async def admin_export_db(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    await bot.send_document(chat_id=ADMIN_ID, document=FSInputFile(DB_NAME))
    await callback.answer()
    
@dp.callback_query(F.data == "admin_import_db")
async def admin_import_db_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await callback.message.edit_text("Отправь мне файл базы данных (<b>medisphere.db</b>) для восстановления.\n⚠️ База будет полностью перезаписана!", reply_markup=InlineKeyboardBuilder().button(text="🔙 Назад", callback_data="admin_main").as_markup())
    await state.set_state(AdminState.waiting_for_db_file)

@dp.message(AdminState.waiting_for_db_file, F.document)
async def admin_import_db_process(message: types.Message, state: FSMContext):
    if not message.document.file_name.endswith('.db'): return await message.answer("Только файл .db!")
    file = await bot.get_file(message.document.file_id)
    await bot.download_file(file.file_path, DB_NAME)
    await message.answer("✅ База данных успешно импортирована! Введи /admin", reply_markup=get_admin_main_kb())
    await state.clear()

@dp.callback_query(F.data == "admin_export_csv")
async def admin_export_csv(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    async with aiosqlite.connect(DB_NAME) as db:
        users = await (await db.execute("SELECT user_id, username, score, weekly_score, course FROM users ORDER BY score DESC")).fetchall()
    output = io.StringIO()
    writer = csv.writer(output, delimiter=';') 
    writer.writerow(['ID Telegram', 'Имя', 'Курс', 'Очки (Всего)', 'Очки (Неделя)'])
    for u in users: writer.writerow(u)
    await bot.send_document(chat_id=ADMIN_ID, document=BufferedInputFile(output.getvalue().encode('utf-8-sig'), filename="medisphere_stats.csv"))
    await callback.answer()

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("📢 Введи сообщение для рассылки всем юзерам:", reply_markup=InlineKeyboardBuilder().button(text="🔙 Отмена", callback_data="admin_main").as_markup())
    await state.set_state(AdminState.waiting_for_broadcast)

@dp.message(AdminState.waiting_for_broadcast)
async def process_broadcast(message: types.Message, state: FSMContext):
    async with aiosqlite.connect(DB_NAME) as db:
        users = await (await db.execute("SELECT user_id FROM users WHERE is_registered = 1")).fetchall()
    await message.answer("Рассылка...")
    success = 0
    for (uid,) in users:
        try:
            await bot.send_message(uid, message.html_text)
            success += 1
        except: pass
        await asyncio.sleep(0.05)
    await message.answer(f"✅ Доставлено: {success}/{len(users)}", reply_markup=get_admin_main_kb())
    await state.clear()

@dp.callback_query(F.data == "admin_clear_ranks_start")
async def admin_clear_ranks_start(callback: types.CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.button(text="🚨 Да, обнулить баллы", callback_data="confirm_clear_ranks")
    builder.button(text="🔙 Отмена", callback_data="admin_main")
    await callback.message.edit_text("⚠️ <b>ОПАСНО:</b> Сбросить ВСЕМ баллы, историю и время? (Тесты не удалятся)", reply_markup=builder.as_markup())

@dp.callback_query(F.data == "confirm_clear_ranks")
async def admin_clear_ranks_yes(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET score=0, weekly_score=0, answered_questions='[]', completed_tests='[]', total_response_time=0")
        await db.commit()
    await callback.message.edit_text("✅ Рейтинги всех пользователей обнулены.", reply_markup=InlineKeyboardBuilder().button(text="🔙 Назад", callback_data="admin_main").as_markup())

# ==========================================
# 11. ЗАПУСК БОТА
# ==========================================
async def main():
    await init_db()
    print("Бот МедиСферы v3 (Smart Edit) успешно запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: print("Бот остановлен.")