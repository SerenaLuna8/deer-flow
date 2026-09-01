import { afterEach, describe, expect, it, rs } from "@rstest/core";

import { createPreviewImageURLs } from "@/core/knowledge/attachment-images";

const PNG_BASE64 =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aQ1sAAAAASUVORK5CYII=";

afterEach(() => {
  rs.restoreAllMocks();
});

describe("preview Knowledge attachment URLs", () => {
  it("releases every preview blob URL exactly once", () => {
    rs.spyOn(URL, "createObjectURL").mockReturnValue("blob:preview-one");
    const revoke = rs
      .spyOn(URL, "revokeObjectURL")
      .mockImplementation(() => undefined);

    const images = createPreviewImageURLs([
      {
        ref: "a".repeat(64),
        media_type: "image/png",
        data_base64: PNG_BASE64,
      },
    ]);

    expect(images.urls.get("a".repeat(64))).toBe("blob:preview-one");
    images.dispose();
    images.dispose();
    expect(revoke).toHaveBeenCalledTimes(1);
  });

  it("releases already-created URLs when a later creation fails", () => {
    const create = rs
      .spyOn(URL, "createObjectURL")
      .mockReturnValueOnce("blob:first")
      .mockImplementationOnce(() => {
        throw new Error("object URL unavailable");
      });
    const revoke = rs
      .spyOn(URL, "revokeObjectURL")
      .mockImplementation(() => undefined);

    expect(() =>
      createPreviewImageURLs([
        {
          ref: "a".repeat(64),
          media_type: "image/png",
          data_base64: PNG_BASE64,
        },
        {
          ref: "b".repeat(64),
          media_type: "image/png",
          data_base64: PNG_BASE64,
        },
      ]),
    ).toThrow("object URL unavailable");
    expect(create).toHaveBeenCalledTimes(2);
    expect(revoke).toHaveBeenCalledExactlyOnceWith("blob:first");
  });
});
