import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createViewerApp, type ViewerApp } from "../src/app";
import { makeValidBundle } from "./fixtures";

describe("read-only viewer DOM flow", () => {
  let root: HTMLElement;
  let app: ViewerApp;

  beforeEach(() => {
    document.body.replaceChildren();
    root = document.createElement("div");
    document.body.append(root);
    app = createViewerApp(root);
  });

  afterEach(() => vi.restoreAllMocks());

  it("renders the full qualitative decision view without network access", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    const bundle = await makeValidBundle({ includeExcerpts: true });
    await app.loadText(JSON.stringify(bundle));

    expect(root.textContent).toContain("고객지원 운영 책임자");
    expect(root.textContent).toContain("규칙 기반");
    expect(root.textContent).toContain("추천과 인간 결정");
    expect(root.textContent).toContain("합성 데이터만 사용하는 로컬 실행 기록");
    expect(root.querySelector("table caption")?.textContent).toContain("점수나 자동 순위");
    expect(root.querySelectorAll("th[scope='col']").length).toBeGreaterThan(1);
    expect(root.querySelector("[role='alert']")).toBeNull();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("removes previously rendered decision data after a corrupted load", async () => {
    const valid = await makeValidBundle();
    await app.loadText(JSON.stringify(valid));
    expect(root.textContent).toContain("규칙 기반");

    valid.status.ready = false;
    await app.loadText(JSON.stringify(valid));

    expect(root.querySelector("[role='alert']")?.textContent).toContain("표시하지 않습니다");
    expect(root.querySelector(".viewer-content")?.textContent).not.toContain("규칙 기반");
  });

  it("renders stale reasons as an ordered trace", async () => {
    const bundle = await makeValidBundle({
      staleReasons: ["CRITERIA_SET_CHANGED", "EVALUATION_BINDING_STALE"],
    });
    await app.loadText(JSON.stringify(bundle));

    const staleItems = [...root.querySelectorAll(".stale-chain li")].map(
      (item) => item.textContent,
    );
    expect(staleItems).toEqual([
      expect.stringContaining("CRITERIA_SET_CHANGED"),
      expect.stringContaining("EVALUATION_BINDING_STALE"),
    ]);
  });

  it("exposes the file target as a keyboard-operable control", () => {
    const dropZone = root.querySelector<HTMLElement>(".drop-zone");
    const fileInput = root.querySelector<HTMLInputElement>("input[type='file']");
    if (!dropZone || !fileInput) throw new Error("upload controls missing");
    const clickSpy = vi.spyOn(fileInput, "click").mockImplementation(() => undefined);

    dropZone.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    dropZone.dispatchEvent(new KeyboardEvent("keydown", { key: " ", bubbles: true }));

    expect(dropZone.getAttribute("role")).toBe("button");
    expect(dropZone.tabIndex).toBe(0);
    expect(clickSpy).toHaveBeenCalledTimes(2);
  });
});
