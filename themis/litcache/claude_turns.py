"""One model turn as the worker's spending steps take it: streamed, its usage counted however it ends.

A turn that fails after the model began answering was billed, so its usage so far is counted and the failure is
raised as `ocr.SpentError`; the job log then counts the attempt as paid for. `raise_classified` sorts a failed
conversation the same way: paid for, refused at a spend limit, or failed before anything was spent.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Sequence
from typing import Any, NoReturn

import anthropic

from themis.litcache import anthropic_ocr, ocr

# The stop reason a turn's usage is counted under when the turn never finished.
UNFINISHED = 'unfinished'


@dataclasses.dataclass
class Billed:
    """A `TokenCounter` passing usage on, and remembering whether any turn was paid for."""

    tokens: ocr.TokenCounter
    paid: bool = False

    def add(self, usage: anthropic.types.Usage, *, model: str, stop_reason: str) -> None:
        self.paid = True
        self.tokens.add(usage, model=model, stop_reason=stop_reason)


def refusal(message: anthropic.types.Message, request_id: str | None) -> dict[str, str | None]:
    """What the API said of a refused turn: its policy category and explanation, and the ids to look it up by."""
    details = message.stop_details
    return {
        'category': details.category if details is not None else None,
        'explanation': details.explanation if details is not None else None,
        'request_id': request_id,
        'message_id': message.id,
    }


async def stream(
    client: anthropic.AsyncAnthropic,
    *,
    tokens: ocr.TokenCounter,
    model: str,
    max_tokens: int,
    system: str | Sequence[anthropic.types.TextBlockParam],
    tools: Iterable[Any],
    messages: list[anthropic.types.MessageParam],
    cached: bool,
    effort: str | None = None,
    tool_choice: anthropic.types.ToolChoiceParam | None = None,
) -> tuple[anthropic.types.Message, str | None]:
    """Take one turn: the conversation so far, and the model's message with its request id.

    Args:
        client: The Anthropic client the turn runs on.
        tokens: Where the turn's usage is counted, however it ends.
        model: The model to run.
        max_tokens: The most the model may write in the turn.
        system: The system prompt, as text or as blocks, which may carry their own cache points.
        tools: The tool definitions the model may call.
        messages: The conversation so far.
        cached: Write the conversation so far to the prompt cache, at 1.25 times the input price, so a later turn
            that sends it again reads it at a tenth. Only a conversation that will be sent again gains from it.
        effort: The output effort, or None for the model's default.
        tool_choice: How the model must use the tools, or None for its own choice.

    Raises:
        ocr.SpentError: The turn failed after the model began answering; its usage so far is counted.
    """
    # The turn as far as the stream got, from `message_start` on: what a turn that fails after it was paid for.
    snapshot: anthropic.types.Message | None = None
    try:
        async with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=system,
            tools=list(tools),
            tool_choice=tool_choice if tool_choice is not None else anthropic.omit,
            messages=messages,
            cache_control={'type': 'ephemeral'} if cached else anthropic.omit,
            output_config=anthropic.types.OutputConfigParam(effort=effort)  # pyright: ignore[reportArgumentType]
            if effort is not None
            else anthropic.omit,
        ) as turn:
            async for event in turn:
                if snapshot is not None or event.type == 'message_start':
                    snapshot = turn.current_message_snapshot
            message = await turn.get_final_message()
            request_id = turn.request_id
    except BaseException as error:
        # A cancellation (the conversation's timeout) included: whatever ends a turn the model began, it was billed.
        if snapshot is not None:
            tokens.add(snapshot.usage, model=snapshot.model, stop_reason=UNFINISHED)
            if isinstance(error, Exception):
                raise ocr.SpentError(f'a turn failed after the model answered: {error!r}') from error
        raise
    tokens.add(message.usage, model=message.model, stop_reason=message.stop_reason or UNFINISHED)
    return message, request_id


def raise_classified(error: Exception, billed: Billed) -> NoReturn:
    """Raise a conversation's failure as what it cost: paid for, refused at a spend limit, or unspent.

    Raises:
        ocr.SpentError: A turn of the conversation had been paid for.
        ocr.SpendRefusedError: The API refused the first turn at a spend limit, or for billing.
        Exception: `error` itself, when nothing was spent.
    """
    if billed.paid:
        raise ocr.SpentError(f'the conversation failed after a paid turn: {error!r}') from error
    if isinstance(error, anthropic.APIStatusError) and anthropic_ocr.spend_refused(error):
        raise ocr.SpendRefusedError(str(error)) from error
    raise error
