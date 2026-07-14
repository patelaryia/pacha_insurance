const eatDateTime = new Intl.DateTimeFormat("en-GB", {
  dateStyle: "medium",
  timeStyle: "short",
  timeZone: "Africa/Nairobi",
});

export function formatEat(value: string): string {
  return eatDateTime.format(new Date(value));
}
