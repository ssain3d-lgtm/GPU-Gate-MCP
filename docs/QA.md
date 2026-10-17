# 실사용 점검표

자동 테스트(`python -m pytest -q`)는 가짜 서버만 쓴다. 이 표는 실제 Strata, ComfyUI, VoiceStudio, 클라이언트로 한 번씩 확인하는 절차다. 결과 칸은 `PENDING` 에서 시작해 확인하면 `PASS` 또는 `FAIL` 로 바꾸고, 메모 칸에 날짜와 관찰한 내용을 적는다.

## 준비

- Strata 가 올라와 있고(`GET /health` 의 `loaded` 가 참), ComfyUI 시작 `.bat` 과 VoiceStudio 가 설정돼 있다. `python gpu_gate_server.py --selftest` 가 통과한다.
- 작고 빠른 API 형식 워크플로우(ComfyUI 의 Export (API)) 두 개와, 음성 한 줄짜리 잡을 준비한다.
- opencode, Qwen Code, Hermes 에 [docs/clients.md](clients.md) 대로 등록해 둔다.
- 배치를 보는 곳: `gate_status` 도구, 상태 디렉터리의 `state.json`, `batches/<id>.json`, `logs/<id>.log` ([README](../README.md#파일-규약)). Strata 쪽은 엔진 로그와 `GET /health`.

## 점검

| 번호 | 절차 | 기대 결과 | 결과 | 메모 |
|---|---|---|---|---|
| 1 | opencode 에서 에이전트에게 워크플로우 두 개로 `comfy_batch` 를 부르게 한다. | Strata 엔진 로그에 unload 다음 "engine started" 가 찍힌다. 결과 `outputs` 의 파일이 모두 있다. 배치 뒤 `GET /health` 의 `loaded` 가 참이고 다음 대화가 정상이다(첫 응답은 프롬프트를 다시 읽느라 느리다). | PENDING | |
| 2 | Qwen Code 에서 1번과 같은 배치를 돌린다. 도구 타임아웃(`timeout` 10800000)이 먹었는지 보려면 기본값 10분보다 오래 걸리는 배치로 한다. | 1번과 같다. 10분이 넘어도 호출이 끊기지 않고 끝까지 간다. | PENDING | |
| 3 | Hermes 에서 1번과 같은 배치를 돌린다(`timeout` 10800). | 1번과 같다. 300초가 넘어도 호출이 끊기지 않는다. | PENDING | |
| 4 | 배치의 잡이 실행되는 도중에 취소한다: 설정 UI 의 취소 버튼, 또는 에이전트에서 Esc(호출 취소). | 큐에 남은 프롬프트가 지워지고 실행 중인 것은 중단된다. 실행기가 꺼지고 Strata 가 복구된다(`GET /health` 의 `loaded` 가 참). 결과는 `ok: false` 이고 취소된 잡의 `status` 가 `cancelled` 이며, 결과가 배치 파일과 `state.json` 에 남는다. | PENDING | |
| 5 | ComfyUI 시작 실패를 일부러 만든다(예: 없는 `comfy_variant` 이름, 또는 실행 줄이 틀린 `.bat`). | 잡은 하나도 실행되지 않고(전부 `skipped`) 정리와 Strata 복구가 일어나며(`strata.restored` 참), 결과 `error` 에 이유가 있다(예: `comfy_variant not found: <이름>`). | PENDING | |
| 6 | 배치가 도는 동안 다른 클라이언트에서 `comfy_batch` 또는 `voice_batch` 를 부른다. | 곧바로 도구 오류 `busy` 가 오고 돌고 있는 배치의 정보가 함께 온다. 원래 배치는 영향 없이 끝까지 간다. | PENDING | |
| 7 | Strata 가 긴 응답을 생성하는 도중에 배치를 부른다. | 게이트가 그 응답이 끝나기를 기다린 뒤에 내린다(Strata 가 바쁘다고 답하면 1분마다 다시 시도, 최대 15분). 응답이 잘리지 않고, 결과 `strata.paused_s` 에 기다린 시간이 들어간다. | PENDING | |
| 8 | Strata 서버를 끈 채로 배치를 부른다. | 결과 `strata.server` 가 `false` 이고(내리기와 올리기는 건너뜀) 잡은 정상 실행돼 `ok: true` 로 끝난다. | PENDING | |
| 9 | `voice_batch` 로 음성 잡 하나를 돌린다. | `out` 의 wav 가 만들어지고 잡의 `status` 가 `done` 이다. 배치 뒤 VoiceStudio 의 포트(기본 3900)를 잡고 있는 프로세스가 없고 Strata 가 복구된다. | PENDING | |
| 10 | (게이트의 상태 디렉터리를 읽는 설정 UI 를 쓰는 경우) 배치 전, 중, 후에 UI 의 게이트 카드를 본다. | 카드가 현재 상태(`pausing`, `starting`, `running`, `cleanup`, `restoring`, `done`)와 경고, 클라이언트별 켜기/끄기 토글을 보여 준다. | PENDING | |
| 11 | 배치가 도는 동안 Strata 와 대화를 시도해 엔진이 다시 올라오게 한다. | 결과 `warnings` 에 `LLM came back during the batch at <시각>` 한 줄만 남고 배치는 끝까지 간다. 게이트는 LLM 을 다시 내리지 않으며, 이미 올라와 있으니 복구는 성공으로 센다(`strata.restored` 참). | PENDING | |
| 12 | 배치가 도는 도중에 클라이언트를 닫는다. | 워커는 계속 돌아 잡을 마치고 Strata 를 복구한다. 클라이언트를 다시 열어 `gate_status` 를 부르면(또는 설정 UI 카드와 `state.json` 에서) `last_result` 로 결과가 보인다. | PENDING | |
| 13 | `other_llm_ports` 의 포트에 다른 LLM 서버를 띄운 채로 배치를 부르고, 이어서 `options.force` 를 참으로 해서 다시 부른다. | 첫 호출은 거부된다: 결과 `error` 가 `other_llm_running: ports [...]` 이고 잡은 모두 `skipped`, Strata 는 그대로다. `force` 로 부르면 진행하고 `warnings` 에 `going on because force is set` 경고가 남는다. | PENDING | |
| 14 | 외부에서 ComfyUI 를 미리 띄워 둔 채로 배치를 부른다. 이어서 `options.keep_executor` 를 참으로 해서 다시 부른다. | 미리 떠 있던 ComfyUI 를 재사용하고(`executor.started_by_gate` 가 `false`) 배치 끝에 끈다(`stopped` 가 `true`). `keep_executor` 로 부르면 배치 뒤에도 떠 있다(`stopped` 가 `false`). | PENDING | |
| 15 | 클라이언트마다 gpu-gate 를 끄고 새 세션을 열어 도구 목록을 보고, 다시 켜서 새 세션에서 본다([끄는 방법](clients.md#끄는-방법-요약)). | 끄면 다음 세션에 gpu-gate 도구가 없고, 켜면 있다(opencode, Qwen Code, Hermes 각각). | PENDING | |
| 16 | `python gpu_gate_server.py --selftest` 를 실행한다. 이어서 `GPU_GATE_HOME` 을 빈 폴더로 가리키고 같은 명령을 실행한다. | 앞의 것은 설정, 세 서버의 응답 여부, 프로토콜과 도구 이름을 표시하고 종료 코드 0 으로 끝나며 아무것도 시작하지 않는다. 뒤의 것은 `config not found: ... run: python gpu_gate_server.py --init` 한 줄을 stderr 에 내고 종료 코드 1 로 끝난다. | PENDING | |
| 17 | 저장소 루트에서 아래 명령을 실행한다. 또 시작 `.bat` 에 시험용 `set` 값을 넣고 배치를 한 번 돌린 뒤 그 값을 `logs/<id>.log`, `batches/<id>.json`, `state.json` 에서 찾는다. | 명령 출력이 없다(개발 PC 의 경로, 사용자 이름, 키가 저장소에 없다). 시험용 값은 게이트가 쓴 로그와 결과 어디에도 없고, `comfyui: starting ...` 줄에는 환경변수 이름만 보인다. | PENDING | |
| 18 | GPU 도 서버도 없는 PC 에서 `python -m pytest -q` 를 실행한다. | 모두 통과한다(가짜 서버만 쓴다). | PENDING | |

17번의 명령. 패턴을 대괄호로 쪼개 놓아서 이 문서 자신은 걸리지 않는다. bash 와 PowerShell 에서 그대로 쓴다.

```
git grep -n -i -E 'D[:]\\|sai[n]3|LM_STUD[I]O|s[k]-[a-z]|C[:]\\Use[r]s' -- . ':!LICENSE'
```

`LICENSE` 는 저작권자 이름이 들어 있어 빼 둔 것이다.
