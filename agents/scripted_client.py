"""A deterministic chat client for MOCK.

This is the piece that keeps the demo honest. Without it, MOCK would need its own
executor to walk a plan and call tools, and the governance middleware would never
actually run - the talk would be claiming that the same enforcement code works in
both modes while only ever exercising it in one.

`ScriptedChatClient` is assembled from the same Agent Framework layers a real
provider client uses:

    FunctionInvocationLayer -> ChatMiddlewareLayer -> ChatTelemetryLayer -> BaseChatClient

which matters more than it looks. `FunctionInvocationLayer` is what actually
executes tool calls and runs function middleware. A bare object satisfying
`SupportsChatGetResponse` will happily return a function-call content and the
agent will hand it straight back to you unexecuted - no tool call, no middleware,
no governance. Only `_inner_get_response` is ours; everything above it is the
framework's own machinery, which is precisely the point.

So MOCK runs a real `Agent`, with real `FunctionMiddleware` and a real Agent
Hooks bundle, over a real MCP tool server. The only thing that is not real is
which tool gets chosen - and that is exactly the thing you do not want a model
improvising on stage.

In LIVE, `FoundryChatClient` takes this client's place. Nothing else changes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from agent_framework import (
    BaseChatClient,
    ChatMiddlewareLayer,
    ChatResponse,
    Content,
    FunctionInvocationLayer,
    Message,
)
from agent_framework.observability import ChatTelemetryLayer

from agents.planner import ToolCall


class ScriptedChatClient(
    FunctionInvocationLayer,
    ChatMiddlewareLayer,
    ChatTelemetryLayer,
    BaseChatClient,
):
    """Emits a fixed set of tool calls, then a fixed closing message."""

    OTEL_PROVIDER_NAME = "scripted"

    def __init__(
        self,
        steps: Sequence[ToolCall] = (),
        *,
        final_text: str = "Done.",
        model: str = "scripted-planner",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._steps = tuple(steps)
        self._final_text = final_text
        self._model = model
        self.turns = 0

    @property
    def service_url(self) -> str:
        return "scripted://helix/planner"

    def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        stream: bool,
        options: Mapping[str, Any],
        **kwargs: Any,
    ):
        if stream:
            return self._build_response_stream(self._stream())
        return self._respond()

    async def _respond(self) -> ChatResponse:
        self.turns += 1
        if self.turns == 1 and self._steps:
            contents = [
                Content.from_function_call(
                    call_id=f"scripted-{index}",
                    name=step.tool,
                    arguments=dict(step.arguments),
                )
                for index, step in enumerate(self._steps)
            ]
            return ChatResponse(
                messages=[Message("assistant", contents)],
                response_id=f"scripted-{self.turns}",
                model=self._model,
                finish_reason="tool_calls",
            )
        # A later turn: the tool results - or the governance refusals - came back.
        return ChatResponse(
            messages=[Message("assistant", [self._final_text])],
            response_id=f"scripted-{self.turns}",
            model=self._model,
            finish_reason="stop",
        )

    async def _stream(self):  # pragma: no cover - the acts never stream
        from agent_framework import ChatResponseUpdate

        response = await self._respond()
        for message in response.messages:
            yield ChatResponseUpdate(contents=list(message.contents))
