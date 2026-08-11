# AI Work Harness Decision Viewer

`decision-view.v1` export 파일을 브라우저에서 검증하고 읽는 정적 뷰어다. 서버나 저장
API를 사용하지 않으며 확인·승인·수정 control을 제공하지 않는다.

```bash
npm ci
npm run lint
npm test
npm run build
npm run test:e2e
```

`npm run dev`로 개발 서버를 열고 JSON 파일을 선택하거나 drop zone에 놓는다. 뷰어는
다음 검사를 모두 통과한 경우에만 결정 정보를 표시한다.

- strict I-JSON 및 `decision-view.v1` Ajv schema
- top-level RFC 8785 canonical JSON SHA-256
- 각 sanitized artifact envelope의 digest
- artifact key와 envelope `artifact_type` 일치
- excerpt opt-in과 private source field 부재

검사에 실패하면 이미 표시된 결정 DOM도 제거한다. `dist/`는 상대 경로 asset만 가진
정적 결과물이므로 임의의 정적 파일 서버나 로컬 패키징 환경에서 사용할 수 있다.
`public/examples/synthetic-decision-view.v1.json`은 외부 데이터가 없는 재현용 bundle이며
`npm run example:generate`로 결정적으로 다시 만들 수 있다.
