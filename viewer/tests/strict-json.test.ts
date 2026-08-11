import { describe, expect, it } from "vitest";

import { parseStrictJson, StrictJsonError } from "../src/strict-json";

describe("strict JSON parsing", () => {
  it("accepts I-JSON values", () => {
    expect(parseStrictJson('{"a":1,"b":[true,null,"한글"]}')).toEqual({
      a: 1,
      b: [true, null, "한글"],
    });
  });

  it.each([
    ['{"a":1,"a":2}', "JSON_DUPLICATE_KEY"],
    ['{"a":1.5}', "JSON_FLOAT_NOT_ALLOWED"],
    ['{"a":9007199254740992}', "JSON_UNSAFE_INTEGER"],
    ['{"a":"\\ud800"}', "JSON_LONE_SURROGATE"],
  ])("rejects non-I-JSON input", (source, code) => {
    expect(() => parseStrictJson(source)).toThrowError(
      expect.objectContaining<Partial<StrictJsonError>>({ code }),
    );
  });
});
