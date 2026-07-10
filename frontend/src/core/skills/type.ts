export interface Skill {
  name: string;
  description: string;
  category: "public" | "custom" | "legacy";
  license: string | null;
  enabled: boolean;
  editable: boolean;
}

export interface SkillContentResponse {
  content: string;
}
