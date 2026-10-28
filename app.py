"""FH Монитор для сервера (Render): без окна, только поиск заказов Freelancehunt и сообщения в Telegram
с готовой ставкой от Gemini. Притворяется сайтом: отвечает «ok» на любой адрес, а UptimeRobot заходит
раз в 5 минут, чтобы бесплатный сервер не засыпал.

Ключи — только в переменных окружения Render (Environment), в файлах их нет:
  TG_TOKEN    токен бота от @BotFather (обязательно)
  GEMINI_KEY  ключ Google AI Studio (необязательно: без него придёт заказ без готовой ставки)
  TG_CHAT     необязательно: программа сама узнает чат, когда вы напишете боту /start
  FH_TOKEN    необязательно: токен API Freelancehunt (без него программа на компьютере и сервер не делят лимит)
  INTERVAL    необязательно: как часто проверять, секунд (по умолчанию 10)
Остальные настройки (разделы, стоп-слова, тихие часы) — в config.json, его собирает make_server.py."""
import ctypes
import json
import os
import sys
import threading
import time
import traceback
import types
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("FH_MONITOR_DATA", os.path.join(HERE, "data"))
os.environ["TZ"] = os.environ.get("TZ_NAME", "Europe/Kyiv")  # тихие часы — по киевскому времени
if hasattr(time, "tzset"):
    time.tzset()
try:  # ядро написано для Windows; на Linux ему нужен только тип DWORD
    from ctypes import wintypes  # noqa: F401
except (ImportError, ValueError):
    sys.modules["ctypes.wintypes"] = types.SimpleNamespace(DWORD=ctypes.c_uint32)
    ctypes.wintypes = sys.modules["ctypes.wintypes"]

import fh_core as core  # noqa: E402
import telegram  # noqa: E402
from sites import freelancehunt as fh  # noqa: E402

status = {"started": datetime.now().strftime("%d.%m %H:%M"), "checks": 0, "sent": 0, "last": "", "error": ""}


def load_config():
    s = core.load_settings()
    try:
        with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    for k in ("sections", "filter", "quiet"):
        if k in cfg:
            s[k] = cfg[k]
    return s


def chat_id(token, settings):
    """Чат из TG_CHAT, из прошлого раза или из сообщения /start боту."""
    chat = os.environ.get("TG_CHAT") or settings.get("server_chat")
    if not chat:
        chat = telegram.find_chat(token)
        if chat:
            settings["server_chat"] = chat
            core.save_settings(settings)
            telegram.send_text(token, chat, "FH Монитор на сервере подключён: сюда будут приходить новые заказы "
                                            "с готовой ставкой. Компьютер можно выключать.")
    return chat


def send(token, chat, p, smap, settings, gkey, silent):
    lines = [f"Freelancehunt · {core.fmt_budget(p)} · ставок: {p.get('bids') or 0}",
             f"{core.RATING_TEXT[core.rating(p)]} · {', '.join(smap.sections_of(p))}"]
    bid = None
    if gkey and core.rating(p) != "red":
        try:
            bid = core.ai_bid(p, gkey, settings)
        except core.ApiError as e:
            core.log(f"Gemini: {e}")
    telegram.send_project(token, chat, p["name"], lines, p["url"], silent, bid)


def loop():
    token = os.environ.get("TG_TOKEN", "").strip()
    gkey = os.environ.get("GEMINI_KEY", "").strip()
    fh_key = os.environ.get("FH_TOKEN", "").strip() or None
    interval = max(3, int(os.environ.get("INTERVAL") or 10))
    settings = load_config()
    state = core.load_state()
    smap = core.SectionMap(fh.cached_skills())
    wait = interval
    while True:
        try:
            if not token:
                raise core.ApiError("не задан TG_TOKEN в Environment на Render")
            chat = chat_id(token, settings)
            skills = fh.refresh_skills(fh_key)
            if skills:
                smap = core.SectionMap(skills)
            chosen = set(settings["sections"])
            ctx = {"key": fh_key, "known": set(state["seen"]), "skill_ids": smap.skill_ids(chosen)}
            fresh = core.merge(state, fh.ID, fh.fetch(ctx), datetime.now().astimezone())
            core.save_state(state)
            fresh = [p for p in fresh if core.passes_filter(p, settings)
                     and set(smap.sections_of(p)) & chosen]
            if fresh and chat:
                silent = core.in_quiet(settings)
                for p in sorted(fresh, key=core.sort_key)[-10:]:
                    send(token, chat, p, smap, settings, gkey, silent)
                    status["sent"] += 1
            status["checks"] += 1
            status["last"] = datetime.now().strftime("%H:%M:%S")
            status["error"] = "" if chat else "напишите боту /start в Telegram"
            wait = interval
        except core.RateLimitError as e:
            wait = e.retry_after or min(600, wait * 2)
            status["error"] = f"Freelancehunt просит реже, жду {wait} с"
        except (core.ApiError, telegram.TgError) as e:
            status["error"] = str(e)
            core.log(str(e))
            wait = min(300, wait * 2)
        except Exception:  # noqa: BLE001 — сервер не должен падать
            status["error"] = "непонятная ошибка, см. логи Render"
            print(traceback.format_exc(), flush=True)
            wait = 60
        time.sleep(wait)


class Page(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (f"FH Monitor ok\nзапущен: {status['started']}\nпроверок: {status['checks']}, "
                f"последняя: {status['last']}\nотправлено в Telegram: {status['sent']}\n"
                f"{('ошибка: ' + status['error']) if status['error'] else 'ошибок нет'}\n").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_HEAD = do_GET

    def log_message(self, *args):
        pass


def keep_awake():
    """Бесплатный Render усыпляет сервер через 15 мин без заходов — заходим на свой адрес сами раз в 10 мин
    (запрос идёт через интернет, Render считает его заходом). UptimeRobot тогда не обязателен."""
    url = os.environ.get("RENDER_EXTERNAL_URL")
    while url:
        time.sleep(600)
        try:
            urllib.request.urlopen(url, timeout=30).read()
        except OSError:
            pass


if __name__ == "__main__":
    threading.Thread(target=loop, daemon=True).start()
    threading.Thread(target=keep_awake, daemon=True).start()
    HTTPServer(("0.0.0.0", int(os.environ.get("PORT") or 10000)), Page).serve_forever()
