import asyncio
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
    def test_handles_message_on_canonical_activity_route(self):
        activity = {
            "type": "message",
            "channelId": "emulator",
            "serviceUrl": "http://localhost",
            "from": {"id": "user-1", "name": "Local user"},
            "recipient": {"id": "activity-model", "name": "Activity model"},
            "conversation": {"id": "route-test"},
            "deliveryMode": "expectReplies",
            "text": "Hello",
        }

        async def respond_with_text(_prompt, _state, on_text_delta, _context):
            on_text_delta("Hello back")
            return "Hello back"

        with (
            patch.object(
                sample,
                "respond_with_history",
                side_effect=respond_with_text,
            ) as respond,
            TestClient(sample.host) as client,
        ):
            response = client.post("/activity/messages", json=activity)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["activities"][0]["text"], "Hello back")
        respond.assert_awaited_once()
        self.assertEqual(respond.await_args.args[0], "Hello")

    def test_welcomes_only_when_agent_is_added_without_calling_model(self):
        cases = [
            ("agent added", {"membersAdded": [{"id": "activity-model"}]}, 1),
            (
                "agent and user added",
                {"membersAdded": [{"id": "user-1"}, {"id": "activity-model"}]},
                1,
            ),
            ("user added", {"membersAdded": [{"id": "user-2"}]}, 0),
            ("no members added", {"membersAdded": []}, 0),
            ("agent removed", {"membersRemoved": [{"id": "activity-model"}]}, 0),
        ]

        with (
            patch.object(sample, "respond_with_history", AsyncMock()) as respond,
            TestClient(sample.host) as client,
        ):
            for name, update, expected_count in cases:
                with self.subTest(name=name):
                    activity = {
                        "type": "conversationUpdate",
                        "channelId": "emulator",
                        "serviceUrl": "http://localhost",
                        "from": {"id": "user-1", "name": "Local user"},
                        "recipient": {"id": "activity-model", "name": "Activity model"},
                        "conversation": {"id": f"welcome-{name}"},
                        "deliveryMode": "expectReplies",
                        **update,
                    }
                    response = client.post("/activity/messages", json=activity)

                    self.assertEqual(response.status_code, 200, response.text)
                    replies = response.json()["activities"]
                    self.assertEqual(len(replies), expected_count)
                    if expected_count:
                        self.assertIn("Ask me a question", replies[0]["text"])
                        self.assertIn("@mention me", replies[0]["text"])

        respond.assert_not_awaited()


class ConversationMappingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        sample._conversation_turns.clear()

    @staticmethod
    def make_context(conversation_id):
        return SimpleNamespace(
            activity=SimpleNamespace(
                channel_id="test-channel",
                conversation=SimpleNamespace(id=conversation_id),
            )
        )

    async def test_first_turn_creates_and_stores_project_conversation(self):
        conversation_state = SimpleNamespace(
            get_value=Mock(return_value=None),
            set_value=Mock(),
        )
        context = self.make_context("first-turn")
        state = SimpleNamespace(
            conversation=conversation_state,
            save=AsyncMock(),
        )
        on_text_delta = Mock()
        call_order = []

        async def save_state(saved_context):
            self.assertIs(saved_context, context)
            call_order.append("save")

        async def call_model(prompt, conversation_id, callback):
            call_order.append("model")
            return "Saved"

        with (
            patch.object(
                sample,
                "create_model_conversation",
                AsyncMock(return_value="conv_new"),
            ) as create_conversation,
            patch.object(
                sample,
                "call_model",
                side_effect=call_model,
            ) as call_model,
        ):
            state.save.side_effect = save_state
            answer = await sample.respond_with_history(
                "Remember CEDAR-842",
                state,
                on_text_delta,
                context,
            )

        self.assertEqual(answer, "Saved")
        self.assertEqual(call_order, ["save", "model"])
        create_conversation.assert_awaited_once_with()
        state.save.assert_awaited_once_with(context)
        conversation_state.set_value.assert_called_once_with(
            "modelConversationId",
            "conv_new",
        )
        call_model.assert_awaited_once_with(
            "Remember CEDAR-842",
            "conv_new",
            on_text_delta,
        )

    async def test_concurrent_first_turns_share_saved_conversation(self):
        context = self.make_context("concurrent-first-turn")
        first_conversation_state = SimpleNamespace(
            get_value=Mock(return_value=None),
            set_value=Mock(),
        )
        second_conversation_state = SimpleNamespace(
            get_value=Mock(return_value=None),
            set_value=Mock(),
        )
        first_state = SimpleNamespace(
            conversation=first_conversation_state,
            save=AsyncMock(),
        )
        second_state = SimpleNamespace(
            conversation=second_conversation_state,
            save=AsyncMock(),
        )
        first_model_started = asyncio.Event()
        release_first_model = asyncio.Event()
        second_model_started = asyncio.Event()
        second_call_order = []

        async def save_second_state(_context):
            second_call_order.append("save")

        async def call_model(prompt, _conversation_id, _on_text_delta):
            if prompt == "first":
                first_model_started.set()
                await release_first_model.wait()
            else:
                second_call_order.append("model")
                second_model_started.set()
            return prompt

        second_state.save.side_effect = save_second_state
        with (
            patch.object(
                sample,
                "create_model_conversation",
                AsyncMock(return_value="conv_shared"),
            ) as create_conversation,
            patch.object(
                sample,
                "call_model",
                side_effect=call_model,
            ) as model_call,
        ):
            first_turn = asyncio.create_task(
                sample.respond_with_history("first", first_state, Mock(), context)
            )
            await first_model_started.wait()
            second_turn = asyncio.create_task(
                sample.respond_with_history("second", second_state, Mock(), context)
            )
            await asyncio.sleep(0)
            self.assertFalse(second_model_started.is_set())
            release_first_model.set()

            answers = await asyncio.gather(first_turn, second_turn)

        self.assertEqual(answers, ["first", "second"])
        create_conversation.assert_awaited_once_with()
        first_state.save.assert_awaited_once_with(context)
        second_state.save.assert_awaited_once_with(context)
        self.assertEqual(second_call_order, ["save", "model"])
        second_conversation_state.set_value.assert_called_once_with(
            "modelConversationId",
            "conv_shared",
        )
        self.assertEqual(
            [call.args[1] for call in model_call.await_args_list],
            ["conv_shared", "conv_shared"],
        )

    async def test_later_turn_reuses_project_conversation(self):
        conversation_state = SimpleNamespace(
            get_value=Mock(return_value="conv_existing"),
            set_value=Mock(),
        )
        state = SimpleNamespace(
            conversation=conversation_state,
            save=AsyncMock(),
        )
        context = self.make_context("later-turn")
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
                context,
            )

        self.assertEqual(answer, "CEDAR-842")
        create_conversation.assert_not_awaited()
        state.save.assert_not_awaited()
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
        state = SimpleNamespace(
            conversation=conversation_state,
            save=AsyncMock(),
        )
        context = self.make_context("stale-turn")
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
                context,
            )

        self.assertEqual(answer, "Recovered")
        create_conversation.assert_awaited_once_with()
        state.save.assert_awaited_once_with(context)
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
    def setUp(self):
        sample._foundry_clients = None

    async def test_creates_project_conversation(self):
        openai = SimpleNamespace(
            conversations=SimpleNamespace(
                create=AsyncMock(return_value=SimpleNamespace(id="conv_created"))
            )
        )

        with patch.object(sample, "get_openai_client", return_value=openai):
            conversation_id = await sample.create_model_conversation()

        self.assertEqual(conversation_id, "conv_created")
        openai.conversations.create.assert_awaited_once_with()

    async def test_model_call_uses_stored_project_conversation(self):
        on_text_delta = Mock()
        openai = SimpleNamespace(
            responses=SimpleNamespace(
                create=AsyncMock(
                    return_value=AsyncEvents(
                        [
                            SimpleNamespace(type="response.created"),
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

        with (
            patch.object(sample, "get_openai_client", return_value=openai),
            patch.dict(
                sample.os.environ,
                {"AZURE_AI_MODEL_DEPLOYMENT_NAME": "gpt-test"},
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

    async def test_shared_client_is_created_once_and_closed_on_shutdown(self):
        credential = SimpleNamespace(close=AsyncMock())
        openai = SimpleNamespace(close=AsyncMock())
        project = SimpleNamespace(
            get_openai_client=Mock(return_value=openai),
            close=AsyncMock(),
        )

        with (
            patch.object(sample, "DefaultAzureCredential", return_value=credential),
            patch.object(sample, "AIProjectClient", return_value=project) as client_type,
            patch.dict(
                sample.os.environ,
                {"FOUNDRY_PROJECT_ENDPOINT": "https://project.example.test"},
            ),
        ):
            first = sample.get_openai_client()
            second = sample.get_openai_client()
            await sample.close_foundry_clients()

        self.assertIs(first, openai)
        self.assertIs(second, openai)
        client_type.assert_called_once_with(
            endpoint="https://project.example.test",
            credential=credential,
        )
        openai.close.assert_awaited_once_with()
        project.close.assert_awaited_once_with()
        credential.close.assert_awaited_once_with()
        self.assertIsNone(sample._foundry_clients)


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
            context,
        )
        streaming_response.end_stream.assert_awaited_once_with()

    async def test_error_finalizes_stream_with_trace_reference(self):
        trace_id = "4962ec560fbbcb05a468b4fef411d301"
        streaming_response = SimpleNamespace(
            queue_text_chunk=Mock(),
            end_stream=AsyncMock(),
        )
        context = SimpleNamespace(
            activity=SimpleNamespace(id="activity-123"),
            streaming_response=streaming_response,
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
        self.assertEqual(
            log_error.call_args.kwargs["exc_info"],
            (RuntimeError, error, error.__traceback__),
        )
        self.assertEqual(log_error.call_args.args[1:], (trace_id, "activity-123"))


class AsyncEvents:
    def __init__(self, events):
        self.events = events

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for event in self.events:
            yield event


if __name__ == "__main__":
    unittest.main()