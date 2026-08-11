import canonicalize from "canonicalize";

import { formatSchemaErrors, validateDecisionView } from "./schema";
import { parseStrictJson } from "./strict-json";
import type { ArtifactEnvelope, DecisionViewBundle } from "./types";

export class BundleError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = "BundleError";
    this.code = code;
  }
}

export async function sha256Hex(value: string): Promise<string> {
  const bytes = new TextEncoder().encode(value);
  const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export function canonicalJson(value: unknown): string {
  const result = canonicalize(value);
  if (typeof result !== "string") {
    throw new BundleError("CANONICALIZATION_FAILED", "Bundle cannot be canonicalized");
  }
  return result;
}

async function hashCanonical(value: unknown): Promise<string> {
  return sha256Hex(canonicalJson(value));
}

function ensureArtifactKeyMatches(key: string, artifact: ArtifactEnvelope): void {
  if (artifact.artifact_type !== key.replaceAll("_", "-")) {
    throw new BundleError("ARTIFACT_TYPE_MISMATCH", "Artifact key does not match its envelope type");
  }
}

function ensureDigestKeysMatch(bundle: DecisionViewBundle): void {
  const artifactKeys = Object.keys(bundle.artifacts).sort();
  const digestKeys = Object.keys(bundle.digest_map).sort();
  if (
    artifactKeys.length !== digestKeys.length ||
    artifactKeys.some((value, index) => value !== digestKeys[index])
  ) {
    throw new BundleError("DIGEST_MAP_MISMATCH", "Artifact and digest-map keys differ");
  }
}

function ensureSemanticShape(bundle: DecisionViewBundle): void {
  if (!bundle.status.lifecycle_state.trim()) {
    throw new BundleError("STATUS_INVALID", "Lifecycle state must not be blank");
  }
  if (
    bundle.status.stale_reasons.some((reason) => !reason.trim()) ||
    new Set(bundle.status.stale_reasons).size !== bundle.status.stale_reasons.length
  ) {
    throw new BundleError("STALE_REASONS_INVALID", "Stale reasons must be non-empty and unique");
  }
  for (const artifact of Object.values(bundle.artifacts)) {
    if (!artifact.producer.kind.trim()) {
      throw new BundleError("PRODUCER_INVALID", "Artifact producer kind must not be blank");
    }
  }
}

const FORBIDDEN_EXPORT_KEYS = new Set([
  "filesystem_path",
  "local_path",
  "raw_bytes",
  "raw_content",
  "raw_source",
  "source_path",
]);

function ensureSanitized(value: unknown, allowExcerpts: boolean): void {
  if (Array.isArray(value)) {
    value.forEach((child) => ensureSanitized(child, allowExcerpts));
    return;
  }
  if (!value || typeof value !== "object") return;

  for (const [key, child] of Object.entries(value)) {
    if (FORBIDDEN_EXPORT_KEYS.has(key)) {
      throw new BundleError("PRIVATE_DATA_PRESENT", "Bundle contains a private source field");
    }
    if (!allowExcerpts && (key === "excerpt" || key === "cited_excerpt")) {
      throw new BundleError("UNEXPECTED_EXCERPT", "Bundle contains an excerpt without opt-in");
    }
    ensureSanitized(child, allowExcerpts);
  }
}

export async function verifyBundle(input: unknown): Promise<DecisionViewBundle> {
  if (!validateDecisionView(input)) {
    throw new BundleError("SCHEMA_INVALID", formatSchemaErrors(validateDecisionView.errors));
  }

  const bundle = input;
  ensureSemanticShape(bundle);
  ensureDigestKeysMatch(bundle);
  ensureSanitized(bundle.artifacts, false);

  const { integrity_sha256: expectedIntegrity, ...unsignedBundle } = bundle;
  const actualIntegrity = await hashCanonical(unsignedBundle);
  if (actualIntegrity !== expectedIntegrity) {
    throw new BundleError("BUNDLE_INTEGRITY_MISMATCH", "Bundle integrity digest does not match");
  }

  for (const [artifactKey, artifact] of Object.entries(bundle.artifacts)) {
    ensureArtifactKeyMatches(artifactKey, artifact);
    const actualDigest = await hashCanonical(artifact);
    if (actualDigest !== bundle.digest_map[artifactKey]) {
      throw new BundleError("ARTIFACT_DIGEST_MISMATCH", "Artifact digest does not match");
    }
  }

  return bundle;
}

export async function loadBundle(source: string): Promise<DecisionViewBundle> {
  let parsed: unknown;
  try {
    parsed = parseStrictJson(source);
  } catch (error) {
    if (error instanceof BundleError) throw error;
    const code =
      error instanceof Error && "code" in error && typeof error.code === "string"
        ? error.code
        : "JSON_INVALID";
    throw new BundleError(code, "File is not valid strict JSON");
  }
  return verifyBundle(parsed);
}

export async function computeArtifactDigest(artifact: ArtifactEnvelope): Promise<string> {
  return hashCanonical(artifact);
}

export async function computeBundleIntegrity(
  bundle: Omit<DecisionViewBundle, "integrity_sha256">,
): Promise<string> {
  return hashCanonical(bundle);
}
