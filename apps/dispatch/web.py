from django.conf import settings
from django.contrib.auth import logout
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.views import LoginView
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import render, redirect
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST
from .views import console_access


class ConsoleAuthForm(AuthenticationForm):
    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if not console_access(user):
            raise ValidationError('此账号尚未开通客服派单权限，请联系管理员')


class ConsoleLogin(LoginView):
    template_name = 'dispatch/login.html'
    authentication_form = ConsoleAuthForm
    next_page = '/dispatch/'
    redirect_authenticated_user = False

    def dispatch(self, request, *args, **kwargs):
        if getattr(settings, 'DISPATCH_REQUIRE_HTTPS', False) and not request.is_secure():
            return redirect('https://' + request.get_host() + request.get_full_path())
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        from .login_security import allow_login_attempt
        if not allow_login_attempt(request):
            form = self.get_form()
            form.is_valid()
            form.add_error(None, '登录尝试过多，请15分钟后再试或联系管理员')
            return self.render_to_response(self.get_context_data(form=form), status=429)
        return super().post(request, *args, **kwargs)


@never_cache
def console(request):
    if not request.user.is_authenticated:
        return redirect('/dispatch/login/')
    if not console_access(request.user):
        raise PermissionDenied('账号未开通客服派单权限')
    return render(request, 'dispatch/console.html', {'operator_id': request.user.pk})


@require_POST
@csrf_protect
def sign_out(request):
    logout(request)
    return redirect('/dispatch/login/')
