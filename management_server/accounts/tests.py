import os
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse

User = get_user_model()


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------
class CustomUserModelTests(TestCase):
    """Tests for the CustomUser model."""

    def test_create_user_default_role(self):
        user = User.objects.create_user(username="alice", password="testpass123")
        self.assertEqual(user.role, "client")

    def test_create_user_default_must_change_password(self):
        user = User.objects.create_user(username="alice", password="testpass123")
        self.assertTrue(user.must_change_password)

    def test_create_user_with_admin_role(self):
        user = User.objects.create_user(
            username="bob", password="testpass123", role="admin"
        )
        self.assertEqual(user.role, "admin")

    def test_create_superuser(self):
        user = User.objects.create_superuser(
            username="superadmin", password="testpass123", role="admin"
        )
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.is_staff)
        self.assertEqual(user.role, "admin")


# ---------------------------------------------------------------------------
# Login view tests
# ---------------------------------------------------------------------------
class LoginViewTests(TestCase):
    """Tests for the login view."""

    def setUp(self):
        self.client = Client()
        self.url = reverse("login")
        self.user = User.objects.create_user(
            username="testuser",
            password="goodpass123",
            must_change_password=False,
        )

    def test_get_shows_form(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("form", response.context)

    def test_post_valid_credentials_redirects_to_home(self):
        response = self.client.post(
            self.url, {"username": "testuser", "password": "goodpass123"}
        )
        self.assertRedirects(response, reverse("home"))

    def test_post_invalid_credentials_shows_error(self):
        response = self.client.post(
            self.url, {"username": "testuser", "password": "wrongpass"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("form", response.context)
        self.assertTrue(response.context["form"].errors)

    def test_post_redirects_to_change_password_when_must_change(self):
        self.user.must_change_password = True
        self.user.save()
        response = self.client.post(
            self.url, {"username": "testuser", "password": "goodpass123"}
        )
        self.assertRedirects(response, reverse("change_password"))


# ---------------------------------------------------------------------------
# Logout view tests
# ---------------------------------------------------------------------------
class LogoutViewTests(TestCase):
    """Tests for the logout view."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username="testuser", password="goodpass123"
        )
        self.client.login(username="testuser", password="goodpass123")

    def test_logout_redirects_to_login(self):
        response = self.client.get(reverse("logout"))
        self.assertRedirects(response, reverse("login"))


# ---------------------------------------------------------------------------
# Force change password view tests
# ---------------------------------------------------------------------------
class ForceChangePasswordViewTests(TestCase):
    """Tests for the change_password view."""

    def setUp(self):
        self.client = Client()
        self.url = reverse("change_password")
        self.user = User.objects.create_user(
            username="testuser",
            password="oldpass1234",
            must_change_password=True,
        )
        self.client.login(username="testuser", password="oldpass1234")

    def test_get_shows_form(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("form", response.context)

    def test_wrong_old_password_shows_error(self):
        response = self.client.post(self.url, {
            "old_password": "wrongold",
            "new_password": "brandnew123",
            "confirm_password": "brandnew123",
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("old_password", response.context["form"].errors)

    def test_mismatched_new_passwords_shows_error(self):
        response = self.client.post(self.url, {
            "old_password": "oldpass1234",
            "new_password": "brandnew123",
            "confirm_password": "different456",
        })
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertTrue(form.errors)

    def test_valid_change_updates_password_and_redirects(self):
        response = self.client.post(self.url, {
            "old_password": "oldpass1234",
            "new_password": "Str0ngN3wPa$$",
            "confirm_password": "Str0ngN3wPa$$",
        })
        self.assertRedirects(response, reverse("home"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("Str0ngN3wPa$$"))
        self.assertFalse(self.user.must_change_password)

    def test_requires_login(self):
        self.client.logout()
        response = self.client.get(self.url)
        self.assertRedirects(response, f"{reverse('login')}?next={self.url}")


# ---------------------------------------------------------------------------
# Manage users view tests
# ---------------------------------------------------------------------------
class ManageUsersViewTests(TestCase):
    """Tests for the manage_users view."""

    def setUp(self):
        self.client = Client()
        self.url = reverse("manage_users")
        self.admin = User.objects.create_user(
            username="adminuser",
            password="adminpass123",
            role="admin",
            must_change_password=False,
        )
        self.normal = User.objects.create_user(
            username="clientuser",
            password="clientpass123",
            role="client",
            must_change_password=False,
        )

    def test_client_redirected_to_home(self):
        self.client.login(username="clientuser", password="clientpass123")
        response = self.client.get(self.url)
        self.assertRedirects(response, reverse("home"))

    def test_admin_can_see_user_list(self):
        self.client.login(username="adminuser", password="adminpass123")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("users", response.context)
        self.assertGreaterEqual(response.context["users"].count(), 2)

    def test_admin_can_create_user(self):
        self.client.login(username="adminuser", password="adminpass123")
        response = self.client.post(self.url, {
            "username": "newuser",
            "role": "client",
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(User.objects.filter(username="newuser").exists())
        new_user = User.objects.get(username="newuser")
        self.assertEqual(new_user.role, "client")
        self.assertTrue(new_user.must_change_password)
        self.assertIsNotNone(response.context["generated_password"])

    def test_requires_login(self):
        response = self.client.get(self.url)
        self.assertRedirects(response, f"{reverse('login')}?next={self.url}")


# ---------------------------------------------------------------------------
# Toggle role view tests
# ---------------------------------------------------------------------------
class ToggleRoleViewTests(TestCase):
    """Tests for the toggle_role view."""

    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_user(
            username="adminuser",
            password="adminpass123",
            role="admin",
            must_change_password=False,
        )
        self.target = User.objects.create_user(
            username="target",
            password="targetpass123",
            role="client",
            must_change_password=False,
        )
        self.url = reverse("toggle_role", args=[self.target.pk])

    def test_toggle_client_to_admin(self):
        self.client.login(username="adminuser", password="adminpass123")
        response = self.client.post(self.url)
        self.assertRedirects(response, reverse("manage_users"))
        self.target.refresh_from_db()
        self.assertEqual(self.target.role, "admin")

    def test_toggle_admin_to_client(self):
        self.target.role = "admin"
        self.target.save()
        self.client.login(username="adminuser", password="adminpass123")
        self.client.post(self.url)
        self.target.refresh_from_db()
        self.assertEqual(self.target.role, "client")

    def test_requires_post(self):
        self.client.login(username="adminuser", password="adminpass123")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)

    def test_non_admin_redirected_to_home(self):
        non_admin = User.objects.create_user(
            username="regularuser",
            password="regpass123",
            role="client",
            must_change_password=False,
        )
        self.client.login(username="regularuser", password="regpass123")
        response = self.client.post(self.url)
        self.assertRedirects(response, reverse("home"))
        self.target.refresh_from_db()
        self.assertEqual(self.target.role, "client")


# ---------------------------------------------------------------------------
# create_initial_admin management command tests
# ---------------------------------------------------------------------------
class CreateInitialAdminCommandTests(TestCase):
    """Tests for the create_initial_admin management command."""

    @patch.dict(os.environ, {"DJANGO_ADMIN_PASSWORD": "securepass123"})
    def test_creates_admin_from_env_var(self):
        call_command("create_initial_admin")
        self.assertTrue(User.objects.filter(username="admin").exists())
        admin = User.objects.get(username="admin")
        self.assertEqual(admin.role, "admin")
        self.assertTrue(admin.is_superuser)
        self.assertTrue(admin.check_password("securepass123"))

    @patch.dict(os.environ, {"DJANGO_ADMIN_PASSWORD": "securepass123"})
    def test_skips_if_admin_exists(self):
        User.objects.create_superuser(
            username="admin", password="existing", role="admin"
        )
        call_command("create_initial_admin")
        self.assertEqual(User.objects.filter(username="admin").count(), 1)

    @patch.dict(os.environ, {}, clear=True)
    def test_fails_without_env_var(self):
        # Ensure DJANGO_ADMIN_PASSWORD is not set
        os.environ.pop("DJANGO_ADMIN_PASSWORD", None)
        call_command("create_initial_admin")
        self.assertFalse(User.objects.filter(username="admin").exists())
