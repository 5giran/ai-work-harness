const INTEGER_TOKEN = /-?(?:0|[1-9][0-9]*)/y;

export class StrictJsonError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = "StrictJsonError";
    this.code = code;
  }
}

class JsonScanner {
  private position = 0;

  constructor(private readonly source: string) {}

  scan(): void {
    this.skipWhitespace();
    this.scanValue();
    this.skipWhitespace();
    if (this.position !== this.source.length) {
      this.fail("JSON_TRAILING_DATA", "JSON document contains trailing data");
    }
  }

  private scanValue(): void {
    const character = this.source[this.position];
    if (character === "{") {
      this.scanObject();
      return;
    }
    if (character === "[") {
      this.scanArray();
      return;
    }
    if (character === '"') {
      this.scanString();
      return;
    }
    if (character === "t") {
      this.consumeLiteral("true");
      return;
    }
    if (character === "f") {
      this.consumeLiteral("false");
      return;
    }
    if (character === "n") {
      this.consumeLiteral("null");
      return;
    }
    if (character === "-" || (character >= "0" && character <= "9")) {
      this.scanInteger();
      return;
    }
    this.fail("JSON_INVALID", "JSON document contains an invalid value");
  }

  private scanObject(): void {
    this.position += 1;
    this.skipWhitespace();
    const keys = new Set<string>();
    if (this.consumeIf("}")) return;

    while (true) {
      if (this.source[this.position] !== '"') {
        this.fail("JSON_INVALID", "JSON object key must be a string");
      }
      const key = this.scanString();
      if (keys.has(key)) {
        this.fail("JSON_DUPLICATE_KEY", "JSON object contains a duplicate key");
      }
      keys.add(key);
      this.skipWhitespace();
      this.expect(":");
      this.skipWhitespace();
      this.scanValue();
      this.skipWhitespace();
      if (this.consumeIf("}")) return;
      this.expect(",");
      this.skipWhitespace();
    }
  }

  private scanArray(): void {
    this.position += 1;
    this.skipWhitespace();
    if (this.consumeIf("]")) return;

    while (true) {
      this.scanValue();
      this.skipWhitespace();
      if (this.consumeIf("]")) return;
      this.expect(",");
      this.skipWhitespace();
    }
  }

  private scanString(): string {
    const start = this.position;
    this.position += 1;
    let escaped = false;
    while (this.position < this.source.length) {
      const character = this.source[this.position];
      if (!escaped && character === '"') {
        this.position += 1;
        const token = this.source.slice(start, this.position);
        try {
          return JSON.parse(token) as string;
        } catch {
          this.fail("JSON_INVALID_STRING", "JSON document contains an invalid string");
        }
      }
      if (!escaped && character.charCodeAt(0) < 0x20) {
        this.fail("JSON_INVALID_STRING", "JSON string contains an unescaped control character");
      }
      if (escaped) {
        escaped = false;
      } else if (character === "\\") {
        escaped = true;
      }
      this.position += 1;
    }
    this.fail("JSON_INVALID_STRING", "JSON string is not terminated");
  }

  private scanInteger(): void {
    INTEGER_TOKEN.lastIndex = this.position;
    const match = INTEGER_TOKEN.exec(this.source);
    if (!match) this.fail("JSON_INVALID_NUMBER", "JSON document contains an invalid number");
    this.position = INTEGER_TOKEN.lastIndex;
    const next = this.source[this.position];
    if (next === "." || next === "e" || next === "E") {
      this.fail("JSON_FLOAT_NOT_ALLOWED", "Floating-point JSON numbers are not supported");
    }
    const value = Number(match[0]);
    if (!Number.isSafeInteger(value)) {
      this.fail("JSON_UNSAFE_INTEGER", "JSON integer exceeds the I-JSON safe range");
    }
  }

  private consumeLiteral(literal: string): void {
    if (this.source.slice(this.position, this.position + literal.length) !== literal) {
      this.fail("JSON_INVALID", "JSON document contains an invalid literal");
    }
    this.position += literal.length;
  }

  private skipWhitespace(): void {
    while (/\s/.test(this.source[this.position] ?? "") && this.position < this.source.length) {
      const character = this.source[this.position];
      if (character !== " " && character !== "\n" && character !== "\r" && character !== "\t") {
        this.fail("JSON_INVALID_WHITESPACE", "JSON document contains invalid whitespace");
      }
      this.position += 1;
    }
  }

  private consumeIf(character: string): boolean {
    if (this.source[this.position] !== character) return false;
    this.position += 1;
    return true;
  }

  private expect(character: string): void {
    if (!this.consumeIf(character)) {
      this.fail("JSON_INVALID", `JSON document is missing '${character}'`);
    }
  }

  private fail(code: string, message: string): never {
    throw new StrictJsonError(code, message);
  }
}

function assertNoLoneSurrogates(value: unknown): void {
  if (typeof value === "string") {
    for (let index = 0; index < value.length; index += 1) {
      const code = value.charCodeAt(index);
      if (code >= 0xd800 && code <= 0xdbff) {
        const next = value.charCodeAt(index + 1);
        if (Number.isNaN(next) || next < 0xdc00 || next > 0xdfff) {
          throw new StrictJsonError("JSON_LONE_SURROGATE", "JSON string contains a lone surrogate");
        }
        index += 1;
      } else if (code >= 0xdc00 && code <= 0xdfff) {
        throw new StrictJsonError("JSON_LONE_SURROGATE", "JSON string contains a lone surrogate");
      }
    }
    return;
  }
  if (Array.isArray(value)) {
    value.forEach(assertNoLoneSurrogates);
    return;
  }
  if (value && typeof value === "object") {
    for (const [key, child] of Object.entries(value)) {
      assertNoLoneSurrogates(key);
      assertNoLoneSurrogates(child);
    }
  }
}

export function parseStrictJson(source: string): unknown {
  new JsonScanner(source).scan();
  const parsed: unknown = JSON.parse(source);
  assertNoLoneSurrogates(parsed);
  return parsed;
}
