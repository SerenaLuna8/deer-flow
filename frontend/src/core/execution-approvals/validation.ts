import { z } from "zod";

export const executionApprovalThreadIdSchema = z.string().min(1).max(256);
export const executionApprovalRunIdSchema = z.string().min(1).max(256);
export const executionApprovalIdSchema = z.string().uuid();
