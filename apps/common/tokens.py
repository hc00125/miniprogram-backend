import secrets


def generate_session_token():
    return secrets.token_urlsafe(32)
