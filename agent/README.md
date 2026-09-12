# 자율 작업 API

사용자가 찾은 [suphotP/chatgpt-api](https://github.com/suphotP/chatgpt-api)의 Web transport를 사용하고, 기존 CodexPro MCP가 실제 VM 작업을 실행합니다. 모델의 JSON 응답 → VM 도구 실행 → 실제 결과 회수 → 다음 모델 응답 → 검증까지 서비스가 이어갑니다. OpenAI 유료 API 키를 사용하지 않습니다. 기존 ChatGPT 계정의 사용량 제한은 그대로 적용됩니다.

## 현재 설치에서 사용

기존 웹의 **`/agent/`** 주소를 열고 기존 웹 접속 암호로 로그인합니다. a/b/c, 작업 폴더, 모델과 목표를 선택해 시작합니다. 화면의 기본 모델은 **Astra · Heavy**이며 실제 요청 추론값은 `max`입니다. Pro도 선택할 수 있습니다. 창을 닫아도 작업은 이어지며, 기록에서 결과·오류·검증 근거를 확인하거나 같은 작업을 중지/재개할 수 있습니다.

CLI 예시:

```bash
codexpro-agent run --account b --model gpt-6-astra-wm --effort max \
  --cwd /절대/작업폴더 --id my-task-001 '목표와 완료 확인 방법'
codexpro-agent status my-task-001
codexpro-agent events my-task-001
codexpro-agent resume my-task-001 --message '필요했던 추가 정보'
codexpro-agent cancel my-task-001
```

CLI/API에서 모델을 생략하면 원래 개선 대상인 `gpt-6-pro`를 선택합니다. **실험에는 `--model gpt-6-astra-wm --effort max`를 명시하거나 웹 화면의 기본값을 사용하세요.** Pro의 이용 제한을 만나도 다른 모델/계정으로 몰래 전환하지 않습니다.

## API

서비스: `http://127.0.0.1:7884`. 모든 작업 API에는 `Authorization: Bearer <개인 토큰>`이 필요합니다. 토큰은 `~/.local/state/codexpro-agent/api-token`에 0600으로 보관합니다. 암호·토큰·계정 세션을 모델 프롬프트나 Git에 넣지 마세요. CLI는 이 파일을 직접 읽으므로 복사할 필요가 없습니다.

`POST /v1/tasks`는 **접수** 결과를 돌려줍니다. 완료 응답으로 해석하면 안 됩니다. `Idempotency-Key` 헤더가 필수이며 같은 키/입력은 기존 작업을 돌려주고 다른 입력은 409로 거절합니다.

```json
{
  "account": "b",
  "model": "gpt-6-astra-wm",
  "effort": "max",
  "cwd": "/절대/작업폴더",
  "goal": "hello.txt에 hello와 줄바꿈을 쓰고 읽어서 확인해줘",
  "checks": [{"path": "hello.txt", "equals": "hello\n"}]
}
```

| 경로 | 의미 |
|---|---|
| `GET /v1/tasks` | 작업 목록 |
| `GET /v1/tasks/{id}` | 접수/실행/확인 필요/검증 후 완료/중지 상태 |
| `GET /v1/tasks/{id}/events?after=0` | 실제 모델 응답·도구 결과. 100개씩, `next_after`로 이어 읽기 |
| `GET /v1/tasks/{id}/receipts/{receipt_id}?offset=0&length=12000` | 보존한 출력 페이지. `next_offset`이 있으면 일부 출력 |
| `POST /v1/tasks/{id}/resume` | 기존 작업 재개. 필요시 `message`, 원대화 확인용 `conversation_id` 전달 |
| `POST /v1/tasks/{id}/cancel` | 추가 실행 중지 및 원래 Bash 명령의 취소 요청 |
| `GET /v1/accounts?model=gpt-6-astra-wm` | 로그인·모델·추론 강도·관찰된 사용량 제한 |

이것은 **작업 접수 API**입니다. 동기식 OpenAI `/v1/chat/completions` 호환 응답으로 가장하지 않습니다. 완료 판정은 성공한 읽기/명령 근거와 제출자가 지정한 파일 내용·SHA-256·부재 검사를 확인합니다. 작업 의미 전체를 자동 증명하지는 않으므로 `verification.scope`와 실제 기록을 확인할 수 있게 합니다.

## 실행·복구 방식

- 각 작업에 별도 MCP 세션과 별도 웹 대화를 사용합니다. 기존 연구 서비스의 슬롯/대화를 사용하거나 재시작하지 않습니다.
- 모델에 `vm_read`, `vm_write`, `vm_edit`, `vm_apply_patch`, `vm_bash`, `vm_show_changes`를 노출합니다. 웹챗 기본 도구와 혼동되지 않도록 이름과 작업 폴더 근거를 분리했습니다. `finish`, `needs_input`, `read_output`은 제어/출력 확인 도구입니다.
- 승인된 관련 VM 읽기·수정·명령·검증에는 추가 승인 단계를 넣지 않습니다. 기존 MCP의 full Bash, 생성 폴더 접근, 무제한 실행 시간 설정을 재사용합니다. 플랫폼의 정책/계정 제한을 해제하지 않습니다.
- 작업·모델 질문·도구 호출 번호를 실행 전에 저장합니다. API 재시작 뒤 진행 중 Bash는 같은 번호의 `bash_status`로 회수하며 재제출하지 않습니다. 일시적 조회 연결 오류는 최대 세 번 재연결하며 변경 명령을 다시 보내지 않습니다.
- 모델 전송 시간 초과는 명령 실패가 아닙니다. 원대화와 질문 ID에 연결된 최종 응답을 조회합니다. 서버 스트림이 끝났는데 최종 응답이 없으면 `model_result_missing`으로 명확히 표시합니다. 로그인/차단/사용량 오류도 별도 기록합니다.
- 파일 수정 호출의 결과를 받기 전에 연결이 끊겨 반영 여부를 모르면, 그 수정을 자동 반복하지 않습니다. 이 경우 실제 파일 확인이 필요하다는 상태가 남습니다. 임의의 파일 수정을 모두 자동 복구한다고 주장하지 않습니다.
- 중지는 추가 VM 실행을 멈추고 기존 명령에 취소를 요청합니다. 이미 시작된 웹 모델 생성까지 취소했다고 주장하지 않습니다.
- 모델 응답이 잘못된 형식이면 구체적인 오류와 회복 방법을 알려줍니다. 완료 근거 번호 뒤의 설명, 검증 뒤 같은 응답의 완료 호출은 허용합니다. 같은 완료 오류가 계속되면 무한 호출하지 않고 기록을 남깁니다.
- 도구 결과는 새 결과만 다음 메시지에 보냅니다. 출력 일부만 모델에 보낼 때 전체 길이·보존 위치·추가 조회 방법을 제공합니다. 기존 MCP가 이미 잘라 버린 출력은 되살릴 수 없으며, 긴 명령 출력이 필요하면 작업 파일에도 저장하도록 요청할 수 있습니다.

## 모델 식별

`gpt-6-pro`는 최종 응답의 실제 모델 식별자가 일치해야 합니다. Astra는 2026-09-12 실제 웹 응답에서 선택값 `gpt-6-astra-wm`, 내부 식별자 `gpt-5-6-auto-thinking`, 추론값 `max`로 관찰됐습니다. 이 명시적 웹 별칭만 허용하며 선택값·내부값·추론값을 모두 기록합니다. 내부 이름을 숨기거나 API 모델과 동일하다고 주장하지 않습니다. `max` 요청의 응답에서도 `max`가 확인되지 않으면 멈추고 이유를 표시합니다.

## 설치·운영

현재 VM은 이미 설치된 `chatgpt-api` Python 환경을 재사용합니다. 기반 소스는 `f998a6d83f324cb3187396dd7efced0c40f29601`, 현재 Node 프로젝트의 MCP SDK를 사용합니다. 기존 계정 캡처 기본 경로는 `~/.local/state/labor-federation/accounts/{a,b,c}/session.json`이며 읽기만 합니다. 별도 로그인 브라우저·유료 리소스·연구 슬롯을 만들지 않습니다.

```bash
# 이미 설치된 chatgpt-api Python 환경에서 실행
python -m agent.install
systemctl --user status codexpro-agent.service
python -m unittest agent.test_agent -q
```

설치 스크립트는 기존 unit/실행 파일이 있으면 먼저 백업합니다. 기존 MCP·계정 웹·연구 서버는 재시작하지 않습니다. `AGENT_ACCOUNTS_DIR`, `AGENT_MCP_URL`, `AGENT_MCP_TOKEN_FILE`로 기존 연결 위치를 지정할 수 있습니다. Python 환경/기존 연결을 다른 VM으로 옮기는 작업까지 자동 수행하는 설치기는 아닙니다.

기존 콘솔 Caddy 호스트에 추가한 경로:

```caddyfile
redir /agent /agent/ 308
handle_path /agent/* {
 reverse_proxy 127.0.0.1:7884
}
```

기존 웹 `/api/login`에 로컬로 암호를 검증하고, 브라우저에는 HttpOnly·Secure 쿠키를 발급합니다. API 토큰은 브라우저 자바스크립트에 전달하지 않습니다. 웹 작업 요청 번호는 탭 새로고침과 같은 내용의 반복 클릭에서도 재사용합니다.

되돌릴 때는 `codexpro-agent.service`만 중지/비활성화하고 Caddy에서 위 `/agent/` 경로만 제거 후 검증·reload합니다. 다른 변경이 섞인 오래된 Caddy 백업 전체를 덮어쓰지 마세요. 작업 기록은 개인 상태 폴더에 남습니다.
