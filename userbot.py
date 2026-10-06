import asyncio
import sqlite3
import os
import json
import random
from datetime import datetime, timedelta
from threading import Thread

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl import types
from telethon.tl.types import PeerUser, PeerChat, PeerChannel, MessageMediaPhoto, MessageMediaDocument
import telebot
from telebot import types as tb_types

# === СЕКРЕТЫ ===
load_dotenv()

api_id_raw = os.getenv('API_ID')
api_hash = os.getenv('API_HASH')
token = os.getenv('BOT_TOKEN')
session_string = os.getenv('SESSION_STRING', '')

missing = [k for k, v in {'API_ID': api_id_raw, 'API_HASH': api_hash, 'BOT_TOKEN': token}.items() if not v]
if missing:
    raise RuntimeError(f"Отсутствуют переменные окружения: {', '.join(missing)}")

api_id = int(api_id_raw)

# Суперадмин (неизменяем, задаётся здесь)
SUPERADMIN_ID = 8179854758

bot = telebot.TeleBot(token)
client = None  # создаётся в main()

MEDIA_DIR = 'deleted_media'
os.makedirs(MEDIA_DIR, exist_ok=True)

conn = sqlite3.connect('userbot.db', check_same_thread=False)
cursor = conn.cursor()

# === ТАБЛИЦЫ БД ===

cursor.execute('''CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    msg_id INTEGER,
    user_id INTEGER,
    chat_id INTEGER,
    text TEXT,
    media_path TEXT,
    media_type TEXT,
    date TEXT
)''')

cursor.execute('''CREATE TABLE IF NOT EXISTS muted_users (user_id INTEGER PRIMARY KEY)''')

cursor.execute('''CREATE TABLE IF NOT EXISTS deleted_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id INTEGER,
    user_id INTEGER,
    chat_id INTEGER,
    text TEXT,
    media_path TEXT,
    media_type TEXT,
    original_date TEXT,
    deleted_at TEXT
)''')

# admins: user_id, username, first_name, added_at
cursor.execute('''CREATE TABLE IF NOT EXISTS admins (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    added_at TEXT
)''')

# bot_users: user_id, username, first_name, status (pending/approved/banned), joined_at
cursor.execute('''CREATE TABLE IF NOT EXISTS bot_users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    status TEXT DEFAULT 'pending',
    joined_at TEXT
)''')

# reminders: id, user_id, text, remind_at
cursor.execute('''CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    text TEXT,
    remind_at TEXT
)''')

# afk: user_id, reason, since
cursor.execute('''CREATE TABLE IF NOT EXISTS afk (
    user_id INTEGER PRIMARY KEY,
    reason TEXT,
    since TEXT
)''')

# Миграция: добавляем колонки если их нет
existing_columns = {row[1] for row in cursor.execute('PRAGMA table_info(messages)')}
if 'media_path' not in existing_columns:
    cursor.execute('ALTER TABLE messages ADD COLUMN media_path TEXT')
if 'media_type' not in existing_columns:
    cursor.execute('ALTER TABLE messages ADD COLUMN media_type TEXT')

conn.commit()

# Добавляем суперадмина в таблицу admins при старте
cursor.execute('INSERT OR IGNORE INTO admins (user_id, username, first_name, added_at) VALUES (?, ?, ?, ?)',
               (SUPERADMIN_ID, 'superadmin', 'SuperAdmin', datetime.now().isoformat()))
conn.commit()

stored_messages = {}
owner_id = None
muted_users = set()
afk_users = {}  # user_id -> {reason, since}


# === ПРОВЕРКА ПРАВ ===

def is_admin(user_id: int) -> bool:
    cursor.execute('SELECT user_id FROM admins WHERE user_id=?', (user_id,))
    return cursor.fetchone() is not None

def is_approved(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    cursor.execute('SELECT status FROM bot_users WHERE user_id=?', (user_id,))
    row = cursor.fetchone()
    return row is not None and row[0] == 'approved'

def is_superadmin(user_id: int) -> bool:
    return user_id == SUPERADMIN_ID

def get_user_status_label(user_id: int) -> str:
    if is_superadmin(user_id):
        return '👑 Суперадмин'
    if is_admin(user_id):
        return '🛡 Админ'
    cursor.execute('SELECT status FROM bot_users WHERE user_id=?', (user_id,))
    row = cursor.fetchone()
    if not row:
        return '❓ Неизвестен'
    status_map = {'approved': '✅ Разрешён', 'pending': '⏳ Ожидает', 'banned': '🚫 Заблокирован'}
    return status_map.get(row[0], row[0])


# === ХЕЛПЕРЫ ===

async def get_user_info(user_id):
    try:
        user = await client.get_entity(user_id)
        username = user.username if user.username else ""
        name = f"{user.first_name or ''} {user.last_name or ''}".strip()
        return username, name, user
    except Exception as e:
        print(f"get_user_info error: {e}")
        return "", "Неизвестный пользователь", None


async def get_chat_title(chat_id):
    try:
        entity = await client.get_entity(chat_id)
        if hasattr(entity, 'title'):
            return entity.title
        if hasattr(entity, 'first_name'):
            return f"{entity.first_name or ''} {entity.last_name or ''}".strip()
    except Exception:
        pass
    return f"ID:{chat_id}"


def send_bot_message_sync(chat_id, text, media_path=None, media_type=None, reply_markup=None):
    try:
        if media_path and os.path.exists(media_path):
            with open(media_path, 'rb') as f:
                if media_type == 'photo':
                    bot.send_photo(chat_id, f, caption=text, parse_mode='HTML', reply_markup=reply_markup)
                elif media_type == 'video':
                    bot.send_video(chat_id, f, caption=text, parse_mode='HTML', reply_markup=reply_markup)
                elif media_type == 'voice':
                    bot.send_voice(chat_id, f, caption=text, parse_mode='HTML', reply_markup=reply_markup)
                elif media_type == 'audio':
                    bot.send_audio(chat_id, f, caption=text, parse_mode='HTML', reply_markup=reply_markup)
                elif media_type == 'sticker':
                    bot.send_sticker(chat_id, f)
                    if text:
                        bot.send_message(chat_id, text, parse_mode='HTML', disable_web_page_preview=True, reply_markup=reply_markup)
                else:
                    bot.send_document(chat_id, f, caption=text, parse_mode='HTML', reply_markup=reply_markup)
        else:
            if text:
                bot.send_message(chat_id, text, parse_mode='HTML', disable_web_page_preview=True, reply_markup=reply_markup)
    except Exception as e:
        print(f"Bot send error: {e}")
        try:
            if text:
                bot.send_message(chat_id, text, parse_mode='HTML', disable_web_page_preview=True)
        except Exception as e2:
            print(f"Bot fallback send error: {e2}")


async def send_bot_message(text, media_path=None, media_type=None, chat_id=None, reply_markup=None):
    target = chat_id or owner_id
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, send_bot_message_sync, target, text, media_path, media_type, reply_markup)


async def check_is_owner(event):
    return event.message.sender_id == owner_id


async def download_media_if_exists(message) -> tuple:
    if not message.media:
        return None, None
    try:
        media = message.media
        media_type = None

        if isinstance(media, MessageMediaPhoto):
            media_type = 'photo'
            ext = '.jpg'
        elif isinstance(media, MessageMediaDocument):
            doc = media.document
            mime = doc.mime_type if hasattr(doc, 'mime_type') else ''
            if mime.startswith('video/'):
                media_type = 'video'
                ext = '.mp4'
            elif mime == 'audio/ogg':
                media_type = 'voice'
                ext = '.ogg'
            elif mime.startswith('audio/'):
                media_type = 'audio'
                ext = '.mp3'
            elif mime == 'image/webp':
                media_type = 'sticker'
                ext = '.webp'
            else:
                media_type = 'document'
                ext = ''
                for attr in getattr(doc, 'attributes', []):
                    if hasattr(attr, 'file_name') and attr.file_name:
                        _, ext = os.path.splitext(attr.file_name)
                        break
                if not ext:
                    ext = '.bin'
        else:
            return None, None

        filename = f"{MEDIA_DIR}/{message.id}_{int(datetime.now().timestamp())}{ext}"
        path = await client.download_media(message, filename)
        return path, media_type
    except Exception as e:
        print(f"Media download error: {e}")
        return None, None


def get_peer_id(peer):
    if isinstance(peer, PeerUser):
        return peer.user_id
    if isinstance(peer, PeerChat):
        return peer.chat_id
    if isinstance(peer, PeerChannel):
        return peer.channel_id
    return None


def is_group_peer(peer):
    return isinstance(peer, (PeerChat, PeerChannel))


# === СОХРАНЕНИЕ СООБЩЕНИЙ ===

async def store_message(message):
    try:
        peer = message.peer_id
        chat_id = get_peer_id(peer)
        sender_id = message.sender_id
        if not chat_id or not sender_id:
            return

        text = message.text or message.message or ""
        media_path, media_type = await download_media_if_exists(message)

        cursor.execute(
            'INSERT OR REPLACE INTO messages (msg_id, user_id, chat_id, text, media_path, media_type, date) '
            'VALUES (?, ?, ?, ?, ?, ?, ?)',
            (message.id, sender_id, chat_id, text,
             media_path, media_type, datetime.now().isoformat())
        )
        conn.commit()
        stored_messages[(chat_id, message.id)] = text
    except Exception as e:
        print(f"Store error: {e}")


# === УДАЛЁННЫЕ СООБЩЕНИЯ ===

async def on_message_deleted(msg_id: int, chat_id=None):
    if chat_id:
        cursor.execute(
            'SELECT user_id, chat_id, text, media_path, media_type, date '
            'FROM messages WHERE msg_id=? AND chat_id=?',
            (msg_id, chat_id)
        )
    else:
        cursor.execute(
            'SELECT user_id, chat_id, text, media_path, media_type, date '
            'FROM messages WHERE msg_id=?',
            (msg_id,)
        )
    row = cursor.fetchone()
    if not row:
        return

    user_id, found_chat_id, text, media_path, media_type, orig_date = row

    if user_id == owner_id:
        cursor.execute('DELETE FROM messages WHERE msg_id=? AND chat_id=?', (msg_id, found_chat_id))
        conn.commit()
        return

    cursor.execute(
        'INSERT INTO deleted_messages (msg_id, user_id, chat_id, text, media_path, media_type, original_date, deleted_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (msg_id, user_id, found_chat_id, text, media_path, media_type, orig_date, datetime.now().isoformat())
    )
    cursor.execute('DELETE FROM messages WHERE msg_id=? AND chat_id=?', (msg_id, found_chat_id))
    conn.commit()
    stored_messages.pop((found_chat_id, msg_id), None)

    username, name, _ = await get_user_info(user_id)
    link = f"https://t.me/{username}" if username else f"tg://user?id={user_id}"

    if found_chat_id != user_id:
        chat_title = await get_chat_title(found_chat_id)
        source = f" в <b>{chat_title}</b>"
    else:
        source = ""

    media_label = ""
    if media_type:
        labels = {'photo': '🖼 Фото', 'video': '🎬 Видео', 'voice': '🎤 Голосовое',
                  'audio': '🎵 Аудио', 'sticker': '🎭 Стикер', 'document': '📎 Документ'}
        media_label = f"\n{labels.get(media_type, '📎 Медиа')}"

    caption = (
        f"🗑 Удалённое сообщение{source}\n\n"
        f"<blockquote><a href=\"{link}\">{name}</a>{media_label}\n"
        f"{text or ''}</blockquote>"
    )

    await send_bot_message(caption, media_path, media_type)


async def raw_deleted_handler(event):
    try:
        channel_id = getattr(event, 'channel_id', None)
        msg_ids = getattr(event, 'messages', [])
        for msg_id in msg_ids:
            await on_message_deleted(msg_id, chat_id=channel_id)
    except Exception as e:
        print(f"Raw delete error: {e}")


# === РЕДАКТИРОВАНИЯ ===

async def process_edited_message(event):
    if event.message.out:
        return
    try:
        peer = event.message.peer_id
        chat_id = get_peer_id(peer)
        if not chat_id:
            return

        cursor.execute('SELECT text FROM messages WHERE msg_id=? AND chat_id=?', (event.message.id, chat_id))
        row = cursor.fetchone()
        if not row:
            return

        old_text = row[0]
        new_text = event.message.text or event.message.message or ""
        if new_text == old_text:
            return

        user_id = event.message.sender_id
        if not user_id or user_id == owner_id:
            return

        username, name, _ = await get_user_info(user_id)
        link = f"https://t.me/{username}" if username else f"tg://user?id={user_id}"

        if chat_id != user_id:
            chat_title = await get_chat_title(chat_id)
            source = f" в <b>{chat_title}</b>"
        else:
            source = ""

        message_text = (
            f"🔏 <a href=\"{link}\">{name}</a> изменил сообщение{source}.\n\n"
            f"Старый текст:\n<blockquote>{old_text}</blockquote>\n"
            f"Новый текст:\n<blockquote>{new_text}</blockquote>"
        )
        await send_bot_message(message_text)

        cursor.execute('UPDATE messages SET text=? WHERE msg_id=? AND chat_id=?', (new_text, event.message.id, chat_id))
        conn.commit()
        stored_messages[(chat_id, event.message.id)] = new_text
    except Exception as e:
        print(f"Edit error: {e}")


# =====================================================
# === БОТ: СИСТЕМА ДОСТУПА И АДМИН-ПАНЕЛЬ ===
# =====================================================

def build_start_keyboard(user_id: int):
    kb = tb_types.InlineKeyboardMarkup(row_width=2)
    if is_admin(user_id):
        kb.add(tb_types.InlineKeyboardButton('🛡 Панель администратора', callback_data='admin_panel'))
    if is_approved(user_id):
        kb.add(tb_types.InlineKeyboardButton('📋 Мои команды', callback_data='my_commands'))
        kb.add(tb_types.InlineKeyboardButton('ℹ️ О боте', callback_data='about'))
    return kb


def build_admin_panel_keyboard():
    kb = tb_types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        tb_types.InlineKeyboardButton('👥 Заявки на доступ', callback_data='admin_pending'),
        tb_types.InlineKeyboardButton('✅ Разрешённые пользователи', callback_data='admin_approved'),
    )
    kb.add(
        tb_types.InlineKeyboardButton('🚫 Заблокированные', callback_data='admin_banned'),
        tb_types.InlineKeyboardButton('👑 Список админов', callback_data='admin_list'),
    )
    kb.add(tb_types.InlineKeyboardButton('🔙 Назад', callback_data='back_start'))
    return kb


@bot.message_handler(commands=['start'])
def start_handler(message):
    user_id = message.from_user.id
    username = message.from_user.username or ''
    first_name = message.from_user.first_name or ''

    # Регистрируем пользователя если не существует
    if not is_superadmin(user_id) and not is_admin(user_id):
        cursor.execute(
            'INSERT OR IGNORE INTO bot_users (user_id, username, first_name, status, joined_at) VALUES (?, ?, ?, ?, ?)',
            (user_id, username, first_name, 'pending', datetime.now().isoformat())
        )
        conn.commit()

    status = get_user_status_label(user_id)

    if is_superadmin(user_id) or is_admin(user_id):
        text = (
            f"👋 Добро пожаловать, <b>{first_name}</b>!\n\n"
            f"Статус: {status}\n\n"
            f"Вы имеете полный доступ к управлению ботом."
        )
        bot.send_message(user_id, text, parse_mode='HTML', reply_markup=build_start_keyboard(user_id))
    elif is_approved(user_id):
        text = (
            f"👋 Привет, <b>{first_name}</b>!\n\n"
            f"Статус: {status}\n\n"
            f"Вам открыт доступ к боту."
        )
        bot.send_message(user_id, text, parse_mode='HTML', reply_markup=build_start_keyboard(user_id))
    else:
        # Уведомляем суперадмина о новой заявке
        row = cursor.execute('SELECT status FROM bot_users WHERE user_id=?', (user_id,)).fetchone()
        if row and row[0] == 'pending':
            notify_admins_about_request(user_id, username, first_name)

        text = (
            f"👋 Привет, <b>{first_name}</b>!\n\n"
            f"⏳ Ваша заявка на доступ отправлена администратору.\n"
            f"Ожидайте одобрения."
        )
        bot.send_message(user_id, text, parse_mode='HTML')


def notify_admins_about_request(user_id, username, first_name):
    text = (
        f"🔔 <b>Новая заявка на доступ</b>\n\n"
        f"👤 Имя: <b>{first_name}</b>\n"
        f"🔗 Username: {'@' + username if username else 'нет'}\n"
        f"🆔 ID: <code>{user_id}</code>\n\n"
        f"Хотите предоставить доступ?"
    )
    kb = tb_types.InlineKeyboardMarkup()
    kb.add(
        tb_types.InlineKeyboardButton('✅ Разрешить', callback_data=f'approve_{user_id}'),
        tb_types.InlineKeyboardButton('🚫 Запретить', callback_data=f'ban_{user_id}'),
    )
    # Уведомляем всех админов
    cursor.execute('SELECT user_id FROM admins')
    for (aid,) in cursor.fetchall():
        try:
            bot.send_message(aid, text, parse_mode='HTML', reply_markup=kb)
        except Exception as e:
            print(f"Notify admin {aid} error: {e}")


@bot.callback_query_handler(func=lambda c: True)
def callback_handler(call):
    user_id = call.from_user.id
    data = call.data

    # --- Кнопки только для админов ---
    if data == 'admin_panel':
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, '⛔ Нет доступа')
            return
        bot.edit_message_text(
            '🛡 <b>Панель администратора</b>\n\nВыберите раздел:',
            call.message.chat.id, call.message.message_id,
            parse_mode='HTML', reply_markup=build_admin_panel_keyboard()
        )

    elif data == 'back_start':
        first_name = call.from_user.first_name or ''
        status = get_user_status_label(user_id)
        text = f"👋 Добро пожаловать, <b>{first_name}</b>!\n\nСтатус: {status}"
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              parse_mode='HTML', reply_markup=build_start_keyboard(user_id))

    elif data == 'admin_pending':
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, '⛔ Нет доступа')
            return
        cursor.execute('SELECT user_id, username, first_name, joined_at FROM bot_users WHERE status=?', ('pending',))
        rows = cursor.fetchall()
        if not rows:
            bot.answer_callback_query(call.id, 'Заявок нет')
            kb = tb_types.InlineKeyboardMarkup()
            kb.add(tb_types.InlineKeyboardButton('🔙 Назад', callback_data='admin_panel'))
            bot.edit_message_text('📭 Нет ожидающих заявок.', call.message.chat.id,
                                  call.message.message_id, reply_markup=kb)
            return
        text = '👥 <b>Ожидающие заявки:</b>\n\n'
        kb = tb_types.InlineKeyboardMarkup(row_width=2)
        for uid, uname, fname, joined in rows:
            uname_display = f'@{uname}' if uname else 'нет'
            text += f"👤 <b>{fname}</b> ({uname_display})\n🆔 <code>{uid}</code>\n\n"
            kb.add(
                tb_types.InlineKeyboardButton(f'✅ {fname}', callback_data=f'approve_{uid}'),
                tb_types.InlineKeyboardButton(f'🚫 {fname}', callback_data=f'ban_{uid}'),
            )
        kb.add(tb_types.InlineKeyboardButton('🔙 Назад', callback_data='admin_panel'))
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              parse_mode='HTML', reply_markup=kb)

    elif data == 'admin_approved':
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, '⛔ Нет доступа')
            return
        cursor.execute('SELECT user_id, username, first_name FROM bot_users WHERE status=?', ('approved',))
        rows = cursor.fetchall()
        kb = tb_types.InlineKeyboardMarkup()
        kb.add(tb_types.InlineKeyboardButton('🔙 Назад', callback_data='admin_panel'))
        if not rows:
            bot.edit_message_text('📭 Нет разрешённых пользователей.', call.message.chat.id,
                                  call.message.message_id, reply_markup=kb)
            return
        text = '✅ <b>Разрешённые пользователи:</b>\n\n'
        for uid, uname, fname in rows:
            uname_display = f'@{uname}' if uname else 'нет'
            text += f"👤 <b>{fname}</b> ({uname_display}) — <code>{uid}</code>\n"
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              parse_mode='HTML', reply_markup=kb)

    elif data == 'admin_banned':
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, '⛔ Нет доступа')
            return
        cursor.execute('SELECT user_id, username, first_name FROM bot_users WHERE status=?', ('banned',))
        rows = cursor.fetchall()
        kb = tb_types.InlineKeyboardMarkup(row_width=1)
        if rows:
            for uid, uname, fname in rows:
                kb.add(tb_types.InlineKeyboardButton(f'♻️ Разблокировать {fname}', callback_data=f'unban_{uid}'))
        kb.add(tb_types.InlineKeyboardButton('🔙 Назад', callback_data='admin_panel'))
        if not rows:
            bot.edit_message_text('📭 Нет заблокированных.', call.message.chat.id,
                                  call.message.message_id, reply_markup=kb)
            return
        text = '🚫 <b>Заблокированные пользователи:</b>\n\n'
        for uid, uname, fname in rows:
            uname_display = f'@{uname}' if uname else 'нет'
            text += f"👤 <b>{fname}</b> ({uname_display}) — <code>{uid}</code>\n"
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              parse_mode='HTML', reply_markup=kb)

    elif data == 'admin_list':
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, '⛔ Нет доступа')
            return
        cursor.execute('SELECT user_id, username, first_name, added_at FROM admins')
        rows = cursor.fetchall()
        text = '👑 <b>Список администраторов:</b>\n\n'
        kb = tb_types.InlineKeyboardMarkup(row_width=1)
        for uid, uname, fname, added in rows:
            label = '👑 Суперадмин' if uid == SUPERADMIN_ID else '🛡 Админ'
            uname_display = f'@{uname}' if uname else 'нет'
            text += f"{label} <b>{fname}</b> ({uname_display})\n🆔 <code>{uid}</code>\n\n"
            if uid != SUPERADMIN_ID and is_superadmin(user_id):
                kb.add(tb_types.InlineKeyboardButton(f'❌ Снять {fname}', callback_data=f'removeadmin_{uid}'))
        if is_superadmin(user_id):
            kb.add(tb_types.InlineKeyboardButton('➕ Добавить админа по ID', callback_data='add_admin_prompt'))
        kb.add(tb_types.InlineKeyboardButton('🔙 Назад', callback_data='admin_panel'))
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              parse_mode='HTML', reply_markup=kb)

    elif data.startswith('approve_'):
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, '⛔ Нет доступа')
            return
        target_id = int(data.split('_')[1])
        cursor.execute('UPDATE bot_users SET status=? WHERE user_id=?', ('approved', target_id))
        conn.commit()
        bot.answer_callback_query(call.id, '✅ Доступ предоставлен')
        try:
            bot.send_message(target_id,
                             '✅ <b>Ваша заявка одобрена!</b>\nТеперь вы можете пользоваться ботом. Нажмите /start',
                             parse_mode='HTML')
        except Exception:
            pass
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        bot.send_message(call.message.chat.id, f'✅ Пользователь <code>{target_id}</code> одобрен.',
                         parse_mode='HTML')

    elif data.startswith('ban_'):
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, '⛔ Нет доступа')
            return
        target_id = int(data.split('_')[1])
        cursor.execute('UPDATE bot_users SET status=? WHERE user_id=?', ('banned', target_id))
        conn.commit()
        bot.answer_callback_query(call.id, '🚫 Доступ запрещён')
        try:
            bot.send_message(target_id, '🚫 Ваша заявка отклонена администратором.', parse_mode='HTML')
        except Exception:
            pass
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        bot.send_message(call.message.chat.id, f'🚫 Пользователь <code>{target_id}</code> заблокирован.',
                         parse_mode='HTML')

    elif data.startswith('unban_'):
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, '⛔ Нет доступа')
            return
        target_id = int(data.split('_')[1])
        cursor.execute('UPDATE bot_users SET status=? WHERE user_id=?', ('pending', target_id))
        conn.commit()
        bot.answer_callback_query(call.id, '♻️ Разблокирован')
        bot.send_message(call.message.chat.id, f'♻️ Пользователь <code>{target_id}</code> разблокирован.',
                         parse_mode='HTML')

    elif data.startswith('removeadmin_'):
        if not is_superadmin(user_id):
            bot.answer_callback_query(call.id, '⛔ Только суперадмин')
            return
        target_id = int(data.split('_')[1])
        cursor.execute('DELETE FROM admins WHERE user_id=?', (target_id,))
        conn.commit()
        bot.answer_callback_query(call.id, '❌ Админ снят')
        bot.send_message(call.message.chat.id, f'❌ Пользователь <code>{target_id}</code> снят с должности админа.',
                         parse_mode='HTML')

    elif data == 'add_admin_prompt':
        if not is_superadmin(user_id):
            bot.answer_callback_query(call.id, '⛔ Только суперадмин')
            return
        bot.answer_callback_query(call.id)
        msg = bot.send_message(call.message.chat.id,
                               '✏️ Введите Telegram ID пользователя которого хотите сделать админом:')
        bot.register_next_step_handler(msg, process_add_admin)

    elif data == 'my_commands':
        if not is_approved(user_id):
            bot.answer_callback_query(call.id, '⛔ Нет доступа')
            return
        text = build_help_text(user_id)
        kb = tb_types.InlineKeyboardMarkup()
        kb.add(tb_types.InlineKeyboardButton('🔙 Назад', callback_data='back_start'))
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              parse_mode='HTML', reply_markup=kb)

    elif data == 'about':
        kb = tb_types.InlineKeyboardMarkup()
        kb.add(tb_types.InlineKeyboardButton('🔙 Назад', callback_data='back_start'))
        bot.edit_message_text(
            '🤖 <b>UserBot</b>\n\nУмный юзербот с расширенным функционалом.\n\n'
            '⚡ Отслеживание удалений и редактирований\n'
            '🔔 Напоминания\n'
            '😴 AFK режим\n'
            '🎲 Случайные факты\n'
            '🔢 Калькулятор\n'
            '🎭 И многое другое',
            call.message.chat.id, call.message.message_id,
            parse_mode='HTML', reply_markup=kb
        )

    bot.answer_callback_query(call.id)


def process_add_admin(message):
    if not is_superadmin(message.from_user.id):
        return
    try:
        target_id = int(message.text.strip())
        # Проверяем что уже не админ
        if is_admin(target_id):
            bot.send_message(message.chat.id, f'ℹ️ Пользователь <code>{target_id}</code> уже является админом.',
                             parse_mode='HTML')
            return
        # Получаем данные из bot_users если есть
        row = cursor.execute('SELECT username, first_name FROM bot_users WHERE user_id=?', (target_id,)).fetchone()
        username = row[0] if row else ''
        first_name = row[1] if row else str(target_id)
        cursor.execute('INSERT OR REPLACE INTO admins (user_id, username, first_name, added_at) VALUES (?, ?, ?, ?)',
                       (target_id, username, first_name, datetime.now().isoformat()))
        conn.commit()
        bot.send_message(message.chat.id, f'✅ Пользователь <code>{target_id}</code> назначен администратором.',
                         parse_mode='HTML')
        try:
            bot.send_message(target_id,
                             '🛡 Вы назначены <b>администратором</b> бота!\nНажмите /start для доступа к панели.',
                             parse_mode='HTML')
        except Exception:
            pass
    except ValueError:
        bot.send_message(message.chat.id, '❌ Неверный формат ID. Введите числовой Telegram ID.')


# === КОМАНДЫ БОТА ДЛЯ РАЗРЕШЁННЫХ ПОЛЬЗОВАТЕЛЕЙ ===

@bot.message_handler(commands=['remind'])
def remind_handler(message):
    if not is_approved(message.from_user.id):
        bot.reply_to(message, '⛔ У вас нет доступа.')
        return
    # Формат: /remind 10m текст напоминания
    try:
        parts = message.text.split(' ', 2)
        if len(parts) < 3:
            bot.reply_to(message, '💬 Использование: /remind 10m текст\nФорматы времени: 10m, 2h, 1d')
            return
        time_str = parts[1].lower()
        text = parts[2]

        if time_str.endswith('m'):
            delta = timedelta(minutes=int(time_str[:-1]))
        elif time_str.endswith('h'):
            delta = timedelta(hours=int(time_str[:-1]))
        elif time_str.endswith('d'):
            delta = timedelta(days=int(time_str[:-1]))
        else:
            bot.reply_to(message, '❌ Неверный формат времени. Используйте: 10m, 2h, 1d')
            return

        remind_at = (datetime.now() + delta).isoformat()
        cursor.execute('INSERT INTO reminders (user_id, text, remind_at) VALUES (?, ?, ?)',
                       (message.from_user.id, text, remind_at))
        conn.commit()

        time_label = str(time_str)
        bot.reply_to(message, f'⏰ Напоминание установлено через <b>{time_label}</b>!\n\n<blockquote>{text}</blockquote>',
                     parse_mode='HTML')
    except Exception as e:
        bot.reply_to(message, f'❌ Ошибка: {e}')


@bot.message_handler(commands=['myreminders'])
def my_reminders_handler(message):
    if not is_approved(message.from_user.id):
        bot.reply_to(message, '⛔ У вас нет доступа.')
        return
    cursor.execute('SELECT id, text, remind_at FROM reminders WHERE user_id=? ORDER BY remind_at', (message.from_user.id,))
    rows = cursor.fetchall()
    if not rows:
        bot.reply_to(message, '📭 У вас нет активных напоминаний.')
        return
    text = '⏰ <b>Ваши напоминания:</b>\n\n'
    for rid, rtext, rat in rows:
        try:
            dt = datetime.fromisoformat(rat).strftime('%d.%m.%Y %H:%M')
        except Exception:
            dt = rat
        text += f"🔔 <code>#{rid}</code> — {dt}\n<blockquote>{rtext}</blockquote>\n\n"
    bot.reply_to(message, text, parse_mode='HTML')


@bot.message_handler(commands=['calc'])
def calc_handler(message):
    if not is_approved(message.from_user.id):
        bot.reply_to(message, '⛔ У вас нет доступа.')
        return
    try:
        expr = message.text[6:].strip()
        if not expr:
            bot.reply_to(message, '💬 Использование: /calc 2+2*3')
            return
        # Безопасный eval — только цифры и операторы
        allowed = set('0123456789+-*/()., ')
        if not all(c in allowed for c in expr):
            bot.reply_to(message, '❌ Недопустимые символы в выражении.')
            return
        result = eval(expr)
        bot.reply_to(message, f'🔢 <code>{expr}</code> = <b>{result}</b>', parse_mode='HTML')
    except Exception:
        bot.reply_to(message, '❌ Ошибка вычисления.')


@bot.message_handler(commands=['fact'])
def fact_handler(message):
    if not is_approved(message.from_user.id):
        bot.reply_to(message, '⛔ У вас нет доступа.')
        return
    facts = [
        '🧠 Осьминоги имеют три сердца и голубую кровь.',
        '🌍 На Земле больше деревьев, чем звёзд в Млечном пути.',
        '⚡ Молния бьёт в Землю около 100 раз в секунду.',
        '🐬 Дельфины спят с одним открытым глазом.',
        '🍯 Мёд никогда не портится — его находили в египетских гробницах.',
        '🦋 Бабочки пробуют вкус еды ногами.',
        '🌙 На Луне нет ветра, поэтому следы астронавтов сохранятся тысячи лет.',
        '🐘 Слоны — единственные животные, которые не могут прыгать.',
        '🦈 Акулы старше деревьев — они появились 400 млн лет назад.',
        '🔬 В теле человека больше бактерий, чем клеток.',
        '🌊 95% океанов до сих пор не исследованы.',
        '🧬 ДНК человека на 98.7% совпадает с ДНК шимпанзе.',
        '🐙 У осьминога нет костей, он может пролезть в любое отверстие размером с его клюв.',
        '🌡 Самая высокая температура во вселенной была достигнута в Большом адронном коллайдере.',
        '🦜 Попугаи — единственные птицы, которые едят лапами.',
    ]
    bot.reply_to(message, random.choice(facts), parse_mode='HTML')


@bot.message_handler(commands=['coin'])
def coin_handler(message):
    if not is_approved(message.from_user.id):
        bot.reply_to(message, '⛔ У вас нет доступа.')
        return
    result = random.choice(['🪙 Орёл', '🪙 Решка'])
    bot.reply_to(message, f'Монетка: <b>{result}</b>', parse_mode='HTML')


@bot.message_handler(commands=['dice'])
def dice_handler(message):
    if not is_approved(message.from_user.id):
        bot.reply_to(message, '⛔ У вас нет доступа.')
        return
    parts = message.text.split()
    sides = 6
    if len(parts) > 1:
        try:
            sides = min(int(parts[1]), 100)
        except ValueError:
            pass
    result = random.randint(1, sides)
    bot.reply_to(message, f'🎲 Бросок кубика d{sides}: <b>{result}</b>', parse_mode='HTML')


@bot.message_handler(commands=['id'])
def id_handler(message):
    if not is_approved(message.from_user.id):
        bot.reply_to(message, '⛔ У вас нет доступа.')
        return
    uid = message.from_user.id
    uname = f'@{message.from_user.username}' if message.from_user.username else 'нет'
    fname = message.from_user.first_name or ''
    text = (
        f'<blockquote>👤 Ваши данные:\n'
        f'├ 🆔 ID: <code>{uid}</code>\n'
        f'├ ✈️ Username: <b>{uname}</b>\n'
        f'└ 📛 Имя: <b>{fname}</b></blockquote>'
    )
    bot.reply_to(message, text, parse_mode='HTML')


@bot.message_handler(commands=['help'])
def help_bot_handler(message):
    if not is_approved(message.from_user.id):
        bot.reply_to(message, '⛔ У вас нет доступа. Отправьте /start для подачи заявки.')
        return
    text = build_help_text(message.from_user.id)
    bot.reply_to(message, text, parse_mode='HTML')


def build_help_text(user_id: int) -> str:
    text = '<b>📋 Доступные команды бота:</b>\n\n'
    text += (
        '<blockquote>'
        '⏰ /remind 10m текст — напоминание\n'
        '📋 /myreminders — мои напоминания\n'
        '🔢 /calc 2+2 — калькулятор\n'
        '🧠 /fact — случайный факт\n'
        '🪙 /coin — подбросить монетку\n'
        '🎲 /dice [стороны] — бросить кубик\n'
        '🆔 /id — ваш Telegram ID\n'
        '❓ /help — эта справка\n'
        '</blockquote>'
    )
    if is_admin(user_id):
        text += (
            '\n<b>🛡 Команды администратора:</b>\n\n'
            '<blockquote>'
            '/addadmin [ID] — добавить админа\n'
            '/removeadmin [ID] — снять админа\n'
            '/users — список всех пользователей\n'
            '/broadcast текст — рассылка всем\n'
            '</blockquote>'
        )
    return text


@bot.message_handler(commands=['addadmin'])
def addadmin_bot_handler(message):
    if not is_superadmin(message.from_user.id):
        bot.reply_to(message, '⛔ Только суперадмин.')
        return
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, '💬 Использование: /addadmin [ID]')
        return
    try:
        target_id = int(parts[1])
        if is_admin(target_id):
            bot.reply_to(message, 'ℹ️ Уже является админом.')
            return
        row = cursor.execute('SELECT username, first_name FROM bot_users WHERE user_id=?', (target_id,)).fetchone()
        username = row[0] if row else ''
        first_name = row[1] if row else str(target_id)
        cursor.execute('INSERT OR REPLACE INTO admins (user_id, username, first_name, added_at) VALUES (?, ?, ?, ?)',
                       (target_id, username, first_name, datetime.now().isoformat()))
        conn.commit()
        bot.reply_to(message, f'✅ <code>{target_id}</code> назначен администратором.', parse_mode='HTML')
        try:
            bot.send_message(target_id, '🛡 Вы назначены <b>администратором</b> бота! Нажмите /start',
                             parse_mode='HTML')
        except Exception:
            pass
    except ValueError:
        bot.reply_to(message, '❌ Неверный ID.')


@bot.message_handler(commands=['removeadmin'])
def removeadmin_bot_handler(message):
    if not is_superadmin(message.from_user.id):
        bot.reply_to(message, '⛔ Только суперадмин.')
        return
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, '💬 Использование: /removeadmin [ID]')
        return
    try:
        target_id = int(parts[1])
        if target_id == SUPERADMIN_ID:
            bot.reply_to(message, '⛔ Нельзя снять суперадмина.')
            return
        cursor.execute('DELETE FROM admins WHERE user_id=?', (target_id,))
        conn.commit()
        bot.reply_to(message, f'❌ <code>{target_id}</code> снят с должности.', parse_mode='HTML')
    except ValueError:
        bot.reply_to(message, '❌ Неверный ID.')


@bot.message_handler(commands=['users'])
def users_handler(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, '⛔ Нет доступа.')
        return
    cursor.execute('SELECT user_id, username, first_name, status FROM bot_users')
    rows = cursor.fetchall()
    if not rows:
        bot.reply_to(message, '📭 Нет пользователей.')
        return
    text = '👥 <b>Все пользователи:</b>\n\n'
    status_icons = {'approved': '✅', 'pending': '⏳', 'banned': '🚫'}
    for uid, uname, fname, status in rows:
        icon = status_icons.get(status, '❓')
        uname_display = f'@{uname}' if uname else 'нет'
        text += f"{icon} <b>{fname}</b> ({uname_display}) — <code>{uid}</code>\n"
    bot.reply_to(message, text, parse_mode='HTML')


@bot.message_handler(commands=['broadcast'])
def broadcast_handler(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, '⛔ Нет доступа.')
        return
    text = message.text[11:].strip()
    if not text:
        bot.reply_to(message, '💬 Использование: /broadcast текст сообщения')
        return
    cursor.execute('SELECT user_id FROM bot_users WHERE status=?', ('approved',))
    rows = cursor.fetchall()
    sent = 0
    for (uid,) in rows:
        try:
            bot.send_message(uid, f'📢 <b>Сообщение от администратора:</b>\n\n{text}', parse_mode='HTML')
            sent += 1
        except Exception:
            pass
    bot.reply_to(message, f'✅ Рассылка отправлена {sent} пользователям.')


# =====================================================
# === USERBOT КОМАНДЫ (через Telethon) ===
# =====================================================

async def deleted_handler(event):
    if not await check_is_owner(event):
        return
    try:
        parts = event.message.text.split()
        limit = 5
        if len(parts) > 1:
            try:
                limit = min(int(parts[1]), 20)
            except ValueError:
                pass

        cursor.execute(
            'SELECT user_id, chat_id, text, media_path, media_type, original_date, deleted_at '
            'FROM deleted_messages ORDER BY id DESC LIMIT ?',
            (limit,)
        )
        rows = cursor.fetchall()

        if not rows:
            await event.edit('📭 Нет сохранённых удалённых сообщений.')
            return

        await event.delete()

        for row in reversed(rows):
            user_id, chat_id, text, media_path, media_type, orig_date, deleted_at = row
            username, name, _ = await get_user_info(user_id)
            link = f"https://t.me/{username}" if username else f"tg://user?id={user_id}"

            try:
                orig_dt = datetime.fromisoformat(orig_date).strftime('%d.%m.%Y %H:%M')
            except Exception:
                orig_dt = orig_date or '?'

            try:
                del_dt = datetime.fromisoformat(deleted_at).strftime('%d.%m.%Y %H:%M')
            except Exception:
                del_dt = deleted_at or '?'

            if chat_id != user_id:
                chat_title = await get_chat_title(chat_id)
                source = f" из <b>{chat_title}</b>"
            else:
                source = ""

            media_label = ""
            if media_type:
                labels = {'photo': '🖼 Фото', 'video': '🎬 Видео', 'voice': '🎤 Голосовое',
                          'audio': '🎵 Аудио', 'sticker': '🎭 Стикер', 'document': '📎 Документ'}
                media_label = f"\n{labels.get(media_type, '📎 Медиа')}"

            caption = (
                f"🗑 Удалённое{source}\n"
                f"<a href=\"{link}\">{name}</a>{media_label}\n"
                f"📅 {orig_dt} → 🗑 {del_dt}\n\n"
                f"<blockquote>{text or '(нет текста)'}</blockquote>"
            )

            await send_bot_message(caption, media_path, media_type)

    except Exception as e:
        print(f"Deleted handler error: {e}")
        await event.edit('❌ Ошибка при получении удалённых сообщений.')


async def mute_handler(event):
    global muted_users
    if not await check_is_owner(event):
        return
    try:
        reply_msg = await event.get_reply_message()
        if reply_msg and hasattr(reply_msg, 'sender_id') and reply_msg.sender_id:
            user_id = reply_msg.sender_id
            cursor.execute('SELECT user_id FROM muted_users WHERE user_id=?', (user_id,))
            if cursor.fetchone():
                await event.delete()
                return
            cursor.execute('INSERT OR IGNORE INTO muted_users (user_id) VALUES (?)', (user_id,))
            conn.commit()
            muted_users.add(user_id)
            await event.edit('🔕 Помолчи.')
        else:
            await event.edit('💬 Использование: .mute (в ответ на сообщение)')
    except Exception as e:
        print(f"Mute error: {e}")
        await event.edit('💬 Использование: .mute (в ответ на сообщение)')


async def unmute_handler(event):
    global muted_users
    if not await check_is_owner(event):
        return
    try:
        reply_msg = await event.get_reply_message()
        if reply_msg and hasattr(reply_msg, 'sender_id') and reply_msg.sender_id:
            user_id = reply_msg.sender_id
            cursor.execute('SELECT user_id FROM muted_users WHERE user_id=?', (user_id,))
            if not cursor.fetchone():
                await event.delete()
                return
            cursor.execute('DELETE FROM muted_users WHERE user_id=?', (user_id,))
            conn.commit()
            muted_users.discard(user_id)
            await event.edit('🔔 Говори.')
        else:
            await event.edit('💬 Использование: .unmute (в ответ на сообщение)')
    except Exception as e:
        print(f"Unmute error: {e}")
        await event.edit('💬 Использование: .unmute (в ответ на сообщение)')


async def incoming_message_handler(event):
    try:
        peer = event.message.peer_id
        sender_id = event.message.sender_id
        if not sender_id:
            return

        # AFK ответ
        if owner_id and sender_id != owner_id:
            cursor.execute('SELECT reason, since FROM afk WHERE user_id=?', (owner_id,))
            afk_row = cursor.fetchone()
            if afk_row:
                reason, since = afk_row
                try:
                    since_dt = datetime.fromisoformat(since).strftime('%H:%M')
                except Exception:
                    since_dt = since
                afk_text = f'😴 Я сейчас AFK (с {since_dt})'
                if reason:
                    afk_text += f'\nПричина: {reason}'
                await event.reply(afk_text)

        if isinstance(peer, PeerUser):
            cursor.execute('SELECT user_id FROM muted_users WHERE user_id=?', (sender_id,))
            if cursor.fetchone():
                await event.delete()
                print(f"Deleted message from muted user {sender_id}")
                return

        await store_message(event.message)
    except Exception as e:
        print(f"Incoming message handler error: {e}")


async def outgoing_message_handler(event):
    try:
        # Если владелец написал — снимаем AFK
        if owner_id and event.message.sender_id == owner_id:
            cursor.execute('DELETE FROM afk WHERE user_id=?', (owner_id,))
            conn.commit()
        await store_message(event.message)
    except Exception as e:
        print(f"Outgoing message handler error: {e}")


async def handler_message_edited(event):
    await process_edited_message(event)


async def type_handler(event):
    if not await check_is_owner(event):
        return
    try:
        text = event.message.text[6:]
        if not text:
            await event.edit('💬 Использование: .type [текст]')
            return
        await event.edit(".")
        typed = ""
        for char in text:
            typed += char
            try:
                await event.edit(typed)
            except Exception:
                pass
            await asyncio.sleep(0.5)
    except Exception as e:
        print(f"Type error: {e}")
        await event.edit('💬 Использование: .type [текст]')


async def spam_handler(event):
    if not await check_is_owner(event):
        return
    try:
        parts = event.message.text.split(' ', 2)
        if len(parts) < 2:
            await event.edit('💬 Использование: .spam [кол-во] [текст или реплай]')
            return
        try:
            count = int(parts[1])
        except ValueError:
            await event.edit('💬 Использование: .spam [кол-во] [текст или реплай]')
            return
        if count > 20:
            count = 20
        reply_msg = await event.get_reply_message()
        if not reply_msg and len(parts) < 3:
            await event.edit('💬 Использование: .spam [кол-во] [текст или реплай]')
            return
        await event.delete()
        for _ in range(count):
            if reply_msg:
                await client.send_message(event.chat_id, message=reply_msg)
            elif len(parts) > 2:
                await client.send_message(event.chat_id, parts[2])
            await asyncio.sleep(0.3)
    except Exception as e:
        print(f"Spam error: {e}")


async def info_handler(event):
    if not await check_is_owner(event):
        return
    try:
        reply_msg = await event.get_reply_message()
        if reply_msg and hasattr(reply_msg, 'sender_id') and reply_msg.sender_id:
            user_id = reply_msg.sender_id
            username, name, _ = await get_user_info(user_id)
            username_display = f"@{username}" if username else "Нет"
            info_text = (
                f"<blockquote>Metadata:\n"
                f"├ 👤 ID: <b>{user_id}</b>\n"
                f"├ ✈️ Username: <b>{username_display}</b>\n"
                f"└ 👁 Full Name: <b>{name or 'Неизвестно'}</b></blockquote>"
            )
            await event.edit(info_text, parse_mode='HTML')
        else:
            await event.edit('Ответьте на сообщение!')
    except Exception as e:
        print(f"Info error: {e}")
        await event.edit('Ответьте на сообщение!')


async def afk_handler(event):
    if not await check_is_owner(event):
        return
    try:
        parts = event.message.text.split(' ', 1)
        reason = parts[1].strip() if len(parts) > 1 else ''
        cursor.execute('INSERT OR REPLACE INTO afk (user_id, reason, since) VALUES (?, ?, ?)',
                       (owner_id, reason, datetime.now().isoformat()))
        conn.commit()
        msg = '😴 AFK режим включён.'
        if reason:
            msg += f'\nПричина: {reason}'
        await event.edit(msg)
    except Exception as e:
        print(f"AFK error: {e}")


async def unafk_handler(event):
    if not await check_is_owner(event):
        return
    try:
        cursor.execute('DELETE FROM afk WHERE user_id=?', (owner_id,))
        conn.commit()
        await event.edit('✅ AFK режим выключен.')
    except Exception as e:
        print(f"UnAFK error: {e}")


async def ping_handler(event):
    if not await check_is_owner(event):
        return
    start = datetime.now()
    await event.edit('🏓 Pong!')
    delta = (datetime.now() - start).microseconds // 1000
    await event.edit(f'🏓 Pong! <code>{delta}ms</code>', parse_mode='HTML')


async def stats_handler(event):
    if not await check_is_owner(event):
        return
    try:
        total_msgs = cursor.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
        total_deleted = cursor.execute('SELECT COUNT(*) FROM deleted_messages').fetchone()[0]
        total_muted = cursor.execute('SELECT COUNT(*) FROM muted_users').fetchone()[0]
        total_users = cursor.execute('SELECT COUNT(*) FROM bot_users').fetchone()[0]
        total_admins = cursor.execute('SELECT COUNT(*) FROM admins').fetchone()[0]

        text = (
            f"<blockquote>📊 Статистика UserBot\n\n"
            f"├ 💬 Сообщений в БД: <b>{total_msgs}</b>\n"
            f"├ 🗑 Удалённых: <b>{total_deleted}</b>\n"
            f"├ 🔕 Замьючено: <b>{total_muted}</b>\n"
            f"├ 👥 Пользователей бота: <b>{total_users}</b>\n"
            f"└ 🛡 Администраторов: <b>{total_admins}</b></blockquote>"
        )
        await event.edit(text, parse_mode='HTML')
    except Exception as e:
        print(f"Stats error: {e}")
        await event.edit('❌ Ошибка получения статистики.')


async def clear_deleted_handler(event):
    """Очищает базу удалённых сообщений."""
    if not await check_is_owner(event):
        return
    try:
        cursor.execute('DELETE FROM deleted_messages')
        conn.commit()
        await event.edit('🧹 База удалённых сообщений очищена.')
    except Exception as e:
        print(f"Clear deleted error: {e}")


HELP_TEXT = """<b>📝 Команды UserBot</b>

<blockquote>▫️ .help — Справка
▫️ .mute / .unmute — Замьютить пользователя
▫️ .spam [N] [текст] — Спам
▫️ .type [текст] — Анимация печати
▫️ .info — Инфо о пользователе
▫️ .deleted [N] — Последние N удалённых
▫️ .afk [причина] — Включить AFK режим
▫️ .unafk — Выключить AFK режим
▫️ .ping — Проверить задержку
▫️ .stats — Статистика бота
▫️ .clear — Очистить базу удалённых</blockquote>"""


async def help_handler(event):
    if not await check_is_owner(event):
        return
    args = event.message.text.split(' ', 1)
    if len(args) > 1:
        command = args[1].strip()
        helps = {
            'type': '⚙️ Typer\n\n· .type [текст] — Анимация печати текста',
            'spam': '⚙️ Spam\n\n· .spam [кол-во] [текст или реплай] — Спам (макс 20)',
            'mute': '⚙️ Mute\n\n· .mute — Замьютить (реплай)\n· .unmute — Размьютить (реплай)',
            'info': '⚙️ UserInfo\n\n· .info — Информация (реплай)',
            'deleted': '⚙️ Deleted\n\n· .deleted [N] — Последние N удалённых (макс 20)',
            'afk': '⚙️ AFK\n\n· .afk [причина] — Включить AFK\n· .unafk — Выключить AFK',
            'stats': '⚙️ Stats\n\n· .stats — Статистика бота',
        }
        await event.edit(helps.get(command, HELP_TEXT), parse_mode='HTML')
    else:
        await event.edit(HELP_TEXT, parse_mode='HTML')


# === ФОНОВЫЕ ЗАДАЧИ ===

async def reminder_checker():
    """Проверяет и отправляет напоминания."""
    while True:
        try:
            now = datetime.now().isoformat()
            cursor.execute('SELECT id, user_id, text FROM reminders WHERE remind_at <= ?', (now,))
            rows = cursor.fetchall()
            for rid, uid, text in rows:
                try:
                    bot.send_message(uid, f'🔔 <b>Напоминание!</b>\n\n<blockquote>{text}</blockquote>',
                                     parse_mode='HTML')
                except Exception as e:
                    print(f"Reminder send error: {e}")
                cursor.execute('DELETE FROM reminders WHERE id=?', (rid,))
            if rows:
                conn.commit()
        except Exception as e:
            print(f"Reminder checker error: {e}")
        await asyncio.sleep(30)


# === ЗАГРУЗКА / СЛУЖЕБНОЕ ===

async def load_muted_users():
    global muted_users
    cursor.execute('SELECT user_id FROM muted_users')
    rows = cursor.fetchall()
    muted_users = {row[0] for row in rows}


def run_bot():
    import time
    while True:
        try:
            bot.polling(none_stop=True, timeout=30)
        except Exception as e:
            print(f"Bot polling error: {e}")
            time.sleep(5)


def register_handlers(c):
    c.add_event_handler(raw_deleted_handler, events.Raw(types.UpdateDeleteMessages))
    c.add_event_handler(raw_deleted_handler, events.Raw(types.UpdateDeleteChannelMessages))

    c.add_event_handler(mute_handler, events.NewMessage(outgoing=True, pattern=r'^\.mute$'))
    c.add_event_handler(unmute_handler, events.NewMessage(outgoing=True, pattern=r'^\.unmute$'))

    c.add_event_handler(incoming_message_handler, events.NewMessage(incoming=True))
    c.add_event_handler(outgoing_message_handler, events.NewMessage(outgoing=True))

    c.add_event_handler(type_handler, events.NewMessage(outgoing=True, pattern=r'^\.type '))
    c.add_event_handler(spam_handler, events.NewMessage(outgoing=True, pattern=r'^\.spam '))
    c.add_event_handler(info_handler, events.NewMessage(outgoing=True, pattern=r'^\.info$'))
    c.add_event_handler(deleted_handler, events.NewMessage(outgoing=True, pattern=r'^\.deleted( \d+)?$'))
    c.add_event_handler(help_handler, events.NewMessage(outgoing=True, pattern=r'^\.help( .*)?$'))
    c.add_event_handler(afk_handler, events.NewMessage(outgoing=True, pattern=r'^\.afk( .*)?$'))
    c.add_event_handler(unafk_handler, events.NewMessage(outgoing=True, pattern=r'^\.unafk$'))
    c.add_event_handler(ping_handler, events.NewMessage(outgoing=True, pattern=r'^\.ping$'))
    c.add_event_handler(stats_handler, events.NewMessage(outgoing=True, pattern=r'^\.stats$'))
    c.add_event_handler(clear_deleted_handler, events.NewMessage(outgoing=True, pattern=r'^\.clear$'))

    c.add_event_handler(handler_message_edited, events.MessageEdited)


async def main():
    global client, owner_id
    client = TelegramClient(StringSession(session_string), api_id, api_hash)
    register_handlers(client)

    await client.start()
    owner_id = (await client.get_me()).id
    await load_muted_users()

    print(f"UB started! User ID: {owner_id}")
    print(f"Muted users loaded: {len(muted_users)}")
    print(f"Media saved to: {os.path.abspath(MEDIA_DIR)}")
    print(f"SuperAdmin: {SUPERADMIN_ID}")

    Thread(target=run_bot, daemon=True).start()

    # Запускаем фоновую задачу напоминаний
    asyncio.create_task(reminder_checker())

    await client.run_until_disconnected()


if __name__ == '__main__':
    asyncio.run(main())
