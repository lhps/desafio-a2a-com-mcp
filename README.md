# A Ponte: agente A2A com MCP por dentro

Entrega do desafio da Hill Valley Tech: dois processos separados que conversam por HTTP.

- **`servidor-mcp/servidor.py`**: servidor MCP em Streamable HTTP (stateless, revisão `2026-07-28`) na porta `7301`, endpoint `/mcp`. Tem as tools `listar_salas`, `consultar_disponibilidade` e `reservar_sala` e o resource `politica://uso`. O ciclo de MRTR fica na reserva.
- **`agente/agente.py`**: agente A2A v1.0 (binding JSON-RPC) na porta `7300`, com o card em `/.well-known/agent-card.json` e o endpoint em `/a2a`. Por dentro ele é host MCP, usando o cliente oficial do SDK, do servidor acima.

Stack: Python ≥ 3.11, SDK oficial `mcp==2.3.0` (v2), Starlette e Uvicorn. As versões estão travadas em `servidor-mcp/pyproject.toml` e `agente/pyproject.toml`. O agente não usa LLM e não tem SDK de provedor de LLM nas dependências.

## Como rodar

A partir de um clone limpo, na raiz do repositório:

```bash
# 1. ambiente (Python 3.11 ou superior)
python3 -m venv .venv
.venv/bin/pip install -e ./servidor-mcp -e ./agente

# 2. segredo do requestState (mínimo de 32 bytes aleatórios; nunca commitar o valor)
export REQUEST_STATE_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
```

> O segredo precisa ser o mesmo enquanto houver `requestState` em circulação: é com ele que um retry continua válido depois de reiniciar o servidor MCP. Para reaproveitar o valor em outro terminal, rode `echo $REQUEST_STATE_SECRET` no primeiro e faça `export REQUEST_STATE_SECRET=<valor>` no segundo. Se o servidor subir sem a variável, ou com menos de 32 bytes, ele se recusa a iniciar.

Terminal 1, servidor MCP (o stderr mostra cada request com método, id, traceparent, capabilities e headers):

```bash
REQUEST_STATE_SECRET="$REQUEST_STATE_SECRET" .venv/bin/python servidor-mcp/servidor.py
```

Terminal 2, agente:

```bash
.venv/bin/python agente/agente.py
```

Terminal 3, validador (com os dois processos recém-iniciados):

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

Variáveis opcionais, com os valores padrão: `MCP_HOST=127.0.0.1`, `MCP_PORT=7301`, `AGENTE_HOST=127.0.0.1`, `AGENTE_PORT=7300`, `AGENTE_URL=http://127.0.0.1:7300` (a URL publicada no card) e `MCP_URL=http://127.0.0.1:7301/mcp`.

Exemplo manual do fluxo com pausa:

```bash
curl -s localhost:7300/.well-known/agent-card.json

curl -s localhost:7300/a2a -H 'Content-Type: application/json' \
  -H 'traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01' \
  -d '{"jsonrpc":"2.0","id":1,"method":"SendMessage","params":{"message":{"messageId":"m1","role":"ROLE_USER","parts":[{"text":"reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty"}]}}}'
# -> TASK_STATE_INPUT_REQUIRED, "alternativas: sala-fusca, sala-mirante"

curl -s localhost:7300/a2a -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"SendMessage","params":{"message":{"messageId":"m2","role":"ROLE_USER","parts":[{"text":"escolha=sala-mirante"}],"taskId":"<id da task>"}}}'
# -> TASK_STATE_COMPLETED com o artifact "reserva"
```

## Onde a ponte acontece

A Task é pausada em `aplicar_resultado` (`agente/agente.py:266`). O agente chama `reservar_sala` com `allow_input_required=True` (`agente/agente.py:167`), então o `InputRequiredResult` chega cru e não passa por nenhum callback de elicitation. Quando o resultado é `input_required`, o agente lê a única entrada de `inputRequests`, tira as alternativas do `enum` (ou do `const`) do `requestedSchema` e guarda em `PAUSAS[task.id]` a chave, as opções, os argumentos originais e o `requestState` intacto (`agente/agente.py:285`). Em seguida a Task vai para `TASK_STATE_INPUT_REQUIRED` com a linha `alternativas: ...` (`agente/agente.py:286`).

O `requestState` volta ao servidor em `continuar` (`agente/agente.py:331`). O agente responde a um `SendMessage` com `taskId` e `escolha=<id>` repetindo o mesmo `tools/call`, com os argumentos que guardou, `inputResponses` na mesma chave e o `requestState` exatamente como veio (`agente/agente.py:353`). A sessão do SDK atribui um id JSON-RPC novo a cada request. `escolha=recusar` vira `action: "decline"`, e a Task termina em `TASK_STATE_CANCELED`. Uma escolha fora do `enum` mantém a Task pausada e repete a mesma linha.

Do lado do servidor, o resolver `escolha_de_sala` (`servidor-mcp/servidor.py:175`) é quem decide se precisa perguntar. Ele roda antes do corpo da tool e devolve `Elicit(...)` com o `enum` das alternativas quando há conflito. O SDK transforma isso no `input_required` e, no retry, injeta a resposta em `reservar_sala`.

## Decisões técnicas

**Proteção do `requestState`.** Uso o utilitário do próprio SDK, `RequestStateSecurity(keys=[REQUEST_STATE_SECRET], ttl=600)` (`servidor-mcp/servidor.py:155`). Ele instala o `RequestStateBoundary`, que sela o estado com AES-256-GCM (chave derivada por HKDF-SHA256), e por isso o estado fica cifrado e autenticado. O envelope selado carrega `iat`/`exp`, o método, o nome da tool, um digest dos `arguments` e a audiência (`central-de-salas`). O que acontece em cada caso:

- Um caractere trocado faz a tag GCM falhar, e o servidor responde `-32602 Invalid or expired requestState`.
- Um estado expirado recebe o mesmo `-32602`.
- Argumentos diferentes dos selados no retry fazem o servidor rejeitar o estado com `-32602` (a divergência não toma efeito).

A chave vem só da variável de ambiente, e o servidor recusa subir se ela tiver menos de 32 bytes. Nada fica em memória entre o `input_required` e o retry, então um retry continua válido depois de reiniciar o servidor MCP com o mesmo segredo. Testei isso manualmente: com o mesmo segredo a reserva é concluída, e com outro segredo o retry recebe `-32602`.

**Validade.** O `requestState` vale 10 minutos (`VALIDADE_DO_ESTADO`, `servidor-mcp/servidor.py:33`), dentro da faixa de 5 a 30 minutos.

**Capability.** A checagem de elicitation em form mode é do SDK. Sem a capability, a resposta é `-32021` com `data.requiredCapabilities` e HTTP 400. A ausência de `protocolVersion` ou de `clientCapabilities` no `_meta` dá `-32602` com HTTP 400, e um header `Mcp-*` que não bate com o corpo dá `-32020`.

**Estado das Tasks.** Fica em memória no processo do agente, em dois dicionários (`agente/agente.py:233`):

- `TASKS` guarda o que é público: id, contextId, status, histórico e artifacts.
- `PAUSAS` guarda o que é privado de cada Task pausada: a chave do `inputRequests`, as opções, os argumentos e o `requestState`.

`PAUSAS` nunca é serializado em resposta A2A, e o `requestState` não aparece no card, nos artifacts nem nas mensagens. Cada Task tem um `asyncio.Lock`, e as pausas são indexadas pelo id da Task, então duas Tasks pausadas ao mesmo tempo não trocam de estado. Estados terminais (`COMPLETED`, `CANCELED`, `FAILED`, `REJECTED`) são definitivos: um `SendMessage` para uma Task terminal recebe o erro `-32004`.

**Host MCP.** Um único `mcp.Client` fica vivo durante a vida do agente, com a versão fixada em `2026-07-28`. Todo request leva no `_meta` `protocolVersion`, `clientInfo` e `clientCapabilities`. O SDK também envia os headers `MCP-Protocol-Version`, `Mcp-Method` e `Mcp-Name`. O agente faz `tools/list` antes do primeiro `tools/call` e confere que `reservar_sala` existe. A cada Task ele lê `politica://uso` e extrai a versão da primeira linha, e é essa versão que vai no campo `politica` do artifact.

**Traceparent.** O trace-id do header `traceparent` do `SendMessage` fica associado à Task. Cada request MCP daquela Task leva `traceparent` no `_meta` com o mesmo trace-id e um span-id novo. Se a continuação chegar sem header, o agente usa o trace-id guardado na Task.

**Limitação do SDK.** Quando existe `elicitation_callback`, o `ClientSession` do SDK anuncia `{"elicitation": {"form": {}, "url": {}}}`, em `mcp/client/session.py`, `_build_capabilities`:

```python
elicitation = (
    types.ElicitationCapability(form=types.FormElicitationCapability(), url=types.UrlElicitationCapability())
    if self._elicitation_callback is not _default_elicitation_callback
    else None
)
```

O agente só trata form mode. Por isso `SessaoFormApenas` (`agente/agente.py:79`) sobrescreve só esse método para anunciar exatamente `{"elicitation": {"form": {}}}`. O resto do protocolo continua com o SDK. O callback existe apenas para que a capability seja declarada e nunca é chamado, porque os `tools/call` usam `allow_input_required=True`.

## Saída do validador

Última execução, com os dois processos recém-iniciados:

```
trace-id desta execucao: ec9fa329ed5a48f11130a077fa46af85
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```

No stderr do servidor MCP desta execução, as linhas com o trace-id `ec9fa329ed5a48f11130a077fa46af85` aparecem primeiro no `tools/list` do agente (`id=1`) e depois em cada `resources/read` e `tools/call` das Tasks. Os pares de `tools/call` de uma reserva pausada aparecem com ids diferentes, e o retry vem marcado com `retry=sim`.
