from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db
from app.models import User
from app.schemas import UsageRead, UserRead
from app.services.billing import get_or_create_usage_record

router = APIRouter(prefix="/users", tags=["Users"])


@router.get("/me", response_model=UserRead)
def read_current_user(current_user: User = Depends(get_current_user)):
    return current_user


@router.get("/me/usage", response_model=UsageRead)
def read_usage(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    usage = get_or_create_usage_record(db, current_user)
    return usage
