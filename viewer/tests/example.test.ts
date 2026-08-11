import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { loadBundle } from "../src/integrity";

describe("published synthetic viewer bundle", () => {
  it("keeps the viewer schema mirror equal to the authoritative Python schema", async () => {
    const [viewerSchema, pythonSchema] = await Promise.all([
      readFile(resolve(import.meta.dirname, "../src/decision-view.v1.schema.json"), "utf8"),
      readFile(
        resolve(
          import.meta.dirname,
          "../../src/ai_work_harness/schemas/v2/decision-view.v1.schema.json",
        ),
        "utf8",
      ),
    ]);
    expect(JSON.parse(viewerSchema)).toEqual(JSON.parse(pythonSchema));
  });

  it("passes the same fail-closed loader used by the browser", async () => {
    const source = await readFile(
      resolve(import.meta.dirname, "../public/examples/synthetic-decision-view.v1.json"),
      "utf8",
    );
    const bundle = await loadBundle(source);

    expect(bundle.status.ready).toBe(true);
    expect(bundle.artifacts.recommendation?.payload.candidate_id).toBe("classical-ml");
    expect(bundle.artifacts.final_decision?.payload.candidate_id).toBe("rules");
  });

  it("validates and renders the shape emitted by Python DecisionService", async () => {
    const source = await readFile(
      resolve(import.meta.dirname, "python-fixtures/approved-decision-view.v1.json"),
      "utf8",
    );
    const bundle = await loadBundle(source);

    expect(bundle.artifacts.candidate_set.schema_version).toBe("2.0");
    expect(bundle.artifacts.candidate_set.artifact_type).toBe("candidate-set");
    expect(bundle.artifacts.evidence_set.payload.evidence).toBeInstanceOf(Array);
    expect(bundle.artifacts.comparison.payload.eligible_candidate_ids).toEqual([
      "classical-ml",
      "rules",
    ]);
    expect(bundle.cited_excerpts).toEqual({});
  });
});
