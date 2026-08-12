def test_application_modules_import():
    import app.main  # noqa: F401
    import app.worker  # noqa: F401
    import app.db  # noqa: F401
