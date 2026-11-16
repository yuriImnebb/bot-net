import hashlib
import hmac
import json

PREFIX = "!devctl "


def canonical_bytes(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign_payload(payload: dict, secret: str) -> dict:
    payload = dict(payload)
    sig = hmac.new(secret.encode("utf-8"), canonical_bytes(payload), hashlib.sha256).hexdigest()
    payload["sig"] = sig
    return payload


def verify_payload(payload: dict, secret: str) -> bool:
    payload = dict(payload)
    sig = payload.pop("sig", None)
    if not isinstance(sig, str):
        return False
    expected = hmac.new(secret.encode("utf-8"), canonical_bytes(payload), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


def make_message(payload: dict, secret: str) -> str:
    return PREFIX + json.dumps(sign_payload(payload, secret), ensure_ascii=False)


def parse_message(content: str):
    if not content or not content.startswith(PREFIX):
        return None
    try:
        data = json.loads(content[len(PREFIX):])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None
