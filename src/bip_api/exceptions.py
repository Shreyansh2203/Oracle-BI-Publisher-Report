from __future__ import annotations


class BIPError(Exception):
    pass


class AuthError(BIPError):
    pass


class ReportError(BIPError):
    pass
