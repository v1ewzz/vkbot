import os

from flask import Flask, request, json
import vk_api
import sqlite3
import time
import requests

app = Flask(__name__)

# ================= НАСТРОЙКИ =================
TOKEN = os.environ.get("VK_TOKEN)
GROUP_ID = 238444172  # Твой числовой ID группы (без минуса)
CONFIRM_STRING = "303bdfac"  # Пока пусто, вставим после запуска
# =============================================

vk = vk_api.VkApi(token=TOKEN).get_api()


# ================= КЛАВИАТУРЫ =================
def create_main_keyboard():
    """Создает нижнее меню бота"""
    keyboard = {
        "one_time": False,
        "buttons": [
            [
                {"action": {"type": "text", "payload": "{\"button\": \"1\"}", "label": "📚 Мой каталог"},
                 "color": "positive"},
                {"action": {"type": "text", "payload": "{\"button\": \"2\"}", "label": "➕ Добавить книгу"},
                 "color": "primary"}
            ],
            [
                {"action": {"type": "text", "payload": "{\"button\": \"3\"}", "label": "❓ Помощь"},
                 "color": "secondary"}
            ]
        ]
    }
    return json.dumps(keyboard, ensure_ascii=False)


def create_book_control_keyboard(book_id):
    """Создает inline-кнопки под сообщением о книге"""
    keyboard = {
        "inline": True,
        "buttons": [
            [
                {"action": {"type": "callback", "payload": json.dumps({"cmd": "read", "id": book_id}),
                            "label": "📖 Читать"}, "color": "primary"},
                {"action": {"type": "callback", "payload": json.dumps({"cmd": "status", "id": book_id}),
                            "label": "🏷 Статус"}, "color": "secondary"}
            ]
        ]
    }
    return json.dumps(keyboard, ensure_ascii=False)


def create_reading_keyboard(book_id):
    keyboard = {
        "inline": True,
        "buttons": [
            [
                {"action": {"type": "callback", "payload": json.dumps({"cmd": "nav", "id": book_id, "dir": "prev"}),
                            "label": "⬅️"}, "color": "secondary"},
                {"action": {"type": "callback", "payload": json.dumps({"cmd": "nav", "id": book_id, "dir": "next"}),
                            "label": "➡️"}, "color": "primary"}
            ]
        ]
    }
    return json.dumps(keyboard, ensure_ascii=False)


# ================= БАЗА ДАННЫХ =================
def get_db_connection():
    conn = sqlite3.connect('library.db')
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (vk_id INTEGER PRIMARY KEY)''')
    c.execute('''CREATE TABLE IF NOT EXISTS books (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    author TEXT, title TEXT, genre TEXT, year INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_books (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vk_id INTEGER, book_id INTEGER,
                    status TEXT DEFAULT 'хочу прочитать',
                    last_pos INTEGER DEFAULT 0,
                    msg_id INTEGER DEFAULT NULL)''')
    try:
        c.execute("ALTER TABLE user_books ADD COLUMN msg_id INTEGER DEFAULT NULL")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


# ================= ЛОГИКА БОТА =================
user_states = {}


def send_msg(user_id, text, keyboard=None):
    """Отправка сообщения. Возвращает message_id."""
    params = {
        "user_id": user_id,
        "message": text,
        "random_id": int(time.time() * 1000)
    }
    if keyboard:
        params["keyboard"] = keyboard

    try:
        resp = vk.messages.send(**params)
        if isinstance(resp, dict):
            return resp.get('message_id') or resp.get('conversation_message_id')
        return int(resp)
    except Exception as e:
        print(f"Ошибка отправки: {e}")


def edit_msg(user_id, message_id, text, keyboard=None):
    """Редактирует существующее сообщение"""
    params = {
        "peer_id": user_id,
        "message": text,
        "message_id": message_id
    }
    if keyboard:
        params["keyboard"] = keyboard

    try:
        vk.messages.edit(**params)
    except Exception as e:
        print(f"Ошибка редактирования: {e}")


def handle_message(user_id, message):
    text = message.get('text', '')
    attachments = message.get('attachments', [])
    text_lower = text.strip().lower()

    # Авто-регистрация
    conn = get_db_connection()
    conn.execute("INSERT OR IGNORE INTO users (vk_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

    # Если пользователь на шаге загрузки файла
    if user_id in user_states and user_states[user_id].get("step") == "file":
        if attachments:
            process_file_upload(user_id, attachments)
        elif text_lower == "пропустить":
            del user_states[user_id]
            send_msg(user_id, "✅ Книга добавлена без текста.", keyboard=create_main_keyboard())
        else:
            send_msg(user_id, "📎 Пришлите .txt файл как вложение, или напишите 'пропустить'.")
        return

    if user_id in user_states:
        process_book_adding(user_id, text)
        return

    if text_lower == "/start" or "помощь" in text_lower:
        send_msg(user_id, "👋 Привет! Я бот «Каталог личной библиотеки».\n\n"
                          "Нажми кнопку ➕ Добавить книгу, чтобы начать.",
                 keyboard=create_main_keyboard())

    elif "мой каталог" in text_lower or text_lower.startswith("/list"):
        show_user_library(user_id)

    elif "добавить книгу" in text_lower or text_lower == "/add":
        user_states[user_id] = {"step": "author", "data": {}}
        send_msg(user_id, "✍️ Начнём добавление книги.\nВведите имя автора:")

    elif text_lower.startswith("/read"):
        try:
            book_id = int(text.split()[1])
            read_book(user_id, book_id)
        except:
            send_msg(user_id, "❌ Использование: /read <ID>")

    elif text_lower.startswith("/status"):
        try:
            parts = text.split()
            book_id = int(parts[1])
            new_status = " ".join(parts[2:])
            update_status(user_id, book_id, new_status)
        except:
            send_msg(user_id, "❌ Использование: /status <ID> <статус>")
    else:
        send_msg(user_id, "🤔 Используй кнопки внизу экрана для навигации.", keyboard=create_main_keyboard())


def show_user_library(user_id):
    """Показывает список книг пользователя с кнопками"""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""SELECT b.id, b.title, b.author, ub.status 
                 FROM user_books ub 
                 JOIN books b ON ub.book_id = b.id 
                 WHERE ub.vk_id = ?""", (user_id,))
    books = c.fetchall()
    conn.close()

    if not books:
        send_msg(user_id, "📚 Ваш каталог пуст. Добавьте первую книгу!", keyboard=create_main_keyboard())
        return

    for book in books:
        msg = f"📖 *{book['title']}*\n✍️ Автор: {book['author']}\n🏷 Статус: {book['status']}\nID: {book['id']}"
        send_msg(user_id, msg, keyboard=create_book_control_keyboard(book['id']))


def process_book_adding(user_id, text):
    state = user_states[user_id]
    step = state["step"]
    data = state["data"]

    conn = get_db_connection()
    c = conn.cursor()

    if step == "author":
        data["author"] = text
        state["step"] = "title"
        send_msg(user_id, "📖 Введите название книги:")

    elif step == "title":
        data["title"] = text
        state["step"] = "genre"
        send_msg(user_id, "🏷 Выберите жанр:\n1. Художественная\n2. Научно-популярная\n3. Профессиональная")

    elif step == "genre":
        genre_map = {"1": "художественная", "художественная": "художественная",
                     "2": "научно-популярная", "научно-популярная": "научно-популярная",
                     "3": "профессиональная", "профессиональная": "профессиональная"}
        clean_genre = genre_map.get(text.lower().strip())

        if not clean_genre:
            send_msg(user_id, "❌ Неверный жанр. Напишите цифру (1-3) или название полностью.")
            return

        data["genre"] = clean_genre
        state["step"] = "year"
        send_msg(user_id, "📅 Введите год издания:")

    elif step == "year":
        try:
            year = int(text)
            data["year"] = year

            c.execute("INSERT INTO books (author, title, genre, year) VALUES (?, ?, ?, ?)",
                      (data["author"], data["title"], data["genre"], data["year"]))
            book_id = c.lastrowid

            c.execute("INSERT INTO user_books (vk_id, book_id) VALUES (?, ?)", (user_id, book_id))
            conn.commit()

            state["step"] = "file"
            state["data"]["book_id"] = book_id
            send_msg(user_id, f"✅ Книга \"{data['title']}\" добавлена (ID: {book_id})!\n"
                              f"📎 Теперь пришлите .txt файл с текстом книги, или напишите 'пропустить'.")

        except ValueError:
            send_msg(user_id, "❌ Год должен быть числом.")

    conn.close()


def process_file_upload(user_id, attachments):
    state = user_states.get(user_id)
    if not state or state.get("step") != "file":
        return

    book_id = state["data"].get("book_id")
    if not book_id:
        del user_states[user_id]
        return

    for attachment in attachments:
        if attachment["type"] == "doc":
            doc = attachment["doc"]
            if doc["ext"].lower() != "txt":
                send_msg(user_id, f"❌ Файл \"{doc['title']}\" не .txt. Поддерживаются только .txt файлы.")
                continue

            url = doc["url"]
            try:
                resp = requests.get(url, timeout=30)
                resp.encoding = resp.apparent_encoding or 'utf-8'

                file_path = f"books/{book_id}.txt"
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(resp.text)

                del user_states[user_id]
                send_msg(user_id, f"✅ Текст загружен ({len(resp.text)} символов).",
                         keyboard=create_main_keyboard())
                return

            except Exception as e:
                send_msg(user_id, f"❌ Ошибка при скачивании: {e}")
                return

    send_msg(user_id, "📎 Пришлите .txt файл как вложение, или напишите 'пропустить'.")


def send_book_chunk(user_id, book_id, chunk_pos, msg_id=None):
    file_path = f"books/{book_id}.txt"
    if not os.path.exists(file_path):
        send_msg(user_id, "⚠️ Текст книги отсутствует.")
        return False

    with open(file_path, "r", encoding="utf-8") as f:
        full_text = f.read()

    CHUNK_SIZE = 1000
    chunks = [full_text[i:i + CHUNK_SIZE] for i in range(0, len(full_text), CHUNK_SIZE)]

    if chunk_pos < 0 or chunk_pos >= len(chunks):
        return False

    chunk_text = chunks[chunk_pos].strip()
    full_message = f"📖 Часть {chunk_pos + 1} из {len(chunks)}:\n\n{chunk_text}"
    keyboard = create_reading_keyboard(book_id)

    if msg_id:
        edit_msg(user_id, msg_id, full_message, keyboard=keyboard)
    else:
        new_msg_id = send_msg(user_id, full_message, keyboard=keyboard)
        if new_msg_id:
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("UPDATE user_books SET msg_id=? WHERE vk_id=? AND book_id=?",
                      (new_msg_id, user_id, book_id))
            conn.commit()
            conn.close()

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE user_books SET last_pos=? WHERE vk_id=? AND book_id=?", (chunk_pos + 1, user_id, book_id))
    conn.commit()
    conn.close()
    return True


def read_book(user_id, book_id):
    conn = get_db_connection()
    c = conn.cursor()

    c.execute("SELECT last_pos FROM user_books WHERE vk_id=? AND book_id=?", (user_id, book_id))
    row = c.fetchone()

    if not row:
        send_msg(user_id, "❌ Книги нет в каталоге.")
        conn.close()
        return

    last_pos = row[0]

    file_path = f"books/{book_id}.txt"

    if not os.path.exists(file_path):
        send_msg(user_id, "⚠️ Текст книги отсутствует (файл books/{book_id}.txt не найден).")
        conn.close()
        return

    with open(file_path, "r", encoding="utf-8") as f:
        full_text = f.read()

    CHUNK_SIZE = 1000
    chunks = [full_text[i:i + CHUNK_SIZE] for i in range(0, len(full_text), CHUNK_SIZE)]

    if last_pos >= len(chunks):
        send_msg(user_id, "🏁 Книга дочитана! Статус изменён на «прочитано».")
        c.execute("UPDATE user_books SET status='прочитано' WHERE vk_id=? AND book_id=?", (user_id, book_id))
        conn.commit()
        conn.close()
        return

    send_book_chunk(user_id, book_id, last_pos)
    conn.close()


def update_status(user_id, book_id, new_status):
    valid_statuses = ["хочу прочитать", "читаю", "прочитано"]
    if new_status not in valid_statuses:
        send_msg(user_id, f"❌ Недопустимый статус. Варианты: {', '.join(valid_statuses)}")
        return

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE user_books SET status=? WHERE vk_id=? AND book_id=?", (new_status, user_id, book_id))
    conn.commit()
    conn.close()
    send_msg(user_id, f"✅ Статус книги #{book_id} изменён на: «{new_status}»")


# ================= CALLBACK API =================
@app.route('/', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        return "✅ Бот работает!"

    data = request.json

    if data['type'] == 'confirmation':
        return CONFIRM_STRING

    elif data['type'] == 'message_new':
        try:
            message = data['object']['message']
            user_id = message['peer_id']

            if message.get('out'):
                return 'ok'

            handle_message(user_id, message)

        except Exception as e:
            print(f"Ошибка обработки сообщения: {e}")
        return 'ok'

    elif data['type'] == 'message_event':
        try:
            obj = data['object']
            user_id = obj['user_id']
            peer_id = obj.get('peer_id', user_id)
            event_id = obj['event_id']
            payload = obj['payload']
            if isinstance(payload, str):
                payload = json.loads(payload)

            cmd = payload.get('cmd')
            book_id = payload.get('id')

            if cmd == 'read':
                read_book(user_id, book_id)
                vk.messages.sendMessageEventAnswer(
                    event_id=event_id,
                    user_id=user_id,
                    peer_id=peer_id,
                    event_data=json.dumps({"type": "show_snackbar", "text": "📖 Открываю..."})
                )

            elif cmd == 'status':
                conn = get_db_connection()
                c = conn.cursor()
                c.execute("SELECT status FROM user_books WHERE vk_id=? AND book_id=?", (user_id, book_id))
                row = c.fetchone()
                if row:
                    statuses = ["хочу прочитать", "читаю", "прочитано"]
                    current = row['status']
                    idx = statuses.index(current) if current in statuses else -1
                    new_status = statuses[(idx + 1) % len(statuses)]
                    c.execute("UPDATE user_books SET status=? WHERE vk_id=? AND book_id=?", (new_status, user_id, book_id))
                    conn.commit()
                    vk.messages.sendMessageEventAnswer(
                        event_id=event_id,
                        user_id=user_id,
                        peer_id=peer_id,
                        event_data=json.dumps({"type": "show_snackbar", "text": f"🏷 Статус: {new_status}"})
                    )
                conn.close()

            elif cmd == 'nav':
                direction = payload.get('dir')
                conn = get_db_connection()
                c = conn.cursor()
                c.execute("SELECT last_pos, msg_id FROM user_books WHERE vk_id=? AND book_id=?", (user_id, book_id))
                row = c.fetchone()
                conn.close()

                if not row:
                    vk.messages.sendMessageEventAnswer(
                        event_id=event_id, user_id=user_id, peer_id=peer_id,
                        event_data=json.dumps({"type": "show_snackbar", "text": "❌ Книга не найдена"})
                    )
                else:
                    current_pos = row['last_pos']
                    msg_id = row['msg_id']
                    target = current_pos if direction == 'next' else current_pos - 2

                    if target < 0:
                        vk.messages.sendMessageEventAnswer(
                            event_id=event_id, user_id=user_id, peer_id=peer_id,
                            event_data=json.dumps({"type": "show_snackbar", "text": "⏮ Вы в начале книги"})
                        )
                    else:
                        ok = send_book_chunk(user_id, book_id, target, msg_id=msg_id)
                        if not ok:
                            vk.messages.sendMessageEventAnswer(
                                event_id=event_id, user_id=user_id, peer_id=peer_id,
                                event_data=json.dumps({"type": "show_snackbar", "text": "🏁 Книга дочитана!"})
                            )

        except Exception as e:
            print(f"Ошибка message_event: {e}")
        return 'ok'

    return 'ok'


if __name__ == '__main__':
    if not os.path.exists('books'):
        os.makedirs('books')
    init_db()
    print("🟢 Бот запущен!")
    app.run(host='0.0.0.0', port=5000)
