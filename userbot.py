import asyncio
import sqlite3
import os
from datetime import datetime
from threading import Thread

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl import types
from telethon.tl.types import PeerUser, PeerChat, PeerChannel, MessageMediaPhoto, MessageMediaDocument
import telebot

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

bot = telebot.TeleBot(token)
client = None  # создаётся в main()

# Папка для хранения медиафайлов удалённых сообщений
MEDIA_DIR = 'deleted_media'
os.makedirs(MEDIA_DIR, exist_ok=True)

conn = sqlite3.connect('userbot.db', check_same_thread=False)
cursor = conn.cursor()

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

# Миграция: добавляем колонки если их нет (для существующих БД)
existing_columns = {row[1] for row in cursor.execute('PRAGMA table_info(messages)')}
if 'media_path' not in existing_columns:
    cursor.execute('ALTER TABLE messages ADD COLUMN media_path TEXT')
if 'media_type' not in existing_columns:
    cursor.execute('ALTER TABLE messages ADD COLUMN media_type TEXT')

conn.commit()

stored_messages = {}
owner_id = None
muted_users = set()


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
    """Возвращает название чата/группы по chat_id."""
    try:
        entity = await client.get_entity(chat_id)
        if hasattr(entity, 'title'):
            return entity.title
        if hasattr(entity, 'first_name'):
            return f"{entity.first_name or ''} {entity.last_name or ''}".strip()
    except Exception:
        pass
    return f"ID:{chat_id}"


def send_bot_message_sync(text, media_path=None, media_type=None):
    try:
        if media_path and os.path.exists(media_path):
            with open(media_path, 'rb') as f:
                if media_type == 'photo':
                    bot.send_photo(owner_id, f, caption=text, parse_mode='HTML')
                elif media_type == 'video':
                    bot.send_video(owner_id, f, caption=text, parse_mode='HTML')
                elif media_type == 'voice':
                    bot.send_voice(owner_id, f, caption=text, parse_mode='HTML')
                elif media_type == 'audio':
                    bot.send_audio(owner_id, f, caption=text, parse_mode='HTML')
                elif media_type == 'sticker':
                    bot.send_sticker(owner_id, f)
                    if text:
                        bot.send_message(owner_id, text, parse_mode='HTML', disable_web_page_preview=True)
                else:
                    bot.send_document(owner_id, f, caption=text, parse_mode='HTML')
        else:
            if text:
                bot.send_message(owner_id, text, parse_mode='HTML', disable_web_page_preview=True)
    except Exception as e:
        print(f"Bot send error: {e}")
        # Фолбэк — просто текст
        try:
            if text:
                bot.send_message(owner_id, text, parse_mode='HTML', disable_web_page_preview=True)
        except Exception as e2:
            print(f"Bot fallback send error: {e2}")


async def send_bot_message(text, media_path=None, media_type=None):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, send_bot_message_sync, text, media_path, media_type)


async def check_is_owner(event):
    return event.message.sender_id == owner_id


async def download_media_if_exists(message) -> tuple[str | None, str | None]:
    """
    Скачивает медиа из сообщения, возвращает (путь, тип) или (None, None).
    Тип: 'photo', 'video', 'voice', 'audio', 'sticker', 'document'
    """
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
                # попытаемся взять оригинальное расширение
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
    """Универсально достаёт числовой ID из PeerUser/PeerChat/PeerChannel."""
    if isinstance(peer, PeerUser):
        return peer.user_id
    if isinstance(peer, PeerChat):
        return peer.chat_id
    if isinstance(peer, PeerChannel):
        return peer.channel_id
    return None


def is_group_peer(peer):
    return isinstance(peer, (PeerChat, PeerChannel))


# === СОХРАНЕНИЕ ВХОДЯЩИХ / ИСХОДЯЩИХ ===

async def store_message(message):
    """Сохраняет сообщение в БД и в памяти для последующего отслеживания удалений."""
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


# === ОБРАБОТКА УДАЛЕНИЙ ===

async def on_message_deleted(msg_id: int, chat_id: int | None = None):
    """
    Единая точка обработки удалённого сообщения.
    chat_id может быть None для личных диалогов (UpdateDeleteMessages без chat_id).
    """
    # Ищем в БД
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

    # Сообщения владельца не уведомляем
    if user_id == owner_id:
        cursor.execute('DELETE FROM messages WHERE msg_id=? AND chat_id=?', (msg_id, found_chat_id))
        conn.commit()
        return

    # Сохраняем в таблицу удалённых
    cursor.execute(
        'INSERT INTO deleted_messages (msg_id, user_id, chat_id, text, media_path, media_type, original_date, deleted_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (msg_id, user_id, found_chat_id, text, media_path, media_type, orig_date, datetime.now().isoformat())
    )
    cursor.execute('DELETE FROM messages WHERE msg_id=? AND chat_id=?', (msg_id, found_chat_id))
    conn.commit()
    stored_messages.pop((found_chat_id, msg_id), None)

    # Формируем уведомление
    username, name, _ = await get_user_info(user_id)
    link = f"https://t.me/{username}" if username else f"tg://user?id={user_id}"

    # Откуда пришло сообщение
    if found_chat_id != user_id:
        chat_title = await get_chat_title(found_chat_id)
        source = f" в <b>{chat_title}</b>"
    else:
        source = ""

    media_label = ""
    if media_type:
        labels = {
            'photo': '🖼 Фото',
            'video': '🎬 Видео',
            'voice': '🎤 Голосовое',
            'audio': '🎵 Аудио',
            'sticker': '🎭 Стикер',
            'document': '📎 Документ',
        }
        media_label = f"\n{labels.get(media_type, '📎 Медиа')}"

    caption = (
        f"🗑 Удалённое сообщение{source}\n\n"
        f"<blockquote><a href=\"{link}\">{name}</a>{media_label}\n"
        f"{text or ''}</blockquote>"
    )

    await send_bot_message(caption, media_path, media_type)


async def raw_deleted_handler(event):
    """Обрабатывает UpdateDeleteMessages (личные чаты) и UpdateDeleteChannelMessages (каналы/супергруппы)."""
    try:
        # event — это сам объект апдейта (UpdateDeleteMessages или UpdateDeleteChannelMessages)
        channel_id = getattr(event, 'channel_id', None)

        # deleted_ids есть в обоих типах апдейта
        msg_ids = getattr(event, 'messages', [])

        for msg_id in msg_ids:
            await on_message_deleted(msg_id, chat_id=channel_id)
    except Exception as e:
        print(f"Raw delete error: {e}")


# === ПРОСМОТР УДАЛЁННЫХ ===

async def deleted_handler(event):
    """
    .deleted [N] — показывает последние N удалённых сообщений (по умолчанию 5, макс 20).
    """
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
                labels = {
                    'photo': '🖼 Фото',
                    'video': '🎬 Видео',
                    'voice': '🎤 Голосовое',
                    'audio': '🎵 Аудио',
                    'sticker': '🎭 Стикер',
                    'document': '📎 Документ',
                }
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


# === ОБРАБОТКА РЕДАКТИРОВАНИЙ ===

async def process_edited_message(event):
    if event.message.out:
        return
    try:
        peer = event.message.peer_id
        chat_id = get_peer_id(peer)
        if not chat_id:
            return

        cursor.execute(
            'SELECT text FROM messages WHERE msg_id=? AND chat_id=?',
            (event.message.id, chat_id)
        )
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

        cursor.execute(
            'UPDATE messages SET text=? WHERE msg_id=? AND chat_id=?',
            (new_text, event.message.id, chat_id)
        )
        conn.commit()
        stored_messages[(chat_id, event.message.id)] = new_text
    except Exception as e:
        print(f"Edit error: {e}")


# === ОСТАЛЬНЫЕ КОМАНДЫ ===

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
    """Сохраняет все входящие сообщения (личка + группы)."""
    try:
        peer = event.message.peer_id
        sender_id = event.message.sender_id
        if not sender_id:
            return

        # Удаляем сообщения от замьюченных (только в личке)
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
    """Сохраняет исходящие сообщения для отслеживания редактирований."""
    try:
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


HELP_TEXT = """<b>📝 Команды</b>

<blockquote>▫️ Help: ( .help ) — Справка
▫️ Mute: ( .mute | .unmute ) — Помолчи
▫️ Spam: ( .spam ) — Спам
▫️ Typer: ( .type ) — Набор текста
▫️ UserInfo: ( .info ) — Инфо о пользователе
▫️ Deleted: ( .deleted [N] ) — Последние N удалённых сообщений</blockquote>

Справка по команде: <code>.help [команда]</code>"""


async def help_handler(event):
    if not await check_is_owner(event):
        return
    args = event.message.text.split(' ', 1)
    if len(args) > 1:
        command = args[1].strip()
        if command == 'type':
            await event.edit('⚙️ Typer\n\n· .type [текст] — Анимация печати текста')
        elif command == 'spam':
            await event.edit('⚙️ Spam\n\n· .spam [кол-во] [текст или реплай] — Спам сообщений (макс 20)')
        elif command == 'mute':
            await event.edit(
                '⚙️ Mute\n\n'
                '· .mute — Заглушить пользователя (в ответ на сообщение)\n'
                '· .unmute — Разглушить пользователя (в ответ на сообщение)'
            )
        elif command == 'info':
            await event.edit('⚙️ UserInfo\n\n· .info — Информация о пользователе (в ответ на сообщение)')
        elif command == 'deleted':
            await event.edit(
                '⚙️ Deleted\n\n'
                '· .deleted — Показать последние 5 удалённых сообщений\n'
                '· .deleted [N] — Показать последние N (макс 20)\n\n'
                'Сохраняются: текст, фото, видео, голосовые, документы, стикеры.\n'
                'Работает в личных диалогах и группах.'
            )
        else:
            await event.edit(HELP_TEXT, parse_mode='HTML')
    else:
        await event.edit(HELP_TEXT, parse_mode='HTML')


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
    # Удаления — единый обработчик для личных и групповых чатов
    c.add_event_handler(raw_deleted_handler, events.Raw(types.UpdateDeleteMessages))
    c.add_event_handler(raw_deleted_handler, events.Raw(types.UpdateDeleteChannelMessages))

    # Мьют
    c.add_event_handler(mute_handler, events.NewMessage(outgoing=True, pattern=r'^\.mute$'))
    c.add_event_handler(unmute_handler, events.NewMessage(outgoing=True, pattern=r'^\.unmute$'))

    # Входящие и исходящие — сохранение
    c.add_event_handler(incoming_message_handler, events.NewMessage(incoming=True))
    c.add_event_handler(outgoing_message_handler, events.NewMessage(outgoing=True))

    # Команды
    c.add_event_handler(type_handler, events.NewMessage(outgoing=True, pattern=r'^\.type '))
    c.add_event_handler(spam_handler, events.NewMessage(outgoing=True, pattern=r'^\.spam '))
    c.add_event_handler(info_handler, events.NewMessage(outgoing=True, pattern=r'^\.info$'))
    c.add_event_handler(deleted_handler, events.NewMessage(outgoing=True, pattern=r'^\.deleted( \d+)?$'))
    c.add_event_handler(help_handler, events.NewMessage(outgoing=True, pattern=r'^\.help( .*)?$'))

    # Редактирования
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

    Thread(target=run_bot, daemon=True).start()
    await client.run_until_disconnected()


if __name__ == '__main__':
    asyncio.run(main())
