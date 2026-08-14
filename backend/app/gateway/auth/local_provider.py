"""Local email/password authentication provider."""

import logging

from app.gateway.auth.identity import DuplicateUserIdentity
from app.gateway.auth.models import User
from app.gateway.auth.password import hash_password_async, needs_rehash, verify_password_async
from app.gateway.auth.providers import AuthProvider
from app.gateway.auth.repositories.base import UserRepository
from app.gateway.auth.username import parse_username, username_from_email_local_part

logger = logging.getLogger(__name__)


class LocalAuthProvider(AuthProvider):
    """Email/password authentication provider using local database."""

    def __init__(self, repository: UserRepository):
        """Initialize with a UserRepository.

        Args:
            repository: PostgreSQL-backed UserRepository implementation
        """
        self._repo = repository

    async def authenticate(self, credentials: dict) -> User | None:
        """Authenticate with email or username plus password.

        Args:
            credentials: dict with 'password' and either 'email' or 'username'

        Returns:
            User if authentication succeeds, None otherwise
        """
        identifier = credentials.get("email") or credentials.get("username")
        password = credentials.get("password")

        if not identifier or not password:
            return None

        if "@" in identifier:
            user = await self._repo.get_user_by_email(identifier)
        else:
            user = await self._repo.get_user_by_username(identifier)
        if user is None:
            return None

        if user.password_hash is None:
            # OAuth user without local password
            return None

        if not await verify_password_async(password, user.password_hash):
            return None

        if needs_rehash(user.password_hash):
            try:
                user.password_hash = await hash_password_async(password)
                await self._repo.update_user(user)
            except Exception:
                # Rehash is an opportunistic upgrade; a transient DB error must not
                # prevent an otherwise-valid login from succeeding.
                logger.warning("Failed to rehash password for user %s; login will still succeed", user.email, exc_info=True)

        return user

    async def get_user(self, user_id: str) -> User | None:
        """Get user by ID."""
        return await self._repo.get_user_by_id(user_id)

    async def create_user(
        self,
        email: str,
        username: str,
        password: str | None = None,
        system_role: str = "user",
        needs_setup: bool = False,
    ) -> User:
        """Create a new local user.

        Args:
            email: User email address
            username: Unique login username
            password: Plain text password (will be hashed)
            system_role: Role to assign ("system_admin" or "user")
            needs_setup: If True, user must complete setup on first login

        Returns:
            Created User instance
        """
        password_hash = await hash_password_async(password) if password else None
        user = User(
            email=email,
            username=parse_username(username),
            password_hash=password_hash,
            system_role=system_role,
            needs_setup=needs_setup,
        )
        return await self._repo.create_user(user)

    async def get_user_by_oauth(self, provider: str, oauth_id: str) -> User | None:
        """Get user by OAuth provider and ID."""
        return await self._repo.get_user_by_oauth(provider, oauth_id)

    async def count_users(self) -> int:
        """Return total number of registered users."""
        return await self._repo.count_users()

    async def count_admin_users(self) -> int:
        """Return number of admin users."""
        return await self._repo.count_admin_users()

    async def update_user(self, user: User) -> User:
        """Update an existing user."""
        return await self._repo.update_user(user)

    async def get_user_by_email(self, email: str) -> User | None:
        """Get user by email."""
        return await self._repo.get_user_by_email(email)

    async def get_user_by_username(self, username: str) -> User | None:
        """Get user by username."""
        return await self._repo.get_user_by_username(username)

    async def create_oauth_user(
        self,
        email: str,
        oauth_provider: str,
        oauth_id: str,
        system_role: str = "user",
    ) -> User:
        """Create a new user from an OAuth/OIDC login.

        Args:
            email: Verified email from the OIDC provider
            oauth_provider: Provider ID (e.g. 'keycloak', 'google')
            oauth_id: User's subject claim from the ID token
            system_role: Role to assign ("system_admin" or "user")

        Returns:
            Created User instance
        """
        base = username_from_email_local_part(email)
        last_error: DuplicateUserIdentity | None = None
        for index in range(1, 100):
            suffix = "" if index == 1 else f"_{index}"
            candidate = parse_username(f"{base[: 32 - len(suffix)]}{suffix}")
            user = User(
                email=email,
                username=candidate,
                password_hash=None,
                system_role=system_role,
                needs_setup=False,
                oauth_provider=oauth_provider,
                oauth_id=oauth_id,
            )
            try:
                return await self._repo.create_user(user)
            except DuplicateUserIdentity as exc:
                last_error = exc
                if exc.field != "username":
                    raise
        raise last_error or DuplicateUserIdentity("username", base)
