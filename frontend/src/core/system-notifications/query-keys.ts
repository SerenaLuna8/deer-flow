export const systemNotificationKeys = {
  list: (userId: string) =>
    ["account", userId, "system-notifications"] as const,
  mutations: (userId: string) =>
    ["account", userId, "system-notifications", "mutation"] as const,
};
