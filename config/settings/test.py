import os
from pathlib import Path

import environ

environ.Env.read_env(Path(__file__).resolve().parent.parent.parent / ".env")
os.environ.setdefault("SECRET_KEY", "test-key")
os.environ.setdefault("DATABASE_URL", "postgres://depthub:depthub@localhost:5433/depthub")

from .base import *  # noqa: E402,F403

DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost"]
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
STORAGES = {
    **STORAGES,  # noqa: F405
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    # Uploaded test documents stay in memory and never touch the disk.
    "documents": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
}
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
SMS_BACKEND = "disabled"
PAYSTACK_SECRET_KEY = "sk_test_dummy"  # noqa: S105
PAYSTACK_PUBLIC_KEY = "pk_test_dummy"
