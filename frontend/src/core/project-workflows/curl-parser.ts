export type WorkflowHttpCurlHeader = Readonly<{
  name: string;
  value: string;
}>;

export type WorkflowHttpCurlImport = Readonly<{
  method: "GET" | "HEAD" | "POST" | "PUT" | "PATCH" | "DELETE";
  url: string;
  headers: readonly WorkflowHttpCurlHeader[];
  body: Readonly<{ kind: "raw"; value: string }> | null;
}>;

const SUPPORTED_METHODS = new Set([
  "GET",
  "HEAD",
  "POST",
  "PUT",
  "PATCH",
  "DELETE",
]);
const VALUE_OPTIONS = new Set([
  "-X",
  "--request",
  "-H",
  "--header",
  "-d",
  "--data",
  "--data-raw",
  "--data-binary",
  "--url",
]);
const DATA_OPTIONS = new Set(["-d", "--data", "--data-raw", "--data-binary"]);
const FORBIDDEN_OPTIONS = new Set([
  "-k",
  "--insecure",
  "-L",
  "--location",
  "--location-trusted",
  "-x",
  "--proxy",
  "--preproxy",
  "--proxy-user",
  "--proxy-header",
  "--resolve",
  "--connect-to",
  "--unix-socket",
  "--abstract-unix-socket",
  "--interface",
  "--local-port",
  "-K",
  "--config",
  "-E",
  "--cert",
  "--cert-type",
  "--key",
  "--key-type",
  "--pass",
  "--cacert",
  "--capath",
  "--pinnedpubkey",
  "-u",
  "--user",
  "--oauth2-bearer",
  "--aws-sigv4",
  "--netrc",
  "--netrc-file",
  "--netrc-optional",
  "-b",
  "--cookie",
  "-c",
  "--cookie-jar",
  "-o",
  "--output",
  "-O",
  "--remote-name",
  "-T",
  "--upload-file",
  "--ftp-create-dirs",
  "--proto",
  "--proto-default",
  "--proto-redir",
]);
const FORBIDDEN_HEADER_NAMES = new Set([
  "authorization",
  "cookie",
  "host",
  "idempotency-key",
  "content-length",
  "proxy-authorization",
  "proxy-authenticate",
  "set-cookie",
  "transfer-encoding",
  "connection",
  "forwarded",
]);
const HEADER_NAME = /^[a-z0-9!#$%&'*+.^_`|~-]{1,128}$/;
const DNS_HOST =
  /^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;
const MAX_SOURCE_UTF8_BYTES = 2_097_152;

const fail = (message: string): never => {
  throw new Error(`Invalid cURL import: ${message}`);
};

const tokenizeWithoutShell = (source: string): string[] => {
  if (
    new TextEncoder().encode(source).byteLength > MAX_SOURCE_UTF8_BYTES ||
    source.includes("\0")
  ) {
    fail("source exceeds the bounded text contract");
  }
  if (/[\r\n]/.test(source)) {
    fail("multiline shell input is not accepted");
  }
  if (source.includes("$(`") || source.includes("$(") || source.includes("`")) {
    fail("shell expansion is not accepted");
  }

  const tokens: string[] = [];
  let token = "";
  let quote: "single" | "double" | null = null;
  let escaping = false;
  let active = false;

  for (const character of source) {
    if (escaping) {
      token += character;
      escaping = false;
      active = true;
      continue;
    }
    if (character === "\\" && quote !== "single") {
      escaping = true;
      active = true;
      continue;
    }
    if (character === "'" && quote !== "double") {
      quote = quote === "single" ? null : "single";
      active = true;
      continue;
    }
    if (character === '"' && quote !== "single") {
      quote = quote === "double" ? null : "double";
      active = true;
      continue;
    }
    if (quote === null && /\s/.test(character)) {
      if (active) {
        tokens.push(token);
        token = "";
        active = false;
      }
      continue;
    }
    if (quote === null && /[|;&<>]/.test(character)) {
      fail("shell operators are not accepted");
    }
    if (quote === "double" && character === "$") {
      fail("shell expansion is not accepted");
    }
    token += character;
    active = true;
  }
  if (escaping || quote !== null) {
    fail("unterminated shell quoting");
  }
  if (active) {
    tokens.push(token);
  }
  return tokens;
};

const splitLongOption = (token: string): readonly [string, string | null] => {
  if (!token.startsWith("--")) {
    return [token, null];
  }
  const equals = token.indexOf("=");
  return equals === -1
    ? [token, null]
    : [token.slice(0, equals), token.slice(equals + 1)];
};

const parseHeader = (raw: string): WorkflowHttpCurlHeader => {
  const separator = raw.indexOf(":");
  if (separator <= 0) {
    fail("headers must use a name:value form");
  }
  const name = raw.slice(0, separator).trim().toLowerCase();
  const value = raw.slice(separator + 1).trim();
  if (!HEADER_NAME.test(name) || /[\r\n\0]/.test(value)) {
    fail("header syntax is invalid");
  }
  if (
    FORBIDDEN_HEADER_NAMES.has(name) ||
    name.startsWith("proxy-") ||
    name.startsWith("x-forwarded-") ||
    /(?:^|[-_])(api[-_]?key|token|secret)(?:$|[-_])/.test(name)
  ) {
    fail("secret-bearing or transport-controlled headers are not imported");
  }
  if (
    new TextEncoder().encode(name).byteLength > 128 ||
    new TextEncoder().encode(value).byteLength > 4_096
  ) {
    fail("header exceeds the bounded transport contract");
  }
  return { name, value };
};

const validateUrl = (raw: string): string => {
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return fail("URL is invalid");
  }
  if (url.protocol !== "https:") {
    fail("only HTTPS URLs are accepted");
  }
  if (url.username || url.password || url.hash) {
    fail("URL user information and fragments are forbidden");
  }
  const hostname = url.hostname.toLowerCase().replace(/\.$/, "");
  if (
    hostname !== url.hostname ||
    hostname === "localhost" ||
    !DNS_HOST.test(hostname)
  ) {
    fail("URL must use one canonical DNS hostname");
  }
  if (url.port && `${Number(url.port)}` !== url.port) {
    fail("URL port is not canonical");
  }
  for (const name of url.searchParams.keys()) {
    if (
      /(?:^|[-_])(api[-_]?key|token|secret|access[-_]?token)(?:$|[-_])/i.test(
        name,
      )
    ) {
      fail("secret-looking query parameters must use a Credential slot");
    }
  }
  return url.toString();
};

/**
 * Parse one deliberately small cURL subset without invoking a shell or network.
 *
 * The source string is never returned.  Callers must clear their textarea when
 * the dialog closes; only this normalized, secret-free result may reach Draft
 * state.
 */
export const parseWorkflowHttpCurl = (
  source: string,
): WorkflowHttpCurlImport => {
  if (typeof source !== "string") {
    return fail("source must be text");
  }
  const tokens = tokenizeWithoutShell(source.trim());
  if (tokens[0]?.toLowerCase() === "curl") {
    tokens.shift();
  }
  if (tokens.length === 0) {
    return fail("one request URL is required");
  }

  let method: WorkflowHttpCurlImport["method"] | null = null;
  let rawUrl: string | null = null;
  let body: WorkflowHttpCurlImport["body"] = null;
  const headers: WorkflowHttpCurlHeader[] = [];

  for (let index = 0; index < tokens.length; index += 1) {
    const token = tokens[index]!;
    if (!token.startsWith("-")) {
      if (rawUrl !== null) {
        return fail("multiple request URLs are not accepted");
      }
      rawUrl = token;
      continue;
    }

    const [option, inlineValue] = splitLongOption(token);
    if (FORBIDDEN_OPTIONS.has(option)) {
      return fail(`option ${option} is forbidden`);
    }
    if (!VALUE_OPTIONS.has(option)) {
      return fail(`option ${option} is unsupported`);
    }
    const value = inlineValue ?? tokens[index + 1];
    if (
      value === undefined ||
      (inlineValue === null && value.startsWith("-"))
    ) {
      return fail(`option ${option} requires one value`);
    }
    if (inlineValue === null) {
      index += 1;
    }

    if (option === "-X" || option === "--request") {
      const normalized = value.toUpperCase();
      if (!SUPPORTED_METHODS.has(normalized)) {
        return fail("HTTP method is unsupported");
      }
      method = normalized as WorkflowHttpCurlImport["method"];
    } else if (option === "-H" || option === "--header") {
      headers.push(parseHeader(value));
    } else if (DATA_OPTIONS.has(option)) {
      if (body !== null || value.startsWith("@")) {
        return fail("body files and multiple body arguments are not accepted");
      }
      body = { kind: "raw", value };
    } else if (option === "--url") {
      if (rawUrl !== null) {
        return fail("multiple request URLs are not accepted");
      }
      rawUrl = value;
    }
  }

  if (rawUrl === null) {
    return fail("one request URL is required");
  }
  if (headers.length > 64) {
    return fail("too many headers");
  }
  const headerNames = headers.map((header) => header.name);
  if (new Set(headerNames).size !== headerNames.length) {
    return fail("duplicate header names are not accepted");
  }
  const effectiveMethod = method ?? (body === null ? "GET" : "POST");
  if (
    body !== null &&
    (effectiveMethod === "GET" || effectiveMethod === "HEAD")
  ) {
    return fail("GET and HEAD requests cannot import a body");
  }
  return {
    method: effectiveMethod,
    url: validateUrl(rawUrl),
    headers,
    body,
  };
};
