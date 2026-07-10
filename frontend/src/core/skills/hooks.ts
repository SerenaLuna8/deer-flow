import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { enableSkill, loadSkillContent, SkillRequestError } from "./api";

import { loadSkills } from ".";

export function useSkills() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["skills"],
    queryFn: () => loadSkills(),
    retry: (count, err) => !(err instanceof SkillRequestError) && count < 3,
  });
  return { skills: data ?? [], isLoading, error };
}

export function useSkillContent(skillName: string | null, enabled: boolean) {
  return useQuery({
    queryKey: ["skills", "content", skillName],
    queryFn: () => loadSkillContent(skillName!),
    enabled: enabled && skillName !== null,
    retry: (count, err) => !(err instanceof SkillRequestError) && count < 3,
  });
}

export function useEnableSkill() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      skillName,
      enabled,
    }: {
      skillName: string;
      enabled: boolean;
    }) => {
      await enableSkill(skillName, enabled);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["skills"] });
    },
  });
}
