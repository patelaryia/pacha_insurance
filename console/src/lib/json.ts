import {
  parse,
  parseNumberAndBigInt,
  stringify,
} from "lossless-json";

export function parseLossless(text: string): unknown {
  return parse(text, null, { parseNumber: parseNumberAndBigInt });
}

export function stringifyLossless(value: unknown, spacing?: number): string {
  const result = stringify(value, null, spacing);
  if (result === undefined) throw new TypeError("Value cannot be represented as JSON");
  return result;
}

export function formatStructured(value: unknown): string {
  try {
    return stringifyLossless(value, 2);
  } catch {
    return String(value);
  }
}

export function toBigInt(value: unknown, label: string): bigint {
  if (typeof value === "bigint") return value;
  if (typeof value === "string" && /^-?\d+$/.test(value)) return BigInt(value);
  throw new TypeError(`${label} must be an integer-cent decimal`);
}

export function toSafeNumber(value: unknown, label: string): number {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "bigint") {
    const result = Number(value);
    if (Number.isSafeInteger(result)) return result;
  }
  throw new TypeError(`${label} must be a safe finite number`);
}
