import type { ValidateFunction } from "ajv";

import type { DecisionViewBundle } from "../types";

declare const validate: ValidateFunction<DecisionViewBundle>;
export default validate;
