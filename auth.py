"""Authentication utilities for admin users."""

import os
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from sqlalchemy.orm import Session

from models import AdminUser

SECRET_KEY = os.environ.get("SECRET_KEY", secrets.token_hex(32))
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 24


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000)
    return f"{salt}${hashed.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    salt, stored_hash = password_hash.split("$")
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000)
    return hashed.hex() == stored_hash


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    to_encode["exp"] = expire
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None


def authenticate_admin(db: Session, username: str, password: str) -> Optional[AdminUser]:
    admin = db.query(AdminUser).filter(AdminUser.username == username).first()
    if admin and verify_password(password, admin.password_hash):
        return admin
    return None


def create_admin_user(db: Session, username: str, password: str) -> AdminUser:
    admin = AdminUser(
        username=username,
        password_hash=hash_password(password),
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)
    return admin


def update_admin_password(db: Session, admin: AdminUser, new_password: str) -> None:
    """Update an existing admin's password."""
    admin.password_hash = hash_password(new_password)
    db.commit()
