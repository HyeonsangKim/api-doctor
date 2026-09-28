# 회의용 도면

[meeting-architecture.html](../meeting-architecture.html) 에 들어간 도면의 원본이다.
`.svg` 가 원본이고 `.png` 는 Chrome 헤드리스로 렌더한 것이다.

| 파일 | 내용 |
|---|---|
| `fig1` | 신뢰 영역 아키텍처 — 네 영역과 경계를 넘는 것들 |
| `fig2` | 실행 흐름 — 모델을 부르는 구간과 부르지 않는 구간 |
| `fig3` | 종료 게이트의 네 단계 판정 |
| `fig4` | 감사 독립성이 어떻게 만들어지나 |

재렌더:

```bash
CH="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
"$CH" --headless --disable-gpu --screenshot=fig1.png --window-size=1700,1170 \
      --default-background-color=FFFFFF "file://$PWD/fig1.svg"
```
