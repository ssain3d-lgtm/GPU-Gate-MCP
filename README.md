# GPU-Gate-MCP

로컬 LLM 서버(Strata)를 잠시 내려 GPU 를 비운 채로 ComfyUI/VoiceStudio 배치를 돌리고, 끝나면 LLM 을 다시 올려 주는 MCP stdio 서버.

## English summary

**What it is.** GPU-Gate-MCP is a small MCP stdio server (Python 3.10+, standard library only) for people who run a local LLM server and ComfyUI/VoiceStudio on the same GPU. An AI-agent client (opencode, Qwen Code, Hermes, ...) registers it and calls `comfy_batch` or `voice_batch` with all the jobs of a task. A detached worker unloads the local LLM server (Strata) once its running turn is finished, starts ComfyUI/VoiceStudio from your own start script, runs the jobs, stops the executor and always reloads the LLM, even if the client died or a step failed. The call blocks until the batch is done and returns per-job status and output paths; `gate_status` shows what the gate is doing. Tested on Windows. The full documentation below is in Korean; the agent instruction block in [docs/clients.md](docs/clients.md) is bilingual.

**Install.**

```
git clone <repo-url> GPU-Gate-MCP && cd GPU-Gate-MCP
python gpu_gate_server.py --init      # writes config.json; edit it for this machine
python gpu_gate_server.py --selftest  # then register the server in your client (docs/clients.md)
```

---

## 무엇이고 왜 필요한가

GPU 한 장에서 로컬 LLM 과 ComfyUI/VoiceStudio 를 함께 올려 두면 VRAM 이 모자란다. 에이전트에게 이미지나 음성 작업을 시키려면 누군가 LLM 을 내리고, 작업 도구를 띄우고, 작업이 끝나면 도구를 끄고, LLM 을 다시 올려야 한다. 이 순서를 에이전트가 직접 하게 두면 LLM 을 끈 채로 잊거나, 작업 도중 다른 요청이 GPU 를 빼앗는 일이 생긴다.

GPU-Gate-MCP 는 이 순서 전체를 도구 호출 하나로 묶는다. 에이전트는 `comfy_batch` 또는 `voice_batch` 에 한 작업의 잡을 전부 담아 한 번 호출하고, 호출은 배치가 끝날 때까지 돌아오지 않는다(blocking). 실제 일은 서버와 분리된(detached) 워커 프로세스가 하므로, 클라이언트가 중간에 닫혀도 워커는 끝까지 가서 LLM 을 다시 올린다.

파이썬 표준 라이브러리만 쓰고 설치할 외부 패키지가 없다. ComfyUI 는 사용자가 쓰던 시작 `.bat` 을 그대로 읽어 같은 환경과 인자로 띄우므로, 게이트용으로 따로 설정을 옮길 필요가 없다. 현재 LLM 쪽 어댑터는 Strata 의 HTTP API 를 기준으로 한다([Strata 에게 바라는 것](#strata-에게-바라는-것)).

## 동작 순서

한 배치는 별도 워커 프로세스에서 다음 순서로 진행된다.

0. **점검** — 잡 목록과 `comfy_variant` 옵션(그 `.bat` 이 `comfyui.dir` 에 있는지)을 확인하고, `other_llm_ports` 의 다른 LLM 서버가 응답하면 배치를 거부한다(`force` 로 무시할 수 있다). 여기서 거부되면 Strata 는 건드리지 않는다.
1. **Strata 내리기** — 진행 중인 응답(턴)이 끝나기를 기다렸다가 `POST /unload?wait=1` 로 엔진을 내린다. 서버가 바쁘면 1분마다 다시 시도하고, 15분이 지나도 바쁘면 `strata pause failed: busy` 로 배치를 포기한다(LLM 은 건드리지 않은 상태 그대로).
2. **실행기 시작** — ComfyUI(또는 VoiceStudio)를 사용자의 시작 스크립트로 띄우고 응답할 때까지 기다린다. 이미 떠 있으면 그것을 쓴다. 응답은 없지만 그 포트를 누가 이미 잡고 있으면(밖에서 막 띄운 ComfyUI 가 아직 뜨는 중) 새로 띄우지 않고 `start_timeout_s` 까지 기다렸다가 그것을 쓴다(외부 실행기로 다룬다).
3. **잡 실행** — 잡을 순서대로 실행한다. 연달은 워크플로우는 한 묶음으로 큐에 넣고 순서대로 기다리며, 명령(command) 잡은 앞선 잡이 모두 끝난 뒤 하나씩 실행한다.
4. **정리** — 큐에 남은 프롬프트를 지우고, ComfyUI 의 `/free` 로 모델을 내리고, 실행기를 끈다(`keep_executor` 인 경우 제외). 끈 프로세스가 VRAM 을 놓았는지 최대 60초 확인한다.
5. **Strata 올리기** — `POST /load` 로 엔진을 다시 올리고 `/health` 가 `loaded` 가 될 때까지 기다린다. **항상 실행된다.** 잡이 실패하거나 취소되거나 클라이언트가 사라져도 4~5단계는 `finally` 로 보장된다.
6. **결과 기록** — 결과를 배치 파일과 `state.json` 에 쓰고, 기다리던 호출에 돌려준다.

Strata 가 처음부터 응답하지 않으면(`GET /health` 무응답) 1·5단계를 건너뛰고 배치만 돌린다(`strata.server` 가 `false`). 배치 도중 사용자가 LLM 과 대화를 시작해 엔진이 다시 올라오면 경고 한 줄만 남기고 계속 진행하며, 다시 내리지 않는다.

## 설치

필요한 것: Python 3.10 이상(외부 패키지 없음), Strata(로컬 LLM 서버), ComfyUI 와 그 시작 `.bat`, 선택으로 VoiceStudio. 검증된 플랫폼은 Windows 이며 POSIX 분기는 있으나 확인하지 못했다.

```
git clone <repo-url> GPU-Gate-MCP
cd GPU-Gate-MCP
python gpu_gate_server.py --init
```

`pip install .` 으로 패키지만 설치했다면 같은 일을 `python -m gpu_gate --init` 처럼 부른다(진입점 스크립트는 만들어지지 않는다).

1. **설정 만들기** — `--init` 은 예시 `config.json` 을 [상태 디렉터리](#파일-규약)에 만든다. 이미 있으면 덮어쓰지 않는다. 다른 위치의 파일을 쓰려면 `--config <경로>` 또는 환경변수 `GPU_GATE_CONFIG`.
2. **설정 고치기** — 예시 값은 자리표시자다. 이 PC 의 ComfyUI 폴더와 시작 `.bat`, VoiceStudio 경로로 바꾼다. **적어 둔 경로는 실제로 있어야 한다.** 없으면 `voicestudio.dir does not exist: ...` 처럼 키 이름을 대고 실패한다. VoiceStudio 를 쓰지 않으면 `voicestudio` 절을 통째로 지운다. 반대로 `comfyui` 는 필수라서, `voice_batch` 만 쓰더라도 `bat` 이나 `command` 중 하나는 있어야 설정이 통과한다.
3. **점검** — `python gpu_gate_server.py --selftest` 는 설정을 읽고, Strata·ComfyUI·VoiceStudio 에 응답이 있는지 물어보고(아무것도 시작하지 않는다), 프로세스 안에서 MCP `initialize`/`tools/list` 를 한 번 돌려 본다.

   ```
   config       <상태 디렉터리>/config.json
   home         <상태 디렉터리>
   strata       http://127.0.0.1:8080  loaded
   comfyui      http://127.0.0.1:8188  down
   voicestudio  http://127.0.0.1:3900  down
   mcp          protocol 2025-06-18, tools: comfy_batch, voice_batch, gate_status
   ```

   설정이 없으면 `--init` 을 안내하는 한 줄 오류와 함께 종료 코드 1 로 끝난다.
4. **클라이언트에 등록** — [docs/clients.md](docs/clients.md) (opencode, Qwen Code, Hermes 스니펫과 에이전트 지침).

직접 실행하면(`python gpu_gate_server.py`) stdin/stdout 으로 MCP 를 말하는 서버가 된다. stdout 에는 프로토콜 줄만 나가고 진단은 모두 stderr 로 간다.

## 설정

`config.json` 은 이 기계에 묶인 값을 전부 담는다. 빠진 절과 키는 기본값을 쓰고, 모르는 키는 무시한다. 잘못된 값은 키 이름과 파일 경로를 대고 시작 단계에서 실패한다.

| 키 | 예시 값 | 설명 |
|---|---|---|
| `strata.url` | `http://127.0.0.1:8080` | Strata 주소. |
| `strata.api_key` | `null` | 있으면 `Authorization: Bearer` 로 보낸다. 로그에는 남기지 않는다. |
| `strata.unload_wait_s` | `300` | `/unload?wait=1` 요청이 기다리는 시간(여기에 30초가 더해진다). |
| `strata.load_timeout_s` | `400` | `/load` 뒤 `/health` 가 `loaded` 가 되기를 기다리는 시간. 실패하면 15초 간격으로 모두 3번 시도한다. |
| `comfyui.dir` | `C:/ComfyUI` | ComfyUI 시작 `.bat` 이 있는 폴더. `bat` 을 쓰면 필수이고, 적으면 실제로 있어야 한다. `comfy_variant` 도 이 폴더에서 찾는다. |
| `comfyui.bat` | `Start ComfyUI.bat` | `dir` 안의 시작 `.bat` 이름. |
| `comfyui.command` | `null` | 문자열 목록(argv). 있으면 `bat` 보다 우선하며 그대로 실행한다(`dir` 에서). `bat`/`command` 중 하나는 있어야 한다. |
| `comfyui.url` | `http://127.0.0.1:8188` | ComfyUI 주소. |
| `comfyui.start_timeout_s` | `300` | 시작 뒤 응답을 기다리는 시간. |
| `comfyui.output_dir` | `null` | ComfyUI 가 결과를 쓰는 폴더. `null` 이면 `<dir>/ComfyUI/output` (포터블 구성). |
| `voicestudio.dir` | `C:/VoiceStudio/app` | VoiceStudio 작업 폴더. 적으면 실제로 있어야 한다. |
| `voicestudio.start_script` | `C:/VoiceStudio/start_backend.ps1` | 백엔드를 포그라운드로 띄우는 스크립트. `.ps1` 은 `powershell -NoProfile -ExecutionPolicy Bypass -File` 로, 그 밖은 그대로 실행한다. 적으면 실제로 있어야 한다. |
| `voicestudio.batch_script` | `C:/VoiceStudio/voice_batch.py` | 잡을 처리하는 배치 스크립트([voice_batch](#voice_batch) 참고). 적으면 실제로 있어야 한다. |
| `voicestudio.url` | `http://127.0.0.1:3900` | VoiceStudio 주소(`GET /health` 가 `{"status": "ok"}`). |
| `voicestudio.start_timeout_s` | `180` | 시작 뒤 응답을 기다리는 시간. |
| `limits.job_timeout_s` | `1800` | 잡 하나의 제한 시간(옵션 `job_timeout_s` 로 호출마다 바꿈). |
| `limits.batch_timeout_s` | `9000` | 배치 전체의 제한 시간(옵션 `batch_timeout_s` 로 바꿈). 클라이언트의 도구 타임아웃은 이보다 길어야 한다. |
| `other_llm_ports` | `[8081]` | 같은 PC 의 다른 로컬 LLM 서버 포트(127.0.0.1). 하나라도 연결되면 배치를 거부한다. |

환경변수는 둘이다.

| 변수 | 뜻 |
|---|---|
| `GPU_GATE_HOME` | 상태 디렉터리(기본값은 아래 파일 규약 참고). |
| `GPU_GATE_CONFIG` | 설정 파일 경로(기본값은 `<상태 디렉터리>/config.json`). `--config` 가 이 값을 정한다. |

ComfyUI 시작 `.bat` 은 이렇게 읽는다. `set NAME=값` 줄들은 환경변수로 모으고(`%~dp0`, `%VAR%` 는 풀어서), `main.py` 로 끝나는 토큰이 있는 첫 줄을 실행 줄로 쓴다. 상대 경로는 `.bat` 폴더 기준 절대 경로로 바꾸고, `--disable-auto-launch` 가 없으면 붙인다(브라우저가 열리지 않게). `if`/`call`/`for`/`set /a` 같은 줄은 해석하지 않고 건너뛰므로, 가상환경을 `call ...activate` 로 켜는 `.bat` 이라면 실행 줄에 파이썬 경로를 직접 적거나 `comfyui.command` 를 쓴다. 로그에는 환경변수의 이름만 남고 값은 남지 않는다(실행 줄로 풀려 들어간 4자 이상의 값은 `<이름>` 으로 가려진다).

## Strata 에게 바라는 것

게이트는 Strata 에게 아래 HTTP 호출만 한다. 호환되는 서버라면 다른 LLM 서버에도 붙을 수 있다.

| 호출 | 기대하는 응답 |
|---|---|
| `GET /health` | 200 과 JSON 의 불리언 `loaded`. 응답이 없으면 서버가 없는 것으로 본다. |
| `GET /status` | JSON 의 `busy` (`gate_status` 가 쓴다). |
| `POST /unload?wait=1` | 200 `{"status": "unloaded"}` 또는 `"not loaded"`. 요청을 처리 중이면 409 또는 200 `{"status": "busy"}`. |
| `POST /load` | 200. 뒤이어 `/health` 가 `loaded: true`. |

## 도구

서버는 도구 세 개를 낸다. 호출 결과는 JSON 텍스트(`content`)와 같은 내용의 `structuredContent` 로 오고, 도구 오류일 때만 `isError` 가 참이다. **잡이 실패한 배치도 오류가 아니라 `ok: false` 인 정상 결과로 돌아온다.**

| 도구 | 하는 일 |
|---|---|
| `comfy_batch(jobs, options?)` | ComfyUI 잡을 실행한다. 배치가 끝나야 돌아온다. |
| `voice_batch(jobs, options?)` | VoiceStudio 잡을 실행한다. 배치가 끝나야 돌아온다. |
| `gate_status()` | 읽기 전용 현황. 아무것도 바꾸지 않는다. |

인자는 `jobs`(1개 이상의 객체 배열, 필수)와 `options`(객체) 뿐이며 다른 이름은 `<이름>: unknown argument` 로 거절된다.

### comfy_batch

`jobs` 의 항목은 두 종류다.

```json
{"kind": "workflow", "path": "<API 형식 워크플로우 JSON 의 경로>", "name": "선택 이름"}
{"kind": "command", "command": ["python", "gen.py", "--arg"], "cwd": "선택", "timeout_s": 600}
```

- 워크플로우는 ComfyUI 의 **Export (API)** 로 저장한 JSON 이어야 한다. 일반 저장(UI 형식)은 `not an API-format workflow (save with 'Export (API)')` 로 그 잡만 실패한다.
- `command` 는 게이트가 직접 실행하는 프로그램이다(ComfyUI 가 켜져 있는 동안, 앞선 잡이 모두 끝난 뒤). 종료 코드 0 이면 `done`, 아니면 `error`(`exited <코드>`). `cwd` 가 있으면 존재해야 하고, `timeout_s` 가 없으면 `job_timeout_s` 를 쓴다. 출력은 로그에 쌓이고 `outputs` 는 비어 있다. 이 도구를 부를 수 있는 누구나 사용자 권한으로 아무 프로그램이나 실행할 수 있다(로컬 에이전트용 도구다).
- 잡 하나가 잘못돼도(파일 없음 등) 그 잡만 `error` 가 되고 나머지는 계속 간다. 단 `stop_on_error` 이면 첫 오류 뒤는 건너뛴다.

`options` (모두 선택):

| 옵션 | 기본 | 뜻 |
|---|---|---|
| `stop_on_error` | `false` | 첫 오류가 나면 나머지를 `skipped` 로 하고, 큐에 넣은 프롬프트는 지운다. |
| `job_timeout_s` | 설정의 `limits.job_timeout_s` | 잡 하나의 제한 시간. |
| `batch_timeout_s` | 설정의 `limits.batch_timeout_s` | 배치 전체의 제한 시간. 지나면 남은 잡은 `timeout`(`batch timeout`). |
| `keep_executor` | `false` | 배치 끝에 실행기를 끄지 않는다. **게이트가 띄우지 않은(이미 떠 있던) 실행기에만 적용된다.** 게이트가 띄운 것은 항상 끈다. |
| `force` | `false` | `other_llm_ports` 의 다른 LLM 서버가 응답해도 진행한다(경고가 남는다). |
| `comfy_variant` | 설정의 `comfyui.bat` | `comfyui.dir` 안의 다른 시작 `.bat` 파일 이름(경로 아님). 없으면 `comfy_variant not found: <이름>`. `comfyui.command` 가 있으면 무시된다. |

**이미 떠 있던 ComfyUI 도 배치가 끝나면 꺼진다**(그 포트를 잡고 있는 프로세스를 종료). 남기려면 `keep_executor` 를 쓴다.

완료된 워크플로우의 `outputs` 는 ComfyUI 기록에 나온 저장 파일(이미지·gif·영상·오디오, 종류가 `output` 인 것)의 경로다. 임시·입력 파일은 제외한다.

### voice_batch

`jobs` 의 항목은 사용자의 `voice_batch.py` 가 읽는 `jobs.json` 항목 그대로다.

```json
{"text": "읽을 문장", "out": "result/line1.wav", "profile_id": "...", "instruct": "...", "speed": 1.0, "language": "ko"}
```

게이트는 잡을 `voice_jobs.json` 에 쓰고 `python <voicestudio.batch_script> <그 파일>` 을 `voicestudio.dir` 에서 한 번 실행한다(게이트를 돌리는 것과 같은 파이썬으로. 배치 폴더 `batches/<batch_id>/` 에 `voice_jobs.json`, `voice_batch.log` 가 남는다). 스크립트 환경에는 `VOICESTUDIO_URL`, (dir 가 있으면) `VOICESTUDIO_ROOT`, `PYTHONUTF8=1` 이 들어간다. 스크립트는 잡마다 `out` 의 wav 를 쓰고 끝나면 TTS 모델을 내린 뒤 종료 코드 0 으로 끝나야 한다.

- 스크립트가 한 번에 모든 잡을 처리하므로 잡별 시간은 따로 재지 않는다(`seconds` 는 전체 실행 시간). 실행 시간은 `batch_timeout_s` 만 제한하고 `job_timeout_s` 는 영향이 없다.
- 잡은 종료 코드가 0 이고 `out` 파일이 있어야 `done` 이다(상대 경로는 스크립트 작업 폴더인 `voicestudio.dir` 기준). 종료 코드가 0 이 아니면 모든 잡이 `error`(`batch script exited <코드>`), 파일이 없으면 그 잡이 `error`(`output missing: <out>`).
- 옵션: `batch_timeout_s`, `keep_executor`, `force` (`stop_on_error`·`comfy_variant` 는 쓰이지 않는다). `voicestudio.start_script`/`batch_script` 가 없으면 해당 단계에서 `voicestudio.start_script is not set` 처럼 실패한다.

### 결과 형식

두 배치 도구가 돌려주는 결과다. 이 객체가 배치 파일의 `result` 와 `state.json` 의 `last_result` 에도 그대로 들어간다.

```json
{
  "batch_id": "20261017-210500-a3f1",
  "started": "2026-10-17T21:05:00+00:00",
  "finished": "2026-10-17T21:12:41+00:00",
  "ok": true,
  "strata": {"server": true, "paused_s": 4.2, "restored": true, "load_s": 38.5},
  "executor": {"kind": "comfyui", "started_by_gate": true, "cold_start_s": 21.3, "stopped": true},
  "jobs": [
    {"index": 0, "kind": "workflow", "name": "portrait.json", "status": "done", "seconds": 52.1,
     "outputs": ["C:/ComfyUI/ComfyUI/output/portrait_00001_.png"], "error": null}
  ],
  "warnings": [],
  "log": "<상태 디렉터리>/logs/20261017-210500-a3f1.log",
  "error": null
}
```

| 필드 | 뜻 |
|---|---|
| `ok` | 모든 잡이 `done` 이고, 배치 오류가 없고, Strata 가 없었거나 복구에 성공했을 때만 `true`. |
| `strata.server` | Strata 가 응답했는가(`false` 면 내리기·올리기를 건너뛴 것). 판단 전에는 `null`. |
| `strata.paused_s` / `restored` / `load_s` | LLM 이 내려가 있던 시간 / 다시 올라왔는가 / 올리는 데 걸린 시간. |
| `executor.kind` | `comfyui` 또는 `voicestudio`. |
| `executor.started_by_gate` / `cold_start_s` / `stopped` | 게이트가 띄웠는가 / 시작에 걸린 시간 / 끝에 껐는가. |
| `jobs[].kind` | `workflow`, `command`, `voice`. |
| `jobs[].name` | 지정한 이름, 없으면 워크플로우는 파일명, 명령은 프로그램명, 음성은 `out` 파일명. |
| `jobs[].status` | `done`, `error`, `timeout`, `cancelled`, `skipped`. |
| `jobs[].outputs` / `error` | 결과 파일 경로 목록 / 실패 이유 한 줄. |
| `warnings` | 예를 들어 `LLM came back during the batch at <시각>`, `previous batch <id> was aborted`, 실행기를 못 끈 경우. |
| `log` | 이 배치의 로그 파일. |
| `error` | 배치 단위의 실패 이유 한 줄(`other_llm_running: ports [8081]`, `strata pause failed: busy`, `comfyui not ready after 300 s` 등). 없으면 `null`. |

결과와 상태 파일(배치 파일, `state.json`)에는 경로·숫자·짧은 문자열만 담긴다. 시작 `.bat` 의 환경변수 값이나 `strata.api_key` 는 들어가지 않는다. 로그(`logs/<id>.log`)에는 실행기와 명령의 출력이 거르지 않고 그대로 쌓이며, `.bat` 에서 `set` 한 값을 가리는 것은 게이트가 쓰는 시작 줄(`comfyui: starting ...`)뿐이다.

### gate_status

```json
{
  "state": "running",
  "batch": {"batch_id": "...", "pid": 1234, "started": "...", "kind": "comfyui",
            "state": "running", "progress": {"done": 1, "total": 3, "current": "portrait.json"}},
  "strata": {"server": true, "loaded": false, "busy": false},
  "executor": {"comfyui": "up(gate)", "voicestudio": "down"},
  "gpu": [{"pid": 4321, "name": "python", "vram_mb": 21000}],
  "warnings": [],
  "last_result": null
}
```

- `state` 는 [상태값](#파일-규약) 중 하나다. 돌고 있는 배치가 있으면 그 배치의 상태, 없으면 `state.json` 의 값(처음에는 `idle`, 배치가 끝난 뒤에는 `done` 또는 `failed`). 워커가 죽은 채 남은 lock 이 있으면 `aborted` 와 경고를 보인다.
- `batch` 는 돌고 있는 배치(없으면 `null`).
- `strata` 의 `server` 는 응답 여부, `loaded`/`busy` 는 Strata 가 답한 값(모르면 `null`).
- `executor` 값은 `down`, `up(gate)`(돌고 있는 배치가 쓰고 있는 것. 배치 도중에는 누가 띄웠는지 구분하지 못한다), `up(external)`(외부에서 띄운 것). 설정을 못 읽으면 `null`.
- `gpu` 는 VRAM 을 300 MiB 이상 쓰는 프로세스 목록(큰 순서, Windows 의 성능 카운터로 읽음). 못 읽으면 `null`. LLM 이 올라와 있는 동안 다른 프로세스가 300 MiB 이상(데스크톱 창 관리자 `dwm` 은 3 GiB 초과) 잡고 있으면 경고를 남긴다.
- 조회가 하나 실패해도 예외 대신 `warnings` 에 적고 나머지를 돌려준다.
- 보고만 한다. 워커가 죽어 `aborted` 로 남은 배치 뒤에 LLM 이 내려가 있어도 `gate_status` 는 Strata 를 다시 올리지 않는다(다음 배치 호출이 올린다).

### 도구 오류

배치를 시작하기 전에 막히면 `{"error": "<한 줄>"}` 가 `isError: true` 로 온다.

| 오류 | 뜻 |
|---|---|
| `busy` (+ `batch`) | 다른 배치가 돌고 있다. `batch` 에 그 배치의 id·pid·진행이 있다. |
| `config not found: <경로> - run: python gpu_gate_server.py --init` 등 | 설정이 없거나 잘못됐다. 메시지가 키와 파일을 댄다. |
| `worker died; see <로그>` | 워커가 결과를 쓰기 전에 사라졌다. 서버가 배치를 `aborted` 로 표시하고, LLM 이 내려가 있으면 다시 올린다. |
| `could not start the worker: ...` | 워커 프로세스를 띄우지 못했다. |
| `batch <id> ended without a result (state …); see <로그>` | 워커가 배치를 끝냈다고 표시했지만 결과를 남기지 않았다. 로그를 본다. |
| `jobs: required`, `jobs: expected a non-empty array` 등 | 인자가 스키마에 맞지 않는다. |

## 파일 규약

서버, 워커, 외부 도구(예: 설정 UI)는 한 상태 디렉터리의 파일로만 서로 이야기한다. 위치는 `GPU_GATE_HOME`, 없으면 Windows 는 `%LOCALAPPDATA%\gpu-gate`, 그 밖에는 `~/.local/share/gpu-gate`. 파일은 임시 파일에 쓴 뒤 바꿔치기로 갱신하므로 읽는 쪽이 반쯤 쓰인 파일을 볼 일은 없다.

| 경로 | 내용 |
|---|---|
| `config.json` | 설정. |
| `lock` | `{"batch_id", "pid", "started"}`. pid 가 살아 있으면 배치가 돌고 있다는 뜻. pid 가 죽은 lock 은 다음 배치가 넘겨받으며 이전 배치를 `aborted` 로 표시한다. |
| `state.json` | `{"state", "batch_id", "updated", "last_result"}`. |
| `batches/<batch_id>.json` | 배치 파일: `batch_id, kind, jobs, options, state, progress{done,total,current}, warnings, result, started, finished, pid`. `kind` 는 `comfyui` 또는 `voicestudio`. |
| `batches/<batch_id>.cancel` | 파일이 있으면 취소 요청. 외부 도구는 빈 파일을 만들면 된다. |
| `batches/<batch_id>/` | 음성 배치의 작업 폴더(`voice_jobs.json`, `voice_batch.log`). |
| `logs/<batch_id>.log` | 서버·워커 로그, 실행기와 명령의 출력. |

`batch_id` 는 `YYYYMMDD-HHMMSS-<16진수 4자리>` (지역 시간) 이다. 오래된 배치 파일과 로그를 지우는 기능은 없다.

상태값: `idle`, `pausing`(LLM 내리는 중), `starting`(실행기 시작), `running`(잡 실행), `cleanup`(정리), `restoring`(LLM 올리는 중), `done`, `failed`, `aborted`. 배치 파일에는 워커가 첫 전환을 하기 전까지 `pending` 이 보인다. 정상 종료는 `done`, 결과가 `ok: false` 이면 `failed`.

## 취소

클라이언트가 실행 중인 호출에 대해 `notifications/cancelled` 를 보내면(에이전트에서 Esc 를 누르거나 클라이언트의 도구 타임아웃이 지났을 때) 서버가 그 배치의 `.cancel` 파일을 만든다. 워커는 잡을 실행하는 동안 이 파일을 확인하고, 있으면 큐에 남은 프롬프트를 지우고, 실행 중인 것은 중단(`/interrupt`)하고, 실행기를 끄고, Strata 를 다시 올린다. 외부 도구가 `.cancel` 파일을 직접 만들어도 같다. LLM 을 내리는 중에 취소하면 실행기를 띄우지 않고(경고 `cancelled before the executor started`), 밖에서 띄운 실행기가 뜨기를 기다리는 중이면 그 기다림을 바로 멈추며, 게이트가 띄운 실행기가 시작하는 중이면 준비되는 대로 잡을 하나도 돌리지 않고 끈다(경고 `cancelled before the jobs started`). 어느 경우든 잡은 `cancelled` 가 되고 Strata 는 다시 올라간다. 취소된 호출에는 응답을 보내지 않으며(MCP 규칙), 결과는 배치 파일과 `state.json` 에 남는다.

**클라이언트를 닫는 것은 취소가 아니다.** 서버는 입력이 끊기면 곧(약 1초 안에) 끝나지만 워커는 계속 돌아 잡을 마치고 Strata 를 올린다. 결과는 `gate_status` 의 `last_result` 로 볼 수 있다.

## 알려진 한계

- **첫 응답이 느리다.** LLM 을 내리면 대화의 KV 캐시가 사라진다. 배치가 끝난 뒤 첫 응답은 프롬프트를 처음부터 다시 읽으므로, 컨텍스트가 100K 토큰 이상이면 20~60초가 더 걸린다.
- **LLM 과 GPU 작업은 동시에 못 돌린다.** 한 장의 GPU 에서는 그렇다. 배치 중에는 LLM 이 응답하지 못한다(그 사이 대화를 시작하면 경고만 남고 배치는 계속 간다).
- **다른 LLM 서버는 막는다.** `other_llm_ports` 의 서버가 응답하면 `force` 가 아닌 한 배치를 거부한다. 게이트는 Strata 만 내리고 올린다.
- **한 번에 배치 하나.** 다른 클라이언트에서 부르면 `busy`.
- **진행 알림은 토큰이 있을 때만.** 클라이언트가 호출에 progress 토큰(`params._meta.progressToken`)을 실어 보내면, 서버는 배치를 기다리는 동안 15초마다(첫 알림은 15초 뒤) `notifications/progress` 를 보낸다. `progress` 는 매번 커지는 알림 번호이고 `total` 은 없으며, 상태와 끝난 잡 수/전체 잡 수는 `message` 에 있다. 토큰이 없으면 보내지 않는다. 그래도 클라이언트의 도구 타임아웃(그리고 Qwen Code 의 무응답 타이머, Hermes 의 도구 마감 시간)이 배치보다 짧으면 클라이언트가 호출을 끊고 취소를 보낸다. 클라이언트별 설정은 [docs/clients.md](docs/clients.md).
- **Windows 우선.** Windows 에서만 검증했다. POSIX 분기(프로세스 종료, 포트 조회)는 있으나 확인하지 않았고, GPU 프로세스별 VRAM 조회는 Windows 성능 카운터에 기댄다.
- **시작 `.bat` 해석은 단순하다.** `set` 줄과 `main.py` 실행 줄만 이해한다. 그 이상이 필요하면 `comfyui.command`.
- **`voice_batch` 는 잡별 시간·부분 결과가 없다.** 시간 초과나 취소면 일부 파일이 이미 쓰였어도 모든 잡의 `outputs` 가 비어 있다.
- **오래된 파일은 쌓인다.** 배치 파일과 로그의 자동 정리는 없다.

## 문제 해결

| 증상 | 원인과 조치 |
|---|---|
| `busy` | 다른 배치가 돌고 있다. `gate_status` 로 진행을 보고 기다린다. 워커가 죽어 남은 lock 은 다음 호출이 넘겨받으며 `previous batch <id> was aborted` 경고가 붙고, LLM 이 내려가 있으면 다시 올린다. 돌고 있는 배치가 없는데도 계속 `busy` 이면 lock 의 pid 를 다른 프로그램이 재사용한 것일 수 있으니 상태 디렉터리의 `lock` 파일을 지운다. |
| 결과 `error`: `other_llm_running: ports [...]` | `other_llm_ports` 의 다른 LLM 서버가 응답한다. 그 서버를 끄거나, 알고도 진행하려면 `options.force`. |
| `worker died; see <로그>` | 워커가 결과 없이 사라졌다. 로그 파일을 열어 마지막 줄을 본다. LLM 은 서버가 다시 올려 둔다. |
| `config not found: ... run: python gpu_gate_server.py --init` | 설정이 없다. `--init` 으로 만들고 고친 뒤 `--selftest`. |
| `<키> does not exist: <경로>` 등 설정 오류 | 메시지의 키와 파일을 고친다. 쓰지 않는 VoiceStudio 는 `voicestudio` 절을 지운다. |
| 결과 `error`: `comfy_variant not found: <이름>` | `comfyui.dir` 안에 그 이름의 `.bat` 이 없다(파일 이름만 쓴다). LLM 을 내리기 전에 확인하므로 LLM 은 그대로다. |
| 잡 `error`: `not an API-format workflow (save with 'Export (API)')` | UI 형식으로 저장한 워크플로우다. ComfyUI 에서 **Export (API)** 로 다시 저장한다. |
| 잡 `error`: `workflow file not found: <경로>` | 경로가 게이트가 도는 PC 에서 존재하지 않는다. 절대 경로를 넘긴다. |
| 결과 `error`: `strata pause failed: busy` | Strata 가 긴 생성을 하고 있었다. 게이트는 응답이 끝나기를 15분까지 기다린 뒤 포기하며, 이때 LLM 은 건드리지 않았다. 나중에 다시 부른다. |
| 결과 `error`: `comfyui not ready after N s` / `comfyui exited with code N before it was ready` | ComfyUI 가 제때 뜨지 못했다. 로그에 ComfyUI 의 출력이 있다. 느린 PC 이면 `comfyui.start_timeout_s` 를 늘린다. |
| 결과 `error`: `comfyui on port N (started outside the gate) did not answer within N s` | 그 포트를 다른 프로세스가 잡고 있는데 응답하지 않았다(밖에서 띄운 ComfyUI 가 멈췄거나, 다른 프로그램이 포트를 쓴다). 게이트는 자기가 띄우지 않은 그 프로세스를 끄지 않는다. 직접 확인하고 끈 뒤 다시 부른다. |
| 경고 `executor was already starting on port N; reused it` / `executor was already starting (pid N); reused it` | 밖에서 막 띄운 실행기가 아직 뜨는 중이라 기다렸다가 썼다. 정상이다. 포트를 아직 잡지 않은 것(ComfyUI 는 시작 뒤 40~60초에야 잡는다)도 프로세스 명령줄(`comfyui.dir` 아래의 `main.py`, VoiceStudio 는 `uvicorn backend.main`)로 찾아낸다. `keep_executor` 가 아니면 배치 뒤에 그것을 끈다. |
| 경고 `executor on port N is not the process the gate started (pid N); treating it as external` | 게이트가 띄운 것과 밖에서 띄운 것이 함께 떴고 밖의 것이 포트를 잡았다. 게이트는 준비 뒤 포트를 잡은 프로세스가 자기 것(또는 그 자식)인지 확인하고, 아니면 자기 것을 끄고 그 실행기를 외부 것으로 다룬다(잡은 거기서 돌고, 배치 뒤 포트로 끈다). |
| 결과 `error`: `executor died during the batch` | 실행기 프로세스가 배치 도중 사라졌다(잡 `error`: `comfyui process ended`). 로그에서 ComfyUI 의 마지막 출력을 본다. |
| `strata.restored: false`, 결과 `error`: `strata resume failed: ...` | LLM 이 올라오지 않았다(3번 시도). 로그를 보고 Strata 에 직접 `POST /load` 를 보내거나 재시작한다. |
| 에이전트가 도중에 호출을 끊었다 | 클라이언트의 도구 타임아웃이 배치보다 짧다. 정확히 5분(Qwen Code `mcp.toolIdleTimeoutMs`)이나 7분(Hermes `timeouts.tools.sequential_call`)에 끊겼다면 서버 항목의 `timeout` 이 아닌 그 키다. [docs/clients.md](docs/clients.md) 의 값으로 늘린다. |

## 개발

```
python -m pytest -q
```

테스트는 가짜 Strata·ComfyUI·VoiceStudio 서버만 쓰므로 GPU 도 실제 서버도 필요 없다. 실제 환경 확인 절차는 [docs/QA.md](docs/QA.md).

| 모듈 | 역할 |
|---|---|
| `gpu_gate/server.py` | MCP stdio 서버, 도구 세 개, 배치 시작·대기·취소 전달 |
| `gpu_gate/worker.py` | 분리된 워커: 6단계 순서와 결과 기록 |
| `gpu_gate/state.py` | 상태 디렉터리(lock, state.json, 배치 파일, 취소 플래그) |
| `gpu_gate/config.py` | 설정 읽기와 검증 |
| `gpu_gate/strata.py`, `comfyui.py`, `voicestudio.py` | 각 서버 어댑터 |
| `gpu_gate/procs.py`, `gpu.py`, `http.py` | 프로세스, GPU 조회, HTTP 도우미 |

## 라이선스

MIT. [LICENSE](LICENSE) 참고.
