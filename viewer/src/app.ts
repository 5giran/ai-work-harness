import { BundleError, loadBundle } from "./integrity";
import {
  candidatesView,
  criteriaView,
  evidenceView,
  evaluationsView,
  finalDecisionView,
  frameView,
  mustEligibility,
  recommendationView,
} from "./selectors";
import type {
  CandidateView,
  DecisionViewBundle,
  EvaluationView,
} from "./types";

const MAX_BUNDLE_BYTES = 20 * 1024 * 1024;

const ASSESSMENT_LABELS: Record<string, string> = {
  meets: "충족",
  partial: "부분 충족",
  fails: "미충족",
  insufficient_evidence: "근거 부족",
  not_applicable: "해당 없음",
  not_evaluated: "평가 없음",
};

function element<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
  content?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== undefined) node.textContent = content;
  return node;
}

function paragraph(label: string, value: string): HTMLElement {
  const wrapper = element("div", "definition");
  wrapper.append(element("dt", "definition__label", label), element("dd", "definition__value", value));
  return wrapper;
}

function section(title: string, eyebrow: string): { section: HTMLElement; body: HTMLElement } {
  const wrapper = element("section", "panel reveal");
  const header = element("header", "section-heading");
  header.append(element("p", "eyebrow", eyebrow), element("h2", undefined, title));
  const body = element("div", "panel__body");
  wrapper.append(header, body);
  return { section: wrapper, body };
}

function statusCard(label: string, value: string, state: "good" | "warn" | "neutral"): HTMLElement {
  const card = element("div", `status-card status-card--${state}`);
  card.append(element("span", "status-card__label", label), element("strong", undefined, value));
  return card;
}

function candidateName(candidates: CandidateView[], candidateId: string | null): string {
  if (!candidateId) return "선택 없음";
  return candidates.find((candidate) => candidate.id === candidateId)?.title ?? candidateId;
}

function renderOverview(bundle: DecisionViewBundle): HTMLElement {
  const { section: wrapper, body } = section("결정 개요", "01 / OVERVIEW");
  const frame = frameView(bundle);
  const statusGrid = element("div", "status-grid");
  statusGrid.append(
    statusCard("Lifecycle", bundle.status.lifecycle_state, "neutral"),
    statusCard("Core 검증", bundle.status.verified ? "통과" : "미통과", bundle.status.verified ? "good" : "warn"),
    statusCard("결정 완료", bundle.status.decision_complete ? "완료" : "진행 중", bundle.status.decision_complete ? "good" : "neutral"),
    statusCard("Ready", bundle.status.ready ? "제출 가능" : "보류", bundle.status.ready ? "good" : "warn"),
  );

  const definitions = element("dl", "definition-grid");
  definitions.append(
    paragraph("의사결정 사용자", frame.businessUser),
    paragraph("막힌 결정", frame.blockedDecision),
    paragraph("합의한 문제", frame.problemStatement),
  );

  const integrity = element("div", "integrity-line");
  integrity.append(
    element("span", "integrity-line__mark", "✓"),
    element("span", undefined, "Bundle integrity와 artifact digest 검증 완료"),
    element("code", undefined, bundle.snapshot_sha256),
  );
  body.append(statusGrid, definitions, integrity);
  return wrapper;
}

function renderCandidates(bundle: DecisionViewBundle): HTMLElement {
  const { section: wrapper, body } = section("검토한 후보", "02 / OPTIONS");
  const candidates = candidatesView(bundle);
  const eligibility = mustEligibility(bundle);
  const grid = element("div", "candidate-grid");
  if (!candidates.length) {
    grid.append(element("p", "empty-state", "활성 후보 artifact가 없습니다."));
  }
  candidates.forEach((candidate, index) => {
    const card = element("article", "candidate-card");
    const heading = element("div", "candidate-card__heading");
    heading.append(
      element("span", "candidate-card__number", String(index + 1).padStart(2, "0")),
      element("h3", undefined, candidate.title),
    );
    if (candidate.id in eligibility) {
      const eligible = eligibility[candidate.id];
      heading.append(
        element("span", `tag tag--${eligible ? "good" : "bad"}`, eligible ? "Must 통과" : "Must 미통과"),
      );
    }
    card.append(heading, element("p", undefined, candidate.summary), element("code", "subtle-code", candidate.id));
    grid.append(card);
  });
  body.append(grid);
  return wrapper;
}

function evaluationLookup(evaluations: EvaluationView[]): Map<string, EvaluationView> {
  return new Map(evaluations.map((value) => [`${value.candidateId}\u0000${value.criterionId}`, value]));
}

function renderMatrix(bundle: DecisionViewBundle): HTMLElement {
  const { section: wrapper, body } = section("정성 비교", "03 / COMPARISON");
  const candidates = candidatesView(bundle);
  const criteria = criteriaView(bundle);
  const evaluations = evaluationLookup(evaluationsView(bundle));

  if (!candidates.length || !criteria.length) {
    body.append(element("p", "empty-state", "비교를 표시하려면 후보와 기준이 필요합니다."));
    return wrapper;
  }

  const scroll = element("div", "table-scroll");
  const table = element("table", "matrix");
  table.setAttribute(
    "aria-label",
    "후보별 정성 평가 매트릭스. 점수나 자동 순위는 사용하지 않습니다.",
  );
  const caption = element("caption", undefined, "후보별 정성 평가 매트릭스. 점수나 자동 순위는 사용하지 않습니다.");
  const head = element("thead");
  const headRow = element("tr");
  const optionHead = element("th", "matrix__corner", "후보 / 기준");
  optionHead.scope = "col";
  headRow.append(optionHead);
  criteria.forEach((criterion) => {
    const cell = element("th");
    cell.scope = "col";
    cell.append(
      element("span", `priority priority--${criterion.priority}`, criterion.priority.toUpperCase()),
      element("strong", undefined, criterion.title),
      element("small", undefined, criterion.definition),
    );
    headRow.append(cell);
  });
  head.append(headRow);

  const tableBody = element("tbody");
  candidates.forEach((candidate) => {
    const row = element("tr");
    const candidateHead = element("th");
    candidateHead.scope = "row";
    candidateHead.append(element("strong", undefined, candidate.title), element("code", undefined, candidate.id));
    row.append(candidateHead);
    criteria.forEach((criterion) => {
      const evaluation = evaluations.get(`${candidate.id}\u0000${criterion.id}`);
      const cell = element("td");
      const assessment = evaluation?.assessment ?? "not_evaluated";
      const badge = element(
        "span",
        `assessment assessment--${assessment}`,
        ASSESSMENT_LABELS[assessment] ?? assessment,
      );
      const context = element(
        "span",
        "sr-only",
        `${candidate.title}, ${criterion.title} 평가: `,
      );
      const rationale = element("p", "matrix__rationale", evaluation?.rationale ?? "평가 artifact 없음");
      cell.append(context, badge, rationale);
      if (evaluation?.evidenceIds.length) {
        const references = element("div", "evidence-links");
        references.setAttribute("aria-label", "연결된 근거");
        evaluation.evidenceIds.forEach((id) => {
          const link = element("a", undefined, id);
          link.href = `#evidence-${encodeURIComponent(id)}`;
          references.append(link);
        });
        cell.append(references);
      }
      row.append(cell);
    });
    tableBody.append(row);
  });

  table.append(caption, head, tableBody);
  scroll.append(table);
  body.append(scroll);
  return wrapper;
}

function renderEvidence(bundle: DecisionViewBundle): HTMLElement {
  const { section: wrapper, body } = section("근거 원장", "04 / EVIDENCE");
  const evidence = evidenceView(bundle);
  const list = element("div", "evidence-list");
  if (!evidence.length) {
    list.append(element("p", "empty-state", "공개 가능한 근거가 없습니다."));
  }
  evidence.forEach((item) => {
    const details = element("details", "evidence-card");
    details.id = `evidence-${item.id}`;
    const summary = element("summary");
    summary.append(
      element("span", `provenance provenance--${item.provenance}`, item.provenance),
      element("strong", undefined, item.claim),
      element("span", "evidence-card__toggle", "상세"),
    );
    const metadata = element("dl", "evidence-meta");
    metadata.append(paragraph("Evidence ID", item.id), paragraph("Source", item.sourceId), paragraph("Locator", item.locator));
    details.append(summary, metadata);
    if (item.excerpt !== undefined) {
      const quote = element("blockquote", undefined, item.excerpt);
      quote.setAttribute("aria-label", "사용자가 포함을 허용한 인용문");
      details.append(quote);
    }
    list.append(details);
  });
  body.append(list);
  return wrapper;
}

function decisionCard(
  label: string,
  disposition: string,
  candidate: string,
  rationale: string,
  accent: string,
): HTMLElement {
  const card = element("article", `decision-card decision-card--${accent}`);
  card.append(
    element("p", "eyebrow", label),
    element("h3", undefined, candidate),
    element("span", "decision-card__disposition", disposition),
    element("p", undefined, rationale),
  );
  return card;
}

function renderDecision(bundle: DecisionViewBundle): HTMLElement {
  const { section: wrapper, body } = section("추천과 인간 결정", "05 / DECISION");
  const candidates = candidatesView(bundle);
  const recommendation = recommendationView(bundle);
  const finalDecision = finalDecisionView(bundle);
  const comparison = element("div", "decision-comparison");
  comparison.append(
    recommendation
      ? decisionCard(
          "AI / FIXTURE RECOMMENDATION",
          recommendation.disposition,
          candidateName(candidates, recommendation.candidateId),
          recommendation.rationale,
          "agent",
        )
      : decisionCard("AI / FIXTURE RECOMMENDATION", "none", "추천 없음", "인간 결정은 추천 없이도 기록할 수 있습니다.", "agent"),
    finalDecision
      ? decisionCard(
          "HUMAN FINAL DECISION",
          finalDecision.disposition,
          candidateName(candidates, finalDecision.candidateId),
          finalDecision.reason,
          "human",
        )
      : decisionCard("HUMAN FINAL DECISION", "pending", "결정 대기", "최종 결정 artifact가 없습니다.", "human"),
  );
  body.append(comparison);

  if (finalDecision) {
    const relation = element("p", "relation-line");
    relation.append(element("strong", undefined, "추천과의 관계"), document.createTextNode(finalDecision.relation));
    body.append(relation);
    if (finalDecision.riskAcknowledgements.length) {
      const heading = element("h3", "minor-heading", "인간이 명시적으로 인지한 위험");
      const list = element("ul", "risk-list");
      finalDecision.riskAcknowledgements.forEach((risk) => list.append(element("li", undefined, risk)));
      body.append(heading, list);
    }
  }
  return wrapper;
}

function renderStaleChain(bundle: DecisionViewBundle): HTMLElement {
  const { section: wrapper, body } = section("Stale 판정", "06 / TRACE");
  const reasons = bundle.status.stale_reasons;
  if (!reasons.length) {
    const clear = element("p", "stale-clear");
    clear.append(element("span", undefined, "✓"), document.createTextNode("현재 스냅샷에 stale 사유가 없습니다."));
    body.append(clear);
    return wrapper;
  }
  const intro = element("p", "stale-warning", "상위 artifact 변경 또는 binding 불일치가 감지되었습니다.");
  const list = element("ol", "stale-chain");
  reasons.forEach((reason, index) => {
    const item = element("li");
    item.append(element("span", "stale-chain__index", String(index + 1)), element("code", undefined, reason));
    list.append(item);
  });
  body.append(intro, list);
  return wrapper;
}

function renderBundle(bundle: DecisionViewBundle): DocumentFragment {
  const fragment = document.createDocumentFragment();
  fragment.append(
    renderOverview(bundle),
    renderCandidates(bundle),
    renderMatrix(bundle),
    renderEvidence(bundle),
    renderDecision(bundle),
    renderStaleChain(bundle),
  );
  return fragment;
}

export interface ViewerApp {
  loadText(source: string): Promise<void>;
}

export function createViewerApp(root: HTMLElement): ViewerApp {
  root.replaceChildren();

  const page = element("main", "page");
  const masthead = element("header", "masthead");
  const brand = element("a", "brand");
  brand.href = "#viewer-content";
  brand.setAttribute("aria-label", "AI Work Harness Decision Viewer 처음으로");
  brand.append(element("span", "brand__mark", "AW"), element("span", undefined, "AI Work Harness"));
  const privacy = element("span", "privacy-badge", "LOCAL · READ ONLY");
  masthead.append(brand, privacy);

  const hero = element("section", "hero");
  const heroCopy = element("div", "hero__copy");
  heroCopy.append(
    element("p", "eyebrow", "DECISION EVIDENCE VIEWER"),
    element("h1", undefined, "추천보다 중요한 것은\n결정이 만들어진 과정입니다."),
    element(
      "p",
      "hero__lede",
      "decision-view.v1 파일을 이 브라우저 안에서 검증하고, 근거·평가·추천·인간 결정을 읽습니다. 파일은 전송되지 않습니다.",
    ),
  );

  const upload = element("section", "drop-zone");
  upload.tabIndex = 0;
  upload.setAttribute("role", "button");
  upload.setAttribute("aria-label", "decision-view.v1 JSON 파일 선택 또는 놓기");
  const input = element("input", "visually-hidden");
  input.type = "file";
  input.accept = ".json,application/json";
  input.tabIndex = -1;
  const uploadIcon = element("span", "drop-zone__icon", "↧");
  uploadIcon.setAttribute("aria-hidden", "true");
  upload.append(
    input,
    uploadIcon,
    element("strong", undefined, "Decision bundle 열기"),
    element("span", undefined, "클릭하거나 JSON 파일을 여기에 놓으세요"),
    element("small", undefined, "20 MiB 이하 · 네트워크 전송 없음 · 편집 기능 없음"),
  );
  hero.append(heroCopy, upload);

  const liveStatus = element("p", "load-status");
  liveStatus.setAttribute("role", "status");
  liveStatus.setAttribute("aria-live", "polite");
  const errorHost = element("div", "error-host");
  const viewerContent = element("div", "viewer-content");
  viewerContent.id = "viewer-content";
  page.append(masthead, hero, liveStatus, errorHost, viewerContent);
  root.append(page);

  const failClosed = (error: unknown): void => {
    viewerContent.replaceChildren();
    const code = error instanceof BundleError ? error.code : "UNEXPECTED_VIEWER_ERROR";
    const panel = element("section", "corruption-panel");
    panel.setAttribute("role", "alert");
    panel.append(
      element("p", "eyebrow", "FAIL CLOSED"),
      element("h2", undefined, "이 파일의 결정 내용을 표시하지 않습니다."),
      element(
        "p",
        undefined,
        "Schema 또는 digest 검증이 실패했습니다. 원본 export를 다시 생성하고 파일이 수정되지 않았는지 확인하세요.",
      ),
      element("code", undefined, code),
    );
    errorHost.replaceChildren(panel);
    liveStatus.textContent = "번들 검증 실패";
  };

  const loadText = async (source: string): Promise<void> => {
    viewerContent.replaceChildren();
    errorHost.replaceChildren();
    liveStatus.textContent = "번들 검증 중…";
    page.setAttribute("aria-busy", "true");
    try {
      const bundle = await loadBundle(source);
      viewerContent.replaceChildren(renderBundle(bundle));
      liveStatus.textContent = "번들 검증 완료. 읽기 전용 화면을 표시합니다.";
    } catch (error) {
      failClosed(error);
    } finally {
      page.removeAttribute("aria-busy");
    }
  };

  const loadFile = async (file: File | undefined): Promise<void> => {
    if (!file) return;
    if (file.size > MAX_BUNDLE_BYTES) {
      failClosed(new BundleError("BUNDLE_TOO_LARGE", "Bundle exceeds the viewer size limit"));
      return;
    }
    await loadText(await file.text());
  };

  upload.addEventListener("click", () => input.click());
  upload.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      input.click();
    }
  });
  input.addEventListener("change", () => void loadFile(input.files?.[0]));
  upload.addEventListener("dragover", (event) => {
    event.preventDefault();
    upload.classList.add("drop-zone--active");
  });
  upload.addEventListener("dragleave", () => upload.classList.remove("drop-zone--active"));
  upload.addEventListener("drop", (event) => {
    event.preventDefault();
    upload.classList.remove("drop-zone--active");
    void loadFile(event.dataTransfer?.files[0]);
  });

  return { loadText };
}
