"""Authentication endpoints."""

import asyncio
import logging
import re
import secrets
import time
import urllib.parse
from contextlib import asynccontextmanager

from fastapi import (
    APIRouter,
    Depends,
    Form,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from starlette.responses import JSONResponse, RedirectResponse

from app.gateway.auth import (
    UserResponse,
    decode_token,
)
from app.gateway.auth.config import get_auth_config
from app.gateway.auth.errors import AuthErrorCode, AuthErrorResponse, TokenError
from app.gateway.auth.identity import DuplicateUserIdentity
from app.gateway.auth.oidc import OIDCError, OIDCService
from app.gateway.auth.oidc_state import (
    OIDCStatePayload,
    compute_code_challenge,
    delete_state_cookie,
    generate_code_verifier,
    generate_nonce,
    generate_oidc_state,
    get_state_cookie,
    set_state_cookie,
)
from app.gateway.auth.proxy_identity import resolve_rate_limit_client_ip
from app.gateway.auth.rate_limit import (
    AUTH_RATE_LIMIT_WINDOW,
    AuthenticationRateLimitAction,
    AuthenticationRateLimitAdmission,
    AuthenticationRateLimitRepository,
)
from app.gateway.auth.session_cookie import (
    ACCESS_TOKEN_COOKIE_NAME,
    SESSION_PERSISTENCE_COOKIE_NAME,
    set_session_cookie,
)
from app.gateway.auth.session_cookie_state import (
    SKIP_AUTH_CSRF_COOKIE_STATE_ATTR,
)
from app.gateway.auth.sessions import (
    AuthSessionUnavailable,
    issue_access_session,
    revoke_access_session,
    revoke_all_access_sessions,
)
from app.gateway.auth.user_provisioning import get_or_provision_oidc_user
from app.gateway.auth.username import UsernameInvalid, parse_username
from app.gateway.csrf_middleware import (
    CSRF_COOKIE_NAME,
    _request_origin,
    auth_csrf_cookie_settings,
    generate_csrf_token,
    is_secure_request,
)
from app.gateway.deps import (
    get_current_user_from_request,
    get_local_provider,
    get_project_quota_enforcer,
)
from app.projects.errors import ProjectBootstrapFailed, ProjectDatabaseUnavailable
from app.system_runtime_settings import (
    AuthPolicyValue,
    RuntimePolicySection,
)
from deerflow.config import get_app_config
from deerflow.config.auth_config import OIDCProviderConfig
from deerflow.persistence.engine import get_engine, get_session_factory

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
_INITIALIZE_ADMIN_LOCK_KEY = 0x0DEE_12F1_494E_4954


@asynccontextmanager
async def _initialize_admin_lock():
    """Serialize first-admin creation across gateway processes.

    The short-lived NullPool engine gives this session-level lock a dedicated
    physical PostgreSQL connection. Explicit unlock is the normal path; close
    and dispose are the fail-safe, so a failed unlock can never return a
    lock-bearing connection to the runtime pool.
    """
    runtime_engine = get_engine()
    if runtime_engine is None:
        raise ProjectDatabaseUnavailable()
    lock_engine = create_async_engine(
        runtime_engine.url,
        poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
    )
    primary_error: BaseException | None = None
    flow_primary: BaseException | None = None
    try:
        try:
            async with lock_engine.connect() as connection:
                idle_session_timeout = await connection.scalar(text("SELECT current_setting('idle_session_timeout', true)"))
                if idle_session_timeout is not None:
                    await connection.execute(text("SET idle_session_timeout = 0"))
                await connection.execute(text("SET statement_timeout = 0"))
                await connection.execute(
                    text("SELECT pg_advisory_lock(:lock_key)"),
                    {"lock_key": _INITIALIZE_ADMIN_LOCK_KEY},
                )
                try:
                    yield
                except BaseException as exc:
                    flow_primary = exc
                    raise
                finally:
                    unlock_failure: BaseException | None = None
                    try:
                        unlocked = await connection.scalar(
                            text("SELECT pg_advisory_unlock(:lock_key)"),
                            {"lock_key": _INITIALIZE_ADMIN_LOCK_KEY},
                        )
                        if unlocked is not True:
                            unlock_failure = ProjectDatabaseUnavailable()
                    except asyncio.CancelledError as exc:
                        unlock_failure = exc
                    except DBAPIError:
                        unlock_failure = ProjectDatabaseUnavailable()
                    except Exception as exc:  # noqa: BLE001 - programming errors remain visible
                        unlock_failure = exc
                    if unlock_failure is not None:
                        try:
                            await connection.invalidate()
                        except Exception:  # noqa: BLE001 - physical close remains the fail-safe
                            pass
                        if flow_primary is None:
                            flow_primary = unlock_failure
                            raise unlock_failure
        except DBAPIError as exc:
            if flow_primary is not None and exc is not flow_primary:
                raise flow_primary
            raise ProjectDatabaseUnavailable() from None
        except BaseException as exc:
            if flow_primary is not None and exc is not flow_primary:
                raise flow_primary
            raise
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            await lock_engine.dispose()
        except Exception as exc:  # noqa: BLE001 - preserve the request's primary failure
            if primary_error is None:
                if isinstance(exc, DBAPIError):
                    raise ProjectDatabaseUnavailable() from None
                raise
            logger.warning("Failed to dispose initialize lock engine after request failure")


# ── Request/Response Models ──────────────────────────────────────────────


class LoginResponse(BaseModel):
    """Response model for login — token only lives in HttpOnly cookie."""

    expires_in: int  # seconds
    needs_setup: bool = False


# Top common-password blocklist. Drawn from the public SecLists "10k worst
# passwords" set, lowercased + length>=8 only (shorter ones already fail
# the min_length check). Kept tight on purpose: this is the **lower bound**
# defense, not a full HIBP / passlib check, and runs in-process per request.
_COMMON_PASSWORDS: frozenset[str] = frozenset(
    {
        "password",
        "password1",
        "password12",
        "password123",
        "password1234",
        "12345678",
        "123456789",
        "1234567890",
        "qwerty12",
        "qwertyui",
        "qwerty123",
        "abc12345",
        "abcd1234",
        "iloveyou",
        "letmein1",
        "welcome1",
        "welcome123",
        "admin123",
        "administrator",
        "passw0rd",
        "p@ssw0rd",
        "monkey12",
        "trustno1",
        "sunshine",
        "princess",
        "football",
        "baseball",
        "superman",
        "batman123",
        "starwars",
        "dragon123",
        "master123",
        "shadow12",
        "michael1",
        "jennifer",
        "computer",
    }
)


def _password_is_common(password: str) -> bool:
    """Case-insensitive blocklist check.

    Lowercases the input so trivial mutations like ``Password`` /
    ``PASSWORD`` are also rejected. Does not normalize digit substitutions
    (``p@ssw0rd`` is included as a literal entry instead) — keeping the
    rule cheap and predictable.
    """
    return password.lower() in _COMMON_PASSWORDS


def _validate_strong_password(value: str) -> str:
    """Pydantic field-validator body shared by Register + ChangePassword.

    Constraint = function, not type-level mixin. The two request models
    have no "is-a" relationship; they only share the password-strength
    rule. Lifting it into a free function lets each model bind it via
    ``@field_validator(field_name)`` without inheritance gymnastics.
    """
    if _password_is_common(value):
        raise ValueError("Password is too common; choose a stronger password.")
    return value


def _validate_username(value: str) -> str:
    try:
        return parse_username(value)
    except UsernameInvalid as exc:
        raise ValueError(str(exc)) from exc


def _user_response(user) -> UserResponse:
    return UserResponse(
        id=str(user.id),
        email=user.email,
        username=user.username,
        system_role=user.system_role,
        needs_setup=user.needs_setup,
        oauth_provider=user.oauth_provider,
    )


def _duplicate_identity_http_error(exc: DuplicateUserIdentity) -> HTTPException:
    if exc.field == "username":
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=AuthErrorResponse(code=AuthErrorCode.USERNAME_ALREADY_EXISTS, message="Username already registered").model_dump(),
        )
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=AuthErrorResponse(code=AuthErrorCode.EMAIL_ALREADY_EXISTS, message="Email already registered").model_dump(),
    )


class RegisterRequest(BaseModel):
    """Request model for user registration."""

    email: EmailStr
    username: str = Field(..., min_length=3, max_length=32)
    password: str = Field(..., min_length=8)
    remember_me: bool = True

    _strong_password = field_validator("password")(classmethod(lambda cls, v: _validate_strong_password(v)))
    _valid_username = field_validator("username")(classmethod(lambda cls, v: _validate_username(v)))


class ChangePasswordRequest(BaseModel):
    """Request model for password change (also handles setup flow)."""

    current_password: str
    new_password: str = Field(..., min_length=8)
    new_email: EmailStr | None = None
    remember_me: bool | None = None

    _strong_password = field_validator("new_password")(classmethod(lambda cls, v: _validate_strong_password(v)))


class MessageResponse(BaseModel):
    """Generic message response."""

    message: str


class SetupStatusResponse(BaseModel):
    """Public first-boot and self-registration status."""

    needs_setup: bool
    registration_enabled: bool


# ── Helpers ───────────────────────────────────────────────────────────────


def _set_session_cookie(
    response: Response,
    token: str,
    request: Request,
    *,
    remember_me: bool | None = None,
) -> None:
    """Set the access_token HttpOnly cookie on the response."""
    set_session_cookie(
        response,
        request,
        token,
        remember_me=remember_me,
    )


def _delete_browser_session_cookies(
    response: Response,
    request: Request,
) -> None:
    """Clear every browser cookie that belongs to the current auth session."""

    secure = is_secure_request(request)
    response.delete_cookie(
        key=ACCESS_TOKEN_COOKIE_NAME,
        secure=secure,
        samesite="lax",
    )
    response.delete_cookie(
        key=CSRF_COOKIE_NAME,
        secure=secure,
        samesite="strict",
    )
    response.delete_cookie(
        key=SESSION_PERSISTENCE_COOKIE_NAME,
        secure=secure,
        samesite="lax",
    )
    setattr(request.state, SKIP_AUTH_CSRF_COOKIE_STATE_ATTR, True)


# ── Rate Limiting ────────────────────────────────────────────────────────

_AUTH_RATE_LIMIT_RETRY_AFTER_SECONDS = int(AUTH_RATE_LIMIT_WINDOW.total_seconds())


def _get_client_ip(request: Request) -> str:
    """Return one canonical client IP from an authenticated proxy boundary."""

    return resolve_rate_limit_client_ip(request, get_app_config().auth)


def _database_unavailable_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "DATABASE_UNAVAILABLE",
            "message": "Project storage unavailable",
        },
    )


def _auth_session_unavailable_detail() -> dict[str, str]:
    return {
        "code": "DATABASE_UNAVAILABLE",
        "message": "Authentication storage unavailable",
    }


def _auth_session_unavailable_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=_auth_session_unavailable_detail(),
    )


async def _issue_session(user) -> str:
    try:
        return await issue_access_session(
            user_id=str(user.id),
            token_version=user.token_version,
        )
    except AuthSessionUnavailable:
        raise _auth_session_unavailable_error() from None


def _rate_limited_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=AuthErrorResponse(
            code=AuthErrorCode.RATE_LIMITED,
            message="Too many authentication attempts. Try again later.",
        ).model_dump(),
        headers={
            "Retry-After": str(_AUTH_RATE_LIMIT_RETRY_AFTER_SECONDS),
        },
    )


async def _admit_auth_attempt(
    action: AuthenticationRateLimitAction,
    request: Request,
) -> AuthenticationRateLimitAdmission:
    """Atomically admit one public-auth attempt in shared PostgreSQL state."""

    client_ip = _get_client_ip(request)
    try:
        factory = get_session_factory()
    except RuntimeError:
        raise _database_unavailable_error() from None
    try:
        async with factory() as session:
            admission = await AuthenticationRateLimitRepository(session).admit_attempt(
                action,
                client_ip,
            )
    except ProjectDatabaseUnavailable:
        raise _database_unavailable_error() from None
    if not admission.admitted:
        raise _rate_limited_error()
    return admission


async def _clear_auth_attempts(
    admission: AuthenticationRateLimitAdmission,
) -> None:
    """Clear a successful login's counter without affecting other actions."""

    try:
        factory = get_session_factory()
    except RuntimeError:
        raise _database_unavailable_error() from None
    try:
        async with factory() as session:
            await AuthenticationRateLimitRepository(session).clear(
                admission,
            )
    except ProjectDatabaseUnavailable:
        raise _database_unavailable_error() from None


# ── Endpoints ─────────────────────────────────────────────────────────────


@router.post("/login/local", response_model=LoginResponse)
async def login_local(
    request: Request,
    response: Response,
    form_data: OAuth2PasswordRequestForm = Depends(),
    remember_me: bool = Form(default=True),
):
    """Local email/password login."""
    admission = await _admit_auth_attempt(
        AuthenticationRateLimitAction.LOGIN,
        request,
    )

    user = await get_local_provider().authenticate({"email": form_data.username, "password": form_data.password})

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=AuthErrorResponse(code=AuthErrorCode.INVALID_CREDENTIALS, message="Incorrect email, username, or password").model_dump(),
        )

    await _clear_auth_attempts(
        admission,
    )
    token = await _issue_session(user)
    _set_session_cookie(
        response,
        token,
        request,
        remember_me=remember_me,
    )

    return LoginResponse(
        expires_in=get_auth_config().token_expiry_days * 24 * 3600,
        needs_setup=user.needs_setup,
    )


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(request: Request, response: Response, body: RegisterRequest):
    """Register a new user account (always 'user' role).

    The first admin is created explicitly through /initialize. This endpoint creates regular users.
    Auto-login by setting the session cookie.
    """
    if not await _local_registration_enabled(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=AuthErrorResponse(
                code=AuthErrorCode.REGISTRATION_DISABLED,
                message="Self-registration is disabled on this deployment",
            ).model_dump(),
        )
    await _admit_auth_attempt(
        AuthenticationRateLimitAction.REGISTER,
        request,
    )
    try:
        user = await get_local_provider().create_user(
            email=body.email,
            username=body.username,
            password=body.password,
            system_role="user",
        )
    except DuplicateUserIdentity as exc:
        raise _duplicate_identity_http_error(exc) from None
    except UsernameInvalid as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=AuthErrorResponse(code=AuthErrorCode.INVALID_USERNAME, message=str(exc)).model_dump(),
        ) from None

    token = await _issue_session(user)
    _set_session_cookie(
        response,
        token,
        request,
        remember_me=body.remember_me,
    )

    return _user_response(user)


@router.post("/logout", response_model=MessageResponse)
async def logout(request: Request, response: Response):
    """Revoke the current durable session, then clear the cookie."""

    access_token = request.cookies.get(ACCESS_TOKEN_COOKIE_NAME)
    if access_token:
        payload = decode_token(access_token)
        if not isinstance(payload, TokenError):
            try:
                await revoke_access_session(payload)
            except AuthSessionUnavailable:
                # Do not trap the browser in a local session when the durable
                # authority is unavailable. The 503 truthfully reports that a
                # copied token may not yet be revoked, while Max-Age=0 removes
                # this browser's cookie immediately.
                failure = JSONResponse(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    content={"detail": _auth_session_unavailable_detail()},
                )
                _delete_browser_session_cookies(failure, request)
                return failure
    _delete_browser_session_cookies(response, request)
    return MessageResponse(message="Successfully logged out")


@router.post("/change-password", response_model=MessageResponse)
async def change_password(request: Request, response: Response, body: ChangePasswordRequest):
    """Change password for the currently authenticated user.

    Also handles the first-boot setup flow:
    - If new_email is provided, updates email (checks uniqueness)
    - If user.needs_setup is True and new_email is given, clears needs_setup
    - Always increments token_version to invalidate old sessions
    - Re-issues session cookie with new token_version
    """
    from app.gateway.auth.password import hash_password_async, verify_password_async
    from app.gateway.auth_disabled import AUTH_SOURCE_AUTH_DISABLED

    user = await get_current_user_from_request(request)

    if getattr(request.state, "auth_source", None) == AUTH_SOURCE_AUTH_DISABLED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=AuthErrorResponse(
                code=AuthErrorCode.INVALID_CREDENTIALS,
                message="Password changes are not available when DEER_FLOW_AUTH_DISABLED=1.",
            ).model_dump(),
        )

    if user.password_hash is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=AuthErrorResponse(code=AuthErrorCode.INVALID_CREDENTIALS, message="OAuth users cannot change password").model_dump())

    if not await verify_password_async(body.current_password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=AuthErrorResponse(code=AuthErrorCode.INVALID_CREDENTIALS, message="Current password is incorrect").model_dump())

    provider = get_local_provider()

    # Update email if provided
    if body.new_email is not None:
        existing = await provider.get_user_by_email(body.new_email)
        if existing and str(existing.id) != str(user.id):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=AuthErrorResponse(code=AuthErrorCode.EMAIL_ALREADY_EXISTS, message="Email already in use").model_dump())
        user.email = body.new_email

    # Update password + bump version
    user.password_hash = await hash_password_async(body.new_password)
    user.token_version += 1

    # Clear setup flag if this is the setup flow
    if user.needs_setup and body.new_email is not None:
        user.needs_setup = False

    try:
        await provider.update_user(user)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=AuthErrorResponse(
                code=AuthErrorCode.EMAIL_ALREADY_EXISTS,
                message="Email already in use",
            ).model_dump(),
        ) from None

    # The token_version update already invalidates every old JWT. Persist the
    # corresponding session revocations before admitting one fresh session.
    try:
        await revoke_all_access_sessions(str(user.id))
    except AuthSessionUnavailable:
        raise _auth_session_unavailable_error() from None
    token = await _issue_session(user)
    _set_session_cookie(
        response,
        token,
        request,
        remember_me=body.remember_me,
    )

    return MessageResponse(message="Password changed successfully")


@router.get("/me", response_model=UserResponse)
async def get_me(request: Request):
    """Get current authenticated user info."""
    user = await get_current_user_from_request(request)
    return _user_response(user)


# Per-IP cache: ip → (timestamp, needs_setup).
# Returns the cached result within the TTL instead of 429, because
# the answer (whether an admin exists) rarely changes and returning
# 429 breaks multi-tab / post-restart reconnection storms.
_SETUP_STATUS_CACHE: dict[str, tuple[float, bool]] = {}
_SETUP_STATUS_CACHE_TTL_SECONDS = 60
_MAX_TRACKED_SETUP_STATUS_IPS = 10000
_SETUP_STATUS_INFLIGHT: dict[str, asyncio.Task[bool]] = {}
_SETUP_STATUS_INFLIGHT_GUARD = asyncio.Lock()


async def _local_registration_enabled(request: Request) -> bool:
    """Read the current database-owned local registration policy."""

    materializer = getattr(
        request.app.state,
        "system_runtime_policy_materializer",
        None,
    )
    try:
        if materializer is None:
            raise RuntimeError
        policy = await materializer.materialize_current(
            RuntimePolicySection.AUTH,
        )
        if type(policy) is not AuthPolicyValue:
            raise RuntimeError
        return policy.allow_registration
    except asyncio.CancelledError:
        raise
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "AUTH_POLICY_UNAVAILABLE",
                "message": "Authentication policy unavailable",
            },
        ) from None


async def _setup_status_response(
    request: Request,
    needs_setup: bool,
) -> dict[str, bool]:
    """Combine cached initialization state with the live registration policy."""

    return {
        "needs_setup": needs_setup,
        "registration_enabled": await _local_registration_enabled(request),
    }


@router.get("/setup-status", response_model=SetupStatusResponse)
async def setup_status(request: Request):
    """Check if an admin account exists. Returns needs_setup=True when no admin exists."""
    client_ip = _get_client_ip(request)
    now = time.time()

    # Return cached result when within TTL — avoids 429 on multi-tab reconnection.
    cached = _SETUP_STATUS_CACHE.get(client_ip)
    if cached is not None:
        cached_time, cached_needs_setup = cached
        if now - cached_time < _SETUP_STATUS_CACHE_TTL_SECONDS:
            return await _setup_status_response(request, cached_needs_setup)

    async with _SETUP_STATUS_INFLIGHT_GUARD:
        # Recheck cache after waiting for the inflight guard.
        now = time.time()
        cached = _SETUP_STATUS_CACHE.get(client_ip)
        if cached is not None:
            cached_time, cached_needs_setup = cached
            if now - cached_time < _SETUP_STATUS_CACHE_TTL_SECONDS:
                return await _setup_status_response(
                    request,
                    cached_needs_setup,
                )

        task = _SETUP_STATUS_INFLIGHT.get(client_ip)
        if task is None:
            # Evict stale entries when dict grows too large to bound memory usage.
            if len(_SETUP_STATUS_CACHE) >= _MAX_TRACKED_SETUP_STATUS_IPS:
                cutoff = now - _SETUP_STATUS_CACHE_TTL_SECONDS
                stale = [k for k, (t, _) in _SETUP_STATUS_CACHE.items() if t < cutoff]
                for k in stale:
                    del _SETUP_STATUS_CACHE[k]
                if len(_SETUP_STATUS_CACHE) >= _MAX_TRACKED_SETUP_STATUS_IPS:
                    by_time = sorted(_SETUP_STATUS_CACHE.items(), key=lambda entry: entry[1][0])
                    for k, _ in by_time[: len(by_time) // 2]:
                        del _SETUP_STATUS_CACHE[k]

            async def _compute_needs_setup() -> bool:
                admin_count = await get_local_provider().count_admin_users()
                return admin_count == 0

            task = asyncio.create_task(_compute_needs_setup())
            _SETUP_STATUS_INFLIGHT[client_ip] = task

    try:
        result = await task
    finally:
        async with _SETUP_STATUS_INFLIGHT_GUARD:
            if _SETUP_STATUS_INFLIGHT.get(client_ip) is task:
                del _SETUP_STATUS_INFLIGHT[client_ip]

    # Cache only the stable "initialized" result to avoid stale setup redirects.
    if result is False:
        _SETUP_STATUS_CACHE[client_ip] = (time.time(), result)
    else:
        _SETUP_STATUS_CACHE.pop(client_ip, None)
    return await _setup_status_response(request, result)


class InitializeAdminRequest(BaseModel):
    """Request model for first-boot admin account creation."""

    email: EmailStr
    username: str = Field(..., min_length=3, max_length=32)
    password: str = Field(..., min_length=8)
    remember_me: bool = True

    _strong_password = field_validator("password")(classmethod(lambda cls, v: _validate_strong_password(v)))
    _valid_username = field_validator("username")(classmethod(lambda cls, v: _validate_username(v)))


@router.post("/initialize", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def initialize_admin(request: Request, response: Response, body: InitializeAdminRequest):
    """Create the first admin account on initial system setup.

    Only callable when no admin exists. Returns 409 Conflict if an admin
    already exists.

    On success, the admin account is created with ``needs_setup=False`` and
    the session cookie is set.
    """
    try:
        async with _initialize_admin_lock():
            admin_count = await get_local_provider().count_admin_users()
            if admin_count > 0:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=AuthErrorResponse(code=AuthErrorCode.SYSTEM_ALREADY_INITIALIZED, message="System already initialized").model_dump(),
                )

            try:
                user = await get_local_provider().create_user(
                    email=body.email,
                    username=body.username,
                    password=body.password,
                    system_role="system_admin",
                    needs_setup=False,
                )
            except DuplicateUserIdentity as exc:
                admin_count = await get_local_provider().count_admin_users()
                if admin_count == 0:
                    raise _duplicate_identity_http_error(exc) from None
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=AuthErrorResponse(code=AuthErrorCode.SYSTEM_ALREADY_INITIALIZED, message="System already initialized").model_dump(),
                ) from None
            except UsernameInvalid as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=AuthErrorResponse(code=AuthErrorCode.INVALID_USERNAME, message=str(exc)).model_dump(),
                ) from None

            try:
                factory = get_session_factory()
            except RuntimeError:
                raise HTTPException(
                    status_code=503,
                    detail={"code": "DATABASE_UNAVAILABLE", "message": "Project storage unavailable"},
                ) from None
            from app.projects.bootstrap import bootstrap_default_project

            async with factory() as session:
                await bootstrap_default_project(
                    session,
                    quota=get_project_quota_enforcer(request),
                )
    except ProjectBootstrapFailed as exc:
        raise HTTPException(status_code=503, detail={"code": exc.code, "message": "Project bootstrap failed"}) from None
    except ProjectDatabaseUnavailable:
        raise HTTPException(status_code=503, detail={"code": "DATABASE_UNAVAILABLE", "message": "Project storage unavailable"}) from None

    token = await _issue_session(user)
    _set_session_cookie(
        response,
        token,
        request,
        remember_me=body.remember_me,
    )

    return _user_response(user)


# ── OIDC / SSO Endpoints ────────────────────────────────────────────────

_OIDC_PROVIDER_KEY_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _get_oidc_service() -> OIDCService:
    """Get (or create) the singleton OIDC service instance."""
    if not hasattr(_get_oidc_service, "_instance"):
        _get_oidc_service._instance = OIDCService()  # type: ignore[attr-defined]
    return _get_oidc_service._instance  # type: ignore[attr-defined]


async def close_oidc_service() -> None:
    service = getattr(_get_oidc_service, "_instance", None)
    if service is not None:
        await service.close()
        delattr(_get_oidc_service, "_instance")


def _set_csrf_cookie(response: Response, request: Request) -> None:
    """Set the CSRF double-submit cookie (needed for GET-based OIDC callback)."""
    csrf_token = generate_csrf_token()
    secure, max_age = auth_csrf_cookie_settings(request)
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,  # Must be JS-readable for Double Submit Cookie pattern
        secure=secure,
        samesite="strict",
        # Persist for the same lifetime as the access_token (see _set_session_cookie)
        # so the double-submit pair is evicted together, never leaving a logged-in
        # session whose csrf_token was dropped (e.g. iOS Safari PWA termination).
        max_age=max_age,
    )


def _resolve_oidc_redirect_uri(request: Request, provider_id: str, provider_config: OIDCProviderConfig) -> str:
    """Resolve the redirect URI for an OIDC provider.

    Prefers the explicitly configured ``redirect_uri``. Falls back to
    constructing one from the request's own base URL for development.
    """
    if provider_config.redirect_uri:
        return provider_config.redirect_uri

    # Development fallback: build from the request's proxy-aware origin (honors
    # Forwarded / X-Forwarded-* the same way CSRF origin checks do) rather than
    # the raw Host header, so a spoofed Host cannot steer the IdP redirect_uri
    # and the scheme reflects the real client-facing protocol behind a proxy.
    origin = _request_origin(request)
    if not origin:
        origin = f"{request.url.scheme}://{request.headers.get('host', 'localhost:8001')}"
    return f"{origin}/api/v1/auth/callback/{provider_id}"


@router.get("/providers")
async def list_auth_providers():
    """List enabled SSO providers for the login page.

    Returns only safe frontend metadata — no secrets, endpoints, or
    internal configuration.
    """
    from deerflow.config.app_config import get_app_config

    app_config = get_app_config()
    oidc_config = app_config.auth.oidc

    if not oidc_config.enabled:
        return {"providers": []}

    providers = []
    for provider_id, provider_cfg in oidc_config.providers.items():
        providers.append(
            {
                "id": provider_id,
                "display_name": provider_cfg.display_name,
                "type": "oidc",
            }
        )
    return {"providers": providers}


@router.get("/oauth/{provider}")
async def oauth_login(
    request: Request,
    provider: str,
    next: str | None = None,  # noqa: A002 (shadowing built-in is intentional — this is the query param name)
    remember_me: bool = True,
):
    """Initiate OIDC login flow.

    Redirects to the OIDC provider's authorization URL with state, nonce,
    and PKCE parameters. The ``next`` query parameter specifies where to
    redirect after successful login (default: /workspace).
    """
    from deerflow.config.app_config import get_app_config

    app_config = get_app_config()
    oidc_config = app_config.auth.oidc

    if not oidc_config.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SSO authentication is not enabled")

    if not _OIDC_PROVIDER_KEY_RE.match(provider):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid provider ID")

    provider_config = oidc_config.providers.get(provider)
    if not provider_config:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown SSO provider: {provider}")

    # Validate `next` / open redirect prevention
    redirect_path = validate_next_param(next) or "/workspace"

    # Resolve redirect URI
    redirect_uri = _resolve_oidc_redirect_uri(request, provider, provider_config)

    # Generate state, nonce, PKCE
    state_value = generate_oidc_state()
    nonce_value = generate_nonce() if provider_config.nonce_enabled else None
    code_verifier = generate_code_verifier() if provider_config.pkce_enabled else None
    code_challenge = compute_code_challenge(code_verifier) if code_verifier else None

    # Get provider metadata via discovery
    overrides = {
        "authorization_endpoint": provider_config.authorization_endpoint,
        "token_endpoint": provider_config.token_endpoint,
        "userinfo_endpoint": provider_config.userinfo_endpoint,
        "jwks_uri": provider_config.jwks_uri,
    }
    service = _get_oidc_service()
    try:
        metadata = await service.discover(provider_config.issuer, overrides)
    except OIDCError as exc:
        logger.error("OIDC discovery failed for provider %s: %s", provider, exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to connect to SSO provider")

    auth_url = service.build_authorization_url(
        metadata=metadata,
        client_id=provider_config.client_id,
        redirect_uri=redirect_uri,
        scopes=provider_config.scopes,
        state=state_value,
        nonce=nonce_value,
        code_challenge=code_challenge,
    )

    # Set signed state cookie
    state_payload = OIDCStatePayload(
        provider=provider,
        state=state_value,
        nonce=nonce_value,
        code_verifier=code_verifier,
        next_path=redirect_path,
        remember_me=remember_me,
    )
    redirect_response = RedirectResponse(url=auth_url, status_code=status.HTTP_302_FOUND)
    set_state_cookie(redirect_response, request, state_payload)

    return redirect_response


@router.get("/callback/{provider}")
async def oauth_callback(
    request: Request,
    provider: str,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    """OIDC callback endpoint.

    Handles the OIDC provider's redirect after user authorization.
    Validates the state cookie, exchanges the code for tokens, validates
    the ID token, provisions/links the ActWeave user, and sets the
    session cookie.
    """
    from deerflow.config.app_config import get_app_config

    app_config = get_app_config()
    oidc_config = app_config.auth.oidc

    # ── Provider error ───────────────────────────────────────────────
    if error:
        logger.warning("OIDC provider returned error for %s: %s (description: %s)", provider, error, error_description)
        redirect = _build_error_redirect(oidc_config.frontend_base_url, "sso_failed")
        return RedirectResponse(url=redirect, status_code=status.HTTP_302_FOUND)

    if not oidc_config.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SSO authentication is not enabled")

    if not _OIDC_PROVIDER_KEY_RE.match(provider):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid provider ID")

    provider_config = oidc_config.providers.get(provider)
    if not provider_config:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown SSO provider: {provider}")

    if not code or not state:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing code or state parameter")

    # ── Verify state cookie ──────────────────────────────────────────
    state_payload = get_state_cookie(request, provider)
    if not state_payload:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Missing or expired OIDC state cookie")

    if not secrets.compare_digest(state_payload.state, state):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="OIDC state mismatch")

    # ── Resolve redirect URI ─────────────────────────────────────────
    redirect_uri = _resolve_oidc_redirect_uri(request, provider, provider_config)

    # ── Get metadata ─────────────────────────────────────────────────
    overrides = {
        "authorization_endpoint": provider_config.authorization_endpoint,
        "token_endpoint": provider_config.token_endpoint,
        "userinfo_endpoint": provider_config.userinfo_endpoint,
        "jwks_uri": provider_config.jwks_uri,
    }
    service = _get_oidc_service()
    try:
        metadata = await service.discover(provider_config.issuer, overrides)
    except OIDCError as exc:
        logger.error("OIDC discovery failed for provider %s during callback: %s", provider, exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to connect to SSO provider")

    # ── Authenticate ─────────────────────────────────────────────────
    try:
        identity = await service.authenticate_callback(
            provider_id=provider,
            metadata=metadata,
            client_id=provider_config.client_id,
            client_secret=provider_config.client_secret,
            code=code,
            redirect_uri=redirect_uri,
            code_verifier=state_payload.code_verifier,
            nonce=state_payload.nonce,
            auth_method=provider_config.token_endpoint_auth_method,
        )
    except OIDCError as exc:
        logger.error("OIDC callback authentication failed for %s: %s", provider, exc)
        redirect = _build_error_redirect(oidc_config.frontend_base_url, "sso_failed")
        return RedirectResponse(url=redirect, status_code=status.HTTP_302_FOUND)

    # ── Provision / link user ────────────────────────────────────────
    try:
        result = await get_or_provision_oidc_user(provider, provider_config, identity, get_local_provider())
    except HTTPException as exc:
        error_map = {
            status.HTTP_403_FORBIDDEN: "sso_not_allowed",
            status.HTTP_409_CONFLICT: "sso_account_exists",
        }
        error_code = error_map.get(exc.status_code, "sso_failed")
        logger.warning("OIDC user provisioning failed for %s (%s): %s", identity.email, provider, exc.detail)
        redirect = _build_error_redirect(oidc_config.frontend_base_url, error_code)
        return RedirectResponse(url=redirect, status_code=status.HTTP_302_FOUND)

    user = result["user"]

    # ── Issue ActWeave session ───────────────────────────────────────
    token = await _issue_session(user)

    redirect_target = state_payload.next_path or "/workspace"
    frontend_base = oidc_config.frontend_base_url or ""
    callback_redirect = f"{frontend_base}/auth/callback?next={urllib.parse.quote(redirect_target)}"

    redirect_response = RedirectResponse(url=callback_redirect, status_code=status.HTTP_302_FOUND)

    # Set session cookie (reuse existing helper)
    _set_session_cookie(
        redirect_response,
        token,
        request,
        remember_me=state_payload.remember_me,
    )

    # Set CSRF cookie (callback is a GET, so CSRF middleware won't set it)
    _set_csrf_cookie(redirect_response, request)

    # Delete state cookie
    delete_state_cookie(redirect_response, request, provider)

    return redirect_response


def _build_error_redirect(frontend_base_url: str | None, error_code: str) -> str:
    """Build a frontend redirect URL with an error parameter."""
    base = frontend_base_url or ""
    return f"{base}/login?error={error_code}"


def validate_next_param(next_param: str | None) -> str | None:
    """Validate and sanitize the ``next`` redirect parameter.

    Only allows relative paths starting with ``/``. Rejects protocol-relative
    URLs (``//``), absolute URLs, and URLs with embedded protocols.
    """
    if not next_param:
        return None
    if not next_param.startswith("/"):
        return None
    if next_param.startswith("//") or next_param.startswith("http://") or next_param.startswith("https://"):
        return None
    if ":" in next_param:
        return None
    return next_param
