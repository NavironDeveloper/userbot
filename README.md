# SaveMode Userbot

Юзербот для Telegram на базе [Telethon](https://github.com/LonamiWebs/Telethon).  
Сохраняет удалённые и редактированные сообщения, присылает уведомления через бота.

## Функции

| Команда | Описание |
|---|---|
| `.help` | Список команд |
| `.deleted [N]` | Последние N удалённых сообщений (по умолчанию 5) |
| `.mute` | Заглушить пользователя (в ответ на сообщение) |
| `.unmute` | Разглушить пользователя |
| `.info` | Информация о пользователе |
| `.spam N текст` | Отправить сообщение N раз (макс 20) |
| `.type текст` | Анимация печати текста |

**Автоматически:**
- Сохраняет все входящие сообщения (личка + группы)
- При удалении — присылает уведомление с текстом и медиа через бота
- При редактировании — показывает старый и новый текст

---

## Быстрый старт

### 1. Клонировать репозиторий

```bash
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git
cd YOUR_REPO
```

### 2. Установить зависимости

```bash
pip install -r requirements.txt
```

### 3. Настроить переменные окружения

```bash
cp .env.example .env
```

Открыть `.env` и заполнить:

```env
API_ID=       # с https://my.telegram.org/apps
API_HASH=     # с https://my.telegram.org/apps
BOT_TOKEN=    # от @BotFather
```

### 4. Первый запуск (авторизация)

```bash
python userbot.py
```

Telethon попросит номер телефона и код — это одноразово.  
После авторизации создастся файл `ub.session` — **не пушить в Git**.

---

## Деплой на хостинг

### Railway / Render / VPS (рекомендуется)

Эти платформы поддерживают постоянное хранилище файлов, что нужно для `ub.session` и `userbot.db`.

**Railway:**
1. Создать новый проект → Deploy from GitHub
2. В Settings → Variables добавить `API_ID`, `API_HASH`, `BOT_TOKEN`
3. В Settings → Start Command: `python userbot.py`
4. Первый раз авторизоваться локально, затем загрузить `ub.session` через Volume или переменную

**VPS (Ubuntu):**
```bash
# Установить зависимости
pip install -r requirements.txt

# Запустить через screen или systemd
screen -S userbot
python userbot.py
# Ctrl+A, D — отключиться от screen
```

### Koyeb / Heroku — не рекомендуется

Эти платформы сбрасывают файловую систему при рестарте.  
`ub.session` и `userbot.db` будут теряться.

---

## Важные замечания

- Файл `ub.session` содержит авторизацию вашего аккаунта — **никогда не публиковать**
- Файл `.env` содержит секреты — **никогда не публиковать**
- Оба файла уже добавлены в `.gitignore`
- Папка `deleted_media/` создаётся автоматически при запуске

---

## Структура проекта

```
├── userbot.py        # Основной файл
├── requirements.txt  # Зависимости
├── .env.example      # Шаблон переменных окружения
├── .env              # Ваши секреты (не в Git!)
├── .gitignore
├── userbot.db        # База данных (не в Git!)
├── ub.session        # Сессия Telethon (не в Git!)
└── deleted_media/    # Медиафайлы (не в Git!)
```
