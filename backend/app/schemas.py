from datetime import date

from pydantic import BaseModel, EmailStr, Field


# ---- Auth ----
class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    role: str = Field(default="member", pattern="^(admin|member)$")


class UserOut(BaseModel):
    id: str
    email: EmailStr
    role: str
    is_active: bool

    class Config:
        from_attributes = True


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ---- Periods ----
class PeriodStatusOut(BaseModel):
    id: str
    property_id: str
    month: date
    status: str

    class Config:
        from_attributes = True


# ---- P&L (computed, never stored) ----
class MonthlyPnL(BaseModel):
    month: date
    gross_rent: float
    operating_expenses: float
    noi: float
    capex: float
    debt_service: float
    other_below_line: float
    below_noi: float
    cash_flow: float
