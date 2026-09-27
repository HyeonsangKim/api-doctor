"""ModelGateway 를 LangChain `BaseChatModel` 로 감싼다 (FR-003).

deepagents/LangGraph 가 모델을 호출하면 **반드시 우리 원장을 지난다.**
프레임워크 내부의 구조화 출력 재시도·요약·자동 재시도까지 여기서 계측된다.
콜백이 아니라 모델 자체를 감싸는 이유다.

`agent_id` 는 실행 컨텍스트에서 온다. 모델 인스턴스를 역할별로 따로 만들고
그 인스턴스가 자기 역할을 알고 있으므로, 프롬프트가 역할을 바꿔치기할 수 없다.
"""

from __future__ import annotations

from typing import Any, Iterator, Sequence

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult

from ..runtime.budget import BudgetExceeded, Cancelled
from .gateway import ModelGateway, ModelUnavailable


def _to_role(message: BaseMessage) -> dict[str, Any]:
    """LangChain 메시지를 OpenAI 호환 형식으로 옮긴다."""
    if isinstance(message, SystemMessage):
        role = "system"
    elif isinstance(message, AIMessage):
        role = "assistant"
    elif isinstance(message, ToolMessage):
        # 도구 결과는 사용자 관측으로 전달한다. 우리 backend 는 tool role 을
        # 쓰지 않으므로 내용을 그대로 싣는다.
        return {"role": "user", "content": f"도구 결과:\n{message.content}"}
    else:
        role = "user"
    content = message.content
    if isinstance(content, list):
        content = "\n".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    return {"role": role, "content": str(content)}


class GatewayChatModel(BaseChatModel):
    """예산 원장을 강제하는 채팅 모델.

    LangChain 이 이 객체를 통해서만 추론하므로, 프레임워크가 몰래 부르는
    호출도 전부 원장에 남는다.
    """

    # pydantic 이 ModelGateway(slots dataclass + Protocol 필드)를 검증하지
    # 못하므로 Any 로 둔다. 타입은 `for_agent` 가 보장한다.
    gateway: Any
    agent_id: str

    model_config = {"arbitrary_types_allowed": True}

    @property
    def _llm_type(self) -> str:
        return "api-doctor-gateway"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"agent_id": self.agent_id, "model": self.gateway.config.model}

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        payload = [_to_role(m) for m in messages]
        try:
            text = self.gateway.complete(agent_id=self.agent_id, messages=payload)
        except Cancelled:
            raise
        except (BudgetExceeded, ModelUnavailable) as exc:
            # 프레임워크에 예외를 그대로 올리면 루프가 끊긴다.
            # 모델의 답으로 내려보내 하네스가 정상 종료 경로를 타게 한다.
            text = _refusal(exc)
        return ChatResult(generations=[ChatGeneration(message=_to_message(text))])

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "GatewayChatModel":
        """도구 바인딩은 프롬프트 수준에서 처리한다.

        우리 backend 는 OpenAI tool-calling 을 쓰지 않고 JSON 프로토콜을 쓴다.
        도구 목록은 시스템 프롬프트에 이미 들어 있으므로 여기서는
        같은 인스턴스를 돌려준다.
        """
        return self


def _to_message(text: str) -> AIMessage:
    """우리 JSON 프로토콜을 LangChain 의 tool_calls 로 옮긴다.

    프롬프트는 `{"tool": ..., "args": {...}}` 를 요구하는데 LangChain 은
    `AIMessage.tool_calls` 로 도구를 돌린다. 둘을 잇지 않으면 하네스 루프가
    도구를 한 번도 부르지 않고 끝난다.

    네이티브 tool-calling 대신 이 방식을 쓰는 이유는, 공급자가 tool 스키마를
    지원하는지와 무관하게 같은 프로토콜로 동작하기 때문이다.
    """
    import uuid

    from ..jsonio import ProtocolError, parse_json_object

    try:
        data = parse_json_object(text)
    except ProtocolError:
        return AIMessage(content=text)

    tool = data.get("tool")
    if not isinstance(tool, str) or not tool.strip():
        return AIMessage(content=text)

    args = data.get("args")
    return AIMessage(
        content=str(data.get("note", "")),
        tool_calls=[{
            "name": tool.strip(),
            "args": args if isinstance(args, dict) else {},
            "id": f"call_{uuid.uuid4().hex[:12]}",
            "type": "tool_call",
        }],
    )


def _refusal(exc: Exception) -> str:
    import json

    return json.dumps(
        {"outcome": "blocked", "summary": f"예산 또는 모델 경로 문제로 중단합니다: {exc}",
         "unknowns": ["조사를 끝내지 못했습니다."]},
        ensure_ascii=False,
    )


def for_agent(gateway: ModelGateway, agent_id: str) -> GatewayChatModel:
    """역할별 모델 인스턴스. 역할은 인스턴스에 고정된다."""
    return GatewayChatModel(gateway=gateway, agent_id=agent_id)
