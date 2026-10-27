"""Agente Central de Salas: host MCP por dentro, servidor A2A v1.0 por fora.

Por fora, publica o Agent Card e atende SendMessage/GetTask no binding JSON-RPC.
Por dentro, e cliente MCP (SDK oficial) do servidor de salas: descobre as tools
por tools/list, le o resource politica://uso e chama reservar_sala.

A ponte: quando o servidor MCP responde input_required, a Task vai para
TASK_STATE_INPUT_REQUIRED e o requestState fica guardado, opaco, ligado a ela
(PAUSAS). A continuacao A2A da mesma Task vira um novo tools/call, com id
JSON-RPC novo, levando inputResponses e o requestState ecoado sem modificacao.

Nao ha LLM nem regra de sala aqui: o pedido tem formato fixo e toda decisao de
dominio (conflito, politica, alternativas) e do servidor MCP.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import uvicorn
from mcp import Client
from mcp.client.session import ClientSession
from mcp.shared.exceptions import MCPError
from mcp_types import (
    CallToolResult,
    ClientCapabilities,
    ElicitationCapability,
    ElicitResult,
    FormElicitationCapability,
    Implementation,
    InputRequiredResult,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

HOST = os.environ.get("AGENTE_HOST", "127.0.0.1")
PORTA = int(os.environ.get("AGENTE_PORT", "7300"))
URL_PUBLICA = os.environ.get("AGENTE_URL", f"http://{HOST}:{PORTA}").rstrip("/")
MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp")
PROTOCOLO_MCP = "2026-07-28"

TOOL_RESERVA = "reservar_sala"
RESOURCE_POLITICA = "politica://uso"

PEDIDO = re.compile(
    r"^\s*reservar\s+sala=(?P<sala>\S+)\s+inicio=(?P<inicio>\S+)\s+fim=(?P<fim>\S+)\s+responsavel=(?P<responsavel>.+?)\s*$"
)
ESCOLHA = re.compile(r"^\s*escolha=(?P<valor>\S+)\s*$")
RECUSA = "recusar"

TERMINAIS = {"TASK_STATE_COMPLETED", "TASK_STATE_CANCELED", "TASK_STATE_FAILED", "TASK_STATE_REJECTED"}

# Erros JSON-RPC do A2A v1.0
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602
TASK_NOT_FOUND, UNSUPPORTED_OPERATION = -32001, -32004


def log(mensagem: str) -> None:
    print(mensagem, file=sys.stderr, flush=True)


def novo_id(prefixo: str) -> str:
    return f"{prefixo}-{secrets.token_hex(6)}"


# --- cliente MCP -------------------------------------------------------------


class SessaoFormApenas(ClientSession):
    """Declara exatamente {"elicitation": {"form": {}}}.

    O ClientSession do SDK, quando ha elicitation_callback, anuncia form e url.
    O agente so trata form, entao anuncia so form.
    """

    def _build_capabilities(self, version: str) -> ClientCapabilities:
        capacidades = super()._build_capabilities(version)
        return capacidades.model_copy(
            update={"elicitation": ElicitationCapability(form=FormElicitationCapability())}
        )


class ClienteMCP(Client):
    async def _build_session(self, exit_stack: AsyncExitStack) -> ClientSession:
        sessao = await super()._build_session(exit_stack)
        sessao.__class__ = SessaoFormApenas
        return sessao


async def nunca_responde_sozinho(*_: Any, **__: Any) -> Any:
    # So existe para o SDK declarar a capability. Os tools/call usam
    # allow_input_required=True, entao o input_required chega cru ao agente e
    # quem responde a elicitation e o cliente A2A, nunca este callback.
    raise RuntimeError("elicitation deve ser respondida pelo cliente A2A")


class HostMCP:
    def __init__(self) -> None:
        self.cliente: Client | None = None
        self.tools: dict[str, Any] = {}
        self.trava = asyncio.Lock()

    async def abrir(self, pilha: AsyncExitStack) -> None:
        self.cliente = await pilha.enter_async_context(
            ClienteMCP(
                MCP_URL,
                mode=PROTOCOLO_MCP,
                client_info=Implementation(name="agente-central-de-salas", version="1.0.0"),
                elicitation_callback=nunca_responde_sozinho,
                cache=None,
            )
        )

    def _meta(self, trace_id: str) -> dict[str, Any]:
        # Mesmo trace-id da Task, span-id novo a cada request MCP.
        return {"traceparent": f"00-{trace_id}-{secrets.token_hex(8)}-01"}

    async def descobrir(self, trace_id: str) -> None:
        """tools/list antes da primeira chamada: nada de lista fixa no codigo."""
        async with self.trava:
            if self.tools:
                return
            assert self.cliente is not None
            resultado = await self.cliente.list_tools(meta=self._meta(trace_id))
            self.tools = {t.name: t for t in resultado.tools}
            log(f"tools descobertas: {sorted(self.tools)}")

    async def versao_da_politica(self, trace_id: str) -> str:
        assert self.cliente is not None
        resultado = await self.cliente.read_resource(RESOURCE_POLITICA, meta=self._meta(trace_id))
        texto = getattr(resultado.contents[0], "text", "") if resultado.contents else ""
        primeira = texto.splitlines()[0] if texto else ""
        chave, _, valor = primeira.partition(":")
        if chave.strip() != "versao" or not valor.strip():
            raise RuntimeError(f"politica sem versao na primeira linha: {primeira!r}")
        return valor.strip()

    async def chamar(
        self,
        nome: str,
        argumentos: dict[str, Any],
        trace_id: str,
        respostas: dict[str, ElicitResult] | None = None,
        estado: str | None = None,
    ) -> CallToolResult | InputRequiredResult:
        await self.descobrir(trace_id)
        if nome not in self.tools:
            raise RuntimeError(f"o servidor MCP nao oferece a tool {nome}")
        assert self.cliente is not None
        # Cada chamada sai com um id JSON-RPC novo, gerado pela sessao do SDK.
        return await self.cliente.session.call_tool(
            nome,
            argumentos,
            meta=self._meta(trace_id),
            input_responses=respostas,
            request_state=estado,
            allow_input_required=True,
        )


HOST_MCP = HostMCP()


# --- Tasks -------------------------------------------------------------------


@dataclass
class Pausa:
    """O que o agente guarda entre o input_required e a continuacao.

    request_state e opaco: guardado e ecoado byte a byte, nunca aberto. Nada
    daqui sai em resposta A2A.
    """

    argumentos: dict[str, Any]
    chave: str
    opcoes: list[str]
    request_state: str


@dataclass
class Task:
    id: str
    context_id: str
    trace_id: str
    estado: str = "TASK_STATE_SUBMITTED"
    mensagem: dict[str, Any] | None = None
    historico: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    trava: asyncio.Lock = field(default_factory=asyncio.Lock)

    def mudar(self, estado: str, texto: str | None = None) -> None:
        if self.estado in TERMINAIS:
            raise RuntimeError(f"Task {self.id} ja esta em estado terminal {self.estado}")
        self.estado = estado
        if texto is not None:
            self.mensagem = {
                "messageId": novo_id("msg"),
                "role": "ROLE_AGENT",
                "parts": [{"text": texto}],
                "taskId": self.id,
                "contextId": self.context_id,
            }
            self.historico.append(self.mensagem)
        log(f"task {self.id} -> {estado}" + (f" ({texto})" if texto else ""))

    def wire(self, tamanho_historico: int | None = None) -> dict[str, Any]:
        status: dict[str, Any] = {"state": self.estado}
        if self.mensagem is not None:
            status["message"] = self.mensagem
        historico = self.historico
        if tamanho_historico is not None:
            historico = historico[-tamanho_historico:] if tamanho_historico > 0 else []
        return {
            "id": self.id,
            "contextId": self.context_id,
            "status": status,
            "history": historico,
            "artifacts": self.artifacts,
        }


TASKS: dict[str, Task] = {}
PAUSAS: dict[str, Pausa] = {}


class ErroA2A(Exception):
    def __init__(self, codigo: int, mensagem: str) -> None:
        super().__init__(mensagem)
        self.codigo = codigo
        self.mensagem = mensagem


def texto_da_mensagem(mensagem: dict[str, Any]) -> str:
    partes = mensagem.get("parts") or []
    return " ".join(p.get("text", "") for p in partes if isinstance(p, dict)).strip()


def texto_do_resultado(resultado: CallToolResult) -> str:
    return " ".join(getattr(c, "text", "") for c in resultado.content).strip()


def trace_id_do_header(traceparent: str | None) -> str | None:
    if not traceparent:
        return None
    partes = traceparent.strip().split("-")
    if len(partes) >= 4 and re.fullmatch(r"[0-9a-f]{32}", partes[1]) and partes[1] != "0" * 32:
        return partes[1]
    return None


def linha_de_alternativas(opcoes: list[str]) -> str:
    return "alternativas: " + ", ".join(opcoes)


async def aplicar_resultado(
    task: Task,
    argumentos: dict[str, Any],
    resultado: CallToolResult | InputRequiredResult,
    politica: str,
) -> None:
    """Traduz o resultado MCP em estado da Task. E aqui que a ponte pausa."""
    if isinstance(resultado, InputRequiredResult):
        pedidos = resultado.input_requests or {}
        if len(pedidos) != 1 or not resultado.request_state:
            task.mudar("TASK_STATE_FAILED", "Resposta input_required inesperada do servidor MCP")
            return
        chave, pedido = next(iter(pedidos.items()))
        params = pedido.model_dump(by_alias=True, mode="json", exclude_none=True).get("params") or {}
        campo = ((params.get("requestedSchema") or {}).get("properties") or {}).get("sala") or {}
        opcoes = list(campo.get("enum") or ([campo["const"]] if "const" in campo else []))
        if pedido.method != "elicitation/create" or params.get("mode", "form") != "form" or not opcoes:
            task.mudar("TASK_STATE_FAILED", "Pedido de input nao suportado pelo agente")
            return
        PAUSAS[task.id] = Pausa(argumentos, chave, opcoes, resultado.request_state)
        task.mudar("TASK_STATE_INPUT_REQUIRED", linha_de_alternativas(opcoes))
        return

    PAUSAS.pop(task.id, None)
    if resultado.is_error:
        task.mudar("TASK_STATE_FAILED", texto_do_resultado(resultado) or "Falha na tool")
        return
    dados = resultado.structured_content or {}
    if not dados.get("reservado"):
        task.mudar("TASK_STATE_CANCELED", f"Reserva nao realizada: {dados.get('motivo') or 'recusado'}.")
        return
    reserva = {
        "reserva": dados.get("reserva"),
        "sala": dados.get("sala"),
        "inicio": dados.get("inicio"),
        "fim": dados.get("fim"),
        "responsavel": dados.get("responsavel"),
        "politica": politica,
    }
    task.artifacts.append(
        {"artifactId": novo_id("art"), "name": "reserva", "parts": [{"text": json.dumps(reserva, ensure_ascii=False)}]}
    )
    task.mudar("TASK_STATE_COMPLETED", f"Reserva {reserva['reserva']} confirmada na {reserva['sala']}.")


async def iniciar(task: Task, texto: str) -> None:
    pedido = PEDIDO.match(texto)
    if not pedido:
        task.mudar(
            "TASK_STATE_REJECTED",
            "Pedido fora do formato: reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>",
        )
        return
    argumentos = pedido.groupdict()
    task.mudar("TASK_STATE_WORKING")
    try:
        await HOST_MCP.descobrir(task.trace_id)
        politica = await HOST_MCP.versao_da_politica(task.trace_id)
        resultado = await HOST_MCP.chamar(TOOL_RESERVA, argumentos, task.trace_id)
        await aplicar_resultado(task, argumentos, resultado, politica)
    except MCPError as e:
        PAUSAS.pop(task.id, None)
        task.mudar("TASK_STATE_FAILED", e.error.message)


async def continuar(task: Task, texto: str) -> None:
    pausa = PAUSAS[task.id]
    escolha = ESCOLHA.match(texto)
    valor = escolha.group("valor") if escolha else None
    if valor != RECUSA and valor not in pausa.opcoes:
        # Fora do enum: a Task continua pausada e repete a mesma linha.
        task.mudar("TASK_STATE_INPUT_REQUIRED", linha_de_alternativas(pausa.opcoes))
        return
    if valor == RECUSA:
        resposta = ElicitResult(action="decline")
    else:
        resposta = ElicitResult(action="accept", content={"sala": valor})
    task.mudar("TASK_STATE_WORKING")
    try:
        politica = await HOST_MCP.versao_da_politica(task.trace_id)
        # O retry: mesmo tools/call, id JSON-RPC novo, mesma chave do inputRequests
        # e o requestState exatamente como veio.
        resultado = await HOST_MCP.chamar(
            TOOL_RESERVA,
            pausa.argumentos,
            task.trace_id,
            respostas={pausa.chave: resposta},
            estado=pausa.request_state,
        )
        await aplicar_resultado(task, pausa.argumentos, resultado, politica)
    except MCPError as e:
        PAUSAS.pop(task.id, None)
        task.mudar("TASK_STATE_FAILED", e.error.message)


def tamanho_historico(params: dict[str, Any]) -> int | None:
    valor = (params.get("configuration") or {}).get("historyLength")
    return valor if isinstance(valor, int) else None


async def send_message(params: dict[str, Any], traceparent: str | None) -> dict[str, Any]:
    mensagem = params.get("message")
    if not isinstance(mensagem, dict) or not isinstance(mensagem.get("parts"), list):
        raise ErroA2A(INVALID_PARAMS, "params.message com parts e obrigatorio")
    texto = texto_da_mensagem(mensagem)
    trace_id = trace_id_do_header(traceparent)
    task_id = mensagem.get("taskId")

    if task_id:
        task = TASKS.get(task_id)
        if task is None:
            raise ErroA2A(TASK_NOT_FOUND, f"Task nao encontrada: {task_id}")
        async with task.trava:
            if task.estado in TERMINAIS:
                raise ErroA2A(UNSUPPORTED_OPERATION, f"Task {task_id} esta em estado terminal {task.estado}")
            if task.estado != "TASK_STATE_INPUT_REQUIRED" or task.id not in PAUSAS:
                raise ErroA2A(UNSUPPORTED_OPERATION, f"Task {task_id} nao esta aguardando input")
            if trace_id:
                task.trace_id = trace_id
            task.historico.append(mensagem)
            await continuar(task, texto)
            return {"task": task.wire(tamanho_historico(params))}

    task = Task(
        id=novo_id("task"),
        context_id=mensagem.get("contextId") or novo_id("ctx"),
        trace_id=trace_id or secrets.token_hex(16),
    )
    TASKS[task.id] = task
    log(f"task {task.id} -> TASK_STATE_SUBMITTED trace-id={task.trace_id}")
    async with task.trava:
        task.historico.append(mensagem)
        await iniciar(task, texto)
    return {"task": task.wire(tamanho_historico(params))}


async def get_task(params: dict[str, Any]) -> dict[str, Any]:
    task = TASKS.get(params.get("id", ""))
    if task is None:
        raise ErroA2A(TASK_NOT_FOUND, f"Task nao encontrada: {params.get('id')}")
    valor = params.get("historyLength")
    return {"task": task.wire(valor if isinstance(valor, int) else None)}


# --- HTTP --------------------------------------------------------------------


def agent_card() -> dict[str, Any]:
    return {
        "name": "Central de Salas",
        "description": "Reserva salas de reuniao da Hill Valley Tech.",
        "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
        "version": "1.0.0",
        "supportedInterfaces": [
            {"url": f"{URL_PUBLICA}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
        ],
        "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "reservar-sala",
                "name": "Reservar sala",
                "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
                "tags": ["salas", "agenda"],
                "inputModes": ["text/plain"],
                "outputModes": ["text/plain"],
                "examples": [
                    "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 "
                    "fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
                ],
            }
        ],
    }


async def rota_card(_: Request) -> JSONResponse:
    return JSONResponse(agent_card())


def erro_rpc(id_: Any, codigo: int, mensagem: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "error": {"code": codigo, "message": mensagem}})


async def rota_a2a(request: Request) -> JSONResponse:
    try:
        corpo = json.loads(await request.body())
    except ValueError:
        return erro_rpc(None, PARSE_ERROR, "JSON invalido")
    if not isinstance(corpo, dict) or corpo.get("jsonrpc") != "2.0" or not isinstance(corpo.get("method"), str):
        return erro_rpc(corpo.get("id") if isinstance(corpo, dict) else None, INVALID_REQUEST, "Request JSON-RPC invalido")
    id_, metodo = corpo.get("id"), corpo["method"]
    params = corpo.get("params") if isinstance(corpo.get("params"), dict) else {}
    traceparent = request.headers.get("traceparent")
    log(f"a2a request method={metodo} id={id_!r} traceparent={traceparent or '-'}")
    try:
        if metodo == "SendMessage":
            resultado = await send_message(params, traceparent)
        elif metodo == "GetTask":
            resultado = await get_task(params)
        else:
            raise ErroA2A(METHOD_NOT_FOUND, f"Metodo nao suportado: {metodo}")
    except ErroA2A as e:
        log(f"a2a erro {e.codigo}: {e.mensagem}")
        return erro_rpc(id_, e.codigo, e.mensagem)
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": resultado})


@asynccontextmanager
async def ciclo_de_vida(_: Starlette):
    async with AsyncExitStack() as pilha:
        await HOST_MCP.abrir(pilha)
        log(f"agente A2A em {URL_PUBLICA}/a2a, cliente MCP de {MCP_URL}")
        yield


app = Starlette(
    routes=[
        Route("/.well-known/agent-card.json", rota_card, methods=["GET"]),
        Route("/a2a", rota_a2a, methods=["POST"]),
    ],
    lifespan=ciclo_de_vida,
)


def main() -> None:
    uvicorn.run(app, host=HOST, port=PORTA, log_level="warning")


if __name__ == "__main__":
    main()
