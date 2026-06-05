import json
import os
import hashlib
import time
import io
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import httpx
from httpx import Timeout
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_community.document_loaders import PyPDFLoader
from langchain_community.vectorstores import FAISS
from langchain_ollama import OllamaEmbeddings
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

app = FastAPI(title="AITEACHER API")
app.mount("/static", StaticFiles(directory="static"), name="static")

OLLAMA_API = "http://localhost:11434/api/chat"
MODEL_NAME = "qwen2.5:3b"
EMBEDDING_MODEL = "nomic-embed-text"
SYSTEM_PROMPT_PATH = "config/system_prompt.txt"
PROMPTS_DIR = "config/prompts"
FAISS_INDEX_DIR = "faiss_index"

USERS_FILE = "users.json"
SESSIONS_FILE = "sessions.json"
ADMIN_FILE = "config/admin.json"
DAILY_STATS_FILE = "daily_stats.json"
USERS_DIR = "uploads"
AVATARS_DIR = "uploads/avatars"
users = {}
sessions = {}
daily_stats = {}
SUSPEND_TIME_THRESHOLD = 30  # минут для трекинга времени

vector_store = None
splitter = RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=50)

os.makedirs("uploads", exist_ok=True)
os.makedirs("uploads/avatars", exist_ok=True)

SUBJECTS = [
    "russian", "algebra", "geometry", "physics", "chemistry",
    "english", "history", "social", "informatics"
]

SUBJECT_LABELS = {
    "russian": "Русский язык",
    "algebra": "Алгебра",
    "geometry": "Геометрия",
    "physics": "Физика",
    "chemistry": "Химия",
    "english": "Английский язык",
    "history": "История",
    "social": "Обществознание",
    "informatics": "Информатика"
}

SUBJECT_ICONS = {
    "russian": "📝",
    "algebra": "➗",
    "geometry": "📐",
    "physics": "⚡",
    "chemistry": "🧪",
    "english": "🔤",
    "history": "📜",
    "social": "🏛️",
    "informatics": "💻"
}

def load_json(path: str) -> dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_json(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_docs_manifest() -> dict:
    docs_dir = "docs"
    if not os.path.isdir(docs_dir):
        return {}
    manifest = {}
    for f in os.listdir(docs_dir):
        if f.endswith(".pdf"):
            path = os.path.join(docs_dir, f)
            st = os.stat(path)
            manifest[f] = {"mtime": st.st_mtime, "size": st.st_size}
    return manifest

def save_docs_manifest(manifest: dict) -> None:
    os.makedirs(FAISS_INDEX_DIR, exist_ok=True)
    with open(os.path.join(FAISS_INDEX_DIR, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

def load_docs_manifest() -> dict | None:
    path = os.path.join(FAISS_INDEX_DIR, "manifest.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def init_rag():
    global vector_store
    docs_dir = "docs"
    if not os.path.exists(docs_dir):
        os.makedirs(docs_dir)
        print("Папка docs/ создана. Поместите туда PDF-учебники и перезапустите сервер.")
        return

    current = get_docs_manifest()
    saved = load_docs_manifest()

    if saved == current and os.path.isdir(FAISS_INDEX_DIR):
        try:
            embeddings = OllamaEmbeddings(model=EMBEDDING_MODEL)
            vector_store = FAISS.load_local(FAISS_INDEX_DIR, embeddings, allow_dangerous_deserialization=True)
            print("RAG-база загружена из кэша.")
            return
        except Exception as e:
            print(f"Не удалось загрузить кэш: {e}. Переиндексация...")

    if not current:
        print("В папке docs/ нет PDF-файлов для индексации.")
        return

    documents = []
    for f in os.listdir(docs_dir):
        if f.endswith(".pdf"):
            documents.extend(PyPDFLoader(os.path.join(docs_dir, f)).load())

    print("Индексация учебных материалов...")
    chunks = splitter.split_documents(documents)
    embeddings = OllamaEmbeddings(model=EMBEDDING_MODEL)
    vector_store = FAISS.from_documents(chunks, embeddings)
    os.makedirs(FAISS_INDEX_DIR, exist_ok=True)
    vector_store.save_local(FAISS_INDEX_DIR)
    save_docs_manifest(current)
    print("RAG-база проиндексирована и сохранена в кэш.")

def retrieve_context(query: str) -> str:
    if vector_store is None:
        return ""
    docs = vector_store.similarity_search(query, k=3)
    return "\n\n".join([doc.page_content for doc in docs])

def load_subject_prompt(subject: str) -> str:
    filepath = os.path.join(PROMPTS_DIR, f"{subject}.txt")
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            return f.read().strip()
    with open(SYSTEM_PROMPT_PATH, "r", encoding="utf-8") as f:
        return f.read().strip()

# ─── Startup ───

@app.on_event("startup")
def startup_event():
    global users, sessions, daily_stats
    users = load_json(USERS_FILE)
    sessions = load_json(SESSIONS_FILE)
    daily_stats = load_json(DAILY_STATS_FILE)
    init_rag()

app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

# ─── Models ───

class RegisterRequest(BaseModel):
    username: str
    password: str

class LoginRequest(BaseModel):
    username: str
    password: str

class ChatRequest(BaseModel):
    username: str
    subject: str
    message: str
    attachments: list[str] = []

class SupportRequest(BaseModel):
    username: str
    subject: str
    message: str

class AccentColorRequest(BaseModel):
    username: str
    color: str

# ─── User endpoints ───

@app.get("/api/users")
def get_users():
    return list(users.keys())

@app.post("/api/register")
def register(req: RegisterRequest):
    name = req.username.strip()
    if not name:
        raise HTTPException(400, "Имя не может быть пустым")
    if name in users:
        return {"status": "ok", "username": name, "new": False}
    now = datetime.now().isoformat()
    pwd_hash = hashlib.sha256(req.password.encode()).hexdigest()
    users[name] = {
        "password_hash": pwd_hash,
        "created_at": now,
        "last_active": now,
        "blocked": False,
        "suspended": False,
        "avatar": None,
        "accent_color": None
    }
    save_json(USERS_FILE, users)
    if name not in sessions:
        sessions[name] = {}
        save_json(SESSIONS_FILE, sessions)
    if name not in daily_stats:
        daily_stats[name] = {}
        save_json(DAILY_STATS_FILE, daily_stats)
    return {"status": "ok", "username": name, "new": True}

@app.post("/api/login")
def login(req: LoginRequest):
    name = req.username.strip()
    if name not in users:
        raise HTTPException(401, "Неверный пароль")
    pwd_hash = hashlib.sha256(req.password.encode()).hexdigest()
    if users[name].get("password_hash") != pwd_hash:
        raise HTTPException(401, "Неверный пароль")
    return {"status": "ok", "username": name}

@app.get("/api/user/{username}")
def get_user(username: str):
    if username not in users:
        raise HTTPException(404, "Пользователь не найден")
    u = users[username]
    return {
        "username": username,
        "avatar": u.get("avatar"),
        "accent_color": u.get("accent_color")
    }

# ─── Subject endpoints ───

@app.get("/api/subjects")
def get_subjects():
    return [{"id": s, "label": SUBJECT_LABELS[s], "icon": SUBJECT_ICONS[s]} for s in SUBJECTS]

# ─── Upload / Avatar / Accent / Support ───

ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".pdf", ".doc", ".docx", ".txt", ".zip", ".rar", ".7z", ".mp4", ".mp3", ".csv", ".xlsx", ".pptx"}

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Недопустимое расширение: {ext}")
    name = f"{int(time.time())}_{file.filename}"
    path = os.path.join(USERS_DIR, name)
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(400, "Файл слишком большой (макс. 10MB)")
    with open(path, "wb") as f:
        f.write(content)
    return {"url": f"/uploads/{name}"}

@app.post("/api/avatar")
async def upload_avatar(username: str = File(...), file: UploadFile = File(...)):
    if username not in users:
        raise HTTPException(404, "Пользователь не найден")
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        raise HTTPException(400, "Только изображения")
    content = await file.read()
    if len(content) > 2 * 1024 * 1024:
        raise HTTPException(400, "Аватар слишком большой (макс. 2MB)")
    save_ext = ".webp" if ext != ".gif" else ".gif"
    name = f"{username}{save_ext}"
    path = os.path.join(AVATARS_DIR, name)
    with open(path, "wb") as f:
        f.write(content)
    url = f"/uploads/avatars/{name}"
    users[username]["avatar"] = url
    save_json(USERS_FILE, users)
    return {"url": url}

@app.post("/api/accent-color")
def set_accent_color(req: AccentColorRequest):
    name = req.username.strip()
    if name not in users:
        raise HTTPException(404, "Пользователь не найден")
    users[name]["accent_color"] = req.color
    save_json(USERS_FILE, users)
    return {"status": "ok", "accent_color": req.color}

@app.post("/api/support")
def support(req: SupportRequest):
    return {"status": "ok", "note": "Функция отправки писем отключена"}

# ─── Chat endpoint ───

def track_daily_stat(username: str, stat_type: str, subject: str = None, hour: int = None):
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if username not in daily_stats:
        daily_stats[username] = {}
    if today not in daily_stats[username]:
        daily_stats[username][today] = {"requests": 0, "responses": 0, "time_spent_minutes": 0.0, "subjects": {}, "hours": {}}
    if stat_type == "request":
        daily_stats[username][today]["requests"] += 1
        if subject:
            daily_stats[username][today]["subjects"][subject] = daily_stats[username][today]["subjects"].get(subject, 0) + 1
        if hour is not None:
            hk = str(hour)
            daily_stats[username][today]["hours"][hk] = daily_stats[username][today]["hours"].get(hk, 0) + 1
    if stat_type == "response":
        daily_stats[username][today]["responses"] += 1

@app.post("/api/chat")
async def chat_endpoint(request: ChatRequest):
    username = request.username.strip()
    user_data = users.get(username, {})
    if user_data.get("blocked"):
        raise HTTPException(403, "Аккаунт заблокирован")
    if user_data.get("suspended"):
        raise HTTPException(403, "Аккаунт приостановлен. Вы не можете отправлять сообщения.")

    now = datetime.now()
    # трекинг времени
    last_active_str = user_data.get("last_active")
    if last_active_str:
        try:
            last_dt = datetime.fromisoformat(last_active_str)
            diff = (now - last_dt).total_seconds() / 60
            if 0 < diff < SUSPEND_TIME_THRESHOLD:
                today = now.strftime("%Y-%m-%d")
                if username not in daily_stats:
                    daily_stats[username] = {}
                if today not in daily_stats[username]:
                    daily_stats[username][today] = {"requests": 0, "responses": 0, "time_spent_minutes": 0.0, "subjects": {}, "hours": {}}
                daily_stats[username][today]["time_spent_minutes"] += diff
        except:
            pass

    users[username]["last_active"] = now.isoformat()
    save_json(USERS_FILE, users)

    subject = request.subject.strip()
    user_msg = request.message.strip()
    attachments = request.attachments or []

    # трекинг запроса
    track_daily_stat(username, "request", subject=subject, hour=now.hour)

    if username not in sessions:
        sessions[username] = {}
    if subject not in sessions[username]:
        sessions[username][subject] = []

    full_msg = user_msg
    for url in attachments:
        ext = os.path.splitext(url.split("?")[0])[1].lower()
        if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
            full_msg += f"\n![image]({url})"
        else:
            name = os.path.basename(url)
            full_msg += f"\n[{name}]({url})"

    context = retrieve_context(full_msg)
    system_prompt = load_subject_prompt(subject)
    if context:
        system_prompt += f"\n\n[УЧЕБНЫЙ КОНТЕКСТ]:\n{context}"

    history = sessions[username][subject]
    history.append({"role": "user", "content": full_msg})
    save_json(SESSIONS_FILE, sessions)

    ollama_payload = {
        "model": MODEL_NAME,
        "messages": [{"role": "system", "content": system_prompt}] + history,
        "stream": True,
        "options": {"num_ctx": 8192, "temperature": 0.3}
    }

    async def generate_sse():
        client_timeout = Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0)
        async with httpx.AsyncClient(timeout=client_timeout) as client:
            try:
                async with client.stream("POST", OLLAMA_API, json=ollama_payload) as resp:
                    if resp.status_code != 200:
                        yield f'data: {{"error": "Ошибка Ollama: {resp.status_code}"}}\n\n'
                        return
                    assistant_text = ""
                    async for line in resp.aiter_lines():
                        if not line.strip(): continue
                        try:
                            chunk = json.loads(line)
                            if "message" in chunk and "content" in chunk["message"]:
                                token = chunk["message"]["content"]
                                assistant_text += token
                                yield f"data: {json.dumps({'content': token})}\n\n"
                            if chunk.get("done", False):
                                sessions[username][subject].append({"role": "assistant", "content": assistant_text})
                                save_json(SESSIONS_FILE, sessions)
                                # трекинг ответа
                                track_daily_stat(username, "response")
                                save_json(DAILY_STATS_FILE, daily_stats)
                                yield f"data: {json.dumps({'done': True})}\n\n"
                                break
                        except json.JSONDecodeError:
                            continue
            except httpx.ConnectError:
                yield 'data: {"error": "Ollama не запущен. Запустите ollama serve"}\n\n'
            except httpx.ReadTimeout:
                yield 'data: {"error": "Ollama не отвечает. Проверьте модель и повторите"}\n\n'

    return StreamingResponse(generate_sse(), media_type="text/event-stream")

# ─── History endpoints ───

@app.get("/api/history/{username}/{subject}")
def get_history(username: str, subject: str):
    return sessions.get(username, {}).get(subject, [])

@app.delete("/api/history/{username}/{subject}")
def clear_history(username: str, subject: str):
    if username in sessions and subject in sessions[username]:
        sessions[username][subject] = []
        save_json(SESSIONS_FILE, sessions)
    return {"status": "ok"}

# ─── Admin endpoints ───

def check_admin(username: str):
    admin = load_json(ADMIN_FILE)
    if username != admin.get("username"):
        raise HTTPException(403, "Доступ запрещён")

@app.post("/api/admin/login")
def admin_login(req: LoginRequest):
    admin = load_json(ADMIN_FILE)
    if req.username != admin["username"] or req.password != admin["password"]:
        raise HTTPException(401, "Неверные данные администратора")
    return {"status": "ok", "username": req.username}

@app.get("/api/admin/users")
def admin_get_users(un: str = ""):
    check_admin(un)
    result = []
    now_dt = datetime.now()
    for name, data in users.items():
        last = data.get("last_active", "")
        online = False
        if last:
            try:
                online = (now_dt - datetime.fromisoformat(last)) < timedelta(minutes=1)
            except: pass
        total_msgs = sum(len(v) for v in sessions.get(name, {}).values()) if isinstance(sessions.get(name), dict) else 0
        result.append({
            "username": name,
            "avatar": data.get("avatar"),
            "online": online,
            "last_active": last,
            "blocked": data.get("blocked", False),
            "created_at": data.get("created_at", ""),
            "total_messages": total_msgs
        })
    return result

@app.get("/api/admin/user/{username}")
def admin_get_user(username: str, un: str = ""):
    check_admin(un)
    if username not in users:
        raise HTTPException(404, "Пользователь не найден")
    user_data = users[username]
    history = []
    user_sessions = sessions.get(username, {})
    if isinstance(user_sessions, dict):
        for subject, msgs in user_sessions.items():
            for msg in msgs:
                history.append({
                    "subject": subject,
                    "role": msg["role"],
                    "content": msg["content"],
                    "label": SUBJECT_LABELS.get(subject, subject),
                    "icon": SUBJECT_ICONS.get(subject, "📚")
                })
    return {
        "username": username,
        "avatar": user_data.get("avatar"),
        "created_at": user_data.get("created_at"),
        "last_active": user_data.get("last_active"),
        "blocked": user_data.get("blocked", False),
        "suspended": user_data.get("suspended", False),
        "accent_color": user_data.get("accent_color"),
        "history": history
    }

@app.post("/api/admin/user/{username}/block")
def admin_block_user(username: str, un: str = ""):
    check_admin(un)
    if username not in users:
        raise HTTPException(404, "Пользователь не найден")
    users[username]["blocked"] = not users[username].get("blocked", False)
    save_json(USERS_FILE, users)
    return {"status": "ok", "blocked": users[username]["blocked"]}

@app.post("/api/admin/user/{username}/suspend")
def admin_suspend_user(username: str, un: str = ""):
    check_admin(un)
    if username not in users:
        raise HTTPException(404, "Пользователь не найден")
    users[username]["suspended"] = not users[username].get("suspended", False)
    save_json(USERS_FILE, users)
    return {"status": "ok", "suspended": users[username]["suspended"]}

@app.delete("/api/admin/user/{username}")
def admin_delete_user(username: str, un: str = ""):
    check_admin(un)
    if username not in users:
        raise HTTPException(404, "Пользователь не найден")
    users.pop(username, None)
    save_json(USERS_FILE, users)
    sessions.pop(username, None)
    save_json(SESSIONS_FILE, sessions)
    daily_stats.pop(username, None)
    save_json(DAILY_STATS_FILE, daily_stats)
    # удалить аватар
    for ext in (".webp", ".jpg", ".png", ".gif"):
        apath = os.path.join(AVATARS_DIR, f"{username}{ext}")
        if os.path.exists(apath):
            os.remove(apath)
    return {"status": "ok"}

@app.get("/api/admin/stats")
def admin_stats(un: str = ""):
    check_admin(un)
    total_users = len(users)
    all_msgs = []
    for uname, subs in sessions.items():
        if isinstance(subs, dict):
            for subj, msgs in subs.items():
                for m in msgs:
                    all_msgs.append({"username": uname, "subject": subj, "role": m["role"]})
    total_messages = len(all_msgs)
    df = pd.DataFrame(all_msgs)
    msgs_per_day = {}
    msgs_per_subject = {}
    msgs_per_user = {}
    active_hours = {}
    if not df.empty:
        msgs_per_subject = df["subject"].value_counts().to_dict()
        msgs_per_user = df["username"].value_counts().to_dict()
    active_today = sum(1 for u in users.values() if u.get("last_active") and
                       (datetime.now() - datetime.fromisoformat(u["last_active"])).days < 1)
    return {
        "total_users": total_users,
        "total_messages": total_messages,
        "active_today": active_today,
        "per_subject": {SUBJECT_LABELS.get(k, k): v for k, v in msgs_per_subject.items()},
        "per_user": msgs_per_user
    }

@app.get("/api/admin/users/table")
def admin_users_table(un: str = ""):
    check_admin(un)
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    result = []
    for name, data in users.items():
        last = data.get("last_active", "")
        online = False
        if last:
            try:
                online = (now - datetime.fromisoformat(last)) < timedelta(minutes=1)
            except: pass
        ds = daily_stats.get(name, {}).get(today, {})
        result.append({
            "username": name,
            "online": online,
            "blocked": data.get("blocked", False),
            "suspended": data.get("suspended", False),
            "last_active": last,
            "requests": ds.get("requests", 0),
            "responses": ds.get("responses", 0),
            "time_spent_minutes": round(ds.get("time_spent_minutes", 0), 1)
        })
    return result

@app.get("/api/admin/stats/monthly/{username}")
def admin_monthly_stats(username: str, un: str = ""):
    check_admin(un)
    if username not in users:
        raise HTTPException(404, "Пользователь не найден")
    now = datetime.now()
    entries = []
    for i in range(30):
        d = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        ds = daily_stats.get(username, {}).get(d, {})
        entries.append({
            "date": d,
            "requests": ds.get("requests", 0),
            "responses": ds.get("responses", 0),
            "time_spent_minutes": round(ds.get("time_spent_minutes", 0), 1)
        })
    entries.reverse()
    return entries

@app.get("/api/admin/stats/table/per_day")
def admin_table_per_day(un: str = ""):
    check_admin(un)
    day_totals = {}
    for name, days in daily_stats.items():
        for d, ds in days.items():
            total = ds.get("requests", 0) + ds.get("responses", 0)
            if total > 0:
                day_totals[d] = day_totals.get(d, 0) + total
    result = []
    for d in sorted(day_totals.keys())[-30:]:
        result.append({"date": d, "messages": day_totals[d]})
    return result

@app.get("/api/admin/stats/table/by_subject")
def admin_table_by_subject(un: str = ""):
    check_admin(un)
    today = datetime.now().strftime("%Y-%m-%d")
    subjects = {}
    for name, days in daily_stats.items():
        s = days.get(today, {}).get("subjects", {})
        for subj, cnt in s.items():
            subjects[subj] = subjects.get(subj, 0) + cnt
    result = []
    for s, cnt in sorted(subjects.items(), key=lambda x: -x[1]):
        result.append({"id": s, "label": SUBJECT_LABELS.get(s, s), "icon": SUBJECT_ICONS.get(s, "📚"), "messages": cnt})
    return result

@app.get("/api/admin/stats/table/active_hours")
def admin_table_active_hours(un: str = ""):
    check_admin(un)
    today = datetime.now().strftime("%Y-%m-%d")
    hour_counts = {h: 0 for h in range(24)}
    for name, days in daily_stats.items():
        hours = days.get(today, {}).get("hours", {})
        for h_str, cnt in hours.items():
            hour_counts[int(h_str)] += cnt
    result = []
    for h in range(24):
        if hour_counts[h] > 0:
            result.append({"hour": h, "messages": hour_counts[h]})
    return result

@app.get("/api/admin/stats/table/user_subjects/{username}")
def admin_table_user_subjects(username: str, un: str = ""):
    check_admin(un)
    today = datetime.now().strftime("%Y-%m-%d")
    subjects = daily_stats.get(username, {}).get(today, {}).get("subjects", {})
    result = []
    for s, cnt in sorted(subjects.items(), key=lambda x: -x[1]):
        result.append({"id": s, "label": SUBJECT_LABELS.get(s, s), "icon": SUBJECT_ICONS.get(s, "📚"), "requests": cnt})
    return result

@app.get("/api/admin/stats/table/user_hours/{username}")
def admin_table_user_hours(username: str, un: str = ""):
    check_admin(un)
    today = datetime.now().strftime("%Y-%m-%d")
    hours = daily_stats.get(username, {}).get(today, {}).get("hours", {})
    result = []
    for h in range(24):
        cnt = hours.get(str(h), 0)
        if cnt > 0:
            result.append({"hour": h, "requests": cnt})
    return result

@app.get("/api/admin/stats/table/time_spent/{username}")
def admin_table_time_spent(username: str, un: str = ""):
    check_admin(un)
    if username not in users:
        raise HTTPException(404, "Пользователь не найден")
    now = datetime.now()
    entries = []
    for i in range(30):
        d = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        ds = daily_stats.get(username, {}).get(d, {}).get("time_spent_minutes", 0)
        if ds > 0:
            entries.append({"date": d, "time_spent_minutes": round(ds, 1)})
    entries.reverse()
    return entries

PLT_COLORS = ["#2563eb", "#ef4444", "#22c55e", "#f59e0b", "#8b5cf6", "#ec4899", "#14b8a6", "#f97316", "#6366f1"]

def make_chart_bytes(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=100, bbox_inches='tight', facecolor='#f8fafc')
    plt.close(fig)
    return buf.getvalue()

@app.get("/api/admin/stats/chart/{chart_type}")
def admin_chart(chart_type: str, un: str = ""):
    check_admin(un)
    all_msgs = []
    for uname, subs in sessions.items():
        if isinstance(subs, dict):
            for subj, msgs in subs.items():
                for m in msgs:
                    all_msgs.append({"username": uname, "subject": subj, "role": m["role"]})
    df = pd.DataFrame(all_msgs)

    if chart_type == "per_day":
        fig, ax = plt.subplots(figsize=(7, 3.5))
        ax.set_title("Сообщения по дням (последние 30)", fontsize=12, fontweight='bold', pad=12)
        day_totals = {}
        for name, days in daily_stats.items():
            for d, ds in days.items():
                total = ds.get("requests", 0) + ds.get("responses", 0)
                if total > 0:
                    day_totals[d] = day_totals.get(d, 0) + total
        if day_totals:
            all_dates = sorted(day_totals.keys())[-30:]
            values = [day_totals[d] for d in all_dates]
            ax.bar(all_dates, values, color=PLT_COLORS[0], width=0.5)
            for i, v in enumerate(values):
                ax.text(i, v + 0.1, str(v), ha='center', fontsize=8)
            ax.tick_params(axis='x', rotation=45, labelsize=8)
        else:
            ax.text(0.5, 0.5, "Нет данных", ha='center', va='center', transform=ax.transAxes, fontsize=12, color='#94a3b8')
        ax.set_ylabel("Сообщения")
        fig.tight_layout()
        return Response(content=make_chart_bytes(fig), media_type="image/png")

    elif chart_type == "by_subject":
        fig, ax = plt.subplots(figsize=(5, 4))
        ax.set_title("Сообщения по предметам", fontsize=12, fontweight='bold', pad=12)
        if not df.empty:
            counts = df["subject"].value_counts()
            labels = [SUBJECT_LABELS.get(s, s) for s in counts.index]
            wedges, texts, autotexts = ax.pie(
                counts.values, labels=None, autopct='%1.0f%%',
                colors=PLT_COLORS[:len(counts)], startangle=90)
            ax.legend(wedges, labels, loc='center left', bbox_to_anchor=(1, 0.5), fontsize=8)
        else:
            ax.text(0.5, 0.5, "Нет данных", ha='center', va='center', transform=ax.transAxes, fontsize=12, color='#94a3b8')
        fig.tight_layout()
        return Response(content=make_chart_bytes(fig), media_type="image/png")

    elif chart_type == "by_user":
        fig, ax = plt.subplots(figsize=(6, 3.5))
        ax.set_title("Запросы пользователей сегодня", fontsize=12, fontweight='bold', pad=12)
        today = datetime.now().strftime("%Y-%m-%d")
        user_totals = {}
        for name, days in daily_stats.items():
            reqs = days.get(today, {}).get("requests", 0)
            if reqs > 0:
                user_totals[name] = reqs
        if user_totals:
            names = list(user_totals.keys())
            values = list(user_totals.values())
            colors = PLT_COLORS[:len(names)] if len(names) <= len(PLT_COLORS) else None
            ax.barh(names, values, color=colors or PLT_COLORS[0], height=0.5)
            for i, v in enumerate(values):
                ax.text(v + 0.1, i, str(v), va='center', fontsize=9)
        else:
            ax.text(0.5, 0.5, "Нет данных за сегодня", ha='center', va='center', transform=ax.transAxes, fontsize=12, color='#94a3b8')
        ax.set_xlabel("Запросы")
        fig.tight_layout()
        return Response(content=make_chart_bytes(fig), media_type="image/png")

    elif chart_type == "active_hours":
        fig, ax = plt.subplots(figsize=(6, 3.5))
        ax.set_title("Активность по часам сегодня", fontsize=12, fontweight='bold', pad=12)
        today = datetime.now().strftime("%Y-%m-%d")
        hour_counts = {h: 0 for h in range(24)}
        for name, days in daily_stats.items():
            hours = days.get(today, {}).get("hours", {})
            for h_str, cnt in hours.items():
                hour_counts[int(h_str)] += cnt
        vals = [hour_counts[h] for h in range(24)]
        if any(vals):
            ax.fill_between(range(24), vals, alpha=0.3, color=PLT_COLORS[0])
            ax.plot(range(24), vals, color=PLT_COLORS[0], linewidth=2)
        else:
            ax.plot([], [])
            ax.text(0.5, 0.5, "Нет данных за сегодня", ha='center', va='center', transform=ax.transAxes, fontsize=12, color='#94a3b8')
        ax.set_xlabel("Час суток")
        ax.set_ylabel("Сообщения")
        ax.set_xticks(range(24))
        ax.set_xticklabels([str(h) for h in range(24)], fontsize=7)
        fig.tight_layout()
        return Response(content=make_chart_bytes(fig), media_type="image/png")

    elif chart_type == "by_subject_today":
        fig, ax = plt.subplots(figsize=(5, 4))
        ax.set_title("Сообщения по предметам сегодня", fontsize=12, fontweight='bold', pad=12)
        today = datetime.now().strftime("%Y-%m-%d")
        subject_totals = {}
        for name, days in daily_stats.items():
            subjects = days.get(today, {}).get("subjects", {})
            for subj, cnt in subjects.items():
                subject_totals[subj] = subject_totals.get(subj, 0) + cnt
        if subject_totals:
            labels = [SUBJECT_LABELS.get(s, s) for s in subject_totals.keys()]
            values = list(subject_totals.values())
            wedges, texts, autotexts = ax.pie(
                values, labels=None, autopct='%1.0f%%',
                colors=PLT_COLORS[:len(subject_totals)], startangle=90)
            ax.legend(wedges, labels, loc='center left', bbox_to_anchor=(1, 0.5), fontsize=8)
        else:
            ax.text(0.5, 0.5, "Нет данных за сегодня", ha='center', va='center', transform=ax.transAxes, fontsize=12, color='#94a3b8')
        fig.tight_layout()
        return Response(content=make_chart_bytes(fig), media_type="image/png")

    elif chart_type == "user_pie":
        fig, ax = plt.subplots(figsize=(5, 4))
        ax.set_title("Запросы пользователей сегодня", fontsize=12, fontweight='bold', pad=12)
        today = datetime.now().strftime("%Y-%m-%d")
        user_reqs = {}
        for name, days in daily_stats.items():
            reqs = days.get(today, {}).get("requests", 0)
            if reqs > 0:
                user_reqs[name] = reqs
        if user_reqs:
            names = list(user_reqs.keys())
            values = list(user_reqs.values())
            wedges, texts, autotexts = ax.pie(
                values, labels=None, autopct='%1.0f%%',
                colors=PLT_COLORS[:len(names)], startangle=90)
            ax.legend(wedges, names, loc='center left', bbox_to_anchor=(1, 0.5), fontsize=8)
        else:
            ax.text(0.5, 0.5, "Нет данных за сегодня", ha='center', va='center', transform=ax.transAxes, fontsize=12, color='#94a3b8')
        fig.tight_layout()
        return Response(content=make_chart_bytes(fig), media_type="image/png")

    elif chart_type == "user_subjects":
        fig, ax = plt.subplots(figsize=(6, 3.5))
        ax.set_title("Запросы по предметам", fontsize=12, fontweight='bold', pad=12)
        today = datetime.now().strftime("%Y-%m-%d")
        subjects = {}
        for name, days in daily_stats.items():
            s = days.get(today, {}).get("subjects", {})
            for subj, cnt in s.items():
                subjects[subj] = subjects.get(subj, 0) + cnt
        if subjects:
            labels = [SUBJECT_LABELS.get(s, s) for s in subjects.keys()]
            values = list(subjects.values())
            colors = PLT_COLORS[:len(labels)]
            ax.bar(labels, values, color=colors, width=0.5)
            for i, v in enumerate(values):
                ax.text(i, v + 0.1, str(v), ha='center', fontsize=9)
            ax.tick_params(axis='x', rotation=30)
        else:
            ax.text(0.5, 0.5, "Нет данных за сегодня", ha='center', va='center', transform=ax.transAxes, fontsize=12, color='#94a3b8')
        ax.set_ylabel("Запросы")
        fig.tight_layout()
        return Response(content=make_chart_bytes(fig), media_type="image/png")

    elif chart_type == "user_subjects_for":
        fig, ax = plt.subplots(figsize=(6, 3.5))
        ax.set_title("Запросы по предметам", fontsize=12, fontweight='bold', pad=12)
        today = datetime.now().strftime("%Y-%m-%d")
        subject_label = request.path_params.get("username", "")
        # We don't know the username from chart_type alone, so this is handled by a separate endpoint
        ax.text(0.5, 0.5, "Используйте /api/admin/stats/chart/user_subjects/{username}", ha='center', va='center', transform=ax.transAxes, fontsize=10, color='#94a3b8')
        fig.tight_layout()
        return Response(content=make_chart_bytes(fig), media_type="image/png")

    raise HTTPException(404, "Неизвестный тип графика")

@app.get("/api/admin/stats/chart/user_subjects/{username}")
def admin_chart_user_subjects(username: str, un: str = ""):
    check_admin(un)
    today = datetime.now().strftime("%Y-%m-%d")
    subjects = daily_stats.get(username, {}).get(today, {}).get("subjects", {})
    fig, ax = plt.subplots(figsize=(6, 3.5))
    ax.set_title(f"Предметы — {username}", fontsize=12, fontweight='bold', pad=12)
    if subjects:
        labels = [SUBJECT_LABELS.get(s, s) for s in subjects.keys()]
        values = list(subjects.values())
        colors = PLT_COLORS[:len(labels)]
        ax.bar(labels, values, color=colors, width=0.5)
        for i, v in enumerate(values):
            ax.text(i, v + 0.1, str(v), ha='center', fontsize=9)
        ax.tick_params(axis='x', rotation=30)
    else:
        ax.text(0.5, 0.5, "Нет данных за сегодня", ha='center', va='center', transform=ax.transAxes, fontsize=12, color='#94a3b8')
    ax.set_ylabel("Запросы")
    fig.tight_layout()
    return Response(content=make_chart_bytes(fig), media_type="image/png")

@app.get("/api/admin/stats/chart/user_hours/{username}")
def admin_chart_user_hours(username: str, un: str = ""):
    check_admin(un)
    today = datetime.now().strftime("%Y-%m-%d")
    hours = daily_stats.get(username, {}).get(today, {}).get("hours", {})
    fig, ax = plt.subplots(figsize=(6, 3.5))
    ax.set_title(f"Активность по часам — {username}", fontsize=12, fontweight='bold', pad=12)
    hour_counts = {h: 0 for h in range(24)}
    for h_str, cnt in hours.items():
        hour_counts[int(h_str)] = cnt
    vals = [hour_counts[h] for h in range(24)]
    if any(vals):
        ax.fill_between(range(24), vals, alpha=0.3, color=PLT_COLORS[0])
        ax.plot(range(24), vals, color=PLT_COLORS[0], linewidth=2)
    else:
        ax.plot([], [])
        ax.text(0.5, 0.5, "Нет данных за сегодня", ha='center', va='center', transform=ax.transAxes, fontsize=12, color='#94a3b8')
    ax.set_xlabel("Час суток")
    ax.set_ylabel("Запросы")
    ax.set_xticks(range(24))
    ax.set_xticklabels([str(h) for h in range(24)], fontsize=7)
    fig.tight_layout()
    return Response(content=make_chart_bytes(fig), media_type="image/png")

@app.get("/api/chart")
def generate_chart(type: str, expr: str = "", data: str = "", labels: str = "", values: str = "", x: str = "", y: str = ""):
    CHART_CACHE_DIR = "uploads/charts"
    params = f"{type}|{expr}|{data}|{labels}|{values}|{x}|{y}"
    cache_key = hashlib.md5(params.encode()).hexdigest()
    cache_path = os.path.join(CHART_CACHE_DIR, f"{cache_key}.png")

    if os.path.exists(cache_path):
        return FileResponse(cache_path, media_type="image/png")

    fig, ax = plt.subplots(figsize=(6, 4))
    ax.set_facecolor('#f8fafc')
    fig.patch.set_facecolor('#f8fafc')

    try:
        if type == "plot" and expr:
            xs = np.linspace(-10, 10, 400)
            safe = {"x": xs, "np": np, "sin": np.sin, "cos": np.cos, "tan": np.tan,
                    "sqrt": np.sqrt, "log": np.log, "exp": np.exp, "pi": np.pi, "e": np.e}
            safe_expr = expr.replace("^", "**")
            ys = eval(safe_expr, {"__builtins__": {}}, safe)
            ax.plot(xs, ys, color='#2563eb', linewidth=2)
            ax.axhline(0, color='#cbd5e1', linewidth=0.5)
            ax.axvline(0, color='#cbd5e1', linewidth=0.5)
            ax.set_title(f"y = {expr}", fontsize=12, fontweight='bold')
            ax.grid(True, alpha=0.3)
            ax.set_xlabel("x"); ax.set_ylabel("y")

        elif type == "histogram" and data:
            nums = [float(v.strip()) for v in data.split(",") if v.strip()]
            ax.hist(nums, bins=min(15, len(nums)), color='#2563eb', edgecolor='white', alpha=0.8)
            ax.set_title("Гистограмма", fontsize=12, fontweight='bold')
            ax.set_xlabel("Значения"); ax.set_ylabel("Частота")

        elif type == "bar" and labels and values:
            lbls = [l.strip() for l in labels.split(",") if l.strip()]
            vals = [float(v.strip()) for v in values.split(",") if v.strip()]
            colors = ["#2563eb", "#ef4444", "#22c55e", "#f59e0b", "#8b5cf6", "#ec4899", "#14b8a6", "#f97316"]
            bars = ax.bar(lbls[:len(vals)], vals[:len(lbls)], color=colors[:max(len(lbls), len(vals))], width=0.5)
            ax.set_title("Столбчатая диаграмма", fontsize=12, fontweight='bold')
            for bar, v in zip(bars, vals[:len(lbls)]):
                ax.text(bar.get_x() + bar.get_width()/2, v + 0.3, str(v), ha='center', fontsize=9)

        elif type == "scatter" and x and y:
            xs = [float(v.strip()) for v in x.split(",") if v.strip()]
            ys = [float(v.strip()) for v in y.split(",") if v.strip()]
            ax.scatter(xs[:min(len(xs), len(ys))], ys[:min(len(xs), len(ys))], color='#2563eb', s=60, alpha=0.7)
            ax.set_title("Точечный график", fontsize=12, fontweight='bold')
            ax.grid(True, alpha=0.3)
            ax.set_xlabel("x"); ax.set_ylabel("y")

        else:
            ax.text(0.5, 0.5, "Неверные параметры", ha='center', va='center', transform=ax.transAxes, fontsize=12, color='#94a3b8')

    except Exception as e:
        ax.clear()
        ax.text(0.5, 0.5, f"Ошибка: {str(e)}", ha='center', va='center', transform=ax.transAxes, fontsize=10, color='#ef4444')

    fig.tight_layout()
    os.makedirs(CHART_CACHE_DIR, exist_ok=True)
    fig.savefig(cache_path, dpi=100, bbox_inches='tight', facecolor='#f8fafc')
    plt.close(fig)
    return FileResponse(cache_path, media_type="image/png")


@app.get("/admin")
async def admin_page():
    return FileResponse("static/admin.html")

@app.get("/")
async def index():
    return FileResponse("static/index.html")
