export type WorkflowEditorFlush = () => void;

export type WorkflowEditorFlushRegistry = {
  register(key: string, flush: WorkflowEditorFlush): () => void;
  flushAll(): void;
  hasPending(): boolean;
};

type FlushRegistration = Readonly<{
  flush: WorkflowEditorFlush;
}>;

const compareKeys = (left: string, right: string): number =>
  left < right ? -1 : left > right ? 1 : 0;

/**
 * Create one Workbench-local registry for controlled editors whose transient
 * transactions must reach the Workflow store before save/validate/publish.
 * Registering a key replaces its prior generation. A stale release callback
 * can therefore never unregister the newer editor instance.
 */
export function createWorkflowEditorFlushRegistry(): WorkflowEditorFlushRegistry {
  const registrations = new Map<string, FlushRegistration>();

  return {
    register(key, flush) {
      if (key.length === 0) {
        throw new TypeError("Workflow editor flush key must not be empty");
      }
      const registration: FlushRegistration = Object.freeze({ flush });
      registrations.set(key, registration);
      return () => {
        if (registrations.get(key) === registration) {
          registrations.delete(key);
        }
      };
    },
    flushAll() {
      const snapshot = [...registrations.entries()].sort(([left], [right]) =>
        compareKeys(left, right),
      );
      const failures: unknown[] = [];

      for (const [key, registration] of snapshot) {
        try {
          registration.flush();
          if (registrations.get(key) === registration) {
            registrations.delete(key);
          }
        } catch (error) {
          failures.push(error);
          // A flush failure remains pending. Preserve a replacement registered
          // by the callback, otherwise restore the failed captured generation.
          if (!registrations.has(key)) {
            registrations.set(key, registration);
          }
        }
      }

      if (failures.length > 0) {
        throw new AggregateError(
          failures,
          "One or more Workflow editor values could not be flushed",
        );
      }
    },
    hasPending: () => registrations.size > 0,
  };
}
