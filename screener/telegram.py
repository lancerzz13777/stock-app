"""Stage 7: send messages to your Telegram, and read commands you send to the bot. Free.

Setup: talk to @BotFather -> /newbot -> copy the token into .env as TELEGRAM_BOT_TOKEN.
Then send any message to your new bot and run  python main.py telegram-test  - it will
find and print your chat id for TELEGRAM_CHAT_ID.
"""
import html
import json
import os
import re

import requests

API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 4000  # Telegram's limit is 4096 characters per message


def _token() -> str | None:
    return os.environ.get("TELEGRAM_BOT_TOKEN") or None


def chat_id() -> str | None:
    return os.environ.get("TELEGRAM_CHAT_ID") or None


def configured() -> bool:
    return bool(_token() and chat_id())


def _call(method: str, params: dict | None = None, http_timeout: int = 20):
    r = requests.post(API.format(token=_token(), method=method), data=params or {}, timeout=http_timeout)
    r.raise_for_status()
    return r.json()


# The button keyboard that stays at the bottom of the chat on your phone
MENU_BUTTONS = [
    ["💼 Wallet", "📊 Picks", "⚡ Day scan"],
    ["📉 Dips", "📰 News", "📒 Paper results"],
    ["🧪 Strategy Lab", "🏦 My demat", "❓ Help"],
]
# What each button does (mapped to the same commands you can type)
BUTTON_COMMANDS = {
    "💼 Wallet": "/wallet", "📊 Picks": "/picks", "⚡ Day scan": "/day", "📉 Dips": "/dips",
    "📰 News": "/news", "📒 Paper results": "/paper", "🧪 Strategy Lab": "/lab", "🏦 My demat": "/demat", "❓ Help": "/help",
}


def esc(s) -> str:
    """Make any text safe inside an HTML-formatted message (stock names can contain & < >)."""
    return html.escape(str(s), quote=False)


def _plain(text: str) -> str:
    return html.unescape(re.sub(r"</?(b|i|u|s|code|pre)>", "", text))


def send(text: str, buttons: list[list[tuple[str, str]]] | None = None, menu: bool = False,
         rich: bool = False) -> bool:
    """Sends text to your chat. Long messages are split. Returns False if not configured/failed.
    buttons: tappable buttons under the message, [[(label, data), ...], ...].
    menu: also (re)show the button keyboard at the bottom of the chat.
    rich: text uses Telegram HTML (<b>bold</b>, <i>italic</i>, <pre>aligned table</pre>)."""
    if not configured():
        return False
    try:
        chunks = [text[i:i + MAX_LEN] for i in range(0, len(text), MAX_LEN)] or [""]
        for n, chunk in enumerate(chunks):
            params = {"chat_id": chat_id(), "text": chunk, "disable_web_page_preview": "true"}
            if rich:
                params["parse_mode"] = "HTML"
            if n == len(chunks) - 1:
                if buttons:
                    params["reply_markup"] = json.dumps({"inline_keyboard": [
                        [{"text": lbl, "callback_data": data} for lbl, data in row] for row in buttons]})
                elif menu:
                    params["reply_markup"] = json.dumps({"keyboard": [[{"text": b} for b in row] for row in MENU_BUTTONS],
                                                         "resize_keyboard": True, "is_persistent": True})
            try:
                _call("sendMessage", params)
            except requests.HTTPError:
                if not rich:
                    raise
                # Formatting rejected (e.g. an unexpected character) - send it as plain text instead
                params.pop("parse_mode")
                params["text"] = _plain(chunk)
                _call("sendMessage", params)
        return True
    except requests.RequestException as e:
        print(f"  ! Telegram send failed: {type(e).__name__}")
        return False


def typing() -> None:
    """Shows 'typing...' in the chat while a slow answer is being prepared."""
    if configured():
        try:
            _call("sendChatAction", {"chat_id": chat_id(), "action": "typing"})
        except requests.RequestException:
            pass


def get_commands(offset: int, wait_seconds: int = 25) -> tuple[list[str], int]:
    """Long-polls for new messages. Only messages from YOUR chat id are returned (others ignored)."""
    if not configured():
        return [], offset
    try:
        data = _call("getUpdates", {"offset": offset, "timeout": wait_seconds}, http_timeout=wait_seconds + 10)
    except requests.RequestException:
        return [], offset
    texts = []
    for u in data.get("result", []):
        offset = max(offset, u["update_id"] + 1)
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) == str(chat_id()) and msg.get("text"):
            text = msg["text"].strip()
            texts.append(BUTTON_COMMANDS.get(text, text))
        cq = u.get("callback_query")  # a tap on a button under a message, e.g. "Exit #3"
        if cq and str(((cq.get("message") or {}).get("chat") or {}).get("id")) == str(chat_id()):
            try:
                _call("answerCallbackQuery", {"callback_query_id": cq["id"]})
            except requests.RequestException:
                pass
            texts.append(cq.get("data", ""))
    return texts, offset


def discover_chat_id() -> list[tuple[str, str]]:
    """Lists (chat id, name) of people who messaged the bot - to help you fill in TELEGRAM_CHAT_ID."""
    data = _call("getUpdates")
    seen = {}
    for u in data.get("result", []):
        chat = (u.get("message") or {}).get("chat") or {}
        if chat:
            seen[str(chat["id"])] = chat.get("first_name") or chat.get("title") or ""
    return list(seen.items())
