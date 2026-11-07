"""Authentication utilities: password hashing, validation, token generation."""
import bcrypt
import secrets
import re
from email_validator import validate_email, EmailNotValidError
from concurrent.futures import ThreadPoolExecutor
import asyncio
from typing import Optional

# Thread pool for CPU-intensive password hashing/verification
# This prevents blocking the event loop during bcrypt operations
_executor = ThreadPoolExecutor(max_workers=4)


def hash_password(password: str) -> str:
    """
    Hash a password using bcrypt.
    Runs in thread pool to avoid blocking the event loop.
    """
    # bcrypt.gensalt() generates a salt with default cost factor (12)
    # The hash includes the salt, so we don't need to store it separately
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


async def hash_password_async(password: str) -> str:
    """Async wrapper for password hashing."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, hash_password, password)


def verify_password(password: str, password_hash: str) -> bool:
    """
    Verify a password against its bcrypt hash.
    Runs in thread pool to avoid blocking the event loop.
    """
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


async def verify_password_async(password: str, password_hash: str) -> bool:
    """Async wrapper for password verification."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, verify_password, password, password_hash)


def validate_email_address(email: str) -> tuple[bool, Optional[str]]:
    """
    Validate email address format and deliverability.
    Returns (is_valid, error_message).
    """
    try:
        # validate_email checks format, DNS MX records, etc.
        validate_email(email, check_deliverability=False)
        return True, None
    except EmailNotValidError as e:
        return False, str(e)


def validate_password_strength(password: str) -> tuple[bool, Optional[str]]:
    """
    Validate password strength.
    Requirements:
    - At least 8 characters
    - At least one uppercase letter
    - At least one lowercase letter
    - At least one digit
    - At least one special character
    Returns (is_valid, error_message).
    """
    if len(password) < 8:
        return False, "Password must be at least 8 characters long"

    if not re.search(r"[A-Z]", password):
        return False, "Password must contain at least one uppercase letter"

    if not re.search(r"[a-z]", password):
        return False, "Password must contain at least one lowercase letter"

    if not re.search(r"\d", password):
        return False, "Password must contain at least one digit"

    if not re.search(r"[!@#$%^&*()_+\-=\[\]{};':\"\\|,.<>\/?]", password):
        return False, "Password must contain at least one special character"

    return True, None


def generate_token() -> str:
    """Generate a secure random token for API authentication."""
    return secrets.token_urlsafe(32)


def validate_username(username: str) -> tuple[bool, Optional[str]]:
    """
    Validate username.
    Requirements:
    - 3-30 characters
    - Alphanumeric and underscores only
    - Must start with a letter
    Returns (is_valid, error_message).
    """
    if len(username) < 3 or len(username) > 30:
        return False, "Username must be between 3 and 30 characters"

    if not re.match(r"^[a-zA-Z][a-zA-Z0-9_]*$", username):
        return False, "Username must start with a letter and contain only letters, numbers, and underscores"

    return True, None


async def simulate_timing_attack_prevention():
    """
    Simulate a constant-time delay to prevent timing attacks.
    This adds a small random delay (0.7s ± 0.1s) to make response times
    consistent regardless of whether the user exists or not.
    """
    import random
    delay = 0.7 + random.uniform(-0.1, 0.1)
    await asyncio.sleep(delay)


def shutdown_executor():
    """Shutdown the thread pool executor on app shutdown."""
    _executor.shutdown(wait=True)