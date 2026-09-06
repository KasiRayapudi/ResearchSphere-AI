from app.core.database import SessionLocal, Base, engine
from app.models.user import User
from app.models.workspace import Workspace
from app.core.security import hash_password

def seed():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    # Seed User
    user = db.query(User).filter_by(email="alex.vance@enterprise-ai.io").first()
    if not user:
        user = User(
            full_name="Alex Vance",
            email="alex.vance@enterprise-ai.io",
            hashed_password=hash_password("password123"),
            role="admin",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        print(f"Seeded user: {user.email}")

    # Seed Workspace
    ws = db.query(Workspace).filter_by(owner_id=user.id).first()
    if not ws:
        ws = Workspace(
            name="Enterprise Research Hub",
            description="Main research workspace for enterprise data RAG and agents.",
            owner_id=user.id,
        )
        db.add(ws)
        db.commit()
        print(f"Seeded workspace: {ws.name}")

    db.close()
    print("Database schema created and initial enterprise data seeded successfully!")

if __name__ == "__main__":
    seed()
