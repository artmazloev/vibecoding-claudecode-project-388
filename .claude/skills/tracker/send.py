#!/usr/bin/env python3
"""Отправка текста в Telegram через Bot API.

Только стандартная библиотека (urllib) — ничего ставить не нужно.

Токен и chat id берутся из переменных окружения TELEGRAM_BOT_TOKEN и
TELEGRAM_CHAT_ID. Если их нет — ищется файл .env (рядом со скриптом или в
текущей директории) со строками:

    TELEGRAM_BOT_TOKEN=1234567:AA...
    TELEGRAM_CHAT_ID=12345678

Запуск:
    python send.py "текст сообщения"
    # или, если python не найден:
    python3 send.py "текст сообщения"
"""
import os
import sys
import json
import urllib.request
import urllib.error
import urllib.parse


def load_env_file():
    """Подхватить TELEGRAM_* из .env, не перетирая уже заданные переменные."""
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(os.getcwd(), ".env"), os.path.join(here, ".env")):
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                os.environ.setdefault(key, value)


def send(text):
    load_env_file()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        sys.stderr.write(
            "Не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID "
            "(переменные окружения или .env рядом).\n"
        )
        return 1

    url = "https://api.telegram.org/bot%s/sendMessage" % token
    data = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}
    ).encode("utf-8")

    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", "replace")
        sys.stderr.write("Ошибка Telegram API (HTTP %s): %s\n" % (err.code, body))
        return 1
    except urllib.error.URLError as err:
        sys.stderr.write("Сеть недоступна: %s\n" % err.reason)
        return 1

    if payload.get("ok"):
        print("Отправлено.")
        return 0
    sys.stderr.write("Telegram вернул ошибку: %s\n" % json.dumps(payload, ensure_ascii=False))
    return 1


def main():
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        sys.stderr.write('Использование: python send.py "текст сообщения"\n')
        return 2
    return send(sys.argv[1])


if __name__ == "__main__":
    sys.exit(main())
