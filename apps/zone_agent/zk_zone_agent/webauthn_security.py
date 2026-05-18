from __future__ import annotations

import json
import secrets
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorAttachment,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    PublicKeyCredentialHint,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from zk_common.time_utils import utc_now
from zk_zone_agent.db import AdminWebAuthnChallenge, AdminWebAuthnCredential, LocalAdmin
from zk_zone_agent.local_security import create_admin, get_admin


WEBAUTHN_RP_ID = "localhost"
WEBAUTHN_RP_NAME = "ZK Zone Agent"
WEBAUTHN_ADMIN_USER_HANDLE = b"zk-zone-local-admin"
WEBAUTHN_CHALLENGE_SECONDS = 5 * 60


def expected_webauthn_origin(port: int | None = 7860, scheme: str = "http") -> str:
    if not port or (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        return f"{scheme}://{WEBAUTHN_RP_ID}"
    return f"{scheme}://{WEBAUTHN_RP_ID}:{port}"


def webauthn_origin_for_request(request: Any) -> str:
    scheme = "https" if request.url.scheme == "https" else "http"
    return expected_webauthn_origin(request.url.port, scheme)


def webauthn_origin_is_canonical(request: Any) -> bool:
    return request.url.hostname == WEBAUTHN_RP_ID


class WebAuthnAdminSecurity:
    def registration_options(self, session: Session, *, label: str | None = None) -> dict[str, Any]:
        admin = get_admin(session)
        challenge = secrets.token_bytes(32)
        challenge_row = self._create_challenge(
            session,
            purpose="registration",
            challenge=challenge,
            admin_id=None if admin is None else admin.id,
        )
        options = generate_registration_options(
            rp_id=WEBAUTHN_RP_ID,
            rp_name=WEBAUTHN_RP_NAME,
            user_id=WEBAUTHN_ADMIN_USER_HANDLE,
            user_name="zone-agent-admin",
            user_display_name="Zone Agent Local Admin",
            challenge=challenge,
            timeout=60_000,
            authenticator_selection=AuthenticatorSelectionCriteria(
                authenticator_attachment=AuthenticatorAttachment.PLATFORM,
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=[
                PublicKeyCredentialDescriptor(id=base64url_to_bytes(row.credential_id))
                for row in self.credentials(session)
            ],
            hints=[PublicKeyCredentialHint.CLIENT_DEVICE],
        )
        return {
            "challenge_id": challenge_row.id,
            "publicKey": json.loads(options_to_json(options)),
            "label": self._credential_label(label),
        }

    def verify_registration(
        self,
        session: Session,
        *,
        challenge_id: str,
        credential: dict[str, Any],
        expected_origin: str,
        label: str | None = None,
        recovery_password: str | None = None,
    ) -> tuple[LocalAdmin, AdminWebAuthnCredential]:
        challenge = self._consume_challenge(session, purpose="registration", challenge_id=challenge_id)
        verification = verify_registration_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(challenge.challenge),
            expected_rp_id=WEBAUTHN_RP_ID,
            expected_origin=expected_origin,
            require_user_verification=True,
        )
        admin = get_admin(session)
        if admin is None:
            admin = create_admin(session, recovery_password)
        credential_id = bytes_to_base64url(verification.credential_id)
        existing = session.scalar(
            select(AdminWebAuthnCredential).where(AdminWebAuthnCredential.credential_id == credential_id)
        )
        if existing is not None:
            raise ValueError("This Windows Hello credential is already enrolled.")
        row = AdminWebAuthnCredential(
            admin_id=admin.id,
            credential_id=credential_id,
            public_key=bytes_to_base64url(verification.credential_public_key),
            sign_count=verification.sign_count,
            label=self._credential_label(label),
            aaguid=verification.aaguid,
            credential_device_type=str(verification.credential_device_type.value),
            credential_backed_up=bool(verification.credential_backed_up),
        )
        session.add(row)
        session.flush()
        return admin, row

    def authentication_options(self, session: Session) -> dict[str, Any]:
        credentials = self.credentials(session)
        if not credentials:
            raise ValueError("Windows Hello unlock is not enrolled.")
        challenge = secrets.token_bytes(32)
        challenge_row = self._create_challenge(
            session,
            purpose="authentication",
            challenge=challenge,
            admin_id=1,
        )
        options = generate_authentication_options(
            rp_id=WEBAUTHN_RP_ID,
            challenge=challenge,
            timeout=60_000,
            allow_credentials=[
                PublicKeyCredentialDescriptor(id=base64url_to_bytes(row.credential_id))
                for row in credentials
            ],
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        return {
            "challenge_id": challenge_row.id,
            "publicKey": json.loads(options_to_json(options)),
        }

    def verify_authentication(
        self,
        session: Session,
        *,
        challenge_id: str,
        credential: dict[str, Any],
        expected_origin: str,
    ) -> LocalAdmin:
        challenge = self._consume_challenge(session, purpose="authentication", challenge_id=challenge_id)
        credential_id = credential.get("id")
        if not isinstance(credential_id, str):
            raise ValueError("Windows Hello response is missing a credential id.")
        stored = session.scalar(
            select(AdminWebAuthnCredential).where(AdminWebAuthnCredential.credential_id == credential_id)
        )
        if stored is None:
            raise ValueError("Windows Hello credential is not recognized.")
        verification = verify_authentication_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(challenge.challenge),
            expected_rp_id=WEBAUTHN_RP_ID,
            expected_origin=expected_origin,
            credential_public_key=base64url_to_bytes(stored.public_key),
            credential_current_sign_count=stored.sign_count,
            require_user_verification=True,
        )
        stored.sign_count = verification.new_sign_count
        stored.credential_device_type = str(verification.credential_device_type.value)
        stored.credential_backed_up = bool(verification.credential_backed_up)
        stored.last_used_at = utc_now()
        stored.updated_at = utc_now()
        admin = get_admin(session)
        if admin is None:
            raise ValueError("Local admin is not configured.")
        admin.failed_login_count = 0
        admin.locked_until = None
        admin.updated_at = utc_now()
        return admin

    def credentials(self, session: Session) -> list[AdminWebAuthnCredential]:
        return list(session.scalars(select(AdminWebAuthnCredential).order_by(AdminWebAuthnCredential.id.asc())))

    def credential_count(self, session: Session) -> int:
        return len(self.credentials(session))

    def _create_challenge(
        self,
        session: Session,
        *,
        purpose: str,
        challenge: bytes,
        admin_id: int | None,
    ) -> AdminWebAuthnChallenge:
        now = utc_now()
        row = AdminWebAuthnChallenge(
            id=secrets.token_urlsafe(32),
            admin_id=admin_id,
            purpose=purpose,
            challenge=bytes_to_base64url(challenge),
            expires_at=now + timedelta(seconds=WEBAUTHN_CHALLENGE_SECONDS),
        )
        session.add(row)
        session.flush()
        return row

    def _consume_challenge(
        self,
        session: Session,
        *,
        purpose: str,
        challenge_id: str,
    ) -> AdminWebAuthnChallenge:
        row = session.get(AdminWebAuthnChallenge, challenge_id)
        now = utc_now()
        if row is None or row.purpose != purpose:
            raise ValueError("Windows Hello challenge was not found.")
        if row.used_at is not None:
            raise ValueError("Windows Hello challenge was already used.")
        if row.expires_at < now:
            raise ValueError("Windows Hello challenge expired.")
        row.used_at = now
        return row

    def _credential_label(self, label: str | None) -> str:
        label = (label or "").strip()
        return label[:255] if label else "Windows Hello"


webauthn_admin_security = WebAuthnAdminSecurity()
