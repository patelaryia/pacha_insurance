export interface ConsoleError {
  code: string;
  detail: string;
  kind: "authentication" | "authorisation" | "retryable";
}

export function consoleError(error: unknown, fallbackDetail: string): ConsoleError {
  let code = "READ_FAILED";
  let detail = fallbackDetail;
  if (typeof error === "object" && error !== null) {
    const value = error as Record<string, unknown>;
    if (typeof value.code === "string") code = value.code;
    if (typeof value.detail === "string") detail = value.detail;
  }
  const kind = code === "AUTHENTICATION_REQUIRED" || code === "INVALID_TOKEN"
    ? "authentication"
    : code === "IDENTITY_NOT_MAPPED" || code === "FORBIDDEN_ROLE"
      ? "authorisation"
      : "retryable";
  return { code, detail, kind };
}
