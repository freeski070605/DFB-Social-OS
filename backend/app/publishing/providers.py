import json
from io import BytesIO
from typing import Protocol
from zipfile import ZipFile, ZIP_DEFLATED
import httpx
from app.core.config import settings
from app.core.errors import ProviderError
from app.security.secrets import decrypt
from app.accounts.credentials import encrypted_credential
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


def facebook_publication_plan(content, account):
    """Describe the Page-native Graph requests bound to one rendered revision."""
    page_id = account.account_id
    count = len(content.assets)
    mode = "TEXT" if count == 0 else "SINGLE_IMAGE" if count == 1 else "MULTI_IMAGE"
    steps = ([{"method": "POST", "path": f"{page_id}/feed", "purpose": "publish_text"}] if mode == "TEXT" else
             [{"method": "POST", "path": f"{page_id}/photos", "purpose": "publish_single_photo"}]
             if mode == "SINGLE_IMAGE" else
             [{"method": "POST", "path": f"{page_id}/photos", "purpose": "upload_unpublished_photo"}
              for _ in content.assets] +
             [{"method": "POST", "path": f"{page_id}/feed", "purpose": "publish_attached_photos"}])
    return {"provider": "META_GRAPH", "platform": "facebook", "page_id": page_id,
            "mode": mode, "message": caption(content) or content.body,
            "media_keys": [asset["key"] for asset in content.assets], "graph_steps": steps}


class MetaPublisher:
    def _media_url(self, content, asset, state, checkpoint, platform):
        provider = public_media_provider()
        key, url = provider.prepare_object(content, asset, platform)
        state.setdefault("media_objects", []).append(key)
        checkpoint(state)
        return url

    def request(self, account, method, path, data=None, *, outward=False):
        token = decrypt(encrypted_credential(account))
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
        if not isinstance(result, dict):
            raise ProviderError("Meta returned an invalid response", uncertain=outward)
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
                data = {"image_url": self._media_url(content, asset, state, checkpoint, "instagram")}
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
        plan = facebook_publication_plan(content, account)
        if plan["mode"] == "TEXT":
            checkpoint({**state, "outward_started": True})
            result = self.request(account, "POST", f"{account.account_id}/feed",
                                  {"message": plan["message"]}, outward=True)
            return self._facebook_post_id(result)
        if plan["mode"] == "SINGLE_IMAGE":
            url = self._media_url(content, content.assets[0], state, checkpoint, "facebook")
            checkpoint({**state, "outward_started": True})
            result = self.request(account, "POST", f"{account.account_id}/photos",
                                  {"url": url, "caption": plan["message"], "published": "true"}, outward=True)
            post_id = result.get("post_id") if isinstance(result, dict) else None
            if isinstance(post_id, str) and post_id:
                return post_id
            photo_id = result.get("id") if isinstance(result, dict) else None
            if not isinstance(photo_id, str) or not photo_id:
                raise ProviderError("Facebook photo creation returned no publication ID; reconcile the Page", uncertain=True)
            try:
                photo = self.request(account, "GET", photo_id, {"fields": "post_id"})
            except ProviderError:
                raise ProviderError("Facebook photo was created but its post ID could not be read; reconcile the Page",
                                    uncertain=True) from None
            post_id = photo.get("post_id") if isinstance(photo, dict) else None
            if not isinstance(post_id, str) or not post_id:
                raise ProviderError("Facebook photo post ID is unavailable; reconcile the Page", uncertain=True)
            return post_id
        media = list(state.get("media", []))
        for asset in content.assets[len(media):]:
            result = self.request(account, "POST", f"{account.account_id}/photos", {
                "url": self._media_url(content, asset, state, checkpoint, "facebook"), "published": "false"},
                outward=True)
            photo_id = result.get("id") if isinstance(result, dict) else None
            if not isinstance(photo_id, str) or not photo_id:
                raise ProviderError("Facebook photo upload returned no ID; reconcile before retry", uncertain=True)
            media.append(photo_id)
            state = {**state, "media": media}
            checkpoint(state)
        data = {"message": plan["message"]}
        data.update({f"attached_media[{index}]": json.dumps({"media_fbid": photo_id})
                     for index, photo_id in enumerate(media)})
        checkpoint({**state, "outward_started": True})
        result = self.request(account, "POST", f"{account.account_id}/feed", data, outward=True)
        return self._facebook_post_id(result)

    @staticmethod
    def _facebook_post_id(result):
        post_id = result.get("id") if isinstance(result, dict) else None
        if not isinstance(post_id, str) or not post_id:
            raise ProviderError("Facebook feed response has no post ID; reconcile the Page", uncertain=True)
        return post_id

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
