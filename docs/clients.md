# 클라이언트에 등록하기

GPU-Gate 는 stdio MCP 서버다. 클라이언트가 `python <repo>/gpu_gate_server.py` 를 자식 프로세스로 띄우고 도구 세 개(`comfy_batch`, `voice_batch`, `gate_status`)를 에이전트에게 보여 준다.

- `<repo>` 는 이 저장소를 clone 한 폴더의 절대 경로로 바꾼다(Windows 에서도 `/` 를 쓸 수 있다).
- `python` 이 PATH 에 없거나 3.10 미만이면 인터프리터의 절대 경로를 쓴다.
- `pip install .` 로 설치했다면 `gpu_gate_server.py` 경로 대신 `python -m gpu_gate` 로도 같은 서버가 뜬다(`command` 는 `python`, 인자는 `-m`, `gpu_gate`).
- 등록하기 전에 `python gpu_gate_server.py --selftest` 가 통과하는지 먼저 확인한다([README](../README.md#설치)).

## 먼저 읽기: 도구 타임아웃

서버는 진행 알림을 보내지 않고, `comfy_batch`/`voice_batch` 호출은 배치가 끝날 때까지 돌아오지 않는다. 클라이언트의 도구 타임아웃이 배치보다 짧으면 클라이언트가 호출을 끊고 취소를 보내며, 그러면 배치가 취소된다. 배치의 기본 제한은 `limits.batch_timeout_s` (예시 설정은 9000초, 2시간 30분)이므로 클라이언트 타임아웃은 이보다 길게, 아래 스니펫처럼 3시간으로 잡는다. `batch_timeout_s` 를 늘렸다면 이 값도 함께 늘린다.

| 클라이언트 | 설정 파일 | 타임아웃 |
|---|---|---|
| opencode | `opencode.jsonc` | 실행 타임아웃의 기본값이 12시간이라 따로 안 건드려도 된다. `startup` 은 서버가 뜨는 시간(밀리초). |
| Qwen Code | `~/.qwen/settings.json` | 기본 도구 타임아웃이 10분이라 `timeout` 을 반드시 늘린다(밀리초). |
| Hermes | `config.yaml` | 기본 300초라 `timeout` 을 늘린다(초). |

## opencode

`opencode.jsonc` 의 최상위 `"mcp": { ... }` 객체 안에 넣는다.

```jsonc
"gpu-gate": {
  "type": "local",
  "command": ["python", "<repo>/gpu_gate_server.py"],
  "timeout": {"startup": 30000}
}
```

끄려면 같은 항목에 `"disabled": true` 를 더한다.

## Qwen Code

`~/.qwen/settings.json` 의 `"mcpServers"` 안에 넣는다.

```json
"gpu-gate": {
  "command": "python",
  "args": ["<repo>/gpu_gate_server.py"],
  "timeout": 10800000,
  "description": "GPU gate: run ComfyUI/VoiceStudio batches with the local LLM unloaded"
}
```

`timeout` 은 밀리초(3시간)다. 기본값 10분을 그대로 두면 10분이 넘는 배치가 도중에 끊긴다.

끄려면 `settings.json` 의 `mcp.excluded` 목록에 `"gpu-gate"` 를 넣는다.

```json
"mcp": {
  "excluded": ["gpu-gate"]
}
```

### Qwen 이 Strata 를 직접 끄지 못하게 하기

Qwen Code 에 Strata 용 MCP 서버(`strata`)가 이미 연결돼 있다면, 에이전트가 그 도구로 LLM 을 직접 끄거나 켜지 못하게 하는 편이 안전하다. 게이트는 Strata 를 HTTP 로 직접 다루므로 이 제한의 영향을 받지 않는다. 기존 `mcpServers.strata` 항목 안에 이 한 줄을 더한다.

```json
"includeTools": ["strata_status"]
```

이러면 에이전트에게는 상태 조회 도구인 `strata_status` 만 보인다.

## Hermes

`config.yaml` 의 `mcp_servers:` 아래에 넣는다.

```yaml
mcp_servers:
  gpu-gate:
    command: python
    args:
      - <repo>/gpu_gate_server.py
    timeout: 10800
```

`timeout` 은 초(3시간)다. 끄려면 같은 항목에 `enabled: false` 를 더한다.

## 끄는 방법 요약

| 클라이언트 | 끄기 |
|---|---|
| opencode | 항목에 `"disabled": true` |
| Qwen Code | `mcp.excluded` 에 `"gpu-gate"` |
| Hermes | 항목에 `enabled: false` |

설정을 바꾼 뒤에는 새 세션부터 반영된다(클라이언트에 따라 재시작이 필요할 수 있다). 끄면 다음 세션에서 도구가 보이지 않고, 다시 켜면 보인다.

## 에이전트 지침

도구를 등록하는 것만으로는 에이전트가 GPU 작업을 항상 게이트로 보낸다는 보장이 없다. 아래 블록을 각 클라이언트의 지침 파일(Qwen Code 의 `QWEN.md`, opencode 의 `AGENTS.md`, Hermes 의 시스템 프롬프트)에 붙여 넣는다. 서버도 `initialize` 응답으로 비슷한 안내를 보내지만, 지침 파일에 있는 쪽이 더 확실하게 지켜진다.

한국어:

```text
`gpu-gate` 도구(`comfy_batch`, `voice_batch`)가 보이면, GPU 작업(ComfyUI, VoiceStudio)은 반드시 그 도구로 한 배치에 모아서 제출한다. ComfyUI/VoiceStudio 를 직접 띄우지 않고, 로컬 LLM 을 직접 끄지 않는다(`strata_stop`, `/unload`, `taskkill`). 워크플로우는 세션 안에서 확정한 뒤 API 형식 JSON(ComfyUI 의 'Export (API)')으로 저장하고 그 경로를 넘긴다. 게이트 호출은 수 분에서 수십 분 걸리고 그동안 LLM 은 꺼져 있다. 끝난 뒤 첫 응답이 20~60초 느린 것은 정상이다(프롬프트를 다시 읽는다). 도구가 보이지 않으면 게이트가 꺼진 것이니, GPU 작업을 하기 전에 사용자에게 먼저 묻는다.
```

English:

```text
If the `gpu-gate` tools (`comfy_batch`, `voice_batch`) are available, every GPU job (ComfyUI, VoiceStudio) must be submitted through them in one batch. Never start ComfyUI/VoiceStudio yourself and never stop the local LLM (`strata_stop`, `/unload`, `taskkill`). Finalize workflows in the session, save them as API-format JSON (ComfyUI 'Export (API)') and pass the paths. A gate call takes minutes to tens of minutes and the LLM is off meanwhile; the first reply afterwards being 20-60 s slower is normal (prompt re-read). If the tools are not visible, the gate is off - ask the user before any GPU job.
```

## 등록이 됐는지 확인

클라이언트를 새로 시작해 MCP 서버 목록에 `gpu-gate` 가 보이고 도구가 세 개인지 확인한다. 에이전트에게 `gate_status` 를 불러 보게 하면 가장 안전하다. 읽기 전용이라 아무것도 켜거나 끄지 않고, Strata·ComfyUI·VoiceStudio 의 상태와 마지막 배치 결과를 돌려준다.
