import { readFile } from "node:fs/promises";
import path from "node:path";

import { expect, test } from "@playwright/test";

type MatrixCase = {
  case_id: string;
  layers: string[];
  evidence_selectors: string[];
};

type IsolationMatrix = {
  schema_version: number;
  cases: MatrixCase[];
};

test("M8 isolation matrix selectors stay project scoped", async () => {
  const matrixPath = path.resolve(
    process.cwd(),
    "../contracts/m8_isolation_matrix.json",
  );
  const matrix = JSON.parse(
    await readFile(matrixPath, "utf8"),
  ) as IsolationMatrix;

  expect(matrix.schema_version).toBe(1);
  expect(matrix.cases.length).toBeGreaterThan(0);
  for (const matrixCase of matrix.cases.filter((item) =>
    item.layers.includes("frontend"),
  )) {
    expect(
      matrixCase.evidence_selectors.some((selector) =>
        selector.startsWith("playwright::tests/e2e/"),
      ),
      matrixCase.case_id,
    ).toBe(true);
  }
});
