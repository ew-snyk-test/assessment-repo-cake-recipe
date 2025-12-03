import hashlib
import json
import os
from enum import Enum
from typing import Any, Optional

from cachetools import TTLCache
from google.oauth2 import service_account
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_google_vertexai.chat_models import ChatVertexAI
from langchain_google_vertexai.model_garden import ChatAnthropicVertex
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import create_react_agent
from loguru import logger

from agent.mcp_servers.mcp_utils import ToolsContext
from agent.mcp_servers.mcps import get_tools
from config.config import conf


class GraphCache:
    _cache: TTLCache[str, CompiledStateGraph] = TTLCache(maxsize=50, ttl=60 * 10)  # 10 minutes

    def _create_key(self, tool_context: ToolsContext) -> str:
        """Return a secure SHA-256 hex digest (64 chars)."""
        hash_data = f"{tool_context.tenant_id}:{tool_context.socket_id}:{tool_context.auth_bearer_token}"
        return f"{tool_context.socket_id}_{hashlib.sha256(hash_data.encode('utf-8')).hexdigest()}"

    def pop(self, socket_id: str) -> None:
        # Find and remove all keys that start with the given socket ID
        keys_to_remove = [key for key in self._cache.keys() if key.startswith(socket_id)]
        for key in keys_to_remove:
            self._cache.pop(key, None)

    def get(self, tool_context: ToolsContext) -> Optional[CompiledStateGraph]:
        key = self._create_key(tool_context)
        return self._cache.get(key)

    def set(self, tool_context: ToolsContext, graph: CompiledStateGraph) -> None:
        key = self._create_key(tool_context)
        self._cache[key] = graph

    def clear(self) -> None:
        self._cache.clear()


graph_cache = GraphCache()


class VertexAiType(Enum):
    Google = "Google"
    Anthropic = "Anthropic"


def _model_to_model_provider(model_name: str) -> VertexAiType:
    if model_name.startswith("gemini"):
        return VertexAiType.Google
    elif model_name.startswith("claude"):
        return VertexAiType.Anthropic
    else:
        raise ValueError(f"Model {model_name} cannot be handle yet.")


def get_llm(model_name: str) -> BaseChatModel:
    vertexai_service_account = conf.agent.vertexAiServiceAccount
    model_provider = _model_to_model_provider(model_name)
    if vertexai_service_account:
        logger.info(
            "Using langchain_google_vertexai.chat_models.ChatVertexAI with service account",
            model_name=model_name,
        )
        account_info: Any
        try:
            account_info = json.loads(vertexai_service_account)
        except json.JSONDecodeError:
            logger.error("Failed to parse vertexai_service_account JSON")
            raise ValueError("Invalid JSON in vertexai_service_account")
        llm_credentials = service_account.Credentials.from_service_account_info(account_info)
        if model_provider is VertexAiType.Google:
            return ChatVertexAI(
                model=model_name,
                temperature=0,
                max_tokens=None,
                max_retries=3,
                stop=None,
                credentials=llm_credentials,
            )
        elif model_provider is VertexAiType.Anthropic:
            return ChatAnthropicVertex(
                model=model_name,
                credentials=llm_credentials.with_scopes(["https://www.googleapis.com/auth/cloud-platform"]),
                location="global",
            )
        else:
            raise ValueError(f"Model {model_name} cannot be handle yet.")
    elif os.getenv("GOOGLE_API_KEY"):
        if model_provider is not VertexAiType.Google:
            raise ValueError(
                f"Either use a vertex ai service account or switch to a google model, current model: {model_name} with provider {model_provider}"
            )
        logger.info(
            "Using langchain_google_genai.ChatGoogleGenerativeAI with GOOGLE_API_KEY",
            model_name=model_name,
        )
        return ChatGoogleGenerativeAI(
            model=model_name,
            temperature=0,
            max_tokens=None,
            timeout=None,
            max_retries=3,
        )
    else:
        raise ValueError(
            "Either GOOGLE_API_KEY (gemini api key), or SNYK_agent__vertexAiServiceAccount env variables need to be provided"
        )


# Generate Mermaid diagram and save to graph.md
def write_graph_md(graph: Any) -> None:
    mermaid_diagram = graph.get_graph().draw_mermaid()
    with open("graph.md", "w") as f:
        f.write("# Graph Visualization\n\n")
        f.write("```mermaid\n")
        f.write(mermaid_diagram)
        f.write("\n```\n")


system_prompt = open(os.path.join(os.path.dirname(__file__), "system_prompt.md")).read()


async def create_graph(
    context: ToolsContext,
    llm: BaseChatModel,
    tools: Optional[Any] = None,
    cache: bool = False,
) -> Any:
    if cache:
        cached_graph = graph_cache.get(context)
        if cached_graph:
            logger.info(f"using cached langgraph for socket_id:{context.socket_id}")
            return cached_graph

    if tools is None:
        tools = await get_tools(context)
    graph = create_react_agent(
        model=llm,
        tools=tools,
        prompt=system_prompt,
    )
    if cache:
        graph_cache.set(context, graph)
    return graph
