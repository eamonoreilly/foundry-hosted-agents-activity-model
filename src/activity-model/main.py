"""Microsoft Foundry Activity agent with project-backed model history."""

import logging
import os
import uuid
from collections.abc import Callable

from azure.ai.agentserver.activity import ActivityAgentServerHost
from azure.ai.projects.aio import AIProjectClient
from azure.identity.aio import DefaultAzureCredential
from openai import NotFoundError
from opentelemetry.trace import format_trace_id, get_current_span


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logger = logging.getLogger("activity-model")

MODEL_CONVERSATION_STATE_KEY = "modelConversationId"
GENERIC_ERROR_MESSAGE = "Sorry, something went wrong. Please try again."

host = ActivityAgentServerHost()
app = host.agent_app


class ModelConversationNotFoundError(Exception):
    """The stored project conversation no longer exists."""


def create_error_reference() -> str:
    """Return the current telemetry trace ID or a searchable fallback reference."""
    span_context = get_current_span().get_span_context()
    if span_context.is_valid:
        return format_trace_id(span_context.trace_id)
    return uuid.uuid4().hex


async def send_error_response(context, error: Exception) -> None:
    """Log diagnostic details and send only an opaque reference to the channel."""
    reference = create_error_reference()
    activity_id = getattr(context.activity, "id", None)
    logger.error(
        "Activity handler failed | reference=%s activity_id=%s",
        reference,
        activity_id,
        exc_info=(type(error), error, error.__traceback__),
    )

    response = f"{GENERIC_ERROR_MESSAGE}\n\nReference: `{reference}`"
    streaming_response = context.streaming_response
    try:
        if streaming_response.get_message():
            response = (
                "\n\nThe response was interrupted. Please try again."
                f"\n\nReference: `{reference}`"
            )
        streaming_response.queue_text_chunk(response)
        await streaming_response.end_stream()
    except Exception:
        logger.exception(
            "Failed to finalize the Activity error response | reference=%s",
            reference,
        )
        try:
            await context.send_activity(
                f"{GENERIC_ERROR_MESSAGE}\n\nReference: `{reference}`"
            )
        except Exception:
            logger.exception(
                "Failed to send the fallback Activity error | reference=%s",
                reference,
            )


async def create_model_conversation() -> str:
    """Create a conversation in the Foundry project Responses service."""
    async with DefaultAzureCredential() as credential:
        async with AIProjectClient(
            endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
            credential=credential,
        ) as project:
            async with project.get_openai_client() as openai:
                conversation = await openai.conversations.create()
                return conversation.id


async def call_model(
    prompt: str,
    conversation_id: str,
    on_text_delta: Callable[[str], None],
) -> str:
    """Stream one stored Responses turn and forward each model text delta."""
    output_text = ""
    async with DefaultAzureCredential() as credential:
        async with AIProjectClient(
            endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
            credential=credential,
        ) as project:
            async with project.get_openai_client() as openai:
                try:
                    events = await openai.responses.create(
                        model=os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"],
                        conversation=conversation_id,
                        input=prompt,
                        store=True,
                        stream=True,
                    )
                    async for event in events:
                        if event.type != "response.output_text.delta":
                            continue
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
) -> str:
    """Map one Activity conversation to one project Responses conversation."""
    conversation_id = state.conversation.get_value(MODEL_CONVERSATION_STATE_KEY)
    if not conversation_id:
        conversation_id = await create_model_conversation()
        state.conversation.set_value(
            MODEL_CONVERSATION_STATE_KEY,
            conversation_id,
        )

    try:
        return await call_model(prompt, conversation_id, on_text_delta)
    except ModelConversationNotFoundError:
        logger.info("Replacing a missing project conversation")
        conversation_id = await create_model_conversation()
        state.conversation.set_value(
            MODEL_CONVERSATION_STATE_KEY,
            conversation_id,
        )
        return await call_model(prompt, conversation_id, on_text_delta)


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
    )
    await streaming_response.end_stream()


@app.error
async def on_error(context, error):
    """Log failures and return a channel-safe message."""
    await send_error_response(context, error)


if __name__ == "__main__":
    logger.info("Starting Activity model agent")
    host.run()