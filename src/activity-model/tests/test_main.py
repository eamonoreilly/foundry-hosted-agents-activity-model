import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from starlette.testclient import TestClient


MODULE_PATH = Path(__file__).parents[1] / "main.py"
SPEC = importlib.util.spec_from_file_location("activity_model", MODULE_PATH)
sample = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sample
SPEC.loader.exec_module(sample)


class LocalEnvironmentTests(unittest.TestCase):
    def test_uses_env_file_next_to_main(self):
        with patch.object(sample, "load_dotenv") as dotenv_loader:
            sample.load_local_environment()

        dotenv_loader.assert_called_once_with(
            dotenv_path=MODULE_PATH.with_name(".env"),
            override=False,
        )

    def test_loads_values_from_env_file(self):
        with tempfile.TemporaryDirectory() as temp_directory:
            env_file = Path(temp_directory) / ".env"
            env_file.write_text(
                "ACTIVITY_MODEL_TEST_VALUE=from-file\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=True):
                sample.load_local_environment(env_file)
                self.assertEqual(
                    os.environ["ACTIVITY_MODEL_TEST_VALUE"],
                    "from-file",
                )

    def test_does_not_override_process_environment(self):
        with tempfile.TemporaryDirectory() as temp_directory:
            env_file = Path(temp_directory) / ".env"
            env_file.write_text(
                "ACTIVITY_MODEL_TEST_VALUE=from-file\n",
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {"ACTIVITY_MODEL_TEST_VALUE": "from-process"},
                clear=True,
            ):
                sample.load_local_environment(env_file)
                self.assertEqual(
                    os.environ["ACTIVITY_MODEL_TEST_VALUE"],
                    "from-process",
                )


class TelemetryConfigurationTests(unittest.TestCase):
    def test_content_recording_is_disabled_by_default(self):
        instrumentor = Mock()

        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                sample,
                "AIProjectInstrumentor",
                return_value=instrumentor,
            ),
        ):
            sample.configure_model_telemetry()

        instrumentor.instrument.assert_called_once_with(
            enable_content_recording=False
        )

    def test_content_recording_can_be_explicitly_enabled(self):
        instrumentor = Mock()

        with (
            patch.dict(
                os.environ,
                {"ENABLE_SENSITIVE_DATA": "true"},
                clear=True,
            ),
            patch.object(
                sample,
                "AIProjectInstrumentor",
                return_value=instrumentor,
            ),
        ):
            sample.configure_model_telemetry()

        instrumentor.instrument.assert_called_once_with(
            enable_content_recording=True
        )


class ActivityRouteTests(unittest.TestCase):
    def test_supports_canonical_and_playground_message_routes(self):
        activity = {
            "type": "message",
            "channelId": "emulator",
            "serviceUrl": "http://localhost",
            "from": {"id": "user-1", "name": "Local user"},
            "recipient": {"id": "activity-model", "name": "Activity model"},
            "conversation": {"id": "route-test"},
            "deliveryMode": "expectReplies",
            "text": " ",
        }

        with TestClient(sample.host) as client:
            canonical = client.post("/activity/messages", json=activity)
            playground = client.post("/api/messages", json=activity)

        self.assertEqual(canonical.status_code, 200, canonical.text)
        self.assertEqual(playground.status_code, 200, playground.text)
        self.assertEqual(playground.json(), canonical.json())


class ConversationMappingTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_turn_creates_and_stores_project_conversation(self):
        conversation_state = SimpleNamespace(
            get_value=Mock(return_value=None),
            set_value=Mock(),
        )
        state = SimpleNamespace(conversation=conversation_state)
        on_text_delta = Mock()

        with (
            patch.object(
                sample,
                "create_model_conversation",
                AsyncMock(return_value="conv_new"),
            ) as create_conversation,
            patch.object(
                sample,
                "call_model",
                AsyncMock(return_value="Saved"),
            ) as call_model,
        ):
            answer = await sample.respond_with_history(
                "Remember CEDAR-842",
                state,
                on_text_delta,
            )

        self.assertEqual(answer, "Saved")
        create_conversation.assert_awaited_once_with()
        conversation_state.set_value.assert_called_once_with(
            "modelConversationId",
            "conv_new",
        )
        call_model.assert_awaited_once_with(
            "Remember CEDAR-842",
            "conv_new",
            on_text_delta,
        )

    async def test_later_turn_reuses_project_conversation(self):
        conversation_state = SimpleNamespace(
            get_value=Mock(return_value="conv_existing"),
            set_value=Mock(),
        )
        state = SimpleNamespace(conversation=conversation_state)
        on_text_delta = Mock()

        with (
            patch.object(
                sample,
                "create_model_conversation",
                AsyncMock(),
            ) as create_conversation,
            patch.object(
                sample,
                "call_model",
                AsyncMock(return_value="CEDAR-842"),
            ) as call_model,
        ):
            answer = await sample.respond_with_history(
                "What is my verification code?",
                state,
                on_text_delta,
            )

        self.assertEqual(answer, "CEDAR-842")
        create_conversation.assert_not_awaited()
        conversation_state.set_value.assert_not_called()
        call_model.assert_awaited_once_with(
            "What is my verification code?",
            "conv_existing",
            on_text_delta,
        )

    async def test_stale_project_conversation_is_replaced_once(self):
        conversation_state = SimpleNamespace(
            get_value=Mock(return_value="conv_stale"),
            set_value=Mock(),
        )
        state = SimpleNamespace(conversation=conversation_state)
        on_text_delta = Mock()

        with (
            patch.object(
                sample,
                "create_model_conversation",
                AsyncMock(return_value="conv_replacement"),
            ) as create_conversation,
            patch.object(
                sample,
                "call_model",
                AsyncMock(
                    side_effect=[
                        sample.ModelConversationNotFoundError("conv_stale"),
                        "Recovered",
                    ]
                ),
            ) as call_model,
        ):
            answer = await sample.respond_with_history(
                "Continue",
                state,
                on_text_delta,
            )

        self.assertEqual(answer, "Recovered")
        create_conversation.assert_awaited_once_with()
        conversation_state.set_value.assert_called_once_with(
            "modelConversationId",
            "conv_replacement",
        )
        self.assertEqual(
            [call.args for call in call_model.await_args_list],
            [
                ("Continue", "conv_stale", on_text_delta),
                ("Continue", "conv_replacement", on_text_delta),
            ],
        )


class FoundrySdkTests(unittest.IsolatedAsyncioTestCase):
    async def test_creates_project_conversation_with_async_foundry_client(self):
        openai = SimpleNamespace(
            conversations=SimpleNamespace(
                create=AsyncMock(return_value=SimpleNamespace(id="conv_created"))
            )
        )
        project = AsyncContext(
            SimpleNamespace(get_openai_client=Mock(return_value=AsyncContext(openai)))
        )

        with (
            patch.object(sample, "DefaultAzureCredential", return_value=AsyncContext()),
            patch.object(sample, "AIProjectClient", return_value=project) as client_type,
            patch.dict(
                sample.os.environ,
                {"FOUNDRY_PROJECT_ENDPOINT": "https://project.example.test"},
            ),
        ):
            conversation_id = await sample.create_model_conversation()

        self.assertEqual(conversation_id, "conv_created")
        client_type.assert_called_once_with(
            endpoint="https://project.example.test",
            credential=unittest.mock.ANY,
        )
        openai.conversations.create.assert_awaited_once_with()

    async def test_model_call_uses_stored_project_conversation(self):
        on_text_delta = Mock()
        openai = SimpleNamespace(
            responses=SimpleNamespace(
                create=AsyncMock(
                    return_value=AsyncEvents(
                        [
                            SimpleNamespace(
                                type="response.created",
                            ),
                            SimpleNamespace(
                                type="response.output_text.delta",
                                delta="Stored ",
                            ),
                            SimpleNamespace(
                                type="response.output_text.delta",
                                delta="reply",
                            ),
                        ]
                    )
                )
            )
        )
        project = AsyncContext(
            SimpleNamespace(get_openai_client=Mock(return_value=AsyncContext(openai)))
        )

        with (
            patch.object(sample, "DefaultAzureCredential", return_value=AsyncContext()),
            patch.object(sample, "AIProjectClient", return_value=project),
            patch.dict(
                sample.os.environ,
                {
                    "FOUNDRY_PROJECT_ENDPOINT": "https://project.example.test",
                    "AZURE_AI_MODEL_DEPLOYMENT_NAME": "gpt-test",
                },
            ),
        ):
            answer = await sample.call_model(
                "Remember this",
                "conv_existing",
                on_text_delta,
            )

        self.assertEqual(answer, "Stored reply")
        openai.responses.create.assert_awaited_once_with(
            model="gpt-test",
            conversation="conv_existing",
            input="Remember this",
            store=True,
            stream=True,
        )
        self.assertEqual(
            [call.args for call in on_text_delta.call_args_list],
            [("Stored ",), ("reply",)],
        )


class ActivityBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_blank_message_does_not_start_activity_stream(self):
        streaming_response = SimpleNamespace(
            queue_text_chunk=Mock(),
            end_stream=AsyncMock(),
        )
        context = SimpleNamespace(
            activity=SimpleNamespace(text="   "),
            streaming_response=streaming_response,
        )

        with patch.object(sample, "respond_with_history", AsyncMock()) as respond:
            await sample.on_message(context, SimpleNamespace())

        respond.assert_not_awaited()
        streaming_response.queue_text_chunk.assert_not_called()
        streaming_response.end_stream.assert_not_awaited()

    async def test_message_deltas_are_finalized_by_activity_stream(self):
        streaming_response = SimpleNamespace(
            queue_text_chunk=Mock(),
            end_stream=AsyncMock(),
        )
        context = SimpleNamespace(
            activity=SimpleNamespace(text="Hello"),
            streaming_response=streaming_response,
        )
        state = SimpleNamespace()

        with patch.object(
            sample,
            "respond_with_history",
            AsyncMock(return_value="Hello back"),
        ) as respond:
            await sample.on_message(context, state)

        respond.assert_awaited_once_with(
            "Hello",
            state,
            streaming_response.queue_text_chunk,
        )
        streaming_response.end_stream.assert_awaited_once_with()

    async def test_error_finalizes_stream_with_trace_reference(self):
        trace_id = "4962ec560fbbcb05a468b4fef411d301"
        streaming_response = SimpleNamespace(
            get_message=Mock(return_value=""),
            queue_text_chunk=Mock(),
            end_stream=AsyncMock(),
        )
        context = SimpleNamespace(
            activity=SimpleNamespace(id="activity-123"),
            streaming_response=streaming_response,
            send_activity=AsyncMock(),
        )
        error = RuntimeError("internal path and secret details")
        span = SimpleNamespace(
            get_span_context=Mock(
                return_value=SimpleNamespace(
                    is_valid=True,
                    trace_id=int(trace_id, 16),
                )
            )
        )

        with (
            patch.object(sample, "get_current_span", return_value=span),
            patch.object(sample.logger, "error") as log_error,
        ):
            await sample.on_error(context, error)

        user_message = streaming_response.queue_text_chunk.call_args.args[0]
        self.assertIn(trace_id, user_message)
        self.assertNotIn(str(error), user_message)
        streaming_response.end_stream.assert_awaited_once_with()
        context.send_activity.assert_not_awaited()
        self.assertEqual(
            log_error.call_args.kwargs["exc_info"],
            (RuntimeError, error, error.__traceback__),
        )
        self.assertEqual(log_error.call_args.args[1:], (trace_id, "activity-123"))

    async def test_error_uses_normal_activity_when_stream_finalization_fails(self):
        streaming_response = SimpleNamespace(
            get_message=Mock(return_value=""),
            queue_text_chunk=Mock(),
            end_stream=AsyncMock(side_effect=RuntimeError("stream failed")),
        )
        context = SimpleNamespace(
            activity=SimpleNamespace(id="activity-789"),
            streaming_response=streaming_response,
            send_activity=AsyncMock(),
        )
        invalid_span = SimpleNamespace(
            get_span_context=Mock(
                return_value=SimpleNamespace(is_valid=False, trace_id=0)
            )
        )

        with (
            patch.object(sample, "get_current_span", return_value=invalid_span),
            patch.object(
                sample.uuid,
                "uuid4",
                return_value=SimpleNamespace(hex="fallback-reference"),
            ),
            patch.object(sample.logger, "error"),
            patch.object(sample.logger, "exception"),
        ):
            await sample.on_error(context, ValueError("private failure detail"))

        fallback_message = context.send_activity.await_args.args[0]
        self.assertIn("fallback-reference", fallback_message)
        self.assertNotIn("private failure detail", fallback_message)
        context.send_activity.assert_awaited_once()

    async def test_error_marks_partial_stream_as_interrupted(self):
        streaming_response = SimpleNamespace(
            get_message=Mock(return_value="An unfinished answer"),
            queue_text_chunk=Mock(),
            end_stream=AsyncMock(),
        )
        context = SimpleNamespace(
            activity=SimpleNamespace(id="activity-456"),
            streaming_response=streaming_response,
            send_activity=AsyncMock(),
        )
        invalid_span = SimpleNamespace(
            get_span_context=Mock(
                return_value=SimpleNamespace(is_valid=False, trace_id=0)
            )
        )

        with (
            patch.object(sample, "get_current_span", return_value=invalid_span),
            patch.object(
                sample.uuid,
                "uuid4",
                return_value=SimpleNamespace(hex="fallback-reference"),
            ),
            patch.object(sample.logger, "error"),
        ):
            await sample.on_error(context, ValueError("private failure detail"))

        user_message = streaming_response.queue_text_chunk.call_args.args[0]
        self.assertTrue(user_message.startswith("\n\nThe response was interrupted."))
        self.assertIn("fallback-reference", user_message)
        self.assertNotIn("private failure detail", user_message)
        streaming_response.end_stream.assert_awaited_once_with()
        context.send_activity.assert_not_awaited()


class AsyncEvents:
    def __init__(self, events):
        self.events = events

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for event in self.events:
            yield event


class AsyncContext:
    def __init__(self, value=None):
        self.value = value or self

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return None


if __name__ == "__main__":
    unittest.main()