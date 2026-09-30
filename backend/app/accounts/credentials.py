"""Resolve a credential stored on an account or its same-brand Meta Page."""
from sqlalchemy.orm import object_session

from app.core.errors import DomainError
from app.models import PlatformAccount


def encrypted_credential(account):
    if account.token_encrypted:
        return account.token_encrypted
    page_id = (account.config or {}).get("credential_account_id")
    db = object_session(account)
    page = db.get(PlatformAccount, page_id) if db and isinstance(page_id, int) else None
    if (page is not None and page.brand_id == account.brand_id and page.platform == "facebook"
            and page.account_id == (account.config or {}).get("page_id") and page.token_encrypted):
        return page.token_encrypted
    raise DomainError("The linked Meta Page credential is unavailable. Check the Page connection.", 409)


def has_credential(account):
    try:
        encrypted_credential(account)
        return True
    except DomainError:
        return False
