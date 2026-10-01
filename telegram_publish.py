"""Minimal Telegram photo publisher for AUQNI. Dry-run is the default."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import time
import urllib.request
import uuid


DEFAULT_CHANNEL = "@auqni_qms"
MAX_CAPTION_CHARS = 1024
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_DIMENSION_SUM = 10_000
MAX_ASPECT_RATIO = 20


class PublishError(Exception):
    """A safe error that never contains credentials or raw API data."""


class ApiRejected(PublishError):
    """Telegram returned a definite rejection."""


class UncertainOutcome(PublishError):
    """Transport failed after request initiation; delivery is unknown."""


def read_token(env_path=Path(".env")):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if token:
        return token
    try:
        for line in env_path.read_text(encoding="utf-8-sig").splitlines():
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def load_caption(path):
    try:
        caption = path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError):
        raise PublishError("Cannot read caption as UTF-8 text.") from None
    if not caption:
        raise PublishError("Caption is empty.")
    if len(caption) > MAX_CAPTION_CHARS:
        raise PublishError("Caption exceeds the 1024-character sendPhoto limit.")
    return caption


def load_content_caption(path):
    try:
        content = json.loads(path.read_text(encoding="utf-8-sig"))
        if content.get("schema_version") != "auqni-content/v1":
            raise ValueError
        caption = content["platforms"]["telegram"]["content"]
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError):
        raise PublishError("Cannot read platforms.telegram.content from auqni-content/v1 JSON.") from None
    if not isinstance(caption, str) or not caption or caption != caption.strip():
        raise PublishError("Telegram content must be nonempty text without edge whitespace.")
    if len(caption) > MAX_CAPTION_CHARS:
        raise PublishError("Caption exceeds the 1024-character sendPhoto limit.")
    return caption


def verify_text_copy(path, caption):
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        raise PublishError("Cannot read Telegram text copy as UTF-8.") from None
    if text != caption:
        raise PublishError("Telegram text copy differs from platforms.telegram.content.")


def load_png(path):
    try:
        data = path.read_bytes()
    except OSError:
        raise PublishError("Cannot read image file.") from None
    if len(data) > MAX_PHOTO_BYTES:
        raise PublishError("PNG exceeds the 10 MiB sendPhoto limit.")
    if len(data) < 24 or not data.startswith(b"\x89PNG\r\n\x1a\n") or data[12:16] != b"IHDR":
        raise PublishError("Expected a PNG file with a valid IHDR header.")
    width, height = struct.unpack(">II", data[16:24])
    if width < 1 or height < 1:
        raise PublishError("PNG dimensions are invalid.")
    if width + height > MAX_DIMENSION_SUM:
        raise PublishError("PNG width plus height exceeds 10000 pixels.")
    if max(width / height, height / width) > MAX_ASPECT_RATIO:
        raise PublishError("PNG aspect ratio exceeds 20.")
    return data, width, height


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def idempotency_key(channel, caption, image):
    material = b"sendPhoto\0" + channel.encode() + b"\0" + caption.encode() + b"\0" + image
    return sha256(material)


def multipart_body(channel, caption, image):
    boundary = "auqni" + uuid.uuid4().hex
    chunks = []

    def field(name, value):
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f'{value}\r\n'.encode("utf-8")
        )

    field("chat_id", channel)
    field("caption", caption)
    chunks.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="image.png"\r\n'
        f'Content-Type: image/png\r\n\r\n'.encode("ascii") + image + b"\r\n"
    )
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), "multipart/form-data; boundary=" + boundary


def request_json(token, body, content_type, timeout=30):
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    request = urllib.request.Request(url, data=body, headers={"Content-Type": content_type}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except Exception:
        raise UncertainOutcome("Telegram transport/HTTP/JSON failure; outcome may be uncertain.") from None


def publish(token, channel, caption, image):
    body, content_type = multipart_body(channel, caption, image)
    result = request_json(token, body, content_type)
    if not isinstance(result, dict) or result.get("ok") is not True:
        code = result.get("error_code") if isinstance(result, dict) else None
        safe_code = str(code) if type(code) is int else "unknown"
        raise ApiRejected("Telegram API rejected the request with error " + safe_code + ".")
    message = result.get("result")
    if not isinstance(message, dict) or type(message.get("message_id")) is not int:
        raise PublishError("Telegram success response has no message_id.")
    chat = message.get("chat")
    username = chat.get("username") if isinstance(chat, dict) else None
    if channel.startswith("@") and username != channel[1:]:
        raise PublishError("Telegram response identifies a different target chat.")
    return message["message_id"], chat.get("id") if isinstance(chat, dict) else None


def journal_entries(path):
    if not path.exists():
        return []
    try:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError):
        raise PublishError("Publication journal cannot be read.") from None


def append_journal(path, record):
    try:
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise PublishError("Publication journal cannot be updated.") from None


class JournalLock:
    def __init__(self, journal_path):
        self.path = Path(str(journal_path) + ".lock")
        self.fd = None

    def __enter__(self):
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(self.fd, str(os.getpid()).encode("ascii"))
        except FileExistsError:
            raise PublishError("Publication journal is locked by another or unresolved attempt.") from None
        except OSError:
            raise PublishError("Publication journal lock cannot be created.") from None
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self.fd is not None:
            os.close(self.fd)
        try:
            self.path.unlink()
        except OSError:
            pass


def publication_url(channel, message_id):
    return f"https://t.me/{channel.lstrip('@')}/{message_id}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--content-json", type=Path,
                        help="Canonical auqni-content/v1 JSON; required for publication")
    parser.add_argument("--text-file", type=Path,
                        help="Optional UTF-8 text copy; must exactly match JSON when both are supplied")
    parser.add_argument("--image", required=True, type=Path, help="PNG image")
    parser.add_argument("--channel", default=DEFAULT_CHANNEL)
    parser.add_argument("--journal", type=Path, default=Path("telegram_publications.jsonl"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Offline validation (default)")
    mode.add_argument("--publish", action="store_true", help="Explicitly enable one real API request")
    args = parser.parse_args(argv)

    try:
        if args.channel != DEFAULT_CHANNEL:
            raise PublishError("Target channel is not the configured AUQNI channel.")
        if args.publish and args.content_json is None:
            raise PublishError("Publication requires canonical --content-json.")
        if args.content_json is not None:
            caption = load_content_caption(args.content_json)
            if args.text_file is not None:
                verify_text_copy(args.text_file, caption)
        elif args.text_file is not None:
            caption = load_caption(args.text_file)
        else:
            raise PublishError("Provide --content-json or --text-file.")
        image, width, height = load_png(args.image)
        token = read_token()
        if token and token in caption:
            raise PublishError("Caption contains the credential; refusing to proceed.")
        key = idempotency_key(args.channel, caption, image)

        if not args.publish:
            print(json.dumps({
                "mode": "dry-run",
                "network_requests": 0,
                "method": "sendPhoto",
                "endpoint": "https://api.telegram.org/bot<TOKEN>/sendPhoto",
                "channel": args.channel,
                "multipart_fields": ["chat_id", "caption", "photo"],
                "parse_mode": None,
                "caption_characters": len(caption),
                "caption_sha256": sha256(caption.encode("utf-8")),
                "image_mime": "image/png",
                "image_bytes": len(image),
                "image_width": width,
                "image_height": height,
                "image_sha256": sha256(image),
                "idempotency_key": key,
                "token_available": bool(token),
                "validation": "ok",
            }, ensure_ascii=True, indent=2))
            return 0

        if not token:
            raise PublishError("TELEGRAM_BOT_TOKEN is unavailable.")
        with JournalLock(args.journal):
            previous = [r for r in journal_entries(args.journal) if r.get("idempotency_key") == key]
            if any(r.get("status") in ("sending", "published", "uncertain") for r in previous):
                raise PublishError("This publication is already sending, published, or uncertain.")
            attempt_id = uuid.uuid4().hex
            base = {"attempt_id": attempt_id, "idempotency_key": key, "channel": args.channel,
                    "method": "sendPhoto", "caption_sha256": sha256(caption.encode()),
                    "image_sha256": sha256(image), "timestamp": int(time.time())}
            append_journal(args.journal, dict(base, status="sending"))
            try:
                message_id, chat_id = publish(token, args.channel, caption, image)
            except ApiRejected:
                append_journal(args.journal, dict(base, status="failed"))
                raise
            except UncertainOutcome:
                append_journal(args.journal, dict(base, status="uncertain"))
                raise
            url = publication_url(args.channel, message_id)
            append_journal(args.journal, dict(base, status="published", message_id=message_id,
                                              chat_id=chat_id, publication_url=url))
        print("Published: " + url)
        return 0
    except PublishError as exc:
        print("Error: " + str(exc), file=sys.stderr)
        return 1
    except Exception:
        print("Error: operation failed safely.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
