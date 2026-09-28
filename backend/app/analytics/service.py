from collections import defaultdict
from datetime import timedelta
from sqlalchemy import select
from app.models import MetricSnapshot, Content, Publication, Brand, StrategyChange
from app.repositories.common import require
from app.db.session import utcnow
from app.core.errors import DomainError, ProviderError
from app.publishing.providers import MetaPublisher
from app.publishing.service import account_for
from app.audit.service import record
from app.schemas.domain import BrandConfig

METRICS = {"reach", "impressions", "views", "likes", "comments", "shares", "saves", "followers", "follower_change", "watch_seconds", "retention"}


def add_snapshot(db, brand_id, content_id, platform, metrics, actor="admin"):
    require(db, Brand, brand_id)
    if not metrics or set(metrics) - METRICS or any(not isinstance(v, (int, float)) or (v < 0 and k != "follower_change") for k, v in metrics.items()):
        raise DomainError("Metrics must be supported numeric counters; only follower_change may be negative")
    item = require(db, Content, content_id, brand_id) if content_id else None
    snapshot = MetricSnapshot(brand_id=brand_id, content_id=content_id, platform=platform, metrics=metrics,
        dimensions={"format": item.format, "pillar": item.pillar, "template": item.slides[0].get("kind", "") if item.slides else "text",
                    "hook": item.hook, "publish_time": item.published_at.isoformat() if item.published_at else None} if item else {})
    db.add(snapshot)
    record(db, "analytics.snapshot", content_id or brand_id, brand_id, actor, after={"platform": platform, "metrics": metrics})
    return snapshot


def collect(db, brand_id):
    pubs = db.scalars(select(Publication).where(Publication.brand_id == brand_id, Publication.state == "PUBLISHED", Publication.platform != "manual")).all()
    captured = 0
    for pub in pubs:
        account = account_for(db, brand_id, pub.platform)
        if not account:
            continue
        mapping = {"reach": "reach", "saved": "saves", "shares": "shares", "likes": "likes", "comments": "comments", "views": "views"} if pub.platform == "instagram" else {"post_impressions_unique": "reach", "post_impressions": "impressions"}
        values = {}
        for metric, normalized in mapping.items():
            try:
                result = MetaPublisher().request(account, "GET", f"{pub.external_id}/insights", {"metric": metric})
                for row in result.get("data", []):
                    value = row.get("total_value", {}).get("value")
                    if value is None and row.get("values"):
                        value = row["values"][-1].get("value")
                    if isinstance(value, (int, float)):
                        values[normalized] = value
            except ProviderError as exc:
                record(db, "analytics.unavailable", pub.id, brand_id, "platform", result="UNAVAILABLE", reason=exc.message, details={"metric": metric})
        if values:
            add_snapshot(db, brand_id, pub.content_id, pub.platform, values, "platform")
            captured += 1
    return {"captured": captured}


def report(db, brand_id, days=30, start=None, end=None):
    end, start = end or utcnow(), start or utcnow() - timedelta(days=days)
    rows = db.scalars(select(MetricSnapshot).where(MetricSnapshot.brand_id == brand_id, MetricSnapshot.captured_at >= start,
                                                   MetricSnapshot.captured_at <= end).order_by(MetricSnapshot.captured_at, MetricSnapshot.id)).all()
    latest, trend = {}, defaultdict(lambda: defaultdict(float))
    for row in rows:
        latest[(row.content_id, row.platform)] = row
    totals, groups, top = defaultdict(float), {"pillars": {}, "formats": {}, "templates": {}, "hours": {}}, []
    for row in latest.values():
        for key, value in row.metrics.items():
            if key not in {"followers", "retention"}:
                totals[key] += value
        if row.content_id:
            item = db.get(Content, row.content_id)
            top.append({"id": row.content_id, "topic": item.topic, "platform": row.platform, **row.metrics})
            for group, dim in (("pillars", "pillar"), ("formats", "format"), ("templates", "template"), ("hours", "publish_time")):
                key = row.dimensions.get(dim) or "Unknown"
                if group == "hours" and key != "Unknown":
                    key = key[11:13] + ":00 UTC"
                bucket = groups[group].setdefault(key, {"content": 0, "saves": 0, "shares": 0, "reach": 0})
                bucket["content"] += 1
                for metric in ("saves", "shares", "reach"):
                    bucket[metric] += row.metrics.get(metric, 0)
    # Each daily point uses one latest snapshot per content/platform, never sums repeated cumulative samples.
    daily = {}
    for row in rows:
        daily[(row.captured_at.date().isoformat(), row.content_id, row.platform)] = row
    for (date, _, _), row in daily.items():
        for metric in ("saves", "shares", "reach"):
            trend[date][metric] += row.metrics.get(metric, 0)
    followers = defaultdict(list)
    for row in rows:
        if row.content_id is None and "followers" in row.metrics:
            followers[row.platform].append(row.metrics["followers"])
    growth = {platform: {"current": values[-1], "change": values[-1] - values[0]} for platform, values in followers.items()}
    volume = len(db.scalars(select(Content.id).where(Content.brand_id == brand_id, Content.published_at >= start, Content.published_at <= end)).all())
    return {"totals": dict(totals), "groups": groups, "top": sorted(top, key=lambda x: x.get("saves", 0) + x.get("shares", 0), reverse=True)[:20],
            "trend": [{"date": k, **v} for k, v in sorted(trend.items())], "growth": growth, "volume": volume, "samples": len(top),
            "note": "Latest observed lifetime counters in the selected range; daily points are snapshots, not daily gains. Missing metrics are unavailable, not zero."}


def recommend(db, brand_id):
    brand = require(db, Brand, brand_id)
    config = BrandConfig.model_validate(brand.config)
    stats = report(db, brand_id, days=90)
    if stats["samples"] < 20:
        raise DomainError("At least 20 observed content/platform samples are required for a strategy recommendation")
    buckets = stats["groups"]["pillars"]
    weights = dict(config.strategy.get("pillar_weights", {}))
    eligible = {k: v for k, v in buckets.items() if v["content"] >= 5 and k in config.pillars}
    if len(eligible) < 2:
        raise DomainError("Need at least two pillars with five samples each")
    best = max(eligible, key=lambda k: (eligible[k]["saves"] + eligible[k]["shares"]) / max(1, eligible[k]["reach"]))
    if "pillar_weights" in config.strategy_locks or best in config.strategy_locks:
        raise DomainError("Recommended strategy value is locked by the administrator")
    weights[best] = min(1.2, weights.get(best, 1) + .05)
    proposal = StrategyChange(brand_id=brand_id, before=config.strategy, after={**config.strategy, "pillar_weights": weights},
        reason=f"{best}: strongest save/share rate among pillars with at least five samples; +0.05 weight, capped at 1.2 to preserve diversity")
    db.add(proposal)
    db.flush()
    from app.approvals.policy import permit
    if permit(db, brand, "strategy", proposal.id):
        apply_strategy(db, brand_id, proposal.id, "system")
    record(db, "strategy.propose", proposal.id, brand_id, "system", reason=proposal.reason)
    return proposal


def apply_strategy(db, brand_id, key, actor, revert=False):
    change, brand = require(db, StrategyChange, key, brand_id), require(db, Brand, brand_id)
    config = BrandConfig.model_validate(brand.config)
    if revert:
        if change.status != "APPLIED" or config.strategy != change.after:
            raise DomainError("Revert later strategy changes first", 409)
        config.strategy, change.status = change.before, "REVERTED"
    else:
        if change.status != "PROPOSED" or config.strategy != change.before:
            raise DomainError("Strategy changed since this proposal", 409)
        if "pillar_weights" in config.strategy_locks:
            raise DomainError("Pillar weights are locked")
        config.strategy, change.status = change.after, "APPLIED"
    brand.config = config.model_dump()
    record(db, "strategy.revert" if revert else "strategy.apply", key, brand_id, actor, before=change.before, after=change.after, reason=change.reason)
    return change
