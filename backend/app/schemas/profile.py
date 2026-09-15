from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

UserRole = Literal["athlete", "coach", "admin"]
CoachVerificationStatus = Literal["unverified", "pending", "verified", "rejected"]


class AthleteSummary(BaseModel):
    position: str | None
    age_group: str | None
    is_minor: bool
    guardian_consented: bool
    visibility: Literal["public", "scouts_only"]


class CoachSummary(BaseModel):
    club_id: UUID | None
    organization_text: str | None
    focus_sport: str | None
    verification_status: CoachVerificationStatus


class Me(BaseModel):
    id: UUID
    email: str | None
    phone: str | None
    role: UserRole | None
    full_name: str | None
    avatar_key: str | None
    region: str | None
    city: str | None
    sport: str | None
    onboarded_at: datetime | None
    athlete: AthleteSummary | None = None
    coach: CoachSummary | None = None
