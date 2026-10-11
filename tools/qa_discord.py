"""Fixed private Discord smoke consumer. Secrets enter only through inherited env."""
import http.client
import json
import os
import re
import sys

MARKER = "LiveChat XR private QA transport check (synthetic; no user chat)."
ID = r"[0-9]{15,22}"
HOOK = re.compile(r"https://discord\.com/api/webhooks/(" + ID + r")/[A-Za-z0-9_-]{40,100}")


class Failure(Exception):
    pass


def receipt():
    return dict(fixture="livechat-xr-discord-qa", matched_scope=False, posted=False,
                readback=False, deleted=False, cleanup=True, http_status=None, error_code="ok")


def request(method, url, payload=None):
    # Fixed host, no proxies or redirect following; URLs are constructed after validation.
    connection = http.client.HTTPSConnection("discord.com", timeout=5)
    try:
        connection.request(method, url[len("https://discord.com"):],
                           None if payload is None else json.dumps(payload).encode(),
                           {"Content-Type": "application/json", "User-Agent": "LiveChatXR-private-QA"})
        response = connection.getresponse()
        body = response.read(65537)
        if len(body) > 65536:
            raise Failure("response_too_large")
        return response.status, body
    finally:
        connection.close()


def run(env, transport=request):
    result = receipt()
    hook = env.get("LIVECHAT_XR_QA_DISCORD_WEBHOOK", "")
    guild = env.get("LIVECHAT_XR_QA_DISCORD_GUILD_ID", "")
    channel = env.get("LIVECHAT_XR_QA_DISCORD_CHANNEL_ID", "")
    match = HOOK.fullmatch(hook)
    if not match or not re.fullmatch(ID, guild) or not re.fullmatch(ID, channel):
        result["error_code"] = "invalid_config"
        return result

    def call(stage, method, url, expected, payload=None):
        try:
            status, body = transport(method, url, payload)
        except Exception:
            raise Failure(stage + "_transport") from None
        result["http_status"] = status
        if status != expected:
            raise Failure(stage + "_http")
        return body

    def document(stage, body):
        try:
            value = json.loads(body)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, UnicodeError):
            raise Failure(stage + "_json") from None

    def identity(message, message_id):
        return (message.get("id") == message_id and message.get("content") == MARKER
                and message.get("webhook_id") == match[1] and message.get("channel_id") == channel)

    message_url = None
    try:
        scope = document("scope", call("scope", "GET", hook, 200))
        if scope.get("guild_id") != guild or scope.get("channel_id") != channel:
            raise Failure("scope_mismatch")
        result["matched_scope"] = True
        # No retries: an ambiguous POST may already have created a message.
        result["cleanup"] = False
        body = call("post", "POST", hook + "?wait=true", 200,
                    {"content": MARKER, "allowed_mentions": {"parse": []}})
        result["posted"] = True
        message = document("post", body)
        message_id = message.get("id")
        if not isinstance(message_id, str) or not re.fullmatch(ID, message_id) or not identity(message, message_id):
            raise Failure("post_identity")
        message_url = hook + "/messages/" + message_id
        message = document("readback", call("readback", "GET", message_url, 200))
        if not identity(message, message_id):
            raise Failure("readback_identity")
        result["readback"] = True
    except Failure as error:
        result["error_code"] = str(error)  # only locally constructed static codes
    finally:
        if message_url:
            try:
                call("delete", "DELETE", message_url, 204)
                result["deleted"] = True
            except Failure as error:
                result["error_code"] = str(error)
            # Independent readback even when the DELETE response was ambiguous.
            try:
                call("cleanup", "GET", message_url, 404)
                result["cleanup"] = True
            except Failure as error:
                result["error_code"] = str(error)
    return result


def main():
    if len(sys.argv) != 1:
        result = receipt()
        result["error_code"] = "arguments_forbidden"
    else:
        try:
            result = run(os.environ)
        except Exception:
            result = receipt()
            result.update(cleanup=False, error_code="internal_error")
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result["error_code"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
