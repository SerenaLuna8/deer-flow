import { z } from "zod";

import {
  invitableProjectRoleSchema,
  invitationStatusSchema,
  projectIdSchema,
} from "@/core/projects/types";

export const systemNotificationProjectSchema = z
  .object({
    id: projectIdSchema,
    slug: z.string().min(1),
    display_name: z.string().min(1),
  })
  .strict();

export const systemNotificationActorSchema = z
  .object({
    email: z.string().email(),
  })
  .strict();

export const systemNotificationSchema = z
  .object({
    id: projectIdSchema,
    kind: z.literal("project_invitation"),
    project: systemNotificationProjectSchema,
    actor: systemNotificationActorSchema,
    role: invitableProjectRoleSchema,
    status: invitationStatusSchema,
    is_read: z.boolean(),
    created_at: z.string().datetime({ offset: true }),
    expires_at: z.string().datetime({ offset: true }),
    version: z.number().int().positive(),
  })
  .strict();

export const systemNotificationPageSchema = z
  .object({
    items: z.array(systemNotificationSchema),
    next_cursor: z.string().min(1).nullable(),
    unread_count: z.number().int().nonnegative(),
  })
  .strict();

export const systemNotificationFiltersSchema = z
  .object({
    cursor: z.string().min(1).optional(),
    limit: z.number().int().min(1).max(100).optional(),
  })
  .strict();

export const markAllSystemNotificationsReadResponseSchema = z
  .object({
    marked_count: z.number().int().nonnegative(),
  })
  .strict();

export type SystemNotification = z.infer<typeof systemNotificationSchema>;
export type SystemNotificationFilters = z.infer<
  typeof systemNotificationFiltersSchema
>;
export type SystemNotificationPage = z.infer<
  typeof systemNotificationPageSchema
>;
export type MarkAllSystemNotificationsReadResponse = z.infer<
  typeof markAllSystemNotificationsReadResponseSchema
>;
