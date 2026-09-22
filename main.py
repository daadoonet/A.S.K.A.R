import os
import json
import logging
import sqlite3
import asyncio
import urllib.parse
import hmac
import uuid
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, status
import httpx
import jdatetime
from google import genai
from google.oauth2 import service_account
from logging.handlers import RotatingFileHandler
from googleapiclient.discovery import build

# تنظیم سیستم لاگینگ با چرخش فایل (حداکثر ۵ مگابایت و نگهداری ۵ فایل بکاپ)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        RotatingFileHandler(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_activity.log"),
            maxBytes=5 * 1024 * 1024,  # 5 MB
            backupCount=5,
            encoding="utf-8"
        ),
        logging.StreamHandler()
    ]
)

load_dotenv()

def sanitize_calendar_id(cal_id: str | None) -> str:
    """استخراج شناسه واقعی تقویم در صورت وارد شدن آدرس Embed گوگل"""
    if not cal_id:
        return "primary"
    cal_id = cal_id.strip()
    if "calendar.google.com" in cal_id:
        try:
            parsed = urllib.parse.urlparse(cal_id)
            query = urllib.parse.parse_qs(parsed.query)
            if "src" in query and query["src"]:
                return urllib.parse.unquote(query["src"][0])
        except Exception:
            pass
    return cal_id

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
CALENDAR_ID = sanitize_calendar_id(os.getenv("GOOGLE_CALENDAR_ID"))
TIMEZONE = os.getenv("TIMEZONE", "Asia/Tehran")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "events.db")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET")


# لیست کلمات کلیدی پایه برای فیلتر اولیه پیام‌ها جهت کاهش مصرف توکن
KEYWORDS_FILTER = [
    # کلمات مرتبط با فناوری اطلاعات و پشتیبانی (FAQ)
    "پرینتر", "چاپ", "کابل", "شبکه", "وایفای", "وای‌فای", "قطع", "پسورد", "ریست", "رمز", "مودم", "درس افزار", "ال ام اس", "lms", 
    "سیستم", "ویندوز", "کامپیوتر", "اینترنت", "اتصال", "پرینت", "کارتریج","لپ تاپ", "پیام رسان", "تلگرام", "واتساپ", "ایمیل", "جیمیل", "gmail", "اکانت", "ورود", "لاگین", "پرداخت", "فاکتور",
    # کلمات مرتبط با تسک و تقویم و جلسات
    "جلسه", "میتینگ", "قرار", "ساعت", "هماهنگ", "فردا", "پس‌فردا", "پس فردا", "امروز",
    "شنبه", "یکشنبه", "دوشنبه", "سه شنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "پنج‌شنبه", "جمعه",
    "کلاس", "سرور", "تسک", "برنامه", "تقویم", "کالندر", "رویداد", "شروع", "پایان", "هفته","اردو",
    # کلمات انگلیسی متداول
    "wifi", "internet", "printer", "password", "reset", "meeting", "session", "server", "task", "calendar", "event"
]

WIFI_KEYWORDS = [
    "وایفای", "وای فای", "wifi", "ssid", "رمز وایفای", "رمز وای فای", 
    "پسورد وایفای", "پسورد وای فای", "دبستان", "دبیرستان", "مهمان"
]

CALENDAR_KEYWORDS = [
    "جلسه", "میتینگ", "قرار", "ساعت", "هماهنگ", "فردا", "پس‌فردا", "پس فردا", "امروز",
    "شنبه", "یکشنبه", "دوشنبه", "سه شنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "پنج‌شنبه", "جمعه",
    "کلاس", "سرور", "تسک", "برنامه", "تقویم", "کالندر", "رویداد", "شروع", "پایان", "هفته", "اردو",
    "meeting", "session", "task", "calendar", "event"
]

_kb_raw_cache = {"mtime": 0, "data": {}}

def load_raw_knowledge_base() -> dict:
    """خواندن و کش کردن ساختار فایل knowledge_base.json"""
    kb_path = os.path.join(BASE_DIR, "knowledge_base.json")
    if not os.path.exists(kb_path):
        return {}
    try:
        mtime = os.path.getmtime(kb_path)
        if mtime != _kb_raw_cache["mtime"]:
            with open(kb_path, "r", encoding="utf-8") as f:
                _kb_raw_cache["data"] = json.load(f)
            _kb_raw_cache["mtime"] = mtime
        return _kb_raw_cache["data"]
    except Exception as e:
        logging.error(f"❌ خطا در بارگذاری فایل knowledge_base.json: {e}")
        return _kb_raw_cache["data"]

def normalize_text(text: str) -> str:
    """نرمال‌سازی متن فارسی برای تطابق دقیق‌تر"""
    if not text:
        return ""
    norm = text.lower().replace("‌", " ").replace("آ", "ا").replace("ي", "ی").replace("ك", "ک")
    import re
    return re.sub(r'[^\w\s]', ' ', norm)

def match_any_keyword(keywords: list[str], normalized_text: str) -> bool:
    """تطابق دقیق کلمات کلیدی با مرز کلمه جهت جلوگیری از تشابهات ناخواسته (مثل رمز و قرمز)"""
    import re
    padded = f" {normalized_text} "
    for kw in keywords:
        k = kw.strip()
        if not k:
            continue
        pattern = r'(?:\s|^)' + re.escape(k) + r'(?:\s|$)'
        if re.search(pattern, padded):
            return True
    return False

def get_relevant_knowledge_base(user_text: str, history_text: str = "") -> str:
    """
    تزریق تطبیقی و انتخابی پایگاه دانش (Selective KB Injection) جهت کاهش ۷۰٪ توکن ورودی.
    اگر درخواست مربوط به جلسه باشد، هیچ پایگاه دانشی فرستاده نمی‌شود.
    اگر مربوط به موضوع خاصی باشد، فقط همان بخش فرستاده می‌شود.
    """
    data = load_raw_knowledge_base()
    if not data:
        return ""

    combined = normalize_text(f"{user_text} {history_text}")
    lines = []

    # ۱. بررسی شبکه‌های وای‌فای
    if match_any_keyword(WIFI_KEYWORDS, combined):
        wifi_list = data.get("wifi_networks", [])
        if wifi_list:
            lines.append("اطلاعات شبکه‌های وای‌فای (Wi-Fi):")
            for w in wifi_list:
                lines.append(f"- {w.get('title')}: SSID: `{w.get('ssid', '-')}` | رمز: `{w.get('password', '-')}` | راهنما: {w.get('guide', '')}")

    # ۲. بررسی راهنماهای عیب‌یابی متداول
    faq_list = data.get("faq_troubleshooting", [])
    for faq in faq_list:
        faq_kws = [normalize_text(k).strip() for k in faq.get("keywords", [])]
        if match_any_keyword(faq_kws, combined):
            lines.append(f"\nراهنمای عیب‌یابی موضوع: {faq.get('topic')}")
            steps = faq.get("diagnostic_steps", [])
            if steps:
                lines.append("مراحل بررسی: " + " | ".join(steps[:3]))
            if faq.get("reply_template"):
                lines.append(f"الگوی پاسخ پیشنهادی: \"{faq.get('reply_template')}\"")

    # ۳. بررسی پاسخ‌های سریع (Quick QA)
    qa_list = data.get("quick_qa", [])
    for qa in qa_list:
        q_norm = normalize_text(qa.get("question", ""))
        words = [w for w in q_norm.split() if len(w) > 3]
        if match_any_keyword(words, combined):
            lines.append(f"\nپاسخ سریع: {qa.get('question')} -> {qa.get('answer')}")

    if not lines:
        return ""
    return "### Technical Knowledge Base (راهنمای فنی مرتبط):\n" + "\n".join(lines)

def handle_quick_chit_chat(text: str) -> str | None:
    """
    پاسخگویی فوری و محلی با ۰ توکن به احوالپرسی‌های کوتاه، تشکر، تایید و خداحافظی
    """
    if not text:
        return None
    
    clean_text = text.strip()
    norm = clean_text.lower().replace("‌", " ").replace("آ", "ا").replace("ي", "ی").replace("ك", "ک")
    import re
    norm_words = re.sub(r'[^\w\s]', '', norm).strip()
    words = norm_words.split()
    
    # اگر پیام بیش از ۵ کلمه باشد، احتمالاً حاوی توضیحات فنی است و به هوش مصنوعی سپرده می‌شود
    if len(words) > 5:
        return None

    # بررسی تشکر و قدردانی
    gratitude_phrases = [
        "مرسی", "ممنون", "ممنونم", "تشکر", "دستت درد نکنه", "دستتون درد نکنه", 
        "دمت گرم", "سپاس", "لطف کردی", "خیلی ممنون", "متشکرم", "خدا خیرت بده"
    ]
    for p in gratitude_phrases:
        if norm_words == p or norm_words.startswith(p + " ") or norm_words.endswith(" " + p):
            return "خواهش می‌کنم، اگر مورد دیگه‌ای بود در خدمتم! ارادت 🙏"

    # بررسی تایید و اختتام
    ack_phrases = ["باشه", "اوکی", "اوکیه", "حله", "چشم", "عالیه", "خیلی خب", "باش", "حتما"]
    if norm_words in ack_phrases:
        return "ارادت، در خدمتم 🙏"

    # بررسی خداحافظی و خسته‌نباشید
    farewell_phrases = ["خداحافظ", "خداحافظی", "فعلا", "خسته نباشی", "خسته نباشید", "روز خوش", "شب خوش"]
    for p in farewell_phrases:
        if norm_words == p or norm_words.startswith(p + " "):
            return "سلامت باشی، روزت خوش! ارادت 🙏"

    # بررسی سلام خالی بدون سوال
    greeting_phrases = ["سلام", "سلام علیکم", "درود", "صبح بخیر", "عصر بخیر", "وقت بخیر", "سلام وقت بخیر"]
    if norm_words in greeting_phrases:
        return "سلام! روزت بخیر، چطور می‌تونم کمکت کنم؟ مشکلی یا درخواستی هست بفرمایید در خدمتم 🙏"

    return None

def should_process_message(text: str) -> bool:
    """
    بررسی اینکه آیا پیام حاوی کلمات کلیدی مربوط به کار هست یا خیر.
    """
    if not text:
        return False
    
    normalized_text = normalize_text(text)
    
    data = load_raw_knowledge_base()
    extra_kws = []
    for faq in data.get("faq_troubleshooting", []):
        extra_kws.extend(faq.get("keywords", []))
    
    combined_keywords = KEYWORDS_FILTER + extra_kws
    return match_any_keyword(combined_keywords, normalized_text)

# کلاینت هوش مصنوعی
client_ai = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# اتصال به Google Calendar
calendar_service = None
try:
    if os.path.exists(os.path.join(BASE_DIR, 'credentials.json')):
        SCOPES = ['https://www.googleapis.com/auth/calendar']
        creds = service_account.Credentials.from_service_account_file(os.path.join(BASE_DIR, 'credentials.json'), scopes=SCOPES)
        calendar_service = build('calendar', 'v3', credentials=creds)
        logging.info("✅ اتصال سرویس اکانت گوگل برقرار شد.")
    else:
        logging.warning("⚠️ فایل credentials.json یافت نشد.")
except Exception as e:
    logging.error(f"❌ خطای اتصال به Google Calendar: {e}")

# ----------------------------------------------------
# لایه پایگاه داده (SQLite) جهت ذخیره‌سازی پایدار رویدادها
# ----------------------------------------------------
SESSION_TIMEOUT_MINUTES = 15

def init_db():
    """ایجاد جداول پایگاه داده در صورت عدم وجود و اعمال ارتقاها"""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pending_events (
                event_id TEXT PRIMARY KEY,
                summary TEXT,
                start_time TEXT,
                end_time TEXT,
                description TEXT,
                user_chat_id TEXT,
                business_connection_id TEXT,
                sender_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # ارتقای خودکار ساختار جدول در صورت استفاده از دیتابیس قدیمی
        cursor.execute("PRAGMA table_info(pending_events)")
        existing_cols = [info[1] for info in cursor.fetchall()]
        if "user_chat_id" not in existing_cols:
            cursor.execute("ALTER TABLE pending_events ADD COLUMN user_chat_id TEXT")
        if "business_connection_id" not in existing_cols:
            cursor.execute("ALTER TABLE pending_events ADD COLUMN business_connection_id TEXT")
        if "sender_name" not in existing_cols:
            cursor.execute("ALTER TABLE pending_events ADD COLUMN sender_name TEXT")

        # جدول تاریخچه گفتگو برای پشتیبانی از چند نوبتی (Multi-turn Context)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_chat_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_chat_history_user ON chat_history (user_chat_id, id DESC)
        """)

        # جدول تنظیمات سیستم (برای ذخیره پایدار وضعیت فعال/غیرفعال بودن پاسخگویی ربات)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('bot_active', 'true')
        """)

        # جدول ثبت آمار و مصرف توکن‌های API جمنای
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS api_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model TEXT,
                prompt_tokens INTEGER DEFAULT 0,
                candidate_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_api_usage_created ON api_usage (created_at)
        """)
        conn.commit()
    logging.info("📦 پایگاه داده رویدادها، تاریخچه گفتگو و تنظیمات سیستم آماده‌سازی شد.")


# لیست مدل‌های عمومی جمنای قابل انتخاب توسط ادمین
AVAILABLE_GEMINI_MODELS = [
    ("gemini-3.8-flash",         "🆕⚡ Gemini 3.8 Flash (پیشنهادی - هوشمندترین Flash)"),
    ("gemini-3.7-flash",         "🔥 Gemini 3.7 Flash (کدنویسی و اتوماسیون)"),
    ("gemini-3.6-flash",         "⚡ Gemini 3.6 Flash (سریع و همه‌منظوره)"),
    ("gemini-3.5-flash",         "💨 Gemini 3.5 Flash (سبک - توان بالا)"),
    ("gemini-3.5-flash-lite",    "🪶 Gemini 3.5 Flash-Lite (سریع‌ترین و ارزان‌ترین)"),
    ("gemini-3.1-flash-lite",    "💰 Gemini 3.1 Flash-Lite (کم‌مصرف‌ترین)"),
    ("gemini-3.1-pro-preview",   "🧠 Gemini 3.1 Pro (قوی‌ترین - Preview)"),
    ("gemini-2.5-flash",         "🔷 Gemini 2.5 Flash (نسل قبل)"),
    ("gemini-2.5-flash-lite",    "🔹 Gemini 2.5 Flash-Lite (نسل قبل - سبک)"),
    ("gemini-2.5-pro",           "🔶 Gemini 2.5 Pro (نسل قبل - قوی)"),
]

def get_bot_active_status() -> bool:
    """بررسی وضعیت روشن یا خاموش بودن پاسخگویی خودکار ربات"""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM bot_settings WHERE key = 'bot_active'")
            row = cursor.fetchone()
            if row and row[0] == 'false':
                return False
    except Exception as e:
        logging.error(f"❌ خطا در خواندن وضعیت ربات: {e}")
    return True

def get_active_gemini_model() -> str:
    """خواندن مدل جمنای انتخابی ادمین از دیتابیس (با fallback به متغیر محیطی)"""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM bot_settings WHERE key = 'gemini_model'")
            row = cursor.fetchone()
            if row and row[0]:
                return row[0]
    except Exception as e:
        logging.error(f"❌ خطا در خواندن مدل جمنای از دیتابیس: {e}")
    return GEMINI_MODEL

def set_active_gemini_model(model: str):
    """ذخیره مدل جمنای انتخابی ادمین در دیتابیس"""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO bot_settings (key, value, updated_at)
            VALUES ('gemini_model', ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
        """, (model,))
        conn.commit()
    logging.info(f"🤖 مدل جمنای توسط ادمین تغییر یافت: {model}")


def set_bot_active_status(active: bool):
    """تنظیم وضعیت فعال یا متوقف بودن پاسخگویی خودکار ربات"""
    val_str = 'true' if active else 'false'
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO bot_settings (key, value, updated_at)
            VALUES ('bot_active', ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
        """, (val_str,))
        conn.commit()

def record_api_usage(model: str, prompt_tokens: int, candidate_tokens: int, total_tokens: int):
    """ثبت مصرف توکن فراخوانی جمنای در دیتابیس"""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO api_usage (model, prompt_tokens, candidate_tokens, total_tokens)
                VALUES (?, ?, ?, ?)
            """, (model, prompt_tokens, candidate_tokens, total_tokens))
            conn.commit()
    except Exception as e:
        logging.error(f"❌ خطا در ثبت مصرف API: {e}")

def get_daily_stats() -> dict:
    """محاسبه آمار پیام‌ها و مصرف API در طول روز جاری"""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            # پیام‌های دریافتی از کاربران امروز
            cursor.execute("SELECT COUNT(*) FROM chat_history WHERE role = 'user' AND date(created_at, 'localtime') = date('now', 'localtime')")
            user_msgs = cursor.fetchone()[0]
            
            # پاسخ‌های خودکار هوش مصنوعی امروز
            cursor.execute("SELECT COUNT(*) FROM chat_history WHERE role = 'model' AND date(created_at, 'localtime') = date('now', 'localtime')")
            model_replies = cursor.fetchone()[0]
            
            # تعداد کاربران منحصربه‌فرد امروز
            cursor.execute("SELECT COUNT(DISTINCT user_chat_id) FROM chat_history WHERE date(created_at, 'localtime') = date('now', 'localtime')")
            unique_users = cursor.fetchone()[0]
            
            # فراخوانی‌های جمنای و توکن‌های مصرفی امروز
            cursor.execute("""
                SELECT COUNT(*), COALESCE(SUM(prompt_tokens), 0), COALESCE(SUM(candidate_tokens), 0), COALESCE(SUM(total_tokens), 0)
                FROM api_usage
                WHERE date(created_at, 'localtime') = date('now', 'localtime')
            """)
            row = cursor.fetchone()
            api_calls = row[0]
            prompt_tokens = row[1]
            candidate_tokens = row[2]
            total_tokens = row[3]
            
            # جلسات در انتظار تایید ادمین
            cursor.execute("SELECT COUNT(*) FROM pending_events")
            pending_count = cursor.fetchone()[0]
            
            return {
                "user_messages_today": user_msgs,
                "model_replies_today": model_replies,
                "total_messages_today": user_msgs + model_replies,
                "unique_users_today": unique_users,
                "api_calls_today": api_calls,
                "prompt_tokens_today": prompt_tokens,
                "candidate_tokens_today": candidate_tokens,
                "total_tokens_today": total_tokens,
                "pending_events_count": pending_count,
                "is_active": get_bot_active_status()
            }
    except Exception as e:
        logging.error(f"❌ خطا در محاسبه آمار روزانه: {e}")
        return {
            "user_messages_today": 0, "model_replies_today": 0, "total_messages_today": 0,
            "unique_users_today": 0, "api_calls_today": 0, "prompt_tokens_today": 0,
            "candidate_tokens_today": 0, "total_tokens_today": 0, "pending_events_count": 0,
            "is_active": get_bot_active_status()
        }

def save_pending_event(event_id: str, ev: dict, user_chat_id: str = None, business_connection_id: str = None, sender_name: str = None):
    """ذخیره رویداد معلق در دیتابیس به همراه اطلاعات کاربر متقاضی"""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO pending_events (
                event_id, summary, start_time, end_time, description,
                user_chat_id, business_connection_id, sender_name
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            event_id,
            ev.get('summary', ''),
            ev.get('start_time', ''),
            ev.get('end_time', ''),
            ev.get('description', ''),
            str(user_chat_id) if user_chat_id else None,
            business_connection_id,
            sender_name
        ))
        conn.commit()

def get_pending_event(event_id: str) -> dict | None:
    """دریافت اطلاعات رویداد معلق از دیتابیس"""
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM pending_events WHERE event_id = ?", (event_id,))
        row = cursor.fetchone()
        if row:
            return dict(row)
    return None

def delete_pending_event(event_id: str):
    """حذف رویداد معلق از دیتابیس"""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM pending_events WHERE event_id = ?", (event_id,))
        conn.commit()

def save_chat_message(user_chat_id: str | int, role: str, content: str):
    """ذخیره پیام در تاریخچه گفتگو جهت حفظ زمینه مکالمه"""
    if not user_chat_id or not content:
        return
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO chat_history (user_chat_id, role, content)
                VALUES (?, ?, ?)
            """, (str(user_chat_id), role, content))
            conn.commit()
    except Exception as e:
        logging.error(f"❌ خطای ذخیره پیام در تاریخچه: {e}")

def is_user_in_active_session(user_chat_id: str | int, timeout_minutes: int = SESSION_TIMEOUT_MINUTES) -> bool:
    """بررسی اینکه آیا کاربر در حال حاضر در یک جلسه گفتگوی فعال است یا خیر"""
    if not user_chat_id:
        return False
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT created_at FROM chat_history
                WHERE user_chat_id = ?
                ORDER BY id DESC LIMIT 1
            """, (str(user_chat_id),))
            row = cursor.fetchone()
            if not row:
                return False
            last_time_str = row[0]
            # پشتیبانی از هر دو فرمت SQLite (با یا بدون میکروثانیه)
            try:
                last_time = datetime.strptime(last_time_str, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)
            except ValueError:
                last_time = datetime.strptime(last_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            diff_seconds = (datetime.now(timezone.utc) - last_time).total_seconds()
            return diff_seconds < (timeout_minutes * 60)
    except Exception as e:
        logging.error(f"❌ خطای بررسی وضعیت گفتگوی فعال: {e}")
        return False

def get_recent_chat_history(user_chat_id: str | int, limit: int = 6) -> list[dict]:
    """دریافت آخرین پیام‌های کاربر به ترتیب زمانی"""
    if not user_chat_id:
        return []
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT role, content FROM chat_history
                WHERE user_chat_id = ?
                ORDER BY id DESC LIMIT ?
            """, (str(user_chat_id), limit))
            rows = cursor.fetchall()
            return [dict(r) for r in reversed(rows)]
    except Exception as e:
        logging.error(f"❌ خطای دریافت تاریخچه گفتگو: {e}")
        return []

def cleanup_expired_events(hours: int = 48):
    """پاکسازی رویدادها و تاریخچه قدیمی‌تر از ۴۸ ساعت"""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM pending_events WHERE created_at <= datetime('now', ?)", (f"-{hours} hours",))
            cursor.execute("DELETE FROM chat_history WHERE created_at <= datetime('now', ?)", (f"-{hours} hours",))
            cursor.execute("DELETE FROM api_usage WHERE created_at <= datetime('now', '-30 days')")
            conn.commit()
    except Exception as e:
        logging.warning(f"⚠️ خطای پاکسازی رکوردهای منقضی شده: {e}")

# ----------------------------------------------------
# مدیریت طول عمر برنامه و کلاینت سراسری HTTP
# ----------------------------------------------------
http_client: httpx.AsyncClient | None = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    init_db()
    cleanup_expired_events()
    http_client = httpx.AsyncClient(timeout=15.0)
    logging.info("🚀 کلاینت مشترک HTTP راه‌اندازی شد.")
    if TELEGRAM_WEBHOOK_SECRET:
        logging.info("🔒 احراز هویت وب‌هوک تلگرام فعال است (X-Telegram-Bot-Api-Secret-Token).")
    else:
        logging.warning("⚠️ هشدار امنیتی: TELEGRAM_WEBHOOK_SECRET تنظیم نشده است. احراز هویت وب‌هوک غیرفعال است.")
    yield
    if http_client:
        await http_client.aclose()
        logging.info("🛑 کلاینت مشترک HTTP بسته شد.")

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return {
        "status": "online",
        "app": "A.S.K.A.R",
        "service": "IT Helpdesk & Calendar Bot",
        "google_calendar_connected": calendar_service is not None
    }

@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "database": os.path.exists(DB_FILE),
        "google_calendar": calendar_service is not None,
        "gemini_configured": client_ai is not None
    }

SYSTEM_PROMPT = """You are an expert IT Administrator managing school and office infrastructure.
Reply in friendly, conversational Persian (فارسی خودمونی، گرم و فنی).
Time: Gregorian: {current_date} ({day_name}) | Shamsi: {shamsi_date} ({shamsi_day_name}) | Timezone: {timezone}.

{knowledge_base}

### Rules:
1. Meetings/Tasks:
- Missing/vague time: type="ask_clarification", ask time friendly.
- Exact time mentioned: type="task", ISO datetime, default duration 30m. In reply_to_user, NEVER say "ثبت شد" (never say it is booked). Say casually that you saw the message and will check the calendar and let them know.
2. Technical Support:
- Follow technical guides if provided. If unresolved or unknown: type="escalate", notify_admin=true.
3. Chit-Chat / Greetings / Ignorable:
- type="ignore", reply_to_user=null.

### Strict JSON Output:
Return ONLY valid JSON matching this schema:
{{
  "type": "faq" | "task" | "ask_clarification" | "escalate" | "ignore",
  "reply_to_user": "Technical yet friendly Persian response or null",
  "notify_admin": true | false,
  "admin_notification_text": "Short Persian alert for admin or null",
  "calendar_event": {{
      "summary": "Meeting title",
      "start_time": "YYYY-MM-DDTHH:MM:SS",
      "end_time": "YYYY-MM-DDTHH:MM:SS",
      "description": "Details"
  }}
}}"""

def _generate_gemini_content(contents: str, system_instruction: str = None, model: str = None) -> tuple[str, int, int, int]:
    """اجرای همگام فراخوانی مدل جمنای در ترد مجزا با تفکیک دستورالعمل سیستم جهت کشینگ بهینه"""
    from google.genai import types
    active_model = model or get_active_gemini_model()
    config = types.GenerateContentConfig(
        response_mime_type='application/json',
        system_instruction=system_instruction
    )
    response = client_ai.models.generate_content(
        model=active_model,
        contents=contents,
        config=config
    )
    prompt_tokens = 0
    candidate_tokens = 0
    total_tokens = 0
    if hasattr(response, "usage_metadata") and response.usage_metadata:
        prompt_tokens = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
        candidate_tokens = getattr(response.usage_metadata, "candidates_token_count", 0) or 0
        total_tokens = getattr(response.usage_metadata, "total_token_count", 0) or 0
    return response.text, prompt_tokens, candidate_tokens, total_tokens, active_model

async def call_gemini(user_text: str, history: list[dict] = None, max_retries: int = 3) -> dict:
    if not client_ai:
        raise RuntimeError("کلید GEMINI_API_KEY تنظیم نشده است.")
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d %H:%M")
    day_name = now.strftime("%A")
    
    try:
        j_now = jdatetime.datetime.now()
        shamsi_date = j_now.strftime("%Y/%m/%d %H:%M")
        persian_weekdays = {
            "Saturday": "شنبه", "Sunday": "یکشنبه", "Monday": "دوشنبه",
            "Tuesday": "سه‌شنبه", "Wednesday": "چهارشنبه", "Thursday": "پنج‌شنبه", "Friday": "جمعه"
        }
        shamsi_day_name = persian_weekdays.get(day_name, day_name)
    except Exception:
        shamsi_date = today_str
        shamsi_day_name = day_name

    # تزریق فقط بخش‌های مرتبط پایگاه دانش متناسب با متن کاربر
    history_user_text = " ".join([m.get("content", "") for m in (history or []) if m.get("role") == "user"])
    kb_text = get_relevant_knowledge_base(user_text, history_user_text)

    sys_instruction = SYSTEM_PROMPT.format(
        current_date=today_str,
        day_name=day_name,
        shamsi_date=shamsi_date,
        shamsi_day_name=shamsi_day_name,
        timezone=TIMEZONE,
        knowledge_base=kb_text
    )
    
    formatted_history = ""
    if history:
        history_lines = []
        # فقط ۳ پیام اخیر مکالمه با خلاصه کردن پاسخ‌های بلند قبلی
        for msg in history[-3:]:
            role = msg.get("role")
            sender = "کاربر" if role == "user" else "دستیار"
            content = msg.get("content", "")
            if role != "user" and len(content) > 120:
                content = content[:120] + "..."
            history_lines.append(f"{sender}: {content}")
        formatted_history = "### سوابق گفتگوی قبلی:\n" + "\n".join(history_lines) + "\n\n"

    full_contents = f"{formatted_history}پیام کاربر:\n{user_text}"

    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            # اجرای غیرمسدودکننده در Worker Thread با system_instruction مجزا
            response_text, p_tok, c_tok, t_tok, active_model = await asyncio.to_thread(
                _generate_gemini_content, full_contents, sys_instruction
            )
            record_api_usage(active_model, p_tok, c_tok, t_tok)
            result = json.loads(response_text)
            # اعتبارسنجی حداقلی ساختار پاسخ جمنای
            if not isinstance(result, dict) or "type" not in result:
                logging.warning(f"⚠️ پاسخ جمنای ساختار نامعتبر دارد: {response_text[:200]}")
                return {"type": "ignore", "reply_to_user": None, "notify_admin": False, "calendar_event": None}
            return result
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                delay = 2 ** (attempt - 1)  # 1s, 2s, 4s
                logging.warning(f"⚠️ خطای فراخوانی جمنای در تلاش {attempt}/{max_retries}: {e}. تلاش مجدد پس از {delay} ثانیه...")
                await asyncio.sleep(delay)
            else:
                logging.error(f"❌ تمامی {max_retries} تلاش برای ارتباط با جمنای با شکست مواجه شد: {e}")

    # در صورت شکست تمامی تلاش‌ها: بازگشت پاسخ جایگزین هوشمند (Graceful Degradation)
    return {
        "type": "fallback",
        "reply_to_user": "سلام! سیستم موقتاً با کندی مواجه شده، پیامت رو دریافت کردم و در اسرع وقت بهت پاسخ می‌دم. 🙏",
        "notify_admin": True,
        "admin_notification_text": f"خطا در ارتباط با سرویس هوش مصنوعی جمنای پس از {max_retries} بار تلاش: {last_err}",
        "calendar_event": None
    }

async def send_telegram_message(chat_id: str, text: str, reply_markup: dict = None, business_connection_id: str = None, parse_mode: str = None):
    if not http_client:
        logging.error("❌ کلاینت HTTP آماده نیست.")
        return
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if business_connection_id:
        payload["business_connection_id"] = business_connection_id
        
    try:
        res = await http_client.post(f"{TELEGRAM_API}/sendMessage", json=payload)
        resp_data = res.json()
        if not resp_data.get("ok"):
            logging.error(f"❌ خطای ارسال پیام تلگرام: {resp_data}")
    except Exception as e:
        logging.error(f"❌ خطا در فراخوانی وب‌سرویس تلگرام: {e}")

async def answer_callback_query(callback_query_id: str, text: str = None):
    if not http_client:
        return
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        await http_client.post(f"{TELEGRAM_API}/answerCallbackQuery", json=payload)
    except Exception as e:
        logging.error(f"❌ خطای پاسخ به Callback Query: {e}")

async def edit_telegram_message(chat_id: str | int, message_id: int, text: str, reply_markup: dict = None, parse_mode: str = None):
    """ویرایش متن و دکمه‌های پیام تلگرام برای بستن دکمه‌ها پس از کلیک"""
    if not http_client:
        logging.error("❌ کلاینت HTTP آماده نیست.")
        return
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        res = await http_client.post(f"{TELEGRAM_API}/editMessageText", json=payload)
        resp_data = res.json()
        if not resp_data.get("ok"):
            logging.error(f"❌ خطای ویرایش پیام تلگرام: {resp_data}")
    except Exception as e:
        logging.error(f"❌ خطا در فراخوانی ویرایش پیام تلگرام: {e}")

async def send_telegram_document(chat_id: str | int, file_path: str, caption: str = ""):
    """ارسال فایل لاگ یا سایر اسناد به تلگرام ادمین"""
    if not http_client:
        logging.error("❌ کلاینت HTTP آماده نیست.")
        return
    if not os.path.exists(file_path):
        await send_telegram_message(chat_id=chat_id, text=f"⚠️ فایل {file_path} یافت نشد.")
        return
    try:
        with open(file_path, "rb") as f:
            file_bytes = f.read()
            files = {"document": (os.path.basename(file_path), file_bytes, "text/plain")}
            data = {"chat_id": str(chat_id), "caption": caption}
            res = await http_client.post(f"{TELEGRAM_API}/sendDocument", data=data, files=files)
            resp_data = res.json()
            if not resp_data.get("ok"):
                logging.error(f"❌ خطای ارسال سند تلگرام: {resp_data}")
    except Exception as e:
        logging.error(f"❌ خطا در ارسال فایل لاگ: {e}")

def build_admin_panel_markup(is_active: bool) -> dict:
    """ساخت دکمه‌های پنل کنترلی ادمین"""
    toggle_text = "🔴 خاموش کردن ربات (توقف پاسخگویی)" if is_active else "🟢 روشن کردن ربات (شروع پاسخگویی)"
    return {
        "inline_keyboard": [
            [{"text": toggle_text, "callback_data": "admin:toggle"}],
            [{"text": "📊 آمار پیام‌ها و مصرف API امروز", "callback_data": "admin:stats"}],
            [{"text": "🤖 تغییر مدل هوش مصنوعی", "callback_data": "admin:model"}],
            [
                {"text": "📄 دریافت فایل لاگ", "callback_data": "admin:logs"},
                {"text": "🔄 بروزرسانی پنل", "callback_data": "admin:refresh"}
            ]
        ]
    }

def build_model_selection_markup(current_model: str) -> dict:
    """ساخت inline keyboard انتخاب مدل جمنای برای ادمین"""
    rows = []
    for model_id, model_label in AVAILABLE_GEMINI_MODELS:
        prefix = "✅ " if model_id == current_model else ""
        rows.append([{"text": f"{prefix}{model_label}", "callback_data": f"admin:setmodel:{model_id}"}])
    rows.append([{"text": "🔙 بازگشت به پنل", "callback_data": "admin:refresh"}])
    return {"inline_keyboard": rows}


def format_admin_stats_text() -> str:
    """قالب‌بندی گزارش آماری روزانه برای نمایش به ادمین"""
    stats = get_daily_stats()
    status_emoji = "🟢 روشن و فعال" if stats["is_active"] else "🔴 متوقف و خاموش"
    free_tier_daily_limit = 1500  # سقف استاندارد درخواست روزانه طرح رایگان جمنای
    remaining_requests = max(0, free_tier_daily_limit - stats["api_calls_today"])
    
    return (
        f"📊 **گزارش عملکرد و آمار امروز ربات A.S.K.A.R**\n\n"
        f"⚙️ **وضعیت سیستم:** {status_emoji}\n\n"
        f"💬 **آمار پیام‌های امروز:**\n"
        f"  • کل پیام‌های رد و بدل شده: `{stats['total_messages_today']}`\n"
        f"  • پیام‌های دریافتی از کاربران: `{stats['user_messages_today']}`\n"
        f"  • پاسخ‌های ارسالی هوش مصنوعی: `{stats['model_replies_today']}`\n"
        f"  • تعداد مخاطبان منحصربه‌فرد: `{stats['unique_users_today']}` نفر\n\n"
        f"🤖 **مصرف API جمنای امروز:**\n"
        f"  • تعداد درخواست‌ها (RPD): `{stats['api_calls_today']}` از `{free_tier_daily_limit}` مجاز روزانه\n"
        f"  • درخواست‌های باقیمانده امروز: `{remaining_requests}`\n"
        f"  • کل توکن‌های مصرفی امروز: `{stats['total_tokens_today']:,}` توکن\n"
        f"    (ورودی: `{stats['prompt_tokens_today']:,}` | تولیدی: `{stats['candidate_tokens_today']:,}`)\n\n"
        f"📅 **جلسات و تقویم:**\n"
        f"  • رویدادهای منتظر تایید: `{stats['pending_events_count']}` مورد"
    )

def format_admin_panel_text(is_active: bool) -> str:
    """قالب‌بندی متن پنل مدیریت ادمین (حذف تکرار)"""
    status_text = "🟢 **ربات فعال است** و پیام‌های کاربران را پاسخ می‌دهد." if is_active else "🔴 **ربات متوقف است** (حالت سکوت/تعمیرات)."
    current_model = get_active_gemini_model()
    # پیدا کردن برچسب فارسی مدل انتخابی
    model_label = next((lbl for mid, lbl in AVAILABLE_GEMINI_MODELS if mid == current_model), current_model)
    return (
        f"👑 **پنل مدیریت ربات هوشمند A.S.K.A.R**\n\n"
        f"وضعیت کنونی: {status_text}\n"
        f"🤖 مدل هوش مصنوعی فعال: `{model_label}`\n\n"
        f"از دکمه‌های زیر برای کنترل و دریافت گزارشات استفاده فرمایید:"
    )

async def handle_admin_command(chat_id: str | int, text: str):
    """پردازش دستورات دریافتی از پیام خصوصی ادمین"""
    cmd = text.lower().strip()
    is_active = get_bot_active_status()
    
    if cmd in ["/start", "/panel", "/help"]:
        await send_telegram_message(
            chat_id=chat_id,
            text=format_admin_panel_text(is_active),
            reply_markup=build_admin_panel_markup(is_active)
        )
    elif cmd == "/stats":
        stats_text = format_admin_stats_text()
        await send_telegram_message(chat_id=chat_id, text=stats_text)
    elif cmd in ["/pause", "/stop"]:
        set_bot_active_status(False)
        await send_telegram_message(chat_id=chat_id, text="🔴 پاسخگویی خودکار ربات متوقف شد.")
    elif cmd in ["/resume", "/play", "/unpause"]:
        set_bot_active_status(True)
        await send_telegram_message(chat_id=chat_id, text="🟢 پاسخگویی خودکار ربات مجدداً فعال شد.")
    elif cmd == "/logs":
        await send_telegram_message(chat_id=chat_id, text="⏳ در حال آماده‌سازی و ارسال فایل لاگ...")
        await send_telegram_document(chat_id=chat_id, file_path=os.path.join(BASE_DIR, "bot_activity.log"), caption="📄 فایل لاگ سرور bot_activity.log")
    elif cmd == "/status":
        status_text = "🟢 فعال" if is_active else "🔴 متوقف"
        await send_telegram_message(chat_id=chat_id, text=f"وضعیت فعلی ربات: {status_text}")
    else:
        await send_telegram_message(
            chat_id=chat_id,
            text="دستور نامعتبر است. برای مشاهده پنل دستور /panel را ارسال کنید."
        )

def insert_google_calendar_event(ev: dict) -> dict:
    """درج رویداد در تقویم گوگل به صورت همگام (برای اجرا در Worker Thread)"""
    if not calendar_service:
        raise RuntimeError("سرویس Google Calendar متصل نیست.")
    
    body = {
        'summary': ev.get('summary'),
        'description': ev.get('description', ''),
        'start': {
            'dateTime': ev.get('start_time'),
            'timeZone': TIMEZONE,
        },
        'end': {
            'dateTime': ev.get('end_time'),
            'timeZone': TIMEZONE,
        },
    }
    return calendar_service.events().insert(calendarId=CALENDAR_ID, body=body).execute()

@app.post("/webhook")
@app.post("/webhook/")
@app.post("/webhook\\")
async def telegram_webhook(request: Request):
    # احراز هویت توکن وب‌هوک تلگرام (در صورت پیکربندی در متغیرهای محیطی)
    if TELEGRAM_WEBHOOK_SECRET:
        secret_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(secret_header, TELEGRAM_WEBHOOK_SECRET):
            logging.warning("⛔ رد درخواست وب‌هوک تلگرام: عدم تطابق X-Telegram-Bot-Api-Secret-Token")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden: Invalid secret token"
            )

    data = await request.json()
    
    # دریافت پیام ورودی به اکانت تجاری
    if "business_message" in data:
        # اگر ربات توسط ادمین در حالت سکوت/تعمیرات قرار گرفته باشد، پاسخگویی انجام نشود
        if not get_bot_active_status():
            logging.info("⏸️ پیام ورودی نادیده گرفته شد (پاسخگویی خودکار ربات توسط ادمین متوقف است).")
            return {"ok": True}

        msg = data["business_message"]
        text = msg.get("text", "")
        sender_name = msg.get("from", {}).get("first_name", "کاربر")
        b_conn_id = msg.get("business_connection_id")
        user_chat_id = msg.get("chat", {}).get("id")

        if not text:
            return {"ok": True}

        # ⚡ اولویت ۰: بررسی پاسخ فوری محلی با ۰ توکن (احوالپرسی خالی، تشکر، تایید، خداحافظی)
        quick_reply = handle_quick_chit_chat(text)
        if quick_reply:
            save_chat_message(user_chat_id, "user", text)
            await send_telegram_message(
                chat_id=user_chat_id,
                text=quick_reply,
                business_connection_id=b_conn_id
            )
            save_chat_message(user_chat_id, "model", quick_reply)
            logging.info(f"⚡ پاسخ فوری محلی به {sender_name} با ۰ توکن ارسال شد: {quick_reply}")
            return {"ok": True}

        # بررسی اینکه آیا کاربر در حال حاضر در یک جلسه گفتگوی فعال است؟
        is_active = is_user_in_active_session(user_chat_id)

        # فیلتر متنی اولیه: اگر خارج از جلسه فعال بود و کلمات کلیدی هم نداشت، نادیده گرفته شود
        if not is_active and not should_process_message(text):
            logging.info(f"⏭️ پیام از {sender_name} نادیده گرفته شد (خارج از جلسه فعال و فاقد کلمات کلیدی، {len(text)} کاراکتر)")
            return {"ok": True}

        logging.info(f"📩 پیام جدید از {sender_name} (جلسه فعال: {is_active}، {len(text)} کاراکتر)")

        # دریافت سوابق گفتگو برای آگاهی هوش مصنوعی از پیشینه صحبت‌ها (حداکثر ۳ پیام اخیر جهت صرفه‌جویی توکن)
        recent_history = get_recent_chat_history(user_chat_id, limit=3)

        # ثبت پیام کاربر در تاریخچه
        save_chat_message(user_chat_id, "user", text)
        
        try:
            analysis = await call_gemini(text, history=recent_history)
            logging.info(f"🤖 نتیجه تحلیل جمنای: {json.dumps(analysis, ensure_ascii=False)}")
        except Exception as e:
            logging.error(f"❌ خطا در فراخوانی جمنای: {e}")
            return {"ok": True}
        
        # ۱. ارسال پاسخ خودکار به کاربر (در صورت وجود)
        if analysis.get("reply_to_user"):
            user_reply_text = analysis["reply_to_user"]
            await send_telegram_message(
                chat_id=user_chat_id,
                text=user_reply_text,
                business_connection_id=b_conn_id
            )
            save_chat_message(user_chat_id, "model", user_reply_text)
            logging.info(f"📤 پاسخ به کاربر ارسال و در تاریخچه ذخیره شد: {user_reply_text}")

        # ۲. پیشنهاد تسک برای تقویم گوگل (ارسال به ادمین همراه با دکمه‌های تایید و رد)
        if analysis.get("type") == "task" and analysis.get("calendar_event"):
            ev = analysis["calendar_event"]
            event_id = f"ev_{uuid.uuid4().hex[:12]}"
            save_pending_event(
                event_id=event_id,
                ev=ev,
                user_chat_id=user_chat_id,
                business_connection_id=b_conn_id,
                sender_name=sender_name
            )
            
            confirm_text = (
                f"📌 **پیشنهاد ثبت جلسه / رویداد جدید:**\n\n"
                f"👤 از طرف: {sender_name}\n"
                f"💬 پیام کاربر: {text}\n\n"
                f"📅 عنوان: {ev.get('summary')}\n"
                f"⏰ شروع: {ev.get('start_time')}\n"
                f"🏁 پایان: {ev.get('end_time')}"
            )
            
            keyboard = {
                "inline_keyboard": [[
                    {"text": "✅ تایید و ثبت در تقویم", "callback_data": f"approve:{event_id}"},
                    {"text": "❌ رد", "callback_data": f"reject:{event_id}"}
                ]]
            }
            await send_telegram_message(chat_id=ADMIN_CHAT_ID, text=confirm_text, reply_markup=keyboard)
            logging.info(f"🔔 درخواست تایید تسک با دکمه برای ادمین ارسال شد (ID: {event_id}).")

        # ۳. ارسال هشدار متنی برای موارد نیازمند بررسی دستی (در صورتی که تسک تقویم نباشد)
        elif analysis.get("notify_admin") and analysis.get("admin_notification_text"):
            admin_alert = (
                f"⚠️ **بررسی دستی مورد نیاز است**\n\n"
                f"👤 کاربر: {sender_name}\n"
                f"💬 پیام: {text}\n"
                f"📌 موضوع: {analysis['admin_notification_text']}"
            )
            await send_telegram_message(chat_id=ADMIN_CHAT_ID, text=admin_alert)

    # ۲. پیام مستقیم در چت خصوصی به خود بات (فرامین ادمین)
    elif "message" in data:
        msg = data["message"]
        sender_id = msg.get("from", {}).get("id")
        chat_id = msg.get("chat", {}).get("id")
        text = msg.get("text", "").strip()

        if not text:
            return {"ok": True}

        # بررسی دسترسی اختصاصی ادمین
        if not ADMIN_CHAT_ID or str(sender_id) != str(ADMIN_CHAT_ID):
            logging.warning(f"⛔ پیام مستقیم از کاربر غیرمجاز مسدود شد (User ID: {sender_id}).")
            await send_telegram_message(
                chat_id=chat_id,
                text="⛔ شما دسترسی ادمین ندارید. این ربات یک دستیار تجاری است و فقط توسط مدیر سیستم کنترل می‌شود."
            )
            return {"ok": True}

        logging.info(f"👑 دستور ادمین دریافت شد: {text}")
        await handle_admin_command(chat_id=chat_id, text=text)
        return {"ok": True}

    # ۳. مدیریت کلیک روی دکمه‌ها
    elif "callback_query" in data:
        cb = data["callback_query"]
        cb_id = cb["id"]
        cb_data = cb.get("data", "")
        cb_msg = cb.get("message", {})
        cb_chat_id = cb_msg.get("chat", {}).get("id") or ADMIN_CHAT_ID
        cb_msg_id = cb_msg.get("message_id")
        original_text = cb_msg.get("text", "")
        sender_id = cb.get("from", {}).get("id")

        # دکمه‌های پنل مدیریت ادمین
        if cb_data.startswith("admin:"):
            if not ADMIN_CHAT_ID or str(sender_id) != str(ADMIN_CHAT_ID):
                await answer_callback_query(cb_id, text="⛔ دسترسی غیرمجاز!")
                return {"ok": True}

            action = cb_data.split(":", 1)[1]
            if action == "toggle":
                current_status = get_bot_active_status()
                new_status = not current_status
                set_bot_active_status(new_status)
                status_str = "🟢 پاسخگویی روشن شد" if new_status else "🔴 پاسخگویی خاموش شد"
                await answer_callback_query(cb_id, text=status_str)

                if cb_msg_id:
                    await edit_telegram_message(
                        chat_id=cb_chat_id,
                        message_id=cb_msg_id,
                        text=format_admin_panel_text(new_status),
                        reply_markup=build_admin_panel_markup(new_status)
                    )
            elif action == "stats":
                await answer_callback_query(cb_id, text="📊 در حال استخراج آمار...")
                stats_text = format_admin_stats_text()
                await send_telegram_message(chat_id=cb_chat_id, text=stats_text)
            elif action == "logs":
                await answer_callback_query(cb_id, text="📄 در حال ارسال فایل لاگ...")
                await send_telegram_document(chat_id=cb_chat_id, file_path=os.path.join(BASE_DIR, "bot_activity.log"), caption="📄 فایل لاگ سرور bot_activity.log")
            elif action == "model":
                # نمایش لیست مدل‌های جمنای برای انتخاب
                current_model = get_active_gemini_model()
                model_panel_text = (
                    f"🤖 **انتخاب مدل هوش مصنوعی**\n\n"
                    f"مدل فعلی: `{current_model}`\n\n"
                    f"یک مدل را از لیست زیر انتخاب کنید:"
                )
                if cb_msg_id:
                    await edit_telegram_message(
                        chat_id=cb_chat_id,
                        message_id=cb_msg_id,
                        text=model_panel_text,
                        reply_markup=build_model_selection_markup(current_model)
                    )
                await answer_callback_query(cb_id)
            elif action.startswith("setmodel:"):
                # ذخیره مدل انتخابی ادمین
                chosen_model = action.split(":", 1)[1]
                valid_model_ids = [mid for mid, _ in AVAILABLE_GEMINI_MODELS]
                if chosen_model not in valid_model_ids:
                    await answer_callback_query(cb_id, text="⛔ مدل انتخابی معتبر نیست!")
                else:
                    set_active_gemini_model(chosen_model)
                    model_label = next((lbl for mid, lbl in AVAILABLE_GEMINI_MODELS if mid == chosen_model), chosen_model)
                    await answer_callback_query(cb_id, text=f"✅ مدل به {model_label} تغییر یافت")
                    # برگشت به پنل اصلی با مدل آپدیت شده
                    is_active = get_bot_active_status()
                    if cb_msg_id:
                        await edit_telegram_message(
                            chat_id=cb_chat_id,
                            message_id=cb_msg_id,
                            text=format_admin_panel_text(is_active),
                            reply_markup=build_admin_panel_markup(is_active)
                        )
            elif action == "refresh":
                is_active = get_bot_active_status()
                if cb_msg_id:
                    await edit_telegram_message(
                        chat_id=cb_chat_id,
                        message_id=cb_msg_id,
                        text=format_admin_panel_text(is_active),
                        reply_markup=build_admin_panel_markup(is_active)
                    )
                await answer_callback_query(cb_id, text="وضعیت بروزرسانی شد ✅")
            return {"ok": True}

        
        if ":" in cb_data:
            action, event_id = cb_data.split(":", 1)
            
            # بررسی دسترسی ادمین برای تایید و رد رویدادها
            if action in ("approve", "reject"):
                if not ADMIN_CHAT_ID or str(sender_id) != str(ADMIN_CHAT_ID):
                    await answer_callback_query(cb_id, text="⛔ دسترسی غیرمجاز!")
                    return {"ok": True}

            if action == "approve":
                ev = get_pending_event(event_id)
                if ev and calendar_service:
                    try:
                        # اجرای غیرمسدودکننده درخواست شبکه گوگل کلندر در Worker Thread
                        await asyncio.to_thread(insert_google_calendar_event, ev)
                        updated_text = f"{original_text}\n\n✅ این رویداد در تقویم گوگل ثبت گردید."
                        if cb_msg_id:
                            await edit_telegram_message(
                                chat_id=cb_chat_id,
                                message_id=cb_msg_id,
                                text=updated_text,
                                reply_markup={"inline_keyboard": []}
                            )
                        else:
                            await send_telegram_message(chat_id=ADMIN_CHAT_ID, text=f"✅ رویداد «{ev.get('summary')}» در گوگل کلندر ثبت شد.")
                        
                        logging.info(f"📅 رویداد «{ev.get('summary')}» با موفقیت در گوگل کلندر ثبت شد.")
                        
                        # ارسال پیام تایید نهایی خودمونی به کاربر متقاضی
                        target_user_id = ev.get("user_chat_id")
                        target_b_conn = ev.get("business_connection_id")
                        summary_title = ev.get("summary", "جلسه")
                        if target_user_id:
                            user_confirm_msg = f"سلام مجدد! تایم «{summary_title}» اوکی شد و گذاشتمش تو تقویم. ✅📅"
                            await send_telegram_message(
                                chat_id=target_user_id,
                                text=user_confirm_msg,
                                business_connection_id=target_b_conn
                            )
                            save_chat_message(target_user_id, "model", user_confirm_msg)
                            logging.info(f"📩 پیام تایید نهایی به کاربر {target_user_id} ارسال شد.")

                        delete_pending_event(event_id)
                        await answer_callback_query(cb_id, text="رویداد ثبت و به کاربر اطلاع داده شد ✅")
                    except Exception as err:
                        logging.error(f"❌ خطای ثبت تقویم: {err}")
                        await send_telegram_message(chat_id=ADMIN_CHAT_ID, text=f"❌ خطا در ثبت تقویم: {err}")
                        await answer_callback_query(cb_id, text="خطا در ثبت تقویم ❌")
                else:
                    if cb_msg_id:
                        await edit_telegram_message(
                            chat_id=cb_chat_id,
                            message_id=cb_msg_id,
                            text=f"{original_text}\n\n⚠️ رویداد منقضی شده یا قبلاً پردازش شده است.",
                            reply_markup={"inline_keyboard": []}
                        )
                    else:
                        await send_telegram_message(chat_id=ADMIN_CHAT_ID, text="⚠️ رویداد در دیتابیس یافت نشد یا منقضی شده است.")
                    await answer_callback_query(cb_id, text="رویداد یافت نشد یا منقضی شده است ⚠️")
                    logging.warning(f"⚠️ تلاش برای ثبت رویدادی که در دیتابیس نیست (ID: {event_id}).")
                
            elif action == "reject":
                ev = get_pending_event(event_id)
                if ev:
                    target_user_id = ev.get("user_chat_id")
                    target_b_conn = ev.get("business_connection_id")
                    summary_title = ev.get("summary", "جلسه")
                    if target_user_id:
                        user_reject_msg = f"سلام! متاسفانه برای تایم «{summary_title}» تداخل دارم و امکانش نیست. بی زحمت یه تایم دیگه پیشنهاد بده با هم هماهنگ کنیم. 🙏"
                        await send_telegram_message(
                            chat_id=target_user_id,
                            text=user_reject_msg,
                            business_connection_id=target_b_conn
                        )
                        save_chat_message(target_user_id, "model", user_reject_msg)
                        logging.info(f"📩 پیام رد درخواست به کاربر {target_user_id} ارسال شد.")
                        
                delete_pending_event(event_id)
                updated_text = f"{original_text}\n\n❌ این رویداد رد شد."
                if cb_msg_id:
                    await edit_telegram_message(
                        chat_id=cb_chat_id,
                        message_id=cb_msg_id,
                        text=updated_text,
                        reply_markup={"inline_keyboard": []}
                    )
                else:
                    await send_telegram_message(chat_id=ADMIN_CHAT_ID, text="❌ رویداد رد شد.")
                await answer_callback_query(cb_id, text="رویداد رد شد و به کاربر اطلاع داده شد ❌")
                logging.info(f"🚫 رویداد رد شد (ID: {event_id}).")
        else:
            await answer_callback_query(cb_id)

    return {"ok": True}