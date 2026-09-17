"""
Standalone configuration validation.

    python -m app.cli.validate_config

Wraps the same ``validate_configuration()`` the application runs during its
lifespan, so the container entrypoint and CI check exactly what the running
service checks. Exits non-zero when configuration is invalid for the current
ENVIRONMENT, which makes a misconfigured production container fail fast and
visibly instead of starting and serving broken requests.
"""

import sys

from app.core.config import settings, validate_configuration


def main() -> int:
    report = validate_configuration(settings)
    errors = report["errors"]
    warnings = report["warnings"]

    print(f"Environment : {settings.ENVIRONMENT}")
    print(f"Application : {settings.APP_NAME} v{settings.APP_VERSION}")
    print("-" * 60)

    for warning in warnings:
        print(f"  [WARN ] {warning}")
    for error in errors:
        print(f"  [ERROR] {error}")

    if not warnings and not errors:
        print("  All configuration checks passed.")

    print("-" * 60)
    if errors:
        print(f"FAILED: {len(errors)} error(s), {len(warnings)} warning(s).")
        return 1

    print(f"OK: 0 errors, {len(warnings)} warning(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
