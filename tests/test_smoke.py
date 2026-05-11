"""Smoke test — keeps pytest's collection non-empty and verifies that the
top-level packages import without side-effect failures."""


def test_packages_import() -> None:
    import analytics  # noqa: F401
    import api  # noqa: F401
    import collectors  # noqa: F401
    import db.connection  # noqa: F401
