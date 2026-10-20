"""Comprehensive Pytest test suite for execute_action in agent/sre_agent.py.

Simulates Docker daemon interactions using unittest.mock to enable
offline testing in CI/CD environments without a running Docker engine.
"""

from typing import Any, Dict
from unittest.mock import MagicMock, patch

import docker
import docker.errors
import pytest

from agent.sre_agent import execute_action


@pytest.fixture
def mock_docker_environment():
    """Pytest fixture providing mocked docker.from_env(), client, and container instances."""
    with patch("agent.sre_agent.docker.from_env") as mock_from_env:
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_container.status = "running"
        mock_client.containers.get.return_value = mock_container
        mock_from_env.return_value = mock_client

        yield {
            "from_env": mock_from_env,
            "client": mock_client,
            "container": mock_container,
        }


class TestExecuteActionNode:
    """Test suite verifying Docker container lifecycle and shell execution in execute_action."""

    def test_successful_restart(self, mock_docker_environment: Dict[str, Any]):
        """Scenario 1: Verify RESTART command restarts and reloads container successfully."""
        mock_container = mock_docker_environment["container"]
        mock_client = mock_docker_environment["client"]

        state = {
            "proposed_action": "RESTART dummy_prod_api",
            "service_name": "dummy_prod_api"
        }

        result = execute_action(state)

        # Verify container resolution
        mock_client.containers.get.assert_called_once_with("dummy_prod_api")

        # Verify Docker restart and reload invocation
        mock_container.restart.assert_called_once_with(timeout=10)
        mock_container.reload.assert_called_once()

        # Verify returned state payload
        assert "status" in result
        assert "action_executed" in result["status"]
        assert "dummy_prod_api" in result["status"]
        assert "running" in result["status"]

    def test_successful_exec(self, mock_docker_environment: Dict[str, Any]):
        """Scenario 2: Verify EXEC command executes shell command inside container and returns output."""
        mock_container = mock_docker_environment["container"]
        mock_client = mock_docker_environment["client"]

        # Mock exec_run returning exit code 0 and byte output
        mock_container.exec_run.return_value = (0, b"Cache cleared successfully\n")

        state = {
            "proposed_action": "EXEC dummy_prod_api rm -rf /tmp/cache",
            "service_name": "dummy_prod_api"
        }

        result = execute_action(state)

        # Verify container was retrieved and command executed
        mock_client.containers.get.assert_called_once_with("dummy_prod_api")
        mock_container.exec_run.assert_called_once_with("rm -rf /tmp/cache")

        # EXEC should not trigger container.reload()
        mock_container.reload.assert_not_called()

        # Verify successful execution status and decoded output
        assert "status" in result
        assert "action_executed" in result["status"]
        assert "EXEC command successful" in result["status"]
        assert "Cache cleared successfully" in result["status"]

    def test_failed_exec(self, mock_docker_environment: Dict[str, Any]):
        """Scenario 3: Verify non-zero exit code from exec_run returns action_failed without crashing."""
        mock_container = mock_docker_environment["container"]
        mock_client = mock_docker_environment["client"]

        # Mock exec_run returning failure exit code 1 with stderr
        mock_container.exec_run.return_value = (1, b"rm: cannot remove '/tmp/cache': Permission denied\n")

        state = {
            "proposed_action": "EXEC dummy_prod_api rm -rf /tmp/cache",
            "service_name": "dummy_prod_api"
        }

        result = execute_action(state)

        mock_client.containers.get.assert_called_once_with("dummy_prod_api")
        mock_container.exec_run.assert_called_once_with("rm -rf /tmp/cache")

        # Verify failure status and diagnostic information
        assert "status" in result
        assert "action_failed" in result["status"]
        assert "exit code 1" in result["status"]
        assert "Permission denied" in result["status"]

    def test_unsupported_command(self, mock_docker_environment: Dict[str, Any]):
        """Scenario 4: Verify unknown commands are rejected without calling Docker mutation methods."""
        mock_container = mock_docker_environment["container"]
        mock_client = mock_docker_environment["client"]

        state = {
            "proposed_action": "MAGIC dummy_prod_api",
            "service_name": "dummy_prod_api"
        }

        result = execute_action(state)

        # Target container was resolved
        mock_client.containers.get.assert_called_once_with("dummy_prod_api")

        # Verify that no action/mutation was performed on the container
        mock_container.restart.assert_not_called()
        mock_container.stop.assert_not_called()
        mock_container.pause.assert_not_called()
        mock_container.unpause.assert_not_called()
        mock_container.exec_run.assert_not_called()
        mock_container.reload.assert_not_called()

        # Verify rejection status
        assert "status" in result
        assert "action_failed" in result["status"]
        assert "MAGIC" in result["status"]
        assert "not supported" in result["status"]

    def test_container_not_found(self, mock_docker_environment: Dict[str, Any]):
        """Scenario 5: Verify NotFound exception from Docker SDK is caught gracefully."""
        mock_client = mock_docker_environment["client"]
        mock_container = mock_docker_environment["container"]

        # Simulate container not found in Docker daemon
        mock_client.containers.get.side_effect = docker.errors.NotFound("Container dummy_prod_api does not exist.")

        state = {
            "proposed_action": "RESTART dummy_prod_api",
            "service_name": "dummy_prod_api"
        }

        result = execute_action(state)

        # Mutation must not be called
        mock_container.restart.assert_not_called()

        # Verify error is returned in state dictionary
        assert "status" in result
        assert "action_failed" in result["status"]
        assert "not found" in result["status"].lower()

    @pytest.mark.parametrize(
        "action, method_name, expected_args",
        [
            ("STOP dummy_prod_api", "stop", {"timeout": 10}),
            ("PAUSE dummy_prod_api", "pause", {}),
            ("UNPAUSE dummy_prod_api", "unpause", {}),
        ],
    )
    def test_additional_lifecycle_commands(
        self,
        mock_docker_environment: Dict[str, Any],
        action: str,
        method_name: str,
        expected_args: Dict[str, Any],
    ):
        """Supplementary test: Verify STOP, PAUSE, and UNPAUSE commands."""
        mock_container = mock_docker_environment["container"]
        state = {"proposed_action": action, "service_name": "dummy_prod_api"}

        result = execute_action(state)

        method = getattr(mock_container, method_name)
        if expected_args:
            method.assert_called_once_with(**expected_args)
        else:
            method.assert_called_once()

        mock_container.reload.assert_called_once()
        assert "action_executed" in result["status"]

    def test_exec_missing_command_tokens(self, mock_docker_environment: Dict[str, Any]):
        """Supplementary test: Verify EXEC without shell command payload is handled safely."""
        mock_container = mock_docker_environment["container"]
        state = {"proposed_action": "EXEC dummy_prod_api", "service_name": "dummy_prod_api"}

        result = execute_action(state)

        mock_container.exec_run.assert_not_called()
        assert "action_failed" in result["status"]
        assert "EXEC command requires a shell command" in result["status"]
