export interface InvitationRedemptionAttempt {
  readonly userId: string | null;
  readonly generation: number;
}

export interface InvitationRedemptionCoordinator {
  begin: (userId: string | null) => InvitationRedemptionAttempt;
  dispose: (attempt: InvitationRedemptionAttempt) => void;
  isCurrent: (
    attempt: InvitationRedemptionAttempt,
    userId: string | null,
  ) => boolean;
}

export function createInvitationRedemptionCoordinator(): InvitationRedemptionCoordinator {
  let generation = 0;
  let current: InvitationRedemptionAttempt | null = null;

  return {
    begin(userId) {
      const attempt = { userId, generation: ++generation };
      current = attempt;
      return attempt;
    },
    dispose(attempt) {
      if (current !== attempt) return;
      current = null;
      generation += 1;
    },
    isCurrent(attempt, userId) {
      return current === attempt && attempt.userId === userId;
    },
  };
}
