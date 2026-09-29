"""Explicit access policy, independent of the persistence backend."""
import os


def mode():
    value = os.environ.get('APP_AUTH_MODE', 'password').strip().lower()
    if value not in {'password', 'public'}:
        raise RuntimeError('APP_AUTH_MODE должен быть password или public.')
    return value


def password():
    return '' if mode() == 'public' else os.environ.get('APP_PASSWORD', '')


def validate(cloud):
    # Public access is an explicit choice; a missing secret must not accidentally
    # expose an existing password-protected cloud installation.
    if cloud and mode() != 'public' and len(password()) < 12:
        raise RuntimeError('Задайте APP_PASSWORD длиной не менее 12 символов или APP_AUTH_MODE=public для общего доступа без пароля.')
    mode()
