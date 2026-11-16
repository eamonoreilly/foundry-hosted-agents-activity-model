"""Microsoft Foundry Activity agent with project-backed model history."""

import asyncio
import logging
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from azure.ai.agentserver.activity import ActivityAgentServerHost
from azure.ai.projects.aio import AIProjectClient
from azure.ai.projects.telemetry import AIProjectInstrumentor
from azure.identity.aio import DefaultAzureCredential
from dotenv import load_dotenv
from openai import AsyncOpenAI, NotFoundError
from opentelemetry.trace import format_trace_id, get_current_span


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logger = logging.getLogger("activity-model")

MODEL_CONVERSATION_STATE_KEY = "modelConversationId"
GENERIC_ERROR_MESSAGE = "Sorry, something went wrong. Please try again."
WELCOME_MESSAGE = (
    "Hello! I'm your Foundry Activity agent. Ask me a question to get started. "
    "In group chats and channels, @mention me so I receive your message."
)


def load_local_environment(env_file: Path | None = None) -> None:
    """Load local settings without overriding the process environment."""
    load_dotenv(
        dotenv_path=env_file or Path(__file__).with_name(".env"),
        override=False,
    )


def configure_model_telemetry() -> None:
    """Enable semantic model spans with explicit content-recording consent."""
    # Keep this false unless prompt and response capture is explicitly approved.
    enable_content_recording = (
        os.getenv("ENABLE_SENSITIVE_DATA", "false").strip().lower() == "true"
    )
    AIProjectInstrumentor().instrument(
        enable_content_recording=enable_content_recording
    )


load_local_environment()
host = ActivityAgentServerHost()
configure_model_telemetry()
app = host.agent_app


class ModelConversationNotFoundError(Exception):
    """The stored project conversation no longer exists."""


@dataclass
class _ConversationTurn:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    model_conversation_id: str | None = None


_conversation_turns: dict[str, _ConversationTurn] = {}


def create_error_reference() -> str:
    """Return the current telemetry trace ID or a searchable fallback reference."""
    span_context = get_current_span().get_span_context()
    if span_context.is_valid:
        return format_trace_id(span_context.trace_id)
    return uuid.uuid4().hex


async def send_error_response(context, error: Exception) -> None:
    """Log diagnostic details and send only an opaque reference to the channel."""
    reference = create_error_reference()
    logger.error(
        "Activity handler failed | reference=%s activity_id=%s",
        reference,
        getattr(context.activity, "id", None),
        exc_info=(type(error), error, error.__traceback__),
    )
    try:
        context.streaming_response.queue_text_chunk(
            f"\n\n{GENERIC_ERROR_MESSAGE}\n\nReference: `{reference}`"
        )
        await context.streaming_response.end_stream()
    except Exception:
        logger.exception("Failed to send the error response | reference=%s", reference)


_foundry_clients: tuple[DefaultAzureCredential, AIProjectClient, AsyncOpenAI] | None = None


def get_openai_client() -> AsyncOpenAI:
    """Return the process-wide Foundry project OpenAI client, creating it once."""
    global _foundry_clients
    if _foundry_clients is None:
        credential = DefaultAzureCredential()
        project = AIProjectClient(
            endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
            credential=credential,
        )
        _foundry_clients = (credential, project, project.get_openai_client())
    return _foundry_clients[2]


@host.shutdown_handler
async def close_foundry_clients() -> None:
    """Close the shared clients when the hosted session's compute stops."""
    global _foundry_clients
    if _foundry_clients is not None:
        credential, project, openai = _foundry_clients
        _foundry_clients = None
        await openai.close()
        await project.close()
        await credential.close()


async def create_model_conversation() -> str:
    """Create a conversation in the Foundry project Responses service."""
    conversation = await get_openai_client().conversations.create()
    return conversation.id


async def call_model(
    prompt: str,
    conversation_id: str,
    on_text_delta: Callable[[str], None],
) -> str:
    """Stream one stored Responses turn and forward each model text delta."""
    output_text = ""
    try:
        events = await get_openai_client().responses.create(
            model=os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"],
            conversation=conversation_id,
            input=prompt,
            store=True,
            stream=True,
        )
        async for event in events:
            if event.type == "response.output_text.delta":
                output_text += event.delta
                on_text_delta(event.delta)
    except NotFoundError as exc:
        raise ModelConversationNotFoundError(conversation_id) from exc

    if not output_text:
        raise RuntimeError("The model response did not contain output text.")
    return output_text


async def respond_with_history(
    prompt: str,
    state,
    on_text_delta: Callable[[str], None],
    context,
) -> str:
    """Map one Activity conversation to one project Responses conversation."""
    activity_conversation = context.activity.conversation
    lock_key = f"{context.activity.channel_id}/{activity_conversation.id}"
    turn = _conversation_turns.get(lock_key)
    if turn is None:
        turn = _ConversationTurn()
        _conversation_turns[lock_key] = turn

    async with turn.lock:
        stored_conversation_id = state.conversation.get_value(
            MODEL_CONVERSATION_STATE_KEY
        )
        conversation_id = turn.model_conversation_id or stored_conversation_id
        if not conversation_id:
            conversation_id = await create_model_conversation()
            state.conversation.set_value(
                MODEL_CONVERSATION_STATE_KEY,
                conversation_id,
            )
            await state.save(context)
        elif stored_conversation_id != conversation_id:
            # The SDK loads state before acquiring this lock, so a waiting turn
            # may need the conversation ID saved by the preceding turn.
            state.conversation.set_value(
                MODEL_CONVERSATION_STATE_KEY,
                conversation_id,
            )
            await state.save(context)

        turn.model_conversation_id = conversation_id

        try:
            return await call_model(prompt, conversation_id, on_text_delta)
        except ModelConversationNotFoundError:
            logger.info("Replacing a missing project conversation")
            conversation_id = await create_model_conversation()
            state.conversation.set_value(
                MODEL_CONVERSATION_STATE_KEY,
                conversation_id,
            )
            await state.save(context)
            turn.model_conversation_id = conversation_id
            return await call_model(prompt, conversation_id, on_text_delta)


@app.conversation_update("membersAdded")
async def on_members_added(context, state):
    """Welcome the conversation when the agent itself is added."""
    if any(
        member.id == context.activity.recipient.id
        for member in context.activity.members_added
    ):
        await context.send_activity(WELCOME_MESSAGE)


@app.activity("message")
async def on_message(context, state):
    """Reply to a Teams message using project-scoped model history."""
    prompt = (context.activity.text or "").strip()
    if not prompt:
        return

    # Responses exposes model deltas; StreamingResponse translates those deltas
    # into the Activity updates expected by Teams and sends the final message.
    streaming_response = context.streaming_response
    await respond_with_history(
        prompt,
        state,
        streaming_response.queue_text_chunk,
        context,
    )
    await streaming_response.end_stream()


@app.error
async def on_error(context, error):
    """Log failures and return a channel-safe message."""
    await send_error_response(context, error)


if __name__ == "__main__":
    logger.info("Starting Activity model agent")
    host.run()