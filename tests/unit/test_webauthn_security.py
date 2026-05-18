from datetime import timedelta

import pytest

from zk_common.time_utils import utc_now


def test_webauthn_challenges_are_one_time_and_expiring(monkeypatch, tmp_path):
    monkeypatch.setenv("ZK_ZONE_DATABASE_URL", f"sqlite:///{tmp_path / 'zone.db'}")
    import importlib
    import zk_zone_agent.db as db_module
    import zk_zone_agent.webauthn_security as webauthn_module

    db_module = importlib.reload(db_module)
    webauthn_module = importlib.reload(webauthn_module)
    db_module.init_db()
    security = webauthn_module.WebAuthnAdminSecurity()

    with db_module.session_scope() as session:
        options = security.registration_options(session, label="Front Desk")
        challenge_id = options["challenge_id"]
        assert options["publicKey"]["rp"]["id"] == "localhost"
        assert options["publicKey"]["authenticatorSelection"]["authenticatorAttachment"] == "platform"
        assert options["publicKey"]["authenticatorSelection"]["userVerification"] == "required"

        security._consume_challenge(session, purpose="registration", challenge_id=challenge_id)
        with pytest.raises(ValueError, match="already used"):
            security._consume_challenge(session, purpose="registration", challenge_id=challenge_id)

    with db_module.session_scope() as session:
        expired = security.registration_options(session, label="Front Desk")
        row = session.get(db_module.AdminWebAuthnChallenge, expired["challenge_id"])
        row.expires_at = utc_now() - timedelta(seconds=1)
        with pytest.raises(ValueError, match="expired"):
            security._consume_challenge(
                session,
                purpose="registration",
                challenge_id=expired["challenge_id"],
            )
        with pytest.raises(ValueError, match="not found"):
            security._consume_challenge(session, purpose="authentication", challenge_id="missing")
