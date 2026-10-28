"""Freelancehunt — официальное API v2 (https://apidocs.freelancehunt.com).
Лимит API — 1200 запросов в час, поэтому минимум раз в 3 с. Ключ необязателен."""
import time
import urllib.parse

import fh_core as core

ID, NAME, KIND = "fh", "Freelancehunt", "API"
WEB = "https://freelancehunt.com/projects"
DEFAULT_ON = True
INTERVALS = [3, 5, 10, 15, 20, 30, 60, 120, 300, 600]
DEFAULT_INTERVAL = 3
KEY_LABEL = "Токен API (необязательно)"
KEY_HELP = "https://freelancehunt.com/my/api2"

API = "https://api.freelancehunt.com/v2"
MAX_PAGES = 3
PAGE_SIZE = 5         # обычная проверка — 5 самых свежих заказов
FIRST_PAGE_SIZE = 10  # первая — побольше, чтобы заполнить список
SKILLS_TTL = 24 * 3600
HEADERS = {"Accept": "application/json", "Accept-Language": "uk"}


def parse(p):
    a = p.get("attributes") or {}
    budget = a.get("budget") or {}
    web = ((p.get("links") or {}).get("self") or {}).get("web")
    return {
        "id": f"fh:{p['id']}", "site": ID,
        "name": " ".join((a.get("name") or "").split()),
        "amount": budget.get("amount"), "currency": budget.get("currency"),
        "bids": a.get("bid_count") or 0,
        "published": a.get("published_at"), "exact_time": True,
        "skills": [s.get("id") for s in a.get("skills") or []],
        "url": web or WEB,
        "plus": bool(a.get("is_only_for_plus")),
        "desc": (a.get("description") or "")[:3000],
    }


def fetch(ctx):
    """Свежие заказы, новые сверху. Следующую страницу берём, только если на этой всё незнакомое."""
    known, got = ctx["known"], []
    size = PAGE_SIZE if any(i.startswith("fh:") for i in known) else FIRST_PAGE_SIZE
    for page in range(1, MAX_PAGES + 1):
        # t= — метка времени: без неё сервер отдаёт копию списка, которая обновляется раз в минуту
        params = {"page[number]": page, "page[size]": size, "t": int(time.time() * 1000)}
        if ctx.get("skill_ids") is not None:
            params["filter[skill_id]"] = ",".join(str(i) for i in sorted(ctx["skill_ids"]))
        try:
            data = core.get_json(API + "/projects?" + urllib.parse.urlencode(params), HEADERS, ctx.get("key"))
        except core.TokenError:
            ctx["key_error"] = True  # ключ не подошёл — продолжаем без него
            ctx["key"] = None
            data = core.get_json(API + "/projects?" + urllib.parse.urlencode(params), HEADERS)
        items = [parse(p) for p in data.get("data") or []]
        got.extend(items)
        if not items or not (data.get("links") or {}).get("next") or any(p["id"] in known for p in items):
            break
    return got


def check_key(key):
    attrs = (core.get_json(API + "/my/profile", HEADERS, key).get("data") or {}).get("attributes") or {}
    return attrs.get("login") or "?"


def cached_skills():
    return {int(k): v for k, v in core.load_json("skills.json", {}).get("skills", {}).items()}


def refresh_skills(key):
    """Раз в сутки берёт список навыков: новые навыки сайта попадут в «Інше»."""
    cache = core.load_json("skills.json", {})
    if cache.get("skills") and time.time() - cache.get("ts", 0) < SKILLS_TTL:
        return None
    data = core.get_json(API + "/skills", HEADERS, key)
    skills = {str(s["id"]): (s.get("attributes") or {}).get("name") or s.get("name") or str(s["id"])
              for s in data.get("data") or []}
    if skills:
        core.save_json("skills.json", {"ts": time.time(), "skills": skills})
    return {int(k): v for k, v in skills.items()}
