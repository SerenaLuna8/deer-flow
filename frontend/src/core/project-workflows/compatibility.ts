import { z } from "zod";

const positiveSafeIntegerSchema = z
  .number()
  .int()
  .positive()
  .refine(
    Number.isSafeInteger,
    "integer exceeds JavaScript safe-integer range",
  );
const uuidSchema = z
  .string()
  .regex(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);
const sha256HexSchema = z.string().regex(/^[0-9a-f]{64}$/);
const safeIdentifierSchema = z
  .string()
  .min(1)
  .max(128)
  .regex(/^[A-Za-z0-9][A-Za-z0-9._:-]*$/);

export const workflowArtifactKindV1Schema = z.enum([
  "draft",
  "published_version",
  "run_snapshot",
]);

export const workflowArtifactSchemaIdentityV1Schema = z
  .object({
    graph_schema_version: positiveSafeIntegerSchema,
    canvas_schema_version: positiveSafeIntegerSchema,
    compiler_contract_version: positiveSafeIntegerSchema,
  })
  .strict();

const sameSchemaIdentity = (
  left: WorkflowArtifactSchemaIdentityV1,
  right: WorkflowArtifactSchemaIdentityV1,
) =>
  left.graph_schema_version === right.graph_schema_version &&
  left.canvas_schema_version === right.canvas_schema_version &&
  left.compiler_contract_version === right.compiler_contract_version;

export const workflowSchemaMigrationPathV1Schema = z
  .object({
    migration_id: safeIdentifierSchema,
    source: workflowArtifactSchemaIdentityV1Schema,
    target: workflowArtifactSchemaIdentityV1Schema,
  })
  .strict()
  .superRefine((value, context) => {
    if (sameSchemaIdentity(value.source, value.target)) {
      context.addIssue({
        code: "custom",
        message: "a migration path must change the schema identity",
      });
    }
  });

const workflowSchemaCurrentV1Schema = z
  .object({
    status: z.literal("current"),
    artifact_kind: workflowArtifactKindV1Schema,
    identity: workflowArtifactSchemaIdentityV1Schema,
    read_only: z.boolean(),
    silent_upgrade_allowed: z.literal(false),
  })
  .strict()
  .superRefine((value, context) => {
    if (value.read_only !== (value.artifact_kind !== "draft")) {
      context.addIssue({
        code: "custom",
        path: ["read_only"],
        message: "only a current Draft is writable",
      });
    }
  });

const workflowSchemaMigratableV1Schema = z
  .object({
    status: z.literal("migratable"),
    artifact_kind: z.literal("draft"),
    source: workflowArtifactSchemaIdentityV1Schema,
    target: workflowArtifactSchemaIdentityV1Schema,
    migration_id: safeIdentifierSchema,
    requires_explicit_save: z.literal(true),
    silent_upgrade_allowed: z.literal(false),
  })
  .strict()
  .superRefine((value, context) => {
    if (sameSchemaIdentity(value.source, value.target)) {
      context.addIssue({
        code: "custom",
        message: "a migratable Draft must change schema identity",
      });
    }
  });

const workflowReadOnlyReasonV1Schema = z.enum([
  "NO_EXPLICIT_DRAFT_MIGRATION",
  "PUBLISHED_VERSION_MIGRATION_FORBIDDEN",
  "RUN_SNAPSHOT_MIGRATION_FORBIDDEN",
]);

const workflowSchemaReadOnlyUnsupportedV1Schema = z
  .object({
    status: z.literal("read_only_unsupported"),
    artifact_kind: workflowArtifactKindV1Schema,
    source: workflowArtifactSchemaIdentityV1Schema,
    supported: workflowArtifactSchemaIdentityV1Schema,
    reason: workflowReadOnlyReasonV1Schema,
    read_only: z.literal(true),
    silent_upgrade_allowed: z.literal(false),
  })
  .strict()
  .superRefine((value, context) => {
    const expected = {
      draft: "NO_EXPLICIT_DRAFT_MIGRATION",
      published_version: "PUBLISHED_VERSION_MIGRATION_FORBIDDEN",
      run_snapshot: "RUN_SNAPSHOT_MIGRATION_FORBIDDEN",
    }[value.artifact_kind];
    if (value.reason !== expected) {
      context.addIssue({
        code: "custom",
        path: ["reason"],
        message: "read-only reason must match the artifact lifecycle",
      });
    }
  });

export const workflowSchemaCompatibilityV1Schema = z.union([
  workflowSchemaCurrentV1Schema,
  workflowSchemaMigratableV1Schema,
  workflowSchemaReadOnlyUnsupportedV1Schema,
]);

export const workflowSchemaCompatibilityCaseV1Schema = z
  .object({
    artifact_kind: workflowArtifactKindV1Schema,
    source: workflowArtifactSchemaIdentityV1Schema,
    supported: workflowArtifactSchemaIdentityV1Schema,
    migration_paths: z.array(workflowSchemaMigrationPathV1Schema).max(32),
    expected: workflowSchemaCompatibilityV1Schema,
  })
  .strict();

export type WorkflowArtifactKindV1 = z.infer<
  typeof workflowArtifactKindV1Schema
>;
export type WorkflowArtifactSchemaIdentityV1 = z.infer<
  typeof workflowArtifactSchemaIdentityV1Schema
>;
export type WorkflowSchemaMigrationPathV1 = z.infer<
  typeof workflowSchemaMigrationPathV1Schema
>;
export type WorkflowSchemaCompatibilityV1 = z.infer<
  typeof workflowSchemaCompatibilityV1Schema
>;

export const assessWorkflowSchemaCompatibility = ({
  artifactKind,
  source: rawSource,
  supported: rawSupported,
  migrationPaths: rawMigrationPaths,
}: {
  artifactKind: WorkflowArtifactKindV1;
  source: WorkflowArtifactSchemaIdentityV1;
  supported: WorkflowArtifactSchemaIdentityV1;
  migrationPaths: readonly WorkflowSchemaMigrationPathV1[];
}): WorkflowSchemaCompatibilityV1 => {
  const source = workflowArtifactSchemaIdentityV1Schema.parse(rawSource);
  const supported = workflowArtifactSchemaIdentityV1Schema.parse(rawSupported);
  const migrationPaths = z
    .array(workflowSchemaMigrationPathV1Schema)
    .max(32)
    .parse(rawMigrationPaths);

  if (sameSchemaIdentity(source, supported)) {
    return workflowSchemaCurrentV1Schema.parse({
      status: "current",
      artifact_kind: artifactKind,
      identity: source,
      read_only: artifactKind !== "draft",
      silent_upgrade_allowed: false,
    });
  }

  if (artifactKind === "draft") {
    const matching = migrationPaths.filter(
      (path) =>
        sameSchemaIdentity(path.source, source) &&
        sameSchemaIdentity(path.target, supported),
    );
    if (matching.length > 1) {
      throw new Error(
        "multiple migration paths match the same schema transition",
      );
    }
    const path = matching[0];
    if (path !== undefined) {
      return workflowSchemaMigratableV1Schema.parse({
        status: "migratable",
        artifact_kind: "draft",
        source,
        target: supported,
        migration_id: path.migration_id,
        requires_explicit_save: true,
        silent_upgrade_allowed: false,
      });
    }
  }

  const reason = {
    draft: "NO_EXPLICIT_DRAFT_MIGRATION",
    published_version: "PUBLISHED_VERSION_MIGRATION_FORBIDDEN",
    run_snapshot: "RUN_SNAPSHOT_MIGRATION_FORBIDDEN",
  } as const;
  return workflowSchemaReadOnlyUnsupportedV1Schema.parse({
    status: "read_only_unsupported",
    artifact_kind: artifactKind,
    source,
    supported,
    reason: reason[artifactKind],
    read_only: true,
    silent_upgrade_allowed: false,
  });
};

export const workflowCompilerContractIdentityV1Schema = z
  .object({
    graph_schema_version: positiveSafeIntegerSchema,
    compiler_contract_version: positiveSafeIntegerSchema,
    semantic_checksum: sha256HexSchema,
  })
  .strict();

export const workflowRunSnapshotIdentityV1Schema = z
  .object({
    schema_version: z.literal(1),
    workflow_run_id: uuidSchema,
    workflow_version_id: uuidSchema,
    graph_schema_version: positiveSafeIntegerSchema,
    compiler_contract_version: positiveSafeIntegerSchema,
    semantic_checksum: sha256HexSchema,
    catalog_generation: safeIdentifierSchema,
    snapshot_checksum: sha256HexSchema,
  })
  .strict();

export const workflowCompilerSnapshotContractV1Schema = z
  .object({
    schema_version: z.literal(1),
    compiler_identity: workflowCompilerContractIdentityV1Schema,
    snapshot_identity: workflowRunSnapshotIdentityV1Schema,
  })
  .strict()
  .superRefine((value, context) => {
    const snapshot = value.snapshot_identity;
    const compiler = value.compiler_identity;
    if (
      snapshot.graph_schema_version !== compiler.graph_schema_version ||
      snapshot.compiler_contract_version !==
        compiler.compiler_contract_version ||
      snapshot.semantic_checksum !== compiler.semantic_checksum
    ) {
      context.addIssue({
        code: "custom",
        path: ["snapshot_identity"],
        message:
          "Run Snapshot must retain the exact compiler contract identity",
      });
    }
  });
