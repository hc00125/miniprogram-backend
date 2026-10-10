from rest_framework_simplejwt.authentication import JWTAuthentication

class StrictWechatJWTAuthentication(JWTAuthentication):
    """Never calls the legacy authenticator (which can create users)."""
    pass
