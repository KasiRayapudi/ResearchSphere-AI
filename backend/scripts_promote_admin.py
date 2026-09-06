"""Promote a user to the admin role.

Admin endpoints require role="admin", but signup always creates "member".
Run this once to grant yourself access:

    python scripts_promote_admin.py you@example.com
"""

import sys

from app.core.database import SessionLocal
from app.models.user import User


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    email = sys.argv[1].strip().lower()
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email.ilike(email)).first()
        if not user:
            print(f"No user found with email {email!r}")
            return 1
        if user.role == "admin":
            print(f"{user.email} is already an admin.")
            return 0
        previous = user.role
        user.role = "admin"
        db.commit()
        print(f"Promoted {user.email}: {previous} -> admin")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
