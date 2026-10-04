# cc-hwp

한글(.hwp / .hwpx) 문서를 Claude가 직접 읽게 해 주는 Claude Code 플러그인입니다.
HOP·한컴오피스에서 PDF로 변환해 올리던 과정을 없앱니다.

- **HWP 5.0**(CFB 바이너리)과 **HWPX**(OWPML ZIP)를 확장자가 아닌 시그니처로 판별
- 본문·표·글상자·각주를 Markdown으로 변환. 병합 셀 표는 `rowspan`/`colspan`을 가진 HTML 표로 보존
- Word(.docx)로 변환. 모든 표와 병합 셀은 Word 표로 옮기고, 셀마다 원본의 테두리(그려지는 변과 굵기)를 유지함. 글꼴·선 모양과 색·쪽 배치·그림은 옮기지 않음
- 문서에 내장된 미리보기 텍스트(PrvText)와 대조한 `preview_coverage`로 누락 여부를 자체 점검
- 배포용·암호 문서, HWP 3.x는 빈 결과 대신 명확한 오류(exit 3)
- 순수 Python 표준 라이브러리만 사용. `pip install` 불필요

## 설치

Claude Code:

```
/plugin marketplace add AndrewDongminYoo/cc-hwp
/plugin install cc-hwp@cc-hwp
```

claude.ai / Claude 데스크톱: `skills/hwp-read/` 폴더를 zip으로 묶어 Settings → Capabilities → Skills에서 업로드합니다.

## 직접 실행

```bash
python3 skills/hwp-read/scripts/hwp_read.py extract 문서.hwp -o 문서.md   # stderr에 JSON 리포트
python3 skills/hwp-read/scripts/hwp_read.py info 문서.hwpx
python3 skills/hwp-read/scripts/hwp_read.py render 문서.hwp -o 문서.pdf    # rhwp 있으면 전체 PDF, 없으면 첫 쪽 썸네일
python3 skills/hwp-read/scripts/hwp_read.py convert 문서.hwpx --to docx -o 문서.docx
```

종료 코드: `0` 성공 · `1` 파싱 오류 · `2` 사용법 · `3` 미지원/보호 문서 · `4` 추출은 됐으나 누락 의심

## 렌더링

레이아웃까지 정확한 PDF 렌더링은 [rhwp](https://github.com/edwardkim/rhwp)(HOP의 엔진) CLI에 위임합니다.
`rhwp`가 PATH에 있으면 `render`가 자동으로 사용합니다.

## 한계

- 수식은 한컴 수식 스크립트 원문으로만 표기(`[수식: ...]`), 그림은 `[그림]` 자리표시자
- 머리말·꼬리말·쪽 번호는 제외
- 원본 .hwp/.hwpx는 수정하지 않음. 서식 채우기·HWP로 저장은 범위 밖

## 테스트

```bash
python3 -m unittest discover tests
```

픽스처마다 원본 텍스트 레코드의 모든 문자가 출력 Markdown에 남는지(문자 보존) 검사합니다.

## License

MIT
