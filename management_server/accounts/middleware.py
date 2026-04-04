from django.shortcuts import redirect
from django.urls import reverse


class ForcePasswordChangeMiddleware:
    """Redirect authenticated users with must_change_password=True to the
    change-password page.  Exempt paths: change-password, logout, login,
    and Django admin/static URLs."""

    EXEMPT_URLS = None

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if self.EXEMPT_URLS is None:
            ForcePasswordChangeMiddleware.EXEMPT_URLS = {
                reverse('change_password'),
                reverse('logout'),
                reverse('login'),
            }

        if (
            request.user.is_authenticated
            and getattr(request.user, 'must_change_password', False)
            and request.path not in self.EXEMPT_URLS
            and not request.path.startswith('/admin/')
            and not request.path.startswith('/static/')
        ):
            return redirect('change_password')

        return self.get_response(request)
