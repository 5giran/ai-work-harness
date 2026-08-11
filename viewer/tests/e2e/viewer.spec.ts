import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

import { expect, test } from "@playwright/test";

const pythonExportPath = resolve(
  import.meta.dirname,
  "../python-fixtures/approved-decision-view.v1.json",
);

test.beforeEach(async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
});

test("renders the real Python export without application network calls", async ({ page }) => {
  const applicationNetwork: string[] = [];
  page.on("request", (request) => {
    if (["fetch", "xhr", "websocket", "eventsource"].includes(request.resourceType())) {
      applicationNetwork.push(request.url());
    }
  });

  await page.goto("/");
  await page.locator('input[type="file"]').setInputFiles(pythonExportPath);

  await expect(page.getByRole("heading", { name: "결정 개요" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Classical ML" })).toHaveCount(2);
  await expect(page.getByRole("heading", { name: "Rules" })).toHaveCount(2);
  await expect(page.locator(".relation-line")).toContainText("different");
  await expect(page.getByRole("table")).toBeVisible();
  await expect(page.locator('th[scope="col"]')).toHaveCount(6);
  await expect(page.locator('th[scope="row"]')).toHaveCount(3);
  await expect(page.locator("section.panel:has(table.matrix)")).toHaveCSS("opacity", "1");
  expect(applicationNetwork).toEqual([]);
});

test("fails closed and removes rendered data after one-byte tampering", async ({ page }) => {
  const original = await readFile(pythonExportPath, "utf8");
  const tampered = original.replace(
    '"lifecycle_state":"finalized"',
    '"lifecycle_state":"finalixed"',
  );
  expect(tampered).not.toBe(original);
  const originalBytes = Buffer.from(original);
  const tamperedBytes = Buffer.from(tampered);
  expect(tamperedBytes).toHaveLength(originalBytes.length);
  expect(
    [...originalBytes].filter((byte, index) => byte !== tamperedBytes[index]),
  ).toHaveLength(1);

  await page.goto("/");
  const input = page.locator('input[type="file"]');
  await input.setInputFiles(pythonExportPath);
  await expect(page.getByRole("heading", { name: "결정 개요" })).toBeVisible();

  await input.setInputFiles({
    name: "tampered-decision-view.v1.json",
    mimeType: "application/json",
    buffer: Buffer.from(tampered),
  });

  await expect(page.getByRole("alert")).toContainText("표시하지 않습니다");
  await expect(page.locator(".viewer-content")).not.toContainText("Classical ML");
});

test("keeps the upload control keyboard reachable and the comparison table semantic", async ({ page }) => {
  await page.goto("/");
  const dropZone = page.locator(".drop-zone");
  await expect(dropZone).toHaveAttribute("role", "button");
  await expect(dropZone).toHaveAttribute("tabindex", "0");
  await dropZone.focus();
  await expect(dropZone).toBeFocused();

  await page.locator('input[type="file"]').setInputFiles(pythonExportPath);
  const table = page.getByRole("table");
  await expect(table).toHaveAttribute(
    "aria-label",
    "후보별 정성 평가 매트릭스. 점수나 자동 순위는 사용하지 않습니다.",
  );
});
