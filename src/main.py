# -*- coding: utf-8 -*-
"""
FL Monitor V2.5 — DUAL SOURCE (главная страница + RSS-страховка)

Приоритет источников:
  1. https://www.fl.ru/projects/ — мгновенные заказы (парсинг HTML)
  2. https://www.fl.ru/rss/all.xml — резерв если главная недоступна (403)

Fixes from V2.4:
  - "ml" / "ui" / "ux" проверяются только как целое слово
  - 2+ дизайн-слов → сразу ❌
  - "telegram" (латиница) учитывается в can_do_1_2_days
  - 403 → человечное "Анализ по RSS-ленте"
  - title_is_anti: быстрая отсечка по названию без загрузки страницы
"""

import os
import re
import time
import html
import hashlib
import logging
import sqlite3
import datetime as dt
import xml.etree.ElementTree as ET
from urllib.parse import urlparse, parse_qs

import requests


BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB_PATH = os.path.join(BASE_DIR, "projects.db")
LOG_PATH = os.path.join(BASE_DIR, "fl_monitor.log")
ENV_PATH = os.path.join(BASE_DIR, ".env")

RSS_URL = "https://www.fl.ru/rss/all.xml"
PROJECTS_URL = "https://www.fl.ru/projects/"


def load_env():
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(ENV_PATH)
        return
    except Exception:
        pass
    if not os.path.exists(ENV_PATH):
        return
    try:
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        return


load_env()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
INTERVAL_SECONDS = int(os.getenv("INTERVAL_SECONDS", "20"))
MAX_PER_CYCLE = int(os.getenv("MAX_PER_CYCLE", "3"))

HTTP_TIMEOUT = (10, 20)
HTTP_RETRIES = 2
SLEEP_BETWEEN_SENDS = 1.0

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Connection": "close",
}

WHOLE_WORD_KEYWORDS = {"ml", "ui", "ux", "ai", "vk", "tg", "api", "sql", "бот"}

CORE_KEYWORDS = [
    "python", "скрипт", "парсер", "парсинг", "selenium", "playwright", "beautifulsoup",
    "requests", "fastapi", "flask", "django", "sql", "sqlite", "postgres", "mysql",
    "api", "интеграция", "автоматизац", "бот", "telegram", "телеграм", "tg",
    "linux", "ubuntu", "debian", "nginx", "docker", "kubernetes", "k8s",
    "vps", "vds", "сервер", "хостинг", "vpn", "vless", "xray", "x-ui", "wireguard",
    "ai", "gpt", "openai", "llm", "нейросет", "машинн", "ml", "rag",
    "node", "nodejs", "nest", "nestjs", "backend", "бэкенд", "typescript",
]

ANTI_KEYWORDS = [
    "логотип", "фирменный стиль", "брендинг", "визитк", "баннер", "лендинг",
    "дизайн", "ui", "ux", "фигма", "figma", "иллюстрац", "3d", "рендер",
    "smm", "таргет", "контент", "копирайт", "seo", "продвижен", "реклама",
    "директ", "vk", "инстаграм", "tiktok", "youtube",
    "перевод", "транскриб", "озвучк", "видео", "монтаж", "фотошоп",
    "чертеж", "архитектур", "интерьер",
    "раскачать", "маркетолог", "спам", "накрутка",
    "modx", "wordpress", "bitrix", "joomla", "opencart", "тильда", "wix",
    "постинг", "ведение канала", "контент план", "администратор канала", "настройка телеграм",
"анимац","эмодзи","стикер","gif","power bi","sharepoint","1c","битрикс","ms project","автосервис",]

HARD_ANTI_KEYWORDS = {
    "логотип", "фирменный стиль", "брендинг", "дизайн", "figma", "фигма",
    "иллюстрац", "ui", "ux", "рендер", "3d",
}

# Слова в НАЗВАНИИ заказа — мгновенная отсечка без загрузки страницы
TITLE_ANTI = [
    "раскачать", "продвижение", "smm", "маркетолог", "копирайтер",
    "спам", "накрутк", "подписчик", "таргетолог",
    "постинг", "ведение", "администратор",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("fl_monitor")


# ── БД ────────────────────────────────────────────────────────────────────────

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS processed (
            id TEXT PRIMARY KEY,
            url TEXT,
            title TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    return conn


def is_processed(conn, pid: str) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM processed WHERE id = ?", (pid,))
    return cur.fetchone() is not None


def mark_processed(conn, pid: str, url: str, title: str):
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO processed (id, url, title, created_at) VALUES (?, ?, ?, ?)",
        (pid, url, title, dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")),
    )
    conn.commit()


# ── ТЕКСТ ─────────────────────────────────────────────────────────────────────

def safe_text(x: str) -> str:
    if not x:
        return ""
    return re.sub(r"\s+", " ", x).strip()


def unescape_all(x: str) -> str:
    if not x:
        return ""
    return safe_text(html.unescape(x))


def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()


def extract_id_from_link(link: str) -> str:
    if not link:
        return sha1("")
    m = re.search(r"/projects/(\d+)", link)
    if m:
        return m.group(1)
    try:
        u = urlparse(link)
        q = parse_qs(u.query)
        for k in ("pid", "id", "project_id"):
            if k in q and q[k]:
                return str(q[k][0])
    except Exception:
        pass
    return sha1(link)


def normalize(s: str) -> str:
    return safe_text(s).lower()


TAG_RE = re.compile(r"<[^>]+>")
SCRIPT_RE = re.compile(r"(?is)<(script|style).*?>.*?</\1>")
BR_RE = re.compile(r"(?i)<br\s*/?>")
P_RE = re.compile(r"(?i)</p\s*>")


def strip_html(s: str) -> str:
    if not s:
        return ""
    s = s.replace("\r", " ").replace("\n", " ").replace("&nbsp;", " ")
    s = SCRIPT_RE.sub(" ", s)
    s = BR_RE.sub("\n", s)
    s = P_RE.sub("\n", s)
    s = TAG_RE.sub(" ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


# ── HTTP ──────────────────────────────────────────────────────────────────────

def http_get(url: str) -> tuple:
    """Возвращает (text, status_code). Cache-bust через timestamp."""
    sep = "&" if "?" in url else "?"
    bust_url = f"{url}{sep}_cb={int(time.time())}"
    last_err = None
    for attempt in range(HTTP_RETRIES + 1):
        try:
            r = requests.get(bust_url, headers=HEADERS, timeout=HTTP_TIMEOUT)
            if r.status_code == 403:
                log.warning("403 for %s", url)
                return ("", 403)
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            return (r.text, r.status_code)
        except Exception as e:
            last_err = e
            time.sleep(0.8)
    log.warning("Failed GET %s: %s", url, last_err)
    return ("", 0)


# ── ИСТОЧНИК 1: ГЛАВНАЯ СТРАНИЦА /projects/ ───────────────────────────────────

def fetch_projects_page() -> list:
    """
    Парсит fl.ru/projects/ — заказы появляются мгновенно.
    Возвращает [] если страница недоступна.
    """
    text, code = http_get(PROJECTS_URL)
    if not text or code == 403:
        log.warning("Главная /projects/ недоступна (код %s)", code)
        return []
    try:
        from bs4 import BeautifulSoup  # type: ignore
        soup = BeautifulSoup(text, "html.parser")
        items = []
        seen = set()
        for a in soup.find_all("a", href=re.compile(r"/projects/\d+")):
            href = a.get("href", "")
            if not re.search(r"/projects/\d+/", href):
                continue
            full_url = (href if href.startswith("http") else "https://www.fl.ru" + href)
            full_url = full_url.split("?")[0].split("#")[0]
            if full_url in seen:
                continue
            seen.add(full_url)
            title = safe_text(a.get_text(" ", strip=True))
            if not title or len(title) < 5:
                continue
            items.append({"title": title, "link": full_url, "desc": ""})
        log.info("Главная /projects/: %d заказов", len(items))
        return items
    except Exception as e:
        log.warning("Ошибка парсинга /projects/: %s", e)
        return []


# ── ИСТОЧНИК 2: RSS (резервный) ───────────────────────────────────────────────

def fetch_rss_items() -> list:
    try:
        text, code = http_get(RSS_URL)
        if not text:
            return []
        root = ET.fromstring(text.encode("utf-8", errors="replace"))
        channel = root.find("channel") or root.find(".//channel")
        if channel is None:
            return []
        items = []
        for it in channel.findall("item"):
            title = unescape_all(it.findtext("title") or "")
            link = safe_text(it.findtext("link") or "")
            desc = unescape_all(strip_html(it.findtext("description") or ""))
            items.append({"title": title, "link": link, "desc": desc})
        log.info("RSS: %d заказов", len(items))
        return items
    except Exception as e:
        log.warning("RSS ошибка: %s", e)
        return []


# ── ПАРСИНГ СТРАНИЦЫ ПРОЕКТА ──────────────────────────────────────────────────

def try_extract_project_text(page_html: str) -> str:
    if not page_html:
        return ""
    try:
        from bs4 import BeautifulSoup  # type: ignore
        soup = BeautifulSoup(page_html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        candidates = []
        for sel in ["[itemprop='description']", ".project_descr",
                    ".task__description", ".b-layout__txt", ".b-layout__main", "main"]:
            el = soup.select_one(sel)
            if el:
                txt = safe_text(el.get_text("\n", strip=True))
                if txt and len(txt) > 150:
                    candidates.append(txt)
        if candidates:
            candidates.sort(key=len, reverse=True)
            return candidates[0]
        return safe_text(soup.get_text("\n", strip=True))
    except Exception:
        return strip_html(page_html)


def looks_like_menu_garbage(text: str) -> bool:
    t = normalize(text)
    if "найти работу" in t:
        return True
    bad = ["фрилансеры", "продвижение сайтов", "реклама и маркетинг",
           "фирменный стиль", "социальные сети"]
    if sum(1 for b in bad if b in t) >= 2:
        return True
    if len(t) > 400 and t.count("сайты") >= 3 and t.count("дизайн") >= 2:
        return True
    return False


# ── ФИЛЬТРАЦИЯ ────────────────────────────────────────────────────────────────

def count_matches(text: str, keywords):
    t = normalize(text)
    hits = []
    for kw in keywords:
        kl = kw.lower()
        if kl in WHOLE_WORD_KEYWORDS:
            if re.search(r'(?<![a-zа-яё])' + re.escape(kl) + r'(?![a-zа-яё])', t):
                hits.append(kw)
        else:
            if kl in t:
                hits.append(kw)
    return hits


def title_is_anti(title: str) -> bool:
    t = normalize(title)
    return any(w in t for w in TITLE_ANTI)


def verdict_and_confidence(core_hits, anti_hits, text_len: int):
    hard_anti = sum(1 for h in anti_hits if h.lower() in HARD_ANTI_KEYWORDS)
    if hard_anti >= 2:
        return ("❌ НЕ РЕЛЕВАНТНО", 0.10, -10)
    core_n, anti_n = len(core_hits), len(anti_hits)
    score = core_n * 3 - anti_n * 2
    if text_len > 900:
        score += 1
    if text_len > 1600:
        score += 1
    if core_n == 0:
        return ("❌ НЕ РЕЛЕВАНТНО", 0.15, score)
    if score >= 7:
        return ("✅ БРАТЬ", 0.85, score)
    if score >= 3:
        return ("🟡 МОЖНО СМОТРЕТЬ", 0.60, score)
    return ("⚠️ СОМНИТЕЛЬНО", 0.35, score)


def can_do_1_2_days(text: str, core_hits):
    t = normalize(text)
    red = any(x in t for x in ["на месяц", "постоянн", "поддержк",
                                 "full-time", "фуллтайм", "в команду"])
    if red:
        return ("скорее нет", "похоже на долгий проект/поддержку")
    green = any(x in t for x in ["скрипт", "парсер", "бот", "автоматизац",
                                   "интеграц", "api", "телеграм", "telegram"])
    if green or len(core_hits) >= 2:
        return ("скорее да", "похоже на задачу 1–2 дня при нормальном ТЗ")
    return ("не ясно", "нужно уточнить объём и доступы")


def build_questions(text: str):
    qs = [
        "Какая конечная цель (что именно должно быть на выходе)?",
        "Есть ли примеры/референсы (как должно работать/выглядеть)?",
        "Нужны ли авторизация/доступы (логин/пароль, токены API)?",
        "Какой формат результата нужен (скрипт, бот, сервис, инструкции)?",
        "Ограничения по стеку и окружению (Windows/VPS, Python/Node)?",
        "Сроки и бюджет: когда нужно и какой диапазон приемлем?",
    ]
    t = normalize(text)
    if "telegram" in t or "телеграм" in t or "бот" in t:
        qs.append("Куда отправлять сообщения (бот уже есть или сделать нового)?")
    if "vpn" in t or "vless" in t or "xray" in t:
        qs.append("На каком сервере/ОС делать настройку (Ubuntu/Debian)? Домен есть?")
    if "парсер" in t or "парс" in t:
        qs.append("Источник данных: ссылка(и) + частота обновления/лимиты?")
    return qs


def build_need_from_customer(text: str):
    needs = [
        "Ссылка на задачу + что именно нужно сделать",
        "Пример ожидаемого результата (скрин/пример поведения)",
        "Доступы (админка/SSH/API) — если есть",
        "Окружение: где запускать (Windows/VPS), сроки",
    ]
    t = normalize(text)
    if "api" in t:
        needs.append("Документация API + тестовый ключ")
    if "сервер" in t or "vps" in t:
        needs.append("IP/SSH доступ или просьба развернуть на моём VPS")
    return needs


def summarize(text: str, max_chars=420):
    t = safe_text(text)
    return t if len(t) <= max_chars else (t[:max_chars] + "…").strip()


def build_reply_draft(title: str, url: str, verdict: str, conf: float):
    return "\n".join([
        "Здравствуйте!",
        f"Посмотрел задачу «{title}».",
        "",
        "Могу взять в работу. Уточните, пожалуйста:",
        "1) Цель и результат (что должно быть на выходе).",
        "2) Какие доступы/исходники есть (админка, FTP/SSH, токены).",
        "3) Есть ли примеры/референсы (как должно работать).",
        "",
        f"Предварительно: {verdict} (уверенность ~{int(conf*100)}%).",
        "Если объём адекватный — обычно укладываюсь в 1–2 дня: прототип → доводка → сдача.",
        "",
        "Готов начать сразу после уточнений. Спасибо!",
    ])


def split_for_telegram(text: str, limit: int = 3900):
    t = text.strip()
    if len(t) <= limit:
        return [t]
    chunks, buf, cur = [], [], 0
    for block in t.split("\n\n"):
        b = block.strip()
        if not b:
            continue
        add_len = len(b) + (2 if buf else 0)
        if cur + add_len <= limit:
            if buf:
                buf.append("")
            buf.append(b)
            cur += add_len
        else:
            if buf:
                chunks.append("\n\n".join(buf).strip())
            while len(b) > limit:
                chunks.append(b[:limit])
                b = b[limit:]
            buf = [b] if b else []
            cur = len(b)
    if buf:
        chunks.append("\n\n".join(buf).strip())
    return chunks


def tg_send(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы (проверь .env)")
        return False
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    ok_all = True
    for part in split_for_telegram(text, limit=3900):
        try:
            r = requests.post(api_url, data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": part,
                "disable_web_page_preview": False,
            }, timeout=HTTP_TIMEOUT)
            if r.status_code != 200:
                ok_all = False
                log.warning("Telegram %s: %s", r.status_code, r.text[:300])
            time.sleep(SLEEP_BETWEEN_SENDS)
        except Exception as e:
            ok_all = False
            log.warning("Telegram exception: %s", e)
    return ok_all


def is_relevant(verdict: str) -> bool:
    return verdict in ("✅ БРАТЬ", "🟡 МОЖНО СМОТРЕТЬ")


# ── СБОРКА СООБЩЕНИЯ ──────────────────────────────────────────────────────────

def build_full_message(title, url, project_text, rss_desc, used_fallback, source_label):
    short = summarize(project_text, 420)
    if looks_like_menu_garbage(short) and rss_desc:
        short = summarize(rss_desc, 420)

    core_hits = count_matches(project_text, CORE_KEYWORDS)
    anti_hits = count_matches(project_text, ANTI_KEYWORDS)
    verdict, conf, score = verdict_and_confidence(core_hits, anti_hits, len(project_text))
    doable, doable_reason = can_do_1_2_days(project_text, core_hits)

    why = []
    if core_hits:
        why.append(f"CORE: {', '.join(sorted(set(core_hits)))}")
    if anti_hits:
        why.append(f"ANTI: {', '.join(sorted(set(anti_hits)))}")
    if not why:
        why.append("Ключевые слова не найдены.")

    risks = []
    if used_fallback:
        risks.append("Анализ по RSS-ленте — описание проекта может быть неполным.")
    if anti_hits and len(core_hits) <= 1:
        risks.append("Много 'дизайн/маркетинг' — риск нерелевант.")
    if "логин" in normalize(project_text) or "доступ" in normalize(project_text):
        risks.append("Потребуются доступы — без них сроки вырастут.")
    if not risks:
        risks.append("Риски зависят от объёма и доступов.")

    questions = build_questions(project_text)
    need = build_need_from_customer(project_text)
    reply = build_reply_draft(title, url, verdict, conf)

    msg = [
        f"📌 {title}",
        url,
        f"🔍 Источник: {source_label}",
        "",
        "🧾 Краткая выжимка",
        short if short else "Описание не получено.",
        "",
        "📊 Вердикт + 📈 Уверенность",
        f"{verdict}  |  {int(conf*100)}% (score={score})",
        "",
        "📌 Почему",
    ]
    for x in why[:2]:
        msg.append(f"— {x}")
    msg += ["", "❓ Вопросы заказчику"]
    for i, q in enumerate(questions[:7], 1):
        msg.append(f"{i}) {q}")
    msg += ["", "📂 Что нужно от заказчика"]
    for x in need[:6]:
        msg.append(f"— {x}")
    msg += ["", "⏱ Можно ли сделать за 1–2 дня", f"— {doable} ({doable_reason})",
            "", "⚠️ Риски"]
    for x in risks[:5]:
        msg.append(f"— {x}")
    msg += ["", "✉️ Отклик (черновик)", reply]

    return "\n".join(msg).strip(), verdict


# ── ГЛАВНЫЙ ЦИКЛ ──────────────────────────────────────────────────────────────

def main():
    log.info("FL Monitor V2.5 старт (dual source: главная + RSS резерв)")
    log.info("Interval=%ss | max_per_cycle=%s", INTERVAL_SECONDS, MAX_PER_CYCLE)
    tg_send(
        f"🟢 FL Monitor запущен (V2.5)\n"
        f"Источник: главная fl.ru/projects/ + RSS резерв\n"
        f"Интервал: {INTERVAL_SECONDS}s | Лимит: {MAX_PER_CYCLE}/цикл"
    )

    conn = db_connect()
    consecutive_fail = 0

    while True:
        try:
            # Пробуем главную страницу (мгновенные заказы)
            items = fetch_projects_page()
            source_label = "главная fl.ru/projects/"

            if not items:
                consecutive_fail += 1
                items = fetch_rss_items()
                source_label = "RSS-лента fl.ru"
                if consecutive_fail == 1:
                    log.warning("Главная недоступна, переключились на RSS")
            else:
                if consecutive_fail > 0:
                    log.info("Главная снова доступна (было %d сбоев)", consecutive_fail)
                consecutive_fail = 0

            if not items:
                log.warning("Нет заказов ни из одного источника")
                time.sleep(INTERVAL_SECONDS)
                continue

            sent = 0
            for it in items:
                title = it.get("title", "").strip()
                link = it.get("link", "").strip()
                rss_desc = it.get("desc", "").strip()

                if not title or not link:
                    continue

                # Быстрая отсечка по названию — без загрузки страницы
                if title_is_anti(title):
                    log.info("Skip title-anti: %s", title[:90])
                    continue

                pid = extract_id_from_link(link) or sha1(title + link)

                if is_processed(conn, pid):
                    log.info("Already processed: %s", title[:90])
                    continue

                # Загружаем страницу проекта для полного описания
                page_html, _ = http_get(link)
                used_fallback = False
                project_text = try_extract_project_text(page_html) if page_html else ""

                if not project_text or len(project_text) < 140 or looks_like_menu_garbage(project_text):
                    used_fallback = True
                    project_text = rss_desc or title

                message, verdict = build_full_message(
                    title, link, project_text, rss_desc, used_fallback, source_label
                )

                if is_relevant(verdict):
                    ok = tg_send(message)
                    log.info("Send: %s | ok=%s | %s", verdict, ok, title[:90])
                    sent += 1
                else:
                    log.info("Skip: %s | %s", verdict, title[:90])

                mark_processed(conn, pid, link, title)

                if sent >= MAX_PER_CYCLE:
                    break

            time.sleep(INTERVAL_SECONDS)

        except Exception as e:
            log.exception("Loop error: %s", e)
            time.sleep(max(10, INTERVAL_SECONDS))


if __name__ == "__main__":
    main()
