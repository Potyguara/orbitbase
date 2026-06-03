from datetime import datetime

from sqlalchemy.orm import Session

from app.models import UsageRecord, User


PLAN_LIMITS = {
    "free": {"minutes": 30, "exports": 2},
    "pro": {"minutes": 500, "exports": 30},
    "premium": {"minutes": 2000, "exports": 200},
}


def current_ref_month() -> str:
    return datetime.utcnow().strftime("%Y-%m")


def get_or_create_usage_record(db: Session, user: User) -> UsageRecord:
    ref_month = current_ref_month()
    record = (
        db.query(UsageRecord)
        .filter(UsageRecord.user_id == user.id, UsageRecord.ref_month == ref_month)
        .first()
    )
    if record:
        return record

    limits = PLAN_LIMITS.get(user.plan, PLAN_LIMITS["free"])
    record = UsageRecord(
        user_id=user.id,
        ref_month=ref_month,
        included_minutes=limits["minutes"],
        included_exports=limits["exports"],
        used_minutes=0,
        used_exports=0,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def add_generated_minutes(db: Session, user: User, minutes: int) -> UsageRecord:
    record = get_or_create_usage_record(db, user)
    record.used_minutes += max(0, minutes)
    db.commit()
    db.refresh(record)
    return record


def add_export(db: Session, user: User, quantity: int = 1) -> UsageRecord:
    record = get_or_create_usage_record(db, user)
    record.used_exports += max(0, quantity)
    db.commit()
    db.refresh(record)
    return record
