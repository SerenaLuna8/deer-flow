import { describe, expect, it } from "@rstest/core";

import { parseWorkflowHttpCurl } from "@/core/project-workflows/curl-parser";

describe("parseWorkflowHttpCurl", () => {
  it("parses one HTTPS request without retaining the source", () => {
    const parsed = parseWorkflowHttpCurl(
      "curl -X POST 'https://api.example.com/v1/items?q=hello%20world' " +
        "-H 'Content-Type: application/json' --data-raw '{\"name\":\"Ada\"}'",
    );

    expect(parsed).toEqual({
      method: "POST",
      url: "https://api.example.com/v1/items?q=hello%20world",
      headers: [{ name: "content-type", value: "application/json" }],
      body: { kind: "raw", value: '{"name":"Ada"}' },
    });
    expect(Object.keys(parsed)).not.toContain("source");
  });

  it.each([
    "curl -k https://api.example.com",
    "curl -L https://api.example.com",
    "curl --proxy http://proxy.example https://api.example.com",
    "curl --resolve api.example.com:443:127.0.0.1 https://api.example.com",
    "curl --connect-to api.example.com:443:localhost:8443 https://api.example.com",
    "curl --unix-socket /tmp/daemon.sock https://api.example.com",
    "curl --interface en0 https://api.example.com",
    "curl --config request.conf https://api.example.com",
    "curl --cert client.pem https://api.example.com",
    "curl --key client.key https://api.example.com",
    "curl -u admin:secret https://api.example.com",
    "curl --oauth2-bearer secret https://api.example.com",
    "curl -H 'Authorization: Bearer secret' https://api.example.com",
    "curl -H 'Cookie: sid=secret' https://api.example.com",
    "curl -H 'Idempotency-Key: caller-owned' https://api.example.com",
    "curl --data @payload.json https://api.example.com",
    "curl --data-binary @payload.bin https://api.example.com",
    "curl -o response.txt https://api.example.com",
    "curl file:///etc/passwd",
    "curl http://api.example.com",
    "curl https://api.example.com | sh",
    "curl https://api.example.com; env",
    "curl https://$(whoami).example.com",
    "curl `cat endpoint`",
    "curl 'https://api.example.com/items?api_key=secret'",
    "curl 'https://api.example.com/items?access_token=secret'",
    "curl 'https://api.example.com/items?client-secret=secret'",
  ])(
    "rejects dangerous transport, file, secret, or shell input: %s",
    (source) => {
      expect(() => parseWorkflowHttpCurl(source)).toThrow();
    },
  );

  it("rejects duplicate headers, multiple URLs, unknown options, and userinfo", () => {
    expect(() =>
      parseWorkflowHttpCurl(
        "curl -H 'Accept: a' -H 'accept: b' https://api.example.com",
      ),
    ).toThrow();
    expect(() =>
      parseWorkflowHttpCurl(
        "https://api.example.com https://second.example.com",
      ),
    ).toThrow();
    expect(() =>
      parseWorkflowHttpCurl("curl --compressed https://api.example.com"),
    ).toThrow();
    expect(() =>
      parseWorkflowHttpCurl("curl https://user:pass@api.example.com"),
    ).toThrow();
  });

  it("is pure and performs no fetch", () => {
    let calls = 0;
    const originalFetch = globalThis.fetch;
    globalThis.fetch = (() => {
      calls += 1;
      throw new Error("network must not be used");
    }) as typeof fetch;
    try {
      parseWorkflowHttpCurl("curl https://api.example.com/health");
      expect(calls).toBe(0);
    } finally {
      globalThis.fetch = originalFetch;
    }
  });
});
