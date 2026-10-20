import asyncio
import ast
import html
import io
import sqlite3
import os
import json
import math
import random
import re
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from threading import Thread

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl import types
from telethon.tl.types import PeerUser, PeerChat, PeerChannel, MessageMediaPhoto, MessageMediaDocument
import telebot
from telebot import types as tb_types
import qrcode
from PIL import Image, ImageFilter, ImageOps, ExifTags

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

    elif data.startswith('ttt_') and data != 'ttt_noop':
        handle_ttt_callback(call)
        return

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
        sender_id = event.message.sender_id
        if not sender_id or not owner_id or sender_id == owner_id:
            return

        cursor.execute('SELECT reason, since FROM afk WHERE user_id=?', (owner_id,))
        afk_row = cursor.fetchone()
        if not afk_row or not isinstance(event.message.peer_id, PeerUser):
            return

        reason, since = afk_row
        try:
            since_dt = datetime.fromisoformat(since).strftime('%H:%M')
        except Exception:
            since_dt = since
        afk_text = f'😴 Сейчас AFK (с {since_dt})'
        if reason:
            afk_text += f'\nПричина: {reason}'
        await event.reply(afk_text)
    except Exception as e:
        print(f"Incoming message handler error: {e}")


async def outgoing_message_handler(event):
    try:
        if owner_id and event.message.sender_id == owner_id:
            cursor.execute('DELETE FROM afk WHERE user_id=?', (owner_id,))
            conn.commit()
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


# =====================================================
# === .DOX — ФЕЙК ДОКС ===
# =====================================================

async def dox_handler(event):
    if not await check_is_owner(event):
        return
    try:
        reply = await event.get_reply_message()
        if not reply:
            await event.edit('❌ Ответьте на сообщение пользователя.')
            return

        user_id = reply.sender_id
        username, name, user = await get_user_info(user_id)
        target_name = name or username or str(user_id)
        username_display = f'@{username}' if username else '—'

        # Фейковые данные
        fake_cities = ['Москва', 'Санкт-Петербург', 'Новосибирск', 'Екатеринбург', 'Казань', 'Нижний Новгород', 'Челябинск', 'Самара', 'Омск', 'Ростов-на-Дону']
        fake_providers = ['МТС', 'Билайн', 'МегаФон', 'Теле2', 'Ростелеком']
        fake_devices = ['iPhone 14 Pro', 'Samsung Galaxy S23', 'Xiaomi 13', 'Google Pixel 7', 'OnePlus 11']
        fake_banks = ['Сбербанк', 'Тинькофф', 'ВТБ', 'Альфа-Банк', 'Газпромбанк']
        city = random.choice(fake_cities)
        provider = random.choice(fake_providers)
        device = random.choice(fake_devices)
        bank = random.choice(fake_banks)
        fake_phone = f'+7{random.randint(900,999)}{random.randint(1000000,9999999)}'
        fake_ip = f'{random.randint(1,255)}.{random.randint(0,255)}.{random.randint(0,255)}.{random.randint(1,254)}'
        fake_coords = f'{random.uniform(55.0, 60.0):.4f}° N, {random.uniform(37.0, 44.0):.4f}° E'

        # Анимация сбора данных
        stages = [
            f'🔍 <b>[ ИНИЦИАЛИЗАЦИЯ ПОИСКА ]</b>\n\n<code>TARGET: {target_name}</code>\n<code>ID: {user_id}</code>\n\n⬛⬛⬛⬛⬛⬛⬛⬛⬛⬛ 0%',
            f'🔍 <b>[ СКАНИРОВАНИЕ БАЗ ДАННЫХ ]</b>\n\n<code>TARGET: {target_name}</code>\n<code>ID: {user_id}</code>\n\n🟩⬛⬛⬛⬛⬛⬛⬛⬛⬛ 10%\n\n<code>» Подключение к базам...\n» Авторизация: OK\n» Поиск совпадений...</code>',
            f'🔎 <b>[ АНАЛИЗ АККАУНТА ]</b>\n\n<code>TARGET: {target_name}</code>\n<code>ID: {user_id}</code>\n\n🟩🟩🟩⬛⬛⬛⬛⬛⬛⬛ 30%\n\n<code>» Username: {username_display}\n» Telegram ID: {user_id}\n» Анализ метаданных...</code>',
            f'📡 <b>[ ГЕОЛОКАЦИЯ ]</b>\n\n<code>TARGET: {target_name}</code>\n<code>ID: {user_id}</code>\n\n🟩🟩🟩🟩🟩⬛⬛⬛⬛⬛ 50%\n\n<code>» IP адрес: {fake_ip}\n» Провайдер: {provider}\n» Определение города...</code>',
            f'📱 <b>[ УСТРОЙСТВО И КОНТАКТЫ ]</b>\n\n<code>TARGET: {target_name}</code>\n<code>ID: {user_id}</code>\n\n🟩🟩🟩🟩🟩🟩🟩⬛⬛⬛ 70%\n\n<code>» Устройство: {device}\n» Телефон: {fake_phone}\n» Привязанный банк: {bank}...</code>',
            f'💾 <b>[ ФИНАЛИЗАЦИЯ ]</b>\n\n<code>TARGET: {target_name}</code>\n<code>ID: {user_id}</code>\n\n🟩🟩🟩🟩🟩🟩🟩🟩🟩⬛ 90%\n\n<code>» Компиляция данных...\n» Шифрование отчёта...\n» Почти готово...</code>',
            (
                f'☠️ <b>[ ДОСЬЕ ГОТОВО ]</b>\n\n'
                f'<code>━━━━━━━━━━━━━━━━━━━━\n'
                f'  СУБЪЕКТ: {target_name}\n'
                f'━━━━━━━━━━━━━━━━━━━━</code>\n\n'
                f'👤 <b>Имя:</b> <code>{name or "Скрыто"}</code>\n'
                f'🔗 <b>Username:</b> <code>{username_display}</code>\n'
                f'🆔 <b>Telegram ID:</b> <code>{user_id}</code>\n'
                f'📞 <b>Телефон:</b> <code>{fake_phone}</code>\n'
                f'🌍 <b>Город:</b> <code>{city}</code>\n'
                f'📍 <b>Координаты:</b> <code>{fake_coords}</code>\n'
                f'🌐 <b>IP:</b> <code>{fake_ip}</code>\n'
                f'📡 <b>Провайдер:</b> <code>{provider}</code>\n'
                f'📱 <b>Устройство:</b> <code>{device}</code>\n'
                f'🏦 <b>Банк:</b> <code>{bank}</code>\n\n'
                f'<code>⚠️ ДАННЫЕ ПОЛУЧЕНЫ ИЗ ОТКРЫТЫХ ИСТОЧНИКОВ\n'
                f'   [FAKE DOX — ТОЛЬКО ДЛЯ РАЗВЛЕЧЕНИЯ]</code>'
            ),
        ]

        delays = [0.8, 1.2, 1.5, 1.5, 1.5, 1.2]

        await event.edit(stages[0], parse_mode='HTML')
        for i, (stage, delay) in enumerate(zip(stages[1:], delays)):
            await asyncio.sleep(delay)
            await event.edit(stage, parse_mode='HTML')

    except Exception as e:
        print(f"Dox error: {e}")
        await event.edit('❌ Ошибка.')


# =====================================================
# === .DEANON — ФЕЙК ДЕАНОН ===
# =====================================================

async def deanon_handler(event):
    if not await check_is_owner(event):
        return
    try:
        reply = await event.get_reply_message()
        if not reply:
            await event.edit('❌ Ответьте на сообщение пользователя.')
            return

        user_id = reply.sender_id
        username, name, _ = await get_user_info(user_id)
        target_name = name or username or str(user_id)

        fake_leaks = ['VK_LEAKED_2021', 'GOSUSLUGI_DB', 'SBERBANK_2022', 'AVITO_DUMP', 'HH_RU_BASE', 'DELIVERY_CLUB_2020', 'YANDEX_FOOD_LEAK']
        fake_emails = [f'{username or "user"}{random.randint(10,99)}@gmail.com', f'{username or "user"}{random.randint(10,99)}@mail.ru', f'id{user_id % 10000}@yandex.ru']
        fake_vk = f'vk.com/id{random.randint(10000000, 999999999)}'
        fake_reg_date = f'{random.randint(2015,2022)}-{random.randint(1,12):02d}-{random.randint(1,28):02d}'
        found_leaks = random.sample(fake_leaks, k=random.randint(2, 4))

        stages = [
            f'🕵️ <b>[ ДЕАНОНИМИЗАЦИЯ ]</b>\n\n<code>ЦЕЛЬ: {target_name}</code>\n\n⬛⬛⬛⬛⬛⬛⬛⬛⬛⬛ 0%\n\n<code>Инициализация поиска по утечкам...</code>',
            f'🕵️ <b>[ ПОИСК ПО УТЕЧКАМ ]</b>\n\n<code>ЦЕЛЬ: {target_name}</code>\n\n🟥🟥🟥⬛⬛⬛⬛⬛⬛⬛ 30%\n\n<code>» Проверка {fake_leaks[0]}... НАЙДЕНО\n» Проверка {fake_leaks[1]}... НАЙДЕНО\n» Проверка {fake_leaks[2]}... НЕТ</code>',
            f'🕵️ <b>[ СОПОСТАВЛЕНИЕ ДАННЫХ ]</b>\n\n<code>ЦЕЛЬ: {target_name}</code>\n\n🟥🟥🟥🟥🟥🟥⬛⬛⬛⬛ 60%\n\n<code>» ВКонтакте: {fake_vk}\n» Email совпадение: ДА\n» Дата регистрации: {fake_reg_date}</code>',
            f'🕵️ <b>[ ФИНАЛИЗАЦИЯ ]</b>\n\n<code>ЦЕЛЬ: {target_name}</code>\n\n🟥🟥🟥🟥🟥🟥🟥🟥🟥⬛ 90%\n\n<code>» Сборка профиля...\n» Верификация данных...</code>',
            (
                f'☠️ <b>[ ДЕАНОН ЗАВЕРШЁН ]</b>\n\n'
                f'👤 <b>Цель:</b> <code>{target_name}</code>\n'
                f'🔗 <b>Username:</b> <code>{"@" + username if username else "—"}</code>\n'
                f'🆔 <b>ID:</b> <code>{user_id}</code>\n\n'
                f'📧 <b>Email из утечек:</b>\n'
                + ''.join([f'<code>  » {e}</code>\n' for e in fake_emails[:2]]) +
                f'\n🔵 <b>ВКонтакте:</b> <code>{fake_vk}</code>\n'
                f'📅 <b>Регистрация:</b> <code>{fake_reg_date}</code>\n\n'
                f'💾 <b>Найдено в базах ({len(found_leaks)}):</b>\n'
                + ''.join([f'<code>  » {l}</code>\n' for l in found_leaks]) +
                f'\n<code>⚠️ [FAKE DEANON — ТОЛЬКО ДЛЯ РАЗВЛЕЧЕНИЯ]</code>'
            ),
        ]

        delays = [1.0, 1.5, 1.5, 1.2]

        await event.edit(stages[0], parse_mode='HTML')
        for stage, delay in zip(stages[1:], delays):
            await asyncio.sleep(delay)
            await event.edit(stage, parse_mode='HTML')

    except Exception as e:
        print(f"Deanon error: {e}")
        await event.edit('❌ Ошибка.')


# =====================================================
# === .FCO — ПРЕДСКАЗАНИЯ НА ДЕНЬ ===
# =====================================================

PREDICTIONS = [
    ('♈ Овен', '🔥 Сегодня звёзды дают тебе силу. Действуй смело — всё получится. Удача на твоей стороне в делах и общении.'),
    ('♉ Телец', '💚 День благоприятен для финансов. Не упусти шанс который появится во второй половине дня. Берегись суеты.'),
    ('♊ Близнецы', '💨 Твоя коммуникабельность сегодня — главный козырь. Новые знакомства принесут пользу. Избегай конфликтов.'),
    ('♋ Рак', '🌊 День эмоциональный. Прислушайся к интуиции — она не подведёт. Вечер проведи с близкими.'),
    ('♌ Лев', '☀️ Твоё время! Сегодня ты в центре внимания. Используй это для важных переговоров и решений.'),
    ('♍ Дева', '🌿 Аналитический ум поможет решить давнюю проблему. День подходит для планирования и порядка.'),
    ('♎ Весы', '⚖️ Гармония достижима — ищи компромисс. Сегодня важно не откладывать на завтра то что можно сделать сейчас.'),
    ('♏ Скорпион', '🖤 Мощная энергия сегодня. Трансформация неизбежна. Не бойся перемен — они ведут к лучшему.'),
    ('♐ Стрелец', '🏹 Стремись к цели без остановок. Удача сопутствует смелым. Новые горизонты ждут тебя.'),
    ('♑ Козерог', '🏔 Упорство принесёт плоды. Сегодня не время для сомнений — действуй по плану. Вечером заслуженный отдых.'),
    ('♒ Водолей', '⚡ Оригинальные идеи придут неожиданно. Доверяй им. Сегодня можно удивить всех своим нестандартным подходом.'),
    ('♓ Рыбы', '🌙 Интуиция на пике. Творческий день — займись тем что давно откладывал. Избегай негативных людей.'),
]

async def fco_handler(event):
    if not await check_is_owner(event):
        return
    try:
        # Псевдослучайный выбор на основе даты и user_id (стабильный в течение дня)
        today = datetime.now().strftime('%Y%m%d')
        seed = int(today) + (event.message.sender_id or 0)
        random.seed(seed)
        sign, prediction = random.choice(PREDICTIONS)
        random.seed()  # сбрасываем seed

        lucky_numbers = sorted(random.sample(range(1, 50), 3))
        lucky_color_list = ['🔴 Красный', '🟠 Оранжевый', '🟡 Жёлтый', '🟢 Зелёный', '🔵 Синий', '🟣 Фиолетовый', '⚪ Белый', '⚫ Чёрный', '🟤 Коричневый']
        lucky_color = random.choice(lucky_color_list)
        energy = random.randint(60, 100)

        bar_filled = energy // 10
        energy_bar = '🟩' * bar_filled + '⬛' * (10 - bar_filled)

        text = (
            f'🔮 <b>Предсказание на {datetime.now().strftime("%d.%m.%Y")}</b>\n\n'
            f'<blockquote>{sign}\n\n'
            f'{prediction}\n\n'
            f'⚡ Энергия дня: {energy_bar} {energy}%\n'
            f'🍀 Счастливые числа: <b>{", ".join(map(str, lucky_numbers))}</b>\n'
            f'🎨 Цвет дня: <b>{lucky_color}</b></blockquote>'
        )
        await event.edit(text, parse_mode='HTML')
    except Exception as e:
        print(f"FCO error: {e}")
        await event.edit('❌ Ошибка предсказания.')


# =====================================================
# === .TTT — КРЕСТИКИ НОЛИКИ ===
# =====================================================

# Хранилище игр: chat_id -> {board, current_player, msg_id, player_x, player_o}
ttt_games = {}

def ttt_board_to_text(board, last_move=None):
    symbols = {0: '⬜', 1: '❌', 2: '⭕'}
    rows = []
    for r in range(3):
        row = []
        for c in range(3):
            idx = r * 3 + c
            row.append(symbols[board[idx]])
        rows.append(' '.join(row))
    return '\n'.join(rows)

def ttt_check_winner(board):
    wins = [
        [0,1,2],[3,4,5],[6,7,8],  # строки
        [0,3,6],[1,4,7],[2,5,8],  # столбцы
        [0,4,8],[2,4,6]           # диагонали
    ]
    for combo in wins:
        if board[combo[0]] != 0 and board[combo[0]] == board[combo[1]] == board[combo[2]]:
            return board[combo[0]]
    if all(c != 0 for c in board):
        return -1  # ничья
    return 0  # игра продолжается

def ttt_make_keyboard(board, game_id):
    symbols = {0: '⬜', 1: '❌', 2: '⭕'}
    kb = tb_types.InlineKeyboardMarkup(row_width=3)
    buttons = []
    for i, cell in enumerate(board):
        if cell == 0:
            buttons.append(tb_types.InlineKeyboardButton('⬜', callback_data=f'ttt_{game_id}_{i}'))
        else:
            buttons.append(tb_types.InlineKeyboardButton(symbols[cell], callback_data=f'ttt_noop'))
    kb.add(*buttons)
    return kb

async def ttt_handler(event):
    if not await check_is_owner(event):
        return
    try:
        chat_id = event.chat_id
        reply = await event.get_reply_message()

        if not reply:
            await event.edit('❌ Ответьте на сообщение противника чтобы начать игру.')
            return

        opponent_id = reply.sender_id
        if opponent_id == event.message.sender_id:
            await event.edit('❌ Нельзя играть с самим собой.')
            return

        game_id = str(chat_id)
        ttt_games[game_id] = {
            'board': [0] * 9,
            'current': 1,  # 1 = X (владелец), 2 = O (противник)
            'player_x': event.message.sender_id,
            'player_o': opponent_id,
            'chat_id': chat_id,
        }

        owner_name = (await client.get_me()).first_name or 'Игрок 1'
        try:
            opp_entity = await client.get_entity(opponent_id)
            opp_name = opp_entity.first_name or 'Игрок 2'
        except Exception:
            opp_name = 'Игрок 2'

        board_text = ttt_board_to_text([0]*9)
        text = (
            f'🎮 <b>Крестики-Нолики</b>\n\n'
            f'❌ {owner_name} vs ⭕ {opp_name}\n\n'
            f'{board_text}\n\n'
            f'Ход: ❌ <b>{owner_name}</b>'
        )

        await event.delete()

        # Отправляем через бот чтобы можно было использовать инлайн кнопки
        loop = asyncio.get_event_loop()
        kb = ttt_make_keyboard([0]*9, game_id)
        await loop.run_in_executor(None, lambda: bot.send_message(
            chat_id, text, parse_mode='HTML', reply_markup=kb
        ))

    except Exception as e:
        print(f"TTT error: {e}")
        await event.edit('❌ Ошибка запуска игры.')


# Обработка ходов крестиков-ноликов через callback
def handle_ttt_callback(call):
    parts = call.data.split('_')
    if len(parts) != 3:
        return
    _, game_id, cell_str = parts
    cell = int(cell_str)
    user_id = call.from_user.id

    if game_id not in ttt_games:
        bot.answer_callback_query(call.id, '❌ Игра не найдена или завершена.')
        return

    game = ttt_games[game_id]
    board = game['board']
    current = game['current']

    # Проверяем чей ход
    if current == 1 and user_id != game['player_x']:
        bot.answer_callback_query(call.id, '⏳ Сейчас не ваш ход!')
        return
    if current == 2 and user_id != game['player_o']:
        bot.answer_callback_query(call.id, '⏳ Сейчас не ваш ход!')
        return

    if board[cell] != 0:
        bot.answer_callback_query(call.id, '❌ Клетка занята!')
        return

    board[cell] = current
    winner = ttt_check_winner(board)

    try:
        px_entity_name = call.from_user.first_name or 'Игрок'
    except Exception:
        px_entity_name = 'Игрок'

    if winner != 0:
        board_text = ttt_board_to_text(board)
        if winner == -1:
            result_text = f'🤝 <b>Ничья!</b>'
        else:
            symbol = '❌' if winner == 1 else '⭕'
            result_text = f'{symbol} <b>{px_entity_name} победил!</b>'

        text = f'🎮 <b>Крестики-Нолики — Игра завершена</b>\n\n{board_text}\n\n{result_text}'
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode='HTML')
        del ttt_games[game_id]
    else:
        game['current'] = 2 if current == 1 else 1
        next_symbol = '❌' if game['current'] == 1 else '⭕'
        board_text = ttt_board_to_text(board)
        text = (
            f'🎮 <b>Крестики-Нолики</b>\n\n'
            f'{board_text}\n\n'
            f'Ход: {next_symbol} <b>{"Игрок X" if game["current"]==1 else "Игрок O"}</b>'
        )
        kb = ttt_make_keyboard(board, game_id)
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              parse_mode='HTML', reply_markup=kb)

    bot.answer_callback_query(call.id)


def text_command(event, transform, usage):
    argument = event.message.text.partition(' ')[2].strip()
    if not argument:
        return event.edit(f'Использование: {usage}')
    return event.edit(transform(argument[:1500]))


async def roll_handler(event):
    if not await check_is_owner(event):
        return
    try:
        parts = event.message.text.split()
        low, high = (1, 100) if len(parts) == 1 else (int(parts[1]), int(parts[2]))
        if len(parts) not in (1, 3) or low > high or high - low > 1_000_000_000:
            raise ValueError
        await event.edit(f'🎲 Выпало: {random.randint(low, high)}')
    except (ValueError, IndexError):
        await event.edit('Использование: .roll [от] [до], например .roll 1 100')


async def ball_handler(event):
    if not await check_is_owner(event):
        return
    answers = [
        'Бесспорно.', 'Предрешено.', 'Никаких сомнений.', 'Определённо да.',
        'Можешь быть уверен.', 'Скорее всего.', 'Пока неясно, попробуй снова.',
        'Спроси позже.', 'Лучше не рассказывать.', 'Сейчас нельзя предсказать.',
        'Сомнительно.', 'Мой ответ — нет.', 'Даже не думай об этом.',
        'Знаки говорят «да».', 'Знаки говорят «нет».',
    ]
    question = event.message.text.partition(' ')[2].strip()
    if not question:
        await event.edit('Задай вопрос: .ball Будет ли сегодня дождь?')
        return
    await event.edit(f'🎱 {random.choice(answers)}')


async def text_transform_handler(event):
    if not await check_is_owner(event):
        return
    command = event.message.text.split(maxsplit=1)[0][1:]
    leet = str.maketrans('aAeEiIoOsStTbB', '4A3E1I0O5S7T8B')
    dumb_replacements = {'ться': 'ццо', 'тся': 'цца', 'что': 'што', 'ч': '4', 'ш': 'щ', 'ё': 'е'}

    def to_dumb(text):
        result = text.lower()
        for source, target in dumb_replacements.items():
            result = result.replace(source, target)
        return result.replace('ого', 'аго').replace('жи', 'жы').replace('ши', 'шы')

    if command == 'leet':
        await text_command(event, lambda text: text.translate(leet), '.leet текст')
    else:
        await text_command(event, to_dumb, '.dumb текст')


MORSE = {
    'а': '.-', 'б': '-...', 'в': '.--', 'г': '--.', 'д': '-..', 'е': '.',
    'ё': '.', 'ж': '...-', 'з': '--..', 'и': '..', 'й': '.---', 'к': '-.-',
    'л': '.-..', 'м': '--', 'н': '-.', 'о': '---', 'п': '.--.', 'р': '.-.',
    'с': '...', 'т': '-', 'у': '..-', 'ф': '..-.', 'х': '....', 'ц': '-.-.',
    'ч': '---.', 'ш': '----', 'щ': '--.-', 'ъ': '--.--', 'ы': '-.--',
    'ь': '-..-', 'э': '..-..', 'ю': '..--', 'я': '.-.-',
    '0': '-----', '1': '.----', '2': '..---', '3': '...--', '4': '....-',
    '5': '.....', '6': '-....', '7': '--...', '8': '---..', '9': '----.',
}
MORSE_REVERSE = {code: char for char, code in MORSE.items()}


async def morse_handler(event):
    if not await check_is_owner(event):
        return
    text = event.message.text.partition(' ')[2].strip().lower()
    if not text:
        await event.edit('Использование: .morse текст')
        return
    words = text.split(' / ')
    if all(all(code in MORSE_REVERSE for code in word.split()) for word in words):
        decoded = ' '.join(''.join(MORSE_REVERSE[code] for code in word.split()) for word in words)
        await event.edit(decoded[:3000])
        return
    encoded = ' / '.join(' '.join(MORSE.get(char, char) for char in word) for word in text.split())
    await event.edit(encoded[:3000])


async def rps_handler(event):
    if not await check_is_owner(event):
        return
    parts = event.message.text.lower().split()
    aliases = {'rock': 'камень', 'scissors': 'ножницы', 'paper': 'бумага'}
    choices = {'камень': '✊', 'ножницы': '✌️', 'бумага': '✋'}
    if len(parts) != 2 or parts[1] not in choices:
        if len(parts) != 2 or parts[1] not in aliases:
            await event.edit('Использование: .rps камень, .rps ножницы или .rps бумага')
            return
    selected = aliases.get(parts[1], parts[1])
    bot_choice = random.choice(('камень', 'ножницы', 'бумага'))
    beats = {'камень': 'ножницы', 'ножницы': 'бумага', 'бумага': 'камень'}
    result = 'Ничья!' if selected == bot_choice else ('Ты выиграл!' if beats[selected] == bot_choice else 'Я выиграл!')
    await event.edit(f'Ты: {choices[selected]}  Я: {choices[bot_choice]}\n{result}')


def safe_calculate(expression):
    if not expression or len(expression) > 100:
        raise ValueError('Слишком длинное выражение')

    tree = ast.parse(expression, mode='eval')
    operations = {
        ast.Add: lambda left, right: left + right,
        ast.Sub: lambda left, right: left - right,
        ast.Mult: lambda left, right: left * right,
        ast.Div: lambda left, right: left / right,
        ast.FloorDiv: lambda left, right: left // right,
        ast.Mod: lambda left, right: left % right,
        ast.Pow: lambda left, right: left ** right,
    }

    def evaluate(node):
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            value = node.value
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            operand = evaluate(node.operand)
            value = operand if isinstance(node.op, ast.UAdd) else -operand
        elif isinstance(node, ast.BinOp) and type(node.op) in operations:
            left = evaluate(node.left)
            right = evaluate(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 100:
                raise ValueError('Слишком большая степень')
            value = operations[type(node.op)](left, right)
        else:
            raise ValueError('Недопустимая операция')

        if not math.isfinite(value) or abs(value) > 1e100:
            raise ValueError('Результат слишком большой')
        return value

    return evaluate(tree)


async def calc_handler(event):
    if not await check_is_owner(event):
        return
    expression = event.message.text.partition(' ')[2].strip()
    try:
        result = safe_calculate(expression)
        await event.edit(f'🔢 {expression} = {result}')
    except (ValueError, SyntaxError, ZeroDivisionError, OverflowError):
        await event.edit('Не удалось вычислить. Поддерживаются числа и +, -, *, /, //, %, **, скобки.')


async def coin_handler(event):
    if not await check_is_owner(event):
        return
    await event.edit('🪙 Подбрасываю...')
    await asyncio.sleep(0.5)
    await event.edit(f'🪙 Выпало: {random.choice(("орёл", "решка"))}')


async def reminder_command_handler(event):
    if not await check_is_owner(event):
        return
    parts = event.message.text.split(maxsplit=2)
    if len(parts) != 3:
        await event.edit('Использование: .remind 30m текст напоминания')
        return
    match = re.fullmatch(r'([1-9]\d{0,4})(s|m|h|d)', parts[1].lower())
    if not match:
        await event.edit('Укажи интервал: например 30m, 2h или 1d.')
        return
    amount, unit = int(match.group(1)), match.group(2)
    factors = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
    seconds = amount * factors[unit]
    if seconds > 30 * 86400:
        await event.edit('Максимальный срок напоминания — 30 дней.')
        return
    remind_at = (datetime.now() + timedelta(seconds=seconds)).isoformat()
    cursor.execute('INSERT INTO reminders (user_id, text, remind_at) VALUES (?, ?, ?)',
                   (owner_id, parts[2][:500], remind_at))
    conn.commit()
    reminder_id = cursor.lastrowid
    await event.edit(f'⏰ Напоминание #{reminder_id} установлено через {parts[1]}.')


async def myreminders_handler(event):
    if not await check_is_owner(event):
        return
    cursor.execute('SELECT id, text, remind_at FROM reminders WHERE user_id=? ORDER BY remind_at LIMIT 20', (owner_id,))
    rows = cursor.fetchall()
    if not rows:
        await event.edit('Активных напоминаний нет.')
        return
    lines = ['⏰ Напоминания:']
    for reminder_id, text, remind_at in rows:
        try:
            display_time = datetime.fromisoformat(remind_at).strftime('%d.%m %H:%M')
        except ValueError:
            display_time = remind_at
        lines.append(f'#{reminder_id} · {display_time} · {text}')
    await event.edit('\n'.join(lines))


async def cancelremind_handler(event):
    if not await check_is_owner(event):
        return
    parts = event.message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await event.edit('Использование: .cancelremind ID')
        return
    cursor.execute('DELETE FROM reminders WHERE id=? AND user_id=?', (int(parts[1]), owner_id))
    conn.commit()
    await event.edit('Напоминание отменено.' if cursor.rowcount else 'Напоминание не найдено.')


ANIMATIONS = {
    'love': ('💗', '💖', '💕', '💞', '💗'),
    'fire': ('🔥', '❤️‍🔥', '🔥🔥', '✨'),
    'boom': ('💥', '💥💥', '✨'),
    'magic': ('✨', '🪄', '🌟', '✨'),
    'matrix': ('▌█▌ 010101', 'ДОСТУП ПОЛУЧЕН', '✅'),
    'heart': ('♡', '♥', '💗', '♥'),
    'load': ('[....] 0%', '[#...] 25%', '[##..] 50%', '[###.] 75%', '[####] 100%'),
    'police': ('🚨', '🔴🚨🔵', '🔵🚨🔴', '🚨'),
    'clown': ('🙂', '🤡', '🤡🎪'),
}


async def animation_handler(event):
    if not await check_is_owner(event):
        return
    command = event.message.text.split(maxsplit=1)[0][1:]
    label = event.message.text.partition(' ')[2].strip()[:150]
    frames = ANIMATIONS[command]
    for index, frame in enumerate(frames):
        suffix = f' {label}' if label else ''
        await event.edit(f'{frame}{suffix}')
        if index < len(frames) - 1:
            await asyncio.sleep(1)


async def image_effect_handler(event):
    if not await check_is_owner(event):
        return
    command = event.message.text.split(maxsplit=1)[0][1:]
    reply = await event.get_reply_message()
    if not reply or not reply.photo:
        await event.edit(f'Ответь командой .{command} на фотографию.')
        return
    if reply.file and reply.file.size and reply.file.size > 15 * 1024 * 1024:
        await event.edit('Файл больше допустимого лимита 15 МБ.')
        return

    try:
        raw_image = await client.download_media(reply, file=bytes)
        with Image.open(io.BytesIO(raw_image)) as source:
            image = ImageOps.exif_transpose(source).convert('RGB')
            if command == 'blur':
                parts = event.message.text.split()
                try:
                    amount = int(parts[1]) if len(parts) > 1 else 50
                except ValueError:
                    amount = 50
                radius = max(1, min(25, amount / 4))
                image = image.filter(ImageFilter.GaussianBlur(radius=radius))
            elif command == 'chp':
                image.thumbnail((640, 640))
            elif command == 'frame':
                image = ImageOps.expand(image, border=18, fill=(28, 31, 36))

            output = io.BytesIO()
            output.name = 'edited.jpg'
            image.save(output, format='JPEG', quality=8 if command == 'chp' else 90)
            output.seek(0)
        await client.send_file(event.chat_id, output, caption=f'Обработка: {command}')
        await event.delete()
    except Exception:
        await event.edit('Не удалось обработать фотографию.')


async def geo_handler(event):
    if not await check_is_owner(event):
        return
    place_query = event.message.text.partition(' ')[2].strip()
    if not place_query:
        await event.edit('Использование: .geo город или общественное место')
        return
    await event.edit('📍 Ищу место...')
    try:
        query = urllib.parse.urlencode({'name': place_query[:100], 'count': 1, 'language': 'ru', 'format': 'json'})
        data = await asyncio.to_thread(fetch_json, f'https://geocoding-api.open-meteo.com/v1/search?{query}')
        places = data.get('results') or []
        if not places:
            await event.edit('Место не найдено.')
            return
        place = places[0]
        latitude, longitude = place['latitude'], place['longitude']
        map_link = f'https://www.openstreetmap.org/?mlat={latitude}&mlon={longitude}#map=12/{latitude}/{longitude}'
        await event.edit(f'📍 {place["name"]}, {place.get("country", "")}\n{map_link}')
    except Exception:
        await event.edit('Не удалось найти место. Попробуй позже.')


async def id_handler(event):
    if not await check_is_owner(event):
        return
    reply = await event.get_reply_message()
    user_id = reply.sender_id if reply else event.message.sender_id
    await event.edit(
        f'🆔 ID чата: {event.chat_id}\n'
        f'ID сообщения: {reply.id if reply else event.message.id}\n'
        f'ID пользователя: {user_id or "не определён"}'
    )


async def link_handler(event):
    if not await check_is_owner(event):
        return
    text = event.message.text.partition(' ')[2].strip()
    me = await client.get_me()
    if not me.username:
        await event.edit('Для ссылки нужен публичный username аккаунта.')
        return
    link = f'https://t.me/{me.username}'
    if text:
        link += f'?text={urllib.parse.quote(text[:500], safe="")}'
    await event.edit(f'🔗 Ссылка на профиль: {link}')


async def qr_handler(event):
    if not await check_is_owner(event):
        return
    value = event.message.text.partition(' ')[2].strip()
    if not value:
        await event.edit('Использование: .qr ссылка или текст')
        return
    if len(value) > 1500:
        await event.edit('Слишком длинный текст для QR-кода (максимум 1500 символов).')
        return
    image = qrcode.make(value)
    output = io.BytesIO()
    output.name = 'qr.png'
    image.save(output, format='PNG')
    output.seek(0)
    await event.delete()
    await client.send_file(event.chat_id, output, caption='QR-код')


def fetch_json(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'SaveModeUserbot/1.0'})
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.loads(response.read(1_000_000))


async def iss_handler(event):
    if not await check_is_owner(event):
        return
    await event.edit('🛰 Получаю координаты МКС...')
    try:
        data = await asyncio.to_thread(fetch_json, 'https://api.wheretheiss.at/v1/satellites/25544')
        await event.edit(
            f'🛰 МКС сейчас над точкой: {data["latitude"]:.3f}, {data["longitude"]:.3f}\n'
            f'Высота: {data["altitude"]:.1f} км\nСкорость: {data["velocity"]:.0f} км/ч'
        )
    except Exception:
        await event.edit('Сервис координат МКС сейчас недоступен.')


async def weather_handler(event):
    if not await check_is_owner(event):
        return
    city = event.message.text.partition(' ')[2].strip()
    if not city:
        await event.edit('Использование: .weather Москва')
        return
    await event.edit('🌦 Ищу прогноз...')
    try:
        query = urllib.parse.urlencode({'name': city[:100], 'count': 1, 'language': 'ru', 'format': 'json'})
        geo = await asyncio.to_thread(fetch_json, f'https://geocoding-api.open-meteo.com/v1/search?{query}')
        places = geo.get('results') or []
        if not places:
            await event.edit('Город не найден. Проверь название и попробуй снова.')
            return
        place = places[0]
        query = urllib.parse.urlencode({
            'latitude': place['latitude'], 'longitude': place['longitude'],
            'current': 'temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m',
            'daily': 'temperature_2m_max,temperature_2m_min,precipitation_probability_max',
            'forecast_days': 2, 'timezone': 'auto',
        })
        forecast = await asyncio.to_thread(fetch_json, f'https://api.open-meteo.com/v1/forecast?{query}')
        current = forecast['current']
        daily = forecast['daily']
        await event.edit(
            f'🌦 {place["name"]}, {place.get("country", "")}\n'
            f'Сейчас: {current["temperature_2m"]}°C, ощущается как {current["apparent_temperature"]}°C\n'
            f'Влажность: {current["relative_humidity_2m"]}% · ветер {current["wind_speed_10m"]} км/ч\n'
            f'Завтра: {daily["temperature_2m_min"][1]}…{daily["temperature_2m_max"][1]}°C, '
            f'вероятность осадков {daily["precipitation_probability_max"][1]}%'
        )
    except Exception:
        await event.edit('Не удалось получить прогноз. Попробуй позже или уточни название города.')


async def exif_handler(event):
    if not await check_is_owner(event):
        return
    reply = await event.get_reply_message()
    if not reply or not reply.photo:
        await event.edit('Ответь командой .spinf на фотографию.')
        return
    if reply.file and reply.file.size and reply.file.size > 15 * 1024 * 1024:
        await event.edit('Файл больше допустимого лимита 15 МБ.')
        return

    path = None
    try:
        handle = tempfile.NamedTemporaryFile(suffix='.img', delete=False)
        path = handle.name
        handle.close()
        await client.download_media(reply, file=path)
        with Image.open(path) as image:
            raw_exif = image.getexif()
            labels = {ExifTags.TAGS.get(key, str(key)): value for key, value in raw_exif.items()}
            safe_fields = ('Make', 'Model', 'DateTime', 'Software', 'Orientation', 'ExposureTime', 'FNumber', 'ISOSpeedRatings', 'FocalLength')
            details = [f'{label}: {labels[label]}' for label in safe_fields if label in labels]
            details.insert(0, f'Размер: {image.width}×{image.height}')
        await event.edit('📷 EXIF фотографии\n' + ('\n'.join(details) if len(details) > 1 else 'EXIF-метаданные не найдены.'))
    except Exception:
        await event.edit('Не удалось прочитать фотографию или её метаданные.')
    finally:
        if path and os.path.exists(path):
            os.remove(path)


async def parody_dox_handler(event):
    if not await check_is_owner(event):
        return
    await event.edit(
        '🎭 <b>ПАРОДИЯ — ВСЁ ВЫДУМАНО</b>\n\n'
        'Объект: секретный агент Печенька\n'
        'Место жительства: Облачное королевство\n'
        'Уровень опасности: 3 из 10 печенек\n'
        'Любимое занятие: искать чай\n\n'
        '<i>Это шуточный текст. Здесь нет поиска или реальных персональных данных.</i>',
        parse_mode='HTML'
    )


HELP_TEXT = """📝 Безопасные команды UserBot

.help — эта справка
.info — публичные данные автора сообщения (ответом)
.afk [причина] / .unafk — режим отсутствия
.ping — задержка ответа
.roll [от] [до] — случайное число
.ball вопрос — шуточный ответ «магического шара»
.rps камень|ножницы|бумага — сыграть с ботом
.calc выражение — безопасный калькулятор
.coin — подбросить монетку
.id — ID чата, сообщения и автора (ответом — данные сообщения)
.link [текст] — ссылка на свой профиль с черновиком сообщения
.qr текст — создать QR-код
.weather город — прогноз на завтра
.iss — координаты Международной космической станции
.spinf — безопасные EXIF-поля фото (ответом; GPS не показывается)
.dox — явно шуточная пародия без поиска данных
.remind 30m текст — напоминание в «Избранное»
.myreminders / .cancelremind ID — список и отмена напоминаний
.geo город — ссылка на найденное публичное место
.blur [0-100] / .chp / .frame — эффекты для фото (ответом)
.love / .fire / .boom / .magic / .matrix / .heart / .load / .police / .clown — короткие анимации
.leet текст — преобразовать текст в leet-speak
.dumb текст — шуточное искажение текста
.morse текст — код Морзе; повторная команда декодирует код
.type текст — анимация печати
.ttt — крестики-нолики (ответом на сообщение соперника)
.stats — локальная статистика
"""


async def help_handler(event):
    if not await check_is_owner(event):
        return
    await event.edit(HELP_TEXT)


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
                    if uid == owner_id:
                        await client.send_message('me', f'🔔 Напоминание!\n\n{text}')
                    else:
                        safe_text = html.escape(text)
                        bot.send_message(uid, f'🔔 <b>Напоминание!</b>\n\n<blockquote>{safe_text}</blockquote>',
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
    c.add_event_handler(incoming_message_handler, events.NewMessage(incoming=True))
    c.add_event_handler(outgoing_message_handler, events.NewMessage(outgoing=True))

    c.add_event_handler(type_handler, events.NewMessage(outgoing=True, pattern=r'^\.type '))
    c.add_event_handler(info_handler, events.NewMessage(outgoing=True, pattern=r'^\.info$'))
    c.add_event_handler(help_handler, events.NewMessage(outgoing=True, pattern=r'^\.help( .*)?$'))
    c.add_event_handler(afk_handler, events.NewMessage(outgoing=True, pattern=r'^\.afk( .*)?$'))
    c.add_event_handler(unafk_handler, events.NewMessage(outgoing=True, pattern=r'^\.unafk$'))
    c.add_event_handler(ping_handler, events.NewMessage(outgoing=True, pattern=r'^\.ping$'))
    c.add_event_handler(stats_handler, events.NewMessage(outgoing=True, pattern=r'^\.stats$'))
    c.add_event_handler(ttt_handler, events.NewMessage(outgoing=True, pattern=r'^\.ttt$'))
    c.add_event_handler(roll_handler, events.NewMessage(outgoing=True, pattern=r'^\.roll(?:\s|$)'))
    c.add_event_handler(ball_handler, events.NewMessage(outgoing=True, pattern=r'^\.(?:8ball|ball)(?:\s|$)'))
    c.add_event_handler(text_transform_handler, events.NewMessage(outgoing=True, pattern=r'^\.(?:leet|dumb)\s'))
    c.add_event_handler(morse_handler, events.NewMessage(outgoing=True, pattern=r'^\.morse\s'))
    c.add_event_handler(rps_handler, events.NewMessage(outgoing=True, pattern=r'^\.rps(?:\s|$)'))
    c.add_event_handler(calc_handler, events.NewMessage(outgoing=True, pattern=r'^\.calc\s'))
    c.add_event_handler(coin_handler, events.NewMessage(outgoing=True, pattern=r'^\.coin$'))
    c.add_event_handler(id_handler, events.NewMessage(outgoing=True, pattern=r'^\.id$'))
    c.add_event_handler(link_handler, events.NewMessage(outgoing=True, pattern=r'^\.link(?:\s|$)'))
    c.add_event_handler(qr_handler, events.NewMessage(outgoing=True, pattern=r'^\.qr\s'))
    c.add_event_handler(weather_handler, events.NewMessage(outgoing=True, pattern=r'^\.weather\s'))
    c.add_event_handler(iss_handler, events.NewMessage(outgoing=True, pattern=r'^\.iss$'))
    c.add_event_handler(exif_handler, events.NewMessage(outgoing=True, pattern=r'^\.spinf$'))
    c.add_event_handler(parody_dox_handler, events.NewMessage(outgoing=True, pattern=r'^\.dox$'))
    c.add_event_handler(reminder_command_handler, events.NewMessage(outgoing=True, pattern=r'^\.remind(?:\s|$)'))
    c.add_event_handler(myreminders_handler, events.NewMessage(outgoing=True, pattern=r'^\.myreminders$'))
    c.add_event_handler(cancelremind_handler, events.NewMessage(outgoing=True, pattern=r'^\.cancelremind(?:\s|$)'))
    c.add_event_handler(geo_handler, events.NewMessage(outgoing=True, pattern=r'^\.geo\s'))
    c.add_event_handler(image_effect_handler, events.NewMessage(outgoing=True, pattern=r'^\.(?:blur|chp|frame)(?:\s|$)'))
    for animation_command in ANIMATIONS:
        c.add_event_handler(
            animation_handler,
            events.NewMessage(outgoing=True, pattern=rf'^\.{animation_command}(?:\s|$)')
        )



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
