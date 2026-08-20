import os
import base64
import hashlib
import hmac
import random
import sqlite3
import threading

from dotenv import load_dotenv
from flask import Flask, request, json
import vk_api
import requests

load_dotenv()

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

TOKEN = os.environ.get("VK_TOKEN")
CONFIRM_STRING = os.environ.get("CONFIRM_STRING")
VK_SECRET = os.environ.get("VK_SECRET")

MAX_BOOK_FILE_SIZE = 50 * 1024 * 1024

vk = vk_api.VkApi(token=TOKEN).get_api()

def create_main_keyboard():
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


def get_db_connection():
    conn = sqlite3.connect(os.path.join(BASE_DIR, 'library.db'), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db_connection()
    try:
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
    finally:
        conn.close()


state_lock = threading.Lock()
user_states = {}


def get_user_state(user_id):
    with state_lock:
        return user_states.get(user_id)


def set_user_state(user_id, state):
    with state_lock:
        user_states[user_id] = state


def del_user_state(user_id):
    with state_lock:
        user_states.pop(user_id, None)


def send_msg(user_id, text, keyboard=None):
    params = {
        "user_id": user_id,
        "message": text,
        "random_id": random.randint(1, 2 ** 31 - 1)
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

    conn = get_db_connection()
    try:
        conn.execute("INSERT OR IGNORE INTO users (vk_id) VALUES (?)", (user_id,))
        conn.commit()
    finally:
        conn.close()

    state = get_user_state(user_id)

    if state and state.get("step") == "file":
        if attachments:
            process_file_upload(user_id, attachments)
        elif text_lower == "пропустить":
            del_user_state(user_id)
            send_msg(user_id, "✅ Книга добавлена без текста.", keyboard=create_main_keyboard())
        else:
            send_msg(user_id, "📎 Пришлите .txt файл как вложение, или напишите 'пропустить'.")
        return

    if state:
        process_book_adding(user_id, text)
        return

    if text_lower == "/start" or "помощь" in text_lower:
        send_msg(user_id, "👋 Привет! Я бот «Каталог личной библиотеки».\n\n"
                          "Нажми кнопку ➕ Добавить книгу, чтобы начать.",
                 keyboard=create_main_keyboard())

    elif "мой каталог" in text_lower or text_lower.startswith("/list"):
        show_user_library(user_id)

    elif "добавить книгу" in text_lower or text_lower == "/add":
        set_user_state(user_id, {"step": "author", "data": {}})
        send_msg(user_id, "✍️ Начнём добавление книги.\nВведите имя автора:")

    elif text_lower.startswith("/read"):
        try:
            book_id = int(text.split()[1])
            read_book(user_id, book_id)
        except Exception:
            send_msg(user_id, "❌ Использование: /read <ID>")

    elif text_lower.startswith("/status"):
        try:
            parts = text.split()
            book_id = int(parts[1])
            new_status = " ".join(parts[2:])
            update_status(user_id, book_id, new_status)
        except Exception:
            send_msg(user_id, "❌ Использование: /status <ID> <статус>")
    else:
        send_msg(user_id, "🤔 Используй кнопки внизу экрана для навигации.", keyboard=create_main_keyboard())


def show_user_library(user_id):
    """Показывает список книг пользователя с кнопками"""
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute("""SELECT b.id, b.title, b.author, ub.status 
                     FROM user_books ub 
                     JOIN books b ON ub.book_id = b.id 
                     WHERE ub.vk_id = ?""", (user_id,))
        books = c.fetchall()
    finally:
        conn.close()

    if not books:
        send_msg(user_id, "📚 Ваш каталог пуст. Добавьте первую книгу!", keyboard=create_main_keyboard())
        return

    for book in books:
        title = escape_vk_format(book['title'])
        author = escape_vk_format(book['author'])
        status = escape_vk_format(book['status'])
        msg = f"📖 *{title}*\n✍️ Автор: {author}\n🏷 Статус: {status}\nID: {book['id']}"
        send_msg(user_id, msg, keyboard=create_book_control_keyboard(book['id']))


def process_book_adding(user_id, text):
    state = get_user_state(user_id)
    if not state:
        return
    step = state["step"]
    data = state["data"]

    conn = get_db_connection()
    try:
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
    finally:
        conn.close()


def escape_vk_format(text):
    """Экранирует служебные символы разметки VK (* _ ~ [ ] | \\), чтобы они отображались буквально."""
    return (text.replace('\\', '\\\\')
                .replace('*', '\\*')
                .replace('_', '\\_')
                .replace('~', '\\~')
                .replace('[', '\\[')
                .replace(']', '\\]')
                .replace('|', '\\|'))


def download_book_text(url, max_size):
    """Скачивает текст документа с fallback-подменой хоста.

    VK отдаёт doc-ссылки с разных хостов (vk.com, vk.ru, userapi.com, ...).
    На PythonAnywhere (free) часть хостов блокируется whitelist-прокси.
    Путь у vk.ru/vk.com одинаковый, поэтому при неудаче пробуем подменить хост.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    }
    candidates = [url]
    for host_from, host_to in (("vk.ru", "vk.com"), ("vk.com", "vk.ru")):
        if host_from in url:
            candidates.append(url.replace(host_from, host_to, 1))
            break

    last_error = None
    for candidate in candidates:
        try:
            resp = requests.get(candidate, timeout=30, headers=headers)
            try:
                if int(resp.headers.get('Content-Length', 0) or 0) > max_size:
                    raise ValueError("Файл слишком большой")
                resp.encoding = resp.apparent_encoding or 'utf-8'
                text = resp.text
                if not text.strip():
                    raise ValueError("Файл пуст или недоступен")
                return text
            finally:
                resp.close()
        except Exception as e:
            last_error = e
    if last_error:
        raise last_error
    return None


def process_file_upload(user_id, attachments):
    state = get_user_state(user_id)
    if not state or state.get("step") != "file":
        return
    book_id = state["data"].get("book_id")
    if not book_id:
        del_user_state(user_id)
        return
    for attachment in attachments:
        if attachment["type"] != "doc":
            continue
        doc = attachment["doc"]
        if doc["ext"].lower() != "txt":
            send_msg(user_id, f"❌ Файл \"{doc['title']}\" не .txt. Поддерживаются только .txt файлы.")
            continue

        url = doc["url"]
        try:
            text = download_book_text(url, MAX_BOOK_FILE_SIZE)

            file_path = os.path.join(BASE_DIR, 'books', f"{book_id}.txt")
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(text)

            del_user_state(user_id)
            send_msg(user_id, f"✅ Текст загружен ({len(text)} символов).",
                     keyboard=create_main_keyboard())
            return

        except Exception as e:
            send_msg(user_id, f"❌ Ошибка при скачивании: {e}")
            return

    send_msg(user_id, "📎 Пришлите .txt файл как вложение, или напишите 'пропустить'.")


def send_book_chunk(user_id, book_id, chunk_pos, msg_id=None):
    file_path = os.path.join(BASE_DIR, 'books', f"{book_id}.txt")
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

    conn = get_db_connection()
    try:
        c = conn.cursor()
        if not msg_id and new_msg_id:
            c.execute("UPDATE user_books SET msg_id=? WHERE vk_id=? AND book_id=?",
                      (new_msg_id, user_id, book_id))
        c.execute("UPDATE user_books SET last_pos=? WHERE vk_id=? AND book_id=?",
                  (chunk_pos + 1, user_id, book_id))
        conn.commit()
    finally:
        conn.close()
    return True


def read_book(user_id, book_id):
    conn = get_db_connection()
    try:
        c = conn.cursor()

        c.execute("SELECT last_pos FROM user_books WHERE vk_id=? AND book_id=?", (user_id, book_id))
        row = c.fetchone()

        if not row:
            send_msg(user_id, "❌ Книги нет в каталоге.")
            return

        last_pos = row[0]

        file_path = os.path.join(BASE_DIR, 'books', f"{book_id}.txt")

        if not os.path.exists(file_path):
            send_msg(user_id, "⚠️ Текст книги отсутствует.")
            return

        with open(file_path, "r", encoding="utf-8") as f:
            full_text = f.read()

        CHUNK_SIZE = 1000
        chunks = [full_text[i:i + CHUNK_SIZE] for i in range(0, len(full_text), CHUNK_SIZE)]

        if last_pos >= len(chunks):
            send_msg(user_id, "🏁 Книга дочитана! Статус изменён на «прочитано».")
            c.execute("UPDATE user_books SET status='прочитано' WHERE vk_id=? AND book_id=?", (user_id, book_id))
            conn.commit()
            return
    finally:
        conn.close()

    send_book_chunk(user_id, book_id, last_pos)


def update_status(user_id, book_id, new_status):
    valid_statuses = ["хочу прочитать", "читаю", "прочитано"]
    if new_status not in valid_statuses:
        send_msg(user_id, f"❌ Недопустимый статус. Варианты: {', '.join(valid_statuses)}")
        return

    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute("UPDATE user_books SET status=? WHERE vk_id=? AND book_id=?", (new_status, user_id, book_id))
        conn.commit()
    finally:
        conn.close()
    send_msg(user_id, f"✅ Статус книги #{book_id} изменён на: «{new_status}»")


def verify_vk_signature():
    if not VK_SECRET:
        return True
    signature = request.headers.get('X-Vk-Signature', '')
    if not signature:
        return False
    query_string = request.query_string
    body = request.get_data()
    expected = base64.b64encode(
        hmac.new(VK_SECRET.encode('utf-8'), query_string + body, hashlib.sha256).digest()
    ).decode('ascii')
    return hmac.compare_digest(signature, expected)


# ================= CALLBACK API =================
@app.route('/', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        return "✅ Бот работает!"

    data = request.get_json(silent=True)
    if not isinstance(data, dict) or 'type' not in data:
        return 'ok'

    event_type = data['type']

    if event_type == 'confirmation':
        return CONFIRM_STRING

    if not verify_vk_signature():
        print("Ошибка подписи X-Vk-Signature")
        return 'ok'

    if event_type == 'message_new':
        try:
            message = data['object']['message']
            user_id = message['peer_id']

            if message.get('out'):
                return 'ok'

            handle_message(user_id, message)

        except Exception as e:
            print(f"Ошибка обработки сообщения: {e}")
        return 'ok'

    elif event_type == 'message_event':
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
                try:
                    c = conn.cursor()
                    c.execute("SELECT status FROM user_books WHERE vk_id=? AND book_id=?", (user_id, book_id))
                    row = c.fetchone()
                    if row:
                        statuses = ["хочу прочитать", "читаю", "прочитано"]
                        current = row['status']
                        idx = statuses.index(current) if current in statuses else -1
                        new_status = statuses[(idx + 1) % len(statuses)]
                        c.execute("UPDATE user_books SET status=? WHERE vk_id=? AND book_id=?",
                                  (new_status, user_id, book_id))
                        conn.commit()
                        vk.messages.sendMessageEventAnswer(
                            event_id=event_id,
                            user_id=user_id,
                            peer_id=peer_id,
                            event_data=json.dumps({"type": "show_snackbar", "text": f"🏷 Статус: {new_status}"})
                        )
                finally:
                    conn.close()

            elif cmd == 'nav':
                direction = payload.get('dir')
                conn = get_db_connection()
                try:
                    c = conn.cursor()
                    c.execute("SELECT last_pos, msg_id FROM user_books WHERE vk_id=? AND book_id=?",
                              (user_id, book_id))
                    row = c.fetchone()
                finally:
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


books_dir = os.path.join(BASE_DIR, 'books')
if not os.path.exists(books_dir):
    os.makedirs(books_dir)
init_db()

if __name__ == '__main__':
    print("🟢 Бот запущен!")
    app.run(host='0.0.0.0', port=5000)