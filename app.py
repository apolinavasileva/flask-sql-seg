from flask import Flask, render_template, request, session
from werkzeug.utils import secure_filename
import os
import re
import sqlite3
from itertools import product
from flask_session import Session

UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'seg_B1', 'seg_Y1', 'seg_G1'}
DB_NAME = "seg.db"

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['SECRET_KEY'] = 'supersecretkey'
app.config['SESSION_TYPE'] = 'filesystem'

Session(app)

# для чтения сегов
letters = "GBRY"
nums = "1234"
levels = [ch + num for num, ch in product(nums, letters)]
level_codes = [2 ** i for i in range(len(levels))]
code_to_level = {i: j for i, j in zip(level_codes, levels)}
level_to_code = {j: i for i, j in zip(level_codes, levels)}


# База данных

# создание таблиц
def create_tables(DBname):
    try:
        sqlite_connection = sqlite3.connect(DBname)  # аодключение БД
        cursor = sqlite_connection.cursor()  # подключение курсора

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS words (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                word TEXT NOT NULL,
                file_name TEXT NOT NULL
            );
        ''')  # уникальные значения id
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS transcriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                word_id INTEGER NOT NULL,
                transcription TEXT NOT NULL,
                file_name TEXT NOT NULL,
                FOREIGN KEY(word_id) REFERENCES words(id)
            );
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS f0_values (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                word_id INTEGER NOT NULL,
                f0_start REAL NOT NULL,
                f0_middle REAL NOT NULL,
                f0_end REAL NOT NULL,
                file_name TEXT NOT NULL,
                FOREIGN KEY(word_id) REFERENCES words(id)
            );
        ''')  # связь с таблицей со словами
        sqlite_connection.commit()  # применение изменений
        cursor.close()  # закрытие курсора
    except sqlite3.Error as error:
        print("Error creating tables:", error)
    finally:
        if sqlite_connection:
            sqlite_connection.close()  # закрытие соединения


# функция для добавления слов в БД
def add_word(DBname, word, file_name):
    try:
        sqlite_connection = sqlite3.connect(DBname)
        cursor = sqlite_connection.cursor()
        cursor.execute('''
            INSERT INTO words (word, file_name) VALUES (?, ?)
        ''', (word, file_name))
        word_id = cursor.lastrowid  # последний вставленный id
        sqlite_connection.commit()
        cursor.close()
        return word_id
    except sqlite3.Error as error:
        print("Error adding word:", error)
    finally:
        if sqlite_connection:
            sqlite_connection.close()


# добавление транскрипций
def add_transcription(DBname, word_id, transcription, file_name):
    try:
        sqlite_connection = sqlite3.connect(DBname)
        cursor = sqlite_connection.cursor()
        cursor.execute('''
            INSERT INTO transcriptions (word_id, transcription, file_name) VALUES (?, ?, ?)
        ''', (word_id, transcription, file_name))
        sqlite_connection.commit()
        cursor.close()
    except sqlite3.Error as error:
        print("Error adding transcription:", error)
    finally:
        if sqlite_connection:
            sqlite_connection.close()


# добавление значений f0
def add_f0_values(DBname, word_id, f0_start, f0_middle, f0_end, file_name):
    try:
        sqlite_connection = sqlite3.connect(DBname)
        cursor = sqlite_connection.cursor()
        cursor.execute('''
            INSERT INTO f0_values (word_id, f0_start, f0_middle, f0_end, file_name) VALUES (?, ?, ?, ?, ?)
        ''', (word_id, f0_start, f0_middle, f0_end, file_name))
        sqlite_connection.commit()
        cursor.close()
    except sqlite3.Error as error:
        print("Error adding F0 values:", error)
    finally:
        if sqlite_connection:
            sqlite_connection.close()


# чтение таблицы и формирование вывода на основе 3 таблиц
def read_sqlite_table(DBname):
    try:
        sqlite_connection = sqlite3.connect(DBname, timeout=20)
        cursor = sqlite_connection.cursor()

        # новый запрос
        cursor.execute("""
            SELECT 
                w.id, 
                w.word, 
                t.transcription, 
                f.f0_start, 
                f.f0_middle, 
                f.f0_end, 
                REPLACE(REPLACE(w.file_name, 'uploads/', ''), '_Y1', '') AS file_name_cleaned
            FROM 
                words AS w
            JOIN 
                transcriptions AS t ON w.id = t.word_id
            JOIN 
                f0_values AS f ON w.id = f.word_id
        """)  # колонка word_id для связи
        data = cursor.fetchall()  # извлечение строк из результата запроса

        # строки для HTML
        sentences = [
            '|'.join(map(str, row)) for row in data
        ]
        cursor.close()
    except sqlite3.Error as error:
        print("Error reading table:", error)
        sentences = []
    finally:
        if sqlite_connection:
            sqlite_connection.close()
    return sentences


# обработка seg файлов

# чтение сегов
def read_seg(filename: str, encoding: str = "cp1251") -> tuple[dict, list[dict]]:
    with open(filename, encoding=encoding) as f:
        lines = [line.strip() for line in f.readlines()]
    header_start = lines.index("[PARAMETERS]") + 1
    data_start = lines.index("[LABELS]") + 1
    params = {key: int(value) for line in lines[header_start:data_start - 1] for key, value in [line.split("=")]}
    labels = []
    for line in lines[data_start:]:
        if line.count(",") >= 2:
            pos, level, name = line.split(",", maxsplit=2)
            labels.append({
                "position": int(pos) // params["N_CHANNEL"],
                "level": code_to_level[int(level)],
                "name": name.strip()  # удаление лишних пробелов
            })
    return params, labels


# получение слов
def get_words(filename: str) -> list[str]:
    _, labels = read_seg(filename)
    words = [
        re.sub(r"\[.*?\]", "", label["name"]).strip()  # удаление выражений типа [+], [-]
        for label in labels
        if label["name"] and label["name"] != "~"  # удаление пустых строк
    ]
    return words


# сопоставление аллофонов со словами
def match_words_to_sounds(filename_upper, filename_lower):
    _, labels_upper = read_seg(filename_upper, encoding="cp1251")
    _, labels_lower = read_seg(filename_lower)

    res, word_positions = [], []
    ctr = 0

    for l1, l2 in zip(labels_upper, labels_upper[1:]):
        if not l1["name"]:  # пропуск пустых строк
            print(f"Skipping empty line: {l1}")
            continue

        labels = []
        for label in labels_lower[ctr:]:
            if l1["position"] <= label["position"] < l2["position"]:  # проверка на принадлежность к слову
                ctr += 1
                labels.append(label)
            elif l2["position"] <= label["position"]:
                break

        label_names = []
        positions = []
        for i in labels:
            phoneme = i["name"]
            if phoneme != '~':
                if phoneme[-1].isdigit():  # очистка от чисел
                    phoneme = phoneme[:-1]
                label_names.append(phoneme)
                positions.append(i["position"])

        if not label_names:
            print(f"No phonemes found between positions {l1['position']} and {l2['position']}")

        res.append(label_names)
        word_positions.append(positions)

    return res, word_positions


def get_f0(filename: str, Y1: str, min_f0: float = 0.0) -> tuple[list[list[float]], list[list[float]]]:
    params, labels = read_seg(filename)
    labels = labels[1:-1]  # удаление меток начала и конца

    _, labelsY1 = read_seg(Y1, encoding="cp1251")
    times, f0_values = [], []
    word_counter = 0

    for l1, l2 in zip(labelsY1, labelsY1[1:]):
        if not l1["name"]:  # Skip empty lines
            print(f"Skipping empty line in Y1: {l1}")
            continue

        times.append([])
        f0_values.append([])
        for left, right in zip(labels, labels[1:]):
            time = (right["position"] + left["position"]) / 2
            if time < l1["position"] or time > l2["position"]:
                continue
            f0 = 1 / ((right["position"] - left["position"]) / params["SAMPLING_FREQ"])
            if f0 >= min_f0 and left["name"] != "0":
                times[word_counter].append(time)
                f0_values[word_counter].append(round(f0, 2))
            else:
                f0_values[word_counter].append(0.0)

        if not times[word_counter]:
            print(f"No f0 values found between positions {l1['position']} and {l2['position']}")

        word_counter += 1

    return times, f0_values


# для работы с файлами
def collect_paths(dir_name: str) -> tuple[list[str], str]:
    paths, names = [], []
    for address, _, files in os.walk(dir_name):
        for name in files:
            paths.append(os.path.join(address, name))
            names.append(name)
    return paths, ', '.join(names) if names else '0 files'


# проверка файлов
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1] in ALLOWED_EXTENSIONS


# главная страница
@app.route('/')
def index():
    _, filenames = collect_paths(UPLOAD_FOLDER)
    sentences = read_sqlite_table(DB_NAME)
    new_files = session.get('new_files', [])
    return render_template('index.html', filenames=filenames, sentences=sentences, new_files=new_files)


# загрузка файлов
@app.route('/upload', methods=['POST'])
def upload():
    uploaded_files = request.files.getlist("file")
    upload_message = ""
    new_files = []

    for file in uploaded_files:
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(file_path)
            new_files.append(file_path)
            upload_message += f"{filename} uploaded successfully. "
        else:
            upload_message += f"{file.filename} is not a valid file type. "

    session['new_files'] = new_files
    _, filenames = collect_paths(UPLOAD_FOLDER)
    return render_template('index.html', upload_message=upload_message, filenames=filenames, new_files=new_files)


# actions
@app.route('/handle_actions', methods=['POST'])
def handle_actions():
    action = request.form.get('action')
    if action == 'erasefiles':  # удаление файлов
        paths, _ = collect_paths(UPLOAD_FOLDER)
        for path in paths:
            os.unlink(path)
        session.pop('new_files', None)
        _, filenames = collect_paths(UPLOAD_FOLDER)
        sentences = read_sqlite_table(DB_NAME)
        return render_template('index.html', filenames=filenames, sentences=sentences, DBmessage="Files erased.")

    elif action == 'erasedb':   # удаление бд
        if os.path.exists(DB_NAME):
            os.unlink(DB_NAME)
            create_tables(DB_NAME)  # пересоздаем три таблицы
            return render_template('index.html', DBmessage="Database erased and reset.")
        return render_template('index.html', DBmessage="Database does not exist.")

    elif action == 'add':   # добавление в бд
        new_files = session.get('new_files', [])
        if len(new_files) != 3:
            return render_template('index.html', DBmessage="Exactly 3 files are required.", filenames=new_files)

        try:
            B1, Y1, G1 = '', '', ''
            for path in new_files:
                if 'B1' in path:
                    B1 = path
                elif 'Y1' in path:
                    Y1 = path
                elif 'G1' in path:
                    G1 = path

            words = get_words(Y1)
            phonemes, _ = match_words_to_sounds(Y1, B1)
            f0_times, f0_values = get_f0(G1, Y1)

            for i, word in enumerate(words):
                transcription = ''.join(phonemes[i]) if i < len(phonemes) else ""
                f0_start = f0_values[i][0] if f0_values[i] else 0
                f0_middle = f0_values[i][len(f0_values[i]) // 2] if f0_values[i] else 0
                f0_end = f0_values[i][-1] if f0_values[i] else 0

                # добавление данных в таблицу
                word_id = add_word(DB_NAME, word, Y1)
                add_transcription(DB_NAME, word_id, transcription, B1)
                add_f0_values(DB_NAME, word_id, f0_start, f0_middle, f0_end, G1)

            sentences = read_sqlite_table(DB_NAME)  # чтение
            session.pop('new_files', None)
            return render_template('index.html', DBmessage="Items added successfully.", sentences=sentences)

        except Exception as e:
            return render_template('index.html', DBmessage=f"Error processing files: {e}")

    return render_template('index.html', DBmessage="Unknown action.")


if __name__ == '__main__':
    if not os.path.exists(UPLOAD_FOLDER):
        os.makedirs(UPLOAD_FOLDER)
    create_tables(DB_NAME)
    app.run(debug=True)
