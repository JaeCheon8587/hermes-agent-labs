# 작업 경로와 watcher 검증 체크리스트

대상 작업: `t_b3101772`
프로젝트 경로: `/home/jjc/.hermes/hermes-agent`

## 1) workspace 고정 확인
- [ ] 현재 작업 디렉터리가 `/home/jjc/.hermes/hermes-agent` 인지 확인한다.
- [ ] Kanban 작업의 `workspace_kind` 가 `dir` 인지 확인한다.
- [ ] Kanban 작업의 `workspace_path` 가 `/home/jjc/.hermes/hermes-agent` 와 일치하는지 확인한다.
- [ ] 작업 중 다른 경로로 이동하지 않았는지 `pwd` / `git rev-parse --show-toplevel` 로 재확인한다.

확인 명령 예시:
```bash
pwd
git rev-parse --show-toplevel
```

## 2) kanban show 확인
- [ ] 실제 Kanban task ID(`t_b3101772`)로 `kanban show` 를 확인한다.
- [ ] 제목, 상태, assignee, workspace 정보가 기대값과 맞는지 본다.
- [ ] `worker_context` 에 포함된 guardrail 문구를 기준으로 작업 범위를 확인한다.

확인 명령 예시:
```bash
hermes kanban show t_b3101772
```

## 3) 의도한 경로 사용 여부
- [ ] 파일 생성/수정은 모두 workspace 내부에서만 수행한다.
- [ ] 체크리스트 문서나 결과물은 프로젝트 루트 또는 합의된 하위 경로에만 둔다.
- [ ] 외부 경로(`/tmp`, 다른 repo, 홈 디렉터리 하위 임의 위치`)에 쓰지 않는다.
- [ ] 작업 후 `git status --short` 로 변경 파일이 예상 범위인지 확인한다.

확인 명령 예시:
```bash
git status --short
```

## 4) watcher 관찰 포인트
- [ ] `terminal(background=true, notify_on_complete=true)` 사용 시 완료 알림이 실제로 오는지 확인한다.
- [ ] 완료 시 새 agent turn 이 발생하는지 확인한다.
- [ ] 실패한 프로세스는 exit code 와 함께 에러로 보이는지 확인한다.
- [ ] 아무 변화 없는 경우 불필요한 메시지가 반복되지 않는지 확인한다.
- [ ] 장기 실행 작업에는 `watch_patterns` 보다 `notify_on_complete` 를 우선 사용한다.

관찰 기준:
- 성공: 프로세스 종료 후 1회 알림 수신
- 실패: non-zero exit 또는 timeout 알림 수신
- 무변화: 추가 메시지 없이 조용히 유지

## 5) 최종 합격 기준
- [ ] workspace 경로가 고정되어 있다.
- [ ] 실제 `kanban show` 결과로 task 상태를 확인했다.
- [ ] 모든 파일 변경이 의도한 경로 안에서만 일어났다.
- [ ] watcher 완료/실패/무변화 동작을 구분해서 확인할 수 있다.

## 참고 메모
이 체크리스트는 PM workflow 검증용이다. 핵심은 "실제 Kanban 카드의 상태와 workspace 경로를 먼저 확인하고, watcher는 완료 알림과 무변화 시 무음 동작을 기준으로 본다"는 점이다.
