import hashlib
import hmac
import json
from fastapi import APIRouter, Request, Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from app.core.config import settings
from app.core.errors import DomainError
from app.db.session import get_db
from app.models import PlatformAccount
from app.schemas.domain import InteractionInput
from app.community.service import ingest
from app.content.director import enqueue

router = APIRouter(prefix="/api/webhooks/meta", tags=["webhooks"])


@router.get("")
def verify(request: Request):
    expected = settings().meta_verify_token
    if not expected or not hmac.compare_digest(request.query_params.get("hub.verify_token", ""), expected) or request.query_params.get("hub.mode") != "subscribe":
        raise DomainError("Webhook verification failed", 403)
    return PlainTextResponse(request.query_params.get("hub.challenge", ""))


@router.post("")
async def receive(request: Request, db=Depends(get_db)):
    body = await request.body()
    if len(body) > 1_000_000:
        raise DomainError("Webhook payload too large", 413)
    secret = settings().meta_app_secret
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not secret or not hmac.compare_digest(signature, request.headers.get("x-hub-signature-256", "")):
        raise DomainError("Invalid webhook signature", 403)
    try:
        event = json.loads(body)
    except ValueError:
        raise DomainError("Invalid JSON payload")
    received = 0
    for entry in event.get("entry", []):
        accounts = db.scalars(select(PlatformAccount).where(PlatformAccount.account_id == str(entry.get("id")), PlatformAccount.enabled.is_(True))).all()
        for account in accounts:
            values = []
            for change in entry.get("changes", []):
                value = change.get("value", {})
                if change.get("field") == "comments":
                    values.append({"external_id": str(value.get("id", "")), "body": value.get("text", ""), "author": str(value.get("from", {}).get("id", "")), "thread_id": str(value.get("media", {}).get("id", ""))})
                elif change.get("field") == "feed" and value.get("item") == "comment" and value.get("verb") == "add":
                    values.append({"external_id": str(value.get("comment_id", "")), "body": value.get("message", ""), "author": str(value.get("from", {}).get("id", "")), "thread_id": str(value.get("post_id", ""))})
            for message in entry.get("messaging", []):
                data = message.get("message", {})
                if not data.get("is_echo") and data.get("text"):
                    values.append({"external_id": str(data.get("mid", "")), "body": data["text"], "kind": "dm", "author": str(message.get("sender", {}).get("id", "")), "thread_id": str(message.get("sender", {}).get("id", ""))})
            for value in values:
                if not value["external_id"] or not value["body"] or value["author"] == account.account_id:
                    continue
                item = ingest(db, account.brand_id, InteractionInput(platform=account.platform, **value), "platform")
                if item.status == "NEW":
                    from app.models import Job
                    key = f"classify:{item.id}"
                    if not db.scalar(select(Job.id).where(Job.key == key)):
                        enqueue(db, account.brand_id, "classify", item.id, key=key)
                received += 1
    db.commit()
    return {"received": received}
