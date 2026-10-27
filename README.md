# AI 사용량 스티커 (Claude / ChatGPT)

바탕화면 위에 떠 있는 작은 스티커로 **Claude**와 **ChatGPT(Codex)** 사용량을 계정별로 보여줍니다.

- 계정 추가 시 Claude / ChatGPT 선택 → 탭의 색 점으로 구분 (주황 = Claude, 초록 = ChatGPT)
  - Claude: claude.ai 5시간 세션 / 주간 사용량
  - ChatGPT: chatgpt.com Codex 사용 한도 (5시간 / 주간)
- 여러 계정 동시 등록, 백그라운드 자동 갱신, 리셋까지 남은 시간 표시
- 둥근 스티커 디자인 · 우하단 모서리 드래그 또는 **Ctrl+휠**로 크기 조절 (70~200%)
- 우클릭 메뉴: 새로고침 / 계정 전환·추가·이름 변경·재로그인·삭제 / Claude 조직(개인·팀) 선택 / 크기 / 테마

## 실행

```
pip install curl_cffi playwright pillow
python ai_usage_sticker.py
```

Edge 또는 Chrome이 설치돼 있으면 `playwright install chromium`은 생략해도 됩니다.
`run_ai_usage_sticker.bat`을 쓰면 콘솔 창 없이 실행되고, `build_exe.bat`을 쓰면 `dist\AIUsageSticker.exe` 단일 실행 파일로 빌드됩니다.

진단: `python ai_usage_sticker.py --dump` (현재 계정의 API 응답 원본 출력)

## 개인정보

로그인 세션은 `ai_usage_config.json`과 `ai_login_profile_*` 폴더에 저장되며, `.gitignore`로 커밋에서 제외됩니다.
구버전(`claude_sticker_config.json`, `claude_login_profile_*`)은 첫 실행 때 자동으로 새 이름으로 이전됩니다.

## 테마

`themes/<이름>/` 폴더에 단계별 PNG를 넣으면 게이지 대신 이미지로 표시됩니다. 자세한 내용은 [themes/README.txt](themes/README.txt) 참고.
