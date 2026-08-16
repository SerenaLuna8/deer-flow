import { describe, expect, test, rs } from "@rstest/core";

import type { UploadedFileInfo } from "@/core/uploads/api";
import { AttachmentUploadCoordinator } from "@/core/uploads/attachment-upload-coordinator";

type Upload = (
  files: File[],
  onFileUploaded: (uploaded: UploadedFileInfo, index: number) => void,
) => Promise<void>;

const THREAD_A_SCOPE = "account-a:project-a:thread-a";
const SAME_THREAD_OTHER_OWNER_SCOPE = "account-b:project-b:thread-a";

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((nextResolve, nextReject) => {
    resolve = nextResolve;
    reject = nextReject;
  });
  return { promise, resolve, reject };
}

function uploadedFile(
  id: string,
  filename: string,
  suffix: string,
): UploadedFileInfo {
  return {
    id,
    kind: "upload",
    filename,
    size: 1,
    path: `/mnt/user-data/uploads/${suffix}`,
    logical_path: `uploads/${suffix}`,
    virtual_path: `/mnt/user-data/uploads/${suffix}`,
    artifact_url: `/api/files/${id}`,
  };
}

describe("AttachmentUploadCoordinator", () => {
  test("starts upload immediately and shares one pending upload with send", async () => {
    const coordinator = new AttachmentUploadCoordinator();
    const file = new File(["a"], "a.png", { type: "image/png" });
    const uploaded = uploadedFile(
      "8f31eef3-0662-42c5-809c-3bbbe2c663af",
      file.name,
      file.name,
    );
    const pending = deferred<void>();
    const upload = rs.fn<Upload>((_files, onFileUploaded) =>
      pending.promise.then(() => {
        onFileUploaded(uploaded, 0);
      }),
    );
    const candidates = [{ clientId: "client-a", file }];

    const eager = coordinator.ensure({
      scopeKey: THREAD_A_SCOPE,
      candidates,
      retryPendingFailure: false,
      upload,
    });

    expect(upload).toHaveBeenCalledTimes(1);
    expect(upload).toHaveBeenCalledWith([file], expect.any(Function));

    const send = coordinator.ensure({
      scopeKey: THREAD_A_SCOPE,
      candidates,
      retryPendingFailure: true,
      upload,
    });
    let sendSettled = false;
    void send.finally(() => {
      sendSettled = true;
    });

    await Promise.resolve();
    expect(sendSettled).toBe(false);
    expect(upload).toHaveBeenCalledTimes(1);

    pending.resolve();
    await expect(eager).resolves.toEqual([uploaded]);
    await expect(send).resolves.toEqual([uploaded]);
    expect(upload).toHaveBeenCalledTimes(1);
  });

  test("retries only missing files after a partial eager-upload failure", async () => {
    const coordinator = new AttachmentUploadCoordinator();
    const fileA = new File(["a"], "a.png", { type: "image/png" });
    const fileB = new File(["b"], "b.png", { type: "image/png" });
    const uploadedA = uploadedFile(
      "8f31eef3-0662-42c5-809c-3bbbe2c663af",
      fileA.name,
      fileA.name,
    );
    const uploadedB = uploadedFile(
      "63c7cdd2-a785-41b5-9e14-b39c026f94a6",
      fileB.name,
      fileB.name,
    );
    const upload = rs
      .fn<Upload>()
      .mockImplementationOnce(async (_files, onFileUploaded) => {
        onFileUploaded(uploadedA, 0);
        throw new Error("second file upload failed");
      })
      .mockImplementationOnce(async (_files, onFileUploaded) => {
        onFileUploaded(uploadedB, 0);
      });
    const candidates = [
      { clientId: "client-a", file: fileA },
      { clientId: "client-b", file: fileB },
    ];

    await expect(
      coordinator.ensure({
        scopeKey: THREAD_A_SCOPE,
        candidates,
        retryPendingFailure: false,
        upload,
      }),
    ).rejects.toThrow("second file upload failed");

    await expect(
      coordinator.ensure({
        scopeKey: THREAD_A_SCOPE,
        candidates,
        retryPendingFailure: true,
        upload,
      }),
    ).resolves.toEqual([uploadedA, uploadedB]);

    expect(upload).toHaveBeenCalledTimes(2);
    expect(upload.mock.calls[0]?.[0]).toEqual([fileA, fileB]);
    expect(upload.mock.calls[1]?.[0]).toEqual([fileB]);
  });

  test("cleans up a ready result that arrives after its attachment is removed", async () => {
    const coordinator = new AttachmentUploadCoordinator();
    const file = new File(["a"], "a.png", { type: "image/png" });
    const uploaded = uploadedFile(
      "8f31eef3-0662-42c5-809c-3bbbe2c663af",
      file.name,
      file.name,
    );
    const pending = deferred<void>();
    const upload = rs.fn<Upload>((_files, onFileUploaded) =>
      pending.promise.then(() => onFileUploaded(uploaded, 0)),
    );
    const cleanup = rs.fn<(uploaded: UploadedFileInfo) => void>();
    const eager = coordinator.ensure({
      scopeKey: THREAD_A_SCOPE,
      candidates: [{ clientId: "client-a", file }],
      retryPendingFailure: false,
      upload,
    });

    expect(coordinator.discard(THREAD_A_SCOPE, "client-a", cleanup)).toBe(true);
    pending.resolve();

    await expect(eager).rejects.toMatchObject({ name: "AbortError" });
    expect(cleanup).toHaveBeenCalledTimes(1);
    expect(cleanup).toHaveBeenCalledWith(uploaded);
  });

  test("does not reuse ready uploads for the same thread in another account and project", async () => {
    const coordinator = new AttachmentUploadCoordinator();
    const file = new File(["a"], "a.png", { type: "image/png" });
    const firstThreadAUpload = uploadedFile(
      "8f31eef3-0662-42c5-809c-3bbbe2c663af",
      file.name,
      "thread-a-first.png",
    );
    const otherOwnerUpload = uploadedFile(
      "63c7cdd2-a785-41b5-9e14-b39c026f94a6",
      file.name,
      "other-owner.png",
    );
    const secondThreadAUpload = uploadedFile(
      "1d1d5182-e33f-4119-b2e7-e3f1bf99af97",
      file.name,
      "thread-a-second.png",
    );
    const results = [firstThreadAUpload, otherOwnerUpload, secondThreadAUpload];
    const upload = rs.fn<Upload>(async (_files, onFileUploaded) => {
      const uploaded = results.shift();
      if (!uploaded) throw new Error("unexpected upload");
      onFileUploaded(uploaded, 0);
    });
    const candidates = [{ clientId: "shared-client-id", file }];

    await expect(
      coordinator.ensure({
        scopeKey: THREAD_A_SCOPE,
        candidates,
        retryPendingFailure: false,
        upload,
      }),
    ).resolves.toEqual([firstThreadAUpload]);
    await expect(
      coordinator.ensure({
        scopeKey: SAME_THREAD_OTHER_OWNER_SCOPE,
        candidates,
        retryPendingFailure: false,
        upload,
      }),
    ).resolves.toEqual([otherOwnerUpload]);

    coordinator.consume(THREAD_A_SCOPE, ["shared-client-id"]);

    await expect(
      coordinator.ensure({
        scopeKey: THREAD_A_SCOPE,
        candidates,
        retryPendingFailure: false,
        upload,
      }),
    ).resolves.toEqual([secondThreadAUpload]);

    expect(upload).toHaveBeenCalledTimes(3);
  });

  test("protects a claimed ready upload until the claim is released", async () => {
    const coordinator = new AttachmentUploadCoordinator();
    const file = new File(["a"], "a.png", { type: "image/png" });
    const uploaded = uploadedFile(
      "8f31eef3-0662-42c5-809c-3bbbe2c663af",
      file.name,
      file.name,
    );
    const upload = rs.fn<Upload>(async (_files, onFileUploaded) => {
      onFileUploaded(uploaded, 0);
    });
    const cleanup = rs.fn<(uploaded: UploadedFileInfo) => void>();

    await coordinator.ensure({
      scopeKey: THREAD_A_SCOPE,
      candidates: [{ clientId: "client-a", file }],
      retryPendingFailure: false,
      upload,
    });

    expect(coordinator.claim(THREAD_A_SCOPE, ["client-a"])).toBe(true);
    expect(coordinator.discard(THREAD_A_SCOPE, "client-a", cleanup)).toBe(
      false,
    );
    expect(cleanup).not.toHaveBeenCalled();

    coordinator.release(THREAD_A_SCOPE, ["client-a"]);

    expect(coordinator.discard(THREAD_A_SCOPE, "client-a", cleanup)).toBe(true);
    expect(cleanup).toHaveBeenCalledTimes(1);
    expect(cleanup).toHaveBeenCalledWith(uploaded);
  });

  test("resetScope cleans unclaimed ready uploads and later pending results", async () => {
    const coordinator = new AttachmentUploadCoordinator();
    const readyFile = new File(["a"], "ready.png", { type: "image/png" });
    const pendingFile = new File(["b"], "pending.png", {
      type: "image/png",
    });
    const readyUploaded = uploadedFile(
      "8f31eef3-0662-42c5-809c-3bbbe2c663af",
      readyFile.name,
      readyFile.name,
    );
    const pendingUploaded = uploadedFile(
      "63c7cdd2-a785-41b5-9e14-b39c026f94a6",
      pendingFile.name,
      pendingFile.name,
    );
    const pending = deferred<void>();
    const readyUpload = rs.fn<Upload>(async (_files, onFileUploaded) => {
      onFileUploaded(readyUploaded, 0);
    });
    const pendingUpload = rs.fn<Upload>((_files, onFileUploaded) =>
      pending.promise.then(() => onFileUploaded(pendingUploaded, 0)),
    );
    const cleanup = rs.fn<(uploaded: UploadedFileInfo) => void>();

    await coordinator.ensure({
      scopeKey: THREAD_A_SCOPE,
      candidates: [{ clientId: "ready-client", file: readyFile }],
      retryPendingFailure: false,
      upload: readyUpload,
    });
    const pendingEnsure = coordinator.ensure({
      scopeKey: THREAD_A_SCOPE,
      candidates: [{ clientId: "pending-client", file: pendingFile }],
      retryPendingFailure: false,
      upload: pendingUpload,
    });

    coordinator.resetScope(THREAD_A_SCOPE, cleanup);

    expect(cleanup).toHaveBeenCalledTimes(1);
    expect(cleanup).toHaveBeenCalledWith(readyUploaded);

    pending.resolve();
    await pendingEnsure.catch(() => undefined);

    expect(cleanup).toHaveBeenCalledTimes(2);
    expect(cleanup).toHaveBeenNthCalledWith(2, pendingUploaded);
  });

  test("defers reset cleanup for a claimed ready upload until release", async () => {
    const coordinator = new AttachmentUploadCoordinator();
    const file = new File(["a"], "claimed-ready.png", {
      type: "image/png",
    });
    const uploaded = uploadedFile(
      "8f31eef3-0662-42c5-809c-3bbbe2c663af",
      file.name,
      file.name,
    );
    const upload = rs.fn<Upload>(async (_files, onFileUploaded) => {
      onFileUploaded(uploaded, 0);
    });
    const cleanup = rs.fn<(uploaded: UploadedFileInfo) => void>();

    await coordinator.ensure({
      scopeKey: THREAD_A_SCOPE,
      candidates: [{ clientId: "claimed-ready", file }],
      retryPendingFailure: false,
      upload,
    });
    expect(coordinator.claim(THREAD_A_SCOPE, ["claimed-ready"])).toBe(true);

    coordinator.resetScope(THREAD_A_SCOPE, cleanup);

    expect(cleanup).not.toHaveBeenCalled();

    coordinator.release(THREAD_A_SCOPE, ["claimed-ready"]);

    expect(cleanup).toHaveBeenCalledTimes(1);
    expect(cleanup).toHaveBeenCalledWith(uploaded);
  });

  test("never cleans a claimed ready upload consumed after reset", async () => {
    const coordinator = new AttachmentUploadCoordinator();
    const file = new File(["a"], "consumed-ready.png", {
      type: "image/png",
    });
    const uploaded = uploadedFile(
      "8f31eef3-0662-42c5-809c-3bbbe2c663af",
      file.name,
      file.name,
    );
    const upload = rs.fn<Upload>(async (_files, onFileUploaded) => {
      onFileUploaded(uploaded, 0);
    });
    const cleanup = rs.fn<(uploaded: UploadedFileInfo) => void>();

    await coordinator.ensure({
      scopeKey: THREAD_A_SCOPE,
      candidates: [{ clientId: "consumed-ready", file }],
      retryPendingFailure: false,
      upload,
    });
    expect(coordinator.claim(THREAD_A_SCOPE, ["consumed-ready"])).toBe(true);
    coordinator.resetScope(THREAD_A_SCOPE, cleanup);

    coordinator.consume(THREAD_A_SCOPE, ["consumed-ready"]);
    coordinator.release(THREAD_A_SCOPE, ["consumed-ready"]);
    await Promise.resolve();

    expect(cleanup).not.toHaveBeenCalled();
  });

  test("cleans a claimed pending upload only after reset and release", async () => {
    const coordinator = new AttachmentUploadCoordinator();
    const file = new File(["a"], "claimed-pending.png", {
      type: "image/png",
    });
    const uploaded = uploadedFile(
      "8f31eef3-0662-42c5-809c-3bbbe2c663af",
      file.name,
      file.name,
    );
    const pending = deferred<void>();
    const upload = rs.fn<Upload>((_files, onFileUploaded) =>
      pending.promise.then(() => onFileUploaded(uploaded, 0)),
    );
    const cleanup = rs.fn<(uploaded: UploadedFileInfo) => void>();
    const eager = coordinator.ensure({
      scopeKey: THREAD_A_SCOPE,
      candidates: [{ clientId: "claimed-pending", file }],
      retryPendingFailure: false,
      upload,
    });

    expect(coordinator.claim(THREAD_A_SCOPE, ["claimed-pending"])).toBe(true);
    coordinator.resetScope(THREAD_A_SCOPE, cleanup);
    expect(cleanup).not.toHaveBeenCalled();

    pending.resolve();
    await expect(eager).resolves.toEqual([uploaded]);
    expect(cleanup).not.toHaveBeenCalled();

    coordinator.release(THREAD_A_SCOPE, ["claimed-pending"]);

    expect(cleanup).toHaveBeenCalledTimes(1);
    expect(cleanup).toHaveBeenCalledWith(uploaded);
  });
});
