"""
Deployment contract: the production stack must be able to probe itself, and
its Qdrant configuration must agree with what the client actually sends.

Both defects covered here were configuration-only, and both survived every
existing test because nothing asserted the deployment files. They were found
only when CI first started the production Compose stack:

  * The container healthcheck probed 127.0.0.1, so it sent
    ``Host: 127.0.0.1:8000``. TrustedHostMiddleware fronts every route and
    answers 400 for a Host outside TRUSTED_HOSTS, which no real deployment
    sets to a loopback literal -- so the backend could never report healthy
    and nginx, which gates on it, never started. The suite missed it because
    conftest puts 127.0.0.1 in TRUSTED_HOSTS; production does not.

  * The Qdrant service was handed ``QDRANT__SERVICE__API_KEY`` with an empty
    value. Qdrant enables authentication whenever that setting is present at
    all, an empty string included, and then rejects every unauthenticated
    request with 403. Its ``/readyz`` endpoint is whitelisted, so the
    container still reported healthy while the backend could not query it.

These assertions read the deployment files directly. That is a layer above
what the rest of this suite touches, but it is the repository's only Python
test runner, and CI always runs it from a full checkout.
"""

import re
from pathlib import Path

import pytest
import yaml
from starlette.applications import Starlette
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.core.config import Settings

pytestmark = pytest.mark.unit

#: backend/tests/ -> backend/ -> repository root.
REPO_ROOT = Path(__file__).resolve().parents[2]
PROD_COMPOSE = REPO_ROOT / "docker-compose.prod.yml"
DEV_COMPOSE = REPO_ROOT / "docker-compose.yml"
BUILD_OVERLAY = REPO_ROOT / "docker-compose.prod.build.yml"
PROD_DOCKERFILE = REPO_ROOT / "backend" / "Dockerfile.prod"
ENV_TEMPLATE = REPO_ROOT / ".env.prod.example"

#: The readiness endpoint the production healthcheck is required to use.
READINESS_PATH = "/api/ready"


def _compose(path: Path) -> dict:
    assert path.is_file(), f"{path} is missing; the suite must run from a full checkout"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _healthcheck_command(service: dict) -> str:
    """The healthcheck as the container's shell will receive it.

    Compose turns a doubled ``$$`` into a single ``$`` before handing the
    string over, so undo that here to assert on the real command.
    """
    test = service["healthcheck"]["test"]
    assert test[0] == "CMD-SHELL", f"expected a shell healthcheck, got {test[0]!r}"
    return test[1].replace("$$", "$")


# ------------------------------------------------- the trusted-host contract --
class TestReadinessProbeHost:
    """The Host the probe sends must be one the application trusts."""

    @staticmethod
    def _app(trusted_hosts: list[str]) -> TestClient:
        app = Starlette(routes=[Route(READINESS_PATH, lambda request: PlainTextResponse("ready"))])
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)
        return TestClient(app)

    @pytest.mark.parametrize(
        "configured",
        ["app.example.com", "app.example.com,www.app.example.com", "localhost"],
    )
    def test_the_first_trusted_host_is_accepted(self, configured):
        """This is the value the healthcheck derives its Host header from."""
        settings = Settings(_env_file=None, TRUSTED_HOSTS=configured)
        probe_host = settings.trusted_hosts[0]
        client = self._app(settings.trusted_hosts)

        assert client.get(READINESS_PATH, headers={"Host": probe_host}).status_code == 200
        # The probe reaches the app on port 8000, so the Host carries a port.
        with_port = client.get(READINESS_PATH, headers={"Host": f"{probe_host}:8000"})
        assert with_port.status_code == 200

    @pytest.mark.parametrize("loopback", ["127.0.0.1", "127.0.0.1:8000"])
    def test_a_loopback_host_is_rejected_when_not_configured(self, loopback):
        """Why probing the URL directly failed: the Host is not in the list."""
        settings = Settings(_env_file=None, TRUSTED_HOSTS="app.example.com")
        client = self._app(settings.trusted_hosts)

        assert client.get(READINESS_PATH, headers={"Host": loopback}).status_code == 400


# ------------------------------------------------ the healthcheck as shipped --
class TestBackendHealthcheck:
    def test_it_probes_readiness_through_a_trusted_host(self):
        command = _healthcheck_command(_compose(PROD_COMPOSE)["services"]["backend"])

        assert READINESS_PATH in command, "the readiness endpoint must stay the probe"
        assert "TRUSTED_HOSTS" in command, (
            "the probe must derive its Host from TRUSTED_HOSTS; probing the URL "
            "alone sends a loopback Host that TrustedHostMiddleware answers 400"
        )
        assert re.search(r'--header\s+"Host: ', command), "no Host header is set"

    def test_the_image_healthcheck_matches(self):
        """A bare `docker run` of the image must be probeable too."""
        dockerfile = PROD_DOCKERFILE.read_text(encoding="utf-8")
        # Anchored to the start of a line: the word also appears in a comment
        # further up, explaining why curl is installed. Backslash
        # continuations are joined so the whole instruction is one string.
        joined = dockerfile.replace("\\\n", " ")
        instructions = [line for line in joined.splitlines() if line.startswith("HEALTHCHECK")]
        assert len(instructions) == 1, f"expected one HEALTHCHECK, found {len(instructions)}"
        healthcheck = instructions[0]

        assert READINESS_PATH in healthcheck
        assert "TRUSTED_HOSTS" in healthcheck
        assert "Host: " in healthcheck


# --------------------------------------------------------- the Qdrant contract --
class TestQdrantAuthentication:
    """Server-side and client-side Qdrant auth must agree, or nothing works."""

    def test_no_compose_file_configures_a_qdrant_api_key(self):
        for path in (PROD_COMPOSE, DEV_COMPOSE, BUILD_OVERLAY):
            text = path.read_text(encoding="utf-8")
            offenders = [
                line.strip()
                for line in text.splitlines()
                if "QDRANT__SERVICE__API_KEY" in line and not line.lstrip().startswith("#")
            ]
            assert not offenders, (
                f"{path.name} configures Qdrant authentication: {offenders}. Qdrant "
                "enforces it even for an empty value, and the backend sends no key, "
                "so every query would be answered 403."
            )

    def test_the_env_template_declares_no_qdrant_api_key(self):
        declared = [
            line
            for line in ENV_TEMPLATE.read_text(encoding="utf-8").splitlines()
            if re.match(r"^\s*QDRANT_API_KEY\s*=", line)
        ]
        assert not declared, f"the template still offers a key that cannot work: {declared}"

    def test_server_and_client_configuration_stay_consistent(self):
        """Either both sides know about a key, or neither does.

        Fails if a future change re-adds the Qdrant service setting without
        also giving the backend a key to send, which is the exact mismatch
        that produced 403 Invalid api-key.
        """
        server_side = "QDRANT__SERVICE__API_KEY" in PROD_COMPOSE.read_text(encoding="utf-8")
        client_side = hasattr(Settings(_env_file=None), "QDRANT_API_KEY")

        assert server_side == client_side, (
            "Qdrant authentication is configured on one side only "
            f"(service={server_side}, backend setting={client_side})"
        )
