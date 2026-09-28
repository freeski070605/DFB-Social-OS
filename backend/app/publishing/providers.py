import json
from io import BytesIO
from typing import Protocol
from zipfile import ZipFile, ZIP_DEFLATED
import httpx
from app.core.config import settings
from app.core.errors import ProviderError
from app.security.secrets import decrypt
from app.storage.local import LocalStorage
from app.repositories.common import serialize
from app.publishing.public_media import public_media_provider


class SocialPublisher(Protocol):
    def publish(self, content, account, state: dict, checkpoint) -> str: ...


class ManualExportPublisher:
    def publish(self, content, account=None, state=None, checkpoint=None):
        storage, output = LocalStorage(), BytesIO()
        with ZipFile(output, "w", ZIP_DEFLATED) as archive:
            archive.writestr("content.json", json.dumps(serialize(content), default=str, indent=2, ensure_ascii=False))
            archive.writestr("caption.txt", caption(content))
            for i, asset in enumerate(content.assets):
                archive.writestr(f"slide-{i + 1:02d}.png", storage.read(asset["key"]))
        key = f"exports/{content.brand_id}/{content.id}-r{content.revision}.zip"
        storage.write(key, output.getvalue())
        return key


def caption(content):
    return "\n\n".join(x for x in (content.caption, content.cta, " ".join("#" + h.lstrip("#") for h in content.hashtags)) if x)


class MetaPublisher:
    def request(self, account, method, path, data=None, *, outward=False):
        token = decrypt(account.token_encrypted)
        url = f"https://graph.facebook.com/{settings().meta_api_version}/{path}"
        try:
            with httpx.Client(timeout=30, trust_env=False) as client:
                response = client.request(method, url, headers={"Authorization": f"Bearer {token}"},
                    params=data if method == "GET" else None, data=data if method != "GET" else None)
        except httpx.HTTPError:
            raise ProviderError("Meta connection failed", transient=not outward, uncertain=outward)
        try:
            result = response.json()
        except ValueError:
            raise ProviderError("Meta returned an unreadable response", uncertain=outward)
        if response.is_error or "error" in result:
            error = result.get("error", {})
            code = error.get("code", response.status_code)
            # API errors are not assumed to prove absence of an outward side effect on server failures.
            raise ProviderError(f"Meta API error {code} (subcode {error.get('error_subcode', 0)}). Check account permissions and API capabilities.",
                                transient=code in {4, 17, 32, 613} or response.status_code == 429,
                                uncertain=outward and response.status_code >= 500)
        return result

    def health(self, account):
        return self.request(account, "GET", account.account_id, {"fields": "id,name"})

    def publish(self, content, account, state, checkpoint):
        if not account:
            raise ProviderError("No enabled Meta account. Configure authorization or export manually.")
        if content.format in {"reel_script", "short_video_script", "story"}:
            raise ProviderError("This adapter publishes feed images, carousels and Facebook text. Export scripts and Stories manually.")
        if account.platform == "instagram":
            return self._instagram(content, account, state, checkpoint)
        return self._facebook(content, account, state, checkpoint)

    def _instagram(self, content, account, state, checkpoint):
        if not content.assets:
            raise ProviderError("Instagram requires rendered images")
        if len(content.assets) > 10:
            raise ProviderError("This integration supports up to 10 carousel images")
        if len(caption(content)) > 2200:
            raise ProviderError("Instagram caption, CTA and hashtags exceed 2200 characters")
        children = list(state.get("children", []))
        if not state.get("container"):
            for asset in content.assets[len(children):]:
                data = {"image_url": public_media_provider().prepare(content, asset)}
                if len(content.assets) > 1:
                    data["is_carousel_item"] = "true"
                else:
                    data["caption"] = caption(content)
                result = self.request(account, "POST", f"{account.account_id}/media", data)
                children.append(result["id"])
                state = {**state, "children": children}
                checkpoint(state)
            if len(children) > 1:
                for child in children:
                    self._ready(account, child)
                result = self.request(account, "POST", f"{account.account_id}/media", {
                    "media_type": "CAROUSEL", "children": ",".join(children), "caption": caption(content)})
                container = result["id"]
            else:
                container = children[0]
            state = {**state, "container": container}
            checkpoint(state)
        self._ready(account, state["container"])
        checkpoint({**state, "outward_started": True})
        result = self.request(account, "POST", f"{account.account_id}/media_publish", {"creation_id": state["container"]}, outward=True)
        return result["id"]

    def _ready(self, account, container):
        result = self.request(account, "GET", container, {"fields": "status_code,status"})
        code = result.get("status_code")
        if code == "IN_PROGRESS":
            raise ProviderError("Meta is processing media; retry after a delay", transient=True)
        if code != "FINISHED":
            raise ProviderError("Meta media container is not publishable; inspect container status")

    def _facebook(self, content, account, state, checkpoint):
        media = list(state.get("media", []))
        for asset in content.assets[len(media):]:
            result = self.request(account, "POST", f"{account.account_id}/photos", {
                "url": public_media_provider().prepare(content, asset), "published": "false"})
            media.append(result["id"])
            state = {**state, "media": media}
            checkpoint(state)
        data = {"message": caption(content) or content.body}
        if media:
            data["attached_media"] = json.dumps([{"media_fbid": key} for key in media])
        checkpoint({**state, "outward_started": True})
        result = self.request(account, "POST", f"{account.account_id}/feed", data, outward=True)
        return result["id"]

    def reply(self, account, item, body):
        if item.kind == "dm":
            return self.request(account, "POST", f"{account.account_id}/messages", {
                "recipient": json.dumps({"id": item.author}), "message": json.dumps({"text": body})}, outward=True).get("message_id", "")
        path = f"{item.external_id}/replies" if account.platform == "instagram" else f"{item.external_id}/comments"
        return self.request(account, "POST", path, {"message": body}, outward=True)["id"]

    def moderate(self, account, item, action):
        if action == "delete":
            return self.request(account, "DELETE", item.external_id, outward=True)
        return self.request(account, "POST", item.external_id,
            {"hide": "true"} if account.platform == "instagram" else {"is_hidden": "true"}, outward=True)
