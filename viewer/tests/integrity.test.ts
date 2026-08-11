import { describe, expect, it } from "vitest";

import { loadBundle } from "../src/integrity";
import { makeValidBundle, resignBundle } from "./fixtures";

describe("decision-view.v1 integrity", () => {
  it("accepts a schema-valid bundle whose digests all match", async () => {
    const bundle = await makeValidBundle();
    await expect(loadBundle(JSON.stringify(bundle))).resolves.toEqual(bundle);
  });

  it("fails closed when the top-level bundle is modified", async () => {
    const bundle = await makeValidBundle();
    bundle.status.ready = false;
    await expect(loadBundle(JSON.stringify(bundle))).rejects.toMatchObject({
      code: "BUNDLE_INTEGRITY_MISMATCH",
    });
  });

  it("detects an artifact edit even when the top-level digest is recomputed", async () => {
    const bundle = await makeValidBundle();
    const frame = bundle.artifacts.decision_frame;
    if (!frame) throw new Error("fixture frame missing");
    frame.payload.problem_statement = "변조된 결정";
    const resigned = await resignBundle(bundle);
    await expect(loadBundle(JSON.stringify(resigned))).rejects.toMatchObject({
      code: "ARTIFACT_DIGEST_MISMATCH",
    });
  });

  it("rejects envelope keys that disagree with artifact_type", async () => {
    const bundle = await makeValidBundle();
    const frame = bundle.artifacts.decision_frame;
    if (!frame) throw new Error("fixture frame missing");
    frame.artifact_type = "candidate-set";
    bundle.digest_map.decision_frame = await import("../src/integrity").then(({ computeArtifactDigest }) =>
      computeArtifactDigest(frame),
    );
    const resigned = await resignBundle(bundle);
    await expect(loadBundle(JSON.stringify(resigned))).rejects.toMatchObject({
      code: "ARTIFACT_TYPE_MISMATCH",
    });
  });

  it("rejects excerpts unless the export explicitly opted in", async () => {
    const bundle = await makeValidBundle({ includeExcerpts: true });
    bundle.include_cited_excerpts = false;
    const resigned = await resignBundle(bundle);
    await expect(loadBundle(JSON.stringify(resigned))).rejects.toMatchObject({
      code: "SCHEMA_INVALID",
    });
  });

  it("rejects undeclared top-level fields", async () => {
    const bundle = (await makeValidBundle()) as unknown as Record<string, unknown>;
    bundle.untrusted = true;
    await expect(loadBundle(JSON.stringify(bundle))).rejects.toMatchObject({
      code: "SCHEMA_INVALID",
    });
  });

  it("rejects duplicate stale reasons outside the generated schema", async () => {
    const bundle = await makeValidBundle({ staleReasons: ["FRAME_CHANGED"] });
    bundle.status.stale_reasons.push("FRAME_CHANGED");
    const resigned = await resignBundle(bundle);
    await expect(loadBundle(JSON.stringify(resigned))).rejects.toMatchObject({
      code: "STALE_REASONS_INVALID",
    });
  });
});
