from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr
import asyncpg
from app.db import get_db
import secrets
import hashlib

router = APIRouter(prefix="/api/auth/email", tags=["auth"])


class UserRegister(BaseModel):
    email: EmailStr
    password: str
    username: str


class UserLogin(BaseModel):
    email: EmailStr
    password: str


def _hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    if salt is None:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        100_000,
    )
    return salt, dk.hex()


def get_password_hash(password: str) -> str:
    salt, h = _hash_password(password)
    return f"{salt}:{h}"


def verify_password(plain_password: str, stored: str) -> bool:
    try:
        salt, h = stored.split(":", 1)
    except ValueError:
        return False
    _, new_h = _hash_password(plain_password, salt)
    return secrets.compare_digest(h, new_h)


@router.post("/register")
async def register(user: UserRegister, request: Request, db: asyncpg.Connection = Depends(get_db)):
    existing = await db.fetchval("SELECT 1 FROM users WHERE email = $1", user.email)
    if existing:
        raise HTTPException(400, "Email already registered")

    user_id = secrets.token_hex(8)
    pwd_hash = get_password_hash(user.password)

    await db.execute(
        """
        INSERT INTO users (id, email, password_hash, account_type)
        VALUES ($1, $2, $3, 'user')
        """,
        user_id,
        user.email,
        pwd_hash,
    )

    await db.execute(
        """
        INSERT INTO user_profiles (user_id, username, global_name) 
        VALUES ($1, $2, $2)
        ON CONFLICT (user_id) DO NOTHING
        """,
        user_id,
        user.username,
    )

    return {"success": True, "message": "Registered successfully. Please login."}


@router.post("/login")
async def login(user: UserLogin, request: Request, db: asyncpg.Connection = Depends(get_db)):
    row = await db.fetchrow(
        "SELECT id, password_hash, account_type FROM users WHERE email = $1",
        user.email,
    )

    if not row or not row["password_hash"] or not verify_password(user.password, row["password_hash"]):
        raise HTTPException(400, "Invalid email or password")

    request.session["user_id"] = row["id"]
    request.session["user"] = {"id": row["id"], "account_type": row["account_type"]}

    return {"success": True, "user_id": row["id"]}
