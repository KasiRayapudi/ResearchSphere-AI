"""
Workspace collaboration: membership, roles, invitations and isolation.

The permission matrix is exercised through the HTTP API rather than by
calling ``can()``, because what matters is whether a viewer can actually
upload a document, not whether a dictionary says they cannot.
"""

from datetime import datetime, timedelta

import pytest

from app.models.membership import WorkspaceRole
from tests.conftest import STRONG_PASSWORD

pytestmark = pytest.mark.integration


# ------------------------------------------------------------- fixtures --
def _account(client, label):
    """A fresh account, returning its headers, id, email and workspace."""
    import uuid

    email = f"{label}_{uuid.uuid4().hex[:8]}@example.com"
    response = client.post(
        "/api/v1/auth/signup",
        json={"name": label.title(), "email": email, "password": STRONG_PASSWORD},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    workspaces = client.get("/api/v1/workspaces", headers=headers).json()
    return {
        "headers": headers,
        "id": body["user"]["id"],
        "email": email,
        "workspace_id": workspaces[0]["id"],
    }


def _add_member(client, owner, invitee, role):
    """Invite and accept, returning the invitee's member id."""
    invite = client.post(
        "/api/v1/workspace/invite",
        headers=owner["headers"],
        json={"email": invitee["email"], "role": role, "workspace_id": owner["workspace_id"]},
    )
    assert invite.status_code == 200, invite.text

    token = _token_for(invite.json()["id"])
    accepted = client.post(
        "/api/v1/workspace/invite/accept", headers=invitee["headers"], json={"token": token}
    )
    assert accepted.status_code == 200, accepted.text

    members = client.get(
        f"/api/v1/workspace/members?workspace_id={owner['workspace_id']}",
        headers=owner["headers"],
    ).json()
    return next(m["id"] for m in members if m["userId"] == invitee["id"])


def _token_for(invitation_id):
    """Recover the raw token for an invitation.

    Only the hash is stored, which is the point, so the raw value is
    regenerated the same way the endpoint would deliver it: by replacing the
    stored hash with the hash of a known token. That keeps the test honest
    about the storage guarantee instead of weakening it.
    """
    import secrets

    from app.core.database import SessionLocal
    from app.core.invitations import hash_token
    from app.models.membership import WorkspaceInvitation

    raw = secrets.token_urlsafe(32)
    session = SessionLocal()
    try:
        invitation = session.get(WorkspaceInvitation, invitation_id)
        invitation.token_hash = hash_token(raw)
        session.commit()
    finally:
        session.close()
    return raw


@pytest.fixture
def owner(client):
    return _account(client, "owner")


@pytest.fixture
def outsider(client):
    return _account(client, "outsider")


# ------------------------------------------------------------ membership --
class TestMembership:
    def test_signup_makes_the_user_the_owner_of_their_workspace(self, client, owner):
        members = client.get(
            f"/api/v1/workspace/members?workspace_id={owner['workspace_id']}",
            headers=owner["headers"],
        ).json()
        assert len(members) == 1
        assert members[0]["userId"] == owner["id"]
        assert members[0]["role"] == WorkspaceRole.OWNER

    def test_member_count_is_real(self, client, owner):
        """Regression: memberCount was the literal 1."""
        editor = _account(client, "editor")
        _add_member(client, owner, editor, WorkspaceRole.EDITOR)

        workspaces = client.get("/api/v1/workspaces", headers=owner["headers"]).json()
        mine = next(w for w in workspaces if w["id"] == owner["workspace_id"])
        assert mine["memberCount"] == 2

    def test_a_shared_workspace_appears_for_the_invitee(self, client, owner):
        viewer = _account(client, "viewer")
        _add_member(client, owner, viewer, WorkspaceRole.VIEWER)

        workspaces = client.get("/api/v1/workspaces", headers=viewer["headers"]).json()
        shared = [w for w in workspaces if w["id"] == owner["workspace_id"]]
        assert shared, "the shared workspace is not listed for the member"
        # And with the role they were actually given, not a hardcoded one.
        assert shared[0]["role"] == WorkspaceRole.VIEWER

    def test_creating_a_workspace_creates_its_owner_membership(self, client, owner):
        created = client.post(
            "/api/v1/workspaces", headers=owner["headers"], json={"name": "Second"}
        )
        assert created.status_code == 200
        workspace_id = created.json()["id"]

        members = client.get(
            f"/api/v1/workspace/members?workspace_id={workspace_id}", headers=owner["headers"]
        ).json()
        assert [m["role"] for m in members] == [WorkspaceRole.OWNER]


# -------------------------------------------------------- the permission --
#: (role, method, path-builder, expected status). Written as data so the
#: matrix is readable as a matrix.
class TestPermissionMatrix:
    @pytest.fixture
    def workspace_with_roles(self, client, owner):
        people = {"owner": owner}
        for role in (WorkspaceRole.ADMIN, WorkspaceRole.EDITOR, WorkspaceRole.VIEWER):
            person = _account(client, role)
            person["member_id"] = _add_member(client, owner, person, role)
            people[role] = person
        return people

    def _upload(self, client, person, workspace_id):
        return client.post(
            "/api/v1/documents/upload",
            headers=person["headers"],
            files={"file": ("m.txt", b"matrix corpus " * 40, "text/plain")},
            data={"workspace_id": workspace_id},
        )

    @pytest.mark.parametrize(
        "role,allowed",
        [
            (WorkspaceRole.OWNER, True),
            (WorkspaceRole.ADMIN, True),
            (WorkspaceRole.EDITOR, True),
            (WorkspaceRole.VIEWER, False),
        ],
    )
    def test_upload_requires_write(self, client, workspace_with_roles, role, allowed, indexing):
        person = workspace_with_roles[role]
        response = self._upload(client, person, workspace_with_roles["owner"]["workspace_id"])
        if allowed:
            assert response.status_code == 200, response.text
        else:
            assert response.status_code == 403

    @pytest.mark.parametrize(
        "role,allowed",
        [
            (WorkspaceRole.OWNER, True),
            (WorkspaceRole.ADMIN, True),
            (WorkspaceRole.EDITOR, True),
            (WorkspaceRole.VIEWER, False),
        ],
    )
    def test_chat_requires_write(self, client, workspace_with_roles, role, allowed):
        workspace_id = workspace_with_roles["owner"]["workspace_id"]
        response = client.post(
            f"/api/v1/chat/sessions?workspace_id={workspace_id}",
            headers=workspace_with_roles[role]["headers"],
        )
        assert (response.status_code == 200) is allowed, response.text

    @pytest.mark.parametrize(
        "role,allowed",
        [
            (WorkspaceRole.OWNER, True),
            (WorkspaceRole.ADMIN, True),
            (WorkspaceRole.EDITOR, False),
            (WorkspaceRole.VIEWER, False),
        ],
    )
    def test_inviting_requires_admin(self, client, workspace_with_roles, role, allowed):
        import uuid

        response = client.post(
            "/api/v1/workspace/invite",
            headers=workspace_with_roles[role]["headers"],
            json={
                "email": f"new_{uuid.uuid4().hex[:8]}@example.com",
                "role": WorkspaceRole.VIEWER,
                "workspace_id": workspace_with_roles["owner"]["workspace_id"],
            },
        )
        assert (response.status_code == 200) is allowed, response.text

    @pytest.mark.parametrize(
        "role",
        [WorkspaceRole.OWNER, WorkspaceRole.ADMIN, WorkspaceRole.EDITOR, WorkspaceRole.VIEWER],
    )
    def test_every_role_can_read(self, client, workspace_with_roles, role):
        workspace_id = workspace_with_roles["owner"]["workspace_id"]
        headers = workspace_with_roles[role]["headers"]
        assert (
            client.get(
                f"/api/v1/documents?workspace_id={workspace_id}", headers=headers
            ).status_code
            == 200
        )
        assert (
            client.get(
                f"/api/v1/workspace/members?workspace_id={workspace_id}", headers=headers
            ).status_code
            == 200
        )

    def test_a_viewer_cannot_generate_a_report(self, client, workspace_with_roles):
        response = client.post(
            "/api/v1/reports/generate",
            headers=workspace_with_roles[WorkspaceRole.VIEWER]["headers"],
            json={
                "title": "T",
                "objective": "o",
                "workspace_id": workspace_with_roles["owner"]["workspace_id"],
            },
        )
        assert response.status_code == 403

    def test_a_viewer_cannot_start_research(self, client, workspace_with_roles):
        response = client.post(
            "/api/v1/research/start",
            headers=workspace_with_roles[WorkspaceRole.VIEWER]["headers"],
            json={
                "title": "T",
                "objective": "o",
                "workspace_id": workspace_with_roles["owner"]["workspace_id"],
            },
        )
        assert response.status_code == 403


# ------------------------------------------------------------- isolation --
class TestCrossWorkspaceIsolation:
    def test_an_outsider_cannot_read_documents(self, client, owner, outsider):
        response = client.get(
            f"/api/v1/documents?workspace_id={owner['workspace_id']}",
            headers=outsider["headers"],
        )
        # 404 rather than 403: workspace ids must not be probeable.
        assert response.status_code == 404

    def test_an_outsider_cannot_list_members(self, client, owner, outsider):
        response = client.get(
            f"/api/v1/workspace/members?workspace_id={owner['workspace_id']}",
            headers=outsider["headers"],
        )
        assert response.status_code == 404

    def test_an_outsider_cannot_invite(self, client, owner, outsider):
        response = client.post(
            "/api/v1/workspace/invite",
            headers=outsider["headers"],
            json={
                "email": "someone@example.com",
                "role": WorkspaceRole.VIEWER,
                "workspace_id": owner["workspace_id"],
            },
        )
        assert response.status_code == 404

    def test_a_removed_member_loses_access_immediately(self, client, owner):
        """The role is read per request, so a removal cannot be outlived."""
        editor = _account(client, "editor")
        member_id = _add_member(client, owner, editor, WorkspaceRole.EDITOR)

        before = client.get(
            f"/api/v1/documents?workspace_id={owner['workspace_id']}", headers=editor["headers"]
        )
        assert before.status_code == 200

        removed = client.delete(
            f"/api/v1/workspace/members/{member_id}?workspace_id={owner['workspace_id']}",
            headers=owner["headers"],
        )
        assert removed.status_code == 200

        # Same token, still valid, but the membership is gone.
        after = client.get(
            f"/api/v1/documents?workspace_id={owner['workspace_id']}", headers=editor["headers"]
        )
        assert after.status_code == 404


# ----------------------------------------------------------- invitations --
class TestInvitations:
    def test_an_invitation_is_created_as_pending(self, client, owner, outsider):
        response = client.post(
            "/api/v1/workspace/invite",
            headers=owner["headers"],
            json={
                "email": outsider["email"],
                "role": WorkspaceRole.EDITOR,
                "workspace_id": owner["workspace_id"],
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "pending"
        assert body["email"] == outsider["email"]
        assert body["role"] == WorkspaceRole.EDITOR

    def test_the_raw_token_is_never_stored(self, client, owner, outsider):
        response = client.post(
            "/api/v1/workspace/invite",
            headers=owner["headers"],
            json={"email": outsider["email"], "workspace_id": owner["workspace_id"]},
        )
        # The response must not leak it either.
        assert "token" not in response.text.lower()

        from app.core.database import SessionLocal
        from app.models.membership import WorkspaceInvitation

        session = SessionLocal()
        try:
            invitation = session.get(WorkspaceInvitation, response.json()["id"])
            assert len(invitation.token_hash) == 64  # sha-256 hex
        finally:
            session.close()

    def test_accepting_grants_the_invited_role(self, client, owner, outsider):
        _add_member(client, owner, outsider, WorkspaceRole.EDITOR)
        workspaces = client.get("/api/v1/workspaces", headers=outsider["headers"]).json()
        shared = next(w for w in workspaces if w["id"] == owner["workspace_id"])
        assert shared["role"] == WorkspaceRole.EDITOR

    def test_a_token_cannot_be_used_twice(self, client, owner, outsider):
        invite = client.post(
            "/api/v1/workspace/invite",
            headers=owner["headers"],
            json={"email": outsider["email"], "workspace_id": owner["workspace_id"]},
        )
        token = _token_for(invite.json()["id"])

        first = client.post(
            "/api/v1/workspace/invite/accept", headers=outsider["headers"], json={"token": token}
        )
        assert first.status_code == 200
        second = client.post(
            "/api/v1/workspace/invite/accept", headers=outsider["headers"], json={"token": token}
        )
        assert second.status_code == 400

    def test_an_expired_invitation_is_refused(self, client, owner, outsider):
        invite = client.post(
            "/api/v1/workspace/invite",
            headers=owner["headers"],
            json={"email": outsider["email"], "workspace_id": owner["workspace_id"]},
        )
        token = _token_for(invite.json()["id"])

        from app.core.database import SessionLocal
        from app.models.membership import WorkspaceInvitation

        session = SessionLocal()
        try:
            invitation = session.get(WorkspaceInvitation, invite.json()["id"])
            invitation.expires_at = datetime.utcnow() - timedelta(hours=1)
            session.commit()
        finally:
            session.close()

        response = client.post(
            "/api/v1/workspace/invite/accept", headers=outsider["headers"], json={"token": token}
        )
        assert response.status_code == 400

    def test_an_invitation_cannot_be_used_by_a_different_address(self, client, owner, outsider):
        """A leaked token must not let anyone join."""
        invite = client.post(
            "/api/v1/workspace/invite",
            headers=owner["headers"],
            json={"email": "intended@example.com", "workspace_id": owner["workspace_id"]},
        )
        token = _token_for(invite.json()["id"])

        response = client.post(
            "/api/v1/workspace/invite/accept", headers=outsider["headers"], json={"token": token}
        )
        assert response.status_code == 403

    def test_an_unknown_token_is_refused(self, client, outsider):
        response = client.post(
            "/api/v1/workspace/invite/accept",
            headers=outsider["headers"],
            json={"token": "not-a-real-token"},
        )
        assert response.status_code == 400

    def test_a_duplicate_invitation_is_refused(self, client, owner, outsider):
        payload = {"email": outsider["email"], "workspace_id": owner["workspace_id"]}
        assert (
            client.post(
                "/api/v1/workspace/invite", headers=owner["headers"], json=payload
            ).status_code
            == 200
        )
        second = client.post("/api/v1/workspace/invite", headers=owner["headers"], json=payload)
        # Two live tokens would mean revoking one leaves the other working.
        assert second.status_code == 409

    def test_inviting_an_existing_member_is_refused(self, client, owner):
        editor = _account(client, "editor")
        _add_member(client, owner, editor, WorkspaceRole.EDITOR)

        response = client.post(
            "/api/v1/workspace/invite",
            headers=owner["headers"],
            json={"email": editor["email"], "workspace_id": owner["workspace_id"]},
        )
        assert response.status_code == 409

    def test_revoking_prevents_acceptance(self, client, owner, outsider):
        invite = client.post(
            "/api/v1/workspace/invite",
            headers=owner["headers"],
            json={"email": outsider["email"], "workspace_id": owner["workspace_id"]},
        )
        token = _token_for(invite.json()["id"])

        revoked = client.post(
            "/api/v1/workspace/invite/revoke",
            headers=owner["headers"],
            json={"invitation_id": invite.json()["id"], "workspace_id": owner["workspace_id"]},
        )
        assert revoked.status_code == 200

        response = client.post(
            "/api/v1/workspace/invite/accept", headers=outsider["headers"], json={"token": token}
        )
        assert response.status_code == 400

    def test_resending_invalidates_the_previous_token(self, client, owner, outsider):
        invite = client.post(
            "/api/v1/workspace/invite",
            headers=owner["headers"],
            json={"email": outsider["email"], "workspace_id": owner["workspace_id"]},
        )
        old_token = _token_for(invite.json()["id"])

        resent = client.post(
            "/api/v1/workspace/invite/resend",
            headers=owner["headers"],
            json={"invitation_id": invite.json()["id"], "workspace_id": owner["workspace_id"]},
        )
        assert resent.status_code == 200
        assert resent.json()["id"] != invite.json()["id"]

        # The old token must be dead: the previous email may have gone astray.
        response = client.post(
            "/api/v1/workspace/invite/accept",
            headers=outsider["headers"],
            json={"token": old_token},
        )
        assert response.status_code == 400

    def test_owner_cannot_be_invited_as_a_role(self, client, owner, outsider):
        response = client.post(
            "/api/v1/workspace/invite",
            headers=owner["headers"],
            json={
                "email": outsider["email"],
                "role": "owner",
                "workspace_id": owner["workspace_id"],
            },
        )
        assert response.status_code == 400

    def test_an_unknown_role_is_refused(self, client, owner, outsider):
        response = client.post(
            "/api/v1/workspace/invite",
            headers=owner["headers"],
            json={
                "email": outsider["email"],
                "role": "superuser",
                "workspace_id": owner["workspace_id"],
            },
        )
        assert response.status_code == 400


# ------------------------------------------------------- member lifecycle --
class TestMemberManagement:
    def test_an_owner_can_change_a_role(self, client, owner):
        person = _account(client, "viewer")
        member_id = _add_member(client, owner, person, WorkspaceRole.VIEWER)

        response = client.patch(
            f"/api/v1/workspace/members/{member_id}",
            headers=owner["headers"],
            json={"role": WorkspaceRole.EDITOR, "workspace_id": owner["workspace_id"]},
        )
        assert response.status_code == 200
        assert response.json()["role"] == WorkspaceRole.EDITOR

    def test_the_owners_role_cannot_be_changed(self, client, owner):
        members = client.get(
            f"/api/v1/workspace/members?workspace_id={owner['workspace_id']}",
            headers=owner["headers"],
        ).json()
        owner_member = next(m for m in members if m["role"] == WorkspaceRole.OWNER)

        response = client.patch(
            f"/api/v1/workspace/members/{owner_member['id']}",
            headers=owner["headers"],
            json={"role": WorkspaceRole.EDITOR, "workspace_id": owner["workspace_id"]},
        )
        assert response.status_code == 400

    def test_the_owner_cannot_be_removed(self, client, owner):
        members = client.get(
            f"/api/v1/workspace/members?workspace_id={owner['workspace_id']}",
            headers=owner["headers"],
        ).json()
        owner_member = next(m for m in members if m["role"] == WorkspaceRole.OWNER)

        response = client.delete(
            f"/api/v1/workspace/members/{owner_member['id']}?workspace_id={owner['workspace_id']}",
            headers=owner["headers"],
        )
        # Otherwise the workspace is left with nobody able to administer it.
        assert response.status_code == 400

    def test_an_admin_cannot_demote_another_admin(self, client, owner):
        first = _account(client, "admin")
        second = _account(client, "admin")
        _add_member(client, owner, first, WorkspaceRole.ADMIN)
        second_id = _add_member(client, owner, second, WorkspaceRole.ADMIN)

        response = client.patch(
            f"/api/v1/workspace/members/{second_id}",
            headers=first["headers"],
            json={"role": WorkspaceRole.VIEWER, "workspace_id": owner["workspace_id"]},
        )
        # A peer must not be able to quietly take over.
        assert response.status_code == 403

    def test_a_member_can_remove_themselves(self, client, owner):
        person = _account(client, "editor")
        member_id = _add_member(client, owner, person, WorkspaceRole.EDITOR)

        response = client.delete(
            f"/api/v1/workspace/members/{member_id}?workspace_id={owner['workspace_id']}",
            headers=person["headers"],
        )
        assert response.status_code == 200

    def test_a_viewer_cannot_remove_someone_else(self, client, owner):
        viewer = _account(client, "viewer")
        editor = _account(client, "editor")
        _add_member(client, owner, viewer, WorkspaceRole.VIEWER)
        editor_id = _add_member(client, owner, editor, WorkspaceRole.EDITOR)

        response = client.delete(
            f"/api/v1/workspace/members/{editor_id}?workspace_id={owner['workspace_id']}",
            headers=viewer["headers"],
        )
        assert response.status_code == 403


class TestOwnershipTransfer:
    def test_ownership_moves_and_the_previous_owner_becomes_admin(self, client, owner):
        successor = _account(client, "admin")
        member_id = _add_member(client, owner, successor, WorkspaceRole.ADMIN)

        response = client.post(
            f"/api/v1/workspace/transfer-ownership?member_id={member_id}",
            headers=owner["headers"],
            json={"role": WorkspaceRole.OWNER, "workspace_id": owner["workspace_id"]},
        )
        assert response.status_code == 200, response.text

        roles = {m["userId"]: m["role"] for m in response.json()}
        assert roles[successor["id"]] == WorkspaceRole.OWNER
        # The previous owner keeps access rather than being locked out.
        assert roles[owner["id"]] == WorkspaceRole.ADMIN

    def test_the_denormalised_owner_pointer_moves_too(self, client, owner):
        successor = _account(client, "admin")
        member_id = _add_member(client, owner, successor, WorkspaceRole.ADMIN)

        client.post(
            f"/api/v1/workspace/transfer-ownership?member_id={member_id}",
            headers=owner["headers"],
            json={"role": WorkspaceRole.OWNER, "workspace_id": owner["workspace_id"]},
        )

        from app.core.database import SessionLocal
        from app.models.workspace import Workspace

        session = SessionLocal()
        try:
            workspace = session.get(Workspace, owner["workspace_id"])
            # Drift here would make owner_id lie about who owns the workspace.
            assert workspace.owner_id == successor["id"]
        finally:
            session.close()

    def test_only_the_owner_can_transfer(self, client, owner):
        admin = _account(client, "admin")
        other = _account(client, "editor")
        _add_member(client, owner, admin, WorkspaceRole.ADMIN)
        other_id = _add_member(client, owner, other, WorkspaceRole.EDITOR)

        response = client.post(
            f"/api/v1/workspace/transfer-ownership?member_id={other_id}",
            headers=admin["headers"],
            json={"role": WorkspaceRole.OWNER, "workspace_id": owner["workspace_id"]},
        )
        assert response.status_code == 403

    def test_there_is_still_exactly_one_owner_afterwards(self, client, owner):
        successor = _account(client, "admin")
        member_id = _add_member(client, owner, successor, WorkspaceRole.ADMIN)

        client.post(
            f"/api/v1/workspace/transfer-ownership?member_id={member_id}",
            headers=owner["headers"],
            json={"role": WorkspaceRole.OWNER, "workspace_id": owner["workspace_id"]},
        )

        members = client.get(
            f"/api/v1/workspace/members?workspace_id={owner['workspace_id']}",
            headers=successor["headers"],
        ).json()
        owners = [m for m in members if m["role"] == WorkspaceRole.OWNER]
        assert len(owners) == 1
