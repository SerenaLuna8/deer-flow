import { describe, expect, test } from "@rstest/core";

import {
  buildKnowledgeSearch,
  parseKnowledgeNavigation,
  type KnowledgeNavigationState,
} from "@/core/knowledge/navigation";

const KB = "40000000-0000-4000-8000-000000000001";
const DOC = "50000000-0000-4000-8000-000000000002";
const SEGMENT = "60000000-0000-4000-8000-000000000003";

const DEFAULTS: KnowledgeNavigationState = {
  kb: null,
  view: "documents",
  doc: null,
  segment: null,
  status: null,
  sort: "created_desc",
  page: 1,
};

function parse(query: string): KnowledgeNavigationState {
  return parseKnowledgeNavigation(new URLSearchParams(query));
}

describe("parseKnowledgeNavigation", () => {
  test("an empty query yields the list defaults", () => {
    expect(parse("")).toEqual(DEFAULTS);
  });

  test("round-trips every whitelisted field", () => {
    const state: KnowledgeNavigationState = {
      kb: KB,
      view: "documents",
      doc: DOC,
      segment: SEGMENT,
      status: "failed",
      sort: "name_asc",
      page: 3,
    };
    expect(parse(buildKnowledgeSearch(state))).toEqual(state);
  });

  test("uppercase UUIDs normalize to lowercase", () => {
    expect(parse(`kb=${KB.toUpperCase()}`).kb).toBe(KB);
  });

  test.each([
    ["kb=not-a-uuid", DEFAULTS],
    ["kb=", DEFAULTS],
    // A malformed kb drops every dependent field with it: they would
    // otherwise describe resources in no base at all.
    [`kb=oops&view=search&doc=${DOC}&page=4`, DEFAULTS],
  ])("a malformed kb (%s) falls back to the list", (query, expected) => {
    expect(parse(query)).toEqual(expected);
  });

  test("unknown view/status/sort values fall back to their defaults", () => {
    const state = parse(`kb=${KB}&view=upload&status=archived&sort=hot`);
    expect(state).toEqual({ ...DEFAULTS, kb: KB });
  });

  test.each([["0"], ["-2"], ["1.5"], ["abc"], ["10001"]])(
    "an invalid page (%s) resets to 1",
    (page) => {
      expect(parse(`kb=${KB}&page=${page}`).page).toBe(1);
    },
  );

  test("a segment without a doc is dropped", () => {
    const state = parse(`kb=${KB}&segment=${SEGMENT}`);
    expect(state.segment).toBeNull();
  });

  test("a doc with a malformed uuid is dropped along with its segment", () => {
    const state = parse(`kb=${KB}&doc=nope&segment=${SEGMENT}`);
    expect(state.doc).toBeNull();
    expect(state.segment).toBeNull();
  });

  test("non-documents views carry no document-list state", () => {
    const state = parse(
      `kb=${KB}&view=search&doc=${DOC}&segment=${SEGMENT}&status=ready&sort=name_desc&page=2`,
    );
    expect(state).toEqual({ ...DEFAULTS, kb: KB, view: "search" });
  });

  test("filters and paging survive alongside an open document", () => {
    const state = parse(`kb=${KB}&doc=${DOC}&status=ready&sort=name_asc&page=2`);
    expect(state).toEqual({
      ...DEFAULTS,
      kb: KB,
      doc: DOC,
      status: "ready",
      sort: "name_asc",
      page: 2,
    });
  });

  test("fields outside the whitelist are ignored", () => {
    const state = parse(`kb=${KB}&keyword=秘密文件&q=query&tab=x`);
    expect(state).toEqual({ ...DEFAULTS, kb: KB });
  });
});

describe("buildKnowledgeSearch", () => {
  test("defaults build an empty search string", () => {
    expect(buildKnowledgeSearch(DEFAULTS)).toBe("");
  });

  test("only non-default fields are written", () => {
    expect(buildKnowledgeSearch({ ...DEFAULTS, kb: KB })).toBe(`?kb=${KB}`);
    expect(
      buildKnowledgeSearch({ ...DEFAULTS, kb: KB, view: "settings" }),
    ).toBe(`?kb=${KB}&view=settings`);
    expect(buildKnowledgeSearch({ ...DEFAULTS, kb: KB, page: 2 })).toBe(
      `?kb=${KB}&page=2`,
    );
  });

  test("a stable field order keeps urls comparable", () => {
    const search = buildKnowledgeSearch({
      kb: KB,
      view: "documents",
      doc: DOC,
      segment: SEGMENT,
      status: "ready",
      sort: "name_desc",
      page: 5,
    });
    expect(search).toBe(
      `?kb=${KB}&doc=${DOC}&segment=${SEGMENT}&status=ready&sort=name_desc&page=5`,
    );
  });

  test("without a kb nothing else is written", () => {
    expect(
      buildKnowledgeSearch({ ...DEFAULTS, doc: DOC, page: 7, status: "ready" }),
    ).toBe("");
  });
});
