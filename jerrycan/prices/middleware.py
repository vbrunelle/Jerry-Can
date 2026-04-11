from django.shortcuts import redirect


class ForcePasswordChangeMiddleware:
    EXEMPT_PATHS = {
        '/accounts/login/',
        '/accounts/logout/',
        '/accounts/password-change/',
        '/accounts/password-change/done/',
    }

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            request.user.is_authenticated
            and request.path not in self.EXEMPT_PATHS
            and not request.path.startswith('/admin/')
        ):
            try:
                if request.user.profile.must_change_password:
                    return redirect('password_change')
            except Exception:
                pass
        return self.get_response(request)
