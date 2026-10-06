"""Standalone VK publisher. Default mode is offline dry-run; standard library only."""

import argparse
import json
import os
from pathlib import Path
import sys
import urllib.parse
import urllib.request
import uuid

GROUP_ID = 241580761
API_VERSION = "5.199"
MAX_IMAGE_BYTES = 10 * 1024 * 1024


class PublishError(Exception):
    """Only locally authored, secret-free messages may be surfaced."""


def read_token():
    token = os.environ.get("VK_GROUP_TOKEN", "").strip()
    if not token and os.name == "nt":
        # Read the Windows user environment if the terminal has an older snapshot.
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value, _ = winreg.QueryValueEx(key, "VK_GROUP_TOKEN")
                token = value.strip() if isinstance(value, str) else ""
        except OSError:
            pass
    return token


def load_text(path):
    try:
        text = path.read_text(encoding="utf-8-sig")
        if path.suffix.lower() == ".json":
            text = json.loads(text)["platforms"]["vk"]["content"]
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        raise PublishError("Cannot read UTF-8 text or platforms.vk.content from JSON.") from None
    if not isinstance(text, str) or not text.strip():
        raise PublishError("Post text is empty or is not a string.")
    text = text.strip()
    if len(text) > 16384:
        raise PublishError("Post text exceeds 16384 characters.")
    return text


def load_image(path):
    if path is None:
        return None
    try:
        if path.stat().st_size > MAX_IMAGE_BYTES:
            raise PublishError("Image exceeds the local 10 MiB limit.")
        data = path.read_bytes()
    except OSError:
        raise PublishError("Cannot read image file.") from None
    suffix = path.suffix.lower()
    if suffix == ".png" and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return data, "image/png", "image.png"
    if suffix in (".jpg", ".jpeg") and data.startswith(b"\xff\xd8\xff"):
        return data, "image/jpeg", "image.jpg"
    raise PublishError("Expected a PNG/JPG file with a matching file signature.")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a credential-bearing request on a redirect.
        return None


def request_json(url, data, content_type):
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": content_type}, method="POST"
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
            result = json.load(response)
    except Exception:
        # urllib errors and raw VK responses can contain credentials: never echo them.
        raise PublishError(
            "Network/HTTP/JSON failure. No automatic retry; check VK before repeating a publish."
        ) from None
    if not isinstance(result, dict):
        raise PublishError("Unexpected API response structure.")
    return result


class VKClient:
    def __init__(self, token):
        self._token = token

    def call(self, method, **params):
        params.update(access_token=self._token, v=API_VERSION)
        body = urllib.parse.urlencode(params).encode("utf-8")
        result = request_json(
            "https://api.vk.com/method/" + method,
            body,
            "application/x-www-form-urlencoded",
        )
        if "error" in result:
            error = result["error"]
            code = error.get("error_code") if isinstance(error, dict) else None
            safe_code = str(code) if type(code) is int else "unknown"
            hint = " This method does not accept a community token." if code == 27 else ""
            raise PublishError("VK API error " + safe_code + "." + hint)
        if "response" not in result:
            raise PublishError("VK API response is missing.")
        return result["response"]

    def upload(self, url, image):
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or not any(host == domain or host.endswith("." + domain)
                           for domain in ("vk.com", "vk.ru", "vkuserphoto.ru", "userapi.com"))):
            raise PublishError("Upload URL is not a trusted VK HTTPS endpoint.")
        data, mime, filename = image
        boundary = "auqni" + uuid.uuid4().hex
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("ascii") + data + f"\r\n--{boundary}--\r\n".encode("ascii")
        return request_json(url, body, "multipart/form-data; boundary=" + boundary)


def publish(client, text, image):
    params = {"owner_id": -GROUP_ID, "from_group": 1, "message": text}
    if image is not None:
        server = client.call("photos.getWallUploadServer", group_id=GROUP_ID)
        uploaded = client.upload(server["upload_url"], image)
        if not uploaded.get("photo") or uploaded["photo"] == "[]":
            raise PublishError("VK did not accept the image; wall.post was not called.")
        saved = client.call(
            "photos.saveWallPhoto", group_id=GROUP_ID,
            server=uploaded["server"], photo=uploaded["photo"], hash=uploaded["hash"],
        )
        photo = saved[0]
        params["attachments"] = f"photo{int(photo['owner_id'])}_{int(photo['id'])}"
        if photo.get("access_key"):
            params["attachments"] += "_" + photo["access_key"]
    result = client.call("wall.post", **params)
    return int(result["post_id"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text-file", required=True, type=Path,
                        help="UTF-8 plain text/Markdown or existing AUQNI content JSON")
    parser.add_argument("--image", type=Path, help="Optional PNG/JPG")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Offline validation (default)")
    mode.add_argument("--publish", action="store_true", help="Explicitly enable real publishing")
    args = parser.parse_args(argv)
    try:
        text = load_text(args.text_file)
        image = load_image(args.image)
        token = read_token()
        # Do not echo post text, file paths, token fragments, raw responses or request bodies.
        if token and token in text:
            raise PublishError("Post text contains the credential; refusing to proceed.")
        if not args.publish:
            print(json.dumps({
                "mode": "dry-run", "network_requests": 0,
                "group_id": GROUP_ID, "owner_id": -GROUP_ID, "api_version": API_VERSION,
                "text_characters": len(text),
                "image_type": image[1] if image else None,
                "image_bytes": len(image[0]) if image else 0,
                "token_available": bool(token),
                "token_validity_and_permissions": "not_checked",
                "editorial_placeholders": "[уточнить]" in text.lower(),
                "validation": "ok",
            }, ensure_ascii=True, indent=2))
            return 0
        if not token:
            raise PublishError("VK_GROUP_TOKEN is unavailable in the process/Windows user environment.")
        post_id = publish(VKClient(token), text, image)
        print(f"Published: https://vk.com/wall{-GROUP_ID}_{post_id}")
        return 0
    except PublishError as exc:
        print("Error: " + str(exc), file=sys.stderr)
        return 1
    except Exception:
        print("Error: operation failed. No automatic retry; check VK before repeating a publish.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
