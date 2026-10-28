"""Telegram-бот через Bot API (только стандартный Python). Работает, когда пользователь вставил токен бота.
Chat ID программа узнаёт сама: после того как пользователь написал боту /start (метод getUpdates)."""
import html
import json
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.telegram.org/bot{token}/{method}"


class TgError(Exception):
    pass


def call(token, method, params=None, timeout=15):
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(API.format(token=token, method=method), data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            res = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 404):
            raise TgError("токен бота не подошёл") from e
        if e.code == 429:
            raise TgError("Telegram просит слать реже") from e
        try:
            raise TgError(json.loads(e.read().decode()).get("description") or f"ошибка {e.code}") from e
        except ValueError:
            raise TgError(f"ошибка {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise TgError("нет связи с Telegram") from e
    if not res.get("ok"):
        raise TgError(res.get("description") or "Telegram ответил ошибкой")
    return res.get("result")


def bot_name(token):
    return "@" + (call(token, "getMe").get("username") or "?")


def find_chat(token):
    """Chat ID из последнего сообщения боту (например, /start) или None."""
    for upd in reversed(call(token, "getUpdates", {"limit": 20}) or []):
        msg = upd.get("message") or upd.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            return str(chat["id"])
    return None


def send_project(token, chat_id, title, lines, url, silent=False, bid=None):
    """bid — (текст, цена, дней) от Gemini: текст идёт блоком <code>, в Telegram он копируется одним нажатием."""
    text = f'<b><a href="{html.escape(url, quote=True)}">{html.escape(title)}</a></b>\n' + \
        "\n".join(html.escape(x) for x in lines)
    if bid:
        comment, price, days = bid
        text += f"\n\n✍ Ставка: {price} грн, {days} дн. (нажмите на текст — скопируется)\n<code>{html.escape(comment)}</code>"
    call(token, "sendMessage", {
        "chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true",
        "disable_notification": "true" if silent else "false",
        "reply_markup": json.dumps({"inline_keyboard": [[{"text": "Открыть заказ", "url": url}]]}),
    })


def send_text(token, chat_id, text, silent=False):
    call(token, "sendMessage", {"chat_id": chat_id, "text": text,
                                "disable_notification": "true" if silent else "false"})
