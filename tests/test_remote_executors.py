import io
from textwrap import dedent
from unittest.mock import MagicMock, patch

import docker
import PIL.Image
import pytest
from rich.console import Console

from smolagents.default_tools import WikipediaSearchTool
from smolagents.monitoring import AgentLogger, LogLevel
from smolagents.remote_executors import DockerExecutor, E2BExecutor, RemotePythonExecutor
from smolagents.utils import AgentError

from .utils.markers import require_run_all


class TestRemotePythonExecutor:
    def test_send_tools_empty_tools(self):
        executor = RemotePythonExecutor(additional_imports=[], logger=MagicMock())
        executor.run_code_raise_errors = MagicMock()
        executor.send_tools({})
        assert executor.run_code_raise_errors.call_count == 1
        # No new packages should be installed
        assert "!pip install" not in executor.run_code_raise_errors.call_args.args[0]

    @require_run_all
    def test_send_tools_with_default_wikipedia_search_tool(self):
        tool = WikipediaSearchTool()
        executor = RemotePythonExecutor(additional_imports=[], logger=MagicMock())
        executor.run_code_raise_errors = MagicMock()
        executor.send_tools({"wikipedia_search": tool})
        assert executor.run_code_raise_errors.call_count == 1
        assert "!pip install wikipedia-api" in executor.run_code_raise_errors.call_args.args[0]

    def test_multiline_final_answer(self):
        executor = RemotePythonExecutor(additional_imports=[], logger=MagicMock())
        code_action = dedent('''
            final_answer("""This is
            a multiline
            final answer""")
        ''')
        match = executor.final_answer_pattern.search(code_action)
        assert match is not None
        assert "This is\na multiline\nfinal answer" in match.group(1)


class TestE2BExecutorUnit:
    def test_e2b_executor_instantiation(self):
        logger = MagicMock()
        with patch("e2b_code_interpreter.Sandbox") as mock_sandbox:
            mock_sandbox.return_value.commands.run.return_value.error = None
            mock_sandbox.return_value.run_code.return_value.error = None
            executor = E2BExecutor(
                additional_imports=[], logger=logger, api_key="dummy-api-key", template="dummy-template-id", timeout=60
            )
        assert isinstance(executor, E2BExecutor)
        assert executor.logger == logger
        assert executor.final_answer_pattern.pattern == r"^final_answer\((.*)\)$"
        assert executor.sandbox == mock_sandbox.return_value
        assert mock_sandbox.call_count == 1
        assert mock_sandbox.call_args.kwargs == {
            "api_key": "dummy-api-key",
            "template": "dummy-template-id",
            "timeout": 60,
        }

    def test_cleanup(self):
        """Test that the cleanup method properly shuts down the sandbox"""
        logger = MagicMock()
        with patch("e2b_code_interpreter.Sandbox") as mock_sandbox:
            # Setup mock
            mock_sandbox.return_value.kill = MagicMock()

            # Create executor
            executor = E2BExecutor(additional_imports=[], logger=logger, api_key="dummy-api-key")

            # Call cleanup
            executor.cleanup()

            # Verify sandbox was killed
            mock_sandbox.return_value.kill.assert_called_once()
            assert logger.log.call_count >= 2  # Should log start and completion messages


@pytest.fixture
def docker_executor():
    executor = DockerExecutor(
        additional_imports=["pillow", "numpy"],
        logger=AgentLogger(LogLevel.INFO, Console(force_terminal=False, file=io.StringIO())),
    )
    yield executor
    executor.delete()


@require_run_all
class TestDockerExecutorIntegration:
    @pytest.fixture(autouse=True)
    def set_executor(self, docker_executor):
        self.executor = docker_executor

    def test_initialization(self):
        """Check if DockerExecutor initializes without errors"""
        assert self.executor.container is not None, "Container should be initialized"

    def test_state_persistence(self):
        """Test that variables and imports form one snippet persist in the next"""
        code_action = "import numpy as np; a = 2"
        self.executor(code_action)

        code_action = "print(np.sqrt(a))"
        result, logs, final_answer = self.executor(code_action)
        assert "1.41421" in logs

    def test_execute_output(self):
        """Test execution that returns a string"""
        code_action = 'final_answer("This is the final answer")'
        result, logs, final_answer = self.executor(code_action)
        assert result == "This is the final answer", "Result should be 'This is the final answer'"

    def test_execute_multiline_output(self):
        """Test execution that returns a string"""
        code_action = 'result = "This is the final answer"\nfinal_answer(result)'
        result, logs, final_answer = self.executor(code_action)
        assert result == "This is the final answer", "Result should be 'This is the final answer'"

    def test_execute_image_output(self):
        """Test execution that returns a base64 image"""
        code_action = dedent("""
            import base64
            from PIL import Image
            from io import BytesIO
            image = Image.new("RGB", (10, 10), (255, 0, 0))
            final_answer(image)
        """)
        result, logs, final_answer = self.executor(code_action)
        assert isinstance(result, PIL.Image.Image), "Result should be a PIL Image"

    def test_syntax_error_handling(self):
        """Test handling of syntax errors"""
        code_action = 'print("Missing Parenthesis'  # Syntax error
        with pytest.raises(AgentError) as exception_info:
            self.executor(code_action)
        assert "SyntaxError" in str(exception_info.value), "Should raise a syntax error"

    def test_cleanup_on_deletion(self):
        """Test if Docker container stops and removes on deletion"""
        container_id = self.executor.container.id
        self.executor.delete()  # Trigger cleanup

        client = docker.from_env()
        containers = [c.id for c in client.containers.list(all=True)]
        assert container_id not in containers, "Container should be removed"


class TestDockerExecutorUnit:
    def test_cleanup(self):
        """Test that cleanup properly stops and removes the container"""
        logger = MagicMock()
        with (
            patch("docker.from_env") as mock_docker_client,
            patch("requests.post") as mock_post,
            patch("websocket.create_connection"),
        ):
            # Setup mocks
            mock_container = MagicMock()
            mock_container.status = "running"
            mock_container.short_id = "test123"

            mock_docker_client.return_value.containers.run.return_value = mock_container
            mock_docker_client.return_value.images.get.return_value = MagicMock()

            mock_post.return_value.status_code = 201
            mock_post.return_value.json.return_value = {"id": "test-kernel-id"}

            # Create executor
            executor = DockerExecutor(additional_imports=[], logger=logger, build_new_image=False)

            # Call cleanup
            executor.cleanup()

            # Verify container was stopped and removed
            mock_container.stop.assert_called_once()
            mock_container.remove.assert_called_once()
