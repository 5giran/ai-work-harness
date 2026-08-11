import type { ErrorObject, ValidateFunction } from "ajv";

import validateGenerated from "./generated/decision-view-validator.mjs";
import decisionViewSchemaDocument from "./decision-view.v1.schema.json";
import type { DecisionViewBundle } from "./types";

export const decisionViewSchema = decisionViewSchemaDocument;
export const validateDecisionView = validateGenerated as ValidateFunction<DecisionViewBundle>;

export function formatSchemaErrors(errors: ErrorObject[] | null | undefined): string {
  if (!errors?.length) return "Bundle does not match decision-view.v1";
  return errors
    .slice(0, 3)
    .map((error) => `${error.instancePath || "/"}: ${error.message ?? "invalid"}`)
    .join("; ");
}
