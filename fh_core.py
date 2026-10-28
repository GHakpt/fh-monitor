"""Ядро монитора: общие запросы, хранение, слияние заказов разных сайтов, форматирование.
Сами сайты — отдельные модули в папке sites. Без окна, чтобы можно было проверить отдельно."""
import base64
import ctypes
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from ctypes import wintypes
from datetime import datetime, timedelta

import rules
import sections

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("FH_MONITOR_DATA") or os.path.join(APP_DIR, "data")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) FH-Monitor/2.0 (personal desktop app)"

KEEP_PROJECTS = 500                   # сколько заказов держать в списке (все сайты вместе)
KEEP_SEEN = 10000                     # сколько номеров заказов помнить
FRESH_WINDOW = timedelta(minutes=30)  # «новый» = опубликован не раньше чем за 30 мин до прошлой проверки сайта


# ---------- запросы ----------

class ApiError(Exception):
    pass


class TokenError(ApiError):
    pass


class RateLimitError(ApiError):
    def __init__(self, msg, retry_after=None):
        super().__init__(msg)
        self.retry_after = retry_after


def http_get(url, headers=None, token=None, timeout=20):
    """Текст ответа. Ошибки переводятся в понятные ApiError."""
    h = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    h.update(headers or {})
    if token:
        h["Authorization"] = "Bearer " + token
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code == 401 and token:
            raise TokenError("Ключ не подошёл") from e
        if e.code == 429:
            try:
                retry = int(e.headers.get("Retry-After") or 0) or None
            except ValueError:
                retry = None
            raise RateLimitError("сайт просит проверять реже", retry) from e
        if e.code == 403:
            raise ApiError("сайт не пускает программу (403)") from e
        raise ApiError(f"сайт ответил ошибкой {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ApiError("нет связи с сайтом (интернет?)") from e


def fix_xml(text):
    """Голый «&» (например, «V&D Agency») ломает ленту — экранируем его, не трогая &amp; и &#123;."""
    return re.sub(r"&(?!#?\w+;)", "&amp;", text)


def get_json(url, headers=None, token=None):
    try:
        return json.loads(http_get(url, headers, token))
    except ValueError as e:
        raise ApiError("сайт прислал непонятный ответ") from e


# ---------- разделы ----------

class SectionMap:
    """Разделы для заказов Freelancehunt (по навыкам) и остальных сайтов (по словам в категориях)."""

    def __init__(self, site_skills=None):
        self.skills = {name: dict(m) for name, m in sections.SECTIONS.items()}
        known = {sid for m in self.skills.values() for sid in m}
        for sid, name in (site_skills or {}).items():
            if sid not in known:
                self.skills[sections.OTHER][sid] = name
        self.owner = {sid: name for name, m in self.skills.items() for sid in m}

    def skill_ids(self, chosen):
        if set(chosen) >= set(self.skills):
            return None  # отмечено всё — фильтр не нужен
        return {sid for name in chosen for sid in self.skills.get(name, {})}

    def sections_of(self, project):
        if project.get("site", "fh") == "fh":
            names = {self.owner.get(sid, sections.OTHER) for sid in project.get("skills") or []}
        else:
            names = sections.classify(f"{project.get('cats') or ''} {project.get('name') or ''}")
        return [n for n in sections.ORDER if n in (names or {sections.OTHER})]

    def describe(self, name):
        words = ", ".join(sections.KEYWORDS.get(name, [])[:12])
        return ", ".join(sorted(self.skills.get(name, {}).values())) + \
            (f"\n\nДругие сайты — по словам: {words}…" if words else "")


# ---------- хранение ----------

def _path(name):
    return os.path.join(DATA_DIR, name)


def load_json(name, default):
    try:
        with open(_path(name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(name, data):
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _path(name))


def log(text):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        path = _path("log.txt")
        if os.path.exists(path) and os.path.getsize(path) > 300_000:
            os.replace(path, path + ".old")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {text.rstrip()}\n")
    except OSError:
        pass


DEFAULT_SETTINGS = {
    "sections": sections.DEFAULT_ON,
    "notify": True,
    "show_delay": True,   # колонка «Узнали через» (для теста)
    "site_filter": "",    # "" — все сайты
    "sites": {},          # id сайта -> {"enabled": bool, "key": зашифрованный ключ, "interval": сек}
    "filter": {"min_budget": 0, "stop_words": rules.DEFAULT_STOP_WORDS, "hide_plus": False, "hide_red": False},
    "quiet": {"enabled": False, "from": "23:00", "to": "08:00"},  # тихие часы: без звука и всплывающих окон
    "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},  # bot_token зашифрован, как ключи
    "geometry": "",
}


def load_settings():
    s = json.loads(json.dumps(DEFAULT_SETTINGS))
    saved = load_json("settings.json", {})
    for k in ("filter", "quiet", "telegram"):  # вложенные: дополняем, а не заменяем
        s[k].update(saved.pop(k, {}) or {})
    s.update(saved)
    s["sections"] = [n for n in s["sections"] if n in sections.SECTIONS]
    # перенос из версии 1: токен и интервал Freelancehunt
    fh = s["sites"].setdefault("fh", {})
    if s.get("token") and not fh.get("key"):
        fh["key"] = s["token"]
    if s.get("interval_sec") and not fh.get("interval"):
        fh["interval"] = s["interval_sec"]
    for old in ("token", "interval_sec", "interval_min"):
        s.pop(old, None)
    return s


def save_settings(s):
    save_json("settings.json", s)


def site_conf(settings, site):
    """Настройки сайта с подстановкой значений по умолчанию из модуля."""
    c = settings["sites"].setdefault(site.ID, {})
    c.setdefault("enabled", site.DEFAULT_ON)
    c.setdefault("key", "")
    if c.get("interval") not in site.INTERVALS:
        c["interval"] = site.DEFAULT_INTERVAL
    return c


def get_key(settings, site_id):
    return unprotect((settings["sites"].get(site_id) or {}).get("key") or "")


def set_key(settings, site_id, key):
    settings["sites"].setdefault(site_id, {})["key"] = protect(key.strip())


def load_state():
    st = load_json("state.json", {})
    last = st.get("last_check") or {}
    if isinstance(last, str):  # версия 1: одна дата для Freelancehunt
        last = {"fh": last}
    seen = [i if isinstance(i, str) else f"fh:{i}" for i in st.get("seen", [])]
    projects = st.get("projects", [])
    for p in projects:
        if not isinstance(p["id"], str):
            p["id"] = f"fh:{p['id']}"
        p.setdefault("site", "fh")
        p.setdefault("exact_time", True)
    return {"last_check": last, "seen": seen, "projects": projects,
            "stats": st.get("stats") or {"bids": [], "won": []}}


def save_state(state):
    save_json("state.json", state)


def merge(state, site, fetched, now):
    """Кладёт заказы сайта в state. Возвращает «свежие» — о них стоит уведомить.
    Уже виденные не добавляются второй раз, у них только обновляются ставки и бюджет.
    Первая проверка сайта — без уведомлений: просто заполняем список."""
    seen = set(state["seen"])
    by_id = {p["id"]: p for p in state["projects"]}
    first = not any(i.startswith(site + ":") for i in seen)
    last_s = state["last_check"].get(site)
    last = parse_time(last_s) if last_s else None
    fresh = []
    for p in fetched:
        old = by_id.get(p["id"])
        if old:
            for k in ("bids", "name", "amount", "currency", "budget_text", "desc", "cats"):
                if p.get(k) is not None:
                    old[k] = p[k]
            continue
        if p["id"] in seen:
            continue
        if p.get("exact_time") and p.get("published"):
            p["new"] = bool(last and not first and parse_time(p["published"]) >= last - FRESH_WINDOW)
            if p["new"]:
                p["delay"] = max(0, int((now - parse_time(p["published"])).total_seconds()))
        else:
            # время на сайте неточное: «новый» — всё, чего не было на прошлой проверке
            p["new"] = not first
            p["exact_time"] = False
            p["published"] = now.isoformat(timespec="seconds") if p["new"] else None
        state["projects"].append(p)
        by_id[p["id"]] = p
        state["seen"].append(p["id"])
        seen.add(p["id"])
        if p["new"]:
            fresh.append(p)
    state["projects"].sort(key=sort_key, reverse=True)
    del state["projects"][KEEP_PROJECTS:]
    del state["seen"][:-KEEP_SEEN]
    state["last_check"][site] = now.isoformat(timespec="seconds")
    return fresh


# ---------- правила: оценка, фильтр, тихие часы ----------

def _haystack(p):
    return " " + re.sub(r"[,/()|;:\-]+", " ", " ".join(
        [p.get("name") or "", p.get("cats") or "", (p.get("desc") or "")[:600]]).lower()) + " "


def rating(p):
    """'red' / 'green' / 'yellow' по словам из rules.py. Plus-заказ без Plus-профиля — 'red'."""
    t = _haystack(p)
    if p.get("plus") or any(w in t for w in rules.RED):
        return "red"
    if any(w in t for w in rules.GREEN):
        return "green"
    return "yellow"


RATING_TEXT = {"green": "✓ брать", "yellow": "~ можно", "red": "✗ нет"}


def budget_uah(p):
    """Верхняя граница бюджета в гривнах (примерно) или None, если бюджета нет."""
    if p.get("amount") is not None:
        return p["amount"] * rules.RATES.get(p.get("currency") or "UAH", 1)
    text = (p.get("budget_text") or "").replace(" ", " ")
    digits = re.sub(r"(?<=\d)[.,](?=\d{3}\b)", "", text)  # 1.000 / 1,000 -> 1000
    nums = [float(n.replace(" ", "")) for n in re.findall(r"\d[\d ]*", digits)]
    if not nums:
        return None
    rate = next((v for k, v in rules.RATES.items() if k in text), rules.RATES.get(p.get("currency") or "", 1))
    if "час" in text or "hour" in text:
        nums = [n * 8 for n in nums]  # почасовой: считаем как один рабочий день
    return max(nums) * rate


def stop_words(settings):
    return [w.strip().lower() for w in (settings["filter"].get("stop_words") or "").split(",") if w.strip()]


def passes_filter(p, settings):
    """Фильтр мусора: стоп-слова, минимальный бюджет, только Plus, оценка «нет»."""
    f = settings["filter"]
    t = _haystack(p)
    if any(w in t for w in stop_words(settings)):
        return False
    if f.get("hide_plus") and p.get("plus"):
        return False
    if f.get("hide_red") and rating(p) == "red":
        return False
    mb = f.get("min_budget") or 0
    if mb:
        b = budget_uah(p)
        if b is not None and b < mb:  # договорной бюджет не прячем
            return False
    return True


def in_quiet(settings, now=None):
    q = settings["quiet"]
    if not q.get("enabled"):
        return False
    try:
        a, b = (datetime.strptime(q[k], "%H:%M").time() for k in ("from", "to"))
    except (KeyError, ValueError):
        return False
    t = (now or datetime.now()).time()
    return a <= t < b if a < b else (t >= a or t < b)


# ---------- черновик ставки по шаблонам из cases.py ----------

def detect_case(p):
    """Тип заказа: шаблон, чьих слов больше всего в названии и начале описания."""
    import cases
    title = _haystack({"name": p.get("name")})
    body = _haystack({"cats": p.get("cats"), "desc": p.get("desc")})

    def score(c):  # слово в названии весит втрое больше, чем в описании
        return sum(3 * (w in title) + (w in body) for w in c["words"])
    best = max(cases.CASES, key=score)
    return best if score(best) else cases.OTHER


def first_sentence(text, limit=160):
    s = re.split(r"(?<=[.!?])\s|\n", (text or "").strip(), maxsplit=1)[0]
    return s[:limit].rstrip(" ,;:") + ("…" if len(s) > limit else "")


def suggest_price(p, case):
    """Немного ниже бюджета заказчика (мы новички) или середина вилки типа заказа."""
    b = budget_uah(p)
    lo, hi = case["price"]
    price = b * 0.9 if b else (lo + hi) / 2
    return max(100, int(round(price / 50.0)) * 50)


DETAIL_MARK = "[ОДНА ДЕТАЛЬ З ТЗ — перепишіть своїми словами: {}]"


def build_reply(p, case, price=None, days=None):
    """Текст ставки. Квадратные скобки — место, которое надо переписать руками перед отправкой."""
    price = price or suggest_price(p, case)
    days = days or case["days"]
    task = (p.get("name") or "").strip().rstrip(".")
    detail = first_sentence(p.get("desc"))
    text = case["template"].format(
        задача=task, деталь=DETAIL_MARK.format(detail) if detail else "", срок=f"{days} дн.",
        цена=price, пример=case.get("example") or "")
    return re.sub(r" +\n", "\n", text).strip(), price, days


# ---------- «Беру» и «Скопировать для Claude» ----------

FREELANCE_DIR = os.path.normpath(os.path.join(APP_DIR, "..", ".."))  # ...\Desktop\Freelance
_TR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюяіїєґ",
               ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s",
                "t", "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya", "i", "yi", "ye", "g"]))


def slug(text, words=4):
    t = "".join(_TR.get(c, c) for c in text.lower())
    parts = re.findall(r"[a-z0-9]+", t)
    return "-".join(parts[:words]) or "zakaz"


def order_text(p, site_name):
    lines = [f"Сайт: {site_name}", f"Заказ: {p.get('name')}", f"Бюджет: {fmt_budget(p)}",
             f"Ставок: {fmt_bids(p)}", f"Опубликован: {fmt_time(p.get('published'), exact=p.get('exact_time', True))}",
             f"Категории: {p.get('cats') or ', '.join(map(str, p.get('skills') or [])) or '—'}",
             f"Ссылка: {p.get('url')}", "", "Описание:", (p.get("desc") or "(нет в списке — смотрите по ссылке)")]
    return "\n".join(lines)


def claude_text(p, site_name):
    return ("Оцени заказ по правилам из CLAUDE.md (вердикт, что хотят, кто что делает, время, риски, ставка, "
            "вопросы заказчику):\n\n" + order_text(p, site_name))


def take_order(p, site_name):
    """Строка в ЗАКАЗЫ.md + папка orders\\ГГГГ-ММ-ДД_кратко с ТЗ.md. Возвращает путь к папке."""
    day = datetime.now().strftime("%Y-%m-%d")
    folder = os.path.join(FREELANCE_DIR, "orders", f"{day}_{slug(p.get('name') or '')}")
    os.makedirs(folder, exist_ok=True)
    tz = os.path.join(folder, "ТЗ.md")
    if not os.path.exists(tz):
        with open(tz, "w", encoding="utf-8") as f:
            f.write(f"# {p.get('name')}\n\n{order_text(p, site_name)}\n\n## Переписка и договорённости\n\n")
    journal_set(p, site_name, "Взял в работу (FH Монитор)")
    return folder


def journal_set(p, site_name, status):
    """Строка заказа в ЗАКАЗЫ.md: добавить или, если уже есть (по ссылке), поменять статус."""
    journal = os.path.join(FREELANCE_DIR, "ЗАКАЗЫ.md")
    url = p.get("url") or ""
    try:
        with open(journal, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        lines = ["# Журнал заказов", "", "| Дата | Площадка | Заказ | Вердикт | Ставка (цена / срок) | Статус |",
                 "|------|----------|-------|---------|----------------------|--------|"]
    for i, line in enumerate(lines):
        if url and url in line and line.startswith("|"):
            cells = line.split("|")
            if len(cells) >= 8:
                cells[-2] = f" {status} "
                lines[i] = "|".join(cells)
            break
    else:
        mark = {"green": "🟢", "yellow": "🟡", "red": "🔴"}[rating(p)]
        name = (p.get("name") or "").replace("|", "/")
        lines.append(f"| {datetime.now():%Y-%m-%d} | {site_name} | {name} ({fmt_budget(p)}, {url}) | {mark} | — "
                     f"| {status} |")
    with open(journal, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ---------- статистика: ставки и взятые заказы (хранится отдельно от списка, он обрезается) ----------

def bid_url(p, comment, price, days):
    """Ссылка на заказ; у Freelancehunt — с готовой ставкой после #fhm= для скрипта fh_bid_fill.user.js
    (Tampermonkey сам откроет окно «Публікація ставки» и впишет текст, цену и срок)."""
    if p["site"] != "fh":
        return p["url"]
    data = {"c": comment, "p": str(price), "d": str(days)}
    return p["url"] + "#fhm=" + base64.urlsafe_b64encode(
        json.dumps(data, ensure_ascii=False).encode()).decode().rstrip("=")


def auto_bid_url(p, settings=None):
    """Готовая ставка: нейросетью Gemini (если есть ключ), иначе по шаблону — подсказка в [скобках] убирается."""
    settings = settings or load_settings()
    key = get_key(settings, "gemini")
    if key and p.get("site") == "fh":
        try:
            model = settings.get("gemini_model")
            text, price, days = ai_bid(p, key, settings)
            if settings.get("gemini_model") != model:
                save_settings(settings)  # рабочая модель — первой в следующий раз, без ожидания
            return bid_url(p, text, price, days)
        except ApiError as e:
            log(f"Gemini: {e} — ставка по шаблону")
    body, price, days = build_reply(p, detect_case(p))
    return bid_url(p, re.sub(r"[ \t]*\[[^\]]*\]", "", body).strip(), price, days)


# ---------- ставка нейросетью Gemini (бесплатный ключ Google AI Studio) ----------

GEMINI_MODELS = ["gemini-flash-latest"]  # запасная; основной список берём у Google (gemini_models)


def gemini_models(key):
    """Модели «flash», доступные ключу, самые новые первыми (старые Google закрывает для новых ключей)."""
    try:
        data = json.loads(http_get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                                   {"x-goog-api-key": key}))
    except ApiError:
        return []
    names = [m["name"].split("/", 1)[1] for m in data.get("models", [])
             if "generateContent" in m.get("supportedGenerationMethods", [])]

    def version(n):
        return tuple(int(x) for x in re.findall(r"\d+", n.split("-")[1])) if re.match(r"gemini-\d", n) else (0,)
    full = sorted((n for n in names if re.fullmatch(r"gemini-[\d.]+-flash", n)), key=version, reverse=True)
    lite = sorted((n for n in names if re.fullmatch(r"gemini-[\d.]+-flash-lite", n)), key=version, reverse=True)
    return full[:3] + lite[:2]

BID_RULES = """Ты пишешь ставку (отклик) на заказ на бирже Freelancehunt от имени фрилансера из Украины.
Кто он: начинающий фрилансер, отзывов пока нет. Работает вместе с нейросетью Claude: сайты и лендинги,
Telegram-боты, парсеры, скрипты, Excel и Google-таблицы, тексты и переводы, монтаж видео (нарезка, Shorts,
субтитры, реклама из стоков), простое 3D в Blender (предметы, рендеры, модели для печати, DXF).
Его работы (упоминай одну, только если она реально похожа на заказ): W02 Shorts на украинском с субтитрами
по слову; W03 рекламный ролик курсов итальянского; W04 рекламный тизер eSIM; W05 картинки Amazon A+;
W07 моушн-ролики; W09 DXF для лазерной резки; W10 фигура для 3D-печати; W11 механика тестера (3D, чертежи);
W13 программа-монитор заказов с Telegram-ботом. Похожей нет — не упоминай работы вообще.
Правила ставки:
- язык заказа (украинский или русский);
- 4–8 предложений, без воды и шаблонов вроде «Здравствуйте, я опытный специалист»;
- первая фраза — про задачу заказчика его же словами, чтобы было видно, что ТЗ прочитано;
- коротко как сделаешь (2–3 шага с конкретикой из ТЗ), срок, цена в гривнах, оплата через Сейф;
- в конце один уточняющий вопрос по сути заказа;
- не выдумывай опыт, отзывы и проекты; можно предложить бесплатно сделать маленький пример, если это меньше часа;
- без длинных тире, без «не просто…, а…», без markdown, без эмодзи, без контактов и ссылок.
Цена: новичок, чуть ниже рынка и бюджета заказчика (примерно 80–90% бюджета), но без демпинга; срок реалистичный
с запасом (если заказчик указал срок — не дольше его).
Ответ — только JSON: {"comment": "текст ставки", "price_uah": число, "days": число}"""


def ai_bid(p, key, settings=None):
    """(текст, цена в грн, дней) от Gemini. Бросает ApiError — тогда берём шаблон."""
    budget = fmt_budget(p)
    order = (f"Заказ: {p.get('name')}\nБюджет: {budget}"
             + (f" (≈ {budget_uah(p)} грн)" if budget_uah(p) and p.get("currency") not in (None, "UAH") else "")
             + f"\nНавыки: {p.get('cats') or ''}\nОписание:\n{(p.get('desc') or '')[:3500]}")
    body = json.dumps({"systemInstruction": {"parts": [{"text": BID_RULES}]},
                       "contents": [{"role": "user", "parts": [{"text": order}]}],
                       "generationConfig": {"responseMimeType": "application/json", "temperature": 0.6}}).encode()
    saved = (settings or {}).get("gemini_model")
    models = [saved] if saved else []
    models += [m for m in gemini_models(key) + GEMINI_MODELS if m not in models]
    last = None
    for model in models:
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            data=body, method="POST",
            headers={"Content-Type": "application/json", "x-goog-api-key": key, "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:  # перегруженная модель молчит минутами — дальше
                res = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = f"{model}: ошибка {e.code}"
            detail = e.read().decode("utf-8", "replace")
            if e.code in (401, 403) or "API_KEY" in detail or "API key" in detail:
                raise ApiError("ключ Gemini не подошёл — скопируйте его заново") from e
            if e.code == 400:
                raise ApiError(last) from e
            continue  # 404 — модели нет, 429/5xx — попробуем следующую
        except TimeoutError:
            last = f"{model}: не ответил за 15 с"
            continue
        except (urllib.error.URLError, OSError) as e:
            if isinstance(getattr(e, "reason", None), TimeoutError):
                last = f"{model}: не ответил за 15 с"
                continue
            raise ApiError("нет связи с Gemini") from e
        try:
            text = res["candidates"][0]["content"]["parts"][0]["text"]
            data = json.loads(text[text.find("{"):text.rfind("}") + 1])
            comment = str(data["comment"]).strip()
            price = max(100, int(round(float(data.get("price_uah") or 0) / 50.0)) * 50)
            days = max(1, int(data.get("days") or 1))
        except (KeyError, IndexError, ValueError, TypeError):
            last = f"{model}: непонятный ответ"
            continue
        if settings is not None and settings.get("gemini_model") != model:
            settings["gemini_model"] = model
        return comment, price, days
    raise ApiError(last or "Gemini не ответил")


def open_url(url):
    """В Chrome: там вход на Freelancehunt и скрипт Tampermonkey. Нет Chrome — браузер по умолчанию."""
    chrome = next((c for c in (os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
                               os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
                               os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"))
                   if os.path.exists(c)), None)
    if chrome:
        import subprocess
        subprocess.Popen([chrome, url])
    else:
        import webbrowser
        webbrowser.open_new_tab(url)


def fetch_my_bids(token, pages=3):
    """Мои ставки на Freelancehunt (новые сверху): [{project_id, project_url, name, status, winner, published}].
    status: active, revoked, rejected. Чтение API FH по-прежнему открыто (закрыта только отправка)."""
    out = []
    for n in range(1, pages + 1):
        data = json.loads(http_get(f"https://api.freelancehunt.com/v2/my/bids?page[number]={n}",
                                   {"Accept": "application/json"}, token=token))
        for b in data.get("data") or []:
            a = b.get("attributes") or {}
            pr = a.get("project") or {}
            out.append({"project_id": pr.get("id"), "name": pr.get("name") or "",
                        "status": a.get("status"), "winner": bool(a.get("is_winner")),
                        "project_status": (pr.get("status") or {}).get("name") or "",
                        "published": a.get("published_at")})
        if not (data.get("links") or {}).get("next"):
            break
    return out


def stats_add(state, kind, p):
    """kind: 'bids' (ставка отправлена) или 'won' (Беру). Один заказ учитывается один раз."""
    log_ = state.setdefault("stats", {}).setdefault(kind, [])
    if any(x["id"] == p["id"] for x in log_):
        return
    log_.append({"id": p["id"], "site": p["site"], "date": datetime.now().isoformat(timespec="seconds"),
                 "budget": budget_uah(p), "rating": rating(p)})


def stats_remove(state, kind, pid):
    st = state.setdefault("stats", {})
    st[kind] = [x for x in st.get(kind, []) if x["id"] != pid]


# ---------- форматирование ----------

CURRENCY = {"UAH": "грн", "USD": "$", "EUR": "€", "PLN": "zł", "RUB": "₽", "GBP": "£", "INR": "₹"}


def parse_time(s):
    return datetime.fromisoformat(s)


def sort_key(p):
    try:
        return parse_time(p["published"]).timestamp()
    except (TypeError, ValueError, KeyError):
        return 0


def fmt_budget(p):
    if p.get("amount") is None:
        return p.get("budget_text") or "договорная"
    n = f"{int(p['amount']):,}".replace(",", " ")
    return f"{n} {CURRENCY.get(p.get('currency'), p.get('currency') or '')}".strip()


def fmt_bids(p):
    return "—" if p.get("bids") is None else p["bids"]


def fmt_delay(p):
    d = p.get("delay")
    if d is None:
        return "—"
    return f"{d} с" if d < 120 else f"{d // 60} мин"


def fmt_time(s, now=None, exact=True):
    try:
        t = parse_time(s).astimezone()
    except (TypeError, ValueError):
        return "—"
    now = now or datetime.now().astimezone()
    secs = max(0, int((now - t).total_seconds()))
    mins = secs // 60
    if secs < 60:
        text = f"{secs} с назад"
    elif mins < 60:
        text = f"{mins} мин назад"
    elif mins < 24 * 60:  # счётчик идёт в самой программе, к сайту не обращаемся
        text = f"{mins // 60} ч {mins % 60} мин назад"
    elif t.date() == (now - timedelta(days=1)).date():
        text = "вчера " + t.strftime("%H:%M")
    else:
        text = t.strftime("%d.%m %H:%M")
    return text if exact else "≈ " + text  # «≈»: сайт не сообщает точное время, это когда мы его увидели


# ---------- шифрование ключей (Windows DPAPI) ----------

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data, encrypt):
    buf = ctypes.create_string_buffer(data, len(data))
    inp = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    fn = ctypes.windll.crypt32.CryptProtectData if encrypt else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(inp), None, None, None, None, 0x1, ctypes.byref(out)):  # 0x1: без окон
        raise OSError("DPAPI error")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def protect(text):
    return base64.b64encode(_dpapi(text.encode("utf-8"), True)).decode() if text else ""


def unprotect(value):
    try:
        return _dpapi(base64.b64decode(value), False).decode("utf-8") if value else ""
    except (OSError, ValueError):
        return ""
