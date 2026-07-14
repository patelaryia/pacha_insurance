export function parseCents(value: string | bigint): bigint {
  if (typeof value === "bigint") return value;
  if (!/^-?\d+$/.test(value)) throw new Error("Money cents must be a decimal integer string");
  return BigInt(value);
}

export function formatKes(cents: bigint): string {
  const negative = cents < 0n;
  const absolute = negative ? -cents : cents;
  const shillings = absolute / 100n;
  const remainder = absolute % 100n;
  const grouped = shillings.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const fraction = remainder === 0n ? "" : `.${remainder.toString().padStart(2, "0")}`;
  return `KES\u00a0${negative ? "-" : ""}${grouped}${fraction}`;
}
