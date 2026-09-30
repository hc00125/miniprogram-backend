from django.conf import settings


class ConsoleCookieSecurity:
    """Secure cookies on the new HTTPS console only; preserve legacy site settings."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if not request.path.startswith('/dispatch/') or not getattr(settings, 'DISPATCH_REQUIRE_HTTPS', False):
            return response
        session_name = settings.SESSION_COOKIE_NAME
        # Upgrade an inherited administrator session upon entering the console.
        if request.path == '/dispatch/' and request.is_secure() and getattr(request, 'user', None) and request.user.is_authenticated and session_name not in response.cookies:
            session = request.session
            if session.session_key:
                response.set_cookie(session_name, session.session_key,
                    max_age=None if session.get_expire_at_browser_close() else session.get_expiry_age(),
                    path=settings.SESSION_COOKIE_PATH, domain=settings.SESSION_COOKIE_DOMAIN,
                    httponly=settings.SESSION_COOKIE_HTTPONLY, samesite=settings.SESSION_COOKIE_SAMESITE,
                    secure=True)
        for name in (session_name, settings.CSRF_COOKIE_NAME):
            if name in response.cookies:
                response.cookies[name]['secure'] = True
        return response
