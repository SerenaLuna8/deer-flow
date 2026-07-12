import { describe, expect, test } from "@rstest/core";

import { createInvitationRedemptionCoordinator } from "@/components/projects/invitation-redemption-state";

describe("invitation redemption callback scope", () => {
  test("rejects claim and redeem callbacks from an older user generation", () => {
    const coordinator = createInvitationRedemptionCoordinator();
    const anonymousClaim = coordinator.begin(null);
    expect(coordinator.isCurrent(anonymousClaim, null)).toBe(true);

    const signedInRedeem = coordinator.begin("user-2");
    expect(coordinator.isCurrent(anonymousClaim, "user-2")).toBe(false);
    expect(coordinator.isCurrent(signedInRedeem, "user-2")).toBe(true);

    coordinator.dispose(signedInRedeem);
    expect(coordinator.isCurrent(signedInRedeem, "user-2")).toBe(false);
  });
});
