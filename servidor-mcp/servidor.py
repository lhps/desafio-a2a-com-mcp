"""Servidor MCP da Central de Salas (Streamable HTTP, stateless, revisao 2026-07-28).

Tres tools (listar_salas, consultar_disponibilidade, reservar_sala), um resource
(politica://uso) e o ciclo de MRTR na reserva: quando a sala pedida esta ocupada,
a tool responde input_required com uma elicitation em form mode, e o estado da
rodada viaja selado no requestState (RequestStateSecurity do SDK, AES-256-GCM com
chave vinda de REQUEST_STATE_SECRET). Nada fica guardado aqui entre as rodadas.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.resolve import Elicit, ElicitationResult, Resolve
from mcp.server.request_state import RequestStateSecurity
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field, create_model

DADOS = Path(__file__).resolve().parent.parent / "dados"
SAO_PAULO = timezone(timedelta(hours=-3))
ABRE, FECHA = time(8, 0), time(20, 0)
DURACAO_MAXIMA = timedelta(hours=2)
MAXIMO_ALTERNATIVAS = 3
# Entre os 5 e 30 minutos que o enunciado pede.
VALIDADE_DO_ESTADO = 10 * 60


def log(mensagem: str) -> None:
    print(mensagem, file=sys.stderr, flush=True)


def ler_segredo() -> bytes:
    segredo = os.environ.get("REQUEST_STATE_SECRET", "")
    if len(segredo.encode()) < 32:
        raise SystemExit(
            "REQUEST_STATE_SECRET ausente ou com menos de 32 bytes. Gere com:\n"
            '  export REQUEST_STATE_SECRET="$(python3 -c \'import secrets; print(secrets.token_hex(32))\')"'
        )
    return segredo.encode()


# --- dominio ---------------------------------------------------------------

SALAS: list[dict[str, Any]] = json.loads((DADOS / "salas.json").read_text(encoding="utf-8"))
SALAS_POR_ID = {s["id"]: s for s in SALAS}
RESERVAS: list[dict[str, Any]] = json.loads((DADOS / "reservas.json").read_text(encoding="utf-8"))
POLITICA_TEXTO = (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")
POLITICA_VERSAO = POLITICA_TEXTO.splitlines()[0].split(":", 1)[1].strip()


def instante(valor: str) -> datetime:
    try:
        dt = datetime.fromisoformat(valor)
    except ValueError as e:
        raise ToolError(f"Data invalida: {valor}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SAO_PAULO)
    return dt


def validar(sala: str, inicio: str, fim: str) -> tuple[datetime, datetime]:
    """Aplica as regras de sala e de politica comuns a consulta e a reserva."""
    if sala not in SALAS_POR_ID:
        raise ToolError(f"Sala inexistente: {sala}")
    ini, fi = instante(inicio), instante(fim)
    if fi <= ini:
        raise ToolError("Intervalo invalido: fim deve ser posterior a inicio")
    ini_local, fim_local = ini.astimezone(SAO_PAULO), fi.astimezone(SAO_PAULO)
    if (
        ini_local.time() < ABRE
        or fim_local.date() != ini_local.date()
        or fim_local.time() > FECHA
    ):
        raise ToolError("Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00")
    if fi - ini > DURACAO_MAXIMA:
        raise ToolError("Duracao acima do limite: a politica permite no maximo 2 horas")
    return ini, fi


def conflitos(sala: str, ini: datetime, fi: datetime) -> list[dict[str, Any]]:
    return [
        r
        for r in RESERVAS
        if r["sala"] == sala and instante(r["inicio"]) < fi and ini < instante(r["fim"])
    ]


def alternativas(sala: str, ini: datetime, fi: datetime) -> list[str]:
    capacidade = SALAS_POR_ID[sala]["capacidade"]
    livres = [
        s
        for s in SALAS
        if s["id"] != sala and s["capacidade"] >= capacidade and not conflitos(s["id"], ini, fi)
    ]
    livres.sort(key=lambda s: (s["capacidade"], s["id"]))
    return [s["id"] for s in livres[:MAXIMO_ALTERNATIVAS]]


def proximo_id() -> str:
    maior = max((int(r["id"].split("-")[1]) for r in RESERVAS), default=0)
    return f"res-{maior + 1:04d}"


# --- contratos de saida ----------------------------------------------------


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


# --- servidor --------------------------------------------------------------

mcp = MCPServer(
    name="central-de-salas",
    version="1.0.0",
    request_state_security=RequestStateSecurity(keys=[ler_segredo()], ttl=VALIDADE_DO_ESTADO),
)


@mcp.tool(description="Lista todas as salas com capacidade e recursos.")
def listar_salas() -> ListaDeSalas:
    return ListaDeSalas(salas=[SalaOut(**s) for s in SALAS])


@mcp.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")
def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    ini, fi = validar(sala, inicio, fim)
    achados = conflitos(sala, ini, fi)
    return Disponibilidade(
        sala=sala,
        livre=not achados,
        conflitos=[ConflitoOut(**{k: r[k] for k in ("id", "inicio", "fim", "responsavel")}) for r in achados],
    )


def escolha_de_sala(sala: str, inicio: str, fim: str) -> Elicit[Any] | None:
    """Resolver do MRTR: so pergunta quando a sala pedida esta ocupada.

    Roda de novo a cada rodada; o SDK so aceita a resposta do cliente se a
    pergunta renderizada agora for identica a que foi selada no requestState.
    """
    ini, fi = validar(sala, inicio, fim)
    if not conflitos(sala, ini, fi):
        return None
    opcoes = alternativas(sala, ini, fi)
    if not opcoes:
        raise ToolError("Sem alternativas disponiveis no intervalo")
    escolha = create_model(
        "EscolhaDeSala",
        sala=(Literal[tuple(opcoes)], Field(title="Sala", description="Sala alternativa escolhida")),
    )
    return Elicit("A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.", escolha)


@mcp.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
def reservar_sala(
    sala: str,
    inicio: str,
    fim: str,
    responsavel: str,
    escolha: Annotated[ElicitationResult[BaseModel], Resolve(escolha_de_sala)],
) -> ReservaOut:
    # Os argumentos que chegam aqui sao os selados: o RequestStateBoundary do SDK
    # amarra o requestState ao digest de name+arguments e recusa qualquer divergencia.
    if escolha.action in ("decline", "cancel"):
        return ReservaOut(reservado=False, motivo="recusado" if escolha.action == "decline" else "cancelado")
    destino = escolha.data.sala if escolha.data is not None else sala
    ini, fi = validar(destino, inicio, fim)
    if conflitos(destino, ini, fi):
        raise ToolError("Sem alternativas disponiveis no intervalo")
    nova = {"id": proximo_id(), "sala": destino, "inicio": inicio, "fim": fim, "responsavel": responsavel}
    RESERVAS.append(nova)
    log(f"reserva criada {nova['id']} sala={destino} {inicio}..{fim} responsavel={responsavel}")
    return ReservaOut(
        reserva=nova["id"],
        sala=destino,
        inicio=inicio,
        fim=fim,
        responsavel=responsavel,
        politica=POLITICA_VERSAO,
    )


@mcp.resource("politica://uso", name="politica-de-uso", mime_type="text/markdown",
              description="Politica de uso das salas. A primeira linha declara a versao.")
def politica() -> str:
    return POLITICA_TEXTO


# --- log de cada request no stderr -----------------------------------------


class LogDeRequests:
    """ASGI: registra metodo, id e traceparent de cada mensagem JSON-RPC recebida."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        corpo = b""
        mais = True
        while mais:
            msg = await receive()
            corpo += msg.get("body", b"")
            mais = msg.get("more_body", False)
        cabecalhos = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        self.registrar(corpo, cabecalhos)
        entregue = False

        async def reenviar() -> dict:
            nonlocal entregue
            if entregue:
                return await receive()
            entregue = True
            return {"type": "http.request", "body": corpo, "more_body": False}

        await self.app(scope, reenviar, send)

    @staticmethod
    def registrar(corpo: bytes, cabecalhos: dict[str, str]) -> None:
        try:
            dados = json.loads(corpo)
        except ValueError:
            log("mcp request ilegivel")
            return
        for m in dados if isinstance(dados, list) else [dados]:
            if not isinstance(m, dict):
                continue
            params = m.get("params") if isinstance(m.get("params"), dict) else {}
            meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
            alvo = params.get("name") or params.get("uri") or ""
            log(
                f"mcp request method={m.get('method')} id={m.get('id')!r}"
                + (f" name={alvo}" if alvo else "")
                + (" retry=sim" if "requestState" in params else "")
                + f" traceparent={meta.get('traceparent', '-')}"
                + f" caps={json.dumps(meta.get('io.modelcontextprotocol/clientCapabilities'), separators=(',', ':'))}"
                + f" hdr[Mcp-Method={cabecalhos.get('mcp-method', '-')}"
                + f" Mcp-Name={cabecalhos.get('mcp-name', '-')}"
                + f" MCP-Protocol-Version={cabecalhos.get('mcp-protocol-version', '-')}]"
            )


def main() -> None:
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    porta = int(os.environ.get("MCP_PORT", "7301"))
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    log(f"servidor MCP central-de-salas em http://{host}:{porta}/mcp (politica {POLITICA_VERSAO})")
    uvicorn.run(LogDeRequests(app), host=host, port=porta, log_level="warning")


if __name__ == "__main__":
    main()
